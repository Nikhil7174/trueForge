"""In-memory stand-in for trueforge_hackathon.agents.ticket_resolver.linear.Linear, shared by the ticket tests."""
from __future__ import annotations

import json

from trueforge_hackathon.agents.ticket_resolver.linear import LinearError

GROUPS = {"Agent": ["Working", "Awaiting approval", "Replied", "Declined", "Failed"],
          "Repro": ["Fixed", "Reproduced, no fix", "Not reproduced"]}


class FakeLinear:
    api_key = "lin_api_fake"

    def __init__(self):
        self.issues: dict[str, dict] = {}
        self.label_ids = {(None, n): f"lbl-{n}" for n in ("Bug", "Feature", "agent-skip")}
        for group, names in GROUPS.items():
            self.label_ids.update({(group, n): f"lbl-{group}-{n}" for n in names})
        self.posted: list[dict] = []

    def add(self, ident: str, title: str, body: str, labels=("Bug",), team="ZYN"):
        self.issues[ident] = {
            "id": f"uuid-{ident}", "identifier": ident, "title": title, "description": body,
            "url": f"https://linear.app/x/issue/{ident}", "createdAt": "2026-09-26T10:00:00Z",
            "updatedAt": "2026-09-26T10:00:00Z", "team": {"id": "t", "key": team},
            "labels": {"nodes": [{"id": self.label_ids[(None, n)], "name": n, "parent": None} for n in labels]},
            "comments": {"nodes": []},
        }

    def issue(self, ref: str) -> dict:
        """By identifier (ZYN-12) or UUID, like the real API."""
        issue = self.issues.get(ref) or next((i for i in self.issues.values() if i["id"] == ref), None)
        if issue is None:
            raise LinearError(f"issue {ref} not found")
        return json.loads(json.dumps(issue))  # a fresh copy, like a real fetch

    def labels(self):
        return self.label_ids

    def set_group_label(self, issue: dict, group: str, name: str):
        nodes = [n for n in self.issues[issue["identifier"]]["labels"]["nodes"]
                 if (n.get("parent") or {}).get("name") != group]
        nodes.append({"id": self.label_ids[(group, name)], "name": name, "parent": {"id": group, "name": group}})
        self.issues[issue["identifier"]]["labels"]["nodes"] = nodes

    def create_comment(self, issue_id: str, body: str) -> dict:
        ident = next(k for k, v in self.issues.items() if v["id"] == issue_id)
        c = {"id": f"c{len(self.posted) + 1}", "body": body, "createdAt": "2026-09-26T11:00:00Z", "user": {"id": "me"}}
        self.issues[ident]["comments"]["nodes"].append(c)
        self.posted.append({"issue": ident, **c})
        return {"id": c["id"], "url": f"https://linear.app/x/issue/{ident}#comment-{c['id']}"}

    def group_label(self, ident: str, group: str) -> str | None:
        return next((n["name"] for n in self.issues[ident]["labels"]["nodes"]
                     if (n.get("parent") or {}).get("name") == group), None)
