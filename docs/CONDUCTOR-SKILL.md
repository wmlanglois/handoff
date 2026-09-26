# Conductor skill

Existing LiteLLM setup: follow [the import guide](LITELLM-IMPORT.md) before asking
the user to re-enter all endpoints. Read only their supplied config or gateway;
preview is not authorization to generate. `--apply` explicitly qualifies one alias
and registers it. Keep credentials as environment references, request the usable
context rather than guessing from output caps, and select roles separately. Never
map each load-balanced deployment to a physical worker or claim a preview proves
tool/coding readiness. No server setup/restart or provider-setting rewrite is needed.

Packet completion is not project completion.

Skeptic coaching is advisory. On the bundled tools path, a receipt-bound draft can
receive the three questions and a same-conversation tool revision before the oracle;
never manufacture a tool receipt from chat text or reset turns to obtain a revision.
Missing review roots are unavailable evidence, not proof of no artifact. For ADJUST,
use recorded generation evidence rather than demanding completed code after truncation.
Read the private coaching/challenge JSONL and checkpoint alongside the ordinary
oracle/judge results; SQLite job rows are not a transcript. External runtime coaching
is not implied by support in the bundled runtime.

## Before execution

Read the [ordered setup](../README.md#start-here-one-project-in-order), [command catalog](FEATURES.md), and [current implementation boundaries](IMPLEMENTATION-STATUS.md). Historical research is not a substitute for the current command path.

Establish the project/package paths, existing plan format, model seats, model-call permission, budget, and delegation policy once up front. `registry.py connect` is a separate setup step and does not assign primary/skeptic roles. Do not silently search unrelated projects for settings. No-token defer must remain no-token.

If the user supplied JSON outcome contracts, use the supported `--plan-file` path instead of regenerating them. If the supplied plan is Markdown, preserve every backlog item and explain the conversion boundary (#21); do not call a smaller intake-derived plan the whole project. Criteria added outside intake are not automatically planner inputs (#6).

Interactive mode stops for actual approval. Explicit standing delegation is different: after confirmed intake and generation of `scoped.md`, use `autonomous --delegate <name>` for existing scope, plan, and derived-map approvals, including resume at a proposed map. Do not repeatedly ask for authority the user already gave, or record invented answers/sign-offs. Missing scope generation remains #13. Delegation does not authorize unrelated actions, waived checks, new spending limits, or human-only sign-offs.

E3=D currently shares C's review-pause behavior. It is not a durable cross-milestone controller (#7). Before promising overnight completion, distinguish the approved executable goal from the remaining project backlog and list predictable unsupported transitions. Preserve admissible authorized progress; do not destroy a working proposal merely to demand a larger one.

## Connected-project sequence

Run this sequence. Do not skip to a packet map and call it done.

1. Approved scope. Bind the entire scoped.md. Its hash is the approval. The North Star stays the goal text.
2. Approved interface map. Components, public calls, one owner per file, staged inputs, and a launch command. Approval is interactive or explicitly delegated; stale approvals are not valid. A test fixture must be marked fixture. This map is not a project backlog. Compare the candidate with the approved scope and map; do not silently edit scope or frozen checks.
3. Connected deliverables. A two-dependent-file slice is a useful minimum demonstration, not a universal two-file product limit. Consumers call accepted producer interfaces; checks exercise those signatures.
4. Receipt-bound candidate. After each wave, assemble from accepted worker receipts (`integrate` resolves the hash in `receipt.json`). A second simultaneous writer on a path is refused. A later repair replaces the earlier file.
5. One launchable journey in a fresh process, after the wave, not only when every packet criterion is already met. Packet oracles passing do not pass this gate. DONE also requires that same command to fail when the delivered behavior is replaced by stubs. Promotion records that passing journey.
6. Evidence-based follow-up. An owned repair enters the ordinary queue with its evidence and staged dependencies. Do not claim a new milestone is automatically planned or admitted: that connection remains #7. Journey Spark is only live when `FLEET_SPARK_LIVE=1`; otherwise its `none found` record is not a model review. When invoked, it receives only the accessible evidence; it is not the acceptance authority.
7. Promoted checkpoint after the journey passes.

Operator command: `python run/conductor.py autonomous <package> --decisions N --seconds N --workers <names>`, adding `--delegate <name>` only under standing authorization. Stored budgets persist for the same goal; new goals must not be used to reset an authorized project budget. `approve-map` is the interactive map gate.

Before planning, obtain the operator's execution choice: `start --tool-mode chat-only` or `--tool-mode tools`. Explain that tools execute code on the trusted service host; neither cadence nor delegated approval grants that authority. Save and reuse the choice, including repairs/follow-ons. Coding tools should inspect staged public interfaces and run bounded self-checks, not demand hidden acceptance code. Chat-only briefs must supply the necessary public interface/return-shape context. Do not promise tools will fix a model's mistakes. Legacy packages retain per-contract settings until explicitly migrated before a proposal; never toggle an approved/in-flight contract or reset its budget.

Default autonomous preflight checks selected workers and a named skeptic. Tool plans additionally require tool-call capability on all selected coding lanes and a healthy service. For chat-only plans the service is explicitly not needed and is not probed. An empty skeptic prints a warning. A same-model review is not independent. Do not use `--skip-preflight` to present an unverified environment as ready. A client timeout is not proof remote generation stopped.

Use an isolated runtime checkout and do not edit it during dispatch. `fingerprint` checks `run/` only and is manual, not a guarantee of stable runtime code (#9).

If a frozen check contradicts the approved requirement, stop and record a check defect. A person approves a versioned correction. The old failure stays in the record.

Name the first consequential failure and act on that cause. A missing file is not an assertion failure. DONE can be reopened by a challenge.

Project work enters through `run/conductor.py`; packet-only completion is not the autonomous project milestone.
