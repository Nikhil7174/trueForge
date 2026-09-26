"""Service tests. Run from the project root: python -m unittest discover -s tests"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aco_api import db, service  # noqa: E402


class ServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = db.connect()
        cls.cclf = cls.conn.execute(
            "SELECT * FROM patients WHERE source_feed = 'CCLF' ORDER BY id LIMIT 1"
        ).fetchone()

    def test_find_patient_by_cclf_mbi(self):
        patient = service.find_patient(self.conn, self.cclf["mbi"])
        self.assertEqual(patient["id"], self.cclf["id"])

    def test_find_patient_ignores_case_and_whitespace(self):
        patient = service.find_patient(self.conn, f"  {self.cclf['mbi'].lower()} ")
        self.assertEqual(patient["id"], self.cclf["id"])

    def test_unknown_mbi(self):
        self.assertIsNone(service.find_patient(self.conn, "9ZZ9ZZ9ZZ99"))
        self.assertIsNone(service.claim_total(self.conn, "9ZZ9ZZ9ZZ99"))

    def test_claim_total_matches_claims(self):
        mbi = self.cclf["mbi"]
        claims = service.patient_claims(self.conn, mbi)
        self.assertTrue(claims)
        self.assertEqual(service.claim_total(self.conn, mbi), sum(Decimal(c["paid_amount"]) for c in claims))

    def test_claim_total_by_year(self):
        mbi = self.cclf["mbi"]
        self.assertEqual(service.claim_total(self.conn, mbi, 2025), service.claim_total(self.conn, mbi))
        self.assertEqual(service.claim_total(self.conn, mbi, 2019), Decimal("0.00"))

    def test_attribution_list_is_scoped_to_npi(self):
        npi = self.cclf["attributed_npi"]
        roster = service.attribution_list(self.conn, npi)
        self.assertTrue(roster)
        self.assertTrue(all(p["attributed_npi"] == npi for p in roster))


if __name__ == "__main__":
    unittest.main()
