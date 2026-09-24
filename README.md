# Handoff

Handoff is a local-model fleet conductor for work that spans multiple assignments. It records a project's approved scope, asks a planner for connected work, dispatches to configured workers, and accepts files only with matching receipts. Packet acceptance is not project completion: autonomous mode checks an assembled candidate with a fresh-process launch before promoting a checkpoint.

This repository contains the runtime, an example configuration, and operator documentation. It does **not** contain anyone's model weights, machine addresses, credentials, project data, run history, tests, fixtures, or CI workflow. A fresh clone requires configuration before it can dispatch workers.

## Why this project exists and what has been researched

Handoff explores whether a strong conductor can turn smaller, cheaper models into a persistent team: scoped work, isolated contributions, skeptical review, evidence-bound acceptance, and integration into a continuing project. The design is experimental. Passing individual packet checks is not evidence of an autonomous, finished application, and the research is not a claim that Handoff has measured a productivity advantage.

- [Research hypothesis and limitations](docs/RESEARCH-automation-reporter-trap.md) — why activity and plausible reports are not the same as progress.
- [Field scan of agentic loops](docs/FIELD-SCAN-agentic-loops.md) — how other systems plan, bound work, verify, and stop, including gaps in that scan.
- [Prior art compared with Handoff](docs/PRIOR-ART.md) — proposed feature comparisons and missing capabilities. This is a dated research snapshot; its claims about which Handoff features are wired must be rechecked against current code before use.

The [feature and command catalog](docs/FEATURES.md) describes the current callable surface. Research, current implementation, and demonstrated end-to-end reliability are three different things.

## Start here: one project, in order

Keep Handoff in its own checkout. Your project folder is separate; `start` points to it. The generated intake package can also live outside the checkout via `--package`. A worker receives staged sources, not the whole project folder.

If an AI agent is guiding setup, it must ask the person for the project goal and model connections. It must **not** invent endpoint addresses, turn its own proposed intake drafts into confirmed user answers, run `confirm`/`intake.py answer` on the person's behalf, or invoke `approve-scope`, `approve`, `approve-map`, or `sign-off` without that person's explicit decision on the exact material. The `--as` field records a name; it does not authenticate a human. Stop and show the user the artifact at each gate. No model calls or worker dispatch should be treated as implicit permission from "read the README."

Before running setup commands, the guiding agent should ask: **What do you want to build, and which folder is the project? Where is an already-running local model API (base URL and model name), and may I send it a short qualification prompt? Should the skeptic use that same API or another registered HTTP model? Do you have an authenticated Claude CLI for the connected conductor, and may the intake/scope/planning calls use it?** If the person has only a CLI skeptic or no Claude CLI, explain the unsupported seat before proceeding; do not silently substitute a model. The intake questions and later approvals still belong to the person.

### 1. Choose the model seats before running a project

Use Python 3.11 or newer. The current connected project path uses an authenticated **Claude CLI** (`claude -p`) for intake drafts, scope writing, planning, and architect decisions. This is separate from the local worker fleet. `architect.py --engine codex` exists for a standalone ruling, but it does **not** switch the whole project path to Codex. `FLEET_ASSIST`, `FLEET_SCOPE`, and `FLEET_PLANNER` accept replacement scripts for those individual steps; they do not replace every architect call. If you do not have Claude CLI access, stop before promising an autonomous run.

Ask the user where an already-running OpenAI-compatible local model API is, including its base URL and model name. Do not search unrelated local repositories for addresses or credentials. With permission to make a short generation check, run:

```text
python run/registry.py check http://your-host:8080 --model your-model
python run/registry.py add http://your-host:8080 --name my-worker --model your-model
python run/registry.py list
```

`check` does not register; `add` qualifies by generating and writes a per-user registry outside this repo. The URL and model are examples, not defaults. Create the Git-ignored `fleet_settings.local.py` with `PRIMARY_WORKER = "my-worker"` and `SKEPTIC_WORKER = "my-worker"` for a one-endpoint trial; later, the skeptic can have its own registered endpoint. **A CLI-only skeptic is not supported by this connected path today.** Do not copy the full example file's Mac/miner placeholder topology unless you actually use that hardware. [The settings template](fleet_settings.example.py) documents optional lab-specific fields.

Tool-using assignments additionally require the trusted local service: `python tool_runtime/service.py` in a separate terminal. It generates a token under ignored `runs/tool-service/`. Its `python_run` tool executes code on this host, so ask before starting it. `python check/preflight.py` probes the configured workers **and** this service; it cannot pass until both are ready. Neither preflight nor a worker is required just to open intake.

### 2. Open intake, then stop for the person's answers

Ask what the project should actually do; a folder name is not a specification. From the Handoff checkout, run:

```text
python run/conductor.py start <project-folder> --name <project-name> --package <package-folder>
```

`start` records the folder and an `observed.md` file list. If `claude` is on `PATH` (or `FLEET_ASSIST` is set), it **automatically calls that drafter once** when no `proposed.json` exists. Ask permission first: the prompt includes the observed folder and intake questions, and may use remote tokens. Without a drafter, `start` still stops and prints self-guided `kickoff.py` and `intake.py answer` commands. An exit code of 2 at a human stop is expected, not a failed worker run.

Show the person the actual questions and proposed answers. Multiple-choice questions require their exact letter, not a prose sentence containing one. A draft is never an answer just because an agent likes it. The person may answer with the kickoff UI or `intake.py answer`; only after they explicitly select a draft should `python run/conductor.py confirm <package-folder> <question-id> --as <person>` be used. The agent must not confirm its own guesses under another actor name.

### 3. Generate and review the North Star

After intake is complete, run `python run/conductor.py start <project-folder> --name <project-name> --package <package-folder>` again. It uses Claude CLI (or `FLEET_SCOPE`) to write `scoped.md` and stops. Read the whole page, especially assumptions, never-rules, and the launch example. If it guesses the domain or contradicts a privacy rule, do not approve it. Once the person approves those exact bytes, **the person** runs `python run/conductor.py approve-scope <package-folder> --as <person>`.

### 4. Propose and approve the plan

Run `start` with the same folder, name, and package again. It asks the planner (Claude CLI or `FLEET_PLANNER`) for assignments and stops before dispatch. Read the proposed files, dependencies, checks, and budget; then **the person** runs the printed `python run/conductor.py approve <goal-id> --as <person>` command. If planning is rejected, `start` retries on the same goal ID rather than inventing a replacement.

### 5. Propose and approve the interface map

Now run `python run/conductor.py autonomous <package-folder> --decisions N --seconds N --workers my-worker`. This first invocation records a cumulative budget, derives a map from the approved plan, and stops at `AWAITING_MAP_APPROVAL` without dispatching. Read the owned paths, public calls, dependency edges, and executable launch command. If correct, **the person** runs `python run/conductor.py approve-map <goal-id> --from-proposed --as <person>`.

### 6. Dispatch only after those gates

Run the *same* `autonomous` command again. It uses the already-recorded cumulative budget; rerunning with larger numbers does not reset it. The approved map and plan now allow worker dispatch. Inspect `python run/conductor.py status <goal-id>` and the ignored `runs/` receipts. A passing packet is not a promoted project checkpoint.

The command catalog and exact flags are in [FEATURES.md](docs/FEATURES.md); `python run/conductor.py --help` shows the CLI. [The conductor guide](docs/CONDUCTOR-SKILL.md) explains the connected-project invariant. This sequence has not yet passed an independent clean-machine, end-to-end release test.

## Runtime configuration

The example settings file documents optional lab-specific hardware endpoints, model names, paths, and the per-user worker registry. A registered generic endpoint still needs its `PRIMARY_WORKER` and `SKEPTIC_WORKER` roles selected. Do not commit `fleet_settings.local.py`, credentials, token files, or project packages. Runs and generated state default to ignored local directories. Settings can be selected with `FLEET_SETTINGS`; see [the environment-variable list](docs/FEATURES.md#environment-variables).

The bundled `tool_runtime/` service supports file and Python tools for trusted local workers. `python_run` executes code on the host: run it only where that trust is appropriate. Its token and workspaces are generated locally, not supplied in this repository. You may also point the conductor at a separately managed compatible service.

## What completion means

Each assignment has its own check and acceptance receipt. Autonomous mode assembles only accepted, receipt-bound deliverables over a frozen project baseline and runs the approved milestone command in a fresh process. A failed journey preserves the last passing checkpoint and may queue a bounded repair. A passing packet or a green import alone does not prove a working project. The human-approved scope, map, and launch behavior remain essential.

This is an early, hardware-dependent system. Review the proposed work and run it within explicit budgets. No model, cloud account, or hardware is bundled.

## Known limitations

- This code-only publication has no public test suite or CI workflow. Basic command and syntax checks are not proof that it runs on every Python version or operating system.
- The connected planner requires Claude CLI access; workers and the optional skeptic require registered OpenAI-compatible HTTP model endpoints. A CLI-only skeptic and an interchangeable connected-project architect backend are not implemented. Their output quality and availability affect a run.
- Project launch checks establish the behavior they actually exercise, not the quality of an entire application. Scope, plan, interface map, and consequential sign-offs require human review.
- Never-rules and prompts are not a security sandbox. In particular, the local tool service's `python_run` grants host code execution to a trusted worker.
