"""Single source of truth for fleet endpoints AND their compute backends. Everything
imports from here so a changed address, a backend quirk, or a patch note is fixed in
one place instead of resurfacing as a stale-config bug.

PORTABILITY (milestone 5). This file used to hard-code one operator's LAN: two Mac IPs, a
Windows SSH key path, a mac username, and a dated directory under C:\\Users. That made the repo
unclonable -- a stranger got plausible-looking config that pointed at machines they do not own.
So every machine-specific value now comes from `fleet_settings.local.py` (gitignored), documented
by the committed `fleet_settings.example.py`. The shapes, ports, capability flags and caps stay
here, because they are facts about the SOFTWARE, not about anybody's house.

The rule that shapes the design: a missing setting must never become a plausible wrong default.
A placeholder like "127.0.0.1" or "" would turn a configuration mistake into a connection error
three layers away, which is exactly the class of bug this module exists to prevent. So:

  * importing `fleet` ALWAYS works, with or without local settings -- otherwise the whole test
    suite and CI would require the author's hardware;
  * every unresolved machine-specific value is an `_Unset` poison object: harmless to hold and to
    repr (so tests and logs can print a config dict), but ANY use -- str(), +, ==, os.fspath(),
    bool() -- raises SettingsMissing naming the file to copy.

REGISTERED WORKERS. The five entries in _TOPOLOGY are the hardware this repo was written around.
An operator with another compatible endpoint should not have to edit this file to use it, so
WORKERS is the union of that static topology and a per-user registry file written by
`run/registry.py` (see the REGISTERED WORKERS section below). A registered worker carries exactly
the same keys as a static one plus `source`, so nothing downstream can tell them apart, and
reading WORKERS never touches the network.

Check your own machine with:

    python -c "import fleet; print(fleet.WORKERS['cluster']['url'], fleet.SSH_KEY)"
    python run/registry.py list
"""
import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
#: Machine-specific values live here. Gitignored; never committed.
LOCAL_SETTINGS = Path(os.environ.get("FLEET_SETTINGS") or (ROOT / "fleet_settings.local.py"))
#: Committed template that documents every setting.
EXAMPLE_SETTINGS = ROOT / "fleet_settings.example.py"


class SettingsMissing(RuntimeError):
    """Raised when a machine-specific value is USED but was never configured."""


def _howto(name):
    return (f"fleet setting {name!r} is not configured.\n"
            f"  Copy {EXAMPLE_SETTINGS.name} to {LOCAL_SETTINGS.name} (in {ROOT}) and fill in\n"
            f"  {name}. That file is gitignored: it holds addresses, usernames and key paths that\n"
            f"  must never be committed. See README.md -> Configure.")


def _load_local(path=None):
    """Import fleet_settings.local.py by path (it is deliberately NOT a package import: the file
    is outside version control and must not shadow anything on sys.path)."""
    path = Path(path or LOCAL_SETTINGS)
    if not path.exists():
        # An explicitly requested file that is absent is a mistake worth shouting about; a plain
        # missing default file is the normal clean-clone/CI state and must stay importable.
        if os.environ.get("FLEET_SETTINGS"):
            raise SettingsMissing(f"FLEET_SETTINGS={path} does not exist "
                                  f"(template: {EXAMPLE_SETTINGS.name})")
        return None
    spec = importlib.util.spec_from_file_location("fleet_settings_local", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_LOCAL = _load_local()


class _Unset:
    """A configured-nothing that refuses to pretend. Holding it is fine; using it raises.

    The failure this prevents: earlier drafts defaulted a missing Mac address to a placeholder, and
    the resulting ConnectionRefusedError looked like "the cluster is down" instead of "you never
    configured the cluster". Only __repr__ and identity are safe, so a dict of workers can still be
    printed in a log or a pytest diff without exploding.
    """
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def _raise(self, *a, **k):
        raise SettingsMissing(_howto(self.name))

    def __repr__(self):
        return f"<unset fleet setting {self.name}: see {EXAMPLE_SETTINGS.name}>"

    __str__ = __bool__ = __eq__ = __ne__ = __lt__ = __gt__ = __le__ = __ge__ = _raise
    __hash__ = __len__ = __iter__ = __contains__ = __getitem__ = __call__ = _raise
    __add__ = __radd__ = __mod__ = __format__ = __fspath__ = _raise


_REQUIRED = object()


def setting(name, default=_REQUIRED):
    """Read one setting from fleet_settings.local.py.

    Settings with a portable default (a loopback URL, the repo root) get that default. Settings
    that name somebody's hardware have NO default: absent, they return `_Unset`, which raises with
    an actionable message the moment anything tries to use it.
    """
    if _LOCAL is not None and hasattr(_LOCAL, name):
        return getattr(_LOCAL, name)
    if default is _REQUIRED:
        return _Unset(name)
    return default


def require(name):
    """Read a setting and raise RIGHT HERE if it is unconfigured (for call sites that want to fail
    before doing work, rather than at the first use of the value)."""
    v = setting(name)
    if isinstance(v, _Unset):
        raise SettingsMissing(_howto(name))
    return v


def settings_loaded():
    """True when fleet_settings.local.py was found and imported."""
    return _LOCAL is not None


#: Every setting that names hardware and therefore has no default. fleet_settings.example.py must
#: document all of them (tests/test_portability.py enforces that).
REQUIRED_SETTINGS = ("MAC_USER", "MAC48", "MAC24", "SSH_KEY", "MINER_HOST",
                     "CLUSTER_MODEL")
#: Settings with a portable default, so a clean clone still does something sensible.
OPTIONAL_SETTINGS = ("TOOL_SERVICE_URL", "CODE_ROOT", "RECENCY_ROOTS", "PORTS", "MODELS",
                     "REGISTRY_FILE", "PRIMARY_WORKER", "SKEPTIC_WORKER", "FLEET_DISPATCH_DIR",
                     "TOOL_SERVICE_TOKEN_FILE")

# Each backend names its runtime, its upstream repo and local checkout (so a worker or
# a human can look up how it actually behaves), and where our patches on top are written.
# Checkout locations are written relative to a host ROLE (mac48, miner), never as somebody's
# absolute path -- the addresses of those roles are settings.
BACKENDS = {
    "mlx-lm": {
        "runtime": "mlx-lm on Apple Metal (Mac48+Mac24 tensor-parallel, JACCL over Thunderbolt, MTP)",
        "repo": "https://github.com/ml-explore/mlx-lm",
        "local": "MAC48:~/Documents/LocalAI/mlx-lm-mtp  (AirRunner feat/mtp-batched)",
        "patches": "patches/MLX-PATCHES.md",
    },
    "llama.cpp": {
        "runtime": "llama.cpp on CUDA (RTX 3060 tensor-split, qwen3 MTP draft)",
        "repo": "https://github.com/ggml-org/llama.cpp",
        "local": "MINER_HOST:C:/AI  (the CUDA box; address is the MINER_HOST setting)",
        "patches": None,  # stock build; document here if that changes
    },
}

# Hardware (2026-09-18, per operator): dedicated silicon on the miner -- 3080(10GB)=spark, 3060=vision,
# and TWO INDEPENDENT dual-3060 rigs (4x 3060 total): pairA = 2x3060, pairB = 2x3060, each its own 24GB
# 27B lane with no cross-lane contention. No VRAM collision between spark and the 27B. The Mac cluster
# (mac48+mac24, 72GB unified) is the PRIMARY local reasoning/coding node.
#   max_inflight: concurrent in-flight cap per worker (cross-process, via the ledger). llama servers are
#   total_slots=1 (verified via /props), so 1 each. The cluster keeps 2 (its two decode slots); a PREFILL
#   LOCK (jobs.acquire_lock via call.chat) serializes the prompt-prefill stage so two requests never hit
#   insert_segments at once -- that wins back the 2nd decode slot without the concurrent-prefill crash.
#   ctx: usable context window (tokens). Cluster runs a ~16k window; the llama servers are n_ctx=65536.
# Workers are defined by CAPABILITIES, not silicon. The orchestrator only speaks OpenAI-style HTTP to
# each url; the flags below drive the few real behavioral differences (all of which must stay -- they
# exist because of measured failures, e.g. the prefill lock prevents the mlx insert_segments crash):
#   requires_prefill_lock : server has one prefill lane; serialize the prefill stage across requests
#   supports_gbnf         : server honors grammar / json_schema constrained decoding (llama.cpp does)
#   reasoning_style       : how to turn thinking off/on for this server --
#                           "template_kwargs" = reasoning_effort via chat_template_kwargs (mlx-lm)
#                           "budget_field"    = reasoning_budget=0 when thinking is off (llama.cpp qwen3.8)
#   kind/backend          : informational (logs, docs); no routing logic keys on these anymore.
# host/port/model are the only parts that move between installations, so they come from settings:
# `host` names WHICH setting holds the address, PORTS/MODELS may override the defaults below.
_TOPOLOGY = {
    "cluster": {"host": "MAC48", "port": 8000, "model_setting": "CLUSTER_MODEL",
                "kind": "mlx",   "backend": "mlx-lm",    "max_inflight": 2, "ctx": 16384,
                "requires_prefill_lock": True,  "supports_gbnf": False, "reasoning_style": "template_kwargs"},
    "pairA":   {"host": "MINER_HOST", "port": 8031, "model": "qwen38-27b",
                "kind": "llama", "backend": "llama.cpp", "max_inflight": 1, "ctx": 65536,
                "requires_prefill_lock": False, "supports_gbnf": True,  "reasoning_style": "budget_field"},
    "pairB":   {"host": "MINER_HOST", "port": 8032, "model": "qwen38-27b",
                "kind": "llama", "backend": "llama.cpp", "max_inflight": 1, "ctx": 65536,
                "requires_prefill_lock": False, "supports_gbnf": True,  "reasoning_style": "budget_field"},
    "spark":   {"host": "MINER_HOST", "port": 8034, "model": "spark-x25-4b",
                "kind": "llama", "backend": "llama.cpp", "max_inflight": 1, "ctx": 65536,
                "requires_prefill_lock": False, "supports_gbnf": True,  "reasoning_style": "none"},
    "vision":  {"host": "MINER_HOST", "port": 8033, "model": "qwen35-9b",
                "kind": "llama", "backend": "llama.cpp", "max_inflight": 1, "ctx": 65536,
                "requires_prefill_lock": False, "supports_gbnf": True,  "reasoning_style": "none"},
}

_PORTS = setting("PORTS", {}) or {}
_MODELS = setting("MODELS", {}) or {}


def _url(host, port):
    """http://host:port, or the poison object if the host setting is unconfigured. Never a
    placeholder address: a wrong URL is indistinguishable from a down worker at the call site."""
    if isinstance(host, _Unset):
        return host
    return f"http://{host}:{port}"


# =================================================================================================
# REGISTERED WORKERS -- endpoints a user adds at runtime, with no source edit
# =================================================================================================
# The five entries above are the topology this repo ships with. An operator who already runs a
# compatible OpenAI-style server should be able to hand it to the fleet without editing _TOPOLOGY,
# so `run/registry.py` qualifies an endpoint once and writes a record here, and everything that
# reads WORKERS picks it up on the next process start.
#
# THREE RULES SHAPE THIS LOADER, and each exists because of a specific way it could go wrong:
#
#  1. READING WORKERS MUST DO NO NETWORK I/O. `from fleet import WORKERS` happens at import in
#     call.py, queue.py, watchdog.py, preflight, skeptic... If the loader probed an endpoint, every
#     one of those imports would block on somebody's unplugged machine. So this reads ONE small
#     JSON file and nothing else. Qualification happens once, at registration, in run/registry.py.
#  2. IMPORTING FLEET MUST NEVER FAIL because of the registry. A corrupt or half-written file must
#     not take the whole harness down -- it degrades to "no registered workers" and reports why in
#     REGISTRY_ERROR, which the registry CLI and preflight surface. A vanished worker leaves its
#     jobs unroutable, which is safe; an unimportable fleet.py stops everything, which is not.
#  3. A REGISTERED RECORD MAY NEVER SHADOW A STATIC ONE. Otherwise a stray line in a file outside
#     the repo could repoint "cluster" at another address and the whole fleet would follow it
#     without a single source change. Registration refuses the collision, and so does this loader,
#     because the file can also be hand-edited.
#
# Registrations live OUTSIDE the repo (per-user state, like a known_hosts file) so a clone carries
# no one's endpoints and `git status` stays clean. Override with FLEET_REGISTRY (env, highest
# precedence -- tests and one-off runs use it) or REGISTRY_FILE in fleet_settings.local.py.
REGISTRY_ENV = "FLEET_REGISTRY"
#: Schema version of the registry file. Bump only for an incompatible change; the loader refuses a
#: version it does not understand rather than guessing at fields it has never seen.
REGISTRY_VERSION = 1
#: Keys EVERY worker record carries, static or registered. I1 requires a registered worker to be
#: indistinguishable from a static one apart from `source`; identical key sets are how that is
#: enforced mechanically instead of promised in prose (tests/test_fleet_registration.py asserts it).
WORKER_KEYS = ("url", "model", "kind", "backend", "ctx", "max_inflight",
               "requires_prefill_lock", "supports_gbnf", "reasoning_style", "source")
#: Context window assumed for an endpoint that advertises none. Deliberately modest: call.fit_context
#: trims to this, and claiming MORE context than a server has is the failure that hangs the mlx
#: cluster at 0% CPU (upstream #1493). Claiming less only costs some trimming. Registration reads the
#: real value when the server reports one (llama.cpp /props n_ctx), and `--ctx` overrides both.
DEFAULT_REGISTERED_CTX = 8192


def _default_registry_path():
    """Per-user state directory, never inside the repo: %LOCALAPPDATA%\\fleet on Windows,
    $XDG_STATE_HOME/fleet (or ~/.local/state/fleet) elsewhere."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"),
                                                                ".local", "state")
    return Path(base) / "fleet" / "workers.json"


def registry_path():
    """Where registrations are read from and written to. Resolved on every call, not cached, so a
    test or a one-off run can point FLEET_REGISTRY somewhere else and have BOTH the reader here and
    the writer in run/registry.py agree -- a split between them would 'lose' a registration that
    was in fact written correctly."""
    env = os.environ.get(REGISTRY_ENV)
    if env:
        return Path(env)
    configured = setting("REGISTRY_FILE", None)
    if configured:
        return Path(configured)
    return _default_registry_path()


REGISTRY_FILE = registry_path()
#: Why registered workers are missing or incomplete, or None. Empty file == no error: a fleet with
#: no registrations is the normal state, and reporting it as a problem trains people to ignore this.
REGISTRY_ERROR = None


def make_worker(url, model, *, kind="openai", backend=None, ctx=DEFAULT_REGISTERED_CTX,
                max_inflight=1, requires_prefill_lock=False, supports_gbnf=False,
                reasoning_style="none", source="registered"):
    """Build one worker record with conservative defaults for an endpoint we know little about.

    Every default below is the SAFE side of a measured failure on this fleet, not a guess at the
    nicest behaviour:

      max_inflight=1          -- never burst an unknown server. It also does the serialization work
                                 the cluster's prefill lock does: one in-flight request per worker
                                 (enforced cross-process by the jobs ledger) means two prefills
                                 cannot meet inside the server, which is the crash the lock exists
                                 to prevent. Raise it only for a server you have actually loaded.
      requires_prefill_lock=False -- the lock path streams SSE and takes a cross-process mutex. With
                                 max_inflight=1 it would buy nothing, and demanding streaming from a
                                 server that does not do it would fail qualification for the wrong
                                 reason. Set it explicitly for a second mlx-lm cluster.
      supports_gbnf=False     -- a server that ignores a grammar returns unconstrained output that
                                 LOOKS fine, so a wrong True is silent and a wrong False only costs
                                 us a constraint we can check ourselves afterwards.
      reasoning_style="none"  -- the two styles we know are backend-specific dialects. Sending the
                                 wrong one is a malformed request; sending neither is a plain call.
    """
    return {"url": url, "model": model, "kind": kind, "backend": backend, "ctx": int(ctx),
            "max_inflight": int(max_inflight),
            "requires_prefill_lock": bool(requires_prefill_lock),
            "supports_gbnf": bool(supports_gbnf), "reasoning_style": str(reasoning_style),
            "source": source}


def _valid_registration(name, rec):
    """Turn one stored record into a worker entry, or return (None, reason).

    A malformed record is DROPPED rather than repaired. The failure this prevents: a record with a
    missing or non-http url still entering WORKERS, where the first thing that happens is a job
    being routed to it and dying at the call site three layers away -- the same class of bug the
    _Unset poison exists to prevent for settings.
    """
    if not isinstance(name, str) or not name or name != name.strip():
        return None, f"registered name {name!r} is not a usable worker name"
    if name in _TOPOLOGY:
        return None, (f"registration {name!r} ignored: a worker defined in fleet.py already owns "
                      f"that name, and a file outside the repo may not repoint it")
    if not isinstance(rec, dict):
        return None, f"registration {name!r} is not an object"
    url, model = rec.get("url"), rec.get("model")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None, f"registration {name!r} has no usable http(s) url ({url!r})"
    if not isinstance(model, str) or not model:
        return None, f"registration {name!r} names no model"
    try:
        kw = {k: rec[k] for k in ("kind", "backend", "ctx", "max_inflight",
                                  "requires_prefill_lock", "supports_gbnf", "reasoning_style")
              if k in rec}
        return make_worker(url, model, **kw), None
    except (TypeError, ValueError) as e:                 # a hand-edited ctx of "lots", say
        return None, f"registration {name!r} has an unusable field: {e}"


def _read_registry(path=None):
    """(records, error). Never raises -- see rule 2 above."""
    path = Path(path or registry_path())
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None                                   # no registrations is the normal state
    except OSError as e:
        return {}, f"cannot read the worker registry at {path}: {e}"
    if not raw.strip():
        return {}, None
    try:
        doc = json.loads(raw)
    except ValueError as e:
        return {}, (f"the worker registry at {path} is not valid JSON ({e}); registered workers "
                    f"are unavailable until it is fixed")
    if not isinstance(doc, dict):
        return {}, f"the worker registry at {path} is not an object"
    version = doc.get("version")
    if version != REGISTRY_VERSION:
        return {}, (f"the worker registry at {path} is version {version!r}, this fleet understands "
                    f"{REGISTRY_VERSION}")
    workers = doc.get("workers")
    if not isinstance(workers, dict):
        return {}, f"the worker registry at {path} has no 'workers' object"
    return workers, None


def _build_workers():
    out = {}
    for name, t in _TOPOLOGY.items():
        host = setting(t["host"])
        model = _MODELS.get(name) or (setting(t["model_setting"]) if "model_setting" in t else t["model"])
        w = {k: v for k, v in t.items() if k not in ("host", "port", "model_setting")}
        w["url"] = _url(host, _PORTS.get(name, t["port"]))
        w["model"] = model
        w["source"] = "static"
        out[name] = w
    stored, error = _read_registry()
    problems = [error] if error else []
    for name, rec in sorted(stored.items()):
        entry, why = _valid_registration(name, rec)
        if entry is None:
            problems.append(why)
            continue
        out[name] = entry
    global REGISTRY_ERROR
    REGISTRY_ERROR = "; ".join(problems) or None
    return out


WORKERS = _build_workers()


def reload_workers():
    """Re-read the registry into the LIVE WORKERS dict and return it.

    Mutated in place on purpose. Half the harness does `from fleet import WORKERS`, which binds the
    dict object itself; rebinding a module global here would leave every one of those modules
    holding the old table and a registration would appear to take effect in some modules and not
    others. A controller restart picks registrations up on its own -- this is for the one process
    that registers and then wants to use what it just registered.
    """
    global REGISTRY_FILE
    REGISTRY_FILE = registry_path()
    fresh = _build_workers()
    WORKERS.clear()
    WORKERS.update(fresh)
    return WORKERS

# --- The Macs (the mlx cluster hosts) and how to reach them over SSH. SINGLE SOURCE: every module that
# ssh's (skeptic grounding, regdb, scout, watchdog, autorecover) imports these instead of re-typing them.
SSH_KEY = setting("SSH_KEY")
MAC_USER = setting("MAC_USER")
# Where the MLX venv and launcher live on the Macs. A path under someone's home
# directory is configuration, not code: hardcoding it both leaks the operator and
# guarantees the ops scripts do nothing on anyone else's machine.
# Where the MLX venv and launcher live on the Macs.
# It has NO computed default, because deriving one from MAC_USER would read an unset
# setting at import time and a clean clone would fail at module load rather than at the
# point of use. Absent, it behaves like every other unconfigured host value: safe to print,
# and raises with instructions the moment something tries to reach a Mac with it.
MAC_LOCALAI = setting("MAC_LOCALAI")
MAC48 = setting("MAC48")   # rank 0, serves :8000, runs the launcher
MAC24 = setting("MAC24")   # rank 1


def ssh_cmd(host=None, connect_timeout=10):
    """Base ssh argv for a Mac host; append the remote command.

    It does NOT raise when the Macs are unconfigured, and that is deliberate: tools/regdb.py and
    web/scout.py build their SSH prefix at MODULE IMPORT time (`SSH = ssh_cmd()`), so raising here
    would make a clean clone unimportable -- and with it the test suite and CI, which must run
    without anyone's hardware. Instead the unresolved parts stay poison objects inside the argv:
    building the command is free, and the first attempt to RUN or print it raises SettingsMissing
    naming the setting. No placeholder host ever reaches subprocess."""
    host = MAC48 if host is None else host
    user = MAC_USER
    if isinstance(host, _Unset):
        target = host
    elif isinstance(user, _Unset):
        target = user
    else:
        target = f"{user}@{host}"
    return ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}",
            "-i", SSH_KEY, target]


# The Mac cluster is the primary reasoning/coding node; the CUDA pairs are strong parallel lanes.
DEFAULT_WORKER = setting("PRIMARY_WORKER", "cluster")
SKEPTIC_WORKER = setting("SKEPTIC_WORKER", "spark")
# A clean installation can use one registered endpoint for both roles. The historical pairA lane
# is capacity, not a requirement for a different user's fleet.
REQUIRED = tuple(dict.fromkeys((DEFAULT_WORKER, SKEPTIC_WORKER)))

# The tool-service runs on the orchestrator box itself, so loopback is a real default; the token
# file is per-installation and has none.
TOOL_SERVICE = {
    "url": setting("TOOL_SERVICE_URL", "http://127.0.0.1:8041"),
    "token_file": setting("TOOL_SERVICE_TOKEN_FILE", str(ROOT / "runs" / "tool-service" / "token.txt")),
}

# Where code the fleet reads/reviews lives. Default: this repo, which is the only tree a clean
# clone is guaranteed to have. Operators with several sibling repos list them in RECENCY_ROOTS.
CODE_ROOT = setting("CODE_ROOT", str(ROOT))
RECENCY_ROOTS = tuple(setting("RECENCY_ROOTS", (str(ROOT),)))
# Optional compatibility with a separately managed tool runtime. The bundled core is the
# clean-clone default; it needs only a token file to start its local service.
FLEET_DISPATCH_DIR = setting("FLEET_DISPATCH_DIR", str(ROOT / "tool_runtime"))
