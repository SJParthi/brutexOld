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

use crate::artifacts;
use crate::audit_chain;
use crate::leaves;
use crate::ops;
use crate::run_core;
use crate::verify;

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

// ───────────────────── slice 3 — artifacts ─────────────────────

/// `sweep_exit_status` wall-clock + suspect resolution law (see
/// [`artifacts::exit_status_resolution`]). An unknown state token raises
/// `ValueError` (the facade degrades to the oracle) — the Python wiring
/// gates the token against the mirrored `RunState` values, so the raise is
/// a belt, never a live path. Floats cross as f64 BIT-exactly; the JSON
/// rendering (float repr) stays Python.
#[pyfunction]
fn exit_status_resolution(
    state: &str,
    prior_started: Option<f64>,
    now: f64,
    suspect: Option<bool>,
    prior_suspect: Option<bool>,
) -> PyResult<(f64, Option<f64>, bool)> {
    artifacts::exit_status_resolution(state, prior_started, now, suspect, prior_suspect)
        .map_err(pyo3::exceptions::PyValueError::new_err)
}

/// `sweep_quarantine` monotonic quarantine-ordinal fold (see
/// [`artifacts::next_ordinal`]). Non-ASCII input or a u64 ordinal overflow
/// raises `ValueError` (the facade gates non-ASCII to the oracle — Python's
/// `int()` accepts Unicode digits; overflow degrades — Python ints are
/// unbounded).
#[pyfunction]
fn quarantine_next_ordinal(names: Vec<String>, prefix: &str, suffix: &str) -> PyResult<u64> {
    artifacts::next_ordinal(&names, prefix, suffix).map_err(pyo3::exceptions::PyValueError::new_err)
}

/// `sweep_checkpoint` monotonic `record` advance decision (see
/// [`artifacts::checkpoint_record_decision`]).
#[pyfunction]
fn checkpoint_record_decision(
    completed_index: i64,
    last_index: i64,
    total_combos: i64,
) -> &'static str {
    artifacts::checkpoint_record_decision(completed_index, last_index, total_combos)
}

/// `sweep_checkpoint` load-tail adoption verdict (see
/// [`artifacts::checkpoint_adoption_verdict`]).
#[pyfunction]
fn checkpoint_adoption_verdict(
    store_sweep_id: &str,
    store_total: i64,
    cp_sweep_id: &str,
    cp_total: i64,
) -> &'static str {
    artifacts::checkpoint_adoption_verdict(store_sweep_id, store_total, cp_sweep_id, cp_total)
}

// ───────────────────── slice 4 — ops ─────────────────────

/// `sweep_detach.probe_handle`'s liveness cross-check fold (see
/// [`ops::detach_probe_verdict`]). A missing/unknown verdict token under a
/// readable progress payload raises `ValueError` (the facade degrades to
/// the oracle) — the Python wiring gates the token against the mirrored
/// `RunVerdict` values, so the raise is a belt, never a live path.
#[pyfunction]
fn detach_probe_verdict(
    has_progress: bool,
    handle_pid_alive: bool,
    verdict_token: Option<&str>,
    pids_match: bool,
) -> PyResult<&'static str> {
    ops::detach_probe_verdict(has_progress, handle_pid_alive, verdict_token, pids_match)
        .map_err(pyo3::exceptions::PyValueError::new_err)
}

/// `sweep_heal.assess_heal`'s per-entry MISMATCH-TAIL decision (see
/// [`ops::heal_mismatch_decision`]). A triple whose arity is not exactly
/// three raises `ValueError` (the facade gates the shape — a belt).
#[pyfunction]
fn heal_mismatch_decision(
    src: Vec<Option<String>>,
    plan: Vec<Option<String>>,
    dest: Vec<Option<String>>,
) -> PyResult<&'static str> {
    ops::heal_mismatch_decision(&src, &plan, &dest).map_err(pyo3::exceptions::PyValueError::new_err)
}

/// `sweep_scheduler.Schedule.next_fire_after`'s clock arithmetic (see
/// [`ops::schedule_next_fire`]). Floats cross as f64 BIT-exactly; the
/// Python wrapper reconstructs the daily arm's `int` return type so the
/// `cycle_audit_payload` JSON bytes stay identical either arm.
#[pyfunction]
fn schedule_next_fire(
    is_interval: bool,
    interval_seconds: i64,
    second_of_day: i64,
    tz_offset_seconds: i64,
    now: f64,
    last_fire: Option<f64>,
) -> PyResult<f64> {
    ops::schedule_next_fire(
        is_interval,
        interval_seconds,
        second_of_day,
        tz_offset_seconds,
        now,
        last_fire,
    )
    .map_err(pyo3::exceptions::PyValueError::new_err)
}

/// `sweep_scheduler.decide_resume`'s pure classification (see
/// [`ops::resume_decision`]) — `(token, resume_from_index)`; the REASON
/// strings are rendered Python-side from the token.
#[pyfunction]
fn resume_decision(
    exists: bool,
    sweep_id_matches: bool,
    next_index: i64,
    total_combos: i64,
) -> (&'static str, i64) {
    ops::resume_decision(exists, sweep_id_matches, next_index, total_combos)
}

// ───────────────────── slice 5 — verify/apriori ─────────────────────

/// `sweep_verify.compare_recount`'s post-byte-diff classification tail
/// (see [`verify::recount_verdict`]). Total — booleans in, a token out;
/// the finding code/severity/detail BYTES and the corrupt-leaderboard
/// side effect stay Python, keyed off the token.
#[pyfunction]
fn recount_verdict(
    vix_attributable: bool,
    is_grid_tuple: bool,
    context_match: bool,
    cell_evidence: bool,
    grid_corrupt_leaderboard: bool,
) -> &'static str {
    verify::recount_verdict(
        vix_attributable,
        is_grid_tuple,
        context_match,
        cell_evidence,
        grid_corrupt_leaderboard,
    )
}

/// `sweep_apriori._join_and_prune`'s level fold with the strided budget
/// trip returned as a VALUE (see [`verify::apriori_join_prune`]) — a trip
/// is a semantic outcome, never an FFI error the facade would degrade
/// away; Python raises its own `SweepAprioriBudgetError` bytes from the
/// returned count. Malformed frontier shapes raise `ValueError` (the
/// facade gates them — a belt, never a live path).
#[pyfunction]
fn apriori_join_prune(
    frontier: Vec<Vec<u16>>,
    drop_conflicts: bool,
    conflict_masks: Vec<u128>,
    n_bars: u64,
    limit: u64,
    stride: u64,
) -> PyResult<(Vec<Vec<u16>>, Option<u64>)> {
    // u16 at the FFI face (a pyo3 `Vec<u8>` output would render as Python
    // `bytes`, not `list[int]`); range-gate BEFORE the u8 narrowing so an
    // out-of-range bit can never truncate silently.
    let ceiling = u16::from(verify::MASK_BITS);
    let mut frontier_u8: Vec<Vec<u8>> = Vec::with_capacity(frontier.len());
    for tuple in &frontier {
        let mut row: Vec<u8> = Vec::with_capacity(tuple.len());
        for &bit in tuple {
            if bit >= ceiling {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "frontier bit {bit} out of range [0, {ceiling})"
                )));
            }
            row.push(bit as u8);
        }
        frontier_u8.push(row);
    }
    let (candidates, tripped) = verify::apriori_join_prune(
        &frontier_u8,
        drop_conflicts,
        &conflict_masks,
        n_bars,
        limit,
        stride,
    )
    .map_err(pyo3::exceptions::PyValueError::new_err)?;
    let out: Vec<Vec<u16>> = candidates
        .into_iter()
        .map(|candidate| candidate.into_iter().map(u16::from).collect())
        .collect();
    Ok((out, tripped))
}

// ───────────────────── slice 6 — run-core/orchestrator ─────────────────────

/// `sweep_run_core.tuple_record_stride`'s D-9 decimation fold (see
/// [`run_core::tuple_record_stride`]). f64-mirrored CPython true-division
/// arithmetic; the facade gates inputs to the shared exact band |v| <= 2^50.
#[pyfunction]
fn tuple_record_stride(total_tuples: i64) -> u64 {
    run_core::tuple_record_stride(total_tuples)
}

/// `sweep_run_core.per_tuple_diffcheck_budget`'s R45 K-distribution fold
/// (see [`run_core::per_tuple_diffcheck_budget`]).
#[pyfunction]
fn per_tuple_diffcheck_budget(k_per_run: i64, total_tuples: i64) -> u64 {
    run_core::per_tuple_diffcheck_budget(k_per_run, total_tuples)
}

/// `sweep_orchestrator.count_sweep_tuples`'s outer-product fold over
/// PRE-EXTRACTED axis cardinalities (see [`run_core::grid_tuple_count`]).
/// A product past u64 raises `ValueError` — the facade degrades to the
/// Python bigint oracle (never a panic, never a truncation).
#[pyfunction]
fn grid_tuple_count(axes: Vec<u64>) -> PyResult<u64> {
    run_core::grid_tuple_count(&axes).map_err(pyo3::exceptions::PyValueError::new_err)
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
    // slice 3 — artifacts
    m.add("RUN_STATE_TOKENS", artifacts::RUN_STATE_TOKENS)?;
    m.add_function(wrap_pyfunction!(exit_status_resolution, m)?)?;
    m.add_function(wrap_pyfunction!(quarantine_next_ordinal, m)?)?;
    m.add_function(wrap_pyfunction!(checkpoint_record_decision, m)?)?;
    m.add_function(wrap_pyfunction!(checkpoint_adoption_verdict, m)?)?;
    // slice 4 — ops
    m.add("RUN_VERDICT_TOKENS", ops::RUN_VERDICT_TOKENS)?;
    m.add_function(wrap_pyfunction!(detach_probe_verdict, m)?)?;
    m.add_function(wrap_pyfunction!(heal_mismatch_decision, m)?)?;
    m.add_function(wrap_pyfunction!(schedule_next_fire, m)?)?;
    m.add_function(wrap_pyfunction!(resume_decision, m)?)?;
    // slice 5 — verify/apriori
    m.add("RECOUNT_VERDICT_TOKENS", verify::RECOUNT_VERDICT_TOKENS)?;
    m.add_function(wrap_pyfunction!(recount_verdict, m)?)?;
    m.add_function(wrap_pyfunction!(apriori_join_prune, m)?)?;
    // slice 6 — run-core/orchestrator
    m.add_function(wrap_pyfunction!(tuple_record_stride, m)?)?;
    m.add_function(wrap_pyfunction!(per_tuple_diffcheck_budget, m)?)?;
    m.add_function(wrap_pyfunction!(grid_tuple_count, m)?)?;
    // slice 7+ registration blocks append below this line, one fenced
    // block per slice (see the module docstring's registration law).
    Ok(())
}
