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

use crate::audit_chain;
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

// ─────────────────── slice 1b — leaves (deferred batch) ───────────────────

/// `sweep_progress` snapshot derived-metrics cluster (see
/// [`leaves::progress_derived`]).
#[pyfunction]
fn progress_derived(
    enumerated: i64,
    evaluated: i64,
    survivors: i64,
    started_at: f64,
    updated_at: f64,
) -> (f64, f64, f64, Option<f64>, f64) {
    leaves::progress_derived(enumerated, evaluated, survivors, started_at, updated_at)
}

/// `sweep_sla_guard` projection decision (see [`leaves::sla_decision`]).
#[pyfunction]
fn sla_decision(
    tuple_count: i64,
    per_tuple_seconds: f64,
    envelope_seconds: f64,
    acknowledge: bool,
) -> (f64, &'static str) {
    leaves::sla_decision(
        tuple_count,
        per_tuple_seconds,
        envelope_seconds,
        acknowledge,
    )
}

/// `sweep_mem_budget` rank-memory decision table (see
/// [`leaves::rank_plan_decision`]). An unknown *mode* token raises
/// `ValueError` (the facade degrades to the oracle) — the Python wiring
/// gates the token, so the raise is a belt, never a live path.
#[pyfunction]
fn rank_plan_decision(
    n_masks: i64,
    budget_bytes: i64,
    mode: &str,
    has_top_k: bool,
    digest_path: bool,
) -> PyResult<(i128, &'static str)> {
    leaves::rank_plan_decision(n_masks, budget_bytes, mode, has_top_k, digest_path).ok_or_else(
        || {
            pyo3::exceptions::PyValueError::new_err(format!(
                "rank_plan_decision: unknown bounded-rank mode token {mode:?} \
                 (expected one of 'off', 'auto', 'on')"
            ))
        },
    )
}

/// `sweep_preflight` zero-bar pair fold (see [`leaves::preflight_missing`]).
#[pyfunction]
fn preflight_missing(
    sizes: Vec<(String, String, i64)>,
    symbols: Vec<String>,
    intervals: Vec<String>,
) -> Vec<(String, String)> {
    leaves::preflight_missing(&sizes, &symbols, &intervals)
}

/// `sweep_preflight` derived-freshness stale fold (see
/// [`leaves::freshness_stale`]).
#[pyfunction]
fn freshness_stale(verdicts: Vec<(String, String, String)>) -> Vec<(String, String)> {
    leaves::freshness_stale(&verdicts)
}

/// `sweep_liveness` verdict tail (see [`leaves::liveness_tail`]).
#[pyfunction]
fn liveness_tail(pid_ok: bool, now_wall: f64, wall: f64, fresh_seconds: f64) -> &'static str {
    leaves::liveness_tail(pid_ok, now_wall, wall, fresh_seconds)
}

// ───────────────────── slice 2 — audit_chain ─────────────────────

/// `sweep_audit` BLAKE3 entry-hash law (see
/// [`audit_chain::audit_entry_hash`]). Every field arrives ALREADY rendered
/// Python-side (`str(seq)` / the float-repr `str(ts_utc)` / the canonical
/// payload JSON) — Rust only joins + hashes.
#[pyfunction]
fn audit_entry_hash(
    seq: &str,
    prev_hash: &str,
    ts: &str,
    actor: &str,
    action: &str,
    subject: &str,
    payload_json: &str,
) -> String {
    audit_chain::audit_entry_hash(seq, prev_hash, ts, actor, action, subject, payload_json)
}

/// `sweep_audit` per-entry `verify_chain` step (see
/// [`audit_chain::chain_verify_step`]).
#[pyfunction]
#[allow(clippy::too_many_arguments)] // The FFI tuple IS the entry's field list.
fn chain_verify_step(
    expected_seq: i64,
    prev_expected: &str,
    seq: i64,
    prev_hash: &str,
    ts: &str,
    actor: &str,
    action: &str,
    subject: &str,
    payload_json: &str,
    entry_hash: &str,
) -> (&'static str, String, String) {
    audit_chain::chain_verify_step(
        expected_seq,
        prev_expected,
        seq,
        prev_hash,
        ts,
        actor,
        action,
        subject,
        payload_json,
        entry_hash,
    )
}

/// `stage_stamp` validate mismatch decision (see
/// [`audit_chain::stamp_verdict`]).
#[pyfunction]
#[allow(clippy::too_many_arguments)] // The FFI tuple IS the verdict's operand list.
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
    audit_chain::stamp_verdict(
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

/// `chain_map` per-link state ladder (see
/// [`audit_chain::chain_link_state`]). A foreign `last_stage` raises
/// `ValueError` (the facade degrades to the oracle) — the Python wiring
/// gates the name against the mirrored links, so the raise is a belt.
#[pyfunction]
#[allow(clippy::too_many_arguments)] // The FFI tuple IS the ladder's operand list.
fn chain_link_state(
    link: &str,
    idx: i64,
    verdict: Option<&str>,
    running: bool,
    last_stage: Option<&str>,
    terminal: Option<&str>,
    refusal_text: Option<&str>,
    error_type: Option<&str>,
) -> PyResult<(&'static str, String)> {
    audit_chain::chain_link_state(
        link,
        idx,
        verdict,
        running,
        last_stage,
        terminal,
        refusal_text,
        error_type,
    )
    .map_err(pyo3::exceptions::PyValueError::new_err)
}

/// `run_tripwires` D-9 count-reconciliation violations fold (see
/// [`audit_chain::count_reconciliation`]). The one checked arithmetic
/// (`evaluated + in_flight`) raises `ValueError` on `i64` overflow (the
/// facade degrades to the oracle — CPython bigints cannot overflow there).
#[pyfunction]
#[allow(clippy::too_many_arguments)] // The FFI tuple IS the counter list.
fn count_reconciliation(
    total: i64,
    enumerated: i64,
    evaluated: i64,
    skipped: i64,
    survivors: i64,
    in_flight: i64,
    prev_evaluated: Option<i64>,
    is_done: bool,
) -> PyResult<String> {
    audit_chain::count_reconciliation(
        total,
        enumerated,
        evaluated,
        skipped,
        survivors,
        in_flight,
        prev_evaluated,
        is_done,
    )
    .map_err(pyo3::exceptions::PyValueError::new_err)
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
    // slice 1b — leaves (deferred batch)
    m.add_function(wrap_pyfunction!(progress_derived, m)?)?;
    m.add_function(wrap_pyfunction!(sla_decision, m)?)?;
    m.add_function(wrap_pyfunction!(rank_plan_decision, m)?)?;
    m.add_function(wrap_pyfunction!(preflight_missing, m)?)?;
    m.add_function(wrap_pyfunction!(freshness_stale, m)?)?;
    m.add_function(wrap_pyfunction!(liveness_tail, m)?)?;
    // slice 2 — audit_chain
    m.add("CHAIN_LINKS", audit_chain::CHAIN_LINKS)?;
    m.add_function(wrap_pyfunction!(audit_entry_hash, m)?)?;
    m.add_function(wrap_pyfunction!(chain_verify_step, m)?)?;
    m.add_function(wrap_pyfunction!(stamp_verdict, m)?)?;
    m.add_function(wrap_pyfunction!(chain_link_state, m)?)?;
    m.add_function(wrap_pyfunction!(count_reconciliation, m)?)?;
    // slice 3..6 registration blocks append below this line, one fenced
    // block per slice (see the module docstring's registration law).
    Ok(())
}
