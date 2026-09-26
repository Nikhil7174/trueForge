---
name: access-review-playbook
description: Review IAM users and roles for unused or over-broad access, draft a least-privilege diff, and never revoke without a human.
---

# Access review playbook

You review identity access. You do not revoke anything alone.

## Reach

1. Call `list_principals`.
2. For each non-denied principal, call `get_principal_policies` and `get_access_last_used`.
3. If a payload is large, work from the sandbox file preview. Do not paste raw IAM JSON into your reasoning.

## Unused rule

Read `references/unused-rule.md` in this skill for the exact window and deny list.

Never propose revoking:

- Principals whose names start with Admin, BreakGlass, or OrganizationAccountAccessRole
- Policies that were used inside the window
- Anything the MCP already refused

## Diff in the sandbox (Code Mode)

Do not invent the unused list. In the sandbox, write a short Python script that:

1. Loads the tool JSON
2. Filters unused attachments
3. Prints a table: principal, policy, lastUsedAt, unusedServices, blastRadius

Only the printed table enters context.

## Present

Use Generative UI (table + cards) when available:

- Active table (used within 90 days — keep)
- Inactive table (unused — candidates to revoke)
- One card per proposed revocation (use the `blast-radius` skill for how to write the card)

Ask which inactive policy to revoke. Do not propose revoking an active policy.

## Stop

Call `revoke_access` only after the operator asked to revoke a specific unused attachment. That tool is destructive. Follow `blast-radius`, then wait for Allow or Deny. After Allow, call `get_principal_policies` again to confirm the policy is gone.
