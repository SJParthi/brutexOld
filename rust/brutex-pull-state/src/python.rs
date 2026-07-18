//! PyO3 binding (feature `python`) — the thin integer-mapping edge.
//!
//! ONE `#[pyfunction]` ([`pull_allowed_us`]) delegates to the pure core
//! (`crate::market_hours::pull_allowed_us`) and maps its outcome to a plain
//! Python `bool`. INTEGERS ONLY cross Rust → Python input (the caller's
//! microsecond-of-day + the trading-day flag); no float, no string, no
//! timestamp object ever crosses (the DEC-FEED-RUST-CORE-001 discipline —
//! guard verdicts stay byte-identical by construction).
//!
//! Panic belt: the core is TOTAL over its input domain (the out-of-range arm
//! returns `Err`, mapped to `ValueError` here — the Python facade's
//! defensive `except Exception` belt degrades such a call to the oracle);
//! `2**63`-and-beyond inputs raise `OverflowError` at the pyo3 `i64` edge,
//! never a silent wrap (the brutex-feed FFI probe precedent).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::wrap_pyfunction;

use crate::auto_pull_gate;
use crate::market_hours;
use crate::pull_outcome;

/// Crate version baked at compile time (`CARGO_PKG_VERSION`).
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// `True` iff a vendor pull is allowed at the given IST instant.
///
/// See `crate::market_hours::pull_allowed_us` for the full contract. An
/// out-of-range `us_of_day` raises `ValueError` (the Python seam's
/// defensive belt catches it and degrades to the oracle).
#[pyfunction]
#[pyo3(signature = (us_of_day, is_trading_day))]
fn pull_allowed_us(us_of_day: i64, is_trading_day: bool) -> PyResult<bool> {
    market_hours::pull_allowed_us(us_of_day, is_trading_day).map_err(PyValueError::new_err)
}

// --- pull_outcome (Wave 1A, DEC-RUST-PULL-STATE-001) ---------------------

/// The last-pull outcome DECISION codes `(outcome, tone, headline_code,
/// detail_code)` — machine tokens only; Python composes every human string
/// and keeps the `verdict_banner` colour authority (the `@verdict` /
/// `@verdict_prepull` markers hand the banner fold back to Python).
///
/// Total over every input: strings cross as UTF-8, `failed` as `i64` (an
/// out-of-`i64` int raises `OverflowError` at the pyo3 edge — the facade's
/// defensive belt degrades such a call to the oracle).
#[pyfunction]
#[pyo3(signature = (state, detail, failed, stalled, wedged, has_verdict))]
fn classify_pull_outcome(
    state: &str,
    detail: &str,
    failed: i64,
    stalled: bool,
    wedged: bool,
    has_verdict: bool,
) -> (String, String, String, String) {
    pull_outcome::classify_pull_outcome(state, detail, failed, stalled, wedged, has_verdict)
}

/// The run-id correlation law of `classify_retained_pull_outcome`: `True`
/// iff the retained snapshot's NON-empty run id equals the verdict holder's
/// stamped id (see `crate::pull_outcome::verdict_correlates`).
#[pyfunction]
#[pyo3(signature = (retained_id, verdict_id))]
fn verdict_correlates(retained_id: &str, verdict_id: &str) -> bool {
    pull_outcome::verdict_correlates(retained_id, verdict_id)
}

// ── pull_scope (Wave 1B) — note-surgery + scope-token laws ───────────────────
//
// Strings cross this edge (the ONE sanctioned exception to the integers-only
// discipline — the outputs of these laws ARE strings, so byte-exactness IS
// the contract; every literal is drift-pinned by the committed golden + the
// Python-side cross-language source pin). The booleans are the seam's
// `note_surgery_miss` WARN inputs; the WARN itself stays Python.

/// `normalize_pull_scope` verdict code (0=full · 1=spot-only · 2=fno-only ·
/// 255=unknown — Python composes the `{token!r}` refusal itself).
#[pyfunction]
#[pyo3(signature = (token))]
fn scope_token(token: Option<&str>) -> u8 {
    crate::pull_scope::scope_token(token)
}

/// The market-hours deferral-note rewrite (both-arms wording; first
/// occurrence each; drifted notes pass through unchanged).
#[pyfunction]
#[pyo3(signature = (note, phases_replacement))]
fn market_hours_note_rewrite(note: &str, phases_replacement: &str) -> String {
    crate::pull_scope::market_hours_note_rewrite(note, phases_replacement)
}

/// `(rewritten_note, prefix_present, clause_present)` — the spot-only
/// completion-note surgery MINUS the Python-side WARN + step-3 appends.
#[pyfunction]
#[pyo3(signature = (base_note))]
fn spot_only_note_surgery(base_note: &str) -> (String, bool, bool) {
    crate::pull_scope::spot_only_note_surgery(base_note)
}

/// `(rewritten_note, prefix_present, clause_present)` — the fno-only mirror.
#[pyfunction]
#[pyo3(signature = (base_note))]
fn fno_only_note_surgery(base_note: &str) -> (String, bool, bool) {
    crate::pull_scope::fno_only_note_surgery(base_note)
}

// --- pull_stall (Wave 1C) ---------------------------------------------------
// The wedge/alive decision plane of `brutex/dashboard/pull_stall.py`
// (DEC-PULL-WEDGE-ESCALATE-001 + DEC-PULL-TELEMETRY-TRIAD-001). Pure +
// clockless: the caller reads its monotonic clock ONCE and passes ages;
// only bools / f64s / short revision-token strings cross the FFI in, one
// small int (the arm token) or bool crosses out. Both cores are TOTAL over
// the full f64 plane (NaN/inf included — IEEE-faithful strict-`<` guards),
// so no error arm exists; the Python facade's defensive belt stays as
// insurance against a stale/corrupt wheel only.

/// The DUAL-signal wedge-escalation arm token (0..=5) — see
/// `crate::stall::wedge_decision` for the full contract.
#[allow(clippy::too_many_arguments, clippy::fn_params_excessive_bools)] // 1:1 FFI mirror of the oracle's state
#[pyfunction]
#[pyo3(signature = (hung, already_fired, hook_bound, baseline_captured, last_beat_age_s, wedge_window_s, restart_age_s, baseline, current))]
fn wedge_decision(
    hung: bool,
    already_fired: bool,
    hook_bound: bool,
    baseline_captured: bool,
    last_beat_age_s: f64,
    wedge_window_s: f64,
    restart_age_s: Option<f64>,
    baseline: Option<&str>,
    current: Option<&str>,
) -> u8 {
    crate::stall::wedge_decision(
        hung,
        already_fired,
        hook_bound,
        baseline_captured,
        last_beat_age_s,
        wedge_window_s,
        restart_age_s,
        baseline,
        current,
    )
}

/// The throttled alive-beat emit-now? law — see `crate::stall::alive_log_due`.
#[pyfunction]
#[pyo3(signature = (since_last_s, interval_s))]
fn alive_log_due(since_last_s: f64, interval_s: f64) -> bool {
    crate::stall::alive_log_due(since_last_s, interval_s)
}

// --- auto_pull_gate (Wave 2D) -----------------------------------------------
// The --auto gate ladder + Muhurat margined-window + settle-token verdict
// kernels (DEC-PULL-SCOPED-ALL-ONECLICK-001 / DEC-SETTLE-EVIDENCE-ADOPTION-001
// re-hosted). Codes/bools only cross Rust -> Python output; the calendar
// predicates cross IN as caller-resolved flags (brutex/calendars/nse.py stays
// the one calendar source of truth) and times as microsecond-of-day. Every
// out-of-range input maps to ValueError -- the facade's defensive belt
// degrades such a call to the Python oracle.

/// The whole `auto_pull_allowed` decision ladder -> `(reason_code,
/// coverage_warn)` -- see `crate::auto_pull_gate::auto_gate_decision`.
#[pyfunction]
#[pyo3(signature = (trading_day, coverage_available, muhurat_listed, now_us_of_day, settle_us_of_day))]
fn auto_gate_decision(
    trading_day: bool,
    coverage_available: bool,
    muhurat_listed: bool,
    now_us_of_day: i64,
    settle_us_of_day: i64,
) -> PyResult<(u8, bool)> {
    auto_pull_gate::auto_gate_decision(
        trading_day,
        coverage_available,
        muhurat_listed,
        now_us_of_day,
        settle_us_of_day,
    )
    .map_err(PyValueError::new_err)
}

/// The margined Muhurat session-window interval law -- see
/// `crate::auto_pull_gate::muhurat_window_active` (the session LOOKUP stays
/// Python; only a LISTED session's interval ever crosses).
#[pyfunction]
#[pyo3(signature = (now_us_of_day, open_us_of_day, close_us_of_day, pre_margin_us, post_margin_us))]
fn muhurat_window_active(
    now_us_of_day: i64,
    open_us_of_day: i64,
    close_us_of_day: i64,
    pre_margin_us: i64,
    post_margin_us: i64,
) -> PyResult<bool> {
    auto_pull_gate::muhurat_window_active(
        now_us_of_day,
        open_us_of_day,
        close_us_of_day,
        pre_margin_us,
        post_margin_us,
    )
    .map_err(PyValueError::new_err)
}

/// The strict `HH:MM` settle-token parse verdict -> `(verdict_code,
/// us_of_day_or_-1)` -- see `crate::auto_pull_gate::settle_token_verdict`
/// (the token arrives PRE-STRIPPED; Python composes + raises every refusal
/// text itself -- the fail-loud env posture never moves).
#[pyfunction]
#[pyo3(signature = (token, block_end_us))]
fn settle_token_verdict(token: &str, block_end_us: i64) -> PyResult<(u8, i64)> {
    auto_pull_gate::settle_token_verdict(token, block_end_us).map_err(PyValueError::new_err)
}

// --- pull_intent (Wave 2E) --------------------------------------------------
// The pull-intent parse-shape + resume-decision laws of
// `brutex/dashboard/pull_intent.py` (DEC-PULL-INTENT-RESUME-001). Codes only:
// the seam classifies the ALREADY-Python-decoded payload's concrete types
// into small integers (the JSON decode — and CPython's whole failure
// taxonomy — stays Python, so taxonomy parity holds by construction), and
// every warn string / structlog event / the frozen PullIntent dataclass stay
// Python.  The ONE string edge is `intent_resume_enabled`'s raw env token
// (the strip+lower+falsy-set law IS a string law); a lone-surrogate value
// fails the pyo3 `&str` edge and the facade's belt degrades to the oracle.

/// `PullIntent.from_payload`'s strict shape lattice over classified codes —
/// see `crate::intent::shape_verdict` (0 = valid; 1..=8 name the first
/// violation in probe order; 255 = out-of-vocabulary input codes, which the
/// facade maps to `kernel_invalid`).
#[allow(clippy::too_many_arguments)] // 1:1 FFI mirror of the classified payload
#[pyfunction]
#[pyo3(signature = (root_mapping, symbols, symbol, from_date, to_date, scope, restarts_kind, restarts_value, created_at_kind))]
fn intent_shape_verdict(
    root_mapping: bool,
    symbols: u8,
    symbol: u8,
    from_date: u8,
    to_date: u8,
    scope: u8,
    restarts_kind: u8,
    restarts_value: i64,
    created_at_kind: u8,
) -> u8 {
    crate::intent::shape_verdict(
        root_mapping,
        symbols,
        symbol,
        from_date,
        to_date,
        scope,
        restarts_kind,
        restarts_value,
        created_at_kind,
    )
}

/// `run_auto_resume`'s arm token (0=no-intent · 1=peer-active · 2=over-cap ·
/// 3=proceed-to-bump) — the load-bearing probe-BEFORE-cap order lives in
/// `crate::intent::resume_decision`.  An out-of-u64 count raises
/// `OverflowError` at the pyo3 edge (the facade pre-checks representability
/// and keeps such calls on the documented Python arm).
#[pyfunction]
#[pyo3(signature = (has_intent, probe_acquirable, restarts, max_restarts))]
fn intent_resume_decision(
    has_intent: bool,
    probe_acquirable: bool,
    restarts: u64,
    max_restarts: u64,
) -> u8 {
    crate::intent::resume_decision(has_intent, probe_acquirable, restarts, max_restarts)
}

/// The refused-start restore arm (0=no-action · 1=leave-absent ·
/// 2=clear-orphan · 3=restore-prior) — see
/// `crate::intent::refused_restore_decision`.
#[pyfunction]
#[pyo3(signature = (dry_run, started, current_present, prior_present))]
fn intent_refused_restore_decision(
    dry_run: bool,
    started: bool,
    current_present: bool,
    prior_present: bool,
) -> u8 {
    crate::intent::refused_restore_decision(dry_run, started, current_present, prior_present)
}

/// `resolve_pull_intent_resume`'s env-token law (default ON; the stripped,
/// lowercased falsy set disables) — the strip is the CPython `str.isspace`
/// twin, see `crate::intent::resume_enabled`.
#[pyfunction]
#[pyo3(signature = (raw))]
fn intent_resume_enabled(raw: Option<&str>) -> bool {
    crate::intent::resume_enabled(raw)
}

// --- pull_progress (Wave 2F) ------------------------------------------------
// The derived-metrics plane of `brutex/dashboard/pull_progress.py`
// (DEC-PULL-PROGRESS-HONESTY-001). Counts cross as u64 (facade-gated to the
// spike-proven <= 2**53 exactness domain; the kernel's own domain refusal
// maps to ValueError as defense-in-depth), instants as f64 (the CALLER's
// injected-clock stamps — the kernel is clockless); numbers only cross out.
// Python composes ProgressSnapshot / to_dict / every string, and folds the
// (eta_present, eta) pair back to the oracle's `float | None`.

/// `(percent, elapsed_seconds, units_per_sec, eta_present, eta_seconds)` —
/// see `crate::progress::progress_derived` for the full contract. An
/// out-of-domain count raises `ValueError` (the facade's eligibility gate
/// never sends one; its defensive belt degrades the raise to the oracle).
#[allow(clippy::too_many_arguments)] // 1:1 FFI mirror of the snapshot's derived inputs
#[pyfunction]
#[pyo3(signature = (state_running, open_ended, total, completed, phase_base_completed, updated_at, started_at, phase_started_at))]
fn progress_derived(
    state_running: bool,
    open_ended: bool,
    total: u64,
    completed: u64,
    phase_base_completed: u64,
    updated_at: f64,
    started_at: f64,
    phase_started_at: f64,
) -> PyResult<(f64, f64, f64, bool, f64)> {
    crate::progress::progress_derived(
        state_running,
        open_ended,
        total,
        completed,
        phase_base_completed,
        updated_at,
        started_at,
        phase_started_at,
    )
    .map_err(|e| PyValueError::new_err(e.to_string()))
}

#[pymodule]
fn _brutex_pull_state(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", version())?;
    // Dunder-named build stamp (sibling convention): invisible to
    // non-underscore symbol-surface checks.
    m.add("__source_digest__", crate::SOURCE_DIGEST)?;
    // Plain-named twin: part of the shim's `_EXPECTED_COMPILED_SYMBOLS`
    // handshake surface (the brutex-feed pattern).
    m.add("SOURCE_DIGEST", crate::SOURCE_DIGEST)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(pull_allowed_us, m)?)?;
    // --- pull_outcome (Wave 1A) ---
    m.add_function(wrap_pyfunction!(classify_pull_outcome, m)?)?;
    m.add_function(wrap_pyfunction!(verdict_correlates, m)?)?;
    // pull_scope (Wave 1B) — append-only registrations.
    m.add_function(wrap_pyfunction!(scope_token, m)?)?;
    m.add_function(wrap_pyfunction!(market_hours_note_rewrite, m)?)?;
    m.add_function(wrap_pyfunction!(spot_only_note_surgery, m)?)?;
    m.add_function(wrap_pyfunction!(fno_only_note_surgery, m)?)?;
    // --- pull_stall (Wave 1C) ---
    m.add_function(wrap_pyfunction!(wedge_decision, m)?)?;
    m.add_function(wrap_pyfunction!(alive_log_due, m)?)?;
    // --- auto_pull_gate (Wave 2D) ---
    m.add_function(wrap_pyfunction!(auto_gate_decision, m)?)?;
    m.add_function(wrap_pyfunction!(muhurat_window_active, m)?)?;
    m.add_function(wrap_pyfunction!(settle_token_verdict, m)?)?;
    // --- pull_intent (Wave 2E) ---
    m.add_function(wrap_pyfunction!(intent_shape_verdict, m)?)?;
    m.add_function(wrap_pyfunction!(intent_resume_decision, m)?)?;
    m.add_function(wrap_pyfunction!(intent_refused_restore_decision, m)?)?;
    m.add_function(wrap_pyfunction!(intent_resume_enabled, m)?)?;
    // --- pull_progress (Wave 2F) ---
    m.add_function(wrap_pyfunction!(progress_derived, m)?)?;
    Ok(())
}
