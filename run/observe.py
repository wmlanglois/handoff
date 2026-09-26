"""Authorized observation: the harness reads what it is allowed to read, instead of asking.

LF-03. Twice the loop stalled because the architect could not see a module the failing assignment
had to call, and either parked a question asking a person to go and look or told a tools-disabled
worker to "open the file". Both are the harness declining to read something it was allowed to read.

Three read-only probes -- list, read, grep -- confined to ONE root the operator named
(`--deliverable-root`, or the run's own workspace). Nothing here writes, executes, or fetches.
A probe outside the root, or against a missing file, returns `ok: False` with the reason, never a
guess and never an exception: an inaccessible fact is a fact about the environment and is recorded
as such, so the architect's next decision is made knowing it is inaccessible.

The probe vocabulary is closed on purpose. Granting `python_run` or the browser here would turn
"obtain a fact the brief omitted" into "do whatever seems useful", which is the unrestricted
curiosity the handoff rules out.
"""
import re
import pathlib
from pathlib import Path

PROBES = ("list", "read", "grep")
MAX_CHARS = 4000
MAX_LINES = 120
TEXT_SUFFIXES = (".py", ".md", ".json", ".txt", ".toml", ".cfg", ".ini", ".yaml", ".yml",
                 ".csv", ".ps1", ".sh")


def _inside(root, p):
    """Containment is judged on the RESOLVED path. A junction or symlink inside the root that
    points outside it is outside it: `linked/memory.py` resolves to wherever the link goes."""
    try:
        q = Path(p).resolve()
    except (OSError, RuntimeError):
        return False
    return q == root or root in q.parents


def _safe(root, rel):
    p = (root / str(rel or "").strip().strip('"').lstrip("/\\")).resolve()
    if not _inside(root, p):
        raise PermissionError("target {0!r} is outside the authorized root".format(rel))
    return p


def _relative_pattern(root, pat):
    """A glob pattern relative to `root` (2026-09-26, observed live). The architect's INVESTIGATE
    passed an ABSOLUTE path; Path.glob() on Python 3.14 raises NotImplementedError for non-relative
    patterns, which escaped observe() and crashed the whole controller. An absolute pattern inside
    the root is made relative; one outside it is refused like any other read outside the root."""
    raw = str(pat or "").strip().strip('"')
    if ".." in raw.replace("\\", "/").split("/"):
        raise PermissionError("glob may not climb out of the root")
    if ".." in raw:
        raise PermissionError("glob may not climb out of the root")
    p = pathlib.PurePath(raw)
    if p.is_absolute() or p.drive or raw.startswith(("/", "\\")):
        anchor = []
        for part in p.parts:
            if any(ch in part for ch in "*?["):
                break
            anchor.append(part)
        fixed = Path(*anchor) if anchor else Path(p.anchor or "/")
        try:
            fixed_res = fixed.resolve()
        except (OSError, RuntimeError):
            raise PermissionError("target {0!r} is outside the authorized root".format(raw))
        if not _inside(root, fixed_res):
            raise PermissionError("target {0!r} is outside the authorized root".format(raw))
        rest = p.parts[len(anchor):]
        rel = fixed_res.relative_to(root).as_posix()
        pattern = "/".join(([rel] if rel not in ("", ".") else []) + list(rest))
        return pattern or "*"
    return raw.replace("\\", "/").lstrip("/") or "*"


def _candidates(root, paths, out):
    """Every glob / traversal candidate passes the SAME resolved-root check as a direct read.

    Reproduced 2026-09-20: a benign junction inside an authorized temp root pointed at repository
    source; `read linked/memory.py` was refused, `grep linked/*.py` returned the outside file.
    Direct reads resolved their target; glob candidates were trusted because glob had produced
    them. Skipped candidates are counted in `out["skipped_outside"]` so a reader can see that
    enumeration was narrowed rather than that nothing was there."""
    kept = []
    for c in paths:
        if _inside(root, c):
            kept.append(c)
        else:
            out["skipped_outside"] = int(out.get("skipped_outside", 0)) + 1
    return kept


def _grep(base, files, pattern, out, ref):
    pattern = str(pattern or "")
    if not pattern:
        out["reason"] = "grep needs a pattern"
        return out
    rx = re.compile(pattern)
    hits = []
    for f in files:
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8",
                                                 errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append("{0}:{1}: {2}".format(f.relative_to(base).as_posix(), i,
                                                      line.strip()[:160]))
                if len(hits) >= 40:
                    break
        except OSError:
            continue
        if len(hits) >= 40:
            break
    out.update(ok=True, ref=ref, text="\n".join(hits)[:MAX_CHARS] or "(no matches)")
    return out


def observe(root, probe):
    """Run one probe. `probe` is {"tool": list|read|grep, "target": <relative path or glob>,
    "pattern": <regex, grep only>, "start": <line, read only>}.

    Returns {"ok": bool, "tool", "target", "text", "ref", "reason"}. `ref` is the path that
    was read, so a downstream brief can cite where the fact came from."""
    if not isinstance(probe, dict):
        # A model-generated probe of the wrong shape is a recorded failure, not an exception in
        # the decision loop.
        return {"ok": False, "tool": "", "target": "", "text": "", "ref": "", "skipped_outside": 0,
                "malformed": True,
                "reason": "malformed probe: expected an object with tool/target, got {0}".format(
                    type(probe).__name__)}
    tool = str(probe.get("tool") or "").lower()
    target = str(probe.get("target") or "")
    out = {"ok": False, "tool": tool, "target": target, "text": "", "ref": "", "reason": "",
           "skipped_outside": 0, "malformed": False}
    if tool not in PROBES:
        out["reason"] = "probe {0!r} is not one of {1}".format(tool, list(PROBES))
        out["malformed"] = True
        return out
    if not root:
        out["reason"] = "no authorized observation root is configured for this run"
        return out
    base = Path(root).resolve()
    if not base.is_dir():
        out["reason"] = "authorized root {0} does not exist".format(base)
        return out
    try:
        if tool == "list":
            pat = target or "*"
            if pat.strip() in (".", "./", ""):        # "list the root" -- glob(".") is not a valid pattern
                pat = "*"
            pat = _relative_pattern(base, pat)
            files = sorted(str(x.relative_to(base)).replace("\\", "/")
                           for x in _candidates(base, base.glob(pat), out) if x.is_file())
            text = "\n".join(files)[:MAX_CHARS] or "(none)"
            if out["skipped_outside"]:
                text += "\n({0} entr{1} resolving outside the authorized root not listed)".format(
                    out["skipped_outside"], "y" if out["skipped_outside"] == 1 else "ies")
            out.update(ok=True, ref=str(base), text=text)
            return out
        if tool == "grep" and any(ch in target for ch in "*?["):
            # The first live INVESTIGATE asked to grep "**/*.py"; a glob is a natural target for
            # a grep and refusing it cost the run its one useful probe. Stay inside the root.
            files = [f for f in _candidates(base, base.glob(_relative_pattern(base, target)), out) if f.is_file()
                     and f.suffix.lower() in TEXT_SUFFIXES]
            return _grep(base, files, (probe or {}).get("pattern"), out, ref=str(base))
        p = _safe(base, target or ".")
        if not p.exists():
            out["reason"] = "{0} does not exist under the authorized root".format(target)
            return out
        if tool == "read":
            if p.is_dir():
                out["reason"] = "{0} is a directory; use list".format(target)
                return out
            if p.suffix.lower() not in TEXT_SUFFIXES:
                out["reason"] = "{0} is not a text file this probe reads".format(target)
                return out
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            try:
                a = max(1, int(probe.get("start") or 1))
            except (TypeError, ValueError):
                out["reason"] = "invalid read offset {0!r}: expected a line number".format(
                    probe.get("start"))
                out["malformed"] = True
                return out
            b = min(len(lines), a + MAX_LINES - 1)
            body = "\n".join("{0:5d}| {1}".format(i, lines[i - 1]) for i in range(a, b + 1))
            out.update(ok=True, ref=str(p), text=body[:MAX_CHARS])
            if b < len(lines):
                out["text"] += "\n... ({0} more lines)".format(len(lines) - b)
            return out
        files = [p] if p.is_file() else [f for f in _candidates(base, p.rglob("*"), out)
                                         if f.is_file() and f.suffix.lower() in TEXT_SUFFIXES]
        return _grep(base, files, (probe or {}).get("pattern"), out, ref=str(p))
    except PermissionError as e:
        out["reason"] = str(e)
        return out
    except (ValueError, NotImplementedError) as e:
        # a malformed glob/pattern (e.g. target ".") is a malformed INVOCATION, not an answer:
        # record it so the architect can correct the probe, never crash the run (O4 4B).
        out["reason"] = "malformed probe target {0!r}: {1}".format(target, e)
        out["malformed"] = True
        return out
    except (OSError, re.error) as e:
        out["reason"] = "{0}: {1}".format(type(e).__name__, e)
        return out
