"""Milestone 4: the process that is still running -- and still honest -- at 4am.

`queue.drain` can already run a pile of work unattended. What it cannot do is survive the night:
it has no notion of one job needing another, it re-reads nothing when restarted except the running
directory, a job that needs a human decision stops being work and becomes silence, and a capacity
refusal comes back as a bare re-queue that the scheduler immediately picks up again in a hot loop.
The supervisor adds exactly those four things ON TOP of the queue. It does not replace it.

What it reuses rather than reinvents (run/queue.py, milestone 1):
  * the directory-IS-the-state model: runs/queue/{ready,running,done,parked,failed}
  * the atomic claim: ready/x.json -> running/.claim.x.json -> running/worker__x.json, so a job is
    never absent from disk and never exists only in one process's memory
  * controller leases: CONTROLLER / touch_lease / lease_alive / reclaim_orphans. A running record is
    reclaimed ONLY when its owner's lease has expired. Two supervisors on one queue is therefore a
    supported state, not a race. We do not invent a second ownership primitive.
  * _classify, including its ruling that a CapacityTimeout/PrefillLockTimeout is `ready` /
    `deferred-capacity` -- the control path working, not a defective job.

The four additions:

1. DEPENDENCIES. A card may declare `needs: ["other-job"]`. A job is dispatched only once every job
   it needs has LANDED (is in done/). Dependencies are checked against the disk, not against a plan
   held in memory, so a restart inherits them for free. A cycle is refused up front with the cycle
   printed; it is never allowed to become a queue that quietly never finishes. An unsatisfiable
   dependency (the prerequisite parked, failed, or was never enqueued) is reported as BLOCKED with
   the reason, and the supervisor gets on with everything else instead of waiting for a job that is
   never coming.

2. EXACTLY-ONCE ACROSS A KILL. The queue's claim closes the window before execution; the window
   AFTER it was still open -- a supervisor killed between "the job finished" and "its card was moved
   out of running/" would, on restart, find the card in running/ and run the whole job again. So the
   result is journaled (with the dispatch's run_id) BEFORE the card moves, and startup settles any
   running record whose result is already on disk instead of re-executing it. Re-running a finished
   job is not free: it costs a worker-hour and can double a side effect.

3. PARKED QUESTIONS ARE FIRST CLASS. A job that cannot proceed without a human decision parks with
   the question recorded IN FULL -- into the card, the journal and the status page. It is never
   truncated to a log line, because the whole value of an overnight run is that the human wakes up
   able to answer. A parked job blocks only its own dependents; everything else keeps running.

4. CAPACITY REFUSALS ARE NORMAL. A deferred-capacity result is re-queued with exponential backoff
   and counted as a retry, never as an error. Without the backoff the scheduler re-dispatches the
   refused job on the very next tick and spins against a full fleet all night.

Progress is written every tick to runs/queue/supervisor/status.txt -- the whole page fits on one
screen on purpose, because it is read by someone holding coffee.

    python run/supervisor.py plan                       # dependency order, or the cycle it refuses
    python run/supervisor.py run --workers cluster,cluster,pairA --max-seconds 28800
    python run/supervisor.py status                     # what the last/current run is doing

DELIBERATE LIMITS. The supervisor never edits queue.py's globals permanently; it rebinds them for
the duration of a queue call (see `_queue_root`) because queue.py addresses its directories through
module globals and is out of scope to change. That rebinding is not thread-safe, so all queue calls
are made from the control thread only. And exactly-once holds for a kill; it cannot hold for a job
whose own side effects are not idempotent AND whose process dies mid-write -- the supervisor can
promise the job is not STARTED twice, not that a half-written artifact is whole.
"""
import argparse, importlib.util, json, os, sys, threading, time, traceback, uuid
from collections import Counter, namedtuple
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "check"))
from safeio import force_utf8  # noqa
force_utf8()
from state_lock import exclusive_file

# run/queue.py shadows the stdlib `queue` module, so `import queue` here would resolve to whichever
# happens to be first on sys.path -- a coin flip that changes with the caller's cwd. Load it by path
# and register it once, so this module and the tests share ONE module object (and therefore one set
# of DIRS/LEASES globals to point at a temp root).
if "fleet_queue" in sys.modules:
    queuelib = sys.modules["fleet_queue"]
else:
    _spec = importlib.util.spec_from_file_location("fleet_queue", Path(__file__).resolve().parent / "queue.py")
    queuelib = importlib.util.module_from_spec(_spec)
    sys.modules["fleet_queue"] = queuelib
    _spec.loader.exec_module(queuelib)

STATES = ("ready", "running", "done", "parked", "failed")
QUESTION_MARKER = "QUESTION:"

#: What an executor hands back. Deliberately the same shape queue.drain gets from subprocess.run,
#: so `queuelib._classify` can rule on it unchanged.
Execution = namedtuple("Execution", "rc stdout stderr")


class DependencyCycle(Exception):
    """Refusing to start is the correct response to a cycle. A cycle cannot be scheduled, and a
    supervisor that merely finds nothing eligible looks exactly like one waiting on slow work --
    the failure would present at 8am as "it did nothing all night" with no reason attached."""

    def __init__(self, cycle):
        self.cycle = list(cycle)
        super().__init__("dependency cycle: " + " -> ".join(self.cycle))


def needs_of(card):
    """Prerequisites declared by a card. `needs` is canonical; `depends_on` is accepted because the
    conductor's planner emits that spelling."""
    raw = card.get("needs") or card.get("depends_on") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(x) for x in raw if str(x).strip()]


def plan_order(cards, landed=()):
    """Kahn's algorithm over the UNFINISHED jobs. Returns names in an order that never puts a job
    before something it needs. Raises DependencyCycle naming the actual cycle.

    Dependencies on jobs that already landed are dropped rather than treated as missing: a resumed
    run must not re-derive a cycle out of history. Dependencies on jobs that are neither present nor
    landed are left in the graph -- they cannot deadlock it (they have no outgoing edges), and the
    caller reports them as unreachable."""
    landed = set(landed)
    incoming = {n: {d for d in needs_of(c) if d not in landed} for n, c in cards.items()}
    ready = sorted(n for n, deps in incoming.items() if not (deps & set(cards)))
    order, remaining = [], dict(incoming)
    while ready:
        n = ready.pop(0)
        order.append(n)
        remaining.pop(n, None)
        freed = []
        for m, deps in remaining.items():
            if n in deps:
                deps.discard(n)
                if not (deps & set(remaining)) and m not in ready:
                    freed.append(m)
        ready = sorted(set(ready) | set(freed))
    if remaining:
        raise DependencyCycle(_find_cycle(remaining))
    return order


def _find_cycle(graph):
    """Name ONE concrete cycle. "there is a cycle somewhere in 40 cards" is not actionable at 4am."""
    colour, stack = {}, []

    def walk(n):
        colour[n] = "grey"
        stack.append(n)
        for d in sorted(graph.get(n, ())):
            if d not in graph:
                continue
            if colour.get(d) == "grey":
                return stack[stack.index(d):] + [d]
            if d not in colour:
                hit = walk(d)
                if hit:
                    return hit
        stack.pop()
        colour[n] = "black"
        return None

    for n in sorted(graph):
        if n not in colour:
            hit = walk(n)
            if hit:
                return hit
    return sorted(graph)


def extract_question(execution):
    """The full question, not a summary. Everything from the QUESTION: marker to the end of the
    stream is kept verbatim -- a decision a human has to make is usually several sentences and a
    couple of options, and a status page that shows only the first line of it is useless."""
    for text in (execution.stdout or "", execution.stderr or ""):
        idx = text.find(QUESTION_MARKER)
        if idx >= 0:
            return text[idx + len(QUESTION_MARKER):].strip()
    tail = [l.strip() for l in (execution.stdout or "").splitlines() if l.strip()]
    if tail:
        return tail[-1]
    return f"(job parked with exit {execution.rc} and stated no question)"


def verified_executor(max_rounds=5, redo_mode=None, timeout=1800):
    """The real executor: the same verified.py invocation queue.drain uses. Kept behind a factory so
    a test can substitute a stub and never start a worker or call a model."""
    import subprocess

    def execute(card, card_path, worker):
        cmd = [sys.executable, str(ROOT / "run" / "verified.py"), str(card_path),
               "--max-rounds", str(max_rounds)]
        # Hand the claim through to the child so it can re-verify ownership from inside itself.
        # The gap between the parent's check and the child's first real work is a process spawn,
        # which is long enough for a lease to expire on a loaded box.
        if card.get("claim_token") and card.get("owner"):
            cmd += ["--claim-token", str(card["claim_token"]),
                    "--claim-controller", str(card["owner"])]
        if redo_mode:
            cmd += ["--redo-mode", redo_mode]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return Execution(124, "", f"TIMEOUT after {timeout}s")
        return Execution(r.returncode, r.stdout, r.stderr)

    return execute


class Supervisor:
    """Durable, dependency-aware, unattended. One instance per queue root per process."""

    def __init__(self, root=None, workers=("cluster",), execute=None, tick=2.0,
                 backoff_base=30.0, backoff_cap=900.0, max_capacity_retries=8,
                 probe=False, on_cycle="refuse", clock=time.time, controller=None):
        # One identity per supervisor, defaulting to queue.py's process-wide CONTROLLER. It is a
        # parameter because ownership is the thing that stops two supervisors duplicating work, and
        # that rule has to be testable with two supervisors inside one process.
        self._controller = controller or queuelib.CONTROLLER
        self.root = Path(root) if root else queuelib.Q
        self.dirs = {s: self.root / s for s in STATES}
        self.leases = self.root / "leases"
        self.index = self.root / "index.jsonl"
        self.home = self.root / "supervisor"
        self.journal = self.home / "journal.jsonl"
        self.workers = list(workers)
        self.execute = execute or verified_executor()
        self.tick = tick
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.max_capacity_retries = max_capacity_retries
        self.probe = probe
        self.on_cycle = on_cycle
        self.clock = clock
        self._lock = threading.Lock()
        self._affinity = {}          # name -> the worker holding its warm prefix
        self._inflight = {}          # name -> {worker, run_id, started, attempt}
        self._attempts = {}          # name -> capacity deferrals so far
        self._not_before = {}        # name -> earliest clock() at which it may be dispatched again
        self._questions = {}         # name -> the full parked question
        self._detail = {}            # name -> one-line outcome detail for failed/ jobs
        self._peer_watch = {}        # peer controller -> (last beat seen, self.clock() when seen)
        for d in list(self.dirs.values()) + [self.leases, self.home]:
            d.mkdir(parents=True, exist_ok=True)
        self._replay()

    # --- reaching into queue.py without editing it ----------------------------------------------

    @contextmanager
    def _queue_root(self):
        """queue.py names its directories in module globals. Pointing its lease and reclaim
        primitives at this supervisor's root means rebinding them, so we do it explicitly, briefly,
        and restore afterwards -- rather than copying reclaim_orphans into this file, where the two
        copies would drift and the ownership rule would exist twice. Control thread only."""
        saved = {k: getattr(queuelib, k) for k in ("Q", "DIRS", "LEASES", "INDEX", "CONTROLLER")}
        queuelib.Q, queuelib.DIRS = self.root, dict(self.dirs)
        queuelib.LEASES, queuelib.INDEX = self.leases, self.index
        queuelib.CONTROLLER = self._controller
        try:
            yield queuelib
        finally:
            for k, v in saved.items():
                setattr(queuelib, k, v)

    @property
    def controller(self):
        return self._controller

    def touch_lease(self):
        # Pass the id EXPLICITLY. queue.touch_lease's signature is `cid=CONTROLLER`, whose default
        # was bound at import: rebinding queuelib.CONTROLLER does not reach it, so calling it bare
        # writes a lease for the wrong controller and this supervisor's work looks unowned.
        with self._queue_root() as q:
            q.touch_lease(self._controller)

    def _peer_fresh(self, cid, observe=True):
        """Should this peer's job be LEFT ALONE (not reclaimed)? Reclaim -- which re-dispatches and
        can duplicate side effects -- happens only when the peer is PROVABLY gone, never merely
        because it went quiet.

          * no lease at all      -> not held (reclaim): there is no owner.
          * provable death       -> not held (reclaim): the lease names THIS host and its pid is gone.
          * everything else      -> HELD: a present lease, whether its beat is advancing (definitely
                                    alive) or stalled (silent but unproven). SILENCE IS NOT DEATH:
                                    taking over a silent cross-host peer would re-run its work with
                                    no proof it stopped, so we hold it and surface it (_peer_silent)
                                    for an operator or an idempotency signal instead of stealing it.

        The heartbeat beat is still watched (for _peer_silent's active/quiet distinction), but it no
        longer drives the reclaim decision. `observe=False` reads without updating the watch."""
        beat = queuelib.lease_beat(cid)
        if beat is None:
            if observe:
                self._peer_watch.pop(cid, None)
            return False
        if queuelib.lease_owner_stopped(cid):
            if observe:
                self._peer_watch.pop(cid, None)
            return False
        seen = self._peer_watch.get(cid)
        if observe and (seen is None or beat > seen[0]):
            self._peer_watch[cid] = (beat, self.clock())
        return True                           # present and not provably dead -> hold, do not steal

    def _settle_under_lock(self, record, destination, owner, token):
        """Validate ownership AND move the record to `destination` under the SAME ownership lock
        that reclaim_orphans uses, so a reclaim can never interleave between the check and the move
        (boundary #2). Crucially this does NOT go through `_queue_root`: publishing runs on WORKER
        threads, and rebinding queue.py's module globals there would race the control thread. It
        locks the path directly and calls the global-free `holds_claim`. Reentrant, so recover()
        (already under this lock) may call it too."""
        destination = Path(destination)
        with exclusive_file(self.root / ".ownership-lock"):
            if not queuelib.holds_claim(record, owner, token):
                return False
            if destination.exists():
                raise RuntimeError("Queue destination already exists: {0}".format(destination))
            _replace(record, destination)
            return True

    def _peer_silent(self, cid, ttl=None):
        """A present, not-provably-dead peer whose beat has not advanced for longer than the
        observer's OWN elapsed `ttl`. These are HELD (not reclaimed) but surfaced as uncertain:
        either the peer is wedged/dead cross-host, or it is slow -- we cannot tell, so we do not
        silently steal and we do not silently strand; we say so."""
        ttl = queuelib.LEASE_TTL if ttl is None else ttl
        beat = queuelib.lease_beat(cid)
        if beat is None or queuelib.lease_owner_stopped(cid):
            return False
        seen = self._peer_watch.get(cid)
        return seen is not None and beat == seen[0] and (self.clock() - seen[1]) > ttl

    # --- the journal: supervisor metadata the queue directories cannot carry --------------------

    def _write(self, row):
        row = dict(row, ts=self.clock(), controller=self.controller)
        self.home.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with open(self.journal, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        return row

    def _rows(self):
        if not self.journal.exists():
            return []
        out = []
        for line in self.journal.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue          # a torn last line after a kill is expected; skip it, don't die
        return out

    def _replay(self):
        """Rebuild retry counts, backoff deadlines and parked questions from disk. Without this a
        restart forgets that a job has already been refused six times and starts the backoff over,
        which is how a wedged worker turns into an all-night hot loop."""
        self._results = {}        # run_id -> result row, for settling an unfiled finished job
        self._settled = set()     # run_ids whose card reached its terminal directory
        for row in self._rows():
            ev, name = row.get("event"), row.get("job")
            if ev == "result":
                self._results[row.get("run_id")] = row
                self._attempts[name] = row.get("attempt", self._attempts.get(name, 0))
                if row.get("not_before") is not None:
                    self._not_before[name] = row["not_before"]
                else:
                    self._not_before.pop(name, None)
                if row.get("question"):
                    self._questions[name] = row["question"]
                if row.get("detail"):
                    self._detail[name] = row["detail"]
            elif ev == "settled":
                self._settled.add(row.get("run_id"))

    # --- disk is the state ----------------------------------------------------------------------

    def _names(self, state):
        return sorted(p.stem for p in self.dirs[state].glob("*.json") if not p.name.startswith("."))

    def _read_card(self, path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _ready_cards(self):
        """Cards in ready/, skipping any that will not parse. A card corrupted by a kill mid-write
        must not take the whole night down, so it is filed as failed and named in the status."""
        out = {}
        for p in sorted(self.dirs["ready"].glob("*.json")):
            if p.name.startswith("."):
                continue
            card = self._read_card(p)
            if card is None:
                self._detail[p.stem] = "unreadable card (not valid JSON)"
                self._write({"event": "result", "job": p.stem, "run_id": None, "dest": "failed",
                             "outcome": "unreadable-card", "detail": self._detail[p.stem]})
                _replace(p, self.dirs["failed"] / p.name)
                continue
            out[p.stem] = card
        return out

    def _running_records(self):
        """{name: (path, card)} for every record in running/, ours and other controllers'."""
        out = {}
        for p in sorted(self.dirs["running"].glob("*.json")):
            if p.name.startswith("."):
                continue
            name = p.name.split("__", 1)[-1][:-len(".json")]
            out[name] = (p, self._read_card(p) or {})
        return out

    def landed(self):
        return set(self._names("done"))

    # --- resume ---------------------------------------------------------------------------------

    def recover(self):
        with exclusive_file(self.root / ".ownership-lock"):
            """Startup, in the only order that is safe:

            1. Settle any running record whose RESULT IS ALREADY ON DISK. Its job finished; only the
               file move was interrupted. Re-running it would be duplicated work -- the exact thing the
               milestone forbids -- so the recorded outcome is filed instead.
            2. Then hand the rest to queue.reclaim_orphans(), which requeues only records whose owning
               controller's lease has expired. A live sibling supervisor keeps its work.

            Returns (settled, requeued, held_by_others)."""
            settled = []
            for name, (path, card) in self._running_records().items():
                run_id, owner = card.get("run_id"), card.get("owner")
                row = self._results.get(run_id)
                if not run_id or not row or run_id in self._settled:
                    continue
                with self._queue_root() as q:
                    if owner and owner != self.controller and self._peer_fresh(owner):
                        continue      # its owner is alive and will file it itself; touching it races
                dest = row.get("dest", "failed")
                if dest == "ready":
                    self._not_before.setdefault(name, self.clock())
                moved = self._settle_under_lock(path, self.dirs[dest] / f"{name}.json",
                                                owner, card.get("claim_token"))
                if not moved:
                    continue                      # ownership changed under us; leave it for its owner
                self._write({"event": "settled", "job": name, "run_id": run_id, "dest": dest,
                             "recovered": True})
                self._settled.add(run_id)
                settled.append(name)
            with self._queue_root() as q:
                q.touch_lease(self._controller)
                requeued, skipped = q.reclaim_orphans(fresh=self._peer_fresh)
            self._write({"event": "session", "settled": settled,
                         "requeued": [n[:-len('.json')] for n in requeued],
                         "held": [n[:-len('.json')] for n in skipped]})
            return ([n for n in settled],
                    [n[:-len(".json")] for n in requeued],
                    [n[:-len(".json")] for n in skipped])

        # --- planning -------------------------------------------------------------------------------

    def unfinished(self):
        """Every job still owed: ready/ plus running/. This is the graph a plan is computed over."""
        cards = self._ready_cards()
        for name, (_p, card) in self._running_records().items():
            cards.setdefault(name, card)
        return cards

    def plan(self):
        """The dependency order for the work still owed. Raises DependencyCycle."""
        return plan_order(self.unfinished(), self.landed())

    def check_plan(self):
        with exclusive_file(self.root / ".ownership-lock"):
            """Refuse a cycle before a single job starts, or quarantine the cyclic cards and run the
            rest -- `on_cycle="quarantine"` is for an overnight run where one bad card should not cost
            the whole night. Returns the list of quarantined names (empty when the plan is clean)."""
            quarantined = []
            while True:
                try:
                    self.plan()
                    return quarantined
                except DependencyCycle as exc:
                    if self.on_cycle != "quarantine":
                        raise
                    for name in dict.fromkeys(exc.cycle):
                        src = self.dirs["ready"] / f"{name}.json"
                        if not src.exists():
                            continue
                        self._detail[name] = "dependency cycle: " + " -> ".join(exc.cycle)
                        self._write({"event": "result", "job": name, "run_id": None, "dest": "failed",
                                     "outcome": "dependency-cycle", "detail": self._detail[name]})
                        _replace(src, self.dirs["failed"] / f"{name}.json")
                        quarantined.append(name)
                    if not quarantined:
                        raise

    def blockers(self, card, landed, ready, mine, running):
        """Why this job cannot run yet, as a list of (dep, reason). Empty means every prerequisite
        has landed. The reason strings exist for the human, not the scheduler -- "needs rates-table
        (parked -- needs a human answer)" is a sentence someone can act on at breakfast."""
        out = []
        for dep in needs_of(card):
            if dep in landed:
                continue
            if dep in mine:
                out.append((dep, "in flight"))
            elif dep in running:
                out.append((dep, "held by another controller"))
            elif dep in ready:
                out.append((dep, "queued"))
            elif dep in set(self._names("parked")):
                out.append((dep, "parked -- needs a human answer"))
            elif dep in set(self._names("failed")):
                out.append((dep, "failed"))
            else:
                out.append((dep, "never enqueued"))
        return out

    def liveness(self, ready, landed, mine):
        """Which queued jobs can EVER run from here, computed as a fixpoint over the graph.

        Per-job blocker checking is not enough, and the difference is the whole bug: if A failed,
        B needs A and C needs B, then C's only blocker is B, which is sitting harmlessly in ready/.
        Judged one hop at a time C looks like it is merely waiting, and the supervisor idles until
        morning on a job that can never start. A job is live only if every prerequisite has landed,
        is in flight HERE, or is itself live. Everything else is reported and stops holding the
        loop open.

        A prerequisite held by another controller is deliberately NOT live for us: a sibling
        supervisor may well finish it, but it may equally have been killed, and blocking our night
        on a process we do not own is the wrong default. We report it and move on."""
        live, changed = set(), True
        while changed:
            changed = False
            for name, card in ready.items():
                if name in live:
                    continue
                if all(d in landed or d in mine or d in live for d in needs_of(card)):
                    live.add(name)
                    changed = True
        return live

    # --- dispatch -------------------------------------------------------------------------------

    def _capacity(self):
        cap = Counter(self.workers)
        if not self.probe:
            return dict(cap)
        usable = {}
        for w in cap:
            ok, st, detail = queuelib.watchdog.usable(w)
            if ok:
                usable[w] = cap[w]
            else:
                print(f"  SKIP {w}: {st} ({detail})")
        return usable

    def _own_hidden_claims(self):
        """This controller's `.claim.<controller>__<job>.json` files in running/.

        `_dispatch` runs synchronously in the control thread, so any such file seen at the top
        of a tick is a claim that was taken and could not be finished -- ours, abandoned. It is
        in neither ready/ nor in-flight nor a running record, so no other rule counts it, and
        the loop would otherwise decide nothing is owed."""
        prefix = f"{queuelib.CLAIM_PREFIX}{self.controller}__"
        out = {}
        for f in self.dirs["running"].glob(prefix + "*.json"):
            out[f.name[len(prefix):-len(".json")]] = f
        return out

    def _sweep_own_claims(self):
        with exclusive_file(self.root / ".ownership-lock"):
            """Return every own hidden claim to ready/. Returns the names still hidden afterwards."""
            stuck = []
            for name, f in self._own_hidden_claims().items():
                try:
                    _replace(f, self.dirs["ready"] / f"{name}.json", tries=5, wait=0.02)
                    self._write({"event": "claim-returned", "job": name,
                                 "detail": "own claim found stranded in running/; put back in ready/"})
                except OSError as e:
                    stuck.append(name)
                    self._detail[name] = f"own claim stuck in running/: {e}"
            return stuck

    def _free_slots(self, cap):
        with self._lock:
            load = Counter(j["worker"] for j in self._inflight.values())
        return [w for w in cap for _ in range(cap[w] - load[w])]

    def _delay(self, attempt):
        """Exponential backoff for a capacity refusal, capped. attempt is 1 for the first refusal."""
        return min(self.backoff_cap, self.backoff_base * (2 ** max(0, attempt - 1)))

    def _dispatch(self, name, worker, card):
        """Claim and start one job, through queue.claim -- the SAME primitive drain uses.

        This used to be a hand-copied version of drain's claim sequence, and it inherited drain's
        defect: it dispatched on os.replace not raising. That is not ownership. Measured over 60
        races through exactly this sequence, 6 produced two executions and one leftover record.
        queue.claim reads the landed record back and only then says the job is ours, so a lost
        race returns None here and nothing starts."""
        run_id = uuid.uuid4().hex
        token = uuid.uuid4().hex
        attempt = self._attempts.get(name, 0)
        with self._queue_root() as q:
            held = q.claim(name, worker, self.controller, token,
                           extra={"run_id": run_id, "attempt": attempt})
        if held is None:
            # Another controller owns it, or the card moved under us. Do not start it and do not
            # touch it: whoever owns it is about to run it.
            return False
        card, record = held.card, held.record
        with self._lock:
            self._inflight[name] = {"worker": worker, "run_id": run_id,
                                    "started": self.clock(), "attempt": attempt,
                                    "token": token}
        self._write({"event": "dispatch", "job": name, "worker": worker, "run_id": run_id,
                     "attempt": attempt})
        self._affinity[name] = worker          # cache locality: its warm prefix now lives here
        # A bare thread per job, exactly as queue.drain does -- NOT a ThreadPoolExecutor. Importing
        # concurrent.futures.thread runs `import queue`, and with run/ on sys.path (which it always
        # is when this file is executed as a script) that resolves to run/queue.py, whereupon the
        # executor dies on `queue.SimpleQueue`. Concurrency here is already bounded by free slots.
        threading.Thread(target=self._run_one, name=f"job-{name}", daemon=True,
                         args=(name, worker, record, card, run_id)).start()
        return True

    def _run_one(self, name, worker, record, card, run_id):
        try:
            return self._execute_and_file(name, worker, record, card, run_id)
        except Exception:
            # A job thread that dies without clearing its in-flight entry would leave the control
            # loop waiting all night for a job nobody is running. Release the slot, then say so.
            with self._lock:
                self._inflight.pop(name, None)
            self._write({"event": "thread-crash", "job": name, "run_id": run_id,
                         "detail": traceback.format_exc()[-600:]})
            self._detail[name] = "supervisor thread crashed: " + (_last_line(traceback.format_exc()))
            return None

    def _execute_and_file(self, name, worker, record, card, run_id):
        t0 = self.clock()
        attempt0 = card.get("attempt", 0)
        # EXECUTOR-START VERIFICATION. A dispatch decision ages: between the claim and this line a
        # stalled heartbeat can let another controller reclaim the record and re-dispatch the job.
        # The process about to spend a worker-hour re-confirms it still owns the claim it took,
        # and abandons quietly if it does not -- the alternative is two processes running one job.
        with self._queue_root() as q:
            mine = q.holds_claim(record, self.controller, card.get("claim_token"))
        if not mine:
            self._write({"event": "abandoned", "job": name, "run_id": run_id, "worker": worker,
                         "detail": "claim no longer held at executor start; not executing"})
            with self._lock:
                self._inflight.pop(name, None)
            return None
        try:
            out = self.execute(card, record, worker)
            if not isinstance(out, Execution):
                out = Execution(*out)
        except Exception:
            # An executor that raises must not take the supervisor down at 3am. Hand the traceback
            # to _classify, which already rules that a traceback is a CRASH rather than a park.
            out = Execution(-1, "", traceback.format_exc())
        dest, outcome = queuelib._classify(out.rc, out.stdout, out.stderr)
        sec = round(self.clock() - t0)
        row = {"event": "result", "job": name, "run_id": run_id, "worker": worker,
               "dest": dest, "outcome": outcome, "attempt": attempt0, "sec": sec}
        if dest == "parked":
            row["question"] = extract_question(out)
        if dest == "failed":
            row["detail"] = _last_line(out.stderr) or _last_line(out.stdout) or outcome
        if outcome == "deferred-capacity":
            # NOT an error: the fleet was full and refused. Retry it later, and remember how many
            # times, so a permanently full fleet eventually becomes a question rather than a loop.
            attempt = attempt0 + 1
            row["attempt"] = attempt
            if attempt > self.max_capacity_retries:
                row["dest"], row["outcome"] = "parked", "capacity-exhausted"
                row["question"] = (
                    f"{name} was refused for capacity {attempt} times in a row and never got a "
                    f"slot. The fleet has been full or wedged for the whole backoff window "
                    f"(last wait {self._delay(attempt - 1):.0f}s). Should this job be dropped, "
                    f"moved to another worker, or is a worker down and needing recovery?")
            else:
                row["not_before"] = self.clock() + self._delay(attempt)

        # Journal the result BEFORE moving the card. A kill between these two lines is the window
        # that used to cost a duplicate execution; recover() now finds the recorded result and files
        # the card instead of re-running the job.
        self._write(row)
        dest = row["dest"]
        with self._lock:
            # Record the backoff deadline BEFORE the card goes back into ready/. The other order
            # had a hole a few milliseconds wide: the control thread saw a queued job carrying no
            # deadline and re-dispatched it at once, so a capacity refusal spun exactly as fast as
            # it would have with no backoff at all -- while the retry counter still ticked up.
            self._attempts[name] = row["attempt"]
            if row.get("not_before") is not None:
                self._not_before[name] = row["not_before"]
            else:
                self._not_before.pop(name, None)
            if row.get("question"):
                self._questions[name] = row["question"]
            if row.get("detail"):
                self._detail[name] = row["detail"]
        # FENCING AT PUBLISH. Symmetric to the executor-start check: a stalled heartbeat can let a
        # peer reclaim and re-dispatch this job DURING the run, so the process that spent the
        # worker-hour re-confirms it still owns the claim before filing the result. A superseded
        # owner must not publish -- otherwise it would move the peer's fresh running record to
        # done/failed under its own stale result. The per-claim `claim_token` is the fence: a
        # reclaim rewrites it, so holds_claim() fails for the superseded owner. The result row is
        # already journaled under THIS run_id; the peer's record carries the peer's run_id, so
        # recover() never settles the peer's job with this result.
        published = self._settle_under_lock(record, self.dirs[dest] / f"{name}.json",
                                            self.controller, card.get("claim_token"))
        if not published:
            self._write({"event": "abandoned-at-publish", "job": name, "run_id": run_id,
                         "detail": "Ownership changed; result was not published"})
            with self._lock:
                self._inflight.pop(name, None)
            return None
        self._write({"event": "settled", "job": name, "run_id": run_id, "dest": dest})
        with self._lock:
            self._settled.add(run_id)
            self._inflight.pop(name, None)
        # Keep queue.py's own index readable: `python run/queue.py status` must still work.
        with open(self.index, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"name": name, "worker": worker, "outcome": row["outcome"],
                                 "sec": sec, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
        return row

    # --- the loop -------------------------------------------------------------------------------

    def run(self, max_seconds=0):
        """Run until nothing is owed (or the budget expires). Returns the final status dict."""
        self.recover()
        self.check_plan()
        cap = self._capacity()
        if not cap:
            raise RuntimeError("no usable workers; refusing to start a run that cannot dispatch")
        t0 = self.clock()
        with self._queue_root() as q:
            self._affinity = q._load_affinity()   # send a re-queued job back to its warm worker
        try:
            while True:
                self.touch_lease()      # our in-flight work stays ours while we are alive
                # Sample our in-flight set FIRST, and decide termination on that sample. Reading it
                # last left a gap: a job thread that moved its card out of running/ and cleared its
                # in-flight entry in between the two reads was in neither, so the loop concluded
                # nothing was owed and went home mid-run -- most visibly on a capacity retry, whose
                # card is on its way back to ready/ at exactly that moment.
                with self._lock:
                    mine = set(self._inflight)
                # Our own stranded claims first: put back, or held open as waiting. A job we took
                # and could not start is still owed by us -- it must not vanish from the count.
                stuck_claims = self._sweep_own_claims()
                landed = self.landed()
                ready = self._ready_cards()
                running = self._running_records()
                now = self.clock()

                live = self.liveness(ready, landed, mine)
                dispatchable = [n for n in sorted(live)
                                if n not in mine          # belt and braces: a job finishing right
                                                          # now is briefly both in ready/ and
                                                          # in-flight, and must not be started twice
                                and not self.blockers(ready[n], landed, set(ready), mine, set(running))
                                and self._not_before.get(n, 0) <= now]
                # A live job that is not dispatchable yet is WAITING (its prerequisite is running,
                # or it is inside a capacity backoff). A job outside `live` is never coming and must
                # not hold the loop open.
                waiting = bool(live - set(dispatchable)) or bool(stuck_claims)

                free = self._free_slots(cap)
                if free and dispatchable:
                    for name, worker in queuelib.choose_assignments(dispatchable, free, self._affinity):
                        self._dispatch(name, worker, ready[name])

                self.write_status()
                if not mine and not dispatchable and not waiting:
                    break               # nothing owed that can ever become runnable here
                if max_seconds and self.clock() - t0 > max_seconds:
                    print("  budget reached; in-flight jobs finish, queued jobs stay queued.")
                    break
                time.sleep(self.tick)
        finally:
            # In-flight jobs are always allowed to finish and file their own results, budget or
            # not: abandoning one here is how a card is left stranded in running/ for no reason.
            self._drain_inflight()
        self.write_status()
        return self.status()

    def _drain_inflight(self):
        while True:
            with self._lock:
                if not self._inflight:
                    return
            time.sleep(self.tick)

    # --- what a human reads ----------------------------------------------------------------------

    def status(self):
        landed = sorted(self.landed())
        ready = self._ready_cards()
        running = self._running_records()
        now = self.clock()
        with self._lock:
            mine = dict(self._inflight)
        in_flight = [{"name": n, "worker": j["worker"], "sec": round(now - j["started"])}
                     for n, j in sorted(mine.items())]
        foreign = []
        for name, (_p, card) in sorted(running.items()):
            if name in mine:
                continue
            owner = card.get("owner")
            with self._queue_root() as q:
                held = self._peer_fresh(owner, observe=False)
                silent = self._peer_silent(owner)
            state = ("held: owner silent, death unproven (awaiting proof or idempotency)" if silent
                     else "held by a live controller" if held else "orphaned")
            foreign.append({"name": name, "owner": owner or "unknown", "state": state})
        live = self.liveness(ready, set(landed), set(mine))
        retrying, blocked = [], []
        for name, card in sorted(ready.items()):
            reasons = self.blockers(card, set(landed), set(ready), set(mine), set(running))
            if reasons:
                blocked.append({"name": name,
                                "needs": [{"job": d, "why": w} for d, w in reasons],
                                "reachable": name in live})
            elif self._not_before.get(name, 0) > now:
                retrying.append({"name": name, "attempt": self._attempts.get(name, 0),
                                 "in_s": round(self._not_before[name] - now),
                                 "why": "capacity refusal (normal)"})
        parked = [{"name": n, "question": self._questions.get(n, "(question not recorded)")}
                  for n in self._names("parked")]
        failed = [{"name": n, "detail": self._detail.get(n, "")} for n in self._names("failed")]
        # Everything queued that is neither blocked nor backing off. Without this line a status
        # read before a run starts reports only the problems and silently omits the actual work.
        queued = sorted(set(ready) - {b["name"] for b in blocked}
                        - {r["name"] for r in retrying} - set(mine))
        return {"controller": self.controller, "root": str(self.root),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "landed": landed, "in_flight": in_flight, "retrying": retrying, "queued": queued,
                "blocked": blocked, "parked": parked, "failed": failed, "foreign": foreign,
                "counts": {"landed": len(landed), "in_flight": len(in_flight),
                           "retrying": len(retrying), "queued": len(queued),
                           "blocked": len(blocked), "parked": len(parked), "failed": len(failed)}}

    def render(self, st=None):
        """One screen. Ordered by what a person does about it: questions they must answer first,
        then what broke, then what is merely waiting."""
        st = st or self.status()
        c = st["counts"]
        L = [f"supervisor {st['ts']}  controller {st['controller']}",
             f"  landed {c['landed']}  in-flight {c['in_flight']}  queued {c['queued']}  "
             f"retrying {c['retrying']}  blocked {c['blocked']}  parked {c['parked']}  "
             f"failed {c['failed']}"]
        if st["parked"]:
            L.append("PARKED -- needs your answer:")
            for p in st["parked"]:
                L.append(f"  {p['name']}")
                for line in str(p["question"]).splitlines() or [""]:
                    L.append(f"      {line}")
        if st["failed"]:
            L.append("FAILED:")
            L += [f"  {f['name']:<24} {f['detail']}" for f in st["failed"]]
        if st["in_flight"]:
            L.append("IN FLIGHT:")
            L += [f"  {j['name']:<24} {j['worker']:<8} {j['sec']:>5}s" for j in st["in_flight"]]
        if st["retrying"]:
            L.append("RETRYING (capacity refusals are normal, not errors):")
            L += [f"  {r['name']:<24} attempt {r['attempt']}, next in {r['in_s']}s"
                  for r in st["retrying"]]
        if st["queued"]:
            L.append("QUEUED: " + ", ".join(st["queued"]))
        if st["blocked"]:
            L.append("BLOCKED:")
            for b in st["blocked"]:
                why = ", ".join(f"{n['job']} ({n['why']})" for n in b["needs"])
                flag = "" if b["reachable"] else "   << will never run"
                L.append(f"  {b['name']:<24} needs {why}{flag}")
        if st["foreign"]:
            L.append("OTHER CONTROLLERS:")
            L += [f"  {f['name']:<24} {f['owner']} ({f['state']})" for f in st["foreign"]]
        if st["landed"]:
            L.append("LANDED: " + ", ".join(st["landed"]))
        return "\n".join(L)

    def write_status(self):
        st = self.status()
        self.home.mkdir(parents=True, exist_ok=True)
        for name, text in (("status.json", json.dumps(st, indent=2)), ("status.txt", self.render(st))):
            tmp = self.home / f".{name}.tmp"
            tmp.write_text(text, encoding="utf-8")
            _replace(tmp, self.home / name)       # atomic: a reader never sees half a status
        return st


def _replace(src, dst, tries=40, wait=0.05):
    """os.replace, retried on a Windows sharing violation.

    WinError 32 -- "the process cannot access the file because it is being used by another
    process" -- is raised if ANY handle is open on the source when the rename runs. On this queue
    that is a routine, self-inflicted collision: the control thread reads every record in running/
    on each status tick while job threads are moving those same records out. The first version lost
    that race often enough that a finished job stayed stuck in running/ with its slot released. The
    journaled result meant the next startup settled it -- but a job should not need rescuing to be
    filed, and an operator's open editor should not be able to strand one either."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return True
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(wait)


def _last_line(text):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1][:120] if lines else ""


def main():
    ap = argparse.ArgumentParser(description="durable overnight supervisor over run/queue.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "plan", "status"):
        p = sub.add_parser(name)
        p.add_argument("--root", default=None, help="queue root (default runs/queue)")
        if name == "run":
            p.add_argument("--workers", default="cluster,cluster,pairA,pairB")
            p.add_argument("--max-rounds", type=int, default=5)
            p.add_argument("--max-seconds", type=int, default=0)
            p.add_argument("--redo-mode", choices=("conversation", "rewrite"), default=None)
            p.add_argument("--no-probe", action="store_true", help="skip the watchdog liveness check")
            p.add_argument("--on-cycle", choices=("refuse", "quarantine"), default="refuse")
    a = ap.parse_args()
    if a.cmd == "run":
        sup = Supervisor(root=a.root,
                         workers=[w.strip() for w in a.workers.split(",") if w.strip()],
                         execute=verified_executor(a.max_rounds, a.redo_mode),
                         probe=not a.no_probe, on_cycle=a.on_cycle)
        try:
            sup.run(max_seconds=a.max_seconds)
        except DependencyCycle as exc:
            print(f"REFUSED: {exc}")
            return 2
        print("\n" + sup.render())
    else:
        sup = Supervisor(root=a.root)
        if a.cmd == "plan":
            try:
                for i, n in enumerate(sup.plan(), 1):
                    print(f"  {i:>3}. {n}")
            except DependencyCycle as exc:
                print(f"REFUSED: {exc}")
                return 2
        else:
            print(sup.render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
