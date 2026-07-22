//! PyO3 binding (feature `python`) — the thin mapping edge under a panic
//! belt.
//!
//! Two `#[pyfunction]`s delegate to the pure cores (`crate::score`,
//! `crate::abstain`) with floats/ints-only FFI. Every core call runs inside
//! `catch_unwind` so a defect-class panic surfaces as a Python
//! `RuntimeError` the facade's degrade belt catches (ship-the-oracle),
//! never a process abort through the FFI boundary (the brutex-lake panic
//! belt precedent). A structural input violation from the core maps to
//! `ValueError`.

use std::panic::{AssertUnwindSafe, catch_unwind};

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::wrap_pyfunction;

use crate::{abstain, score};

/// Crate version baked at compile time (`CARGO_PKG_VERSION`).
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// Batch logistic P(win) scorer — see `crate::score::score_logistic`.
///
/// Structural violations (`n_features == 0`, length mismatches) raise
/// `ValueError`; a caught panic raises `RuntimeError`. The facade
/// (`brutex/meta/meta_rust.py`) degrades BOTH to the Python oracle.
#[pyfunction]
#[pyo3(signature = (features, n_features, weights, bias))]
fn score_logistic(
    features: Vec<f64>,
    n_features: usize,
    weights: Vec<f64>,
    bias: f64,
) -> PyResult<Vec<f64>> {
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        score::score_logistic(&features, n_features, &weights, bias)
    }))
    .map_err(|_| PyRuntimeError::new_err("brutex_meta.score_logistic: kernel panic"))?;
    outcome.map_err(PyValueError::new_err)
}

/// Batch abstention-gate reason bitmasks — see
/// `crate::abstain::abstain_masks` for the full door semantics + the
/// NaN-encoding conventions (NaN vix = missing stamp, NaN band side =
/// unbounded, NaN tau = no confidence gate).
#[pyfunction]
#[pyo3(signature = (regimes, allowed_mask, vix, vix_lo, vix_hi, event_flags, block_event_days, confidence, tau))]
#[allow(clippy::too_many_arguments)]
fn abstain_masks(
    regimes: Vec<i64>,
    allowed_mask: u64,
    vix: Vec<f64>,
    vix_lo: f64,
    vix_hi: f64,
    event_flags: Vec<u8>,
    block_event_days: bool,
    confidence: Vec<f64>,
    tau: f64,
) -> PyResult<Vec<u8>> {
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        abstain::abstain_masks(
            &regimes,
            allowed_mask,
            &vix,
            vix_lo,
            vix_hi,
            &event_flags,
            block_event_days,
            &confidence,
            tau,
        )
    }))
    .map_err(|_| PyRuntimeError::new_err("brutex_meta.abstain_masks: kernel panic"))?;
    outcome.map_err(PyValueError::new_err)
}

#[pymodule]
fn _brutex_meta(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", version())?;
    // Dunder-named build stamp (sibling convention): invisible to
    // non-underscore symbol-surface checks.
    m.add("__source_digest__", crate::SOURCE_DIGEST)?;
    // Plain-named twin: part of the shim's `_EXPECTED_COMPILED_SYMBOLS`
    // handshake surface.
    m.add("SOURCE_DIGEST", crate::SOURCE_DIGEST)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(score_logistic, m)?)?;
    m.add_function(wrap_pyfunction!(abstain_masks, m)?)?;
    Ok(())
}
