//! Hand-derived edge matrix — one Rust row per Python pin class
//! (DEC-RUST-OBSERVABILITY-001 §8.2).
//!
//! Law A mirrors `tests/unit/test_observability_loop_watchdog.py::TestCheckOnce`
//! (the full transition matrix, exact values), plus the junk-tier /
//! NaN-threshold / signed-zero rows the Python suite reaches only through
//! the opaque-token law. Law B mirrors
//! `test_observability_watchdog.py::TestCheckOnceTransitions`'s decision
//! rows; law C mirrors `test_observability_sla.py::TestPredicateHolds`'s
//! 9-row matrix + the unknown-token fallthrough; D1 is exhaustive over all
//! 64 boolean combinations; D2 is the option-arm truth table.

use brutex_observability::{
    EPISODES_OVERFLOW_MSG, EpisodesOverflow, LoopVerdict, TIER_CRITICAL, TIER_OK, TIER_WARNED,
    VERDICT_HASH_MISMATCH, VERDICT_OK, loop_watchdog_check_once, predicate_holds, sla_decide,
    stamp_shape_index, stamp_verdict, watchdog_decide,
};

/// The Python module defaults (loop_watchdog.py constants).
const WARN: f64 = 5.0;
const CRITICAL: f64 = 30.0;
const RELOG: f64 = 60.0;
const REALERT: f64 = 300.0;

/// Law-A helper at the module defaults over a decomposed state. In-domain
/// episodes (< u64::MAX on a warn edge) always return `Ok` — the sole `Err`
/// arm is pinned by `a_episodes_at_u64_max_warn_edge_errors_never_panics`.
#[allow(clippy::too_many_arguments)]
fn check(
    now: f64,
    last_beat: f64,
    tier: &str,
    ssb: f64,
    lla: f64,
    laa: f64,
    episodes: u64,
) -> LoopVerdict {
    loop_watchdog_check_once(
        now, last_beat, tier, ssb, lla, laa, episodes, WARN, CRITICAL, RELOG, REALERT,
    )
    .expect("in-domain law-A call")
}

/// Law-A helper on a FRESH state (the Python `WatchdogState()` default).
fn check_fresh(now: f64, last_beat: f64) -> LoopVerdict {
    check(now, last_beat, TIER_OK, 0.0, 0.0, 0.0, 0)
}

fn no_flags(v: &LoopVerdict) -> bool {
    !(v.warn_edge || v.critical_edge || v.relog || v.realert || v.recovered)
}

// ── Law A — the TestCheckOnce matrix, row for row ────────────────────────

#[test]
fn a_healthy_noop() {
    let v = check_fresh(10.0, 9.0);
    assert_eq!(v.tier, TIER_OK);
    assert_eq!(v.lag_s, 1.0);
    assert!(no_flags(&v));
    assert_eq!(v.stall_total_s, 0.0);
}

#[test]
fn a_non_monotonic_clock_never_negative() {
    let v = check_fresh(5.0, 9.0); // now BEHIND the beat
    assert_eq!(v.lag_s, 0.0);
    assert_eq!(v.tier, TIER_OK);
}

#[test]
fn a_warn_edge_at_threshold() {
    // Thresholds are `>=` — a lag exactly AT 5 s trips (the `>=`→`>`
    // boundary mutant dies here).
    let v = check_fresh(5.0, 0.0);
    assert!(v.warn_edge && !v.critical_edge);
    assert_eq!(v.tier, TIER_WARNED);
    assert_eq!(v.stall_started_beat, 0.0);
    assert_eq!(v.last_logged_at, 5.0);
    assert_eq!(v.episodes, 1);
}

#[test]
fn a_below_warn_threshold_no_edge() {
    let v = check_fresh(4.999, 0.0);
    assert!(!v.warn_edge);
    assert_eq!(v.tier, TIER_OK);
}

#[test]
fn a_straight_to_critical_fires_both_edges() {
    // A starved monitor may observe the lag jumping past 30 s in one
    // sample — one episode, warn AND critical edges together.
    let v = check_fresh(31.0, 0.0);
    assert!(v.warn_edge && v.critical_edge);
    assert_eq!(v.tier, TIER_CRITICAL);
    assert_eq!(v.last_alerted_at, 31.0);
    assert_eq!(v.episodes, 1);
}

#[test]
fn a_warned_to_critical_edge() {
    let s = check_fresh(6.0, 0.0);
    let v = check(
        30.0,
        0.0,
        s.tier,
        s.stall_started_beat,
        s.last_logged_at,
        s.last_alerted_at,
        s.episodes,
    );
    assert!(v.critical_edge && !v.warn_edge);
    assert_eq!(v.tier, TIER_CRITICAL);
    assert_eq!(v.episodes, 1); // SAME episode — no second count
}

#[test]
fn a_relog_cap_while_stalled() {
    // critical raised out of the way so the growing lag stays WARNED-tier.
    let far = 1000.0;
    let s = loop_watchdog_check_once(
        6.0, 0.0, TIER_OK, 0.0, 0.0, 0.0, 0, WARN, far, RELOG, REALERT,
    )
    .expect("in-domain law-A call");
    let quiet = loop_watchdog_check_once(
        6.0 + RELOG - 1.0,
        0.0,
        s.tier,
        s.stall_started_beat,
        s.last_logged_at,
        s.last_alerted_at,
        s.episodes,
        WARN,
        far,
        RELOG,
        REALERT,
    )
    .expect("in-domain law-A call");
    assert!(!quiet.relog); // inside the 60 s window — silent
    let loud = loop_watchdog_check_once(
        6.0 + RELOG,
        0.0,
        quiet.tier,
        quiet.stall_started_beat,
        quiet.last_logged_at,
        quiet.last_alerted_at,
        quiet.episodes,
        WARN,
        far,
        RELOG,
        REALERT,
    )
    .expect("in-domain law-A call");
    assert!(loud.relog); // at the window — re-log with the grown lag
    assert_eq!(loud.last_logged_at, 6.0 + RELOG);
}

#[test]
fn a_realert_cap_while_critical() {
    let s = check_fresh(31.0, 0.0); // alerted at 31.0
    let quiet = check(
        31.0 + REALERT - 1.0,
        0.0,
        s.tier,
        s.stall_started_beat,
        s.last_logged_at,
        s.last_alerted_at,
        s.episodes,
    );
    assert!(!quiet.realert);
    let loud = check(
        31.0 + REALERT,
        0.0,
        quiet.tier,
        quiet.stall_started_beat,
        quiet.last_logged_at,
        quiet.last_alerted_at,
        quiet.episodes,
    );
    assert!(loud.realert);
    assert_eq!(loud.last_alerted_at, 31.0 + REALERT);
}

#[test]
fn a_no_realert_at_warned_tier() {
    let s2 = check_fresh(6.0, 0.0);
    // lag 10 — still WARNED tier (a genuinely-warned-only re-check).
    let v2 = check(
        100.0,
        90.0,
        s2.tier,
        s2.stall_started_beat,
        s2.last_logged_at,
        s2.last_alerted_at,
        s2.episodes,
    );
    assert_eq!(v2.tier, TIER_WARNED);
    assert!(!v2.realert); // re-alerts are CRITICAL-tier only
}

#[test]
fn a_recovery_resets_and_reports_total() {
    let s = check_fresh(6.0, 0.0); // stalled beat = 0.0
    let v = check(
        50.0,
        49.5,
        s.tier,
        s.stall_started_beat,
        s.last_logged_at,
        s.last_alerted_at,
        s.episodes,
    );
    assert!(v.recovered);
    assert_eq!(v.stall_total_s, 49.5); // resumed beat − stalled beat
    assert_eq!(v.tier, TIER_OK);
    assert_eq!(v.episodes, 1); // the counter survives the reset
    assert_eq!(v.stall_started_beat, 0.0);
    assert_eq!(v.last_logged_at, 0.0);
    assert_eq!(v.last_alerted_at, 0.0);
}

#[test]
fn a_recovery_total_never_negative() {
    // A clock oddity — the beat behind the recorded stall start.
    let v = check(50.0, 49.0, TIER_WARNED, 100.0, 0.0, 0.0, 0);
    assert!(v.recovered);
    assert_eq!(v.stall_total_s, 0.0);
}

#[test]
fn a_ok_to_ok_carries_episodes() {
    let v = check(1.0, 0.5, TIER_OK, 0.0, 0.0, 0.0, 3);
    assert_eq!(v.episodes, 3);
    assert!(!v.recovered);
}

// ── Law A — opaque-tier / NaN / signed-zero rows ─────────────────────────

#[test]
fn a_junk_tier_behaves_previously_stalled_non_critical() {
    // The tier compare is OPAQUE — a junk token is neither "ok" (no warn
    // edge) nor "critical" (a critical target still fires the edge).
    let v = check(6.0, 0.0, "bogus", 2.0, 3.0, 4.0, 7);
    assert_eq!(v.tier, TIER_WARNED);
    assert!(!v.warn_edge && !v.critical_edge && !v.relog);
    assert_eq!(v.stall_started_beat, 2.0); // carried — no warn edge
    assert_eq!(v.episodes, 7);
    let vc = check(31.0, 0.0, "bogus", 2.0, 3.0, 4.0, 7);
    assert!(vc.critical_edge && !vc.warn_edge);
    assert_eq!(vc.episodes, 7); // episodes count WARN edges only
}

#[test]
fn a_junk_tier_recovers_to_ok() {
    let v = check(1.0, 0.9, "bogus", 5.0, 1.0, 1.0, 2);
    assert!(v.recovered);
    assert_eq!(v.tier, TIER_OK);
    assert_eq!(v.stall_total_s, 0.0); // max(0.0, 0.9 − 5.0)
    assert_eq!(v.episodes, 2);
}

#[test]
fn a_nan_thresholds_read_ok_tier() {
    // `lag >= NaN` is False on both thresholds → OK target.
    let v = loop_watchdog_check_once(
        100.0,
        0.0,
        TIER_OK,
        0.0,
        0.0,
        0.0,
        0,
        f64::NAN,
        f64::NAN,
        RELOG,
        REALERT,
    )
    .expect("in-domain law-A call");
    assert_eq!(v.tier, TIER_OK);
    assert!(no_flags(&v));
}

#[test]
fn a_nan_now_clamps_lag_to_zero() {
    // d = NaN − the Python `max(0.0, d)` keeps the FIRST operand 0.0.
    let v = check_fresh(f64::NAN, 0.0);
    assert_eq!(v.lag_s, 0.0);
    assert_eq!(v.lag_s.to_bits(), 0.0f64.to_bits()); // +0.0, sign bit clear
    assert_eq!(v.tier, TIER_OK);
}

#[test]
fn a_negative_zero_diff_clamps_to_positive_zero() {
    let v = check_fresh(-0.0, 0.0); // d = −0.0
    assert_eq!(v.lag_s.to_bits(), 0.0f64.to_bits());
}

#[test]
fn a_infinite_lag_is_critical() {
    let v = check_fresh(f64::INFINITY, 0.0);
    assert_eq!(v.tier, TIER_CRITICAL);
    assert!(v.warn_edge && v.critical_edge);
    assert_eq!(v.lag_s, f64::INFINITY);
}

#[test]
fn a_episodes_at_u64_max_warn_edge_errors_never_panics() {
    // The former disclosed overflow-checks PANIC is dead at the root: the
    // warn-edge `episodes + 1` is `checked_add` — at u64::MAX it returns the
    // typed refusal (→ ValueError across the FFI → the facade's degrade set
    // serves the oracle's unbounded-int value) instead of a panic whose
    // per-extension PanicException no name-based belt home could resolve.
    let result = loop_watchdog_check_once(
        5.0,
        0.0,
        TIER_OK,
        0.0,
        0.0,
        0.0,
        u64::MAX,
        WARN,
        CRITICAL,
        RELOG,
        REALERT,
    );
    assert_eq!(result, Err(EpisodesOverflow));
    let message = result.unwrap_err().to_string();
    assert!(message.contains("episodes counter overflow"), "{message}");
    assert_eq!(message, EPISODES_OVERFLOW_MSG);
}

#[test]
fn a_episodes_at_u64_max_without_warn_edge_is_ok_carried() {
    // Only the warn-edge INCREMENT can overflow — an already-stalled tier at
    // u64::MAX carries the counter untouched (Ok), and recovery too.
    let v = check(6.0, 0.0, TIER_WARNED, 0.0, 6.0, 0.0, u64::MAX);
    assert_eq!(v.episodes, u64::MAX);
    assert!(!v.warn_edge);
    let r = check(1.0, 0.9, TIER_WARNED, 0.0, 6.0, 0.0, u64::MAX);
    assert!(r.recovered);
    assert_eq!(r.episodes, u64::MAX);
}

#[test]
fn a_episodes_at_u64_max_minus_one_warn_edge_increments_to_max() {
    // The Ok boundary sibling: checked_add at u64::MAX − 1 still increments.
    let v = check(5.0, 0.0, TIER_OK, 0.0, 0.0, 0.0, u64::MAX - 1);
    assert!(v.warn_edge);
    assert_eq!(v.episodes, u64::MAX);
}

// ── Law B — the watchdog decision rows ───────────────────────────────────

#[test]
fn b_recent_beat_healthy_no_edge() {
    assert_eq!(watchdog_decide(10.0, 9.0, 5.0, false), (1.0, false, false));
}

#[test]
fn b_strict_gt_boundary_exact_at_timeout_not_hung() {
    // STRICT `>` — elapsed EXACTLY equal to the timeout is NOT hung
    // (contrast law A's `>=`; the modules deliberately differ).
    assert_eq!(
        watchdog_decide(15.0, 0.0, 15.0, false),
        (15.0, false, false)
    );
}

#[test]
fn b_hung_fire_with_exact_elapsed() {
    // The Python pin: `last_beat_seconds_ago == 15.0` exactly.
    assert_eq!(watchdog_decide(25.0, 10.0, 5.0, false), (15.0, true, true));
}

#[test]
fn b_steady_hung_no_edge() {
    assert_eq!(watchdog_decide(25.0, 10.0, 5.0, true), (15.0, true, false));
}

#[test]
fn b_recovery_edge() {
    assert_eq!(watchdog_decide(10.0, 9.0, 5.0, true), (1.0, false, true));
}

#[test]
fn b_negative_elapsed_preserved_no_clamp() {
    // NO clamp — negative elapsed is legal and preserved (unlike law A).
    assert_eq!(watchdog_decide(5.0, 9.0, 5.0, false), (-4.0, false, false));
}

#[test]
fn b_nan_elapsed_reads_healthy_and_fires_recovery_edge() {
    let (elapsed, hung, edge) = watchdog_decide(f64::NAN, 0.0, 5.0, true);
    assert!(elapsed.is_nan());
    assert!(!hung);
    assert!(edge); // prev HUNG + NaN-healthy IS a recovery edge
}

#[test]
fn b_signed_zero_elapsed_preserved() {
    let (elapsed, _, _) = watchdog_decide(-0.0, 0.0, 5.0, false);
    assert_eq!(elapsed.to_bits(), (-0.0f64).to_bits()); // −0.0 − 0.0 = −0.0
}

// ── Law C — the TestPredicateHolds 9-row matrix + fallthrough ────────────

#[test]
fn c_predicate_matrix() {
    let rows: [(&str, f64, f64, bool); 9] = [
        ("greater_than", 101.0, 100.0, true),
        ("greater_than", 99.0, 100.0, false),
        ("greater_than", 100.0, 100.0, false),
        ("less_than", 99.0, 100.0, true),
        ("less_than", 101.0, 100.0, false),
        ("less_than", 100.0, 100.0, false),
        ("equals", 100.0, 100.0, true),
        ("equals", 100.0 + 1e-12, 100.0, true),
        ("equals", 100.5, 100.0, false),
    ];
    for (predicate, value, threshold, expected) in rows {
        assert_eq!(
            predicate_holds(predicate, value, threshold),
            expected,
            "row ({predicate}, {value}, {threshold})"
        );
    }
}

#[test]
fn c_unknown_token_takes_the_equals_arm() {
    // The Python law has NO else-raise — ANY unknown token falls through to
    // the EQUALS-epsilon arm (never a refusal).
    assert!(predicate_holds("bogus", 100.0, 100.0));
    assert!(!predicate_holds("bogus", 100.5, 100.0));
    assert!(predicate_holds("", 0.0, 0.0));
}

#[test]
fn c_equals_epsilon_boundary_is_strict() {
    // abs(diff) < 1e-9 — a diff EXACTLY at the epsilon does NOT equal.
    assert!(!predicate_holds("equals", 1e-9, 0.0));
    assert!(predicate_holds("equals", 0.9e-9, 0.0));
    assert!(predicate_holds("equals", -0.9e-9, 0.0)); // abs of the negative side
}

#[test]
fn c_nan_and_inf_rows() {
    assert!(!predicate_holds("greater_than", f64::NAN, 100.0));
    assert!(!predicate_holds("less_than", f64::NAN, 100.0));
    assert!(!predicate_holds("equals", f64::NAN, 100.0));
    assert!(predicate_holds("greater_than", f64::INFINITY, 100.0));
    assert!(!predicate_holds(
        "greater_than",
        f64::INFINITY,
        f64::INFINITY
    ));
    // abs(inf − inf) = NaN < eps is False — IEEE-identical to CPython.
    assert!(!predicate_holds("equals", f64::INFINITY, f64::INFINITY));
}

#[test]
fn c_edge_law_truth_table() {
    assert_eq!(
        sla_decide("greater_than", 101.0, 100.0, false),
        (true, true)
    );
    assert_eq!(
        sla_decide("greater_than", 101.0, 100.0, true),
        (true, false)
    );
    assert_eq!(sla_decide("greater_than", 99.0, 100.0, true), (false, true));
    assert_eq!(
        sla_decide("greater_than", 99.0, 100.0, false),
        (false, false)
    );
}

#[test]
fn c_nan_observation_fires_recovery_edge() {
    // A NaN observation never breaches; from a prior BREACH that IS a
    // recovery edge — mirrored exactly, no NaN special-casing.
    assert_eq!(
        sla_decide("greater_than", f64::NAN, 100.0, true),
        (false, true)
    );
}

// ── Law D1 — exhaustive over all 64 boolean combinations ─────────────────

#[test]
fn d1_all_64_combinations_first_false_wins() {
    for bits in 0u32..64 {
        let flags: Vec<bool> = (0..6).map(|i| bits & (1 << i) != 0).collect();
        let expected = flags
            .iter()
            .position(|ok| !ok)
            .map_or(-1, |index| index as i64);
        assert_eq!(
            stamp_shape_index(flags[0], flags[1], flags[2], flags[3], flags[4], flags[5]),
            expected,
            "bits {bits:#08b}"
        );
    }
}

// ── Law D2 — the option-arm truth table ──────────────────────────────────

#[test]
fn d2_all_equal_no_options_is_ok() {
    assert_eq!(
        stamp_verdict("h", "h", "s", "s", "r", None, None, None),
        VERDICT_OK
    );
}

#[test]
fn d2_recomputed_hash_mismatch() {
    assert_eq!(
        stamp_verdict("h1", "h2", "s", "s", "r", None, None, None),
        VERDICT_HASH_MISMATCH
    );
}

#[test]
fn d2_stage_mismatch() {
    assert_eq!(
        stamp_verdict("h", "h", "swapped", "s", "r", None, None, None),
        VERDICT_HASH_MISMATCH
    );
}

#[test]
fn d2_run_id_checked_only_when_expected_present() {
    // No expected_run_id → a differing run_id is NOT failed on that axis.
    assert_eq!(
        stamp_verdict("h", "h", "s", "s", "stale", None, None, None),
        VERDICT_OK
    );
    assert_eq!(
        stamp_verdict("h", "h", "s", "s", "r", Some("r"), None, None),
        VERDICT_OK
    );
    assert_eq!(
        stamp_verdict("h", "h", "s", "s", "stale", Some("r"), None, None),
        VERDICT_HASH_MISMATCH
    );
}

#[test]
fn d2_snapshot_checked_only_when_both_sides_present() {
    // A stamp WITHOUT the extra is never failed on the snapshot axis
    // (absent evidence is never fabricated) — and vice versa.
    assert_eq!(
        stamp_verdict("h", "h", "s", "s", "r", None, None, Some("snap")),
        VERDICT_OK
    );
    assert_eq!(
        stamp_verdict("h", "h", "s", "s", "r", None, Some("snap"), None),
        VERDICT_OK
    );
    assert_eq!(
        stamp_verdict("h", "h", "s", "s", "r", None, Some("snap"), Some("snap")),
        VERDICT_OK
    );
    assert_eq!(
        stamp_verdict("h", "h", "s", "s", "r", None, Some("old"), Some("new")),
        VERDICT_HASH_MISMATCH
    );
}

#[test]
fn d2_unicode_strings_compare_by_codepoint() {
    // Rust UTF-8 byte == ≡ Python codepoint == for well-formed strings.
    assert_eq!(
        stamp_verdict("héash", "héash", "s", "s", "r", None, None, None),
        VERDICT_OK
    );
    assert_eq!(
        stamp_verdict("héash", "heash", "s", "s", "r", None, None, None),
        VERDICT_HASH_MISMATCH
    );
}
