# TrueForge hackathon template

Generic TrueForge integration: **one adapter, many agent plugins**.

| Plugin | Job | Gate |
|---|---|---|
| **access-reviewer** | Official starter #05: reach IAM, find unused access | `revoke_access` |
| **migration-rehearsal** | Restore prod into a sandbox, run a Postgres migration there, diff every row, report a SAFE / REVIEW / BLOCK verdict. Setup and demo: [docs/migration-rehearsal.md](docs/migration-rehearsal.md) | `apply_migration` |

TrueForge runs the agent loop. We only supply the job, MCP tools, **skills catalog**, and approval policy.

The integration is **Python**. The TrueForge server and UI stay Node (`npx @truefoundry/trueforge`).

For what the hackathon required, what we built, and what is still stubbed, see [WHAT_WE_BUILT.md](WHAT_WE_BUILT.md).

```text
skills/                                      # TrueForge skill catalog (git-backed SKILL.md)
  access-review-playbook/                    # unused IAM + Code Mode + stop
  blast-radius/                              # shared: explain irreversible actions
  migration-rehearsal/                       # restore → migrate → row diff → verdict (+ sandbox scripts)
  _template/                                 # copy-me pack
src/trueforge_hackathon/
  types.py / registry.py / skill_catalog.py  # AgentPlugin + catalog
  adapter/                                   # client, session/turn, approval pause
  agents/_template/                          # copy-me agent (no skill folder here)
  agents/access_reviewer/                    # spec + MCP only; attaches skills by name
  agents/migration_rehearsal/                # spec + db-gate MCP (tf-gate) + demo DB seed/migrations
  cli/seed.py
  cli/run.py
tests/migration_rehearsal_e2e.py             # 27 guard checks against db-gate, no TrueForge needed
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
- Node.js 22.14+ only for the TrueForge server: `npx @truefoundry/trueforge` → [http://localhost:8790](http://localhost:8790)
- A model provider configured in TrueForge **Settings → Models**
- Daytona sandbox — **required for this demo**. Skills and Code Mode load `SKILL.md` in the sandbox; without Daytona the playbook never attaches.
- A public GitHub/GitLab clone of **this** repo. TrueForge fetches `skills/` from that URL (see order below).

## Run

```bash
cd trueforge-hackathon
cp .env.example .env
# set TRUEFORGE_MODEL
python3 -m venv .venv
source .venv/bin/activate
pip install -e .              # add '.[migration]' for the migration-rehearsal plugin
```

Skills need a public git URL before seed can attach them. Do this in order:

1. Push this repo to GitHub or GitLab.
2. Put that HTTPS URL in `.env` as `SKILL_GIT_URL` (and `SKILL_GIT_REF` if not `main`).
3. Run `tf-seed` (or re-run it if you already seeded without the URL).

Without `SKILL_GIT_URL`, seed still creates the agent and instructions cover the job, but skills do not attach.

`tf-mcp` is a long-running server. Leave it up in **one terminal**, then run seed and run in a **second terminal**:

```bash
# terminal 1 — IAM fixture MCP; leave this running
tf-mcp

# terminal 2 — register connectors, skills, and the named agent
tf-seed

# terminal 2 — or use the TrueForge chat UI: open Agents → access-reviewer
tf-run --agent access-reviewer
```

Same commands as a module: `python -m trueforge_hackathon mcp|gate|seed|run`.

The migration-rehearsal plugin uses its own MCP (`tf-gate`) and a demo Postgres. See [docs/migration-rehearsal.md](docs/migration-rehearsal.md).

Try: `Review unused IAM access. Show blast radius. Do not revoke yet.` Then: `Revoke AmazonS3FullAccess from ci-bot.`

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

**AI assistants used while building:** Cursor; Claude (Anthropic) for the migration-rehearsal plugin.
