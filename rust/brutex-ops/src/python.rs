//! PyO3 binding (feature `python`) — the thin per-lane primitive-mapping edge.
//!
//! Every `#[pyfunction]` delegates to a pure core (`crate::backup`,
//! `crate::crossverify`, `crate::nightly`) and maps its outcome to plain
//! Python primitives (ints, bools, floats, strings, bytes, tuples, `None`).
//! No datetime, no exception text, no path object ever crosses Rust →
//! Python — each lane's facade (`brutex/backup/backup_rust.py`,
//! `brutex/crossverify/rust_seam.py`, `brutex/nightly/nightly_rust.py`)
//! materialises every report / datetime / error itself with the verbatim
//! existing Python expressions (the `DEC-FEED-RUST-CORE-001` structural
//! bit-identity discipline).
//!
//! ## Lane registration (APPEND-ONLY)
//!
//! One clearly-delimited block per ops lane — backup, crossverify, nightly —
//! both for the `#[pyfunction]` definitions and for the `#[pymodule]`
//! registrations. A future lane APPENDS its own block; it never edits an
//! existing lane's block (the rust/brutex-lake multi-wave precedent).

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyo3::wrap_pyfunction;

use crate::backup;
use crate::crossverify;
use crate::nightly::{
    INPUT_REFUSED_MARKER, audit_reconcile_detail_impl, count_text_impl, exit_code_impl,
    first_breaker_index_impl, parse_at_impl, pmset_wake_command_impl, render_launchd_plist_impl,
    seconds_since_fire_impl, summary_lines_impl, verdict_impl, xml_escape_impl,
};

/// Crate version baked at compile time (`CARGO_PKG_VERSION`).
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

// ─────────────────── backup lane (APPEND-ONLY block) ───────────────────
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

// ────────────────── end backup lane ──────────────────

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

// ─────────────────── nightly lane (APPEND-ONLY block) ───────────────────
// The FFI faces moved VERBATIM from src/nightly.rs at the unification (so
// nightly.rs stays pure Rust and the whole PyO3 surface is feature-gated
// here); only the `use` resolution changed (`crate::nightly::*_impl`).

/// Stamp a nightly core's `Err(String)` as a MARKED envelope refusal.
///
/// Applied ONLY to the four cores whose every `Err` arm is an envelope
/// refusal by construction (`summary_lines_impl` · `parse_at_impl` ·
/// `pmset_wake_command_impl` · `seconds_since_fire_impl` — each has exactly
/// one `Err` site, all input-shaped). `exit_code_impl` and
/// `audit_reconcile_detail_impl` deliberately keep the plain
/// `PyValueError::new_err` mapping: their only `Err` is a
/// statuses/hints length mismatch their own doc-comments call *"a facade
/// bug — refuse loudly"*, which is CLASS A and MUST refuse.
fn nightly_input_refusal(detail: String) -> PyErr {
    PyValueError::new_err(format!("{INPUT_REFUSED_MARKER}: {detail}"))
}

/// Re-stamp a PyO3 extraction failure as a MARKED input-envelope refusal.
///
/// Applied to the argument extractions whose Python-side domain is genuinely
/// WIDER than the crate's — `parse_at`'s free-text `--at` (a non-UTF-8 argv
/// byte reaches Python as a lone surrogate under `surrogateescape`, which
/// `String` extraction rejects while the Python oracle answers) and the
/// unbounded-`int` schedule parameters (`pmset_wake_command` /
/// `seconds_since_scheduled_fire` take plain `int`, so a value past `i64`
/// is a legitimate call the Python oracle serves). The exception CLASS
/// collapses to `TypeError` (when the original was one) or `ValueError`
/// otherwise — both are facade degrade-set members, so routing is
/// unchanged; only the marker (and hence the strict-mode class) is added.
///
/// Deliberately NOT applied to `nightly_summary_lines`: every one of its
/// nine arguments is minted INSIDE the chain from typed values (`StrEnum`
/// `.value` tokens, bounded `int | None` counts, the `float` run stamps,
/// the internally-formatted id / notes), so an extraction failure there is
/// a facade-contract bug — CLASS A, and it must refuse. That asymmetry is
/// the honest reading of the two argument populations, not an oversight.
fn nightly_mark_extract(py: Python<'_>, context: &str, err: &PyErr) -> PyErr {
    let detail = err.value(py).str().map_or_else(
        |_| "<unprintable>".to_owned(),
        |s| s.to_string_lossy().into_owned(),
    );
    let message = format!("{context} — {INPUT_REFUSED_MARKER}: {detail}");
    if err.is_instance_of::<PyTypeError>(py) {
        PyTypeError::new_err(message)
    } else {
        PyValueError::new_err(message)
    }
}

/// Extract one MARKED `i64` schedule component (the helper above, applied).
fn nightly_extract_i64(py: Python<'_>, context: &str, value: &Bound<'_, PyAny>) -> PyResult<i64> {
    value
        .extract::<i64>()
        .map_err(|err| nightly_mark_extract(py, context, &err))
}

/// FFI face of [`first_breaker_index_impl`].
#[pyfunction]
#[pyo3(signature = (statuses))]
fn nightly_first_breaker_index(statuses: Vec<String>) -> Option<usize> {
    first_breaker_index_impl(&statuses)
}

/// FFI face of [`exit_code_impl`].
#[pyfunction]
#[pyo3(signature = (statuses, exit_hints, has_tripwires))]
fn nightly_exit_code(
    statuses: Vec<String>,
    exit_hints: Vec<Option<i64>>,
    has_tripwires: bool,
) -> PyResult<i64> {
    exit_code_impl(&statuses, &exit_hints, has_tripwires).map_err(PyValueError::new_err)
}

/// FFI face of [`audit_reconcile_detail_impl`].
#[pyfunction]
#[pyo3(signature = (
    nightly_id, events, outcome_links, outcome_attempted,
    link_starts_repr, attempted_sorted_repr, terminal_repr, declared_repr
))]
#[allow(clippy::too_many_arguments)]
fn nightly_audit_reconcile_detail(
    nightly_id: &str,
    events: Vec<(String, Option<String>)>,
    outcome_links: Vec<String>,
    outcome_attempted: Vec<bool>,
    link_starts_repr: &str,
    attempted_sorted_repr: &str,
    terminal_repr: &str,
    declared_repr: &str,
) -> PyResult<Option<String>> {
    audit_reconcile_detail_impl(
        nightly_id,
        &events,
        &outcome_links,
        &outcome_attempted,
        link_starts_repr,
        attempted_sorted_repr,
        terminal_repr,
        declared_repr,
    )
    .map_err(PyValueError::new_err)
}

/// FFI face of [`count_text_impl`].
#[pyfunction]
#[pyo3(signature = (value))]
fn nightly_count_text(value: Option<i64>) -> String {
    count_text_impl(value)
}

/// FFI face of [`verdict_impl`].
#[pyfunction]
#[pyo3(signature = (statuses, suspect))]
fn nightly_verdict(statuses: Vec<String>, suspect: bool) -> &'static str {
    verdict_impl(&statuses, suspect)
}

/// FFI face of [`summary_lines_impl`].
///
/// The core's ONE `Err` arm is the out-of-`f64`-exact-range / non-finite
/// duration — an envelope refusal, so it is MARKED
/// ([`INPUT_REFUSED_MARKER`]). Argument extraction is left to the
/// signature (unmarked → CLASS A) for the reason documented on
/// [`nightly_mark_extract`].
#[pyfunction]
#[pyo3(signature = (
    nightly_id, started_utc, ended_utc, links, counts,
    tripwires, honesty_notes, dry_run, suspect
))]
#[allow(clippy::too_many_arguments)]
fn nightly_summary_lines(
    nightly_id: &str,
    started_utc: f64,
    ended_utc: f64,
    links: Vec<(String, String, Option<String>)>,
    counts: Vec<(String, Option<i64>)>,
    tripwires: Vec<(String, String)>,
    honesty_notes: Vec<String>,
    dry_run: bool,
    suspect: bool,
) -> PyResult<(Vec<String>, String)> {
    summary_lines_impl(
        nightly_id,
        started_utc,
        ended_utc,
        &links,
        &counts,
        &tripwires,
        &honesty_notes,
        dry_run,
        suspect,
    )
    .map_err(nightly_input_refusal)
}

/// FFI face of [`xml_escape_impl`].
#[pyfunction]
#[pyo3(signature = (text))]
fn nightly_xml_escape(text: &str) -> String {
    xml_escape_impl(text)
}

/// FFI face of [`render_launchd_plist_impl`].
#[pyfunction]
#[pyo3(signature = (label, program_arguments, hour, minute, env_vars, out_log, err_log))]
#[allow(clippy::too_many_arguments)]
fn nightly_render_launchd_plist(
    label: &str,
    program_arguments: Vec<String>,
    hour: i64,
    minute: i64,
    env_vars: Vec<(String, String)>,
    out_log: &str,
    err_log: &str,
) -> String {
    render_launchd_plist_impl(
        label,
        &program_arguments,
        hour,
        minute,
        &env_vars,
        out_log,
        err_log,
    )
}

/// FFI face of [`parse_at_impl`] — `None` = clean no-match (the facade
/// raises Python's exact ValueError with the `{text!r}` repr Python-side).
///
/// BOTH refusal paths are MARKED envelope refusals: the core's non-ASCII
/// `Err` and the `String` extraction (a lone-surrogate `--at`, which argv
/// carries whenever a non-UTF-8 byte is `surrogateescape`-decoded and which
/// the Python oracle answers with its own exact ValueError).
#[pyfunction]
#[pyo3(signature = (text))]
fn nightly_parse_at(py: Python<'_>, text: &Bound<'_, PyAny>) -> PyResult<Option<(i64, i64)>> {
    let owned: String = text
        .extract()
        .map_err(|err| nightly_mark_extract(py, "nightly_parse_at: text", &err))?;
    parse_at_impl(&owned).map_err(nightly_input_refusal)
}

/// FFI face of [`pmset_wake_command_impl`].
///
/// The public Python law takes plain unbounded `int`s, so BOTH the `i64`
/// extraction and the core's `checked_*` overflow `Err` are MARKED envelope
/// refusals — the Python oracle computes either one natively.
#[pyfunction]
#[pyo3(signature = (hour, minute, lead_minutes))]
fn nightly_pmset_wake_command(
    py: Python<'_>,
    hour: &Bound<'_, PyAny>,
    minute: &Bound<'_, PyAny>,
    lead_minutes: &Bound<'_, PyAny>,
) -> PyResult<String> {
    let hour = nightly_extract_i64(py, "nightly_pmset_wake_command: hour", hour)?;
    let minute = nightly_extract_i64(py, "nightly_pmset_wake_command: minute", minute)?;
    let lead_minutes =
        nightly_extract_i64(py, "nightly_pmset_wake_command: lead_minutes", lead_minutes)?;
    pmset_wake_command_impl(hour, minute, lead_minutes).map_err(nightly_input_refusal)
}

/// FFI face of [`seconds_since_fire_impl`].
///
/// As above: the `hour`/`minute` the caller supplies are plain unbounded
/// `int`s (Python's `datetime.replace` raises its OWN ValueError for an
/// out-of-range one — the genuine law surface), so extraction failures AND
/// the core's component-range `Err` are both MARKED.
#[pyfunction]
#[pyo3(signature = (hour, minute, now_hour, now_minute, now_second, now_microsecond))]
fn nightly_seconds_since_fire(
    py: Python<'_>,
    hour: &Bound<'_, PyAny>,
    minute: &Bound<'_, PyAny>,
    now_hour: &Bound<'_, PyAny>,
    now_minute: &Bound<'_, PyAny>,
    now_second: &Bound<'_, PyAny>,
    now_microsecond: &Bound<'_, PyAny>,
) -> PyResult<f64> {
    let hour = nightly_extract_i64(py, "nightly_seconds_since_fire: hour", hour)?;
    let minute = nightly_extract_i64(py, "nightly_seconds_since_fire: minute", minute)?;
    let now_hour = nightly_extract_i64(py, "nightly_seconds_since_fire: now_hour", now_hour)?;
    let now_minute = nightly_extract_i64(py, "nightly_seconds_since_fire: now_minute", now_minute)?;
    let now_second = nightly_extract_i64(py, "nightly_seconds_since_fire: now_second", now_second)?;
    let now_microsecond = nightly_extract_i64(
        py,
        "nightly_seconds_since_fire: now_microsecond",
        now_microsecond,
    )?;
    seconds_since_fire_impl(
        hour,
        minute,
        now_hour,
        now_minute,
        now_second,
        now_microsecond,
    )
    .map_err(nightly_input_refusal)
}

// ────────────────── end nightly lane (append new lanes BELOW) ──────────────────

#[pymodule]
fn _brutex_ops(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", version())?;
    // Dunder-named build stamp (sibling convention): invisible to
    // non-underscore symbol-surface checks.
    m.add("__source_digest__", crate::SOURCE_DIGEST)?;
    // Plain-named twin (the brutex-feed shape).
    m.add("SOURCE_DIGEST", crate::SOURCE_DIGEST)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    // ── backup lane registrations (APPEND-ONLY block) ──
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
    // ── end backup lane ──
    // ── crossverify lane registrations (APPEND-ONLY block) ──
    m.add_function(wrap_pyfunction!(crossverify_render_csv, m)?)?;
    m.add_function(wrap_pyfunction!(crossverify_canonical_identity, m)?)?;
    m.add_function(wrap_pyfunction!(crossverify_s3_key, m)?)?;
    // ── end crossverify lane ──
    // ── nightly lane registrations (APPEND-ONLY block) ──
    m.add_function(wrap_pyfunction!(nightly_first_breaker_index, m)?)?;
    m.add_function(wrap_pyfunction!(nightly_exit_code, m)?)?;
    m.add_function(wrap_pyfunction!(nightly_audit_reconcile_detail, m)?)?;
    m.add_function(wrap_pyfunction!(nightly_count_text, m)?)?;
    m.add_function(wrap_pyfunction!(nightly_verdict, m)?)?;
    m.add_function(wrap_pyfunction!(nightly_summary_lines, m)?)?;
    m.add_function(wrap_pyfunction!(nightly_xml_escape, m)?)?;
    m.add_function(wrap_pyfunction!(nightly_render_launchd_plist, m)?)?;
    m.add_function(wrap_pyfunction!(nightly_parse_at, m)?)?;
    m.add_function(wrap_pyfunction!(nightly_pmset_wake_command, m)?)?;
    m.add_function(wrap_pyfunction!(nightly_seconds_since_fire, m)?)?;
    // ── end nightly lane (append new lanes BELOW) ──
    Ok(())
}
