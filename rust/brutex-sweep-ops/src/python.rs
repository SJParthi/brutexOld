//! PyO3 surface of brutex-sweep-ops (feature `python` — wheel builds only).
//!
//! Registration law (slice 0): each conversion slice appends its OWN clearly
//! fenced registration block to [`_brutex_sweep_ops`] and its own `#[pyfunction]`
//! wrappers below — never editing another slice's block — so this file grows
//! by disjoint appends (the smallest possible collision surface after
//! `lib.rs`'s frozen-manifest law).
//!
//! FFI discipline (the house pattern — DEC-FEED-RUST-CORE-001 /
//! DEC-LAKE-RUST-WINDOW-LEDGER-001): simplest-possible types cross the
//! boundary (str/f64/i64/bool + tuples/options of them); Python keeps ALL
//! error materialization, logging and domain-object construction.

use pyo3::prelude::*;

use crate::leaves;

// ───────────────────────── slice 1 — leaves ─────────────────────────

/// `run_layout` path-join law (see [`leaves::join_path_segments`]).
#[pyfunction]
fn join_path_segments(base: &str, segments: Vec<String>) -> String {
    leaves::join_path_segments(base, &segments)
}

/// `sweep_identity` gate-config fragment emission law
/// (see [`leaves::gate_config_fragment`]).
#[pyfunction]
#[allow(clippy::type_complexity)] // The FFI tuple IS the law's shape.
fn gate_config_fragment(
    overfit: Option<(f64, f64, f64, f64)>,
    stage3_enable: bool,
    stage3_top_k: i64,
    stage3_max_fevals: i64,
    stage3_max_restarts: i64,
) -> (Option<(f64, f64, f64, f64)>, Option<(i64, i64, i64)>) {
    leaves::gate_config_fragment(
        overfit,
        stage3_enable,
        stage3_top_k,
        stage3_max_fevals,
        stage3_max_restarts,
    )
}

/// `sweep_stall_policy` scaled-timeout tail (see
/// [`leaves::stall_scaled_timeout`]).
#[pyfunction]
fn stall_scaled_timeout(factor: f64, estimate: f64, base: f64) -> (f64, bool) {
    leaves::stall_scaled_timeout(factor, estimate, base)
}

// ──────────────────────── module registration ────────────────────────

#[pymodule]
fn _brutex_sweep_ops(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("RUN_DIR_SUBDIR", leaves::RUN_DIR_SUBDIR)?;
    // slice 1 — leaves
    m.add_function(wrap_pyfunction!(join_path_segments, m)?)?;
    m.add_function(wrap_pyfunction!(gate_config_fragment, m)?)?;
    m.add_function(wrap_pyfunction!(stall_scaled_timeout, m)?)?;
    // slice 2..6 registration blocks append below this line, one fenced
    // block per slice (see the module docstring's registration law).
    Ok(())
}
