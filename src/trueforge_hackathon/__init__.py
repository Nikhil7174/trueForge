from trueforge_hackathon.agents.access_reviewer.plugin import create_access_reviewer_plugin
from trueforge_hackathon.registry import get_plugin, list_plugins, register_plugin

register_plugin(create_access_reviewer_plugin())

__all__ = ["get_plugin", "list_plugins", "register_plugin"]
