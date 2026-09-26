# Handoff

Handoff is a local-model fleet conductor for work that spans multiple assignments. It records a project's approved scope, asks a planner for connected work, dispatches to configured workers, and accepts files only with matching receipts. Packet acceptance is not project completion: autonomous mode checks an assembled candidate with a fresh-process launch before promoting a checkpoint.

This repository contains the runtime, an example configuration, and operator documentation. It does **not** contain anyone's model weights, machine addresses, credentials, project data, run history, tests, fixtures, or CI workflow. A fresh clone requires configuration before it can dispatch workers.

## Why this project exists and what has been researched

Handoff explores whether a strong conductor can turn smaller, cheaper models into a persistent team: scoped work, isolated contributions, skeptical review, evidence-bound acceptance, and integration into a continuing project. The design is experimental. Passing individual packet checks is not evidence of an autonomous, finished application, and the research is not a claim that Handoff has measured a productivity advantage.

- [Research hypothesis and limitations](docs/RESEARCH-automation-reporter-trap.md) — why activity and plausible reports are not the same as progress.
- [Field scan of agentic loops](docs/FIELD-SCAN-agentic-loops.md) — how other systems plan, bound work, verify, and stop, including gaps in that scan.
- [Prior art compared with Handoff](docs/PRIOR-ART.md) — proposed feature comparisons and missing capabilities. This is a dated research snapshot; its claims about which Handoff features are wired must be rechecked against current code before use.

The [feature and command catalog](docs/FEATURES.md) describes the current callable surface. Research, current implementation, and demonstrated end-to-end reliability are three different things.

The bundled tools path supports a pre-check skeptic coaching turn on a receipted
draft, within the existing turn budget. Review evidence and continuation are recorded
in private checkpoints/JSONL, not solely SQLite. See
[skeptic evidence and logging](docs/FEATURES.md#skeptic-evidence-and-tools-mode-coaching).

See [current implementation status and open work](docs/IMPLEMENTATION-STATUS.md) for the September 25 reconciliation of shipped fixes, remaining connections, and research proposals. The original research documents are preserved; their dated recommendations are not current operator instructions.

## Start here: one project, in order

Before a first run or a substantial environment change, read the
[technical environment guide](docs/ENVIRONMENT-REQUIREMENTS.md). It separates model serving,
tool execution, context/output budgets, role setup and persistent state, with current commands
and explicitly labeled proposed automation. For future inspection/dashboard work, see the
[Phase 2 environment proposal](docs/PHASE-2-ENVIRONMENT.md).

Keep Handoff in its own checkout. Your project folder is separate; `start` points to it. The generated intake package can also live outside the checkout via `--package`. A worker receives staged sources, not the whole project folder.

If an AI agent is guiding setup, establish the project goal, first-use choice, model-call budget, and approval policy up front. Model connections can wait if the person defers. Do not invent endpoint addresses or turn model drafts into confirmed answers. An agent may record answers the person actually supplied, but may not confirm its own guesses under another actor name. In interactive mode, show the material and obtain approval at each gate. With explicit standing delegation, use the supported `autonomous --delegate <name>` path for existing scope, plan, and map approvals without repeatedly asking for the same authorization. Delegation does not supply missing intake answers, waive human-only sign-offs, or authorize unrelated actions. `--as` and `--delegate` record names, not authentication. "Read the README" alone does not authorize model calls or dispatch.

Before running setup commands, the guiding agent should ask: **What do you want to build, and which folder is the project? Should this remain a local folder, use an existing Git repository, or initialize a new local Git repository? Do you want guided kickoff, to bring an existing brief through the same intake review, or to defer for now?** Explain that guided intake helps turn a rough idea into an approved North Star and connected, checkable assignments for smaller workers; model-generated drafts can cost tokens. Obtain permission for model calls individually or under an explicit bounded run authorization; do not ask again for calls already covered by that authorization. If proceeding toward dispatch, establish the worker API URL/model, qualification permission, skeptic seat, and authenticated Claude CLI access. Do not silently substitute a model.

### 1. Choose the first-use path, then configure model seats if needed

Already use LiteLLM? [Preview and import your existing gateway aliases](docs/LITELLM-IMPORT.md)
with `python run/litellm_import.py --config <file>` or `--gateway <url>`.
Preview makes no generation calls or registry changes. Explicit `--apply` qualifies
one selected alias and saves it in the existing registry; role selection stays separate.
No LiteLLM install or server restructuring is required.

You can defer kickoff without connecting any model. For guided kickoff or an existing brief, choose whether to use optional model drafts; neither path skips the intake questions, scope approval, plan approval, or map approval. `start` with no choice prints the resolved project and package paths and stops without creating a package or calling a model. Relative paths resolve against the shell's current directory. `--project-kind local` records a local-folder preference, `git-existing` requires an existing Git worktree, and `git-new` runs `git init` in the project folder only. Handoff never creates a remote or pushes the project.

Use Python 3.11 or newer. The current connected project path uses an authenticated **Claude CLI** (`claude -p`) for intake drafts, scope writing, planning, and architect decisions. This is separate from the local worker fleet. `architect.py --engine codex` exists for a standalone ruling, but it does **not** switch the whole project path to Codex. `FLEET_ASSIST`, `FLEET_SCOPE`, and `FLEET_PLANNER` accept replacement scripts for those individual steps; they do not replace every architect call. If you do not have Claude CLI access, stop before promising an autonomous run.

Ask the user where an already-running OpenAI-compatible local model API is, including its base URL and model name. Do not search unrelated local repositories for addresses or credentials. With permission to make a short generation check, run:

```text
python run/registry.py connect        # first use: find a local model server, register it, re-check saved ones
python run/registry.py check http://your-host:8080 --model your-model
python run/registry.py add http://your-host:8080 --name my-worker --model your-model
python run/registry.py list
```

`connect` is a separate setup command, not yet invoked automatically by `start`. It discovers common local server ports, qualifies new registrations, and re-checks saved workers. `connect --url <url>` restricts discovery to the supplied address (repeatable). A saved configuration alone is not readiness: failed checks do not count as ready, and `--no-recheck` leaves saved entries unverified. The commands above are alternatives for discovery or explicit setup, not a requirement to qualify the same model three times. `check` does not register; `add` qualifies and registers.

The registry defaults to a per-user file shared by clones. For a project-local registry, set `FLEET_REGISTRY` **before** running setup and keep that setting in the environment used to launch Handoff. In PowerShell: `$env:FLEET_REGISTRY = "C:\path\to\project\workers.json"`; in a POSIX shell: `export FLEET_REGISTRY=/path/to/project/workers.json`. The [`workers.example.json`](workers.example.json) envelope is `{"version": 1, "workers": {...}}`; replace placeholders and use non-reserved names such as `my-worker`. Handwritten configuration still needs qualification/health checks.

Registration does not select roles. Create the Git-ignored `fleet_settings.local.py` with `PRIMARY_WORKER = "my-worker"` and `SKEPTIC_WORKER = "my-worker"` for a one-endpoint trial; this is not independent review. A separate endpoint can fill the skeptic role. An explicitly empty `SKEPTIC_WORKER = ""` is treated by autonomous preflight as an opt-out and prints a warning; do not describe that run as independently reviewed. A named missing or broken skeptic fails preflight. **A CLI-only skeptic is not supported by this connected path today.** [The settings template](fleet_settings.example.py) documents optional lab-specific fields; do not copy unrelated Mac/miner placeholders.

Tool-using assignments additionally require the trusted local service: `python tool_runtime/service.py` in a separate terminal. It generates a token under ignored `runs/tool-service/`. Its `python_run` tool executes code on this host, so ask before starting it. `python check/preflight.py` probes the configured workers **and** this service; it cannot pass until both are ready. Neither preflight nor a worker is required just to open intake.

Choose worker execution before planning: `--tool-mode chat-only` gives workers text-only generation; they cannot inspect workspace files or run their own checks. `--tool-mode tools` authorizes the configured trusted tool service for every new assignment, including repairs and follow-ons. Coding workers are instructed to inspect staged interfaces and run bounded self-checks. This can execute code on the service host; it is not a security sandbox, does not start the service, and does not authorize unrelated paths or changes to your limits.

Chat-only repairs receive the full, hash-checked prior artifact as reference text in the worker request, not merely a path they cannot open. That reference is retained when older conversation turns are trimmed. Missing, changed, non-text, or oversized carry is refused explicitly; Handoff does not silently truncate it, enable tools, or raise your server limits. The context guard uses a token estimate, not the server's tokenizer.

The choice is saved in the project package and goal. `autonomous` verifies selected workers and the named skeptic in either mode; it also requires tool-call capability on each selected coding lane and a working service when the plan uses tools. For chat-only work it explicitly reports the service as `not-needed` and does not probe it. `--skip-preflight` bypasses all these checks, not just the service; use it only for explicit diagnostics.

Existing packages without this setting retain their per-contract behavior and print a legacy-policy notice. To select a mode before their first plan, pass `--tool-mode` to `autonomous` (or `start`). Once a plan is proposed, the mode is frozen: reject an unapproved proposal before changing it; approved work needs a new explicitly authorized goal, not an in-place toggle or a budget reset. Omitting the flag on resume keeps the saved choice. Review cadence and `--delegate` never enable tools by themselves.

### 2. Open intake, then stop for the person's answers

Ask what the project should actually do; a folder name is not a specification. From the Handoff checkout, choose one of these commands (replace the paths and name):

```text
python run/conductor.py start <project-folder> --name <project-name> --package <package-folder> --project-kind local --intake-mode guided --tool-mode chat-only
python run/conductor.py start <project-folder> --name <project-name> --package <package-folder> --project-kind local --intake-mode brief --brief <existing-brief.md> --tool-mode chat-only
python run/conductor.py start <project-folder> --name <project-name> --package <package-folder> --project-kind local --intake-mode defer
```

Replace `local` with `git-existing` or `git-new` only if the person chooses that. `start` records the resolved folder and an `observed.md` file list. Guided mode prints the intake questions without making a model call. To request **one** proposed-answer call, add `--draft` to the guided or brief command after the person agrees to spend tokens; it sends the observed listing, questions, and any imported brief to Claude CLI (or `FLEET_ASSIST`). The brief is copied to `brief.md` as **unconfirmed context**, not copied into answers or scope. `defer` records only local state, makes no model call, and prints how to resume: rerun `start` with the same folder/name/package and `--intake-mode guided` or `--intake-mode brief --brief <file>`. An exit code of 2 at these human stops is expected, not a failed worker run.

Show the person the actual questions and proposed answers. Multiple-choice questions require their exact letter, not a prose sentence containing one. A draft is never an answer just because an agent likes it. The person may answer with the kickoff UI or `intake.py answer`; only after they explicitly select a draft should `python run/conductor.py confirm <package-folder> <question-id> --as <person>` be used. The agent must not confirm its own guesses under another actor name.

### 3. Generate and review the North Star

After intake is complete, run `python run/conductor.py start <project-folder> --name <project-name> --package <package-folder>` again. It uses Claude CLI (or `FLEET_SCOPE`) to write `scoped.md` and stops. Read the whole page, especially assumptions, never-rules, and the launch example. If it guesses the domain or contradicts a privacy rule, do not approve it. Once the person approves those exact bytes, **the person** runs `python run/conductor.py approve-scope <package-folder> --as <person>`.

### 4. Propose and approve the plan

Run `start` with the same folder, name, and package again. It asks the planner (Claude CLI or `FLEET_PLANNER`) for assignments and stops before dispatch. Read the proposed files, dependencies, checks, and budget; then **the person** runs the printed `python run/conductor.py approve <goal-id> --as <person>` command. If the proposal is wrong, use `python run/conductor.py reject-plan <goal-id> --reason "what is wrong" --as <person>` and rerun `start` on the same goal. Machine-rejected plans also re-plan on that goal; neither kind of rejection approves or dispatches work.

Intake question E3 selects review cadence: A = one piece, B = one batch, C = until done or stuck, D = overnight. Q0 does not shorten that choice. C and D currently have the same review-pause behavior; D does not create a cross-milestone controller. To delegate the existing scope, plan, and map gates for a bounded goal, use the alternative below. Budgets, never-rules, and human-only sign-offs still apply.

The planner is handed only the intake done-when criteria (from S2, plus the pass/fail examples). A criterion added to a goal another way that no assignment covers is reported as an `UNPLANNED_CRITERION` rather than dropped; a large backlog is not compiled into one plan, so run separable pieces as their own goals.

If you already have executable JSON contracts, pass `--plan-file <plan.json>` to `autonomous` when proposing the plan (a JSON object with an `outcomes` list). It replaces the model proposal, not the gate or approval policy; it does not replace an already-approved plan. A Markdown plan is not this format. Preserve its full backlog and explain the conversion gap rather than silently reducing it to intake answers; the import/compiler is tracked in [#21](https://github.com/wmlanglois/handoff/issues/21).

Run each concurrent session in its own checkout (a separate git worktree), and do not edit the harness while a run is in flight. `python run/conductor.py fingerprint` hashes Python files under `run/`; `python run/conductor.py fingerprint --expect <hash>` manually compares them. This excludes root runtime files such as `call.py` and is not an automatically enforced execution lock. Stable-runtime enforcement remains [#9](https://github.com/wmlanglois/handoff/issues/9).

### 5. Propose and approve the interface map

Now run `python run/conductor.py autonomous <package-folder> --decisions N --seconds N --workers my-worker`. This first invocation records a cumulative budget, derives a map from the approved plan, and stops at `AWAITING_MAP_APPROVAL` without dispatching. Read the owned paths, public calls, dependency edges, and executable launch command. If correct, **the person** runs `python run/conductor.py approve-map <goal-id> --from-proposed --as <person>`.

### 6. Dispatch only after those gates

Run the *same* `autonomous` command again. It uses the already-recorded cumulative budget; rerunning with larger numbers does not reset it. The approved map and plan now allow worker dispatch. Inspect `python run/conductor.py status <goal-id>` and the ignored `runs/` receipts. A passing packet is not a promoted project checkpoint.

The command catalog and exact flags are in [FEATURES.md](docs/FEATURES.md); `python run/conductor.py --help` shows the CLI. [The conductor guide](docs/CONDUCTOR-SKILL.md) explains the connected-project invariant. This sequence has not yet passed an independent clean-machine, end-to-end release test.

### Delegated alternative to the interactive approval steps

After confirmed intake and scope generation in steps 1–3, an explicitly authorized operator can use:

```text
python run/conductor.py autonomous <package-folder> --decisions 8 --seconds 3600 --workers my-worker --delegate <name>
```

The numbers are an example budget, not a recommendation for every project. This invocation approves the existing `scoped.md`, proposes/approves a plan, derives/approves its map, runs preflight, and can dispatch. It also handles resuming a previously proposed map. It does not generate a missing scope, answer intake, or grant unlimited authority; those remaining connections are tracked in [#13](https://github.com/wmlanglois/handoff/issues/13). Use the same package and delegation flag on resume. A stale already-approved map still requires reconciliation; delegation is not a blanket waiver of changed requirements.

Completion of this goal does not automatically start all later milestones in a supplied project backlog. That controller is tracked in [#7](https://github.com/wmlanglois/handoff/issues/7). Explain predictable stops before a walkaway run, not after the user leaves.

## Runtime configuration

The example settings file documents optional lab-specific hardware endpoints, model names, paths, and the per-user worker registry. A registered generic endpoint still needs its `PRIMARY_WORKER` and `SKEPTIC_WORKER` roles selected. Do not commit `fleet_settings.local.py`, credentials, token files, or project packages. Runs and generated state default to ignored local directories. Settings can be selected with `FLEET_SETTINGS`; see [the environment-variable list](docs/FEATURES.md#environment-variables).

The bundled `tool_runtime/` service supports file and Python tools for trusted local workers. `python_run` executes code on the host: run it only where that trust is appropriate. Its token and workspaces are generated locally, not supplied in this repository. You may also point the conductor at a separately managed compatible service.

## What completion means

Each assignment has its own check and acceptance receipt. Autonomous mode assembles only accepted, receipt-bound deliverables over a frozen project baseline and runs the approved milestone command in a fresh process. A failed journey preserves the last passing checkpoint and may queue a bounded repair. A passing packet or a green import alone does not prove a working project. The human-approved scope, map, and launch behavior remain essential.

This is an early, hardware-dependent system. Review the proposed work and run it within explicit budgets. No model, cloud account, or hardware is bundled.

## Known limitations

- This code-only publication has no public test suite or CI workflow. Basic command and syntax checks are not proof that it runs on every Python version or operating system.
- The connected planner requires Claude CLI access; workers and the optional skeptic require registered OpenAI-compatible HTTP model endpoints. A CLI-only skeptic and an interchangeable connected-project architect backend are not implemented. Their output quality and availability affect a run.
- Project launch checks establish the behavior they actually exercise, not the quality of an entire application. Scope/plan/map approval can be explicitly delegated as above; human-only sign-offs remain separate. Static tautology filtering is not proof of behavioral coverage.
- Intake-to-connection wiring, all-goal-criteria planning, Markdown-backlog compilation, and durable cross-milestone continuation remain open. See [implementation status](docs/IMPLEMENTATION-STATUS.md) for the exact issue links.
- Never-rules and prompts are not a security sandbox. In particular, the local tool service's `python_run` grants host code execution to a trusted worker.
