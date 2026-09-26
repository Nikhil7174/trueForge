# Waste rules

A resource is a **candidate** only when every "required evidence" line holds. Price is never evidence of waste: it only says how much the waste costs.

Always report counter-evidence next to the finding (recent CloudTrail writes, an owner tag, a recent creation date).

## Unattached EBS volume

- Required evidence:
  - `state == "available"` and `attachments == []`
  - `protected` is null
- Strengthens the case:
  - no CloudTrail write events (`AttachVolume`, `ModifyVolume`, `CreateTags`) in the lookback window
  - the name or tags describe a retired use (`old`, `legacy`, `scratch`, `cache`)
  - no `owner` tag
  - an existing snapshot already covers it
- Weakens the case:
  - created in the last 24 h (it may be mid-provisioning)
  - recent `AttachVolume`/`DetachVolume` events (it may be rotated between hosts)
- Action: `create_snapshot`, then `delete_volume`.
- Cost: `size_gib × volume price (GB-Mo)`. gp3 IOPS/throughput above the baseline (3000 IOPS / 125 MB/s) bills separately. Flag it if present.

## Unassociated Elastic IP

- Required evidence:
  - `associated == false`
  - `protected` is null
- Strengthens the case:
  - the name refers to a retired host
  - no `AssociateAddress` events in the lookback window
- Weakens the case: allow-listed addresses (DNS records or partner firewalls). Ask if the name suggests egress/allow-listing.
- Action: `release_elastic_ip`. There is no backup. The same address is usually unrecoverable, so say so.
- Cost: idle public IPv4 `usd_per_hour × 730`.

## Snapshot

- Candidate when the source volume no longer exists (`source_volume_exists == false`), or the snapshot is older than the retention the operator states, **and** it is not protected and backs no AMI.
- A snapshot whose source volume still exists is a backup. Keep it unless the operator explicitly asks otherwise.
- Never delete a `created-by=cost-janitor` backup younger than 7 days. The server refuses anyway: it is the rollback path.
- Cost: `size_gib × snapshot price`. This is an upper bound, because snapshots bill for stored changed blocks.

## Stopped EC2 instance

- A stopped instance costs **no instance-hours**. Its waste is its EBS volumes and any public IPv4.
- Evidence: `state == "stopped"`, and `state_transition_reason` shows when it stopped.
- Default recommendation: report it, and propose termination only if the operator's scope includes it. `terminate_instance` deletes delete-on-termination volumes with it, so snapshot those first.

## Extending (same shape, new read tool first)

These are not implemented yet: idle load balancers (no healthy targets and no requests), NAT gateways with no traffic, and CloudWatch log groups without retention. Add a read tool and a rule here before acting on any of them.
