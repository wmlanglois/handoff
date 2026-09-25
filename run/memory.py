"""Retained experience: evidence-backed observations, corrections and usable skills that outlive a goal.

LF-01 (cross-goal experience) and LF-02 (reusable skills) share one store and one rule: nothing is
offered to later work unless the evidence it rests on can still be opened and still says what it
said. A retrospective paragraph with no run behind it is advice, not a lesson; a script nobody
verified is a file, not a skill; a source path nobody checked is a string, not evidence.

FOUR THINGS THE STORE KEEPS APART (audit 2026-09-20). They used to collapse into one row:
  * an OBSERVED EVENT   -- a check failed in round n and passed in round n+1 (kind "observation");
  * a SUPPORTED CORRECTION -- the recorded change between those attempts, in a run whose artifact
                              was finally ACCEPTED (kind "correction");
  * FINAL ACCEPTANCE    -- the receipt's, never this store's: an oracle pass is not acceptance;
  * a SKILL             -- an accepted, verified procedure with a callable contract, a location,
                           a content hash and prerequisites the harness can check.
When the record cannot establish a correction (no attempt snapshots, or the run was not
accepted) the store keeps an observation with its limits stated, and does not promote it.

IDENTITY. Rows are append-only. A status row replaces the entry; a `tally` row (uses, offered
outcomes) only adds counters and can never restore an older status over a concurrent withdrawal
or demotion. Skill ids derive from the source RUN identity and the artifact's content hash, so a
later goal that reuses a display name registers a different skill instead of replacing this one.

Every entry carries `source` (run id, evidence path, sha256 at the time), `applies_when`, `limits`,
`status`, and for skills a `contract`. Withdrawn, contradicted, demoted and legacy entries stay in
the file; retrieval ignores them and a reader can still see why.
"""
import ast
import difflib
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _runs_dir():
    """The run-state root (FLEET_RUNS_DIR aware) -- the same one the verified writer uses."""
    import fleet
    return fleet.runs_root()
def _memory_dir():
    import fleet
    return fleet.memory_dir()


# FLEET_MEMORY_DIR, else <runs root>/memory: retained experience must follow the run state it came
# from, or a checkout pointed at another tree neither reads nor extends what that fleet learned.
STORE = _memory_dir()
LESSONS = "lessons.jsonl"
SKILLS = "skills.jsonl"
SCHEMA = 2
# FLEET_MEMORY_DISABLED=1 turns retention AND retrieval off: the control condition for measuring
# whether retained experience helps. Everything else in the loop is unchanged.
DISABLED = os.environ.get("FLEET_MEMORY_DISABLED", "") == "1"
TALLY_FIELDS = ("uses", "last_used", "offered_outcomes")

STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are", "be",
        "that", "this", "it", "as", "by", "from", "at", "its", "into", "not", "must", "you", "your",
        # Harness vocabulary that appears in EVERY lesson and every brief. Left in, a lesson about
        # tax forms matched invoice work on "check", "module" and "passed" alone (live, run 3).
        "check", "checks", "passed", "failed", "fail", "pass", "module", "attempt", "next",
        "accepted", "artifact", "file", "line", "run", "python", "code", "returns", "return",
        "own", "one", "such", "already", "instead", "using", "use", "new", "outcome", "own",
        "evidence", "acceptance", "progress", "investigated", "verified", "assertionerror",
        "error", "exposing", "existing", "correct", "project", "list", "int", "string",
        "import", "def", "self", "runs", "dev", "fleet"}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _sha(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _path(name):
    STORE.mkdir(parents=True, exist_ok=True)
    return STORE / name


def _rows(name):
    p = _path(name)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _append(name, row):
    with open(_path(name), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def _tally(name, entry, **counters):
    """Append a counter-only row. It can never change status: `_latest` folds it in as numbers."""
    row = {"id": entry["id"], "op": "tally", "at": _now()}
    row.update(counters)
    return _append(name, row)


def _latest(name):
    """The current entry per id.

    A row without `op` (or op "status") REPLACES the entry, except that counters are never
    lowered by it -- a status row written from a stale read must not erase uses recorded since.
    A row with op "tally" only adds counters. Reproduced before this rule: withdraw(), then a
    concurrent note_use() built from a read taken BEFORE the withdrawal, appended a whole row with
    status "active" and the withdrawal was undone. Counters and status now travel separately."""
    seen = {}
    for r in _rows(name):
        cur = seen.get(r["id"])
        if r.get("op") == "verify":
            # A verification result is appended as an OP, never as a whole row: it adds to the
            # reliability record and may DEMOTE an active entry, but it cannot set any other
            # status. Reproduced before this rule: a verifier that read ACTIVE, then an operator
            # withdrawal, then the verifier's whole-row write -- the skill came back ACTIVE.
            if cur is None:
                continue
            rel = dict(cur.get("reliability") or {"uses": 0, "verified": 0, "failed": 0, "history": []})
            rel["uses"] = int(rel.get("uses", 0)) + 1
            key = "verified" if r.get("ok") else "failed"
            rel[key] = int(rel.get(key, 0)) + 1
            rel["history"] = list(rel.get("history") or []) + [r.get("entry") or {}]
            cur["reliability"] = rel
            if (not r.get("ok") and cur.get("status") == "active"
                    and int(rel["failed"]) >= int(r.get("demote_after") or 2)):
                cur["status"] = "demoted"
                cur["demoted"] = {"reason": "{0} verification failure(s)".format(rel["failed"]),
                                  "by": (r.get("entry") or {}).get("by"), "at": r.get("at")}
            continue
        if r.get("op") == "contract":
            if cur is not None:
                for k in ("contract", "procedure", "contract_refreshed"):
                    if k in r:
                        cur[k] = r[k]
            continue
        if r.get("op") == "withdraw":
            # Terminal and unconditional: whatever the writer had read, withdrawal is the
            # operator's decision and applies to the entry as it stands now.
            if cur is not None:
                cur["status"] = "withdrawn"
                cur["withdrawn"] = r.get("withdrawn")
            continue
        if r.get("op") == "revalidate":
            # Authorized transition demoted -> active ONLY. A revalidation computed against a
            # stale read cannot lift a withdrawal that landed in between (audit counterexample).
            if cur is not None and cur.get("status") == "demoted":
                rel = dict(cur.get("reliability") or {})
                rel["failed_before_revalidation"] = int(rel.get("failed", 0))
                rel["failed"] = 0
                cur["status"] = "active"
                cur["reliability"] = rel
                cur["revalidated"] = r.get("revalidated")
            elif cur is not None:
                cur.setdefault("refused_revalidations", []).append(
                    dict(r.get("revalidated") or {}, status_at_apply=cur.get("status")))
            continue
        if r.get("op") == "suspend":
            # LF-14: an applicability CONDITION the entry depends on is contradicted by current
            # evidence. The entry keeps its history and evidence; it is simply not offered until
            # a reassessment with evidence says the condition holds again (or it is narrowed).
            if cur is not None and cur.get("status") == "active":
                cur["status"] = "suspended"
                cur["suspended"] = r.get("suspended")
            elif cur is not None:
                cur.setdefault("suspend_notes", []).append(r.get("suspended"))
            continue
        if r.get("op") == "reassess":
            if cur is not None and cur.get("status") == "suspended":
                cur["status"] = "active"
                cur["reassessed"] = r.get("reassessed")
                if r.get("applies_when") is not None:
                    cur["applies_when"] = r["applies_when"]          # narrowed applicability
                if r.get("depends_on") is not None:
                    cur["depends_on"] = r["depends_on"]
            elif cur is not None:
                cur.setdefault("refused_reassessments", []).append(
                    dict(r.get("reassessed") or {}, status_at_apply=cur.get("status")))
            continue
        if r.get("op") == "contradict":
            if cur is not None:
                cur["status"] = "contradicted"
                for k in ("contradicts", "note", "by", "at"):
                    if k in r:
                        cur[k] = r[k]
            continue
        if r.get("op") == "tally":
            if cur is None:
                continue                      # a tally for something not yet defined: ignore
            for k, v in r.items():
                if k in ("id", "op", "at"):
                    continue
                if k in ("offered_outcomes", "unknown_verifications"):
                    hist = dict(cur.get(k) or {})
                    for o, n in (v or {}).items():
                        hist[o] = int(hist.get(o, 0)) + int(n)
                    cur[k] = hist
                elif k == "uses":
                    cur["uses"] = int(cur.get("uses", 0)) + int(v)
                else:
                    cur[k] = v
            continue
        new = dict(r)
        if cur is not None:
            new["uses"] = max(int(cur.get("uses", 0)), int(new.get("uses", 0)))
            merged = dict(cur.get("offered_outcomes") or {})
            for o, n in (new.get("offered_outcomes") or {}).items():
                merged[o] = max(int(merged.get(o, 0)), int(n))
            if merged:
                new["offered_outcomes"] = merged
        seen[r["id"]] = new
    return seen


def tags_for(text):
    """Applicability tags from free text: lowercase content words, de-duplicated."""
    text = re.sub(r"[-_./]", " ", (text or "").lower())
    words = re.findall(r"[a-z][a-z0-9]{2,}", text)
    return sorted({w for w in words if w not in STOP})[:40]


def _overlap(a, b):
    """Share of the entry's applicability tags present in the work at hand, both sides filtered
    through the CURRENT stop list (the store is append-only, so re-tagging happens here)."""
    a = {t for t in (a or ()) if t not in STOP}
    b = {t for t in (b or ()) if t not in STOP}
    return len(a & b) / float(len(a) or 1)


# =================================================================================================
# evidence
# =================================================================================================

def evidence_state(entry):
    """present | changed | missing | unverified. Judged now, against the file the entry named.

    A path that is merely non-empty proves nothing; an entry whose evidence has been rewritten
    since it was recorded no longer says what the entry says it says. Both are excluded from
    retrieval and shown as such in listings."""
    src = entry.get("source") or {}
    ref = src.get("evidence_ref") or src.get("ref") or ""
    want = src.get("sha256") or ""
    if not ref or not want:
        return "unverified"
    p = Path(ref)
    if not p.exists():
        return "missing"
    if p.is_dir():
        return "present" if want == "dir" else "changed"
    return "present" if _sha(p) == want else "changed"


def _source(ref):
    p = Path(ref)
    return {"evidence_ref": str(p), "sha256": ("dir" if p.is_dir() else _sha(p)) if p.exists() else ""}


# =================================================================================================
# LF-01  observations and corrections
# =================================================================================================

def extract_lessons_from_run(run_name, family_tags=(), runs_root=None):
    """Turn one FINISHED run record into observations and, where the record supports it, corrections.

    The pattern looked for is a FAIL -> PASS on the check between adjacent rounds. What is made of
    it depends on what the record can establish:
      * the run's FINAL disposition is `accepted` AND the two attempts' artifacts were snapshotted
        (runs/verified-<run>/attempts/r<n>/<artifact>) -> a CORRECTION carrying the actual diff;
      * otherwise -> an OBSERVATION: the failure text, the later pass, the real final disposition,
        and an explicit statement of what was NOT recorded or NOT accepted.
    A parked-unsupported run therefore never yields a row that speaks of an "accepted artifact",
    which is what the first extractor did (audit 2026-09-20)."""
    base = Path(runs_root or _runs_dir()) / ("verified-" + run_name)
    if DISABLED:
        return []
    try:
        rec = json.loads((base / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    disposition = str(rec.get("outcome") or "unknown")
    hist = rec.get("history") or []
    oracle_rows = _oracle_rows(base.parent, run_name) + _receipt_rows(base / "receipt.md")
    receipt = base / "receipt.md"
    made = []
    for i in range(1, len(hist)):
        prev, cur = hist[i - 1], hist[i]
        if not (prev.get("oracle_ok") is False and cur.get("oracle_ok") is True):
            continue
        failure = (_oracle_text(oracle_rows, prev.get("round"), ok=False)
                   or str(prev.get("basis") or prev.get("ruling") or ""))[:300]
        passing = (_oracle_text(oracle_rows, cur.get("round"), ok=True)
                   or str(cur.get("basis") or ""))[:300]
        change = _attempt_diff(base, prev.get("round"), cur.get("round"))
        lid = "L-" + hashlib.sha256((run_name + failure + str(prev.get("round"))).encode()
                                    ).hexdigest()[:10]
        if lid in _latest(LESSONS):
            continue
        tags = sorted(set(tags_for(failure)) | set(family_tags))
        src = _source(receipt)
        src.update(run=run_name, round_failed=prev.get("round"), round_passed=cur.get("round"),
                   disposition=disposition)
        depends_on = _dependencies_of(base, cur.get("artifact"))
        if disposition == "accepted" and change:
            kind = "correction"
            text = ("PROBLEM: the check failed as: {0}. SUPPORTED CHANGE (recorded diff between "
                    "attempt r{1} and attempt r{2}, after which the frozen check passed: {3}):\n{4}"
                    ).format(failure, prev.get("round"), cur.get("round"),
                             passing or "no detail", change)
            limits = ("The diff is what changed; it is not established that every hunk was "
                      "necessary, nor that the same change is right for a different failure "
                      "text. Acceptance is the receipt's verdict on that run's artifact, not a "
                      "property of this correction. Observed once, on one worker.")
            src["attempts"] = [str(base / "attempts" / "r{0}".format(prev.get("round"))),
                               str(base / "attempts" / "r{0}".format(cur.get("round")))]
        else:
            kind = "observation"
            why_not = []
            if disposition != "accepted":
                why_not.append("the run's final disposition was {0!r}, not accepted".format(
                    disposition))
            if not change:
                why_not.append("no attempt snapshots exist, so the change between the two "
                               "rounds is not recorded")
            text = ("OBSERVED: the check failed as: {0} (round {1}); a later round passed ({2}). "
                    "Final disposition of the run: {3}. This is an observation, not a "
                    "correction: {4}.").format(failure, prev.get("round"),
                                              passing or "no detail", disposition,
                                              "; ".join(why_not))
            limits = ("Shows only that this failure text was followed by a pass on one run. "
                      "What resolved it is not recorded here; do not apply it as a fix.")
        made.append(_append(LESSONS, {
            "id": lid, "schema": SCHEMA, "kind": kind, "text": text, "source": src,
            "applies_when": tags, "limits": limits, "depends_on": depends_on,
            "status": "active", "created": _now(), "uses": 0}))
    return made


def _dependencies_of(base, artifact):
    """The conditions a lesson about this artifact rests on, as the harness can actually inspect
    them: the non-stdlib modules the accepted artifact imports, with the bytes they had (hash) in
    the run's workspace. If money.py changes later, advice derived against this money.py is
    suspect -- not because the lesson text changed, but because its condition did."""
    out = []
    try:
        art = base / (artifact or "output.md")
        if not art.is_file() or art.suffix != ".py":
            return out
        contract = _module_contract(art)
        for dep in contract.get("dependencies") or []:
            p = base / (dep + ".py")
            if p.is_file():
                out.append({"type": "file", "module": dep, "path": str(p), "sha256": _sha(p),
                            "note": "the artifact imported this module with these bytes"})
            else:
                out.append({"type": "module", "module": dep, "note": "imported; bytes not in the workspace"})
    except Exception:
        return out
    return out


def _condition_holds(cond, evidence):
    """satisfied | contradicted | unknown, for one dependency against current evidence:
    evidence = {"files": {module_or_path: sha256}, "claims": {text: bool}}."""
    files = evidence.get("files") or {}
    claims = evidence.get("claims") or {}
    if cond.get("type") == "file":
        cur = files.get(cond.get("module")) or files.get(cond.get("path"))
        if cur is None:
            return "unknown"
        return "satisfied" if cur == cond.get("sha256") else "contradicted"
    if cond.get("type") == "claim":
        v = claims.get(_norm(cond.get("text")))
        if v is None:
            return "unknown"
        return "satisfied" if v else "contradicted"
    if cond.get("type") == "skill":
        st = _latest(SKILLS).get(cond.get("id"))
        if st is None:
            return "unknown"
        return "satisfied" if st.get("status") == "active" else "contradicted"
    return "unknown"


def reconcile_assumptions(evidence, *, by="harness"):
    """Suspend every active entry with a `depends_on` condition CONTRADICTED by current evidence;
    leave satisfied and unknown ones alone. Returns the ids suspended, each with the reason.
    `evidence["files"]` maps module names (or paths) to the sha256 of their CURRENT bytes."""
    out = []
    if DISABLED:
        return out
    for name in (LESSONS, SKILLS):
        for eid, e in _latest(name).items():
            if e.get("status") != "active":
                continue
            for cond in e.get("depends_on") or []:
                if _condition_holds(cond, evidence) == "contradicted":
                    reason = ("condition contradicted: {0} {1} now differs from the bytes this entry was "
                              "derived against".format(cond.get("type"), cond.get("module") or cond.get("text") or cond.get("id")))
                    _append(name, {"id": eid, "op": "suspend", "at": _now(),
                                   "suspended": {"by": by, "reason": reason, "condition": cond, "at": _now()}})
                    out.append((eid, reason))
                    break
    return out


def reassess(entry_id, *, by, evidence_ref, note="", applies_when=None, depends_on=None):
    """Return a suspended entry to service on stated evidence, optionally NARROWING its
    applicability or restating what it depends on. Refused without an evidence reference."""
    if not (evidence_ref or "").strip():
        raise ValueError("a reassessment needs an evidence reference")
    for name in (LESSONS, SKILLS):
        cur = _latest(name).get(entry_id)
        if cur:
            if cur.get("status") != "suspended":
                raise ValueError("only a suspended entry is reassessed; {0} is {1}".format(entry_id, cur.get("status")))
            row = {"id": entry_id, "op": "reassess", "at": _now(),
                   "reassessed": {"by": by, "evidence_ref": evidence_ref, "note": note, "at": _now()}}
            if applies_when is not None:
                row["applies_when"] = sorted(set(applies_when))
            if depends_on is not None:
                row["depends_on"] = list(depends_on)
            _append(name, row)
            return _latest(name)[entry_id]
    raise KeyError(entry_id)


def _attempt_diff(base, r_fail, r_pass, max_lines=40):
    """Unified diff between two attempt snapshots of the named artifact, or "" when either is
    missing. Snapshots are written by run/verified.py per round; runs before 2026-09-20 have none."""
    a_dir, b_dir = base / "attempts" / "r{0}".format(r_fail), base / "attempts" / "r{0}".format(r_pass)
    if not (a_dir.is_dir() and b_dir.is_dir()):
        return ""
    out = []
    for b_file in sorted(b_dir.iterdir()):
        if not b_file.is_file() or b_file.name == "output.md":
            continue
        a_file = a_dir / b_file.name
        a_text = a_file.read_text(encoding="utf-8", errors="replace").splitlines() if a_file.exists() else []
        b_text = b_file.read_text(encoding="utf-8", errors="replace").splitlines()
        diff = list(difflib.unified_diff(a_text, b_text, "r{0}/{1}".format(r_fail, b_file.name),
                                         "r{0}/{1}".format(r_pass, b_file.name), lineterm="", n=1))
        out.extend(diff)
    if not out:
        return ""
    if len(out) > max_lines:
        out = out[:max_lines] + ["... ({0} more diff lines in the attempt snapshots)".format(
            len(out) - max_lines)]
    return "\n".join(out)


def _oracle_rows(runs_root, run_name):
    """Oracle events from the run's newest emit log, as (round, ok, msg)."""
    logs = sorted(Path(runs_root).glob("verified-" + run_name + "-*.jsonl"),
                  key=lambda q: q.stat().st_mtime, reverse=True)
    rows = []
    if not logs:
        return rows
    for line in logs[0].read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("event") == "oracle":
            rows.append((str(row.get("round")), str(row.get("ok")) == "True",
                         str(row.get("msg") or "")))
    return rows


_RECEIPT_ROUND = re.compile(r"^round (\d+)\b")
_RECEIPT_ORACLE = re.compile(r"^\s*oracle:\s+(PASS|FAIL)(?:\s+\((.*)\))?\s*$")


def _receipt_rows(receipt_path):
    """Oracle verdicts from the human-readable receipt, as (round, ok, msg)."""
    try:
        text = Path(receipt_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows, rnd = [], None
    for line in text.splitlines():
        m = _RECEIPT_ROUND.match(line)
        if m:
            rnd = m.group(1)
            continue
        m = _RECEIPT_ORACLE.match(line)
        if m and rnd is not None:
            rows.append((rnd, m.group(1) == "PASS", (m.group(2) or "").strip()))
    return rows


def _noise(line):
    s = line.strip()
    return (not s or s.startswith("File ") or s.startswith("Traceback")
            or set(s) <= set("~^ "))


def _oracle_text(rows, rnd, ok):
    """The most informative oracle message for one round: prefer one naming an error."""
    cleaned = []
    for r, k, m in rows:
        if r != str(rnd) or k != ok:
            continue
        lines = [ln.strip() for ln in m.splitlines() if not _noise(ln)]
        named = [ln for ln in lines if "Error" in ln]
        text = named[-1] if named else " ".join(lines[-2:])
        if text.strip():
            cleaned.append(text.strip()[-300:])
    if not cleaned:
        return ""
    cleaned.sort(key=lambda t: ("Error" in t or "assert" in t.lower(), len(t)), reverse=True)
    return cleaned[0]


def add_lesson(text, source_ref, applies_when, limits, *, by, kind="observation", unverified=False,
               depends_on=None):
    """A human- or architect-authored entry.

    The source must be a path that exists NOW (it is hashed so later changes are detectable), or
    the caller must say `unverified=True`, in which case the entry is stored with status
    "unverified" and is never offered as valid. A non-empty string was accepted before; that let
    an opinion wear the clothes of a validated lesson."""
    if not (source_ref or "").strip():
        raise ValueError("a lesson needs a source reference; unsourced advice is not a lesson")
    if not (by or "").strip():
        raise ValueError("a lesson needs an author")
    if kind not in ("observation", "correction"):
        raise ValueError("kind must be observation or correction")
    p = Path(source_ref)
    if not p.exists() and not unverified:
        raise ValueError("source {0!r} does not exist; pass unverified=True to store it as an "
                         "UNVERIFIED note that is never offered as valid".format(source_ref))
    lid = "L-" + hashlib.sha256((text + source_ref).encode()).hexdigest()[:10]
    src = _source(p) if p.exists() else {"evidence_ref": str(p), "sha256": ""}
    src["by"] = by
    return _append(LESSONS, {"id": lid, "schema": SCHEMA, "kind": kind, "text": text.strip(),
                             "source": src,
                             "applies_when": sorted(set(applies_when or tags_for(text))),
                             "limits": limits or "", "depends_on": list(depends_on or []),
                             "status": "active" if p.exists() else "unverified",
                             "created": _now(), "uses": 0})


def withdraw_lesson(lesson_id, reason, *, by):
    cur = _latest(LESSONS).get(lesson_id)
    if not cur:
        raise KeyError(lesson_id)
    _append(LESSONS, {"id": lesson_id, "op": "withdraw", "at": _now(),
                      "withdrawn": {"reason": reason, "by": by, "at": _now()}})
    return _latest(LESSONS)[lesson_id]


def contradict(lesson_a, lesson_b, note, *, by):
    """Two entries that cannot both hold: both marked, neither offered until a person resolves it."""
    latest = _latest(LESSONS)
    out = []
    for lid, other in ((lesson_a, lesson_b), (lesson_b, lesson_a)):
        if lid not in latest:
            raise KeyError(lid)
        _append(LESSONS, {"id": lid, "op": "contradict", "contradicts": other, "note": note,
                          "by": by, "at": _now()})
        out.append(_latest(LESSONS)[lid])
    return out


def retrieve_lessons(brief, tags=(), k=3, min_overlap=0.15):
    """Active entries whose evidence is still present and whose applicability overlaps the work.
    Corrections rank above observations at equal overlap. Empty when nothing fits."""
    want = set(tags_for(brief)) | set(tags or ())
    scored = []
    for r in _latest(LESSONS).values():
        if r.get("status") != "active":
            continue
        if evidence_state(r) != "present":
            continue
        s = _overlap(r.get("applies_when"), want)
        if s >= min_overlap:
            scored.append((s, 1 if r.get("kind") == "correction" else 0, r))
    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [dict(r, match=round(s, 2)) for s, _, r in scored[:k]]


def note_outcome(entry_id, outcome):
    """How work that was HANDED this entry ended: a tally, never a verdict or a status change."""
    for name in (LESSONS, SKILLS):
        cur = _latest(name).get(entry_id)
        if cur:
            return _tally(name, cur, offered_outcomes={outcome: 1})
    return None


def note_use(entry_id):
    for name in (LESSONS, SKILLS):
        cur = _latest(name).get(entry_id)
        if cur:
            return _tally(name, cur, uses=1, last_used=_now())
    return None


def disposition_legacy(*, by="harness"):
    """Non-destructive disposition of rows written before this schema.

    A pre-schema lesson was extracted without the run's final disposition or a change record, so
    it is re-labelled an OBSERVATION with an explicit evidential status; its evidence hash is
    recorded now. A pre-schema skill has no callable contract or content hash, so it is marked
    `legacy` and not offered. History is appended to, not rewritten; nothing is relabelled
    trustworthy."""
    done = []
    for lid, r in _latest(LESSONS).items():
        if r.get("schema") == SCHEMA or r.get("status") in ("withdrawn", "contradicted"):
            continue
        src = dict(r.get("source") or {})
        ref = src.get("evidence_ref") or src.get("ref") or ""
        src.update(_source(ref) if ref else {})
        src["evidential_status"] = ("legacy: extracted before final disposition and attempt "
                                    "snapshots were recorded; treated as an observation")
        done.append(_append(LESSONS, dict(r, schema=SCHEMA, kind="observation", source=src,
                                          limits=(r.get("limits") or "") + " Legacy row: the "
                                          "'accepted artifact' wording it may carry was not "
                                          "checked against the run's final disposition.",
                                          dispositioned={"by": by, "at": _now()})))
    for sid, r in _latest(SKILLS).items():
        if r.get("schema") == SCHEMA or r.get("status") != "active":
            continue
        done.append(_append(SKILLS, dict(r, schema=SCHEMA, status="legacy",
                                         legacy={"by": by, "at": _now(),
                                                 "reason": "no callable contract, location hash "
                                                           "or prerequisite structure recorded"})))
    return done


# =================================================================================================
# LF-02  skills
# =================================================================================================

def _module_contract(path):
    """What the module ACTUALLY exposes, read from its source with `ast`: top-level functions with
    their parameter names and first docstring line, and the modules it imports. Facts, not prose."""
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError) as e:
        return {"error": "{0}: {1}".format(type(e).__name__, e), "entry_points": [], "imports": []}
    entries, imports = [], set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            args = [a.arg for a in node.args.args]
            doc = (ast.get_docstring(node) or "").strip().splitlines()
            entries.append({"name": node.name, "params": args,
                            "doc": doc[0][:160] if doc else ""})
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            doc = (ast.get_docstring(node) or "").strip().splitlines()
            entries.append({"name": node.name, "params": [],
                            "doc": doc[0][:160] if doc else ""})
        elif isinstance(node, ast.Import):
            imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    std = getattr(sys, "stdlib_module_names", set())
    return {"entry_points": entries,
            "imports": sorted(imports),
            "dependencies": sorted(m for m in imports if m not in std and m != "__future__")}


_EXAMPLE = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\((.*?)\)\s*==\s*([^;,]+?)(?:[,;]|$)")


def _examples_from_oracle(oracle, module):
    """Verified example calls with their literal argument and return types, read from the frozen
    oracle. These are the only I/O facts the harness can vouch for; units beyond the literal type
    (cents, dollars) are stated as shown by the examples, not inferred."""
    out = []
    found = list(_EXAMPLE.findall(oracle or ""))
    # The other common oracle shape: `got=mod.fn(args); assert got==value, '...'`. Without it
    # the live discount skill carried "no examples" and the composition note could not fire.
    got = re.compile(r"\b(\w+)\s*=\s*([A-Za-z_]\w*)\.([A-Za-z_]\w*)\((.*?)\);\s*assert\s+\1\s*==\s*([^,;]+?)(?:[,;]|$)")
    found += [(mod, fn, args, ret) for _v, mod, fn, args, ret in got.findall(oracle or "")]
    for mod, fn, args, ret in found:
        if mod != module:
            continue
        try:
            arg_vals = list(ast.literal_eval("(" + args + ",)")) if args.strip() else []
            ret_val = ast.literal_eval(ret.strip())
        except (ValueError, SyntaxError):
            continue
        out.append({"call": "{0}.{1}({2})".format(mod, fn, args.strip()), "returns": repr(ret_val),
                    "arg_types": [type(v).__name__ for v in arg_vals],
                    "return_type": type(ret_val).__name__, "function": fn})
    return out[:6]


def register_skill(name, procedure, preconditions, expected_output, verify, *, source_ref,
                   version="1", failure_behavior="", by="harness", contract=None, location="",
                   sha256="", run_id="", module="", applies_when=None, depends_on=None):
    """A verified procedure. `verify` is how a consumer confirms it (a check expression);
    `contract` is what it actually exposes; `location`/`sha256` identify the exact bytes."""
    for field, val in (("procedure", procedure), ("verify", verify), ("source_ref", source_ref)):
        if not (val or "").strip():
            raise ValueError("a skill needs a non-empty {0}".format(field))
    ident = (run_id or name) + ":" + (sha256 or version)
    sid = "S-" + hashlib.sha256(ident.encode()).hexdigest()[:10]
    src = _source(source_ref)
    src["by"] = by
    src["run"] = run_id or ""
    return _append(SKILLS, {"id": sid, "schema": SCHEMA, "kind": "skill", "name": name,
                            "version": version, "module": module or Path(location).stem,
                            "location": str(location), "sha256": sha256,
                            "procedure": procedure.strip(),
                            "preconditions": list(preconditions or ()),
                            "expected_output": expected_output or "",
                            "contract": contract or {}, "verify": verify.strip(),
                            "verify_scope": ("the source run's frozen oracle; it is re-run in the "
                                             "destination workspace against the staged copy before "
                                             "the skill is relied on"),
                            "failure_behavior": failure_behavior or "unspecified",
                            # Applicability comes from what the skill IS (name, purpose, entry
                            # points), not from the whole procedure text: a long procedure dilutes
                            # the overlap until a relevant skill is never offered (live, goal 6).
                            "applies_when": sorted(set(applies_when) if applies_when else
                                                   set(tags_for(name + " " + procedure))),
                            "source": src,
                            # LF-14: the conditions this skill was accepted under, as the harness
                            # can inspect them (the declared sources' bytes). A different money.py
                            # later suspends the skill until it is reassessed on evidence.
                            "depends_on": list(depends_on or []),
                            "reliability": {"uses": 0, "verified": 0, "failed": 0, "history": []},
                            "status": "active", "created": _now(), "uses": 0})


def _accepted_py(run_name, card, base, deliverable_path, art):
    """The accepted .py bytes, not whichever same-named file was staged first.

    Tool jobs leave the receipt-bound module in the tool workspace. The verified workspace often
    holds no copy, or an older one. Persist the bound bytes next to the receipt so a later goal
    can stage that exact version."""
    names = [art]
    dest = (card.get("dest") or "").replace("\\", "/")
    if dest and dest not in names:
        names.append(dest)
    # Accepted-artifact identity ONLY: the exact bytes the run's receipt bound, resolved by hash
    # through the shared resolver (rooted at THIS run's runs dir). Missing or mismatched bound bytes
    # are REFUSED -- never a silent fallback to a same-named, unbound file on disk. That fallback
    # let stale/unbound bytes be registered as accepted experience (IDENTITY_MISMATCH_FALLBACK).
    data = None
    _accepted_py.last_reason = ""
    try:
        import integrate
        _name, data, _h = integrate._resolve_accepted(run_name, names, runs_root=base.parent)
    except Exception as e:
        _accepted_py.last_reason = str(e)
        return None
    if data is None:
        _accepted_py.last_reason = "accepted bytes for {0} could not be resolved by receipt identity".format(names)
        return None
    rel = dest or art
    if not str(rel).endswith(".py"):
        return None
    loc = base / rel
    try:
        loc.parent.mkdir(parents=True, exist_ok=True)
        if not loc.is_file() or loc.read_bytes() != data:
            loc.write_bytes(data)
    except OSError as e:
        _accepted_py.last_reason = "could not persist accepted bytes: {0}".format(e)
        return None
    return loc


def register_skill_from_run(run_name, deliverable_path, card, runs_root=None):
    """Promote an ACCEPTED deliverable with a frozen oracle into a skill, or return None with the
    reason in `register_skill_from_run.last_reason`.

    Resolves the actual file (the `deliverable_path` the caller names, else the run's named
    artifact), hashes it, reads its real entry points and dependencies, and records the verified
    example calls from the oracle. It does NOT claim the module is on anyone's import path: the
    consumer stages the exact bytes into its own workspace (run/verified.py) and re-verifies."""
    base = Path(runs_root or _runs_dir()) / ("verified-" + run_name)
    register_skill_from_run.last_reason = ""
    if DISABLED:
        register_skill_from_run.last_reason = "memory disabled"
        return None
    try:
        rec = json.loads((base / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        register_skill_from_run.last_reason = "no result record"
        return None
    if rec.get("outcome") != "accepted":
        register_skill_from_run.last_reason = "not accepted ({0})".format(rec.get("outcome"))
        return None
    if not (card.get("oracle") or "").strip():
        register_skill_from_run.last_reason = "no frozen oracle: unverified deliverable"
        return None
    art = (card.get("artifact") or "output.md").replace("\\", "/")
    loc = _accepted_py(run_name, card, base, deliverable_path, art)
    if loc is None:
        register_skill_from_run.last_reason = (
            getattr(_accepted_py, "last_reason", "") or "no importable module at {0}".format(base / art))
        return None
    module = loc.stem
    contract = _module_contract(loc)
    contract["examples"] = _examples_from_oracle(card["oracle"], module)
    if not contract["entry_points"]:
        register_skill_from_run.last_reason = "module exposes no public function"
        return None
    sha = _sha(loc)
    display = card.get("assignment") or card.get("name") or run_name
    pre = [{"type": "file", "path": str(loc), "sha256": sha}]
    pre += [{"type": "module", "name": d} for d in contract.get("dependencies", [])]
    pre += [{"type": "claim", "text": kc} for kc in (card.get("keep_compatible") or [])]
    sig = "; ".join("{0}({1})".format(e["name"], ", ".join(e["params"]))
                    for e in contract["entry_points"][:6])
    ex = "; ".join("{0} -> {1} [{2} -> {3}]".format(e["call"], e["returns"],
                                                     ",".join(e["arg_types"]), e["return_type"])
                   for e in contract["examples"][:4]) or "none recorded in the oracle"
    procedure = ("Accepted module `{0}` (version {1}, staged into your workspace as {0}.py when "
                 "offered; import it as `import {0}`). Entry points: {2}. Verified examples "
                 "(argument types -> return type): {3}. Depends on: {4}. Purpose: {5}").format(
                     module, sha[:8], sig, ex,
                     ", ".join(contract.get("dependencies") or []) or "standard library only",
                     (card.get("capability") or card.get("brief") or "")[:200].replace("\n", " "))
    # Applicability is the module's own surface, not the repair brief or the project check.
    # A history class registered from an integration-repair card was tagged with combo/smoke
    # words and then missed a later replay task that actually called it.
    applies = tags_for(" ".join([
        display, module, card.get("capability") or "",
        " ".join(e["name"] for e in contract["entry_points"]),
        " ".join((e.get("doc") or "") for e in contract["entry_points"]),
    ]))
    return register_skill(name=display, procedure=procedure, preconditions=pre, applies_when=applies,
                          expected_output=str(card.get("done_when") or "")[:300],
                          verify=card["oracle"], source_ref=str(base / "receipt.md"),
                          version=sha[:8], run_id=run_name, module=module, location=str(loc),
                          sha256=sha, contract=contract,
                          depends_on=[{"type": "file", "module": d.get("module"), "path": d.get("location"),
                                       "sha256": d.get("sha256"),
                                       "note": "accepted with this declared source staged beside it"}
                                      for d in (card.get("deps") or []) if d.get("module") and d.get("sha256")],
                          failure_behavior=("its frozen oracle fails when re-run against the staged "
                                            "copy in the destination workspace; the harness then "
                                            "records a verification failure against this version"))


register_skill_from_run.last_reason = ""
_accepted_py.last_reason = ""


def find_skills(brief, tags=(), k=3, min_overlap=0.2):
    """Active skills with present evidence, one per display name (the newest active version)."""
    want = set(tags_for(brief)) | set(tags or ())
    scored = []
    for r in _latest(SKILLS).values():
        if r.get("status") != "active" or r.get("schema") != SCHEMA:
            continue
        if evidence_state(r) != "present":
            continue
        s = _overlap(r.get("applies_when"), want)
        if s >= min_overlap:
            scored.append((s, r))
    scored.sort(key=lambda t: (-t[0], t[1].get("created", "")))
    seen, out = set(), []
    for s, r in sorted(scored, key=lambda t: t[1].get("created", ""), reverse=True):
        if r["name"] in seen:
            continue
        seen.add(r["name"])
        out.append((s, r))
    out.sort(key=lambda t: -t[0])
    return [dict(r, match=round(s, 2)) for s, r in out[:k]]


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower().rstrip(".")


_NEG = re.compile(r"^(?:not |no )|(?: is (?:false|absent|missing|not true)| missing| absent)$")


def prerequisites_state(skill, facts=(), env=None):
    """Tri-state check of a skill's prerequisites: {"satisfied": [...], "contradicted": [...],
    "unknown": [...]}.

      file   -> the harness checks existence and content hash itself;
      module -> satisfied if `env["modules"]` (what the destination can import) contains it,
                contradicted if env is given and it is absent, unknown without env;
      claim  -> matched against `facts`: a fact equal to the claim satisfies; a fact that is its
                negation ("not X", "X is false", "X missing") contradicts; anything else is
                unknown. A requirement to KEEP something compatible is not a fact about it.
    The old substring test accepted `money.py present is FALSE` as satisfying `money.py present`."""
    state = {"satisfied": [], "contradicted": [], "unknown": []}
    fact_true, fact_false = set(), set()
    for f in facts or ():
        if isinstance(f, dict):
            (fact_true if f.get("true", True) else fact_false).add(_norm(f.get("claim")))
            continue
        t = _norm(f)
        m = re.match(r"^(?:not |no )(.*)$", t) or re.match(r"^(.*?) (?:is )?(?:false|absent|missing|not true)$", t)
        if m:
            fact_false.add(_norm(m.group(1)))
        else:
            fact_true.add(t)
    for p in skill.get("preconditions") or ():
        if isinstance(p, str):
            p = {"type": "claim", "text": p}
        kind = p.get("type")
        if kind == "file":
            path = Path(p.get("path", ""))
            if not path.is_file():
                state["contradicted"].append("file missing: {0}".format(path))
            elif p.get("sha256") and _sha(path) != p["sha256"]:
                state["contradicted"].append("file changed since verification: {0}".format(path))
            else:
                state["satisfied"].append("file present and unchanged: {0}".format(path.name))
        elif kind == "module":
            mods = None if env is None else set(env.get("modules") or ())
            if mods is None:
                state["unknown"].append("module {0!r}: destination imports not known".format(p["name"]))
            elif p["name"] in mods:
                state["satisfied"].append("module {0!r} importable in the destination".format(p["name"]))
            else:
                state["contradicted"].append("module {0!r} not importable in the destination".format(p["name"]))
        else:
            t = _norm(p.get("text"))
            if t in fact_false:
                state["contradicted"].append("claim contradicted by a stated fact: {0}".format(t))
            elif t in fact_true:
                state["satisfied"].append("claim stated as a fact: {0}".format(t))
            else:
                state["unknown"].append("claim not established by any stated fact: {0}".format(t))
    return state


def preconditions_hold(skill, facts=(), env=None):
    st = prerequisites_state(skill, facts, env)
    return not st["contradicted"] and not st["unknown"]


def record_skill_result(skill_id, verified_ok, *, by="harness", scope="", note="", demote_after=2):
    """Record a verification of THIS skill version (its own check, not the caller's outcome) as
    an append-only OP. It can demote an active skill after `demote_after` failures; it can never
    change any other status, so a result computed against a stale read cannot undo a withdrawal
    or a demotion that landed in between. Returns the entry as it now stands."""
    cur = _latest(SKILLS).get(skill_id)
    if not cur:
        raise KeyError(skill_id)
    _append(SKILLS, {"id": skill_id, "op": "verify", "ok": bool(verified_ok),
                     "demote_after": int(demote_after), "at": _now(),
                     "entry": {"ok": bool(verified_ok), "by": by, "scope": scope, "note": note,
                               "at": _now()}})
    return _latest(SKILLS)[skill_id]


def record_skill_unknown(skill_id, reason, *, by="harness", scope=""):
    """Verification could not be attributed to the skill (dependency missing, unsupported
    environment, invalid check). Kept as history, counted as `unknown`, never a failure."""
    cur = _latest(SKILLS).get(skill_id)
    if not cur:
        raise KeyError(skill_id)
    return _tally(SKILLS, cur, unknown_verifications={reason[:80]: 1})


def revalidate_skill(skill_id, *, by, note=""):
    """A person (or a fresh verification) returns a DEMOTED skill to service, as an op that
    `_latest` applies only if the entry is still demoted when the row is folded in. The failure
    history stays. Raises if the entry is not demoted at the time of the call; a withdrawal that
    lands between the caller's read and this write is honoured by the fold, not by luck."""
    cur = _latest(SKILLS).get(skill_id)
    if not cur:
        raise KeyError(skill_id)
    if cur.get("status") != "demoted":
        raise ValueError("cannot revalidate a {0} skill; only a demoted one".format(cur.get("status")))
    _append(SKILLS, {"id": skill_id, "op": "revalidate", "at": _now(),
                     "revalidated": {"by": by, "note": note, "at": _now()}})
    return _latest(SKILLS)[skill_id]


def refresh_contract(skill_id, *, by="operator"):
    """Re-derive a skill's callable contract from the SAME bytes (hash re-checked) and the SAME
    stored verify, as a contract-only op. Nothing about what the skill is or was verified to do
    changes; only what the harness could read out of it. Refused if the bytes have changed --
    that is a new version."""
    cur = _latest(SKILLS).get(skill_id)
    if not cur:
        raise KeyError(skill_id)
    loc = Path(cur.get("location") or "")
    if not loc.is_file() or _sha(loc) != cur.get("sha256"):
        raise ValueError("bytes at {0} are missing or differ from the verified version; a changed "
                         "module is a new skill, not a refreshed contract".format(loc))
    contract = _module_contract(loc)
    contract["examples"] = _examples_from_oracle(cur.get("verify", ""), cur.get("module", loc.stem))
    sig = "; ".join("{0}({1})".format(e["name"], ", ".join(e["params"]))
                    for e in contract["entry_points"][:6])
    ex = "; ".join("{0} -> {1} [{2} -> {3}]".format(e["call"], e["returns"],
                                                     ",".join(e["arg_types"]), e["return_type"])
                   for e in contract["examples"][:4]) or "none recorded in the oracle"
    proc = re.sub(r"Entry points: .*?\. Verified examples \(argument types -> return type\): .*?\. Depends on:",
                  "Entry points: {0}. Verified examples (argument types -> return type): {1}. Depends on:".format(sig, ex),
                  cur.get("procedure", ""), count=1, flags=re.S)
    _append(SKILLS, {"id": skill_id, "op": "contract", "contract": contract, "procedure": proc,
                     "contract_refreshed": {"by": by, "at": _now()}, "at": _now()})
    return _latest(SKILLS)[skill_id]


def withdraw_skills_from_run(run_id, reason, *, by="harness"):
    """Withdraw every active skill promoted from `run_id`. Used when a challenge REFUTES the
    acceptance that skill was retained from (O5): a skill's reusable status must track the
    disposition of the result it came from, not stay 'active' after that result was refuted."""
    if DISABLED:
        return []
    out = []
    for sid, e in _latest(SKILLS).items():
        if e.get("status") == "active" and (e.get("source") or {}).get("run") == run_id:
            withdraw_skill(sid, reason, by=by)
            out.append(sid)
    return out


def withdraw_skill(skill_id, reason, *, by):
    cur = _latest(SKILLS).get(skill_id)
    if not cur:
        raise KeyError(skill_id)
    _append(SKILLS, {"id": skill_id, "op": "withdraw", "at": _now(),
                     "withdrawn": {"reason": reason, "by": by, "at": _now()}})
    return _latest(SKILLS)[skill_id]


# =================================================================================================
# the consumer seam: what a worker is handed, and what the harness stages for it
# =================================================================================================

def plan_skills(brief, tags=(), facts=(), env=None, max_skills=3):
    """Skills to offer for this work, with their prerequisite state. Returns
    [(skill, state, offered)] so the card can stage the offered ones and name the rest."""
    out = []
    if DISABLED:
        return out
    # Rank MORE candidates than will be offered, then let prerequisites decide: a highly ranked
    # skill whose prerequisites fail must not consume the slot of a lower-ranked one that is
    # actually usable (live: a composite `net` skill outranked `discount` and was then refused,
    # and `discount` was never considered).
    offered, refused = 0, 0
    modules = set()
    for sk in find_skills(brief, tags, k=max_skills * 3):
        if sk.get("module") in modules:
            continue                     # two accepted runs of the same module: offer it once
        modules.add(sk.get("module"))
        st = prerequisites_state(sk, facts, env)
        ok = not st["contradicted"] and not st["unknown"]
        if ok and offered < max_skills:
            out.append((sk, st, True)); offered += 1
        elif not ok and refused < max_skills:
            out.append((sk, st, False)); refused += 1
    return out


def composition_notes(skills):
    """Where one offered skill's verified return type differs from another's verified argument
    types, say so. A conversion is the caller's explicit job, under a stated contract."""
    notes = []
    for a in skills:
        for b in skills:
            if a is b:
                continue
            ra = {e["return_type"] for e in (a.get("contract") or {}).get("examples") or []}
            # The FIRST argument is the value a caller pipes in. Comparing against every
            # argument type hid the live mismatch: discount(amount: str, percent: int) shares
            # `int` with totals' return type through `percent`, and the note never fired.
            first = {e["arg_types"][0] for e in (b.get("contract") or {}).get("examples") or []
                     if e.get("arg_types")}
            # Only scalar-taking consumers: piping a value into a function whose first argument
            # is a list is not the mistake this note exists for, and six symmetric notes for
            # three skills buried the one that mattered (live, net-total-c3 card: 9.6 KB).
            first = {t for t in first if t in ("str", "int", "float")}
            if ra and first and not (ra & first):
                ex = ((b.get("contract") or {}).get("examples") or [{}])[0].get("call", "")
                notes.append("COMPOSITION: `{0}` returns {1}, but `{2}` takes {3} as its first "
                             "argument (verified example: {4}); do not pass one into the other "
                             "without an explicit, checked conversion, and do not invent a helper "
                             "for it.".format(a["module"], "/".join(sorted(ra)), b["module"],
                                              "/".join(sorted(first)), ex))
    return notes


def sources_for(brief, tags=(), facts=(), env=None, max_lessons=3, max_skills=3, plan=None):
    """Lines for a contract's brief, or [] when nothing applies. `plan` is a prior plan_skills()
    result so the card and the lines agree on what was offered."""
    lines = []
    if DISABLED:
        return lines
    for les in retrieve_lessons(brief, tags, k=max_lessons):
        label = "CORRECTION" if les.get("kind") == "correction" else "OBSERVATION"
        lines.append("{6} {0} (match {1}): {2} Applies when: {3}. Limits: {4} Evidence: {5}".format(
            les["id"], les["match"], les["text"],
            ", ".join(les.get("applies_when") or [])[:120], les.get("limits", ""),
            (les.get("source") or {}).get("evidence_ref"), label))
        note_use(les["id"])
    plan = plan if plan is not None else plan_skills(brief, tags, facts, env, max_skills)
    offered = [sk for sk, st, ok in plan if ok]
    for sk, st, ok in plan:
        if ok:
            lines.append("SKILL {0} v{1}: {2} Prerequisites checked by the harness: {3}. Verified "
                         "by: {4} (scope: {5}). On failure: {6}. Evidence: {7}".format(
                             sk["id"], sk["version"], sk["procedure"],
                             "; ".join(st["satisfied"]) or "none",
                             sk["verify"][:160], sk.get("verify_scope", ""),
                             sk.get("failure_behavior", ""),
                             (sk.get("source") or {}).get("evidence_ref")))
            note_use(sk["id"])
        else:
            lines.append("SKILL {0} v{1} considered and NOT offered: {2}".format(
                sk["id"], sk["version"], "; ".join(st["contradicted"] + st["unknown"])))
    lines.extend(composition_notes(offered))
    return lines
