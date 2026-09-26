"""Experience-informed assignment (LF-05, bounded): what happened when THIS worker did THIS kind
of work before, and how that should change routing or the help it gets.

WHAT EXISTED. `queue.select_worker` ranks workers by capability flags, context size and capacity,
after health; the ledger records outcomes per assignment; nothing tied an outcome to the worker
that produced it, so the fleet assigned work as if every worker were new every night.

WHAT THIS ADDS. An append-only record per (worker, model, task family) of how work ended:

    accepted | implementation_failure | missing_information | infrastructure | unknown

with whether the attempt was ASSISTED (repair guidance, observed facts, staged skills or lessons
in the brief) or unassisted. Task family is the card's own vocabulary (tags of its capability and
criterion), not a taxonomy. Two things consume it, both AFTER capability, health and capacity:

  * `prefer(card, candidates)` reorders the qualified candidates: a worker with accepted results
    on overlapping work moves up; one with repeated implementation failures and no acceptance on
    it moves down -- never out. Cold-start workers keep the capability order and are labelled
    `uncertain`. A down-ranked worker is not trapped: after `RETRY_AFTER` skips on a family it is
    tried again, because the record is evidence about the past, not a verdict about the worker.
  * `assistance(worker, card)` returns the help that history says this worker needed on this
    kind of work (the failure reasons it hit unassisted), for the brief.

No percentages. Sparse history is reported as sparse. Nothing here changes what acceptance means.
"""
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
def _memory_dir():
    import fleet
    return fleet.memory_dir()


STORE = _memory_dir()   # FLEET_MEMORY_DIR, else <runs root>/memory (same store as memory.py)
FILE = "workers.jsonl"
RETRY_AFTER = 3          # skips on a family before a down-ranked worker is tried again
MIN_OVERLAP = 0.3
DISABLED = os.environ.get("FLEET_MEMORY_DISABLED", "") == "1"

ACCEPTED, IMPL, MISSING, INFRA, UNKNOWN = ("accepted", "implementation_failure",
                                           "missing_information", "infrastructure", "unknown")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _path():
    STORE.mkdir(parents=True, exist_ok=True)
    return STORE / FILE


def rows():
    p = _path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def family_of(card):
    """The card's own words for what it is: tags of capability + criterion text (+ brief head)."""
    import memory
    text = " ".join([str(card.get("capability") or ""), str(card.get("criterion_text") or ""),
                     str(card.get("brief") or "")[:300]])
    return sorted(memory.tags_for(text))[:24]


def classify(outcome, result=None):
    """One of the five classes, from the closed outcome vocabulary plus the run record."""
    o = (outcome or "").strip()
    if o == "accepted":
        return ACCEPTED
    if o in ("worker-unavailable", "CRASH", "parked-timeout"):
        return INFRA            # parked-timeout: the harness stopped waiting; says nothing about competence
    if o == "parked-undefined":
        return MISSING
    if o in ("parked-stagnant", "parked-max-rounds", "parked-unsupported"):
        # Attribution from the run's OWN recorded evidence, not just the decision basis. A parked run
        # whose rounds recorded a tool-limit / truncated tool response, or missing inputs / an
        # unresolvable transitive import, says nothing about the worker's competence -- crediting it
        # as an implementation failure would contaminate competence-based routing (seam 5). The
        # harness recorded WHY at the point it observed it (round recovery kind, failure_kind); this
        # propagates that rather than re-deriving it from a generic "no achievement" basis.
        r = result or {}
        basis = str(r.get("basis") or "")
        kinds, observed = set(), []
        for h in (r.get("history") or []):
            rec = h.get("recovery") or {}
            if rec.get("kind"):
                kinds.add(str(rec.get("kind")))
            if rec.get("observed"):
                observed.append(str(rec.get("observed")))
            if h.get("failure_kind"):
                kinds.add(str(h.get("failure_kind")))
        fk = str(r.get("failure_kind") or "")
        blob = " ".join([basis, fk] + observed)
        # tool interruption / round-limit / truncated model output: infrastructure, not competence
        if (fk in ("tool-limit", "truncated", "interrupted")
                or "tool-limit" in kinds or "truncated" in kinds
                or any(k in blob for k in ("checkpoint retained", "tool-round", "truncat",
                                           "tool response was truncated"))):
            return INFRA
        # missing declared/transitive material or an unreachable probe: missing information. Prefer
        # the harness's own recovery kind over blanket import-error matching, so a genuine behavioural
        # failure that happens to mention an import is NOT silently exempted.
        if ("missing_inputs_or_tools" in kinds
                or any(k in blob for k in ("ModuleNotFoundError", "No module named",
                                           "does not exist", "NOT ACCESSIBLE"))):
            return MISSING
        return IMPL
    if o == "awaiting-review":
        return ACCEPTED           # machine-complete; the person's part is not the worker's failure
    return UNKNOWN


def assisted(card):
    """Did the brief carry help beyond the plan: repair guidance, observed facts, staged skills,
    retained lessons, an API block?"""
    b = str(card.get("brief") or "")
    return any(k in b for k in ("REPAIR GUIDANCE", "OBSERVED FACTS", "EXACT PUBLIC API"))\
        or bool(card.get("skills")) or bool(card.get("experience"))


def record_outcome(worker, model, card, outcome, result=None, *, run_id="", reason=""):
    if DISABLED or not worker:
        return None
    row = {"worker": worker, "model": model or "?", "family": family_of(card),
           "outcome": outcome, "class": classify(outcome, result), "assisted": assisted(card),
           "run_id": run_id, "reason": (reason or "")[:300], "ts": _now(),
           "assignment": card.get("assignment") or card.get("name")}
    with open(_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def _overlap(a, b):
    a, b = set(a or ()), set(b or ())
    return len(a & b) / float(len(a) or 1)


def history(worker, card, min_overlap=MIN_OVERLAP):
    """This worker's records on work overlapping this card's family."""
    fam = family_of(card)
    out = []
    for r in rows():
        if r.get("worker") != worker or r.get("skip"):
            continue
        if _overlap(fam, r.get("family")) >= min_overlap:
            out.append(r)
    return out


def summarize(worker, card):
    h = history(worker, card)
    counts = {k: 0 for k in (ACCEPTED, IMPL, MISSING, INFRA, UNKNOWN)}
    unassisted_fail, assisted_ok = 0, 0
    for r in h:
        counts[r.get("class", UNKNOWN)] = counts.get(r.get("class", UNKNOWN), 0) + 1
        if r.get("class") == IMPL and not r.get("assisted"):
            unassisted_fail += 1
        if r.get("class") == ACCEPTED and r.get("assisted"):
            assisted_ok += 1
    return {"n": len(h), "counts": counts, "unassisted_failures": unassisted_fail,
            "assisted_acceptances": assisted_ok,
            "reasons": [r.get("reason") for r in h if r.get("class") == IMPL and r.get("reason")][-3:],
            "certainty": "none" if not h else ("sparse" if len(h) < 3 else "some")}


def _skips(worker, fam):
    n = 0
    for r in reversed(rows()):
        if r.get("worker") == worker and r.get("skip") and _overlap(fam, r.get("family")) >= MIN_OVERLAP:
            n += 1
        elif r.get("worker") == worker and not r.get("skip") and _overlap(fam, r.get("family")) >= MIN_OVERLAP:
            break
    return n


def prefer(card, candidates):
    """Reorder capability-qualified candidates by history. Returns (ordered, notes)."""
    if DISABLED or len(candidates) < 2:
        return list(candidates), {c: "single candidate" if len(candidates) == 1 else "" for c in candidates}
    fam = family_of(card)
    scored, notes = [], {}
    for i, w in enumerate(candidates):
        s = summarize(w, card)
        c = s["counts"]
        if s["n"] == 0:
            tier, note = 1, "no history on this kind of work (uncertain; capability order kept)"
        elif c[ACCEPTED] > 0 and c[IMPL] <= c[ACCEPTED]:
            tier, note = 0, "{0} accepted on similar work ({1} records)".format(c[ACCEPTED], s["n"])
        elif c[IMPL] >= 2 and c[ACCEPTED] == 0:
            if _skips(w, fam) >= RETRY_AFTER:
                tier, note = 1, ("{0} implementation failures, none accepted; skipped {1} times -- "
                                 "tried again so the record cannot trap it".format(c[IMPL], RETRY_AFTER))
            else:
                tier, note = 2, "{0} implementation failures and no acceptance on similar work".format(c[IMPL])
        else:
            tier, note = 1, "mixed or sparse history ({0} records): capability order kept".format(s["n"])
        infra = c[INFRA]
        if infra and s["n"] == infra:
            note += "; only infrastructure failures recorded, which say nothing about competence"
        scored.append((tier, i, w))
        notes[w] = note
    scored.sort()
    ordered = [w for _, _, w in scored]
    # record skips for down-ranked workers so the retry rule can count them
    chosen = ordered[0]
    for tier, _, w in scored:
        if tier == 2 and w != chosen:
            with open(_path(), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"worker": w, "family": fam, "skip": True, "ts": _now(),
                                     "for": card.get("assignment") or card.get("name")}) + "\n")
    return ordered, notes


def assistance(worker, card):
    """Help this worker's own record says it needed on this kind of work, or ""."""
    if DISABLED or not worker:
        return ""
    s = summarize(worker, card)
    if s["unassisted_failures"] >= 1 and s["reasons"]:
        return ("ASSISTANCE from this worker's record on similar work ({0} unassisted attempt(s) "
                "failed): earlier failures read: {1}. Address these explicitly.".format(
                    s["unassisted_failures"], " | ".join(r[:140] for r in s["reasons"])))
    return ""
