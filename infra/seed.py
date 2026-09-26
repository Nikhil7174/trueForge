"""Create the AWS environment the hygiene agent works against (us-west-2).

Idempotent: re-running reuses resources already recorded in infra/.seed-state.json
(or found by their Name tag) instead of creating duplicates. Every ID is written to
the state file as soon as it exists, so a partial run can still be torn down.

Usage:  python infra/seed.py [--no-instance]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(__file__))
from common import REGION, load_state, now_iso, save_state, session, tag_list  # noqa: E402

VOLUMES: list[dict[str, Any]] = [
    {
        "name": "pg-replica-data-old",
        "size": 100,
        "type": "gp2",
        "tags": {"team": "data-platform", "env": "staging", "owner": "ravi",
                 "service": "orders-db", "cost-center": "CC-2140"},
    },
    {
        "name": "ci-runner-cache-07",
        "size": 50,
        "type": "gp3",
        "tags": {"team": "platform", "env": "ci", "service": "build-runners",
                 "cost-center": "CC-1100"},
    },
    {
        "name": "ml-feature-scratch",
        "size": 200,
        "type": "gp3",
        "tags": {"team": "ml", "owner": "ananya", "service": "feature-store",
                 "cost-center": "CC-3300"},
    },
    {
        "name": "payments-db-data",
        "size": 20,
        "type": "gp3",
        "tags": {"team": "payments", "env": "prod", "owner": "meera",
                 "service": "payments-api", "cost-center": "CC-4200"},
    },
]

EIP = {"name": "legacy-bastion-ip", "tags": {"team": "platform", "service": "bastion", "cost-center": "CC-1100"}}

SNAPSHOT = {
    "name": "pg-pre-upgrade-backup",
    "volume": "pg-replica-data-old",
    "description": "orders-db replica volume before Postgres 14 -> 16 upgrade",
    "tags": {"team": "data-platform", "owner": "ravi", "service": "orders-db", "cost-center": "CC-2140"},
}

INSTANCE = {
    "name": "jenkins-agent-legacy",
    "type": "t3.micro",
    "tags": {"team": "platform", "env": "ci", "service": "jenkins", "cost-center": "CC-1100"},
}

ROLES: list[dict[str, Any]] = [
    {
        "name": "analytics-reader",
        "description": "BI and reporting jobs for the analytics team",
        "policies": [
            "arn:aws:iam::aws:policy/AmazonS3FullAccess",
            "arn:aws:iam::aws:policy/AmazonAthenaFullAccess",
        ],
        "tags": {"team": "analytics", "owner": "kiran", "cost-center": "CC-3100"},
        "session": "looker-nightly-export",
        "usage": ("s3", "list_buckets", {}),
    },
    {
        "name": "ci-deployer",
        "description": "Deploy pipeline role assumed by CI runners",
        "policies": [
            "arn:aws:iam::aws:policy/AmazonEC2FullAccess",
            "arn:aws:iam::aws:policy/AmazonS3FullAccess",
            "arn:aws:iam::aws:policy/IAMFullAccess",
        ],
        "tags": {"team": "platform", "service": "build-runners", "cost-center": "CC-1100"},
        "session": "deploy-pipeline-main",
        "usage": ("ec2", "describe_instances", {"MaxResults": 5}),
    },
]


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- EC2 / EBS


def _pick_placement(ec2) -> tuple[str, str | None]:
    """Return (availability_zone, default_subnet_id or None)."""
    subnets = ec2.describe_subnets(Filters=[{"Name": "default-for-az", "Values": ["true"]}])["Subnets"]
    if subnets:
        subnets.sort(key=lambda s: s["AvailabilityZone"])
        return subnets[0]["AvailabilityZone"], subnets[0]["SubnetId"]
    zones = ec2.describe_availability_zones(Filters=[{"Name": "state", "Values": ["available"]}])
    return sorted(z["ZoneName"] for z in zones["AvailabilityZones"])[0], None


def _existing_volume(ec2, state_id: str | None, name: str) -> str | None:
    if state_id:
        try:
            vols = ec2.describe_volumes(VolumeIds=[state_id])["Volumes"]
            if vols and vols[0]["State"] not in ("deleting", "deleted"):
                return state_id
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "InvalidVolume.NotFound":
                raise
    vols = ec2.describe_volumes(
        Filters=[{"Name": "tag:Name", "Values": [name]},
                 {"Name": "status", "Values": ["creating", "available", "in-use"]}]
    )["Volumes"]
    return vols[0]["VolumeId"] if vols else None


def seed_volumes(ec2, state: dict, az: str) -> None:
    vols = state.setdefault("volumes", {})
    # Every volume ID ever created, so teardown also finds backups of volumes
    # that were cleaned up and re-created between runs.
    history = state.setdefault("volume_history", [])
    history.extend(v for v in vols.values() if v not in history)
    for spec in VOLUMES:
        name = spec["name"]
        found = _existing_volume(ec2, vols.get(name), name)
        if found:
            vols[name] = found
            _log(f"volume {name}: exists {found}")
            continue
        tags = {"Name": name, **spec["tags"]}
        vol = ec2.create_volume(
            AvailabilityZone=az,
            Size=spec["size"],
            VolumeType=spec["type"],
            TagSpecifications=[{"ResourceType": "volume", "Tags": tag_list(tags)}],
        )
        vols[name] = vol["VolumeId"]
        history.append(vol["VolumeId"])
        save_state(state)
        _log(f"volume {name}: created {vol['VolumeId']} ({spec['size']} GiB {spec['type']}, {az})")
    ec2.get_waiter("volume_available").wait(VolumeIds=list(vols.values()))
    _log("volumes available")


def seed_eip(ec2, state: dict) -> None:
    cur = state.get("eip") or {}
    if cur.get("allocation_id"):
        try:
            addr = ec2.describe_addresses(AllocationIds=[cur["allocation_id"]])["Addresses"][0]
            _log(f"eip {EIP['name']}: exists {addr['AllocationId']} {addr['PublicIp']}")
            return
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("InvalidAllocationID.NotFound",):
                raise
    found = ec2.describe_addresses(Filters=[{"Name": "tag:Name", "Values": [EIP["name"]]}])["Addresses"]
    if found:
        addr = found[0]
    else:
        addr = ec2.allocate_address(
            Domain="vpc",
            TagSpecifications=[{"ResourceType": "elastic-ip",
                                "Tags": tag_list({"Name": EIP["name"], **EIP["tags"]})}],
        )
    state["eip"] = {"name": EIP["name"], "allocation_id": addr["AllocationId"], "public_ip": addr["PublicIp"]}
    save_state(state)
    _log(f"eip {EIP['name']}: {addr['AllocationId']} {addr['PublicIp']}")


def seed_snapshot(ec2, state: dict) -> None:
    vol_id = state["volumes"][SNAPSHOT["volume"]]
    cur = state.get("snapshots", {}).get(SNAPSHOT["name"])
    if cur:
        try:
            ec2.describe_snapshots(SnapshotIds=[cur])
            _log(f"snapshot {SNAPSHOT['name']}: exists {cur}")
            return
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "InvalidSnapshot.NotFound":
                raise
    found = ec2.describe_snapshots(
        OwnerIds=["self"], Filters=[{"Name": "tag:Name", "Values": [SNAPSHOT["name"]]}]
    )["Snapshots"]
    if found:
        snap_id = found[0]["SnapshotId"]
    else:
        snap_id = ec2.create_snapshot(
            VolumeId=vol_id,
            Description=SNAPSHOT["description"],
            TagSpecifications=[{"ResourceType": "snapshot",
                                "Tags": tag_list({"Name": SNAPSHOT["name"], **SNAPSHOT["tags"]})}],
        )["SnapshotId"]
    state.setdefault("snapshots", {})[SNAPSHOT["name"]] = snap_id
    save_state(state)
    _log(f"snapshot {SNAPSHOT['name']}: {snap_id} (of {vol_id})")


def seed_instance(ec2, ssm, state: dict, subnet_id: str | None) -> None:
    cur = state.get("instance") or {}
    if cur.get("instance_id"):
        try:
            res = ec2.describe_instances(InstanceIds=[cur["instance_id"]])["Reservations"]
            inst = res[0]["Instances"][0]
            if inst["State"]["Name"] not in ("terminated", "shutting-down"):
                _log(f"instance {INSTANCE['name']}: exists {inst['InstanceId']} ({inst['State']['Name']})")
                if inst["State"]["Name"] in ("pending", "running"):
                    ec2.stop_instances(InstanceIds=[inst["InstanceId"]])
                return
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "InvalidInstanceID.NotFound":
                raise
    if not subnet_id:
        _log("instance: no default subnet in region, skipping (optional resource)")
        return
    ami = ssm.get_parameter(Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64")
    tags = tag_list({"Name": INSTANCE["name"], **INSTANCE["tags"]})
    inst = ec2.run_instances(
        ImageId=ami["Parameter"]["Value"],
        InstanceType=INSTANCE["type"],
        MinCount=1,
        MaxCount=1,
        SubnetId=subnet_id,
        TagSpecifications=[{"ResourceType": "instance", "Tags": tags},
                           {"ResourceType": "volume", "Tags": tags}],
    )["Instances"][0]
    iid = inst["InstanceId"]
    state["instance"] = {"name": INSTANCE["name"], "instance_id": iid}
    save_state(state)
    _log(f"instance {INSTANCE['name']}: launched {iid}, waiting to stop it")
    ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
    ec2.stop_instances(InstanceIds=[iid])
    ec2.get_waiter("instance_stopped").wait(InstanceIds=[iid])
    _log(f"instance {INSTANCE['name']}: stopped")


# ---------------------------------------------------------------- IAM


def seed_roles(iam, sts_client, sess, state: dict, caller_arn: str) -> None:
    roles = state.setdefault("roles", {})
    trust = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"AWS": caller_arn}, "Action": "sts:AssumeRole"}],
    }
    for spec in ROLES:
        name = spec["name"]
        try:
            role = iam.create_role(
                RoleName=name,
                AssumeRolePolicyDocument=json.dumps(trust),
                Description=spec["description"],
                Tags=tag_list(spec["tags"]),
            )["Role"]
            _log(f"role {name}: created")
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "EntityAlreadyExists":
                raise
            iam.update_assume_role_policy(RoleName=name, PolicyDocument=json.dumps(trust))
            role = iam.get_role(RoleName=name)["Role"]
            _log(f"role {name}: exists")
        for arn in spec["policies"]:
            iam.attach_role_policy(RoleName=name, PolicyArn=arn)
        roles[name] = {"arn": role["Arn"], "policies": spec["policies"]}
        save_state(state)

    # Exercise each role once so CloudTrail and service-last-accessed hold real usage.
    for spec in ROLES:
        name = spec["name"]
        creds = _assume_with_retry(sts_client, roles[name]["arn"], spec["session"])
        service, op, kwargs = spec["usage"]
        client = sess.client(
            service,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
        getattr(client, op)(**kwargs)
        roles[name]["used"] = {"action": f"{service}:{op}", "at": now_iso()}
        save_state(state)
        _log(f"role {name}: assumed as {spec['session']} and called {service}:{op}")


def _assume_with_retry(sts_client, arn: str, session_name: str) -> dict:
    # New roles/trust policies take a few seconds to propagate.
    deadline = time.time() + 120
    while True:
        try:
            return sts_client.assume_role(RoleArn=arn, RoleSessionName=session_name)["Credentials"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "AccessDenied" or time.time() > deadline:
                raise
            time.sleep(5)


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-instance", action="store_true", help="skip the optional stopped EC2 instance")
    args = parser.parse_args()

    sess = session()
    sts_client = sess.client("sts")
    ident = sts_client.get_caller_identity()
    state = load_state()
    if state.get("account") and state["account"] != ident["Account"]:
        print(f"State file belongs to account {state['account']}, current is {ident['Account']}. Aborting.")
        return 1
    state.update({"account": ident["Account"], "region": REGION, "caller": ident["Arn"]})
    state.setdefault("created_at", now_iso())
    save_state(state)
    _log(f"account {ident['Account']} as {ident['Arn']} in {REGION}")

    ec2 = sess.client("ec2")
    iam = sess.client("iam")
    # IAM first: usage telemetry lags, so start the clock as early as possible.
    seed_roles(iam, sts_client, sess, state, ident["Arn"])
    az, subnet_id = _pick_placement(ec2)
    seed_volumes(ec2, state, az)
    seed_eip(ec2, state)
    seed_snapshot(ec2, state)
    if not args.no_instance:
        seed_instance(ec2, sess.client("ssm"), state, subnet_id)

    state["updated_at"] = now_iso()
    save_state(state)
    _log(f"done. state: {len(state.get('volumes', {}))} volumes, eip, snapshot, "
         f"{'instance, ' if state.get('instance') else ''}{len(state.get('roles', {}))} roles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
