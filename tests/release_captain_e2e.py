#!/usr/bin/env python3
"""End-to-end guard test for ship-gate, without TrueForge and without GitHub.

Plays the agent: calls ship-gate over MCP exactly like TrueForge does, runs verify_build.py's
report shape where the sandbox would, and checks every guard - above all the one that makes this
more than a tagger: a release cannot be published while a migration it contains is unapplied.

GitHub is a local stub (GITHUB_API_URL), so this runs with no token and no network. The stub only
stands in for the transport; every refusal under test is ship-gate's own. The live demo uses real
GitHub.

Migration states come from rows written straight into db-gate's ledger, which is the table
ship-gate reads. Writing them here keeps the test fast and hermetic; the live demo gets them the
real way, by rehearsing and applying through db-gate.

Run from the repo root, with the stub and gate started by this script:
    python tests/release_captain_e2e.py
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "migration-rehearsal" / "scripts"))
sys.path.insert(0, str(ROOT / "skills" / "release-captain" / "scripts"))
import gatecore  # noqa: E402
import shipcore  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.shared._httpx_utils import create_mcp_http_client  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)

REPO = "acme/claims-platform"
BASE_TAG = "v1.3.0"
HEAD = "a" * 40
WORK = Path(tempfile.mkdtemp(prefix="ship-e2e-"))
STATE_DIR = WORK / ".gate"
TOKEN = secrets.token_hex(24)

passed, failed = [], []


def check(name: str, ok: bool, info=None):
    (passed if ok else failed).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok or info is None else f"\n        -> {info}"))


# ---------------------------------------------------------------- GitHub stub

MIGRATIONS = {
    "migrations/0101_add_mbi_normalized.sql": "ALTER TABLE patients ADD COLUMN mbi_normalized char(11);\n",
    "migrations/0102_index_mbi_normalized.sql": "CREATE INDEX patients_mbi_norm_idx ON patients (mbi_normalized);\n",
}


class Stub(BaseHTTPRequestHandler):
    state = {"head": HEAD, "tags": [BASE_TAG], "refs": [], "releases": [],
             "fail_release": False}

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        q = urlparse(self.path).query
        st = Stub.state
        if path == f"/repos/{REPO}":
            return self._send(200, {"default_branch": "main"})
        if path == f"/repos/{REPO}/tags":
            return self._send(200, [{"name": t} for t in st["tags"]])
        if path.startswith(f"/repos/{REPO}/commits/"):
            return self._send(200, {"sha": st["head"]})
        if path.startswith(f"/repos/{REPO}/compare/"):
            commits = [{"sha": f"{i:040x}",
                        "commit": {"message": m, "author": {"name": "Dev"}}}
                       for i, m in enumerate(
                           ["feat: normalise MBI for attribution",
                            "feat: read mbi_normalized in the attribution job",
                            "fix: guard against null mbi", "chore: bump deps"], 1)]
            files = [{"filename": f} for f in [*MIGRATIONS, "src/attribution.py", "tests/test_attribution.py"]]
            return self._send(200, {"commits": commits, "files": files})
        if path.startswith(f"/repos/{REPO}/contents/"):
            rel = path.split("/contents/", 1)[1]
            if rel not in MIGRATIONS:
                return self._send(404, {"message": "Not Found"})
            return self._send(200, {"encoding": "base64",
                                    "content": base64.b64encode(MIGRATIONS[rel].encode()).decode()})
        return self._send(404, {"message": f"stub has no GET {path}"})

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        st = Stub.state
        if path == f"/repos/{REPO}/git/refs":
            tag = body["ref"].removeprefix("refs/tags/")
            st["refs"].append({"tag": tag, "sha": body["sha"]})
            st["tags"].insert(0, tag)
            return self._send(201, {"ref": body["ref"], "object": {"sha": body["sha"]}})
        if path == f"/repos/{REPO}/releases":
            if st["fail_release"]:
                return self._send(422, {"message": "stubbed release failure"})
            st["releases"].append(body)
            return self._send(201, {"html_url": f"https://github.com/{REPO}/releases/tag/{body['tag_name']}"})
        return self._send(404, {"message": f"stub has no POST {path}"})

    def do_DELETE(self):
        path = unquote(urlparse(self.path).path)
        st = Stub.state
        if "/git/refs/tags/" in path:
            tag = path.split("/git/refs/tags/", 1)[1]
            st["tags"] = [t for t in st["tags"] if t != tag]
            st["refs"] = [r for r in st["refs"] if r["tag"] != tag]
            return self._send(204, {})
        return self._send(404, {"message": f"stub has no DELETE {path}"})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- ledger fixtures

def ledger():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(STATE_DIR / "ledger.db")
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS rehearsals (
        rehearsal_id TEXT PRIMARY KEY, snapshot_id TEXT, target TEXT, migration_name TEXT,
        migration_sha256 TEXT, verdict TEXT, summary TEXT, reasons TEXT,
        snapshot_fingerprint TEXT, schema_after_fingerprint TEXT, digest TEXT,
        rehearsed_at TEXT, submitted_at TEXT);
    CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, rehearsal_id TEXT, migration_sha256 TEXT, target TEXT,
        status TEXT, detail TEXT, at TEXT);
    """)
    return db


def set_state(path: str, state: str) -> str:
    """Put a migration into one of db-gate's states and return its sha256."""
    sha = gatecore.sql_sha256(MIGRATIONS[path])
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    with ledger() as db:
        db.execute("DELETE FROM rehearsals WHERE migration_sha256 = ?", (sha,))
        db.execute("DELETE FROM applications WHERE migration_sha256 = ?", (sha,))
        if state == "not_rehearsed":
            return sha
        verdict = {"blocked": "BLOCK", "ready_needs_review": "REVIEW"}.get(state, "SAFE")
        db.execute("INSERT INTO rehearsals (rehearsal_id, migration_sha256, verdict, summary, "
                   "submitted_at, target, migration_name) VALUES (?,?,?,?,?,?,?)",
                   (f"rh_{secrets.token_hex(4)}", sha, verdict, f"{verdict}: fixture", now,
                    "appdb@localhost", Path(path).name))
        if state == "applied":
            db.execute("INSERT INTO applications (migration_sha256, target, status, detail, at) "
                       "VALUES (?,?,?,?,?)", (sha, "appdb@localhost", "applied", "fixture", now))
    return sha


# ---------------------------------------------------------------- report builder

def verification_report(*, head: str, migrations: list[dict], exit_code: int = 0,
                        passed_n: int = 42) -> dict:
    findings = {
        "from_tag": BASE_TAG,
        "commits": [{"sha": f"{i:012x}", "message": m} for i, m in enumerate(["a", "b", "c", "d"], 1)],
        "migrations": migrations,
        "tests": {"command": "python3 -m pytest -q", "exit_code": exit_code, "duration_ms": 8123,
                  "passed": passed_n, "failed": 0 if exit_code == 0 else 3, "skipped": 0,
                  "output_tail": "42 passed"},
        "install": None,
    }
    verdict, reasons = shipcore.compute_verdict(findings)
    report = {"report_version": shipcore.REPORT_VERSION, "repo": REPO, "head_sha": head,
              "head_ref": "main", "verdict": verdict, "reasons": reasons,
              "summary": shipcore.summarize(findings, verdict), "findings": findings,
              "verified_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()}
    report["digest"] = shipcore.verification_digest(report)
    return report


def redigest(report: dict) -> str:
    report = dict(report)
    report["digest"] = shipcore.verification_digest(report)
    return json.dumps(report)


class Ship:
    def __init__(self, session):
        self.s = session

    async def call(self, tool, **args):
        res = await self.s.call_tool(tool, args)
        if res.structured_content is not None:
            return res.structured_content
        text = res.content[0].text
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


# ---------------------------------------------------------------- the run

async def run(ship: Ship):
    m1, m2 = list(MIGRATIONS)

    print("\n1. release_scope reads the repo and the migration ledger")
    set_state(m1, "not_rehearsed")
    set_state(m2, "not_rehearsed")
    scope = await ship.call("release_scope")
    check("release_scope returns commits, files and both migrations",
          scope.get("from_tag") == BASE_TAG and scope.get("head_sha") == HEAD
          and len(scope.get("commits", [])) == 4 and len(scope.get("migrations", [])) == 2, scope)
    check("migration sha256 matches gatecore's hash of the committed file",
          {m["sha256"] for m in scope["migrations"]} == {gatecore.sql_sha256(MIGRATIONS[m1]),
                                                        gatecore.sql_sha256(MIGRATIONS[m2])})
    check("unrehearsed migrations report not_rehearsed",
          all(m["state"] == "not_rehearsed" for m in scope["migrations"]), scope["migrations"])

    print("\n2. reports can't be edited or relabelled")
    migs = [{"path": p, "sha256": gatecore.sql_sha256(MIGRATIONS[p])} for p in (m1, m2)]
    good = verification_report(head=HEAD, migrations=migs)
    tampered = json.loads(json.dumps(good))
    tampered["findings"]["tests"]["exit_code"] = 1
    t = await ship.call("submit_verification", report_json=json.dumps(tampered))
    check("tampered report (digest mismatch) rejected",
          not t.get("accepted") and "digest" in t.get("reason", ""), t)
    relabel = json.loads(json.dumps(verification_report(head=HEAD, migrations=migs, exit_code=1)))
    relabel["verdict"] = shipcore.VERDICT_PASS
    t = await ship.call("submit_verification", report_json=redigest(relabel))
    check("relabelled verdict rejected (gate recomputes from findings)",
          not t.get("accepted") and t.get("verdict") == shipcore.VERDICT_FAIL, t)
    wrong_repo = json.loads(json.dumps(good)); wrong_repo["repo"] = "someone/else"
    t = await ship.call("submit_verification", report_json=redigest(wrong_repo))
    check("report for another repository rejected", not t.get("accepted"), t)
    stale_head = json.loads(json.dumps(good)); stale_head["head_sha"] = "b" * 40
    t = await ship.call("submit_verification", report_json=redigest(stale_head))
    check("report for a commit that is no longer HEAD rejected",
          not t.get("accepted") and "moved on" in t.get("reason", ""), t)

    print("\n3. a failing suite cannot be published")
    failing = verification_report(head=HEAD, migrations=migs, exit_code=1)
    sf = await ship.call("submit_verification", report_json=json.dumps(failing))
    check("failing tests -> FAIL (gate-recomputed)",
          sf.get("accepted") and sf["verdict"] == shipcore.VERDICT_FAIL, sf)
    r = await ship.call("publish_release", verification_id=sf["verification_id"], tag="v1.4.0",
                        target_repo=REPO, release_summary=sf["summary"], notes_md="## notes")
    check("publish refuses a FAIL verification", r.get("refused") and "FAIL" in r["reason"], r)

    print("\n4. THE INTEGRATION: green tests, unapplied migration, refused anyway")
    sv = await ship.call("submit_verification", report_json=json.dumps(good))
    check("passing tests -> PASS", sv.get("accepted") and sv["verdict"] == shipcore.VERDICT_PASS, sv)
    args = dict(verification_id=sv["verification_id"], tag="v1.4.0", target_repo=REPO,
                release_summary=sv["summary"],
                notes_md="## v1.4.0\n- normalise MBI for attribution\n")
    r = await ship.call("publish_release", **args)
    check("publish refuses while migrations are not_rehearsed",
          r.get("refused") and "not applied to production" in r["reason"]
          and len(r.get("migrations", [])) == 2, r)

    set_state(m1, "blocked")
    set_state(m2, "applied")
    r = await ship.call("publish_release", **args)
    check("publish still refuses, naming the blocked migration",
          r.get("refused") and "blocked" in r["reason"] and "0101" in r["reason"], r)

    set_state(m1, "ready_safe")
    r = await ship.call("publish_release", **args)
    check("rehearsed-but-unapplied is still not good enough (ready_safe)",
          r.get("refused") and "ready_safe" in r["reason"], r)

    print("\n5. the other publish guards")
    set_state(m1, "applied")
    r = await ship.call("publish_release", **{**args, "release_summary": sv["summary"] + " (roughly)"})
    check("publish refuses a paraphrased summary", r.get("refused") and "summary" in r["reason"], r)
    r = await ship.call("publish_release", **{**args, "target_repo": "someone/else"})
    check("publish refuses a different target_repo", r.get("refused") and "repository" in r["reason"], r)
    r = await ship.call("publish_release", **{**args, "verification_id": "vf_deadbeef"})
    check("publish refuses an unknown verification", r.get("refused") and "unknown" in r["reason"], r)
    r = await ship.call("publish_release", **{**args, "notes_md": "   "})
    check("publish refuses empty release notes", r.get("refused") and "notes" in r["reason"], r)
    r = await ship.call("publish_release", **{**args, "tag": "v1.4.0 oops"})
    check("publish refuses an unusable tag name", r.get("refused") and "tag" in r["reason"], r)
    r = await ship.call("publish_release", **{**args, "tag": BASE_TAG})
    check("publish refuses a tag that already exists", r.get("refused") and "already exists" in r["reason"], r)

    Stub.state["head"] = "c" * 40
    r = await ship.call("publish_release", **args)
    check("publish refuses when the repo moved on since verification",
          r.get("refused") and "moved on" in r["reason"], r)
    Stub.state["head"] = HEAD

    print("\n6. a tag without a release is rolled back")
    Stub.state["fail_release"] = True
    r = await ship.call("publish_release", **args)
    check("release failure rolls the tag back",
          r.get("refused") and "rolled back" in r["reason"] and "v1.4.0" not in Stub.state["tags"], r)
    Stub.state["fail_release"] = False

    print("\n7. everything applied and green -> publishes once")
    r = await ship.call("publish_release", **args)
    check("publishes when every migration is applied",
          r.get("published") and r.get("tag") == "v1.4.0"
          and Stub.state["releases"] and Stub.state["refs"][-1]["sha"] == HEAD, r)
    r2 = await ship.call("publish_release", **args)
    check("publish refuses a second publish of the same tag",
          r2.get("refused") and ("already" in r2["reason"]), r2)

    print("\n8. release_status records refusals, not just successes")
    st = await ship.call("release_status")
    statuses = [row["status"] for row in st.get("releases", [])]
    check("ledger holds the publish and the refusals",
          "published" in statuses and "refused" in statuses and "rolled_back" in statuses, statuses)


async def main():
    port = free_port()
    stub = ThreadingHTTPServer(("127.0.0.1", port), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    ship_port = free_port()
    env = {**os.environ, "GITHUB_API_URL": f"http://127.0.0.1:{port}", "GITHUB_REPO": REPO,
           "GITHUB_TOKEN": "stub-token", "GATE_TOKEN": TOKEN, "SHIP_PORT": str(ship_port),
           "GATE_STATE_DIR": str(STATE_DIR), "SHIP_REQUIRE_MIGRATIONS_APPLIED": "true"}
    proc = subprocess.Popen([sys.executable, "-m", "trueforge_hackathon", "ship"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = f"http://127.0.0.1:{ship_port}/mcp"
    try:
        for _ in range(60):
            if proc.poll() is not None:
                raise SystemExit(f"ship-gate exited early:\n{proc.stdout.read()}")
            try:
                import httpx
                httpx.post(url, json={}, timeout=2)
                break
            except Exception:
                time.sleep(0.5)

        import httpx
        r = httpx.post(url, json={}, timeout=5)
        check("ship-gate rejects requests without the bearer token", r.status_code == 401, r.status_code)

        http = create_mcp_http_client(headers={"Authorization": f"Bearer {TOKEN}"})
        async with http, streamable_http_client(url, http_client=http) as (rd, wr):
            async with ClientSession(rd, wr) as session:
                await session.initialize()
                tools = {t.name: t for t in (await session.list_tools()).tools}
                check("publish_release is annotated destructive",
                      tools["publish_release"].annotations.destructive_hint is True
                      and tools["publish_release"].annotations.read_only_hint is not True)
                check("release_scope is annotated read-only",
                      tools["release_scope"].annotations.read_only_hint is True)
                await run(Ship(session))
    finally:
        proc.terminate()
        stub.shutdown()

    print(f"\n{len(passed)} passed, {len(failed)} failed  (work dir {WORK})")
    if failed:
        print("failed: " + "; ".join(failed))
    shutil.rmtree(WORK, ignore_errors=True)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
