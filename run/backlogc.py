"""Compile a supplied Markdown project backlog into traceable, dependency-closed batches (#21).

The source is kept, not summarised: every item keeps its id, dependencies, deliverables and
acceptance lines, and every item is accounted for in the output -- compiled into exactly one batch,
or listed with the reason it could not be (never silently dropped, never given invented answers).

Item format (one per heading; anything between headings belongs to the item above):

    ## BL-7: Event store
    depends: BL-3, BL-5
    deliverable: engine/events.py
    acceptance:
    - append() then replay() returns the events in order
    - replay() of an empty store returns []

Batches are bounded (--max-items) and dependency-closed: an item's dependencies are in the same or
an earlier batch, and a batch's `after` names the earlier batches it depends on. Output, next to
the backlog unless --out is given:

  <name>.batches.json  lineage: item -> batch, each batch's items and criteria (id = item id,
                       text = title + acceptance), items not compiled and why
  <name>.status.json   per-item status (pending / blocked / completed), kept across restarts and
                       re-derived from an overnight state file with `status --overnight <state>`

Criteria use the item ids, so a batch's criteria can be added to its milestone goal and reach the
planner with those ids (#6); run the batches in order with run/overnight.py (#7).

    python run/backlogc.py compile <backlog.md> [--max-items 6] [--out DIR]
    python run/backlogc.py status <backlog.md> [--overnight <backlog.json.state.json>]
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

_HEAD = re.compile(r"^(#{2,6})\s+(.*?)\s*$")
_ID_TITLE = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]*)\s*[:–—-]\s+(.+)$")
_FIELD = re.compile(r"^(depends|deliverables?|acceptance)\s*:\s*(.*)$", re.I)
_BULLET = re.compile(r"^\s*[-*+]\s+(?:\[[ xX]\]\s+)?(.*\S)\s*$")
DEFAULT_MAX_ITEMS = 6


def parse(text):
    """(items, problems). items: [{id, title, depends, deliverables, acceptance, notes, line}].
    problems: [{line, id?, problem}] for everything that could not become an item."""
    items, problems = [], []
    cur, field = None, None
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        h = _HEAD.match(line)
        if h:
            cur, field = None, None
            m = _ID_TITLE.match(h.group(2))
            if not m:
                problems.append({"line": n, "problem": "heading without an item id ('## <ID>: <title>'): "
                                                       + h.group(2)[:80]})
                continue
            cur = {"id": m.group(1), "title": m.group(2).strip(), "depends": [], "deliverables": [],
                   "acceptance": [], "notes": [], "line": n}
            items.append(cur)
            continue
        if cur is None:
            continue
        f = _FIELD.match(line.strip())
        if f:
            key = f.group(1).lower()
            val = f.group(2).strip()
            if key == "depends":
                cur["depends"] += [d.strip() for d in re.split(r"[,\s]+", val) if d.strip()]
                field = None
            elif key.startswith("deliverable"):
                cur["deliverables"] += [d.strip() for d in val.split(",") if d.strip()]
                field = "deliverables"
            else:
                if val:
                    cur["acceptance"].append(val)
                field = "acceptance"
            continue
        b = _BULLET.match(line)
        if b and field in ("acceptance", "deliverables"):
            cur[field].append(b.group(1))
            continue
        if line.strip():
            field = None if not b else field
            cur["notes"].append(line.strip())
    return items, problems


def compile_batches(items, problems=(), *, max_items=DEFAULT_MAX_ITEMS):
    """{"batches": [...], "not_compiled": [...], "lineage": {item: batch}}; every item appears once."""
    problems = list(problems)
    not_compiled = []
    seen = {}
    for it in items:
        if it["id"] in seen:
            not_compiled.append({"id": it["id"], "reason": "duplicate id (first at line {0}, again at line {1})".format(
                seen[it["id"]], it["line"])})
            it["_dup"] = True
        else:
            seen[it["id"]] = it["line"]
    usable = {it["id"]: it for it in items if not it.get("_dup")}
    bad = {}
    for it in usable.values():
        if not it["acceptance"]:
            bad[it["id"]] = "no acceptance lines: nothing checkable to plan against (answer it in the backlog)"
        unknown = [d for d in it["depends"] if d not in usable]
        if unknown:
            bad[it["id"]] = "depends on unknown item(s) " + ", ".join(unknown)
    # Anything depending (transitively) on a bad item cannot be compiled either.
    changed = True
    while changed:
        changed = False
        for it in usable.values():
            if it["id"] not in bad and any(d in bad for d in it["depends"]):
                bad[it["id"]] = "depends on an item that was not compiled: " + ", ".join(
                    d for d in it["depends"] if d in bad)
                changed = True
    order, placed = [], set()
    pending = [i for i in usable if i not in bad]
    while pending:
        ready = [i for i in pending if all(d in placed for d in usable[i]["depends"])]
        if not ready:
            for i in pending:
                bad[i] = "dependency cycle among: " + ", ".join(pending)
            break
        for i in ready:                              # source order within a wave
            order.append(i)
            placed.add(i)
        pending = [i for i in pending if i not in placed]
    batches, lineage = [], {}
    for i in order:
        deps_batches = {lineage[d] for d in usable[i]["depends"]}
        if not batches or len(batches[-1]["items"]) >= max_items:
            batches.append({"id": "batch-{0:02d}".format(len(batches) + 1), "items": [], "after": []})
        b = batches[-1]
        b["items"].append(i)
        for db in sorted(deps_batches):
            if db != b["id"] and db not in b["after"]:
                b["after"].append(db)
        lineage[i] = b["id"]
    for b in batches:
        b["criteria"] = [{"id": i, "text": "{0}: {1}".format(usable[i]["title"], "; ".join(usable[i]["acceptance"]))}
                         for i in b["items"]]
        b["deliverables"] = sorted({d for i in b["items"] for d in usable[i]["deliverables"]})
    not_compiled += [{"id": i, "reason": r} for i, r in bad.items()]
    return {"batches": batches, "not_compiled": not_compiled, "lineage": lineage,
            "unparsed": problems}


def _atomic(path, doc):
    tmp = path.with_name("." + path.name + "." + str(os.getpid()) + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def compile_file(backlog, *, max_items=DEFAULT_MAX_ITEMS, out=None):
    backlog = Path(backlog)
    raw = backlog.read_bytes()
    items, problems = parse(raw.decode("utf-8"))
    res = compile_batches(items, problems, max_items=max_items)
    res.update(source=str(backlog), source_sha256=hashlib.sha256(raw).hexdigest(),
               compiled_at=time.strftime("%Y-%m-%dT%H:%M:%S"), items=len(items))
    out = Path(out) if out else backlog.parent
    out.mkdir(parents=True, exist_ok=True)
    stem = backlog.stem
    _atomic(out / (stem + ".batches.json"), res)
    status_path = out / (stem + ".status.json")
    try:
        prior = json.loads(status_path.read_text(encoding="utf-8")).get("items") or {}
    except (OSError, ValueError):
        prior = {}
    status = {}
    for it in items:
        if it.get("_dup"):
            continue
        keep = prior.get(it["id"], {})
        status[it["id"]] = {"batch": res["lineage"].get(it["id"]),
                            "status": keep.get("status") if keep.get("status") == "completed" else
                            ("pending" if it["id"] in res["lineage"] else "blocked"),
                            "reason": next((n["reason"] for n in res["not_compiled"] if n["id"] == it["id"]), None)}
    _atomic(status_path, {"source_sha256": res["source_sha256"], "items": status})
    return res, out / (stem + ".batches.json"), status_path


def item_text(it):
    """The source item, verbatim in substance, for a batch's scope page."""
    out = ["### {0}: {1}".format(it["id"], it["title"])]
    if it["depends"]:
        out.append("Depends on: " + ", ".join(it["depends"]))
    if it["deliverables"]:
        out.append("Deliverables: " + ", ".join(it["deliverables"]))
    out.append("Acceptance:")
    out += ["- " + a for a in it["acceptance"]]
    out += it["notes"]
    return "\n".join(out)


def make_packages(backlog, batches_doc, parent_package, out_dir):
    """One milestone package per batch, plus the overnight backlog that runs them in order.

    Each package: the parent's project root and CONFIRMED intake (environment, limits, launch
    command), a scoped.md made of the batch's source items and the parent's approved scope as
    context, and seed_criteria.json with the item-id criteria the planner must cover. Nothing is
    approved here: a standing delegate approves each scoped.md at its exact bytes when it runs."""
    import intake
    import projectpkg
    parent = Path(parent_package)
    pdoc = projectpkg.load_project(parent)
    pname = pdoc.get("name") or "project"
    st = intake.load(pname, directory=parent)
    if st is None or not intake.is_done(st):
        raise SystemExit("the parent package's intake is not confirmed; confirm it before generating batch packages")
    items, _p = parse(Path(backlog).read_text(encoding="utf-8"))
    by_id = {it["id"]: it for it in items if not it.get("_dup")}
    parent_scope = (parent / "scoped.md").read_text(encoding="utf-8") if (parent / "scoped.md").is_file() else ""
    out_dir = Path(out_dir)
    milestones = []
    for b in batches_doc["batches"]:
        pkg = out_dir / b["id"]
        if (pkg / "project.json").is_file():
            milestones.append({"id": b["id"], "package": os.path.relpath(pkg, out_dir), "after": b["after"]})
            continue                                   # never rewrite a package that may have started
        projectpkg.ensure(pkg, Path(pdoc["project_root"]), b["id"])
        doc = projectpkg.load_project(pkg)
        for k in ("tool_mode", "project_kind", "intake_mode"):
            if pdoc.get(k) is not None:
                doc[k] = pdoc[k]
        doc["backlog_batch"] = {"source": str(backlog), "batch": b["id"], "items": b["items"]}
        projectpkg.save_project(pkg, doc)
        intake.save(b["id"], dict(st, project=b["id"]), directory=pkg)
        page = ["# {0}: backlog batch {1}".format(pname, b["id"]), "",
                "This milestone delivers exactly these backlog items (source: {0}).".format(Path(backlog).name), ""]
        page += [item_text(by_id[i]) + "\n" for i in b["items"]]
        if parent_scope:
            page += ["## Project context (the approved project scope)", "", parent_scope]
        (pkg / "scoped.md").write_text("\n".join(page).rstrip() + "\n", encoding="utf-8")
        (pkg / "seed_criteria.json").write_text(json.dumps({"batch": b["id"], "criteria": b["criteria"]}, indent=2),
                                               encoding="utf-8")
        milestones.append({"id": b["id"], "package": os.path.relpath(pkg, out_dir), "after": b["after"]})
    bl = out_dir / "backlog.json"
    _atomic(bl, {"milestones": milestones, "source": str(backlog)})
    return bl


def refresh_status(status_path, batches_doc, overnight_state):
    """Mark items completed whose batch the overnight controller finished (batch id == milestone id)."""
    st = json.loads(Path(status_path).read_text(encoding="utf-8"))
    ms = (overnight_state or {}).get("milestones") or {}
    for iid, rec in st["items"].items():
        b = rec.get("batch")
        if b and (ms.get(b) or {}).get("status") == "done":
            rec["status"] = "completed"
    _atomic(Path(status_path), st)
    return st


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compile")
    c.add_argument("backlog")
    c.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    c.add_argument("--out")
    k = sub.add_parser("packages")
    k.add_argument("backlog")
    k.add_argument("--parent-package", required=True, help="the project package whose intake/scope these extend")
    k.add_argument("--out", required=True, help="folder for the batch packages and backlog.json")
    s = sub.add_parser("status")
    s.add_argument("backlog")
    s.add_argument("--overnight", help="an overnight state file whose milestone ids are the batch ids")
    s.add_argument("--out")
    a = ap.parse_args(argv)
    if a.cmd == "compile":
        res, bpath, spath = compile_file(a.backlog, max_items=a.max_items, out=a.out)
        print("{0} items -> {1} batches; {2} not compiled; {3} unparsed headings".format(
            res["items"], len(res["batches"]), len(res["not_compiled"]), len(res["unparsed"])))
        for b in res["batches"]:
            print("  {0} ({1} items) after {2}: {3}".format(b["id"], len(b["items"]), ", ".join(b["after"]) or "-",
                                                             ", ".join(b["items"])))
        for n in res["not_compiled"]:
            print("  NOT COMPILED {0}: {1}".format(n["id"], n["reason"]))
        for p in res["unparsed"]:
            print("  UNPARSED line {0}: {1}".format(p["line"], p["problem"]))
        print("wrote {0} and {1}".format(bpath, spath))
        return 0 if not res["not_compiled"] and not res["unparsed"] else 1
    if a.cmd == "packages":
        res, _b, _s = compile_file(a.backlog)
        bl = make_packages(a.backlog, res, a.parent_package, a.out)
        print("wrote {0} batch package(s) and {1}".format(len(res["batches"]), bl))
        print("next: python run/overnight.py authorize {0} --as <you> --decisions N --seconds S".format(bl))
        return 0
    out = Path(a.out) if a.out else Path(a.backlog).parent
    spath = out / (Path(a.backlog).stem + ".status.json")
    st = json.loads(spath.read_text(encoding="utf-8"))
    if a.overnight:
        st = refresh_status(spath, None, json.loads(Path(a.overnight).read_text(encoding="utf-8")))
    counts = {}
    for rec in st["items"].values():
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    print(", ".join("{0} {1}".format(v, k) for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
