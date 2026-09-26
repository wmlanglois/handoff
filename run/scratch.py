"""Tool-service scratch workspaces: inventory, quota and reference-aware pruning (#30).

Every tool job writes into its own workspace under the tool service root
(`<service root>/jobs/<job id>/`), and nothing ever removed them. This command reports what is
there and which workspaces are still needed, and prunes only the rest.

A workspace is KEPT when any of these holds:
  * it belongs to an assignment that is still ready, running, parked or blocked (work may resume);
  * it belongs to an ACCEPTED assignment (its receipts are provenance for what was integrated);
  * it was modified within the retention window (default 7 days);
  * its job id is not a recognisable tool workspace name (never guess about unknown data).
Everything else -- probe workspaces (workprobe-*, preflight-probe) and workspaces of finished,
not-accepted assignments -- is a prune candidate once it is older than the retention window.

Pruning is a DRY RUN unless --apply is given, and it deletes only candidate directories under the
service's jobs/ folder.

    python run/scratch.py [--root <service root>] [--retention-days 7] [--quota-mb 2048] [--apply]
"""
import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "run")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

KEEP_STATUSES = ("ready", "running", "parked", "blocked")
PROBE_PREFIXES = ("workprobe-", "preflight-probe")
DEFAULT_RETENTION_DAYS = 7


def service_jobs_dir():
    """The configured tool service's jobs/ folder (bundled: <checkout>/runs/tool-service/jobs)."""
    import tooljob
    return tooljob.workspace_dir("x").parent


def _ws_id(run_id):
    return re.sub(r"[^A-Za-z0-9_-]", "-", "verified-" + str(run_id))


def references(goals_dir=None):
    """{workspace id: (goal id, assignment, status, disposition)} for every goal assignment."""
    import fleet
    gdir = Path(goals_dir) if goals_dir else fleet.goals_dir()
    refs = {}
    for f in sorted(gdir.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        gid = doc.get("goal_id") or f.stem
        for name, a in (doc.get("assignments") or {}).items():
            if not isinstance(a, dict):
                continue
            rid = a.get("run_id") or name
            refs[_ws_id(rid)] = (gid, name, a.get("status"), a.get("disposition"))
    return refs


def _size(p):
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def _mtime(p):
    latest = p.stat().st_mtime
    for f in p.rglob("*"):
        try:
            latest = max(latest, f.stat().st_mtime)
        except OSError:
            pass
    return latest


def inventory(jobs_dir, refs, *, retention_days=DEFAULT_RETENTION_DAYS, now=None):
    """One row per workspace: id, bytes, age_days, verdict (keep/prune) and why."""
    now = time.time() if now is None else now
    rows = []
    if not Path(jobs_dir).is_dir():
        return rows
    for ws in sorted(p for p in Path(jobs_dir).iterdir() if p.is_dir() and not p.is_symlink()):
        age = (now - _mtime(ws)) / 86400.0
        ref = refs.get(ws.name)
        if ref is None:
            # Derived workspaces (e.g. `<job>-skillverify`) share their parent job's references.
            parents = [k for k in refs if ws.name.startswith(k + "-")]
            ref = refs[max(parents, key=len)] if parents else None
        if ref and (ref[2] in KEEP_STATUSES):
            verdict, why = "keep", "assignment {0} is {1} (goal {2})".format(ref[1], ref[2], ref[0])
        elif ref and ref[3] == "accepted":
            verdict, why = "keep", "accepted assignment {0}: receipts are provenance (goal {1})".format(ref[1], ref[0])
        elif age < retention_days:
            verdict, why = "keep", "modified {0:.1f} days ago (retention {1})".format(age, retention_days)
        elif ref:
            verdict, why = "prune", "assignment {0} finished as {1} (goal {2})".format(ref[1], ref[3], ref[0])
        elif ws.name.startswith(PROBE_PREFIXES):
            verdict, why = "prune", "probe workspace"
        elif ws.name.startswith("verified-"):
            verdict, why = "prune", "no goal references this tool workspace"
        else:
            verdict, why = "keep", "unrecognised workspace name; not guessing"
        rows.append({"id": ws.name, "path": str(ws), "bytes": _size(ws), "age_days": round(age, 1),
                     "verdict": verdict, "why": why})
    return rows


def prune(jobs_dir, rows):
    """Delete the candidate directories, only under jobs_dir. Returns the ids removed."""
    base = Path(jobs_dir).resolve()
    gone = []
    for r in rows:
        if r["verdict"] != "prune":
            continue
        p = Path(r["path"]).resolve()
        if p.parent != base:
            continue
        shutil.rmtree(p)
        gone.append(r["id"])
    return gone


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", help="tool service jobs folder (default: the configured service's jobs/)")
    ap.add_argument("--retention-days", type=float, default=DEFAULT_RETENTION_DAYS)
    ap.add_argument("--quota-mb", type=float, default=None, help="warn when scratch exceeds this size")
    ap.add_argument("--apply", action="store_true", help="actually delete the prune candidates")
    a = ap.parse_args(argv)
    jobs = Path(a.root) if a.root else service_jobs_dir()
    rows = inventory(jobs, references(), retention_days=a.retention_days)
    total = sum(r["bytes"] for r in rows)
    cand = [r for r in rows if r["verdict"] == "prune"]
    print("scratch: {0}  ({1} workspaces, {2:.1f} MB)".format(jobs, len(rows), total / 1e6))
    for r in rows:
        print("  {0:<5} {1:<60} {2:>9.1f} KB {3:>6} d  {4}".format(
            r["verdict"], r["id"][:60], r["bytes"] / 1e3, r["age_days"], r["why"]))
    freed = sum(r["bytes"] for r in cand) / 1e6
    if a.quota_mb is not None and total / 1e6 > a.quota_mb:
        print("  OVER QUOTA: {0:.1f} MB > {1} MB; pruning candidates would free {2:.1f} MB".format(
            total / 1e6, a.quota_mb, freed))
    if not a.apply:
        print("dry run: {0} candidate(s), {1:.1f} MB; re-run with --apply to delete them".format(len(cand), freed))
        return 0
    gone = prune(jobs, rows)
    print("deleted {0} workspace(s), {1:.1f} MB".format(len(gone), freed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
