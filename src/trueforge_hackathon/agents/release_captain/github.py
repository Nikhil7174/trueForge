"""GitHub REST access for ship-gate. The token lives here and nowhere else.

Only two calls write: create_tag and create_release, both reached exclusively through
publish_release, which is the approval-gated tool. Everything else is read-only.

GITHUB_API_URL exists so tests can point at a stub; it is not a production knob.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
REPO = os.environ.get("GITHUB_REPO", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
TIMEOUT = float(os.environ.get("GITHUB_TIMEOUT_SECONDS", "30"))


class GitHubError(RuntimeError):
    pass


def _call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    url = f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:600]
        raise GitHubError(f"{method} {path} -> {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise GitHubError(f"{method} {path} -> unreachable: {e.reason}") from e


def _repo_path(suffix: str, repo: str | None = None) -> str:
    return f"/repos/{repo or REPO}{suffix}"


# ---------------------------------------------------------------- read

def list_tags(repo: str | None = None) -> list[dict[str, Any]]:
    return _call("GET", _repo_path("/tags?per_page=100", repo)) or []


def head_sha(ref: str = "HEAD", repo: str | None = None) -> str:
    """Commit sha a ref points at. `HEAD` means the repository's default branch."""
    if ref in ("HEAD", ""):
        default = _call("GET", _repo_path("", repo)).get("default_branch", "main")
        ref = default
    commit = _call("GET", _repo_path(f"/commits/{urllib.parse.quote(ref, safe='')}", repo))
    return commit["sha"]


def compare(base: str, head: str, repo: str | None = None) -> dict[str, Any]:
    """Commits and changed files between two refs (GitHub's three-dot compare)."""
    b, h = urllib.parse.quote(base, safe=""), urllib.parse.quote(head, safe="")
    return _call("GET", _repo_path(f"/compare/{b}...{h}", repo))


def file_text(path: str, ref: str, repo: str | None = None) -> str:
    """Raw file contents at a ref. Used to hash migrations exactly as committed."""
    import base64

    q = urllib.parse.urlencode({"ref": ref})
    blob = _call("GET", _repo_path(f"/contents/{urllib.parse.quote(path)}?{q}", repo))
    if blob.get("encoding") != "base64":
        raise GitHubError(f"unexpected encoding for {path}: {blob.get('encoding')}")
    return base64.b64decode(blob["content"]).decode("utf-8", "replace")


# ---------------------------------------------------------------- write (gated)

def create_tag(tag: str, sha: str, repo: str | None = None) -> dict[str, Any]:
    return _call("POST", _repo_path("/git/refs", repo), {"ref": f"refs/tags/{tag}", "sha": sha})


def delete_tag(tag: str, repo: str | None = None) -> None:
    _call("DELETE", _repo_path(f"/git/refs/tags/{urllib.parse.quote(tag, safe='')}", repo))


def create_release(tag: str, name: str, body: str, repo: str | None = None) -> dict[str, Any]:
    return _call("POST", _repo_path("/releases", repo),
                 {"tag_name": tag, "name": name, "body": body, "draft": False, "prerelease": False})
