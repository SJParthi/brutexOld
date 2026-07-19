//! slice 3 — durable run-artifact laws (exit-status · quarantine ordinal · checkpoint) (DEC-RUST-SWEEP-OPS-001).
//!
//! The PURE decision laws of the durable-artifacts family — each a
//! token-for-token mirror of its retained Python oracle. NO write, fsync,
//! rename or lock crosses the FFI: durability / crash-convergence ORDERING
//! is #1712-hostile-reviewed and stays Python; only the decisions route.
//!
//! * [`exit_status_resolution`] — `brutex/engine/sweep_exit_status.py`'s
//!   wall-clock preservation + suspect-resolution law (the #1712 round-3
//!   `wall_started_utc` preservation fix: a prior readable start is ALWAYS
//!   preserved — only the very first write stamps `now`). Floats cross the
//!   FFI as f64 (bit-exact both ways); RENDERING (json.dumps float repr)
//!   stays Python — the float-repr trap never crosses.
//! * [`next_ordinal`] — `brutex/engine/sweep_quarantine.py`'s monotonic
//!   quarantine-dirname ordinal fold over ALREADY-LISTED entry names (the
//!   `iterdir` I/O stays Python). Python's `re` `\d` + `int()` accept
//!   NON-ASCII Unicode digits — the facade's input gate therefore refuses
//!   any non-ASCII name/prefix/suffix to the oracle, and this law is
//!   ASCII-exact by construction (defensive `Err` otherwise).
//! * [`checkpoint_record_decision`] / [`checkpoint_adoption_verdict`] —
//!   `brutex/engine/sweep_checkpoint.py`'s monotonic high-water advance
//!   decision and the load-tail adoption verdict (foreign-id → ignore;
//!   same-id total mismatch → the tamper refusal). Message BYTES stay with
//!   the Python raiser.
//!
//! Every function is TOTAL (the crate profile is `panic = "abort"` +
//! `overflow-checks = true` — a panic would abort the host process, so
//! fallible arithmetic uses `checked_*` and unknown tokens return `Err`).

/// The `RunState` wire-token vocabulary (`sweep_exit_status.RunState` values).
pub const RUN_STATE_TOKENS: [&str; 6] = [
    "running",
    "complete",
    "failed",
    "refused",
    "halted",
    "suspect_complete",
];

/// The exit-status wall-clock + suspect resolution law.
///
/// Mirrors `write_exit_status`'s decision block over the already-extracted
/// primitives (`prior_started` = the prior artifact's finite
/// `wall_started_utc` or `None`; `prior_suspect` = the prior artifact's
/// coerced `suspect` flag or `None` when no prior artifact was readable):
///
/// * `started` — the prior start when present, else `now` (the #1712
///   round-3 preservation law — holds for RUNNING re-writes too);
/// * `ended` — `None` while `state == "running"`, else `now`;
/// * `suspect` — `suspect_complete` FORCES `true` (D-12); else an explicit
///   `suspect` wins; else the prior flag (default `false`).
///
/// Returns `Err` on an unknown state token (the facade's input gate makes
/// this unreachable in production — defensive totality).
pub fn exit_status_resolution(
    state: &str,
    prior_started: Option<f64>,
    now: f64,
    suspect: Option<bool>,
    prior_suspect: Option<bool>,
) -> Result<(f64, Option<f64>, bool), String> {
    if !RUN_STATE_TOKENS.contains(&state) {
        return Err(format!("unknown run state token: {state:?}"));
    }
    let started = prior_started.unwrap_or(now);
    let ended = if state == "running" { None } else { Some(now) };
    let resolved_suspect = if state == "suspect_complete" {
        true
    } else if let Some(explicit) = suspect {
        explicit
    } else {
        prior_suspect.unwrap_or(false)
    };
    Ok((started, ended, resolved_suspect))
}

/// The monotonic quarantine-ordinal fold over already-listed entry names.
///
/// Mirrors `sweep_quarantine._next_ordinal`'s per-name law exactly: a name
/// participates iff it starts with `prefix`, ends with `suffix`, and its
/// core (the slice between them) is a non-empty run of ASCII digits; the
/// result is `max(core) + 1` (`1` when none). Overlapping prefix/suffix
/// (`core_end < core_start`) skips the name — the Python slice yields `""`
/// there and the `^\d+$` regex rejects it.
///
/// Returns `Err` on any non-ASCII input (the facade gates these to the
/// oracle — Python's `int()` accepts Unicode digits, this law does not) or
/// on an ordinal that overflows u64 (Python ints are unbounded — degrade).
pub fn next_ordinal(names: &[String], prefix: &str, suffix: &str) -> Result<u64, String> {
    if !prefix.is_ascii() || !suffix.is_ascii() {
        return Err("next_ordinal: non-ASCII prefix/suffix".to_string());
    }
    let mut highest: u64 = 0;
    for name in names {
        if !name.is_ascii() {
            return Err(format!("next_ordinal: non-ASCII name {name:?}"));
        }
        if !(name.starts_with(prefix) && name.ends_with(suffix)) {
            continue;
        }
        let core_start = prefix.len();
        let Some(core_end) = name.len().checked_sub(suffix.len()) else {
            continue; // suffix longer than the name — unreachable post ends_with, defensive
        };
        if core_end < core_start {
            continue; // overlapping prefix/suffix — the Python empty-slice arm
        }
        let core = &name[core_start..core_end];
        if core.is_empty() || !core.bytes().all(|b| b.is_ascii_digit()) {
            continue;
        }
        let Ok(value) = core.parse::<u64>() else {
            return Err(format!("next_ordinal: ordinal overflow in {name:?}"));
        };
        highest = highest.max(value);
    }
    highest
        .checked_add(1)
        .ok_or_else(|| "next_ordinal: ordinal overflow".to_string())
}

/// The checkpoint `record` monotonic-advance decision.
///
/// Mirrors `SweepCheckpointStore.record`'s guard order exactly (message
/// bytes stay with the Python raiser):
///
/// * `completed_index < 0` → `"reject_below_zero"`;
/// * `completed_index >= total_combos` → `"reject_beyond_total"`;
/// * `completed_index <= last_index` → `"noop"` (the never-rewind law);
/// * else → `"advance"`.
pub fn checkpoint_record_decision(
    completed_index: i64,
    last_index: i64,
    total_combos: i64,
) -> &'static str {
    if completed_index < 0 {
        "reject_below_zero"
    } else if completed_index >= total_combos {
        "reject_beyond_total"
    } else if completed_index <= last_index {
        "noop"
    } else {
        "advance"
    }
}

/// The checkpoint load-tail adoption verdict.
///
/// Mirrors `SweepCheckpointStore.load`'s post-parse decision order: a
/// FOREIGN `sweep_id` is another sweep's valid state (`"foreign"` — ignored
/// upstream, never quarantined); a same-id checkpoint whose `total_combos`
/// disagrees is corruption/tampering by construction (`"total_mismatch"` —
/// the loud refusal); else `"adopt"`.
pub fn checkpoint_adoption_verdict(
    store_sweep_id: &str,
    store_total: i64,
    cp_sweep_id: &str,
    cp_total: i64,
) -> &'static str {
    if cp_sweep_id != store_sweep_id {
        "foreign"
    } else if cp_total != store_total {
        "total_mismatch"
    } else {
        "adopt"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // ── exit_status_resolution — the wall-preservation + suspect laws ──

    #[test]
    fn running_first_write_stamps_now() {
        let (started, ended, suspect) =
            exit_status_resolution("running", None, 100.5, None, None).unwrap();
        assert_eq!(started, 100.5);
        assert_eq!(ended, None);
        assert!(!suspect);
    }

    #[test]
    fn running_rewrite_preserves_prior_start() {
        // The #1712 round-3 law: a mid-run RUNNING write (mark_suspect's
        // never-launder belt) must NOT reset the launch instant.
        let (started, ended, _) =
            exit_status_resolution("running", Some(50.25), 100.5, Some(true), None).unwrap();
        assert_eq!(started, 50.25);
        assert_eq!(ended, None);
    }

    #[test]
    fn terminal_states_stamp_ended_now() {
        for state in [
            "complete",
            "failed",
            "refused",
            "halted",
            "suspect_complete",
        ] {
            let (started, ended, _) =
                exit_status_resolution(state, Some(50.25), 100.5, None, None).unwrap();
            assert_eq!(started, 50.25);
            assert_eq!(ended, Some(100.5));
        }
    }

    #[test]
    fn suspect_complete_forces_true_over_explicit_false() {
        let (_, _, suspect) =
            exit_status_resolution("suspect_complete", None, 1.0, Some(false), Some(false))
                .unwrap();
        assert!(suspect);
    }

    #[test]
    fn explicit_suspect_wins_over_prior() {
        let (_, _, suspect) =
            exit_status_resolution("complete", None, 1.0, Some(false), Some(true)).unwrap();
        assert!(!suspect);
        let (_, _, suspect) =
            exit_status_resolution("complete", None, 1.0, Some(true), Some(false)).unwrap();
        assert!(suspect);
    }

    #[test]
    fn none_suspect_preserves_prior_defaulting_false() {
        let (_, _, suspect) =
            exit_status_resolution("failed", None, 1.0, None, Some(true)).unwrap();
        assert!(suspect);
        let (_, _, suspect) = exit_status_resolution("failed", None, 1.0, None, None).unwrap();
        assert!(!suspect);
    }

    #[test]
    fn unknown_state_errs_never_panics() {
        assert!(exit_status_resolution("exploded", None, 1.0, None, None).is_err());
    }

    #[test]
    fn float_bits_pass_through_exactly() {
        let now = f64::from_bits(0x3FF0_0000_0000_0001); // 1.0 + 1 ulp
        let (started, ended, _) = exit_status_resolution("halted", None, now, None, None).unwrap();
        assert_eq!(started.to_bits(), now.to_bits());
        assert_eq!(ended.unwrap().to_bits(), now.to_bits());
    }

    // ── next_ordinal — the monotonic quarantine-dirname fold ──────────

    #[test]
    fn empty_names_yield_one() {
        assert_eq!(next_ordinal(&[], "run-", "").unwrap(), 1);
    }

    #[test]
    fn max_plus_one_over_matching_names() {
        let names = vec![
            "run-1".to_string(),
            "run-7".to_string(),
            "run-3".to_string(),
        ];
        assert_eq!(next_ordinal(&names, "run-", "").unwrap(), 8);
    }

    #[test]
    fn foreign_names_are_skipped() {
        let names = vec![
            "other-9".to_string(), // wrong prefix
            "run-x7".to_string(),  // non-digit core
            "run-".to_string(),    // empty core
            "run-2b".to_string(),  // digit+letter core
            "run-4".to_string(),   // the only participant
            "notes.txt".to_string(),
        ];
        assert_eq!(next_ordinal(&names, "run-", "").unwrap(), 5);
    }

    #[test]
    fn suffix_form_matches_checkpoint_shape() {
        let names = vec![
            "1.json".to_string(),
            "12.json".to_string(),
            "x.json".to_string(),
        ];
        assert_eq!(next_ordinal(&names, "", ".json").unwrap(), 13);
    }

    #[test]
    fn overlapping_prefix_suffix_skips_like_the_python_empty_slice() {
        // name="ab", prefix="ab", suffix="b": starts_with + ends_with both
        // hold but the Python slice name[2:1] is "" → regex-rejected.
        let names = vec!["ab".to_string()];
        assert_eq!(next_ordinal(&names, "ab", "b").unwrap(), 1);
    }

    #[test]
    fn non_ascii_name_errs_never_mismatches_python_unicode_int() {
        // Python's int("٢٢") == 22 — this law refuses instead (the facade
        // gates such names to the oracle before the FFI).
        let names = vec!["run-٢٢".to_string()];
        assert!(next_ordinal(&names, "run-", "").is_err());
        assert!(next_ordinal(&[], "run-ß", "").is_err());
    }

    #[test]
    fn ordinal_overflow_errs_never_panics() {
        let names = vec![format!("run-{}", "9".repeat(30))];
        assert!(next_ordinal(&names, "run-", "").is_err());
        let names = vec![format!("run-{}", u64::MAX)];
        assert!(next_ordinal(&names, "run-", "").is_err()); // max+1 overflows
    }

    // ── checkpoint_record_decision — the never-rewind law ─────────────

    #[test]
    fn record_decision_matrix_exact() {
        assert_eq!(checkpoint_record_decision(-1, -1, 10), "reject_below_zero");
        assert_eq!(
            checkpoint_record_decision(10, -1, 10),
            "reject_beyond_total"
        );
        assert_eq!(
            checkpoint_record_decision(11, -1, 10),
            "reject_beyond_total"
        );
        assert_eq!(checkpoint_record_decision(3, 3, 10), "noop"); // same index
        assert_eq!(checkpoint_record_decision(2, 3, 10), "noop"); // stale rewind
        assert_eq!(checkpoint_record_decision(4, 3, 10), "advance");
        assert_eq!(checkpoint_record_decision(0, -1, 1), "advance"); // fresh store
    }

    #[test]
    fn record_guard_order_below_zero_wins_over_beyond_total() {
        // total_combos == 0: -1 is BOTH < 0 and >= total — the Python guard
        // order checks < 0 first.
        assert_eq!(checkpoint_record_decision(-1, -1, 0), "reject_below_zero");
        assert_eq!(checkpoint_record_decision(0, -1, 0), "reject_beyond_total");
    }

    // ── checkpoint_adoption_verdict — foreign vs tamper ───────────────

    #[test]
    fn adoption_verdict_matrix_exact() {
        assert_eq!(checkpoint_adoption_verdict("a", 10, "b", 10), "foreign");
        // Foreign wins even when the total ALSO disagrees (the Python order).
        assert_eq!(checkpoint_adoption_verdict("a", 10, "b", 99), "foreign");
        assert_eq!(
            checkpoint_adoption_verdict("a", 10, "a", 99),
            "total_mismatch"
        );
        assert_eq!(checkpoint_adoption_verdict("a", 10, "a", 10), "adopt");
    }
}
