"""EC2 / EBS / CloudTrail operations behind the cost-janitor MCP tools.

Every mutation re-reads live state first, refuses on drift or protection, and is
idempotent: a resource that is already gone returns status "already_absent".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from trueforge_hackathon.agents.cost_janitor.policy import protection_reason, tags_dict

BACKUP_TAG = "created-by"
BACKUP_TAG_VALUE = "cost-janitor"
BACKUP_RETENTION_DAYS = 7
_RETRY = Config(retries={"mode": "adaptive", "max_attempts": 8})


@lru_cache(maxsize=16)
def _client(service: str, region: str):
    return boto3.client(service, region_name=region, config=_RETRY)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _age_days(value: datetime | None) -> float | None:
    return round((_now() - value).total_seconds() / 86400, 2) if value else None


def _code(exc: ClientError) -> str:
    return exc.response["Error"]["Code"]


def _refused(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "refused", "reason": reason, **extra}


# ------------------------------------------------------------------ reads


def _volume_row(v: dict[str, Any]) -> dict[str, Any]:
    tags = tags_dict(v.get("Tags"))
    return {
        "volume_id": v["VolumeId"],
        "name": tags.get("Name"),
        "size_gib": v["Size"],
        "type": v["VolumeType"],
        "iops": v.get("Iops"),
        "throughput": v.get("Throughput"),
        "state": v["State"],
        "availability_zone": v["AvailabilityZone"],
        "create_time": _iso(v["CreateTime"]),
        "age_days": _age_days(v["CreateTime"]),
        "attachments": [
            {"instance_id": a["InstanceId"], "device": a["Device"], "state": a["State"]}
            for a in v.get("Attachments", [])
        ],
        "snapshot_id": v.get("SnapshotId") or None,
        "encrypted": v.get("Encrypted"),
        "tags": tags,
        "protected": protection_reason(tags, tags.get("Name")),
    }


def list_ebs_volumes(region: str, state: str | None = None) -> dict[str, Any]:
    filters = [{"Name": "status", "Values": [state]}] if state else []
    rows = []
    for page in _client("ec2", region).get_paginator("describe_volumes").paginate(Filters=filters):
        rows.extend(_volume_row(v) for v in page["Volumes"])
    return {"region": region, "count": len(rows), "volumes": rows}


def list_elastic_ips(region: str) -> dict[str, Any]:
    rows = []
    for a in _client("ec2", region).describe_addresses()["Addresses"]:
        tags = tags_dict(a.get("Tags"))
        associated = bool(a.get("AssociationId") or a.get("InstanceId") or a.get("NetworkInterfaceId"))
        rows.append(
            {
                "allocation_id": a.get("AllocationId"),
                "public_ip": a.get("PublicIp"),
                "name": tags.get("Name"),
                "associated": associated,
                "association": {
                    "association_id": a.get("AssociationId"),
                    "instance_id": a.get("InstanceId"),
                    "network_interface_id": a.get("NetworkInterfaceId"),
                    "private_ip": a.get("PrivateIpAddress"),
                }
                if associated
                else None,
                "domain": a.get("Domain"),
                "tags": tags,
                "protected": protection_reason(tags, tags.get("Name")),
            }
        )
    return {"region": region, "count": len(rows), "elastic_ips": rows}


def list_snapshots(region: str, owner: str = "self") -> dict[str, Any]:
    if owner != "self":
        return {"error": "only owner='self' is supported"}
    ec2 = _client("ec2", region)
    snaps = []
    for page in ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"]):
        snaps.extend(page["Snapshots"])
    volume_ids = sorted({s["VolumeId"] for s in snaps if s.get("VolumeId") and s["VolumeId"] != "vol-ffffffff"})
    existing: set[str] = set()
    for i in range(0, len(volume_ids), 190):
        chunk = volume_ids[i : i + 190]
        for page in ec2.get_paginator("describe_volumes").paginate(Filters=[{"Name": "volume-id", "Values": chunk}]):
            existing.update(v["VolumeId"] for v in page["Volumes"])
    rows = []
    for s in snaps:
        tags = tags_dict(s.get("Tags"))
        rows.append(
            {
                "snapshot_id": s["SnapshotId"],
                "name": tags.get("Name"),
                "volume_id": s.get("VolumeId"),
                "source_volume_exists": s.get("VolumeId") in existing,
                "size_gib": s["VolumeSize"],
                "state": s["State"],
                "progress": s.get("Progress"),
                "start_time": _iso(s["StartTime"]),
                "age_days": _age_days(s["StartTime"]),
                "description": s.get("Description"),
                "storage_tier": s.get("StorageTier"),
                "tags": tags,
                "protected": protection_reason(tags, tags.get("Name")),
            }
        )
    return {"region": region, "count": len(rows), "snapshots": rows}


def _instance_row(i: dict[str, Any]) -> dict[str, Any]:
    tags = tags_dict(i.get("Tags"))
    return {
        "instance_id": i["InstanceId"],
        "name": tags.get("Name"),
        "type": i["InstanceType"],
        "state": i["State"]["Name"],
        "state_transition_reason": i.get("StateTransitionReason") or None,
        "launch_time": _iso(i.get("LaunchTime")),
        "public_ip": i.get("PublicIpAddress"),
        "volumes": [
            {
                "volume_id": m["Ebs"]["VolumeId"],
                "device": m["DeviceName"],
                "delete_on_termination": m["Ebs"].get("DeleteOnTermination"),
            }
            for m in i.get("BlockDeviceMappings", [])
            if "Ebs" in m
        ],
        "tags": tags,
        "protected": protection_reason(tags, tags.get("Name")),
    }


def list_instances(region: str, state: str | None = None) -> dict[str, Any]:
    filters = [{"Name": "instance-state-name", "Values": [state]}] if state else []
    rows = []
    for page in _client("ec2", region).get_paginator("describe_instances").paginate(Filters=filters):
        for res in page["Reservations"]:
            rows.extend(_instance_row(i) for i in res["Instances"])
    return {"region": region, "count": len(rows), "instances": rows}


def get_resource_activity(resource_id: str, region: str, lookback_hours: int = 168) -> dict[str, Any]:
    hours = max(1, min(int(lookback_hours), 90 * 24))
    start = _now() - timedelta(hours=hours)
    events: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "LookupAttributes": [{"AttributeKey": "ResourceName", "AttributeValue": resource_id}],
        "StartTime": start,
        "MaxResults": 50,
    }
    ct = _client("cloudtrail", region)
    while len(events) < 200:
        page = ct.lookup_events(**kwargs)
        for e in page["Events"]:
            events.append(
                {
                    "time": _iso(e["EventTime"]),
                    "event": e["EventName"],
                    "source": e.get("EventSource"),
                    "user": e.get("Username"),
                    "read_only": e.get("ReadOnly"),
                }
            )
        if not page.get("NextToken"):
            break
        kwargs["NextToken"] = page["NextToken"]
    writes = [e for e in events if e.get("read_only") != "true"]
    return {
        "resource_id": resource_id,
        "region": region,
        "lookback_hours": hours,
        "event_count": len(events),
        "write_event_count": len(writes),
        "last_event": events[0] if events else None,
        "events": events[:25],
        "caveat": "CloudTrail event history covers management events only, lags by minutes, and keeps 90 days.",
    }


# ------------------------------------------------------------------ reversible


def create_snapshot(volume_id: str, reason: str, region: str, wait_seconds: int = 90) -> dict[str, Any]:
    ec2 = _client("ec2", region)
    try:
        vol = ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"][0]
    except ClientError as exc:
        if _code(exc) == "InvalidVolume.NotFound":
            return {"status": "volume_absent", "volume_id": volume_id}
        raise
    vtags = tags_dict(vol.get("Tags"))
    since = _now() - timedelta(hours=24)
    existing = ec2.describe_snapshots(
        OwnerIds=["self"],
        Filters=[
            {"Name": "volume-id", "Values": [volume_id]},
            {"Name": f"tag:{BACKUP_TAG}", "Values": [BACKUP_TAG_VALUE]},
            {"Name": "tag:backup-reason", "Values": [reason[:255]]},
        ],
    )["Snapshots"]
    existing = [s for s in existing if s["StartTime"] >= since and s["State"] != "error"]
    if existing:
        snap = max(existing, key=lambda s: s["StartTime"])
        status = "exists"
    else:
        name = f"{vtags.get('Name') or volume_id}-pre-delete"
        tags = {
            "Name": name,
            BACKUP_TAG: BACKUP_TAG_VALUE,
            "backup-reason": reason[:255],
            "source-volume": volume_id,
        }
        for key in ("team", "owner", "service", "cost-center"):
            if key in vtags:
                tags[key] = vtags[key]
        snap = ec2.create_snapshot(
            VolumeId=volume_id,
            Description=f"Backup of {vtags.get('Name') or volume_id} before cleanup: {reason}"[:255],
            TagSpecifications=[{"ResourceType": "snapshot", "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}],
        )
        status = "created"
    snap_id = snap["SnapshotId"]
    if snap["State"] != "completed" and wait_seconds > 0:
        try:
            ec2.get_waiter("snapshot_completed").wait(
                SnapshotIds=[snap_id],
                WaiterConfig={"Delay": 5, "MaxAttempts": max(1, wait_seconds // 5)},
            )
        except Exception:  # noqa: BLE001  still pending is a valid answer
            pass
        snap = ec2.describe_snapshots(SnapshotIds=[snap_id])["Snapshots"][0]
    return {
        "status": status,
        "snapshot_id": snap_id,
        "volume_id": volume_id,
        "state": snap["State"],
        "progress": snap.get("Progress"),
        "size_gib": snap["VolumeSize"],
        "start_time": _iso(snap["StartTime"]),
    }


# ------------------------------------------------------------------ destructive


def delete_volume(
    *, volume_id: str, name_tag: str, size_gib: int, backup_snapshot_id: str, region: str
) -> dict[str, Any]:
    ec2 = _client("ec2", region)
    try:
        vol = ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"][0]
    except ClientError as exc:
        if _code(exc) == "InvalidVolume.NotFound":
            return {"status": "already_absent", "volume_id": volume_id}
        raise
    if vol["State"] in ("deleting", "deleted"):
        return {"status": "already_absent", "volume_id": volume_id, "state": vol["State"]}
    tags = tags_dict(vol.get("Tags"))
    reason = protection_reason(tags, tags.get("Name"))
    if reason:
        return _refused(f"protected resource: {reason}", volume_id=volume_id)
    if (tags.get("Name") or "") != name_tag:
        return _refused(f"name_tag mismatch: live Name is {tags.get('Name')!r}", volume_id=volume_id)
    if int(size_gib) != vol["Size"]:
        return _refused(f"size_gib mismatch: live size is {vol['Size']} GiB", volume_id=volume_id)
    if vol["State"] != "available" or vol.get("Attachments"):
        return _refused(f"state drift: volume is {vol['State']} with {len(vol.get('Attachments', []))} attachment(s)")
    try:
        snap = ec2.describe_snapshots(SnapshotIds=[backup_snapshot_id])["Snapshots"][0]
    except ClientError as exc:
        if _code(exc) in ("InvalidSnapshot.NotFound", "InvalidSnapshotID.Malformed"):
            return _refused(f"backup snapshot {backup_snapshot_id} not found; call create_snapshot first")
        raise
    if snap.get("VolumeId") != volume_id:
        return _refused(f"backup snapshot {backup_snapshot_id} belongs to {snap.get('VolumeId')}, not {volume_id}")
    if snap["State"] != "completed":
        return _refused(f"backup snapshot is {snap['State']} ({snap.get('Progress')}); retry when completed")
    ec2.delete_volume(VolumeId=volume_id)
    return {
        "status": "deleted",
        "volume_id": volume_id,
        "name": name_tag,
        "backup_snapshot_id": backup_snapshot_id,
        "verify_with": "list_ebs_volumes",
    }


def release_elastic_ip(*, allocation_id: str, public_ip: str, region: str) -> dict[str, Any]:
    ec2 = _client("ec2", region)
    try:
        addr = ec2.describe_addresses(AllocationIds=[allocation_id])["Addresses"][0]
    except ClientError as exc:
        if _code(exc) == "InvalidAllocationID.NotFound":
            return {"status": "already_absent", "allocation_id": allocation_id}
        raise
    tags = tags_dict(addr.get("Tags"))
    reason = protection_reason(tags, tags.get("Name"))
    if reason:
        return _refused(f"protected resource: {reason}", allocation_id=allocation_id)
    if addr.get("PublicIp") != public_ip:
        return _refused(f"public_ip mismatch: live address is {addr.get('PublicIp')}")
    if addr.get("AssociationId") or addr.get("InstanceId") or addr.get("NetworkInterfaceId"):
        return _refused("state drift: address is now associated", association_id=addr.get("AssociationId"))
    ec2.release_address(AllocationId=allocation_id)
    return {"status": "released", "allocation_id": allocation_id, "public_ip": public_ip, "verify_with": "list_elastic_ips"}


def delete_snapshot(*, snapshot_id: str, source_volume: str, region: str) -> dict[str, Any]:
    ec2 = _client("ec2", region)
    try:
        snap = ec2.describe_snapshots(SnapshotIds=[snapshot_id])["Snapshots"][0]
    except ClientError as exc:
        if _code(exc) == "InvalidSnapshot.NotFound":
            return {"status": "already_absent", "snapshot_id": snapshot_id}
        raise
    tags = tags_dict(snap.get("Tags"))
    reason = protection_reason(tags, tags.get("Name"))
    if reason:
        return _refused(f"protected resource: {reason}", snapshot_id=snapshot_id)
    if snap.get("VolumeId") != source_volume:
        return _refused(f"source_volume mismatch: live source is {snap.get('VolumeId')}")
    if tags.get(BACKUP_TAG) == BACKUP_TAG_VALUE and _age_days(snap["StartTime"]) < BACKUP_RETENTION_DAYS:
        return _refused(f"this is a cleanup backup younger than {BACKUP_RETENTION_DAYS} days; it is the rollback path")
    images = ec2.describe_images(
        Owners=["self"], Filters=[{"Name": "block-device-mapping.snapshot-id", "Values": [snapshot_id]}]
    )["Images"]
    if images:
        return _refused("snapshot backs a registered AMI", image_ids=[i["ImageId"] for i in images])
    ec2.delete_snapshot(SnapshotId=snapshot_id)
    return {"status": "deleted", "snapshot_id": snapshot_id, "verify_with": "list_snapshots"}


def terminate_instance(*, instance_id: str, name_tag: str, attached_volumes: list[str], region: str) -> dict[str, Any]:
    ec2 = _client("ec2", region)
    try:
        res = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"]
    except ClientError as exc:
        if _code(exc) == "InvalidInstanceID.NotFound":
            return {"status": "already_absent", "instance_id": instance_id}
        raise
    inst = res[0]["Instances"][0]
    row = _instance_row(inst)
    if row["state"] in ("shutting-down", "terminated"):
        return {"status": "already_absent", "instance_id": instance_id, "state": row["state"]}
    if row["protected"]:
        return _refused(f"protected resource: {row['protected']}", instance_id=instance_id)
    if (row["name"] or "") != name_tag:
        return _refused(f"name_tag mismatch: live Name is {row['name']!r}")
    if row["state"] != "stopped":
        return _refused(f"state drift: instance is {row['state']}; only stopped instances can be terminated")
    live_volumes = sorted(v["volume_id"] for v in row["volumes"])
    if sorted(attached_volumes) != live_volumes:
        return _refused(f"attached_volumes mismatch: live volumes are {live_volumes}")
    attr = ec2.describe_instance_attribute(InstanceId=instance_id, Attribute="disableApiTermination")
    if attr["DisableApiTermination"]["Value"]:
        return _refused("termination protection is enabled on this instance")
    ec2.terminate_instances(InstanceIds=[instance_id])
    return {
        "status": "terminating",
        "instance_id": instance_id,
        "volumes_deleted_with_instance": [v["volume_id"] for v in row["volumes"] if v["delete_on_termination"]],
        "volumes_left_behind": [v["volume_id"] for v in row["volumes"] if not v["delete_on_termination"]],
        "verify_with": "list_instances",
    }
