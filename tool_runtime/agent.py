"""Portable OpenAI-compatible tool loop used by run/tooljob.py.

The dispatcher checkpoint records assistant tool calls before execution and each tool result
afterward. Retrying a call uses the same service call_id, whose persisted response prevents a
completed write from being repeated after a process interruption.
"""
import hashlib
import json
from pathlib import Path
import urllib.request
from generation import response_evidence

#: What this runtime implements. tooljob records it per job and preflight refuses a tools-mode run
#: on a runtime missing the required ones, so a less capable runtime is never used silently.
CAPABILITIES = ("length_recovery", "generation_evidence", "incremental_files", "context_check")


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
    else:
        messages = [{"role": "user", "content": job["prompt"]}]
        rounds = 0
        generations = []
        incomplete = []
        truncations = 0

    def save():
        dispatcher.checkpoint(execution_id, {"messages": messages, "rounds": rounds,
                                             "generations": generations, "incomplete": incomplete,
                                             "truncations": truncations})

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
                "tool_choice": "auto", "max_tokens": int(job.get("max_tokens", 1400)),
                "temperature": 0.2, "stream": False,
            }
            # Conservative estimate includes tool schemas and call arguments, not just content.
            # Do not trim a tool-call/result group or silently discard essential project material.
            estimated_input = (len(json.dumps({"messages": messages, "tools": tools},
                                               ensure_ascii=False)) + 3) // 4
            if estimated_input + request["max_tokens"] + 512 > int(worker.get("ctx", 8192)):
                save()
                raise RuntimeError("tool context capacity exceeded (estimated input + output + reserve); "
                                   "checkpoint preserved; configure a supported budget or reduce input")
            response = _post(worker["url"].rstrip("/") + "/v1/chat/completions", request, timeout=300)
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
                save()
                return {"choices": [{"message": {"content": pending.get("content") or ""}}],
                        "generations": generations}
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
    return {"choices": [{"message": {"content": "Tool round limit reached; work remains in the workspace."}}],
            "generations": generations, "loop_stop": "tool_round_budget"}
