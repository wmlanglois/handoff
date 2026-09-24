"""Oscillation detector for the local repair loop.

At temp=0 a small model fed pytest tracebacks can ping-pong forever: fix line 14, break line 10; fix
line 10, revert line 14 -- deterministically, until the round cap, burning compute for nothing. A plain
retry counter can't tell "making progress" from "stuck in a cycle". This tracks the sequence of output
STATES (a normalized hash of each attempt) and flags:

  - "oscillation": this exact state == the state two turns ago (A -> B -> A ping-pong), OR a longer
    cycle where the current state has been seen before AND the last state was also a repeat.
  - "repeat": this exact state was seen at some earlier turn (converged onto a fixed point that still
    fails -- also stuck).
  - "progress": a genuinely new state.

Either "oscillation" or "repeat" means the local worker is structurally stuck: hard-abort and escalate
to the frontier instead of looping to the cap.
"""
import hashlib
import re
from dataclasses import dataclass


def normalize(text: str) -> str:
    """Collapse whitespace so trivial reformatting doesn't read as a new state."""
    return re.sub(r"\s+", " ", (text or "").strip())


def state_hash(text: str) -> str:
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()


@dataclass
class Observation:
    h: str
    tag: str        # "progress" | "repeat" | "oscillation"
    turn: int       # 1-based
    stuck: bool     # tag in ("repeat", "oscillation")


class StateTracker:
    """Feed each attempt's output; get back whether it's progress or a stuck cycle."""

    def __init__(self):
        self._hashes: list[str] = []

    def observe(self, output: str) -> Observation:
        h = state_hash(output)
        turn = len(self._hashes) + 1
        seen_before = h in self._hashes
        pingpong = len(self._hashes) >= 2 and self._hashes[-2] == h
        if pingpong:
            tag = "oscillation"
        elif seen_before:
            tag = "repeat"
        else:
            tag = "progress"
        self._hashes.append(h)
        return Observation(h=h, tag=tag, turn=turn, stuck=tag in ("repeat", "oscillation"))

    @property
    def history(self) -> list[str]:
        return list(self._hashes)
