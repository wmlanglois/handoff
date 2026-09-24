"""The durable goal ledger: what the operator approved, what has actually been established, what
is still an open question, and how much of the budget has been spent getting there.

WHY THIS EXISTS. The conductor used to hold a goal in a local variable: it planned a list of small
jobs, ran them in order, printed a count and exited. Nothing survived the process, so "where is this
goal up to" could only be answered by a human re-reading a terminal, and the answer to "is it
finished" was "the job list is empty". Those are different questions. An empty queue with unmet
criteria is BLOCKED, and a harness that cannot tell blocked from done will cheerfully report success
for work it never did.

Three separations are load-bearing here, and each one exists because collapsing it produced a wrong
answer in this repo before:

  * queue state is not goal state.  `complete()` never consults the work list. It reads the
    criteria and the evidence attached to them. Nothing makes it true except recorded evidence.
  * a parked question is not a failure, and human review is not completion.  `run/verified.py`
    distinguishes `awaiting-review` (machine done, a person must look) from `parked-undefined`
    (nobody ever said what the target was) from `worker-unavailable` (the fleet broke, the job was
    fine). DISPOSITION item 15 is what happens when those are folded together: human-only criteria
    were silently reported as ACCEPTED. This ledger keeps all four apart.
  * infrastructure failure is not progress and not blame.  A `worker-unavailable` returns the
    assignment to ready and costs a retry; it does not park the criterion, because the criterion
    was never the problem.

DURABILITY BY HAVING NO CACHE. Every function reads the goal file and writes it back atomically.
There is no in-memory ledger object to go stale, so "survives a restart" is not a feature that can
rot: a process that has just started and a process that has been running an hour read the same
bytes. Goal files live under `runs/` (gitignored) or wherever `FLEET_GOALS_DIR` points.

Interface I7 in internal/docs/INTERFACES.md is the contract. Consumers read; only this module writes.
"""
import json
import contextlib
import threading
import functools
import os
import re
import sys
import time
import uuid
from pathlib import Path
from state_lock import exclusive_file

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "run"))      # so `plan` resolves however this module was imported

# --- criterion status -----------------------------------------------------------------------
# Deliberately five values, not two. "open vs closed" is the shape that loses information: it
# cannot express "a machine finished this and a person still has to look", which is the single
# most common honest end state on this fleet.
OPEN = "open"          # nothing established yet
MET = "met"            # accepted, with an evidence reference
REVIEW = "review"      # machine-complete, a human-only clause is outstanding
PARKED = "parked"      # an open QUESTION blocks it -- undefined target, unbound artifact, stall
BLOCKED = "blocked"    # infrastructure or a dependency, not the work itself

# --- assignment status ----------------------------------------------------------------------
READY = "ready"
RUNNING = "running"
DONE = "done"

# How a terminal job outcome moves the criterion it was advancing.
#
# The keys are the CLOSED outcome vocabulary of interface I3, owned by Track A. This table is a
# mirror, not a second source of truth: tests/test_goals.py asserts it covers exactly
# `verified.TERMINAL` plus `worker-unavailable`, so if A adds a member without telling C, the
# tripwire fires here rather than the new outcome being silently swallowed as "some kind of park".
OUTCOME_EFFECT = {
    "accepted": MET,
    "awaiting-review": REVIEW,
    "parked-undefined": PARKED,
    "parked-unsupported": PARKED,
    "parked-stagnant": PARKED,
    "parked-max-rounds": PARKED,
    "worker-unavailable": "retry",      # not a criterion state: the fleet failed, not the work
}

# A wedged worker must not consume a goal forever. After this many consecutive infrastructure
# failures the assignment stops being re-offered and says so, instead of cycling ready->ready
# and burning the budget on a worker that is not coming back.
MAX_INFRA_ATTEMPTS = 3


class GoalError(Exception):
    """A malformed ledger operation. Raised, never swallowed: a goal that silently accepts a
    record for a criterion that does not exist looks like progress and leaves the real criterion
    open forever."""


# =================================================================================================
# storage
# =================================================================================================

def goals_dir(root=None):
    if root:
        return Path(root)
    env = os.environ.get("FLEET_GOALS_DIR")
    return Path(env) if env else ROOT / "runs" / "goals"


def _path(goal_id, root=None):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", goal_id or ""):
        # A goal id reaches the filesystem. Anything path-shaped ("../../x") would write outside
        # the ledger directory, so ids are restricted at the door rather than sanitised later.
        raise GoalError(f"unusable goal id: {goal_id!r}")
    return goals_dir(root) / f"{goal_id}.json"


class StaleWrite(GoalError):
    """A write built from a document that is no longer current. Refused, never merged."""


def _load(goal_id, root=None):
    """Load the document AND remember the revision it was built from.

    `_rev` is the correctness mechanism, not the lock. The lock is an optimisation that keeps
    writers out of each other's way in the normal case; it cannot help once a holder has been
    legitimately taken over, because that holder is still running and still holding a document it
    read before the takeover."""
    p = _path(goal_id, root)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except OSError:
        raise GoalError(f"no such goal: {goal_id} (looked in {p.parent})")
    doc.setdefault("_rev", 0)
    return doc


def _disk_rev(p):
    try:
        return int(json.loads(p.read_text(encoding="utf-8")).get("_rev", 0))
    except (OSError, ValueError, TypeError):
        return None


COMMIT_STALE_S = 120        # a reserved-but-unpublished revision marker older than this = a dead writer


def _marker_is_stale(marker, now=None):
    """A commit marker whose writer never published within COMMIT_STALE_S is an orphan. Publishing
    takes milliseconds, so this never mistakes a live mid-commit writer for a dead one."""
    try:
        age = (now or time.time()) - marker.stat().st_mtime
    except OSError:
        return True         # gone already: treat as reclaimable
    return age > COMMIT_STALE_S


def _replace_with_retry(src, dst, tries=40, wait=0.05):
    """os.replace, retried past Windows' transient sharing errors.

    Publishing is atomic on POSIX and *usually* atomic on Windows -- but a concurrent READER with
    the destination open makes os.replace fail with WinError 5 (access denied). Under contention
    that is common: the writers that are about to be refused are reading the same ledger to learn
    the current revision. Observed as an intermittent crash in one of six racing writers, which
    looked exactly like a lost commit until the test stopped swallowing the exception.

    Retrying is correct rather than a workaround: the condition is transient by construction, the
    readers hold the handle for microseconds, and the alternative -- a partially published ledger
    -- does not exist because the write still lands whole or not at all."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(wait)


def _save(doc, root=None):
    p = _path(doc["goal_id"], root)
    base = int(doc.get("_rev", 0))

    # Covers BOTH revision comparison and publication under ONE exclusive lock (reentrant when the
    # caller already holds _locked()). No expiring marker, so a paused writer cannot be superseded
    # and then overwrite: it simply still holds the lock.
    with _locked(doc["goal_id"], root):
        try:
            current = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            current = None
        except (OSError, ValueError) as exc:
            raise GoalError(
                "Cannot safely read ledger before publication: {0}".format(p)) from exc

        disk_revision = (
            int(current.get("_rev", 0)) if current is not None else None
        )
        if disk_revision is None:
            if base != 0:
                raise StaleWrite("Existing ledger disappeared; refusing recreation")
        elif disk_revision != base:
            raise StaleWrite(
                "Expected revision {0}; found {1}".format(base, disk_revision))

        updated = dict(doc)
        updated["_rev"] = base + 1
        tmp = p.with_name(".{0}.{1}.tmp".format(p.name, uuid.uuid4().hex))

        try:
            with tmp.open("x", encoding="utf-8", newline="\n") as fh:
                json.dump(updated, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            _replace_with_retry(tmp, p)
        finally:
            tmp.unlink(missing_ok=True)

        doc["_rev"] = updated["_rev"]
        return p


def _prune_commit_markers(p, current, keep=5):
    """Keep a short tail of commit markers; they are a record, not a leak."""
    for m in p.parent.glob(".{0}.rev*.commit".format(p.stem)):
        try:
            n = int(m.name.rsplit(".rev", 1)[1].split(".commit")[0])
        except (IndexError, ValueError):
            continue
        if n <= current - keep:
            try:
                m.unlink()
            except OSError:
                pass



_HELD = threading.local()   # goal ids this thread already holds: the lock is re-entrant

LOCK_STALE_S = 120          # a lock older than this belonged to a process that died holding it


@contextlib.contextmanager
def _locked(goal_id, root=None, timeout=30.0, poll=0.05):
    """Serialise read-modify-write on one goal across PROCESSES with an OS advisory lock that is
    NEVER stolen for being old. A paused holder keeps it; process death releases it (OS-level).
    This replaces the age-based lockfile whose failure mode was mistaking a paused holder for a
    dead one. Requires one shared, lock-coherent state directory."""
    p = _path(goal_id, root)
    # A new filename avoids conflict with legacy, deletable .lock files.
    with exclusive_file(p.with_suffix(".state-lock"), timeout=timeout):
        yield


def lock_token_on_disk(goal_id, root=None):
    """The current holder's fencing token, or "". Exposed so tests can assert OWNERSHIP rather
    than merely that a file exists. The token stops an expired holder deleting a successor's
    lockfile; it is not what protects the ledger -- the atomic revision claim in _save is."""
    try:
        return _path(goal_id, root).with_suffix(".lock").read_text(encoding="utf-8")
    except OSError:
        return ""


def _serialised(fn):
    """Every ledger mutation holds the goal lock, not just `claim`.

    `claim` was locked and `record` was not. Both read the whole document, edit it and write it
    back, so an unlocked writer could interleave with a locked one. Locking one writer is not
    locking: if it calls _save, it holds the lock. The lock is re-entrant so a mutator may call
    another, and it is an OPTIMISATION -- correctness comes from the atomic revision claim in
    _save, which refuses a stale write even when the lock was lost or never held."""
    @functools.wraps(fn)
    def wrapper(goal_id, *a, **kw):
        with _locked(goal_id, kw.get("root")):
            return fn(goal_id, *a, **kw)
    return wrapper


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _event(doc, kind, **fields):
    doc.setdefault("events", []).append(dict(kind=kind, ts=_now(), **fields))


# =================================================================================================
# I7
# =================================================================================================

def create(goal_text, criteria, *, budget=None, root=None, goal_id=None, project_root=None,
           integration=None):
    """Open a goal. `criteria` are strings, or dicts {id, text, human_only}.

    A goal starts UNAPPROVED and with no plan. `next_work` yields nothing until a human approves
    both, because "the harness assigns work without repeated human prompting" means one approval
    up front, not zero.
    """
    if not (goal_text or "").strip():
        raise GoalError("a goal with no text cannot be checked against anything")
    norm = []
    for i, c in enumerate(criteria or [], 1):
        if isinstance(c, str):
            c = {"text": c}
        text = (c.get("text") or "").strip()
        if not text:
            raise GoalError("a criterion with no text is not a criterion")
        norm.append({"id": c.get("id") or f"c{i}", "text": text,
                     "human_only": bool(c.get("human_only")),
                     # Declared by the person at creation and approved with the plan: the only
                     # authorized statement of how much this criterion matters. Structural
                     # importance (dependents, sole remaining) is computed; this is not.
                     "priority": c.get("priority", "normal"),
                     "status": OPEN, "evidence": [], "outcomes": []})
    if not norm:
        raise GoalError("a goal with no criteria can never be complete or blocked, only forgotten")
    ids = [c["id"] for c in norm]
    if len(set(ids)) != len(ids):
        raise GoalError(f"duplicate criterion ids: {ids}")
    gid = goal_id or ("g-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4])
    doc = {
        "goal_id": gid,
        "goal": goal_text.strip(),
        # The project the work lives in. Declared relative sources ("datakit/money.py") resolve
        # against THIS root, not the fleet checkout, so the planner need not know absolute paths.
        "project_root": str(project_root) if project_root else None,
        "created": _now(),
        "approved": False,
        "approved_by": None,
        "criteria": norm,
        "proposed": [],          # contracts awaiting human approval
        "assignments": {},       # name -> assignment record (the approved plan, plus follow-ons)
        "parked": [],            # open questions, each naming the criterion it blocks
        "budget": {"assignments": (budget or {}).get("assignments"),
                   "consumed": 0,
                   "seconds_limit": (budget or {}).get("seconds"),
                   "seconds": 0.0},
        "events": [],
        "integration": {"declared": False},
    }
    if integration:
        import integrate
        spec = integrate.validate_spec(integration, project_root=project_root)
        doc["integration"] = {
            "declared": True,
            "groups": spec["groups"],
            "check_source": spec["check_source"],
            "check_sha256": spec["check_sha256"],
            "project_root": spec["project_root"],
            "status": {},
        }
    _event(doc, "created", criteria=len(norm))
    _save(doc, root)
    return gid


def state(goal_id, root=None):
    """The whole ledger as a plain dict: criteria, evidence, parked questions, consumed budget.
    Read-only by contract -- mutating the returned dict changes nothing on disk."""
    return _load(goal_id, root)


@_serialised
def propose(goal_id, contracts, *, root=None):
    """Record a plan for the human to approve. `contracts` are dicts from run/plan.py.

    Each outcome is bound to its criterion structurally (by id) and the criterion's EXACT source text
    is preserved on the outcome as `criterion_source` -- structured planning evidence, so the binding
    is auditable without forcing the planner to paste the criterion's prose into its brief/oracle."""
    doc = _load(goal_id, root)
    by_id = {c["id"]: c for c in (doc.get("criteria") or [])}
    stored = []
    for c in contracts:
        c = dict(c)
        crit = by_id.get(c.get("criterion_id"))
        if crit:
            c["criterion_source"] = crit.get("text", "")
        stored.append(c)
    doc["proposed"] = stored
    _event(doc, "proposed", count=len(doc["proposed"]))
    _save(doc, root)
    return len(doc["proposed"])


@_serialised
def approve(goal_id, approver, *, root=None):
    """The human approves the goal AND the proposed plan in one act. Everything the harness does
    afterwards happens under this approval -- that is the whole point of the ledger. Follow-on
    outcomes added later by `assign` inherit it; a NEW goal needs a new approval."""
    doc = _load(goal_id, root)
    if not (approver or "").strip():
        raise GoalError("approval needs an approver; an anonymous approval is not one")
    doc["approved"] = True
    doc["approved_by"] = approver
    for c in doc.pop("proposed", []):
        _add_assignment(doc, c)
    doc["proposed"] = []
    integ = doc.get("integration") or {}
    if integ.get("declared"):
        import integrate
        spec = {
            "groups": integ.get("groups") or [],
            "check_source": integ.get("check_source") or "",
            "project_root": integ.get("project_root") or doc.get("project_root"),
        }
        meta = integrate.bootstrap(goal_id, spec)
        doc["integration"]["status"] = meta.get("groups") or {}
        doc["integration"]["bootstrapped"] = True
    _event(doc, "approved", by=approver, assignments=len(doc["assignments"]))
    _save(doc, root)
    return True


@_serialised
def record_integration(goal_id, spec, *, root=None):
    """Persist the approved integration declaration on the ledger (same lock as other writes)."""
    doc = _load(goal_id, root)
    import integrate
    spec = integrate.validate_spec(spec, project_root=spec.get("project_root") or doc.get("project_root"))
    doc["integration"] = {
        "declared": True,
        "groups": spec["groups"],
        "check_source": spec["check_source"],
        "check_sha256": spec["check_sha256"],
        "project_root": spec["project_root"],
        "status": (doc.get("integration") or {}).get("status") or {},
    }
    meta = integrate.bootstrap(goal_id, spec)
    doc["integration"]["status"] = meta.get("groups") or {}
    doc["integration"]["bootstrapped"] = True
    _event(doc, "integration_declared", groups=[g["id"] for g in spec["groups"]])
    _save(doc, root)
    return doc["integration"]


@_serialised
def set_unapproved_text(goal_id, goal_text, *, root=None):
    """Replace the page on a goal that has not been approved, and drop the plan built for the old page."""
    doc = _load(goal_id, root)
    if doc.get("approved"):
        raise GoalError("an approved goal keeps its page until a person opens a new one")
    doc["goal"] = goal_text
    doc["proposed"] = []
    doc["plan_rejections"] = []
    doc["parked"] = [q for q in (doc.get("parked") or []) if q.get("outcome") != "plan-rejected"]
    _event(doc, "retargeted")
    _save(doc, root)
    return doc["goal_id"]


@_serialised
def clear_rejected_plan(goal_id, *, root=None):
    """Drop a rejected plan so the same goal can be planned again. Does not create a second goal."""
    doc = _load(goal_id, root)
    if doc.get("approved"):
        raise GoalError("an approved plan is not cleared from here")
    doc["proposed"] = []
    doc["plan_rejections"] = []
    doc["parked"] = [q for q in (doc.get("parked") or []) if q.get("outcome") != "plan-rejected"]
    _event(doc, "plan_cleared")
    _save(doc, root)
    return doc["goal_id"]


@_serialised
def stage_integration(goal_id, spec, *, root=None):
    """Record groups, destinations' check, and project_root. approve() is what freezes the tree."""
    doc = _load(goal_id, root)
    if doc.get("approved"):
        raise GoalError("declare integration before approval; approve() freezes it")
    import integrate
    spec = integrate.validate_spec(spec, project_root=spec.get("project_root") or doc.get("project_root"))
    doc["integration"] = {
        "declared": True,
        "groups": spec["groups"],
        "check_source": spec["check_source"],
        "check_sha256": spec["check_sha256"],
        "project_root": spec["project_root"],
        "status": {},
        "bootstrapped": False,
    }
    _event(doc, "integration_staged", groups=[g["id"] for g in spec["groups"]])
    _save(doc, root)
    return doc["integration"]


@_serialised
def set_run_policy(goal_id, *, review_cadence="", review_letter="", domain_branch="", limits=None, root=None):
    """E3 and the confirmed never-rules, stored where the run loop can read them."""
    doc = _load(goal_id, root)
    doc["review_cadence"] = review_cadence or ""
    doc["review_letter"] = review_letter or ""
    doc["domain_branch"] = domain_branch or ""
    doc["limits"] = dict(limits or {})
    _event(doc, "run_policy", review_letter=doc["review_letter"])
    _save(doc, root)
    return doc["review_letter"]


@_serialised
def add_criterion(goal_id, criterion, *, root=None):
    """Add one criterion to an unapproved goal. Used when a prose example needs a person."""
    doc = _load(goal_id, root)
    if doc.get("approved"):
        raise GoalError("an approved goal's criteria stay until a person opens a new plan")
    cid = criterion.get("id")
    if any(c["id"] == cid for c in doc["criteria"]):
        return cid
    text = (criterion.get("text") or "").strip()
    if not cid or not text:
        raise GoalError("a criterion needs an id and text")
    doc["criteria"].append({"id": cid, "text": text, "human_only": bool(criterion.get("human_only")),
                            "priority": "normal", "status": OPEN, "evidence": [], "outcomes": []})
    _event(doc, "criterion_added", criterion=cid)
    _save(doc, root)
    return cid


@_serialised
def record_integration_status(goal_id, meta, *, root=None):
    """Write group/checkpoint status through the ledger lock."""
    doc = _load(goal_id, root)
    integ = doc.setdefault("integration", {"declared": True})
    integ["declared"] = True
    integ["status"] = (meta or {}).get("groups") or {}
    integ["owners"] = (meta or {}).get("owners") or {}
    _save(doc, root)
    return integ


def _add_assignment(doc, contract):
    name = contract.get("name")
    if not name:
        raise GoalError("an assignment with no name cannot be tracked or depended on")
    if name in doc["assignments"]:
        raise GoalError(f"duplicate assignment name: {name}")
    cid = contract.get("criterion_id")
    known = {c["id"] for c in doc["criteria"]}
    if cid not in known:
        # An assignment that advances no criterion of this goal is exactly the errand the
        # delegation check exists to reject; refusing it here as well means a caller that
        # bypasses run/plan.py still cannot smuggle one in.
        raise GoalError(f"assignment {name!r} names criterion {cid!r}, not one of {sorted(known)}")
    doc["assignments"][name] = {"contract": dict(contract), "status": READY,
                                "attempts": 0, "infra_failures": 0, "outcomes": [],
                                "added": _now(),
                                # DURABLE IDENTITY (LF-04). `name` is what a person reads and
                                # what this ledger is keyed by; `run_id` is what the queue, the
                                # workspace, the receipt and the memory store are keyed by. Two
                                # goals may both call an assignment "totals"; they must never
                                # share a workspace, or the second run overwrites the evidence
                                # the first acceptance and its lessons point at (it did, live).
                                "run_id": _run_id(doc["goal_id"], name)}


def _run_id(goal_id, name):
    return "{0}.{1}".format(name, goal_id)


def run_id(goal_id, name, *, root=None, doc=None):
    """The execution identity of one assignment: workspace `runs/verified-<run_id>`, queue file
    `<run_id>.json`, receipt job id. Assignments recorded before identities existed have none and
    keep their legacy `name` key, so their old workspaces stay inspectable under the old path."""
    doc = doc or _load(goal_id, root)
    a = (doc.get("assignments") or {}).get(name)
    if a is None:
        return _run_id(goal_id, name)
    return a.get("run_id") or name


@_serialised
def assign(goal_id, contracts, *, root=None):
    """Add follow-on outcomes to an already-approved goal.

    This is how the harness keeps choosing useful work without asking again: the human approved
    the GOAL and the shape of the plan; discovering that criterion c3 needs one more outcome is
    the harness doing its job, not a new mandate. It refuses on an unapproved goal so that
    approval cannot be skipped by calling this instead of `propose`."""
    doc = _load(goal_id, root)
    if not doc.get("approved"):
        raise GoalError("cannot assign work on an unapproved goal; call propose() then approve()")
    for c in contracts:
        if (c.get("origin") or "") == "integration-repair":
            import integrate
            if integrate.vacuous_oracle(c.get("oracle")):
                raise GoalError(
                    "integration-repair {0} refuses a vacuous oracle; the frozen integration "
                    "check is the mechanical bar".format(c.get("name") or "?"))
        _add_assignment(doc, c)
    _event(doc, "assigned", names=[c.get("name") for c in contracts])
    _save(doc, root)
    return len(contracts)


@_serialised
def record(goal_id, criterion_id, outcome, evidence_ref, *, assignment=None, seconds=0.0,
           detail="", root=None):
    """Fold one terminal job outcome into the ledger.

    The guards here are the ones that keep `complete()` honest:
      * an unknown criterion raises. A typo'd id used to create nothing and leave the real
        criterion open while the run looked like it was advancing.
      * `accepted` without an evidence reference raises. Acceptance whose evidence nobody can name
        is the failure mode DISPOSITION item 18 describes -- a verdict with nothing behind it.
      * `worker-unavailable` does not touch the criterion at all. The fleet broke; the work did
        not fail, and marking it parked would turn an outage into a permanent open question.
    """
    doc = _load(goal_id, root)
    crit = _criterion(doc, criterion_id)
    if outcome not in OUTCOME_EFFECT:
        raise GoalError(f"unknown outcome {outcome!r}; the vocabulary in I3 is closed "
                        f"(known: {sorted(OUTCOME_EFFECT)})")
    effect = OUTCOME_EFFECT[outcome]
    if effect == MET and not (evidence_ref or "").strip():
        raise GoalError("accepted requires an evidence_ref; an acceptance nobody can inspect is "
                        "not evidence of anything")

    doc["budget"]["seconds"] = round(float(doc["budget"].get("seconds", 0.0)) + float(seconds or 0), 3)
    row = {"outcome": outcome, "evidence_ref": evidence_ref, "assignment": assignment,
           "detail": detail, "ts": _now()}
    crit["outcomes"].append(row)

    a = doc["assignments"].get(assignment) if assignment else None
    if a is not None:
        a["outcomes"].append(row)

    if effect == "retry":
        # Infrastructure. Return the work to the pool, count the failure, and stop re-offering it
        # once the fleet has proved it is not coming back -- but as BLOCKED (an infrastructure
        # statement), never as PARKED (a question for a human about the work itself).
        if a is not None:
            a["infra_failures"] += 1
            if a["infra_failures"] >= MAX_INFRA_ATTEMPTS:
                a["status"] = DONE
                a["disposition"] = "blocked-infrastructure"
                crit["status"] = BLOCKED
                crit["blocked_reason"] = (f"{a['infra_failures']} consecutive worker-unavailable "
                                          f"outcomes on {assignment}")
            else:
                a["status"] = READY
        _event(doc, "infrastructure", criterion=criterion_id, assignment=assignment)
        _save(doc, root)
        return

    if a is not None:
        a["status"] = DONE
        a["disposition"] = outcome
        a["infra_failures"] = 0

    if (crit["status"] == MET and effect in (REVIEW, PARKED)) or (crit["status"] == REVIEW and effect == PARKED):
        # A sibling approach that parks AFTER another approach established the criterion must
        # not un-meet it. Live (goal totals4): `sum` parked seconds after `loop` was accepted,
        # the criterion flipped back to PARKED with a question, and a DONE goal reported
        # BLOCKED. Likewise a later park must not erase REVIEW: machine-complete work awaiting
        # a person (live, planner goal r2) was re-parked by a pointless repair's failure.
        # The assignment's own record keeps the park; the criterion keeps its evidence.
        _event(doc, "recorded", criterion=criterion_id, outcome=outcome, assignment=assignment,
               note="criterion already met by another assignment; status unchanged")
        _save(doc, root)
        return

    if effect == MET:
        crit["status"] = MET
        crit["evidence"].append(evidence_ref)
        # A question about a criterion that has now been established with evidence is answered.
        # Leaving it open would make `complete()` unreachable for any goal that ever stumbled.
        for q in doc["parked"]:
            if q["criterion_id"] == criterion_id and not q.get("resolved"):
                q["resolved"] = _now()
    elif effect == REVIEW:
        crit["status"] = REVIEW
        if evidence_ref:
            crit["evidence"].append(evidence_ref)
    elif effect == PARKED:
        crit["status"] = PARKED
        doc["parked"].append({"criterion_id": criterion_id, "assignment": assignment,
                              "outcome": outcome,
                              "question": detail or _default_question(outcome, crit),
                              "asked": _now(), "resolved": None})
    _event(doc, "recorded", criterion=criterion_id, outcome=outcome, assignment=assignment)
    _save(doc, root)


def _default_question(outcome, crit):
    if outcome == "parked-undefined":
        return (f"criterion {crit['id']!r} has no target: what value or artifact would satisfy "
                f"{crit['text']!r}?")
    if outcome == "parked-unsupported":
        return (f"the checks for {crit['id']!r} passed but nothing bound the artifact to the work; "
                f"what evidence should count?")
    return f"work on {crit['id']!r} ended as {outcome}; how should it proceed?"


@_serialised
def park(goal_id, criterion_id, question, *, assignment=None, root=None):
    """Record an open question against a criterion without a job outcome -- e.g. the conductor
    itself noticed the requirement is undefined. Parked questions survive restart and are part of
    the reason a goal can be blocked rather than done."""
    doc = _load(goal_id, root)
    crit = _criterion(doc, criterion_id)
    if not (question or "").strip():
        raise GoalError("a parked question with no question is just a stall")
    crit["status"] = PARKED
    doc["parked"].append({"criterion_id": criterion_id, "assignment": assignment,
                          "outcome": "parked-undefined", "question": question.strip(),
                          "asked": _now(), "resolved": None})
    _event(doc, "parked", criterion=criterion_id)
    _save(doc, root)


def _criterion(doc, criterion_id):
    for c in doc["criteria"]:
        if c["id"] == criterion_id:
            return c
    raise GoalError(f"no criterion {criterion_id!r} on goal {doc['goal_id']}; "
                    f"known: {[c['id'] for c in doc['criteria']]}")


@_serialised
def resolve(goal_id, question_index=None, *, answer, by, criterion_id=None, reopen=True,
            root=None):
    """Answer a parked question and, by default, return its criterion to OPEN so work resumes.

    THE GAP THIS CLOSES. `park` could record a question and nothing could ever answer one, so a
    criterion that was parked stayed parked and the goal could never move again. The loop noticed:
    an architect reading "two open questions awaiting a human" correctly refused to act and stopped,
    which is honest and permanently stuck at the same time.

    `by` is mandatory and recorded. An answer with no answerer is exactly the unattributed judgement
    this ledger exists to prevent, and it matters here more than anywhere else, because a resolution
    lets the machine start spending again.

    This does NOT mark the criterion met. Answering a question unblocks work; it does not do the
    work, and it is not evidence that the work succeeded. Use `sign_off` for a human verdict."""
    doc = _load(goal_id, root)
    if not (answer or "").strip():
        raise GoalError("a resolution with no answer resolves nothing")
    if not (by or "").strip():
        raise GoalError("a resolution needs an answerer; an anonymous one is not a decision")
    opens = [i for i, q in enumerate(doc["parked"]) if not q.get("resolved")
             and (criterion_id is None or q.get("criterion_id") == criterion_id)]
    if question_index is not None:
        opens = [i for i in opens if i == question_index]
    if not opens:
        raise GoalError("no open question matches that selection")
    touched = []
    for i in opens:
        doc["parked"][i]["resolved"] = _now()
        doc["parked"][i]["answer"] = answer.strip()
        doc["parked"][i]["answered_by"] = by.strip()
        touched.append(doc["parked"][i].get("criterion_id"))
    reopened = []
    if reopen:
        touched_ids = set(c for c in touched if c)
        for cid in touched_ids:
            crit = _criterion(doc, cid)
            if crit["status"] == PARKED:
                crit["status"] = OPEN
        # A question that was a harness refusal, not a finished worker, has to put the
        # assignment back on the queue. One destination, one assignment: a later
        # parked repair of the same path stays done so two writers are not offered.
        by_dest = {}
        for name, a in doc["assignments"].items():
            if a.get("status") != DONE or a.get("disposition") != "parked-undefined":
                continue
            con = a.get("contract") or {}
            if con.get("criterion_id") not in touched_ids:
                continue
            if any(o.get("outcome") == "accepted" for o in (a.get("outcomes") or [])):
                continue
            dest = (con.get("dest") or name).replace("\\", "/")
            by_dest.setdefault(dest, []).append((str(a.get("added") or ""), name, a))
        for rows in by_dest.values():
            rows.sort()
            _added, name, a = rows[0]
            a["status"] = READY
            a["disposition"] = ""
            reopened.append(name)
    _event(doc, "resolved", count=len(touched), by=by.strip(), criteria=sorted(set(touched)),
           reopened=reopened)
    _save(doc, root)
    return touched


@_serialised
def sign_off(goal_id, criterion_id, *, by, note="", root=None):
    """A PERSON accepts a criterion no machine can check. Recorded as human evidence, never as a
    machine verdict.

    The distinction is the whole reason this function exists rather than reusing `record`: a
    criterion closed this way carries `human_only: True` in its evidence, so a reader can always
    tell which criteria a machine established and which a person asserted. Collapsing those is how
    "machine-complete" silently becomes "done"."""
    doc = _load(goal_id, root)
    if not (by or "").strip():
        raise GoalError("a sign-off needs a signer")
    crit = _criterion(doc, criterion_id)
    crit["status"] = MET
    # A person's acceptance answers every open question about this criterion, exactly as a
    # machine acceptance does in record(); live, a signed-off criterion left its two parked
    # questions open and the goal stayed BLOCKED with a met criterion.
    for q in doc["parked"]:
        if q["criterion_id"] == crit["id"] and not q.get("resolved"):
            q["resolved"] = _now()
            q["answer"] = "closed by human sign-off ({0})".format(by.strip())
    crit["evidence"].append({"kind": "human-sign-off", "by": by.strip(),
                             "note": (note or "").strip()[:500], "at": _now(),
                             "human_only": True})
    _event(doc, "signed_off", criterion=criterion_id, by=by.strip())
    _save(doc, root)
    return True


def open_questions(goal_id, root=None):
    return [q for q in _load(goal_id, root)["parked"] if not q.get("resolved")]


def _landed(doc):
    """Assignment names whose obligation is satisfied: an assignment accepted, OR any ancestor an
    accepted assignment lists in its `lineage`. A REPAIR (`X` -> `X-r2`) records lineage=[...,X],
    so an accepted repair satisfies a dependent that still `needs` the original name -- WITHOUT
    treating an unrelated success on the same criterion as interchangeable (lineage is explicit)."""
    out = set()
    for n, a in doc["assignments"].items():
        if a["status"] == DONE and a.get("disposition") == "accepted":
            out.add(n)
            for anc in (a.get("contract") or {}).get("lineage") or []:
                out.add(anc)
    return out


def _producers(doc, exclude=None):
    """basename(artifact) -> (produced-file path, producing assignment name) for every accepted
    assignment that wrote a real artifact. Later-added assignments win, so an accepted repair's
    fixed bytes override the earlier attempt's. This is how a dependent RECEIVES the repaired
    deliverable instead of the stale project file it declared as a source."""
    runs = Path(os.environ.get("FLEET_RUNS_DIR") or (ROOT / "runs"))
    out = {}
    for n, a in sorted(doc["assignments"].items(), key=lambda kv: kv[1].get("added", "")):
        if n == exclude:
            continue
        if a["status"] == DONE and a.get("disposition") == "accepted":
            art = (a.get("contract") or {}).get("artifact")
            if art and art != "output.md":
                # The accepted producer's workspace. Honour FLEET_RUNS_DIR so this agrees with the
                # receipt resolver (integrate._resolve_accepted); otherwise a dependent could be
                # staged the stale project file while the candidate uses the receipt-bound bytes.
                p = runs / ("verified-" + (a.get("run_id") or n)) / art
                if p.is_file():
                    out[os.path.basename(art)] = (str(p), n)
    return out



def _integration_needs_assignment(doc, name, rec):
    """Keep offering READY group members after a sibling met the criterion."""
    if not (doc.get("integration") or {}).get("declared"):
        return False
    try:
        import integrate
        meta = integrate.group_states(doc["goal_id"])
    except Exception:
        return False
    con = rec.get("contract") or {}
    lin = set([name] + list(con.get("lineage") or []))
    for g in meta.values():
        if g.get("state") == "passed":
            continue
        members = set(g.get("members") or [])
        if lin & members or name in members:
            return True
    return False


def next_work(goal_id, available_capacity, *, root=None):
    """The dependency-aware selector. Returns up to `available_capacity` job cards (interface I5)
    that are ready to run RIGHT NOW, or [] -- and [] means blocked, not done. Ask `disposition()`
    which it is.

    A card is ready when: the goal is approved, the assignment has not been dispatched or finished,
    its criterion is not already met, every name in its `needs` has actually landed as `accepted`,
    and the budget has room. `needs` pointing at a parked or blocked assignment keeps the dependent
    ready-but-not-offered, which is the case that must NOT stop independent work elsewhere.

    Pure query: it mutates nothing. The caller claims what it intends to run, so a crash between
    selecting and claiming loses nothing.
    """
    doc = _load(goal_id, root)
    if not doc.get("approved"):
        return []
    cap = max(0, int(available_capacity or 0))
    if not cap:
        return []
    room = _budget_room(doc)
    if room is not None:
        cap = min(cap, room)
        if cap <= 0:
            return []
    landed = _landed(doc)
    out = []
    for name, a in sorted(doc["assignments"].items(), key=lambda kv: kv[1]["added"]):
        if len(out) >= cap:
            break
        if a["status"] != READY:
            continue
        crit = _criterion(doc, a["contract"]["criterion_id"])
        origin = (a.get("contract") or {}).get("origin")
        if crit["status"] == MET and origin != "integration-repair":
            if not _integration_needs_assignment(doc, name, a):
                continue        # another assignment already established it; do not redo work
        if any(n not in landed for n in a["contract"].get("needs") or ()):
            continue
        out.append(card_for(a["contract"], doc))
    return out


@_serialised
def claim(goal_id, names, *, root=None):
    """Mark assignments dispatched and spend the budget. Separate from `next_work` so that
    selection stays a pure query and the budget is consumed exactly once per dispatch -- a loop
    that consumed budget while merely LOOKING for work would starve itself."""
    doc = _load(goal_id, root)
    claimed = []
    for n in names:
        a = doc["assignments"].get(n)
        if a is None:
            raise GoalError(f"cannot claim unknown assignment {n!r}")
        if a["status"] != READY:
            continue
        a["status"] = RUNNING
        a["attempts"] += 1
        a["claimed_at"] = time.time()
        doc["budget"]["consumed"] += 1
        claimed.append(n)
    _event(doc, "claimed", names=claimed)
    _save(doc, root)
    return claimed


@_serialised
def reclaim(goal_id, *, older_than_s=0, root=None):
    """Return assignments that were dispatched but never reported back to READY.

    A conductor killed between `claim` and `record` leaves an assignment RUNNING. Without this
    the goal is stuck forever behind "assignments in flight", which is an honest report of a
    stranded job but not a recovery.

    NOT automatic, and never called by `advance` unless the operator asks. A RUNNING assignment
    may still be executing in another process; re-offering it is exactly the duplicate execution
    Track A's leases exist to prevent, and the goal ledger is not the place that decides a job is
    dead. `older_than_s` is the operator's assertion about how long is long enough."""
    doc = _load(goal_id, root)
    back = []
    for n, a in doc["assignments"].items():
        if a["status"] != RUNNING:
            continue
        if time.time() - float(a.get("claimed_at") or 0) < older_than_s:
            continue
        a["status"] = READY
        back.append(n)
    _event(doc, "reclaimed", names=back, older_than_s=older_than_s)
    _save(doc, root)
    return back


@_serialised
def observe(goal_id, criterion_id, question, probe, result, *, by="architect", root=None):
    """Record one authorized observation against a criterion (LF-03).

    `result` is what run/observe.py returned, successful or not. An inaccessible fact is recorded
    with its reason, because "the harness could not see X" is itself the evidence the next
    decision must be made on -- silently dropping it would make the architect ask again."""
    doc = _load(goal_id, root)
    crit = _criterion(doc, criterion_id)
    row = {"criterion_id": crit["id"], "question": (question or "").strip(),
           "probe": dict(probe or {}), "ok": bool(result.get("ok")),
           "text": str(result.get("text") or "")[:4000],
           "ref": str(result.get("ref") or ""), "reason": str(result.get("reason") or ""),
           "malformed": bool(result.get("malformed")), "by": by, "ts": _now()}
    doc.setdefault("observations", []).append(row)
    _event(doc, "observed", criterion=crit["id"], ok=row["ok"], probe=row["probe"].get("tool"))
    qid = (probe or {}).get("question_id")
    if qid:
        # an investigation made FOR a question attaches its evidence there too (LF-07)
        for q in doc.get("questions") or []:
            if q["id"] == qid:
                q["observations"].append({"probe": row["probe"], "ok": row["ok"],
                                          "text": row["text"][:1500], "ref": row["ref"],
                                          "reason": row["reason"], "at": _now()})
                row["question_id"] = qid
    _save(doc, root)
    return row


def observations(goal_id, criterion_id=None, root=None):
    rows = _load(goal_id, root).get("observations") or []
    return [r for r in rows if criterion_id is None or r["criterion_id"] == criterion_id]


def alternatives(goal_id, criterion_id, root=None):
    """Every assignment that approaches one criterion, with its approach label, status,
    disposition and the workspace that holds its artifact (LF-04). Nothing here is erased when a
    sibling lands: an alternative that was never dispatched stays READY with its label, and one
    that was dispatched keeps its own `runs/verified-<name>` workspace, because every assignment
    name has its own."""
    doc = _load(goal_id, root)
    out = []
    for name, a in sorted(doc["assignments"].items(), key=lambda kv: kv[1]["added"]):
        c = a["contract"]
        if c.get("criterion_id") != criterion_id:
            continue
        out.append({"name": name, "approach": c.get("approach") or "(unlabelled)",
                    "lineage": list(c.get("lineage") or []),
                    "status": a["status"], "disposition": a.get("disposition"),
                    "attempts": a.get("attempts", 0),
                    "run_id": a.get("run_id") or name,
                    "evidence": [o.get("evidence_ref") for o in a.get("outcomes") or []
                                 if o.get("evidence_ref")],
                    "workspace": str(ROOT / "runs" / ("verified-" + (a.get("run_id") or name)))})
    return out


@_serialised
def revisit(goal_id, name, *, why, root=None):
    """Re-open a preserved alternative as a NEW assignment with the SAME acceptance (LF-04).

    Not the same name: re-running `name` would overwrite `runs/verified-<name>` and erase the
    evidence of the earlier attempt, which is exactly the loss this exists to prevent. The copy
    carries `lineage` so a reader can follow it back, and its oracle/done_when/criterion are the
    original's -- reconsideration under different acceptance would be a different goal."""
    doc = _load(goal_id, root)
    a = doc["assignments"].get(name)
    if a is None:
        raise GoalError(f"cannot revisit unknown assignment {name!r}")
    if a["status"] == RUNNING:
        raise GoalError(f"{name!r} is running; revisiting it would duplicate live work")
    crit = _criterion(doc, a["contract"]["criterion_id"])
    if crit["status"] == MET:
        raise GoalError(f"criterion {crit['id']!r} is already met; revisiting {name!r} changes "
                        f"nothing unless that acceptance is withdrawn first")
    if not (why or "").strip():
        raise GoalError("a revisit needs the new evidence that makes it relevant")
    k = 2
    while f"{name}-v{k}" in doc["assignments"]:
        k += 1
    new = f"{name}-v{k}"
    c = dict(a["contract"])
    c["name"] = new
    c["origin"] = "revisit"
    c["lineage"] = list(c.get("lineage") or []) + [name]
    # `needs` is KEPT. Clearing it (the first version) let a revisit dispatch past an unfinished
    # prerequisite: the acceptance test was the same, but the assignment's other obligations were
    # not. `next_work` applies the ordinary dependency rule to the copy.
    c["brief"] = (c.get("brief", "") + "\n\n--- REVISITED (architect) ---\n"
                  + "Why this approach is relevant again: " + why.strip())
    # Doc 05 (A): a REVISIT, like a REPAIR, must receive the parked approach's files, prior
    # feedback, established facts, unresolved questions and lineage -- not just a reason. Reuse
    # the one carry builder (lazy import: goals has no load-time dep on decide).
    try:
        import decide as _decide
        carry, inp, brief_add = _decide.carry_package(goal_id, name, a["contract"], new, root=root)
        c["carry"] = carry
        if inp:
            c["inputs"] = list(c.get("inputs") or []) + [inp]
        if brief_add:
            c["brief"] = c.get("brief", "") + brief_add
    except Exception as e:
        c["carry_error"] = "carry package unavailable: {0}".format(e)[:200]
    _add_assignment(doc, c)
    _event(doc, "revisited", of=name, as_=new)
    _save(doc, root)
    return new


# =================================================================================================
# worker-originated proposals (LF-06) and persistent questions (LF-07)
# =================================================================================================

PROPOSAL_RESOLUTIONS = ("accept", "defer", "reject")


@_serialised
def add_proposals(goal_id, assignment, proposals, *, run_id="", evidence_ref="", root=None):
    """Record what a worker proposed while executing `assignment`, with its supporting evidence and
    the run it came from. Pending until the decision path resolves it. A proposal of kind
    `question` also opens a persistent question. Nothing here creates work or grants anything."""
    doc = _load(goal_id, root)
    a = doc["assignments"].get(assignment) or {}
    cid = (a.get("contract") or {}).get("criterion_id")
    added = []
    seen = {}
    for q in doc.get("proposals") or []:
        seen[(q.get("kind"), _norm_text(q.get("text")))] = q
    for p in proposals or []:
        if p.get("kind") == "malformed":
            continue
        key = (p.get("kind"), _norm_text(p.get("text")))
        if key in seen:
            # Live (goal 2a77): a worker restated one proposal in each of three rounds and the
            # ledger held three pending copies for the architect to resolve separately. The same
            # proposal is one proposal; the repeat is recorded on it, not beside it.
            seen[key]["repeats"] = int(seen[key].get("repeats") or 0) + 1
            continue
        pid = "P-" + uuid.uuid4().hex[:8]
        row = {"id": pid, "kind": p.get("kind"), "text": p.get("text", ""),
               "evidence": p.get("evidence", ""), "from_assignment": assignment,
               "criterion_id": cid, "run_id": run_id, "round": p.get("round"),
               "evidence_ref": evidence_ref, "status": "pending", "created": _now(),
               "resolution": None}
        doc.setdefault("proposals", []).append(row)
        seen[key] = row
        added.append(row)
        if p.get("kind") == "question":
            # A question is not work to accept or reject: it is opened as a persistent question
            # and the proposal row records that conversion, so the architect's next decision is
            # about INVESTIGATING it, not about filing it.
            q = _open_question(doc, p.get("text", ""), criterion_id=cid, created_by="worker:" + assignment,
                               source={"proposal": pid, "run_id": run_id, "evidence_ref": evidence_ref},
                               evidence=p.get("evidence", ""))
            row["status"] = "converted"
            row["resolution"] = {"by": "harness", "reason": "opened as question " + q["id"],
                                 "at": _now(), "question_id": q["id"]}
    if added or any(int(q.get("repeats") or 0) for q in doc.get("proposals") or []):
        if added:
            _event(doc, "proposed_by_worker", assignment=assignment, ids=[r["id"] for r in added])
        _save(doc, root)
    return added


def _norm_text(s):
    return " ".join(str(s or "").lower().split())[:400]


# =================================================================================================
# challenges (LF-11, experimental): doubting an ACCEPTED result without touching its receipt
# =================================================================================================

CHALLENGE_KINDS = ("purpose_unmet", "counterexample")
CHALLENGE_OUTCOMES = ("established", "refuted", "uncertain")


@_serialised
def challenge(goal_id, criterion_id, *, by, kind, basis, evidence_ref="", assist="", root=None):
    """Record a challenge to a criterion that is already MET (or in REVIEW): the acceptance stands
    as recorded -- its receipt, evidence and status are untouched -- but a persistent question is
    opened that `complete()` will not look past. `kind` says what is doubted: the accepted artifact
    does not serve the criterion's PURPOSE, or a concrete COUNTEREXAMPLE is claimed. `basis` is
    what the challenger actually saw; `assist` is whatever the skeptic said about the artifact.
    One open challenge per criterion. Not raised automatically after every success."""
    if kind not in CHALLENGE_KINDS:
        raise GoalError(f"challenge kind must be one of {CHALLENGE_KINDS}")
    if not (basis or "").strip():
        raise GoalError("a challenge states what was seen; doubt without a basis is not recorded")
    doc = _load(goal_id, root)
    crit = _criterion(doc, criterion_id)
    if crit["status"] not in (MET, REVIEW):
        raise GoalError(f"{criterion_id} is {crit['status']}: only an established result is challenged; "
                        f"open work is repaired or parked")
    if any(c["criterion_id"] == criterion_id and c["status"] == "open" for c in doc.get("challenges") or []):
        raise GoalError(f"{criterion_id} already has an open challenge")
    text = ("Does the accepted result for {0} actually satisfy it? Challenge ({1}): {2}".format(
        criterion_id, kind, basis.strip()))[:600]
    q = _open_question(doc, text, criterion_id=criterion_id, created_by=str(by),
                       source={"challenge": True, "evidence_ref": evidence_ref},
                       evidence=basis.strip(),
                       explanations=[
                           {"text": "the acceptance holds: the artifact satisfies the criterion as recorded",
                            "predicts": "the check or counterexample named in the challenge does not reproduce"},
                           {"text": "the challenge holds: " + basis.strip()[:200],
                            "predicts": "an observation or check of the accepted artifact reproduces it"}],
                       distinguishing="run or observe the named counterexample / purpose check against "
                                      "the ACCEPTED artifact (its run directory), not against a description of it")
    row = {"id": "CH-" + uuid.uuid4().hex[:8], "criterion_id": criterion_id, "kind": kind,
           "basis": basis.strip(), "evidence_ref": evidence_ref or "", "by": str(by),
           "assist": (assist or "")[:1500], "question_id": q["id"], "status": "open",
           "outcome": None, "created": _now(), "resolution": None, "reconsider_when": None,
           "accepted_evidence": list(crit.get("evidence") or [])}
    doc.setdefault("challenges", []).append(row)
    _event(doc, "challenged", criterion=criterion_id, id=row["id"], challenge_kind=kind, by=str(by))
    _save(doc, root)
    return row


@_serialised
def resolve_challenge(goal_id, challenge_id, outcome, *, by, why, evidence_ref="", reconsider_when=None,
                      root=None):
    """established: the acceptance stands, the question is resolved on the named evidence.
    refuted: the counterexample or purpose gap reproduced; the criterion drops to REVIEW (its
    receipt and evidence remain, it is no longer counted complete until new work or a person
    establishes it). uncertain: nothing settled; the challenge stays open with the conditions
    under which it is worth looking again. Every outcome needs evidence or an attached observation."""
    if outcome not in CHALLENGE_OUTCOMES:
        raise GoalError(f"challenge outcome must be one of {CHALLENGE_OUTCOMES}")
    if not (why or "").strip():
        raise GoalError("a challenge is resolved with a reason")
    doc = _load(goal_id, root)
    ch = next((c for c in doc.get("challenges") or [] if c["id"] == challenge_id), None)
    if ch is None:
        raise GoalError(f"unknown challenge {challenge_id}")
    if ch["status"] != "open":
        raise GoalError(f"challenge {challenge_id} is already {ch['status']}")
    q = next((x for x in doc.get("questions") or [] if x["id"] == ch["question_id"]), None)
    has_obs = bool(q and q.get("observations"))
    if outcome in ("established", "refuted") and not has_obs and not (evidence_ref or "").strip():
        raise GoalError("established/refuted needs an attached observation on the challenge's question "
                        "or an evidence reference; a challenge is not closed on confidence")
    crit = _criterion(doc, ch["criterion_id"])
    if outcome == "uncertain":
        ok, bad = validate_conditions(reconsider_when or [])
        if not ok:
            raise GoalError(bad)
        ch["reconsider_when"] = list(reconsider_when or [])
        ch["reconsider_seen"] = None
        ch["notes"] = (ch.get("notes") or []) + [{"by": str(by), "why": why.strip(), "at": _now()}]
        _event(doc, "challenge_uncertain", id=challenge_id, by=str(by))
        _save(doc, root)
        return ch
    ch["status"] = "closed"
    ch["outcome"] = outcome
    ch["resolution"] = {"by": str(by), "why": why.strip(), "evidence_ref": evidence_ref or "", "at": _now()}
    if q is not None and q.get("status") == "open":
        q["status"] = "resolved"
        q["resolution"] = {"explanation": 0 if outcome == "established" else 1, "why": why.strip(),
                           "evidence_ref": evidence_ref or "", "by": str(by), "at": _now(),
                           "challenge": challenge_id}
        _event(doc, "question_resolved", id=q["id"], by=str(by))
    if outcome == "refuted" and crit["status"] == MET:
        crit["status"] = REVIEW
        crit["review_reason"] = "challenge {0} refuted the acceptance: {1}".format(challenge_id, why.strip()[:200])
        # O5: a skill retained from the now-refuted acceptance must not stay reusable. Withdraw
        # skills promoted from the accepted run(s) of this criterion (self or an accepted repair).
        try:
            import memory
            runs = set()
            for a in doc["assignments"].values():
                con = a.get("contract") or {}
                if a.get("disposition") == "accepted" and con.get("criterion_id") == ch["criterion_id"]:
                    runs.add(a.get("run_id") or con.get("name"))
            withdrawn = []
            for rid in runs:
                withdrawn += memory.withdraw_skills_from_run(
                    rid, "challenge {0} refuted the acceptance this skill was retained from".format(challenge_id),
                    by=str(by))
            if withdrawn:
                ch["skills_withdrawn"] = withdrawn
        except Exception:
            pass
    _event(doc, "challenge_" + outcome, criterion=ch["criterion_id"], id=challenge_id, by=str(by))
    _save(doc, root)
    return ch


def challenges(goal_id, status=None, root=None):
    return [c for c in _load(goal_id, root).get("challenges") or []
            if status is None or c.get("status") == status]


# =================================================================================================
# re-engagement (LF-07/LF-08): deferred items come back when the EVIDENCE they were waiting on
# arrives -- never because time passed
# =================================================================================================

CONDITION_TYPES = ("criterion_met", "file_present", "skill_available", "assumption_invalidated")
_TIME_WORDS = ("time", "elapsed", "after", "wait", "date", "days", "hours", "minutes", "later", "tomorrow")


def validate_conditions(conds):
    """(ok, why). A condition names something the harness can check against the environment. Any
    time-shaped condition is refused by name: the passing of time is not evidence."""
    if not isinstance(conds, list):
        return False, "reconsider_when must be a list of conditions"
    for c in conds:
        if not isinstance(c, dict):
            return False, "each condition is an object with a `type`"
        t = str(c.get("type") or "").strip().lower()
        if t not in CONDITION_TYPES and (t in _TIME_WORDS or any(w in t.replace("_", " ").split()
                                                                 for w in ("time", "elapsed", "date"))):
            return False, ("condition type {0!r} refused: time alone is not evidence; name what would "
                           "have to be TRUE (criterion_met, file_present, skill_available, "
                           "assumption_invalidated)".format(t))
        if t not in CONDITION_TYPES:
            return False, "unknown condition type {0!r}; known: {1}".format(t, list(CONDITION_TYPES))
        need = {"criterion_met": "criterion_id", "file_present": "path", "skill_available": "id",
                "assumption_invalidated": "memory_id"}[t]
        if not str(c.get(need) or "").strip() and not (t == "skill_available" and c.get("name")):
            return False, "condition {0} needs `{1}`".format(t, need)
    return True, ""


def _condition_state(cond, doc, observe_root=None):
    """satisfied | unsatisfied | unknown against the CURRENT environment."""
    t = cond.get("type")
    try:
        if t == "criterion_met":
            c = next((x for x in doc["criteria"] if x["id"] == cond.get("criterion_id")), None)
            if c is None:
                return "unknown"
            return "satisfied" if c["status"] == MET else "unsatisfied"
        if t == "file_present":
            if not observe_root:
                return "unknown"
            import observe
            base = Path(observe_root).resolve()
            p = (base / str(cond.get("path"))).resolve()
            if not observe._inside(base, p):
                return "unknown"
            return "satisfied" if p.exists() else "unsatisfied"
        if t == "skill_available":
            import memory
            sk = memory._latest(memory.SKILLS)
            hit = sk.get(cond.get("id")) or next((s for s in sk.values() if cond.get("name")
                                                  and s.get("name") == cond.get("name")), None)
            if hit is None:
                return "unsatisfied"
            return "satisfied" if hit.get("status") == "active" and hit.get("evidence") else "unsatisfied"
        if t == "assumption_invalidated":
            import memory
            for name in (memory.LESSONS, memory.SKILLS):
                e = memory._latest(name).get(cond.get("memory_id"))
                if e is not None:
                    return "satisfied" if e.get("status") in ("suspended", "withdrawn", "contradicted",
                                                              "demoted") else "unsatisfied"
            return "unknown"
    except Exception:
        return "unknown"
    return "unknown"


def _reconsider_rows(doc):
    for p in doc.get("proposals") or []:
        if p.get("status") == "deferred" and p.get("reconsider_when"):
            yield "proposal", p
    for q in doc.get("questions") or []:
        if q.get("status") == "open" and q.get("reconsider_when"):
            yield "question", q
    for c in doc.get("challenges") or []:
        if c.get("status") == "open" and c.get("reconsider_when"):
            yield "challenge", c


@_serialised
def set_reconsider(goal_id, kind, item_id, conditions, *, root=None):
    """Attach the conditions under which a deferred proposal, open question or open challenge is
    worth looking at again. Validated; time-shaped conditions are refused."""
    ok, why = validate_conditions(conditions)
    if not ok:
        raise GoalError(why)
    doc = _load(goal_id, root)
    key = {"proposal": "proposals", "question": "questions", "challenge": "challenges"}[kind]
    row = next((r for r in doc.get(key) or [] if r["id"] == item_id), None)
    if row is None:
        raise GoalError(f"unknown {kind} {item_id}")
    row["reconsider_when"] = list(conditions)
    row["reconsider_seen"] = None
    _event(doc, "reconsider_set", item_kind=kind, id=item_id, conditions=len(conditions))
    _save(doc, root)
    return row


@_serialised
def reengage(goal_id, *, observe_root=None, root=None):
    """Which deferred items have a stated condition that NOW holds, surfaced ONCE per change in
    the condition picture. Nothing time-based exists to trigger this. Items on criteria that are
    prerequisites, declared priority or the last thing open are listed first."""
    doc = _load(goal_id, root)
    out = []
    changed = False
    unmet = [c["id"] for c in doc["criteria"] if c["status"] != MET]
    for kind, row in _reconsider_rows(doc):
        states = [(c, _condition_state(c, doc, observe_root)) for c in row["reconsider_when"]]
        key = [s for _, s in states]
        if not any(s == "satisfied" for s in key):
            if row.get("reconsider_seen") != key:
                row["reconsider_seen"] = key          # remember the picture; nothing to surface
                changed = True
            continue
        if row.get("reconsider_seen") == key:
            continue                                   # already surfaced this exact picture
        row["reconsider_seen"] = key
        changed = True
        cid = row.get("criterion_id")
        crit = next((c for c in doc["criteria"] if c["id"] == cid), {}) if cid else {}
        important = (crit.get("priority") in ("high", "critical")) or len(unmet) == 1
        held = [c for c, s in states if s == "satisfied"]
        out.append({"kind": kind, "id": row["id"], "criterion": cid,
                    "text": str(row.get("text") or row.get("basis") or "")[:300],
                    "now_true": held,
                    "why": "condition now holds: " + "; ".join(
                        "{0} {1}".format(c.get("type"), c.get("criterion_id") or c.get("path") or c.get("id")
                                         or c.get("name") or c.get("memory_id")) for c in held),
                    "important": bool(important)})
    if changed:
        if out:
            _event(doc, "reengaged", items=[[i["kind"], i["id"]] for i in out])
        _save(doc, root)
    out.sort(key=lambda i: (not i["important"], i["kind"] != "proposal", i["id"]))
    return out


def pending_proposals(goal_id, root=None):
    return [p for p in _load(goal_id, root).get("proposals") or [] if p.get("status") == "pending"]


@_serialised
def resolve_proposal(goal_id, proposal_id, resolution, reason, *, by="architect", assignment=None,
                     root=None):
    """accept | defer | reject, with a reason. `assignment` names the work an acceptance became."""
    if resolution not in PROPOSAL_RESOLUTIONS:
        raise GoalError(f"resolution must be one of {PROPOSAL_RESOLUTIONS}")
    if not (reason or "").strip():
        raise GoalError("a proposal is resolved with a reason or not at all")
    doc = _load(goal_id, root)
    for p in doc.get("proposals") or []:
        if p["id"] == proposal_id:
            if p.get("status") not in ("pending", "deferred"):
                raise GoalError(f"proposal {proposal_id} is already {p.get('status')}")
            p["status"] = "accepted" if resolution == "accept" else ("deferred" if resolution == "defer" else "rejected")
            p["resolution"] = {"by": by, "reason": reason.strip(), "at": _now(), "assignment": assignment}
            _event(doc, "proposal_" + p["status"], id=proposal_id, by=by)
            _save(doc, root)
            return p
    raise GoalError(f"unknown proposal {proposal_id}")


def _open_question(doc, text, *, criterion_id=None, created_by="architect", source=None,
                   evidence="", explanations=None, distinguishing=None):
    qid = "Q-" + uuid.uuid4().hex[:8]
    q = {"id": qid, "text": (text or "").strip(), "criterion_id": criterion_id, "status": "open",
         "created_by": created_by, "created": _now(), "source": source or {},
         "initial_evidence": evidence or "",
         "explanations": [dict(e) for e in (explanations or [])],   # {text, predicts, evidence_for, evidence_against}
         "distinguishing": distinguishing or "",                   # what evidence would tell them apart
         "observations": [], "resolution": None}
    doc.setdefault("questions", []).append(q)
    _event(doc, "question_opened", id=qid, by=created_by)
    return q


@_serialised
def open_question(goal_id, text, *, criterion_id=None, created_by="architect", explanations=None,
                  distinguishing=None, evidence="", root=None):
    """A question worth keeping: what is unknown, the competing explanations, and what evidence
    would distinguish them. Survives runs and processes with the goal."""
    if not (text or "").strip():
        raise GoalError("a question needs text")
    doc = _load(goal_id, root)
    if criterion_id is not None:
        _criterion(doc, criterion_id)
    q = _open_question(doc, text, criterion_id=criterion_id, created_by=created_by,
                       explanations=explanations, distinguishing=distinguishing, evidence=evidence)
    _save(doc, root)
    return q


@_serialised
def update_question(goal_id, question_id, *, explanations=None, distinguishing=None,
                    observation=None, resolution=None, by="architect", root=None):
    """Attach an observation, add/replace explanations, or resolve. A resolution needs at least
    one attached observation or an explicit evidence reference: a question is not closed by
    confidence."""
    doc = _load(goal_id, root)
    q = next((x for x in doc.get("questions") or [] if x["id"] == question_id), None)
    if q is None:
        raise GoalError(f"unknown question {question_id}")
    if q.get("status") != "open" and resolution is None and observation is None:
        raise GoalError(f"question {question_id} is {q.get('status')}")
    if explanations is not None:
        q["explanations"] = [dict(e) for e in explanations]
    if distinguishing is not None:
        q["distinguishing"] = distinguishing
    if observation is not None:
        q["observations"].append(dict(observation, at=_now()))
    if resolution is not None:
        if not q["observations"] and not (resolution.get("evidence_ref") or "").strip():
            raise GoalError("a question is resolved on attached observations or a named evidence "
                            "reference, not on confidence")
        q["status"] = "resolved" if resolution.get("kind", "resolved") != "dropped" else "dropped"
        q["resolution"] = dict(resolution, by=by, at=_now())
        _event(doc, "question_" + q["status"], id=question_id, by=by)
    _save(doc, root)
    return q


def questions(goal_id, status=None, root=None):
    return [q for q in _load(goal_id, root).get("questions") or []
            if status is None or q.get("status") == status]


@_serialised
def record_selection(goal_id, dispatched, deferred, *, root=None):
    """Write the pre-dispatch selection into the ledger so a person can see WHY a ready card was
    or was not run: [(name, kind, reason)] for each side. Nothing else changes."""
    doc = _load(goal_id, root)
    _event(doc, "selected", dispatched=[[n, k] for n, k, _ in dispatched],
           deferred=[[n, k, r[:160]] for n, k, r in deferred])
    doc.setdefault("selection_log", []).append({"ts": _now(),
                                                "dispatched": [{"name": n, "kind": k, "why": r[:200]} for n, k, r in dispatched],
                                                "deferred": [{"name": n, "kind": k, "why": r[:200]} for n, k, r in deferred]})
    doc["selection_log"] = doc["selection_log"][-50:]
    _save(doc, root)


def resolve_source(src, doc=None):
    """Resolve a declared source path to an existing file. Order: as given (absolute or cwd),
    then under the goal's project_root, then under the fleet checkout. Returns a Path (possibly
    non-existent, so callers can report NO_MATERIAL honestly)."""
    p = Path(str(src))
    if p.is_file():
        return p
    roots = []
    if doc and doc.get("project_root"):
        roots.append(Path(doc["project_root"]))
    roots.append(ROOT)
    rel = str(src).replace("\\", "/").lstrip("/")
    for r in roots:
        cand = (r / rel)
        if cand.is_file():
            return cand
    return p


def _staged_interface(deps):
    """A compact public interface for each staged .py dependency, read from its source with ast:
    what a tools-disabled worker would learn by opening the file. This is the fix for a module
    being STAGED but its API unknowable, so the worker guesses names until another model reads it."""
    import memory
    out = []
    for d in deps or ():
        loc = d.get("location")
        if not loc or not str(loc).endswith(".py"):
            continue
        c = memory._module_contract(loc)
        eps = c.get("entry_points") or []
        if eps:
            sig = "; ".join("{0}({1}){2}".format(e["name"], ", ".join(e.get("params") or []),
                                                 " -- " + e["doc"] if e.get("doc") else "")
                            for e in eps[:10])
            out.append("  {0}: {1}".format(d.get("module"), sig))
        elif c.get("error"):
            out.append("  {0}: (interface unreadable: {1})".format(d.get("module"), c["error"]))
    return out


def card_for(contract, doc=None):
    """Project a contract onto the job card of interface I5. The card carries `goal_id`,
    `criterion_id` and `needs`, which Track A tolerates and ignores; the contract prose is folded
    into `brief` so the worker actually reads what it owns and what it may decide alone.

    THE CONSUMER SEAM for retained experience (LF-01/02) and authorized observations (LF-03).
    Anything the harness has learned or observed reaches the worker HERE, in the brief it already
    reads, or it does not reach the worker at all. `experience` and `observed` on the card record
    exactly what was attached, so a run's evidence can say which lesson was in play."""
    from plan import contract_brief          # local import: plan imports nothing from goals
    brief = contract_brief(contract, goal_text=(doc or {}).get("goal"))
    experience, observed = [], []
    crit_text = ""
    if doc:
        for c in doc.get("criteria") or []:
            if c["id"] == contract.get("criterion_id"):
                crit_text = c.get("text", "")
        for o in doc.get("observations") or []:
            if o["criterion_id"] == contract.get("criterion_id"):
                observed.append(o)
    skills, deps = [], []
    # Forward the contract's OWN declared inputs (e.g. a repair's read-only `_carry/<file>` prior
    # artifact, or decide.carry_package's carry) so explicit staged material actually reaches the
    # worker; source-derived deps/inputs and the __init__ chain are appended below.
    inputs, missing = list(contract.get("inputs") or []), []
    try:
        import memory
        import hashlib
        # FACTS are what the contract STATES (`facts`), not what it asks to keep compatible and
        # not the paths of its source material: a requirement is not evidence that it holds.
        facts = list(contract.get("facts") or [])
        # Retrieve experience on the SEMANTIC contract text, never the rendered brief: the
        # delivery-contract/ownership boilerplate would otherwise dilute tag overlap and hide
        # a relevant correction (regression caught by test_memory after the O2 brief change).
        match_text = " ".join([contract.get("brief") or "", contract.get("capability") or "", crit_text])
        # DEPENDENCIES the executor will STAGE into the workspace: the contract's own declared
        # .py sources, by exact bytes. A file existing somewhere else on this machine does not
        # make its module importable in the destination (audit gate 1); only staging does, so
        # the destination environment is built from what WILL be staged, plus the stdlib.
        producers = _producers(doc, exclude=contract.get("name")) if doc else {}
        proot = Path(doc["project_root"]) if doc and doc.get("project_root") else None
        def _reldest(sp):
            # Preserve the declared relative structure under project_root (so a worker that opens
            # `data/rates.csv` finds it there), falling back to the basename when it is outside.
            if proot:
                try:
                    return str(sp.relative_to(proot)).replace("\\", "/")
                except ValueError:
                    pass
            return sp.name
        for src in contract.get("sources") or []:
            sp = resolve_source(src, doc)
            if sp.suffix != ".py":
                # (#3) a declared NON-.py source (a data file) is a project input, staged into the
                # tool workspace. A declared source that does NOT resolve is recorded as MISSING,
                # not silently dropped -- the executor refuses before dispatch so the worker never
                # runs against a brief promising material the workspace lacks (S-B1 provisioning).
                if sp.is_file():
                    inputs.append({"name": sp.name, "dest": _reldest(sp), "location": str(sp),
                                   "sha256": hashlib.sha256(sp.read_bytes()).hexdigest()})
                else:
                    missing.append({"ref": str(src), "reason": "declared non-.py source not found"})
                continue
            # A dependent must receive the ACCEPTED producer's artifact (incl. an accepted repair),
            # not the stale project file it declared. If an accepted assignment in this goal wrote
            # a module of the same basename, stage THOSE bytes; otherwise the declared file.
            prod = producers.get(sp.name)
            use = Path(prod[0]) if prod else sp
            if use.is_file():
                # `dest` preserves the declared package-relative structure (game/rules.py), so a
                # tools worker that does `import game.rules` finds a real package -- not a module
                # flattened to the workspace root. The bytes come from the accepted producer (or the
                # declared file); the dest comes from the DECLARED source path, which is the logical
                # location regardless of where the accepted bytes physically sit.
                d = {"module": sp.stem, "dest": _reldest(sp), "location": str(use),
                     "sha256": hashlib.sha256(use.read_bytes()).hexdigest()}
                if prod:
                    d["from_assignment"] = prod[1]
                deps.append(d)
            else:
                missing.append({"ref": str(src), "reason": "declared .py dependency not found"})
        # Package markers: a dep staged at game/rules.py only imports as `game.rules` when the
        # package's __init__.py is staged too. Stage the __init__.py chain from project_root down to
        # each dep's directory, so the worker receives an importable package, not loose modules.
        if proot:
            pkg_dirs = set()
            for _d in deps:
                parts = (_d.get("dest") or "").split("/")[:-1]
                for i in range(1, len(parts) + 1):
                    pkg_dirs.add("/".join(parts[:i]))
            have = {i.get("dest") for i in inputs} | {_d.get("dest") for _d in deps}
            for pd in sorted(d for d in pkg_dirs if d):
                initp = proot / pd / "__init__.py"
                dest = pd + "/__init__.py"
                if initp.is_file() and dest not in have:
                    inputs.append({"name": "__init__.py", "dest": dest, "location": str(initp),
                                   "sha256": hashlib.sha256(initp.read_bytes()).hexdigest()})
        mods = {d["module"] for d in deps}
        for item in list(deps) + list(inputs):
            dest = str(item.get("dest") or "").replace("\\", "/")
            if "/" in dest:
                mods.add(dest.split("/", 1)[0])
        env = {"modules": mods | set(getattr(sys, "stdlib_module_names", ()))}
        # LF-14: the declared sources' CURRENT bytes are evidence about the conditions retained
        # advice depends on. An entry derived against a different money.py is suspended before
        # retrieval; unrelated entries are untouched.
        try:
            suspended = memory.reconcile_assumptions({"files": {d["module"]: d["sha256"] for d in deps}})
        except Exception:
            suspended = []
        plan_ = memory.plan_skills(match_text, tags=memory.tags_for(crit_text),
                                   facts=facts, env=env)
        # A skill whose only missing module is another offered skill is satisfiable: both are
        # staged together, in dependency order. Re-evaluate with the offered set in the
        # environment; the executor re-checks the same thing against the real workspace.
        env["modules"] |= {sk["module"] for sk, st, ok in plan_ if ok}
        plan_ = [(sk, memory.prerequisites_state(sk, facts, env),
                  not memory.prerequisites_state(sk, facts, env)["contradicted"]
                  and not memory.prerequisites_state(sk, facts, env)["unknown"]) for sk, _, _ in plan_]
        experience = memory.sources_for(match_text, tags=memory.tags_for(crit_text),
                                        facts=facts, env=env, plan=plan_)
        skills = [{"id": sk["id"], "module": sk["module"], "location": sk["location"],
                   "sha256": sk["sha256"], "version": sk["version"], "verify": sk["verify"],
                   "dependencies": list((sk.get("contract") or {}).get("dependencies") or [])}
                  for sk, st, ok in plan_ if ok]
        if suspended:
            experience.append("SUSPENDED (condition changed, not offered): " + "; ".join(
                "{0} -- {1}".format(i, r[:120]) for i, r in suspended))
    except Exception as e:           # memory must never make a card undeliverable
        experience = ["(retained experience unavailable: {0})".format(e)]
    if experience:
        # The skill section sits between markers so the executor can RECONCILE it with what
        # staging actually achieved (run/verified.py reconcile_brief). Lessons stay outside.
        lessons_ = [ln for ln in experience if not ln.startswith(("SKILL ", "COMPOSITION"))]
        skills_ = [ln for ln in experience if ln.startswith(("SKILL ", "COMPOSITION"))]
        brief += ("\n\nRETAINED EXPERIENCE from earlier work. Each item names its source and its "
                  "limits; it is evidence about a past case, not a rule.")
        if lessons_:
            brief += "\n- " + "\n- ".join(lessons_)
        if skills_:
            brief += ("\n<<SKILLS>>\n- " + "\n- ".join(skills_) + "\n<</SKILLS>>")
        if skills and not contract.get("tools"):
            # Live (net-total-c2/c3): told that modules were "staged beside your module", a
            # tools-disabled worker replied "I'll investigate the workspace" and emitted no code
            # for three rounds. It cannot investigate. Say so, and say what to do instead.
            brief += ("\n\nYou have NO file tools and cannot open or investigate these modules; "
                      "everything you may rely on about them is stated above (entry points, "
                      "verified example calls with their argument and return types). Do not "
                      "announce an investigation. Write your complete module now, as a single "
                      "```python fenced block, importing the staged modules by name.")
    if observed:
        lines = []
        for o in observed[-6:]:
            if o["ok"]:
                lines.append("{0} ({1} {2}) -> from {3}:\n{4}".format(
                    o["question"], o["probe"].get("tool"), o["probe"].get("target"),
                    o["ref"], o["text"][:1500]))
            else:
                lines.append("{0} ({1} {2}) -> NOT ACCESSIBLE: {3}".format(
                    o["question"], o["probe"].get("tool"), o["probe"].get("target"),
                    o["reason"]))
        brief += ("\n\nOBSERVED FACTS the harness gathered from the authorized project root, so "
                  "you do not have to guess them:\n" + "\n".join(lines))
    # STAGED DEPENDENCY INTERFACES (worker enablement): a tools-disabled worker cannot open the
    # modules staged in its workspace, so surface their public interface -- read from the staged
    # source with ast -- in the brief. Independent of retained experience; it is about the
    # deliverable's own declared dependencies, and is the fix for "module staged but API unknowable".
    try:
        iface = _staged_interface(deps) if not contract.get("tools") else []
    except Exception:
        iface = []
    if iface:
        brief += ("\n\nSTAGED MODULES you may import (you have NO file tools; this IS their public "
                  "interface, read from the staged source -- call these exactly, do not reimplement "
                  "them and do not invent other names):\n" + "\n".join(iface))

    rid = run_id(None, contract["name"], doc=doc) if doc else contract["name"]
    return {"name": rid,                       # execution identity: workspace, queue file, receipt
            "assignment": contract["name"],    # ledger identity: what the goal calls this work
            "run_id": rid,
            "worker": contract.get("worker"),
            "approach": contract.get("approach"),
            "experience": [ln.split(" ")[1] for ln in experience
                           if ln.startswith(("CORRECTION ", "OBSERVATION ", "SKILL "))
                           and "NOT offered" not in ln],
            # Exact bytes the executor stages into the workspace before round 1 (LF-02). The
            # worker is told a module is importable only because THIS list makes it so.
            "skills": skills,
            "deps": deps,
            "inputs": inputs,
            "missing_sources": missing,
            "capability": contract.get("capability", ""),
            "criterion_text": crit_text,
            "observed": len(observed),
            "brief": brief,
            "done_when": list(contract.get("done_when") or []),
            "oracle": contract.get("oracle") or "",
            "tools": bool(contract.get("tools")),
            "needs": list(contract.get("needs") or []),
            "goal_id": (doc or {}).get("goal_id", contract.get("goal_id", "")),
            "criterion_id": contract["criterion_id"],
            "artifact": contract.get("artifact", "output.md"),
            # The architect's DECLARATION that this card's frozen oracle is the mechanical check
            # for its done_when clauses. It must reach the card or the criteria arrive as prose,
            # every outcome lands awaiting-review, and a dependent that needs them ACCEPTED never
            # becomes ready. That is exactly how the first unattended run stopped BLOCKED with
            # correct working code already on disk.
            "oracle_covers_done_when": bool(contract.get("oracle_covers_done_when")),
            "origin": contract.get("origin") or "",
            "group": contract.get("group") or "",
            "dests": list(contract.get("dests") or []),
            "dest": contract.get("dest") or "",
            "consumer": contract.get("consumer") or "",
            "provides": contract.get("provides") or "",
            "require_consumer": bool(contract.get("require_consumer")),
            "integration_stage": list(contract.get("integration_stage") or []),
            "repair_reason": contract.get("repair_reason") or "",
            "limits": dict((doc or {}).get("limits") or {})}


def _budget_room(doc):
    lim = doc["budget"].get("assignments")
    if lim is None:
        return None
    return max(0, int(lim) - int(doc["budget"].get("consumed", 0)))


def criteria_met(goal_id, *, skip=(), root=None):
    """True when every criterion except those in skip is MET with evidence."""
    doc = _load(goal_id, root)
    rows = [c for c in doc["criteria"] if c["id"] not in skip]
    return bool(rows) and all(c["status"] == MET and c["evidence"] for c in rows)


@_serialised
def record_run_limits(goal_id, *, assignments=None, repairs=None, seconds=None, decisions=None, root=None):
    """Record the trial's ACTUAL limits on the goal so they are enforced, not implicit: an assignment
    cap (goal budget, enforced by next_work/_budget_room), a repair-admission ceiling and a time cap
    (run_budget, enforced by consume_milestone and run_goal). Recorded amounts are not reset by a
    later launch; spend accumulates against them (see record_spend)."""
    doc = _load(goal_id, root)
    if assignments is not None:
        doc.setdefault("budget", {})["assignments"] = int(assignments)
    rb = doc.setdefault("run_budget", {})
    if seconds is not None:
        rb["seconds"] = int(seconds)
    if decisions is not None:
        rb["decisions"] = int(decisions)
    if repairs is not None:
        rb["repairs"] = int(repairs)
    doc.setdefault("run_spent", {"decisions": 0, "seconds": 0, "repairs": 0})
    _event(doc, "run_limits", assignments=assignments, repairs=repairs, seconds=seconds, decisions=decisions)
    _save(doc, root)
    return {"budget": doc.get("budget"), "run_budget": rb}


def set_run_mode(goal_id, mode, *, decisions, seconds, root=None):
    """Record the launch mode and DURABLE budgets on the goal. Autonomous is not a hidden zero.

    The budget is set once and then held: a repeated `autonomous` launch of the same approved goal
    must NOT reset the recorded time/decision budget or the spend already accumulated against it.
    Time, decisions and repair admissions are cumulative across resume/restart (see record_spend);
    they persist here so a second invocation resumes under the remaining budget, not a fresh one."""
    if mode not in ("packet-only", "autonomous"):
        raise GoalError("mode must be packet-only or autonomous")
    doc = _load(goal_id, root)
    doc["run_mode"] = mode
    if not doc.get("run_budget"):
        doc["run_budget"] = {"decisions": int(decisions), "seconds": int(seconds)}
        doc.setdefault("run_spent", {"decisions": 0, "seconds": 0, "repairs": 0})
        _event(doc, "run_mode", mode=mode, decisions=int(decisions), seconds=int(seconds), set=True)
    else:
        # Repeated launch: keep the existing budget and accumulated spend untouched.
        doc.setdefault("run_spent", {"decisions": 0, "seconds": 0, "repairs": 0})
        _event(doc, "run_mode", mode=mode, kept_budget=doc["run_budget"])
    _save(doc, root)
    return doc["run_budget"]


@_serialised
def record_spend(goal_id, *, seconds=0, decisions=0, repairs=0, root=None):
    """Accumulate what an autonomous run consumed, so budgets survive resume/restart. Never resets."""
    doc = _load(goal_id, root)
    sp = doc.setdefault("run_spent", {"decisions": 0, "seconds": 0, "repairs": 0})
    sp["seconds"] = int(sp.get("seconds", 0)) + int(seconds)
    sp["decisions"] = int(sp.get("decisions", 0)) + int(decisions)
    sp["repairs"] = int(sp.get("repairs", 0)) + int(repairs)
    _save(doc, root)
    return dict(sp)


def run_spent(goal_id, root=None):
    sp = (_load(goal_id, root).get("run_spent")) or {}
    return {"decisions": int(sp.get("decisions", 0)), "seconds": int(sp.get("seconds", 0)),
            "repairs": int(sp.get("repairs", 0))}


@_serialised
def mark_run_active(goal_id, ts, *, root=None):
    """Persist the wall time at which the current autonomous run last COMMITTED its spend. Reconciled
    on the next entry so a crash during a long blocking call still charges that interval (see
    orchestrate.run_goal). Advanced by the loop heartbeat as spend is committed forward."""
    doc = _load(goal_id, root)
    doc["run_active_since"] = float(ts)
    _save(doc, root)
    return doc["run_active_since"]


def run_active_since(goal_id, root=None):
    v = _load(goal_id, root).get("run_active_since")
    return float(v) if v is not None else None


@_serialised
def clear_run_active(goal_id, *, root=None):
    doc = _load(goal_id, root)
    doc.pop("run_active_since", None)
    _save(doc, root)


def budget_remaining(goal_id, root=None):
    """Remaining time/decisions on the durable budget (None where no budget is set)."""
    doc = _load(goal_id, root)
    b = doc.get("run_budget") or {}
    sp = doc.get("run_spent") or {}
    def _rem(key):
        return (int(b[key]) - int(sp.get(key, 0))) if b.get(key) is not None else None
    return {"seconds": _rem("seconds"), "decisions": _rem("decisions"),
            "repairs": (int(b["repairs"]) - int(sp.get("repairs", 0))) if b.get("repairs") is not None else None}


@_serialised
def record_journey(goal_id, *, passed, transcript, root=None):
    """The launch command's result. Packet criteria do not set this."""
    doc = _load(goal_id, root)
    doc["journey"] = {"passed": bool(passed), "transcript": (transcript or "")[:4000], "ts": _now()}
    _event(doc, "journey", passed=bool(passed))
    _save(doc, root)
    return doc["journey"]


def complete(goal_id, root=None):
    """True only when every criterion is MET with at least one evidence reference and no question
    is still open.

    It does not look at the work list, ever. That is the defect this whole module exists to fix:
    an empty queue was being read as a finished goal. A goal is finished when its criteria are
    established, and nothing else makes this function true. Integration-enabled goals also require
    every integration group to have passed; assignment acceptance is intermediate."""
    doc = _load(goal_id, root)
    if not doc["criteria"]:
        return False
    if any(not q.get("resolved") for q in doc["parked"]):
        return False
    if any(c.get("status") == "open" for c in doc.get("challenges") or []):
        return False
    if not all(c["status"] == MET and c["evidence"] for c in doc["criteria"]):
        return False
    imap = (doc.get("interface_map") or {})
    if imap.get("spec"):
        # A journey must have PASSED -- packet acceptance alone is never project completion.
        if not (doc.get("journey") or {}).get("passed"):
            return False
        # A stale map approval (scope/assignments changed after approval) cannot drive DONE.
        stale, _why = map_stale(goal_id, doc=doc, root=root)
        if stale:
            return False
        # The milestone command's dependence on the delivered work is enforced by the differential
        # probe in conductor.consume_milestone BEFORE promotion, which only records a passed journey
        # when the command discriminates. journey.passed here therefore already implies that gate.
    integ = doc.get("integration") or {}
    if integ.get("declared"):
        import integrate
        ok, _reason = integrate.completion_state(goal_id, doc=doc, root=root)
        if not ok:
            return False
    return True


@_serialised
def record_plan_rejections(goal_id, findings, uncovered, *, root=None):
    """Preserve the exact gate findings for a rejected plan (O3), and park each criterion that no
    admissible outcome covered -- with the specific finding, so a person sees the real constraint
    rather than an empty queue. `findings` are dicts {round, name, criterion_id, problems:[(code,
    why)], contract?}. `uncovered` is the list of criteria (dicts or ids) still without work."""
    doc = _load(goal_id, root)
    if findings:
        doc.setdefault("plan_rejections", []).extend([dict(f) for f in findings])
    for c in uncovered or ():
        cid = c["id"] if isinstance(c, dict) else c
        why = ""
        for f in reversed(findings or ()):
            if f.get("criterion_id") == cid and f.get("problems"):
                why = "; ".join(w for _code, w in f["problems"])
                break
        why = why or "planning produced no admissible outcome for this criterion"
        if not any(q["criterion_id"] == cid and not q.get("resolved") for q in doc["parked"]):
            doc["parked"].append({"criterion_id": cid, "assignment": None, "outcome": "plan-rejected",
                                  "question": "planning could not produce admissible work: " + why[:400],
                                  "asked": _now(), "resolved": None})
    _event(doc, "plan_rejections", n=len(findings or ()),
           uncovered=[(c["id"] if isinstance(c, dict) else c) for c in (uncovered or ())])
    _save(doc, root)
    return doc.get("plan_rejections", [])


def plan_rejections(goal_id, root=None):
    return _load(goal_id, root).get("plan_rejections", [])


def disposition(goal_id, root=None):
    """Why the harness is not asking for more work. Returns

        {"state": "done" | "working" | "blocked", "reasons": [...], "unmet": [...],
         "awaiting_review": [...], "parked": [...], "budget": {...}}

    DONE and BLOCKED are the two ends `complete()` alone cannot distinguish for a caller, and
    telling them apart is the proof obligation for this track: an empty queue with unmet criteria
    is BLOCKED and says so, with the specific thing that is in the way."""
    doc = _load(goal_id, root)
    unmet = [c["id"] for c in doc["criteria"] if c["status"] != MET]
    review = [c["id"] for c in doc["criteria"] if c["status"] == REVIEW]
    parked = [q for q in doc["parked"] if not q.get("resolved")]
    out = {"goal_id": goal_id, "unmet": unmet, "awaiting_review": review,
           "parked": parked, "budget": dict(doc["budget"]), "reasons": []}
    if complete(goal_id, root):
        out["state"] = "done"
        return out
    integ = doc.get("integration") or {}
    if integ.get("declared"):
        import integrate
        ok, reason = integrate.completion_state(goal_id, doc=doc, root=root)
        if not ok:
            out["reasons"].append(reason)
        else:
            try:
                for gid, g in integrate.group_states(goal_id).items():
                    st = g.get("state")
                    if st == "waiting":
                        out["reasons"].append(
                            "integration group %s is waiting for components: %s" % (gid, g.get("reason") or ""))
                    elif st == "failed":
                        out["reasons"].append(
                            "integration group %s failed; last checkpoint preserved" % gid)
                    elif st == "conflict":
                        out["reasons"].append(
                            "integration group %s conflict: %s" % (gid, g.get("reason") or ""))
            except integrate.IntegrateError as e:
                out["reasons"].append(str(e))
    if not doc.get("approved"):
        out["state"] = "blocked"
        out["reasons"].append("plan not approved by a human yet")
        return out
    if next_work(goal_id, 1, root=root):
        out["state"] = "working"
        return out
    if any(a["status"] == RUNNING for a in doc["assignments"].values()):
        out["state"] = "working"
        out["reasons"].append("assignments in flight")
        return out

    out["state"] = "blocked"
    room = _budget_room(doc)
    if room == 0:
        out["reasons"].append(
            f"budget exhausted: {doc['budget']['consumed']} of {doc['budget']['assignments']} "
            f"assignments spent, {len(unmet)} criteria still unmet")
    for q in parked:
        out["reasons"].append(f"parked question on {q['criterion_id']}: {q['question']}")
    for cid in review:
        out["reasons"].append(f"{cid} is awaiting human review; machine work is complete")
    for c in doc["criteria"]:
        if c["status"] == BLOCKED:
            out["reasons"].append(f"{c['id']} blocked: {c.get('blocked_reason', 'infrastructure')}")
    # Dependency deadlock: something is still READY but can never be offered because what it
    # needs did not land. Naming it beats reporting "queue empty" and leaving a human to guess.
    landed = _landed(doc)
    for name, a in doc["assignments"].items():
        if a["status"] == READY:
            missing = [n for n in a["contract"].get("needs") or () if n not in landed]
            if missing:
                out["reasons"].append(f"{name} waits on {missing}, which did not land")
    if not out["reasons"]:
        out["reasons"].append(
            f"no assignment covers {unmet}: the plan does not reach the goal, which is a planning "
            f"gap, not a finished goal")
    return out


def summary(goal_id, root=None):
    """One screen for a human who walked away for an hour."""
    doc = _load(goal_id, root)
    d = disposition(goal_id, root)
    # Reasons are truncated HERE and nowhere else: `disposition()` keeps them whole for a
    # machine, while a human reading a terminal needs the first clause of each, not a paragraph.
    reasons = "; ".join((r[:150] + "...") if len(r) > 150 else r for r in d["reasons"])
    lines = [f"goal {goal_id}: {doc['goal']}",
             f"  state: {d['state'].upper()}" + (f" ({reasons})" if reasons else ""),
             f"  budget: {doc['budget']['consumed']}"
             + (f"/{doc['budget']['assignments']}" if doc['budget']['assignments'] else "")
             + f" assignments, {doc['budget']['seconds']:.0f}s"]
    for c in doc["criteria"]:
        ev = f" [{c['evidence'][-1]}]" if c["evidence"] else ""
        lines.append(f"  [{c['status']:<6}] {c['id']}: {c['text'][:70]}{ev}")
    for q in d["parked"]:
        lines.append(f"  ?? {q['criterion_id']}: {q['question'][:90]}")
    return "\n".join(lines)


@_serialised
def bind_scope(goal_id, scoped_text, *, path, approval_sha, root=None):
    """Bind the approved scoped.md. The hash must match the approval record, and the path is stored.

    Does not replace the goal text. A mismatched approval is refused."""
    import hashlib
    doc = _load(goal_id, root)
    raw = scoped_text if isinstance(scoped_text, str) else scoped_text.decode("utf-8")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if digest != (approval_sha or ""):
        raise GoalError("scoped.md bytes do not match the recorded approval")
    if not str(path or "").strip():
        raise GoalError("scope binding needs the approved path")
    doc["scope"] = {"sha256": digest, "text": raw, "path": str(path)}
    _event(doc, "scope_bound", sha256=digest, path=str(path))
    _save(doc, root)
    return doc["scope"]


@_serialised
def approve_interface_map(goal_id, spec, approver, *, fixture=False, root=None):
    """Record one approved interface map. A fixture approval is marked so it cannot be
    reported as a real user's approval."""
    import hashlib
    if not (approver or "").strip():
        raise GoalError("a map approval needs an approver")
    if not isinstance(spec, dict) or not spec.get("components") or not spec.get("milestone"):
        raise GoalError("an interface map needs components and one milestone")
    seen = set()
    ids = set()
    prefixes = set()          # valid `calls` prefixes: component ids AND the module stem of each path
    for component in spec["components"]:
        cid = (component.get("id") or "").strip()
        path_ = (component.get("path") or "").strip()
        provides = (component.get("provides") or "").strip()
        if not cid or not path_ or not provides:
            raise GoalError("each component needs an id, an owned path, and a public call")
        if path_ in seen:
            raise GoalError("two components own {0}".format(path_))
        seen.add(path_)
        ids.add(cid)
        prefixes.add(cid)
        stem = path_.replace("\\", "/").rsplit("/", 1)[-1]
        if stem.endswith(".py"):
            stem = stem[:-3]
        if stem:
            prefixes.add(stem)
    for component in spec["components"]:
        raw = component.get("calls") or []
        # `calls` may be a single "mod.call" string or a LIST of them (a component that calls several
        # dependencies). Validate every edge points at a providing component -- by its id OR by the
        # module stem of the file it owns, since a code edge is naturally written `world.get_drifts`
        # (the module) rather than `world-model.get_drifts` (the outcome id).
        calls_list = [raw] if isinstance(raw, str) else list(raw)
        for call in calls_list:
            call = (call or "").strip()
            if call and call.split(".", 1)[0] not in prefixes:
                raise GoalError("call {0} has no providing component".format(call))
    command = (spec.get("milestone") or {}).get("command")
    if not isinstance(command, list) or not command:
        raise GoalError("the milestone needs a launch command")
    doc = _load(goal_id, root)
    blob = json.dumps(spec, sort_keys=True)
    # Bind the approval to the exact approved scope AND the FULL assignment contracts (each owned
    # destination and its declared interface). A later scope change, a NEW owned destination, or a
    # CHANGED interface on an existing destination all make this approval STALE (see map_stale), so a
    # person re-approves rather than a moved plan silently reusing an old approval. A same-interface
    # repair of an existing destination does not change the signature and stays fresh.
    # FAIL-CLOSED baseline freeze: snapshot project_root into an immutable, verifiably-complete
    # baseline BEFORE recording the approval. If the freeze cannot complete (a partial/failed copy),
    # the error propagates and NO approval is recorded -- there is never an approved map whose
    # baseline silently falls back to the mutable project_root.
    baseline_frozen = False
    import proofloop
    frozen = proofloop.freeze_baseline(goal_id)
    if frozen is not None:
        if not proofloop.baseline_is_complete(goal_id):
            raise GoalError("project baseline freeze did not complete; map approval refused (fail closed)")
        baseline_frozen = True
    doc["interface_map"] = {
        "spec": spec,
        "sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
        "approved_by": approver.strip(),
        "fixture": bool(fixture),
        "approved": _now(),
        "scope_sha256": (doc.get("scope") or {}).get("sha256"),
        "assignment_dests": sorted(_assignment_pairs(doc).keys()),
        "assignments_sig": _assignment_signature(doc),
        "baseline_frozen": baseline_frozen,
        "baseline_sha256": (proofloop._baseline_marker(goal_id).is_file()
                            and json.loads(proofloop._baseline_marker(goal_id).read_text(encoding="utf-8")).get("sha256")
                            or None),
    }
    _event(doc, "map_approved", sha256=doc["interface_map"]["sha256"], fixture=bool(fixture),
           scope_sha256=doc["interface_map"]["scope_sha256"], baseline_frozen=baseline_frozen)
    _save(doc, root)
    return doc["interface_map"]


@_serialised
def propose_interface_map(goal_id, spec, *, root=None):
    """Record a DERIVED interface map for a person to approve -- not approved, not driving anything.
    This is how one-entry autonomous planning connects an approved plan to a map without the operator
    hand-writing JSON: the harness proposes, the human still approves (approve_interface_map)."""
    import hashlib
    if not isinstance(spec, dict) or not spec.get("components") or not spec.get("milestone"):
        raise GoalError("a proposed interface map needs components and one milestone")
    doc = _load(goal_id, root)
    doc["proposed_map"] = {"spec": spec,
                           "sha256": hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
                           "ts": _now()}
    _event(doc, "map_proposed", sha256=doc["proposed_map"]["sha256"])
    _save(doc, root)
    return doc["proposed_map"]


def _assignment_pairs(doc):
    """{owned destination -> declared interface} across the plan: approved assignments (latest bytes
    per destination win, so an accepted repair keeps the same destination and interface) plus any
    still-proposed contracts. This is what an approved map is bound to."""
    m = {}
    for _n, a in sorted((doc.get("assignments") or {}).items(), key=lambda kv: kv[1].get("added", "")):
        c = a.get("contract") or {}
        dest = (c.get("dest") or "").strip()
        if dest:
            m[dest] = (c.get("provides") or "").strip()
    for c in (doc.get("proposed") or []):
        dest = (c.get("dest") or "").strip()
        if dest:
            m.setdefault(dest, (c.get("provides") or "").strip())
    return m


def _assignment_interface(doc):
    """{owned destination -> (provides, consumer, sorted needs)} over the plan's NON-repair
    assignments (latest per destination) plus still-proposed contracts. A repair (contract.replaces)
    is EXCLUDED: it is a same-interface re-do of an existing destination, not a plan change, so its
    differing consumer/needs must not make the approved map stale. Any change a NON-repair assignment
    makes to a destination's provides, consumer, or needs -- or a new/removed destination -- does."""
    m = {}
    for _n, a in sorted((doc.get("assignments") or {}).items(), key=lambda kv: kv[1].get("added", "")):
        c = a.get("contract") or {}
        if c.get("replaces"):
            continue
        dest = (c.get("dest") or "").strip()
        if dest:
            m[dest] = [(c.get("provides") or "").strip(), (c.get("consumer") or "").strip(),
                       sorted(str(x) for x in (c.get("needs") or []))]
    for c in (doc.get("proposed") or []):
        if c.get("replaces"):
            continue
        dest = (c.get("dest") or "").strip()
        if dest and dest not in m:
            m[dest] = [(c.get("provides") or "").strip(), (c.get("consumer") or "").strip(),
                       sorted(str(x) for x in (c.get("needs") or []))]
    return m


def _assignment_signature(doc):
    import hashlib
    return hashlib.sha256(
        json.dumps(sorted(_assignment_interface(doc).items())).encode()).hexdigest()


def map_stale(goal_id, doc=None, root=None):
    """(stale, reason). An approved interface map is stale when the bound scope changed since
    approval, when the plan's owned destinations or their interfaces changed (a new destination, a
    removed one, or an interface change on an existing one), or when the map owns a destination no
    assignment produces. A same-interface repair of an existing destination is NOT stale. A stale
    approval must not drive assembly/journey/DONE until a person re-approves."""
    doc = doc or _load(goal_id, root)
    m = doc.get("interface_map") or {}
    if not m.get("spec"):
        return False, ""
    cur_scope = (doc.get("scope") or {}).get("sha256")
    if m.get("scope_sha256") and cur_scope and m["scope_sha256"] != cur_scope:
        return True, "the approved scope changed after the map was approved; re-approve the map"
    if m.get("assignments_sig") and m["assignments_sig"] != _assignment_signature(doc):
        return True, ("the plan's owned destinations or interfaces changed after the map was "
                      "approved (added/removed destination, or changed interface); re-approve the map")
    map_paths = {(c.get("path") or "").strip() for c in (m["spec"].get("components") or [])
                 if (c.get("path") or "").strip()}
    cur_dests = set(_assignment_pairs(doc).keys())
    unproduced = sorted(map_paths - cur_dests) if cur_dests else []
    if cur_dests and unproduced:
        return True, "the map owns {0} but no assignment produces it; re-plan or re-approve".format(
            ", ".join(unproduced))
    return False, ""




@_serialised
def record_check_defect(goal_id, *, oracle, requirement, failure, root=None):
    """A frozen check contradicted the approved requirement. The check is not edited."""
    import hashlib
    doc = _load(goal_id, root)
    row = {
        "oracle_sha256": hashlib.sha256((oracle or "").encode("utf-8")).hexdigest(),
        "requirement": requirement,
        "failure": failure,
        "ts": _now(),
    }
    doc.setdefault("check_defects", []).append(row)
    _event(doc, "check_defect", oracle_sha256=row["oracle_sha256"])
    _save(doc, root)
    return row


@_serialised
def record_trace(goal_id, row, *, root=None):
    """Append one causal step. The row is stored as given; callers supply the hashes."""
    doc = _load(goal_id, root)
    row = dict(row)
    row.setdefault("ts", _now())
    doc.setdefault("trace", []).append(row)
    _save(doc, root)
    return row


def list_goals(root=None):
    d = goals_dir(root)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json") if not p.name.startswith("."))
