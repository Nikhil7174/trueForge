"""Deterministic synthetic Medicare ACO data: 5000 beneficiaries, 15000 claims, their ICD-10-CM codes.

Same generator (same seed, same output) as the migration-rehearsal demo
(`agents/migration_rehearsal/demo/seed.py`), without the Postgres dependency so it runs anywhere.
`tests/ticket_resolver_demo_check.py` asserts the two stay identical.

Everything is generated. No real PHI.
"""
from __future__ import annotations

import datetime as dt
import random

SEED = 20260926
N_PATIENTS = 5000
N_DUPLICATES = 37          # beneficiaries who arrive from both the CCLF and the EHR feed
N_CLAIMS = 15000

MBI_ALPHA = "ACDEFGHJKMNPQRTUVWXY"          # CMS excludes S, L, O, I, B, Z
MBI_ALNUM = MBI_ALPHA + "0123456789"

FIRST = ["James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda", "David", "Elizabeth",
         "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica", "Thomas", "Sarah", "Charles", "Karen",
         "Christopher", "Lisa", "Daniel", "Nancy", "Matthew", "Betty", "Anthony", "Margaret", "Mark", "Sandra",
         "Donald", "Ashley", "Steven", "Dorothy", "Paul", "Kimberly", "Andrew", "Emily", "Joshua", "Donna",
         "Kenneth", "Michelle", "Kevin", "Carol", "Brian", "Amanda", "George", "Melissa", "Edward", "Deborah",
         "Ronald", "Stephanie", "Timothy", "Rebecca", "Jason", "Sharon", "Jeffrey", "Laura", "Ryan", "Cynthia"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
        "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
        "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson",
        "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
        "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell", "Carter", "Roberts"]

DX_SHORT = [("I10", 14), ("E11.9", 10), ("E78.5", 9), ("Z79.4", 5), ("N18.30", 5), ("I25.10", 5),
            ("J44.9", 5), ("I50.9", 4), ("E66.9", 4), ("F32.9", 3), ("M17.11", 3), ("G47.33", 3),
            ("K21.9", 3), ("E03.9", 3), ("I48.91", 3), ("R53.83", 2), ("N40.0", 2), ("D64.9", 2)]
DX_LONG = [("E11.649", 3), ("S72.001A", 2), ("C50.911", 2), ("J45.909", 2), ("E11.319", 1)]
P_LONG = 0.1035
DX_PER_CLAIM = [(1, 20), (2, 30), (3, 25), (4, 15), (5, 10)]
CLAIM_TYPES = [("professional", 70), ("outpatient", 20), ("inpatient", 6), ("snf", 4)]


def weighted(rng, pairs):
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def make_mbi(rng):
    d = lambda: str(rng.randint(0, 9))  # noqa: E731
    a = lambda: rng.choice(MBI_ALPHA)   # noqa: E731
    an = lambda: rng.choice(MBI_ALNUM)  # noqa: E731
    return str(rng.randint(1, 9)) + a() + an() + d() + a() + an() + d() + a() + a() + d() + d()


def ehr_format(mbi):
    return f"{mbi[:4]}-{mbi[4:7]}-{mbi[7:]}".lower()


def make_npi(rng):
    # 10 digits with a valid Luhn check digit over the 80840 prefix (CMS NPI rule)
    base = "1" + "".join(str(rng.randint(0, 9)) for _ in range(8))
    digits = [int(x) for x in "80840" + base]
    total = 0
    for i, v in enumerate(reversed(digits)):
        if i % 2 == 0:
            v *= 2
            v = v - 9 if v > 9 else v
        total += v
    return base + str((10 - total % 10) % 10)


def generate():
    """Return (patients, claims, diagnoses) rows. Patient ids are 1-based positions in `patients`."""
    rng = random.Random(SEED)
    npis = [make_npi(rng) for _ in range(60)]
    mbis = {"1EG4TE5MK72"}
    while len(mbis) < N_PATIENTS - N_DUPLICATES:
        mbis.add(make_mbi(rng))
    mbis = sorted(mbis)
    rng.shuffle(mbis)

    people = []
    for m in mbis:
        people.append({"mbi": m, "first": rng.choice(FIRST), "last": rng.choice(LAST),
                       "dob": dt.date(1935, 1, 1) + dt.timedelta(days=rng.randint(0, 365 * 25)),
                       "npi": rng.choice(npis)})
    rows = []
    for p in people:  # most arrive via CCLF, some via EHR only (dashed, lowercase)
        feed = "EHR" if rng.random() < 0.18 else "CCLF"
        rows.append((p["mbi"] if feed == "CCLF" else ehr_format(p["mbi"]), p["first"], p["last"], p["dob"],
                     feed, p["npi"]))
    # 37 CCLF beneficiaries also arrive from the EHR feed as separate rows
    cclf_idx = [i for i, r in enumerate(rows) if r[4] == "CCLF" and r[0] != "1EG4TE5MK72"]
    dup_idx = [next(i for i, r in enumerate(rows) if r[0] == "1EG4TE5MK72")] + rng.sample(cclf_idx, N_DUPLICATES - 1)
    for i in dup_idx:
        mbi, first, last, dob, _, npi = rows[i]
        rows.append((ehr_format(mbi), first.upper(), last.upper(), dob, "EHR", rng.choice(npis)))
    rng.shuffle(rows)

    claims, dx = [], []
    per_patient = [1] * N_PATIENTS
    for _ in range(N_CLAIMS - N_PATIENTS):
        per_patient[rng.randrange(N_PATIENTS)] += 1
    claim_id = 0
    for pid, k in enumerate(per_patient, 1):
        for _ in range(k):
            claim_id += 1
            ctype = weighted(rng, CLAIM_TYPES)
            amount = {"professional": (40, 400), "outpatient": (150, 2500),
                      "inpatient": (4000, 42000), "snf": (2000, 18000)}[ctype]
            claims.append((pid, f"CLM{claim_id:010d}", rng.choice(npis), ctype,
                           dt.date(2025, 1, 1) + dt.timedelta(days=rng.randint(0, 364)),
                           round(rng.uniform(*amount), 2)))
            for seq in range(1, weighted(rng, DX_PER_CLAIM) + 1):
                code = weighted(rng, DX_LONG) if rng.random() < P_LONG else weighted(rng, DX_SHORT)
                dx.append((claim_id, seq, code))
    return rows, claims, dx
