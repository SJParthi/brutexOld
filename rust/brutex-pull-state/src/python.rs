//! PyO3 binding (feature `python`) — the thin integer-mapping edge.
//!
//! ONE `#[pyfunction]` ([`pull_allowed_us`]) delegates to the pure core
//! (`crate::market_hours::pull_allowed_us`) and maps its outcome to a plain
//! Python `bool`. INTEGERS ONLY cross Rust → Python input (the caller's
//! microsecond-of-day + the trading-day flag); no float, no string, no
//! timestamp object ever crosses (the DEC-FEED-RUST-CORE-001 discipline —
//! guard verdicts stay byte-identical by construction).
//!
//! Panic belt: the core is TOTAL over its input domain (the out-of-range arm
//! returns `Err`, mapped to `ValueError` here — the Python facade's
//! defensive `except Exception` belt degrades such a call to the oracle);
//! `2**63`-and-beyond inputs raise `OverflowError` at the pyo3 `i64` edge,
//! never a silent wrap (the brutex-feed FFI probe precedent).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::wrap_pyfunction;

use crate::market_hours;
use crate::pull_outcome;

/// Crate version baked at compile time (`CARGO_PKG_VERSION`).
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// `True` iff a vendor pull is allowed at the given IST instant.
///
/// See `crate::market_hours::pull_allowed_us` for the full contract. An
/// out-of-range `us_of_day` raises `ValueError` (the Python seam's
/// defensive belt catches it and degrades to the oracle).
#[pyfunction]
#[pyo3(signature = (us_of_day, is_trading_day))]
fn pull_allowed_us(us_of_day: i64, is_trading_day: bool) -> PyResult<bool> {
    market_hours::pull_allowed_us(us_of_day, is_trading_day).map_err(PyValueError::new_err)
}

// --- pull_outcome (Wave 1A, DEC-RUST-PULL-STATE-001) ---------------------

/// The last-pull outcome DECISION codes `(outcome, tone, headline_code,
/// detail_code)` — machine tokens only; Python composes every human string
/// and keeps the `verdict_banner` colour authority (the `@verdict` /
/// `@verdict_prepull` markers hand the banner fold back to Python).
///
/// Total over every input: strings cross as UTF-8, `failed` as `i64` (an
/// out-of-`i64` int raises `OverflowError` at the pyo3 edge — the facade's
/// defensive belt degrades such a call to the oracle).
#[pyfunction]
#[pyo3(signature = (state, detail, failed, stalled, wedged, has_verdict))]
fn classify_pull_outcome(
    state: &str,
    detail: &str,
    failed: i64,
    stalled: bool,
    wedged: bool,
    has_verdict: bool,
) -> (String, String, String, String) {
    pull_outcome::classify_pull_outcome(state, detail, failed, stalled, wedged, has_verdict)
}

/// The run-id correlation law of `classify_retained_pull_outcome`: `True`
/// iff the retained snapshot's NON-empty run id equals the verdict holder's
/// stamped id (see `crate::pull_outcome::verdict_correlates`).
#[pyfunction]
#[pyo3(signature = (retained_id, verdict_id))]
fn verdict_correlates(retained_id: &str, verdict_id: &str) -> bool {
    pull_outcome::verdict_correlates(retained_id, verdict_id)
}

#[pymodule]
fn _brutex_pull_state(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", version())?;
    // Dunder-named build stamp (sibling convention): invisible to
    // non-underscore symbol-surface checks.
    m.add("__source_digest__", crate::SOURCE_DIGEST)?;
    // Plain-named twin: part of the shim's `_EXPECTED_COMPILED_SYMBOLS`
    // handshake surface (the brutex-feed pattern).
    m.add("SOURCE_DIGEST", crate::SOURCE_DIGEST)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(pull_allowed_us, m)?)?;
    // --- pull_outcome (Wave 1A) ---
    m.add_function(wrap_pyfunction!(classify_pull_outcome, m)?)?;
    m.add_function(wrap_pyfunction!(verdict_correlates, m)?)?;
    Ok(())
}
