"""Rules shared by the sandbox (repro.py) and the gate (ticket_resolver/gate_server.py).

Everything that decides a ticket's outcome lives here, so the gate can recompute it from the raw
attempts instead of trusting the label a report carries. Also the source-bundle format the gate
exports and the sandbox unpacks. Standard library only.
"""
from __future__ import annotations

import base64
import fnmatch
import gzip
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

REPORT_VERSION = 1
SOURCE_MAGIC = "TICKET-GATE-SOURCE v1 "
OUTPUT_TAIL = 4000               # chars of stdout+stderr kept in the report per run
MIN_ATTEMPTS_NOT_REPRODUCED = 2  # distinct repro scripts before "could not reproduce" is honest

FIXED, REPRODUCED_NO_FIX, NOT_REPRODUCED, INCOMPLETE = "FIXED", "REPRODUCED_NO_FIX", "NOT_REPRODUCED", "INCOMPLETE"
OUTCOMES = (FIXED, REPRODUCED_NO_FIX, NOT_REPRODUCED)

# Build output and caches: not part of the source, never in a bundle or a tree digest.
IGNORED = (".git", "__pycache__", "*.pyc", ".data", ".pytest_cache", ".mypy_cache", "*.egg-info", ".DS_Store")

EXIT_EXPECTED, EXIT_REPORTED = 0, 1  # repro exit codes: behaves as customer expected / reported bug happens


# ---------------------------------------------------------------- hashing

def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def report_digest(report: dict) -> str:
    body = {k: v for k, v in report.items() if k != "digest"}
    return sha256_text(canonical_json(body))


def tail(text: str, limit: int = OUTPUT_TAIL) -> str:
    return text if len(text) <= limit else "...[truncated]...\n" + text[-limit:]


# ---------------------------------------------------------------- source trees

def _ignored(rel: PurePosixPath) -> bool:
    return any(fnmatch.fnmatch(part, pat) for part in rel.parts for pat in IGNORED)


def tree_files(root: Path) -> list[str]:
    root = Path(root)
    out = []
    for p in root.rglob("*"):
        rel = PurePosixPath(p.relative_to(root).as_posix())
        if p.is_file() and not p.is_symlink() and not _ignored(rel):
            out.append(str(rel))
    return sorted(out)


def tree_digest(root: Path) -> str:
    """sha256 over every source file's path and content. Build output and caches don't count."""
    root = Path(root)
    entries = [[rel, sha256_bytes((root / rel).read_bytes())] for rel in tree_files(root)]
    return sha256_text(canonical_json(entries))


def pack_source(root: Path, header: dict) -> str:
    """Gate side: a deterministic tar.gz of the source tree as one text blob with a JSON header."""
    root = Path(root)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in tree_files(root):
            data = (root / rel).read_bytes()
            info = tarfile.TarInfo(rel)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            tar.addfile(info, io.BytesIO(data))
    tar_bytes = gzip.compress(buf.getvalue(), compresslevel=6, mtime=0)
    full = {**header, "tree_sha256": tree_digest(root), "tar_sha256": sha256_bytes(tar_bytes),
            "files": len(tree_files(root)), "format": "tar | gzip | base64"}
    return SOURCE_MAGIC + canonical_json(full) + "\n" + base64.b64encode(tar_bytes).decode()


def _unwrap(raw: str) -> str:
    """Harnesses deliver tool results in different wrappers: plain text, {"result": ...}, a JSON list of
    content parts, or a Python repr like `type='text' text='TICKET-GATE-SOURCE v1 {...}\\n<base64>' ...`."""
    stripped = raw.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return raw
        parts = obj if isinstance(obj, list) else [obj]
        texts = []
        for p in parts:
            if isinstance(p, dict):
                texts.append(str(p.get("result") or p.get("text") or ""))
            else:
                texts.append(str(p))
        return "\n".join(texts)
    return raw


def read_source_blob(raw: str) -> tuple[dict, bytes]:
    text = _unwrap(raw)
    start = text.find(SOURCE_MAGIC)
    if start < 0:
        raise ValueError(f"not an export_source result (missing '{SOURCE_MAGIC.strip()}' header)")
    try:
        header, end = json.JSONDecoder().raw_decode(text, start + len(SOURCE_MAGIC))
    except json.JSONDecodeError as e:
        raise ValueError(f"export_source header is not valid JSON: {e}") from e
    rest = text[end:]
    rest = rest[2:] if rest.startswith("\\n") else rest  # a repr-escaped newline
    payload = re.match(r"\s*([A-Za-z0-9+/=\s]*)", rest).group(1)
    tar_bytes = base64.b64decode("".join(payload.split()))
    if sha256_bytes(tar_bytes) != header.get("tar_sha256"):
        raise ValueError("source archive sha256 does not match its header (truncated or altered file)")
    return header, tar_bytes


def extract_tar(tar_gz: bytes, dest: Path) -> None:
    """Extract a pack_source archive: regular files only, no absolute paths or `..`."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(tar_gz)), mode="r") as tar:
        for m in tar.getmembers():
            rel = PurePosixPath(m.name)
            if not m.isfile() or rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"unsafe archive member: {m.name}")
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(m).read())


def unpack_source(raw: str, dest: Path) -> dict:
    """Sandbox side: verify and extract a pack_source blob into dest. Returns the header."""
    header, tar_bytes = read_source_blob(raw)
    extract_tar(tar_bytes, dest)
    if tree_digest(dest) != header["tree_sha256"]:
        raise ValueError("unpacked tree digest does not match the header")
    return header


# ---------------------------------------------------------------- test output

def parse_tests(output: str, exit_code: int) -> dict:
    """Counts from unittest or pytest output. `ran` is None when the output has no recognisable summary."""
    ran = failed = None
    m = re.search(r"^Ran (\d+) tests? in", output, re.M)
    if m:  # unittest
        ran = int(m.group(1))
        f = re.search(r"^FAILED \((.*)\)", output, re.M)
        failed = sum(int(n) for n in re.findall(r"(?:failures|errors)=(\d+)", f.group(1))) if f else 0
    else:  # pytest: "=== 3 failed, 7 passed, 1 error in 0.12s ==="
        counts = {k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|errors?)\b", output)}
        if counts:
            failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
            ran = counts.get("passed", 0) + failed
    return {"ran": ran, "failed": failed, "passed": exit_code == 0 and not failed}


# ---------------------------------------------------------------- outcome

def _counted(attempt: dict, source_tree: str) -> bool:
    return attempt.get("exit_code") in (EXIT_EXPECTED, EXIT_REPORTED) and attempt.get("tree_sha256") == source_tree


def compute_outcome(report: dict) -> tuple[str, list[str]]:
    """The only place an outcome is decided. Returns (outcome, reasons)."""
    reasons: list[str] = []
    source_tree = report.get("source_tree_sha256")
    attempts = report.get("attempts") or []

    for a in attempts:
        if a.get("tree_sha256") != source_tree:
            reasons.append(f"attempt {a.get('n')} ({a.get('repro')}) did not run on the exported source; not counted")
        elif a.get("exit_code") not in (EXIT_EXPECTED, EXIT_REPORTED):
            reasons.append(f"attempt {a.get('n')} ({a.get('repro')}) exited {a.get('exit_code')}: "
                           "the repro itself is broken; not counted")
    counted = [a for a in attempts if _counted(a, source_tree)]
    reproducing = [a for a in counted if a["exit_code"] == EXIT_REPORTED]
    baseline = report.get("baseline_tests") or {}
    if baseline and not baseline.get("passed"):
        reasons.append("the product's test suite already failed before any change")

    patch = report.get("patch")
    if not reproducing:
        distinct = {a["repro_sha256"] for a in counted}
        if patch:
            reasons.append("patch ignored: nothing reproduced")
        if len(distinct) < MIN_ATTEMPTS_NOT_REPRODUCED:
            reasons.append(f"only {len(distinct)} distinct repro(s) ran cleanly; "
                           f"at least {MIN_ATTEMPTS_NOT_REPRODUCED} are needed to call it not reproduced")
            return INCOMPLETE, reasons
        return NOT_REPRODUCED, reasons

    if not patch:
        reasons.append("no patch")
        return REPRODUCED_NO_FIX, reasons

    problems = []
    if not (patch.get("diff") or "").strip():
        problems.append("patch is empty")
    if patch.get("diff_sha256") != sha256_text(patch.get("diff") or ""):
        problems.append("patch diff does not match its sha256")
    reruns = {(r.get("repro"), r.get("repro_sha256")): r for r in patch.get("reruns") or []}
    for a in {(a["repro"], a["repro_sha256"]): a for a in counted}.values():
        r = reruns.get((a["repro"], a["repro_sha256"]))
        if r is None:
            problems.append(f"{a['repro']} was not re-run on the patched code")
        elif r.get("exit_code") != EXIT_EXPECTED:
            was = "still reproduces" if a["exit_code"] == EXIT_REPORTED else "regressed"
            problems.append(f"{a['repro']} {was} on the patched code (exit {r.get('exit_code')})")
    tests = patch.get("tests") or {}
    if not tests.get("passed"):
        problems.append("test suite fails on the patched code")
    if tests.get("ran") is None or baseline.get("ran") is None:
        problems.append("could not count tests, so no regression test can be confirmed")
    elif tests["ran"] <= baseline["ran"]:
        problems.append(f"no regression test added (tests {baseline['ran']} -> {tests['ran']})")
    if problems:
        return REPRODUCED_NO_FIX, reasons + ["patch not accepted: " + p for p in problems]
    return FIXED, reasons


def summarize(report: dict, outcome: str) -> str:
    """One deterministic line. The gate recomputes it."""
    source_tree = report.get("source_tree_sha256")
    counted = [a for a in report.get("attempts") or [] if _counted(a, source_tree)]
    reproducing = sorted({a["repro"] for a in counted if a["exit_code"] == EXIT_REPORTED})
    names = sorted({a["repro"] for a in counted})
    src = f"source {report.get('source_id')}"
    if outcome == NOT_REPRODUCED or outcome == INCOMPLETE:
        return f"{outcome}: none of {len(names)} repro(s) showed the reported behaviour on {src} ({', '.join(names)})"
    parts = [f"{outcome}: {len(reproducing)}/{len(names)} repro(s) showed the reported behaviour on {src} "
             f"({', '.join(reproducing)})"]
    patch = report.get("patch")
    if patch:
        tests = patch.get("tests") or {}
        base = (report.get("baseline_tests") or {}).get("ran")
        verdict = "accepted" if outcome == FIXED else "not accepted"
        parts.append(f"patch {verdict}: +{patch.get('added', 0)}/-{patch.get('removed', 0)} in "
                     f"{len(patch.get('files') or [])} file(s); tests {base} -> {tests.get('ran')}, "
                     f"{'all passing' if tests.get('passed') else 'failing'}")
    else:
        parts.append("no patch")
    return "; ".join(parts)
