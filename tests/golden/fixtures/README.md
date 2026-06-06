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
| 7 | 52-condition truth table | per-condition determinism (G9) | ✅ **landed** (see below) |
| 8 | 12-fill paisa accounting | gross/net/Σ PnL (P-03, P-04) | Phase 2 |
| 9 | combo-hash distinct inputs | hash uniqueness (P-01, P-02) | Phase 2 |

## Landed corpora

### #7 — 52-condition truth table (G9 per-condition determinism)

`../condition_truth_table.csv` (DEC-PHASE2-TRUTH-TABLE-001, MIGRATION entry
#538). A flat, human-auditable `(snippet_id, condition_name, bit_position,
expected_bool, description)` table — the readable projection of the frozen
`vocab_golden_vectors.json` `input → u64` oracle. Every `expected_bool` is
**decoded from the already-proven `expected_u64`** (Golden Rule #1 — no
hand-authored values); the bit↔name mapping comes from the single source of
truth, `brutex/vocabulary/bits.py`.

* Generator: `scripts/gen_condition_truth_table.py` (deterministic; greedy
  both-ways set-cover of the 52 bits, padded to ≥ 200 rows — currently 1,144).
* Golden gate: `../test_condition_truth_table.py` — structure + registry
  agreement + oracle-decode match + G9 both-ways coverage + byte-for-byte
  no-drift (SHA-256 manifest pinned in the test).
* Regenerate: `python scripts/gen_condition_truth_table.py` then update the
  `_EXPECTED_SHA256` manifest in the test.

## Status

📋 ROADMAP (remaining corpora #1–5, #8, #9) — these land alongside the Phase 2
ingest pipeline. Corpus #7 (truth table) is ✅ landed.

## Decision

DEC-PHASE1-GOLDEN-FIXTURES-DIR-001 (MIGRATION entry #251);
DEC-PHASE2-TRUTH-TABLE-001 (MIGRATION entry #538).
