# Safety

The MCP server enforces these rules in code. The agent follows them too, so it never plans an action the server will refuse.

## Never touch

- Resources tagged `env` or `environment` = `prod` / `production`.
- Resources tagged `keep=true`, or with a `do-not-delete` key or value.
- Resources whose Name starts with a prefix in `DENY_RESOURCE_PREFIXES` (default `prod-`, `production-`).
- Anything the operator excluded, even if it looks like waste.

A protected resource still appears in the findings with its reason, so the operator sees the guardrail working. If the operator asks you to delete one, explain that the policy refuses it and that you cannot override it. Do not call the tool.

## Before every mutation

- The server **re-reads live state** and refuses on drift: a volume attached since discovery, an address associated, a mismatched name, size or public IP. Treat `refused` as final.
- `delete_volume` requires a **completed** backup snapshot of that same volume.
- Every destructive tool is gated by its literal name in TrueForge's approval list. Render the `PreApproval` card first.
- Click-safe: all mutations are idempotent. `already_absent` means done. Never re-issue a destructive call after an approval to "make sure".

## Evidence discipline

- Never infer waste from cost alone.
- Treat absence of CloudTrail events as weak evidence. Event history covers management events only and lags by minutes.
- State confidence and counter-evidence in the findings.

## Credentials

- The sandbox has no AWS credentials, and must not. Pass it data inline (JSON from tool results), never keys.
- Never echo account secrets, tokens or keys into chat, the issue or the sandbox.
