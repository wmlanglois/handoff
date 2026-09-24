"""The loop's dials, in ONE place. Python, not JSON, and no prompt text lives here.

WHY PYTHON. A JSON config cannot carry a comment, and every value below needs to say WHY it is
that number. A dial whose reason is lost gets tuned at random the first time something goes wrong.

WHY NO PROMPTS HERE. Prompts are prose and belong in docs/PROMPTS.md, versioned and diffable as
text. Config that embeds prompts turns every wording change into a config migration, and it hides
the most important part of the system inside a settings file nobody reads.

Override any value per-run without editing this file:
    from loop_config import LOOP
    LOOP = LOOP.replace(max_rounds=8)
"""
from dataclasses import dataclass, replace as _replace


# =================================================================================================
# ROLES
# =================================================================================================
# Three roles, deliberately separated because they fail differently.
#
#   ARCHITECT  plans, decomposes, judges. Frontier model. The expensive one, so it is used for
#              decisions and never for labour.
#   WORKER     does the work. Local model. Cheap enough to run many turns, which is the whole point
#              of owning hardware -- persistence is free here and rationed everywhere else.
#   SKEPTIC    challenges the evidence. Local model, deliberately NOT the worker and deliberately
#              not the architect. It is a JURY, never a judge: it raises doubts, it does not get a
#              vote on acceptance. The moment a skeptic's "passed" substitutes for a real check,
#              it has become another authority and the verification is theatre.
#
# The skeptic is never fed the rationale documents. It reads the artifact and the claim, and it
# checks them against sources. A skeptic that has read the reasoning will agree with the reasoning.

ARCHITECT = "architect"
WORKER = "worker"
SKEPTIC = "skeptic"

# Which fleet worker serves each role. Names must exist in fleet.WORKERS.
# See docs/PROMPTS.md for the model-choice rationale and the field's own recommendations.
ROLE_WORKER = {
    ARCHITECT: None,        # None = the CLI frontier model driving this session, not an HTTP worker
    WORKER: "cluster",      # the 2-Mac MLX cluster: most capable local reasoner, so it does the work
    SKEPTIC: "spark",       # a DIFFERENT model from the worker -- a model will not catch its own
                            # habitual errors, so self-review is close to worthless
}

# Roles that must never be served by the same worker in one round. Enforce, do not merely intend.
MUST_DIFFER = [(WORKER, SKEPTIC)]


# =================================================================================================
# THE LOOP
# =================================================================================================

@dataclass(frozen=True)
class LoopConfig:
    # --- how long a single job may try -----------------------------------------------------------
    max_rounds: int = 5
    # 5, not 3: multi-turn degradation is real but so is one-shot optimism. Observed on this fleet,
    # the first attempt is usually wrong in a way the second fixes; past five the model is
    # rearranging rather than improving.

    keep_context_turns: int = 3
    # The worker KEEPS its conversation for this many turns instead of being re-prompted cold. A
    # cold re-prompt discards everything the model worked out and buys a fresh hallucination.
    # Beyond three turns the context is mostly its own earlier prose, which is when drift starts.

    # --- when to stop trying ----------------------------------------------------------------------
    stagnation_k: int = 3
    # Park after this many consecutive rounds with NO achievement and NO new evidence. Note this
    # counts progress, not activity: rewriting the output every round is churn and does not reset it.

    investigation_budget: int = 6
    # Park after this many rounds if evidence keeps arriving but nothing is ever ACHIEVED. Without
    # this, a loop that varies its tool arguments registers new evidence forever and is immortal.
    # This is the reporter trap's last escape hatch, so it must be closed.

    # --- concurrency and the fragile backend ------------------------------------------------------
    capacity_wait_s: int = 300
    # How long to wait for a worker slot before REFUSING. It refuses rather than admitting: the cap
    # exists because concurrent prefill crashes the MLX cluster, and a refusal is recoverable where
    # an overload is not.

    prefill_lock_wait_s: int = 45
    # Failing to get the prefill lock is a refusal too, never a formality. Proceeding without it
    # runs the exact unserialized prefill the lock exists to prevent.

    job_timeout_s: int = 1800
    lease_ttl_s: int = 1800 + 300
    # MUST exceed job_timeout_s. A lease shorter than the longest job lets a second controller
    # declare a live job abandoned and run it twice, which is the cardinal sin of a durable queue.
    # An earlier 90s lease against 1800s jobs had exactly this hole.

    # --- how much room the worker gets ---------------------------------------------------------
    worker_max_tokens: int = 4096
    # Was a hardcoded 900 in the worker call. A 900-token ceiling cannot hold a working module plus
    # its tests, so the harness was structurally incapable of receiving a complete deliverable and
    # every assignment had to be an errand. The cap shapes the work: ask for an outcome, then leave
    # room for one.

    planner_min_jobs: int = 1
    planner_max_jobs: int = 5
    # The planner asks for "1 to 5 SMALL, checkable jobs". Small is the operative word and it is a
    # choice, not a law. Raising the ceiling alone does not fix it; the PROMPT has to ask for a
    # bounded outcome. See docs/PROMPTS.md.

    # --- acceptance -------------------------------------------------------------------------------
    require_evidence: bool = True
    # Acceptance must present an evidence record binding the worker's output to the accepted
    # artifact. Missing evidence is NOT acceptance; it is a refusal with a reason.

    frozen_criteria_block: bool = True
    # A criterion with an unset parameter is an OPEN QUESTION, not an absent one. It blocks the
    # machine verdict until somebody supplies the number. Frozen must never equal deleted.

    human_only_is_awaiting_review: bool = True
    # Criteria no machine can check do not block the loop and do not count as passed. They get
    # their own outcome so "the machine is finished" is never reported as "the goal is met".

    # --- autonomy leash ---------------------------------------------------------------------------
    leash: str = "batch"
    # every_piece | batch | until_stuck | overnight. Maps to intake question E3.

    overnight_max_hours: int = 8


LOOP = LoopConfig()


def for_leash(leash):
    """Presets. The leash is the operator's risk appetite, so it moves several dials at once."""
    if leash == "every_piece":
        return _replace(LOOP, leash=leash, max_rounds=3, stagnation_k=2, investigation_budget=3)
    if leash == "overnight":
        return _replace(LOOP, leash=leash, max_rounds=6, stagnation_k=3, investigation_budget=8)
    if leash == "until_stuck":
        return _replace(LOOP, leash=leash, max_rounds=6, stagnation_k=4, investigation_budget=8)
    return _replace(LOOP, leash="batch")


def check(cfg=LOOP, role_worker=None):
    """Fail loudly on a configuration that cannot be safe. Called at loop start, not at review
    time, because a misconfigured lease does its damage silently."""
    rw = role_worker or ROLE_WORKER
    problems = []
    if cfg.lease_ttl_s <= cfg.job_timeout_s:
        problems.append(
            "lease_ttl_s ({0}) must exceed job_timeout_s ({1}), or a still-running job can have "
            "its lease expire and be executed twice".format(cfg.lease_ttl_s, cfg.job_timeout_s))
    for a, b in MUST_DIFFER:
        if rw.get(a) is not None and rw.get(a) == rw.get(b):
            problems.append(
                "roles {0} and {1} are both '{2}'; a model does not catch its own habitual errors, "
                "so self-review is not review".format(a, b, rw.get(a)))
    if cfg.investigation_budget and cfg.investigation_budget < cfg.stagnation_k:
        problems.append("investigation_budget below stagnation_k makes stagnation_k unreachable")
    if cfg.keep_context_turns > cfg.max_rounds:
        problems.append("keep_context_turns exceeds max_rounds; context would never be reset")
    if problems:
        raise ValueError("loop_config is unsafe:\n  - " + "\n  - ".join(problems))
    return True
