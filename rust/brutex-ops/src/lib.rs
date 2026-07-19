//! # brutex-ops — BruteX ops/automation Rust kernels
//!
//! DEC-NIGHTLY-RUST-CORE-001. This file is MODULE REGISTRATION ONLY — the
//! append-only spine for the ops conversion lanes. ALL code lives in the
//! per-lane module files; sibling ops lanes (the named
//! DEC-NIGHTLY-RUST-CORE-002 backup/restore ordering-law follow-up, any
//! future backup ops lane) APPEND their own `mod <lane>;` line plus their
//! registration lines inside the ONE `#[pymodule]` below — keep-both
//! merges by design, never in place of an existing line (the brutex-lake
//! §4.1 concurrent-sibling additive-registration law).
//!
//! The Python consumer is the default-OFF facade
//! `brutex/nightly/nightly_rust.py` (`BRUTEX_NIGHTLY_RUST`); an absent or
//! stale install degrades loudly to the retained Python oracle there, so a
//! missing wheel can never break a nightly run, an install, or a status
//! probe.

use pyo3::prelude::*;

pub mod nightly;

/// Crate version re-exported to Python as `brutex_ops.__version__`.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

// `#[pymodule]` registers the `_brutex_ops` Python submodule. Maturin
// produces a cdylib whose extern "C" `PyInit__brutex_ops` symbol matches
// this function name; under the mixed layout it installs at
// `site-packages/brutex_ops/_brutex_ops.<abi>.so` with
// `brutex_ops/__init__.py` re-exporting the symbols (the fno-math
// DEC-P0-055 convention).
#[pymodule]
fn _brutex_ops(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", VERSION)?;
    // ── nightly lane (DEC-NIGHTLY-RUST-CORE-001) ────────────────────────
    // Sibling lanes APPEND below this block, never inside or above it.
    m.add_function(wrap_pyfunction!(nightly::nightly_first_breaker_index, m)?)?;
    m.add_function(wrap_pyfunction!(nightly::nightly_exit_code, m)?)?;
    m.add_function(wrap_pyfunction!(
        nightly::nightly_audit_reconcile_detail,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(nightly::nightly_count_text, m)?)?;
    m.add_function(wrap_pyfunction!(nightly::nightly_verdict, m)?)?;
    m.add_function(wrap_pyfunction!(nightly::nightly_summary_lines, m)?)?;
    m.add_function(wrap_pyfunction!(nightly::nightly_xml_escape, m)?)?;
    m.add_function(wrap_pyfunction!(nightly::nightly_render_launchd_plist, m)?)?;
    m.add_function(wrap_pyfunction!(nightly::nightly_parse_at, m)?)?;
    m.add_function(wrap_pyfunction!(nightly::nightly_pmset_wake_command, m)?)?;
    m.add_function(wrap_pyfunction!(nightly::nightly_seconds_since_fire, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    #[test]
    fn version_matches_manifest() {
        assert_eq!(super::VERSION, "0.1.0");
    }
}
