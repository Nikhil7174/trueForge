---
name: aws-hygiene-ui
description: Card and question contract for AWS hygiene work. Use it for every findings table, cost table, plan, policy diff, blast radius, pre-approval and outcome card, and for every ask_user_question call.
---

# AWS hygiene UI contract

Every artifact of the same kind must look the same in every run, whether it is about EBS, Elastic IPs or IAM. Render with Generative UI (OpenUI). Call `get_openui_instructions` once per session if you have not already. Use only stock components. Never invent component names.

## Card kinds

There are seven kinds: `FindingsTable`, `CostTable`, `PlanCard`, `PolicyDiff`, `BlastRadius`, `PreApproval` and `Outcome`.

Every card has the same frame:

1. A **summary card**. `CardHeader(title, subtitle)`:
   - title: `<Kind> · <one-line summary> · <risk>`
   - risk is exactly one of `read`, `reversible` or `irreversible`
   - subtitle: region/account scope and the evidence source, for example `us-west-2 · live EC2 + Price List API`.
2. A `TextCallout` that states the risk. Use `neutral` for `read`, `warning` for `reversible` and `danger` for `irreversible`. Its title is the risk word. Its description is one sentence saying what happens if nothing else is done.
3. The **key table** with the fixed columns listed below, in that order. Never add, drop or reorder columns. Put `—` in a cell with no value.
4. Optional **details**. After the card, render an `Accordion` with one `AccordionItem("details", "Details and evidence", [...])` holding raw evidence (CloudTrail events, price SKUs, policy JSON as a `CodeBlock`).

`Accordion` is not allowed inside `Card`. Always wrap the card and the accordion in a `Stack`.

Template (replace the values, keep the structure):

```openui
root = Stack([card, details], "column", "s")
card = Card([header, risk, tbl])
header = CardHeader("CostTable · 5 resources, $36.60/month · read", "us-west-2 · AWS Price List API")
risk = TextCallout("neutral", "read", "Nothing has changed yet. These are live prices × live sizes.")
tbl = Table([Col("Resource", names), Col("ID", ids), Col("Quantity", qty), Col("Unit", units), Col("USD per unit", unit_prices, "number"), Col("Monthly USD", monthly, "number"), Col("Price source", skus)])
names = ["pg-replica-data-old", "TOTAL"]
ids = ["vol-…", "—"]
qty = [100, "—"]
units = ["GB-Mo", "—"]
unit_prices = [0.10, "—"]
monthly = [10.00, 10.00]
skus = ["SKU … (USW2-EBS:VolumeUsage.gp2)", "—"]
details = Accordion([AccordionItem("details", "Details and evidence", [evidence])])
evidence = MarkDownRenderer("- Price List effective date …\n- Formula: GiB × $/GB-month; IPv4 hourly × 730")
```

### Fixed columns

| Kind | Risk | Columns (in order) |
|---|---|---|
| `FindingsTable` | read | Resource · ID · Type / size · State · Age (days) · Owner / team · Evidence · Protected |
| `CostTable` | read | Resource · ID · Quantity · Unit · USD per unit · Monthly USD · Price source. The last row is `TOTAL` |
| `PlanCard` | the highest risk among its steps | # · Action · Target · Tool · Risk · Rollback |
| `PolicyDiff` | read | Change (`removed` / `added` / `kept`) · Service · Actions · Resource · Evidence of use |
| `BlastRadius` | irreversible | What changes · Who / what breaks · What does not change · Evidence · Confidence |
| `PreApproval` | irreversible | Action · Target · Monthly cost · Backup · Rollback · Blast radius · Evidence |
| `Outcome` | the risk of the action taken | Action · Target · Result · Verified by · Audit issue |

Put a `PolicyDiff`'s full old and new documents in the details accordion as `CodeBlock("json", …)`.

## Before every approval-gated tool call

1. Render a `PreApproval` card for exactly that call.
2. Make the call **in the same turn**, passing the same values in its self-describing arguments: target ID and name, monthly cost, backup ID, reason and rollback. The approver sees those arguments in the native approval prompt, and they must match the card word for word.
3. One gated call per `PreApproval` card. Never batch several targets into one card.
4. After the result, render an `Outcome` card, then verify with a read tool.

If the approval is **denied**: acknowledge the reason in one sentence, render an `Outcome` card with Result `denied by operator: <reason>`, make no alternative change, and log the denial to the audit issue.

## Question contract (`ask_user_question`)

Only the root agent asks. Subagents cannot.

1. **Gather evidence first.** Never ask before discovery has run.
2. Offer 2–4 options. **Exactly one** is formatted `<label> (Recommended: <reason citing evidence>)`. Every other option ends with ` — <short reason or trade-off>`.
3. Never offer an option the evidence makes impossible or irrelevant. For example, don't offer age thresholds when every candidate is the same age. Never mention how the environment was set up.
4. The question text ends with a line saying exactly: `Or type your own instructions.`
5. If the reply **is one of the options**, restate the chosen scope in one sentence and continue.
6. If the reply **is not one of the options** (free text, which means "Other"), restate your understanding in one sentence. Then call `ask_user_question` **again** with a reworded question and updated options that reflect the user's words. Keep exactly one Recommended option with a reason. Do not proceed until the user picks.
7. If the reply is **an option plus extra text**, honour both. Restate the combined constraint in the `PlanCard`.
