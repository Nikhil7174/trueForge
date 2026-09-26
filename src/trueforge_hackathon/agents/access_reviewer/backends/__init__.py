"""IAM backends for the access-reviewer MCP. IAM_BACKEND=fixture (default) | aws."""

from __future__ import annotations

import os
from functools import lru_cache


def backend_name() -> str:
    return os.environ.get("IAM_BACKEND", "fixture").strip().lower() or "fixture"


@lru_cache(maxsize=1)
def get_backend():
    name = backend_name()
    if name == "aws":
        from trueforge_hackathon.agents.access_reviewer.backends.aws import AwsIamBackend

        return AwsIamBackend()
    if name == "fixture":
        from trueforge_hackathon.agents.access_reviewer.backends.fixture import FixtureBackend

        return FixtureBackend()
    raise ValueError(f"IAM_BACKEND must be 'fixture' or 'aws', got {name!r}")
