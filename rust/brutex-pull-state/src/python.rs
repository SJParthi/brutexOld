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

// ── pull_scope (Wave 1B) — note-surgery + scope-token laws ───────────────────
//
// Strings cross this edge (the ONE sanctioned exception to the integers-only
// discipline — the outputs of these laws ARE strings, so byte-exactness IS
// the contract; every literal is drift-pinned by the committed golden + the
// Python-side cross-language source pin). The booleans are the seam's
// `note_surgery_miss` WARN inputs; the WARN itself stays Python.

/// `normalize_pull_scope` verdict code (0=full · 1=spot-only · 2=fno-only ·
/// 255=unknown — Python composes the `{token!r}` refusal itself).
#[pyfunction]
#[pyo3(signature = (token))]
fn scope_token(token: Option<&str>) -> u8 {
    crate::pull_scope::scope_token(token)
}

/// The market-hours deferral-note rewrite (both-arms wording; first
/// occurrence each; drifted notes pass through unchanged).
#[pyfunction]
#[pyo3(signature = (note, phases_replacement))]
fn market_hours_note_rewrite(note: &str, phases_replacement: &str) -> String {
    crate::pull_scope::market_hours_note_rewrite(note, phases_replacement)
}

/// `(rewritten_note, prefix_present, clause_present)` — the spot-only
/// completion-note surgery MINUS the Python-side WARN + step-3 appends.
#[pyfunction]
#[pyo3(signature = (base_note))]
fn spot_only_note_surgery(base_note: &str) -> (String, bool, bool) {
    crate::pull_scope::spot_only_note_surgery(base_note)
}

/// `(rewritten_note, prefix_present, clause_present)` — the fno-only mirror.
#[pyfunction]
#[pyo3(signature = (base_note))]
fn fno_only_note_surgery(base_note: &str) -> (String, bool, bool) {
    crate::pull_scope::fno_only_note_surgery(base_note)
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
    // pull_scope (Wave 1B) — append-only registrations.
    m.add_function(wrap_pyfunction!(scope_token, m)?)?;
    m.add_function(wrap_pyfunction!(market_hours_note_rewrite, m)?)?;
    m.add_function(wrap_pyfunction!(spot_only_note_surgery, m)?)?;
    m.add_function(wrap_pyfunction!(fno_only_note_surgery, m)?)?;
    Ok(())
}
