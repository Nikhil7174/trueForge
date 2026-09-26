#!/usr/bin/env python3
"""Plays the ticket-resolver agent through the three demo tickets with the real sandbox scripts. No TrueForge,
Linear or gate needed: the source bundle is packed here exactly as ticket-gate export_source will pack it.

Checks every outcome rule in skills/ticket-resolver/scripts/ticketcore.py against real runs of aco-api:
FIXED, REPRODUCED_NO_FIX (no patch / patch without a regression test / patch that doesn't fix),
NOT_REPRODUCED, INCOMPLETE, plus tamper, broken-repro and bad-bundle refusals.

Run from the repo root: `python tests/ticket_resolver_repro_check.py`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "ticket-resolver" / "scripts"
DEMO = ROOT / "src" / "trueforge_hackathon" / "agents" / "ticket_resolver" / "demo"
ACO_API = DEMO / "aco-api"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(DEMO))

import ticketcore as tc  # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="tr-check-"))
os.environ["TICKET_WORK_ROOT"] = str(WORK / "tr")
os.environ["ACO_DB"] = str(WORK / "aco.db")  # scenario lookup only; repros use each tree's own .data/

import scenarios as sc  # noqa: E402

failures = 0


def check(name: str, ok: bool, info=None):
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n      {info}" if info is not None and not ok else ""))


def repro(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / "repro.py"), *args], capture_output=True, text=True,
                          env={k: v for k, v in os.environ.items() if k != "ACO_DB"})


def bundle(issue: str, source_id: str) -> Path:
    blob = tc.pack_source(ACO_API, {"source_id": source_id, "issue_id": issue, "project": "aco-api",
                                    "test_command": ["python3", "-m", "unittest", "discover", "-s", "tests"]})
    path = WORK / f"{issue}.source"
    path.write_text(blob)
    return path


def setup(issue: str) -> Path:
    p = repro("setup", "--source", str(bundle(issue, f"src_{issue.lower()}")), "--issue", issue)
    check(f"{issue}: setup", p.returncode == 0, p.stderr or p.stdout)
    return WORK / "tr" / issue


def write_repro(wd: Path, name: str, body: str):
    (wd / "repros" / name).write_text(body)


def run(wd: Path, name: str, note: str = "") -> int:
    p = repro("run", "--workdir", str(wd), "--repro", name, "--note", note)
    return json.loads((wd / "state.json").read_text())["attempts"][-1]["exit_code"] if p.returncode == 0 else -p.returncode


def report(wd: Path) -> tuple[subprocess.CompletedProcess, dict | None]:
    p = repro("report", "--workdir", str(wd))
    path = wd / "report.json"
    return p, (json.loads(path.read_text()) if p.returncode == 0 and path.exists() else None)


LOOKUP = '''import subprocess, sys
p = subprocess.run([sys.executable, "-m", "aco_api", "patient", {mbi!r}], capture_output=True, text=True)
print(p.stdout.strip() or p.stderr.strip())
sys.exit(0 if p.returncode == 0 and {name!r}.split()[-1].lower() in p.stdout.lower() else 1)
'''

s1, s2, s3 = sc.scenarios()

# --- ZYN-1: MBI lookup -> FIXED ---------------------------------------------------------------------------------
wd = setup("ZYN-1")
state = json.loads((wd / "state.json").read_text())
check("ZYN-1: baseline tests recorded (6 passing)", state["baseline_tests"]["passed"] and state["baseline_tests"]["ran"] == 6,
      state["baseline_tests"])
write_repro(wd, "lookup_by_cclf_mbi.py", LOOKUP.format(mbi=s1["mbi"], name=s1["name"]))
write_repro(wd, "lookup_by_dashed_mbi.py", LOOKUP.format(mbi=s1["stored_mbi"].upper(), name=s1["name"]))
check("ZYN-1: customer's MBI reproduces (exit 1)", run(wd, "lookup_by_cclf_mbi.py", "as typed") == 1)
check("ZYN-1: dashed MBI also reproduces", run(wd, "lookup_by_dashed_mbi.py", "EHR spelling, upper case") == 1)

# a patch that changes nothing relevant
(wd / "src" / "README.md").write_text((wd / "src" / "README.md").read_text() + "\n")
p = repro("patch", "--workdir", str(wd))
check("ZYN-1: irrelevant patch is not accepted", "still reproduces" in p.stdout and "REPRODUCED_NO_FIX" in p.stdout, p.stdout)

# the real fix, but no regression test yet
service = wd / "src" / "aco_api" / "service.py"
service.write_text(service.read_text().replace(
    '''    row = conn.execute(
        "SELECT * FROM patients WHERE mbi = ? ORDER BY id LIMIT 1", (mbi.strip().upper(),)
    ).fetchone()''',
    '''    key = mbi.strip().upper().replace("-", "")
    row = conn.execute(
        "SELECT * FROM patients WHERE replace(upper(mbi), '-', '') = ?"
        " ORDER BY source_feed = 'CCLF' DESC, id LIMIT 1", (key,)
    ).fetchone()'''))
p = repro("patch", "--workdir", str(wd))
check("ZYN-1: fix without a regression test is not accepted", "no regression test added" in p.stdout, p.stdout)

tests = wd / "src" / "tests" / "test_service.py"
tests.write_text(tests.read_text().replace(
    "    def test_unknown_mbi(self):",
    '''    def test_find_ehr_patient_by_any_mbi_spelling(self):
        ehr = self.conn.execute("SELECT * FROM patients WHERE source_feed = 'EHR' ORDER BY id LIMIT 1").fetchone()
        plain = ehr["mbi"].replace("-", "").upper()
        for spelling in (plain, ehr["mbi"], ehr["mbi"].upper()):
            self.assertEqual(service.find_patient(self.conn, spelling)["mbi"].replace("-", "").upper(), plain)

    def test_unknown_mbi(self):'''))
p = repro("patch", "--workdir", str(wd))
check("ZYN-1: fix + regression test -> FIXED", "OUTCOME IF REPORTED NOW: FIXED" in p.stdout, p.stdout + p.stderr)
p, rep = report(wd)
check("ZYN-1: report outcome FIXED", rep is not None and rep["outcome"] == tc.FIXED, p.stdout + p.stderr)
if rep:
    check("ZYN-1: digest verifies", rep["digest"] == tc.report_digest(rep))
    check("ZYN-1: gate recomputes the same outcome", tc.compute_outcome(rep)[0] == tc.FIXED)
    check("ZYN-1: summary is deterministic", tc.summarize(rep, rep["outcome"]) == rep["summary"], rep["summary"])
    check("ZYN-1: pristine untouched by the fix", tc.tree_digest(wd / "pristine") == rep["source_tree_sha256"])
    forged = {**rep, "outcome": tc.FIXED, "patch": {**rep["patch"], "tests": {**rep["patch"]["tests"], "ran": 6}}}
    check("ZYN-1: edited report fails the digest", forged["digest"] != tc.report_digest(forged))
    check("ZYN-1: ...and its findings no longer mean FIXED", tc.compute_outcome(forged)[0] == tc.REPRODUCED_NO_FIX)
    print(f"      summary: {rep['summary']}")

# --- ZYN-2: duplicate member -> REPRODUCED_NO_FIX --------------------------------------------------------------
wd = setup("ZYN-2")
write_repro(wd, "one_row_per_beneficiary.py", f'''import sqlite3, subprocess, sys
subprocess.run([sys.executable, "-m", "aco_api", "build"], check=True, capture_output=True)
rows = sqlite3.connect(".data/aco.db").execute(
    "SELECT mbi, source_feed, attributed_npi FROM patients WHERE replace(upper(mbi), '-', '') = ?", ({s2["mbi"]!r},)).fetchall()
print(rows)
sys.exit(0 if len(rows) == 1 else 1)
''')
check("ZYN-2: duplicate reproduces", run(wd, "one_row_per_beneficiary.py", "rows for the customer's MBI") == 1)
p, rep = report(wd)
check("ZYN-2: no patch -> REPRODUCED_NO_FIX", rep is not None and rep["outcome"] == tc.REPRODUCED_NO_FIX, p.stdout + p.stderr)

# --- ZYN-3: doubled total -> NOT_REPRODUCED (and INCOMPLETE first) ---------------------------------------------
wd = setup("ZYN-3")
write_repro(wd, "total_not_doubled.py", f'''import json, subprocess, sys
p = subprocess.run([sys.executable, "-m", "aco_api", "claims-total", {s3["mbi"]!r}, "--year", "2025"],
                   capture_output=True, text=True)
total = json.loads(p.stdout)["total_paid"]
print("service total", total, "customer reported", {s3["reported_total"]!r})
sys.exit(1 if total == {s3["reported_total"]!r} else 0)
''')
check("ZYN-3: customer's total does not reproduce (exit 0)", run(wd, "total_not_doubled.py", "as reported") == 0)
p, rep = report(wd)
check("ZYN-3: one attempt -> INCOMPLETE, report refused", p.returncode == 3 and "INCOMPLETE" in p.stdout, p.stdout)
write_repro(wd, "broken.py", "import sys\nsys.exit(2)\n")
check("ZYN-3: broken repro recorded as exit 2", run(wd, "broken.py") == 2)
p, rep = report(wd)
check("ZYN-3: broken repro doesn't count toward 2 attempts", p.returncode == 3, p.stdout)
write_repro(wd, "no_duplicate_claims.py", f'''import json, subprocess, sys
p = subprocess.run([sys.executable, "-m", "aco_api", "claims", {s3["mbi"]!r}], capture_output=True, text=True)
nos = [c["claim_no"] for c in json.loads(p.stdout)]
print(len(nos), "claims,", len(set(nos)), "distinct")
sys.exit(1 if len(nos) != len(set(nos)) else 0)
''')
check("ZYN-3: no duplicate claims (exit 0)", run(wd, "no_duplicate_claims.py", "claim list for the MBI") == 0)
# tamper: an attempt run after pristine/ was edited doesn't count
shutil.copy(wd / "pristine" / "README.md", WORK / "readme.bak")
(wd / "pristine" / "README.md").write_text("changed\n")
write_repro(wd, "third.py", "import sys\nsys.exit(0)\n")
p = repro("run", "--workdir", str(wd), "--repro", "third.py")
check("ZYN-3: run on edited pristine warns", "won't count" in p.stdout, p.stdout)
shutil.copy(WORK / "readme.bak", wd / "pristine" / "README.md")
p, rep = report(wd)
check("ZYN-3: -> NOT_REPRODUCED", rep is not None and rep["outcome"] == tc.NOT_REPRODUCED, p.stdout + p.stderr)
if rep:
    check("ZYN-3: tampered and broken attempts named in reasons",
          any("did not run on the exported source" in r for r in rep["reasons"])
          and any("exited 2" in r for r in rep["reasons"]), rep["reasons"])
    print(f"      summary: {rep['summary']}")

# --- bundle refusals --------------------------------------------------------------------------------------------
p = repro("setup", "--source", str(bundle("ZYN-9", "src_x")), "--issue", "ZYN-8")
check("setup refuses a bundle exported for another issue", p.returncode != 0 and "exported for ZYN-9" in p.stderr, p.stderr)
path = bundle("ZYN-7", "src_y")
text = path.read_text()
path.write_text(text[:-40] + ("A" * 40 if not text.endswith("A" * 40) else "B" * 40))
p = repro("setup", "--source", str(path), "--issue", "ZYN-7")
check("setup refuses a corrupted bundle", p.returncode != 0 and "sha256" in p.stderr, p.stderr)
p = repro("setup", "--source", str(bundle("ZYN-1", "src_z")), "--issue", "ZYN-1")
check("setup won't overwrite recorded attempts without --force", p.returncode != 0 and "already exists" in p.stderr, p.stderr)

shutil.rmtree(WORK, ignore_errors=True)
print(f"\n{'OK' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
