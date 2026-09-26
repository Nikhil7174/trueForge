#!/usr/bin/env python3
"""Guard test for ticket-gate. No TrueForge and no writes to a real Linear workspace.

Part 1 calls the gate's tool functions directly, with Linear replaced by an in-memory fake, and plays the agent
with the real sandbox scripts (skills/ticket-resolver/scripts/repro.py) against the real aco-api code. It checks
every refusal: scope, tampered reports, forged outcomes, patches that don't match, reply content rules, changed
tickets, stale attempts, superseded / declined / already-sent drafts.

Part 2 starts the real `tf-ticket-gate` server and checks bearer auth and the MCP tool list over HTTP.

Run from the repo root: `python tests/ticket_gate_check.py`.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "ticket-resolver" / "scripts"
DEMO = ROOT / "src" / "trueforge_hackathon" / "agents" / "ticket_resolver" / "demo"
WORK = Path(tempfile.mkdtemp(prefix="tg-check-"))
TOKEN = secrets.token_hex(24)
FAKE_KEY = "lin_api_" + secrets.token_hex(16)

os.environ.update({"TICKET_GATE_STATE_DIR": str(WORK / "gate"), "TICKET_GATE_TOKEN": TOKEN,
                   "LINEAR_API_KEY": FAKE_KEY, "LINEAR_TEAM_KEY": "ZYN", "LINEAR_TRIGGER_LABEL": "Bug",
                   "TICKET_WORK_ROOT": str(WORK / "tr"), "ACO_DB": str(WORK / "aco.db")})
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(DEMO))
sys.path.insert(0, str(ROOT / "tests"))

import scenarios as sc  # noqa: E402
import ticketcore as tc  # noqa: E402

from trueforge_hackathon.agents.ticket_resolver import gate_server as g  # noqa: E402
from linear_fake import FakeLinear  # noqa: E402

logging.disable(logging.INFO)  # httpx request lines from the MCP client
failures = 0


def check(name: str, ok: bool, info=None):
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n      {info}" if info is not None and not ok else ""))


# ---------------------------------------------------------------- fake Linear

fake = FakeLinear()
fake.api_key = FAKE_KEY
g.linear = fake
s1, s2, s3 = sc.scenarios()
fake.add("ZYN-1", s1["title"], s1["body"])
fake.add("ZYN-2", s2["title"], s2["body"])
fake.add("ZYN-3", s3["title"], s3["body"])
fake.add("ZYN-4", "Add dark mode", "Feature request", labels=("Feature",))
fake.add("ZYN-5", "Bug but skipped", "x", labels=("Bug", "agent-skip"))
fake.add("OPS-1", "Other team bug", "x", team="OPS")


# ---------------------------------------------------------------- agent helpers (real sandbox scripts)

def repro(*args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "ACO_DB"}
    return subprocess.run([sys.executable, str(SCRIPTS / "repro.py"), *args], capture_output=True, text=True, env=env)


def start(ident: str) -> Path:
    blob = g._export_source(ident)
    path = WORK / f"{ident}.source"
    path.write_text(blob)
    p = repro("setup", "--source", str(path), "--issue", ident)
    assert p.returncode == 0, p.stderr
    return WORK / "tr" / ident


def add_repro(wd: Path, name: str, body: str, expect: int):
    (wd / "repros" / name).write_text(body)
    repro("run", "--workdir", str(wd), "--repro", name)
    got = json.loads((wd / "state.json").read_text())["attempts"][-1]["exit_code"]
    assert got == expect, f"{name}: exit {got}, expected {expect}"


def report(wd: Path) -> str:
    p = repro("report", "--workdir", str(wd))
    assert p.returncode == 0, p.stdout + p.stderr
    return (wd / "report.json").read_text()


def redigest(rep: dict) -> str:
    rep = {k: v for k, v in rep.items() if k != "digest"}
    rep["digest"] = tc.report_digest(rep)
    return tc.canonical_json(rep)


LOOKUP = '''import subprocess, sys
p = subprocess.run([sys.executable, "-m", "aco_api", "patient", {mbi!r}], capture_output=True, text=True)
print(p.stdout.strip() or p.stderr.strip())
sys.exit(0 if p.returncode == 0 else 1)
'''

# ================================================================ part 1: gate logic

# --- scope ------------------------------------------------------------------------------------------------------
check("export refuses a ticket without the Bug label", g._export_source("ZYN-4").startswith("REFUSED: ZYN-4 has no Bug"))
check("export refuses a ticket labelled agent-skip", "agent-skip" in g._export_source("ZYN-5"))
check("export refuses another team's ticket", "not ZYN" in g._export_source("OPS-1"))
check("export refuses an unknown ticket", g._export_source("ZYN-404").startswith("REFUSED"))

# --- ZYN-1: FIXED -----------------------------------------------------------------------------------------------
wd = start("ZYN-1")
add_repro(wd, "lookup_cclf.py", LOOKUP.format(mbi=s1["mbi"]), 1)
add_repro(wd, "lookup_dashed.py", LOOKUP.format(mbi=s1["stored_mbi"].upper()), 1)
svc = wd / "src" / "aco_api" / "service.py"
svc.write_text(svc.read_text().replace(
    '"SELECT * FROM patients WHERE mbi = ? ORDER BY id LIMIT 1", (mbi.strip().upper(),)',
    '"SELECT * FROM patients WHERE replace(upper(mbi), \'-\', \'\') = ? ORDER BY source_feed = \'CCLF\' DESC, id LIMIT 1",'
    ' (mbi.strip().upper().replace("-", ""),)'))
tests = wd / "src" / "tests" / "test_service.py"
tests.write_text(tests.read_text().replace("    def test_unknown_mbi(self):", '''    def test_ehr_patient_by_cclf_mbi(self):
        ehr = self.conn.execute("SELECT * FROM patients WHERE source_feed = 'EHR' ORDER BY id LIMIT 1").fetchone()
        self.assertIsNotNone(service.find_patient(self.conn, ehr["mbi"].replace("-", "").upper()))

    def test_unknown_mbi(self):'''))
p = repro("patch", "--workdir", str(wd))
check("ZYN-1: sandbox patch is FIXED", "OUTCOME IF REPORTED NOW: FIXED" in p.stdout, p.stdout + p.stderr)
rep_json = report(wd)
rep = json.loads(rep_json)

r = g._submit_attempt("ZYN-1", tc.canonical_json({**rep, "summary": rep["summary"] + " (all good)"}))
check("submit refuses an edited report (digest)", r.get("refused") and "digest" in r["reason"], r)
r = g._submit_attempt("ZYN-2", rep_json)
check("submit refuses a report filed on another ticket", r.get("refused") and "is for ZYN-1" in r["reason"], r)
r = g._submit_attempt("ZYN-1", redigest({**rep, "source_id": "src_deadbeef"}))
check("submit refuses a source the gate never exported", r.get("refused") and "not exported" in r["reason"], r)
r = g._submit_attempt("ZYN-1", redigest({**rep, "outcome": "NOT_REPRODUCED"}))
check("submit refuses a forged outcome", r.get("refused") and "claims NOT_REPRODUCED" in r["reason"], r)
no_test = {**rep, "patch": {**rep["patch"], "tests": {**rep["patch"]["tests"], "ran": 6}}}
r = g._submit_attempt("ZYN-1", redigest(no_test))
check("submit recomputes: claimed FIXED without a new test", r.get("refused") and "REPRODUCED_NO_FIX" in r["reason"], r)
bad_diff = rep["patch"]["diff"].replace("ORDER BY source_feed", "ORDER BY  source_feed")
swapped = {**rep, "patch": {**rep["patch"], "diff": bad_diff, "diff_sha256": tc.sha256_text(bad_diff)}}
r = g._submit_attempt("ZYN-1", redigest(swapped))
check("submit refuses a patch that isn't the one tested", r.get("refused") and "doesn't give the tree" in r["reason"], r)

r = g._submit_attempt("ZYN-1", rep_json)
check("submit accepts the real report as FIXED", r.get("ok") and r["outcome"] == "FIXED", r)
att1 = r.get("attempt_id")
check("ZYN-1 labelled Repro → Fixed", fake.group_label("ZYN-1", "Repro") == "Fixed")

GOOD_FIXED = (f"Thanks for reporting this. Searching by MBI {s1['mbi']} failed because patients who came to us "
              "through the EHR feed are stored with dashes and lower case, and the search only matched the exact "
              "stored spelling. We changed the search so any spelling of the MBI finds the patient. Until that "
              "change ships, searching with the dashed form will not help either; please ask us to look up the "
              "patient for you.")
bad_drafts = {
    "too short": "Fixed it.",
    "sandbox path": GOOD_FIXED + " See /tmp/tr/ZYN-1/patch.diff.",
    "tool name": GOOD_FIXED + " (verified with repro.py)",
    "status line": "Status: fixed\n" + GOOD_FIXED,
    "stack trace": GOOD_FIXED + '\nTraceback (most recent call last):\n  File "x.py", line 1',
    "another beneficiary's MBI": GOOD_FIXED + f" The same happened for {s3['mbi']}.",
    "a secret": GOOD_FIXED + f" key {FAKE_KEY}",
    "a release claim": GOOD_FIXED + " The fix has been deployed.",
}
for what, body in bad_drafts.items():
    r = g._propose_reply(att1, body)
    check(f"propose refuses a draft with {what}", r.get("refused"), r)
r = g._propose_reply(att1, GOOD_FIXED)
check("propose accepts a good draft", r.get("ok"), r)
draft1 = r.get("draft_id")
check("will_post has the gate's FIXED header and disclosure",
      r.get("will_post", "").startswith("**Status: reproduced and fixed.**") and "reviewed and approved by a person"
      in r.get("will_post", ""), r.get("will_post"))
check("ZYN-1 labelled Agent → Awaiting approval", fake.group_label("ZYN-1", "Agent") == "Awaiting approval")
check("nothing posted before approval", not fake.posted)

r = g._reply_to_customer(draft1, GOOD_FIXED + " ")
check("reply refuses a body that differs from the draft", r.get("refused") and "differs" in r["reason"], r)
r = g._reply_to_customer("dr_00000000", GOOD_FIXED)
check("reply refuses an unknown draft", r.get("refused"), r)
r = g._reply_to_customer(draft1, GOOD_FIXED)
check("reply posts the approved draft", r.get("ok") and len(fake.posted) == 1, r)
check("posted text is exactly will_post", fake.posted and fake.posted[0]["body"].endswith(
    "reviewed and approved by a person before sending._") and GOOD_FIXED in fake.posted[0]["body"])
check("ZYN-1 labelled Agent → Replied", fake.group_label("ZYN-1", "Agent") == "Replied")
check("Repro label kept alongside Agent label", fake.group_label("ZYN-1", "Repro") == "Fixed")
r = g._reply_to_customer(draft1, GOOD_FIXED)
check("reply refuses a second send of the same draft", r.get("refused") and "sent" in r["reason"], r)
r = g._propose_reply(att1, GOOD_FIXED)
check("propose refuses once the attempt was answered", r.get("refused") and "already sent" in r["reason"], r)
with g.ledger() as db:
    fp = db.execute("SELECT ticket_fingerprint FROM attempts WHERE attempt_id = ?", (att1,)).fetchone()[0]
check("the gate's own reply doesn't count as the ticket changing", g.ticket_fingerprint(fake.issue("ZYN-1")) == fp)

# --- ZYN-2: REPRODUCED_NO_FIX, superseded drafts ---------------------------------------------------------------
wd = start("ZYN-2")
add_repro(wd, "one_row.py", f'''import sqlite3, subprocess, sys
subprocess.run([sys.executable, "-m", "aco_api", "build"], check=True, capture_output=True)
n = sqlite3.connect(".data/aco.db").execute(
    "SELECT count(*) FROM patients WHERE replace(upper(mbi), '-', '') = ?", ({s2["mbi"]!r},)).fetchone()[0]
print(n, "rows")
sys.exit(0 if n == 1 else 1)
''', 1)
r = g._submit_attempt("ZYN-2", report(wd))
check("ZYN-2 accepted as REPRODUCED_NO_FIX", r.get("ok") and r["outcome"] == "REPRODUCED_NO_FIX", r)
att2 = r.get("attempt_id")
GOOD_NOFIX = (f"We confirmed that MBI {s2['mbi']} appears twice on the roster, once from the CMS claim feed and once "
              "from the EHR feed, each attributed to a different primary care provider. That is also why the "
              "dashboard total is 37 higher than the CMS roster. Merging the two records needs a decision on which "
              "provider keeps the attribution, so we have handed it to the data team.")
r = g._propose_reply(att2, GOOD_NOFIX.replace("so we have handed", "we have fixed the count and handed"))
check("propose refuses a fix claim on REPRODUCED_NO_FIX", r.get("refused") and "claims a fix" in r["reason"], r)
first = g._propose_reply(att2, GOOD_NOFIX)
second = g._propose_reply(att2, GOOD_NOFIX + " We will update this ticket when that is scheduled.")
check("two proposals both accepted", first.get("ok") and second.get("ok"), (first, second))
r = g._reply_to_customer(first.get("draft_id"), GOOD_NOFIX)
check("reply refuses a superseded draft", r.get("refused") and "superseded" in r["reason"], r)

# --- ZYN-3: NOT_REPRODUCED, decline, ticket changes, staleness ------------------------------------------------
wd = start("ZYN-3")
add_repro(wd, "total.py", f'''import json, subprocess, sys
p = subprocess.run([sys.executable, "-m", "aco_api", "claims-total", {s3["mbi"]!r}, "--year", "2025"],
                   capture_output=True, text=True)
t = json.loads(p.stdout)["total_paid"]
print(t)
sys.exit(1 if t == {s3["reported_total"]!r} else 0)
''', 0)
add_repro(wd, "distinct_claims.py", f'''import json, subprocess, sys
p = subprocess.run([sys.executable, "-m", "aco_api", "claims", {s3["mbi"]!r}], capture_output=True, text=True)
nos = [c["claim_no"] for c in json.loads(p.stdout)]
sys.exit(0 if len(nos) == len(set(nos)) else 1)
''', 0)
r = g._submit_attempt("ZYN-3", report(wd))
check("ZYN-3 accepted as NOT_REPRODUCED", r.get("ok") and r["outcome"] == "NOT_REPRODUCED", r)
att3 = r.get("attempt_id")
check("ZYN-3 labelled Repro → Not reproduced", fake.group_label("ZYN-3", "Repro") == "Not reproduced")
GOOD_NR = (f"We could not reproduce a doubled total. For MBI {s3['mbi']}, the 2025 paid total is "
           f"${float(s3['actual_total']):,.2f} across {s3['n_claims']} claims, and no claim is counted twice. "
           "Could you send a screenshot of where you saw the higher figure, and roughly when?")
r = g._propose_reply(att3, GOOD_NR.replace("We could not reproduce", "We fixed"))
check("propose refuses a fix claim on NOT_REPRODUCED", r.get("refused"), r)
r = g._propose_reply(att3, GOOD_NR)
check("propose accepts the honest not-reproduced draft", r.get("ok"), r)
d3 = r.get("draft_id")
r = g._record_decline(d3, "tone")
check("record_decline marks it declined", r.get("ok") and fake.group_label("ZYN-3", "Agent") == "Declined", r)
r = g._reply_to_customer(d3, GOOD_NR)
check("reply refuses a declined draft", r.get("refused") and "declined" in r["reason"], r)

fake.issues["ZYN-3"]["comments"]["nodes"].append(
    {"id": "cust1", "body": "Update: it was the 2024 total, sorry", "createdAt": "2026-09-26T12:00:00Z", "user": {"id": "x"}})
r = g._propose_reply(att3, GOOD_NR)
check("propose refuses when the customer commented after the attempt", r.get("refused") and "changed" in r["reason"], r)
fake.issues["ZYN-3"]["comments"]["nodes"].pop()

r = g._propose_reply(att3, GOOD_NR)
d3b = r.get("draft_id")
fake.issues["ZYN-3"]["labels"]["nodes"].append({"id": "lbl-agent-skip", "name": "agent-skip", "parent": None})
r = g._reply_to_customer(d3b, GOOD_NR)
check("reply refuses once the ticket is labelled agent-skip", r.get("refused") and "agent-skip" in r["reason"], r)
fake.issues["ZYN-3"]["labels"]["nodes"].pop()

with g.ledger() as db:
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)).replace(microsecond=0).isoformat()
    db.execute("UPDATE attempts SET submitted_at = ? WHERE attempt_id = ?", (old, att3))
r = g._reply_to_customer(d3b, GOOD_NR)
check("reply refuses an attempt older than 24h", r.get("refused") and "older than" in r["reason"], r)
check("still exactly one comment posted in total", len(fake.posted) == 1, fake.posted)

st = g._ticket_status("zyn-1")
check("ticket_status shows source, attempt, sent draft and reply",
      len(st["sources"]) == 1 and len(st["attempts"]) == 1 and st["drafts"][0]["status"] == "sent"
      and len(st["replies"]) == 1, st)

# ================================================================ part 2: the real server over MCP


async def mcp_checks(url: str):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Accept": "application/json, text/event-stream"})
    check("server rejects requests without the bearer token", r.status_code == 401, r.status_code)
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {TOKEN}"})
    async with http, streamable_http_client(url, http_client=http) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            tools = {t.name: t for t in (await s.list_tools()).tools}
            check("server lists the seven tools", set(tools) == {"export_source", "submit_attempt", "request_migration",
                                                                 "propose_reply", "reply_to_customer", "record_decline",
                                                                 "ticket_status"},
                  sorted(tools))
            rt = tools.get("reply_to_customer")
            check("reply_to_customer is marked destructive", rt is not None and rt.annotations.destructive_hint is True)
            check("export_source is marked read-only", tools["export_source"].annotations.read_only_hint is True)
            res = await s.call_tool("ticket_status", {"issue_id": "ZYN-1"})
            body = res.structured_content or json.loads(res.content[0].text)
            check("ticket_status works over MCP (same ledger)", len(body.get("replies", [])) == 1, body)


with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
server = subprocess.Popen([sys.executable, "-m", "trueforge_hackathon", "ticket-gate"],
                          env={**os.environ, "TICKET_GATE_PORT": str(port)}, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)
try:
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.2)
    asyncio.run(mcp_checks(f"http://127.0.0.1:{port}/mcp"))
finally:
    server.terminate()
    out = server.communicate(timeout=10)[0]
    if failures:
        print("server output:\n" + out[-2000:])

print(f"\n{'OK' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
