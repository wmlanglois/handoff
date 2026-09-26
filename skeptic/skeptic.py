"""The grounded skeptic. Read the artifact behind a claim, then challenge it.

    python skeptic/skeptic.py "<claim>" [--worker spark] [--root PATH] [--point path,path]

Its only job: append 2-3 specific, code-cited challenges so a claim can't roll up to
the architect unchallenged. Never agrees, never praises. It MUST read before it doubts;
that grounding is what separates a useful challenge from a memory riff.

Validated: grounded Spark (4B, 65k ctx, thinking off) lands sharp challenges in ~14s.
"""
import argparse, ast, json, operator, re, sys, time, urllib.request
from pathlib import Path

# Deterministic arithmetic for the jury: a 4B cannot be trusted to multiply, but it CAN call this and
# read the exact result. Whitelisted AST only -- numbers, + - * / // % ** and round(); no names, no
# attributes, no calls. This is what lets the CHEAP skeptic catch a wrong figure so the frontier
# architect never has to compute one.
_CALC_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
             ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
             ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}

def _calc_eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _CALC_OPS:
        return _CALC_OPS[type(node.op)](_calc_eval(node.left), _calc_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _CALC_OPS:
        return _CALC_OPS[type(node.op)](_calc_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "round":
        return round(*[_calc_eval(a) for a in node.args])
    raise ValueError("only numbers and + - * / // % ** round() are allowed")

def calc(expr):
    try:
        return str(_calc_eval(ast.parse(str(expr), mode="eval").body))
    except Exception as e:
        return f"calc error: {e}"

_FLEET = Path(__file__).resolve().parent.parent
for _p in (_FLEET, _FLEET / "tools"):
    if str(_p) not in sys.path: sys.path.insert(0, str(_p))
import grammar  # noqa  (tools/ on path): schemas that force the jury's final output to valid JSON


def _final_bits(label):
    """(schema, instruction) for the schema-constrained final answer, by mode."""
    if label == "question":
        return grammar.three_questions_schema(), (
            "Now output ONLY a JSON object: {\"questions\": [ ... ]} with EXACTLY three items, one per "
            "angle, each {\"angle\": one of TECHNICAL/BIG PICTURE/IMPROVEMENT, \"question\": \"...\"}.")
    if label == "review":
        return grammar.verdict_schema(), (
            "Now output ONLY a JSON object {\"tag\": one of "
            "SUPPORTED/OVERCLAIMING/HALLUCINATING/UNVERIFIABLE, \"note\": \"...\"}.")
    return grammar.challenges_schema(), (
        "Now output ONLY a JSON object {\"challenges\": [ ... ]} with 2-3 grounded, code-cited strings.")


def format_answer(label, data, raw):
    """Render the schema JSON into the note string the loop returns (falls back to raw text)."""
    if not isinstance(data, dict):
        return (raw or "").strip() or "SKEPTIC: (no answer)"
    if label == "question":
        qs = data.get("questions") or []
        lines = [f"{i+1}. {q.get('angle','?')}: {(q.get('question') or '').strip()}" for i, q in enumerate(qs)]
        return "\n".join(lines) or "SKEPTIC QUESTION: none"
    if label == "review":
        return f"SKEPTIC: [{data.get('tag','?')}] {(data.get('note') or '').strip()}"
    return "\n".join(f"- {c}" for c in (data.get("challenges") or [])) or "(no challenges)"

# Single source of truth: fleet.py. (A private copy of this table here is exactly what caused the
# stale-address bug earlier -- never re-type worker addresses in a module.)
from fleet import WORKERS as _FW, CODE_ROOT, SKEPTIC_WORKER  # noqa
WK = {k: (v["url"], v["model"]) for k, v in _FW.items()}
# The tree the skeptic may read (--root default). It was C:/Dev, one operator's layout; on any
# other machine that resolves to nothing and the skeptic "grounds" itself on an empty directory,
# which looks like a working run and is the exact ungrounded-riff failure this module exists to
# stop. CODE_ROOT is a setting, defaulting to this repo -- a tree that always exists.
DEFAULT_ROOT = CODE_ROOT

# The skeptic's system prompts are prose and live in docs/PROMPTS.md (#46), so tuning how the
# skeptic is prompted is a text edit, not a Python change. No fallback copy lives here: a second
# copy is exactly how the file and the code drifted apart before.
PROMPTS_FILE = Path(__file__).resolve().parent.parent / "docs" / "PROMPTS.md"


def load_prompt(name, path=None):
    """The text inside the one `text` fence under `### <name>` in docs/PROMPTS.md."""
    p = Path(path) if path else PROMPTS_FILE
    lines = p.read_text(encoding="utf-8").splitlines()
    try:
        i = lines.index("### " + name)
    except ValueError:
        raise RuntimeError("prompt {0!r} missing from {1}".format(name, p))
    j = i + 1
    while j < len(lines) and not lines[j].startswith("#"):
        if lines[j].strip() == "```text":
            k = lines.index("```", j + 1)
            body = "\n".join(lines[j + 1:k]).strip("\n")
            if not body.strip():
                raise RuntimeError("prompt {0!r} in {1} is empty".format(name, p))
            return body
        j += 1
    raise RuntimeError("prompt {0!r} in {1} has no text fence".format(name, p))


SYSTEM_QUESTION = load_prompt("SKEPTIC QUESTION")

SYSTEM_CHALLENGE = load_prompt("SKEPTIC CHALLENGE")

def review_read_instruction(artifact=None):
    """What the review skeptic is told to read FIRST. Names the ACTUAL deliverable when known, so the
    skeptic inspects `game/replay.py` (etc.) instead of hunting for a non-existent output.md."""
    if artifact:
        return "read_file the deliverable `{0}` (the exact file this output was produced as)".format(artifact)
    return "read_file the file it names (usually output.md)"


SYSTEM_REVIEW = load_prompt("SKEPTIC REVIEW")

def tools():
    def t(n, d, p, req): return {"type":"function","function":{"name":n,"description":d,
        "parameters":{"type":"object","properties":p,"required":req}}}
    return [
      t("list_files","List files matching a glob under the root.",{"pattern":{"type":"string"}},["pattern"]),
      t("read_file","Read a file (numbered lines); optional start_line/end_line.",
        {"path":{"type":"string"},"start_line":{"type":"integer"},"end_line":{"type":"integer"}},["path"]),
      t("grep","Regex search within a file or dir under the root.",
        {"pattern":{"type":"string"},"path":{"type":"string"}},["pattern","path"]),
      t("calc","Evaluate an arithmetic expression EXACTLY (e.g. '2 + 2') to check a number the "
        "output claims. You cannot be trusted to multiply in your head -- call this for EVERY computed "
        "figure and compare the exact result to what the output states.",{"expr":{"type":"string"}},["expr"]),
      t("done","Finish with 2-3 grounded, code-cited challenges.",{"challenges":{"type":"string"}},["challenges"]),
    ]

def run(claim, worker, root, rounds, system=SYSTEM_CHALLENGE, artifact=None):
    url, model = WK[worker]
    root = Path(root).resolve()
    def safe(rel):
        p = (root / str(rel).strip().strip('"').lstrip("/\\")).resolve()
        if root not in p.parents and p != root: raise ValueError("escapes root")
        return p
    def do_list(pat):
        return "\n".join(str(x.relative_to(root)).replace("\\","/") for x in root.glob(pat) if x.is_file())[:4000] or "(none)"
    def do_read(path, a=None, b=None):
        p = safe(path)
        if not p.exists(): return f"ERROR: {path} missing"
        L = p.read_text(encoding="utf-8", errors="replace").splitlines()
        a = a or 1; b = min(b or a+120, len(L), a+120)
        return "\n".join(f"{i:5d}| {L[i-1]}" for i in range(a, b+1))
    def do_grep(pat, path):
        p = safe(path); rx = re.compile(pat); out = []
        files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file() and f.suffix in (".ps1",".py",".md",".json")]
        for f in files:
            try:
                for i, l in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if rx.search(l): out.append(f"{f.relative_to(root).as_posix()}:{i}: {l.strip()[:160]}")
                    if len(out) >= 40: return "\n".join(out)
            except Exception: pass
        return "\n".join(out) or "(no matches)"
    def post(msgs, schema=None):
        # schema set -> GBNF/json_schema-constrained content (no tools): the final answer is FORCED to
        # valid JSON so a 4B cannot emit malformed output. schema None -> the tool-reading turns.
        body = {"model":model,"messages":msgs,"max_tokens":1100,
                "temperature":0.6,"chat_template_kwargs":{"enable_thinking":False}}
        if schema is not None:
            body["json_schema"] = schema
        else:
            body["tools"] = tools(); body["tool_choice"] = "auto"
        req = urllib.request.Request(url+"/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())["choices"][0]["message"]

    msgs = [{"role":"system","content":system}, {"role":"user","content":claim}]
    label = "question" if system is SYSTEM_QUESTION else "review" if system is SYSTEM_REVIEW else "challenges"
    if system in (SYSTEM_REVIEW, SYSTEM_QUESTION):
        ending = ("Then give your SKEPTIC verdict." if system is SYSTEM_REVIEW
                  else "Then ask your THREE SKEPTIC QUESTIONS -- one technical, one big-picture, one improvement.")
        msgs[1]["content"] = ("Read the ACTUAL artifact under the root FIRST -- call list_files, then "
                              + review_read_instruction(artifact) + "; do NOT judge from the text alone, and do NOT "
                              "claim the file is missing without listing first. If the output states a number, check it with calc. "
                              + ending + "\n\nWorker output:\n" + claim + "\n\nBegin by listing files.")
    t0 = time.time(); nudged = False; read_any = False
    # Phase 1: read the artifact with tools (llama.cpp constrains these tool calls natively).
    for step in range(rounds):
        if step >= max(4, rounds - 4) and not nudged:
            nudged = True
            msgs.append({"role": "user", "content": "You have read enough. Stop investigating; prepare your answer."})
        m = post(msgs); calls = m.get("tool_calls") or []
        if not calls:
            if read_any:
                break                                   # ready to answer
            msgs += [{"role": "assistant", "content": m.get("content") or ""},
                     {"role": "user", "content": "Read the artifact with a tool FIRST (list_files then read_file)."}]
            continue
        c = calls[0]; name = c["function"]["name"]
        try: args = json.loads(c["function"].get("arguments") or "{}")
        except Exception: args = {}
        if name == "done":
            break                                       # model signals it's ready
        read_any = True
        try:
            out = {"list_files":lambda:do_list(args.get("pattern","")),
                   "read_file":lambda:do_read(args.get("path",""),args.get("start_line"),args.get("end_line")),
                   "grep":lambda:do_grep(args.get("pattern",""),args.get("path","")),
                   "calc":lambda:calc(args.get("expr",""))}.get(name, lambda:"unknown tool")()
        except Exception as e: out = f"ERROR {e}"
        print(f"[{step}] {name} {json.dumps(args)[:80]}", flush=True)
        am = {"role":"assistant","content":m.get("content") or "","tool_calls":calls}
        for cc in am["tool_calls"]:
            if isinstance(cc["function"]["arguments"], dict): cc["function"]["arguments"] = json.dumps(cc["function"]["arguments"])
        msgs += [am, {"role":"tool","tool_call_id":c.get("id","0"),"name":name,"content":out[:6000]}]
    # Phase 2: FINAL answer, GBNF/json_schema-constrained -> always valid, no free-form drift.
    schema, ask = _final_bits(label)
    m = post(msgs + [{"role": "user", "content": ask}], schema=schema)
    raw = (m.get("content") or "").strip()
    try: data = json.loads(raw)
    except Exception: data = None
    note = format_answer(label, data, raw)
    print(f"=== {label} ({worker}, {round(time.time()-t0)}s) ===\n{note}")
    return note

if __name__ == "__main__":
    try:
        from safeio import force_utf8; force_utf8()
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("claim", help="the claim/conclusion to challenge (point it at files with --point)")
    ap.add_argument("--worker", default=SKEPTIC_WORKER, choices=list(WK))
    ap.add_argument("--root", default=DEFAULT_ROOT,
                    help="tree the skeptic may read (default: CODE_ROOT setting, else this repo)")
    ap.add_argument("--rounds", type=int, default=14)
    ap.add_argument("--mode", choices=("challenge", "review"), default="challenge",
                    help="challenge = doubt a conclusion; review = filter a worker's output vs its artifact")
    a = ap.parse_args()
    run(a.claim, a.worker, a.root, a.rounds, SYSTEM_REVIEW if a.mode == "review" else SYSTEM_CHALLENGE)
