"""tf-ticket-trigger: starts a ticket-resolver session in TrueForge when a bug ticket appears in Linear.

  tf-ticket-trigger                          serve: webhook endpoint + poll backstop (TRIGGER_MODE=both)
  tf-ticket-trigger register-webhook <url>   create/update the Linear webhook, e.g. https://<tunnel>/linear/webhook

A ticket qualifies when it is in team LINEAR_TEAM_KEY, has label LINEAR_TRIGGER_LABEL and not TICKET_SKIP_LABEL,
either on creation or when the label is added later. Each ticket starts at most one session (ledger dedupe).
The session runs until it finishes or pauses on reply_to_customer; a person approves in the TrueForge UI.

Config (env or ./.env): LINEAR_API_KEY, LINEAR_WEBHOOK_SECRET, TRIGGER_MODE (both|webhook|poll),
TRIGGER_POLL_SECONDS (60), TRIGGER_MAX_CONCURRENT (2), TICKET_TRIGGER_HOST, TICKET_TRIGGER_PORT (8822).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import threading
import time

from trueforge_hackathon.env import load_env_file

load_env_file()

from trueforge_hackathon.adapter.client import create_trueforge_client  # noqa: E402
from trueforge_hackathon.adapter.run_session import create_agent_session, run_agent_message  # noqa: E402
from trueforge_hackathon.agents.ticket_resolver import gate_server as gate  # noqa: E402
from trueforge_hackathon.agents.ticket_resolver.linear import LinearError  # noqa: E402

AGENT_NAME = "ticket-resolver"
MODE = os.environ.get("TRIGGER_MODE", "both")
POLL_SECONDS = float(os.environ.get("TRIGGER_POLL_SECONDS", "60"))
MAX_CONCURRENT = int(os.environ.get("TRIGGER_MAX_CONCURRENT", "2"))
HOST = os.environ.get("TICKET_TRIGGER_HOST", "127.0.0.1")
PORT = int(os.environ.get("TICKET_TRIGGER_PORT", "8822"))
WEBHOOK_SECRET = os.environ.get("LINEAR_WEBHOOK_SECRET", "")
WEBHOOK_LABEL = "ticket-resolver trigger"
MAX_WEBHOOK_AGE_MS = 60_000
UI_URL = os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790").rstrip("/")

linear = gate.linear
_slots = threading.Semaphore(MAX_CONCURRENT)


def log(msg: str):
    print(f"{dt.datetime.now().strftime('%H:%M:%S')} trigger: {msg}", flush=True)


def now_iso() -> str:
    return gate.now().isoformat()


# ---------------------------------------------------------------- dispatch

def claim(issue: dict) -> bool:
    """Record the ticket once. False if it was already triggered (by the webhook, the poller, or an earlier run)."""
    with gate.ledger() as db:
        cur = db.execute(
            "INSERT OR IGNORE INTO triggered_issues (issue, issue_uuid, status, triggered_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (issue["identifier"], issue["id"], "starting", now_iso(), now_iso()))
        return cur.rowcount == 1


def set_status(ident: str, status: str, session_id: str | None = None, detail: str | None = None):
    with gate.ledger() as db:
        db.execute("UPDATE triggered_issues SET status = ?, session_id = coalesce(?, session_id), detail = ?, "
                   "updated_at = ? WHERE issue = ?", (status, session_id, detail, now_iso(), ident))


def message_for(issue: dict) -> str:
    return (f"Resolve {issue['identifier']}. This session was started automatically by the trigger because the "
            f"ticket was filed with the {gate.TRIGGER_LABEL} label. Run the whole job and finish by calling "
            "reply_to_customer, so a person can approve or deny the reply.")


def run_session(issue: dict):
    ident = issue["identifier"]
    with _slots:
        try:
            client = create_trueforge_client()
            session_id = create_agent_session(client, {"name": AGENT_NAME})
            set_status(ident, "running", session_id)
            log(f"{ident}: session {session_id} started ({UI_URL})")
            result = run_agent_message(client, message=message_for(issue), session_id=session_id)
        except Exception as exc:  # noqa: BLE001  a failed run must not kill the trigger
            set_status(ident, "failed", detail=str(exc)[:500])
            label_error = gate.set_label(issue, gate.AGENT_GROUP, "Failed")
            log(f"{ident}: FAILED: {exc}" + (f" (label not set: {label_error})" if label_error else ""))
            return
    if result.pending_approvals:
        set_status(ident, "awaiting_approval", detail=", ".join(p.tool_name for p in result.pending_approvals))
        log(f"{ident}: paused for approval ({', '.join(p.tool_name for p in result.pending_approvals)}); "
            f"open session {session_id} in TrueForge to Allow or Deny")
    elif result.pending_questions:
        set_status(ident, "awaiting_answer", detail=result.pending_questions[0].question[:500])
        log(f"{ident}: the agent asked a question; answer it in session {session_id}")
    else:
        set_status(ident, "finished", detail=result.status)
        log(f"{ident}: session finished ({result.status}) without asking to reply")


def dispatch(issue_ref: str, source: str) -> str:
    """Fetch the ticket fresh (never trust the event), check scope, dedupe, and start a session in the background."""
    try:
        issue = linear.issue(issue_ref)
    except LinearError as e:
        log(f"{source}: can't read {issue_ref}: {e}")
        return "unreadable"
    problem = gate.scope_problem(issue)
    if problem:
        return f"skipped: {problem}"
    if not claim(issue):
        return f"skipped: {issue['identifier']} was already triggered"
    label_error = gate.set_label(issue, gate.AGENT_GROUP, "Working")
    log(f"{source}: {issue['identifier']} '{issue['title']}' -> starting"
        + (f" (label not set: {label_error})" if label_error else ""))
    threading.Thread(target=run_session, args=(issue,), name=f"session-{issue['identifier']}", daemon=True).start()
    return f"started {issue['identifier']}"


# ---------------------------------------------------------------- webhook

def verify_webhook(body: bytes, signature: str | None, now_ms: float | None = None) -> str | None:
    """Problem with a webhook delivery, or None if it is authentic and fresh."""
    if not WEBHOOK_SECRET:
        return "LINEAR_WEBHOOK_SECRET is not set"
    expected = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        return "bad signature"
    try:
        sent_ms = float(json.loads(body).get("webhookTimestamp"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return "missing webhookTimestamp"
    if abs((now_ms if now_ms is not None else time.time() * 1000) - sent_ms) > MAX_WEBHOOK_AGE_MS:
        return "stale delivery"
    return None


def trigger_label_id() -> str | None:
    return linear.labels().get((None, gate.TRIGGER_LABEL)) or next(
        (i for (_, n), i in linear.labels().items() if n.lower() == gate.TRIGGER_LABEL.lower()), None)


def wants(payload: dict) -> bool:
    """Cheap pre-filter on the event; dispatch() re-checks everything against the live ticket."""
    if payload.get("type") != "Issue":
        return False
    data = payload.get("data") or {}
    label_id = trigger_label_id()
    has_label = label_id in (data.get("labelIds") or [])
    if payload.get("action") == "create":
        return has_label
    if payload.get("action") == "update":
        before = (payload.get("updatedFrom") or {}).get("labelIds")
        return has_label and before is not None and label_id not in before
    return False


def make_app():
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def webhook(request: Request):
        body = await request.body()
        problem = verify_webhook(body, request.headers.get("linear-signature"))
        if problem:
            log(f"webhook rejected: {problem}")
            return JSONResponse({"error": problem}, status_code=401)
        payload = json.loads(body)
        if not wants(payload):
            return JSONResponse({"ok": True, "ignored": True})
        # Linear expects a fast 200; the ticket fetch and session start happen off the request.
        threading.Thread(target=lambda: log("webhook: " + dispatch(payload["data"]["id"], "webhook")),
                         daemon=True).start()
        return JSONResponse({"ok": True, "accepted": True})

    async def healthz(_: Request):
        with gate.ledger() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT issue, status, session_id, updated_at FROM triggered_issues ORDER BY triggered_at DESC LIMIT 20")]
        return JSONResponse({"ok": True, "mode": MODE, "team": gate.TEAM_KEY, "label": gate.TRIGGER_LABEL,
                             "recent": rows})

    return Starlette(routes=[Route("/linear/webhook", webhook, methods=["POST"]), Route("/healthz", healthz)])


# ---------------------------------------------------------------- poll backstop

def linear_ts(t: dt.datetime) -> str:
    """Linear's own timestamp format, so cursors compare correctly as strings."""
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def cursor_path():
    return gate.STATE_DIR / "trigger_cursor"


def poll_once() -> list[str]:
    """One pass: dispatch every qualifying ticket created after the cursor, then advance it."""
    path = cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():  # first start: only tickets filed from now on, never a backlog
        path.write_text(linear_ts(dt.datetime.now(dt.timezone.utc)))
    since = path.read_text().strip()
    results = []
    for issue in linear.issues_created_since(since, gate.TRIGGER_LABEL):
        results.append(dispatch(issue["id"], "poll"))
        since = max(since, issue["createdAt"])
        path.write_text(since)
    return results


def poll_forever():
    try:
        poll_once()  # sets the cursor on first start
    except LinearError as e:
        log(f"poll: {e}")
    log(f"poll: every {POLL_SECONDS:.0f}s for {gate.TEAM_KEY} tickets labelled {gate.TRIGGER_LABEL} "
        f"created after {cursor_path().read_text().strip()}")
    while True:
        time.sleep(POLL_SECONDS)
        try:
            for result in poll_once():
                if result.startswith("started"):
                    log(f"poll: {result}")
        except LinearError as e:
            log(f"poll: {e}")


# ---------------------------------------------------------------- register-webhook

def register_webhook(url: str):
    if not WEBHOOK_SECRET or len(WEBHOOK_SECRET) < 16:
        sys.exit("Set LINEAR_WEBHOOK_SECRET in .env first (e.g. `openssl rand -hex 24`), then re-run.")
    try:
        hooks = linear.gql("{ webhooks { nodes { id url label enabled team { key } } } }")["webhooks"]["nodes"]
        mine = next((h for h in hooks if h.get("label") == WEBHOOK_LABEL), None)
        if mine:
            linear.gql("mutation($id: String!, $input: WebhookUpdateInput!) { webhookUpdate(id: $id, input: $input) "
                       "{ success } }", {"id": mine["id"], "input": {"url": url, "enabled": True,
                                                                     "secret": WEBHOOK_SECRET}})
            print(f"Updated webhook {mine['id']}: {url}")
        else:
            data = linear.gql(
                "mutation($input: WebhookCreateInput!) { webhookCreate(input: $input) { success webhook { id } } }",
                {"input": {"url": url, "teamId": linear.team_id(), "resourceTypes": ["Issue"],
                           "label": WEBHOOK_LABEL, "enabled": True, "secret": WEBHOOK_SECRET}})
            print(f"Created webhook {data['webhookCreate']['webhook']['id']} for team {linear.team_key}: {url}")
    except LinearError as e:
        sys.exit(f"register-webhook: {e}\n(Webhooks need a Linear admin API key; you can also add it by hand in "
                 f"Settings → API → Webhooks with the same URL, team {linear.team_key}, resource Issues, "
                 "and LINEAR_WEBHOOK_SECRET as the signing secret.)")


# ---------------------------------------------------------------- main

def serve():
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per Linear poll otherwise
    if not linear.api_key:
        sys.exit("tf-ticket-trigger: LINEAR_API_KEY is not set")
    if MODE not in ("both", "webhook", "poll"):
        sys.exit("TRIGGER_MODE must be both, webhook or poll")
    if MODE in ("both", "webhook") and not WEBHOOK_SECRET:
        sys.exit("tf-ticket-trigger: LINEAR_WEBHOOK_SECRET is not set; set it or use TRIGGER_MODE=poll")
    if MODE in ("both", "poll"):
        threading.Thread(target=poll_forever, name="poll", daemon=True).start()
    if MODE == "poll":
        while True:
            time.sleep(3600)
    import uvicorn
    log(f"webhook endpoint at http://{HOST}:{PORT}/linear/webhook (expose it with a tunnel); "
        f"status at http://{HOST}:{PORT}/healthz")
    uvicorn.run(make_app(), host=HOST, port=PORT, log_level="warning")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Start ticket-resolver sessions for new Linear bug tickets.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="webhook endpoint + poll backstop (default)")
    reg = sub.add_parser("register-webhook", help="create or update the Linear webhook")
    reg.add_argument("url", help="public URL ending in /linear/webhook")
    args = parser.parse_args(argv)
    if args.command == "register-webhook":
        register_webhook(args.url)
    else:
        serve()


if __name__ == "__main__":
    main()
