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
const PIVOT_LEVELS_LEN: usize = 10;
/// Number of `fib_levels` entries [`evaluate_with_levels`] expects.
const FIB_LEVELS_LEN: usize = 15;

/// The crate version, so a loaded extension can be sanity-checked.
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// The number of atomic conditions (52).
#[pyfunction]
fn total_conditions() -> u32 {
    TOTAL_CONDITIONS
}

/// Evaluate one bar's 52-condition `u64` mask, RECOMPUTING pivots+fib from the
/// previous day's `pdh/pdl/pdc`. Used by the standalone parity check; BruteX
/// itself uses [`evaluate_with_levels`] (it already holds the levels).
#[pyfunction]
#[expect(
    clippy::too_many_arguments,
    reason = "flat primitive ABI for the parity check"
)]
#[pyo3(signature = (
    tod_seconds, open_paisa, high_paisa, low_paisa, close_paisa,
    ema20_paisa, ema200_paisa, prev_pdh_paisa, prev_pdl_paisa, prev_pdc_paisa,
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
) -> u64 {
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
    evaluate_atoms(&snapshot, &proximity, &prior)
}

/// Evaluate one bar's 52-condition `u64` mask from PRE-BUILT levels (no
/// recompute) — BruteX's `evaluate_atoms` delegation target.
///
/// `pivot_levels` (len 10) = `[p, r1, r2, r3, s1, s2, s3, pdh, pdl, pdc]`.
/// `fib_levels` (len 15) = `[retrace@pdh, r23, r38, r50, r62, r78, retrace@pdl,
/// up127, up162, up200, up262, dn127, dn162, dn200, dn262]`. The struct fields
/// the 52 conditions never read (pivot r4/s4, fib range/424) are set to 0.
/// Because the levels are passed verbatim, this is bit-identical to the Python
/// evaluator for ANY snapshot — including hand-built levels with `pdc == 0`
/// (the gap conditions' own `pdc > 0` guard handles that, no recompute to reject it).
#[pyfunction]
#[expect(
    clippy::too_many_arguments,
    reason = "flat primitive ABI for the BruteX binding"
)]
#[pyo3(signature = (
    tod_seconds, open_paisa, high_paisa, low_paisa, close_paisa,
    ema20_paisa, ema200_paisa, pivot_levels, fib_levels,
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
) -> PyResult<u64> {
    if pivot_levels.len() != PIVOT_LEVELS_LEN {
        return Err(PyValueError::new_err(format!(
            "pivot_levels must have {PIVOT_LEVELS_LEN} entries, got {}",
            pivot_levels.len()
        )));
    }
    if fib_levels.len() != FIB_LEVELS_LEN {
        return Err(PyValueError::new_err(format!(
            "fib_levels must have {FIB_LEVELS_LEN} entries, got {}",
            fib_levels.len()
        )));
    }
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
    Ok(())
}
