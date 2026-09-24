# Handoff feature wiki

The harness in this repository is fleet. Local workers author files. A frontier model plans, judges, and decides. The harness checks, integrates, and continues from what was actually accepted. Packet completion is not project completion.

This page is the callable surface. Each heading is an exact command a search can match. Flags are the ones `--help` prints. The ordered entry for a new project is [the README start-here section](../README.md#start-here-one-project-in-order).

Nothing here is a claim that an unattended night succeeds. The [known limitations](../README.md#known-limitations) still apply.

## What a prolonged run is allowed to end on

Walkaway means an approved goal keeps producing checked work across many turns without a person carrying the next instruction. A status sentence, a red adversarial test, or one trivial failure is not that ending.

These ends are deliberate:

- Scope approval and assignment approval. `start` stops before either is skipped.
- A never-rule from intake S4 or E4. The assignment is parked. It is not dispatched.
- Review cadence E3. `A` stops after one finished assignment. `B` stops after one batch. On a branch-B project, `C` and `D` are treated as `B` for that first run. The letter is not a worker count.
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

`python run/conductor.py start <folder> --name <project> [--package <path>]`

Default package when `--package` is omitted: `intake/packages/<name>/` under this repository.

| Flag | Meaning |
|---|---|
| `folder` | Bound as `project_root`. `observed.md` is a file list, with no model. At 200 files the list says it stopped. |
| `--name` | One path segment. |
| `--package` | Package directory. |

Package files: `project.json`, `observed.md`, `proposed.json`, `<name>.json`, `<name>.md`, `scoped.md`, `approval.json`. Kickoff with `--package` also writes `kickoff.md`.

`start` resumes the first incomplete step. If intake is missing and Claude CLI is on `PATH` (or `FLEET_ASSIST` is set), it calls the drafter once to write `proposed.json`; ask before sending the observed folder and questions to that model. Without a drafter it prints the self-guided intake commands. It does not overwrite `observed.md` or `scoped.md`, confirm a draft, approve the plan, or start a worker. A rejected plan, or a scope page that was edited and approved again, is planned again on the same goal id. `--as` records an actor name but is not human authentication; an agent must not self-confirm or self-approve.

Goal criteria come from the confirmed S2 lines and, when a machine can check the work, from V2 and V3. A done-when outcome must set `integrator` to `handoff` and a `dest`. The integration group and frozen check are recorded before assignment approval. `approve` approves the plan; `approve-map` freezes the project baseline for autonomous mode.

Related calls:

- `python run/conductor.py continue <package>`
- `python run/conductor.py assist <package> [--drafts FILE]`
- `python run/conductor.py confirm <package> <qid> --as <name>`
- `python run/conductor.py scope <package>`
- `python run/conductor.py approve-scope <package> --as <name> [--note TEXT]`

`assist` with no `--drafts` sends the observed folder and the intake questions to Claude CLI (`claude -p`); `FLEET_ASSIST` names a script instead. Scope and planning also default to Claude CLI; those three script hooks do not replace every architect call. The standalone `architect.py --engine codex` flag does not switch the connected project path. Workers and the skeptic use registered OpenAI-compatible HTTP endpoints; a CLI-only skeptic is not implemented. Proposals stay in `proposed.json`.

Intake question ids, fixed, no model: `Q0`, `S1`, `S2`, `S3`, `S4`, `S5`, `S6a`, `S6b`, `V1`, `V2`, `V3`, `E1`, `E2`, `E3`, `E4`. `Q0` `A` asks `S6a` and not `S6b`. `Q0` `B` asks `S6b` and not `S6a`. `V1` that is only `E` skips `V2` and `V3`.

E3 letters: `A` every finished piece, `B` one batch, `C` until done or stuck, `D` overnight. Branch B does not run past `B` on the first pass.

## Conductor

`python run/conductor.py --help`

`python run/conductor.py list`

Prints goal ids.

`python run/conductor.py plan <goal> --criterion TEXT [--criterion TEXT ...] [--budget N] [--plan-file PATH] [--project-root PATH] [--integration PATH] [--integration-check PATH]`

Opens a goal and proposes a plan. `--criterion` is repeatable; each value is one acceptance sentence. `--budget` is an integer cap on assignments; `0` means no cap. `--plan-file` is a JSON object with an `outcomes` list (or `jobs`) of contracts, used instead of a model. `--project-root` is the directory where declared relative sources resolve. `--integration` is a JSON file of groups, destinations, and a frozen check. `--integration-check` is that check's Python source.

`python run/conductor.py approve <goal_id> --as <name>`

Approves the goal and the proposed plan. Nothing dispatches before this.

### Autonomous project mode

Packet-only mode dispatches the approved plan and stops; **packet completion is not project completion**. Autonomous mode adds one connected proof: approved full `scoped.md` → approved interface map → receipt-bound candidate → one fresh-process launch journey → one owned, evidence-based repair if needed → a promoted checkpoint. The reusable entry point is `run/conductor.py`.

`python run/conductor.py autonomous <package> --decisions N --seconds N [--workers NAMES] [--map FILE --as NAME]`

`--decisions` and `--seconds` are required integers. `--workers` is a comma-separated list of worker names. Default is `cluster`. `--map` is a JSON file and is accepted only together with `--as`. The file is one object: `components` is a list of `{id, path, provides, calls?}`, and `milestone` is `{id, command}` where `command` is a list of strings, the first usually the Python executable and the last the launcher path. A person can skip `--map` and approve the derived map with `approve-map --from-proposed` instead.

Binds the entire approved `scoped.md` by path and exact-byte hash, records the launched-and-working milestone separately from the North Star, and sets autonomous mode with a durable budget. One entry walks the ordinary path with a human stop at each consequential step: from a freshly approved scope with no plan it runs the ordinary planner and stops at `AWAITING_PLAN_APPROVAL` (or `AWAITING_INTAKE` when the intake done-when answers are not in yet); once the plan is approved it derives and proposes an interface map and stops at `AWAITING_MAP_APPROVAL`; once the map is approved it runs. It never silently approves a plan or a map. With no approved map the loop stops `BLOCKED`. `--decisions 0` is a visible choice, never a hidden zero.

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

`python check/watchdog.py [--workers NAMES] [--gen-timeout SECONDS] [--loop SECONDS] [--patches]`

`--loop 0` is one pass. `--patches` asserts the cluster's MLX patches are active. That check is about one lab, not a portable requirement.

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

Settings keys in `fleet_settings.example.py`: `MAC_USER`, `MAC_LOCALAI`, `MAC48`, `MAC24`, `SSH_KEY`, `MINER_HOST`, `CLUSTER_MODEL`, `PRIMARY_WORKER`, `SKEPTIC_WORKER`, `TOOL_SERVICE_URL`, `TOOL_SERVICE_TOKEN_FILE`, `FLEET_DISPATCH_DIR`, `RECENCY_ROOTS`. For a registered generic endpoint, the gitignored local file needs only `PRIMARY_WORKER` and `SKEPTIC_WORKER` naming registered workers; do not copy the Mac/miner placeholders. `MAC48` and `MAC24` name lab-specific machines. The bundled service uses `runs/tool-service/token.txt` by default; `FLEET_DISPATCH_DIR` is only an override.

## Not shipped

Private project data, generated runs, domain-specific corpora and adapters, and development-only material are not included. The optional `regdb` hook in the runtime does not provide a corpus.

## Shipped after the walkaway audit

- `python status.py` names an approved unfinished goal and prints `python run/orchestrate.py <goal>`. `python status.py --resume` runs that same goal. Hung jobs still block a resume. It does not open a new goal.
- `trivial_error` is a failure class for syntax and name errors. `ModuleNotFoundError` and `ImportError` stay `missing_inputs_or_tools`. `STOP` is refused when every recorded failure is one of those two. The repair stays on this goal.
- A pass or fail example that is not an `assert` line does not fail the project check. The plan parks a question on criterion `examples`. `python run/conductor.py sign-off <goal> examples --by <name>` closes it.
- `scan_emitted` reads the worker's deliverable. A forbidden call there keeps the round from being accepted and sends a REDO on the same card.

## Not built yet

These are directions, not commands.

- Never-rules do not sandbox the process. They refuse acceptance when the emitted source text matches the rule. A worker can still be stopped only after that text exists.
