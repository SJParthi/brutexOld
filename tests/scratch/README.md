# tests/scratch — executable evidence scripts (NOT pytest-collected)

Files here carry no `test_` prefix, so pytest never collects them. Each is a
standalone, committed repro/evidence script run directly with
`python tests/scratch/<name>.py` — the reproducible record behind a ledgered
measurement, never a suite member.

- `overfit_gate_fluke_repro.py` — the DEC-SWEEP-OVERFIT-GATE-DEFAULT-ON-001
  (MIGRATION.md #1961) measured fluke case: DSR 0.1304 / PF 95.1538 / gate
  OFF rank #1 of 38 / gate ON refused / 10 of 37 legit collateral.
