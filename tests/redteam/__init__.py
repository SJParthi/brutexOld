"""Red-team / planted-bug test harness.

Per `docs/STANDARDS_100PCT.md` §1 and `docs/track2/ASSURANCE_ROUND_2026-05-16.md`,
the operator wanted EMPIRICAL PROOF that each CI gate actually blocks
violations — not just that the gate is configured.

This directory implements the "test the tester" pattern:
- For each CI gate, plant a known violation
- Run the gate tool against the planted violation
- ASSERT the tool returns non-zero exit / flags the violation

If a planted violation goes undetected → the gate is broken → CI is lying.

References (peer-reviewed):
- Rotten Green Tests (Delplanque et al., ICSE 2019) — 294 of 19,905 tests
  had assertions that never executed
- Meta Engineering Sep 2025 — LLM-mutation pipeline finds 1.8× more bugs
  than rule-based mutation
- Trail of Bits Sep 2025 — "Use mutation testing to find the bugs your
  tests don't catch"
- Hypothesis works — integrated shrinking

The single highest-leverage confidence mechanism is THIS directory:
configured ≠ enforcing ≠ effective.
"""
