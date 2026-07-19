//! PyO3 binding (feature `python`) — the thin primitive-mapping edge.
//!
//! Every `#[pyfunction]` delegates to the pure core (`crate::backup`) and
//! maps its outcome to plain Python primitives (ints, bools, floats,
//! strings, `None`). No datetime, no exception text, no path object ever
//! crosses Rust → Python — the facade (`brutex/backup/backup_rust.py`)
//! materialises every `DeadmanReport` detail / `SnapshotInfo` datetime /
//! `BackupError` itself with the verbatim existing Python expressions
//! (the `DEC-FEED-RUST-CORE-001` structural bit-identity discipline).

use pyo3::prelude::*;
use pyo3::wrap_pyfunction;

use crate::backup;

/// Crate version baked at compile time (`CARGO_PKG_VERSION`).
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// The dead-man verdict decision — see `backup::evaluate_deadman_decision`.
///
/// Returns `(arm, verdict, should_alert, hours_since_success, elapsed)`.
/// Every `Option<f64>` input is the corresponding
/// `(now − stamp).total_seconds()` computed PYTHON-side (exact — the
/// oracle's own datetime arithmetic); pyo3 crosses `f64` bit-exactly.
#[pyfunction]
#[pyo3(signature = (
    state_error_present,
    state_present,
    now_minus_success_s,
    now_minus_running_s,
    now_minus_alert_s,
    threshold_hours,
    realert_hours,
    skew_tolerance_s,
    max_runtime_hours,
))]
#[allow(clippy::too_many_arguments)]
fn evaluate_deadman_decision(
    state_error_present: bool,
    state_present: bool,
    now_minus_success_s: Option<f64>,
    now_minus_running_s: Option<f64>,
    now_minus_alert_s: Option<f64>,
    threshold_hours: f64,
    realert_hours: f64,
    skew_tolerance_s: f64,
    max_runtime_hours: f64,
) -> (u8, &'static str, bool, Option<f64>, f64) {
    let d = backup::evaluate_deadman_decision(
        state_error_present,
        state_present,
        now_minus_success_s,
        now_minus_running_s,
        now_minus_alert_s,
        threshold_hours,
        realert_hours,
        skew_tolerance_s,
        max_runtime_hours,
    );
    (
        d.arm,
        d.verdict,
        d.should_alert,
        d.hours_since_success,
        d.elapsed,
    )
}

/// `schedule_render.render_backup_plist` twin (byte-exact).
#[pyfunction]
fn render_backup_plist() -> String {
    backup::render_backup_plist()
}

/// `schedule_render.render_deadman_plist` twin (byte-exact).
#[pyfunction]
fn render_deadman_plist() -> String {
    backup::render_deadman_plist()
}

/// `schedule_render.render_drill_plist` twin (byte-exact).
#[pyfunction]
fn render_drill_plist() -> String {
    backup::render_drill_plist()
}

/// `schedule_render.render_cron_lines` twin (byte-exact).
#[pyfunction]
fn render_cron_lines() -> String {
    backup::render_cron_lines()
}

/// `restic_runner.parse_backup_summary_snapshot_id` twin —
/// `(1, id)` found | `(0, None)` oracle (abstain / raise-shaped).
#[pyfunction]
fn parse_backup_summary_snapshot_id(stdout: &str) -> (u8, Option<String>) {
    backup::parse_backup_summary_snapshot_id(stdout)
}

/// `restic_runner.parse_snapshots_latest` decision twin —
/// `(1, None, None)` empty | `(2, id, time_raw)` ok | `(0, None, None)` oracle.
#[pyfunction]
fn parse_snapshots_latest_decision(stdout: &str) -> (u8, Option<String>, Option<String>) {
    backup::parse_snapshots_latest_decision(stdout)
}

/// `restic_runner.parse_ls_file_paths` twin — the FILE node paths, or
/// `None` (abstain → the Python oracle).
#[pyfunction]
fn parse_ls_file_paths(stdout: &str) -> Option<Vec<String>> {
    backup::parse_ls_file_paths(stdout)
}

/// `restic_runner.parse_ls_snapshot_time` decision twin —
/// `(0, None)` abstain | `(1, None)` no header | `(2, None)` header with a
/// non-string time (→ Python `None`) | `(3, time_raw)`.
#[pyfunction]
fn parse_ls_snapshot_time_decision(stdout: &str) -> (u8, Option<String>) {
    backup::parse_ls_snapshot_time_decision(stdout)
}

/// `restic_runner.build_backup_argv` twin — the exact `restic backup` argv
/// (paths pre-rendered `str(p)` Python-side).
#[pyfunction]
fn build_backup_argv(
    binary: &str,
    paths: Vec<String>,
    excludes: Vec<String>,
    tags: Vec<String>,
) -> Vec<String> {
    backup::build_backup_argv(binary, &paths, &excludes, &tags)
}

#[pymodule]
fn _brutex_ops(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", version())?;
    m.add("SOURCE_DIGEST", crate::SOURCE_DIGEST)?;
    m.add("__source_digest__", crate::SOURCE_DIGEST)?;
    m.add_function(wrap_pyfunction!(evaluate_deadman_decision, m)?)?;
    m.add_function(wrap_pyfunction!(render_backup_plist, m)?)?;
    m.add_function(wrap_pyfunction!(render_deadman_plist, m)?)?;
    m.add_function(wrap_pyfunction!(render_drill_plist, m)?)?;
    m.add_function(wrap_pyfunction!(render_cron_lines, m)?)?;
    m.add_function(wrap_pyfunction!(parse_backup_summary_snapshot_id, m)?)?;
    m.add_function(wrap_pyfunction!(parse_snapshots_latest_decision, m)?)?;
    m.add_function(wrap_pyfunction!(parse_ls_file_paths, m)?)?;
    m.add_function(wrap_pyfunction!(parse_ls_snapshot_time_decision, m)?)?;
    m.add_function(wrap_pyfunction!(build_backup_argv, m)?)?;
    Ok(())
}
