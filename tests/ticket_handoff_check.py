#!/usr/bin/env python3
"""Checks the serial ticket-resolver -> migration-rehearsal handoff without TrueForge or live Linear.

ticket-gate's request_migration and propose_reply run for real against a fake Linear. db-gate's ledger is the real
one (its own ledger() creates it); the "rehearsal agent" is a stub that writes to it the way submit_rehearsal would.
TrueForge sessions are stubbed, and every call is logged so the test can check the order:
ticket turn finished -> rehearsal session -> db-gate verdict -> the same ticket session resumed -> reply allowed.

Run from the repo root: `python tests/ticket_handoff_check.py`.
"""
from __future__ import annotations

import os
import secrets
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
WORK = Path(tempfile.mkdtemp(prefix="th-check-"))
os.environ.update({"TICKET_GATE_STATE_DIR": str(WORK / "ticket-gate"), "GATE_STATE_DIR": str(WORK / "db-gate"),
                   "LINEAR_API_KEY": "lin_api_" + secrets.token_hex(16), "LINEAR_TEAM_KEY": "ZYN",
                   "LINEAR_TRIGGER_LABEL": "Bug", "HANDOFF_POLL_SECONDS": "0"})
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "skills" / "migration-rehearsal" / "scripts"))

import gatecore  # noqa: E402
from linear_fake import FakeLinear  # noqa: E402
from trueforge_hackathon.adapter.run_session import PendingApproval, TurnResult  # noqa: E402
from trueforge_hackathon.agents.migration_rehearsal import gate_server as dbg  # noqa: E402
from trueforge_hackathon.agents.ticket_resolver import gate_server as g  # noqa: E402
from trueforge_hackathon.agents.ticket_resolver import handoff as ho  # noqa: E402
from trueforge_hackathon.agents.ticket_resolver import policy  # noqa: E402

failures = 0


def check(name: str, ok: bool, info=None):
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n      {info}" if info is not None and not ok else ""))


fake = FakeLinear()
g.linear = fake
fake.add("ZYN-2", "Beneficiary 1EG4TE5MK72 listed twice", "MBI 1EG4TE5MK72 is on two PCP lists.")
fake.add("ZYN-3", "Another duplicate", "MBI 1EG4TE5MK72 again, different screen.")
fake.add("ZYN-9", "Fixed bug", "x" * 40)

SQL = """-- merge the duplicate member rows
UPDATE claims SET patient_id = 1 WHERE patient_id = 2;
DELETE FROM patients WHERE id = 2;
"""
SQL3 = SQL.replace("= 2", "= 7").replace("= 1", "= 6")
REASON = "Two patient rows share one MBI; claims must be re-pointed and one row removed, which code can't do."
BODY = ("We confirmed that beneficiary 1EG4TE5MK72 appears twice, once per PCP list. A data correction has been "
        "prepared and checked against a copy of the records; it is waiting for sign-off.")


def attempt(ident: str, outcome: str) -> str:
    """An attempt as submit_attempt would record it (its own checks are covered by ticket_gate_check.py)."""
    aid = "att_" + secrets.token_hex(4)
    with g.ledger() as db:
        db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (aid, ident, f"uuid-{ident}", "src_x", outcome, "summary", "[]", "d", None,
                    g.ticket_fingerprint(fake.issue(ident)), g.now().isoformat()))
    return aid


def db_gate_records(sql: str, verdict: str, summary: str, submitted_at: str | None = None):
    """What db-gate's submit_rehearsal writes after a rehearsal of `sql`."""
    with dbg.ledger() as db:
        db.execute("INSERT INTO rehearsals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("rh_" + secrets.token_hex(4), "snap_x", "appdb@localhost", "m.sql", gatecore.sql_sha256(sql),
                    verdict, summary, "[]", "fp", "fp2", "d", None, submitted_at or g.now().isoformat()))


# ---------------------------------------------------------------- stub TrueForge

class FakeTrueForge:
    def __init__(self):
        self.calls: list[tuple] = []
        self.sessions_by_agent: dict[str, list[str]] = {"ticket-resolver": []}
        self.events: dict[str, list] = {}
        self._busy: dict[str, int] = {}
        self.agents = NS(list=lambda: [NS(id="ag-ticket", name="ticket-resolver"),
                                       NS(id="ag-mr", name="migration-rehearsal")])
        self.sessions = NS(list=self._list, list_events=self._list_events)

    def ticket_session(self, sid: str, tool_result: str, busy_checks: int = 0):
        self.sessions_by_agent["ticket-resolver"].insert(0, sid)
        # newest first; a busy session shows a non-turn.done head for `busy_checks` polls
        self.events[sid] = [NS(type="turn.done"), NS(type="tool.response", content=tool_result)]
        self._busy[sid] = busy_checks

    def _list(self, agent_id=None, limit=25):
        return [NS(id=s) for s in self.sessions_by_agent["ticket-resolver" if agent_id == "ag-ticket" else "x"]]

    def _list_events(self, session_id):
        if self._busy.get(session_id):
            self._busy[session_id] -= 1
            self.calls.append(("busy", session_id))
            return iter([NS(type="model.message.delta")] + self.events[session_id])
        return iter(self.events.get(session_id, []))


client = FakeTrueForge()
rehearsal_behaviour = {}


def fake_create(c, agent, metadata=None):
    sid = f"sess-{agent['name']}-{len(c.calls)}"
    c.calls.append(("create", agent["name"], sid, metadata))
    return sid


def fake_run(c, *, message, session_id, **_):
    c.calls.append(("run", session_id, message))
    if session_id.startswith("sess-migration-rehearsal"):
        how = rehearsal_behaviour["how"]
        if how == "submits":
            db_gate_records(SQL, "REVIEW", "REVIEW: patients: 1 row deleted; claims: 3 rows re-pointed")
        elif how == "edits_sql":  # the agent "improved" the SQL: db-gate's verdict is for other SQL
            db_gate_records(SQL3 + "\nVACUUM patients;", "SAFE", "SAFE: other sql")
        elif how == "crashes":
            raise RuntimeError("model provider timed out")
        return TurnResult(session_id=session_id, status="done")
    return TurnResult(session_id=session_id, status="done", pending_approvals=[
        PendingApproval(thread_id="main", tool_call_id="c1", tool_name="reply_to_customer", arguments="{}")])


ho.create_agent_session = fake_create
ho.run_agent_message = fake_run
factory = lambda: client  # noqa: E731
no_sleep = lambda _s: None  # noqa: E731

# ================================================================ request_migration

fixed = attempt("ZYN-9", "FIXED")
r = g._request_migration(fixed, "0102_x.sql", SQL, REASON)
check("request_migration refuses a FIXED attempt", r.get("refused") and "REPRODUCED_NO_FIX" in r["reason"], r)
check("request_migration refuses an unknown attempt", g._request_migration("att_00000000", "a.sql", SQL, REASON).get("refused"))

a2 = attempt("ZYN-2", "REPRODUCED_NO_FIX")
for name, args, why in [
    ("a path as the file name", ("../../etc/x.sql", SQL, REASON), "plain file name"),
    ("a name without .sql", ("merge", SQL, REASON), "plain file name"),
    ("empty SQL", ("0102_merge.sql", "  \n", REASON), "empty"),
    ("a one-word reason", ("0102_merge.sql", SQL, "dupes"), "reason"),
]:
    r = g._request_migration(a2, *args)
    check(f"request_migration refuses {name}", r.get("refused") and why in r["reason"], r)

check("propose_reply works before any handoff", g._propose_reply(a2, BODY).get("ok"))
r = g._request_migration(a2, "0102_merge_duplicate_members.sql", SQL, REASON)
check("request_migration queues a REPRODUCED_NO_FIX correction", r.get("ok") and r["status"] == "queued", r)
hid = r["handoff_id"]
check("the handoff's sha is db-gate's sha for the same SQL", r["migration_sha256"] == gatecore.sql_sha256(SQL))
with g.ledger() as db:
    earlier = db.execute("SELECT status FROM drafts WHERE attempt_id = ?", (a2,)).fetchone()["status"]
check("an earlier proposed draft is superseded by the handoff", earlier == "superseded", earlier)
r = g._request_migration(a2, "0103_other.sql", SQL, REASON)
check("a second handoff for the same ticket is refused while one is open", r.get("refused") and hid in r["reason"], r)
r = g._propose_reply(a2, BODY)
check("propose_reply is refused while the handoff is queued", r.get("refused") and "queued" in r["reason"], r)

# an old rehearsal of the same SQL, from before the handoff, doesn't count
db_gate_records(SQL, "SAFE", "SAFE: stale", submitted_at="2020-01-01T00:00:00+00:00")
check("a rehearsal older than the handoff doesn't close it", g.refresh_handoff(hid)["status"] == "queued")

# ================================================================ worker: happy path, strict order

client.ticket_session("sess-ticket-ZYN-2", f'{{"ok": true, "handoff_id": "{hid}"}}', busy_checks=2)
client.ticket_session("sess-ticket-other", '{"ok": true}')  # newer, unrelated session must not be picked
rehearsal_behaviour["how"] = "submits"
h = ho.claim_next()
check("worker claims the queued handoff", h and h["handoff_id"] == hid and h["status"] == "rehearsing", h)
check("claiming again finds nothing (no double run)", ho.claim_next() is None)
orig_wait = ho.wait_until_idle
ho.wait_until_idle = lambda c, sid: orig_wait(c, sid, timeout=60, sleep=no_sleep)
final = ho.run_one(h, factory)

kinds = [(c[0], c[1]) for c in client.calls]
busy_idx = max(i for i, c in enumerate(client.calls) if c[0] == "busy")
create_idx = next(i for i, c in enumerate(client.calls) if c[0] == "create")
run_mr = next(i for i, c in enumerate(client.calls) if c[0] == "run" and "migration-rehearsal" in c[1])
run_tk = next(i for i, c in enumerate(client.calls) if c[0] == "run" and c[1] == "sess-ticket-ZYN-2")
check("rehearsal starts only after the ticket turn is idle", busy_idx < create_idx, kinds)
check("the ticket session is resumed only after the rehearsal turn", run_mr < run_tk, kinds)
check("the rehearsal is a separate migration-rehearsal session tagged with the ticket",
      client.calls[create_idx][1] == "migration-rehearsal"
      and client.calls[create_idx][3] == {"ticket": "ZYN-2", "handoff": hid}, client.calls[create_idx])
check("the rehearsal message carries the exact SQL", SQL.strip() in client.calls[run_mr][2])
check("the resumed session is the one that requested the handoff, not the newest",
      final["ticket_session_id"] == "sess-ticket-ZYN-2", final)
check("verdict comes from db-gate's ledger", final["status"] == "rehearsed" and final["verdict"] == "REVIEW", final)
resume = client.calls[run_tk][2]
check("the resume message quotes db-gate's verdict and summary",
      "REVIEW" in resume and "3 rows re-pointed" in resume, resume)

r = g._propose_reply(a2, BODY)
check("propose_reply is accepted after the verdict", r.get("ok"), r)
check("propose_reply reports the migration verdict", (r.get("migration") or {}).get("verdict") == "REVIEW", r)
status = g._ticket_status("ZYN-2")
check("ticket_status lists the handoff with both session ids",
      status["handoffs"][0]["rehearsal_session_id"] and status["handoffs"][0]["ticket_session_id"], status["handoffs"])

# ================================================================ worker: agent edits the SQL / crashes

a3 = attempt("ZYN-3", "REPRODUCED_NO_FIX")
hid3 = g._request_migration(a3, "0104_merge.sql", SQL3, REASON)["handoff_id"]
client.calls.clear()
client.ticket_session("sess-ticket-ZYN-3", f'{{"handoff_id": "{hid3}"}}')
rehearsal_behaviour["how"] = "edits_sql"
final = ho.run_one(ho.claim_next(), factory)
check("a verdict for different SQL is not accepted (no_verdict)", final["status"] == "no_verdict" and not final["verdict"], final)
resume = [c for c in client.calls if c[0] == "run" and c[1] == "sess-ticket-ZYN-3"][0][2]
check("the ticket is still resumed, told there is no verdict", "no verdict" in resume and "SAFE" not in resume, resume)
check("propose_reply is allowed once the handoff is closed", g._propose_reply(a3, BODY).get("ok"))

hid3b = g._request_migration(a3, "0104_merge.sql", SQL3, REASON)
check("a new handoff can be requested after no_verdict", hid3b.get("ok"), hid3b)
client.ticket_session("sess-ticket-ZYN-3b", f'{{"handoff_id": "{hid3b['handoff_id']}"}}')
client.calls.clear()
rehearsal_behaviour["how"] = "crashes"
final = ho.run_one(ho.claim_next(), factory)
check("a crashed rehearsal session is marked failed", final["status"] == "failed" and "timed out" in final["detail"], final)
check("the ticket session is still resumed after a crash",
      any(c[0] == "run" and c[1] == "sess-ticket-ZYN-3b" and "no verdict" in c[2] for c in client.calls), client.calls)

# ================================================================ a ticket that asks for its own SQL to be rehearsed

TICKET_SQL = "ALTER TABLE diagnoses\n    ALTER COLUMN icd10_code TYPE varchar(6) USING icd10_code::varchar(6);"
fake.add("ZYN-10", "Rehearse 0008_cap_icd10_length.sql",
         "Rehearse `0008_cap_icd10_length.sql`:\n\n```sql\n-- 0008\n" + TICKET_SQL + "\n```")
fake.add("ZYN-11", "Rehearse something", "Please rehearse our ICD migration, thanks.")
ticket_sql = "-- 0008\n" + TICKET_SQL

r = g._request_migration("", "0008_cap_icd10_length.sql", ticket_sql, REASON)
check("request_migration needs attempt_id or issue_id", r.get("refused") and "exactly one" in r["reason"], r)
r = g._request_migration(a2, "0008.sql", ticket_sql, REASON, issue_id="ZYN-10")
check("request_migration refuses both attempt_id and issue_id", r.get("refused") and "exactly one" in r["reason"], r)
r = g._request_migration("", "x.sql", "DROP TABLE claims;", REASON, issue_id="ZYN-11")
check("a ticket without a SQL block can't be a migration request", r.get("refused") and "no fenced SQL" in r["reason"], r)
r = g._request_migration("", "x.sql", "DROP TABLE claims;", REASON, issue_id="ZYN-10")
check("SQL that isn't the ticket's is refused", r.get("refused") and "not one of the SQL blocks" in r["reason"], r)
r = g._request_migration("", "0008_cap_icd10_length.sql", "\n" + ticket_sql + "\n\n", REASON, issue_id="ZYN-4")
check("an out-of-scope ticket is refused", r.get("refused"), r)
r = g._request_migration("", "0008_cap_icd10_length.sql", "\n" + ticket_sql + "\n\n", REASON, issue_id="zyn-10")
check("the ticket's SQL is queued (surrounding whitespace ignored, like db-gate)", r.get("ok") and r["attempt_id"], r)
hid10, att10 = r["handoff_id"], r["attempt_id"]
r = g._request_migration("", "0008_cap_icd10_length.sql", ticket_sql, REASON, issue_id="ZYN-10")
check("a second request for the same ticket is refused while open", r.get("refused") and hid10 in r["reason"], r)
REQ_BODY = "The rehearsal cut 4,159 diagnosis codes to six characters, so they are no longer billable."
check("propose_reply waits for the verdict on a migration request", g._propose_reply(att10, REQ_BODY).get("refused"))

client.calls.clear()
client.ticket_session("sess-ticket-ZYN-10", f'{{"handoff_id": "{hid10}"}}')
rehearsal_behaviour["how"] = "ticket_sql"
orig_run = ho.run_agent_message


def run_with_report(c, *, message, session_id, **kw):
    if session_id.startswith("sess-migration-rehearsal"):
        c.calls.append(("run", session_id, message))
        db_gate_records(ticket_sql, "REVIEW", "REVIEW: diagnoses: icd10_code rewritten in 4159 rows")
        return TurnResult(session_id=session_id, status="done", output="REVIEW. 4,159 codes truncated, e.g. E11.65 -> E11.6")
    return orig_run(c, message=message, session_id=session_id, **kw)


ho.run_agent_message = run_with_report
final = ho.run_one(ho.claim_next(), factory)
ho.run_agent_message = orig_run
mr_msg = next(c[2] for c in client.calls if c[0] == "run" and "migration-rehearsal" in c[1])
resume = next(c[2] for c in client.calls if c[0] == "run" and c[1] == "sess-ticket-ZYN-10")
check("the rehearsal message says the ticket asked for it", "asks for this migration to be rehearsed" in mr_msg, mr_msg)
check("the ticket request is rehearsed with db-gate's verdict", final["verdict"] == "REVIEW", final)
check("the resume message points at step 1b with the attempt id and carries the rehearsal report",
      "step 1b" in resume and att10 in resume and "E11.65 -> E11.6" in resume, resume)
r = g._propose_reply(att10, REQ_BODY)
check("the reply is headed with the rehearsal verdict", r.get("ok") and r["outcome"] == "MIGRATION_REVIEW"
      and r["will_post"].startswith("**Status: rehearsed, verdict REVIEW.**"), r)
check("the reply carries db-gate's summary verbatim",
      "Rehearsal result: `REVIEW: diagnoses: icd10_code rewritten in 4159 rows`" in r.get("will_post", ""), r)
draft = r["draft_id"]
check("reply_to_customer posts the migration request's reply", g._reply_to_customer(draft, REQ_BODY).get("sent"))
check("the posted comment is exactly will_post", fake.posted[-1]["body"] == r["will_post"])

# ================================================================ queue order and reply policy

a9 = attempt("ZYN-2", "REPRODUCED_NO_FIX")
first = g._request_migration(a9, "0105_a.sql", SQL, REASON)["handoff_id"]
second = g._request_migration(attempt("ZYN-3", "REPRODUCED_NO_FIX"), "0106_b.sql", SQL, REASON)["handoff_id"]
check("handoffs are claimed oldest first, one at a time",
      ho.claim_next()["handoff_id"] == first and ho.claim_next()["handoff_id"] == second)

problems = policy.check_reply(BODY + f" Reference {first}.", "REPRODUCED_NO_FIX", "1EG4TE5MK72")
check("a reply may not leak the handoff id", any("internal ids" in p for p in problems), problems)
problems = policy.check_reply(BODY + " Our db-gate checked it.", "REPRODUCED_NO_FIX", "1EG4TE5MK72")
check("a reply may not name db-gate", any("internal tool names" in p for p in problems), problems)

print()
print("OK" if not failures else f"{failures} FAILED")
sys.exit(1 if failures else 0)
