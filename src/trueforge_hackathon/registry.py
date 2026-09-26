from __future__ import annotations

from trueforge_hackathon.types import AgentPlugin

_plugins: dict[str, AgentPlugin] = {}


def register_plugin(plugin: AgentPlugin) -> AgentPlugin:
    name = plugin["name"]
    if name in _plugins:
        raise ValueError(f"Agent plugin already registered: {name}")
    _plugins[name] = plugin
    return plugin


def get_plugin(name: str) -> AgentPlugin:
    plugin = _plugins.get(name)
    if plugin is None:
        known = ", ".join(_plugins) or "(none)"
        raise KeyError(f'Unknown agent plugin "{name}". Registered: {known}')
    return plugin


def list_plugins() -> list[AgentPlugin]:
    return list(_plugins.values())
