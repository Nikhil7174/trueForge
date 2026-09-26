from __future__ import annotations

import os

from trueforge_hackathon.skill_catalog import resolve_skills
from trueforge_hackathon.types import AgentPlugin, AgentSpec

RELEASE_CAPTAIN_MCP_NAME = "ship-gate"
# The captain carries the rehearsal skill too: when a release is blocked by an unapplied migration,
# the same agent rehearses and applies it rather than handing the user to a second agent.
RELEASE_CAPTAIN_SKILL_NAMES = ("release-captain", "migration-rehearsal", "blast-radius")


def _mcp_url() -> str:
    return os.environ.get("SHIP_PUBLIC_URL") or f"http://localhost:{os.environ.get('SHIP_PORT', '8812')}/mcp"


def _db_gate_url() -> str:
    return os.environ.get("GATE_PUBLIC_URL") or f"http://localhost:{os.environ.get('GATE_PORT', '8811')}/mcp"


def _mcp_headers() -> dict[str, str]:
    token = os.environ.get("GATE_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _instructions() -> str:
    return "\n".join(
        [
            "You are the release captain for a repository whose releases can include Postgres migrations against a production Medicare ACO claims database.",
            "",
            "Your job: prepare a release, prove it builds, and publish it only when the schema it depends on is already in production. Follow the `release-captain` skill exactly:",
            "release_scope -> verify_build.py in the sandbox -> submit_verification -> draft notes -> report -> publish_release only when asked.",
            "",
            "Rules:",
            "- A release is not ready because the tests pass. It is ready when the tests pass AND every migration in scope is `applied`. Check the migration states in release_scope before anything else and say them out loud.",
            "- The gates' verdicts and summaries are authoritative. Quote them; never soften, reword or recompute them.",
            "- A `refused: true` result is a finding to act on, not an error to retry. Never call a refused tool again unchanged, and never look for another route to the same effect.",
            "- Write release notes from the actual commit list. Never invent a change that is not there.",
            "- If an unapplied migration blocks the release, switch to the `migration-rehearsal` skill, rehearse it, and apply it through db-gate's own approval. Then re-read release_scope; do not assume the scope is unchanged.",
            "- Only call publish_release when the user explicitly asks to publish. Pass target_repo and release_summary verbatim. Say plainly that a published release notifies watchers and cannot be quietly withdrawn.",
            "- The sandbox holds no GitHub token and no database credentials. Never try to reach either from it.",
            "- Be concise: can this ship, then evidence, then next step.",
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
                "name": RELEASE_CAPTAIN_MCP_NAME,
                "enable_tools": ["@all"],
                "require_approval_for_tools": ["publish_release"],
                "preload": True,
            },
            {
                # The captain needs db-gate to fix a blocked release, and apply_migration keeps its
                # own approval: two irreversible steps, two separate human decisions.
                "name": "db-gate",
                "enable_tools": ["@all"],
                "require_approval_for_tools": ["apply_migration"],
                "preload": True,
            },
        ],
        "config": {
            "sandbox": {"enabled": True, "file_downloads": True},
            "dynamic_sub_agents": {"enabled": False},
            "context_management": {
                "compaction": {"enabled": True},
                "large_tool_response": {"enabled": True},
            },
            # The nine-step flow (scope, verify, refuse, rehearse, apply, re-scope, publish) does
            # not fit in the rehearsal agent's 80.
            "iteration_limit": 140,
        },
    }
    if include_skills:
        spec["skills"] = [{"name": name} for name in RELEASE_CAPTAIN_SKILL_NAMES]
    return spec


def create_release_captain_plugin() -> AgentPlugin:
    skills = resolve_skills(list(RELEASE_CAPTAIN_SKILL_NAMES))
    attach_skills = all(skill.get("repoUrl") for skill in skills)
    return {
        "name": "release-captain",
        "description": (
            "Cuts releases: reads the commits since the last tag, runs the tests in a sandbox, drafts "
            "notes, and refuses to publish while any migration in the release is unapplied."
        ),
        "manifest": _manifest(include_skills=attach_skills),
        "default_message": (
            "Cut the next release. Scope it, verify the build, and draft the notes. "
            "Do not publish yet."
        ),
        "mcp": {
            "name": RELEASE_CAPTAIN_MCP_NAME,
            "url": _mcp_url(),
            "description": (
                "Repository gatekeeper: release scope, build-verification ledger, approval-gated "
                "tag and publish that refuses unapplied migrations."
            ),
            "headers": _mcp_headers(),
        },
        "skills": skills,
    }
