//! Unit + property gates for the P-06 `BarrierState` core
//! (`DEC-FEED-RUST-MATERIALIZE-002`) — the Rust twins of the pinned
//! Python barrier laws (`tests/unit/test_lookahead_barrier.py` +
//! `tests/property/test_lookahead_properties.py`).

use brutex_feed::{
    ACCESS_LOOKAHEAD, ACCESS_NEGATIVE, ACCESS_NO_CURSOR, ACCESS_OK, ADVANCE_BACKWARDS,
    ADVANCE_NEGATIVE, ADVANCE_OK, BarrierState,
};
use proptest::prelude::*;

#[test]
fn fresh_barrier_has_no_cursor() {
    let b = BarrierState::new(true);
    assert_eq!(b.current_index(), -1);
    assert!(b.records());
    assert!(b.touched().is_empty());
    assert_eq!(b.max_touched(), None);
}

#[test]
fn boundary_contract_n_minus_one_n_n_plus_one() {
    let mut b = BarrierState::new(true);
    assert_eq!(b.advance_to(7), ADVANCE_OK);
    assert_eq!(b.access(7), ACCESS_OK); // exactly N
    assert_eq!(b.access(6), ACCESS_OK); // N-1
    assert_eq!(b.access(0), ACCESS_OK); // far back
    assert_eq!(b.access(8), ACCESS_LOOKAHEAD); // N+1
}

#[test]
fn check_order_no_cursor_before_negative() {
    // The deleted oracle checked `current_index < 0` FIRST — a negative
    // index before any advance reads "no bar reached yet", never the
    // negative-index ValueError.
    let mut b = BarrierState::new(true);
    assert_eq!(b.access(-1), ACCESS_NO_CURSOR);
    assert_eq!(b.access(0), ACCESS_NO_CURSOR);
    assert_eq!(b.advance_to(3), ADVANCE_OK);
    assert_eq!(b.access(-1), ACCESS_NEGATIVE);
}

#[test]
fn advance_check_order_negative_before_backwards() {
    let mut b = BarrierState::new(false);
    assert_eq!(b.advance_to(5), ADVANCE_OK);
    assert_eq!(b.advance_to(-1), ADVANCE_NEGATIVE); // negative wins over backwards
    assert_eq!(b.advance_to(4), ADVANCE_BACKWARDS);
    assert_eq!(b.advance_to(5), ADVANCE_OK); // equal move is legal
    assert_eq!(b.current_index(), 5);
}

#[test]
fn rejected_verdicts_never_move_state() {
    let mut b = BarrierState::new(true);
    b.advance_to(2);
    b.access(1);
    assert_eq!(b.access(9), ACCESS_LOOKAHEAD);
    assert_eq!(b.touched(), &[1]); // DEC-LOOKAHEAD-HONESTY-001: 9 absent
    assert_eq!(b.max_touched(), Some(1));
    assert_eq!(b.advance_to(1), ADVANCE_BACKWARDS);
    assert_eq!(b.current_index(), 2); // cursor unmoved on a rejection
}

#[test]
fn record_false_keeps_o1_memory() {
    let mut b = BarrierState::new(false);
    b.advance_to(5);
    assert_eq!(b.access(3), ACCESS_OK);
    assert_eq!(b.access(5), ACCESS_OK);
    assert!(b.touched().is_empty());
    assert_eq!(b.max_touched(), None);
}

#[test]
fn reset_restores_the_just_constructed_form() {
    let mut b = BarrierState::new(true);
    b.advance_to(3);
    b.access(3);
    b.reset();
    assert_eq!(b.current_index(), -1);
    assert!(b.touched().is_empty());
    assert_eq!(b.advance_to(0), ADVANCE_OK);
    assert_eq!(b.access(0), ACCESS_OK);
}

proptest! {
    /// Never-false-negative: any access index > N is a look-ahead verdict
    /// (both record modes).
    #[test]
    fn p06_access_beyond_current_always_lookahead(
        current in 0i64..10_000,
        delta in 1i64..10_000,
        record in any::<bool>(),
    ) {
        let mut b = BarrierState::new(record);
        prop_assert_eq!(b.advance_to(current), ADVANCE_OK);
        prop_assert_eq!(b.access(current + delta), ACCESS_LOOKAHEAD);
    }

    /// Never-false-positive: any access index in [0, N] is OK, and in
    /// record mode enters the trail.
    #[test]
    fn p06_access_within_window_is_ok(current in 0i64..10_000, frac in 0.0f64..=1.0) {
        #[allow(clippy::cast_possible_truncation, clippy::cast_precision_loss)]
        let index = ((current as f64) * frac) as i64;
        let mut b = BarrierState::new(true);
        prop_assert_eq!(b.advance_to(current), ADVANCE_OK);
        prop_assert_eq!(b.access(index), ACCESS_OK);
        prop_assert!(b.touched().contains(&index));
    }

    /// Forward replay touches only [0, n-1]; max ≤ final cursor.
    #[test]
    fn p06_forward_replay_touches_only_past_or_present(n in 1i64..500) {
        let mut b = BarrierState::new(true);
        for index in 0..n {
            prop_assert_eq!(b.advance_to(index), ADVANCE_OK);
            prop_assert_eq!(b.access(index), ACCESS_OK);
        }
        let expected: Vec<i64> = (0..n).collect();
        prop_assert_eq!(b.touched(), expected.as_slice());
        prop_assert_eq!(b.max_touched(), Some(n - 1));
        prop_assert!(b.max_touched().unwrap() <= b.current_index());
    }
}
