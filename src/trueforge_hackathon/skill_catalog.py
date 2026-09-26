from __future__ import annotations

import os

from trueforge_hackathon.types import SkillRef

SKILL_CATALOG: list[SkillRef] = [
    {
        "name": "access-review-playbook",
        "description": (
            "Review IAM users and roles for unused access, draft a least-privilege "
            "diff, never revoke without a human."
        ),
        "path": "skills/access-review-playbook",
    },
    {
        "name": "blast-radius",
        "description": "Before any destructive tool, spell out who and what breaks, then wait for a human.",
        "path": "skills/blast-radius",
    },
    {
        "name": "migration-rehearsal",
        "description": (
            "Rehearse a Postgres migration on a production snapshot in the sandbox, diff every row, "
            "get a gate-verified verdict, and apply only through the approval-gated gate."
        ),
        "path": "skills/migration-rehearsal",
    },
]


def resolve_skill(name: str) -> SkillRef:
    skill = next((s for s in SKILL_CATALOG if s["name"] == name), None)
    if skill is None:
        raise KeyError(
            f'Unknown skill "{name}". Add it under skills/<name>/SKILL.md and skill_catalog.py.'
        )
    resolved: SkillRef = {
        "name": skill["name"],
        "description": skill["description"],
        "path": skill.get("path", ""),
        "ref": os.environ.get("SKILL_GIT_REF", "main"),
    }
    repo = os.environ.get("SKILL_GIT_URL")
    if repo:
        resolved["repoUrl"] = repo
    return resolved


def resolve_skills(names: list[str]) -> list[SkillRef]:
    return [resolve_skill(name) for name in names]


def skills_ready_to_register() -> list[SkillRef]:
    if not os.environ.get("SKILL_GIT_URL"):
        return []
    return [resolve_skill(skill["name"]) for skill in SKILL_CATALOG]
