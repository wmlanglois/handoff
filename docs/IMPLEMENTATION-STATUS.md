# Implementation status and issue map

Reconciled September 26, 2026 against runtime [0dc9256](https://github.com/wmlanglois/handoff/commit/0dc9256). [Change rationale](CHANGE-RATIONALE.md) explains why behavior changed; [FEATURES](FEATURES.md) and the [overnight guide](OVERNIGHT.md) describe operation.

**Implemented** means code and an exercised offline path exist. **Live-validated** requires the relevant real environment/model path. An open issue can contain implemented work plus outstanding acceptance criteria. A closed narrow bug does not certify the whole application.

## Planning, startup and continuation

| Tracking | Implemented | Remaining boundary |
|---|---|---|
| [#2–#5](https://github.com/wmlanglois/handoff/issues/5) | Explicit intake choice, requested cadence preserved, structural plan checks, supplied JSON contracts. | Imported contracts still go through gates; arbitrary Markdown is not JSON. |
| [#6](https://github.com/wmlanglois/handoff/issues/6), closed | Machine-checkable seeded goal criteria reach planning with original IDs; batch seed criteria replace intake packet criteria. | Milestone, human-only and empty-text criteria are deliberately not packet-planned. |
| [#7](https://github.com/wmlanglois/handoff/issues/7), open | Overnight controller authorizes and advances dependent packages under a cumulative budget, records stops and resumes durable state. | Live multi-milestone proof remains. Baseline inheritance copies only the last listed dependency, not parallel checkpoint merging; blocked milestones are not reset on rerun. |
| [#9](https://github.com/wmlanglois/handoff/issues/9), closed | CLI autonomous/overnight launch from content-addressed runtime copies, including worker subprocess source. | External settings, inference services and separately started tool service are outside isolation. The manual fingerprint command is narrower. |
| [#11](https://github.com/wmlanglois/handoff/issues/11), open | Registry role persistence and guided/brief start role canaries before intake/planner spending. Missing roles get setup instructions. | Clean-environment live first-use through dispatch remains; not a complete installation wizard. |
| [#13](https://github.com/wmlanglois/handoff/issues/13), open | Delegated startup generates missing scope from confirmed intake and uses scope/plan/map approvals. | Live missing-scope-to-dispatch demonstration remains. Unconfirmed answers/human-only sign-offs still stop. |
| [#21](https://github.com/wmlanglois/handoff/issues/21), open | Structured Markdown items compile into batches, packages and item status; IDs/dependencies retained. | Live planner/gate batch proof remains. Packages recompile at default six-item size; unparsed/not-compiled entries need resolution first. Not a free-form document interpreter. |

Within-goal E3=D is cadence, not cross-milestone authorization. Use the explicit overnight controller for the latter. A supplied 60-item design is not a missing user plan merely because it needs structured conversion.

## Environment, tools and evidence

| Tracking | Implemented | Remaining boundary |
|---|---|---|
| [#15/#16](https://github.com/wmlanglois/handoff/issues/16) | Correct registry envelope, project-local override, saved-worker rechecks. #17 is duplicate. | Registration is not role selection or workload qualification. |
| [#12/#22](https://github.com/wmlanglois/handoff/issues/22) | Preflight and saved tools/chat-only policy through planning/repairs; service required only when used. | Trusted host execution, not a sandbox or quality guarantee. |
| [#25/#28](https://github.com/wmlanglois/handoff/issues/28) | Real service workprobe, identity-keyed reusable readiness, request profiles/pins, runtime/dependency checks. | Full guided qualification, model revision/quantization identity, representative fail/revise and memory retrieval checks remain. Readiness is advisory unless strict mode selected. |
| [#26](https://github.com/wmlanglois/handoff/issues/26), closed | Hash-checked carry content reaches chat-only repairs rather than an inaccessible path. | Essential input must fit; no silent truncation or server retuning. |
| [#27](https://github.com/wmlanglois/handoff/issues/27), open | Finish/usage evidence, request-timeout classification, budget-aware waits, evidence-based ADJUST within ceilings/pins. | Complete taxonomy and equivalent external-runtime behavior remain. Missing provider usage is unknown. |
| [#30](https://github.com/wmlanglois/handoff/issues/30), open | Guarded append/edit, ranged reads, crash-reconciled journals, final-byte receipts, durable conversation, active-context compaction, reference-aware scratch pruning. | Full accept/promote/cleanup lifecycle integration, external-runtime qualification and measured KV-cache behavior remain. Disk checkpoint is not server KV cache. |
| [#31](https://github.com/wmlanglois/handoff/issues/31), open | LiteLLM config/gateway discovery and explicit qualified alias import with environment-referenced credentials. | Real gateway chat/tools interoperability remains; no server reconfiguration or automatic role choice. |
| [#32](https://github.com/wmlanglois/handoff/issues/32), closed | Receipt-bound tools draft coaching, correct architect challenge evidence roots, no repo fallback. | External runtime coaching not implied. SQLite lifecycle rows are not a full transcript. |

## Latest narrow fixes and adjacent gaps

| Issue | Current behavior |
|---|---|
| [#8](https://github.com/wmlanglois/handoff/issues/8) | Scope's own never-rule is not mistaken for a packet's requested action. |
| [#14](https://github.com/wmlanglois/handoff/issues/14) | Existing liveness/timeouts; generated-token watchdog is transport-specific. Client timeout does not establish remote cancellation. |
| [#18](https://github.com/wmlanglois/handoff/issues/18) | Static assertion analysis avoids the old regex false positive; not a behavioral quality certificate. |
| [#33](https://github.com/wmlanglois/handoff/issues/33) | Bundled tools default thinking off; request profiles may override. Hidden reasoning remains evidence, not replayed working context. |
| [#34](https://github.com/wmlanglois/handoff/issues/34) | Bookkeeping excluded from progress; unchanged artifacts/repeated failures do not count as new investigation. |
| [#35](https://github.com/wmlanglois/handoff/issues/35) | Packet wall-clock budget derived rather than fixed at 1,800 seconds; timeout not mislabeled infrastructure unavailability. |
| [#36/#37](https://github.com/wmlanglois/handoff/issues/37) | Chat/tools share 4,096-token default; bounded low/medium thinking cutoffs may be adjusted within policy. Output allowance is not context capacity. |
| [#38](https://github.com/wmlanglois/handoff/issues/38) | Authorized absolute observation globs normalized; unsupported/path-escape cases do not crash the controller. |
| [#39](https://github.com/wmlanglois/handoff/issues/39), open | Callback convention can still be absent from a contract. #42 delivers but does not infer it. |
| [#40](https://github.com/wmlanglois/handoff/issues/40), open | Scheduler does not yet prefer lanes with applicable successful incremental-write readiness. |
| [#41](https://github.com/wmlanglois/handoff/issues/41), closed narrow fix | Journey supports service readiness probe instead of crashing on persistent launch. Separate sensitivity gate still uses exit-based mutant checking; readiness alone is not promotion. Follow-up [#43](https://github.com/wmlanglois/handoff/issues/43) tracks the reproduced connection gap. |
| [#42](https://github.com/wmlanglois/handoff/issues/42), closed | Worker brief carries binding provides/consumer verbatim. Public interfaces are not hidden oracle code. |

## Evidence and research boundaries

Local offline suite at this runtime baseline: **268 passed** before this reconciliation; four additional private characterization checks reproduced partial packaging, last-dependency-only inheritance, blocked-resume behavior and the service sensitivity gap. Characterization checks confirm limits, not fixes. Publication excludes private tests, fixtures, run logs, machine configuration and CI. This does not demonstrate every transport, OS or live overnight workflow.

[#24](https://github.com/wmlanglois/handoff/issues/24) closed as an evaluation report, not a working-app milestone: reported chat met 2/4 criteria, tools 3/4, neither delivered the launched application. Different thinking settings confounded comparison. It supports tool-loop viability, not a general tools/thinking quality ranking.

[Research](RESEARCH-automation-reporter-trap.md), [field scan](FIELD-SCAN-agentic-loops.md) and [prior art](PRIOR-ART.md) retain historical findings and citations. Add dated interpretation; do not rewrite observations as if current fixes were present then. External sources were not re-researched for this reconciliation.

Issue updates should name behavior, commit, test boundary and remaining acceptance criteria. Keep narrow fixes credited; link genuine follow-ups. Do not equate open validation issues with absent implementation, or helper tests with proven complete workflows.
