"""Reusable, evidence-backed lane readiness records (#25 / #28).

A lane's work-compliance results (run/workprobe.py) used to evaporate: every run either re-probed or
assumed. This module saves them as a versioned record OUTSIDE the repository -- beside the worker
registry, keyed by environment (HANDOFF_ENVIRONMENT, default "default") -- together with the
lane's IDENTITY: the parts of its configuration that the checks depend on. On the next run, and on
project two, an unchanged identity means the recorded checks still apply; a changed part is named,
and only that lane needs re-checking. A new project is not a change.

Readiness is not liveness: preflight still canaries every lane on every run. Readiness is not
permission either: it says what the lane was shown to do, nothing about what a project may do.

    python run/readiness.py show [worker ...]
    python run/readiness.py forget <worker>
"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "run")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: Bump when what a check proves changes; every record made by an older version is then stale.
PROBE_VERSION = 2
REQUIRED_CHECKS = ("format", "tool_call", "incremental_write")


def environment():
    return os.environ.get("HANDOFF_ENVIRONMENT") or "default"


def path():
    """The readiness file: HANDOFF_READINESS_FILE, else beside the worker registry (per user)."""
    env = os.environ.get("HANDOFF_READINESS_FILE")
    if env:
        return Path(env)
    import fleet
    return fleet.registry_path().parent / "readiness.json"


def identity(worker):
    """{part: value} of what the recorded checks depend on for `worker`. Never includes secrets."""
    import fleet
    import generation
    w = fleet.WORKERS.get(worker) or {}
    try:
        profile = generation.worker_request_profile(worker)
    except ValueError as e:
        profile = {"invalid": str(e)[:80]}
    try:
        import tooljob
        rt = tooljob.runtime_info()
        runtime = {"kind": rt.get("kind"), "path": rt.get("path"),
                   "capabilities": sorted(rt.get("capabilities") or [])}
    except Exception:
        runtime = None
    return {
        "endpoint": w.get("url"),
        "model": w.get("model"),
        "context": w.get("ctx"),
        "reasoning_style": w.get("reasoning_style"),
        "max_output": w.get("max_output"),
        "request_profile": profile,
        "output_pin": generation.pinned_output_limit(worker),
        "tool_runtime": runtime,
        "tool_service": (fleet.TOOL_SERVICE or {}).get("url"),
        "probe_version": PROBE_VERSION,
    }


def _hash(ident):
    return hashlib.sha256(json.dumps(ident, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def load():
    try:
        doc = json.loads(path().read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(doc):
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name("." + p.name + "." + str(os.getpid()) + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)                       # atomic: an interrupted write never leaves a half record


def record(worker, report):
    """Save a workprobe report for `worker` under the current identity. Returns the entry."""
    ident = identity(worker)
    checks = {k: {"ok": bool(v.get("ok")), "note": str(v.get("note") or "")[:300]}
              for k, v in (report.get("probes") or {}).items()}
    entry = {"identity": ident, "identity_hash": _hash(ident), "checks": checks,
             "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "workspace": report.get("workspace")}
    doc = load()
    doc.setdefault("format", 1)
    doc.setdefault("environments", {}).setdefault(environment(), {})[worker] = entry
    _save(doc)
    return entry


def status(worker, required=REQUIRED_CHECKS):
    """{"state": qualified|changed|failed|missing, "changed": [...], "failed": [...], "missing": [...],
    "recorded_at", "summary"} for `worker` in the current environment."""
    entry = ((load().get("environments") or {}).get(environment()) or {}).get(worker)
    if not entry:
        return {"state": "missing", "changed": [], "failed": [], "missing": list(required),
                "recorded_at": None, "summary": "no readiness record; run: python run/workprobe.py " + worker}
    now = identity(worker)
    old = entry.get("identity") or {}
    changed = sorted(k for k in set(now) | set(old) if now.get(k) != old.get(k))
    checks = entry.get("checks") or {}
    missing = [c for c in required if c not in checks]
    failed = [c for c in required if c in checks and not checks[c].get("ok")]
    if changed:
        state = "changed"
        summary = "recorded {0}, but {1} changed since; re-check: python run/workprobe.py {2}".format(
            entry.get("recorded_at"), ", ".join(changed), worker)
    elif missing or failed:
        state = "failed" if failed else "missing"
        summary = "recorded {0}: {1}".format(entry.get("recorded_at"), "; ".join(
            ["{0} failed ({1})".format(c, checks[c].get("note")) for c in failed] +
            ["{0} never checked".format(c) for c in missing]))
    else:
        state = "qualified"
        summary = "qualified {0} ({1}); configuration unchanged".format(entry.get("recorded_at"),
                                                                      ", ".join(required))
    return {"state": state, "changed": changed, "failed": failed, "missing": missing,
            "recorded_at": entry.get("recorded_at"), "summary": summary}


def forget(worker):
    doc = load()
    envs = doc.get("environments") or {}
    gone = envs.get(environment(), {}).pop(worker, None) is not None
    if gone:
        _save(doc)
    return gone


def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    cmd = args.pop(0) if args else "show"
    if cmd == "forget" and args:
        print("forgot {0}".format(args[0]) if forget(args[0]) else "{0} had no record".format(args[0]))
        return 0
    if cmd != "show":
        print(__doc__.strip().splitlines()[-2].strip())
        return 2
    import fleet
    names = args or sorted(((load().get("environments") or {}).get(environment()) or {}).keys()) or \
        sorted(fleet.WORKERS)
    print("readiness file: {0}  (environment {1})".format(path(), environment()))
    for n in names:
        s = status(n)
        print("  {0:<16} {1:<10} {2}".format(n, s["state"], s["summary"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
