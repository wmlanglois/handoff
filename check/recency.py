"""Timestamp analysis across the many repos, so a stale mapping does not get trusted.

The failure this prevents: several repos (several sibling repos and a tool-service tree)
describe the same fleet, and an old doc from days ago gets read as current. Before trusting
a fact ("the miner endpoints", "the cluster address"), run this to see which source is
NEWEST. Trust the top row; be suspicious of anything old.

    python check/recency.py 8031
    python check/recency.py "miner" --days 14

Prints matching files newest-first by last-commit date (falling back to file mtime for
non-git trees), across the known roots.

The roots are the RECENCY_ROOTS setting (fleet_settings.local.py), not a hard-coded list: the
sibling repos that describe the same fleet differ per installation, and a baked-in list silently
scanned nothing on anyone else's machine -- the worst possible failure for a tool whose whole job
is to tell you what is current. A clean clone defaults to this repo alone.
"""
import argparse, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fleet import RECENCY_ROOTS  # noqa  -- single source for machine-specific paths

ROOTS = [Path(r) for r in RECENCY_ROOTS]
EXTS = {".py", ".md", ".json", ".yaml", ".yml", ".ps1", ".txt", ".sh"}
SKIP = {".git", "__pycache__", "node_modules", ".venv", "runs", "state"}

def last_commit(root, rel):
    try:
        out = subprocess.run(["git", "-C", str(root), "log", "-1",
                              "--format=%cd", "--date=format:%Y-%m-%d %H:%M", "--", rel],
                             capture_output=True, text=True, timeout=8).stdout.strip()
        return out or None
    except Exception:
        return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--days", type=int, default=0, help="only show sources changed within N days (0 = all)")
    ap.add_argument("--limit", type=int, default=20)
    a = ap.parse_args()
    q = a.query.lower()
    cutoff = time.time() - a.days * 86400 if a.days else 0
    hits = []
    for root in ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in EXTS:
                continue
            if any(part in SKIP for part in p.relative_to(root).parts):
                continue
            try:
                if q not in p.name.lower() and q not in p.read_text(encoding="utf-8", errors="ignore").lower():
                    continue
            except Exception:
                continue
            mtime = p.stat().st_mtime
            if cutoff and mtime < cutoff:
                continue
            rel = str(p.relative_to(root)).replace("\\", "/")
            hits.append({"repo": root.name, "rel": rel, "mtime": mtime,
                         "commit": last_commit(root, rel)})
    # Sort by commit date when present, else mtime. Newest first.
    hits.sort(key=lambda h: (h["commit"] or time.strftime("%Y-%m-%d %H:%M", time.localtime(h["mtime"]))), reverse=True)
    print(f"'{a.query}': {len(hits)} sources, NEWEST FIRST (trust the top, distrust the old)\n")
    for h in hits[:a.limit]:
        stamp = h["commit"] or ("mtime " + time.strftime("%Y-%m-%d %H:%M", time.localtime(h["mtime"])))
        print(f"  {stamp:<18} {h['repo']}/{h['rel']}")
    if not hits:
        missing = [str(r) for r in ROOTS if not r.exists()]
        print("  (no matches — widen the query or check the roots)")
        print(f"  roots scanned: {', '.join(str(r) for r in ROOTS) or '(none)'}")
        if missing:
            # A root that does not exist is scanned as zero files, which reads exactly like "this
            # fact appears nowhere" -- say so instead of implying the fleet has no such source.
            print(f"  NOT FOUND (set RECENCY_ROOTS in fleet_settings.local.py): {', '.join(missing)}")

if __name__ == "__main__":
    main()
