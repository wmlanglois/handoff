"""Real liveness for the fleet: an END-TO-END generation canary, not /health.

The Mac cluster taught us /health lies -- it returns 200 while a dead tensor-parallel peer
leaves the survivor hung at 0% CPU, so every real request times out. This watchdog instead
(1) checks the TCP port is open, then (2) asks the worker to actually GENERATE a token under a
short timeout. It reports UP, WEDGED (port open but generation timed out -- the silent-hang
case), or DOWN (unreachable), and writes runs/watchdog.json so callers can fail fast on a bad
worker instead of blocking the full request timeout.

    python check/watchdog.py                 # one pass, all workers, table + JSON
    python check/watchdog.py --workers cluster
    python check/watchdog.py --loop 60       # re-check every 60s (a standing watchdog)
"""
import argparse, json, shlex, socket, subprocess, sys, time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from safeio import force_utf8  # noqa
force_utf8()
from fleet import WORKERS, ssh_cmd, MAC48, MAC_LOCALAI  # noqa
from call import chat       # noqa

STATUS = ROOT / "runs" / "watchdog.json"

def port_open(url, timeout=3):
    u = urlparse(url)
    try:
        with socket.create_connection((u.hostname, u.port or 80), timeout=timeout):
            return True
    except Exception:
        return False

def probe(worker, gen_timeout=30):
    """Return (status, ms, detail). status in UP / WEDGED / DOWN."""
    w = WORKERS[worker]
    if not port_open(w["url"]):
        return "DOWN", 0, "port not open (server not listening)"
    t = time.time()
    try:
        # track=False: a health canary must not consume an in-flight cap slot.
        # max_tokens was 2. On a reasoning model that spends its budget in a separate field before
        # emitting any content, a HEALTHY worker returns empty content and was reported WEDGED --
        # which makes usable() False, denies it capacity, and removes it from capability selection.
        # A liveness canary must not fail a worker for thinking. Budget raised, and any generated
        # token counts as alive: content, or reasoning, or a tool call.
        m, _ = chat(worker, [{"role": "user", "content": "Reply with one word: ok"}],
                    max_tokens=48, temperature=0, timeout=gen_timeout, track=False)
        ms = round((time.time() - t) * 1000)
        txt = (m.get("content") or "").strip()
        alive = txt or (m.get("reasoning") or "").strip() or m.get("tool_calls")
        return ("UP", ms, (txt or "generated (reasoning only)")[:20]) if alive else                ("WEDGED", ms, "no tokens generated at all")
    except socket.timeout:
        return "WEDGED", round((time.time() - t) * 1000), f"port open but no token in {gen_timeout}s (hung peer?)"
    except Exception as e:
        msg = str(e)
        # urllib wraps a read timeout; treat a timeout as WEDGED (listening, not generating).
        if "timed out" in msg.lower():
            return "WEDGED", round((time.time() - t) * 1000), f"generation timed out in {gen_timeout}s"
        return "DOWN", round((time.time() - t) * 1000), f"{type(e).__name__}: {msg[:80]}"

# The mlx-lm-mtp patches that actually prevent the concurrent-batch crash + spin. If a rebuild silently
# drops them, throughput and correctness regress invisibly -- so assert they're in the running tree.
_SSH = ssh_cmd(MAC48)   # single source: fleet.py
EXPECTED_PATCHES = [
    "_kv_alive through BatchKVCache.extend",       # second-agent join crash
    "MTP steps on the BatchGenerator stream",       # GPU wedge/100%-spin under 2 slots
    "force_fused",                                   # head_dim-256 prefill memory transient
]

# Commit subjects are NOT sufficient. Two ways that check lies:
#   1. A patch living only as an uncommitted working-tree edit has no commit to match. The rank>0
#      idle-sleep fix was exactly this -- present and working on mac48, invisible to git log.
#   2. A rebase onto upstream can squash or reword subjects, so the text vanishes while the fix stays.
# So also assert the patched SOURCE is in the running tree. Each entry is (file, marker).
# The interpreter the cluster actually runs (see the mlx.launch cmdline on mac48).
# From settings, not hardcoded: this path is one operator's home directory, so a clone that
# inherited it would silently probe a venv that does not exist and report every patch missing.
# Resolved LAZILY: concatenating an unset setting at import time made a clean clone fail to
# even collect its tests, which turns a Mac-only requirement into a universal one.
def venv_py():
    return str(MAC_LOCALAI) + "/venv-mtp/bin/python"
EXPECTED_SOURCE = [   # (label, importable module, source marker)
    ("mlx_lm/server.py", "mlx_lm.server", "Match rank 0's idle wait"),      # rank>0 idle spin (uncommitted!)
    ("mlx/launch.py", "mlx._distributed_utils.launch", "stdin pipe is always writable"),
]

def patches_ok(worker="cluster"):
    """Assert the crash-preventing MLX patches are present in the running mlx-lm-mtp tree on mac48.
    Returns (ok, detail, missing). Only meaningful for the mlx cluster."""
    if WORKERS.get(worker, {}).get("kind") != "mlx":
        return True, "n/a (not an mlx worker)", []
    try:
        r = subprocess.run(_SSH + ["cd ~/Documents/LocalAI/mlx-lm-mtp && git log --oneline -25"],
                           capture_output=True, text=True, timeout=20)
        log_text = r.stdout or ""
    except Exception as e:
        return False, f"ssh/git check failed: {e}", list(EXPECTED_PATCHES)
    missing = [p for p in EXPECTED_PATCHES if p not in log_text]

    # Content check: grep the running tree for each patch's own marker. Catches working-tree-only
    # patches and survives a rebase that rewords the commits.
    # Resolve each module's file through the VENV interpreter the cluster actually runs -- not
    # system python, and not a guessed site-packages path (that guess produced a false MISSING).
    probe_sh = " ; ".join(
        f'F=$({venv_py()} -c "import {mod} as m;print(m.__file__)" 2>/dev/null); '
        f'grep -qF {shlex.quote(marker)} "$F" 2>/dev/null && echo "OK {f}" || echo "GONE {f}"'
        for f, mod, marker in EXPECTED_SOURCE)
    try:
        r2 = subprocess.run(_SSH + [probe_sh], capture_output=True, text=True, timeout=25)
        out = r2.stdout or ""
        gone = [f for f, _, _ in EXPECTED_SOURCE if f"GONE {f}" in out]
        if not out.strip():
            gone = [f"source check inconclusive: {(r2.stderr or '')[:60]}"]
    except Exception as e:
        gone = [f"source check failed: {e}"]
    missing += gone

    return (not missing), ("all patches present" if not missing else f"MISSING patches: {missing}"), missing

def latest():
    """Last written watchdog status, or {} if none."""
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except Exception:
        return {}

def usable(worker, max_age=120, gen_timeout=20):
    """True if the worker is UP. Trust a fresh cached status (< max_age s); otherwise probe live
    (and record it). This is the fail-fast gate: callers check it BEFORE dispatching so a WEDGED
    worker fails in seconds here instead of hanging every request for the full timeout."""
    row = latest().get(worker)
    if row and (time.time() - row.get("ts", 0)) <= max_age:
        return row.get("status") == "UP", row.get("status", "UNKNOWN"), row.get("detail", "")
    status, ms, detail = probe(worker, gen_timeout)
    # merge this single result into the status file so it isn't lost
    cur = latest(); cur[worker] = {"status": status, "ms": ms, "detail": detail, "ts": round(time.time())}
    try:
        STATUS.parent.mkdir(exist_ok=True); STATUS.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    except Exception:
        pass
    return status == "UP", status, detail

def run(workers, gen_timeout=30):
    out = {}
    for wk in workers:
        status, ms, detail = probe(wk, gen_timeout)
        out[wk] = {"status": status, "ms": ms, "detail": detail, "ts": round(time.time())}
        print(f"  {wk:<8} {status:<7} {ms:>6}ms  {detail}")
    STATUS.parent.mkdir(exist_ok=True)
    STATUS.write_text(json.dumps(out, indent=2), encoding="utf-8")
    bad = [w for w, s in out.items() if s["status"] != "UP"]
    print(f"  -> {len(workers)-len(bad)}/{len(workers)} UP" + (f"; DOWN/WEDGED: {bad}" if bad else "") +
          f"   ({STATUS})")
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", default=",".join(WORKERS), help="comma list; default all")
    ap.add_argument("--gen-timeout", type=int, default=30, help="seconds to wait for one token")
    ap.add_argument("--loop", type=int, default=0, help="re-check every N seconds (0 = one pass)")
    ap.add_argument("--patches", action="store_true", help="also assert the cluster's MLX patches are active")
    a = ap.parse_args()
    if a.patches:
        ok, detail, _ = patches_ok("cluster")
        print(f"  cluster patches: {'OK' if ok else 'FAIL'} -- {detail}")
    workers = [w.strip() for w in a.workers.split(",") if w.strip() in WORKERS]
    while True:
        print(f"watchdog {time.strftime('%H:%M:%S')}:")
        run(workers, a.gen_timeout)
        if not a.loop:
            break
        time.sleep(a.loop)

if __name__ == "__main__":
    main()
