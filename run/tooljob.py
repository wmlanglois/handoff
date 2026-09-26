"""Tool-using worker: runs a local model through the bundled tool loop against the
local tool-service (files/python_run), producing artifacts in a job workspace.
This is the worker step for a card marked "tools": true, so verified.py can gate real
spreadsheet/browser work through the same jury+judge loop.

An explicitly configured FLEET_DISPATCH_DIR may supply a separately managed runtime instead.

    python run/tooljob.py --worker cluster --ws fleet-tooljob-test "Use python_run to ..."

The bundled core keeps clean clones independent of a private dispatcher. Optional Office,
browser and vision adapters are not claimed as part of this core.
"""
import argparse, importlib.util, re, sys, threading
from pathlib import Path
import json
import os
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from fleet import WORKERS, TOOL_SERVICE, DEFAULT_WORKER, SettingsMissing, setting  # noqa
from log import logger  # noqa
from state_lock import exclusive_file


def load_agent():
    """Load the bundled agent or an explicitly configured compatible runtime."""
    root = setting("FLEET_DISPATCH_DIR", str(ROOT / "tool_runtime"))
    path = Path(root)
    if not (path / "agent.py").exists():
        raise SettingsMissing(
            f"FLEET_DISPATCH_DIR={path} does not contain agent.py.\n"
            f"  Remove the override to use the bundled core, or point it at a compatible runtime.\n"
            f"  See README.md -> Configure.")
    if path.resolve() != (ROOT / "tool_runtime").resolve() and str(path) not in sys.path:
        # A compatible external runtime may import its sibling modules. Its directory was
        # explicitly configured by the operator; the bundled core needs no path mutation.
        sys.path.insert(0, str(path))
    spec = importlib.util.spec_from_file_location("handoff_tool_agent", path / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Needed for tools mode to work and be diagnosable: a cut-off turn must not be executed or silently
#: end the job, stop reasons must be recorded, and a file larger than one turn must be buildable.
REQUIRED_TOOL_CAPABILITIES = ("length_recovery", "generation_evidence", "incremental_files")


def _request_profile_for(worker, w):
    """WORKER_REQUEST_PROFILES[worker] as a request fragment for the tool loop ({} when none)."""
    from generation import SAMPLING_KEYS, apply_request_profile, worker_request_profile
    prof = worker_request_profile(worker)
    if not prof:
        return {}
    base = {"temperature": 0.2}
    body = apply_request_profile(dict(base), prof, w.get("reasoning_style", "none"))
    omit = [k for k in SAMPLING_KEYS if k in prof and prof[k] is None]
    return {"request_overrides": body, "request_omit": omit}


def runtime_info(agent=None):
    """Which tool runtime will run tool jobs, and what it declares it can do. Never raises.
    Pass the already-loaded module to avoid loading an external runtime twice."""
    root = Path(setting("FLEET_DISPATCH_DIR", str(ROOT / "tool_runtime")))
    try:
        bundled = root.resolve() == (ROOT / "tool_runtime").resolve()
    except OSError:
        bundled = False
    info = {"kind": "bundled" if bundled else "external", "path": str(root)}
    try:
        caps = tuple(getattr(agent if agent is not None else load_agent(), "CAPABILITIES", ()) or ())
    except Exception as e:
        info.update(capabilities=[], missing=list(REQUIRED_TOOL_CAPABILITIES), error=repr(e)[:200])
        return info
    info.update(capabilities=sorted(caps),
                missing=[c for c in REQUIRED_TOOL_CAPABILITIES if c not in caps])
    return info


def workspace_dir(workspace_id):
    """Where the configured tool-service writes this job's artifacts."""
    root = setting("FLEET_DISPATCH_DIR", str(ROOT / "tool_runtime"))
    return (ROOT / "runs" / "tool-service" / "jobs" / workspace_id
            if Path(root).resolve() == (ROOT / "tool_runtime").resolve()
            else Path(root) / "tool-service" / "jobs" / workspace_id)

def _lineage_path(ckpt_dir):
    """The per-workspace record of which LOGICAL execution each attempt checkpoint belongs to.
    Lives beside the attempt-*.json files, keyed by execution_id -> lineage token."""
    return Path(ckpt_dir) / "lineage.json"


def _lineage_map(ckpt_dir):
    try:
        data = json.loads(_lineage_path(ckpt_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _lineage_dir(token):
    """Directory segment for one logical execution. A new token never shares attempt files with
    an older one, so a reused workspace name cannot reopen the previous run's transcript."""
    return "".join(c if c in _WS_SAFE else "-" for c in str(token))[:64]


def _record_lineage(ckpt_dir, execution_id, token):
    """Stamp this attempt's checkpoint as part of logical-execution `token`. Idempotent. A no-op
    when no token is supplied. An execution_id already stamped with a DIFFERENT token is left
    alone: reassigning it would make the previous run's messages match the new execution."""
    if not token:
        return
    d = Path(ckpt_dir)
    data = _lineage_map(d)
    existing = data.get(execution_id)
    if existing:
        return
    data[execution_id] = token
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = _lineage_path(d).with_name("lineage.json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(_lineage_path(d))
    except OSError:
        pass


def _ckpt_path(workspace_id, attempt, lineage=None):
    """Durable home for a tool conversation, OUTSIDE the tool workspace (so it never appears in the
    workspace file listing the worker/oracle see). Keyed by attempt so a crash-restart of the SAME
    attempt resumes, while a genuinely new attempt starts its own conversation. When a lineage
    token is present the directory is that token, so a later logical run of the same workspace
    name does not open the previous run's attempt files."""
    safe = "".join(c if c in _WS_SAFE else "-" for c in str(workspace_id))
    base = ROOT / "runs" / "tool-ckpt" / safe
    if lineage:
        base = base / _lineage_dir(lineage)
    return base / "attempt-{0}.json".format(attempt)


_WS_SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")


class CheckpointRecoveryRequired(RuntimeError):
    """Raised when restart safety cannot be honoured: a started attempt lost or corrupted its
    checkpoint, or a checkpoint could not be published. Fail closed rather than run without it."""
    pass


def _atomic_checkpoint(path, state):
    import uuid
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".{0}.{1}.tmp".format(path.name, uuid.uuid4().hex))
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _load_or_start_checkpoint(path):
    path = Path(path)
    started = path.with_suffix(path.suffix + ".started")
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with started.open("x", encoding="utf-8") as fh:
            fh.write("attempt initialized\n")
            fh.flush()
            os.fsync(fh.fileno())
        new_attempt = True
    except FileExistsError:
        new_attempt = False

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        if not new_attempt:
            raise CheckpointRecoveryRequired(
                "Started attempt lost its checkpoint: {0}".format(path)) from exc
        state = {}
        _atomic_checkpoint(path, state)
    except (OSError, ValueError) as exc:
        raise CheckpointRecoveryRequired(
            "Unreadable checkpoint; refusing fresh execution: {0}".format(path)) from exc

    if not isinstance(state, dict):
        raise CheckpointRecoveryRequired("Invalid checkpoint structure: {0}".format(path))

    for saved in state.values():
        if (
            not isinstance(saved, dict)
            or not isinstance(saved.get("messages"), list)
            or type(saved.get("rounds")) is not int
            or saved["rounds"] < 0
        ):
            raise CheckpointRecoveryRequired("Invalid conversation checkpoint: {0}".format(path))
    return state


class _Dispatch:
    """Minimal shim providing exactly what agent.run() needs from a dispatcher.

    CONTINUITY. `_ck` used to be in-memory only, so a process that died mid tool-conversation lost
    the whole thread; a re-dispatch started over and RE-EXECUTED tool calls the dead process had
    already completed (repeated side effects). When `ckpt_path` is given the checkpoint is also
    written to disk and reloaded here, so agent.run resumes from it -- its own pending/done logic
    then replays only UNFINISHED tool calls, never completed ones."""
    def __init__(self, emit, ckpt_path=None):
        self.config = {
            "workers": {k: {"url": v["url"], "model": v["model"], "slots": 2} for k, v in WORKERS.items()},
            "tools": {"url": TOOL_SERVICE["url"], "token_file": TOOL_SERVICE["token_file"]},
        }
        self.condition = threading.Condition()
        self.active = {k: 0 for k in WORKERS}
        self._ck = {}
        self._ckpt_path = Path(ckpt_path) if ckpt_path else None
        self._emit = emit
        if self._ckpt_path is not None:
            self._ck = _load_or_start_checkpoint(self._ckpt_path)
    def checkpoint(self, job_id, data=None):
        if data is None:
            return self._ck.get(job_id)
        updated = dict(self._ck)
        updated[job_id] = data
        if self._ckpt_path is not None:
            try:
                _atomic_checkpoint(self._ckpt_path, updated)
            except OSError as exc:
                raise CheckpointRecoveryRequired(
                    "Checkpoint publication failed; execution stopped") from exc
        self._ck = updated

    def event(self, job_id, kind, detail):
        if isinstance(detail, dict):
            self._emit(kind, **{k: str(v)[:100] for k, v in list(detail.items())[:4]})
        else:
            self._emit(kind, detail=str(detail)[:150])

def _tool_call(path, payload, timeout=60):
    """One authenticated call to the tool-service (same contract agent.py uses)."""
    import urllib.request
    tok = Path(TOOL_SERVICE["token_file"]).read_text(encoding="utf-8").strip()
    req = urllib.request.Request(TOOL_SERVICE["url"] + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _stage_manifest(files):
    """Normalise a staging list into manifest entries {dest, text, data}. Accepts the legacy
    `(name, text)` tuple (text file) or a dict carrying `text=` (text) or `data=<bytes>` (binary)."""
    out = []
    for f in files:
        if isinstance(f, (tuple, list)):
            out.append({"dest": f[0], "text": f[1], "data": None})
        else:
            out.append({"dest": f["dest"], "text": f.get("text"), "data": f.get("data")})
    return out


def _stage_id(operation, destination, phase):
    import hashlib
    value = json.dumps([operation, destination, phase], separators=(",", ":")).encode("utf-8")
    return "stage-" + hashlib.sha256(value).hexdigest()


def _dest_sha256(workspace_id, dest, request_id):
    """SHA-256 of `dest` AS IT LANDED, computed in the workspace via python_run. Content integrity
    for binary (and text), not a size or utf-8-read check. Distinct request_id per phase so the
    tool-service call-id cache never returns a stale earlier read."""
    code = (
        "import hashlib\n"
        "print(hashlib.sha256(open({0!r}, 'rb').read()).hexdigest())".format(dest)
    )
    response = _tool_call("/call", {
        "job_id": workspace_id,
        "call_id": request_id,
        "name": "python_run",
        "arguments": {"code": code},
    })
    result = response.get("result") or {}
    if not response.get("ok") or result.get("exit_code") != 0:
        return None
    lines = (result.get("stdout") or "").strip().splitlines()
    return lines[-1].strip() if lines else None


def tool_service_available():
    """True only when the configured tool service's token file is present.
    Availability is never assumed: an unconfigured or unreachable service must yield a truthful
    'unavailable'/unknown outcome, not a silent pass or fail."""
    try:
        url = TOOL_SERVICE.get("url")
        tf = TOOL_SERVICE.get("token_file")
        return bool(url) and bool(tf) and Path(tf).is_file()
    except Exception:
        return False


def verify_in_tool_service(workspace_id, stage_files, verify_script, emit=None):
    """Run a skill/module behavioral check in the ACTUAL tool-service workspace, through the SAME
    stage + python_run path production uses (identical call contract to _dest_sha256). A local
    subprocess shadow cannot establish that a module imports and runs in the tool ENVIRONMENT the
    worker actually uses; only this can. Returns (status, msg):
        'ok'           the check printed VERIFY_OK with exit_code 0 in the tool service
        'defective'    the check RAN there and failed (bad exit code / no VERIFY_OK)
        'unavailable'  the tool service is not configured/reachable, or staging/the call could not
                       complete -> truthful UNKNOWN about remote usability, never a pass or a fail
    The caller must treat 'unavailable' as neither success nor failure of the skill."""
    if not tool_service_available():
        return "unavailable", "tool service not configured/reachable"
    import uuid
    try:
        staged = stage_into_workspace(workspace_id, stage_files, emit)
        if any(not ok for ok in staged.values()):
            return "unavailable", "could not stage the check's material into the tool workspace"
        response = _tool_call("/call", {
            "job_id": workspace_id,
            "call_id": "skill-verify-" + uuid.uuid4().hex,
            "name": "python_run",
            "arguments": {"code": verify_script},
        })
    except Exception as exc:
        return "unavailable", "tool service call failed: {0}".format(str(exc)[:200])
    result = response.get("result") or {}
    combined = (result.get("stdout") or "") + (result.get("stderr") or "")
    if response.get("ok") and result.get("exit_code") == 0 and "VERIFY_OK" in combined:
        return "ok", combined[-260:]
    return "defective", combined[-260:]


def stage_into_workspace(workspace_id, files, emit=None, operation_id=None):
    import base64
    import hashlib
    import uuid

    # Reuse operation_id only when RETRYING this same staging operation; a new observation/staging
    # operation receives a new id, so pre/post hash reads are never served from a stale call cache.
    operation = operation_id or uuid.uuid4().hex
    result = {}

    for entry in _stage_manifest(files):
        dest = entry["dest"]
        text = entry.get("text")
        data = entry.get("data")
        raw = text.encode("utf-8") if text is not None else data
        if not isinstance(raw, bytes):
            raise ValueError("No bytes supplied for {0}".format(dest))

        expected = hashlib.sha256(raw).hexdigest()
        ok = False
        try:
            before = _dest_sha256(workspace_id, dest, _stage_id(operation, dest, "pre"))
            if before == expected:
                ok = True
            else:
                if text is not None:
                    tool = "files"
                    arguments = {"action": "write", "path": dest, "content": text}
                else:
                    tool = "_upload"
                    arguments = {"path": dest, "base64": base64.b64encode(raw).decode("ascii")}
                uploaded = _tool_call("/call", {
                    "job_id": workspace_id,
                    "call_id": _stage_id(operation, dest, "write"),
                    "name": tool,
                    "arguments": arguments,
                })
                if uploaded.get("ok"):
                    after = _dest_sha256(workspace_id, dest, _stage_id(operation, dest, "post"))
                    ok = after == expected
        except Exception as exc:
            if emit:
                emit("stage_error", file=dest, error=str(exc)[:160])
        result[dest] = ok

    if emit:
        emit("staged_into_tool_workspace",
             ok=", ".join(k for k, value in result.items() if value),
             failed=", ".join(k for k, value in result.items() if not value))
    return result


def _workspace_lock_path(workspace_id):
    """One lock for the tool workspace, shared by every lineage that uses that name.
    Lineage-specific checkpoint directories must not each grow their own lock: two executions
    writing the same workspace would otherwise run together."""
    safe = "".join(c if c in _WS_SAFE else "-" for c in str(workspace_id))
    return ROOT / "runs" / "tool-ckpt" / safe / ".execution-lock"


def run_tooljob(worker, brief, workspace_id, max_rounds=16, max_tokens=1400, stage=None,
                artifact=None, attempt=0, lineage=None, branch=False, draft_review=None):
    # Serialise the WHOLE invocation for this workspace, not just checkpoint writes: two runners of
    # the same tool workspace must never interleave, even when their checkpoints live in different
    # lineage directories. Reentrant within a process.
    with exclusive_file(_workspace_lock_path(workspace_id), timeout=1.0):
        return _run_tooljob_locked(worker, brief, workspace_id, max_rounds=max_rounds,
                                   max_tokens=max_tokens, stage=stage, artifact=artifact,
                                   attempt=attempt, lineage=lineage, branch=branch, draft_review=draft_review)


def _run_tooljob_locked(worker, brief, workspace_id, max_rounds=16, max_tokens=1400, stage=None,
                        artifact=None, attempt=0, lineage=None, branch=False, draft_review=None):
    import hashlib
    agent = load_agent()                      # fails here, by name, if the dependency is unset
    emit = logger(f"tooljob-{workspace_id}")
    _rt = runtime_info(agent)
    emit("tool_runtime", kind=_rt["kind"], path=_rt["path"], capabilities=_rt.get("capabilities"),
         missing=_rt.get("missing"))
    ckpt = _ckpt_path(workspace_id, attempt, lineage)
    d = _Dispatch(emit, ckpt_path=ckpt)
    # A distinct external execution identity per attempt: the conversation for this attempt is read
    # from d._ck under THIS id, so attempts never cross-read each other's tool history.
    execution_id = "attempt-" + hashlib.sha256(
        json.dumps([workspace_id, attempt]).encode("utf-8")).hexdigest()[:40]
    # Stamp this attempt with its LOGICAL-execution lineage before anything runs. execution_id is a
    # pure function of (workspace_id, attempt), so a fresh logical run that reuses the tool-service
    # workspace NAME produces the SAME execution_ids and shares this directory with stale attempts;
    # the lineage token is what lets the cross-attempt receipt search tell one execution's attempts
    # from another's rather than trusting the reused name.
    _record_lineage(ckpt.parent, execution_id, lineage)
    if d.checkpoint(execution_id):
        emit("resumed_tool_conversation", workspace=workspace_id, attempt=attempt)
    w = WORKERS[worker]
    ws = workspace_dir(workspace_id)
    if stage:
        staged = stage_into_workspace(workspace_id, stage, emit)   # material the worker can read/import
        failed = [n for n, ok in staged.items() if not ok]
        if failed:
            emit("staging_failed", files=", ".join(failed))
            emit.close()
            raise RuntimeError("required project material failed to stage into the tool workspace: "
                               + ", ".join(failed))
    from generation import worker_output_limit
    max_tokens = worker_output_limit(worker, max_tokens)
    job = {"prompt": brief, "workspace_id": workspace_id, "max_rounds": max_rounds, "max_tokens": max_tokens}
    if draft_review:
        if _rt["kind"] == "bundled":
            def review_draft():
                text, authored = _receipted_artifact(d, execution_id, workspace_id, artifact, lineage=lineage)
                if text is None:
                    emit("skeptic_draft_unavailable", artifact=artifact, reason="no matching artifact receipt")
                    return ""
                emit("skeptic_draft", attempt=attempt, artifact=artifact, evidence_root=str(ws),
                     sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), authored=authored)
                return draft_review(str(ws), text)
            job["draft_review"] = review_draft
        else:
            emit("skeptic_draft_unavailable", reason="external runtime lacks bundled coaching hook")
    # Logical-job continuity across OUTER attempts (N -> N+1) and process restart: by default a
    # retry continues this execution's own prior work (bounded, read-only, no side-effect replay);
    # a fresh conversation is taken ONLY when the caller explicitly branches, never as an accidental
    # default of retrying.
    # Carry earlier same-lineage attempts even when this process's loop index is 1 (a restart
    # re-enters at the first round). A fresh lineage has no other checkpoints, so the digest is
    # empty. An explicit branch is the only way to start over on purpose.
    if lineage and not branch:
        _ctx = _prior_attempt_context(workspace_id, execution_id, lineage)
        if _ctx:
            job["prompt"] = _ctx + "\n\n" + job["prompt"]
            emit("prior_attempt_context_carried", attempt=attempt, bytes=len(_ctx))
    elif lineage and branch:
        emit("fresh_conversation_branch", attempt=attempt)
    if artifact:
        # The delivery is an explicit files.write receipt, NOT a side effect a later read might see.
        job["prompt"] += (
            "\nFinal delivery: produce {0!r} with a successful receipted files operation. "
            "Use files.write, or files.append/files.edit ONLY if the service advertises them. "
            "For large files, write a small section then use supported guarded edits; use the "
            "latest returned sha256 as expected_sha256. The last successful mutation receipts "
            "the whole resulting file; do not retransmit it just to deliver. Run relevant checks. "
            "python_run alone is not a delivery receipt. Do not change the artifact after its "
            "final receipted mutation.".format(artifact))
    # Stop-reason evidence must be recorded whether the loop RETURNS or RAISES. The length-stop and
    # context-capacity exits raise, and those are exactly the cases the architect needs to see (ADJUST
    # is refused without recorded length-limited turns); emitting only on return lost all of it,
    # observed live. Only this call's turns are emitted: a resumed conversation carries earlier ones.
    _seen = len(((d.checkpoint(execution_id) or {}).get("generations")) or [])

    def _emit_generations(generations):
        for index, evidence in enumerate(list(generations or [])[_seen:], start=_seen):
            emit("generation", index=index, evidence=evidence)

    agent_worker = {"url": w["url"], "model": w["model"], "ctx": w.get("ctx", 8192), "slots": 2}
    if w.get("api_key_env"):
        if Path(agent.__file__).resolve().parent != ROOT / "tool_runtime":
            raise RuntimeError("authenticated gateway workers require the bundled tool runtime")
        agent_worker["api_key_env"] = w["api_key_env"]
    agent_worker.update(_request_profile_for(worker, w))
    from generation import request_timeout
    agent_worker["request_timeout"] = request_timeout(worker, int(max_tokens or 1400))
    if agent_worker.get("request_overrides") or agent_worker.get("request_omit"):
        emit("request_profile", overrides=agent_worker.get("request_overrides"),
             omit=agent_worker.get("request_omit"))
    try:
        res = agent.run(d, execution_id, job, agent_worker)
    except (RuntimeError, OSError) as exc:   # OSError covers a socket/read TimeoutError
        _emit_generations((d.checkpoint(execution_id) or {}).get("generations"))
        emit("skeptic_coaching_outcome", coaching=(d.checkpoint(execution_id) or {}).get("coaching"),
             outcome="tool_loop_stopped")
        emit("tool_loop_stop", reason=str(exc)[:200])
        raise
    _emit_generations(res.get("generations"))
    emit("skeptic_coaching_outcome", coaching=(d.checkpoint(execution_id) or {}).get("coaching"),
         outcome="returned_to_verifier")
    if res.get("loop_stop"):
        emit("tool_loop_stop", reason=res["loop_stop"])
    content = (res["choices"][0]["message"].get("content") or "").strip()
    # PROVENANCE: bind ONLY to a service write RECEIPT -- a successful files.write whose returned
    # sha256 matches the artifact's current bytes. No inference from "a file changed" or "some tool
    # ran". A python_run write is deliberately NOT a receipt (see the delivery instruction above).
    artifact_content, _authored = (
        _receipted_artifact(d, execution_id, workspace_id, artifact, lineage=lineage)
        if artifact else (None, None)
    )
    # attempt-specific attribution: the accepting attempt vs the attempt that actually wrote the
    # (byte-identical) receipted deliverable. Not the same as crediting the current attempt with the
    # write -- it records the real authoring attempt.
    same_attempt = (_authored == execution_id)
    how = ("service-write-receipt" if same_attempt else "service-write-receipt-prior-attempt") \
        if artifact_content is not None else "none"
    if artifact_content is not None:
        import call as _call
        _call.external_capture(worker, w["model"], artifact_content)
        if not same_attempt:
            emit("receipt_from_prior_attempt", authored=str(_authored), accepting=str(execution_id),
                 artifact=artifact)
    elif artifact:
        emit("provenance_none",
             reason="No matching service write receipt for final artifact bytes")
    emit("tooljob-done", chars=len(content), ws=str(ws), artifact_bytes=len(artifact_content or ""), attribution=how)
    if draft_review:
        emit("skeptic_delivery", attempt=attempt, artifact=artifact,
             sha256=hashlib.sha256(artifact_content.encode("utf-8")).hexdigest() if artifact_content is not None else None,
             coaching=(d.checkpoint(execution_id) or {}).get("coaching"), attribution=how)
    emit.close()
    return {"content": content, "workspace": str(ws), "artifact_content": artifact_content, "attribution": how,
            "coaching": (d.checkpoint(execution_id) or {}).get("coaching")}


def _write_receipt_shas(messages, target):
    """Every sha256 a SUCCESSFUL files.write receipt reported for `target` in one attempt's
    conversation, in order (last is the final write of that attempt)."""
    requests, shas = {}, []
    for message in messages or []:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                requests[call.get("id")] = call.get("function") or {}
        elif message.get("role") == "tool":
            function = requests.get(message.get("tool_call_id"), {})
            if function.get("name") != "files":
                continue
            try:
                args = json.loads(function.get("arguments") or "{}")
                response = json.loads(message.get("content") or "{}")
            except (TypeError, ValueError):
                continue
            if (args.get("action") not in ("write", "edit", "append") or _norm_rel(args.get("path")) != target
                    or not response.get("ok")):
                continue
            receipt = response.get("result") or {}
            if args.get('action') != 'write' and (
                    receipt.get('operation') != args.get('action') or not args.get('expected_sha256')
                    or receipt.get('before_sha256') != args.get('expected_sha256')):
                continue
            if _norm_rel(receipt.get("path")) == target and receipt.get("sha256"):
                shas.append(receipt.get("sha256"))
    return shas


def _all_write_receipts(messages):
    """(path, sha256) for every SUCCESSFUL files.write in one attempt's conversation, in order."""
    requests, out = {}, []
    for message in messages or []:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                requests[call.get("id")] = call.get("function") or {}
        elif message.get("role") == "tool":
            fn = requests.get(message.get("tool_call_id"), {})
            if fn.get("name") != "files":
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
                resp = json.loads(message.get("content") or "{}")
            except (TypeError, ValueError):
                continue
            if args.get("action") not in ("write", "edit", "append") or not resp.get("ok"):
                continue
            receipt = resp.get("result") or {}
            if args.get('action') != 'write' and (
                    receipt.get('operation') != args.get('action') or not args.get('expected_sha256')
                    or receipt.get('before_sha256') != args.get('expected_sha256')):
                continue
            if receipt.get("path"):
                out.append((_norm_rel(receipt.get("path")), receipt.get("sha256")))
    return out


def _snip(text, limit=400):
    return " ".join(str(text or "").split())[:limit]


def _attempt_findings(messages):
    """Read-only notes from one attempt: discoveries, failures, tool feedback, questions, next work.
    Does not treat a write receipt as proof the file is finished."""
    requests = {}
    discoveries, failures, feedback, questions = [], [], [], []
    for message in messages or []:
        if message.get("role") == "assistant":
            text = message.get("content") if isinstance(message.get("content"), str) else ""
            for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
                if "?" in sentence and sentence.strip():
                    questions.append(_snip(sentence, 240))
            for call in message.get("tool_calls") or []:
                requests[call.get("id")] = call.get("function") or {}
        elif message.get("role") == "tool":
            fn = requests.get(message.get("tool_call_id"), {})
            name = fn.get("name") or "tool"
            raw = message.get("content") or ""
            try:
                body = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (TypeError, ValueError):
                body = {"content": _snip(raw, 240)}
            if not isinstance(body, dict):
                body = {"content": _snip(body, 240)}
            feedback.append("{0}: {1}".format(name, _snip(json.dumps(body), 240)))
            if body.get("ok") is False or body.get("error"):
                failures.append("{0} failed: {1}".format(name, _snip(body.get("error") or body, 240)))
            result = body.get("result") if isinstance(body.get("result"), dict) else {}
            stdout = result.get("stdout") or body.get("stdout") or ""
            if name == "python_run" and stdout:
                discoveries.append("python_run: " + _snip(stdout, 300))
            if name == "files" and result.get("content"):
                discoveries.append("read {0}: {1}".format(result.get("path") or "?", _snip(result.get("content"), 200)))
    next_work = ""
    for message in reversed(messages or []):
        if message.get("role") == "assistant" and isinstance(message.get("content"), str) and message["content"].strip():
            next_work = _snip(message["content"], 800)
            break
    return discoveries, failures, feedback, questions, next_work


def _prior_attempt_context(workspace_id, execution_id, lineage, max_chars=8000):
    """A bounded, read-only digest of earlier attempts of THIS logical execution.

    Carries discoveries, failed approaches, tool feedback, outstanding questions, and the previous
    attempt's own statement of next work. A write receipt is listed as a write, not as proof the
    file is finished. Original checkpoint files are read and never modified."""
    if not lineage:
        return ""
    ckpt_dir = _ckpt_path(workspace_id, 1, lineage).parent
    if not ckpt_dir.is_dir():
        return ""
    lin_map = _lineage_map(ckpt_dir)
    writes, order = {}, []
    discoveries, failures, feedback, questions, next_work = [], [], [], [], ""

    def _attempt_num(path):
        try:
            return int(path.stem.split("-", 1)[1])
        except (IndexError, ValueError):
            return 10 ** 9

    for f in sorted(ckpt_dir.glob("attempt-*.json"), key=_attempt_num):
        try:
            ck = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(ck, dict):
            continue
        for exec_key, blk in ck.items():
            if exec_key == execution_id or not isinstance(blk, dict):
                continue
            if lin_map.get(exec_key) != lineage:
                continue
            msgs = blk.get("messages") or []
            for path, sha in _all_write_receipts(msgs):
                if path not in writes:
                    order.append(path)
                writes[path] = sha
            d, fail, fb, qs, nxt = _attempt_findings(msgs)
            discoveries.extend(d)
            failures.extend(fail)
            feedback.extend(fb)
            questions.extend(qs)
            if nxt:
                next_work = nxt
    if not (writes or discoveries or failures or feedback or questions or next_work):
        return ""
    lines = [
        "PRIOR ATTEMPT CONTEXT -- continuation of this same execution.",
        "Reconstructed from the original attempt records, which are left unchanged.",
        "A file listed here was written; that is not a claim it is finished or correct.",
    ]
    if order:
        lines.append("Files written:")
        for path in order:
            lines.append("  - {0} (sha256 {1})".format(path, (writes[path] or "")[:12]))
    if discoveries:
        lines.append("Discoveries:")
        lines.extend("  - " + item for item in discoveries[-8:])
    if failures:
        lines.append("Failed approaches:")
        lines.extend("  - " + item for item in failures[-8:])
    if feedback:
        lines.append("Tool feedback:")
        lines.extend("  - " + item for item in feedback[-8:])
    if questions:
        lines.append("Outstanding questions:")
        lines.extend("  - " + item for item in questions[-8:])
    if next_work:
        lines.append("Next work stated by the previous attempt:")
        lines.append("  " + next_work)
    return "\n".join(lines)[:max_chars]


def _receipted_artifact(d, execution_id, workspace_id, artifact, lineage=None):
    """(bytes-as-str, authoring_attempt) or (None, None). The artifact is bound ONLY to a genuine
    service write receipt whose sha256 equals the artifact's CURRENT bytes -- unreceipted bytes are
    still refused. The receipt may come from the CURRENT attempt or, when a tool-round limit split
    one logical job across outer attempts and the deliverable was written in an earlier attempt and
    only re-read/re-verified later, from another attempt of the SAME job (same tool-ckpt lineage).
    This binds to the REAL receipt and records WHICH attempt authored it; it never fabricates a
    receipt, never accepts bytes no receipt covers, and re-runs no tool call (no side-effect replay).
    The demonstrated defect it fixes: attempt 3 wrote transcript.py (receipt sha X == on-disk), the
    accepting attempt 5 only read it, and a single-attempt lookup found no receipt -> false refusal."""
    import hashlib
    target = _norm_rel(artifact)
    root = workspace_dir(workspace_id).resolve()
    path = (root / artifact).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Artifact escapes workspace")
    try:
        raw = path.read_bytes()
    except OSError:
        return None, None
    on_disk = hashlib.sha256(raw).hexdigest()

    # 1) the current attempt's own write receipt for these exact bytes (the common case).
    if on_disk in _write_receipt_shas((d._ck.get(execution_id) or {}).get("messages") or [], target):
        return raw.decode("utf-8"), execution_id
    # 2) otherwise a write receipt for these EXACT bytes from another attempt of the SAME LOGICAL
    #    EXECUTION -- the file was carried on the stable tool workspace and only re-verified in the
    #    accepting attempt. Restricted to attempts stamped with the CURRENT execution's lineage
    #    token: a matching sha in a checkpoint that merely reused this workspace NAME (a stale
    #    attempt from a prior run, or an execution_id collision) is NOT this execution's authorship
    #    and must not bind. With no current lineage token there is nothing to establish sameness, so
    #    cross-attempt binding is refused outright (the current attempt's own receipt still binds).
    if not lineage:
        return None, None
    ckpt_dir = _ckpt_path(workspace_id, 1, lineage).parent
    if ckpt_dir.is_dir():
        lin_map = _lineage_map(ckpt_dir)
        for f in sorted(ckpt_dir.glob("attempt-*.json")):
            try:
                ck = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(ck, dict):
                continue
            for exec_key, block in ck.items():
                if exec_key == execution_id or not isinstance(block, dict):
                    continue
                if lin_map.get(exec_key) != lineage:
                    continue                       # same name, different (or unknown) logical execution
                if on_disk in _write_receipt_shas(block.get("messages") or [], target):
                    return raw.decode("utf-8"), exec_key
    return None, None


def _norm_rel(p):
    """A workspace-relative path in canonical form: forward slashes, no leading `./`. So a write to
    `sub/discount.py` is NOT the same artifact as `discount.py` -- basename matching would conflate
    them and credit the wrong file."""
    s = str(p or "").replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return os.path.normpath(s).replace("\\", "/")


def _result_ok(content):
    """Did the tool-service report this call succeeded? The result message's content is the JSON the
    service returned (`{"ok": ...}`), or a large-result envelope that carries the same `ok`."""
    try:
        return bool(json.loads(content).get("ok"))
    except (ValueError, TypeError, AttributeError):
        return False


def _tool_results(d, workspace_id):
    """tool_call_id -> succeeded?, read from the `role:'tool'` result messages agent.py appends."""
    ck = getattr(d, "_ck", {}) or {}
    msgs = (ck.get(workspace_id) or {}).get("messages") or []
    return {m.get("tool_call_id"): _result_ok(m.get("content"))
            for m in msgs if m.get("role") == "tool" and m.get("tool_call_id")}


def _successful_writes(d, workspace_id, artifact):
    """Content of each SUCCESSFUL worker write to EXACTLY `artifact` this attempt, in order.

    Two tightenings over the old extractor, both to stop crediting the wrong bytes:
      * a write counts only if its matching tool RESULT reported `ok` -- an assistant REQUEST to
        write is not evidence the write happened (the service may have refused it);
      * the path is matched canonically, so `other/discount.py` does not satisfy `discount.py`.
    """
    target = _norm_rel(artifact)
    ok_by_id = _tool_results(d, workspace_id)
    ck = getattr(d, "_ck", {}) or {}
    msgs = (ck.get(workspace_id) or {}).get("messages") or []
    writes = []
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            fn = (tc.get("function") or {})
            if fn.get("name") not in ("files", "_upload"):
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (ValueError, TypeError):
                continue
            if args.get("action") not in (None, "write") or args.get("content") is None:
                continue
            if _norm_rel(args.get("path") or "") != target:
                continue
            if not ok_by_id.get(tc.get("id")):        # require the write to have SUCCEEDED
                continue
            writes.append(args["content"])
    return writes


def _write_capable_success(d, workspace_id):
    """Did the worker run a WRITE-CAPABLE tool (python_run) successfully this attempt? A file that
    merely changed on disk is attributed to the worker only via a tool that could plausibly have
    written it. A successful READ-ONLY or unrelated call (files list/read, pdf_read, excel read, ...)
    is NOT such evidence, so it must not prop up change-based attribution."""
    ok_by_id = _tool_results(d, workspace_id)
    ck = getattr(d, "_ck", {}) or {}
    msgs = (ck.get(workspace_id) or {}).get("messages") or []
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            if fn.get("name") == "python_run" and ok_by_id.get(tc.get("id")):
                return True
    return False


def _artifact_write_from_transcript(d, workspace_id, artifact):
    """The last SUCCESSFUL worker write to `artifact` this attempt, or None. Kept as a thin wrapper
    over `_successful_writes` for callers that want just the final bytes."""
    writes = _successful_writes(d, workspace_id, artifact)
    return writes[-1] if writes else None

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("brief")
    ap.add_argument("--worker", default=DEFAULT_WORKER)
    ap.add_argument("--ws", default="fleet-tooljob-test")
    ap.add_argument("--max-rounds", type=int, default=16)
    a = ap.parse_args()
    r = run_tooljob(a.worker, a.brief, a.ws, a.max_rounds)
    print("\nWORKSPACE:", r["workspace"])
    print("FINAL:", r["content"][:800])
