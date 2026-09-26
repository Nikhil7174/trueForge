from __future__ import annotations

import os

from trueforge_hackathon.agents.access_reviewer.backends import backend_name
from trueforge_hackathon.agents.access_reviewer.store import deny_prefixes
from trueforge_hackathon.agents.cost_janitor.plugin import QUESTION_CONTRACT
from trueforge_hackathon.integrations.github_audit import audit_instructions, github_connector, github_mcp_servers
from trueforge_hackathon.skill_catalog import resolve_skills
from trueforge_hackathon.types import AgentPlugin, AgentSpec

ACCESS_REVIEWER_MCP_NAME = "access-reviewer-iam"
ACCESS_REVIEWER_SKILL_NAMES = ("access-review-playbook", "blast-radius")
# Live AWS mode also uses the shared card/question contract.
ACCESS_REVIEWER_AWS_SKILL_NAMES = ACCESS_REVIEWER_SKILL_NAMES + ("aws-hygiene-ui",)
# Every destructive tool, by literal name (never rely on @destructive alone).
ACCESS_REVIEWER_DESTRUCTIVE_TOOLS = ("revoke_access", "detach_role_policy", "put_role_policy")


def _skill_names() -> tuple[str, ...]:
    return ACCESS_REVIEWER_AWS_SKILL_NAMES if backend_name() == "aws" else ACCESS_REVIEWER_SKILL_NAMES


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
            "Do not invent last-used dates. Use the active and inactive lists from get_access_last_used.",
            "Present two tables: Active (used within 90 days — keep) and Inactive (unused — candidates to revoke).",
            "Only propose revoke for inactive attachments. Ask the operator which inactive policy to remove.",
            "revoke_access is destructive. Call it only for a specific inactive attachment the operator named, then wait for approval.",
            "After a revoke is allowed, re-read the principal to confirm.",
        ]
    )


def _aws_instructions() -> str:
    deny = ", ".join(deny_prefixes())
    return "\n".join(
        [
            "You are an access reviewer for one live AWS account. Job: least privilege for IAM roles, backed by evidence, changed only with human approval.",
            "Load the access-review-playbook, aws-hygiene-ui and blast-radius skills and follow them instead of inventing procedure.",
            f"Never propose changes to roles whose names start with: {deny}, nor to AWS service-linked roles. The MCP refuses them anyway.",
            "Delegate evidence gathering to one subagent per role (create_sub_agent): get_role_policies, get_service_last_accessed, "
            "get_role_cloudtrail_activity. Subagents gather and summarise only; they never ask questions or call destructive tools.",
            "Synthesise the least-privilege policy, the diff and the lint in the sandbox with stdlib Python "
            "(/opt/tf/skills/access-review-playbook/scripts/lint_policy.py). The sandbox has no AWS credentials; pass evidence inline.",
            "Weigh telemetry honestly: service-last-accessed lags up to ~4 h, CloudTrail minutes. "
            "'Insufficient evidence, no revocation' is a valid outcome.",
            "Render PolicyDiff and BlastRadius cards, then a PreApproval card right before each gated call "
            f"({', '.join(ACCESS_REVIEWER_DESTRUCTIVE_TOOLS)}), with self-describing arguments that match the card.",
            "If the operator denies, acknowledge the reason in one sentence, change nothing else, and record the denial.",
            audit_instructions(),
            QUESTION_CONTRACT,
        ]
    )


def access_reviewer_mcp_servers() -> list[dict]:
    return [
        {
            "name": ACCESS_REVIEWER_MCP_NAME,
            "enable_tools": ["@all"],
            "require_approval_for_tools": list(ACCESS_REVIEWER_DESTRUCTIVE_TOOLS),
            "preload": True,
        }
    ]


def _manifest(*, include_skills: bool) -> AgentSpec:
    spec: AgentSpec = {
        "model": {"name": os.environ.get("TRUEFORGE_MODEL", "anthropic/claude-sonnet-4-6")},
        "instructions": _aws_instructions() if backend_name() == "aws" else _instructions(),
        "mcp_servers": access_reviewer_mcp_servers() + (github_mcp_servers() if backend_name() == "aws" else []),
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
        spec["skills"] = [{"name": name} for name in _skill_names()]
    return spec


def create_access_reviewer_plugin() -> AgentPlugin:
    skills = resolve_skills(list(_skill_names()))
    attach_skills = all(skill.get("repoUrl") for skill in skills)
    return {
        "name": "access-reviewer",
        "description": (
            "Find unused IAM access, draft a least-privilege diff with blast radius, "
            "and revoke nothing until a human approves."
        ),
        "manifest": _manifest(include_skills=attach_skills),
        "default_message": (
            "Review IAM access for NikhilZynix. Show Active (used within 90 days) and "
            "Inactive (unused) tables with blast radius. Do not revoke until I pick an inactive policy."
        ),
        "mcp": {
            "name": ACCESS_REVIEWER_MCP_NAME,
            "url": _mcp_url(),
            "description": "IAM principals, last-used access, and gated revoke_access.",
        },
        "mcps": [c for c in [github_connector()] if c and backend_name() == "aws"],
        "skills": skills,
        "policy": {"denyPrincipalPrefixes": deny_prefixes()},
    }
