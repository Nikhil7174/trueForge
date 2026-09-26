"""What a customer reply may say. Enforced by ticket-gate on propose_reply and again on reply_to_customer.

Pure functions, so the rules are testable without Linear.
"""
from __future__ import annotations

import re

MIN_CHARS, MAX_CHARS = 40, 4000

HEADERS = {
    "FIXED": "**Status: reproduced and fixed.** The fix is written and tested; it is pending review and release.",
    "REPRODUCED_NO_FIX": "**Status: confirmed.** We reproduced this; it needs a follow-up decision before it can be fixed.",
    "NOT_REPRODUCED": "**Status: could not reproduce.** Details of what we checked are below.",
    # A ticket that asked for a migration to be rehearsed: the header is the rehearsal verdict. Nothing is applied.
    "MIGRATION_SAFE": "**Status: rehearsed, verdict SAFE.** It ran cleanly on a copy of production. It has not been "
                      "applied; applying is a separate, approved step.",
    "MIGRATION_REVIEW": "**Status: rehearsed, verdict REVIEW.** It changes existing data; a person must accept those "
                        "changes before it can be applied. It has not been applied.",
    "MIGRATION_BLOCK": "**Status: rehearsed, verdict BLOCK.** It must not be applied as written. Nothing was applied.",
    "MIGRATION_NO_VERDICT": "**Status: not rehearsed.** The rehearsal did not produce a verdict for this SQL. Nothing "
                            "was applied.",
}

SECRET_PATTERNS = [
    (re.compile(r"\blin_(?:api|oauth)_[A-Za-z0-9]{8,}"), "a Linear API key"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "an API secret key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "a GitHub token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}", re.I), "a bearer token"),
    (re.compile(r"postgres(?:ql)?://\S+:\S+@", re.I), "a database URL with a password"),
]

INTERNAL_PATTERNS = [
    (re.compile(r"(?:^|[\s(`'\"])/(?:tmp|opt|home|Users|var|private)/\S*"), "a sandbox or server file path"),
    (re.compile(r"\b(?:pristine|state\.json|repro\.py|ticketcore|report\.json|patch\.diff)\b"), "internal tooling names"),
    (re.compile(r"\b(?:ticket-gate|db-gate|export_source|submit_attempt|request_migration|propose_reply|reply_to_customer)\b"), "internal tool names"),
    (re.compile(r"Traceback \(most recent call last\)|^\s*File \".*\", line \d+", re.M), "a stack trace"),
    (re.compile(r"\b(?:src|att|dr|ho|rh|snap)_[0-9a-f]{8}\b"), "internal ids"),
]

# Claims a reply may only make when the gate's outcome is FIXED.
FIX_CLAIMS = re.compile(
    r"\b(?:has been|have|we've|we have|we)\s+(?:fixed|resolved|patched)\b"
    r"|\b(?:is|was|now)\s+(?:now\s+)?(?:fixed|resolved|patched)\b"
    r"|\bfix(?:ed)? (?:is|has been) (?:ready|written|in place)\b",
    re.I,
)
# No outcome may claim the change is live: the gate never deploys anything.
RELEASE_CLAIMS = re.compile(
    r"\b(?:has been|is now|was|been|now)\s+(?:deployed|released|shipped|rolled out)\b|\bis (?:now )?live\b"
    r"|\bin production\b",
    re.I,
)
STATUS_LINE = re.compile(r"^\s*(?:\*\*)?\s*status\s*:", re.I | re.M)

# Medicare Beneficiary Identifier, with or without the dashes an EHR adds. PHI: only the ticket's own may appear.
MBI = re.compile(r"\b[1-9][A-Z][A-Z0-9]\d-?[A-Z][A-Z0-9]\d-?[A-Z]{2}\d{2}\b", re.I)


def normalize_mbi(mbi: str) -> str:
    return mbi.replace("-", "").upper()


def mbis_in(text: str) -> set[str]:
    return {normalize_mbi(m) for m in MBI.findall(text or "")}


def check_reply(body: str, outcome: str, ticket_text: str, known_secrets: list[str] | None = None) -> list[str]:
    """Problems with a draft. Empty list means it may be proposed."""
    problems: list[str] = []
    text = body or ""
    if outcome not in HEADERS:
        return [f"unknown outcome {outcome!r}"]
    if len(text.strip()) < MIN_CHARS:
        problems.append(f"reply is too short (under {MIN_CHARS} characters)")
    if len(text) > MAX_CHARS:
        problems.append(f"reply is too long ({len(text)} > {MAX_CHARS} characters)")
    for rx, what in SECRET_PATTERNS:
        if rx.search(text):
            problems.append(f"contains {what}")
    for secret in known_secrets or []:
        if secret and len(secret) >= 8 and secret in text:
            problems.append("contains a configured secret")
    for rx, what in INTERNAL_PATTERNS:
        if rx.search(text):
            problems.append(f"contains {what}; write for the customer, not for engineers")
    if STATUS_LINE.search(text):
        problems.append("don't write a status line; the gate adds one that matches the outcome")
    if outcome != "FIXED":
        m = FIX_CLAIMS.search(text)
        if m:
            problems.append(f"claims a fix ({m.group(0)!r}) but the outcome is {outcome}")
    m = RELEASE_CLAIMS.search(text)
    if m:
        problems.append(f"claims the change is live ({m.group(0)!r}); nothing has been deployed")
    foreign = sorted(mbis_in(text) - mbis_in(ticket_text))
    if foreign:
        problems.append(f"mentions MBIs that are not in the ticket ({', '.join(foreign)}); "
                        "never share another beneficiary's identifier")
    return problems


def render_reply(body: str, outcome: str, rehearsal_summary: str | None = None) -> str:
    """The exact text posted to the ticket: the gate's status header, the approved body, db-gate's rehearsal summary
    when there is one (verbatim, never the agent's), and a disclosure line."""
    rehearsal = f"Rehearsal result: `{rehearsal_summary}`\n\n" if rehearsal_summary else ""
    return (f"{HEADERS[outcome]}\n\n{body.strip()}\n\n{rehearsal}"
            "_Investigated by the ticket-resolver agent; reviewed and approved by a person before sending._")
