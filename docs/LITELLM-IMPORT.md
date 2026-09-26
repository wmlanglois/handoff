# Bring an existing LiteLLM environment

Handoff can preview an existing config or running gateway, then qualify and register
one selected model alias. No LiteLLM SDK/server installation is required in Handoff.
Keep your servers as they are. This does not change LiteLLM, load models, scan your
network, assign roles or copy provider credentials/settings.

## Preview without generation

Read an explicitly selected local file, offline:

```text
python run/litellm_import.py --config C:\path\to\config.yaml
```

YAML needs optional PyYAML in this Python environment; the command reports a missing
parser without installing it. JSON with the same `model_list` structure needs no
extra dependency. YAML tags, anchors, aliases, includes and duplicate keys are not
supported: use a plain export. Environment references are not evaluated. Preview
shows only aliases, entry counts and selectability, not raw configuration.

Or read the running gateway's `/v1/models` list:

```text
python run/litellm_import.py --gateway https://gateway.example --api-key-env LITELLM_API_KEY
```

Set that environment variable privately first; pass its **name**, never its value.
Omit the option for an unauthenticated gateway. Use HTTPS for remote credentials.
Discovery and authenticated calls refuse redirects. URLs containing credentials,
query strings or fragments are refused by import.
A trailing `/v1` is accepted; any deployment path prefix is preserved.
Preview makes no generations or registry writes. Gateway preview performs one HTTP read.

File config can omit database-managed models. Duplicate aliases are grouped, not
turned into separate workers: an alias can load-balance multiple deployments and is
not necessarily one physical machine. Wildcard aliases cannot be imported.

## Explicitly qualify and import one alias

```text
python run/litellm_import.py --gateway https://gateway.example --api-key-env LITELLM_API_KEY --model coding --name my-coder --ctx 32768 --apply
```

The example context is not a recommendation. Supply the confirmed usable **total
context**, not an output cap. Use a conservative common value for heterogeneous
deployments, or create a dedicated alias in your gateway yourself.

`--apply` authorizes at most two ordinary Handoff chat calls: 128 and 48 requested
output tokens, each with a 30-second timeout. It never tries another model. Gateway
internal retries/fallbacks can add work independently; check its configuration.
With both `--config` and `--gateway`, candidates come from the file but qualification
still calls the gateway alias, not the provider endpoint in the file.

The existing registry is used (`FLEET_REGISTRY` or the per-user default). Existing
and reserved worker names are refused; there is no overwrite flag. Failed
qualification leaves registry content unchanged. Success uses the registry's lock
and atomic replacement, then reads the record back. Only the key's environment
variable name is saved. Every process using this worker must inherit that variable.
No response sample is saved by the importer. Registry URLs/aliases remain private.

## Select roles and check the actual workload

```text
python run/registry.py list
python run/registry.py health my-coder
```

Select `PRIMARY_WORKER` and optionally `SKEPTIC_WORKER` in your normal private
settings file. Import does not change roles or replace the connected architect CLI.
Two names for one alias are not independent review or independent capacity. Avoid
duplicate workers for one deployment: per-worker concurrency limits do not coordinate
different aliases sharing a backend.

Generation qualification does not prove coding/tool readiness. Use the authorized
workload checks and normal preflight. Authentication works through ordinary chat,
streaming chat, preflight and the bundled tool runtime. Authenticated gateway workers
are refused with an external tool runtime rather than silently dropping the key.
The gateway credential is separate from the tool-service credential.

Handoff request defaults/profiles still apply. Provider sampling, reasoning, context,
routing and concurrency settings are **not** translated by import. Review the
[environment guide](ENVIRONMENT-REQUIREMENTS.md) before dispatch. Unsupported request
options may fail qualification; do not disable gateway policy to make it pass.

## Design references and validation boundary

- [LiteLLM config](https://docs.litellm.ai/docs/proxy/configs): client `model_name`
  and provider `litellm_params.model` are different identities.
- [Model management](https://docs.litellm.ai/docs/proxy/model_management): file and
  database definitions can coexist; a static file is not necessarily the live inventory.
- [Issue #31](https://github.com/wmlanglois/handoff/issues/31) tracks this adapter.

Private offline tests use a fake authenticated HTTP gateway through discovery,
registry reload, chat, preflight and the bundled tool loop, plus config/conflict/
failure/redirect cases. This is not live LiteLLM certification or model-quality
evidence. No existing fleet service is modified or started by these commands.
