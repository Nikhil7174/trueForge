"""The three demo bug reports, derived from the synthetic ACO data so every number in a ticket is real.

  mbi-lookup        FIXED               EHR-only beneficiary not found by its CCLF-style MBI
  duplicate-member  REPRODUCED_NO_FIX   1EG4TE5MK72 is two patient rows with different PCPs and split claims
  doubled-total     NOT_REPRODUCED      customer reports a claim total twice the real one

Used by `seed_linear.py` (ticket text) and `tests/ticket_resolver_demo_check.py` (proof each one behaves as designed).
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ACO_API_ROOT = Path(__file__).resolve().parent / "aco-api"
sys.path.insert(0, str(ACO_API_ROOT))

from aco_api import db, service  # noqa: E402
from aco_api.synthetic import N_DUPLICATES, N_PATIENTS  # noqa: E402

DUPLICATE_MBI = "1EG4TE5MK72"


def cclf_form(mbi: str) -> str:
    return mbi.replace("-", "").upper()


def _mbi_lookup(conn) -> dict:
    row = conn.execute(
        """
        SELECT p.*, count(c.id) AS n_claims
        FROM patients p JOIN claims c ON c.patient_id = p.id
        WHERE p.source_feed = 'EHR'
          AND (SELECT count(*) FROM patients q
               WHERE replace(upper(q.mbi), '-', '') = replace(upper(p.mbi), '-', '')) = 1
        GROUP BY p.id HAVING n_claims >= 3
        ORDER BY p.id LIMIT 1
        """
    ).fetchone()
    mbi = cclf_form(row["mbi"])
    return {
        "key": "mbi-lookup",
        "expected_outcome": "FIXED",
        "mbi": mbi,
        "stored_mbi": row["mbi"],
        "name": f"{row['first_name']} {row['last_name']}",
        "birth_date": row["birth_date"],
        "n_claims": row["n_claims"],
        "title": f"Patient search says \"not found\" for MBI {mbi}",
        "body": "\n".join(
            [
                f"Our care coordinator searched for {row['first_name']} {row['last_name']} "
                f"(DOB {row['birth_date']}) by MBI **{mbi}** and got \"patient not found\".",
                "",
                "The beneficiary is definitely on our roster: we saw them in the EHR referral list this week, "
                f"and they have {row['n_claims']} claims this year. This blocks the coordinator from opening "
                "the patient's claim history before the care-gap call.",
                "",
                f"Steps: `python -m aco_api patient {mbi}` (same result in the care-team screen).",
                "Expected: the beneficiary's record. Actual: not found.",
            ]
        ),
    }


def _duplicate_member(conn) -> dict:
    rows = conn.execute(
        """
        SELECT p.*, (SELECT count(*) FROM claims c WHERE c.patient_id = p.id) AS n_claims
        FROM patients p WHERE replace(upper(p.mbi), '-', '') = ? ORDER BY p.id
        """,
        (DUPLICATE_MBI,),
    ).fetchall()
    total = conn.execute("SELECT count(*) FROM patients").fetchone()[0]
    return {
        "key": "duplicate-member",
        "expected_outcome": "REPRODUCED_NO_FIX",
        "mbi": DUPLICATE_MBI,
        "records": [
            {"mbi": r["mbi"], "name": f"{r['first_name']} {r['last_name']}", "source_feed": r["source_feed"],
             "attributed_npi": r["attributed_npi"], "n_claims": r["n_claims"]}
            for r in rows
        ],
        "attributed_count": total,
        "cms_roster_count": N_PATIENTS - N_DUPLICATES,
        "title": f"Beneficiary {DUPLICATE_MBI} listed twice under two PCPs; attributed count is off",
        "body": "\n".join(
            [
                f"{rows[0]['first_name'].title()} {rows[0]['last_name'].title()} (MBI {DUPLICATE_MBI}) shows up on "
                f"two different PCP attribution lists: NPI {rows[0]['attributed_npi']} and NPI "
                f"{rows[1]['attributed_npi']}. Their claims are split between the two entries, so neither PCP "
                "sees the full history.",
                "",
                f"Related: the dashboard says we have **{total:,}** attributed beneficiaries, but the CMS "
                f"assignment roster for this performance year lists **{N_PATIENTS - N_DUPLICATES:,}**.",
                "",
                "Steps: `python -m aco_api attributed-count`, then "
                f"`python -m aco_api attribution {rows[0]['attributed_npi']}` and "
                f"`python -m aco_api attribution {rows[1]['attributed_npi']}`.",
            ]
        ),
    }


def _doubled_total(conn) -> dict:
    row = conn.execute(
        """
        SELECT p.*, count(c.id) AS n_claims
        FROM patients p JOIN claims c ON c.patient_id = p.id
        WHERE p.source_feed = 'CCLF'
          AND (SELECT count(*) FROM patients q
               WHERE replace(upper(q.mbi), '-', '') = p.mbi) = 1
        GROUP BY p.id HAVING n_claims >= 4
        ORDER BY p.id LIMIT 1
        """
    ).fetchone()
    actual = service.claim_total(conn, row["mbi"], 2025)
    reported = (actual * 2).quantize(Decimal("0.01"))
    return {
        "key": "doubled-total",
        "expected_outcome": "NOT_REPRODUCED",
        "mbi": row["mbi"],
        "name": f"{row['first_name']} {row['last_name']}",
        "n_claims": row["n_claims"],
        "actual_total": str(actual),
        "reported_total": str(reported),
        "title": f"2025 claim total doubled for MBI {row['mbi']}",
        "body": "\n".join(
            [
                f"The 2025 paid total for {row['first_name']} {row['last_name']} (MBI {row['mbi']}) shows "
                f"**${reported:,}**. That looks like every claim is counted twice: finance's claim export "
                "has about half that.",
                "",
                f"Steps: `python -m aco_api claims-total {row['mbi']} --year 2025`.",
                "Expected: roughly half. Please check whether claims are being double counted.",
            ]
        ),
    }


def scenarios(conn=None) -> list[dict]:
    conn = conn or db.connect()
    return [_mbi_lookup(conn), _duplicate_member(conn), _doubled_total(conn)]


if __name__ == "__main__":
    import json

    print(json.dumps([{k: v for k, v in s.items() if k != "body"} for s in scenarios()], indent=2))
