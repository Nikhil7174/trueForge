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

- Unused access table
- One card per proposed revocation (use the `blast-radius` skill for how to write the card)

Ask a clarifying question only if two principals match or the operator did not say which unused policy to revoke.

## Stop

Call `revoke_access` only after the operator asked to revoke a specific unused attachment. That tool is destructive. Follow `blast-radius`, then wait for Allow or Deny. After Allow, call `get_principal_policies` again to confirm the policy is gone.

## Live AWS mode (IAM_BACKEND=aws)

This section applies when `get_role_policies` is available. It adds to the steps above.

### Gather evidence with a subagent

Only the root agent asks questions. Subagents gather evidence.

1. Call `list_principals`. Skip denied names and anything the operator did not ask about.
2. For each role in scope, start **one subagent** (`create_sub_agent`) whose instructions are:
   - Call `get_role_policies(role)`, `get_service_last_accessed(role)` and `get_role_cloudtrail_activity(role, 168)`.
   - Return compact JSON: the granted services and actions per policy, the services and actions used with timestamps, the CloudTrail sessions and API calls, and any conditions in the current policies.
   - Do not ask questions and do not call any destructive tool.
3. Merge the subagents' results in the root agent.

### Weigh the evidence honestly

- **Used**: a service has `lastAuthenticated` inside the window, or CloudTrail shows calls.
- **Unused**: the service was granted, but neither source shows use, **and** the observation window is long enough. The role must be older than the window, and service-last-accessed lags up to about 4 hours.
- **Insufficient evidence**: the role is younger than the window, or its only activity is very recent. Say so plainly. "No revocation recommended" is a valid, successful finding.
- CloudTrail event names are not always IAM action names. For example, the `ListBuckets` event maps to `s3:ListAllMyBuckets`. Map them before you build a policy.

### Ask the operator (question contract, root only)

Once the evidence is in, ask which change to prepare. Build the options from the evidence. The recommended option cites it, for example `Detach IAMFullAccess from ci-deployer (Recommended: 0 IAM calls in 7 days; only ec2 used)`. A free-text reply means re-ask with reworded options.

### Synthesise in the sandbox (stdlib only)

1. Write the evidence JSON and the current policy documents to files in the sandbox.
2. Build the least-privilege policy from the actions actually used, scoped as narrowly as the evidence allows.
3. Run `python3 /path/to/skill/scripts/lint_policy.py new.json current_union.json`. The lint has to pass: no wildcard actions, no IAM actions on `*`, no new privilege-escalation actions, nothing the role does not already have, and any condition kept for an action that still needs it. The MCP runs the same lint on `put_role_policy` and refuses violations.
4. Print the diff: removed / added / kept, per service, with evidence of use.

### Present

Render a `PolicyDiff` card (full JSON in the details accordion) and a `BlastRadius` card from the `aws-hygiene-ui` skill: what breaks, what does not, and your confidence.

### Stop at the gate

- Render the `PreApproval` card, then make **one** gated call with matching arguments:
  - `detach_role_policy(role, policy_arn, services_lost, actions_lost_count, blast_radius, rollback, reason)`
  - `put_role_policy(role, policy_name, policy_json, replaces, diff_summary, rollback, reason)`
- Order: put the least-privilege inline policy **before** detaching the broad managed policies it replaces, so the role never loses access it still uses.
- If the call is **denied**: acknowledge the operator's reason in one sentence and make no alternative change. Render an `Outcome` card showing the denial. If an audit issue exists, log it there.
- After an allowed call, re-read with `get_role_policies` to verify.
