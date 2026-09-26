from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from trueforge_hackathon.agents.access_reviewer.backends import backend_name, get_backend
from trueforge_hackathon.agents.access_reviewer.store import deny_prefixes

mcp = MCPServer(
    "access-reviewer-iam",
    instructions=(
        "IAM read and evidence tools plus destructive revoke/detach/put tools. Destructive tools are paused by TrueForge. "
        "Deny-listed principals and AWS service-linked roles are refused in code."
    ),
)

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


def _dump(payload: Any) -> str:
    return json.dumps(payload, default=lambda o: getattr(o, "__dict__", str(o)), indent=2)


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
    return JSONResponse({"ok": True, "mcp": "access-reviewer-iam", "backend": backend_name()})


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
    return _call("list_principals")


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
    return _call("get_principal_policies", principal)


@mcp.tool(
    name="get_access_last_used",
    description="Return last-used timestamps and which attachments are unused for the review window. Read-only.",
    annotations=ToolAnnotations(
        title="Get access last used",
        read_only_hint=True,
        destructive_hint=False,
        open_world_hint=False,
    ),
)
def get_access_last_used(principal: str | None = None) -> str:
    return _call("get_access_last_used", principal)


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
    return _call("revoke_access", principal, policy)


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
    host = os.environ.get("ACCESS_REVIEWER_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("ACCESS_REVIEWER_MCP_PORT", "8765"))
    print(f"access-reviewer MCP listening on http://{host}:{port}/mcp (backend: {backend_name()})")
    print(f"deny prefixes: {', '.join(deny_prefixes())}")
    mcp.run(transport="streamable-http", host=host, port=port, stateless_http=True, streamable_http_path="/mcp")


if __name__ == "__main__":
    main()
