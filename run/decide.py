"""M1: the architect decision step. What turns a scheduler into a loop.

Until now `run/orchestrate.py` selected ready assignments, ran them, recorded results, and when
nothing was left it stopped BLOCKED. That is a batch runner. The missing step is the one the
operator asked for from the beginning: when a batch completes or the loop stalls, hand the
architect a compact picture of the environment and let it take the NEXT AUTHORIZED ACTION without
the user typing anything.

This module supplies that step and nothing else. It is not a new role and not a framework:
  * the ARCHITECT is the same `claude -p` call run/conductor.py already uses,
  * the SKEPTIC is the existing skeptic, challenging the architect's own conclusion,
  * every action is applied through the existing run/goals.py API.

FOUR AUTHORIZED ACTIONS, and the set is closed. An architect that can invent an action can invent
one that ends the run.

  REPAIR   a failed assignment gets another attempt with a revised brief. The revision may change
           only the brief. The oracle and done_when are copied VERBATIM from the original, so a
           repair can never buy acceptance by softening what acceptance means. That rule is the
           reason this is safe to run unattended.
  FOLLOWON a new assignment against a criterion that is still open.
  PARK     a real question for the human, recorded against its criterion.
  STOP     nothing useful remains; say why.

WHY THE SKEPTIC RULES ON THE ARCHITECT HERE. Every other check in this repo points at the worker.
The architect's decision is the one conclusion nothing examines, and it is the conclusion that
decides whether the loop keeps spending. The skeptic gets the decision and the evidence, and its
challenges are attached to the record. It does not veto: it is the jury, and the architect rules.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "run"))

import goals  # noqa: E402
import steer  # noqa: E402
from fleet import SKEPTIC_WORKER  # noqa: E402
from fleet import runs_root as fleet_runs_root  # noqa: E402  (one runs root for writer+readers)

REPAIR, FOLLOWON, PARK, STOP = "REPAIR", "FOLLOWON", "PARK", "STOP"
INVESTIGATE, REVISIT = "INVESTIGATE", "REVISIT"
PROPOSAL, QUESTION = "PROPOSAL", "QUESTION"
CHALLENGE = "CHALLENGE"
ADJUST = "ADJUST"
ACTIONS = (REPAIR, FOLLOWON, PARK, STOP, INVESTIGATE, REVISIT, PROPOSAL, QUESTION, CHALLENGE, ADJUST)

SYSTEM = """You are the ARCHITECT of an autonomous work loop. A batch of work just finished and
nothing is currently dispatchable. Decide the single next action. You do not do the work.

You are given the goal, its criteria and their status, what each assignment produced, and the exact
failure text of anything that did not succeed. Decide from that evidence, not from assumption.

Reply with JSON ONLY, one object, no prose around it:

  {"action": "REPAIR",   "assignment": "<name>", "why": "...", "brief_addition": "..."}
  {"action": "FOLLOWON", "criterion_id": "<id>", "name": "<kebab-name>", "why": "...", "brief": "..."}
  {"action": "PARK",     "criterion_id": "<id>", "question": "...", "why": "..."}
  {"action": "STOP",     "why": "..."}
  {"action": "INVESTIGATE", "criterion_id": "<id>", "question": "...",
                            "probe": {"tool": "list"|"read"|"grep", "target": "<relative path or glob>",
                                      "pattern": "<regex, grep only>"}, "why": "..."}
  {"action": "REVISIT",  "assignment": "<name>", "why": "<the new evidence that makes this approach relevant>"}
  {"action": "ADJUST",   "assignment": "<name>", "max_output_tokens": <int>, "why": "<the recorded evidence>"}
  {"action": "PROPOSAL", "proposal_id": "P-...", "resolution": "accept"|"defer"|"reject", "reason": "...",
                         "criterion_id": "<id, accept only>", "name": "<kebab-name, accept only>",
                         "brief": "<what the worker builds, accept only>", "approach": "<optional>",
                         "artifact": "<module>.py", "done_when": ["<mechanical check>", ...],
                         "oracle": "<python that imports <module> from the workspace and asserts the checks>"}
  {"action": "QUESTION", "question_id": "Q-... (omit to open a new one)", "text": "...",
                         "criterion_id": "<id or null>",
                         "explanations": [{"text": "...", "predicts": "...", "evidence_for": "...", "evidence_against": "..."}],
                         "distinguishing": "<what observation would tell them apart>",
                         "resolution": {"explanation": <index>, "why": "...", "evidence_ref": "..."} (optional),
                         "reconsider_when": [<condition>, ...] (optional, see RE-ENGAGEMENT)}
  {"action": "CHALLENGE", "criterion_id": "<id of a MET criterion>", "kind": "purpose_unmet"|"counterexample",
                          "basis": "<what you saw in the artifact or evidence that the acceptance may not cover>",
                          "evidence_ref": "<optional file>", "why": "..."}
  A PROPOSAL with "resolution": "defer" may carry "reconsider_when": [<condition>, ...].

RULES.
- INVESTIGATE is for a FACT the failing work needed and the brief did not contain, when that fact
  is available in the authorized project root (the file list, a module's contents, a grep). The
  harness runs the read-only probe itself and the result is attached to every later card for that
  criterion. If the probe reports NOT ACCESSIBLE, do not investigate the same target again; PARK
  with the specific inaccessible thing named, or STOP.
- REVISIT re-opens a preserved alternative approach (listed under `alternatives`) as a new
  assignment under the SAME acceptance, when a later result makes that approach relevant again.
  Name the evidence; "try the other one" is not a reason.
- REPAIR is for an assignment that failed for a REASON YOU CAN NAME AND FIX in the instructions.
  `brief_addition` is appended to the original brief. Its acceptance check is reused unchanged and
  you cannot alter it, so do not propose loosening a requirement: say what the worker must do
  differently. If the failure text shows the worker misunderstood an interface, state the exact
  interface it should have used.
- FOLLOWON is for real remaining work on an OPEN criterion, never a report, summary or
  investigation task. If the useful next step is work you would have to do yourself, that is STOP.
- PARK when only a person can resolve it. State the question so a human can answer it in one
  sitting.
- STOP when no authorized action would advance the goal. Say plainly what is missing.
- PROPOSAL resolves something a WORKER proposed (listed under `proposals` with its evidence and
  the run it came from). Accept only when it advances an UNMET criterion of THIS goal; an accepted
  proposal becomes ordinary work under the goal's existing budget and permissions (no tools, no new
  criteria). An acceptance is a CONTRACT like any planned one: name the `artifact` module, the
  `done_when` checks and an `oracle` that imports the module from the workspace and asserts them --
  work accepted without a mechanical check parked stagnant twice (live, 2026-09-20) because nothing
  could tell the worker's prose from a deliverable. Defer when it may matter later; reject when it
  does not serve the approved outcome. Always give the reason; the worker's evidence is what you
  weigh, not its enthusiasm.
- QUESTION keeps an open question ALIVE across runs: what is unknown, the competing explanations,
  and what observation would distinguish them. Open one when successful or failed work exposed
  something the evidence cannot yet settle; INVESTIGATE with `question_id` to attach evidence to it;
  resolve it only on attached observations, naming the explanation the evidence supports. A
  resolved question is a new fact for its criterion.
- CHALLENGE doubts a result that is already ACCEPTED, when the artifact or the evidence in front of
  you shows the acceptance may not cover the criterion's purpose, or you can name a concrete input
  the accepted artifact should handle and may not. The receipt is untouched; a persistent question
  is opened and the goal is not complete while it is open. Not after every success: only with a
  basis you can state. Resolve it later with QUESTION on evidence (observation or evidence_ref).
- Never propose changing a criterion, an oracle, or what counts as done. That is not yours.
- Prefer the action that advances a criterion. Do not repeat an action that already failed twice.

STEERING. `steer` in STATE gives, per unmet criterion, what it has cost, whether it is a REAL
PREREQUISITE of the approved outcome (something needs it, it is the last thing open, or the person
declared its priority) and a verdict:
  persist          -- effort is still buying movement; any action.
  change_strategy  -- the current strategy has failed twice on a prerequisite. The criterion is not
                      abandoned; REPAIR of the same lineage is refused. Investigate a needed fact,
                      revisit a preserved approach, or add work with a DIFFERENT `approach`.
  redirect         -- effort is not moving a non-prerequisite while other authorized work waits:
                      PARK it with its effort account and take that work.
  park             -- nothing depends on it and nothing else is owed: PARK or STOP.
  await_human      -- machine-complete; a person signs off. Do not repair or extend it.
Actions listed under `refused` for a criterion are rejected by the harness. Important is not the
same as difficult: cost alone never makes work important, and difficulty alone never makes it
optional.

RE-ENGAGEMENT. When you defer a proposal, keep a question open, or leave a challenge uncertain, say
under what EVIDENCE it is worth looking at again, as `reconsider_when` conditions:
  {"type": "criterion_met", "criterion_id": "<id>"}          another criterion is established
  {"type": "file_present", "path": "<relative to the project root>"}
  {"type": "skill_available", "id": "<skill id>"} or {"type": "skill_available", "name": "<module>"}
  {"type": "assumption_invalidated", "memory_id": "<lesson or skill id>"}   retained advice was suspended
Time is not a condition; "later" is refused. `reengage` in STATE lists the deferred items whose
stated condition NOW holds (each surfaced once per change). Address them before STOP: resolve the
proposal, resolve or investigate the question, or say in `why` what still stands in the way.
"""


def state_delta(goal_id, last_results=None, root=None, observe_root=None):
    """The compact picture the architect decides from. Environment state only, never narrative.
    `observe_root` lets file_present re-engagement conditions be checked; without it they are unknown."""
    kw0 = {"root": root} if root else {}
    try:
        reengage = goals.reengage(goal_id, observe_root=observe_root, **kw0)
    except Exception as e:            # re-engagement must never make the decision step undeliverable
        reengage = [{"error": "re-engagement unavailable: {0}".format(e)}]
    st = goals.state(goal_id, root=root) if root else goals.state(goal_id)
    crits = [{"id": c["id"], "status": c["status"], "text": c["text"],
              "evidence": len(c.get("evidence") or [])} for c in st["criteria"]]
    assigns = {}
    for name, a in st["assignments"].items():
        assigns[name] = {
            "status": a["status"], "attempts": a.get("attempts", 0),
            "criterion": (a.get("contract") or {}).get("criterion_id"),
            "has_oracle": bool(str((a.get("contract") or {}).get("oracle") or "").strip()),
            "outcomes": [o.get("outcome") for o in (a.get("outcomes") or [])]}
    kw = {"root": root} if root else {}
    unmet = [c["id"] for c in crits if c["status"] != "met"]
    # LF-03/LF-04: what the harness has already observed, and which preserved approaches exist,
    # so the architect decides against the environment rather than re-asking for either.
    observed = [{"criterion": o["criterion_id"], "question": o["question"],
                 "probe": o["probe"], "ok": o["ok"], "malformed": bool(o.get("malformed")),
                 "summary": (o["text"] if o["ok"] else "NOT ACCESSIBLE: " + o["reason"])[:400]}
                for o in (st.get("observations") or [])]
    alts = {cid: [{"name": a["name"], "approach": a["approach"], "status": a["status"],
                   "disposition": a["disposition"]} for a in goals.alternatives(goal_id, cid, **kw)]
            for cid in unmet}
    proposals = [{"id": p["id"], "kind": p["kind"], "text": p["text"][:300],
                  "reconsider_when": p.get("reconsider_when"),
                  "evidence": p.get("evidence", "")[:300], "from": p.get("from_assignment"),
                  "criterion": p.get("criterion_id"), "status": p.get("status")}
                 for p in (st.get("proposals") or []) if p.get("status") in ("pending", "deferred")]
    questions_ = [{"id": q["id"], "text": q["text"][:300], "criterion": q.get("criterion_id"),
                   "status": q["status"], "created_by": q.get("created_by"),
                   "explanations": [{"text": e.get("text", "")[:200], "evidence_for": e.get("evidence_for", "")[:160],
                                     "evidence_against": e.get("evidence_against", "")[:160]}
                                    for e in q.get("explanations") or []],
                   "distinguishing": q.get("distinguishing", "")[:200],
                   "observations": [{"ok": ob.get("ok"), "summary": (ob.get("text") or ob.get("reason") or "")[:200]}
                                    for ob in q.get("observations") or []],
                   "resolution": q.get("resolution")}
                  for q in (st.get("questions") or []) if q.get("status") == "open"]
    try:
        steering = steer.assess(st)
    except Exception as e:            # steering must never make the decision step undeliverable
        steering = {"error": "steering unavailable: {0}".format(e)}
    failures_ = {}
    repair_guidance_ = {}
    for name, a in st["assignments"].items():
        disp = a.get("disposition") or ""
        if disp.startswith("parked") or disp in ("CRASH",):
            rid = a.get("run_id") or name
            cat, suggest = classify_failure(failure_text(rid))
            failures_[name] = {"category": cat, "suggest": suggest}
        b = (a.get("contract") or {}).get("brief") or ""
        if "REPAIR GUIDANCE (architect)" in b:
            add = b.split("REPAIR GUIDANCE (architect)", 1)[1]
            repair_guidance_.setdefault(_repair_base(name), []).append(add.strip()[:300])
    challenges_ = [{"id": c["id"], "criterion": c["criterion_id"], "kind": c["kind"], "basis": c["basis"][:300],
                    "question_id": c.get("question_id"), "status": c["status"], "outcome": c.get("outcome"),
                    "reconsider_when": c.get("reconsider_when")}
                   for c in (st.get("challenges") or []) if c.get("status") == "open"]
    try:
        _d = goals.disposition(goal_id, **({"root": root} if root else {}))
        # reporting fields only; the budget dict is omitted here because its own "assignments"
        # key otherwise collides with the assignments tail when the prompt is scanned.
        disp = {k: _d[k] for k in ("state", "reasons", "unmet", "awaiting_review", "parked") if k in _d}
    except Exception as e:
        disp = {"error": "disposition unavailable: {0}".format(e)}
    return {"goal_id": goal_id, "goal": st.get("goal", ""),
            "disposition": disp,
            "met": [c["id"] for c in crits if c["status"] == "met"],
            "challenges": challenges_, "reengage": reengage,
            "failures": failures_, "repair_guidance": repair_guidance_,
            "criteria": crits, "assignments": assigns,
            "open_questions": goals.open_questions(goal_id, **kw),
            "unmet": unmet, "observations": observed,
            "alternatives": {k: v for k, v in alts.items() if len(v) > 1},
            "steer": steering,
            "proposals": proposals, "questions": questions_,
            "last_results": list(last_results or [])}


SYSTEM += """
OUTPUT BUDGETS ARE NOT FIXED. Each assignment's failure evidence includes a `generation evidence` line:
how many model turns were LENGTH-LIMITED (stopped at the per-call output limit), the limit used, the
lane's context and its output ceiling, and whether the operator PINNED the limit. A length-limited
delivery is a truncated file, not wrong code: do not REPAIR the code, split the work or shrink the
packet as the first response. Use ADJUST to raise that assignment's max_output_tokens above the limit
that was cut off, at or below the ceiling shown; it reopens the assignment under the SAME acceptance.
The harness refuses ADJUST without recorded length-limited turns, above the ceiling, or when the limit
is pinned (then PARK with a question for the operator). ADJUST never changes server settings.
A `timeout:` failure (or `N request timeouts` in the generation evidence) means the harness stopped
waiting for the reply: the lane generated the requested output slower than the wait allowed. It is NOT
evidence the lane is down, so do not re-dispatch to another lane or PARK it as infrastructure on that
basis. Either ADJUST max_output_tokens DOWN (the harness allows a lower limit when timeouts are recorded)
so each turn fits the wait and the worker writes in sections, or PARK a question asking the operator to
set the lane's min_tokens_per_s.
If the evidence says cut-off turns ran out while THINKING (hidden reasoning, no output), raising the
limit does not help and ADJUST is refused: the lane's reasoning setting is the operator's to change.
"""

OUTPUT_LIMIT = "output_limit"
REQUEST_TIMEOUT = "request_timeout"
MISSING_INFO, INTERFACE, MISSING_INPUTS, NO_DELIVERABLE, BAD_STRATEGY, PROGRESSING, TRIVIAL, UNKNOWN = (
    "missing_information", "interface_mismatch", "missing_inputs_or_tools", "no_deliverable_produced",
    "unsuccessful_strategy", "progressing", "trivial_error", "unclear")


def classify_failure(text):
    """From a run's failure evidence, name WHAT is wrong so the next action can be DIFFERENT rather
    than a repetition (O4). Evidence-based keywords, not the worker's self-description. Returns
    (category, suggested_action)."""
    t = (text or "").lower()
    if not t.strip():
        return UNKNOWN, "INVESTIGATE the failure: no failure text was recorded to act on"
    if "timeout:" in t or "request timeouts" in t or "timed out" in t or "wall clock" in t or "parked-timeout" in t:
        return REQUEST_TIMEOUT, ("ADJUST max_output_tokens DOWN so a turn fits the wait (the worker then "
                                 "writes in sections), or PARK a question for the operator about the lane's "
                                 "min_tokens_per_s: the harness stopped waiting; the lane is not shown to be down")
    if "length-limited" in t:
        # First: a file cut off at the output limit surfaces downstream as a syntax, import or
        # "no deliverable" error, and treating those as the cause repairs code that was never wrong.
        return OUTPUT_LIMIT, ("ADJUST this assignment's max_output_tokens above the limit that cut it off "
                              "(within the ceiling shown): the delivery was truncated, not wrong")
    if any(k in t for k in ("syntaxerror", "indentationerror", "taberror", "nameerror", "unboundlocalerror")):
        return TRIVIAL, ("REPAIR the same assignment on this goal. A syntax or name error is not a "
                         "failed purpose, and it is not a reason to STOP or to open a new goal.")
    if "no achievement" in t and ("new evidence" in t or "investigated" in t):
        # movement happened (new facts entered) even though nothing passed yet
        return PROGRESSING, "PERSIST: the work is advancing; let it continue rather than changing it"
    if any(k in t for k in ("modulenotfounderror", "no module named", "importerror",
                            "not accessible", "does not exist", "filenotfounderror")):
        return MISSING_INPUTS, ("the worker could not import/find something it needed: check what is "
                                "actually staged and correct the delivery/inputs, do not just resend the error")
    if any(k in t for k in ("attributeerror", "typeerror", "unexpected keyword", "takes no arguments",
                            "positional argument", "has no attribute", "signature")):
        return INTERFACE, ("the worker assumed the wrong interface: state the exact API it must call "
                           "(names, arguments, return), do not repeat the same brief")
    if any(k in t for k in ("churn", "narrat", "no deliverable", "not a deliverable", "transcript",
                            "no code", "0 new evidence")):
        return NO_DELIVERABLE, ("the worker narrated instead of delivering: the delivery contract must "
                                "be made explicit (artifact, no tools, one fenced block), not restated as prose")
    if any(k in t for k in ("assertionerror", "expected", "!=", "should be", "got ")):
        return BAD_STRATEGY, ("the approach produced a wrong result: name the specific defect to fix, or "
                              "change the approach; another identical attempt will not help")
    return UNKNOWN, "INVESTIGATE the failure before spending another identical attempt"


def _repair_base(name):
    return re.sub(r"(-r\d+)+$", "", name or "")


def generation_summary(name):
    """(text, facts) from the recorded stop reasons for run `name` (#27): how many model turns were
    cut off at the output limit, the limit used, the lane's context and output ceiling, whether the
    operator pinned the limit, and which tool runtime ran. This is what lets the architect tell a
    truncated delivery from wrong code -- and what ADJUST is validated against."""
    import generation
    root = fleet_runs_root()
    tool_ws = re.sub(r"[^A-Za-z0-9_-]", "-", "verified-" + str(name))
    files = sorted(list(root.glob("verified-" + name + "-*.jsonl")) + list(root.glob("tooljob-" + tool_ws + "-*.jsonl")),
                   key=lambda q: q.stat().st_mtime)
    turns = limited = timeouts = thinking_cut = reasoning_chars = 0
    #: Effort levels whose thinking has a ceiling. At these, a thinking cut-off means the limit was
    #: too small for bounded reasoning PLUS code, so raising it helps (A/B 2026-09-25: "low" thought
    #: ~1,400 tokens per turn and filled a 1,400 limit exactly). Unset / "xhigh" is unbounded.
    bounded_cut = 0
    max_requested = max_prompt = timeout_tokens = 0
    worker = runtime = None
    for f in files:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            ev = row.get("event")
            if ev == "worker" and row.get("worker"):
                worker = row["worker"]
            elif ev == "tool_runtime":
                runtime = row.get("kind")
            elif ev == "worker_timeout":
                timeouts += 1
                if isinstance(row.get("requested_max_tokens"), int):
                    timeout_tokens = max(timeout_tokens, row["requested_max_tokens"])
            elif ev == "generation" and isinstance(row.get("evidence"), dict):
                e = row["evidence"]
                turns += 1
                if e.get("response_kind") in ("length_limited", "reasoning_exhausted") or e.get("finish_reason") == "length":
                    limited += 1
                if e.get("response_kind") == "reasoning_exhausted":
                    thinking_cut += 1
                    if e.get("reasoning_effort") in ("low", "medium"):
                        bounded_cut += 1
                if isinstance(e.get("reasoning_chars"), int):
                    reasoning_chars += e["reasoning_chars"]
                if isinstance(e.get("requested_max_tokens"), int):
                    max_requested = max(max_requested, e["requested_max_tokens"])
                pt = (e.get("usage") or {}).get("prompt_tokens")
                if isinstance(pt, int):
                    max_prompt = max(max_prompt, pt)
    if worker is None:
        try:
            worker = json.loads((root / "goal-cards" / (name + ".json")).read_text(encoding="utf-8")).get("worker")
        except (OSError, ValueError):
            worker = None
    import fleet
    ctx = (fleet.WORKERS.get(worker) or {}).get("ctx") if worker else None
    pinned = generation.pinned_output_limit(worker) if worker else None
    ceiling = generation.output_ceiling(worker, max_prompt or None) if worker else 0
    facts = {"turns": turns, "length_limited": limited, "max_requested": max_requested, "worker": worker,
             "ctx": ctx, "max_prompt": max_prompt or None, "ceiling": ceiling, "pinned": pinned,
             "runtime": runtime, "timeouts": timeouts, "timeout_tokens": timeout_tokens or None,
             "reasoning_exhausted": thinking_cut, "reasoning_chars": reasoning_chars,
             "bounded_reasoning_exhausted": bounded_cut}
    if thinking_cut or reasoning_chars:
        tail_think = ("; {0} cut-off turn(s) spent the whole limit on hidden THINKING with no output; {1} "
                      "chars of hidden reasoning recorded".format(thinking_cut, reasoning_chars))
    else:
        tail_think = ""
    tail = ("; {0} request timeouts waiting for up to {1} output tokens (the harness stopped waiting, "
            "not a down lane)".format(timeouts, timeout_tokens or "?")) if timeouts else ""
    if not turns:
        if timeouts:
            return "generation evidence: no completed turns" + tail, facts
        return ("generation evidence: none recorded for this run (the runtime may not report stop "
                "reasons)"), facts
    head = ("{0} of {1} model turns LENGTH-LIMITED (stopped at the output limit)".format(limited, turns)
            if limited else "none of {0} model turns was cut off by the output limit".format(turns))
    return ("generation evidence: {0}; max_tokens up to {1}; lane {2} context {3}, largest reported "
            "prompt {4}, output ceiling {5}; output limit {6}; tool runtime {7}".format(
                head, max_requested or "unknown", worker or "unknown", ctx or "unknown",
                max_prompt or "unknown", ceiling or "unknown",
                "PINNED by operator at {0}".format(pinned) if pinned else "not pinned (ADJUST may raise it)",
                runtime or "n/a")) + tail + tail_think, facts


def failure_text(name, limit=1400):
    """Why something actually failed, from the run's own records.

    The result record carries the DECISION basis ("3 rounds with no achievement"), which says the
    loop gave up but not what went wrong. The assertion text lives in the emit log and is the only
    thing an architect can repair against, so it is dug out here. Without it a REPAIR is a guess
    wearing a decision's clothes."""
    base = fleet_runs_root() / ("verified-" + name)
    try:
        rec = json.loads((base / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    bits = ["outcome: {0}".format(rec.get("outcome")),
            "basis: {0}".format(rec.get("basis", ""))[:300]]

    logs = sorted(fleet_runs_root().glob("verified-" + name + "-*.jsonl"),
                  key=lambda q: q.stat().st_mtime, reverse=True)
    if logs:
        # Scan EVERY failing oracle row and prefer one that names the failure. The newest row is
        # often a truncated traceback tail, and handing the architect a row of carets instead of
        # the reason is worse than handing it nothing.
        best = ""
        for line in logs[0].read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("event") != "oracle" or str(row.get("ok")) != "False":
                continue
            msg = str(row.get("msg") or "")
            i = msg.rfind("AssertionError:")
            if i >= 0:
                best = msg[i:i + 500].strip()
            elif not best:
                best = msg[-300:].strip()
        if best:
            bits.append("the check actually said: " + best)
    # Early, so the length limit on this text never cuts it off.
    bits.append(generation_summary(name)[0])

    # Show the architect WHAT THE WORKER ACTUALLY WROTE -- the REAL deliverable, which for a tools
    # job is a package-relative file in the tool workspace, not output.md in the run dir. Reading
    # only output.md left package-relative tool output (e.g. game/replay.py) and crash evidence
    # invisible, so a repair was written blind (seam 4). Resolve the artifact by the card's declared
    # name across the workspaces it can land in, and surface tool-limit / crash evidence too.
    artifact = ""
    try:
        card = json.loads((fleet_runs_root() / "goal-cards" / (name + ".json")).read_text(encoding="utf-8"))
        artifact = (card.get("artifact") or card.get("dest") or "").replace("\\", "/")
    except (OSError, ValueError):
        card = {}
    best = str(rec.get("best_artifact") or "")
    roots = [Path(best)] if best else []
    roots.append(base)
    try:
        import verified, tooljob
        tw = tooljob.workspace_dir(verified.tool_workspace_id(name))
        if tw:
            roots.append(Path(tw))
    except Exception:
        pass
    rels = [r for r in (artifact, Path(artifact).name if artifact else "", "output.md") if r]
    shown = None
    for root in roots:
        for rel in rels:
            f = Path(root) / rel
            if f.is_file():
                try:
                    shown = (str(f), f.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                break
        if shown:
            break
    if shown:
        bits.append("what the worker produced ({0}, first 1400 chars):\n{1}".format(
            shown[0], shown[1][:1400]))
    else:
        bits.append("what the worker produced: NOT FOUND at the recorded workspace for artifact "
                    "{0!r} -- treat as unknown, not as worker output".format(artifact or "output.md"))

    # tool-boundary evidence (limit / truncation / missing material): the architect must see a tool
    # failure as a tool failure, not infer worker incompetence from a missing file.
    if logs:
        marks = []
        for line in logs[0].read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            ev = row.get("event")
            if ev in ("tooljob_limit", "required_material_missing", "tool_response_truncated"):
                marks.append("{0}: {1}".format(ev, str(row.get("error") or row.get("refs") or "")[:160]))
        if marks:
            bits.append("tool-boundary events: " + "; ".join(marks[-3:]))
    return "\n".join(b for b in bits if b)[:limit]


def read_sources(root, limit=6000):
    """Public API of every python module under `root`, for the architect to decide against.

    THE FAILURE THIS FIXES. Investigating available code is the harness's job, not a question for
    the operator. Twice the loop stalled on exactly this: the architect could not see the modules a
    failing assignment had to call, so it parked a question asking a human to go and look, and its
    one repair told a tools-disabled worker to "open each of those three files". Both are the
    harness declining to do its own reading.

    Signatures only, deliberately: enough to call the thing correctly, small enough to sit in a
    prompt beside the failure text. Imports are executed, so this is for the project's OWN output
    directory, never arbitrary paths."""
    import importlib.util
    import inspect
    out = []
    base = Path(root) if root else None
    if not base or not base.is_dir():
        return ""
    for f in sorted(base.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f.stem, f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            out.append("  {0}: DOES NOT IMPORT -- {1}: {2}".format(f.name, type(e).__name__, e))
            continue
        for name, fn in vars(mod).items():
            if (callable(fn) and not name.startswith("_")
                    and getattr(fn, "__module__", None) == f.stem):
                try:
                    out.append("  {0}.{1}{2}".format(f.stem, name, inspect.signature(fn)))
                except (TypeError, ValueError):
                    out.append("  {0}.{1}(...)".format(f.stem, name))
    return "\n".join(out)[:limit]


def _extract_json(text):
    """Models wrap JSON in prose and fences however they like. Take the last balanced object."""
    if not text:
        return None
    for blob in reversed(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)):
        try:
            return json.loads(blob)
        except ValueError:
            pass
    depth, start, best = 0, None, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    best = json.loads(text[start:i + 1])
                except ValueError:
                    pass
    return best


def ask_architect(delta, failures, model=None, timeout=240, runner=None,
                  sources=""):
    """Call the architect. `runner` exists so this path is testable without a model."""
    fail_block = "\n\n".join("FAILURE TEXT for {0}:\n{1}".format(k, v)
                             for k, v in (failures or {}).items() if v)
    src_block = ("\n\nPUBLIC API OF THE DELIVERABLES ALREADY ON DISK (read for you -- do not"
                 " ask anyone to go and look at these):\n" + sources) if sources else ""
    prompt = "{0}\n\nSTATE:\n{1}\n\n{2}{3}\n\nReturn the decision JSON now.".format(
        SYSTEM, render_state(delta), fail_block[:6000], src_block)
    if runner is not None:
        return _extract_json(runner(prompt))
    try:
        from architect import ARCHITECT_MODEL
    except Exception:
        ARCHITECT_MODEL = "sonnet"
    exe = shutil.which("claude") or "claude"
    r = subprocess.run([exe, "-p", "--model", model or ARCHITECT_MODEL, "--output-format", "text"],
                       input=prompt, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return _extract_json(r.stdout or "")


# What the architect must never lose to truncation, in the order it should read it. Everything
# else follows and is capped per section. Live (2026-09-20) a 12 000-char cut of an unordered
# dump could have hidden `proposals` and `questions` behind 40 assignment records.
_LEAD_KEYS = ("goal_id", "goal", "unmet", "met", "disposition", "failures", "criteria", "steer", "reengage", "challenges", "proposals",
              "questions", "open_questions", "alternatives", "prior_actions", "last_refusal")
_TAIL_KEYS = ("assignments", "observations", "last_results")
_TAIL_CAP = 4000


def render_state(delta, lead_cap=24000):
    """The STATE block: lead sections whole (up to lead_cap), tail sections each capped and
    marked when cut, so a cut is visible to the reader rather than silent."""
    lead = {k: delta[k] for k in _LEAD_KEYS if k in delta}
    parts = [json.dumps(lead, indent=1)[:lead_cap]]
    for k in _TAIL_KEYS:
        if k not in delta:
            continue
        txt = json.dumps({k: delta[k]}, indent=1)
        if len(txt) > _TAIL_CAP:
            txt = txt[:_TAIL_CAP] + "\n... [" + k + " truncated: " + str(len(txt) - _TAIL_CAP) + " more chars]"
        parts.append(txt)
    return "\n".join(parts)


def signature(decision):
    """What counts as 'the same action again'. For INVESTIGATE the probe is part of it: the first
    live run refused a second, different probe on the same criterion as a repeat, which left the
    architect unable to look at the file it had just learned existed."""
    d = decision or {}
    act = (d.get("action") or "").upper()
    target = d.get("criterion_id") or d.get("assignment") or ""
    if act == CHALLENGE:
        return (act, decision.get("criterion_id"))
    if act == INVESTIGATE:
        probe = d.get("probe") or {}
        target = "{0}/{1}/{2}".format(target, probe.get("tool"), probe.get("target"))
    return "{0}:{1}".format(act, target)


def validate(decision, delta):
    """Reject anything outside the closed action set or aimed at the wrong target.

    Returns (ok, reason). A malformed decision is NOT applied and NOT silently retried: an
    architect that cannot say what it wants does not get to act on it."""
    if not isinstance(decision, dict):
        return False, "no JSON decision returned"
    prior = delta.get("prior_actions") or []
    sig = signature(decision)
    if sig in prior:
        # The first live run parked the same criterion twice with near-identical questions.
        # Repeating an action that already had no effect is the loop spending to stand still.
        return False, "already did {0} this run; repeating it changes nothing".format(sig)
    act = (decision.get("action") or "").strip().upper()
    if act not in ACTIONS:
        return False, "action {0!r} is not one of {1}".format(act, list(ACTIONS))
    # STEERING IS BINDING. The verdict names the actions that would spend more effort on a
    # failed strategy or on drift; those are refused here, not left to the architect's goodwill.
    cid = steer.target_of(decision, delta)
    verdict = (delta.get("steer") or {}).get(cid) if cid else None
    if isinstance(verdict, dict) and act in (verdict.get("refused") or {}):
        return False, "steering ({0}) refuses {1} on {2}: {3}".format(
            verdict.get("verdict"), act, cid, verdict["refused"][act])
    if (isinstance(verdict, dict) and verdict.get("verdict") == steer.CHANGE_STRATEGY
            and act == FOLLOWON):
        tried = set(str(x) for x in ((verdict.get("effort_detail") or {}).get("approaches") or []))
        approach = str(decision.get("approach") or "").strip()
        if not approach or approach in tried:
            return False, ("steering (change_strategy) requires a FOLLOWON on {0} to declare an "
                           "`approach` not tried before{1}".format(
                               cid, " (tried: " + ", ".join(sorted(tried)) + ")" if tried else ""))
    if act == REPAIR:
        name = decision.get("assignment")
        if name not in delta["assignments"]:
            return False, "REPAIR names unknown assignment {0!r}".format(name)
        outs = delta["assignments"][name].get("outcomes") or []
        if outs and outs[-1] == "awaiting-review":
            # Machine-complete. The oracle passed; what remains is a PERSON's judgement of the
            # prose criteria (conductor sign-off). Live, the architect tried to "ACCEPT", was
            # refused, and repaired a finished assignment into a failure.
            return False, ("{0!r} is awaiting human review, not repair: its check passed and a "
                           "person must sign off the remaining criteria".format(name))
        add = (decision.get("brief_addition") or "").strip()
        if not add:
            return False, "REPAIR without a brief_addition changes nothing"
        prior = (delta.get("repair_guidance") or {}).get(_repair_base(name)) or []
        def _norm(x):
            return re.sub(r"[^a-z0-9 ]+", " ", re.sub(r"\s+", " ", (x or "").lower())).strip()
        na = _norm(add)
        if len(na) >= 10 and any(na in _norm(p) or _norm(p) in na for p in prior):
            return False, ("this REPAIR repeats guidance already tried on {0} and rejected; another "
                           "identical attempt buys nothing. Change the information (INVESTIGATE a fact), "
                           "the interface, the delivery contract, or the approach -- see `failures` for "
                           "what the evidence says is wrong".format(_repair_base(name)))
        if delta["assignments"][name].get("has_oracle") is False:
            return False, ("{0!r} has no mechanical acceptance check, so a repair can only churn again "
                           "(live: json-line-report). Add the work with FOLLOWON and a contract -- artifact, "
                           "done_when and an oracle that imports the module".format(name))
    if act == FOLLOWON:
        cid = decision.get("criterion_id")
        if cid not in delta["unmet"]:
            return False, "FOLLOWON targets {0!r}, which is not an unmet criterion".format(cid)
        if not (decision.get("brief") or "").strip():
            return False, "FOLLOWON without a brief is not an assignment"
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,48}", decision.get("name") or ""):
            return False, "FOLLOWON needs a kebab-case name"
        if decision["name"] in delta["assignments"]:
            return False, "FOLLOWON name {0!r} already exists".format(decision["name"])
    if act == PARK:
        if decision.get("criterion_id") not in [c["id"] for c in delta["criteria"]]:
            return False, "PARK names an unknown criterion"
        if not (decision.get("question") or "").strip():
            return False, "PARK without a question parks nothing"
    if act == INVESTIGATE:
        # Tied to an approved purpose: an unmet criterion of THIS goal, a closed probe vocabulary,
        # and a target the operator's root confines. Anything else is curiosity, and is refused.
        import observe
        cid = decision.get("criterion_id")
        qid = decision.get("question_id")
        open_q = next((q for q in delta.get("questions") or [] if q["id"] == qid), None) if qid else None
        if qid and open_q is None:
            return False, "INVESTIGATE names unknown or closed question {0!r}".format(qid)
        if cid not in delta["unmet"] and not (open_q and open_q.get("criterion") == cid):
            # Live (8355): a re-engaged question and an open challenge sat on a MET criterion, and
            # the only way to gather their evidence was refused as "not unmet". An open question
            # or challenge is an approved purpose for a probe on its own criterion.
            return False, ("INVESTIGATE targets {0!r}, which is not an unmet criterion and has no open "
                           "question named by question_id".format(cid))
        probe = decision.get("probe") or {}
        if not isinstance(probe, dict) or probe.get("tool") not in observe.PROBES:
            return False, "INVESTIGATE probe must be one of {0}".format(list(observe.PROBES))
        if not (decision.get("question") or "").strip():
            return False, "INVESTIGATE without the question it answers is unbounded curiosity"
        same = [o for o in delta.get("observations") or []
                if o["criterion"] == cid and not o["ok"] and not o.get("malformed")
                and o["probe"].get("target") == probe.get("target")]
        if same:
            return False, ("that target was already found NOT ACCESSIBLE: {0}; asking again "
                           "changes nothing".format(same[-1]["summary"][:120]))
    if act == STOP:
        cats = set()
        for v in (delta.get("failures") or {}).values():
            if isinstance(v, dict):
                cats.add(v.get("category"))
            else:
                cats.add(classify_failure(str(v or ""))[0])
        if cats and cats <= {TRIVIAL, MISSING_INPUTS}:
            return False, ("STOP refused: the recorded failures are trivial errors or missing imports. "
                           "REPAIR this goal. Do not open a new one.")
        if any("id" in r for r in delta.get("reengage") or []):
            ids = ", ".join(r["id"] for r in delta["reengage"] if "id" in r)
            return False, ("re-engaged item(s) {0}: a condition you named now holds. Resolve the proposal, "
                           "investigate or resolve the question, or PARK with the reason it still cannot "
                           "proceed -- STOP over it is refused".format(ids))
    if act == CHALLENGE:
        cid = decision.get("criterion_id")
        crit = next((c for c in delta["criteria"] if c["id"] == cid), None)
        if crit is None:
            return False, "CHALLENGE names an unknown criterion {0!r}".format(cid)
        if crit["status"] not in ("met", "review"):
            return False, ("CHALLENGE is for an ESTABLISHED result; {0} is {1} -- REPAIR, INVESTIGATE or "
                           "PARK open work".format(cid, crit["status"]))
        if decision.get("kind") not in goals.CHALLENGE_KINDS:
            return False, "CHALLENGE kind must be one of {0}".format(list(goals.CHALLENGE_KINDS))
        if len(str(decision.get("basis") or "").strip()) < 20:
            return False, "CHALLENGE needs a basis: what in the artifact or evidence the acceptance may not cover"
        if any(c["criterion"] == cid for c in delta.get("challenges") or []):
            return False, "{0} already has an open challenge; resolve it with QUESTION on evidence".format(cid)
        ref = str(decision.get("evidence_ref") or "").strip()
        if ref and not Path(ref).exists():
            return False, "CHALLENGE evidence_ref {0!r} does not exist".format(ref)
    if act in (PROPOSAL, QUESTION) and decision.get("reconsider_when") is not None:
        okc, whyc = goals.validate_conditions(decision.get("reconsider_when"))
        if not okc:
            return False, "reconsider_when: " + whyc
        if act == PROPOSAL and str(decision.get("resolution") or "").lower() != "defer":
            return False, "reconsider_when belongs to a DEFERRED proposal"
    if act == PROPOSAL:
        pid = decision.get("proposal_id")
        prop = next((p for p in delta.get("proposals") or [] if p["id"] == pid), None)
        if prop is None:
            return False, "PROPOSAL names unknown or already-resolved proposal {0!r}".format(pid)
        res = str(decision.get("resolution") or "").lower()
        if res not in ("accept", "defer", "reject"):
            return False, "PROPOSAL resolution must be accept, defer or reject"
        if not str(decision.get("reason") or "").strip():
            return False, "PROPOSAL without a reason resolves nothing"
        if res == "accept":
            cid = decision.get("criterion_id")
            if cid not in delta["unmet"]:
                return False, ("accepting {0} must name an UNMET criterion of this goal; {1!r} is not "
                               "one. A proposal cannot add a criterion or change the purpose".format(pid, cid))
            if not str(decision.get("brief") or "").strip():
                return False, "an accepted proposal needs a brief for the worker"
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,48}", decision.get("name") or ""):
                return False, "an accepted proposal needs a kebab-case name"
            if decision["name"] in delta["assignments"]:
                return False, "name {0!r} already exists".format(decision["name"])
            if prop.get("kind") == "question":
                return False, "a question is not accepted into work; open it with QUESTION or INVESTIGATE it"
            # Live twice (goals 2a77, 8355): an acceptance with no oracle, `done_when` "the proposed
            # work is delivered" and artifact output.md parked stagnant after three rounds of prose.
            # Accepted work is a contract: it names its module, its checks and the check that runs.
            art = str(decision.get("artifact") or "").strip()
            if not re.fullmatch(r"[a-z_][a-z0-9_]*\.py", art):
                return False, "an accepted proposal names its `artifact` as a module file (<name>.py)"
            dw = decision.get("done_when")
            if not isinstance(dw, list) or not any(str(x).strip() for x in dw):
                return False, "an accepted proposal lists mechanical `done_when` checks"
            oracle = str(decision.get("oracle") or "").strip()
            if len(oracle) < 20 or art[:-3] not in oracle or "assert" not in oracle:
                return False, ("an accepted proposal needs an `oracle`: python that imports {0} from the "
                               "workspace and asserts the done_when checks".format(art[:-3]))
            # integration with steering: accepting a proposal is adding work, and a criterion
            # steering has redirected or parked does not get more work through the side door
            v = (delta.get("steer") or {}).get(cid) or {}
            if v.get("verdict") in ("redirect", "park"):
                return False, ("steering ({0}) on {1}: effort there is not buying movement; a proposal "
                               "does not override that -- defer it or PARK the criterion".format(v["verdict"], cid))
    if act == QUESTION:
        qid = decision.get("question_id")
        if qid:
            if not any(q["id"] == qid for q in delta.get("questions") or []):
                return False, "QUESTION names unknown or closed question {0!r}".format(qid)
        elif not str(decision.get("text") or "").strip():
            return False, "a new QUESTION needs its text"
        cid = decision.get("criterion_id")
        if cid and cid not in [c["id"] for c in delta["criteria"]]:
            return False, "QUESTION names an unknown criterion"
        res = decision.get("resolution")
        if res is not None:
            q = next((q for q in delta.get("questions") or [] if q["id"] == qid), None)
            if q is None:
                return False, "only an existing open question can be resolved"
            if not q.get("observations") and not str((res or {}).get("evidence_ref") or "").strip():
                return False, ("QUESTION {0} has no attached observation; INVESTIGATE it with "
                               "question_id first or name an evidence reference".format(qid))
    if act == REVISIT:
        name = decision.get("assignment")
        a = delta["assignments"].get(name)
        if a is None:
            return False, "REVISIT names unknown assignment {0!r}".format(name)
        if a["criterion"] not in delta["unmet"]:
            return False, "REVISIT of {0!r}: its criterion is already met".format(name)
        if a["status"] == "running":
            return False, "REVISIT of {0!r}: it is still running".format(name)
        if len(str(decision.get("why") or "").strip()) < 20:
            return False, "REVISIT needs the evidence that makes the approach relevant again"
    if act == ADJUST:
        name = decision.get("assignment")
        a = delta["assignments"].get(name)
        if a is None:
            return False, "ADJUST names unknown assignment {0!r}".format(name)
        if a["criterion"] not in delta["unmet"]:
            return False, "ADJUST of {0!r}: its criterion is already met".format(name)
        if a["status"] == "running":
            return False, "ADJUST of {0!r}: it is still running".format(name)
        n = decision.get("max_output_tokens")
        if type(n) is not int or n <= 0:
            return False, "ADJUST needs max_output_tokens as a positive integer"
        if len(str(decision.get("why") or "").strip()) < 20:
            return False, "ADJUST needs the recorded evidence that justifies it"
        try:
            rid = goals.run_id(delta.get("goal_id"), name)
        except Exception:
            rid = name
        _t, f = generation_summary(rid)
        if f["timeouts"] and not f["pinned"] and n < (f["timeout_tokens"] or f["max_requested"] or n + 1):
            if n < 256:
                return False, "ADJUST of {0!r}: {1} is too small to deliver anything useful".format(name, n)
            return True, "ok"   # lowering a limit that timed out: each turn must fit the wait
        # Refuse only UNBOUNDED thinking cut-offs: there, a higher limit buys more thinking. A cut-off
        # at a bounded effort level (low/medium) means reasoning plus code did not fit -- raise it.
        unbounded = f["reasoning_exhausted"] - f.get("bounded_reasoning_exhausted", 0)
        if f["length_limited"] and f["reasoning_exhausted"] >= f["length_limited"] and unbounded > 0:
            return False, ("ADJUST of {0!r} refused: every cut-off turn ran out while THINKING (hidden "
                           "reasoning, no output); a higher limit buys more thinking, not code. PARK a "
                           "question for the operator to lower the lane's reasoning (request profile)".format(name))
        if not f["length_limited"]:
            return False, ("ADJUST of {0!r} refused: no length-limited generation is recorded for it; "
                           "raise a budget only on evidence that the output limit cut it off".format(name))
        if f["pinned"]:
            return False, ("ADJUST of {0!r} refused: the output limit for {1} is PINNED by the operator at {2}; "
                           "PARK with a question instead".format(name, f["worker"], f["pinned"]))
        if n <= f["max_requested"]:
            return False, ("ADJUST of {0!r}: {1} does not exceed the limit that was cut off ({2})".format(
                name, n, f["max_requested"]))
        if not f["ceiling"] or n > f["ceiling"]:
            return False, ("ADJUST of {0!r}: {1} exceeds the output ceiling {2} for lane {3} (context minus "
                           "the largest reported prompt and a margin)".format(name, n, f["ceiling"], f["worker"]))
    return True, "ok"


def _api_block(sources):
    """Attach the deliverables' real API to a repair brief.

    A repair once told a tools-disabled worker to "open each of those three files". It could not.
    The harness knows those signatures, so it supplies them rather than asking the worker to
    fetch what it has no way to fetch."""
    if not sources:
        return ""
    return ("\n\nEXACT PUBLIC API of the modules already on disk beside you. You may have"
            " no file tools, so they are given here; import and call them directly by these"
            " names.\n" + sources)


def accepted_artifact_root(goal_id, criterion_id, root=None):
    """The run directory of the evidence that established a criterion, or None."""
    kw = {"root": root} if root else {}
    st = goals.state(goal_id, **kw)
    crit = next((c for c in st["criteria"] if c["id"] == criterion_id), None)
    for ref in reversed((crit or {}).get("evidence") or []):
        p = Path(str(ref))
        cand = p if p.is_dir() else p.parent
        if cand.exists():
            return cand
    return None


def assist_challenge(criterion_id, basis, artifact_root, worker=SKEPTIC_WORKER, rounds=2):
    """The skeptic, rooted at the ACCEPTED artifact, asked whether the challenge has support.
    Advisory: recorded on the challenge row, never a verdict."""
    if not artifact_root:
        return "no accepted artifact directory found; skeptic not run"
    try:
        sys.path.insert(0, str(ROOT / "skeptic"))
        import skeptic as sk
    except Exception as e:
        return "skeptic unavailable: {0}".format(e)
    claim = ("Criterion {0} was ACCEPTED; its artifact is in this directory. A challenge says: {1}. "
             "Read the artifact and say whether the challenge has support in it, citing what you read."
             .format(criterion_id, str(basis)[:400]))
    try:
        return str(sk.run(claim, worker, str(artifact_root), rounds))[:1500]
    except Exception as e:
        return "skeptic error: {0}".format(e)


def _resolve_deliverable(rid, artifact):
    """(path, bytes) of the parked run's actual deliverable, or (None, None). Resolves the same way
    the architect-evidence path does: the recorded best_artifact, the run dir, and the tool
    workspace, by the artifact's package-relative name and its basename."""
    base = fleet_runs_root() / ("verified-" + rid)
    best = ""
    try:
        best = str(json.loads((base / "result.json").read_text(encoding="utf-8")).get("best_artifact") or "")
    except (OSError, ValueError):
        pass
    roots = ([Path(best)] if best else []) + [base]
    try:
        import verified, tooljob
        tw = tooljob.workspace_dir(verified.tool_workspace_id(rid))
        if tw:
            roots.append(Path(tw))
    except Exception:
        pass
    art = (artifact or "output.md").replace("\\", "/")
    for root in roots:
        for rel in (art, Path(art).name):
            f = Path(root) / rel
            if f.is_file():
                try:
                    return f, f.read_bytes()
                except OSError:
                    continue
    return None, None


def carry_package(goal_id, name, orig, new_name, root=None):
    """The continuation carry for a REPAIR: an IMMUTABLE snapshot of the parked contribution copied
    into an isolated reference (never overwriting the source), plus prior feedback, established
    facts, unresolved questions and lineage. Returns (carry_dict, input_entry_or_None, brief_text).

    This lets the next attempt CONTINUE from the team's actual work instead of restarting from a
    rewritten brief. It does not reuse a receipt, replay side effects, or touch the source run dir."""
    import hashlib
    rid = goals.run_id(goal_id, name, root=root)
    artifact = orig.get("artifact") or "output.md"
    doc = goals.state(goal_id, root=root)
    cid = orig.get("criterion_id")
    facts = ["criterion {0} is {1}".format(c["id"], c.get("status"))
             for c in (doc.get("criteria") or []) if c.get("status") in ("met", "established")]
    qs = [ {"id": q.get("id"), "question": q.get("question"), "hypotheses": q.get("hypotheses") or []}
           for q in (doc.get("questions") or [])
           if q.get("status") == "open" and (q.get("criterion_id") in (cid, None)) ]
    feedback = failure_text(rid, limit=900)
    carry = {"parked_run": rid, "lineage": list(orig.get("lineage") or []) + [name],
             "prior_feedback": feedback, "established_facts": facts,
             "unresolved_questions": qs, "snapshot": None, "snapshot_sha256": None}
    src, data = _resolve_deliverable(rid, artifact)
    inp = None
    if src is not None and data is not None:
        snap_dir = fleet_runs_root() / "carry" / new_name
        snap_dir.mkdir(parents=True, exist_ok=True)
        rel = Path(artifact).name
        snap = snap_dir / rel
        snap.write_bytes(data)                        # immutable copy; source (src) is left untouched
        sha = hashlib.sha256(data).hexdigest()
        carry["snapshot"] = str(snap)
        carry["snapshot_sha256"] = sha
        # staged into the repair worker's OWN workspace at _carry/<file> (a read reference to
        # continue from), never at the deliverable dest -- so it is not mistaken for the new output.
        inp = {"name": rel, "dest": "_carry/" + rel, "location": str(snap), "sha256": sha}
    return carry, inp, _carry_brief(carry, artifact)


def _carry_brief(carry, artifact):
    lines = ["\n\n--- CONTINUE YOUR PRIOR WORK (do not start over) ---"]
    if carry.get("snapshot"):
        lines.append("Your previous attempt at {0} is preserved read-only at `_carry/{1}`. "
                     "With file tools, read that reference; without tools, use its contents "
                     "supplied in the request. "
                     "keep what is correct, and apply the guidance below -- do NOT rewrite from "
                     "scratch.".format(artifact, Path(artifact).name))
    if carry.get("prior_feedback"):
        lines.append("What the previous attempt's evidence showed:\n" + str(carry["prior_feedback"])[:900])
    if carry.get("established_facts"):
        lines.append("Established (do not re-litigate): " + "; ".join(carry["established_facts"][:8]))
    if carry.get("unresolved_questions"):
        lines.append("Open questions to keep in mind: "
                     + "; ".join(str(q.get("question") or "")[:120] for q in carry["unresolved_questions"][:5]))
    return "\n".join(lines) if len(lines) > 1 else ""


def apply(goal_id, decision, delta, root=None, sources="", observe_root=None, assist=False):
    """Apply one validated decision through the existing ledger API. Returns a readable note.

    `observe_root` is the ONLY place INVESTIGATE may read (LF-03): the operator's deliverable
    root. With none configured every probe is recorded NOT ACCESSIBLE, which is the honest
    answer, not a silent widening to the repository."""
    import plan as planning
    act = (decision.get("action") or "").strip().upper()
    kw = {"root": root} if root else {}

    if act == STOP:
        return "architect stopped: {0}".format(str(decision.get("why", ""))[:200])

    if act == INVESTIGATE:
        import observe
        probe = dict(decision.get("probe") or {})
        if decision.get("question_id"):
            probe["question_id"] = decision["question_id"]
        res = observe.observe(observe_root, probe)
        goals.observe(goal_id, decision["criterion_id"], decision.get("question", ""),
                      probe, res, **kw)
        if res["ok"]:
            return "observed {0} {1} for {2} ({3} chars attached to later cards)".format(
                res["tool"], res["target"], decision["criterion_id"], len(res["text"]))
        return "observation NOT ACCESSIBLE for {0}: {1}".format(decision["criterion_id"],
                                                                 res["reason"])

    if act == REVISIT:
        new = goals.revisit(goal_id, decision["assignment"], why=str(decision.get("why", "")),
                            **kw)
        return "revisited {0} as {1} under the same acceptance".format(decision["assignment"],
                                                                        new)

    if act == ADJUST:
        n = int(decision["max_output_tokens"])
        new = goals.revisit(goal_id, decision["assignment"],
                            why="output budget raised to {0} tokens on recorded length-limited "
                                "generations: {1}".format(n, str(decision.get("why", ""))),
                            contract_update={"max_output_tokens": n}, **kw)
        return "adjusted {0}: max_output_tokens {1}, reopened as {2} under the same acceptance".format(
            decision["assignment"], n, new)

    if act == CHALLENGE:
        cid = decision["criterion_id"]
        art_root = accepted_artifact_root(goal_id, cid, **kw)
        assist_text = ""
        if assist:
            assist_text = assist_challenge(cid, decision.get("basis", ""), art_root)
        row = goals.challenge(goal_id, cid, by="architect", kind=decision["kind"],
                              basis=decision.get("basis", ""), evidence_ref=decision.get("evidence_ref", ""),
                              assist=assist_text, **kw)
        return "challenged accepted {0} ({1}) as {2}; question {3} opened; goal not complete while open".format(
            cid, decision["kind"], row["id"], row["question_id"])

    if act == PROPOSAL:
        res = str(decision.get("resolution")).lower()
        if res == "defer" and decision.get("reconsider_when"):
            goals.resolve_proposal(goal_id, decision["proposal_id"], res, decision["reason"], **kw)
            goals.set_reconsider(goal_id, "proposal", decision["proposal_id"], decision["reconsider_when"], **kw)
            return "deferred proposal {0} until {1}".format(
                decision["proposal_id"], "; ".join(
                    "{0} {1}".format(c.get("type"), c.get("criterion_id") or c.get("path") or c.get("id")
                                     or c.get("name") or c.get("memory_id")) for c in decision["reconsider_when"]))
        if res == "accept":
            # Ordinary work under the goal's existing budget and tool policy: no worker
            # pin, no new criterion. What the worker proposed, the architect scoped, the ledger
            # records as origin=proposal so selection can see where it came from.
            contract = planning.as_contract({
                "name": decision["name"], "criterion_id": decision["criterion_id"],
                "capability": str(decision.get("reason", ""))[:200],
                "brief": decision["brief"],
                "done_when": decision.get("done_when") or ["the proposed work is delivered"],
                "evidence": "accepted worker proposal {0}; acceptance is the criterion's own check".format(
                    decision["proposal_id"]),
                "needs": [], "worker": None,
                "tools": goals.state(goal_id, **kw).get("tool_mode") == "tools",
                "oracle": decision.get("oracle", ""),
                "artifact": decision.get("artifact") or "output.md",
                "oracle_covers_done_when": True,
                "origin": "proposal", "approach": decision.get("approach") or None,
                "proposal_id": decision["proposal_id"],
            })
            goals.assign(goal_id, [contract], **kw)
            goals.resolve_proposal(goal_id, decision["proposal_id"], "accept", decision["reason"],
                                   assignment=decision["name"], **kw)
            return "accepted proposal {0} as {1} on {2}".format(decision["proposal_id"],
                                                                decision["name"], decision["criterion_id"])
        goals.resolve_proposal(goal_id, decision["proposal_id"], res, decision["reason"], **kw)
        return "{0} proposal {1}: {2}".format({"defer": "deferred", "reject": "rejected"}[res],
                                              decision["proposal_id"], str(decision["reason"])[:120])

    if act == QUESTION:
        qid = decision.get("question_id")
        if not qid:
            q = goals.open_question(goal_id, decision["text"], criterion_id=decision.get("criterion_id"),
                                    explanations=decision.get("explanations") or [],
                                    distinguishing=decision.get("distinguishing") or "", **kw)
            if decision.get("reconsider_when"):
                goals.set_reconsider(goal_id, "question", q["id"], decision["reconsider_when"], **kw)
            return "opened question {0}".format(q["id"])
        q = goals.update_question(goal_id, qid, explanations=decision.get("explanations"),
                                  distinguishing=decision.get("distinguishing"),
                                  resolution=decision.get("resolution"), **kw)
        if q["status"] == "open" and decision.get("reconsider_when"):
            goals.set_reconsider(goal_id, "question", qid, decision["reconsider_when"], **kw)
        if q["status"] != "open":
            # a resolved question that carried a challenge closes that challenge on the same evidence
            for ch in goals.challenges(goal_id, status="open", **kw):
                if ch.get("question_id") == qid:
                    res = decision.get("resolution") or {}
                    outcome = "established" if int(res.get("explanation") or 0) == 0 else "refuted"
                    goals.resolve_challenge(goal_id, ch["id"], outcome, by="architect",
                                            why=str(res.get("why") or "resolved with its question"),
                                            evidence_ref=str(res.get("evidence_ref") or ""), **kw)
                    return "resolved question {0}; challenge {1} {2}".format(qid, ch["id"], outcome)
        return "{0} question {1}".format("resolved" if q["status"] != "open" else "updated", qid)

    if act == PARK:
        goals.park(goal_id, decision["criterion_id"], decision["question"],
                   assignment=decision.get("assignment"), **kw)
        return "parked a question on {0}".format(decision["criterion_id"])

    if act == REPAIR:
        name = decision["assignment"]
        st = goals.state(goal_id, **kw)
        orig = dict(st["assignments"][name]["contract"])
        new_name = "{0}-r{1}".format(name, int(st["assignments"][name].get("attempts", 1)) + 1)
        if new_name in st["assignments"]:
            return "repair name {0} already exists; skipped".format(new_name)
        contract = planning.as_contract({
            **orig,
            "name": new_name,
            "brief": (orig.get("brief", "") + "\n\n--- REPAIR GUIDANCE (architect) ---\n"
                      + str(decision["brief_addition"]).strip() + _api_block(sources)),
            # A repair CONTINUES the original's obligation: it keeps the prerequisites the original
            # required, and records lineage so an accepted repair satisfies a dependent that still
            # `needs` the original name (goals._landed). Wiping needs to [] silently dropped real
            # prerequisites; that is fixed here.
            "needs": list(orig.get("needs") or []),
            "lineage": list(orig.get("lineage") or []) + [name],
        })
        # The bar comes from the ORIGINAL contract, never from the decision. TWO independent
        # guards, deliberately: the decision dict is never spread into the contract, AND these
        # fields are re-set from `orig` afterwards. Mutation testing showed either one alone is
        # sufficient and the test only goes red when BOTH are removed -- so this is defence in
        # depth, not a redundant line to tidy away. A repair changes instructions; it never
        # changes what acceptance means, and that is what makes unattended repair safe.
        contract["oracle"] = orig.get("oracle", "")
        contract["done_when"] = list(orig.get("done_when") or [])
        contract["criterion_id"] = orig.get("criterion_id")
        contract["oracle_covers_done_when"] = bool(orig.get("oracle_covers_done_when"))
        contract["origin"] = "repair"
        # Continuation (seam 1): carry the PARKED contribution + evidence into the repair rather than
        # only a rewritten brief. The parked deliverable is snapshotted immutably and staged as a
        # read-only `_carry/` reference in the repair worker's own workspace; prior feedback,
        # established facts, unresolved questions and lineage travel with it.
        try:
            carry, inp, carry_brief = carry_package(goal_id, name, orig, new_name, root=root)
            contract["carry"] = carry
            if inp:
                contract["inputs"] = list(contract.get("inputs") or []) + [inp]
            if carry_brief:
                contract["brief"] = contract.get("brief", "") + carry_brief
        except Exception as e:
            contract["carry_error"] = "carry package unavailable: {0}".format(e)[:200]
        goals.assign(goal_id, [contract], **kw)
        return "repaired {0} as {1}".format(name, new_name)

    if act == FOLLOWON:
        contract = planning.as_contract({
            "name": decision["name"], "criterion_id": decision["criterion_id"],
            "capability": str(decision.get("why", ""))[:200],
            "brief": decision["brief"],
            "done_when": decision.get("done_when") or ["the follow-on brief is satisfied"],
            "evidence": "architect follow-on; acceptance is the criterion's own check",
            "needs": [], "worker": None,
            "tools": goals.state(goal_id, **kw).get("tool_mode") == "tools",
            "oracle": decision.get("oracle", ""),
            "origin": "followon", "approach": decision.get("approach") or None,
        })
        goals.assign(goal_id, [contract], **kw)
        return "created follow-on {0} for {1}".format(decision["name"], decision["criterion_id"])

    return "no action"


RECONSIDER = """The skeptic has challenged your decision. You are still the decision maker.

Read the challenges. Then either REVISE the decision or RETAIN it, and say why in one or two
sentences. A challenge that misreads the evidence should be retained against, explicitly -- the
skeptic is the jury, not the judge, and it is sometimes simply wrong.

Reply with JSON ONLY:
  {"verdict": "REVISED", "why": "...", "decision": { ...a full replacement decision object... }}
  {"verdict": "RETAINED", "why": "..."}
"""


def reconsider(decision, doubt, delta, model=None, timeout=240, runner=None):
    """Put the skeptic's challenge in front of the architect BEFORE the action is applied.

    Appending skeptical prose to a record that was already acted on is not review; it is a
    transcript. The challenge has to reach the decision maker while the decision can still change,
    and whether the architect revised or retained has to be recorded either way -- a retained
    decision with a stated reason is a real outcome, not a failure of the skeptic.

    Returns (final_decision, verdict, why). On any parse failure the ORIGINAL decision stands:
    a confused reconsideration must not silently discard a decision that already validated."""
    prompt = (RECONSIDER
              + "\n\nYOUR DECISION:\n" + json.dumps(decision, indent=2)[:4000]
              + "\n\nSKEPTIC CHALLENGES:\n" + str(doubt)[:4000]
              + "\n\nSTATE:\n"
              + json.dumps({k: delta.get(k) for k in ("unmet", "criteria")},
                           indent=2)[:3000])
    if runner is not None:
        out = _extract_json(runner(prompt))
    else:
        try:
            from architect import ARCHITECT_MODEL
        except Exception:
            ARCHITECT_MODEL = "sonnet"
        exe = shutil.which("claude") or "claude"
        r = subprocess.run([exe, "-p", "--model", model or ARCHITECT_MODEL,
                            "--output-format", "text"],
                           input=prompt, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        out = _extract_json(r.stdout or "")
    if not isinstance(out, dict):
        return decision, "RETAINED", "no reconsideration returned; original decision stands"
    verdict = (out.get("verdict") or "").strip().upper()
    why = str(out.get("why", ""))[:400]
    if verdict == "REVISED" and isinstance(out.get("decision"), dict):
        return out["decision"], "REVISED", why
    return decision, "RETAINED", why or "architect retained the decision"


def challenge_root(goal_id, target):
    """Resolve the actual execution workspace, never the source repository."""
    if not target:
        return None
    assignment = goals.state(goal_id).get("assignments", {}).get(target)
    if assignment is None:
        return None
    contract = assignment.get("contract") or {}
    rid = goals.run_id(goal_id, target)
    if contract.get("tools"):
        import tooljob
        from verified import tool_workspace_id
        return tooljob.workspace_dir(tool_workspace_id(rid))
    return fleet_runs_root() / ("verified-" + rid)


def challenge(decision, delta, worker=SKEPTIC_WORKER, root=None, rounds=2, artifact_root=None):
    """Let the existing skeptic question the ARCHITECT's conclusion.

    Advisory by design. The skeptic is the jury; recording its doubt beside the decision is the
    point, because nothing else in this repo examines an architect conclusion at all."""
    from log import logger
    emit = logger("skeptic-challenge", echo=False)
    evidence_root = artifact_root or root
    target = decision.get("assignment")
    summary = "No assignment-specific generation evidence supplied."
    if target and delta.get("goal_id"):
        try:
            summary = generation_summary(goals.run_id(delta["goal_id"], target))[0]
        except Exception:
            summary = "Generation evidence unavailable."
    if not worker or not evidence_root or not Path(evidence_root).is_dir():
        note = "skeptic unavailable: review root missing or skeptic disabled; not evidence that no artifact exists"
        emit("skeptic_challenge", status="unavailable", target=target,
             evidence_root=str(evidence_root) if evidence_root else None, generation_summary=summary)
        emit.close()
        return note
    try:
        sys.path.insert(0, str(ROOT / "skeptic"))
        import skeptic as sk
    except Exception as e:
        emit("skeptic_challenge", status="unavailable", error=type(e).__name__)
        emit.close()
        return "skeptic unavailable: {0}".format(e)
    claim = ("The architect decided: {0}. Justification: {1}. Unmet criteria: {2}. "
             "Challenge whether this action can actually advance the goal.".format(
                 json.dumps({k: v for k, v in decision.items() if k not in ("brief",)}),
                 str(decision.get("why", ""))[:300], delta.get("unmet")))
    claim += ("\nRecorded generation evidence:\n" + summary[:6000] +
              "\nAssess a capacity adjustment using this evidence. An unfinished or absent artifact "
              "after truncation is not by itself a reason to reject an adjustment. If evidence is "
              "unavailable say so; do not infer missing work from missing access.")
    try:
        # Root the skeptic AT THE ARTIFACT. The first live run rooted it at the repo, where
        # the delivered file does not exist, and it produced three confident challenges
        # about code it had never read. A skeptic that cannot see the artifact does not
        # produce doubt, it produces fiction -- the exact failure the field scan warned of.
        answer = str(sk.run(claim, worker, str(evidence_root), rounds))[:1500]
        emit("skeptic_challenge", status="reviewed", target=target, evidence_root=str(evidence_root),
             generation_summary=summary, claim=claim, answer=answer)
        return answer
    except Exception as e:
        emit("skeptic_challenge", status="error", target=target, error=type(e).__name__)
        return "skeptic error: {0}".format(e)
    finally:
        emit.close()
