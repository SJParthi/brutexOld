//! Property-based tests for the shared `trading-core` kernel.
//!
//! `DEC-TRADING-CORE-PROPTEST-MUTANTS-001` (`docs/MIGRATION.md`). The crate
//! shipped with only 2 `#[test]`s + the golden answer-key; this adds the
//! property layer the rest of the repo already mandates (CLAUDE.md §4 — 7 test
//! types incl. property + mutation).
//!
//! The properties fall in three families, each chosen to be *robust* (never
//! flaky) AND *mutation-killing* (every comparison / bit / threshold has a case
//! that fails when that operator is perturbed):
//!
//! 1. **Universal invariants** — hold for ALL bounded inputs: determinism,
//!    non-implemented bits (the tombstoned VWAP 52-53 and the reserved
//!    headroom >= 66) always clear,
//!    mutually-exclusive bit pairs, at most one time-of-day bucket, the CPR
//!    partition, the pivot/fib level ladders are monotonic.
//! 2. **Independent oracle parity** — a deliberately separate re-implementation
//!    of the 52 predicates (this file is in `tests/`, which `cargo-mutants`
//!    never mutates, so a mutant in `src/` diverges from this oracle and dies).
//!    Generators are boundary-aware so `<`↔`<=`/`==`↔`!=` mutants are reached.
//! 3. **Golden parity under sampling** — re-evaluating an arbitrary golden case
//!    reproduces its frozen `expected_u64` (leaderboard-safety: the mask that
//!    ranks strategies is bit-stable).
//!
//! Money stays inside the documented finance-safe i64 paisa domain
//! (`src/lib.rs` "honest envelope"): `[1, 10^12]` paisa, whose largest derived
//! value (`pdh + 3.236*range ≈ 4·10^12`) is < 0.05 % of `i64::MAX`, so no
//! property ever trips the release `overflow-checks` panic — exactly the
//! "no panic/overflow on arbitrary (in-domain) inputs" guarantee.

use proptest::prelude::*;
use trading_core::{
    FibLevels, GapDirection, IndicatorSnapshot, MarketStructureSignal, PivotLevels,
    PriorBarHistory, ProximityParams, bits, compute_fib_levels, compute_gap_mid,
    compute_pivot_levels, evaluate_atoms,
};

/// Upper bound for generated paisa magnitudes (₹10 billion). See module docs:
/// chosen so every derived level stays far inside the i64 overflow guard.
const MAX_PAISA: i64 = 1_000_000_000_000;

/// Time-of-day cutoffs (seconds since IST midnight) — mirror of `evaluator.rs`,
/// used by the oracle and to seed boundary-aligned timestamps.
const EARLY_MORNING_START: u32 = 9 * 3600 + 16 * 60;
const EARLY_MORNING_END: u32 = 10 * 3600;
const MID_MORNING_END: u32 = 11 * 3600 + 30 * 60;
const MIDDAY_END: u32 = 13 * 3600;
const AFTERNOON_END: u32 = 15 * 3600;

/// All IMPLEMENTED condition bits set — the union the evaluator can ever
/// produce: bits 0..=51 and 54..=65 (incl. the R5-S5/BOS/CHoCH/CPR/Supertrend
/// groups). Bits 52-53 are the TOMBSTONED VWAP positions — outside
/// IMPLEMENTED_MASK, so the `non_implemented_bits_are_always_clear` property
/// pins them (and the >= 66 headroom) permanently clear.
const FULL_MASK: u128 = bits::IMPLEMENTED_MASK;

// ───────────────────────────── strategies ──────────────────────────────────

/// A valid previous-day OHLC anchor: `pdl <= pdc <= pdh`, all in `[1, MAX]`.
/// (The indicators `assert!` on violations; we only ever feed valid anchors —
/// the corrupt-input panic path is covered by the dedicated unit tests below.)
fn arb_prev_day() -> impl Strategy<Value = (i64, i64, i64)> {
    (1i64..=MAX_PAISA, 0i64..=MAX_PAISA).prop_flat_map(|(pdl, range)| {
        let pdh = pdl + range;
        (Just(pdh), Just(pdl), pdl..=pdh)
    })
}

/// Proximity tolerances spanning 0 bps (degenerate, only exact hits) … 2 %.
fn arb_proximity() -> impl Strategy<Value = ProximityParams> {
    (0i64..=200, 0i64..=200, 0i64..=200, 0i64..=200).prop_map(|(a, b, c, d)| ProximityParams {
        pivot_proximity_bps: a,
        pdh_pdl_proximity_bps: b,
        fib_proximity_bps: c,
        gap_mid_proximity_bps: d,
    })
}

/// Prior-bar window: `prior_n` in 1..=6 with a flag vector whose length is
/// sometimes equal (the active branch) and sometimes not (the early-return
/// branch), so both Group-6 paths are exercised.
fn arb_prior() -> impl Strategy<Value = PriorBarHistory> {
    (1usize..=6, prop::collection::vec(any::<bool>(), 0..=6))
        .prop_map(|(prior_n, flags)| PriorBarHistory { prior_n, flags })
}

/// Time-of-day: a mix of exact bucket cutoffs (boundary mutants on the
/// half-open ranges) and uniform random seconds.
fn arb_tod() -> impl Strategy<Value = u32> {
    prop_oneof![
        Just(0u32),
        Just(EARLY_MORNING_START - 1),
        Just(EARLY_MORNING_START),
        Just(EARLY_MORNING_END - 1),
        Just(EARLY_MORNING_END),
        Just(MID_MORNING_END - 1),
        Just(MID_MORNING_END),
        Just(MIDDAY_END - 1),
        Just(MIDDAY_END),
        Just(AFTERNOON_END - 1),
        Just(AFTERNOON_END),
        Just(86_399u32),
        0u32..=86_399,
    ]
}

/// An EMA reading: `None` (warm-up) or a value at/just-around a reference price
/// so the strict `>`/`<` comparisons see their equality boundary.
fn arb_ema(reference: i64) -> impl Strategy<Value = Option<i64>> {
    prop_oneof![
        Just(None),
        Just(Some(reference)),
        Just(Some(reference - 1)),
        Just(Some(reference + 1)),
        (1i64..=2 * MAX_PAISA).prop_map(Some),
    ]
}

/// A candle constructed from an exact `(range, body, lower_wick, direction)`
/// budget so Group-5 ratio thresholds (`body*10` vs `range*{1,3,7}`,
/// `wick*2` vs `range`) land exactly on their boundary, killing `<`↔`<=`
/// mutants. Returns `(open, high, low, close)`.
fn arb_candle(low_anchor: i64) -> impl Strategy<Value = (i64, i64, i64, i64)> {
    (0i64..=MAX_PAISA, any::<bool>())
        .prop_flat_map(|(range, bull)| {
            // Body candidates hit each ratio boundary plus random.
            let body_opts = prop_oneof![
                Just(0i64),
                Just(range / 10),
                Just(range * 3 / 10),
                Just(range * 7 / 10),
                Just(range),
                0i64..=range.max(0),
            ];
            (Just(range), Just(bull), body_opts)
        })
        .prop_flat_map(|(range, bull, body)| {
            let body = body.clamp(0, range);
            let slack = range - body;
            // Lower-wick candidates hit the wick*2 == range boundary.
            let lw_opts = prop_oneof![
                Just(0i64),
                Just(slack),
                Just((range / 2).min(slack)),
                0i64..=slack.max(0),
            ];
            (Just(range), Just(bull), Just(body), lw_opts)
        })
        .prop_map(move |(range, bull, body, lower_wick)| {
            let lower_wick = lower_wick.clamp(0, range - body);
            let low = low_anchor;
            let high = low + range;
            let (open, close) = if bull {
                let open = low + lower_wick;
                (open, open + body)
            } else {
                let close = low + lower_wick;
                (close + body, close)
            };
            (open, high, low, close)
        })
}

/// A full scenario: consistent pivots/fib derived from a valid prev-day anchor
/// (exactly how the real engine feeds the evaluator), a boundary-aware candle,
/// and day-structure / gap inputs that sometimes coincide with `close`, a
/// level, or `pdc±gap` so Group-7/9 equality and threshold mutants are reached.
fn arb_scenario() -> impl Strategy<Value = (IndicatorSnapshot, ProximityParams, PriorBarHistory)> {
    arb_prev_day()
        .prop_flat_map(|(pdh, pdl, pdc)| {
            let pivots = compute_pivot_levels(pdh, pdl, pdc);
            let fib = compute_fib_levels(pdh, pdl);
            // Anchor the candle near the prev-day band, sometimes exactly on a level
            // so proximity / above-below boundaries are hit.
            let level_choices = prop_oneof![
                Just(pdh),
                Just(pdl),
                Just(pivots.p_paisa),
                Just(pivots.r1_paisa.max(1)),
                Just(pivots.s1_paisa.max(1)),
                Just(fib.retrace_50_paisa),
                Just(((pdh + pdl) / 2).max(1)),
                1i64..=MAX_PAISA,
            ];
            (Just(pivots), Just(fib), Just(pdc), level_choices)
        })
        .prop_flat_map(|(pivots, fib, pdc, low_anchor)| {
            let low_anchor = low_anchor.clamp(1, MAX_PAISA);
            (Just(pivots), Just(fib), Just(pdc), arb_candle(low_anchor))
        })
        .prop_flat_map(|(pivots, fib, pdc, candle)| {
            let (_o, _h, _l, close) = candle;
            // Day structure: day_low <= day_high, sometimes ==close so the
            // close_at_day_high/low equality bits fire. Yields (day_high, day_low).
            let day_strat = prop_oneof![
                Just((close, close)),                // day_high==day_low==close
                (Just(close), 1i64..=2 * MAX_PAISA), // day_low==close
                (1i64..=2 * MAX_PAISA, Just(close)), // day_high==close
                (1i64..=2 * MAX_PAISA, 1i64..=2 * MAX_PAISA),
            ]
            .prop_map(|(a, b)| (a.max(b), a.min(b)));
            // Open-of-day: sometimes exactly at the gap threshold edges.
            let gap_thr = (pdc * 50).div_euclid(10_000);
            let ood_strat = prop_oneof![
                Just((pdc + gap_thr).max(1)),
                Just((pdc + gap_thr + 1).max(1)),
                Just((pdc - gap_thr).max(1)),
                Just((pdc - gap_thr - 1).max(1)),
                1i64..=2 * MAX_PAISA,
            ];
            // Nine elements (nested tuples keep us inside proptest's 12-tuple cap).
            (
                Just(pivots),
                Just(fib),
                Just(candle),
                day_strat,
                ood_strat,
                // ema20 + ema200 share the `arb_ema` around-close strategy
                // (above / below / None) so the Group-1 comparisons see their
                // equality boundary both ways.
                (arb_ema(close), arb_ema(close)),
                arb_tod(),
                arb_proximity(),
                arb_prior(),
                // The four resolved BOS/CHoCH flags (Group 12) — generated
                // independently (each an arbitrary bool) so the oracle-parity
                // property exercises bits 56-59 BOTH set and clear (the
                // look-ahead-clean RESOLUTION of these flags is proven separately
                // by the `MarketStructureState` unit + property tests; here the
                // evaluator's job is only to copy each flag onto its bit).
                arb_market_structure(),
                // The resolved SuperTrend ×2 line (Group 14) — boundary-aware
                // around this candle's close (see `arb_supertrend`).
                arb_supertrend(close),
                // The resolved Group-15 gap-midpoint level
                // (DEC-GAP-MIDPOINT-CONDITIONS-001) — around/at/absent like the
                // EMAs (the stateful X1/X2 RESOLUTION is proven by the
                // stage1a_batch tests; the evaluator's job here is the pure
                // above/below/near verdict), so bits 66-68 are exercised both
                // ways by the property suite.
                arb_ema(close),
            )
        })
        .prop_map(
            |(
                pivots,
                fib,
                candle,
                day,
                open_of_day,
                emas,
                tod_seconds,
                proximity,
                prior,
                market_structure,
                supertrend_x2_paisa,
                gap_mid,
            )| {
                let (open, high, low, close) = candle;
                let (day_high, day_low) = day;
                let (ema20, ema200) = emas;
                let snapshot = IndicatorSnapshot {
                    tod_seconds,
                    open_paisa: open,
                    high_paisa: high,
                    low_paisa: low,
                    close_paisa: close,
                    ema20_paisa: ema20,
                    ema200_paisa: ema200,
                    pivots,
                    fib,
                    day_high_paisa: day_high,
                    day_low_paisa: day_low,
                    open_of_day_paisa: open_of_day,
                    market_structure,
                    supertrend_x2_paisa,
                    gap_mid_paisa: gap_mid,
                };
                (snapshot, proximity, prior)
            },
        )
}

/// Strategy: a resolved SuperTrend line (×2-scaled paisa) around the bar's
/// close so the two STRICT Group-14 comparisons see their equality boundary
/// both ways, plus `None` (warm-up / unwired) and a uniform in-domain value.
/// The streaming derivation of the line is owned + proven by
/// `SupertrendState`'s own unit/property suite; the evaluator's job here is
/// only the exact `close·2` vs line compare.
fn arb_supertrend(close: i64) -> impl Strategy<Value = Option<i64>> {
    prop_oneof![
        Just(None),
        Just(Some(close * 2)),     // the exact equality edge (both bits clear)
        Just(Some(close * 2 - 1)), // half a paisa below ⇒ above-bit fires
        Just(Some(close * 2 + 1)), // half a paisa above ⇒ below-bit fires
        (1i64..=4 * MAX_PAISA).prop_map(Some),
    ]
}

/// Strategy: an arbitrary `MarketStructureSignal` (each of the four resolved
/// BOS/CHoCH flags independently true/false). The look-ahead-clean derivation of
/// these flags is owned + proven by `MarketStructureState`; for the evaluator
/// oracle-parity property the four flags are simply arbitrary booleans so bits
/// 56-59 are exercised both set and clear.
fn arb_market_structure() -> impl Strategy<Value = MarketStructureSignal> {
    (any::<bool>(), any::<bool>(), any::<bool>(), any::<bool>()).prop_map(
        |(bos_bullish, bos_bearish, choch_bullish, choch_bearish)| MarketStructureSignal {
            bos_bullish,
            bos_bearish,
            choch_bullish,
            choch_bearish,
        },
    )
}

// ─────────────────────────── independent oracle ─────────────────────────────

/// Proximity primitive — independent re-derivation of `is_near` (`evaluator.rs`).
fn oracle_is_near(close: i64, level: i64, bps: i64) -> bool {
    if close <= 0 {
        return false;
    }
    let tolerance = (close * bps).div_euclid(10_000);
    (close - level).abs() <= tolerance
}

/// A second, deliberately independent implementation of all 52 conditions, used
/// only as a test oracle. `cargo-mutants` mutates `src/` but never `tests/`, so
/// any mutant in `evaluate_atoms` diverges from this reference and is killed
/// (provided the generators reach the mutated line — they are boundary-aware).
fn oracle(s: &IndicatorSnapshot, p: &ProximityParams, prior: &PriorBarHistory) -> u128 {
    let mut m = 0u128;
    let c = s.close_paisa;
    let pv: &PivotLevels = &s.pivots;
    let fib: &FibLevels = &s.fib;

    // Group 1: EMA position.
    if let Some(e) = s.ema20_paisa {
        if c > e {
            m |= 1u128 << bits::BIT_CLOSE_ABOVE_EMA20;
        }
        if c < e {
            m |= 1u128 << bits::BIT_CLOSE_BELOW_EMA20;
        }
    }
    if let Some(e) = s.ema200_paisa {
        if c > e {
            m |= 1u128 << bits::BIT_CLOSE_ABOVE_EMA200;
        }
        if c < e {
            m |= 1u128 << bits::BIT_CLOSE_BELOW_EMA200;
        }
    }
    if let (Some(e20), Some(e200)) = (s.ema20_paisa, s.ema200_paisa) {
        if e20 > e200 {
            m |= 1u128 << bits::BIT_EMA20_ABOVE_EMA200;
        }
        if e20 < e200 {
            m |= 1u128 << bits::BIT_EMA20_BELOW_EMA200;
        }
    }

    // Group 2: pivot proximity.
    let bp = p.pivot_proximity_bps;
    for (lvl, bit) in [
        (pv.p_paisa, bits::BIT_NEAR_PIVOT_P),
        (pv.r1_paisa, bits::BIT_NEAR_PIVOT_R1),
        (pv.r2_paisa, bits::BIT_NEAR_PIVOT_R2),
        (pv.r3_paisa, bits::BIT_NEAR_PIVOT_R3),
        (pv.s1_paisa, bits::BIT_NEAR_PIVOT_S1),
        (pv.s2_paisa, bits::BIT_NEAR_PIVOT_S2),
        (pv.s3_paisa, bits::BIT_NEAR_PIVOT_S3),
    ] {
        if oracle_is_near(c, lvl, bp) {
            m |= 1 << bit;
        }
    }

    // Group 3: PDH/PDL.
    if c > pv.pdh_paisa {
        m |= 1u128 << bits::BIT_CLOSE_ABOVE_PDH;
    }
    if c < pv.pdh_paisa {
        m |= 1u128 << bits::BIT_CLOSE_BELOW_PDH;
    }
    if c > pv.pdl_paisa {
        m |= 1u128 << bits::BIT_CLOSE_ABOVE_PDL;
    }
    if c < pv.pdl_paisa {
        m |= 1u128 << bits::BIT_CLOSE_BELOW_PDL;
    }
    if oracle_is_near(c, pv.pdh_paisa, p.pdh_pdl_proximity_bps) {
        m |= 1u128 << bits::BIT_NEAR_PDH;
    }
    if oracle_is_near(c, pv.pdl_paisa, p.pdh_pdl_proximity_bps) {
        m |= 1u128 << bits::BIT_NEAR_PDL;
    }

    // Group 4: fib proximity.
    let bf = p.fib_proximity_bps;
    for (lvl, bit) in [
        (fib.retrace_level_at_pdh_paisa, bits::BIT_NEAR_FIB_0),
        (fib.retrace_23_paisa, bits::BIT_NEAR_FIB_236),
        (fib.retrace_38_paisa, bits::BIT_NEAR_FIB_382),
        (fib.retrace_50_paisa, bits::BIT_NEAR_FIB_50),
        (fib.retrace_62_paisa, bits::BIT_NEAR_FIB_618),
        (fib.retrace_78_paisa, bits::BIT_NEAR_FIB_786),
        (fib.retrace_level_at_pdl_paisa, bits::BIT_NEAR_FIB_100),
    ] {
        if oracle_is_near(c, lvl, bf) {
            m |= 1 << bit;
        }
    }
    for (up, down, bit) in [
        (
            fib.ext_up_127_paisa,
            fib.ext_down_127_paisa,
            bits::BIT_NEAR_FIB_1272,
        ),
        (
            fib.ext_up_162_paisa,
            fib.ext_down_162_paisa,
            bits::BIT_NEAR_FIB_1618,
        ),
        (
            fib.ext_up_200_paisa,
            fib.ext_down_200_paisa,
            bits::BIT_NEAR_FIB_200,
        ),
        (
            fib.ext_up_262_paisa,
            fib.ext_down_262_paisa,
            bits::BIT_NEAR_FIB_2618,
        ),
    ] {
        if oracle_is_near(c, up, bf) || oracle_is_near(c, down, bf) {
            m |= 1 << bit;
        }
    }

    // Group 5: candle shape.
    let (o, h, lo) = (s.open_paisa, s.high_paisa, s.low_paisa);
    let body = (c - o).abs();
    let total_range = h - lo;
    if c > o {
        m |= 1u128 << bits::BIT_BAR_BULLISH;
    }
    if c < o {
        m |= 1u128 << bits::BIT_BAR_BEARISH;
    }
    if total_range > 0 && body * 10 < total_range {
        m |= 1u128 << bits::BIT_BAR_DOJI;
    }
    if total_range > 0 {
        if body * 10 > total_range * 7 {
            m |= 1u128 << bits::BIT_BAR_LARGE_BODY;
        }
        if body * 10 < total_range * 3 {
            m |= 1u128 << bits::BIT_BAR_SMALL_BODY;
        }
        if (h - o.max(c)) * 2 > total_range {
            m |= 1u128 << bits::BIT_LONG_UPPER_WICK;
        }
        if (o.min(c) - lo) * 2 > total_range {
            m |= 1u128 << bits::BIT_LONG_LOWER_WICK;
        }
    }

    // Group 6: prior bar pattern.
    if prior.all_bullish() {
        m |= 1u128 << bits::BIT_PRIOR_N_BULLISH;
    }
    if prior.all_bearish() {
        m |= 1u128 << bits::BIT_PRIOR_N_BEARISH;
    }
    if prior.alternating() {
        m |= 1u128 << bits::BIT_PRIOR_ALTERNATING;
    }

    // Group 7: day structure.
    if c == s.day_high_paisa {
        m |= 1u128 << bits::BIT_CLOSE_AT_DAY_HIGH;
    }
    if c == s.day_low_paisa {
        m |= 1u128 << bits::BIT_CLOSE_AT_DAY_LOW;
    }
    let day_range = s.day_high_paisa - s.day_low_paisa;
    if day_range > 0 {
        if (c - s.day_low_paisa) * 3 > day_range * 2 {
            m |= 1u128 << bits::BIT_CLOSE_IN_UPPER_THIRD;
        }
        if (c - s.day_low_paisa) * 3 < day_range {
            m |= 1u128 << bits::BIT_CLOSE_IN_LOWER_THIRD;
        }
    }

    // Group 8: time of day (mutually-exclusive half-open buckets).
    let t = s.tod_seconds;
    if (EARLY_MORNING_START..EARLY_MORNING_END).contains(&t) {
        m |= 1u128 << bits::BIT_EARLY_MORNING;
    } else if (EARLY_MORNING_END..MID_MORNING_END).contains(&t) {
        m |= 1u128 << bits::BIT_MID_MORNING;
    } else if (MID_MORNING_END..MIDDAY_END).contains(&t) {
        m |= 1u128 << bits::BIT_MIDDAY;
    } else if (MIDDAY_END..AFTERNOON_END).contains(&t) {
        m |= 1u128 << bits::BIT_AFTERNOON;
    }

    // Group 9: day type.
    let pdc = pv.pdc_paisa;
    if pdc > 0 {
        let gap = (pdc * 50).div_euclid(10_000);
        if s.open_of_day_paisa > pdc + gap {
            m |= 1u128 << bits::BIT_GAP_UP_DAY;
        }
        if s.open_of_day_paisa < pdc - gap {
            m |= 1u128 << bits::BIT_GAP_DOWN_DAY;
        }
    }
    if s.day_high_paisa < pv.pdh_paisa && s.day_low_paisa > pv.pdl_paisa {
        m |= 1u128 << bits::BIT_INSIDE_DAY;
    }
    if s.day_high_paisa > pv.pdh_paisa && s.day_low_paisa < pv.pdl_paisa {
        m |= 1u128 << bits::BIT_OUTSIDE_DAY;
    }

    // (Group 10, bits 52-53, is the TOMBSTONED VWAP pair — never set.)

    // Group 11: pivot R5/S5 proximity (bits 54-55) — same tolerance as Group 2.
    if oracle_is_near(c, pv.r5_paisa, bp) {
        m |= 1u128 << bits::BIT_NEAR_PIVOT_R5;
    }
    if oracle_is_near(c, pv.s5_paisa, bp) {
        m |= 1u128 << bits::BIT_NEAR_PIVOT_S5;
    }

    // Group 12: BOS/CHoCH market structure (bits 56-59) — the resolved flags are
    // simply copied onto their bits (the look-ahead-clean detection lives in
    // `MarketStructureState`; the evaluator is a pure bit-setter here).
    let ms = &s.market_structure;
    if ms.bos_bullish {
        m |= 1u128 << bits::BIT_BOS_BULLISH;
    }
    if ms.bos_bearish {
        m |= 1u128 << bits::BIT_BOS_BEARISH;
    }
    if ms.choch_bullish {
        m |= 1u128 << bits::BIT_CHOCH_BULLISH;
    }
    if ms.choch_bearish {
        m |= 1u128 << bits::BIT_CHOCH_BEARISH;
    }

    // Group 14: Supertrend (bits 64-65) — independent re-derivation of the
    // strict ×2-domain compare (`None` ⇒ neither bit; equality ⇒ neither).
    if let Some(st) = s.supertrend_x2_paisa {
        let close_x2 = i128::from(c) * 2;
        let st = i128::from(st);
        if close_x2 > st {
            m |= 1u128 << bits::BIT_CLOSE_ABOVE_SUPERTREND;
        }
        if close_x2 < st {
            m |= 1u128 << bits::BIT_CLOSE_BELOW_SUPERTREND;
        }
    }

    // Group 13: Central Pivot Range (bits 60-63) — an independent re-derivation
    // of the Ochoa/PivotBoss formulas (BC = round-half-even midpoint of the raw
    // prev-day H/L; TC = 2·P − BC on the rounded pivot P; canonical tc >= bc).
    let bc_raw = ((pv.pdh_paisa + pv.pdl_paisa) as f64 / 2.0).round_ties_even() as i64;
    let tc_raw = 2 * pv.p_paisa - bc_raw;
    let (tc, bc) = (tc_raw.max(bc_raw), tc_raw.min(bc_raw));
    if c > tc {
        m |= 1u128 << bits::BIT_ABOVE_CPR_TC;
    }
    if c < bc {
        m |= 1u128 << bits::BIT_BELOW_CPR_BC;
    }
    if c >= bc && c <= tc {
        m |= 1u128 << bits::BIT_INSIDE_CPR;
    }
    if pv.p_paisa > 0 && (i128::from(tc) - i128::from(bc)) * 10_000 <= i128::from(pv.p_paisa) * 30 {
        m |= 1u128 << bits::BIT_NARROW_CPR_DAY;
    }

    // Group 15: gap midpoint (bits 66-68) — the resolved optional level
    // (DEC-GAP-MIDPOINT-CONDITIONS-001).
    if let Some(gm) = s.gap_mid_paisa {
        if c > gm {
            m |= 1u128 << bits::BIT_CLOSE_ABOVE_GAP_MID;
        }
        if c < gm {
            m |= 1u128 << bits::BIT_CLOSE_BELOW_GAP_MID;
        }
        if oracle_is_near(c, gm, p.gap_mid_proximity_bps) {
            m |= 1u128 << bits::BIT_NEAR_GAP_MID;
        }
    }

    m
}

// ───────────────────────────── properties ───────────────────────────────────

proptest! {
    // Run a generous case budget — the generators are cheap and boundary-heavy.
    // `max_shrink_time` bounds shrinking so that when a property fails (e.g. when
    // cargo-mutants perturbs the evaluator) the test binary EXITS FAST instead of
    // shrinking the deep nested strategy for minutes — otherwise a detected
    // mutant is mis-classified as a timeout. `failure_persistence: None` keeps
    // the run from writing `*.proptest-regressions` scratch (the only seeds ever
    // generated here were mutant-induced, not real failures).
    #![proptest_config(ProptestConfig {
        cases: 4096,
        max_shrink_time: 3_000,
        failure_persistence: None,
        ..ProptestConfig::default()
    })]

    /// The evaluator is a pure function: identical inputs → identical mask, and
    /// it never panics for any in-domain scenario (this is also the
    /// "no panic/overflow on arbitrary inputs" guarantee).
    #[test]
    fn evaluator_is_deterministic_and_total((s, p, prior) in arb_scenario()) {
        let a = evaluate_atoms(&s, &p, &prior);
        let b = evaluate_atoms(&s, &p, &prior);
        prop_assert_eq!(a, b);
    }

    /// Every bit outside IMPLEMENTED_MASK must always be clear: the TOMBSTONED
    /// VWAP bits 52-53 AND the reserved u128 headroom (>= 66).
    #[test]
    fn non_implemented_bits_are_always_clear((s, p, prior) in arb_scenario()) {
        let mask = evaluate_atoms(&s, &p, &prior);
        prop_assert_eq!(mask & !FULL_MASK, 0u128);
    }

    /// The evaluator must equal the independent oracle on every scenario. This
    /// is the workhorse mutation-killer: it pins the exact predicate of every
    /// implemented bit, and the boundary-aware generators reach each
    /// comparison's edge.
    #[test]
    fn evaluator_matches_independent_oracle((s, p, prior) in arb_scenario()) {
        prop_assert_eq!(evaluate_atoms(&s, &p, &prior), oracle(&s, &p, &prior));
    }

    /// Logically-opposite bits can never be set together.
    #[test]
    fn opposite_bits_are_mutually_exclusive((s, p, prior) in arb_scenario()) {
        let m = evaluate_atoms(&s, &p, &prior);
        let pair = |a: u32, b: u32| (m >> a) & (m >> b) & 1;
        prop_assert_eq!(pair(bits::BIT_CLOSE_ABOVE_EMA20, bits::BIT_CLOSE_BELOW_EMA20), 0);
        prop_assert_eq!(pair(bits::BIT_CLOSE_ABOVE_EMA200, bits::BIT_CLOSE_BELOW_EMA200), 0);
        prop_assert_eq!(pair(bits::BIT_EMA20_ABOVE_EMA200, bits::BIT_EMA20_BELOW_EMA200), 0);
        prop_assert_eq!(pair(bits::BIT_CLOSE_ABOVE_PDH, bits::BIT_CLOSE_BELOW_PDH), 0);
        prop_assert_eq!(pair(bits::BIT_CLOSE_ABOVE_PDL, bits::BIT_CLOSE_BELOW_PDL), 0);
        prop_assert_eq!(pair(bits::BIT_BAR_BULLISH, bits::BIT_BAR_BEARISH), 0);
        prop_assert_eq!(pair(bits::BIT_CLOSE_IN_UPPER_THIRD, bits::BIT_CLOSE_IN_LOWER_THIRD), 0);
        prop_assert_eq!(pair(bits::BIT_GAP_UP_DAY, bits::BIT_GAP_DOWN_DAY), 0);
        prop_assert_eq!(pair(bits::BIT_INSIDE_DAY, bits::BIT_OUTSIDE_DAY), 0);
        prop_assert_eq!(
            pair(bits::BIT_CLOSE_ABOVE_SUPERTREND, bits::BIT_CLOSE_BELOW_SUPERTREND),
            0
        );
        prop_assert_eq!(pair(bits::BIT_ABOVE_CPR_TC, bits::BIT_BELOW_CPR_BC), 0);
        prop_assert_eq!(pair(bits::BIT_ABOVE_CPR_TC, bits::BIT_INSIDE_CPR), 0);
        prop_assert_eq!(pair(bits::BIT_BELOW_CPR_BC, bits::BIT_INSIDE_CPR), 0);
        prop_assert_eq!(
            pair(bits::BIT_CLOSE_ABOVE_GAP_MID, bits::BIT_CLOSE_BELOW_GAP_MID),
            0
        );
    }

    /// The three CPR position bits (60-62) form an EXACT PARTITION: with the
    /// canonicalized band (tc >= bc), any close is strictly above TC, strictly
    /// below BC, or inside the inclusive band — exactly one of the three fires
    /// on EVERY scenario (never zero, never two).
    #[test]
    fn cpr_position_bits_form_an_exact_partition((s, p, prior) in arb_scenario()) {
        let m = evaluate_atoms(&s, &p, &prior);
        let cpr_pos = (m >> bits::BIT_ABOVE_CPR_TC) & 0b111;
        prop_assert_eq!(cpr_pos.count_ones(), 1, "CPR position bits = {:#05b}", cpr_pos);
    }

    /// The CPR band is ORDERED post-canonicalization (G-CPRCOR-07): for ANY
    /// prev-day anchor triple — including the roughly-half of real days whose
    /// RAW pair inverts (tc_raw < bc_raw) — `compute_cpr_levels` returns
    /// tc >= bc, the precondition the 60/61/62 trichotomy above stands on.
    #[test]
    fn cpr_band_is_ordered(
        p in 1i64..=1_000_000_000_000i64,
        pdh in 1i64..=1_000_000_000_000i64,
        pdl in 1i64..=1_000_000_000_000i64,
    ) {
        let cpr = trading_core::compute_cpr_levels(p, pdh, pdl);
        prop_assert!(
            cpr.tc_paisa >= cpr.bc_paisa,
            "canonicalized CPR must satisfy tc >= bc, got tc={} bc={}",
            cpr.tc_paisa,
            cpr.bc_paisa
        );
    }

    /// At most one Group-8 time-of-day bucket fires (the `else if` chain).
    #[test]
    fn at_most_one_time_bucket((s, p, prior) in arb_scenario()) {
        let m = evaluate_atoms(&s, &p, &prior);
        let tod = (m >> bits::BIT_EARLY_MORNING) & 0b1111;
        prop_assert!(tod.count_ones() <= 1, "tod buckets = {tod:#06b}");
    }

    /// `compute_pivot_levels` is deterministic and produces a monotonic ladder
    /// `s4 <= s3 <= s2 <= s1 <= p <= r1 <= r2 <= r3 <= r4` for any valid anchor.
    #[test]
    fn pivot_ladder_is_monotonic_and_deterministic((pdh, pdl, pdc) in arb_prev_day()) {
        let a = compute_pivot_levels(pdh, pdl, pdc);
        let b = compute_pivot_levels(pdh, pdl, pdc);
        prop_assert_eq!(a, b);
        prop_assert!(a.s4_paisa <= a.s3_paisa);
        prop_assert!(a.s3_paisa <= a.s2_paisa);
        prop_assert!(a.s2_paisa <= a.s1_paisa);
        prop_assert!(a.s1_paisa <= a.p_paisa);
        prop_assert!(a.p_paisa <= a.r1_paisa);
        prop_assert!(a.r1_paisa <= a.r2_paisa);
        prop_assert!(a.r2_paisa <= a.r3_paisa);
        prop_assert!(a.r3_paisa <= a.r4_paisa);
        // The central pivot stays inside the prior-day range.
        prop_assert!(a.p_paisa >= pdl && a.p_paisa <= pdh);
        // Stored prev-day anchors are echoed verbatim.
        prop_assert_eq!(a.pdh_paisa, pdh);
        prop_assert_eq!(a.pdl_paisa, pdl);
        prop_assert_eq!(a.pdc_paisa, pdc);
    }

    /// `compute_fib_levels` is deterministic and produces an ordered retracement
    /// ladder bounded by the extensions, for any valid anchor.
    #[test]
    fn fib_ladder_is_ordered_and_deterministic((pdh, pdl, _pdc) in arb_prev_day()) {
        let a = compute_fib_levels(pdh, pdl);
        let b = compute_fib_levels(pdh, pdl);
        prop_assert_eq!(a, b);
        // Retracements: pdl <= 78% <= 62% <= 50% <= 38% <= 23% <= pdh.
        prop_assert!(a.retrace_level_at_pdl_paisa <= a.retrace_78_paisa);
        prop_assert!(a.retrace_78_paisa <= a.retrace_62_paisa);
        prop_assert!(a.retrace_62_paisa <= a.retrace_50_paisa);
        prop_assert!(a.retrace_50_paisa <= a.retrace_38_paisa);
        prop_assert!(a.retrace_38_paisa <= a.retrace_23_paisa);
        prop_assert!(a.retrace_23_paisa <= a.retrace_level_at_pdh_paisa);
        // BULLISH ladder (PDL-anchored, DEC-FIB-BULLISH-RETRACE-001):
        // pdl <= bull23 <= bull38 <= 50% <= bull62 <= bull78 <= pdh — the
        // shared retrace_50 IS the bullish midpoint (no separate bull_50).
        prop_assert!(pdl <= a.retrace_bull_23_paisa);
        prop_assert!(a.retrace_bull_23_paisa <= a.retrace_bull_38_paisa);
        prop_assert!(a.retrace_bull_38_paisa <= a.retrace_50_paisa);
        prop_assert!(a.retrace_50_paisa <= a.retrace_bull_62_paisa);
        prop_assert!(a.retrace_bull_62_paisa <= a.retrace_bull_78_paisa);
        prop_assert!(a.retrace_bull_78_paisa <= pdh);
        // Stored anchors + range field.
        prop_assert_eq!(a.pdh_paisa, pdh);
        prop_assert_eq!(a.pdl_paisa, pdl);
        prop_assert_eq!(a.range_paisa, pdh - pdl);
        prop_assert_eq!(a.retrace_level_at_pdh_paisa, pdh);
        prop_assert_eq!(a.retrace_level_at_pdl_paisa, pdl);
        // Up extensions sit above pdh and ascend 127 ≤ 162 ≤ 200 ≤ 262 ≤ 424;
        // down extensions sit below pdl and descend (incl. the unused 424 leg,
        // which only the property tests observe).
        prop_assert!(a.ext_up_127_paisa >= pdh);
        prop_assert!(a.ext_up_127_paisa <= a.ext_up_162_paisa);
        prop_assert!(a.ext_up_162_paisa <= a.ext_up_200_paisa);
        prop_assert!(a.ext_up_200_paisa <= a.ext_up_262_paisa);
        prop_assert!(a.ext_up_262_paisa <= a.ext_up_424_paisa);
        prop_assert!(a.ext_down_127_paisa <= pdl);
        prop_assert!(a.ext_down_162_paisa <= a.ext_down_127_paisa);
        prop_assert!(a.ext_down_262_paisa <= a.ext_down_200_paisa);
        prop_assert!(a.ext_down_200_paisa <= a.ext_down_162_paisa);
        prop_assert!(a.ext_down_424_paisa <= a.ext_down_262_paisa);
    }

    /// The bullish retracements are a REAL PDL-anchored ladder, NOT the
    /// reversed bearish (PDH-anchored) ladder: Fibonacci ratios are asymmetric
    /// (`0.236 + 0.786 = 1.022 ≠ 1.000`), so for a non-degenerate range the
    /// bull 23/78 rungs sit at DIFFERENT prices than bearish 78/23 — the teeth
    /// that kill a revert-to-alias mutant (DEC-FIB-BULLISH-RETRACE-001). The
    /// gap is `0.022 × range`, so a `range ≥ 100` paisa (₹1) guarantees the
    /// rounded values differ (0.022·100 = 2.2 > the ≤1 rounding slop); the
    /// 38/62 pair coincides ONLY because `0.382 + 0.618 = 1.000` exactly in
    /// IEEE — an observation, never relied on, so NOT pinned here.
    #[test]
    fn bullish_retracements_are_not_bearish_aliases(
        pdl in 1i64..=MAX_PAISA,
        range in 100i64..=MAX_PAISA,
    ) {
        let pdh = pdl + range;
        let a = compute_fib_levels(pdh, pdl);
        // bull 23% (pdl + 0.236·r) != bearish 78% (pdh − 0.786·r = pdl + 0.214·r)
        prop_assert_ne!(a.retrace_bull_23_paisa, a.retrace_78_paisa);
        // bull 78% (pdl + 0.786·r) != bearish 23% (pdh − 0.236·r = pdl + 0.764·r)
        prop_assert_ne!(a.retrace_bull_78_paisa, a.retrace_23_paisa);
        // And the bull ladder is anchored bottom-up: 23% sits in the LOWER
        // half, 78% in the UPPER half of the band.
        prop_assert!(a.retrace_bull_23_paisa <= a.retrace_50_paisa);
        prop_assert!(a.retrace_bull_78_paisa >= a.retrace_50_paisa);
    }
}

// ──────────────────── deterministic per-bit reachability ─────────────────────

/// Every IMPLEMENTED bit is *individually* reachable: a hand-built scenario
/// forces exactly that condition true and the bit fires. This is non-flaky
/// (no RNG), proves reachability for the whole implemented set (complementing
/// the golden union test), and is a sharp mutation-killer — a deleted
/// `mask |= 1 << BIT_X` statement, or a flipped comparison for that bit,
/// makes its case fail.
#[test]
fn every_implemented_bit_is_individually_reachable() {
    // Anchor chosen so ALL pivot + fib levels are strictly positive (so the
    // `close <= 0` short-circuit in `is_near` never masks a proximity case).
    let pivots = compute_pivot_levels(1_100_000, 1_000_000, 1_060_000);
    let fib = compute_fib_levels(1_100_000, 1_000_000);
    let base = IndicatorSnapshot {
        tod_seconds: 0,
        open_paisa: 1_050_000,
        high_paisa: 1_050_000,
        low_paisa: 1_050_000,
        close_paisa: 1_050_000,
        ema20_paisa: None,
        ema200_paisa: None,
        pivots,
        fib,
        day_high_paisa: 1_050_000,
        day_low_paisa: 1_050_000,
        open_of_day_paisa: 1_050_000,
        market_structure: MarketStructureSignal::default(),
        supertrend_x2_paisa: None,
        gap_mid_paisa: None,
    };
    let prox = ProximityParams {
        pivot_proximity_bps: 10,
        pdh_pdl_proximity_bps: 10,
        fib_proximity_bps: 10,
        gap_mid_proximity_bps: 10,
    };
    let no_prior = PriorBarHistory {
        prior_n: 3,
        flags: vec![],
    };

    let mut seen: u128 = 0;
    let mut check = |bit: u32, s: IndicatorSnapshot, pr: &PriorBarHistory| {
        let m = evaluate_atoms(&s, &prox, pr);
        assert!(
            (m >> bit) & 1 == 1,
            "bit {bit} should be set but mask = {m:#054b}"
        );
        seen |= 1 << bit;
    };

    // Group 1: EMA position.
    check(
        bits::BIT_CLOSE_ABOVE_EMA20,
        IndicatorSnapshot {
            close_paisa: 1_200_000,
            ema20_paisa: Some(1_000_000),
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_CLOSE_BELOW_EMA20,
        IndicatorSnapshot {
            close_paisa: 1_000_000,
            ema20_paisa: Some(1_200_000),
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_CLOSE_ABOVE_EMA200,
        IndicatorSnapshot {
            close_paisa: 1_200_000,
            ema200_paisa: Some(1_000_000),
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_CLOSE_BELOW_EMA200,
        IndicatorSnapshot {
            close_paisa: 1_000_000,
            ema200_paisa: Some(1_200_000),
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_EMA20_ABOVE_EMA200,
        IndicatorSnapshot {
            ema20_paisa: Some(1_200_000),
            ema200_paisa: Some(1_000_000),
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_EMA20_BELOW_EMA200,
        IndicatorSnapshot {
            ema20_paisa: Some(1_000_000),
            ema200_paisa: Some(1_200_000),
            ..base
        },
        &no_prior,
    );

    // Group 2: pivot proximity (close exactly on each level).
    check(
        bits::BIT_NEAR_PIVOT_P,
        IndicatorSnapshot {
            close_paisa: pivots.p_paisa,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_PIVOT_R1,
        IndicatorSnapshot {
            close_paisa: pivots.r1_paisa,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_PIVOT_R2,
        IndicatorSnapshot {
            close_paisa: pivots.r2_paisa,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_PIVOT_R3,
        IndicatorSnapshot {
            close_paisa: pivots.r3_paisa,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_PIVOT_S1,
        IndicatorSnapshot {
            close_paisa: pivots.s1_paisa,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_PIVOT_S2,
        IndicatorSnapshot {
            close_paisa: pivots.s2_paisa,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_PIVOT_S3,
        IndicatorSnapshot {
            close_paisa: pivots.s3_paisa,
            ..base
        },
        &no_prior,
    );

    // Group 3: PDH/PDL.
    check(
        bits::BIT_CLOSE_ABOVE_PDH,
        IndicatorSnapshot {
            close_paisa: 1_200_000,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_CLOSE_BELOW_PDH,
        IndicatorSnapshot {
            close_paisa: 1_050_000,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_CLOSE_ABOVE_PDL,
        IndicatorSnapshot {
            close_paisa: 1_050_000,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_CLOSE_BELOW_PDL,
        IndicatorSnapshot {
            close_paisa: 950_000,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_PDH,
        IndicatorSnapshot {
            close_paisa: 1_100_000,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_PDL,
        IndicatorSnapshot {
            close_paisa: 1_000_000,
            ..base
        },
        &no_prior,
    );

    // Group 4: fib proximity.
    check(
        bits::BIT_NEAR_FIB_0,
        IndicatorSnapshot {
            close_paisa: fib.retrace_level_at_pdh_paisa,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_FIB_236,
        IndicatorSnapshot {
            close_paisa: fib.retrace_23_paisa,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_FIB_382,
        IndicatorSnapshot {
            close_paisa: fib.retrace_38_paisa,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_FIB_50,
        IndicatorSnapshot {
            close_paisa: fib.retrace_50_paisa,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_FIB_618,
        IndicatorSnapshot {
            close_paisa: fib.retrace_62_paisa,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_FIB_786,
        IndicatorSnapshot {
            close_paisa: fib.retrace_78_paisa,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_FIB_100,
        IndicatorSnapshot {
            close_paisa: fib.retrace_level_at_pdl_paisa,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_FIB_1272,
        IndicatorSnapshot {
            close_paisa: fib.ext_up_127_paisa,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_FIB_1618,
        IndicatorSnapshot {
            close_paisa: fib.ext_up_162_paisa,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_FIB_200,
        IndicatorSnapshot {
            close_paisa: fib.ext_up_200_paisa,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_FIB_2618,
        IndicatorSnapshot {
            close_paisa: fib.ext_up_262_paisa,
            ..base
        },
        &no_prior,
    );

    // Group 5: candle shape.
    check(
        bits::BIT_BAR_BULLISH,
        IndicatorSnapshot {
            open_paisa: 1_000_000,
            close_paisa: 1_100_000,
            high_paisa: 1_100_000,
            low_paisa: 1_000_000,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_BAR_BEARISH,
        IndicatorSnapshot {
            open_paisa: 1_100_000,
            close_paisa: 1_000_000,
            high_paisa: 1_100_000,
            low_paisa: 1_000_000,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_BAR_DOJI,
        IndicatorSnapshot {
            open_paisa: 1_050_000,
            close_paisa: 1_050_000,
            high_paisa: 1_100_000,
            low_paisa: 1_000_000,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_BAR_LARGE_BODY,
        IndicatorSnapshot {
            open_paisa: 1_000_000,
            close_paisa: 1_080_000,
            high_paisa: 1_080_000,
            low_paisa: 1_000_000,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_BAR_SMALL_BODY,
        IndicatorSnapshot {
            open_paisa: 1_000_000,
            close_paisa: 1_010_000,
            high_paisa: 1_100_000,
            low_paisa: 1_000_000,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_LONG_UPPER_WICK,
        IndicatorSnapshot {
            open_paisa: 1_000_000,
            close_paisa: 1_000_000,
            high_paisa: 1_100_000,
            low_paisa: 1_000_000,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_LONG_LOWER_WICK,
        IndicatorSnapshot {
            open_paisa: 1_100_000,
            close_paisa: 1_100_000,
            high_paisa: 1_100_000,
            low_paisa: 1_000_000,
            ..base
        },
        &no_prior,
    );

    // Group 6: prior bar pattern.
    check(
        bits::BIT_PRIOR_N_BULLISH,
        base,
        &PriorBarHistory {
            prior_n: 3,
            flags: vec![true, true, true],
        },
    );
    check(
        bits::BIT_PRIOR_N_BEARISH,
        base,
        &PriorBarHistory {
            prior_n: 3,
            flags: vec![false, false, false],
        },
    );
    check(
        bits::BIT_PRIOR_ALTERNATING,
        base,
        &PriorBarHistory {
            prior_n: 3,
            flags: vec![true, false, true],
        },
    );

    // Group 7: day structure.
    check(
        bits::BIT_CLOSE_AT_DAY_HIGH,
        IndicatorSnapshot {
            close_paisa: 1_050_000,
            day_high_paisa: 1_050_000,
            day_low_paisa: 1_000_000,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_CLOSE_AT_DAY_LOW,
        IndicatorSnapshot {
            close_paisa: 1_000_000,
            day_high_paisa: 1_100_000,
            day_low_paisa: 1_000_000,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_CLOSE_IN_UPPER_THIRD,
        IndicatorSnapshot {
            close_paisa: 1_080_000,
            day_high_paisa: 1_100_000,
            day_low_paisa: 1_000_000,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_CLOSE_IN_LOWER_THIRD,
        IndicatorSnapshot {
            close_paisa: 1_020_000,
            day_high_paisa: 1_100_000,
            day_low_paisa: 1_000_000,
            ..base
        },
        &no_prior,
    );

    // Group 8: time of day (each bucket's lower edge).
    check(
        bits::BIT_EARLY_MORNING,
        IndicatorSnapshot {
            tod_seconds: EARLY_MORNING_START,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_MID_MORNING,
        IndicatorSnapshot {
            tod_seconds: EARLY_MORNING_END,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_MIDDAY,
        IndicatorSnapshot {
            tod_seconds: MID_MORNING_END,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_AFTERNOON,
        IndicatorSnapshot {
            tod_seconds: MIDDAY_END,
            ..base
        },
        &no_prior,
    );

    // Group 9: day type.
    check(
        bits::BIT_GAP_UP_DAY,
        IndicatorSnapshot {
            open_of_day_paisa: 1_070_000,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_GAP_DOWN_DAY,
        IndicatorSnapshot {
            open_of_day_paisa: 1_050_000,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_INSIDE_DAY,
        IndicatorSnapshot {
            day_high_paisa: 1_090_000,
            day_low_paisa: 1_010_000,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_OUTSIDE_DAY,
        IndicatorSnapshot {
            day_high_paisa: 1_110_000,
            day_low_paisa: 990_000,
            ..base
        },
        &no_prior,
    );

    // (Group 10, bits 52-53, is the TOMBSTONED VWAP pair — reachability is
    // deliberately IMPOSSIBLE; `non_implemented_bits_are_always_clear` +
    // `tombstoned_vwap_bits_never_fire` own that direction.)

    // Group 11: pivot R5/S5 proximity (bits 54-55). close exactly ON R5 / S5.
    check(
        bits::BIT_NEAR_PIVOT_R5,
        IndicatorSnapshot {
            close_paisa: pivots.r5_paisa,
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_PIVOT_S5,
        IndicatorSnapshot {
            close_paisa: pivots.s5_paisa,
            ..base
        },
        &no_prior,
    );

    // Group 12: BOS/CHoCH market structure (bits 56-59). Each resolved flag maps
    // directly onto its bit (the look-ahead-clean derivation is `MarketStructureState`'s
    // job, proven there; here we only assert the evaluator surfaces each flag).
    check(
        bits::BIT_BOS_BULLISH,
        IndicatorSnapshot {
            market_structure: MarketStructureSignal {
                bos_bullish: true,
                ..Default::default()
            },
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_BOS_BEARISH,
        IndicatorSnapshot {
            market_structure: MarketStructureSignal {
                bos_bearish: true,
                ..Default::default()
            },
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_CHOCH_BULLISH,
        IndicatorSnapshot {
            market_structure: MarketStructureSignal {
                choch_bullish: true,
                ..Default::default()
            },
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_CHOCH_BEARISH,
        IndicatorSnapshot {
            market_structure: MarketStructureSignal {
                choch_bearish: true,
                ..Default::default()
            },
            ..base
        },
        &no_prior,
    );

    // Group 13: Central Pivot Range (bits 60-63). For the base anchor
    // (1_100_000 / 1_000_000 / 1_060_000): P = round(3_160_000/3) = 1_053_333,
    // BC = round(2_100_000/2) = 1_050_000, TC = 2·P − BC = 1_056_666.
    check(
        bits::BIT_ABOVE_CPR_TC,
        IndicatorSnapshot {
            close_paisa: 1_060_000, // > TC 1_056_666
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_BELOW_CPR_BC,
        IndicatorSnapshot {
            close_paisa: 1_000_000, // < BC 1_050_000
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_INSIDE_CPR,
        IndicatorSnapshot {
            close_paisa: 1_050_000, // == BC — the INCLUSIVE lower edge
            ..base
        },
        &no_prior,
    );
    // Narrow CPR: pdc exactly at the (pdh+pdl)/2 midpoint collapses the band
    // to width 0 (P == BC == TC = 1_050_000) — trivially <= 30 bps of P.
    check(
        bits::BIT_NARROW_CPR_DAY,
        IndicatorSnapshot {
            pivots: compute_pivot_levels(1_100_000, 1_000_000, 1_050_000),
            ..base
        },
        &no_prior,
    );

    // Group 14: Supertrend (bits 64-65). close_x2 = 2_100_000; a line one
    // half-paisa unit below fires ABOVE, one above fires BELOW — strict, exact.
    check(
        bits::BIT_CLOSE_ABOVE_SUPERTREND,
        IndicatorSnapshot {
            supertrend_x2_paisa: Some(2_099_999),
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_CLOSE_BELOW_SUPERTREND,
        IndicatorSnapshot {
            supertrend_x2_paisa: Some(2_100_001),
            ..base
        },
        &no_prior,
    );
    // Group 15: gap midpoint (bits 66-68, DEC-GAP-MIDPOINT-CONDITIONS-001) —
    // a resolved level below / above / exactly at the close.
    check(
        bits::BIT_CLOSE_ABOVE_GAP_MID,
        IndicatorSnapshot {
            gap_mid_paisa: Some(1_049_000),
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_CLOSE_BELOW_GAP_MID,
        IndicatorSnapshot {
            gap_mid_paisa: Some(1_051_000),
            ..base
        },
        &no_prior,
    );
    check(
        bits::BIT_NEAR_GAP_MID,
        IndicatorSnapshot {
            gap_mid_paisa: Some(1_050_000),
            ..base
        },
        &no_prior,
    );

    assert_eq!(
        seen, FULL_MASK,
        "not all implemented bits (incl. R5-S5/BOS/CHoCH/CPR/Supertrend/gap-mid) were exercised"
    );
}

/// The tombstoned VWAP bits 52-53 can NEVER fire — even on the scenario that
/// used to set them (close far above / below every level, warm EMAs). The
/// deterministic companion of `non_implemented_bits_are_always_clear`.
#[test]
fn tombstoned_vwap_bits_never_fire() {
    let pivots = compute_pivot_levels(1_100_000, 1_000_000, 1_060_000);
    let fib = compute_fib_levels(1_100_000, 1_000_000);
    let prox = ProximityParams {
        pivot_proximity_bps: 10,
        pdh_pdl_proximity_bps: 10,
        fib_proximity_bps: 10,
        gap_mid_proximity_bps: 10,
    };
    let no_prior = PriorBarHistory {
        prior_n: 3,
        flags: vec![],
    };
    for close in [1i64, 1_050_000, 5_000_000] {
        let s = IndicatorSnapshot {
            tod_seconds: 10 * 3600,
            open_paisa: close,
            high_paisa: close,
            low_paisa: close,
            close_paisa: close,
            ema20_paisa: Some(1_050_000),
            ema200_paisa: Some(1_050_000),
            pivots,
            fib,
            day_high_paisa: close,
            day_low_paisa: close,
            open_of_day_paisa: close,
            market_structure: MarketStructureSignal::default(),
            supertrend_x2_paisa: None,
            gap_mid_paisa: None,
        };
        let m = evaluate_atoms(&s, &prox, &no_prior);
        assert_eq!(
            m & (0b11u128 << 52),
            0,
            "tombstoned VWAP bits fired for close={close}: {m:#x}"
        );
    }
}

/// Deterministic CPR boundary pins: at close == TC the strict above-bit stays
/// CLEAR while the inclusive inside-bit is SET (and symmetrically at BC) — the
/// `>`↔`>=` / `<`↔`<=` mutant killers for the Group-13 comparisons. Plus the
/// exact narrow-CPR threshold edge in both directions.
#[test]
fn cpr_equality_boundaries_are_pinned() {
    // Base anchor: BC = 1_050_000, TC = 1_056_666 (see the reachability test).
    let pivots = compute_pivot_levels(1_100_000, 1_000_000, 1_060_000);
    let fib = compute_fib_levels(1_100_000, 1_000_000);
    let prox = ProximityParams {
        pivot_proximity_bps: 0,
        pdh_pdl_proximity_bps: 0,
        fib_proximity_bps: 0,
        gap_mid_proximity_bps: 0,
    };
    let no_prior = PriorBarHistory {
        prior_n: 3,
        flags: vec![],
    };
    let eval = |close: i64| {
        let s = IndicatorSnapshot {
            tod_seconds: 0,
            open_paisa: close,
            high_paisa: close,
            low_paisa: close,
            close_paisa: close,
            ema20_paisa: None,
            ema200_paisa: None,
            pivots,
            fib,
            day_high_paisa: close,
            day_low_paisa: close,
            open_of_day_paisa: close,
            market_structure: MarketStructureSignal::default(),
            supertrend_x2_paisa: None,
            gap_mid_paisa: None,
        };
        evaluate_atoms(&s, &prox, &no_prior)
    };
    let bit = |m: u128, b: u32| (m >> b) & 1;

    // close == TC: above CLEAR (strict), inside SET (inclusive upper edge).
    let at_tc = eval(1_056_666);
    assert_eq!(bit(at_tc, bits::BIT_ABOVE_CPR_TC), 0);
    assert_eq!(bit(at_tc, bits::BIT_INSIDE_CPR), 1);
    assert_eq!(bit(at_tc, bits::BIT_BELOW_CPR_BC), 0);
    // one paisa above TC: above SET, inside CLEAR.
    let over_tc = eval(1_056_667);
    assert_eq!(bit(over_tc, bits::BIT_ABOVE_CPR_TC), 1);
    assert_eq!(bit(over_tc, bits::BIT_INSIDE_CPR), 0);
    // close == BC: below CLEAR (strict), inside SET (inclusive lower edge).
    let at_bc = eval(1_050_000);
    assert_eq!(bit(at_bc, bits::BIT_BELOW_CPR_BC), 0);
    assert_eq!(bit(at_bc, bits::BIT_INSIDE_CPR), 1);
    assert_eq!(bit(at_bc, bits::BIT_ABOVE_CPR_TC), 0);
    // one paisa below BC: below SET, inside CLEAR.
    let under_bc = eval(1_049_999);
    assert_eq!(bit(under_bc, bits::BIT_BELOW_CPR_BC), 1);
    assert_eq!(bit(under_bc, bits::BIT_INSIDE_CPR), 0);

    // Narrow threshold edge: the law is (tc − bc)·10_000 <= p·30, i.e. at
    // p = 1_000_000 the tie width is EXACTLY 3_000 paisa. Hand-built anchor:
    //   pdh = 1_048_500, pdl = 948_500 ⇒ BC = round(1_997_000/2) = 998_500;
    //   pdc = 1_003_000 ⇒ sum = 3_000_000 ⇒ P = 1_000_000;
    //   TC = 2_000_000 − 998_500 = 1_001_500 ⇒ width = 3_000 — the EXACT tie,
    //   so narrow FIRES (the `<=` law; a `<` mutant dies here).
    let tie = compute_pivot_levels(1_048_500, 948_500, 1_003_000);
    assert_eq!(tie.p_paisa, 1_000_000);
    let cpr = trading_core::compute_cpr_levels(tie.p_paisa, 1_048_500, 948_500);
    assert_eq!((cpr.bc_paisa, cpr.tc_paisa), (998_500, 1_001_500));
    let s_tie = IndicatorSnapshot {
        tod_seconds: 0,
        open_paisa: 1_000_000,
        high_paisa: 1_000_000,
        low_paisa: 1_000_000,
        close_paisa: 1_000_000,
        ema20_paisa: None,
        ema200_paisa: None,
        pivots: tie,
        fib,
        day_high_paisa: 1_000_000,
        day_low_paisa: 1_000_000,
        open_of_day_paisa: 1_000_000,
        market_structure: MarketStructureSignal::default(),
        supertrend_x2_paisa: None,
        gap_mid_paisa: None,
    };
    let m_tie = evaluate_atoms(&s_tie, &prox, &no_prior);
    assert_eq!(
        bit(m_tie, bits::BIT_NARROW_CPR_DAY),
        1,
        "an exact 30 bps width is narrow (<= law)"
    );
    // One paisa wider than the tie (p unchanged): NOT narrow. Widen the band
    // by lowering bc_raw one paisa: pdh + pdl one LOWER halves oddly — use
    // pdl − 2 so bc drops exactly 1 ⇒ width 3_002 > 3_000 ⇒ clear.
    let wider = compute_pivot_levels(1_048_500, 948_498, 1_003_002);
    assert_eq!(wider.p_paisa, 1_000_000);
    let cpr_w = trading_core::compute_cpr_levels(wider.p_paisa, 1_048_500, 948_498);
    assert_eq!(cpr_w.tc_paisa - cpr_w.bc_paisa, 3_002);
    let m_wide = evaluate_atoms(
        &IndicatorSnapshot {
            pivots: wider,
            ..s_tie
        },
        &prox,
        &no_prior,
    );
    assert_eq!(
        bit(m_wide, bits::BIT_NARROW_CPR_DAY),
        0,
        "a width past 30 bps of P must not be narrow"
    );
}

// ───────────────────── deterministic equality boundaries ────────────────────

/// Every strict comparison in the evaluator is pinned at its EXACT equality
/// edge: at the boundary the bit must be CLEAR (the conditions use `<`/`>`, not
/// `<=`/`>=`). This is the non-flaky companion to the boundary-aware proptest
/// generators — it deterministically kills `<`↔`<=` / `>`↔`>=` mutants (the
/// ones that only diverge on an exact tie) in <1 ms, with no RNG and no shrink.
#[test]
fn equality_boundaries_keep_strict_bits_clear() {
    let pivots = compute_pivot_levels(1_100_000, 1_000_000, 1_060_000);
    let fib = compute_fib_levels(1_100_000, 1_000_000);
    let base = IndicatorSnapshot {
        tod_seconds: 0,
        open_paisa: 1_050_000,
        high_paisa: 1_050_000,
        low_paisa: 1_050_000,
        close_paisa: 1_050_000,
        ema20_paisa: None,
        ema200_paisa: None,
        pivots,
        fib,
        day_high_paisa: 1_050_000,
        day_low_paisa: 1_050_000,
        open_of_day_paisa: 1_050_000,
        market_structure: MarketStructureSignal::default(),
        supertrend_x2_paisa: None,
        gap_mid_paisa: None,
    };
    let prox = ProximityParams {
        pivot_proximity_bps: 0,
        pdh_pdl_proximity_bps: 0,
        fib_proximity_bps: 0,
        gap_mid_proximity_bps: 0,
    };
    let no_prior = PriorBarHistory {
        prior_n: 3,
        flags: vec![],
    };
    let clear = |bit: u32, s: IndicatorSnapshot| {
        let m = evaluate_atoms(&s, &prox, &no_prior);
        assert_eq!(
            (m >> bit) & 1,
            0,
            "bit {bit} must be CLEAR at its equality boundary, mask = {m:#054b}"
        );
    };

    // Group 3 — close exactly on PDH/PDL (the mutants that previously survived).
    clear(
        bits::BIT_CLOSE_ABOVE_PDH,
        IndicatorSnapshot {
            close_paisa: 1_100_000,
            ..base
        },
    );
    clear(
        bits::BIT_CLOSE_BELOW_PDH,
        IndicatorSnapshot {
            close_paisa: 1_100_000,
            ..base
        },
    );
    clear(
        bits::BIT_CLOSE_ABOVE_PDL,
        IndicatorSnapshot {
            close_paisa: 1_000_000,
            ..base
        },
    );
    clear(
        bits::BIT_CLOSE_BELOW_PDL,
        IndicatorSnapshot {
            close_paisa: 1_000_000,
            ..base
        },
    );

    // Group 1 — close exactly on EMA, and ema20 exactly on ema200.
    clear(
        bits::BIT_CLOSE_ABOVE_EMA20,
        IndicatorSnapshot {
            close_paisa: 1_050_000,
            ema20_paisa: Some(1_050_000),
            ..base
        },
    );
    clear(
        bits::BIT_CLOSE_BELOW_EMA20,
        IndicatorSnapshot {
            close_paisa: 1_050_000,
            ema20_paisa: Some(1_050_000),
            ..base
        },
    );
    clear(
        bits::BIT_CLOSE_ABOVE_EMA200,
        IndicatorSnapshot {
            close_paisa: 1_050_000,
            ema200_paisa: Some(1_050_000),
            ..base
        },
    );
    clear(
        bits::BIT_CLOSE_BELOW_EMA200,
        IndicatorSnapshot {
            close_paisa: 1_050_000,
            ema200_paisa: Some(1_050_000),
            ..base
        },
    );
    clear(
        bits::BIT_EMA20_ABOVE_EMA200,
        IndicatorSnapshot {
            ema20_paisa: Some(1_050_000),
            ema200_paisa: Some(1_050_000),
            ..base
        },
    );
    clear(
        bits::BIT_EMA20_BELOW_EMA200,
        IndicatorSnapshot {
            ema20_paisa: Some(1_050_000),
            ema200_paisa: Some(1_050_000),
            ..base
        },
    );

    // Group 5 — candle equality edges.
    // close == open → neither bullish nor bearish.
    let flat = IndicatorSnapshot {
        open_paisa: 1_050_000,
        close_paisa: 1_050_000,
        high_paisa: 1_100_000,
        low_paisa: 1_000_000,
        ..base
    };
    clear(bits::BIT_BAR_BULLISH, flat);
    clear(bits::BIT_BAR_BEARISH, flat);
    // body*10 == range → doji uses `<`, so clear.
    clear(
        bits::BIT_BAR_DOJI,
        IndicatorSnapshot {
            open_paisa: 1_000_000,
            close_paisa: 1_010_000,
            high_paisa: 1_100_000,
            low_paisa: 1_000_000,
            ..base
        },
    );
    // body*10 == range*7 → large-body uses `>`, so clear.
    clear(
        bits::BIT_BAR_LARGE_BODY,
        IndicatorSnapshot {
            open_paisa: 1_000_000,
            close_paisa: 1_070_000,
            high_paisa: 1_100_000,
            low_paisa: 1_000_000,
            ..base
        },
    );
    // body*10 == range*3 → small-body uses `<`, so clear.
    clear(
        bits::BIT_BAR_SMALL_BODY,
        IndicatorSnapshot {
            open_paisa: 1_000_000,
            close_paisa: 1_030_000,
            high_paisa: 1_100_000,
            low_paisa: 1_000_000,
            ..base
        },
    );
    // upper_wick*2 == range AND lower_wick*2 == range → both wick bits clear.
    let balanced_wicks = IndicatorSnapshot {
        open_paisa: 1_050_000,
        close_paisa: 1_050_000,
        high_paisa: 1_100_000,
        low_paisa: 1_000_000,
        ..base
    };
    clear(bits::BIT_LONG_UPPER_WICK, balanced_wicks);
    clear(bits::BIT_LONG_LOWER_WICK, balanced_wicks);

    // Group 7 — close ≠ day_high/day_low → the `==` bits clear; and the thirds
    // sit exactly on their boundary (day_range = 30_000 for clean integers).
    clear(
        bits::BIT_CLOSE_AT_DAY_HIGH,
        IndicatorSnapshot {
            close_paisa: 1_050_000,
            day_high_paisa: 1_100_000,
            day_low_paisa: 1_000_000,
            ..base
        },
    );
    clear(
        bits::BIT_CLOSE_AT_DAY_LOW,
        IndicatorSnapshot {
            close_paisa: 1_050_000,
            day_high_paisa: 1_100_000,
            day_low_paisa: 1_000_000,
            ..base
        },
    );
    // (c-day_low)*3 == day_range*2 → upper-third uses `>`, so clear.
    clear(
        bits::BIT_CLOSE_IN_UPPER_THIRD,
        IndicatorSnapshot {
            close_paisa: 1_020_000,
            day_high_paisa: 1_030_000,
            day_low_paisa: 1_000_000,
            ..base
        },
    );
    // (c-day_low)*3 == day_range → lower-third uses `<`, so clear.
    clear(
        bits::BIT_CLOSE_IN_LOWER_THIRD,
        IndicatorSnapshot {
            close_paisa: 1_010_000,
            day_high_paisa: 1_030_000,
            day_low_paisa: 1_000_000,
            ..base
        },
    );

    // Group 9 — gap thresholds exactly on the edge (gap = 0.5% of pdc = 5_300).
    clear(
        bits::BIT_GAP_UP_DAY,
        IndicatorSnapshot {
            open_of_day_paisa: 1_065_300,
            ..base
        },
    );
    clear(
        bits::BIT_GAP_DOWN_DAY,
        IndicatorSnapshot {
            open_of_day_paisa: 1_054_700,
            ..base
        },
    );
    // inside/outside use strict `<`/`>` — at day_high == pdh (or day_low == pdl)
    // neither fires.
    clear(
        bits::BIT_INSIDE_DAY,
        IndicatorSnapshot {
            day_high_paisa: 1_100_000,
            day_low_paisa: 1_050_000,
            ..base
        },
    );
    clear(
        bits::BIT_OUTSIDE_DAY,
        IndicatorSnapshot {
            day_high_paisa: 1_100_000,
            day_low_paisa: 990_000,
            ..base
        },
    );
}

// ─────────────── Supertrend deterministic pins (bits 64-65) ─────────────────

/// The newly-IMPLEMENTED Supertrend bits genuinely FIRE both ways — the
/// deterministic companion of the reachability battery (a flipped comparison,
/// a dropped `mask |=`, or an accidental un-implement makes a case fail) —
/// and the strict-equality / absent-line edges keep BOTH bits clear.
#[test]
fn supertrend_bits_fire_both_ways_and_equality_clears_both() {
    let pivots = compute_pivot_levels(1_100_000, 1_000_000, 1_060_000);
    let fib = compute_fib_levels(1_100_000, 1_000_000);
    let base = IndicatorSnapshot {
        tod_seconds: 0,
        open_paisa: 1_050_000,
        high_paisa: 1_050_000,
        low_paisa: 1_050_000,
        close_paisa: 1_050_000, // close_x2 = 2_100_000
        ema20_paisa: None,
        ema200_paisa: None,
        pivots,
        fib,
        day_high_paisa: 1_050_000,
        day_low_paisa: 1_050_000,
        open_of_day_paisa: 1_050_000,
        market_structure: MarketStructureSignal::default(),
        supertrend_x2_paisa: None,
    };
    let prox = ProximityParams {
        pivot_proximity_bps: 0,
        pdh_pdl_proximity_bps: 0,
        fib_proximity_bps: 0,
    };
    let no_prior = PriorBarHistory {
        prior_n: 3,
        flags: vec![],
    };
    let st_bits = |st: Option<i64>| {
        let m = evaluate_atoms(
            &IndicatorSnapshot {
                supertrend_x2_paisa: st,
                ..base
            },
            &prox,
            &no_prior,
        );
        (
            (m >> bits::BIT_CLOSE_ABOVE_SUPERTREND) & 1,
            (m >> bits::BIT_CLOSE_BELOW_SUPERTREND) & 1,
        )
    };
    // Fires both ways (the implemented-flip teeth): one ×2 unit (half a
    // paisa) either side of the close flips exactly one bit on.
    assert_eq!(
        st_bits(Some(2_099_999)),
        (1, 0),
        "line below close ⇒ ABOVE fires"
    );
    assert_eq!(
        st_bits(Some(2_100_001)),
        (0, 1),
        "line above close ⇒ BELOW fires"
    );
    // STRICT equality edge: close_x2 == line ⇒ NEITHER bit (the `>`↔`>=` /
    // `<`↔`<=` mutant killers for the Group-14 comparisons).
    assert_eq!(
        st_bits(Some(2_100_000)),
        (0, 0),
        "equality sets neither bit"
    );
    // Absent line (warm-up / unwired) ⇒ neither bit.
    assert_eq!(st_bits(None), (0, 0), "None sets neither bit");
    // Far-domain sanity: the compare is exact ×2 (no truncation): a line at
    // 2_099_998 (one full paisa below) also reads ABOVE.
    assert_eq!(st_bits(Some(2_099_998)), (1, 0));
}

// ──────────────── degenerate-input guards (zero-range / zero pdc) ────────────

/// The Group-5/7/9 `total_range > 0` / `day_range > 0` / `pdc > 0` guards are
/// LOAD-BEARING for degenerate inputs that no valid-candle generator can reach
/// (zero range with a non-zero body, a flat day, a zero prev-close): with a
/// guard relaxed to `>= 0` a flat bar would spuriously fire shape / third / gap
/// bits. Feeding such snapshots and asserting the bits stay clear kills those
/// guard mutants deterministically (DEC-TRADING-CORE-PROPTEST-MUTANTS-001).
#[test]
fn degenerate_bars_fire_no_shape_or_gap_bits() {
    let pivots = compute_pivot_levels(1_100_000, 1_000_000, 1_060_000);
    let fib = compute_fib_levels(1_100_000, 1_000_000);
    let base = IndicatorSnapshot {
        tod_seconds: 0,
        open_paisa: 1_050_000,
        high_paisa: 1_050_000,
        low_paisa: 1_050_000,
        close_paisa: 1_050_000,
        ema20_paisa: None,
        ema200_paisa: None,
        pivots,
        fib,
        day_high_paisa: 1_050_000,
        day_low_paisa: 1_050_000,
        open_of_day_paisa: 1_050_000,
        market_structure: MarketStructureSignal::default(),
        supertrend_x2_paisa: None,
        gap_mid_paisa: None,
    };
    let prox = ProximityParams {
        pivot_proximity_bps: 0,
        pdh_pdl_proximity_bps: 0,
        fib_proximity_bps: 0,
        gap_mid_proximity_bps: 0,
    };
    let no_prior = PriorBarHistory {
        prior_n: 3,
        flags: vec![],
    };
    let clear = |bit: u32, s: IndicatorSnapshot| {
        let m = evaluate_atoms(&s, &prox, &no_prior);
        assert_eq!(
            (m >> bit) & 1,
            0,
            "bit {bit} must be CLEAR for a degenerate bar, mask = {m:#054b}"
        );
    };

    // total_range == 0 (high == low) but body > 0 (open != close): the block
    // guard must suppress EVERY candle-shape bit.
    let flat_range_big_body = IndicatorSnapshot {
        open_paisa: 1_050_000,
        close_paisa: 1_000_000,
        high_paisa: 1_000_000,
        low_paisa: 1_000_000,
        ..base
    };
    clear(bits::BIT_BAR_DOJI, flat_range_big_body);
    clear(bits::BIT_BAR_LARGE_BODY, flat_range_big_body);
    clear(bits::BIT_BAR_SMALL_BODY, flat_range_big_body);
    clear(bits::BIT_LONG_UPPER_WICK, flat_range_big_body);
    clear(bits::BIT_LONG_LOWER_WICK, flat_range_big_body);

    // day_range == 0 (day_high == day_low) but close off that level: the day
    // guard must suppress both third bits.
    let flat_day = IndicatorSnapshot {
        close_paisa: 1_060_000,
        day_high_paisa: 1_050_000,
        day_low_paisa: 1_050_000,
        ..base
    };
    clear(bits::BIT_CLOSE_IN_UPPER_THIRD, flat_day);
    clear(bits::BIT_CLOSE_IN_LOWER_THIRD, flat_day);

    // pdc == 0: the 0.5%-gap threshold collapses to 0; a positive open must NOT
    // gap (the `pdc > 0` guard skips the whole block).
    let zero_pdc = IndicatorSnapshot {
        pivots: compute_pivot_levels(100_000, 0, 0),
        open_of_day_paisa: 50_000,
        ..base
    };
    clear(bits::BIT_GAP_UP_DAY, zero_pdc);
    clear(bits::BIT_GAP_DOWN_DAY, zero_pdc);
}

// ───────────────────── corrupt-input panic guards ───────────────────────────

/// Corrupt prev-day OHLC (`pdh < pdl`) must panic LOUD in both indicators — the
/// release-safe mirror of Python's `ValueError` (`src/lib.rs` honest envelope).
#[test]
#[should_panic(expected = "PDH must be >= PDL")]
fn pivots_panic_on_pdh_below_pdl() {
    let _ = compute_pivot_levels(1_000_000, 1_100_000, 1_050_000);
}

#[test]
#[should_panic(expected = "PDC must be in [PDL, PDH]")]
fn pivots_panic_on_pdc_out_of_range() {
    let _ = compute_pivot_levels(1_100_000, 1_000_000, 1_200_000);
}

#[test]
#[should_panic(expected = "PDH must be >= PDL")]
fn fib_panics_on_pdh_below_pdl() {
    let _ = compute_fib_levels(1_000_000, 1_100_000);
}

// ───────────────────── gap-midpoint level properties ────────────────────────
// DEC-GAP-MIDPOINT-CONDITIONS-001: the pure session-edge level law.

/// Two ordered candles (X1, X2) plus an open/pdc pair inside the paisa domain.
fn arb_gap_inputs() -> impl Strategy<Value = (i64, i64, i64, i64, i64, i64)> {
    (
        (1i64..=MAX_PAISA, 0i64..=100_000),
        (1i64..=MAX_PAISA, 0i64..=100_000),
        1i64..=MAX_PAISA,
        1i64..=MAX_PAISA,
    )
        .prop_map(|((x1_lo, r1), (x2_lo, r2), open, pdc)| {
            (x1_lo + r1, x1_lo, x2_lo + r2, x2_lo, open, pdc)
        })
}

proptest! {
    /// The minted level lies INCLUSIVELY between its two source extremes, with
    /// equality only when the two extremes are equal (the midpoint law).
    #[test]
    fn gap_mid_lies_between_its_source_extremes(
        (x1_hi, x1_lo, x2_hi, x2_lo, open, pdc) in arb_gap_inputs()
    ) {
        if let Some((level, direction)) = compute_gap_mid(x1_hi, x1_lo, x2_hi, x2_lo, open, pdc) {
            let (a, b) = match direction {
                GapDirection::Up => (x1_hi, x2_hi),
                GapDirection::Down => (x1_lo, x2_lo),
            };
            prop_assert!(a.min(b) <= level && level <= a.max(b),
                "level {level} outside [{}, {}]", a.min(b), a.max(b));
            if a != b {
                // Strictly interior unless the pair degenerates … except the
                // 1-paisa-apart pair, whose half-even midpoint legally lands
                // ON the even endpoint. For gaps >= 2 paisa it is interior.
                if (a - b).abs() >= 2 {
                    prop_assert!(a.min(b) < level && level < a.max(b));
                }
            } else {
                prop_assert_eq!(level, a, "equal extremes yield the shared value");
            }
        }
    }

    /// The direction law: gap-up (`open > pdc`) selects the HIGH pair, gap-down
    /// the LOW pair, and flipping the gap sign flips the pair; a zero gap
    /// yields None.
    #[test]
    fn gap_mid_direction_law(
        (x1_hi, x1_lo, x2_hi, x2_lo, open, pdc) in arb_gap_inputs()
    ) {
        let got = compute_gap_mid(x1_hi, x1_lo, x2_hi, x2_lo, open, pdc);
        if open == pdc {
            prop_assert_eq!(got, None, "zero gap must yield no level");
        } else {
            let (level, direction) = got.expect("nonzero gap always mints a level");
            let expected_dir = if open > pdc { GapDirection::Up } else { GapDirection::Down };
            prop_assert_eq!(direction, expected_dir);
            // The level equals the midpoint of the SELECTED pair — recomputed
            // independently with integer arithmetic + explicit half-even.
            let (a, b) = match direction {
                GapDirection::Up => (x1_hi, x2_hi),
                GapDirection::Down => (x1_lo, x2_lo),
            };
            let sum = a + b;
            let expected_level = if sum.rem_euclid(2) == 0 {
                sum / 2
            } else {
                // .5 tie → nearest EVEN integer.
                let floor = sum.div_euclid(2);
                if floor % 2 == 0 { floor } else { floor + 1 }
            };
            prop_assert_eq!(level, expected_level);
        }
    }

    /// Zero gap yields None for ANY candle shape (the dedicated zero-gap law).
    #[test]
    fn gap_mid_zero_gap_is_always_none(
        (x1_hi, x1_lo, x2_hi, x2_lo, open, _pdc) in arb_gap_inputs()
    ) {
        prop_assert_eq!(compute_gap_mid(x1_hi, x1_lo, x2_hi, x2_lo, open, open), None);
    }
}
