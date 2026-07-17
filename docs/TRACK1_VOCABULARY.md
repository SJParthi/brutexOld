# Track 1 — 52-Atomic-Condition Vocabulary

> **Status:** Locked 2026-05-15. Companion to `TRACK1_BACKTEST_SPEC.md §8`.

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

## 16. Post-lock growth register (u128 era)

The 52-condition lock above is the HISTORICAL Phase-1 record. The living
single source of truth for the grown vocabulary is the manifest
`rust/trading-core/conditions.toml` (codegen → `src/bits.rs` +
`brutex/vocabulary/bits.py`); the mask is **u128** since
DEC-VOCAB-U128-CODEGEN-001 (52→60), reshaped to 66 declared bits by the
vocab-66 reshape (VWAP 52/53 tombstoned; CPR 60-63; SuperTrend 64/65
layout-only) and to **74 declared bits** by
**DEC-FIB-TEMPLATE-EXPRESS-001** (MIGRATION.md #1935):

| Bit | Name | Group | Semantics | Band |
|---|---|---|---|---|
| 66-68 | `placeholder_gap_66..68` | Gap Placeholder | RESERVED for DEC-GAP-MIDPOINT-CONDITIONS-001 (#1901) — dropped at the re-stack, never implemented here | — |
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
