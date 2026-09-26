"""Probe the AWS permissions the hygiene agent's MCP servers need. Changes nothing.

Read APIs are called directly. Mutating EC2 APIs are probed with DryRun=True.
Usage:  python infra/doctor.py
"""

from __future__ import annotations

import os
import sys
from typing import Callable

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(__file__))
from common import REGION, load_state, session  # noqa: E402


def main() -> int:
    sess = session()
    ec2 = sess.client("ec2")
    iam = sess.client("iam")
    state = load_state()
    # Dry runs need a real volume ID: a seeded one, else any volume in the region.
    vol = next(iter(state.get("volumes", {}).values()), None)
    if vol is None:
        found = ec2.describe_volumes(MaxResults=5)["Volumes"]
        vol = found[0]["VolumeId"] if found else None
    role = next(iter(state.get("roles", {})), None)

    checks: list[tuple[str, Callable[[], object]]] = [
        ("sts:GetCallerIdentity", lambda: sess.client("sts").get_caller_identity()),
        ("ec2:DescribeVolumes", lambda: ec2.describe_volumes(MaxResults=5)),
        ("ec2:DescribeAddresses", lambda: ec2.describe_addresses()),
        ("ec2:DescribeSnapshots", lambda: ec2.describe_snapshots(OwnerIds=["self"], MaxResults=5)),
        ("ec2:DescribeInstances", lambda: ec2.describe_instances(MaxResults=5)),
        ("cloudtrail:LookupEvents", lambda: sess.client("cloudtrail").lookup_events(MaxResults=1)),
        ("pricing:GetProducts (us-east-1)", lambda: sess.client("pricing", region_name="us-east-1").get_products(
            ServiceCode="AmazonEC2", MaxResults=1,
            Filters=[{"Type": "TERM_MATCH", "Field": "regionCode", "Value": REGION}])),
        ("iam:ListRoles", lambda: iam.list_roles(MaxItems=1)),
    ]
    if vol:
        checks += [
            ("ec2:CreateSnapshot (dry run)", lambda: ec2.create_snapshot(VolumeId=vol, DryRun=True)),
            ("ec2:DeleteVolume (dry run)", lambda: ec2.delete_volume(VolumeId=vol, DryRun=True)),
        ]
    else:
        print("skip  ec2:CreateSnapshot / ec2:DeleteVolume dry runs (no volume in region yet; re-run after make seed)")
    if role:
        checks += [
            ("iam:GetRole", lambda: iam.get_role(RoleName=role)),
            ("iam:ListAttachedRolePolicies", lambda: iam.list_attached_role_policies(RoleName=role)),
            ("iam:GetServiceLastAccessedDetails (generate)",
             lambda: iam.generate_service_last_accessed_details(Arn=state["roles"][role]["arn"])),
        ]

    failed = 0
    for label, fn in checks:
        try:
            fn()
            print(f"ok    {label}")
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "DryRunOperation":
                print(f"ok    {label}")
            else:
                failed += 1
                print(f"FAIL  {label}: {code}")
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed in {REGION}.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
