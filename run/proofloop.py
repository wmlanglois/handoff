"""Map and milestone connections on the existing ledger and integrate checkpoints.

Handoff.py is a Riftwake adapter. This module is the reusable proof loop:
approved scope, approved interface map, accepted bytes, one journey, one follow-up.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "run"))


class ProofError(Exception):
    pass


def launch_summary(mode, decisions, seconds):
    """Visible mode and budgets. Autonomous is never a silent max_decisions=0."""
    if mode not in ("packet-only", "autonomous"):
        raise ProofError("mode must be packet-only or autonomous")
    return {
        "mode": mode,
        "decisions": int(decisions),
        "seconds": int(seconds),
        "follow_up": mode == "autonomous",
    }


def format_summary(summary):
    return ("mode {mode}; decision budget {decisions}; time budget {seconds}s; "
            "follow-up {follow_up}").format(**summary)


def milestone_text(scoped):
    """The launched-and-working behavior, kept separate from the North Star. Extract it however the
    scope-writer phrased or cased the marker (a production frontier scope naturally writes
    "launched and working" inside a markdown header, matching the intake reference card). If the
    marker sits in a header, return the first real paragraph of that section; otherwise the paragraph
    from the marker. Raises only when the scope never describes the launched-and-working picture."""
    text = scoped.replace("\r\n", "\n")
    at = text.casefold().find("launched and working")
    if at < 0:
        raise ProofError("scoped.md has no launched-and-working milestone")
    line_start = text.rfind("\n", 0, at) + 1
    line_end = text.find("\n", at)
    line = text[line_start:(line_end if line_end >= 0 else len(text))]
    if line.lstrip().startswith("#"):
        # a header: return the first substantive paragraph of the section body
        for block in text[(line_end if line_end >= 0 else len(text)):].split("\n\n"):
            b = block.strip()
            if b and not b.lstrip().startswith("#") and b != "---":
                return b
    return text[at:].split("\n\n", 1)[0].strip()


def open_proof(scoped, criteria, project_root, *, goal_text=None, root=None, integration_check=None):
    """Create a goal whose text is the full scoped.md and whose milestone is separate."""
    import goals
    full = scoped if isinstance(scoped, str) else scoped.decode("utf-8")
    gid = goals.create(goal_text if goal_text is not None else full, criteria,
                       project_root=str(project_root), root=root)
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()
    goals.bind_scope(gid, full, path="scoped.md", approval_sha=digest, root=root)
    return gid


def scope_still_approved(goal_id, scoped_text, *, root=None):
    import goals
    doc = goals.state(goal_id, root=root)
    bound = (doc.get("scope") or {}).get("sha256")
    current = hashlib.sha256(scoped_text.encode("utf-8")).hexdigest()
    return bound == current, bound, current


def approve_map(goal_id, spec, approver, *, fixture=False, root=None):
    import goals
    return goals.approve_interface_map(goal_id, spec, approver, fixture=fixture, root=root)


def record_accepted(goal_id, assignment, dest, data, *, root=None):
    """Receipt-bound bytes. A second assignment cannot claim the same path."""
    import goals
    digest = hashlib.sha256(data).hexdigest()

    def _write(gid, root=None):
        doc = goals._load(gid, root)
        owned = doc.setdefault("accepted_bytes", {})
        prev = owned.get(dest)
        if prev and prev.get("assignment") != assignment:
            raise goals.GoalError("refusing a second writer on {0}".format(dest))
        owned[dest] = {"sha256": digest, "assignment": assignment}
        goals._save(doc, root)
        import integrate
        target = integrate.contained_path(integrate._tree(gid) / "accepted", dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return digest

    _write = goals._serialised(_write)
    return _write(goal_id, root=root)


def blobs_from_receipts(goal_id, runs_root=None):
    """Accepted artifact bytes from worker receipts. A hashless receipt is refused.

    A later accepted repair of the same path replaces the earlier file. Two acceptances
    with no later one are a second writer and are refused.
    """
    import os
    import goals
    import integrate
    runs_root = runs_root or os.environ.get("FLEET_RUNS_DIR")
    doc = goals.state(goal_id)
    grouped = {}
    for name, rec in (doc.get("assignments") or {}).items():
        if rec.get("disposition") != "accepted":
            continue
        dest = ((rec.get("contract") or {}).get("dest") or "").replace("\\", "/")
        if not dest:
            continue
        grouped.setdefault(dest, []).append((name, rec))
    blobs = {}
    for dest, rows in grouped.items():
        if len(rows) > 1 and not any((rec.get("contract") or {}).get("replaces") for _name, rec in rows):
            raise ProofError("refusing a second writer on {0}".format(dest))
        name, rec = rows[-1]
        rid = rec.get("run_id") or name
        _matched, data, _sha = integrate._resolve_accepted(rid, [dest], runs_root=runs_root)
        blobs[dest] = data
    return blobs


def accepted_blobs(goal_id):
    """Bytes previously recorded, checked against the receipt hash."""
    import goals
    import integrate
    doc = goals.state(goal_id)
    out = {}
    for dest, row in (doc.get("accepted_bytes") or {}).items():
        path = integrate.contained_path(integrate._tree(goal_id) / "accepted", dest)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ProofError("accepted bytes for {0} no longer match the receipt".format(dest))
        out[dest] = data
    return out


_BASELINE_SKIP = {"__pycache__", ".git", ".handoff", ".pytest_cache", "node_modules"}


def _baseline_dir(goal_id):
    import integrate
    return integrate._tree(goal_id) / "baseline"


def _baseline_marker(goal_id):
    import integrate
    return integrate._tree(goal_id) / "baseline.frozen.json"


def baseline_is_complete(goal_id):
    """True only when the frozen baseline exists AND its completeness marker lists every file with a
    matching hash. A partial copy (crash mid-snapshot) has no marker, or a mismatching one, so it is
    NEVER mistaken for a complete freeze. An empty project_root freezes to an empty-but-complete
    baseline (marker with count 0)."""
    import hashlib
    frozen, marker = _baseline_dir(goal_id), _baseline_marker(goal_id)
    if not (frozen.is_dir() and marker.is_file()):
        return False
    try:
        m = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    manifest = m.get("manifest")
    if not isinstance(manifest, dict) or len(manifest) != m.get("count"):
        return False
    for rel, sha in manifest.items():
        f = frozen / rel
        try:
            if not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest() != sha:
                return False
        except OSError:
            return False
    return True


def freeze_baseline(goal_id):
    """Snapshot project_root into an IMMUTABLE, VERIFIABLY COMPLETE baseline ONCE, so every wave
    builds on the same tree even if project_root is edited afterwards. ATOMIC and FAIL-CLOSED: the
    snapshot is written to a temp dir, swapped in with a single os.replace, and only then is a
    completeness marker (a manifest of every file's hash) written LAST. If anything fails mid-way the
    temp is removed and the error is RAISED -- a partial baseline never exists and a caller must not
    proceed. Returns the frozen Path, None when there is nothing to freeze (no project_root, or a
    passing checkpoint already is the baseline)."""
    import hashlib
    import integrate
    import os
    import shutil
    import tempfile
    if baseline_is_complete(goal_id):
        return _baseline_dir(goal_id)
    live = integrate.live_dir(goal_id)
    if live.is_dir() and any(x.is_file() for x in live.rglob("*")):
        return None                     # a passing checkpoint already IS the frozen baseline
    import goals
    proot = (goals.state(goal_id) or {}).get("project_root")
    if not (proot and Path(proot).is_dir()):
        return None
    proot = Path(proot).resolve()
    frozen, marker = _baseline_dir(goal_id), _baseline_marker(goal_id)
    # The harness's OWN state (ledger, integrate trees, run workspaces) is never a project file, even
    # when a test points project_root at a directory that happens to contain them. Exclude them by
    # resolved-path containment so the snapshot copies only real project files.
    # Exclude the harness's OWN state (ledger, integrate trees, run workspaces) ONLY when it is nested
    # INSIDE project_root -- that is the case where snapshotting project_root would traverse into it
    # (e.g. a test whose project_root is a dir that also holds goals/). When project_root itself lives
    # UNDER a harness dir (a project kept inside runs/), those harness dirs are SIBLINGS the snapshot
    # never reaches, so excluding them would wrongly drop the project's own files (a real launcher).
    import os as _os
    _harness = []
    import fleet as _fleet
    for h0 in (_fleet.goals_dir(), _fleet.integrate_dir(), _fleet.runs_root()):
        try:
            h = Path(h0).resolve()
            h.relative_to(proot)          # keep only harness dirs nested under project_root
            _harness.append(h)
        except (OSError, ValueError):
            pass

    def _under_harness(p):
        for h in _harness:
            try:
                p.relative_to(h)
                return True
            except ValueError:
                continue
        return False

    frozen.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=str(frozen.parent), prefix=".baseline-staging-"))
    manifest = {}
    try:
        for src in sorted(proot.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(proot).as_posix()
            if rel.split("/", 1)[0] in _BASELINE_SKIP or _under_harness(src.resolve()):
                continue
            data = src.read_bytes()
            t = integrate.contained_path(tmp, rel)
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_bytes(data)
            manifest[rel] = hashlib.sha256(data).hexdigest()
        if marker.exists():
            marker.unlink()             # invalidate any prior marker BEFORE swapping the tree
        if frozen.exists():
            shutil.rmtree(frozen)
        os.replace(str(tmp), str(frozen))          # atomic swap (same filesystem)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    overall = hashlib.sha256(json.dumps(sorted(manifest.items())).encode("utf-8")).hexdigest()
    marker.write_text(json.dumps({"manifest": manifest, "sha256": overall, "count": len(manifest)}),
                      encoding="utf-8")
    return frozen


def project_baseline(goal_id):
    """The FROZEN starting tree the candidate is built ON TOP OF. Order: the last passing checkpoint
    (integrate.live_dir); else the COMPLETE immutable baseline snapshot taken at map approval; else,
    only if no map has been approved, the live project_root (a pre-approval convenience). FAIL-CLOSED:
    once a map was approved WITH a frozen baseline, a missing/incomplete snapshot never falls back to
    the mutable project_root -- it raises. Returns (Path|None, origin). READ-ONLY: assemble copies and
    never writes back."""
    import integrate
    import goals
    live = integrate.live_dir(goal_id)
    if live.is_dir() and any(x.is_file() for x in live.rglob("*")):
        return live, "checkpoint"
    if baseline_is_complete(goal_id):
        return _baseline_dir(goal_id), "frozen"
    doc = goals.state(goal_id) or {}
    imap = doc.get("interface_map") or {}
    if imap.get("spec") and imap.get("baseline_frozen"):
        raise ProofError("the frozen project baseline is missing or incomplete after map approval; "
                         "refusing to fall back to the mutable project_root (fail closed)")
    proot = doc.get("project_root")
    if proot and Path(proot).is_dir():
        return Path(proot), "project_root"          # only before a map+freeze exists
    return None, "none"


def assemble(goal_id, blobs, *, group="slice"):
    """Build an ISOLATED candidate for the milestone journey: a copy of the frozen project baseline
    (last passing checkpoint or project_root -- so a real launcher and its untouched neighbours
    survive) with ONLY the receipt-bound accepted destinations in `blobs` overlaid on top. `blobs`
    is {dest: bytes} and in the production path comes from blobs_from_receipts, i.e. bytes resolved
    against the worker's receipt hash; a pre-existing baseline file is NOT credited as accepted work
    unless a receipt overlays it. Path escapes are refused (integrate.contained_path). The candidate
    is rebuilt from scratch each call; on any failure it is torn down and the baseline/live tree is
    left exactly as it was."""
    import shutil
    import integrate
    cand = integrate.candidate_dir(goal_id, group)
    if cand.exists():
        shutil.rmtree(cand)
    cand.mkdir(parents=True)
    try:
        base, origin = project_baseline(goal_id)
        if base is not None:
            base = base.resolve()
            for src in sorted(base.rglob("*")):
                if not src.is_file():
                    continue
                rel = src.relative_to(base).as_posix()
                if rel.split("/", 1)[0] in _BASELINE_SKIP:
                    continue
                target = integrate.contained_path(cand, rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(src.read_bytes())
        for dest, data in blobs.items():
            target = integrate.contained_path(cand, dest)     # refuses escapes / drive / parent segments
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    except BaseException:
        shutil.rmtree(cand, ignore_errors=True)               # never leave a half-built candidate
        raise
    tree_hash = hashlib.sha256()
    for path in sorted(cand.rglob("*")):
        if path.is_file():
            tree_hash.update(path.relative_to(cand).as_posix().encode())
            tree_hash.update(path.read_bytes())
    return {"candidate": str(cand), "sha256": tree_hash.hexdigest(), "baseline": origin}


# A module that imports and exposes every public NAME (so `from x import value` still binds), but
# whose callables return a value equal to NOTHING. It distinguishes "the command uses the delivered
# BEHAVIOUR" from "the command merely imports the name" -- an empty module could not.
_MUTANT_SOURCE = (
    "class _Wrong(object):\n"
    "    def __eq__(self, other): return False\n"
    "    def __ne__(self, other): return True\n"
    "    def __bool__(self): return False\n"
    "    def __hash__(self): return 0\n"
    "    def __call__(self, *a, **k): return _Wrong()\n"
    "    def __iter__(self): return iter(())\n"
    "    def __getattr__(self, n): return _Wrong()\n"
    "_wrong = _Wrong()\n"
    "def __getattr__(name):\n"
    "    return _wrong\n"
)


def milestone_completion(candidate, command, deliverable_dests, timeout=20):
    """Decide whether a PASSED milestone journey may make the project DONE, WITHOUT claiming a
    keyword/filename match proves anything. Run the command ONCE, in a fresh isolated process, against
    a MUTANT copy of the candidate in which each accepted deliverable is replaced by a module that
    still imports and still exposes every public name, but whose callables return a value equal to
    nothing. Then:
      * mutant exits NON-zero  -> the command's success DEPENDS on the delivered BEHAVIOUR   -> 'sensitive'
      * mutant exits ZERO       -> success is independent of behaviour (import-only, constant  -> 'independent'
                                   exit, or a self-contained side effect)
      * timeout / cannot run    -> dependence cannot be established                            -> 'inconclusive'
    Returns (status, detail). The caller grants DONE ONLY on 'sensitive' AND a journey that already
    passed on the REAL candidate; 'independent' and 'inconclusive' both FAIL CLOSED (no DONE). A bare
    `from x import value` is 'independent' because the mutant still binds `value`; a timeout is never
    proof. This is a SENSITIVITY check, not a quality certificate -- the human approves the launcher."""
    import shutil
    import subprocess
    import tempfile
    dests = {d for d in (deliverable_dests or []) if d}
    # NEVER hollow the entry file(s) the command itself runs: a hollowed entry point is a no-op that
    # exits 0, which would falsely read as "the command is independent of the work". Hollow only the
    # OTHER deliverables (the dependencies the entry imports), so a real launcher meeting a hollowed
    # dependency fails.
    entry_files = {str(t).replace("\\", "/") for t in (command or []) if isinstance(t, str) and t.endswith(".py")}
    to_hollow = {d for d in dests if d.replace("\\", "/") not in entry_files}
    if not to_hollow:
        return "inconclusive", "no non-entry deliverables to test the command's dependence against"
    probe = Path(tempfile.mkdtemp(prefix="milestone-mutant-"))
    try:
        for src in sorted(Path(candidate).rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(candidate).as_posix()
            t = probe / rel
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_bytes(_MUTANT_SOURCE.encode("utf-8") if rel in to_hollow else src.read_bytes())
        try:
            r = subprocess.run(list(command), cwd=str(probe), capture_output=True, text=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            return "inconclusive", "the command timed out against the mutant; dependence not established"
        except Exception as e:
            return "inconclusive", "the command could not run against the mutant: {0}".format(str(e)[:200])
        if r.returncode == 0:
            return "independent", ("the command still exits 0 when the delivered behaviour is wrong "
                                   "(mutant output: {0})".format((r.stdout + r.stderr)[-200:]))
        return "sensitive", "the command fails when the delivered behaviour is wrong (mutant exit {0})".format(
            r.returncode)
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def run_journey(candidate, command):
    """Fresh process. command is an argument list."""
    proc = subprocess.run(command, cwd=str(candidate), capture_output=True, text=True, timeout=30)
    transcript = (proc.stdout or "") + (proc.stderr or "")
    return {"ok": proc.returncode == 0, "code": proc.returncode, "transcript": transcript,
            "command": list(command)}


def spark_note(journey, files, finding):
    """files is the set of paths Spark was actually given. A citation outside it is refused.

    `journey` is the dict from run_journey. An empty stdout is still a transcript.
    Passing None means Spark was not given the run."""
    if not isinstance(journey, dict) or "transcript" not in journey:
        raise ProofError("spark was not given the journey transcript")
    transcript = journey.get("transcript") or ""
    cited = list((finding or {}).get("files") or [])
    missing = [p for p in cited if p not in files]
    if missing:
        raise ProofError("spark cited files it was not given: {0}".format(", ".join(missing)))
    if not finding or finding.get("found") is False:
        return {"found": False, "text": "none found", "files": cited, "transcript": True}
    return {"found": True, "text": finding.get("text") or "", "files": cited,
            "command": finding.get("command"), "observed": finding.get("observed"),
            "transcript": True}


def first_failure(events):
    """The earliest consequential event in the trajectory, not a preferred category."""
    kinds = {"delivery", "check_defect", "assertion", "journey"}
    for event in events or []:
        if event.get("kind") in kinds:
            return event
    return None


def record_check_defect(goal_id, *, oracle, requirement, failure, root=None):
    import goals
    before = goals.state(goal_id, root=root).get("check_defects") or []
    row = goals.record_check_defect(goal_id, oracle=oracle, requirement=requirement,
                                    failure=failure, root=root)
    after = goals.state(goal_id, root=root)
    if (after.get("scope") or {}).get("text") and oracle in ((after.get("scope") or {}).get("text") or ""):
        pass
    # The stored oracle text on the defect is the hash only. Confirm the caller's oracle
    # string was not written back as a replacement.
    stored = (after.get("check_defects") or [])[-1]
    if "oracle" in stored:
        raise ProofError("a check defect must not store a rewritten oracle")
    if len(after.get("check_defects") or []) != len(before) + 1:
        raise ProofError("the original failure was not kept")
    return row


def follow_up(goal_id, *, evidence, owner, source, root=None):
    """One packet or one question. source is journey, spark, or milestone."""
    import goals
    if source not in ("journey", "spark", "milestone"):
        raise ProofError("follow-up source must be journey, spark, or milestone")
    if not (evidence or "").strip():
        raise ProofError("follow-up requires evidence")
    if not (owner or "").strip():
        raise ProofError("follow-up requires an ownership decision")
    doc = goals.state(goal_id, root=root)
    if not doc.get("interface_map"):
        raise ProofError("no approved interface map")
    name = "follow-" + hashlib.sha256(evidence.encode("utf-8")).hexdigest()[:8]
    packet = {
        "name": name,
        "owner": owner,
        "source": source,
        "evidence": evidence,
        "criterion_id": "milestone",
    }
    goals.record_trace(goal_id, {
        "kind": "follow_up", "source": source, "owner": owner,
        "evidence_sha256": hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
    }, root=root)
    return packet


def promote_checkpoint(goal_id, candidate, group, command, journey=None):
    """Promote the candidate to the live checkpoint. `journey` is the result already established on
    THIS candidate; when given it is trusted and the command is NOT re-run (avoiding a second, possibly
    side-effecting execution). Promotion only happens for a passing journey. When no journey is passed
    (direct callers/tests), the command is run once here."""
    import integrate
    if journey is None:
        journey = run_journey(candidate, command)
    if not journey.get("ok"):
        return {"promoted": False, "candidate": str(candidate),
                "boundary": "journey failed; candidate preserved",
                "transcript": journey.get("transcript", "")}
    cand = Path(candidate)
    if not integrate._meta_path(goal_id).is_file():
        integrate.bootstrap(goal_id, {
            "groups": [{"id": group, "assignments": ["base", "use"]}],
            "check_source": (
                "import subprocess\n"
                "r = subprocess.run({0!r})\n"
                "assert r.returncode == 0\n"
            ).format(list(command)),
            "project_root": str(cand),
        })
    integrate._promote(goal_id, group, cand)
    meta = integrate._load_meta(goal_id)
    g = meta["groups"].setdefault(group, {"members": ["base", "use"], "integrated": {}})
    g["state"] = integrate.PASSED
    g["reason"] = ""
    g["live_intact"] = True
    integrate._save_meta(goal_id, meta)
    try:
        integrate._ledger_sync(goal_id, meta)
    except Exception:
        pass
    import goals
    goals.record_journey(goal_id, passed=True, transcript=journey["transcript"])
    live = integrate.live_dir(goal_id)
    digest = hashlib.sha256()
    for path in sorted(p for p in live.rglob("*") if p.is_file()):
        digest.update(path.relative_to(live).as_posix().encode())
        digest.update(path.read_bytes())
    return {"promoted": True, "sha256": digest.hexdigest()}
