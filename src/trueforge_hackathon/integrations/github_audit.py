"""GitHub issues as the append-only audit trail (TrueForge catalog GitHub MCP).

Enabled only when GITHUB_PAT and AUDIT_REPO=owner/name are set. The PAT is sent as a
header on the connector, stored in TrueForge settings, never in the agent manifest.
"""

from __future__ import annotations

import os

from trueforge_hackathon.types import McpConnector

GITHUB_MCP_NAME = "github"
GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
# Creating and commenting on issues is append-only audit, so it is not approval-gated.
AUDIT_TOOLS = ("issue_write", "add_issue_comment", "issue_read")


def audit_repo() -> str | None:
    repo = os.environ.get("AUDIT_REPO", "").strip()
    return repo if "/" in repo and os.environ.get("GITHUB_PAT") else None


def github_connector() -> McpConnector | None:
    token = os.environ.get("GITHUB_PAT")
    if not token or not audit_repo():
        return None
    return {
        "name": GITHUB_MCP_NAME,
        "url": GITHUB_MCP_URL,
        "description": "GitHub issues used as the audit trail for AWS hygiene runs.",
        "headers": {"Authorization": f"Bearer {token}", "X-MCP-Toolsets": "issues"},
    }


def github_mcp_servers() -> list[dict]:
    if not audit_repo():
        return []
    return [
        {
            "name": GITHUB_MCP_NAME,
            "enable_tools": list(AUDIT_TOOLS),
            "require_approval_for_tools": [],
            "preload": True,
        }
    ]


def audit_instructions() -> str:
    repo = audit_repo()
    if not repo:
        return "No audit repository is configured; skip the GitHub audit-issue steps and say so once."
    owner, name = repo.split("/", 1)
    return (
        f"Audit trail: GitHub repository {repo} (owner={owner}, repo={name}). "
        "Open one issue per run with issue_write (method=create) after the cost/finding tables, "
        "then add_issue_comment for backups, each approved or denied action (with the operator's reason), and the verified outcome."
    )
