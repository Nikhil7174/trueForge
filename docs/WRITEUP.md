# Platform Guardian: one agent for a platform team's risky chores

**Problem.** Platform teams put off work that is tedious and irreversible when it goes wrong: cleaning up AWS waste, trimming IAM access, applying database migrations, cutting releases and resolving bug tickets. Platform Guardian takes these jobs on. It gathers evidence, does the work in a sandbox, and stops for a human before anything irreversible.

**How it works.** Every task starts as a Linear ticket, and its label picks the job: `cloudcost`, `iam-review`, `migration`, `release` or `Bug`. One trigger starts a session on the single platform-guardian agent and reports back on the ticket through labels: Working, Awaiting approval, Replied or Declined.

**What it reaches.** Five narrow MCP servers built in this repo:
- AWS EC2/EBS, with CloudTrail and live Price List prices
- AWS IAM, with CloudTrail
- Production Postgres (db-gate)
- GitHub (ship-gate)
- Linear and the product code (ticket-gate)

There is no generic "call anything" tool, so every tool name is a clear approval boundary.

**Where it stops.** Every destructive tool is gated by exact name on TrueForge's native approval: delete volume, detach or replace IAM policy, apply migration, publish release, reply to customer. Tool arguments describe the blast radius. The servers also enforce policy in code: they refuse prod-tagged resources, refuse when live state has drifted, require backups, lint IAM policies, and block any release whose migrations are unapplied.

**TrueForge features used.**
- Remote MCP connectors, plus the catalog Linear connector
- Native approvals
- Daytona sandbox: cost math, policy synthesis, migration rehearsal, build checks, bug reproduction
- Git-backed skills
- Dynamic subagents for per-role evidence
- Generative UI with a fixed card contract
- `ask_user_question` with one recommended option
- The sessions SDK, for reproducible registration

**Real vs staged.**
- **Real:** every call, price, deletion and approval.
- **Staged:** the AWS resources are seeded by `infra/seed.py`, and the claims database and product code are synthetic.

**Limits.** IAM telemetry lags by hours, so new roles get "insufficient evidence". The agent needs Daytona, and the UI uses stock OpenUI components only.
