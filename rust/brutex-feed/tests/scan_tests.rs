//! Edge-case matrix for the pure decision core (`brutex_feed::scan_bars`) —
//! the cargo half of the DEC-FEED-RUST-CORE-001 bit-identity proof, re-derived
//! for the session-head median-confirm law (DEC-BADTICK-HEAD-SEED-001) + the
//! high/low arm (DEC-BADTICK-HILO-SCREEN-001). Every case transcribes a
//! pinned behaviour of the Python oracle (`brutex/feed/replay.py` +
//! `brutex/data/bad_tick.py::BadTickScreen.screen_mask`); the Python-side
//! hypothesis parity gate proves the two ends agree on generated inputs.
//!
//! Warn events are the 9-int decision-plane records
//! `(kind, emit_at_row, session_first_row, a, b, c, d, e, f)` — see
//! `brutex_feed::WarnEvent`.

use brutex_feed::{
    DAY_US, EVENT_KIND_HEAD_UNCONFIRMED, EVENT_KIND_MAJORITY_DROPPED, EVENT_KIND_SKIP,
    IST_OFFSET_US, MAX_SAFE_TS_US, REASON_CLOSE, REASON_HIGH, REASON_LOW, ScanOutcome, WarnEvent,
    scan_bars,
};

/// 2024-01-02T03:45:00Z (09:15 IST) in epoch microseconds — the same anchor
/// the Python fixtures use.
const SESSION_1: i64 = 1_704_167_100_000_000;

/// One minute in microseconds.
const MINUTE_US: i64 = 60_000_000;

/// Minute-spaced timestamps inside SESSION_1.
fn minutes(n: usize) -> Vec<i64> {
    (0..n as i64).map(|k| SESSION_1 + k * MINUTE_US).collect()
}

/// Run the scan over uniform bars whose four prices all equal `closes[i]`,
/// screen ON at ratio 4.0 — the common shape for close-arm decision tests.
fn scan_uniform(ts: &[i64], closes: &[f64]) -> ScanOutcome {
    scan_bars(ts, closes, closes, closes, closes, true, 4.0)
        .expect("lengths agree")
        .expect("inside the ts envelope")
}

/// Run the scan with distinct high/low columns (closes double as opens),
/// screen ON at ratio 4.0 — the hi/lo-arm shape.
fn scan_hilo(ts: &[i64], closes: &[f64], highs: &[f64], lows: &[f64]) -> ScanOutcome {
    scan_bars(ts, closes, highs, lows, closes, true, 4.0)
        .expect("lengths agree")
        .expect("inside the ts envelope")
}

#[test]
fn empty_input_scans_clean() {
    let out = scan_bars(&[], &[], &[], &[], &[], true, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.kept_rows, Vec::<usize>::new());
    assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
    assert_eq!(out.positivity_stop, None);
}

#[test]
fn single_bar_kept_no_witness() {
    // A 1-bar head has no witness — silent keep, no event.
    let out = scan_uniform(&[SESSION_1], &[27_940.0]);
    assert_eq!(out.kept_rows, vec![0]);
    assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
    assert_eq!(out.positivity_stop, None);
}

#[test]
fn two_bar_consistent_head_silent_keep() {
    let out = scan_uniform(&minutes(2), &[100.0, 101.0]);
    assert_eq!(out.kept_rows, vec![0, 1]);
    assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
}

#[test]
fn two_bar_inconsistent_head_unconfirmed_keeps_both() {
    // DEC-BADTICK-HEAD-SEED-001: a 2-bar session with mutually-out-of-band
    // closes has no median witness — BOTH kept, ONE disclosure event at the
    // finish anchor. The exact boundary (4x) is bad both directions.
    let up = scan_uniform(&minutes(2), &[100.0, 400.0]);
    assert_eq!(up.kept_rows, vec![0, 1]);
    assert_eq!(
        up.warn_events,
        vec![(EVENT_KIND_HEAD_UNCONFIRMED, 2, 0, 0, 1, 0, 0, 0, 0)]
    );
    let down = scan_uniform(&minutes(2), &[400.0, 100.0]);
    assert_eq!(down.kept_rows, vec![0, 1]);
    assert_eq!(
        down.warn_events,
        vec![(EVENT_KIND_HEAD_UNCONFIRMED, 2, 0, 0, 1, 0, 0, 0, 0)]
    );
}

#[test]
fn two_bar_unconfirmed_anchors_at_next_session_first_row() {
    // Session 1 = the unconfirmable pair; session 2 opens at row 2 — the
    // disclosure anchors THERE, not at n.
    let ts = [SESSION_1, SESSION_1 + MINUTE_US, SESSION_1 + DAY_US];
    let out = scan_uniform(&ts, &[27_940.0, 2_794.0, 6_000.0]);
    assert_eq!(out.kept_rows, vec![0, 1, 2]);
    assert_eq!(
        out.warn_events,
        vec![(EVENT_KIND_HEAD_UNCONFIRMED, 2, 0, 0, 1, 0, 0, 0, 0)]
    );
}

#[test]
fn exact_boundary_is_bad_post_head_both_directions() {
    // bad_tick.py::is_bad_tick — the boundary (exactly max_ratio x) is BAD
    // (>= / <=), judged post-head against the median-seeded baseline
    // (median row 1 under duplicate closes — the (value, row) tie law).
    let up = scan_uniform(&minutes(4), &[100.0, 100.0, 100.0, 400.0]);
    assert_eq!(up.kept_rows, vec![0, 1, 2]);
    assert_eq!(
        up.warn_events,
        vec![(EVENT_KIND_SKIP, 4, 0, 1, 1, 3, REASON_CLOSE, 0, 0)]
    );
    let down = scan_uniform(&minutes(4), &[400.0, 400.0, 400.0, 100.0]);
    assert_eq!(down.kept_rows, vec![0, 1, 2]);
    assert_eq!(
        down.warn_events,
        vec![(EVENT_KIND_SKIP, 4, 0, 1, 1, 3, REASON_CLOSE, 0, 0)]
    );
}

#[test]
fn just_inside_boundary_is_good() {
    let up = scan_uniform(&minutes(4), &[100.0, 100.0, 100.0, 399.99]);
    assert_eq!(up.kept_rows, vec![0, 1, 2, 3]);
    let down = scan_uniform(&minutes(4), &[400.0, 400.0, 400.0, 100.01]);
    assert_eq!(down.kept_rows, vec![0, 1, 2, 3]);
}

#[test]
fn custom_max_ratio_arms() {
    // Mirrors test_data_bad_tick.py's max_ratio 2.0 / 3.0 arms (post-head).
    let ts = minutes(4);
    let closes = [100.0, 100.0, 100.0, 250.0];
    let strict = scan_bars(&ts, &closes, &closes, &closes, &closes, true, 2.0)
        .unwrap()
        .unwrap();
    assert_eq!(strict.kept_rows, vec![0, 1, 2]); // 250 >= 100*2 — bad at 2.0
    let loose = scan_bars(&ts, &closes, &closes, &closes, &closes, true, 3.0)
        .unwrap()
        .unwrap();
    assert_eq!(loose.kept_rows, vec![0, 1, 2, 3]); // 250 < 100*3 — good at 3.0
}

#[test]
fn poison_head_bar_dropped_by_median_vote() {
    // DEC-BADTICK-HEAD-SEED-001 headline: a x10 poison FIRST bar no longer
    // seats the baseline and collapses its session — the median (row 2 of
    // the sorted trio) votes it out. first_ref_row = the MEDIAN row (2),
    // which lies AFTER first_skip_row (0).
    let out = scan_uniform(&minutes(4), &[279_400.0, 27_940.0, 27_941.0, 27_942.0]);
    assert_eq!(out.kept_rows, vec![1, 2, 3]);
    assert_eq!(
        out.warn_events,
        vec![(EVENT_KIND_SKIP, 4, 0, 1, 2, 0, REASON_CLOSE, 0, 0)]
    );
}

#[test]
fn escape_band_head_spike_kept_but_never_seats_baseline() {
    // The #1811 phase-1 blindness probe [4, 4.05, 15.9, 16.5]: the head
    // spike 15.9 survives the median vote (15.9 < 4.05*4 = 16.2) but the
    // baseline SEEDS from the median (4.05 @ row 1) — so 16.5 is
    // out-of-band (16.5 >= 16.2) and drops.
    let out = scan_uniform(&minutes(4), &[4.0, 4.05, 15.9, 16.5]);
    assert_eq!(out.kept_rows, vec![0, 1, 2]);
    assert_eq!(
        out.warn_events,
        vec![(EVENT_KIND_SKIP, 4, 0, 1, 1, 3, REASON_CLOSE, 0, 0)]
    );
}

#[test]
fn corrupt_majority_head_wins_documented_tradeoff() {
    // The recorded head-vote trade-off: when 2 of the first 3 bars agree at
    // the CORRUPT level, the majority wins — the lone sane bar (and its
    // sane successor) drop. Session accounting: 4 judged rows, 2 skipped,
    // 2 kept — NOT a strict majority, so no MAJORITY_DROPPED event.
    let out = scan_uniform(&minutes(4), &[27_940.0, 2_794.0, 2_795.0, 27_950.0]);
    assert_eq!(out.kept_rows, vec![1, 2]);
    assert_eq!(
        out.warn_events,
        vec![(EVENT_KIND_SKIP, 4, 0, 2, 2, 0, REASON_CLOSE, 0, 0)]
    );
}

#[test]
fn head_median_tie_breaks_by_row() {
    // Duplicate closes: the (value, row) lexicographic sort makes row 1 the
    // median under a full tie — pinned so both arms name the same
    // reference row.
    let out = scan_uniform(&minutes(4), &[100.0, 100.0, 100.0, 500.0]);
    assert_eq!(
        out.warn_events,
        vec![(EVENT_KIND_SKIP, 4, 0, 1, 1, 3, REASON_CLOSE, 0, 0)]
    );
}

#[test]
fn kept_post_head_bar_advances_baseline() {
    // A kept post-head close becomes the new reference: 1300 would be bad
    // vs the head median (100*4 = 400) but is judged vs the advanced 350.
    let out = scan_uniform(&minutes(5), &[100.0, 100.0, 100.0, 350.0, 1_300.0]);
    assert_eq!(out.kept_rows, vec![0, 1, 2, 3, 4]);
    assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
}

#[test]
fn skipped_bar_never_advances_baseline() {
    // Post-head spike skipped; the next bar is judged against the last KEPT
    // close (the median row 1), not the spike.
    let out = scan_uniform(&minutes(5), &[100.0, 100.0, 100.0, 500.0, 120.0]);
    assert_eq!(out.kept_rows, vec![0, 1, 2, 4]);
    assert_eq!(
        out.warn_events,
        vec![(EVENT_KIND_SKIP, 5, 0, 1, 1, 3, REASON_CLOSE, 0, 0)]
    );
}

#[test]
fn in_head_skip_ref_row_is_median_row() {
    // A head bar voted out carries the MEDIAN row as its reference: trio
    // sorted [(2794,1), (27940,0), (27946,2)] → median (27940, row 0).
    let out = scan_uniform(&minutes(3), &[27_940.0, 2_794.0, 27_946.0]);
    assert_eq!(out.kept_rows, vec![0, 2]);
    assert_eq!(
        out.warn_events,
        vec![(EVENT_KIND_SKIP, 3, 0, 1, 0, 1, REASON_CLOSE, 0, 0)]
    );
}

#[test]
fn high_arm_drops_scaled_high_on_sane_close() {
    // DEC-BADTICK-HILO-SCREEN-001 (DATA-3b): a x10 HIGH with a sane close
    // drops the WHOLE bar, reason HIGH, tallied in n_high.
    let closes = [100.0, 101.0, 102.0, 103.0];
    let highs = [101.0, 102.0, 1_020.0, 104.0];
    let lows = [99.0, 100.0, 101.0, 102.0];
    let out = scan_hilo(&minutes(4), &closes, &highs, &lows);
    assert_eq!(out.kept_rows, vec![0, 1, 3]);
    assert_eq!(
        out.warn_events,
        vec![(EVENT_KIND_SKIP, 4, 0, 1, 1, 2, REASON_HIGH, 1, 0)]
    );
}

#[test]
fn low_arm_drops_scaled_low_on_sane_close_and_high() {
    // A crushed LOW (20 vs the 101 reference: 20*4 <= 101) with sane close
    // + high — reason LOW, tallied in n_low.
    let closes = [100.0, 101.0, 102.0, 103.0];
    let highs = [101.0, 102.0, 103.0, 104.0];
    let lows = [99.0, 100.0, 20.0, 102.0];
    let out = scan_hilo(&minutes(4), &closes, &highs, &lows);
    assert_eq!(out.kept_rows, vec![0, 1, 3]);
    assert_eq!(
        out.warn_events,
        vec![(EVENT_KIND_SKIP, 4, 0, 1, 1, 2, REASON_LOW, 0, 1)]
    );
}

#[test]
fn reason_precedence_close_beats_high_beats_low() {
    // PINNED precedence close → high → low: a bar with BOTH a scaled close
    // and a scaled high counts once as CLOSE; a sane-close bar with both
    // high AND low out-of-band (the "fat range") counts once as HIGH.
    let closes = [100.0, 101.0, 102.0, 500.0];
    let highs = [101.0, 102.0, 103.0, 2_000.0];
    let lows = [99.0, 100.0, 101.0, 102.0];
    let out = scan_hilo(&minutes(4), &closes, &highs, &lows);
    assert_eq!(
        out.warn_events,
        vec![(EVENT_KIND_SKIP, 4, 0, 1, 1, 3, REASON_CLOSE, 0, 0)]
    );
    let closes = [100.0, 101.0, 102.0, 103.0];
    let highs = [101.0, 102.0, 103.0, 2_000.0];
    let lows = [99.0, 100.0, 101.0, 10.0];
    let out = scan_hilo(&minutes(4), &closes, &highs, &lows);
    assert_eq!(
        out.warn_events,
        vec![(EVENT_KIND_SKIP, 4, 0, 1, 1, 3, REASON_HIGH, 1, 0)]
    );
}

#[test]
fn exactly_three_bar_session_emptied_by_hilo_arm() {
    // The documented honest limit: an EXACTLY-3-bar session whose every
    // head bar carries an out-of-band high is fully emptied (the close
    // vote alone can never empty a session — the median survives — but the
    // hi/lo arm judges the median bar's OWN high too). Strict majority
    // (3 > 0) → the MAJORITY_DROPPED disclosure follows at the same anchor.
    let closes = [100.0, 100.0, 100.0];
    let highs = [1_000.0, 1_000.0, 1_000.0];
    let lows = [99.0, 99.0, 99.0];
    let out = scan_hilo(&minutes(3), &closes, &highs, &lows);
    assert_eq!(out.kept_rows, Vec::<usize>::new());
    assert_eq!(
        out.warn_events,
        vec![
            (EVENT_KIND_SKIP, 3, 0, 3, 1, 0, REASON_HIGH, 3, 0),
            (EVENT_KIND_MAJORITY_DROPPED, 3, 0, 3, 0, 0, 0, 0, 0),
        ]
    );
}

#[test]
fn majority_dropped_fires_on_strict_majority_only() {
    // 7 judged rows, 4 skipped > 3 kept → SKIP + MAJORITY_DROPPED at one
    // shared anchor (the non-decreasing anchor law).
    let out = scan_uniform(
        &minutes(7),
        &[100.0, 101.0, 102.0, 405.0, 410.0, 420.0, 430.0],
    );
    assert_eq!(out.kept_rows, vec![0, 1, 2]);
    assert_eq!(
        out.warn_events,
        vec![
            (EVENT_KIND_SKIP, 7, 0, 4, 1, 3, REASON_CLOSE, 0, 0),
            (EVENT_KIND_MAJORITY_DROPPED, 7, 0, 4, 3, 0, 0, 0, 0),
        ]
    );
    // Exactly half (2 skipped, 2 kept) is NOT a strict majority.
    let out = scan_uniform(&minutes(4), &[100.0, 101.0, 405.0, 410.0]);
    assert_eq!(out.kept_rows.len(), 2);
    assert_eq!(out.warn_events.len(), 1);
    assert_eq!(out.warn_events[0].0, EVENT_KIND_SKIP);
}

#[test]
fn reopen_gap_across_sessions_never_trips() {
    // 27940 close, then a new IST date opening at 2000 — two 1-bar
    // sessions, each headless-silent (test_data_bad_tick.py reopen-gap pin).
    let ts = [SESSION_1, SESSION_1 + DAY_US];
    let out = scan_uniform(&ts, &[27_940.0, 2_000.0]);
    assert_eq!(out.kept_rows, vec![0, 1]);
    assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
}

#[test]
fn per_session_events_boundary_and_finish() {
    // Post-head skips in BOTH sessions: session 1's WARN anchors at the
    // boundary row (4), session 2's at n — non-decreasing anchors.
    let mut ts = minutes(4);
    for k in 0..4 {
        ts.push(SESSION_1 + DAY_US + k * MINUTE_US);
    }
    let closes = [
        100.0, 100.0, 100.0, 500.0, // session 1: head + spike
        6_000.0, 6_000.0, 6_000.0, 100.0, // session 2: head + crash
    ];
    let out = scan_uniform(&ts, &closes);
    assert_eq!(out.kept_rows, vec![0, 1, 2, 4, 5, 6]);
    assert_eq!(
        out.warn_events,
        vec![
            (EVENT_KIND_SKIP, 4, 0, 1, 1, 3, REASON_CLOSE, 0, 0),
            (EVENT_KIND_SKIP, 8, 4, 1, 5, 7, REASON_CLOSE, 0, 0),
        ]
    );
}

#[test]
fn session_flip_flop_backwards_dates_resets_each_time() {
    // ANY date inequality resets — including going backwards (the oracle's
    // session key comparison is not sortedness-aware).
    let ts = [SESSION_1, SESSION_1 + DAY_US, SESSION_1];
    let out = scan_uniform(&ts, &[100.0, 5_000.0, 100.0]);
    // Every bar is the first of "its" session after a flip — all kept.
    assert_eq!(out.kept_rows, vec![0, 1, 2]);
    assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
}

#[test]
fn positivity_stop_field_order_open_high_low_close() {
    let ts = [SESSION_1];
    // Each case corrupts ONE field (plus later ones stay positive) and pins
    // the named field index — the oracle's per-field check order.
    let cases: [(f64, f64, f64, f64, usize); 4] = [
        (-1.0, 2.0, 1.0, 1.5, 0),
        (2.0, -1.0, 1.0, 1.5, 1),
        (2.0, 3.0, -1.0, 1.5, 2),
        (2.0, 3.0, 1.0, -1.5, 3),
    ];
    for (o, h, l, c, field) in cases {
        let out = scan_bars(&ts, &[o], &[h], &[l], &[c], true, 4.0)
            .unwrap()
            .unwrap();
        assert_eq!(out.positivity_stop, Some((0, field)), "field {field}");
        assert_eq!(out.kept_rows, Vec::<usize>::new());
        assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
    }
    // A bar corrupt in EVERY field names the FIRST (open).
    let out = scan_bars(&ts, &[-1.0], &[-1.0], &[-1.0], &[-1.0], true, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.positivity_stop, Some((0, 0)));
}

#[test]
fn negative_zero_and_neg_inf_refuse_positivity() {
    // -0.0 <= 0.0 is true (IEEE) — refused, exactly like CPython.
    let out = scan_bars(&[SESSION_1], &[-0.0], &[1.0], &[1.0], &[1.0], true, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.positivity_stop, Some((0, 0)));
    let out = scan_bars(
        &[SESSION_1],
        &[1.0],
        &[1.0],
        &[f64::NEG_INFINITY],
        &[1.0],
        true,
        4.0,
    )
    .unwrap()
    .unwrap();
    assert_eq!(out.positivity_stop, Some((0, 2)));
}

#[test]
fn stop_suppresses_same_row_boundary_flush_and_finish() {
    // Session 1 is an unconfirmable 2-bar head; row 2 opens a new session
    // but fails POSITIVITY — the oracle raises BEFORE the screen consult
    // would flush session 1's disclosure, so the kernel records NO event at
    // the stop row and NO finish event. The pending head rows stay in
    // kept_rows (their verdict never resolved false — downstream-
    // unobservable, the raise discards the bars).
    let ts = [SESSION_1, SESSION_1 + MINUTE_US, SESSION_1 + DAY_US];
    let closes = [27_940.0, 2_794.0, -5.0];
    let out = scan_bars(&ts, &closes, &closes, &closes, &closes, true, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.positivity_stop, Some((2, 0)));
    assert_eq!(out.kept_rows, vec![0, 1]);
    assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
}

#[test]
fn stop_mid_session_suppresses_finish_only() {
    // A post-head skip then an in-session positivity stop: verdicts strictly
    // before the stop row survive in the mask; the owed finish flush is NOT
    // recorded (no SKIP event for the session's tally).
    let ts = minutes(5);
    let closes = [100.0, 100.0, 100.0, 500.0, -5.0];
    let out = scan_bars(&ts, &closes, &closes, &closes, &closes, true, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.positivity_stop, Some((4, 0)));
    assert_eq!(out.kept_rows, vec![0, 1, 2]);
    assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
}

#[test]
fn nan_close_kept_unjudged_and_excluded_from_references() {
    // Non-finite robustness (the #1897 composition): a NaN close is KEPT
    // UNJUDGED and excluded from the head vote — the two finite closes form
    // a 2-bar pending head, mutually out-of-band → HEAD_UNCONFIRMED (the
    // pre-port law kept NaN as a baseline POISON that silently blinded the
    // rest of the session).
    let ts = minutes(3);
    let closes = [100.0, f64::NAN, 12_345.6];
    let out = scan_bars(&ts, &closes, &[1e9; 3], &[1e-9; 3], &closes, true, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.kept_rows, vec![0, 1, 2]);
    assert_eq!(
        out.warn_events,
        vec![(EVENT_KIND_HEAD_UNCONFIRMED, 3, 0, 0, 2, 0, 0, 0, 0)]
    );
    assert_eq!(out.positivity_stop, None);
}

#[test]
fn nan_close_excluded_from_head_median_and_tallies() {
    // Finite rows 0/2/3 form the head (median row 2 @ 101); the NaN row is
    // outside session_rows so the 1-skip / 3-kept tally has no majority.
    let ts = minutes(5);
    let closes = [100.0, f64::NAN, 101.0, 102.0, 500.0];
    let out = scan_bars(&ts, &closes, &closes, &closes, &closes, true, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.kept_rows, vec![0, 1, 2, 3]);
    assert_eq!(
        out.warn_events,
        vec![(EVENT_KIND_SKIP, 5, 0, 1, 2, 4, REASON_CLOSE, 0, 0)]
    );
}

#[test]
fn plus_inf_close_kept_unjudged() {
    // +inf close passes positivity, is non-finite → kept unjudged; the lone
    // finite head bar has no witness — silent keep. (Production never sees
    // this: the eligibility gate degrades non-finite columns to the oracle,
    // whose finiteness gate raises.)
    let ts = minutes(2);
    let closes = [100.0, f64::INFINITY];
    let out = scan_bars(&ts, &closes, &closes, &closes, &closes, true, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.kept_rows, vec![0, 1]);
    assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
}

#[test]
fn plus_inf_high_dropped_by_hilo_arm() {
    // A +inf HIGH on a finite sane close rides the ordinary predicate
    // (inf >= ref*4) — dropped, reason HIGH. Mirrors screen_mask exactly;
    // production-unreachable at this boundary (eligibility gate).
    let closes = [100.0, 101.0, 102.0, 103.0];
    let highs = [101.0, 102.0, f64::INFINITY, 104.0];
    let lows = [99.0, 100.0, 101.0, 102.0];
    let out = scan_hilo(&minutes(4), &closes, &highs, &lows);
    assert_eq!(out.kept_rows, vec![0, 1, 3]);
    assert_eq!(
        out.warn_events,
        vec![(EVENT_KIND_SKIP, 4, 0, 1, 1, 2, REASON_HIGH, 1, 0)]
    );
}

#[test]
fn prev_times_ratio_overflow_to_inf_arm() {
    // prev * 4.0 overflows to +inf post-head: `close >= inf` is false and
    // `close * 4.0 <= prev` is false — the successor is KEPT (identical in
    // CPython; floats saturate, never raise).
    let closes = [f64::MAX, f64::MAX, f64::MAX, 1.0e308];
    let out = scan_uniform(&minutes(4), &closes);
    assert_eq!(out.kept_rows, vec![0, 1, 2, 3]);
    assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
}

#[test]
fn ist_midnight_boundary_day_ordinals() {
    // 18:29:59.999999Z is IST 23:59:59.999999 (same day); 18:30:00Z is IST
    // 00:00:00 (NEXT day) — the div_euclid boundary. A reopen gap across it
    // is exempt (new session); the same gap inside one session lands in the
    // 2-bar unconfirmable disclosure (no median witness).
    let day0_2330 = DAY_US - IST_OFFSET_US - 1; // 1970-01-01T18:29:59.999999Z
    let day1_0000 = DAY_US - IST_OFFSET_US; // 1970-01-01T18:30:00Z
    let out = scan_uniform(&[day0_2330, day1_0000], &[27_940.0, 2_000.0]);
    assert_eq!(
        out.kept_rows,
        vec![0, 1],
        "midnight crossing = new session, exempt"
    );
    assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
    let out = scan_uniform(&[day0_2330 - MINUTE_US, day0_2330], &[27_940.0, 2_000.0]);
    assert_eq!(out.kept_rows, vec![0, 1], "same IST day — unconfirmable");
    assert_eq!(
        out.warn_events,
        vec![(EVENT_KIND_HEAD_UNCONFIRMED, 2, 0, 0, 1, 0, 0, 0, 0)]
    );
    // Post-head, the same-day gap TRIPS (the original teeth, head-resolved).
    let ts = [
        day0_2330 - 3 * MINUTE_US,
        day0_2330 - 2 * MINUTE_US,
        day0_2330 - MINUTE_US,
        day0_2330,
    ];
    let out = scan_uniform(&ts, &[27_940.0, 27_940.0, 27_940.0, 2_000.0]);
    assert_eq!(out.kept_rows, vec![0, 1, 2]);
    assert_eq!(
        out.warn_events,
        vec![(EVENT_KIND_SKIP, 4, 0, 1, 1, 3, REASON_CLOSE, 0, 0)]
    );
}

#[test]
fn epoch_zero_and_envelope_top_are_in_window() {
    let out = scan_uniform(&[0], &[100.0]);
    assert_eq!(out.kept_rows, vec![0]);
    let out = scan_uniform(&[MAX_SAFE_TS_US - 1], &[100.0]);
    assert_eq!(out.kept_rows, vec![0]);
}

#[test]
fn abstain_on_negative_ts_and_at_bound() {
    assert_eq!(
        scan_bars(&[-1], &[1.0], &[1.0], &[1.0], &[1.0], true, 4.0).unwrap(),
        None
    );
    assert_eq!(
        scan_bars(&[MAX_SAFE_TS_US], &[1.0], &[1.0], &[1.0], &[1.0], true, 4.0).unwrap(),
        None
    );
    // Lazy: rows BEFORE the offending timestamp were already decided, but
    // the whole call abstains — nothing partial leaks out.
    assert_eq!(
        scan_bars(
            &[SESSION_1, -1],
            &[1.0, 1.0],
            &[1.0, 1.0],
            &[1.0, 1.0],
            &[1.0, 1.0],
            true,
            4.0
        )
        .unwrap(),
        None
    );
}

#[test]
fn positivity_precedes_abstain_on_the_same_row() {
    // The oracle checks positivity BEFORE astimezone — a row that is both
    // non-positive AND out-of-envelope STOPS (ValueError in Python), never
    // abstains into the oracle's OverflowError path.
    let out = scan_bars(&[-1], &[-1.0], &[1.0], &[1.0], &[1.0], true, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.positivity_stop, Some((0, 0)));
}

#[test]
fn screen_disabled_ignores_ts_entirely() {
    // ts_us MUST be [] with the screen off; a spike is kept; positivity
    // still stops.
    let closes = [100.0, 1_000_000.0];
    let out = scan_bars(&[], &closes, &closes, &closes, &closes, false, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.kept_rows, vec![0, 1]);
    assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
    let out = scan_bars(
        &[],
        &[1.0, -1.0],
        &[1.0; 2],
        &[1.0; 2],
        &[1.0; 2],
        false,
        4.0,
    )
    .unwrap()
    .unwrap();
    assert_eq!(out.positivity_stop, Some((1, 0)));
}

#[test]
fn length_mismatches_are_loud_errors() {
    assert!(scan_bars(&[], &[1.0], &[1.0, 2.0], &[1.0], &[1.0], false, 4.0).is_err());
    assert!(scan_bars(&[], &[1.0], &[1.0], &[1.0, 2.0], &[1.0], false, 4.0).is_err());
    assert!(scan_bars(&[], &[1.0], &[1.0], &[1.0], &[1.0, 2.0], false, 4.0).is_err());
    // ts length must equal n when the screen is ON …
    assert!(
        scan_bars(
            &[SESSION_1],
            &[1.0, 2.0],
            &[1.0, 2.0],
            &[1.0, 2.0],
            &[1.0, 2.0],
            true,
            4.0
        )
        .is_err()
    );
    // … and must be EMPTY when the screen is OFF.
    assert!(scan_bars(&[SESSION_1], &[1.0], &[1.0], &[1.0], &[1.0], false, 4.0).is_err());
}
