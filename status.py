"""Fleet watchdog for the wake-up cron. A hung job is reported and not piled onto.
An approved goal with work still ready is resumed as that same goal, not replaced.

    python status.py
    python status.py --resume
"""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "run"))
import jobs  # noqa

def snapshot(stale_s=600):
    now = time.time()
    with jobs._conn() as c:
        opens = c.execute("SELECT id,worker,prompt,started FROM jobs WHERE status='open' ORDER BY started").fetchall()
        recent = c.execute("SELECT worker,status,round(ended-started,1),prompt FROM jobs "
                           "WHERE ended IS NOT NULL ORDER BY ended DESC LIMIT 8").fetchall()
    hung = [o for o in opens if now - o[3] > stale_s]
    return opens, hung, recent


def resumable_goal_ids():
    """Approved, unfinished goals that still have ready work. Never a new goal."""
    import goals
    found = []
    for gid in goals.list_goals():
        try:
            st = goals.state(gid)
        except Exception:
            continue
        if not st.get("approved") or goals.complete(gid):
            continue
        if goals.next_work(gid, 1):
            found.append(gid)
    return found


def report(opens, hung, recent, resumable):
    """Lines to print, and the goal id to resume, if any. Hung work blocks a resume."""
    now = time.time()
    lines = ["FLEET STATUS  {0}".format(time.strftime("%Y-%m-%d %H:%M")),
             "  in flight: {0}   hung (>{1}s): {2}".format(len(opens), 600, len(hung))]
    for jid, worker, prompt, started in hung:
        lines.append("    HUNG {0} age {1}s  {2}".format(worker, round(now - started), (prompt or "")[:48]))
    fails = [r for r in recent if r[1] != "done"]
    lines.append("  recent: {0} closed, {1} failed".format(len(recent), len(fails)))
    for w, s, d, p in fails:
        lines.append("    FAIL {0} {1}  {2}".format(w, s, (p or "")[:48]))
    if hung:
        lines.append("  -> WAKE: hung jobs present; kill/investigate before starting new work")
        return lines, None
    if resumable:
        gid = resumable[0]
        lines.append("  -> RESUME {0}: python run/orchestrate.py {1}".format(gid, gid))
        lines.append("  -> same goal; a failure here does not open a new goal")
        return lines, gid
    if fails:
        lines.append("  -> WAKE: recent failures stay on the current goal; do not start a new goal")
        return lines, None
    if opens:
        lines.append("  -> QUIET: {0} in flight, nothing to start".format(len(opens)))
        return lines, None
    lines.append("  -> IDLE: nothing in flight and no approved goal is waiting")
    return lines, None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    resume = "--resume" in argv
    opens, hung, recent = snapshot()
    try:
        waiting = resumable_goal_ids()
    except Exception:
        waiting = []
    lines, gid = report(opens, hung, recent, waiting)
    print("\n".join(lines))
    if resume and gid:
        import orchestrate
        print("resuming {0} through orchestrate".format(gid))
        summary = orchestrate.run_goal(gid, ["cluster"])
        print("STOP: {0} -- {1}".format(summary.get("stop"), summary.get("reason")))
        return 0 if summary.get("stop") == "DONE" else 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
