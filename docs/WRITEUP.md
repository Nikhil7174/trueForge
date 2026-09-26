# aws-hygiene: an AWS hygiene agent that knows when to stop

**Problem.** Every AWS account slowly fills with waste: unattached volumes, idle Elastic IPs, forgotten snapshots, and roles with far more access than they use. Cleaning this up is tedious, and a mistake is irreversible, so it rarely happens. We delegate it to an agent that gathers evidence, does the arithmetic, and asks before every irreversible step.

**What it reaches.** It works on a live AWS account through two narrow MCP servers built in this repo:
- `cost-janitor-aws`: EC2/EBS, CloudTrail, and the Price List API for live prices.
- `access-reviewer-iam`: IAM policies, service-last-accessed data and CloudTrail sessions.

There is no generic `call_aws` tool, so every tool name is a clear approval boundary.

**Where it stops.** Every destructive tool is listed by exact name for TrueForge's native approval:
- `cost-janitor-aws`: delete volume, release IP, delete snapshot, terminate instance.
- `access-reviewer-iam`: detach policy, put policy.

Each tool's arguments describe its own blast radius (size, monthly cost, backup ID, rollback), so the approval prompt shows them. The MCP also enforces policy in code:
- It refuses `env=prod`/`keep`/`do-not-delete` resources.
- It re-reads live state and refuses on drift.
- It requires a completed backup before any volume delete.
- It lints IAM policies, blocking wildcards, privilege escalation and any expansion of access.

When the operator denies a call, the agent acknowledges the reason and changes nothing else.

**Architecture and use of TrueForge.**
- One umbrella agent merges two plugins.
- Git-backed skills hold the playbooks, waste rules and a shared OpenUI card contract.
- Dynamic subagents gather IAM evidence per role.
- The Daytona sandbox runs the stdlib-only cost math and least-privilege policy synthesis. It holds no AWS credentials.
- `ask_user_question` follows a contract: evidence first, one recommended option with its reason, and a rewritten re-ask when the user types free text.

**Real vs staged.**
- **Real:** every AWS call, price, deletion and approval.
- **Staged:** the resources themselves. `infra/seed.py` creates a believable platform-team environment and assumes the IAM roles so their usage data is genuine. `make teardown` removes exactly what it created.
- **Fixture:** the IAM backend remains available for tests only.

**Known limits.**
- IAM telemetry lags up to about 4 hours. With young roles, the agent correctly answers "insufficient evidence, no revocation".
- It depends on Daytona, whose free tier caps sandbox disk.
- It uses stock OpenUI components only.
- It maps CloudTrail event names to IAM action names heuristically.
- It covers one region per run.
