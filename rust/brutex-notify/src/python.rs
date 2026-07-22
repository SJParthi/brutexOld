//! PyO3 binding (feature `python`) — the thin law-delegating edge.
//!
//! Four `#[pyfunction]`s delegate 1:1 to the pure core ([`crate::laws`]).
//! FFI discipline (DEC-NOTIFY-RUST-CORE-001, the DEC-FEED-RUST-CORE-001
//! shape): only `str` text, `Option<i64>` counts and `u64` IEEE-754 bit
//! patterns cross INTO Rust; only `String`/`bool` law results cross out.
//! No float OBJECT, no datetime, no secret (token / chat id / config) ever
//! crosses in either direction — the Rust surface is message text + integer
//! bit patterns only.
//!
//! Panic posture: every law is panic-free by construction (no indexing, no
//! `unwrap`, no overflow-capable arithmetic — see `laws.rs`), the release
//! profile is `panic = "abort"` (a panic can never unwind across the FFI),
//! and the Python facade carries the defensive by-name `PanicException`
//! belt regardless (the window_ledger_rust precedent).
//!
//! ## FFI edge probes (pyo3 0.29, `abi3-py312` — house discipline)
//!
//! * A Python `str` carrying a LONE SURROGATE fails UTF-8 conversion at the
//!   pyo3 edge (`UnicodeEncodeError`) — the facade's degrade set ships the
//!   oracle (pinned by the facade suite + the hypothesis differential).
//! * A Python int outside `i64` raises `OverflowError` at the edge — the
//!   facade's degrade set ships the oracle (never a silent wrap).

use pyo3::prelude::*;
use pyo3::wrap_pyfunction;

use crate::laws;

/// Crate version baked at compile time (`CARGO_PKG_VERSION`).
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// Backslash-escape every MarkdownV2-reserved character (the escape law).
#[pyfunction]
fn escape_markdown_v2(text: &str) -> String {
    laws::escape_markdown_v2(text)
}

/// Render an optional count — `"?"` on `None`, else plain decimal.
#[pyfunction]
#[pyo3(signature = (value))]
fn count_or_question(value: Option<i64>) -> String {
    laws::count_or_question(value)
}

/// The dedupe-window drop predicate over IEEE-754 f64 bit patterns.
#[pyfunction]
#[pyo3(signature = (window_bits, last_seen_bits, now_bits))]
fn dedupe_should_drop(window_bits: u64, last_seen_bits: Option<u64>, now_bits: u64) -> bool {
    laws::dedupe_should_drop(window_bits, last_seen_bits, now_bits)
}

/// Keep the first 12 Unicode code points (the Python `text[:12]` law).
#[pyfunction]
fn truncate_id_12(text: &str) -> String {
    laws::truncate_id_12(text)
}

#[pymodule]
fn _brutex_notify(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", version())?;
    // Dunder-named build stamp (sibling convention): invisible to
    // non-underscore symbol-surface checks.
    m.add("__source_digest__", crate::SOURCE_DIGEST)?;
    // Plain-named twin: part of the shim's `_EXPECTED_COMPILED_SYMBOLS`
    // handshake surface (the brutex-feed pattern).
    m.add("SOURCE_DIGEST", crate::SOURCE_DIGEST)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(escape_markdown_v2, m)?)?;
    m.add_function(wrap_pyfunction!(count_or_question, m)?)?;
    m.add_function(wrap_pyfunction!(dedupe_should_drop, m)?)?;
    m.add_function(wrap_pyfunction!(truncate_id_12, m)?)?;
    Ok(())
}
