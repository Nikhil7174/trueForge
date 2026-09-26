#!/usr/bin/env python3
"""Checks the ticket-resolver demo codebase (aco-api) and its three bug scenarios. No TrueForge or Linear needed.

  - aco-api's synthetic data is identical to the migration-rehearsal demo's (skipped without psycopg)
  - aco-api's own test suite passes
  - each scenario behaves as designed: one real bug with a code fix, one real data problem, one non-bug

Run from the repo root: `python tests/ticket_resolver_demo_check.py`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "src" / "trueforge_hackathon" / "agents" / "ticket_resolver" / "demo"
ACO_API = DEMO / "aco-api"
sys.path.insert(0, str(DEMO))

os.environ["ACO_DB"] = str(Path(tempfile.mkdtemp(prefix="aco-check-")) / "aco.db")

import scenarios as sc  # noqa: E402
from aco_api import db, service, synthetic  # noqa: E402

failures = 0


def check(name: str, ok: bool, info=None):
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({info})" if info is not None and not ok else ""))


def cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "aco_api", *args], cwd=ACO_API, capture_output=True, text=True,
                          env={**os.environ})


# --- data -------------------------------------------------------------------------------------------------------
try:
    from trueforge_hackathon.agents.migration_rehearsal.demo import seed as migration_seed
except ImportError as exc:  # psycopg is an optional extra
    print(f"SKIP  parity with migration-rehearsal generator ({exc})")
else:
    check("synthetic data identical to migration-rehearsal generator", synthetic.generate() == migration_seed.generate())

conn = db.connect()
count = service.attributed_count(conn)
check("5,000 patient rows", count == synthetic.N_PATIENTS, count)
n_claims = conn.execute("SELECT count(*) FROM claims").fetchone()[0]
check("15,000 claims", n_claims == synthetic.N_CLAIMS, n_claims)

# --- aco-api test suite -----------------------------------------------------------------------------------------
suite = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=ACO_API,
                       capture_output=True, text=True, env={**os.environ})
check("aco-api unit tests pass", suite.returncode == 0, suite.stderr[-800:])

s1, s2, s3 = sc.scenarios(conn)

# --- 1. mbi-lookup: reproducible, fixable in code ------------------------------------------------------------------
check("mbi-lookup: stored in EHR form", s1["stored_mbi"] != s1["mbi"] and "-" in s1["stored_mbi"], s1)
check("mbi-lookup: bug reproduces (CCLF-style MBI not found)", service.find_patient(conn, s1["mbi"]) is None)
check("mbi-lookup: dashed upper-case MBI not found either", service.find_patient(conn, synthetic.ehr_format(s1["mbi"]).upper()) is None)
check("mbi-lookup: CLI exits non-zero", cli("patient", s1["mbi"]).returncode == 1)
check("mbi-lookup: row exists but no spelling of the MBI finds it", service.find_patient(conn, s1["stored_mbi"]) is None
      and conn.execute("SELECT 1 FROM patients WHERE mbi = ?", (s1["stored_mbi"],)).fetchone() is not None)

# --- 2. duplicate-member: reproducible, needs a data decision ----------------------------------------------------
recs = s2["records"]
check("duplicate-member: two patient rows", len(recs) == 2, recs)
check("duplicate-member: one CCLF and one EHR row", sorted(r["source_feed"] for r in recs) == ["CCLF", "EHR"], recs)
check("duplicate-member: different attributed PCPs", recs[0]["attributed_npi"] != recs[1]["attributed_npi"], recs)
check("duplicate-member: claims on both rows", all(r["n_claims"] > 0 for r in recs), recs)
distinct = conn.execute("SELECT count(DISTINCT replace(upper(mbi), '-', '')) FROM patients").fetchone()[0]
check("duplicate-member: 5,000 rows but 4,963 beneficiaries",
      s2["attributed_count"] == 5000 and distinct == s2["cms_roster_count"] == 4963, (s2["attributed_count"], distinct))

# --- 3. doubled-total: not a bug ---------------------------------------------------------------------------------
direct = conn.execute(
    "SELECT sum(c.paid_cents) FROM claims c JOIN patients p ON p.id = c.patient_id WHERE p.mbi = ?", (s3["mbi"],)
).fetchone()[0]
check("doubled-total: service total equals raw SQL sum", Decimal(s3["actual_total"]) == Decimal(direct) / 100,
      (s3["actual_total"], direct))
check("doubled-total: reported total is twice the real one",
      Decimal(s3["reported_total"]) == Decimal(s3["actual_total"]) * 2)
claim_nos = [c["claim_no"] for c in service.patient_claims(conn, s3["mbi"])]
check("doubled-total: no claim counted twice", len(claim_nos) == len(set(claim_nos)) == s3["n_claims"])

print(f"\n{'OK' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
