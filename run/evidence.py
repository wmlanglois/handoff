"""Earned acceptance: an evidence record that BINDS the worker's output to the bytes accepted.

Milestone 2. The caution this module exists to enforce, from internal/docs/MILESTONES.md:

    "One model call plus one file read can still be ceremonial. The provenance gate must connect
     the worker's output to the artifact being accepted, not merely count tool calls."

The adversary is explicit and is NOT a malicious human -- it is an ordinary stalled run that has
learned to look busy. It calls a local model. It reads a file. Its tool-call counter goes up. Then
it accepts an artifact that some earlier turn (or a template, or a hand edit) put on disk. Every
surface signal is green and nothing the worker produced ever became the thing that shipped. A
counter of tool calls cannot see this, because the counter is incremented by the ceremony itself.

So the binding here is arithmetic, never declarative:

  * `seal()` is the ONLY sanctioned way to make a record, and it refuses to make one unless the
    artifact bytes are DEMONSTRABLY derived from the worker output bytes -- either identical
    (IDENTITY) or reproduced by re-running the declared transform on the output and comparing the
    result byte for byte. A run whose output never became the artifact cannot get a record at all;
    it fails at seal time with `Unbindable`, before any verdict exists to be trusted.
  * `accept()` re-hashes the exact bytes in hand and compares them to the hash the record was
    sealed against, so a rewrite after the verdict is caught even though the record is intact.

The second standing caution applies to the tool results this record carries:

    "A citation-shaped string establishes neither relevance nor correctness."

`ToolResult` is therefore provenance only: it says which tool ran with which arguments and what
came back, so a human can go re-derive it. Acceptance NEVER reads the number of tool results, and
never treats a well-formed-looking reference as support. Twenty tool calls and zero tool calls take
exactly the same path through `accept()`; only the output->artifact binding, the integrity digest,
the job/attempt identity, and a trusted verifier's PASS can produce acceptance.

The four refusals the milestone requires, each typed, checked in this order:

  MISSING   -- there is no record for this job/attempt. Nothing to reason from.
  ALTERED   -- the record's contents disagree with its own integrity digest. Checked BEFORE the
               identity and freshness tests on purpose: once a record has been mutated, its
               `job_id`, `attempt` and `artifact_sha256` fields are exactly the fields a forger
               would have edited, so reporting UNRELATED or STALE from them would be reporting a
               conclusion drawn from data already known to be untrustworthy.
  UNRELATED  -- the record is real and intact but belongs to a different job or a different
               attempt. Attempt 2 does not inherit attempt 1's verdict.
  STALE      -- the record binds a different artifact hash than the bytes now in hand: the artifact
               was rewritten after the verdict was rendered.

Two further refusals that fall out of taking the verifier field seriously rather than decoratively:
NO_PASS (the recorded verdict is not a pass) and UNTRUSTED_VERIFIER (the identity+version that
rendered it is not on the accepting side's allow-list -- a verifier name in a record is itself just
a citation-shaped string until someone checks it, and a version bump is a different verifier).

WHAT CLOSED B2, 2026-09-19. Everything above rested on `worker_output` being the worker's output,
and nothing checked that. Passing the same bytes as output and artifact issued a clean IDENTITY
record for a run that never called a worker. The fix could not live here -- by the time seal() is
called, "what the worker returned" is just another argument -- so provenance is now captured in
call.py, where the response actually arrives, and arrives here as a `capture`. `capture.confirms()`
checks the call layer's own register, not merely a hash, so a fabricated capture object is refused
as well. A record says which it is (`provenance`: CAPTURED or ASSERTED), the digest covers that
field, and `accept(require_captured=True)` refuses an asserted one. ASSERTED remains legal and
remains the default so a simulated run with no call layer is still testable -- but it is never
again indistinguishable from an observed one.

The same defect applied to the VERDICT, which also arrived as a bare caller assertion. A
`VerifierResult` carries the job, attempt and artifact hash it was computed over, and seal()
refuses it against any other -- so attempt 1's PASS cannot be presented for attempt 3.

HONEST LIMIT, stated because the rest of this repo is built on not overclaiming: the default
integrity digest is a plain SHA-256 over the canonical payload. That is tamper-EVIDENT -- it catches
mutation of a record at rest or in transit -- but it is not a signature, so anyone able to call
`seal`'s internals can produce a self-consistent record. What actually stops the ceremonial run is
`seal()` refusing to bind an artifact it cannot derive from the output; the digest only stops later
edits. Pass `key=` to `seal()`/`accept()` to upgrade the digest to a keyed HMAC, which additionally
defeats a forger who does not hold the key.
"""
import hashlib
import hmac
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

# --- derivations: HOW the artifact came from the worker's output --------------------------------
IDENTITY = "identity"              # the artifact bytes ARE the worker output bytes
TRANSFORM = "transform"            # artifact = f(output), re-run and compared at seal time

# --- verdicts -----------------------------------------------------------------------------------
PASS = "PASS"
FAIL = "FAIL"

# --- typed refusal reasons ----------------------------------------------------------------------
MISSING = "MISSING"                        # no record for this job/attempt
STALE = "STALE"                            # record binds different bytes than the ones in hand
UNRELATED = "UNRELATED"                    # record belongs to another job or another attempt
ALTERED = "ALTERED"                        # contents disagree with the integrity digest
NO_PASS = "NO_PASS"                        # the recorded verdict was not a pass
UNTRUSTED_VERIFIER = "UNTRUSTED_VERIFIER"  # verifier id+version not on the allow-list
UNCAPTURED = "UNCAPTURED"                  # the output was asserted by the caller, not captured

REFUSALS = (MISSING, ALTERED, UNRELATED, STALE, NO_PASS, UNTRUSTED_VERIFIER, UNCAPTURED)

# --- how the worker's output got into this record -----------------------------------------------
# ASSERTED is the honest name for what seal() accepted before the call layer captured anything: a
# caller said "these are the worker's bytes" and nothing checked. It is still allowed, because a
# simulated or stubbed run has no call layer and must remain testable -- but it is RECORDED, it is
# covered by the digest, and `accept(require_captured=True)` refuses it. The distinction has to be
# visible in the record; a record that cannot tell you whether its provenance was checked is a
# record that will be read as though it was.
ASSERTED = "asserted"
CAPTURED = "captured"


class Unbindable(Exception):
    """seal() could not demonstrate that the artifact came from the worker's output.

    This is the ceremonial run's exit. It is raised at SEAL time, not at accept time, so that no
    record ever exists claiming a binding that was never shown. A refusal you can only discover at
    acceptance is a refusal something upstream may have already reported as success."""


# --- hashing ------------------------------------------------------------------------------------

def _as_bytes(data):
    """Accept str or bytes; hash the exact bytes either way. Text is UTF-8, no error swallowing --
    a prompt that will not round-trip should blow up here rather than hash as mojibake."""
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, str):
        return data.encode("utf-8")
    raise TypeError(f"expected bytes or str, got {type(data).__name__}")


def sha256(data):
    return hashlib.sha256(_as_bytes(data)).hexdigest()


def sha256_file(path):
    """Hash a file's exact bytes. Read in binary on purpose: hashing decoded text would make a
    line-ending rewrite invisible, and a line-ending rewrite is still a rewrite."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(obj):
    """Deterministic JSON. Sorted keys and fixed separators so the same record hashes the same on
    any machine and in any dict-insertion order; `ensure_ascii` so the digest does not depend on
    the encoder's unicode policy."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def integrity_digest(payload, key=None):
    """SHA-256 over the canonical payload, or HMAC-SHA256 when a key is supplied.

    Keyless: tamper-evident (catches edits), forgeable by anyone who can run this code.
    Keyed: also unforgeable without the key. See the module docstring's honest limit."""
    blob = canonical(payload).encode("utf-8")
    if key is None:
        return hashlib.sha256(blob).hexdigest()
    # DEFECT 2026-09-19: key="" took the HMAC path with an empty secret -- keyless in substance
    # while reading as keyed at the call site. A caller who believes they are signing must not be
    # silently unsigned. Passing None is the way to ask for a keyless digest; "" is a mistake.
    kb = _as_bytes(key)
    if not kb:
        raise ValueError("integrity key is empty; pass key=None for a keyless digest, or a real "
                         "secret. An empty key is HMAC with no secret and signs nothing.")
    return hmac.new(kb, blob, hashlib.sha256).hexdigest()


# --- the pieces that make up a record -----------------------------------------------------------

@dataclass(frozen=True)
class WorkerRequest:
    """The request ACTUALLY issued, not the one a plan said would be issued.

    The prompt is stored as a hash, never as text: the prompt may carry client material and this
    record is meant to be committable and shareable. The hash is still enough to prove that the
    prompt someone shows you later is the one that ran."""
    worker: str
    model: str
    prompt_sha256: str
    params: tuple = ()          # (("temperature", 0.0), ...) sorted; sampling params affect output

    @classmethod
    def of(cls, worker, model, prompt, **params):
        return cls(worker=worker, model=model, prompt_sha256=sha256(prompt),
                   params=tuple(sorted(params.items())))

    def payload(self):
        return {"worker": self.worker, "model": self.model, "prompt_sha256": self.prompt_sha256,
                "params": [list(p) for p in self.params]}


@dataclass(frozen=True)
class ToolResult:
    """One tool call whose result entered the worker's context.

    Provenance only. This is deliberately NOT support for anything: a row here says "this tool was
    called with these arguments and returned these bytes", which is a pointer for a human to go
    re-derive, not a claim that what came back was relevant or correct. Acceptance never counts
    these rows and never inspects their contents."""
    tool: str
    args_sha256: str
    result_sha256: str

    @classmethod
    def of(cls, tool, args, result):
        # args are canonicalized first so {"a":1,"b":2} and {"b":2,"a":1} are the same call.
        blob = args if isinstance(args, (str, bytes, bytearray)) else canonical(args)
        return cls(tool=tool, args_sha256=sha256(blob), result_sha256=sha256(result))

    def payload(self):
        return {"tool": self.tool, "args_sha256": self.args_sha256,
                "result_sha256": self.result_sha256}


@dataclass(frozen=True)
class Verifier:
    """Who rendered the verdict, and which version of them. Version is part of the identity: a
    verifier whose rules changed is a different verifier, and a verdict from the old one does not
    carry over."""
    id: str
    version: str

    def payload(self):
        return {"id": self.id, "version": self.version}

    def as_pair(self):
        return (self.id, self.version)


class StaleResult(Exception):
    """A verifier result was offered for an attempt or an artifact it was not computed over."""


@dataclass(frozen=True)
class VerifierResult:
    """A verdict that carries WHAT IT WAS COMPUTED OVER.

    `verdict` used to reach seal() as a bare PASS/FAIL -- the same class of unverified caller
    assertion that `worker_output` was. A PASS is only meaningful about specific bytes at a
    specific attempt, so the result records both and seal() cross-checks them. The failure this
    prevents is concrete: attempt 1 passes, attempts 2 and 3 do not, and attempt 3 is accepted on
    attempt 1's verdict -- every field in the record true, the conclusion false.

    `ran_at` and `detail` are for the human reading a receipt; neither is consulted by any gate."""
    verifier: Verifier
    verdict: str
    job_id: str
    attempt: int
    artifact_sha256: str
    ran_at: float = 0.0
    detail: str = ""

    def bind(self, job_id, attempt, artifact_sha256):
        """Raise unless this result is about exactly this job, attempt and these bytes."""
        if self.job_id != job_id or self.attempt != attempt:
            raise StaleResult(
                f"verifier result is for {self.job_id}#{self.attempt}, offered for "
                f"{job_id}#{attempt}: a verdict does not carry across attempts")
        if self.artifact_sha256 != artifact_sha256:
            raise StaleResult(
                f"verifier result was computed over artifact {self.artifact_sha256[:12]} but the "
                f"bytes being sealed hash to {artifact_sha256[:12]}: the artifact changed between "
                f"the check and the seal")
        return True

    def payload(self):
        return {"verifier": self.verifier.payload(), "verdict": self.verdict,
                "job_id": self.job_id, "attempt": self.attempt,
                "artifact_sha256": self.artifact_sha256}


@dataclass(frozen=True)
class EvidenceRecord:
    """Everything that must hold together for acceptance to be earned, plus its own digest.

    `output_sha256` and `derivation` are the part a tool-call counter does not have: they say what
    the worker actually produced and how those bytes became the artifact. `seal()` proves the
    derivation by recomputation before this object exists."""
    job_id: str
    attempt: int
    request: WorkerRequest
    tool_results: tuple
    output_sha256: str
    artifact_sha256: str
    derivation: str                 # IDENTITY, or "transform:<name>"
    verifier: Verifier
    verdict: str
    digest: str = ""
    provenance: str = ASSERTED      # ASSERTED, or "captured:<capture id>"
    result_bound: bool = False      # the verdict arrived as an attempt-bound VerifierResult

    def payload(self):
        """The bytes the digest covers. `digest` itself is excluded -- a digest cannot cover
        itself -- and EVERY other field is included, so there is no field a forger can edit that
        the digest does not notice."""
        return {
            "job_id": self.job_id,
            "attempt": self.attempt,
            "request": self.request.payload(),
            "tool_results": [t.payload() for t in self.tool_results],
            "output_sha256": self.output_sha256,
            "artifact_sha256": self.artifact_sha256,
            "derivation": self.derivation,
            "verifier": self.verifier.payload(),
            "verdict": self.verdict,
            # In the payload, so a record cannot be quietly relabelled as captured after the fact.
            "provenance": self.provenance,
            "result_bound": self.result_bound,
        }

    @property
    def captured(self):
        return self.provenance.startswith(CAPTURED)

    def intact(self, key=None):
        """Does the record still match the digest it was sealed with?"""
        return hmac.compare_digest(self.digest, integrity_digest(self.payload(), key))

    def describe(self):
        """One line for a ledger or a human review pass. No verdict language -- this is what the
        record SAYS, which is not the same as what acceptance concluded."""
        return (f"{self.job_id}#{self.attempt} {self.verdict} by {self.verifier.id}"
                f"@{self.verifier.version}  artifact={self.artifact_sha256[:12]}"
                f"  from {self.request.worker}/{self.request.model} via {self.derivation}"
                f"  ({len(self.tool_results)} tool result(s), not evidence of anything)")


# --- sealing: the only sanctioned way to make a record ------------------------------------------

def seal(job_id, attempt, request, tool_results, worker_output, artifact_bytes,
         verifier, verdict, transform=None, transform_name=None, key=None,
         capture=None, result=None):
    """Bind a verdict to an artifact, refusing unless the artifact is derived from the output.

    worker_output  -- the exact bytes the worker returned
    artifact_bytes -- the exact bytes being put forward as the artifact
    transform      -- None means the artifact must BE the output (IDENTITY). Otherwise a callable
                      that is RE-RUN here on `worker_output`; its result must equal `artifact_bytes`
                      byte for byte. A named transform with no recomputation would be a
                      citation-shaped string: it would assert a link instead of showing one.
    capture        -- the call layer's record of the response, from `call.chat`'s telemetry. When
                      given, `worker_output` must be bytes that capture CONFIRMS it received --
                      which is what turns `worker_output` from a caller's assertion into an
                      observation. When absent the record is stamped ASSERTED and says so.
    result         -- a VerifierResult. When given, its job, attempt and artifact hash must match
                      this seal exactly, and ITS verdict is the one recorded: a verdict computed
                      over other bytes or another attempt cannot be laundered through here.

    Raises Unbindable when the link cannot be shown. That is the whole point: the ceremonial run --
    model called, file read, artifact from somewhere else entirely -- terminates here, with no
    record for a downstream accept() to find."""
    if not job_id:
        raise ValueError("job_id is required; an unlabelled record cannot be matched to a job")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        # A bool would sail through `int` and silently make every attempt 1 or 0.
        raise ValueError(f"attempt must be an int >= 1, got {attempt!r}")
    if verdict not in (PASS, FAIL):
        raise ValueError(f"verdict must be {PASS!r} or {FAIL!r}, got {verdict!r}")

    out = _as_bytes(worker_output)
    art = _as_bytes(artifact_bytes)

    # PROVENANCE, before any derivation work. If the "worker output" was never received from a
    # worker, nothing downstream is worth computing: the whole chain hangs off these bytes being
    # the ones that came back. `confirms` checks the capture's IDENTITY in the call layer's own
    # register as well as its hash, so a fabricated Capture carrying a correct-looking digest is
    # refused -- otherwise the assertion this parameter removes would simply move one object out.
    provenance = ASSERTED
    if capture is not None:
        if not capture.confirms(out):
            raise Unbindable(
                f"{job_id}#{attempt}: the bytes offered as the worker's output are not the bytes "
                f"the call layer captured for this call ({getattr(capture, 'id', '?')}). Either "
                f"they came from somewhere else, or the capture was not issued by this process.")
        provenance = f"{CAPTURED}:{capture.id}"

    if transform is None:
        if out != art:
            raise Unbindable(
                f"{job_id}#{attempt}: the artifact is not the worker's output "
                f"(output {sha256(out)[:12]}, artifact {sha256(art)[:12]}) and no transform was "
                f"declared. The worker ran; its output is not what is being accepted.")
        derivation = IDENTITY
    else:
        produced = _as_bytes(transform(out))
        if produced != art:
            raise Unbindable(
                f"{job_id}#{attempt}: re-running the declared transform on the worker's output "
                f"produced {sha256(produced)[:12]}, not the artifact {sha256(art)[:12]}. The "
                f"declared derivation does not hold.")
        # DEFECT 2026-09-19: recomputation alone was not enough. `lambda _out: artifact` reproduces
        # the artifact byte for byte while ignoring the worker entirely -- the ceremonial run walked
        # straight through the check built to stop it. So the transform must also be SENSITIVE to
        # its input: if it yields the same artifact from bytes the worker never produced, then the
        # artifact does not depend on the worker's work and the link is decoration.
        for probe in (b"", b"fleet-evidence-sensitivity-probe-not-the-worker-output"):
            try:
                if _as_bytes(transform(probe)) == art:
                    raise Unbindable(
                        f"{job_id}#{attempt}: the declared transform returns the artifact even "
                        f"when given bytes the worker never produced, so it ignores its input. "
                        f"The artifact is not derived from the worker's output.")
            except Unbindable:
                raise
            except Exception:
                pass    # a transform that REFUSES junk input is behaving correctly
        derivation = f"{TRANSFORM}:{transform_name or getattr(transform, '__name__', 'anonymous')}"

    # The verdict last, because binding it needs the artifact hash the checks above established.
    # A StaleResult here is Unbindable for the same reason a failed derivation is: a record that
    # exists is a record someone downstream may accept, so the refusal belongs at seal time.
    result_bound = False
    if result is not None:
        try:
            result.bind(job_id, attempt, sha256(art))
        except StaleResult as exc:
            raise Unbindable(f"{job_id}#{attempt}: {exc}")
        if result.verdict not in (PASS, FAIL):
            raise ValueError(f"verifier result verdict must be {PASS!r} or {FAIL!r}, "
                             f"got {result.verdict!r}")
        verdict = result.verdict
        verifier = result.verifier
        result_bound = True

    rec = EvidenceRecord(
        job_id=job_id, attempt=attempt, request=request,
        tool_results=tuple(tool_results or ()),
        output_sha256=sha256(out), artifact_sha256=sha256(art),
        derivation=derivation, verifier=verifier, verdict=verdict,
        provenance=provenance, result_bound=result_bound)
    return replace(rec, digest=integrity_digest(rec.payload(), key))


# --- the decision --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Refusal:
    reason: str
    detail: str = ""

    def __str__(self):
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


@dataclass(frozen=True)
class Decision:
    """Falsy when refused, so `if not accept(...)` reads correctly and a caller who forgets to
    check `.accepted` still cannot treat a refusal as a pass in a boolean context."""
    accepted: bool
    refusal: object = None          # Refusal | None
    record: object = None           # EvidenceRecord | None

    def __bool__(self):
        return self.accepted

    @property
    def reason(self):
        return None if self.refusal is None else self.refusal.reason


def _refuse(reason, detail, record=None):
    return Decision(accepted=False, refusal=Refusal(reason, detail), record=record)


def accept(record, job_id, attempt, artifact_bytes, trusted_verifiers, key=None,
           require_captured=False):
    """Decide whether these exact bytes may be accepted for this job/attempt.

    `trusted_verifiers` has NO DEFAULT on purpose. A default would mean "trust whoever the record
    names", which turns the verifier field into decoration -- exactly the citation-shaped-string
    failure. Pass an explicit collection of `Verifier` or `(id, version)` pairs; an empty one trusts
    nobody and refuses everything, which is the correct fail-closed posture for a caller that has
    not yet decided who it trusts.

    Note what is NOT consulted anywhere below: how many tool results the record carries, what they
    contain, how long the output was, or anything the worker said about itself."""
    if record is None:
        return _refuse(MISSING, f"no evidence record for {job_id}#{attempt}")

    # Integrity FIRST. Every test after this one reads fields off the record, and reading fields
    # off a record that has already been shown to be mutated would be drawing a conclusion from
    # data known to be untrustworthy.
    if not record.intact(key):
        return _refuse(ALTERED, f"{record.job_id}#{record.attempt}: contents do not match the "
                                f"integrity digest; the record was edited after it was sealed",
                       record)

    # DEFECT 2026-09-19 (skeptic): accept() read job_id, attempt, verifier, verdict and
    # artifact_sha256, and never looked at `derivation` or `output_sha256`. So a record whose
    # derivation claims IDENTITY while its two hashes differ -- internally self-contradictory, and
    # impossible for seal() to have produced -- sailed through without comment. seal() being the
    # only SANCTIONED constructor is not the same as it being the only POSSIBLE one, since
    # EvidenceRecord and integrity_digest are both public. So accept() re-checks the one invariant
    # it can verify from the record alone, rather than trusting that seal() must have been used.
    if record.derivation == IDENTITY and record.output_sha256 != record.artifact_sha256:
        return _refuse(ALTERED, f"{record.job_id}#{record.attempt}: record claims the artifact IS "
                                f"the worker's output, but its output hash "
                                f"({record.output_sha256[:12]}) and artifact hash "
                                f"({record.artifact_sha256[:12]}) differ. seal() cannot produce "
                                f"this record; it was constructed by hand.")

    if record.job_id != job_id or record.attempt != attempt:
        return _refuse(UNRELATED, f"record is for {record.job_id}#{record.attempt}, "
                                  f"acceptance is for {job_id}#{attempt}", record)

    trusted = {v.as_pair() if isinstance(v, Verifier) else tuple(v) for v in trusted_verifiers}
    if record.verifier.as_pair() not in trusted:
        return _refuse(UNTRUSTED_VERIFIER,
                       f"{record.verifier.id}@{record.verifier.version} is not trusted here "
                       f"(a verifier named in a record is not thereby a verifier)", record)

    if require_captured and not record.captured:
        return _refuse(UNCAPTURED,
                       f"{record.job_id}#{record.attempt}: the worker output in this record was "
                       f"ASSERTED by its caller, not captured at the call layer. Nothing here "
                       f"establishes that a worker ever produced these bytes", record)

    if record.verdict != PASS:
        return _refuse(NO_PASS, f"the recorded verdict is {record.verdict!r}", record)

    # Re-hash the bytes actually in hand. The record is intact and the verdict is real; the
    # question left is whether the verdict is about THESE bytes.
    now = sha256(artifact_bytes)
    if now != record.artifact_sha256:
        return _refuse(STALE, f"the verdict binds artifact {record.artifact_sha256[:12]} but the "
                              f"bytes being accepted hash to {now[:12]}; the artifact was "
                              f"rewritten after the verdict", record)

    return Decision(accepted=True, refusal=None, record=record)


# --- the ledger -----------------------------------------------------------------------------------

@dataclass
class Ledger:
    """Records keyed by (job_id, attempt). In-memory and deliberately small; the point is the
    lookup semantics, not storage."""
    _records: dict = field(default_factory=dict)

    @property
    def records(self):
        """A COPY. DEFECT 2026-09-19: this was the live dict, so a caller could assign straight
        into it and replace a FAIL record with a friendlier PASS, bypassing put()'s write-once
        rule entirely. The guard is only a guard if it is the only door."""
        return dict(self._records)

    def put(self, record):
        """An attempt happens once, so its record is written once. Silently replacing a record
        would let a second, friendlier verdict overwrite the first one and leave no trace -- the
        ledger equivalent of rewriting the artifact after the verdict."""
        keyed = (record.job_id, record.attempt)
        existing = self._records.get(keyed)
        if existing is not None and existing != record:
            raise ValueError(f"{record.job_id}#{record.attempt} already has a different evidence "
                             f"record; an attempt's evidence is written once")
        self._records[keyed] = record
        return record

    def find(self, job_id, attempt):
        return self._records.get((job_id, attempt))

    def decide(self, job_id, attempt, artifact_bytes, trusted_verifiers, key=None):
        """Lookup + accept. A job/attempt with no record refuses as MISSING rather than raising,
        because 'there is no evidence' is a verdict the caller must handle, not an error."""
        return accept(self.find(job_id, attempt), job_id, attempt, artifact_bytes,
                      trusted_verifiers, key)
