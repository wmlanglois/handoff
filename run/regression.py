"""Golden-set regression with SEMANTIC DIFFING.

Re-generating a corpus of returns with a 27B on 3060s overnight is far too slow to do wholesale. So we
only spend the LLM where it can matter: fingerprint each form's deterministic inputs + the rule/source
text it depends on; if the fingerprint is unchanged since the last golden run, DON'T re-generate -- just
re-run the cheap deterministic oracle against the cached output. Only forms whose inputs or rules
actually changed get queued for LLM regeneration.

    plan = plan_regression(items, cache)      # decide regenerate vs verify_cached per item
    # run the 'regenerate' items through the fleet; run the oracle on 'verify_cached' items locally
"""
import hashlib
import json


def fingerprint(inputs, rule_source: str) -> str:
    """Deterministic hash of what the output depends on: the inputs and the rule/source text.
    Order-independent for the inputs mapping; sensitive to any rule-text change."""
    blob = json.dumps(inputs, sort_keys=True, separators=(",", ":")) + "\x00" + (rule_source or "")
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def plan_regression(items, cache, *, log=print):
    """items: [{id, inputs, rule_source}]; cache: {id: last_fingerprint}.
    Returns [{id, fingerprint, action}] where action is 'regenerate' (changed/new -> needs the LLM) or
    'verify_cached' (unchanged -> just re-run the deterministic oracle on the cached output)."""
    plan = []
    changed = 0
    for it in items:
        fp = fingerprint(it["inputs"], it.get("rule_source", ""))
        prev = cache.get(it["id"])
        action = "verify_cached" if prev == fp else "regenerate"
        if action == "regenerate":
            changed += 1
        plan.append({"id": it["id"], "fingerprint": fp, "action": action})
    log(f"[regression] {len(items)} forms: {changed} changed -> regenerate (LLM), "
        f"{len(items) - changed} unchanged -> verify_cached (oracle only)")
    return plan


def update_cache(cache, plan):
    """After a successful run, advance the cache to the new fingerprints."""
    for row in plan:
        cache[row["id"]] = row["fingerprint"]
    return cache
