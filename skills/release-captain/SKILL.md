---
name: release-captain
description: Cut a release - read the commits since the last tag, run the test suite in the sandbox, draft release notes, and publish only through ship-gate's approval step. Refuses to publish while any migration in the release is unapplied. Use whenever a release needs preparing, verifying or publishing.
---

# Release captain

You never tag or publish directly. The repository is reachable for writing only through the
`ship-gate` MCP server, whose one write tool, `publish_release`, pauses for human approval and
refuses anything that was not verified here first.

A release is a compound irreversible action: the tag goes up, CI ships the code, and if a
migration the code depends on has not been applied, production breaks. `publish_release` therefore
refuses while any migration in the release is not `applied` in db-gate's ledger. That is not advice
you can talk it out of; it is a rule in the gate.

Scripts live at `/opt/tf/skills/release-captain/scripts/`. Below, `$S` means that path.

## Procedure

1. **Scope the release:** call `ship-gate.release_scope`. Note `head_sha`, `from_tag`, the commit
   list, and every entry under `migrations` with its `sha256` and `state`.

2. **Read the migration states before doing anything else.** If any is not `applied`, say so now
   and tell the user what it will take. States mean:
   - `applied` — ready, nothing to do
   - `ready_safe` / `ready_needs_review` — rehearsed but not applied; it needs `apply_migration`
     through db-gate, which is a separate human approval
   - `blocked` — the rehearsal found it unsafe; it needs a fixed migration, not an approval
   - `not_rehearsed` — nobody has proven what it does yet

3. **Verify the build in the sandbox:**
   ```
   python3 $S/verify_build.py run --repo <owner/name> --ref <head_sha> --from-tag <from_tag> \
       --migrations <path>=<sha256> ... --commits '<the JSON commits array>'
   ```
   Add `--test-command` or `--install` if the project needs them. A failing suite is a finding, not
   an error: the script still writes a report and you still submit it.

4. **Register the report:** `cat` the report file and pass the parsed JSON object, unmodified, as
   `report_json` to `ship-gate.submit_verification`. The gate recomputes PASS/FAIL itself and
   checks the repository has not moved on.

5. **Draft the release notes.** Notes a human would actually publish: what changed, grouped, in
   plain language, from the commit list. Name any migration in the release and say that it is
   applied. Do not invent changes that are not in the commits.

6. **Report to the user**, leading with whether this release can go out:
   - the gate's verdict and `summary`
   - the migration states, explicitly
   - the notes you drafted
   - if anything blocks publishing, what to do about it

7. **Publish only when asked**, and only on a PASS. Call `ship-gate.publish_release` with:
   - `verification_id`, plus `target_repo` and `release_summary` copied verbatim from
     `submit_verification`'s output
   - `tag` the user asked for, and `notes_md` you drafted

   This pauses for human approval. Before calling it, tell the user in one short block what they
   are about to approve: repository, tag, commit, how many commits it covers, and that a published
   release notifies watchers and cannot be quietly withdrawn.

8. **If an unapplied migration is the blocker**, hand off: follow the `migration-rehearsal` skill to
   snapshot, rehearse and (with its own approval) apply it, then return here and start again at
   step 1. The scope has to be re-read, because applying a migration changes nothing about the
   repository but everything about whether this release may ship.

## Refusals from the gate are findings, not errors

`refused: true` is information, not a transient failure. Never retry a refused call unchanged, and
never work around it by reaching for another tool. Read the reason, tell the user, and fix the
cause. There is no path to production that skips this gate.
