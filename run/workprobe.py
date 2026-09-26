"""Work-compliance probes for a worker lane (goal item #9, 2026-09-25).

Qualification (run/registry.py) proves an endpoint can hold a conversation. It does not prove the
model can do what a packet asks: return code in the fence the harness extracts, emit a well-formed
`files` tool call, and write a file in sections when one turn cannot hold it. Those three failed
live only at dispatch, after a plan was approved and a lane was committed. This module asks each
of them once, through the production call path (call.chat: context guard, prefill lock for the
cluster, the worker's request profile), with the bundled tool service's own `files` schema, and
the harness's own code extractor.

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


def probe_tool_call(worker, chat):
    prompt = ("Create the file hello.py containing exactly one line: print('hi'). "
              "Use the files tool with action write. Do not answer in text.")
    msg, _ = chat(worker, [{"role": "user", "content": prompt}], tools=_files_tool(),
                  max_tokens=PROBE_TOKENS)
    calls = _calls(msg)
    if not calls:
        return {"ok": False, "note": "no tool call (answered in text)"}
    c = calls[0]
    if c["name"] != "files":
        return {"ok": False, "note": "called {0!r}, not files".format(c["name"])}
    if not isinstance(c["args"], dict):
        return {"ok": False, "note": "tool arguments are not valid JSON"}
    if c["args"].get("action") != "write" or c["args"].get("path") != "hello.py":
        return {"ok": False, "note": "files call was {0}".format(
            {k: c["args"].get(k) for k in ("action", "path")})}
    return {"ok": True, "note": "well-formed files write"}


def probe_incremental_write(worker, chat):
    prompt = ("Create numbers.py with 40 functions f1 through f40, each returning its own number "
              "(def f1(): return 1, and so on). Each files call may carry at most {0} lines: write "
              "the first {0} lines with action write, then add the rest with action append. "
              "Do not answer in text.".format(SECTION_LINES))
    messages = [{"role": "user", "content": prompt}]
    msg, _ = chat(worker, messages, tools=_files_tool(), max_tokens=PROBE_TOKENS)
    calls = _calls(msg)
    if not calls or not isinstance(calls[0]["args"], dict):
        return {"ok": False, "note": "no usable first files call"}
    first = calls[0]["args"]
    lines = len((first.get("content") or "").splitlines())
    if first.get("action") != "write":
        return {"ok": False, "note": "first call was {0!r}, not write".format(first.get("action"))}
    if lines > SECTION_LINES + SECTION_SLACK:
        return {"ok": False, "note": "first write carried {0} lines; asked for at most {1}".format(
            lines, SECTION_LINES)}
    if any(c["args"] and c["args"].get("action") == "append" for c in calls[1:]):
        return {"ok": True, "note": "write {0} lines, then append (same turn)".format(lines)}
    messages += [
        {"role": "assistant", "content": msg.get("content") or "", "tool_calls": [calls[0]["raw"]]},
        {"role": "tool", "tool_call_id": calls[0]["id"], "name": "files",
         "content": json.dumps({"ok": True, "result": {"path": first.get("path"),
                                                        "bytes": len(first.get("content") or "")}})},
    ]
    msg2, _ = chat(worker, messages, tools=_files_tool(), max_tokens=PROBE_TOKENS)
    nxt = [c for c in _calls(msg2) if isinstance(c["args"], dict)]
    if not nxt or nxt[0]["args"].get("action") != "append":
        return {"ok": False, "note": "after a {0}-line write the next call was {1!r}, not append".format(
            lines, nxt[0]["args"].get("action") if nxt else None)}
    return {"ok": True, "note": "write {0} lines, then append".format(lines)}


PROBES = (("format", probe_format), ("tool_call", probe_tool_call),
          ("incremental_write", probe_incremental_write))


def probe(worker, chat=None):
    """Run every probe against `worker`. Never raises: an error is that probe's failure."""
    if chat is None:
        import call

        def chat(w, messages, tools=None, max_tokens=PROBE_TOKENS):
            return call.chat(w, messages, tools=tools, max_tokens=max_tokens, timeout=180,
                             track=True, profile=True)   # honor max_inflight on a live fleet
    out = {"worker": worker, "checked": time.strftime("%Y-%m-%dT%H:%M:%S"), "probes": {}}
    for name, fn in PROBES:
        t = time.time()
        try:
            res = fn(worker, chat)
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
