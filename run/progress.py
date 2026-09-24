"""Step one of the delta program: measure progress by ENVIRONMENT STATE, not by what a model says.

This is the module the reporter trap dies on. A stalled architect re-reports ("I did my job") with
fresh prose every turn; its text changes and nothing else does. So progress is defined here on
artifacts the model cannot author about itself -- oracle results, tool evidence, goal criteria, queue
transitions -- and the model's status text is deliberately NOT part of a snapshot.

See docs/RESEARCH-automation-reporter-trap.md section 6.1 for the definition and the evidence
(false success is 44-52% of failures without an independent state check, 3% with one).

Core distinction:
  progress  -- a check flipped, new evidence entered, a goal criterion was satisfied, a job moved
  churn     -- files changed and NOTHING progress-bearing moved. This is the reporter's fingerprint.
  waiting   -- nothing moved but an action is still in flight. NOT stuck. (OpenHands #5355 killed
               agents waiting on long builds by missing exactly this distinction.)

Domain-agnostic on purpose: this module only diffs snapshots. What POPULATES oracle/goal is the
domain's business (pytest for code, assertion oracles and grounded citations for a tax memo). The
core never contains the word "tax" -- that is the open-source adapter seam.
"""
import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path


def hash_text(text):
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()


def hash_files(paths):
    """{path: content_hash} for existing files; missing files are simply absent."""
    out = {}
    for p in paths or []:
        fp = Path(p)
        try:
            out[str(p)] = hashlib.sha1(fp.read_bytes()).hexdigest()
        except OSError:
            continue
    return out


@dataclass(frozen=True)
class Snapshot:
    """The environment after a turn. Built ONLY from artifacts -- never from the model's narrative.

    artifacts : {path: content_hash}
    oracle    : {check_id: "PASS" | "FAIL"}      per deterministic check (a pytest test, an assert)
    evidence  : set of "tool:args_hash" strings   tool calls whose results entered context
    goal      : {criterion: bool}                 done_when satisfaction
    queue     : {job_id: state}                   ready/running/done/parked/failed
    in_flight : set of action ids started but not finished (a live pid, a pending tool call)
    tool_calls: int   this turn's tool-call count (oracle v1 P3/F3: prose with 0 tools is invalid)
    tokens    : int   this turn's output size     (oracle v1 F7: identical outcome AND >500 tokens)
    outcome   : str   the turn's outcome text, hashed on ingest for repetition detection only --
                      it is NEVER a progress signal, only an identity signal
    lanes     : set of worker lanes invoked this turn (oracle v1 P7/F1: local models did the labor)
    """
    artifacts: dict = field(default_factory=dict)
    oracle: dict = field(default_factory=dict)
    evidence: frozenset = frozenset()
    goal: dict = field(default_factory=dict)
    queue: dict = field(default_factory=dict)
    in_flight: frozenset = frozenset()
    tool_calls: int = 0
    tokens: int = 0
    outcome: str = ""
    lanes: frozenset = frozenset()

    @property
    def outcome_hash(self):
        return hash_text(self.outcome)


EMPTY = Snapshot()


@dataclass(frozen=True)
class Delta:
    """diff(prev, cur). `progressed` is the only thing stagnation keys on."""
    oracle_flips: dict = field(default_factory=dict)     # check -> (old, new)
    regressions: dict = field(default_factory=dict)      # subset: PASS -> FAIL
    new_evidence: frozenset = frozenset()
    goal_gained: frozenset = frozenset()
    goal_lost: frozenset = frozenset()
    queue_transitions: dict = field(default_factory=dict)  # job -> (old, new)
    files_changed: frozenset = frozenset()               # CHURN ONLY -- never progress
    waiting: bool = False
    repeated_outcome: bool = False

    # --- three measurements, not one ------------------------------------------------------------
    # "Nothing changed" is a reliable signal. "Something changed" is NOT automatically progress.
    # An earlier version folded regressions, lost goals and every queue transition into a single
    # `progressed` flag, so a loop could thrash jobs between states, lose ground, and still look
    # like it was advancing. Liveness and completion are different questions and get different
    # answers.

    @property
    def activity(self):
        """LIVENESS only: the machinery is running. Says nothing about whether it is helping.
        A reporter rewriting the same file forever is active."""
        return bool(self.files_changed or self.queue_transitions or self.oracle_flips
                    or self.goal_lost or self.new_evidence)

    @property
    def investigation(self):
        """New evidence entered the run. This is what justifies ANOTHER ATTEMPT -- the loop learned
        something it did not have before. It is not achievement, and on its own it must not extend a
        run indefinitely: re-calling a tool with fresh arguments registers new evidence whether or
        not it resolved anything."""
        return bool(self.new_evidence)

    @property
    def achievement(self):
        """COMPLETION: an acceptance criterion was satisfied, or a check went FAIL->PASS, and
        nothing regressed in the same step. This is the only measurement that means the objective
        actually moved closer."""
        gained = bool(self.goal_gained) or any(o == "FAIL" and n == "PASS"
                                               for o, n in self.oracle_flips.values())
        return gained and not self.regressions and not self.goal_lost

    @property
    def progressed(self):
        """Kept for callers that only need 'was this turn worth continuing from'. Achievement or
        investigation -- deliberately NOT activity, which is how a reporter fakes motion."""
        return self.achievement or self.investigation

    @property
    def churn_only(self):
        """The reporter's state-level fingerprint: activity with no achievement and no new evidence.
        Covers file rewrites AND the subtler case of shuffling jobs between queue states."""
        return self.activity and not self.progressed

    @property
    def stuck(self):
        """No progress AND nothing pending. Distinguishing this from `waiting` is the whole point."""
        return not self.progressed and not self.waiting

    def summary(self):
        """One line a disruption can carry -- the delta IS the new information handed to the model."""
        if self.progressed:
            bits = ["achieved" if self.achievement else "investigated"]
            if self.oracle_flips:
                bits.append(", ".join(f"{k}:{o}->{n}" for k, (o, n) in sorted(self.oracle_flips.items())))
            if self.goal_gained:
                bits.append(f"goal+{len(self.goal_gained)}")
            if self.new_evidence:
                bits.append(f"{len(self.new_evidence)} new evidence")
            if self.queue_transitions:
                bits.append(f"{len(self.queue_transitions)} job moves")
            return "progress: " + "; ".join(bits)
        if self.waiting:
            return "waiting: nothing moved, action(s) still in flight"
        if self.churn_only:
            return (f"CHURN: {len(self.files_changed)} file(s) rewritten, 0 checks flipped, "
                    f"0 new evidence, 0 goal movement")
        return "no change: 0 checks flipped, 0 new evidence, 0 goal movement, nothing in flight"


def delta(prev, cur):
    """Diff two snapshots. prev=None treats it as the first turn (everything is new)."""
    prev = prev or EMPTY
    flips, regressions = {}, {}
    for check, now in (cur.oracle or {}).items():
        before = (prev.oracle or {}).get(check)
        if before is not None and before != now:
            flips[check] = (before, now)
            if before == "PASS" and now == "FAIL":
                regressions[check] = (before, now)
    gained = frozenset(k for k, v in (cur.goal or {}).items() if v and not (prev.goal or {}).get(k))
    lost = frozenset(k for k, v in (prev.goal or {}).items() if v and not (cur.goal or {}).get(k))
    transitions = {j: ((prev.queue or {}).get(j), s) for j, s in (cur.queue or {}).items()
                   if (prev.queue or {}).get(j) != s}
    changed = frozenset(p for p, h in (cur.artifacts or {}).items()
                        if (prev.artifacts or {}).get(p) != h)
    return Delta(
        oracle_flips=flips,
        regressions=regressions,
        new_evidence=frozenset(cur.evidence or frozenset()) - frozenset(prev.evidence or frozenset()),
        goal_gained=gained,
        goal_lost=lost,
        queue_transitions=transitions,
        files_changed=changed,
        waiting=bool(cur.in_flight),
        repeated_outcome=bool(cur.outcome) and cur.outcome_hash == prev.outcome_hash,
    )


def stagnant(deltas, k=3, investigation_budget=None):
    """True when the loop should stop attempting. Two independent ways to trip:

    1. No progress (no achievement, no new evidence) for the last k turns.
    2. `investigation_budget` turns have passed with new evidence arriving but NO achievement.
       Investigation buys another attempt; it must not buy unlimited attempts. Without this a loop
       that keeps varying its tool arguments registers new evidence forever and never terminates --
       the reporter trap wearing a lab coat.

    `waiting` suppresses both: an action still in flight is not a stalled loop (OpenHands #5355)."""
    if k <= 0 or len(deltas) < k:
        return False
    if deltas[-1].waiting:
        return False
    if all(not d.progressed for d in deltas[-k:]):
        return True
    if investigation_budget and len(deltas) >= investigation_budget:
        window = deltas[-investigation_budget:]
        if not any(d.achievement for d in window):
            return True
    return False


def goal_complete(snap):
    """Evidence-carrying done, NAIVE form: every goal criterion satisfied AND no oracle check
    failing. The model never gets a vote. (ECT: 0/288 unsafe completions when done must carry
    evidence.)

    LIMITATION -- this treats every criterion as mechanically checkable. Real specs contain criteria
    no machine can ever observe ("somebody tells me it really helped them"), and if one of those is
    in `goal` this returns False forever: the loop waits on something unobservable. Use
    grounding.status(criteria, results) for the honest picture, which separates
    `mechanically_complete` (the loop may stop) from `signed_off` (a human closed the rest).
    This function remains for snapshots whose goal dict is known to be fully mechanical."""
    if not snap.goal:
        return False
    return all(snap.goal.values()) and not any(v == "FAIL" for v in (snap.oracle or {}).values())


def track(snapshots):
    """Convenience: the delta sequence for a whole run."""
    out, prev = [], None
    for s in snapshots:
        out.append(delta(prev, s))
        prev = s
    return out
