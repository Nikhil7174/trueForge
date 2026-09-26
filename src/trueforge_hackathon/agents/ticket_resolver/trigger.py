"""tf-ticket-trigger: the single Linear entry point. Starts a platform-guardian session when a routed ticket appears.

Routing is by label (LINEAR_ROUTES, default Bug=ticket-resolver, cloudcost=cost-janitor, iam-review=access-reviewer,
migration=migration-rehearsal, release=release-captain). Status goes back on the ticket through the Agent label
group (Working, Awaiting approval, Replied, Declined, Failed). Bug tickets keep the ticket-gate flow unchanged.

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
from trueforge_hackathon.adapter.run_session import create_agent_session, pending_actions, run_agent_message  # noqa: E402
from trueforge_hackathon.agents.umbrella.plugin import umbrella_agent_name  # noqa: E402
from trueforge_hackathon.agents.ticket_resolver import gate_server as gate  # noqa: E402
from trueforge_hackathon.agents.ticket_resolver.linear import LinearError  # noqa: E402

# One platform agent; the label picks the job inside it.
AGENT_NAME = os.environ.get("TRIGGER_AGENT_NAME") or umbrella_agent_name()
BUG_JOB = "ticket-resolver"
ROUTES = {
    k.strip().lower(): v.strip()
    for k, v in (pair.split("=", 1) for pair in os.environ.get(
        "LINEAR_ROUTES",
        f"{os.environ.get('LINEAR_TRIGGER_LABEL', 'Bug')}={BUG_JOB},cloudcost=cost-janitor,iam-review=access-reviewer,"
        "migration=migration-rehearsal,release=release-captain",
    ).split(",") if "=" in pair)
}
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


def route_for(issue: dict) -> tuple[str, str] | None:
    """(label, job) for the first routed label on the ticket."""
    for name in (n["name"] for n in issue["labels"]["nodes"]):
        if name.lower() in ROUTES:
            return name, ROUTES[name.lower()]
    return None


def route_problem(issue: dict) -> str | None:
    names = [n["name"].lower() for n in issue["labels"]["nodes"]]
    if issue["team"]["key"] != gate.TEAM_KEY:
        return f"{issue['identifier']} is in team {issue['team']['key']}, not {gate.TEAM_KEY}"
    if gate.SKIP_LABEL.lower() in names:
        return f"{issue['identifier']} is labelled {gate.SKIP_LABEL}"
    route = route_for(issue)
    if route is None:
        return f"{issue['identifier']} has no routed label ({', '.join(ROUTES)})"
    if route[1] == BUG_JOB:
        return gate.scope_problem(issue)  # the ticket-gate's own checks, unchanged
    return None


def message_for(issue: dict) -> str:
    label, job = route_for(issue) or (gate.TRIGGER_LABEL, BUG_JOB)
    if job == BUG_JOB:
        return (f"Resolve {issue['identifier']}. This session was started automatically by the trigger because the "
                f"ticket was filed with the {gate.TRIGGER_LABEL} label. Do the {BUG_JOB} job: run the whole job "
                "and finish by calling reply_to_customer, so a person can approve or deny the reply.")
    return (f"Linear ticket {issue['identifier']} was labelled '{label}', so do the {job} job for it.\n\n"
            f"Title: {issue['title']}\n\n{issue.get('description') or ''}\n\n"
            "Questions and approvals are answered by a person in this TrueForge session. "
            "End with a short summary of the outcome; it is posted back to the ticket.")


def _jobs_path():
    return gate.STATE_DIR / "trigger_jobs.json"


def _jobs() -> dict:
    path = _jobs_path()
    return json.loads(path.read_text()) if path.exists() else {}


def _remember(ident: str, **fields) -> None:
    jobs = _jobs()
    jobs.setdefault(ident, {}).update(fields)
    _jobs_path().parent.mkdir(parents=True, exist_ok=True)
    _jobs_path().write_text(json.dumps(jobs, indent=2))


def _last_output(client, session_id: str) -> str:
    for turn in sorted(client.sessions.list_turns(session_id=session_id, limit=25),
                       key=lambda t: str(getattr(t, "created_at", "")), reverse=True):
        content = getattr(getattr(getattr(turn, "state", None), "output", None), "content", None)
        if content:
            return content if isinstance(content, str) else str(content)
    return "(no final text)"


def report_back(issue: dict, session_id: str, client) -> str:
    """Non-Bug jobs: mirror the session state onto the ticket (comment + Agent label). Returns the new status."""
    approvals, questions = pending_actions(client, session_id)
    if approvals or questions:
        lines = [f"**{AGENT_NAME} is waiting for you** in TrueForge session `{session_id}` ({UI_URL}):"]
        for q in questions:
            lines.append(f"- Question: {q.question}")
            lines += [f"  - {o}" for o in q.options]
        lines += [f"- Approval: `{a.tool_name}` {a.arguments[:400]}" for a in approvals]
        linear.create_comment(issue["id"], "\n".join(lines))
        gate.set_label(issue, gate.AGENT_GROUP, "Awaiting approval")
        return "awaiting_approval" if approvals else "awaiting_answer"
    output = _last_output(client, session_id)
    declined = any(w in output.lower() for w in ("denied", "declined"))
    linear.create_comment(issue["id"], f"**{AGENT_NAME} finished.**\n\n{output[:60000]}\n\nSession `{session_id}`")
    gate.set_label(issue, gate.AGENT_GROUP, "Declined" if declined else "Replied")
    return "declined" if declined else "finished"


def followup_once() -> None:
    """Paused non-Bug sessions: once the person has answered in TrueForge, post the outcome to the ticket."""
    client = None
    for ident, job in _jobs().items():
        if job.get("job") == BUG_JOB or job.get("status") not in ("awaiting_approval", "awaiting_answer"):
            continue
        client = client or create_trueforge_client()
        approvals, questions = pending_actions(client, job["session_id"])
        if approvals or questions:
            continue
        status = report_back(linear.issue(ident), job["session_id"], client)
        _remember(ident, status=status)
        set_status(ident, status)
        log(f"{ident}: {status} (after the person answered in TrueForge)")


def run_session(issue: dict):
    ident = issue["identifier"]
    job = (route_for(issue) or ("", BUG_JOB))[1]
    with _slots:
        try:
            client = create_trueforge_client()
            session_id = create_agent_session(client, {"name": AGENT_NAME})
            set_status(ident, "running", session_id)
            _remember(ident, job=job, session_id=session_id, status="running")
            log(f"{ident}: {job} session {session_id} started on {AGENT_NAME} ({UI_URL})")
            if job != BUG_JOB:
                linear.create_comment(issue["id"], f"Picked up by **{AGENT_NAME}** ({job}). "
                                                   f"TrueForge session `{session_id}` at {UI_URL}")
            result = run_agent_message(client, message=message_for(issue), session_id=session_id)
        except Exception as exc:  # noqa: BLE001  a failed run must not kill the trigger
            set_status(ident, "failed", detail=str(exc)[:500])
            label_error = gate.set_label(issue, gate.AGENT_GROUP, "Failed")
            log(f"{ident}: FAILED: {exc}" + (f" (label not set: {label_error})" if label_error else ""))
            return
    if job != BUG_JOB:
        try:
            status = report_back(issue, session_id, client)
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            log(f"{ident}: could not report back: {exc}")
        _remember(ident, status=status)
        set_status(ident, status)
        log(f"{ident}: {job} {status}")
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
    problem = route_problem(issue)
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


def routed_label_ids() -> set[str]:
    return {i for (_, n), i in linear.labels().items() if n.lower() in ROUTES}


def wants(payload: dict) -> bool:
    """Cheap pre-filter on the event; dispatch() re-checks everything against the live ticket."""
    if payload.get("type") != "Issue":
        return False
    data = payload.get("data") or {}
    routed = routed_label_ids()
    now_ids = set(data.get("labelIds") or [])
    if payload.get("action") == "create":
        return bool(now_ids & routed)
    if payload.get("action") == "update":
        before = (payload.get("updatedFrom") or {}).get("labelIds")
        return before is not None and bool((now_ids - set(before)) & routed)
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
        return JSONResponse({"ok": True, "mode": MODE, "team": gate.TEAM_KEY, "routes": ROUTES,
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
    results, newest = [], since
    seen: set[str] = set()
    for label in ROUTES:
        for issue in linear.issues_created_since(since, label):
            if issue["id"] in seen:
                continue
            seen.add(issue["id"])
            results.append(dispatch(issue["id"], "poll"))
            newest = max(newest, issue["createdAt"])
    path.write_text(newest)
    return results


def poll_forever():
    try:
        poll_once()  # sets the cursor on first start
    except LinearError as e:
        log(f"poll: {e}")
    log(f"poll: every {POLL_SECONDS:.0f}s for {gate.TEAM_KEY} tickets routed by {ROUTES} to {AGENT_NAME} "
        f"created after {cursor_path().read_text().strip()}")
    while True:
        time.sleep(POLL_SECONDS)
        try:
            for result in poll_once():
                if result.startswith("started"):
                    log(f"poll: {result}")
            followup_once()
        except Exception as e:  # noqa: BLE001  keep polling
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
    parser = argparse.ArgumentParser(description="Start platform-guardian sessions for routed Linear tickets.")
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
