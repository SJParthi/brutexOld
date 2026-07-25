//! The condition evaluator — a bit-for-bit port of
//! `brutex/vocabulary/evaluator.py::evaluate_atoms`.
//!
//! Pure function of its inputs (int64 paisa), no state, no allocation: the same
//! code path serves the BruteX backtest (via PyO3, Phase 2) and the TickVault
//! live tick path (native). Returns a `u128` mask (the mask is u128 for headroom
//! per DEC-VOCAB-U128-CODEGEN-001 / ADR-0012 D4); every IMPLEMENTED bit
//! (0..=73) can fire. PR-2 added the VWAP position (bits 52-53 — RETAINED per
//! the operator ruling DEC-VOCAB-VWAP-RETAIN-001: evaluate when traded volume
//! exists, abstain (`None` → bits clear) when it doesn't) and the pivot
//! R5/S5 proximity (bits 54-55) branches; DEC-NIFTY-BOS-CHOCH-001 added the
//! BOS/CHoCH branch (bits 56-59) — the four booleans are RESOLVED upstream by the
//! stateful, look-ahead-clean `indicators::market_structure::MarketStructureState`
//! and threaded in via the snapshot exactly like the resolved `PriorBarHistory`
//! flags, so `evaluate_atoms` stays a pure stateless bit-setter. The Supertrend
//! bits 64-65 read the resolved ×2-scaled line the stateful
//! `indicators::supertrend::SupertrendState` produces upstream (threaded in via
//! the snapshot exactly like the EMA values);
//! DEC-GAP-MIDPOINT-CONDITIONS-001 added the session-edge gap-midpoint branch
//! (bits 66-68) — the level is resolved upstream (stateful Stage-1A X1/X2
//! capture + `indicators::gap_mid::compute_gap_mid`) and threaded in as an
//! optional level. A u128 AND is
//! two u64 ANDs — still O(1) per bar (NOT an O(1)-total claim).

use crate::bits;
use crate::indicators::fib::FibLevels;
use crate::indicators::market_structure::MarketStructureSignal;
use crate::indicators::pivots::{NARROW_CPR_WIDTH_BPS, PivotLevels, compute_cpr_levels};

// Time-of-day cutoffs, in seconds since IST midnight (mirrors evaluator.py:116-120).
const EARLY_MORNING_START: u32 = 9 * 3600 + 16 * 60; // 09:16
const EARLY_MORNING_END: u32 = 10 * 3600; // 10:00
const MID_MORNING_END: u32 = 11 * 3600 + 30 * 60; // 11:30
const MIDDAY_END: u32 = 13 * 3600; // 13:00
const AFTERNOON_END: u32 = 15 * 3600; // 15:00

/// The single-bit mask for condition `pos` (`1 << pos`). Routing every
/// `mask |= …` through this one site is the DYNAMIC/COMMON discipline (one shift
/// expression, not 52) and removes the `1u128 << 0` ⇔ `1u128 >> 0` equivalent
/// mutant at bit 0: because this helper is called for every implemented bit, a
/// `<<`→`>>` perturbation breaks bit 1+ and is killed
/// (DEC-TRADING-CORE-PROPTEST-MUTANTS-001). `u128` (ADR-0012 D4) — the shift is
/// the only width-sensitive site; bit positions stay `u32`.
#[inline]
const fn bit(pos: u32) -> u128 {
    1u128 << pos
}

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
    /// Session-anchored intraday VWAP (paisa). `None` during the warm-up bar and
    /// on the 1day timeframe (ADR-0012 D12 — VWAP is intraday-only/NA on 1day),
    /// so the Group-10 bits simply do not fire.
    pub vwap_paisa: Option<i64>,
    pub pivots: PivotLevels,
    pub fib: FibLevels,
    pub day_high_paisa: i64,
    pub day_low_paisa: i64,
    pub open_of_day_paisa: i64,
    /// The bar's resolved BOS/CHoCH market-structure flags (Group 12, bits
    /// 56-59), produced by the stateful look-ahead-clean
    /// [`crate::indicators::market_structure::MarketStructureState`] the engine
    /// holds per (instrument, timeframe, contract) — fed in exactly like the
    /// resolved `PriorBarHistory` flags. `default()` (all false) during warm-up
    /// (before any confirmed swing) so the Group-12 bits simply do not fire.
    pub market_structure: MarketStructureSignal,
    /// The bar's resolved SuperTrend line (Group 14, bits 64-65) in the
    /// **×2-scaled paisa domain** (= line value × 2 exactly — the hl2 source is
    /// a half-paisa quantity, see [`crate::indicators::supertrend`]), produced
    /// by the stateful [`crate::indicators::supertrend::SupertrendState`] the
    /// engine holds per (instrument, timeframe, contract). `None` during the
    /// 9-bar warm-up (or when no state is wired) — the Group-14 bits simply do
    /// not fire. The evaluator's comparison is a pure i128 compare of
    /// `close × 2` against this value, total for ANY i64 inputs — so this field
    /// needs NO boundary magnitude bound.
    pub supertrend_x2_paisa: Option<i64>,
    /// Session-edge gap-midpoint level (paisa) for the Group-15 bits 66-68
    /// (DEC-GAP-MIDPOINT-CONDITIONS-001), resolved upstream by the stateful
    /// Stage-1A X1/X2 capture + [`crate::indicators::gap_mid::compute_gap_mid`]
    /// and threaded in as an OPTIONAL level. `None` on a zero-gap day, when
    /// X1/X2 are not captured (first stream day / post-roll / Muhurat-skipped
    /// prior session), on the session's FIRST TF bar (the arm-from-bar-2 law)
    /// and on the 1day timeframe — the Group-15 bits then simply do not fire.
    /// APPENDED LAST (the grow-only FFI law; never reorder existing fields).
    pub gap_mid_paisa: Option<i64>,
    /// The last k-confirmed swing HIGH level (int64 paisa), read from the SAME
    /// look-ahead-clean `MarketStructureState` AFTER its per-bar `update` (a
    /// swing is confirmed only k bars later, all at index <= N —
    /// DEC-FIB-TEMPLATE-EXPRESS-001, Swing Proximity bit 72). `None` until the
    /// first confirmed swing high exists (warm-up, or after a contract-roll
    /// hard reset) — bit 72 then simply does not fire (never fabricated).
    pub last_swing_high_paisa: Option<i64>,
    /// The last k-confirmed swing LOW level — the bit-73 twin of
    /// [`IndicatorSnapshot::last_swing_high_paisa`].
    pub last_swing_low_paisa: Option<i64>,
}

/// Proximity tolerances (basis points) for the `near_*` conditions.
///
/// Carries OPTIONAL serde derives (behind the `serde` feature) so it can be
/// embedded on a serializable [`crate::spec::StrategySpec`]; the core stays
/// dependency-free when the feature is off (DEC-STRATEGYSPEC-CORE-001).
#[cfg_attr(feature = "serde", derive(serde::Serialize, serde::Deserialize))]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProximityParams {
    pub pivot_proximity_bps: i64,
    pub pdh_pdl_proximity_bps: i64,
    pub fib_proximity_bps: i64,
    /// Tolerance for the Group-15 `near_gap_mid` bit
    /// (DEC-GAP-MIDPOINT-CONDITIONS-001), matching the sibling 10-bps
    /// defaults. APPENDED LAST; a serialized pre-gap spec (no field) loads the
    /// default so old TickVault spec JSON stays readable. Deliberately NOT
    /// encoded by `StrategySpec::canonical_bytes` (frozen at
    /// `SPEC_SCHEMA_VERSION = 1`, exactly three bps) — BruteX carries a swept
    /// non-default value in run identity via the default-omit
    /// `canonical_trade_params` key instead (the
    /// DEC-NIFTY-PRICE-SOURCE-AXIS-001 Override-1 pattern).
    #[cfg_attr(feature = "serde", serde(default = "default_gap_mid_proximity_bps"))]
    pub gap_mid_proximity_bps: i64,
    /// Swing-proximity tolerance for the near-swing bits 72/73
    /// (DEC-FIB-TEMPLATE-EXPRESS-001). Appended-last with a serde default so a
    /// pre-existing serialized `StrategySpec` (which never carried the field)
    /// deserializes to the production default 10 — the same family-scoped
    /// default the three sibling knobs use. NOT part of the FROZEN 57-byte
    /// `StrategySpec::canonical_bytes` encode (the spec axis carries no swing
    /// knob; a spec-axis swing sweep would need its own DEC).
    #[cfg_attr(feature = "serde", serde(default = "default_swing_proximity_bps"))]
    pub swing_proximity_bps: i64,
}

/// The default `gap_mid_proximity_bps` (10 bps — the sibling knobs' default;
/// DEC-GAP-MIDPOINT-CONDITIONS-001). Also the serde default so a pre-gap
/// serialized spec deserializes to the SAME evaluation it always had.
pub const DEFAULT_GAP_MID_PROXIMITY_BPS: i64 = 10;

/// serde default fn for [`ProximityParams::gap_mid_proximity_bps`] (serde
/// requires a fn path, not a const).
#[cfg(feature = "serde")]
fn default_gap_mid_proximity_bps() -> i64 {
    DEFAULT_GAP_MID_PROXIMITY_BPS
}

/// The production default for [`ProximityParams::swing_proximity_bps`] (10 bps —
/// the sibling knobs' default; the serde-default + `Default` source of truth).
#[must_use]
pub const fn default_swing_proximity_bps() -> i64 {
    10
}

impl Default for ProximityParams {
    /// The production defaults (10 bps per family) — mirrors the Python
    /// `ProximityParams()` dataclass defaults, so a TickVault consumer that
    /// constructed the pre-DEC-FIB-TEMPLATE-EXPRESS-001 triple can migrate
    /// mechanically via `ProximityParams { swing_proximity_bps: …, ..Default::default() }`.
    fn default() -> Self {
        ProximityParams {
            pivot_proximity_bps: 10,
            pdh_pdl_proximity_bps: 10,
            fib_proximity_bps: 10,
            gap_mid_proximity_bps: DEFAULT_GAP_MID_PROXIMITY_BPS,
            swing_proximity_bps: default_swing_proximity_bps(),
        }
    }
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

/// Evaluate the implemented atomic conditions on one bar → a `u128` mask.
///
/// Every IMPLEMENTED bit (0..=73) can fire. The VWAP bits (52..=53) read the
/// resolved `snapshot.vwap_paisa` level (`None` = abstain — no volume yet /
/// volumeless instrument / 1day; DEC-VOCAB-VWAP-RETAIN-001). The
/// BOS/CHoCH bits (56..=59) read the resolved `snapshot.market_structure`
/// flags (DEC-NIFTY-BOS-CHOCH-001); the CPR bits (60..=63) derive from the
/// SAME prev-day pivot anchor as Groups 2/3/9; the Supertrend bits (64..=65)
/// compare the close against the resolved ×2-scaled `supertrend_x2_paisa`
/// line (strict, exact — `None` clears both); the Gap-Midpoint bits (66..=68)
/// read the resolved `snapshot.gap_mid_paisa` level
/// (DEC-GAP-MIDPOINT-CONDITIONS-001); the Fibonacci-Extended bits (69..=71)
/// read the bullish/424 `FibLevels` fields and the Swing-Proximity bits
/// (72..=73) read the snapshot's resolved swing levels
/// (DEC-FIB-TEMPLATE-EXPRESS-001).
#[must_use]
pub fn evaluate_atoms(s: &IndicatorSnapshot, p: &ProximityParams, prior: &PriorBarHistory) -> u128 {
    let mut mask: u128 = 0;
    let close = s.close_paisa;

    // ── Group 1: EMA Position (bits 0-5) ──
    if let Some(ema20) = s.ema20_paisa {
        if close > ema20 {
            mask |= bit(bits::BIT_CLOSE_ABOVE_EMA20);
        }
        if close < ema20 {
            mask |= bit(bits::BIT_CLOSE_BELOW_EMA20);
        }
    }
    if let Some(ema200) = s.ema200_paisa {
        if close > ema200 {
            mask |= bit(bits::BIT_CLOSE_ABOVE_EMA200);
        }
        if close < ema200 {
            mask |= bit(bits::BIT_CLOSE_BELOW_EMA200);
        }
    }
    if let (Some(ema20), Some(ema200)) = (s.ema20_paisa, s.ema200_paisa) {
        if ema20 > ema200 {
            mask |= bit(bits::BIT_EMA20_ABOVE_EMA200);
        }
        if ema20 < ema200 {
            mask |= bit(bits::BIT_EMA20_BELOW_EMA200);
        }
    }

    // ── Group 2: Pivot Proximity (bits 6-12) ──
    let bps = p.pivot_proximity_bps;
    let pv = &s.pivots;
    if is_near(close, pv.p_paisa, bps) {
        mask |= bit(bits::BIT_NEAR_PIVOT_P);
    }
    if is_near(close, pv.r1_paisa, bps) {
        mask |= bit(bits::BIT_NEAR_PIVOT_R1);
    }
    if is_near(close, pv.r2_paisa, bps) {
        mask |= bit(bits::BIT_NEAR_PIVOT_R2);
    }
    if is_near(close, pv.r3_paisa, bps) {
        mask |= bit(bits::BIT_NEAR_PIVOT_R3);
    }
    if is_near(close, pv.s1_paisa, bps) {
        mask |= bit(bits::BIT_NEAR_PIVOT_S1);
    }
    if is_near(close, pv.s2_paisa, bps) {
        mask |= bit(bits::BIT_NEAR_PIVOT_S2);
    }
    if is_near(close, pv.s3_paisa, bps) {
        mask |= bit(bits::BIT_NEAR_PIVOT_S3);
    }

    // ── Group 3: PDH/PDL (bits 13-18) ──
    let pdh = pv.pdh_paisa;
    let pdl = pv.pdl_paisa;
    if close > pdh {
        mask |= bit(bits::BIT_CLOSE_ABOVE_PDH);
    }
    if close < pdh {
        mask |= bit(bits::BIT_CLOSE_BELOW_PDH);
    }
    if close > pdl {
        mask |= bit(bits::BIT_CLOSE_ABOVE_PDL);
    }
    if close < pdl {
        mask |= bit(bits::BIT_CLOSE_BELOW_PDL);
    }
    let bps_pp = p.pdh_pdl_proximity_bps;
    if is_near(close, pdh, bps_pp) {
        mask |= bit(bits::BIT_NEAR_PDH);
    }
    if is_near(close, pdl, bps_pp) {
        mask |= bit(bits::BIT_NEAR_PDL);
    }

    // ── Group 4: Fib Proximity (bits 19-29) ──
    let bps_fib = p.fib_proximity_bps;
    let fib = &s.fib;
    if is_near(close, fib.retrace_level_at_pdh_paisa, bps_fib) {
        mask |= bit(bits::BIT_NEAR_FIB_0);
    }
    if is_near(close, fib.retrace_23_paisa, bps_fib) {
        mask |= bit(bits::BIT_NEAR_FIB_236);
    }
    if is_near(close, fib.retrace_38_paisa, bps_fib) {
        mask |= bit(bits::BIT_NEAR_FIB_382);
    }
    if is_near(close, fib.retrace_50_paisa, bps_fib) {
        mask |= bit(bits::BIT_NEAR_FIB_50);
    }
    if is_near(close, fib.retrace_62_paisa, bps_fib) {
        mask |= bit(bits::BIT_NEAR_FIB_618);
    }
    if is_near(close, fib.retrace_78_paisa, bps_fib) {
        mask |= bit(bits::BIT_NEAR_FIB_786);
    }
    if is_near(close, fib.retrace_level_at_pdl_paisa, bps_fib) {
        mask |= bit(bits::BIT_NEAR_FIB_100);
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
            mask |= bit(bit_pos);
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
        mask |= bit(bits::BIT_BAR_BULLISH);
    }
    if c < o {
        mask |= bit(bits::BIT_BAR_BEARISH);
    }
    // No `total_range > 0` guard needed: `body >= 0`, so a strict
    // `body * 10 < total_range` already implies `total_range > 0` (when
    // total_range <= 0 the comparison is false). Dropping the redundant guard
    // keeps the mask bit-identical (golden 400 + the independent oracle prove
    // it) AND removes a provably-equivalent `>`→`>=` mutant
    // (DEC-TRADING-CORE-PROPTEST-MUTANTS-001).
    if body * 10 < total_range {
        mask |= bit(bits::BIT_BAR_DOJI);
    }
    // The remaining shape bits DO need the guard: at `total_range <= 0` a
    // degenerate bar (e.g. high == low with open != close) could otherwise
    // spuriously fire `large_body` etc. — the guard is load-bearing and its
    // mutant is killed by `degenerate_bars_fire_no_shape_or_gap_bits`.
    if total_range > 0 {
        if body * 10 > total_range * 7 {
            mask |= bit(bits::BIT_BAR_LARGE_BODY);
        }
        if body * 10 < total_range * 3 {
            mask |= bit(bits::BIT_BAR_SMALL_BODY);
        }
        let upper_wick = h - o.max(c);
        let lower_wick = o.min(c) - lo;
        if upper_wick * 2 > total_range {
            mask |= bit(bits::BIT_LONG_UPPER_WICK);
        }
        if lower_wick * 2 > total_range {
            mask |= bit(bits::BIT_LONG_LOWER_WICK);
        }
    }

    // ── Group 6: Prior Bar Pattern (bits 37-39) ──
    if prior.all_bullish() {
        mask |= bit(bits::BIT_PRIOR_N_BULLISH);
    }
    if prior.all_bearish() {
        mask |= bit(bits::BIT_PRIOR_N_BEARISH);
    }
    if prior.alternating() {
        mask |= bit(bits::BIT_PRIOR_ALTERNATING);
    }

    // ── Group 7: Day Structure (bits 40-43) ──
    if c == s.day_high_paisa {
        mask |= bit(bits::BIT_CLOSE_AT_DAY_HIGH);
    }
    if c == s.day_low_paisa {
        mask |= bit(bits::BIT_CLOSE_AT_DAY_LOW);
    }
    // The `day_range > 0` guard is load-bearing: at day_high == day_low a close
    // off that level would otherwise spuriously fire the third bits; its mutant
    // is killed by `degenerate_bars_fire_no_shape_or_gap_bits`.
    let day_range = s.day_high_paisa - s.day_low_paisa;
    if day_range > 0 {
        if (c - s.day_low_paisa) * 3 > day_range * 2 {
            mask |= bit(bits::BIT_CLOSE_IN_UPPER_THIRD);
        }
        if (c - s.day_low_paisa) * 3 < day_range {
            mask |= bit(bits::BIT_CLOSE_IN_LOWER_THIRD);
        }
    }

    // ── Group 8: Time of Day (bits 44-47) ──
    let t = s.tod_seconds;
    if (EARLY_MORNING_START..EARLY_MORNING_END).contains(&t) {
        mask |= bit(bits::BIT_EARLY_MORNING);
    } else if (EARLY_MORNING_END..MID_MORNING_END).contains(&t) {
        mask |= bit(bits::BIT_MID_MORNING);
    } else if (MID_MORNING_END..MIDDAY_END).contains(&t) {
        mask |= bit(bits::BIT_MIDDAY);
    } else if (MIDDAY_END..AFTERNOON_END).contains(&t) {
        mask |= bit(bits::BIT_AFTERNOON);
    }

    // ── Group 9: Day Type (bits 48-51) ──
    // The `pdc > 0` guard is load-bearing: at pdc == 0 the 0.5%-gap threshold is
    // 0 and any positive open would spuriously gap; its mutant is killed by
    // `degenerate_bars_fire_no_shape_or_gap_bits`.
    let pdc = pv.pdc_paisa;
    if pdc > 0 {
        let gap_threshold = (pdc * 50).div_euclid(10_000); // 0.5% of pdc
        if s.open_of_day_paisa > pdc + gap_threshold {
            mask |= bit(bits::BIT_GAP_UP_DAY);
        }
        if s.open_of_day_paisa < pdc - gap_threshold {
            mask |= bit(bits::BIT_GAP_DOWN_DAY);
        }
    }
    if s.day_high_paisa < pdh && s.day_low_paisa > pdl {
        mask |= bit(bits::BIT_INSIDE_DAY);
    }
    if s.day_high_paisa > pdh && s.day_low_paisa < pdl {
        mask |= bit(bits::BIT_OUTSIDE_DAY);
    }

    // ── Group 10: VWAP Position (bits 52-53) ──
    // Session-anchored intraday futures VWAP (ADR-0012 D12; RETAINED per the
    // operator ruling DEC-VOCAB-VWAP-RETAIN-001 — evaluate when volume exists,
    // abstain when it doesn't). `None` during the warm-up bar (no post-09:15
    // volume yet), on any volumeless instrument (a spot index stores volume=0
    // by construction — MIGRATION #394 — so the state never warms) AND on the
    // 1day timeframe (a session VWAP is degenerate with one bar/session) — so
    // the bit simply does not fire, mirroring the EMA warm-up `if let Some(..)`
    // pattern. The running VWAP math (cum(price·vol)/cum(vol), reset at 09:15
    // IST, auction-volume excluded, i128 accumulators, reset on contract roll)
    // lives in the stateful `indicators::vwap::VwapState`; the evaluator only
    // reads the resolved level.
    if let Some(vwap) = s.vwap_paisa {
        if close > vwap {
            mask |= bit(bits::BIT_CLOSE_ABOVE_VWAP);
        }
        if close < vwap {
            mask |= bit(bits::BIT_CLOSE_BELOW_VWAP);
        }
    }

    // ── Group 11: Pivot Proximity Extended (R5/S5, bits 54-55) ──
    // R5/S5 extend the standard floor-trader pivot ladder past R4/S4
    // (`indicators::pivots`); the proximity check reuses the same `is_near`
    // primitive + the Group-2 pivot tolerance.
    if is_near(close, pv.r5_paisa, bps) {
        mask |= bit(bits::BIT_NEAR_PIVOT_R5);
    }
    if is_near(close, pv.s5_paisa, bps) {
        mask |= bit(bits::BIT_NEAR_PIVOT_S5);
    }

    // ── Group 12: Market Structure (BOS/CHoCH, bits 56-59) ──
    // The four booleans are RESOLVED upstream by the stateful, look-ahead-clean
    // `indicators::market_structure::MarketStructureState` (a swing is only
    // confirmed k bars LATER, all at index ≤ N), and threaded in via the snapshot
    // exactly like the resolved `PriorBarHistory` flags — `evaluate_atoms` stays a
    // pure, stateless function that only sets bits from the resolved flags
    // (DEC-NIFTY-BOS-CHOCH-001). All false during warm-up → no bit fires.
    let ms = &s.market_structure;
    if ms.bos_bullish {
        mask |= bit(bits::BIT_BOS_BULLISH);
    }
    if ms.bos_bearish {
        mask |= bit(bits::BIT_BOS_BEARISH);
    }
    if ms.choch_bullish {
        mask |= bit(bits::BIT_CHOCH_BULLISH);
    }
    if ms.choch_bearish {
        mask |= bit(bits::BIT_CHOCH_BEARISH);
    }

    // ── Group 13: Central Pivot Range (bits 60-63) ──
    // CPR from the SAME prev-day OHLC anchor the pivots use (the Muhurat-safe
    // #1616 anchor carries through automatically): BC = round_half_even((PDH +
    // PDL)/2), TC = 2·P − BC on the ALREADY-ROUNDED pivot P (never re-derived
    // from rounded R/S rungs), canonicalized tc >= bc — `compute_cpr_levels`.
    // Bits 60/61 are strict, 62 is the inclusive band: with tc >= bc the three
    // form an EXACT PARTITION of any close (property-pinned). Bit 63 fires iff
    // the band is at or under NARROW_CPR_WIDTH_BPS of P — evaluated in i128 so
    // the products are total for ANY i64 inputs (the from_raw anchor ceiling
    // `i64::MAX / 8` would overflow the i64 `width * 10_000` product). A cold
    // (no-prev-day) anchor is handled UPSTREAM by the anchor-dependent clear —
    // exactly like the Group-3/9 comparison bits (never fabricated to output).
    let cpr = compute_cpr_levels(pv.p_paisa, pv.pdh_paisa, pv.pdl_paisa);
    if close > cpr.tc_paisa {
        mask |= bit(bits::BIT_ABOVE_CPR_TC);
    }
    if close < cpr.bc_paisa {
        mask |= bit(bits::BIT_BELOW_CPR_BC);
    }
    if close >= cpr.bc_paisa && close <= cpr.tc_paisa {
        mask |= bit(bits::BIT_INSIDE_CPR);
    }
    if pv.p_paisa > 0
        && (i128::from(cpr.tc_paisa) - i128::from(cpr.bc_paisa)) * 10_000
            <= i128::from(pv.p_paisa) * i128::from(NARROW_CPR_WIDTH_BPS)
    {
        mask |= bit(bits::BIT_NARROW_CPR_DAY);
    }

    // ── Group 14: Supertrend (bits 64-65) ──
    // The ×2-scaled line is RESOLVED upstream by the stateful
    // `indicators::supertrend::SupertrendState` (ATR-10 Wilder, factor 3, hl2
    // source — all in exact half-paisa integers) and threaded in via the
    // snapshot like the EMA values. Both comparisons are STRICT in the exact
    // ×2 domain (`close × 2` vs the line): equality sets NEITHER bit, `None`
    // (warm-up / no state wired) sets neither — so the two bits are mutually
    // exclusive by construction (the (64, 65) conflict pair). i128 keeps the
    // ×2 product total for ANY i64 close — no overflow, no boundary bound.
    if let Some(st_x2) = s.supertrend_x2_paisa {
        let close_x2 = i128::from(close) * 2;
        let st_x2 = i128::from(st_x2);
        if close_x2 > st_x2 {
            mask |= bit(bits::BIT_CLOSE_ABOVE_SUPERTREND);
        }
        if close_x2 < st_x2 {
            mask |= bit(bits::BIT_CLOSE_BELOW_SUPERTREND);
        }
    }

    // ── Group 15: Gap Midpoint (bits 66-68) ──
    // The session-edge gap-midpoint level (DEC-GAP-MIDPOINT-CONDITIONS-001) is
    // RESOLVED upstream (stateful Stage-1A X1/X2 capture + the pure
    // `indicators::gap_mid::compute_gap_mid`) and threaded in as an OPTIONAL
    // level: `None` (zero gap / missing X1/X2 / the session's first TF bar /
    // 1day) ⇒ no bit fires. Strict-above and strict-below are mutually
    // exclusive by construction; `near` uses the ONE `is_near` primitive with
    // its own bps knob and may legally co-fire with either strict bit (the
    // PDH/PDL family convention).
    if let Some(gap_mid) = s.gap_mid_paisa {
        if close > gap_mid {
            mask |= bit(bits::BIT_CLOSE_ABOVE_GAP_MID);
        }
        if close < gap_mid {
            mask |= bit(bits::BIT_CLOSE_BELOW_GAP_MID);
        }
        if is_near(close, gap_mid, p.gap_mid_proximity_bps) {
            mask |= bit(bits::BIT_NEAR_GAP_MID);
        }
    }

    // ── Group: Fibonacci Extended (bits 69-71) — DEC-FIB-TEMPLATE-EXPRESS-001 ──
    // Bits 69/70 read the PDL-anchored BULLISH retracement ladder
    // (DEC-FIB-BULLISH-RETRACE-001 fields — the asymmetric 23.6/78.6 pair only;
    // bull 38/62 numerically coincide with bearish 62/38). Bit 71 reads the
    // 424% extensions with the EXACT up-OR-down convention of bits 26-29. All
    // three share the swept Group-4 `fib_proximity_bps` (family-scoped band
    // law) and are cleared upstream with the Group-4 bits on anchor-cold /
    // fib-cold days (the stage1a `_NON_ANCHOR_MASK` / `_NON_FIB_MASK` clears).
    if is_near(close, fib.retrace_bull_23_paisa, bps_fib) {
        mask |= bit(bits::BIT_NEAR_FIB_BULL_236);
    }
    if is_near(close, fib.retrace_bull_78_paisa, bps_fib) {
        mask |= bit(bits::BIT_NEAR_FIB_BULL_786);
    }
    if is_near(close, fib.ext_up_424_paisa, bps_fib)
        || is_near(close, fib.ext_down_424_paisa, bps_fib)
    {
        mask |= bit(bits::BIT_NEAR_FIB_424);
    }

    // ── Group: Swing Proximity (bits 72-73) — DEC-FIB-TEMPLATE-EXPRESS-001 ──
    // "Is price AT the structure level" — the retest/approach half the
    // break-EVENT bits 56-59 cannot express. The levels are resolved upstream
    // by the SAME look-ahead-clean `MarketStructureState` that feeds the
    // Group-12 flags (a swing is confirmed only k bars LATER, all at index
    // <= N), threaded in via the snapshot's two `Option<i64>` fields. `None`
    // (no confirmed swing yet, or a contract-roll hard reset) ⇒ the bit does
    // not fire — never fabricated. Band: the swept `swing_proximity_bps`.
    let bps_swing = p.swing_proximity_bps;
    if let Some(sh) = s.last_swing_high_paisa
        && is_near(close, sh, bps_swing)
    {
        mask |= bit(bits::BIT_NEAR_SWING_HIGH);
    }
    if let Some(sl) = s.last_swing_low_paisa
        && is_near(close, sl, bps_swing)
    {
        mask |= bit(bits::BIT_NEAR_SWING_LOW);
    }

    mask
}
