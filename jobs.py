"""Persistent job ledger in SQLite, so a hung job is one quick query away, not a rerun.

Every real dispatch through call.chat logs OPEN (worker, prompt, started); on return it
logs CLOSE (ended, status). An OPEN row whose `started` is older than the threshold, with
no close, is HUNG. This is the "I prompted cluster at 2:00, saw an open row at 2:15 with no
close, so it's hung" check, answerable instantly without pulling logs or rerunning.

    python jobs.py            # open jobs; flags any open longer than --stale as HUNG
    python jobs.py --all      # also the last 10 closed jobs with durations
    python jobs.py --stale 300
"""
import argparse, contextlib, sqlite3, time, uuid
from pathlib import Path

#: Explicit override (tests); None means resolve fleet.jobs_db() on every connection, so every checkout
#: pointed at the same run state shares ONE ledger -- and therefore one max_inflight and one prefill lock.
DB = None


def db_path():
    if DB is not None:
        return Path(DB)
    import fleet
    return fleet.jobs_db()


def _conn():
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path), timeout=15)
    c.execute("CREATE TABLE IF NOT EXISTS jobs "
              "(id TEXT PRIMARY KEY, worker TEXT, prompt TEXT, started REAL, ended REAL, status TEXT, note TEXT)")
    return c

def open_job(worker, prompt):
    jid = uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                  (jid, worker, (prompt or "")[:200], time.time(), None, "open", None))
    return jid

def _locks_table(c):
    c.execute("CREATE TABLE IF NOT EXISTS locks (name TEXT PRIMARY KEY, pid INTEGER, ts REAL)")

def acquire_lock(name, timeout=45, stale_s=60, poll=0.2):
    """Cross-process mutex via the ledger. Used as the cluster PREFILL lock: a request holds it only
    across its prompt-prefill stage so two prefills never hit insert_segments at once, then releases so a
    second request can prefill while the first decodes. Steals a lock older than stale_s (dead holder).
    Returns True on acquire, False on timeout. A False is NOT permission to proceed: the prefill
    lock exists because two concurrent prefills crash the cluster, so the caller must treat a failed
    acquire as a refusal, not a formality. See call.py's PrefillLockTimeout."""
    import os
    t0 = time.time()
    while True:
        try:
            c = _conn()
            try:
                _locks_table(c)
                c.execute("BEGIN IMMEDIATE")
                row = c.execute("SELECT pid, ts FROM locks WHERE name=?", (name,)).fetchone()
                now = time.time()
                if row is None or (now - row[1]) > stale_s:
                    c.execute("INSERT OR REPLACE INTO locks VALUES (?,?,?)", (name, os.getpid(), now))
                    c.commit(); return True
                c.rollback()
            finally:
                c.close()
        except sqlite3.OperationalError:
            pass
        if time.time() - t0 > timeout:
            return False
        time.sleep(poll)

def renew_lock(name):
    """Move a held lock's timestamp to now.

    Used when a caller times out before the first token. The server may still be prefilling, so the
    lock must stay, but its age must be measured from the abandon. Otherwise a later caller whose
    stale bound is longer than the original request would steal it immediately and start a second
    prefill on top of the one still running."""
    try:
        c = _conn()
        try:
            _locks_table(c)
            c.execute("UPDATE locks SET ts=? WHERE name=?", (time.time(), name))
            c.commit()
        finally:
            c.close()
    except sqlite3.OperationalError:
        pass


def release_lock(name):
    try:
        c = _conn()
        try:
            _locks_table(c); c.execute("DELETE FROM locks WHERE name=?", (name,)); c.commit()
        finally:
            c.close()
    except sqlite3.OperationalError:
        pass

def inflight(worker, stale_s=600):
    """Count open, non-stale jobs for a worker. A stale/hung open row (a crashed process that
    never closed) does NOT hold a slot, so it is excluded -- otherwise one crash would wedge the cap."""
    now = time.time()
    with _conn() as c:
        rows = c.execute("SELECT started FROM jobs WHERE status='open' AND worker=?", (worker,)).fetchall()
    return sum(1 for (started,) in rows if now - started <= stale_s)

class CapacityTimeout(RuntimeError):
    """Raised when a worker stayed at capacity for the whole wait. The caller decides: park, retry
    elsewhere, or surface. Never silently admitted."""


def open_job_capped(worker, prompt, cap, stale_s=600, timeout=300, poll=0.5):
    """Open a job only when fewer than `cap` non-stale jobs are already open for `worker`, so we
    never saturate a backend's batch (the mlx cluster crashes on concurrent insert_segments).
    Atomic check-and-insert under a write lock, so separate processes (fleetbatch subprocesses)
    can't overshoot the cap.

    FAIL-CLOSED. An earlier version opened the job anyway after `timeout` "rather than deadlock the
    fleet". That defeated the only control standing between the scheduler and the concurrent
    insert_segments crash this cap exists to prevent: under sustained load every waiter eventually
    times out and piles onto a worker that is already full. Refusing is recoverable -- the caller
    parks or retries the job; overloading the cluster is not. Raises CapacityTimeout."""
    if not cap or cap <= 0:
        return open_job(worker, prompt)
    t0 = time.time()
    while True:
        try:
            c = _conn()
            try:
                c.execute("BEGIN IMMEDIATE")
                now = time.time()
                rows = c.execute("SELECT started FROM jobs WHERE status='open' AND worker=?", (worker,)).fetchall()
                n = sum(1 for (s,) in rows if now - s <= stale_s)
                if n < cap:
                    jid = uuid.uuid4().hex[:12]
                    c.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?)",
                              (jid, worker, (prompt or "")[:200], now, None, "open", None))
                    c.commit(); return jid
                c.rollback()
            finally:
                c.close()
        except sqlite3.OperationalError:
            pass  # lock contention; back off and retry
        if time.time() - t0 > timeout:
            raise CapacityTimeout(
                f"{worker}: {cap} slot(s) still busy after {timeout}s; refusing to exceed the cap")
        time.sleep(poll)

def close_job(jid, status="done", note=None):
    with _conn() as c:
        c.execute("UPDATE jobs SET ended=?, status=?, note=? WHERE id=?",
                  (time.time(), status, (note or "")[:300] if note else None, jid))

@contextlib.contextmanager
def dispatch(worker, prompt):
    jid = open_job(worker, prompt)
    try:
        yield jid
        close_job(jid, "done")
    except BaseException as e:
        close_job(jid, "error", repr(e))
        raise

def hung(stale_s):
    now = time.time()
    with _conn() as c:
        rows = c.execute("SELECT id,worker,prompt,started FROM jobs WHERE status='open'").fetchall()
    return [{"id": r[0], "worker": r[1], "prompt": r[2], "age_s": round(now - r[3])}
            for r in rows if now - r[3] > stale_s]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stale", type=int, default=600, help="an open job older than this is HUNG")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    now = time.time()
    with _conn() as c:
        opens = c.execute("SELECT id,worker,prompt,started FROM jobs WHERE status='open' ORDER BY started").fetchall()
    print(f"OPEN jobs ({len(opens)}), --stale {a.stale}s:")
    for jid, worker, prompt, started in opens:
        age = round(now - started)
        print(f"  {jid}  {worker:<8} age {age:>6}s  {(prompt or '')[:48]}" + ("   <-- HUNG" if age > a.stale else ""))
    if not opens:
        print("  (none open)")
    if a.all:
        with _conn() as c:
            done = c.execute("SELECT worker,status,round(ended-started,1),prompt FROM jobs "
                             "WHERE ended IS NOT NULL ORDER BY ended DESC LIMIT 10").fetchall()
        print("\nrecent closed:")
        for w, s, d, p in done:
            print(f"  {w:<8} {s:<6} {d}s  {(p or '')[:48]}")

if __name__ == "__main__":
    main()
