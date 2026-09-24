# Conductor skill

Packet completion is not project completion.

Run this sequence. Do not skip to a packet map and call it done.

1. Approved scope. Bind the entire scoped.md. Its hash is the approval. The North Star stays the goal text.
2. Approved interface map. Components, the public call between them, one owner per file, staged inputs, and one launch command. A person approves it once. A test fixture must be marked fixture. This map is not a Spec Kit roadmap. The converge pattern is the review: compare the candidate to the approved scope and map, and append missing work. Do not edit the scope or a frozen check.
3. Two dependent files. The second calls the first. The check calls that accepted signature.
4. Receipt-bound candidate. After each wave, assemble from accepted worker receipts (`integrate` resolves the hash in `receipt.json`). A second simultaneous writer on a path is refused. A later repair replaces the earlier file.
5. One launchable journey in a fresh process, after the wave, not only when every packet criterion is already met. Packet oracles passing do not pass this gate. DONE also requires that same command to fail when the delivered behavior is replaced by stubs. Promotion records that passing journey.
6. Evidence-based follow-up. One packet, validated, assigned, and left READY on the ledger. Sources are a failed journey, one concrete assembled-candidate finding, or the next approved milestone. Spark receives the journey transcript and only the files it was given; the finding comes from that call.
7. Promoted checkpoint after the journey passes.

Operator command: `python run/conductor.py autonomous <package> --decisions N --seconds N`. Those numbers are the execution budget. `approve-map` is how a person approves a new map.

If a frozen check contradicts the approved requirement, stop and record a check defect. A person approves a versioned correction. The old failure stays in the record.

Name the first consequential failure and act on that cause. A missing file is not an assertion failure. DONE can be reopened by a challenge.

Project work enters through `run/conductor.py`; packet-only completion is not the autonomous project milestone.
