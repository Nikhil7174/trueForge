# release-captain: cut releases that can't outrun their migrations

An agent that reads every commit since the last tag, runs the test suite in a TrueForge sandbox,
drafts release notes a human would publish, and **refuses to publish while any migration in the
release is not applied to production.**

That refusal is the point. A release is a *compound* irreversible action: the tag goes up, CI ships
the code, and if a migration the code depends on never landed, production breaks. The migration
agent can't see the release; a plain release agent can't see the schema. `publish_release` closes
that gap in code, by reading db-gate's ledger — so "applied" means *a human approved
`apply_migration` after a gate-verified rehearsal*, not that somebody said so in chat.

```
TrueForge local (npx)                      your machine
┌────────────────────────────┐             ┌──────────────────────────────────────┐
│ release-captain agent      │  MCP (HTTP, │ ship-gate  (tf-ship)                 │
│  skills: release-captain   │  bearer)    │  release_scope        GitHub PAT     │──► GitHub
│          migration-rehearsal│◄──────────►│  submit_verification  ledger only    │
│          blast-radius      │             │  publish_release ⛔   tag + release   │
│  approvals: publish_release │             └──────────────┬───────────────────────┘
│             apply_migration │                            │ reads .gate/ledger.db
└──────────────┬─────────────┘             ┌──────────────▼───────────────────────┐
               │ sandbox tool              │ db-gate  (tf-gate)                   │──► prod Postgres
┌──────────────▼─────────────┐             │  apply_migration ⛔  write role       │
│ Daytona sandbox (no creds) │             └──────────────────────────────────────┘
│  verify_build.py: clone →  │
│  install → test → report   │
└────────────────────────────┘
```

## How the pieces meet the three requirements

| Requirement | Where it happens |
|---|---|
| **Reaches something real** | `ship-gate` → the GitHub REST API with a real PAT: commits, changed files, file contents, and (gated) tags and releases |
| **Runs what it writes** | `verify_build.py` in the sandbox: a real clone at a real commit, a real install, the project's real test suite |
| **Knows when to stop** | `publish_release` is annotated destructive and listed in `require_approval_for_tools`, so TrueForge pauses with Allow/Deny — and the gate enforces its own rules first |

### Two irreversible steps, two separate human decisions

The captain carries the `migration-rehearsal` skill and db-gate as well, so when a release is
blocked it can fix the cause instead of handing you to another agent. Both gates keep their own
approval: applying a migration to production and publishing a release are different decisions, and
approving one never approves the other.

### What `publish_release` refuses (in code, not in the prompt)

- **any migration in the release that is not `applied`** — naming each one and its state:
  `not_rehearsed`, `blocked`, `ready_safe`, `ready_needs_review`. A migration rehearsed and
  verdicted SAFE is still refused: SAFE is not applied.
- a verification whose recomputed verdict is FAIL
- a `target_repo` or `release_summary` that doesn't match the gate's record. These are arguments on
  purpose: the approval card shows the real repository and the real verified impact line.
- empty release notes
- a tag that already exists, or isn't a usable git tag name
- a repository that moved on since verification (HEAD drift)
- a stale verification (over 24 h), or a repeat publish of the same tag

On `submit_verification` the gate recomputes PASS/FAIL from the raw test findings rather than
trusting the report's label, and a digest over the report catches edits in transit — the same
contract db-gate uses for rehearsals.

If creating the release fails after the tag was created, the gate **deletes the tag** rather than
leaving litter behind.

## Setup (about 5 minutes on top of migration-rehearsal)

Do [docs/migration-rehearsal.md](migration-rehearsal.md) first; ship-gate shares its `GATE_TOKEN`
and its ledger.

1. **A public repository you own**, with at least one tag. It must be public: `verify_build.py`
   clones it from the sandbox, which holds no token by design.

2. **A fine-grained PAT** on that repository with **Contents: read and write** (tags and releases).
   ship-gate is the only holder.

3. **`.env`:**
   ```bash
   GITHUB_REPO=your-org/your-repo
   GITHUB_TOKEN=github_pat_...
   SHIP_PORT=8812
   SHIP_MIGRATION_PATHS=migrations/,demo/migrations/   # what counts as a migration
   ```

4. **Start it** in its own terminal, next to `tf-gate`:
   ```bash
   tf-ship          # → MCP at http://127.0.0.1:8812/mcp
   ```

5. **Seed the agent:**
   ```bash
   tf-seed release-captain
   ```

6. **Run it** in the TrueForge UI (Agents → release-captain), or:
   ```bash
   tf-run --agent release-captain --message "Cut release v1.4.0."
   ```

## Demo script (what to film)

The whole submission is beat 4. Do not rush it.

1. **Scope.** *"Cut release v1.4.0."* The captain calls `release_scope`: commits since the last
   tag, changed files, and the migrations in the range with their sha256 and ledger state.
2. **Verify.** It clones the repo at that commit in the sandbox and runs the test suite.
   **The tests pass.**
3. **Notes.** It drafts release notes from the actual commits.
4. **The refusal.** It calls `publish_release` and the gate says no: *"1 migration in this release
   is not applied to production: migrations/0101_add_mbi_normalized.sql (3f9a…) is not_rehearsed.
   Publishing would ship code that expects a schema production does not have."*
   **Green tests, and it refused to ship.** Sit on this.
5. **The fix.** The captain switches to the rehearsal skill: snapshot production, restore it in the
   sandbox, run the migration, diff every row, get a gate verdict.
6. **Approval #1.** `apply_migration` pauses. The card shows the exact SQL, the target and the
   gate-verified impact line. Approve.
7. **Approval #2.** `publish_release` again — now every migration is `applied`, so it reaches the
   approval card: repository, tag, commit, and that a published release notifies watchers and
   cannot be quietly withdrawn. Approve.
8. **Sessions.** Show the tool calls, the sandbox, and both approval events.

Worth saying out loud: **the invariant is directional.** Additive migrations must land *before* the
code that reads them; a destructive migration is the other way round — the code must stop using the
column first. This gate enforces the additive case, which is the common one, and we know the
destructive case inverts.

## Verify without TrueForge and without GitHub

`tests/release_captain_e2e.py` plays the agent: it calls ship-gate over MCP exactly as TrueForge
does, against a local GitHub stub, and runs **27 checks** — bearer auth, annotations, scope and
sha256 agreement with `gatecore`, tampered and relabelled reports, wrong repo, HEAD drift, failing
suites, every migration state, paraphrased summaries, bad tags, duplicate publishes, tag rollback,
and that refusals are recorded in the ledger.

```bash
python tests/release_captain_e2e.py      # starts its own stub and gate; no token, no network
```

The stub stands in for the transport only; every refusal under test is ship-gate's own. Migration
states are written straight into the ledger table ship-gate reads, which keeps the test hermetic —
the live demo gets them the real way, by rehearsing and applying through db-gate.

## Layout

```
src/trueforge_hackathon/agents/release_captain/
  plugin.py          agent spec, ship-gate + db-gate connectors, skills, two approval gates
  ship_gate.py       ship-gate MCP server (`tf-ship`): the only holder of the GitHub token
  github.py          GitHub REST; only create_tag and create_release write
skills/release-captain/SKILL.md            the procedure the agent follows
skills/release-captain/scripts/
  verify_build.py    runs in the sandbox: clone at a ref, install, test, write the report
  shipcore.py        digest + PASS/FAIL rules shared by the sandbox and the gate
tests/release_captain_e2e.py               27 guard checks against a GitHub stub
```

`ship_gate.py` imports `gatecore` from the migration skill pack so a migration sha computed here is
byte-identical to one rehearsed there. Install with `pip install -e` to keep that path valid.

## Honest limits

- **A tag is recoverable; a release is not, quite.** Deleting a tag is easy, but a published release
  notifies watchers and may already have been mirrored. The gate treats publishing as the
  irreversible step, and the approval card says so.
- **No package registry yet.** Publishing to npm or PyPI is genuinely irreversible and would belong
  behind the same gate; `publish_release` stops at the GitHub release.
- **`verify_build.py` needs a public repo.** Giving the sandbox a token to clone a private one would
  put a credential in the sandbox, which the whole design avoids. A deploy key scoped to read would
  be the next step.
- **Test-count parsing is best-effort.** The verdict rests on the suite's exit code; the
  passed/failed numbers in the summary are parsed from output and are cosmetic.
- **The directional invariant is not modelled.** The gate enforces "applied before publish". A
  destructive migration needs the opposite order and the gate does not know the difference.
- **One repository per gate.** `GITHUB_REPO` is fixed at startup, deliberately: `target_repo` is an
  argument only so the approval card can show it, never so the model can choose it.

## Built with

- [TrueForge](https://github.com/truefoundry/trueforge) (agent harness, sandbox, tool approval)
- MCP Python SDK; GitHub REST v3 over `urllib` (no extra dependencies)
- **AI assistants used while building:** Claude (Anthropic) for design and code.
