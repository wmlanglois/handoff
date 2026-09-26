# Why the September 25–26 connections changed

Reconciled against `0dc9256`. This record connects observed failure patterns to implemented changes and outstanding work. It does not expose private run content, machine settings or credentials. [Current status](IMPLEMENTATION-STATUS.md) is authoritative for implementation; [research](RESEARCH-automation-reporter-trap.md) retains the historical argument and sources.

## The complete user plan was not reaching execution

A supplied backlog could be reduced to a few intake-derived criteria. Adding goal criteria did not help when the planner never received them. Requiring another approval at every predictable transition also contradicted bounded walkaway delegation.

The response is a chain of existing mechanisms, not a replacement scheduler: explicit registry roles/start checks (#11), confirmed-intake scope generation under delegation (#13), seeded criterion IDs delivered to planning (#6), structured backlog batches (#21), and an authorized multi-milestone wrapper over the conductor (#7). A 60-item plan need not become 60 contracts in one response. Every item must remain accounted for while bounded connected batches execute.

The current wrapper is implementation progress, not proof of an unattended night. Fan-in baseline merging, blocked-milestone recovery and the compiler's partial-output/custom-batch handling need attention; see [operating boundaries](OVERNIGHT.md). Do not explain these as the user failing to provide a plan.

## Model failures were sometimes harness-induced

- A worker told to read a carried file could not do so in chat-only mode. #26 supplies hash-checked carry text instead.
- Tools were advertised without proof they executed. Workprobe now calls the real service and records results (#25/#28). Reusable readiness avoids full re-onboarding for unchanged environments; it is not liveness or permission. Scheduling still does not act on it (#40).
- Tools had a 1,400-token default while chat had 4,096. #36 shares the default. Neither value is a server's total context or a good setting for every model. Pins/ceilings and request profiles remain operator controls.
- Tools inherited server thinking while chat disabled it. #33 aligns the default and excludes old hidden reasoning from working context while retaining evidence. #37 permits evidence-based increases for bounded low/medium thinking. This does not establish that thinking off is universally better.
- Whole-file delivery and truncated tool calls wasted attempts. Incremental receipted append/edit, length-stop evidence and continuation, durable checkpoints, compaction and conservative scratch pruning address separate parts of this (#27/#30).

Handoff should adapt requests to a known environment, not silently tune servers, install runtimes, change GPU allocation or invent model capacity. Server KV caching, retained conversation, disk artifacts and SQLite lifecycle state are distinct. A disk checkpoint does not prove cache reuse; compacting a request may alter its cacheable prefix.

## The evidence driving recovery was misleading

Receipt files were being counted as progress while delivered code and the failure stayed unchanged (#34). A fixed packet timeout looked like a dead worker (#35). Architect challenges pointed skeptics at the wrong folder, and tools drafts skipped the intended coaching turn (#32).

The fixes target measurements and evidence delivery: exclude bookkeeping, classify timeouts, size waits from budgets, show generation evidence, and provide the real workspace. The skeptic remains advisory. Journals, tool checkpoints and JSONL hold details SQLite job rows do not. Diagnosing the right layer comes before spending another retry.

## The interface existed but the worker could not see it

#39 found underspecified callback conventions. #42 then exposed an even earlier delivery gap: even corrected provides/consumer fields never appeared in ordinary worker briefs. The brief now includes them verbatim as binding interfaces. That is not exposing the hidden oracle; it is supplying the requirement. #39 still needs plan-time validation when the contract itself is incomplete.

## A running service was treated like a failed batch command

#41 fixes the journey crash when a correct server does not exit: an explicit probe can verify readiness. But the later mutation sensitivity check remains exit-based. [#43](https://github.com/wmlanglois/handoff/issues/43) records the reproduced follow-through gap. Keep the narrow crash fix credited and test service-to-promotion separately; a green health endpoint is not proof that the delivered business behavior works.

## Concurrent development changed the runtime under tests

#9 snapshots harness source for CLI autonomous/overnight launches, including worker subprocess files. This isolates code, not the whole environment: settings, a separately started service and remote servers remain external. Future launches can use a new snapshot. Record the runtime identity with evidence rather than assuming a Git commit alone identifies dirty-tree execution.

## What the evaluation actually supports

The #24 report describes genuine multi-turn file/check/edit activity and more criteria completed in its tools arm, but neither arm launched the application. Thinking and output settings differed. It demonstrates a viable tool loop and specific harness faults, not a controlled comparison of tools or thinking quality. A matched follow-up should hold contracts, lane, settings, budgets and evaluation criteria constant.

The research direction remains: give smaller models executable tools, accessible inputs, explicit interfaces and bounded opportunities to revise; measure accepted integrated behavior rather than activity or persuasive reporting. Ship those mechanisms, record missing connections as issues, and keep implementation evidence separate from live effectiveness claims.

## Next work, without restarting the design

1. Complete interface specification and readiness-aware routing (#39/#40), so workers get the actual requirement and capability evidence affects assignment.
2. Close compiler packaging and continuation recovery/fan-in gaps (#21/#7) before describing the entire supplied backlog as automatically preserved through execution.
3. Connect service behavior through completion/promotion (#43), preserving #41's crash fix.
4. Run the existing backlog-to-overnight path on a bounded real workload, recording source snapshot, settings identity, accepted artifacts, promoted checkpoints and remaining items. Keep #11/#13/#21/#7 open for their exact live acceptance work, not because their implementations are absent.
5. Finish qualification/storage/memory and cleanup lifecycle coverage (#25/#28/#30), and real LiteLLM interoperability (#31). Do not turn this into a new scheduler, server installer or dashboard prerequisite.
