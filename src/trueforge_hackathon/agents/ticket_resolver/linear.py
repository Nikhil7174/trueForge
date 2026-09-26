"""Minimal Linear GraphQL client for ticket-gate and the trigger. Holds LINEAR_API_KEY; nothing else does."""
from __future__ import annotations

import os
from typing import Any

import httpx

API_URL = "https://api.linear.app/graphql"

ISSUE_FIELDS = """
  id identifier title description url createdAt updatedAt
  team { id key }
  labels { nodes { id name parent { id name } } }
  comments(first: 100) { nodes { id body createdAt user { id } } }
"""


class LinearError(RuntimeError):
    pass


class Linear:
    def __init__(self, api_key: str | None = None, team_key: str | None = None):
        self.api_key = api_key or os.environ.get("LINEAR_API_KEY", "")
        self.team_key = team_key or os.environ.get("LINEAR_TEAM_KEY", "ZYN")
        self._viewer_id: str | None = None
        self._labels: dict[tuple[str | None, str], str] | None = None

    def gql(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        if not self.api_key:
            raise LinearError("LINEAR_API_KEY is not set")
        try:
            r = httpx.post(API_URL, json={"query": query, "variables": variables or {}},
                           headers={"Authorization": self.api_key, "Content-Type": "application/json"}, timeout=20)
        except httpx.HTTPError as e:
            raise LinearError(f"Linear request failed: {e}") from e
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or data.get("errors"):
            msg = "; ".join(e.get("message", "") for e in data.get("errors") or []) or r.text[:300]
            raise LinearError(f"Linear API error ({r.status_code}): {msg}")
        return data["data"]

    # ------------------------------------------------------------ reads

    def viewer_id(self) -> str:
        if self._viewer_id is None:
            self._viewer_id = self.gql("{ viewer { id } }")["viewer"]["id"]
        return self._viewer_id

    def issue(self, id_or_identifier: str) -> dict:
        """Accepts a UUID or an identifier like ZYN-12."""
        data = self.gql(f"query($id: String!) {{ issue(id: $id) {{ {ISSUE_FIELDS} }} }}", {"id": id_or_identifier})
        if not data.get("issue"):
            raise LinearError(f"issue {id_or_identifier} not found")
        return data["issue"]

    def issues_created_since(self, since_iso: str, label: str) -> list[dict]:
        data = self.gql(
            f"""query($team: String!, $since: DateTimeOrDuration!, $label: String!) {{
                  issues(first: 50, orderBy: createdAt, filter: {{
                    team: {{ key: {{ eq: $team }} }}, createdAt: {{ gt: $since }},
                    labels: {{ name: {{ eqIgnoreCase: $label }} }} }}) {{ nodes {{ {ISSUE_FIELDS} }} }} }}""",
            {"team": self.team_key, "since": since_iso, "label": label},
        )
        return sorted(data["issues"]["nodes"], key=lambda i: i["createdAt"])

    def labels(self) -> dict[tuple[str | None, str], str]:
        """(group name or None, label name) -> label id, for the team's and the workspace's labels."""
        if self._labels is None:
            data = self.gql(
                """query($team: String!) { issueLabels(first: 250, filter: { or: [
                     { team: { key: { eq: $team } } }, { team: { null: true } } ] }) {
                     nodes { id name parent { name } } } }""",
                {"team": self.team_key},
            )
            self._labels = {((n["parent"] or {}).get("name"), n["name"]): n["id"] for n in data["issueLabels"]["nodes"]}
        return self._labels

    # ------------------------------------------------------------ writes

    def set_group_label(self, issue: dict, group: str, name: str) -> None:
        """Put exactly one label of `group` on the issue (Linear allows one per group), keeping all others."""
        label_id = self.labels().get((group, name))
        if label_id is None:
            raise LinearError(f"label {group} → {name} does not exist in team {self.team_key}")
        keep = [n["id"] for n in issue["labels"]["nodes"] if (n.get("parent") or {}).get("name") != group]
        self.gql(
            "mutation($id: String!, $labels: [String!]) { issueUpdate(id: $id, input: { labelIds: $labels }) { success } }",
            {"id": issue["id"], "labels": keep + [label_id]},
        )

    def create_comment(self, issue_id: str, body: str) -> dict:
        data = self.gql(
            "mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success comment { id url } } }",
            {"input": {"issueId": issue_id, "body": body}},
        )
        if not data["commentCreate"]["success"]:
            raise LinearError("commentCreate returned success=false")
        return data["commentCreate"]["comment"]


def label_names(issue: dict) -> list[str]:
    return [n["name"] for n in issue["labels"]["nodes"]]
