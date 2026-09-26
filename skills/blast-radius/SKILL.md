---
name: blast-radius
description: Before any destructive tool, spell out who and what breaks, then wait for a human.
---

# Blast radius

Use this skill whenever you are about to call a destructive MCP tool (`destructiveHint` or named in `require_approval_for_tools`).

For each pending action, state in plain language:

1. **What** — tool name and the exact resource (principal, policy, ARN).
2. **When last used** — timestamp or “never”.
3. **What breaks** — the smallest real consequence if Allow is clicked.
4. **What does not change** — so the operator is not guessing.

Do not call the destructive tool until that card is in the reply. After Allow, re-read the resource to confirm. After Deny, stop.
