"""The conductor: an approved goal becomes sustained useful work.

WHAT IT USED TO DO, AND WHY THAT WAS THE DEFECT. The old conductor asked a frontier model to
"decompose the GOAL into 1 to 5 SMALL, checkable jobs", ran that list in order, printed
"3/5 accepted" and exited. Three things were wrong and only the third is obvious:

  1. the unit of delegation was a CHORE, so the harness could only produce chores (see run/plan.py);
  2. nothing was durable -- the goal lived in a local variable, so a restart lost the goal, the
     evidence, the open questions and the budget, and the only way to resume was a human
     re-typing the goal;
  3. an empty job list was reported as a finished run. It is not. An empty queue with unmet
     criteria is BLOCKED, and a harness that cannot tell those apart reports success for work it
     never did. That is the specific dishonesty this rewrite exists to remove.

WHAT IT DOES NOW. A human approves a goal and a plan ONCE. After that the conductor keeps choosing
useful work by itself: it asks the ledger (`run/goals.py`) what is ready given the dependency graph
and the remaining budget, grades each assignment against the delegation check (`run/plan.py`)
BEFORE spending a worker on it, dispatches, folds the outcome back into the ledger, and asks again.
It stops for a DEMONSTRATED BLOCKER -- a parked question, a human-review gate, an infrastructure
failure, an exhausted budget -- and never merely to report status. The stop reason is printed with
the specific thing in the way.

THE EXECUTOR IS A SEAM. `advance()` takes any callable card -> Execution. Track A is still proving
the exclusive-execution and earned-acceptance controls, so the DEFAULT here is the simulated
executor and the live one has to be asked for explicitly by name. Nothing in this module or its
tests calls a model. When A reports its controls proven, `verified_executor` is the drop-in: it
already speaks the FLEET_OUTCOME marker contract that run/queue.py classifies on.

    python run/conductor.py start <folder> --name <project>
    python run/conductor.py plan "Own X: ..." --criterion "..." --criterion "..."
    python run/conductor.py approve g-20260919-...-ab12 --as operator
    python run/conductor.py advance g-20260919-...-ab12 --simulate accepted
    python run/conductor.py status g-20260919-...-ab12
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "run"))
from safeio import force_utf8  # noqa
force_utf8()

import goals  # noqa
import plan as planning  # noqa
from fleet import DEFAULT_WORKER, SKEPTIC_WORKER  # noqa
from fleet import runs_root as fleet_runs_root  # noqa: E402  (one runs root for writer+readers)

# A continuation loop with no bound is a runaway. When the goal carries no assignment budget this
# is the backstop, chosen to be obviously finite rather than generous.
DEFAULT_MAX_ASSIGNMENTS = 50


@dataclass
class Execution:
    """What came back from running one card. `outcome` MUST be a member of the closed I3
    vocabulary; the ledger refuses anything else rather than guessing."""
    outcome: str
    evidence_ref: str = ""
    seconds: float = 0.0
    detail: str = ""


@dataclass
class Run:
    """One bounded continuation: what was dispatched, what came back, and why it stopped."""
    goal_id: str
    dispatched: list = field(default_factory=list)      # [(name, outcome)]
    rejected: list = field(default_factory=list)        # [(name, why)] -- delegation check
    stopped_because: str = ""
    disposition: dict = field(default_factory=dict)

    @property
    def executions(self):
        return len(self.dispatched)


# =================================================================================================
# executors
# =================================================================================================

def simulated_executor(script=None, default="accepted", seconds=0.0):
    """A stub executor for developing the continuation loop without touching the fleet.

    `script` maps an assignment name to an outcome, or to a LIST of outcomes consumed one per
    attempt (which is how an infrastructure failure followed by a success is expressed), or to a
    callable taking the card. Everything not named gets `default`.

    This is a seam, not a mock of convenience: the real executor obeys the same contract, so the
    continuation logic under test is the production logic."""
    script = dict(script or {})
    calls = []

    def run(card):
        display = card.get("assignment") or card["name"]
        calls.append(display)
        spec = script.get(display, default)
        if callable(spec):
            spec = spec(card)
        if isinstance(spec, list):
            spec = spec.pop(0) if spec else default
        if isinstance(spec, Execution):
            return spec
        ref = "" if spec != "accepted" else f"sim://{display}/{len(calls)}"
        return Execution(outcome=spec, evidence_ref=ref, seconds=seconds,
                         detail=f"simulated {spec}")
    run.calls = calls
    return run


def verified_executor(max_rounds=5, timeout=1800, cards_dir=None):
    """The real executor: run one card through run/verified.py, exactly as run/queue.py does.

    It reads the `FLEET_OUTCOME=` marker rather than the exit code, because three terminal outcomes
    share exit code 1 and classifying on the code files "a human must judge this" identically to
    "ran out of rounds". The evidence reference is the receipt path that job leaves behind, so the
    goal ledger points at something inspectable rather than at a claim.

    NOT the default, and not exercised by C's tests: Track A owns the controls that make live
    execution safe. Ask for it by name once those are proven."""
    cards = Path(cards_dir) if cards_dir else fleet_runs_root() / "goal-cards"

    def run(card):
        if not card.get("worker"):
            # I6 (capability selection for worker=None) is Track A's to implement. Until it lands,
            # run/verified.py reads card["worker"] directly and would die on a KeyError far from
            # here. Refusing as infrastructure keeps the job alive and re-offerable.
            return Execution(outcome="worker-unavailable",
                             detail="card has no worker and capability selection (I6) is not "
                                    "available in this checkout")
        cards.mkdir(parents=True, exist_ok=True)
        p = cards / f"{card['name']}.json"
        p.write_text(json.dumps(card, indent=2), encoding="utf-8")
        t = time.time()
        try:
            r = subprocess.run([sys.executable, str(ROOT / "run" / "verified.py"), str(p),
                                "--max-rounds", str(max_rounds)],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return Execution(outcome="worker-unavailable", seconds=time.time() - t,
                             detail=f"job exceeded {timeout}s")
        outcome = ""
        for line in (r.stdout or "").splitlines():
            if line.startswith("FLEET_OUTCOME="):
                outcome = line.split("=", 1)[1].strip()
        if not outcome:
            outcome = "worker-unavailable" if r.returncode == 3 else "parked-stagnant"
        receipt = fleet_runs_root() / f"verified-{card['name']}" / "receipt.json"
        tail = ((r.stdout or "").strip().splitlines() or [""])[-1]
        return Execution(outcome=outcome,
                         evidence_ref=str(receipt) if receipt.exists() else "",
                         seconds=round(time.time() - t, 1), detail=tail)
    return run


# =================================================================================================
# planning
# =================================================================================================

def frontier_plan(goal_text, criteria, model=None, timeout=300, feedback="", context=""):
    """Ask the frontier CLI for outcome contracts. Isolated in one function so every other part of
    this module is testable without a model."""
    from architect import ARCHITECT_MODEL
    crit = "\n".join(f"  {c['id'] if isinstance(c, dict) else i}: "
                     f"{c['text'] if isinstance(c, dict) else c}"
                     for i, c in enumerate(criteria, 1))
    fb = ("\n\nYOUR PREVIOUS PLAN WAS REJECTED BEFORE DISPATCH. Fix EXACTLY these problems against "
          "the SAME approved purpose and criteria; do not change the criteria, do not duplicate an "
          "already-accepted outcome, and cover every criterion still listed as uncovered:\n"
          + feedback) if feedback else ""
    ctx = ("\n" + context.strip() + "\n") if (context or "").strip() else ""
    prompt = (f"{planning.PLAN_SYS}\n{ctx}\nGOAL:\n{goal_text}\n\nAPPROVED CRITERIA:\n{crit}{fb}\n\n"
              "Return the outcome JSON now.")
    exe = shutil.which("claude") or "claude"
    r = subprocess.run([exe, "-p", "--model", model or ARCHITECT_MODEL, "--output-format", "text"],
                       input=prompt, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return planning.parse_plan(r.stdout or "")


def _plan_feedback(findings, uncovered):
    lines = []
    for f in findings:
        if f.get("problems"):
            lines.append("- outcome {0!r} (criterion {1}): {2}".format(
                f.get("name"), f.get("criterion_id"),
                "; ".join("{0}: {1}".format(code, why) for code, why in f["problems"])))
        elif f.get("error"):
            lines.append("- planner error: {0}".format(f["error"]))
    unc = ", ".join("{0} ({1})".format(c["id"], c["text"][:80]) if isinstance(c, dict) else str(c)
                    for c in uncovered)
    return "\n".join(lines) + ("\nStill UNCOVERED: " + unc if unc else "")


def plan_with_recovery(goal_id, goal_text, criteria, *, rounds=2, planner=None, echo=print, root=None,
                       extra_review=None, initial_feedback=""):
    """O3: plan, gate, and give the planner a BOUNDED chance to correct rejected work against the
    unchanged purpose. Valid outcomes from any round are kept (one owner per criterion); criteria
    still uncovered after the bound are parked with their exact finding (honest stop). Returns
    (accepted, findings, uncovered)."""
    planner = planner or frontier_plan
    cids = [c["id"] if isinstance(c, dict) else c for c in criteria]
    accepted, findings, covered, feedback = [], [], set(), initial_feedback
    for r in range(rounds + 1):
        uncovered = [c for c in criteria if (c["id"] if isinstance(c, dict) else c) not in covered]
        if not uncovered:
            break
        try:
            import toolpolicy
            tool_mode = goals.state(goal_id, root=root).get("tool_mode")
            contracts = planner(goal_text, criteria,
                                feedback=feedback + "\n" + toolpolicy.instruction(tool_mode))
            contracts = [toolpolicy.apply(c, tool_mode) for c in contracts]
        except Exception as e:
            findings.append({"round": r, "error": repr(e)})
            break
        st_plan = goals.state(goal_id, root=root)
        ok, bad = planning.gate(contracts, criteria=[{"id": i} for i in cids] if not isinstance(criteria[0], dict) else criteria, root=ROOT, project_root=st_plan.get("project_root"), integration_check=(st_plan.get("integration") or {}).get("check_source") or None, enforce_closure=False)
        if extra_review:
            kept = []
            for con in ok:
                problems = list(extra_review(con) or [])
                if problems:
                    bad.append((con, planning.Review(name=con.get("name") or "?", ok=False, problems=problems)))
                else:
                    kept.append(con)
            ok = kept
        for con in ok:
            cid = con.get("criterion_id")
            approach = (con.get("approach") or "").strip()
            # One owner per criterion, unless the planner declared a distinct competing approach
            # (LF-04). An unnamed second outcome is still a duplicate and is not kept.
            clash = False
            for prev in accepted:
                if prev.get("criterion_id") != cid:
                    continue
                other = (prev.get("approach") or "").strip()
                if not approach or not other or approach == other:
                    clash = True
                    break
            if clash:
                continue
            con["goal_id"] = goal_id
            accepted.append(con)
            covered.add(cid)
            echo("  proposed {0} -> {1}: {2}".format(con.get("name"), cid, (con.get("capability") or "")[:70]))
        for con, rev in bad:
            findings.append({"round": r, "name": con.get("name"), "criterion_id": con.get("criterion_id"),
                             "problems": list(rev.problems), "oracle": (con.get("oracle") or "")[:600]})
            echo("  REJECTED {0}: {1}".format(con.get("name"), rev.why()))
        # WHOLE-PLAN CONSISTENCY (2026-09-23). Acceptance is incremental across rounds, but the
        # cross-contract checks in review() only saw the siblings present in the round each contract
        # was accepted in. A consumer accepted in round 0 against a provider that was REJECTED that
        # round, then re-authored in round 1 with a different public interface, leaves the assembled
        # plan self-inconsistent (launcher/test-suite calling engine.simulate_journey while the engine
        # that landed provides run_route). Re-review every accepted contract against the CURRENT full
        # set; kick back any that now fail so the remaining rounds re-author it against the real
        # interface. Without this, a per-round-green plan ships an interface mismatch to a worker.
        cids2 = [c["id"] if isinstance(c, dict) else c for c in criteria]
        crit_objs = criteria if criteria and isinstance(criteria[0], dict) else [{"id": i} for i in cids2]
        kicked = True
        while kicked:
            kicked = False
            for con in list(accepted):
                rv = planning.review(con, others=[o for o in accepted if o is not con],
                                     criteria=crit_objs, root=ROOT,
                                     project_root=st_plan.get("project_root"),
                                     enforce_closure=False)
                probs = list(rv.problems)
                if extra_review:
                    probs += list(extra_review(con) or [])
                if probs:
                    accepted.remove(con)
                    covered.discard(con.get("criterion_id"))
                    findings.append({"round": r, "name": con.get("name"),
                                     "criterion_id": con.get("criterion_id"),
                                     "problems": probs, "phase": "whole-plan"})
                    echo("  KICKED BACK {0}: {1}".format(con.get("name"),
                         "; ".join("{0}: {1}".format(k, v) for k, v in probs)[:160]))
                    kicked = True
                    break
        uncovered = [c for c in criteria if (c["id"] if isinstance(c, dict) else c) not in covered]
        # Once every criterion appears covered, validate the assembled dependency graph BEFORE
        # ending the bounded planner loop. Otherwise a complete-looking but orphaned plan uses up
        # no repair round and the user has to restart planning merely to hear the same defect.
        if not uncovered:
            _drop_invalid_dependencies(accepted, covered, findings, crit_objs, st_plan,
                                       round_id=r, echo=echo)
            uncovered = [c for c in criteria if (c["id"] if isinstance(c, dict) else c) not in covered]
        if not uncovered:
            break
        feedback = _plan_feedback(findings, uncovered)
        if r < rounds:
            echo("  RE-PLANNING ({0} criteria uncovered)".format(len(uncovered)))
    # A partial final plan also needs closure before it is recorded, even if other criteria never
    # received admissible work at all.
    crit_objs_final = criteria if criteria and isinstance(criteria[0], dict) else [{"id": i} for i in cids]
    _drop_invalid_dependencies(accepted, covered, findings, crit_objs_final,
                               goals.state(goal_id, root=root), round_id="closure", echo=echo)
    goals.propose(goal_id, accepted, root=root)
    uncovered = [c for c in criteria if (c["id"] if isinstance(c, dict) else c) not in covered]
    goals.record_plan_rejections(goal_id, findings, uncovered, root=root)
    return accepted, findings, uncovered


def _drop_invalid_dependencies(accepted, covered, findings, criteria, goal_state, *, round_id, echo):
    """Remove graph edges the scheduler cannot satisfy, then recheck consumers of removed nodes."""
    dropped = True
    while dropped:
        dropped = False
        for con in list(accepted):
            rv = planning.review(con, others=[o for o in accepted if o is not con],
                                 criteria=criteria, root=ROOT,
                                 project_root=goal_state.get("project_root"), enforce_closure=True)
            graph_errors = [(code, msg) for code, msg in rv.problems
                            if code in ("ORPHAN_DEP", "CYCLE_DEP")]
            if graph_errors:
                accepted.remove(con)
                if not any(o.get("criterion_id") == con.get("criterion_id") for o in accepted):
                    covered.discard(con.get("criterion_id"))
                findings.append({"round": round_id, "name": con.get("name"),
                                 "criterion_id": con.get("criterion_id"),
                                 "problems": graph_errors, "phase": "closure"})
                echo("  ORPHANED {0}: {1}".format(con.get("name"),
                     "; ".join("{0}: {1}".format(k, v) for k, v in graph_errors)[:160]))
                dropped = True
                break


def propose_plan(goal_id, contracts, *, root=None, echo=print):
    """Grade the plan, park what fails, and record the rest for human approval.

    Rejections do not sink the plan: the good assignments are proposed and each rejection becomes a
    parked QUESTION against its criterion, so the gap is visible to the operator instead of being
    silently dropped. That is also why the check runs here and not inside the worker -- it grades
    the MANAGER, and nothing else in this repo does."""
    st = goals.state(goal_id, root)
    import toolpolicy
    contracts = [toolpolicy.apply(c, st.get("tool_mode")) for c in contracts]
    ok, bad = planning.gate(contracts, criteria=st["criteria"], root=ROOT, project_root=(st.get("project_root")), integration_check=(st.get("integration") or {}).get("check_source") or None)
    for c, rev in bad:
        echo(f"  REJECTED {c.get('name')}: {rev.why()}")
        cid = c.get("criterion_id")
        if any(x["id"] == cid for x in st["criteria"]):
            goals.park(goal_id, cid,
                       f"assignment {c.get('name')!r} was refused before dispatch -- {rev.why()}",
                       assignment=c.get("name"), root=root)
    for c in ok:
        c["goal_id"] = goal_id
        echo(f"  proposed {c['name']} -> {c['criterion_id']}: {c['capability'][:70]}")
    goals.propose(goal_id, ok, root=root)
    return ok, bad


# =================================================================================================
# bounded continuation
# =================================================================================================

def advance(goal_id, executor, *, capacity=1, max_assignments=None, root=None, echo=print):
    """Keep choosing useful work until there is none, within a budget. Returns a `Run`.

    The loop body is deliberately dull: ask the ledger what is ready, claim it, execute it, record
    the outcome, ask again. All the judgement lives in `goals.next_work` (dependency- and
    budget-aware) and in the outcome vocabulary. Two properties this shape buys:

      * one blocked assignment does not stop independent work. A park removes that assignment from
        the ready set and the next iteration picks up whatever else is ready -- there is no
        "abort on first failure" path, because there is no failure path at all, only outcomes.
      * stopping is always explained. The loop exits when nothing is ready, and the reason comes
        from `goals.disposition`, which names the parked question, the review gate, the
        infrastructure block, the unmet dependency or the exhausted budget.
    """
    st = goals.state(goal_id, root)
    budget = st["budget"].get("assignments")
    cap_total = max_assignments if max_assignments is not None else (
        budget if budget is not None else DEFAULT_MAX_ASSIGNMENTS)
    run = Run(goal_id=goal_id)
    import projectpkg
    letter = projectpkg.effective_cadence(st.get("review_letter") or "", st.get("domain_branch") or "")
    one_wave = letter == "B"
    if letter == "A":
        cap_total = min(cap_total, 1)
    while run.executions < cap_total:
        cards = goals.next_work(goal_id, min(capacity, cap_total - run.executions), root=root)
        if cards:
            import steer
            chosen, deferred = steer.select(goals.state(goal_id, root), cards)
            if deferred:
                goals.record_selection(goal_id,
                                       [(c.get("assignment") or c["name"], k, w) for c, k, w in chosen],
                                       [(c.get("assignment") or c["name"], k, w) for c, k, w in deferred], root=root)
                for cd, k, w in deferred:
                    echo(f"  deferred {cd.get('assignment') or cd['name']:<22} [{k}] {w[:100]}")
            cards = [cd for cd, k, w in chosen]
        if not cards:
            break
        st_now = goals.state(goal_id, root)
        limits = (st_now.get("limits") or {})
        assignments = st_now.get("assignments") or {}
        allowed = []
        for card in cards:
            display = card.get("assignment") or card["name"]
            # Scan the stored contract, not the rendered card (mirrors orchestrate.py). card_for
            # pastes the whole scope into the brief, and a scope that says "Never delete files"
            # would match the never-rule against its own statement.
            stored = (assignments.get(display) or {}).get("contract")
            hit = projectpkg.limit_hits(stored or card, limits)
            if hit:
                echo("  refused {0}: {1}".format(display, hit))
                goals.record(goal_id, card["criterion_id"], "parked-undefined", "",
                             assignment=display, detail="never-rule: " + hit, root=root)
                run.rejected.append((display, hit))
            else:
                allowed.append(card)
        cards = allowed
        if not cards:
            continue
        claimed = set(goals.claim(goal_id, [c.get("assignment") or c["name"] for c in cards],
                                  root=root))
        for card in cards:
            display = card.get("assignment") or card["name"]
            if display not in claimed:
                continue                      # someone else took it; not ours to run
            ex = executor(card)
            goals.record(goal_id, card["criterion_id"], ex.outcome, ex.evidence_ref,
                         assignment=display, seconds=ex.seconds, detail=ex.detail, root=root)
            run.dispatched.append((display, ex.outcome))
            echo(f"  [{ex.outcome:<18}] {display:<28} {card['criterion_id']}")
        if one_wave and run.executions:
            break
    run.disposition = goals.disposition(goal_id, root)
    if letter in ("A", "B") and run.executions and goals.next_work(goal_id, 1, root=root):
        run.stopped_because = ("review cadence {0}: stopped so you can see a draft. "
                               "This does not choose how many workers run.".format(letter))
    elif run.executions >= cap_total and goals.next_work(goal_id, 1, root=root):
        run.stopped_because = (f"continuation budget spent: {run.executions} assignments. "
                               f"Work remains ready; this is a BOUND, not a conclusion.")
    else:
        run.stopped_because = (f"{run.disposition['state'].upper()}: "
                               + ("; ".join(run.disposition["reasons"]) or "all criteria met"))
    echo(f"\n{goals.summary(goal_id, root)}\n\nstopped: {run.stopped_because}")
    return run


# =================================================================================================
# CLI
# =================================================================================================

def _cmd_plan(a):
    criteria = [{"id": f"c{i}", "text": t} for i, t in enumerate(a.criterion, 1)]
    integ = None
    if getattr(a, "integration", None):
        integ = json.loads(Path(a.integration).read_text(encoding="utf-8"))
        if getattr(a, "integration_check", None):
            integ["check_source"] = Path(a.integration_check).read_text(encoding="utf-8")
        if a.project_root and not integ.get("project_root"):
            integ["project_root"] = a.project_root
    gid = goals.create(a.goal, criteria,
                       budget={"assignments": a.budget} if a.budget else None,
                       project_root=getattr(a, "project_root", None),
                       integration=integ)
    if getattr(a, "tool_mode", None):
        goals.set_tool_mode(gid, a.tool_mode)
    print(f"goal {gid}")
    if a.plan_file:
        contracts = planning.parse_plan(Path(a.plan_file).read_text(encoding="utf-8"))
        propose_plan(gid, contracts)
    else:
        accepted, findings, uncovered = plan_with_recovery(gid, a.goal, criteria)
        if uncovered:
            print("  UNRECOVERED after re-planning: " + ", ".join(
                (c["id"] if isinstance(c, dict) else c) for c in uncovered))
    print(f"\nnothing runs until a human approves:\n"
          f"  python run/conductor.py approve {gid} --as <you>")
    return gid


def _cmd_approve(a):
    if not (goals.state(a.goal_id).get("proposed") or []):
        raise SystemExit("no proposed plan to approve; run start to plan this goal")
    goals.approve(a.goal_id, a.approver)
    print(goals.summary(a.goal_id))


def _cmd_reject_plan(a):
    """A person rejects the proposed plan on the same goal (issue #4). Keeps the goal and its approved
    scope, records the rejection reason, clears ONLY the proposal, and leaves the goal ready for the
    ordinary planner on the next `start`. Refuses once approved or once any worker has been dispatched,
    so no approval or dispatch can happen against a rejected plan."""
    if not (a.reason or "").strip():
        raise SystemExit("reject-plan needs --reason; a silent rejection is not actionable")
    st = goals.state(a.goal_id)
    if st.get("approved"):
        raise SystemExit("goal {0} is already approved; a rejection cannot follow approval".format(a.goal_id))
    if st.get("assignments"):
        raise SystemExit("goal {0} already dispatched workers; reject is a pre-dispatch action".format(a.goal_id))
    if not (st.get("proposed") or []):
        raise SystemExit("goal {0} has no proposed plan to reject".format(a.goal_id))
    goals.clear_rejected_plan(a.goal_id, reason=a.reason, by=(a.approver or ""))
    print("rejected the proposed plan for {0}; scope and criteria are kept.".format(a.goal_id))
    print("reason recorded: {0}".format(a.reason))
    print("re-plan the SAME goal by running start again on this package:")
    print("  python run/conductor.py start <project-folder> --name <name> --package <package-folder>")
    print("no approval or dispatch occurs while the plan is rejected.")


def _cmd_advance(a):
    """Live work goes through run/orchestrate.py -- the SAME path the demonstrations used.

    This used to dispatch straight to run/verified.py through its own executor seam, which skips
    the claim, the lease and the executor-start check, and never reaches the architect decision
    step. Two execution paths for one workflow means the one the operator types is not the one
    that was verified. Simulation keeps the local seam: it exists precisely to avoid the fleet."""
    if a.live:
        import orchestrate
        print("LIVE execution: dispatching through the queue (claims, leases, evidence) "
              "and the architect decision step.")
        summary = orchestrate.run_goal(
            a.goal_id,
            [w.strip() for w in (a.workers or DEFAULT_WORKER).split(",") if w.strip()],
            max_seconds=a.max_seconds, max_rounds=a.max_rounds,
            max_decisions=(0 if a.no_decisions else a.max_decisions),
            use_skeptic=not a.no_skeptic, deliverable_root=a.deliverable_root)
        print("\nSTOP: {0} -- {1}".format(summary["stop"], summary["reason"]))
        for i in summary.get("interventions") or []:
            print("  intervention: " + str(i))
        return 0 if summary["stop"] == "DONE" else 1
    ex = simulated_executor(default=a.simulate)
    print(f"SIMULATED execution (every card returns {a.simulate!r}). "
          f"Nothing is sent to a worker.")
    advance(a.goal_id, ex, capacity=a.capacity, max_assignments=a.max_assignments)


def _cmd_resolve(a):
    touched = goals.resolve(a.goal_id, answer=a.answer, by=a.by, criterion_id=a.criterion)
    print("answered; reopened: {0}".format(", ".join(touched) or "(none)"))
    print(goals.summary(a.goal_id))


def _cmd_sign_off(a):
    goals.sign_off(a.goal_id, a.criterion, by=a.by, note=a.note or "")
    print(goals.summary(a.goal_id))
    print("\ncomplete(): {0}".format(goals.complete(a.goal_id)))


def _cmd_memory(a):
    import memory
    if a.action == "list":
        for kind, name in (("lesson", memory.LESSONS), ("skill", memory.SKILLS)):
            rows = memory._latest(name).values()
            print("{0}s: {1}".format(kind, len(rows)))
            for r in rows:
                head = r.get("text") if kind == "lesson" else "{0} v{1} ({2})".format(
                    r.get("name"), r.get("version"), r.get("module", "?"))
                rel = r.get("reliability") or {}
                print("  {0} [{1}] {2} evidence={3} uses={4}{5} :: {6}".format(
                    r["id"], r["status"], r.get("kind", "?"), memory.evidence_state(r),
                    r.get("uses", 0),
                    " verified={0} failed={1}".format(rel.get("verified", 0), rel.get("failed", 0))
                    if kind == "skill" else "",
                    str(head)[:100].replace(chr(10), " ")))
        return 0
    if a.action == "skill-result":
        if a.ok == a.failed:
            raise SystemExit("skill-result needs exactly one of --ok / --failed")
        for i in a.id:
            row = memory.record_skill_result(i, a.ok, by=a.by or "operator", scope=a.scope,
                                             note=a.note)
            print("{0} -> {1} (verified={2} failed={3})".format(
                i, row["status"], row["reliability"]["verified"], row["reliability"]["failed"]))
        return 0
    if a.action == "revalidate":
        for i in a.id:
            row = memory.revalidate_skill(i, by=a.by or "operator", note=a.note or a.reason)
            print("{0} -> {1}; failures before revalidation: {2}".format(
                i, row["status"], row["reliability"].get("failed_before_revalidation")))
        return 0
    if a.action == "withdraw-skill":
        for i in a.id:
            memory.withdraw_skill(i, a.reason, by=a.by or "operator")
            print("withdrawn " + i)
        return 0
    if a.action == "reassess":
        for i in a.id:
            row = memory.reassess(i, by=a.by or "operator", evidence_ref=a.evidence_ref, note=a.note,
                                  applies_when=a.narrow or None)
            print("{0} -> {1}".format(i, row["status"]))
        return 0
    if a.action == "refresh-contract":
        for i in a.id:
            row = memory.refresh_contract(i, by=a.by or "operator")
            print("{0}: {1} example(s) now recorded".format(i, len(row["contract"].get("examples") or [])))
        return 0
    if a.action == "legacy-disposition":
        rows = memory.disposition_legacy(by=a.by or "operator")
        print("dispositioned {0} legacy row(s): {1}".format(len(rows), [r["id"] for r in rows]))
        return 0
    if a.action == "match":
        lines = memory.sources_for(a.brief)
        print("\n".join(lines) or "(nothing applicable; the store abstained)")
        return 0
    if a.action == "withdraw":
        for i in a.id:
            memory.withdraw_lesson(i, a.reason, by=a.by)
            print("withdrawn " + i)
        return 0
    if a.action == "contradict":
        if len(a.id) != 2:
            raise SystemExit("contradict needs exactly two --id values")
        memory.contradict(a.id[0], a.id[1], a.reason, by=a.by)
        print("both marked contradicted; neither is offered until a person resolves it")
        return 0
    if a.action == "add-lesson":
        row = memory.add_lesson(a.text, a.source, memory.tags_for(a.text), a.limits, by=a.by,
                                unverified=a.unverified)
        print("added {0} [{1}]".format(row["id"], row["status"]))
        return 0


def _cmd_workers(a):
    import profiles
    card = {"capability": a.brief, "brief": a.brief}
    seen = sorted({r.get("worker") for r in profiles.rows() if r.get("worker") and not r.get("skip")})
    if not seen:
        print("(no worker records yet)")
        return 0
    for wk in seen:
        s = profiles.summarize(wk, card) if a.brief else {"n": len([r for r in profiles.rows() if r.get("worker") == wk and not r.get("skip")]),
                                                           "counts": {}, "certainty": "all work", "reasons": [], "unassisted_failures": 0, "assisted_acceptances": 0}
        print("{0}: {1} record(s) [{2}] {3}".format(wk, s["n"], s["certainty"], s["counts"]))
    if a.brief and len(seen) > 1:
        ordered, notes = profiles.prefer(card, seen)
        print("preference for this work:", " > ".join(ordered))
        for wk in ordered:
            print("  {0}: {1}".format(wk, notes.get(wk, "")))
    return 0


def _parse_conditions(items):
    """'criterion_met:c2' 'file_present:money.py' 'skill_available:S-1' 'assumption_invalidated:L-1'"""
    out = []
    for it in items or []:
        t, _, v = it.partition(":")
        key = {"criterion_met": "criterion_id", "file_present": "path", "skill_available": "id",
               "assumption_invalidated": "memory_id"}.get(t.strip(), "value")
        out.append({"type": t.strip(), key: v.strip()})
    return out


def _cmd_challenge(a):
    import decide
    dec = {"action": "CHALLENGE", "criterion_id": a.criterion, "kind": a.kind, "basis": a.basis,
           "evidence_ref": a.evidence_ref, "why": a.basis}
    d = decide.state_delta(a.goal_id)
    ok, why = decide.validate(dec, d)
    if not ok:
        raise SystemExit("refused: " + why)
    if a.by != "architect":
        art = decide.accepted_artifact_root(a.goal_id, a.criterion)
        assist = decide.assist_challenge(a.criterion, a.basis, art) if a.assist else ""
        row = goals.challenge(a.goal_id, a.criterion, by=a.by, kind=a.kind, basis=a.basis,
                              evidence_ref=a.evidence_ref, assist=assist)
        print("challenged {0} as {1}; question {2}; accepted artifact: {3}".format(
            a.criterion, row["id"], row["question_id"], art))
        if assist:
            print("skeptic: " + assist[:600])
        return 0
    print(decide.apply(a.goal_id, dec, d, assist=a.assist))
    return 0


def _cmd_challenge_resolve(a):
    row = goals.resolve_challenge(a.goal_id, a.id, a.outcome, by=a.by, why=a.why,
                                  evidence_ref=a.evidence_ref, reconsider_when=_parse_conditions(a.reconsider))
    print("{0} -> {1} ({2})".format(a.id, row["status"], row.get("outcome") or "uncertain"))
    return 0


def _cmd_reengage(a):
    items = goals.reengage(a.goal_id, observe_root=a.root)
    if not items:
        print("(nothing re-engaged: no stated condition changed to true)")
    for i in items:
        print("{0} {1} on {2}{3}: {4}".format(i["kind"], i["id"], i.get("criterion"),
                                             " [important]" if i.get("important") else "", i["why"]))
    return 0


def _cmd_proposal(a):
    import decide
    dec = {"action": "PROPOSAL", "proposal_id": a.proposal_id, "resolution": a.resolution,
           "reason": a.reason, "criterion_id": a.criterion, "name": a.name, "brief": a.brief}
    for k in ("artifact", "oracle"):
        if getattr(a, k, None):
            dec[k] = getattr(a, k)
    if getattr(a, "done_when", None):
        dec["done_when"] = list(a.done_when)
    if getattr(a, "reconsider", None):
        dec["reconsider_when"] = _parse_conditions(a.reconsider)
    d = decide.state_delta(a.goal_id)
    ok, why = decide.validate(dec, d)
    if not ok:
        raise SystemExit("refused: " + why)
    print(decide.apply(a.goal_id, dec, d))
    return 0


def _cmd_question(a):
    if a.action == "add":
        q = goals.open_question(a.goal_id, a.text, criterion_id=a.criterion, created_by=a.by,
                                explanations=[{"text": e} for e in a.explanation],
                                distinguishing=a.distinguishing)
        if getattr(a, "reconsider", None):
            goals.set_reconsider(a.goal_id, "question", q["id"], _parse_conditions(a.reconsider))
        print("opened " + q["id"])
        return 0
    if a.action == "reconsider":
        goals.set_reconsider(a.goal_id, "question", a.id, _parse_conditions(a.reconsider))
        print("{0}: reconsider when {1}".format(a.id, ", ".join(a.reconsider)))
        return 0
    res = {"explanation": a.chosen, "why": a.why, "evidence_ref": a.evidence_ref}
    q = goals.update_question(a.goal_id, a.id, resolution=res, by=a.by)
    print("{0} -> {1}".format(a.id, q["status"]))
    return 0


def _cmd_observe(a):
    import observe
    probe = {"tool": a.tool, "target": a.target, "pattern": a.pattern}
    if a.question_id:
        probe["question_id"] = a.question_id
    res = observe.observe(a.root, probe)
    goals.observe(a.goal_id, a.criterion, a.question, probe, res, by=a.by)
    print(("observed from {0}:\n{1}".format(res["ref"], res["text"][:2000])) if res["ok"]
          else "NOT ACCESSIBLE: " + res["reason"])
    return 0 if res["ok"] else 1


def _package_arg(a):
    import projectpkg
    name = getattr(a, "name", None) or ""
    raw = getattr(a, "package", None)
    if raw:
        return Path(raw)
    if not name:
        raise SystemExit("pass --package, or --name so the package can default to intake/packages/<name>/")
    return projectpkg.default_package(name)


def _planner_for(project_root):
    """An external planner process, when FLEET_PLANNER names a script. Otherwise the frontier CLI.

    Either way the planner is told the project root. The goal text stays the approved page."""
    exe = os.environ.get("FLEET_PLANNER")

    def planner(goal_text, criteria, feedback=""):
        if exe:
            payload = json.dumps({
                "goal": goal_text,
                "project_root": str(project_root),
                "criteria": criteria,
                "feedback": feedback or "",
            }, ensure_ascii=False)
            r = subprocess.run([sys.executable, exe], input=payload, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=120)
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout or "planner failed")[:800])
            return planning.parse_plan(r.stdout or "")
        context = ("PROJECT_ROOT: {0}\n"
                   "Land each done-when outcome in this project with integrator handoff and a dest. "
                   "Do not cover a criterion with assert True.").format(project_root)
        return frontier_plan(goal_text, criteria, feedback=feedback, context=context)

    return planner


def unplanned_criteria(goal_criteria, intake_ids, planned_ids):
    """Issue #6: goal criteria the planner never sees and no accepted outcome covers.

    The planner is only handed the intake done-when criteria (criteria_from_intake). A criterion
    added to the goal another way -- e.g. a caller seeding extra spine criteria via add_criterion --
    is neither planned nor reported: it just sits uncovered. This returns those ids so the harness can
    surface them with guidance instead of dropping them silently. The milestone (covered by the
    journey, not a packet) and human_only criteria (examples, sign-offs) are excluded by design.
    """
    intake_ids = set(intake_ids or ())
    planned_ids = set(planned_ids or ())
    out = []
    for c in goal_criteria or ():
        cid = c.get("id")
        if not cid or c.get("human_only") or cid == "milestone":
            continue
        if cid in intake_ids or cid in planned_ids:
            continue
        out.append(cid)
    return out


def _planner_from_file(plan_file):
    """Issue #5: a planner that returns a PRE-WRITTEN plan instead of calling a model, so an existing
    plan can enter the ordinary flow. The contracts still go through the gate, review_first_use, human
    plan approval and map derivation -- only the source of the proposal changes. Re-planning rounds
    return the same contracts (a supplied plan does not self-revise), so a rejected plan stays rejected
    and is reported, never silently model-replaced."""
    contracts = planning.parse_plan(Path(plan_file).read_text(encoding="utf-8"))

    def planner(goal_text, criteria, feedback=""):
        return [dict(c) for c in contracts]
    return planner


def _plan_package(package, name, gid, page, root, *, stage_integ=True, plan_file=None):
    """Plan the approved page against the user's criteria, then stage integration. Same goal id.

    With `plan_file` the proposal comes from that file (issue #5) instead of the model planner; it is
    still gated, approved and mapped through the ordinary path."""
    import projectpkg
    import intake
    import proofloop
    _bind_tool_mode(package, gid)
    st = projectpkg.intake_state(package, name)
    criteria = projectpkg.criteria_from_intake(st)
    limits = projectpkg.limits_from_intake(st)
    answers = (st.get("answers") or {})
    goals.set_unapproved_text(gid, page)
    goals.set_run_policy(
        gid,
        review_cadence=(answers.get("E3") or "").strip(),
        review_letter=projectpkg.cadence_letter(answers.get("E3") or ""),
        domain_branch=intake.branch(st) or "",
        limits=limits,
    )

    def extra(con):
        return projectpkg.review_first_use(con, criteria, limits)

    prior_rejections = goals.state(gid).get("plan_rejection_log") or []
    initial_feedback = (prior_rejections[-1].get("reason") or "") if prior_rejections else ""
    planner = _planner_from_file(plan_file) if plan_file else _planner_for(root)
    accepted, _findings, uncovered = plan_with_recovery(
        gid, page, criteria, planner=planner, extra_review=extra,
        initial_feedback=initial_feedback)
    # Issue #4: an empty-project plan whose approved launch command is `python game.py` must have a
    # producer of that entry file (or a baseline one). A plan that builds only libraries the launch
    # command cannot invoke is not launchable, so it must not be presented as approvable.
    try:
        launch_picture = proofloop.milestone_text(page)
    except proofloop.ProofError:
        launch_picture = page
    unlaunchable = planning.missing_launcher(launch_picture, accepted, project_root=root) if accepted else []
    if uncovered or not accepted or unlaunchable:
        # Do NOT leave an inadmissible plan (uncovered criterion, orphaned dependency, or no launch
        # producer) sitting in `proposed` where a person could approve it. Record the launcher gap as
        # actionable feedback, then clear the approvable proposal so the same goal re-plans cleanly.
        if unlaunchable:
            goals.record_plan_rejections(gid, [{
                "round": "closure", "name": None, "criterion_id": None,
                "problems": [["MISSING_LAUNCHER",
                              "the approved launch command runs {0} but no assignment produces it and "
                              "it is not in the baseline; add a producer of the launch entry point".format(
                                  ", ".join(unlaunchable))]]}], [])
        goals.propose(gid, [])
        return False
    # Issue #6: surface any goal criterion the planner never saw (added via add_criterion, not
    # intake-derived) that no accepted outcome covers -- record it with guidance instead of dropping
    # it silently. Normally empty: the autonomous flow only adds intake criteria (planned), the
    # milestone (excluded) and human_only examples (excluded).
    orphaned = unplanned_criteria(goals.state(gid).get("criteria") or [],
                                  {c["id"] for c in criteria},
                                  {c.get("criterion_id") for c in accepted})
    if orphaned:
        goals.record_plan_rejections(gid, [{
            "round": "closure", "name": None, "criterion_id": cid,
            "problems": [["UNPLANNED_CRITERION",
                          "criterion {0!r} was added to the goal but the planner only plans the "
                          "intake done-when criteria, so it was not worked; supply it via "
                          "`plan --plan-file` or run it as a separate goal".format(cid)]]}
            for cid in orphaned], [])
    # In autonomous proof-loop mode the MILESTONE JOURNEY is the project integration (a fresh-process
    # launch of the assembled candidate); a separate packet-integration group would be a second,
    # redundant DONE gate. `stage_integ=False` leaves the journey as the sole integration.
    spec = projectpkg.integration_spec(
        root, accepted, (answers.get("V2") or "").strip(), (answers.get("V3") or "").strip())
    if spec and stage_integ:
        goals.stage_integration(gid, spec)
    gaps = projectpkg.prose_gaps((answers.get("V2") or "").strip(), (answers.get("V3") or "").strip())
    if gaps:
        goals.add_criterion(gid, {"id": "examples", "text": " ".join(gaps), "human_only": True})
        # Idempotent: a re-plan of the same goal must not stack a second identical park (the examples
        # park survives clear_rejected_plan, which only drops plan-rejected parks). Park once.
        already = any(p.get("criterion_id") == "examples" and not p.get("resolved")
                      for p in (goals.state(gid).get("parked") or []))
        if not already:
            goals.park(gid, "examples", " ".join(gaps) + " Resolve with sign-off, or replace the example with an assert line.")
    doc = projectpkg.load_project(package)
    doc["goal_id"] = gid
    doc["planned_sha256"] = projectpkg.sha256_file(package / "scoped.md")
    projectpkg.save_project(package, doc)
    return True


def _produce_scope(package, name):
    """One scope page from the reference card plus the observed folder. Never overwrites."""
    import projectpkg
    prompt = projectpkg.scope_prompt(package, name)
    exe = os.environ.get("FLEET_SCOPE")
    if exe:
        r = subprocess.run([sys.executable, exe], input=prompt, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=120)
        if r.returncode != 0:
            raise SystemExit((r.stderr or r.stdout or "scope writer failed")[:800])
        text = r.stdout or ""
    else:
        from architect import ARCHITECT_MODEL
        claude = shutil.which("claude") or "claude"
        r = subprocess.run([claude, "-p", "--model", ARCHITECT_MODEL, "--output-format", "text"],
                           input=prompt, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
        text = r.stdout or ""
    if not text.strip():
        raise SystemExit("scope writer returned an empty page")
    if not text.endswith("\n"):
        text += "\n"
    projectpkg.write_scope(package, text)


def _print_approve(gid):
    print("goal {0}".format(gid))
    print("\nnothing runs until a human approves:\n"
          "  python run/conductor.py approve {0} --as <you>\n"
          "or, to send the plan back to the planner (keeps the goal and scope):\n"
          "  python run/conductor.py reject-plan {0} --reason \"...\" --as <you>\n"
          "stopped before assignment approval; no worker was started".format(gid))


def _resume(package, *, allow_draft=False):
    """First incomplete step only. Does not approve, dispatch, or rewrite user files."""
    import projectpkg
    import intake
    package = Path(package)
    doc = projectpkg.load_project(package)
    if doc.get("tool_mode_required") and not doc.get("tool_mode"):
        raise SystemExit("select --tool-mode chat-only or tools with start before continuing")
    name = doc["name"]
    root = doc["project_root"]
    observed = package / "observed.md"
    if not observed.exists():
        observed.write_bytes(projectpkg.observe(Path(root), skip=package).encode("utf-8"))

    st = projectpkg.intake_state(package, name)
    if st is None or not intake.is_done(st):
        mode = doc.get("intake_mode") or ""
        if mode == "defer" or (not mode and not projectpkg.proposed_path(package).is_file()):
            print("stopped before intake drafting; no model call was made")
            print("resume with start <project-folder> --name {0} --package {1} "
                  "--project-kind <local|git-existing|git-new> "
                  "--intake-mode <guided|brief> [--brief FILE] [--draft]".format(name, package))
            raise SystemExit(2)
        if allow_draft and not projectpkg.proposed_path(package).is_file():
            try:
                _cmd_assist_run(package, name)
            except SystemExit as exc:
                print("drafts were not written: {0}".format(exc))
        _print_intake_stop(package, name)
        raise SystemExit(2)

    projectpkg.record_review_cadence(package, name)
    scope = package / "scoped.md"
    if not scope.is_file():
        _produce_scope(package, name)
        print("stopped: review the North Star before anything is planned")
        print("  {0}".format(scope.resolve()))
        print("user statements, observations, assumptions, and proposals are labeled in that page.")
        print("when the page says what you mean:\n"
              "  python run/conductor.py approve-scope {0} --as <you>".format(package))
        raise SystemExit(2)

    if not projectpkg.approval_matches(package):
        current = projectpkg.sha256_file(scope)
        approval = package / "approval.json"
        prior = ""
        if approval.is_file():
            prior = json.loads(approval.read_text(encoding="utf-8")).get("sha256") or ""
        print("stopped: scoped.md is not approved at its current bytes")
        if prior and prior != current:
            print("  approved sha256 {0}".format(prior))
            print("  current  sha256 {0}".format(current))
            print("the page changed, so the earlier approval does not apply")
        print("approve this exact page:\n"
              "  python run/conductor.py approve-scope {0} --as <you>".format(package))
        raise SystemExit(2)

    gid = doc.get("goal_id")
    page = scope.read_bytes().decode("utf-8")
    current = projectpkg.sha256_file(scope)
    if gid:
        st_goal = goals.state(gid)
        if st_goal.get("approved"):
            print("goal {0} is already approved".format(gid))
            print("stopped before dispatch; no worker was started")
            return 0
        planned = doc.get("planned_sha256")
        proposed = st_goal.get("proposed") or []
        assigned = st_goal.get("assignments") or {}
        if planned and planned != current:
            print("scoped.md changed after this goal was planned; replanning {0}".format(gid))
            goals.set_unapproved_text(gid, page)
        elif planned == current and (proposed or assigned):
            print("reusing goal {0}".format(gid))
            _print_approve(gid)
            return 0
        else:
            print("replanning goal {0}; the previous plan was not accepted".format(gid))
            prior = st_goal.get("plan_rejections") or []
            if prior:
                problem = next((f.get("problems") for f in reversed(prior) if f.get("problems")), [])
                feedback = "; ".join("{0}: {1}".format(code, msg) for code, msg in problem)
                goals.clear_rejected_plan(gid, reason=feedback[:800], by="plan-gate")
            # An explicit reject-plan already cleared the proposal and recorded the person's
            # reason. Clearing again here would append an empty automated rejection and erase
            # the only feedback the next planner invocation needs to hear.
    else:
        criteria = projectpkg.criteria_from_intake(projectpkg.intake_state(package, name))
        gid = goals.create(page, criteria, project_root=root)
        goals.bind_scope(gid, page, path=str(scope), approval_sha=current)
        doc = projectpkg.load_project(package)
        doc["goal_id"] = gid
        projectpkg.save_project(package, doc)
    if not _plan_package(package, name, gid, page, root):
        print("goal {0} was created but the plan was not accepted by the gate".format(gid))
        print("run start again to replan this same goal")
        print("stopped before assignment approval; no worker was started")
        raise SystemExit(2)
    _print_approve(gid)
    return 0


def _print_intake_stop(package, name):
    import projectpkg
    import intake
    st = projectpkg.intake_state(package, name) or {"answers": {}}
    miss = intake.missing(st) if st.get("answers") is not None else ["Q0"]
    if not miss and intake.branch(st) is None:
        miss = ["Q0"]
    print("stopped: intake is not complete")
    print("project root: {0}".format(projectpkg.load_project(package)["project_root"]))
    print("observed: {0}".format(package / "observed.md"))
    opened = projectpkg.consequential_open(package, name)
    if opened:
        print("\nUnanswered consequential questions. A draft is not an answer:")
        for qid in opened:
            _qid, _layer, prompt, _opts, _req = intake.QBYID[qid]
            draft = projectpkg.draft_text(package, qid)
            print("  {0}  {1}".format(qid, prompt))
            print("    draft: {0}".format(draft if draft else "(none)"))
    print("\nStill required before intake can be rendered:")
    for qid in miss:
        _qid, _layer, prompt, _opts, _req = intake.QBYID[qid]
        print("  {0}  {1}".format(qid, prompt))
    print("\nSelf-guided, stored verbatim:")
    print("  python run/kickoff.py --name {0} --package {1}".format(name, package))
    print("Agent-assisted drafts stay proposed until you confirm them:")
    print("  python run/conductor.py assist {0}".format(package))
    print("  python run/conductor.py confirm {0} <question> --as <you>".format(package))
    print("Or one answer:")
    print("  python run/intake.py answer {0} <question> \"...\" --dir {1}".format(name, package))


def _cmd_assist_run(package, name, drafts_path=None):
    import projectpkg
    if drafts_path:
        drafts = json.loads(Path(drafts_path).read_text(encoding="utf-8"))
    else:
        prompt = projectpkg.draft_prompt(package, name)
        exe = os.environ.get("FLEET_ASSIST")
        if exe:
            r = subprocess.run([sys.executable, exe], input=prompt, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=120)
            if r.returncode != 0:
                raise SystemExit((r.stderr or r.stdout or "assist failed")[:800])
            text = r.stdout or ""
        else:
            from architect import ARCHITECT_MODEL
            claude = shutil.which("claude")
            if not claude:
                raise SystemExit("no claude command on PATH and FLEET_ASSIST is unset")
            r = subprocess.run([claude, "-p", "--model", ARCHITECT_MODEL, "--output-format", "text"],
                               input=prompt, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=300)
            text = r.stdout or ""
            if r.returncode != 0 and not text.strip():
                raise SystemExit((r.stderr or "assist frontier call failed")[:800])
        drafts = projectpkg.parse_drafts(text)
    projectpkg.assist(package, name, drafts)
    print("recorded drafts as proposed. They are not answers until you confirm them.")
    for qid in (drafts or {}):
        print("  python run/conductor.py confirm {0} {1} --as <you>".format(package, qid))


def _select_tool_mode(package, mode=None, *, allow_unselected=False):
    import projectpkg
    import toolpolicy
    doc = projectpkg.load_project(package)
    selected = mode or doc.get("tool_mode")
    if selected is None and doc.get("tool_mode_required") and not allow_unselected:
        raise SystemExit("select --tool-mode chat-only or tools before planning")
    if selected is not None:
        toolpolicy.validate(selected)
        if doc.get("goal_id"):
            goals.set_tool_mode(doc["goal_id"], selected)
        if doc.get("tool_mode") != selected:
            doc["tool_mode"] = selected
            projectpkg.save_project(package, doc)
    print(toolpolicy.instruction(selected))
    return selected


def _bind_tool_mode(package, gid):
    import projectpkg
    mode = projectpkg.load_project(package).get("tool_mode")
    if mode is not None:
        goals.set_tool_mode(gid, mode)


def _cmd_start(a):
    import projectpkg
    if not a.name or any(s in a.name for s in ("/", "\\")) or a.name in (".", ".."):
        raise SystemExit("--name must be a single path segment")
    folder = Path(a.folder).resolve()
    if not folder.is_dir():
        raise SystemExit("not a folder: {0}".format(folder))
    package = Path(a.package).resolve() if a.package else projectpkg.default_package(a.name)
    print("project root (relative paths resolve from the current shell directory): {0}".format(folder))
    print("intake package: {0}".format(package.resolve()))
    existing = (package / "project.json").is_file()
    prior = projectpkg.load_project(package) if existing else {}
    mode = a.intake_mode or prior.get("intake_mode") or ""
    kind = a.project_kind or prior.get("project_kind") or ""
    import intake
    prior_intake = projectpkg.intake_state(package, a.name) if existing else None
    legacy_ready = existing and ((prior_intake is not None and intake.is_done(prior_intake))
                                 or projectpkg.proposed_path(package).is_file())
    if (not mode or not kind) and not legacy_ready:
        print("choose before creating the package or calling a model:")
        print("  --project-kind local | git-existing | git-new")
        print("  --intake-mode guided | brief | defer")
        print("guided: answer intake questions; add --draft only if you want a model to propose answers")
        print("brief: add --brief FILE; it becomes unconfirmed context for those same questions")
        print("defer: record only local project state and return later, with no model call")
        raise SystemExit(2)
    if a.draft and mode == "defer":
        raise SystemExit("--draft cannot be used with --intake-mode defer")
    if (mode != "defer" and not getattr(a, "tool_mode", None) and not prior.get("tool_mode")
            and (not existing or prior.get("intake_mode") == "defer")):
        raise SystemExit("choose --tool-mode chat-only or tools before planning. tools authorizes "
                         "workspace code execution on the configured tool-service host; chat-only does not.")
    if mode == "brief" and not (a.brief or (package / "brief.md").is_file()):
        raise SystemExit("--intake-mode brief requires --brief FILE (or an existing package brief.md)")
    if a.brief and mode != "brief":
        raise SystemExit("--brief is only used with --intake-mode brief")
    if a.brief:
        projectpkg.validated_brief(Path(a.brief))
    if not existing and kind == "git-existing":
        probe = subprocess.run(["git", "-C", str(folder), "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True)
        if probe.returncode:
            raise SystemExit("--project-kind git-existing requires a Git worktree")
    if not existing and kind == "git-new":
        probe = subprocess.run(["git", "-C", str(folder), "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True)
        if not probe.returncode:
            raise SystemExit("folder is already inside a Git worktree; choose git-existing instead")
        init = subprocess.run(["git", "init", str(folder)], capture_output=True, text=True)
        if init.returncode or not (folder / ".git").exists():
            raise SystemExit("git init failed: {0}".format((init.stderr or init.stdout)[:500]))
        print("initialized local Git repository; no remote was created and nothing was pushed")
    projectpkg.ensure(package, folder, a.name)
    if not existing:
        doc = projectpkg.load_project(package)
        doc["tool_mode_required"] = True
        projectpkg.save_project(package, doc)
    _select_tool_mode(package, getattr(a, "tool_mode", None), allow_unselected=mode == "defer")
    projectpkg.set_first_use(package, project_kind=kind, intake_mode=mode)
    if a.brief:
        target = projectpkg.import_brief(package, Path(a.brief))
        print("stored existing brief as unconfirmed context: {0}".format(target))
    if mode == "defer":
        print("deferred. No model was called; scope, plan, map, and workers were not started.")
        print("resume with the same start command and --intake-mode guided or brief")
        raise SystemExit(2)
    return _resume(package, allow_draft=a.draft)


def _cmd_continue(a):
    return _resume(Path(a.package).resolve())


def _cmd_assist(a):
    import projectpkg
    package = Path(a.package).resolve()
    doc = projectpkg.load_project(package)
    return _cmd_assist_run(package, doc["name"], drafts_path=a.drafts)


def _cmd_confirm(a):
    import projectpkg
    package = Path(a.package).resolve()
    doc = projectpkg.load_project(package)
    projectpkg.confirm(package, doc["name"], a.qid, a.approver)
    print("confirmed {0} as an answer. The draft was not an answer before this.".format(a.qid))


def _cmd_scope(a):
    import projectpkg
    package = Path(a.package).resolve()
    doc = projectpkg.load_project(package)
    _produce_scope(package, doc["name"])
    print("wrote {0}".format(package / "scoped.md"))
    print("review it, then:\n  python run/conductor.py approve-scope {0} --as <you>".format(package))


def _cmd_autonomous(a):
    """Operator entry. Prints the recorded budgets, then runs the loop under those budgets."""
    import orchestrate
    import projectpkg
    package = Path(a.package).resolve()
    _select_tool_mode(package)
    delegate = (getattr(a, "delegate", "") or "").strip() or None
    _select_tool_mode(package, getattr(a, "tool_mode", None))
    # SCOPE gate (issue #13): scope is the one gate --delegate did not cover, so a "paste once
    # and walk away" run always stopped here for a human. A standing --delegate is the human's
    # up-front approval of the exact scoped.md bytes, exactly like the plan and map gates below;
    # the budget, never-rules and consequential sign-offs stay hard bounds regardless.
    if delegate and (package / "scoped.md").is_file() and not projectpkg.approval_matches(package):
        projectpkg.approve_scope(package, delegate, note="standing --delegate (unattended run)")
        print("scoped.md AUTO-APPROVED by standing delegate {0}.".format(delegate))
    gid = autonomous_launch(package, decisions=a.decisions, seconds=a.seconds)
    _bind_tool_mode(package, gid)
    if a.map:
        if not (a.approver or "").strip():
            raise SystemExit("a map approval needs --as <you>")
        spec = json.loads(Path(a.map).read_text(encoding="utf-8"))
        rec = goals.approve_interface_map(gid, spec, a.approver, fixture=False)
        print("map approved sha256 {0} by {1}".format(rec["sha256"], rec["approved_by"]))
    doc = goals.state(gid)
    have_map = bool((doc.get("interface_map") or {}).get("spec"))
    rem = goals.budget_remaining(gid)
    budget_gone = (rem.get("seconds") is not None and rem["seconds"] <= 0)
    # ONE-ENTRY PLANNING (do not silently approve anything). From a freshly approved scope, the
    # ordinary planner PROPOSES a plan; a person approves it; the harness then DERIVES and PROPOSES a
    # map; a person approves that; only then does the loop run. Each stop is a human approval stop and
    # needs no hand-written JSON. Skipped when the budget is already spent, so `run_goal` reports
    # BUDGET rather than planning under an exhausted budget.
    if not have_map and not budget_gone:
        # PLAN stop: no plan proposed or approved yet -> run the ordinary planner and stop.
        if not doc.get("approved") and not doc.get("proposed"):
            stop = _autonomous_plan(gid, Path(a.package).resolve(),
                                    plan_file=getattr(a, "plan_file", None))
            if stop is not None:
                return stop
            doc = goals.state(gid)
        # a plan is proposed but not approved -> a person approves it, UNLESS the operator gave a
        # standing --delegate for this unattended (overnight) run. Issue #7: without --delegate an
        # overnight run still stops here, so "overnight" never actually ran unattended. --delegate is
        # the human's explicit up-front approval; the budget, never-rules and consequential sign-offs
        # stay hard bounds regardless.
        if doc.get("proposed") and not doc.get("approved"):
            if delegate:
                goals.approve(gid, delegate)
                print("plan AUTO-APPROVED by standing delegate {0} ({1} assignments).".format(
                    delegate, len(doc["proposed"])))
                doc = goals.state(gid)
            else:
                print("plan PROPOSED ({0} assignments), NOT approved.".format(len(doc["proposed"])))
                print("  approve:  python run/conductor.py approve {0} --as <you>".format(gid))
                print("  or run unattended:  add --delegate <you> to pre-approve the plan and map")
                print("then re-run the same autonomous command to derive and approve the interface map.")
                return {"stop": "AWAITING_PLAN_APPROVAL", "goal_id": gid, "proposed": doc["proposed"]}
        # MAP stop: plan approved, no APPROVED map -> derive the map from the CURRENT approved plan and
        # propose it; a person approves it, unless a standing --delegate pre-authorizes this run.
        # This also runs when a proposal already exists (a normal run proposed it and paused): an
        # earlier version skipped the whole block then, so a later --delegate resume dispatched with no
        # approved map. A recorded proposal that no longer matches the current plan is stale and is
        # replaced, never approved.
        if doc.get("approved") and doc.get("assignments"):
            spec = derive_map_spec(gid)
            if not spec:
                print("could not derive an interface map from the approved plan; not dispatching.")
                return {"stop": "MAP_DERIVATION_FAILED", "goal_id": gid}
            if spec != (doc.get("proposed_map") or {}).get("spec"):
                goals.propose_interface_map(gid, spec)
            if spec:
                if delegate:
                    goals.approve_interface_map(gid, spec, delegate, fixture=False)
                    print("interface map AUTO-APPROVED by standing delegate {0}.".format(delegate))
                    doc = goals.state(gid)
                else:
                    print("interface map PROPOSED from the approved plan (NOT approved).")
                    print("  review/approve:  python run/conductor.py approve-map {0} --from-proposed --as <you>".format(gid))
                    print("  or edit + approve a file:  python run/conductor.py approve-map {0} <map.json> --as <you>".format(gid))
                    print("then re-run the same autonomous command to launch under the recorded budget.")
                    return {"stop": "AWAITING_MAP_APPROVAL", "goal_id": gid, "proposed_map": spec}
    workers = [w.strip() for w in (a.workers or DEFAULT_WORKER).split(",") if w.strip()]
    # PREFLIGHT gate (issue #12): a configured URL, or a /health 200, is not proof a model
    # generates. Verify the run's coding lanes AND the skeptic AND the tool-service before
    # committing to a run, and refuse with the specific cause -- instead of discovering a dead
    # or locked lane (or a missing skeptic) partway through an unattended run. Reuses the
    # existing check/preflight probes; --skip-preflight (or HANDOFF_SKIP_PREFLIGHT) bypasses it.
    if not (getattr(a, "skip_preflight", False) or os.environ.get("HANDOFF_SKIP_PREFLIGHT")):
        import preflight
        need_tools = any((rec.get("contract") or {}).get("tools")
                         for rec in (goals.state(gid).get("assignments") or {}).values())
        ok, reason, pf_rows = preflight.gate(workers, need_tool_service=need_tools)
        print("tool service: required" if need_tools else "tool service: not-needed (not probed)")
        import fleet as _fleet
        for _k, _v in _fleet.state_paths().items():
            print("state {0:9} {1}".format(_k + ":", _v))
        for _r in pf_rows:
            if _r.get("role") == "tool-runtime":
                print("tool runtime: {0} at {1} ({2}){3}".format(
                    _r.get("kind"), _r.get("path"), ", ".join(_r.get("capabilities") or []) or "no declared capabilities",
                    "; MISSING " + ", ".join(_r["missing"]) if _r.get("missing") else ""))
        if not ok:
            print("PREFLIGHT FAILED: " + reason)
            print("  fix the fleet, or re-run with --skip-preflight to bypass.")
            return {"stop": "PREFLIGHT_FAILED", "reason": reason}
        if any(r.get("verdict") == "not-configured" and r.get("role") == "skeptic" for r in pf_rows):
            print("WARNING: no skeptic configured (SKEPTIC_WORKER is empty) -- this run has no "
                  "independent review of worker output.")
    summary = orchestrate.run_goal(gid, workers, max_seconds=int(a.seconds),
                                   max_decisions=int(a.decisions))
    print("STOP: {0} -- {1}".format(summary["stop"], summary.get("reason")))
    return summary


def _cmd_approve_map(a):
    if getattr(a, "from_proposed", False):
        proposed = (goals.state(a.goal_id).get("proposed_map") or {}).get("spec")
        if not proposed:
            raise SystemExit("no proposed interface map to approve; run autonomous to derive one")
        spec = proposed
    else:
        spec = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    fixture = bool(getattr(a, "fixture", False))
    rec = goals.approve_interface_map(a.goal_id, spec, a.approver, fixture=fixture)
    print("map approved sha256 {0} by {1}{2}".format(
        rec["sha256"], rec["approved_by"], " [FIXTURE, not a real approval]" if fixture else ""))
    return rec


def _cmd_approve_scope(a):
    import projectpkg
    package = Path(a.package).resolve()
    doc = projectpkg.approve_scope(package, a.approver, note=a.note or "")
    print("approved scoped.md sha256 {0} by {1}".format(doc["sha256"], doc["by"]))
    print("resume with:\n  python run/conductor.py start {0} --name {1} --package {2}".format(
        projectpkg.load_project(package)["project_root"],
        projectpkg.load_project(package)["name"], package))


def _cmd_status(a):
    print(goals.summary(a.goal_id))
    d = goals.disposition(a.goal_id)
    print(f"\ncomplete(): {goals.complete(a.goal_id)}   state: {d['state']}")


def _autonomous_plan(gid, package, plan_file=None):
    """The PLAN stop of one-entry autonomy: run the ORDINARY planner (intake done-when criteria +
    plan_with_recovery) to PROPOSE a plan for human approval. Returns None when a plan was proposed
    (the caller then stops for approval), or a stop dict when planning cannot proceed -- e.g. the
    intake done-when answers are not in yet, which is a human step, not a crash.

    With `plan_file` the proposal comes from that pre-written plan (issue #5) instead of the model."""
    import projectpkg
    name = projectpkg.load_project(package).get("name") or "project"
    st = goals.state(gid)
    page = (st.get("scope") or {}).get("text") or st.get("goal") or ""
    # The goal opened by autonomous_launch carries only the milestone criterion; the ordinary
    # planner plans the intake done-when criteria, so add them to the goal first (the milestone stays
    # separate, covered by the journey, not a packet).
    try:
        intake_criteria = projectpkg.criteria_from_intake(projectpkg.intake_state(package, name))
    except SystemExit as e:
        print("cannot plan yet: {0}".format(e))
        return {"stop": "AWAITING_INTAKE", "goal_id": gid, "reason": str(e)}
    have = {c["id"] for c in st.get("criteria") or []}
    for c in intake_criteria:
        if c["id"] not in have:
            goals.add_criterion(gid, c)
    # _plan_package's `root` is the PROJECT root (where declared sources resolve and the integration
    # tree is seeded from), not the fleet checkout.
    project_root = st.get("project_root") or projectpkg.load_project(package).get("project_root") or str(package)
    try:
        _plan_package(package, name, gid, page, Path(project_root), stage_integ=False,
                      plan_file=plan_file)
    except SystemExit as e:
        print("cannot plan yet: {0}".format(e))
        return {"stop": "AWAITING_INTAKE", "goal_id": gid, "reason": str(e)}
    if not goals.state(gid).get("proposed"):
        return {"stop": "BLOCKED", "goal_id": gid,
                "reason": "planning produced no admissible plan; see plan_rejections"}
    return None


def derive_map_spec(goal_id):
    """Derive a PROPOSED interface map from the approved (or proposed) plan: one component per
    consumer-facing assignment with its owned path and public call, `calls` edges taken from the
    dependency graph, and a first-milestone command that runs a real launcher when the project has
    one (else imports the top consumer). It is a PROPOSAL only -- a person approves the map (and the
    consequential command choice) via approve_interface_map. Returns a spec dict, or None."""
    doc = goals.state(goal_id)
    items = [(n, (a.get("contract") or {})) for n, a in (doc.get("assignments") or {}).items()]
    if not items:
        items = [(c.get("name"), c) for c in (doc.get("proposed") or [])]
    items = [(n, c) for n, c in items if n and (c.get("dest") or c.get("artifact"))]
    if not items:
        return None
    by_name = dict(items)

    def module_of(dest):
        return os.path.splitext(os.path.basename(dest or ""))[0]

    def _is_test(dest, name):
        b = os.path.basename(dest or "")
        return b.startswith("test_") or b.endswith("_test.py") or "test" in (name or "")

    _LAUNCHER_NAMES = {"launch.py", "launcher.py", "main.py", "app.py", "run.py", "play.py", "__main__.py"}

    def _is_launcher(dest, contract):
        # By the deliverable's own name, or by what it PROVIDES (an entry point / runnable main). NOT
        # by `consumer`: `consumer` names who calls THIS component, so a module consumed BY the
        # launcher (consumer == "launcher.py") is the opposite of being the launcher.
        if os.path.basename(dest or "") in _LAUNCHER_NAMES:
            return True
        prov = (contract.get("provides") or "").lower()
        base = os.path.basename(dest or "").lower()
        return ("entry point" in prov or "main()" in prov or "runnable" in prov
                or ("python " + base) in prov)

    # Runtime components of the playable slice: exclude test outcomes (they are not launched, they are
    # a separate later check), but keep them in the plan. A launcher component IS a runtime component
    # AND the milestone entry point.
    components, launcher_dest = [], None
    for n, c in items:
        dest = (c.get("dest") or c.get("artifact") or "").strip()
        if _is_test(dest, n):
            continue
        comp = {"id": n, "path": dest, "provides": (c.get("provides") or "").strip() or "the public call"}
        edges = []
        for dep in c.get("needs") or []:
            d = by_name.get(dep)
            ddest = (d or {}).get("dest") or (d or {}).get("artifact")
            if d and ddest:
                call = ((d.get("provides") or "").split("(")[0].strip() or "value")
                edges.append("{0}.{1}".format(module_of(ddest), call))
        if edges:
            # Represent EVERY dependency edge, not just the first: a launcher that needs both world
            # and engine must show both calls, or the approved map understates the wiring.
            comp["calls"] = edges if len(edges) > 1 else edges[0]
        components.append(comp)
        if _is_launcher(dest, c):
            launcher_dest = dest

    proot = Path(doc.get("project_root") or ".")
    baseline_launcher = next((cand for cand in ("launch.py", "main.py", "app.py", "run.py")
                              if (proot / cand).is_file()), None)
    if launcher_dest:
        command = [sys.executable, launcher_dest]           # a worker-authored launcher IS the milestone entry
    elif baseline_launcher:
        command = [sys.executable, baseline_launcher]
    else:
        consumers = [c for _n, c in items if c.get("needs")] or [items[-1][1]]
        command = [sys.executable, "-c", "import {0}".format(
            module_of(consumers[-1].get("dest") or consumers[-1].get("artifact")))]
    try:
        import proofloop
        requirement = proofloop.milestone_text(doc.get("scope", {}).get("text") or doc.get("goal") or "")
    except Exception:
        requirement = "the approved launched-and-working milestone"
    return {"components": components,
            "milestone": {"id": "slice", "command": command, "requirement": requirement}}


def autonomous_launch(package, *, decisions, seconds):
    """Bind the whole scoped.md, add the milestone, set autonomous mode. No workers."""
    import projectpkg
    import proofloop
    package = Path(package)
    scope = package / "scoped.md"
    if not scope.is_file():
        raise SystemExit("no scoped.md")
    if not projectpkg.approval_matches(package):
        raise SystemExit("scoped.md is not approved at its current bytes")
    page = scope.read_bytes().decode("utf-8")
    summary = proofloop.launch_summary("autonomous", decisions, seconds)
    print(proofloop.format_summary(summary))
    doc = projectpkg.load_project(package)
    gid = doc.get("goal_id")
    milestone = proofloop.milestone_text(page)
    if not gid:
        gid = goals.create(page, [{"id": "milestone", "text": milestone}],
                           project_root=doc.get("project_root") or str(package))
        doc["goal_id"] = gid
        projectpkg.save_project(package, doc)
    approval = json.loads((package / "approval.json").read_text(encoding="utf-8"))
    goals.bind_scope(gid, page, path=str(scope), approval_sha=approval["sha256"])
    goals.set_run_mode(gid, "autonomous", decisions=int(decisions), seconds=int(seconds))
    print("scope sha256 {0}".format(goals.state(gid)["scope"]["sha256"]))
    print("milestone is separate from the North Star; no packet was queued")
    return gid


_spark_runner = None


def ask_spark(journey, files):
    """Spark sees the journey transcript and the candidate files it was handed.

    A test sets `_spark_runner`. A live seat is used only when FLEET_SPARK_LIVE=1.
    Otherwise the finding is 'none found'. This function does not rule ACCEPT or REDO,
    and the caller does not invent the finding.
    """
    if _spark_runner is not None:
        return _spark_runner(journey, files)
    if os.environ.get("FLEET_SPARK_LIVE") != "1":
        return {"found": False, "text": "none found", "files": []}
    import call
    names = sorted(files)
    body = ["Journey transcript:", (journey or {}).get("transcript") or "(empty)", "",
            "Files you may cite: " + ", ".join(names), ""]
    for name in names:
        body.append("----- {0} -----".format(name))
        body.append(files[name][:4000])
    body.append("Name one concrete inconsistency, missing connection, or counterexample, "
                "or reply exactly: none found. Cite only a file listed above.")
    msg, _meta = call.chat(SKEPTIC_WORKER, [{"role": "user", "content": "\n".join(body)}],
                           max_tokens=400, timeout=90)
    text = (msg.get("content") or "").strip() or "none found"
    cited = [name for name in names if name in text]
    found = text.casefold() != "none found"
    return {"found": found, "text": text, "files": cited if found else []}


def resolve_repair_owner(spec, note, blobs):
    """Who owns the repair after a failed journey. The map's consumer (a component that `calls`
    another) is the party whose behavior the launch exercised; a Spark finding that cites a file
    narrows it. Returns (owner, reason). owner == "" means ambiguous or absent -- the caller PARKS a
    specific question rather than guessing a file to rewrite."""
    components = (spec or {}).get("components") or []
    paths = {(c.get("path") or "").strip() for c in components if c.get("path")}
    cited = [f for f in (note or {}).get("files") or [] if f in paths]
    if len(set(cited)) == 1:
        return cited[0], ""
    if len(set(cited)) > 1:
        return "", "the reviewer cited more than one file ({0})".format(", ".join(sorted(set(cited))))
    callers = [(c.get("path") or "").strip() for c in components if (c.get("calls") or "").strip()]
    callers = [c for c in callers if c]
    if len(set(callers)) == 1:
        return callers[0], ""
    if len(set(callers)) > 1:
        return "", "more than one component calls another ({0}); no single owner".format(
            ", ".join(sorted(set(callers))))
    only = [c.get("path") for c in components if c.get("path")]
    if len(only) == 1:
        return only[0], ""
    return "", "the interface map names no consumer and no single component to own the repair"


def build_repair_contract(goal_id, *, owner, spec, note, journey, blobs, candidate):
    """A repair the worker can actually execute: the exact failed command and output, the map row it
    owns, the accepted dependency bytes and the project launcher staged as sources, its own prior
    (broken) file carried read-only, and an oracle that RE-RUNS the frozen launch command -- it
    exercises the failed behavior without rewriting the requirement. Lineage (replaces/needs/dest)
    keeps it on the normal queue/receipt/acceptance/integration path."""
    import hashlib
    command = list((spec.get("milestone") or {}).get("command") or [])
    components = spec.get("components") or []
    row = next((c for c in components if (c.get("path") or "").strip() == owner), {})
    cand = Path(candidate)
    doc = goals.state(goal_id)
    proot = Path(doc.get("project_root") or candidate)
    # producing assignment per owned destination, so the repair can `needs` them and card_for stages
    # the ACCEPTED producer bytes (integrate receipts), not a stale project file.
    prod_by_dest = {}
    owner_provides = ""
    for n, a in (doc.get("assignments") or {}).items():
        c = a.get("contract") or {}
        d = (c.get("dest") or "").strip()
        if d == owner:
            owner_provides = (c.get("provides") or "").strip()
        if d and a.get("disposition") == "accepted":
            prod_by_dest[d] = n
    # Declare each dependency at its LOGICAL project-relative path (game/base.py), NOT an absolute
    # candidate path. card_for then keeps the package destination (`_reldest`) and substitutes the
    # accepted producer's bytes; `needs` records the producer so lineage/order are correct. This is
    # the fix for a candidate-tree source being flattened to a root-level module.
    sources, dep_names, needs, inputs = [], [], [], []
    for c in components:
        cp = (c.get("path") or "").strip()
        if not cp or cp == owner:
            continue
        sources.append(str(proot / cp))
        dep_names.append(cp)
        if cp in prod_by_dest:
            needs.append(prod_by_dest[cp])
    # ALL baseline material (launcher, package __init__ chain) is taken from the CANDIDATE, which is
    # assembled from the FROZEN baseline -- NEVER from the mutable project_root. So a post-approval
    # edit to project_root's launcher cannot reach the repair worker: it gets the same frozen launcher
    # the journey ran. Staged as inputs at their logical dest (card_for forwards contract inputs).
    def _add_input(rel):
        f = cand / rel
        if f.is_file() and rel != owner and not any(i["dest"] == rel for i in inputs):
            inputs.append({"name": Path(rel).name, "dest": rel, "location": str(f),
                           "sha256": hashlib.sha256(f.read_bytes()).hexdigest()})
    launcher_files = [tok for tok in command if isinstance(tok, str) and tok.endswith(".py") and tok != owner]
    for tok in launcher_files:
        _add_input(tok)
    # the package __init__ chain for the owner, the deps, and the launcher(s), from the frozen candidate
    for rel in [owner] + dep_names + launcher_files:
        parts = str(rel).replace("\\", "/").split("/")[:-1]
        for i in range(1, len(parts) + 1):
            _add_input("/".join(parts[:i]) + "/__init__.py")
    prior = cand / owner
    if prior.is_file():
        raw = prior.read_bytes()
        inputs.append({"name": Path(owner).name, "dest": "_carry/" + owner, "location": str(prior),
                       "sha256": hashlib.sha256(raw).hexdigest()})
    transcript = (journey.get("transcript") or "").strip()
    spark = (note or {}).get("text") if (note or {}).get("found") else ""
    brief = (
        "The approved launch milestone FAILED. Repair `{owner}` so the frozen launch command "
        "passes -- do NOT change the command, the map, or the requirement; make the real behavior "
        "satisfy it.\n\n"
        "Launch command: {cmd}\n"
        "Observed output:\n{out}\n\n"
        "Cross-project reviewer (Spark): {spark}\n\n"
        "Interface map row you own: provides {prov}; calls {calls}.\n"
        "Accepted dependencies staged in your workspace: {deps}.\n"
        "Your previous {owner} is preserved read-only at `_carry/{owner}`. With file tools, "
        "read that reference; without tools, use its contents supplied in the request. Fix it."
    ).format(owner=owner, cmd=" ".join(str(t) for t in command), out=transcript[-1500:] or "(no output)",
             spark=spark or "none", prov=(row.get("provides") or "?"), calls=(row.get("calls") or "-"),
             deps=", ".join(dep_names) or "(none)")
    oracle = (
        "import os, subprocess\n"
        "cmd = {cmd!r}\n"
        "r = subprocess.run(cmd, cwd=os.getcwd(), capture_output=True, text=True)\n"
        "assert r.returncode == 0, (r.stdout + r.stderr)[-800:]\n"
    ).format(cmd=command)
    name = "repair-" + hashlib.sha256((owner + "|" + transcript).encode("utf-8")).hexdigest()[:8]
    return {
        "name": name,
        "criterion_id": "milestone",
        "capability": "repair {0} so the approved launch command passes".format(owner),
        "brief": brief,
        "done_when": ["the approved launch command exits 0"],
        "decides_alone": ["the repair body of " + owner],
        "evidence": "the fresh launch command",
        "integrator": "handoff",
        "oracle": oracle,
        "oracle_covers_done_when": True,
        "tools": doc.get("tool_mode") == "tools",
        "artifact": owner,
        "dest": owner,
        "consumer": "the approved launch command",
        # keep the owner's EXISTING declared interface so a same-interface repair does not make the
        # approved map stale (see goals.map_stale / _assignment_signature).
        "provides": (owner_provides or row.get("provides") or "the call the launch command uses"),
        "require_consumer": True,
        "replaces": owner,
        "worker": None,
        "needs": needs,
        "sources": sources,
        "inputs": inputs,
    }


def queue_follow_up(goal_id, *, owner, evidence, source):
    """Validate and assign one repair. The ledger, not a returned dict, holds it."""
    import proofloop
    packet = proofloop.follow_up(goal_id, evidence=evidence, owner=owner, source=source)
    contract = {
        "name": packet["name"],
        "criterion_id": "milestone",
        "capability": "repair {0} so the approved launch command passes".format(owner),
        "brief": evidence[:800],
        "done_when": ["the launch command exits 0"],
        "decides_alone": ["the repair"],
        "evidence": "the fresh launch command",
        "integrator": "handoff",
        "oracle": "import pathlib\ntext = pathlib.Path({0!r}).read_text(encoding='utf-8')\nassert text.strip()\n".format(owner),
        "oracle_covers_done_when": False,
        "tools": goals.state(goal_id).get("tool_mode") == "tools",
        "artifact": owner,
        "dest": owner,
        "consumer": "launch.py",
        "provides": "the call the launch command uses",
        "require_consumer": True,
        "replaces": owner,
        "worker": None,
        "needs": [],
        "sources": [],
    }
    problems = consumer_contract_problems(contract)
    if problems:
        raise proofloop.ProofError("follow-up contract refused: {0}".format(", ".join(problems)))
    goals.assign(goal_id, [contract])
    goals.record_trace(goal_id, {
        "kind": "decision",
        "next_decision": "assign " + contract["name"],
        "owner": owner,
        "source": source,
        "evidence_sha256": packet.get("evidence", "")[:12],
    })
    return contract


def consume_milestone(goal_id, *, runs_root=None):
    """Assemble from worker receipts, run the launch command, then promote or assign one repair."""
    import proofloop
    doc = goals.state(goal_id)
    spec = (doc.get("interface_map") or {}).get("spec")
    if not spec:
        return {"done": False, "boundary": "no approved interface map"}
    stale, why = goals.map_stale(goal_id, doc=doc)
    if stale:
        return {"done": False, "boundary": "interface map approval is stale: {0}".format(why)}
    command = list((spec.get("milestone") or {}).get("command") or [])
    if len(command) < 2:
        return {"done": False, "boundary": "the milestone command is empty; a person must supply the "
                "launch command that runs the launcher or the owned modules"}
    try:
        blobs = proofloop.blobs_from_receipts(goal_id, runs_root)
    except Exception as exc:
        return {"done": False, "boundary": "receipts did not resolve: {0}".format(exc)}
    if not blobs:
        return {"done": False, "ready": False, "boundary": "no accepted receipts to assemble"}
    built = proofloop.assemble(goal_id, blobs)
    needed = [c.get("path") for c in (spec.get("components") or []) if c.get("path")]
    missing = [path for path in needed if path not in blobs]
    if missing:
        goals.record_trace(goal_id, {
            "kind": "candidate",
            "artifact_sha256": built["sha256"],
            "missing": missing,
            "next_decision": "wait for the remaining components",
            "attempt": 1,
        })
        return {"done": False, "ready": False, "candidate": built["candidate"],
                "boundary": "waiting on {0}".format(", ".join(missing))}
    journey = proofloop.run_journey(built["candidate"], command)
    finding = ask_spark(journey, {path: blobs[path].decode("utf-8", "replace") for path in blobs})
    note = proofloop.spark_note(journey, set(blobs), finding)
    import hashlib
    goals.record_trace(goal_id, {
        "kind": "journey",
        "ok": journey["ok"],
        "code": journey["code"],
        "command": command,
        "prompt_sha256": hashlib.sha256(repr(command).encode("utf-8")).hexdigest(),
        "reply": (journey.get("transcript") or "")[:800],
        "candidate": built["sha256"],
        "artifact_sha256": built["sha256"],
        "oracle": "exit {0}".format(journey["code"]),
        "spark": note.get("text"),
        "ruling": "promote" if journey["ok"] else "repair",
        "integration": "pending",
        "next_decision": "promote" if journey["ok"] else "assign repair",
        "skeptic": note.get("text"),
        "model": "spark" if note.get("found") else "none",
        "attempt": 1,
    })
    if journey["ok"]:
        # HONEST completion gate (fail-closed): the journey already passed on the REAL candidate;
        # grant DONE only if the command is also SENSITIVE to the delivered behaviour -- it must FAIL
        # against a mutant candidate whose deliverables still import but return wrong values. An
        # import-only command (`from x import value`) is 'independent'; a timeout is 'inconclusive';
        # both refuse DONE. The command runs once on the candidate (the journey) and once on the
        # isolated mutant -- the promotion below does NOT re-run it.
        status, detail = proofloop.milestone_completion(built["candidate"], command, list(blobs.keys()))
        if status != "sensitive":
            goals.record_trace(goal_id, {"kind": "journey", "ok": True, "completion": status,
                                         "next_decision": "refuse DONE ({0})".format(status)})
            return {"done": False, "candidate": built["candidate"], "completion": status,
                    "boundary": ("cannot make the project DONE: {0}. The journey passed but {1}. A "
                                 "person must supply a launch command that exercises the delivered "
                                 "behaviour.").format(status, detail)}
        promoted = proofloop.promote_checkpoint(
            goal_id, built["candidate"], spec["milestone"].get("id") or "slice", command,
            journey=journey)
        if not promoted.get("promoted"):
            return {"done": False, "boundary": promoted.get("boundary"),
                    "candidate": built["candidate"]}
        try:
            goals.record(goal_id, "milestone", "accepted", "journey:" + built["sha256"])
        except Exception:
            pass
        goals.record_trace(goal_id, {
            "kind": "decision",
            "integration": "promoted",
            "artifact_sha256": promoted.get("sha256") or built["sha256"],
            "next_decision": "done",
            "model": "none",
            "attempt": 1,
        })
        return {"done": True, "promoted": True, "sha256": promoted.get("sha256") or built["sha256"],
                "transcript": journey["transcript"], "candidate": built["candidate"]}
    cause = proofloop.first_failure([{
        "kind": "journey",
        "detail": (journey["transcript"] or "").strip() or "exit {0}".format(journey["code"]),
    }])
    owner, ambiguous = resolve_repair_owner(spec, note, blobs)
    if not owner:
        # An ambiguous or absent owner is a QUESTION for a person, not a guess. Park it with the
        # concrete reason and the evidence; the candidate and last checkpoint are untouched.
        question = ("the launch journey failed but the repair owner is unclear: {0}. Name the file "
                    "to repair, or amend the interface map.".format(ambiguous))
        try:
            goals.park(goal_id, "milestone", question)
        except Exception:
            pass
        goals.record_trace(goal_id, {
            "kind": "decision", "next_decision": "park owner question", "source": "journey",
            "owner": "", "spark": note.get("text"), "reason": ambiguous,
        })
        return {"done": False, "boundary": "journey failed; repair owner ambiguous (parked)",
                "cause": cause["kind"], "parked": question, "follow_up": None,
                "candidate": built["candidate"], "transcript": journey["transcript"]}
    # Admitting a repair is an architect decision and spends the durable budget. When admissions are
    # closed (no decisions left), do NOT admit a new repair -- but the candidate and any in-flight
    # work are preserved, never killed.
    rem = goals.budget_remaining(goal_id)
    _decisions_gone = rem.get("decisions") is not None and rem["decisions"] <= 0
    _repairs_gone = rem.get("repairs") is not None and rem["repairs"] <= 0
    if _decisions_gone or _repairs_gone:
        which = "repair" if _repairs_gone else "decision"
        goals.record_trace(goal_id, {"kind": "decision", "next_decision": "hold repair (budget)",
                                     "owner": owner, "source": "journey", "limit": which})
        return {"done": False, "boundary": "journey failed; {0} budget exhausted, candidate preserved".format(which),
                "cause": cause["kind"], "follow_up": None, "candidate": built["candidate"],
                "transcript": journey["transcript"]}
    contract = build_repair_contract(goal_id, owner=owner, spec=spec, note=note,
                                     journey=journey, blobs=blobs, candidate=built["candidate"])
    problems = consumer_contract_problems(contract)
    if problems:
        raise proofloop.ProofError("repair contract refused: {0}".format(", ".join(problems)))
    goals.assign(goal_id, [contract])
    goals.record_spend(goal_id, decisions=1, repairs=1)
    goals.record_trace(goal_id, {
        "kind": "decision", "next_decision": "assign " + contract["name"], "owner": owner,
        "source": "journey", "replaces": owner, "spark": note.get("text"),
        "evidence_sha256": hashlib.sha256((journey.get("transcript") or "").encode("utf-8")).hexdigest()[:12],
    })
    return {"done": False, "boundary": "journey failed; candidate preserved",
            "cause": cause["kind"], "follow_up": contract, "candidate": built["candidate"],
            "transcript": journey["transcript"]}


def consumer_contract_problems(contract):
    """A packet must name the consumer, the call, and the one path it owns."""
    problems = []
    if not (contract.get("consumer") or "").strip():
        problems.append("no consumer")
    if not (contract.get("dest") or "").strip():
        problems.append("no owned path")
    provides = contract.get("provides") or ""
    if not provides.strip():
        problems.append("no callable behavior")
    return problems


def harness_fingerprint(root=None):
    """Issue #9: a stable hash of the controller source, so a live/evaluation run can tell whether
    the harness was edited under it (a second session editing this same checkout mid-run). Hashes the
    .py files under `root` (default: the run/ package this module lives in), sorted, excluding caches.
    Two sessions should each use an isolated checkout; when they do not, this makes drift detectable
    instead of silent."""
    import hashlib
    base = Path(root) if root else Path(__file__).resolve().parent
    h = hashlib.sha256()
    for p in sorted(base.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        h.update(p.relative_to(base).as_posix().encode("utf-8") + b"\0")
        h.update(p.read_bytes() + b"\0")
    return h.hexdigest()


def _cmd_fingerprint(a):
    fp = harness_fingerprint()
    print(fp)
    expect = getattr(a, "expect", None)
    if expect and expect != fp:
        raise SystemExit("HARNESS CHANGED since {0}...; the controller source was edited. Use an "
                         "isolated checkout (a separate worktree) for a live run, and do not edit "
                         "the harness while a run is in flight.".format(expect[:12]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start", help="first-use entry: record a folder and resume the project package")
    p.add_argument("folder", help="the project folder to observe and bind as project_root")
    p.add_argument("--name", required=True, help="project name; one path segment")
    p.add_argument("--package", default=None,
                   help="package directory (default: intake/packages/<name>/ under the fleet root)")
    p.add_argument("--project-kind", choices=("local", "git-existing", "git-new"), default=None,
                   help="record local/Git intent separately from the project path; git-new runs local git init only")
    p.add_argument("--intake-mode", choices=("guided", "brief", "defer"), default=None,
                   help="explicit first-use choice; omit on an existing package to keep its saved mode")
    p.add_argument("--brief", default=None, help="UTF-8 existing brief, stored as unconfirmed context")
    p.add_argument("--draft", action="store_true", help="allow one model call for proposed intake drafts")
    p.add_argument("--tool-mode", choices=("chat-only", "tools"), default=None,
                   help="persist worker execution authority; tools enables workspace code execution")
    p.set_defaults(fn=_cmd_start)

    p = sub.add_parser("continue", help="resume an existing package at the first incomplete step")
    p.add_argument("package")
    p.set_defaults(fn=_cmd_continue)

    p = sub.add_parser("assist", help="record agent drafts; they are not answers")
    p.add_argument("package")
    p.add_argument("--drafts", default=None, help="JSON object of question id to proposed text")
    p.set_defaults(fn=_cmd_assist)

    p = sub.add_parser("confirm", help="accept one proposed draft as the user's answer")
    p.add_argument("package")
    p.add_argument("qid")
    p.add_argument("--as", dest="approver", required=True)
    p.set_defaults(fn=_cmd_confirm)

    p = sub.add_parser("scope", help="write scoped.md from the intake and observed folder; never overwrites")
    p.add_argument("package")
    p.set_defaults(fn=_cmd_scope)

    p = sub.add_parser("approve-scope", help="approve the exact scoped.md bytes")
    p.add_argument("package")
    p.add_argument("--as", dest="approver", required=True)
    p.add_argument("--note", default="")
    p.set_defaults(fn=_cmd_approve_scope)

    p = sub.add_parser("plan", help="open a goal and propose a plan for approval")
    p.add_argument("--tool-mode", choices=("chat-only", "tools"), default=None,
                   help="execution authority for new contracts; omission preserves legacy behavior")
    p.add_argument("goal")
    p.add_argument("--criterion", action="append", required=True,
                   help="an acceptance criterion; repeat. These are what completion MEANS.")
    p.add_argument("--budget", type=int, default=0, help="max assignments before the loop stops")
    p.add_argument("--plan-file", help="read the outcome JSON from a file instead of a model")
    p.add_argument("--project-root", default=None, help="the project the work lives in; declared relative sources resolve here")
    p.add_argument("--integration", default=None,
                   help="JSON file: groups, destinations, frozen check_source (or use --integration-check)")
    p.add_argument("--integration-check", default=None,
                   help="frozen integration check source; persisted with the approved goal")
    p.set_defaults(fn=_cmd_plan)

    p = sub.add_parser("autonomous", help="bind scoped.md and run the autonomous loop under the recorded budgets")
    p.add_argument("--tool-mode", choices=("chat-only", "tools"), default=None,
                   help="select before planning, then retain the saved policy; cannot alter approved work")
    p.add_argument("package")
    p.add_argument("--decisions", type=int, required=True)
    p.add_argument("--seconds", type=int, required=True)
    p.add_argument("--workers", default=DEFAULT_WORKER)
    p.add_argument("--map", default=None, help="interface map JSON; requires --as")
    p.add_argument("--plan-file", default=None, metavar="PATH",
                   help="propose this pre-written plan (JSON with an 'outcomes' list) instead of "
                        "calling the model planner; it is still gated, human-approved and mapped.")
    p.add_argument("--delegate", default=None, metavar="NAME",
                   help="run unattended (overnight): pre-approve the scope, plan and derived map as "
                        "NAME's standing approval. Budget, never-rules and consequential sign-offs still apply.")
    p.add_argument("--skip-preflight", action="store_true",
                   help="do not verify the fleet before dispatch (default: preflight the run's "
                        "coding lanes, the skeptic and the tool-service, and refuse on failure).")
    p.add_argument("--as", dest="approver", default="")
    p.set_defaults(fn=_cmd_autonomous)

    p = sub.add_parser("approve-map", help="a person approves one interface map")
    p.add_argument("goal_id")
    p.add_argument("--spec", default=None, help="JSON file: components, calls, milestone command")
    p.add_argument("--from-proposed", dest="from_proposed", action="store_true",
                   help="approve the map autonomous derived from the approved plan")
    p.add_argument("--fixture", action="store_true",
                   help="mark this as a TEST-FIXTURE approval (never a real human approval)")
    p.add_argument("--as", dest="approver", required=True)
    p.set_defaults(fn=_cmd_approve_map)

    p = sub.add_parser("approve", help="a human approves the goal and the proposed plan")
    p.add_argument("goal_id")
    p.add_argument("--as", dest="approver", required=True)
    p.set_defaults(fn=_cmd_approve)

    p = sub.add_parser("reject-plan",
                       help="a human rejects the proposed plan; keeps the goal+scope, clears the "
                            "proposal, and re-enters the planner on the next start")
    p.add_argument("goal_id")
    p.add_argument("--reason", required=True, help="why the plan is being sent back")
    p.add_argument("--as", dest="approver", required=True)
    p.set_defaults(fn=_cmd_reject_plan)

    p = sub.add_parser("advance", help="keep choosing useful work until blocked or out of budget")
    p.add_argument("goal_id")
    p.add_argument("--capacity", type=int, default=1)
    p.add_argument("--max-assignments", type=int, default=None)
    p.add_argument("--max-rounds", type=int, default=5)
    p.add_argument("--simulate", default="accepted",
                   help="outcome every simulated card returns (default: accepted)")
    p.add_argument("--live", action="store_true",
                   help="dispatch through the queue and the architect decision step")
    p.add_argument("--workers", default=DEFAULT_WORKER,
                   help="comma-separated worker names for live execution")
    p.add_argument("--max-seconds", type=int, default=3600)
    p.add_argument("--max-decisions", type=int, default=3,
                   help="architect decisions allowed per live run")
    p.add_argument("--no-decisions", action="store_true",
                   help="plain batch runner: no architect decisions (comparison baseline)")
    p.add_argument("--no-skeptic", action="store_true")
    p.add_argument("--deliverable-root", default=None,
                   help="directory holding the goal's deliverables; the harness reads their "
                        "public API for itself instead of asking anyone to look")
    p.set_defaults(fn=_cmd_advance)

    p = sub.add_parser("resolve", help="answer a parked question so work can resume")
    p.add_argument("goal_id")
    p.add_argument("--answer", required=True)
    p.add_argument("--by", required=True, help="who is answering; an anonymous answer is not one")
    p.add_argument("--criterion", default=None, help="limit to one criterion's questions")
    p.set_defaults(fn=_cmd_resolve)

    p = sub.add_parser("sign-off", help="a person accepts a criterion no machine can check")
    p.add_argument("goal_id")
    p.add_argument("criterion")
    p.add_argument("--by", required=True)
    p.add_argument("--note", default="")
    p.set_defaults(fn=_cmd_sign_off)

    p = sub.add_parser("status", help="where a goal actually stands")
    p.add_argument("goal_id")
    p.set_defaults(fn=_cmd_status)

    p = sub.add_parser("list", help="print goal ids")
    p.set_defaults(fn=lambda a: print("\n".join(goals.list_goals()) or "(no goals)"))

    # --- retained experience (LF-01/02) and preserved approaches (LF-03/04) ------------------
    p = sub.add_parser("memory", help="lessons and skills the fleet retained from earlier work")
    p.add_argument("action", choices=["list", "match", "withdraw", "add-lesson", "contradict",
                                      "skill-result", "revalidate", "withdraw-skill",
                                      "legacy-disposition", "refresh-contract", "reassess"])
    p.add_argument("--evidence-ref", default="", help="reassess: evidence that the condition holds again")
    p.add_argument("--narrow", action="append", default=[], help="reassess: replace applicability tags")
    p.add_argument("--ok", action="store_true", help="skill-result: the skill's own check passed")
    p.add_argument("--failed", action="store_true", help="skill-result: the skill's own check failed")
    p.add_argument("--scope", default="operator verification",
                   help="skill-result: what was actually checked, and where")
    p.add_argument("--note", default="")
    p.add_argument("--unverified", action="store_true",
                   help="add-lesson: store although the source path does not exist (never offered)")
    p.add_argument("--brief", default="", help="match: text of the work at hand")
    p.add_argument("--id", action="append", default=[], help="withdraw/contradict: entry id(s)")
    p.add_argument("--text", default="", help="add-lesson: the lesson")
    p.add_argument("--source", default="", help="add-lesson: the evidence it rests on")
    p.add_argument("--limits", default="", help="add-lesson: what the evidence does not show")
    p.add_argument("--reason", default="", help="withdraw/contradict: why")
    p.add_argument("--by", default="", help="who is acting")
    p.set_defaults(fn=_cmd_memory)

    p = sub.add_parser("alternatives", help="every approach tried or preserved for a criterion")
    p.add_argument("goal_id")
    p.add_argument("criterion")
    p.set_defaults(fn=lambda a: print(json.dumps(goals.alternatives(a.goal_id, a.criterion),
                                                 indent=2)))

    p = sub.add_parser("revisit", help="re-open a preserved approach under the same acceptance")
    p.add_argument("goal_id")
    p.add_argument("assignment")
    p.add_argument("--why", required=True, help="the new evidence that makes it relevant")
    p.set_defaults(fn=lambda a: print("reopened as " + goals.revisit(a.goal_id, a.assignment,
                                                                     why=a.why)))

    p = sub.add_parser("workers", help="what each worker's record says about a kind of work")
    p.add_argument("--brief", default="", help="the work in question (its capability / criterion text)")
    p.set_defaults(fn=_cmd_workers)

    p = sub.add_parser("proposals", help="what workers proposed on a goal, and how each was resolved")
    p.add_argument("goal_id")
    p.set_defaults(fn=lambda a: print(json.dumps(goals.state(a.goal_id).get("proposals") or [], indent=2)))

    p = sub.add_parser("proposal", help="resolve a worker proposal: accept into work, defer, or reject")
    p.add_argument("goal_id")
    p.add_argument("proposal_id")
    p.add_argument("--resolution", choices=["accept", "defer", "reject"], required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--by", default="operator")
    p.add_argument("--criterion", default=None, help="accept: the unmet criterion it advances")
    p.add_argument("--name", default=None, help="accept: kebab-case assignment name")
    p.add_argument("--brief", default=None, help="accept: what the worker builds")
    p.add_argument("--artifact", default=None, help="accept: the module file the worker delivers (<name>.py)")
    p.add_argument("--done-when", action="append", default=[], help="accept: mechanical check (repeatable)")
    p.add_argument("--oracle", default=None, help="accept: python that imports the module and asserts the checks")
    p.add_argument("--reconsider", action="append", default=[],
                   help="defer: condition to look again, e.g. criterion_met:c2 file_present:x.py (repeatable)")
    p.set_defaults(fn=_cmd_proposal)

    p = sub.add_parser("challenge", help="doubt an ACCEPTED criterion: receipt untouched, question opened, goal not complete while open")
    p.add_argument("goal_id")
    p.add_argument("criterion")
    p.add_argument("--kind", choices=list(goals.CHALLENGE_KINDS), required=True)
    p.add_argument("--basis", required=True, help="what you saw that the acceptance may not cover")
    p.add_argument("--evidence-ref", default="")
    p.add_argument("--by", default="operator")
    p.add_argument("--assist", action="store_true", help="run the skeptic on the accepted artifact (needs a worker)")
    p.set_defaults(fn=_cmd_challenge)

    p = sub.add_parser("challenges", help="challenges recorded on a goal and how each ended")
    p.add_argument("goal_id")
    p.set_defaults(fn=lambda a: print(json.dumps(goals.state(a.goal_id).get("challenges") or [], indent=2)))

    p = sub.add_parser("challenge-resolve", help="established | refuted | uncertain, on evidence")
    p.add_argument("goal_id")
    p.add_argument("id")
    p.add_argument("--outcome", choices=list(goals.CHALLENGE_OUTCOMES), required=True)
    p.add_argument("--why", required=True)
    p.add_argument("--evidence-ref", default="")
    p.add_argument("--reconsider", action="append", default=[], help="uncertain: conditions to look again")
    p.add_argument("--by", default="operator")
    p.set_defaults(fn=_cmd_challenge_resolve)

    p = sub.add_parser("reengage", help="deferred items whose stated condition now holds (surfaced once per change)")
    p.add_argument("goal_id")
    p.add_argument("--root", default=None, help="project root for file_present conditions")
    p.set_defaults(fn=_cmd_reengage)

    p = sub.add_parser("questions", help="open and resolved questions kept with a goal")
    p.add_argument("goal_id")
    p.set_defaults(fn=lambda a: print(json.dumps(goals.state(a.goal_id).get("questions") or [], indent=2)))

    p = sub.add_parser("question", help="open or resolve a persistent question")
    p.add_argument("goal_id")
    p.add_argument("action", choices=["add", "resolve", "reconsider"])
    p.add_argument("--id", default=None)
    p.add_argument("--reconsider", action="append", default=[],
                   help="condition under which to look again, e.g. criterion_met:c2 (repeatable)")
    p.add_argument("--text", default="")
    p.add_argument("--criterion", default=None)
    p.add_argument("--explanation", action="append", default=[], help="competing explanation (repeatable)")
    p.add_argument("--distinguishing", default="", help="what observation would tell them apart")
    p.add_argument("--why", default="", help="resolve: why the evidence supports the chosen explanation")
    p.add_argument("--chosen", type=int, default=None, help="resolve: index of the supported explanation")
    p.add_argument("--evidence-ref", default="", help="resolve: evidence reference if no observation is attached")
    p.add_argument("--by", default="operator")
    p.set_defaults(fn=_cmd_question)

    p = sub.add_parser("observe", help="record an authorized read-only observation for a criterion")
    p.add_argument("goal_id")
    p.add_argument("criterion")
    p.add_argument("--root", required=True, help="the only directory the probe may read")
    p.add_argument("--tool", choices=["list", "read", "grep"], required=True)
    p.add_argument("--target", default="")
    p.add_argument("--pattern", default="")
    p.add_argument("--question", required=True)
    p.add_argument("--question-id", default=None, help="attach the observation to a persistent question")
    p.add_argument("--by", default="operator")
    p.set_defaults(fn=_cmd_observe)

    p = sub.add_parser("fingerprint",
                       help="print a hash of the controller source (issue #9: detect a harness edited "
                            "under a live run). --expect HASH exits non-zero if it changed.")
    p.add_argument("--expect", default=None, help="exit non-zero if the current hash differs from this")
    p.set_defaults(fn=_cmd_fingerprint)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    main()
