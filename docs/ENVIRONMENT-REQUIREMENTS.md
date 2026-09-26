# Local environment requirements and readiness

For an environment already configured in LiteLLM, use the optional
[discovery/import guide](LITELLM-IMPORT.md). It previews aliases and imports one
through the existing registry after explicit generation qualification. Existing
servers stay unchanged; configuration reuse is not full workload qualification.

Technical setup guide and requirements — September 25, 2026. Not a completed setup wizard or a claim of cross-platform qualification. Runtime observations include the carry correction (#26) and partial generation diagnostics (#27); see implementation status for remaining work. No model servers were probed or configured for this research. See also the [Phase 2 environment/workbench proposal](PHASE-2-ENVIRONMENT.md).

Tracking: [environment journey #28](https://github.com/wmlanglois/handoff/issues/28), [profiles/readiness #25](https://github.com/wmlanglois/handoff/issues/25), [role setup #11](https://github.com/wmlanglois/handoff/issues/11), [carry #26](https://github.com/wmlanglois/handoff/issues/26), [diagnostics #27](https://github.com/wmlanglois/handoff/issues/27).

## Start with the right expectation

Handoff coordinates work using your environment. It does not make an unstable model server stable, install missing drivers, or turn insufficient memory into a frontier-model experience. Establish a reliable serving baseline before an unattended project run. Conversely, Handoff must not blame a model for missing tools, inaccessible inputs, incorrect request limits, or broken state mapping that the harness introduced.

For autonomous coding, workers need an authorized way to inspect material, execute checks, observe failures and revise their work. Chat-only generation with external feedback is a reduced execution mode, not equivalent to worker-controlled testing. An advertised tool is not enough: its actual execution environment must work.

Established environments also need compatibility checks. A model that works in a desktop chat can still have an incompatible tool template, a different API configuration, no persistent service, or no interpreter suitable for the project. Preserve known-good settings rather than retuning them automatically.

## 1. Map the environment before choosing commands

Record these separately, even if they are all on one computer:

| Component | Establish |
|---|---|
| Controller | Where Handoff runs, Python executable, checkout revision, launching account and process environment. |
| Model serving | Host, serving application/version, exact model identifier, API address, authentication and how the service stays running. |
| Tool execution | Where generated code executes, interpreter/virtual environment, packages, executables, timeout and permitted workspace. |
| Architect and skeptic | Backend, role assignment, authentication, permitted tools and evidence access. |
| Persistent state | Resolved registry, jobs/locks, goals, memory, artifacts and project/package paths. |

The model host does not automatically contain the worker's code or dependencies. SSH access to a computer is not an inference API. A model loaded on a GPU is not proof of a listening HTTP service. A desktop chat history is not automatically Handoff's conversation history.

Ask first: “Which application or command do you normally use to talk to the model, and on which computer?” Accept “I don't know the URL”; use the named application's server page or documented settings to establish it. Do not require the user to already speak API terminology.

### Cold-start discovery sequence (proposed complete workflow)

1. Inspect the explicitly selected Handoff registry/settings and ask which existing setup to use. Show names and targets without printing credentials. Do not search unrelated private repositories for addresses.
2. Determine whether the model is on this computer, a named remote host, a container/VM, or only accessible through SSH/terminal generation.
3. For local desktop serving, inspect its server screen or authorized local listener/process information. Common ports are candidates, never proof.
4. For remote serving, obtain the specific hostname/address and port from the user or authorized service configuration. No subnet sweep or guessed credentials.
5. Check the HTTP API from the controller's network context. A ping tests neither the correct service nor its model; ping can also fail when HTTP works. Distinguish DNS, connection, TLS, authorization, model-list and generation failures.
6. Confirm the exact advertised generation model. An embedding model in a list is not a coding worker.
7. With a bounded authorization, qualify generation and the required workflow. Reuse applicable standing approval; do not request it again per packet.
8. Save the resolved mapping and qualification evidence. Only then proceed to substantial planning and dispatch.

If no inference service exists, explain the missing prerequisite and offer provider-specific setup instructions. Installing or starting it is a separate authorized operation. Do not silently replace an SSH workflow with a new server.

## 2. HTTP compatibility, remote machines and headless operation

“OpenAI-compatible” describes the worker API protocol here; it does not mean an OpenAI account or cloud model is required. Implementations differ in tool support, reasoning fields, errors and metadata. Compatibility must be checked at the actual request path.

Current Handoff workers use HTTP, not arbitrary remote shell generation. The connected architect path currently uses Claude CLI; a generic local architect replacement is not established by registering a worker. See the [main README](../README.md) for its current limitations.

| Situation | What to establish before work |
|---|---|
| Same computer | Correct port/model; server enabled; no conflict; execution interpreter distinct from inference runtime. |
| Headless remote server | Service survives logout, controller can reach it, logs are accessible, restart ownership is explicit. |
| SSH-only access | Whether a compatible API already listens remotely. A tunnel can transport that API; it does not convert a terminal generator into one. |
| Container, WSL or VM | `localhost` refers to the current network context. Verify port publication/routing and separate host paths from mounted paths. |
| Shared server | Other workloads consume capacity. Record permitted concurrency and queue behavior; available GPU count is not a concurrency contract. |

LM Studio documents GUI-independent operation through `llmster`, separate from its desktop UI. Its network-serving documentation warns that non-loopback binding exposes the API and recommends authentication. Do not casually change a bind address or firewall to fix a connection. [Headless](https://lmstudio.ai/docs/developer/core/headless), [network serving](https://lmstudio.ai/docs/developer/core/server/serve-on-network).

An authorized SSH local forward is one possible route to an existing remote loopback API. For example, `ssh -N -L 127.0.0.1:18080:127.0.0.1:8080 user@model-host` forwards a local port to the remote service. This is an illustrative operator command, not something Handoff starts today. Verify host identity, choose unused ports and account for tunnel lifetime during overnight work. Do not put passwords in commands. [OpenSSH forwarding](https://man.openbsd.org/ssh#L).

The MLX LM server documents an API similar to OpenAI chat, but also warns that its security checks are basic and it is not recommended for production. Do not equate protocol compatibility with safe public exposure. [MLX server documentation](https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/SERVER.md).

## 3. Hardware is not the serving configuration

CUDA, Metal, HIP/ROCm, Vulkan and SYCL are acceleration/runtime choices, not interchangeable labels for processor brands. The selected server build, driver, OS and model format determine what is usable.

| Hardware family | Questions, not automatic configuration |
|---|---|
| NVIDIA discrete GPUs | Does the installed serving build use CUDA or another supported backend? Does it actually use the intended cards rather than CPU fallback? |
| AMD discrete/integrated GPUs | Which OS, exact device and supported ROCm/HIP or Vulkan path? Are required device permissions available? |
| Intel CPU/GPU/NPU | Which device performs inference? GPU paths such as SYCL and vendor-specific NPU providers have different prerequisites; detecting an Intel device is insufficient. |
| Apple Silicon | Which MLX/Metal serving stack and model format? How much shared memory remains under the intended workload? |
| CPU-only | Does the chosen model complete representative checks within the intended latency/budget? Slow is not necessarily hung. |

llama.cpp documents separate CPU, Metal, CUDA, HIP, Vulkan and SYCL build paths; its Intel CPU BLAS support is not the same as its Intel GPU support. Consult the installed version's requirements, not a blanket instruction to install every toolkit. [Build documentation](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md).

Ollama's hardware documentation distinguishes NVIDIA, AMD and Metal support and provides backend/device-selection details. Verify the exact current support matrix rather than copying driver versions into permanent Handoff defaults. [Hardware support](https://docs.ollama.com/gpu).

### Multiple GPUs and multiple machines

Two 12 GB cards provide 24 GB of aggregate physical VRAM, not one unrestricted 24 GB allocation. Model placement, per-device overhead and communication still matter. Distinguish one model spread over cards, independent model replicas, and several requests sharing one model. GPUs in different computers need an explicitly supported distributed serving arrangement; Handoff scheduling separate endpoints does not combine their memory.

Ollama describes preferring a single GPU when a model fits and distributing it when it does not. Its documentation also ties parallel requests to additional context-memory demand. These are provider behaviors, not a universal formula for all servers. [Ollama FAQ](https://docs.ollama.com/faq#how-does-ollama-load-models-on-multiple-gpus).

### Operator-reported examples, not requirements or benchmarks

- A 48 GB Apple Silicon machine with a roughly 27B, 4-bit Qwen-family model.
- A machine with two 12 GB RTX 3060 GPUs serving through Ollama, with memory/cache choices tailored by its operator.

These describe the motivating environment, not verified minimums. The reported model label/version, exact quantization, context, device placement and cache/offload configuration require inventory before making capacity claims. No addresses, private paths or configuration files are reproduced here.

Model-weight quantization, KV-cache quantization and CPU/GPU offload are different settings. “Cache offloading” alone does not identify which is in use. Ask for the actual serving configuration rather than translating the phrase into a setting.

## 4. Headroom for useful multi-turn coding

Fitting model weights is only the beginning. Leave capacity for the serving runtime, conversation/KV cache, tool results, concurrent requests and other applications. A loaded model's usable context is not necessarily its advertised theoretical maximum. Ollama exposes running-model information including context length; use live/provider evidence where available. [Running models](https://docs.ollama.com/api/ps), [context guidance](https://docs.ollama.com/context-length).

For each request, plan approximately:

`instructions + retained history + current code/tool results + allowed new output + safety reserve <= usable request context`

Illustration, not a default: 64,000 usable tokens minus 18,000 of current input and a 2,000 reserve leaves at most 44,000 for this generation, BEFORE applying model/server/output-policy caps. That is a mathematical ceiling, not a recommendation to generate 44,000 tokens. Future turns will include relevant earlier output and feedback.

Do not divide context automatically by two or by the allowed number of turns. Do not spend the entire window on a first reply if the task needs correction. Full-file replacement, short tool actions and large tool results grow context differently. Tool-based editing can avoid repeatedly returning a whole file, but only when that edit mechanism is actually supported.

A useful qualification checks retained information across a correction and realistic input length, not only a short greeting. Stable generation at low load does not establish stability at full concurrency. Increase test load only within an agreed budget; never search automatically for the crash boundary.

Temperature is a sampling control, not a duration or context setting. Reasoning modes may consume output allowance differently across providers. Record requested settings and observed usage separately. A closed code fence does not establish that a response was not cut short; preserve provider stop/usage evidence when available.

Current behavior: chat calls default to `loop_config.worker_max_tokens=4096` and tool jobs to 1400 per model turn. These are harness defaults, not the server's context. Stop reason and usage are recorded for every worker turn, including tool loops that stop by raising. When turns are recorded as length-limited, the architect can `ADJUST` one assignment's `max_output_tokens` up to the lane's ceiling (context minus observed prompt minus a margin, capped by a worker's `max_output`). An operator value in `WORKER_OUTPUT_LIMITS` is **pinned**: it always wins and the architect is refused. The planner is told each dispatch lane's per-response allowance and ceiling, and plan review rejects a chat-mode outcome whose `est_lines` cannot fit the smallest lane's ceiling (a tools-mode outcome builds its file across turns, so whole-file size does not reject it). `call.fit_context` still uses a character-based estimate. No 64K minimum is established.

Sampling and thinking are per worker and set by the operator, in `WORKER_REQUEST_PROFILES` (keys `temperature, top_p, top_k, min_p, presence_penalty, repeat_penalty, think, reasoning_effort, thinking_budget`). They apply to worker work calls in chat and tools mode. A sampling key set to `None` is not sent, so the server's own value applies. Health canaries and qualification keep the fixed settings. The values actually sent are recorded with each turn's evidence. If no profile is set, the harness defaults stay: thinking off, temperature 0.6 for chat and 0.2 for tools.

## 5. Tools: verify the execution host, not just the schema

Current bundled `python_run` invokes the tool-service process's `sys.executable` in a job workspace, with a 60-second subprocess timeout. A package installed in another Python, another container or the model server is not thereby available. It can execute host code; the bundled service is not a security sandbox.

Preflight for a tools run now checks the host through the service's own `python_run`. It reports that interpreter's path and version and refuses the run when a module the plan's checks run as `python -m <module>` (plus any module in `TOOL_HOST_REQUIRED_MODULES`) is missing. `HANDOFF_ALLOW_MISSING_TOOL_DEPS=1` accepts the run anyway. Preflight also names the tool runtime (bundled or `FLEET_DISPATCH_DIR`) and refuses one that lacks the required capabilities, unless `HANDOFF_ALLOW_DEGRADED_TOOL_RUNTIME=1` is set.

Before coding work, establish interpreter identity, required imports/executables, writable authorized workspace, subprocess behavior, dependency network policy and how tests terminate. A long-running app needs a bounded test that starts it, waits for readiness and cleans up its process; do not leave test servers accumulating.

Test file read/write and check execution independently of the model, then test model-driven use. MCP Inspector's smoke-testing pattern separates initialization, tool listing and invocation; Handoff should adopt that distinction without claiming an MCP integration it does not yet ship. [Inspector checks](https://github.com/modelcontextprotocol/inspector/blob/main/docs/cli-smoke-testing.md).

OpenHands separates its action-execution runtime from the agent and uses runtime images to carry dependencies. The lesson is to make execution identity explicit. Handoff could support an isolated runtime later, but should not silently require Docker or imply its current host service has container isolation. [Runtime architecture](https://github.com/OpenHands/docs/blob/main/openhands/usage/architecture/runtime.mdx).

## 6. Storage, review and continuity are setup requirements too

Handoff's jobs/locks use Python `sqlite3`, not a separately administered SQL server. Verify that the exact controller Python supports it and that the intended storage can initialize, transact and reopen. Python documents SQLite as an embedded database without a separate server process. [Python SQLite](https://docs.python.org/3/library/sqlite3.html).

Every store now follows `FLEET_RUNS_DIR`: `jobs.sqlite3` (which also holds the cluster prefill lock), goals, integrate and memory. Each one can be overridden separately (`FLEET_JOBS_DB`, `FLEET_GOALS_DIR`, `FLEET_INTEGRATE_DIR`, `FLEET_MEMORY_DIR`). The conductor prints every resolved path after preflight. Two checkouts that share one fleet must resolve to the same jobs database, or they will not see each other's prefill lock. Check the printed paths before running more than one checkout. Do not move active databases or merge histories during setup.

The skeptic needs a configured model, accessible evidence and a working feedback path. Same-model review is allowed in some configurations but must not be described as independent. Deliberately disabling review is a reduced mode. Qualification should demonstrate an actual feedback/revision cycle, not simply that two model names exist.

Persistent memory also needs an integration check: record a synthetic lesson, restart, retrieve relevant evidence, and confirm it reaches the appropriate request. Existing files or populated SQL rows alone do not prove that connection. Keep private work out of public setup fixtures.

## 7. What can be done with current commands

Run from the selected Handoff checkout. Do not run every alternative below in sequence.

```text
python run/registry.py list
python run/registry.py connect --url http://specified-host:8080
python run/registry.py check http://specified-host:8080 --model exact-model-id
python run/registry.py add http://specified-host:8080 --name coding-worker --model exact-model-id
```

```text
python run/workprobe.py coding-worker
```

`workprobe` checks a registered lane for work compliance through the production call path. It asks for three things: code in a fence the harness extracts, a `files` write, and a file written in sections (write, then a guarded append quoting the file's sha256). The tool calls are executed on the configured tool service in a private `workprobe-*` workspace, so they pass only when the service performed them. If the service is unreachable the tool probes fail. The report is saved under the runs root and the registry is not changed.

Each `workprobe` run is saved as a readiness record outside the repository, beside the worker registry, keyed by `HANDOFF_ENVIRONMENT` (default `default`). The record includes the lane's identity: endpoint, model, context, reasoning style, request profile, output pin, tool runtime and probe version. Preflight reports each lane as `qualified`, `changed` (naming the part that changed), `failed` or `missing`, and gives the command to re-check only that lane. A new project is not a change. Readiness is advisory unless `HANDOFF_REQUIRE_READINESS=1`; liveness is still checked by the canary on every run. `python run/readiness.py show` lists the records.

`python run/scratch.py` lists tool-service scratch workspaces with a keep/prune verdict. It prunes only with `--apply`, and never touches workspaces of running, parked or accepted work.

`list` reads registration. `connect` discovers/qualifies and can register; `check` qualifies without registration; `add` qualifies and registers. Qualification can generate model output. `--no-register` is not a no-generation promise. Current default discovery only tries known localhost candidates (1234, 8080, 11434, 1337), not remote fleet discovery. Registration exposes `--ctx` and `--max-inflight`; supply established values, not guesses. The current API client appends versioned endpoint paths, so use the base format documented by Handoff rather than copying another client's URL blindly.

The current registry explicitly does not establish general authenticated-endpoint support. Do not remove server authentication to make registration work; treat missing client auth support as a compatibility gap. Roles are an explicit choice: `python run/registry.py roles --primary <name> --skeptic <name>`, or `--no-skeptic` for a deliberate run without independent review. They are saved in the registry, and a role set in the settings file overrides them. Registration alone never assigns a role. `conductor.py start` (guided or brief) checks that both roles resolve to workers that answer a live canary before any intake or planning tokens are spent. If they don't, it reports what answered and the exact commands to run, then stops. Defer mode makes no model call. These commands do not implement the complete proposed readiness journey.

## 8. Proposed reusable qualification and troubleshooting

Proposed sequence: inspect -> resolve missing facts -> validate execution/storage -> bounded generation and multi-turn tool/review task -> persist profile -> plan within demonstrated constraints. Prefer extending existing registry/configuration rather than introducing a second source of truth.

Save model/backend identity, relevant settings, card/tool versions, execution interpreter/dependencies, resolved state paths, check versions/results and timestamps. Credentials stay in appropriate secret storage, not reports. Reuse unchanged qualification on a second project; recheck changed capabilities and current liveness. A project needing a new library may need that execution check without requalifying every model. Permission to use an environment is not permission for every future project path.

| Symptom | Investigate first |
|---|---|
| Chat works, Handoff cannot connect | API service enabled, controller-visible URL, bind/port/tunnel/auth, correct endpoint suffix. |
| Model list works, generation fails | Exact generation model, request dialect, loading/queue state, actual error response. |
| Empty answer | Reasoning/visible-content handling, output allowance and termination evidence; not automatic incapability. |
| Announces a Read but writes nothing | Tool authority/support and whether prior code is actually in the request or accessible workspace. |
| Import or command not found | Tool host interpreter/environment and installed project dependencies. |
| Later turns forget fixes | Actual retained messages, trimming, restart carry and essential-material availability. |
| Slowdown or memory failure under load | Real context depth, placement/offload, concurrent clients and available memory; do not restart blindly. |
| Resume loses jobs or lessons | Resolved state paths, checkout/environment selection, permissions and initialization. |
| Tool succeeds, project fails | Separate self-check evidence from acceptance and assembled-project tests. |

## 9. Research patterns to borrow, with limits

- **LexiPanel:** guided checks with an inspect-only mode, dependency setup and preservation of customized launch scripts. Its installer still assumes a particular service account/layout; do not copy private-machine paths into a generic wizard. [Installer](https://github.com/W61k3r/LexiPanel/blob/main/install-interactive.sh).
- **LexiPanel agent readiness:** reads live arguments/properties and generates model/MCP configuration. Template substring and bind-address checks are heuristics, not executed tool calls or proof of remote connectivity. Borrow the evidence-to-configuration flow, not its agent-specific fixed context threshold. [Hermes adapter](https://github.com/W61k3r/LexiPanel/blob/main/hermes.py).
- **Aider:** separates model metadata, behavioral settings and request overrides with precedence. It explicitly does not enforce token limits; use its configuration design, not as proof of solved budgeting. [Model settings](https://aider.chat/docs/config/adv-model-settings.html).
- **Lemonade:** publishes standard-compatible APIs alongside provider-specific management/discovery endpoints, including version-local API documentation. This supports backend adapters rather than assuming one universal capability response. API availability is not proof that Handoff has qualified that backend. [API design](https://lemonade-server.ai/docs/api/).

Source caveat: upstream pages and versions change. The Ollama hardware page and older portions of its FAQ currently contain different ROCm-era statements; use the installed build and current platform support documentation, not a synthesized universal driver requirement. Hardware support listings are not Handoff qualification results.

## 10. Work remaining for automated environment readiness

- Exercise the existing instructions from a clean external project with no private settings imported.
- Implement/verify the complete environment mapping and selective qualification under #25/#28.
- Capacity is connected to planning (lane budgets, OVERSIZED review) and to recovery (architect ADJUST within ceilings). Still open: failing explicitly when essential input cannot fit, and a real model-context figure for lanes whose configured `ctx` is actually a generation cap.
- Complete structured failure classification and architect routing (#27). Response evidence is preserved across chat and bundled tools; the carry-access correction is implemented (#26).
- Add backend/version-specific qualification records for Windows, Linux and Apple Silicon; include headless, remote and shared-GPU cases without claiming every combination works.
- Define installation, network access, service lifecycle and dependency-remediation permissions up front for unattended use.
- Demonstrate synthetic persistent-state restart/retrieval plus worker/tool/skeptic/integration behavior.
- The guide is linked before the main README's first project command. Examples omit private addresses and paths; review any added historical cards separately. Do not publish private tests or machine configuration.

This guide combines current manual setup with explicitly proposed automation. It does not authorize environment changes and does not close #28.
