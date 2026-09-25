"""Add an already-running endpoint to the fleet, once, from the command line -- no source edit.

    python run/registry.py add http://gpu-box:8080 --name gpu-box
    python run/registry.py list
    python run/registry.py health gpu-box
    python run/registry.py remove gpu-box

WHY THIS EXISTS. `fleet._TOPOLOGY` is the hardware this repo was written around. Anyone else's
machine is a source edit away from being usable, which means it is never used. This module is the
other half of `fleet.registry_path()`: it QUALIFIES an endpoint, then PERSISTS it where fleet.py
reads it, and from that moment the endpoint is an ordinary worker -- the queue schedules onto it,
the watchdog gates it, call.chat talks to it, and nothing downstream knows it was added at runtime.

QUALIFICATION IS ESTABLISHED, NOT ASSUMED. That is the whole point of the module, and it is the
part every naive version gets wrong. A port check proves a socket, not a model; `/v1/models`
proves an advertisement, not a completion. So qualification drives the PRODUCTION CALL PATH --
call.chat, with the exact record that will be stored -- and demands generated text back. Two
environment facts that a live 2-Mac mlx-lm cluster taught us the hard way are handled explicitly,
because both look like success from a distance:

  * THE FIRST ADVERTISED MODEL IS NOT A CHAT MODEL. The cluster lists an embedding model first,
    and it answers /v1/chat/completions with 404. Qualifying "the first model" registers a worker
    that 404s on every real job. So every advertised model is tried in turn and a 404 disqualifies
    that MODEL, not the endpoint.
  * A SUCCESSFUL CALL CAN RETURN NULL CONTENT. HTTP 200, a well-formed choice, and
    `message.content = null`, because the token budget went to a separate reasoning field. An
    implementation that tests `resp.ok` registers a worker that returns nothing. So a chat is only
    qualifying when it yields non-empty TEXT, and an empty one is retried once with a larger budget
    before the model is written off.

Lifted and hardened from the verified onboarding run of 2026-09-19
(runs/verified-onboard-endpoint/output.md, 7/7 stages against the live cluster). What changed:
qualification goes through call.chat instead of a private urllib payload, so what is qualified is
what will actually run; capabilities are detected and recorded rather than defaulted silently; the
record is durable in a per-user file instead of a temp file; and registration refuses to shadow a
worker defined in fleet.py.

OUT OF SCOPE THIS TRANCHE, deliberately: hardware discovery, model installation, and authenticated
endpoints (storing a bearer token is a security decision, not a plumbing one). Existing, open
endpoints only.
"""
import argparse
import contextlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "check")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fleet  # noqa: E402
import call as call_mod  # noqa: E402  -- the production call path; qualification uses it on purpose

#: How long to wait for the model list. An unreachable endpoint must fail FAST (I2: `qualify` never
#: raises and returns ok:False promptly), because the CLI blocks on it and so does a human.
DISCOVER_TIMEOUT = 5
#: How long one qualifying completion may take. Generous enough for a cold model load on a Mac,
#: short enough that a wedged server does not hold the operator for the full job timeout.
CHAT_TIMEOUT = 60
#: Token budgets tried in order for the qualifying chat. The second exists only because of the
#: null-content trap: the first budget can be entirely consumed by a reasoning field, and the same
#: model answers perfectly with more room. Ordered small-then-large so a healthy endpoint is cheap.
PROBE_BUDGETS = (128, 640)
#: Reasoning dialects tried, in order, when the operator does not name one. There are only two
#: distinguishable ones for a non-thinking request: call.body_for always sends
#: chat_template_kwargs.enable_thinking=False, and on top of that "budget_field" adds
#: reasoning_budget=0 (the llama.cpp / qwen3.8 dialect). "template_kwargs" differs from "none" only
#: when think=True, so probing it here would cost a call and prove nothing.
PROBE_STYLES = ("none", "budget_field")
#: check/watchdog.py's liveness canary asks for ONE word in max_tokens=2, and the queue's drain
#: refuses to schedule onto a worker that canary calls WEDGED. So a budget this small is not a
#: curiosity -- it decides whether a registered endpoint ever receives work, and qualification
#: measures it. Kept as a conservative floor: watchdog may ask for less room, never for more.
CANARY_BUDGET = 2
#: An endpoint may advertise a long list; each entry costs a live completion. Past this many the
#: list is almost certainly not "one server, a couple of models" and a human should name one.
MAX_MODELS_TRIED = 8
#: The qualifying prompt. Deliberately trivial and deterministic: we are testing the endpoint, not
#: the model's ability, and a hard prompt would fail a small model that is perfectly usable.
PROBE_PROMPT = "Reply with one word: ok"


class Unqualified(RuntimeError):
    """An endpoint failed qualification, so it was not registered. Carries the reason."""


# =================================================================================================
# discovery and capability detection  (network, at registration time only)
# =================================================================================================

def _get_json(url, timeout):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def discover(url, timeout=DISCOVER_TIMEOUT):
    """{"ok", "models", "entries", "error"} -- what the endpoint says it serves.

    Never raises: an unreachable endpoint is an ANSWER here, not an exception. Callers are a CLI and
    a qualification loop, and both want the reason as data.
    """
    base = (url or "").rstrip("/")
    out = {"ok": False, "models": [], "entries": [], "error": None}
    if not base.startswith(("http://", "https://")):
        out["error"] = f"{url!r} is not an http(s) url"
        return out
    try:
        doc = _get_json(base + "/v1/models", timeout)
    except urllib.error.HTTPError as e:
        out["error"] = f"GET /v1/models returned HTTP {e.code}"
        return out
    except Exception as e:                       # URLError, timeout, bad JSON, anything
        out["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        return out
    entries = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        out["error"] = "GET /v1/models did not return an OpenAI-style {\"data\": [...]} list"
        return out
    out["entries"] = [e for e in entries if isinstance(e, dict)]
    out["models"] = [e["id"] for e in out["entries"] if isinstance(e.get("id"), str) and e["id"]]
    if not out["models"]:
        out["error"] = "the endpoint advertises no models"
        return out
    out["ok"] = True
    return out


#: Keys servers use to advertise a context window. Read in this order; the first sane integer wins.
_CTX_KEYS = ("context_length", "max_model_len", "n_ctx", "max_context_length", "context_window")


def _ctx_hint(entry):
    for k in _CTX_KEYS:
        v = entry.get(k)
        if isinstance(v, int) and 512 <= v <= 2_000_000:
            return v, k
    return None, None


def detect_capabilities(url, entries=(), timeout=DISCOVER_TIMEOUT):
    """Report what the endpoint tells us about itself. {"caps": {...}, "evidence": [...]}.

    Detection is restricted to things a server states in a machine-readable way. Everything else
    keeps the conservative default from fleet.make_worker, because a WRONG capability flag is worse
    than a modest one: claiming grammar support we do not have yields silently unconstrained
    output, and claiming more context than the server has is what hangs the mlx cluster at 0% CPU.
    """
    caps, evidence = {}, []
    for e in entries:                              # a model entry may state its own window
        ctx, key = _ctx_hint(e)
        if ctx:
            caps["ctx"] = ctx
            evidence.append(f"/v1/models advertises {key}={ctx}")
            break
    props = None
    for path in ("/props", "/v1/props"):           # llama.cpp only; 404 elsewhere, which is fine
        try:
            props = _get_json(url.rstrip("/") + path, timeout)
            break
        except Exception:
            continue
    if isinstance(props, dict):
        # /props is a llama.cpp server endpoint and llama.cpp honours GBNF grammars and
        # json_schema. This is the same server family as the pairA/pairB/spark workers in
        # fleet.py, where supports_gbnf=True is verified in production.
        caps["supports_gbnf"] = True
        caps["kind"] = "llama"
        evidence.append("responds to /props, so it is a llama.cpp server (grammar-capable)")
        n_ctx = props.get("n_ctx")
        if not isinstance(n_ctx, int):
            gs = props.get("default_generation_settings")
            n_ctx = gs.get("n_ctx") if isinstance(gs, dict) else None
        if isinstance(n_ctx, int) and n_ctx >= 512:
            caps["ctx"] = n_ctx
            evidence.append(f"/props reports n_ctx={n_ctx}")
        slots = props.get("total_slots")
        if isinstance(slots, int) and slots >= 1:
            # Reported and NOT acted on. A server advertising several decode slots may still share
            # one prefill lane -- that is exactly the cluster's shape, and taking its slot count at
            # face value is how you get two prefills inside insert_segments at once. Concurrency
            # above 1 is an operator decision (--max-inflight), made after loading the thing.
            evidence.append(f"/props reports total_slots={slots} (not adopted; see --max-inflight)")
    return {"caps": caps, "evidence": evidence}


# =================================================================================================
# qualification  (drives the production call path)
# =================================================================================================

_PROVISIONAL = threading.Lock()


@contextlib.contextmanager
def _provisional_worker(record):
    """Expose `record` in fleet.WORKERS under a throwaway name so call.chat can use it.

    call.chat looks its worker up BY NAME in the shared WORKERS dict, so qualifying through the
    production path means the candidate has to be in that dict for the duration of one request.
    That is safe here and it is worth saying exactly why, because "temporarily inserting a worker"
    reads alarming: WORKERS is per-process in-memory state, so no other process -- no drain, no
    watchdog, no controller -- can see this entry. Inside this process the only reader during the
    window is the qualification call itself. The name is reserved, unique per call, and removed in
    a finally, so a crash mid-qualification cannot leave a fake worker behind for later code.

    The alternative was to rebuild call.body_for's request here, which would have meant qualifying
    a request shaped differently from the one production sends -- the exact gap this module exists
    to close.
    """
    name = "__qualifying__" + uuid.uuid4().hex[:8]
    with _PROVISIONAL:
        fleet.WORKERS[name] = record
    try:
        yield name
    finally:
        with _PROVISIONAL:
            fleet.WORKERS.pop(name, None)


def _one_chat(record, budget, timeout):
    """(text, note, fatal). fatal=True means the ENDPOINT is gone, not just this model."""
    try:
        with _provisional_worker(record) as name:
            msg, _meta = call_mod.chat(name, [{"role": "user", "content": PROBE_PROMPT}],
                                       max_tokens=budget, think=False, temperature=0,
                                       timeout=timeout, track=False)
    except urllib.error.HTTPError as e:
        # The embedding-model trap: the endpoint is fine, this model is not a chat model.
        return None, f"HTTP {e.code} on /v1/chat/completions", False
    except urllib.error.URLError as e:
        return None, f"unreachable: {getattr(e, 'reason', e)}", True
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}", False
    return (msg.get("content") or ""), None, False


def qualify(url, *, timeout=CHAT_TIMEOUT, discover_timeout=DISCOVER_TIMEOUT,
            model=None, budgets=PROBE_BUDGETS, **overrides):
    """Establish that `url` can actually hold a conversation. Never raises (I2).

    Returns {"ok", "model", "sample", "error", "url", "worker", "capabilities", "tried"}:
      worker        the record that would be registered, ready for fleet.WORKERS
      capabilities  what was detected and how (evidence strings, for the operator)
      tried         one row per model attempted, with why it was rejected
    """
    out = {"ok": False, "model": None, "sample": None, "error": None, "url": url,
           "worker": None, "capabilities": {"caps": {}, "evidence": []}, "tried": []}
    found = discover(url, timeout=discover_timeout)
    if not found["ok"]:
        out["error"] = found["error"]
        return out
    out["capabilities"] = detect_capabilities(url, found["entries"], timeout=discover_timeout)
    caps = dict(out["capabilities"]["caps"])
    caps.update({k: v for k, v in overrides.items() if v is not None})

    candidates = [model] if model else found["models"][:MAX_MODELS_TRIED]
    if model and model not in found["models"]:
        out["tried"].append({"model": model, "why": "not advertised by this endpoint"})
        out["error"] = f"{model!r} is not among the models this endpoint advertises"
        return out
    # An operator who names a dialect has decided; otherwise both are tried at the SMALL budget,
    # because which one an endpoint needs is exactly what cannot be guessed from its model list.
    styles = (caps["reasoning_style"],) if caps.get("reasoning_style") else PROBE_STYLES

    for mid in candidates:
        why = None
        # True while every failure for this model has been a 200 carrying no text. That is the ONE
        # failure a larger budget can fix. An error -- a 404 from an embedding model, a timeout --
        # will not improve with more room to generate, and retrying it just spends the operator's
        # time on an endpoint that already answered.
        empty_only = True
        for style in styles:
            record = fleet.make_worker(url.rstrip("/"), mid, **{**caps, "reasoning_style": style})
            text, note, fatal = _one_chat(record, budgets[0], timeout)
            if fatal:
                out["tried"].append({"model": mid, "why": note})
                out["error"] = note
                return out                                   # the endpoint is down; stop asking
            if note:
                why, empty_only = note, False
                break                                        # a dialect will not fix an error
            if text.strip():
                if style != "none":
                    out["capabilities"]["evidence"].append(
                        f"needs reasoning_style={style!r} to answer inside a "
                        f"{budgets[0]}-token budget")
                return _qualified(out, found, record, mid, text, budgets[0], timeout)
            why = (f"no text in a {budgets[0]}-token budget with reasoning_style={style!r} "
                   f"(content was empty or null)")
        if why and empty_only:
            # 200 and no text under either dialect: the null-content trap with no off switch.
            # The model may still be perfectly usable with room to think, so ask once more.
            record = fleet.make_worker(url.rstrip("/"), mid, **caps)
            text, note, fatal = _one_chat(record, budgets[-1], timeout)
            if fatal:
                out["error"] = note
                out["tried"].append({"model": mid, "why": note})
                return out
            if not note and text.strip():
                return _qualified(out, found, record, mid, text, budgets[-1], timeout)
            why = note or f"{why}; and none in a {budgets[-1]}-token budget either"
        out["tried"].append({"model": mid, "why": why})
    out["error"] = ("no advertised model completed a chat: " +
                    "; ".join(f"{t['model']}: {t['why']}" for t in out["tried"] if t.get("why")))
    return out


def _qualified(out, found, record, mid, text, budget, timeout):
    """Finish a successful qualification, including the liveness canary.

    The canary is checked HERE, at registration, and not left for the operator to discover: an
    endpoint that returns no text in check/watchdog.py's two-token probe is reported WEDGED by the
    watchdog, and the queue's drain refuses to give a WEDGED worker any capacity. Such an endpoint
    would register cleanly, pass every check in this module, and then sit there never receiving a
    job -- silently, which is the worst version of it. So the fact is measured and surfaced.
    """
    canary_text, canary_note, _fatal = _one_chat(record, CANARY_BUDGET, timeout)
    canary_ok = bool(canary_text and canary_text.strip()) and not canary_note
    out.update(ok=True, model=mid, sample=text.strip()[:400], worker=record,
               canary_ok=canary_ok)
    out["tried"].append({"model": mid, "why": None, "budget": budget})
    out.setdefault("qualification", {}).update(
        {"budget": budget, "prompt": PROBE_PROMPT, "canary_ok": canary_ok,
         "canary_budget": CANARY_BUDGET, "models_advertised": found["models"][:20]})
    if not canary_ok:
        out["warnings"] = out.get("warnings", []) + [
            f"this endpoint produced no text in a {CANARY_BUDGET}-token budget "
            f"({canary_note or 'empty content'}). check/watchdog.py's liveness canary uses that "
            f"budget, so it will report the worker WEDGED and run/queue.py's drain will not "
            f"schedule onto it. Re-register with an explicit --reasoning-style if the server has "
            f"a way to turn reasoning off."]
    return out


# =================================================================================================
# durable registration
# =================================================================================================

def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def suggest_name(url):
    """A stable, readable worker name derived from the endpoint: host and port, punctuation
    flattened. Stable matters: re-registering the same endpoint must land on the same record
    instead of quietly accumulating duplicates that all point at one server."""
    u = urlparse(url)
    host = (u.hostname or "endpoint").replace(".", "-").replace(":", "-")
    name = f"{host}-{u.port}" if u.port else host
    name = "".join(c if (c.isalnum() or c in "-_") else "-" for c in name).strip("-")
    return name or "endpoint"


#: A derived name for a bare IP spells that IP out. The registry file lives outside the repo, but a
#: JOB CARD naming the worker does not, and this repo has a standing problem with LAN addresses in
#: committed files (tests/test_portability.py). So it is said once, at the moment a name is chosen.
_LOOKS_LIKE_A_LAN_ADDRESS = re.compile(r"^(?:192-168|10|172-(?:1[6-9]|2\d|3[01]))-\d")


def name_warnings(name):
    if _LOOKS_LIKE_A_LAN_ADDRESS.match(name):
        return [f"the name {name!r} spells out a private network address. Any job card naming this "
                f"worker carries it into the repo, where tests/test_portability.py will fail the "
                f"build. Prefer --name with something like 'gpu-box'."]
    return []


@contextlib.contextmanager
def _registry_lock(path, timeout=10.0, stale_s=60.0):
    """Exclusive-create lock around read-modify-write of the registry file.

    Without it, two registrations started at once each read the file, each add their own worker,
    and the second write erases the first -- a registration that reported success and does not
    exist. That is precisely the durability failure this module is supposed to eliminate, so it is
    locked even though registration is a rare, human-initiated act.
    """
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:                                   # a crashed holder must not block forever
                if time.time() - lock.stat().st_mtime > stale_s:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise TimeoutError(f"another process is holding {lock}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def _read_doc(path):
    records, error = fleet._read_registry(path)
    if error:
        raise RuntimeError(error)
    return dict(records)


def _write_doc(path, workers):
    """Atomic replace, so a crash mid-write leaves the previous registry intact rather than a
    truncated file that fleet.py would report as corrupt."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps({"version": fleet.REGISTRY_VERSION, "workers": workers}, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def _reload_if_live(path):
    """Refresh fleet.WORKERS only when the file just written is the one this process reads.

    Writing to an explicit `path` that is not the live registry (building a registry for another
    machine, or for a subprocess under a different FLEET_REGISTRY) must not rebuild this process's
    table from a DIFFERENT file -- that would quietly drop the workers this process does have.
    """
    if Path(path) == Path(fleet.registry_path()):
        fleet.reload_workers()


def entries(path=None):
    """name -> stored registration record (url, model, capabilities, qualification, last health).

    This is the FULL record. fleet.WORKERS deliberately exposes only the worker keys, so that a
    registered worker and a static one are the same shape downstream; provenance lives here.
    """
    return _read_doc(path or fleet.registry_path())


def register(url, name=None, force=False, *, path=None, model=None, timeout=CHAT_TIMEOUT,
             **overrides):
    """Qualify `url`, then persist it. Returns the stored record. Raises Unqualified if it cannot
    hold a conversation, and ValueError for a name that is not ours to give.

    `force` REPLACES an existing registration of the same name. It does NOT skip qualification --
    there is no such option, because a worker registered without a completion is a worker that
    fails on its first real job instead of at the moment somebody could have fixed it.
    """
    path = Path(path or fleet.registry_path())
    name = name or suggest_name(url)
    if name in fleet._TOPOLOGY:
        raise ValueError(f"{name!r} is a worker defined in fleet.py; pick another --name "
                         f"(a registration may never repoint the shipped topology)")
    result = qualify(url, timeout=timeout, model=model, **overrides)
    if not result["ok"]:
        raise Unqualified(f"{url} did not qualify: {result['error']}")
    record = dict(result["worker"])
    record.update({
        "registered_at": _now(),
        "warnings": result.get("warnings", []) + name_warnings(name),
        "qualification": {"model": result["model"], "sample": result["sample"],
                          "evidence": result["capabilities"]["evidence"],
                          "tried": result["tried"], **result.get("qualification", {})},
    })
    with _registry_lock(path):
        workers = _read_doc(path)
        if name in workers and not force:
            raise ValueError(f"{name!r} is already registered ({workers[name].get('url')}); "
                             f"pass force=True / --force to replace it")
        if name in workers:
            record["last_health"] = workers[name].get("last_health")
        workers[name] = record
        _write_doc(path, workers)
    _reload_if_live(path)
    return record


def unregister(name, path=None):
    """Remove a registration. True if it was there. Refuses to pretend it can remove a static
    worker: those come from fleet.py and deleting a line from a JSON file would not touch them."""
    path = Path(path or fleet.registry_path())
    if name in fleet._TOPOLOGY:
        raise ValueError(f"{name!r} is defined in fleet.py, not registered; edit the source to "
                         f"remove it")
    with _registry_lock(path):
        workers = _read_doc(path)
        if name not in workers:
            return False
        workers.pop(name)
        _write_doc(path, workers)
    _reload_if_live(path)
    return True


# =================================================================================================
# health
# =================================================================================================

def health(name, live=True, path=None):
    """Is this worker currently usable? {"name", "ok", "status", "detail", "checked"}.

    It DELEGATES to check/watchdog.usable rather than inventing a second notion of health, and that
    is the load-bearing decision in this function. The queue's drain already calls watchdog.usable
    before handing a worker any capacity, so a registered endpoint that goes away is skipped by the
    same gate, for the same reason, as a static one that goes away -- no separate code path exists
    to be forgotten. The watchdog's probe is an end-to-end generation canary, not a port check,
    because /health lies: the cluster returns 200 while its tensor-parallel peer is dead.
    """
    if name not in fleet.WORKERS:
        return {"name": name, "ok": False, "status": "UNKNOWN",
                "detail": "not a worker in fleet.WORKERS", "checked": _now()}
    import watchdog                                  # local: keeps `import registry` cheap
    ok, status, detail = watchdog.usable(name, max_age=0 if live else 120)
    row = {"name": name, "ok": bool(ok), "status": status, "detail": detail, "checked": _now()}
    _remember_health(name, row, path)
    return row


def _remember_health(name, row, path=None):
    """Best effort: store the last health result beside the registration so `list` can report it
    without probing. A failure here must never turn a health answer into an exception -- the
    caller asked whether a worker is usable, not whether the disk is writable."""
    path = Path(path or fleet.registry_path())
    try:
        with _registry_lock(path, timeout=2.0):
            workers = _read_doc(path)
            if name not in workers:
                return
            workers[name]["last_health"] = row
            _write_doc(path, workers)
    except Exception:
        pass


def status(path=None):
    """Every worker the fleet can see, registered or not, with its provenance and last known
    health. This is the operator's answer to "did my endpoint actually get added?"."""
    stored = {}
    error = None
    try:
        stored = entries(path)
    except RuntimeError as e:
        error = str(e)
    rows = []
    for name, w in sorted(fleet.WORKERS.items()):
        rec = stored.get(name, {})
        rows.append({"name": name, "source": w.get("source", "static"), "url": w["url"],
                     "model": w["model"], "ctx": w["ctx"], "max_inflight": w["max_inflight"],
                     "registered_at": rec.get("registered_at"),
                     "last_health": rec.get("last_health")})
    return {"workers": rows, "registry": str(fleet.registry_path()),
            "error": error or fleet.REGISTRY_ERROR}


# =================================================================================================
# CLI
# =================================================================================================

def _safe(v):
    """Never str() a worker's url or model blindly: an unconfigured STATIC worker holds a poison
    object that raises on str(), and `registry list` -- whose whole job is to say what the fleet can
    see -- must keep working on a machine with no settings file at all. That is the normal state of
    a clean clone whose only worker is a registered one."""
    try:
        return str(v)
    except fleet.SettingsMissing:
        return "<unconfigured: see fleet_settings.example.py>"


#: The OpenAI-compatible local servers a first-use user is most likely already running. A returning
#: user's saved workers live in the registry and are reused directly; this is only for finding a
#: first endpoint when nothing is registered yet.
DEFAULT_ENDPOINTS = [
    ("http://localhost:1234", "LM Studio"),
    ("http://localhost:8080", "llama.cpp (llama-server)"),
    ("http://localhost:11434", "Ollama"),
    ("http://localhost:1337", "Jan"),
]


def connect(urls=None, *, path=None, register_found=True, recheck=True,
            discover_timeout=DISCOVER_TIMEOUT):
    """First-use / returning-user helper (issues #11, #16). Never raises.

    Reuses what is already saved -- a returning user's registry persists across projects, so this
    re-canaries the saved workers instead of re-onboarding -- then probes the common local model
    servers for an OpenAI /v1/models endpoint and REGISTERS what actually generates (register()
    qualifies with a real completion; a reachable port is not proof). Returns a structured report;
    the CLI turns an empty result into plain "here is how to start a model" guidance.
    """
    candidates = list(urls) if urls is not None else [u for u, _ in DEFAULT_ENDPOINTS]
    labels = {} if urls is not None else {u: lbl for u, lbl in DEFAULT_ENDPOINTS}
    existing = entries(path)
    existing_urls = {(w.get("url") or "").rstrip("/") for w in existing.values()}
    saved = []
    for nm, w in existing.items():
        row = {"name": nm, "url": w.get("url"), "model": w.get("model")}
        if recheck:
            try:
                row["health"] = health(nm, live=True, path=path)
            except Exception as e:  # a re-canary failure is data, not a crash
                row["health"] = {"status": "error", "error": repr(e)}
        saved.append(row)
    found, registered, failed = [], [], []
    for url in candidates:
        d = discover(url, timeout=discover_timeout)
        if not d["ok"]:
            failed.append({"url": url, "label": labels.get(url, ""), "error": d["error"]})
            continue
        found.append({"url": url, "label": labels.get(url, ""), "models": d["models"]})
        if register_found and url.rstrip("/") not in existing_urls:
            try:
                rec = register(url, path=path)
                registered.append({"url": url, "name": suggest_name(url), "model": rec["model"]})
            except (Unqualified, ValueError) as e:
                failed.append({"url": url, "label": labels.get(url, ""),
                               "error": "advertises models but did not qualify: {0}".format(e)})
    usable = bool(saved) or bool(registered)
    return {"saved": saved, "found": found, "registered": registered, "failed": failed,
            "usable": usable}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="qualify an endpoint and register it")
    a.add_argument("url", help="base url of a running OpenAI-style server, e.g. http://host:8080")
    a.add_argument("--name", help="worker name (default: derived from host and port; give a real "
                                  "one if the host is a bare IP -- see the warning it prints)")
    a.add_argument("--model", help="qualify this model instead of trying each advertised one")
    a.add_argument("--force", action="store_true", help="replace an existing registration")
    a.add_argument("--ctx", type=int, help="override the detected context window")
    a.add_argument("--max-inflight", type=int, help="override concurrent requests (default 1)")
    a.add_argument("--supports-gbnf", action="store_true", help="server honours grammars")
    a.add_argument("--prefill-lock", action="store_true",
                   help="serialize prefills (a second mlx-lm tensor-parallel cluster needs this)")
    a.add_argument("--reasoning-style", choices=("none", "template_kwargs", "budget_field"))

    sub.add_parser("list", help="show every worker, with provenance and last known health")
    h = sub.add_parser("health", help="probe a worker now (end-to-end generation canary)")
    h.add_argument("name")
    r = sub.add_parser("remove", help="drop a registration")
    r.add_argument("name")
    q = sub.add_parser("check", help="qualify an endpoint WITHOUT registering it")
    q.add_argument("url")
    q.add_argument("--model")

    c = sub.add_parser("connect", help="first-use: find and register a local model server, and "
                                       "re-check already-saved workers")
    c.add_argument("--url", action="append", dest="urls",
                   help="probe this url instead of the common defaults (repeatable)")
    c.add_argument("--no-register", action="store_true", help="only report what is found")
    c.add_argument("--no-recheck", action="store_true", help="do not re-canary saved workers")

    args = ap.parse_args(argv)

    if args.cmd == "connect":
        rep = connect(urls=args.urls, register_found=not args.no_register,
                      recheck=not args.no_recheck)
        for s in rep["saved"]:
            h = s.get("health") or {}
            print(f"  saved      {s['name']:<18} {_safe(s['url'])[:34]:<34} {h.get('status', '-')}")
        for r in rep["registered"]:
            print(f"  registered {r['name']:<18} {_safe(r['url'])[:34]:<34} model={_safe(r['model'])[:24]}")
        for f in rep["failed"]:
            print(f"  no model   {(f['label'] or f['url']):<18} {_safe(f['url'])[:34]:<34} {f['error']}")
        if not rep["usable"]:
            print("\n  No working model found. Start one, then re-run `registry.py connect`:")
            print("    LM Studio  ->  Developer tab, Start Server   (http://localhost:1234)")
            print("    llama.cpp  ->  llama-server -m your-model.gguf --port 8080")
            print("    Ollama     ->  ollama serve                  (http://localhost:11434)")
            print("  Or point at a specific address:  registry.py connect --url http://HOST:PORT")
            return 1
        print(f"\n  ready: {len(rep['saved'])} saved, {len(rep['registered'])} newly registered.")
        print(f"  stored in {fleet.registry_path()}")
        return 0

    if args.cmd == "add":
        overrides = {"ctx": args.ctx, "max_inflight": args.max_inflight,
                     "reasoning_style": args.reasoning_style}
        if args.supports_gbnf:
            overrides["supports_gbnf"] = True
        if args.prefill_lock:
            overrides["requires_prefill_lock"] = True
        try:
            rec = register(args.url, args.name, force=args.force, model=args.model, **overrides)
        except (Unqualified, ValueError) as e:
            print(f"NOT REGISTERED: {e}")
            return 1
        name = args.name or suggest_name(args.url)
        print(f"registered {name}: {rec['url']} model={rec['model']} ctx={rec['ctx']} "
              f"max_inflight={rec['max_inflight']}")
        print(f"  qualified by generating: {rec['qualification']['sample'][:60]!r}")
        for e in rec["qualification"]["evidence"]:
            print(f"  detected: {e}")
        print(f"  reasoning_style={rec['reasoning_style']} supports_gbnf={rec['supports_gbnf']}")
        for w in rec.get("warnings") or []:
            print(f"  WARNING: {w}")
        print(f"  stored in {fleet.registry_path()}")
        return 0

    if args.cmd == "check":
        res = qualify(args.url, model=args.model)
        print(json.dumps({k: v for k, v in res.items() if k != "worker"}, indent=2))
        return 0 if res["ok"] else 1

    if args.cmd == "list":
        st = status()
        print(f"registry: {st['registry']}")
        if st["error"]:
            print(f"  PROBLEM: {st['error']}")
        for row in st["workers"]:
            hr = row["last_health"] or {}
            print(f"  {row['name']:<18} {row['source']:<10} {_safe(row['url']):<34} "
                  f"{_safe(row['model'])[:28]:<28} "
                  f"{hr.get('status', '-'):<7} {hr.get('checked', '')}")
        return 0

    if args.cmd == "health":
        row = health(args.name)
        print(f"{row['name']}: {row['status']} -- {row['detail']}")
        return 0 if row["ok"] else 1

    if args.cmd == "remove":
        try:
            gone = unregister(args.name)
        except ValueError as e:
            print(str(e))
            return 1
        print(f"removed {args.name}" if gone else f"{args.name} was not registered")
        return 0 if gone else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
