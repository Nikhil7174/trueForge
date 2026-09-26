"""ship-gate: the only process that holds the GitHub token. It never holds database credentials.

MCP tools (streamable HTTP, bearer auth):
  release_scope       read-only -> commits since the last tag, changed files, migrations + their sha256
  submit_verification verifies a sandbox test report, recomputes PASS/FAIL, records it in the ledger
  publish_release     tag + GitHub release, guarded (destructive, needs approval)
  release_status      ledger view of verifications and publish attempts

The rule that makes this more than a tagger: publish_release refuses while any migration in the
release is not yet applied to production. It reads that from db-gate's ledger, so "applied" means
a human approved apply_migration after a gate-verified rehearsal - not that someone said so.

Config (env or ./.env): GITHUB_TOKEN, GITHUB_REPO, GATE_TOKEN, SHIP_HOST, SHIP_PORT,
GATE_STATE_DIR (default ./.gate, shared with db-gate), SHIP_MAX_VERIFICATION_AGE_HOURS,
SHIP_REQUIRE_MIGRATIONS_APPLIED (default true).
Run with `tf-ship` (or `python -m trueforge_hackathon ship`).
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from trueforge_hackathon.env import load_env_file

# gatecore hashes migrations the same way db-gate does, so a sha computed here matches a sha
# rehearsed there. Never hash migration SQL any other way.
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "skills" / "migration-rehearsal" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "release-captain" / "scripts"))
import gatecore  # noqa: E402
import shipcore  # noqa: E402

load_env_file()

import anyio  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp_types import ToolAnnotations  # noqa: E402

from trueforge_hackathon.agents.release_captain import github  # noqa: E402

GATE_TOKEN = os.environ.get("GATE_TOKEN", "")
HOST = os.environ.get("SHIP_HOST", "127.0.0.1")
PORT = int(os.environ.get("SHIP_PORT", "8812"))
STATE_DIR = Path(os.environ.get("GATE_STATE_DIR", Path.cwd() / ".gate"))
MAX_VERIFICATION_AGE = dt.timedelta(hours=float(os.environ.get("SHIP_MAX_VERIFICATION_AGE_HOURS", "24")))
REQUIRE_MIGRATIONS = os.environ.get("SHIP_REQUIRE_MIGRATIONS_APPLIED", "true").lower() != "false"
MIGRATION_GLOBS = tuple(
    g.strip() for g in os.environ.get("SHIP_MIGRATION_PATHS", "migrations/,demo/migrations/").split(",") if g.strip()
)

_publish_lock = threading.Lock()


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


# ---------------------------------------------------------------- ledger (shared with db-gate)

def ledger() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(STATE_DIR / "ledger.db")
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS verifications (
        verification_id TEXT PRIMARY KEY, repo TEXT, from_tag TEXT, head_sha TEXT,
        verdict TEXT, summary TEXT, reasons TEXT, migrations TEXT, digest TEXT,
        verified_at TEXT, submitted_at TEXT);
    CREATE TABLE IF NOT EXISTS releases (
        id INTEGER PRIMARY KEY AUTOINCREMENT, verification_id TEXT, repo TEXT, tag TEXT,
        status TEXT, detail TEXT, at TEXT);
    """)
    return db


def refuse(reason: str, **extra) -> dict:
    return {"published": False, "refused": True, "reason": reason, **extra}


def audit(verification_id: str, repo: str, tag: str, status: str, detail: Any) -> None:
    with ledger() as db:
        db.execute("INSERT INTO releases (verification_id, repo, tag, status, detail, at) "
                   "VALUES (?,?,?,?,?,?)",
                   (verification_id, repo, tag, status,
                    detail if isinstance(detail, str) else json.dumps(detail), now().isoformat()))


# ---------------------------------------------------------------- migration cross-check

def migration_state(sha: str) -> dict[str, Any]:
    """Where a migration stands in db-gate's ledger. Same states migration_status reports."""
    with ledger() as db:
        applied = db.execute("SELECT * FROM applications WHERE migration_sha256 = ? AND status = 'applied' "
                             "ORDER BY id DESC LIMIT 1", (sha,)).fetchone()
        rehearsal = db.execute("SELECT * FROM rehearsals WHERE migration_sha256 = ? "
                               "ORDER BY submitted_at DESC LIMIT 1", (sha,)).fetchone()
    if applied:
        return {"state": "applied", "applied_at": applied["at"], "target": applied["target"]}
    if rehearsal is None:
        return {"state": "not_rehearsed"}
    verdict = rehearsal["verdict"]
    if verdict == gatecore.VERDICT_BLOCK:
        return {"state": "blocked", "summary": rehearsal["summary"]}
    if verdict == gatecore.VERDICT_REVIEW:
        return {"state": "ready_needs_review", "summary": rehearsal["summary"]}
    return {"state": "ready_safe", "summary": rehearsal["summary"]}


def _is_migration(path: str) -> bool:
    return path.endswith(".sql") and any(g in path for g in MIGRATION_GLOBS)


# ---------------------------------------------------------------- release_scope

def _latest_tag(repo: str | None) -> Optional[str]:
    tags = github.list_tags(repo)
    return tags[0]["name"] if tags else None


def _release_scope(from_tag: Optional[str], to_ref: Optional[str]) -> dict:
    repo = github.REPO
    head = github.head_sha(to_ref or "HEAD", repo)
    base = from_tag or _latest_tag(repo)
    if not base:
        return {"repo": repo, "head_sha": head, "from_tag": None, "commits": [], "files": [],
                "migrations": [], "tags": [],
                "note": "no tags in this repository yet; pass from_tag to scope a range"}
    cmp = github.compare(base, head, repo)
    commits = [{"sha": c["sha"][:12], "message": (c["commit"]["message"] or "").splitlines()[0][:200],
                "author": (c["commit"].get("author") or {}).get("name")}
               for c in cmp.get("commits", [])]
    files = [f["filename"] for f in cmp.get("files", [])]
    migrations = []
    for path in files:
        if not _is_migration(path):
            continue
        try:
            sql = github.file_text(path, head, repo)
        except github.GitHubError:
            continue  # deleted in this range; nothing to apply
        sha = gatecore.sql_sha256(sql)
        migrations.append({"path": path, "sha256": sha, **migration_state(sha)})
    return {"repo": repo, "head_sha": head, "from_tag": base, "commits": commits,
            "files": files, "migrations": migrations,
            "tags": [t["name"] for t in github.list_tags(repo)][:20],
            "next": ("Run verify_build.py in the sandbox, then submit_verification. "
                     "publish_release refuses while any migration above is not 'applied'.")}


# ---------------------------------------------------------------- submit_verification

def _submit_verification(report_json: str) -> dict:
    try:
        report = json.loads(report_json)
    except json.JSONDecodeError as e:
        return {"accepted": False, "reason": f"report_json is not valid JSON: {e}"}
    if not isinstance(report, dict) or report.get("report_version") != shipcore.REPORT_VERSION:
        return {"accepted": False, "reason": "unknown report format"}
    if report.get("digest") != shipcore.verification_digest(report):
        return {"accepted": False, "reason": "digest mismatch: the report was altered after verify_build.py "
                                             "wrote it. Pass the report file contents verbatim."}
    if report.get("repo") != github.REPO:
        return {"accepted": False,
                "reason": f"report is for {report.get('repo')!r} but this gate serves {github.REPO!r}"}
    head_now = github.head_sha(report.get("head_ref") or "HEAD", github.REPO)
    if report.get("head_sha") != head_now:
        return {"accepted": False,
                "reason": f"the repository moved on: verified {str(report.get('head_sha'))[:12]}, "
                          f"{head_now[:12]} is current. Verify again.",
                "verified_head": report.get("head_sha"), "current_head": head_now}

    findings = report.get("findings") or {}
    verdict, reasons = shipcore.compute_verdict(findings)
    summary = shipcore.summarize(findings, verdict)
    if report.get("verdict") != verdict:
        return {"accepted": False, "verdict": verdict,
                "reason": f"report claims {report.get('verdict')} but its findings mean {verdict}"}
    if report.get("summary") != summary:
        return {"accepted": False, "reason": "report summary does not match its findings"}

    migrations = findings.get("migrations") or []
    vid = "vf_" + secrets.token_hex(4)
    with ledger() as db:
        db.execute("INSERT INTO verifications (verification_id, repo, from_tag, head_sha, verdict, summary, "
                   "reasons, migrations, digest, verified_at, submitted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (vid, github.REPO, findings.get("from_tag"), report["head_sha"], verdict, summary,
                    json.dumps(reasons), json.dumps(migrations), report["digest"],
                    report.get("verified_at"), now().isoformat()))
    out = {"accepted": True, "verification_id": vid, "verdict": verdict, "reasons": reasons,
           "summary": summary, "target_repo": github.REPO,
           "migrations": [{**m, **migration_state(m["sha256"])} for m in migrations]}
    if verdict == shipcore.VERDICT_FAIL:
        out["next"] = "Tests did not pass. Do not request approval; report the failures."
    else:
        out["next"] = ("To publish, call publish_release with verification_id, tag, "
                       f"target_repo={github.REPO!r} and release_summary={summary!r} verbatim. "
                       "It refuses while any migration in scope is not applied to production.")
    return out


# ---------------------------------------------------------------- publish_release

def _publish_release(verification_id: str, tag: str, target_repo: str, release_summary: str,
                     notes_md: str) -> dict:
    with ledger() as db:
        vf = db.execute("SELECT * FROM verifications WHERE verification_id = ?",
                        (verification_id,)).fetchone()
    if vf is None:
        return refuse(f"unknown verification_id {verification_id!r}")
    if target_repo != github.REPO or target_repo != vf["repo"]:
        return refuse(f"target_repo {target_repo!r} is not this gate's repository {github.REPO!r}")
    if release_summary != vf["summary"]:
        return refuse("release_summary does not match the gate's record; quote it verbatim",
                      expected=vf["summary"])
    if vf["verdict"] != shipcore.VERDICT_PASS:
        return refuse(f"verification verdict is {vf['verdict']}", summary=vf["summary"])
    if not (notes_md or "").strip():
        return refuse("notes_md is empty; a release a human would publish needs notes")
    if not re.fullmatch(r"[A-Za-z0-9._\-+/]{1,100}", tag or ""):
        return refuse(f"tag {tag!r} is not a usable git tag name")
    age = now() - dt.datetime.fromisoformat(vf["submitted_at"])
    if age > MAX_VERIFICATION_AGE:
        return refuse(f"verification is stale ({age} old, limit {MAX_VERIFICATION_AGE}); verify again")

    # The integration: code may not ship ahead of the schema it expects.
    migrations = json.loads(vf["migrations"] or "[]")
    if REQUIRE_MIGRATIONS:
        blocking = []
        for m in migrations:
            st = migration_state(m["sha256"])
            if st["state"] != "applied":
                blocking.append({**m, **st})
        if blocking:
            detail = "; ".join(f"{b['path']} ({b['sha256'][:12]}) is {b['state']}" for b in blocking)
            audit(verification_id, target_repo, tag, "refused", detail)
            return refuse(
                f"{len(blocking)} migration(s) in this release are not applied to production: {detail}. "
                "Publishing would ship code that expects a schema production does not have. "
                "Rehearse and apply them through db-gate first.",
                migrations=blocking)

    try:
        head_now = github.head_sha("HEAD", target_repo)
    except github.GitHubError as e:
        return refuse(f"could not read the repository: {e}")
    if head_now != vf["head_sha"]:
        audit(verification_id, target_repo, tag, "refused", "head drift")
        return refuse(f"the repository moved on since verification: verified {vf['head_sha'][:12]}, "
                      f"{head_now[:12]} is current. Verify again.")
    if tag in {t["name"] for t in github.list_tags(target_repo)}:
        audit(verification_id, target_repo, tag, "refused", "tag exists")
        return refuse(f"tag {tag!r} already exists in {target_repo}")

    with _publish_lock:
        with ledger() as db:
            done = db.execute("SELECT * FROM releases WHERE repo = ? AND tag = ? AND status = 'published'",
                              (target_repo, tag)).fetchone()
        if done:
            return refuse(f"{tag} was already published at {done['at']}")
        try:
            github.create_tag(tag, vf["head_sha"], target_repo)
        except github.GitHubError as e:
            audit(verification_id, target_repo, tag, "failed", f"create_tag: {e}")
            return refuse(f"could not create the tag: {e}")
        try:
            release = github.create_release(tag, tag, notes_md, target_repo)
        except github.GitHubError as e:
            try:  # a tag without a release is litter; take it back
                github.delete_tag(tag, target_repo)
                rolled = "tag deleted"
            except github.GitHubError as e2:
                rolled = f"tag left behind ({e2})"
            audit(verification_id, target_repo, tag, "rolled_back", f"create_release: {e}; {rolled}")
            return refuse(f"tag created but the release failed, so it was rolled back ({rolled}): {e}")

    audit(verification_id, target_repo, tag, "published", {"url": release.get("html_url"),
                                                           "sha": vf["head_sha"]})
    return {"published": True, "refused": False, "repo": target_repo, "tag": tag,
            "commit": vf["head_sha"], "release_url": release.get("html_url"),
            "verification_id": verification_id, "summary": vf["summary"],
            "migrations_applied": [m["path"] for m in migrations]}


# ---------------------------------------------------------------- release_status

def _release_status(tag: Optional[str]) -> dict:
    with ledger() as db:
        vfs = [dict(r) for r in db.execute(
            "SELECT verification_id, repo, from_tag, head_sha, verdict, summary, submitted_at "
            "FROM verifications ORDER BY submitted_at DESC LIMIT 10")]
        q = "SELECT verification_id, repo, tag, status, detail, at FROM releases"
        rows = ([dict(r) for r in db.execute(q + " WHERE tag = ? ORDER BY id DESC", (tag,))] if tag
                else [dict(r) for r in db.execute(q + " ORDER BY id DESC LIMIT 10")])
    return {"repo": github.REPO, "verifications": vfs, "releases": rows}


# ---------------------------------------------------------------- MCP surface

mcp = MCPServer(name="ship-gate")
READ = dict(readOnlyHint=True, destructiveHint=False, openWorldHint=True)


@mcp.tool(annotations=ToolAnnotations(title="Release scope", idempotentHint=True, **READ))
async def release_scope(from_tag: Optional[str] = None, to_ref: Optional[str] = None) -> dict:
    """Commits, changed files and migrations between a tag and a ref, with each migration's sha256
    and where it stands in db-gate's ledger (not_rehearsed / blocked / ready_* / applied).

    from_tag defaults to the newest tag; to_ref defaults to the repository's default branch."""
    return await anyio.to_thread.run_sync(_release_scope, from_tag, to_ref)


@mcp.tool(annotations=ToolAnnotations(title="Submit verification report", readOnlyHint=False,
                                      destructiveHint=False, idempotentHint=False, openWorldHint=True))
async def submit_verification(report_json: str) -> dict:
    """Record a build verification. `report_json` must be the exact contents of the report file
    verify_build.py wrote. The gate checks its digest, checks the repository has not moved on, and
    recomputes PASS/FAIL from the raw test findings. Returns verification_id, summary and
    target_repo: the values publish_release needs."""
    return await anyio.to_thread.run_sync(_submit_verification, report_json)


@mcp.tool(annotations=ToolAnnotations(title="Publish release to GitHub", readOnlyHint=False,
                                      destructiveHint=True, idempotentHint=False, openWorldHint=True))
async def publish_release(verification_id: str, tag: str, target_repo: str, release_summary: str,
                          notes_md: str) -> dict:
    """IRREVERSIBLE: creates the tag and publishes a GitHub release. Requires human approval.

    - target_repo and release_summary: copied verbatim from submit_verification, so the approval
      card shows the real repository and the real verified impact line.
    - notes_md: the release notes, as a human would publish them.

    Refuses a FAILing verification, empty notes, a stale verification, a tag that exists, a
    repository that moved on since verification, a repeat publish, and - the rule that matters -
    any release containing a migration that is not yet applied to production."""
    return await anyio.to_thread.run_sync(_publish_release, verification_id, tag, target_repo,
                                          release_summary, notes_md)


@mcp.tool(annotations=ToolAnnotations(title="Release status", idempotentHint=True, **READ))
async def release_status(tag: Optional[str] = None) -> dict:
    """Recent verifications and publish attempts from the ledger, including refusals."""
    return await anyio.to_thread.run_sync(_release_status, tag)


class BearerAuth:
    """Pure ASGI middleware: every HTTP request needs `Authorization: Bearer <GATE_TOKEN>`."""

    def __init__(self, app, token: str):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def main():
    missing = [k for k, v in {"GITHUB_REPO": github.REPO, "GITHUB_TOKEN": github.TOKEN,
                              "GATE_TOKEN": GATE_TOKEN}.items() if not v]
    if missing:
        sys.exit(f"ship-gate: missing config {', '.join(missing)} (copy .env.example to .env)")
    if len(GATE_TOKEN) < 16:
        sys.exit("ship-gate: GATE_TOKEN is too short; use `openssl rand -hex 24`")
    if "/" not in github.REPO:
        sys.exit(f"ship-gate: GITHUB_REPO must look like owner/name, got {github.REPO!r}")
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
            + [h for h in os.environ.get("GATE_ALLOWED_HOSTS", "").split(",") if h],
        ),
    ), GATE_TOKEN)
    print(f"ship-gate: repo {github.REPO}; migrations must be applied: {REQUIRE_MIGRATIONS}; "
          f"MCP at http://{HOST}:{PORT}/mcp", flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
