# TrueForge spikes (2026-09-26)

Each spike ran through the TrueForge API (`trueforge-sdk`, `sessions.create_turn_stream`) against a local
TrueForge on `:8790`. It used an inline agent spec with sandbox, Generative UI, ask-user-questions and dynamic subagents
enabled, and the model `openai/gpt-5-5`.

| # | Question | Answer | Evidence |
|---|---|---|---|
| 1 | TrueForge running, model provider set | **Yes** | `settings.model_providers` → `openai` with `gpt-5-4-mini`, `gpt-5-5`, `gpt-5-6-{luna,sol,terra}` |
| 2 | Daytona configured | **Yes** | `settings.sandbox_providers.get()` → type `daytona`, status `ready`, exec timeout 60 s, auto-stop 5 min |
| 3 | Sandbox exec works | **Yes** | Built-in `exec` tool; `sandbox.created` event (`v1:daytona:default.<uuid>`), about 10 s round trip including provisioning |
| 4 | Sandbox runtime | Python **3.13.15**, Debian glibc 2.36, x86_64 | `python3 -c 'import sys,platform…'` |
| 5 | Sandbox outbound network / pip | **Reachable** (`https://pypi.org/simple/` → HTTP 200) | We still use the **stdlib only** so the flow does not depend on network availability |
| 6 | Cloud credentials in the sandbox | **None found** | The agent's own credential-indicator check reported `matched_indicator_count=0` for AWS/cloud credential names. The model refuses to print env var names, so this is indirect |
| 7 | OpenUI renders a table and a card | **Yes** | The model emitted a fenced ```` ```openui ```` block with `Table([Col…])` and `Card([CardHeader…, TextContent…])` |
| 8 | Collapsible component exists | **Yes: `Accordion` / `AccordionItem(value, trigger, content[])`** | Listed as "Collapsible sections" in `get_openui_instructions`. `Tabs`, `Steps`, `Tag`, `Callout`, `TextCallout`, `Modal` and several charts also exist |
| 9 | How the model learns OpenUI | Built-in tool **`get_openui_instructions`**, called at the start of the turn | Full text captured in [openui-reference.md](openui-reference.md) |
| 10 | `ask_user_question` accepts free text | **Yes** | The turn ends with `tool.response_required`. We resumed with `{"type":"user.tool_response","tool_call_id":…,"content":"<arbitrary text>"}` and the model received it |
| 11 | Free text triggers a rewritten re-ask | **Only when the rule is in the agent's system instructions** | With the rule placed only in the user message, the model restated the answer and stopped. With the rule in `instructions`, it restated and called `ask_user_question` again with a reworded question and exactly one `(Recommended: …)` option |

## Consequences for the build

- **Card contract (`skills/aws-hygiene-ui`)**: use `Card` + `CardHeader(title=<kind · summary · risk>)` for the summary line, and
  `Accordion([AccordionItem("details", "<summary>", [Table…])])` for click-to-expand details. Use `Tag` with variants for
  risk (`read` → neutral, `reversible` → warning, `irreversible` → danger). No custom React is needed.
- **Question contract**: the re-ask rule must live in the **agent instructions** (umbrella/plugin `instructions`), not only in
  the skill body, because the skill body loads lazily. Repeat it in the skill for detail.
- **Sandbox**: stdlib-only Python 3.13 works. Network is available but not relied on.
- **Still to check by eye in the UI** (the API cannot show this): that the stock UI shows a free-text box next to the
  options for `ask_user_question`, and how the Accordion looks when rendered. The spike sessions are listed in the TrueForge session list.
