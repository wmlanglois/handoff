"""Allowlisted generation diagnostics, not a copy of prompts or server responses."""


def worker_output_limit(worker, fallback):
    """Operator-owned output allowance, shared by worker chat and tool execution.

    A configured value is PINNED: it wins over everything, including an architect ADJUST. An absent
    profile uses `fallback`, which is the assignment's own budget (raised by ADJUST from recorded
    evidence) or the harness default.
    """
    from fleet import setting
    profiles = setting("WORKER_OUTPUT_LIMITS", {})
    if not isinstance(profiles, dict):
        raise ValueError("WORKER_OUTPUT_LIMITS must be a mapping of worker names to token limits")
    value = profiles.get(worker, fallback)
    if type(value) is not int or value <= 0:
        raise ValueError("worker output limit must be a positive integer: " + str(worker))
    return value


def pinned_output_limit(worker):
    """The operator's pinned output limit for `worker`, or None when the limit is not pinned."""
    from fleet import setting
    profiles = setting("WORKER_OUTPUT_LIMITS", {})
    value = profiles.get(worker) if isinstance(profiles, dict) else None
    return value if type(value) is int and value > 0 else None


#: Assumed prompt size when the server has not reported one, and headroom kept beyond the prompt.
DEFAULT_PROMPT_TOKENS = 7168
PROMPT_MARGIN = 1024


def output_ceiling(worker, observed_prompt_tokens=None):
    """The largest output limit a packet on `worker` can safely request: its context minus the
    prompt (the largest the server actually reported, else a conservative default) and a margin.
    Never above a known server generation cap (`max_output` on the worker record). 0 if unknown."""
    import fleet
    w = fleet.WORKERS.get(worker) or {}
    try:
        ctx = int(w.get("ctx") or 0)
    except (TypeError, ValueError):
        ctx = 0
    prompt = observed_prompt_tokens if isinstance(observed_prompt_tokens, int) and observed_prompt_tokens > 0         else DEFAULT_PROMPT_TOKENS
    ceiling = max(0, ctx - prompt - PROMPT_MARGIN)
    cap = w.get("max_output")
    if isinstance(cap, int) and cap > 0:
        ceiling = min(ceiling, cap)
    return ceiling


def response_evidence(response, request):
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = response.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    counts = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(name)
        counts[name] = value if type(value) is int and value >= 0 else None
    details = usage.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}
    value = details.get("reasoning_tokens")
    counts["reasoning_tokens"] = value if type(value) is int and value >= 0 else None
    reason = choice.get("finish_reason")
    # Do not persist arbitrary provider strings: they can contain request material.
    reason = reason if isinstance(reason, str) and reason in (
        "stop", "length", "tool_calls", "function_call", "content_filter") else None
    return {
        "finish_reason": reason,
        "usage": counts,
        "requested_max_tokens": request.get("max_tokens"),
        "requested_max_completion_tokens": request.get("max_completion_tokens"),
        "temperature": request.get("temperature"),
        "response_kind": ("length_limited" if reason == "length" else
                          "tool_calls" if message.get("tool_calls") else
                          "empty_visible_content" if not message.get("content") else "content"),
    }
