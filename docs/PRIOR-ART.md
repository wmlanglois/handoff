# Prior art vs. this harness

> September 25 implementation note: this September 19 comparison is preserved as a historical research snapshot. Its statements about what is wired are not current release status. See [implementation status and issue mapping](IMPLEMENTATION-STATUS.md) and [operator commands](FEATURES.md) for current behavior. External comparisons below were not re-verified by this documentation reconciliation.

*Comparison written 2026-09-19 against four research lanes that harvested local-LLM orchestration
projects. Every claim about **us** cites a file in this repo that was read to make it. Every claim
about **them** cites the URL the lane fetched it from.*

## Reading rules for this document

1. **I did not fetch any external URL myself.** Everything in the "them" column is as-reported by
   the research lanes. Where a lane flagged its own finding as unverified, I repeat the flag rather
   than launder it into a fact.
2. Specific claims I am **not** treating as established, because the lanes said so themselves:
   - Star counts for `beads` (27.3k), `gastown` (18.1k), `loopx`, `oh-my-agent`. Lane 1 reported
     that loopx and oh-my-agent exist as swarms of byte-identical forks with no established
     canonical upstream, and that it fetched a fork (0 stars, 3,273 commits) for oh-my-agent.
     **Read those designs, do not depend on those repos.**
   - SGLang router flag names (`--cache-threshold` etc.) — lane 2 could not retrieve a primary page.
   - Ray Serve LLM — lane 2's fetch DNS-failed; excluded entirely.
   - Envoy adaptive concurrency — lane 2's fetch DNS-failed; the Netflix library covers the same
     algorithm family and *was* fetched.
   - `Ivy-Tendril` and `humanlayer` — lane 1 got no mechanism detail. Not used below.
   - The three-agent planner/generator/evaluator structure attributed to Anthropic. Lane 1 fetched
     https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents and found a
     **two**-agent structure with no dedicated evaluator. Do not design against the three-agent claim.
3. Our own `docs/RESEARCH-automation-reporter-trap.md` already carries its own "VERIFY" flag on the
   tau2-bench 44–52% / 3% figure (section 2.3), and explicitly says that contrast is *between
   benchmark settings, not a measured intervention*. Nothing below leans on it as a measured effect.

---

## 1. HAVE — we implement this, and ours is comparable or better

| # | Capability | Ours | Closest external | Why ours holds up |
|---|---|---|---|---|
| H1 | **Progress measured from environment state, not model text** | `run/progress.py` `Snapshot`/`Delta`; `run/verified.py` builds the snapshot from disk and the docstring pins "Nothing below this line reads `ruling`" | Anthropic `feature_list.json` per-feature pass/fail (anthropic.com/engineering/effective-harnesses-for-long-running-agents); LongHorizon-Harness Auditor (github.com/AMAP-ML/LongHorizon-Harness); statewright guard `test_result eq pass` (github.com/statewright/statewright) | Ours is finer than all three. `Delta` separates **activity / investigation / achievement** as three distinct properties, and `Snapshot.outcome` is hashed for repetition identity only, never scored. Nobody in the four lanes names a three-way split; they all have a binary pass/fail ledger. |
| H2 | **Investigation cannot buy unlimited retries** | `progress.stagnant(..., investigation_budget=...)` — "the reporter trap wearing a lab coat" | oh-my-agent reinforcement limit of 5 (github.com/FaintFlower/oh-my-agent, *fork, unverified provenance*); MartinLoop iteration caps (github.com/Keesan12/martin-loop) | Theirs is an attempt counter. Ours is semantically typed: new evidence extends the run, but only *achievement* resets the clock. This is the exact hole a plain retry counter leaves open. |
| H3 | **Stuck vs. waiting** | `Delta.waiting` from `Snapshot.in_flight`; `decide()` short-circuits on it; `Hooks.worker` returns `in_flight_ids` specifically so an async worker can say "still running" | Cyclops "idle never infers success" (github.com/cyclops-team/cyclops); OpenHands #5355 is the documented failure both of us are patching | Equivalent quality, opposite framing. Cyclops refuses to read silence as success; we refuse to read silence as stall. Both invariants are needed and we should adopt theirs too (see M-8). |
| H4 | **The judge cannot outvote the environment** | `run/verified.py::decide` — "Ground truth over narrative"; an ACCEPT with unmet mechanical criteria is recorded as *overruled* and still falls through to the stagnation break | agent-kanban schema rule that the assignee cannot accept its own submission (github.com/saltbo/agent-kanban); MartinLoop three terminal states (github.com/Keesan12/martin-loop); NEEDLE shipped-work gate (github.com/jedarden/NEEDLE) | Comparable. One refinement of ours is worth noting: we explicitly *do not* return early on an overruled ACCEPT, because a judge that accepts every round would otherwise hold a stuck loop open to its full budget. |
| H5 | **Artifact-to-output binding stronger than a commit gate** | `run/evidence.py::seal` — IDENTITY or a **re-run transform**, plus an input-**sensitivity probe** that rejects `lambda _: artifact` | NEEDLE's shipped-work gate: a pushed commit on a path outside `notes/` and `.beads/` (github.com/jedarden/NEEDLE) | **Ours is the stronger mechanism.** NEEDLE proves *something shipped*; we prove *the bytes accepted are a function of what the worker produced*. The sensitivity probe closes a hole (a transform that ignores its input) that I could not find addressed anywhere in the four lanes. **Caveat: not wired — see M-1.** |
| H6 | **Honesty about what is mechanically checkable** | `run/grounding.py`: `GROUNDED / PENDING_PARAM / PROXY / FLOOR / UNGROUNDABLE`, and `status()` separating `mechanically_complete` from `signed_off`; inert criteria **block** completion | LoopX decision scopes / user gates (github.com/loopx-project/loopx, *fork swarm*); GitHub Spec Kit `[NEEDS CLARIFICATION]` (cited in our own research doc) | Better. A frozen slot that blocks the machine verdict rather than being dropped from the question is a finer distinction than a binary "needs human". The 2026-09-19 defect note in `grounding.status` records that we already got this wrong once and fixed it. |
| H7 | **A health check that catches the 200-OK-while-dead case** | `check/watchdog.py::probe` — port open, then an actual generation under a short timeout, reporting `UP / WEDGED / DOWN` | exo documents **no** failure recovery at all (github.com/exo-explore/exo); AIBrix has a separate accelerator fault detector (github.com/vllm-project/aibrix); nora treats "stalled telemetry" as a first-class alarm (github.com/solomon2773/nora) | Ours is the best answer found for this exact failure. Lane 1's finding that the 47.5k-star exo README says nothing about node resilience confirms we will be building this ourselves. |
| H8 | **Durable queue: directory is the state, atomic claims, owned leases** | `run/queue.py` — `ready/running/done/parked/failed`, `os.replace` claim, `CONTROLLER`/`touch_lease`/`lease_alive`/`reclaim_orphans`, reclaim only on an expired lease | NEEDLE SQLite transactional claim; beads `bd update --claim` (github.com/gastownhall/beads); gastown git-worktree hooks (github.com/gastownhall/gastown); Cyclops fsynced journal | Comparable, and cheaper. Ours is stdlib with no daemon. The claim sequence comment in `queue.drain` shows we already closed the in-memory-only window their designs also close. |
| H9 | **Exactly-once across a kill** | `run/supervisor.py::_execute_and_file` journals the result **before** moving the card; `recover()` settles an unfiled finished job instead of re-running it | Nothing in the lanes describes this window explicitly. Closest is background-agents' snapshot-restore (github.com/ColeMurray/background-agents) | Ours addresses a narrower and sharper problem than anything reported. Keep it. |
| H10 | **Dependency liveness, not just dependency order** | `run/supervisor.py::liveness` — a fixpoint that answers "can this *ever* run", plus `_find_cycle` naming one concrete cycle | beads computes ready-work from the blocker graph (github.com/gastownhall/beads) | beads answers "is it ready now". We also answer "will it never run", which is the difference between a supervisor that idles until morning and one that says why. |
| H11 | **Capacity refusal is a normal outcome, not a failure** | `run/queue.py::_classify` files `CapacityTimeout`/`PrefillLockTimeout` as `ready`/`deferred-capacity`; supervisor retries with capped exponential backoff and parks as a question after `max_capacity_retries` | LiteLLM immediate 429 with cooldown (docs.litellm.ai/docs/routing); llama-swap `concurrencyLimit` → 429 (github.com/mostlygeek/llama-swap) | Comparable at the job layer. Theirs is at the request layer; both are needed. |
| H12 | **Auditable overnight run** | `run/verified.py::Receipt` (`receipt.md` + `receipt.json`), `run/supervisor.py::write_status` atomic one-screen status | oh-my-agent `events.jsonl`; AutoHarness JSONL provenance (github.com/aiming-lab/AutoHarness, *377 stars / 8 commits — a design document, not a system*) | Ours is better for the actual use case. The receipt defines its own vocabulary on the page for someone who was not there; a JSONL event stream does not. |
| H13 | **Config that refuses to guess** | `fleet.py::_Unset` poison object — any *use* raises `SettingsMissing` naming the file to copy | The awni MLX gist's host preconditions (`iogpu.wired_limit_mb`, identical python path) are exactly this class of silent-degradation source (gist.github.com/awni/ec071fd27940698edd14a4191855bba6) | Ours is the right pattern; we just have not extended it to *host* preconditions (M-6). |

---

## 2. MISSING — they have it, we do not, we plausibly need it (ranked by value to us)

**M-1. Wire `run/evidence.py` into the acceptance path.** *(Highest value; it is the cheapest, and
it closes a gap between what the repo claims and what it runs.)*
`grep` shows `evidence.py` is imported **only by `tests/`** — `tests/test_evidence.py`,
`tests/test_evidence_defects.py`, `tests/skeptic/test_m2_challenge.py`. `run/verified.py` never
imports it. So the capstone loop accepts on `oracle_ok and not unmet and judge_accepts`
(`verified.decide`) with **no sealed record binding the accepted bytes to the worker's output**.
Milestone 2 is built; milestone 3 does not use it. External equivalents that *are* wired:
NEEDLE's shipped-work gate, MartinLoop's VERIFIED terminal state, agent-kanban's task claim.
Cost: roughly 30 lines in `verified.py` — seal at the point `output.md` is written, `accept()` at
the `ACCEPTED` branch, `trusted_verifiers` naming the card oracle plus its version.

**M-2. Per-worker circuit breaker with cooldown and automatic readmission.**
Today `check/watchdog.usable` is a binary gate with a 120 s cache, and `run/verified.py` turns a
non-`UP` worker into exit code 3, which `queue._classify` files as **`failed`/`worker-unavailable`**
— terminal. Compare: a capacity refusal is retried up to eight times with backoff, but a *wedged
cluster kills the job outright*. That asymmetry is backwards for our known failure mode.
External: olla's circuit breakers plus auto-rediscovery on recovery (github.com/thushan/olla);
LiteLLM cooldown on the **individual deployment**, not the model group, with `allowed_fails` and
failure-counter reset on health (docs.litellm.ai/docs/routing).

**M-3. Capability-keyed routing with typed fallbacks.**
`run/verified.py::main` reads `card["worker"]` as a literal string; `run/conductor.py` has the
frontier planner *write* that literal into the card. There is no model-group indirection and no
failover. `fleet.py` already carries the right raw material (`requires_prefill_lock`,
`supports_gbnf`, `reasoning_style`, `ctx`, `max_inflight`) — it is simply not used for selection.
External: LiteLLM `model_list` with `model_info.access_groups`, tag routing (`&`/`!`/OR), `order`
priority tiers, and three fallback lists keyed by failure class — generic, context-window,
content-policy (docs.litellm.ai/docs/routing).
Note a real capability key we already have and do not route on: `cluster` is
`supports_gbnf: False` while every llama worker is `True`, and `call.body_for` silently drops the
grammar for the cluster. A card that *needs* constrained output must not be routed there.
(XGrammar's README does not list MLX integration — github.com/mlc-ai/xgrammar.)

**M-4. Adaptive concurrency instead of declared caps.**
`fleet._TOPOLOGY` hard-codes `max_inflight` 2/1/1/1 and `jobs.open_job_capped` enforces those
numbers. They were measured once, on one topology. External: Netflix concurrency-limits treats the
limit as a TCP congestion window discovered at runtime — Vegas (queue depth from
`L * (1 - minRTT/sampleRtt)`) and Gradient2 (short vs. long latency EMA divergence *duration*),
with clean Limit/Limiter separation (github.com/Netflix/concurrency-limits). Also relevant:
**partitioned limiters** reserving a share for interactive traffic, so an overnight drain cannot
starve an architect call.

**M-5. Read the backend's own admission signal instead of maintaining a side ledger.**
`jobs.inflight` counts rows in `runs/jobs.sqlite3` and excludes rows older than `stale_s=600`.
That is an *estimate of* the backend's state, maintained out of band, and it desynchronizes on any
crash path the ledger did not see. External: `llama.cpp` exposes `GET /slots?fail_on_no_slot=1`
returning **503** when no slot is free (github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md),
and vLLM exports `num_requests_waiting` / `num_requests_running` / `kv_cache_usage_perc`
(docs.vllm.ai/en/latest/usage/metrics.html). Our `pairA`/`pairB`/`spark`/`vision` are all
llama.cpp — the signal already exists on four of five workers.

**M-6. Host-precondition preflight for the Mac cluster.**
`check/watchdog.patches_ok` asserts the MLX patches are in the running tree, which is good and
unusual. It does **not** assert the preconditions the awni gist names: `iogpu.wired_limit_mb`
(resets on reboot), identical `mlx-lm` version on every rank, identical python path on every host
(gist.github.com/awni/ec071fd27940698edd14a4191855bba6; MLX distributed docs at
ml-explore.github.io/mlx/build/html/usage/launching_distributed.html state the identical-path
requirement outright). Every one is a silent-degradation source and every one is a cheap SSH check
before dispatch — exactly the `_Unset` discipline extended to hosts.

**M-7. The `mlx.launch` supervision contract.**
Per lane 2, `mlx.launch` monitors the launched processes and **terminates the rest if one fails**
(ml-explore.github.io/mlx/build/html/usage/launching_distributed.html). Per
`MEMORY.md → reference_mlx_cluster_fragility.md`, our observed failure is a rank dying silently
while `/health` still returns 200 and the survivor hangs at 0% CPU — i.e. the group is *not* being
torn down. If our cluster is started outside `mlx.launch`, we have opted out of the only failure
detection Apple ships. This is a question to answer against `ops/`, not a conclusion — I did not
read the launcher invocation on the Mac.

**M-8. "A failed delivery never auto-retries; idle never infers success."**
Cyclops states both as design law and halts in `attention_required` (github.com/cyclops-team/cyclops).
We have the second half (`Delta.waiting`) but not the first: our supervisor auto-retries a
capacity refusal eight times. Against a cluster that hangs at 0% CPU while answering health checks,
auto-retry into it is precisely the wrong move — which is why M-2 and M-8 should land together.

**M-9. Token / spend budget as an orchestrator pre-check.**
We budget in *rounds* (`--max-rounds`, `investigation_budget`) and *seconds* (`--max-seconds`).
Nothing counts tokens or cost. External: MartinLoop stops the **next** attempt before a USD/token
cap is crossed; LoopX charges quota only on **validated completion** and explicitly exempts skips,
preflight failures and dry runs (github.com/loopx-project/loopx, *fork swarm — read, don't depend*);
nora hard-caps with auto-pause (github.com/solomon2773/nora). LoopX's rule is the interesting one
for us: a loop that produces nothing should also cost nothing.

**M-10. A context-blind judge re-run over the full criteria set.**
`evaluate_criteria` *does* re-run every evaluable criterion every round, so we have the mechanical
half. What we lack is the oh-my-agent construction: a judge spawned with **fresh context**, never
told what was previously claimed. Our `live_judge` gets the card and the output; in
`redo_mode=conversation` the worker carries the whole thread. Cheap to add with `spark` or a second
`pairB` lane.

**M-11 through M-14 (lower value, noted for completeness).**
Record-and-replay of a run to fork it onto a different model with zero token spend (OrcaReplay,
github.com/Continuum-AI-Corp/OrcaReplay) — the cheap experiment for M-3.
Write-permission split: the agent proposes, a differently-privileged job disposes (gh-aw safe
outputs, github.com/github/gh-aw).
Per-state tool allowlists plus bash discernment (statewright, github.com/statewright/statewright) —
matters only once workers get write tools; `ops/` sandboxes untrusted snippets today.
KV-slot save/restore for resumable inference state (llama.cpp `/slots/{id}?action=save`).

---

## 3. THEIRS IS BETTER — we have something here, the external mechanism is stronger

**T-1. The prefill lock's stale-steal is a correctness hole; a heartbeat lease is the fix, and we
already own one.**
`jobs.acquire_lock(name, timeout=45, stale_s=60)` **steals** a lock older than 60 s. A prefill on
the cluster that takes longer than 60 s therefore permits a second process to start one — the exact
concurrent `insert_segments` crash the lock exists to prevent — while `call.chat` releases only on
the first generated token. Meanwhile `run/queue.py` already implements the correct primitive:
`touch_lease` heartbeats every `LEASE_BEAT=20` under `LEASE_TTL=90`, and `lease_alive` distinguishes
"holder is slow" from "holder is dead". `run/supervisor.py` states the governing rule explicitly —
*"We do not invent a second ownership primitive."* We invented one anyway, in `jobs.py`.
**Adopting:** heartbeat the prefill lock from the holding request, or bound `stale_s` by the
request `timeout` rather than a flat 60. Cost: ~15 lines in `jobs.py`, one test.
External framing: LiteLLM's **reservation** pattern — reserve estimated capacity at admission,
reconcile with actuals on completion (docs.litellm.ai/docs/routing).

**T-2. Chunked prefill beats a prefill mutex — but we probably cannot have it.**
vLLM interleaves split prefills with decode steps rather than excluding other work, and preempts
(RECOMPUTE/SWAP) instead of crashing when KV blocks run out (docs.vllm.ai/en/latest/configuration/optimization.html).
Our whole prefill-lock design is the degenerate case of that: a global mutex. **Honest cost
assessment: not adoptable.** It is a property of the engine, and our primary reasoning node is
`mlx-lm`, whose own SERVER.md says it is "not recommended for production", exposes only
`/v1/chat/completions` and `/v1/models`, and — notably — **serializes to one request at a time when
`--kv-bits` is set** (github.com/ml-explore/mlx-lm `mlx_lm/SERVER.md`). That last point is worth
checking against our launch flags: if the cluster already runs a quantized KV cache, part of what
the prefill lock enforces may already be enforced inside the server, and `max_inflight: 2` would be
optimistic.

**T-3. Deployment-level cooldown beats a binary usable/unusable gate.**
`watchdog.usable` returns a boolean with a 120 s cache; there is no notion of "this worker failed
three times in the last minute, take it out of rotation and try it again in 5 s". LiteLLM cools the
**individual deployment**, keyed on 429s, >50% failure rate in a minute, and non-retryable
401/404/408, with configurable `allowed_fails` and counters that reset on health
(docs.litellm.ai/docs/routing). olla adds automatic model-discovery refresh so a restarted backend
rejoins the catalog with no operator action (github.com/thushan/olla).
**Adopting cost:** low — a failure counter and a `not_before` per worker in `runs/watchdog.json`,
which already exists and already carries a timestamp. The supervisor's per-job `_not_before` is the
same pattern one level up; this is copying our own idea to the worker layer.

**T-4. NEEDLE scopes acceptance to *where* the artifact landed; we scope only *what bytes*.**
`workspace_evidence` excludes `LOOP_FILES` and the artifact under judgement from *evidence*, which
is the same instinct. But `decide()` has no equivalent rule on the **acceptance** side: nothing
stops a card whose artifact is `notes.md` from being accepted for producing prose. NEEDLE requires a
pushed commit on a path outside `notes/` and `.beads/` (github.com/jedarden/NEEDLE) — lane 1 notes
that the exclusion was clearly added *after* watching agents satisfy a commit gate by committing
more prose. Cost: one card field (`artifact_must_be`), one check in `decide`.

**T-5. A supervisor role whose only job is watching worker health.**
gastown separates Witnesses (per-rig lifecycle, watching agent health) from Deacon (cross-rig
patrol) from the workers themselves (github.com/gastownhall/gastown). Our `check/watchdog.py` is a
*library called by dispatchers* (`queue.drain`, `verified.main`, `supervisor._capacity`) plus an
optional `--loop`. Nothing runs continuously by default and nothing watches a worker *during* a job.
Given that our failure is a rank dying mid-request, the check we have runs at exactly the wrong
time. Cost: moderate — a standing `watchdog --loop` process and a rule in `supervisor.run` that
reads it each tick, which is nearly free because `usable()` already consults the cached file.

**T-6. Bounded-load affinity.**
`queue.choose_assignments` honours affinity in pass 1 unconditionally, then fills. It is bounded in
practice because it only ever assigns into free slots, so the pathology is mild — but KubeAI's CHWBL
gives the principled version: prefix affinity **with an overflow threshold** when a replica's
in-flight count exceeds the fleet mean by a configured percentage (kubeai.org/concepts/load-balancing/).
Cost: ~15 lines. Low priority; flagged because it is the textbook form of what we hand-rolled.

---

## 4. REINVENTING — hand-built where a solved pattern exists *(the most valuable list)*

An operator specifically asked about the **durable queue** and **capacity/admission control**.
Verdicts first, reasoning after.

### R-1. Durable queue → **KEEP OURS.**
`run/queue.py` + `run/supervisor.py` is ~1,100 lines of directory-as-state with atomic renames,
controller leases, journal-before-move, dependency liveness and Windows `PermissionError` retry
(`_replace`). The established alternatives all cost a daemon or a service: beads is Dolt-backed SQL
(github.com/gastownhall/beads, *star count unverified*), background-agents needs Cloudflare Durable
Objects (github.com/ColeMurray/background-agents), nora needs BullMQ + Redis
(github.com/solomon2773/nora). For a single-operator fleet on Windows with Macs as compute, a
stdlib queue with no service to keep alive is the right trade, and ours already solves two problems
theirs document but do not (exactly-once across a kill; "will never run" reachability).

Three things to steal *into* it rather than replace it with:
- **Hash-based IDs.** `queue.add` writes `ready/{name}.json` where `name` is `card["name"]` or the
  filename stem, and `os.replace` **silently overwrites** a same-named card. beads uses hash IDs
  (`bd-a1b2`) explicitly so concurrent producers never collide. Our conductor generates card names
  from a frontier model's JSON — collisions are plausible. Cheap fix: refuse an existing name, or
  suffix a short hash.
- **Typed links.** beads has `relates-to / duplicates / supersedes / replies-to` and an issue type
  of `message`. We have only `needs`/`depends_on` (`supervisor.needs_of`). Add only if a real goal
  demands it.
- **`graphlib.TopologicalSorter` is in the stdlib** and we hand-rolled Kahn in `plan_order`. Keep
  ours: `graphlib` raises `CycleError` without naming the cycle, and `_find_cycle` naming one
  concrete cycle is the thing that makes a 4am failure actionable. Worth a one-line comment saying
  we considered it.

### R-2. Capacity / admission control → **SPLIT: wrap for llama, keep for the cluster.**
This is the clearest reinvention in the repo. `jobs.open_job_capped` maintains a **side ledger** of
in-flight requests in SQLite, with a `stale_s=600` heuristic to stop a crashed process wedging the
cap, and `fleet._TOPOLOGY` hard-codes the caps. That is a model *of* the backend, maintained out of
band, and it can only ever be an approximation of the backend's real state.

- **For `pairA`/`pairB`/`spark`/`vision` (all llama.cpp): wrap, do not model.** `--parallel N`
  makes slots the capacity ledger, and `GET /slots?fail_on_no_slot=1` returns **503** when none is
  free (github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md). Let the backend own and
  publish its own capacity; a 503 you read cannot desynchronize from reality the way a counter you
  maintain can. This also deletes the `stale_s` heuristic for four of five workers.
- **For `cluster` (mlx-lm): keep ours.** mlx-lm's SERVER.md documents only
  `/v1/chat/completions` and `/v1/models` — **there is no admission signal to read**
  (github.com/ml-explore/mlx-lm). Our external gate is not redundant; it is the only option. Keep
  `open_job_capped`, and fix T-1.
- **Longer term, adopt LiteLLM's derivation rather than hand-tuned integers.** LiteLLM derives
  `max_parallel_requests` from one declared throughput number (`int(tpm / 1000 * 6)`), and filters
  over-limit deployments **before** the provider call (docs.litellm.ai/docs/routing). Our four
  integers in `_TOPOLOGY` are four things to keep true by hand.

### R-3. Exponential backoff → **KEEP, add jitter.**
`supervisor._delay` is `min(cap, base * 2**(attempt-1))` — three correct lines, no dependency
needed. But it has **no jitter**, so N jobs deferred by the same full worker in the same tick all
wake in the same tick and re-collide. Standard AIMD practice and LiteLLM's rate-limit backoff both
assume jitter. Cost: one `random.uniform` call.

### R-4. Outcome classification by exit code → **KEEP, adopt NEEDLE's two extra codes.**
`queue._classify` maps rc 0 → done, 1/2 → parked, 3 → failed, traceback → CRASH. NEEDLE keys on
**124 = timeout → release and defer** and **>128 = signal/crash → release, auto-create an alert
bead** (github.com/jedarden/NEEDLE). Our `supervisor.verified_executor` already *returns* 124 on
`TimeoutExpired`, but `_classify` has no 124 branch, so a timeout falls to `exit124` → `failed`.
Also see M-2: rc 3 (`worker-unavailable`) being terminal rather than retryable is the same class of
mapping bug. Cost: two lines in `_classify`, and it materially changes overnight behaviour.

### R-5. Token estimation and context trimming → **KEEP as a guard; do not grow it.**
`call.est_tokens` is `len//4 + 8*n`, and `fit_context` drops oldest non-system messages. This is a
*guard against a known hang* (the cluster wedges at 0% CPU on over-long prompts), not a packing
optimizer, and the docstring says so. A real tokenizer would be a dependency for a safety margin we
deliberately over-provision anyway. One improvement worth taking: read the real context window from
the worker rather than the hard-coded `ctx` in `_TOPOLOGY` (llama.cpp `/props` exposes it), so a
server relaunched with a different `n_ctx` cannot silently invalidate our guard.

### R-6. Health checking → **KEEP; ours is better than the field.** See H7.
Add circuit-breaker *state* (T-3) rather than replacing the generation canary. Do **not** adopt
`/health` semantics from any of the routing projects — every one of them health-checks in the way
that lane 1 confirmed exo cannot handle and that our memory records as our actual failure mode.

### R-7. Oscillation detection → **KEEP.**
`run/oscillation.py` (A→B→A ping-pong and fixed-point repeat over normalized output hashes) plus
`progress.stagnant` covers four of the five OpenHands StuckDetector patterns
(docs.openhands.dev/sdk/guides/agent-stuck-detector, cited in our own research doc). The fifth —
*monologue*, the agent talking without acting — we cover more strongly than they do, because
`Delta.churn_only` catches it at the state level rather than the text level.

### R-8. Integrity digest → **KEEP.**
`evidence.integrity_digest` is canonical JSON under SHA-256, optionally HMAC, with the empty-key
defect already fixed and the tamper-evident-not-unforgeable limit already documented in the module
docstring. Nothing in the lanes does better; agent-kanban's signed sessions
(github.com/saltbo/agent-kanban) are the only stronger construction found, and they solve a
multi-party trust problem we do not have.

### R-9. Affinity scheduling → **KEEP, bound it.** See T-6.

### R-10. Status page / receipt → **KEEP.** Nothing better was found in four lanes.

---

## Three direct answers

### Is anything we built genuinely novel, or has all of it been done?

**Mostly done elsewhere — with two exceptions that are real but narrow.**

*Novel #1: the three-way progress typing.* `Delta.activity` / `.investigation` / `.achievement`
with an **investigation budget** (`progress.stagnant`) does not appear in any of the ~50 projects
the lanes surveyed. Every external system has a binary notion: progressed or not. The specific
insight that *new evidence justifies another attempt but must not justify unlimited attempts*, and
the specific failure it names — "re-calling a tool with fresh arguments registers new evidence
whether or not it resolved anything" — I found nowhere. It is a small idea and it is ours.

*Novel #2: the transform sensitivity probe in `evidence.seal`.* Re-running a declared transform and
comparing bytes is not novel. Additionally probing the transform with bytes the worker never
produced, and refusing it if it still yields the artifact (`lambda _out: artifact`), is a hole every
recomputation-based binding has and that nothing in the lanes closes. The in-code note dated
2026-09-19 records that we found it by being attacked by our own ceremonial run.

**Closest prior art for our state-delta progress measurement**, in order of closeness:
1. **LongHorizon-Harness** (github.com/AMAP-ML/LongHorizon-Harness) — bounded step, fresh context,
   verify against files/UI/logs/tests, and the checkpoint-vs-evidence split. Closest in *shape*.
2. **Anthropic's `feature_list.json`** with per-feature pass/fail as the durable ledger
   (anthropic.com/engineering/effective-harnesses-for-long-running-agents). Closest in *substance*:
   progress is a count of passing features on disk. Simpler than ours and probably sufficient for
   most work.
3. **statewright** conditional transitions guarded by `test_result eq pass`
   (github.com/statewright/statewright) — the same "advance on environment facts" rule, applied to
   a state machine rather than a loop.
4. **OpenHands StuckDetector** — the text-tier ancestor, already cited in our own research doc.
Our `run/progress.py` docstring credits the reporter-trap research; the honest summary is that the
*idea* is convergent and well-attested, and our *typing* of it is the increment.

**Closest prior art for our evidence-bound acceptance**, in order:
1. **Evidence-Carrying Termination** (arXiv 2608.23623) — already cited in
   `docs/RESEARCH-automation-reporter-trap.md` §2.4: complete only when "a typed certificate binds
   every required answer claim to valid, in-scope trace evidence and a deterministic replay
   reconstructs the claimed value." That is `evidence.seal` stated academically, and our own doc
   already carries the correct caveat that it certifies support, not truth.
2. **NEEDLE's shipped-work gate** (github.com/jedarden/NEEDLE) — the closest *implementation*, and
   stronger than ours in scope (it names where the artifact must land) while weaker in binding.
3. **agent-kanban** (github.com/saltbo/agent-kanban) — task claims cryptographically bound to the
   runtime that produced them, with self-acceptance forbidden at the schema level.
4. **MartinLoop** (github.com/Keesan12/martin-loop) — policy-checks the **verifier command itself**
   as untrusted input, which is a hole *we have*: `run_oracle` executes `card["oracle"]` as a
   `python -c` expression with no policy check, and `run/conductor.py` has a **model** write that
   card. A planner-authored oracle is an unvalidated verifier.

### What is the single highest-value thing to adopt from outside, and what would it replace?

**LiteLLM's deployment-group model: route to a *capability group*, cool down a *failing worker*,
and fall back by *failure class*.** (docs.litellm.ai/docs/routing, reinforced by olla's circuit
breaker plus auto-rediscovery at github.com/thushan/olla.)

What it replaces, concretely:
- `card["worker"]` as a literal string in `run/verified.py::main` and in every card the conductor
  writes → a capability requirement resolved at dispatch against `fleet.WORKERS`, which already
  carries the flags.
- `watchdog.usable`'s binary gate plus its 120 s cache → a per-worker failure counter and cooldown
  deadline in `runs/watchdog.json`, which already exists and already stores a timestamp.
- `EXIT_CODES["worker-unavailable"] = 3` → `queue._classify` rc 3 → **`failed`** → a retryable
  `ready`/`deferred-worker` outcome that reroutes to another member of the group.

Why this one: it is the only change that converts our single most common operational failure — the
cluster wedges, per `MEMORY.md → reference_mlx_cluster_fragility.md`, and human-only recovery is
required — from *"the job died"* into *"the job ran on pairA"*. It reuses machinery we already have
(the capability flags, the watchdog file, the supervisor's `_not_before` pattern) and needs no new
dependency. Estimated cost: a day, mostly tests.

### Given the operator wants ONE real goal completed end to end before more infrastructure — what accelerates that, and what is a distraction?

**What accelerates it, in order:**

1. **Wire `evidence.py` into `verified.py` (M-1).** Half a day. Today the repo *claims* earned
   acceptance and *runs* oracle+judge. Closing that makes "one real goal completed end to end"
   mean something checkable, and it is the deliverable of milestone 3 as `docs/MILESTONES.md`
   states it: "inspectable artifact + receipt, with injected failures proving rejection and
   recovery."
2. **Adopt Anthropic's session discipline for the goal itself**
   (anthropic.com/engineering/effective-harnesses-for-long-running-agents): a `feature_list.json`
   with per-feature pass/fail, and the rule that a session **reads the ledger first, then picks
   exactly ONE feature**. We have the machinery (`grounding.Criterion`, `Receipt`) and no
   goal-level ledger — `run/verified.py` is per-job, and `docs/RESEARCH-automation-reporter-trap.md`
   §7 lists "the goal-level versions of the two exits" as *not yet built*. This is the smallest
   piece of infrastructure that directly serves "one goal end to end" rather than deferring it.
3. **Fix the two mapping bugs that will actually end the run (R-4, M-2).** rc 124 → `failed` and
   rc 3 → `failed`. Both are two-line changes and both are the difference between a night that
   produces a parked question and a night that produces a failed card.
4. **Fix the prefill-lock stale-steal (T-1).** ~15 lines. It is the one item on this list that can
   *crash the cluster* rather than merely waste a night.
5. **Add the host-precondition preflight (M-6).** An SSH check of `iogpu.wired_limit_mb` and the
   `mlx-lm` version on both ranks before dispatch. Cheap, and it converts a silent 0%-CPU hang into
   a refusal with a reason.

**What is a distraction right now** — all of it good, none of it on the path to one completed goal:

- **Every distributed-inference idea.** exo's topology-aware partitioning, tightwad's cross-machine
  speculative decoding (github.com/youngharold/tightwad — 21 commits; lane 1 calls it a hypothesis,
  not a dependency), GPUStack's engine-as-scheduling-decision. These change *how fast* inference is.
  Nothing here is blocked on speed.
- **Every Kubernetes-shaped control plane.** llm-d, AIBrix, production-stack, gateway-api-inference-extension,
  KubeAI, agenttier. Read them for mechanism (M-5's signal set came from there); deploying any of
  them is a platform migration for a five-worker LAN.
- **Sandbox proliferation.** agentbox, omnigent's twelve backends, gh-aw's safe outputs,
  statewright's bash discernment. These matter when workers get write tools. Our workers write
  `output.md` into a workspace; `ops/` already sandboxes untrusted snippets.
- **Redundancy-as-verification.** MassGen's convergence voting (github.com/massgen/MassGen),
  tightwad's multi-drafter consensus. Lane 1 states the honest limit itself: agreement between
  models is weaker evidence than a passing test. We have passing tests. Spend the idle rigs on
  M-10's fresh-context judge instead, which is a stronger signal for the same wall-clock.
- **Record-and-replay (OrcaReplay).** Genuinely clever and genuinely premature — it is the cheap
  experiment for a routing layer we have not built.
- **Adopting `beads`/`loopx`/`oh-my-agent` as dependencies.** Lane 1 could not establish provenance
  for two of the three. Read the designs; write the fifteen lines.

**The one-sentence version:** we have better verification primitives than almost anything surveyed
and worse plumbing around them — so the fastest route to one completed goal is to *connect what we
already built* (evidence → acceptance, goal-level ledger) and fix four small mapping and locking
bugs, not to adopt a routing layer, a scheduler, or a cluster manager.
