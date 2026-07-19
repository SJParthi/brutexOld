//! PyO3 binding (feature `python`) — the thin scalar-mapping edge.
//!
//! Twelve `#[pyfunction]`s (the five A-D detector/stamp laws + the seven
//! T2-T6/T9 tripwire laws of `DEC-RUST-OBSERVABILITY-TRIPWIRES-002`)
//! delegate to the pure core and map its outcomes to plain Python tuples /
//! lists / strings of SCALARS. No datetime, no PyAny, no dicts, no
//! callables cross the boundary — every conversion is documented on the
//! facade side (`brutex/observability/observability_rust.py`).
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

use crate::{detector, stamp, tripwires};

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

/// Law T2 — the count-reconciliation violation bitmask (bit order = the
/// `_reconcile_counts` append order; bit-4 sum in i128 — lossless).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (in_flight, enumerated, evaluated, skipped, survivors, total, prev_evaluated, is_done))]
fn tripwire_reconcile_counts(
    in_flight: i64,
    enumerated: i64,
    evaluated: i64,
    skipped: i64,
    survivors: i64,
    total: i64,
    prev_evaluated: Option<i64>,
    is_done: bool,
) -> u32 {
    tripwires::tripwire_reconcile_counts(
        in_flight,
        enumerated,
        evaluated,
        skipped,
        survivors,
        total,
        prev_evaluated,
        is_done,
    )
}

/// Law T3 — the throughput floor/dwell watch:
/// `(new_peak, new_below, floor, code)`. A dwell-counter overflow maps to
/// `ValueError` (the facade degrade set serves the oracle's unbounded int).
#[pyfunction]
#[pyo3(signature = (rate, peak, below, fraction, dwell))]
fn tripwire_throughput_watch(
    rate: f64,
    peak: f64,
    below: i64,
    fraction: f64,
    dwell: i64,
) -> PyResult<(f64, i64, f64, u8)> {
    tripwires::tripwire_throughput_watch(rate, peak, below, fraction, dwell)
        .map_err(|err| PyValueError::new_err(err.to_string()))
}

/// Law T4a — the RSS budget arm: `(budget, code)`.
#[pyfunction]
#[pyo3(signature = (rss, ram_total, fraction))]
fn tripwire_memory_budget(rss: i64, ram_total: i64, fraction: f64) -> (f64, u8) {
    tripwires::tripwire_memory_budget(rss, ram_total, fraction)
}

/// Law T4b — the RSS growth-slope arm: `(slope, new_high, code)`. A
/// dwell-counter overflow maps to `ValueError` (same degrade route as T3).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (rss, evaluated, prev_rss, prev_evaluated, slope_threshold, slope_high, dwell))]
fn tripwire_memory_slope(
    rss: i64,
    evaluated: i64,
    prev_rss: i64,
    prev_evaluated: i64,
    slope_threshold: f64,
    slope_high: i64,
    dwell: i64,
) -> PyResult<(f64, i64, u8)> {
    tripwires::tripwire_memory_slope(
        rss,
        evaluated,
        prev_rss,
        prev_evaluated,
        slope_threshold,
        slope_high,
        dwell,
    )
    .map_err(|err| PyValueError::new_err(err.to_string()))
}

/// Law T5 — the batch NaN/absurd screen: `(nan_codes, absurd_codes)`,
/// aligned with the input order. A length mismatch maps to `ValueError`
/// (belt — the facade builds both arrays from one iteration). The code
/// vecs cross as `Vec<i64>` (widened from the core's `u8`) because pyo3
/// special-cases `Vec<u8>` into Python `bytes` — the facade's output gate
/// demands exact `list[int]`.
#[pyfunction]
#[pyo3(signature = (values, screened, absurd_threshold))]
fn tripwire_summary_classify(
    values: Vec<f64>,
    screened: Vec<bool>,
    absurd_threshold: f64,
) -> PyResult<(Vec<i64>, Vec<i64>)> {
    tripwires::tripwire_summary_classify(&values, &screened, absurd_threshold)
        .map(|(nan_codes, absurd_codes)| {
            (
                nan_codes.into_iter().map(i64::from).collect(),
                absurd_codes.into_iter().map(i64::from).collect(),
            )
        })
        .map_err(|err| PyValueError::new_err(err.to_string()))
}

/// Law T6 — the stage-prerequisite membership law: the per-token MISSING
/// mask over the Python-pre-sorted required list.
#[pyfunction]
#[pyo3(signature = (required_sorted, present))]
fn tripwire_stage_missing(required_sorted: Vec<String>, present: Vec<String>) -> Vec<bool> {
    tripwires::tripwire_stage_missing(&required_sorted, &present)
}

/// Law T9 — the `ru_maxrss` platform unit law (`darwin` → bytes verbatim,
/// else KiB × 1024; a `checked_mul` overflow maps to `ValueError` — the
/// facade degrades to the oracle's unbounded-int product).
#[pyfunction]
#[pyo3(signature = (ru_maxrss, platform))]
fn tripwire_ru_maxrss_bytes(ru_maxrss: i64, platform: &str) -> PyResult<i64> {
    tripwires::normalized_ru_maxrss_bytes(ru_maxrss, platform)
        .map_err(|err| PyValueError::new_err(err.to_string()))
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
    m.add_function(wrap_pyfunction!(tripwire_reconcile_counts, m)?)?;
    m.add_function(wrap_pyfunction!(tripwire_throughput_watch, m)?)?;
    m.add_function(wrap_pyfunction!(tripwire_memory_budget, m)?)?;
    m.add_function(wrap_pyfunction!(tripwire_memory_slope, m)?)?;
    m.add_function(wrap_pyfunction!(tripwire_summary_classify, m)?)?;
    m.add_function(wrap_pyfunction!(tripwire_stage_missing, m)?)?;
    m.add_function(wrap_pyfunction!(tripwire_ru_maxrss_bytes, m)?)?;
    Ok(())
}
