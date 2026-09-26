#!/usr/bin/env python3
"""Verify a release candidate inside the sandbox: clone the repo at a ref, run its tests, write a
report that ship-gate can check.

  python verify_build.py run --repo owner/name --ref <sha> --from-tag v1.3.0 \
      [--migrations path.sql=sha256 ...] [--test-command 'pytest -q']
  python verify_build.py run ... --report /tmp/release/report.json

The clone is unauthenticated over HTTPS, so this only works for a public repository - the sandbox
holds no GitHub token by design. The token stays in ship-gate.

Exit codes: 0 report written (PASS or FAIL - a failing suite is a finding, not an error), 2 the
verification itself could not be carried out.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shipcore  # noqa: E402

HOME = Path(os.environ.get("VERIFY_HOME", "/tmp/release"))


def die(msg: str, code: int = 2):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd: list[str] | str, cwd: Path | None = None, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), capture_output=True, text=True,
                          timeout=timeout)


def clone(repo: str, ref: str) -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    work = HOME / "repo"
    if work.exists():
        shutil.rmtree(work)
    url = f"https://github.com/{repo}.git"
    p = run(["git", "clone", "--quiet", url, str(work)])
    if p.returncode != 0:
        die(f"clone failed: {p.stderr[-800:]}")
    p = run(["git", "checkout", "--quiet", ref], cwd=work)
    if p.returncode != 0:
        die(f"checkout {ref} failed: {p.stderr[-400:]}")
    got = run(["git", "rev-parse", "HEAD"], cwd=work).stdout.strip()
    if not got.startswith(ref[:7]):
        die(f"checked out {got} but expected {ref}")
    return work


def detect_test_command(work: Path) -> str | None:
    """Pick the project's own test command. Explicit --test-command always wins over this.

    Only guess pytest when files it would actually collect exist. A `tests/` directory of
    standalone scripts collects nothing, and pytest then exits 5, which looks like a broken
    suite rather than the wrong command."""
    collectable = list(work.glob("test_*.py")) + list(work.glob("*_test.py")) \
        + list(work.glob("tests/test_*.py")) + list(work.glob("tests/*_test.py"))
    if collectable:
        return "python3 -m pytest -q"
    if (work / "package.json").exists():
        try:
            pkg = json.loads((work / "package.json").read_text())
            if "test" in (pkg.get("scripts") or {}):
                return "npm test --silent"
        except json.JSONDecodeError:
            pass
    if (work / "Makefile").exists() and re.search(r"^test:", (work / "Makefile").read_text(), re.M):
        return "make test"
    return None


def parse_counts(output: str) -> dict[str, int]:
    """Best-effort pass/fail counts. The verdict rests on the exit code, not on this."""
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    m = re.search(r"(\d+) passed", output)
    if m:
        counts["passed"] = int(m.group(1))
    m = re.search(r"(\d+) failed", output)
    if m:
        counts["failed"] = int(m.group(1))
    m = re.search(r"(\d+) skipped", output)
    if m:
        counts["skipped"] = int(m.group(1))
    if not any(counts.values()):
        m = re.search(r"Tests:.*?(\d+) passed", output, re.S)
        if m:
            counts["passed"] = int(m.group(1))
    return counts


def cmd_run(args):
    work = clone(args.repo, args.ref)
    install_log = ""
    if args.install:
        p = run(args.install, cwd=work)
        install_log = (p.stdout + p.stderr)[-1500:]
        if p.returncode != 0:
            die(f"install command failed:\n{install_log}")

    command = args.test_command or detect_test_command(work)
    if not command:
        die("could not find a test command; pass --test-command")
    t0 = time.monotonic()
    try:
        p = run(command, cwd=work, timeout=args.timeout)
        out = (p.stdout + p.stderr)
        exit_code = p.returncode
    except subprocess.TimeoutExpired:
        out, exit_code = f"test command timed out after {args.timeout}s", 124
    duration_ms = int((time.monotonic() - t0) * 1000)

    migrations = []
    for spec in args.migrations or []:
        path, _, sha = spec.partition("=")
        if not sha:
            die(f"--migrations expects path=sha256, got {spec!r}")
        migrations.append({"path": path, "sha256": sha})

    findings = {
        "from_tag": args.from_tag,
        "commits": json.loads(args.commits) if args.commits else [],
        "migrations": migrations,
        "tests": {"command": command, "exit_code": exit_code, "duration_ms": duration_ms,
                  **parse_counts(out), "output_tail": out[-4000:]},
        "install": {"command": args.install, "output_tail": install_log} if args.install else None,
    }
    verdict, reasons = shipcore.compute_verdict(findings)
    report = {
        "report_version": shipcore.REPORT_VERSION,
        "repo": args.repo,
        "head_sha": run(["git", "rev-parse", "HEAD"], cwd=work).stdout.strip(),
        "head_ref": args.head_ref,
        "verdict": verdict,
        "reasons": reasons,
        "summary": shipcore.summarize(findings, verdict),
        "findings": findings,
        "verified_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
    }
    report["digest"] = shipcore.verification_digest(report)

    out_path = Path(args.report or (HOME / "report.json"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report))
    print(f"{verdict}: {report['summary']}")
    if reasons:
        print("reasons: " + "; ".join(reasons))
    print(f"\nreport: {out_path}")
    print("Submit it with ship-gate submit_verification, passing the file contents verbatim.")
    print(f"\n--- last 40 lines of test output ---\n" + "\n".join(out.splitlines()[-40:]))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="clone, test, write the report")
    r.add_argument("--repo", required=True, help="owner/name")
    r.add_argument("--ref", required=True, help="commit sha to verify (from release_scope.head_sha)")
    r.add_argument("--head-ref", default="HEAD", help="branch the sha came from (default HEAD)")
    r.add_argument("--from-tag", help="tag the release is measured from")
    r.add_argument("--migrations", nargs="*", help="path=sha256 pairs from release_scope")
    r.add_argument("--commits", help="JSON array of commits from release_scope, for the summary")
    r.add_argument("--test-command", help="override the detected test command")
    r.add_argument("--install", help="command to install dependencies first")
    r.add_argument("--timeout", type=int, default=900)
    r.add_argument("--report", help="where to write report.json")
    r.set_defaults(func=cmd_run)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
