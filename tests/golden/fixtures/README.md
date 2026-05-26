# Golden Fixtures

Phase 1 Week 1 scaffolding placeholder per `docs/TRACK1_PHASE_PLAN.md`.

This directory holds byte-frozen test fixtures the golden-master suite
in `tests/golden/` asserts against. Each fixture has a SHA-256 manifest
in the calling test so any unintended drift is caught at test time.

## Planned corpora (per `docs/TRACK1_VERIFICATION_PYRAMID.md`)

| # | Corpus | Purpose | Phase |
|---|---|---|---|
| 1 | NIFTY 50 — 1 trading day (375 bars × 4 instruments) | reproducibility round-trip + tick-rounding (P-19, P-21) | Phase 2 |
| 2 | BANKNIFTY — 1 week (5 days × 1-minute) | session window + holiday-calendar pruning (P-22, P-23) | Phase 2 |
| 3 | SENSEX — circuit-close edge case | forced-exit (P-09) + simulator robustness | Phase 2 |
| 4 | INDIA VIX — 1 week | reference-only data; no signal bits | Phase 2 |
| 5 | Cross-instrument resample (1m → 5m) | resample associativity (P-08) | Phase 2 |
| 7 | 52-condition truth table | per-condition determinism (G9) | Phase 2 |
| 8 | 12-fill paisa accounting | gross/net/Σ PnL (P-03, P-04) | Phase 2 |
| 9 | combo-hash distinct inputs | hash uniqueness (P-01, P-02) | Phase 2 |

## Status

📋 ROADMAP — fixtures land alongside the Phase 2 ingest pipeline.
Until then this README is a placeholder so the directory exists in git
and the verification pyramid spec has a real path to point at.

## Decision

DEC-PHASE1-GOLDEN-FIXTURES-DIR-001 (MIGRATION entry #251).
