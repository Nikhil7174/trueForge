from __future__ import annotations

import os

from trueforge_hackathon.agents.cost_janitor.policy import DESTRUCTIVE_TOOLS, default_region, deny_resource_prefixes
from trueforge_hackathon.skill_catalog import resolve_skills
from trueforge_hackathon.types import AgentPlugin, AgentSpec

COST_JANITOR_MCP_NAME = "cost-janitor-aws"
COST_JANITOR_SKILL_NAMES = ("cost-janitor", "aws-hygiene-ui")

# Shared with other AWS hygiene plugins: the question contract must sit in the agent
# instructions (the skill body loads lazily; see docs/spikes.md #11).
QUESTION_CONTRACT = "\n".join(
    [
        "Questions (ask_user_question), only after discovery:",
        "- 2-4 options. Exactly one is '<label> (Recommended: <reason citing evidence>)'; each other option ends with ' — <trade-off>'.",
        "- The question's last line is exactly: 'Or type your own instructions.'",
        "- If the reply is not one of the options, restate your understanding in one sentence, then call ask_user_question again "
        "in the same turn with a reworded question and updated options that reflect the user's words (again exactly one Recommended). "
        "Do not act until the user picks.",
        "- If the reply is an option plus extra text, honour both and restate the combined constraint in the plan.",
    ]
)


def _mcp_url() -> str:
    return os.environ.get("COST_JANITOR_MCP_URL", "http://127.0.0.1:8766/mcp")


def _instructions() -> str:
    return "\n".join(
        [
            f"You are a cost janitor for one AWS account. Default region: {default_region()}.",
            "Job: find resources that cost money for nothing, prove it with evidence, price them live, back them up, "
            "and remove them only through approval-gated tools.",
            "Load the cost-janitor and aws-hygiene-ui skills and follow them instead of inventing procedure.",
            "Every table and card follows the aws-hygiene-ui card contract.",
            "Do all arithmetic in the sandbox with stdlib Python. The sandbox has no AWS credentials, so pass data inline.",
            "Never type a price from memory; use get_price.",
            "Protected resources (env=prod/production, keep=true, do-not-delete, name prefixes "
            f"{', '.join(deny_resource_prefixes()) or 'none'}) are shown in findings but never acted on.",
            f"Destructive tools ({', '.join(DESTRUCTIVE_TOOLS)}) pause for human approval. Before each call, render a PreApproval card, "
            "then call it with self-describing arguments that match the card. One resource per call.",
            "If the server returns refused, quote it and stop for that resource. If the operator denies, acknowledge the reason and change nothing else.",
            "After every mutation, verify with the matching list tool.",
            QUESTION_CONTRACT,
        ]
    )


def cost_janitor_mcp_servers() -> list[dict]:
    return [
        {
            "name": COST_JANITOR_MCP_NAME,
            "enable_tools": ["@all"],
            "require_approval_for_tools": list(DESTRUCTIVE_TOOLS),
            "preload": True,
        }
    ]


def _manifest(*, include_skills: bool) -> AgentSpec:
    spec: AgentSpec = {
        "model": {"name": os.environ.get("TRUEFORGE_MODEL", "openai/gpt-5-5")},
        "instructions": _instructions(),
        "mcp_servers": cost_janitor_mcp_servers(),
        "config": {
            "sandbox": {"enabled": True, "file_downloads": True},
            "generative_ui": {"enabled": True},
            "ask_user_questions": {"enabled": True},
            "dynamic_sub_agents": {"enabled": True},
            "context_management": {
                "compaction": {"enabled": True},
                "large_tool_response": {"enabled": True},
            },
            "iteration_limit": 80,
        },
    }
    if include_skills:
        spec["skills"] = [{"name": name} for name in COST_JANITOR_SKILL_NAMES]
    return spec


def create_cost_janitor_plugin() -> AgentPlugin:
    skills = resolve_skills(list(COST_JANITOR_SKILL_NAMES))
    attach_skills = all(skill.get("repoUrl") for skill in skills)
    return {
        "name": "cost-janitor",
        "description": (
            "Finds AWS waste with evidence, prices it live, backs it up, and deletes nothing "
            "until a human approves each resource."
        ),
        "manifest": _manifest(include_skills=attach_skills),
        "mcp": {
            "name": COST_JANITOR_MCP_NAME,
            "url": _mcp_url(),
            "description": "AWS EC2/EBS/EIP inventory, CloudTrail activity, live Price List prices, gated cleanup.",
        },
        "skills": skills,
        "policy": {
            "denyResourcePrefixes": deny_resource_prefixes(),
            "requireApprovalForTools": list(DESTRUCTIVE_TOOLS),
        },
    }
