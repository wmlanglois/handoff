"""Purpose-preserving work selection: keep the approved outcome in view, account for effort, and
change strategy or move to other authorized work when the current approach is consuming effort
without moving anything.

WHAT ALREADY EXISTED, AND WHAT DID NOT. The ledger (run/goals.py) knows criteria, assignments,
outcomes and a budget COUNT; progress.py judges stagnation INSIDE one run; decide.py closes the
action set and refuses an exact repeat. Nothing added up what a criterion had cost across its
attempts and decisions, nothing said whether a stuck criterion actually blocks the approved outcome,
and nothing stopped the architect from committing to one strategy: the frozen trajectory in
tests/fixtures/steer shows a REPAIR on a wrong diagnosis, a REPAIR of already-finished work, and
repeated repairs on the same lineage while other authorized work waited.

THE DISTINCTIONS THIS MODULE DRAWS, each from ledger state and run records, never from a score:
  important vs merely difficult    -- importance is STRUCTURAL (other work needs this criterion, or it
                                      is the last thing between the goal and done) or DECLARED by the
                                      person at creation (`priority`); difficulty is effort spent.
  real prerequisite vs refinement  -- a criterion something else `needs` is a prerequisite; a FOLLOWON
                                      that adds work to a criterion that is not moving is a refinement.
  productive investigation vs      -- an observation that produced a NEW fact is movement (evidence);
  repeated explanation                a repair whose lineage has not moved anything is not.
  commitment to the goal vs to     -- when a strategy has failed twice without movement on a real
  one strategy                        prerequisite the answer is a DIFFERENT strategy (investigate,
                                      another approach), never another repair of the same one.
  authorized scope change vs drift -- a criterion is changed only by a person (park -> resolve /
                                      sign-off); piling follow-ons onto an unmoving criterion is drift.

The assessment is ADVISORY TO THE ARCHITECT AND BINDING ON VALIDATION: decide.state_delta carries it,
decide.validate refuses actions the verdict rules out. It never relaxes acceptance, never edits a
criterion, and never abandons a prerequisite -- it forbids the failing strategy, not the goal.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _runs_dir():
    """The run-state root (FLEET_RUNS_DIR aware) -- the same one the verified writer uses."""
    import fleet
    return fleet.runs_root()

PERSIST, CHANGE_STRATEGY, REDIRECT, PARK_IT, AWAIT_HUMAN = (
    "persist", "change_strategy", "redirect", "park", "await_human")
SPIRAL_AFTER = 2            # repairs (or non-investigating decisions) without movement


def _index_rows(runs_root):
    p = Path(runs_root or _runs_dir()) / "queue" / "index.jsonl"
    rows = []
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _run_record(runs_root, run_id, runs=None):
    if runs is not None:
        return runs.get(run_id) or {}
    p = Path(runs_root or _runs_dir()) / ("verified-" + run_id) / "result.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _lineage_root(name):
    """`totals-r2-r2` and `totals-v2` belong to the lineage of `totals`."""
    parts = name.split("-")
    while len(parts) > 1 and parts[-1][:1] in ("r", "v") and parts[-1][1:].isdigit():
        parts.pop()
    return "-".join(parts)


def effort(state, *, runs_root=None, runs=None, index_rows=None):
    """Per criterion: what it has cost and what it has moved, from the ledger and run records."""
    idx = index_rows if index_rows is not None else _index_rows(runs_root)
    secs_by_run = {}
    for row in idx:
        if row.get("name"):
            secs_by_run[row["name"]] = secs_by_run.get(row["name"], 0) + float(row.get("sec") or 0)
    out = {}
    for c in state.get("criteria") or []:
        cid = c["id"]
        e = {"attempts": 0, "rounds": 0, "seconds": 0.0, "assignments": [], "repairs": 0,
             "revisits": 0, "followons": 0, "observations_ok": 0, "observations_failed": 0,
             "new_facts": 0, "approaches": set(), "movement": [], "outcomes": []}
        roots = set()
        for name, a in (state.get("assignments") or {}).items():
            con = a.get("contract") or {}
            if con.get("criterion_id") != cid:
                continue
            e["assignments"].append(name)
            e["attempts"] += int(a.get("attempts", 0))
            if con.get("approach"):
                e["approaches"].add(con["approach"])
            root = _lineage_root(name)
            if root != name and name.rsplit("-", 1)[-1].startswith("r"):
                e["repairs"] += 1
            if con.get("lineage"):
                e["revisits"] += 1
            if root == name and not con.get("lineage") and con.get("origin") == "followon":
                e["followons"] += 1
            roots.add(root)
            rid = a.get("run_id") or name
            rec = _run_record(runs_root, rid, runs)
            e["rounds"] += int(rec.get("rounds") or 0)
            e["seconds"] += float(secs_by_run.get(rid, 0))
            hist = rec.get("history") or []
            for i in range(1, len(hist)):
                if hist[i - 1].get("oracle_ok") is False and hist[i].get("oracle_ok") is True:
                    e["movement"].append({"kind": "oracle-flip", "assignment": name})
            for o in a.get("outcomes") or []:
                e["outcomes"].append(o.get("outcome"))
                if o.get("outcome") in ("accepted", "awaiting-review"):
                    e["movement"].append({"kind": o["outcome"], "assignment": name, "ts": o.get("ts")})
        seen_text = set()
        for o in state.get("observations") or []:
            if o.get("criterion_id") != cid:
                continue
            if o.get("ok"):
                e["observations_ok"] += 1
                key = (o.get("text") or "").strip()
                # An empty listing or an empty grep is not a fact gained (live: `list
                # tests/fixtures` -> "(none)" was counted as movement on c1).
                if key and key not in seen_text and key not in ("(no matches)", "(none)")                         and not key.startswith("(") :
                    seen_text.add(key)
                    e["new_facts"] += 1
                    e["movement"].append({"kind": "evidence", "ts": o.get("ts")})
            else:
                e["observations_failed"] += 1
        for q in state.get("questions") or []:
            if q.get("criterion_id") == cid and q.get("status") == "resolved":
                e["new_facts"] += 1
                e["movement"].append({"kind": "question-resolved", "ts": (q.get("resolution") or {}).get("at")})
        # decisions after the last movement, from the ledger's own event order
        events = state.get("events") or []
        last_move_ts = max([m.get("ts") or "" for m in e["movement"]] or [""])
        after = [ev for ev in events if (ev.get("ts") or "") > last_move_ts]
        e["repairs_since_movement"] = sum(
            1 for ev in after if ev.get("kind") == "assigned"
            for n in (ev.get("names") or []) if n in e["assignments"] and _lineage_root(n) != n
            and n.rsplit("-", 1)[-1].startswith("r"))
        e["decisions_since_movement"] = sum(
            1 for ev in after if ev.get("kind") in ("assigned", "revisited", "observed", "parked")
            and (ev.get("criterion") == cid or any(n in e["assignments"] for n in (ev.get("names") or []))
                 or ev.get("as_") in e["assignments"]))
        e["approaches"] = sorted(e["approaches"])
        e["status"] = c.get("status")
        out[cid] = e
    return out


def importance(state, cid):
    """Structural and declared importance of one criterion. Nothing here is a score."""
    crit = next((c for c in state.get("criteria") or [] if c["id"] == cid), {})
    mine = {n for n, a in (state.get("assignments") or {}).items()
            if (a.get("contract") or {}).get("criterion_id") == cid}
    dependents = set()
    for n, a in (state.get("assignments") or {}).items():
        con = a.get("contract") or {}
        if con.get("criterion_id") == cid:
            continue
        if any(need in mine for need in (con.get("needs") or [])):
            dependents.add(con.get("criterion_id"))
    unmet = [c["id"] for c in state.get("criteria") or [] if c.get("status") != "met"]
    return {"dependents": sorted(d for d in dependents if d),
            "sole_unmet": unmet == [cid],
            "priority": crit.get("priority", "normal"),
            "human_only": bool(crit.get("human_only")),
            "prerequisite": bool(dependents) or unmet == [cid]
            or crit.get("priority") in ("high", "critical")}


def assess(state, *, runs_root=None, runs=None, index_rows=None, spiral_after=SPIRAL_AFTER):
    """Verdict per unmet criterion: persist | change_strategy | redirect | park | await_human,
    with the actions it rules out and why. Binding on decide.validate."""
    eff = effort(state, runs_root=runs_root, runs=runs, index_rows=index_rows)
    verdicts = {}
    unmet = [c for c in state.get("criteria") or [] if c.get("status") != "met"]
    for c in unmet:
        cid = c["id"]
        e, imp = eff[cid], importance(state, cid)
        summary = ("{0} attempt(s), {1} round(s), {2:.0f}s, {3} repair(s), {4} new fact(s); "
                   "{5} repair(s) and {6} decision(s) since the last movement").format(
                       e["attempts"], e["rounds"], e["seconds"], e["repairs"], e["new_facts"],
                       e["repairs_since_movement"], e["decisions_since_movement"])
        v = {"verdict": PERSIST, "basis": "", "refused": {}, "effort": summary,
             "effort_detail": {"approaches": e["approaches"], "repairs": e["repairs"],
                               "new_facts": e["new_facts"], "seconds": round(e["seconds"]),
                               "rounds": e["rounds"], "attempts": e["attempts"]},
             "importance": imp, "allowed_note": ""}
        if c.get("status") == "review":
            v.update(verdict=AWAIT_HUMAN,
                     basis="machine-complete; a person must sign off the prose criteria",
                     refused={"REPAIR": "finished work awaits a person, not a repair",
                              "FOLLOWON": "finished work awaits a person, not more work"})
            verdicts[cid] = v
            continue
        failing = (e["repairs_since_movement"] >= spiral_after
                   or (e["decisions_since_movement"] >= spiral_after + 1 and e["new_facts"] == 0
                       and e["repairs_since_movement"] >= 1))
        if e["followons"] >= spiral_after and e["repairs_since_movement"] == 0 and not e["movement"]:
            v["refused"]["FOLLOWON"] = ("{0} follow-ons added to a criterion that has not moved: "
                                        "adding work is drift, not progress".format(e["followons"]))
        if not failing:
            v["basis"] = "effort is still buying movement, or nothing has been tried twice"
            verdicts[cid] = v
            continue
        others = [o["id"] for o in unmet if o["id"] != cid and o.get("status") in ("open", "parked")]
        why_stuck = ("the current strategy has cost {0} and produced no movement on {1}"
                     .format(summary, cid))
        if imp["prerequisite"]:
            reason = ("{0}; but {1} is a real prerequisite ({2}), so the strategy changes and the "
                      "criterion is NOT abandoned".format(
                          why_stuck,
                          cid, "needed by " + ", ".join(imp["dependents"]) if imp["dependents"]
                          else ("the last unmet criterion" if imp["sole_unmet"]
                                else "declared priority " + str(imp["priority"]))))
            v.update(verdict=CHANGE_STRATEGY, basis=reason)
            v["refused"]["REPAIR"] = ("{0} repair(s) of the same lineage since anything moved; "
                                      "repairing it again is commitment to a strategy, not to the "
                                      "goal. Investigate, revisit another approach, or add work "
                                      "with a different approach".format(e["repairs_since_movement"]))
            v["allowed_note"] = ("INVESTIGATE a fact the failing work needed; REVISIT a preserved "
                                 "alternative; FOLLOWON with an `approach` not yet tried "
                                 "({0} tried); PARK with this effort account if none applies".format(
                                     ", ".join(e["approaches"]) or "none"))
        elif others:
            v.update(verdict=REDIRECT,
                     basis=why_stuck + "; nothing else depends on it and authorized work remains "
                     "on " + ", ".join(others) + ": park it with its effort account and move")
            for act in ("REPAIR", "FOLLOWON", "INVESTIGATE", "REVISIT"):
                v["refused"][act] = ("effort on {0} is not buying movement and other authorized "
                                     "work exists ({1}); PARK this criterion and take that work"
                                     .format(cid, ", ".join(others)))
        else:
            v.update(verdict=PARK_IT,
                     basis=why_stuck + "; nothing depends on it and no other work remains: park it "
                     "with its effort account for a person")
            for act in ("REPAIR", "FOLLOWON", "REVISIT"):
                v["refused"][act] = "the strategy has failed and nothing else is owed; PARK or STOP"
        verdicts[cid] = v
    return verdicts


def target_of(decision, delta):
    """Which criterion a decision is about, for enforcing the verdict."""
    d = decision or {}
    act = (d.get("action") or "").upper()
    if act in ("REPAIR", "REVISIT"):
        a = (delta.get("assignments") or {}).get(d.get("assignment")) or {}
        return a.get("criterion")
    return d.get("criterion_id")


# =================================================================================================
# pre-dispatch selection: compare the batch with the approved outcome, not only the last failure
# =================================================================================================

REQUIRED, PREREQUISITE, REFINEMENT, PROPOSAL, UNKNOWN = (
    "required", "prerequisite", "refinement", "proposal", "unknown")


def classify(state, card, verdicts=None):
    """What a ready card IS relative to the approved outcome.

      prerequisite -- its criterion is needed by other work, or is the last unmet criterion;
      required     -- approved plan work, a repair or a revisit on an unmet criterion;
      refinement   -- an architect follow-on on a criterion that already has movement, or whose
                      criterion the approved plan already covers with unfinished required work;
      proposal     -- work that entered through a worker proposal (LF-06);
      unknown      -- the card's origin cannot be established; recorded, never invented.
    Uncertainty is a label with a reason, not a number."""
    cid = card.get("criterion_id")
    a = (state.get("assignments") or {}).get(card.get("assignment") or card.get("name")) or {}
    con = a.get("contract") or {}
    imp = importance(state, cid) if cid else {"prerequisite": False}
    origin = con.get("origin") or ("plan" if not con.get("lineage") and not _is_repair(card.get("assignment") or "") else None)
    if origin is None:
        origin = "repair" if _is_repair(card.get("assignment") or "") else ("revisit" if con.get("lineage") else None)
    if origin == "followon":
        e = effort(state).get(cid) or {}
        others_required = [n for n, b in (state.get("assignments") or {}).items()
                           if (b.get("contract") or {}).get("criterion_id") == cid
                           and (b.get("contract") or {}).get("origin") in (None, "plan", "repair", "revisit")
                           and b.get("status") in ("ready", "running")]
        if e.get("movement") or others_required:
            return REFINEMENT, ("architect follow-on on {0}, which {1}".format(
                cid, "already has movement" if e.get("movement") else "still has required work pending: " + ", ".join(others_required)))
        # not a refinement: it is the only work on its criterion, judged below like plan work
    if imp.get("prerequisite"):
        return PREREQUISITE, "criterion {0} {1}".format(cid, "is needed by " + ", ".join(imp["dependents"])
                                                    if imp.get("dependents") else "is the last unmet criterion"
                                                    if imp.get("sole_unmet") else "has declared priority " + str(imp.get("priority")))
    if origin == "proposal":
        return PROPOSAL, "entered through an accepted worker proposal"
    if origin in ("plan", "repair", "revisit", "followon"):
        return REQUIRED, "{0} work on unmet criterion {1}".format(origin, cid)
    return UNKNOWN, "origin of {0} could not be established; dispatched, flagged".format(card.get("assignment"))


def _is_repair(name):
    return name != _lineage_root(name) and name.rsplit("-", 1)[-1].startswith("r") \
        and name.rsplit("-", 1)[-1][1:].isdigit()


def select(state, cards, *, runs_root=None, runs=None, index_rows=None):
    """Decide which ready cards to dispatch NOW and which to defer, against the approved outcome.

    Rules, in order:
      1. a card whose criterion's verdict is `redirect` or `park` is deferred (its effort is not
         buying movement and steering has already said where effort should go);
      2. a `refinement` is deferred while any `prerequisite` or `required` card is in the batch --
         the approved outcome comes before polishing it;
      3. everything else dispatches, prerequisites first, then required, proposals, unknown.
    A `change_strategy` prerequisite is DISPATCHED if its card is a different strategy (revisit,
    new approach) -- persistence with a blocking prerequisite is the point -- and only a
    same-lineage repair would have been refused upstream by decide.validate.
    Returns (dispatch, deferred) as lists of (card, kind, reason)."""
    verdicts = assess(state, runs_root=runs_root, runs=runs, index_rows=index_rows)
    labelled = []
    for c in cards:
        kind, why = classify(state, c, verdicts)
        labelled.append((c, kind, why))
    has_core = any(k in (PREREQUISITE, REQUIRED) for _, k, _ in labelled)
    dispatch, deferred = [], []
    for c, kind, why in labelled:
        v = verdicts.get(c.get("criterion_id")) or {}
        if v.get("verdict") in (REDIRECT, PARK_IT):
            deferred.append((c, kind, "steering verdict {0} on {1}: {2}".format(
                v["verdict"], c.get("criterion_id"), v.get("basis", "")[:200])))
            continue
        if kind == REFINEMENT and has_core:
            deferred.append((c, kind, why + "; required work is ready and comes first"))
            continue
        dispatch.append((c, kind, why))
    rank = {PREREQUISITE: 0, REQUIRED: 1, PROPOSAL: 2, UNKNOWN: 3, REFINEMENT: 4}
    dispatch.sort(key=lambda t: rank.get(t[1], 9))
    return dispatch, deferred
