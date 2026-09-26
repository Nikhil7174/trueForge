"""Shared helpers for infra/seed.py, infra/teardown.py and infra/doctor.py.

The state file (infra/.seed-state.json) is gitignored. It is the only record of
which AWS resources these scripts own; teardown removes exactly those IDs.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3

REGION = os.environ.get("AWS_REGION", "us-west-2")
STATE_PATH = Path(__file__).resolve().parent / ".seed-state.json"


def session() -> boto3.session.Session:
    return boto3.session.Session(region_name=REGION)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def tag_list(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"Key": k, "Value": v} for k, v in tags.items()]
