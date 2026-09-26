from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from trueforge_hackathon.env import load_env_file
from trueforge_hackathon.agents.access_reviewer.store import (
    backend_name,
    deny_prefixes,
    demo_revoke_principal,
    get_principal,
    is_denied_principal,
    access_status,
    is_unused,
    list_principals,
    revoke_access,
    unused_after_days,
    use_aws,
)

mcp = MCPServer(
    "access-reviewer-iam",
    instructions="IAM read tools plus one destructive revoke. Revoke is paused by TrueForge.",
)


def _dump(payload: Any) -> str:
    return json.dumps(payload, default=lambda o: getattr(o, "__dict__", str(o)), indent=2)


from trueforge_hackathon.agents.access_reviewer.backends import backend_name as iam_backend, get_backend  # noqa: E402

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


def _live() -> bool:
    """IAM_BACKEND=aws: the CloudTrail/lint backend (backends/aws.py). Otherwise the store/Access Advisor path below."""
    return iam_backend() == "aws"


def _call(method: str, *args: Any, **kwargs: Any) -> str:
    backend = get_backend()
    fn = getattr(backend, method, None)
    if fn is None:
        return _dump({"error": f"{method} requires IAM_BACKEND=aws (current backend: {backend.name})"})
    try:
        return _dump(fn(*args, **kwargs))
    except Exception as exc:  # noqa: BLE001  surface AWS errors to the agent as data
        code = exc.response.get("Error", {}).get("Code") if hasattr(exc, "response") else None
        return _dump({"error": f"{type(exc).__name__}: {exc}", "aws_error_code": code})


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    payload: dict[str, Any] = {
        "ok": True,
        "mcp": "access-reviewer-iam",
        "backend": backend_name(),
        "demoRevokePrincipal": demo_revoke_principal() if use_aws() else None,
    }
    if use_aws():
        from trueforge_hackathon.agents.access_reviewer.aws_iam import account_id

        payload["account"] = account_id()
    return JSONResponse(payload)


@mcp.tool(
    name="list_principals",
    description="List IAM users and roles in the current account (fixture or live). Read-only.",
    annotations=ToolAnnotations(
        title="List principals",
        read_only_hint=True,
        destructive_hint=False,
        open_world_hint=False,
    ),
)
def list_principals_tool() -> str:
    if _live():
        return _call("list_principals")
    account: Any = "fixture"
    if use_aws():
        from trueforge_hackathon.agents.access_reviewer.aws_iam import account_id

        account = account_id()
    return _dump(
        {
            "account": account,
            "backend": backend_name(),
            "denyPrincipalPrefixes": deny_prefixes(),
            "demoRevokePrincipal": demo_revoke_principal() if use_aws() else None,
            "principals": list_principals(),
        }
    )


@mcp.tool(
    name="get_principal_policies",
    description="Get attached policies for one IAM user or role. Read-only.",
    annotations=ToolAnnotations(
        title="Get principal policies",
        read_only_hint=True,
        destructive_hint=False,
        open_world_hint=False,
    ),
)
def get_principal_policies(principal: str) -> str:
    if _live():
        return _call("get_principal_policies", principal)
    found = get_principal(principal)
    if found is None:
        return _dump({"error": f"Unknown principal: {principal}"})
    return _dump(
        {
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
                    "status": access_status(policy.last_used_at),
                    "unused": is_unused(policy.last_used_at),
                    "unusedServices": policy.unused_services,
                    "blastRadius": policy.blast_radius,
                }
                for policy in found.policies
            ],
        }
    )


@mcp.tool(
    name="get_access_last_used",
    description="Return last-used timestamps split into active (used within the window) and inactive (unused). Read-only.",
    annotations=ToolAnnotations(
        title="Get access last used",
        read_only_hint=True,
        destructive_hint=False,
        open_world_hint=False,
    ),
)
def get_access_last_used(principal: str | None = None) -> str:
    if _live():
        return _call("get_access_last_used", principal)
    window = unused_after_days()
    if principal:
        targets = [get_principal(principal)]
    else:
        # Unscoped last-used is users only. Roles are fetched when named.
        # Access Advisor is a job per principal and is too slow for every role.
        targets = [
            get_principal(row["name"])
            for row in list_principals()
            if row.get("kind") == "user"
        ]
    rows: list[dict[str, Any]] = []
    for found in targets:
        if found is None:
            continue
        for policy in found.policies:
            unused = is_unused(policy.last_used_at, window)
            rows.append(
                {
                    "principal": found.name,
                    "kind": found.kind,
                    "policy": policy.name,
                    "arn": policy.arn,
                    "lastUsedAt": policy.last_used_at,
                    "status": "inactive" if unused else "active",
                    "unused": unused,
                    "unusedServices": policy.unused_services,
                    "blastRadius": policy.blast_radius,
                    "deniedPrincipal": is_denied_principal(found.name),
                }
            )
    active = [row for row in rows if row["status"] == "active"]
    inactive = [row for row in rows if row["status"] == "inactive"]
    return _dump(
        {
            "unusedAfterDays": window,
            "active": active,
            "inactive": inactive,
            "attachments": rows,
        }
    )


@mcp.tool(
    name="revoke_access",
    description="Detach a managed policy from a principal. Irreversible for this session. Requires human approval.",
    annotations=ToolAnnotations(
        title="Revoke access",
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=False,
    ),
)
def revoke_access_tool(principal: str, policy: str) -> str:
    if _live():
        return _call("revoke_access", principal, policy)
    result = revoke_access(principal, policy)
    if not result.get("ok"):
        return _dump({"error": result.get("error")})
    detached = result["detached"]
    found = result["principal"]
    return _dump(
        {
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
    )


# ------------------------------------------------------------------ evidence (live AWS backend)


@mcp.tool(
    name="get_role_policies",
    description="Full policy documents for one role: attached managed policies (default version), inline policies "
    "and the trust policy. Read-only.",
    annotations=READ,
)
def get_role_policies(role: str) -> str:
    return _call("get_role_policies", role)


@mcp.tool(
    name="get_service_last_accessed",
    description="IAM service-last-accessed report for one role (generate, poll, get; action-level where AWS tracks it). "
    "Can lag up to ~4 hours. Read-only.",
    annotations=READ,
)
def get_service_last_accessed(role: str) -> str:
    return _call("get_service_last_accessed", role)


@mcp.tool(
    name="get_role_cloudtrail_activity",
    description="CloudTrail evidence for one role: AssumeRole sessions in the lookback window and the API calls each "
    "session made (by access key). Lags minutes; 90-day maximum. Read-only.",
    annotations=READ,
)
def get_role_cloudtrail_activity(role: str, lookback_hours: int = 168) -> str:
    return _call("get_role_cloudtrail_activity", role, lookback_hours)


# ------------------------------------------------------------------ destructive (approval-gated by name)


@mcp.tool(
    name="detach_role_policy",
    description="DESTRUCTIVE. Detach one managed policy from a role. Every argument is shown to the approver. "
    "Refused for deny-listed and service-linked roles; already-detached returns already_absent.",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False),
)
def detach_role_policy(
    role: str,
    policy_arn: str,
    services_lost: list[str],
    actions_lost_count: int,
    blast_radius: str,
    rollback: str,
    reason: str,
) -> str:
    result = json.loads(_call("detach_role_policy", role, policy_arn))
    result["declared"] = {
        "services_lost": services_lost,
        "actions_lost_count": actions_lost_count,
        "blast_radius": blast_radius,
        "rollback": rollback,
        "reason": reason,
    }
    return _dump(result)


@mcp.tool(
    name="put_role_policy",
    description="DESTRUCTIVE. Create or replace an inline least-privilege policy on a role. The server lints it "
    "(no wildcard actions, no IAM on '*', no new privilege-escalation actions, no access beyond the current "
    "policies, conditions preserved) and refuses on any violation.",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False),
)
def put_role_policy(
    role: str,
    policy_name: str,
    policy_json: str,
    replaces: list[str],
    diff_summary: str,
    rollback: str,
    reason: str,
) -> str:
    result = json.loads(_call("put_role_policy", role, policy_name, policy_json))
    result["declared"] = {"replaces": replaces, "diff_summary": diff_summary, "rollback": rollback, "reason": reason}
    return _dump(result)


def main() -> None:
    load_env_file()
    host = os.environ.get("ACCESS_REVIEWER_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("ACCESS_REVIEWER_MCP_PORT", "8765"))
    print(f"access-reviewer MCP listening on http://{host}:{port}/mcp")
    print(f"backend: {backend_name()}")
    print(f"deny prefixes: {', '.join(deny_prefixes())}")
    if use_aws():
        print(f"demo revoke principal: {demo_revoke_principal()}")
    mcp.run(transport="streamable-http", host=host, port=port, stateless_http=True, streamable_http_path="/mcp")


if __name__ == "__main__":
    main()
