//! slice 4 — process/lock ops decision laws (detach · heal · scheduler) (DEC-RUST-SWEEP-OPS-001).
//!
//! The PURE decision folds of the process/lock-ops family — each a
//! token-for-token mirror of its retained Python oracle. NOTHING OS-shaped
//! crosses the FFI: flock/OFD composition (`sweep_singleflight` — honest
//! N/A, its only "decision" is a trivial observability pid-parse), the
//! fork/`start_new_session` spawn + env-sentinel stamp (`sweep_detach`),
//! every heal REFUSAL message + the destructive quarantine MOVE
//! (`sweep_heal`), and Telegram MarkdownV2 rendering (`sweep_alerts` —
//! honest N/A, the escaping is a byte-law) all stay Python.
//!
//! * [`detach_probe_verdict`] — `brutex/engine/sweep_detach.py`'s
//!   `probe_handle` cross-check fold (R7). The liveness verdict over the
//!   progress payload is an INPUT token here (computed Python-side by
//!   `progress_verdict`, whose own decision tail is the slice-1b routed
//!   `liveness_tail` law — this fold takes its OUTPUT, so nothing is
//!   double-routed), as is the `pids_match` equality (Python `!=` over an
//!   arbitrary JSON value stays Python).
//! * [`heal_mismatch_decision`] — `brutex/engine/sweep_heal.py`'s
//!   `assess_heal` MISMATCH TAIL over the three `Option<sha256-hex>`
//!   triples in `ARTIFACT_SUFFIXES` order (`"" · .trials.json ·
//!   .complete.json`). The happy arm (`src == plan` → `move_trio`) stays
//!   Python so dest hashing remains LAZY (no new I/O surface), and the
//!   `refuse` token maps back to the Python-owned C2 refusal message.
//! * [`schedule_next_fire`] — `brutex/engine/sweep_scheduler.py`'s
//!   `Schedule.next_fire_after` clock-injected arithmetic (interval +
//!   daily arms). The facade gates every input to the exactly-representable
//!   f64 integer range so the daily arm's Python INT arithmetic and this
//!   f64 arithmetic are value-identical; the Python wrapper reconstructs
//!   the daily arm's `int` return TYPE (the audit payload is
//!   `json.dumps`-rendered — int vs float bytes differ).
//! * [`resume_decision`] — `sweep_scheduler.decide_resume`'s pure
//!   FRESH / RESUME / ALREADY_COMPLETE classification, returned as a
//!   4-way token + resume index; the human-readable REASON strings are
//!   rendered Python-side from the token (byte-identical either arm).
//!
//! Every function is TOTAL (the crate profile is `panic = "abort"` +
//! `overflow-checks = true` — a panic would abort the host process, so
//! fallible shapes return `Err` and no unchecked arithmetic is used).

/// The `RunVerdict` wire-token vocabulary (`sweep_liveness.RunVerdict` values).
pub const RUN_VERDICT_TOKENS: [&str; 4] = ["running", "stale", "not_running", "unknown"];

/// Number of artifact slots per condemned tuple (`sweep_heal.ARTIFACT_SUFFIXES`).
const ARTIFACT_SLOTS: usize = 3;

/// Seconds per day (the scheduler's `_SECONDS_PER_DAY`).
const SECONDS_PER_DAY: f64 = 86_400.0;

/// `sweep_detach.probe_handle`'s liveness cross-check fold (R7).
///
/// Mirrors the decision law token-for-token:
///
/// 1. no readable progress (`has_progress == false`) → `"not_running"` when
///    the handle's pid is dead, else `"unknown"` (an alive pid alone is NOT
///    evidence a SWEEP runs — pid recycling);
/// 2. readable progress → the INPUT `verdict_token` (the common
///    `progress_verdict` output, computed Python-side);
/// 3. a `"running"` verdict whose progress pid differs from the handle's
///    (`pids_match == false`) → `"unknown"` (some sweep runs, not provably
///    ours).
///
/// `handle_pid_alive` is consulted ONLY in arm 1 (the Python caller probes
/// the pid lazily in that arm and passes a dummy `false` otherwise);
/// `verdict_token`/`pids_match` are consulted ONLY in arms 2-3. A missing or
/// unknown verdict token under `has_progress` is `Err` (the facade degrades
/// to the oracle — a belt, never a live path).
pub fn detach_probe_verdict(
    has_progress: bool,
    handle_pid_alive: bool,
    verdict_token: Option<&str>,
    pids_match: bool,
) -> Result<&'static str, String> {
    if !has_progress {
        return Ok(if handle_pid_alive {
            "unknown"
        } else {
            "not_running"
        });
    }
    let token = verdict_token
        .ok_or_else(|| "verdict token required when a progress payload is readable".to_string())?;
    let canonical = RUN_VERDICT_TOKENS
        .iter()
        .find(|known| **known == token)
        .copied()
        .ok_or_else(|| format!("unknown run-verdict token: {token:?}"))?;
    if canonical == "running" && !pids_match {
        return Ok("unknown");
    }
    Ok(canonical)
}

/// `sweep_heal.assess_heal`'s per-entry MISMATCH-TAIL decision (C2).
///
/// Consulted ONLY after the Python happy arm (`src == plan` → `move_trio`)
/// has failed, with the destination hashes now computed. Each slice is the
/// `Option<sha256-hex>` triple in `ARTIFACT_SUFFIXES` order:
///
/// * every source slot `None` AND `dest == plan` → `"already_quarantined"`
///   (a fully-moved, hash-matching quarantined trio from a crashed heal);
/// * the store slot (`src[0]`) `None` AND every sidecar slot either `None`
///   or plan-matching AND at least one sidecar present →
///   `"complete_partial"` (exact-match leftovers of a crashed heal);
/// * anything else → `"refuse"` (the tree changed since condemnation — the
///   C2 refusal MESSAGE stays with the Python raiser).
///
/// A slice whose length is not exactly [`ARTIFACT_SLOTS`] is `Err` (the
/// facade degrades to the oracle).
pub fn heal_mismatch_decision(
    src: &[Option<String>],
    plan: &[Option<String>],
    dest: &[Option<String>],
) -> Result<&'static str, String> {
    for (name, values) in [("src", src), ("plan", plan), ("dest", dest)] {
        if values.len() != ARTIFACT_SLOTS {
            return Err(format!(
                "{name} must carry exactly {ARTIFACT_SLOTS} artifact slots, got {}",
                values.len()
            ));
        }
    }
    if src.iter().all(Option::is_none) && dest == plan {
        return Ok("already_quarantined");
    }
    let sidecars_consistent = (1..ARTIFACT_SLOTS).all(|i| src[i].is_none() || src[i] == plan[i]);
    let any_sidecar_present = (1..ARTIFACT_SLOTS).any(|i| src[i].is_some());
    if src[0].is_none() && sidecars_consistent && any_sidecar_present {
        return Ok("complete_partial");
    }
    Ok("refuse")
}

/// `sweep_scheduler.Schedule.next_fire_after`'s clock arithmetic — O(1).
///
/// Interval arm: `anchor + interval` where `anchor` is `last_fire` (or
/// `now` on the first loop); a fallen-behind target fires at `now`.
/// Daily arm: local midnight on-or-before `now` plus the fire offset,
/// rolled forward a day when already past OR at-or-before `last_fire`
/// (post-fire idempotence).
///
/// The facade gates every numeric input to the exactly-representable f64
/// integer range (|value| ≤ 2^52), under which the daily arm's f64
/// arithmetic here is value-identical to the Python oracle's exact INT
/// arithmetic (every intermediate is an integer < 2^53). Non-finite
/// clock inputs are `Err` (defensive totality — the facade gates them).
pub fn schedule_next_fire(
    is_interval: bool,
    interval_seconds: i64,
    second_of_day: i64,
    tz_offset_seconds: i64,
    now: f64,
    last_fire: Option<f64>,
) -> Result<f64, String> {
    if !now.is_finite() {
        return Err("now must be finite".to_string());
    }
    if last_fire.is_some_and(|value| !value.is_finite()) {
        return Err("last_fire must be finite".to_string());
    }
    if is_interval {
        let anchor = last_fire.unwrap_or(now);
        let candidate = anchor + interval_seconds as f64;
        return Ok(if candidate > now { candidate } else { now });
    }
    let tz = tz_offset_seconds as f64;
    let local_now = now + tz;
    let day_start = (local_now / SECONDS_PER_DAY).floor() * SECONDS_PER_DAY;
    let today_fire = day_start + second_of_day as f64 - tz;
    let mut candidate = if today_fire >= now {
        today_fire
    } else {
        today_fire + SECONDS_PER_DAY
    };
    if last_fire.is_some_and(|fired| candidate <= fired) {
        candidate += SECONDS_PER_DAY;
    }
    Ok(candidate)
}

/// `sweep_scheduler.decide_resume`'s pure classification — O(1).
///
/// Returns `(token, resume_from_index)` with the token in
/// `{"fresh_absent", "fresh_foreign", "already_complete", "resume"}` — the
/// two FRESH arms are DISTINCT tokens because their human-readable REASON
/// strings differ; the Python caller maps token → (`ResumeKind`, reason)
/// so the rendered bytes stay Python-owned and byte-identical either arm.
pub fn resume_decision(
    exists: bool,
    sweep_id_matches: bool,
    next_index: i64,
    total_combos: i64,
) -> (&'static str, i64) {
    if !exists {
        return ("fresh_absent", 0);
    }
    if !sweep_id_matches {
        return ("fresh_foreign", 0);
    }
    if next_index >= total_combos {
        return ("already_complete", total_combos);
    }
    ("resume", next_index)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn some(text: &str) -> Option<String> {
        Some(text.to_string())
    }

    // ── detach_probe_verdict ────────────────────────────────────────────

    #[test]
    fn probe_no_progress_dead_pid_is_not_running() {
        assert_eq!(
            detach_probe_verdict(false, false, None, false).unwrap(),
            "not_running"
        );
    }

    #[test]
    fn probe_no_progress_alive_pid_is_unknown() {
        // An alive pid alone is NOT evidence a sweep runs (pid recycling).
        assert_eq!(
            detach_probe_verdict(false, true, None, false).unwrap(),
            "unknown"
        );
    }

    #[test]
    fn probe_running_with_matching_pid_stays_running() {
        assert_eq!(
            detach_probe_verdict(true, false, Some("running"), true).unwrap(),
            "running"
        );
    }

    #[test]
    fn probe_running_with_foreign_pid_is_unknown() {
        assert_eq!(
            detach_probe_verdict(true, false, Some("running"), false).unwrap(),
            "unknown"
        );
    }

    #[test]
    fn probe_non_running_verdicts_pass_through_regardless_of_pid() {
        for token in ["stale", "not_running", "unknown"] {
            for pids_match in [false, true] {
                assert_eq!(
                    detach_probe_verdict(true, false, Some(token), pids_match).unwrap(),
                    token
                );
            }
        }
    }

    #[test]
    fn probe_missing_or_unknown_token_with_progress_errs() {
        assert!(detach_probe_verdict(true, false, None, true).is_err());
        assert!(detach_probe_verdict(true, false, Some("RUNNING"), true).is_err());
        assert!(detach_probe_verdict(true, false, Some("dead"), true).is_err());
    }

    // ── heal_mismatch_decision ──────────────────────────────────────────

    #[test]
    fn heal_fully_moved_matching_trio_is_already_quarantined() {
        let plan = [some("aa"), some("bb"), None];
        let src = [None, None, None];
        let dest = [some("aa"), some("bb"), None];
        assert_eq!(
            heal_mismatch_decision(&src, &plan, &dest).unwrap(),
            "already_quarantined"
        );
    }

    #[test]
    fn heal_moved_store_with_matching_leftover_sidecar_is_complete_partial() {
        let plan = [some("aa"), some("bb"), some("cc")];
        let src = [None, some("bb"), None];
        let dest = [None, None, None];
        assert_eq!(
            heal_mismatch_decision(&src, &plan, &dest).unwrap(),
            "complete_partial"
        );
    }

    #[test]
    fn heal_all_src_absent_with_diverged_dest_refuses() {
        // Store gone but the quarantined copy does NOT hash-match the plan.
        let plan = [some("aa"), some("bb"), None];
        let src = [None, None, None];
        let dest = [some("XX"), some("bb"), None];
        assert_eq!(
            heal_mismatch_decision(&src, &plan, &dest).unwrap(),
            "refuse"
        );
    }

    #[test]
    fn heal_diverged_leftover_sidecar_refuses() {
        let plan = [some("aa"), some("bb"), some("cc")];
        let src = [None, some("XX"), None];
        let dest = [None, None, None];
        assert_eq!(
            heal_mismatch_decision(&src, &plan, &dest).unwrap(),
            "refuse"
        );
    }

    #[test]
    fn heal_store_still_present_but_diverged_refuses() {
        // src != plan (the Python happy arm failed) with the store present.
        let plan = [some("aa"), some("bb"), None];
        let src = [some("XX"), some("bb"), None];
        let dest = [None, None, None];
        assert_eq!(
            heal_mismatch_decision(&src, &plan, &dest).unwrap(),
            "refuse"
        );
    }

    #[test]
    fn heal_no_sidecar_present_is_not_complete_partial() {
        // Store absent, every sidecar absent, dest empty ≠ plan → refuse
        // (nothing left to recognise a crashed heal by).
        let plan = [some("aa"), some("bb"), None];
        let src = [None, None, None];
        let dest = [None, None, None];
        assert_eq!(
            heal_mismatch_decision(&src, &plan, &dest).unwrap(),
            "refuse"
        );
    }

    #[test]
    fn heal_wrong_arity_errs() {
        let ok = [None, None, None];
        let short = [None, None];
        assert!(heal_mismatch_decision(&short, &ok, &ok).is_err());
        assert!(heal_mismatch_decision(&ok, &short, &ok).is_err());
        assert!(heal_mismatch_decision(&ok, &ok, &short).is_err());
    }

    // ── schedule_next_fire ──────────────────────────────────────────────

    #[test]
    fn interval_first_loop_anchors_on_now() {
        let fire = schedule_next_fire(true, 300, 0, 0, 1_000.0, None).unwrap();
        assert_eq!(fire, 1_300.0);
    }

    #[test]
    fn interval_behind_schedule_fires_now() {
        let fire = schedule_next_fire(true, 300, 0, 0, 5_000.0, Some(1_000.0)).unwrap();
        assert_eq!(fire, 5_000.0);
    }

    #[test]
    fn interval_on_time_advances_from_last_fire() {
        let fire = schedule_next_fire(true, 300, 0, 0, 1_100.0, Some(1_000.0)).unwrap();
        assert_eq!(fire, 1_300.0);
    }

    #[test]
    fn daily_today_fire_still_ahead_is_today() {
        // now = 100 s past UTC midnight of day 20000; fire at 02:00 UTC.
        let now = 20_000.0 * SECONDS_PER_DAY + 100.0;
        let fire = schedule_next_fire(false, 0, 7_200, 0, now, None).unwrap();
        assert_eq!(fire, 20_000.0 * SECONDS_PER_DAY + 7_200.0);
    }

    #[test]
    fn daily_past_fire_rolls_to_tomorrow() {
        let now = 20_000.0 * SECONDS_PER_DAY + 10_000.0;
        let fire = schedule_next_fire(false, 0, 7_200, 0, now, None).unwrap();
        assert_eq!(fire, 20_001.0 * SECONDS_PER_DAY + 7_200.0);
    }

    #[test]
    fn daily_just_fired_rolls_forward_one_day() {
        let today_fire = 20_000.0 * SECONDS_PER_DAY + 7_200.0;
        let fire = schedule_next_fire(false, 0, 7_200, 0, today_fire, Some(today_fire)).unwrap();
        assert_eq!(fire, today_fire + SECONDS_PER_DAY);
    }

    #[test]
    fn daily_ist_offset_maps_local_wall_clock_onto_utc() {
        // 02:30 IST with IST = UTC+19800, probed exactly at a UTC midnight.
        let tz = 19_800;
        let now = 20_000.0 * SECONDS_PER_DAY;
        let fire = schedule_next_fire(false, 0, 2 * 3_600 + 30 * 60, tz, now, None).unwrap();
        // Hand derivation: local day floor = day 20000 (local_now = now+tz,
        // still inside local day 20000) → today_fire = 20000d + 9000 − 19800
        // < now → roll forward one day.
        assert_eq!(
            fire,
            20_000.0 * SECONDS_PER_DAY + 9_000.0 - 19_800.0 + SECONDS_PER_DAY
        );
    }

    #[test]
    fn schedule_non_finite_clock_errs() {
        assert!(schedule_next_fire(true, 300, 0, 0, f64::NAN, None).is_err());
        assert!(schedule_next_fire(false, 0, 0, 0, 0.0, Some(f64::INFINITY)).is_err());
    }

    // ── resume_decision ─────────────────────────────────────────────────

    #[test]
    fn resume_no_checkpoint_is_fresh_absent() {
        assert_eq!(resume_decision(false, false, 7, 10), ("fresh_absent", 0));
    }

    #[test]
    fn resume_foreign_identity_is_fresh_foreign() {
        assert_eq!(resume_decision(true, false, 7, 10), ("fresh_foreign", 0));
    }

    #[test]
    fn resume_cursor_at_or_past_total_is_already_complete() {
        assert_eq!(
            resume_decision(true, true, 10, 10),
            ("already_complete", 10)
        );
        assert_eq!(
            resume_decision(true, true, 99, 10),
            ("already_complete", 10)
        );
    }

    #[test]
    fn resume_with_work_left_resumes_at_cursor() {
        assert_eq!(resume_decision(true, true, 7, 10), ("resume", 7));
    }
}
