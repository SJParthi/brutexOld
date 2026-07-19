//! PyO3 binding (feature `python`) — the thin per-lane mapping edge.
//!
//! Every pyfunction delegates to a pure core and maps its outcome to plain
//! Python objects: `bytes`, `str`, tuples of `str`, and typed integer
//! refusal codes. Per the ints+pre-formatted-strings FFI law nothing here
//! formats a price, a timestamp, or an error message — Python owns all of
//! those on both sides of the boundary.
//!
//! ## Lane registration (APPEND-ONLY)
//!
//! One clearly-delimited block per ops lane, each function name prefixed
//! with its lane token (`crossverify_*`). A future lane (backup, nightly, …)
//! APPENDS its own block — it never edits an existing lane's block (the
//! rust/brutex-lake multi-wave precedent).

use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyo3::wrap_pyfunction;

use crate::crossverify;

/// Crate version baked at compile time (`CARGO_PKG_VERSION`).
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

// ─────────────────── crossverify lane (APPEND-ONLY block) ───────────────────

/// Assemble one day file's pinned CSV bytes from PRE-VALIDATED,
/// PRE-FORMATTED cell rows; return `(bytes, None)` on success or
/// `(None, (code, row, cell))` on a typed refusal (the string-law belt —
/// see `crate::crossverify::render_csv_bytes`). Never raises on data; a
/// non-list/non-str argument fails PyO3 extraction (`TypeError`), which the
/// Python seam's degrade set catches.
#[pyfunction]
#[pyo3(signature = (header, rows))]
#[allow(clippy::type_complexity)] // The (bytes | refusal-code) union crosses as one plain tuple.
fn crossverify_render_csv(
    py: Python<'_>,
    header: String,
    rows: Vec<Vec<String>>,
) -> (Option<Py<PyBytes>>, Option<(u8, usize, usize)>) {
    match crossverify::render_csv_bytes(&header, &rows) {
        Ok(bytes) => (Some(PyBytes::new(py, &bytes).unbind()), None),
        Err(refusal) => (None, Some((refusal.code, refusal.row, refusal.cell))),
    }
}

/// Map one lake coordinate (+ the PYTHON-parsed FNO grammar fields) to the
/// canonical identity's six fields, or `None` (deny-by-default — see
/// `crate::crossverify::canonical_identity_fields`).
#[pyfunction]
#[pyo3(signature = (exchange, segment, lake_symbol, fno_underlying, fno_expiry_iso, fno_is_future, fno_exchange))]
fn crossverify_canonical_identity(
    exchange: String,
    segment: String,
    lake_symbol: String,
    fno_underlying: Option<String>,
    fno_expiry_iso: Option<String>,
    fno_is_future: bool,
    fno_exchange: Option<String>,
) -> Option<crossverify::IdentityFields> {
    crossverify::canonical_identity_fields(
        &exchange,
        &segment,
        &lake_symbol,
        fno_underlying.as_deref(),
        fno_expiry_iso.as_deref(),
        fno_is_future,
        fno_exchange.as_deref(),
    )
}

/// The ONE S3 key law — `<prefix>/<day_iso>/<segment_token>/<symbol>.csv`
/// (see `crate::crossverify::s3_key_assemble`; *day_iso* is the Python-side
/// `date.isoformat()`, never re-derived here).
#[pyfunction]
#[pyo3(signature = (prefix, day_iso, segment_token, canonical_symbol))]
fn crossverify_s3_key(
    prefix: String,
    day_iso: String,
    segment_token: String,
    canonical_symbol: String,
) -> String {
    crossverify::s3_key_assemble(&prefix, &day_iso, &segment_token, &canonical_symbol)
}

// ────────────────── end crossverify lane (append new lanes BELOW) ──────────────────

#[pymodule]
fn _brutex_ops(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", version())?;
    // Dunder-named build stamp (sibling convention): invisible to
    // non-underscore symbol-surface checks.
    m.add("__source_digest__", crate::SOURCE_DIGEST)?;
    // Plain-named twin (the brutex-feed shape).
    m.add("SOURCE_DIGEST", crate::SOURCE_DIGEST)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    // ── crossverify lane registrations (APPEND-ONLY block) ──
    m.add_function(wrap_pyfunction!(crossverify_render_csv, m)?)?;
    m.add_function(wrap_pyfunction!(crossverify_canonical_identity, m)?)?;
    m.add_function(wrap_pyfunction!(crossverify_s3_key, m)?)?;
    // ── end crossverify lane (future lanes append their blocks below) ──
    Ok(())
}
