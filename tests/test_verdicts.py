"""Unit tests for the rules both gates rest on. No database, no network, no sandbox.

The e2e suites prove the gates refuse things over MCP; these prove the verdict functions
themselves, which is what those refusals are computed from. They are also the suite
release-captain's verify_build.py runs, so they must stay infra-free.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "migration-rehearsal" / "scripts"))
sys.path.insert(0, str(ROOT / "skills" / "release-captain" / "scripts"))

import gatecore  # noqa: E402
import shipcore  # noqa: E402


# ---------------------------------------------------------------- migration identity

def test_sql_sha256_ignores_line_endings_and_surrounding_space():
    sql = "ALTER TABLE t ADD COLUMN c int;"
    assert gatecore.sql_sha256(sql) == gatecore.sql_sha256(f"\r\n  {sql}\r\n  ")


def test_sql_sha256_is_sensitive_to_the_statement_itself():
    assert gatecore.sql_sha256("DROP TABLE t;") != gatecore.sql_sha256("DROP TABLE u;")


def test_report_digest_changes_when_any_field_changes():
    report = {"verdict": "SAFE", "findings": {"tables": {}}, "digest": "ignored"}
    before = gatecore.report_digest(report)
    report["verdict"] = "BLOCK"
    assert gatecore.report_digest(report) != before


# ---------------------------------------------------------------- migration verdicts

def findings(**over):
    base = {"migration": {"ok": True}, "non_transactional": [], "tables": {}, "locks": {}}
    return {**base, **over}


def test_clean_schema_change_is_safe():
    verdict, reasons = gatecore.compute_verdict(findings(
        tables={"patients": {"status": "changed", "rows_before": 10, "rows_after": 10,
                             "columns_added": {"mbi_normalized": 10}}}))
    assert (verdict, reasons) == (gatecore.VERDICT_SAFE, [])


def test_a_failed_migration_blocks():
    verdict, reasons = gatecore.compute_verdict(findings(migration={"ok": False}))
    assert verdict == gatecore.VERDICT_BLOCK and "migration failed" in reasons


def test_deleted_rows_block():
    verdict, reasons = gatecore.compute_verdict(findings(
        tables={"patients": {"status": "changed", "rows_deleted": 37}}))
    assert verdict == gatecore.VERDICT_BLOCK
    assert any("37 rows deleted" in r for r in reasons)


def test_dropping_a_column_that_holds_data_blocks():
    verdict, reasons = gatecore.compute_verdict(findings(
        tables={"claims": {"status": "changed", "columns_dropped": {"note": 120}}}))
    assert verdict == gatecore.VERDICT_BLOCK
    assert any("note" in r and "120" in r for r in reasons)


def test_dropping_an_empty_column_does_not_block():
    verdict, _ = gatecore.compute_verdict(findings(
        tables={"claims": {"status": "changed", "columns_dropped": {"unused": 0}}}))
    assert verdict == gatecore.VERDICT_SAFE


def test_rewritten_values_need_review():
    verdict, reasons = gatecore.compute_verdict(findings(
        tables={"diagnoses": {"status": "changed", "columns_changed": {"icd10_code": 4159}}}))
    assert verdict == gatecore.VERDICT_REVIEW
    assert any("4159" in r for r in reasons)


def test_a_long_write_lock_needs_review():
    verdict, reasons = gatecore.compute_verdict(findings(
        locks={"max_write_lock_ms": gatecore.LOCK_REVIEW_MS + 1}))
    assert verdict == gatecore.VERDICT_REVIEW and any("write lock" in r for r in reasons)


def test_non_transactional_statements_block():
    verdict, reasons = gatecore.compute_verdict(findings(non_transactional=["CREATE INDEX CONCURRENTLY"]))
    assert verdict == gatecore.VERDICT_BLOCK


def test_block_outranks_review():
    verdict, _ = gatecore.compute_verdict(findings(
        tables={"patients": {"status": "changed", "rows_deleted": 1,
                             "columns_changed": {"mbi": 5}}}))
    assert verdict == gatecore.VERDICT_BLOCK


@pytest.mark.parametrize("sql,flagged", [
    ("CREATE INDEX CONCURRENTLY idx ON t (c);", True),
    ("VACUUM FULL t;", True),
    ("CREATE INDEX idx ON t (c);", False),
    ("-- CREATE INDEX CONCURRENTLY in a comment\nSELECT 1;", False),
])
def test_non_transactional_detection(sql, flagged):
    assert bool(gatecore.non_transactional_statements(sql)) is flagged


def test_summary_is_deterministic():
    f = findings(tables={"diagnoses": {"status": "changed", "columns_changed": {"icd10_code": 4159}}})
    verdict, _ = gatecore.compute_verdict(f)
    assert gatecore.summarize(f, verdict) == gatecore.summarize(f, verdict)


# ---------------------------------------------------------------- release verdicts

def build(**over):
    tests = {"command": "python3 -m pytest -q", "exit_code": 0, "passed": 12, "failed": 0}
    return {"from_tag": "v0.1.0", "commits": [], "migrations": [], "tests": {**tests, **over}}


def test_a_green_suite_passes():
    verdict, reasons = shipcore.compute_verdict(build())
    assert (verdict, reasons) == (shipcore.VERDICT_PASS, [])


def test_a_nonzero_exit_fails():
    verdict, reasons = shipcore.compute_verdict(build(exit_code=1))
    assert verdict == shipcore.VERDICT_FAIL and any("exit 1" in r for r in reasons)


def test_failing_counts_fail_even_on_a_zero_exit():
    verdict, reasons = shipcore.compute_verdict(build(failed=3))
    assert verdict == shipcore.VERDICT_FAIL and any("3 failing" in r for r in reasons)


def test_a_suite_that_never_ran_fails():
    verdict, reasons = shipcore.compute_verdict(build(exit_code=None))
    assert verdict == shipcore.VERDICT_FAIL and any("did not run" in r for r in reasons)


def test_a_suite_that_collected_nothing_fails_and_says_so():
    """pytest exit 5. A release nothing was verified against must not look like a passing one."""
    verdict, reasons = shipcore.compute_verdict(build(exit_code=shipcore.NO_TESTS_COLLECTED,
                                                     passed=0))
    assert verdict == shipcore.VERDICT_FAIL
    assert any("collected no tests" in r for r in reasons)
    assert not any("failed (exit" in r for r in reasons)   # not reported as a failing suite


def test_a_clean_exit_with_no_passing_tests_fails():
    verdict, reasons = shipcore.compute_verdict(build(exit_code=0, passed=0))
    assert verdict == shipcore.VERDICT_FAIL and any("no passing tests" in r for r in reasons)


def test_no_test_command_fails():
    verdict, reasons = shipcore.compute_verdict(build(command=None))
    assert verdict == shipcore.VERDICT_FAIL and any("no test command" in r for r in reasons)


def test_verification_digest_covers_every_field_but_itself():
    report = {"repo": "a/b", "verdict": "PASS", "digest": "ignored"}
    before = shipcore.verification_digest(report)
    report["digest"] = "something else"
    assert shipcore.verification_digest(report) == before
    report["repo"] = "a/c"
    assert shipcore.verification_digest(report) != before
