"""First-use project package. One directory a fresh process can load.

Default location when --package is omitted: intake/packages/<name>/ under the fleet root.
The user's verbatim answers, agent drafts, folder facts, and the approved North Star stay in
separate files. E3 is review cadence only. It does not choose how many workers run.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import intake

ROOT = Path(__file__).resolve().parent.parent
CONSEQUENTIAL = ("Q0", "S4", "E2", "E4", "V1")


def default_package(name: str) -> Path:
    return ROOT / "intake" / "packages" / name


def _read_json(path: Path, default):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_project(package: Path) -> dict:
    doc = _read_json(Path(package) / "project.json", None)
    if not isinstance(doc, dict):
        raise SystemExit("package has no project.json: {0}".format(package))
    return doc


def save_project(package: Path, doc: dict) -> None:
    _write_json(Path(package) / "project.json", doc)


def ensure(package: Path, folder: Path, name: str) -> dict:
    """Create the package. Do not overwrite an existing project_root or observed.md."""
    package = Path(package)
    folder = Path(folder).resolve()
    package.mkdir(parents=True, exist_ok=True)
    path = package / "project.json"
    if path.is_file():
        doc = load_project(package)
        if Path(doc.get("project_root") or "").resolve() != folder:
            raise SystemExit("package {0} is already bound to {1}".format(package, doc.get("project_root")))
        doc.setdefault("name", name)
        doc.setdefault("package", str(package.resolve()))
        save_project(package, doc)
        return doc
    doc = {
        "name": name,
        "project_root": str(folder),
        "package": str(package.resolve()),
        "review_cadence": None,
        "goal_id": None,
    }
    save_project(package, doc)
    observed = package / "observed.md"
    if not observed.exists():
        observed.write_bytes(observe(folder, skip=package).encode("utf-8"))
    return doc


def set_first_use(package: Path, *, project_kind: str = "", intake_mode: str = "") -> dict:
    """Record explicit setup choices separately from the project path; never infer Git intent."""
    doc = load_project(package)
    if project_kind:
        previous = doc.get("project_kind")
        if previous and previous != project_kind:
            raise SystemExit("project kind is already {0}; refusing to change it silently".format(previous))
        doc["project_kind"] = project_kind
    if intake_mode:
        previous = doc.get("intake_mode")
        if previous and previous != intake_mode and (package / "scoped.md").exists():
            raise SystemExit("scope already exists; refusing to change the intake path")
        doc["intake_mode"] = intake_mode
    save_project(package, doc)
    return doc


def validated_brief(source: Path) -> bytes:
    """Read-only validation, so a bad brief cannot leave a newly created package behind."""
    source = Path(source).resolve()
    if not source.is_file():
        raise SystemExit("brief is not a file: {0}".format(source))
    raw = source.read_bytes()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SystemExit("brief must be UTF-8 text")
    if not content.strip():
        raise SystemExit("brief is empty")
    return raw


def import_brief(package: Path, source: Path) -> Path:
    """Keep a supplied brief as context, never as confirmed intake answers or approved scope."""
    source = Path(source).resolve()
    raw = validated_brief(source)
    target = Path(package) / "brief.md"
    if target.exists() and target.read_bytes() != raw:
        raise SystemExit("package already has a different brief.md; refusing to overwrite it")
    if not target.exists():
        target.write_bytes(raw)
    doc = load_project(package)
    doc["brief_source"] = str(source)
    doc["brief_sha256"] = hashlib.sha256(raw).hexdigest()
    save_project(package, doc)
    return target


def observe(folder: Path, skip: Path = None) -> str:
    """Factual listing. No model, no summary of intent."""
    folder = Path(folder).resolve()
    skip_res = Path(skip).resolve() if skip else None
    ignored = {".git", "__pycache__", "node_modules", ".handoff", ".pytest_cache"}
    files = []
    capped = False
    for dirpath, dirnames, filenames in os.walk(folder):
        if skip_res and Path(dirpath).resolve() == skip_res:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in ignored and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".pyc"):
                continue
            rel = Path(dirpath, fn).resolve().relative_to(folder).as_posix()
            files.append(rel)
            if len(files) >= 200:
                capped = True
                break
        if capped:
            break
    files.sort()
    tests = (folder / "tests").is_dir() or (folder / "pytest.ini").is_file() or (folder / "pyproject.toml").is_file()
    lines = ["# Observed", "", "root: {0}".format(folder), "tests_present: {0}".format("yes" if tests else "no"), "", "files:"]
    lines += ["- {0}".format(rel) for rel in files] or ["- (empty)"]
    if capped:
        lines += ["", "truncated: listing stopped at 200 files; more may remain."]
    lines += ["", "These lines are observed facts. They are not the user's answers and not a proposal."]
    return "\n".join(lines) + "\n"


def _state_dir(package: Path) -> Path:
    return Path(package)


def intake_state(package: Path, name: str):
    return intake.load(name, directory=_state_dir(package))


def proposed_path(package: Path) -> Path:
    return Path(package) / "proposed.json"


def load_proposed(package: Path) -> dict:
    doc = _read_json(proposed_path(package), {"drafts": {}})
    doc.setdefault("drafts", {})
    return doc


def save_proposed(package: Path, doc: dict) -> None:
    _write_json(proposed_path(package), doc)


def draft_text(package: Path, qid: str) -> str:
    item = (load_proposed(package).get("drafts") or {}).get(qid) or {}
    if isinstance(item, str):
        return item
    return item.get("text") or ""


def _human_only(text: str) -> bool:
    letters = intake._letters(text or "")
    return letters == {"E"} or "human only" in (text or "").lower() or "only a human" in (text or "").lower()


def consequential_open(package: Path, name: str) -> list:
    """Unanswered consequential questions that this branch is asking now.

    V1 is consequential only when its draft says a human is the only verifier.
    A draft is never an answer."""
    st = intake_state(package, name) or {"answers": {}}
    answers = st.get("answers") or {}
    required = intake.required_ids(st) if st.get("answers") is not None and intake.branch(st) else ["Q0"]
    open_ids = []
    for qid in CONSEQUENTIAL:
        if qid not in required:
            continue
        if (answers.get(qid) or "").strip():
            continue
        if qid == "V1" and not _human_only(draft_text(package, qid)):
            continue
        open_ids.append(qid)
    return open_ids


def assist(package: Path, name: str, drafts: dict) -> None:
    """Record agent drafts. Never writes the answer store."""
    doc = load_proposed(package)
    for qid, text in (drafts or {}).items():
        if qid not in intake.QBYID:
            continue
        doc["drafts"][qid] = {"text": text, "source": "proposed"}
    save_proposed(package, doc)


def confirm(package: Path, name: str, qid: str, by: str) -> None:
    text = draft_text(package, qid)
    if not text.strip():
        raise SystemExit("no proposed draft for {0}".format(qid))
    if not (by or "").strip():
        raise SystemExit("confirm needs --as; an anonymous confirmation is not one")
    intake.answer(name, qid, text, directory=_state_dir(package), source="confirmed", by=by)
    doc = load_proposed(package)
    doc["drafts"].pop(qid, None)
    save_proposed(package, doc)


def render_intake(package: Path, name: str) -> str:
    st = intake_state(package, name)
    if not st or not intake.is_done(st):
        raise SystemExit("intake is not complete; refused to render")
    return intake.render(name, directory=_state_dir(package))


def record_review_cadence(package: Path, name: str) -> None:
    """E3 says when the user wants to see a draft. It is not a worker count."""
    st = intake_state(package, name) or {}
    e3 = ((st.get("answers") or {}).get("E3") or "").strip()
    if not e3:
        return
    doc = load_project(package)
    doc["review_cadence"] = e3
    save_project(package, doc)


def scope_prompt(package: Path, name: str) -> str:
    observed = (Path(package) / "observed.md").read_bytes().decode("utf-8")
    rendered = render_intake(package, name)
    return (
        intake.REFERENCE_CARD
        + "\n\nLABELING. Mark user statements as quotes from the intake. Mark folder facts as "
        "observed. Mark anything you add as proposed or as an assumption. Do not present a "
        "proposed sentence as something the user said.\n\n"
        "# Observed folder\n" + observed + "\n# Intake\n" + rendered
    )


def write_scope(package: Path, text: str) -> None:
    path = Path(package) / "scoped.md"
    if path.exists():
        raise SystemExit("scoped.md already exists; refusing to overwrite. Edit it or delete it yourself.")
    path.write_bytes(text.encode("utf-8"))


def approval_matches(package: Path) -> bool:
    scope = Path(package) / "scoped.md"
    approval = Path(package) / "approval.json"
    if not scope.is_file() or not approval.is_file():
        return False
    doc = _read_json(approval, {})
    return doc.get("sha256") == sha256_file(scope) and bool((doc.get("by") or "").strip())


def approve_scope(package: Path, by: str, note: str = "") -> dict:
    scope = Path(package) / "scoped.md"
    if not scope.is_file():
        raise SystemExit("no scoped.md to approve")
    if not (by or "").strip():
        raise SystemExit("approve-scope needs --as")
    doc = {"sha256": sha256_file(scope), "by": by, "note": note or ""}
    _write_json(Path(package) / "approval.json", doc)
    return doc


def criteria_from_intake(st) -> list:
    """The user's done-when lines, plus the pass and fail examples when a machine can check them.

    These are the goal criteria. A generic sentence about the North Star is not a criterion."""
    answers = (st or {}).get("answers") or {}
    raw = answers.get("S2") or ""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines and raw.strip():
        lines = [raw.strip()]
    if not lines:
        raise SystemExit("intake has no done-when answer (S2); refusing to invent a criterion")
    human = intake._v1_human_only(st or {"answers": {}})
    out = [{"id": "done-{0}".format(i), "text": line, "human_only": human}
           for i, line in enumerate(lines, 1)]
    if not human:
        v2 = (answers.get("V2") or "").strip()
        v3 = (answers.get("V3") or "").strip()
        if v2:
            out.append({"id": "pass-example", "text": v2, "human_only": False})
        if v3:
            out.append({"id": "fail-example", "text": v3, "human_only": False})
    return out


def limits_from_intake(st) -> dict:
    answers = (st or {}).get("answers") or {}
    return {"never": (answers.get("S4") or "").strip(), "rules": (answers.get("E4") or "").strip()}


_NEVER_ACT = (
    ("delete", re.compile(
        r"\b(os\.remove|os\.unlink|shutil\.rmtree|rmtree\(|\.unlink\(|\bdelete\s+(?:the|a|all|files|file)\b)",
        re.I)),
    ("send", re.compile(r"\b(smtplib|sendmail|requests\.post|urllib\.request|http\.client)\b", re.I)),
    ("spend", re.compile(r"\b(stripe|payment|charge the|spend money)\b", re.I)),
)


def limit_hits(contract, limits) -> str:
    """A confirmed never-rule matched something this assignment would do. Empty means no hit."""
    blob = ((limits or {}).get("never") or "") + "\n" + ((limits or {}).get("rules") or "")
    if not blob.strip():
        return ""
    text = " ".join([
        str(contract.get("brief") or ""),
        str(contract.get("oracle") or ""),
        " ".join(contract.get("done_when") or []),
        str(contract.get("capability") or ""),
        str(contract.get("dest") or ""),
        " ".join(str(s) for s in (contract.get("sources") or [])),
    ])
    low = blob.lower()
    for verb, rx in _NEVER_ACT:
        if re.search(r"\b" + verb + r"\b", low) and rx.search(text):
            return "the confirmed never-rule forbids {0}".format(verb)
    for tok in re.findall(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+", blob):
        if tok.lower() in text.lower():
            return "the confirmed never-rule names {0}".format(tok)
    return ""


def cadence_letter(text: str) -> str:
    picked = intake._letters(text or "")
    if len(picked) == 1:
        letter = next(iter(picked))
        if letter in "ABCD":
            return letter
    head = (text or "").lstrip()[:1].upper()
    return head if head in "ABCD" else ""


def effective_cadence(letter: str, branch: str = "") -> str:
    """E3 alone chooses when the user sees a draft, honored within the approved budget.

    The letter is not a worker count. `branch` (Q0 domain) is NOT an autonomy cap: a non-coder
    (branch B) needs stronger outcome evidence and explanation, not more forced code-review stops,
    so it does not downgrade the selected cadence (issue #2). The required scope/plan/map approvals
    and consequential-action stops apply regardless of this letter."""
    letter = (letter or "").strip().upper()[:1]
    if letter not in "ABCD":
        return ""
    return letter


def _executable_asserts(example: str) -> list:
    out = []
    for ln in (example or "").splitlines():
        s = ln.strip()
        if not s.startswith("assert "):
            continue
        flat = re.sub(r"\s+", " ", s).lower().rstrip(";")
        if flat in {"assert true", "assert true == true", "assert 1", "assert 1 == 1", "assert 1==1"}:
            continue
        out.append(s)
    return out


def integration_check(dests, pass_example, fail_example) -> str:
    """Frozen project check. Prose examples refuse to pass. assert True is not a check."""
    dests = [str(d).replace("\\", "/") for d in dests if str(d).strip()]
    lines = [
        "import os, sys, pathlib",
        "root = os.environ.get('INTEGRATION_ROOT') or os.getcwd()",
        "sys.path.insert(0, root)",
        "os.chdir(root)",
        "dests = {0}".format(json.dumps(dests)),
        "missing = [d for d in dests if not (pathlib.Path(root) / d).is_file()]",
        "assert not missing, 'deliverable not in the project: %s' % missing",
        "PASS_EXAMPLE = {0}".format(json.dumps(pass_example or "")),
        "FAIL_EXAMPLE = {0}".format(json.dumps(fail_example or "")),
    ]
    for dest in dests:
        if dest.endswith(".py"):
            lines.append("import {0}".format(dest[:-3].replace("/", ".")))
    passed = _executable_asserts(pass_example)
    failed = _executable_asserts(fail_example)
    if not passed:
        lines.append("# pass example is prose; a person supplies an assert or signs off. This check does not fail for that.")
    else:
        lines.extend(passed)
    if (fail_example or "").strip() and not failed:
        lines.append("# fail example is prose; a person supplies an assert or signs off. This check does not fail for that.")
    for fa in failed:
        lines += [
            "try:",
            "    " + fa,
            "    raise SystemExit('fail example was accepted as success: ' + FAIL_EXAMPLE)",
            "except AssertionError:",
            "    pass",
        ]
    return "\n".join(lines) + "\n"


def prose_gaps(pass_example, fail_example) -> list:
    """Confirmed examples that are not assert lines. They become a question, not a failing check."""
    gaps = []
    if (pass_example or "").strip() and not _executable_asserts(pass_example):
        gaps.append("pass example is prose; supply an assert line or sign off: " + pass_example.strip())
    if (fail_example or "").strip() and not _executable_asserts(fail_example):
        gaps.append("fail example is prose; supply an assert line or sign off: " + fail_example.strip())
    return gaps


def scan_emitted(source, limits) -> str:
    """Never-rules applied to code a worker actually wrote, not only to the assignment text."""
    return limit_hits({"brief": "", "oracle": source or "", "done_when": [], "capability": "",
                       "dest": "", "sources": []}, limits)


def integration_spec(project_root, contracts, pass_example, fail_example):
    landing, dests = [], []
    for con in contracts or []:
        dest = (con.get("dest") or "").strip().replace("\\", "/")
        if not dest or not con.get("name"):
            continue
        landing.append(con["name"])
        if dest not in dests:
            dests.append(dest)
    if not landing:
        return None
    return {
        "project_root": str(project_root),
        "groups": [{"id": "project", "assignments": landing}],
        "check_source": integration_check(dests, pass_example, fail_example),
    }


def review_first_use(contract, criteria, limits) -> list:
    """Problems that would let the fleet accept the wrong result or skip the project.

    BINDING (2026-09-23). An outcome is bound to a criterion STRUCTURALLY -- it must name a real goal
    criterion by id -- and the criterion's exact source text is preserved as structured planning
    evidence on the outcome (`criterion_source`, attached by goals.propose), not by requiring the
    planner to paste the criterion's prose into its brief or oracle. The MEANINGFUL check on the
    contract is that its oracle is not vacuous and (for a done-when outcome) it lands at a real project
    dest -- an outcome can advance a criterion without quoting it, but it cannot with a fake check or
    nowhere to land."""
    by_id = {c["id"]: c for c in criteria}
    cid = contract.get("criterion_id") or ""
    crit = by_id.get(cid)
    if not crit:
        # Structural binding failure: the outcome names no real criterion -- adjacent activity.
        return [("UNBOUND", "criterion_id {0!r} is not one of the goal's criteria".format(cid))]
    problems = []
    oracle = contract.get("oracle") or ""
    if not crit.get("human_only"):
        import integrate
        if integrate.vacuous_oracle(oracle):
            problems.append(("VACUOUS", "a vacuous oracle cannot establish {0}".format(crit["id"])))
    if str(crit["id"]).startswith("done-") and not crit.get("human_only"):
        if (contract.get("integrator") or "").strip().lower() != "handoff":
            problems.append(("NO_DEST", "set integrator to \"handoff\" so this done-when outcome lands "
                             "in the project (it is currently {0!r})".format(
                                 (contract.get("integrator") or "").strip() or "unset")))
        dest = (contract.get("dest") or "").strip()
        if not dest:
            problems.append(("NO_DEST", "set dest (and artifact) to the package-relative path where "
                             "this file lands, e.g. \"world.py\""))
        elif str(contract.get("artifact") or "").replace("\\", "/") != dest.replace("\\", "/"):
            problems.append(("ARTIFACT_DEST", "artifact must equal dest {0}".format(dest)))
    # STDLIB-ONLY: when the scope forbids non-stdlib dependencies, an outcome (especially a test
    # outcome) must not pull in a third-party package such as pytest -- use the stdlib (unittest).
    # Keyword-based limit_hits cannot connect "non-stdlib deps" to a specific package, so check here.
    never = (((limits or {}).get("never") or "") + " " + ((limits or {}).get("rules") or "")).lower()
    if any(k in never for k in ("standard library", "stdlib", "non-stdlib", "no third-party", "no external")):
        blob = " ".join([str(contract.get("oracle") or ""), str(contract.get("brief") or ""),
                         " ".join(contract.get("done_when") or [])])
        # INVOCATION, not mention: a done_when that says "use unittest, not pytest" names pytest to
        # forbid it -- that must not be rejected. Flag only when the package is actually pulled in or
        # run: `import pkg`, `from pkg import`, `python -m pkg`, or `pkg.<attr>`.
        for pkg in ("pytest", "numpy", "pandas", "requests", "nose", "unittest2", "hypothesis"):
            p = re.escape(pkg)
            used = re.search(
                r"import\s+{0}\b|from\s+{0}\b|-m['\"\s,]*{0}\b|{0}\.[A-Za-z_]|['\"]{0}['\"]".format(p),
                blob)
            if used:
                problems.append(("STDLIB_ONLY", "invokes the third-party package {0!r} but the project is "
                                 "standard-library only; use the stdlib (e.g. run the suite with "
                                 "`python -m unittest`)".format(pkg)))
                break
    hit = limit_hits(contract, limits)
    if hit:
        problems.append(("NEVER", hit))
    return problems


def parse_drafts(text: str) -> dict:
    raw = text or ""
    a, b = raw.find("{"), raw.rfind("}")
    if a < 0 or b < 0:
        raise SystemExit("assist returned no draft JSON")
    doc = json.loads(raw[a:b + 1])
    if isinstance(doc, dict) and isinstance(doc.get("drafts"), dict):
        doc = doc["drafts"]
    if not isinstance(doc, dict):
        raise SystemExit("assist drafts must be a JSON object")
    out = {}
    for key, val in doc.items():
        if isinstance(val, dict):
            val = val.get("text") or ""
        if isinstance(val, str) and val.strip():
            out[str(key)] = val
    return out


def draft_prompt(package: Path, name: str) -> str:
    observed = (Path(package) / "observed.md").read_bytes().decode("utf-8")
    st = intake_state(package, name) or {"answers": {}}
    lines = [
        "You are drafting proposed answers to a fixed intake. You are not the user.",
        "Use the observed folder and any supplied brief as context. Do not invent facts.",
        "Return a JSON object whose keys are question ids and whose values are proposed answer strings.",
        "A proposal is not something the user said. Do not confirm it.",
        "Branching is fixed: Q0 A requires S6a and not S6b. Q0 B requires S6b and not S6a.",
        "If V1 is only E, leave V2 and V3 unanswered.",
        "",
        observed,
        "",
    ]
    brief = Path(package) / "brief.md"
    if brief.is_file():
        lines += ["", "User-supplied existing brief (context, NOT confirmed answers):",
                  brief.read_text(encoding="utf-8")]
    lines += ["", "Questions:"]
    for qid, _layer, prompt, opts, branches in intake.QUESTIONS:
        lines.append("{0} branches={1}".format(qid, ",".join(sorted(branches))))
        lines.append(prompt)
        if opts:
            lines.append("options: " + "; ".join("{0}={1}".format(k, v) for k, v in opts.items()))
        answered = ((st.get("answers") or {}).get(qid) or "").strip()
        if answered:
            lines.append("already answered by the user; do not redraft: " + answered)
        lines.append("")
    return "\n".join(lines)
