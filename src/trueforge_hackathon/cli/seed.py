from __future__ import annotations

import argparse
import sys

import trueforge_hackathon  # noqa: F401  registers plugins
from trueforge_sdk import GitSkill, McpServerHeaderAuth, RemoteMcpServerManifest
from trueforge_sdk.core.api_error import ApiError

from trueforge_hackathon.adapter.client import create_trueforge_client
from trueforge_hackathon.env import load_env_file
from trueforge_hackathon.registry import get_plugin, list_plugins
from trueforge_hackathon.skill_catalog import skills_ready_to_register
from trueforge_hackathon.types import AgentPlugin


def _seed_mcp(client, plugin: AgentPlugin) -> None:
    connectors = ([plugin["mcp"]] if plugin.get("mcp") else []) + list(plugin.get("mcps", []))
    for mcp in connectors:
        _seed_connector(client, mcp)


def _seed_connector(client, mcp) -> None:
    headers = mcp.get("headers")
    client.settings.mcp_servers.create_or_update(
        manifest=RemoteMcpServerManifest(
            name=mcp["name"],
            url=mcp["url"],
            description=mcp.get("description", ""),
            auth=McpServerHeaderAuth(headers=headers) if headers else None,
        )
    )
    print(f"MCP connector: {mcp['name']} → {mcp['url']}")


def _seed_skills(client) -> None:
    ready = skills_ready_to_register()
    if not ready:
        print("Skills: skipped. Set SKILL_GIT_URL to this public GitHub/GitLab repo, then re-run seed.")
        return
    for skill in ready:
        client.settings.skills.create_or_update(
            manifest=GitSkill(
                name=skill["name"],
                url=skill["repoUrl"],
                ref=skill.get("ref", "main"),
                path=skill.get("path"),
                description=skill.get("description", ""),
            )
        )
        print(f"Skill: {skill['name']} ({skill.get('path')})")


def _seed_agent(client, plugin: AgentPlugin) -> None:
    try:
        created = client.agents.create(
            name=plugin["name"],
            description=plugin["description"],
            manifest=plugin["manifest"],
        )
        print(f"Agent created: {created.data.name} ({created.data.id})")
        return
    except ApiError as exc:
        if exc.status_code != 409:
            raise

    existing = next(
        (agent for agent in client.agents.list(agent_name=plugin["name"]) if agent.name == plugin["name"]),
        None,
    )
    if existing is None:
        raise RuntimeError(f"Agent {plugin['name']} already exists but could not be found to update.")
    updated = client.agents.update(
        agent_id=existing.id,
        description=plugin["description"],
        manifest=plugin["manifest"],
    )
    print(f"Agent updated: {updated.data.name} ({updated.data.id})")


def main(argv: list[str] | None = None) -> None:
    load_env_file()
    parser = argparse.ArgumentParser(description="Register MCP, skills, and named agents in TrueForge.")
    parser.add_argument("name", nargs="?", help="Seed one plugin. Omit to seed every registered plugin.")
    parsed = parser.parse_args(argv)
    plugins = [get_plugin(parsed.name)] if parsed.name else list_plugins()
    if not plugins:
        raise SystemExit("No plugins registered.")

    client = create_trueforge_client()
    _seed_skills(client)
    for plugin in plugins:
        print(f"\nSeeding {plugin['name']}")
        _seed_mcp(client, plugin)
        _seed_agent(client, plugin)
    print("\nDone. Open TrueForge, pick the agent, and run a review.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc
