"""Delta grounding: turn verbatim intake language into criteria the delta can actually measure.

This is the layer between intake.py (what the user said, word for word) and progress.py (what the
environment did). Without it the delta has nothing to measure against -- `goal` would be an
unexplained dict of booleans with no provenance and no honesty about what is checkable.

The grammar here is lifted from a zero-context model's grounding pass over a real intake spec
(intake/fleet-automation.injection.md, Step 1). Its governing rule, kept verbatim:

    "if the clause cannot be checked mechanically, it is flagged and NO check is invented."

Two distinctions that pass surfaced, both of which the delta needs:

1. A criterion can be STRUCTURALLY grounded but NUMERICALLY ungrounded. "Persistence is avoided on
   small problems" has a real detector -- consecutive identical outcomes over 500 tokens -- but the
   spec says "three, four, five, six, seven turns", so N is unset. The check is registered and inert:
   "it is listed so the slot is frozen." A pending parameter must never silently default.

2. Some criteria are NOT mechanically checkable at all ("somebody tells me it really helped them",
   "a real product"). If those are treated as ordinary booleans the loop can never finish and will
   run forever waiting on something no machine can observe -- the reporter trap arriving by the
   front door. They are separated out and routed to human sign-off, so the loop can reach
   MECHANICALLY COMPLETE and stop, reporting what only a person can close.

A proxy is never silently promoted to a criterion. It is labelled, and it counts only once accepted.
"""
from dataclasses import dataclass, field

# --- criterion states -------------------------------------------------------------------------
GROUNDED = "GROUNDED"            # mechanical check, fully parameterized: evaluable now
PENDING_PARAM = "PENDING_PARAM"  # check exists but a named parameter is unset: frozen and inert
PROXY = "PROXY"                  # a mechanical stand-in exists, awaiting the user's acceptance
FLOOR = "FLOOR"                  # partial coverage: excludes some failures, does not establish it
UNGROUNDABLE = "UNGROUNDABLE"    # no mechanical check possible: human sign-off only

MECHANICAL = {GROUNDED, PROXY, FLOOR}   # states that can contribute a machine verdict


@dataclass
class Criterion:
    """One done_when clause, grounded. `quote` is the user's own words -- provenance is mandatory,
    because a criterion no one can trace back to what was said is an inference."""
    id: str
    quote: str
    source: str = ""                      # which intake field it came from (S2, V2, S4...)
    state: str = UNGROUNDABLE
    check: str = ""                       # what the mechanical check is (description or ref)
    params: dict = field(default_factory=dict)   # {"N": None} -- None means UNSET
    proxy_note: str = ""                  # what the proxy stands in for, if PROXY
    accepted: bool = False                # user accepted this proxy / this criterion as sufficient
    clarifications: list = field(default_factory=list)   # NC ids raised against it

    @property
    def unset_params(self):
        return sorted(k for k, v in (self.params or {}).items() if v is None)

    @property
    def evaluable(self):
        """Can a machine render a verdict on this right now?"""
        if self.state == UNGROUNDABLE:
            return False
        if self.unset_params:
            return False                   # frozen slot: registered but inert
        if self.state == PROXY and not self.accepted:
            return False                   # an unaccepted proxy is not a criterion
        return self.state in MECHANICAL

    @property
    def blocks_done(self):
        """Does this have to be satisfied before the loop may call itself finished?
        UNGROUNDABLE criteria do NOT block the machine -- they block SIGN-OFF."""
        return self.evaluable


def ground(crit, **params):
    """Fill named parameters. A criterion whose slots are now all set is promoted out of
    PENDING_PARAM. Never invents a value; unknown keys are rejected so a typo can't silently
    leave a slot unset."""
    unknown = [k for k in params if k not in crit.params]
    if unknown:
        raise KeyError(f"{crit.id}: unknown parameter(s) {unknown}; declared: {sorted(crit.params)}")
    crit.params.update(params)
    if crit.state == PENDING_PARAM and not crit.unset_params:
        crit.state = GROUNDED
    return crit


def accept_proxy(crit, yes=True):
    """The user's call, never the model's. An accepted proxy counts; an unaccepted one does not."""
    if crit.state != PROXY:
        raise ValueError(f"{crit.id} is {crit.state}, not a PROXY")
    crit.accepted = yes
    return crit


# --- roll-up over a criteria set ----------------------------------------------------------------

def gaps(criteria):
    """Everything standing between this spec and a fully measurable goal. This is the report the
    loop hands back instead of guessing."""
    return {
        "unset_params": {c.id: c.unset_params for c in criteria if c.unset_params},
        "unaccepted_proxies": [c.id for c in criteria if c.state == PROXY and not c.accepted],
        "ungroundable": [c.id for c in criteria if c.state == UNGROUNDABLE],
        "clarifications": sorted({nc for c in criteria for nc in c.clarifications}),
    }


def measurable(criteria):
    """The subset the delta can actually score."""
    return [c for c in criteria if c.evaluable]


def status(criteria, results):
    """results: {criterion_id: bool}. Returns the honest completion picture.

    mechanically_complete -- every evaluable criterion is satisfied AND nothing is still inert. The
                             loop may STOP here; it has done everything a machine can verify.
    signed_off            -- nothing is left that only a human can close.
    complete              -- both. Only then is the goal truly done.

    Keeping these apart is what lets the loop finish without either lying ("done!" with an
    unverifiable criterion open) or running forever (waiting on one).

    INERT CRITERIA BLOCK COMPLETION. This is the correction to a defect found on 2026-09-19: an
    earlier version computed completion over the EVALUABLE subset only, so a criterion frozen by an
    unset parameter was not counted as unmet -- it was silently dropped from the question. Frozen and
    deleted produced byte-identical results, which defeats the entire purpose of freezing a slot:
    the loop reported mechanically complete while an unanswered parameter sat in the spec. A frozen
    criterion is an OPEN QUESTION, not an absent one, so it blocks the machine verdict until it is
    either grounded (ground()) or explicitly accepted (accept_proxy()).

    The distinction from UNGROUNDABLE is deliberate. UNGROUNDABLE means no machine can ever check
    this, so the loop may stop and hand it to a person. INERT means a machine COULD check it as soon
    as somebody supplies a number -- stopping there would be guessing."""
    ev = measurable(criteria)
    unmet = [c.id for c in ev if not results.get(c.id)]
    awaiting = [c.id for c in criteria if c.state == UNGROUNDABLE]
    inert = [c.id for c in criteria if c.unset_params or (c.state == PROXY and not c.accepted)]
    mech = bool(ev) and not unmet and not inert
    return {
        "mechanically_complete": mech,
        "signed_off": not awaiting,
        "complete": mech and not awaiting,
        "unmet": unmet,
        "awaiting_human": awaiting,
        "inert": inert,                 # registered but cannot fire: unset params / unaccepted proxy
        "blocked_by_inert": bool(inert), # explicit: the machine verdict is withheld, not granted
        "measurable_count": len(ev),
        "total": len(criteria),
    }


def report(criteria, results=None):
    """One human-readable block: what can be measured, what cannot, and what is blocking."""
    st = status(criteria, results or {})
    lines = [f"criteria: {st['measurable_count']}/{st['total']} measurable   "
             f"mechanically_complete={st['mechanically_complete']}  signed_off={st['signed_off']}"]
    for c in criteria:
        mark = "x" if (results or {}).get(c.id) else " "
        extra = ""
        if c.unset_params:
            extra = f"  <- UNSET {','.join(c.unset_params)} (frozen, cannot fire)"
        elif c.state == PROXY and not c.accepted:
            extra = "  <- PROXY, not accepted"
        elif c.state == UNGROUNDABLE:
            extra = "  <- human sign-off only"
        lines.append(f"  [{mark}] {c.id:<6} {c.state:<14} {c.quote[:48]!r}{extra}")
    g = gaps(criteria)
    if g["clarifications"]:
        lines.append(f"  open clarifications: {', '.join(g['clarifications'])}")
    return "\n".join(lines)
