"""db-gate: the only process that holds production database credentials.

MCP tools (streamable HTTP, bearer auth):
  export_snapshot    read-only role -> pg_dump of the public schema, delivered as text
  submit_rehearsal   verifies a sandbox report, recomputes its verdict, records it in the ledger
  apply_migration    migration role -> runs a rehearsed migration, guarded (destructive, needs approval)
  migration_status   ledger + drift view, the Release Captain's entry ticket

Config (env or ./.env): READER_URL, MIGRATOR_URL, GATE_TOKEN, GATE_HOST, GATE_PORT,
MIGRATION_ROLE (default app_owner), GATE_STATE_DIR (default ./.gate).
Run with `tf-gate` (or `python -m trueforge_hackathon gate`).
"""
from __future__ import annotations

import base64
import datetime as dt
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from trueforge_hackathon.env import load_env_file

# gatecore is shared with the sandbox, so it lives in the skill pack. Editable install (`pip install -e`)
# keeps this path valid.
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "skills" / "migration-rehearsal" / "scripts"))
import gatecore  # noqa: E402

load_env_file()

import anyio  # noqa: E402
import psycopg  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp_types import ToolAnnotations  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.conninfo import conninfo_to_dict  # noqa: E402

READER_URL = os.environ.get("READER_URL", "")
MIGRATOR_URL = os.environ.get("MIGRATOR_URL", "")
GATE_TOKEN = os.environ.get("GATE_TOKEN", "")
HOST = os.environ.get("GATE_HOST", "127.0.0.1")
PORT = int(os.environ.get("GATE_PORT", "8811"))
MIGRATION_ROLE = os.environ.get("MIGRATION_ROLE", "app_owner")
STATE_DIR = Path(os.environ.get("GATE_STATE_DIR", Path.cwd() / ".gate"))
MAX_REHEARSAL_AGE = dt.timedelta(hours=float(os.environ.get("GATE_MAX_REHEARSAL_AGE_HOURS", "24")))
LOCK_TIMEOUT = os.environ.get("GATE_LOCK_TIMEOUT", "5s")
STATEMENT_TIMEOUT = os.environ.get("GATE_STATEMENT_TIMEOUT", "10min")

_apply_lock = threading.Lock()


def target_of(url: str) -> str:
    p = conninfo_to_dict(url)
    host = p.get("host") or "localhost"
    port = p.get("port")
    return f"{p.get('dbname') or p.get('user')}@{host}" + (f":{port}" if port and port != "5432" else "")


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


# ---------------------------------------------------------------- ledger

def ledger() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(STATE_DIR / "ledger.db")
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS snapshots (
        snapshot_id TEXT PRIMARY KEY, target TEXT, fingerprint TEXT, dump_sha256 TEXT,
        bytes INTEGER, taken_at TEXT, server_version TEXT);
    CREATE TABLE IF NOT EXISTS rehearsals (
        rehearsal_id TEXT PRIMARY KEY, snapshot_id TEXT, target TEXT, migration_name TEXT,
        migration_sha256 TEXT, verdict TEXT, summary TEXT, reasons TEXT,
        snapshot_fingerprint TEXT, schema_after_fingerprint TEXT, digest TEXT,
        rehearsed_at TEXT, submitted_at TEXT);
    CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, rehearsal_id TEXT, migration_sha256 TEXT, target TEXT,
        status TEXT, detail TEXT, at TEXT);
    """)
    cols = {r["name"] for r in db.execute("PRAGMA table_info(snapshots)")}
    if "server_version" not in cols:
        db.execute("ALTER TABLE snapshots ADD COLUMN server_version TEXT")
    return db


def refuse(reason: str, **extra) -> dict:
    return {"applied": False, "refused": True, "reason": reason, **extra}


# ---------------------------------------------------------------- export_snapshot

def _pg_dump_major(path: str) -> Optional[int]:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.", out)
    return int(m.group(1)) if m else None


def pg_dump_path(server_major: int | None = None) -> str:
    """A pg_dump at least as new as the server. An older one aborts with a version mismatch, and
    the first pg_dump on PATH is often the wrong major (a stray Homebrew keg, an old client
    package), so check rather than trust it."""
    candidates: list[str] = []
    if os.environ.get("PG_DUMP"):
        candidates.append(os.environ["PG_DUMP"])
    found = shutil.which("pg_dump")
    if found:
        candidates.append(found)
    try:
        import pgserver  # bundled PG16 binaries
        candidates.append(str(Path(pgserver.__file__).parent / "pginstall" / "bin" / "pg_dump"))
    except ImportError:
        pass
    if not candidates:
        raise RuntimeError("pg_dump not found; install PostgreSQL client tools or set PG_DUMP")
    if server_major is None:
        return candidates[0]
    seen = []
    for path in candidates:
        major = _pg_dump_major(path)
        seen.append(f"{path} ({major or 'unknown'})")
        if major is None or major >= server_major:
            return path
    raise RuntimeError(
        f"every pg_dump found is older than the server (PostgreSQL {server_major}): {', '.join(seen)}. "
        f"Install postgresql-client-{server_major} or set PG_DUMP to a matching binary.")


def _export_snapshot() -> str:
    with psycopg.connect(READER_URL) as c:
        fp = gatecore.fingerprint(c)
        server_version = str(c.execute("SHOW server_version").fetchone()[0])
    p = subprocess.run(
        [pg_dump_path(int(str(server_version).split(".")[0])),
         "--format=plain", "--schema=public", "--no-owner", "--no-privileges",
         "--no-publications", "--no-subscriptions", "--no-security-labels", "--dbname", READER_URL],
        capture_output=True, timeout=600)
    if p.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {p.stderr.decode(errors='replace')[-1500:]}")
    with psycopg.connect(READER_URL) as c:  # nothing changed shape while we dumped
        if gatecore.fingerprint(c) != fp:
            raise RuntimeError("schema changed during the dump; retry")
    dump = p.stdout
    header = {
        "snapshot_id": "snap_" + secrets.token_hex(4),
        "target_database": target_of(READER_URL),
        "fingerprint": fp,
        "dump_sha256": hashlib.sha256(dump).hexdigest(),
        "bytes": len(dump),
        "taken_at": now().isoformat(),
        "server_version": server_version,
        "format": "pg_dump plain | gzip | base64",
    }
    with ledger() as db:
        db.execute(
            "INSERT INTO snapshots (snapshot_id, target, fingerprint, dump_sha256, bytes, taken_at, "
            "server_version) VALUES (?,?,?,?,?,?,?)",
            (header["snapshot_id"], header["target_database"], fp, header["dump_sha256"],
             len(dump), header["taken_at"], server_version))
    payload = base64.b64encode(gzip.compress(dump, compresslevel=6)).decode()
    return "RELEASE-GATE-SNAPSHOT v1 " + json.dumps(header) + "\n" + payload


def _major(version: str | None) -> str | None:
    """Postgres major from a version string or an `engine` label. Tolerates b'16.15' reprs."""
    if not version:
        return None
    m = re.search(r"(\d+)", str(version).replace("b'", "").replace('b"', ""))
    return m.group(1) if m else None


# ---------------------------------------------------------------- submit_rehearsal

def _submit_rehearsal(report_json: str) -> dict:
    try:
        report = json.loads(report_json)
    except json.JSONDecodeError as e:
        return {"accepted": False, "reason": f"report_json is not valid JSON: {e}"}
    if not isinstance(report, dict) or report.get("report_version") != gatecore.REPORT_VERSION:
        return {"accepted": False, "reason": "unknown report format"}
    if report.get("digest") != gatecore.report_digest(report):
        return {"accepted": False, "reason": "digest mismatch: the report was altered after rehearse.py wrote it. "
                                             "Pass the report file contents verbatim."}
    with ledger() as db:
        snap = db.execute("SELECT * FROM snapshots WHERE snapshot_id = ?", (report.get("snapshot_id"),)).fetchone()
    if snap is None:
        return {"accepted": False, "reason": f"unknown snapshot_id {report.get('snapshot_id')!r}"}
    if (report.get("snapshot_fingerprint") != snap["fingerprint"]
            or report.get("snapshot_dump_sha256") != snap["dump_sha256"]
            or report.get("target_database") != snap["target"]):
        return {"accepted": False, "reason": "report does not match the snapshot the gate issued"}

    # The sandbox engine must be the same Postgres major as production. rehearse.py checks the
    # restored schema fingerprint, but that check lives in the sandbox where it can be edited out,
    # so the gate re-checks here where the agent cannot reach it.
    prod_major, sandbox_major = _major(snap["server_version"]), _major(report.get("engine"))
    if prod_major and sandbox_major and prod_major != sandbox_major:
        return {"accepted": False,
                "reason": (f"rehearsal ran on PostgreSQL {sandbox_major} but production is "
                           f"PostgreSQL {prod_major}; a rehearsal on a different major version does "
                           f"not prove what the migration does to production. Install a matching "
                           f"major in the sandbox and rehearse again."),
                "production_server_version": snap["server_version"],
                "rehearsal_engine": report.get("engine")}

    findings = report.get("findings") or {}
    verdict, reasons = gatecore.compute_verdict(findings)
    summary = gatecore.summarize(findings, verdict)
    if report.get("verdict") != verdict:
        return {"accepted": False, "verdict": verdict,
                "reason": f"report claims {report.get('verdict')} but its findings mean {verdict}"}
    if report.get("summary") != summary:
        return {"accepted": False, "reason": "report summary does not match its findings"}

    rid = "rh_" + secrets.token_hex(4)
    with ledger() as db:
        db.execute("INSERT INTO rehearsals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (rid, snap["snapshot_id"], snap["target"], report.get("migration_name"),
                    report.get("migration_sha256"), verdict, summary, json.dumps(reasons),
                    snap["fingerprint"], report.get("schema_after_fingerprint"), report["digest"],
                    report.get("rehearsed_at"), now().isoformat()))
    return {
        "accepted": True,
        "rehearsal_id": rid,
        "verdict": verdict,
        "reasons": reasons,
        "summary": summary,
        "target_database": snap["target"],
        "migration_name": report.get("migration_name"),
        "migration_sha256": report.get("migration_sha256"),
        "apply": ("refused for BLOCK" if verdict == gatecore.VERDICT_BLOCK else
                  "apply_migration needs accept_review=true" if verdict == gatecore.VERDICT_REVIEW else
                  "eligible for apply_migration (human approval required)"),
    }


# ---------------------------------------------------------------- apply_migration

def _apply_migration(rehearsal_id: str, migration_sql: str, target_database: str,
                     rehearsal_summary: str, accept_review: bool) -> dict:
    with ledger() as db:
        rh = db.execute("SELECT * FROM rehearsals WHERE rehearsal_id = ?", (rehearsal_id,)).fetchone()
    if rh is None:
        return refuse(f"unknown rehearsal_id {rehearsal_id!r}")
    sha = gatecore.sql_sha256(migration_sql)
    if sha != rh["migration_sha256"]:
        return refuse("migration_sql differs from the SQL that was rehearsed (sha256 mismatch)",
                      rehearsed_sha256=rh["migration_sha256"], submitted_sha256=sha)
    target = target_of(MIGRATOR_URL)
    if target_database != rh["target"] or target_database != target:
        return refuse(f"target_database {target_database!r} does not match the rehearsed target {rh['target']!r}")
    if rehearsal_summary != rh["summary"]:
        return refuse("rehearsal_summary does not match the gate's record; quote it verbatim",
                      expected=rh["summary"])
    if rh["verdict"] == gatecore.VERDICT_BLOCK:
        return refuse("rehearsal verdict is BLOCK", summary=rh["summary"])
    if rh["verdict"] == gatecore.VERDICT_REVIEW and not accept_review:
        return refuse("rehearsal verdict is REVIEW; a human must accept the findings (accept_review=true)",
                      summary=rh["summary"])
    age = now() - dt.datetime.fromisoformat(rh["submitted_at"])
    if age > MAX_REHEARSAL_AGE:
        return refuse(f"rehearsal is stale ({age} old, limit {MAX_REHEARSAL_AGE}); rehearse again")
    non_tx = gatecore.non_transactional_statements(migration_sql)
    if non_tx:
        return refuse("migration contains statements that cannot run in a transaction", statements=non_tx)

    with _apply_lock:
        with ledger() as db:
            done = db.execute("SELECT * FROM applications WHERE migration_sha256 = ? AND target = ? "
                              "AND status = 'applied'", (sha, target)).fetchone()
        if done:
            return refuse(f"already applied to {target} at {done['at']} (rehearsal {done['rehearsal_id']})")
        with psycopg.connect(READER_URL) as c:
            current_fp = gatecore.fingerprint(c)
        if current_fp != rh["snapshot_fingerprint"]:
            return refuse("schema drift: production's schema changed since the rehearsed snapshot; "
                          "take a new snapshot and rehearse again")

        results, detail, status = [], {}, "applied"
        conn = psycopg.connect(MIGRATOR_URL, autocommit=False)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
                cur.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                if MIGRATION_ROLE:
                    cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(MIGRATION_ROLE)))
                t0 = dt.datetime.now()
                for i, stmt in enumerate(gatecore.split_statements(migration_sql), 1):
                    cur.execute(stmt)
                    results.append({"statement": i, "rows": cur.rowcount if cur.rowcount >= 0 else None})
                after_fp = gatecore.fingerprint(conn)
                if after_fp != rh["schema_after_fingerprint"]:
                    conn.rollback()
                    status = "rolled_back"
                    detail = {"reason": "post-apply schema differs from the rehearsal result; rolled back",
                              "expected": rh["schema_after_fingerprint"], "got": after_fp}
                else:
                    conn.commit()
                    detail = {"duration_ms": int((dt.datetime.now() - t0).total_seconds() * 1000),
                              "statements": results, "schema_fingerprint": after_fp}
        except psycopg.Error as e:
            conn.rollback()
            status = "failed"
            detail = {"reason": f"database error, rolled back: {str(e).strip()}"}
        finally:
            conn.close()
        with ledger() as db:
            db.execute("INSERT INTO applications (rehearsal_id, migration_sha256, target, status, detail, at) "
                       "VALUES (?,?,?,?,?,?)", (rehearsal_id, sha, target, status, json.dumps(detail),
                                               now().isoformat()))
    if status != "applied":
        return refuse(detail["reason"], status=status, **{k: v for k, v in detail.items() if k != "reason"})
    return {"applied": True, "refused": False, "target_database": target, "rehearsal_id": rehearsal_id,
            "migration_name": rh["migration_name"], "summary": rh["summary"], **detail}


# ---------------------------------------------------------------- migration_status

def _migration_status(migration_sql: Optional[str], migration_sha256: Optional[str]) -> dict:
    sha = gatecore.sql_sha256(migration_sql) if migration_sql else migration_sha256
    target = target_of(MIGRATOR_URL)
    with psycopg.connect(READER_URL) as c:
        current_fp = gatecore.fingerprint(c)
    with ledger() as db:
        if sha:
            rhs = db.execute("SELECT * FROM rehearsals WHERE migration_sha256 = ? ORDER BY rowid DESC",
                             (sha,)).fetchall()
            apps = db.execute("SELECT * FROM applications WHERE migration_sha256 = ? ORDER BY id DESC",
                              (sha,)).fetchall()
        else:
            rhs = db.execute("SELECT * FROM rehearsals ORDER BY rowid DESC LIMIT 10").fetchall()
            apps = db.execute("SELECT * FROM applications ORDER BY id DESC LIMIT 10").fetchall()

    def rh_view(r):
        age = now() - dt.datetime.fromisoformat(r["submitted_at"])
        return {"rehearsal_id": r["rehearsal_id"], "migration_name": r["migration_name"],
                "migration_sha256": r["migration_sha256"], "verdict": r["verdict"], "summary": r["summary"],
                "submitted_at": r["submitted_at"], "stale": age > MAX_REHEARSAL_AGE,
                "snapshot_matches_prod": r["snapshot_fingerprint"] == current_fp}

    out = {"target_database": target, "prod_schema_fingerprint": current_fp,
           "rehearsals": [rh_view(r) for r in rhs],
           "applications": [{"rehearsal_id": a["rehearsal_id"], "migration_sha256": a["migration_sha256"],
                             "status": a["status"], "at": a["at"]} for a in apps]}
    if sha:
        applied = any(a["status"] == "applied" for a in apps)
        latest = rhs[0] if rhs else None
        if applied:
            state = "applied"
        elif latest is None:
            state = "not_rehearsed"
        elif latest["verdict"] == gatecore.VERDICT_BLOCK:
            state = "blocked"
        elif (now() - dt.datetime.fromisoformat(latest["submitted_at"])) > MAX_REHEARSAL_AGE \
                or latest["snapshot_fingerprint"] != current_fp:
            state = "needs_rehearsal"
        else:
            state = "ready_safe" if latest["verdict"] == gatecore.VERDICT_SAFE else "ready_needs_review"
        out.update(migration_sha256=sha, state=state)
    return out


# ---------------------------------------------------------------- MCP wiring

mcp = MCPServer(
    "db-gate",
    instructions=(
        "Gatekeeper for one production Postgres database. Never has the agent hold credentials. "
        "Flow: export_snapshot -> rehearse in the sandbox -> submit_rehearsal -> (human-approved) apply_migration."),
)


@mcp.tool(
    annotations=ToolAnnotations(title="Export production snapshot", read_only_hint=True, destructive_hint=False,
                                idempotent_hint=False, open_world_hint=False),
    structured_output=False,
)
async def export_snapshot() -> str:
    """Take a read-only pg_dump of production's public schema (schema + rows) for rehearsal.

    Returns one text blob: a header line `RELEASE-GATE-SNAPSHOT v1 {json}` (snapshot_id, target_database,
    schema fingerprint, dump sha256) followed by the gzipped+base64 dump. It is large: the harness saves
    it to a file in the sandbox. Pass that file path to `rehearse.py run --snapshot`. Never paste it."""
    return await anyio.to_thread.run_sync(_export_snapshot)


@mcp.tool(
    annotations=ToolAnnotations(title="Submit rehearsal report", read_only_hint=False, destructive_hint=False,
                                idempotent_hint=False, open_world_hint=False),
)
async def submit_rehearsal(report_json: str) -> dict:
    """Record a rehearsal. `report_json` must be the exact contents of the report file rehearse.py wrote.

    The gate checks the digest, matches the report to a snapshot it issued, and recomputes the verdict
    (SAFE / REVIEW / BLOCK) and summary from the raw findings. Returns rehearsal_id, verdict, summary and
    target_database: the values apply_migration needs."""
    return await anyio.to_thread.run_sync(_submit_rehearsal, report_json)


@mcp.tool(
    annotations=ToolAnnotations(title="Apply migration to PRODUCTION", read_only_hint=False, destructive_hint=True,
                                idempotent_hint=False, open_world_hint=False),
)
async def apply_migration(rehearsal_id: str, migration_sql: str, target_database: str,
                          rehearsal_summary: str, accept_review: bool = False) -> dict:
    """Apply a rehearsed migration to PRODUCTION in one transaction. Requires human approval.

    - migration_sql: exactly the SQL that was rehearsed (sha256 must match).
    - target_database: the target_database returned by submit_rehearsal.
    - rehearsal_summary: the summary returned by submit_rehearsal, verbatim.
    - accept_review: true only if a human explicitly accepted a REVIEW verdict's findings.

    Refuses BLOCK verdicts, stale (>24h) rehearsals, schema drift since the snapshot, repeats, and
    non-transactional statements. If the resulting schema differs from the rehearsal, it rolls back."""
    return await anyio.to_thread.run_sync(_apply_migration, rehearsal_id, migration_sql, target_database,
                                          rehearsal_summary, accept_review)


@mcp.tool(
    annotations=ToolAnnotations(title="Migration status", read_only_hint=True, destructive_hint=False,
                                idempotent_hint=True, open_world_hint=False),
)
async def migration_status(migration_sql: Optional[str] = None, migration_sha256: Optional[str] = None) -> dict:
    """Where a migration stands: not_rehearsed / blocked / needs_rehearsal / ready_needs_review / ready_safe /
    applied, plus its rehearsals and applications. With no arguments, lists recent activity."""
    return await anyio.to_thread.run_sync(_migration_status, migration_sql, migration_sha256)


class BearerAuth:
    """Pure ASGI middleware: every HTTP request needs `Authorization: Bearer <GATE_TOKEN>`."""

    def __init__(self, app, token: str):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                body = b'{"error":"unauthorized"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def main():
    missing = [k for k, v in {"READER_URL": READER_URL, "MIGRATOR_URL": MIGRATOR_URL,
                              "GATE_TOKEN": GATE_TOKEN}.items() if not v]
    if missing:
        sys.exit(f"db-gate: missing config {', '.join(missing)} (copy .env.example to .env)")
    if len(GATE_TOKEN) < 16:
        sys.exit("db-gate: GATE_TOKEN is too short; use `openssl rand -hex 24`")
    if target_of(READER_URL) != target_of(MIGRATOR_URL):
        sys.exit(f"db-gate: READER_URL ({target_of(READER_URL)}) and MIGRATOR_URL ({target_of(MIGRATOR_URL)}) "
                 "must point at the same database")
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
    print(f"db-gate: target {target_of(MIGRATOR_URL)}; MCP at http://{HOST}:{PORT}/mcp", flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
