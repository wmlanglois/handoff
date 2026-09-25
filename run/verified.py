"""The capstone loop: a carded job run to a ruling -- and, since the delta wiring, a
continue / park / stop decision taken from ENVIRONMENT STATE rather than from prose.

Per round:
  1. worker produces output (guided by the prior rejection on a REDO)
  2. output is written to the job workspace as an artifact
  3. Spark (jury) reads the artifact and returns a grounded note
  4. the architect (judge) rules ACCEPT or REDO, given the card + output + note
  5. a progress.Snapshot is built FROM DISK -- oracle result, tool evidence, goal criteria,
     artifact hashes -- and progress.delta() says what actually moved
  6. decide() turns that delta into continue / accept / park. ACCEPT needs the judge AND the
     environment; a park needs progress.stagnant(), not a model's opinion.
  7. a RECEIPT is written: what moved each round, what was accepted, on what basis.

WHY THE DECISION MOVED OFF THE MODEL'S NARRATIVE
------------------------------------------------
The halt used to be `stuck(prev_reason, reason)`: a cheap Spark call asked "is this the SAME
problem as last round?" and the loop parked when a model said STUCK. That is the reporter trap
with the judge's robe on -- the run's termination depended on prose ABOUT the run, and prose is
exactly what a stalled worker keeps producing freshly. docs/RESEARCH-automation-reporter-trap.md
6.1: an independent state check drops false success from 44-52% of failures to 3%. So the halt is
now progress.stagnant() over deltas computed from artifacts, and `Snapshot.outcome` (the only text
that enters at all) is hashed for REPETITION detection only -- never scored as progress.

Three measurements, never one (internal/docs/MILESTONES.md):
  activity      -- the machinery is live. Licenses NOTHING; a rewritten file is activity.
  investigation -- new tool evidence entered. Buys ONE more attempt, inside a budget.
  achievement   -- a criterion was met / a check flipped FAIL->PASS with nothing regressing.

Two guards worth naming, because both have a test that fails without them:
  * The artifact under judgement is excluded from `evidence`. If the answer file counted as
    evidence, every rewrite would register "new evidence" and buy another attempt forever --
    churn laundered into investigation.
  * A judge's ACCEPT cannot outvote an unmet mechanical criterion. The judge rules on the prose;
    the criteria ran. Ground truth wins.

done_when clauses are grounded through run/grounding.py, which never invents a check for a prose
clause: those become UNGROUNDABLE and route to human sign-off instead of blocking the machine
forever. The receipt says which ones are still waiting on a person.

    python run/verified.py run/cards/example.json
"""
import argparse, json, os, re, subprocess, sys, time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from safeio import force_utf8   # noqa
force_utf8()                    # non-cp1252 model/authority text must not crash the print path
import call as call_layer        # noqa  -- the provenance register lives there
from call import chat            # noqa
from fleet import SKEPTIC_WORKER  # noqa
import evidence                  # noqa  -- acceptance must present a sealed record
from loop_config import LOOP as _LOOP  # noqa  -- single source for loop policy
from log import logger          # noqa
import progress                 # noqa  -- the delta core; imported, never edited from here
import grounding                # noqa  -- done_when -> criteria, honest about what is checkable
sys.path.insert(0, str(ROOT / "skeptic"))
import skeptic                  # noqa
sys.path.insert(0, str(ROOT))
import architect                # noqa
sys.path.insert(0, str(ROOT / "tools"))
try:
    import regdb                # optional; a private corpus is not part of this tree
except ImportError:
    regdb = None
sys.path.insert(0, str(ROOT / "web"))
try:
    import scout                # optional; not part of this tree
except ImportError:
    scout = None

def retrieve_units(grounding_spec):
    """Pull real authority BEFORE the worker runs (retrieval-augmented, like the advisor).
    Returns [(evidence_id, text)] rather than one blob: each retrieved unit is a tool result that
    entered the run, and the delta needs those identities to tell 'looked something new up' from
    'said the same thing again'. Deterministic retrieval, not a tool the weak worker has to
    remember to call."""
    units = []
    if regdb is None and not grounding_spec.get("scout"):
        return units
    for sec in grounding_spec.get("sections", []):
        if regdb is None:
            break
        r = regdb.section(sec)
        for u in (r.get("results") or [])[:3]:
            units.append((f"regdb.section:{sec}:{u['source']} {u['section']}",
                          f"[{u['source']} {u['section']}] {u['text'][:800]}"))
    for q in grounding_spec.get("search", []):
        if regdb is None:
            break
        r = regdb.search(q, limit=3)
        for u in (r.get("results") or []):
            units.append((f"regdb.search:{q}:{u['source']} {u['section']}",
                          f"[{u['source']} {u['section']}] {u['text'][:500]}"))
    for q in grounding_spec.get("scout", []):
        if scout is None:
            units.append((f"scout:{q}", "scout is not installed in this checkout"))
            continue
        s = scout.scout(q)
        if s.get("ok") and s.get("citations"):
            units.append((f"scout:{q}", "CITATIONS: " + ", ".join(s["citations"])))
    return units

def retrieve(grounding_spec):
    """The authority text alone, for callers that do not track evidence identities."""
    return "\n\n".join(t for _, t in retrieve_units(grounding_spec))

def _tool_lineage_token(ws):
    """A stable id for THIS logical verified execution, minted once and persisted in the run
    workspace. A --resume of the SAME run reuses the same run_id -> same workspace dir -> same
    token (one lineage across process restarts and outer attempts), while a genuinely new run has a
    new run_id -> new workspace -> a fresh token. It is threaded into every tool attempt so a
    receipt written in an earlier attempt binds in a later one ONLY when both belong to this
    execution -- not merely because they reused the same tool-service workspace NAME."""
    import uuid
    pth = Path(ws) / "tool_lineage.txt"
    try:
        tok = pth.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = uuid.uuid4().hex
    try:
        pth.parent.mkdir(parents=True, exist_ok=True)
        pth.write_text(tok, encoding="utf-8")
    except OSError:
        pass
    return tok


def tool_workspace_id(name):
    """The tool service's job id for this run. Run ids carry the goal (`totals.g-2026...`) and the
    tool service rejects '.' in a job id ("Invalid job ID" on every files call, live
    2026-09-20: a planner-produced tools card crashed six times). Only [A-Za-z0-9_-] survive."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", "verified-" + str(name))


SKILL_MARK_OPEN, SKILL_MARK_CLOSE = "<<SKILLS>>", "<</SKILLS>>"

_VERIFY_WRAPPER = """import sys, os
_ws = os.path.realpath(os.getcwd())
sys.path.insert(0, _ws)
_MODULE = {module!r}
_DEPS = {deps!r}
try:
    exec(compile({verify!r}, '<skill-verify>', 'exec'))
except SystemExit as e:
    if e.code not in (0, None):
        print('VERIFY_FAILED: SystemExit ' + str(e.code)); raise SystemExit(2)
except (OSError, ImportError) as e:
    # missing fixture/data file, unreadable path, missing module: the ENVIRONMENT the check
    # ran in lacked something, which says nothing about the skill's correctness
    print('VERIFY_ENV: ' + type(e).__name__ + ': ' + str(e)[:300]); raise SystemExit(4)
except BaseException as e:
    print('VERIFY_FAILED: ' + type(e).__name__ + ': ' + str(e)[:300]); raise SystemExit(2)
if _MODULE not in sys.modules:
    print('VERIFY_INVALID: the check never imported the staged module ' + _MODULE); raise SystemExit(3)
_bad = []
_std = set(getattr(sys, 'stdlib_module_names', ())) | set(['__future__', '__main__'])
_interp = [os.path.realpath(_p) + os.sep for _p in (sys.base_prefix, sys.prefix, sys.base_exec_prefix, sys.exec_prefix) if _p]
_staged = dict()
_staged[_MODULE] = os.path.join(_ws, _MODULE + '.py')
for _d in _DEPS:
    _staged[_d] = os.path.join(_ws, _d + '.py')
for _n, _m in list(sys.modules.items()):
    if _n.split('.')[0] in _std or _m is None:
        continue
    _f = getattr(_m, '__file__', None)
    if not _f:
        continue
    _rf = os.path.realpath(_f)
    if any(_rf.startswith(_i) for _i in _interp):
        continue
    if _n in _staged:
        # the staged module must be THE staged file, not a same-named copy in a subdirectory
        if os.path.normcase(_rf) != os.path.normcase(_staged[_n]):
            _bad.append(_n + ' <- ' + _f + ' (expected ' + _staged[_n] + ')')
        continue
    if os.path.normcase(os.path.dirname(_rf)) != os.path.normcase(_ws):
        _bad.append(_n + ' <- ' + _f)
if _bad:
    print('VERIFY_INVALID: modules loaded from outside the destination: ' + '; '.join(_bad)); raise SystemExit(3)
print('VERIFY_OK')
"""


def _run_check(script, cwd, timeout=60):
    """Same isolation as run_oracle (-I, scrubbed env), but the exit code is kept: 0 verified,
    2 the skill's own check failed, 3 the check was invalid as a destination check."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTHON") and k not in ("PYTHONPATH", "PYTHONHOME")}
    try:
        r = subprocess.run([sys.executable, "-I", "-c", script], cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()[-600:]
    except Exception as e:
        return 1, "check error: {0}".format(e)


def _stdlib():
    return set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}


_TOOL_VERIFY = """import sys, os
_ws = os.path.realpath(os.getcwd())
sys.path.insert(0, _ws)
_TARGET = {import_as!r}
_DEST = os.path.realpath(os.path.join(_ws, {dest!r}))
try:
    exec(compile({verify!r}, '<skill-verify>', 'exec'))
except SystemExit as e:
    if e.code not in (0, None):
        print('VERIFY_FAILED: SystemExit ' + str(e.code)); raise SystemExit(2)
except (OSError, ImportError, ModuleNotFoundError) as e:
    print('VERIFY_ENV: ' + type(e).__name__ + ': ' + str(e)[:300]); raise SystemExit(4)
except BaseException as e:
    print('VERIFY_FAILED: ' + type(e).__name__ + ': ' + str(e)[:300]); raise SystemExit(2)
_m = sys.modules.get(_TARGET)
if _m is None:
    print('VERIFY_INVALID: the check never imported ' + _TARGET); raise SystemExit(3)
_f = os.path.realpath(getattr(_m, '__file__', '') or '')
if os.path.normcase(_f) != os.path.normcase(_DEST):
    print('VERIFY_INVALID: ' + _TARGET + ' loaded from ' + _f + ' not the staged ' + _DEST); raise SystemExit(3)
print('VERIFY_OK')
"""


def _dotted_from_dest(dest):
    """The import identity a file at `dest` presents: game/history.py -> game.history."""
    d = str(dest).replace("\\", "/")
    if d.endswith(".py"):
        d = d[:-3]
    return d.replace("/", ".")


def _toolenv_stage_files(ws, staged_dest, sdata, deps, inputs):
    """The skill plus the dependencies and package inputs it needs, as bytes. Ordinary tool-env
    verification stages this set into the tool service; a skill file alone is not enough when the
    check imports a package neighbor."""
    files = [{"dest": staged_dest, "data": sdata}]
    seen = {staged_dest}
    for item in list(deps or []) + list(inputs or []):
        dest = str(item.get("dest") or item.get("name") or "").replace("\\", "/")
        if not dest or dest in seen:
            continue
        raw = None
        if item.get("bytes") is not None:
            raw = item["bytes"]
        else:
            loc = Path(item["location"]) if item.get("location") else None
            disk = (Path(ws) / dest) if ws else None
            if disk is not None and disk.is_file():
                raw = disk.read_bytes()
            elif loc is not None and loc.is_file():
                raw = loc.read_bytes()
        if raw is None:
            continue
        files.append({"dest": dest, "data": raw})
        seen.add(dest)
    return files


def _toolenv_verify(card, staged_dest, sdata, script, emit, deps, inputs):
    """Run the skill check in the tool service on every ordinary tools dispatch. A local shadow
    pass is not availability. Returns 'ok' / 'defective' / 'unavailable'."""
    try:
        import tooljob
        wsid = tool_workspace_id((card.get("name") or "skill")) + "-skillverify"
        stage_files = _toolenv_stage_files(None, staged_dest, sdata, deps, inputs)
        # Prefer bytes already recorded on the dep rows; fall back to locations.
        return tooljob.verify_in_tool_service(wsid, stage_files, script, emit=emit)
    except Exception as exc:
        return "unavailable", "tool-environment verify could not run: {0}".format(str(exc)[:200])


def _stage_skills_toolpath(card, ws, skills, deps, emit, memory):
    """Tool-path retained-skill lifecycle. The tool worker runs in the tool service; this stages the
    same bytes into a local destination copy WITH the package material and runs each skill's OWN
    behavioral check against that copy, so availability is truthful before anything is advertised:

      * dependencies are staged at their package-relative dest, plus the package __init__ chain;
      * a retained skill whose module coincides with a project dependency is delivered THROUGH that
        dependency (verified in place, NO duplicate root copy); if the bytes differ it is refused
        truthfully and the valid dependency is left untouched;
      * a standalone skill is staged at its own module and verified;
      * verification runs the skill's `verify` and requires the target module to have loaded from the
        exact staged file -- a successful upload/hash is never reported as the behavioral check.
    """
    ws = Path(ws)
    ws.mkdir(parents=True, exist_ok=True)
    out, provenance, dep_by_mod = [], [], {}
    for d in deps:
        loc = Path(d.get("location") or "")
        dest = (d.get("dest") or ((d.get("module") or "") + ".py")).replace("\\", "/")
        if not loc.is_file():
            out.append({"id": None, "module": d.get("module"), "kind": "dependency", "staged": False,
                        "verified": False, "available": False, "attribution": "environment",
                        "reason": "source missing: {0}".format(loc), "dest": dest})
            continue
        data = loc.read_bytes()
        if d.get("sha256") and evidence.sha256(data) != d["sha256"]:
            out.append({"id": None, "module": d.get("module"), "kind": "dependency", "staged": False,
                        "verified": False, "available": False, "attribution": "environment",
                        "reason": "source changed since it was verified: {0}".format(loc), "dest": dest})
            continue
        p = ws / dest
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        dep_by_mod[d.get("module")] = {"dest": dest, "bytes": data, "location": str(loc)}
        out.append({"id": None, "module": d.get("module"), "kind": "dependency", "staged": True,
                    "verified": True, "available": True, "attribution": "dependency", "reason": "",
                    "dest": dest})
        provenance.append({"module": d.get("module"), "to": str(p), "sha256": evidence.sha256(data)})
    for inp in (card.get("inputs") or []):
        dest = (inp.get("dest") or inp.get("name") or "").replace("\\", "/")
        loc = Path(inp.get("location") or "")
        if dest.endswith("__init__.py") and loc.is_file():
            p = ws / dest
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(loc.read_bytes())

    deliverable = Path(card.get("artifact") or "output.md").stem
    for sk in skills:
        mod = sk.get("module")
        row = {"id": sk.get("id"), "module": mod, "kind": "skill", "staged": False,
               "verified": False, "available": False, "attribution": "", "reason": "",
               "dest": None, "import_as": None}
        if not mod:
            out.append(row); continue
        if mod == deliverable:
            row.update(attribution="environment",
                       reason="conflicts with this card's deliverable {0}.py".format(deliverable))
            out.append(row); continue
        loc = Path(sk.get("location") or "")
        if not loc.is_file():
            row.update(attribution="environment", reason="retained skill source missing: {0}".format(loc))
            out.append(row); continue
        sdata = loc.read_bytes()
        if sk.get("sha256") and evidence.sha256(sdata) != sk["sha256"]:
            row.update(attribution="environment", reason="retained skill bytes changed since verification")
            out.append(row); continue
        dep = dep_by_mod.get(mod)
        if dep is not None:
            dest = dep["dest"]
            if dep["bytes"] != sdata:
                row.update(dest=dest, attribution="environment",
                           reason=("the project integrates a different {0} at {1}; the retained skill "
                                   "version is not the one in the project".format(mod, dest)))
                out.append(row); continue
            staged_dest = dest                      # SAME bytes: delivered through the dependency
        else:
            staged_dest = mod + ".py"
            p = ws / staged_dest
            if p.exists() and p.read_bytes() != sdata:
                row.update(attribution="environment",
                           reason="workspace already holds a different {0}".format(staged_dest))
                out.append(row); continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(sdata)
        import_as = _dotted_from_dest(staged_dest)
        row.update(dest=staged_dest, import_as=import_as, staged=True)
        script = _TOOL_VERIFY.format(import_as=import_as, dest=staged_dest,
                                     verify=sk.get("verify") or "raise AssertionError('no verify')")
        rc, msg = _run_check(script, str(ws))
        if rc == 0 and "VERIFY_OK" in msg:
            # A LOCAL shadow subprocess pass establishes the skill works HERE -- NOT that it imports
            # and runs in the tool ENVIRONMENT the worker actually uses. Label it as local-shadow and
            # never advertise remote usability from a local pass (doc 05 #4).
            # Local shadow is recorded only as a label. Availability requires the tool-service
            # check, for a standalone skill and for one that coincides with a dependency.
            neighbors = [{"dest": info["dest"], "bytes": info["bytes"]} for info in dep_by_mod.values()]
            neighbors += list(card.get("inputs") or [])
            te_status, te_msg = _toolenv_verify(card, staged_dest, sdata, script, emit, neighbors, [])
            if te_status == "ok":
                row.update(verified=True, available=True, attribution="verified",
                           environment="tool-service", tool_env="verified", reason="")
            elif te_status == "defective":
                row.update(verified=False, available=False, attribution="defective",
                           tool_env="defective", environment="tool-service",
                           reason="FAILED in the tool environment: " + te_msg[-200:])
            else:
                row.update(verified=True, available=False, attribution="verified-local-shadow",
                           environment="local-shadow", tool_env="unavailable",
                           reason="tool environment did not verify this skill: " + te_msg[-160:])
        elif rc == 3:
            row.update(attribution="unknown", reason="verification INVALID as a destination check: " + msg[-260:])
        elif rc == 4:
            row.update(attribution="environment", reason="the check needed something this environment lacks: " + msg[-260:])
        elif rc == 2:
            row.update(attribution="defective", reason="the skill's own check failed in a supported environment: " + msg[-260:])
        else:
            row.update(attribution="unknown", reason="verification could not run: " + msg[-260:])
        if memory is not None and sk.get("id"):
            try:
                if row["attribution"] == "verified" and row.get("tool_env") == "verified":
                    # Only a pass inside the tool environment counts as a reliability success.
                    # A local shadow is not evidence the worker's environment can import the skill.
                    memory.record_skill_result(sk["id"], True, by="harness", scope="tool dest {0}".format(ws.name))
                elif row["attribution"] == "defective":
                    memory.record_skill_result(sk["id"], False, by="harness", scope="tool dest {0}".format(ws.name),
                                               note=row["reason"][:260])
                else:
                    memory.record_skill_unknown(sk["id"], row["reason"][:80], by="harness")
            except Exception as e:
                row["reason"] += " (reliability record failed: {0})".format(e)
        if emit is not None:
            emit("skill_staged", skill=row["id"], module=row["module"], staged=row["staged"],
                 verified=row["verified"], available=row["available"],
                 attribution=row["attribution"], reason=row["reason"][:200])
        out.append(row)
    _write_staging(ws, out, provenance)
    return out


def stage_skills(card, ws, emit=None):
    """Make each skill the card offers REAL in this workspace, or say precisely why it is not.

    THE CONTRACT (LF-02 closure gates). "Copied" is not "available" and "the oracle ran" is not
    "the staged artifact was verified":
      1. declared dependencies (`card["deps"]`: the contract's own .py sources) are staged FIRST,
         hash-checked, never over a different file already there;
      2. skills are staged in dependency order; a skill whose declared dependency is not present
         in THIS workspace is UNAVAILABLE (attribution: environment), not verified, not offered;
      3. each staged skill is verified by its own check run in the destination inside a wrapper
         that refuses the result unless the skill module AND its dependencies were imported from
         this workspace -- a check that succeeded by importing another copy, or by an external
         directory on sys.path, is INVALID (attribution: unknown), never a verification;
      4. only a check that failed in a supported environment counts against the skill
         (attribution: defective) and reaches `record_skill_result`;
      5. a skill that is not verified is removed from the workspace again and dropped from the
         worker's brief by `reconcile_brief`, so nothing is advertised that is not there.
    Tool-enabled cards get no staging: their workspace is the tool service's, not this one."""
    out = []
    skills = list(card.get("skills") or [])
    deps = list(card.get("deps") or [])
    if not skills and not deps:
        return out
    ws = Path(ws)
    ws.mkdir(parents=True, exist_ok=True)
    try:
        import memory
    except Exception:
        memory = None
    present = set(_stdlib())            # module names importable from THIS workspace
    provenance = []                     # what was put where, from where

    created = set()                     # files THIS staging wrote; only these may be removed

    def _copy(module, location, sha, dest_rel=None):
        src = Path(location or "")
        if not src.is_file():
            return False, "source missing: {0}".format(src)
        data = src.read_bytes()
        if sha and evidence.sha256(data) != sha:
            return False, "source changed since it was verified: {0}".format(src)
        rel = str(dest_rel or (str(module) + ".py")).replace("\\", "/").lstrip("/")
        dest = ws / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            if dest.read_bytes() != data:
                return False, "workspace already holds a different {0}; not overwritten".format(rel)
            pre_existing = True         # identical bytes already there: use them, never delete them
        else:
            dest.write_bytes(data)
            created.add(module)
            pre_existing = False
        provenance.append({"module": module, "from": str(src), "sha256": evidence.sha256(data),
                           "to": str(dest), "pre_existing": pre_existing})
        return True, ""

    if card.get("tools"):
        # Verify retained skills against the ACTUAL destination copy (package material and all) and
        # advertise only what its own behavioral check passes -- not a blanket "pending" that made
        # the worker be told a delivered module was unavailable.
        return _stage_skills_toolpath(card, ws, skills, deps, emit, memory)

    # 1. declared dependencies
    for d in deps:
        ok, why = _copy(d.get("module"), d.get("location"), d.get("sha256"), d.get("dest"))
        row = {"id": None, "module": d.get("module"), "kind": "dependency", "staged": ok,
               "verified": ok, "available": ok, "attribution": "dependency", "reason": why}
        if ok:
            present.add(d["module"])
        out.append(row)
        if emit is not None:
            emit("dependency_staged", module=row["module"], staged=ok, reason=why[:200])

    # 1b. declared INPUTS: the package __init__ chain, a repair's read-only `_carry/<file>` prior
    # artifact, and non-.py data files. Staged at their real dest so a package-relative worker
    # (`import game.base`) receives an importable package and its accepted-producer dependencies at
    # their logical paths. Without this a package repair could not import through the real executor.
    for inp in (card.get("inputs") or []):
        dest = (inp.get("dest") or inp.get("name") or "").replace("\\", "/").lstrip("/")
        loc = Path(inp.get("location") or "")
        if not dest or not loc.is_file():
            continue
        try:
            data = loc.read_bytes()
        except OSError as e:
            out.append({"id": None, "module": inp.get("name"), "kind": "input", "staged": False,
                        "verified": False, "available": False, "attribution": "environment",
                        "reason": "input unreadable: {0}".format(e), "dest": dest})
            continue
        target = ws / dest
        if target.exists() and target.read_bytes() != data:
            out.append({"id": None, "module": inp.get("name"), "kind": "input", "staged": False,
                        "verified": False, "available": False, "attribution": "environment",
                        "reason": "workspace already holds a different {0}".format(dest), "dest": dest})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        out.append({"id": None, "module": inp.get("name"), "kind": "input", "staged": True,
                    "verified": True, "available": True, "attribution": "input", "reason": "",
                    "dest": dest})
        if emit is not None:
            emit("input_staged", dest=dest, staged=True)

    # 2. skills in dependency order (a skill may depend on another offered skill)
    offered = {sk["module"]: sk for sk in skills if sk.get("module")}
    ordered, seen = [], set()

    def visit(sk, stack=()):
        m = sk["module"]
        if m in seen or m in stack:
            return
        for dname in sk.get("dependencies") or []:
            if dname in offered and dname not in seen:
                visit(offered[dname], stack + (m,))
        seen.add(m)
        ordered.append(sk)
    for sk in skills:
        if sk.get("module"):
            visit(sk)

    deliverable = Path(card.get("artifact") or "output.md").stem
    for sk in ordered:
        row = {"id": sk.get("id"), "module": sk["module"], "kind": "skill", "staged": False,
               "verified": False, "available": False, "attribution": "", "reason": ""}
        missing = [d for d in (sk.get("dependencies") or []) if d not in present]
        if sk["module"] == deliverable:
            # Live (net-total-c5): the accepted `net` skill was offered to a card whose own
            # deliverable is net.py. The worker's first reply would have overwritten the staged
            # skill, and the oracle would then have judged whichever bytes were last written.
            row["attribution"] = "environment"
            row["reason"] = ("conflicts with this card's deliverable {0}.py; a skill cannot share "
                             "the name of the module being written".format(deliverable))
        elif missing:
            row["attribution"] = "environment"
            row["reason"] = "dependency not available in the destination: {0}".format(
                ", ".join(missing))
            if memory is not None and sk.get("id"):
                try:
                    memory.record_skill_unknown(sk["id"], row["reason"][:80], by="harness")
                except Exception:
                    pass
        else:
            ok, why = _copy(sk["module"], sk.get("location"), sk.get("sha256"))
            row["staged"] = ok
            if not ok:
                row["attribution"] = "environment"
                row["reason"] = why
            else:
                # 3. verify THE STAGED ARTIFACT, with provenance enforced
                script = _VERIFY_WRAPPER.format(module=sk["module"],
                                                deps=list(sk.get("dependencies") or []),
                                                verify=sk.get("verify") or "raise AssertionError('no verify')")
                rc, msg = _run_check(script, str(ws))
                if rc == 0 and "VERIFY_OK" in msg:
                    row.update(verified=True, available=True, attribution="verified")
                    present.add(sk["module"])
                elif rc == 3:
                    row["attribution"] = "unknown"
                    row["reason"] = ("verification INVALID as a destination check: "
                                     + (msg.split("VERIFY_INVALID:", 1)[-1].strip()[:300]))
                elif rc == 4:
                    row["attribution"] = "environment"
                    row["reason"] = ("the check needed something this environment lacks: "
                                     + (msg.split("VERIFY_ENV:", 1)[-1].strip()[:300]))
                elif rc == 2:
                    row["attribution"] = "defective"
                    row["reason"] = ("the skill's own check failed in a supported environment: "
                                     + (msg.split("VERIFY_FAILED:", 1)[-1].strip()[:300]))
                else:
                    row["attribution"] = "unknown"
                    row["reason"] = "verification could not run: " + msg[-300:]
                # 4. reliability: only justified attributions reach the record
                if memory is not None and sk.get("id"):
                    try:
                        if row["attribution"] == "verified":
                            memory.record_skill_result(sk["id"], True, by="harness",
                                                       scope="destination workspace {0}".format(ws.name))
                        elif row["attribution"] == "defective":
                            memory.record_skill_result(sk["id"], False, by="harness",
                                                       scope="destination workspace {0}".format(ws.name),
                                                       note=row["reason"][:300])
                        else:
                            memory.record_skill_unknown(sk["id"], row["reason"][:80], by="harness")
                    except Exception as e:      # the store must not stop the job
                        row["reason"] += " (reliability record failed: {0})".format(e)
                # 5. never leave a failed or invalid skill lying in the workspace as if ready --
                #    but only remove what THIS staging wrote; an identical file that was already
                #    there belongs to whoever put it there (audit: it used to be deleted)
                if not row["available"]:
                    if sk["module"] in created:
                        try:
                            (ws / (sk["module"] + ".py")).unlink()
                        except OSError:
                            pass
                    provenance[:] = [x for x in provenance if x["module"] != sk["module"]]
        if emit is not None:
            emit("skill_staged", skill=row["id"], module=row["module"], staged=row["staged"],
                 verified=row["verified"], available=row["available"],
                 attribution=row["attribution"], reason=row["reason"][:200])
        out.append(row)
    _write_staging(ws, out, provenance)
    return out


def _write_staging(ws, rows, provenance):
    try:
        (Path(ws) / "staged-skills.json").write_text(
            json.dumps({"rows": rows, "provenance": provenance}, indent=2), encoding="utf-8")
    except OSError:
        pass


def reconcile_brief(brief, staged):
    """Make the worker's brief agree with what is ACTUALLY in its workspace.

    goals.card_for wrote the skill section between markers on the assumption that staging would
    succeed. Lines for skills that are not available are replaced by one line saying why, and
    composition notes that mention an unavailable module are dropped. A worker is never told a
    module is importable that stage_skills could not make so."""
    if SKILL_MARK_OPEN not in brief or SKILL_MARK_CLOSE not in brief:
        return brief
    head, rest = brief.split(SKILL_MARK_OPEN, 1)
    section, tail = rest.split(SKILL_MARK_CLOSE, 1)
    rows = [r for r in staged if r.get("kind") == "skill"]
    unavailable = {r["module"]: r for r in rows if not r.get("available")}
    bad_ids = {r["id"] for r in rows if not r.get("available") and r.get("id")}
    kept = []
    for line in section.splitlines():
        if line.startswith("- SKILL ") and any(("SKILL " + i) in line for i in bad_ids):
            continue
        if line.startswith("- COMPOSITION") and any("`{0}`".format(m) in line for m in unavailable):
            continue
        kept.append(line)
    for r in rows:
        if not r.get("available"):
            kept.append("- SKILL {0} ({1}) is NOT available in your workspace: {2}. Do not import "
                        "{1}.".format(r.get("id") or "?", r.get("module"), r.get("reason")))
    staged_deps = [r for r in staged if r.get("kind") == "dependency" and r.get("available")]
    if staged_deps:
        kept.append("- STAGED beside your module (import by name): "
                    + ", ".join("`{0}`".format(r["module"]) for r in staged_deps))
    return head + "\n".join(kept).strip("\n") + tail


def run_oracle(expr, cwd):
    """Frozen deterministic gate from the card. Exit 0 = pass. The worker can't argue past it,
    and it only checks the objectively checkable -- the model still judges the rest.

    RUNS ISOLATED (-I). This gate executes with cwd set to the worker's OWN workspace, and for
    `python -c` the cwd is sys.path[0]. Without isolation the worker could defeat the frozen oracle
    by planting a module: drop a three-line json.py beside output.md and `import json` inside the
    oracle expression resolves to the worker's file first. Reproduced 2026-09-19 -- the oracle
    returned 0 against a planted stdlib shadow, so the gate passed on a lie. `-I` drops cwd from
    sys.path and ignores PYTHON* environment variables and user site-packages, which is the whole
    attack surface. The environment is scrubbed for the same reason.

    Found by reading NEEDLE's validation module, which extracts a pinned revision into a fresh
    tempdir rather than ever running a gate in the directory the agent just wrote to. That is the
    stronger construction and the right eventual fix; this is the one-flag version of it."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("PYTHON") and k not in ("PYTHONPATH", "PYTHONHOME")}
    try:
        r = subprocess.run([sys.executable, "-I", "-c", expr], cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=60)
        out = (r.stdout + r.stderr).strip()
        # Keep the exception line whole. A one-line oracle's traceback echoes the whole source
        # line, so a 300-char tail could cut "AssertionError: <reason>" down to its last four
        # characters (live: "nt')"), and the worker, the architect and the lesson store all lost
        # the reason the check was written to give them.
        exc = [ln for ln in out.splitlines() if _EXC_LINE.match(ln)]
        head = (exc[-1][:400] + "\n") if exc and exc[-1] not in out[-300:] else ""
        return r.returncode == 0, head + out[-300:]
    except Exception as e:
        return False, f"oracle error: {e}"

# The last line of a traceback: "<Qualified.Name>: <detail>". Not only *Error/*Exception --
# live, `decimal.InvalidOperation: [...]` slipped past an Error-only pattern and the receipt
# showed a frame line instead of the reason.
_EXC_LINE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Operation|Warning|Exit|Interrupt|Iteration)\b:\s*\S")


def _diag_empty(text):
    t = (text or "").strip().lower()
    return (not t) or t.startswith("skeptic: (no note)") or t.startswith("skeptic error") or t.startswith("(no ")


def _run_diagnose(hooks, rnd, reason, output, review_root):
    """Invoke the FRESH failure-diagnosis hook (a real review of THIS workspace, never cached
    post-success feedback). Returns (text, ran). An exception means it could not run."""
    try:
        txt = hooks.diagnose(rnd, reason, output, review_root)
        return (str(txt or ""), True)
    except Exception:
        return ("", False)


def _failure_kind(reason):
    r = (reason or "").lower()
    if any(k in r for k in ("no module named", "modulenotfound", "importerror", "no such file",
                            "does not exist", "not accessible")):
        return "missing_inputs_or_tools"
    if any(k in r for k in ("has no attribute", "attributeerror", "unexpected keyword",
                            "positional argument", "takes no", "not callable", "typeerror")):
        return "interface_mismatch"
    if any(k in r for k in ("no module named 'split'", "name 'split'", "no code", "fenced")):
        return "no_deliverable_produced"
    return "unsuccessful_strategy"


def oracle_reason(oracle_msg):
    """Plain-language reason an oracle failed: its own assert message if present, else the raw tail.
    This is what makes an oracle REDO explicit ('no bracketed citation') instead of a cryptic traceback."""
    text = oracle_msg or ""
    m = re.search(r"AssertionError:[ \t]*(.+)", text)
    if m and m.group(1).strip():
        return m.group(1).strip().splitlines()[0]
    # A bare `assert expr` has no message. Handing the worker the traceback's first line
    # ("Traceback (most recent call last):") made it keep the file unchanged.
    if re.search(r"^AssertionError\s*$", text, re.M):
        return "an assertion failed and the check gave no message"
    exc = [ln for ln in text.splitlines() if _EXC_LINE.match(ln)]
    if exc:
        return exc[-1].strip()      # keep the exception's name: "KeyError: 'boxes'" is the reason
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return (lines[-1] if lines else "") or "a deterministic check"


# =================================================================================================
# ENVIRONMENT STATE
# Everything in this section reads DISK and PROCESS EXIT CODES. Nothing in it reads what the
# worker, the skeptic or the judge SAID. That separation is the whole point of the module.
# =================================================================================================

ORACLE_CHECK = "card_oracle"     # the slot the card's frozen oracle occupies in Snapshot.oracle
# Files the loop itself writes into the workspace. They are bookkeeping, not the worker's tool
# output, and counting them as evidence would make the loop's own receipt look like discovery.
# output.md is the worker's reply, written by this loop, not a tool result. Counting it (or the
# per-round copy under attempts/) as evidence made a non-tool worker's identical reply look like
# investigation, so the loop spent its whole budget on a check that never moved.
LOOP_FILES = {"result.json", "receipt.md", "receipt.json", "output.md"}


def criteria_from_card(card):
    """card['done_when'] -> [grounding.Criterion]. The grammar, kept from grounding.py: a clause
    that cannot be checked mechanically gets NO invented check.

    A bare string is prose -- UNGROUNDABLE, routed to human sign-off, and it does NOT block the
    machine (otherwise every existing card, whose done_when is four English sentences, could never
    finish and the loop would burn its whole round budget waiting on something unobservable).
    A dict may carry `check` (a python expression run in the workspace, exit 0 = met), `params`
    (a None value means the slot is declared and UNSET -- frozen and inert, never defaulted), and
    `proxy` (a stand-in that counts only once the user has `accepted` it)."""
    # DECLARED oracle coverage. A card may state that its frozen oracle IS the mechanical check
    # for its done_when clauses. This is never inferred: the architect who wrote the oracle asserts
    # the binding and owns it, because guessing that an oracle happens to cover a sentence is
    # exactly the invented check this grammar forbids.
    #
    # Found 2026-09-19 by the first unattended goal run. Every clause was prose, so every criterion
    # was UNGROUNDABLE, so three outcomes whose oracle had mechanically verified them all landed
    # awaiting-review instead of accepted. Their dependent needed them ACCEPTED, so it never became
    # ready and the goal stopped BLOCKED with correct working code sitting on disk. The loop was
    # honest and useless at the same time.
    covered = bool(card.get("oracle")) and bool(card.get("oracle_covers_done_when"))
    out = []
    for i, entry in enumerate(card.get("done_when") or [], 1):
        if isinstance(entry, str):
            if covered:
                out.append(grounding.Criterion(
                    id=f"g{i}", quote=entry, source="done_when", state=grounding.GROUNDED,
                    check=ORACLE_COVERED))
            else:
                out.append(grounding.Criterion(id=f"g{i}", quote=entry, source="done_when",
                                               state=grounding.UNGROUNDABLE))
            continue
        check = entry.get("check") or ""
        params = dict(entry.get("params") or {})
        if not check:
            state = grounding.UNGROUNDABLE
        elif entry.get("proxy"):
            state = grounding.PROXY
        elif any(v is None for v in params.values()):
            state = grounding.PENDING_PARAM
        else:
            state = grounding.GROUNDED
        out.append(grounding.Criterion(
            id=entry.get("id") or f"g{i}", quote=entry.get("quote") or entry.get("text") or "",
            source="done_when", state=state, check=check, params=params,
            proxy_note=entry.get("proxy_note", ""), accepted=bool(entry.get("accepted"))))
    return out


# A criterion whose check is this sentinel is satisfied exactly when the card's frozen oracle
# passed. It is not re-executed: the oracle already ran, and running it twice would let the two
# answers disagree.
ORACLE_COVERED = "<frozen oracle>"


def evaluate_criteria(criteria, cwd, runner=run_oracle, oracle_ok=None):
    """{criterion_id: bool} for the criteria a machine may rule on RIGHT NOW.

    Inert criteria (unset parameter, unaccepted proxy, ungroundable prose) are ABSENT from the
    result, not False. A frozen slot reported as False would read as a failing check and would keep
    the run alive forever chasing a criterion that cannot fire."""
    results = {}
    for c in criteria:
        if not c.evaluable:
            continue
        if c.check == ORACLE_COVERED:
            # Absent, not False, when the oracle verdict is unknown: a criterion reported False
            # would read as a failing check and keep the run chasing something that never ran.
            if oracle_ok is not None:
                results[c.id] = bool(oracle_ok)
            continue
        expr = c.check.format(**c.params) if c.params else c.check
        ok, _msg = runner(expr, cwd)
        results[c.id] = bool(ok)
    return results


def workspace_evidence(root, artifacts=()):
    """Tool evidence = the files a tool-using round left in the workspace, by content identity.

    The artifact under judgement is EXCLUDED. This is not tidiness: Delta.investigation buys the
    loop another attempt, so if the answer file counted as evidence a worker rewriting its answer
    would earn an unlimited extension by doing nothing -- the exact laundering of churn into
    progress this module exists to stop."""
    base = Path(root)
    if not base.is_dir():
        return frozenset()
    skip = {Path(a).name for a in artifacts} | LOOP_FILES
    files = [f for f in sorted(base.rglob("*")) if f.is_file() and f.name not in skip]
    return frozenset(f"tool:{Path(p).relative_to(base).as_posix()}#{h[:12]}"
                     for p, h in progress.hash_files(files).items())


def snapshot(review_root, artifacts, oracle_ok, goal_results, evidence, in_flight=(), outcome=""):
    """The environment after a round, assembled from artifacts only.

    `outcome` is the worker's text. It goes in because progress.Snapshot hashes it to notice a
    worker repeating itself verbatim; it is NEVER a progress signal (test_progress.py pins that)."""
    # An oracle may publish per-stage results to stages.json. When it does, each stage becomes its
    # own check in the snapshot, so the delta can see a run advance from 3/7 to 5/7 instead of
    # reporting CHURN because one all-or-nothing boolean stayed False. Observed directly: the
    # worker fixed a real URL bug between rounds and every round still read as CHURN.
    checks = {ORACLE_CHECK: "PASS" if oracle_ok else "FAIL"}
    try:
        st = json.loads((Path(review_root) / "stages.json").read_text(encoding="utf-8"))
        for row in st.get("stages") or []:
            checks["stage:" + str(row.get("name"))] = "PASS" if row.get("ok") else "FAIL"
    except (OSError, ValueError, TypeError):
        pass

    return progress.Snapshot(
        artifacts=progress.hash_files([Path(review_root) / a for a in artifacts]),
        oracle=checks,
        evidence=frozenset(evidence or frozenset()),
        goal=dict(goal_results or {}),
        in_flight=frozenset(in_flight or ()),
        outcome=outcome or "",
    )


def movement_label(d):
    """One word for the receipt. Ordered by what a reader most needs to know: going backwards
    outranks everything, and achievement outranks the mere fact that something happened."""
    if d.regressions or d.goal_lost:
        return "REGRESSION"
    if d.achievement:
        return "ACHIEVEMENT"
    if d.investigation:
        return "INVESTIGATION"
    if d.waiting:
        return "WAITING"
    if d.churn_only:
        return "CHURN"
    return "NO CHANGE"


# =================================================================================================
# =================================================================================================
# EVIDENCE: acceptance must present a record binding the worker's output to the accepted bytes
# =================================================================================================
# Before this, ACCEPT was `oracle_ok and no unmet criteria and the judge said ACCEPT`. None of those
# establishes that the bytes being accepted are the bytes the worker produced -- a run could pass a
# check against an artifact it did not write. evidence.seal() refuses unless the artifact IS the
# output or is reproducible from it, so the record cannot exist for a run whose output never became
# the artifact. A missing record is a REFUSAL, never a silent pass.

try:
    from fleet import WORKERS as _FW
    _WORKER_MODELS = {k: v.get("model") for k, v in _FW.items()}
except Exception:          # settings absent (clean clone / CI): the model name is
    _WORKER_MODELS = {}    # provenance, not a control -- never block sealing on it

VERIFIER = evidence.Verifier(id="fleet-oracle", version="1")
TRUSTED_VERIFIERS = {(VERIFIER.id, VERIFIER.version)}


def _artifact_path(review_root, artifacts):
    return Path(review_root) / (artifacts[0] if artifacts else "output.md")


def verify_round(job_id, rnd, review_root, artifacts, oracle):
    """Run the verifier and return (ok, message, VerifierResult) bound to THESE bytes.

    The artifact is hashed before and after the check. If it changed underneath, the verdict is
    about bytes that no longer exist and is discarded as a FAIL with that reason -- a check whose
    subject moved is not a check. The result carries the job, the attempt and the artifact hash,
    so the verdict cannot later be presented for a different attempt (see evidence.VerifierResult).

    `ok` is ALWAYS the oracle's own answer, never overridden here. The oracle is the card's frozen
    check and may legitimately test things other than the named artifact; rewriting its verdict
    because this function could not hash a file would put this function in the judging business.
    What moves instead is the RESULT, which is what acceptance binds against:

      * artifact missing  -> no result at all. There is nothing for a verdict to be about, and
        seal_round then fails to read the artifact, which is already the PARK_UNSUPPORTED path.
      * artifact changed  -> a FAIL result over the bytes now on disk. The oracle may well have
        passed, but it passed on bytes that are gone, and a PASS re-pointed at replacement bytes
        is exactly the stale-evidence acceptance this track exists to stop."""
    path = _artifact_path(review_root, artifacts)
    before = evidence.sha256_file(path) if path.exists() else None
    ok, msg = oracle(review_root)
    after = evidence.sha256_file(path) if path.exists() else None
    if after is None:
        return ok, (msg or ""), None
    if before != after:
        return ok, (msg or "") + " [artifact changed during verification]", evidence.VerifierResult(
            verifier=VERIFIER, verdict=evidence.FAIL, job_id=job_id, attempt=rnd,
            artifact_sha256=after, ran_at=time.time(),
            detail="the artifact was rewritten while the verifier ran; the verdict is not about "
                   "the bytes now on disk")
    return ok, msg, evidence.VerifierResult(
        verifier=VERIFIER, verdict=evidence.PASS if ok else evidence.FAIL, job_id=job_id,
        attempt=rnd, artifact_sha256=after, ran_at=time.time(), detail=(msg or "")[:200])


# ARTIFACT WRITES USE newline="" TO DEFEAT NEWLINE TRANSLATION.
# On Windows, text-mode write rewrites line endings, so the bytes on disk are NOT the
# bytes the worker produced, and evidence.seal() -- which compares them exactly -- could
# never bind a multi-line artifact. The gate built to protect acceptance was instead
# refusing every real one: three correct modules landed parked-unsupported in the first
# goal run that reached the accept branch. Earlier unit tests passed only because their
# fixture strings contained no line breaks, which is why this survived to a live run.


def seal_round(job_id, rnd, worker, output, review_root, artifacts, oracle_ok, result=None,
               capture=None, transform=None, transform_name=None):
    """Seal this round's output against the artifact on disk. Returns a record or None.

    None is not an error here -- it means the artifact could not be bound to the output, which is
    exactly the condition acceptance must refuse on. `capture` is the call layer's record of the
    response (call.chat telemetry); when the hooks supply one, the output stops being this
    function's assertion and becomes an observation. `result` binds the verdict to this attempt."""
    if output is None:
        return None
    path = _artifact_path(review_root, artifacts)
    try:
        art = path.read_bytes()
    except OSError:
        return None
    req = evidence.WorkerRequest(worker=worker or "?", model=str((_WORKER_MODELS.get(worker) or "?")),
                                 prompt_sha256=evidence.sha256(evidence._as_bytes(job_id)),
                                 params={"round": rnd})
    try:
        return evidence.seal(job_id=job_id, attempt=rnd, request=req, tool_results=(),
                             worker_output=output, artifact_bytes=art,
                             verifier=VERIFIER, verdict=evidence.PASS if oracle_ok else evidence.FAIL,
                             capture=capture, result=result,
                             transform=transform, transform_name=transform_name)
    except evidence.Unbindable:
        return None          # the artifact is not derived from this round's output


def evidence_supports(record, job_id, rnd, review_root, artifacts, require_captured=False):
    """Re-check the record against the bytes on disk RIGHT NOW. Returns (ok, reason)."""
    if record is None:
        return False, ("no evidence record for this round: the artifact on disk is not the bytes "
                       "the worker produced, so nothing binds the result to the work")
    try:
        art = _artifact_path(review_root, artifacts).read_bytes()
    except OSError as e:
        return False, f"artifact unreadable at acceptance time: {e}"
    d = evidence.accept(record, job_id, rnd, art, TRUSTED_VERIFIERS,
                        require_captured=require_captured)
    return bool(d), ("evidence bound to artifact " + evidence.sha256(art)[:12] +
                     " (" + record.provenance.split(":")[0] + " output"
                     + (", attempt-bound verdict)" if record.result_bound else ")")
                     if d else f"evidence refused: {d.reason}")


# THE DECISION
# =================================================================================================

CONTINUE = "continue"
ACCEPTED = "accepted"
AWAITING_REVIEW = "awaiting-review"     # machine done; a human-only criterion is still open
PARK_UNDEFINED = "parked-undefined"     # a criterion exists but its target was never supplied
PARK_UNSUPPORTED = "parked-unsupported" # the checks passed but no evidence binds the artifact
PARK_STAGNANT = "parked-stagnant"
PARK_MAX_ROUNDS = "parked-max-rounds"

# Every decision the loop may stop on. CONTINUE is the only non-terminal action, so any
# new outcome belongs here unless it explicitly means "go round again".
TERMINAL = frozenset({ACCEPTED, AWAITING_REVIEW, PARK_UNDEFINED, PARK_UNSUPPORTED,
                      PARK_STAGNANT})


def decide(deltas, judge_accepts, oracle_ok, crit_status, results=None, k=3,
           investigation_budget=None):
    """continue / accept / park, and the human-readable basis for it. Returns (action, basis).

    Acceptance needs three independent things to agree: the frozen oracle passed, no mechanical
    criterion is unmet, and the judge ruled ACCEPT. The judge is one vote and the weakest one --
    it reads prose, the other two ran. `crit_status['unmet']` (rather than `mechanically_complete`)
    is deliberate: a card whose done_when is all prose has nothing mechanical to satisfy, and
    demanding mechanical completeness there would make such a card unacceptable forever.

    Parking is progress.stagnant() over the deltas: k motionless rounds, or an investigation budget
    spent without a single achievement. `waiting` short-circuits both -- an action still in flight
    is a slow round, not a stalled loop (OpenHands #5355 killed agents waiting on long builds)."""
    unmet = list(crit_status.get("unmet") or [])
    met = sorted(c for c, v in (results or {}).items() if v)

    # DEFECT 2026-09-19: grounding.status() gained `blocked_by_inert` -- a criterion that is
    # registered but cannot fire because its parameter was never supplied -- and this function
    # never read it. `unmet` covers only EVALUABLE criteria, so a frozen one is absent from it,
    # and acceptance sailed through while grounding was simultaneously reporting complete=False.
    # Two components disagreeing about the same goal is worse than either being wrong alone.
    # An undefined target is not a satisfied one: refuse and name the missing parameter.
    inert = list(crit_status.get("inert") or [])
    if crit_status.get("blocked_by_inert") or inert:
        return PARK_UNDEFINED, (
            "cannot accept: criteria {0} are registered but have no target -- a parameter was "
            "never supplied, so nothing can be measured against them. Ground the parameter or "
            "accept the proxy, then re-run.".format(", ".join(inert)))

    if judge_accepts and oracle_ok and not unmet:
        basis = ("oracle PASS" if oracle_ok else "no oracle") + \
                ("; criteria met: " + ", ".join(met) if met else "; no mechanical criterion on this card") + \
                "; judge ruled ACCEPT"
        if crit_status.get("awaiting_human"):
            # The machine has done everything it can verify, which is NOT the same as the goal
            # being met. Reporting that as "accepted" is how a loop claims a win on a criterion no
            # machine ever checked. It gets its own terminal outcome so a human sees the queue.
            return AWAITING_REVIEW, (
                basis + "; MACHINE-COMPLETE ONLY -- still needs a person to judge " +
                ", ".join(crit_status["awaiting_human"]))
        return ACCEPTED, basis
    # Ground truth over narrative: the judge liked the prose, the checks disagree. Note it, then
    # fall through -- an overruled ACCEPT must still be subject to the stagnation break, or a judge
    # that accepts every round would hold a stuck loop open to its full round budget.
    overruled = (f"judge ruled ACCEPT but the environment disagrees -- unmet criteria: "
                 f"{', '.join(unmet)}. ") if (judge_accepts and oracle_ok and unmet) else ""
    last = deltas[-1] if deltas else None
    if last is not None and last.waiting:
        return CONTINUE, overruled + "an action is still in flight; a slow round is not a stalled loop"
    if progress.stagnant(deltas, k=k, investigation_budget=investigation_budget):
        if all(not d.progressed for d in deltas[-k:]):
            return PARK_STAGNANT, overruled + (f"{k} consecutive rounds with no achievement and no "
                                               f"new evidence: " + last.summary())
        return PARK_STAGNANT, overruled + (f"investigation budget of {investigation_budget} rounds "
                                           f"spent with new evidence but no achievement")
    return CONTINUE, overruled + (last.summary() if last is not None else "first round")


# =================================================================================================
# THE RECEIPT
# A run nobody watched has to be auditable afterwards, and "the model said it was done" is not an
# audit trail. Each row records what MOVED (from the delta), what the oracle and the criteria said,
# what the judge said, and which of those the decision actually rested on.
# =================================================================================================

class Receipt:
    def __init__(self, name, worker, card_path=""):
        self.name, self.worker, self.card_path = name, worker, str(card_path)
        self.started = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.rows = []
        self.outcome = "incomplete"
        self.final = {}

    def record(self, rnd, d, snap, action, basis, oracle_ok, oracle_msg, ruling, note,
               review_root, results=None):
        self.rows.append({
            "round": rnd,
            "movement": movement_label(d),
            "moved": d.summary(),
            "files_changed": sorted(d.files_changed),
            "oracle": "PASS" if oracle_ok else f"FAIL ({oracle_reason(oracle_msg)})",
            "oracle_flips": {k: list(v) for k, v in d.oracle_flips.items()},
            "regressions": sorted(d.regressions),
            "new_evidence": sorted(d.new_evidence),
            "goal_gained": sorted(d.goal_gained),
            "goal_lost": sorted(d.goal_lost),
            "criteria": dict(results or {}),
            "waiting_on": sorted(snap.in_flight),
            "repeated_outcome": d.repeated_outcome,
            "judge": (ruling or "").strip().splitlines()[0][:200] if ruling else "",
            "skeptic": (note or "")[:200],
            "artifact_hashes": dict(snap.artifacts),
            "decision": action,
            "basis": basis,
            "workspace": str(review_root),
        })

    def finish(self, outcome, rounds, review_root, criteria=None, results=None, basis=""):
        st = grounding.status(criteria or [], results or {})
        self.outcome = outcome
        self.final = {
            "outcome": outcome, "rounds": rounds, "best_artifact": str(review_root),
            "basis": basis,
            "mechanically_complete": st["mechanically_complete"],
            "awaiting_human": st["awaiting_human"],
            "inert": st["inert"],
            "unmet": st["unmet"],
            "criteria": {c.id: {"state": c.state, "quote": c.quote[:120],
                                "result": (results or {}).get(c.id)} for c in (criteria or [])},
        }
        return self

    def render(self):
        """Plain text for someone who was not here. No jargon that is not defined on the page."""
        L = [f"RECEIPT  job={self.name}  worker={self.worker}  outcome={self.outcome}  "
             f"rounds={len(self.rows)}  started={self.started}",
             f"card: {self.card_path}" if self.card_path else "",
             "",
             "How this run was judged: the continue/park/accept decision below was computed from the",
             "ENVIRONMENT -- the frozen oracle's exit code, files on disk and their hashes, tool",
             "evidence, and the done_when criteria that could be checked mechanically. The worker's",
             "and the judge's words are printed for context and were never scored. Terms:",
             "  ACHIEVEMENT   a check flipped FAIL->PASS or a criterion was met, nothing regressed",
             "  INVESTIGATION new tool evidence entered the run: buys one more attempt, not unlimited",
             "  CHURN         files were rewritten and nothing else moved (the reporter's fingerprint)",
             "  REGRESSION    something that was passing stopped passing",
             "  WAITING       nothing moved but an action was still in flight: not a stalled loop",
             ""]
        for r in self.rows:
            L.append(f"round {r['round']}  {r['movement']}")
            L.append(f"    moved:    {r['moved']}")
            L.append(f"    oracle:   {r['oracle']}")
            if r["oracle_flips"]:
                L.append("    flips:    " + ", ".join(f"{k}: {o} -> {n}"
                                                       for k, (o, n) in r["oracle_flips"].items()))
            if r["criteria"]:
                L.append("    criteria: " + ", ".join(f"{k}={'met' if v else 'unmet'}"
                                                      for k, v in sorted(r["criteria"].items())))
            if r["files_changed"]:
                # basenames here, full paths in receipt.json: the reader wants to know WHICH
                # artifact moved, not to re-read the workspace path on every line.
                L.append(f"    rewrote:  {', '.join(Path(p).name for p in r['files_changed'])}")
            if r["new_evidence"]:
                L.append(f"    evidence: {len(r['new_evidence'])} new "
                         f"({', '.join(r['new_evidence'][:3])}{' ...' if len(r['new_evidence']) > 3 else ''})")
            if r["regressions"]:
                L.append(f"    REGRESSED: {', '.join(r['regressions'])}")
            if r["waiting_on"]:
                L.append(f"    in flight: {', '.join(r['waiting_on'])}")
            if r["repeated_outcome"]:
                L.append("    note:     the worker returned the identical text as last round")
            if r["judge"]:
                L.append(f"    judge:    {r['judge']}")
            L.append(f"    DECISION: {r['decision']} -- {r['basis']}")
            L.append("")
        f = self.final
        L.append(f"FINAL: {f.get('outcome', self.outcome)} after {f.get('rounds', len(self.rows))} round(s)")
        if f.get("basis"):
            L.append(f"  basis: {f['basis']}")
        L.append(f"  artifact: {f.get('best_artifact', '')}")
        if f.get("unmet"):
            L.append(f"  mechanical criteria still unmet: {', '.join(f['unmet'])}")
        if f.get("awaiting_human"):
            L.append("  NOT machine-checkable, needs a person to close: "
                     + ", ".join(f["awaiting_human"]))
        if f.get("inert"):
            L.append("  registered but could not fire (unset parameter or unaccepted proxy): "
                     + ", ".join(f["inert"]))
        return "\n".join(x for x in L if x is not None)

    def to_json(self):
        return {"job": self.name, "worker": self.worker, "card": self.card_path,
                "started": self.started, "rounds": self.rows, "final": self.final}

    def save(self, ws):
        ws = Path(ws); ws.mkdir(parents=True, exist_ok=True)
        (ws / "receipt.md").write_text(self.render(), encoding="utf-8")
        (ws / "receipt.json").write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")
        return ws / "receipt.md"


# =================================================================================================
# THE LOOP
# =================================================================================================

def _noop_emit(event, **fields):
    return {"event": event, **fields}


def _has_capture_hook(hooks):
    """Does this hook set have a call layer behind it?

    getattr, not attribute access: several callers -- the acceptance tests here, and Track C's
    simulated execution -- pass a duck-typed object rather than the Hooks dataclass. A run with
    NO call layer is a legitimate, testable configuration whose records are honestly stamped
    ASSERTED; it must not become an AttributeError crash the moment provenance was added."""
    return getattr(hooks, "capture", None) is not None


def _capture_hook(hooks, rnd):
    fn = getattr(hooks, "capture", None)
    return fn(rnd) if fn else None


@dataclass
class Hooks:
    """Every boundary the loop crosses to something outside itself, in one place, so a test can
    run the real decision logic against a stubbed worker instead of a 27B model.

    worker(rnd, guidance)      -> (output_text, review_root, in_flight_ids)
    oracle(review_root)        -> (ok, message)
    skeptic(rnd, out, root)    -> note text
    judge(rnd, out, note)      -> ruling text, expected to start ACCEPT or REDO
    evidence(review_root)      -> frozenset of tool-evidence ids
    criteria(review_root)      -> {criterion_id: bool}
    capture(rnd)               -> call.Capture for the response THIS round produced, or None

    `in_flight_ids` is how a worker reports an action it started but has not finished. The live
    worker is synchronous, so it always reports none; the seam exists because an asynchronous one
    must be able to say "still running" and NOT be parked for it.
    """
    worker: object
    oracle: object = None
    skeptic: object = None
    diagnose: object = None
    judge: object = None
    evidence: object = None
    criteria: object = None
    capture: object = None
    commentary: object = None

    def __post_init__(self):
        self.oracle = self.oracle or (lambda root: (True, ""))
        self.skeptic = self.skeptic or (lambda rnd, out, root: "SKEPTIC: (no note)")
        self.judge = self.judge or (lambda rnd, out, note: "REDO -- no judge configured")
        self.evidence = self.evidence or (lambda root: frozenset())
        self.criteria = self.criteria or (lambda root: {})
        # No default capture hook on purpose. A default returning None would be indistinguishable
        # from a live worker whose response was never captured, and the difference between "there
        # is no call layer here" and "the call layer captured nothing" is the whole point of the
        # provenance field. `capture is None` means the former and is recorded as ASSERTED.


def _prior_artifact(ws):
    try:
        return (ws / "output.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# Any language tag (python, json, md, toml, ...) or none -- the deliverable is the file the card
# NAMES, whatever its type. The `proposal` fence is explicitly excluded so a worker-proposal block
# is never mistaken for the deliverable (extract_proposals owns it).
_FENCE = re.compile(r"(?P<f>`{3,}|~{3,})[ \t]*(?!proposal\b)(?:[a-zA-Z0-9_+.\-]+[ \t]*)?\r?\n(?P<body>.*?)\r?\n[ \t]*(?P=f)", re.S | re.I)
# Fallback for a common local-model slip: a single opening fence the model never closed. Only
# applied when there is exactly one lone fence marker (no properly-closed fence anywhere), so a
# reply that is plainly "the file in a fence, minus the closer" is still recovered. Junk with no
# fence still yields "" -- the refusal the evidence sensitivity probe relies on -- and the same
# transform runs at write time and seal time, so byte-exact lineage is preserved.
_FENCE_OPEN = re.compile(r"(?:`{3,}|~{3,})[ \t]*(?!proposal\b)(?:[a-zA-Z0-9_+.\-]+[ \t]*)?\r?\n(?P<body>.*)$", re.S | re.I)


def oracle_coverage(card):
    """What a PASSING oracle actually establishes, stated for the judge (O5). A pass proves only
    the properties the oracle asserts -- not general correctness -- so the judge is given the
    oracle's own assertions and the done_when clauses, and told the pass is bounded to them. This
    is the fix for a shape/type oracle being treated as universal proof and used to dismiss a
    concrete numeric concern it never checked."""
    oracle = str(card.get("oracle") or "").strip()
    if not oracle:
        return ("(no oracle on this card -- nothing was mechanically checked; rely on the skeptic "
                "and the brief for correctness)")
    dw = card.get("done_when") or []
    body = oracle if len(oracle) <= 600 else oracle[:600] + " ...[truncated]"
    return ("PASSED -- the frozen oracle ran and its assertions held. It checks ONLY the following, "
            "and a pass establishes THESE PROPERTIES AND NOTHING BEYOND THEM:\n"
            "  assertions: {0}\n  done_when: {1}\n"
            "A concrete concern about a property NOT in this list is OUTSIDE the oracle's coverage; "
            "do not dismiss it by pointing at the pass.".format(body, "; ".join(dw) or "(none stated)"))


def has_deliverable(root, artifact, output):
    """A blank reply or a named artifact that was never written is a delivery failure.

    That is not an oracle failure. A check that reads a missing file and raises
    ModuleNotFoundError is the symptom; the cause is that nothing was delivered."""
    if not str(output or "").strip():
        return False
    art = artifact or "output.md"
    if art == "output.md":
        return True
    path = Path(root) / art
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def apply_skeptic_bounce(draft, *, has_file, ask, revise, write, check):
    """Question a real draft before the check, and keep whichever candidate passes.

    The skeptic runs only when a file was delivered. Both the original draft and the
    revision are checked. A revision that fails does not erase a draft that passes.
    When neither passes, the revision stays on disk so the next round sees that
    check's message. A missing file never calls `ask`.
    """
    if not has_file:
        return {"kept": "draft", "text": draft, "delivery": True, "asked": False,
                "oracle_ok": False, "oracle_msg": "", "reverted": False, "questions": ""}
    questions = ask(draft) or ""
    if "?" not in questions:
        write(draft)
        ok, msg = check()
        return {"kept": "draft", "text": draft, "delivery": False, "asked": False,
                "oracle_ok": ok, "oracle_msg": msg, "reverted": False, "questions": questions}
    revised = revise(questions)
    write(revised)
    rev_ok, rev_msg = check()
    if rev_ok:
        return {"kept": "revision", "text": revised, "delivery": False, "asked": True,
                "oracle_ok": True, "oracle_msg": rev_msg, "reverted": False, "questions": questions}
    write(draft)
    draft_ok, draft_msg = check()
    if draft_ok:
        return {"kept": "draft", "text": draft, "delivery": False, "asked": True,
                "oracle_ok": True, "oracle_msg": draft_msg, "reverted": True, "questions": questions}
    write(revised)
    return {"kept": "revision", "text": revised, "delivery": False, "asked": True,
            "oracle_ok": False, "oracle_msg": rev_msg, "reverted": False, "questions": questions}


def first_code_block(output):
    """The declared transform from a worker's reply to a NAMED code artifact.

    A card may name `artifact: totals.py` and freeze an oracle that imports `totals`. For a
    tools-disabled worker the reply is markdown, and until 2026-09-20 only that markdown was ever
    written (as output.md), so the named artifact never existed and such an oracle failed with
    ModuleNotFoundError on every round -- both competing approaches in the first LF-04 run died
    identically of it, and the architect then "repaired" the arithmetic. This transform is what
    makes the named artifact real AND bindable: evidence.seal re-runs it on the reply and compares
    byte for byte, and probes it with junk to confirm it is sensitive to its input. Bytes with no
    fence produce nothing, which is the refusal the sensitivity probe expects."""
    text = output.decode("utf-8", "replace") if isinstance(output, (bytes, bytearray)) else (output or "")
    m = _FENCE.search(text)
    if m:
        return m.group("body") + "\n"
    # No properly-closed fence. Recover a single unterminated fence (opening ``` / ~~~ that the
    # model never closed) ONLY when there is exactly one lone fence marker, so junk with no fence
    # still yields "" and a reply with several fences is not mis-sliced.
    markers = len(re.findall(r"`{3,}|~{3,}", text))
    if markers == 1:
        mo = _FENCE_OPEN.search(text)
        if mo:
            body = mo.group("body").rstrip()
            return (body + "\n") if body else ""
    return ""


def _snapshot_attempt(ws, rnd, artifact):
    """Keep this round's artifact bytes under attempts/r<n>/ so a later reader can see WHAT
    CHANGED between a failing and a passing attempt. Without this the memory store could only
    say that a check failed and later passed (LF-01 audit); with it, a correction can carry the
    actual diff. Cheap, and never touches the live artifact."""
    if rnd is None:
        return
    try:
        d = ws / "attempts" / "r{0}".format(rnd)
        d.mkdir(parents=True, exist_ok=True)
        for name in {"output.md", artifact or "output.md"}:
            src = ws / name
            if src.is_file():
                (d / name).write_bytes(src.read_bytes())
    except OSError:
        pass


_PROPOSAL_FENCE = re.compile(r"```[ \t]*proposal[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S | re.I)
PROPOSAL_KINDS = ("next_work", "prerequisite", "alternative", "question")


def extract_proposals(output):
    """Worker-originated proposals from a reply (LF-06): every fenced ```proposal block holding a
    JSON object or list of objects {kind, text, evidence?}. Malformed blocks are kept as a
    `malformed` note, never silently dropped and never executed. Proposals are DATA the
    architect decides on; they grant nothing."""
    out = []
    for m in _PROPOSAL_FENCE.finditer(output or ""):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except ValueError as e:
            out.append({"kind": "malformed", "text": raw[:400], "evidence": "",
                        "note": "not JSON: {0}".format(e)})
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if not isinstance(it, dict):
                out.append({"kind": "malformed", "text": str(it)[:400], "evidence": "",
                            "note": "not an object"})
                continue
            kind = str(it.get("kind") or "").strip().lower()
            if kind not in PROPOSAL_KINDS:
                out.append({"kind": "malformed", "text": str(it.get("text") or "")[:400],
                            "evidence": str(it.get("evidence") or "")[:400],
                            "note": "kind {0!r} not one of {1}".format(kind, list(PROPOSAL_KINDS))})
                continue
            if not str(it.get("text") or "").strip():
                continue
            out.append({"kind": kind, "text": str(it["text"]).strip()[:600],
                        "evidence": str(it.get("evidence") or "").strip()[:600]})
    return out[:6]


def _keep_artifact(ws, output, previous, artifact="output.md", rnd=None):
    """Write the round's output, but NEVER let an empty response destroy a good artifact.

    `artifact` is the card's named artifact. output.md always receives the raw reply (it is the
    provenance); when the card names something else, that file receives `first_code_block(reply)`,
    the same transform evidence.seal is told about, so what the oracle imports is what the record
    binds. A reply with no code block leaves an earlier good artifact alone for the same reason an
    empty reply does.

    Observed 2026-09-19: rounds 1 and 2 produced ~2580 characters each; round 3 returned an empty
    completion and that empty string overwrote output.md. The run then failed with "no python code
    block -- a description is not an implementation", which is a true statement about an artifact
    the worker had already written correctly twice. A null response is a failed attempt, not a
    deletion instruction.

    Returns the text that is actually on disk, so the caller keeps reasoning about the real
    artifact rather than the empty string it just received."""
    path = ws / "output.md"
    if (output or "").strip():
        path.write_text(output, encoding="utf-8", newline="")
        if artifact and artifact != "output.md":
            code = first_code_block(output)
            if code.strip():
                target = ws / artifact
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(code, encoding="utf-8", newline="")
            # no fence: the named artifact from the previous round, if any, stays as it was
        _snapshot_attempt(ws, rnd, artifact)
        return output
    if previous and previous.strip():
        return previous              # leave the earlier, better artifact exactly where it is
    path.write_text(output or "", encoding="utf-8", newline="")
    return output or ""


def run_loop(card, ws, hooks, emit=_noop_emit, max_rounds=5, criteria=None, guidance="",
             k=3, investigation_budget=None, receipt=None, require_evidence=None, job_id=None):
    """Run the rounds and return the run record. The caller owns process exit codes and printing.

    Returns {outcome, rounds, best_artifact, history, receipt, deltas, snapshots, basis}."""
    ws = Path(ws)
    # Default from loop_config so the policy lives in ONE place the operator can see, rather than
    # being a literal buried in a function signature.
    require_evidence = _LOOP.require_evidence if require_evidence is None else require_evidence
    job_id = job_id or card.get("name") or "job"
    record = None
    criteria = criteria if criteria is not None else criteria_from_card(card)
    artifacts = [card.get("artifact") or "output.md"]
    receipt = receipt or Receipt(card.get("name", "job"), card.get("worker", "?"))
    history, deltas, snaps = [], [], []
    prev = None
    review_root = str(ws)
    outcome, basis, rnd = PARK_MAX_ROUNDS, "round budget exhausted", 0
    results = {}
    last_fail_reason = None          # O4: the previous round's mechanical failure reason
    assisted_reasons = set()         # O4: reasons a diagnostic review has already been spent on
    seen_fail = set()                # O4: failure reasons seen in a PRIOR round (repetition, incl. thrashing)
    recovery = []                    # O4: recovery-transition records, for attribution

    for rnd in range(1, max_rounds + 1):
        round_recovery = None
        recovery_extra = ""
        output, review_root, in_flight = hooks.worker(rnd, guidance)
        review_root = review_root or str(ws)
        # Verify and seal in one attempt-bound step. The verdict now carries the artifact hash it
        # was computed over and the attempt it belongs to, so it cannot be offered for another --
        # and if the artifact moved while the verifier ran, that is a FAIL with a reason rather
        # than a PASS about bytes that no longer exist.
        oracle_ok, oracle_msg, vresult = verify_round(job_id, rnd, review_root, artifacts,
                                                      hooks.oracle)
        # Seal NOW, while the worker's output and the artifact on disk are both in hand. Sealing
        # later would mean binding whatever happens to be on disk at acceptance time, which is the
        # gap the whole mechanism exists to close.
        # A tools-enabled worker writes its named artifact itself and its output IS the file
        # (IDENTITY). Only the chat path needs the fence transform.
        named = artifacts[0] != "output.md" and not card.get("tools")
        record = seal_round(job_id, rnd, card.get("worker"), output, review_root, artifacts,
                            oracle_ok, result=vresult, capture=_capture_hook(hooks, rnd),
                            transform=first_code_block if named else None,
                            transform_name="first_code_block" if named else None)
        delivery = getattr(hooks.worker, "delivery_failure", "") or ""
        # `reason` feeds the recurrence tracker below (`if not oracle_ok`). It was assigned only in
        # the oracle-failed branch, so a DELIVERY failure (no usable file) with a failing oracle --
        # the single most common local-model outcome -- reached the tracker with `reason` unbound and
        # crashed the whole run with UnboundLocalError. Give every not-oracle_ok path a reason.
        reason = ""
        if delivery:
            # A blank file, a missing fence, or a tool-limit is not a wrong answer. Do not ask the
            # worker to satisfy an assertion about content that was never written.
            note = "(delivery failure; skeptic not asked)"
            ruling = ("REDO -- no deliverable was written ({0}). Return the complete file in one "
                      "fenced block. Narration is not the file.").format(delivery)
            reason = "delivery failure: " + str(delivery)
        elif oracle_ok:
            note = hooks.skeptic(rnd, output, review_root)
            ruling = hooks.judge(rnd, output, note)
        else:
            # Explicit-kickback contract: tell the worker WHY in plain terms, not a raw traceback.
            # The oracle's own assert message is the human reason ("no bracketed citation"), so the
            # worker fixes exactly that next round instead of guessing. The RECOVERY transition is
            # deferred to AFTER the progress delta and the decision (below), so recurrence is only a
            # signal and this round's real movement is what gates it.
            reason = oracle_reason(oracle_msg)
            note = "(oracle failed; mechanical feedback only)"
            ruling = (f"REDO -- the check failed: {reason}. "
                      f"Change the file so this check passes. Do not return the same file again.")

        # --- environment state, built from disk. Nothing below this line reads `ruling`. --------
        # Pass the oracle verdict to criteria evaluation when the hook accepts it. Older hooks
        # (and every existing test) take only the root, so fall back rather than forcing every
        # caller to change for a feature most cards do not use.
        try:
            results = hooks.criteria(review_root, oracle_ok)
        except TypeError:
            results = hooks.criteria(review_root)
        snap = snapshot(review_root, artifacts, oracle_ok, results, hooks.evidence(review_root),
                        in_flight=in_flight or (), outcome=output)
        d = progress.delta(prev, snap)
        prev = snap
        snaps.append(snap); deltas.append(d)
        st = grounding.status(criteria, results)
        judge_accepts = bool(ruling) and ruling.strip().upper().startswith("ACCEPT")
        action, basis = decide(deltas, judge_accepts, oracle_ok, st, results=results, k=k,
                               investigation_budget=investigation_budget)
        if action == ACCEPTED:
            import projectpkg
            blob = output or ""
            rootp = Path(review_root) if review_root else None
            if rootp:
                for name in artifacts or []:
                    fp = rootp / str(name)
                    if fp.is_file():
                        blob += "\n" + fp.read_text(encoding="utf-8", errors="replace")
            hit = projectpkg.scan_emitted(blob, card.get("limits") or {})
            if hit:
                action = CONTINUE
                basis = "never-rule on emitted code: " + hit
                ruling = "REDO -- the deliverable violates a confirmed never-rule: " + hit + ". Remove it."

        # RECOVERY TRANSITION (O4), placed here on purpose: `d` (this round's progress) and `action`
        # already exist, so recurrence is a SIGNAL and no-movement is CHECKED, not assumed. When a
        # failure reason recurs with no ACHIEVEMENT this round (churn/new-evidence is not
        # achievement), obtain a FRESH diagnostic (its own
        # hook: a real review of this workspace, never cached post-success text). If a further
        # attempt is authorized, deliver the finding to it -- overriding a stagnation park into ONE
        # more attempt so the guidance is actually used. If none is authorized, do NOT claim it was
        # consumed: record it, preserve it, and let the real stop stand. Once per reason.
        if not oracle_ok:
            recurred = reason in seen_fail
            seen_fail.add(reason)
            if hooks.diagnose and recurred and (not d.achievement) and reason not in assisted_reasons:
                assisted_reasons.add(reason)
                diag_text, diag_ran = _run_diagnose(hooks, rnd, reason, output, review_root)
                found = bool(diag_ran and not _diag_empty(diag_text))
                will_retry = rnd < max_rounds
                rec = {"round": rnd, "observed": reason[:200], "kind": _failure_kind(reason),
                       "action": "diagnostic_review", "diagnostic_ran": diag_ran, "found": found,
                       "delivered_to_next_attempt": bool(found and will_retry),
                       "why": "the failure reason recurred and this round produced no achievement (an oracle flip or a met criterion)",
                       "falsifier": "if the next attempt fails with the same reason, the diagnosis was wrong"}
                if found and will_retry:
                    recovery_extra = ("\n\nA FRESH review of your workspace and the failing check "
                                      "found NEW information:\n" + diag_text[:900] + "\nChange your "
                                      "approach to address exactly this; resubmitting the previous "
                                      "output will fail the same way.")
                    note = "FRESH DIAGNOSTIC (delivered to the next attempt): " + diag_text[:300]
                    if action == PARK_STAGNANT:
                        action = CONTINUE
                        basis = ("recovery: a fresh diagnostic was obtained on a repeated failure with "
                                 "budget remaining; granting one more attempt to apply it -- was: " + basis)
                    rec["changed"] = "the diagnostic finding is delivered to the next worker attempt"
                elif found and not will_retry:
                    rec["changed"] = ("diagnostic obtained but NOT delivered: no further attempt is "
                                      "authorized (round budget reached); preserved for a continuation")
                elif diag_ran:
                    rec["changed"] = "diagnostic ran but established no usable finding; guidance unchanged"
                else:
                    rec["changed"] = "diagnostic could not run; guidance unchanged"
                recovery.append(rec)
                round_recovery = rec
                emit("recovery", **{k: str(x)[:200] for k, x in rec.items()})

        emit("delta", round=rnd, movement=movement_label(d), moved=d.summary()[:160])
        emit("decision", round=rnd, action=action, basis=basis[:160])
        receipt.record(rnd, d, snap, action, basis, oracle_ok, oracle_msg, ruling, note,
                       review_root, results=results)
        _cm = hooks.commentary(rnd) if getattr(hooks, "commentary", None) else {}
        _reply = (_cm.get("reply") if isinstance(_cm, dict) else "") or output
        _fk = (_cm.get("failure_kind") if isinstance(_cm, dict) else "") or ""
        history.append({"round": rnd, "artifact": review_root, "output_chars": len(output or ""),
                        "proposals": extract_proposals(_reply),       # from the REPLY, not the artifact bytes
                        "commentary": (_reply or "")[:2000],          # the worker's final commentary, preserved
                        "failure_kind": _fk,                          # tool-limit etc., for correct attribution
                        "unaccepted_edits": (_cm.get("unaccepted_edits") if isinstance(_cm, dict) else None) or [],
                        "oracle_ok": oracle_ok, "skeptic": (note or "")[:300],
                        "ruling": (ruling or "")[:500], "movement": movement_label(d),
                        "moved": d.summary(), "decision": action, "basis": basis,
                        "recovery": round_recovery})
        # TERMINAL outcomes. Listing them explicitly rather than special-casing ACCEPTED: adding
        # AWAITING_REVIEW and PARK_UNDEFINED without touching this branch made the loop spin to
        # its round limit on a goal it had already decided about, and report parked-max-rounds --
        # a decision silently downgraded into a budget failure. A new terminal decision that the
        # loop does not know how to end on is worse than no new decision at all.
        # Acceptance must PRESENT evidence. The judge, the oracle and the criteria all speak to
        # whether the artifact is good; none of them speaks to whether it is the worker's. An
        # ACCEPT that cannot show a sealed record is downgraded, never granted.
        if action == ACCEPTED and require_evidence:
            # Demand CAPTURED provenance exactly when this run has a call layer to capture from.
            # Demanding it unconditionally would break a simulated run that has no worker at all;
            # not demanding it when a capture hook exists would make the hook decorative.
            supported, why = evidence_supports(record, job_id, rnd, review_root, artifacts,
                                               require_captured=_has_capture_hook(hooks))
            emit("evidence", round=rnd, supported=supported, detail=why[:160])
            if not supported:
                action = PARK_UNSUPPORTED
                basis = ("accept was refused: " + why + ". The checks passed, but nothing binds "
                         "the accepted bytes to the work that was done.")
            else:
                basis += "; " + why

        if action in TERMINAL:
            outcome = action
            break
        outcome = PARK_MAX_ROUNDS
        basis = f"round budget of {max_rounds} exhausted; last state: {d.summary()}"
        guidance = ((ruling or "").strip() + recovery_extra
                    + ("\n\nSkeptic also noted: " + note if note and oracle_ok else ""))

    receipt.finish(outcome, rnd, review_root, criteria=criteria, results=results, basis=basis)
    return {"outcome": outcome, "rounds": rnd, "best_artifact": review_root, "history": history,
            "receipt": receipt, "deltas": deltas, "snapshots": snaps, "basis": basis,
            "criteria": criteria, "results": results, "recovery": recovery}


# AWAITING_REVIEW and PARK_UNDEFINED must NOT map to 0. Exit 0 means accepted, and both of
# these mean the opposite: the machine finished but the goal is not established. They park
# (code 1/2 -> queue._classify -> parked/) so a person sees them instead of the run reading
# as a clean success.
EXIT_CODES = {ACCEPTED: 0, PARK_MAX_ROUNDS: 1, PARK_STAGNANT: 2, "worker-unavailable": 3,
              AWAITING_REVIEW: 1, PARK_UNDEFINED: 1, PARK_UNSUPPORTED: 1}


def _claim_still_held(card_path, controller, token):
    """Ask run/queue.py -- the module that issued the claim -- whether we still hold it.

    Loaded by path, and only when a token was actually passed, for two reasons. run/queue.py
    shadows the stdlib `queue` module, so a plain import here would resolve to whichever came
    first on sys.path; and the ownership rule must exist in ONE place. A local re-implementation
    of "compare owner and token" is four lines that will drift from the four lines that matter."""
    import importlib.util
    if "fleet_queue" in sys.modules:
        q = sys.modules["fleet_queue"]
    else:
        spec = importlib.util.spec_from_file_location("fleet_queue", HERE / "queue.py")
        q = importlib.util.module_from_spec(spec)
        sys.modules["fleet_queue"] = q
        spec.loader.exec_module(q)
    return q.holds_claim(card_path, controller, token)


def _verification_candidate(stage, tool_ws, artifact):
    """The tree the oracle is allowed to see: authorized staged inputs, plus the declared
    deliverable taken from the worker workspace. Undeclared files the worker created are not
    copied. This is not a copy of the worker workspace and is not itself the integration
    candidate; integration remains the gate that promotes."""
    import tempfile
    dst = Path(tempfile.mkdtemp(prefix="fleet-candidate-"))
    art_rel = str(artifact or "").replace("\\", "/")
    src = Path(tool_ws)
    for e in stage or []:
        dest = str(e.get("dest") or "").replace("\\", "/")
        if not dest or dest == art_rel:
            continue
        raw = e["text"].encode("utf-8") if e.get("text") is not None else (e.get("data") or b"")
        p = dst / dest
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
    if art_rel:
        worker_art = src / art_rel
        if worker_art.is_file():
            target = dst / art_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(worker_art.read_bytes())
    return dst


def _unaccepted_edits(stage, tool_ws, art):
    """Doc 05 (verification<->integration consistency): a tools oracle runs in the worker's OWN
    workspace, where the worker may freely rewrite files it was GIVEN (staged dependencies). But
    integration promotes ONLY accepted deliverable members onto the baseline -- never those edits.
    So a deliverable that verified by leaning on an EDITED dependency would meet a DIFFERENT
    (baseline) dependency at integration: the environment that passes verification would not be the
    candidate that is integrated. Detect the divergence up front -- staged dependency dests whose
    current tool-ws bytes differ from the bytes that were staged -- so it is never imported silently.
    The deliverable itself is excluded (the worker authors it). This RECORDS the divergence; the
    authoritative consistency gate is the integration frozen check, which assembles baseline deps +
    accepted members only (established read-only for g-20260922-085803-a3d5: both the baseline and
    the worker-edited state.py/rules.py pass, so the oracle did not rely on the edits in that run --
    but nothing had flagged that the two environments differed)."""
    art_rel = str(art or "").replace("\\", "/")
    tw = Path(tool_ws)
    out = []
    for e in stage or []:
        dest = str(e.get("dest") or "").replace("\\", "/")
        if not dest or dest == art_rel:
            continue
        staged = (e["text"].encode("utf-8") if e.get("text") is not None
                  else (e.get("data") or b""))
        f = tw / dest
        try:
            if f.is_file() and f.read_bytes() != staged:
                out.append(dest)
        except OSError:
            continue
    return out


def _build_tool_manifest(card, art):
    """One validated staging manifest for the tool path. Each entry is {dest, text, data}: a .py
    dependency/skill is a text module at the workspace root; a declared input keeps its relative
    destination and is staged as TEXT when its bytes decode as utf-8, else as raw BYTES (so binary
    inputs are not corrupted by a lossy decode). The deliverable artifact itself is never staged."""
    manifest, seen = [], set()
    def _add(dest, path):
        dest = str(dest).replace("\\", "/")
        if not dest or dest == art or dest in seen:
            return
        raw = Path(path).read_bytes()
        try:
            manifest.append({"dest": dest, "text": raw.decode("utf-8"), "data": None})
        except UnicodeDecodeError:
            manifest.append({"dest": dest, "text": None, "data": raw})     # true binary -> bytes
        seen.add(dest)
    for _d in (card.get("deps") or []):
        if _d.get("location") and Path(_d["location"]).is_file():
            # Package-relative dest (game/rules.py) when card_for provided one; else a flat module.
            _add(_d.get("dest") or ((_d.get("module") or "") + ".py"), _d["location"])
    _dep_mods = {_d.get("module") for _d in (card.get("deps") or [])}
    for _sk in (card.get("skills") or []):
        if _sk.get("module") in _dep_mods:
            continue                       # delivered THROUGH the project dependency; no duplicate copy
        if _sk.get("location") and Path(_sk["location"]).is_file():
            _add((_sk.get("module") or "") + ".py", _sk["location"])
    for _inp in (card.get("inputs") or []):
        if _inp.get("location") and Path(_inp["location"]).is_file():
            _add(_inp.get("dest") or _inp.get("name"), _inp["location"])
    # Integration-repair candidate tree: the assembled project's files at their RELATIVE dests,
    # INCLUDING the artifact dest (the worker must see the current broken file to fix it).
    for _st in (card.get("integration_stage") or []):
        dest, loc = _st.get("dest"), _st.get("location")
        if not (dest and loc and Path(loc).is_file()):
            continue
        dest = str(dest).replace("\\", "/")
        if dest in seen:
            continue
        raw = Path(loc).read_bytes()
        try:
            manifest.append({"dest": dest, "text": raw.decode("utf-8"), "data": None})
        except UnicodeDecodeError:
            manifest.append({"dest": dest, "text": None, "data": raw})
        seen.add(dest)
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("card")
    ap.add_argument("--max-rounds", type=int, default=5)
    ap.add_argument("--resume", action="store_true",
                    help="continue a parked/halted job, carrying its last correction forward")
    ap.add_argument("--stagnation-k", type=int, default=3,
                    help="park after this many consecutive rounds with no achievement and no new "
                         "evidence (environment-measured, not the model's opinion)")
    ap.add_argument("--investigation-budget", type=int, default=4,
                    help="park after this many rounds if new evidence keeps arriving but nothing is "
                         "ever achieved; 0 disables the adaptive break. Keep it BELOW --max-rounds "
                         "or it can never fire and the round budget is the only brake")
    ap.add_argument("--claim-token", default=None,
                    help="the dispatching controller's claim token. When given, this process "
                         "re-reads the card from disk and refuses to execute unless the record "
                         "still carries this token -- so a job whose claim was reclaimed during "
                         "the process spawn is not executed by two processes at once.")
    ap.add_argument("--claim-controller", default=None,
                    help="the controller the claim belongs to; checked with --claim-token")
    ap.add_argument("--redo-mode", choices=("conversation", "rewrite"), default=None,
                    help="on a REDO: 'conversation' (default) = the worker KEEPS its full context across "
                         "rounds (its own drafts + every correction) and continues the thread -- the model "
                         "reasons better with the whole conversation than from a fresh one-shot; 'rewrite' = "
                         "a fresh unanchored swing each round, blind to its prior draft. CLI overrides the "
                         "card's redo_mode.")
    ap.add_argument("--skeptic-bounce", choices=("on", "off"), default=None,
                    help="on (default, conversation mode only): Spark asks the single sharpest skeptical "
                         "QUESTION and the worker answers it in-context BEFORE the frontier rules, so the "
                         "architect sees near-ready work (a question helps even when Spark is unreliable). "
                         "off: Spark instead verifies the output and passes a verdict note. CLI > card.")
    a = ap.parse_args()
    card_text = Path(a.card).read_text(encoding="utf-8")
    card = json.loads(card_text)
    # EXECUTOR-START VERIFICATION, before anything is written or any worker is called. The
    # dispatching controller checked it owned this job; a process spawn later that may no longer
    # be true, and the cost of being wrong is two processes running one job against one workspace.
    # Exit 3 / worker-unavailable is deliberate and stays inside the closed vocabulary (I3): a
    # lost claim is an INFRASTRUCTURE condition, not a defect in the job, so the job returns to
    # ready/ intact rather than being failed or accepted on somebody else's work.
    if a.claim_token and not _claim_still_held(a.card, a.claim_controller, a.claim_token):
        print(f"  claim lost before execution: {a.card} no longer carries "
              f"{a.claim_controller}'s token; another controller owns this job. Not executing.")
        print("FLEET_OUTCOME=worker-unavailable")
        sys.exit(EXIT_CODES["worker-unavailable"])
    name = card["name"]; worker = card["worker"]; brief = card["brief"]
    # precedence: explicit CLI flag > card field > default 'conversation' (keep the model's context
    # across turns; 'rewrite' is the opt-out when you want an unanchored fresh swing each round).
    redo_mode = a.redo_mode or card.get("redo_mode") or "conversation"
    _bounce = a.skeptic_bounce or card.get("skeptic_bounce") or "on"
    skeptic_bounce = (_bounce == "on")
    mode_text = (ROOT / card["mode"]).read_text(encoding="utf-8") if card.get("mode") else ""
    authority, authority_evidence = "", frozenset()
    if card.get("grounding"):
        units = retrieve_units(card["grounding"])
        authority = "\n\n".join(t for _, t in units)
        # Retrieval IS tool evidence: it entered the run once, before round 1, and it must not be
        # re-counted as a discovery on later rounds -- hence identities, not a length.
        authority_evidence = frozenset(f"retrieval:{i}" for i, _ in units)
        print(f"  retrieved authority: {len(authority)} chars from regdb/scout")
    emit = logger(f"verified-{name}")
    ws = ROOT / "runs" / f"verified-{name}"; ws.mkdir(parents=True, exist_ok=True)
    print(f"verified job '{name}' worker={worker}  ->  ws:{ws}  log:{emit.path}")
    staged = stage_skills(card, ws, emit)
    if staged:
        print("  staged: " + ", ".join(
            "{0} ({1})".format(x["module"], x.get("attribution") or "unavailable") for x in staged))
    brief = reconcile_brief(brief, staged)

    criteria = criteria_from_card(card)
    receipt = Receipt(name, worker, card_path=str(Path(a.card).resolve()))
    guidance = ""
    convo = []

    def preserve(outcome, code, rnd, review_root, history, basis=""):
        # Never lose the work on a park/halt: record where the best artifact is + the history,
        # and always leave the receipt behind -- a run nobody watched has to be auditable after.
        rec = {"name": name, "outcome": outcome, "rounds": rnd, "best_artifact": review_root,
               "basis": basis, "history": history,
               # worker-originated proposals, with the round that carried them (LF-06)
               "proposals": [dict(pp, round=h.get("round")) for h in history
                             for pp in (h.get("proposals") or [])]}
        (ws / "result.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
        rpath = receipt.save(ws)
        emit(outcome, round=rnd); emit.close()
        # Machine-readable outcome. The exit CODE cannot carry the distinction -- three
        # terminal outcomes share code 1 -- so a queue classifying on the code alone files
        # "a human must judge this" identically to "ran out of rounds".
        print(f"FLEET_OUTCOME={outcome}")
        print(f"\n  {outcome.upper()} at round {rnd}. work preserved: {review_root}"
              f"\n  record: {ws/'result.json'}\n  receipt: {rpath}")
        if code:
            sys.exit(code)

    # #5 durable resume: a parked/halted job continues with its last correction, not cold.
    if a.resume and (ws / "result.json").exists():
        prev_rec = json.loads((ws / "result.json").read_text(encoding="utf-8"))
        if prev_rec.get("outcome") != "accepted" and prev_rec.get("history"):
            guidance = prev_rec["history"][-1].get("ruling", "")
            print(f"  RESUME: prior outcome '{prev_rec['outcome']}' at round {prev_rec['rounds']}; "
                  "carrying the last correction forward.")

    # fail-fast: a wedged/down worker would hang every round on the request timeout. Gate once here.
    sys.path.insert(0, str(ROOT / "check"))
    import watchdog  # noqa
    ok, wstatus, wdetail = watchdog.usable(worker)
    if not ok:
        emit("worker-unavailable", worker=worker, status=wstatus, detail=wdetail[:150])
        print(f"  worker '{worker}' not usable: {wstatus} ({wdetail}); not dispatching.")
        receipt.finish("worker-unavailable", 0, str(ws), criteria=criteria, results={},
                       basis=f"{worker}: {wstatus} ({wdetail[:100]})")
        preserve("worker-unavailable", 3, 0, str(ws), [], f"{worker} {wstatus}")

    base = ((f"REPLY-FORMAT (follow exactly):\n{mode_text}\n\n" if mode_text else "")
            + (f"RETRIEVED AUTHORITY (use ONLY this; do not cite from memory):\n{authority}\n\n" if authority else "")
            + brief)

    at = {"round": 0}      # the round the live hooks are currently serving, for honest log lines
    # The call layer's capture of the response THIS round's artifact was written from. Cleared at
    # the top of every round so a round whose worker call failed cannot present the previous
    # round's capture -- which would be exactly the stale-provenance defect, one level up.
    captured = {"round": 0, "capture": None}

    def _take(msg, telemetry, rnd):
        """Record the call layer's capture and return the response text. Every model call in the
        live worker goes through here, so the capture in hand always belongs to the LAST response
        written to the artifact -- including the skeptic-bounce revision, which replaces it."""
        captured["round"] = rnd
        captured["capture"] = (telemetry or {}).get("capture")
        return (msg.get("content") or "").strip()

    def live_oracle(review_root):
        # The loop calls this again after the worker hook returns. That second run is the
        # authoritative one: it reads the artifact state the loop is actually about to judge,
        # after any skeptic bounce or revert. The extra subprocess is cheap next to a model call.
        if card.get("oracle"):
            edits = captured.get("unaccepted_edits") or []
            root = review_root
            prefix = ""
            if captured.get("stage") is not None:
                # Authorized inputs plus the declared deliverable. An undeclared helper left in
                # the worker workspace is not part of this tree.
                root = str(_verification_candidate(captured["stage"], review_root,
                                                   card.get("artifact") or "output.md"))
                prefix = ("oracle ran on authorized staged inputs plus the deliverable, not the "
                          "full worker workspace. ")
                if edits:
                    prefix += ("unaccepted dependency edits will not be promoted: {0}. "
                               ).format(", ".join(edits))
            ok, msg = run_oracle(card["oracle"], root)
            msg = prefix + msg
            # 600, not 150: the assertion text is the LAST line of a traceback, and 150 chars
            # kept only the frames. The architect's failure_text and lesson extraction both read
            # this row, and a lesson about a caret line teaches nothing.
            emit("oracle", round=at["round"], ok=ok, msg=msg[:600])
            return ok, msg
        return True, ""

    def live_worker(rnd, guidance):
        """Produce the artifact. Returns (output, review_root, in_flight). The skeptic-question
        bounce lives here rather than in the loop because it is worker-side: Spark asks, the worker
        answers IN ITS OWN THREAD, and the result is only kept if the oracle still passes."""
        at["round"] = rnd
        live_worker.delivery_failure = ""
        _reply, _fk = "", ""      # worker commentary (chat reply) + failure-kind channel (seams 2,3,5)
        reject_tail = f"\n\nYour previous attempt was REJECTED. Fix exactly this:\n{guidance}" if guidance else ""
        _prev_edits = captured.get("unaccepted_edits") or []
        if _prev_edits:
            reject_tail += (
                "\n\nThe previous attempt modified files integration will NOT keep: {0}. "
                "Those files are restored to what you were given before the oracle is graded. "
                "Put the required behavior in the deliverable, not in those dependencies."
            ).format(", ".join(_prev_edits))
        if card.get("tools"):
            # tool jobs keep context WITHIN a job (the agent's own tool-round loop) and inherit round 1's
            # files on disk via a stable workspace, so the redo_mode conversation-carry is moot here.
            import tooljob
            _stage, _art = None, (card.get("artifact") or "output.md")
            # A declared source that card_for could not resolve is recorded as missing. Refuse the
            # attempt HERE, before dispatch, rather than running the worker against a brief that
            # promises material the workspace will not hold (S-B1 provisioning).
            _missing = card.get("missing_sources") or []
            if _missing:
                refs = ", ".join(m.get("ref", "?") for m in _missing)
                emit("required_material_missing", round=rnd, refs=refs)
                r = {"content": "required project material could not be resolved: " + refs,
                     "workspace": str(ws), "artifact_content": None}
            else:
                # Stage the deliverable's project material into the tool workspace so a tools-enabled
                # worker can inspect/import it AND the frozen oracle (which runs there) can too. The
                # deliverable artifact itself is never staged -- the worker writes that.
                _stage = _build_tool_manifest(card, _art)
                try:
                    r = tooljob.run_tooljob(worker, base + reject_tail, tool_workspace_id(name),
                                            max_rounds=card.get("max_tool_rounds", 12),
                                            stage=_stage or None, artifact=_art, attempt=rnd,
                                            lineage=_tool_lineage_token(ws),
                                            branch=bool(card.get("branch")))
                except RuntimeError as e:
                    # The agent's own round limit ("checkpoint retained") is a FAILED ATTEMPT, not a
                    # harness crash: filed as CRASH it was re-queued as worker-unavailable three
                    # times and then blocked the criterion as infrastructure (live, 2026-09-20).
                    # An empty round lets the loop's own verdicts (stagnation, max rounds) apply.
                    emit("tooljob_limit", round=rnd, error=str(e)[:200])
                    # Preserve the AUTHORITATIVE tool workspace (not the local verified ws): the
                    # worker's partial files live there, so oracle/diagnosis must inspect it, and the
                    # failure is a tool boundary, not the worker's competence (seams 3,5).
                    _tool_ws = str(ws)
                    try:
                        import tooljob as _tj
                        _tw = _tj.workspace_dir(tool_workspace_id(name))
                        if _tw:
                            _tool_ws = str(_tw)
                    except Exception:
                        pass
                    r = {"content": str(e)[:300], "workspace": _tool_ws, "failure_kind": "tool-limit"}
            # For provenance, SEAL against the bytes the worker actually wrote to the deliverable
            # (recovered from the tool transcript / per-job workspace and registered as a capture),
            # not the chat reply -- a tool worker's reply is not the artifact. The chat reply is
            # still available in r["content"] for the judge; the artifact-write bytes are what bind.
            output = r.get("artifact_content")
            if output is None:
                output = r["content"]
            review_root = r["workspace"]
            _reply = r.get("content") or ""          # the worker's chat reply -- NOT the sealed artifact
            _fk = r.get("failure_kind") or ""
            # Verification<->integration consistency (doc 05): flag staged dependencies the worker
            # rewrote in its own workspace. Integration promotes only accepted members, so these
            # edits are NOT imported; recording them keeps the divergence from passing silently.
            captured["stage"] = _stage
            _unaccepted = _unaccepted_edits(_stage, review_root, _art) if _stage else []
            if _unaccepted:
                emit("unaccepted_dependency_edits", round=rnd, files=", ".join(_unaccepted)[:200])
            captured["unaccepted_edits"] = _unaccepted
            captured["round"] = rnd
            captured["capture"] = call_layer.find_capture(output or "")
        elif redo_mode == "conversation":
            # persistent multi-turn: the worker KEEPS the whole thread across rounds -- its own prior
            # drafts and every correction stay in context, so it reasons from the full conversation
            # instead of one-shotting from a rebuilt prompt each time.
            if not convo:
                convo.append({"role": "user", "content": base})
            else:
                convo.append({"role": "user", "content": "Your previous attempt was REJECTED. Fix exactly "
                              f"this, keeping everything already correct unchanged:\n{guidance}"})
            msg, _ti = chat(worker, convo, max_tokens=_LOOP.worker_max_tokens, timeout=200)
            output = _take(msg, _ti, rnd)
            convo.append({"role": "assistant", "content": output})
            output = _keep_artifact(ws, output, _prior_artifact(ws),
                                    artifact=card.get("artifact") or "output.md", rnd=rnd)
            review_root = str(ws)
            emit("convo", round=rnd, turns=len(convo), ctx_chars=sum(len(m["content"]) for m in convo))
        else:  # rewrite: a fresh unanchored swing each round, blind to the prior draft
            msg, _ti = chat(worker, [{"role": "user", "content": base + reject_tail}], max_tokens=_LOOP.worker_max_tokens, timeout=200)
            output = _take(msg, _ti, rnd)
            output = _keep_artifact(ws, output, _prior_artifact(ws),
                                    artifact=card.get("artifact") or "output.md", rnd=rnd)
            review_root = str(ws)
        emit("worker", round=rnd, worker=worker, chars=len(output), tools=bool(card.get("tools")), redo=redo_mode)

        # Question a delivered draft BEFORE the check. A missing file is a delivery failure:
        # Spark is not asked to critique work that is not there. A revision is kept only when
        # the check passes; a revision that fails restores a draft that passes.
        _art = card.get("artifact") or "output.md"
        if _fk:
            live_worker.delivery_failure = _fk
        elif not has_deliverable(review_root, _art, output):
            live_worker.delivery_failure = "no deliverable"
        elif skeptic_bounce and redo_mode == "conversation" and convo and not card.get("tools"):
            pre_capture = captured.get("capture")

            def _ask(_draft):
                q = skeptic.run(f"The deliverable is `{_art}`. WORKER {worker} output:\n{_draft[:600]}",
                                SKEPTIC_WORKER, review_root, 8, artifact=_art,
                                system=skeptic.SYSTEM_QUESTION) or ""
                emit("skeptic_q", round=rnd, q=q[:280])
                return q

            def _revise(questions):
                convo.append({"role": "user", "content": "A skeptical reviewer raised these questions "
                              "(technical, big-picture, improvement):\n" + questions + "\n\nAddress EACH "
                              "one. Where a question reveals a real problem, fix it; where it does not, "
                              "keep your answer and briefly say why it holds. Return the COMPLETE answer "
                              "in the required format, not just replies to the questions."})
                msg, _ti = chat(worker, convo, max_tokens=_LOOP.worker_max_tokens, timeout=200)
                revised = _take(msg, _ti, rnd)
                convo.append({"role": "assistant", "content": revised})
                return revised

            def _write(text):
                _keep_artifact(ws, text, "", artifact=_art, rnd=rnd)

            info = apply_skeptic_bounce(
                output, has_file=True, ask=_ask, revise=_revise, write=_write,
                check=lambda: live_oracle(review_root))
            output = info["text"]
            if info["reverted"]:
                captured["capture"] = pre_capture
            if info["asked"]:
                emit("worker_answer", round=rnd, chars=len(output), kept=info["kept"] == "revision",
                     reverted=info["reverted"])
            live_worker.last_question = info.get("questions") or ""
        captured["round"] = rnd
        captured["commentary"] = {"reply": (_reply or output or ""), "failure_kind": _fk,
                                  "unaccepted_edits": captured.get("unaccepted_edits") or []}
        return output, review_root, ()

    live_worker.last_question = ""

    def live_skeptic(rnd, output, review_root):
        if skeptic_bounce and redo_mode == "conversation" and convo:
            # the skeptic already asked and the worker answered in-thread; tell the judge that.
            return ("SKEPTIC asked: " + (live_worker.last_question or "(no question raised)") +
                    "  -- the worker answered this in-thread; judge the (revised) output.")
        _art = card.get("artifact") or "output.md"
        return skeptic.run(f"The deliverable is `{_art}`. WORKER {worker} output claims: {output[:400]}",
                           SKEPTIC_WORKER, review_root, 8, system=skeptic.SYSTEM_REVIEW,
                           artifact=_art) or "SKEPTIC: (no note)"

    def live_judge(rnd, output, note):
        judge_card = card_text + (
            "\n\nREPLY-FORMAT the worker was REQUIRED to follow (do NOT fault the worker for "
            "following it -- e.g. a byline, heading, or preamble it prescribes is intended, not "
            "a fabrication):\n" + mode_text if mode_text else "")
        # Give the judge the deterministic oracle outcome so it trusts ground truth over a
        # misreading skeptic (e.g. skeptic says "no tests" when the oracle just ran them).
        oracle_str = oracle_coverage(card)
        ruling = architect.rule(brief, output, note, card=judge_card, oracle=oracle_str)
        emit("ruling", round=rnd, ruling=(ruling or "")[:200])
        print(f"  [round {rnd}] {ruling.splitlines()[0][:100] if ruling else '(no ruling)'}")
        return ruling

    def live_commentary(rnd):
        """The worker's REPLY (commentary) and failure-kind for THIS round -- a channel separate
        from the sealed artifact bytes. Proposals/questions are extracted from the reply, not the
        artifact body (seam 2), and a tool-limit is carried for correct attribution (seams 3,5)."""
        c = captured.get("commentary") or {}
        return c if captured.get("round") == rnd else {}

    def live_capture(rnd):
        """The call layer's capture for THIS round, or None.

        The round check is the point: a round whose worker call raised leaves the previous
        round's capture in the slot, and handing that to seal() would bind this round's artifact
        to last round's response -- stale provenance wearing a fresh record. Returning None makes
        the round unsealed, which acceptance already refuses."""
        return captured["capture"] if captured["round"] == rnd else None

    def live_diagnose(rnd, reason, output, review_root):
        # A FRESH review of the failing attempt -- its OWN skeptic.run, never the cached
        # conversation-bounce text. It reads the assignment workspace (staged modules + the
        # deliverable) and is asked to name, concretely, what must change to satisfy the check.
        claim = ("A local worker's attempt at {0} FAILED the frozen check with: {1}. Read the "
                 "modules staged in THIS workspace (including any the deliverable must call) and the "
                 "current deliverable, and state concretely what the deliverable must change to "
                 "satisfy the check -- exact function names, arguments and return values. If the "
                 "workspace does not contain enough to tell, say so plainly.").format(
                     card.get("artifact") or "the deliverable", reason)
        try:
            return skeptic.run(claim, SKEPTIC_WORKER, review_root, 6, system=skeptic.SYSTEM_REVIEW,
                               artifact=(card.get("artifact") or "output.md")) or ""
        except Exception as e:
            return ""

    hooks = Hooks(worker=live_worker, oracle=live_oracle, skeptic=live_skeptic, diagnose=live_diagnose,
                  judge=live_judge, commentary=live_commentary,
                  evidence=lambda root: authority_evidence | workspace_evidence(
                      root, artifacts=[card.get("artifact") or "output.md"]),
                  criteria=lambda root, ok=None: evaluate_criteria(criteria, root, oracle_ok=ok),
                  capture=live_capture)

    r = run_loop(card, ws, hooks, emit=emit, max_rounds=a.max_rounds, criteria=criteria,
                 guidance=guidance, k=a.stagnation_k,
                 investigation_budget=(a.investigation_budget or None), receipt=receipt)
    print("\n" + r["receipt"].render())
    preserve(r["outcome"], EXIT_CODES.get(r["outcome"], 1), r["rounds"], r["best_artifact"],
             r["history"], r["basis"])

if __name__ == "__main__":
    main()
