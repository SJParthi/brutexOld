//! The pure backup DECISION laws — token-for-token twins of `brutex/backup/`.
//!
//! `DEC-BACKUP-RUST-OPS-001`. Three law families, each mirroring its Python
//! source exactly (the cited function is the LIVE parity oracle the
//! default-OFF `brutex/backup/backup_rust.py` seam compares against):
//!
//! * [`evaluate_deadman_decision`] — the verdict state machine of
//!   `brutex/backup/deadman.py::evaluate_deadman` over PRIMITIVE inputs
//!   (Python pre-computes every `(now − stamp).total_seconds()` the oracle
//!   computes, so each remaining float op here — `/ 3600.0`, negation,
//!   comparisons — is the bit-identical IEEE op the oracle performs).
//!   Detail STRINGS stay Python-side (they embed `isoformat()` stamps and
//!   the `state_error` text): the decision returns the ARM code that names
//!   which detail expression the facade materialises.
//! * [`render_backup_plist`] / [`render_deadman_plist`] /
//!   [`render_drill_plist`] / [`render_cron_lines`] — byte-exact twins of
//!   `brutex/backup/schedule_render.py` (the golden files under
//!   `docs/runbooks/backup-templates/` are the cross-language byte pin —
//!   see `tests/golden_templates.rs`).
//! * [`parse_backup_summary_snapshot_id`] / [`parse_snapshots_latest_decision`]
//!   / [`parse_ls_file_paths`] / [`parse_ls_snapshot_time_decision`] /
//!   [`build_backup_argv`] — the restic `--json` parse/argv twins of
//!   `brutex/backup/restic_runner.py`. ABSTAIN RULE (the brutex-feed
//!   abstain contract): any outcome whose Python spelling embeds a Python
//!   repr / a CPython `json` error message — or any line/document
//!   `serde_json` cannot parse (CPython's `json` accepts `NaN`/`Infinity`
//!   literals and lone surrogates that serde refuses) — returns the ORACLE
//!   code instead of a claim, and the facade serves the Python oracle
//!   verbatim; behavior is byte-identical for EVERY input by construction.

use serde_json::Value;

/// Seconds per hour — the `deadman._SECONDS_PER_HOUR` twin.
pub const SECONDS_PER_HOUR: f64 = 3600.0;

/// The fixed `--host` every backup snapshot carries
/// (`restic_runner.BACKUP_HOST`).
pub const BACKUP_HOST: &str = "brutex";

// ── Dead-man verdict tokens (deadman.DeadmanVerdict values) ────────────────
pub const VERDICT_FRESH: &str = "fresh";
pub const VERDICT_RUNNING: &str = "running";
pub const VERDICT_HUNG: &str = "hung";
pub const VERDICT_OVERDUE: &str = "overdue";
pub const VERDICT_NEVER_SUCCEEDED: &str = "never_succeeded";
pub const VERDICT_STATE_CORRUPT: &str = "state_corrupt";
pub const VERDICT_CLOCK_SKEW: &str = "clock_skew";

// ── Detail ARM codes — each maps 1:1 to ONE `return DeadmanReport(...)`
//    branch of the Python oracle (the facade picks the matching detail
//    f-string; Python materialises the text). ─────────────────────────────
/// `state_error is not None` → STATE_CORRUPT.
pub const ARM_STATE_CORRUPT: u8 = 1;
/// `state is None` → NEVER_SUCCEEDED ("no backup state file …").
pub const ARM_NEVER_NO_FILE: u8 = 2;
/// `running_since` in the future beyond tolerance → CLOCK_SKEW.
pub const ARM_CLOCK_SKEW_RUNNING: u8 = 3;
/// `running_since` set, elapsed < max → RUNNING.
pub const ARM_RUNNING: u8 = 4;
/// `running_since` set, elapsed ≥ max → HUNG.
pub const ARM_HUNG: u8 = 5;
/// state present, no success stamp → NEVER_SUCCEEDED ("… no success ever stamped").
pub const ARM_NEVER_NO_STAMP: u8 = 6;
/// `last_success_at` in the future beyond tolerance → CLOCK_SKEW.
pub const ARM_CLOCK_SKEW_SUCCESS: u8 = 7;
/// elapsed > threshold → OVERDUE.
pub const ARM_OVERDUE: u8 = 8;
/// elapsed ≤ threshold → FRESH.
pub const ARM_FRESH: u8 = 9;

/// One evaluated dead-man decision (the primitive plane of
/// `deadman.DeadmanReport` — the facade adds the detail string).
#[derive(Debug, Clone, PartialEq)]
pub struct DeadmanDecision {
    /// The detail-arm code (`ARM_*`).
    pub arm: u8,
    /// The verdict token (`deadman.DeadmanVerdict` value).
    pub verdict: &'static str,
    /// `verdict ∈ ALARM_VERDICTS` AND outside the re-alert window.
    pub should_alert: bool,
    /// `(now − last_success_at)` in hours (`None` = never stamped).
    pub hours_since_success: Option<f64>,
    /// `max(0.0, hours)` — meaningful for the OVERDUE/FRESH arms only
    /// (`0.0` elsewhere; the oracle computes it only on that path).
    pub elapsed: f64,
}

/// The `deadman._should_alert` twin over primitive inputs.
///
/// `now_minus_alert_s` is `(now − sidecar.last_overdue_alert_at)
/// .total_seconds()` computed PYTHON-side (`None` = no prior alert). A
/// NEGATIVE value is the oracle's `last > now` future-stamp arm (sign is
/// exact: the datetime compare is integer-µs, and `total_seconds()` of a
/// non-zero µs delta is a non-zero f64 of the same sign).
#[must_use]
pub fn should_alert_decision(
    alarm: bool,
    now_minus_alert_s: Option<f64>,
    realert_hours: f64,
) -> bool {
    if !alarm {
        return false;
    }
    match now_minus_alert_s {
        None => true,
        Some(s) => {
            if s < 0.0 {
                // A FUTURE re-alert stamp reads as "no prior alert" —
                // fail-toward-alarm (the oracle's `last > now` arm).
                return true;
            }
            s >= realert_hours * SECONDS_PER_HOUR
        }
    }
}

/// The `deadman.evaluate_deadman` verdict state machine over primitives.
///
/// Evaluation ORDER (each earlier rule wins — the oracle's docstring law):
/// `state_error` → STATE_CORRUPT; no state → NEVER_SUCCEEDED;
/// `running_since` set → CLOCK_SKEW (future) / RUNNING (< max) / HUNG (≥ max);
/// no success stamp → NEVER_SUCCEEDED; future success stamp → CLOCK_SKEW;
/// then elapsed vs `threshold_hours` → OVERDUE / FRESH.
///
/// Every `Option<f64>` input is the corresponding
/// `(now − stamp).total_seconds()` computed PYTHON-side with the oracle's own
/// datetime arithmetic (exact — integer µs → one correctly-rounded division),
/// so the ops left here are bit-identical to the oracle's:
/// `hours = s / 3600.0`; the future-stamp checks are
/// `(stamp − now).total_seconds() > tol` ≡ `-s > tol` (IEEE negation is
/// sign-symmetric, bit-exact); `elapsed = max(0.0, hours)` is mirrored as
/// `if h > 0.0 { h } else { 0.0 }` (CPython `max(0.0, h)` returns the FIRST
/// argument on ties, so `-0.0` and `0.0` both yield `+0.0` — as here).
#[must_use]
#[allow(clippy::too_many_arguments)]
pub fn evaluate_deadman_decision(
    state_error_present: bool,
    state_present: bool,
    now_minus_success_s: Option<f64>,
    now_minus_running_s: Option<f64>,
    now_minus_alert_s: Option<f64>,
    threshold_hours: f64,
    realert_hours: f64,
    skew_tolerance_s: f64,
    max_runtime_hours: f64,
) -> DeadmanDecision {
    let alertable = |alarm: bool| should_alert_decision(alarm, now_minus_alert_s, realert_hours);
    if state_error_present {
        return DeadmanDecision {
            arm: ARM_STATE_CORRUPT,
            verdict: VERDICT_STATE_CORRUPT,
            should_alert: alertable(true),
            hours_since_success: None,
            elapsed: 0.0,
        };
    }
    if !state_present {
        return DeadmanDecision {
            arm: ARM_NEVER_NO_FILE,
            verdict: VERDICT_NEVER_SUCCEEDED,
            should_alert: alertable(true),
            hours_since_success: None,
            elapsed: 0.0,
        };
    }
    // The oracle computes `hours` BEFORE the running check (deadman.py:236).
    let hours = now_minus_success_s.map(|s| s / SECONDS_PER_HOUR);
    if let Some(run_s) = now_minus_running_s {
        let elapsed_run_hours = run_s / SECONDS_PER_HOUR;
        let (arm, verdict) = if -run_s > skew_tolerance_s {
            (ARM_CLOCK_SKEW_RUNNING, VERDICT_CLOCK_SKEW)
        } else if elapsed_run_hours < max_runtime_hours {
            (ARM_RUNNING, VERDICT_RUNNING)
        } else {
            (ARM_HUNG, VERDICT_HUNG)
        };
        // RUNNING deliberately does not page (deadman.ALARM_VERDICTS).
        let alarm = arm != ARM_RUNNING;
        return DeadmanDecision {
            arm,
            verdict,
            should_alert: alertable(alarm),
            hours_since_success: hours,
            elapsed: 0.0,
        };
    }
    let Some(success_s) = now_minus_success_s else {
        return DeadmanDecision {
            arm: ARM_NEVER_NO_STAMP,
            verdict: VERDICT_NEVER_SUCCEEDED,
            should_alert: alertable(true),
            hours_since_success: None,
            elapsed: 0.0,
        };
    };
    if -success_s > skew_tolerance_s {
        return DeadmanDecision {
            arm: ARM_CLOCK_SKEW_SUCCESS,
            verdict: VERDICT_CLOCK_SKEW,
            should_alert: alertable(true),
            hours_since_success: hours,
            elapsed: 0.0,
        };
    }
    let h = success_s / SECONDS_PER_HOUR;
    let elapsed = if h > 0.0 { h } else { 0.0 };
    if elapsed > threshold_hours {
        return DeadmanDecision {
            arm: ARM_OVERDUE,
            verdict: VERDICT_OVERDUE,
            should_alert: alertable(true),
            hours_since_success: hours,
            elapsed,
        };
    }
    DeadmanDecision {
        arm: ARM_FRESH,
        verdict: VERDICT_FRESH,
        should_alert: false,
        hours_since_success: hours,
        elapsed,
    }
}

// ── schedule_render twins (byte-exact — schedule_render.py) ────────────────

/// `schedule_render._GENERATED_NOTE` — byte-for-byte.
const GENERATED_NOTE: &str = "     GENERATED by brutex/backup/schedule_render.py — edit the renderer, not this file\n     (a golden test pins byte-equality). Replace every {{PLACEHOLDER}}; NO secrets here:\n     the restic password rides BRUTEX_BACKUP_PASSWORD_FILE (0600) or _COMMAND; S3/B2 creds\n     ride ~/.brutex/.env, loaded by bootstrap_env() at dispatch.";

/// `schedule_render._KEYCHAIN_ROUTE_NOTE` — byte-for-byte.
const KEYCHAIN_ROUTE_NOTE: &str = "    <!-- keychain _COMMAND route (runbook step 4): set the value below to an EMPTY\n         string (blank reads as unset) or delete this key — never leave the literal\n         placeholder: any non-blank value alongside the .env _COMMAND is the\n         ambiguous-source EXIT_CONFIG refusal (F6). -->";

/// `schedule_render._HEALTHCHECK_URL_NOTE` — byte-for-byte.
const HEALTHCHECK_URL_NOTE: &str = "    <!-- fill AFTER the healthchecks.io check exists (runbook step 10 / M6); until\n         then set the value below to an EMPTY string (blank reads as unset) — never\n         leave the literal placeholder (any non-http(s) value is refused with a WARN\n         every tick while `backup status` reads SET). The hourly checker reads THIS\n         value: launchd env OVERRIDES ~/.brutex/.env, so set the URL here and\n         bootout+bootstrap the agent. A .env-only URL works only if this key is\n         DELETED — a blank value here still reaches the checker as a PRESENT (empty)\n         env var, and bootstrap_env never overrides a present var (blank included),\n         so blank keeps the ping lane OFF while a shell-run `backup status` (no\n         launchd env) reads SET. -->";

/// `schedule_render.render_backup_plist` twin (the format!-escape spelling is
/// the Python f-string body verbatim — `{{{{…}}}}` renders `{{…}}`).
#[must_use]
pub fn render_backup_plist() -> String {
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!-- launchd template — nightly BruteX off-box backup (DEC-BACKUP-OFFBOX-001).\n{GENERATED_NOTE} -->\n<plist version=\"1.0\"><dict>\n  <key>Label</key><string>com.brutex.backup</string>\n  <key>ProgramArguments</key><array>\n    <string>{{{{BRUTEX_VENV_BIN}}}}/brutex</string><string>backup</string><string>run</string>\n  </array>\n  <key>StartCalendarInterval</key><dict>\n    <key>Hour</key><integer>3</integer><key>Minute</key><integer>35</integer>\n  </dict>\n  <key>RunAtLoad</key><false/>\n  <key>ThrottleInterval</key><integer>300</integer>\n  <key>EnvironmentVariables</key><dict>\n    <key>BRUTEX_LAKE_ROOT</key><string>{{{{BRUTEX_LAKE_ROOT}}}}</string>\n    <key>BRUTEX_BACKUP_REPO</key><string>{{{{BRUTEX_BACKUP_REPO}}}}</string>\n{KEYCHAIN_ROUTE_NOTE}\n    <key>BRUTEX_BACKUP_PASSWORD_FILE</key><string>{{{{BRUTEX_BACKUP_PASSWORD_FILE}}}}</string>\n    <key>PATH</key><string>{{{{BRUTEX_VENV_BIN}}}}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>\n  </dict>\n  <key>StandardOutPath</key><string>{{{{BRUTEX_LOG_DIR}}}}/backup.out.log</string>\n  <key>StandardErrorPath</key><string>{{{{BRUTEX_LOG_DIR}}}}/backup.err.log</string>\n</dict></plist>\n"
    )
}

/// `schedule_render.render_deadman_plist` twin.
#[must_use]
pub fn render_deadman_plist() -> String {
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!-- launchd template — hourly BruteX backup dead-man check (DEC-BACKUP-OFFBOX-001).\n{GENERATED_NOTE}\n     Deliberately NO repo env: the shallow check must work when the backup env\n     is broken; the daily deep check self-gates inside the hourly tick. -->\n<plist version=\"1.0\"><dict>\n  <key>Label</key><string>com.brutex.backup-deadman</string>\n  <key>ProgramArguments</key><array>\n    <string>{{{{BRUTEX_VENV_BIN}}}}/brutex</string><string>backup</string><string>check</string>\n  </array>\n  <key>StartInterval</key><integer>3600</integer>\n  <key>RunAtLoad</key><true/>\n  <key>ThrottleInterval</key><integer>300</integer>\n  <key>EnvironmentVariables</key><dict>\n{HEALTHCHECK_URL_NOTE}\n    <key>BRUTEX_BACKUP_HEALTHCHECK_URL</key><string>{{{{BRUTEX_BACKUP_HEALTHCHECK_URL}}}}</string>\n    <key>PATH</key><string>{{{{BRUTEX_VENV_BIN}}}}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>\n  </dict>\n  <key>StandardOutPath</key><string>{{{{BRUTEX_LOG_DIR}}}}/deadman.out.log</string>\n  <key>StandardErrorPath</key><string>{{{{BRUTEX_LOG_DIR}}}}/deadman.err.log</string>\n</dict></plist>\n"
    )
}

/// `schedule_render.render_drill_plist` twin.
#[must_use]
pub fn render_drill_plist() -> String {
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!-- launchd template — monthly BruteX backup restore drill (DEC-BACKUP-OFFBOX-001).\n{GENERATED_NOTE} -->\n<plist version=\"1.0\"><dict>\n  <key>Label</key><string>com.brutex.backup-drill</string>\n  <key>ProgramArguments</key><array>\n    <string>{{{{BRUTEX_VENV_BIN}}}}/brutex</string><string>backup</string><string>restore-drill</string>\n  </array>\n  <key>StartCalendarInterval</key><dict>\n    <key>Day</key><integer>1</integer>\n    <key>Hour</key><integer>13</integer><key>Minute</key><integer>0</integer>\n  </dict>\n  <key>RunAtLoad</key><false/>\n  <key>ThrottleInterval</key><integer>300</integer>\n  <key>EnvironmentVariables</key><dict>\n    <key>BRUTEX_LAKE_ROOT</key><string>{{{{BRUTEX_LAKE_ROOT}}}}</string>\n    <key>BRUTEX_BACKUP_REPO</key><string>{{{{BRUTEX_BACKUP_REPO}}}}</string>\n{KEYCHAIN_ROUTE_NOTE}\n    <key>BRUTEX_BACKUP_PASSWORD_FILE</key><string>{{{{BRUTEX_BACKUP_PASSWORD_FILE}}}}</string>\n    <key>PATH</key><string>{{{{BRUTEX_VENV_BIN}}}}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>\n  </dict>\n  <key>StandardOutPath</key><string>{{{{BRUTEX_LOG_DIR}}}}/drill.out.log</string>\n  <key>StandardErrorPath</key><string>{{{{BRUTEX_LOG_DIR}}}}/drill.err.log</string>\n</dict></plist>\n"
    )
}

/// `schedule_render.render_cron_lines` twin (byte-for-byte).
#[must_use]
pub fn render_cron_lines() -> String {
    concat!(
        "35 3 * * *  BRUTEX_LAKE_ROOT=/path/lake /path/venv/bin/brutex backup run",
        "           >> ~/.brutex/logs/backup.log 2>&1\n",
        "0  * * * *  /path/venv/bin/brutex backup check",
        "                                     >> ~/.brutex/logs/backup.log 2>&1\n",
        "0 13 1 * *  BRUTEX_LAKE_ROOT=/path/lake /path/venv/bin/brutex backup restore-drill",
        " >> ~/.brutex/logs/backup.log 2>&1\n",
    )
    .to_string()
}

// ── restic_runner parse twins ──────────────────────────────────────────────

/// The `restic_runner._json_lines` twin: split `stdout` on `\n` (CPython's
/// `io.StringIO` line iteration under its default `newline="\n"`), trim,
/// skip empties, parse each line, keep the JSON objects.
///
/// Returns `None` (ABSTAIN → the facade serves the Python oracle) when any
/// stripped-non-empty line fails the serde parse: CPython's `json.loads`
/// accepts `NaN`/`Infinity` literals and lone surrogates serde refuses, and
/// Python's `str.strip()` removes chars (`\x1c`–`\x1f`) Rust's `trim` keeps
/// — every such divergence lands HERE and abstains, so a claimed parse can
/// never differ from the oracle. (Rust's `trim` set is a SUBSET of Python's
/// strip set — Unicode `White_Space` ⊆ `str.isspace` — so the reverse
/// direction cannot occur.) Valid-JSON non-object lines are SKIPPED, as the
/// oracle skips non-dicts.
fn json_line_objects(stdout: &str) -> Option<Vec<serde_json::Map<String, Value>>> {
    let mut objects = Vec::new();
    for line in stdout.split('\n') {
        let stripped = line.trim();
        if stripped.is_empty() {
            continue;
        }
        match serde_json::from_str::<Value>(stripped) {
            Ok(Value::Object(map)) => objects.push(map),
            Ok(_) => continue,
            Err(_) => return None,
        }
    }
    Some(objects)
}

/// CPython truthiness of a parsed-JSON value (the `or` in
/// `last.get("short_id") or last.get("id")`).
fn is_py_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_none_or(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `restic_runner.parse_backup_summary_snapshot_id` twin.
///
/// * `(1, Some(id))` — the first `message_type == "summary"` line carrying a
///   non-empty string `snapshot_id` (the oracle's return value);
/// * `(0, None)` — ORACLE: either an abstain (unparseable line) or the
///   no-summary raise arm (`BackupError` with a constant message) — the
///   facade re-runs the Python oracle, which raises the exact exception.
#[must_use]
pub fn parse_backup_summary_snapshot_id(stdout: &str) -> (u8, Option<String>) {
    let Some(objects) = json_line_objects(stdout) else {
        return (0, None);
    };
    for obj in &objects {
        if obj.get("message_type").and_then(Value::as_str) == Some("summary")
            && let Some(Value::String(id)) = obj.get("snapshot_id")
            && !id.is_empty()
        {
            return (1, Some(id.clone()));
        }
    }
    (0, None)
}

/// `restic_runner.parse_snapshots_latest` decision twin.
///
/// * `(1, None, None)` — empty snapshot array → the oracle returns `None`;
/// * `(2, Some(id), Some(time_raw))` — the selected id + the RAW `time`
///   string; Python parses the time via the oracle's own
///   `_parse_snapshot_time` (so unparseable/naive-time `BackupError`s are
///   byte-identical by construction);
/// * `(0, None, None)` — ORACLE: unparseable JSON / not-an-array /
///   entry-not-object / id missing / `time` not a string (each raise arm
///   embeds a CPython repr or `json` error text) or a serde-dialect abstain
///   — the facade re-runs the Python oracle verbatim.
#[must_use]
pub fn parse_snapshots_latest_decision(stdout: &str) -> (u8, Option<String>, Option<String>) {
    const ORACLE: (u8, Option<String>, Option<String>) = (0, None, None);
    let Ok(data) = serde_json::from_str::<Value>(stdout) else {
        return ORACLE;
    };
    let Value::Array(entries) = data else {
        return ORACLE;
    };
    if entries.is_empty() {
        return (1, None, None);
    }
    let Some(Value::Object(last)) = entries.last() else {
        return ORACLE;
    };
    // `last.get("short_id") or last.get("id")` — CPython-truthiness or-chain.
    let null = Value::Null;
    let short_id = last.get("short_id").unwrap_or(&null);
    let chosen = if is_py_truthy(short_id) {
        short_id
    } else {
        last.get("id").unwrap_or(&null)
    };
    let Value::String(snapshot_id) = chosen else {
        return ORACLE;
    };
    if snapshot_id.is_empty() {
        return ORACLE;
    }
    match last.get("time") {
        Some(Value::String(time_raw)) => (2, Some(snapshot_id.clone()), Some(time_raw.clone())),
        _ => ORACLE,
    }
}

/// `restic_runner.parse_ls_file_paths` twin — the FILE node paths
/// (`None` = abstain → oracle; dirs and non-file nodes skipped).
#[must_use]
pub fn parse_ls_file_paths(stdout: &str) -> Option<Vec<String>> {
    let objects = json_line_objects(stdout)?;
    let mut paths = Vec::new();
    for obj in &objects {
        if obj.get("type").and_then(Value::as_str) == Some("file")
            && let Some(Value::String(path)) = obj.get("path")
            && !path.is_empty()
        {
            paths.push(path.clone());
        }
    }
    Some(paths)
}

/// `restic_runner.parse_ls_snapshot_time` decision twin.
///
/// The FIRST object whose `type` reads Python-`None` (absent OR JSON null)
/// while carrying a `time` key decides — matching the oracle's
/// return-inside-the-loop shape:
///
/// * `(0, None)` — ABSTAIN (unparseable line) → oracle;
/// * `(1, None)` — no header object found → the oracle returns `None`;
/// * `(2, None)` — header found, `time` NOT a string → the oracle's
///   `_parse_snapshot_time` raises "not a string", is caught, returns `None`
///   — a CONSTANT outcome the decision encodes directly;
/// * `(3, Some(time_raw))` — header found with a string `time`; Python
///   runs `_parse_snapshot_time` under the oracle's own `except BackupError
///   → None` arm (byte-identical values + fallbacks by construction).
#[must_use]
pub fn parse_ls_snapshot_time_decision(stdout: &str) -> (u8, Option<String>) {
    let Some(objects) = json_line_objects(stdout) else {
        return (0, None);
    };
    for obj in &objects {
        let type_is_none = match obj.get("type") {
            None | Some(Value::Null) => true,
            Some(_) => false,
        };
        if type_is_none && obj.contains_key("time") {
            return match obj.get("time") {
                Some(Value::String(time_raw)) => (3, Some(time_raw.clone())),
                _ => (2, None),
            };
        }
    }
    (1, None)
}

/// `restic_runner.build_backup_argv` twin — the exact `restic backup` argv.
///
/// `paths` arrive as strings (Python passes `str(p)` — `Path` rendering
/// stays Python-side).
#[must_use]
pub fn build_backup_argv(
    binary: &str,
    paths: &[String],
    excludes: &[String],
    tags: &[String],
) -> Vec<String> {
    let mut argv: Vec<String> = vec![binary.to_string(), "backup".to_string()];
    argv.extend(paths.iter().cloned());
    for exclude in excludes {
        argv.push("--exclude".to_string());
        argv.push(exclude.clone());
    }
    argv.push("--exclude-caches".to_string());
    for tag in tags {
        argv.push("--tag".to_string());
        argv.push(tag.clone());
    }
    argv.push("--host".to_string());
    argv.push(BACKUP_HOST.to_string());
    argv.push("--json".to_string());
    argv
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── evaluate_deadman_decision matrix (one test per arm) ────────────────

    fn decide(
        state_error: bool,
        state: bool,
        success_s: Option<f64>,
        running_s: Option<f64>,
        alert_s: Option<f64>,
    ) -> DeadmanDecision {
        evaluate_deadman_decision(
            state_error,
            state,
            success_s,
            running_s,
            alert_s,
            26.0,
            6.0,
            300.0,
            48.0,
        )
    }

    #[test]
    fn state_corrupt_wins_first() {
        let d = decide(true, true, Some(10.0), Some(10.0), None);
        assert_eq!(d.arm, ARM_STATE_CORRUPT);
        assert_eq!(d.verdict, VERDICT_STATE_CORRUPT);
        assert!(d.should_alert);
        assert_eq!(d.hours_since_success, None);
    }

    #[test]
    fn missing_state_is_never_succeeded() {
        let d = decide(false, false, None, None, None);
        assert_eq!(d.arm, ARM_NEVER_NO_FILE);
        assert_eq!(d.verdict, VERDICT_NEVER_SUCCEEDED);
        assert!(d.should_alert);
    }

    #[test]
    fn running_within_max_is_quiet() {
        let d = decide(false, true, Some(3600.0), Some(3600.0), None);
        assert_eq!(d.arm, ARM_RUNNING);
        assert_eq!(d.verdict, VERDICT_RUNNING);
        assert!(!d.should_alert);
        assert_eq!(d.hours_since_success, Some(1.0));
    }

    #[test]
    fn running_at_max_boundary_is_hung() {
        // elapsed_run_hours == max_runtime_hours → NOT `< max` → HUNG.
        let d = decide(false, true, None, Some(48.0 * 3600.0), None);
        assert_eq!(d.arm, ARM_HUNG);
        assert_eq!(d.verdict, VERDICT_HUNG);
        assert!(d.should_alert);
    }

    #[test]
    fn future_running_beyond_tolerance_is_clock_skew() {
        let d = decide(false, true, None, Some(-301.0), None);
        assert_eq!(d.arm, ARM_CLOCK_SKEW_RUNNING);
        assert_eq!(d.verdict, VERDICT_CLOCK_SKEW);
        assert!(d.should_alert);
    }

    #[test]
    fn future_running_within_tolerance_is_running() {
        // -(-300.0) == 300.0 is NOT > 300.0 → falls to the RUNNING arm.
        let d = decide(false, true, None, Some(-300.0), None);
        assert_eq!(d.arm, ARM_RUNNING);
    }

    #[test]
    fn no_success_stamp_is_never_succeeded() {
        let d = decide(false, true, None, None, None);
        assert_eq!(d.arm, ARM_NEVER_NO_STAMP);
        assert_eq!(d.verdict, VERDICT_NEVER_SUCCEEDED);
        assert!(d.should_alert);
        assert_eq!(d.hours_since_success, None);
    }

    #[test]
    fn future_success_beyond_tolerance_is_clock_skew() {
        let d = decide(false, true, Some(-301.0), None, None);
        assert_eq!(d.arm, ARM_CLOCK_SKEW_SUCCESS);
        assert_eq!(d.verdict, VERDICT_CLOCK_SKEW);
        assert!(d.should_alert);
        assert_eq!(d.hours_since_success, Some(-301.0 / 3600.0));
    }

    #[test]
    fn overdue_past_threshold() {
        let d = decide(false, true, Some(27.0 * 3600.0), None, None);
        assert_eq!(d.arm, ARM_OVERDUE);
        assert_eq!(d.verdict, VERDICT_OVERDUE);
        assert!(d.should_alert);
        assert_eq!(d.elapsed, 27.0);
    }

    #[test]
    fn fresh_at_threshold_boundary() {
        // elapsed == threshold is NOT `> threshold` → FRESH.
        let d = decide(false, true, Some(26.0 * 3600.0), None, None);
        assert_eq!(d.arm, ARM_FRESH);
        assert_eq!(d.verdict, VERDICT_FRESH);
        assert!(!d.should_alert);
        assert_eq!(d.elapsed, 26.0);
    }

    #[test]
    fn slightly_future_success_within_tolerance_clamps_elapsed_to_zero() {
        // A small negative hours (within skew tolerance) clamps like
        // CPython's `max(0.0, hours)` — to literal +0.0.
        let d = decide(false, true, Some(-1.0), None, None);
        assert_eq!(d.arm, ARM_FRESH);
        assert_eq!(d.elapsed, 0.0);
        assert!(d.elapsed.is_sign_positive());
        assert_eq!(d.hours_since_success, Some(-1.0 / 3600.0));
    }

    #[test]
    fn negative_zero_success_delta_clamps_to_positive_zero() {
        let d = decide(false, true, Some(-0.0), None, None);
        assert_eq!(d.arm, ARM_FRESH);
        assert!(d.elapsed.is_sign_positive());
    }

    // ── should_alert_decision (the realert throttle) ───────────────────────

    #[test]
    fn alert_throttled_inside_realert_window() {
        let d = decide(false, false, None, None, Some(5.0 * 3600.0));
        assert_eq!(d.arm, ARM_NEVER_NO_FILE);
        assert!(!d.should_alert, "5 h since last alert < 6 h realert bound");
    }

    #[test]
    fn alert_fires_at_realert_boundary() {
        let d = decide(false, false, None, None, Some(6.0 * 3600.0));
        assert!(d.should_alert, ">= realert bound fires (the oracle's >=)");
    }

    #[test]
    fn future_alert_stamp_fails_toward_alarm() {
        let d = decide(false, false, None, None, Some(-1.0));
        assert!(
            d.should_alert,
            "a FUTURE re-alert stamp reads as no prior alert"
        );
    }

    #[test]
    fn non_alarm_verdict_never_alerts() {
        assert!(!should_alert_decision(false, None, 6.0));
    }

    // ── renderers (structure smoke — the byte pins live in
    //    tests/golden_templates.rs against the docs golden files) ──────────

    #[test]
    fn renders_carry_labels_and_placeholders() {
        let backup = render_backup_plist();
        assert!(backup.contains("<string>com.brutex.backup</string>"));
        assert!(backup.contains("{{BRUTEX_VENV_BIN}}"));
        assert!(backup.ends_with("</dict></plist>\n"));
        let deadman = render_deadman_plist();
        assert!(deadman.contains("com.brutex.backup-deadman"));
        assert!(deadman.contains("{{BRUTEX_BACKUP_HEALTHCHECK_URL}}"));
        let drill = render_drill_plist();
        assert!(drill.contains("com.brutex.backup-drill"));
        assert!(drill.contains("<key>Day</key><integer>1</integer>"));
        let cron = render_cron_lines();
        assert_eq!(cron.matches('\n').count(), 3);
        assert!(cron.contains("backup restore-drill"));
    }

    // ── restic parse twins ─────────────────────────────────────────────────

    #[test]
    fn summary_snapshot_id_found() {
        let stdout = "{\"message_type\":\"status\"}\n{\"message_type\":\"summary\",\"snapshot_id\":\"abc123\"}\n";
        assert_eq!(
            parse_backup_summary_snapshot_id(stdout),
            (1, Some("abc123".to_string()))
        );
    }

    #[test]
    fn summary_snapshot_id_skips_invalid_summary_and_keeps_scanning() {
        // A summary line with a non-string / empty id does NOT return — the
        // oracle keeps scanning (restic_runner.py loop shape).
        let stdout = "{\"message_type\":\"summary\",\"snapshot_id\":\"\"}\n{\"message_type\":\"summary\",\"snapshot_id\":42}\n{\"message_type\":\"summary\",\"snapshot_id\":\"real\"}\n";
        assert_eq!(
            parse_backup_summary_snapshot_id(stdout),
            (1, Some("real".to_string()))
        );
    }

    #[test]
    fn summary_snapshot_id_missing_defers_to_oracle() {
        assert_eq!(
            parse_backup_summary_snapshot_id("{\"message_type\":\"status\"}\n"),
            (0, None)
        );
    }

    #[test]
    fn summary_snapshot_id_abstains_on_unparseable_line() {
        // CPython json accepts NaN — serde does not: ABSTAIN, never a claim.
        let stdout = "{\"message_type\":\"summary\",\"snapshot_id\":\"abc\", \"x\": NaN}\n";
        assert_eq!(parse_backup_summary_snapshot_id(stdout), (0, None));
    }

    #[test]
    fn summary_skips_valid_non_object_lines() {
        let stdout = "[1,2]\n\"str\"\n7\n{\"message_type\":\"summary\",\"snapshot_id\":\"ok\"}\n";
        assert_eq!(
            parse_backup_summary_snapshot_id(stdout),
            (1, Some("ok".to_string()))
        );
    }

    #[test]
    fn snapshots_latest_empty_array() {
        assert_eq!(parse_snapshots_latest_decision("[]"), (1, None, None));
    }

    #[test]
    fn snapshots_latest_selects_last_entry_short_id_first() {
        let stdout = "[{\"short_id\":\"aaa\",\"id\":\"a-full\",\"time\":\"2026-01-01T00:00:00Z\"},{\"short_id\":\"bbb\",\"id\":\"b-full\",\"time\":\"2026-02-01T00:00:00Z\"}]";
        assert_eq!(
            parse_snapshots_latest_decision(stdout),
            (
                2,
                Some("bbb".to_string()),
                Some("2026-02-01T00:00:00Z".to_string())
            )
        );
    }

    #[test]
    fn snapshots_latest_falsy_short_id_falls_to_id() {
        for falsy in ["null", "\"\"", "0", "false", "[]", "{}", "0.0"] {
            let stdout = format!(
                "[{{\"short_id\":{falsy},\"id\":\"full-id\",\"time\":\"2026-01-01T00:00:00Z\"}}]"
            );
            assert_eq!(
                parse_snapshots_latest_decision(&stdout),
                (
                    2,
                    Some("full-id".to_string()),
                    Some("2026-01-01T00:00:00Z".to_string())
                ),
                "falsy short_id {falsy} must fall through to id"
            );
        }
    }

    #[test]
    fn snapshots_latest_absent_short_id_falls_to_id() {
        let stdout = "[{\"id\":\"only-id\",\"time\":\"t\"}]";
        assert_eq!(
            parse_snapshots_latest_decision(stdout),
            (2, Some("only-id".to_string()), Some("t".to_string()))
        );
    }

    #[test]
    fn snapshots_latest_raise_shapes_defer_to_oracle() {
        for stdout in [
            "not json",           // invalid JSON → oracle raises with the CPython json message
            "{\"a\":1}",          // not an array → oracle raises naming type(dict)
            "[1]",                // entry not an object
            "[{\"short_id\":7}]", // truthy non-string id → "carries no id"
            "[{\"id\":\"\"}]",    // empty id (falsy→falsy or-chain, null chosen)
            "[{\"id\":\"x\",\"time\":42}]", // time not a string → repr-bearing raise
            "[{\"id\":\"x\"}]",   // time absent → repr-bearing raise (None)
        ] {
            assert_eq!(
                parse_snapshots_latest_decision(stdout),
                (0, None, None),
                "{stdout}"
            );
        }
    }

    #[test]
    fn ls_file_paths_keeps_file_nodes_only() {
        let stdout = "{\"time\":\"t\"}\n{\"type\":\"dir\",\"path\":\"/a\"}\n{\"type\":\"file\",\"path\":\"/a/b\"}\n{\"type\":\"file\",\"path\":\"\"}\n{\"type\":\"file\"}\n{\"type\":\"file\",\"path\":\"/c\"}\n";
        assert_eq!(
            parse_ls_file_paths(stdout),
            Some(vec!["/a/b".to_string(), "/c".to_string()])
        );
    }

    #[test]
    fn ls_file_paths_abstains_on_unparseable_line() {
        assert_eq!(
            parse_ls_file_paths("{\"type\":\"file\",\"path\":\"/a\"}\nInfinity garbage\n"),
            None
        );
    }

    #[test]
    fn ls_snapshot_time_header_with_string_time() {
        let stdout = "{\"time\":\"2026-01-01T00:00:00Z\",\"paths\":[\"/x\"]}\n{\"type\":\"file\",\"path\":\"/a\"}\n";
        assert_eq!(
            parse_ls_snapshot_time_decision(stdout),
            (3, Some("2026-01-01T00:00:00Z".to_string()))
        );
    }

    #[test]
    fn ls_snapshot_time_null_type_counts_as_header() {
        // `obj.get("type") is None` is True for a JSON null too.
        let stdout = "{\"type\":null,\"time\":\"t\"}\n";
        assert_eq!(
            parse_ls_snapshot_time_decision(stdout),
            (3, Some("t".to_string()))
        );
    }

    #[test]
    fn ls_snapshot_time_first_header_decides_even_with_bad_time() {
        // The oracle RETURNS (None) on the first header — never scans on.
        let stdout = "{\"time\":42}\n{\"time\":\"2026-01-01T00:00:00Z\"}\n";
        assert_eq!(parse_ls_snapshot_time_decision(stdout), (2, None));
    }

    #[test]
    fn ls_snapshot_time_no_header() {
        assert_eq!(
            parse_ls_snapshot_time_decision("{\"type\":\"file\",\"path\":\"/a\"}\n"),
            (1, None)
        );
    }

    #[test]
    fn ls_snapshot_time_abstains_on_unparseable_line() {
        assert_eq!(parse_ls_snapshot_time_decision("NaN\n"), (0, None));
    }

    #[test]
    fn backup_argv_exact_shape() {
        let argv = build_backup_argv(
            "restic",
            &["/lake".to_string(), "/audit".to_string()],
            &[".locks".to_string(), "*.tmp".to_string()],
            &["brutex".to_string(), "pull-quiet".to_string()],
        );
        assert_eq!(
            argv,
            vec![
                "restic",
                "backup",
                "/lake",
                "/audit",
                "--exclude",
                ".locks",
                "--exclude",
                "*.tmp",
                "--exclude-caches",
                "--tag",
                "brutex",
                "--tag",
                "pull-quiet",
                "--host",
                "brutex",
                "--json",
            ]
        );
    }

    #[test]
    fn backup_argv_empty_lists() {
        let argv = build_backup_argv("restic", &[], &[], &[]);
        assert_eq!(
            argv,
            vec![
                "restic",
                "backup",
                "--exclude-caches",
                "--host",
                "brutex",
                "--json"
            ]
        );
    }
}
