# From a supplied backlog to an overnight run

Current source: September 26, 2026 (`0dc9256`). Compiler and controller have offline coverage. Live multi-milestone validation remains [#21](https://github.com/wmlanglois/handoff/issues/21) / [#7](https://github.com/wmlanglois/handoff/issues/7).

## Choose the entry that matches your input

| Input | Entry |
|---|---|
| Rough idea | README guided intake. |
| Existing prose design | Brief context, retaining the original; explicitly structure items for batching. |
| JSON outcome contracts | `autonomous --plan-file <plan.json>`; gates/approvals still apply. |
| Structured Markdown backlog | `backlogc.py` then `overnight.py`. |
| One confirmed package | `autonomous --delegate <name>`; not automatic future-goal creation. |

Keep project/packages/run state outside public source. Establish model seats, tool authority, confirmed intake, budget and standing delegation before walking away. Use one controller and a consistent `FLEET_RUNS_DIR`. E3=D is within-goal cadence, not backlog authorization.

## 1. Preserve the design; make item boundaries explicit

```markdown
## APP-1: Event store
deliverable: app/events.py
acceptance:
- append then replay returns events in insertion order

## APP-2: Event view
depends: APP-1
deliverable: app/view.py
acceptance:
- the view renders the accepted event store's replay result
```

Keep the full design in the parent scope/brief. Items need checkable acceptance; callbacks need argument/return and success/failure conventions. Compiler output is criteria and lineage, not final contracts: the ordinary planner still authors and gates those. Each milestone needs a meaningful launch check.

```text
python run/backlogc.py compile <backlog.md> --max-items 6
```

Review `<name>.batches.json` and `<name>.status.json` beside the source. Resolve every `not_compiled` or `unparsed` entry. A compiler failure does not authorize dropping requirements.

## 2. Make milestone packages

```text
python run/backlogc.py packages <backlog.md> --parent-package <confirmed-parent-package> --out <batch-folder>
```

This writes batch packages, scope pages, `seed_criteria.json`, and `<batch-folder>/backlog.json`. Parent confirmed intake/environment and project scope supply context; batch IDs supply planner criteria. No approval is recorded. Existing package directories are retained, not rewritten.

**Current boundaries:** `packages` recompiles at the default six-item batch size, ignoring a previous custom `compile --max-items` result. It does not refuse every partial compilation. Inspect/fix source problems first; compare generated batches to the full source. Do not reuse an existing batch folder for a changed design and assume retained packages updated. Track these under #21.

## 3. Authorize the exact backlog and total budget

Example amounts are illustrative, not new permission or universal defaults:

```text
python run/overnight.py authorize <batch-folder>/backlog.json --as <operator> --decisions 40 --seconds 28800
python run/overnight.py run <batch-folder>/backlog.json --workers worker-a,worker-b --tool-mode tools
python run/overnight.py status <batch-folder>/backlog.json
```

Tools require explicit trusted-host execution permission and a working service. Workers are comma-separated. The operator name records delegation, not authentication; do not invent another person's approval.

The controller calls ordinary `autonomous --delegate` for eligible milestones. `<backlog>.state.json` records backlog SHA, budget and progress; `<backlog>.report.md` consolidates results. Remaining budget comes from recorded goal spend, not a universal meter for every setup/planner token. Changed backlog needs reauthorization. Rerunning `run` is continuation, not a budget reset.

## 4. Know the continuation boundaries

- DONE permits dependents to run; a not-yet-started dependent gets a copied promoted baseline.
- Crashes have bounded retries. Human/intake/preflight/review stops block that milestone; independent eligible milestones may continue. BUDGET ends the run.
- Baseline inheritance currently selects **only the last listed dependency**. It does not merge parallel checkpoints. Do not promise fan-in combines predecessor files; a cumulative chain must already carry required files in its selected checkpoint.
- Blocked milestones stay blocked on rerun; there is no public unblock command yet. Preserve evidence rather than hand-editing state as routine recovery. This belongs to #7.
- Each ordinary CLI launch snapshots current source. It protects the running process, not future launches, external settings, the tool service or servers. It is not a concurrency lock.

Do not invent new requirements or promise every item finishes in one night. The morning report must account for completed, blocked and remaining backlog—not just accepted packets.

## Service milestones

A launcher contract can declare `probe`, copied to the approved map's `milestone.probe`:

```json
{"url": "http://127.0.0.1:8765/health", "expect": 200, "contains": "healthy"}
```

Use an explicitly local, unused port. The journey checks readiness and stops the service; without a probe, staying alive is unverified. Readiness is not a full functional test. The separate sensitivity gate still expects exit-based mutant behavior and can be inconclusive for persistent servers; [#43](https://github.com/wmlanglois/handoff/issues/43) tracks this reproduced gap. Do not claim #41 alone proves server checkpoint promotion. An approved bounded journey that starts, behaviorally checks and stops the service is the existing exit-based shape; changing it requires the applicable authority.
