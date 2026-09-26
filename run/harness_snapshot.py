"""Stable harness code for a live run (#9).

A live run executed the checkout's files as they were at each moment: a second session editing or
committing in the same checkout changed code under a running evaluation -- the controller's next
import and every worker subprocess picked up the edit. Now `autonomous` and `overnight run` copy the
harness runtime to a content-addressed snapshot (`<runs>/harness-snapshots/<fingerprint>/`) and
re-run themselves from it; every subprocess they start (verified.py, tool loop, skeptic) runs from
the same frozen files. Edits to the checkout take effect on the NEXT launch, never mid-run.

Not copied: .git, runs/, tests/, caches, and the settings file (it can hold secrets) -- the relaunch
pins FLEET_SETTINGS and FLEET_RUNS_DIR to the source checkout's values instead, and records the
source checkout as HANDOFF_SOURCE_ROOT so the bundled tool service's workspaces are still found.
HANDOFF_NO_SNAPSHOT=1 runs in place (for development only).
"""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXCLUDE_DIRS = {".git", "runs", "tests", "__pycache__", ".pytest_cache", "node_modules", "intake", ".venv", "venv"}
EXCLUDE_SUFFIXES = (".local.py", ".pyc", ".tmp", ".sqlite3")
MAX_FILE_BYTES = 5_000_000


def runtime_files(root=ROOT):
    """Sorted relative paths of the files a run can execute or read as code/config."""
    root = Path(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".tmp"))
        for f in sorted(filenames):
            p = Path(dirpath) / f
            if f.endswith(EXCLUDE_SUFFIXES) or p.is_symlink():
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            out.append(p.relative_to(root).as_posix())
    return out


def fingerprint(root=ROOT):
    """sha256 over every runtime file's path and bytes (not only run/*.py)."""
    h = hashlib.sha256()
    for rel in runtime_files(root):
        h.update(rel.encode("utf-8") + b"\0")
        h.update((Path(root) / rel).read_bytes() + b"\0")
    return h.hexdigest()


def snapshot(root=ROOT, base=None):
    """(fingerprint, path) of a frozen copy of the harness, created once per content version."""
    import fleet
    root = Path(root)
    base = Path(base) if base else fleet.runs_root() / "harness-snapshots"
    fp = fingerprint(root)
    dest = base / fp[:16]
    if (dest / "SNAPSHOT").is_file():
        return fp, dest
    tmp = base / (".tmp-" + fp[:16] + "-" + str(os.getpid()))
    if tmp.exists():
        shutil.rmtree(tmp)
    for rel in runtime_files(root):
        target = tmp / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, target)
    if fingerprint(tmp) != fp:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("the checkout changed while it was being snapshotted; launch again")
    (tmp / "SNAPSHOT").write_text("{0}\nsource: {1}\n".format(fp, root), encoding="utf-8")
    try:
        tmp.rename(dest)
    except OSError:                                   # another launcher made the same snapshot first
        shutil.rmtree(tmp, ignore_errors=True)
    return fp, dest


def relaunch_env(fp, source_root=ROOT, environ=None):
    import fleet
    env = dict(os.environ if environ is None else environ)
    env["HANDOFF_SNAPSHOT"] = fp
    env["HANDOFF_SOURCE_ROOT"] = str(Path(source_root))
    env.setdefault("FLEET_RUNS_DIR", str(fleet.runs_root()))
    if not env.get("FLEET_SETTINGS") and Path(fleet.LOCAL_SETTINGS).is_file():
        env["FLEET_SETTINGS"] = str(Path(fleet.LOCAL_SETTINGS).resolve())
    return env


def isolate(script, argv, *, run=None, echo=print):
    """Re-run `script` (relative to the harness root, e.g. 'run/conductor.py') from a snapshot.
    Returns the child's exit code, or None when already isolated / disabled (caller continues)."""
    if os.environ.get("HANDOFF_SNAPSHOT") or os.environ.get("HANDOFF_NO_SNAPSHOT"):
        return None
    fp, dest = snapshot()
    echo("harness snapshot {0} at {1} (edits to {2} apply from the next launch)".format(fp[:16], dest, ROOT))
    cmd = [sys.executable, str(dest / script)] + list(argv)
    r = (run or subprocess.run)(cmd, env=relaunch_env(fp))
    return getattr(r, "returncode", r)
