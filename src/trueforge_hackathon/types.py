from __future__ import annotations

from typing import Any, NotRequired, TypedDict


class AgentSpec(TypedDict):
    model: dict[str, Any]
    instructions: NotRequired[str]
    mcp_servers: NotRequired[list[dict[str, Any]]]
    skills: NotRequired[list[dict[str, Any]]]
    config: NotRequired[dict[str, Any]]


class McpConnector(TypedDict):
    name: str
    url: str
    description: str
    headers: NotRequired[dict[str, str]]


class SkillRef(TypedDict):
    name: str
    description: str
    path: NotRequired[str]
    repoUrl: NotRequired[str]
    ref: NotRequired[str]


class AgentPlugin(TypedDict):
    name: str
    description: str
    manifest: AgentSpec
    mcp: NotRequired[McpConnector]
    skills: NotRequired[list[SkillRef]]
    policy: NotRequired[dict[str, Any]]
