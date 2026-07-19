//! Unit gates for the series decision arithmetic
//! (`DEC-FEED-RUST-MATERIALIZE-002`) — hand-derived instants against the
//! deleted Python bodies' documented laws. The cross-language
//! differential (hypothesis vs the frozen in-test Python oracle) lives in
//! `tests/unit/test_feed_series_load.py`.

use brutex_feed::{
    DAY_US, IST_OFFSET_US, MAX_SAFE_TS_US, boundary_monotonic_break, expected_bar_instant,
    ownership_cut,
};

/// Midnight IST on 2024-01-02 in epoch µs (2024-01-01T18:30:00Z).
const MIDNIGHT_IST: i64 = 1_704_133_800_000_000;
/// 09:15 IST on 2024-01-02.
const OPEN: i64 = MIDNIGHT_IST + (9 * 60 + 15) * 60_000_000;
/// 15:30 IST on 2024-01-02.
const CLOSE: i64 = MIDNIGHT_IST + (15 * 60 + 30) * 60_000_000;
const MIN_US: i64 = 60_000_000;

// ── expected_bar_instant — intraday grid arm ────────────────────────────

#[test]
fn degenerate_window_is_false_even_over_a_session() {
    assert!(!expected_bar_instant(
        OPEN + 10 * MIN_US,
        OPEN,
        MIN_US,
        CLOSE,
        &[(MIDNIGHT_IST, OPEN, CLOSE)],
    ));
}

#[test]
fn window_containing_open_label_is_true() {
    assert!(expected_bar_instant(
        MIDNIGHT_IST,
        OPEN,
        MIN_US,
        CLOSE,
        &[(MIDNIGHT_IST, OPEN, CLOSE)],
    ));
}

#[test]
fn pre_open_sliver_holds_no_grid_label() {
    // [midnight → 09:14 IST] ends before the first 1-minute label (09:15).
    assert!(!expected_bar_instant(
        MIDNIGHT_IST,
        OPEN - MIN_US,
        MIN_US,
        CLOSE,
        &[(MIDNIGHT_IST, OPEN, CLOSE)],
    ));
}

#[test]
fn post_close_sliver_holds_no_grid_label() {
    // [15:30 → 16:00 IST): labels live in [open, close) — 15:30 is past
    // the last 1-minute label 15:29.
    assert!(!expected_bar_instant(
        CLOSE,
        CLOSE + 30 * MIN_US,
        MIN_US,
        CLOSE + DAY_US,
        &[(MIDNIGHT_IST, OPEN, CLOSE)],
    ));
}

#[test]
fn coarse_tf_fragment_between_labels_is_false() {
    // 4hour grid: labels at 09:15 + k·4h → 09:15 and 13:15 only. A
    // 13:30 → 23:59 fragment overlaps the session but holds no label
    // (the review round-3 finding-7 shape).
    let step = 4 * 60 * MIN_US;
    let frag_start = MIDNIGHT_IST + (13 * 60 + 30) * 60_000_000;
    assert!(!expected_bar_instant(
        frag_start,
        MIDNIGHT_IST + DAY_US - 1,
        step,
        CLOSE + DAY_US,
        &[(MIDNIGHT_IST, OPEN, CLOSE)],
    ));
    // …but a fragment reaching back to 13:15 holds that label.
    assert!(expected_bar_instant(
        MIDNIGHT_IST + (13 * 60 + 15) * 60_000_000,
        MIDNIGHT_IST + DAY_US - 1,
        step,
        CLOSE + DAY_US,
        &[(MIDNIGHT_IST, OPEN, CLOSE)],
    ));
}

#[test]
fn ceil_division_lands_on_the_next_grid_label() {
    // seg_start = 09:15:30 (sub-minute): first 1-minute label at/after is
    // 09:16 — inside a window ending 09:16 → true; ending 09:15:59 → false.
    let half_min = 30_000_000;
    assert!(expected_bar_instant(
        OPEN + half_min,
        OPEN + MIN_US,
        MIN_US,
        CLOSE,
        &[(MIDNIGHT_IST, OPEN, CLOSE)],
    ));
    assert!(!expected_bar_instant(
        OPEN + half_min,
        OPEN + MIN_US - 1,
        MIN_US,
        CLOSE,
        &[(MIDNIGHT_IST, OPEN, CLOSE)],
    ));
}

// ── expected_bar_instant — derived-1day arm (step_us == 0) ──────────────

#[test]
fn one_day_deep_history_with_stamp_in_window_is_true() {
    // Close already passed on the wall clock (now > close): the prebuild
    // owes the bar — an in-window midnight stamp is expectable.
    assert!(expected_bar_instant(
        MIDNIGHT_IST,
        MIDNIGHT_IST + 60 * MIN_US,
        0,
        CLOSE + DAY_US, // now, well past close
        &[(MIDNIGHT_IST, OPEN, CLOSE)],
    ));
}

#[test]
fn one_day_true_live_edge_is_false() {
    // Window ends before the close AND the close is still in the future
    // (the morning-after-expiry sliver — review round-6 finding 3).
    assert!(!expected_bar_instant(
        MIDNIGHT_IST,
        MIDNIGHT_IST + 60 * MIN_US,
        0,
        OPEN, // now: mid-morning, close still ahead
        &[(MIDNIGHT_IST, OPEN, CLOSE)],
    ));
}

#[test]
fn one_day_window_reaching_close_is_true_even_live() {
    assert!(expected_bar_instant(
        MIDNIGHT_IST,
        CLOSE,
        0,
        OPEN, // now before close — but the window reaches the close
        &[(MIDNIGHT_IST, OPEN, CLOSE)],
    ));
}

#[test]
fn one_day_stamp_outside_window_is_false() {
    assert!(!expected_bar_instant(
        MIDNIGHT_IST + MIN_US,
        CLOSE + DAY_US,
        0,
        CLOSE + 2 * DAY_US,
        &[(MIDNIGHT_IST, OPEN, CLOSE)],
    ));
}

#[test]
fn first_qualifying_session_wins_across_days() {
    // Day 1 holds no label for the fragment; day 2 does.
    let d2 = MIDNIGHT_IST + DAY_US;
    assert!(expected_bar_instant(
        CLOSE, // after day-1 close
        d2 + (9 * 60 + 20) * 60_000_000,
        MIN_US,
        d2 + DAY_US,
        &[
            (MIDNIGHT_IST, OPEN, CLOSE),
            (d2, OPEN + DAY_US, CLOSE + DAY_US),
        ],
    ));
}

#[test]
fn empty_sessions_is_false() {
    assert!(!expected_bar_instant(OPEN, CLOSE, MIN_US, CLOSE, &[]));
}

// ── ownership_cut ───────────────────────────────────────────────────────

#[test]
fn ownership_cut_keeps_only_owned_ist_days() {
    let day_ord = (MIDNIGHT_IST + IST_OFFSET_US).div_euclid(DAY_US);
    let ts = vec![
        OPEN - DAY_US,     // day before → out
        OPEN,              // owned day → in
        CLOSE,             // owned day → in
        OPEN + DAY_US,     // day after → out
        OPEN + 2 * DAY_US, // two after → out
    ];
    assert_eq!(ownership_cut(&ts, day_ord, day_ord), Some(vec![1, 2]));
}

#[test]
fn ownership_cut_ist_midnight_boundary_is_exact() {
    // 2024-01-01T18:30:00Z == 2024-01-02T00:00 IST — owned by day 2; one
    // microsecond earlier belongs to day 1 (the exact civil-date law).
    let d2 = (MIDNIGHT_IST + IST_OFFSET_US).div_euclid(DAY_US);
    assert_eq!(ownership_cut(&[MIDNIGHT_IST], d2, d2), Some(vec![0]));
    assert_eq!(ownership_cut(&[MIDNIGHT_IST - 1], d2, d2), Some(vec![]));
    assert_eq!(
        ownership_cut(&[MIDNIGHT_IST - 1], d2 - 1, d2 - 1),
        Some(vec![0])
    );
}

#[test]
fn ownership_cut_abstains_outside_the_envelope() {
    assert_eq!(ownership_cut(&[-1], 0, 1_000_000), None);
    assert_eq!(ownership_cut(&[MAX_SAFE_TS_US], 0, i64::MAX), None);
    // …and the abstain is total: a good row before the bad one does not
    // rescue the call (the caller must refuse the WHOLE cut).
    assert_eq!(ownership_cut(&[OPEN, -1], 0, i64::MAX), None);
}

// ── boundary monotonicity ───────────────────────────────────────────────

#[test]
fn boundary_monotonic_break_law() {
    assert!(boundary_monotonic_break(100, 100)); // equal — broken
    assert!(boundary_monotonic_break(100, 99)); // backwards — broken
    assert!(!boundary_monotonic_break(100, 101)); // strictly after — fine
}
