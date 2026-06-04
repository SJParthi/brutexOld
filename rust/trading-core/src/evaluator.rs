//! The 52-condition evaluator — a bit-for-bit port of
//! `brutex/vocabulary/evaluator.py::evaluate_atoms`.
//!
//! Pure function of its inputs (int64 paisa), no state, no allocation: the same
//! code path serves the BruteX backtest (via PyO3, Phase 2) and the TickVault
//! live tick path (native). Returns a `u64` mask with bits 0..=51 set per the
//! [`crate::bits`] layout; bits 52-63 are always 0.

use crate::bits;
use crate::indicators::fib::FibLevels;
use crate::indicators::pivots::PivotLevels;

// Time-of-day cutoffs, in seconds since IST midnight (mirrors evaluator.py:116-120).
const EARLY_MORNING_START: u32 = 9 * 3600 + 16 * 60; // 09:16
const EARLY_MORNING_END: u32 = 10 * 3600; // 10:00
const MID_MORNING_END: u32 = 11 * 3600 + 30 * 60; // 11:30
const MIDDAY_END: u32 = 13 * 3600; // 13:00
const AFTERNOON_END: u32 = 15 * 3600; // 15:00

/// All per-bar indicator values consumed by [`evaluate_atoms`]. Money is int64
/// paisa; `ema*` are `None` during warm-up. `tod_seconds` is the bar's IST
/// wall-clock time-of-day in seconds (Group 8 reads only the local HH:MM:SS,
/// never the UTC instant — see the golden test's timestamp parsing).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IndicatorSnapshot {
    pub tod_seconds: u32,
    pub open_paisa: i64,
    pub high_paisa: i64,
    pub low_paisa: i64,
    pub close_paisa: i64,
    pub ema20_paisa: Option<i64>,
    pub ema200_paisa: Option<i64>,
    pub pivots: PivotLevels,
    pub fib: FibLevels,
    pub day_high_paisa: i64,
    pub day_low_paisa: i64,
    pub open_of_day_paisa: i64,
}

/// Proximity tolerances (basis points) for the `near_*` conditions.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProximityParams {
    pub pivot_proximity_bps: i64,
    pub pdh_pdl_proximity_bps: i64,
    pub fib_proximity_bps: i64,
}

/// Rolling history of the last N bars' bullish (`true`) / bearish (`false`)
/// flags, for the Group-6 conditions (mirrors `PriorBarHistory` in evaluator.py).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PriorBarHistory {
    pub prior_n: usize,
    pub flags: Vec<bool>,
}

impl PriorBarHistory {
    /// True iff the last `prior_n` bars are all bullish.
    #[must_use]
    pub fn all_bullish(&self) -> bool {
        self.flags.len() == self.prior_n && self.flags.iter().all(|&b| b)
    }

    /// True iff the last `prior_n` bars are all bearish.
    #[must_use]
    pub fn all_bearish(&self) -> bool {
        self.flags.len() == self.prior_n && !self.flags.iter().any(|&b| b)
    }

    /// True iff the last `prior_n` bars strictly alternate bull/bear. `n==1`
    /// (a single flag) is trivially alternating; an empty window is not.
    #[must_use]
    pub fn alternating(&self) -> bool {
        if self.flags.len() != self.prior_n {
            return false;
        }
        (0..self.flags.len().saturating_sub(1)).all(|i| self.flags[i] != self.flags[i + 1])
    }
}

/// `|close - level| <= close * bps / 10000` — the proximity primitive
/// (`evaluator.py:123-131`). `close <= 0` is never near. Uses Euclidean
/// division to match Python's floor `//` for any sign (here always positive).
#[inline]
fn is_near(close: i64, level: i64, bps: i64) -> bool {
    if close <= 0 {
        return false;
    }
    let tolerance = (close * bps).div_euclid(10_000);
    (close - level).abs() <= tolerance
}

/// Evaluate all 52 atomic conditions on one bar → a `u64` mask (bits 0..=51).
#[must_use]
pub fn evaluate_atoms(s: &IndicatorSnapshot, p: &ProximityParams, prior: &PriorBarHistory) -> u64 {
    let mut mask: u64 = 0;
    let close = s.close_paisa;

    // ── Group 1: EMA Position (bits 0-5) ──
    if let Some(ema20) = s.ema20_paisa {
        if close > ema20 {
            mask |= 1u64 << bits::BIT_CLOSE_ABOVE_EMA20;
        }
        if close < ema20 {
            mask |= 1u64 << bits::BIT_CLOSE_BELOW_EMA20;
        }
    }
    if let Some(ema200) = s.ema200_paisa {
        if close > ema200 {
            mask |= 1u64 << bits::BIT_CLOSE_ABOVE_EMA200;
        }
        if close < ema200 {
            mask |= 1u64 << bits::BIT_CLOSE_BELOW_EMA200;
        }
    }
    if let (Some(ema20), Some(ema200)) = (s.ema20_paisa, s.ema200_paisa) {
        if ema20 > ema200 {
            mask |= 1u64 << bits::BIT_EMA20_ABOVE_EMA200;
        }
        if ema20 < ema200 {
            mask |= 1u64 << bits::BIT_EMA20_BELOW_EMA200;
        }
    }

    // ── Group 2: Pivot Proximity (bits 6-12) ──
    let bps = p.pivot_proximity_bps;
    let pv = &s.pivots;
    if is_near(close, pv.p_paisa, bps) {
        mask |= 1u64 << bits::BIT_NEAR_PIVOT_P;
    }
    if is_near(close, pv.r1_paisa, bps) {
        mask |= 1u64 << bits::BIT_NEAR_PIVOT_R1;
    }
    if is_near(close, pv.r2_paisa, bps) {
        mask |= 1u64 << bits::BIT_NEAR_PIVOT_R2;
    }
    if is_near(close, pv.r3_paisa, bps) {
        mask |= 1u64 << bits::BIT_NEAR_PIVOT_R3;
    }
    if is_near(close, pv.s1_paisa, bps) {
        mask |= 1u64 << bits::BIT_NEAR_PIVOT_S1;
    }
    if is_near(close, pv.s2_paisa, bps) {
        mask |= 1u64 << bits::BIT_NEAR_PIVOT_S2;
    }
    if is_near(close, pv.s3_paisa, bps) {
        mask |= 1u64 << bits::BIT_NEAR_PIVOT_S3;
    }

    // ── Group 3: PDH/PDL (bits 13-18) ──
    let pdh = pv.pdh_paisa;
    let pdl = pv.pdl_paisa;
    if close > pdh {
        mask |= 1u64 << bits::BIT_CLOSE_ABOVE_PDH;
    }
    if close < pdh {
        mask |= 1u64 << bits::BIT_CLOSE_BELOW_PDH;
    }
    if close > pdl {
        mask |= 1u64 << bits::BIT_CLOSE_ABOVE_PDL;
    }
    if close < pdl {
        mask |= 1u64 << bits::BIT_CLOSE_BELOW_PDL;
    }
    let bps_pp = p.pdh_pdl_proximity_bps;
    if is_near(close, pdh, bps_pp) {
        mask |= 1u64 << bits::BIT_NEAR_PDH;
    }
    if is_near(close, pdl, bps_pp) {
        mask |= 1u64 << bits::BIT_NEAR_PDL;
    }

    // ── Group 4: Fib Proximity (bits 19-29) ──
    let bps_fib = p.fib_proximity_bps;
    let fib = &s.fib;
    if is_near(close, fib.retrace_level_at_pdh_paisa, bps_fib) {
        mask |= 1u64 << bits::BIT_NEAR_FIB_0;
    }
    if is_near(close, fib.retrace_23_paisa, bps_fib) {
        mask |= 1u64 << bits::BIT_NEAR_FIB_236;
    }
    if is_near(close, fib.retrace_38_paisa, bps_fib) {
        mask |= 1u64 << bits::BIT_NEAR_FIB_382;
    }
    if is_near(close, fib.retrace_50_paisa, bps_fib) {
        mask |= 1u64 << bits::BIT_NEAR_FIB_50;
    }
    if is_near(close, fib.retrace_62_paisa, bps_fib) {
        mask |= 1u64 << bits::BIT_NEAR_FIB_618;
    }
    if is_near(close, fib.retrace_78_paisa, bps_fib) {
        mask |= 1u64 << bits::BIT_NEAR_FIB_786;
    }
    if is_near(close, fib.retrace_level_at_pdl_paisa, bps_fib) {
        mask |= 1u64 << bits::BIT_NEAR_FIB_100;
    }
    // Extensions: the bit fires if EITHER the up or the down variant is near.
    for (ext_up, ext_down, bit_pos) in [
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
        if is_near(close, ext_up, bps_fib) || is_near(close, ext_down, bps_fib) {
            mask |= 1u64 << bit_pos;
        }
    }

    // ── Group 5: Candle Shape (bits 30-36) ──
    let o = s.open_paisa;
    let h = s.high_paisa;
    let lo = s.low_paisa;
    let c = close;
    let body = (c - o).abs();
    let total_range = h - lo;
    if c > o {
        mask |= 1u64 << bits::BIT_BAR_BULLISH;
    }
    if c < o {
        mask |= 1u64 << bits::BIT_BAR_BEARISH;
    }
    if total_range > 0 && body * 10 < total_range {
        mask |= 1u64 << bits::BIT_BAR_DOJI;
    }
    if total_range > 0 {
        if body * 10 > total_range * 7 {
            mask |= 1u64 << bits::BIT_BAR_LARGE_BODY;
        }
        if body * 10 < total_range * 3 {
            mask |= 1u64 << bits::BIT_BAR_SMALL_BODY;
        }
        let upper_wick = h - o.max(c);
        let lower_wick = o.min(c) - lo;
        if upper_wick * 2 > total_range {
            mask |= 1u64 << bits::BIT_LONG_UPPER_WICK;
        }
        if lower_wick * 2 > total_range {
            mask |= 1u64 << bits::BIT_LONG_LOWER_WICK;
        }
    }

    // ── Group 6: Prior Bar Pattern (bits 37-39) ──
    if prior.all_bullish() {
        mask |= 1u64 << bits::BIT_PRIOR_N_BULLISH;
    }
    if prior.all_bearish() {
        mask |= 1u64 << bits::BIT_PRIOR_N_BEARISH;
    }
    if prior.alternating() {
        mask |= 1u64 << bits::BIT_PRIOR_ALTERNATING;
    }

    // ── Group 7: Day Structure (bits 40-43) ──
    if c == s.day_high_paisa {
        mask |= 1u64 << bits::BIT_CLOSE_AT_DAY_HIGH;
    }
    if c == s.day_low_paisa {
        mask |= 1u64 << bits::BIT_CLOSE_AT_DAY_LOW;
    }
    let day_range = s.day_high_paisa - s.day_low_paisa;
    if day_range > 0 {
        if (c - s.day_low_paisa) * 3 > day_range * 2 {
            mask |= 1u64 << bits::BIT_CLOSE_IN_UPPER_THIRD;
        }
        if (c - s.day_low_paisa) * 3 < day_range {
            mask |= 1u64 << bits::BIT_CLOSE_IN_LOWER_THIRD;
        }
    }

    // ── Group 8: Time of Day (bits 44-47) ──
    let t = s.tod_seconds;
    if (EARLY_MORNING_START..EARLY_MORNING_END).contains(&t) {
        mask |= 1u64 << bits::BIT_EARLY_MORNING;
    } else if (EARLY_MORNING_END..MID_MORNING_END).contains(&t) {
        mask |= 1u64 << bits::BIT_MID_MORNING;
    } else if (MID_MORNING_END..MIDDAY_END).contains(&t) {
        mask |= 1u64 << bits::BIT_MIDDAY;
    } else if (MIDDAY_END..AFTERNOON_END).contains(&t) {
        mask |= 1u64 << bits::BIT_AFTERNOON;
    }

    // ── Group 9: Day Type (bits 48-51) ──
    let pdc = pv.pdc_paisa;
    if pdc > 0 {
        let gap_threshold = (pdc * 50).div_euclid(10_000); // 0.5% of pdc
        if s.open_of_day_paisa > pdc + gap_threshold {
            mask |= 1u64 << bits::BIT_GAP_UP_DAY;
        }
        if s.open_of_day_paisa < pdc - gap_threshold {
            mask |= 1u64 << bits::BIT_GAP_DOWN_DAY;
        }
    }
    if s.day_high_paisa < pdh && s.day_low_paisa > pdl {
        mask |= 1u64 << bits::BIT_INSIDE_DAY;
    }
    if s.day_high_paisa > pdh && s.day_low_paisa < pdl {
        mask |= 1u64 << bits::BIT_OUTSIDE_DAY;
    }

    mask
}
