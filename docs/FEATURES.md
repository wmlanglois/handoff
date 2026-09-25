# Handoff feature wiki

The harness in this repository is fleet. Local workers author files. A frontier model plans, judges, and decides. The harness checks, integrates, and continues from what was actually accepted. Packet completion is not project completion.

This page is the callable surface. Each heading is an exact command a search can match. Flags are the ones `--help` prints. The ordered entry for a new project is [the README start-here section](../README.md#start-here-one-project-in-order).

Nothing here is a claim that an unattended night succeeds. The [known limitations](../README.md#known-limitations) still apply.

The [implementation status](IMPLEMENTATION-STATUS.md) separates shipped fixes from open connections and dated research proposals (source baseline: `221f434`, September 25, 2026).

## What a prolonged run is allowed to end on

Walkaway means an approved goal keeps producing checked work across many turns without a person carrying the next instruction. A status sentence, a red adversarial test, or one trivial failure is not that ending.

These ends are deliberate:

- Scope approval and assignment approval. `start` stops before either is skipped.
- A never-rule from intake S4 or E4. The assignment is parked. It is not dispatched.
- Review cadence E3. `A` stops after one finished assignment. `B` stops after one batch. `C` runs until done or stuck; `D` is labeled overnight but currently uses the same review-pause behavior as C. Neither grants standing approval or starts later milestones. `autonomous --delegate` separately covers existing scope, plan, and derived-map approvals. Q0 does not cap autonomy. The letter is not a worker count.
- `--stagnation-k` on `verified.py`: consecutive rounds with no achievement and no new evidence.
- `--investigation-budget` on `verified.py`: evidence keeps arriving and achievement never does.
- An architect action, one of nine: `REPAIR`, `FOLLOWON`, `PARK`, `STOP`, `INVESTIGATE`, `REVISIT`, `PROPOSAL`, `QUESTION`, `CHALLENGE`.
- Integration. A goal with a declared group is not complete until the frozen project check passes. `assert True` is not that check.
- Queue claim and lease. A job whose claim was reclaimed is not executed twice.
- Wall-clock `--max-seconds` and assignment `--budget`.

These are not the job:

- This code-only publication does not include private test suites, fixtures, or a CI workflow. It must not be interpreted as a passing public test release.
- A draft from `assist` is not a user answer until `confirm`.
- A red oracle on one round is repair input. Oscillation escalates. It does not open a replacement goal.

## First use

`python run/conductor.py start <folder> --name <project> [--package <path>] [--project-kind local|git-existing|git-new] [--intake-mode guided|brief|defer] [--brief FILE] [--draft] [--tool-mode chat-only|tools]`

New non-deferred setup requires `--tool-mode` before model calls. `chat-only` denies worker tool execution; `tools` authorizes workspace file/code tools on the configured trusted service host for all new assignments, including repairs and follow-ons. The service is not started automatically. The choice is persisted as `tool_mode` in project.json and the goal, and passed to the planner and cards. Worker self-checks do not replace acceptance checks or disclose hidden oracle code. Existing packages without the field preserve legacy per-contract settings with a notice; no authority is inferred from cadence, registration, or delegation.

The mode is immutable after proposal/approval: reject an unapproved proposal before selecting a different mode, or create a new explicitly authorized goal for approved work. Do not reset the project's spending limits to change modes. Resume without the flag reuses the saved choice. Deferred setup may omit the choice until resumed through `start`.

Default package when `--package` is omitted: `intake/packages/<name>/` under this repository.

| Flag | Meaning |
|---|---|
| `folder` | Bound as `project_root`. `observed.md` is a file list, with no model. At 200 files the list says it stopped. |
| `--name` | One path segment. |
| `--package` | Package directory. |
| `--project-kind` | Required when choosing first use: local folder, existing Git worktree, or a new local Git repo (`git init` only; no remote/push). Stored separately from `project_root`. |
| `--intake-mode` | Guided questions, existing brief as context, or a no-token defer. Required on first use. |
| `--brief` | UTF-8 file for brief mode. Stored as `brief.md`; never treated as confirmed answers or an approved scope. |
| `--draft` | Explicitly allow one model call for proposed intake answers in guided/brief mode. No automatic call merely because Claude is installed. |

Package files: `project.json`, `observed.md`, optional `brief.md` and `proposed.json`, `<name>.json`, `<name>.md`, `scoped.md`, `approval.json`. Kickoff with `--package` also writes `kickoff.md`.

`start` prints the absolute project/package paths first; relative paths resolve against the current shell directory. With no first-use choices it stops before creating files or calling a model. `defer` creates minimal local package state and stops with no model call. Guided/brief modes print the same intake questions; only explicit `--draft` calls the drafter once, and `assist` remains an explicit later option. A brief supplies context but cannot skip answer confirmation, scope review, or approvals. `start` does not overwrite `observed.md` or `scoped.md`, confirm a draft, approve the plan, or start a worker. A rejected plan, or a scope page that was edited and approved again, is planned again on the same goal id. `--as` records an actor name but is not human authentication; an agent must not self-confirm or self-approve.

Existing packages with confirmed intake or already-recorded drafts continue without retroactive first-use choices. No new model call is inferred from that compatibility path.

Goal criteria come from the confirmed S2 lines and, when a machine can check the work, from V2 and V3. The planner is handed only these intake criteria; a criterion added to the goal another way that no accepted outcome covers is recorded as an `UNPLANNED_CRITERION` with guidance (supply it via `plan --plan-file`, or run it as a separate goal), not dropped silently. A done-when outcome must set `integrator` to `handoff` and a `dest`. The integration group and frozen check are recorded before assignment approval. `approve` approves the plan; `approve-map` freezes the project baseline for autonomous mode.

Related calls:

- `python run/conductor.py continue <package>`
- `python run/conductor.py assist <package> [--drafts FILE]`
- `python run/conductor.py confirm <package> <qid> --as <name>`
- `python run/conductor.py scope <package>`
- `python run/conductor.py approve-scope <package> --as <name> [--note TEXT]`

`assist` with no `--drafts` sends the observed folder and the intake questions to Claude CLI (`claude -p`); `FLEET_ASSIST` names a script instead. Scope and planning also default to Claude CLI; those three script hooks do not replace every architect call. The standalone `architect.py --engine codex` flag does not switch the connected project path. Workers and the skeptic use registered OpenAI-compatible HTTP endpoints; a CLI-only skeptic is not implemented. Proposals stay in `proposed.json`.

Intake question ids, fixed, no model: `Q0`, `S1`, `S2`, `S3`, `S4`, `S5`, `S6a`, `S6b`, `V1`, `V2`, `V3`, `E1`, `E2`, `E3`, `E4`. `Q0` `A` asks `S6a` and not `S6b`. `Q0` `B` asks `S6b` and not `S6a`. `V1` that is only `E` skips `V2` and `V3`.

E3 letters: `A` every finished piece, `B` one batch, `C` until done or stuck, `D` overnight. The chosen letter is honored within budget; Q0 (ability to inspect code) does not downgrade it.

## Conductor

`python run/conductor.py --help`

`python run/conductor.py list`

Prints goal ids.

`python run/conductor.py plan <goal> --criterion TEXT [--criterion TEXT ...] [--budget N] [--plan-file PATH] [--project-root PATH] [--integration PATH] [--integration-check PATH] [--tool-mode chat-only|tools]`

`--tool-mode` sets the same execution policy for this new standalone goal. Omission keeps the legacy per-contract behavior. Imported contracts are checked against explicit chat-only denial; tools mode provisions new contracts before the plan gate.

Repair and revisit carry: tools workers use staged `_carry/` references. Chat-only workers receive complete UTF-8 carry text in the request after SHA-256 verification on executor start, including resume. The initial request containing carry is protected from conversation trimming. Missing, altered or non-text carry raises an explicit carry error; essential context exceeding the estimated input budget raises context overflow before generation. Hidden oracle code is not added to this reference. No server configuration or execution authority changes.

Opens a goal and proposes a plan. `--criterion` is repeatable; each value is one acceptance sentence. `--budget` is an integer cap on assignments; `0` means no cap. `--plan-file` is a JSON object with an `outcomes` list (or `jobs`) of contracts, used instead of a model. `--project-root` is the directory where declared relative sources resolve. `--integration` is a JSON file of groups, destinations, and a frozen check. `--integration-check` is that check's Python source.

Before approval, the proposed plan must be dependency-closed and launchable. Every `needs` edge names a different planned or already accepted assignment, not a filename; orphaned and cyclic edges are refused. If the approved launch milestone says `python <file>` and the baseline lacks that file, an outcome must produce that exact path. One destination has one owning outcome; checks for multiple criteria on the same file belong in one outcome's `done_when`.

`python run/conductor.py approve <goal_id> --as <name>`

Approves the goal and the proposed plan. Nothing dispatches before this.

`python run/conductor.py reject-plan <goal_id> --reason TEXT --as <name>`

Sends an unapproved proposed plan back to the planner on the same goal. Records the reason, keeps the approved scope, and clears only the proposal. The next `start` re-plans. It refuses after approval or dispatch.

### Autonomous project mode

Packet-only mode dispatches the approved plan and stops; **packet completion is not project completion**. Autonomous mode adds one connected proof: approved full `scoped.md` → approved interface map → receipt-bound candidate → one fresh-process launch journey → one owned, evidence-based repair if needed → a promoted checkpoint. The reusable entry point is `run/conductor.py`.

`python run/conductor.py autonomous <package> --decisions N --seconds N [--workers NAMES] [--map FILE --as NAME] [--plan-file PATH] [--delegate NAME] [--skip-preflight] [--tool-mode chat-only|tools]`

`--decisions` and `--seconds` are required integers. `--workers` is a comma-separated list of worker names; when omitted the configured `PRIMARY_WORKER` is used (`cluster` if unset). `--plan-file` is a JSON object with an `outcomes` list of contracts; at the planning stage it supplies the proposal instead of calling the model. It is still gated, approved interactively or under delegation, and mapped through the same path. A rejected supplied plan is reported, not model-replaced; the flag does not replace an already-approved plan and does not compile a Markdown backlog. `--map` is a JSON file and is accepted only together with `--as`. The file is one object: `components` is a list of `{id, path, provides, calls?}`, and `milestone` is `{id, command}` where `command` is a list of strings, the first usually the Python executable and the last the launcher path. A person can skip `--map` and approve the derived map with `approve-map --from-proposed` instead.

Binds the entire approved `scoped.md` by path and exact-byte hash, records the launched-and-working milestone separately, and sets autonomous mode with a durable budget. Without delegation, the ordinary path stops at `AWAITING_PLAN_APPROVAL` (or `AWAITING_INTAKE`), then `AWAITING_MAP_APPROVAL`, then permits execution after approval. `--delegate NAME` records standing approval of an **existing** scope page, the proposed plan, and the derived map. It does not generate missing intake/scope or waive budgets, never-rules, or human-only sign-offs. Delegated resume also handles an already-proposed map: while no approved map exists, the map is derived from the current approved plan and an outdated proposal is replaced before approval. Failed derivation returns `MAP_DERIVATION_FAILED`, not dispatch. An already-approved stale map still fails the execution validity check. `--decisions 0` is a visible choice, never a hidden zero.

Before `run_goal`, default preflight canaries selected coding lanes and the named skeptic. When any assignment uses tools it also checks every selected coding lane's tool-call capability and requires the tool service. Otherwise the service is reported `not-needed` and not probed; worker and skeptic checks still run. A failure returns `PREFLIGHT_FAILED`. An explicitly empty `SKEPTIC_WORKER` produces a no-independent-review warning. `--skip-preflight` or any nonempty `HANDOFF_SKIP_PREFLIGHT` bypasses this entire check (even `0`); this is a diagnostic escape hatch, not readiness evidence. Preflight is not automatically run by `start` before intake/planning.

This is one goal's connected execution loop, not a durable controller over an entire multi-milestone backlog. That remaining connection is [#7](https://github.com/wmlanglois/handoff/issues/7); Markdown import is [#21](https://github.com/wmlanglois/handoff/issues/21).

The recorded time and decision budgets are cumulative across resume and restart. A repeated `autonomous` command does not reset them. A `BUDGET` stop reports what remains. Time is charged from a persisted active marker, including the gap after a crash, in whole seconds. A failed spend write stops the loop. Admitting a journey repair spends one decision. If the ledger also has `run_budget.repairs`, that ceiling stops new repairs on its own. There is no `autonomous` flag for the repair ceiling. An in-flight worker is left to finish.

`python run/conductor.py approve-map <goal_id> (--spec FILE | --from-proposed) [--fixture] --as <name>`

A person approves one versioned interface map: components, owned paths, public calls, `calls` edges, and one executable milestone command. `--from-proposed` approves the map `autonomous` derived from the approved plan (no hand-written JSON). `--fixture` marks a TEST-FIXTURE approval (never reported as a real human approval); omit it for a real approval. The approval is bound to the approved scope hash **and to a signature of the full assignment contracts** — each owned destination's `provides`, `consumer`, and `needs`: a scope change, an added/removed destination, or a changed interface (provides/consumer/needs) makes the approval **stale** and it must be re-approved; a same-interface repair (`replaces`) of an existing destination does not.

At approval the project baseline is **frozen atomically and fail-closed**: project_root is snapshotted to a temp tree, swapped in with one rename, and marked complete with a per-file manifest written last. If the snapshot cannot complete, the approval is **refused** — an approved map never falls back to the mutable project_root, and a partial/missing/corrupt snapshot fails closed rather than reading the live tree.

The milestone command cannot report the project `DONE` on a keyword or filename match. After a journey passes on the real candidate, the command is run once more against a **behavioural mutant** — each accepted deliverable replaced by a module that still imports and still exposes every public name but whose callables return a value equal to nothing. Only if the command then **fails** is its success shown to depend on the delivered behaviour (`sensitive`). An import-only command (`from x import value`) is `independent` and refused; a probe timeout or execution error is `inconclusive` and **fails closed**. This is a sensitivity check, not a quality certificate — the human approves that the launcher is the launched-and-working behaviour. The candidate is assembled from receipt-bound accepted bytes overlaid on the frozen baseline (or the last passing checkpoint); a pre-existing or caller-supplied file is never credited as accepted work, and a failed journey preserves the prior checkpoint. A failed journey admits **one** owned repair card — the failed command and output, the map row, the accepted dependencies (staged at their real package paths through `card_for`, with the accepted producer's bytes), the package launcher taken from the **frozen** candidate (never a drifted project_root), and the prior artifact carried read-only — onto the normal queue/receipt/acceptance path; an ambiguous owner parks a question instead of guessing.

`python run/conductor.py advance <goal_id> [--capacity N] [--max-assignments N] [--max-rounds N] [--simulate OUTCOME] [--live] [--workers NAMES] [--max-seconds N] [--max-decisions N] [--no-decisions] [--no-skeptic] [--deliverable-root PATH]`

Continues until blocked, out of budget, or the review cadence says to show a draft. `--simulate` is the default when `--live` is absent. `--live` goes through the queue and the architect. `--no-decisions` is the batch-runner baseline. `--workers` is a comma-separated list. `--capacity` is how many cards one wave may take. It is not E3.

`python run/conductor.py resolve <goal_id> --answer TEXT --by NAME [--criterion ID]`

Answers a parked question so work can resume. An anonymous answer is refused.

`python run/conductor.py sign-off <goal_id> <criterion> --by NAME [--note TEXT]`

A person accepts a criterion no machine can check.

`python run/conductor.py status <goal_id>`

Prints where the goal stands, including whether `complete()` is true.

`python run/conductor.py fingerprint [--expect HASH]`

Prints a hash of Python source under `run/`. Capture it before a live or evaluation run and pass it back to this command with `--expect`; it exits non-zero on a mismatch. It does not cover root runtime modules or automatically run at dispatch, and is not an execution lock. Give concurrent sessions separate checkouts and do not edit a live runtime; enforced stability is still #9.

`python run/conductor.py memory <action> [flags]`

Actions: `list`, `match`, `withdraw`, `add-lesson`, `contradict`, `skill-result`, `revalidate`, `withdraw-skill`, `legacy-disposition`, `refresh-contract`, `reassess`.

| Flag | Use |
|---|---|
| `--brief` | `match`: text of the work at hand. |
| `--id` | Repeatable. `withdraw`, `contradict`, and skill actions. |
| `--text` | `add-lesson`: the lesson. |
| `--source` | `add-lesson`: the evidence it rests on. |
| `--limits` | `add-lesson`: what the evidence does not show. |
| `--unverified` | `add-lesson`: store even though the source path does not exist. Never offered to a worker. |
| `--reason` | `withdraw` / `contradict`. |
| `--by` | Who is acting. |
| `--ok` / `--failed` | `skill-result`. Exactly one. |
| `--scope` | `skill-result`: what was checked, and where. |
| `--note` | Free note. |
| `--evidence-ref` | `reassess`: evidence the condition holds again. |
| `--narrow` | Repeatable. `reassess`: replacement applicability tags. |

Retrieval happens inside `goals.card_for`. A skill is staged with the contract's declared sources. A local shadow of a skill is not a reliability success.

`python run/conductor.py alternatives <goal_id> <criterion>`

Every approach tried or preserved for that criterion.

`python run/conductor.py revisit <goal_id> <assignment> --why TEXT`

Re-opens a preserved approach under the same acceptance. `--why` is the new evidence.

`python run/conductor.py workers [--brief TEXT]`

What each worker's record says about a kind of work. Ordering happens after capability, health, and capacity. It does not drop a worker forever.

`python run/conductor.py proposals <goal_id>`

Worker proposals and how each was resolved.

`python run/conductor.py proposal <goal_id> <proposal_id> --resolution accept|defer|reject --reason TEXT [--by NAME] [--criterion ID] [--name NAME] [--brief TEXT] [--artifact PATH] [--done-when TEXT] [--oracle CODE] [--reconsider COND]`

`--done-when` and `--reconsider` are repeatable. Accept builds a follow-on assignment. Defer looks again when a condition holds, for example `criterion_met:c2` or `file_present:x.py`.

`python run/conductor.py challenge <goal_id> <criterion> --kind purpose_unmet|counterexample --basis TEXT [--evidence-ref REF] [--by NAME] [--assist]`

Doubts an accepted criterion. The receipt stays. The goal is not complete while the challenge is open. `--assist` runs the skeptic and needs a worker.

`python run/conductor.py challenges <goal_id>`

`python run/conductor.py challenge-resolve <goal_id> <id> --outcome established|refuted|uncertain --why TEXT [--evidence-ref REF] [--reconsider COND] [--by NAME]`

`python run/conductor.py reengage <goal_id> [--root PATH]`

Deferred items whose stated condition now holds. Surfaced once per change.

`python run/conductor.py questions <goal_id>`

`python run/conductor.py question <goal_id> add|resolve|reconsider [--id ID] [--text TEXT] [--criterion ID] [--explanation TEXT] [--distinguishing TEXT] [--why TEXT] [--chosen N] [--evidence-ref REF] [--reconsider COND] [--by NAME]`

`--explanation` and `--reconsider` are repeatable. Competing explanations stay until evidence picks one.

`python run/conductor.py observe <goal_id> <criterion> --root PATH --tool list|read|grep --question TEXT [--target PATH] [--pattern REGEX] [--question-id ID] [--by NAME]`

Read-only. `--root` is the only directory the probe may read.

## Intake and kickoff

`python run/intake.py --help`

| Command | Flags |
|---|---|
| `start <project> [--dir DIR]` | Open an empty store. |
| `next <project> [--dir DIR]` | Next unanswered required question. |
| `answer <project> <qid> <text> [--dir DIR]` | Stored verbatim. |
| `status <project> [--dir DIR]` | Filled and missing. |
| `render <project> [--dir DIR]` | Writes the markdown plus the reference card. |
| `done <project> [--dir DIR]` | Explicit early stop. |

`--dir` is the project package. Without it, files go under `intake/`.

`python run/kickoff.py [--port N] [--name NAME] [--package DIR] [--no-open]`

One browser pass. `--package` stores answers in that package verbatim. `--no-open` does not launch a browser. Default port is 8777.

## Execution

`python run/orchestrate.py <goal_id> [--workers NAMES] [--max-seconds N] [--max-rounds N] [--max-decisions N] [--no-skeptic] [--deliverable-root PATH]`

The live path after assignment approval: claim, lease, verified execution, architect decision. `--max-decisions 0` is the batch baseline with no architect continuation. Review cadence `A` dispatches one assignment per pass and then pauses if work remains. Cadence `B` pauses after the pass.

`python run/verified.py <card> [--max-rounds N] [--resume] [--stagnation-k N] [--investigation-budget N] [--claim-token TOKEN] [--claim-controller NAME] [--redo-mode conversation|rewrite] [--skeptic-bounce on|off]`

One card. `--resume` carries the last correction. `--claim-token` refuses to run if the queue reclaimed the job during spawn. `--redo-mode conversation` keeps the worker thread. `rewrite` starts each round blind. `--skeptic-bounce on` asks one skeptical question in context before the frontier rules.

`python run/queue.py add <card> [<card> ...]`

`python run/queue.py add-dir <dir>`

`python run/queue.py status`

`python run/queue.py drain [--workers NAMES] [--max-rounds N] [--max-seconds N] [--redo-mode conversation|rewrite]`

`--max-seconds 0` runs until ready is empty. Default workers string is `cluster,cluster,pairA,pairB`.

`python run/supervisor.py run [--root PATH] [--workers NAMES] [--max-rounds N] [--max-seconds N] [--redo-mode conversation|rewrite] [--no-probe] [--on-cycle refuse|quarantine]`

`python run/supervisor.py plan [--root PATH]`

`python run/supervisor.py status [--root PATH]`

Overnight wrapper over the queue. `--no-probe` skips the watchdog liveness check. `--on-cycle refuse` is the default.

`python run/tooljob.py <brief> [--worker NAME] [--ws NAME] [--max-rounds N]`

One tool-using worker conversation. Default worker is `PRIMARY_WORKER` (`cluster` if unset), default workspace `fleet-tooljob-test`, default `--max-rounds 16`.

`python run/localrun.py <spec.json>`

Spec steps on the one call path. Each step gets a health canary and a duration ceiling. A step past the ceiling is hung, not "still busy". Exit is non-zero if any step fails.

`python run/repair.py --help`

`python run/repair.py --demo`

Library entry is `repair_loop`. Bare execution prints help. `--demo` shows oscillation escalating instead of riding the round cap. Production repair is `repair_tooljob` inside the verified path, not this script.

## Registry, architect, skeptic

`python run/registry.py connect [--url URL ...] [--no-register] [--no-recheck]`

Separate setup helper: discover common local server ports, qualify/register new endpoints, and re-canary saved workers. `--url` is repeatable and replaces the default discovery list. `--no-register` suppresses new registrations, but saved workers are still rechecked unless `--no-recheck` is also set. A new qualification or successful saved-worker check establishes endpoint readiness; saved entries alone do not. Unchecked saved entries are reported as unverified. Discovery alone does not qualify a model or select primary/skeptic roles. Role settings remain explicit in `fleet_settings.local.py`; `start` does not yet perform this setup for you.

Set `FLEET_REGISTRY` before these commands for project-local storage. Otherwise the per-user registry is shared across clones. The committed `workers.example.json` uses `version: 1` and a `workers` object; its non-reserved placeholder names must match the selected roles. Validate configured models before dispatch; a copied sample is not qualification evidence.

`python run/registry.py add <url> [--name NAME] [--model MODEL] [--force] [--ctx N] [--max-inflight N] [--supports-gbnf] [--prefill-lock] [--reasoning-style none|template_kwargs|budget_field]`

Qualifies a running OpenAI-style server and registers it. `--force` replaces an existing name.

`python run/registry.py list`

`python run/registry.py health <name>`

`python run/registry.py remove <name>`

`python run/registry.py check <url> [--model MODEL]`

Qualifies without registering.

`python architect.py --brief TEXT --output TEXT --note TEXT [--engine claude|codex] [--model MODEL] [--card PATH]`

One frontier ruling. The connected loop calls this through `decide`, not as the way to start a project.

`python skeptic/skeptic.py "<claim>" [--worker cluster|pairA|pairB|spark|vision] [--root PATH] [--rounds N] [--mode challenge|review]`

`challenge` doubts a conclusion. `review` filters a worker's output against its artifact. Default worker is `spark`.

`python status.py [--resume]`

Ledger snapshot for a wake-up. With no flag it reports in-flight jobs, hung jobs, and recent failures, and names one approved unfinished goal with the `orchestrate` command that would resume it. `--resume` runs that same goal through `orchestrate.run_goal` on worker `cluster`. Hung jobs still block a resume. It does not open a new goal.

`python jobs.py [--stale SECONDS] [--all]`

Open jobs in the local job database. `--stale` is an integer number of seconds. An open job older than that is marked hung. Default is `600`. `--all` also prints the ten most recently closed jobs. Without `--all`, closed jobs are not printed.

`python call.py`

Library used by the runners. It is not a help-stable command. Do not pass `--help` and wait. Callers use `call.chat`.

## Checks and release

`python check/preflight.py [--canary-timeout SECONDS]`

Standalone environment diagnostic; canary probes spend model tokens. The autonomous command separately calls its run-specific gate before dispatch. The standalone command scans configured workers and the tool service; it is not required to defer/open intake.

`python check/watchdog.py [--workers NAMES] [--gen-timeout SECONDS] [--loop SECONDS] [--patches]`

`--loop 0` is one pass. `--patches` asserts the cluster's MLX patches are active. That check is about one lab, not a portable requirement.

Liveness protections include generation canaries, request socket timeouts, and the queue subprocess timeout. `call.py` additionally tracks generated-token progress on the prefill-lock streaming path; this is not a universal independent heartbeat. A client timeout does not prove a server stopped executing. Do not assume immediate duplicate dispatch or automatic recovery on every transport.

`python check/recency.py <query> [--days N] [--limit N]`

Which copy of a fact is newest. `--days 0` shows all. Roots come from `RECENCY_ROOTS` in settings. A clean clone searches this repo only.

The development checkout has a private test suite. This code-only publication does not ship that suite or a CI gate. Local command checks are not a substitute for platform validation.

## Environment variables

| Variable | Effect |
|---|---|
| `FLEET_SETTINGS` | Settings module. Default `fleet_settings.local.py`. |
| `FLEET_GOALS_DIR` | Goal ledger directory. Default `runs/goals`. |
| `FLEET_MEMORY_DIR` | Lessons and skills. Default `runs/memory`. |
| `FLEET_MEMORY_DISABLED` | `1` turns retrieval off. |
| `FLEET_INTEGRATE_DIR` | Integration trees. Default `runs/integrate`. |
| `FLEET_PLANNER` | Script whose stdin is JSON `{goal, project_root, criteria, feedback}` and whose stdout is outcome JSON. Unset calls the frontier planner. |
| `FLEET_SCOPE` | Script that reads the scope prompt on stdin and writes `scoped.md` on stdout. Unset calls the frontier model. |
| `FLEET_ASSIST` | Script that reads the draft prompt on stdin and writes proposed answers as JSON. Unset calls the frontier model. |
| `FLEET_RUNS_DIR` | Directory for `verified-<run_id>` workspaces and goal-card files. Default `runs/`. |
| `FLEET_SPARK_LIVE` | `1` calls the `spark` worker from the journey review. Any other value, including unset, records the finding `none found` and does not call a model. |
| `FLEET_REGISTRY` | Path of the worker registry file. Unset uses the per-user state directory. |
| `HANDOFF_SKIP_PREFLIGHT` | Any nonempty value skips the entire autonomous pre-dispatch preflight. Unset for normal validation; `0` still skips it. |

Settings keys in `fleet_settings.example.py`: `MAC_USER`, `MAC_LOCALAI`, `MAC48`, `MAC24`, `SSH_KEY`, `MINER_HOST`, `CLUSTER_MODEL`, `PRIMARY_WORKER`, `SKEPTIC_WORKER`, `TOOL_SERVICE_URL`, `TOOL_SERVICE_TOKEN_FILE`, `FLEET_DISPATCH_DIR`, `RECENCY_ROOTS`. For a registered generic endpoint, the gitignored local file needs only `PRIMARY_WORKER` and `SKEPTIC_WORKER` naming registered workers; do not copy the Mac/miner placeholders. `MAC48` and `MAC24` name lab-specific machines. The bundled service uses `runs/tool-service/token.txt` by default; `FLEET_DISPATCH_DIR` is only an override.

## Not shipped

Private project data, generated runs, domain-specific corpora and adapters, and development-only material are not included. The optional `regdb` hook in the runtime does not provide a corpus.

## Shipped after the walkaway audit

- `python status.py` names an approved unfinished goal and prints `python run/orchestrate.py <goal>`. `python status.py --resume` runs that same goal. Hung jobs still block a resume. It does not open a new goal.
- `trivial_error` is a failure class for syntax and name errors. `ModuleNotFoundError` and `ImportError` stay `missing_inputs_or_tools`. `STOP` is refused when every recorded failure is one of those two. The repair stays on this goal.
- A pass or fail example that is not an `assert` line does not fail the project check. The plan parks a question on criterion `examples`. `python run/conductor.py sign-off <goal> examples --by <name>` closes it.
- `scan_emitted` reads the worker's deliverable. A forbidden call there keeps the round from being accepted and sends a REDO on the same card.

## Generation diagnostics

`WORKER_OUTPUT_LIMITS` in the private settings file maps worker names to positive integer
output-token allowances, shared by coding-worker chat and tool jobs. For example,
`WORKER_OUTPUT_LIMITS = {"my-worker": 4096}`. Values are operator-pinned; omitted workers
retain the existing defaults. Architect auto-adjustment is not implemented. This is not a
server context or sampling setting. Select values that leave input/context headroom.

The bundled tool runtime checks estimated input (including tools and arguments), output
allowance and a 512-token reserve before generation. It preserves the checkpoint rather
than silently trimming tool history. Estimates are not backend token counts. A length stop
saves the returned fragment as unexecuted diagnostic evidence and permits one corrective
continuation; a second length stop halts that attempt, including on resume. Content-filter
stops are distinct and are not automatically retried within that attempt. Outer attempt
budgets remain unchanged. External runtimes receive the output allowance but must implement
their own recovery/context policy; these bundled behaviors are not claimed for them.

Tool-mode delivery feedback requests tool inspection/checks and receipt-bound file mutations,
not a fenced chat answer. The bundled service supports `files.append` and `files.edit` with
`expected_sha256`; edit replaces exactly one nonempty `old_text` with `content`. Read returns
the whole-file hash and supports one-based `start_line` and positive `max_lines`. Each mutation
receipts the complete resulting bytes, so no final full-file rewrite is required. Run checks
before finishing; any subsequent unreceipted change invalidates delivery.

File changes are serialized per workspace, published atomically, and journaled before mutation.
Retrying the same call ID reconciles an interrupted write/append/edit instead of duplicating it.
Reusing an ID with different arguments is rejected. Old cached receipts lacking request identity
require explicit reconciliation rather than guessed replay. Service receipt files are excluded
from file listings and direct file-tool access; `python_run` is still trusted host execution,
not a hostile-code sandbox. External services must advertise incremental actions before use.
Reference-aware scratch cleanup and active-context compaction remain #30 work.

Diagnostic support (#27): chat telemetry includes `generation` with the actual requested
output limit, provider finish reason and usage when supplied. Chat worker logs record it;
the bundled tool loop saves per-call evidence in its checkpoint and reports it in its log.
Null counts mean unavailable, not zero. Input-token estimates and configured context are
labeled separately. This does not yet provide the complete structured failure/architect
routing required by #27, and does not change sampling, model-server settings or acceptance.

## Not built yet

These are directions, not commands.

- Complete guided connection/role setup ([#11](https://github.com/wmlanglois/handoff/issues/11)) and delegated generation of a missing scope from confirmed intake ([#13](https://github.com/wmlanglois/handoff/issues/13)).
- Planning all authorized seeded criteria ([#6](https://github.com/wmlanglois/handoff/issues/6)), Markdown-backlog compilation ([#21](https://github.com/wmlanglois/handoff/issues/21)), and durable delegated cross-milestone continuation ([#7](https://github.com/wmlanglois/handoff/issues/7)).
- Enforced stable execution code across controller/subprocesses ([#9](https://github.com/wmlanglois/handoff/issues/9)); the fingerprint command is a manual diagnostic over `run/`, not a lock or complete runtime identity.

- Never-rules do not sandbox the process. They refuse acceptance when the emitted source text matches the rule. A worker can still be stopped only after that text exists.
