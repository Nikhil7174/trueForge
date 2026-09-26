from __future__ import annotations

import argparse
import sys

import trueforge_hackathon  # noqa: F401  registers plugins
from trueforge_hackathon.adapter.client import create_trueforge_client
from trueforge_hackathon.adapter.run_session import PendingApproval, run_agent_message
from trueforge_hackathon.env import load_env_file
from trueforge_hackathon.registry import get_plugin, list_plugins

DEFAULT_PROMPT = (
    "Review unused IAM access in this account. Show the unused table with blast radius. "
    "Do not revoke anything yet."
)


def _print_approvals(pending: list[PendingApproval]) -> None:
    print("\n\n--- approval required ---")
    for item in pending:
        print(f"tool: {item.tool_name}")
        print(f"args: {item.arguments}")
    print("Resume with: tf-run --agent access-reviewer --approve allow --session <id>")


def main() -> None:
    load_env_file()
    plugins = list_plugins()
    parser = argparse.ArgumentParser(description="Run one seeded agent turn on TrueForge.")
    parser.add_argument("--agent", default=plugins[0]["name"] if plugins else None)
    parser.add_argument("--message", default=DEFAULT_PROMPT)
    parser.add_argument("--session")
    parser.add_argument("--approve", choices=("allow", "deny"))
    args = parser.parse_args()
    if not args.agent:
        raise SystemExit("No agent plugins registered.")

    plugin = get_plugin(args.agent)
    client = create_trueforge_client()
    print(f"Agent: {plugin['name']}")
    if args.session:
        print(f"Session: {args.session}")
    print(f"Message: {args.message}\n")

    result = run_agent_message(
        client,
        message=args.message,
        agent_name=plugin["name"],
        session_id=args.session,
        on_approve=lambda pending: (
            _print_approvals(pending) or (args.approve if args.approve else "pause")
        ),
        deny_reason="denied from CLI",
    )

    if result.pending_approvals and not args.approve:
        print(f"\nsession: {result.session_id}")
        raise SystemExit(2)

    print(f"\n\nstatus: {result.status}")
    print(f"session: {result.session_id}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc
