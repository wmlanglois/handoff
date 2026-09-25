"""Assemble accepted worker artifacts into a recoverable project checkpoint.

Handoff owns this. Local workers author the files. Integration applies exact accepted
lineage bytes onto a candidate, runs a frozen check, then promotes. Failures keep the
last live checkpoint and dispatch bounded repair through the queue/tool-worker path.
"""
from __future__ import annotations

import hashlib
import re
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "run"))
_WS_SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")

WAITING, PASSED, FAILED, CONFLICT, UNBOUND, EXHAUSTED = (
    "waiting", "passed", "failed", "conflict", "unbound", "exhausted",
)

# A failed group gets at most this many integration-repair attempts across ALL drains and
# controller restarts (the count is derived from the persisted repair assignments in the ledger,
# so it survives restarts). On exhaustion the working checkpoint is preserved and the group is
# marked EXHAUSTED -- an actionable blocked outcome, not an endless succession of fresh budgets.
MAX_REPAIR_ATTEMPTS = 3

_ASSERT_LINE = re.compile(r"^[ \t]*assert[ \t]+.+\S[ \t]*$", re.M)


class IntegrateError(Exception):
    pass


def assertion_lines(source: str) -> list:
    """The mechanical assertions in a frozen check, in source order."""
    return [ln.strip() for ln in _ASSERT_LINE.findall(source or "")]


_TRUTHY_LITERAL = re.compile(r"""^(?:True|[1-9][0-9]*(?:\.[0-9]+)?|"[^"]+"|'[^']+')$""", re.I)
# `... or True` / `... or 7` (string contents are blanked before these run, so a literal
# like `== "x or True"` is not mistaken for a tautology).
_OR_TRUTHY = re.compile(r"""\bor\s+(?:True|[1-9][0-9]*(?:\.[0-9]+)?)(?=\s|$|\)|:)""", re.I)
_TRUTHY_OR = re.compile(r"""(?:^|\()\s*(?:True|[1-9][0-9]*(?:\.[0-9]+)?)\s+or\b""", re.I)
_LITERAL_TRUE = {"true", "true == true", "1", "1 == 1", "pass"}


def _assert_condition(line: str) -> str:
    """The condition expression of an `assert` line, with any trailing message removed."""
    s = str(line or "").strip()
    if not re.match(r"assert\b", s, re.I):
        return ""
    s = s[len("assert"):].strip().rstrip(";")
    depth = 0
    inq = None
    for i, ch in enumerate(s):
        if inq:
            if ch == inq and s[i - 1] != "\\":
                inq = None
            continue
        if ch in "\"'":
            inq = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            return s[:i].strip()
    return s.strip()


def assert_can_fail(line: str) -> bool:
    """False when an `assert` line is tautological by construction -- a bare truthy literal, a
    literal identity (`1 == 1`), or an `or`-disjunction with a truthy-literal operand
    (`... or True`, `True or ...`) -- so it can never fail on a wrong artifact. Real assertions
    (comparisons, variable disjunctions, existence checks like `os.path.exists(...)`) return True."""
    cond = _assert_condition(line)
    if not cond:
        return False
    flat = re.sub(r"\s+", " ", cond).strip()
    if flat.lower() in _LITERAL_TRUE or _TRUTHY_LITERAL.match(flat):
        return False
    blanked = re.sub(r"\"[^\"]*\"|'[^']*'", '""', flat)  # neutralize string contents
    if _OR_TRUTHY.search(blanked) or _TRUTHY_OR.search(blanked):
        return False
    return True


def vacuous_oracle(oracle) -> bool:
    """True when an oracle cannot fail on a wrong artifact.

    Two ways an oracle is vacuous: the whole thing is empty / `assert True` / `pass`, or it
    LOOKS like a check but every one of its assertions is tautological by construction
    (`... or True`, a bare truthy literal, `1 == 1`). The second is the omission that lets a
    packet go green having verified nothing -- see plan item A07 (contract-to-check coverage)."""
    text = re.sub(r"(?m)#.*$", "", str(oracle or ""))
    flat = re.sub(r"\s+", " ", text).strip().rstrip(";")
    if flat.lower() in {"", "assert true", "pass"}:
        return True
    asserts = assertion_lines(oracle)
    if asserts and not any(assert_can_fail(a) for a in asserts):
        return True
    return False


def worker_oracle(check_source: str) -> str:
    """The same frozen check, pointed at the worker workspace. Not a substitute assertion."""
    if not (check_source or "").strip():
        raise IntegrateError("integration repair has no frozen check to use as its oracle")
    return "import os\nos.environ['INTEGRATION_ROOT'] = os.getcwd()\n" + str(check_source)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(p: Path) -> str:
    return _sha(p.read_bytes())


def _tree(goal_id: str) -> Path:
    base = Path(os.environ.get("FLEET_INTEGRATE_DIR") or (ROOT / "runs" / "integrate"))
    return Path(base) / goal_id


def live_dir(goal_id: str) -> Path:
    return _tree(goal_id) / "live"


def checkpoint_root(goal_id: str) -> Path:
    return live_dir(goal_id)


def candidate_dir(goal_id: str, group_id: str) -> Path:
    return _tree(goal_id) / "groups" / group_id / "candidate"


def _meta_path(goal_id: str) -> Path:
    return _tree(goal_id) / "meta.json"


def _check_path(goal_id: str) -> Path:
    return _tree(goal_id) / "check" / "integration-check.py"


def _load_meta(goal_id: str) -> dict:
    p = _meta_path(goal_id)
    if not p.is_file():
        raise IntegrateError(
            "integration state missing at {0}; recover by re-approving the goal "
            "with its declared integration spec".format(p)
        )
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise IntegrateError(
            "integration state unreadable at {0}: {1}; do not treat as disabled".format(p, e)
        ) from e


def _save_meta(goal_id: str, meta: dict) -> None:
    p = _meta_path(goal_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    tmp.replace(p)


def enabled(goal_id: str) -> bool:
    try:
        import goals
        doc = goals.state(goal_id)
        if (doc.get("integration") or {}).get("declared"):
            return True
    except Exception:
        pass
    return _meta_path(goal_id).is_file()


def groups_passed(goal_id: str) -> bool:
    ok, _ = completion_state(goal_id)
    return ok


def group_states(goal_id: str) -> dict:
    return dict(_load_meta(goal_id).get("groups") or {})


def dest_is_forbidden(dest: str) -> str:
    """Return a problem string, or empty if dest is a relative contained path."""
    if dest is None or not str(dest).strip():
        return "empty dest"
    dest = str(dest)
    if dest != dest.strip():
        return "dest has surrounding whitespace"
    p = Path(dest)
    if p.is_absolute():
        return "absolute dest is forbidden"
    if len(dest) >= 2 and dest[1] == ":":
        return "drive-qualified dest is forbidden"
    if dest.startswith("/") or dest.startswith("\\"):
        return "absolute dest is forbidden"
    if any(part in ("..", "") for part in p.parts):
        return "dest escapes via empty or parent segment"
    if any(part.endswith(":") for part in p.parts):
        return "drive-qualified dest is forbidden"
    return ""


def contained_path(tree: Path, dest: str) -> Path:
    err = dest_is_forbidden(dest)
    if err:
        raise IntegrateError(err + ": " + dest)
    tree = Path(tree).resolve()
    target = (tree / dest).resolve()
    try:
        target.relative_to(tree)
    except ValueError as e:
        raise IntegrateError(
            "dest {0!r} is not inside {1} (resolved ancestry, not prefix)".format(dest, tree)
        ) from e
    if target == tree:
        raise IntegrateError("dest must be a file inside the tree, not the tree itself")
    return target


def validate_spec(spec: dict, *, project_root=None) -> dict:
    if not isinstance(spec, dict):
        raise IntegrateError("integration spec must be an object")
    groups = spec.get("groups") or []
    if not groups:
        raise IntegrateError("integration requires at least one group")
    out_groups = []
    seen = set()
    for g in groups:
        gid = (g.get("id") or "").strip()
        members = list(g.get("assignments") or g.get("members") or [])
        if not gid:
            raise IntegrateError("integration group needs an id")
        if gid in seen:
            raise IntegrateError("duplicate integration group id: " + gid)
        if not members:
            raise IntegrateError("integration group {0} has no assignments".format(gid))
        seen.add(gid)
        out_groups.append({"id": gid, "assignments": members})
    src = spec.get("check_source")
    if not (src or "").strip():
        raise IntegrateError("integration requires a frozen check_source")
    proot = spec.get("project_root") or project_root
    if not proot:
        raise IntegrateError("integration requires project_root")
    return {
        "groups": out_groups,
        "check_source": str(src),
        "check_sha256": _sha(str(src).encode("utf-8")),
        "project_root": str(Path(proot).resolve()),
    }


def _copytree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def bootstrap(goal_id: str, spec: dict) -> dict:
    """Create live/ from project_root and freeze the check. Called from approve."""
    spec = validate_spec(spec, project_root=spec.get("project_root"))
    tree = _tree(goal_id)
    tree.mkdir(parents=True, exist_ok=True)
    live = live_dir(goal_id)
    src = Path(spec["project_root"])
    if not src.is_dir():
        raise IntegrateError("project_root does not exist: " + str(src))
    if not live.exists():
        _copytree(src, live)
    chk = _check_path(goal_id)
    chk.parent.mkdir(parents=True, exist_ok=True)
    chk.write_text(spec["check_source"], encoding="utf-8")
    groups = {}
    prev = {}
    if _meta_path(goal_id).is_file():
        try:
            prev = json.loads(_meta_path(goal_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = {}
    for g in spec["groups"]:
        old = (prev.get("groups") or {}).get(g["id"]) or {}
        groups[g["id"]] = {
            "state": old.get("state") or WAITING,
            "reason": old.get("reason") or "",
            "live_intact": True,
            "integrated": old.get("integrated") or {},
            "repairing": bool(old.get("repairing")),
            "members": list(g["assignments"]),
        }
    meta = {
        "goal_id": goal_id,
        "declared": True,
        "check_sha256": spec["check_sha256"],
        "project_root": spec["project_root"],
        "groups": groups,
        "owners": prev.get("owners") or {},
    }
    _save_meta(goal_id, meta)
    return meta


def enable(goal_id, *, groups, check_source, project_root):
    """Compatibility helper. Production callers declare via goals.create/approve."""
    spec = validate_spec(
        {"groups": groups, "check_source": check_source, "project_root": project_root},
        project_root=project_root,
    )
    try:
        import goals
        goals.record_integration(goal_id, spec)
    except Exception:
        bootstrap(goal_id, spec)
    return spec


def _ledger_sync(goal_id: str, meta: dict) -> None:
    try:
        import goals
        goals.record_integration_status(goal_id, meta)
    except Exception:
        pass


def _accepted_lineage(doc: dict, member: str):
    """Exact accepted assignment or later accepted repair whose lineage includes member."""
    assignments = doc.get("assignments") or {}
    chosen = None
    chosen_name = None
    for name, rec in assignments.items():
        if rec.get("disposition") != "accepted":
            continue
        con = rec.get("contract") or {}
        lin = list(con.get("lineage") or [])
        if name != member and member not in lin:
            continue
        if chosen is None or str(rec.get("added") or "") >= str(chosen.get("added") or ""):
            chosen, chosen_name = rec, name
    if chosen is None:
        rec = assignments.get(member)
        if rec and rec.get("disposition") == "accepted":
            return member, rec
    return chosen_name, chosen


def _member_bytes(doc: dict, member: str):
    name, rec = _accepted_lineage(doc, member)
    if rec is None:
        return None, None, None, "assignment {0} is not accepted".format(member)
    con = rec.get("contract") or {}
    rid = rec.get("run_id") or name
    dest = (con.get("dest") or "").strip()
    artifact = con.get("artifact") or "output.md"
    # ONE authoritative accepted-artifact identity: the exact bytes the acceptance receipt bound,
    # verified by hash across the verified AND tool workspaces -- the same resolver repair uses. A
    # tools-authored member's accepted bytes live in the tool workspace, not the stale copy staging
    # left in the verified workspace; binding here is what makes a multi-file candidate out of the
    # ACCEPTED bytes rather than whatever same-named file happens to sit on disk.
    try:
        _matched, data, _sha_hex = _resolve_accepted(rid, [dest, artifact])
    except IntegrateError as e:
        return name, rec, None, str(e)
    if dest and artifact != "output.md" and Path(dest).name != Path(artifact).name:
        # dest file name must match this assignment's own artifact, not another producer's basename
        return name, rec, None, "dest basename does not match this assignment artifact"
    return name, rec, data, ""


def _run_check(goal_id: str, root: Path) -> tuple:
    meta = _load_meta(goal_id)
    chk = _check_path(goal_id)
    if not chk.is_file():
        return False, "frozen integration check is missing; recover from the approved check_source"
    body = chk.read_text(encoding="utf-8")
    if _sha(body.encode("utf-8")) != meta.get("check_sha256"):
        return False, "frozen integration check hash changed; restore the approved check_source"
    env = os.environ.copy()
    env["INTEGRATION_ROOT"] = str(root)
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run(
        [sys.executable, str(chk)],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "check failed").strip()
        return False, err[:2000]
    return True, ""


def _expected_hash(meta: dict, rec: dict, dest: str, live_target: Path) -> str:
    owners = meta.get("owners") or {}
    own = owners.get(dest) or {}
    integ = own.get("sha256")
    if integ:
        return integ
    return (rec.get("contract") or {}).get("base_sha256") or ""


def _apply_member(goal_id, meta, group_id, member, rec, data, cand: Path):
    con = rec.get("contract") or {}
    dest = (con.get("dest") or "").strip()
    artifact = con.get("artifact") or "output.md"
    if not dest:
        if artifact == "output.md":
            return UNBOUND, "unbound output.md is not a project dest"
        dest = artifact
    target = contained_path(cand, dest)
    live_target = contained_path(live_dir(goal_id), dest)
    expected = _expected_hash(meta, rec, dest, live_target)
    current = _sha_file(live_target) if live_target.is_file() else ""
    owners = meta.setdefault("owners", {})
    owner = owners.get(dest)
    rid = rec.get("run_id")
    # Already integrated this exact accepted version: skip write (idempotent).
    g = meta["groups"][group_id]
    prev = (g.get("integrated") or {}).get(member) or {}
    if prev.get("run_id") == rid and prev.get("sha256") == _sha(data) and current == _sha(data):
        return PASSED, "already integrated"
    lin = list((rec.get("contract") or {}).get("lineage") or [])
    if owner and owner.get("producer") not in {member, (rec.get("contract") or {}).get("name")} and member not in lin:
        if owner.get("run_id") != rid:
            return CONFLICT, "dest {0} owned by {1}, not {2}".format(
                dest, owner.get("producer"), member)
    if expected and current and current != expected:
        if current == _sha(data):
            # live already holds these bytes from a prior promote of this lineage
            pass
        else:
            return CONFLICT, "live {0} hash {1} != expected starting hash {2}".format(
                dest, current[:12], expected[:12])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    owners[dest] = {"producer": member, "run_id": rid, "sha256": _sha(data)}
    g.setdefault("integrated", {})[member] = {
        "run_id": rid, "sha256": _sha(data), "dest": dest, "assignment": rec.get("contract", {}).get("name"),
    }
    return PASSED, dest


def consider(goal_id: str) -> dict:
    import goals
    doc = goals.state(goal_id)
    if not enabled(goal_id):
        return {"reports": [], "enabled": False}
    meta = _load_meta(goal_id)
    reports = []
    for gid, g in meta["groups"].items():
        reports.append(_consider_group(goal_id, doc, meta, gid, g))
    _save_meta(goal_id, meta)
    _ledger_sync(goal_id, meta)
    return {"reports": reports, "enabled": True}


def _consider_group(goal_id, doc, meta, gid, g) -> dict:
    members = g.get("members") or []
    missing = []
    resolved = []
    for m in members:
        name, rec, data, err = _member_bytes(doc, m)
        if data is None:
            missing.append(err or m)
        else:
            resolved.append((m, rec, data))
    if missing:
        if g.get("state") == FAILED and g.get("repairing"):
            return {"group": gid, "state": FAILED, "reason": g.get("reason"),
                    "live_intact": True, "repairing": True}
        g["state"] = WAITING
        g["reason"] = "; ".join(missing)
        g["live_intact"] = True
        return {"group": gid, "state": WAITING, "reason": g["reason"], "live_intact": True}

    # Failed group's candidate stays with that group; another group must not replace it.
    if g.get("state") == FAILED and candidate_dir(goal_id, gid).is_dir():
        same = True
        for m, rec, data in resolved:
            prev = (g.get("integrated") or {}).get(m) or {}
            if prev.get("run_id") != rec.get("run_id") or prev.get("sha256") != _sha(data):
                same = False
                break
        if same:
            return {"group": gid, "state": FAILED, "reason": g.get("reason"),
                    "live_intact": True, "dests": _group_dests(doc, members)}

    # Idempotent: all current accepted versions already integrated and group passed.
    if g.get("state") == PASSED:
        same = True
        for m, rec, data in resolved:
            prev = (g.get("integrated") or {}).get(m) or {}
            if prev.get("run_id") != rec.get("run_id") or prev.get("sha256") != _sha(data):
                same = False
                break
        if same:
            return {"group": gid, "state": PASSED, "reason": "already integrated",
                    "live_intact": True}

    cand = candidate_dir(goal_id, gid)
    _copytree(live_dir(goal_id), cand)
    applied = []
    for m, rec, data in resolved:
        st, why = _apply_member(goal_id, meta, gid, m, rec, data, cand)
        if st != PASSED:
            g["state"] = st
            g["reason"] = why
            g["live_intact"] = True
            g["repairing"] = False
            return {"group": gid, "state": st, "reason": why, "live_intact": True,
                    "dests": _group_dests(doc, members)}
        applied.append(why)

    ok, err = _run_check(goal_id, cand)
    if not ok:
        g["state"] = FAILED
        g["reason"] = err
        g["live_intact"] = True
        g["repairing"] = False
        return {"group": gid, "state": FAILED, "reason": err, "live_intact": True,
                "dests": _group_dests(doc, members)}
    _promote(goal_id, gid, cand)
    g["state"] = PASSED
    g["reason"] = ""
    g["live_intact"] = True
    g["repairing"] = False
    return {"group": gid, "state": PASSED, "reason": "", "live_intact": True,
            "promoted": True}


def _group_dests(doc, members):
    dests = []
    for m in members:
        rec = (doc.get("assignments") or {}).get(m) or {}
        d = (rec.get("contract") or {}).get("dest")
        if d:
            dests.append(d)
    return dests


def _promote(goal_id: str, gid: str, cand: Path) -> None:
    live = live_dir(goal_id)
    backup = _tree(goal_id) / "live.prev"
    if backup.exists():
        shutil.rmtree(backup)
    if live.exists():
        shutil.copytree(live, backup)
    tmp = _tree(goal_id) / "live.next"
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(cand, tmp)
    if live.exists():
        shutil.rmtree(live)
    tmp.rename(live)


def stage_repair_inputs(card: dict) -> Path:
    """Copy the failed group's candidate dests into the verified workspace before drain."""
    goal_id = card.get("goal_id")
    gid = card.get("group")
    rid = card.get("run_id") or card.get("name")
    ws = ROOT / "runs" / ("verified-" + rid)
    ws.mkdir(parents=True, exist_ok=True)
    cand = candidate_dir(goal_id, gid)
    dests = list(card.get("dests") or [])
    # A tools worker gets the candidate staged into its OWN tool-service workspace (integration_stage),
    # authors the artifact there, and both review and ingest read that authoritative copy. Planting a
    # second copy of the *broken* candidate here would only reintroduce a stale same-named file -- the
    # one that a non-productive round's review_root fallback made the skeptic read (the r3 judge split),
    # and that ingest historically re-applied. So stage the dests only for the chat/fenced path.
    if not card.get("tools"):
        for dest in dests:
            src = contained_path(cand, dest)
            dst = contained_path(ws, dest)
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    (ws / "integration-repair.json").write_text(
        json.dumps({"group": gid, "dests": dests, "reason": card.get("repair_reason") or ""}, indent=2),
        encoding="utf-8",
    )
    return ws


def _relativize(text: str, goal_id: str) -> str:
    """Strip this goal's absolute integrate-tree paths out of a check traceback, so a repair
    brief never hands the worker an absolute path to (mistakenly) write to."""
    if not text:
        return text
    base = str(_tree(goal_id))
    return text.replace(base + os.sep, "<project>/").replace(base + "/", "<project>/").replace(base, "<project>")


def repair_contract(goal_id: str, group_id: str, attempt: int = 1) -> dict:
    import goals
    doc = goals.state(goal_id)
    meta = _load_meta(goal_id)
    g = meta["groups"][group_id]
    members = g["members"]
    dests = _group_dests(doc, members)
    cid = None
    for m in members:
        rec = (doc.get("assignments") or {}).get(m)
        if rec:
            cid = (rec.get("contract") or {}).get("criterion_id")
            break
    name = "integrate-{0}-r{1}".format(group_id, attempt)
    cand = candidate_dir(goal_id, group_id)
    reason = _relativize(g.get("reason") or "", goal_id)
    # The whole assembled candidate project is staged into the worker's workspace at its RELATIVE
    # paths (preserving the game/ package), so a tools worker can read the current file, import the
    # package to self-check with python_run, and rewrite only the authorised dest.
    stage = []
    if cand.is_dir():
        for f in sorted(cand.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(cand).as_posix()
            if (f.name == "integration-check.py" or "__pycache__" in rel
                    or f.suffix in {".pyc", ".pyo"} or f.name.endswith(".writing")):
                continue                 # never stage the check, compiled bytecode, or temp files
            stage.append({"dest": rel, "location": str(f)})
    art = dests[0] if dests else "output.md"
    check_src = ""
    chk = _check_path(goal_id)
    if chk.is_file():
        check_src = chk.read_text(encoding="utf-8")
    oracle = worker_oracle(check_src)
    asserts = assertion_lines(check_src) or ["the frozen integration check passes"]
    brief = (
        "INTEGRATION REPAIR for group {0}. The assembled project's frozen check FAILED:\n{1}\n\n"
        "The assembled project is already in your workspace (the game/ package, including the "
        "current broken file). READ it, then FIX it so the check passes.\n"
        "Write ONLY these authorised destinations, each as a RELATIVE path inside your workspace: "
        "{2}.\n"
        "Do NOT write to any absolute path or outside your workspace -- paths shown in the traceback "
        "are diagnostic only; the tool service refuses writes that escape the workspace."
    ).format(group_id, reason, ", ".join(dests))
    return {
        "name": name,
        "criterion_id": cid or "c1",
        "capability": "repair integrated " + group_id,
        "brief": brief,
        "done_when": asserts,
        "oracle": oracle,
        "oracle_covers_done_when": True,
        "artifact": art,
        "dest": dests[0] if dests else "",
        "dests": dests,
        "base_sha256": "",
        "needs": list(members),
        "lineage": list(members),
        "tools": True,
        "integration_stage": stage,
        "evidence": "frozen integration check passes after authorized dest writes",
        "decides_alone": [name],
        "integrator": "handoff",
        "origin": "integration-repair",
        "group": group_id,
        "repair_reason": reason,
    }


def queue_repairs(goal_id: str) -> list:
    """Ensure a READY tools assignment exists for each failed group. Orchestrate dispatches it."""
    import goals
    doc = goals.state(goal_id)
    meta = _load_meta(goal_id)
    created = []
    for gid, g in meta["groups"].items():
        if g.get("state") != FAILED:
            continue
        existing = [
            n for n, a in (doc.get("assignments") or {}).items()
            if (a.get("contract") or {}).get("origin") == "integration-repair"
            and (a.get("contract") or {}).get("group") == gid
            and a.get("status") in {"ready", "running"}
            and a.get("disposition") not in {"accepted"}
        ]
        if existing:
            g["repairing"] = True
            continue
        prior = sum(
            1 for a in (doc.get("assignments") or {}).values()
            if (a.get("contract") or {}).get("group") == gid
            and (a.get("contract") or {}).get("origin") == "integration-repair"
        )
        attempt = 1 + prior
        if attempt > MAX_REPAIR_ATTEMPTS:
            # Budget exhausted across all drains/restarts. Preserve the live checkpoint and stop;
            # do not open a fresh budget. Completion then reports a blocked, actionable outcome.
            g["state"] = EXHAUSTED
            g["repairing"] = False
            g["reason"] = ("integration repair budget exhausted after {0} attempts; the last good "
                           "checkpoint is preserved. Last check failure:\n{1}").format(
                MAX_REPAIR_ATTEMPTS, _relativize(g.get("reason") or "", goal_id))
            _save_meta(goal_id, meta)
            _ledger_sync(goal_id, meta)
            continue
        con = repair_contract(goal_id, gid, attempt)
        goals.assign(goal_id, [con])
        g["repairing"] = True
        created.append(con["name"])
    _save_meta(goal_id, meta)
    _ledger_sync(goal_id, meta)
    return created


def _tool_workspace(name: str):
    """The tool-service workspace dir for a job name, or None if the tool dep is unavailable.
    Mirrors verified.tool_workspace_id (sanitise to [A-Za-z0-9_-])."""
    try:
        import tooljob, verified
        # Use verified.tool_workspace_id VERBATIM so the id can never drift from the one the tool
        # job was actually dispatched under. It prefixes "verified-" before sanitising; deriving the
        # id here without that prefix pointed ingest at an empty sibling dir and hid the accepted
        # artifact (the r3 recovery: the real job was "verified-integrate-score-r3-...").
        return tooljob.workspace_dir(verified.tool_workspace_id(name))
    except Exception:
        try:
            import tooljob
            wid = "".join(c if c in _WS_SAFE else "-" for c in ("verified-" + str(name)))
            return tooljob.workspace_dir(wid)
        except Exception:
            return None


def _bound_artifact_sha(rid: str, runs_root=None):
    """The SHA the acceptance receipt BOUND the artifact to -- authoritative accepted-artifact
    identity, not a filename. From runs/verified-<rid>/receipt.json. Supports the real verified
    receipt ("evidence bound to artifact <hex>" / accepted_sha256) and the simple {sha256} form.
    None means the receipt records no binding -- then ingestion must refuse, not guess."""
    base = Path(runs_root) if runs_root else (ROOT / "runs")
    try:
        rec = json.loads((base / ("verified-" + rid) / "receipt.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if rec.get("accepted_sha256"):
        return rec["accepted_sha256"]
    fin = rec.get("final") or {}
    m = re.search(r"bound to artifact ([0-9a-f]{8,64})", fin.get("basis") or "")
    if m:
        return m.group(1)
    if rec.get("sha256"):
        return rec["sha256"]
    return None


def _resolve_accepted(rid: str, names, runs_root=None):
    """(matched_name, data, sha) for the accepted artifact bound under run `rid`, located by the
    receipt-bound hash among `names` (each authorised relative dest/artifact, also tried by
    basename) across the verified AND tool workspaces. THE single accepted-artifact identity, shared
    by candidate assembly and integration repair. Content identity decides -- not filename, not
    directory search order -- so a stale same-named copy simply never matches. Raises IntegrateError
    with an explicit reason when the receipt binds no hash, or when no available copy matches; it
    never returns an unbound substitute or a staged input."""
    bound = _bound_artifact_sha(rid, runs_root=runs_root)
    if not bound:
        raise IntegrateError(
            "run {0} receipt has no bound artifact hash; a PASS-only receipt is not "
            "accepted-artifact evidence".format(rid))
    ws = (Path(runs_root) if runs_root else (ROOT / "runs")) / ("verified-" + rid)
    tool_ws = _tool_workspace(rid)
    roots = [ws] + ([Path(tool_ws)] if tool_ws else [])
    seen = {}
    for n in names:
        if not n:
            continue
        for rel in dict.fromkeys([n, Path(n).name]):
            for root in roots:
                c = Path(root) / rel
                if not c.is_file():
                    continue
                data = c.read_bytes()
                h = _sha(data)
                seen[str(c)] = h[:12]
                if h == bound or (len(bound) >= 8 and h.startswith(bound)):
                    return n, data, h
    raise IntegrateError(
        "accepted artifact (bound hash {0}) for {1} is not present in the verified or tool "
        "workspace; refusing an unbound substitute or a staged input. Saw {2}".format(
            bound, list(names), seen))


def _identity_problem(cand: Path, dest: str, data: bytes, bound: str) -> str:
    """Empty when the candidate file is exactly the receipt-bound bytes. Otherwise a harness
    mismatch: do not promote, and do not describe it as the worker failing the check."""
    try:
        target = contained_path(cand, dest)
    except IntegrateError as e:
        return "harness identity mismatch: {0}. This is not a worker failure.".format(e)
    if not target.is_file():
        return ("harness identity mismatch: {0} is missing on the candidate after apply; "
                "refusing to promote. This is not a worker failure.".format(dest))
    got = _sha_file(target)
    want = _sha(data)
    if got != want:
        return ("harness identity mismatch: candidate {0} sha {1} != accepted bytes {2}; "
                "refusing to promote. This is not a worker failure.".format(dest, got[:12], want[:12]))
    if bound and not (got == bound or (len(bound) >= 8 and got.startswith(bound))):
        return ("harness identity mismatch: candidate {0} sha {1} != receipt binding {2}; "
                "refusing to promote. This is not a worker failure.".format(dest, got[:12], bound[:12]))
    return ""


def ingest_repair(goal_id: str, assignment: str, run_id: str | None = None) -> dict:
    """Apply worker-produced dest files from the verified workspace onto the group's candidate."""
    import goals
    doc = goals.state(goal_id)
    rec = (doc.get("assignments") or {}).get(assignment) or {}
    con = rec.get("contract") or {}
    gid = con.get("group")
    if not gid:
        raise IntegrateError("repair assignment {0} names no group".format(assignment))
    meta = _load_meta(goal_id)
    cand = candidate_dir(goal_id, gid)
    if not cand.is_dir():
        _copytree(live_dir(goal_id), cand)
    rid = run_id or rec.get("run_id") or assignment
    ws = ROOT / "runs" / ("verified-" + rid)
    dests = list(con.get("dests") or ([con.get("dest")] if con.get("dest") else []))
    applied = []
    stop_reason = None

    # Resolve THE accepted artifact by the receipt-bound hash (the shared identity), then bind and
    # apply EXACTLY that one destination. A repair is a one-destination accepted assignment: it does
    # NOT import additional, unbound changes from the tool workspace. Every other authorised dest
    # already holds its own accepted member bytes in the candidate from assembly; re-importing an
    # unbound same-named file is exactly the substitution this mechanism refuses.
    try:
        accepted_dest, accepted_data, _h = _resolve_accepted(rid, list(dests) + [con.get("artifact")])
    except IntegrateError as e:
        stop_reason = str(e)
    else:
        dst = contained_path(cand, accepted_dest)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(accepted_data)
        applied.append(accepted_dest)
        bound = _bound_artifact_sha(rid) or ""
        ident = _identity_problem(cand, accepted_dest, accepted_data, bound)
        (meta.setdefault("owners", {}))[accepted_dest] = {
            "producer": assignment, "run_id": rid, "sha256": _sha(accepted_data),
        }
        if ident:
            stop_reason = ident
    if stop_reason is not None:
        # An explicit, honest stop: the accepted artifact could not be resolved by its bound hash.
        # Do NOT fall back and do NOT mutate the candidate beyond any secondary dests already applied.
        g = meta["groups"][gid]
        g["state"] = FAILED
        g["reason"] = stop_reason
        g["live_intact"] = True
        g["repairing"] = False
        _save_meta(goal_id, meta)
        _ledger_sync(goal_id, meta)
        return {"state": FAILED, "reason": stop_reason, "live_intact": True, "applied": applied}
    ok, err = _run_check(goal_id, cand)
    g = meta["groups"][gid]
    if not ok:
        applied_sha = _sha(accepted_data)
        g["state"] = FAILED
        g["reason"] = (
            "accepted artifact {0} sha {1} matches the receipt and was applied to the candidate, "
            "so this is not an identity mismatch. The frozen integration check then failed. "
            "Matching hashes do not prove the worker or the specification is at fault; "
            "provisioning, sibling files, and the check environment remain possible causes:\n{2}"
        ).format(accepted_dest, applied_sha[:12], err)
        g["live_intact"] = True
        g["repairing"] = False
        _save_meta(goal_id, meta)
        _ledger_sync(goal_id, meta)
        return {"state": FAILED, "reason": g["reason"], "live_intact": True, "applied": applied,
                "attribution": "check-failed-on-accepted-bytes", "bound_sha256": applied_sha}
    ident = _identity_problem(cand, accepted_dest, accepted_data, _bound_artifact_sha(rid) or "")
    if ident:
        g["state"] = FAILED
        g["reason"] = ident
        g["live_intact"] = True
        g["repairing"] = False
        _save_meta(goal_id, meta)
        _ledger_sync(goal_id, meta)
        return {"state": FAILED, "reason": ident, "live_intact": True, "applied": applied,
                "attribution": "harness"}
    _promote(goal_id, gid, cand)
    g["state"] = PASSED
    g["reason"] = ""
    g["live_intact"] = True
    g["repairing"] = False
    _save_meta(goal_id, meta)
    _ledger_sync(goal_id, meta)
    return {"state": PASSED, "reason": "", "live_intact": True, "applied": applied}


def execute_repair(goal_id, group_id, runner, *, workspace=None) -> dict:
    """Test/helper path: stage candidate, run a callable, ingest dest writes, recheck."""
    meta = _load_meta(goal_id)
    cand = candidate_dir(goal_id, group_id)
    if not cand.is_dir():
        _copytree(live_dir(goal_id), cand)
    ws = Path(workspace) if workspace else Path(tempfile.mkdtemp(prefix="integrate-repair-"))
    ws.mkdir(parents=True, exist_ok=True)
    dests = []
    import goals
    doc = goals.state(goal_id)
    dests = _group_dests(doc, meta["groups"][group_id]["members"])
    for dest in dests:
        src = contained_path(cand, dest)
        dst = contained_path(ws, dest)
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    runner(ws, dests, meta["groups"][group_id].get("reason") or "")
    # copy authorized dests back
    for dest in dests:
        src = contained_path(ws, dest)
        dst = contained_path(cand, dest)
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    ok, err = _run_check(goal_id, cand)
    g = meta["groups"][group_id]
    if not ok:
        g["state"] = FAILED
        g["reason"] = err
        g["live_intact"] = True
        _save_meta(goal_id, meta)
        _ledger_sync(goal_id, meta)
        return {"state": FAILED, "reason": err, "live_intact": True}
    _promote(goal_id, group_id, cand)
    g["state"] = PASSED
    g["reason"] = ""
    g["live_intact"] = True
    g["repairing"] = False
    _save_meta(goal_id, meta)
    _ledger_sync(goal_id, meta)
    return {"state": PASSED, "live_intact": True}


def completion_state(goal_id: str, doc=None, root=None) -> tuple:
    """Authoritative gate: declared integration must be readable and every group PASSED."""
    try:
        import goals
        doc = doc or goals.state(goal_id, root=root)
    except Exception as e:
        declared = False
        try:
            declared = bool((doc or {}).get("integration", {}).get("declared"))
        except Exception:
            declared = _meta_path(goal_id).is_file()
        if declared:
            return False, "cannot load goal ledger for integration: {0}".format(e)
        return True, ""
    declared = bool((doc.get("integration") or {}).get("declared"))
    if not declared and not _meta_path(goal_id).is_file():
        return True, ""
    if declared or _meta_path(goal_id).is_file():
        try:
            meta = _load_meta(goal_id)
        except IntegrateError as e:
            return False, str(e)
        groups = meta.get("groups") or {}
        if not groups:
            return False, "integration declared but no groups recorded; recover by re-approving"
        for gid, g in groups.items():
            st = g.get("state")
            if st != PASSED:
                return False, "integration group {0} is {1}: {2}".format(
                    gid, st, g.get("reason") or "not passed")
        return True, ""
    return True, ""


def dest_problems(dest: str) -> list:
    err = dest_is_forbidden(dest)
    return [("BAD_DEST", err + ": " + dest)] if err else []
