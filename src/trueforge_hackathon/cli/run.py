from __future__ import annotations

import argparse
import sys

import trueforge_hackathon  # noqa: F401  registers plugins
from trueforge_hackathon.adapter.client import create_trueforge_client
from trueforge_hackathon.adapter.run_session import (
    PendingApproval,
    TurnResult,
    pending_actions,
    resume_session,
    run_agent_message,
)
from trueforge_hackathon.env import load_env_file
from trueforge_hackathon.registry import get_plugin, list_plugins


def _print_approvals(pending: list[PendingApproval]) -> None:
    print("\n\n--- approval required ---")
    for item in pending:
        print(f"tool: {item.tool_name}")
        print(f"args: {item.arguments}")


def _report_pending(result: TurnResult, agent: str) -> None:
    """Print what the session is waiting on and how to resume it; exit 2 if anything is pending."""
    if result.pending_questions:
        print("\n\n--- question from the agent ---")
        for q in result.pending_questions:
            print(q.question)
            for option in q.options:
                print(f"  - {option}")
        print(f'Answer with: tf-run --agent {agent} --session {result.session_id} --answer "<your answer>"')
    if result.pending_approvals:
        print(f"Resume with: tf-run --agent {agent} --session {result.session_id} --approve allow|deny")
    if result.pending_approvals or result.pending_questions:
        print(f"\nsession: {result.session_id}")
        raise SystemExit(2)


def _main() -> None:
    load_env_file()
    plugins = list_plugins()
    parser = argparse.ArgumentParser(description="Run one seeded agent turn on TrueForge.")
    parser.add_argument("--agent", default=plugins[0]["name"] if plugins else None)
    parser.add_argument("--message", help="Send a message. Omit with --session to resume a paused session.")
    parser.add_argument("--session")
    parser.add_argument(
        "--approve",
        choices=("allow", "deny"),
        help="Decide tool approvals: pending ones on --session, or any raised during this turn.",
    )
    parser.add_argument("--answer", help="Answer a question the agent asked on --session.")
    args = parser.parse_args()
    if not args.agent:
        raise SystemExit("No agent plugins registered.")

    plugin = get_plugin(args.agent)
    client = create_trueforge_client()
    print(f"Agent: {plugin['name']}")
    if args.session:
        print(f"Session: {args.session}")

    if args.session and args.message is None:
        approvals, questions = pending_actions(client, args.session)
        if approvals:
            _print_approvals(approvals)
            if args.approve is None:
                raise SystemExit("\nThe session is waiting for approval; pass --approve allow or --approve deny.")
            print(f"decision: {args.approve}\n")
        if questions and args.answer is None:
            for q in questions:
                print(f"\nquestion: {q.question}")
            raise SystemExit("The session is waiting for an answer; pass --answer.")
        if not approvals and not questions:
            raise SystemExit("Nothing is pending on this session; pass --message to continue it.")
        result = resume_session(
            client,
            args.session,
            decision=args.approve,
            answer=args.answer,
            deny_reason="denied from CLI",
        )
    else:
        message = args.message or plugin.get("default_message")
        if not message:
            raise SystemExit(f"Pass --message for {plugin['name']}.")
        print(f"Message: {message}\n")
        result = run_agent_message(
            client,
            message=message,
            agent_name=plugin["name"],
            session_id=args.session,
            on_approve=lambda pending: _print_approvals(pending) or (args.approve or "pause"),
            deny_reason="denied from CLI",
        )

    _report_pending(result, plugin["name"])
    print(f"\n\nstatus: {result.status}")
    print(f"session: {result.session_id}")


def main() -> None:
    try:
        _main()
    except SystemExit:
        raise
    except Exception as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
