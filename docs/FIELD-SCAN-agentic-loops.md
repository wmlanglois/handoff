# Field scan: what agentic loops actually do, September 2026

Source: four scraping lanes over vendor docs, agent source code, and two community bug threads.
Every claim below carries the URL it came from. Claims a lane reported without a URL are dropped.

**Coverage gaps, stated up front so nothing here is read as complete:**

- Lane 3 (open-source agent source code: SWE-agent, OpenHands, aider, smolagents, goose, Roo,
  crewAI, LangGraph, AutoGen) was **truncated mid-sentence** in the middle of its SWE-agent
  reviewer entry. Its findings on aider, smolagents, goose, Roo Code, Kilo, crewAI, the OpenAI
  Agents SDK, LangGraph, AutoGen, Magentic-One, the self-correction papers (arXiv 2310.01798,
  2407.01489, 2604.14820) and Terminal-Bench **never arrived**. Their URLs are listed as fetched;
  the content is not here.
- Lane 4 never reported at all.
- Windsurf/Cascade, Augment, Continue, Lovable, Zencoder, v0 and Bolt were **not reachable** (DNS
  failures from the scraping environment) and are absent. Circulating claims about Windsurf rule
  size caps came from search snippets only and are dropped.
- OpenHands loop internals (max_iterations, stuck detection, the condenser) and the OpenHands
  critic-32b numbers are **not** in this document. The model card URL was fetched
  (https://huggingface.co/OpenHands/openhands-critic-32b-exp-20250417/blob/main/README.md) but the
  lane was cut before reporting the numbers. This is the single most relevant missing evidence for
  our skeptic role and is worth a re-fetch.
- Terminal-Bench leaderboard rows are client-rendered and were not captured
  (https://www.tbench.ai/leaderboard).

---

## 1. THE CONSENSUS LOOP

The honest summary first: **the field ships a hard numeric cap plus human review.** Almost every
shipped product's terminal state is "a person looks at a PR." The systems that terminate without a
human do it by anchoring on an external machine signal (CI green, tests exit 0, a convergence
report file), not on the agent's own opinion. Nobody documents "the model decides it is done" as
the primary criterion.

What nearly every working system does, in order:

1. **Plan in a non-mutating phase.** The agent reads, searches, asks clarifying questions, and
   writes a plan. It cannot edit yet. Cursor Plan Mode
   (https://cursor.com/docs/agent/planning), Cline Plan/Act
   (https://github.com/cline/cline), Factory Spec Mode
   (https://docs.factory.ai/cli/user-guides/specification-mode), Replit Plan mode
   (https://docs.replit.com/replitai/agent), Claude Code explore→plan→code→commit
   (https://code.claude.com/docs/en/best-practices), Copilot's research-then-plan
   (https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-coding-agent), Devin's
   Ask-Devin-then-session (https://docs.devin.ai/essential-guidelines).
2. **The plan becomes a durable, human-editable file, not hidden reasoning.** Cursor saves plans as
   markdown you can move into the workspace (https://cursor.com/docs/agent/planning); Spec Kit
   writes spec/plan/tasks artifacts under `.specify/` (https://github.com/github/spec-kit); Devin
   playbooks are versioned documents (https://docs.devin.ai/product-guides/creating-playbooks).
3. **An explicit gate to execution** — a human click or a mode switch. Nobody ships a serious agent
   that starts editing immediately.
4. **The worker runs in an isolated context** with its own tool policy: its own VM, worktree, or
   subagent context window. Cursor cloud agents (https://cursor.com/docs/cloud-agent), Devin's
   managed Devins (https://cognition.com/blog/devin-can-now-manage-devins), Claude Code subagents
   (https://code.claude.com/docs/en/sub-agents), Amp subagents
   (https://ampcode.com/docs/models-and-subagents).
5. **The worker must be able to run the check.** Cursor states the precondition bluntly: an agent
   that can write code but cannot run tests or reach APIs cannot close the loop on its work
   (https://cursor.com/docs/cloud-agent).
6. **Completion is paid for in artifacts, not prose.** Cursor cloud agents attach screenshots,
   videos and logs to the PR (https://cursor.com/docs/cloud-agent); Claude Code tells you to make
   the agent show the command and what it returned rather than assert success
   (https://code.claude.com/docs/en/best-practices).
7. **A dumb counter can overrule the model.** Copilot's 59-minute hard session cap
   (https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-coding-agent); Claude
   Code overriding a blocking Stop hook after 8 consecutive blocks and capping idle check-ins at 3
   per goal (https://code.claude.com/docs/en/best-practices, https://code.claude.com/docs/en/goal);
   Cursor's per-hook `loop_limit` (https://cursor.com/docs/agent/hooks); Claude Code subagent
   `maxTurns` (https://code.claude.com/docs/en/sub-agents); mini-SWE-agent's default $3 cost ceiling
   (https://raw.githubusercontent.com/SWE-agent/mini-swe-agent/main/src/minisweagent/agents/default.py).
8. **Someone other than the worker decides done** — either an external signal or a fresh-context
   judge. See §2 and §3.
9. **A human reviews the final diff.** Mandatory in Copilot's docs
   (https://docs.github.com/en/copilot/responsible-use/agents), the acceptance gate in Cursor cloud
   agents and Codex cloud (https://learn.chatgpt.com/docs/cloud).
10. **Cheap rollback throughout**, treated as a prerequisite for autonomy rather than a nicety:
    checkpoints in Cursor, Cline (https://github.com/cline/cline) and Replit
    (https://docs.replit.com/replitai/agent), or a throwaway branch in an ephemeral VM.

A second, weaker consensus: **split work into more, shorter sessions rather than one long run.**
Devin's three-human-hour scoping rule (https://docs.devin.ai/essential-guidelines), Copilot's
"break it into smaller focused tasks", Factory's 1–500-feature Mission ceiling
(https://docs.factory.ai/docs/missions/overview), Amp's one-thread-per-task plus Handoff
(https://ampcode.com/docs/threads).

---

## 2. STOPPING CONDITIONS

| Product | How it stops | What verifies the work |
|---|---|---|
| Claude Code `/goal` (https://code.claude.com/docs/en/goal) | Separate small fast model judges after **every** turn: Met / Not yet met / **Impossible**. Also stops on 4 unrecoverable errors, and aborts if the model produces several turns with no tool use. Condition capped at 4,000 chars; docs tell you to write a turn bound into the condition text. | The evaluator reads only the transcript — it cannot run commands or read files — so the worker must surface evidence itself. |
| Claude Code Stop hook (https://code.claude.com/docs/en/best-practices) | Deterministic script blocks the turn from ending until it passes; **overridden after 8 consecutive blocks**. | Your script's exit status. |
| Cursor Agent (https://cursor.com/docs/agent/overview) | Not documented. Explicitly **no limit on tool calls**. Human checkpoints are the practical stop. | Agent-discretionary browser testing. |
| Cursor cloud agents (https://cursor.com/docs/cloud-agent) | Terminates by producing a merge-ready PR. No documented runtime or idle timeout. | Screenshots, videos, logs attached to the PR; human reviews. |
| Cursor Subscriptions / `/goal` (https://cursor.com/changelog) | `/goal` is goal-terminated; Subscriptions are event-terminated and never end. | CI green plus resolved bot comments. |
| Cursor hooks (https://cursor.com/docs/agent/hooks) | `stop` event with a per-hook `loop_limit`; exit 2 blocks; fails **open** unless `failClosed`. | Permission hooks return allow/deny/ask; deny beats ask beats allow. |
| Cursor Projects coordinator (https://cursor.com/blog/projects) | **Not documented.** The most ambitious published orchestrator does not say how its coordinator decides the work is done. | PRs; subscriptions driven by external events. |
| Devin sessions (https://docs.devin.ai/essential-guidelines) | Postconditions in the playbook's Specifications section; auto-sleep after 30 min idle (5–120 configurable). | Tests / CI. Docs say tasks with test suites or verifiable outcomes give the best results. |
| Devin autofix (https://cognition.com/blog/closing-the-agent-loop-devin-autofixes-review-comments) | CI clean and bot comments resolved, then a human sees it. | Every linter, CI job and scanner treated uniformly as a comment source. |
| Devin Dynamic Workflows (https://docs.devin.ai/work-with-devin/dynamic-workflows.md) | **A deterministic Python script decides**, not a model. Agents memoized on hash(prompt + output schema); resumable 7 days. | A JSON Schema per stage — a stage cannot pass garbage downstream and typecheck. |
| GitHub Copilot coding agent (https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-coding-agent) | **59-minute hard cap**, cannot be extended. One PR, one repo per task. | CodeQL, secret scanning, dependency analysis; mandatory human review. |
| Codex cloud (https://learn.chatgpt.com/docs/cloud) | Human inspects summary + diff. No documented autonomous criterion. | Review-based. |
| Codex `/review` (https://learn.chatgpt.com/codex/code-review) | Bounded by the diff. Does **not** execute code. | Static analysis, read-only, cannot touch the working tree. |
| Factory Missions (https://docs.factory.ai/docs/missions/overview) | All milestones complete with validated results; sweet spot ~1–500 features. | Mission Control runs user-facing QA against the running app — requires the repo to have automated testing. |
| Amp (https://ampcode.com/docs/models-and-subagents) | Not documented. Handoff to a fresh thread is the long-run mechanism. | Oracle: read-only reviewer at higher reasoning effort, invoked by name. |
| Cline (https://github.com/cline/cline) | Human approval per edit/command, or auto-approve with **no documented cap**. | The approval gate itself. |
| Replit Agent (https://docs.replit.com/replitai/agent) | Task-list bounded, conversationally redirected; rollback to any checkpoint. | Claims continuous self-testing while building. |
| Spec Kit (https://github.com/github/spec-kit) | "Repeat implement → converge until convergence reports **Converged**." A terminal literal in a file you can grep. | `/speckit-converge` emits a convergence report checking implementation against spec. |
| mini-SWE-agent (https://raw.githubusercontent.com/SWE-agent/mini-swe-agent/main/src/minisweagent/agents/default.py) | `while True`; breaks only on a message with role `exit`. Defaults: step_limit 0 (off), **cost_limit $3**, wall_time 0 (off), max_consecutive_format_errors 3. Out of the box the only hard stop is money. | Nothing internal. External SWE-bench harness runs the tests on the patch. |
| SWE-agent (https://raw.githubusercontent.com/SWE-agent/SWE-agent/main/sweagent/agent/agents.py) | Submission marker, explicit `exit`, an **EXIT_FORFEIT token** so the agent can give up honestly, cost limits (which trigger **autosubmission** of best-so-far), context exhaustion, consecutive command timeouts, total execution timeout. **No step-count limit.** token_budget 200000; max_requeries 3. | Pre-submit checklist in the prompt; optionally the RetryAgent reviewer. |
| SWE-agent RetryAgent (https://raw.githubusercontent.com/SWE-agent/SWE-agent/main/sweagent/agent/reviewer.py) | Continues only while cost < limit AND attempts < max AND accepted < max_accepts (default 1) AND **remaining budget > min_budget_for_new_attempt** — do not start an attempt you cannot afford to finish. | A reviewer model scores each submission, sampled n=5 and **averaged**, over a *filtered* trajectory. Ties broken by fewest API calls. |
| Anthropic multi-agent research (https://www.anthropic.com/engineering/multi-agent-research-system) | Effort budgeted **up front by task class** and hard-coded into the lead agent's prompt: simple fact-finding = 1 agent, 3–10 tool calls; comparisons = 2–4 subagents, 10–15 calls each; complex = 10+ subagents. | LLM-as-judge on factual accuracy, citation accuracy, completeness, source quality, plus human testing. |

**Two direct contradictions worth naming:**

- **Does a resumed run inherit its spent budget?** Claude Code `/goal` explicitly **resets** turn
  count, timer and token-spend baseline on resume (https://code.claude.com/docs/en/goal). The
  Cursor community field note persists budget as part of state precisely so it **cannot** reset
  (https://forum.cursor.com/t/field-note-making-long-background-agent-runs-resumable-without-re-doing-work-already-done/166667).
  Incompatible defaults; matters a lot for an unattended fleet that restarts often.
- **Turn cap vs cost cap.** SWE-agent and mini-SWE-agent have **no step limit** and bound on money
  and wall clock. Claude Code subagents bound on `maxTurns`. Copilot bounds on wall clock only.

---

## 3. ROLE SPLITS, AND WHETHER THE CRITIC EARNS ITS COST

**Who runs a real planner/worker/critic split:**

- **Claude Code**: writer/reviewer as two sessions, the reviewer in a fresh context seeing only the
  diff and the criteria; `/goal` adds a per-turn judge model; shipped `code-reviewer` subagent is
  read-only (Read/Glob/Grep) (https://code.claude.com/docs/en/best-practices,
  https://code.claude.com/docs/en/sub-agents).
- **SWE-agent RetryAgent**: worker + reviewer/scorer + chooser/preselector, each with its own model
  config (https://raw.githubusercontent.com/SWE-agent/SWE-agent/main/sweagent/agent/reviewer.py).
- **Cursor**: `/verifier` is the canonical subagent example; `readonly` is a frontmatter field
  (https://cursor.com/docs/subagents). Projects splits coordinator (never writes code) from
  implementers (https://cursor.com/blog/projects).
- **Devin**: coordinator + managed Devins, each with its own shell and test runner; Devin Review is
  a separate pass (https://cognition.com/blog/devin-can-now-manage-devins).
- **Amp**: main agent + read-only Oracle at higher reasoning effort
  (https://ampcode.com/docs/models-and-subagents).
- **Factory**: Code Reviewer droid pinned to the read-only tool category in frontmatter
  (https://docs.factory.ai/docs/harness/subagents).
- **Spec Kit**: roles are commands producing files, with `/speckit-converge` as the critic
  (https://github.com/github/spec-kit).

**Universal detail: the reviewer is read-only, enforced structurally, not by instruction.** Codex
enforces it with a sandbox mode (https://learn.chatgpt.com/codex/sandboxing), Factory with a tool
policy category, Claude Code and Cursor with a tools allowlist in frontmatter.

**Evidence FOR the critic:**

- Anthropic: Opus 4 lead with Sonnet 4 subagents beat single-agent Opus 4 by **90.2%** on their
  internal research eval (https://www.anthropic.com/engineering/multi-agent-research-system). This
  is the only published head-to-head win in the scan — and it is a **research** task, not code.
- Claude Code's stated rationale: a fresh context is not biased toward code it just wrote
  (https://code.claude.com/docs/en/best-practices).
- SWE-agent's reviewer is elaborate enough (n_sample=5, averaged, filtered trajectory) to imply it
  was worth building, but **the file fetched publishes no ablation**
  (https://raw.githubusercontent.com/SWE-agent/SWE-agent/main/sweagent/agent/reviewer.py).

**Evidence AGAINST:**

- Cognition argues for single-threaded linear agents: share full traces, not messages; parallel
  subagents that cannot see each other's traces build incompatible pieces
  (https://cognition.com/blog/dont-build-multi-agents). The same post approves of Claude Code's
  subagents *specifically because they only answer questions and never write code in parallel*.
- GitHub concedes the review pass **hallucinates** — flagging problems that do not exist or that
  rest on misunderstanding the code (https://docs.github.com/en/copilot/responsible-use/agents).
- Claude Code concedes **reviewer bias**: a reviewer prompted to find gaps will report some even
  when the work is sound, and chasing every finding produces extra abstraction, defensive code and
  tests for impossible cases (https://code.claude.com/docs/en/best-practices).
- A Cursor forum bug report describes agents looping forever at the **review step**, repeating
  identical messages, never recovering without a human stop — correlated with Auto model selection
  and Gemini 2.5 Flash, i.e. a small fast model in the judging seat
  (https://forum.cursor.com/t/cursor-agent-enters-endless-loop-without-progressing-past-review-step/126815).
  Community report, not a vendor admission.
- Cursor names the anti-pattern: do not create dozens of generic subagents; start with **2–3**
  focused ones (https://cursor.com/docs/subagents).
- Cognition's autofix loop increased internal token usage and they publish **no metrics**, only the
  qualitative claim of cleaner PRs
  (https://cognition.com/blog/closing-the-agent-loop-devin-autofixes-review-comments).

**Honest verdict: nobody has published evidence that an LLM critic pays for itself on code tasks.**
The one published win is on read-heavy research. The costs — hallucinated findings, reviewer bias
toward over-engineering, and a documented forever-loop when a weak model sits in the judge seat —
are all vendor-admitted or field-observed. Cognition's own reconciliation is instructive: they
shipped parallel workers anyway, but only because each worker has its own shell and test runner so
it can verify itself, and the coordinator owns all conflict resolution
(https://cognition.com/blog/devin-can-now-manage-devins).

---

## 4. CONFIG AND PROMPT FILE CONVENTIONS

**The shape has converged: markdown body + YAML frontmatter, one file per role, discovered by
walking up to the git root, with nested per-directory overrides and a hard size cap.**

Role files:

- `.claude/agents/*.md` — frontmatter fields: `name`, `description` (drives automatic delegation),
  `model` (sonnet|opus|haiku|fable|full id|inherit), `tools` allowlist / `disallowedTools`,
  **`maxTurns`**, `permissionMode`, `effort`, `skills`, `mcpServers`, per-agent `hooks`, `memory`
  scope, `background`, `omitClaudeMd`, **`isolation: worktree`**. Five-level precedence
  (https://code.claude.com/docs/en/sub-agents).
- `.cursor/agents/*.md` — `name`, `description`, `model`, **`readonly`**, `is_background`
  (https://cursor.com/docs/subagents).
- `.github/agents/*.agent.md` — `description`, `name`, `tools`, **`model` as a prioritized array**,
  `agents`, **`handoffs`** (declares legal transitions between roles)
  (https://code.visualstudio.com/docs/copilot/customization/custom-chat-modes). It also reads
  `~/.claude/agents` — de facto convergence on that layout.
- `.factory/droids/*.md` — `name`, `description`, `model|inherit`, `tools` as a **policy category**
  (read-only / edit / execute / web / custom), `reasoningEffort`, `mcpServers`, body as system
  prompt (https://docs.factory.ai/docs/harness/subagents).

Instruction/rules files:

- `AGENTS.md` is the shared substrate, read natively by Claude Code
  (https://code.claude.com/docs/en/memory), Cursor (https://cursor.com/docs/context/rules), Copilot
  (https://docs.github.com/en/copilot/how-tos/configure-custom-instructions/add-repository-instructions),
  VS Code and Cline. Its de facto section set is three headings: dev environment tips, testing
  instructions, PR instructions (https://github.com/openai/agents.md). Adoption figures circulating
  in secondary writeups were snippet-only and are dropped.
- Codex adds a three-tier precedence chain with an **`AGENTS.override.md`** beating `AGENTS.md` at
  the same level, concatenated root-downward, capped at **32 KiB** combined
  (https://learn.chatgpt.com/codex/agent-configuration/agents-md).
- Cursor `.cursor/rules/*.mdc` — exactly three frontmatter fields: `description`, `globs`,
  `alwaysApply`, giving four routing modes (Always / Apply Intelligently / glob-matched /
  manual-only). Plain `.md` in that directory is **silently ignored** — `.mdc` required
  (https://cursor.com/docs/context/rules).
- Claude Code `.claude/rules/*.md` with a `paths:` glob loads only when matching files are touched;
  `@path` imports max depth 4; auto-memory loads only the first 200 lines or 25KB
  (https://code.claude.com/docs/en/memory).
- Copilot `.github/instructions/NAME.instructions.md` with `applyTo` globs, plus **`excludeAgent`**
  — a per-file switch naming which *role* may see this instruction (`code-review` or `cloud-agent`)
  (https://docs.github.com/en/copilot/how-tos/configure-custom-instructions/add-repository-instructions).
  No other tool in the scan has this.
- Devin playbooks are the best published prompt template: Overview / **Procedure** (one imperative
  step per line, MECE, covering setup + execution + delivery) / **Specifications** (postconditions)
  / Advice and Pointers / **Forbidden Actions** / What's Needed From User
  (https://docs.devin.ai/product-guides/creating-playbooks).

**Prompts vs configuration — the cleanest statement in the field:** Claude Code's memory docs say
CLAUDE.md is *context, not enforced configuration*, and that a PreToolUse hook is what you use if
you need to actually block something (https://code.claude.com/docs/en/memory). Cursor draws the
same line with exit-code-2-blocks and a permission enum (https://cursor.com/docs/agent/hooks).
Factory separates DESIGN.md / SKILL.md / AGENTS.md / `.factory/settings.json` so prompt content and
machine policy never share a file (https://docs.factory.ai/cli/configuration/agents-md).

**Size caps disagree and none is measured:** Claude Code targets under 200 lines
(https://code.claude.com/docs/en/memory); Cursor says under 500 lines
(https://cursor.com/docs/context/rules); Copilot says no more than 2 pages
(https://docs.github.com/en/copilot/how-tos/configure-custom-instructions/add-repository-instructions);
Factory caps 80,000 chars initial plus 40,000 dynamic (https://docs.factory.ai/cli/configuration/agents-md);
Codex 32 KiB (https://learn.chatgpt.com/codex/agent-configuration/agents-md). Every one of them
gives the same reason — adherence drops — and none publishes a study. Treat as folklore; pick the
smallest, since local models have less room.

**One structural warning:** VS Code concedes that when multiple instruction files apply it combines
them with **no guaranteed order**
(https://code.visualstudio.com/docs/copilot/customization/custom-instructions). Any prompt file
that depends on "this rule comes after that one" is broken by construction. Each file must be
self-contained and non-contradictory.

**Negative finding worth stating:** the largest community rules corpus, awesome-cursorrules (40.8k
stars), is organized by *technology*, not role — 40+ frontend files against exactly **one**
security file — and contains essentially nothing about how an agent should decide it is done
(https://github.com/PatrickJS/awesome-cursorrules). Do not expect to find a skeptic or architect
prompt there.

---

## 5. SUGGESTED MODEL PER ROLE, AS THE FIELD RECOMMENDS IT

| Role | What the field ships | Source |
|---|---|---|
| Coordinator / architect | Frontier model, and **structurally barred from writing code** so it stays responsive | https://cursor.com/blog/projects |
| Lead / orchestrator | Opus 4 lead over Sonnet 4 subagents | https://www.anthropic.com/engineering/multi-agent-research-system |
| Planner | Frontier model with read-only-ish tools; the shipped example is a prioritized array `['Claude Opus 4.5', 'GPT-5.2']` with tools `web/fetch`, `search/codebase` and no edit | https://code.visualstudio.com/docs/copilot/customization/custom-chat-modes |
| Worker / debugger | `model: sonnet`, full tools (Read, Grep, Glob, Bash, Edit) | https://code.claude.com/docs/en/sub-agents |
| Code reviewer | `model: sonnet`, tools Read/Glob/Grep only | https://code.claude.com/docs/en/sub-agents |
| Security reviewer | `model: opus`, Read/Grep/Glob/Bash | https://code.claude.com/docs/en/best-practices |
| Done-ness evaluator | **Haiku by default**, a small fast model, judging only the transcript | https://code.claude.com/docs/en/goal |
| Hard-reasoning reviewer | Amp's Oracle: read-only, **higher reasoning effort than the main agent**; the announcement says it was powered by o3 | https://ampcode.com/news/oracle |
| Explore / search | Deliberately a **faster** model | https://cursor.com/docs/subagents |
| Per-request routing | Cursor Router routes by complexity with Cost/Balance/Intelligence dials and **requires a cheap fallback model to function at all** — but publishes no accuracy or latency figures and does not disclose its signals | https://cursor.com/docs/cursor-router |
| Plan vs Act modes | **No per-mode model recommendation published** | https://github.com/cline/cline |

The pattern, such as it is: **frontier for decisions, cheap for labour and for search, cheap for
the done-ness verdict, expensive only where reasoning depth is the product.** The contested point
is the judge seat — Anthropic ships Haiku there and calls the cost negligible
(https://code.claude.com/docs/en/goal), while the Cursor forum's review-step forever-loop is
correlated with a small fast model in that exact seat
(https://forum.cursor.com/t/cursor-agent-enters-endless-loop-without-progressing-past-review-step/126815).
The difference is plausibly that Claude Code's evaluator returns a three-value enum and cannot act,
while Cursor's review step is free-form.

VS Code's `model` as a **prioritized array** is the field's only published answer to a model being
unavailable mid-run (https://code.visualstudio.com/docs/copilot/customization/custom-chat-modes).

---

## 6. ADMITTED FAILURE MODES

Vendor concessions, each with its URL:

- **"Looks done" is the only signal available without a runnable check.** Claude Code states it
  outright: without a check it can run, the human becomes the verification loop, and every mistake
  waits for a person to notice (https://code.claude.com/docs/en/best-practices).
- **Context anxiety.** Cognition found Sonnet 4.5 monitors its own remaining context and takes
  shortcuts or abandons the task when it thinks it is low, even with substantial capacity left.
  Their fix was to expose a 1M window while capping actual usage at 200k — deliberately misleading
  the model about its headroom
  (https://cognition.com/blog/devin-sonnet-4-5-lessons-and-challenges).
- **Agent-written summaries are not adequate handoffs.** Same post: the model writes CHANGELOG.md
  and SUMMARY.md unprompted, but the summaries were not comprehensive because it did not know what
  it did not know; their own memory systems outperformed them.
- **Reviewer bias and over-engineering.** A reviewer asked to find gaps will find some even when
  the work is sound (https://code.claude.com/docs/en/best-practices).
- **The review pass hallucinates**, flagging problems that do not exist; generated code may look
  valid but be semantically wrong; human review remains mandatory
  (https://docs.github.com/en/copilot/responsible-use/agents).
- **Runaway fan-out.** Anthropic's early versions spawned 50 subagents for a simple query, scoured
  the web endlessly for sources that did not exist, and distracted each other with excessive
  updates; subagents without detailed task descriptions duplicate work and leave gaps. Multi-agent
  costs ~**15x** the tokens of chat, agents ~4x
  (https://www.anthropic.com/engineering/multi-agent-research-system).
- **Single-session reliability degrades with length.** Devin's own guidance: tasks over roughly
  three human hours should not be given to one session at all
  (https://docs.devin.ai/essential-guidelines).
- **The four failure classes Devin instruments for**: build failures, environment configuration
  problems, **incorrect assumptions about the codebase**, and **scope ambiguity**
  (https://docs.devin.ai/product-guides/session-insights.md).
- **Listed validation commands are not run.** Codex does not execute the commands in AGENTS.md;
  they are guidance only (https://learn.chatgpt.com/codex/agent-configuration/agents-md).
- **A deterministic gate can itself hang**, so Claude Code overrides a blocking Stop hook after 8
  consecutive blocks (https://code.claude.com/docs/en/best-practices), and Cursor hooks fail
  **open** by default (https://cursor.com/docs/agent/hooks).
- **Compaction thrashing**: if one file or tool output refills context immediately after each
  summary, Claude Code stops auto-compacting after a few attempts and errors rather than looping
  (https://code.claude.com/docs/en/best-practices).
- **Correcting over and over**: after **two** failed corrections on the same issue, `/clear` and
  rewrite the prompt rather than continue (https://code.claude.com/docs/en/best-practices).
- **Amp's subagents cannot communicate with each other, cannot be guided mid-task, start without
  accumulated context, and the main agent sees only their final summary**
  (https://ampcode.com/docs/models-and-subagents).
- **Hard caps admitted as caps**: Copilot's 59 minutes cannot be extended or bypassed; one PR, one
  repo per task (https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-coding-agent).
- **Cursor**: long-running is not yet available for multi-repo environments; hooks do not execute
  during early read-only turns; no maximum runtime or idle timeout is published
  (https://cursor.com/docs/background-agent, https://cursor.com/docs/cloud-agent). No context window
  sizes are published on the models page, and the Router publishes no accuracy numbers
  (https://cursor.com/docs/cursor-router).
- **Silent config failure**: plain `.md` files in `.cursor/rules` are ignored entirely
  (https://cursor.com/docs/context/rules).
- **Community-reported, not vendor-admitted**: agents entering an endless loop at the review step
  and never recovering without a human stop
  (https://forum.cursor.com/t/cursor-agent-enters-endless-loop-without-progressing-past-review-step/126815).
- **Timestamps and randomness inside a cached step make resumed runs diverge** from the original
  decisions; inject them externally so replay stays deterministic
  (https://forum.cursor.com/t/field-note-making-long-background-agent-runs-resumable-without-re-doing-work-already-done/166667).

---

## 7. WHAT THIS MEANS FOR US

Context: single operator, frontier architect in the CLI session, local MLX workers, `loop_config.py`
already carrying `max_rounds=5`, `stagnation_k=3`, `investigation_budget=6`, `require_evidence`, and
a `MUST_DIFFER` constraint between worker and skeptic.

### Adopt

1. **Three verdicts from the skeptic, not two — Met / Not yet / Impossible.** An explicit IMPOSSIBLE
   verdict is what ends a forever-loop honestly (https://code.claude.com/docs/en/goal). Pair it with
   SWE-agent's forfeit token so the *worker* can also declare a task impossible without faking
   success (https://raw.githubusercontent.com/SWE-agent/SWE-agent/main/sweagent/agent/reviewer.py).
2. **Termination as a distinguished message type, never inferred from prose.** mini-SWE-agent breaks
   only on a message with role `exit`
   (https://raw.githubusercontent.com/SWE-agent/mini-swe-agent/main/src/minisweagent/agents/default.py).
   Narration can then never satisfy the exit condition. This is the cheapest anti-reporter device in
   the whole scan and we do not have it.
3. **Add a cost/wall-clock ceiling alongside `max_rounds`.** The field bounds on money and time, not
   just turns: $3 default in mini-SWE-agent, 200k token_budget in SWE-agent, 59 minutes in Copilot.
   Turn counts reward padding.
4. **Add the "do not start an attempt you cannot afford to finish" check** —
   `min_budget_for_new_attempt` (SWE-agent reviewer.py). One line, prevents the worst waste.
5. **Autosubmit best-so-far on budget exhaustion** (SWE-agent agents.py). Running out of money must
   produce an artifact, not nothing.
6. **Non-progress detectors as dumb counters, separate from the LLM's judgment**: a no-tool-use
   stall detector (https://code.claude.com/docs/en/goal), a consecutive-format-error counter
   (mini-SWE-agent, max 3), and a repeated-turn hash abort — the Cursor forum loop was literally the
   same message repeating
   (https://forum.cursor.com/t/cursor-agent-enters-endless-loop-without-progressing-past-review-step/126815).
   All three are cheaper than an LLM evaluator.
7. **Give the skeptic a filtered artifact, not the worker's trace**, and sample it more than once.
   SWE-agent's reviewer sees a filtered trajectory and is sampled n=5 and averaged
   (https://raw.githubusercontent.com/SWE-agent/SWE-agent/main/sweagent/agent/reviewer.py). Our
   config already says the skeptic must not read the rationale — the field agrees, and adds
   averaging over samples because a single local-model verdict is noisy.
8. **Constrain the skeptic's output to a small enum plus a line reference, never free text.** GitHub
   admits reviewers hallucinate findings (https://docs.github.com/en/copilot/responsible-use/agents)
   and Claude Code admits they over-report (https://code.claude.com/docs/en/best-practices). A
   malformed or repeated verdict should be an abort condition, not a retry.
9. **Tell the skeptic to flag only gaps affecting correctness or the stated requirements**, treating
   the rest as optional — verbatim mitigation from
   https://code.claude.com/docs/en/best-practices.
10. **Enforce read-only on the skeptic at the tool layer, not in the prompt.** Codex sandbox mode,
    Factory tool categories, Cursor `readonly`, Claude Code `tools` allowlist — four independent
    implementations all enforce it structurally.
11. **The architect must not do worker labour.** `loop_config.py` already says this; Cursor Projects
    is the published confirmation and gives the reason — the coordinator stays responsive and never
    blocked (https://cursor.com/blog/projects).
12. **Persist the budget across resume, and inject time/randomness externally.** Pick the Cursor
    field-note side of the disagreement, not the `/goal` side: an unattended fleet that restarts
    often must not get a fresh allowance each time
    (https://forum.cursor.com/t/field-note-making-long-background-agent-runs-resumable-without-re-doing-work-already-done/166667).
    Content-address each step on hash(step_id, resolved_inputs, code_version) so a worker dying
    mid-run is cheap — the MLX cluster's known failure mode makes this load-bearing.
13. **A deterministic Python driver with agents as typed, memoized functions.** Devin's Dynamic
    Workflows is the closest published architecture to what we are building, and a JSON Schema on
    every worker return is itself an anti-reporter device: a worker that produced nothing cannot
    fill in the schema (https://docs.devin.ai/work-with-devin/dynamic-workflows.md).
14. **Prompt-file structure: copy Devin's playbook sections** — Overview / Procedure / Specifications
    (postconditions) / Advice / **Forbidden Actions** / What's Needed From User
    (https://docs.devin.ai/product-guides/creating-playbooks). Forbidden Actions and Specifications
    are the two most harnesses omit and both are load-bearing. `docs/PROMPTS.md` should carry these
    headings per role.
15. **Per-role prompt visibility.** Copilot's `excludeAgent` is the only published mechanism for
    keeping the worker's implementation guidance out of the reviewer's context
    (https://docs.github.com/en/copilot/how-tos/configure-custom-instructions/add-repository-instructions).
    Add a role field to each prompt block.
16. **A prioritized model array per role, not a single name**
    (https://code.visualstudio.com/docs/copilot/customization/custom-chat-modes). `ROLE_WORKER`
    currently maps one role to one worker; given that a dead rank silently hangs the cluster, a
    declared fallback is not optional.
17. **`handoffs` — declare the legal transitions between architect, worker and skeptic** so an
    undeclared transition is a detectable error rather than an implicit free-for-all (same URL).
18. **Every artifact stamped with the run id**, the way Amp puts an `Amp-Thread-ID` trailer on
    commits (https://ampcode.com/docs/threads).
19. **Instrument the four Devin failure classes per run** — build failure, environment config,
    incorrect assumption about the codebase, scope ambiguity — plus an obstacle/recovery timeline
    (https://docs.devin.ai/product-guides/session-insights.md). The last two are exactly the ones
    that produce a reporter.
20. **Scope gate before dispatch**: if the architect cannot state a sub-task as a ≤3-human-hour unit
    with checkable postconditions, it decomposes further (https://docs.devin.ai/essential-guidelines).

### Drop

1. **Drop any expectation that the skeptic improves output quality, until we measure it.** No
   vendor publishes evidence that an LLM critic pays for itself on code. The one published win is
   90.2% on *research* (https://www.anthropic.com/engineering/multi-agent-research-system), and
   Cognition argues the opposite for code generation
   (https://cognition.com/blog/dont-build-multi-agents). Keep the skeptic as a *jury* — the role
   `loop_config.py` already assigns it — and let real checks decide acceptance.
2. **Drop free-text skeptic output.** See adopt #8.
3. **Drop the idea of a fourth role.** Cursor names the anti-pattern explicitly: 2–3 focused
   subagents, not a zoo (https://cursor.com/docs/subagents).
4. **Drop reliance on the worker's self-written summary as the handoff artifact.** The harness owns
   the memory format and extracts it (https://cognition.com/blog/devin-sonnet-4-5-lessons-and-challenges).
5. **Drop any stopping condition that reads the worker's own sense of remaining budget.** Context
   anxiety makes it stop early and misreport why (same URL). Track budget externally and do not
   surface true headroom to the worker.
6. **Drop validation commands that only live in prose.** Codex's admission is the exact gap: if the
   harness does not execute the check itself, it does not happen
   (https://learn.chatgpt.com/codex/agent-configuration/agents-md). `require_evidence=True` is only
   real if the harness runs the command.
7. **Drop any deterministic gate without an escape hatch.** 8 consecutive blocks then override
   (https://code.claude.com/docs/en/best-practices); fail open unless explicitly `failClosed`
   (https://cursor.com/docs/agent/hooks).
8. **Drop long single sessions.** `keep_context_turns=3` is already in the right spirit; the field
   says go further and split the job, not the context
   (https://docs.devin.ai/essential-guidelines, https://ampcode.com/docs/threads).
9. **Drop per-request model routing as a goal.** The only shipped implementation publishes no
   accuracy or latency numbers and hides its signals (https://cursor.com/docs/cursor-router), and
   the same vendor's Auto selection is correlated with the review-step forever-loop
   (https://forum.cursor.com/t/cursor-agent-enters-endless-loop-without-progressing-past-review-step/126815).
   Pin models per role; revisit later.
10. **Drop the 200/500-line instruction-file caps as if they were measured.** They are folklore —
    three vendors, three different numbers, same justification, no study. Pick the smallest because
    local models have less room, not because anyone proved it.
11. **Drop any prompt file whose meaning depends on load order.** Concatenation order is not
    guaranteed (https://code.visualstudio.com/docs/copilot/customization/custom-instructions).
12. **Drop `awesome-cursorrules` as a source for role prompts** — it has one security rule against
    40+ frontend ones and nothing about loop control
    (https://github.com/PatrickJS/awesome-cursorrules).
