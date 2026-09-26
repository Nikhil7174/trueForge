# platform-guardian: one platform agent on TrueForge

One TrueForge agent, **platform-guardian**, does five jobs a platform team rarely has time for. Every task starts as a **Linear ticket**; its **label** picks the job. Every irreversible step pauses on TrueForge's native approval.

| Plugin | Job | Gate |
|---|---|---|
| **access-reviewer** | Official starter #05: reach IAM, find unused access | `revoke_access` |
| **migration-rehearsal** | Restore prod into a sandbox, run a Postgres migration there, diff every row, report a SAFE / REVIEW / BLOCK verdict. Setup and demo: [docs/migration-rehearsal.md](docs/migration-rehearsal.md) | `apply_migration` |
| **release-captain** | Read the commits since the last tag, run the tests in a sandbox, draft release notes - and refuse to publish while any migration in the release is unapplied. Setup and demo: [docs/release-captain.md](docs/release-captain.md) | `publish_release` |
| **ticket-resolver** | Reproduce a Linear bug ticket in a sandbox, prove any fix, and reply only after approval. Data corrections are handed, in order, to migration-rehearsal and the reply waits for db-gate's verdict: [docs/ticket-migration-handoff.md](docs/ticket-migration-handoff.md) | `reply_to_customer` |

Shared by all jobs: the **umbrella plugin** that merges the five into one agent, the **single Linear trigger** that routes by label, the **`aws-hygiene-ui` card contract** (Generative UI), and the `infra/` AWS scripts (Sai). The adapter, plugin registry, skill catalog and `tf-seed` come from Nikhil's original template.

```text
Linear ticket (team ZYN) + routing label
  │  status on the ticket via the Agent label group:
  │  Working → Awaiting approval → Replied / Declined / Failed
  ▼
tf-ticket-trigger (make linear)          one entry point, poll and/or webhook, deduped
  ▼
TrueForge :8790 ── platform-guardian     one agent; questions and approvals answered in the UI
  ├─ cost-janitor-aws   :8766 ─ AWS EC2/EBS, CloudTrail, Price List
  ├─ access-reviewer-iam :8765 ─ AWS IAM, CloudTrail
  ├─ db-gate            :8811 ─ Postgres
  ├─ ship-gate          :8812 ─ GitHub
  ├─ ticket-gate        :8821 ─ Linear writes + product code
  ├─ linear (catalog connector) ─ ticket reads
  ├─ 7 git-backed skills, Daytona sandbox, dynamic subagents, OpenUI cards
  └─ native approval on every destructive tool, by exact name
```
<img width="1478" height="546" alt="Screenshot 2026-09-26 at 5 55 26 PM" src="https://github.com/user-attachments/assets/df0c6fa9-a243-4ddd-867a-68d47ef32f11" />

## Run it from a clean clone

**Prerequisites:**
- Python 3.12+ and Node.js 22.14+
- AWS credentials in the standard boto3 chain (e.g. `~/.aws/credentials`)
- In TrueForge Settings: a model provider, a Daytona key (sandbox + snapshot permissions), and the catalog **Linear** connector
- For the non-AWS jobs: a Postgres (migration), a GitHub repo + PAT (release), a Linear API key (tickets). See `.env.example`.

```bash
git clone https://github.com/Nikhil7174/trueForge.git && cd trueForge
python3.12 -m venv .venv && .venv/bin/pip install -e '.[aws,migration]'
cp .env.example .env        # fill in the values; never commit .env

make doctor                 # read-only / dry-run AWS permission probe
make seed                   # the us-west-2 environment for cloudcost / iam-review (see "Real vs staged")
```

1. **Start TrueForge.** Allow it to reach the local MCP servers (an exact-host allow-list; everything else stays protected):
   ```bash
   OUTBOUND_URL_ALLOWED_HOSTS='["127.0.0.1","localhost"]' npx @truefoundry/trueforge   # UI: http://localhost:8790
   ```
   In **Settings**: add the model provider (`TRUEFORGE_MODEL=<provider>/<model>`), Daytona (set auto-delete low, e.g. 60 min, because the free tier caps sandbox disk at 30 GiB), and the **Linear** connector.
2. **Start the gates**, one terminal each. You only need the ones for the labels you use:
   ```bash
   make mcp-cost               # :8766  cloudcost
   make mcp-iam                # :8765  iam-review (IAM_BACKEND=aws)
   .venv/bin/tf-gate           # :8811  migration
   .venv/bin/tf-ship           # :8812  release
   .venv/bin/tf-ticket-gate    # :8821  Bug
   ```
3. **Register the one agent:** `make agent`. This registers the skills (git-backed from `SKILL_GIT_URL@SKILL_GIT_REF`), the connectors and **platform-guardian**. Plain `tf-seed` would register every plugin as its own agent instead.
4. **Start the single Linear entry point:** `make linear`. It routes by label (`LINEAR_ROUTES`) and only picks up tickets created after it starts.
5. **File a ticket** in Linear with a routing label, e.g. `cloudcost`: *Clean up unattached EBS volumes and idle IPs in us-west-2*. Within about 20 s the ticket shows `Working`, and a session appears in TrueForge → **Sessions**. Click **Resume Chat** to answer questions and Allow or Deny approvals. The outcome is posted back to the ticket.
6. **Clean up:** `make teardown` removes everything `make seed` created, plus the backups the agent took.

You can also chat with **Agents → platform-guardian** directly in the UI, without Linear.

**AWS permissions:**
- Agent, read:
  - `ec2:Describe*`
  - `cloudtrail:LookupEvents`
  - `pricing:GetProducts`
  - `iam:List*`, `iam:Get*`, `iam:GenerateServiceLastAccessedDetails`
- Agent, write:
  - `ec2:CreateSnapshot`, `ec2:CreateTags`, `ec2:DeleteVolume`, `ec2:ReleaseAddress`, `ec2:DeleteSnapshot`, `ec2:TerminateInstances`
  - `iam:DetachRolePolicy`, `iam:PutRolePolicy` (`iam-review` with `IAM_BACKEND=aws`)
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
| SDK / settings API | `tf-seed` / `make agent` registers skills, connectors and the agent reproducibly; the Linear trigger starts sessions through the SDK |
| Catalog MCP connector | `linear` (ticket reads for ticket-resolver) |

## Real vs staged

- **Real:**
  - Every AWS call goes to a live account (us-west-2): EC2/EBS, IAM, CloudTrail, the Price List API.
  - Deletions and approvals are real.
  - Prices are fetched live; none are hardcoded.
  - The sandbox is a real Daytona sandbox.
- **Staged:** the AWS resources themselves. `infra/seed.py` creates the "platform team" environment: waste volumes, an idle IP, a `env=prod` volume that must be refused, and two over-privileged roles it assumes once so the telemetry is genuine. The resource IDs are tracked in a gitignored state file, not in tags.
- **Fixture:** `IAM_BACKEND=fixture` keeps in-memory IAM data for tests. platform-guardian runs on `aws`.
- **Optional:** a GitHub issues audit trail for the AWS jobs (`src/trueforge_hackathon/integrations/github_audit.py`), enabled by setting `GITHUB_PAT` and `AUDIT_REPO`.

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

```text
infra/                                       # seed / teardown / doctor for the live AWS environment
skills/                                      # TrueForge skill catalog (git-backed SKILL.md)
  cost-janitor/                              # AWS waste: evidence, live price, backup, gated delete
  aws-hygiene-ui/                            # shared card + question contract (OpenUI)
  access-review-playbook/                    # IAM least privilege (+ scripts/lint_policy.py)
  blast-radius/                              # shared: explain irreversible actions
  migration-rehearsal/                       # restore → migrate → row diff → verdict (+ sandbox scripts)
  release-captain/                           # scope → verify → notes → gated publish (+ sandbox scripts)
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
  agents/release_captain/                    # spec + ship-gate MCP (tf-ship) + GitHub REST
  cli/seed.py
  cli/run.py
tests/migration_rehearsal_e2e.py             # 28 guard checks against db-gate, no TrueForge needed
tests/release_captain_e2e.py                 # 27 guard checks against ship-gate, no GitHub needed
docs/migration-rehearsal.md                  # migration-rehearsal setup + demo script
docs/release-captain.md                      # release-captain setup + demo script
docs/ticket-migration-handoff.md             # ticket-resolver → migration-rehearsal serial handoff
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

The migration-rehearsal plugin uses its own MCP (`tf-gate`) and a demo Postgres. See
[docs/migration-rehearsal.md](docs/migration-rehearsal.md). The release-captain plugin adds
`tf-ship` and a GitHub PAT, and reads db-gate's ledger to check migrations are applied. See
[docs/release-captain.md](docs/release-captain.md).

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

## Demo script

1. **File a ticket** in Linear with a routing label, e.g. `cloudcost`: *Clean up unattached EBS volumes and idle IPs in us-west-2*. It moves to `Working`.
2. **Reach:** platform-guardian reads live AWS (volumes, IPs, CloudTrail, live prices). The `env=prod` volume shows as protected.
3. **Run what it writes:** the cost math runs in the Daytona sandbox. FindingsTable and CostTable cards render in the chat.
4. **Ask:** one scope question, with a Recommended option and its reason. The ticket shows `Awaiting approval`.
5. **Stop:** a PreApproval card, then the native approval for each deletion. Allow or Deny.
6. **Close the loop:** the Outcome card shows verified savings, the result is posted to the ticket, and the full trace is in Sessions.

`release-captain` goes one step further: `publish_release` refuses while a migration in the release
is unapplied, so passing tests are not enough to ship. That rule lives in the gate, not the prompt.

Keys never go in git: `.env` is gitignored, and connector credentials live in TrueForge Settings.

**AI assistants used while building:** Cursor; Claude (Anthropic) for the migration-rehearsal plugin; Claude Code (Anthropic) for the platform-guardian work (infra, cost-janitor, IAM AWS backend, umbrella agent, label-routed Linear trigger, skills, docs).
