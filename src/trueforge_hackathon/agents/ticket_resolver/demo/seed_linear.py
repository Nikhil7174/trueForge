"""Create the three demo bug tickets in Linear (team LINEAR_TEAM_KEY, label LINEAR_TRIGGER_LABEL).

  tf-ticket-seed-demo            create any of the three that don't exist yet (matched by title)
  tf-ticket-seed-demo --dry-run  print them without touching Linear

Every number in a ticket comes from the synthetic aco-api data (see scenarios.py). No real PHI.
"""
from __future__ import annotations

import argparse
import os
import sys

from trueforge_hackathon.env import load_env_file

load_env_file()

from trueforge_hackathon.agents.ticket_resolver.demo import scenarios as sc  # noqa: E402
from trueforge_hackathon.agents.ticket_resolver.linear import Linear, LinearError  # noqa: E402


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Create the ticket-resolver demo tickets in Linear.")
    parser.add_argument("--dry-run", action="store_true", help="print the tickets; don't create anything")
    args = parser.parse_args(argv)

    tickets = sc.scenarios()
    if args.dry_run:
        for t in tickets:
            print(f"--- [{t['expected_outcome']}] {t['title']}\n{t['body']}\n")
        return

    label = os.environ.get("LINEAR_TRIGGER_LABEL", "Bug")
    linear = Linear()
    try:
        label_id = linear.labels().get((None, label))
        if label_id is None:
            sys.exit(f"label {label!r} not found in team {linear.team_key} or the workspace")
        existing = linear.team_issue_titles()
        for t in tickets:
            if t["title"] in existing:
                print(f"exists   {existing[t['title']]}  {t['title']}")
                continue
            issue = linear.create_issue(t["title"], t["body"], [label_id])
            print(f"created  {issue['identifier']}  {t['title']}  (expect {t['expected_outcome']})  {issue['url']}")
    except LinearError as e:
        sys.exit(f"tf-ticket-seed-demo: {e}")


if __name__ == "__main__":
    main()
