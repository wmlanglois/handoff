"""Work-compliance probes for a worker lane (goal item #9, 2026-09-25).

Qualification (run/registry.py) proves an endpoint can hold a conversation. It does not prove the
model can do what a packet asks: return code in the fence the harness extracts, emit a well-formed
`files` tool call, and write a file in sections when one turn cannot hold it. Those three failed
live only at dispatch, after a plan was approved and a lane was committed. This module asks each
of them once, through the production call path (call.chat: context guard, prefill lock for the
cluster, the worker's request profile), with the bundled tool service's own `files` schema, and
the harness's own code extractor. Tool probes EXECUTE on the configured tool service in a
private workspace, so a guarded append passes only when the service accepts its sha256.

The probes are reported, never used to rewrite the registry: a lane that fails one is still
usable for work that does not need it, and the operator decides.

    python run/workprobe.py <worker> [<worker> ...]
"""
import ast
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "run"), str(ROOT / "tool_runtime")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: Output allowance per probe turn. Small on purpose: a lane that cannot answer these inside it
#: will not fit a real packet's turn either.
PROBE_TOKENS = 700
#: The per-call line limit the incremental probe asks for, and the slack allowed over it.
SECTION_LINES = 15
SECTION_SLACK = 5
FENCE = "`" * 3


def _files_tool():
    from service import TOOLS
    return [t for t in TOOLS if t["function"]["name"] == "files"]


def _calls(msg):
    out = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (TypeError, ValueError):
            args = None
        out.append({"id": tc.get("id") or "call-{0}".format(len(out)), "name": fn.get("name"),
                    "args": args, "raw": tc})
    return out


def probe_format(worker, chat):
    from verified import first_code_block
    prompt = ("Write a Python function add(a, b) that returns a + b. Reply with only one "
              "{0}python fenced code block.".format(FENCE))
    msg, _ = chat(worker, [{"role": "user", "content": prompt}], max_tokens=PROBE_TOKENS)
    code = first_code_block(msg.get("content") or "")
    if not code.strip():
        return {"ok": False, "note": "no fenced code block the harness can extract"}
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return {"ok": False, "note": "extracted code does not parse: {0}".format(e.msg)}
    if not any(isinstance(n, ast.FunctionDef) and n.name == "add" for n in ast.walk(tree)):
        return {"ok": False, "note": "extracted code defines no add()"}
    return {"ok": True, "note": "fenced code extracted and parsed"}


def service_executor(job_id):
    """Execute files calls on the configured tool service, in a private per-probe workspace.

    Every result is the service's own answer -- including the sha256 a guarded append must quote --
    so a probe passes only on operations the service actually performed. Nothing is fabricated."""
    import secrets
    import urllib.request
    from fleet import TOOL_SERVICE
    token = Path(TOOL_SERVICE["token_file"]).read_text(encoding="utf-8").strip()

    def execute(name, args):
        body = json.dumps({"job_id": job_id, "call_id": "wp-" + secrets.token_hex(8),
                           "name": name, "arguments": args}).encode("utf-8")
        req = urllib.request.Request(TOOL_SERVICE["url"].rstrip("/") + "/call", data=body, method="POST",
                                     headers={"Authorization": "Bearer " + token,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    return execute


def _run_tools(worker, chat, execute, prompt, max_turns):
    """Drive a short tool conversation, executing every call for real. Returns the executed calls as
    [(args, service_result)] in order, and a note if the model stopped using tools."""
    messages = [{"role": "user", "content": prompt}]
    done = []
    for _turn in range(max_turns):
        msg, _ = chat(worker, messages, tools=_files_tool(), max_tokens=PROBE_TOKENS)
        calls = _calls(msg)
        if not calls:
            return done, "no tool call" if not done else ""
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": [c["raw"] for c in calls]})
        for c in calls:
            if c["name"] != "files" or not isinstance(c["args"], dict):
                res = {"ok": False, "error": "not a well-formed files call"}
            else:
                try:
                    res = execute("files", c["args"])
                except Exception as e:
                    res = {"ok": False, "error": "tool service call failed: {0}".format(str(e)[:160])}
            done.append((c["args"] if isinstance(c["args"], dict) else {}, res))
            messages.append({"role": "tool", "tool_call_id": c["id"], "name": "files",
                             "content": json.dumps(res)})
    return done, ""


def probe_tool_call(worker, chat, execute):
    prompt = ("Create the file hello.py containing exactly one line: print('hi'). "
              "Use the files tool with action write. Do not answer in text.")
    done, note = _run_tools(worker, chat, execute, prompt, max_turns=1)
    if not done:
        return {"ok": False, "note": "no tool call (answered in text)"}
    args, res = done[0]
    if not args:
        return {"ok": False, "note": "tool call was not a well-formed files call"}
    if args.get("action") != "write" or args.get("path") != "hello.py":
        return {"ok": False, "note": "files call was {0}".format({k: args.get(k) for k in ("action", "path")})}
    if not res.get("ok"):
        return {"ok": False, "note": "the service refused the write: {0}".format(res.get("error"))}
    return {"ok": True, "note": "files write executed by the service ({0} bytes)".format(
        (res.get("result") or {}).get("bytes"))}


def probe_incremental_write(worker, chat, execute):
    prompt = ("Create numbers.py with 40 functions f1 through f40, each returning its own number "
              "(def f1(): return 1, and so on). Each files call may carry at most {0} lines: write "
              "the first {0} lines with action write, then add the rest with action append. An append "
              "must quote the file's current sha256 (from the previous result) as expected_sha256. "
              "Do not answer in text.".format(SECTION_LINES))
    done, note = _run_tools(worker, chat, execute, prompt, max_turns=4)
    if not done:
        return {"ok": False, "note": "no usable files call"}
    first, first_res = done[0]
    lines = len((first.get("content") or "").splitlines())
    if first.get("action") != "write":
        return {"ok": False, "note": "first call was {0!r}, not write".format(first.get("action"))}
    if not first_res.get("ok"):
        return {"ok": False, "note": "the service refused the first write: {0}".format(first_res.get("error"))}
    if lines > SECTION_LINES + SECTION_SLACK:
        return {"ok": False, "note": "first write carried {0} lines; asked for at most {1}".format(
            lines, SECTION_LINES)}
    appends = [(a, r) for a, r in done[1:] if a.get("action") == "append"]
    if not appends:
        return {"ok": False, "note": "after a {0}-line write no append was attempted".format(lines)}
    good = [r for _a, r in appends if r.get("ok")]
    if not good:
        return {"ok": False, "note": "the service refused every append: {0}".format(appends[0][1].get("error"))}
    try:
        final = execute("files", {"action": "read", "path": first.get("path") or "numbers.py"})
        text = (final.get("result") or {}).get("content") or ""
        ast.parse(text)
    except SyntaxError as e:
        return {"ok": False, "note": "the assembled file does not parse: {0}".format(e.msg)}
    except Exception as e:
        return {"ok": False, "note": "could not read back the file: {0}".format(str(e)[:120])}
    total = len(text.splitlines())
    if total <= lines:
        return {"ok": False, "note": "the file did not grow past the first write ({0} lines)".format(total)}
    return {"ok": True, "note": "write {0} lines, {1} guarded append(s) accepted by the service, "
                                "{2}-line file parses".format(lines, len(good), total)}


PROBES = (("format", probe_format), ("tool_call", probe_tool_call),
          ("incremental_write", probe_incremental_write))


def probe(worker, chat=None, execute=None):
    """Run every probe against `worker`. Never raises: an error is that probe's failure.

    Tool probes execute on the configured tool service in a fresh private workspace; when the
    service is unreachable they FAIL with that reason rather than pass on an imitation."""
    if chat is None:
        import call

        def chat(w, messages, tools=None, max_tokens=PROBE_TOKENS):
            return call.chat(w, messages, tools=tools, max_tokens=max_tokens, timeout=180,
                             track=True, profile=True)   # honor max_inflight on a live fleet
    job_id = "workprobe-{0}-{1}".format("".join(ch if ch.isalnum() else "-" for ch in worker),
                                        time.strftime("%Y%m%d%H%M%S"))
    if execute is None:
        try:
            execute = service_executor(job_id)
        except Exception as e:
            reason = "tool service unavailable: {0}".format(str(e)[:120])

            def execute(name, args):
                raise RuntimeError(reason)
    out = {"worker": worker, "checked": time.strftime("%Y-%m-%dT%H:%M:%S"), "workspace": job_id,
           "probes": {}}
    for name, fn in PROBES:
        t = time.time()
        try:
            res = fn(worker, chat) if fn is probe_format else fn(worker, chat, execute)
        except Exception as e:
            res = {"ok": False, "note": "{0}: {1}".format(type(e).__name__, str(e)[:160])}
        res["ms"] = round((time.time() - t) * 1000)
        out["probes"][name] = res
    out["ok"] = all(p["ok"] for p in out["probes"].values())
    return out


def main(argv=None):
    names = list(argv if argv is not None else sys.argv[1:])
    if not names:
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    import fleet
    rc = 0
    for name in names:
        rep = probe(name)
        path = fleet.runs_root() / "workprobe-{0}-{1}.json".format(name, time.strftime("%Y%m%d-%H%M%S"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rep, indent=2), encoding="utf-8")
        print("{0}: {1}".format(name, "ok" if rep["ok"] else "FAILED"))
        for k, p in rep["probes"].items():
            print("  {0:<18} {1:<4} {2} ({3} ms)".format(k, "ok" if p["ok"] else "FAIL", p["note"], p["ms"]))
        print("  recorded {0}".format(path))
        rc = rc or (0 if rep["ok"] else 1)
    return rc


if __name__ == "__main__":
    sys.exit(main())
