"""SQLite store for the ACO API, built from the synthetic generator on first use."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from aco_api.synthetic import generate

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = PROJECT_ROOT / ".data" / "aco.db"

SCHEMA = """
CREATE TABLE patients (
    id              INTEGER PRIMARY KEY,
    mbi             TEXT NOT NULL,      -- as received: CCLF sends 1EG4TE5MK72, EHR sends 1eg4-te5-mk72
    first_name      TEXT NOT NULL,
    last_name       TEXT NOT NULL,
    birth_date      TEXT NOT NULL,
    source_feed     TEXT NOT NULL CHECK (source_feed IN ('CCLF', 'EHR')),
    attributed_npi  TEXT NOT NULL
);
CREATE INDEX patients_mbi_idx ON patients (mbi);

CREATE TABLE claims (
    id            INTEGER PRIMARY KEY,
    patient_id    INTEGER NOT NULL REFERENCES patients(id),
    claim_no      TEXT NOT NULL UNIQUE,
    rendering_npi TEXT NOT NULL,
    claim_type    TEXT NOT NULL,
    service_date  TEXT NOT NULL,
    paid_cents    INTEGER NOT NULL
);
CREATE INDEX claims_patient_id_idx ON claims (patient_id);

CREATE TABLE diagnoses (
    id          INTEGER PRIMARY KEY,
    claim_id    INTEGER NOT NULL REFERENCES claims(id),
    seq         INTEGER NOT NULL,
    icd10_code  TEXT NOT NULL
);
CREATE INDEX diagnoses_claim_id_idx ON diagnoses (claim_id);
"""


def db_path() -> Path:
    return Path(os.environ.get("ACO_DB", DEFAULT_PATH))


def build(path: Path | None = None) -> Path:
    path = Path(path or db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    patients, claims, dx = generate()
    conn = sqlite3.connect(tmp)
    with conn:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO patients (id, mbi, first_name, last_name, birth_date, source_feed, attributed_npi)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(i, mbi, first, last, dob.isoformat(), feed, npi)
             for i, (mbi, first, last, dob, feed, npi) in enumerate(patients, 1)],
        )
        conn.executemany(
            "INSERT INTO claims (id, patient_id, claim_no, rendering_npi, claim_type, service_date, paid_cents)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(i, pid, no, npi, ctype, day.isoformat(), round(amount * 100))
             for i, (pid, no, npi, ctype, day, amount) in enumerate(claims, 1)],
        )
        conn.executemany("INSERT INTO diagnoses (claim_id, seq, icd10_code) VALUES (?, ?, ?)", dx)
    conn.close()
    tmp.replace(path)
    return path


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or db_path())
    if not path.exists():
        build(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn
