"""Offline IAM policy lint (standard library only).

The MCP server enforces these checks on put_role_policy. The same file is copied to
skills/access-review-playbook/scripts/lint_policy.py so the agent can run it in the
sandbox before proposing a policy. Keep the two copies identical.

Usage in the sandbox:  python3 lint_policy.py new.json [old.json]
"""

from __future__ import annotations

import fnmatch
import json
import sys
from typing import Any

# Actions that let a principal grant itself more access.
PRIV_ESC_ACTIONS = {
    "iam:attachrolepolicy",
    "iam:attachuserpolicy",
    "iam:attachgrouppolicy",
    "iam:putrolepolicy",
    "iam:putuserpolicy",
    "iam:putgrouppolicy",
    "iam:createpolicyversion",
    "iam:setdefaultpolicyversion",
    "iam:createaccesskey",
    "iam:createloginprofile",
    "iam:updateloginprofile",
    "iam:updateassumerolepolicy",
    "iam:addusertogroup",
    "iam:passrole",
    "sts:assumerole",
    "lambda:updatefunctioncode",
    "lambda:createfunction",
    "glue:updatedevendpoint",
    "cloudformation:createstack",
    "ec2:runinstances",
}


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def statements(doc: dict) -> list[dict]:
    return [s for s in _as_list(doc.get("Statement")) if isinstance(s, dict)]


def _allowed_actions(doc: dict) -> set[str]:
    out: set[str] = set()
    for s in statements(doc):
        if s.get("Effect") == "Allow":
            out.update(a.lower() for a in _as_list(s.get("Action")))
    return out


def _grants(actions: set[str], target: str) -> bool:
    """True if any (possibly wildcarded) action in `actions` covers `target`."""
    return any(fnmatch.fnmatchcase(target, pattern) for pattern in actions)


def _conditions_for(doc: dict, action: str) -> list[str] | None:
    """Conditions under which `doc` allows `action`; None if some grant is unconditional."""
    found: list[str] = []
    for s in statements(doc):
        if s.get("Effect") != "Allow":
            continue
        if not _grants({a.lower() for a in _as_list(s.get("Action"))}, action):
            continue
        if "Condition" not in s:
            return None
        found.append(json.dumps(s["Condition"], sort_keys=True))
    return found


def lint(new: dict, old: dict | None = None) -> list[str]:
    """Return a list of violations; empty means the policy passes."""
    problems: list[str] = []
    if new.get("Version") != "2012-10-17":
        problems.append("Version must be 2012-10-17")
    stmts = statements(new)
    if not stmts:
        problems.append("policy has no statements")
    old_actions = _allowed_actions(old) if old else None
    for i, s in enumerate(stmts):
        if s.get("Effect") != "Allow":
            continue
        if "NotAction" in s or "NotResource" in s:
            problems.append(f"statement {i}: NotAction/NotResource is not allowed")
        actions = [a.lower() for a in _as_list(s.get("Action"))]
        resources = _as_list(s.get("Resource"))
        for a in actions:
            if a == "*" or a.endswith(":*"):
                problems.append(f"statement {i}: wildcard action {a!r}")
            elif "*" in a:
                problems.append(f"statement {i}: partial wildcard action {a!r}")
        if "*" in resources and any(a.startswith("iam:") for a in actions):
            problems.append(f"statement {i}: IAM actions on Resource '*'")
        for a in actions:
            if a in PRIV_ESC_ACTIONS and (old_actions is None or not _grants(old_actions, a)):
                problems.append(f"statement {i}: privilege-escalation action {a!r} not granted before")
            elif a in PRIV_ESC_ACTIONS and a == "iam:passrole" and "*" in resources:
                problems.append(f"statement {i}: iam:PassRole on Resource '*'")
    if old is not None:
        old_actions = old_actions or set()
        for a in _allowed_actions(new):
            if not _grants(old_actions, a):
                problems.append(f"expands access: {a!r} is not granted by the current policy")
        # An action the current policy only allows under a condition must keep that condition.
        for a in sorted(_allowed_actions(new)):
            required = _conditions_for(old, a)
            if not required:
                continue
            granted_with = _conditions_for(new, a)
            if granted_with is None or not set(granted_with) <= set(required):
                problems.append(f"condition dropped for {a!r}: must keep {required[0]}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    new = json.load(open(argv[1], encoding="utf-8"))
    old = json.load(open(argv[2], encoding="utf-8")) if len(argv) > 2 else None
    problems = lint(new, old)
    print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
