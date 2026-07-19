# Golden Fixtures

Phase 1 Week 1 scaffolding placeholder per `docs/TRACK1_PHASE_PLAN.md`.

This directory holds byte-frozen test fixtures the golden-master suite
in `tests/golden/` asserts against. Each fixture has a SHA-256 manifest
in the calling test so any unintended drift is caught at test time.

## Planned corpora (per `docs/TRACK1_VERIFICATION_PYRAMID.md`)

| # | Corpus | Purpose | Phase |
|---|---|---|---|
| 1 | NIFTY 50 — 1 trading day (375 bars) | reproducibility round-trip + tick-rounding (P-19, P-21) | ✅ **landed** (see below) |
| 2 | BANKNIFTY — 1 week | session window + holiday-calendar pruning (P-22, P-23) | ✅ **landed** (see below) |
| 3 | SENSEX — circuit-close edge case | forced-exit (P-09) + simulator robustness | ✅ **landed** (see below) |
| 4 | INDIA VIX — 1 week | reference-only data; no signal bits | ✅ **landed** (see below) |
| 5 | Cross-instrument resample (1m → 5m) | resample associativity (P-08) | ✅ **landed** (see below) |
| 7 | 52-condition truth table | per-condition determinism (G9) | ✅ **landed** (see below) |
| 8 | 12-fill paisa accounting | gross/net/Σ PnL (P-03, P-04) | ✅ **landed** (see below) |
| 9 | combo-hash distinct inputs | hash uniqueness (P-01, P-02) | ✅ **landed** (see below) |

## Landed corpora

### #1–5, #9 — Phase 2 engine/resample/combo corpora (DEC-PHASE2-CORPORA-001)

MIGRATION entry #548. Six byte-frozen goldens that each drive a REAL existing
component over a small hand-derived (or translation-invariant / deterministic)
fixture, with the expected values either hand-derived bar-for-bar or pinned as a
SHA-256 / digest manifest (Golden Rule #1 — no hand-authored numbers without a
justification):

* **#1 — `../test_engine_nifty_day_golden.py`** (NIFTY, 375-bar trading day):
  drives `engine.stage1a.build_bar_bits`. Every bar is a flat doji on the first
  IST day, so each `u64` mask is a closed form (`{40,41} ∪ time_bit ∪ {38 if
  i≥3}`) asserted bar-for-bar + pinned by SHA-256; plus the **reproducibility
  round-trip** (batch==batch==stream, P-19) and the **tick-rounding** 2-dp
  ROUND_HALF_UP seam (P-21).
* **#2 — `../test_engine_banknifty_week_golden.py`** (BANKNIFTY, holiday week):
  `calendars.nse.trading_days_between` prunes 2024-08-12→18 to 4 trading days
  (Independence Day + weekend removed, P-23), and `build_bar_bits` walks the
  pruned session-window feed across the IST day-rollover (P-22) — masks
  hand-derived + SHA-256 pinned.
* **#3 — `../test_engine_sensex_forced_exit_golden.py`** (SENSEX): drives
  `engine.trade_sim.simulate_trades` for the 15:20 **forced-exit override**
  (P-09) and the **end-of-data** close-at-last-CLOSE robustness path — both the
  proven Fixture-A windows translated to SENSEX scale (price-difference fields
  invariant under translation).
* **#4 — `../test_india_vix_reference_golden.py`** (INDIA VIX, reference-only):
  `data.tf_resampler` aggregates the VIX week to hand-derived daily bars (the
  reference data path), and `brutex.universe` proves VIX is `reference_only` and
  **excluded** from the brute-force engine — the "no signal bits" guarantee.
* **#5 — `../test_resample_cross_instrument_golden.py`**: drives
  `data.tf_resampler.resample_1m_to_tf` 1m→5m at three instrument price scales
  (hand-derived buckets) + the **associativity** 1m→15m == (1m→5m)→15m (P-08).
* **#9 — `../test_combo_hash_distinct_golden.py`**: byte-freezes the 9-field
  BLAKE3 `strategies.combo.combo_hash` over a base + nine single-field variants —
  determinism (P-01), distinct-inputs→distinct-digests + per-field sensitivity
  (P-02), and param-order invariance.

All six run under CI (3.12 + the `fno_math` / `trading_core` wheels); they were
authored against a local 3.12 venv with both wheels built (the CI-equivalent
oracle).

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

### #8 — 12-fill paisa accounting (P-03 per-trade · P-04 Σ PnL)

`../test_engine_paisa_accounting_golden.py` (DEC-ENGINE-PAISA-ACCOUNTING-GOLDEN-001,
MIGRATION entry #544). Locks the int64-paisa accounting across many back-to-back
round-trips: one `ClosedTrade` per fill with the exact per-trade paisa P&L
(P-03), and the aggregate **Σ PnL as the EXACT integer sum** with no float drift
(P-04). Correct-by-construction: **12 identical copies** of the already-golden
Fixture-A trade 1 from `test_engine_trade_sim_golden.py` (long, take-profit +50p
→ pnl 50 / mfe 60 / mae 10 / holding 2) ⇒ **Σ = 12 × 50 = 600 paisa**. Track-1
spot is signal-only → NO brokerage (CLAUDE.md §7), so net == gross.

### #10 — one-click artifact contract corpus (DEC-OPERATOR-PAGE-REAL-001)

Files live under `tests/fixtures/oneclick/sweep_runs/**` (NOT this directory
— the corpus is a whole `sweep_runs/` directory TREE the dashboard adapter
reads path-shaped, registered HERE per the fixture law; migrated from the
original `runs/` tree under gap-register Ruling 4 /
DEC-ONECLICK-SWEEP-RUNS-REBIND-001, bytes unchanged). CONTRACT-shaped fixtures
for the P4 display build (sweep-parallel-integration-contracts): the P2 run
manifest (`manifest.json`, funnel hand-derived so R44 check #7 reconciles to
the digit), the P3 top-100 leaderboard (`top100.json`, 100 rows over a 3-TF ×
2-direction mini-universe incl. zero-trades / negative-PF / huge-DD /
null-vix-band / metric-tie edge rows), the P2 flight-recorder events JSONL
(118 records, hash-chained via the adapter's OWN `chain_hash` seam —
generator↔verifier SELF-consistency only, algorithm ASSUMED until P2 merges),
the INTERIM fixture-only trades page (`trades/rank-0001.json`, 412 rows,
every 13th VIX null), plus the empty (`r-empty01`) / short (`r-short01`) /
corrupt (`r-corrupt01`, truncated JSON + half-written events tail) / hostile
XSS (`r-hostile01`) twins.

* Generator: `../generate_oneclick_fixture.py` (deterministic pure
  arithmetic; imports `chain_hash` from `brutex.dashboard.oneclick_artifacts`
  so exactly ONE chain-algorithm definition exists).
* Golden gate: `../test_oneclick_fixture_golden.py` — SHA-256 manifest +
  adapter round-trip + end-to-end chain verify + funnel reconcile + per-twin
  named statuses.
* Regenerate: `python tests/golden/generate_oneclick_fixture.py` then update
  the `_EXPECTED_SHA256` manifest in the test.

### Results-store optional-column back-compat corpora (DEC-VIX-STAMP-WIRE-001)

`results_store_pre_vix_columns.parquet` + `results_store_pre_deflated_sharpe.parquet`
— the only *committed Parquet* fixtures (a `.gitignore` exception admits exactly
`tests/golden/fixtures/*.parquet`). They freeze the two HISTORICAL
`brutex/engine/results_store.py` on-disk layouts so the optional-column reader
law is pinned against genuine legacy bytes:

* **`results_store_pre_vix_columns.parquet`** — the exact pre-Stage-C layout
  (ends `… deflated_sharpe, avg_entry_price, avg_exit_price`; NO
  `vix_mean`/`vix_min`/`vix_max`). Byte-fidelity was PROVEN at generation time
  by rendering the same rows through the actual pre-change
  `ResultsStore._render_bytes` (git HEAD before Stage C) — identical SHA-256.
  Loads with the VIX trio `None` (honest unknown — never a fabricated `0.0`)
  while `deflated_sharpe` round-trips (one real value `0.25`, one null).
* **`results_store_pre_deflated_sharpe.parquet`** — one optional-column
  generation older (additionally lacks `deflated_sharpe`; every column the
  current reader REQUIRES is present). Loads with `deflated_sharpe` AND the
  trio `None`.

* Generator: `../generate_results_store_backcompat_fixtures.py`
  (deterministic; real `ResultRow` values + the store's exact determinism
  knobs; the legacy column lists are deliberately FROZEN local copies — a
  committed legacy file never tracks the live schema).
* Golden gate: `../test_results_store_backcompat_golden.py` — SHA-256
  manifest + generator-reproduces-committed-bytes + the loads-as-`None` law +
  the lossless re-flush-to-current-schema migration.
* Regenerate: `.venv/bin/python
  tests/golden/generate_results_store_backcompat_fixtures.py` then update the
  `_EXPECTED_SHA256` manifest in the gate (byte pins are polars/zstd-build
  sensitive, same stance as `test_engine_sweep_run_golden.py`).

## Status

✅ All Phase 2 corpora (#1–5, #7, #8, #9) are landed.
✅ Results-store back-compat corpora (DEC-VIX-STAMP-WIRE-001) are landed.

## Decision

DEC-PHASE1-SMOKE-AND-SKELETONS-001 (MIGRATION entry #251);
DEC-PHASE2-TRUTH-TABLE-001 (MIGRATION entry #538);
DEC-ENGINE-PAISA-ACCOUNTING-GOLDEN-001 (MIGRATION entry #544);
DEC-PHASE2-CORPORA-001 (MIGRATION entry #548);
DEC-VIX-STAMP-WIRE-001 (results-store optional-column back-compat corpora).
