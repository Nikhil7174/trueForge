# Skills catalog

TrueForge skills are **git-backed `SKILL.md` packs**, not agent types.

- Register once: Settings → Skills (or `tf-seed` after `SKILL_GIT_URL` is set).
- Attach by **name** on any agent. One skill can be reused; one agent can attach several.
- Only `name` + `description` enter context. The body and `references/` load from the sandbox when the agent picks the skill (progressive disclosure).
- Skills need the agent **sandbox** on (Daytona).

```text
skills/
  _template/                 # copy-me pack
  access-review-playbook/    # unused IAM + Code Mode + stop before revoke
  blast-radius/              # shared: how to explain irreversible actions
```

Add a pack: copy `_template/`, keep the folder rooted at `SKILL.md`, then attach `{ name }` in the agent plugin.
