from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

PrincipalKind = Literal["user", "role"]


@dataclass
class AttachedPolicy:
    name: str
    arn: str
    unused_services: list[str]
    last_used_at: str | None
    blast_radius: str


@dataclass
class Principal:
    id: str
    name: str
    kind: PrincipalKind
    tags: dict[str, str]
    policies: list[AttachedPolicy] = field(default_factory=list)


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _seed() -> list[Principal]:
    return [
        Principal(
            id="AIDATESTUNUSED01",
            name="ci-bot",
            kind="user",
            tags={"hackathon": "true", "team": "platform"},
            policies=[
                AttachedPolicy(
                    name="AmazonS3FullAccess",
                    arn="arn:aws:iam::aws:policy/AmazonS3FullAccess",
                    unused_services=["s3"],
                    last_used_at=_days_ago(214),
                    blast_radius=(
                        "CI can no longer upload build artifacts to s3://demo-ci-artifacts. "
                        "Deploy pipeline fails until a tighter s3:PutObject policy is attached."
                    ),
                ),
                AttachedPolicy(
                    name="AmazonEC2ReadOnlyAccess",
                    arn="arn:aws:iam::aws:policy/AmazonEC2ReadOnlyAccess",
                    unused_services=[],
                    last_used_at=_days_ago(3),
                    blast_radius="Read-only EC2 describe calls used by the health check.",
                ),
            ],
        ),
        Principal(
            id="AROAOLDANALYTICS",
            name="legacy-analytics-role",
            kind="role",
            tags={"hackathon": "true", "team": "data"},
            policies=[
                AttachedPolicy(
                    name="AmazonDynamoDBFullAccess",
                    arn="arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess",
                    unused_services=["dynamodb"],
                    last_used_at=_days_ago(401),
                    blast_radius=(
                        "No production writer assumes this role today. Revoking stops a "
                        "forgotten nightly Glue job if it is re-enabled."
                    ),
                ),
                AttachedPolicy(
                    name="AWSLambda_FullAccess",
                    arn="arn:aws:iam::aws:policy/AWSLambda_FullAccess",
                    unused_services=["lambda"],
                    last_used_at=None,
                    blast_radius=(
                        "Never used. Detach is low risk. Confirm no event source still "
                        "points at functions this role could mutate."
                    ),
                ),
            ],
        ),
        Principal(
            id="AIDAONCALLUSER01",
            name="oncall-readonly",
            kind="user",
            tags={"hackathon": "true", "team": "sre"},
            policies=[
                AttachedPolicy(
                    name="CloudWatchReadOnlyAccess",
                    arn="arn:aws:iam::aws:policy/CloudWatchReadOnlyAccess",
                    unused_services=[],
                    last_used_at=_days_ago(1),
                    blast_radius="On-call dashboards lose CloudWatch reads.",
                ),
            ],
        ),
        Principal(
            id="AROAADMINBREAKGLASS",
            name="BreakGlassAdmin",
            kind="role",
            tags={"emergency": "true"},
            policies=[
                AttachedPolicy(
                    name="AdministratorAccess",
                    arn="arn:aws:iam::aws:policy/AdministratorAccess",
                    unused_services=[],
                    last_used_at=_days_ago(12),
                    blast_radius="Emergency admin. MCP policy must refuse this principal.",
                ),
            ],
        ),
    ]


_principals = _seed()


def unused_after_days() -> int:
    try:
        return int(os.environ.get("UNUSED_AFTER_DAYS", "90"))
    except ValueError:
        return 90


def is_unused(last_used_at: str | None, window_days: int | None = None) -> bool:
    if not last_used_at:
        return True
    window = unused_after_days() if window_days is None else window_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=window)
    return datetime.fromisoformat(last_used_at) < cutoff


def deny_prefixes() -> list[str]:
    raw = os.environ.get(
        "DENY_PRINCIPAL_PREFIXES",
        "Admin,BreakGlass,OrganizationAccountAccessRole",
    )
    return [part.strip() for part in raw.split(",") if part.strip()]


def is_denied_principal(name: str) -> bool:
    lower = name.lower()
    return any(lower.startswith(prefix.lower()) for prefix in deny_prefixes())


def list_principals() -> list[dict]:
    return [
        {
            "id": p.id,
            "name": p.name,
            "kind": p.kind,
            "tags": p.tags,
            "policyCount": len(p.policies),
        }
        for p in _principals
    ]


def get_principal(name_or_id: str) -> Principal | None:
    return next((p for p in _principals if p.name == name_or_id or p.id == name_or_id), None)


def revoke_access(principal_name: str, policy_name: str) -> dict:
    principal = get_principal(principal_name)
    if principal is None:
        return {"ok": False, "error": f"Unknown principal: {principal_name}"}
    if is_denied_principal(principal.name):
        return {
            "ok": False,
            "error": f"Refused: {principal.name} matches deny list ({', '.join(deny_prefixes())}).",
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
    detached = principal.policies.pop(index)
    return {"ok": True, "principal": principal, "detached": detached}


class FixtureBackend:
    """In-memory IAM fixture. Returns the exact payloads the MCP tools always returned.

    `src` is any module exposing list_principals / get_principal / revoke_access with the
    fixture's Principal shape (the advisor backend passes aws_iam).
    """

    name = "fixture"

    def __init__(self, src=None, name: str = "fixture", account=None) -> None:
        import sys

        self._src = src or sys.modules[__name__]
        self.name = name
        self._account = account or (lambda: "fixture")

    def list_principals(self) -> dict:
        list_principals = self._src.list_principals
        return {
            "account": self._account(),
            "denyPrincipalPrefixes": deny_prefixes(),
            "principals": list_principals(),
        }

    def get_principal_policies(self, principal: str) -> dict:
        found = self._src.get_principal(principal)
        if found is None:
            return {"error": f"Unknown principal: {principal}"}
        return {
            "id": found.id,
            "name": found.name,
            "kind": found.kind,
            "tags": found.tags,
            "denied": is_denied_principal(found.name),
            "policies": [
                {
                    "name": policy.name,
                    "arn": policy.arn,
                    "lastUsedAt": policy.last_used_at,
                    "unusedServices": policy.unused_services,
                    "blastRadius": policy.blast_radius,
                }
                for policy in found.policies
            ],
        }

    def get_access_last_used(self, principal: str | None = None) -> dict:
        window = unused_after_days()
        get_principal, list_principals = self._src.get_principal, self._src.list_principals
        targets = [get_principal(principal)] if principal else [get_principal(row["name"]) for row in list_principals()]
        rows: list[dict] = []
        for found in targets:
            if found is None:
                continue
            for policy in found.policies:
                rows.append(
                    {
                        "principal": found.name,
                        "kind": found.kind,
                        "policy": policy.name,
                        "arn": policy.arn,
                        "lastUsedAt": policy.last_used_at,
                        "unused": is_unused(policy.last_used_at, window),
                        "unusedServices": policy.unused_services,
                        "blastRadius": policy.blast_radius,
                        "deniedPrincipal": is_denied_principal(found.name),
                    }
                )
        return {"unusedAfterDays": window, "attachments": rows}

    def revoke_access(self, principal: str, policy: str) -> dict:
        result = self._src.revoke_access(principal, policy)
        if not result.get("ok"):
            return {"error": result.get("error")}
        detached = result["detached"]
        found = result["principal"]
        return {
            "revoked": True,
            "principal": found.name,
            "detached": {
                "name": detached.name,
                "arn": detached.arn,
                "lastUsedAt": detached.last_used_at,
                "unusedServices": detached.unused_services,
                "blastRadius": detached.blast_radius,
            },
            "remainingPolicies": [item.name for item in found.policies],
        }
