"""Intake: rigid, branched questions that build a verbatim spec file -- with NO model inference.

Implements docs/INTAKE-questions.md. The protocol:
  - questions are fixed; branching is by rigid A/B answers, never by a model's judgment
  - every answer is stored EXACTLY as given (speech-to-text, broken grammar, all of it)
  - "done" is a coverage check over required fields, or an explicit user 'done' -- never a model call
  - render() emits a deliberately dumb Markdown file: one field, one verbatim answer, no summary
  - the finished file is then handed to a model in ONE turn with the reference card (single injection)

    python run/intake.py start   fleet-automation
    python run/intake.py next    fleet-automation          # the next required question to ask
    python run/intake.py answer  fleet-automation S1 "..."  # stored verbatim
    python run/intake.py status  fleet-automation          # checklist: filled / missing / done?
    python run/intake.py render  fleet-automation          # write intake/<project>.md
    python run/intake.py done    fleet-automation          # explicit early stop
"""
import argparse, json, re, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / "intake"

# (id, layer, prompt, options, required_on_branches)
QUESTIONS = [
    ("Q0", "branch", "Is the thing you want to build inside your own field of expertise? "
                     "A = Yes, I could check the output myself. B = No, I'd have to trust the output.",
     {"A": "in my domain -- I can verify the output", "B": "outside my domain -- I'd have to trust it"}, {"A", "B"}),
    ("S1", "spec", "Describe what you want to exist when this is finished. Not how -- what. "
                   "What would you point at and say 'that's it'?", None, {"A", "B"}),
    ("S2", "spec", "What does 'done' look like? Finish the sentence as many times as you can: "
                   "'I'll know it worked when ___.'", None, {"A", "B"}),
    ("S3", "spec", "What is the actual deliverable? A = code/a program, B = a document or report, "
                   "C = a spreadsheet or data file, D = a decision or recommendation, E = something else (name it). "
                   "Pick all that apply.",
     {"A": "code", "B": "document", "C": "data file", "D": "decision", "E": "other"}, {"A", "B"}),
    ("S4", "spec", "What must never happen? Things it must not touch, delete, send, spend, or claim.", None, {"A", "B"}),
    ("S5", "spec", "What do you already have that it should use? Files, data, an example of a good result, "
                   "a previous attempt, a reference doc. Name them, or say 'nothing yet'.", None, {"A", "B"}),
    ("S6a", "spec", "What's the standard a professional in your field would hold this to? "
                    "(A regulation, a convention, a spec, a rule of thumb.)", None, {"A"}),
    ("S6b", "spec", "Show me one example of what 'good' looks like, from anywhere -- a link, a product, a thing "
                    "you admire. If you can't, say so.", None, {"B"}),
    ("V1", "verifier", "How could a machine check this without you? A = run tests / code must pass, "
                       "B = a number must come out to a known value, C = it must match a reference, "
                       "D = a checklist of required parts must all be present, E = only a human can tell. "
                       "Pick all that apply.",
     {"A": "tests", "B": "known value", "C": "match reference", "D": "checklist", "E": "human only"}, {"A", "B"}),
    ("V2", "verifier", "Give one concrete example the check should PASS: 'If I put in X, it should produce Y.'", None, {"A", "B"}),
    ("V3", "verifier", "Give one example it should FAIL -- something that looks right but isn't.", None, {"A", "B"}),
    ("E1", "environment", "What models / servers do you have? Name them, or say 'use whatever is in the fleet config'.", None, {"A", "B"}),
    ("E2", "environment", "Where does the work live? A folder, a repo, a workspace. Where may it read, and where may it write?", None, {"A", "B"}),
    ("E3", "environment", "Set the leash -- how far may it run before you see a draft? A = after every finished piece, "
                          "B = after a batch, C = until done or stuck, D = overnight.",
     {"A": "every piece", "B": "per batch", "C": "until done/stuck", "D": "overnight"}, {"A", "B"}),
    ("E4", "environment", "Three short lists: what may it do WITHOUT asking, what must it ASK first, what may it NEVER do?", None, {"A", "B"}),
]
QBYID = {q[0]: q for q in QUESTIONS}
FIELD = {"Q0": "domain_branch", "S1": "spec.description", "S2": "spec.done_when", "S3": "spec.deliverable",
         "S4": "spec.never", "S5": "spec.inputs", "S6a": "spec.standard", "S6b": "spec.exemplar",
         "V1": "verifier.kind", "V2": "verifier.pass_example", "V3": "verifier.fail_example",
         "E1": "env.workers", "E2": "env.workspace", "E3": "env.leash", "E4": "env.rules"}
DONT_KNOW = re.compile(r"\b(don'?t know|dont know|not sure|no idea|can'?t)\b", re.I)

REFERENCE_CARD = """You are receiving a complete intake spec, collected verbatim from one user in one pass.
These answers describe ONE project. They are not fifteen separate requests. Read the whole file before
writing anything, and treat it the way you would treat a person who just talked you through their idea.

FIRST, and before any extraction, write THE SCOPED PROJECT -- about a page, in your own words:
  a. What this project is, and the problem it exists to solve.
  b. What "launched and working" looks like as a single coherent picture (synthesize across ALL the
     answers -- do not restate them one by one).
  c. Given the CURRENT STATE supplied to you, what already exists vs. what is missing. Scope is a
     delta: desired state minus current state. If no current state is supplied, say so and scope from zero.
  d. The phases that get from here to there, in order, with the reasoning for that order.
  e. The ONE thing to point the autonomous loop at first, and why that one.

THEN, and only then, derive the supporting machinery FROM that picture:
  1. Checkable criteria from the done_when lines.
  2. A first frozen oracle from verifier.pass_example and verifier.fail_example.
  3. The autonomy level from env.leash: HONOR the user's selected E3 cadence within the approved
     budget. The required scope/plan/map approvals and stops for consequential actions still apply,
     but do NOT add extra mandatory review stops. domain_branch (Q0) is input to VERIFICATION and
     EXPLANATION strategy -- a branch-B user who cannot read code needs stronger outcome evidence
     and clearer explanation -- and is NOT an autonomy cap: it never downgrades the chosen leash.
  4. env.rules 'never' items as hard gates, enforced rather than requested.
  5. The first batch of job cards, traceable to a phase in (d). Do not start work.

ON JUDGMENT -- this is the part most models get wrong:
  - You MUST synthesize intent across answers. Reading the whole and forming a view is the job.
  - You MUST NOT invent specific checks, numbers, thresholds, or facts that are not there. Flag those
    as [NEEDS CLARIFICATION] with the quote.
  - A gap NEVER excuses you from producing the scoped project. Scope around it, state the assumption
    plainly, and keep going. A pile of clarification flags with no plan is a failed response.
  - Garbled speech-to-text is expected. Read through it for meaning; flag it only where the meaning
    actually changes what you would build.
  - If STATUS: UNDERSPECIFIED, still produce (a)-(e), then offer ten distinct candidate directions
    around spec.description rather than choosing for the user."""


def _path(project, ext="json", directory=None):
    d = Path(directory) if directory else DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{project}.{ext}"

def load(project, directory=None):
    p = _path(project, directory=directory)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

def save(project, st, directory=None):
    _path(project, directory=directory).write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")

def start(project, directory=None):
    st = {"project": project, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "answers": {}, "done": False,
          "provenance": {}}
    save(project, st, directory=directory); return st

_CONNECT = {"and", "&", "plus", ",", "or"}

def _letters(text):
    """Option letters picked by a rigid A-E answer, read ONLY from the START of the answer.
    Speech-to-text answers are sentences ('E, only a human can tell'); scanning the whole sentence would
    read the article 'a' as option A. So: accept leading single letters A-E joined by connectors, and stop
    at the first other word. 'A and C because...' -> {A, C}; 'E only a human can tell' -> {E}."""
    picked = set()
    for tok in re.findall(r"[A-Za-z&]+|,", text or ""):
        t = tok.strip().lower()
        if len(t) == 1 and t.upper() in "ABCDE":
            picked.add(t.upper())
        elif t in _CONNECT:
            continue
        else:
            break
    return picked

_YES = {"yes", "yeah", "yep", "yup", "y"}
_NO = {"no", "nope", "nah", "n"}

def branch(st):
    a = st["answers"].get("Q0") or ""
    p = _letters(a)
    if p == {"A"}:
        return "A"
    if p == {"B"}:
        return "B"
    # Q0 is phrased as a yes/no question, so people answer 'No.' -- accept the plain leading word as
    # the rigid answer (yes -> A, no -> B). Still a fixed rule, still no inference.
    words = re.findall(r"[A-Za-z]+", a)
    w = words[0].lower() if words else ""
    return "A" if w in _YES else "B" if w in _NO else None

def _v1_human_only(st):
    return _letters(st["answers"].get("V1") or "") == {"E"}

def required_ids(st):
    """Required question ids for this state's branch (rigid rules, no inference)."""
    b = branch(st)
    if b is None:
        return ["Q0"]
    ids = [q[0] for q in QUESTIONS if b in q[4]]
    if _v1_human_only(st):
        ids = [i for i in ids if i not in ("V2", "V3")]
    return ids

def missing(st):
    return [i for i in required_ids(st) if not (st["answers"].get(i) or "").strip()]

def next_question(st):
    m = missing(st)
    return QBYID[m[0]] if m else None

def answer(project, qid, text, directory=None, source="user", by=None):
    st = load(project, directory=directory) or start(project, directory=directory)
    if qid not in QBYID:
        raise SystemExit(f"unknown question id {qid}")
    st["answers"][qid] = text            # VERBATIM. no cleanup, no interpretation.
    st.setdefault("provenance", {})
    if source == "user" and st["provenance"].get(qid, {}).get("source") == "confirmed":
        pass
    else:
        rec = {"source": source}
        if by:
            rec["by"] = by
        st["provenance"][qid] = rec
    save(project, st, directory=directory); return st

def is_done(st):
    return st.get("done") or (branch(st) is not None and not missing(st))

def underspecified(st):
    """UNDERSPECIFIED only on an ANSWERED 'don't know' (S1 or S6b), or an explicit early stop that left the
    exemplar empty. An unasked S6b mid-intake must not trip it -- that was flagging every branch-B intake
    as underspecified before the question was ever reached."""
    b = branch(st)
    s1 = st["answers"].get("S1") or ""
    ex = st["answers"].get("S6b") or ""
    if DONT_KNOW.search(s1):
        return True
    if b != "B":
        return False
    if ex.strip():
        return bool(DONT_KNOW.search(ex))
    return bool(st.get("done"))          # stopped early without an exemplar

def render(project, directory=None):
    st = load(project, directory=directory)
    if not st:
        raise SystemExit("no such intake")
    lines = [f"# Intake: {st['project']}", f"collected: {st['created']}  branch: {branch(st) or '?'}  "
             f"done: {is_done(st)}"]
    if underspecified(st):
        lines.insert(1, "STATUS: UNDERSPECIFIED")
    prov = st.get("provenance") or {}
    for qid, _layer, _prompt, _opts, _req in QUESTIONS:
        if qid not in required_ids(st) and qid not in st["answers"]:
            continue
        val = st["answers"].get(qid)
        kind = (prov.get(qid) or {}).get("source") or "user"
        lines += ["", f"## {FIELD[qid]}  ({qid})  [{kind}]", val if (val or "").strip() else "(skipped)"]
    lines += ["", "---", "## reference card (delivered with this file in one turn)", REFERENCE_CARD]
    out = "\n".join(lines) + "\n"
    _path(project, "md", directory=directory).write_text(out, encoding="utf-8")
    return out

def status(project, directory=None):
    st = load(project, directory=directory)
    if not st:
        print("no such intake"); return
    b = branch(st); req = required_ids(st); miss = missing(st)
    print(f"{st['project']}  branch={b or '?'}  filled={len(req)-len(miss)}/{len(req)}  done={is_done(st)}"
          + ("  STATUS: UNDERSPECIFIED" if underspecified(st) else ""))
    for i in req:
        v = st["answers"].get(i)
        print(f"  [{'x' if (v or '').strip() else ' '}] {i:<4} {FIELD[i]}")

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c, help_text in (
        ("next", "print the next unanswered intake question"),
        ("status", "show which required answers are still missing"),
        ("render", "write the verbatim intake markdown"),
        ("done", "mark the intake finished even if fields remain"),
    ):
        sp = sub.add_parser(c, help=help_text)
        sp.add_argument("project")
        sp.add_argument("--dir", default=None, help="intake store directory (a project package)")
    stt = sub.add_parser("start", help="open an empty intake store")
    stt.add_argument("project")
    stt.add_argument("--dir", default=None, help="write the intake store in this directory")
    a2 = sub.add_parser("answer", help="store one answer verbatim"); a2.add_argument("project"); a2.add_argument("qid"); a2.add_argument("text")
    a2.add_argument("--dir", default=None, help="intake store directory (a project package)")
    a = ap.parse_args()
    if a.cmd == "start":
        start(a.project, directory=getattr(a, "dir", None)); print(f"started {a.project}")
    elif a.cmd == "next":
        st = load(a.project, directory=a.dir) or start(a.project, directory=a.dir); q = next_question(st)
        if q is None:
            print("DONE -- all required fields answered. run: render")
        else:
            print(f"{q[0]} [{q[1]}]: {q[2]}")
    elif a.cmd == "answer":
        answer(a.project, a.qid, a.text, directory=a.dir); print(f"stored {a.qid} verbatim ({len(a.text)} chars)")
    elif a.cmd == "status":
        status(a.project, directory=a.dir)
    elif a.cmd == "render":
        print(render(a.project, directory=a.dir))
    elif a.cmd == "done":
        st = load(a.project, directory=a.dir); st["done"] = True; save(a.project, st, directory=a.dir); print("marked done")

if __name__ == "__main__":
    main()
