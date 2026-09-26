from __future__ import annotations

import os

from trueforge_hackathon.skill_catalog import resolve_skills
from trueforge_hackathon.types import AgentPlugin, AgentSpec

MIGRATION_REHEARSAL_MCP_NAME = "db-gate"
MIGRATION_REHEARSAL_SKILL_NAMES = ("migration-rehearsal",)


def _mcp_url() -> str:
    return os.environ.get("GATE_PUBLIC_URL") or f"http://localhost:{os.environ.get('GATE_PORT', '8811')}/mcp"


def _mcp_headers() -> dict[str, str]:
    token = os.environ.get("GATE_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _instructions() -> str:
    return "\n".join(
        [
            "You are the migration-rehearsal agent for a production Postgres database (a Medicare ACO claims database: patients keyed by MBI, their claims, and ICD-10-CM diagnoses).",
            "",
            "Your job: before any migration reaches production, prove what it does to the real data. Follow the `migration-rehearsal` skill exactly:",
            "snapshot production with db-gate export_snapshot -> restore and run the migration in the sandbox with rehearse.py -> investigate with read-only queries -> submit_rehearsal -> report.",
            "",
            "Rules:",
            "- The gate's verdict (SAFE / REVIEW / BLOCK) and summary are authoritative. Quote them; never soften, reword or recompute them.",
            "- Back every claim with numbers and example rows from your sandbox queries.",
            "- For BLOCK or REVIEW, explain the root cause in domain terms and propose a safer migration as a new, clearly named file.",
            "- Only call apply_migration when the user explicitly asks to apply. Pass the rehearsed SQL, target_database and the gate summary verbatim. Set accept_review=true only when the user has explicitly accepted a REVIEW verdict's findings.",
            "- The sandbox has no production credentials. Never try to connect to production from it.",
            "- Be concise: verdict first, then evidence, then next step.",
        ]
    )


def _manifest(*, include_skills: bool) -> AgentSpec:
    spec: AgentSpec = {
        "model": {
            "name": os.environ.get("TRUEFORGE_MODEL", "anthropic/claude-sonnet-4-6"),
            "params": {"reasoning_effort": "medium"},
        },
        "instructions": _instructions(),
        "mcp_servers": [
            {
                "name": MIGRATION_REHEARSAL_MCP_NAME,
                "enable_tools": ["@all"],
                "require_approval_for_tools": ["apply_migration"],
                "preload": True,
            }
        ],
        "config": {
            "sandbox": {"enabled": True, "file_downloads": True},
            "dynamic_sub_agents": {"enabled": False},
            "context_management": {
                "compaction": {"enabled": True},
                "large_tool_response": {"enabled": True},
            },
            "iteration_limit": 80,
        },
    }
    if include_skills:
        spec["skills"] = [{"name": name} for name in MIGRATION_REHEARSAL_SKILL_NAMES]
    return spec


def create_migration_rehearsal_plugin() -> AgentPlugin:
    skills = resolve_skills(list(MIGRATION_REHEARSAL_SKILL_NAMES))
    attach_skills = all(skill.get("repoUrl") for skill in skills)
    return {
        "name": "migration-rehearsal",
        "description": (
            "Rehearses Postgres migrations on a real copy of production in a sandbox, diffs every row, "
            "and applies them only through an approval-gated tool."
        ),
        "manifest": _manifest(include_skills=attach_skills),
        "mcp": {
            "name": MIGRATION_REHEARSAL_MCP_NAME,
            "url": _mcp_url(),
            "description": (
                "Production Postgres gatekeeper: read-only snapshots, rehearsal ledger, approval-gated apply."
            ),
            "headers": _mcp_headers(),
        },
        "skills": skills,
    }
