"""Portable OpenAI-compatible tool loop used by run/tooljob.py.

The dispatcher checkpoint records assistant tool calls before execution and each tool result
afterward. Retrying a call uses the same service call_id, whose persisted response prevents a
completed write from being repeated after a process interruption.
"""
import hashlib
import json
from pathlib import Path
import urllib.request
from generation import TOOL_TURN_DEFAULT, response_evidence

#: What this runtime implements. tooljob records it per job and preflight refuses a tools-mode run
#: on a runtime missing the required ones, so a less capable runtime is never used silently.
CAPABILITIES = ("length_recovery", "generation_evidence", "incremental_files", "context_check",
                "request_profile")


def _post(url, payload, token=None, timeout=120):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _get(url, token):
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


#: Messages at the end of the conversation that are never compacted (the current working set).
COMPACT_KEEP_TAIL = 6
#: A body shorter than this is left alone: replacing it would save nothing worth a reread.
COMPACT_MIN_CHARS = 400


def _estimate(messages, tools):
    return (len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False)) + 3) // 4


def compact_view(messages, tools, budget_tokens):
    """(view, replaced) -- the conversation to SEND when the full one would not fit `budget_tokens`.

    The full conversation stays in the checkpoint; only the request view shrinks (#30). Oldest first,
    file bodies that already reached the workspace are replaced with a reference to reread them:
    the `content` argument of an earlier files write/append/edit, and the `content` of an earlier
    files read result. The first message (the task and its requirements) and the last
    COMPACT_KEEP_TAIL messages are never touched, and no message is removed, so every assistant
    tool call keeps its tool result. If the view still does not fit, the caller refuses as before."""
    if _estimate(messages, tools) <= budget_tokens:
        return messages, 0
    view = [dict(m) for m in messages]
    replaced = 0
    last = max(1, len(view) - COMPACT_KEEP_TAIL)
    for i in range(1, last):
        m = view[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                fn = dict(tc.get("function") or {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = None
                if isinstance(args, dict) and isinstance(args.get("content"), str) \
                        and len(args["content"]) >= COMPACT_MIN_CHARS:
                    n = len(args["content"])
                    args["content"] = ("[{0} chars sent to {1} in an earlier turn; omitted to fit context. "
                                       "Reread the file with files read (start_line/max_lines) if you "
                                       "need it.]".format(n, args.get("path") or "the file"))
                    fn["arguments"] = json.dumps(args)
                    replaced += 1
                calls.append(dict(tc, function=fn))
            m["tool_calls"] = calls
        elif m.get("role") == "tool":
            try:
                res = json.loads(m.get("content") or "")
            except (TypeError, ValueError):
                res = None
            inner = res.get("result") if isinstance(res, dict) else None
            if isinstance(inner, dict) and isinstance(inner.get("content"), str) \
                    and len(inner["content"]) >= COMPACT_MIN_CHARS:
                n = len(inner["content"])
                inner = dict(inner, content=("[{0} chars of {1} (sha256 {2}) read in an earlier turn; omitted "
                                             "to fit context. Reread the lines you need.]".format(
                                                 n, inner.get("path") or "the file",
                                                 str(inner.get("sha256") or "?")[:12])))
                m["content"] = json.dumps(dict(res, result=inner))
                replaced += 1
        if _estimate(view, tools) <= budget_tokens:
            break
    return view, replaced


def _call_id(execution_id, tool_id):
    return "tool-" + hashlib.sha256(json.dumps([execution_id, tool_id]).encode()).hexdigest()


def run(dispatcher, execution_id, job, worker):
    cfg = dispatcher.config["tools"]
    token = Path(cfg["token_file"]).read_text(encoding="utf-8").strip()
    base = cfg["url"].rstrip("/")
    tools = _get(base + "/tools", token).get("tools") or []
    if not tools:
        raise RuntimeError("tool service exposes no tools")
    saved = dispatcher.checkpoint(execution_id)
    if saved:
        messages = saved["messages"]
        rounds = saved["rounds"]
        generations = saved.get("generations", [])
        incomplete = saved.get("incomplete", [])
        truncations = saved.get("truncations", 0)
        compactions = saved.get("compactions", [])
        coaching = saved.get("coaching")
        if coaching and coaching.get("status") == "started":
            coaching = {"status": "interrupted", "reason": "review did not checkpoint a result"}
    else:
        messages = [{"role": "user", "content": job["prompt"]}]
        rounds = 0
        generations = []
        incomplete = []
        truncations = 0
        compactions = []
        coaching = None

    def save():
        dispatcher.checkpoint(execution_id, {"messages": messages, "rounds": rounds,
                                             "generations": generations, "incomplete": incomplete,
                                             "truncations": truncations, "coaching": coaching,
                                             "compactions": compactions})

    save()
    maximum = int(job.get("max_rounds", 16))
    while rounds < maximum:
        pending = None
        # On a crash after one of several tool results, resume the remaining calls instead of
        # asking the model for a new turn with an incomplete tool-call group.
        for previous in reversed(messages):
            if previous.get("role") == "assistant":
                if previous.get("tool_calls"):
                    seen = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
                    if any(t.get("id") not in seen for t in previous["tool_calls"]):
                        pending = previous
                break
        if pending is not None:
            pass
        else:
            if incomplete and incomplete[-1].get("reason") == "content_filter":
                raise RuntimeError("tool content-filter stop; checkpoint preserved; no calls executed")
            if truncations >= 2:
                raise RuntimeError("tool output capacity exhausted after two length stops; "
                                   "checkpoint preserved; no incomplete calls executed")
            request = {
                "model": worker["model"], "messages": messages, "tools": tools,
                "tool_choice": "auto", "max_tokens": int(job.get("max_tokens") or TOOL_TURN_DEFAULT),
                "temperature": 0.2, "stream": False,
                # #33: thinking OFF unless the harness's request profile says otherwise. Without this
                # the server default applied -- thinking ON for Qwen3.x -- and hidden reasoning
                # consumed the output budget and the context window.
                "chat_template_kwargs": {"enable_thinking": False},
            }
            # Operator request profile (sampling/thinking) resolved by the harness; omitted keys fall
            # back to the server's own configured value.
            request.update(worker.get("request_overrides") or {})
            for key in worker.get("request_omit") or ():
                request.pop(key, None)
            # Conservative estimate includes tool schemas and call arguments, not just content.
            # Never trim a tool-call/result group or the task: when the full conversation would not
            # fit, SEND a compacted view (earlier file bodies -> reread references); the checkpoint
            # keeps the full conversation as the recoverable record.
            budget = int(worker.get("ctx", 8192)) - request["max_tokens"] - 512
            # Earlier hidden reasoning is evidence, not working context: it stays in the checkpoint
            # and is not re-sent (#33: it was ~500K chars across one run's histories).
            sent = [{k: v for k, v in m.items() if k != "reasoning_content"} if m.get("reasoning_content") else m
                    for m in messages]
            request["messages"] = sent
            view, replaced = compact_view(sent, tools, budget)
            if replaced:
                request["messages"] = view
                compactions.append({"round": rounds, "replaced": replaced,
                                    "full_tokens_est": _estimate(sent, tools),
                                    "sent_tokens_est": _estimate(view, tools)})
            estimated_input = _estimate(request["messages"], tools)
            if estimated_input + request["max_tokens"] + 512 > int(worker.get("ctx", 8192)):
                save()
                raise RuntimeError("tool context capacity exceeded (estimated input + output + reserve); "
                                   "checkpoint preserved; configure a supported budget or reduce input")
            if worker.get("api_key_env"):
                from endpoint_http import open_request
                req = urllib.request.Request(worker["url"].rstrip("/") + "/v1/chat/completions",
                    data=json.dumps(request).encode(), headers={"Content-Type": "application/json"})
                with open_request(req, int(worker.get("request_timeout") or 300), worker["api_key_env"]) as r:
                    response = json.load(r)
            else:
                response = _post(worker["url"].rstrip("/") + "/v1/chat/completions", request,
                                 timeout=int(worker.get("request_timeout") or 300))
            generations.append(response_evidence(response, request))
            pending = response["choices"][0]["message"]
            reason = response["choices"][0].get("finish_reason")
            if reason in ("length", "content_filter"):
                # Keep returned fragments as unexecuted diagnostics, never valid tool history.
                incomplete.append({"round": rounds, "reason": reason, "message": pending})
                rounds += 1
                if reason == "content_filter":
                    save()
                    raise RuntimeError("tool content-filter stop; checkpoint preserved; no calls executed")
                truncations += 1
                messages.append({"role": "user", "content":
                    "The last response reached the output limit. None of its tool calls executed. "
                    "Previously completed workspace edits remain. Inspect existing work and use a "
                    "smaller complete operation using advertised edit/append tools if available. "
                    "If no supported delivery operation can fit, report that "
                    "capacity blocker instead of regenerating the same oversized file."})
                save()
                if truncations >= 2:
                    raise RuntimeError("tool output capacity exhausted after two length stops; "
                                       "checkpoint preserved; no incomplete calls executed")
                continue
            if not pending.get("tool_calls"):
                review = job.get("draft_review")
                if review and coaching is None:
                    rounds += 1  # the draft completion also consumes a turn
                    coaching = {"status": "started" if rounds < maximum else "budget_exhausted"}
                    save()  # interrupted review must not silently spend again on resume
                    if rounds < maximum:
                        try:
                            questions = str(review() or "")[:6000]
                            coaching = {"status": "questions" if "?" in questions else "no_revision",
                                        "questions": questions}
                        except Exception as exc:
                            coaching = {"status": "unavailable", "error": type(exc).__name__}
                        if coaching["status"] == "questions":
                            messages.append(dict(pending, role="assistant"))
                            messages.append({"role": "user", "content":
                                "A read-only skeptic reviewed your actual draft. Address these questions "
                                "using existing files and advertised tools. Fix genuine problems; otherwise "
                                "explain why the draft stands. Preserve correct work. Any edits must use "
                                "normal receipted delivery, not a fenced replacement.\n" + questions})
                            save()
                            continue
                save()
                return {"choices": [{"message": {"content": pending.get("content") or ""}}],
                        "generations": generations, "coaching": coaching}
            messages.append(pending)
            save()
        existing = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
        for tool in pending["tool_calls"]:
            tid = tool["id"]
            if tid in existing:
                continue
            fn = tool["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
                result = _post(base + "/call", {"job_id": job["workspace_id"],
                    "call_id": _call_id(execution_id, tid), "name": fn["name"], "arguments": args},
                    token=token, timeout=120)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)[:500]}
            messages.append({"role": "tool", "tool_call_id": tid, "name": fn["name"],
                             "content": json.dumps(result)})
            save()
        rounds += 1
        save()
    if job.get("draft_review") and coaching is None:
        coaching = {"status": "budget_exhausted", "reason": "no completed draft within tool budget"}
        save()
    return {"choices": [{"message": {"content": "Tool round limit reached; work remains in the workspace."}}],
            "generations": generations, "loop_stop": "tool_round_budget"}
