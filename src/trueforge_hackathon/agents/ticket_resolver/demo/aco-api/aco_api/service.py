"""Care-team lookups over the ACO beneficiary roster and claims."""
from __future__ import annotations

import sqlite3
from decimal import Decimal

CENT = Decimal("0.01")


def _patient(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "mbi": row["mbi"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "birth_date": row["birth_date"],
        "source_feed": row["source_feed"],
        "attributed_npi": row["attributed_npi"],
    }


def find_patient(conn: sqlite3.Connection, mbi: str) -> dict | None:
    """Look up a beneficiary by MBI as typed by the care team."""
    row = conn.execute(
        "SELECT * FROM patients WHERE mbi = ? ORDER BY id LIMIT 1", (mbi.strip().upper(),)
    ).fetchone()
    return _patient(row) if row else None


def patient_claims(conn: sqlite3.Connection, mbi: str, year: int | None = None) -> list[dict]:
    patient = find_patient(conn, mbi)
    if patient is None:
        return []
    query = "SELECT * FROM claims WHERE patient_id = ?"
    params: list = [patient["id"]]
    if year is not None:
        query += " AND substr(service_date, 1, 4) = ?"
        params.append(str(year))
    rows = conn.execute(query + " ORDER BY service_date, id", params).fetchall()
    return [
        {
            "claim_no": r["claim_no"],
            "claim_type": r["claim_type"],
            "service_date": r["service_date"],
            "rendering_npi": r["rendering_npi"],
            "paid_amount": str((Decimal(r["paid_cents"]) / 100).quantize(CENT)),
        }
        for r in rows
    ]


def claim_total(conn: sqlite3.Connection, mbi: str, year: int | None = None) -> Decimal | None:
    """Total paid across a beneficiary's claims, or None if the MBI is unknown."""
    if find_patient(conn, mbi) is None:
        return None
    return sum((Decimal(c["paid_amount"]) for c in patient_claims(conn, mbi, year)), Decimal("0.00"))


def attribution_list(conn: sqlite3.Connection, npi: str) -> list[dict]:
    """Beneficiaries attributed to one primary-care NPI."""
    rows = conn.execute(
        "SELECT * FROM patients WHERE attributed_npi = ? ORDER BY last_name, first_name, id", (npi,)
    ).fetchall()
    return [_patient(r) for r in rows]


def attributed_count(conn: sqlite3.Connection) -> int:
    """Number of beneficiaries attributed to the ACO."""
    return conn.execute("SELECT count(*) FROM patients").fetchone()[0]
