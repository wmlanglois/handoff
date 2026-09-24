"""Health diagnosis and authorized recovery for workers, driven by configuration.

WHY THIS EXISTS. An unavailable worker used to end the night: `orchestrate` probed once at start,
dropped whatever was down, and reported BLOCKED. The machinery to fix a wedged MLX cluster already
existed in `ops/autorecover.py`, but nothing connected it to goal execution, and it was hardcoded
to one operator's two Macs. So the harness could watch a worker die and do nothing about it.

THREE THINGS THIS IS DELIBERATELY NOT:
  * not a model. Routine health, retries, readiness and restarts are Python. A language model is
    asked only about an UNFAMILIAR failure, and then it is handed the diagnostic bundle rather than
    told to rediscover basic health in prose.
  * not a topology. Each worker declares how it is managed and what may be done to it. Nothing in
    here knows about Macs.
  * not an installer. Recovery restarts a service it was told it owns. It never installs a model,
    never touches an unrelated process, and never restarts something it does not manage.

MANAGEMENT OWNERSHIP is the safety boundary, and the default is the safe one:

  EXTERNAL       (default) the harness may OBSERVE but MUST NOT restart. Someone else owns this
                 process. An endpoint a user registered is external until they say otherwise, so
                 an unconfigured worker can never be restarted by accident.
  MANAGED_LOCAL  a process on this machine that the harness started or is authorized to restart.
  MANAGED_REMOTE a service on another host the harness is authorized to restart, via a configured
                 command. The command comes from settings; this module supplies no default.

CONFIGURE per worker in fleet_settings.local.py, keyed by worker name:

    RECOVERY = {
      "cluster": {"management": "managed_remote",
                  "recover": ["ssh", "host", "bash ~/relaunch.sh"],   # exact argv, no shell
                  "startup_grace_s": 240, "max_attempts": 2, "cooldown_s": 60},
      "pairA":   {"management": "external"},        # observe only; the default anyway
    }
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "check"))
sys.path.insert(0, str(ROOT / "run"))

EXTERNAL = "external"
MANAGED_LOCAL = "managed_local"
MANAGED_REMOTE = "managed_remote"
MANAGEMENTS = (EXTERNAL, MANAGED_LOCAL, MANAGED_REMOTE)

# Failure classes. The point of separating them is that they need different responses: a restart
# helps a wedged process and does nothing at all for a bad model name.
CONNECTION = "connection"       # nothing is listening
LOADING = "loading"             # listening, not serving yet -- WAIT, do not restart
INFERENCE = "inference"         # serving, but generation fails or hangs
CONFIGURATION = "configuration"  # reachable and healthy, but not configured as we expect
HEALTHY = "healthy"
UNKNOWN = "unknown"

EVIDENCE = ROOT / "runs" / "recovery.jsonl"

DEFAULTS = {"management": EXTERNAL, "recover": None, "startup_grace_s": 180,
            "max_attempts": 2, "cooldown_s": 30, "probe_timeout_s": 30,
            "total_effort_s": 900}


class RecoveryRefused(Exception):
    """Recovery was not attempted, and the reason is a policy fact rather than a failure."""


def policy(worker, overrides=None):
    """The recovery policy for one worker: settings first, safe defaults otherwise."""
    conf = dict(DEFAULTS)
    try:
        import fleet
        table = getattr(fleet, "RECOVERY", None) or {}
        conf.update(table.get(worker) or {})
    except Exception:
        pass
    conf.update(overrides or {})
    if conf["management"] not in MANAGEMENTS:
        raise ValueError("worker {0!r}: management must be one of {1}, got {2!r}".format(
            worker, list(MANAGEMENTS), conf["management"]))
    return conf


def _record(**row):
    """Structured evidence for every attempt. Appended, never rewritten."""
    row.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
    try:
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        with open(EVIDENCE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass
    return row


def diagnose(worker, gen_timeout=30, probe=None, port_open=None, models=None):
    """Classify WHY a worker is unavailable, using what the backend actually exposes.

    Four classes, because they need different answers and a single boolean cannot tell them apart.
    In particular LOADING must never trigger a restart: killing a server that is still mapping
    weights turns a wait into an outage."""
    import watchdog
    probe = probe or watchdog.probe
    port_open = port_open or watchdog.port_open
    try:
        import fleet
        url = fleet.WORKERS[worker]["url"]
    except Exception as e:
        return {"worker": worker, "class": CONFIGURATION, "detail": "unknown worker: {0}".format(e)}

    if not port_open(url):
        return {"worker": worker, "class": CONNECTION, "url": url,
                "detail": "nothing accepting connections on {0}".format(url)}

    listed = None
    if models is not None:
        listed = models(worker)
    else:
        try:
            import urllib.request
            with urllib.request.urlopen(url + "/v1/models", timeout=10) as r:
                listed = [m.get("id") for m in (json.loads(r.read()).get("data") or [])]
        except Exception:
            listed = None

    status, ms, detail = probe(worker, gen_timeout=gen_timeout)
    if status == "UP":
        return {"worker": worker, "class": HEALTHY, "ms": ms, "detail": detail, "models": listed}
    if listed is None:
        # Port open, API not answering: the usual shape of a server still loading a model.
        return {"worker": worker, "class": LOADING, "url": url, "ms": ms,
                "detail": "port open but the model list is not being served yet; "
                          "a restart here would turn a wait into an outage"}
    return {"worker": worker, "class": INFERENCE, "url": url, "ms": ms, "models": listed,
            "detail": "serving the API but generation failed: {0}".format(detail)}


def _run_command(argv, timeout):
    """Run exactly the configured argv. No shell, so nothing here can be turned into one."""
    if not argv or not isinstance(argv, (list, tuple)):
        raise RecoveryRefused("no recovery command configured")
    return subprocess.run(list(argv), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def recover(worker, *, attempt=1, overrides=None, sleep=time.sleep, runner=None, prober=None,
            run_id=None):
    """Attempt ONE authorized recovery and verify it with real inference.

    Returns an evidence row. It never raises for an ordinary failure: a refusal and a failed
    attempt are both outcomes the caller has to act on, and turning them into exceptions hides the
    diagnostics."""
    run_id = run_id or uuid.uuid4().hex[:8]
    conf = policy(worker, overrides)
    started = time.time()
    base = {"run_id": run_id, "worker": worker, "attempt": attempt,
            "management": conf["management"], "operation": "recover"}

    if conf["management"] == EXTERNAL:
        return _record(**base, result="refused", verified=False,
                       error="worker is EXTERNAL: the harness may observe it but must not restart "
                             "it. Set RECOVERY[{0!r}]['management'] if this harness owns the "
                             "process.".format(worker))
    if attempt > int(conf["max_attempts"]):
        return _record(**base, result="exhausted", verified=False,
                       error="max_attempts ({0}) reached".format(conf["max_attempts"]))

    before = diagnose(worker, probe=prober) if prober else diagnose(worker)
    if before["class"] == LOADING:
        return _record(**base, result="waited", verified=False, before=before["class"],
                       error="worker is still loading; waiting is the correct action and a "
                             "restart would turn a wait into an outage")

    try:
        proc = (runner or _run_command)(conf.get("recover"), conf.get("startup_grace_s", 180))
    except RecoveryRefused as e:
        return _record(**base, result="refused", verified=False, before=before["class"],
                       error=str(e))
    except subprocess.TimeoutExpired:
        return _record(**base, result="timeout", verified=False, before=before["class"],
                       seconds=round(time.time() - started, 1),
                       error="recovery command exceeded startup_grace_s")
    except Exception as e:
        return _record(**base, result="error", verified=False, before=before["class"],
                       error="{0}: {1}".format(type(e).__name__, e))

    rc = getattr(proc, "returncode", 0)
    sleep(min(10, conf.get("startup_grace_s", 180) / 10.0))     # let the service bind

    after = diagnose(worker, probe=prober) if prober else diagnose(worker)
    ok = after["class"] == HEALTHY
    return _record(**base,
                   result="recovered" if ok else "failed",
                   verified=ok, returncode=rc, before=before["class"], after=after["class"],
                   seconds=round(time.time() - started, 1),
                   detail=after.get("detail", "")[:300],
                   error="" if ok else "recovery ran but inference did not come back")


def recover_with_retries(worker, *, overrides=None, sleep=time.sleep, runner=None, prober=None):
    """Bounded: max_attempts, a cooldown between them, and a total effort ceiling.

    Returns (ok, rows). Unbounded retry against a machine that is not coming back is how a night
    gets spent producing nothing."""
    conf = policy(worker, overrides)
    run_id = uuid.uuid4().hex[:8]
    rows, t0 = [], time.time()
    for attempt in range(1, int(conf["max_attempts"]) + 1):
        if time.time() - t0 > conf["total_effort_s"]:
            rows.append(_record(run_id=run_id, worker=worker, attempt=attempt,
                                operation="recover", result="effort_exhausted", verified=False,
                                error="total_effort_s ({0}) spent".format(conf["total_effort_s"])))
            break
        row = recover(worker, attempt=attempt, overrides=overrides, sleep=sleep,
                      runner=runner, prober=prober, run_id=run_id)
        rows.append(row)
        if row.get("verified"):
            return True, rows
        if row.get("result") in ("refused", "waited"):
            break                      # policy says stop; retrying changes nothing
        if attempt < int(conf["max_attempts"]):
            sleep(conf["cooldown_s"])
    return False, rows


def required_next_action(rows):
    """When recovery is unavailable or exhausted, say exactly what a person must do."""
    if not rows:
        return "no recovery was attempted"
    last = rows[-1]
    r = last.get("result")
    if r == "refused" and last.get("management") == EXTERNAL:
        return ("{0} is an external endpoint: start it yourself, or set "
                "RECOVERY['{0}'] management and a recover command if this harness owns it."
                .format(last["worker"]))
    if r == "refused":
        return "{0}: {1}".format(last["worker"], last.get("error", "recovery refused"))
    if r == "waited":
        return "{0} is still loading; re-check rather than restart".format(last["worker"])
    if r in ("exhausted", "effort_exhausted"):
        return ("{0}: recovery exhausted after {1} attempts; inspect the service by hand"
                .format(last["worker"], len(rows)))
    return ("{0}: recovery ran but inference did not return ({1}); inspect the service"
            .format(last["worker"], last.get("after", "unknown")))
