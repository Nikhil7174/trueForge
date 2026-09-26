from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from trueforge_hackathon.agents.access_reviewer.store import (
    AttachedPolicy,
    Principal,
    deny_prefixes,
    is_denied_principal,
    review_only_principals,
    is_unused,
    unused_after_days,
)

_advisor_cache: dict[str, list[dict[str, Any]]] = {}
_account_id: str | None = None


def demo_revoke_principal() -> str:
    return os.environ.get("DEMO_REVOKE_PRINCIPAL", "tf-hackathon-throwaway").strip()


def _visible(name: str) -> bool:
    allowed = review_only_principals()
    if not allowed:
        return True
    return any(name.lower() == item.lower() for item in allowed)


def _session():
    kwargs: dict[str, str] = {}
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    profile = os.environ.get("AWS_PROFILE")
    if region:
        kwargs["region_name"] = region
    if profile:
        return boto3.Session(profile_name=profile, **kwargs)
    return boto3.Session(**kwargs)


def _iam():
    return _session().client("iam")


def _sts():
    return _session().client("sts")


def account_id() -> str:
    global _account_id
    if _account_id:
        return _account_id
    _account_id = _sts().get_caller_identity()["Account"]
    return _account_id


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _paginate(client, method: str, key: str, **kwargs) -> list[dict]:
    paginator = client.get_paginator(method)
    rows: list[dict] = []
    for page in paginator.paginate(**kwargs):
        rows.extend(page.get(key, []))
    return rows


def _tags(client, *, user: str | None = None, role: str | None = None) -> dict[str, str]:
    try:
        if user:
            raw = client.list_user_tags(UserName=user).get("Tags", [])
        else:
            raw = client.list_role_tags(RoleName=role).get("Tags", [])
    except ClientError:
        return {}
    return {item["Key"]: item["Value"] for item in raw}


def _is_service_linked(path: str) -> bool:
    return path.startswith("/aws-service-role/")


def _policy_document(client, arn: str) -> dict[str, Any] | None:
    try:
        policy = client.get_policy(PolicyArn=arn)["Policy"]
        version = client.get_policy_version(
            PolicyArn=arn,
            VersionId=policy["DefaultVersionId"],
        )["PolicyVersion"]["Document"]
    except ClientError:
        return None
    if isinstance(version, str):
        return json.loads(version)
    return version


def _services_from_policy_doc(doc: dict[str, Any] | None) -> list[str]:
    if not doc:
        return []
    statements = doc.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    services: set[str] = set()
    for stmt in statements:
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        for action in actions:
            if action == "*":
                services.add("*")
            elif ":" in str(action):
                services.add(str(action).split(":", 1)[0].lower())
    return sorted(services)


def _service_last_accessed(client, arn: str) -> list[dict[str, Any]]:
    cached = _advisor_cache.get(arn)
    if cached is not None:
        return cached
    job_id = client.generate_service_last_accessed_details(Arn=arn)["JobId"]
    details: list[dict[str, Any]] = []
    for _ in range(40):
        out = client.get_service_last_accessed_details(JobId=job_id)
        status = out.get("JobStatus")
        if status == "COMPLETED":
            details = out.get("ServicesLastAccessed", [])
            break
        if status == "FAILED":
            break
        time.sleep(1)
    _advisor_cache[arn] = details
    return details


def _blast_radius(principal: str, policy: str, unused_services: list[str]) -> str:
    if unused_services:
        services = ", ".join(unused_services)
        return (
            f"Detach {policy} from {principal} stops that identity from calling "
            f"these unused services: {services}."
        )
    return f"Detach {policy} from {principal} stops that identity from using this managed policy."


def _policy_usage(
    advisor: list[dict[str, Any]],
    services: list[str],
) -> tuple[str | None, list[str]]:
    by_ns = {
        str(row.get("ServiceNamespace", "")).lower(): row for row in advisor if row.get("ServiceNamespace")
    }
    if not services or "*" in services:
        timestamps = [_iso(row.get("LastAuthenticated")) for row in advisor]
        last_used = max((ts for ts in timestamps if ts), default=None)
        unused = [
            str(row.get("ServiceNamespace"))
            for row in advisor
            if is_unused(_iso(row.get("LastAuthenticated")))
        ]
        return last_used, unused
    last_candidates: list[str] = []
    unused: list[str] = []
    for service in services:
        row = by_ns.get(service)
        ts = _iso(row.get("LastAuthenticated")) if row else None
        if ts:
            last_candidates.append(ts)
        if is_unused(ts):
            unused.append(service)
    last_used = max(last_candidates, default=None)
    return last_used, unused


def _attached_policies(client, *, user: str | None = None, role: str | None = None) -> list[dict]:
    if user:
        return _paginate(client, "list_attached_user_policies", "AttachedPolicies", UserName=user)
    return _paginate(client, "list_attached_role_policies", "AttachedPolicies", RoleName=role)


def _principal_from_aws(
    client,
    *,
    name: str,
    principal_id: str,
    kind: str,
    arn: str,
    tags: dict[str, str],
) -> Principal:
    attached = _attached_policies(client, user=name if kind == "user" else None, role=name if kind == "role" else None)
    advisor = _service_last_accessed(client, arn) if attached else []
    policies: list[AttachedPolicy] = []
    for item in attached:
        policy_arn = item["PolicyArn"]
        policy_name = item["PolicyName"]
        services = _services_from_policy_doc(_policy_document(client, policy_arn))
        last_used, unused = _policy_usage(advisor, services)
        policies.append(
            AttachedPolicy(
                name=policy_name,
                arn=policy_arn,
                unused_services=unused,
                last_used_at=last_used,
                blast_radius=_blast_radius(name, policy_name, unused),
            )
        )
    return Principal(id=principal_id, name=name, kind=kind, tags=tags, policies=policies)  # type: ignore[arg-type]


def list_principals() -> list[dict]:
    client = _iam()
    rows: list[dict] = []
    for user in _paginate(client, "list_users", "Users"):
        rows.append(
            {
                "id": user["UserId"],
                "name": user["UserName"],
                "kind": "user",
                "tags": {},
                "policyCount": None,
            }
        )
    for role in _paginate(client, "list_roles", "Roles"):
        if _is_service_linked(role.get("Path", "/")):
            continue
        rows.append(
            {
                "id": role["RoleId"],
                "name": role["RoleName"],
                "kind": "role",
                "tags": {},
                "policyCount": None,
            }
        )
    return [row for row in rows if _visible(row["name"])]


def get_principal(name_or_id: str) -> Principal | None:
    if not _visible(name_or_id):
        return None
    client = _iam()
    try:
        user = client.get_user(UserName=name_or_id)["User"]
        return _principal_from_aws(
            client,
            name=user["UserName"],
            principal_id=user["UserId"],
            kind="user",
            arn=user["Arn"],
            tags=_tags(client, user=user["UserName"]),
        )
    except ClientError:
        pass
    try:
        role = client.get_role(RoleName=name_or_id)["Role"]
        return _principal_from_aws(
            client,
            name=role["RoleName"],
            principal_id=role["RoleId"],
            kind="role",
            arn=role["Arn"],
            tags=_tags(client, role=role["RoleName"]),
        )
    except ClientError:
        pass
    return None


def _allowed_to_revoke(name: str) -> bool:
    allowed = demo_revoke_principal()
    return bool(allowed) and name.lower() == allowed.lower()


def revoke_access(principal_name: str, policy_name: str) -> dict:
    principal = get_principal(principal_name)
    if principal is None:
        return {"ok": False, "error": f"Unknown principal: {principal_name}"}
    if is_denied_principal(principal.name):
        return {
            "ok": False,
            "error": f"Refused: {principal.name} matches deny list ({', '.join(deny_prefixes())}).",
        }
    if not _allowed_to_revoke(principal.name):
        return {
            "ok": False,
            "error": (
                f"Refused: live AWS revoke is allowed only for {demo_revoke_principal()!r}. "
                f"{principal.name} is not the allowed demo principal."
            ),
        }
    if "admin" in policy_name.lower() or policy_name.lower().startswith("administrator"):
        return {
            "ok": False,
            "error": f"Refused: will not detach {policy_name!r} from {principal.name}.",
        }
    index = next(
        (
            i
            for i, policy in enumerate(principal.policies)
            if policy.name == policy_name or policy.arn == policy_name
        ),
        None,
    )
    if index is None:
        return {"ok": False, "error": f"Policy {policy_name} is not attached to {principal.name}"}
    detached = principal.policies[index]
    client = _iam()
    try:
        if principal.kind == "user":
            client.detach_user_policy(UserName=principal.name, PolicyArn=detached.arn)
        else:
            client.detach_role_policy(RoleName=principal.name, PolicyArn=detached.arn)
    except ClientError as exc:
        return {"ok": False, "error": str(exc)}
    principal.policies.pop(index)
    _advisor_cache.pop(
        f"arn:aws:iam::{account_id()}:{'user' if principal.kind == 'user' else 'role'}/{principal.name}",
        None,
    )
    return {"ok": True, "principal": principal, "detached": detached}
