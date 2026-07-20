# Track 1 — 52-Atomic-Condition Vocabulary

> **Status:** Locked 2026-05-15. Companion to `TRACK1_BACKTEST_SPEC.md §8`.
>
> **Grown since lock (the grow-only law — bits 0-51 below are UNCHANGED,
> never renumbered):** the LIVE vocabulary is **69 declared conditions
> (65 ACTIVE) on a u128 mask**, governed by the single-source-of-truth
> manifest `rust/trading-core/conditions.toml` → codegen
> (`scripts/codegen_vocab.py` emits `brutex/vocabulary/bits.py` +
> `rust/trading-core/src/bits.rs`; DEC-VOCAB-U128-CODEGEN-001,
> MIGRATION.md #1010). Post-lock groups: **Group 10 — VWAP Position**
> (bits 52-53, DEC-NIFTY-INDICATORS-001 #1011 — RETIRED tombstones since
> the 60→66 reshape, DEC-VOCAB-66-RESHAPE-IMPL-001 #1804) · **Group 11 —
> Pivot Proximity Extended** (bits 54-55, #1011) · **Group 12 —
> Market Structure** (bits 56-59, DEC-NIFTY-BOS-CHOCH-001, MIGRATION.md
> #1012) · **Group 13 — Central Pivot Range** (bits 60-63, #1804-#1805)
> · **Group 14 — Supertrend** (bits 64-65, LAYOUT-ONLY, #1806) ·
> **Group 15 — Gap Midpoint** (bits 66-68,
> DEC-GAP-MIDPOINT-CONDITIONS-001, MIGRATION.md #1901) — see §16. The
> u64 / 52-bit / "bits 52-63 reserved" arithmetic in the locked body
> below is point-in-time history, kept verbatim; the manifest is the
> live layout authority.

This document enumerates the **52 atomic boolean conditions** that the
brute-force pattern generator combines (K ∈ {2,3,4,5} ANDed) to form
strategy patterns.

Each condition is evaluated on **bar close** and returns a single
boolean. The 52 booleans pack into a u64 mask. A "strategy" = a
required-conditions u64 mask. Detection becomes `(strategy_mask &
bar_mask) == strategy_mask` (bitwise AND + compare per strategy per
bar — ~1 nanosecond on Apple M4).

> **Scope reminder (2026-05-15):** the 52 conditions are evaluated on
> the 3 brute-force instruments only (NIFTY, BANKNIFTY, SENSEX).
> **India VIX is NOT in this vocabulary.** VIX is fetched + stored but
> is captured only as `vix_at_entry` / `vix_at_exit` context columns
> on the result row — never as a pattern input. See
> `TRACK1_BACKTEST_SPEC.md §4.1`. Property P-25 (Phase 1) enforces
> this: no generated pattern mask shall contain any VIX-referencing
> bit.
>
> **Single-TF reminder:** every condition below is evaluated on the
> chosen run's intraday TF only. PDH, PDL, PDC, daily pivots, daily
> Fib anchors, prev_close are scalar daily constants broadcast across
> the day's intraday bars — NOT a parallel multi-TF stream. See
> `TRACK1_BACKTEST_SPEC.md §6.0` for the full single-TF invariant.

---

## 1. Counting Summary

| Group | Count | Bits |
|---|---|---|
| EMA position | 6 | 0-5 |
| Pivot proximity | 7 | 6-12 |
| PDH/PDL relationships | 6 | 13-18 |
| Fib proximity (11 levels) | 11 | 19-29 |
| Candle shape | 7 | 30-36 |
| Prior bar pattern | 3 | 37-39 |
| Day structure | 4 | 40-43 |
| Time of day | 4 | 44-47 |
| Day type | 4 | 48-51 |
| **TOTAL** | **52** | **bits 0-51 of u64** |

Bits 52-63 reserved for future extensions and the direction bit
(packed in the dedup hash, not the bar mask).

---

## 2. Group 1 — EMA Position (bits 0-5, 6 conditions)

| Bit | Condition | Definition |
|---|---|---|
| 0 | `close_above_ema20` | `bar.close > ema20[t]` |
| 1 | `close_below_ema20` | `bar.close < ema20[t]` |
| 2 | `close_above_ema200` | `bar.close > ema200[t]` |
| 3 | `close_below_ema200` | `bar.close < ema200[t]` |
| 4 | `ema20_above_ema200` | `ema20[t] > ema200[t]` (uptrend regime) |
| 5 | `ema20_below_ema200` | `ema20[t] < ema200[t]` (downtrend regime) |

**Conflict pairs (pruned at generation):** (0, 1), (2, 3), (4, 5).
Any pattern containing both members of a pair fires zero trades and is
removed at the pattern-generation stage by the conflict-graph pruner.

---

## 3. Group 2 — Pivot Proximity (bits 6-12, 7 conditions)

Pivots P, R1, R2, R3, S1, S2, S3 computed daily from PDH/PDL/PDC.
"Near" means `abs(bar.close - pivot) ≤ bar.close × proximity_bps /
10000` where `proximity_bps` is a brute-forced parameter ∈ {5, 10, 25,
50}.

| Bit | Condition | Definition |
|---|---|---|
| 6 | `near_pivot_p` | within `pivot_proximity_bps` of P |
| 7 | `near_pivot_r1` | within of R1 |
| 8 | `near_pivot_r2` | within of R2 |
| 9 | `near_pivot_r3` | within of R3 |
| 10 | `near_pivot_s1` | within of S1 |
| 11 | `near_pivot_s2` | within of S2 |
| 12 | `near_pivot_s3` | within of S3 |

**No conflict pairs** within this group (a bar can be near multiple
pivots when proximity is loose).

---

## 4. Group 3 — PDH/PDL Relationships (bits 13-18, 6 conditions)

PDH and PDL anchored to the **previous trading day** (skips holidays).

| Bit | Condition | Definition |
|---|---|---|
| 13 | `close_above_pdh` | `bar.close > pdh[date]` |
| 14 | `close_below_pdh` | `bar.close < pdh[date]` |
| 15 | `close_above_pdl` | `bar.close > pdl[date]` |
| 16 | `close_below_pdl` | `bar.close < pdl[date]` |
| 17 | `near_pdh` | within `pivot_proximity_bps` of pdh[date] |
| 18 | `near_pdl` | within `pivot_proximity_bps` of pdl[date] |

**Conflict pairs:** (13, 14), (15, 16). Pruned at generation.

---

## 5. Group 4 — Fib Proximity (bits 19-29, 11 conditions)

Static Daily Fibonacci levels anchored to the **previous trading day's
H→L range**. Extensions (127.2%, 161.8%, 200%, 261.8%, 423.6%)
projected in the **trade direction** (bull: above 100%, bear: below
0%).

| Bit | Fib Level | Condition |
|---|---|---|
| 19 | 0% | `near_fib_0` |
| 20 | 23.6% | `near_fib_236` |
| 21 | 38.2% | `near_fib_382` |
| 22 | 50% | `near_fib_50` |
| 23 | 61.8% | `near_fib_618` |
| 24 | 78.6% | `near_fib_786` |
| 25 | 100% | `near_fib_100` |
| 26 | 127.2% | `near_fib_1272` |
| 27 | 161.8% | `near_fib_1618` |
| 28 | 200% | `near_fib_200` |
| 29 | 261.8% | `near_fib_2618` |

All use `fib_proximity_bps ∈ {5, 10, 25, 50}` as the tolerance
parameter. The 423.6% extension level is reserved for Phase 2 (rarely
hits in 5-year spot data).

**No conflict pairs** within the group (a bar can be near multiple Fib
levels at loose proximity, though it's unusual).

---

## 6. Group 5 — Candle Shape (bits 30-36, 7 conditions)

Computed from OHLC only, no volume.

| Bit | Condition | Definition |
|---|---|---|
| 30 | `bar_bullish` | `bar.close > bar.open` |
| 31 | `bar_bearish` | `bar.close < bar.open` |
| 32 | `bar_doji` | `abs(bar.close - bar.open) < bar.range × 0.1` (10% body threshold) |
| 33 | `bar_large_body` | `body_pct > 70%` of total range |
| 34 | `bar_small_body` | `body_pct < 30%` of total range |
| 35 | `long_upper_wick` | `upper_wick_pct > 50%` of total range |
| 36 | `long_lower_wick` | `lower_wick_pct > 50%` of total range |

**Conflict pairs:** (30, 31), (30, 32), (31, 32), (33, 34).

---

## 7. Group 6 — Prior Bar Pattern (bits 37-39, 3 conditions)

| Bit | Condition | Definition |
|---|---|---|
| 37 | `prior_n_bullish` | All of bars [t-N, t-1] are bullish, with N parametrised ∈ {1,2,3,4} |
| 38 | `prior_n_bearish` | All of bars [t-N, t-1] are bearish, with N parametrised |
| 39 | `prior_alternating` | Bars [t-N, t-1] alternate bull/bear/bull/bear, with N parametrised |

**Conflict pairs:** (37, 38), (37, 39), (38, 39).

---

## 8. Group 7 — Day Structure (bits 40-43, 4 conditions)

Relative to running intraday day-high/day-low.

| Bit | Condition | Definition |
|---|---|---|
| 40 | `close_at_day_high` | `bar.close == day_high[t]` |
| 41 | `close_at_day_low` | `bar.close == day_low[t]` |
| 42 | `close_in_upper_third` | `bar.close > day_low + (day_high - day_low) × 2/3` |
| 43 | `close_in_lower_third` | `bar.close < day_low + (day_high - day_low) × 1/3` |

**Conflict pairs:** (40, 41), (42, 43), (40, 43), (41, 42).

---

## 9. Group 8 — Time of Day (bits 44-47, 4 conditions)

| Bit | Condition | Window (IST) |
|---|---|---|
| 44 | `early_morning` | 09:16 - 10:00 |
| 45 | `mid_morning` | 10:00 - 11:30 |
| 46 | `midday` | 11:30 - 13:00 |
| 47 | `afternoon` | 13:00 - 15:00 |

**All 4 are mutually exclusive within a single bar.** Pattern
generator may pick at most one. Conflict pairs: all 6 pairs ANDed
together fire zero trades and are pruned.

---

## 10. Group 9 — Day Type (bits 48-51, 4 conditions)

Computed at session start (09:15:00) using previous day's close.

| Bit | Condition | Definition |
|---|---|---|
| 48 | `gap_up_day` | `open[0] > prev_close × 1.005` (0.5%+ gap up) |
| 49 | `gap_down_day` | `open[0] < prev_close × 0.995` (0.5%+ gap down) |
| 50 | `inside_day` | `day_high < pdh AND day_low > pdl` (so far this session) |
| 51 | `outside_day` | `day_high > pdh AND day_low < pdl` (so far this session) |

**Conflict pairs:** (48, 49), (50, 51).

---

## 11. Conflict Graph (pruning at pattern generation)

The pattern generator builds a 52×52 boolean conflict matrix where
`conflict[i, j] = true` if having both bits i and j in a single
strategy is degenerate.

| Conflict Type | Example | Pruned? |
|---|---|---|
| Direct opposition | bit 0 (above EMA20) + bit 1 (below EMA20) | YES — zero trades guaranteed |
| Mutual exclusion (same group) | bit 30 (bullish) + bit 31 (bearish) | YES |
| Independent (orthogonal) | bit 0 (above EMA20) + bit 13 (above PDH) | NO — common pattern |
| Soft correlation | bit 4 (EMA20 > EMA200) + bit 13 (above PDH) | NO — both consistent with uptrend |

Pre-pruned pattern count drops from **2,898,610** (raw `Σ C(52, K)`)
to **~1.2-1.5 million** survivors. Saves ~50% of Stage 1A compute.

---

## 12. Direction Parameter (separate from the 52 bits)

The `direction` parameter (bull / bear) is NOT one of the 52 bits.
It applies AFTER pattern match: same conditions, opposite trade
direction. So one (pattern × direction) tuple is one tested combo;
~1.2M patterns × 2 directions = ~2.4M unique (pattern, direction)
candidates.

Direction is encoded in the dedup hash separately (see
`TRACK1_BACKTEST_SPEC.md §14`).

---

## 13. Implementation (sketch)

```rust
// rust/fno-math/src/vocabulary/atoms.rs (Phase 2)

pub fn evaluate_all_atoms(
    bar: &Bar,
    indicators: &IndicatorSnapshot,
    proximity_bps: ProximityParams,
) -> u64 {
    let mut mask: u64 = 0;
    
    // Group 1 — EMA position
    if bar.close > indicators.ema20 { mask |= 1 << 0; }
    if bar.close < indicators.ema20 { mask |= 1 << 1; }
    // ... and so on for all 52
    
    mask
}

pub fn strategy_matches(strategy_mask: u64, bar_mask: u64) -> bool {
    (strategy_mask & bar_mask) == strategy_mask
}
```

Per-bar evaluation cost: ~50 ns on M4 Pro (one indicator-load + 52
boolean checks + 52 bit-ors). Cached and shared across all strategies
that test against this bar.

Per-strategy match cost: ~1 ns (single u64 AND + CMP). With 8-way
SIMD, 8 strategies per cycle.

---

## 14. Golden Test Coverage

`tests/golden/condition_truth_table.csv` provides ≥ 200 rows of
hand-curated `(input_snippet_id, condition_id, expected_bool)`.
Coverage requirements:

- Every one of 52 atoms appears ≥ 3 times (both `true` and `false` cases)
- Every conflict-pair appears ≥ 2 times (asserting both can't be `true`)
- Every proximity-tolerance value appears ≥ 1 time per atom that uses it

Test failure on any row = fail CI.

---

## 15. Mode B — Exit Vocabulary (separate 10-item list)

For Mode B, the exit-condition vocabulary is documented in
`TRACK1_BACKTEST_SPEC.md §9` and is NOT part of the 52-bit pattern
mask. The 10 exit conditions are stored as a separate 16-bit mask in
the result row (`exit_mask_u16`).

---

## 16. Post-Lock Growth (bits 52-73) — manifest-governed

The vocabulary grew past the 2026-05-15 lock under the grow-only law
(append at the next free bit; never renumber; a bit is
`implemented=true` only WITH its real, tested, look-ahead-clean math).
The manifest `rust/trading-core/conditions.toml` is the single source
of truth (codegen → `src/bits.rs` + `brutex/vocabulary/bits.py`; the
mask is **u128** since DEC-VOCAB-U128-CODEGEN-001); the evaluator's
boolean branches stay hand-written. The declared width is **74 bits**
since **DEC-FIB-TEMPLATE-EXPRESS-001** (MIGRATION.md #1935).

| Group | Bits | Conditions | Decision |
|---|---|---|---|
| 10 — VWAP Position | 52-53 | `close_above_vwap`, `close_below_vwap` — **RETIRED tombstones** since the 60→66 reshape (`reserved = true`; the VWAP/volume wipe — positions held forever, evaluator emits 0) | DEC-NIFTY-INDICATORS-001 #1011; tombstoned by DEC-VOCAB-66-RESHAPE-IMPL-001, MIGRATION.md #1804 |
| 11 — Pivot Proximity Extended | 54-55 | `near_pivot_r5`, `near_pivot_s5` | DEC-NIFTY-INDICATORS-001, MIGRATION.md #1011 |
| 12 — Market Structure | 56-59 | `bos_bullish`, `bos_bearish`, `choch_bullish`, `choch_bearish` (fractal swing state machine, confirmed k bars later) | DEC-NIFTY-BOS-CHOCH-001, MIGRATION.md #1012 |
| 13 — Central Pivot Range | 60-63 | `above_cpr_tc`, `below_cpr_bc`, `inside_cpr`, `narrow_cpr_day` | DEC-CPR-CONDITIONS-001 / DEC-VOCAB-66-RESHAPE-IMPL-001, MIGRATION.md #1804-#1805 |
| 14 — Supertrend | 64-65 | `close_above_supertrend`, `close_below_supertrend` — declared LAYOUT-ONLY (`implemented = false`; math in a later PR) | DEC-SUPERTREND-LAYOUT-001, MIGRATION.md #1806 |
| 15 — Gap Midpoint | 66-68 | `close_above_gap_mid`, `close_below_gap_mid`, `near_gap_mid` | DEC-GAP-MIDPOINT-CONDITIONS-001, MIGRATION.md #1901 |
| 16 — Fibonacci Extended | 69-71 | `near_fib_bull_236`, `near_fib_bull_786`, `near_fib_424` | DEC-FIB-TEMPLATE-EXPRESS-001, MIGRATION.md #1935 |
| 17 — Swing Proximity | 72-73 | `near_swing_high`, `near_swing_low` | DEC-FIB-TEMPLATE-EXPRESS-001, MIGRATION.md #1935 |

### Group 15 — Gap Midpoint (bits 66-68), the per-TF session-edge level

The level is the **gap midpoint** `(X1 + X2) / 2` between two
session-edge candle extremes, per sweep timeframe (each TF anchors on
its OWN session-edge buckets — the spreadsheet's 3-minute construction
is the TF=3minute member, pinned by the spreadsheet parity golden
`tests/golden/test_gap_mid_spreadsheet_parity_golden.py`):

| Bit | Condition | Definition |
|---|---|---|
| 66 | `close_above_gap_mid` | close strictly ABOVE the gap-midpoint level (armed only when a level exists) |
| 67 | `close_below_gap_mid` | close strictly BELOW the level (same arming guard; conflicts with bit 66) |
| 68 | `near_gap_mid` | `is_near(close, gap_mid, gap_mid_proximity_bps)` — default 10 bps; co-fires legally with 66/67 (the PDH/PDL convention) |

Semantics (the binding laws, each test-pinned):

- **X1** = the prior session's LAST completed TF bar's extreme;
  **X2** = today's FIRST completed TF bar's extreme. Gap **direction**
  = the EXACT sign of `open_of_day − pdc` — gap-UP uses the HIGHs,
  gap-DOWN the LOWs; the level is the `round_half_even` paisa midpoint
  (`rust/trading-core/src/indicators/gap_mid.rs::compute_gap_mid`, the
  single source of the math; `brutex/indicators/gap_mid.py` is a thin
  delegator). A ZERO gap (`open_of_day == pdc`) mints NO level — all
  three bits stay 0 all day. Deliberately NO gap-size threshold: bits
  48/49 already carry the 0.5% thresholded day flags and masks compose
  by AND (on a sub-threshold gap day the gap-mid direction and bits
  48/49 legitimately disagree — disclosed, pinned).
- **Arm-from-bar-2:** the bits arm only from the session's SECOND TF
  bar (the first bar both defines X2 and would self-reference). On a
  1day-shaped stream every bar is its session's first TF bar, so the
  bits stay dark BY THE ARMING ORDINAL — the load-bearing 1day abstain
  (`applies_to_1day = false` is declared as metadata but is
  production-dormant on the spot-only engine).
- **Muhurat:** a completed Muhurat session NEVER becomes X1 — the
  prior REGULAR session's edge carries through (the prev-day-anchor
  law). **Contract roll:** the session-edge state joins the Stage-1A
  D11 total reset. **Data hole:** a missing prior-session tail yields
  X1 = the last STORED TF bar (data-as-is; self-heals on re-pull).
- **Coarse-TF trade-inertness (disclosed):** under arm-from-bar-2 and
  the forced-exit law, entry masks gated on bits 66-68 are
  structurally ZERO-TRADE on TF=180/240 (and partially inert on
  TF=120) — the bits stay evaluable there; accepted + pinned.

### Groups 16-17 — Fibonacci Extended + Swing Proximity (bits 69-73), the fib-template express

Five conditions added by **DEC-FIB-TEMPLATE-EXPRESS-001** (MIGRATION.md
#1935) on top of the live Gap Midpoint group (the member's reserved
`placeholder_gap_66..68` rows were dropped verbatim at the re-stack onto
the gap branch, per that DEC's own re-stack obligation — bits 66-68 are
the REAL gap conditions above, never placeholders):

| Bit | Name | Group | Semantics | Band |
|---|---|---|---|---|
| 69 | `near_fib_bull_236` | Fibonacci Extended | close within tolerance of the bullish 23.6% retracement (PDL-anchored) | `fib_proximity_bps` |
| 70 | `near_fib_bull_786` | Fibonacci Extended | close within tolerance of the bullish 78.6% retracement (PDL-anchored) | `fib_proximity_bps` |
| 71 | `near_fib_424` | Fibonacci Extended | close within tolerance of the 424% extension (up OR down variant — the bits-26-29 pair convention) | `fib_proximity_bps` |
| 72 | `near_swing_high` | Swing Proximity | close within tolerance of the last k-confirmed swing high (`None` ⇒ never fires) | `swing_proximity_bps` (default 10, default-omitted from run identity) |
| 73 | `near_swing_low` | Swing Proximity | close within tolerance of the last k-confirmed swing low (`None` ⇒ never fires) | `swing_proximity_bps` |

The swing anchors are read from the SAME look-ahead-clean
`trading_core.MarketStructureState` that resolves the BOS/CHoCH flags
(bits 56-59) — a swing confirms only k bars later, so at bar N the levels
can only be highs/lows of bars ≤ N−k (the P-06 family + the dedicated
suffix-independence property pin it). Bits 69-71 reuse the swept Group-4
`fib_proximity_bps` band and clear with the fib family on anchor-cold days.
