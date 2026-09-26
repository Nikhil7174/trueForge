from __future__ import annotations

import os

from trueforge_hackathon.agents.access_reviewer.store import deny_prefixes
from trueforge_hackathon.skill_catalog import resolve_skills
from trueforge_hackathon.types import AgentPlugin, AgentSpec

ACCESS_REVIEWER_MCP_NAME = "access-reviewer-iam"
ACCESS_REVIEWER_SKILL_NAMES = ("access-review-playbook", "blast-radius")


def _mcp_url() -> str:
    return os.environ.get("ACCESS_REVIEWER_MCP_URL", "http://127.0.0.1:8765/mcp")


def _instructions() -> str:
    deny = ", ".join(deny_prefixes())
    return "\n".join(
        [
            "You are an access reviewer for one AWS account (or the local IAM fixture).",
            "Job: list principals, find unused or over-broad policy attachments, draft a least-privilege diff, and revoke nothing alone.",
            f"Never propose or attempt revoke on principals whose names start with: {deny}.",
            "Use list_principals, get_principal_policies, and get_access_last_used first.",
            "Load the access-review-playbook and blast-radius skills when they are attached. Follow them instead of inventing procedure.",
            "Compute the unused set in the sandbox / Code Mode. Do not invent last-used dates or diffs.",
            "Present unused access as a table. Each proposed revocation must include blast radius.",
            "revoke_access is destructive. Call it only for a specific unused attachment the operator asked to revoke, then wait for approval.",
            "After a revoke is allowed, re-read the principal to confirm.",
        ]
    )


def _manifest(*, include_skills: bool) -> AgentSpec:
    spec: AgentSpec = {
        "model": {"name": os.environ.get("TRUEFORGE_MODEL", "anthropic/claude-sonnet-4-6")},
        "instructions": _instructions(),
        "mcp_servers": [
            {
                "name": ACCESS_REVIEWER_MCP_NAME,
                "enable_tools": ["@all"],
                "require_approval_for_tools": ["revoke_access"],
                "preload": True,
            }
        ],
        "config": {
            "sandbox": {"enabled": True, "file_downloads": True},
            "generative_ui": {"enabled": True},
            "ask_user_questions": {"enabled": True},
            "dynamic_sub_agents": {"enabled": True},
            "context_management": {
                "compaction": {"enabled": True},
                "large_tool_response": {"enabled": True},
            },
            "iteration_limit": 50,
        },
    }
    if include_skills:
        spec["skills"] = [{"name": name} for name in ACCESS_REVIEWER_SKILL_NAMES]
    return spec


def create_access_reviewer_plugin() -> AgentPlugin:
    skills = resolve_skills(list(ACCESS_REVIEWER_SKILL_NAMES))
    attach_skills = all(skill.get("repoUrl") for skill in skills)
    return {
        "name": "access-reviewer",
        "description": (
            "Find unused IAM access, draft a least-privilege diff with blast radius, "
            "and revoke nothing until a human approves."
        ),
        "manifest": _manifest(include_skills=attach_skills),
        "mcp": {
            "name": ACCESS_REVIEWER_MCP_NAME,
            "url": _mcp_url(),
            "description": "IAM principals, last-used access, and gated revoke_access.",
        },
        "skills": skills,
        "policy": {"denyPrincipalPrefixes": deny_prefixes()},
    }
