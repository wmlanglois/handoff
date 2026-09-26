"""Preview LiteLLM aliases, then explicitly qualify one in the existing worker registry."""
import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fleet
import registry
from endpoint_http import gateway_url, open_request, key_value

MAX_BYTES = 2_000_000


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON keys")
        result[key] = value
    return result


def read_config(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("config exceeds the 2 MB import limit")
    try:
        return json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError):
        pass
    try:
        import yaml
    except ImportError:
        raise ValueError("YAML preview needs optional PyYAML; install it explicitly or supply JSON") from None

    class UniqueLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            result = {}
            for k, v in node.value:
                key = self.construct_object(k, deep=deep)
                if not isinstance(key, str) or key in result:
                    raise ValueError("config contains duplicate or non-string keys")
                result[key] = self.construct_object(v, deep=deep)
            return result

    try:
        # Disallow aliases/tags; never run constructors, interpolate env or follow includes.
        if any(isinstance(t, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken, yaml.tokens.TagToken))
               for t in yaml.scan(raw)):
            raise ValueError("YAML anchors, aliases and explicit tags are not supported; use a plain export")
        return yaml.load(raw, Loader=UniqueLoader)
    except Exception:
        raise ValueError("invalid or unsupported YAML; use a plain model_list export (details withheld)") from None


def discover(config=None, gateway=None, api_key_env=None):
    """Return only aliases/counts. Provider credentials and raw metadata never enter the preview."""
    if config:
        doc = read_config(config)
        rows = doc.get("model_list") if isinstance(doc, dict) else None
        key = "model_name"
    else:
        if not gateway:
            raise ValueError("supply --config or --gateway")
        req = urllib.request.Request(gateway_url(gateway) + "/v1/models",
                                     headers={"Accept": "application/json"})
        try:
            with open_request(req, 10, api_key_env) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("oversized model list")
            doc = json.loads(raw)
        except Exception:
            raise ValueError("gateway discovery failed; check URL, credential environment and /v1/models") from None
        rows = doc.get("data") if isinstance(doc, dict) else None
        key = "id"
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("expected a bounded model_list (config) or data (gateway) array")
    counts = {}
    for row in rows:
        alias = row.get(key) if isinstance(row, dict) else None
        if not isinstance(alias, str) or not alias.strip() or len(alias) > 256 or any(ord(c) < 32 for c in alias):
            raise ValueError("model entry has an invalid alias")
        counts[alias] = counts.get(alias, 0) + 1
    return [{"alias": alias, "entries": count, "selectable": "*" not in alias,
             "status": "declared, not qualified"} for alias, count in sorted(counts.items())]


def import_alias(aliases, *, gateway, model, name, ctx, api_key_env=None, path=None):
    if not any(r["alias"] == model and r["selectable"] for r in aliases):
        raise ValueError("select one concrete advertised alias with --model; wildcards cannot be imported")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name) or name in fleet._TOPOLOGY:
        raise ValueError("choose a non-reserved worker name (letters, digits, dot, underscore, hyphen)")
    if type(ctx) is not int or not 512 <= ctx <= 2_000_000:
        raise ValueError("--ctx must be the operator-confirmed usable context, 512..2000000 tokens")
    url = gateway_url(gateway)
    key_value(api_key_env)  # validate before any generation; never store the resolved value
    target = Path(path or fleet.registry_path())
    record = fleet.make_worker(url, model, ctx=ctx, api_key_env=api_key_env)
    # Keep the existing registry lock through qualification: no race that spends then overwrites.
    with registry._registry_lock(target, stale_s=300):
        workers = registry.entries(target)
        if name in workers:
            raise ValueError("worker name already registered; choose another name (no implicit overwrite)")
        # Two bounded ordinary chat calls, no model scan, retries or provider property guesses.
        for budget in (128, 48):
            text, note, fatal = registry._one_chat(record, budget, 30)
            if note or fatal or not (text or "").strip():
                raise ValueError("selected alias failed generation qualification; registry unchanged")
        record.update({"registered_at": registry._now(),
                       "import_source": "litellm-gateway",
                       "qualification": {"model": model, "budgets": [128, 48],
                                         "status": "generation-only", "context_source": "operator"}})
        workers[name] = record
        registry._write_doc(target, workers)
        if registry.entries(target).get(name) != record:
            raise RuntimeError("registry readback mismatch")
    registry._reload_if_live(target)
    return {"name": name, "model": model, "status": "registered; roles unchanged; tools not qualified"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="explicit LiteLLM YAML/JSON file; read-only and offline")
    parser.add_argument("--gateway", help="gateway base URL; /v1 suffix is accepted")
    parser.add_argument("--api-key-env", help="environment variable containing the gateway key; never the key itself")
    parser.add_argument("--apply", action="store_true", help="authorize up to two generation calls and register one alias")
    parser.add_argument("--model", help="exact public alias to import")
    parser.add_argument("--name", help="new Handoff worker name")
    parser.add_argument("--ctx", type=int, help="confirmed usable context tokens, not max output")
    args = parser.parse_args(argv)
    try:
        if args.gateway:
            gateway_url(args.gateway)
        if args.apply and not all((args.gateway, args.model, args.name, args.ctx)):
            raise ValueError("--apply requires --gateway, --model, --name and --ctx")
        aliases = discover(args.config, args.gateway, args.api_key_env)
        if args.apply:
            result = import_alias(aliases, gateway=args.gateway, model=args.model, name=args.name,
                                  ctx=args.ctx, api_key_env=args.api_key_env)
        else:
            result = {"models": aliases, "note": "Preview only: no generations or registry writes. "
                      "Aliases may load-balance multiple deployments; config may omit database models. "
                      "Provider settings and credentials are not imported."}
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        # Do not print input config, provider responses, credentials or a traceback.
        print("LiteLLM import: " + (str(exc) if isinstance(exc, ValueError) else
                                    "operation failed; check local file/registry access"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
