"""Spec-driven runner on the fleet's one call path. For every step it does an actual
health canary, enforces a hard duration CEILING (a step past its ceiling is HUNG, killed,
and logged — never assumed "still busy"), logs everything as JSONL, and exits NON-ZERO if
any step fails. No fail-open: a broken run reports broken.

    python run/localrun.py run/specs/smoke.json
"""
import argparse, json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from safeio import force_utf8  # noqa
force_utf8()
from call import chat  # noqa
from log import logger  # noqa

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    spec = json.loads(Path(ap.parse_args().spec).read_text(encoding="utf-8"))
    emit = logger("localrun")
    print(f"localrun {spec.get('name','?')}  ->  log: {emit.path}")
    results, ctx = [], {}
    for step in spec["steps"]:
        sid, worker = step["id"], step["worker"]
        task = step["task"]
        for dep in step.get("needs", []):
            task = task.replace("{" + dep + "}", ctx.get(dep, "")[:4000])
        ceiling = step.get("ceiling_s", 180)
        # 1. health canary — is the worker completing, not just reachable
        try:
            _m, _t = chat(worker, [{"role": "user", "content": "reply ok"}], max_tokens=3, timeout=15, track=False)
            emit("health-ok", step=sid, worker=worker, ms=_t["ms"])
        except Exception as e:
            emit("worker-down", step=sid, worker=worker, error=repr(e))
            results.append({"id": sid, "status": "worker-down"}); continue
        # 2. the step, under a hard ceiling
        t0 = time.time()
        try:
            m, ti = chat(worker, [{"role": "user", "content": task}],
                         max_tokens=step.get("max_tokens", 700), think=step.get("think", False), timeout=ceiling)
            out = (m.get("content") or "").strip()
            if not out:
                emit("empty", step=sid, worker=worker, ms=ti["ms"])
                results.append({"id": sid, "status": "empty"}); continue
            ctx[sid] = out
            emit("done", step=sid, worker=worker, ms=ti["ms"], chars=len(out))
            results.append({"id": sid, "status": "done", "ms": ti["ms"]})
        except Exception as e:
            secs = round(time.time() - t0)
            status = "hung" if secs >= ceiling - 1 else "error"
            emit(status, step=sid, worker=worker, seconds=secs, ceiling_s=ceiling, error=repr(e))
            results.append({"id": sid, "status": status})
    bad = [r for r in results if r["status"] != "done"]
    emit("summary", done=len(results) - len(bad), total=len(results),
         bad=[f"{r['id']}:{r['status']}" for r in bad])
    emit.close()
    print("\n  " + "  ".join(f"{r['id']}:{r['status']}" for r in results))
    print(f"  DONE {len(results)-len(bad)}/{len(results)}")
    if bad:
        print(f"  FAIL: {', '.join(f'{r['id']}:{r['status']}' for r in bad)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
