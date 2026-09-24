"""Portable OpenAI-compatible tool loop used by run/tooljob.py.

The dispatcher checkpoint records assistant tool calls before execution and each tool result
afterward. Retrying a call uses the same service call_id, whose persisted response prevents a
completed write from being repeated after a process interruption.
"""
import hashlib
import json
from pathlib import Path
import urllib.request


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
    else:
        messages = [{"role": "user", "content": job["prompt"]}]
        rounds = 0
        dispatcher.checkpoint(execution_id, {"messages": messages, "rounds": rounds})
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
            response = _post(worker["url"].rstrip("/") + "/v1/chat/completions", {
                "model": worker["model"], "messages": messages, "tools": tools,
                "tool_choice": "auto", "max_tokens": int(job.get("max_tokens", 1400)),
                "temperature": 0.2, "stream": False,
            }, timeout=300)
            pending = response["choices"][0]["message"]
            if not pending.get("tool_calls"):
                return {"choices": [{"message": {"content": pending.get("content") or ""}}]}
            messages.append(pending)
            dispatcher.checkpoint(execution_id, {"messages": messages, "rounds": rounds})
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
            dispatcher.checkpoint(execution_id, {"messages": messages, "rounds": rounds})
        rounds += 1
        dispatcher.checkpoint(execution_id, {"messages": messages, "rounds": rounds})
    return {"choices": [{"message": {"content": "Tool round limit reached; work remains in the workspace."}}]}
