"""Remove exactly the AWS resources recorded in infra/.seed-state.json.

Also removes EBS snapshots taken of those recorded volumes (backups the agent
makes before deleting a volume), so nothing billable is left behind.
Already-absent resources are skipped, so it is safe to re-run.

Usage:  python infra/teardown.py [--yes]
"""

from __future__ import annotations

import argparse
import os
import sys

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(__file__))
from common import STATE_PATH, load_state, save_state, session  # noqa: E402

NOT_FOUND = {
    "InvalidVolume.NotFound",
    "InvalidSnapshot.NotFound",
    "InvalidAllocationID.NotFound",
    "InvalidInstanceID.NotFound",
    "NoSuchEntity",
}


def _gone(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] in NOT_FOUND


def _step(label: str, fn) -> None:
    try:
        fn()
        print(f"removed  {label}")
    except ClientError as exc:
        if not _gone(exc):
            raise
        print(f"absent   {label}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args()

    state = load_state()
    if not state:
        print(f"No state at {STATE_PATH}; nothing to remove.")
        return 0

    sess = session()
    account = sess.client("sts").get_caller_identity()["Account"]
    if account != state.get("account"):
        print(f"State is for account {state.get('account')}, current credentials are {account}. Aborting.")
        return 1

    volumes = state.get("volumes", {})
    snapshots = state.get("snapshots", {})
    instance = state.get("instance") or {}
    eip = state.get("eip") or {}
    roles = state.get("roles", {})

    print(f"Account {account}, region {state['region']}. Will remove:")
    for name, vid in volumes.items():
        print(f"  volume    {name} {vid}")
    for name, sid in snapshots.items():
        print(f"  snapshot  {name} {sid}")
    print("  snapshots of the volumes above (any)")
    if instance:
        print(f"  instance  {instance['name']} {instance['instance_id']}")
    if eip:
        print(f"  eip       {eip['name']} {eip['allocation_id']} {eip['public_ip']}")
    for name in roles:
        print(f"  iam role  {name}")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        return 1

    ec2 = sess.client("ec2")
    iam = sess.client("iam")

    if instance:
        iid = instance["instance_id"]

        def _terminate() -> None:
            ec2.terminate_instances(InstanceIds=[iid])
            ec2.get_waiter("instance_terminated").wait(InstanceIds=[iid])

        _step(f"instance {iid}", _terminate)

    snap_ids = set(snapshots.values())
    if volumes:
        found = ec2.describe_snapshots(
            OwnerIds=["self"], Filters=[{"Name": "volume-id", "Values": list(volumes.values())}]
        )["Snapshots"]
        snap_ids.update(s["SnapshotId"] for s in found)
    for sid in sorted(snap_ids):
        _step(f"snapshot {sid}", lambda sid=sid: ec2.delete_snapshot(SnapshotId=sid))

    for name, vid in volumes.items():
        _step(f"volume {name} {vid}", lambda vid=vid: ec2.delete_volume(VolumeId=vid))

    if eip:
        _step(f"eip {eip['allocation_id']}", lambda: ec2.release_address(AllocationId=eip["allocation_id"]))

    for name in roles:

        def _delete_role(name: str = name) -> None:
            for p in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
                iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
            for pname in iam.list_role_policies(RoleName=name)["PolicyNames"]:
                iam.delete_role_policy(RoleName=name, PolicyName=pname)
            iam.delete_role(RoleName=name)

        _step(f"iam role {name}", _delete_role)

    STATE_PATH.rename(STATE_PATH.with_suffix(".json.removed"))
    print(f"Done. State moved to {STATE_PATH.with_suffix('.json.removed').name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
