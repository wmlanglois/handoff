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


#: Harness default output allowance per model turn in tools mode (run/verified.py, tool jobs).
TOOL_TURN_DEFAULT = 1400
#: Rough tokens per line of delivered Python, for sizing packets against output budgets.
TOKENS_PER_LINE = 12


def lane_budgets(workers, tool_mode):
    """What a packet on each lane can emit: the default per-response allowance (pinned or not) and
    the ceiling an architect ADJUST can raise it to."""
    import fleet
    try:
        from loop_config import LOOP
        chat_default = int(LOOP.worker_max_tokens)
    except Exception:
        chat_default = 4096
    rows = []
    for w in workers:
        pin = pinned_output_limit(w)
        default = TOOL_TURN_DEFAULT if tool_mode == "tools" else chat_default
        rows.append({"worker": w, "ctx": int((fleet.WORKERS.get(w) or {}).get("ctx") or 0),
                     "per_response": pin or default, "pinned": bool(pin),
                     "ceiling": pin or output_ceiling(w)})
    return rows


def lane_budget_text(rows, tool_mode):
    """Planner-facing statement of the run's lane budgets."""
    if not rows:
        return ""
    lines = ["LANE BUDGETS (the workers this run dispatches to; a packet may land on any of them):"]
    for r in rows:
        lines.append("  {0}: context {1} tokens; writes at most {2} output tokens per {3} ({4}); "
                     "ceiling {5}".format(
                         r["worker"], r["ctx"] or "unknown", r["per_response"],
                         "model turn" if tool_mode == "tools" else "response",
                         "PINNED by the operator" if r["pinned"] else "default; the architect may raise it on evidence",
                         r["ceiling"] or "unknown"))
    smallest = min(r["per_response"] for r in rows)
    lines.append("Size outcomes to these budgets (about {0} tokens per line of Python). Set `est_lines` on "
                 "every code outcome.".format(TOKENS_PER_LINE))
    if tool_mode == "tools":
        lines.append("In tools mode a file is written in sections: each files write/append must fit in "
                     "{0} tokens (~{1} lines), so write a small first section and append the rest.".format(
                         smallest, smallest // TOKENS_PER_LINE))
    else:
        lines.append("In chat mode the whole artifact is ONE response: keep each outcome under ~{0} lines "
                     "or split it into outcomes linked by `needs`.".format(smallest // TOKENS_PER_LINE))
    return "\n".join(lines)


#: Conservative generation speed assumed when a worker declares none (`min_tokens_per_s`). A request
#: timeout must cover the output it asks for: a fixed timeout turns every raised output limit into a
#: timeout once the limit outgrows it (observed 2026-09-25: ADJUST to 16000 tokens, 300 s timeout).
DEFAULT_MIN_TOKENS_PER_S = 20
REQUEST_OVERHEAD_S = 60


def request_timeout(worker, max_tokens, floor=300):
    """Seconds to wait for one generation of up to `max_tokens` on `worker`."""
    import fleet
    rate = (fleet.WORKERS.get(worker) or {}).get("min_tokens_per_s") or DEFAULT_MIN_TOKENS_PER_S
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        rate = DEFAULT_MIN_TOKENS_PER_S
    need = REQUEST_OVERHEAD_S + int(max_tokens or 0) / max(rate, 1.0)
    return int(max(floor, need))


#: Sampling keys an operator may set per worker. A key set to None is NOT SENT, so the server's own
#: configured value applies (e.g. a server launched with the model card's sampling).
SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "repeat_penalty")
_PROFILE_KEYS = set(SAMPLING_KEYS) | {"think", "reasoning_effort", "thinking_budget"}


def worker_request_profile(worker):
    """Operator-owned request settings for WORKER calls (WORKER_REQUEST_PROFILES[worker]); {} when none,
    which keeps the harness defaults (thinking off, temperature 0.6 chat / 0.2 tools). Health canaries
    and qualification do not use it: thinking plus a tiny canary budget returns empty content and would
    mark a healthy lane down."""
    from fleet import setting
    profiles = setting("WORKER_REQUEST_PROFILES", {})
    if not isinstance(profiles, dict):
        raise ValueError("WORKER_REQUEST_PROFILES must map worker names to settings")
    prof = profiles.get(worker) or {}
    if not isinstance(prof, dict):
        raise ValueError("request profile for {0} must be a mapping".format(worker))
    unknown = set(prof) - _PROFILE_KEYS
    if unknown:
        raise ValueError("request profile for {0} has unknown keys {1}; allowed {2}".format(
            worker, sorted(unknown), sorted(_PROFILE_KEYS)))
    return dict(prof)


def apply_request_profile(body, profile, reasoning_style="none"):
    """Merge a request profile into an OpenAI-style body (in place; also returned)."""
    for key in SAMPLING_KEYS:
        if key in profile:
            if profile[key] is None:
                body.pop(key, None)
            else:
                body[key] = profile[key]
    think = bool((body.get("chat_template_kwargs") or {}).get("enable_thinking"))
    if "think" in profile:
        think = bool(profile["think"])
        body.setdefault("chat_template_kwargs", {})["enable_thinking"] = think
        if think:
            body.pop("reasoning_budget", None)      # the budget_field style zeroes it only for think-off
    # reasoning_effort is a chat-template setting (Qwen3.8: low / medium / xhigh). Verified live on a
    # llama.cpp lane whose reasoning_style is "none" (2026-09-25), so it is applied whenever thinking
    # is on, not only for the template_kwargs dialect.
    if think and profile.get("reasoning_effort"):
        body.setdefault("chat_template_kwargs", {})["reasoning_effort"] = profile["reasoning_effort"]
    if think and reasoning_style == "budget_field" and type(profile.get("thinking_budget")) is int:
        body["reasoning_budget"] = profile["thinking_budget"]
    return body


def thinking_default(reasoning_style="none"):
    """The request fragment that turns model thinking OFF, as call.body_for does for chat (#33).
    Without it the server's own default applies, and Qwen3.x templates default to thinking ON."""
    frag = {"chat_template_kwargs": {"enable_thinking": False}}
    if reasoning_style == "budget_field":
        frag["reasoning_budget"] = 0
    return frag


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
        "sampling": {k: request.get(k) for k in SAMPLING_KEYS if k in request},
        "thinking": (request.get("chat_template_kwargs") or {}).get("enable_thinking"),
        "reasoning_effort": (request.get("chat_template_kwargs") or {}).get("reasoning_effort"),
        # Size only, never the text: hidden reasoning that consumes the output budget is otherwise
        # invisible in the evidence (#33: 132K chars of reasoning, 0 of content, read as "length").
        "reasoning_chars": len(message.get("reasoning_content") or "") if isinstance(
            message.get("reasoning_content"), str) else None,
        "response_kind": ("reasoning_exhausted" if reason == "length" and not message.get("content")
                          and not message.get("tool_calls") and message.get("reasoning_content") else
                          "length_limited" if reason == "length" else
                          "tool_calls" if message.get("tool_calls") else
                          "empty_visible_content" if not message.get("content") else "content"),
    }
