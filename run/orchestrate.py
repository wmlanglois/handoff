"""Integration seam: an approved GOAL becomes work that runs through the REAL execution path.

Architect-owned. The three tracks landed independently and correct in isolation, and the gap
between them was the whole point of this file:

  * Track C's conductor dispatched through its own executor seam, straight to run/verified.py. That
    bypasses the claim, the lease, and the executor-start check -- exactly the safety controls
    Track A had just finished proving. Goal work must not take a side door around them.
  * Track C refused every card whose worker is None, on the stale premise that capability selection
    "is not available in this checkout". It landed in Track A while C was writing that comment.
  * The queue's index row carries {name, worker, outcome, sec, ts}, so goal_id and criterion_id
    survive INTO a card and are dropped on the way out. Rather than widen another owner's contract
    mid-flight, this reads the CARD BACK from the directory the queue filed it in: the card still
    carries its own goal_id and criterion_id, and the directory IS the outcome.

So: goals.next_work -> queue.add -> queue.drain (claims, leases, evidence, outcome routing) ->
read the filed cards -> goals.record. The goal ledger learns only what the execution path proved.

TERMINATION. The loop stops for an evidenced reason and says which:
  DONE      every criterion met, with evidence
  BLOCKED   nothing is dispatchable and criteria remain open -- NOT completion
  BUDGET    wall-clock exhausted, state left resumable
An empty queue is never by itself a finished goal. That conflation is the failure this whole
project exists to prevent, and it would arrive here first.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "run"))
sys.path.insert(0, str(ROOT / "check"))
from safeio import force_utf8  # noqa: E402
force_utf8()

import goals                      # noqa: E402  Track C
import decide                     # noqa: E402  M1 architect decision step
import steer                      # noqa: E402  purpose-aware selection
import recovery                   # noqa: E402  health + authorized restart
import queue as fleet_queue       # noqa: E402  Track A  (shadows stdlib queue; run/ is on sys.path)
import watchdog                   # noqa: E402
from fleet import DEFAULT_WORKER  # noqa: E402
from fleet import runs_root as fleet_runs_root  # noqa: E402  (one runs root for writer+readers)

DONE, BLOCKED, BUDGET = "DONE", "BLOCKED", "BUDGET"

# Where the queue files a card tells us what happened to it. This mirrors queue.OUTCOME_DIR, which
# is Track A's closed vocabulary (INTERFACES.md I3); we read it rather than restate it.
TERMINAL_DIRS = ("done", "review", "parked", "failed")


def _runs_root():
    return fleet_runs_root()


def _cards_dir(root=None):
    d = (Path(root) if root else _runs_root()) / "goal-cards"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stage(cards, root=None):
    """Write each card to disk and hand the paths to the queue."""
    out = []
    d = _cards_dir(root)
    for card in cards:
        p = d / (card["name"] + ".json")
        p.write_text(json.dumps(card, indent=2), encoding="utf-8")
        out.append(str(p))
        if card.get("origin") == "integration-repair":
            import integrate
            integrate.stage_repair_inputs(card)
    fleet_queue.add(out)
    return out


def _clear_prior(names):
    """Remove earlier terminal copies of these cards before re-dispatching them.

    DEFECT, caught before it produced a wrong answer: a card re-run after a previous attempt has a
    stale copy sitting in whichever directory that attempt landed in. _collect scans the terminal
    directories in a fixed order, so an old `review/` copy would overwrite a fresh `done/` result
    and the ledger would record awaiting-review for a run that was actually accepted. Reading a
    directory as an outcome only works if the directory holds THIS attempt."""
    removed = []
    for state in TERMINAL_DIRS:
        for p in fleet_queue.DIRS[state].glob("*.json"):
            if p.stem in names:
                try:
                    p.unlink()
                    removed.append("{0}/{1}".format(state, p.name))
                except OSError:
                    pass
    return removed


def _collect(names, since=0.0):
    """Read each card back from wherever the queue filed it. Newest wins.

    `since` guards the same failure from the other side: a file older than this dispatch is not
    evidence about it."""
    found = {}
    for state in TERMINAL_DIRS:
        for p in fleet_queue.DIRS[state].glob("*.json"):
            if p.stem not in names:
                continue
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if mt < since:
                continue
            if p.stem in found and found[p.stem][0] >= mt:
                continue
            try:
                card = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                card = {}
            found[p.stem] = (mt, state, card)
    return {k: (v[1], v[2]) for k, v in found.items()}


def _seconds_for(name):
    """Wall-clock the queue recorded for this run id, so the ledger's effort account is real."""
    p = _runs_root() / "queue" / "index.jsonl"
    total = 0.0
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("name") == name:
                total += float(row.get("sec") or 0)
    except OSError:
        pass
    return total


def _evidence_ref(name):
    """Point the ledger at something a person can open, not at a claim that it went well."""
    for candidate in (_runs_root() / ("verified-" + name) / "receipt.md",
                      _runs_root() / ("verified-" + name) / "result.json"):
        if candidate.exists():
            return str(candidate)
    return ""


# The queue folds four distinct parked outcomes into one directory. The directory is therefore
# enough to know a job did NOT succeed, and not enough to say why -- and the goal ledger rejects an
# imprecise outcome by design, because "parked" is not a member of the closed I3 vocabulary. The
# precise outcome is in the run's own result.json, which run/verified.py writes before exiting.
_DIR_FALLBACK = {"done": "accepted", "review": "awaiting-review",
                 "parked": "parked-stagnant", "failed": "worker-unavailable"}


def _outcome_for(state, card, name=None):
    """Prefer the run's DECLARED outcome; fall back to the directory only if it is unreadable."""
    if name:
        try:
            rec = json.loads((_runs_root() / ("verified-" + name) / "result.json")
                             .read_text(encoding="utf-8"))
            declared = (rec.get("outcome") or "").strip()
            if declared:
                return declared
        except (OSError, ValueError):
            pass
    return _DIR_FALLBACK.get(state, state)


def healthy_workers(workers, *, recover=True, notes=None, sleep=time.sleep):
    """Which workers can actually generate right now, ATTEMPTING authorized recovery first.

    This used to probe once and drop whatever was down, so an unavailable worker ended the night.
    The machinery to bring one back already existed; nothing connected it to goal execution.

    What it will not do: restart anything the configuration has not put under this harness's
    ownership (the default is EXTERNAL), and restart a worker that is merely still LOADING, which
    would turn a wait into an outage. Both come back as an explicit condition plus the action a
    person would have to take, rather than a silent skip."""
    notes = notes if notes is not None else []
    ok, down = [], []
    for w in workers:
        try:
            usable, status, _d = watchdog.usable(w)
        except Exception:
            usable, status = False, "PROBE-ERROR"
        (ok if usable else down).append(w if usable else (w, status))

    for w, status in down:
        if not recover:
            print("  SKIP {0}: {1}".format(w, status)); continue
        pol = recovery.policy(w)
        if pol["management"] == recovery.EXTERNAL:
            # Observe-only. Say so once, with the action, instead of retrying something forbidden.
            msg = recovery.required_next_action([{ "worker": w, "result": "refused",
                                                   "management": recovery.EXTERNAL}])
            print("  DOWN {0}: {1} -- {2}".format(w, status, msg))
            notes.append(msg)
            continue
        print("  DOWN {0}: {1} -- attempting authorized recovery".format(w, status))
        recovered, rows = recovery.recover_with_retries(w, sleep=sleep)
        if recovered:
            print("  RECOVERED {0}: inference verified".format(w))
            notes.append("recovered {0} after {1} attempt(s)".format(w, len(rows)))
            ok.append(w)
        else:
            action = recovery.required_next_action(rows)
            print("  UNRECOVERED {0}: {1}".format(w, action))
            notes.append(action)
    return ok


def _harvest_proposals(goal_id, display, run_id, evidence_ref):
    """Read the run's recorded proposals (verified.py extract_proposals) into the ledger."""
    try:
        rec = json.loads((fleet_runs_root() / ("verified-" + run_id) / "result.json")
                         .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    props = [pp for pp in (rec.get("proposals") or []) if pp.get("kind") != "malformed"]
    if not props:
        return []
    return goals.add_proposals(goal_id, display, props, run_id=run_id, evidence_ref=evidence_ref)


def _retain(name, card, outcome, criterion_id, goal_id):
    """Fold one landed run into the experience store. Returns the ids it created."""
    import memory
    tags = memory.tags_for((card.get("brief") or "")[:600])
    crit_text = ""
    for c in goals.state(goal_id)["criteria"]:
        if c["id"] == criterion_id:
            crit_text = c["text"]
    tags = sorted(set(tags) | set(memory.tags_for(crit_text)))
    for eid in card.get("experience") or []:
        memory.note_outcome(eid, outcome)      # the offer's track record, not a verdict on it
    made = [r["id"] for r in memory.extract_lessons_from_run(name, family_tags=tags)]
    if outcome == "accepted":
        best = ""
        try:
            rec = json.loads((fleet_runs_root() / ("verified-" + name) / "result.json")
                             .read_text(encoding="utf-8"))
            best = str(rec.get("best_artifact") or "")
        except (OSError, ValueError):
            pass
        art = card.get("artifact") or "output.md"
        sk = memory.register_skill_from_run(name, str(fleet_runs_root() / ("verified-" + name) / art),
                                            card)
        if sk:
            made.append(sk["id"])
        elif memory.register_skill_from_run.last_reason:
            print("   skill not registered: " + memory.register_skill_from_run.last_reason,
                  flush=True)
    return made


def _failed_names(goal_id):
    """Assignments whose last outcome was not an acceptance -- the ones worth explaining."""
    st = goals.state(goal_id)
    out = []
    for name, a in st["assignments"].items():
        outs = [o.get("outcome") for o in (a.get("outcomes") or [])]
        if outs and outs[-1] != "accepted":
            out.append(name)
    return out[:4]


def _autonomous_after_wave(goal_id, results):
    """After accepted receipts land, assemble a candidate and run the launch command.

    A wave that has not yet produced every component keeps the partial candidate and
    returns None so the next packet can run. A journey failure assigns one repair.
    """
    st = goals.state(goal_id)
    if st.get("run_mode") != "autonomous":
        return None
    if not (st.get("interface_map") or {}).get("spec"):
        return None
    if not any(r.get("outcome") == "accepted" for r in results or []):
        return None
    import conductor
    result = conductor.consume_milestone(goal_id)
    if result.get("ready") is False:
        print("   candidate held: {0}".format(result.get("boundary")), flush=True)
        return None
    if not result.get("done"):
        return {"stop": BLOCKED, "reason": result.get("boundary") or "journey failed",
                "follow_up": result.get("follow_up"), "cause": result.get("cause"),
                "passes": [], "interventions": [], "elapsed_s": 0}
    if goals.complete(goal_id):
        return {"stop": DONE, "reason": "launch journey passed; checkpoint promoted",
                "checkpoint": result.get("sha256"), "passes": [], "interventions": [],
                "elapsed_s": 0}
    return None


def run_goal(goal_id, workers, max_seconds=3600, max_rounds=4, poll_cards=8,
             max_decisions=3, use_skeptic=True, deliverable_root=None):
    """Durable-budget wrapper around the driver: an autonomous goal resumes under the REMAINING
    budget and accumulates the time it spends, so a repeated `autonomous` invocation never restarts
    the wall clock. Decisions and repair admissions are recorded where they are taken."""
    autonomous = goals.state(goal_id).get("run_mode") == "autonomous"
    if autonomous:
        # CRASH-DURABLE, CONSERVATIVE metering. If a prior run left an uncleared active marker (it
        # crashed mid-call), charge the whole wall gap since it went active BEFORE starting -- never
        # under-charge. Then mark this run active; the heartbeat advances the marker as it commits
        # spend, and the finally below charges the residual and clears it on a clean exit.
        now = time.time()
        prev = goals.run_active_since(goal_id)
        if prev is not None:
            goals.record_spend(goal_id, seconds=max(0, int(now - prev)))
        goals.mark_run_active(goal_id, now)
    try:
        summary = _run_goal_impl(goal_id, workers, max_seconds=max_seconds, max_rounds=max_rounds,
                                 poll_cards=poll_cards, max_decisions=max_decisions,
                                 use_skeptic=use_skeptic, deliverable_root=deliverable_root)
    finally:
        if autonomous:
            # Charge the residual since the last commit and clear the marker on a clean exit. If the
            # ledger write itself fails, the loop already surfaced that (a BUDGET 'unmetered' stop);
            # leave the active marker in place so the NEXT start reconciles conservatively.
            try:
                marker = goals.run_active_since(goal_id)
                if marker is not None:
                    goals.record_spend(goal_id, seconds=max(0, int(time.time() - marker)))
                goals.clear_run_active(goal_id)
            except Exception:
                pass
    if autonomous:
        try:
            summary.setdefault("remaining_budget", goals.budget_remaining(goal_id))
        except Exception:
            pass
    return summary


def _run_goal_impl(goal_id, workers, max_seconds=3600, max_rounds=4, poll_cards=8,
                   max_decisions=3, use_skeptic=True, deliverable_root=None):
    """Drive one approved goal to an evidenced stop. Returns a summary dict."""
    st0 = goals.state(goal_id)
    if not deliverable_root:
        deliverable_root = st0.get("project_root") or None
    if st0.get("run_mode") == "autonomous":
        # DURABLE budgets: resume under what REMAINS after prior invocations, not the full budget.
        budget = st0.get("run_budget") or {}
        spent = st0.get("run_spent") or {}
        rem_seconds = rem_decisions = None
        if budget.get("seconds") is not None:
            rem_seconds = max(0, int(budget["seconds"]) - int(spent.get("seconds", 0)))
            max_seconds = min(int(max_seconds), rem_seconds)
        if budget.get("decisions") is not None:
            rem_decisions = max(0, int(budget["decisions"]) - int(spent.get("decisions", 0)))
            max_decisions = min(int(max_decisions), rem_decisions)
        if rem_seconds is not None and max_seconds <= 0:
            return {"stop": BUDGET,
                    "reason": "time budget exhausted ({0}s of {1}s spent across invocations)".format(
                        int(spent.get("seconds", 0)), int(budget.get("seconds", 0))),
                    "remaining_budget": {"seconds": 0, "decisions": rem_decisions},
                    "passes": [], "interventions": [], "elapsed_s": 0}
    if st0.get("run_mode") == "autonomous" and not (st0.get("interface_map") or {}).get("spec"):
        return {"stop": BLOCKED,
                "reason": "autonomous mode has no approved interface map; packet checks are not the project",
                "passes": [], "interventions": [], "elapsed_s": 0}
    if st0.get("run_mode") == "autonomous":
        _stale, _why = goals.map_stale(goal_id, doc=st0)
        if _stale:
            return {"stop": BLOCKED, "reason": "interface map approval is stale: " + _why,
                    "passes": [], "interventions": [], "elapsed_s": 0}
    pending = any(a.get("status") == "ready" for a in (st0.get("assignments") or {}).values())
    if (st0.get("run_mode") == "autonomous" and goals.criteria_met(goal_id, skip=("milestone",))
            and not (st0.get("journey") or {}).get("passed") and not pending):
        import conductor
        result = conductor.consume_milestone(goal_id)
        if not result.get("done"):
            return {"stop": BLOCKED, "reason": result.get("boundary") or "journey failed",
                    "follow_up": result.get("follow_up"), "cause": result.get("cause"),
                    "passes": [], "interventions": [], "elapsed_s": 0}
        if goals.complete(goal_id):
            return {"stop": DONE, "reason": "launch journey passed; checkpoint promoted",
                    "checkpoint": result.get("sha256"), "passes": [], "interventions": [],
                    "elapsed_s": 0}
    t0 = time.time()
    interventions = []          # anything a human or the architect had to do mid-run
    passes = []
    decisions = []              # architect decisions taken INSIDE the loop (not interventions)
    decisions_left = max_decisions
    _auton = st0.get("run_mode") == "autonomous"
    _last_beat = t0            # durable time metering: record whole seconds as they accrue
    last_results = []
    refusals, last_refusal = 0, ""

    recovery_notes = []
    live = healthy_workers(workers, notes=recovery_notes)
    if not live:
        return {"stop": BLOCKED,
                "reason": "no healthy worker to dispatch to; " + (recovery_notes[-1]
                          if recovery_notes else "no authorized recovery path is configured"),
                "recovery": recovery_notes, "passes": [], "interventions": interventions,
                "elapsed_s": 0}

    while True:
        if _auton:
            # Commit elapsed whole seconds AND advance the persisted active marker, so a crash charges
            # up to the last commit on the next start. A spend-write FAILURE is NOT swallowed: we stop
            # rather than keep running unmetered (fail closed on durability).
            _whole = int(time.time() - _last_beat)
            if _whole > 0:
                try:
                    goals.record_spend(goal_id, seconds=_whole)
                    _last_beat += _whole
                    goals.mark_run_active(goal_id, _last_beat)
                except Exception as _e:
                    return {"stop": BUDGET,
                            "reason": "stopping: could not record time spend ({0}); refusing to run "
                                      "unmetered".format(str(_e)[:120]),
                            "passes": passes, "interventions": interventions,
                            "elapsed_s": round(time.time() - t0), "state": goals.state(goal_id)}
        if time.time() - t0 > max_seconds:
            return {"stop": BUDGET, "reason": "wall-clock budget of {0}s spent".format(max_seconds),
                    "passes": passes, "interventions": interventions,
                    "elapsed_s": round(time.time() - t0),
                    "state": goals.state(goal_id)}

        # Refresh capacity every pass. A worker that died after the run started used to be
        # carried as live for the rest of the night; one that recovers must be picked back up
        # without re-dispatching by hand.
        refreshed = healthy_workers(workers, notes=recovery_notes)
        if refreshed != live:
            print("  capacity changed: {0} -> {1}".format(live, refreshed))
        live = refreshed
        if not live:
            return {"stop": BLOCKED,
                    "reason": "no worker can generate; " + (recovery_notes[-1] if recovery_notes
                                                            else "no authorized recovery path"),
                    "recovery": recovery_notes, "passes": passes, "decisions": decisions,
                    "interventions": interventions,
                    "elapsed_s": round(time.time() - t0), "state": goals.state(goal_id)}
        capacity = sum(max(1, int(fleet_queue.WORKERS.get(w, {}).get("max_inflight", 1)))
                       for w in live)
        import projectpkg
        pol = goals.state(goal_id)
        if projectpkg.effective_cadence(pol.get("review_letter") or "", pol.get("domain_branch") or "") == "A":
            capacity = 1
        cards = goals.next_work(goal_id, min(capacity, poll_cards))
        if cards:
            # PURPOSE-AWARE SELECTION (LF-08). The ledger offers what is dependency-ready; before a
            # worker-hour is spent, compare the batch with the approved outcome: prerequisites
            # and required work first, refinements deferred while they wait, criteria steering
            # has redirected or parked not run again. Deferred cards stay READY; the ledger
            # records why they were not run. If everything was deferred, this pass falls through
            # to the architect exactly as an empty batch does.
            chosen, deferred = steer.select(goals.state(goal_id), cards)
            if deferred or chosen:
                try:
                    goals.record_selection(goal_id,
                                           [(c.get("assignment") or c["name"], k, w) for c, k, w in chosen],
                                           [(c.get("assignment") or c["name"], k, w) for c, k, w in deferred])
                except Exception as e:
                    interventions.append("selection record failed: {0}".format(e))
            for c, k, w in deferred:
                print("   deferred {0:<22} [{1}] {2}".format(c.get("assignment") or c["name"], k, w[:110]),
                      flush=True)
            cards = [c for c, k, w in chosen]

        if cards:
            import projectpkg
            limits = (goals.state(goal_id).get("limits") or {})
            kept = []
            for card in cards:
                # Scan the stored contract, not the rendered card. card_for pastes the whole
                # scope into the brief, and a scope that says "Never delete files" would
                # match the never-rule against its own statement.
                display = card.get("assignment") or card["name"]
                stored = ((pol.get("assignments") or {}).get(display) or {}).get("contract")
                hit = projectpkg.limit_hits(stored or card, limits)
                if hit:
                    goals.record(goal_id, card["criterion_id"], "parked-undefined", "",
                                 assignment=display, detail="never-rule: " + hit)
                    interventions.append("never-rule refused {0}: {1}".format(display, hit))
                    print("   refused {0}: {1}".format(display, hit), flush=True)
                else:
                    kept.append(card)
            cards = kept

        if cards and goals.state(goal_id).get("run_mode") == "autonomous":
            spec = (goals.state(goal_id).get("interface_map") or {}).get("spec")
            if spec:
                import conductor
                kept_cards = []
                for card in cards:
                    problems = conductor.consumer_contract_problems(card)
                    display = card.get("assignment") or card["name"]
                    if problems:
                        interventions.append("consumer contract refused {0}: {1}".format(
                            display, ", ".join(problems)))
                        print("   refused {0}: {1}".format(display, ", ".join(problems)), flush=True)
                        try:
                            goals.record(goal_id, card["criterion_id"], "parked-undefined", "",
                                         assignment=display,
                                         detail="consumer contract: " + ", ".join(problems))
                        except Exception as e:
                            interventions.append("consumer refusal record failed: {0}".format(e))
                    else:
                        kept_cards.append(card)
                cards = kept_cards

        if not cards:
            # M1: an empty work list is where a batch runner stops and a LOOP thinks. Before
            # reporting blocked, hand the architect the state delta and let it take the next
            # authorized action. Bounded, because an architect that can always find one more
            # action never finishes.
            if not goals.complete(goal_id) and decisions_left > 0:
                delta = decide.state_delta(goal_id, last_results=last_results,
                                           observe_root=deliverable_root)
                for r in delta.get("reengage") or []:
                    if "id" in r:
                        print("   re-engage {0} {1} on {2}: {3}".format(r["kind"], r["id"], r.get("criterion"),
                                                                       r["why"][:100]), flush=True)
                delta["prior_actions"] = [decide.signature(d.get("final") or d["decision"])
                                          for d in decisions if d.get("applied")]
                if last_refusal:
                    delta["last_refusal"] = ("your previous decision was refused: " + last_refusal
                                             + " -- choose a different action or target")
                failures = {n: decide.failure_text(goals.run_id(goal_id, n))
                            for n in _failed_names(goal_id)}
                print("\n== architect deciding ({0} left): unmet {1}".format(
                    decisions_left, delta["unmet"]), flush=True)
                # Read the deliverables ourselves. Asking a human to go and look at
                # code the harness can open is the harness shirking its own job.
                sources = decide.read_sources(deliverable_root) if deliverable_root else ""
                dec = decide.ask_architect(delta, failures, sources=sources)
                ok, why = decide.validate(dec, delta)
                if not ok:
                    # A refusal costs one decision and is fed back as `last_refusal`; TWO
                    # consecutive refusals end the deciding. Zeroing on the first refusal (the
                    # old rule) ended a live run one step after the architect had OBTAINED the
                    # missing fact by INVESTIGATE and then re-asked the same probe.
                    decisions.append({"decision": dec, "applied": False, "reason": why})
                    interventions.append("architect decision refused: {0}".format(why))
                    print("   REFUSED: {0}".format(why), flush=True)
                    refusals += 1
                    last_refusal = why
                    decisions_left = 0 if refusals >= 2 else decisions_left - 1
                    if _auton:
                        goals.record_spend(goal_id, decisions=1)   # a refused decision still spends budget
                    if decisions_left > 0:
                        continue
                else:
                    # Root the skeptic at the artifact of the assignment under discussion.
                    target = dec.get("assignment") or (_failed_names(goal_id) or [""])[0]
                    if (dec.get("action") or "").upper() == decide.PROPOSAL:
                        # The proposal's evidence is the run that made it. Rooted at the repo the
                        # skeptic cited an unrelated goal card as if it were evidence (live, 8355).
                        prop = next((p for p in delta.get("proposals") or []
                                     if p["id"] == dec.get("proposal_id")), None)
                        target = (prop or {}).get("from") or target
                    art_root = (fleet_runs_root() / ("verified-" + goals.run_id(goal_id, target))
                                if target else None)
                    if (dec.get("action") or "").upper() == decide.INVESTIGATE and deliverable_root:
                        # An INVESTIGATE is about the PROJECT root. Rooting the skeptic at the
                        # failed run's workspace made it "prove" the project held no config
                        # files (it was listing receipt.json), and the architect revised to a
                        # worse probe (live, late-fee2). Same authority boundary, right root.
                        art_root = Path(deliverable_root)
                    doubt = (decide.challenge(dec, delta,
                                              artifact_root=art_root if art_root
                                              and art_root.exists() else None)
                             if use_skeptic else "(skeptic skipped)")
                    # The challenge reaches the decision maker BEFORE the action is applied.
                    # Appending skeptical prose to an action already taken is a transcript, not
                    # review. Whether the architect revised or retained is recorded either way: a
                    # retained decision with a stated reason is a real outcome, and the skeptic is
                    # sometimes simply wrong.
                    final, verdict, vwhy = (decide.reconsider(dec, doubt, delta)
                                            if use_skeptic else (dec, "SKIPPED", ""))
                    if verdict == "REVISED":
                        ok2, why2 = decide.validate(final, delta)
                        if not ok2:
                            final, verdict, vwhy = dec, "RETAINED", (
                                "revision refused (" + why2 + "); original stands")
                    note = decide.apply(goal_id, final, delta, sources=sources,
                                        observe_root=deliverable_root, assist=use_skeptic)
                    decisions.append({"decision": dec, "final": final, "applied": True,
                                      "note": note, "skeptic": str(doubt)[:800],
                                      "skeptic_verdict": verdict, "verdict_why": vwhy})
                    print("   skeptic -> {0}: {1}".format(verdict, vwhy[:120]), flush=True)
                    dec = final
                    print("   {0} -> {1}".format(dec.get("action"), note), flush=True)
                    decisions_left -= 1
                    if _auton:
                        goals.record_spend(goal_id, decisions=1)   # applied architect decision spends budget
                    if (dec.get("action") or "").upper() == decide.STOP:
                        disp = goals.disposition(goal_id)
                        recheck = goals.next_work(goal_id, poll_cards)
                        still, _def = (steer.select(goals.state(goal_id), recheck)
                                       if recheck else ([], []))
                        if still:
                            # The ledger still has dispatchable work (e.g. a repaired prerequisite
                            # just released its dependent). A STOP here would strand it, so refuse
                            # the STOP and let the loop dispatch what is ready.
                            note = ("STOP refused: {0} still dispatchable ({1}); continuing".format(
                                len(still), ", ".join((c.get('assignment') or c['name']) for c, _k, _w in still)[:120]))
                            interventions.append(note)
                            print("   " + note, flush=True)
                            decisions[-1]["note"] = decisions[-1].get("note", "") + " | " + note
                            continue
                        return {"stop": BLOCKED,
                                "reason": "architect stopped: " + str(dec.get("why", ""))[:200]
                                          + " || ledger: " + "; ".join(disp["reasons"])[:400],
                                "disposition": disp,
                                "passes": passes, "decisions": decisions,
                                "interventions": interventions,
                                "elapsed_s": round(time.time() - t0),
                                "state": goals.state(goal_id)}
                    continue      # re-enter with whatever the decision created

            if goals.complete(goal_id):
                return {"stop": DONE, "reason": "every criterion met with evidence",
                        "passes": passes, "decisions": decisions, "interventions": interventions,
                        "recovery": recovery_notes, "disposition": goals.disposition(goal_id),
                        "elapsed_s": round(time.time() - t0), "state": goals.state(goal_id)}
            disp = goals.disposition(goal_id)
            return {"stop": BLOCKED,
                    "reason": "; ".join(disp["reasons"]) or "nothing is dispatchable and criteria remain open",
                    "disposition": disp,
                    "decisions": decisions,
                    "open_questions": goals.open_questions(goal_id),
                    "passes": passes, "interventions": interventions,
                    "elapsed_s": round(time.time() - t0), "state": goals.state(goal_id)}

        names = [c["name"] for c in cards]                         # run ids
        displays = {c["name"]: (c.get("assignment") or c["name"]) for c in cards}
        stale = _clear_prior(set(names))
        if stale:
            print("   cleared stale terminal copies: {0}".format(", ".join(stale)))
        dispatch_started = time.time() - 1
        try:
            goals.claim(goal_id, [displays[n] for n in names])
        except Exception as e:
            interventions.append("goal-ledger claim failed: {0}".format(e))
        _stage(cards)
        print("\n== dispatching {0}: {1}".format(len(names), ", ".join(names)), flush=True)

        # THE REAL PATH. drain() does the claim, the lease, the executor-start check, the evidence
        # gate and the outcome routing. Nothing here re-implements any of it.
        fleet_queue.drain(live, max_rounds=max_rounds,
                          max_seconds=max(60, int(max_seconds - (time.time() - t0))),
                          redo_mode="conversation")

        filed = _collect(set(names), since=dispatch_started)
        missing = [n for n in names if n not in filed]
        if missing:
            interventions.append("cards never filed by the queue: {0}".format(missing))

        results = []
        recorded = set()
        for name, (state, card) in sorted(filed.items()):
            # `name` is the RUN identity (workspace, receipt); the ledger is keyed by the
            # assignment's display name, which the card carries as `assignment`.
            display = card.get("assignment") or name
            outcome = _outcome_for(state, card, name)
            crit = card.get("criterion_id")
            ref = _evidence_ref(name)
            if crit:
                try:
                    goals.record(goal_id, crit, outcome, ref, assignment=display,
                                 seconds=_seconds_for(name))
                    recorded.add(name)
                    if outcome == "accepted":
                        import integrate
                        st = goals.state(goal_id)
                        con = ((st.get("assignments") or {}).get(display) or {}).get("contract") or {}
                        declared = bool((st.get("integration") or {}).get("declared")) or integrate.enabled(goal_id)
                        if con.get("origin") == "integration-repair":
                            irep = integrate.ingest_repair(goal_id, display, name)
                            print("   integrate-repair: {0}".format(irep), flush=True)
                        elif declared:
                            irep = integrate.consider(goal_id)
                            print("   integrate: {0}".format(irep.get("reports")), flush=True)
                        if declared:
                            queued = integrate.queue_repairs(goal_id)
                            if queued:
                                print("   integrate-repair queued: {0}".format(queued), flush=True)
                except Exception as e:
                    interventions.append("ledger record failed for {0}: {1}".format(name, e))
            results.append({"name": display, "run_id": name, "criterion": crit,
                            "outcome": outcome, "evidence": ref})
            print("   {0:<22} {1:<16} {2}".format(display, outcome, ref or "(no receipt)"),
                  flush=True)
            # LF-05: the worker's record on this kind of work, from what actually happened.
            try:
                import profiles
                wk = card.get("worker")
                model = (fleet_queue.WORKERS.get(wk) or {}).get("model") if wk else None
                try:
                    rec_ = json.loads((_runs_root() / ("verified-" + name) / "result.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    rec_ = {}
                reason = "" if outcome == "accepted" else decide.failure_text(name)[:300]
                profiles.record_outcome(wk, model, card, outcome, rec_, run_id=name, reason=reason)
            except Exception as e:
                interventions.append("profile record failed for {0}: {1}".format(name, e))
            # LF-06: what the worker PROPOSED while doing this, with its evidence and run,
            # becomes a pending item the decision path must resolve.
            try:
                harvested = _harvest_proposals(goal_id, display, name, ref)
                if harvested:
                    print("   proposals: {0}".format(", ".join(
                        "{0} ({1})".format(h["id"], h["kind"]) for h in harvested)), flush=True)
            except Exception as e:
                interventions.append("proposal harvest failed for {0}: {1}".format(name, e))
            # LF-01/02: retain what this run proved, from its own records, AFTER the ledger has
            # it. A lesson is extracted only from a fail->pass on the same check; a skill only
            # from an ACCEPTED deliverable with a frozen oracle. Nothing else is worth keeping.
            if name in recorded:
                try:
                    kept = _retain(name, card, outcome, crit, goal_id)
                    if kept:
                        print("   retained: {0}".format(", ".join(kept)), flush=True)
                except Exception as e:     # retention failing must not fail the run
                    interventions.append("retention skipped for {0}: {1}".format(name, e))

        # RELEASE WHAT WE CLAIMED AND COULD NOT RECORD.
        # goals.reclaim() is deliberately manual: the ledger cannot know whether a RUNNING
        # assignment is still executing somewhere, and re-offering a live job is the duplicate
        # execution Track A's leases exist to prevent. But THIS caller knows something the ledger
        # does not -- it claimed these names and its own drain has returned, so nothing it started
        # is still running. That is a sound basis for releasing exactly those, and nothing else.
        #
        # Without this, a failed record strands the assignment in RUNNING forever. It happened:
        # a ledger write rejected for an imprecise outcome left three assignments stranded, and
        # the next run reported BLOCKED in three seconds with no work to do and no explanation.
        stranded = [displays[n] for n in names if n not in recorded]
        if stranded:
            still_running = [n for n, a in goals.state(goal_id)["assignments"].items()
                             if a.get("status") == "running"]
            foreign = [n for n in still_running if n not in displays.values()]
            if foreign:
                # Someone else's work is in flight. Releasing would re-offer THEIR job, so refuse
                # and say so rather than risk the duplicate execution.
                interventions.append(
                    "stranded {0} but another controller holds {1}; not reclaiming".format(
                        stranded, foreign))
            elif still_running:
                freed = goals.reclaim(goal_id, older_than_s=0)
                print("   released stranded assignments: {0}".format(", ".join(freed)))
                interventions.append("auto-released stranded assignments: {0}".format(freed))

        passes.append({"dispatched": names, "results": results})
        last_results = results
        wave = _autonomous_after_wave(goal_id, results)
        if wave is not None:
            wave["passes"] = passes
            wave["interventions"] = interventions
            wave["elapsed_s"] = round(time.time() - t0)
            return wave

        if not results:
            return {"stop": BLOCKED, "reason": "a dispatch pass produced no filed results",
                    "passes": passes, "interventions": interventions,
                    "elapsed_s": round(time.time() - t0), "state": goals.state(goal_id)}
        import projectpkg
        st_goal = goals.state(goal_id)
        pause = projectpkg.effective_cadence(st_goal.get("review_letter") or "",
                                             st_goal.get("domain_branch") or "")
        if pause in ("A", "B") and goals.next_work(goal_id, 1):
            return {"stop": "REVIEW",
                    "reason": ("review cadence {0}: stopped so you can see a draft. "
                               "This does not choose how many workers run.".format(pause)),
                    "passes": passes, "interventions": interventions,
                    "elapsed_s": round(time.time() - t0), "state": st_goal}


def main():
    ap = argparse.ArgumentParser(description="Run an approved goal through the real execution path")
    ap.add_argument("goal_id")
    ap.add_argument("--workers", default=DEFAULT_WORKER)
    ap.add_argument("--max-seconds", type=int, default=3600)
    ap.add_argument("--max-rounds", type=int, default=4)
    ap.add_argument("--max-decisions", type=int, default=3,
                    help="architect decisions allowed per run; 0 gives the plain batch-runner "
                         "baseline with no evidence-informed continuation")
    ap.add_argument("--no-skeptic", action="store_true",
                    help="skip the skeptic challenge (baseline arm of the comparison)")
    ap.add_argument("--deliverable-root", default=None,
                    help="directory holding the goal's python deliverables; their public "
                         "API is read and given to the architect and to repair briefs")
    a = ap.parse_args()
    summary = run_goal(a.goal_id, [w.strip() for w in a.workers.split(",") if w.strip()],
                       max_seconds=a.max_seconds, max_rounds=a.max_rounds,
                       deliverable_root=a.deliverable_root,
                       max_decisions=a.max_decisions, use_skeptic=not a.no_skeptic)
    print("\n" + "=" * 70)
    print("STOP: {0} -- {1}".format(summary["stop"], summary["reason"]))
    print("elapsed: {0}s   passes: {1}".format(summary.get("elapsed_s"), len(summary["passes"])))
    if summary.get("interventions"):
        print("INTERVENTIONS ({0}):".format(len(summary["interventions"])))
        for i in summary["interventions"]:
            print("  - " + str(i))
    else:
        print("INTERVENTIONS: none")
    out = fleet_runs_root() / ("goalrun-" + a.goal_id + ".json")
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print("summary: " + str(out))
    return 0 if summary["stop"] == DONE else 1


if __name__ == "__main__":
    sys.exit(main())
