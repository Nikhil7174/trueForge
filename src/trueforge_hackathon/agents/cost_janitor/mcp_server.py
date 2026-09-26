from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from trueforge_hackathon.agents.cost_janitor import ec2_ops, pricing
from trueforge_hackathon.agents.cost_janitor.policy import default_region, deny_resource_prefixes

COST_JANITOR_MCP_NAME = "cost-janitor-aws"

mcp = MCPServer(
    COST_JANITOR_MCP_NAME,
    instructions=(
        "AWS cost hygiene: read inventory, CloudTrail activity and live prices; snapshot volumes (reversible); "
        "delete/release/terminate (destructive, paused by TrueForge). Destructive tools re-read live state, "
        "refuse protected resources (env=prod/production, keep=true, do-not-delete) and refuse on drift."
    ),
)

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)


def _dump(payload: Any) -> str:
    return json.dumps(payload, default=str, indent=2)


def _run(fn, *args: Any, **kwargs: Any) -> str:
    try:
        return _dump(fn(*args, **kwargs))
    except ValueError as exc:
        return _dump({"error": str(exc)})
    except Exception as exc:  # noqa: BLE001  surface AWS errors to the agent as data
        code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
        return _dump({"error": f"{type(exc).__name__}: {exc}", "aws_error_code": code})


def _audit(result: dict[str, Any], **declared: Any) -> dict[str, Any]:
    """Echo the self-describing approval arguments next to the live result."""
    return {**result, "declared": declared}


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "mcp": COST_JANITOR_MCP_NAME, "region": default_region()})


# ------------------------------------------------------------------ read


@mcp.tool(
    name="list_ebs_volumes",
    description="List EBS volumes with size, type, state, attachments, age, tags and a 'protected' reason. "
    "state filter: available | in-use | creating. Read-only.",
    annotations=READ,
)
def list_ebs_volumes(region: str | None = None, state: str | None = None) -> str:
    return _run(ec2_ops.list_ebs_volumes, region or default_region(), state)


@mcp.tool(
    name="list_elastic_ips",
    description="List Elastic IPs with association details, tags and a 'protected' reason. Read-only.",
    annotations=READ,
)
def list_elastic_ips(region: str | None = None) -> str:
    return _run(ec2_ops.list_elastic_ips, region or default_region())


@mcp.tool(
    name="list_snapshots",
    description="List EBS snapshots owned by this account, with source volume, whether that volume still exists, "
    "age, tags and a 'protected' reason. Read-only.",
    annotations=READ,
)
def list_snapshots(region: str | None = None, owner: str = "self") -> str:
    return _run(ec2_ops.list_snapshots, region or default_region(), owner)


@mcp.tool(
    name="list_instances",
    description="List EC2 instances with type, state, state transition reason, EBS volumes (delete-on-termination), "
    "tags and a 'protected' reason. state filter: running | stopped | ... Read-only.",
    annotations=READ,
)
def list_instances(region: str | None = None, state: str | None = None) -> str:
    return _run(ec2_ops.list_instances, region or default_region(), state)


@mcp.tool(
    name="get_resource_activity",
    description="Summarise CloudTrail management events that reference one resource ID over the lookback window "
    "(max 90 days). Evidence of recent use. Read-only.",
    annotations=READ,
)
def get_resource_activity(resource_id: str, region: str | None = None, lookback_hours: int = 168) -> str:
    return _run(ec2_ops.get_resource_activity, resource_id, region or default_region(), lookback_hours)


@mcp.tool(
    name="get_price",
    description="Live on-demand unit price from the AWS Price List API. kind: ebs_volume (attributes.volume_type, "
    "e.g. gp3), ebs_snapshot (attributes.tier: standard|archive), public_ipv4 (attributes.state: idle|in_use). "
    "Returns unit and USD per unit; rejects ambiguous matches instead of guessing. Read-only.",
    annotations=READ,
)
def get_price(kind: str, region: str | None = None, attributes: dict[str, str] | None = None) -> str:
    return _run(pricing.get_price, kind, region or default_region(), attributes or {})


# ------------------------------------------------------------------ reversible


@mcp.tool(
    name="create_snapshot",
    description="Snapshot an EBS volume as a backup before cleanup. Reversible (the snapshot can be deleted). "
    "Idempotent: returns the existing snapshot made for the same volume and reason in the last 24 h. "
    "Waits up to ~90 s for completion.",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True),
)
def create_snapshot(volume_id: str, reason: str, region: str | None = None) -> str:
    return _run(ec2_ops.create_snapshot, volume_id, reason, region or default_region())


# ------------------------------------------------------------------ destructive (approval-gated by name)


@mcp.tool(
    name="delete_volume",
    description="DESTRUCTIVE. Permanently delete an unattached EBS volume. Every argument is shown to the approver. "
    "The server re-reads the volume and refuses if it is protected, attached, if name_tag/size_gib do not match, "
    "or if backup_snapshot_id is missing, not completed, or not a snapshot of this volume.",
    annotations=DESTRUCTIVE,
)
def delete_volume(
    volume_id: str,
    name_tag: str,
    size_gib: int,
    monthly_cost_usd: float,
    backup_snapshot_id: str,
    reason: str,
    rollback: str,
    region: str | None = None,
) -> str:
    return _run(
        lambda: _audit(
            ec2_ops.delete_volume(
                volume_id=volume_id,
                name_tag=name_tag,
                size_gib=size_gib,
                backup_snapshot_id=backup_snapshot_id,
                region=region or default_region(),
            ),
            monthly_cost_usd=monthly_cost_usd,
            reason=reason,
            rollback=rollback,
        )
    )


@mcp.tool(
    name="release_elastic_ip",
    description="DESTRUCTIVE. Release an unassociated Elastic IP; the address cannot be guaranteed back. "
    "The server re-reads it and refuses if it is protected, associated, or public_ip does not match.",
    annotations=DESTRUCTIVE,
)
def release_elastic_ip(
    allocation_id: str,
    public_ip: str,
    monthly_cost_usd: float,
    reason: str,
    rollback: str,
    region: str | None = None,
) -> str:
    return _run(
        lambda: _audit(
            ec2_ops.release_elastic_ip(
                allocation_id=allocation_id, public_ip=public_ip, region=region or default_region()
            ),
            monthly_cost_usd=monthly_cost_usd,
            reason=reason,
            rollback=rollback,
        )
    )


@mcp.tool(
    name="delete_snapshot",
    description="DESTRUCTIVE. Permanently delete an EBS snapshot. The server refuses if it is protected, backs an AMI, "
    "source_volume does not match, or it is a cleanup backup younger than 7 days.",
    annotations=DESTRUCTIVE,
)
def delete_snapshot(
    snapshot_id: str,
    source_volume: str,
    monthly_cost_usd: float,
    reason: str,
    rollback: str,
    region: str | None = None,
) -> str:
    return _run(
        lambda: _audit(
            ec2_ops.delete_snapshot(snapshot_id=snapshot_id, source_volume=source_volume, region=region or default_region()),
            monthly_cost_usd=monthly_cost_usd,
            reason=reason,
            rollback=rollback,
        )
    )


@mcp.tool(
    name="terminate_instance",
    description="DESTRUCTIVE. Terminate a stopped EC2 instance. Volumes with delete-on-termination go with it. "
    "The server refuses if it is protected, running, termination-protected, or name_tag/attached_volumes do not match.",
    annotations=DESTRUCTIVE,
)
def terminate_instance(
    instance_id: str,
    name_tag: str,
    attached_volumes: list[str],
    monthly_cost_usd: float,
    reason: str,
    rollback: str,
    region: str | None = None,
) -> str:
    return _run(
        lambda: _audit(
            ec2_ops.terminate_instance(
                instance_id=instance_id,
                name_tag=name_tag,
                attached_volumes=attached_volumes,
                region=region or default_region(),
            ),
            monthly_cost_usd=monthly_cost_usd,
            reason=reason,
            rollback=rollback,
        )
    )



def main() -> None:
    host = os.environ.get("COST_JANITOR_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("COST_JANITOR_MCP_PORT", "8766"))
    print(f"cost-janitor MCP listening on http://{host}:{port}/mcp (region {default_region()})")
    print(f"deny resource prefixes: {', '.join(deny_resource_prefixes()) or '(none)'}")
    mcp.run(transport="streamable-http", host=host, port=port, stateless_http=True, streamable_http_path="/mcp")


if __name__ == "__main__":
    main()
