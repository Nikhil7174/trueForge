"""Server-side guardrails for cost-janitor. The prompt cannot override these."""

from __future__ import annotations

import os

PROTECTED_ENV_VALUES = {"prod", "production"}
PROTECT_KEYS = {"do-not-delete", "donotdelete"}

# Listed by literal name in require_approval_for_tools (never rely on @destructive alone).
DESTRUCTIVE_TOOLS = ("delete_volume", "release_elastic_ip", "delete_snapshot", "terminate_instance")


def default_region() -> str:
    return os.environ.get("COST_JANITOR_REGION") or os.environ.get("AWS_REGION") or "us-west-2"


def deny_resource_prefixes() -> list[str]:
    raw = os.environ.get("DENY_RESOURCE_PREFIXES", "prod-,production-")
    return [p.strip() for p in raw.split(",") if p.strip()]


def tags_dict(tags: list[dict[str, str]] | None) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in (tags or [])}


def protection_reason(tags: dict[str, str], name: str | None = None) -> str | None:
    """Return why a resource must never be mutated, or None if it is not protected."""
    lowered = {k.lower(): v.strip().lower() for k, v in tags.items()}
    env = lowered.get("env") or lowered.get("environment")
    if env in PROTECTED_ENV_VALUES:
        return f"tagged env={env}"
    if lowered.get("keep") == "true":
        return "tagged keep=true"
    for key, value in lowered.items():
        if key in PROTECT_KEYS or value in PROTECT_KEYS:
            return f"tagged {key}={value}" if value else f"tagged {key}"
    if name:
        for prefix in deny_resource_prefixes():
            if name.lower().startswith(prefix.lower()):
                return f"name starts with protected prefix '{prefix}'"
    return None
