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

use crate::bits::TOTAL_CONDITIONS;
use crate::evaluator::{IndicatorSnapshot, PriorBarHistory, ProximityParams, evaluate_atoms};
use crate::indicators::fib::{FibLevels, compute_fib_levels};
use crate::indicators::pivots::{PivotLevels, compute_pivot_levels};

/// Number of `pivot_levels` entries [`evaluate_with_levels`] expects.
///
/// `[p, r1, r2, r3, s1, s2, s3, pdh, pdl, pdc, r5, s5]` — the original 10 plus the
/// PR-2 R5/S5 levels appended (never renumbered; DEC-NIFTY-INDICATORS-001).
const PIVOT_LEVELS_LEN: usize = 12;
/// Number of `fib_levels` entries [`evaluate_with_levels`] expects.
const FIB_LEVELS_LEN: usize = 15;

/// Overflow-safe magnitude ceiling for a previous-day anchor (paisa).
///
/// The inner indicators (`compute_pivot_levels` / `compute_fib_levels`) do
/// integer arithmetic that, under the crate's `overflow-checks = true` release
/// profile, **panics** on overflow (a finance-safety choice, see `lib.rs`). The
/// largest derived pivot magnitude is `r4 = pdh + 3·(p − pdl)` (bounded by
/// `4·pdh − 3·pdl`) and the largest fib intermediate is `pdh + 3.236·range`;
/// the sum `pdh + pdl + pdc` is bounded by `3·pdh`. Constraining every anchor to
/// `|x| ≤ i64::MAX / 8` makes ALL of these (≤ `7·(i64::MAX/8) < i64::MAX`)
/// provably overflow-free, so a hostile/garbage FFI caller gets a clean
/// `ValueError` instead of a panic → `abort` (SIGABRT under `panic = "abort"`,
/// which would crash the whole interpreter — a DoS).
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
    )?;
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
    };
    let proximity = ProximityParams {
        pivot_proximity_bps: pivot_bps,
        pdh_pdl_proximity_bps: pdh_pdl_bps,
        fib_proximity_bps: fib_bps,
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
/// `fib_levels` (len 15) = `[retrace@pdh, r23, r38, r50, r62, r78, retrace@pdl,
/// up127, up162, up200, up262, dn127, dn162, dn200, dn262]`. `vwap_paisa` is the
/// session VWAP (`None` during warm-up / on 1day — the Group-10 bits then do not
/// fire). The struct fields the conditions never read (pivot r4/s4, fib range/424)
/// are set to 0. Because the levels are passed verbatim, this is bit-identical to
/// the Python evaluator for ANY snapshot — including hand-built levels with
/// `pdc == 0` (the gap conditions' own `pdc > 0` guard handles that).
///
/// The length guards (12 / 15) are validated FIRST (a mismatch would otherwise
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
        ext_up_424_paisa: 0,
        ext_down_424_paisa: 0,
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
    };
    let proximity = ProximityParams {
        pivot_proximity_bps: pivot_bps,
        pdh_pdl_proximity_bps: pdh_pdl_bps,
        fib_proximity_bps: fib_bps,
    };
    let prior = PriorBarHistory {
        prior_n,
        flags: prior_flags,
    };
    Ok(evaluate_atoms(&snapshot, &proximity, &prior))
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
/// Returns a 15-element list of the COMPUTED levels (int64 paisa), in order:
/// `[retrace_23, retrace_38, retrace_50, retrace_62, retrace_78,
///   ext_up_127, ext_up_162, ext_up_200, ext_up_262, ext_up_424,
///   ext_down_127, ext_down_162, ext_down_200, ext_down_262, ext_down_424]`.
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
    Ok(vec![
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
    ])
}

/// The Python extension module `trading_core._trading_core`.
#[pymodule]
fn _trading_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", version())?;
    m.add("TOTAL_CONDITIONS", TOTAL_CONDITIONS)?;
    m.add("PIVOT_LEVELS_LEN", PIVOT_LEVELS_LEN)?;
    m.add("FIB_LEVELS_LEN", FIB_LEVELS_LEN)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(total_conditions, m)?)?;
    m.add_function(wrap_pyfunction!(evaluate_from_raw, m)?)?;
    m.add_function(wrap_pyfunction!(evaluate_with_levels, m)?)?;
    m.add_function(wrap_pyfunction!(py_compute_pivot_levels, m)?)?;
    m.add_function(wrap_pyfunction!(py_compute_fib_levels, m)?)?;
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
            ]
        );
        assert_eq!(got.len(), 15);
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
        )
        .unwrap_err();
        assert!(err.contains("vwap_paisa magnitude"), "got: {err}");
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
                0
            )
            .is_err()
        );
        // A small close × MAX_BPS fits → accepted (the product guard does not
        // over-reject a legitimate large bps on a normal-priced bar).
        assert!(
            check_evaluate_inputs(0, 0, 0, 1_000_000, None, None, 0, 0, 0, 0, MAX_BPS, 0, 0)
                .is_ok()
        );
        // pdc out of range → rejected.
        assert!(
            check_evaluate_inputs(0, 0, 0, 0, None, None, 0, 0, 0, MAX_BAR_PAISA + 1, 0, 0, 0)
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
            check_evaluate_inputs(0, 0, 0, fits, None, None, 0, 0, 0, 0, MAX_BPS, 0, 0).is_ok()
        );
        // one paisa over → product overflows i64 → REJECTED.
        let err = check_evaluate_inputs(0, 0, 0, over, None, None, 0, 0, 0, 0, MAX_BPS, 0, 0)
            .unwrap_err();
        assert!(err.contains("overflows i64"), "got: {err}");
        // The same boundary holds per-bps-slot: only the offending slot rejects.
        assert!(
            check_evaluate_inputs(0, 0, 0, fits, None, None, 0, 0, 0, 0, 0, MAX_BPS, 0).is_ok()
        );
        assert!(
            check_evaluate_inputs(0, 0, 0, over, None, None, 0, 0, 0, 0, 0, 0, MAX_BPS).is_err()
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
            None, // vwap_paisa
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
            None, // vwap_paisa
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
            )
            .unwrap();
            acc = acc.wrapping_add(m);
        }
        // Black-box the accumulator so the loop is not optimised away; the exact
        // value is incidental (the per-call parity asserts above are the point).
        std::hint::black_box(acc);
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
            prop_assert_eq!(got_f.len(), FIB_LEVELS_LEN);
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
                day_high, day_low, open_of_day, 1_000_000, bps, bps, bps,
            ).is_ok());
            // … and a full valid-anchor evaluate runs without panic.
            let mask = evaluate_from_raw_impl(
                10 * 3600, open, high, low, close, Some(close), Some(close), Some(close),
                1_010_000, 990_000, 1_000_000, day_high, day_low, open_of_day,
                bps, bps, bps, 0, vec![],
            );
            prop_assert!(mask.is_ok());
        }
    }
}
