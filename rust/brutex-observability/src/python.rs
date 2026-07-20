//! PyO3 binding (feature `python`) — the thin scalar-mapping edge.
//!
//! Five `#[pyfunction]`s delegate to the pure core and map its outcomes to
//! plain Python tuples / strings of SCALARS. No datetime, no PyAny, no
//! dicts, no callables cross the boundary — every conversion is documented
//! on the facade side (`brutex/observability/observability_rust.py`).
//!
//! Argument-conversion faults (a non-float where f64 is expected, a
//! negative/huge int where u64 is expected, a lone-surrogate string where
//! `&str` is expected) raise `TypeError`/`OverflowError`/`ValueError` at
//! this edge, and the law-A `episodes + 1` overflow refusal
//! ([`crate::EpisodesOverflow`], the `checked_add` law) maps to `ValueError`
//! here — all members of the facade's degrade set, which serves the Python
//! oracle instead.
//!
//! The module ALSO exports **`PanicException`** — pyo3's panic class is a
//! PER-EXTENSION-MODULE static (each abi3 wheel lazily inits its OWN type
//! object), so a name-resolved class from `pyo3_runtime` (never a real
//! importable module) or another extension (e.g. polars) can NEVER be this
//! wheel's class. Exporting our own lets the facade's panic belt resolve
//! the EXACT class a panic from THIS wheel would raise.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::wrap_pyfunction;

use crate::{detector, stamp};

/// The law-A wire shape — the 12-scalar decomposition of `WatchdogVerdict`:
/// `(tier, stall_started_beat, last_logged_at, last_alerted_at, episodes,
/// lag_s, warn_edge, critical_edge, relog, realert, recovered,
/// stall_total_s)`.
type LoopVerdictTuple = (
    &'static str,
    f64,
    f64,
    f64,
    u64,
    f64,
    bool,
    bool,
    bool,
    bool,
    bool,
    f64,
);

/// Law A — the loop-watchdog tiered stall decision.
///
/// Returns the 12-scalar tuple `(tier, stall_started_beat, last_logged_at,
/// last_alerted_at, episodes, lag_s, warn_edge, critical_edge, relog,
/// realert, recovered, stall_total_s)` — the scalar decomposition of the
/// Python `WatchdogVerdict` (the facade recomposes via the caller-passed
/// dataclass factories). A warn edge at `episodes == u64::MAX` raises
/// `ValueError` (the `checked_add` refusal, message naming the field) — the
/// facade's degrade set serves the oracle.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (now, last_beat, tier, stall_started_beat, last_logged_at, last_alerted_at, episodes, warn_after_s, critical_after_s, relog_every_s, realert_every_s))]
fn loop_watchdog_check_once(
    now: f64,
    last_beat: f64,
    tier: &str,
    stall_started_beat: f64,
    last_logged_at: f64,
    last_alerted_at: f64,
    episodes: u64,
    warn_after_s: f64,
    critical_after_s: f64,
    relog_every_s: f64,
    realert_every_s: f64,
) -> PyResult<LoopVerdictTuple> {
    let v = detector::loop_watchdog_check_once(
        now,
        last_beat,
        tier,
        stall_started_beat,
        last_logged_at,
        last_alerted_at,
        episodes,
        warn_after_s,
        critical_after_s,
        relog_every_s,
        realert_every_s,
    )
    .map_err(|err| PyValueError::new_err(err.to_string()))?;
    Ok((
        v.tier,
        v.stall_started_beat,
        v.last_logged_at,
        v.last_alerted_at,
        v.episodes,
        v.lag_s,
        v.warn_edge,
        v.critical_edge,
        v.relog,
        v.realert,
        v.recovered,
        v.stall_total_s,
    ))
}

/// Law B — the per-worker HEALTHY↔HUNG decision: `(elapsed, hung, edge)`.
#[pyfunction]
#[pyo3(signature = (now, last_beat, timeout_seconds, prev_hung))]
fn watchdog_decide(
    now: f64,
    last_beat: f64,
    timeout_seconds: f64,
    prev_hung: bool,
) -> (f64, bool, bool) {
    detector::watchdog_decide(now, last_beat, timeout_seconds, prev_hung)
}

/// Law C — the SLA predicate + hysteresis edge: `(breached, edge)`.
#[pyfunction]
#[pyo3(signature = (predicate, value, threshold, prev_breach))]
fn sla_decide(predicate: &str, value: f64, threshold: f64, prev_breach: bool) -> (bool, bool) {
    detector::sla_decide(predicate, value, threshold, prev_breach)
}

/// Law D1 — the strict-parse acceptance-order law (first-false index, −1 accept).
#[pyfunction]
#[pyo3(signature = (root_is_dict, stage_is_str, run_id_is_str, hash_is_str, produced_ok, extra_ok))]
fn stamp_shape_index(
    root_is_dict: bool,
    stage_is_str: bool,
    run_id_is_str: bool,
    hash_is_str: bool,
    produced_ok: bool,
    extra_ok: bool,
) -> i64 {
    stamp::stamp_shape_index(
        root_is_dict,
        stage_is_str,
        run_id_is_str,
        hash_is_str,
        produced_ok,
        extra_ok,
    )
}

/// Law D2 — the `validate_stage_stamp` verdict matrix: `"ok"` / `"hash_mismatch"`.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (recomputed, content_hash, stage, expected_stage, run_id, expected_run_id, stamped_snapshot, expected_snapshot))]
fn stamp_verdict(
    recomputed: &str,
    content_hash: &str,
    stage: &str,
    expected_stage: &str,
    run_id: &str,
    expected_run_id: Option<&str>,
    stamped_snapshot: Option<&str>,
    expected_snapshot: Option<&str>,
) -> &'static str {
    stamp::stamp_verdict(
        recomputed,
        content_hash,
        stage,
        expected_stage,
        run_id,
        expected_run_id,
        stamped_snapshot,
        expected_snapshot,
    )
}

#[pymodule]
fn _brutex_observability(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    // THIS wheel's own panic class (a per-extension-module static — module
    // docs): the facade's panic belt resolves the EXACT class from here,
    // because neither name-based home (`pyo3_runtime`, another extension's
    // `PanicException`) can ever be this wheel's class. Part of the shim's
    // `_EXPECTED_COMPILED_SYMBOLS` + the facade's `_CRATE_API` all-or-nothing
    // handshake — a stale pre-fix wheel missing it latches `crate_stale`.
    m.add(
        "PanicException",
        m.py().get_type::<pyo3::panic::PanicException>(),
    )?;
    // Dunder-named build stamp (sibling convention): invisible to
    // non-underscore symbol-surface checks.
    m.add("__source_digest__", crate::SOURCE_DIGEST)?;
    // Plain-named twin: part of the shim's `_EXPECTED_COMPILED_SYMBOLS`
    // handshake surface (the brutex-feed shape).
    m.add("SOURCE_DIGEST", crate::SOURCE_DIGEST)?;
    m.add_function(wrap_pyfunction!(loop_watchdog_check_once, m)?)?;
    m.add_function(wrap_pyfunction!(watchdog_decide, m)?)?;
    m.add_function(wrap_pyfunction!(sla_decide, m)?)?;
    m.add_function(wrap_pyfunction!(stamp_shape_index, m)?)?;
    m.add_function(wrap_pyfunction!(stamp_verdict, m)?)?;
    Ok(())
}
