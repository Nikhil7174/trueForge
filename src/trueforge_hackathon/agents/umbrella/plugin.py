"""Composite agent: one TrueForge agent built by merging member plugins.

Members come from UMBRELLA_MEMBERS (default "cost-janitor,access-reviewer"; add
"migration-rehearsal" to include it). Connectors, skills, approval lists, deny
policies and instructions are merged; member plugins are not modified.
The agent name is one env var (UMBRELLA_AGENT_NAME) so renaming is one change.
"""

from __future__ import annotations

import os
from typing import Any

from trueforge_hackathon.registry import get_plugin
from trueforge_hackathon.types import AgentPlugin, AgentSpec, McpConnector, SkillRef

DEFAULT_MEMBERS = "cost-janitor,access-reviewer"


def umbrella_agent_name() -> str:
    return os.environ.get("UMBRELLA_AGENT_NAME", "aws-hygiene").strip() or "aws-hygiene"


def umbrella_members() -> list[str]:
    raw = os.environ.get("UMBRELLA_MEMBERS", DEFAULT_MEMBERS)
    return [m.strip() for m in raw.split(",") if m.strip()]


def _merge_mcp_servers(members: list[AgentPlugin]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for plugin in members:
        for server in plugin["manifest"].get("mcp_servers", []):
            cur = merged.setdefault(server["name"], {**server, "enable_tools": [], "require_approval_for_tools": []})
            for key in ("enable_tools", "require_approval_for_tools"):
                for tool in server.get(key, []):
                    if tool not in cur[key]:
                        cur[key].append(tool)
            cur["preload"] = cur.get("preload", False) or server.get("preload", False)
    return list(merged.values())


def _merge_config(members: list[AgentPlugin]) -> dict[str, Any]:
    def merge(a: Any, b: Any) -> Any:
        if isinstance(a, dict) and isinstance(b, dict):
            return {k: merge(a.get(k), b.get(k)) if k in a and k in b else a.get(k, b.get(k)) for k in {**a, **b}}
        if isinstance(a, bool) and isinstance(b, bool):
            return a or b  # a capability any member needs stays enabled
        if isinstance(a, int) and isinstance(b, int):
            return max(a, b)
        return a

    config: dict[str, Any] = {}
    for plugin in members:
        config = merge(config, plugin["manifest"].get("config", {})) if config else dict(plugin["manifest"].get("config", {}))
    return config


def _merge_instructions(name: str, members: list[AgentPlugin]) -> str:
    seen: set[str] = set()
    parts = [
        f"You are {name}, an AWS hygiene agent for a platform team. You combine these jobs: "
        + "; ".join(f"{p['name']} ({p['description']})" for p in members)
        + ".",
        "Pick the job that matches the request and follow its section and skills. Never mix one job's destructive tools into another job's plan.",
    ]
    for plugin in members:
        lines = [ln for ln in plugin["manifest"].get("instructions", "").splitlines() if ln.strip()]
        fresh = [ln for ln in lines if ln not in seen]
        seen.update(lines)
        parts.append(f"\n## {plugin['name']}\n" + "\n".join(fresh))
    return "\n".join(parts)


def create_umbrella_plugin() -> AgentPlugin:
    name = umbrella_agent_name()
    members = [get_plugin(m) for m in umbrella_members()]

    connectors: dict[str, McpConnector] = {}
    skills: dict[str, SkillRef] = {}
    policy: dict[str, Any] = {"members": [p["name"] for p in members]}
    for plugin in members:
        for conn in ([plugin["mcp"]] if plugin.get("mcp") else []) + list(plugin.get("mcps", [])):
            connectors.setdefault(conn["name"], conn)
        for skill in plugin.get("skills", []):
            skills.setdefault(skill["name"], skill)
        policy[plugin["name"]] = plugin.get("policy", {})

    first = members[0]["manifest"]
    manifest: AgentSpec = {
        "model": first["model"],
        "instructions": _merge_instructions(name, members),
        "mcp_servers": _merge_mcp_servers(members),
        "config": _merge_config(members),
    }
    member_skills = [s for p in members for s in p["manifest"].get("skills", [])]
    if member_skills:
        manifest["skills"] = [{"name": n} for n in dict.fromkeys(s["name"] for s in member_skills)]

    all_connectors = list(connectors.values())
    return {
        "name": name,
        "description": "AWS hygiene agent: cleans up waste and right-sizes IAM on live AWS, with evidence, "
        "sandboxed analysis and human approval on every irreversible action.",
        "manifest": manifest,
        **({"mcp": all_connectors[0], "mcps": all_connectors[1:]} if all_connectors else {}),
        "skills": list(skills.values()),
        "policy": policy,
    }
