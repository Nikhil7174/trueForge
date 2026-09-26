"""Backward-compatible import path. The fixture store now lives in backends/fixture.py."""

from trueforge_hackathon.agents.access_reviewer.backends.fixture import (  # noqa: F401
    AttachedPolicy,
    Principal,
    deny_prefixes,
    get_principal,
    is_denied_principal,
    is_unused,
    list_principals,
    revoke_access,
    unused_after_days,
)
