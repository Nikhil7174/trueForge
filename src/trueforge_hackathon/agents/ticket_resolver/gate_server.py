"""ticket-gate: the only process that writes to Linear on the ticket-resolver's behalf.

MCP tools (streamable HTTP, bearer auth):
  export_source      packs the product code for one in-scope ticket, delivered as text to the sandbox
  submit_attempt     verifies a sandbox repro report, recomputes its outcome, re-applies its patch, labels the ticket
  propose_reply      checks a draft reply against the outcome and the content rules, stores it
  reply_to_customer  posts a proposed draft to the ticket, exactly as proposed (destructive, needs approval)
  record_decline     marks a draft declined after a human denied it
  ticket_status      ledger view for one ticket

Config (env or ./.env): LINEAR_API_KEY, LINEAR_TEAM_KEY (ZYN), LINEAR_TRIGGER_LABEL (Bug), TICKET_GATE_TOKEN,
TICKET_GATE_HOST, TICKET_GATE_PORT (8821), TICKET_REPO_PATH (demo aco-api), TICKET_GATE_STATE_DIR (./.ticket-gate).
Run with `tf-ticket-gate` (or `python -m trueforge_hackathon ticket-gate`).
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from trueforge_hackathon.env import load_env_file

# ticketcore is shared with the sandbox, so it lives in the skill pack. Editable install (`pip install -e`)
# keeps this path valid.
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "skills" / "ticket-resolver" / "scripts"))
import ticketcore as tc  # noqa: E402

load_env_file()

import anyio  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp_types import ToolAnnotations  # noqa: E402

from trueforge_hackathon.agents.ticket_resolver import policy  # noqa: E402
from trueforge_hackathon.agents.ticket_resolver.linear import Linear, LinearError, label_names  # noqa: E402

GATE_TOKEN = os.environ.get("TICKET_GATE_TOKEN", "")
HOST = os.environ.get("TICKET_GATE_HOST", "127.0.0.1")
PORT = int(os.environ.get("TICKET_GATE_PORT", "8821"))
STATE_DIR = Path(os.environ.get("TICKET_GATE_STATE_DIR", Path.cwd() / ".ticket-gate"))
REPO_PATH = Path(os.environ.get("TICKET_REPO_PATH", Path(__file__).resolve().parent / "demo" / "aco-api"))
PROJECT = os.environ.get("TICKET_PROJECT", REPO_PATH.name)
TEST_COMMAND = shlex.split(os.environ.get("TICKET_TEST_COMMAND", "python3 -m unittest discover -s tests"))
SETUP_COMMAND = shlex.split(os.environ.get("TICKET_SETUP_COMMAND", "")) or None
TEAM_KEY = os.environ.get("LINEAR_TEAM_KEY", "ZYN")
TRIGGER_LABEL = os.environ.get("LINEAR_TRIGGER_LABEL", "Bug")
SKIP_LABEL = os.environ.get("TICKET_SKIP_LABEL", "agent-skip")
MAX_ATTEMPT_AGE = dt.timedelta(hours=float(os.environ.get("TICKET_MAX_ATTEMPT_AGE_HOURS", "24")))

AGENT_GROUP, REPRO_GROUP = "Agent", "Repro"
OUTCOME_LABELS = {tc.FIXED: "Fixed", tc.REPRODUCED_NO_FIX: "Reproduced, no fix", tc.NOT_REPRODUCED: "Not reproduced"}

linear = Linear(team_key=TEAM_KEY)
_reply_lock = threading.Lock()


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def refuse(reason: str, **extra) -> dict:
    return {"ok": False, "refused": True, "reason": reason, **extra}


# ---------------------------------------------------------------- ledger

def ledger() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(STATE_DIR / "ledger.db")
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS sources (
        source_id TEXT PRIMARY KEY, issue TEXT, project TEXT, tree_sha256 TEXT, tar_sha256 TEXT,
        commit_sha TEXT, dirty INTEGER, exported_at TEXT);
    CREATE TABLE IF NOT EXISTS attempts (
        attempt_id TEXT PRIMARY KEY, issue TEXT, issue_uuid TEXT, source_id TEXT, outcome TEXT, summary TEXT,
        reasons TEXT, digest TEXT, patch_sha256 TEXT, ticket_fingerprint TEXT, submitted_at TEXT);
    CREATE TABLE IF NOT EXISTS drafts (
        draft_id TEXT PRIMARY KEY, attempt_id TEXT, issue TEXT, body TEXT, final_body TEXT,
        status TEXT, proposed_at TEXT, closed_at TEXT, note TEXT);
    CREATE TABLE IF NOT EXISTS replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id TEXT, attempt_id TEXT, issue TEXT,
        comment_id TEXT, comment_url TEXT, posted_at TEXT);
    CREATE TABLE IF NOT EXISTS triggered_issues (
        issue TEXT PRIMARY KEY, issue_uuid TEXT, session_id TEXT, status TEXT, detail TEXT,
        triggered_at TEXT, updated_at TEXT);
    """)
    return db


# ---------------------------------------------------------------- ticket checks

def scope_problem(issue: dict) -> str | None:
    names = [n.lower() for n in label_names(issue)]
    if issue["team"]["key"] != TEAM_KEY:
        return f"{issue['identifier']} is in team {issue['team']['key']}, not {TEAM_KEY}"
    if TRIGGER_LABEL.lower() not in names:
        return f"{issue['identifier']} has no {TRIGGER_LABEL} label"
    if SKIP_LABEL.lower() in names:
        return f"{issue['identifier']} is labelled {SKIP_LABEL}"
    return None


def gate_comment_ids(issue: str) -> set[str]:
    with ledger() as db:
        return {r["comment_id"] for r in db.execute("SELECT comment_id FROM replies WHERE issue = ?", (issue,))}


def customer_comments(issue: dict) -> list[dict]:
    """Every comment except the replies this gate posted."""
    ours = gate_comment_ids(issue["identifier"])
    return [c for c in issue["comments"]["nodes"] if c["id"] not in ours]


def ticket_fingerprint(issue: dict) -> str:
    """What the customer told us: title, description and their comments. Our own labels and replies don't count."""
    comments = sorted([c["id"], c["body"]] for c in customer_comments(issue))
    return tc.sha256_text(tc.canonical_json([issue["title"], issue.get("description") or "", comments]))


def ticket_text(issue: dict) -> str:
    return "\n".join([issue["title"], issue.get("description") or ""] + [c["body"] for c in customer_comments(issue)])


def fetch_in_scope(issue_id: str) -> tuple[dict | None, str | None]:
    try:
        issue = linear.issue(issue_id)
    except LinearError as e:
        return None, str(e)
    return issue, scope_problem(issue)


def set_label(issue: dict, group: str, name: str) -> str | None:
    """Label failures never undo a recorded decision; they're reported back instead."""
    try:
        linear.set_group_label(issue, group, name)
        return None
    except LinearError as e:
        return str(e)


def attempt_problem(attempt: sqlite3.Row, issue: dict) -> str | None:
    if now() - dt.datetime.fromisoformat(attempt["submitted_at"]) > MAX_ATTEMPT_AGE:
        return f"attempt {attempt['attempt_id']} is older than {MAX_ATTEMPT_AGE}; reproduce again"
    if ticket_fingerprint(issue) != attempt["ticket_fingerprint"]:
        return (f"{issue['identifier']} changed after the attempt was submitted (edited description or a new "
                "comment). Re-read the ticket; if it changes the picture, reproduce again")
    return None


# ---------------------------------------------------------------- export_source

def _repo_commit() -> tuple[str | None, bool]:
    if shutil.which("git") is None:
        return None, False
    head = subprocess.run(["git", "-C", str(REPO_PATH), "log", "-1", "--format=%H", "--", "."],
                          capture_output=True, text=True)
    dirty = subprocess.run(["git", "-C", str(REPO_PATH), "status", "--porcelain", "--", "."],
                           capture_output=True, text=True)
    return (head.stdout.strip() or None), bool(dirty.stdout.strip())


def _export_source(issue_id: str) -> str:
    issue, problem = fetch_in_scope(issue_id)
    if problem:
        return f"REFUSED: {problem}"
    commit, dirty = _repo_commit()
    header = {
        "source_id": "src_" + secrets.token_hex(4),
        "issue_id": issue["identifier"],
        "project": PROJECT,
        "commit": commit,
        "dirty": dirty,
        "test_command": TEST_COMMAND,
        "setup_command": SETUP_COMMAND,
        "exported_at": now().isoformat(),
    }
    blob = tc.pack_source(REPO_PATH, header)
    full, tar_bytes = tc.read_source_blob(blob)
    (STATE_DIR / "sources").mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "sources" / f"{header['source_id']}.tar.gz").write_bytes(tar_bytes)
    with ledger() as db:
        db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)",
                   (header["source_id"], issue["identifier"], PROJECT, full["tree_sha256"], full["tar_sha256"],
                    commit, int(dirty), header["exported_at"]))
    return blob


# ---------------------------------------------------------------- submit_attempt

def _patched_tree(source_id: str, diff: str) -> tuple[str | None, str | None]:
    """Re-apply the patch to the exact source the gate exported. Returns (tree digest, error)."""
    if shutil.which("git") is None:
        return None, "git is not installed on the gate host; can't verify the patch"
    with tempfile.TemporaryDirectory() as tmp:
        code_dir = Path(tmp) / "code"
        tc.extract_tar((STATE_DIR / "sources" / f"{source_id}.tar.gz").read_bytes(), code_dir)
        patch_file = Path(tmp) / "patch.diff"
        patch_file.write_text(diff)
        p = subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch_file)], cwd=code_dir,
                           capture_output=True, text=True)
        if p.returncode != 0:
            return None, f"patch does not apply to source {source_id}: {p.stderr.strip()[:300]}"
        return tc.tree_digest(code_dir), None


def _submit_attempt(issue_id: str, report_json: str) -> dict:
    issue, problem = fetch_in_scope(issue_id)
    if problem:
        return refuse(problem)
    try:
        report = json.loads(report_json)
    except json.JSONDecodeError as e:
        return refuse(f"report_json is not valid JSON: {e}")
    if not isinstance(report, dict) or report.get("report_version") != tc.REPORT_VERSION:
        return refuse("unknown report format")
    if report.get("digest") != tc.report_digest(report):
        return refuse("digest mismatch: the report was altered after repro.py wrote it. "
                      "Pass the report file contents verbatim.")
    if report.get("issue_id") != issue["identifier"]:
        return refuse(f"report is for {report.get('issue_id')}, not {issue['identifier']}")
    with ledger() as db:
        src = db.execute("SELECT * FROM sources WHERE source_id = ?", (report.get("source_id"),)).fetchone()
    if src is None or src["issue"] != issue["identifier"]:
        return refuse(f"source {report.get('source_id')!r} was not exported by this gate for {issue['identifier']}")
    if report.get("source_tree_sha256") != src["tree_sha256"]:
        return refuse("report does not match the source the gate exported")

    outcome, reasons = tc.compute_outcome(report)
    if outcome == tc.INCOMPLETE:
        return refuse("not enough evidence: " + "; ".join(reasons), outcome=outcome)
    if report.get("outcome") != outcome:
        return refuse(f"report claims {report.get('outcome')} but its evidence means {outcome}", outcome=outcome)
    summary = tc.summarize(report, outcome)
    if report.get("summary") != summary:
        return refuse("report summary does not match its evidence")

    patch = report.get("patch")
    if patch and outcome == tc.FIXED:
        tree, err = _patched_tree(src["source_id"], patch["diff"])
        if err:
            return refuse(err)
        if tree != patch.get("patched_tree_sha256"):
            return refuse("re-applying the patch to the exported source doesn't give the tree the report tested")

    attempt_id = "att_" + secrets.token_hex(4)
    (STATE_DIR / "reports").mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "reports" / f"{attempt_id}.json").write_text(report_json)
    with ledger() as db:
        db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (attempt_id, issue["identifier"], issue["id"], src["source_id"], outcome, summary,
                    json.dumps(reasons), report["digest"], (patch or {}).get("diff_sha256"),
                    ticket_fingerprint(issue), now().isoformat()))
    label_error = set_label(issue, REPRO_GROUP, OUTCOME_LABELS[outcome])
    return {
        "ok": True,
        "attempt_id": attempt_id,
        "issue": issue["identifier"],
        "outcome": outcome,
        "summary": summary,
        "reasons": reasons,
        "patch": ({"files": patch["files"], "added": patch["added"], "removed": patch["removed"]}
                  if patch and outcome == tc.FIXED else None),
        "label": f"{REPRO_GROUP} → {OUTCOME_LABELS[outcome]}" + (f" (not set: {label_error})" if label_error else ""),
        "next": "draft the reply, then propose_reply(attempt_id, body)",
    }


# ---------------------------------------------------------------- propose_reply / reply_to_customer

def _check_draft(attempt: sqlite3.Row, body: str) -> tuple[dict | None, str | None, list[str]]:
    issue, problem = fetch_in_scope(attempt["issue"])
    if problem:
        return None, problem, []
    problem = attempt_problem(attempt, issue)
    if problem:
        return issue, problem, []
    return issue, None, policy.check_reply(body, attempt["outcome"], ticket_text(issue),
                                           [linear.api_key, GATE_TOKEN])


def _propose_reply(attempt_id: str, body: str) -> dict:
    with ledger() as db:
        attempt = db.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        replied = db.execute("SELECT 1 FROM replies WHERE attempt_id = ?", (attempt_id,)).fetchone()
    if attempt is None:
        return refuse(f"unknown attempt_id {attempt_id!r}")
    if replied:
        return refuse(f"a reply for {attempt_id} was already sent")
    issue, problem, problems = _check_draft(attempt, body)
    if problem:
        return refuse(problem)
    if problems:
        return refuse("draft not accepted: " + "; ".join(problems), problems=problems)

    draft_id = "dr_" + secrets.token_hex(4)
    final_body = policy.render_reply(body, attempt["outcome"])
    with ledger() as db:
        db.execute("UPDATE drafts SET status = 'superseded', closed_at = ? WHERE attempt_id = ? AND status = 'proposed'",
                   (now().isoformat(), attempt_id))
        db.execute("INSERT INTO drafts VALUES (?,?,?,?,?,?,?,?,?)",
                   (draft_id, attempt_id, attempt["issue"], body, final_body, "proposed", now().isoformat(), None, None))
    label_error = set_label(issue, AGENT_GROUP, "Awaiting approval")
    return {
        "ok": True,
        "draft_id": draft_id,
        "issue": attempt["issue"],
        "outcome": attempt["outcome"],
        "will_post": final_body,
        "label": f"{AGENT_GROUP} → Awaiting approval" + (f" (not set: {label_error})" if label_error else ""),
        "next": "call reply_to_customer(draft_id, body) with the same body; a human approves it",
    }


def _reply_to_customer(draft_id: str, body: str) -> dict:
    with _reply_lock:
        with ledger() as db:
            draft = db.execute("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
            attempt = db.execute("SELECT * FROM attempts WHERE attempt_id = ?",
                                 (draft["attempt_id"] if draft else None,)).fetchone()
            replied = db.execute("SELECT 1 FROM replies WHERE attempt_id = ?",
                                 (draft["attempt_id"] if draft else None,)).fetchone()
        if draft is None or attempt is None:
            return refuse(f"unknown draft_id {draft_id!r}; call propose_reply first")
        if draft["status"] != "proposed":
            return refuse(f"draft {draft_id} is {draft['status']}")
        if replied:
            return refuse(f"a reply for {draft['attempt_id']} was already sent")
        if body != draft["body"]:
            return refuse("body differs from the proposed draft; propose the new text first")
        issue, problem, problems = _check_draft(attempt, body)
        if problem or problems:
            return refuse(problem or "draft no longer passes: " + "; ".join(problems))
        try:
            comment = linear.create_comment(issue["id"], draft["final_body"])
        except LinearError as e:
            return refuse(f"Linear rejected the comment: {e}")
        with ledger() as db:
            db.execute("INSERT INTO replies (draft_id, attempt_id, issue, comment_id, comment_url, posted_at) "
                       "VALUES (?,?,?,?,?,?)",
                       (draft_id, attempt["attempt_id"], attempt["issue"], comment["id"], comment["url"],
                        now().isoformat()))
            db.execute("UPDATE drafts SET status = 'sent', closed_at = ? WHERE draft_id = ?",
                       (now().isoformat(), draft_id))
    label_error = set_label(issue, AGENT_GROUP, "Replied")
    return {
        "ok": True,
        "sent": True,
        "issue": attempt["issue"],
        "comment_id": comment["id"],
        "comment_url": comment["url"],
        "label": f"{AGENT_GROUP} → Replied" + (f" (not set: {label_error})" if label_error else ""),
    }


def _record_decline(draft_id: str, reason: str) -> dict:
    with ledger() as db:
        draft = db.execute("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
        if draft is None:
            return refuse(f"unknown draft_id {draft_id!r}")
        if draft["status"] != "proposed":
            return refuse(f"draft {draft_id} is {draft['status']}")
        db.execute("UPDATE drafts SET status = 'declined', closed_at = ?, note = ? WHERE draft_id = ?",
                   (now().isoformat(), reason[:500], draft_id))
    issue, problem = fetch_in_scope(draft["issue"])
    label_error = set_label(issue, AGENT_GROUP, "Declined") if issue else problem
    return {"ok": True, "draft_id": draft_id, "status": "declined",
            "label": f"{AGENT_GROUP} → Declined" + (f" (not set: {label_error})" if label_error else "")}


def _ticket_status(issue_id: str) -> dict:
    ident = issue_id.upper()
    with ledger() as db:
        rows = lambda q: [dict(r) for r in db.execute(q, (ident,))]  # noqa: E731
        return {
            "issue": ident,
            "trigger": rows("SELECT * FROM triggered_issues WHERE issue = ?"),
            "sources": rows("SELECT source_id, project, commit_sha, dirty, exported_at FROM sources WHERE issue = ?"),
            "attempts": rows("SELECT attempt_id, source_id, outcome, summary, submitted_at FROM attempts WHERE issue = ?"),
            "drafts": rows("SELECT draft_id, attempt_id, status, proposed_at, closed_at, note FROM drafts WHERE issue = ?"),
            "replies": rows("SELECT draft_id, attempt_id, comment_url, posted_at FROM replies WHERE issue = ?"),
        }


# ---------------------------------------------------------------- MCP

mcp = MCPServer(
    "ticket-gate",
    instructions=(
        "Gatekeeper between the ticket-resolver agent and Linear. The agent never holds Linear write access. "
        "Flow: export_source -> reproduce in the sandbox -> submit_attempt -> propose_reply -> "
        "(human-approved) reply_to_customer."),
)


@mcp.tool(
    annotations=ToolAnnotations(title="Export product source", read_only_hint=True, destructive_hint=False,
                                idempotent_hint=False, open_world_hint=False),
    structured_output=False,
)
async def export_source(issue_id: str) -> str:
    """Pack the product code for one in-scope bug ticket (e.g. ZYN-12) so the sandbox can reproduce against it.

    Returns one text blob: a header line `TICKET-GATE-SOURCE v1 {json}` (source_id, issue_id, project, commit,
    test_command, tree and archive sha256) followed by the base64 tar.gz. It is large: the harness saves it to a
    file in the sandbox. Pass that file path to `repro.py setup --source`. Never paste it."""
    return await anyio.to_thread.run_sync(_export_source, issue_id)


@mcp.tool(
    annotations=ToolAnnotations(title="Submit reproduction attempt", read_only_hint=False, destructive_hint=False,
                                idempotent_hint=False, open_world_hint=False),
)
async def submit_attempt(issue_id: str, report_json: str) -> dict:
    """Record a reproduction attempt. `report_json` must be the exact contents of the file repro.py report wrote.

    The gate checks the digest, matches the report to a source it exported for this ticket, recomputes the outcome
    (FIXED / REPRODUCED_NO_FIX / NOT_REPRODUCED) and summary from the raw evidence, re-applies any patch to the
    exported source, and sets the ticket's Repro label. Returns attempt_id and the outcome."""
    return await anyio.to_thread.run_sync(_submit_attempt, issue_id, report_json)


@mcp.tool(
    annotations=ToolAnnotations(title="Propose customer reply", read_only_hint=False, destructive_hint=False,
                                idempotent_hint=False, open_world_hint=False),
)
async def propose_reply(attempt_id: str, body: str) -> dict:
    """Check and store a draft reply for a submitted attempt. Nothing is sent.

    Refuses drafts that claim more than the outcome shows, claim anything was deployed, include secrets,
    internal paths, tool names, stack traces or a status line, or mention an MBI that isn't in the ticket.
    Refuses if the ticket changed since the attempt. Returns draft_id and the exact text that would be posted
    (the gate adds a status header and a disclosure line). Marks the ticket Awaiting approval."""
    return await anyio.to_thread.run_sync(_propose_reply, attempt_id, body)


@mcp.tool(
    annotations=ToolAnnotations(title="Reply to the customer on the ticket", read_only_hint=False,
                                destructive_hint=True, idempotent_hint=False, open_world_hint=True),
)
async def reply_to_customer(draft_id: str, body: str) -> dict:
    """Post a proposed draft as a comment on the ticket, visible to the reporter. Requires human approval.

    - draft_id: from propose_reply.
    - body: the same body passed to propose_reply, character for character.

    Posts exactly the text propose_reply returned as `will_post`. Refuses unknown, superseded, declined or
    already-sent drafts, a changed body, a ticket that changed since the attempt, and attempts older than 24h.
    Does not merge or deploy anything and does not close the ticket."""
    return await anyio.to_thread.run_sync(_reply_to_customer, draft_id, body)


@mcp.tool(
    annotations=ToolAnnotations(title="Record declined reply", read_only_hint=False, destructive_hint=False,
                                idempotent_hint=True, open_world_hint=False),
)
async def record_decline(draft_id: str, reason: str = "") -> dict:
    """After a human denied reply_to_customer: mark the draft declined and label the ticket Declined."""
    return await anyio.to_thread.run_sync(_record_decline, draft_id, reason)


@mcp.tool(
    annotations=ToolAnnotations(title="Ticket status", read_only_hint=True, destructive_hint=False,
                                idempotent_hint=True, open_world_hint=False),
)
async def ticket_status(issue_id: str) -> dict:
    """Everything the gate recorded for one ticket: trigger, exported sources, attempts, drafts and replies."""
    return await anyio.to_thread.run_sync(_ticket_status, issue_id)


class BearerAuth:
    """Pure ASGI middleware: every HTTP request needs `Authorization: Bearer <TICKET_GATE_TOKEN>`."""

    def __init__(self, app, token: str):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def main():
    missing = [k for k, v in {"LINEAR_API_KEY": linear.api_key, "TICKET_GATE_TOKEN": GATE_TOKEN}.items() if not v]
    if missing:
        sys.exit(f"ticket-gate: missing config {', '.join(missing)} (copy .env.example to .env)")
    if len(GATE_TOKEN) < 16:
        sys.exit("ticket-gate: TICKET_GATE_TOKEN is too short; use `openssl rand -hex 24`")
    if not (REPO_PATH / "README.md").exists() and not any(REPO_PATH.iterdir()):
        sys.exit(f"ticket-gate: TICKET_REPO_PATH {REPO_PATH} is empty")
    if shutil.which("git") is None:
        print("ticket-gate: warning: git not found; FIXED attempts can't be verified and will be refused", flush=True)
    import uvicorn
    app = BearerAuth(mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=16 * 1024 * 1024,
        host=HOST,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*",
                           "host.docker.internal", "host.docker.internal:*"]
            + [h for h in os.environ.get("TICKET_GATE_ALLOWED_HOSTS", "").split(",") if h],
        ),
    ), GATE_TOKEN)
    print(f"ticket-gate: team {TEAM_KEY}, label {TRIGGER_LABEL}, source {REPO_PATH} ({PROJECT}); "
          f"MCP at http://{HOST}:{PORT}/mcp", flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
