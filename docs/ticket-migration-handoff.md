# ticket-resolver → migration-rehearsal: serial handoff

Two separate TrueForge agents, run one after the other, each with its own session, gate and approval.
ticket-resolver never touches a database. migration-rehearsal never touches Linear.

```text
Linear ticket (Bug)
  └─ tf-ticket-trigger ─ starts ─▶ ticket-resolver session
                                    reproduce → submit_attempt = REPRODUCED_NO_FIX
                                    write the correcting migration
                                    ticket-gate.request_migration  ──▶ handoffs row: queued
                                    turn ends (propose_reply is refused while queued)
  handoff worker (in tf-ticket-trigger)
    1. claims the oldest queued handoff            queued → rehearsing
    2. waits until that ticket session is idle
    3. starts a migration-rehearsal session ─────▶ snapshot → rehearse → db-gate.submit_rehearsal
                                                    (no apply; that stays a separate, explicit request)
    4. ticket-gate reads db-gate's ledger by the SQL's sha256
                                                    rehearsing → rehearsed (SAFE / REVIEW / BLOCK)
                                                              or no_verdict / failed
    5. resumes the SAME ticket-resolver session with the verdict
                                    propose_reply (now accepted) → reply_to_customer ⏸ human Allow/Deny
```

Why each step holds:

- **Order is enforced by ticket-gate, not by prompts.** `propose_reply` refuses while the ticket has a queued or
  rehearsing handoff, so the reply can't overtake the rehearsal even if the agent ignores its instructions.
- **The verdict comes from db-gate's ledger**, read-only (`GATE_STATE_DIR/ledger.db`, the same file ship-gate reads).
  It must be for the exact SQL that was handed off (sha256 via `gatecore.sql_sha256`) and submitted after the
  handoff was requested. If the rehearsal agent rewrites the SQL, the handoff closes as `no_verdict` and the ticket
  is told so; it is never given a verdict for different SQL.
- **One handoff at a time, oldest first.** Claiming is an atomic `queued → rehearsing` update, so the webhook, the
  poller and a second worker can't run the same handoff twice.
- **The right session is resumed.** The worker finds the ticket-resolver session whose `request_migration` result
  names the handoff id, falling back to the session the trigger started. Sessions it creates carry
  `metadata = {ticket, handoff}`.

## Run it

Both gates must share state on one machine: ticket-gate reads db-gate's ledger.

```bash
tf-gate                      # db-gate      :8811 (or GATE_PORT); writes GATE_STATE_DIR (./.gate)
tf-ticket-gate               # ticket-gate  :8821; reads GATE_STATE_DIR too, so start both from the repo root
tf-ticket-trigger            # webhook + poll + handoff worker
# or, if you start tickets by hand in the TrueForge UI:
tf-ticket-trigger handoffs   # just the handoff worker
tf-seed                      # re-register both agents after pulling this change (instructions + tools changed)
```

Skills are git-backed, so push the branch (and point `SKILL_GIT_REF` at it) before `tf-seed`, otherwise TrueForge
still serves the old SKILL.md files.

## Demo

The duplicate-member ticket (`REPRODUCED_NO_FIX`: MBI 1EG4TE5MK72 is two patient rows with split claims) is the one
that hands off. Expect ticket-resolver to propose a member-merge migration; migration-rehearsal will usually return
**REVIEW**, because it deletes a patient row and re-points claims. Watch `/healthz` on the trigger
(`http://127.0.0.1:8822/healthz`, `handoffs` key) or `ticket-gate → ticket_status` for the handoff's state and both
session ids.

Check without TrueForge or Linear: `python tests/ticket_handoff_check.py`.
