"""Rules shared by verify_build.py (in the sandbox) and ship-gate (on the host).

Same contract as gatecore: the sandbox writes a report, the gate recomputes the verdict from the
raw findings rather than trusting the report's own label, and a digest catches edits in transit.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

REPORT_VERSION = 1
VERDICT_PASS, VERDICT_FAIL = "PASS", "FAIL"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def verification_digest(report: dict[str, Any]) -> str:
    body = {k: v for k, v in report.items() if k != "digest"}
    return sha256_text(canonical_json(body))


def compute_verdict(findings: dict[str, Any]) -> tuple[str, list[str]]:
    """The only place a verification verdict is decided. Returns (verdict, reasons)."""
    reasons: list[str] = []
    tests = findings.get("tests") or {}
    if tests.get("exit_code") is None:
        reasons.append("test suite did not run")
    elif tests.get("exit_code") != 0:
        reasons.append(f"test suite failed (exit {tests['exit_code']})")
    if tests.get("failed"):
        reasons.append(f"{tests['failed']} failing tests")
    if not tests.get("command"):
        reasons.append("no test command recorded")
    return (VERDICT_FAIL, reasons) if reasons else (VERDICT_PASS, [])


def summarize(findings: dict[str, Any], verdict: str) -> str:
    """One deterministic line. publish_release requires it quoted verbatim."""
    tests = findings.get("tests") or {}
    migs = findings.get("migrations") or []
    commits = findings.get("commits") or []
    parts = [f"{verdict}: {len(commits)} commits since {findings.get('from_tag') or 'the start'}"]
    if tests.get("command"):
        parts.append(f"tests {tests.get('passed', 0)} passed / {tests.get('failed', 0)} failed"
                     f" (exit {tests.get('exit_code')})")
    parts.append(f"{len(migs)} migration(s) in scope" if migs else "no migrations in scope")
    return "; ".join(parts)
