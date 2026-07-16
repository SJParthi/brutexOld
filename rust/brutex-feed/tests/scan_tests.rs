//! Edge-case matrix for the pure decision core (`brutex_feed::scan_bars`) —
//! the cargo half of the DEC-FEED-RUST-CORE-001 bit-identity proof. Every
//! case transcribes a pinned behaviour of the Python oracle
//! (`brutex/feed/replay.py` + `brutex/data/bad_tick.py`); the Python-side
//! hypothesis parity gate proves the two ends agree on generated inputs.

use brutex_feed::{DAY_US, IST_OFFSET_US, MAX_SAFE_TS_US, ScanOutcome, WarnEvent, scan_bars};

/// 2024-01-02T03:45:00Z (09:15 IST) in epoch microseconds — the same anchor
/// the Python fixtures use.
const SESSION_1: i64 = 1_704_167_100_000_000;

/// One minute in microseconds.
const MINUTE_US: i64 = 60_000_000;

/// Run the scan over uniform bars whose four prices all equal `closes[i]`,
/// screen ON at ratio 4.0 — the common shape for screen-decision tests.
fn scan_uniform(ts: &[i64], closes: &[f64]) -> ScanOutcome {
    scan_bars(ts, closes, closes, closes, closes, true, 4.0)
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
fn single_bar_kept() {
    let out = scan_uniform(&[SESSION_1], &[27_940.0]);
    assert_eq!(out.kept_rows, vec![0]);
    assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
    assert_eq!(out.positivity_stop, None);
}

#[test]
fn exact_boundary_is_bad_both_directions() {
    // bad_tick.py:131 — the boundary (exactly max_ratio x) is BAD (>= / <=).
    let ts = [SESSION_1, SESSION_1 + MINUTE_US];
    let up = scan_uniform(&ts, &[100.0, 400.0]);
    assert_eq!(up.kept_rows, vec![0]);
    assert_eq!(up.warn_events, vec![(2, 0, 1, 0, 1)]);
    let down = scan_uniform(&ts, &[400.0, 100.0]);
    assert_eq!(down.kept_rows, vec![0]);
    assert_eq!(down.warn_events, vec![(2, 0, 1, 0, 1)]);
}

#[test]
fn just_inside_boundary_is_good() {
    let ts = [SESSION_1, SESSION_1 + MINUTE_US];
    assert_eq!(scan_uniform(&ts, &[100.0, 399.99]).kept_rows, vec![0, 1]);
    assert_eq!(scan_uniform(&ts, &[400.0, 100.01]).kept_rows, vec![0, 1]);
}

#[test]
fn custom_max_ratio_arms() {
    // Mirrors test_data_bad_tick.py's max_ratio 2.0 / 3.0 arms.
    let ts = [SESSION_1, SESSION_1 + MINUTE_US];
    let closes = [100.0, 250.0];
    let strict = scan_bars(&ts, &closes, &closes, &closes, &closes, true, 2.0)
        .unwrap()
        .unwrap();
    assert_eq!(strict.kept_rows, vec![0]); // 250 >= 100*2 — bad at 2.0
    let loose = scan_bars(&ts, &closes, &closes, &closes, &closes, true, 3.0)
        .unwrap()
        .unwrap();
    assert_eq!(loose.kept_rows, vec![0, 1]); // 250 < 100*3 — good at 3.0
}

#[test]
fn first_bar_of_session_wild_value_kept() {
    let out = scan_uniform(&[SESSION_1], &[0.0001_f64.max(2.0)]);
    assert_eq!(out.kept_rows, vec![0]);
}

#[test]
fn reopen_gap_across_sessions_never_trips() {
    // 27940 close, then a new IST date opening at 2000 — first bar of the
    // new session is exempt (test_data_bad_tick.py reopen-gap pin).
    let ts = [SESSION_1, SESSION_1 + DAY_US];
    let out = scan_uniform(&ts, &[27_940.0, 2_000.0]);
    assert_eq!(out.kept_rows, vec![0, 1]);
    assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
}

#[test]
fn skipped_bar_never_advances_baseline() {
    // Spike skipped; the next bar is judged against the last KEPT close
    // (27940), not the spike — so it is kept.
    let ts = [SESSION_1, SESSION_1 + MINUTE_US, SESSION_1 + 2 * MINUTE_US];
    let out = scan_uniform(&ts, &[27_940.0, 2_794.0, 27_946.0]);
    assert_eq!(out.kept_rows, vec![0, 2]);
    assert_eq!(out.warn_events, vec![(3, 0, 1, 0, 1)]);
}

#[test]
fn two_consecutive_spikes_one_event_skipped_2() {
    let ts: Vec<i64> = (0..4).map(|k| SESSION_1 + k * MINUTE_US).collect();
    let out = scan_uniform(&ts, &[27_940.0, 2_794.0, 2_795.0, 27_950.0]);
    assert_eq!(out.kept_rows, vec![0, 3]);
    // ONE event for the session, skipped=2, first offender = (row 0, row 1).
    assert_eq!(out.warn_events, vec![(4, 0, 2, 0, 1)]);
}

#[test]
fn boundary_flush_anchors_at_next_session_first_row() {
    // Session 1 has a skip; session 2 starts at row 2 → the session-1 WARN
    // is anchored at emit_at_row = 2 (the next session's first row, itself
    // kept), NOT at n.
    let ts = [SESSION_1, SESSION_1 + MINUTE_US, SESSION_1 + DAY_US];
    let out = scan_uniform(&ts, &[27_940.0, 2_794.0, 6_000.0]);
    assert_eq!(out.kept_rows, vec![0, 2]);
    assert_eq!(out.warn_events, vec![(2, 0, 1, 0, 1)]);
}

#[test]
fn finish_flush_anchors_at_n() {
    let ts = [SESSION_1, SESSION_1 + MINUTE_US];
    let out = scan_uniform(&ts, &[27_940.0, 2_794.0]);
    assert_eq!(out.kept_rows, vec![0]);
    assert_eq!(out.warn_events, vec![(2, 0, 1, 0, 1)]);
}

#[test]
fn per_session_events_boundary_and_finish() {
    // Skips in BOTH sessions: session 1's WARN anchors at the boundary row,
    // session 2's at n — ascending emit_at_row.
    let ts = [
        SESSION_1,
        SESSION_1 + MINUTE_US,
        SESSION_1 + DAY_US,
        SESSION_1 + DAY_US + MINUTE_US,
    ];
    let out = scan_uniform(&ts, &[27_940.0, 2_794.0, 6_000.0, 600.0]);
    assert_eq!(out.kept_rows, vec![0, 2]);
    assert_eq!(out.warn_events, vec![(2, 0, 1, 0, 1), (4, 2, 1, 2, 3)]);
}

#[test]
fn session_flip_flop_backwards_dates_resets_each_time() {
    // ANY date inequality resets — including going backwards (the oracle's
    // `ist_date != self._session` is not sortedness-aware).
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
    // the named field index — the replay.py:341 check order.
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
    // Session 1 skips a spike; row 2 opens a new session but fails
    // POSITIVITY — the oracle raises BEFORE keep() would flush session 1's
    // WARN, so the kernel records NO event at/after the stop row and NO
    // finish event.
    let ts = [SESSION_1, SESSION_1 + MINUTE_US, SESSION_1 + DAY_US];
    let closes = [27_940.0, 2_794.0, -5.0];
    let out = scan_bars(&ts, &closes, &closes, &closes, &closes, true, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.positivity_stop, Some((2, 0)));
    assert_eq!(out.kept_rows, vec![0]);
    assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
}

#[test]
fn stop_mid_session_suppresses_finish_only() {
    // A skip then an in-session positivity stop: entries strictly before the
    // stop row survive; the owed finish flush is NOT recorded.
    let ts: Vec<i64> = (0..3).map(|k| SESSION_1 + k * MINUTE_US).collect();
    let closes = [27_940.0, 2_794.0, -5.0];
    let out = scan_bars(&ts, &closes, &closes, &closes, &closes, true, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.positivity_stop, Some((2, 0)));
    assert_eq!(out.kept_rows, vec![0]);
    assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
}

#[test]
fn nan_close_kept_and_poisons_baseline() {
    // NaN passes positivity (nan <= 0 is false) and every predicate arm is
    // false against/for it — the bar is KEPT, becomes the baseline, and all
    // subsequent bars are kept too (Python then raises InvalidOperation at
    // construction — the oracle's own behaviour, out of kernel scope).
    let ts: Vec<i64> = (0..3).map(|k| SESSION_1 + k * MINUTE_US).collect();
    let closes = [100.0, f64::NAN, 12_345.6];
    let out = scan_bars(&ts, &closes, &[1e9; 3], &[1e-9; 3], &closes, true, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.kept_rows, vec![0, 1, 2]);
    assert_eq!(out.warn_events, Vec::<WarnEvent>::new());
    assert_eq!(out.positivity_stop, None);
}

#[test]
fn plus_inf_close_skipped_as_up_spike() {
    let ts = [SESSION_1, SESSION_1 + MINUTE_US];
    let closes = [100.0, f64::INFINITY];
    let out = scan_bars(&ts, &closes, &closes, &closes, &closes, true, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.kept_rows, vec![0]);
    assert_eq!(out.warn_events, vec![(2, 0, 1, 0, 1)]);
}

#[test]
fn prev_times_ratio_overflow_to_inf_arm() {
    // prev * 4.0 overflows to +inf: `close >= inf` is false and
    // `close * 4.0 <= prev` is false — the successor is KEPT (identical in
    // CPython; floats saturate, never raise).
    let ts = [SESSION_1, SESSION_1 + MINUTE_US];
    let closes = [f64::MAX, 1.0e308];
    let out = scan_bars(&ts, &closes, &closes, &closes, &closes, true, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.kept_rows, vec![0, 1]);
    // An +inf successor against the huge baseline: inf >= inf — skipped.
    let closes = [f64::MAX, f64::INFINITY];
    let out = scan_bars(&ts, &closes, &closes, &closes, &closes, true, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.kept_rows, vec![0]);
    assert_eq!(out.warn_events, vec![(2, 0, 1, 0, 1)]);
}

#[test]
fn ist_midnight_boundary_day_ordinals() {
    // 18:29:59.999999Z is IST 23:59:59.999999 (same day); 18:30:00Z is IST
    // 00:00:00 (NEXT day) — the div_euclid boundary. A reopen gap across it
    // must be exempt (new session), while the same gap inside one session
    // trips.
    let day0_2330 = DAY_US - IST_OFFSET_US - 1; // 1970-01-01T18:29:59.999999Z
    let day1_0000 = DAY_US - IST_OFFSET_US; // 1970-01-01T18:30:00Z
    let out = scan_uniform(&[day0_2330, day1_0000], &[27_940.0, 2_000.0]);
    assert_eq!(
        out.kept_rows,
        vec![0, 1],
        "midnight crossing = new session, exempt"
    );
    let out = scan_uniform(&[day0_2330 - MINUTE_US, day0_2330], &[27_940.0, 2_000.0]);
    assert_eq!(out.kept_rows, vec![0], "same IST day — the gap trips");
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
