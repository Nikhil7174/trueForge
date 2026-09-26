# platform-guardian: an AWS hygiene agent on TrueForge

One agent that does two jobs a platform team rarely has time for, on a **live AWS account**:

- **Cost janitor**: finds resources that cost money for nothing (unattached EBS volumes, idle Elastic IPs, orphaned snapshots, stopped instances). It proves each one with evidence, prices it live from the AWS Price List API, backs it up, and deletes it only after a human approves that specific resource.
- **Access reviewer**: reviews IAM roles for least privilege, using the roles' actual policies, IAM service-last-accessed data and CloudTrail. It builds and lints a least-privilege policy in the sandbox and changes nothing without approval. It also respects a "no".

TrueForge runs the agent loop. This repo supplies the MCP tools, the skills, the policy and the approval boundary.

| Plugin | Job | Gate (literal tool names) |
|---|---|---|
| **platform-guardian** (umbrella) | One agent merging the members below (`UMBRELLA_MEMBERS`) | union of the members' gates |
| **cost-janitor** | AWS waste: discover → evidence → price → back up → delete | `delete_volume`, `release_elastic_ip`, `delete_snapshot`, `terminate_instance` |
| **access-reviewer** | IAM least privilege (`IAM_BACKEND=aws`, or `fixture` for tests) | `revoke_access`, `detach_role_policy`, `put_role_policy` |
| **migration-rehearsal** | Postgres migration rehearsal ([docs](docs/migration-rehearsal.md)); optional umbrella member | `apply_migration` |

## Run it from a clean clone

**Prerequisites:**
- Python 3.12+
- Node.js 22.14+
- An AWS account (credentials in the standard boto3 chain, e.g. `~/.aws/credentials`)
- A Daytona API key with sandbox and snapshot permissions
- An OpenAI (or other) model key

```bash
git clone https://github.com/Nikhil7174/trueForge.git && cd trueForge
python3.12 -m venv .venv && .venv/bin/pip install -e '.[aws]'
cp .env.example .env        # set TRUEFORGE_MODEL, SKILL_GIT_URL, SKILL_GIT_REF (no secrets needed for AWS if ~/.aws is set)

make doctor                 # read-only / dry-run AWS permission probe
make seed                   # creates the us-west-2 environment (see "Real vs staged"); run early, IAM telemetry lags ~4 h
```

1. **Start TrueForge.** Allow it to reach the local MCP servers. The allow-list is exact-host, so everything else stays protected.
   ```bash
   OUTBOUND_URL_ALLOWED_HOSTS='["127.0.0.1","localhost"]' npx @truefoundry/trueforge   # UI: http://localhost:8790
   ```
2. **Configure it** at `http://localhost:8790`:
   - **Settings → Models**: add your provider. `TRUEFORGE_MODEL` must be `<provider>/<model name>`.
   - **Settings → Sandbox providers**: add Daytona. Set auto-delete low (for example 60 min), because Daytona's free tier caps total sandbox disk at 30 GiB.
3. **Run the two MCP servers**, one per terminal, and register the agent:
   ```bash
   make mcp-cost   # cost-janitor-aws MCP on :8766
   make mcp-iam    # access-reviewer-iam MCP on :8765 (IAM_BACKEND=aws)
   make agent      # registers skills (git-backed from SKILL_GIT_URL@SKILL_GIT_REF), connectors, and the platform-guardian agent
   ```
4. **Open Agents → platform-guardian** and try:
   - `Clean up unattached EBS volumes and idle IPs in us-west-2.`
   - `Review least privilege for our CI and analytics roles.`
5. **Remove everything the seed created** (plus the backups the agent took) with `make teardown`.

**AWS permissions:**
- Agent, read:
  - `ec2:Describe*`
  - `cloudtrail:LookupEvents`
  - `pricing:GetProducts`
  - `iam:List*`, `iam:Get*`, `iam:GenerateServiceLastAccessedDetails`
- Agent, write:
  - `ec2:CreateSnapshot`, `ec2:CreateTags`, `ec2:DeleteVolume`, `ec2:ReleaseAddress`, `ec2:DeleteSnapshot`, `ec2:TerminateInstances`
  - `iam:DetachRolePolicy`, `iam:PutRolePolicy`
- The seed additionally creates volumes, an Elastic IP, a stopped `t3.micro` and two IAM roles, and assumes those roles once.

## How it stops

- **Approval by literal tool name.** Every destructive tool is listed in `require_approval_for_tools`, not left to `@destructive`. Destructive tools take self-describing arguments, so the native approval prompt shows the blast radius: name, size, monthly cost, backup ID, rollback and reason. The agent also renders a `PreApproval` card first.
- **Policy lives in the MCP, not the prompt.**
  - Resources the server refuses to touch: `env=prod`/`production`, `keep=true`, `do-not-delete`, deny-listed name prefixes, deny-listed principals and AWS service-linked roles.
  - Live state is re-read before every mutation. A call is refused on drift, or when its arguments don't match the real resource.
  - `delete_volume` needs a completed backup snapshot of that same volume.
  - `put_role_policy` is linted server-side: no wildcards, no IAM actions on `*`, no new privilege escalation, no access the role doesn't already have, and conditions are preserved.
  - Every mutation is idempotent, returning `already_absent` if the resource is already gone.
- **The sandbox never holds AWS credentials.** It gets data inline from tool results and runs stdlib-only Python: cost math, policy synthesis, diff and lint.

## TrueForge features used

| Feature | Where |
|---|---|
| Remote MCP connectors (streamable HTTP) | `cost-janitor-aws`, `access-reviewer-iam` (FastMCP servers in this repo) |
| Tool approval (literal names), Allow/Deny with reason | every destructive tool; the denial reason is acknowledged and nothing else changes |
| Sandbox (Daytona) + code execution | cost arithmetic, least-privilege policy synthesis, diff, lint |
| Skills (git-backed, progressive disclosure) | `cost-janitor`, `aws-hygiene-ui`, `access-review-playbook` (+ `scripts/lint_policy.py`), `blast-radius` |
| Generative UI (OpenUI, stock components) | fixed card contract: FindingsTable, CostTable, PlanCard, PolicyDiff, BlastRadius, PreApproval, Outcome (`skills/aws-hygiene-ui`) |
| `ask_user_question` | evidence-first questions, one recommended option with a reason; free text triggers a rewritten re-ask |
| Dynamic subagents | one evidence-gathering subagent per IAM role, run in parallel |
| SDK / settings API | `tf-seed` registers skills, connectors and the agent reproducibly |

## Real vs staged

- **Real:**
  - Every AWS call goes to a live account (us-west-2): EC2/EBS, IAM, CloudTrail, the Price List API.
  - Deletions and approvals are real.
  - Prices are fetched live; none are hardcoded.
  - The sandbox is a real Daytona sandbox.
- **Staged:** the AWS resources themselves. `infra/seed.py` creates the "platform team" environment: waste volumes, an idle IP, a `env=prod` volume that must be refused, and two over-privileged roles it assumes once so the telemetry is genuine. The resource IDs are tracked in a gitignored state file, not in tags.
- **Fixture:** `IAM_BACKEND=fixture` keeps the original in-memory IAM data, for tests only. The demo uses `aws`.
- **Built but off:** a GitHub issues audit trail (`src/trueforge_hackathon/integrations/github_audit.py`). It's enabled by setting `GITHUB_PAT` and `AUDIT_REPO`, and was skipped at the owner's request.

**Known limits:**
- IAM service-last-accessed lags up to about 4 hours and CloudTrail by minutes. Because the staged roles are new, the agent often concludes "insufficient evidence, no revocation", which is intended.
- The staged resources are only minutes old, and the agent says so.
- The sandbox depends on Daytona (free-tier disk cap).
- Stock OpenUI only, with no custom components.
- CloudTrail event names are mapped to IAM action names heuristically.
- Snapshot cost is an upper bound, because snapshots bill for changed blocks.
- One account and one region per run.

---

## Generic TrueForge template (original)

Generic TrueForge integration: **one adapter, many agent plugins**. Everything below documents the original template these plugins follow.

The integration is **Python**. The TrueForge server and UI stay Node (`npx @truefoundry/trueforge`).

For what the hackathon required, what we built first, and what is still stubbed, see [WHAT_WE_BUILT.md](WHAT_WE_BUILT.md).

```text
infra/                                       # seed / teardown / doctor for the live AWS environment
skills/                                      # TrueForge skill catalog (git-backed SKILL.md)
  cost-janitor/                              # AWS waste: evidence, live price, backup, gated delete
  aws-hygiene-ui/                            # shared card + question contract (OpenUI)
  access-review-playbook/                    # IAM least privilege (+ scripts/lint_policy.py)
  blast-radius/                              # shared: explain irreversible actions
  migration-rehearsal/                       # restore → migrate → row diff → verdict (+ sandbox scripts)
  _template/                                 # copy-me pack
src/trueforge_hackathon/
  types.py / registry.py / skill_catalog.py  # AgentPlugin + catalog
  adapter/                                   # client, session/turn, approval pause
  integrations/github_audit.py               # optional GitHub issues audit trail
  agents/_template/                          # copy-me agent (no skill folder here)
  agents/umbrella/                           # composite agent merging member plugins
  agents/cost_janitor/                       # cost-janitor-aws MCP (tf-cost-mcp) + plugin
  agents/access_reviewer/                    # IAM MCP (tf-mcp): backends/fixture.py | backends/aws.py
  agents/migration_rehearsal/                # spec + db-gate MCP (tf-gate) + demo DB seed/migrations
  cli/seed.py
  cli/run.py
tests/migration_rehearsal_e2e.py             # 27 guard checks against db-gate, no TrueForge needed
docs/spikes.md                               # verified TrueForge behaviours (sandbox, OpenUI, questions)
docs/migration-rehearsal.md                  # migration-rehearsal setup + demo script
```

## Skills vs agents

You do **not** put a `skill/` folder inside each agent. Skills are a **shared catalog**:

- Register once (`Settings → Skills` or `tf-seed` with `SKILL_GIT_URL`).
- Attach by name on the agent spec (`access-review-playbook`, `blast-radius`).
- TrueForge clones the pack into the sandbox and loads the body only when the agent picks it.

`access-reviewer` attaches both packs so the demo shows progressive disclosure + a reusable playbook.

## What TrueForge already does

Do not rebuild these: agent loop, MCP routing, sandbox-as-a-tool, Code Mode, compaction, subagents, tool approval, ask-user questions, Generative UI, sessions, schedules, **skills**.

## What a plugin adds

| Slot | Where |
|---|---|
| Job + instructions | `src/trueforge_hackathon/agents/<id>/plugin.py` |
| Side effects | MCP tools with `readOnlyHint` / `destructiveHint` |
| Playbook | `skills/<name>/SKILL.md` (attach by name) |
| Human gate | `require_approval_for_tools` |
| Hard rules | inside the MCP (deny prefixes), not the LLM |

## Prerequisites

- Python 3.12+
- Node.js 22.14+ only for the TrueForge server. Allow the local MCP host, then start it:
  `OUTBOUND_URL_ALLOWED_HOSTS='["localhost","127.0.0.1"]' npx @truefoundry/trueforge` → [http://localhost:8790](http://localhost:8790)
- A model provider configured in TrueForge **Settings → Models**
- Daytona sandbox — **required for this demo**. Skills and Code Mode load `SKILL.md` in the sandbox; without Daytona the playbook never attaches.
- A public GitHub/GitLab clone of **this** repo. TrueForge fetches `skills/` from that URL (see order below).

## Run

```bash
cd trueforge-hackathon
cp .env.example .env
# set TRUEFORGE_MODEL
# optional live AWS: AWS_PROFILE=default AWS_REGION=us-west-2 DEMO_REVOKE_PRINCIPAL=tf-hackathon-throwaway
python3 -m venv .venv
source .venv/bin/activate
pip install -e .              # add '.[migration]' for the migration-rehearsal plugin
```

With `AWS_PROFILE` or `AWS_ACCESS_KEY_ID` set, the same four tools read real IAM. `revoke_access` can detach a policy only from `DEMO_REVOKE_PRINCIPAL` (default `tf-hackathon-throwaway`). Create that user, attach an unused managed policy, and do not give it access keys. Without AWS vars the in-memory fixture is used.

Skills need a public git URL before seed can attach them. Do this in order:

1. Push this repo to GitHub or GitLab.
2. Put that HTTPS URL in `.env` as `SKILL_GIT_URL` (and `SKILL_GIT_REF` if not `main`).
3. Run `tf-seed` (or re-run it if you already seeded without the URL).

Without `SKILL_GIT_URL`, seed still creates the agent and instructions cover the job, but skills do not attach.

`tf-mcp` is a long-running server. Leave it up in **one terminal**, then run seed and run in a **second terminal**:

```bash
# terminal 1 — IAM MCP (fixture or live AWS); leave this running
tf-mcp

# terminal 2 — register connectors, skills, and the named agent
tf-seed

# terminal 2 — or use the TrueForge chat UI: open Agents → access-reviewer
tf-run --agent access-reviewer
```

Same commands as a module: `python -m trueforge_hackathon mcp|gate|seed|run`.

The migration-rehearsal plugin uses its own MCP (`tf-gate`) and a demo Postgres. See [docs/migration-rehearsal.md](docs/migration-rehearsal.md).

Try: `Review unused IAM access. Show blast radius. Do not revoke yet.` Then, on the fixture: `Revoke AmazonS3FullAccess from ci-bot.` On live AWS: `Revoke AmazonS3ReadOnlyAccess from tf-hackathon-throwaway.`

The second prompt should pause on `revoke_access`. In the UI, Allow or Deny. From the CLI:

```bash
tf-run --agent access-reviewer --approve allow --session <session-id> --message "Revoke AmazonS3FullAccess from ci-bot."
```

`BreakGlassAdmin` is on the deny list. The MCP refuses that revoke even if the model asks.

## Add another agent

1. Copy `src/trueforge_hackathon/agents/_template/` to `src/trueforge_hackathon/agents/<job>/`.
2. Fill `plugin.py` (MCP + `require_approval_for_tools`).
3. Add or reuse a pack under `skills/<name>/` and attach it with `resolve_skills(["..."])`.
4. `register_plugin(...)` in `src/trueforge_hackathon/__init__.py`.
5. Ship **one** finished job before starting a second.

## Demo script (5 minutes)

1. Job: find unused IAM access; revoke nothing alone.
2. **Reach:** `list_principals` / `get_access_last_used`.
3. **Skill + sandbox:** playbook loaded from `skills/`; Code Mode builds the unused table.
4. Freeze on `revoke_access` with a blast-radius card. Allow.
5. Confirm the policy is gone with `get_principal_policies`.
6. Sessions: tool calls, skill, sandbox, approval event.

## Judging map

| Requirement | This template |
|---|---|
| Reach something real | MCP to IAM fixture (swap in live AWS behind `AWS_*`) |
| Run what it writes | Sandbox / Code Mode + skill pack cloned into the sandbox |
| Know when to stop | `revoke_access` + `blast-radius` skill + named approval |

Do not put keys in git. Disclose AI assistants in this README when you submit.

**AI assistants used while building:** Cursor; Claude (Anthropic) for the migration-rehearsal plugin; Claude Code (Anthropic) for the aws-hygiene work (infra, cost-janitor, IAM AWS backend, umbrella, skills).
