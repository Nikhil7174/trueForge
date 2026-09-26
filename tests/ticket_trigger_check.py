#!/usr/bin/env python3
"""Checks tf-ticket-trigger without TrueForge or live Linear: webhook signature and freshness, the event filter,
the HTTP endpoint, dedupe across webhook and poller, scope, and the poll cursor. Sessions are stubbed.

Run from the repo root: `python tests/ticket_trigger_check.py`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = Path(tempfile.mkdtemp(prefix="tt-check-"))
SECRET = secrets.token_hex(24)
os.environ.update({"TICKET_GATE_STATE_DIR": str(WORK / "gate"), "LINEAR_API_KEY": "lin_api_" + secrets.token_hex(16),
                   "LINEAR_WEBHOOK_SECRET": SECRET, "LINEAR_TEAM_KEY": "ZYN", "LINEAR_TRIGGER_LABEL": "Bug"})
sys.path.insert(0, str(ROOT / "tests"))

from linear_fake import FakeLinear  # noqa: E402
from trueforge_hackathon.agents.ticket_resolver import gate_server as gate  # noqa: E402
from trueforge_hackathon.agents.ticket_resolver import trigger as tr  # noqa: E402

import logging  # noqa: E402
logging.disable(logging.INFO)  # httpx request lines from the test client
failures = 0


def check(name: str, ok: bool, info=None):
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n      {info}" if info is not None and not ok else ""))


fake = FakeLinear()
gate.linear = tr.linear = fake
started: list[str] = []
tr.run_session = lambda issue: started.append(issue["identifier"])  # no TrueForge: record instead
fake.add("ZYN-10", "New bug", "details")
fake.add("ZYN-11", "Feature", "x", labels=("Feature",))
fake.add("ZYN-12", "Skip me", "x", labels=("Bug", "agent-skip"))
BUG = fake.label_ids[(None, "Bug")]


def signed(payload: dict) -> tuple[bytes, str]:
    body = json.dumps(payload).encode()
    return body, hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def event(action="create", labels=(BUG,), before=None, ident="ZYN-10", type_="Issue", ts=None) -> dict:
    p = {"action": action, "type": type_, "webhookTimestamp": ts if ts is not None else time.time() * 1000,
         "data": {"id": f"uuid-{ident}", "identifier": ident, "labelIds": list(labels)}}
    if before is not None:
        p["updatedFrom"] = {"labelIds": list(before)}
    return p


# --- signature and freshness ------------------------------------------------------------------------------------
body, sig = signed(event())
check("authentic, fresh delivery accepted", tr.verify_webhook(body, sig) is None)
check("wrong signature rejected", tr.verify_webhook(body, "0" * 64) == "bad signature")
check("missing signature rejected", tr.verify_webhook(body, None) == "bad signature")
check("body changed after signing rejected", tr.verify_webhook(body.replace(b"ZYN-10", b"ZYN-99"), sig) == "bad signature")
old_body, old_sig = signed(event(ts=time.time() * 1000 - 5 * 60_000))
check("replayed 5-minute-old delivery rejected", tr.verify_webhook(old_body, old_sig) == "stale delivery")

# --- event filter -----------------------------------------------------------------------------------------------
check("create with Bug label wanted", tr.wants(event()))
check("create without Bug label ignored", not tr.wants(event(labels=())))
check("update that adds Bug wanted", tr.wants(event("update", labels=(BUG,), before=())))
check("update where Bug was already there ignored", not tr.wants(event("update", labels=(BUG,), before=(BUG,))))
check("update that doesn't touch labels ignored", not tr.wants(event("update", labels=(BUG,))))
check("comment events ignored", not tr.wants(event(type_="Comment")))
check("remove events ignored", not tr.wants(event("remove")))

# --- dispatch: scope, dedupe, labels -------------------------------------------------------------------------------
res = tr.dispatch("ZYN-10", "webhook")
time.sleep(0.2)
check("in-scope ticket starts a session", res == "started ZYN-10" and started == ["ZYN-10"], (res, started))
check("ticket labelled Agent → Working", fake.group_label("ZYN-10", "Agent") == "Working")
check("second delivery of the same ticket is deduped", tr.dispatch("ZYN-10", "webhook").endswith("already triggered"))
check("poller finding it by UUID is deduped too", tr.dispatch("uuid-ZYN-10", "poll").endswith("already triggered"))
check("ticket without a routed label skipped", "no routed label" in tr.dispatch("ZYN-11", "webhook"))
check("agent-skip ticket skipped", "agent-skip" in tr.dispatch("ZYN-12", "webhook"))
check("unknown ticket doesn't crash", tr.dispatch("ZYN-404", "webhook") == "unreadable")
check("only one session started in total", started == ["ZYN-10"], started)

# --- HTTP endpoint ----------------------------------------------------------------------------------------------
from starlette.testclient import TestClient  # noqa: E402

fake.add("ZYN-13", "Another bug", "details")
client = TestClient(tr.make_app())
body, sig = signed(event(ident="ZYN-13"))
r = client.post("/linear/webhook", content=body, headers={"linear-signature": sig})
deadline = time.time() + 3
while "ZYN-13" not in started and time.time() < deadline:
    time.sleep(0.05)
check("signed webhook → 200 accepted and session started", r.status_code == 200 and r.json().get("accepted")
      and "ZYN-13" in started, (r.status_code, r.text, started))
r = client.post("/linear/webhook", content=body, headers={"linear-signature": "bad"})
check("unsigned webhook → 401", r.status_code == 401)
body, sig = signed(event(ident="ZYN-13", labels=()))
r = client.post("/linear/webhook", content=body, headers={"linear-signature": sig})
check("signed but unwanted event → 200 ignored", r.status_code == 200 and r.json().get("ignored"))
h = client.get("/healthz").json()
check("healthz lists triggered tickets", {x["issue"] for x in h["recent"]} == {"ZYN-10", "ZYN-13"}, h)

# --- poll cursor --------------------------------------------------------------------------------------------------
ts = tr.linear_ts(dt.datetime(2026, 9, 26, 11, 0, 31, 173000, tzinfo=dt.timezone.utc))
check("cursor uses Linear's timestamp format", ts == "2026-09-26T11:00:31.173Z", ts)
check("cursor compares correctly with Linear timestamps",
      tr.linear_ts(dt.datetime(2026, 9, 26, 11, 0, 31, tzinfo=dt.timezone.utc)) < "2026-09-26T11:00:31.173Z")

calls = []


def created_since(since, label):
    calls.append(since)
    created = (("ZYN-14", "2026-01-02T12:00:00.000Z"), ("ZYN-10", "2026-01-02T12:00:01.000Z"))
    return [fake.issue(i) | {"createdAt": c} for i, c in created if c > since]


fake.add("ZYN-14", "Polled bug", "details")
fake.issues_created_since = created_since
tr.cursor_path().unlink(missing_ok=True)
first = tr.poll_once()
check("first poll starts from now, not a backlog", first == [] and len(calls[0]) == 24, (first, calls))
tr.cursor_path().write_text("2026-01-01T00:00:00.000Z")
res = tr.poll_once()
time.sleep(0.2)
check("poll starts new tickets and dedupes old ones", res == ["started ZYN-14", "skipped: ZYN-10 was already triggered"]
      and started.count("ZYN-14") == 1, res)
check("cursor advanced to the newest createdAt", tr.cursor_path().read_text() == "2026-01-02T12:00:01.000Z")
check("next poll finds nothing new", tr.poll_once() == [])

print(f"\n{'OK' if not failures else f'{failures} FAILED'}")
sys.exit(1 if failures else 0)
