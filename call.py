"""One call path for the whole fleet. body_for() is the backend filter: it adapts the request to a
worker's compute backend so callers never hardcode per-worker quirks. This is the one-size-fits-all
entry; localjob, skeptic, preflight and the runner all go through chat().

Three guards live here so every caller gets them for free:
  - context-overflow guard: trims/【raises before a request exceeds the worker's ctx window (the mlx
    cluster silently hangs at 0% CPU on over-long prompts -- upstream #1493).
  - prefill lock (cluster): the mlx server has 2 decode slots but 1 prefill lane; a request holds a
    cross-process lock across its prefill stage (released on the first streamed token) so two prefills
    never hit insert_segments at once, while still allowing 2 concurrent decodes.
  - grammar passthrough: a GBNF grammar / json_schema can be forced on llama.cpp so structured output
    (e.g. a jury tool call) can't be malformed.
"""
import hashlib, json, sys, threading, time, urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fleet import WORKERS  # noqa
import jobs  # noqa


# =================================================================================================
# PROVENANCE
# =================================================================================================
# DEFECT B2 (internal/docs/DISPOSITION.md), and the deepest hole in the acceptance chain: evidence.seal
# took `worker_output` as an UNVERIFIED CALLER ASSERTION. Hand it the same bytes as output and as
# artifact and it issued a clean record -- for a run that never called a worker at all. It cannot
# be fixed inside evidence.py, because by the time seal() runs, "what the worker returned" is just
# another argument. It has to be captured HERE, at the one place the response actually arrives.
#
# So every completed call records a Capture: the exact response bytes, hashed, alongside who
# produced them. A Capture is only trustworthy because this module ISSUED it -- `confirms()` looks
# the capture up in this process's own register and compares identity as well as hash, so a
# hand-built object carrying a plausible hash is not a capture and does not bind anything.
#
# The register is bounded. An unbounded provenance log in a long-running drain is a slow leak, and
# the only captures that matter are the recent ones a seal is about to be built from.
_CAPTURE_LIMIT = 256
_captures = OrderedDict()          # capture id -> Capture, most recent last
_capture_lock = threading.Lock()


def _sha256(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    elif isinstance(data, bytearray):
        data = bytes(data)
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Capture:
    """One worker response, as it arrived. Provenance, not a verdict.

    `raw_sha256` is the response content exactly as the server sent it. `stripped_sha256` is the
    same bytes with surrounding whitespace removed, because that single normalisation is what
    every caller in this repo does before writing an artifact, and a provenance record that does
    not survive it would be provenance nobody could use. That pair is the WHOLE set of forms this
    capture vouches for: anything else a caller does to the bytes is a transform it must declare
    to evidence.seal, where it gets re-run and tested for sensitivity to its input."""
    id: str
    worker: str
    model: str
    prompt_sha256: str
    raw_sha256: str
    stripped_sha256: str
    params: tuple
    received_at: float

    def covers(self, data):
        """Are these the bytes this call returned (raw, or whitespace-stripped)?"""
        h = _sha256(data)
        return h == self.raw_sha256 or h == self.stripped_sha256

    def confirms(self, data):
        """The full test a sealer must pass: this capture was issued by THIS call layer, and the
        bytes being sealed are the bytes it recorded. Identity first -- a Capture that this module
        never issued is a caller's assertion wearing a capture's shape, which is the exact defect
        being closed, so it is refused before its hashes are even consulted."""
        with _capture_lock:
            issued = _captures.get(self.id)
        return issued is not None and issued == self and self.covers(data)

    def describe(self):
        return (f"capture {self.id[:12]} {self.worker}/{self.model} "
                f"-> {self.raw_sha256[:12]}")


def _record_capture(worker, model, prompt, content, params):
    import uuid
    raw = content if isinstance(content, str) else (content or "")
    cap = Capture(id=uuid.uuid4().hex, worker=worker, model=str(model),
                  prompt_sha256=_sha256(prompt or ""), raw_sha256=_sha256(raw),
                  stripped_sha256=_sha256(raw.strip()), params=tuple(sorted(params.items())),
                  received_at=time.time())
    with _capture_lock:
        _captures[cap.id] = cap
        while len(_captures) > _CAPTURE_LIMIT:
            _captures.popitem(last=False)
    return cap


def capture(capture_id):
    """Look one up by id. Returns None for an id this process never issued."""
    with _capture_lock:
        return _captures.get(capture_id)


def external_capture(worker, model, content, prompt=""):
    """Register a capture for bytes this process obtained outside chat() -- e.g. the content a
    tool-enabled worker wrote to its deliverable via the tool-service `files` tool. It is issued by
    THIS process (so `confirms` accepts it) and covers exactly those bytes, which is what binds a
    tool-written artifact to the worker that wrote it. Weaker than a chat capture only in that it
    does not say which model call produced it (same caveat as find_capture)."""
    return _record_capture(worker, str(model), prompt, content, {"source": "tool-write"})


def find_capture(data):
    """The most recent capture that COVERS these bytes, or None.

    For callers that cannot thread telemetry back out -- a nested agent loop like run/tooljob.py
    returns only the final content. Searching this process's own register still establishes the
    thing that matters: these bytes were returned by a call this process actually made. It
    establishes nothing about WHICH call, so it is a weaker statement than a capture handed
    straight back from chat(), and it is only used where the stronger one is unavailable."""
    with _capture_lock:
        recent = list(_captures.values())
    for cap in reversed(recent):
        if cap.covers(data):
            return cap
    return None


def captures_issued():
    """How many responses this process has captured. For tests and for an honest status line."""
    with _capture_lock:
        return len(_captures)


def est_tokens(messages):
    """Cheap token estimate (~4 chars/token + a little per-message overhead). Good enough for a guard."""
    return sum(len(m.get("content") or "") for m in messages) // 4 + 8 * len(messages)


def fit_context(messages, ctx, reserve, log=None, preserve_first_user=False):
    """Keep the request under the worker's context window. Reserve room for the completion. Drop the
    OLDEST non-system messages first (they're the stalest); always keep system messages + the last
    message. With preserve_first_user, also retain the first user request containing essential
    carry. Raise if required messages cannot fit; never silently truncate essential reference data.
    Capacity is estimated, not measured with the backend tokenizer."""
    budget = ctx - reserve
    if budget <= 0:
        raise ValueError(f"context guard: ctx {ctx} smaller than reserve {reserve}")
    if est_tokens(messages) <= budget:
        return messages
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    pinned = []
    if preserve_first_user and len(rest) > 1 and rest[0].get("role") == "user":
        pinned = [rest.pop(0)]
    tail = rest[-1:] if rest else []
    middle = rest[:-1]
    while middle and est_tokens(sys_msgs + pinned + middle + tail) > budget:
        middle.pop(0)
    result = sys_msgs + pinned + middle + tail
    if est_tokens(result) > budget:
        raise ValueError(f"context overflow: required messages (~{est_tokens(result)} tok) exceeds budget "
                         f"{budget} (ctx {ctx}, reserve {reserve}); shrink the input/authority")
    if log and len(result) < len(messages):
        log(f"context guard: trimmed {len(messages) - len(result)} old message(s) to fit ctx {ctx}")
    return result


def body_for(worker, messages, tools=None, max_tokens=1024, think=False, temperature=0.6, grammar=None):
    w = WORKERS[worker]
    body = {"model": w["model"], "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "stream": False,
            "chat_template_kwargs": {"enable_thinking": bool(think)}}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    # ---- capability filter (keyed on declared capabilities, never on the silicon) ----
    # The underlying behaviors are unchanged -- each exists because of a measured failure -- only the
    # lookup moved from `kind == "mlx"` to the worker's declared flags in fleet.py.
    style = w.get("reasoning_style", "none")
    if style == "template_kwargs" and think:          # mlx-lm: reasoning_effort is a template kwarg
        body["chat_template_kwargs"]["reasoning_effort"] = "medium"
    elif style == "budget_field" and not think:       # llama.cpp qwen3.8: don't draft a discarded reasoning pass
        body["reasoning_budget"] = 0
    if grammar and w.get("supports_gbnf"):            # grammar / json_schema constrained decoding
        if isinstance(grammar, dict):
            body["json_schema"] = grammar
        else:
            body["grammar"] = grammar
    body.update(w.get("params", {}))
    return body


def _post_nonstream(url, body, timeout):
    req = urllib.request.Request(url + "/v1/chat/completions",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        response = json.loads(r.read())
        msg = dict(response["choices"][0]["message"])
        from generation import response_evidence
        msg["_handoff_generation"] = response_evidence(response, body)
        return msg


class PrefillLockTimeout(RuntimeError):
    """The cluster prefill lock could not be acquired. Refusing beats an unserialized prefill."""


def _client_timed_out(exc):
    """True when the client gave up waiting, which is not evidence the server stopped."""
    if isinstance(exc, TimeoutError):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, TimeoutError):
        return True
    return "timed out" in str(exc).lower()


def _post_stream_with_prefill_lock(url, body, worker, timeout):
    """Stream the response and hold the cluster prefill lock ONLY until the first token arrives (first
    token == prefill done). Serializes prefills, allows concurrent decodes. Releases the lock on the
    first token, or on an error that means the server is not prefilling. A client timeout keeps the
    lock: the Mac may still be inside that prefill."""
    body = dict(body); body["stream"] = True
    req = urllib.request.Request(url + "/v1/chat/completions",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    lock = f"prefill:{worker}"
    # stale_s MUST exceed the longest a holder can legitimately hold this lock. Default 60s was a
    # correctness hole (found in prior-art review, 2026-09-19): a prefill of a long prompt takes
    # more than 60s on the cluster, so a second request would STEAL the lock from a live prefill
    # and produce the exact concurrent insert_segments crash the lock exists to prevent. A holder
    # cannot outlive its own request timeout, so the timeout is the correct staleness bound.
    if not jobs.acquire_lock(lock, stale_s=timeout + 30):
        # Proceeding here would run an unserialized prefill -- the exact concurrency this lock
        # exists to prevent. Refuse and let the caller retry or park.
        raise PrefillLockTimeout(f"{worker}: could not acquire the prefill lock; refusing to "
                                 f"start an unserialized prefill")
    acquired = True
    released = False
    # A client timeout does not stop the Mac. mlx often will not notice the dropped connection
    # until it tries to write a token, so the prefill keeps running. Releasing the lock here let
    # the next probe -- the one Handoff runs before dispatch -- start a second prefill on the one
    # prefill lane. That is the insert_segments wedge. Keep the lock until a later caller is
    # allowed to treat it as stale.
    abandoned = False
    role = "assistant"; content = []; tcs = {}
    finish_reason = None
    usage = None
    # PROGRESS WATCHDOG (issue #14): the socket timeout is per-READ, so a server that trickles
    # keepalives/empty chunks keeps resetting it and never trips the wall -- the stream hangs while
    # making no real progress. Bound the time since the last GENERATED token (keepalives and the
    # role-only preamble do NOT count): if nothing is generated for `timeout` seconds, give up. The
    # raised TimeoutError is treated exactly like a socket timeout below (the prefill lock is kept,
    # not handed away mid-prefill).
    last_progress = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                if time.monotonic() - last_progress > timeout:
                    raise TimeoutError(
                        "{0}: no generated token in {1}s (stream made no progress; "
                        "likely keepalive trickle)".format(worker, timeout))
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except Exception:
                    continue
                choice = (chunk.get("choices") or [{}])[0]
                if choice.get("finish_reason") is not None:
                    finish_reason = choice["finish_reason"]
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                delta = choice.get("delta", {}) or {}
                # Release only once a GENERATED TOKEN has actually arrived. A chunk alone is not
                # proof of generation: servers emit a role-only preamble and keepalives before the
                # first token, and releasing on those hands the lock away mid-prefill. This server
                # streams its thinking in `reasoning`, so that counts as generation too.
                generated = bool(delta.get("content") or delta.get("reasoning")
                                 or delta.get("tool_calls"))
                if generated:
                    last_progress = time.monotonic()  # real token -> the stream is making progress
                if not released and generated:
                    jobs.release_lock(lock)
                    released = True
                if delta.get("role"):
                    role = delta["role"]
                if delta.get("content"):
                    content.append(delta["content"])
                for tc in (delta.get("tool_calls") or []):
                    slot = tcs.setdefault(tc.get("index", 0), {"id": "", "type": "function",
                                                               "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]
    except Exception as e:
        if not released and _client_timed_out(e):
            abandoned = True
            jobs.renew_lock(lock)                       # measure the hold from the abandon, not the start
        raise
    finally:
        if acquired and not released and not abandoned:
            jobs.release_lock(lock)
    msg = {"role": role, "content": "".join(content)}
    if tcs:
        msg["tool_calls"] = [tcs[i] for i in sorted(tcs)]
    from generation import response_evidence
    msg["_handoff_generation"] = response_evidence({
        "choices": [{"message": dict(msg), "finish_reason": finish_reason}], "usage": usage}, body)
    return msg


def chat(worker, messages, tools=None, max_tokens=1024, think=False, temperature=0.6, timeout=300,
         track=True, grammar=None, preserve_first_user=False, profile=False):
    """One call path. Applies the context guard, the cluster prefill lock, and (llama) grammar. When
    track=True the dispatch is logged to the ledger and capped by max_inflight."""
    w = WORKERS[worker]
    original_message_count = len(messages)
    messages = fit_context(messages, w.get("ctx", 65536), max_tokens + 512,
                           preserve_first_user=preserve_first_user)
    prof = {}
    if profile:   # opt-in: worker calls only. Canaries/qualification keep fixed, safe settings.
        from generation import worker_request_profile
        prof = worker_request_profile(worker)
        think = bool(prof.get("think", think))
    body = body_for(worker, messages, tools, max_tokens, think, temperature, grammar)
    if prof:
        from generation import apply_request_profile
        apply_request_profile(body, prof, w.get("reasoning_style", "none"))
    jid = None
    if track:
        prompt = messages[-1].get("content", "") if messages else ""
        cap = w.get("max_inflight", 0)
        jid = jobs.open_job_capped(worker, prompt, cap) if cap else jobs.open_job(worker, prompt)
    t = time.time()
    try:
        if w.get("requires_prefill_lock"):
            msg = _post_stream_with_prefill_lock(w["url"], body, worker, timeout)
        else:
            msg = _post_nonstream(w["url"], body, timeout)
        from generation import response_evidence
        generation = msg.pop("_handoff_generation", None)
        if generation is None:
            generation = response_evidence({"choices": [{"message": msg}]}, body)
        generation.update({"configured_context": w.get("ctx", 65536),
                           "input_tokens_estimate": est_tokens(messages),
                           "messages_trimmed": original_message_count - len(messages)})
        if jid:
            jobs.close_job(jid, "done")
        # Capture the response HERE, where it arrives, before any caller has had a chance to
        # substitute bytes for it. Everything downstream that wants to claim "the worker produced
        # this" has to present the capture; see the PROVENANCE block at the top of this module.
        cap = _record_capture(worker, w.get("model"), messages[-1].get("content", "") if messages else "",
                              msg.get("content") or "",
                              {"temperature": body.get("temperature"), "max_tokens": max_tokens,
                               "think": bool(think)})
        return msg, {"ms": round((time.time() - t) * 1000), "backend": w["kind"],
                     "capture": cap, "generation": generation}
    except BaseException as e:
        if jid:
            jobs.close_job(jid, "error", repr(e))
        raise


if __name__ == "__main__":
    for wk in ("cluster", "spark", "pairA"):
        try:
            m, t = chat(wk, [{"role": "user", "content": "Reply with one word: ok"}], max_tokens=5)
            print(f"  {wk:<8} [{t['backend']:>5}] {t['ms']:>5}ms -> {(m.get('content') or '').strip()[:30]}")
        except Exception as e:
            print(f"  {wk:<8} ERR {type(e).__name__}: {e}")
