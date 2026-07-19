//! PyO3 binding (feature `python`) — the thin tuple-mapping edge.
//!
//! Three `#[pyfunction]`s delegate to the pure core. Only
//! ints/strings/bools/bytes cross the FFI; every body runs under
//! `std::panic::catch_unwind` so a panic surfaces as a Python
//! `RuntimeError`, never a process abort (the brutex-lake FFI-boundary
//! law — the release profile deliberately keeps unwinding on).
//!
//! JSON-engine divergence is an ABSTAIN, never a guess: the two
//! payload-taking functions receive the RAW bytes Python already
//! `json.loads`-decoded successfully. Where serde_json refuses what Python
//! accepted (`NaN`/`Infinity` literals, lone-surrogate `\udXXX` escapes,
//! >128-deep nesting), `validate_settle_evidence` returns `None` and
//! `parse_observed_sidecar` raises `ValueError` — both loud signals the
//! caller maps to the retained Python oracle.

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::wrap_pyfunction;
use std::panic::{AssertUnwindSafe, catch_unwind};

use crate::{drift, settle};

fn panic_err(what: &str) -> PyErr {
    PyRuntimeError::new_err(format!(
        "brutex-calendar {what} panicked (caught at the FFI boundary)"
    ))
}

/// Crate version baked at compile time (`CARGO_PKG_VERSION`).
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// The pure `classify_month_drift` core.
///
/// `evidence` rows are `(day_iso, zero_bars_all_indices,
/// indices_with_month_data, clean_success_windows,
/// failed_windows_touching)` in `DayEvidence` field order (day strings are
/// opaque tokens — never parsed). Returns `(candidates, flagged,
/// vetoed_failed, no_evidence, over_month_cap, over_year_cap)` where
/// `flagged` items are `(day_iso, clean_success_windows)`; the Python side
/// re-attaches `month` / `detected_at` / `zero_bar_indices` (decision-inert
/// passthroughs).
#[pyfunction]
#[pyo3(signature = (evidence, existing_year_overlay_days, max_flags_per_month, max_overlay_days_per_year))]
#[allow(clippy::type_complexity)]
fn classify_month_drift(
    evidence: Vec<(String, bool, i64, i64, i64)>,
    existing_year_overlay_days: i64,
    max_flags_per_month: i64,
    max_overlay_days_per_year: i64,
) -> PyResult<(
    Vec<String>,
    Vec<(String, i64)>,
    Vec<String>,
    Vec<String>,
    bool,
    bool,
)> {
    let out = catch_unwind(AssertUnwindSafe(|| {
        drift::classify_month_drift(
            &evidence,
            existing_year_overlay_days,
            max_flags_per_month,
            max_overlay_days_per_year,
        )
    }))
    .map_err(|_| panic_err("classify_month_drift"))?;
    Ok((
        out.candidates,
        out.flagged,
        out.vetoed_failed,
        out.no_evidence,
        out.over_month_cap,
        out.over_year_cap,
    ))
}

/// The settle-evidence truth-table validator over the RAW artifact bytes.
///
/// Returns `None` (ABSTAIN — serde_json could not parse what Python's
/// `json.loads` accepted; degrade to the Python validator), or
/// `Some((valid, message, adopted_hhmm, generated_at_raw))`:
///
/// * valid → `(True, "", Some("HH:MM"), Some(generated_at_utc))` — the
///   remaining `SettleEvidence` fields are re-extracted Python-side from
///   its own parse of the SAME document under the just-proved guarantees;
/// * invalid → `(False, message, None, None)` with `message` the EXACT
///   Python `ValueError` text `settle evidence {path_repr}: {reason}` —
///   `path_repr` is passed PRE-repr'd by Python.
#[pyfunction]
#[pyo3(signature = (payload_json, path_repr))]
#[allow(clippy::type_complexity)]
fn validate_settle_evidence(
    payload_json: Vec<u8>,
    path_repr: String,
) -> PyResult<Option<(bool, String, Option<String>, Option<String>)>> {
    catch_unwind(AssertUnwindSafe(|| {
        let Ok(payload) = serde_json::from_slice::<serde_json::Value>(&payload_json) else {
            return None; // parse-engine divergence — abstain to the oracle
        };
        Some(
            match settle::validate_evidence_payload(&payload, &path_repr) {
                Ok(valid) => (
                    true,
                    String::new(),
                    Some(valid.adopted_hhmm),
                    Some(valid.generated_raw),
                ),
                Err(message) => (false, message, None, None),
            },
        )
    }))
    .map_err(|_| panic_err("validate_settle_evidence"))
}

/// The observed-holidays overlay whole-accept/whole-reject decision over
/// the RAW sidecar bytes.
///
/// `Some(sorted unique ISO days)` iff the payload passes EVERY loader rule
/// (version `== 1` under Python `==` punning, entries a list, every entry
/// labeled + canonical); `None` when the overlay is REJECTED (one bad entry
/// rejects the whole overlay). A serde_json parse failure raises
/// `ValueError` — a LOUD abstain (Python's `json.loads` may accept shapes
/// serde refuses; the caller degrades to the Python loader, which owns the
/// per-failure WARN wording anyway).
#[pyfunction]
#[pyo3(signature = (payload_json,))]
fn parse_observed_sidecar(payload_json: Vec<u8>) -> PyResult<Option<Vec<String>>> {
    catch_unwind(AssertUnwindSafe(|| {
        let payload = serde_json::from_slice::<serde_json::Value>(&payload_json).map_err(|_| {
            PyValueError::new_err(
                "brutex-calendar: sidecar JSON parse diverged from Python json — \
                 degrade to the Python loader",
            )
        })?;
        Ok(drift::parse_observed_sidecar(&payload))
    }))
    .map_err(|_| panic_err("parse_observed_sidecar"))?
}

#[pymodule]
fn _brutex_calendar(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", version())?;
    // Dunder-named build stamp (sibling convention): invisible to
    // non-underscore symbol-surface checks.
    m.add("__source_digest__", crate::SOURCE_DIGEST)?;
    // Plain-named twin: part of the shim's `_EXPECTED_COMPILED_SYMBOLS`
    // handshake surface (the brutex-feed pattern).
    m.add("SOURCE_DIGEST", crate::SOURCE_DIGEST)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(classify_month_drift, m)?)?;
    m.add_function(wrap_pyfunction!(validate_settle_evidence, m)?)?;
    m.add_function(wrap_pyfunction!(parse_observed_sidecar, m)?)?;
    Ok(())
}
