"""Live AWS IAM backend (boto3). Selected with IAM_BACKEND=aws.

Keeps the fixture's four tool contracts and adds evidence tools (policies, service
last accessed, CloudTrail) plus two gated mutations. Deny prefixes and AWS
service-linked roles are refused in code.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any
from urllib.parse import unquote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from trueforge_hackathon.agents.access_reviewer.backends.fixture import (
    deny_prefixes,
    is_denied_principal,
    unused_after_days,
)
from trueforge_hackathon.agents.access_reviewer.policy_lint import lint

_RETRY = Config(retries={"mode": "adaptive", "max_attempts": 8})
SERVICE_LINKED_PATH = "/aws-service-role/"
TELEMETRY_CAVEAT = (
    "IAM service-last-accessed can lag up to ~4 hours; CloudTrail event history lags minutes and keeps 90 days "
    "of management events only. Absence of evidence is weak evidence, not proof of non-use."
)


@lru_cache(maxsize=8)
def _client(service: str, region: str | None = None):
    return boto3.client(service, region_name=region or _home_region(), config=_RETRY)


def _home_region() -> str:
    return os.environ.get("AWS_REGION", "us-west-2")


def _trail_regions() -> list[str]:
    raw = os.environ.get("IAM_CLOUDTRAIL_REGIONS")
    regions = [r.strip() for r in raw.split(",")] if raw else [_home_region(), "us-east-1"]
    return list(dict.fromkeys(r for r in regions if r))


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _code(exc: ClientError) -> str:
    return exc.response["Error"]["Code"]


def _refused(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "refused", "reason": reason, **extra}


def _services_in(doc: dict) -> list[str]:
    services: set[str] = set()
    for s in doc.get("Statement", []) if isinstance(doc.get("Statement"), list) else [doc.get("Statement", {})]:
        if s.get("Effect") != "Allow":
            continue
        actions = s.get("Action", [])
        for a in actions if isinstance(actions, list) else [actions]:
            services.add("*" if a == "*" else a.split(":", 1)[0].lower())
    return sorted(services)


def _policy_document(arn: str) -> dict:
    iam = _client("iam")
    version = iam.get_policy(PolicyArn=arn)["Policy"]["DefaultVersionId"]
    doc = iam.get_policy_version(PolicyArn=arn, VersionId=version)["PolicyVersion"]["Document"]
    return json.loads(unquote(doc)) if isinstance(doc, str) else doc


class AwsIamBackend:
    name = "aws"

    # ---------------------------------------------------------------- lookup

    def _role(self, name: str) -> dict | None:
        try:
            return _client("iam").get_role(RoleName=name)["Role"]
        except ClientError as exc:
            if _code(exc) == "NoSuchEntity":
                return None
            raise

    def _guard(self, role: dict | None, name: str) -> dict | None:
        if role is None:
            return _refused(f"unknown role {name}")
        if is_denied_principal(role["RoleName"]):
            return _refused(f"{role['RoleName']} matches deny list ({', '.join(deny_prefixes())})")
        if role.get("Path", "/").startswith(SERVICE_LINKED_PATH):
            return _refused("AWS service-linked roles are never modified")
        return None

    # ---------------------------------------------------------------- existing contract

    def list_principals(self) -> dict:
        iam = _client("iam")
        account = _client("sts").get_caller_identity()["Account"]
        principals = []
        for page in iam.get_paginator("list_roles").paginate():
            for r in page["Roles"]:
                if r.get("Path", "/").startswith(SERVICE_LINKED_PATH):
                    continue
                attached = iam.list_attached_role_policies(RoleName=r["RoleName"])["AttachedPolicies"]
                principals.append(
                    {
                        "id": r["RoleId"],
                        "name": r["RoleName"],
                        "kind": "role",
                        "description": r.get("Description"),
                        "createdAt": _iso(r["CreateDate"]),
                        "policyCount": len(attached),
                        "denied": is_denied_principal(r["RoleName"]),
                    }
                )
        return {
            "account": account,
            "backend": "aws",
            "denyPrincipalPrefixes": deny_prefixes(),
            "note": "AWS service-linked roles are hidden and never modified.",
            "principals": principals,
        }

    def get_principal_policies(self, principal: str) -> dict:
        role = self._role(principal)
        if role is None:
            return {"error": f"Unknown principal: {principal}"}
        iam = _client("iam")
        tags = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName=principal)["Tags"]}
        last = role.get("RoleLastUsed") or {}
        policies = []
        for p in iam.list_attached_role_policies(RoleName=principal)["AttachedPolicies"]:
            policies.append(
                {
                    "name": p["PolicyName"],
                    "arn": p["PolicyArn"],
                    "type": "managed",
                    "services": _services_in(_policy_document(p["PolicyArn"])),
                }
            )
        for name in iam.list_role_policies(RoleName=principal)["PolicyNames"]:
            doc = iam.get_role_policy(RoleName=principal, PolicyName=name)["PolicyDocument"]
            policies.append({"name": name, "arn": None, "type": "inline", "services": _services_in(doc)})
        return {
            "id": role["RoleId"],
            "name": role["RoleName"],
            "kind": "role",
            "arn": role["Arn"],
            "tags": tags,
            "denied": is_denied_principal(role["RoleName"]),
            "roleLastUsed": {"at": _iso(last.get("LastUsedDate")), "region": last.get("Region")},
            "policies": policies,
        }

    def get_access_last_used(self, principal: str | None = None) -> dict:
        window = unused_after_days()
        cutoff = datetime.now(timezone.utc) - timedelta(days=window)
        names = [principal] if principal else [p["name"] for p in self.list_principals()["principals"]]
        rows: list[dict] = []
        for name in names:
            view = self.get_principal_policies(name)
            if "error" in view:
                continue
            services = {s["service"]: s for s in self.get_service_last_accessed(name).get("services", [])}
            for pol in view["policies"]:
                used = [services[s]["lastAuthenticated"] for s in pol["services"] if services.get(s, {}).get("lastAuthenticated")]
                last_used = max(used) if used else None
                unused_services = [
                    s for s in pol["services"]
                    if not services.get(s, {}).get("lastAuthenticated")
                    or datetime.fromisoformat(services[s]["lastAuthenticated"]) < cutoff
                ]
                rows.append(
                    {
                        "principal": name,
                        "kind": "role",
                        "policy": pol["name"],
                        "arn": pol["arn"],
                        "lastUsedAt": last_used,
                        "unused": last_used is None or datetime.fromisoformat(last_used) < cutoff,
                        "unusedServices": unused_services,
                        "blastRadius": f"Removes {', '.join(pol['services'])} permissions granted by {pol['name']}.",
                        "deniedPrincipal": view["denied"],
                    }
                )
        return {"unusedAfterDays": window, "attachments": rows, "caveat": TELEMETRY_CAVEAT}

    def revoke_access(self, principal: str, policy: str) -> dict:
        role = self._role(principal)
        refused = self._guard(role, principal)
        if refused:
            return {"error": f"Refused: {refused['reason']}"}
        attached = _client("iam").list_attached_role_policies(RoleName=principal)["AttachedPolicies"]
        match = next((p for p in attached if policy in (p["PolicyName"], p["PolicyArn"])), None)
        if match is None:
            return {"error": f"Policy {policy} is not attached to {principal}"}
        _client("iam").detach_role_policy(RoleName=principal, PolicyArn=match["PolicyArn"])
        remaining = _client("iam").list_attached_role_policies(RoleName=principal)["AttachedPolicies"]
        return {
            "revoked": True,
            "principal": principal,
            "detached": {"name": match["PolicyName"], "arn": match["PolicyArn"]},
            "remainingPolicies": [p["PolicyName"] for p in remaining],
        }

    # ---------------------------------------------------------------- evidence tools

    def get_role_policies(self, role_name: str) -> dict:
        role = self._role(role_name)
        if role is None:
            return {"error": f"Unknown role: {role_name}"}
        iam = _client("iam")
        managed = []
        for p in iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]:
            managed.append({"name": p["PolicyName"], "arn": p["PolicyArn"], "document": _policy_document(p["PolicyArn"])})
        inline = []
        for name in iam.list_role_policies(RoleName=role_name)["PolicyNames"]:
            inline.append({"name": name, "document": iam.get_role_policy(RoleName=role_name, PolicyName=name)["PolicyDocument"]})
        trust = role["AssumeRolePolicyDocument"]
        return {
            "role": role_name,
            "arn": role["Arn"],
            "denied": is_denied_principal(role_name),
            "service_linked": role.get("Path", "/").startswith(SERVICE_LINKED_PATH),
            "trust_policy": json.loads(unquote(trust)) if isinstance(trust, str) else trust,
            "managed": managed,
            "inline": inline,
        }

    def get_service_last_accessed(self, role_name: str, timeout_s: int = 60) -> dict:
        role = self._role(role_name)
        if role is None:
            return {"error": f"Unknown role: {role_name}"}
        iam = _client("iam")
        job = iam.generate_service_last_accessed_details(Arn=role["Arn"], Granularity="ACTION_LEVEL")["JobId"]
        deadline = time.time() + timeout_s
        while True:
            res = iam.get_service_last_accessed_details(JobId=job)
            if res["JobStatus"] != "IN_PROGRESS" or time.time() > deadline:
                break
            time.sleep(1.5)
        if res["JobStatus"] != "COMPLETED":
            return {"role": role_name, "job_status": res["JobStatus"], "error": res.get("Error"), "caveat": TELEMETRY_CAVEAT}
        services = list(res["ServicesLastAccessed"])
        while res.get("IsTruncated"):
            res = iam.get_service_last_accessed_details(JobId=job, Marker=res["Marker"])
            services.extend(res["ServicesLastAccessed"])
        rows = []
        for s in services:
            tracked = [
                {"action": f"{s['ServiceNamespace']}:{a['ActionName']}", "lastAccessed": _iso(a.get("LastAccessedTime"))}
                for a in s.get("TrackedActionsLastAccessed", [])
                if a.get("LastAccessedTime")
            ]
            rows.append(
                {
                    "service": s["ServiceNamespace"],
                    "serviceName": s["ServiceName"],
                    "lastAuthenticated": _iso(s.get("LastAuthenticated")),
                    "lastAuthenticatedRegion": s.get("LastAuthenticatedRegion"),
                    "trackedActionsUsed": tracked,
                }
            )
        return {
            "role": role_name,
            "job_status": "COMPLETED",
            "generated_at": _iso(res.get("JobCompletionDate")),
            "services_granted": len(rows),
            "services_used": sum(1 for r in rows if r["lastAuthenticated"]),
            "services": rows,
            "caveat": TELEMETRY_CAVEAT,
        }

    def get_role_cloudtrail_activity(self, role_name: str, lookback_hours: int = 168) -> dict:
        role = self._role(role_name)
        if role is None:
            return {"error": f"Unknown role: {role_name}"}
        hours = max(1, min(int(lookback_hours), 90 * 24))
        start = datetime.now(timezone.utc) - timedelta(hours=hours)
        sessions: dict[str, dict] = {}
        for region in _trail_regions():
            ct = _client("cloudtrail", region)
            for e in ct.lookup_events(
                LookupAttributes=[{"AttributeKey": "ResourceName", "AttributeValue": role["Arn"]}],
                StartTime=start,
                MaxResults=50,
            )["Events"]:
                if e["EventName"] != "AssumeRole":
                    continue
                detail = json.loads(e["CloudTrailEvent"])
                key = ((detail.get("responseElements") or {}).get("credentials") or {}).get("accessKeyId")
                if key:
                    sessions[key] = {
                        "assumed_at": _iso(e["EventTime"]),
                        "session_name": (detail.get("requestParameters") or {}).get("roleSessionName"),
                        "assumed_by": (detail.get("userIdentity") or {}).get("arn"),
                    }
        calls: dict[str, dict] = {}
        for key in list(sessions)[:10]:
            for region in _trail_regions():
                for e in _client("cloudtrail", region).lookup_events(
                    LookupAttributes=[{"AttributeKey": "AccessKeyId", "AttributeValue": key}],
                    StartTime=start,
                    MaxResults=50,
                )["Events"]:
                    action = f"{e.get('EventSource', '').split('.')[0]}:{e['EventName']}"
                    row = calls.setdefault(action, {"action": action, "count": 0, "last_seen": None, "regions": set()})
                    row["count"] += 1
                    row["regions"].add(region)
                    t = _iso(e["EventTime"])
                    row["last_seen"] = max(filter(None, [row["last_seen"], t]))
        return {
            "role": role_name,
            "lookback_hours": hours,
            "regions_searched": _trail_regions(),
            "sessions": list(sessions.values()),
            "api_calls": [{**r, "regions": sorted(r["regions"])} for r in sorted(calls.values(), key=lambda r: -r["count"])],
            "note": "api_calls use CloudTrail event names (e.g. s3:ListBuckets); IAM action names can differ "
            "(s3:ListAllMyBuckets). Map them before building a policy.",
            "caveat": TELEMETRY_CAVEAT,
        }

    # ---------------------------------------------------------------- gated mutations

    def detach_role_policy(self, role_name: str, policy_arn: str) -> dict:
        role = self._role(role_name)
        refused = self._guard(role, role_name)
        if refused:
            return refused
        attached = _client("iam").list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]
        if not any(p["PolicyArn"] == policy_arn for p in attached):
            return {"status": "already_absent", "role": role_name, "policy_arn": policy_arn}
        _client("iam").detach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
        remaining = _client("iam").list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]
        return {
            "status": "detached",
            "role": role_name,
            "policy_arn": policy_arn,
            "remaining_managed": [p["PolicyArn"] for p in remaining],
            "verify_with": "get_role_policies",
        }

    def put_role_policy(self, role_name: str, policy_name: str, policy_json: str) -> dict:
        role = self._role(role_name)
        refused = self._guard(role, role_name)
        if refused:
            return refused
        try:
            new = json.loads(policy_json)
        except json.JSONDecodeError as exc:
            return _refused(f"policy_json is not valid JSON: {exc}")
        current = self.get_role_policies(role_name)
        union = {
            "Version": "2012-10-17",
            "Statement": [
                s
                for p in current["managed"] + current["inline"]
                for s in (p["document"]["Statement"] if isinstance(p["document"]["Statement"], list) else [p["document"]["Statement"]])
            ],
        }
        problems = lint(new, union)
        if problems:
            return _refused("policy failed lint", problems=problems)
        existing = next((p for p in current["inline"] if p["name"] == policy_name), None)
        if existing and existing["document"] == new:
            return {"status": "already_applied", "role": role_name, "policy_name": policy_name}
        _client("iam").put_role_policy(RoleName=role_name, PolicyName=policy_name, PolicyDocument=json.dumps(new))
        return {"status": "applied", "role": role_name, "policy_name": policy_name, "verify_with": "get_role_policies"}
