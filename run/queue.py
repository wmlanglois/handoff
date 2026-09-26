"""A small durable job queue for the fleet -- the good part of the old dispatcher (atomic enqueue,
directory-state tracking, an index of outcomes) without the 2000-line packet machinery.

Enqueue carded jobs, then `drain` runs them across the fleet: capacity-aware (respects each worker's
max_inflight and the watchdog, so it never bursts a worker), durable (every job is a file whose
directory IS its state), and resumable (a crashed drain leaves jobs in running/, re-queued on restart).
This is what lets you batch a pile of work, walk away, and let it run.

    python run/queue.py add run/cards/code/*.json         # enqueue
    python run/queue.py add-dir run/cards/batch           # enqueue a whole dir
    python run/queue.py status                            # counts + recent outcomes
    python run/queue.py drain                             # run until ready is empty
    python run/queue.py drain --workers cluster,cluster,pairA,pairB --max-seconds 1800

States: runs/queue/{ready,running,done,parked,failed}/  + runs/queue/index.jsonl
"""
import argparse, json, os, socket, subprocess, sys, threading, time, uuid
from dataclasses import dataclass
from pathlib import Path
from collections import Counter
import functools

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "check"))
from safeio import force_utf8  # noqa
force_utf8()
from fleet import WORKERS  # noqa
from fleet import runs_root as fleet_runs_root  # noqa: E402  (one runs root for writer+readers)
from loop_config import LOOP as _LOOP  # noqa  -- single source for timing invariants
import watchdog  # noqa
from state_lock import exclusive_file

Q = fleet_runs_root() / "queue"
DIRS = {s: Q / s for s in ("ready", "running", "done", "review", "parked", "failed")}
INDEX = Q / "index.jsonl"
LEASES = Q / "leases"

# --- controller ownership -----------------------------------------------------------------------
# A running record belongs to the controller that started it. Startup used to requeue EVERY record
# in running/ unconditionally, which silently duplicates work whenever a second drain is alive: the
# original process keeps executing the job while the new one runs it again. A record may only be
# reclaimed once its owner's lease has gone stale.
CONTROLLER = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
_HOST = socket.gethostname()      # so a lease's pid is only trusted for liveness on the same host
# LEASE_TTL comes from loop_config so there is ONE place that knows how long a job may run and how
# long its owner's claim survives. DEFECT 2026-09-19: this was a hardcoded 90 while jobs run up to
# job_timeout_s (1800). A lease shorter than the longest job means a still-running job's lease
# expires, a second controller declares it abandoned, and the SAME JOB EXECUTES TWICE -- the
# cardinal sin of a durable queue. loop_config.check() refuses to start if the relationship is
# ever inverted again, so the invariant is enforced rather than merely commented.
LEASE_TTL = _LOOP.lease_ttl_s
LEASE_BEAT = max(5, min(20, LEASE_TTL // 4))   # refresh well inside the TTL


def _lease_path(cid):
    return LEASES / f"{cid}.json"


def touch_lease(cid=CONTROLLER):
    LEASES.mkdir(parents=True, exist_ok=True)
    # `beat` is a per-controller monotonically increasing heartbeat counter. Liveness judged by a
    # PEER is its ADVANCE over the observer's own elapsed time (see supervisor._peer_fresh), which
    # needs no comparison of two machines' wall clocks -- the wall `ts` is kept only for humans.
    prev = 0
    try:
        prev = int(json.loads(_lease_path(cid).read_text(encoding="utf-8")).get("beat", 0))
    except (OSError, ValueError, TypeError):
        prev = 0
    tmp = LEASES / f".{cid}.tmp"
    tmp.write_text(json.dumps({"controller": cid, "pid": os.getpid(), "host": _HOST,
                               "ts": time.time(), "beat": prev + 1}), encoding="utf-8")
    os.replace(tmp, _lease_path(cid))


def _process_alive(pid):
    """Tri-state liveness of a local pid: True alive, False provably gone, None can't tell. The
    safe default is None -> the caller does NOT reclaim, because a false 'gone' duplicates work
    while a false 'alive' only strands a job for the recover path to pick up later."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k.OpenProcess(0x1000, False, pid)           # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False if ctypes.get_last_error() == 87 else None   # 87 = invalid param = no such pid
        try:
            code = wintypes.DWORD()
            if k.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == 259                 # STILL_ACTIVE
            return None
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def lease_owner_stopped(cid):
    """True only when we can PROVE the lease's owner has stopped: its lease names THIS host and its
    pid is provably gone. Cross-host, or an uncheckable pid, returns False (we cannot prove it, so
    we do not reclaim -- holding an uncertain job beats duplicating a live one)."""
    try:
        row = json.loads(_lease_path(cid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # No readable lease. The controller id itself is `host-pid-nonce` (see CONTROLLER), so the
        # owner can still be proven stopped: same host and a provably gone pid. Without this, a
        # controller that died before (or after losing) its lease file stranded its running jobs
        # forever -- "cannot prove it stopped" -- observed live: a job held 4h by a dead pid.
        host, pid = _cid_host_pid(cid)
        if host != _HOST or pid is None:
            return False
        return _process_alive(pid) is False
    if row.get("host") != _HOST:
        return False
    return _process_alive(row.get("pid")) is False


def _cid_host_pid(cid):
    """(host, pid) from a controller id `host-pid-nonce`; (None, None) when it does not parse. The
    host may itself contain dashes, so the pid and nonce are taken from the right."""
    parts = str(cid or "").rsplit("-", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        return None, None
    return parts[0], int(parts[1])


def lease_beat(cid):
    """The controller's current heartbeat counter, or None if it has no lease. This is the
    skew-free liveness signal: an observer watches it ADVANCE against its own clock."""
    if not cid:
        return None
    try:
        return int(json.loads(_lease_path(cid).read_text(encoding="utf-8")).get("beat", 0))
    except (OSError, ValueError, TypeError):
        return None


def lease_alive(cid, ttl=LEASE_TTL):
    """DEPRECATED for cross-machine use: compares the observer's wall clock to the owner's written
    timestamp, so clock skew larger than `ttl` mis-judges a live peer as dead (S-G2). Retained only
    for single-controller/self-lease checks. Multi-controller liveness goes through the skew-free
    `lease_beat` + supervisor._peer_fresh instead."""
    if not cid:
        return False
    try:
        row = json.loads(_lease_path(cid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (time.time() - float(row.get("ts", 0))) <= ttl


# --- claim identity ------------------------------------------------------------------------------
# A claim is a temporary filename inside running/ that a job passes through on its way from ready/
# to its running record. The name carries the CLAIMING CONTROLLER, for two measured reasons:
#
#  1. DUPLICATE EXECUTION, reproduced 2026-09-19. The claim path used to be `.claim.<name>.json` --
#     the SAME path for every controller. Two drains racing for one job then both renamed ready/x
#     onto that one path, both read it, both promoted it to their own running record, and BOTH RAN
#     THE JOB, leaving exactly one file behind. Measured on this platform over 60 races through
#     drain()'s own sequence: 6 races produced 2 executions and 1 leftover record. "One file is
#     left" is precisely the reading that hides this, which is why the tests count executions.
#  2. A crash between the two renames used to strand `.claim.<name>.json` in running/, where
#     reclaim_orphans' `split("__")` could not recover the job's real name and requeued it as
#     `.claim.<name>` -- a job silently renamed into something no card matches.
CLAIM_PREFIX = ".claim."


def _claim_path(name, controller=None):
    return DIRS["running"] / f"{CLAIM_PREFIX}{controller or CONTROLLER}__{name}.json"


def _record_path(worker, name):
    return DIRS["running"] / f"{worker}__{name}.json"


def job_name_of(filename):
    """The job's real name from any filename running/ can hold: a record `worker__job.json`, a
    claim `.claim.controller__job.json`, or a legacy claim `.claim.job.json`. Recovery depends on
    this being exact -- a job requeued under a mangled name is a job nobody will ever run."""
    stem = filename[:-len(".json")] if filename.endswith(".json") else filename
    if stem.startswith(CLAIM_PREFIX):
        stem = stem[len(CLAIM_PREFIX):]
    return stem.split("__", 1)[-1]


def _claim_owner(filename):
    """The controller named in a claim filename, or None for a legacy claim that names none.

    Read from the NAME, never the contents: a claim is written in two steps (rename, then write),
    so a claim caught mid-flight has no `owner` field yet. Judging it by its contents would let a
    passing reclaim steal a claim that a live controller took microseconds ago."""
    stem = filename[len(CLAIM_PREFIX):-len(".json")]
    return stem.split("__", 1)[0] if "__" in stem else None


@dataclass(frozen=True)
class Claim:
    """Proof, not belief. A Claim is only ever returned after the landed record has been READ BACK
    and found to carry this controller's token, so holding one means this process owns the job."""
    name: str
    worker: str
    controller: str
    token: str
    record: Path
    card: dict


def holds_claim(record, controller, token):
    """Does the record on disk RIGHT NOW still belong to this controller and this claim?

    Called again at executor start. Between the claim and the first line of work a lease can
    expire, another controller can reclaim the record, and the job can be re-dispatched; the
    process that then begins executing must confirm the claim it thinks it holds still exists.
    Any read failure is a NO: an unreadable claim is not a held one. So is an EMPTY identity --
    without the first line, holds_claim(record, None, None) matched any record that carries no
    owner, because None == None. That is a fail-open in the one predicate whose whole job is to
    fail closed: a caller that lost track of its token would have been told it owned everything
    unclaimed. Found by its own test, 2026-09-19."""
    if not controller or not token:
        return False
    try:
        card = json.loads(Path(record).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return card.get("owner") == controller and card.get("claim_token") == token


def running_owner(name):
    """The controller currently RUNNING (or holding a claim on) job `name` in the supported queue,
    or None. Alternate, queue-bypassing runners (run/batch.py, run/fleetbatch.py) consult this to
    refuse launching work the queue already owns -- otherwise the same job executes twice, the exact
    thing the claim/lease protocol exists to prevent. Matches by job name across both a running
    record (`worker__name.json`) and a hidden claim (`.claim.owner__name.json`)."""
    d = DIRS["running"]
    if not d.exists():
        return None
    for f in sorted(d.glob("*.json")):
        if job_name_of(f.name) != name:
            continue
        if f.name.startswith(CLAIM_PREFIX):
            return _claim_owner(f.name) or "unknown"
        try:
            return json.loads(f.read_text(encoding="utf-8")).get("owner") or "unknown"
        except (OSError, ValueError):
            return "unknown"
    return None


def alt_run_refusal(name):
    """Refusal message if an alternate runner must NOT launch `name`, else None. Enforced (not a
    doc warning): the supported queue owns it right now, so running it here would duplicate work."""
    owner = running_owner(name)
    if owner:
        return ("refused: the supported queue currently owns job {0!r} (controller {1}). Dispatch it "
                "through the supervisor/queue, not this experimental runner, so the same work is not "
                "executed twice.".format(name, owner))
    return None


def queue_serialized(fn):
    """Every queue OWNERSHIP mutation runs under one exclusive lock on the shared queue dir, so a
    reclaim can never interleave between an ownership check and the settlement it authorises."""
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with exclusive_file(Q / ".ownership-lock"):
            return fn(*args, **kwargs)
    return wrapped


@queue_serialized
def settle_claim(record, destination, controller, token):
    """Validate ownership AND move the record to its terminal/ready destination under ONE lock. A
    previous terminal record is never silently overwritten."""
    record = Path(record)
    destination = Path(destination)
    if not holds_claim(record, controller, token):
        return False
    if destination.exists():
        raise RuntimeError("Queue destination already exists: {0}".format(destination))
    os.replace(record, destination)
    return True


@queue_serialized
def claim(name, worker, controller=None, token=None, extra=None):
    """Take exclusive ownership of ready/<name>.json for `worker`, or return None.

    Returns a Claim whose ownership has been CONFIRMED by reading the landed record back, never a
    Claim inferred from os.replace not raising. That inference is the defect this closes: measured
    over 200 races on this platform, BOTH callers returned from os.replace without raising 200
    times while exactly one record landed, so a clean return proves only that the call finished.
    Dispatching on it is how one job becomes two executions.

    None means "not ours" and is the ordinary outcome of losing a race. The caller must not
    execute, and must not touch the job: whoever does own it is about to run it.

    Every failure path after the first rename PUTS THE CARD BACK in ready/. A claim we took and
    could not finish is not somebody else's job -- it is ours, abandoned -- and leaving it in
    running/ means the job waits for the next recover() to notice. On Windows a transient sharing
    violation (WinError 32, raised when anything holds a handle on the file, including this
    controller's own status tick) is a routine cause of exactly that, so "try again next tick" has
    to be the outcome rather than "wait for a restart"."""
    controller = controller or CONTROLLER
    token = token or uuid.uuid4().hex
    src = DIRS["ready"] / f"{name}.json"
    cl = _claim_path(name, controller)

    def _give_back():
        """Return our own unfinished claim to ready/. Never touches a record we do not own: `cl`
        is controller-unique, so what is being put back is unambiguously this attempt's.

        Retried: the same transient sharing violation that made the claim fail can make the
        give-back fail, and a give-back that silently fails leaves a HIDDEN claim -- a dot-file
        in running/ that no liveness rule counts, so the supervisor concludes nothing is owed and
        goes home with one job never executed (reproduced by an independent audit: four jobs
        queued, three executed, one `.claim.*` left). The supervisor also sweeps its own hidden
        claims every tick, so this retry is the first line, not the only one."""
        for i in range(10):
            try:
                if cl.exists():
                    os.replace(cl, DIRS["ready"] / f"{name}.json")
                return None
            except OSError:
                time.sleep(0.02)
        return None                      # still hidden: the per-tick sweep / reclaim picks it up

    try:
        os.replace(src, cl)
    except OSError:
        return None                      # someone else took it, or it was never there
    try:
        card = json.loads(cl.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _give_back()              # our claim was moved or corrupted under us
    card.update(extra or {})
    card["worker"] = worker
    card["owner"] = controller
    card["claim_token"] = token
    card["claimed"] = _now()
    try:
        cl.write_text(json.dumps(card, indent=2), encoding="utf-8")
    except OSError:
        return _give_back()
    rec = _record_path(worker, name)
    # Never promote ON TOP of an existing record: os.replace would silently overwrite another
    # controller's running job, erasing the owner field that is the only thing protecting it.
    if rec.exists():
        return _give_back()
    try:
        os.replace(cl, rec)              # atomic: claim -> running, never absent
    except OSError:
        return _give_back()
    if not holds_claim(rec, controller, token):
        # The record that landed is not ours. Do NOT dispatch and do NOT touch it: the controller
        # whose token it carries is about to run it, and putting it back would duplicate the work.
        return None
    return Claim(name=name, worker=worker, controller=controller, token=token, record=rec,
                 card=card)


@queue_serialized
def reclaim_orphans(fresh=None):
    """Recover ONLY what is provably safe to recover, under the ownership lock.

    A hidden CLAIM (`.claim.` prefix) is a job caught BEFORE executor dispatch; it can be requeued
    once its claiming process is confirmed stopped. An ordinary running record may already be
    executing remotely -- a dead LOCAL controller does not prove the REMOTE execution stopped -- so
    it is left as recovery-required rather than replayed. This deliberately holds uncertain work
    instead of risking a duplicate execution; `fresh` is accepted for signature compatibility and
    is not used to steal live work."""
    requeued, skipped = [], []
    for record in sorted(DIRS["running"].glob("*.json")):
        name = job_name_of(record.name)
        hidden = record.name.startswith(CLAIM_PREFIX)
        owner = _claim_owner(record.name) if hidden else None
        if not hidden or not owner or not lease_owner_stopped(owner):
            skipped.append(name + ".json")
            continue
        destination = DIRS["ready"] / f"{name}.json"
        if destination.exists():
            skipped.append(name + ".json")
            continue
        os.replace(record, destination)
        requeued.append(name + ".json")
    return requeued, skipped

def recover_dispatched(older_than_s=None):
    """OPERATOR-INVOKED recovery of DISPATCHED running records, which reclaim_orphans deliberately
    leaves (a dead local controller alone does not prove a remote call stopped). Before this there was
    no recovery at all: such a record blocked its job forever, because a new claim refuses to promote
    on top of an existing record -- an interrupted goal could never finish.

    A record is recovered only when BOTH hold:
      * its owner is provably stopped (same host, pid gone -- see lease_owner_stopped); not overridable;
      * it was dispatched more than `older_than_s` ago. The default is LEASE_TTL, which loop_config
        requires to exceed job_timeout_s, so every request that controller made has passed its own
        timeout. A smaller value is the operator's assertion that its remote generation has ended.
    If a ready copy of the job already exists the stale record is ARCHIVED (renamed out of running/,
    kept as evidence); otherwise the record is requeued. Returns (recovered, held) with reasons."""
    older = LEASE_TTL if older_than_s is None else max(0, int(older_than_s))
    recovered, held = [], []
    with exclusive_file(Q / ".ownership-lock"):
        for record in sorted(DIRS["running"].glob("*.json")):
            if record.name.startswith(CLAIM_PREFIX):
                continue                                  # reclaim_orphans owns hidden claims
            name = job_name_of(record.name)
            try:
                card = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                held.append((name, "unreadable record"))
                continue
            owner = card.get("owner")
            if not owner or not lease_owner_stopped(owner):
                held.append((name, "owner {0} not provably stopped".format(owner or "(none)")))
                continue
            age = time.time() - record.stat().st_mtime
            if age < older:
                held.append((name, "dispatched {0:.0f} min ago, under {1:.0f} min".format(age / 60, older / 60)))
                continue
            ready = DIRS["ready"] / f"{name}.json"
            if ready.exists():
                os.replace(record, DIRS["failed"] / (record.name + ".stale-recovered"))
                recovered.append((name, "archived stale record; a ready copy is queued"))
            else:
                os.replace(record, ready)
                recovered.append((name, "requeued"))
    return recovered, held


def _ensure():
    for d in DIRS.values():
        d.mkdir(parents=True, exist_ok=True)

def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")

@queue_serialized
def add(card_paths):
    _ensure(); n = 0
    for p in card_paths:
        p = Path(p)
        if not p.is_file():
            print(f"  skip (not a file): {p}"); continue
        card = json.loads(p.read_text(encoding="utf-8"))
        name = card.get("name") or p.stem
        tmp = DIRS["ready"] / f".{name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(card, indent=2), encoding="utf-8")
        os.replace(tmp, DIRS["ready"] / f"{name}.json")   # atomic
        n += 1; print(f"  queued {name}")
    print(f"enqueued {n} job(s) -> {DIRS['ready']}")

def status():
    _ensure()
    counts = {s: len(list(d.glob("*.json"))) for s, d in DIRS.items()}
    print("queue:", counts)
    if INDEX.exists():
        rows = INDEX.read_text(encoding="utf-8").splitlines()[-8:]
        print("recent outcomes:")
        for r in rows:
            try:
                j = json.loads(r); print(f"  [{j['outcome']:<9}] {j['worker']:<7} {j['name']:<22} {j.get('sec','?')}s")
            except Exception:
                pass

def choose_assignments(ready_names, free_slots, affinity, required=None):
    """Cache-locality scheduling: pair ready jobs with free worker slots, PREFERRING each job's affine
    worker (the one that already holds its warm prefix) before falling back to any free slot.
    free_slots is a list of worker names (repeats = multiple slots). Returns [(name, worker), ...].

    `required` maps a job name to the ONE worker it must run on -- either because its card names a
    worker (I5/I6: an explicit `worker` still works) or because capability selection resolved it.
    A required job takes a slot of that worker or it takes no slot at all; it is NEVER quietly put
    somewhere else, because "the fleet was busy" must not silently change which machine ran a job."""
    required = required or {}
    ready = list(ready_names); slots = list(free_slots); out = []
    for name in list(ready):                      # pass 0: honour an explicit/required worker
        need = required.get(name)
        if not need:
            continue
        ready.remove(name)
        if need in slots:
            slots.remove(need); out.append((name, need))
    for name in list(ready):                      # pass 1: honor affinity
        pref = affinity.get(name)
        if pref and pref in slots:
            slots.remove(pref); ready.remove(name); out.append((name, pref))
    for name in list(ready):                      # pass 2: fill remaining slots
        if not slots:
            break
        out.append((name, slots.pop(0))); ready.remove(name)
    return out


# --- capability selection (INTERFACES.md I6) -----------------------------------------------------
# A card whose `worker` is None says "choose by capability". Selection reads fleet.WORKERS, which
# Track B owns and this module only reads.
#
# The rule that shapes this: SELECTION NEVER READS `kind` OR A WORKER'S NAME. Both are identity,
# not capability, and routing on identity is how a registered endpoint ends up being a second-class
# citizen that the scheduler will not send real work to -- the exact failure onboarding exists to
# end. `kind` and `backend` survive in fleet.py as documentation for logs; nothing here may branch
# on them. Ties are broken by keeping fleet.WORKERS' own order (Python's sort is stable), not by
# comparing names, so no worker wins for being called "cluster".

#: Chars per token, matching call.est_tokens. A guard's estimate must agree with the guard that
#: will actually refuse the request, or selection picks a worker whose context guard then rejects.
_CHARS_PER_TOKEN = 4
#: Room for the completion plus the loop's own framing (reply format, retrieved authority, the
#: rejection tail on a REDO). A brief that only just fits on round 1 will not fit on round 3.
_CTX_HEADROOM = 2048


def capability_requirements(card):
    """What a card NEEDS, derived only from the fixed card shape in INTERFACES.md I5.

    No `requires` key is invented here: widening the card would be an interface change, and a
    requirement a planner has to remember to write down is a requirement that will be missing on
    the card that needed it most. So the needs are read off what every card already carries."""
    text = " ".join(str(card.get(k) or "") for k in ("brief", "oracle"))
    text += " ".join(str(c) for c in (card.get("done_when") or []))
    return {
        "min_ctx": len(text) // _CHARS_PER_TOKEN + _CTX_HEADROOM,
        # A tool job asks the worker for structured calls. Constrained decoding makes a malformed
        # one impossible, so it is PREFERRED -- never required, because the cluster has no GBNF and
        # runs tool jobs today. A preference reorders; a filter would silently shrink the fleet.
        "prefers_gbnf": bool(card.get("tools")),
    }


def select_worker(card, workers=None, healthy=None):
    """Pick a worker for a card by capability, or None when nothing qualifies.

    None is a real answer and the caller must leave the job in ready/. The alternative -- falling
    back to "any worker" -- is how a job with a 40k-token brief gets handed to a 16k-context
    endpoint and comes back as an unexplained failure.

    `healthy(name) -> bool` is injected rather than imported so selection can be tested without a
    live fleet; the drain passes the watchdog. An unhealthy worker is never selected (I6)."""
    table = WORKERS if workers is None else workers
    healthy = healthy or (lambda _name: True)
    need = capability_requirements(card)
    fits = []
    for name, w in table.items():
        try:
            ctx = int(w.get("ctx") or 0)
        except Exception:
            # Deliberately broad. fleet.py fills an unconfigured setting with a poison object that
            # raises SettingsMissing on ANY use -- including the `or 0` truthiness test above, and
            # it is not a TypeError or a ValueError. A clean clone with two workers configured and
            # one not must still be able to schedule onto the two, so an entry we cannot read is
            # simply not a candidate. Found by its own test, 2026-09-19.
            continue
        if ctx < need["min_ctx"]:
            continue
        if not healthy(name):
            continue
        fits.append((name, w, ctx))
    if not fits:
        return None
    # Rank by capability only. Stable sort, so equal-capability workers keep fleet.WORKERS' order
    # and no worker is preferred for its name.
    fits.sort(key=lambda t: (
        0 if (need["prefers_gbnf"] and t[1].get("supports_gbnf")) else 1,
        -int(t[1].get("max_inflight") or 1),
        -t[2],
    ))
    names = [t[0] for t in fits]
    # LF-05: AFTER capability, health and capacity, the qualified candidates are reordered by what
    # happened when each did this kind of work before. Never narrows the set; a candidate with a
    # bad record is moved down, not out, and comes back after RETRY_AFTER skips.
    try:
        import profiles
        ordered, notes = profiles.prefer(card, names)
        ROUTING_NOTES[card.get("name") or ""] = notes
        return ordered[0]
    except Exception:
        return names[0]


ROUTING_NOTES = {}      # card name -> {worker: why}, for the record the executor reads


@queue_serialized
def _augment_record(record_path, card, worker, routing_notes):
    """LF-05 assistance: the record the executor reads gets the help THIS worker's history says it
    needed on this kind of work, plus the routing notes, so the run itself shows why this worker
    and what it was told. Owner/token fields are untouched, so the claim stays verifiable."""
    try:
        import profiles
        assist = profiles.assistance(worker, card)
    except Exception:
        assist = ""
    if not assist and not routing_notes:
        return
    try:
        rec = json.loads(Path(record_path).read_text(encoding="utf-8"))
        if assist:
            rec["brief"] = (rec.get("brief") or "") + "\n\n" + assist
            rec["assisted_by_profile"] = True
            card["brief"] = rec["brief"]
            card["assisted_by_profile"] = True
        if routing_notes:
            rec["routing"] = routing_notes
            card["routing"] = routing_notes
        Path(record_path).write_text(json.dumps(rec, indent=2), encoding="utf-8")
    except (OSError, ValueError):
        pass


def worker_for(card, healthy=None, workers=None):
    """The worker a card must run on: its own `worker` when it names one, else capability
    selection. Returns None when the card asked for a choice and no worker qualifies."""
    named = card.get("worker")
    return named if named else select_worker(card, workers=workers, healthy=healthy)

def _ready_cards():
    """{name: card} for everything in ready/, skipping temp writes and cards that will not parse.

    An unreadable card is left where it is rather than dispatched with an empty dict: a card
    corrupted by a kill mid-write must not be sent to a worker as if it were a brief."""
    out = {}
    for p in sorted(DIRS["ready"].glob("*.json")):
        if p.name.startswith("."):
            continue
        try:
            card = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(card, dict):
            out[p.stem] = card
    return out


def _load_affinity():
    """Which worker last ran each job (from the index) -> its warm-prefix home."""
    aff = {}
    if INDEX.exists():
        for line in INDEX.read_text(encoding="utf-8").splitlines():
            try:
                j = json.loads(line); aff[j["name"]] = j["worker"]
            except Exception:
                pass
    return aff

# Where each terminal outcome from run/verified.py is filed. AWAITING_REVIEW gets its own
# directory: work that SUCCEEDED mechanically and waits on a person is neither a failure nor
# a finished job, and burying it in parked/ is how a queue loses a completed result.
OUTCOME_DIR = {
    "accepted": "done",
    "awaiting-review": "review",
    "parked-undefined": "parked",
    "parked-unsupported": "parked",   # checks passed, nothing bound the bytes to the work
    "parked-stagnant": "parked",
    "parked-max-rounds": "parked",
    "parked-timeout": "parked",       # the packet wall clock ran out (#35); never worker-unavailable
    "worker-unavailable": "ready",      # retryable: the worker was down, the job was not wrong
}


def _classify(rc, stdout, stderr):
    # A capacity/prefill refusal is the control path working as designed: the fleet was full, so the
    # job was declined rather than admitted. That is a RETRYABLE condition, not a defective job --
    # filing it as "failed" would blame the work for the scheduler being busy.
    err = stderr or ""
    if "CapacityTimeout" in err or "PrefillLockTimeout" in err:
        return "ready", "deferred-capacity"
    if "Traceback (most recent call last)" in err:
        return "failed", "CRASH"

    # Prefer the job's DECLARED outcome over a guess from its exit code.
    marker = ""
    for line in (stdout or "").splitlines():
        if line.startswith("FLEET_OUTCOME="):
            marker = line.split("=", 1)[1].strip()
    if marker:
        return OUTCOME_DIR.get(marker, "parked"), marker
    if rc == 0:
        return "done", "accepted"
    if rc in (1, 2):
        return "parked", "parked"
    if rc == 3:
        return "failed", "worker-unavailable"
    return "failed", f"exit{rc}"

DEFAULT_PACKET_SECONDS = 1800
TURNS_PER_ROUND = 3


def packet_wall_clock(card, max_rounds, remaining_s=None):
    """Seconds one packet may run (#35). Sized from its own output allowance -- rounds x a few
    turns x the per-request wait that allowance needs -- never below the old 1800 s, never above
    PACKET_MAX_SECONDS (default 7200) or the drain's remaining time budget."""
    from fleet import setting
    import generation
    worker = card.get("worker")
    tools = bool(card.get("tools"))
    n = int(card.get("max_output_tokens") or (1400 if tools else 4096))
    per_request = generation.request_timeout(worker, n, floor=300 if tools else 200)
    want = max(DEFAULT_PACKET_SECONDS, int(max_rounds) * TURNS_PER_ROUND * per_request)
    cap = int(setting("PACKET_MAX_SECONDS", 7200) or 7200)
    wall = min(want, cap)
    if remaining_s is not None and remaining_s > 0:
        wall = min(wall, max(DEFAULT_PACKET_SECONDS, int(remaining_s)))
    return int(wall)


def _record_timeout(name, wall, sec):
    """A killed packet wrote no result.json; write one so readers see parked-timeout, not a
    directory fallback to worker-unavailable."""
    d = fleet_runs_root() / ("verified-" + name)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "result.json"
    try:
        prior = json.loads(p.read_text(encoding="utf-8"))
        if prior.get("outcome") and os.path.getmtime(p) >= time.time() - sec:
            return                               # the run recorded its own outcome in time
    except (OSError, ValueError):
        pass
    p.write_text(json.dumps({"outcome": "parked-timeout", "rounds": None,
                             "basis": "the dispatcher's packet wall clock ({0}s) ran out after {1}s; the "
                                      "run was stopped mid-round (a harness wait, not a down lane)".format(wall, sec)}),
                 encoding="utf-8")


def drain(workers, max_rounds, max_seconds, redo_mode):
    _ensure()
    # resume: requeue ONLY jobs whose owning controller is gone. A live controller keeps its work.
    touch_lease()
    requeued, skipped = reclaim_orphans()
    for n in requeued:
        print(f"  requeued orphan {n}")
    for n in skipped:
        # Not always a live lease: an already-DISPATCHED record is held on purpose, and only an
        # operator can recover it (`python run/queue.py recover-stale`).
        print(f"  left {n} in running/ (owner live, or dispatched: recover with `queue.py recover-stale`)")
    # capacity = each UP worker's max_inflight (fail-fast drops wedged workers)
    cap = {}
    for w in dict.fromkeys(workers):
        ok, st, detail = watchdog.usable(w)
        if ok:
            cap[w] = WORKERS[w].get("max_inflight", 1)
        else:
            print(f"  SKIP {w}: {st} ({detail})")
    if not cap:
        print("no usable workers; aborting."); return
    print(f"drain: capacity {cap}, max-rounds {max_rounds}" + (f", budget {max_seconds}s" if max_seconds else ""))

    running = {}           # name -> worker
    lock = threading.Lock()
    results = []
    reported = {}          # name -> the blocked reason already printed, so it is said once
    affinity = _load_affinity()   # cache-locality: send a re-queued job back to its warm worker
    t0 = time.time()

    def run_job(name, worker, tmp, card, token):
        # `tmp` already exists on disk (written atomically by the scheduler before the claim was
        # consumed), so there is no point at which this job is untracked.
        # EXECUTOR-START VERIFICATION. Between the claim and this line the record can have been
        # reclaimed by another controller (a stalled heartbeat is enough) and re-dispatched. The
        # process about to spend a worker-hour re-confirms it still owns the claim, and the child
        # re-confirms it again from inside itself via --claim-token, because the gap between this
        # check and the child's first real work is a whole process spawn.
        if not holds_claim(tmp, CONTROLLER, token):
            with lock:
                running.pop(name, None)
                # INDEXED, not merely printed. An abandonment means this controller was handed a
                # claim it did not really own -- the exact false belief the exclusivity claim was
                # retracted over. Silently skipping it would make the defect invisible in exactly
                # the runs where it matters, so it goes in the index where a test and an operator
                # can both count it. Nothing is filed: the job belongs to whoever reclaimed it.
                with open(INDEX, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"name": name, "worker": worker,
                                         "outcome": "abandoned-claim-lost", "sec": 0,
                                         "ts": _now()}) + "\n")
            print(f"  [abandoned] {worker:<7} {name:<22} claim lost before execution", flush=True)
            return
        wall = packet_wall_clock(card, max_rounds,
                                 remaining_s=(max_seconds - (time.time() - t0)) if max_seconds else None)
        cmd = [sys.executable, str(ROOT / "run" / "verified.py"), str(tmp), "--max-rounds", str(max_rounds),
               "--claim-token", token, "--claim-controller", CONTROLLER,
               "--deadline-seconds", str(max(60, wall - 120))]
        if redo_mode:
            cmd += ["--redo-mode", redo_mode]
        t = time.time()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=wall)
            dest_dir, outcome = _classify(r.returncode, r.stdout, r.stderr)
            err_tail = (r.stderr or "").strip()
        except subprocess.TimeoutExpired as e:
            # #35: a harness wait, recorded as such -- never worker-unavailable / infrastructure.
            dest_dir, outcome = "parked", "parked-timeout"
            err_tail = ((e.stderr or "") if isinstance(e.stderr, str) else "")[-800:]
            _record_timeout(name, wall, round(time.time() - t))
        sec = round(time.time() - t)
        # Validate ownership AND move under the one ownership lock, so a reclaim cannot interleave
        # between the check and the settle. If we no longer own it (reclaimed while the job ran),
        # do NOT overwrite the new owner's state -- record the outcome and stop.
        try:
            moved = settle_claim(tmp, DIRS[dest_dir] / f"{name}.json", CONTROLLER, token)
        except (OSError, RuntimeError):
            moved = False
        if not moved:
            outcome = f"{outcome}/record-lost"
        row = {"name": name, "worker": worker, "outcome": outcome, "sec": sec, "ts": _now()}
        if err_tail and outcome not in ("accepted", "done"):
            row["detail"] = err_tail[-800:]
        with lock:
            with open(INDEX, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            results.append(row); running.pop(name, None)
            print(f"  [{outcome:<9}] {worker:<7} {name:<22} {sec:>4}s", flush=True)
            if row.get("detail"):
                print(row["detail"][-500:], flush=True)

    while True:
        if max_seconds and time.time() - t0 > max_seconds:
            print("  budget reached; stopping (in-flight jobs finish; ready jobs stay queued)."); break
        # schedule: fill free capacity from ready/
        with lock:
            load = Counter(running.values())
            free = [w for w in cap for _ in range(cap[w] - load[w])]
        undispatchable = {}
        if free:
            ready_cards = _ready_cards()
            required, unroutable = {}, {}
            for name, card in ready_cards.items():
                want = worker_for(card, healthy=lambda w: w in cap)
                if want is None:
                    # No worker in this fleet has the capability this card needs. Leave it in
                    # ready/ and SAY SO. Silence here is the failure mode that matters: a job that
                    # can never be dispatched, in a queue that looks merely busy.
                    unroutable[name] = "no worker satisfies its capability needs"
                elif want not in cap:
                    unroutable[name] = f"needs worker '{want}', which is not usable in this drain"
                else:
                    required[name] = want
            undispatchable = unroutable
            for name, why in sorted(unroutable.items()):
                if reported.get(name) != why:
                    print(f"  BLOCKED {name}: {why}"); reported[name] = why
            for name, worker in choose_assignments(sorted(required), free, affinity, required=required):
                # ONE call does claim + promote + ownership verification. A None is a lost race:
                # the job belongs to another controller and must not be touched, let alone run.
                token = uuid.uuid4().hex
                held = claim(name, worker, CONTROLLER, token)
                if held is None:
                    continue
                _augment_record(held.record, held.card, worker, ROUTING_NOTES.pop(name, None))
                with lock:
                    running[name] = worker; affinity[name] = worker   # remember its home
                threading.Thread(target=run_job,
                                 args=(name, worker, held.record, held.card, token),
                                 daemon=True).start()
        touch_lease()          # keep this controller's claim on its in-flight work
        with lock:
            # A card nobody can run does not keep the drain alive. Without excluding it here the
            # constrained scheduler polls an unroutable card until the budget expires and reports
            # nothing; the job stays safely in ready/ either way, which is the part that matters --
            # an unroutable job is never destroyed and never silently sent somewhere else.
            pending = [p for p in DIRS["ready"].glob("*.json") if p.stem not in undispatchable]
            done = not pending and not running
        if done:
            break
        time.sleep(2)
    # wait for stragglers
    while True:
        with lock:
            if not running:
                break
        time.sleep(2)
    print("\n=== drain summary ===")
    print(dict(Counter(r["outcome"] for r in results)))
    print(f"{len(results)} jobs in {round(time.time()-t0)}s; index: {INDEX}")

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("add"); a1.add_argument("cards", nargs="+")
    a2 = sub.add_parser("add-dir"); a2.add_argument("dir")
    sub.add_parser("status")
    d = sub.add_parser("drain")
    d.add_argument("--workers", default="cluster,cluster,pairA,pairB")
    d.add_argument("--max-rounds", type=int, default=5)
    d.add_argument("--max-seconds", type=int, default=0, help="0 = until ready is empty")
    d.add_argument("--redo-mode", choices=("conversation", "rewrite"), default=None)
    r = sub.add_parser("recover-stale", help="requeue/archive dispatched jobs whose controller is provably stopped")
    r.add_argument("--older-than", type=int, default=None,
                   help="seconds since dispatch (default: lease TTL); smaller is your assertion that its remote call ended")
    a = ap.parse_args()
    if a.cmd == "add":
        add(a.cards)
    elif a.cmd == "add-dir":
        add(sorted((ROOT / a.dir).glob("*.json")) if not Path(a.dir).is_absolute() else sorted(Path(a.dir).glob("*.json")))
    elif a.cmd == "status":
        status()
    elif a.cmd == "recover-stale":
        recovered, held = recover_dispatched(a.older_than)
        for n, why in recovered:
            print(f"  recovered {n}: {why}")
        for n, why in held:
            print(f"  held {n}: {why}")
    elif a.cmd == "drain":
        drain([w.strip() for w in a.workers.split(",") if w.strip()], a.max_rounds, a.max_seconds, a.redo_mode)

if __name__ == "__main__":
    main()
