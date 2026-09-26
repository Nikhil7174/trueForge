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


def use_aws() -> bool:
    return bool(os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE"))


def demo_revoke_principal() -> str:
    return os.environ.get("DEMO_REVOKE_PRINCIPAL", "tf-hackathon-throwaway").strip()


def review_only_principals() -> list[str]:
    raw = os.environ.get("ACCESS_REVIEWER_ONLY_PRINCIPALS", "").strip()
    return [part.strip() for part in raw.split(",") if part.strip()]


def backend_name() -> str:
    return "aws" if use_aws() else "fixture"


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


def access_status(last_used_at: str | None, window_days: int | None = None) -> str:
    return "inactive" if is_unused(last_used_at, window_days) else "active"


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
    if use_aws():
        from trueforge_hackathon.agents.access_reviewer.aws_iam import list_principals as aws_list

        return aws_list()
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
    if use_aws():
        from trueforge_hackathon.agents.access_reviewer.aws_iam import get_principal as aws_get

        return aws_get(name_or_id)
    return next((p for p in _principals if p.name == name_or_id or p.id == name_or_id), None)


def revoke_access(principal_name: str, policy_name: str) -> dict:
    if use_aws():
        from trueforge_hackathon.agents.access_reviewer.aws_iam import revoke_access as aws_revoke

        return aws_revoke(principal_name, policy_name)
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
