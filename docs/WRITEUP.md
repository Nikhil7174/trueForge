# platform-guardian: an AWS hygiene agent that knows when to stop

**Problem.** AWS accounts slowly fill with waste (unattached volumes, idle IPs, stale snapshots) and with roles holding far more access than they use. Cleanup is tedious and mistakes are irreversible, so it rarely happens. We delegate it to an agent that gathers evidence, does the arithmetic, and asks before every irreversible step.

**What it reaches.** A live AWS account, through two narrow MCP servers built in this repo:
- EC2/EBS, CloudTrail and live Price List prices.
- IAM policies, service-last-accessed data and CloudTrail sessions.

There is no generic `call_aws` tool, so each tool name is a clear approval boundary.

**Where it stops.** All seven destructive tools are listed by exact name for TrueForge's native approval. Their arguments describe the blast radius (size, cost, backup, rollback), so the approval prompt shows it. The MCP also enforces policy in code:
- It refuses prod-tagged or protected resources.
- It re-reads live state and refuses on drift.
- It requires a completed backup before deleting a volume.
- It lints IAM policies for wildcards, privilege escalation and any expansion of access.

When the operator denies a call, the agent acknowledges the reason and changes nothing else.

**Architecture and use of TrueForge.**
- One umbrella agent merges two plugins.
- Git-backed skills hold the playbooks, waste rules and a shared OpenUI card contract.
- Subagents gather IAM evidence per role.
- The Daytona sandbox runs the cost math and policy synthesis, with no credentials.
- Questions follow a contract: one recommended option with a reason, and a re-ask after free text.

**Real vs staged.**
- **Real:** every AWS call, price, deletion and approval.
- **Staged:** the resources. `infra/seed.py` creates a believable environment and exercises the IAM roles so their telemetry is genuine; `make teardown` removes it.
- **Fixture:** the IAM backend remains for tests only.

**Known limits.**
- IAM telemetry lags up to about 4 hours, so new roles correctly yield "insufficient evidence, no revocation".
- It depends on Daytona.
- It uses stock OpenUI components only.
- It covers one region per run.
