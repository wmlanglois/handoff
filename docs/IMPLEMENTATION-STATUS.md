# Implementation status and research connections

Reconciled September 25, 2026 against runtime commit [`221f434`](https://github.com/wmlanglois/handoff/commit/221f434afe5815670a7d72eedf0dba88f6062e1c). This page maps shipped behavior to remaining work; it is not a claim of universal platform support, live endurance success, or measured productivity benefit.

## Shipped corrections

| Area | Current behavior | Tracking |
|---|---|---|
| Review cadence | Q0 does not silently reduce the chosen E3 cadence. | [#2](https://github.com/wmlanglois/handoff/issues/2) |
| First-use choice | Guided questions, brief context, and no-token defer are explicit; paths and project kind are shown. | [#3](https://github.com/wmlanglois/handoff/issues/3) |
| Plan validation | Structural dependency/launcher checks reject inadmissible proposals; rejection can stay on the same goal. | [#4](https://github.com/wmlanglois/handoff/issues/4) |
| Supplied contracts | `--plan-file` imports JSON outcomes instead of asking a model to recreate them. It is not Markdown-plan compilation. | [#5](https://github.com/wmlanglois/handoff/issues/5) |
| Never-rule check | Dispatch prefers the stored contract over a scope-expanded card, avoiding the scope's own prohibition being treated as an action. | [#8](https://github.com/wmlanglois/handoff/issues/8) |
| Delegated map resume | An existing proposal no longer skips the approval step. While no approved map exists, derive from the current approved plan; failed derivation stops. The downstream map validity gate still applies. | [#7](https://github.com/wmlanglois/handoff/issues/7) |
| Registry configuration | Sample has the version/workers envelope and non-reserved names; `FLEET_REGISTRY` permits explicit project-local storage. | [#15](https://github.com/wmlanglois/handoff/issues/15) |
| Returning workers | `connect` checks saved endpoints; failed or unchecked saved entries do not count as ready. | [#16](https://github.com/wmlanglois/handoff/issues/16), duplicate [#17](https://github.com/wmlanglois/handoff/issues/17) |
| Preflight | Autonomous checks selected lanes, named skeptic, and tool service before execution; an empty skeptic produces an explicit warning. | [#12](https://github.com/wmlanglois/handoff/issues/12) |
| Oracle filter | Static AST-based analysis replaces the regex false positive on expressions such as `value == 42 or 1 == 2`. It does not certify full behavioral coverage. | [#18](https://github.com/wmlanglois/handoff/issues/18) |

Issue #18's historical wording conflates existence/shape checks with tautologies. Such checks can fail and may be appropriate to a contract; they are not evidence of all application behavior. Mechanical checks, skeptical questions, acceptance receipts, and integration journeys have different roles.

## Remaining connections

| Issue | What exists | What remains |
|---|---|---|
| [#6](https://github.com/wmlanglois/handoff/issues/6) | Diagnostic for extra uncovered goal criteria after a successful intake-based plan. | Pass authorized seeded criteria into planning or explicitly reject them before model calls; do not leave hidden unplannable state. |
| [#7](https://github.com/wmlanglois/handoff/issues/7) | Within-goal execution, repairs, cumulative goal budget, delegated approval gates. | Durable cross-milestone continuation under one project authorization/budget, including restart and remaining backlog. D does not currently add this over C. |
| [#9](https://github.com/wmlanglois/handoff/issues/9) | Separate-checkout guidance and a manual `run/` fingerprint. | Enforced stable runtime identity/isolation across the controller and all subprocesses. |
| [#11](https://github.com/wmlanglois/handoff/issues/11) | Separate connection helper and documented role settings. | Guided `start` connection/setup and persistent explicit role selection, verified from a clean environment. |
| [#13](https://github.com/wmlanglois/handoff/issues/13) | Delegated approval of an existing scope page. | Confirmed-intake-to-missing-scope generation connected to delegated dispatch; upfront explanation of remaining stops. |
| [#21](https://github.com/wmlanglois/handoff/issues/21) | JSON contract import and Markdown brief-as-context. | Preserve a supplied Markdown backlog, compile dependency-closed batches, retain IDs and account for every item. Execution continuation belongs to #7, not a second scheduler. |

These open issues preserve the distinction between a missing software connection and a missing user plan. A user can supply a complete project design even when the harness cannot yet compile it. The solution need not generate every packet in a single model call: preserve the full backlog while executing bounded connected batches.

## Liveness and operational boundaries

[#14](https://github.com/wmlanglois/handoff/issues/14) remains closed with a coverage clarification. Existing mechanisms include request socket timeouts, the queue subprocess timeout, generation-based health checks, and authorized worker recovery. The added generated-token progress watchdog is on the prefill-lock streaming path, not every transport. No claim here establishes every end-to-end stall/retry scenario. Client timeout does not prove remote execution stopped.

The default autonomous preflight still checks the tool service even for non-tool plans. Its bypass skips all preflight. A configured endpoint, a green port check, or a copied sample does not establish working generation or correct role assignment.

Local regression evidence is distinct from public CI and live deployment evidence. This publication intentionally excludes private tests, fixtures, run logs, machine configuration, and CI workflows. Issue closure is not itself test evidence.

## Research preserved, not silently rewritten

- [Reporter-trap research](RESEARCH-automation-reporter-trap.md): the hypothesis concerns real state-changing progress versus repeated reports. The current planning/continuation gaps above are engineering findings consistent with that concern, not proof of the hypothesis or measured benefit.
- [Agentic-loop field scan](FIELD-SCAN-agentic-loops.md): a dated scan with explicit source-coverage gaps. Its role recommendations and model examples are historical research, not mandatory current configuration.
- [Prior-art comparison](PRIOR-ART.md): the September 19 inventory compares code at that time. Statements that a component is unwired, or that a particular pattern is better, must not be read as a current release guarantee. Use this status page and current source for implementation state.

This reconciliation does not re-verify every external paper, vendor claim, or repository in those research documents. Their citations and original text are retained. Research proposals such as richer memory retrieval, independent critique, and workflow improvements require their own measured evaluations; no blanket claim is made that they are implemented or improve outcomes.

## Updating the record

When a fix ships, link the commit from its issue and state the exact behavior changed, the test boundary, and remaining scope. Keep successful narrow fixes credited. Reopen an issue for unmet acceptance criteria or create a clearly distinct follow-up; avoid duplicate continuation issues and avoid turning “implemented” into “live-validated.”
