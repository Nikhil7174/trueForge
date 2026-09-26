#!/usr/bin/env python3
"""Reproduce a bug report against exported product code, inside the sandbox, and record the evidence.

  repro.py setup  --source <file> --issue ZYN-12 [--force]
  repro.py run    --workdir /tmp/tr/ZYN-12 --repro <name.py> [--note "what this attempt varies"]
  repro.py patch  --workdir /tmp/tr/ZYN-12
  repro.py report --workdir /tmp/tr/ZYN-12 [--out report.json]
  repro.py status --workdir /tmp/tr/ZYN-12

Workdir layout:
  pristine/  the exported code, untouched. Every `run` executes here, and records its tree digest.
  src/       a git checkout of the same code. Edit here; `patch` diffs it and re-runs everything.
  repros/    your repro scripts. Exit 0 = behaves as the customer expected, 1 = reported bug happens.
  state.json every run, the baseline tests and the patch. Append-only through this script.

No Linear or Git credentials exist here: the code arrives as a file from ticket-gate export_source.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ticketcore as tc  # noqa: E402

ROOT = Path(os.environ.get("TICKET_WORK_ROOT", "/tmp/tr"))
REPRO_TIMEOUT_S = 120
TESTS_TIMEOUT_S = 600
MAX_REPRO_BYTES = 20_000
GIT_ID = ["-c", "user.name=ticket-resolver", "-c", "user.email=ticket-resolver@localhost", "-c", "commit.gpgsign=false"]


def die(msg: str, code: int = 2):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_state(workdir: Path) -> dict:
    path = workdir / "state.json"
    if not path.exists():
        die(f"{workdir} has no state.json; run `repro.py setup` first")
    return json.loads(path.read_text())


def save_state(workdir: Path, state: dict):
    tmp = workdir / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(workdir / "state.json")


def execute(cmd: list[str], cwd: Path, timeout: int) -> dict:
    env = {**os.environ, "PYTHONPATH": str(cwd), "PYTHONDONTWRITEBYTECODE": "1", "REPRO_CODE_DIR": str(cwd)}
    started = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        code, out = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        code = 124
        out = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")) + \
              f"\n[timed out after {timeout}s]"
    return {"exit_code": code, "duration_ms": int((time.monotonic() - started) * 1000),
            "output": tc.tail(out), "output_sha256": tc.sha256_text(out)}


def command(cmd) -> list[str]:
    """Commands from the bundle header use `python3`; run them with this interpreter."""
    cmd = list(cmd)
    if cmd and cmd[0] in ("python", "python3"):
        cmd[0] = sys.executable
    return cmd


def run_tests(state: dict, code_dir: Path) -> dict:
    res = execute(command(state["test_command"]), code_dir, TESTS_TIMEOUT_S)
    return {"command": " ".join(state["test_command"]), **res, **tc.parse_tests(res["output"], res["exit_code"])}


def run_setup_command(state: dict, code_dir: Path):
    if state.get("setup_command"):
        res = execute(command(state["setup_command"]), code_dir, TESTS_TIMEOUT_S)
        if res["exit_code"] != 0:
            die(f"setup command failed in {code_dir}:\n{res['output']}")


def git(src: Path, *args: str) -> str:
    p = subprocess.run(["git", *GIT_ID, *args], cwd=src, capture_output=True, text=True)
    if p.returncode != 0:
        die(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def repro_path(workdir: Path, name: str) -> Path:
    repros = (workdir / "repros").resolve()
    path = Path(name)
    path = (path if path.is_absolute() else repros / path).resolve()
    if path.parent != repros:
        die(f"repros live directly in {repros}; got {name}")
    if not path.exists():
        die(f"repro not found: {path}")
    if path.stat().st_size > MAX_REPRO_BYTES:
        die(f"repro is over {MAX_REPRO_BYTES} bytes; keep it minimal")
    return path


def describe(exit_code: int) -> str:
    return {tc.EXIT_REPORTED: "REPORTED BEHAVIOUR SEEN (reproduced)",
            tc.EXIT_EXPECTED: "behaves as the customer expected (not reproduced)"}.get(
        exit_code, f"REPRO BROKEN (exit {exit_code}): fix the script; this attempt won't count")


# ---------------------------------------------------------------- setup

def cmd_setup(args):
    source = Path(args.source)
    if not source.exists():
        die(f"source file not found: {source}")
    workdir = ROOT / args.issue
    if workdir.exists():
        if not args.force:
            die(f"{workdir} already exists with recorded attempts. Use `repro.py status --workdir {workdir}`, "
                "or --force to start over (discards them)")
        shutil.rmtree(workdir)
    if shutil.which("git") is None:
        die("git is not installed in the sandbox")

    try:
        header = tc.unpack_source(source.read_text(), workdir / "pristine")
    except ValueError as e:
        die(str(e))
    if header.get("issue_id") and header["issue_id"] != args.issue:
        die(f"this source was exported for {header['issue_id']}, not {args.issue}")
    shutil.copytree(workdir / "pristine", workdir / "src")
    (workdir / "repros").mkdir()

    src = workdir / "src"
    git(src, "init", "-q")
    (src / ".git" / "info" / "exclude").write_text("\n".join(p for p in tc.IGNORED if p != ".git") + "\n")
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", f"source {header['source_id']} for {args.issue}")

    state = {
        "issue_id": args.issue,
        "source_id": header["source_id"],
        "source_tree_sha256": header["tree_sha256"],
        "project": header.get("project"),
        "test_command": header.get("test_command") or ["python3", "-m", "unittest", "discover", "-s", "tests"],
        "setup_command": header.get("setup_command"),
        "created_at": now(),
        "attempts": [],
        "patch": None,
    }
    run_setup_command(state, workdir / "pristine")
    run_setup_command(state, src)
    state["baseline_tests"] = run_tests(state, workdir / "pristine")
    if tc.tree_digest(workdir / "pristine") != header["tree_sha256"]:
        die("the product's setup or tests modified its own source files; can't use it as a baseline")
    save_state(workdir, state)

    b = state["baseline_tests"]
    print(f"WORKDIR: {workdir}")
    print(f"SOURCE: {header['source_id']} ({header.get('project')}, {header.get('files')} files, "
          f"tree {header['tree_sha256'][:12]})")
    print(f"BASELINE TESTS: {'passing' if b['passed'] else 'FAILING'}, {b['ran']} ran ({b['command']})")
    if not b["passed"]:
        print(b["output"])
    print(f"Edit code in {src}. Write repros in {workdir / 'repros'} and run them with `repro.py run`.")


# ---------------------------------------------------------------- run

def cmd_run(args):
    workdir = Path(args.workdir)
    state = load_state(workdir)
    path = repro_path(workdir, args.repro)
    code = path.read_text()
    pristine = workdir / "pristine"
    tree = tc.tree_digest(pristine)
    res = execute([sys.executable, str(path)], pristine, REPRO_TIMEOUT_S)
    attempt = {
        "n": len(state["attempts"]) + 1,
        "repro": path.name,
        "repro_sha256": tc.sha256_text(code),
        "repro_code": code,
        "note": args.note or "",
        "tree_sha256": tree,
        "ran_at": now(),
        **res,
    }
    state["attempts"].append(attempt)
    save_state(workdir, state)

    print(f"ATTEMPT {attempt['n']}: {path.name} -> exit {res['exit_code']}: {describe(res['exit_code'])}")
    if tree != state["source_tree_sha256"]:
        print("WARNING: pristine/ no longer matches the exported source, so this attempt won't count. "
              "Don't edit pristine/; edit src/.")
    print(res["output"].rstrip())
    if state["patch"]:
        print("A patch was already recorded. Run `repro.py patch` again so this repro is re-run on it.")


# ---------------------------------------------------------------- patch

def cmd_patch(args):
    workdir = Path(args.workdir)
    state = load_state(workdir)
    if not state["attempts"]:
        die("record at least one repro with `repro.py run` before patching")
    src = workdir / "src"
    git(src, "add", "-A")
    diff = git(src, "diff", "--cached", "--no-color", "--no-ext-diff", "HEAD")
    if not diff.strip():
        die(f"no changes in {src}")
    files, added, removed = [], 0, 0
    for line in git(src, "diff", "--cached", "--numstat", "HEAD").splitlines():
        a, r, name = line.split("\t", 2)
        files.append(name)
        added += int(a) if a.isdigit() else 0
        removed += int(r) if r.isdigit() else 0

    run_setup_command(state, src)
    reruns = []
    seen = set()
    with tempfile.TemporaryDirectory() as tmp:
        for a in state["attempts"]:
            key = (a["repro"], a["repro_sha256"])
            if key in seen:
                continue
            seen.add(key)
            script = Path(tmp) / a["repro"]
            script.write_text(a["repro_code"])  # exactly the code that ran before the patch
            res = execute([sys.executable, str(script)], src, REPRO_TIMEOUT_S)
            reruns.append({"repro": a["repro"], "repro_sha256": a["repro_sha256"], "before": a["exit_code"], **res})
    tests = run_tests(state, src)

    state["patch"] = {
        "diff": diff,
        "diff_sha256": tc.sha256_text(diff),
        "files": files,
        "added": added,
        "removed": removed,
        "patched_tree_sha256": tc.tree_digest(src),
        "reruns": reruns,
        "tests": tests,
        "recorded_at": now(),
    }
    save_state(workdir, state)
    (workdir / "patch.diff").write_text(diff)

    print(f"PATCH: +{added}/-{removed} in {len(files)} file(s): {', '.join(files)}")
    for r in reruns:
        print(f"  {r['repro']:40} before exit {r['before']} -> after exit {r['exit_code']}")
    base = state["baseline_tests"]
    print(f"TESTS: {base['ran']} -> {tests['ran']}, {'all passing' if tests['passed'] else 'FAILING'}")
    if not tests["passed"]:
        print(tests["output"])
    outcome, reasons = tc.compute_outcome(build_report(state))
    print(f"OUTCOME IF REPORTED NOW: {outcome}")
    for r in reasons:
        print(f"  - {r}")
    print(f"PATCH_FILE: {workdir / 'patch.diff'}")


# ---------------------------------------------------------------- report / status

def build_report(state: dict) -> dict:
    report = {
        "report_version": tc.REPORT_VERSION,
        "issue_id": state["issue_id"],
        "source_id": state["source_id"],
        "source_tree_sha256": state["source_tree_sha256"],
        "project": state.get("project"),
        "baseline_tests": state.get("baseline_tests"),
        "attempts": state["attempts"],
        "patch": state["patch"],
    }
    outcome, reasons = tc.compute_outcome(report)
    report.update(outcome=outcome, reasons=reasons, summary=tc.summarize(report, outcome), generated_at=now())
    return report


def cmd_report(args):
    workdir = Path(args.workdir)
    report = build_report(load_state(workdir))
    print(f"OUTCOME: {report['outcome']}")
    print(f"SUMMARY: {report['summary']}")
    for r in report["reasons"]:
        print(f"  - {r}")
    if report["outcome"] == tc.INCOMPLETE:
        die("not enough evidence to submit yet: record more distinct repro attempts", 3)
    report["digest"] = tc.report_digest(report)
    out = Path(args.out) if args.out else workdir / "report.json"
    out.write_text(tc.canonical_json(report))
    print(f"REPORT_FILE: {out} ({out.stat().st_size:,} bytes)")


def cmd_status(args):
    workdir = Path(args.workdir)
    state = load_state(workdir)
    print(f"{state['issue_id']}: source {state['source_id']}, baseline tests "
          f"{'passing' if state['baseline_tests']['passed'] else 'FAILING'} ({state['baseline_tests']['ran']} ran)")
    for a in state["attempts"]:
        print(f"  #{a['n']} {a['repro']:36} exit {a['exit_code']}  {a['note']}")
    p = state["patch"]
    print(f"  patch: +{p['added']}/-{p['removed']} in {', '.join(p['files'])}" if p else "  patch: none")
    outcome, _ = tc.compute_outcome(build_report(state))
    print(f"OUTCOME IF REPORTED NOW: {outcome}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup", help="unpack exported source, init git, run baseline tests")
    s.add_argument("--source", required=True)
    s.add_argument("--issue", required=True)
    s.add_argument("--force", action="store_true")
    r = sub.add_parser("run", help="run one repro against the unchanged code and record it")
    r.add_argument("--workdir", required=True)
    r.add_argument("--repro", required=True)
    r.add_argument("--note")
    for name, help_ in (("patch", "diff src/, re-run every repro and the tests on it"),
                        ("report", "write the report for ticket-gate submit_attempt"),
                        ("status", "show recorded attempts and the current outcome")):
        c = sub.add_parser(name, help=help_)
        c.add_argument("--workdir", required=True)
        if name == "report":
            c.add_argument("--out")
    args = p.parse_args()
    {"setup": cmd_setup, "run": cmd_run, "patch": cmd_patch, "report": cmd_report, "status": cmd_status}[args.command](args)


if __name__ == "__main__":
    main()
