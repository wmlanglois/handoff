# Implementation status and research connections

## Local follow-up: skeptic evidence connection (#32)

Bundled tool drafts now have bounded, receipt-checked pre-oracle coaching in the
existing conversation. Architect challenges resolve tools workspaces and include
generation evidence; missing roots report unavailable instead of falling back to
the repo. Structured private review logs complement SQLite job/lock records.
External runtimes have an explicit unsupported-coaching event, not an invented pass.
Offline tests exercise real bundled tool/service edits and receipt resolution with
scripted model/reviewer responses. No live trial was changed by this patch.

## Local follow-up: LiteLLM discovery/import (#31)

`run/litellm_import.py` previews an explicitly supplied YAML/JSON config or gateway
model list and qualifies one selected alias into the existing registry on `--apply`.
Gateway auth uses environment references through chat, preflight and bundled tools.
No server changes, role selection, provider-setting translation or readiness-cache
automation. The [guide](LITELLM-IMPORT.md) lists commands and dependencies. Offline
fake-HTTP integration tests cover the connection paths; no live LiteLLM instance
has been exercised. Publication status belongs in #31; this paragraph does not
claim this local change has been pushed.

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
| Preflight | Autonomous checks selected lanes and named skeptic; tools additionally require lane tool-call capability and the service. An empty skeptic warns. | [#12](https://github.com/wmlanglois/handoff/issues/12), [#22](https://github.com/wmlanglois/handoff/issues/22) |
| Worker tool policy | Explicit `--tool-mode chat-only\|tools` persists through planning, repairs, follow-ons, cards and resume; approved work cannot be silently toggled. | [#22](https://github.com/wmlanglois/handoff/issues/22) |
| Oracle filter | Static AST-based analysis replaces the regex false positive on expressions such as `value == 42 or 1 == 2`. It does not certify full behavioral coverage. | [#18](https://github.com/wmlanglois/handoff/issues/18) |

Issue #18's historical wording conflates existence/shape checks with tautologies. Such checks can fail and may be appropriate to a contract; they are not evidence of all application behavior. Mechanical checks, skeptical questions, acceptance receipts, and integration journeys have different roles.

## Remaining connections

Implemented correction: [#26](https://github.com/wmlanglois/handoff/issues/26)
delivers hash-checked carry text to chat-only repair/revisit requests and protects it from
context trimming. Offline request-boundary tests cover all three repair constructors;
no live repair success is claimed. Worker readiness/profiles remain in
[#25](https://github.com/wmlanglois/handoff/issues/25), and generation termination evidence
remains in [#27](https://github.com/wmlanglois/handoff/issues/27).

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

The #22 patch makes unused-service handling explicit: chat-only plans report `not-needed` without probing the service, while lane/skeptic checks remain. Its bypass still skips all preflight. A configured endpoint, a green port check, or a copied sample does not establish working generation or correct role assignment. Tool policy is authorization, not capability proof or measured quality benefit. Offline regression checks exercise policy propagation, denial, service failure, and actual local tool self-check/write-receipt binding; no live model comparison is claimed.

Local regression evidence is distinct from public CI and live deployment evidence. This publication intentionally excludes private tests, fixtures, run logs, machine configuration, and CI workflows. Issue closure is not itself test evidence.

## Research preserved, not silently rewritten

- [Reporter-trap research](RESEARCH-automation-reporter-trap.md): the hypothesis concerns real state-changing progress versus repeated reports. The current planning/continuation gaps above are engineering findings consistent with that concern, not proof of the hypothesis or measured benefit.
- [Agentic-loop field scan](FIELD-SCAN-agentic-loops.md): a dated scan with explicit source-coverage gaps. Its role recommendations and model examples are historical research, not mandatory current configuration.
- [Prior-art comparison](PRIOR-ART.md): the September 19 inventory compares code at that time. Statements that a component is unwired, or that a particular pattern is better, must not be read as a current release guarantee. Use this status page and current source for implementation state.

This reconciliation does not re-verify every external paper, vendor claim, or repository in those research documents. Their citations and original text are retained. Research proposals such as richer memory retrieval, independent critique, and workflow improvements require their own measured evaluations; no blanket claim is made that they are implemented or improve outcomes.

## Updating the record

Operator-pinned `WORKER_OUTPUT_LIMITS` is shared by coding-worker chat and tool jobs.
Tool-mode delivery feedback no longer asks for fences. The bundled tool loop keeps incomplete
replies as unexecuted evidence, bounds length-stop recovery, and checks estimated context
before generating.

2026-09-25 (local commits, under #25/#28):
- All run-state stores follow `FLEET_RUNS_DIR`, including the jobs/prefill-lock database.
- Preflight names the tool runtime and refuses one that lacks required capabilities. It
  probes the tool host's Python for the modules the plan's checks run.
- Stop-reason evidence is recorded even when a tool loop stops by raising. The architect sees
  it and can `ADJUST` an assignment's output limit within the lane ceiling, unless the limit
  is pinned.
- On a failed check, the skeptic's questions go into the next round.
- Operators can set per-worker request profiles (thinking and sampling) in
  `WORKER_REQUEST_PROFILES`.
- The planner sees lane budgets, and plan review rejects outcomes too large for any lane.
- `run/workprobe.py` probes a lane for code-fence, tool-call and sectioned-write compliance.
- Operators can recover dispatched records from dead controllers (`queue.py recover-stale`).

A live tools-mode trial on the bundled runtime recorded length-limited turns, and the architect
issued an evidence-based ADJUST. Whether the raised limit completes the packet is recorded in the
trial notes, not asserted here. Prompt compaction and scratch garbage collection are not implemented.

Additional local #30 work: guarded append/edit, ranged reads, per-workspace mutation serialization,
request-identity checks and crash-reconciled file journals. Receipt resolution accepts validated
incremental mutation receipts matching current artifact bytes; final full-file retransmission
is unnecessary. Private tests exercise the real tool loop/service with scripted model replies,
seal exact resulting bytes, reject tampering/stale edits and interrupt an append before receipt
publication. Retention/cleanup and prompt compaction are not implemented by this patch.

September 25 environment follow-up:

- [Technical environment guide](ENVIRONMENT-REQUIREMENTS.md) is linked before first-use
  commands. It documents current setup plus clearly marked qualification work still needed.
- [Phase 2 environment proposal](PHASE-2-ENVIRONMENT.md) separates the chat/workbench layer
  from backend inspection and opt-in tuning; it records headroom-calculator constraints.
- #27 now has response-evidence preservation for chat streaming/nonstreaming and the
  bundled tool loop. Provider usage remains unknown when omitted. Transport/budget failure
  classification and architect routing remain open; this is not complete #27 coverage.
- Private offline suite: 121 passing at this checkpoint, including nested carry staged and
  read through the local tool service, with missing/tampered snapshots refused. No live
  generation or server configuration changes were performed. Private tests are not published.

When a fix ships, link the commit from its issue and state the exact behavior changed, the test boundary, and remaining scope. Keep successful narrow fixes credited. Reopen an issue for unmet acceptance criteria or create a clearly distinct follow-up; avoid duplicate continuation issues and avoid turning “implemented” into “live-validated.”
