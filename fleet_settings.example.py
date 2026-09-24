"""Optional hardware template for the gitignored fleet_settings.local.py.

For a registered OpenAI-compatible endpoint, do NOT copy the Mac/miner block below.
Instead, after `python run/registry.py add URL --name my-worker --model MODEL`, create
fleet_settings.local.py with only:

    PRIMARY_WORKER = "my-worker"
    SKEPTIC_WORKER = "my-worker"

The second seat can name a different registered endpoint. A CLI-only skeptic is not
supported by the connected project path. See README.md for the ordered setup flow.

Copy this whole template only if you operate the Mac/miner topology described below,
then replace every placeholder you actually use. The values below are deliberately
invalid, not example servers to try. Omitted optional settings use defaults; the
static hardware settings are needed only by the paths that use that hardware.
"""

# ----------------------------------------------------------------------------------------------
# LAB-SPECIFIC -- only for the Mac/miner topology. Not needed for a registered endpoint.
# ----------------------------------------------------------------------------------------------

#: Login name on the Mac cluster hosts (used for ssh user@host).
MAC_USER = "your-mac-username"

# Where the MLX venv and launcher live on the Macs. Defaults to
# /Users/<MAC_USER>/Documents/LocalAI; set it if yours is elsewhere.
MAC_LOCALAI = "<absolute path to the LocalAI directory on your Macs>"
# e.g. the Documents/LocalAI folder under MAC_USER's home. Required by the MLX ops
# scripts and the patch watchdog; unused if you run no Mac cluster.

#: Rank 0 of the mlx tensor-parallel pair: serves the OpenAI-style API and runs the launcher.
MAC48 = "10.x.x.x"

#: Rank 1 of the mlx pair. Reachable over ssh for recovery; it does not serve HTTP itself.
MAC24 = "10.x.x.y"

#: Private key for BatchMode ssh to both Macs. Absolute path on THIS machine.
#: Windows example: r"C:\path\to\.ssh\mac_control_ed25519"
SSH_KEY = "/path/to/.ssh/mac_control_ed25519"

#: The CUDA box that serves the llama.cpp workers (pairA, pairB, spark, vision) on ports
#: 8031/8032/8034/8033. One address; the ports are fixed in fleet.py (override with PORTS below).
MINER_HOST = "10.x.x.z"

#: Model identifier the CLUSTER server expects. For mlx-lm this is the model DIRECTORY as seen
#: BY THE MAC, e.g. "/Users/<you>/Models/Qwen3.8-27B-4bit-mtp".
CLUSTER_MODEL = "/path/on/the/mac/to/Qwen3.8-27B-4bit-mtp"

#: The bundled service creates a random token at runs/tool-service/token.txt on first start.
#: Override only if you run a separately managed service with a different token file.
# TOOL_SERVICE_TOKEN_FILE = r"C:\path\to\tool-service\token.txt"

#: Optional compatibility override for a separately managed tool runtime containing agent.py.
#: The bundled core in tool_runtime/ is used when this is unset.
# FLEET_DISPATCH_DIR = r"C:\path\to\other-runtime"

# ----------------------------------------------------------------------------------------------
# OPTIONAL -- portable defaults already work. Uncomment only to change them.
# ----------------------------------------------------------------------------------------------

#: Where the tool-service listens. Default: "http://127.0.0.1:8041" (same box as the orchestrator).
# TOOL_SERVICE_URL = "http://127.0.0.1:8041"

#: Root the skeptic and other readers are allowed to read code under (the --root default).
#: Default: this repo. Widen it to a parent directory if your workers review sibling repos.
# CODE_ROOT = r"C:\Dev"

#: Trees check/recency.py scans when answering "which source is newest?".
#: Default: (this repo,). Add the sibling repos that describe the same fleet.
# RECENCY_ROOTS = (r"C:\path\to\fleet", r"C:\path\to\another-repo")

#: Per-worker port overrides, if your servers do not use the defaults in fleet.py.
# PORTS = {"cluster": 8000, "pairA": 8031, "pairB": 8032, "spark": 8034, "vision": 8033}

#: Per-worker model-name overrides (the llama.cpp aliases; the cluster uses CLUSTER_MODEL).
# MODELS = {"pairA": "qwen38-27b", "spark": "spark-x25-4b"}

# Controller roles. Defaults preserve the original lab's names (cluster and spark). For a new
# installation, register your own endpoint(s) and name them here. The same endpoint may fill both
# roles while you are getting started; add a separate cheap skeptic later if you want one.
# PRIMARY_WORKER = "my-worker"
# SKEPTIC_WORKER = "my-worker"

#: Where endpoints you ADD AT RUNTIME are stored (see below). Default: a per-user state file
#: outside the repo -- %LOCALAPPDATA%\fleet\workers.json on Windows,
#: $XDG_STATE_HOME/fleet/workers.json (or ~/.local/state/fleet/workers.json) elsewhere. Point it
#: somewhere else only if you want several checkouts to share one set of registered endpoints.
#: The FLEET_REGISTRY environment variable overrides both this and the default.
# REGISTRY_FILE = r"C:\path\to\fleet-workers.json"

# ----------------------------------------------------------------------------------------------
# ADDING YOUR OWN ENDPOINT -- no setting required, and no source edit
# ----------------------------------------------------------------------------------------------
# The five workers in fleet.py describe one operator's hardware. If you already run an
# OpenAI-compatible server (llama.cpp, mlx-lm, vLLM, Ollama's OpenAI endpoint, anything that
# answers GET /v1/models and POST /v1/chat/completions), hand it to the fleet instead of editing
# fleet.py:
#
#     python run/registry.py add http://your-host:8080 --name your-box
#     python run/registry.py list
#
# `add` QUALIFIES the endpoint before it will store it: it tries each advertised model until one
# actually generates text, which is how it steps over an embedding model that 404s on chat and
# over a reply whose content is null because the token budget went to a reasoning field. What it
# detects (context window, grammar support, which reasoning dialect turns thinking off) is printed
# and stored with the record. Override any of it with --ctx / --max-inflight / --reasoning-style /
# --supports-gbnf / --prefill-lock.
#
# The registration is a file, not a code change: the next controller you start picks it up, the
# queue schedules onto it by capability, and the watchdog gates it like any other worker. This
# works on a clean clone with NO settings file at all -- a registered endpoint needs none of the
# values above.
