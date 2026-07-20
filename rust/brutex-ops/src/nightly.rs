//! # nightly — the nightly chain's PURE decision kernels (first ops lane)
//!
//! DEC-NIGHTLY-RUST-CORE-001. Token-for-token mirrors of the pure laws in
//! `brutex/nightly/` — the retained Python bodies (`*_python`) are the LIVE
//! parity oracle every facade degrade path serves:
//!
//! 1. **Break law + exit code** — `chain.py::first_breaker` /
//!    `chain.py::exit_code_for`: the first `refused`/`failed` outcome breaks
//!    the chain; `REFUSED` ⇒ 1, `FAILED` ⇒ its `exit_hint` when set else 2,
//!    all green ⇒ 0; ANY tripwire upgrades to `max(base, 2)`.
//! 2. **Audit-reconcile tripwire (T2)** —
//!    `chain.py::audit_reconcile_tripwire`: exactly one `NIGHTLY_START`;
//!    `LINK_START`s a dup-free SUBSET of attempted links; one
//!    `LINK_DONE`/`LINK_BLOCKED` per declared link (sorted-multiset
//!    equality). The problem strings embed CPython `repr()` renderings of
//!    the derived lists — those repr strings are PRE-FORMATTED Python-side
//!    and cross the FFI verbatim (the lake_rust_stats "isoformat pinned
//!    both arms" law: both arms consume the identical strings, so no Rust
//!    re-derivation of CPython's `repr` exists). The DECISIONS (counts,
//!    dup/subset, multiset equality) live here.
//! 3. **Verdict + Telegram summary** — `report.py::_verdict` /
//!    `report.py::summary_lines` / `report.py::_count_text`: the
//!    OK/SUSPECT/REFUSED/FAILED label + the chain-order per-link body lines
//!    with honest `?` counts (never a fabricated 0 — Golden Rule #1).
//! 4. **launchd plist render + schedule arithmetic** —
//!    `schedule.py::render_launchd_plist` (+ its `_xml_escape`),
//!    `schedule.py::parse_at`, `schedule.py::pmset_wake_command`,
//!    `schedule.py::seconds_since_scheduled_fire`. The two log paths are
//!    PRE-FORMATTED Python-side (`str(log_dir / f"{stem}.out.log")`) so
//!    pathlib's join/normalization semantics never get a Rust re-derivation.
//!
//! NO I/O anywhere in this module. Anything the mirror cannot decide
//! faithfully (a non-ASCII `--at` candidate — CPython's un-flagged `\d`
//! matches Unicode digits; an out-of-`datetime`-range component; a
//! non-finite duration) returns `Err` → the facade's degrade set serves the
//! Python oracle, which produces the genuine Python behavior (value OR the
//! original exception). Refuse-to-guess, never silently-different.
//!
//! HONESTY (the ADR-0020 banner, applied to this lane): expected payoff
//! ≈ 0 — conversion is for uniformity under the operator Rust-everywhere
//! mandate, not speed. Every kernel is microsecond-scale in Python and the
//! FFI hop is a small SLOWDOWN, accepted because the seams run once per
//! nightly run / install / status probe and ship DEFAULT-OFF.

// ── chain.py mirrors ────────────────────────────────────────────────────

/// `chain._BREAKERS` membership — `{REFUSED, FAILED}` by StrEnum value.
fn is_breaker(status: &str) -> bool {
    status == "refused" || status == "failed"
}

/// The verbatim `first_breaker` loop, projected to the breaker's INDEX
/// (the facade maps the index back onto the original `LinkOutcome`).
pub fn first_breaker_index_impl(statuses: &[String]) -> Option<usize> {
    statuses.iter().position(|status| is_breaker(status))
}

/// The `exit_code_for` law. `statuses[i]` / `exit_hints[i]` are the i-th
/// outcome's `status.value` / `exit_hint`; `has_tripwires` mirrors the
/// truthiness of the tripwires sequence. Length mismatch = a facade bug —
/// refuse loudly (degrades to the oracle), never index out of bounds.
pub fn exit_code_impl(
    statuses: &[String],
    exit_hints: &[Option<i64>],
    has_tripwires: bool,
) -> Result<i64, String> {
    if statuses.len() != exit_hints.len() {
        return Err(format!(
            "statuses/exit_hints length mismatch: {} != {}",
            statuses.len(),
            exit_hints.len()
        ));
    }
    let base = match first_breaker_index_impl(statuses) {
        None => 0,                                        // EXIT_OK
        Some(index) if statuses[index] == "refused" => 1, // EXIT_USER_ERROR
        Some(index) => exit_hints[index].unwrap_or(2),    // hint else EXIT_INTERNAL_ERROR
    };
    if has_tripwires {
        // SUSPECT only ever UPGRADES severity: max(base, EXIT_INTERNAL_ERROR).
        return Ok(base.max(2));
    }
    Ok(base)
}

/// The `audit_reconcile_tripwire` decision core. Inputs:
///
/// * `events` — the `(action, link-or-None)` projection of this run's audit
///   stream (verbatim from `_own_audit_events`);
/// * `outcome_links` / `outcome_attempted` — per declared outcome, its
///   `link.value` and `started_utc is not None`;
/// * the four `*_repr` strings — CPython `repr()` of the DERIVED lists
///   (`link_starts`, `sorted(attempted)`, `terminal`, `declared`),
///   pre-formatted Python-side (module doc, law 2).
///
/// Returns the tripwire DETAIL string, or `None` when the stream
/// reconciles (the facade builds the `Tripwire` object).
#[allow(clippy::too_many_arguments)]
pub fn audit_reconcile_detail_impl(
    nightly_id: &str,
    events: &[(String, Option<String>)],
    outcome_links: &[String],
    outcome_attempted: &[bool],
    link_starts_repr: &str,
    attempted_sorted_repr: &str,
    terminal_repr: &str,
    declared_repr: &str,
) -> Result<Option<String>, String> {
    if outcome_links.len() != outcome_attempted.len() {
        return Err(format!(
            "outcome_links/outcome_attempted length mismatch: {} != {}",
            outcome_links.len(),
            outcome_attempted.len()
        ));
    }
    let starts = events
        .iter()
        .filter(|(action, _)| action == "NIGHTLY_START")
        .count();
    let mut problems: Vec<String> = Vec::new();
    if starts != 1 {
        problems.push(format!("NIGHTLY_START x{starts} (want 1)"));
    }

    // attempted = {o.link.value for o in outcomes if o.started_utc is not None}
    let attempted: std::collections::BTreeSet<&str> = outcome_links
        .iter()
        .zip(outcome_attempted.iter())
        .filter(|pair| *pair.1)
        .map(|(link, _)| link.as_str())
        .collect();
    // named_starts = [x for x in link_starts if x is not None]
    let named_starts: Vec<&str> = events
        .iter()
        .filter(|(action, _)| action == "LINK_START")
        .filter_map(|(_, link)| link.as_deref())
        .collect();
    let named_unique: std::collections::BTreeSet<&str> = named_starts.iter().copied().collect();
    let has_dup = named_starts.len() != named_unique.len();
    let is_subset = named_unique.iter().all(|link| attempted.contains(link));
    if has_dup || !is_subset {
        problems.push(format!(
            "LINK_START {link_starts_repr} not a subset of attempted {attempted_sorted_repr}"
        ));
    }

    // sorted(declared) != sorted(x for x in terminal if x is not None)
    let mut declared_sorted: Vec<&str> = outcome_links.iter().map(String::as_str).collect();
    declared_sorted.sort_unstable();
    let mut terminal_sorted: Vec<&str> = events
        .iter()
        .filter(|(action, _)| action == "LINK_DONE" || action == "LINK_BLOCKED")
        .filter_map(|(_, link)| link.as_deref())
        .collect();
    terminal_sorted.sort_unstable();
    if declared_sorted != terminal_sorted {
        problems.push(format!(
            "LINK_DONE/BLOCKED {terminal_repr} != declared {declared_repr}"
        ));
    }

    if problems.is_empty() {
        return Ok(None);
    }
    Ok(Some(format!(
        "nightly {nightly_id} audit does not reconcile: {}",
        problems.join("; ")
    )))
}

// ── report.py mirrors ───────────────────────────────────────────────────

/// `report._count_text` — unknown counts render `"?"`, NEVER a fabricated 0.
pub fn count_text_impl(value: Option<i64>) -> String {
    match value {
        None => "?".to_string(),
        Some(count) => count.to_string(),
    }
}

/// `report._verdict` — the terminal label over the link statuses + suspect.
pub fn verdict_impl(statuses: &[String], suspect: bool) -> &'static str {
    match first_breaker_index_impl(statuses) {
        Some(index) if statuses[index] == "refused" => "REFUSED",
        Some(_) => "FAILED",
        None if suspect => "SUSPECT",
        None => "OK",
    }
}

/// The exactly-representable-in-f64 integer bound (2^53). A duration at or
/// past it (or non-finite) cannot be mirrored faithfully against CPython's
/// arbitrary-precision `int(duration)` — refuse to the oracle.
const MAX_EXACT_DURATION: f64 = 9_007_199_254_740_992.0;

/// `report.summary_lines` — the pure Telegram body builder.
///
/// * `links` rows are `(link.value, status.value, blocked_by.value|None)`;
/// * `counts` is `report.counts.items()` (values `int | None`);
/// * `tripwires` rows are `(name, detail)`;
/// * `suspect` mirrors `report.suspect` (`bool(report.tripwires)`).
#[allow(clippy::too_many_arguments)]
pub fn summary_lines_impl(
    nightly_id: &str,
    started_utc: f64,
    ended_utc: f64,
    links: &[(String, String, Option<String>)],
    counts: &[(String, Option<i64>)],
    tripwires: &[(String, String)],
    honesty_notes: &[String],
    dry_run: bool,
    suspect: bool,
) -> Result<(Vec<String>, String), String> {
    let statuses: Vec<String> = links.iter().map(|(_, status, _)| status.clone()).collect();
    let verdict = verdict_impl(&statuses, suspect).to_string();
    // Python `max(0.0, ended - started)`: a NaN difference compares False
    // against 0.0, so Python returns 0.0 — Rust f64::max(NaN, 0.0) == 0.0
    // too (returns the non-NaN operand). A +inf difference is refused (the
    // oracle raises Python's own OverflowError at `int(inf)`).
    let duration = (ended_utc - started_utc).max(0.0);
    if !duration.is_finite() || duration >= MAX_EXACT_DURATION {
        return Err(format!(
            "duration {duration} outside the faithful mirror domain — python oracle decides"
        ));
    }
    let total = duration as i64; // int(duration): truncation, duration >= 0
    let hours = total / 3600; // divmod(total, 3600) — non-negative
    let minutes = (total % 3600) / 60;

    let breaker = first_breaker_index_impl(&statuses);
    let headline = match verdict.as_str() {
        "OK" => "✅ OK".to_string(),
        "SUSPECT" => "⚠️ SUSPECT".to_string(),
        "REFUSED" => match breaker {
            Some(index) => format!("⛔ REFUSED at {}", links[index].0),
            None => "⛔ REFUSED".to_string(),
        },
        _ => match breaker {
            Some(index) => format!("❌ FAILED at {}", links[index].0),
            None => "❌ FAILED".to_string(),
        },
    };
    let mut lines = vec![format!(
        "🌙 Nightly {nightly_id} — {headline} in {hours}:{minutes:02}."
    )];

    // dict(counts).get(key) — last duplicate wins, absent == None-valued.
    let count_get = |key: &str| -> Option<i64> {
        let mut found: Option<i64> = None;
        for (candidate, value) in counts {
            if candidate == key {
                found = *value;
            }
        }
        found
    };

    let mut parts: Vec<String> = Vec::new();
    for (link, status_token, blocked_by) in links {
        let mut status = status_token.clone();
        if status_token == "blocked"
            && let Some(blocker) = blocked_by
        {
            status = format!("blocked({blocker})");
        }
        if link == "backup" && status_token != "blocked" {
            status.push_str(&format!(
                " ({} files)",
                count_text_impl(count_get("backup_files"))
            ));
        } else if link == "pull" && status_token != "blocked" {
            status.push_str(&format!(
                " ({} bars)",
                count_text_impl(count_get("pull_bars"))
            ));
        } else if link == "prebuild" && status_token != "blocked" {
            status.push_str(&format!(
                " ({} built)",
                count_text_impl(count_get("prebuild_built"))
            ));
        } else if link == "sweep" && status_token != "blocked" {
            status.push_str(&format!(
                " ({} run · {} skipped-cache · {} failed)",
                count_text_impl(count_get("sweep_run")),
                count_text_impl(count_get("sweep_skipped_cache")),
                count_text_impl(count_get("sweep_failed"))
            ));
        }
        parts.push(format!("{link} {status}"));
    }
    lines.push(parts.join(" · "));

    for (name, detail) in tripwires {
        lines.push(format!("⚠️ tripwire {name}: {detail}"));
    }
    let backup_skipped = links
        .iter()
        .any(|(link, status, _)| link == "backup" && status == "skipped");
    if backup_skipped && !dry_run {
        lines.push("⚠️ backup SKIPPED by operator flag — stores unprotected tonight.".to_string());
    }
    for note in honesty_notes {
        lines.push(format!("ℹ️ {note}"));
    }
    Ok((lines, verdict))
}

// ── schedule.py mirrors ─────────────────────────────────────────────────

/// `schedule._xml_escape` — the chained-replace law (`&` FIRST, so the
/// later entity ampersands are never re-escaped).
pub fn xml_escape_impl(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

/// `schedule.render_launchd_plist` — golden-pinned plist bytes.
///
/// `out_log` / `err_log` arrive PRE-FORMATTED (`str(log_dir /
/// f"{log_stem}.out.log")` — pathlib semantics stay Python-owned, module
/// doc law 4); `env_vars` arrives as raw `.items()` pairs and is sorted
/// HERE (the `sorted(env_vars.items())` law — Python tuple order ≡ Rust
/// tuple `Ord`, both code-point-wise on the strings).
#[allow(clippy::too_many_arguments)]
pub fn render_launchd_plist_impl(
    label: &str,
    program_arguments: &[String],
    hour: i64,
    minute: i64,
    env_vars: &[(String, String)],
    out_log: &str,
    err_log: &str,
) -> String {
    let argv_xml: String = program_arguments
        .iter()
        .map(|token| format!("    <string>{}</string>\n", xml_escape_impl(token)))
        .collect();
    let mut env_xml = String::new();
    if !env_vars.is_empty() {
        let mut sorted_env: Vec<&(String, String)> = env_vars.iter().collect();
        sorted_env.sort();
        let rows: String = sorted_env
            .iter()
            .map(|(key, value)| {
                format!(
                    "    <key>{}</key><string>{}</string>\n",
                    xml_escape_impl(key),
                    xml_escape_impl(value)
                )
            })
            .collect();
        env_xml = format!("  <key>EnvironmentVariables</key><dict>\n{rows}  </dict>\n");
    }
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n\
         <!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \
         \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n\
         <plist version=\"1.0\"><dict>\n  \
         <key>Label</key><string>{label_xml}</string>\n  \
         <key>ProgramArguments</key><array>\n{argv_xml}  </array>\n  \
         <key>StartCalendarInterval</key><dict>\n    \
         <key>Hour</key><integer>{hour}</integer>\
         <key>Minute</key><integer>{minute}</integer>\n  \
         </dict>\n  \
         <key>RunAtLoad</key><false/>\n\
         {env_xml}  \
         <key>StandardOutPath</key><string>{out_log_xml}</string>\n  \
         <key>StandardErrorPath</key><string>{err_log_xml}</string>\n  \
         <key>ProcessType</key><string>Background</string>\n  \
         <key>ThrottleInterval</key><integer>30</integer>\n\
         </dict></plist>\n",
        label_xml = xml_escape_impl(label),
        argv_xml = argv_xml,
        hour = hour,
        minute = minute,
        env_xml = env_xml,
        out_log_xml = xml_escape_impl(out_log),
        err_log_xml = xml_escape_impl(err_log),
    )
}

/// CPython's `str.strip()` whitespace set, restricted to ASCII (the only
/// bytes that can reach this after the non-ASCII refusal below): note it
/// INCLUDES `\x1c..\x1f`, which Unicode `White_Space` (Rust `trim()`) does
/// NOT — the mirror strips exactly Python's set.
fn is_python_ascii_space(byte: u8) -> bool {
    matches!(
        byte,
        b'\t' | b'\n' | 0x0b | 0x0c | b'\r' | b' ' | 0x1c..=0x1f
    )
}

/// `schedule.parse_at`'s DECISION core: `Some((hour, minute))` on a match
/// of `^([01]?\d|2[0-3]):([0-5]\d)$` over the stripped text, `None` on a
/// clean no-match (the facade raises Python's exact ValueError), `Err` on
/// non-ASCII input — CPython's un-flagged `\d` matches Unicode decimal
/// digits, which this hand-rolled ASCII matcher deliberately does not
/// re-derive (refuse-to-guess; the oracle decides).
pub fn parse_at_impl(text: &str) -> Result<Option<(i64, i64)>, String> {
    if !text.is_ascii() {
        return Err("non-ASCII --at input — python oracle decides".to_string());
    }
    let bytes = text.as_bytes();
    let mut start = 0usize;
    let mut end = bytes.len();
    while start < end && is_python_ascii_space(bytes[start]) {
        start += 1;
    }
    while end > start && is_python_ascii_space(bytes[end - 1]) {
        end -= 1;
    }
    let stripped = &bytes[start..end];
    let Some(colon) = stripped.iter().position(|&byte| byte == b':') else {
        return Ok(None);
    };
    let (hour_part, rest) = (&stripped[..colon], &stripped[colon + 1..]);
    let hour = match hour_part {
        [digit] if digit.is_ascii_digit() => i64::from(digit - b'0'),
        [tens @ (b'0' | b'1'), ones] if ones.is_ascii_digit() => {
            i64::from(tens - b'0') * 10 + i64::from(ones - b'0')
        }
        [b'2', ones @ b'0'..=b'3'] => 20 + i64::from(ones - b'0'),
        _ => return Ok(None),
    };
    let minute = match rest {
        [tens @ b'0'..=b'5', ones] if ones.is_ascii_digit() => {
            i64::from(tens - b'0') * 10 + i64::from(ones - b'0')
        }
        _ => return Ok(None),
    };
    Ok(Some((hour, minute)))
}

/// `schedule.pmset_wake_command` — the midnight-wrapping lead arithmetic.
/// Python `%` is floored (always non-negative for a positive modulus) —
/// mirrored by `rem_euclid`; an i64 overflow (impossible through the
/// facade's parse_at-validated callers) refuses to the oracle.
pub fn pmset_wake_command_impl(
    hour: i64,
    minute: i64,
    lead_minutes: i64,
) -> Result<String, String> {
    let total = hour
        .checked_mul(60)
        .and_then(|value| value.checked_add(minute))
        .and_then(|value| value.checked_sub(lead_minutes))
        .ok_or_else(|| "pmset arithmetic overflow — python oracle decides".to_string())?;
    let total = total.rem_euclid(24 * 60);
    Ok(format!(
        "sudo pmset repeat wakeorpoweron MTWRFSU {:02}:{:02}:00",
        total / 60,
        total % 60
    ))
}

/// `schedule.seconds_since_scheduled_fire` — pure calendar arithmetic over
/// the fire `HH:MM` and `now_local`'s intraday components (the facade
/// extracts them; the datetime object never crosses the FFI).
///
/// Equivalence to the Python body (`fire = now.replace(hour, minute, 0, 0);
/// if fire > now: fire -= 1 day; return (now - fire).total_seconds()`):
/// with both datetimes sharing date + tzinfo, `fire > now` ⇔ the fire
/// instant's microseconds-into-day exceed now's, and the day-borrowed
/// difference is exactly `86_400s + (now − fire-intraday)`. The integer
/// microsecond numerator is < 2^53, so `f64` division by 1e6 is the same
/// correctly-rounded value CPython's `timedelta.total_seconds()` produces.
/// Out-of-range components (Python's `replace` would raise ValueError)
/// refuse to the oracle, which raises the genuine Python error.
pub fn seconds_since_fire_impl(
    hour: i64,
    minute: i64,
    now_hour: i64,
    now_minute: i64,
    now_second: i64,
    now_microsecond: i64,
) -> Result<f64, String> {
    for (value, low, high) in [
        (hour, 0, 23),
        (minute, 0, 59),
        (now_hour, 0, 23),
        (now_minute, 0, 59),
        (now_second, 0, 59),
        (now_microsecond, 0, 999_999),
    ] {
        if value < low || value > high {
            return Err(format!(
                "component {value} outside {low}..={high} — python oracle decides"
            ));
        }
    }
    let now_us = (now_hour * 3600 + now_minute * 60 + now_second) * 1_000_000 + now_microsecond;
    let fire_us = (hour * 3600 + minute * 60) * 1_000_000;
    let mut delta = now_us - fire_us;
    if delta < 0 {
        delta += 86_400 * 1_000_000;
    }
    // delta <= 86_400e6 < 2^53 — the cast is exact; /1e6 is the same
    // correctly-rounded value CPython's int/int true division produces.
    let seconds = delta as f64 / 1e6;
    Ok(seconds)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn strings(tokens: &[&str]) -> Vec<String> {
        tokens.iter().map(|token| (*token).to_string()).collect()
    }

    // ── break law + exit code ───────────────────────────────────────────

    #[test]
    fn first_breaker_finds_the_first_refused_or_failed() {
        assert_eq!(first_breaker_index_impl(&strings(&[])), None);
        assert_eq!(
            first_breaker_index_impl(&strings(&["ok", "skipped", "ok"])),
            None
        );
        assert_eq!(
            first_breaker_index_impl(&strings(&["ok", "refused", "failed"])),
            Some(1)
        );
        assert_eq!(
            first_breaker_index_impl(&strings(&["failed", "refused"])),
            Some(0)
        );
        // BLOCKED is not a breaker (sequencer-assigned, never a break cause).
        assert_eq!(
            first_breaker_index_impl(&strings(&["ok", "blocked", "failed"])),
            Some(2)
        );
    }

    #[test]
    fn exit_code_law_matrix() {
        // All green ⇒ EXIT_OK.
        assert_eq!(
            exit_code_impl(&strings(&["ok", "skipped"]), &[None, None], false),
            Ok(0)
        );
        // REFUSED breaker ⇒ EXIT_USER_ERROR, hint ignored.
        assert_eq!(
            exit_code_impl(&strings(&["ok", "refused"]), &[None, Some(6)], false),
            Ok(1)
        );
        // FAILED breaker with hint ⇒ the hint (the sweep 6 pass-through).
        assert_eq!(
            exit_code_impl(&strings(&["failed"]), &[Some(6)], false),
            Ok(6)
        );
        // FAILED breaker without hint ⇒ EXIT_INTERNAL_ERROR.
        assert_eq!(exit_code_impl(&strings(&["failed"]), &[None], false), Ok(2));
        // Tripwires upgrade: 0→2, 1→2, 2→2, 6 stays 6 (numerically max()).
        assert_eq!(exit_code_impl(&strings(&["ok"]), &[None], true), Ok(2));
        assert_eq!(exit_code_impl(&strings(&["refused"]), &[None], true), Ok(2));
        assert_eq!(
            exit_code_impl(&strings(&["failed"]), &[Some(6)], true),
            Ok(6)
        );
        // Length mismatch refuses (facade-bug rail).
        assert!(exit_code_impl(&strings(&["ok"]), &[], false).is_err());
    }

    // ── audit reconcile ─────────────────────────────────────────────────

    fn event(action: &str, link: Option<&str>) -> (String, Option<String>) {
        (action.to_string(), link.map(str::to_string))
    }

    #[test]
    fn audit_reconcile_clean_stream_is_none() {
        // One NIGHTLY_START; every LINK_START attempted at most once; one
        // terminal per declared link (the PULL step's PREBUILD derivation
        // legitimately has a LINK_DONE with NO LINK_START — subset law).
        let events = vec![
            event("NIGHTLY_START", None),
            event("LINK_START", Some("pull")),
            event("LINK_DONE", Some("pull")),
            event("LINK_DONE", Some("prebuild")),
        ];
        let got = audit_reconcile_detail_impl(
            "nightly-x",
            &events,
            &strings(&["pull", "prebuild"]),
            &[true, false],
            "r1",
            "r2",
            "r3",
            "r4",
        );
        assert_eq!(got, Ok(None));
    }

    #[test]
    fn audit_reconcile_start_count_only_problem_exact_detail() {
        // Zero outcomes + zero terminals reconcile vacuously on the third
        // check — the ONLY problem is the start count.
        let events = vec![event("NIGHTLY_START", None), event("NIGHTLY_START", None)];
        let got =
            audit_reconcile_detail_impl("nightly-x", &events, &[], &[], "[]", "[]", "[]", "[]")
                .unwrap()
                .unwrap();
        assert_eq!(
            got,
            "nightly nightly-x audit does not reconcile: NIGHTLY_START x2 (want 1)"
        );
    }

    #[test]
    fn audit_reconcile_duplicate_link_start_uses_preformatted_reprs() {
        let events = vec![
            event("NIGHTLY_START", None),
            event("LINK_START", Some("pull")),
            event("LINK_START", Some("pull")),
            event("LINK_DONE", Some("pull")),
        ];
        let got = audit_reconcile_detail_impl(
            "nightly-y",
            &events,
            &strings(&["pull"]),
            &[true],
            "['pull', 'pull']",
            "['pull']",
            "['pull']",
            "['pull']",
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            got,
            "nightly nightly-y audit does not reconcile: LINK_START ['pull', 'pull'] \
             not a subset of attempted ['pull']"
        );
    }

    #[test]
    fn audit_reconcile_unattempted_start_and_terminal_mismatch_join_with_semicolon() {
        let events = vec![
            event("NIGHTLY_START", None),
            event("LINK_START", Some("sweep")),
        ];
        let got = audit_reconcile_detail_impl(
            "nightly-z",
            &events,
            &strings(&["pull"]),
            &[true],
            "['sweep']",
            "['pull']",
            "[]",
            "['pull']",
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            got,
            "nightly nightly-z audit does not reconcile: LINK_START ['sweep'] not a \
             subset of attempted ['pull']; LINK_DONE/BLOCKED [] != declared ['pull']"
        );
    }

    #[test]
    fn audit_reconcile_terminal_multiset_and_none_links() {
        // A None-linked LINK_START is kept in the dup/subset check exactly
        // as Python keeps it (named_starts filters None; the raw list repr
        // carries it) — a lone None start neither dups nor escapes subset.
        let events = vec![
            event("NIGHTLY_START", None),
            event("LINK_START", None),
            event("LINK_DONE", Some("pull")),
            event("LINK_DONE", Some("pull")),
        ];
        // declared = [pull] but terminal (non-None) = [pull, pull] — mismatch.
        let got = audit_reconcile_detail_impl(
            "nightly-m",
            &events,
            &strings(&["pull"]),
            &[true],
            "[None]",
            "['pull']",
            "['pull', 'pull']",
            "['pull']",
        )
        .unwrap()
        .unwrap();
        assert_eq!(
            got,
            "nightly nightly-m audit does not reconcile: LINK_DONE/BLOCKED \
             ['pull', 'pull'] != declared ['pull']"
        );
    }

    #[test]
    fn audit_reconcile_length_mismatch_refuses() {
        assert!(
            audit_reconcile_detail_impl("id", &[], &strings(&["pull"]), &[], "", "", "", "")
                .is_err()
        );
    }

    // ── verdict / count_text ────────────────────────────────────────────

    #[test]
    fn verdict_matrix() {
        assert_eq!(verdict_impl(&strings(&["ok", "ok"]), false), "OK");
        assert_eq!(verdict_impl(&strings(&["ok", "ok"]), true), "SUSPECT");
        assert_eq!(
            verdict_impl(&strings(&["ok", "refused", "failed"]), true),
            "REFUSED"
        );
        assert_eq!(
            verdict_impl(&strings(&["failed", "refused"]), false),
            "FAILED"
        );
        assert_eq!(verdict_impl(&[], false), "OK");
    }

    #[test]
    fn count_text_never_fabricates_zero() {
        assert_eq!(count_text_impl(None), "?");
        assert_eq!(count_text_impl(Some(0)), "0");
        assert_eq!(count_text_impl(Some(375)), "375");
        assert_eq!(count_text_impl(Some(-1)), "-1");
        assert_eq!(count_text_impl(Some(i64::MIN)), "-9223372036854775808");
    }

    // ── summary_lines ───────────────────────────────────────────────────

    /// A full six-link OK-run golden (hand-derived from the Python body;
    /// re-proven bit-equal against the live oracle by the facade's
    /// flag-ON parity tests wheel-free, and by the burn-in in production).
    #[test]
    fn summary_lines_full_ok_golden() {
        let links = vec![
            ("collision".to_string(), "ok".to_string(), None),
            ("disk".to_string(), "ok".to_string(), None),
            ("backup".to_string(), "ok".to_string(), None),
            ("pull".to_string(), "ok".to_string(), None),
            ("prebuild".to_string(), "ok".to_string(), None),
            ("sweep".to_string(), "ok".to_string(), None),
        ];
        let counts = vec![
            ("backup_files".to_string(), Some(12)),
            ("pull_bars".to_string(), Some(375)),
            ("prebuild_built".to_string(), Some(19)),
            ("sweep_run".to_string(), Some(4)),
            ("sweep_skipped_cache".to_string(), Some(2)),
            ("sweep_failed".to_string(), Some(0)),
        ];
        let (lines, verdict) = summary_lines_impl(
            "nightly-20260718T013000Z-abc123",
            1_000.0,
            1_000.0 + 3_723.0, // 1h 2m 3s → "1:02"
            &links,
            &counts,
            &[],
            &[],
            false,
            false,
        )
        .unwrap();
        assert_eq!(verdict, "OK");
        assert_eq!(
            lines,
            vec![
                "🌙 Nightly nightly-20260718T013000Z-abc123 — ✅ OK in 1:02.".to_string(),
                "collision ok · disk ok · backup ok (12 files) · pull ok (375 bars) · \
                 prebuild ok (19 built) · sweep ok (4 run · 2 skipped-cache · 0 failed)"
                    .to_string(),
            ]
        );
    }

    #[test]
    fn summary_lines_failed_blocked_and_unknown_counts_golden() {
        let links = vec![
            ("collision".to_string(), "ok".to_string(), None),
            ("disk".to_string(), "refused".to_string(), None),
            (
                "backup".to_string(),
                "blocked".to_string(),
                Some("disk".to_string()),
            ),
            (
                "pull".to_string(),
                "blocked".to_string(),
                Some("disk".to_string()),
            ),
        ];
        let (lines, verdict) = summary_lines_impl(
            "nightly-r",
            0.0,
            59.0, // 0:00 — divmod truncates below one minute
            &links,
            &[],
            &[("audit_reconcile".to_string(), "stream torn".to_string())],
            &strings(&["note one"]),
            false,
            true,
        )
        .unwrap();
        assert_eq!(verdict, "REFUSED");
        assert_eq!(
            lines,
            vec![
                "🌙 Nightly nightly-r — ⛔ REFUSED at disk in 0:00.".to_string(),
                "collision ok · disk refused · backup blocked(disk) · pull blocked(disk)"
                    .to_string(),
                "⚠️ tripwire audit_reconcile: stream torn".to_string(),
                "ℹ️ note one".to_string(),
            ]
        );
    }

    #[test]
    fn summary_lines_backup_skipped_nag_only_on_wet_runs() {
        let links = vec![("backup".to_string(), "skipped".to_string(), None)];
        // Wet run: the nag appears; backup SKIPPED still gets its count text.
        let (wet, _) =
            summary_lines_impl("n", 0.0, 0.0, &links, &[], &[], &[], false, false).unwrap();
        assert!(
            wet.contains(
                &"⚠️ backup SKIPPED by operator flag — stores unprotected tonight.".to_string()
            ),
            "{wet:?}"
        );
        assert_eq!(wet[1], "backup skipped (? files)");
        // Dry run: no nag.
        let (dry, _) =
            summary_lines_impl("n", 0.0, 0.0, &links, &[], &[], &[], true, false).unwrap();
        assert!(
            !dry.iter().any(|line| line.contains("unprotected tonight")),
            "{dry:?}"
        );
    }

    #[test]
    fn summary_lines_negative_and_nan_durations_clamp_to_zero() {
        let links = vec![("collision".to_string(), "ok".to_string(), None)];
        let (lines, _) =
            summary_lines_impl("n", 100.0, 40.0, &links, &[], &[], &[], false, false).unwrap();
        assert!(lines[0].ends_with("in 0:00."), "{:?}", lines[0]);
        // NaN difference: Python max(0.0, nan) is 0.0 — mirrored.
        let (nan_lines, _) =
            summary_lines_impl("n", f64::NAN, 40.0, &links, &[], &[], &[], false, false).unwrap();
        assert!(nan_lines[0].ends_with("in 0:00."), "{:?}", nan_lines[0]);
    }

    #[test]
    fn summary_lines_infinite_duration_refuses_to_the_oracle() {
        let links = vec![("collision".to_string(), "ok".to_string(), None)];
        assert!(
            summary_lines_impl("n", 0.0, f64::INFINITY, &links, &[], &[], &[], false, false)
                .is_err()
        );
    }

    // ── xml escape + plist render ───────────────────────────────────────

    #[test]
    fn xml_escape_chained_replace_order() {
        assert_eq!(xml_escape_impl(r#"a&b<c>d"e"#), "a&amp;b&lt;c&gt;d&quot;e");
        // '&' first — pre-existing entities double-escape exactly like Python.
        assert_eq!(xml_escape_impl("&lt;"), "&amp;lt;");
    }

    /// The FULL hardcoded plist golden — byte-for-byte the
    /// `test_plist_golden_with_env_vars` expectation in
    /// `tests/unit/test_nightly_schedule.py` (the Python-suite oracle).
    #[test]
    fn plist_full_golden_with_env_vars() {
        let rendered = render_launchd_plist_impl(
            "com.brutex.nightly",
            &strings(&["/opt/bin/brutex", "nightly", "run"]),
            1,
            30,
            &[
                // Deliberately UNSORTED input — the render sorts.
                (
                    "BRUTEX_NIGHTLY_BACKUP_ROOT".to_string(),
                    "/Volumes/backups".to_string(),
                ),
                ("BRUTEX_LAKE_ROOT".to_string(), "/Volumes/lake".to_string()),
            ],
            "/Users/op/.brutex/logs/nightly-launchd.out.log",
            "/Users/op/.brutex/logs/nightly-launchd.err.log",
        );
        let expected = concat!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n",
            "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" ",
            "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n",
            "<plist version=\"1.0\"><dict>\n",
            "  <key>Label</key><string>com.brutex.nightly</string>\n",
            "  <key>ProgramArguments</key><array>\n",
            "    <string>/opt/bin/brutex</string>\n",
            "    <string>nightly</string>\n",
            "    <string>run</string>\n",
            "  </array>\n",
            "  <key>StartCalendarInterval</key><dict>\n",
            "    <key>Hour</key><integer>1</integer><key>Minute</key><integer>30</integer>\n",
            "  </dict>\n",
            "  <key>RunAtLoad</key><false/>\n",
            "  <key>EnvironmentVariables</key><dict>\n",
            "    <key>BRUTEX_LAKE_ROOT</key><string>/Volumes/lake</string>\n",
            "    <key>BRUTEX_NIGHTLY_BACKUP_ROOT</key><string>/Volumes/backups</string>\n",
            "  </dict>\n",
            "  <key>StandardOutPath</key>",
            "<string>/Users/op/.brutex/logs/nightly-launchd.out.log</string>\n",
            "  <key>StandardErrorPath</key>",
            "<string>/Users/op/.brutex/logs/nightly-launchd.err.log</string>\n",
            "  <key>ProcessType</key><string>Background</string>\n",
            "  <key>ThrottleInterval</key><integer>30</integer>\n",
            "</dict></plist>\n",
        );
        assert_eq!(rendered, expected);
    }

    #[test]
    fn plist_omits_env_dict_when_empty_and_escapes_label() {
        let rendered = render_launchd_plist_impl(
            "com.brutex.<test>&co",
            &strings(&["/usr/bin/python3", "-m", "brutex", "nightly", "run"]),
            23,
            5,
            &[],
            "/logs/nightly-launchd.out.log",
            "/logs/nightly-launchd.err.log",
        );
        assert!(!rendered.contains("EnvironmentVariables"));
        assert!(rendered.contains("com.brutex.&lt;test&gt;&amp;co"));
        assert!(
            rendered.contains(
                "<key>Hour</key><integer>23</integer><key>Minute</key><integer>5</integer>"
            )
        );
    }

    // ── parse_at ────────────────────────────────────────────────────────

    #[test]
    fn parse_at_valid_matrix() {
        assert_eq!(parse_at_impl("01:30"), Ok(Some((1, 30))));
        assert_eq!(parse_at_impl("23:59"), Ok(Some((23, 59))));
        assert_eq!(parse_at_impl(" 9:05 "), Ok(Some((9, 5))));
        assert_eq!(parse_at_impl("0:00"), Ok(Some((0, 0))));
        assert_eq!(parse_at_impl("19:45"), Ok(Some((19, 45))));
        assert_eq!(parse_at_impl("20:00"), Ok(Some((20, 0))));
        // Python str.strip() strips \x1c..\x1f too (not Unicode White_Space).
        assert_eq!(parse_at_impl("\x1c01:30\x1f"), Ok(Some((1, 30))));
    }

    #[test]
    fn parse_at_junk_is_a_clean_no_match() {
        for junk in [
            "24:00", "1:5", "01-30", "noon", "", "01:60", "2:60", "25:00", "1:300", "1:3:0", ":30",
            "12:", "-1:30", "+1:30", "01 :30",
        ] {
            assert_eq!(parse_at_impl(junk), Ok(None), "{junk:?}");
        }
    }

    #[test]
    fn parse_at_non_ascii_refuses_to_the_oracle() {
        // CPython's un-flagged \d matches Unicode digits (e.g. '٣') — this
        // mirror refuses rather than re-deriving Unicode Nd tables.
        assert!(parse_at_impl("0٣:05").is_err());
        assert!(parse_at_impl("\u{a0}01:30").is_err()); // NBSP-stripped by Python
    }

    // ── pmset + seconds-since-fire ──────────────────────────────────────

    #[test]
    fn pmset_wake_lead_and_midnight_wrap() {
        assert_eq!(
            pmset_wake_command_impl(1, 30, 5).as_deref(),
            Ok("sudo pmset repeat wakeorpoweron MTWRFSU 01:25:00")
        );
        // A 00:02 trigger wakes at 23:57 (wraps across midnight).
        assert_eq!(
            pmset_wake_command_impl(0, 2, 5).as_deref(),
            Ok("sudo pmset repeat wakeorpoweron MTWRFSU 23:57:00")
        );
        assert_eq!(
            pmset_wake_command_impl(0, 5, 5).as_deref(),
            Ok("sudo pmset repeat wakeorpoweron MTWRFSU 00:00:00")
        );
        assert!(pmset_wake_command_impl(i64::MAX, 0, 0).is_err());
    }

    #[test]
    fn seconds_since_fire_matrix() {
        // Fire already past today: 01:30 fire, now 02:00:00.000000 → 1800 s.
        assert_eq!(seconds_since_fire_impl(1, 30, 2, 0, 0, 0), Ok(1_800.0));
        // Exactly at the fire instant → 0 (Python: `fire > now` is False).
        assert_eq!(seconds_since_fire_impl(1, 30, 1, 30, 0, 0), Ok(0.0));
        // Fire still ahead today → yesterday's fire: 23:00 fire, now 01:00
        // → 2 h since yesterday's 23:00.
        assert_eq!(seconds_since_fire_impl(23, 0, 1, 0, 0, 0), Ok(7_200.0));
        // Microsecond precision (exact f64: numerator < 2^53).
        assert_eq!(seconds_since_fire_impl(1, 30, 1, 30, 0, 250_000), Ok(0.25));
        // Just before the fire: 1 µs short → 86 399.999999 s.
        assert_eq!(
            seconds_since_fire_impl(1, 30, 1, 29, 59, 999_999),
            Ok(86_399.999_999)
        );
        // Out-of-range components refuse (Python replace() would ValueError).
        assert!(seconds_since_fire_impl(24, 0, 1, 0, 0, 0).is_err());
        assert!(seconds_since_fire_impl(1, 60, 1, 0, 0, 0).is_err());
        assert!(seconds_since_fire_impl(1, 0, 1, 0, 0, 1_000_000).is_err());
    }
}
