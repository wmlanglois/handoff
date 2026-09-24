"""The cheapest tier: a LOCAL oracle-first repair loop. No frontier, no skeptic.

A worker generates; the frozen oracle (pytest / assertion) runs; on failure the RAW traceback -- not a
natural-language critique -- is fed straight back, because LLMs are fine-tuned on tracebacks and fix from
them fast. It runs deterministically (the caller uses temp 0) and, crucially, ABORTS to escalation the
instant the worker OSCILLATES (repeats a prior state) rather than ping-ponging to the round cap.

Outcomes:
  passed     -> oracle green; hand off to the skeptic + architect review tier.
  escalate   -> worker is structurally stuck (oscillation/repeat); send to the frontier, don't burn more local rounds.
  exhausted  -> hit max_rounds still failing but still making changes; caller decides (usually escalate).

Dependency-injected for testing: pass gen(feedback)->output and check()->(ok, msg). Production wrappers
(repair_tooljob) wire those to the real tool worker + oracle.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "run"))
from oscillation import StateTracker  # noqa


def repair_loop(gen, check, *, max_rounds=4, log=print):
    """gen(feedback: str | None) -> output_str (also writes the artifact the oracle checks).
       check() -> (ok: bool, msg: str)  (runs the frozen oracle; msg is the raw stderr/traceback on fail).
       Returns {status, rounds, ...}."""
    tracker = StateTracker()
    feedback = None
    last_msg = ""
    for rnd in range(1, max_rounds + 1):
        output = gen(feedback)
        obs = tracker.observe(output)
        ok, msg = check()
        last_msg = msg
        log(f"[repair r{rnd}] oracle={'PASS' if ok else 'FAIL'} state={obs.tag} hash={obs.h[:8]} "
            f"chars={len(output or '')}")
        if ok:
            log(f"[repair] PASSED at round {rnd} -> hand to skeptic+architect")
            return {"status": "passed", "rounds": rnd}
        if obs.stuck:
            log(f"[repair] STUCK ({obs.tag}) at round {rnd} -> escalate to frontier (no more local rounds)")
            return {"status": "escalate", "reason": obs.tag, "rounds": rnd, "last_error": msg}
        # raw traceback back into the worker; do NOT summarize it.
        feedback = ("Your previous attempt FAILED its test. Fix ONLY what this traceback reports, and "
                    "return the complete corrected file:\n\n" + (msg or "(no oracle output)"))
    log(f"[repair] EXHAUSTED {max_rounds} rounds still failing -> escalate")
    return {"status": "exhausted", "rounds": max_rounds, "last_error": last_msg}


def repair_tooljob(worker, base_prompt, name, oracle_expr, *, max_rounds=4, log=print):
    """Production wiring: a tool worker that writes files + the frozen oracle run in its workspace, at
    temp 0 for determinism. Kept thin so the loop logic (repair_loop) stays unit-testable without hardware."""
    sys.path.insert(0, str(ROOT / "run"))
    import tooljob  # noqa
    import verified  # noqa: run_oracle lives here
    workspace = {"path": None}

    def gen(feedback):
        prompt = base_prompt if feedback is None else base_prompt + "\n\n" + feedback
        r = tooljob.run_tooljob(worker, prompt, f"repair-{name}", max_rounds=12)
        workspace["path"] = r["workspace"]
        return r["content"]

    def check():
        if not oracle_expr or not workspace["path"]:
            return True, ""
        return verified.run_oracle(oracle_expr, workspace["path"])

    return repair_loop(gen, check, max_rounds=max_rounds, log=log) | {"workspace": workspace["path"]}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Local oracle-first repair. The library is repair_loop; --demo runs the oscillation example.")
    ap.add_argument("--demo", action="store_true", help="run the built-in oscillation example and exit")
    a = ap.parse_args()
    if not a.demo:
        ap.print_help()
        raise SystemExit(0)
    seq = iter(["draft-A", "draft-B", "draft-A", "draft-B"])
    def gen(_fb): return next(seq)
    def check(): return (False, "AssertionError: still wrong")
    print(repair_loop(gen, check, max_rounds=6))
