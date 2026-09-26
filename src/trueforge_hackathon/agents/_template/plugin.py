from __future__ import annotations

from trueforge_hackathon.types import AgentPlugin


def create_template_plugin() -> AgentPlugin:
    """Copy this folder. Register from `trueforge_hackathon.__init__`."""
    return {
        "name": "template-agent",
        "description": "Copy this folder. Put business logic in MCP + skills, not here.",
        "manifest": {
            "model": {"provider": "openai", "id": "gpt-4.1-mini"},
            "instructions": "You are a stub. Replace this plugin with a real job.",
            "mcp_servers": [],
            "config": {
                "sandbox": {"enabled": True},
                "approvals": {"require_approval_for_tools": []},
            },
        },
    }
