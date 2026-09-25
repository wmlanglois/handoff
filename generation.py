"""Allowlisted generation diagnostics, not a copy of prompts or server responses."""


def worker_output_limit(worker, fallback):
    """Operator-owned output allowance, shared by worker chat and tool execution.

    No model or architect may change this setting through a packet. An absent profile
    retains the existing limit; a configured value is a pinned value, not permission to tune.
    """
    from fleet import setting
    profiles = setting("WORKER_OUTPUT_LIMITS", {})
    if not isinstance(profiles, dict):
        raise ValueError("WORKER_OUTPUT_LIMITS must be a mapping of worker names to token limits")
    value = profiles.get(worker, fallback)
    if type(value) is not int or value <= 0:
        raise ValueError("worker output limit must be a positive integer: " + str(worker))
    return value


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
