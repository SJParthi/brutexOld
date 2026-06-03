//! PyO3 binding scaffold (feature `python`) — Phase-2 entry points for BruteX.
//!
//! Phase 1 exposes a version probe + a single-bar `evaluate_from_raw` (the exact
//! fixture-case → `u64` mapping). Phase 2 replaces/augments this with the batched
//! columnar API (pass N bar-snapshots, return N `u64`s in one PyO3 crossing) and
//! wires `brutex/vocabulary` + `brutex/indicators` to delegate here.

use pyo3::prelude::*;
use pyo3::wrap_pyfunction;

use crate::bits::TOTAL_CONDITIONS;
use crate::evaluator::{IndicatorSnapshot, PriorBarHistory, ProximityParams, evaluate_atoms};
use crate::indicators::fib::compute_fib_levels;
use crate::indicators::pivots::compute_pivot_levels;

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

/// Evaluate one bar's 52-condition `u64` mask from raw int64-paisa inputs.
///
/// Pivots + Fib levels are recomputed internally from the previous day's
/// `pdh/pdl/pdc` — the identical reconstruction the golden fixture + the Python
/// test use. This is the per-bar primitive BruteX will call in Phase 2 (the
/// batched form is the Phase-2 optimisation).
#[pyfunction]
#[expect(
    clippy::too_many_arguments,
    reason = "flat primitive ABI for the Phase-2 binding"
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

/// The Python extension module `trading_core._trading_core`.
#[pymodule]
fn _trading_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", version())?;
    m.add("TOTAL_CONDITIONS", TOTAL_CONDITIONS)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(total_conditions, m)?)?;
    m.add_function(wrap_pyfunction!(evaluate_from_raw, m)?)?;
    Ok(())
}
