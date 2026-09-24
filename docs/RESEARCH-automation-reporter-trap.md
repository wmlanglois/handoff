# Research Notes on Automation: Breaking the Reporter Trap in Autonomous Local-LLM Loops

*Working notes for the fleet harness. Status: research consolidation + design proposal. Nothing here is
settled; the point of writing it down is to be argued with.*

---

## 1. The problem, stated plainly

The goal of this harness is an autonomous loop: an architect model drafts work, local workers do it, a
cheap skeptic questions it, deterministic oracles gate it, and the loop turns itself without a human at
the terminal and without burning frontier tokens on grunt work.

Every attempt at the last step has died the same way. Multi-step loop prompts, a `CLAUDE.md` with a
north-star goal, cron-driven wakeups -- all of them eventually reach a state where **nothing in the
environment changes, and the frontier model degrades into a reporter.** It stops making new iterations
and starts re-stating its status: *"I did my job. I did my job."* It runs forever and does nothing.

The operator's diagnosis, which this note sets out to test against the literature:

> If the architect doesn't rotate, or there isn't some new input, it starts saying "I did my job."
> What it needs is someone to walk in and say "you missed a spot." More heads are better than one --
> the outspoken one, the quiet one, the slow careful one, the fast wrong one. A tiny, cheap model
> whose only job is to notice "this is the same status as the last two turns" might be the variable
> that breaks the loop. It sounds too simple to be groundbreaking.

A second, structural claim: **crons are not the answer**, because a cron fires on a clock with a
predetermined prompt, and there is not enough *variable* in a predetermined prompt to dislodge a
model that has already decided it is finished.

## 2. What the literature says

The operator's observation turns out to be two documented failure modes stacked on top of each other,
plus a quantified explanation of why "try harder" prompts cannot fix them.

### 2.1 Degeneration-of-Thought: self-reflection cannot generate novelty

Liang et al. formalize exactly this: reflection-style methods "suffer from the Degeneration-of-Thought
(DoT) problem: once the LLM has established confidence in its solutions, it is unable to generate
novel thoughts later through reflection even if its initial stance is incorrect" [1]. Their remedy is
Multi-Agent Debate (MAD): several agents argue in a "tit for tat" state while a judge manages the
process. This is the room-of-humans intuition, made formal.

Two of their empirical findings constrain any implementation:

- The debate needs an **adaptive break** -- it must be able to stop. An unbounded debate does not
  converge to truth; it wanders.
- Only a **modest** level of tit-for-tat helps. Too much disagreement degrades results.

### 2.2 The self-refinement plateau, quantified

Iterative self-refinement (Self-Refine [2], Reflexion-style loops) is the mechanism every "loop
prompt" secretly relies on: the same model critiques and revises its own output. The measured limits:

- Without external feedback, state-of-the-art models gain **+1.8 percentage points or less** across
  five iterative attempts. With guided external feedback, the same models reach **+80% within five
  turns** [3].
- **Self-bias monotonically amplifies** across self-refinement steps: the model systematically
  overrates its own generations, and does so more each round [3].
- The bottleneck is **error detection, not error correction**. Models can fix a mistake once it is
  pointed out; they cannot reliably find their own [3].

This is why a better loop prompt cannot solve the reporter trap. A nudge from the same model is still
self-refinement. It plateaus, and the model's confidence in its own "done" *grows* as it stalls.

### 2.3 False success: agents confidently declare done while the state says otherwise

Independent of stagnation, agents fail by reporting completion when the underlying system state has
not changed. On tau2-bench (9,876 trajectories, 8 frontier model families), **false success accounts
for roughly half of all failures** in single-control domains, against **about 3%** in a dual-control
domain where the environment itself can contradict the agent [4].

Three cautions on that contrast, because it is the single most over-claimed number in this area:

1. It is a **comparison between benchmark settings, not an intervention**. Nobody added a verifier to
   a single-control domain and measured 3%. Dual-control domains differ in more ways than
   verifiability, so the gap bounds an opportunity; it does not size a fix.
2. It therefore **cannot be cited as evidence that this harness's verifier reduces false success**.
   That claim requires measuring this harness, which section 8 has not yet done.
3. Published summaries of the single-control figure vary in the low-to-high forties and beyond.
   The exact range should be read off the primary source before any public use; it is quoted here as
   approximate on purpose. (VERIFY: the precise interval was not re-checked against [4] at the time
   of writing -- the authoring session had no network access.)

The mechanism is operationally dangerous
because it "propagates silently -- unlike a crash or refusal, the agent presents the interaction as
resolved."

The Unreliable Progress Bar study sharpens the point: agents' *reports* of progress change with the
continuation conditions they are given, **while actual task progress stays fixed** [5]. The report is
the unreliable thing. Any stagnation detector that reads the agent's own status text is therefore
comparing two unreliable reports.

### 2.4 Evidence-carrying termination

The corrective for false success is to make "done" carry proof. Evidence-Carrying Termination (ECT)
requires an agent to return *complete* only when "a typed certificate binds every required answer
claim to valid, in-scope trace evidence and a deterministic replay reconstructs the claimed value."
ECT produced **0/288 unsafe completions** versus **252/288** for a termination-critic baseline [6].
Read precisely: ECT certifies that a claim is **supported by in-scope trace evidence under declared
assumptions**. It does not certify that the claim is true of the world. An agent whose tools return
wrong data can still terminate with a valid certificate over that wrong data. Evidence-carrying
termination raises the floor on *unsupported* completion; it is not a correctness oracle, and this
project should not market it as one.
MAST, the NeurIPS 2025 multi-agent failure taxonomy, files premature termination as failure mode
FM-3.1 [7].

### 2.5 A shipped mechanical stuck detector, and its known failure

OpenHands (formerly OpenDevin) ships a StuckDetector [8] that scans recent events for five patterns:
repeating action+observation, repeating action+error, **monologue** (the agent talking without
acting -- the reporter, literally), alternating action/observation, and a context-window error loop.
Four identical action-observation pairs trip it; it nudges once per streak with a *specific* message
("You've called {tool} with the same arguments {n} times in a row and gotten the same error each
time..."), comparing observations by content rather than by timestamps or IDs.

Its documented failure is the cautionary tale for this design: with hardcoded thresholds, **the
detector kills agents that are legitimately waiting on long-running processes** [9]. "Same status for
N turns" is indistinguishable from "waiting for a slow build" unless the detector knows whether an
action is still in flight.

## 3. Synthesis: why the loop dies

Putting the evidence together yields one sentence: **the loop dies when no new information enters it,
and the same model cannot supply that information to itself.**

- A stalled architect re-prompted with its own context has nothing new to reason over; DoT says
  reflection will not produce novelty [1].
- Its confidence that it is finished rises each round it stalls (self-bias amplification) [3].
- Its own status report is not evidence of progress; it drifts independently of real progress [5].
- The only interventions with large measured effect are **external and independent**: a different
  agent's perspective [1], guided feedback from outside the model [3], and an independent check of
  the actual state [4][6].

That reframes an operator's proposal. The tiny observer is valuable *because* it is a different
perspective and *because* it is cheap enough to always be present -- not because it is clever. The
"variable" the loop needs is not randomness in the abstract; it is new, external information.

## 4. The proposal under test

Run a second, permanent, tiny model (Spark, a 4B) alongside the architect. It receives only a
compressed status per turn. Its whole job: *is this the same situation as the last two turns?* If so,
inject a disruption -- "you appear to be in a deterministic loop and are not moving the goal" -- and,
because it is small and cheap, let its output be the randomized variable that dislodges the architect.

Section 2 supports the core of this: a different observer is what breaks DoT, and cheap independence is
the right property. Section 5 is where the specific mechanism breaks.

## 5. Holes in the proposal (the skeptic pass)

**H1. Judging stagnation from the architect's status text inherits the false-success problem.**
The status text is the unreliable artifact [5]. Comparing two of them is comparing two unreliable
reports. Progress must be measured by **state delta** -- files changed, tests passed, oracle results,
new tool outputs -- not by what the model says. Zero state delta over N turns is stagnation regardless
of how productive the prose sounds. Text-sameness is a weak proxy; state-sameness is ground truth.

**H2. A bare "you seem stuck" is still self-refinement.** The model acknowledges it and re-reports in
fresher words -- the +1.8-point plateau in action [3]. OpenHands' nudge works only because it is
specific about the repeated call and the repeated error [8]. The disruption must inject **new
information**: an untried action whose result is unknown, a concrete counterexample, a genuinely
different angle. Not a mood.

**H3. False positives will kill real work.** Long test runs, big builds, and pending tool results all
read as "same status" [9]. The detector must distinguish *stuck* from *waiting*: is there an in-flight
action whose result has not yet landed? Thresholds must be contextual, not a fixed "same for two
turns."

**H4. Randomization alone is noise, not progress, and it regresses.** A random perturbation breaks
passing work as readily as it frees stuck work. Variability must be a **bounded escape**, and the bound
must be a deterministic gate: swing randomly, let the oracle decide, revert anything that fails a check
that previously passed.

**H5. The loop needs two exits, or it fails both ways.** DoT requires an adaptive break [1]; the
false-success work requires that "done" carry evidence [6]. So: a **stagnation exit** (K disruptions
with zero state delta -> stop, report a GAP, never run forever) *and* an **evidence-carrying done**
(complete only with an oracle pass bound to real state). Omit the first and it runs forever; omit the
second and it lies that it finished.

**H6. The observer must ask, never direct.** A 4B is a coin toss. That is acceptable -- even ideal --
for *detection and questioning*, because a wrong question costs almost nothing and a wrong "you might
be stuck" is cheap to dismiss. It is not acceptable for *directives*: a wrong instruction from a small
model derails the loop. Its disruption stays a question or a counterexample; the oracle judges the
result. (This is also why MAD wants only "modest" tit-for-tat [1].)

## 6. The design that survives the holes

1. **Progress ledger = state delta.** Compute "what changed since the last turn" from the job ledger,
   result records, oracle outcomes, and tool outputs. This is the ground truth everything else keys
   on, and it defeats the reporter trap mechanically: a reporter changes text, not state.
2. **Two-tier stagnation detection.** *Mechanical*: hash-based oscillation/repeat detection plus
   state-delta = 0. *Semantic*: the cheap observer judges "same situation as two turns ago?" over a
   **compressed state**, never over the architect's prose. Both must agree, or the mechanical tier
   alone triggers after a longer horizon, to keep false positives down (H3).
3. **Disruption injects new information.** On stagnation, in escalating order: force the untried
   action; deliver angled questions (technical / big-picture / improvement); rotate the observer's
   angle -- the room of humans. Every disruption is bounded by the oracle and a revert guard (H4).
4. **Two exits.** Evidence-carrying done: no completion without an oracle pass bound to state (H5).
   Stagnation exit: K stagnant disruptions -> a GAP report and a hard stop (H5).
5. **Stagnation as a wake event.** The loop's driver ends a cycle -- and wakes the architect -- on
   exactly two events: *batch complete* or *stagnation detected*, and the stagnation wake **carries
   the specific stuck-context**. This is the event-driven closed loop the operator was reaching for
   with "how do we get the machine's output to wake you up." It differs from a cron in the decisive
   way: a cron fires on time with a fixed prompt; this fires on a state event with the live context
   of exactly where and how the loop stalled. The architect (a frontier model today; a large local
   model on a 256GB unified-memory machine later) then injects the new angle.
6. **Re-planning from the delta.** The conductor/planner falls into the same trap if it re-plans from
   the same results. It must re-plan from *what changed*, and it inherits the same two exits.

The unifying principle, from Sections 2-3: **all of the loop's value comes from external, independent
signal -- the oracle's state and a different observer -- never from asking the same model to try
harder.**

### 6.1 The state delta, defined

The state delta is the one term everything above depends on, so it needs a precise definition.

**Definition.** A *snapshot* `S_t` is a deterministic, machine-readable record of the environment after
turn `t`, built only from artifacts -- never from the model's text. The *state delta* is
`Δ_t = diff(S_{t-1}, S_t)`. A turn made **progress** if `Δ_t` is non-zero on a *progress-bearing*
dimension. The loop is **stagnant** when `Δ = 0` on every progress-bearing dimension for `K`
consecutive turns *while nothing is in flight*.

**Snapshot fields (generic core, stdlib-only):**

| field | what it records | source in this harness |
|---|---|---|
| `artifacts` | `{path: content_hash}` for the job workspace | tooljob workspace / `output.md` |
| `oracle` | `{check_id: PASS or FAIL}` per deterministic check (per pytest test, or per assert message) | `run_oracle` output |
| `evidence` | set of `(tool, args_hash)` calls whose results entered context this turn | worker / skeptic tool calls |
| `queue` | each job's state (`ready/running/done/parked/failed`) | `runs/queue/` + `index.jsonl` |
| `in_flight` | actions started but not finished (a live pid, a pending tool call) | ledger `open` rows |
| `goal` | which `done_when` criteria / goal conditions are satisfied | card `done_when`, oracle |

**Delta fields, and which ones count as progress:**

| delta | meaning | progress-bearing? |
|---|---|---|
| `oracle_delta` | checks that flipped `FAIL -> PASS` (or regressed `PASS -> FAIL`) | **yes** (regression is negative progress) |
| `new_evidence` | tool calls in `S_t` never seen in any prior snapshot | **yes** -- this is literally new information entering the loop |
| `goal_delta` | criteria newly satisfied | **yes** |
| `queue_transitions` | jobs that changed state | **yes** |
| `files_changed` | artifacts whose hash changed | **no -- churn only** |

The last row is the crucial nuance. A changed file hash is *necessary* for real progress but not
*sufficient*: rephrasing a memo changes its hash and moves nothing. So `files_changed` is tracked but
does not count. A turn with `files_changed != ∅` and every progress-bearing delta empty is **churn** --
the state-level fingerprint of the reporter.

**Deliberately excluded from the snapshot:** the model's status report, its narrative, its output length,
the judge's prose. Those are exactly the signals Section 2.3 shows to be unreliable. The reporter's
entire failure mode lives in the excluded set, which is why the delta cannot be fooled by it.

**Worked example, from this harness's own runs.**

- *A numeric-assertion job.* Turn 1: the oracle check `expected total missing` is `FAIL`. Turn 2: that check is
  `PASS`. `Δ = { oracle_delta: {check_a: FAIL->PASS}, files_changed: {output.md} }`. Progress.
- *b04-qbi (before the citation rule was relaxed).* Turn 1: check `raw section sign` `FAIL`. Turn 2: the
  worker rephrased -- `output.md`'s hash changed -- but the same check `FAIL`s identically, no new tool
  evidence, no goal movement. `Δ` on every progress-bearing dimension is `0`. **Churn.** This is exactly
  the case that stuck-halted, but the delta detects it mechanically, with no model, in the first repeat.

**How this solves the reporter trap, mechanically:**

1. *Detection needs no model and cannot be talked out of.* The reporter changes text, not state. With
   progress defined on state, a re-reporting architect registers `Δ = 0` no matter how confident or
   varied its prose. This follows the same logic the tau2-bench contrast points at [4] -- with the caveat in
   section 2.3 that the contrast is between settings, not a measured intervention: an independent state
   check instead of a self-report.
2. *The disruption gets concrete content.* Instead of "you seem stuck," the injection is the delta
   itself: "last 3 turns: 0 checks flipped, the same 2 checks fail identically, 0 new evidence -- take
   an untried action: run `test_x`, read `file_y`." The delta *is* the new information (H2).
3. *Stuck vs. waiting is decidable.* `Δ = 0` with `in_flight != ∅` is *waiting*, not stuck. The detector
   holds. This is the fix for the OpenHands false positive [9] (H3).
4. *Both exits fall out for free.* **Done** = every goal criterion satisfied and every oracle check
   `PASS` -- evidence-carrying by construction [6]. **GAP** = `K` consecutive stagnant turns with nothing
   in flight -- the adaptive break [1]. Neither depends on the model agreeing.
5. *Bounded variability is enforceable.* A disruption that produces `oracle_delta` with a `PASS -> FAIL`
   regression is reverted (H4). The delta is what the revert guard reads.

**How it fits the open-source harness.**

- It is a small, domain-agnostic core module: `progress.py` exposing `Snapshot` and `Delta` (plain
  dataclasses) and two pure functions, `snapshot(...)` and `delta(prev, cur)`. Stdlib only. Fully
  unit-testable without any model or hardware -- feed it two snapshots, assert the delta.
- It composes with what exists rather than replacing it: the hash-based oscillation detector is the
  *text* tier; the state delta is the *state* tier. `verified.py` already records per-round oracle
  results and rulings; the queue already records transitions; tool workspaces already hold artifacts.
  The delta reads those, it does not need new instrumentation.
- **It is the adapter seam between the generic core and any domain -- without a premature base class.**
  The core only diffs snapshots. What *populates* `oracle` and `goal` is the domain's business: a code
  domain populates them from pytest; a document domain from its assertion oracle and its grounded
  bracketed citations; a MeF domain from populated form fields. The generic core never contains the word
  any one domain. This is the concrete answer to the open question in Section 8 about non-code progress signals:
  progress for a memo is *checks flipped and citations newly grounded*, not words changed.
- It is the harness's actual claim to novelty, and the thing a contributor can test: *this loop measures
  progress by environment state, so it cannot lie to itself about being finished or about moving.*
  Prompt-loop frameworks measure nothing; that is why they turn into reporters.

### 6.2 The front end: cache vs. ongoing inference at goal intake

Everything above concerns the loop once it is running. The same failure has a twin at the front end,
where the goal is first specified -- and a recurring observation is that the common advice
("just have the model interview you until it understands") quietly makes things worse.

**The finding.** Laban et al. [12] transformed single-shot tasks into "sharded" instructions delivered
across turns and measured 15 leading LLMs. Multi-turn delivery cost an **average 39% drop** across six
generation tasks. Decomposed: aptitude fell only about 15%, but **unreliability rose 112%**. The
mechanisms were exactly the ones an interviewing agent exhibits: models "generate overly verbose
responses, propose final solutions prematurely, make assumptions about underspecified details, and
overrely on previous (incorrect) answers." Critically, their **"Concat" condition -- the same sharded
content re-stated in a single turn -- recovered the lost performance.** The information was never the
problem; its delivery across turns was.

**What exists, and what it gets wrong.** Spec-driven development is mainstream in 2026: GitHub Spec Kit
[13] (with explicit `[NEEDS CLARIFICATION]` markers telling the agent to flag gaps rather than invent),
AWS Kiro's interview mode writing EARS-notation requirements [14], and plan modes in Claude Code and
Cursor. Early adopters report 3-10x higher first-pass success on non-trivial tasks [13]. But nearly all
of them *infer as they go* -- proposing, assuming, and drafting the spec incrementally while the
interview is still in progress -- which is the precise behavior [12] identifies as the failure.

**The principle (an operator's refinement).** Separate *collection* from *inference*:

1. Collect the intake **literally**, into a durable file, with **no model inference during collection**.
   Each answer is logged verbatim. Nothing is proposed, assumed, or drafted.
2. **"Done" is a coverage check, not a model judgment**: the intake has a fixed set of required fields
   for its branch, and completes when they are all filled (plus an explicit user override). The model
   never decides the spec is finished -- that would be the false-success trap of Section 2.3 at the front
   door.
3. **Inject once.** The completed file is delivered to the model in a single consolidated turn -- the
   Concat condition [12] -- accompanied by a reference card stating what to do with it.

This is the same recency-and-poison dynamic found inside the worker loop (Section 5, H1/H2 and the
context guard), at the other end of the pipeline: stacked failed drafts poison a 27B worker; stacked
Q&A turns devalue question one for the architect. The fix is identical at both ends -- **consolidate,
don't shard.**

**Karpathy's three layers, and where the harness sits in them.** Karpathy's working method for agents
is three layers [15]: **Spec** (have the agent interview you about the actual goal *before* any code,
then break the work into checkpoints), **Verifier** ("the external signal the ghost can't generate about
itself"), and **Environment** (the persistent world model -- rules, context, and tools carried across
sessions). The mapping onto this harness is direct: the intake described here *is* Layer 1; the frozen
oracle, the state delta, and the skeptic *are* Layer 2 -- and Section 2's evidence that only external
signal moves the loop is the empirical case for why that layer must exist; the fleet config, ledger,
queue, and rule files are Layer 3. His enforcement tiers -- "always do" (autopilot), "ask first," and
"never do" (enforced by hooks, not requests) -- correspond to the harness's oracle gates and the
autonomy slider [16]. On the "north star" itself, an operator's skepticism stands and Karpathy's own
structure agrees with it: the north star is not a layer. A one-line north star ("I want an autonomous
system") gives an agent almost nothing to infer from; a five-thousand-character spec gives it a great
deal. The north star is only as good as the spec written under it, which is why Layer 1 is an interview
and not a slogan.

**Design consequences for the intake.**

- **The first question sets the autonomy slider.** Whether the project is *in the user's domain*
  (a contractor building an estimator) or *outside it* (a CPA building an LLM harness) determines how
  much of the output the user can verify -- and therefore how high the leash can safely be set [16].
  In-domain users can run longer before a draft; out-of-domain users should see drafts often.
- **Ask for the leash, not a time estimate.** "How far should this run before you see a draft?" is the
  autonomy-slider question. "How long will this take?" is not askable: duration estimation is among the
  things models are worst at, and it invites the premature-conclusion behavior of [12].
- **Ten goals for the user who cannot articulate one.** When the human cannot supply the variable, the
  system generates ten candidate goals around the fragment it was given and lets the loop test them.
  This is Section 5's bounded-variability principle applied at goal time rather than mid-loop, and it is
  kept honest by the same thing: the verifier and the state delta decide which candidates actually moved.
- **Log verbatim, in any input modality.** Answers may arrive by speech-to-text with broken grammar or
  by typing; the collection logs them literally and never "cleans them up," because the cleaned version
  is an inference.

The novelty claim, stated carefully: the *questionnaire* and the *spec file* are ordinary. The
contribution is the **discipline** -- no-inference collection, coverage-defined done, single consolidated
injection -- and the fact that it rests on a measured result ([12]) rather than a preference.

## 7. What already exists in this harness vs. what is new

Already built and tested (as of this note):

- Frozen deterministic oracles gating each round; `ACCEPT` requires the oracle to pass (a per-job
  form of evidence-carrying done).
- A grounded, grammar-constrained skeptic that asks three angled questions (technical / big picture
  / improvement) and never rules -- the "ask, don't direct" observer of H6.
- A hash-based oscillation detector (A->B->A ping-pong, fixed-point repeat) and a semantic
  `stuck()` check that halts a job when the goal stops moving.
- A revert guard: a disruption that regresses a previously passing output is discarded (H4).
- Explicit kickbacks: every REDO states *why* (the oracle's own assertion message, the skeptic's
  questions, the judge's reason) so the next turn has something new to act on.
- A durable job queue with capacity-aware, cache-affine scheduling, and an event-driven wake on
  completion.

New (proposed, not yet built):

- The **state-delta progress ledger** (item 1) -- the ground-truth progress signal.
- **Stuck-vs-waiting** discrimination and contextual thresholds (H3).
- The **goal-level** versions of the two exits: an evidence-carrying "goal done" and a stagnation
  GAP stop (today these exist only per job).
- **Stagnation as a wake event** carrying stuck-context (item 5).
- A **queue-driven conductor** that re-plans from the delta (item 6).

## 8. Open questions for contributors

These are the places where more heads would help. Disagreement welcome.

- **What is the right progress signal for non-code work?** For code, "tests passed" is a clean state
  delta. For a written deliverable or a research task, what observable state change distinguishes progress from
  rephrasing? Oracle pass counts? Newly grounded citations? Something else?
- **How should stuck-vs-waiting be decided?** In-flight action tracking handles the obvious case.
  What about an architect that is legitimately deliberating across turns with no tool calls?
- **Is a 4B observer actually good enough for semantic stagnation detection over compressed state**,
  or does this need a mid-size model? The asymmetry argument (wrong questions are cheap) says small is
  fine; it has not been measured at scale.
- **What disruption schedule works?** Untried action first, then questions, then angle rotation is a
  guess. Is there a measurably better order, or should it be sampled?
- **Where does the architect's persistence live** -- a re-woken stateless frontier session over a
  durable ledger (the current design), or a genuinely persistent local model? The former is cheap and
  robust; the latter is the fully closed loop with no token cost beyond electricity.
- **How many stagnant disruptions before a GAP?** K=3 is a guess. Too low and real slow work is
  abandoned; too high and the loop burns compute doing nothing.

## References

[1] Liang, T. et al. *Encouraging Divergent Thinking in Large Language Models through Multi-Agent
Debate.* EMNLP 2024. https://arxiv.org/abs/2305.19118 -- introduces Degeneration-of-Thought (DoT)
and the Multi-Agent Debate (MAD) framework; finds an adaptive break and a modest tit-for-tat level are
required.

[2] Madaan, A. et al. *Self-Refine: Iterative Refinement with Self-Feedback.* NeurIPS 2023.
https://arxiv.org/abs/2303.17651

[3] Empirical limits of iterative self-refinement: bounded gains without external feedback (<= +1.8pp
over five iterations vs. +80% with guided feedback), monotonic self-bias amplification, and error
detection as the bottleneck. See *How Many Tries Does It Take? Iterative Self-Repair in LLM Code
Generation Across Model Scales and Benchmarks* (https://arxiv.org/pdf/2604.10508), *CoRefine:
Confidence-Guided Self-Refinement* (https://arxiv.org/pdf/2602.08948), and the self-refinement
survey at https://www.emergentmind.com/topics/self-refinement.

[4] *From Confident Closing to Silent Failure: Characterizing False Success in LLM Agents.*
https://arxiv.org/pdf/2606.09863 -- false success accounts for roughly half of failures in
single-control domains versus ~3% in dual-control domains (tau2-bench, 9,876 trajectories). The
two settings differ in more than verifiability; this is not a measured effect of adding a verifier.

[5] *The Unreliable Progress Bar: Can LLM Agents Reliably Report Task Progress Throughout Execution?*
https://arxiv.org/html/2609.08589 -- progress reports change with continuation conditions while
actual progress stays fixed.

[6] *When May an Agent Stop? Evidence-Carrying Termination for Tool-Using LLMs.*
https://arxiv.org/html/2608.23623 -- ECT: 0/288 unsafe completions vs. 252/288 for a termination
critic.

[7] MAST: multi-agent failure taxonomy (NeurIPS 2025); premature termination as FM-3.1. Summarized
with related failure modes at https://latenteval.ai/research/multi-agent-failure-modes and
https://latenteval.ai/glossary/premature-termination-agents.

[8] OpenHands StuckDetector documentation. https://docs.openhands.dev/sdk/guides/agent-stuck-detector
-- five patterns (repeating action+observation, repeating action+error, monologue, alternating,
context-window loop); four identical pairs trip it; one specific nudge per streak.

[9] OpenHands issue #5355, *Loop detection kills agents that are waiting on long-running processes.*
https://github.com/OpenHands/OpenHands/issues/5355 -- the false-positive failure of hardcoded stuck
thresholds.

[10] *Beyond Task Completion: Revealing Corrupt Success in LLM Agents through Procedure-Aware
Evaluation.* https://arxiv.org/pdf/2603.03116 -- completion is not the same as correct process.

[11] *When Agents Commit Too Soon: Diagnosing Premature Commitment in LLM Agents.*
https://arxiv.org/pdf/2606.22936

[12] Laban, P., Hayashi, H., Zhou, Y., Neville, J. *LLMs Get Lost In Multi-Turn Conversation.*
Microsoft Research / Salesforce Research, May 2025; ICLR 2026 Best Paper. https://arxiv.org/abs/2505.06120
-- 15 LLMs, six generation tasks: sharded (multi-turn) delivery costs an average 39%; aptitude -15%,
unreliability +112%; the "Concat" single-turn condition recovers performance.

[13] GitHub Spec Kit and the spec-driven-development landscape (2026): `[NEEDS CLARIFICATION]` markers,
3-10x first-pass success reports. https://ssojet.com/blog/prd-spec-templates-ai-agents and
https://www.augmentcode.com/tools/best-spec-driven-development-tools

[14] AWS Kiro: interview mode producing EARS-notation requirements; the Constitution / Specify / Plan /
Tasks / Implement lifecycle. https://dev.to/aws-builders/kiros-agentic-ide-hype-hope-and-hard-truths-1dpi

[15] Karpathy's three-layer method for working with agents -- Spec (interview before code, then
checkpoints), Verifier (the external signal the model cannot generate about itself), Environment (the
persistent world model) -- and the always-do / ask-first / never-do enforcement tiers.
https://vensas.de/en/blog/karpathy-three-layers and https://guides.kno2gether.com/karpathy-method/

[16] Karpathy, A. *Software 3.0* keynote, Y Combinator AI Startup School, June 2025: the autonomy
slider, partial autonomy, "keep it on the leash." https://www.latent.space/p/s3


## 9. Measured comparison: does the decision step earn its cost?

Run 2026-09-19 on the reference deployment. One task, two arms, identical goal text, criteria and frozen
verifier; distinct assignment names so the arms never share a workspace. Task: a duration
formatter (comparable in shape to one outcome of the earlier multi-outcome goal, briefed with the RULE and not the
expected output strings so a first-attempt miss was plausible).

| arm | architect decisions | skeptic | outcome | wall clock |
|---|---|---|---|---|
| baseline (`--max-decisions 0 --no-skeptic`) | 0 | off | DONE, criterion met with evidence | 65s |
| full loop (`--max-decisions 3`) | 0 | on | DONE, criterion met with evidence | 76s |

Both delivered working code, verified independently of the harness. **The decision step was never
exercised, because neither arm failed.** On this task it added 11s and nothing else.

### The finding that matters, and it is not the table

Before this pair of runs, the SAME task failed in BOTH arms — including the full loop with three
decisions available. The worker's only defect was a separator: it emitted `1m 1s` where the
verifier expected `1m1s`. Every stated rule had been followed.

The cause was the verifier, not the model and not the loop. Its minutes check used bare
assertions, so the failure carried no message. The worker received "no achievement" three rounds
running, and the architect — which could see the worker's source but not the expected output —
parked a question instead of repairing. Adding expected-vs-actual to every assertion
(`format_duration(61): expected '1m1s', got '1m 1s'`) was the only change between the failing runs
and the table above. After it, the plain batch runner succeeded unaided.

**Provisional conclusion, one task, single sample.** Feedback quality did the work the decision
step was built to do. A frontier decision per stall lost to a batch runner twice, both times
because the failure text was uninformative. This is consistent with the field scan (section 8): no
published evidence shows a critic agent paying for itself on code, and Cognition argues against
multi-agent structure for coding specifically.

### What is therefore NOT established

- **Autonomous recovery.** No goal has been carried to completion by architect decisions alone.
  The earlier multi-outcome goal's completion was assisted: its final run made no architect decisions and dispatched an
  assignment repaired by hand. The decision step is implemented, unit-tested, and has taken live
  PARK/RETAIN/STOP actions; it has never produced a REPAIR that reached acceptance unaided.
- **That the skeptic improves outcomes.** Its challenge did reach the architect before the action
  applied, and the architect retained with a reason — the mechanism works. Whether it changes
  results is unmeasured, because the only failure it saw was caused by a defect it could not have
  named.
- **Any benefit on harder tasks.** One formatter is not evidence about long-horizon work. The
  honest claim is that on short, well-verified tasks the loop is currently indistinguishable from
  a batch runner with a good verifier.

### The transferable lesson

Every defect that mattered across this project was found by running the system, not by its tests,
and the most expensive one was a check that knew the answer and would not say it. Before adding a
reasoning layer to recover from failures, make the failures legible. That is cheaper, and here it
was sufficient.
