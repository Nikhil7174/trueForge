from __future__ import annotations

import os

from trueforge_sdk import TrueForge


def create_trueforge_client() -> TrueForge:
    kwargs: dict = {
        "base_url": os.environ.get("TRUEFORGE_BASE_URL", "http://localhost:8790"),
        "timeout": 600,
    }
    token = os.environ.get("TRUEFORGE_TOKEN")
    if token:
        kwargs["token"] = token
    return TrueForge(**kwargs)
