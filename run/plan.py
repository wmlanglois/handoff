"""Outcome contracts and the delegation-quality check: what the harness is allowed to ASK FOR.

THE DEFECT THIS REPLACES. The production planner said, verbatim:

    "Decompose the GOAL into 1 to 5 SMALL, checkable jobs"

and then ran that list sequentially and stopped. Nothing here was broken in the ordinary sense --
it did exactly what it was told, which is why it kept producing errands. The unit of delegation was
a chore, so the harness could only ever deliver chores, and the operator was the only one holding
the actual outcome. `loop_config.worker_max_tokens` was raised from 900 to 4096 for the same
reason; a ceiling that cannot hold a module plus its tests makes an outcome physically
undeliverable. Raising the ceiling was necessary and, as DISPOSITION B4 records, not sufficient:
the ASK had to change too.

The contrast the operator drew, which is the specification for this module:

    weak:   "Inspect configuration handling and suggest tests."
    strong: "Own the single-machine onboarding experience: discover, propose, qualify, persist,
             and demonstrate a successful job. Preserve the existing fleet. Return the working
             implementation and evidence."

The difference is not size. It is that the strong one names a USER CAPABILITY, says what the owner
may decide alone, says what must keep working, and says what evidence ends it.

WHAT AN OUTCOME CONTRACT CARRIES (interface I7):
    capability        the user-facing thing that exists afterwards and did not before
    criterion_id      which approved goal criterion it advances -- an assignment that cannot say
                      this is not work on the goal, it is adjacent activity
    decides_alone     the decisions the owner makes without asking. An owner who decides nothing
                      is a courier; the architect still holds the work.
    keep_compatible   the interfaces it must not break, so "done" cannot mean "done and something
                      else is now broken"
    evidence          what will establish completion, agreed BEFORE the work, so acceptance is not
                      negotiated afterwards by whoever is most fluent
    integrator        who takes the result. Work nobody integrates is a report nobody needs.

THE DELEGATION CHECK GRADES THE MANAGER, NOT THE WORKER. Everything else in this repo checks the
worker's output: the oracle, the skeptic, the evidence record, the grounding status. Nothing checked
the assignment itself, so a badly-shaped ask spent a worker's hour before anyone noticed it could
not have succeeded. `review()` runs BEFORE dispatch and rejects an assignment that

  1. duplicates another owner's capability,
  2. produces a report nobody consumes,
  3. lacks the source material or the tools to succeed,
  4. leaves all consequential implementation with the architect,
  5. cannot say how completing it advances the approved goal.

The checks are lexical and therefore fallible. They are deliberately biased towards letting work
through: each one requires a positive signal of the defect, not the absence of a signal of health,
because a planner blocked by a false positive stops working entirely while a false negative merely
costs one badly-shaped job.
"""
import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# =================================================================================================
# the contract
# =================================================================================================

@dataclass(frozen=True)
class OutcomeContract:
    """One bounded outcome, owned end to end by one worker."""
    name: str                       # kebab id, unique within the goal
    capability: str                 # the user capability this creates
    criterion_id: str               # which goal criterion it advances
    brief: str                      # what to build, in the owner's own terms
    done_when: tuple = ()
    decides_alone: tuple = ()
    keep_compatible: tuple = ()
    evidence: str = ""
    integrator: str = ""
    sources: tuple = ()             # files/dirs the owner is handed
    oracle: str = ""
    tools: bool = False
    needs: tuple = ()               # names of contracts that must land first
    worker: str = None              # None = choose by capability (I6)
    artifact: str = "output.md"
    goal_id: str = ""

    def to_dict(self):
        d = asdict(self)
        for k in ("done_when", "decides_alone", "keep_compatible", "sources", "needs"):
            d[k] = list(d[k])
        return d


def as_contract(obj):
    """Accept either a dataclass or the plain dict a planner returns."""
    if isinstance(obj, OutcomeContract):
        return obj.to_dict()
    d = dict(obj)
    # `evidence` and `integrator` belong here too. contract_brief() reads c["evidence"] directly,
    # so a contract built through this documented entry point crashed the brief builder with a
    # KeyError -- found during integration 2026-09-19. A normaliser that does not normalise every
    # field its own consumer requires is worse than no normaliser, because it looks safe.
    for k in ("done_when", "decides_alone", "keep_compatible", "sources", "needs"):
        v = d.get(k) or []
        d[k] = list(v) if not isinstance(v, str) else [v]
    # `evidence` is PROSE, not a list: review() calls .strip() on it. contract_brief() reads it
    # directly, so a contract built through this entry point used to KeyError -- found during
    # integration 2026-09-19. Defaulting it to a LIST fixed that crash and broke review() instead,
    # which is a reminder that a normaliser must match its consumers' shapes, not just their keys.
    ev = d.get("evidence") or ""
    d["evidence"] = "; ".join(str(x) for x in ev) if isinstance(ev, (list, tuple)) else str(ev)
    d.setdefault("integrator", "architect")
    d.setdefault("artifact", "output.md")
    d.setdefault("dest", "")
    d.setdefault("base_sha256", "")
    d.setdefault("tools", False)
    d.setdefault("oracle", "")
    d.setdefault("worker", None)
    return d


def contract_brief(contract, goal_text=None):
    """Render the contract as the prose the worker actually receives.

    The ownership statement is not decoration. A worker handed only "implement X" reasonably
    returns the smallest X that compiles; a worker told what capability it owns, what it may decide
    alone and what evidence ends the job returns something a person can use. This text is what
    makes the difference between the weak and strong asks above visible AT THE WORKER."""
    c = as_contract(contract)
    parts = [f"OUTCOME YOU OWN: {c['capability']}", "", c["brief"].strip(), ""]
    if goal_text:
        parts += [f"This advances the approved goal: {goal_text}",
                  f"Specifically criterion {c['criterion_id']}.", ""]
    if c["decides_alone"]:
        parts += ["You decide these WITHOUT asking: " + "; ".join(c["decides_alone"]), ""]
    if c["keep_compatible"]:
        parts += ["Keep working, do not break: " + "; ".join(c["keep_compatible"]), ""]
    if c["sources"]:
        parts += ["Source material you are handed: " + ", ".join(c["sources"]), ""]
    if c["evidence"]:
        parts += [f"Completion is established by: {c['evidence']}", ""]
    if c["integrator"]:
        parts += [f"Your result is integrated by: {c['integrator']}", ""]
    parts += ["Investigate, implement, test and document the whole outcome. Returning a plan, a "
              "summary of what you would do, or a list of suggestions is NOT this outcome."]

    # DELIVERY CONTRACT (O2): what to produce, where it goes, what is available, and how the reply
    # becomes the artifact -- mode-aware, so a worker receives an assignment it can physically
    # execute rather than a prose request assuming capabilities it lacks. Domain-agnostic: it names
    # the deliverable file, not any task domain, and does not assume Python.
    art = c.get("artifact") or "output.md"
    staged = sorted({Path(str(sp)).stem for sp in (c.get("sources") or []) if str(sp).endswith(".py")})
    dc = ["", "DELIVERY CONTRACT (how your work becomes the accepted artifact):",
          "- The deliverable is the file `{0}`.".format(art)]
    if staged:
        dc.append("- These modules are already staged in your workspace and importable by name; "
                  "import them directly and do NOT re-implement them: {0}.".format(", ".join(staged)))
    if c.get("tools"):
        dc.append("- You HAVE tools/a workspace: create or modify `{0}` there, then stop. The harness "
                  "reads `{0}` from your workspace and runs the frozen oracle against it.".format(art))
    else:
        dc.append("- You have NO shell and NO tools. Return EXACTLY ONE fenced code block whose body "
                  "is the COMPLETE contents of `{0}` (for a .py file, real importable standard-library "
                  "Python; for any other file type, its literal contents). The harness writes that "
                  "block verbatim to `{0}` and runs the frozen oracle. Narration, a plan, or several "
                  "blocks do not produce the artifact.".format(art))
    dc.append("- Produce `{0}` itself; do not describe what you would do.".format(art))
    parts += dc
    parts += ["", "If, while doing this, you find work the approved goal still needs, a missing "
              "prerequisite, a better alternative approach, or an open question the evidence cannot "
              "yet settle, you may add AFTER your deliverable one fenced block tagged `proposal` "
              "containing JSON: {\"kind\": \"next_work\"|\"prerequisite\"|\"alternative\"|\"question\", "
              "\"text\": \"...\", \"evidence\": \"what you saw that supports it\"}. A proposal is "
              "recorded for the architect to accept, defer or reject; it grants no tools, no budget, "
              "and does not replace your deliverable."]
    return "\n".join(parts).strip()


# =================================================================================================
# the delegation-quality check
# =================================================================================================

@dataclass
class Review:
    name: str
    ok: bool
    problems: list = field(default_factory=list)      # [(code, why)]

    def __bool__(self):
        return self.ok

    def why(self):
        return "; ".join(f"{c}: {w}" for c, w in self.problems)


# Verbs whose OBJECT is a description of work rather than the work. They are only damning in the
# absence of any production signal -- "investigate, implement and test" is the shape we want, and
# it contains "investigate".
_REPORT_SHAPE = re.compile(
    r"\b(suggest|recommend|propose|outline|assess|evaluate|summari[sz]e|"
    r"report\s+on|write\s+(?:up\s+)?a\s+(?:report|summary|analysis|memo\s+about)|"
    r"identify\s+(?:issues|problems|gaps)|list\s+(?:issues|problems|options))\b", re.I)

# Positive evidence that the assignment produces something rather than describing something.
_PRODUCTION = re.compile(
    r"\b(implement|build|write\s+(?:the\s+)?(?:code|module|test|tests|function|class|script)|"
    r"add\s+(?:a\s+)?test|create\s+(?:the\s+)?(?:file|module|script|workbook|spreadsheet)|"
    r"fix|refactor|register|persist|execute|demonstrate|working\s+implementation|"
    r"produce\s+(?:the\s+)?(?:file|artifact|workbook|spreadsheet|module))\b", re.I)

# Work that cannot be done by emitting text into output.md: it needs the tool service.
_NEEDS_TOOLS = re.compile(
    r"\b(run\s+(?:the\s+)?(?:tests?|suite|command|script)|execute\s+|"
    r"create\s+(?:a\s+)?(?:spreadsheet|workbook|xlsx|csv)|write\s+(?:a\s+)?file\s+to|"
    r"commit|install|invoke\s+the\s+tool)\b", re.I)

# The manager keeping the real work. "You propose, I implement" is the errand generator.
_ARCHITECT_KEEPS = re.compile(
    r"(for\s+the\s+architect\s+to\s+(?:implement|write|build|finish)|"
    r"the\s+architect\s+will\s+(?:implement|write|build|do|finish)|"
    r"hand\s+(?:it\s+|the\s+\w+\s+)?(?:back|off)\s+to\s+the\s+architect|"
    r"leave\s+the\s+implementation\s+to|so\s+(?:I|we)\s+can\s+implement)", re.I)


def _norm(s):
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split()


def _overlap(a, b):
    """Jaccard over content words. Crude on purpose: it flags two assignments describing the same
    capability in slightly different words, which is the duplicate that actually happens."""
    sa, sb = set(_norm(a)), set(_norm(b))
    sa -= _STOP; sb -= _STOP
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


_STOP = set("the a an of to and for with that this it its on in by is are be own owner make "
            "makes making add adds user users job jobs work working".split())

DUPLICATE_THRESHOLD = 0.6


def review(contract, *, others=(), criteria=(), root=None, project_root=None,
           integration_check=None, known_producers=(), enforce_closure=True):
    """Grade ONE assignment before it is dispatched. Returns a falsy Review when it must not run.

    `known_producers` names assignments already accepted for this goal, so a `needs` edge onto
    one of them is satisfied even though it is not a
    sibling in THIS set of contracts. `enforce_closure` gates the orphaned-dependency check: it is
    a WHOLE-PLAN property, so an INCREMENTAL caller that reviews partial rounds (plan_with_recovery)
    passes False and runs the closure check once on the assembled plan instead."""
    c = as_contract(contract)
    root = Path(root or ROOT)
    problems = []

    # -- 5. cannot say how it advances the approved goal ------------------------------------------
    ids = [x["id"] if isinstance(x, dict) else x for x in (criteria or ())]
    if not c.get("criterion_id"):
        problems.append(("UNLINKED", "names no goal criterion, so nothing it produces can be "
                                     "checked against the approved goal"))
    elif ids and c["criterion_id"] not in ids:
        problems.append(("UNLINKED", f"criterion {c['criterion_id']!r} is not one of {ids}"))

    if not (c.get("capability") or "").strip():
        problems.append(("NO_CAPABILITY", "states no user capability; an assignment that cannot "
                                          "name what will exist afterwards is an activity"))
    if not (c.get("evidence") or "").strip():
        problems.append(("NO_EVIDENCE", "names no evidence that would establish completion, so "
                                        "acceptance would be settled by argument afterwards"))
    if not (c.get("done_when") or ()):
        problems.append(("NO_CRITERIA", "has no done_when clauses to check"))
    if c.get("require_consumer"):
        import conductor
        for msg in conductor.consumer_contract_problems(c):
            problems.append(("NO_CONSUMER", msg))

    text = " ".join([c.get("brief") or "", c.get("capability") or "",
                     " ".join(c.get("done_when") or ())])

    # -- 2. a report nobody needs -----------------------------------------------------------------
    produces = bool(c.get("tools")) or bool((c.get("oracle") or "").strip()) or \
        bool(_PRODUCTION.search(text))
    if _REPORT_SHAPE.search(text) and not produces:
        problems.append(("REPORT_ONLY",
                         "describes work instead of doing it (report-shaped, no artifact, no "
                         "oracle, no tools); the weak ask 'inspect X and suggest tests' in "
                         "exactly this shape produced nothing anyone used"))
    if not (c.get("integrator") or "").strip():
        problems.append(("NO_INTEGRATOR", "names nobody who takes the result; unintegrated output "
                                          "is a report nobody needs"))
    dest = (c.get("dest") or "").strip()
    if dest:
        import integrate
        problems.extend(integrate.dest_problems(dest))
    elif (c.get("integrator") or "").strip().lower() == "handoff":
        problems.append(("NO_DEST",
                         "an outcome integrated by handoff must set dest to the package-relative "
                         "project path of its artifact; the project check cannot land an unbound file"))
    if dest:
        art = str(c.get("artifact") or "").replace("\\", "/")
        if art != dest.replace("\\", "/"):
            problems.append(("ARTIFACT_DEST",
                             "artifact must be the same package-relative path as dest "
                             "({0}); a basename-only artifact is written at the workspace root "
                             "and is not the file the project imports".format(dest)))
        if "/" in dest.replace("\\", "/"):
            rel = dest.replace("\\", "/")
            mod = rel.rsplit("/", 1)[-1][:-3] if rel.endswith(".py") else ""
            qualified = rel[:-3].replace("/", ".") if rel.endswith(".py") else ""
            oracle = c.get("oracle") or ""
            bare = ("import {0}".format(mod) in oracle) or ("from {0} import".format(mod) in oracle)
            if mod and bare and qualified not in oracle:
                problems.append(("ORACLE_IMPORT",
                                 "oracle imports {0} as a top-level module, but dest is {1}; "
                                 "import {2}".format(mod, rel, qualified)))

    # -- 3. lacks source material or tools --------------------------------------------------------
    def _exists(sp):
        q = Path(sp)
        if q.is_absolute():
            return q.is_file()
        rel = str(sp).replace("\\", "/").lstrip("/")
        return (root / rel).is_file() or bool(project_root and (Path(project_root) / rel).is_file())
    # A source that another outcome in THIS plan produces is not "missing": the scheduler orders by
    # `needs` and the executor stages the accepted producer's bytes at that dest (card_for producer
    # substitution). Exempt those, so a connected multi-file plan (world.py -> engine.py -> launcher)
    # is not rejected merely because the upstream file does not exist on disk yet.
    produced = set()
    for o in others or ():
        od = as_contract(o)
        if od.get("name") == c.get("name"):
            continue
        for key in ("dest", "artifact"):
            v = (od.get(key) or "").replace("\\", "/").lstrip("/")
            if v and v != "output.md":
                produced.add(v)
                produced.add(Path(v).name)

    def _available(sp):
        rel = str(sp).replace("\\", "/").lstrip("/")
        return _exists(sp) or rel in produced or Path(sp).name in produced

    missing = [s for s in (c.get("sources") or ()) if not _available(s)]
    if missing:
        problems.append(("NO_MATERIAL", f"source material does not exist: {missing}"))
    if _NEEDS_TOOLS.search(text) and not c.get("tools"):
        problems.append(("NO_MATERIAL", "the brief requires running or writing files but tools is "
                                        "false, so the worker has no way to do it"))

    # -- 4. all consequential implementation stays with the architect ------------------------------
    if _ARCHITECT_KEEPS.search(text):
        problems.append(("ARCHITECT_KEEPS_WORK",
                         "hands the implementation back to the architect; the owner is left "
                         "couriering a proposal"))
    if not (c.get("decides_alone") or ()):
        problems.append(("ARCHITECT_KEEPS_WORK",
                         "grants the owner no decision of its own, so every real choice returns "
                         "to the architect and the outcome is not owned"))

    # -- 1. duplicates another owner ---------------------------------------------------------------
    for o in others or ():
        od = as_contract(o)
        if od.get("name") == c.get("name"):
            problems.append(("DUPLICATE_NAME", "two planned outcomes use the same assignment name"))
            break
        if dest and (od.get("dest") or "").replace("\\", "/") == dest.replace("\\", "/"):
            problems.append(("DUPLICATE_DEST", "two planned outcomes own the same destination"))
            break
        if (c.get("approach") and od.get("approach") and c["approach"] != od["approach"]
                and od.get("criterion_id") == c.get("criterion_id")):
            # Declared competing approaches to ONE criterion (LF-04). They share acceptance on
            # purpose; integration does not "pick a winner", the frozen oracle does. Undeclared
            # duplicates are still refused: two owners who do not know about each other is the
            # failure this check exists for.
            continue
        if _overlap(od.get("capability"), c.get("capability")) >= DUPLICATE_THRESHOLD:
            problems.append(("DUPLICATE",
                             f"capability overlaps {od.get('name')!r}: two owners would build the "
                             f"same thing and integration would have to pick a winner"))
            break
        if od.get("done_when") and list(od["done_when"]) == list(c.get("done_when") or ()):
            problems.append(("DUPLICATE", f"identical done_when to {od.get('name')!r}"))
            break

    # -- orphaned dependency (issue #4) -----------------------------------------------------------
    # next_work resolves `needs` against accepted ASSIGNMENT NAMES, not filenames or stems.
    # A filename alias or self-dependency would pass a loose check but never dispatch.
    if enforce_closure and c.get("needs"):
        producers = set(known_producers or ())
        graph = {c.get("name"): c.get("needs") or []}
        for o in others or ():
            od = as_contract(o)
            producers.add(od.get("name"))
            graph[od.get("name")] = od.get("needs") or []
        producers.discard("")
        for dep in c.get("needs") or ():
            if dep and dep not in producers:
                problems.append(("ORPHAN_DEP",
                                 "needs {0!r}, but no other planned or accepted assignment has "
                                 "that name; the scheduler cannot release it".format(dep)))
        # A closed cycle has producers for every edge but no first ready assignment.
        def reaches_self(node, seen):
            if node == c.get("name"):
                return True
            if node in seen or node not in graph:
                return False
            return any(reaches_self(n, seen | {node}) for n in graph[node])
        if any(reaches_self(dep, set()) for dep in c.get("needs") or ()):
            problems.append(("CYCLE_DEP", "dependency cycle leaves no first schedulable assignment"))

    # -- consumer calls the real interface (2026-09-23) --------------------------------------------
    # A consumer that says it calls `engine.simulate_journey` when the engine outcome only PROVIDES
    # `run_journey` has named a function that will not exist -- the assignment is incoherent before a
    # worker touches it, and (for a test outcome) it would certify against an interface nobody built.
    # Grade every `<needed-module>.<attr>` the consumer names against that module's declared provides.
    # Bounded to modules this contract `needs`, so an unrelated dotted token cannot trip it.
    _EXT = {"py", "txt", "md", "json", "csv", "cfg", "ini", "toml"}
    by_dest_stem = {}
    for o in others or ():
        od = as_contract(o)
        if od.get("name") == c.get("name"):
            continue
        d = (od.get("dest") or od.get("artifact") or "").replace("\\", "/")
        if d.endswith(".py"):
            by_dest_stem[d.rsplit("/", 1)[-1][:-3]] = od
    needed_stems = set()
    for dep in c.get("needs") or ():
        for stem, od in by_dest_stem.items():
            if od.get("name") == dep:
                needed_stems.add(stem)
    if needed_stems:
        blob = " ".join([c.get("oracle") or "", c.get("provides") or "",
                         " ".join(c.get("done_when") or ()), c.get("brief") or ""])
        for stem in needed_stems:
            provided = set(re.findall(r"[A-Za-z_]\w*", by_dest_stem[stem].get("provides") or ""))
            for attr in re.findall(re.escape(stem) + r"\.([A-Za-z_]\w*)", blob):
                if attr in _EXT or attr in provided or (attr.startswith("__") and attr.endswith("__")):
                    continue
                problems.append(("WRONG_CALL",
                                 "names {0}.{1}, but the {2!r} outcome provides no {1!r}; call its "
                                 "declared interface ({3}) instead".format(
                                     stem, attr, by_dest_stem[stem].get("name"),
                                     (by_dest_stem[stem].get("provides") or "").strip()[:80] or "?")))
                break

    return Review(name=c.get("name", "?"), ok=not problems, problems=problems)


def gate(contracts, *, criteria=(), root=None, project_root=None, integration_check=None,
         known_producers=(), enforce_closure=True):
    """Run the check over a whole plan. Returns (accepted_contracts, [(contract, Review), ...]).

    Rejections are returned, not raised: a plan with one bad assignment should dispatch the other
    four and report the one, exactly as a blocked assignment must not stop independent work.

    Component oracles are not required to reproduce a frozen integration check. Passing
    `integration_check` does not add an assertion-text gate."""
    cs = [as_contract(c) for c in contracts]
    ok, bad = [], []
    for c in cs:
        r = review(c, others=[o for o in cs if o is not c], criteria=criteria, root=root, project_root=project_root,
                   integration_check=integration_check, known_producers=known_producers,
                   enforce_closure=enforce_closure)
        (ok if r.ok else bad).append(c if r.ok else (c, r))
    return ok, bad


_LAUNCH_CMD = re.compile(r"python[0-9.]*\s+([^\s`'\"]+\.py)", re.IGNORECASE)


def required_launchers(scope_text):
    """Launcher entry files the approved scope says the project is launched with, e.g. a scope that
    reads `python game.py` requires a producer of `game.py` (issue #4). Returns relative paths.
    `python -c ...` and module invocations name no file and impose no launcher requirement."""
    out = set()
    for m in _LAUNCH_CMD.finditer(scope_text or ""):
        target = m.group(1).replace("\\", "/")
        if target and not Path(target).is_absolute() and not target.startswith("../"):
            out.add(target.removeprefix("./"))
    return out


def missing_launcher(scope_text, accepted, *, project_root=None):
    """A required launcher with no producer among the accepted outcomes and not present in the
    baseline project tree. Returns the sorted list of unproduced launcher paths (empty when the
    plan is launchable)."""
    targets = required_launchers(scope_text)
    if not targets:
        return []
    produced = set()
    for c in accepted or ():
        cc = as_contract(c)
        for key in ("dest", "artifact"):
            v = (cc.get(key) or "").replace("\\", "/")
            if v:
                produced.add(v.removeprefix("./"))
    proot = Path(project_root) if project_root else None
    out = []
    for t in sorted(targets):
        if t in produced:
            continue
        if proot and (proot / t).is_file():
            continue
        out.append(t)
    return out


# =================================================================================================
# the planner prompt
# =================================================================================================

PLAN_SYS = """You are the conductor of a small local model fleet. You do NOT do the work.

Decompose the GOAL into {lo} to {hi} bounded OUTCOMES. An outcome is not a chore and not a whole
program: it is a user-facing capability that one owner can investigate, implement, test and
document end to end, and that a person could USE when it lands.

  weak:   "Inspect configuration handling and suggest tests."
  strong: "Own the single-machine onboarding experience: discover, propose, qualify, persist and
           demonstrate a successful job. Preserve the existing fleet. Return the working
           implementation and evidence."

Every outcome you emit is checked before dispatch and REJECTED if it duplicates another owner,
produces a report nobody consumes, lacks the material or tools to succeed, leaves the real
implementation with you, or cannot say which goal criterion it advances.

The plan must be DEPENDENCY-CLOSED and LAUNCHABLE:
  - Every name in an outcome's `needs` must be the assignment NAME of another outcome in THIS
    plan (or an already accepted assignment). A baseline filename is a source, not a `needs` name.
    Never reference a `needs` that no assignment produces.
  - If the goal is launched with a command like `python game.py`, one outcome must PRODUCE that entry
    file (its `dest` is that path). Do not assume a launcher that no outcome builds. When the project
    starts empty, the outcome that satisfies the "runs via `python <file>`" criterion is the one whose
    `dest` is `<file>`.
  - One `dest` file has exactly ONE owning outcome. If that file satisfies several criteria, give it a
    single outcome that lists all their checks in `done_when` and binds the primary `criterion_id`; do
    NOT emit two outcomes with the same `dest`. Two rows for one file are one deliverable, not two, and
    two owners of the same file collide at integration.

Reply with JSON ONLY:
{"outcomes":[{
  "name":"kebab-id",
  "capability":"the user capability that exists when this lands",
  "criterion_id":"which goal criterion this advances",
  "brief":"what to build, in the owner's terms",
  "done_when":["checkable clause", "..."],
  "decides_alone":["what the owner settles without asking"],
  "keep_compatible":["what must keep working"],
  "evidence":"what will establish completion",
  "integrator":"handoff whenever this outcome lands a file in the project (any code / done-when outcome); otherwise who takes the result",
  "sources":["paths the owner is handed"],
  "needs":["names of outcomes that must land first"],
  "worker":null,
  "tools":false,
  "oracle":"python assert run in the workspace, or empty",
  "oracle_covers_done_when":true,
  "artifact":"<file the worker delivers, e.g. refund.py; default output.md>",
  "dest":"<package-relative path where that file lands in the project, e.g. game/history.py, or empty>",
  "provides":"<the exact public call this outcome exposes, e.g. simulate(route) -> dict; empty only for a non-code outcome>",
  "consumer":"<who calls `provides`: another outcome's dest (e.g. engine.py) or the launcher; empty only if nothing consumes it>",
  "require_consumer":true
}]}

`worker` stays null unless one specific worker is required: the scheduler selects by capability.
Use `needs` for real ordering only -- independent outcomes must run in parallel."""

# The count comes from loop_config, which is the one place that knows the fleet's dials and which
# already carries the note that raising the ceiling alone does not fix the errand problem -- the
# ASK has to change too. Formatting it in means the dial and the prompt cannot drift apart.
# (str.replace, not str.format: the prompt is mostly a JSON skeleton and format() would try to
# read every brace in it as a field.)
try:
    import sys as _sys
    _sys.path.insert(0, str(ROOT))
    from loop_config import LOOP as _LOOP
    _LO, _HI = _LOOP.planner_min_jobs, _LOOP.planner_max_jobs
except Exception:                       # noqa -- a missing dial must not stop planning
    _LO, _HI = 1, 5
PLAN_SYS = PLAN_SYS.replace("{lo}", str(_LO)).replace("{hi}", str(_HI))
PLAN_SYS += """
WORKSPACE RULE (2026-09-20, from a planner-produced goal that crashed six times). A worker, with or
without tools, can only write INSIDE its own workspace; the tool service refuses absolute paths
and cannot see the operator's project directory. Therefore: set BOTH "artifact" AND "dest" to the
SAME package-relative path of the file this outcome delivers (e.g. both "game/history.py", or both
"tests/test_game.py" for a test file in a tests/ package) -- never a bare basename in "artifact"
while "dest" carries a subdirectory, because a basename-only artifact is written at the workspace
root and is not the file the project imports. Set "integrator" to exactly "handoff" for EVERY outcome
that lands a file in the project (every code / done-when outcome, including world/engine modules, the
launcher, and the test suite): an outcome that lands a project file with any other integrator is
rejected before dispatch. Write the oracle to import it from the workspace --
start it with `import sys, os; sys.path.insert(0, os.getcwd())` -- never from an absolute path. Put any
project modules the deliverable must call in "sources" so their real API can be handed to the
worker. Prefer "tools": false unless the work genuinely needs to run or read files. When the
oracle mechanically checks every done_when clause, set "oracle_covers_done_when": true; otherwise
every clause is prose and the result waits for a person (awaiting-review) even after the oracle
passes.
When the goal names a project, set "dest" to the package-relative path of the file this
outcome owns (the same path as "artifact" when the deliverable is that file). A component
oracle checks that outcome only. It must not be a copy of the whole-project integration
check, and it must not be `assert True` when the outcome is supposed to establish behavior.

ORACLE AUTHORING. An oracle is a hard veto on an objective property. It is not a quality judge.
- Code: check the behavior the done_when names (a call, a return shape, a raised error). Names, paths, and glyphs stay exact.
- Prose: do not invent a case-sensitive keyword list. If a word must appear, compare case-insensitively and say so in the assertion message. Do not require lowercase when the request said "a silhouette".
- The assertion message must say what was required and what comparison was used. A bare `assert expr` becomes "Traceback" in the worker's next turn.
- Set "oracle_covers_done_when" only when every done_when clause is actually checked. A handful of keywords does not cover a portrait, a tone, or a scene. Leave the flag false and let the skeptic and the judge read the file.
- Do not write an oracle whose only job is to reject a blank. A missing file is a delivery failure, handled before any content check.
"""



def parse_plan(text):
    """Pull the outcome list out of a frontier model's reply. Tolerates prose around the JSON."""
    a, b = (text or "").find("{"), (text or "").rfind("}")
    if a < 0 or b < 0:
        raise ValueError(f"planner returned no JSON:\n{(text or '')[:400]}")
    doc = json.loads(text[a:b + 1])
    items = doc.get("outcomes") or doc.get("jobs") or []
    return [as_contract(i) for i in items]

PLAN_SYS += """
CONSUMER CONTRACTS (2026-09-23). Every CODE outcome is a behaviour contract for a NAMED consumer, so
the whole plan connects and a milestone launcher can exercise the pieces together:
  - `provides` is the exact public call the outcome exposes (a signature, e.g. `simulate(route) -> dict`).
  - `consumer` is who calls it -- another outcome's `dest` (the downstream file), or the launcher.
  - a downstream outcome lists the upstream outcome name in `needs` and calls its `provides`; do NOT
    reimplement the upstream. At least one outcome should be consumed by the launcher/milestone.
Leave `provides`/`consumer` empty ONLY for a genuinely non-code outcome (a document). A code outcome
with no consumer or no public call is rejected before dispatch."""

PLAN_SYS += """
TEST OUTCOMES (2026-09-23). A test/regression outcome must: (a) use ONLY the standard library test
runner -- `python -m unittest` -- when the scope forbids non-stdlib dependencies (never pytest or
another third-party runner); (b) call each module under test through its ACTUAL public interface --
the exact `provides` of the outcome it `needs` (do not invent a different function name); (c) have an
oracle that RUNS the suite and passes/fails on its real result -- it must NOT "certify" the tests by
grepping their names or printed output for keywords. The anti-regression guarantee comes from the
tests actually exercising the real behaviour, not from string-matching."""
