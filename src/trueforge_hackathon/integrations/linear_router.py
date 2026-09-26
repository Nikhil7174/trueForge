"""Linear → platform-guardian label router.

Every task starts as a Linear issue. Issues carrying LINEAR_TRIGGER_LABEL are handed to
the platform-guardian agent (issue title + description = the request). The router talks
back on the issue with comments and labels:

  LINEAR_TRIGGER_LABEL (default "platform-guardian")   → picked up, session started
  LINEAR_WAITING_LABEL (default "agent-needs-human")   → paused on a question/approval; answer in TrueForge
  LINEAR_DONE_LABEL    (default "agent-done")          → finished; final answer posted

Questions and approvals are answered by the human in the TrueForge UI (the approval
gate stays there). Run `tf-linear` repeatedly (or `tf-linear --watch`) to pick up new
issues and post results when paused sessions finish.

Env: LINEAR_API_KEY (personal API key), optional LINEAR_TEAM_KEY to limit to one team.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

from trueforge_sdk import SessionAgentNameRef

from trueforge_hackathon.adapter.client import create_trueforge_client
from trueforge_hackathon.adapter.run_session import pending_actions, run_turn
from trueforge_hackathon.env import load_env_file

API = "https://api.linear.app/graphql"
STATE_PATH = Path(".linear-state.json")


def _labels() -> tuple[str, str, str]:
    return (
        os.environ.get("LINEAR_TRIGGER_LABEL", "platform-guardian"),
        os.environ.get("LINEAR_WAITING_LABEL", "agent-needs-human"),
        os.environ.get("LINEAR_DONE_LABEL", "agent-done"),
    )


def _agent_name() -> str:
    from trueforge_hackathon.agents.umbrella.plugin import umbrella_agent_name

    return umbrella_agent_name()


def gql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        raise SystemExit("Set LINEAR_API_KEY in .env")
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Content-Type": "application/json", "Authorization": key},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
    if body.get("errors"):
        raise RuntimeError(f"Linear API error: {body['errors']}")
    return body["data"]


def _issues_with_label(label: str) -> list[dict[str, Any]]:
    team = os.environ.get("LINEAR_TEAM_KEY")
    flt: dict[str, Any] = {"labels": {"name": {"eq": label}}, "state": {"type": {"nin": ["completed", "canceled"]}}}
    if team:
        flt["team"] = {"key": {"eq": team}}
    data = gql(
        """query($f: IssueFilter) { issues(filter: $f, first: 25) { nodes {
             id identifier title description url team { id }
             labels { nodes { id name } } } } }""",
        {"f": flt},
    )
    return data["issues"]["nodes"]


def _label_id(name: str, team_id: str) -> str | None:
    data = gql(
        "query($n: String!) { issueLabels(filter: {name: {eq: $n}}, first: 20) { nodes { id name team { id } } } }",
        {"n": name},
    )
    nodes = data["issueLabels"]["nodes"]
    match = next((n for n in nodes if n["team"] is None or n["team"]["id"] == team_id), None)
    if match:
        return match["id"]
    created = gql(
        "mutation($i: IssueLabelCreateInput!) { issueLabelCreate(input: $i) { issueLabel { id } } }",
        {"i": {"name": name, "teamId": team_id}},
    )
    return created["issueLabelCreate"]["issueLabel"]["id"]


def _set_label(issue: dict[str, Any], add: str | None, remove: list[str]) -> None:
    current = {n["name"]: n["id"] for n in issue["labels"]["nodes"]}
    ids = {i for name, i in current.items() if name not in remove}
    if add:
        ids.add(_label_id(add, issue["team"]["id"]))
    gql(
        "mutation($id: String!, $l: [String!]) { issueUpdate(id: $id, input: {labelIds: $l}) { success } }",
        {"id": issue["id"], "l": sorted(ids)},
    )


def _comment(issue_id: str, body: str) -> None:
    gql("mutation($i: CommentCreateInput!) { commentCreate(input: $i) { success } }", {"i": {"issueId": issue_id, "body": body}})


def _load_state() -> dict[str, Any]:
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _waiting_text(approvals, questions) -> str:
    lines = ["**Waiting for you in TrueForge** (open the session and answer there):"]
    for q in questions:
        lines.append(f"- Question: {q.question}")
        lines += [f"  - {o}" for o in q.options]
    for a in approvals:
        lines.append(f"- Approval needed: `{a.tool_name}` with `{a.arguments[:500]}`")
    return "\n".join(lines)


def _last_output(client, session_id: str) -> str | None:
    turns = list(client.sessions.list_turns(session_id=session_id, limit=25))
    for turn in reversed(sorted(turns, key=lambda t: str(getattr(t, "created_at", "")))):
        out = getattr(getattr(turn, "state", None), "output", None)
        content = getattr(out, "content", None)
        if content:
            return content if isinstance(content, str) else str(content)
    return None


def _report(client, issue: dict[str, Any], session_id: str, result_output: str | None) -> str:
    trigger, waiting, done = _labels()
    approvals, questions = pending_actions(client, session_id)
    if approvals or questions:
        _comment(issue["id"], _waiting_text(approvals, questions) + f"\n\nSession: `{session_id}`")
        _set_label(issue, waiting, remove=[done])
        return "waiting"
    output = result_output or _last_output(client, session_id) or "(no final text)"
    _comment(issue["id"], f"**{_agent_name()} finished.**\n\n{output[:60000]}\n\nSession: `{session_id}`")
    _set_label(issue, done, remove=[waiting])
    return "done"


def run_once() -> None:
    load_env_file()
    trigger, waiting, done = _labels()
    client = create_trueforge_client()
    state = _load_state()
    for issue in _issues_with_label(trigger):
        names = {n["name"] for n in issue["labels"]["nodes"]}
        entry = state.get(issue["id"])
        if entry is None:
            session_id = client.sessions.create(agent=SessionAgentNameRef(name=_agent_name())).data.id
            state[issue["id"]] = {"identifier": issue["identifier"], "session_id": session_id, "status": "running"}
            _save_state(state)
            _comment(issue["id"], f"Picked up by **{_agent_name()}**. TrueForge session `{session_id}`.")
            print(f"{issue['identifier']}: started session {session_id}")
            prompt = f"Linear issue {issue['identifier']}: {issue['title']}\n\n{issue.get('description') or ''}".strip()
            result = run_turn(client, session_id, [{"type": "user.message", "content": prompt}])
            state[issue["id"]]["status"] = _report(client, issue, session_id, result.output)
        elif entry["status"] == "waiting" and done not in names:
            approvals, questions = pending_actions(client, entry["session_id"])
            if approvals or questions:
                continue  # still waiting for the human in TrueForge
            entry["status"] = _report(client, issue, entry["session_id"], None)
        _save_state(state)
        print(f"{issue['identifier']}: {state[issue['id']]['status']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Route labelled Linear issues to the platform-guardian agent.")
    parser.add_argument("--watch", action="store_true", help="poll every 20 s")
    args = parser.parse_args()
    while True:
        run_once()
        if not args.watch:
            return
        time.sleep(20)


if __name__ == "__main__":
    main()
