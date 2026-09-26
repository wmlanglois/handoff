"""Overnight: delegated continuation across milestones within one cumulative budget (#7).

One goal is one milestone; it ends at its promoted checkpoint and nothing carried the work on. This
is a thin controller over the ordinary per-milestone path (`conductor.py autonomous` with a standing
delegate) -- not a second scheduler or ledger. It adds only what spans milestones:

  * an AUTHORIZED backlog: an ordered list of milestone packages with dependencies, bound by its
    sha256 to one approver and one cumulative decision/time budget. A backlog edited after
    authorization is refused until re-authorized;
  * continuation: the next milestone whose dependencies are done runs next; it starts from a COPY of
    its dependency's promoted checkpoint (never the live tree) when it has not started yet;
  * one budget: every milestone's goal is launched with only what the project has left, and spend
    is summed from the goals' own durable records, so a restart neither resets nor double-counts;
  * stop classification: a DONE milestone continues the backlog; a crash is retried (bounded); an
    approval/intake/preflight/blocked/review stop is a human boundary -- that milestone and its
    dependents wait, independent milestones keep going; BUDGET ends the night;
  * one consolidated wake report instead of per-gate surfacing.

State lives beside the backlog (`<backlog>.state.json`, atomic writes) so a restarted controller
resumes where it stopped.

    python run/overnight.py authorize <backlog.json> --as <you> --decisions N --seconds S
    python run/overnight.py run <backlog.json> --workers a,b --tool-mode tools|chat-only
    python run/overnight.py status <backlog.json>

backlog.json: {"milestones": [{"id": "m1", "package": "path/to/pkg1"},
                              {"id": "m2", "package": "path/to/pkg2", "after": ["m1"]}]}
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "run")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

MAX_CRASH_RETRIES = 2
DONE, BLOCKED, BUDGET = "DONE", "BLOCKED", "BUDGET"
HUMAN_STOPS = ("AWAITING_PLAN_APPROVAL", "AWAITING_MAP_APPROVAL", "AWAITING_MAP", "AWAITING_INTAKE",
               "MAP_DERIVATION_FAILED", "PREFLIGHT_FAILED", "BLOCKED", "REVIEW")


class BacklogError(RuntimeError):
    pass


def _state_path(backlog):
    return Path(str(backlog) + ".state.json")


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_backlog(backlog):
    doc = json.loads(Path(backlog).read_text(encoding="utf-8"))
    ms = doc.get("milestones")
    if not isinstance(ms, list) or not ms:
        raise BacklogError("backlog has no milestones")
    ids = [m.get("id") for m in ms]
    if len(set(ids)) != len(ids) or not all(ids):
        raise BacklogError("milestone ids must be present and unique")
    for m in ms:
        if not m.get("package"):
            raise BacklogError("milestone {0} names no package".format(m["id"]))
        for dep in m.get("after") or ():
            if dep not in ids:
                raise BacklogError("milestone {0} is after unknown milestone {1}".format(m["id"], dep))
    return ms


def load_state(backlog):
    try:
        return json.loads(_state_path(backlog).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(backlog, st):
    p = _state_path(backlog)
    tmp = p.with_name("." + p.name + "." + str(os.getpid()) + ".tmp")
    tmp.write_text(json.dumps(st, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def authorize(backlog, by, *, decisions, seconds):
    load_backlog(backlog)
    if not (by or "").strip():
        raise BacklogError("authorization needs --as <you>")
    st = load_state(backlog)
    st["authorized"] = {"by": by, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "sha256": _sha(backlog)}
    st["budget"] = {"decisions": int(decisions), "seconds": int(seconds)}
    st.setdefault("milestones", {})
    save_state(backlog, st)
    return st


def _goal_of(package):
    import projectpkg
    try:
        return projectpkg.load_project(Path(package)).get("goal_id")
    except Exception:
        return None


def spent(backlog, st):
    """Cumulative spend across every milestone goal, from the goals' own durable records."""
    import goals
    total = {"decisions": 0, "seconds": 0}
    for rec in (st.get("milestones") or {}).values():
        gid = rec.get("goal_id")
        if not gid:
            continue
        try:
            sp = goals.run_spent(gid) or {}
        except Exception:
            continue
        total["decisions"] += int(sp.get("decisions") or 0)
        total["seconds"] += int(float(sp.get("seconds") or 0))
    return total


def remaining(backlog, st):
    b, s = st.get("budget") or {}, spent(backlog, st)
    return {"decisions": int(b.get("decisions", 0)) - s["decisions"],
            "seconds": int(b.get("seconds", 0)) - s["seconds"]}


def seed_baseline(package, source_checkout, *, milestone):
    """Point a not-yet-started milestone package at a COPY of its dependency's promoted checkpoint."""
    import projectpkg
    package = Path(package)
    doc = projectpkg.load_project(package)
    if doc.get("goal_id"):
        return None                                  # already started: never move its baseline
    dest = package / "baseline-from-dependency"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source_checkout, dest)
    doc["project_root"] = str(dest)
    doc["baseline_from"] = {"milestone": milestone, "checkpoint": str(source_checkout)}
    projectpkg.save_project(package, doc)
    return dest


def default_runner(package, *, decisions, seconds, workers, delegate, tool_mode):
    import conductor
    argv = ["autonomous", str(package), "--decisions", str(decisions), "--seconds", str(seconds),
            "--workers", workers, "--delegate", delegate]
    if tool_mode:
        argv += ["--tool-mode", tool_mode]
    return conductor.main(argv)


def classify(summary):
    """(status, reason) for one milestone run's stop."""
    stop = (summary or {}).get("stop") if isinstance(summary, dict) else None
    reason = (summary or {}).get("reason") if isinstance(summary, dict) else None
    if stop == DONE:
        return "done", reason or "milestone journey passed"
    if stop == BUDGET:
        return "budget", reason or "budget spent"
    if stop in HUMAN_STOPS:
        return "blocked", "{0}: {1}".format(stop, reason or "needs a human")
    return "blocked", "unexpected stop {0!r}: {1}".format(stop, reason)


def run(backlog, *, workers, tool_mode=None, runner=None, checkpoint_of=None, echo=print, max_steps=50):
    """Advance the backlog until done, budget, or every remaining milestone waits on a human."""
    ms = load_backlog(backlog)
    st = load_state(backlog)
    auth = st.get("authorized") or {}
    if not auth:
        raise BacklogError("backlog not authorized: run `overnight.py authorize` first")
    if auth.get("sha256") != _sha(backlog):
        raise BacklogError("backlog changed since it was authorized by {0}; re-authorize it".format(auth.get("by")))
    runner = runner or default_runner
    if checkpoint_of is None:
        import integrate
        checkpoint_of = integrate.live_dir
    recs = st.setdefault("milestones", {})
    for m in ms:
        recs.setdefault(m["id"], {"status": "pending"})
    save_state(backlog, st)
    base = Path(backlog).parent
    for _step in range(max_steps):
        rem = remaining(backlog, st)
        if rem["decisions"] <= 0 or rem["seconds"] <= 0:
            st["stop"] = {"stop": BUDGET, "reason": "cumulative budget spent", "remaining": rem}
            break
        eligible = [m for m in ms if recs[m["id"]]["status"] in ("pending", "running", "retry")
                    and all(recs[d]["status"] == "done" for d in m.get("after") or ())]
        if not eligible:
            if all(r["status"] == "done" for r in recs.values()):
                st["stop"] = {"stop": DONE, "reason": "every milestone done"}
            else:
                st["stop"] = {"stop": BLOCKED, "reason": "remaining milestones wait on a human or a blocked dependency"}
            break
        m = eligible[0]
        rec = recs[m["id"]]
        pkg = (base / m["package"]).resolve() if not Path(m["package"]).is_absolute() else Path(m["package"])
        deps = m.get("after") or ()
        if deps and rec["status"] == "pending":
            src_goal = recs[deps[-1]].get("goal_id")
            src = checkpoint_of(src_goal) if src_goal else None
            if src and Path(src).is_dir():
                seeded = seed_baseline(pkg, src, milestone=deps[-1])
                if seeded:
                    echo("{0}: baseline seeded from {1}'s promoted checkpoint".format(m["id"], deps[-1]))
        rec["status"] = "running"
        rec["started"] = rec.get("started") or time.strftime("%Y-%m-%dT%H:%M:%S")
        save_state(backlog, st)
        echo("== milestone {0} ({1} decisions, {2}s left in the project budget)".format(
            m["id"], rem["decisions"], rem["seconds"]))
        try:
            summary = runner(pkg, decisions=rem["decisions"], seconds=rem["seconds"], workers=workers,
                             delegate=auth["by"], tool_mode=tool_mode)
        except SystemExit as e:
            summary = {"stop": "BLOCKED", "reason": "exited {0}".format(e.code)}
        except Exception as e:
            rec["crashes"] = int(rec.get("crashes", 0)) + 1
            rec["goal_id"] = rec.get("goal_id") or _goal_of(pkg)
            if rec["crashes"] > MAX_CRASH_RETRIES:
                rec.update(status="blocked", reason="crashed {0} times: {1}".format(rec["crashes"], repr(e)[:200]))
            else:
                rec.update(status="retry", reason="crash (will retry): {0}".format(repr(e)[:200]))
            save_state(backlog, st)
            continue
        rec["goal_id"] = rec.get("goal_id") or _goal_of(pkg) or (summary or {}).get("goal_id")
        status, reason = classify(summary)
        rec.setdefault("stops", []).append({"stop": (summary or {}).get("stop"), "reason": reason,
                                            "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        if status == "done":
            rec.update(status="done", reason=reason, checkpoint=str(checkpoint_of(rec["goal_id"])) if rec.get("goal_id") else None)
        elif status == "budget":
            rec.update(status="running", reason=reason)
            save_state(backlog, st)
            st["stop"] = {"stop": BUDGET, "reason": "milestone {0}: {1}".format(m["id"], reason),
                          "remaining": remaining(backlog, st)}
            break
        else:
            rec.update(status="blocked", reason=reason)
        save_state(backlog, st)
    else:
        st["stop"] = {"stop": BLOCKED, "reason": "step limit reached"}
    save_state(backlog, st)
    report = wake_report(backlog, ms, st)
    Path(str(backlog) + ".report.md").write_text(report, encoding="utf-8")
    echo(report)
    return st["stop"]


def wake_report(backlog, ms, st):
    rem = remaining(backlog, st)
    lines = ["# Overnight report: {0}".format(Path(backlog).name), "",
             "Stopped: {0} -- {1}".format(st.get("stop", {}).get("stop"), st.get("stop", {}).get("reason")),
             "Budget left: {0} decisions, {1}s (authorized by {2})".format(
                 rem["decisions"], rem["seconds"], (st.get("authorized") or {}).get("by")), "",
             "| milestone | status | goal | reason |", "|---|---|---|---|"]
    for m in ms:
        r = (st.get("milestones") or {}).get(m["id"], {})
        lines.append("| {0} | {1} | {2} | {3} |".format(m["id"], r.get("status"), r.get("goal_id") or "-",
                                                         (r.get("reason") or "").replace("|", "/")[:160]))
    waiting = [m["id"] for m in ms if (st.get("milestones") or {}).get(m["id"], {}).get("status") == "blocked"]
    if waiting:
        lines += ["", "Needs you: " + ", ".join(waiting) + " (see each reason; dependents wait on them)."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("authorize")
    a.add_argument("backlog")
    a.add_argument("--as", dest="by", required=True)
    a.add_argument("--decisions", type=int, required=True)
    a.add_argument("--seconds", type=int, required=True)
    r = sub.add_parser("run")
    r.add_argument("backlog")
    r.add_argument("--workers", required=True)
    r.add_argument("--tool-mode", choices=("tools", "chat-only"))
    s = sub.add_parser("status")
    s.add_argument("backlog")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "authorize":
            st = authorize(args.backlog, args.by, decisions=args.decisions, seconds=args.seconds)
            print("authorized by {0}: {1} decisions, {2}s cumulative".format(
                args.by, st["budget"]["decisions"], st["budget"]["seconds"]))
            return 0
        if args.cmd == "status":
            print(wake_report(args.backlog, load_backlog(args.backlog), load_state(args.backlog)))
            return 0
        stop = run(args.backlog, workers=args.workers, tool_mode=args.tool_mode)
        return 0 if stop.get("stop") == DONE else 2
    except BacklogError as e:
        print("refused: {0}".format(e))
        return 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"]:
        import harness_snapshot            # #9: run the night from a frozen harness snapshot
        _rc = harness_snapshot.isolate("run/overnight.py", sys.argv[1:])
        if _rc is not None:
            sys.exit(_rc)
    sys.exit(main())
