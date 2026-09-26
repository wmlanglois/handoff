# Phase 2: environment inspection and a workbench over Handoff

## September 25 follow-up: reuse LiteLLM configuration

The optional [LiteLLM import adapter](LITELLM-IMPORT.md) implements a narrow part of
environment reuse: explicit read-only alias discovery followed by qualified registry
import. Existing gateway routing remains authoritative; no SDK, second scheduler or
server restructuring is required. The design follows LiteLLM's documented alias and
database/file distinction, with references in the guide. Private offline HTTP tests
cover connection paths; a real LiteLLM deployment has not been tested here. Reusable
readiness records are now implemented; explicit roles can persist in the registry.
Automatic role choice and broader environment management remain separate work.
Configuration discovery must not be confused with tool qualification.

September 25, 2026 amendment. This is a design proposal, not a shipped panel or a replacement
for earlier project plans. Track environment qualification in [#28](https://github.com/wmlanglois/handoff/issues/28)
and saved profiles in [#25](https://github.com/wmlanglois/handoff/issues/25).

## Why this belongs in the product

A person should not spend a project run discovering that their model cannot access its prior
file, its tool host lacks Python dependencies, or the request reserves too little output.
Even an experienced local-model user may never have tested multi-turn tool use, skeptical
review and restart continuity through Handoff. First-use questions and a reusable verified
profile should establish these facts before substantial work starts.

## Two separate responsibilities

- The chat/CLI workbench is the interaction layer: project ideas, authorization, progress,
  questions, logs and evidence. It uses Handoff's existing controller and ledger, not a
  second scheduler.
- The environment inspector establishes what the configured services can actually do.
  It distinguishes controller, model hosts and execution hosts. A GPU inventory is helpful
  context, not proof of model capacity, working tools or available concurrency.

[LexiPanel](https://github.com/W61k3r/LexiPanel) is a useful reference for inspection,
configuration and an optional management surface. Its
[installer](https://github.com/W61k3r/LexiPanel/blob/main/install-interactive.sh) and
[Hermes integration](https://github.com/W61k3r/LexiPanel/blob/main/hermes.py) illustrate checks
and generated configuration. They do not establish that Handoff supports the same platforms
or that heuristic readiness checks replace an executed worker/tool task.

## Proposed delivery order

1. Finish the manual technical environment guide and existing diagnostic/carry fixes.
2. Save a versioned qualification profile using existing registry/configuration ownership.
   Reuse unchanged evidence on the next project; recheck changed components and liveness.
3. Expose a read-only environment board: declared versus observed backend/model/context,
   actual request settings, tool execution interpreter, state paths, health and active jobs.
4. Add backend-specific inspection adapters. Unsupported or inaccessible properties say
   unknown, not zero. A remote server must be inspected from the controller's viewpoint.
5. Add headroom estimates with explicit assumptions and, separately, opt-in measured trials.
6. Add optional setup/tuning actions under separate installation, network and service-lifecycle
   authorization. Never retune an established server simply because a project connected.

## Headroom calculator constraints

Display configured total context, estimated rendered input, reserved output, observed usage
when available, and margin. Do not divide 64K into fixed per-turn halves or infer per-slot
capacity from the number of parallel clients. Multi-turn histories share the request window.
Reasoning output can consume the generation allowance without producing visible code.

Memory estimates must include model/quantization, KV cache, concurrency, runtime overhead,
offload and backend placement. Two 12GB GPUs are not automatically one interchangeable 24GB
pool; Apple unified memory is not equivalent to dedicated VRAM. A calculator offers estimates,
not guaranteed settings. Preserve the user's known-good configuration.

## Completion evidence

A clean external project can reuse a saved environment without repeating expensive model
checks; a changed model/tool/runtime invalidates only relevant evidence. A synthetic worker
task reads material, runs a check, revises, receives skeptic feedback and resumes after a
restart with the same resolved state. Missing dependencies, unsupported auth and inaccessible
carry produce actionable failures before a large project wave. Nothing requires a public
copy of local addresses, credentials, tests, transcripts or private project cards.
