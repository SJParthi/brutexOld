//! Hand-derived edge matrix for the tripwire laws T2-T6 + T9
//! (DEC-RUST-OBSERVABILITY-TRIPWIRES-002 §5).
//!
//! Row-for-row mirrors of the Python unit-suite matrix classes in
//! `tests/unit/test_observability_run_tripwires.py` (the 7-violation
//! reconcile matrix incl. the exact `evaluated + in_flight == total` legal
//! boundary; the throughput exactly-at-floor / `==`-dwell-edge / past-dwell
//! NONE / NaN-rate-keeps-peak / zero-peak rows; the budget 500-vs-501
//! boundary; the slope exact-at-threshold / recovery-reset rows; the
//! summary nonfinite-continues-absurd-NONE + strict absurd boundary; the
//! stage missing mask; the ru_maxrss darwin/linux/zero + `i64::MAX`
//! `checked_mul` Err rows).

use brutex_observability::{
    COUNTER_OVERFLOW_MSG, CounterOverflow, RSS_OVERFLOW_MSG, RssOverflow, SummaryLenMismatch,
    TW_CLEAR, TW_FIRE, TW_NONE, normalized_ru_maxrss_bytes, tripwire_memory_budget,
    tripwire_memory_slope, tripwire_reconcile_counts, tripwire_stage_missing,
    tripwire_summary_classify, tripwire_throughput_watch,
};

// ── T2 — count reconciliation ─────────────────────────────────────────────

/// A clean snapshot: zero violations.
#[test]
fn t2_clean_snapshot_is_zero_mask() {
    assert_eq!(
        tripwire_reconcile_counts(0, 10, 5, 2, 1, 10, Some(4), false),
        0
    );
}

/// Bit 0 — negative in_flight.
#[test]
fn t2_negative_in_flight_sets_bit_0() {
    assert_eq!(
        tripwire_reconcile_counts(-1, 10, 5, 0, 0, 10, None, false),
        1
    );
}

/// Bit 1 — enumerated drift from the armed total.
#[test]
fn t2_enumerated_drift_sets_bit_1() {
    assert_eq!(
        tripwire_reconcile_counts(0, 9, 5, 0, 0, 10, None, false),
        1 << 1
    );
}

/// Bit 2 — skipped exceeding evaluated (skipped ⊆ evaluated).
#[test]
fn t2_skipped_over_evaluated_sets_bit_2() {
    assert_eq!(
        tripwire_reconcile_counts(0, 10, 5, 6, 0, 10, None, false),
        1 << 2
    );
}

/// Bit 3 — survivors exceeding evaluated.
#[test]
fn t2_survivors_over_evaluated_sets_bit_3() {
    assert_eq!(
        tripwire_reconcile_counts(0, 10, 5, 0, 6, 10, None, false),
        1 << 3
    );
}

/// Bit 4 — evaluated + in_flight overflowing the total (strict `>`); the
/// exact `evaluated + in_flight == total` boundary is LEGAL.
#[test]
fn t2_evaluated_plus_in_flight_over_total_sets_bit_4() {
    assert_eq!(
        tripwire_reconcile_counts(6, 10, 5, 0, 0, 10, None, false),
        1 << 4
    );
    assert_eq!(
        tripwire_reconcile_counts(5, 10, 5, 0, 0, 10, None, false),
        0
    );
}

/// Bit 4's sum runs in i128 — two i64::MAX-class operands can never wrap
/// (the lossless exact-compare law; overflow-checks would panic an i64 sum).
#[test]
fn t2_bit_4_sum_is_lossless_at_i64_max() {
    let mask = tripwire_reconcile_counts(i64::MAX, i64::MAX, i64::MAX, 0, 0, i64::MAX, None, false);
    assert_eq!(mask & (1 << 4), 1 << 4);
    // The negative twin: i64::MIN + i64::MIN never wraps positive.
    let mask = tripwire_reconcile_counts(
        i64::MIN,
        i64::MAX,
        i64::MIN,
        i64::MIN,
        i64::MIN,
        i64::MAX,
        None,
        false,
    );
    assert_eq!(mask & (1 << 4), 0);
}

/// Bit 5 — evaluated going backwards vs a PRESENT prev; a None prev never
/// sets it (the first-sample law).
#[test]
fn t2_backwards_evaluated_sets_bit_5_only_with_prev() {
    assert_eq!(
        tripwire_reconcile_counts(0, 10, 4, 0, 0, 10, Some(5), false),
        1 << 5
    );
    assert_eq!(
        tripwire_reconcile_counts(0, 10, 4, 0, 0, 10, Some(4), false),
        0
    );
    assert_eq!(
        tripwire_reconcile_counts(0, 10, 4, 0, 0, 10, None, false),
        0
    );
}

/// Bit 6 — done with an incomplete evaluated; done-exact is legal.
#[test]
fn t2_done_incomplete_sets_bit_6() {
    assert_eq!(
        tripwire_reconcile_counts(0, 10, 5, 0, 0, 10, None, true),
        1 << 6
    );
    assert_eq!(
        tripwire_reconcile_counts(0, 10, 10, 0, 0, 10, None, true),
        0
    );
}

/// Multiple simultaneous violations OR together in append order.
#[test]
fn t2_multiple_violations_or_together() {
    // in_flight < 0 (bit 0) + enumerated drift (bit 1) + done-incomplete (bit 6).
    assert_eq!(
        tripwire_reconcile_counts(-3, 7, 5, 0, 0, 10, None, true),
        (1 << 0) | (1 << 1) | (1 << 6)
    );
}

// ── T3 — throughput floor/dwell ───────────────────────────────────────────

/// A healthy rate above the floor clears and resets the counter.
#[test]
fn t3_above_floor_clears_and_resets() {
    let (peak, below, floor, code) =
        tripwire_throughput_watch(50.0, 100.0, 3, 0.2, 5).expect("in-domain");
    assert_eq!((peak, below, floor, code), (100.0, 0, 20.0, TW_CLEAR));
}

/// A below-floor sample counts toward the dwell; below the edge it is NONE.
#[test]
fn t3_below_floor_counts_without_firing_before_dwell() {
    let (peak, below, floor, code) =
        tripwire_throughput_watch(10.0, 100.0, 0, 0.2, 5).expect("in-domain");
    assert_eq!((peak, below, floor, code), (100.0, 1, 20.0, TW_NONE));
}

/// The dwell EDGE is `==` — the sample that reaches dwell fires…
#[test]
fn t3_dwell_edge_fires_exactly_at_dwell() {
    let (_, below, _, code) = tripwire_throughput_watch(10.0, 100.0, 4, 0.2, 5).expect("in-domain");
    assert_eq!((below, code), (5, TW_FIRE));
}

/// …and past-dwell samples are NONE (never `>=` — the once-per-edge
/// decision lives in Python's `_edge_fire`).
#[test]
fn t3_past_dwell_is_none_not_refire() {
    let (_, below, _, code) = tripwire_throughput_watch(10.0, 100.0, 5, 0.2, 5).expect("in-domain");
    assert_eq!((below, code), (6, TW_NONE));
}

/// Exactly-at-floor is NOT below (strict `<`) — clears.
#[test]
fn t3_exactly_at_floor_is_not_below() {
    let (_, below, floor, code) =
        tripwire_throughput_watch(20.0, 100.0, 3, 0.2, 5).expect("in-domain");
    assert_eq!((below, floor, code), (0, 20.0, TW_CLEAR));
}

/// A NaN rate keeps the prior peak (the `max(peak, rate)` argument-order
/// mirror) and — NaN < floor being false — clears.
#[test]
fn t3_nan_rate_keeps_peak_and_clears() {
    let (peak, below, _, code) =
        tripwire_throughput_watch(f64::NAN, 100.0, 2, 0.2, 5).expect("in-domain");
    assert_eq!(peak, 100.0);
    assert_eq!((below, code), (0, TW_CLEAR));
}

/// Zero peak (a rate before any positive observation): the peak is WRITTEN,
/// nothing else — below untouched, floor 0.0, code NONE.
#[test]
fn t3_zero_peak_writes_peak_only() {
    let (peak, below, floor, code) =
        tripwire_throughput_watch(0.0, 0.0, 3, 0.2, 5).expect("in-domain");
    assert_eq!((peak, below, floor, code), (0.0, 3, 0.0, TW_NONE));
}

/// A new record rate folds into the peak before the floor compare.
#[test]
fn t3_record_rate_becomes_the_peak() {
    let (peak, below, floor, code) =
        tripwire_throughput_watch(200.0, 100.0, 3, 0.2, 5).expect("in-domain");
    assert_eq!((peak, below, floor, code), (200.0, 0, 40.0, TW_CLEAR));
}

/// The dwell counter at i64::MAX refuses with the typed overflow (never a
/// panic, never a wrap) — the exact message the facade's ValueError carries.
#[test]
fn t3_below_at_i64_max_errors_never_panics() {
    let err = tripwire_throughput_watch(10.0, 100.0, i64::MAX, 0.2, 5).unwrap_err();
    assert_eq!(err, CounterOverflow);
    assert_eq!(err.to_string(), COUNTER_OVERFLOW_MSG);
}

/// i64::MAX − 1 still increments through the checked add.
#[test]
fn t3_below_at_i64_max_minus_one_increments() {
    let (_, below, _, _) =
        tripwire_throughput_watch(10.0, 100.0, i64::MAX - 1, 0.2, 5).expect("in-domain");
    assert_eq!(below, i64::MAX);
}

// ── T4a — memory budget ───────────────────────────────────────────────────

/// The 500-vs-501 boundary: exactly-at-budget is LEGAL (strict `>`).
#[test]
fn t4a_budget_boundary_exact_is_legal() {
    let (budget, code) = tripwire_memory_budget(500, 1000, 0.5);
    assert_eq!((budget, code), (500.0, TW_CLEAR));
    let (budget, code) = tripwire_memory_budget(501, 1000, 0.5);
    assert_eq!((budget, code), (500.0, TW_FIRE));
}

/// A fractional budget compares in f64 exactly as CPython does.
#[test]
fn t4a_fractional_budget_matches_python_float_math() {
    // 0.85 × 48 GiB — the §8 box's default budget shape.
    let ram: i64 = 48 * (1 << 30);
    let budget = 0.85 * (ram as f64);
    let (got, code) = tripwire_memory_budget(ram, ram, 0.85);
    assert_eq!(got, budget);
    assert_eq!(code, TW_FIRE); // rss == ram > 0.85 × ram
}

// ── T4b — memory slope ────────────────────────────────────────────────────

/// Under-slope clears and resets the high counter.
#[test]
fn t4b_under_slope_clears_and_resets() {
    // slope = 1000 / 10 = 100.0 ≤ 200.0.
    let (slope, high, code) =
        tripwire_memory_slope(2000, 20, 1000, 10, 200.0, 3, 5).expect("in-domain");
    assert_eq!((slope, high, code), (100.0, 0, TW_CLEAR));
}

/// Exactly-at-threshold is sane (strict `>`) — clears.
#[test]
fn t4b_exactly_at_threshold_is_sane() {
    let (slope, high, code) =
        tripwire_memory_slope(2000, 20, 1000, 10, 100.0, 3, 5).expect("in-domain");
    assert_eq!((slope, high, code), (100.0, 0, TW_CLEAR));
}

/// Over-slope counts toward the dwell (NONE below the edge, FIRE at `==`,
/// NONE past it — the over arm NEVER clears, mirroring `_slope_breach`).
#[test]
fn t4b_over_slope_dwell_edge_law() {
    let over = |high: i64| tripwire_memory_slope(2000, 20, 1000, 10, 50.0, high, 5);
    let (_, high, code) = over(0).expect("in-domain");
    assert_eq!((high, code), (1, TW_NONE));
    let (_, high, code) = over(4).expect("in-domain");
    assert_eq!((high, code), (5, TW_FIRE));
    let (_, high, code) = over(5).expect("in-domain");
    assert_eq!((high, code), (6, TW_NONE));
}

/// The slope diffs run in i128 — extreme i64 operands never wrap (the
/// facade's 2^53 gate keeps the ROUTED domain exact; this row proves the
/// crate itself is total over raw i64s).
#[test]
fn t4b_extreme_diffs_never_panic() {
    let (slope, _, _) =
        tripwire_memory_slope(i64::MAX, i64::MAX, i64::MIN, 0, 0.0, 0, 5).expect("in-domain");
    assert!(slope.is_finite());
    assert!(slope > 0.0);
}

/// The high counter at i64::MAX refuses with the typed overflow.
#[test]
fn t4b_high_at_i64_max_errors_never_panics() {
    let err = tripwire_memory_slope(2000, 20, 1000, 10, 50.0, i64::MAX, 5).unwrap_err();
    assert_eq!(err, CounterOverflow);
    assert_eq!(err.to_string(), COUNTER_OVERFLOW_MSG);
}

/// The 2^53-exactness fence: within the facade gate the converts-first
/// division equals the correctly-rounded rational (a hand row at the gate
/// boundary — 2^53 / 1).
#[test]
fn t4b_two_pow_53_diff_is_exact() {
    let diff: i64 = 1 << 53;
    let (slope, _, _) = tripwire_memory_slope(diff, 1, 0, 0, 0.0, 0, 5).expect("in-domain");
    assert_eq!(slope, 9007199254740992.0);
}

// ── T5 — summary NaN/absurd screen ────────────────────────────────────────

/// A non-finite value fires the nan slot and leaves the absurd slot NONE
/// (the `continue` mirror — even for a SCREENED name).
#[test]
fn t5_nonfinite_fires_nan_and_skips_absurd() {
    let (nan, absurd) = tripwire_summary_classify(
        &[f64::NAN, f64::INFINITY, f64::NEG_INFINITY],
        &[true; 3],
        10.0,
    )
    .expect("aligned");
    assert_eq!(nan, vec![TW_FIRE, TW_FIRE, TW_FIRE]);
    assert_eq!(absurd, vec![TW_NONE, TW_NONE, TW_NONE]);
}

/// A finite unscreened value clears nan and leaves absurd NONE.
#[test]
fn t5_finite_unscreened_clears_nan_only() {
    let (nan, absurd) = tripwire_summary_classify(&[5.0], &[false], 10.0).expect("aligned");
    assert_eq!(nan, vec![TW_CLEAR]);
    assert_eq!(absurd, vec![TW_NONE]);
}

/// The absurd boundary is STRICT: exactly-at-threshold is sane, both signs
/// (|value| via `f64::abs` — the Python `abs` mirror).
#[test]
fn t5_absurd_strict_boundary_both_signs() {
    let (_, absurd) =
        tripwire_summary_classify(&[10.0, -10.0, 10.5, -10.5], &[true; 4], 10.0).expect("aligned");
    assert_eq!(absurd, vec![TW_CLEAR, TW_CLEAR, TW_FIRE, TW_FIRE]);
}

/// −0.0 is finite and |−0.0| = +0.0 — never absurd at a positive threshold.
#[test]
fn t5_negative_zero_is_sane() {
    let (nan, absurd) = tripwire_summary_classify(&[-0.0], &[true], 10.0).expect("aligned");
    assert_eq!((nan[0], absurd[0]), (TW_CLEAR, TW_CLEAR));
}

/// Codes align with the input order (a mixed batch preserves positions).
#[test]
fn t5_codes_align_with_input_order() {
    let (nan, absurd) =
        tripwire_summary_classify(&[1.0, f64::NAN, 99.0], &[false, true, true], 10.0)
            .expect("aligned");
    assert_eq!(nan, vec![TW_CLEAR, TW_FIRE, TW_CLEAR]);
    assert_eq!(absurd, vec![TW_NONE, TW_NONE, TW_FIRE]);
}

/// Empty batches are legal (both outputs empty).
#[test]
fn t5_empty_batch_is_legal() {
    let (nan, absurd) = tripwire_summary_classify(&[], &[], 10.0).expect("aligned");
    assert!(nan.is_empty() && absurd.is_empty());
}

/// A length mismatch refuses with the typed error (the belt row).
#[test]
fn t5_length_mismatch_refuses() {
    let err = tripwire_summary_classify(&[1.0, 2.0], &[true], 10.0).unwrap_err();
    assert_eq!(err, SummaryLenMismatch);
}

// ── T6 — stage-prerequisite membership ────────────────────────────────────

/// The mask marks exactly the absent required tokens, in required order.
#[test]
fn t6_missing_mask_marks_absent_tokens() {
    let required = vec!["cascade_manifest".to_string(), "cascade_report".to_string()];
    let present = vec!["cascade_manifest".to_string(), "unrelated".to_string()];
    assert_eq!(
        tripwire_stage_missing(&required, &present),
        vec![false, true]
    );
}

/// All present → all-false; empty required → empty mask (vacuous pass).
#[test]
fn t6_all_present_and_empty_required() {
    let required = vec!["stage2".to_string()];
    let present = vec!["stage2".to_string(), "stage3".to_string()];
    assert_eq!(tripwire_stage_missing(&required, &present), vec![false]);
    assert_eq!(tripwire_stage_missing(&[], &present), Vec::<bool>::new());
}

/// Empty present → every required token missing.
#[test]
fn t6_empty_present_marks_all_missing() {
    let required = vec!["a".to_string(), "b".to_string()];
    assert_eq!(tripwire_stage_missing(&required, &[]), vec![true, true]);
}

/// Unicode tokens compare by UTF-8 bytes ≡ Python codepoint equality
/// (composed vs decomposed é are DIFFERENT strings on both sides).
#[test]
fn t6_unicode_tokens_compare_bytewise() {
    let required = vec!["caf\u{e9}".to_string()];
    let decomposed = vec!["cafe\u{301}".to_string()];
    assert_eq!(tripwire_stage_missing(&required, &decomposed), vec![true]);
    let composed = vec!["caf\u{e9}".to_string()];
    assert_eq!(tripwire_stage_missing(&required, &composed), vec![false]);
}

// ── T9 — ru_maxrss normalization ──────────────────────────────────────────

/// darwin reports BYTES — the reading passes through verbatim.
#[test]
fn t9_darwin_is_verbatim_bytes() {
    assert_eq!(normalized_ru_maxrss_bytes(123_456, "darwin"), Ok(123_456));
}

/// Every other platform reports KiB — × 1024.
#[test]
fn t9_linux_multiplies_kib() {
    assert_eq!(normalized_ru_maxrss_bytes(1024, "linux"), Ok(1_048_576));
    assert_eq!(normalized_ru_maxrss_bytes(1, "freebsd14"), Ok(1024));
}

/// Zero and negative readings pass through the law unchanged in shape.
#[test]
fn t9_zero_and_negative_readings() {
    assert_eq!(normalized_ru_maxrss_bytes(0, "linux"), Ok(0));
    assert_eq!(normalized_ru_maxrss_bytes(-2, "linux"), Ok(-2048));
    assert_eq!(normalized_ru_maxrss_bytes(-2, "darwin"), Ok(-2));
}

/// The `checked_mul` Err row: i64::MAX × 1024 refuses (darwin verbatim is
/// still fine at the same magnitude).
#[test]
fn t9_i64_max_kib_errors_never_panics() {
    let err = normalized_ru_maxrss_bytes(i64::MAX, "linux").unwrap_err();
    assert_eq!(err, RssOverflow);
    assert_eq!(err.to_string(), RSS_OVERFLOW_MSG);
    assert_eq!(normalized_ru_maxrss_bytes(i64::MAX, "darwin"), Ok(i64::MAX));
}

/// The largest exactly-representable KiB reading still multiplies.
#[test]
fn t9_max_in_domain_kib_multiplies() {
    let max_kib = i64::MAX / 1024;
    assert_eq!(
        normalized_ru_maxrss_bytes(max_kib, "linux"),
        Ok(max_kib * 1024)
    );
    assert_eq!(
        normalized_ru_maxrss_bytes(max_kib + 1, "linux"),
        Err(RssOverflow)
    );
}
