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

// --- pull_stall (Wave 1C) ---------------------------------------------------
// The wedge/alive decision plane of `brutex/dashboard/pull_stall.py`
// (DEC-PULL-WEDGE-ESCALATE-001 + DEC-PULL-TELEMETRY-TRIAD-001). Pure +
// clockless: the caller reads its monotonic clock ONCE and passes ages;
// only bools / f64s / short revision-token strings cross the FFI in, one
// small int (the arm token) or bool crosses out. Both cores are TOTAL over
// the full f64 plane (NaN/inf included — IEEE-faithful strict-`<` guards),
// so no error arm exists; the Python facade's defensive belt stays as
// insurance against a stale/corrupt wheel only.

/// The DUAL-signal wedge-escalation arm token (0..=5) — see
/// `crate::stall::wedge_decision` for the full contract.
#[allow(clippy::too_many_arguments, clippy::fn_params_excessive_bools)] // 1:1 FFI mirror of the oracle's state
#[pyfunction]
#[pyo3(signature = (hung, already_fired, hook_bound, baseline_captured, last_beat_age_s, wedge_window_s, restart_age_s, baseline, current))]
fn wedge_decision(
    hung: bool,
    already_fired: bool,
    hook_bound: bool,
    baseline_captured: bool,
    last_beat_age_s: f64,
    wedge_window_s: f64,
    restart_age_s: Option<f64>,
    baseline: Option<&str>,
    current: Option<&str>,
) -> u8 {
    crate::stall::wedge_decision(
        hung,
        already_fired,
        hook_bound,
        baseline_captured,
        last_beat_age_s,
        wedge_window_s,
        restart_age_s,
        baseline,
        current,
    )
}

/// The throttled alive-beat emit-now? law — see `crate::stall::alive_log_due`.
#[pyfunction]
#[pyo3(signature = (since_last_s, interval_s))]
fn alive_log_due(since_last_s: f64, interval_s: f64) -> bool {
    crate::stall::alive_log_due(since_last_s, interval_s)
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
    // --- pull_stall (Wave 1C) ---
    m.add_function(wrap_pyfunction!(wedge_decision, m)?)?;
    m.add_function(wrap_pyfunction!(alive_log_due, m)?)?;
    Ok(())
}
