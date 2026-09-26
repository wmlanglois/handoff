"""Fleet preflight: run this before every iteration. Actual probes, not port-open lies.

Per worker it checks three distinct things, because a reachable port does NOT mean a
working model (a /health 200 can lie):
  1. reachable   - GET /v1/models answers (records latency)
  2. completes   - a tiny canary prompt returns non-empty text within a tight timeout
                   (a reachable worker that does not complete the canary is LOCKED, not busy)
  3. tool-calls  - a one-tool request actually emits tool_calls (cluster + spark)

Plus the Legion tool-service /health. Everything is logged to runs/preflight-<ts>.jsonl
with latency and error text. Exits non-zero if any REQUIRED worker or the tool-service
fails, so an iteration cannot proceed on a broken fleet (no fail-open).

Assumes the fleet is idle: a REQUIRED single-slot worker legitimately mid-job will queue
the canary and read as 'locked'. That case is handled by per-job duration ceilings in the
runner, not here; preflight is the between-iterations idle check.

    python check/preflight.py            # all workers
    python check/preflight.py --canary-timeout 30
"""
import argparse, json, os, re, secrets, sys, time, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fleet import WORKERS, REQUIRED, TOOL_SERVICE, DEFAULT_WORKER, SKEPTIC_WORKER  # noqa
from log import logger  # noqa

def _get(url, timeout):
    t = time.time()
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read(), round((time.time() - t) * 1000)

def _post(url, body, timeout):
    t = time.time()
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()), round((time.time() - t) * 1000)

def probe_worker(name, w, canary_timeout, emit):
    url, model = w["url"], w["model"]
    row = {"worker": name, "verdict": "ok"}
    # 1. reachable
    try:
        _, _, ms = _get(url + "/v1/models", 6)
        row["reach_ms"] = ms
        emit("reachable", worker=name, ms=ms)
    except Exception as e:
        row["verdict"] = "unreachable"
        emit("unreachable", worker=name, url=url, error=repr(e))
        return row
    # 2. completes (canary) - distinguishes locked from reachable
    try:
        d, ms = _post(url + "/v1/chat/completions",
                      {"model": model, "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                       "max_tokens": 5, "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}},
                      canary_timeout)
        text = (d.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        row["canary_ms"] = ms
        if not text:
            row["verdict"] = "empty"
            emit("canary-empty", worker=name, ms=ms)
            return row
        emit("completes", worker=name, ms=ms, reply=text[:40])
    except Exception as e:
        # reachable but no completion in time = locked, NOT assumed-busy
        row["verdict"] = "locked"
        emit("locked", worker=name, canary_timeout_s=canary_timeout, error=repr(e))
        return row
    # 3. tool-call capability (cluster + spark)
    if w.get("_require_toolcall", name in (DEFAULT_WORKER, SKEPTIC_WORKER)):
        try:
            tool = [{"type": "function", "function": {"name": "ping", "description": "return ok",
                     "parameters": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}}}]
            d, ms = _post(url + "/v1/chat/completions",
                          {"model": model, "messages": [{"role": "user", "content": "Call the ping tool with x='ok'."}],
                           "tools": tool, "tool_choice": "auto", "max_tokens": 60,
                           "chat_template_kwargs": {"enable_thinking": False}}, canary_timeout)
            has = bool(d.get("choices", [{}])[0].get("message", {}).get("tool_calls"))
            row["toolcall"] = has
            emit("toolcall", worker=name, ok=has, ms=ms)
            if not has:
                row["verdict"] = "no-toolcall"
        except Exception as e:
            row["toolcall"] = False
            emit("toolcall-error", worker=name, error=repr(e))
            row["verdict"] = "no-toolcall"
    return row

def probe_tool_service(emit):
    try:
        token = Path(TOOL_SERVICE["token_file"]).read_text(encoding="utf-8").strip()
    except Exception as e:
        emit("tool-service-no-token", error=repr(e))
        return {"tool_service": "no-token"}
    try:
        req = urllib.request.Request(TOOL_SERVICE["url"] + "/health",
                                     headers={"Authorization": "Bearer " + token})
        t = time.time()
        with urllib.request.urlopen(req, timeout=6) as r:
            ok = r.status == 200
        emit("tool-service", ok=ok, ms=round((time.time() - t) * 1000))
        return {"tool_service": "ok" if ok else "bad-status"}
    except Exception as e:
        emit("tool-service-down", url=TOOL_SERVICE["url"], error=repr(e))
        return {"tool_service": "down"}

_RUNNER = re.compile(r"\bpython3?(?:\.exe)?\s+-m\s+([A-Za-z_]\w*)")


def modules_named(texts):
    """Third-party modules the plan's checks RUN as `python -m <module>` (pytest, hypothesis, ...),
    plus the operator's TOOL_HOST_REQUIRED_MODULES. Stdlib runners (unittest) are dropped. Imports
    inside check text are not guessed at: they are usually the project's own modules."""
    from fleet import setting
    found = set(setting("TOOL_HOST_REQUIRED_MODULES", []) or [])
    for t in texts:
        found.update(m.split(".")[0] for m in _RUNNER.findall(t or ""))
    std = getattr(sys, "stdlib_module_names", ())
    return sorted(m for m in found if m not in std)


def probe_tool_host(emit, modules):
    """Run a probe through the tool service's own python_run, so the answer is about the
    interpreter worker code and self-checks actually execute on -- not the conductor's."""
    code = ("import importlib.util, json, sys\n"
            "mods = {0!r}\n"
            "print(json.dumps({{'python': sys.executable, 'version': sys.version.split()[0],"
            " 'missing': [m for m in mods if importlib.util.find_spec(m) is None]}}))").format(list(modules))
    try:
        token = Path(TOOL_SERVICE["token_file"]).read_text(encoding="utf-8").strip()
        body = json.dumps({"job_id": "preflight-probe", "call_id": "deps-" + secrets.token_hex(6),
                           "name": "python_run", "arguments": {"code": code}}).encode()
        req = urllib.request.Request(TOOL_SERVICE["url"] + "/call", data=body, method="POST",
                                     headers={"Authorization": "Bearer " + token,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
        out = (resp.get("result") or {}) if resp.get("ok") else {}
        info = json.loads((out.get("stdout") or "").strip().splitlines()[-1])
    except Exception as e:
        emit("tool-host-probe-failed", error=repr(e))
        return {"verdict": "probe-failed", "error": repr(e)[:200], "required": list(modules)}
    info["required"] = list(modules)
    info["verdict"] = "missing-modules" if info.get("missing") else "ok"
    emit("tool-host", **info)
    return info


def tool_runtime_info():
    """Which tool runtime tool jobs will use and what it declares (see run/tooljob.runtime_info)."""
    run_dir = str(Path(__file__).resolve().parent.parent / "run")
    if run_dir not in sys.path:
        sys.path.insert(0, run_dir)
    import tooljob
    return tooljob.runtime_info()


def gate(worker_names, canary_timeout=25, emit=None, need_tool_service=True, tool_host_modules=()):
    """Non-exiting preflight for one run. Returns (ok, reason, rows).

    Probes the run's coding lanes AND the skeptic (run_goal's own health check covers only
    coding lanes, so a dead/missing skeptic would otherwise pass) plus the tool-service. `reason`
    names every concrete failure -- which lane and its verdict (unreachable/locked/empty/
    no-toolcall/unconfigured), a missing skeptic, or a down tool-service -- so a run refuses up
    front with a specific cause instead of dying at dispatch. A reachable port is not health.

    Skeptic policy (README: the skeptic is optional): a NAMED skeptic that is missing or broken
    fails the gate like any lane. An EMPTY SKEPTIC_WORKER is an explicit opt-out: it passes, but a
    `not-configured` skeptic row is returned so the caller can say the run has no independent
    review -- it is never silently omitted.

    The tool-service is probed only when the run needs it (`need_tool_service`: some assignment
    has tools:true). A plan of chat-only packets does not require it, and must not be forced to
    --skip-preflight -- which would also skip the lane and skeptic checks -- just to run."""
    if emit is None:
        emit = lambda *a, **k: None  # noqa: E731
    names = [n for n in (worker_names or []) if n]
    if SKEPTIC_WORKER:
        names.append(SKEPTIC_WORKER)
    seen, probe_names = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            probe_names.append(n)
    rows, reasons = [], []
    for n in probe_names:
        w = WORKERS.get(n)
        if not w:
            rows.append({"worker": n, "verdict": "unconfigured"})
            reasons.append("{0}: not in the fleet config".format(n))
            continue
        required_tools = (need_tool_service and n in (worker_names or [])) or n == SKEPTIC_WORKER
        r = probe_worker(n, dict(w, _require_toolcall=required_tools), canary_timeout, emit)
        rows.append(r)
        if r.get("verdict") != "ok":
            reasons.append("{0}: {1}".format(n, r.get("verdict")))
    if not SKEPTIC_WORKER:
        rows.append({"worker": "(skeptic)", "role": "skeptic", "verdict": "not-configured"})
    if need_tool_service:
        ts = probe_tool_service(emit)
        if ts["tool_service"] not in ("ok",):
            reasons.append("tool-service: {0}".format(ts["tool_service"]))
        if ts["tool_service"] == "ok":
            th = probe_tool_host(emit, tool_host_modules)
            rows.append({"worker": "(tool-host)", "role": "tool-host", **th})
            if th["verdict"] != "ok" and not os.environ.get("HANDOFF_ALLOW_MISSING_TOOL_DEPS"):
                reasons.append(
                    "tool-host: python {0} at {1} lacks {2}; install them there or set "
                    "HANDOFF_ALLOW_MISSING_TOOL_DEPS=1".format(th.get("version", "?"), th.get("python", "?"),
                                                                ", ".join(th.get("missing") or []))
                    if th["verdict"] == "missing-modules" else
                    "tool-host: could not run the dependency probe ({0})".format(th.get("error")))
        rt = tool_runtime_info()
        rows.append({"worker": "(tool-runtime)", "role": "tool-runtime",
                     "verdict": "ok" if not rt.get("missing") else "degraded", **rt})
        if rt.get("missing") and not os.environ.get("HANDOFF_ALLOW_DEGRADED_TOOL_RUNTIME"):
            reasons.append("tool-runtime: {0} runtime at {1} lacks {2}; use the bundled runtime (unset "
                           "FLEET_DISPATCH_DIR) or set HANDOFF_ALLOW_DEGRADED_TOOL_RUNTIME=1 to accept it"
                           .format(rt.get("kind"), rt.get("path"), ", ".join(rt["missing"])))
    else:
        rows.append({"worker": "(tool-service)", "role": "tool-service", "verdict": "not-needed"})
    return (not reasons), "; ".join(reasons), rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--canary-timeout", type=int, default=25)
    a = ap.parse_args()
    emit = logger("preflight")
    print(f"fleet preflight  ->  log: {emit.path}")
    rows = [probe_worker(n, w, a.canary_timeout, emit) for n, w in WORKERS.items()]
    ts = probe_tool_service(emit)

    print("\n  worker    reach   canary   toolcall  verdict")
    for r in rows:
        print(f"  {r['worker']:<9} {str(r.get('reach_ms','-'))+'ms':>6} {str(r.get('canary_ms','-'))+'ms':>7}"
              f"   {str(r.get('toolcall','-')):>7}   {r['verdict']}")
    print(f"  tool-service: {ts['tool_service']}")

    bad_required = [r['worker'] for r in rows if r['worker'] in REQUIRED and r['verdict'] != 'ok']
    bad_optional = [r['worker'] for r in rows if r['worker'] not in REQUIRED and r['verdict'] != 'ok']
    ts_bad = ts['tool_service'] not in ('ok',)
    emit("summary", required_bad=bad_required, optional_bad=bad_optional, tool_service=ts['tool_service'])
    emit.close()
    if bad_optional:
        print(f"  WARN optional workers not ok: {', '.join(bad_optional)}")
    if bad_required or ts_bad:
        print(f"\n  FAIL: required={bad_required or 'ok'} tool_service={ts['tool_service']}")
        sys.exit(1)
    print("\n  PASS: required workers and tool-service healthy.")

if __name__ == "__main__":
    main()
