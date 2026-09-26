# What we had to do, and what is in this repo

This is the design-and-implementation record for the TrueFoundry × Polaris hackathon project **Agents That Act**, living in `/Users/nikhilkumarsingh/trueforge-hackathon` (sibling of `zynix`, not part of it).

Official brief: [truefoundry.com/truefoundry-hackathon#build](https://www.truefoundry.com/truefoundry-hackathon#build).  
TrueForge docs: [trueforge.dev/introduction](https://trueforge.dev/introduction).

---

## 1. What we had to do

### Theme

Not a chatbot. An agent that **reaches a real system, runs generated code in a sandbox, and stops before something irreversible.**

| Requirement | Meaning |
|---|---|
| Reach something real | MCP to a real system (or a live-shaped fixture). No mocked “function that returns fixtures” as the whole product. |
| Run what it writes | Generated code executes in TrueForge’s sandbox (Daytona). Skills and Code Mode need this. |
| Know when to stop | Pause before revoke / delete / apply-to-prod. Blast radius must be obvious. |

Judges score the harness doing the work (real tool call + sandbox + approval), that it actually runs, where it stops, whether the job is worth delegating, and a five-minute demo.

### What TrueForge already is (do not rebuild)

TrueForge is an **agent harness**: the loop around the model (plan → model → tools → results), plus MCP routing, skills, sandbox-as-a-tool, approvals, subagents, compaction, sessions, and a bundled chat UI.

Mental model: **Agent (saved spec) → Session → Turn → Event → Delta**.

It does **not** come with IAM “list unused access / detach policy.” The [MCP catalog](https://trueforge.dev/api-reference/mcp-servers/get-the-mcp-catalog) is discovery of shipped connectors (GitHub, Linear, Exa, …). You copy those into Settings, or register **your own URL**. Agents attach connectors **by name**. There is no “try TrueForge MCP first, then fall back to ours.”

### What we had to build

- One **job** (we chose official starter #05: Access Reviewer).
- A **domain MCP** with annotated read vs destructive tools.
- **Skills** (`SKILL.md` playbooks) in a shared catalog.
- An **agent spec** (instructions, MCP attach, approval policy, sandbox / Generative UI / subagents on).
- **Policy in the MCP** (deny break-glass), not a hope the model is careful.
- Seed + run so someone else can clone and operate it.
- Use TrueForge UI; do not write a second chat product.

Zynix was **design reference only** (plugin registry, policy at the trust boundary). No Zynix code, PHI, or prod keys.

---

## 2. What we decided

**First plugin:** Access Reviewer, not Fargate janitor.

> List IAM users and roles, find unused or over-broad access, draft a least-privilege diff with blast radius, **revoke nothing until a human approves.**

**Template + one plugin:** one TrueForge adapter and agent registry; swap business logic by adding a folder under `src/trueforge_hackathon/agents/` and packs under `skills/`.

**Language:** the whole integration is **Python** (`trueforge-sdk`, FastMCP). TrueForge’s own server/UI stay Node (`npx @truefoundry/trueforge`). We do not rebuild that.

**MCP:** our own thin server (`access-reviewer-iam`). TrueForge only routes to it.

**UI:** bundled TrueForge chat at `http://localhost:8790` (`npx @truefoundry/trueforge`). We did not embed `@truefoundry/trueforge-ui` and did not build a React app.

**Skills:** repo-root `skills/` catalog (TrueForge-native: git-backed, attach by name). Not a `skill/` folder inside each agent.

---

## 3. What is in the code

### Layout

```text
trueforge-hackathon/
  README.md                 How to run
  WHAT_WE_BUILT.md          This file
  pyproject.toml            Python package + tf-mcp / tf-seed / tf-run
  skills/                   TrueForge skill catalog
    access-review-playbook/
    blast-radius/
    _template/
  src/trueforge_hackathon/
    types.py / registry.py / skill_catalog.py
    adapter/                SDK client, session/turn stream, approval pause
    agents/access_reviewer/ First plugin: spec + MCP + fixture store
    agents/_template/       Copy-me agent (no skill folder)
    cli/seed.py             Register MCP + skills + named agent
    cli/run.py              One turn from the CLI
    __init__.py             register_plugin(access-reviewer)
```

### Generic template (write once)

| Piece | Path | What it does |
|---|---|---|
| Plugin type | `src/trueforge_hackathon/types.py` | `AgentPlugin`: name, description, TrueForge `manifest`, optional MCP URL, skill refs, deny prefixes |
| Registry | `src/trueforge_hackathon/registry.py` | `register_plugin` / `get_plugin` / `list_plugins` |
| Skill catalog | `src/trueforge_hackathon/skill_catalog.py` | Named packs under `skills/`; resolved with `SKILL_GIT_URL` |
| Client | `src/trueforge_hackathon/adapter/client.py` | `TrueForge` SDK (`base_url`, optional OIDC token, 600s timeout) |
| Session runner | `src/trueforge_hackathon/adapter/run_session.py` | `sessions.create` → `create_turn_stream` → fold deltas → collect `tool.approval_required` → resume with `user.tool_approval` allow/deny/pause |
| Seed | `src/trueforge_hackathon/cli/seed.py` | `settings.mcp_servers.create_or_update`, optional `settings.skills.create_or_update`, `agents.create` or update on 409 |
| Run CLI | `src/trueforge_hackathon/cli/run.py` | `--agent`, `--message`, `--session`, `--approve allow\|deny` |

Adding another job: copy `_template`, add MCP + attach skill names, `register_plugin` in `__init__.py`. Do not fork the adapter.

### Access Reviewer plugin (in code)

`src/trueforge_hackathon/agents/access_reviewer/plugin.py`:

- Agent name: `access-reviewer`
- Model from `TRUEFORGE_MODEL`
- Instructions: unused-access job, deny prefixes, use skills, Code Mode for the table, revoke only on request
- Attaches MCP `access-reviewer-iam` with `require_approval_for_tools: ["revoke_access"]` and `preload: true`
- Attaches skills `access-review-playbook` and `blast-radius` **only if** `SKILL_GIT_URL` is set (TrueForge clones git skills; unknown names fail agent create)
- Runtime config on: sandbox, Generative UI, ask-user questions, dynamic subagents, compaction, large-tool offload, iteration limit 50

### Our MCP (in code)

`src/trueforge_hackathon/agents/access_reviewer/mcp_server.py` — Streamable HTTP at `http://127.0.0.1:8765/mcp`.

| Tool | Annotation | Behavior |
|---|---|---|
| `list_principals` | `readOnlyHint` | Users/roles from the in-memory store |
| `get_principal_policies` | `readOnlyHint` | Policies, last used, blast-radius text |
| `get_access_last_used` | `readOnlyHint` | Unused if `lastUsedAt` is null or older than `UNUSED_AFTER_DAYS` (90) |
| `revoke_access` | `destructiveHint: true` | Detach policy in the store, or refuse deny-list names |

Policy in `store.py`: deny prefixes `Admin`, `BreakGlass`, `OrganizationAccountAccessRole` (override with `DENY_PRINCIPAL_PREFIXES`). Fixture principals: `ci-bot`, `legacy-analytics-role`, `oncall-readonly`, `BreakGlassAdmin`.

Live AWS is **not** wired. `.env.example` has `AWS_*` placeholders only.

### Skills catalog (in code)

| Pack | Role |
|---|---|
| `skills/access-review-playbook/` | Reach tools, unused rule, Code Mode table, stop before revoke. Extra: `references/unused-rule.md` |
| `skills/blast-radius/` | Shared: spell out what breaks before any destructive tool |
| `skills/_template/` | Copy-me pack |

These are TrueForge skills: register once, attach by name, load in the sandbox on demand.

### UI (not in this repo)

Chat, Allow/Deny, Agents, Sessions, Settings = **TrueForge bundled UI** after `npx @truefoundry/trueforge`.  
This repo: MCP process + seed/CLI + specs. No React app.

### Scripts

```bash
tf-mcp          # IAM fixture MCP
tf-seed         # register connector + (optional) skills + agent
tf-run          # one SDK turn
```

---

## 4. Flow (what handles what)

TrueForge does **not** try a built-in MCP first. The agent only sees tools on connectors listed in its spec. For us that is one URL: our MCP.

```text
Operator  →  TrueForge UI or tf-run
                 │
                 ├── model, skills, sandbox, Generative UI, approvals
                 └── HTTP POST http://127.0.0.1:8765/mcp
                           └── list / last-used / revoke (+ deny list)
```

1. User: “Review unused access.”
2. TrueForge + model call `list_principals` / `get_access_last_used` on **our** MCP.
3. Optional: sandbox / Code Mode builds the unused table (needs Daytona).
4. User: “Revoke AmazonS3FullAccess from ci-bot.”
5. TrueForge pauses (`revoke_access`). Operator Allow/Deny in the UI (or `--approve` on the CLI).
6. Our MCP detaches in the fixture, or refuses `BreakGlassAdmin`.

---

## 5. Feature checklist

### Implemented

- [x] Generic `AgentPlugin` + registry
- [x] TrueForge SDK adapter (session, SSE, approval resume)
- [x] Seed MCP + named agent
- [x] Access Reviewer spec (approvals, sandbox, Generative UI, subagents, compaction)
- [x] Custom MCP: 3 read tools + 1 destructive revoke
- [x] MCP deny list for break-glass / admin
- [x] In-memory IAM fixture with unused vs used vs denied principals
- [x] Shared `skills/` catalog (two real packs + template)
- [x] Copy-me agent template
- [x] CLI run with allow / deny / pause
- [x] TrueForge bundled UI as the product surface
- [x] README how-to + this document
- [x] Entire integration in Python (`trueforge-sdk` + FastMCP)
- [x] Second plugin: **migration-rehearsal** (see section 8)

### Designed, not coded yet

- [ ] Live AWS IAM behind the same four tools
- [ ] Skill registration without a public git URL (needs `SKILL_GIT_URL` after push)
- [ ] Custom / embedded `@truefoundry/trueforge-ui`
- [ ] Third plugin (Fargate janitor)
- [ ] TrueForge Schedules (daily unused-access brief)
- [ ] Catalog MCP (GitHub, etc.) attached alongside ours

### Intentionally not built

- Custom agent loop, tool router, or “MCP gateway” in front of TrueForge
- Zynix / healthcare / Service Bus / GraphQL
- Secrets in git
- A second chat UI
- A Node copy of the adapter / MCP / seed (TrueForge server itself remains `npx`)

---

## 6. Demo script (five minutes)

1. Job: find unused IAM access; revoke nothing alone.
2. Reach: live tool calls to our MCP (`list_principals` / `get_access_last_used`).
3. Sandbox: unused table / diff (if Daytona is configured).
4. Freeze on `revoke_access` with blast radius. Allow.
5. Confirm detach with `get_principal_policies`.
6. Sessions: tool calls, approval event.

Try revoke on `BreakGlassAdmin` to show MCP policy (refused even if the model asks).

---

## 7. How this maps to the rubric

| Rubric | How we hit it |
|---|---|
| Harness is doing the work | TrueForge calls our MCP, can run Code Mode, pauses on `revoke_access` |
| It actually runs | `tf-mcp` + `tf-seed` + TrueForge UI or `tf-run` |
| Where it stops | Named approval + `destructiveHint` + deny list |
| Job worth handing over | Unused IAM review is a real chore |
| Demo clarity | One job, one gate, TrueForge Sessions as the log |

---

## 8. Second plugin: migration-rehearsal

> Take a Postgres migration, restore a copy of production into the sandbox, run the migration there, diff what happened to every row, and apply to production only through an approval-gated tool.

| Piece | Path |
|---|---|
| Agent spec + connector | `src/trueforge_hackathon/agents/migration_rehearsal/plugin.py` (MCP `db-gate` with bearer auth, `require_approval_for_tools: ["apply_migration"]`, skill `migration-rehearsal`) |
| MCP | `src/trueforge_hackathon/agents/migration_rehearsal/gate_server.py` (`tf-gate`): `export_snapshot` (read-only role), `submit_rehearsal`, `apply_migration` (destructive), `migration_status` |
| Skill pack | `skills/migration-rehearsal/` with `rehearse.py` (runs in the sandbox), `gatecore.py` (verdict rules shared with the gate), `setup_sandbox.sh` |
| Demo data | `agents/migration_rehearsal/demo/` (`tf-gate-seed-demo`: synthetic Medicare ACO claims DB, no PHI) and three demo migrations |
| Guard test | `tests/migration_rehearsal_e2e.py`: 23 checks (tampered reports, drift, stale rehearsals, rollback, …) |

Policy lives in the gate, not the prompt: `apply_migration` refuses SQL that wasn't rehearsed, BLOCK verdicts, REVIEW without `accept_review`, schema drift, stale rehearsals, double applies and non-transactional statements, and rolls back when prod's post-apply schema differs from the rehearsal.

Template changes it needed: `McpConnector.headers` (seeded as TrueForge header auth), `.env` loaded before plugins register, and `tf-gate` / `tf-gate-seed-demo` scripts. Full setup and demo: [docs/migration-rehearsal.md](docs/migration-rehearsal.md).

**AI assistants used while building:** Cursor.
