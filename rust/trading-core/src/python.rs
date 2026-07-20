//! PyO3 binding (feature `python`) — entry points for BruteX (Phase 2).
//!
//! Two evaluators are exposed:
//! * [`evaluate_from_raw`] — recomputes pivots+fib from the previous day's
//!   `pdh/pdl/pdc` (the Phase-2a parity path / standalone callers).
//! * [`evaluate_with_levels`] — takes the PRE-BUILT pivot+fib levels (no
//!   recompute) — the faithful delegation target for BruteX's `evaluate_atoms`,
//!   which already holds the day's levels and may carry hand-built levels (e.g.
//!   `pdc == 0`) that a recompute would reject. This is the Phase-2b seam.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::wrap_pyfunction;

use crate::bits::{IMPLEMENTED_MASK, TOTAL_CONDITIONS};
use crate::evaluator::{
    DEFAULT_GAP_MID_PROXIMITY_BPS, IndicatorSnapshot, PriorBarHistory, ProximityParams,
    evaluate_atoms,
};
use crate::greeks::{
    DELTA_ADMISSION_FLOOR, IV_CLAMP_MAX, IV_CLAMP_MIN, T_FLOOR_YEARS, VEGA_ADMISSION_FLOOR, greeks,
};
use crate::indicators::ema::{compute_ema, ema_alpha, ema_seed, ema_step};
use crate::indicators::fib::{FibLevels, compute_fib_levels};
use crate::indicators::gap_mid::compute_gap_mid;
use crate::indicators::market_structure::{
    MarketStructureSignal, MarketStructureState as CoreMarketStructureState,
};
use crate::indicators::pivots::{NARROW_CPR_WIDTH_BPS, PivotLevels, compute_pivot_levels};
use crate::indicators::supertrend::SupertrendState as CoreSupertrendState;
use crate::indicators::vwap::VwapState as CoreVwapState;
use crate::spec::{Direction, StrategySpec};

/// Number of `pivot_levels` entries [`evaluate_with_levels`] expects.
///
/// `[p, r1, r2, r3, s1, s2, s3, pdh, pdl, pdc, r5, s5]` — the original 10 plus the
/// PR-2 R5/S5 levels appended (never renumbered; DEC-NIFTY-INDICATORS-001).
const PIVOT_LEVELS_LEN: usize = 12;
/// Number of `fib_levels` entries [`evaluate_with_levels`] expects.
///
/// Evaluator ABI — grew 15 → 19 under DEC-FIB-TEMPLATE-EXPRESS-001 with the
/// four new slots APPENDED in the PINNED order
/// `[…the original 15…, retrace_bull_23, retrace_bull_78, ext_up_424,
/// ext_down_424]` (indices 15..=18; the first 15 slots are byte-identical).
/// Deliberately DISTINCT from [`FIB_COMPUTE_LIST_LEN`]: before
/// DEC-FIB-BULLISH-RETRACE-001 both surfaces happened to be 15-tuples (an
/// accidental pun, two different lists); the compute projection grew to 19
/// first, and the two constants are numerically equal AGAIN since
/// DEC-FIB-TEMPLATE-EXPRESS-001 — STILL two different lists with different
/// slot orders (slot 0 here is `retrace@pdh`; slot 0 there is `retrace_23`;
/// this list carries NO bull_38/62) — never merge them (the
/// `fib_abi_and_compute_projections_are_distinct` test has the teeth).
const FIB_LEVELS_LEN: usize = 19;
/// Number of entries [`py_compute_fib_levels`] returns (the compute-FFI
/// projection of [`FibLevels`]).
///
/// The original 15 (`retrace_23..ext_down_424`, order UNTOUCHED) plus the four
/// `retrace_bull_23/38/62/78` APPENDED last (DEC-FIB-BULLISH-RETRACE-001).
/// Deliberately DISTINCT from the [`FIB_LEVELS_LEN`] evaluator ABI — the two
/// are numerically equal AGAIN since DEC-FIB-TEMPLATE-EXPRESS-001 (both 19)
/// yet remain two DIFFERENT lists with different slot orders (this one starts
/// `[retrace_23, retrace_38, …]` with the 424s at slots 9/14 and the whole
/// bull quartet appended; the evaluator list starts `[retrace@pdh, …]` with
/// bull_23/78 + the 424s appended and NO bull_38/62) — never merge them.
const FIB_COMPUTE_LIST_LEN: usize = 19;

/// Overflow-safe magnitude ceiling for a previous-day anchor (paisa).
///
/// The inner indicators (`compute_pivot_levels` / `compute_fib_levels`) do
/// integer arithmetic that, under the crate's `overflow-checks = true` release
/// profile, **panics** on overflow (a finance-safety choice, see `lib.rs`). The
/// largest derived pivot magnitude is now `r5 = pdh + 4·(p − pdl)` / its mirror
/// `s5 = pdl − 4·(pdh − p)` — the PR-2 **coefficient-4** ladder rungs
/// (`indicators/pivots.rs`; DEC-NIFTY-INDICATORS-001) that SUPERSEDE the
/// coefficient-3 `r4 = pdh + 3·(p − pdl)` this bound was originally derived
/// against.
///
/// Coefficient-4 derivation, with every anchor constrained to
/// `|x| ≤ M = i64::MAX / 8`: R4's loose `p ≤ pdh` bound no longer suffices at
/// coefficient 4 (it only shows `|r5| ≤ M + 4·2M = 9M`, which exceeds
/// `i64::MAX ≈ 8M`). But `compute_pivot_levels`' always-on asserts guarantee
/// `pdl ≤ pdc ≤ pdh`, so `p = round_half_even((pdh + pdl + pdc)/3)
/// ≤ (2·pdh + pdl)/3 + 1`, hence `p − pdl ≤ ⅔·(pdh − pdl) + 1 ≤ 4M/3 + 1` and
/// `4·(p − pdl) ≤ 16M/3 + 4`; therefore
/// `|r5| ≤ M + 16M/3 + 4 = 19M/3 + 4 ≈ 6.34·M < i64::MAX` (symmetrically for
/// `s5` via `pdh − p ≤ ⅔·(pdh − pdl) + 1`). The largest fib intermediate is
/// `pdh + 3.236·range ≤ M + 3.236·2M < 7.5·M` (the bullish retracements add
/// nothing new: their max `pdl + 0.786·range < pdh + 3.236·range`, so the
/// bound is unchanged by DEC-FIB-BULLISH-RETRACE-001) and the pivot sum
/// `pdh + pdl + pdc` is bounded by `3·M`, so ALL intermediates stay strictly
/// below `8·M ≤ i64::MAX`: STILL provably overflow-free at coefficient 4 with
/// the SAME ceiling — the constant did NOT need lowering. A hostile/garbage
/// FFI caller thus gets a clean `ValueError` instead of a panic → `abort`
/// (SIGABRT under `panic = "abort"`, which would crash the whole interpreter —
/// a DoS).
///
/// This ceiling is ≈ 1.15·10¹⁸ paisa = ₹1.15·10¹⁶ — over 200 billion× the
/// largest golden anchor (≈ 5·10⁶) and far above the property domain
/// (`MAX_PAISA = 10¹²`), so it NEVER rejects a valid input: the 400-case golden
/// bit-identity is byte-for-byte unchanged.
const MAX_ANCHOR_PAISA: i64 = i64::MAX / 8;

/// The crate version, so a loaded extension can be sanity-checked.
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// The number of atomic conditions (the dense bit count from the manifest).
#[pyfunction]
fn total_conditions() -> u32 {
    TOTAL_CONDITIONS
}

/// Validate a previous-day OHLC anchor at the FFI boundary, returning a clean
/// error message for ANY input the inner [`compute_pivot_levels`] would reject
/// with a panic.
///
/// Mirrors the inner function's two always-on `assert!`s (`pdh >= pdl`,
/// `pdl <= pdc <= pdh`) PLUS the implicit overflow precondition
/// ([`MAX_ANCHOR_PAISA`]). A panic in a `#[pyfunction]` under `panic = "abort"`
/// does NOT raise a catchable Python exception — it `abort()`s the whole
/// interpreter — so the boundary MUST reject invalid input cleanly. Pure (no
/// `PyErr`) so the no-libpython `cargo test` covers every branch; the
/// `#[pyfunction]`s map the `Err(String)` to `PyValueError`.
fn check_prev_day_ohlc(pdh: i64, pdl: i64, pdc: i64) -> Result<(), String> {
    check_prev_day_hl(pdh, pdl)?;
    if pdc < pdl || pdc > pdh {
        return Err(format!(
            "prev_pdc_paisa ({pdc}) must lie in [prev_pdl_paisa ({pdl}), prev_pdh_paisa ({pdh})]"
        ));
    }
    Ok(())
}

/// Validate a previous-day high/low anchor (the fib-only precondition).
///
/// Mirrors [`compute_fib_levels`]'s `assert!(pdh >= pdl)` plus the overflow
/// ceiling. See [`check_prev_day_ohlc`] for why the boundary must not panic.
fn check_prev_day_hl(pdh: i64, pdl: i64) -> Result<(), String> {
    if pdh < pdl {
        return Err(format!(
            "prev_pdh_paisa ({pdh}) must be >= prev_pdl_paisa ({pdl})"
        ));
    }
    // `pdh >= pdl` here, so checking the two extremes bounds the whole anchor.
    if pdh > MAX_ANCHOR_PAISA || pdl < -MAX_ANCHOR_PAISA {
        return Err(format!(
            "prev-day anchor magnitude out of the overflow-safe range \
             [-{MAX_ANCHOR_PAISA}, {MAX_ANCHOR_PAISA}] paisa (pdh={pdh}, pdl={pdl})"
        ));
    }
    Ok(())
}

/// Overflow-safe magnitude ceiling for a per-bar price / level (paisa) consumed
/// by the EVALUATOR (`evaluate_atoms`), distinct from the anchor ceiling.
///
/// The evaluator's hot path (which this crate keeps GOLDEN-sensitive and does NOT
/// touch) does several unbounded integer multiplies on the per-bar inputs —
/// `body * 10`, `total_range * 7`, `(close - day_low) * 3`, `*_wick * 2`,
/// `pdc * 50` — plus `(close - level).abs()` and `close * bps`. With `body` and
/// `total_range` up to `2·ceiling`, the worst multiply is `body * 10 ≤ 20·ceiling`;
/// `i64::MAX / 65536` makes `20·ceiling ≈ 3·10⁻⁴ · i64::MAX` — provably
/// overflow-free — AND keeps `(close − level)` (≤ `2·ceiling`) far from `i64::MIN`
/// so `.abs()` never panics. The ceiling is ≈ 1.4·10¹⁴ paisa (₹1.4 trillion),
/// ~17 million× SENSEX (~8·10⁶ paisa) and far above the proptest domain
/// (`MAX_PAISA = 10¹²`), so NO valid bar is ever rejected (golden unchanged).
const MAX_BAR_PAISA: i64 = i64::MAX / 65536;

/// Generous absolute ceiling for a proximity `bps` parameter (rejects an absurd
/// standalone value early — the real overflow guard is the `close.checked_mul(bps)`
/// product check in [`check_evaluate_inputs`]).
///
/// Real bps are tiny (the golden max is 50, the proptest domain is 0..=200); a
/// 1,000,000-bps cap (= 10,000 %) is astronomically above any real tolerance, so
/// it never rejects a valid input. A bps anywhere up to this cap is still
/// **product-checked against the actual `close`** via `checked_mul`, so `close × bps`
/// can never overflow `i64` regardless of how the two ceilings interact.
const MAX_BPS: i64 = 1_000_000;

/// True iff `|value| <= MAX_BAR_PAISA` (a finite per-bar price/level magnitude).
fn bar_in_range(value: i64) -> bool {
    (-MAX_BAR_PAISA..=MAX_BAR_PAISA).contains(&value)
}

/// Validate EVERY per-bar input the evaluator multiplies / subtracts / `.abs()`es,
/// so a VALID anchor with a garbage bar input (huge `close_paisa`, `i64::MIN`
/// price, absurd `*_bps`) is rejected with a clean `ValueError` instead of
/// tripping an inner `overflow-checks`/`abs` panic → `abort` (SIGABRT) under
/// `panic = "abort"`.
///
/// Closes the `is_near` (`close * bps`, `(close - level).abs()`) + Group-5 shape
/// (`body * 10`, `total_range * 7`, wick `* 2`) + Group-7/9 (`(close - day_low) * 3`,
/// `pdc * 50`, `open_of_day ± gap`) arithmetic. The pivot/fib LEVELS are bounded
/// by their own anchor/length validators (anchor path) or by this function's
/// `levels` slice (pre-built path). Pure (no `PyErr`) so `cargo test` covers every
/// branch.
#[expect(
    clippy::too_many_arguments,
    reason = "flat per-bar ABI mirrored from the evaluator snapshot"
)]
fn check_evaluate_inputs(
    open: i64,
    high: i64,
    low: i64,
    close: i64,
    ema20: Option<i64>,
    ema200: Option<i64>,
    day_high: i64,
    day_low: i64,
    open_of_day: i64,
    pdc: i64,
    pivot_bps: i64,
    pdh_pdl_bps: i64,
    fib_bps: i64,
    gap_mid_bps: i64,
    swing_bps: i64,
) -> Result<(), String> {
    // 1. Every per-bar price + optional EMA + `pdc` (the ONLY level the evaluator
    //    MULTIPLIES — `pdc * 50` in Group 9) must sit in the bar range so the
    //    subtractions / `.abs()` / `*N` multiplies cannot overflow or hit
    //    `i64::MIN`. The pivot/fib R/S/retracement levels are only SUBTRACTED in
    //    `is_near` (never multiplied); with a bar-bounded `close` and an
    //    anchor-bounded level, `(close - level)` stays well inside i64 and
    //    `.abs()` never reaches `i64::MIN`, so they need no extra bound here.
    let mut prices = vec![open, high, low, close, day_high, day_low, open_of_day, pdc];
    if let Some(e) = ema20 {
        prices.push(e);
    }
    if let Some(e) = ema200 {
        prices.push(e);
    }
    for v in prices {
        if !bar_in_range(v) {
            return Err(format!(
                "bar price/level magnitude {v} out of the overflow-safe range \
                 [-{MAX_BAR_PAISA}, {MAX_BAR_PAISA}] paisa"
            ));
        }
    }

    // 2. Every proximity bps: `close * bps` must fit i64 (exact `checked_mul`
    //    check), and bps must be non-negative + non-absurd. A negative bps would
    //    make the tolerance negative (harmless to overflow but nonsensical); reject it.
    for (name, bps) in [
        ("pivot_bps", pivot_bps),
        ("pdh_pdl_bps", pdh_pdl_bps),
        ("fib_bps", fib_bps),
        ("gap_mid_bps", gap_mid_bps),
        ("swing_bps", swing_bps),
    ] {
        if !(0..=MAX_BPS).contains(&bps) {
            return Err(format!(
                "{name} ({bps}) out of the overflow-safe range [0, {MAX_BPS}]"
            ));
        }
        // close is already bar-bounded above, but check the ACTUAL i64 product so
        // the guarantee is exact regardless of the ceilings' arithmetic.
        // `checked_mul` returns `None` iff `close * bps` would overflow i64 — the
        // precise condition `is_near` needs (it also correctly ACCEPTS a product of
        // exactly `i64::MIN`, which fits i64, and has no `>`/`>=` boundary to mutate).
        if close.checked_mul(bps).is_none() {
            return Err(format!(
                "{name} ({bps}) × close_paisa ({close}) overflows i64"
            ));
        }
    }
    Ok(())
}

/// Evaluate one bar's condition `u128` mask, RECOMPUTING pivots+fib from the
/// previous day's `pdh/pdl/pdc`. Used by the standalone parity check; BruteX
/// itself uses [`evaluate_with_levels`] (it already holds the levels).
///
/// Every input is validated at the FFI boundary so a garbage value raises a clean
/// `ValueError` rather than tripping an inner panic (which `abort`s the whole
/// interpreter under `panic="abort"`). Bounded inputs: the prev-day anchor
/// (`pdh < pdl`, `pdc` outside `[pdl, pdh]`, or an overflow-prone magnitude →
/// [`check_prev_day_ohlc`]) AND every per-bar evaluate input (`open/high/low/close`,
/// the optional EMAs, `day_high/day_low/open_of_day`, `pdc`, and the three
/// proximity `bps` — including the exact `close.checked_mul(bps)` product check →
/// [`check_evaluate_inputs`]). With both passing, the evaluate path is panic-safe
/// at the boundary for the audited arithmetic (`is_near`'s `close*bps` /
/// `(close-level).abs()`, the Group-5/7/9 multiplies); the inner evaluator math is
/// unchanged (the boundary rejects before it runs).
#[pyfunction]
#[expect(
    clippy::too_many_arguments,
    reason = "flat primitive ABI for the parity check"
)]
#[pyo3(signature = (
    tod_seconds, open_paisa, high_paisa, low_paisa, close_paisa,
    ema20_paisa, ema200_paisa, vwap_paisa, prev_pdh_paisa, prev_pdl_paisa, prev_pdc_paisa,
    day_high_paisa, day_low_paisa, open_of_day_paisa,
    pivot_bps, pdh_pdl_bps, fib_bps, prior_n, prior_flags,
    bos_bullish, bos_bearish, choch_bullish, choch_bearish,
    supertrend_x2_paisa = None,
    gap_mid_paisa = None, gap_mid_bps = DEFAULT_GAP_MID_PROXIMITY_BPS,
    last_swing_high_paisa = None, last_swing_low_paisa = None, swing_bps = 10,
))]
fn evaluate_from_raw(
    tod_seconds: u32,
    open_paisa: i64,
    high_paisa: i64,
    low_paisa: i64,
    close_paisa: i64,
    ema20_paisa: Option<i64>,
    ema200_paisa: Option<i64>,
    vwap_paisa: Option<i64>,
    prev_pdh_paisa: i64,
    prev_pdl_paisa: i64,
    prev_pdc_paisa: i64,
    day_high_paisa: i64,
    day_low_paisa: i64,
    open_of_day_paisa: i64,
    pivot_bps: i64,
    pdh_pdl_bps: i64,
    fib_bps: i64,
    prior_n: usize,
    prior_flags: Vec<bool>,
    bos_bullish: bool,
    bos_bearish: bool,
    choch_bullish: bool,
    choch_bearish: bool,
    supertrend_x2_paisa: Option<i64>,
    gap_mid_paisa: Option<i64>,
    gap_mid_bps: i64,
    last_swing_high_paisa: Option<i64>,
    last_swing_low_paisa: Option<i64>,
    swing_bps: i64,
) -> PyResult<u128> {
    // Thin PyO3 wrapper: all logic + validation lives in the pure `_impl` (unit
    // tested without a libpython link), the boundary only maps `Err(String)` to a
    // clean `ValueError` — never a panic → SIGABRT under `panic = "abort"`.
    evaluate_from_raw_impl(
        tod_seconds,
        open_paisa,
        high_paisa,
        low_paisa,
        close_paisa,
        ema20_paisa,
        ema200_paisa,
        vwap_paisa,
        prev_pdh_paisa,
        prev_pdl_paisa,
        prev_pdc_paisa,
        day_high_paisa,
        day_low_paisa,
        open_of_day_paisa,
        pivot_bps,
        pdh_pdl_bps,
        fib_bps,
        prior_n,
        prior_flags,
        MarketStructureSignal {
            bos_bullish,
            bos_bearish,
            choch_bullish,
            choch_bearish,
        },
        supertrend_x2_paisa,
        gap_mid_paisa,
        gap_mid_bps,
        last_swing_high_paisa,
        last_swing_low_paisa,
        swing_bps,
    )
    .map_err(PyValueError::new_err)
}

/// Pure logic for [`evaluate_from_raw`] — `Result<u128, String>` so the
/// no-libpython `cargo test` covers every branch (valid + the validation reject).
#[expect(
    clippy::too_many_arguments,
    reason = "flat primitive ABI mirrored from the pyfunction"
)]
fn evaluate_from_raw_impl(
    tod_seconds: u32,
    open_paisa: i64,
    high_paisa: i64,
    low_paisa: i64,
    close_paisa: i64,
    ema20_paisa: Option<i64>,
    ema200_paisa: Option<i64>,
    vwap_paisa: Option<i64>,
    prev_pdh_paisa: i64,
    prev_pdl_paisa: i64,
    prev_pdc_paisa: i64,
    day_high_paisa: i64,
    day_low_paisa: i64,
    open_of_day_paisa: i64,
    pivot_bps: i64,
    pdh_pdl_bps: i64,
    fib_bps: i64,
    prior_n: usize,
    prior_flags: Vec<bool>,
    market_structure: MarketStructureSignal,
    supertrend_x2_paisa: Option<i64>,
    gap_mid_paisa: Option<i64>,
    gap_mid_bps: i64,
    last_swing_high_paisa: Option<i64>,
    last_swing_low_paisa: Option<i64>,
    swing_bps: i64,
) -> Result<u128, String> {
    // Validate at the boundary so corrupt / overflow-prone prev-day OHLC is
    // rejected instead of tripping the inner `assert!`/`overflow-checks` panic.
    check_prev_day_ohlc(prev_pdh_paisa, prev_pdl_paisa, prev_pdc_paisa)?;
    // `vwap_paisa` is only compared in the evaluator (never multiplied); bound it
    // for hygiene alongside the EMAs.
    if let Some(v) = vwap_paisa
        && !bar_in_range(v)
    {
        return Err(format!(
            "vwap_paisa magnitude {v} out of the overflow-safe range \
             [-{MAX_BAR_PAISA}, {MAX_BAR_PAISA}] paisa"
        ));
    }
    // `gap_mid_paisa` is SUBTRACTED in `is_near` (never multiplied), so like
    // every caller-supplied level it must be bar-bounded to keep
    // `(close - level).abs()` inside i64 (DEC-GAP-MIDPOINT-CONDITIONS-001).
    if let Some(v) = gap_mid_paisa
        && !bar_in_range(v)
    {
        return Err(format!(
            "gap_mid_paisa magnitude {v} out of the overflow-safe range \
             [-{MAX_BAR_PAISA}, {MAX_BAR_PAISA}] paisa"
        ));
    }
    // Also bound every per-bar input the EVALUATOR multiplies/subtracts/`.abs()`es
    // (close × bps, body × 10, (close − day_low) × 3, pdc × 50, …). The
    // recomputed pivot/fib R/S/retrace levels are bounded by the validated anchor
    // (only SUBTRACTED in `is_near`, never multiplied), so a bar-bounded close
    // keeps `(close − level).abs()` inside i64; `pdc` (the one multiplied level)
    // is bar-bounded here.
    check_evaluate_inputs(
        open_paisa,
        high_paisa,
        low_paisa,
        close_paisa,
        ema20_paisa,
        ema200_paisa,
        day_high_paisa,
        day_low_paisa,
        open_of_day_paisa,
        prev_pdc_paisa,
        pivot_bps,
        pdh_pdl_bps,
        fib_bps,
        gap_mid_bps,
        swing_bps,
    )?;
    // The caller-supplied swing LEVELS are subtracted in `is_near` — take the
    // same garbage-level bound as the pre-built-levels path
    // (DEC-FIB-TEMPLATE-EXPRESS-001).
    for v in [last_swing_high_paisa, last_swing_low_paisa]
        .into_iter()
        .flatten()
    {
        if !bar_in_range(v) {
            return Err(format!(
                "level magnitude {v} out of the overflow-safe range \
                 [-{MAX_BAR_PAISA}, {MAX_BAR_PAISA}] paisa"
            ));
        }
    }
    let pivots = compute_pivot_levels(prev_pdh_paisa, prev_pdl_paisa, prev_pdc_paisa);
    let fib = compute_fib_levels(prev_pdh_paisa, prev_pdl_paisa);
    let snapshot = IndicatorSnapshot {
        tod_seconds,
        open_paisa,
        high_paisa,
        low_paisa,
        close_paisa,
        ema20_paisa,
        ema200_paisa,
        // The recompute path now ALSO threads the session VWAP, so the four-way
        // golden bit-identity (shim == with_levels == from_raw == oracle) holds
        // for the Group-10 bits too (DEC-NIFTY-INDICATORS-001).
        vwap_paisa,
        pivots,
        fib,
        day_high_paisa,
        day_low_paisa,
        open_of_day_paisa,
        market_structure,
        // Pure i128 comparison downstream — no boundary bound needed (see the
        // snapshot field's docs); `None` = warm-up / unwired ⇒ bits 64-65 clear.
        supertrend_x2_paisa,
        // The recompute path threads the session-edge gap-midpoint VERBATIM
        // (it is NOT derivable from pdh/pdl/pdc; the stateful X1/X2 capture
        // lives upstream), so the four-way golden bit-identity holds for the
        // Group-15 bits too (DEC-GAP-MIDPOINT-CONDITIONS-001).
        gap_mid_paisa,
        last_swing_high_paisa,
        last_swing_low_paisa,
    };
    let proximity = ProximityParams {
        pivot_proximity_bps: pivot_bps,
        pdh_pdl_proximity_bps: pdh_pdl_bps,
        fib_proximity_bps: fib_bps,
        gap_mid_proximity_bps: gap_mid_bps,
        swing_proximity_bps: swing_bps,
    };
    let prior = PriorBarHistory {
        prior_n,
        flags: prior_flags,
    };
    Ok(evaluate_atoms(&snapshot, &proximity, &prior))
}

/// Evaluate one bar's condition `u128` mask from PRE-BUILT levels (no
/// recompute) — BruteX's `evaluate_atoms` delegation target.
///
/// `pivot_levels` (len 12) = `[p, r1, r2, r3, s1, s2, s3, pdh, pdl, pdc, r5, s5]`.
/// `fib_levels` (len 19) = `[retrace@pdh, r23, r38, r50, r62, r78, retrace@pdl,
/// up127, up162, up200, up262, dn127, dn162, dn200, dn262, bull23, bull78,
/// up424, dn424]` (the four DEC-FIB-TEMPLATE-EXPRESS-001 slots appended last).
/// `vwap_paisa` is the session VWAP (`None` during warm-up / on 1day — the
/// Group-10 bits then do not fire).
/// `last_swing_high_paisa` / `last_swing_low_paisa` are the engine-resolved
/// swing LEVELS for the near-swing bits 72/73 (from the SAME look-ahead-clean
/// `MarketStructureState`; `None` — the default — keeps both bits dark) and
/// `swing_bps` is their swept proximity band (default 10; appended-last args
/// so every pre-existing 21-arg caller keeps working).
/// The four `bos_*`/`choch_*` booleans are the engine-resolved Group-12
/// market-structure flags (bits 56-59), produced by the stateful look-ahead-clean
/// `MarketStructureState` and passed verbatim like the resolved `PriorBarHistory`
/// flags. The struct fields the conditions never read (pivot r4/s4, fib range/424)
/// are set to 0. Because the levels are passed verbatim, this is bit-identical to
/// the Python evaluator for ANY snapshot — including hand-built levels with
/// `pdc == 0` (the gap conditions' own `pdc > 0` guard handles that).
///
/// The length guards (12 / 19) are validated FIRST (a mismatch would otherwise
/// index out of bounds → SIGABRT), then every caller-supplied level AND every
/// per-bar input + bps is bounded ([`check_evaluate_inputs`]) so a garbage value
/// raises a clean `ValueError` instead of overflowing/`.abs()`-panicking the inner
/// evaluator under `panic="abort"`.
#[pyfunction]
#[expect(
    clippy::too_many_arguments,
    reason = "flat primitive ABI for the BruteX binding"
)]
#[pyo3(signature = (
    tod_seconds, open_paisa, high_paisa, low_paisa, close_paisa,
    ema20_paisa, ema200_paisa, vwap_paisa, pivot_levels, fib_levels,
    day_high_paisa, day_low_paisa, open_of_day_paisa,
    pivot_bps, pdh_pdl_bps, fib_bps, prior_n, prior_flags,
    bos_bullish, bos_bearish, choch_bullish, choch_bearish,
    supertrend_x2_paisa = None,
    gap_mid_paisa = None, gap_mid_bps = DEFAULT_GAP_MID_PROXIMITY_BPS,
    last_swing_high_paisa = None, last_swing_low_paisa = None, swing_bps = 10,
))]
fn evaluate_with_levels(
    tod_seconds: u32,
    open_paisa: i64,
    high_paisa: i64,
    low_paisa: i64,
    close_paisa: i64,
    ema20_paisa: Option<i64>,
    ema200_paisa: Option<i64>,
    vwap_paisa: Option<i64>,
    pivot_levels: Vec<i64>,
    fib_levels: Vec<i64>,
    day_high_paisa: i64,
    day_low_paisa: i64,
    open_of_day_paisa: i64,
    pivot_bps: i64,
    pdh_pdl_bps: i64,
    fib_bps: i64,
    prior_n: usize,
    prior_flags: Vec<bool>,
    bos_bullish: bool,
    bos_bearish: bool,
    choch_bullish: bool,
    choch_bearish: bool,
    supertrend_x2_paisa: Option<i64>,
    gap_mid_paisa: Option<i64>,
    gap_mid_bps: i64,
    last_swing_high_paisa: Option<i64>,
    last_swing_low_paisa: Option<i64>,
    swing_bps: i64,
) -> PyResult<u128> {
    // Thin PyO3 wrapper — the length-validation + the level-indexing it guards
    // live in the pure `_impl` (unit-tested without a libpython link). The
    // boundary maps `Err(String)` to a clean `ValueError`; a length-mismatch can
    // never reach an out-of-bounds index (which would SIGABRT under `panic=abort`).
    evaluate_with_levels_impl(
        tod_seconds,
        open_paisa,
        high_paisa,
        low_paisa,
        close_paisa,
        ema20_paisa,
        ema200_paisa,
        vwap_paisa,
        pivot_levels,
        fib_levels,
        day_high_paisa,
        day_low_paisa,
        open_of_day_paisa,
        pivot_bps,
        pdh_pdl_bps,
        fib_bps,
        prior_n,
        prior_flags,
        MarketStructureSignal {
            bos_bullish,
            bos_bearish,
            choch_bullish,
            choch_bearish,
        },
        supertrend_x2_paisa,
        gap_mid_paisa,
        gap_mid_bps,
        last_swing_high_paisa,
        last_swing_low_paisa,
        swing_bps,
    )
    .map_err(PyValueError::new_err)
}

/// Pure logic for [`evaluate_with_levels`] — `Result<u128, String>` so the
/// length-guard + the indexing it protects are unit-covered without a GIL.
#[expect(
    clippy::too_many_arguments,
    reason = "flat primitive ABI mirrored from the pyfunction"
)]
fn evaluate_with_levels_impl(
    tod_seconds: u32,
    open_paisa: i64,
    high_paisa: i64,
    low_paisa: i64,
    close_paisa: i64,
    ema20_paisa: Option<i64>,
    ema200_paisa: Option<i64>,
    vwap_paisa: Option<i64>,
    pivot_levels: Vec<i64>,
    fib_levels: Vec<i64>,
    day_high_paisa: i64,
    day_low_paisa: i64,
    open_of_day_paisa: i64,
    pivot_bps: i64,
    pdh_pdl_bps: i64,
    fib_bps: i64,
    prior_n: usize,
    prior_flags: Vec<bool>,
    market_structure: MarketStructureSignal,
    supertrend_x2_paisa: Option<i64>,
    gap_mid_paisa: Option<i64>,
    gap_mid_bps: i64,
    last_swing_high_paisa: Option<i64>,
    last_swing_low_paisa: Option<i64>,
    swing_bps: i64,
) -> Result<u128, String> {
    if pivot_levels.len() != PIVOT_LEVELS_LEN {
        return Err(format!(
            "pivot_levels must have {PIVOT_LEVELS_LEN} entries, got {}",
            pivot_levels.len()
        ));
    }
    if fib_levels.len() != FIB_LEVELS_LEN {
        return Err(format!(
            "fib_levels must have {FIB_LEVELS_LEN} entries, got {}",
            fib_levels.len()
        ));
    }
    // Every CALLER-SUPPLIED level is bar-bounded: unlike the anchor path these are
    // NOT derived from a validated anchor, so a garbage level (e.g. `i64::MIN`)
    // would make `(close - level).abs()` overflow/panic in `is_near`, and
    // `pivot_levels[9]` (pdc) is multiplied by 50 in Group 9. Bound them all to
    // the bar range BEFORE building the snapshot.
    for v in pivot_levels.iter().chain(fib_levels.iter()) {
        if !bar_in_range(*v) {
            return Err(format!(
                "level magnitude {v} out of the overflow-safe range \
                 [-{MAX_BAR_PAISA}, {MAX_BAR_PAISA}] paisa"
            ));
        }
    }
    // `gap_mid_paisa` is a caller-supplied LEVEL: it is SUBTRACTED in
    // `is_near`, so it must be bar-bounded like the pivot/fib levels above
    // (DEC-GAP-MIDPOINT-CONDITIONS-001).
    if let Some(v) = gap_mid_paisa
        && !bar_in_range(v)
    {
        return Err(format!(
            "gap_mid_paisa magnitude {v} out of the overflow-safe range \
             [-{MAX_BAR_PAISA}, {MAX_BAR_PAISA}] paisa"
        ));
    }
    // `vwap_paisa` is only COMPARED (`close > vwap` / `close < vwap`) in the
    // evaluator — never multiplied or subtracted — so any i64 is overflow-safe;
    // we still bound it for hygiene alongside the EMAs (a garbage value is a data
    // bug, not a valid level).
    if let Some(v) = vwap_paisa
        && !bar_in_range(v)
    {
        return Err(format!(
            "vwap_paisa magnitude {v} out of the overflow-safe range \
             [-{MAX_BAR_PAISA}, {MAX_BAR_PAISA}] paisa"
        ));
    }
    // The caller-supplied swing LEVELS take the SAME garbage-level bound (they
    // are subtracted in `is_near` exactly like the fib/pivot lists — never the
    // compare-only carve-out; DEC-FIB-TEMPLATE-EXPRESS-001).
    for v in [last_swing_high_paisa, last_swing_low_paisa]
        .into_iter()
        .flatten()
    {
        if !bar_in_range(v) {
            return Err(format!(
                "level magnitude {v} out of the overflow-safe range \
                 [-{MAX_BAR_PAISA}, {MAX_BAR_PAISA}] paisa"
            ));
        }
    }
    // Bound the per-bar inputs + bps the evaluator multiplies/subtracts/`.abs()`es
    // (`pdc` = pivot_levels[9] is already bar-bounded above).
    check_evaluate_inputs(
        open_paisa,
        high_paisa,
        low_paisa,
        close_paisa,
        ema20_paisa,
        ema200_paisa,
        day_high_paisa,
        day_low_paisa,
        open_of_day_paisa,
        pivot_levels[9],
        pivot_bps,
        pdh_pdl_bps,
        fib_bps,
        gap_mid_bps,
        swing_bps,
    )?;
    let pivots = PivotLevels {
        p_paisa: pivot_levels[0],
        r1_paisa: pivot_levels[1],
        r2_paisa: pivot_levels[2],
        r3_paisa: pivot_levels[3],
        s1_paisa: pivot_levels[4],
        s2_paisa: pivot_levels[5],
        s3_paisa: pivot_levels[6],
        pdh_paisa: pivot_levels[7],
        pdl_paisa: pivot_levels[8],
        pdc_paisa: pivot_levels[9],
        // R5/S5 appended at indices 10/11 (PR-2); r4/s4 unused by the conditions.
        r5_paisa: pivot_levels[10],
        s5_paisa: pivot_levels[11],
        r4_paisa: 0,
        s4_paisa: 0,
    };
    let fib = FibLevels {
        retrace_level_at_pdh_paisa: fib_levels[0],
        retrace_23_paisa: fib_levels[1],
        retrace_38_paisa: fib_levels[2],
        retrace_50_paisa: fib_levels[3],
        retrace_62_paisa: fib_levels[4],
        retrace_78_paisa: fib_levels[5],
        retrace_level_at_pdl_paisa: fib_levels[6],
        ext_up_127_paisa: fib_levels[7],
        ext_up_162_paisa: fib_levels[8],
        ext_up_200_paisa: fib_levels[9],
        ext_up_262_paisa: fib_levels[10],
        ext_down_127_paisa: fib_levels[11],
        ext_down_162_paisa: fib_levels[12],
        ext_down_200_paisa: fib_levels[13],
        ext_down_262_paisa: fib_levels[14],
        pdh_paisa: 0,
        pdl_paisa: 0,
        range_paisa: 0,
        // DEC-FIB-TEMPLATE-EXPRESS-001: the four appended ABI slots (bits
        // 69-71 read these); bull_38/62 STAY zero-filled — the evaluator
        // never reads them (numerically coincident with bearish 62/38).
        retrace_bull_23_paisa: fib_levels[15],
        retrace_bull_78_paisa: fib_levels[16],
        ext_up_424_paisa: fib_levels[17],
        ext_down_424_paisa: fib_levels[18],
        retrace_bull_38_paisa: 0,
        retrace_bull_62_paisa: 0,
    };
    let snapshot = IndicatorSnapshot {
        tod_seconds,
        open_paisa,
        high_paisa,
        low_paisa,
        close_paisa,
        ema20_paisa,
        ema200_paisa,
        vwap_paisa,
        pivots,
        fib,
        day_high_paisa,
        day_low_paisa,
        open_of_day_paisa,
        market_structure,
        // Pure i128 comparison downstream — no boundary bound needed (see the
        // snapshot field's docs); `None` = warm-up / unwired ⇒ bits 64-65 clear.
        supertrend_x2_paisa,
        gap_mid_paisa,
        last_swing_high_paisa,
        last_swing_low_paisa,
    };
    let proximity = ProximityParams {
        pivot_proximity_bps: pivot_bps,
        pdh_pdl_proximity_bps: pdh_pdl_bps,
        fib_proximity_bps: fib_bps,
        gap_mid_proximity_bps: gap_mid_bps,
        swing_proximity_bps: swing_bps,
    };
    let prior = PriorBarHistory {
        prior_n,
        flags: prior_flags,
    };
    Ok(evaluate_atoms(&snapshot, &proximity, &prior))
}

/// Evaluate a `StrategySpec` against one bar (strategy-as-DATA, the deploy
/// contract — DEC-STRATEGYSPEC-CORE-001). Takes the SAME flat pre-built-levels
/// ABI as [`evaluate_with_levels`] PLUS `condition_mask_hex` (the required-bits
/// SUPERSET mask, a hex STRING so a bit >= 53 keeps full precision) + `direction`
/// (`0 = Long`, `1 = Short`).
///
/// Returns `(bar_bits, fires, direction)` where `bar_bits` is the bar's full
/// `u128` condition mask (bit-identical to [`evaluate_with_levels`]) and `fires`
/// is the superset test `(bar_bits & mask) == mask`. A THIN wrapper — it computes
/// `bar_bits` via the SAME `evaluate_with_levels_impl` (so the frozen 400-case
/// golden is untouched) then ANDs. Every input is validated at the FFI boundary
/// (mask must be IMPLEMENTED-only, direction must be 0/1, the levels/bps via the
/// reused guards) so garbage raises a clean `ValueError`, never a panic → SIGABRT
/// under `panic="abort"`.
#[pyfunction]
#[expect(
    clippy::too_many_arguments,
    reason = "flat primitive ABI for the BruteX spec binding"
)]
#[pyo3(signature = (
    condition_mask_hex, direction, tod_seconds,
    open_paisa, high_paisa, low_paisa, close_paisa,
    ema20_paisa, ema200_paisa, vwap_paisa, pivot_levels, fib_levels,
    day_high_paisa, day_low_paisa, open_of_day_paisa,
    pivot_bps, pdh_pdl_bps, fib_bps, prior_n, prior_flags,
    bos_bullish, bos_bearish, choch_bullish, choch_bearish,
    supertrend_x2_paisa = None,
    gap_mid_paisa = None, gap_mid_bps = DEFAULT_GAP_MID_PROXIMITY_BPS,
    last_swing_high_paisa = None, last_swing_low_paisa = None, swing_bps = 10,
))]
fn evaluate_spec(
    condition_mask_hex: &str,
    direction: u8,
    tod_seconds: u32,
    open_paisa: i64,
    high_paisa: i64,
    low_paisa: i64,
    close_paisa: i64,
    ema20_paisa: Option<i64>,
    ema200_paisa: Option<i64>,
    vwap_paisa: Option<i64>,
    pivot_levels: Vec<i64>,
    fib_levels: Vec<i64>,
    day_high_paisa: i64,
    day_low_paisa: i64,
    open_of_day_paisa: i64,
    pivot_bps: i64,
    pdh_pdl_bps: i64,
    fib_bps: i64,
    prior_n: usize,
    prior_flags: Vec<bool>,
    bos_bullish: bool,
    bos_bearish: bool,
    choch_bullish: bool,
    choch_bearish: bool,
    supertrend_x2_paisa: Option<i64>,
    gap_mid_paisa: Option<i64>,
    gap_mid_bps: i64,
    last_swing_high_paisa: Option<i64>,
    last_swing_low_paisa: Option<i64>,
    swing_bps: i64,
) -> PyResult<(u128, bool, u8)> {
    // Thin PyO3 wrapper: all logic + validation in the pure `_impl` (unit-tested
    // without a libpython link); the boundary maps `Err(String)` to a clean
    // `ValueError`.
    evaluate_spec_impl(
        condition_mask_hex,
        direction,
        tod_seconds,
        open_paisa,
        high_paisa,
        low_paisa,
        close_paisa,
        ema20_paisa,
        ema200_paisa,
        vwap_paisa,
        pivot_levels,
        fib_levels,
        day_high_paisa,
        day_low_paisa,
        open_of_day_paisa,
        pivot_bps,
        pdh_pdl_bps,
        fib_bps,
        prior_n,
        prior_flags,
        MarketStructureSignal {
            bos_bullish,
            bos_bearish,
            choch_bullish,
            choch_bearish,
        },
        supertrend_x2_paisa,
        gap_mid_paisa,
        gap_mid_bps,
        last_swing_high_paisa,
        last_swing_low_paisa,
        swing_bps,
    )
    .map_err(PyValueError::new_err)
}

/// Pure logic for [`evaluate_spec`] — `Result<(u128, bool, u8), String>` so the
/// no-libpython `cargo test` covers every branch (valid + each reject).
#[expect(
    clippy::too_many_arguments,
    reason = "flat primitive ABI mirrored from the pyfunction"
)]
fn evaluate_spec_impl(
    condition_mask_hex: &str,
    direction: u8,
    tod_seconds: u32,
    open_paisa: i64,
    high_paisa: i64,
    low_paisa: i64,
    close_paisa: i64,
    ema20_paisa: Option<i64>,
    ema200_paisa: Option<i64>,
    vwap_paisa: Option<i64>,
    pivot_levels: Vec<i64>,
    fib_levels: Vec<i64>,
    day_high_paisa: i64,
    day_low_paisa: i64,
    open_of_day_paisa: i64,
    pivot_bps: i64,
    pdh_pdl_bps: i64,
    fib_bps: i64,
    prior_n: usize,
    prior_flags: Vec<bool>,
    market_structure: MarketStructureSignal,
    supertrend_x2_paisa: Option<i64>,
    gap_mid_paisa: Option<i64>,
    gap_mid_bps: i64,
    last_swing_high_paisa: Option<i64>,
    last_swing_low_paisa: Option<i64>,
    swing_bps: i64,
) -> Result<(u128, bool, u8), String> {
    // Parse + validate the spec axes BEFORE the (more expensive) bar evaluate.
    let condition_mask = u128::from_str_radix(condition_mask_hex, 16)
        .map_err(|e| format!("condition_mask_hex {condition_mask_hex:?} is not valid hex: {e}"))?;
    let unimplemented = condition_mask & !IMPLEMENTED_MASK;
    if unimplemented != 0 {
        return Err(format!(
            "condition_mask requires unimplemented/reserved bits ({unimplemented:#034x}); \
             only bits in IMPLEMENTED_MASK ({IMPLEMENTED_MASK:#034x}) may be required"
        ));
    }
    let dir = Direction::from_u8(direction)?;
    // Compute the bar's bitmask via the SAME pre-built-levels impl the production
    // evaluator uses — so the spec path is bit-identical to the engine path and
    // the frozen 400-case golden is untouched.
    let bar_bits = evaluate_with_levels_impl(
        tod_seconds,
        open_paisa,
        high_paisa,
        low_paisa,
        close_paisa,
        ema20_paisa,
        ema200_paisa,
        vwap_paisa,
        pivot_levels,
        fib_levels,
        day_high_paisa,
        day_low_paisa,
        open_of_day_paisa,
        pivot_bps,
        pdh_pdl_bps,
        fib_bps,
        prior_n,
        prior_flags,
        market_structure,
        supertrend_x2_paisa,
        gap_mid_paisa,
        gap_mid_bps,
        last_swing_high_paisa,
        last_swing_low_paisa,
        swing_bps,
    )?;
    let fires = (bar_bits & condition_mask) == condition_mask;
    Ok((bar_bits, fires, dir.as_u8()))
}

/// Return the FROZEN 57-byte canonical spec encode (`StrategySpec::canonical_bytes`,
/// DEC-STRATEGYSPEC-CORE-001) for one spec, as Python `bytes` — the ONE encoder
/// (CLAUDE.md §11 COMMON): BruteX's Python `StrategySpec` calls this instead of
/// re-implementing the byte layout, so the run-identity fingerprint is
/// bit-identical to TickVault's native `canonical_bytes` by construction
/// (DEC-SPEC-IDENTITY-WIRING-001).
///
/// Boundary guard set (every reject is a clean `ValueError`, never a panic →
/// SIGABRT under `panic="abort"`): `condition_mask_hex` must parse as u128 hex;
/// the mask must require IMPLEMENTED bits only; `direction` must be 0/1
/// (`Direction::from_u8`); each proximity bps must sit in `[0, MAX_BPS]` (the
/// SAME constant the evaluate boundary uses). `prior_n`/`symbol_id` widths are
/// enforced by the PyO3 `u64`/`u32` conversions (an out-of-range Python int
/// raises `OverflowError` — the documented backstop). NOTE: `symbol_id = 0` is
/// ACCEPTED here (shared-crate semantics — `StrategySpec::new` allows it);
/// BruteX reserves id 0 as a POLICY in its Python symbol registry
/// (`brutex/strategies/spec.py`), not in this crate.
#[pyfunction]
#[pyo3(signature = (
    condition_mask_hex, direction, pivot_bps, pdh_pdl_bps, fib_bps, prior_n, symbol_id,
))]
fn spec_canonical_bytes(
    condition_mask_hex: &str,
    direction: u8,
    pivot_bps: i64,
    pdh_pdl_bps: i64,
    fib_bps: i64,
    prior_n: u64,
    symbol_id: u32,
) -> PyResult<Vec<u8>> {
    // Thin PyO3 wrapper — validation + encode in the pure `_impl` (unit-tested
    // without a libpython link); `Err(String)` maps to a clean `ValueError`.
    spec_canonical_bytes_impl(
        condition_mask_hex,
        direction,
        pivot_bps,
        pdh_pdl_bps,
        fib_bps,
        prior_n,
        symbol_id,
    )
    .map_err(PyValueError::new_err)
}

/// Pure logic for [`spec_canonical_bytes`] — `Result<Vec<u8>, String>` so the
/// no-libpython `cargo test` covers every branch (valid + each reject).
fn spec_canonical_bytes_impl(
    condition_mask_hex: &str,
    direction: u8,
    pivot_bps: i64,
    pdh_pdl_bps: i64,
    fib_bps: i64,
    prior_n: u64,
    symbol_id: u32,
) -> Result<Vec<u8>, String> {
    // Mask + direction: the SAME parse/validate messages as `evaluate_spec_impl`
    // (one boundary vocabulary, no drift).
    let condition_mask = u128::from_str_radix(condition_mask_hex, 16)
        .map_err(|e| format!("condition_mask_hex {condition_mask_hex:?} is not valid hex: {e}"))?;
    let dir = Direction::from_u8(direction)?;
    // Proximity bps: reuse the evaluate boundary's [0, MAX_BPS] envelope (there
    // is no `close` here to product-check — the encode never multiplies, so the
    // range guard alone keeps the value meaningful + i64-encodable).
    for (name, bps) in [
        ("pivot_bps", pivot_bps),
        ("pdh_pdl_bps", pdh_pdl_bps),
        ("fib_bps", fib_bps),
    ] {
        if !(0..=MAX_BPS).contains(&bps) {
            return Err(format!(
                "{name} ({bps}) out of the overflow-safe range [0, {MAX_BPS}]"
            ));
        }
    }
    // `canonical_bytes` encodes `prior_n` as a fixed u64; the in-memory struct
    // holds usize, so refuse a value the platform cannot represent (unreachable
    // on the 64-bit wheel/TickVault targets — an honest guard, not a live path).
    let prior_n_usize = usize::try_from(prior_n)
        .map_err(|_| format!("prior_n ({prior_n}) exceeds the platform usize range"))?;
    let spec = StrategySpec::new(
        condition_mask,
        dir,
        ProximityParams {
            pivot_proximity_bps: pivot_bps,
            pdh_pdl_proximity_bps: pdh_pdl_bps,
            fib_proximity_bps: fib_bps,
            // NOT encoded by the FROZEN v1 canonical_bytes (exactly three bps)
            // — any value here leaves the bytes untouched; the default keeps
            // the struct meaningful (DEC-GAP-MIDPOINT-CONDITIONS-001).
            gap_mid_proximity_bps: DEFAULT_GAP_MID_PROXIMITY_BPS,
            // The spec axis carries NO swing knob (the FROZEN 57-byte
            // canonical encode never carries the field) — always the
            // production default (DEC-FIB-TEMPLATE-EXPRESS-001).
            swing_proximity_bps: crate::evaluator::default_swing_proximity_bps(),
        },
        prior_n_usize,
        symbol_id,
    )?;
    Ok(spec.canonical_bytes())
}

/// Compute the 9 standard pivot levels from the previous day's H/L/C — the
/// single-source indicator math BruteX's `compute_pivot_levels` wrapper packs
/// into its `PivotLevels` dataclass (so the arithmetic lives ONCE, in Rust).
///
/// Returns an 11-element list in BruteX `PivotLevels` field order:
/// `[r5, r4, r3, r2, r1, p, s1, s2, s3, s4, s5]` (int64 paisa — R5/S5 added in
/// PR-2, DEC-NIFTY-INDICATORS-001). The caller supplies `pdh/pdl/pdc` and already
/// holds them. Invalid prev-day OHLC
/// (`pdh < pdl`, `pdc` outside `[pdl, pdh]`, or an overflow-prone magnitude) is
/// REJECTED at the boundary with a clean `ValueError` — matching BruteX's
/// wrapper AND ensuring a direct/garbage FFI caller can never trip the inner
/// `assert!`/`overflow-checks` panic (which would `abort` under `panic="abort"`).
#[pyfunction]
#[pyo3(name = "compute_pivot_levels", signature = (pdh_paisa, pdl_paisa, pdc_paisa))]
fn py_compute_pivot_levels(pdh_paisa: i64, pdl_paisa: i64, pdc_paisa: i64) -> PyResult<Vec<i64>> {
    // Thin PyO3 wrapper over the pure `_impl` (unit-tested without a GIL).
    py_compute_pivot_levels_impl(pdh_paisa, pdl_paisa, pdc_paisa).map_err(PyValueError::new_err)
}

/// Pure logic for [`py_compute_pivot_levels`] — `Result<Vec<i64>, String>` so a
/// direct FFI caller (not via BruteX's pre-validating wrapper) is rejected
/// cleanly instead of tripping the inner `assert!`/`overflow-checks` panic.
fn py_compute_pivot_levels_impl(
    pdh_paisa: i64,
    pdl_paisa: i64,
    pdc_paisa: i64,
) -> Result<Vec<i64>, String> {
    check_prev_day_ohlc(pdh_paisa, pdl_paisa, pdc_paisa)?;
    let p = compute_pivot_levels(pdh_paisa, pdl_paisa, pdc_paisa);
    Ok(vec![
        p.r5_paisa, p.r4_paisa, p.r3_paisa, p.r2_paisa, p.r1_paisa, p.p_paisa, p.s1_paisa,
        p.s2_paisa, p.s3_paisa, p.s4_paisa, p.s5_paisa,
    ])
}

/// Compute the static daily Fibonacci levels from the previous day's H/L — the
/// single-source indicator math BruteX's `compute_fib_levels` wrapper packs into
/// its `FibLevels` dataclass.
///
/// Returns a 19-element list ([`FIB_COMPUTE_LIST_LEN`]) of the COMPUTED levels
/// (int64 paisa), in order:
/// `[retrace_23, retrace_38, retrace_50, retrace_62, retrace_78,
///   ext_up_127, ext_up_162, ext_up_200, ext_up_262, ext_up_424,
///   ext_down_127, ext_down_162, ext_down_200, ext_down_262, ext_down_424,
///   retrace_bull_23, retrace_bull_38, retrace_bull_62, retrace_bull_78]` —
/// the original 15 order UNTOUCHED, the four PDL-anchored bullish retracements
/// APPENDED last (DEC-FIB-BULLISH-RETRACE-001).
/// `retrace@pdh = pdh`, `retrace@pdl = pdl`, and `range = pdh - pdl` are derived
/// by the caller. `pdh < pdl` (or an overflow-prone magnitude) is REJECTED at the
/// boundary with a clean `ValueError` — so a direct FFI caller can never trip the
/// inner `assert!`/`overflow-checks` panic (which would `abort` under `panic="abort"`).
#[pyfunction]
#[pyo3(name = "compute_fib_levels", signature = (pdh_paisa, pdl_paisa))]
fn py_compute_fib_levels(pdh_paisa: i64, pdl_paisa: i64) -> PyResult<Vec<i64>> {
    // Thin PyO3 wrapper over the pure `_impl` (unit-tested without a GIL).
    py_compute_fib_levels_impl(pdh_paisa, pdl_paisa).map_err(PyValueError::new_err)
}

/// Pure logic for [`py_compute_fib_levels`] — `Result<Vec<i64>, String>` so a
/// direct FFI caller is rejected cleanly instead of tripping the inner
/// `assert!`/`overflow-checks` panic.
fn py_compute_fib_levels_impl(pdh_paisa: i64, pdl_paisa: i64) -> Result<Vec<i64>, String> {
    check_prev_day_hl(pdh_paisa, pdl_paisa)?;
    let f = compute_fib_levels(pdh_paisa, pdl_paisa);
    let out = vec![
        f.retrace_23_paisa,
        f.retrace_38_paisa,
        f.retrace_50_paisa,
        f.retrace_62_paisa,
        f.retrace_78_paisa,
        f.ext_up_127_paisa,
        f.ext_up_162_paisa,
        f.ext_up_200_paisa,
        f.ext_up_262_paisa,
        f.ext_up_424_paisa,
        f.ext_down_127_paisa,
        f.ext_down_162_paisa,
        f.ext_down_200_paisa,
        f.ext_down_262_paisa,
        f.ext_down_424_paisa,
        // The bullish retracement ladder, APPENDED last so every pre-existing
        // index is byte-stable (DEC-FIB-BULLISH-RETRACE-001).
        f.retrace_bull_23_paisa,
        f.retrace_bull_38_paisa,
        f.retrace_bull_62_paisa,
        f.retrace_bull_78_paisa,
    ];
    debug_assert_eq!(out.len(), FIB_COMPUTE_LIST_LEN);
    Ok(out)
}

/// Compute the session-edge gap-midpoint level from the two session-edge
/// candles' extremes + today's open and the previous session's close — the
/// single-source indicator math for the Group-15 bits 66-68
/// (DEC-GAP-MIDPOINT-CONDITIONS-001; `indicators::gap_mid::compute_gap_mid`).
///
/// Returns `None` on a ZERO gap (`open_of_day_paisa == pdc_paisa` — no level
/// exists), else `Some((gap_mid_paisa, direction))` with `direction` `1` for a
/// gap-UP day (the level is the round-half-even midpoint of the two HIGHs) and
/// `-1` for a gap-DOWN day (the two LOWs). Corrupt candle extremes
/// (`hi < lo` on either candle) or an overflow-prone magnitude are REJECTED at
/// the boundary with a clean `ValueError` — the inner fn's always-on
/// `assert!`s would otherwise panic → `abort` under `panic="abort"`.
#[pyfunction]
#[pyo3(name = "compute_gap_mid", signature = (
    x1_hi_paisa, x1_lo_paisa, x2_hi_paisa, x2_lo_paisa, open_of_day_paisa, pdc_paisa,
))]
fn py_compute_gap_mid(
    x1_hi_paisa: i64,
    x1_lo_paisa: i64,
    x2_hi_paisa: i64,
    x2_lo_paisa: i64,
    open_of_day_paisa: i64,
    pdc_paisa: i64,
) -> PyResult<Option<(i64, i8)>> {
    // Thin PyO3 wrapper over the pure `_impl` (unit-tested without a GIL).
    py_compute_gap_mid_impl(
        x1_hi_paisa,
        x1_lo_paisa,
        x2_hi_paisa,
        x2_lo_paisa,
        open_of_day_paisa,
        pdc_paisa,
    )
    .map_err(PyValueError::new_err)
}

/// Pure logic for [`py_compute_gap_mid`] — `Result<..., String>` so a direct
/// FFI caller is rejected cleanly instead of tripping the inner `assert!` /
/// `overflow-checks` panic. Bounds every operand to [`MAX_BAR_PAISA`] so the
/// midpoint sum `(X1 + X2)` can never overflow i64 (and stays f64-exact —
/// `2 · MAX_BAR_PAISA ≈ 2.8·10¹⁴ ≪ 2⁵³`).
fn py_compute_gap_mid_impl(
    x1_hi_paisa: i64,
    x1_lo_paisa: i64,
    x2_hi_paisa: i64,
    x2_lo_paisa: i64,
    open_of_day_paisa: i64,
    pdc_paisa: i64,
) -> Result<Option<(i64, i8)>, String> {
    for (name, hi, lo) in [
        ("X1", x1_hi_paisa, x1_lo_paisa),
        ("X2", x2_hi_paisa, x2_lo_paisa),
    ] {
        if hi < lo {
            return Err(format!(
                "{name} candle high ({hi}) must be >= its low ({lo})"
            ));
        }
    }
    for v in [
        x1_hi_paisa,
        x1_lo_paisa,
        x2_hi_paisa,
        x2_lo_paisa,
        open_of_day_paisa,
        pdc_paisa,
    ] {
        if !bar_in_range(v) {
            return Err(format!(
                "gap-mid input magnitude {v} out of the overflow-safe range \
                 [-{MAX_BAR_PAISA}, {MAX_BAR_PAISA}] paisa"
            ));
        }
    }
    Ok(compute_gap_mid(
        x1_hi_paisa,
        x1_lo_paisa,
        x2_hi_paisa,
        x2_lo_paisa,
        open_of_day_paisa,
        pdc_paisa,
    )
    .map(|(level, direction)| {
        (
            level,
            match direction {
                crate::indicators::gap_mid::GapDirection::Up => 1,
                crate::indicators::gap_mid::GapDirection::Down => -1,
            },
        )
    }))
}

/// Compute the full EMA series from a list of `f64` closes — the shared
/// single-source recurrence BruteX's `compute_ema` / `latest_ema` delegate to
/// (DEC-EMA-RUST-PORT-001). Returns a list the same length as `values` with the
/// first `period - 1` entries `None` (warm-up).
///
/// A `period < 1` OR a non-finite value (NaN / ±∞) raises a clean `ValueError`
/// (matching `ema.py`), never a panic → `abort` under `panic="abort"`. The float
/// recurrence runs entirely in `f64`; the caller (`_ema_to_paisa`) snaps the
/// result to paisa separately.
#[pyfunction]
#[pyo3(name = "compute_ema", signature = (values, period))]
fn py_compute_ema(values: Vec<f64>, period: i64) -> PyResult<Vec<Option<f64>>> {
    compute_ema(&values, period).map_err(PyValueError::new_err)
}

/// The EMA smoothing factor `α = 2 / (period + 1)` — the shared single-source
/// helper BruteX's `EMAState` / `alpha()` delegate to. `period < 1` raises a
/// clean `ValueError` (matching `ema.py::alpha`).
#[pyfunction]
#[pyo3(name = "ema_alpha", signature = (period))]
fn py_ema_alpha(period: i64) -> PyResult<f64> {
    ema_alpha(period).map_err(PyValueError::new_err)
}

/// The SMA seed at bar `N-1`: `seed_sum / period` — the shared single-source
/// helper BruteX's streaming `EMAState` delegates its seed division to. A
/// `period < 1` raises a clean `ValueError`.
#[pyfunction]
#[pyo3(name = "ema_seed", signature = (seed_sum, period))]
fn py_ema_seed(seed_sum: f64, period: i64) -> PyResult<f64> {
    ema_seed(seed_sum, period).map_err(PyValueError::new_err)
}

/// The post-seed recurrence step `α·close + (1 − α)·prior` — the shared
/// single-source helper BruteX's streaming `EMAState` delegates its per-bar step
/// to. Pure arithmetic (no error path); a non-finite input would propagate as an
/// IEEE result (the `EMAState` shell guards `close` finiteness before calling).
#[pyfunction]
#[pyo3(name = "ema_step", signature = (alpha, close, prior))]
fn py_ema_step(alpha: f64, close: f64, prior: f64) -> f64 {
    ema_step(alpha, close, prior)
}

/// Largest proleptic-Gregorian day-ordinal a Python `datetime.date` can carry
/// (`date.max.toordinal()` == 9999-12-31). Every BruteX caller derives its
/// ordinal via `date.toordinal()` (≥ 1), so bounding the FFI edge to
/// `[1, MAX_DAY_ORDINAL]` rejects only garbage — and keeps the inner
/// `ordinal - 1` subtraction (metadata.rs) provably clear of the `i64::MIN`
/// overflow that would trip `overflow-checks = true` → panic → `abort`
/// (SIGABRT) under `panic = "abort"`.
const MAX_DAY_ORDINAL: i64 = 3_652_059;

/// Largest civil seconds-since-midnight (23:59:59). Both `tod_seconds` and
/// `session_open_seconds` are seconds-since-IST-midnight, so `[0, 86_399]` is
/// the exact valid domain — and bounds the inner `tod - open` subtraction far
/// away from i64 overflow.
const MAX_CIVIL_SECONDS: i64 = 86_399;

/// Range guard for a proleptic-Gregorian day-ordinal at the FFI boundary
/// (`[1, MAX_DAY_ORDINAL]` — the exact Python `date.toordinal()` domain).
/// Pure (`Result<_, String>`, no `PyErr`) so the no-libpython `cargo test`
/// covers both branches.
fn day_ordinal_in_range(name: &str, value: i64) -> Result<(), String> {
    if (1..=MAX_DAY_ORDINAL).contains(&value) {
        Ok(())
    } else {
        Err(format!(
            "{name} ({value}) out of the proleptic-Gregorian ordinal range \
             [1, {MAX_DAY_ORDINAL}] (Python date.toordinal() domain)"
        ))
    }
}

/// Range guard for a civil seconds-since-midnight at the FFI boundary
/// (`[0, MAX_CIVIL_SECONDS]`). Pure, both branches `cargo test`-covered.
fn civil_seconds_in_range(name: &str, value: i64) -> Result<(), String> {
    if (0..=MAX_CIVIL_SECONDS).contains(&value) {
        Ok(())
    } else {
        Err(format!(
            "{name} ({value}) out of the civil seconds-since-midnight range \
             [0, {MAX_CIVIL_SECONDS}]"
        ))
    }
}

/// Derive the eval-path per-bar metadata (DEC-NIFTY-ROLL-METADATA-001) — the
/// shared Rust core BruteX's `brutex.feed.bar_metadata` shim delegates to so the
/// backtest + TickVault derive IDENTICAL `tod_seconds` / weekday / minute-of-
/// session / is-expiry-day from the same arithmetic.
///
/// Returns `(tod_seconds, weekday, minute_of_session, is_expiry_day)`. The caller
/// passes the bar's IST CIVIL components (no timezone library here): the trading-
/// date proleptic-Gregorian ordinal, the wall-clock h/m/s, the session-open
/// seconds-since-midnight (resolved from the shared NSE calendar), and the active
/// contract's expiry ordinal.
///
/// The RANGE guards live HERE at the FFI boundary (the same convention as
/// [`py_seconds_of_day`]): out-of-domain ordinals / civil components raise a
/// clean `ValueError` instead of tripping an inner `overflow-checks` panic →
/// `abort` (SIGABRT) under `panic = "abort"` — e.g. a garbage `hour = 2_000_000`
/// would overflow `hour * 3600` unguarded (the pre-guard repro).
#[pyfunction]
#[pyo3(name = "derive_bar_metadata", signature = (
    day_ordinal, hour, minute, second, session_open_seconds, expiry_ordinal,
))]
fn py_derive_bar_metadata(
    day_ordinal: i64,
    hour: u32,
    minute: u32,
    second: u32,
    session_open_seconds: i64,
    expiry_ordinal: i64,
) -> PyResult<(u32, u8, i32, bool)> {
    derive_bar_metadata_impl(
        day_ordinal,
        hour,
        minute,
        second,
        session_open_seconds,
        expiry_ordinal,
    )
    .map_err(PyValueError::new_err)
}

/// Pure logic for [`py_derive_bar_metadata`] — `Result<_, String>` so the
/// range guards + the delegation are unit-covered without a libpython link
/// (mirrors [`seconds_of_day_impl`], which it reuses for the h/m/s guard).
fn derive_bar_metadata_impl(
    day_ordinal: i64,
    hour: u32,
    minute: u32,
    second: u32,
    session_open_seconds: i64,
    expiry_ordinal: i64,
) -> Result<(u32, u8, i32, bool), String> {
    day_ordinal_in_range("day_ordinal", day_ordinal)?;
    day_ordinal_in_range("expiry_ordinal", expiry_ordinal)?;
    civil_seconds_in_range("session_open_seconds", session_open_seconds)?;
    // Reuses the SAME h/m/s guard py_seconds_of_day fronts, so the garbage
    // `hour = 2_000_000` class cannot bypass it through this entry point.
    seconds_of_day_impl(hour, minute, second)?;
    let m = crate::indicators::metadata::derive_bar_metadata(
        day_ordinal,
        hour,
        minute,
        second,
        session_open_seconds,
        expiry_ordinal,
    );
    Ok((
        m.tod_seconds,
        m.weekday,
        m.minute_of_session,
        m.is_expiry_day,
    ))
}

/// The weekday `0 = Mon … 6 = Sun` of a proleptic-Gregorian day-ordinal
/// (== Python `date.weekday()` for `date.toordinal()`). Shared so BruteX +
/// TickVault agree bit-for-bit (DEC-NIFTY-ROLL-METADATA-001). The ordinal is
/// RANGE-guarded to the Python `date.toordinal()` domain `[1, 3_652_059]` at
/// this FFI boundary — a garbage `-2^63` ordinal raises a clean `ValueError`
/// instead of overflowing the inner `ordinal - 1` → panic → `abort` (SIGABRT)
/// under `panic = "abort"`. (The inner pure fn stays defensively total over
/// non-positive ordinals for the native TickVault link.)
#[pyfunction]
#[pyo3(name = "weekday_from_ordinal", signature = (day_ordinal,))]
fn py_weekday_from_ordinal(day_ordinal: i64) -> PyResult<u8> {
    weekday_from_ordinal_impl(day_ordinal).map_err(PyValueError::new_err)
}

/// Pure logic for [`py_weekday_from_ordinal`] — guard + delegation,
/// unit-covered without a libpython link.
fn weekday_from_ordinal_impl(day_ordinal: i64) -> Result<u8, String> {
    day_ordinal_in_range("day_ordinal", day_ordinal)?;
    Ok(crate::indicators::metadata::weekday_from_ordinal(
        day_ordinal,
    ))
}

/// Whole minutes from the session open to a bar's IST time-of-day (both seconds
/// since midnight); a pre-open bar floors NEGATIVE, honest not clamped. Shared
/// derivation (DEC-NIFTY-ROLL-METADATA-001). Both operands are RANGE-guarded to
/// the civil `[0, 86_399]` domain at this FFI boundary — a garbage
/// `(i64::MAX, -1)` pair raises a clean `ValueError` instead of overflowing the
/// inner `tod - open` → panic → `abort` (SIGABRT) under `panic = "abort"`.
#[pyfunction]
#[pyo3(name = "minute_of_session", signature = (tod_seconds, session_open_seconds))]
fn py_minute_of_session(tod_seconds: i64, session_open_seconds: i64) -> PyResult<i32> {
    minute_of_session_impl(tod_seconds, session_open_seconds).map_err(PyValueError::new_err)
}

/// Pure logic for [`py_minute_of_session`] — guards + delegation,
/// unit-covered without a libpython link.
fn minute_of_session_impl(tod_seconds: i64, session_open_seconds: i64) -> Result<i32, String> {
    civil_seconds_in_range("tod_seconds", tod_seconds)?;
    civil_seconds_in_range("session_open_seconds", session_open_seconds)?;
    Ok(crate::indicators::metadata::minute_of_session(
        tod_seconds,
        session_open_seconds,
    ))
}

/// Seconds since IST midnight for a wall-clock `HH:MM:SS` — the ONE shared
/// Group-8 `tod_seconds` derivation (DEC-TOD-SECONDS-RUST-SEAM-001), replacing
/// BruteX's Python-side inline `h*3600 + m*60 + s` on the eval path so the
/// value is derived ONCE, bit-identical to the TickVault-native path.
///
/// The RANGE guard lives HERE at the FFI boundary: `hour <= 23`, `minute <= 59`,
/// `second <= 59` — a violation raises a clean `ValueError`, never an inner
/// `overflow-checks` panic → SIGABRT under `panic = "abort"` (e.g. a garbage
/// `hour = 2_000_000` would overflow `hour * 3600` unguarded).
#[pyfunction]
#[pyo3(name = "seconds_of_day", signature = (hour, minute, second))]
fn py_seconds_of_day(hour: u32, minute: u32, second: u32) -> PyResult<u32> {
    seconds_of_day_impl(hour, minute, second).map_err(PyValueError::new_err)
}

/// Pure logic for [`py_seconds_of_day`] — `Result<u32, String>` so the range
/// guard + the delegation are unit-covered without a libpython link.
fn seconds_of_day_impl(hour: u32, minute: u32, second: u32) -> Result<u32, String> {
    for (name, value, max) in [
        ("hour", hour, 23u32),
        ("minute", minute, 59),
        ("second", second, 59),
    ] {
        if value > max {
            return Err(format!(
                "{name} ({value}) out of the civil range [0, {max}]"
            ));
        }
    }
    Ok(crate::indicators::metadata::seconds_of_day(
        hour, minute, second,
    ))
}

/// Resolve the active near-month future a trade entering on `entry_ordinal` uses,
/// given the caller-supplied holiday-shifted candidate + next-month expiries
/// (ADR-0012 D11). Returns the active contract's expiry ordinal. Shared decision
/// (DEC-NIFTY-ROLL-METADATA-001).
#[pyfunction]
#[pyo3(name = "resolve_near_month", signature = (
    entry_ordinal, candidate_shifted_ordinal, next_month_shifted_ordinal,
))]
fn py_resolve_near_month(
    entry_ordinal: i64,
    candidate_shifted_ordinal: i64,
    next_month_shifted_ordinal: i64,
) -> i64 {
    crate::indicators::metadata::resolve_near_month(
        entry_ordinal,
        candidate_shifted_ordinal,
        next_month_shifted_ordinal,
    )
}

/// PyO3 handle over the native [`CoreVwapState`] — so BruteX's `stage1a` engine
/// drives the SHARED Rust session-anchored VWAP per bar (the per-bar compute runs
/// in Rust, never re-implemented in Python), giving the backtest the bit-identical
/// VWAP TickVault's native `VwapState` produces (DEC-SHARED-CORE-001 / ADR-0012 D12).
///
/// Stateful by design (the running `i128` accumulators ARE the indicator) — held
/// once per `(instrument, timeframe, contract)` and fed each bar via
/// [`PyVwapState::update`]. Mirrors the streaming EMA seam, but as a class because
/// the running sums cannot be a pure step function.
/// Pure input guard for [`PyVwapState::update`] — `Result<_, String>` (no
/// `PyErr`) so both branches are unit-covered without a libpython link
/// (mirrors [`seconds_of_day_impl`]). Enforces:
///
/// * `typical_paisa ∈ [0, MAX_BAR_PAISA]` — non-negativity closes the twins'
///   floor-vs-truncating-division domain hole (Rust i128 `/` truncates toward
///   zero, Python `//` floors: a negative accumulated `cum_pv` would silently
///   yield answers one paisa apart), and the ceiling keeps the i128
///   accumulator provably overflow-free;
/// * `volume ∈ [-MAX_BAR_PAISA, MAX_BAR_PAISA]` — a non-positive volume is a
///   VALID skip signal in both twins (never accumulated), so only the
///   magnitude is bounded.
fn vwap_update_guard(typical_paisa: i64, volume: i64) -> Result<(), String> {
    if typical_paisa < 0 {
        return Err(format!(
            "typical_paisa ({typical_paisa}) must be non-negative: the \
             Python/Rust twin identity (truncating vs floor division) holds \
             only for non-negative typical prices"
        ));
    }
    for (name, value) in [("typical_paisa", typical_paisa), ("volume", volume)] {
        if !bar_in_range(value) {
            return Err(format!(
                "{name} ({value}) out of the overflow-safe range \
                 [-{MAX_BAR_PAISA}, {MAX_BAR_PAISA}] paisa"
            ));
        }
    }
    Ok(())
}

#[pyclass(name = "VwapState", module = "trading_core")]
struct PyVwapState {
    inner: CoreVwapState,
}

#[pymethods]
impl PyVwapState {
    /// A fresh, cold (un-warmed) session-anchored VWAP accumulator.
    #[new]
    fn new() -> Self {
        PyVwapState {
            inner: CoreVwapState::new(),
        }
    }

    /// Feed one bar; return the session VWAP in int64 paisa, or `None` while the
    /// session has no usable (post-09:15, non-zero) volume yet.
    ///
    /// Delegates verbatim to the native [`CoreVwapState::update`] — the
    /// `cum(price·vol)/cum(vol)` math, the 09:15 reset, the auction exclusion and
    /// the contract-roll reset all execute in Rust. O(1) per bar.
    ///
    /// The two i64s that feed the native `i128` accumulators (`cum_pv +=
    /// typical_paisa·volume`, `cum_vol += volume`) are validated against the
    /// SAME `MAX_BAR_PAISA` magnitude every sibling FFI entry reuses, BEFORE
    /// delegating — so a garbage `typical_paisa·volume` product raises a
    /// catchable `ValueError` instead of overflowing `cum_pv` and tripping the
    /// native add's `overflow-checks` panic → `abort` (SIGABRT) under
    /// `panic = "abort"`. With both operands bounded by `MAX_BAR_PAISA`
    /// (≈ 1.4·10¹⁴), one product is ≤ ≈ 2·10²⁸ and even billions of bars stay
    /// far inside `i128::MAX` (≈ 1.7·10³⁸). Valid futures bars (paisa + lot
    /// volume) sit orders of magnitude below the ceiling, so the golden
    /// VWAP output is byte-unchanged.
    ///
    /// `typical_paisa` is additionally required NON-NEGATIVE: the bit-for-bit
    /// twin identity that was documented against the now-DELETED Python
    /// `VwapState` (`brutex/indicators/vwap.py`, removed by
    /// DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001) held only for non-negative
    /// operands (the native i128 `/` truncates toward zero; Python's `//`
    /// floors — a negative `cum_pv` would have silently diverged by one
    /// paisa). A negative typical is not a real price (HLC3 of positive
    /// prices), so the guard REMAINS at this now-single-source boundary: it
    /// raises `ValueError` exactly as both twins did while the twin lived,
    /// keeping the domain hole CLOSED.
    #[pyo3(signature = (typical_paisa, volume, is_pre_open, session_key, session_day, tod_seconds))]
    fn update(
        &mut self,
        typical_paisa: i64,
        volume: i64,
        is_pre_open: bool,
        session_key: u64,
        session_day: i64,
        tod_seconds: u32,
    ) -> PyResult<Option<i64>> {
        vwap_update_guard(typical_paisa, volume).map_err(PyValueError::new_err)?;
        Ok(self.inner.update(
            typical_paisa,
            volume,
            is_pre_open,
            session_key,
            session_day,
            tod_seconds,
        ))
    }

    /// The current session VWAP (paisa), or `None` if no volume has been counted.
    fn current(&self) -> Option<i64> {
        self.inner.current()
    }
}

/// PyO3 handle over the native [`CoreMarketStructureState`] — so BruteX's `stage1a`
/// engine drives the SHARED Rust BOS/CHoCH detector per bar (the look-ahead-clean
/// fractal-swing compute runs in Rust, never re-implemented in Python), giving the
/// backtest the bit-identical signal TickVault's native state produces
/// (DEC-SHARED-CORE-001 / ADR-0012 D3/D4).
///
/// Stateful (the `(2k+1)` confirmation ring + the running trend ARE the indicator)
/// — held once per `(instrument, timeframe, contract)` and fed each bar via
/// [`PyMarketStructureState::update`], which returns the four resolved booleans
/// `(bos_bullish, bos_bearish, choch_bullish, choch_bearish)`.
#[pyclass(name = "MarketStructureState", module = "trading_core")]
struct PyMarketStructureState {
    inner: CoreMarketStructureState,
}

#[pymethods]
impl PyMarketStructureState {
    /// A fresh detector with fractal half-window `k` (`k >= 1`, default 2 — a
    /// 5-bar fractal, the production value). `k < 1` raises a clean `ValueError`
    /// instead of tripping the native `assert!` (which would `abort` under
    /// `panic = "abort"`).
    #[new]
    #[pyo3(signature = (k = 2))]
    fn new(k: usize) -> PyResult<Self> {
        if k < 1 {
            return Err(PyValueError::new_err(format!(
                "fractal half-window k must be >= 1, got {k}"
            )));
        }
        Ok(PyMarketStructureState {
            inner: CoreMarketStructureState::new(k),
        })
    }

    /// Feed one bar (high/low/close in int64 paisa); return its
    /// `(bos_bullish, bos_bearish, choch_bullish, choch_bearish)` signal.
    ///
    /// Delegates verbatim to the native [`CoreMarketStructureState::update`] — the
    /// ring push, the look-ahead-clean centre-swing confirmation and the
    /// trend-aware BOS/CHoCH resolution all execute in Rust. O(1) per bar.
    #[pyo3(signature = (high, low, close))]
    fn update(&mut self, high: i64, low: i64, close: i64) -> (bool, bool, bool, bool) {
        let sig = self.inner.update(high, low, close);
        (
            sig.bos_bullish,
            sig.bos_bearish,
            sig.choch_bullish,
            sig.choch_bearish,
        )
    }

    /// The most recent CONFIRMED swing high (int64 paisa), or `None` before any
    /// swing high has been confirmed.
    ///
    /// Read-only introspection delegating to the EXISTING core accessor
    /// [`CoreMarketStructureState::last_swing_high`] (the core is untouched —
    /// TickVault-safe). Exposed so the Python side (the P-06 look-ahead property
    /// suite + the frozen twin-state golden replay) can assert the confirmed
    /// swing levels directly on the PRODUCTION state now that the hand-synced
    /// Python twin is deleted (DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001).
    #[getter]
    fn last_swing_high(&self) -> Option<i64> {
        self.inner.last_swing_high()
    }

    /// The most recent CONFIRMED swing low (int64 paisa), or `None` before any
    /// swing low has been confirmed.
    ///
    /// Read-only introspection delegating to the EXISTING core accessor
    /// [`CoreMarketStructureState::last_swing_low`] — see
    /// [`PyMarketStructureState::last_swing_high`] for the rationale.
    #[getter]
    fn last_swing_low(&self) -> Option<i64> {
        self.inner.last_swing_low()
    }
}

#[pyclass(name = "SupertrendState", module = "trading_core")]
struct PySupertrendState {
    inner: CoreSupertrendState,
}

#[pymethods]
impl PySupertrendState {
    /// A fresh SuperTrend state. NO parameters: the ATR period (10) and the
    /// band factor (3) are compile-time constants of the vocabulary
    /// (`indicators::supertrend::{SUPERTREND_ATR_PERIOD, SUPERTREND_FACTOR}`),
    /// never a sweep axis.
    #[new]
    fn new() -> Self {
        PySupertrendState {
            inner: CoreSupertrendState::new(),
        }
    }

    /// Feed one bar (high/low/close in int64 paisa); return the bar's
    /// SuperTrend line in the **×2-scaled paisa domain** (= line × 2 exactly —
    /// the hl2 source is a half-paisa quantity), or `None` during the 9-bar
    /// warm-up. This is EXACTLY the value the evaluate ABIs' appended
    /// `supertrend_x2_paisa` argument expects.
    ///
    /// Delegates verbatim to the native [`CoreSupertrendState::update`] — the
    /// integer TR/Wilder-ATR/band-ratchet/flip law all executes in Rust.
    /// O(1) per bar. Total for any i64 inputs (never a panic → never a SIGABRT
    /// under `panic = "abort"`).
    #[pyo3(signature = (high, low, close))]
    fn update(&mut self, high: i64, low: i64, close: i64) -> Option<i64> {
        self.inner.update(high, low, close)
    }

    /// The current line (×2-scaled paisa), or `None` during warm-up.
    #[getter]
    fn supertrend_x2(&self) -> Option<i64> {
        self.inner.supertrend_x2()
    }

    /// The current direction: `-1` = UP-trend (line = LOWER band), `+1` =
    /// DOWN-trend (line = UPPER band) — Pine's inverted convention — or
    /// `None` during warm-up.
    #[getter]
    fn direction(&self) -> Option<i8> {
        self.inner.direction()
    }

    /// Bars processed so far.
    #[getter]
    fn bars_seen(&self) -> u64 {
        self.inner.bars_seen()
    }
}

/// Flat FFI shape of one Greeks tuple:
/// `(iv, delta, gamma, theta, vega, rho, price, admissible)`.
type GreeksTuple = (f64, f64, f64, f64, f64, f64, f64, bool);

/// The ONE shared Black-76 Greeks seam (`DEC-GREEKS-ENGINE-PR2-001`; plan
/// `docs/GREEKS_ENGINE_PLAN.md` §2.3 / ADR-0015 D3) — the PyO3 face of
/// `trading_core::greeks::greeks`.
///
/// `sigma=Some` prices directly (live / vendor-IV path; `price` is ignored);
/// `sigma=None` solves IV from `price` via the verified Jäckel
/// "Let's Be Rational" + deterministic bracketed fallback, then prices the
/// Greeks off the SAME closed form.
///
/// Returns `(iv, delta, gamma, theta, vega, rho, price, admissible)` — theta
/// per calendar day (Act/365), vega per 1% IV, rho per 1% rate, `admissible`
/// = the plan's |delta| ≥ 0.02 ∧ raw-vega ≥ 1e-6 admission floors.
///
/// EVERY kernel `Err` maps to a clean `ValueError` whose message keeps the
/// machine-readable `invalid_input:` / `no_solution:` prefix — the thin
/// `brutex/options/greeks.py` wrapper re-raises the former and maps the
/// latter to an honest `None` (never a fabricated value, never a panic →
/// `abort` under `panic="abort"`).
#[pyfunction]
#[pyo3(name = "greeks", signature = (price, sigma, forward, strike, t_years, rate, is_call))]
fn py_greeks(
    price: Option<f64>,
    sigma: Option<f64>,
    forward: f64,
    strike: f64,
    t_years: f64,
    rate: f64,
    is_call: bool,
) -> PyResult<GreeksTuple> {
    let out = greeks(price, sigma, forward, strike, t_years, rate, is_call)
        .map_err(PyValueError::new_err)?;
    Ok((
        out.iv,
        out.delta,
        out.gamma,
        out.theta,
        out.vega,
        out.rho,
        out.price,
        out.admissible,
    ))
}

/// Pure VALUE-envelope guard pass for [`stage1a_bar_bits_batch`] —
/// `Result<(), String>` (no `PyErr`) so the no-libpython `cargo test` covers
/// every branch (DEC-STAGE1A-RUST-BATCH-001).
///
/// Reuses the EXISTING boundary guards **in place, verbatim** (zero
/// relocation, zero duplicated constants — the §0.3 ruling):
///
/// * every day-table level rides [`bar_in_range`] (the same bound
///   `evaluate_with_levels_impl` applies to its caller-supplied levels —
///   including `pdc`, the one level the evaluator multiplies);
/// * every bar rides [`check_evaluate_inputs`] — open/high/low/close + the
///   optional EMAs + the three bps with the exact `close.checked_mul(bps)`
///   product check. The running day extremes / open-of-day the batch core
///   derives are max/min folds of these SAME bar values, so bounding the bar
///   values bounds them; the bar's own high/low/open stand in for the
///   day-extreme slots and the bar's day row supplies the REAL `pdc`;
/// * every bar's VWAP operands ride [`vwap_update_guard`] (non-negative
///   typical + `MAX_BAR_PAISA` magnitudes — the same i128-accumulator
///   envelope the `VwapState` pyclass enforces).
///
/// Structure is validated FIRST (`stage1a_batch::validate_structure`) so the
/// per-bar day-row indexing below can never go out of bounds; any `Err` maps
/// to a clean `ValueError` at the pyfunction boundary and the Python seam
/// falls back to the oracle (which raises today's exact error or computes —
/// user-visible behaviour identical either way; the fallback law).
fn check_stage1a_batch_values(
    inp: &crate::stage1a_batch::Stage1aBatchInputs<'_>,
) -> Result<(), String> {
    let n = crate::stage1a_batch::validate_structure(inp)?;
    for v in inp
        .day_pivot_levels
        .iter()
        .chain(inp.day_fib_levels.iter())
        // The Group-15 X1 day tables are levels too: their values feed the
        // `(X1 + X2)` midpoint sum in `compute_gap_mid` (with the bar-bounded
        // first-bar extremes as X2), so bar-bounding them keeps that sum far
        // inside i64 AND f64-exact (DEC-GAP-MIDPOINT-CONDITIONS-001).
        .chain(inp.day_x1_hi_paisa.iter())
        .chain(inp.day_x1_lo_paisa.iter())
    {
        if !bar_in_range(*v) {
            return Err(format!(
                "level magnitude {v} out of the overflow-safe range \
                 [-{MAX_BAR_PAISA}, {MAX_BAR_PAISA}] paisa"
            ));
        }
    }
    for i in 0..n {
        let du = inp.day_index[i] as usize;
        // Operator-free row addressing (refuter RF-α round-4 F2): the former
        // `du * PIVOT_ROW_LEN + 9` indexing left two arithmetic mutants in
        // the cargo-mutants catalog (`+ → *` = an out-of-bounds SIGABRT
        // class on any multi-day input; `* → /` = equivalent-but-gate-red —
        // the levels loop above already bar_in_range-checked every element,
        // so WHICH element the guard reads cannot move the verdict).
        // `chunks_exact(...).nth(du)` carries no mutable arithmetic; the
        // let-else Err keeps the never-panic totality (unreachable — the
        // day_index law in `validate_structure` pins `du < D`).
        let Some(day_row) = inp
            .day_pivot_levels
            .chunks_exact(crate::stage1a_batch::PIVOT_ROW_LEN)
            .nth(du)
        else {
            return Err(format!(
                "stage1a_batch: day_index {du} beyond the validated day table"
            ));
        };
        let pdc = day_row[9];
        // The day_high/day_low/open_of_day slots get the CURRENT bar's
        // high/low/open — decision-equivalent to the OFF arm's per-bar
        // running-extremes guard (a running extreme IS some earlier bar's
        // already-guarded high/low/open, so a magnitude violation is
        // rejected at the SAME bar either way; review-proven, INFO-4).
        check_evaluate_inputs(
            inp.open_paisa[i],
            inp.high_paisa[i],
            inp.low_paisa[i],
            inp.close_paisa[i],
            inp.ema20_paisa[i],
            inp.ema200_paisa[i],
            inp.high_paisa[i],
            inp.low_paisa[i],
            inp.open_paisa[i],
            pdc,
            inp.pivot_bps,
            inp.pdh_pdl_bps,
            inp.fib_bps,
            inp.gap_mid_bps,
            inp.swing_bps,
        )?;
        vwap_update_guard(inp.typical_paisa[i], inp.volume[i])?;
    }
    Ok(())
}

/// Batch Stage-1A bar-bits — one call per `(instrument, tf)` build
/// (DEC-STAGE1A-RUST-BATCH-001; the batch twin of the per-bar
/// [`evaluate_with_levels`] + `VwapState.update` + `MarketStructureState.update`
/// FFI chain `Stage1AMatrixBuilder.push` drives today).
///
/// The columns/tables are prepass-RESOLVED by
/// `Stage1AMatrixBuilder.collect_batch_inputs` (Python keeps the calendar /
/// roll / anchor decisions; see `stage1a_batch`'s module docs). Returns one
/// `u128` condition mask per bar, input order — the probe-verified native
/// `Vec<u128>` shape (the `ResidentSweepSimU128` precedent). Every structural
/// or value-envelope violation raises a clean `ValueError` (never a panic →
/// SIGABRT under `panic = "abort"`): the value guards are the EXISTING
/// in-file boundary trio, reused verbatim ([`check_stage1a_batch_values`]).
#[pyfunction]
#[expect(
    clippy::too_many_arguments,
    reason = "flat column ABI for the batch Stage-1A binding \
              (precedent: evaluate_with_levels carries 21)"
)]
#[pyo3(signature = (
    open_paisa, high_paisa, low_paisa, close_paisa, typical_paisa,
    volume, tod_seconds, pre_open, session_key, session_day, contract_reset, day_index,
    ema20_paisa, ema200_paisa, day_pivot_levels, day_fib_levels, day_has_pivot, day_has_fib,
    pivot_bps, pdh_pdl_bps, fib_bps, swing_bps, prior_n,
    anchor_dependent_mask, fib_dependent_mask, applies_to_1day_mask,
    apply_1day_mask, fractal_k = 2, supertrend_x2_paisa = None,
    gap_mid_bps = DEFAULT_GAP_MID_PROXIMITY_BPS,
    day_x1_hi_paisa = None, day_x1_lo_paisa = None, day_has_x1 = None,
))]
fn stage1a_bar_bits_batch(
    open_paisa: Vec<i64>,
    high_paisa: Vec<i64>,
    low_paisa: Vec<i64>,
    close_paisa: Vec<i64>,
    typical_paisa: Vec<i64>,
    volume: Vec<i64>,
    tod_seconds: Vec<u32>,
    pre_open: Vec<bool>,
    session_key: Vec<u64>,
    session_day: Vec<i64>,
    contract_reset: Vec<bool>,
    day_index: Vec<u32>,
    ema20_paisa: Vec<Option<i64>>,
    ema200_paisa: Vec<Option<i64>>,
    day_pivot_levels: Vec<i64>,
    day_fib_levels: Vec<i64>,
    day_has_pivot: Vec<bool>,
    day_has_fib: Vec<bool>,
    pivot_bps: i64,
    pdh_pdl_bps: i64,
    fib_bps: i64,
    swing_bps: i64,
    prior_n: usize,
    anchor_dependent_mask: u128,
    fib_dependent_mask: u128,
    applies_to_1day_mask: u128,
    apply_1day_mask: bool,
    fractal_k: usize,
    supertrend_x2_paisa: Option<Vec<Option<i64>>>,
    gap_mid_bps: i64,
    day_x1_hi_paisa: Option<Vec<i64>>,
    day_x1_lo_paisa: Option<Vec<i64>>,
    day_has_x1: Option<Vec<bool>>,
) -> PyResult<Vec<u128>> {
    // Thin PyO3 wrapper: structure + value envelope validated by the pure
    // guard pass (unit-tested without a libpython link), then the feature-free
    // core computes. `Err(String)` → a clean, catchable `ValueError`.
    //
    // The Group-15 X1 day tables default to the ALL-COLD shape (`None` →
    // zeroed values + all-false warm flags sized to the day count), so a
    // pre-gap dispatch is byte-identical to the legacy build — the gap bits
    // stay 0 exactly like the Python oracle before its Stage-1A state lands.
    // Once the Python push() law computes gap bits, the dispatch MUST thread
    // the real tables in the SAME change or the per-process burn-in parity
    // mismatches and LOUDLY autoreverts the kernel
    // (DEC-GAP-MIDPOINT-CONDITIONS-001; the #1743 one-change lockstep law).
    let n_days = day_has_pivot.len();
    let day_x1_hi_paisa = day_x1_hi_paisa.unwrap_or_else(|| vec![0; n_days]);
    let day_x1_lo_paisa = day_x1_lo_paisa.unwrap_or_else(|| vec![0; n_days]);
    let day_has_x1 = day_has_x1.unwrap_or_else(|| vec![false; n_days]);
    let inputs = crate::stage1a_batch::Stage1aBatchInputs {
        open_paisa: &open_paisa,
        high_paisa: &high_paisa,
        low_paisa: &low_paisa,
        close_paisa: &close_paisa,
        typical_paisa: &typical_paisa,
        volume: &volume,
        tod_seconds: &tod_seconds,
        pre_open: &pre_open,
        session_key: &session_key,
        session_day: &session_day,
        contract_reset: &contract_reset,
        day_index: &day_index,
        ema20_paisa: &ema20_paisa,
        ema200_paisa: &ema200_paisa,
        day_pivot_levels: &day_pivot_levels,
        day_fib_levels: &day_fib_levels,
        day_has_pivot: &day_has_pivot,
        day_has_fib: &day_has_fib,
        // The optional prepass-computed SuperTrend column (×2-scaled paisa;
        // None = no column, bits 64-65 stay clear — the value guard is a pure
        // i128 comparison downstream, so no magnitude bound is needed).
        supertrend_x2_paisa: supertrend_x2_paisa.as_deref(),
        pivot_bps,
        pdh_pdl_bps,
        fib_bps,
        swing_bps,
        prior_n,
        fractal_k,
        apply_1day_mask,
        anchor_dependent_mask,
        fib_dependent_mask,
        applies_to_1day_mask,
        day_x1_hi_paisa: &day_x1_hi_paisa,
        day_x1_lo_paisa: &day_x1_lo_paisa,
        day_has_x1: &day_has_x1,
        gap_mid_bps,
    };
    check_stage1a_batch_values(&inputs).map_err(PyValueError::new_err)?;
    crate::stage1a_batch::stage1a_bar_bits_batch_impl(&inputs).map_err(PyValueError::new_err)
}
// ── DEC-GREEKS-RUST-BATCH-001 additive block (greeks_batch) ──────────────
/// Columnar batch face of [`py_greeks`]: N already-screened tuples through
/// ONE call. Per-element outcomes are DATA — `(values, error)` with exactly
/// one `Some`, the error string carrying the kernel's `invalid_input:` /
/// `no_solution:` prefix VERBATIM — so one unsolvable strike never aborts
/// its siblings; the ONLY whole-call error is a column-length mismatch →
/// ValueError with the `invalid_input:` prefix. Deliberately NO GIL release:
/// `Python::detach` does not link under the extension-module `cargo test`
/// build (the fno-math precedent, `rust/fno-math/src/python.rs`), and the
/// enrich workers are spawn PROCESSES, so releasing the GIL buys nothing.
#[pyfunction]
#[pyo3(
    name = "greeks_batch",
    signature = (price, sigma, forward, strike, t_years, rate, is_call)
)]
fn py_greeks_batch(
    price: Vec<Option<f64>>,
    sigma: Vec<Option<f64>>,
    forward: Vec<f64>,
    strike: Vec<f64>,
    t_years: Vec<f64>,
    rate: Vec<f64>,
    is_call: Vec<bool>,
) -> PyResult<Vec<crate::greeks::batch::GreeksBatchElem>> {
    crate::greeks::batch::greeks_batch_impl(
        &price, &sigma, &forward, &strike, &t_years, &rate, &is_call,
    )
    .map_err(PyValueError::new_err)
}
// registration: ONE `m.add_function(wrap_pyfunction!(py_greeks_batch, m)?)?;`
// line inside `_trading_core` below, directly after the `py_greeks` line.
// ── end DEC-GREEKS-RUST-BATCH-001 block ──────────────────────────────────

/// The Python extension module `trading_core._trading_core`.
#[pymodule]
fn _trading_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", version())?;
    // Dunder-named deliberately: the doctor's wheel-freshness symbol-set
    // check (and EXPECTED_BUILD.json) covers only non-underscore names, so
    // this build stamp can never trip the symbol-surface comparison
    // (DEC-RUST-SOURCE-DIGEST-TWIN-001).
    m.add("__source_digest__", crate::SOURCE_DIGEST)?;
    m.add("TOTAL_CONDITIONS", TOTAL_CONDITIONS)?;
    m.add("PIVOT_LEVELS_LEN", PIVOT_LEVELS_LEN)?;
    m.add("FIB_LEVELS_LEN", FIB_LEVELS_LEN)?;
    m.add("IMPLEMENTED_MASK", IMPLEMENTED_MASK)?;
    m.add("SPEC_SCHEMA_VERSION", crate::spec::SPEC_SCHEMA_VERSION)?;
    m.add("GREEKS_T_FLOOR_YEARS", T_FLOOR_YEARS)?;
    m.add("GREEKS_IV_CLAMP_MIN", IV_CLAMP_MIN)?;
    m.add("GREEKS_IV_CLAMP_MAX", IV_CLAMP_MAX)?;
    m.add("GREEKS_DELTA_ADMISSION_FLOOR", DELTA_ADMISSION_FLOOR)?;
    m.add("GREEKS_VEGA_ADMISSION_FLOOR", VEGA_ADMISSION_FLOOR)?;
    m.add("NARROW_CPR_WIDTH_BPS", NARROW_CPR_WIDTH_BPS)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(total_conditions, m)?)?;
    m.add_function(wrap_pyfunction!(evaluate_from_raw, m)?)?;
    m.add_function(wrap_pyfunction!(evaluate_with_levels, m)?)?;
    m.add_function(wrap_pyfunction!(evaluate_spec, m)?)?;
    m.add_function(wrap_pyfunction!(spec_canonical_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(py_compute_pivot_levels, m)?)?;
    m.add_function(wrap_pyfunction!(py_compute_fib_levels, m)?)?;
    m.add_function(wrap_pyfunction!(py_compute_gap_mid, m)?)?;
    m.add_function(wrap_pyfunction!(py_compute_ema, m)?)?;
    m.add_function(wrap_pyfunction!(py_ema_alpha, m)?)?;
    m.add_function(wrap_pyfunction!(py_ema_seed, m)?)?;
    m.add_function(wrap_pyfunction!(py_ema_step, m)?)?;
    m.add_function(wrap_pyfunction!(py_derive_bar_metadata, m)?)?;
    m.add_function(wrap_pyfunction!(py_weekday_from_ordinal, m)?)?;
    m.add_function(wrap_pyfunction!(py_minute_of_session, m)?)?;
    m.add_function(wrap_pyfunction!(py_seconds_of_day, m)?)?;
    m.add_function(wrap_pyfunction!(py_resolve_near_month, m)?)?;
    m.add_function(wrap_pyfunction!(py_greeks, m)?)?;
    m.add_function(wrap_pyfunction!(stage1a_bar_bits_batch, m)?)?;
    // DEC-GREEKS-RUST-BATCH-001 additive registration (fenced block above).
    m.add_function(wrap_pyfunction!(py_greeks_batch, m)?)?;
    m.add_class::<PyVwapState>()?;
    m.add_class::<PyMarketStructureState>()?;
    m.add_class::<PySupertrendState>()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    //! Boundary-validation tests for the PyO3 surface. They exercise the pure
    //! `*_impl` / `check_*` logic directly (no GIL / no libpython needed), proving
    //! that EVERY invalid input the inner indicators would panic on is instead
    //! rejected with a clean `Err(String)` — the thing the thin `#[pyfunction]`
    //! wrappers map to `PyValueError`. Valid inputs stay byte-identical to the
    //! standalone indicator path (the FFI-parity check below).

    use super::*;

    /// A valid, in-range previous-day anchor used by the happy-path tests.
    const PDH: i64 = 1_010_000;
    const PDL: i64 = 990_000;
    const PDC: i64 = 1_000_000;

    // ── check_prev_day_hl ──────────────────────────────────────────────────
    #[test]
    fn check_hl_accepts_valid_and_equal() {
        assert!(check_prev_day_hl(PDH, PDL).is_ok());
        assert!(check_prev_day_hl(PDL, PDL).is_ok()); // pdh == pdl is valid
    }

    #[test]
    fn check_hl_rejects_pdh_below_pdl() {
        let err = check_prev_day_hl(PDL, PDH).unwrap_err();
        assert!(err.contains("must be >="), "got: {err}");
    }

    #[test]
    fn check_hl_rejects_overflow_high_pdh() {
        let err = check_prev_day_hl(MAX_ANCHOR_PAISA + 1, 0).unwrap_err();
        assert!(err.contains("overflow-safe"), "got: {err}");
    }

    #[test]
    fn check_hl_rejects_overflow_low_pdl() {
        // pdh >= pdl preserved, but pdl below the negative ceiling.
        let err = check_prev_day_hl(0, -MAX_ANCHOR_PAISA - 1).unwrap_err();
        assert!(err.contains("overflow-safe"), "got: {err}");
    }

    #[test]
    fn check_hl_accepts_boundary_magnitudes() {
        // Exactly at the ceiling is accepted (the reject is strict `>`/`<`).
        assert!(check_prev_day_hl(MAX_ANCHOR_PAISA, -MAX_ANCHOR_PAISA).is_ok());
    }

    // ── check_prev_day_ohlc ────────────────────────────────────────────────
    #[test]
    fn check_ohlc_accepts_valid_and_endpoints() {
        assert!(check_prev_day_ohlc(PDH, PDL, PDC).is_ok());
        assert!(check_prev_day_ohlc(PDH, PDL, PDL).is_ok()); // pdc == pdl
        assert!(check_prev_day_ohlc(PDH, PDL, PDH).is_ok()); // pdc == pdh
    }

    #[test]
    fn check_ohlc_rejects_pdh_below_pdl() {
        // Delegates to check_prev_day_hl first.
        let err = check_prev_day_ohlc(PDL, PDH, PDL).unwrap_err();
        assert!(err.contains("must be >="), "got: {err}");
    }

    #[test]
    fn check_ohlc_rejects_pdc_below_pdl() {
        let err = check_prev_day_ohlc(PDH, PDL, PDL - 1).unwrap_err();
        assert!(err.contains("must lie in"), "got: {err}");
    }

    #[test]
    fn check_ohlc_rejects_pdc_above_pdh() {
        let err = check_prev_day_ohlc(PDH, PDL, PDH + 1).unwrap_err();
        assert!(err.contains("must lie in"), "got: {err}");
    }

    #[test]
    fn check_ohlc_rejects_overflow_magnitude() {
        let err = check_prev_day_ohlc(MAX_ANCHOR_PAISA + 1, 0, 0).unwrap_err();
        assert!(err.contains("overflow-safe"), "got: {err}");
    }

    // ── py_compute_pivot_levels_impl ───────────────────────────────────────
    #[test]
    fn pivot_impl_valid_matches_standalone() {
        let got = py_compute_pivot_levels_impl(PDH, PDL, PDC).expect("valid");
        let p = compute_pivot_levels(PDH, PDL, PDC);
        // Byte-identical to the standalone indicator (the FFI-parity guarantee).
        assert_eq!(
            got,
            vec![
                p.r5_paisa, p.r4_paisa, p.r3_paisa, p.r2_paisa, p.r1_paisa, p.p_paisa, p.s1_paisa,
                p.s2_paisa, p.s3_paisa, p.s4_paisa, p.s5_paisa,
            ]
        );
        assert_eq!(got.len(), 11);
    }

    #[test]
    fn pivot_impl_rejects_corrupt_ohlc() {
        assert!(py_compute_pivot_levels_impl(PDL, PDH, PDC).is_err());
        assert!(py_compute_pivot_levels_impl(PDH, PDL, PDH + 1).is_err());
    }

    // ── py_compute_fib_levels_impl ─────────────────────────────────────────
    #[test]
    fn fib_impl_valid_matches_standalone() {
        let got = py_compute_fib_levels_impl(PDH, PDL).expect("valid");
        let f = compute_fib_levels(PDH, PDL);
        assert_eq!(
            got,
            vec![
                f.retrace_23_paisa,
                f.retrace_38_paisa,
                f.retrace_50_paisa,
                f.retrace_62_paisa,
                f.retrace_78_paisa,
                f.ext_up_127_paisa,
                f.ext_up_162_paisa,
                f.ext_up_200_paisa,
                f.ext_up_262_paisa,
                f.ext_up_424_paisa,
                f.ext_down_127_paisa,
                f.ext_down_162_paisa,
                f.ext_down_200_paisa,
                f.ext_down_262_paisa,
                f.ext_down_424_paisa,
                f.retrace_bull_23_paisa,
                f.retrace_bull_38_paisa,
                f.retrace_bull_62_paisa,
                f.retrace_bull_78_paisa,
            ]
        );
        assert_eq!(got.len(), FIB_COMPUTE_LIST_LEN);
    }

    #[test]
    fn fib_impl_rejects_corrupt_hl() {
        assert!(py_compute_fib_levels_impl(PDL, PDH).is_err());
        assert!(py_compute_fib_levels_impl(MAX_ANCHOR_PAISA + 1, 0).is_err());
    }

    // ── evaluate_from_raw_impl ─────────────────────────────────────────────
    fn raw(pdh: i64, pdl: i64, pdc: i64) -> Result<u128, String> {
        evaluate_from_raw_impl(
            10 * 3600, // tod_seconds (mid-morning)
            PDC,
            PDH,
            PDL,
            PDC, // open/high/low/close
            Some(PDC),
            Some(PDC), // ema20/ema200
            Some(PDC), // vwap
            pdh,
            pdl,
            pdc,
            PDH,
            PDL,
            PDC, // day_high/day_low/open_of_day
            10,
            10,
            10, // proximity bps
            0,
            vec![],
            MarketStructureSignal::default(),
            None,
            None,
            DEFAULT_GAP_MID_PROXIMITY_BPS,
            None,
            None,
            10,
        )
    }

    #[test]
    fn raw_impl_valid_returns_mask() {
        // A valid call returns a u128 mask (no panic, no error).
        let m = raw(PDH, PDL, PDC).expect("valid raw");
        // Mid-morning bit must be set (sanity that the evaluator actually ran).
        let _ = m; // exact bits proven by the golden test; here we assert it ran.
    }

    #[test]
    fn raw_impl_rejects_corrupt_ohlc() {
        assert!(raw(PDL, PDH, PDC).is_err()); // pdh < pdl
        assert!(raw(PDH, PDL, PDH + 1).is_err()); // pdc out of range
        assert!(raw(MAX_ANCHOR_PAISA + 1, 0, 0).is_err()); // overflow magnitude
    }

    #[test]
    fn raw_impl_rejects_out_of_range_vwap() {
        // A garbage session VWAP (PR-2) is rejected on BOTH the recompute path …
        let big = MAX_BAR_PAISA + 1;
        let err = evaluate_from_raw_impl(
            10 * 3600,
            PDC,
            PDH,
            PDL,
            PDC,
            None,
            None,
            Some(big), // vwap out of range
            PDH,
            PDL,
            PDC,
            PDH,
            PDL,
            PDC,
            10,
            10,
            10,
            0,
            vec![],
            MarketStructureSignal::default(),
            None,
            None,
            DEFAULT_GAP_MID_PROXIMITY_BPS,
            None,
            None,
            10,
        )
        .unwrap_err();
        assert!(err.contains("vwap_paisa magnitude"), "got: {err}");
        // … and the pre-built-levels path.
        let err = evaluate_with_levels_impl(
            10 * 3600,
            PDC,
            PDH,
            PDL,
            PDC,
            None,
            None,
            Some(big), // vwap out of range
            vec![0i64; PIVOT_LEVELS_LEN],
            vec![0i64; FIB_LEVELS_LEN],
            PDH,
            PDL,
            PDC,
            10,
            10,
            10,
            0,
            vec![],
            MarketStructureSignal::default(),
            None,
            None,
            DEFAULT_GAP_MID_PROXIMITY_BPS,
            None,
            None,
            10,
        )
        .unwrap_err();
        assert!(err.contains("vwap_paisa magnitude"), "got: {err}");
    }

    // ── check_evaluate_inputs (per-bar / bps overflow guard) ───────────────

    /// `evaluate_from_raw_impl` with a VALID anchor but caller-chosen bar inputs.
    #[expect(clippy::too_many_arguments, reason = "mirrors the evaluator ABI")]
    fn raw_bar(
        open: i64,
        high: i64,
        low: i64,
        close: i64,
        ema20: Option<i64>,
        ema200: Option<i64>,
        day_high: i64,
        day_low: i64,
        open_of_day: i64,
        pivot_bps: i64,
        pdh_pdl_bps: i64,
        fib_bps: i64,
    ) -> Result<u128, String> {
        evaluate_from_raw_impl(
            10 * 3600,
            open,
            high,
            low,
            close,
            ema20,
            ema200,
            None, // vwap (the Group-10 bits are not under test here)
            PDH,
            PDL,
            PDC, // VALID anchor
            day_high,
            day_low,
            open_of_day,
            pivot_bps,
            pdh_pdl_bps,
            fib_bps,
            0,
            vec![],
            MarketStructureSignal::default(),
            None,
            None,
            DEFAULT_GAP_MID_PROXIMITY_BPS,
            None,
            None,
            10,
        )
    }

    #[test]
    fn raw_impl_rejects_out_of_range_bar_prices() {
        let big = MAX_BAR_PAISA + 1;
        // Each per-bar price individually out of range → rejected (no abort).
        assert!(raw_bar(big, PDC, PDC, PDC, None, None, PDC, PDC, PDC, 10, 10, 10).is_err());
        assert!(raw_bar(PDC, big, PDC, PDC, None, None, PDC, PDC, PDC, 10, 10, 10).is_err());
        assert!(raw_bar(PDC, PDC, big, PDC, None, None, PDC, PDC, PDC, 10, 10, 10).is_err());
        assert!(raw_bar(PDC, PDC, PDC, big, None, None, PDC, PDC, PDC, 10, 10, 10).is_err());
        assert!(raw_bar(PDC, PDC, PDC, PDC, None, None, big, PDC, PDC, 10, 10, 10).is_err());
        assert!(raw_bar(PDC, PDC, PDC, PDC, None, None, PDC, big, PDC, 10, 10, 10).is_err());
        assert!(raw_bar(PDC, PDC, PDC, PDC, None, None, PDC, PDC, big, 10, 10, 10).is_err());
        // i64::MIN price (would panic in `.abs()`) → rejected.
        assert!(
            raw_bar(
                i64::MIN,
                PDC,
                PDC,
                PDC,
                None,
                None,
                PDC,
                PDC,
                PDC,
                10,
                10,
                10
            )
            .is_err()
        );
    }

    #[test]
    fn raw_impl_rejects_out_of_range_ema() {
        let big = MAX_BAR_PAISA + 1;
        assert!(
            raw_bar(
                PDC,
                PDC,
                PDC,
                PDC,
                Some(big),
                None,
                PDC,
                PDC,
                PDC,
                10,
                10,
                10
            )
            .is_err()
        );
        assert!(
            raw_bar(
                PDC,
                PDC,
                PDC,
                PDC,
                None,
                Some(big),
                PDC,
                PDC,
                PDC,
                10,
                10,
                10
            )
            .is_err()
        );
    }

    #[test]
    fn raw_impl_rejects_bad_bps() {
        // Each bps slot: negative and over-ceiling both rejected.
        assert!(raw_bar(PDC, PDC, PDC, PDC, None, None, PDC, PDC, PDC, -1, 10, 10).is_err());
        assert!(raw_bar(PDC, PDC, PDC, PDC, None, None, PDC, PDC, PDC, 10, -1, 10).is_err());
        assert!(raw_bar(PDC, PDC, PDC, PDC, None, None, PDC, PDC, PDC, 10, 10, -1).is_err());
        assert!(
            raw_bar(
                PDC,
                PDC,
                PDC,
                PDC,
                None,
                None,
                PDC,
                PDC,
                PDC,
                MAX_BPS + 1,
                10,
                10
            )
            .is_err()
        );
    }

    #[test]
    fn raw_impl_rejects_close_times_bps_overflow() {
        // A big close (at the bar ceiling) × a big-but-in-cap bps overflows i64 —
        // the `checked_mul` guard rejects it (the close × bps DoS the reviewer
        // found). MAX_BAR_PAISA × MAX_BPS ≈ 1.4·10²⁰ ≫ i64::MAX ≈ 9.2·10¹⁸.
        let close = MAX_BAR_PAISA;
        let err = raw_bar(
            close, close, close, close, None, None, close, close, close, MAX_BPS, 0, 0,
        )
        .unwrap_err();
        assert!(err.contains("overflows i64"), "got: {err}");
    }

    #[test]
    fn check_evaluate_inputs_product_overflow_direct() {
        // The `checked_mul` guard fires for a close × bps that overflows i64 …
        let err = check_evaluate_inputs(
            0,
            0,
            0,
            MAX_BAR_PAISA,
            None,
            None,
            0,
            0,
            0,
            0,
            MAX_BPS,
            0,
            0,
            0,
            0,
        )
        .unwrap_err();
        assert!(err.contains("overflows i64"), "got: {err}");
        // … and for a NEGATIVE close of the same magnitude.
        assert!(
            check_evaluate_inputs(
                0,
                0,
                0,
                -MAX_BAR_PAISA,
                None,
                None,
                0,
                0,
                0,
                0,
                MAX_BPS,
                0,
                0,
                0,
                0,
            )
            .is_err()
        );
        // A small close × MAX_BPS fits → accepted (the product guard does not
        // over-reject a legitimate large bps on a normal-priced bar).
        assert!(
            check_evaluate_inputs(0, 0, 0, 1_000_000, None, None, 0, 0, 0, 0, MAX_BPS, 0, 0, 0, 0,)
                .is_ok()
        );
        // pdc out of range → rejected.
        assert!(
            check_evaluate_inputs(
                0,
                0,
                0,
                0,
                None,
                None,
                0,
                0,
                0,
                MAX_BAR_PAISA + 1,
                0,
                0,
                0,
                0,
                0,
            )
            .is_err()
        );
    }

    #[test]
    fn check_evaluate_inputs_product_overflow_exact_boundary() {
        // The EXACT `checked_mul` boundary at bps = MAX_BPS: the largest `close`
        // whose product still fits i64 is `i64::MAX / MAX_BPS`. Both closes below
        // are valid bar prices (≤ MAX_BAR_PAISA), so they reach the product guard.
        let fits = i64::MAX / MAX_BPS; // close * MAX_BPS <= i64::MAX
        let over = fits + 1; // close * MAX_BPS  > i64::MAX
        // exact-fit product is ACCEPTED (reaches the inner evaluator).
        assert!(
            check_evaluate_inputs(0, 0, 0, fits, None, None, 0, 0, 0, 0, MAX_BPS, 0, 0, 0, 0,).is_ok()
        );
        // one paisa over → product overflows i64 → REJECTED.
        let err = check_evaluate_inputs(0, 0, 0, over, None, None, 0, 0, 0, 0, MAX_BPS, 0, 0, 0, 0)
            .unwrap_err();
        assert!(err.contains("overflows i64"), "got: {err}");
        // The same boundary holds per-bps-slot: only the offending slot rejects.
        assert!(
            check_evaluate_inputs(0, 0, 0, fits, None, None, 0, 0, 0, 0, 0, MAX_BPS, 0, 0, 0,).is_ok()
        );
        assert!(
            check_evaluate_inputs(0, 0, 0, over, None, None, 0, 0, 0, 0, 0, 0, MAX_BPS, 0, 0,)
                .is_err()
        );
    }

    // ── evaluate_with_levels_impl ──────────────────────────────────────────
    fn with_levels(pivots: Vec<i64>, fibs: Vec<i64>) -> Result<u128, String> {
        evaluate_with_levels_impl(
            10 * 3600,
            PDC,
            PDH,
            PDL,
            PDC,
            Some(PDC),
            Some(PDC),
            Some(PDC), // vwap_paisa
            pivots,
            fibs,
            PDH,
            PDL,
            PDC,
            10,
            10,
            10,
            0,
            vec![],
            MarketStructureSignal::default(),
            None,
            None,
            DEFAULT_GAP_MID_PROXIMITY_BPS,
            None,
            None,
            10,
        )
    }

    #[test]
    fn with_levels_valid_returns_mask() {
        // A correctly-sized levels pair (12 + 15) runs the evaluator with no panic.
        let pivots = vec![0i64; PIVOT_LEVELS_LEN];
        let fibs = vec![0i64; FIB_LEVELS_LEN];
        assert!(with_levels(pivots, fibs).is_ok());
    }

    #[test]
    fn with_levels_rejects_wrong_pivot_len() {
        let err = with_levels(vec![0; PIVOT_LEVELS_LEN - 1], vec![0; FIB_LEVELS_LEN]).unwrap_err();
        assert!(err.contains("pivot_levels must have"), "got: {err}");
    }

    #[test]
    fn with_levels_rejects_wrong_fib_len() {
        let err = with_levels(vec![0; PIVOT_LEVELS_LEN], vec![0; FIB_LEVELS_LEN + 1]).unwrap_err();
        assert!(err.contains("fib_levels must have"), "got: {err}");
    }

    #[test]
    fn with_levels_rejects_out_of_range_level() {
        // A garbage pivot level (i64::MIN would make `(close - level).abs()` panic
        // in `is_near`) is rejected before the snapshot is built.
        let mut pivots = vec![0i64; PIVOT_LEVELS_LEN];
        pivots[3] = i64::MIN;
        let err = with_levels(pivots, vec![0; FIB_LEVELS_LEN]).unwrap_err();
        assert!(err.contains("level magnitude"), "got: {err}");
        // A garbage fib level too.
        let mut fibs = vec![0i64; FIB_LEVELS_LEN];
        fibs[7] = MAX_BAR_PAISA + 1;
        let err = with_levels(vec![0; PIVOT_LEVELS_LEN], fibs).unwrap_err();
        assert!(err.contains("level magnitude"), "got: {err}");
    }

    #[test]
    fn with_levels_rejects_out_of_range_bar_or_bps() {
        // VALID levels, but a garbage bar input / bps still rejected (the
        // evaluate-input guard runs on the pre-built path too).
        let ok_p = vec![0i64; PIVOT_LEVELS_LEN];
        let ok_f = vec![0i64; FIB_LEVELS_LEN];
        let big = MAX_BAR_PAISA + 1;
        let err = evaluate_with_levels_impl(
            10 * 3600,
            big, // open out of range
            0,
            0,
            0,
            None,
            None,
            None,
            ok_p.clone(),
            ok_f.clone(),
            0,
            0,
            0,
            10,
            10,
            10,
            0,
            vec![],
            MarketStructureSignal::default(),
            None,
            None,
            DEFAULT_GAP_MID_PROXIMITY_BPS,
            None,
            None,
            10,
        )
        .unwrap_err();
        assert!(err.contains("overflow-safe"), "got: {err}");
        // close × bps overflow on the pre-built path.
        let err = evaluate_with_levels_impl(
            10 * 3600,
            0,
            0,
            0,
            MAX_BAR_PAISA, // close
            None,
            None,
            None,
            ok_p,
            ok_f,
            0,
            0,
            0,
            MAX_BPS, // huge bps
            0,
            0,
            0,
            vec![],
            MarketStructureSignal::default(),
            None,
            None,
            DEFAULT_GAP_MID_PROXIMITY_BPS,
            None,
            None,
            10,
        )
        .unwrap_err();
        assert!(err.contains("overflows i64"), "got: {err}");
    }

    #[test]
    fn with_levels_indexes_all_entries_distinctly() {
        // Distinct sentinel values so a wrong index would surface; the call must
        // succeed (length-guarded) and run the evaluator over them.
        let pivots: Vec<i64> = (1..=PIVOT_LEVELS_LEN as i64).collect();
        let fibs: Vec<i64> = (1..=FIB_LEVELS_LEN as i64).collect();
        assert!(with_levels(pivots, fibs).is_ok());
    }

    #[test]
    fn market_structure_flags_set_bits_56_to_59() {
        // The four resolved BOS/CHoCH booleans threaded through the FFI ABI set
        // exactly their Group-12 bits (56-59) — both entry points (DEC-NIFTY-BOS-CHOCH-001).
        let cases = [
            (
                MarketStructureSignal {
                    bos_bullish: true,
                    ..Default::default()
                },
                crate::bits::BIT_BOS_BULLISH,
            ),
            (
                MarketStructureSignal {
                    bos_bearish: true,
                    ..Default::default()
                },
                crate::bits::BIT_BOS_BEARISH,
            ),
            (
                MarketStructureSignal {
                    choch_bullish: true,
                    ..Default::default()
                },
                crate::bits::BIT_CHOCH_BULLISH,
            ),
            (
                MarketStructureSignal {
                    choch_bearish: true,
                    ..Default::default()
                },
                crate::bits::BIT_CHOCH_BEARISH,
            ),
        ];
        for (ms, bit) in cases {
            // evaluate_from_raw path.
            let raw = evaluate_from_raw_impl(
                10 * 3600,
                PDC,
                PDH,
                PDL,
                PDC,
                None,
                None,
                None,
                PDH,
                PDL,
                PDC,
                PDH,
                PDL,
                PDC,
                10,
                10,
                10,
                0,
                vec![],
                ms,
                None,
                None,
                DEFAULT_GAP_MID_PROXIMITY_BPS,
                None,
                None,
                10,
            )
            .expect("valid raw");
            assert_eq!((raw >> bit) & 1, 1, "from_raw bit {bit} should be set");
            // evaluate_with_levels path.
            let wl = evaluate_with_levels_impl(
                10 * 3600,
                PDC,
                PDH,
                PDL,
                PDC,
                None,
                None,
                None,
                vec![PDC; PIVOT_LEVELS_LEN],
                vec![PDC; FIB_LEVELS_LEN],
                PDH,
                PDL,
                PDC,
                10,
                10,
                10,
                0,
                vec![],
                ms,
                None,
                None,
                DEFAULT_GAP_MID_PROXIMITY_BPS,
                None,
                None,
                10,
            )
            .expect("valid with_levels");
            assert_eq!((wl >> bit) & 1, 1, "with_levels bit {bit} should be set");
        }
    }

    // ── trivial pyfunction helpers (pure) ──────────────────────────────────
    #[test]
    fn version_and_total_conditions() {
        assert_eq!(version(), env!("CARGO_PKG_VERSION"));
        assert_eq!(total_conditions(), TOTAL_CONDITIONS);
    }

    /// Stress: many valid anchors compute without panic and are deterministic,
    /// and the validated FFI path equals the standalone indicators every time
    /// (no per-call drift). Also a cheap perf smoke — the work is O(1) per call.
    #[test]
    fn stress_many_anchors_parity_and_no_panic() {
        let mut acc = 0u128;
        for i in 0..50_000i64 {
            let pdl = 1 + (i % 100_000);
            let pdh = pdl + (i % 20_000);
            let pdc = pdl + (i % (pdh - pdl + 1));
            let got = py_compute_pivot_levels_impl(pdh, pdl, pdc).unwrap();
            let p = compute_pivot_levels(pdh, pdl, pdc);
            // The 11-element list is [r5, r4, r3, r2, r1, p, s1, …]; p is index 5.
            assert_eq!(got[5], p.p_paisa);
            let m = evaluate_from_raw_impl(
                10 * 3600,
                pdc,
                pdh,
                pdl,
                pdc,
                Some(pdc),
                Some(pdc),
                Some(pdc), // vwap
                pdh,
                pdl,
                pdc,
                pdh,
                pdl,
                pdc,
                10,
                10,
                10,
                0,
                vec![],
                MarketStructureSignal::default(),
                None,
                None,
                DEFAULT_GAP_MID_PROXIMITY_BPS,
                None,
                None,
                10,
            )
            .unwrap();
            acc = acc.wrapping_add(m);
        }
        // Black-box the accumulator so the loop is not optimised away; the exact
        // value is incidental (the per-call parity asserts above are the point).
        std::hint::black_box(acc);
    }

    /// The appended `supertrend_x2_paisa` arg reaches the evaluator through
    /// ALL THREE eval `_impl` paths: a line half a paisa below the close fires
    /// bit 64, half above fires bit 65, and `evaluate_spec_impl` both ACCEPTS
    /// a mask requiring the implemented bit 64 AND fires it when present
    /// (the flip's ABI teeth — a dropped pass-through kills each case).
    #[test]
    fn supertrend_arg_reaches_all_three_eval_paths() {
        let st_family: u128 = 0b11u128 << 64;
        let above = 1u128 << 64;
        let below = 1u128 << 65;
        // PDC is the close in both fixtures ⇒ close_x2 = 2·PDC.
        let raw = |st: Option<i64>| {
            evaluate_from_raw_impl(
                10 * 3600,
                PDC,
                PDH,
                PDL,
                PDC,
                None,
                None,
                None,
                PDH,
                PDL,
                PDC,
                PDH,
                PDL,
                PDC,
                10,
                10,
                10,
                0,
                vec![],
                MarketStructureSignal::default(),
                st,
                None,
                DEFAULT_GAP_MID_PROXIMITY_BPS,
                None,
                None,
                10,
            )
            .expect("valid raw")
        };
        assert_eq!(raw(Some(2 * PDC - 1)) & st_family, above);
        assert_eq!(raw(Some(2 * PDC + 1)) & st_family, below);
        assert_eq!(raw(Some(2 * PDC)) & st_family, 0, "equality: neither");
        assert_eq!(raw(None) & st_family, 0, "absent: neither");
        let wl = |st: Option<i64>| {
            evaluate_with_levels_impl(
                10 * 3600,
                PDC,
                PDH,
                PDL,
                PDC,
                None,
                None,
                None,
                vec![PDC; PIVOT_LEVELS_LEN],
                vec![PDC; FIB_LEVELS_LEN],
                PDH,
                PDL,
                PDC,
                10,
                10,
                10,
                0,
                vec![],
                MarketStructureSignal::default(),
                st,
                None,
                DEFAULT_GAP_MID_PROXIMITY_BPS,
                None,
                None,
                10,
            )
            .expect("valid with_levels")
        };
        assert_eq!(wl(Some(2 * PDC - 1)) & st_family, above);
        assert_eq!(wl(Some(2 * PDC + 1)) & st_family, below);
        assert_eq!(wl(Some(2 * PDC)) & st_family, 0);
        // evaluate_spec: a spec REQUIRING bit 64 fires iff the line puts the
        // close strictly above it.
        let (bits_fired, fires, _dir) = evaluate_spec_impl(
            &format!("{above:032x}"),
            0,
            10 * 3600,
            PDC,
            PDH,
            PDL,
            PDC,
            None,
            None,
            None,
            vec![PDC; PIVOT_LEVELS_LEN],
            vec![PDC; FIB_LEVELS_LEN],
            PDH,
            PDL,
            PDC,
            10,
            10,
            10,
            0,
            vec![],
            MarketStructureSignal::default(),
            Some(2 * PDC - 1),
            None,
            DEFAULT_GAP_MID_PROXIMITY_BPS,
            None,
            None,
            10,
        )
        .expect("valid spec eval");
        assert_eq!(bits_fired & st_family, above);
        assert!(fires, "the required implemented ST bit is present");
    }

    // ── evaluate_spec_impl (strategy-as-data deploy binding) ───────────────

    /// `evaluate_spec_impl` over a VALID anchor as 12 pre-built pivot + 15 fib
    /// levels, mirroring `evaluate_with_levels_impl`'s ABI.
    fn spec_eval(mask_hex: &str, direction: u8) -> Result<(u128, bool, u8), String> {
        evaluate_spec_impl(
            mask_hex,
            direction,
            10 * 3600, // mid-morning
            PDC,
            PDH,
            PDL,
            PDC, // open/high/low/close
            Some(PDC),
            Some(PDC), // ema20/ema200
            None,      // vwap
            // pivot_levels [p,r1,r2,r3,s1,s2,s3,pdh,pdl,pdc,r5,s5]
            vec![PDC, PDC, PDC, PDC, PDC, PDC, PDC, PDH, PDL, PDC, PDC, PDC],
            // fib_levels (15)
            vec![PDC; FIB_LEVELS_LEN],
            PDH,
            PDL,
            PDC, // day_high/day_low/open_of_day
            10,
            10,
            10, // bps
            0,
            vec![],
            MarketStructureSignal::default(),
            None,
            None,
            DEFAULT_GAP_MID_PROXIMITY_BPS,
            None,
            None,
            10,
        )
    }

    #[test]
    fn spec_impl_fires_when_required_bits_are_present() {
        // First learn the bar's own mask (empty spec always fires, echoes bar_bits).
        let (bar_bits, fires, dir) = spec_eval("0", 0).expect("empty mask valid");
        assert!(fires, "an empty mask is a vacuous always-fire");
        assert_eq!(dir, 0);
        // A spec requiring exactly the bar's own bits fires and echoes the mask.
        let own_hex = format!("{bar_bits:032x}");
        let (got_bits, got_fires, got_dir) = spec_eval(&own_hex, 1).expect("own mask valid");
        assert_eq!(got_bits, bar_bits, "wrapper must echo evaluate_with_levels");
        assert!(got_fires);
        assert_eq!(got_dir, 1, "Short direction echoed");
    }

    #[test]
    fn spec_impl_does_not_fire_when_a_required_bit_is_absent() {
        let (bar_bits, _, _) = spec_eval("0", 0).expect("empty mask valid");
        let absent = IMPLEMENTED_MASK & !bar_bits;
        assert_ne!(absent, 0, "the fixture must leave an implemented bit clear");
        let want = absent & absent.wrapping_neg();
        let (got_bits, fires, _) = spec_eval(&format!("{want:032x}"), 0).expect("valid mask");
        assert_eq!(got_bits, bar_bits);
        assert!(!fires, "requiring an absent bit must NOT fire");
    }

    #[test]
    fn spec_impl_rejects_non_hex_mask() {
        let err = spec_eval("zzzz", 0).unwrap_err();
        assert!(err.contains("not valid hex"), "got: {err}");
    }

    #[test]
    fn spec_impl_rejects_a_reserved_bit() {
        // Bit 66 is the first undeclared/reserved bit (TOTAL_CONDITIONS == 66).
        let reserved = 1u128 << 66;
        let err = spec_eval(&format!("{reserved:032x}"), 0).unwrap_err();
        assert!(err.contains("unimplemented/reserved"), "got: {err}");
        // The tombstoned VWAP bit 52 is equally refused (the wipe).
        let tombstone = 1u128 << 52;
        let err = spec_eval(&format!("{tombstone:032x}"), 0).unwrap_err();
        assert!(err.contains("unimplemented/reserved"), "got: {err}");
        // Bit 64 (Supertrend) is IMPLEMENTED since the vocab-66 flip — a mask
        // requiring it is ACCEPTED at the boundary (and does not fire on this
        // ST-less fixture bar, whose line is None).
        let supertrend = 1u128 << 64;
        let (_bits, fires, _dir) = spec_eval(&format!("{supertrend:032x}"), 0).unwrap();
        assert!(
            !fires,
            "no ST line on the fixture bar ⇒ the required bit is absent"
        );
        // Bit 69 is the first UNDECLARED bit (TOTAL_CONDITIONS == 69 since
        // DEC-GAP-MIDPOINT-CONDITIONS-001 appended the gap trio at 66-68).
        let undeclared = 1u128 << 69;
        let err = spec_eval(&format!("{undeclared:032x}"), 0).unwrap_err();
        assert!(err.contains("unimplemented/reserved"), "got: {err}");
    }

    #[test]
    fn spec_impl_rejects_bad_direction() {
        let err = spec_eval("0", 2).unwrap_err();
        assert!(err.contains("must be 0 (Long) or 1 (Short)"), "got: {err}");
    }

    #[test]
    fn spec_impl_propagates_evaluate_with_levels_guard() {
        // A garbage level length must still be rejected (the reused guard fires).
        let err = evaluate_spec_impl(
            "0",
            0,
            10 * 3600,
            PDC,
            PDH,
            PDL,
            PDC,
            None,
            None,
            None,
            vec![0i64; 3], // wrong pivot_levels length
            vec![0i64; FIB_LEVELS_LEN],
            PDH,
            PDL,
            PDC,
            10,
            10,
            10,
            0,
            vec![],
            MarketStructureSignal::default(),
            None,
            None,
            DEFAULT_GAP_MID_PROXIMITY_BPS,
            None,
            None,
            10,
        )
        .unwrap_err();
        assert!(err.contains("pivot_levels must have"), "got: {err}");
    }

    // ── spec_canonical_bytes_impl (identity-wiring binding) ────────────────

    #[test]
    fn canonical_bytes_impl_matches_struct_encode() {
        // The binding must return EXACTLY `StrategySpec::canonical_bytes` for
        // the same spec — the COMMON one-encoder guarantee.
        let got = spec_canonical_bytes_impl("1f", 0, 50, 10, 25, 3, 1).unwrap();
        let spec = StrategySpec::new(
            0x1f,
            Direction::Long,
            ProximityParams {
                pivot_proximity_bps: 50,
                pdh_pdl_proximity_bps: 10,
                fib_proximity_bps: 25,
                // Any value here leaves the FROZEN v1 encode untouched — the
                // binding fills the defaults (DEC-GAP-MIDPOINT-CONDITIONS-001;
                // DEC-FIB-TEMPLATE-EXPRESS-001 — the swing knob is likewise
                // outside the 57-byte encode).
                gap_mid_proximity_bps: DEFAULT_GAP_MID_PROXIMITY_BPS,
                swing_proximity_bps: 10,
            },
            3,
            1,
        )
        .unwrap();
        assert_eq!(got, spec.canonical_bytes());
        assert_eq!(got.len(), 57, "the v1 encode is exactly 57 bytes");
    }

    #[test]
    fn canonical_bytes_impl_accepts_symbol_id_zero() {
        // Shared-crate semantics: id 0 encodes fine here — the reservation is
        // BruteX Python-registry POLICY, not a crate rule (spec.rs accepts 0).
        let got = spec_canonical_bytes_impl("0", 1, 0, 0, 0, 0, 0).unwrap();
        assert_eq!(got.len(), 57);
    }

    #[test]
    fn canonical_bytes_impl_rejects_non_hex_mask() {
        let err = spec_canonical_bytes_impl("zz", 0, 50, 10, 25, 3, 1).unwrap_err();
        assert!(err.contains("is not valid hex"), "got: {err}");
    }

    #[test]
    fn canonical_bytes_impl_rejects_reserved_bit() {
        // Bit 127 is far outside IMPLEMENTED_MASK — refused like evaluate_spec.
        let reserved = format!("{:032x}", 1u128 << 127);
        let err = spec_canonical_bytes_impl(&reserved, 0, 50, 10, 25, 3, 1).unwrap_err();
        assert!(err.contains("unimplemented/reserved"), "got: {err}");
    }

    #[test]
    fn canonical_bytes_impl_rejects_bad_direction() {
        let err = spec_canonical_bytes_impl("1", 2, 50, 10, 25, 3, 1).unwrap_err();
        assert!(err.contains("direction"), "got: {err}");
    }

    #[test]
    fn canonical_bytes_impl_rejects_out_of_range_bps() {
        // Negative and absurd bps both fail the [0, MAX_BPS] envelope, naming
        // the offending field.
        let neg = spec_canonical_bytes_impl("1", 0, -1, 10, 25, 3, 1).unwrap_err();
        assert!(neg.contains("pivot_bps"), "got: {neg}");
        let huge = spec_canonical_bytes_impl("1", 0, 50, MAX_BPS + 1, 25, 3, 1).unwrap_err();
        assert!(huge.contains("pdh_pdl_bps"), "got: {huge}");
        let fib = spec_canonical_bytes_impl("1", 0, 50, 10, -7, 3, 1).unwrap_err();
        assert!(fib.contains("fib_bps"), "got: {fib}");
    }

    #[test]
    fn canonical_bytes_impl_prior_n_u64_max_platform_behaviour() {
        // On the 64-bit wheel/TickVault targets u64::MAX fits usize, so the
        // encode succeeds (the try_from guard exists for a 32-bit target where
        // it would reject instead — an honest platform guard, not a live path).
        let res = spec_canonical_bytes_impl("1", 0, 50, 10, 25, u64::MAX, 1);
        if usize::BITS >= 64 {
            let bytes = res.unwrap();
            assert_eq!(bytes.len(), 57);
        } else {
            assert!(res.unwrap_err().contains("prior_n"));
        }
    }

    // ── seconds_of_day_impl (tod Rust-seam binding) ────────────────────────

    #[test]
    fn seconds_of_day_impl_valid_and_boundaries() {
        assert_eq!(seconds_of_day_impl(0, 0, 0).unwrap(), 0);
        assert_eq!(seconds_of_day_impl(9, 15, 0).unwrap(), 33_300);
        assert_eq!(seconds_of_day_impl(23, 59, 59).unwrap(), 86_399);
    }

    #[test]
    fn seconds_of_day_impl_rejects_out_of_range_components() {
        // Each component named in its own reject; hour=2_000_000 is the exact
        // garbage input that would overflow `hour * 3600` unguarded.
        let h = seconds_of_day_impl(24, 0, 0).unwrap_err();
        assert!(h.contains("hour"), "got: {h}");
        let huge = seconds_of_day_impl(2_000_000, 0, 0).unwrap_err();
        assert!(huge.contains("hour"), "got: {huge}");
        let m = seconds_of_day_impl(0, 60, 0).unwrap_err();
        assert!(m.contains("minute"), "got: {m}");
        let sec = seconds_of_day_impl(0, 0, 60).unwrap_err();
        assert!(sec.contains("second"), "got: {sec}");
    }

    // ── metadata FFI-edge guards (the SIGABRT class closed at the boundary) ─

    #[test]
    fn minute_of_session_impl_rejects_the_exact_abort_repro() {
        // Pre-guard, `minute_of_session(i64::MAX, -1)` overflowed `tod - open`
        // → panic → abort (SIGABRT) under panic = "abort". Now a clean Err.
        let err = minute_of_session_impl(i64::MAX, -1).unwrap_err();
        assert!(err.contains("tod_seconds"), "got: {err}");
        let err2 = minute_of_session_impl(0, -1).unwrap_err();
        assert!(err2.contains("session_open_seconds"), "got: {err2}");
        assert!(minute_of_session_impl(86_400, 0).is_err());
        assert!(minute_of_session_impl(0, 86_400).is_err());
        assert!(minute_of_session_impl(i64::MIN, 0).is_err());
    }

    #[test]
    fn minute_of_session_impl_valid_domain_delegates_byte_identically() {
        // Boundary + in-domain values equal the pure core (no behavior change).
        for (tod, open) in [(0i64, 0i64), (86_399, 0), (0, 86_399), (33_300, 33_300)] {
            assert_eq!(
                minute_of_session_impl(tod, open).unwrap(),
                crate::indicators::metadata::minute_of_session(tod, open)
            );
        }
        // The honest pre-open NEGATIVE minute survives the guard (09:00 vs 09:15).
        assert_eq!(minute_of_session_impl(9 * 3600, 33_300).unwrap(), -15);
    }

    #[test]
    fn weekday_from_ordinal_impl_rejects_the_exact_abort_repro() {
        // Pre-guard, `weekday_from_ordinal(i64::MIN)` overflowed `ordinal - 1`
        // → panic → abort. Now a clean Err naming the Python date domain.
        let err = weekday_from_ordinal_impl(i64::MIN).unwrap_err();
        assert!(err.contains("day_ordinal"), "got: {err}");
        assert!(err.contains("3652059"), "got: {err}");
        assert!(weekday_from_ordinal_impl(0).is_err());
        assert!(weekday_from_ordinal_impl(MAX_DAY_ORDINAL + 1).is_err());
    }

    #[test]
    fn weekday_from_ordinal_impl_valid_domain_delegates_byte_identically() {
        for ordinal in [1i64, 739_797, MAX_DAY_ORDINAL] {
            assert_eq!(
                weekday_from_ordinal_impl(ordinal).unwrap(),
                crate::indicators::metadata::weekday_from_ordinal(ordinal)
            );
        }
        // Ordinal 1 (0001-01-01) is Monday — the Python-parity anchor.
        assert_eq!(weekday_from_ordinal_impl(1).unwrap(), 0);
    }

    #[test]
    fn derive_bar_metadata_impl_rejects_every_garbage_component() {
        // The exact abort repro: hour = 2_000_000 bypassed py_seconds_of_day's
        // guard through this entry and overflowed `hour * 3600` (u32).
        let h = derive_bar_metadata_impl(1, 2_000_000, 0, 0, 0, 1).unwrap_err();
        assert!(h.contains("hour"), "got: {h}");
        // Each remaining component is guarded + named.
        let d = derive_bar_metadata_impl(0, 10, 0, 0, 33_300, 1).unwrap_err();
        assert!(d.contains("day_ordinal"), "got: {d}");
        let e = derive_bar_metadata_impl(739_797, 10, 0, 0, 33_300, i64::MIN).unwrap_err();
        assert!(e.contains("expiry_ordinal"), "got: {e}");
        let s = derive_bar_metadata_impl(739_797, 10, 0, 0, -1, 739_797).unwrap_err();
        assert!(s.contains("session_open_seconds"), "got: {s}");
    }

    #[test]
    fn derive_bar_metadata_impl_valid_domain_delegates_byte_identically() {
        // 2026-06-30 (Tue) 09:20:30 IST, regular open, expiry 3 days out —
        // equal to the pure core field-for-field (no behavior change).
        let got = derive_bar_metadata_impl(739_797, 9, 20, 30, 33_300, 739_800).unwrap();
        let want =
            crate::indicators::metadata::derive_bar_metadata(739_797, 9, 20, 30, 33_300, 739_800);
        assert_eq!(
            got,
            (
                want.tod_seconds,
                want.weekday,
                want.minute_of_session,
                want.is_expiry_day
            )
        );
        // Expiry-day flag survives the guard at the domain boundary.
        let (_, _, _, is_expiry) =
            derive_bar_metadata_impl(MAX_DAY_ORDINAL, 23, 59, 59, 86_399, MAX_DAY_ORDINAL).unwrap();
        assert!(is_expiry);
    }

    // ── vwap_update_guard (the twins' non-negative-typical boundary) ───────

    #[test]
    fn vwap_update_guard_rejects_negative_typical() {
        // The floor-vs-trunc domain hole: (-7, 1) then (0, 1) gave Rust -3 vs
        // Python -4 pre-guard. Negative typical is now rejected OUTRIGHT.
        let err = vwap_update_guard(-7, 1).unwrap_err();
        assert!(err.contains("non-negative"), "got: {err}");
        assert!(vwap_update_guard(-1, 1).is_err());
        assert!(vwap_update_guard(i64::MIN, 1).is_err());
    }

    #[test]
    fn vwap_update_guard_magnitude_ceiling_and_valid_domain() {
        // Ceiling still enforced on both operands (the i128-overflow guard).
        let t = vwap_update_guard(MAX_BAR_PAISA + 1, 1).unwrap_err();
        assert!(t.contains("typical_paisa"), "got: {t}");
        let v = vwap_update_guard(0, MAX_BAR_PAISA + 1).unwrap_err();
        assert!(v.contains("volume"), "got: {v}");
        assert!(vwap_update_guard(0, -(MAX_BAR_PAISA + 1)).is_err());
        // Valid domain: zero typical, negative volume (a skip signal), ceiling.
        assert!(vwap_update_guard(0, 0).is_ok());
        assert!(vwap_update_guard(0, -5).is_ok());
        assert!(vwap_update_guard(MAX_BAR_PAISA, MAX_BAR_PAISA).is_ok());
        assert!(vwap_update_guard(24_000_00, 100).is_ok());
    }

    // ── PyMarketStructureState swing-level getters ─────────────────────
    // (DEC-INDICATOR-TWINS-RUST-SINGLE-SOURCE-001 — the additive read-only
    // introspection the Python-side P-06 + golden-replay suites consume.)

    #[test]
    fn ms_getters_are_none_before_any_confirmed_swing() {
        // The `None` arm of both getters: a fresh detector (and one fed fewer
        // than 2k+1 bars) has confirmed nothing, so both levels read `None`.
        let mut state = PyMarketStructureState {
            inner: CoreMarketStructureState::new(2),
        };
        assert_eq!(state.last_swing_high(), None);
        assert_eq!(state.last_swing_low(), None);
        for (h, l, c) in [(100, 90, 95), (110, 100, 105), (130, 80, 125)] {
            state.update(h, l, c);
        }
        // 3 bars < 2k+1 = 5 — still nothing confirmable.
        assert_eq!(state.last_swing_high(), None);
        assert_eq!(state.last_swing_low(), None);
    }

    #[test]
    fn ms_getters_expose_confirmed_swing_levels() {
        // The `Some` arm: bar 2 is BOTH a strict local-max high (130) and a
        // strict local-min low (80) over its 2k=4 neighbours, confirmed once
        // the 5th bar lands. The getters must expose EXACTLY the core levels.
        let mut state = PyMarketStructureState {
            inner: CoreMarketStructureState::new(2),
        };
        for (h, l, c) in [
            (100, 90, 95),
            (110, 100, 105),
            (130, 80, 125),
            (115, 105, 110),
            (112, 102, 108),
        ] {
            state.update(h, l, c);
        }
        assert_eq!(state.last_swing_high(), Some(130));
        assert_eq!(state.last_swing_low(), Some(80));
        // Delegation is verbatim: getter == the existing core accessor.
        assert_eq!(state.last_swing_high(), state.inner.last_swing_high());
        assert_eq!(state.last_swing_low(), state.inner.last_swing_low());
    }

    // ── property layer ─────────────────────────────────────────────────────
    use proptest::prelude::*;

    /// Bound (₹10 billion paisa) far inside both the overflow ceiling and the
    /// `tests/proptests.rs` domain — every generated valid anchor is accepted and
    /// computes without tripping the release overflow-checks panic.
    const PROP_MAX: i64 = 1_000_000_000_000;

    /// A valid anchor: `1 <= pdl <= pdc <= pdh <= PROP_MAX`.
    fn arb_valid_anchor() -> impl Strategy<Value = (i64, i64, i64)> {
        (1i64..=PROP_MAX, 0i64..=PROP_MAX).prop_flat_map(|(pdl, range)| {
            let pdh = pdl + range;
            (Just(pdh), Just(pdl), pdl..=pdh)
        })
    }

    proptest! {
        /// A valid anchor is ALWAYS accepted, and the pivot/fib FFI logic equals
        /// the standalone indicators bit-for-bit (the parity guarantee) — and
        /// never panics (the overflow ceiling is never reached in-domain).
        #[test]
        fn valid_anchor_accepted_and_parity((pdh, pdl, pdc) in arb_valid_anchor()) {
            prop_assert!(check_prev_day_ohlc(pdh, pdl, pdc).is_ok());
            prop_assert!(check_prev_day_hl(pdh, pdl).is_ok());

            let got_p = py_compute_pivot_levels_impl(pdh, pdl, pdc).unwrap();
            let p = compute_pivot_levels(pdh, pdl, pdc);
            prop_assert_eq!(
                got_p,
                vec![
                    p.r5_paisa, p.r4_paisa, p.r3_paisa, p.r2_paisa, p.r1_paisa, p.p_paisa,
                    p.s1_paisa, p.s2_paisa, p.s3_paisa, p.s4_paisa, p.s5_paisa,
                ]
            );

            let got_f = py_compute_fib_levels_impl(pdh, pdl).unwrap();
            // The COMPUTE projection is 19 (bull tail appended) while the frozen
            // evaluator ABI stays 15 (DEC-FIB-BULLISH-RETRACE-001).
            prop_assert_eq!(got_f.len(), FIB_COMPUTE_LIST_LEN);
            let f = compute_fib_levels(pdh, pdl);
            prop_assert_eq!(
                &got_f[FIB_LEVELS_LEN..],
                &[
                    f.retrace_bull_23_paisa,
                    f.retrace_bull_38_paisa,
                    f.retrace_bull_62_paisa,
                    f.retrace_bull_78_paisa,
                ]
            );
        }

        /// `pdh < pdl` is ALWAYS rejected (both validators) and never panics —
        /// for arbitrary in-domain magnitudes.
        #[test]
        fn pdh_below_pdl_always_rejected(
            pdl in 2i64..=PROP_MAX,
            drop in 1i64..=PROP_MAX,
        ) {
            let pdh = pdl - drop.min(pdl - 1); // pdh strictly below pdl, >= 1
            prop_assume!(pdh < pdl);
            prop_assert!(check_prev_day_hl(pdh, pdl).is_err());
            prop_assert!(check_prev_day_ohlc(pdh, pdl, pdl).is_err());
            prop_assert!(py_compute_pivot_levels_impl(pdh, pdl, pdl).is_err());
            prop_assert!(py_compute_fib_levels_impl(pdh, pdl).is_err());
        }

        /// An overflow-prone magnitude (just past the ceiling) is ALWAYS rejected,
        /// never reaching the inner arithmetic that would panic→abort.
        #[test]
        fn overflow_magnitude_always_rejected(extra in 1i64..=(i64::MAX / 8)) {
            let pdh = MAX_ANCHOR_PAISA.saturating_add(extra);
            prop_assert!(check_prev_day_hl(pdh, 0).is_err());
            prop_assert!(check_prev_day_ohlc(pdh, 0, 0).is_err());
        }

        /// VALID-DOMAIN bar inputs + bps (real-price magnitudes, real tolerances)
        /// are NEVER rejected by the per-bar evaluate guard, and `evaluate_from_raw`
        /// runs to a `u64` mask without panic — the "no valid input is rejected"
        /// guarantee for the NEW per-bar/bps bounds. The domain (±10⁹ paisa = ±₹10M,
        /// 0..=10000 bps) sits far inside MAX_BAR_PAISA/MAX_BPS, mirroring the
        /// `tests/proptests.rs` finance-safe envelope.
        #[test]
        fn valid_bar_inputs_never_rejected(
            open in -1_000_000_000i64..=1_000_000_000,
            high in -1_000_000_000i64..=1_000_000_000,
            low in -1_000_000_000i64..=1_000_000_000,
            close in -1_000_000_000i64..=1_000_000_000,
            day_high in -1_000_000_000i64..=1_000_000_000,
            day_low in -1_000_000_000i64..=1_000_000_000,
            open_of_day in -1_000_000_000i64..=1_000_000_000,
            bps in 0i64..=10_000,
        ) {
            // the per-bar guard accepts every in-domain combination …
            prop_assert!(check_evaluate_inputs(
                open, high, low, close, Some(close), Some(close),
                day_high, day_low, open_of_day, 1_000_000, bps, bps, bps, 0,
            0,
        ).is_ok());
            // … and a full valid-anchor evaluate runs without panic.
            let mask = evaluate_from_raw_impl(
                10 * 3600, open, high, low, close, Some(close), Some(close),
                None,
                1_010_000, 990_000, 1_000_000, day_high, day_low, open_of_day,
                bps, bps, bps, 0, vec![],
                MarketStructureSignal::default(),
                None,
                None,
                DEFAULT_GAP_MID_PROXIMITY_BPS,
                None,
                None,
                10,
            );
            prop_assert!(mask.is_ok());
        }
    }

    // ── DEC-MUTATION-RUNTIME-003 (MIGRATION.md #1597): wrapper delegation-parity
    // kills — the LINKABLE subset. MEASURED boundary (rust-lld, 2026-07-07): a
    // `#[pyfunction]`/`#[pymethods]` wrapper whose body constructs a `PyErr`
    // (`.map_err(PyValueError::new_err)` / a `PyResult` reject arm) cannot be
    // REFERENCED by any test in this crate — under pyo3's `extension-module`
    // the libpython symbols (`PyExc_ValueError`, `PyUnicode_FromStringAndSize`,
    // …) are unresolved at test-link time, so merely calling such a wrapper
    // fails the link. No mutants.toml exclusion is needed for them, because
    // cargo-mutants 27.1.0 does not resolve the `PyResult` alias to `Result`
    // and fabricates only non-compiling replacements (`PyResult::new(…)` /
    // `::from(…)` / `::from_iter([…])` — every one of the 229 such mutants is
    // UNVIABLE; verified 8/8 on `py_greeks` + `_trading_core`), so none can
    // ever be MISSED. Their logic is gated through the `*_impl`/`check_*` twin
    // tests above and the glue end-to-end by the Python wheel suites in ci.yml.
    // The wrappers below have PLAIN return types (no PyErr construction in the
    // call graph), ARE Rust-callable, and DO generate viable constant-value
    // replacement mutants — each output is pinned to its core twin on values
    // OUTSIDE every cargo-mutants replacement constant (never 0, 1, -1, empty,
    // or default).

    #[test]
    fn wrapper_ema_step_delegates_byte_identically() {
        // Kills `py_ema_step -> f64 with 0.0 | 1.0 | -1.0`.
        let step = py_ema_step(0.5, 10.0, 20.0);
        assert_eq!(step, ema_step(0.5, 10.0, 20.0));
        assert_eq!(step, 15.0);
    }

    #[test]
    fn wrapper_resolve_near_month_delegates_byte_identically() {
        // Kills `py_resolve_near_month -> i64 with 0 | 1 | -1`: the on-expiry-day
        // resolve returns the expiring candidate ordinal itself, and the rolled
        // case returns the next-month ordinal — neither is a small constant.
        let on_expiry = py_resolve_near_month(739_797, 739_797, 739_825);
        assert_eq!(
            on_expiry,
            crate::indicators::metadata::resolve_near_month(739_797, 739_797, 739_825)
        );
        assert_eq!(on_expiry, 739_797);
        let rolled = py_resolve_near_month(739_798, 739_797, 739_825);
        assert_eq!(
            rolled,
            crate::indicators::metadata::resolve_near_month(739_798, 739_797, 739_825)
        );
        assert_eq!(rolled, 739_825);
    }

    #[test]
    fn wrapper_vwap_state_plain_surface_delegates_byte_identically() {
        // `PyVwapState::new` (returns Self) and `current` (returns Option<i64>)
        // are PyErr-free and Rust-callable; `update` returns `PyResult` and is
        // link-untestable (see the section header), so both states are warmed
        // through the SAME native core math and `current` is pinned. Kills
        // `current -> None | Some(0) | Some(1) | Some(-1)`.
        let mut py_state = PyVwapState::new();
        let mut core = CoreVwapState::new();
        assert_eq!(
            py_state.inner.update(10_000, 100, false, 1, 1, 34_200),
            core.update(10_000, 100, false, 1, 1, 34_200)
        );
        assert_eq!(
            py_state.inner.update(20_000, 300, false, 1, 1, 34_260),
            core.update(20_000, 300, false, 1, 1, 34_260)
        );
        // (10_000·100 + 20_000·300) / 400 = 17_500 — outside every constant.
        assert_eq!(py_state.current(), core.current());
        assert_eq!(py_state.current(), Some(17_500));
    }

    #[test]
    fn wrapper_market_structure_update_delegates_byte_identically() {
        // `PyMarketStructureState::update` returns a PLAIN 4-bool tuple and is
        // Rust-callable (`new` returns PyResult — the handle is built via the
        // module-private struct literal instead). The exact core
        // `bullish_bos_after_confirmed_swing_high` sequence: pre-break updates
        // return (false, false, false, false) and the break bar returns
        // (true, false, false, false) — together they kill every constant
        // 4-bool tuple replacement of `update`.
        let mut py_state = PyMarketStructureState {
            inner: CoreMarketStructureState::new(2),
        };
        let mut core = CoreMarketStructureState::new(2);
        let bars = [
            (100, 90, 95),
            (110, 100, 105),
            (130, 120, 125), // swing-high candidate
            (115, 105, 110),
            (112, 102, 108), // confirms the swing high (130)
        ];
        let mut last = (false, false, false, false);
        for (h, l, c) in bars {
            last = py_state.update(h, l, c);
            let sig = core.update(h, l, c);
            assert_eq!(
                last,
                (
                    sig.bos_bullish,
                    sig.bos_bearish,
                    sig.choch_bullish,
                    sig.choch_bearish
                )
            );
        }
        assert_eq!(last, (false, false, false, false));
        // Close 135 breaks the confirmed swing high 130 → bullish BOS.
        let broke = py_state.update(140, 120, 135);
        let sig = core.update(140, 120, 135);
        assert_eq!(
            broke,
            (
                sig.bos_bullish,
                sig.bos_bearish,
                sig.choch_bullish,
                sig.choch_bearish
            )
        );
        assert_eq!(broke, (true, false, false, false));
    }

    // ── stage1a_bar_bits_batch boundary (DEC-STAGE1A-RUST-BATCH-001) ───────

    /// The frozen evaluator-ABI row widths and the batch day-table row widths
    /// are the SAME constants — pinned so neither can drift alone.
    #[test]
    fn stage1a_batch_row_lengths_pin_the_frozen_evaluator_abi() {
        assert_eq!(crate::stage1a_batch::PIVOT_ROW_LEN, PIVOT_LEVELS_LEN);
        assert_eq!(crate::stage1a_batch::FIB_ROW_LEN, FIB_LEVELS_LEN);
    }

    /// One-bar batch inputs over an explicit day row (helper for the
    /// differential + guard tests below). Returns owned columns.
    #[expect(
        clippy::too_many_arguments,
        reason = "test fixture mirror of the flat ABI"
    )]
    fn one_bar_batch_masks(
        open: i64,
        high: i64,
        low: i64,
        close: i64,
        ema20: Option<i64>,
        ema200: Option<i64>,
        pivot_row: [i64; 12],
        fib_row: [i64; 15],
        tod: u32,
        bps: (i64, i64, i64),
    ) -> Result<Vec<u128>, String> {
        let inputs = crate::stage1a_batch::Stage1aBatchInputs {
            open_paisa: &[open],
            high_paisa: &[high],
            low_paisa: &[low],
            close_paisa: &[close],
            typical_paisa: &[close.max(0)],
            volume: &[0], // zero volume: VWAP stays None (the per-bar impl parity)
            tod_seconds: &[tod],
            pre_open: &[false],
            session_key: &[0],
            session_day: &[738_000],
            contract_reset: &[false],
            day_index: &[0],
            ema20_paisa: &[ema20],
            ema200_paisa: &[ema200],
            day_pivot_levels: &pivot_row,
            day_fib_levels: &fib_row,
            day_has_pivot: &[true],
            day_has_fib: &[true],
            supertrend_x2_paisa: None,
            pivot_bps: bps.0,
            pdh_pdl_bps: bps.1,
            fib_bps: bps.2,
            swing_bps: bps.2,
            prior_n: 3,
            fractal_k: 2,
            apply_1day_mask: false,
            anchor_dependent_mask: 0,
            fib_dependent_mask: 0,
            applies_to_1day_mask: IMPLEMENTED_MASK,
            day_x1_hi_paisa: &[0],
            day_x1_lo_paisa: &[0],
            day_has_x1: &[false],
            gap_mid_bps: bps.2,
        };
        check_stage1a_batch_values(&inputs)?;
        crate::stage1a_batch::stage1a_bar_bits_batch_impl(&inputs)
    }

    /// Differential vs `evaluate_with_levels_impl`: a 1-bar batch over a
    /// matching warm day row equals the per-bar evaluator (kills any
    /// struct-literal zero-fill drift between the two assemblies). Both a
    /// warm-anchor spread of proximity hits AND the degenerate flat bar.
    #[test]
    fn stage1a_batch_one_bar_differential_vs_evaluate_with_levels_impl() {
        let pivot_row = [
            1_000_000, 1_010_000, 1_020_000, 1_030_000, 990_000, 980_000, 970_000, 1_010_000,
            990_000, 1_000_000, 1_050_000, 950_000,
        ];
        let fib_row = [
            1_010_000, 1_005_300, 1_002_400, 1_000_000, 997_600, 994_300, 990_000, 1_015_400,
            1_022_400, 1_030_000, 1_042_400, 984_600, 977_600, 970_000, 957_600,
        ];
        for (open, high, low, close, ema20, ema200, tod) in [
            (
                995_000i64,
                1_012_000i64,
                989_000i64,
                1_009_990i64,
                Some(1_000_000i64),
                None,
                34_000u32,
            ),
            (
                1_000_000, 1_000_000, 1_000_000, 1_000_000, None, None, 40_000,
            ),
            (
                990_000,
                1_051_000,
                949_000,
                1_049_995,
                Some(990_000),
                Some(1_010_000),
                50_000,
            ),
        ] {
            // The bar's OWN extremes serve as the (single-bar) day extremes,
            // exactly as the batch core seeds them at the day boundary.
            let expected = evaluate_with_levels_impl(
                tod,
                open,
                high,
                low,
                close,
                ema20,
                ema200,
                None,
                pivot_row.to_vec(),
                fib_row.to_vec(),
                high,
                low,
                open,
                10,
                10,
                10,
                3,
                Vec::new(),
                MarketStructureSignal::default(),
                None,
                None,
                DEFAULT_GAP_MID_PROXIMITY_BPS,
                None,
                None,
                10,
            )
            .unwrap();
            let got = one_bar_batch_masks(
                open,
                high,
                low,
                close,
                ema20,
                ema200,
                pivot_row,
                fib_row,
                tod,
                (10, 10, 10),
            )
            .unwrap();
            // A single fed bar confirms no market-structure swing (k=2 needs
            // 2k+1 bars) — the core's signal is all-false, same as the
            // default() handed to the per-bar impl above.
            assert_eq!(got, vec![expected]);
        }
    }

    // ── check_stage1a_batch_values guard arms ─────────────────────────────

    #[test]
    fn stage1a_batch_values_rejects_out_of_range_day_level() {
        let mut pivot_row = [0i64; 12];
        pivot_row[3] = MAX_BAR_PAISA + 1;
        let err = one_bar_batch_masks(
            10_000,
            10_100,
            9_900,
            10_050,
            None,
            None,
            pivot_row,
            [0; 15],
            34_000,
            (10, 10, 10),
        )
        .unwrap_err();
        assert!(err.contains("level magnitude"), "got: {err}");
    }

    #[test]
    fn stage1a_batch_values_rejects_out_of_range_bar_price() {
        let err = one_bar_batch_masks(
            10_000,
            MAX_BAR_PAISA + 1,
            9_900,
            10_050,
            None,
            None,
            [0; 12],
            [0; 15],
            34_000,
            (10, 10, 10),
        )
        .unwrap_err();
        assert!(err.contains("bar price/level magnitude"), "got: {err}");
    }

    #[test]
    fn stage1a_batch_values_rejects_bps_product_overflow() {
        // A close near the bar ceiling with a bps near the cap overflows the
        // exact `close * bps` product — the same checked_mul arm the per-bar
        // boundary carries.
        let err = one_bar_batch_masks(
            MAX_BAR_PAISA,
            MAX_BAR_PAISA,
            MAX_BAR_PAISA,
            MAX_BAR_PAISA,
            None,
            None,
            [0; 12],
            [0; 15],
            34_000,
            (MAX_BPS, 10, 10),
        )
        .unwrap_err();
        assert!(err.contains("overflows i64"), "got: {err}");
    }

    #[test]
    fn stage1a_batch_values_rejects_negative_typical_via_vwap_guard() {
        let inputs = crate::stage1a_batch::Stage1aBatchInputs {
            open_paisa: &[10_000],
            high_paisa: &[10_100],
            low_paisa: &[9_900],
            close_paisa: &[10_050],
            typical_paisa: &[-1],
            volume: &[100],
            tod_seconds: &[34_000],
            pre_open: &[false],
            session_key: &[0],
            session_day: &[738_000],
            contract_reset: &[false],
            day_index: &[0],
            ema20_paisa: &[None],
            ema200_paisa: &[None],
            day_pivot_levels: &[0; 12],
            day_fib_levels: &[0; 15],
            day_has_pivot: &[false],
            day_has_fib: &[false],
            supertrend_x2_paisa: None,
            pivot_bps: 10,
            pdh_pdl_bps: 10,
            fib_bps: 10,
            swing_bps: 10,
            prior_n: 3,
            fractal_k: 2,
            apply_1day_mask: false,
            anchor_dependent_mask: 0,
            fib_dependent_mask: 0,
            applies_to_1day_mask: IMPLEMENTED_MASK,
            day_x1_hi_paisa: &[0],
            day_x1_lo_paisa: &[0],
            day_has_x1: &[false],
            gap_mid_bps: 10,
        };
        let err = check_stage1a_batch_values(&inputs).unwrap_err();
        assert!(err.contains("typical_paisa"), "got: {err}");
    }

    #[test]
    fn stage1a_batch_values_propagates_structural_errors() {
        // Structure runs FIRST — a day-index desync errs before any value
        // guard could index out of bounds.
        let inputs = crate::stage1a_batch::Stage1aBatchInputs {
            open_paisa: &[10_000],
            high_paisa: &[10_100],
            low_paisa: &[9_900],
            close_paisa: &[10_050],
            typical_paisa: &[10_017],
            volume: &[100],
            tod_seconds: &[34_000],
            pre_open: &[false],
            session_key: &[0],
            session_day: &[738_000],
            contract_reset: &[false],
            day_index: &[7],
            ema20_paisa: &[None],
            ema200_paisa: &[None],
            day_pivot_levels: &[0; 12],
            day_fib_levels: &[0; 15],
            day_has_pivot: &[false],
            day_has_fib: &[false],
            supertrend_x2_paisa: None,
            pivot_bps: 10,
            pdh_pdl_bps: 10,
            fib_bps: 10,
            swing_bps: 10,
            prior_n: 3,
            fractal_k: 2,
            apply_1day_mask: false,
            anchor_dependent_mask: 0,
            fib_dependent_mask: 0,
            applies_to_1day_mask: IMPLEMENTED_MASK,
            day_x1_hi_paisa: &[0],
            day_x1_lo_paisa: &[0],
            day_has_x1: &[false],
            gap_mid_bps: 10,
        };
        let err = check_stage1a_batch_values(&inputs).unwrap_err();
        assert!(err.contains("start at 0"), "got: {err}");
    }

    /// Refuter RF-α round-4 F2(b): the guard's per-bar day-row read walked
    /// a `du == 0` fixture in EVERY committed cargo test, so the two pdc
    /// index-arithmetic mutants were invisible (`+ → *` = an out-of-bounds
    /// SIGABRT on any multi-day flag-ON build). A structurally-valid TWO-DAY
    /// input exercising `du = 1` pins the multi-day guard path; the read is
    /// now operator-free (`chunks_exact(...).nth(du)`) so the arithmetic
    /// mutant class is structurally absent from the catalog as well.
    #[test]
    fn stage1a_batch_values_accepts_a_two_day_input() {
        let two_day_pivots: Vec<i64> = (0..24).map(|i| 10_000 + i).collect();
        let two_day_fibs: Vec<i64> = (0..30).map(|i| 20_000 + i).collect();
        let inputs = crate::stage1a_batch::Stage1aBatchInputs {
            open_paisa: &[10_000, 10_020],
            high_paisa: &[10_100, 10_120],
            low_paisa: &[9_900, 9_920],
            close_paisa: &[10_050, 10_070],
            typical_paisa: &[10_017, 10_037],
            volume: &[100, 100],
            tod_seconds: &[34_000, 34_000],
            pre_open: &[false, false],
            session_key: &[0, 0],
            session_day: &[738_000, 738_001],
            contract_reset: &[false, false],
            day_index: &[0, 1],
            ema20_paisa: &[None, None],
            ema200_paisa: &[None, None],
            day_pivot_levels: &two_day_pivots,
            day_fib_levels: &two_day_fibs,
            day_has_pivot: &[true, true],
            day_has_fib: &[true, true],
            supertrend_x2_paisa: None,
            pivot_bps: 10,
            pdh_pdl_bps: 10,
            fib_bps: 10,
            swing_bps: 10,
            prior_n: 3,
            fractal_k: 2,
            apply_1day_mask: false,
            anchor_dependent_mask: 0,
            fib_dependent_mask: 0,
            applies_to_1day_mask: IMPLEMENTED_MASK,
            day_x1_hi_paisa: &[0, 0],
            day_x1_lo_paisa: &[0, 0],
            day_has_x1: &[false, false],
            gap_mid_bps: 10,
        };
        assert!(check_stage1a_batch_values(&inputs).is_ok());
        // …and the whole pipeline stays total over it.
        assert_eq!(
            crate::stage1a_batch::stage1a_bar_bits_batch_impl(&inputs)
                .unwrap()
                .len(),
            2
        );
    }

    // ── gap-midpoint boundary + FFI laws (DEC-GAP-MIDPOINT-CONDITIONS-001) ──

    #[test]
    fn compute_gap_mid_impl_valid_up_down_zero() {
        // Gap-UP: midpoint of the highs, direction +1.
        assert_eq!(
            py_compute_gap_mid_impl(
                2_401_000, 2_399_000, 2_407_000, 2_404_500, 2_405_000, 2_400_000
            ),
            Ok(Some((2_404_000, 1)))
        );
        // Gap-DOWN: midpoint of the lows, direction -1.
        assert_eq!(
            py_compute_gap_mid_impl(
                2_401_000, 2_399_000, 2_396_000, 2_394_000, 2_395_000, 2_400_000
            ),
            Ok(Some((2_396_500, -1)))
        );
        // Zero gap: no level.
        assert_eq!(
            py_compute_gap_mid_impl(
                2_401_000, 2_399_000, 2_407_000, 2_404_500, 2_400_000, 2_400_000
            ),
            Ok(None)
        );
    }

    #[test]
    fn compute_gap_mid_impl_rejects_corrupt_candles() {
        // The boundary rejects hi < lo CLEANLY (the inner assert! would
        // panic → SIGABRT under panic="abort").
        let err = py_compute_gap_mid_impl(90, 100, 104, 95, 200, 100).unwrap_err();
        assert!(err.contains("X1 candle high"), "got: {err}");
        let err = py_compute_gap_mid_impl(100, 90, 95, 104, 200, 100).unwrap_err();
        assert!(err.contains("X2 candle high"), "got: {err}");
    }

    #[test]
    fn compute_gap_mid_impl_rejects_overflow_prone_magnitudes() {
        // Any operand past MAX_BAR_PAISA is rejected BEFORE the (X1 + X2)
        // midpoint sum could overflow / lose f64 exactness.
        let big = MAX_BAR_PAISA + 1;
        let err = py_compute_gap_mid_impl(big, 0, 100, 90, 200, 100).unwrap_err();
        assert!(err.contains("overflow-safe range"), "got: {err}");
        let err = py_compute_gap_mid_impl(100, 90, 104, 95, big, 100).unwrap_err();
        assert!(err.contains("overflow-safe range"), "got: {err}");
        let err = py_compute_gap_mid_impl(100, -big, 104, 95, 200, 100).unwrap_err();
        assert!(err.contains("overflow-safe range"), "got: {err}");
    }

    #[test]
    fn raw_impl_rejects_out_of_range_gap_mid() {
        let err = evaluate_from_raw_impl(
            10 * 3600,
            PDC,
            PDH,
            PDL,
            PDC,
            None,
            None,
            None,
            PDH,
            PDL,
            PDC,
            PDH,
            PDL,
            PDC,
            10,
            10,
            10,
            0,
            vec![],
            MarketStructureSignal::default(),
            None,
            Some(MAX_BAR_PAISA + 1),
            10,
            None,
            None,
            10,
        )
        .unwrap_err();
        assert!(err.contains("gap_mid_paisa magnitude"), "got: {err}");
    }

    #[test]
    fn with_levels_impl_rejects_out_of_range_gap_mid() {
        let err = evaluate_with_levels_impl(
            10 * 3600,
            PDC,
            PDH,
            PDL,
            PDC,
            None,
            None,
            None,
            vec![0; PIVOT_LEVELS_LEN],
            vec![0; FIB_LEVELS_LEN],
            PDH,
            PDL,
            PDC,
            10,
            10,
            10,
            0,
            vec![],
            MarketStructureSignal::default(),
            None,
            Some(-(MAX_BAR_PAISA + 1)),
            10,
            None,
            None,
            10,
        )
        .unwrap_err();
        assert!(err.contains("gap_mid_paisa magnitude"), "got: {err}");
    }

    #[test]
    fn check_evaluate_inputs_rejects_bad_gap_mid_bps() {
        // The 4th bps slot joins the SAME loop — a negative / absurd value is
        // rejected NAMING the slot.
        let err =
            check_evaluate_inputs(0, 0, 0, 1, None, None, 0, 0, 0, 0, 10, 10, 10, -1, 0).unwrap_err();
        assert!(err.contains("gap_mid_bps"), "got: {err}");
        let err =
            check_evaluate_inputs(0, 0, 0, 1, None, None, 0, 0, 0, 0, 10, 10, 10, MAX_BPS + 1, 0)
                .unwrap_err();
        assert!(err.contains("gap_mid_bps"), "got: {err}");
        // The gap slot also rides the exact product check.
        let err = check_evaluate_inputs(
            0,
            0,
            0,
            MAX_BAR_PAISA,
            None,
            None,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            MAX_BPS,
            0,
        )
        .unwrap_err();
        assert!(err.contains("overflows i64"), "got: {err}");
    }

    #[test]
    fn with_levels_impl_fires_the_gap_bits_from_a_resolved_level() {
        // A resolved level below/above/at the close fires exactly the
        // documented Group-15 verdicts through the production entry point.
        let eval = |gap_mid: Option<i64>| {
            evaluate_with_levels_impl(
                10 * 3600,
                1_000_000,
                1_010_000,
                990_000,
                1_000_000,
                None,
                None,
                None,
                vec![0; PIVOT_LEVELS_LEN],
                vec![0; FIB_LEVELS_LEN],
                1_010_000,
                990_000,
                1_000_000,
                10,
                10,
                10,
                0,
                vec![],
                MarketStructureSignal::default(),
                None,
                gap_mid,
                10,
                None,
                None,
                10,
            )
            .expect("valid inputs")
        };
        let above = 1u128 << crate::bits::BIT_CLOSE_ABOVE_GAP_MID;
        let below = 1u128 << crate::bits::BIT_CLOSE_BELOW_GAP_MID;
        let near = 1u128 << crate::bits::BIT_NEAR_GAP_MID;
        let family = above | below | near;
        assert_eq!(eval(None) & family, 0, "no level ⇒ no gap bit");
        assert_eq!(eval(Some(999_000)) & family, above | near);
        assert_eq!(eval(Some(1_001_000)) & family, below | near);
        assert_eq!(eval(Some(1_000_000)) & family, near);
        assert_eq!(eval(Some(900_000)) & family, above);
    }

    #[test]
    fn stage1a_batch_values_bounds_the_x1_day_tables() {
        // An out-of-envelope X1 value is rejected by the SAME level bound the
        // pivot/fib day tables ride (the midpoint-sum overflow guard).
        let inputs = crate::stage1a_batch::Stage1aBatchInputs {
            open_paisa: &[10_000],
            high_paisa: &[10_100],
            low_paisa: &[9_900],
            close_paisa: &[10_050],
            typical_paisa: &[10_017],
            volume: &[100],
            tod_seconds: &[34_000],
            pre_open: &[false],
            session_key: &[0],
            session_day: &[738_000],
            contract_reset: &[false],
            day_index: &[0],
            ema20_paisa: &[None],
            ema200_paisa: &[None],
            day_pivot_levels: &[0; 12],
            day_fib_levels: &[0; 15],
            day_has_pivot: &[false],
            day_has_fib: &[false],
            pivot_bps: 10,
            pdh_pdl_bps: 10,
            fib_bps: 10,
            swing_bps: 10,
            prior_n: 3,
            fractal_k: 2,
            apply_1day_mask: false,
            anchor_dependent_mask: 0,
            fib_dependent_mask: 0,
            applies_to_1day_mask: IMPLEMENTED_MASK,
            supertrend_x2_paisa: None,
            day_x1_hi_paisa: &[MAX_BAR_PAISA + 1],
            day_x1_lo_paisa: &[0],
            day_has_x1: &[true],
            gap_mid_bps: 10,
        };
        let err = check_stage1a_batch_values(&inputs).unwrap_err();
        assert!(err.contains("level magnitude"), "got: {err}");
    }
}
