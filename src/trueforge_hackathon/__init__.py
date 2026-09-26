from trueforge_hackathon.env import load_env_file

# Plugins read env (MCP URLs, tokens, SKILL_GIT_URL) when they are built, so load .env first.
load_env_file()

from trueforge_hackathon.agents.access_reviewer.plugin import create_access_reviewer_plugin  # noqa: E402
from trueforge_hackathon.agents.migration_rehearsal.plugin import create_migration_rehearsal_plugin  # noqa: E402
from trueforge_hackathon.registry import get_plugin, list_plugins, register_plugin  # noqa: E402

register_plugin(create_access_reviewer_plugin())
register_plugin(create_migration_rehearsal_plugin())

__all__ = ["get_plugin", "list_plugins", "register_plugin"]
