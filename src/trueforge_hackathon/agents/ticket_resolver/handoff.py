"""Serial handoff from ticket-resolver to migration-rehearsal: two separate agents, one after the other.

  1. ticket-resolver calls ticket-gate request_migration, either for a data correction it wrote after reproducing a
     data problem (REPRODUCED_NO_FIX), or for SQL a ticket asks to have rehearsed. The handoff is queued and the
     ticket session ends its turn.
  2. This worker claims the handoff, waits for that ticket turn to finish, and starts a migration-rehearsal
     session with the SQL. That agent snapshots production, rehearses, and submits to db-gate.
  3. When the rehearsal session's turn ends, ticket-gate reads db-gate's verdict for the exact SQL (by sha256) from
     db-gate's ledger. No verdict means `no_verdict`; an agent's word never counts.
  4. The worker resumes the same ticket session with the verdict. Only then does ticket-gate accept propose_reply,
     and reply_to_customer still pauses for a human.

One handoff at a time, oldest first. Runs inside `tf-ticket-trigger` (serve), or alone with
`tf-ticket-trigger handoffs` when tickets are started by hand in the TrueForge UI.
Config: HANDOFF_POLL_SECONDS (5), HANDOFF_IDLE_TIMEOUT_SECONDS (900).
"""
from __future__ import annotations

import datetime as dt
import os
import time
from typing import Any, Callable

from trueforge_hackathon.adapter.client import create_trueforge_client
from trueforge_hackathon.adapter.run_session import create_agent_session, run_agent_message
from trueforge_hackathon.agents.ticket_resolver import gate_server as gate

TICKET_AGENT = "ticket-resolver"
REHEARSAL_AGENT = "migration-rehearsal"
POLL_SECONDS = float(os.environ.get("HANDOFF_POLL_SECONDS", "5"))
IDLE_TIMEOUT = float(os.environ.get("HANDOFF_IDLE_TIMEOUT_SECONDS", "900"))
UI_URL = os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790").rstrip("/")


def log(msg: str):
    print(f"{dt.datetime.now().strftime('%H:%M:%S')} handoff: {msg}", flush=True)


# ---------------------------------------------------------------- queue

def claim_next() -> dict | None:
    """Atomically move the oldest queued handoff to `rehearsing`. None if the queue is empty."""
    with gate.ledger() as db:
        row = db.execute("SELECT handoff_id FROM handoffs WHERE status = 'queued' "
                         "ORDER BY requested_at, rowid LIMIT 1").fetchone()
        if row is None:
            return None
        cur = db.execute("UPDATE handoffs SET status = 'rehearsing', updated_at = ? "
                         "WHERE handoff_id = ? AND status = 'queued'", (gate.now().isoformat(), row["handoff_id"]))
        if cur.rowcount != 1:
            return None
    return gate.get_handoff(row["handoff_id"])


def set_trigger_status(issue: str, status: str, detail: str | None = None):
    """Keep the trigger's view of the ticket current, if the trigger started it."""
    with gate.ledger() as db:
        db.execute("UPDATE triggered_issues SET status = ?, detail = ?, updated_at = ? WHERE issue = ?",
                   (status, detail, gate.now().isoformat(), issue))


# ---------------------------------------------------------------- sessions

def _events(client, session_id: str):
    for item in client.sessions.list_events(session_id=session_id):
        yield getattr(item, "event", item)


def find_ticket_session(client, handoff: dict) -> str | None:
    """The ticket-resolver session that called request_migration: the one whose tool result names this handoff.
    Falls back to the session the trigger started for the ticket."""
    agent_id = next((a.id for a in client.agents.list() if a.name == TICKET_AGENT), None)
    if agent_id:
        for session in client.sessions.list(agent_id=agent_id, limit=25):
            for event in _events(client, session.id):
                if getattr(event, "type", None) == "tool.response" and handoff["handoff_id"] in str(
                        getattr(event, "content", "")):
                    return session.id
    with gate.ledger() as db:
        row = db.execute("SELECT session_id FROM triggered_issues WHERE issue = ?", (handoff["issue"],)).fetchone()
    return row["session_id"] if row and row["session_id"] else None


def wait_until_idle(client, session_id: str, timeout: float = IDLE_TIMEOUT,
                    sleep: Callable[[float], None] = time.sleep) -> bool:
    """True once the session's newest event is turn.done (its turn finished, so it can take a new message)."""
    deadline = time.monotonic() + timeout
    while True:
        newest = next(iter(_events(client, session_id)), None)  # events are listed newest first
        if newest is not None and getattr(newest, "type", None) == "turn.done":
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(3)


# ---------------------------------------------------------------- messages

def is_ticket_request(h: dict) -> bool:
    """True when the ticket itself asked for this SQL to be rehearsed (not a correction the agent wrote)."""
    with gate.ledger() as db:
        row = db.execute("SELECT outcome FROM attempts WHERE attempt_id = ?", (h["attempt_id"],)).fetchone()
    return bool(row) and row["outcome"] == gate.MIGRATION_REQUEST


def rehearsal_message(h: dict) -> str:
    origin = (f"{h['issue']} asks for this migration to be rehearsed; the ticket-resolver agent handed you the SQL "
              "exactly as the ticket gives it." if is_ticket_request(h) else
              f"The ticket-resolver agent reproduced {h['issue']} and found a data problem that needs a database "
              "change rather than a code fix; this is the correction it wrote.")
    return "\n".join([
        f"Rehearse this migration against production. It is a handoff from the ticket-resolver agent. {origin}",
        "",
        f"Why: {h['reason']}",
        "",
        f"Save it byte-for-byte as /tmp/migrations/{h['migration_name']} (quoted heredoc, no edits) and follow the "
        "migration-rehearsal skill through step 7: snapshot, rehearse, investigate, submit_rehearsal, report. "
        "Rehearse this exact SQL first, even if you would write it differently: the ticket waits for db-gate's "
        "verdict on this SQL. If it is BLOCK or REVIEW, propose a safer version as a new file in your report, but "
        "don't rehearse it in this session. Do not call apply_migration: applying is a separate, explicit request.",
        "",
        "````sql",
        h["migration_sql"].strip("\n"),
        "````",
    ])


def resume_message(h: dict, report: str | None = None) -> str:
    head = (f"Migration handoff {h['handoff_id']} for {h['issue']} is finished. The migration-rehearsal agent "
            f"rehearsed {h['migration_name']} in its own session ({UI_URL}, session {h['rehearsal_session_id']}).")
    if h["status"] == "rehearsed" and is_ticket_request(h):
        body = [
            f"db-gate verdict (authoritative, quote it, don't reword it): {h['verdict']}",
            f"db-gate summary: {h['summary']}",
            "",
            f"Continue with step 1b of the ticket-resolver skill: draft the reply to the requester and call "
            f"propose_reply with attempt_id {h['attempt_id']}. Explain what the rehearsal found in plain words, "
            "using migration-rehearsal's report below for evidence (counts, example values), and what the requester "
            "should do next. For BLOCK or REVIEW, say what would make it safe. The gate adds a status header with "
            "the verdict and appends db-gate's summary verbatim, so don't write either. Nothing was applied: never "
            "say it was. Then reply_to_customer.",
        ]
    elif h["status"] == "rehearsed":
        body = [
            f"db-gate verdict (authoritative, quote it, don't reword it): {h['verdict']}",
            f"db-gate summary: {h['summary']}",
            "",
            "Continue the ticket-resolver skill from step 7. The customer reply must match that verdict: SAFE means "
            "a correction is prepared and checked against a copy of the data and is waiting for sign-off; REVIEW "
            "means it is prepared but needs a person to accept what it changes; BLOCK means the proposed "
            "correction was stopped and needs rework. It has not been applied in any case: never say it has. "
            "Then propose_reply and reply_to_customer as usual.",
        ]
    else:
        body = [
            f"db-gate has no verdict for that exact SQL ({h['status']}: {h.get('detail') or 'no detail'}).",
            "",
            "Continue the ticket-resolver skill (step 1b for a migration request, step 7 otherwise) without "
            f"claiming any result: say the rehearsal did not complete. Use attempt_id {h['attempt_id']} for "
            "propose_reply, then reply_to_customer as usual.",
        ]
    if report:
        body += ["", "migration-rehearsal's report (its own words; the verdict above is the authority):", "",
                 report.strip()[:6000]]
    return "\n".join([head, "", *body])


# ---------------------------------------------------------------- run

def run_one(h: dict, client_factory: Callable[[], Any] = create_trueforge_client) -> dict:
    """Rehearse one claimed handoff, then resume its ticket session. Returns the final handoff row."""
    hid, issue = h["handoff_id"], h["issue"]
    client = client_factory()
    report = None

    ticket_session = find_ticket_session(client, h)
    gate.set_handoff(hid, ticket_session_id=ticket_session)
    if ticket_session and not wait_until_idle(client, ticket_session):
        log(f"{issue}: ticket session {ticket_session} still busy after {IDLE_TIMEOUT:.0f}s; rehearsing anyway")
    set_trigger_status(issue, "rehearsing_migration", hid)

    try:
        rehearsal_session = create_agent_session(client, {"name": REHEARSAL_AGENT},
                                                 metadata={"ticket": issue, "handoff": hid})
        gate.set_handoff(hid, rehearsal_session_id=rehearsal_session)
        log(f"{issue}: {h['migration_name']} -> {REHEARSAL_AGENT} session {rehearsal_session}")
        result = run_agent_message(client, message=rehearsal_message(h), session_id=rehearsal_session)
        report = result.output
    except Exception as exc:  # noqa: BLE001  a failed rehearsal must not kill the worker
        gate.set_handoff(hid, status="failed", detail=f"rehearsal session failed: {exc}"[:500])
        log(f"{issue}: rehearsal FAILED: {exc}")
    else:
        h = gate.refresh_handoff(hid)
        if h["status"] == "rehearsing":
            gate.set_handoff(hid, status="no_verdict",
                             detail=f"rehearsal session ended ({result.status}) with no db-gate verdict for this SQL")
        h = gate.get_handoff(hid)
        log(f"{issue}: {h['status']}" + (f" {h['verdict']}" if h.get("verdict") else ""))

    h = gate.get_handoff(hid)
    if not ticket_session:
        set_trigger_status(issue, "migration_" + h["status"], "no ticket session to resume; continue it by hand")
        log(f"{issue}: no ticket-resolver session found to resume; tell it the verdict by hand")
        return h
    try:
        result = run_agent_message(client, message=resume_message(h, report), session_id=ticket_session)
    except Exception as exc:  # noqa: BLE001
        set_trigger_status(issue, "failed", f"resume failed: {exc}"[:500])
        log(f"{issue}: resuming ticket session FAILED: {exc}")
        return h
    if result.pending_approvals:
        set_trigger_status(issue, "awaiting_approval", ", ".join(p.tool_name for p in result.pending_approvals))
        log(f"{issue}: ticket session {ticket_session} paused for approval; Allow or Deny in TrueForge")
    else:
        set_trigger_status(issue, "finished", result.status)
        log(f"{issue}: ticket session {ticket_session} finished ({result.status})")
    return h


def work_forever(client_factory: Callable[[], Any] = create_trueforge_client):
    log(f"worker: rehearsing queued migration handoffs one at a time (every {POLL_SECONDS:.0f}s)")
    while True:
        h = None
        try:
            h = claim_next()
            if h is None:
                time.sleep(POLL_SECONDS)
                continue
            run_one(h, client_factory)
        except Exception as exc:  # noqa: BLE001
            log(f"worker error: {exc}")
            if h is not None and (gate.get_handoff(h["handoff_id"]) or {}).get("status") == "rehearsing":
                gate.set_handoff(h["handoff_id"], status="failed", detail=f"worker error: {exc}"[:500])
            time.sleep(POLL_SECONDS)
