"""The architect seat as a headless CLI, so the loop closes without a human in it.

A local worker (Spark) cannot prompt a live interactive session. It doesn't need to:
the harness runs the architect model as a CLI and feeds it the job context, the worker's
output, and Spark's skeptic (jury) note, then captures the RULING. That is the channel
the whole design turns on -- Spark passes its note to the harness, the harness passes
output+note to the architect CLI, the architect rules.

    from architect import rule
    verdict = rule(job_brief, worker_output, skeptic_note)   # -> "ACCEPT ..." / "REDO ..."

Uses `claude -p` (Opus/Sonnet) by default; `--engine codex` swaps in the Codex CLI.
"""
import argparse, shutil, subprocess

# The judge model. Deliberately a cheap-but-capable tier, NEVER Fable/Opus-5, so a batch of
# jobs doesn't burn credits. One place to change. (Sonnet has been catching the real errors;
# to force Opus 4.8 instead, set its model id here.)
ARCHITECT_MODEL = "sonnet"

TEMPLATE = """You are the architect ruling on a LOCAL WORKER'S result. You do not redo the work.
Decide ACCEPT or REDO in 2-3 sentences, giving the reason. Weigh the skeptic's note: it is the
jury raising doubt, you are the judge. If the skeptic flags a real gap between what was asked and
what the worker actually produced, REDO.

DO NOT re-derive arithmetic yourself -- that wastes tokens. A frozen oracle has already checked the
objectively verifiable figures, and the skeptic has a calculator for the rest; rely on them for
numbers. Never call a figure "fine" or "correct" unless the oracle passed or the skeptic's calc
confirmed it -- if in doubt, say the figure is UNVERIFIED, do not bless it.
When a number, citation, or claim IS wrong, REDO and state it EMPHATICALLY and specifically -- e.g.
"This figure is INCORRECT: X" and name what to fix -- so the worker cannot repeat the same mistake.

The FROZEN ORACLE RESULT below is DETERMINISTIC GROUND TRUTH for THE PROPERTIES IT ACTUALLY CHECKS,
which are listed with it. It really ran (including any tests), so do NOT REDO on a skeptic claim that
contradicts something the oracle DID check (e.g. "no tests present" when it ran them, or a value it
verified) -- trust the oracle there. But a passing oracle proves ONLY what it asserts. If the skeptic
raises a concrete concern about a property OUTSIDE the listed coverage (an untested value, an edge
case the assertions skip, a purpose the checks do not reach), you may NOT dismiss it by pointing at
the pass: weigh it, and if it names a concrete counterexample or an uncovered requirement of the job,
REDO and say what is unverified. Do not, however, invent new requirements beyond the job and its
criteria.

FROZEN ORACLE RESULT:
{oracle}

JOB ASKED:
{brief}

WORKER OUTPUT:
{output}

SKEPTIC (jury) NOTE:
{note}

Rule now. Start your reply with ACCEPT or REDO."""

def rule(brief, output, note, engine="claude", model=ARCHITECT_MODEL, timeout=300, card="", oracle=""):
    header = f"JOB DEFINITION (card):\n{card.strip()}\n\n" if card else ""
    prompt = header + TEMPLATE.format(brief=brief.strip(), output=output.strip(), note=note.strip(),
                                      oracle=(oracle.strip() or "(no oracle on this card)"))
    if engine == "claude":
        exe = shutil.which("claude") or "claude"
        cmd = [exe, "-p", "--model", model, "--output-format", "text"]
    else:
        exe = shutil.which("codex") or "codex"
        cmd = [exe, "exec", "-"]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    out = (r.stdout or "").strip()
    return out or f"(architect CLI gave no text; rc={r.returncode}; {(r.stderr or '')[:200]})"

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--note", required=True)
    ap.add_argument("--engine", default="claude", choices=("claude", "codex"))
    ap.add_argument("--model", default=ARCHITECT_MODEL)
    ap.add_argument("--card", help="path to the job card (spec/done_when), prepended to the ruling prompt")
    a = ap.parse_args()
    from pathlib import Path as _P
    card = _P(a.card).read_text(encoding="utf-8") if a.card else ""
    print(rule(a.brief, a.output, a.note, a.engine, a.model, card=card))
