//! Unit + differential gates for `scan_bars_strict`
//! (`DEC-FEED-RUST-MATERIALIZE-002`).
//!
//! The load-bearing gate is the DIFFERENTIAL law: over every ALL-FINITE
//! input, `scan_bars_strict` ≡ `scan_bars` (kept rows, warn events,
//! abstains, and the positivity stop modulo the appended `kind`) — so the
//! two transcriptions of the shared decision plane can never drift. On
//! top: the finiteness-first edge matrix the old kernel never saw
//! (NaN / ±inf per field, finiteness-before-positivity ordering,
//! truncation semantics at a non-finite stop).

use brutex_feed::{MAX_SAFE_TS_US, STOP_NONFINITE, STOP_NONPOSITIVE, scan_bars, scan_bars_strict};
use proptest::prelude::*;

/// 09:15 IST on 2024-01-02, epoch µs (one canonical session anchor).
const BASE: i64 = 1_704_167_100_000_000;
const MIN_US: i64 = 60_000_000;

fn ts(n: usize) -> Vec<i64> {
    (0..n).map(|k| BASE + (k as i64) * MIN_US).collect()
}

// ── Differential vs the frozen scan_bars (finite plane) ─────────────────

fn assert_equiv_on_finite(
    ts_us: &[i64],
    opens: &[f64],
    highs: &[f64],
    lows: &[f64],
    closes: &[f64],
    screen: bool,
    ratio: f64,
) {
    let old = scan_bars(ts_us, opens, highs, lows, closes, screen, ratio).unwrap();
    let new = scan_bars_strict(ts_us, opens, highs, lows, closes, screen, ratio).unwrap();
    match (old, new) {
        (None, None) => {}
        (Some(o), Some(n)) => {
            assert_eq!(o.kept_rows, n.kept_rows);
            assert_eq!(o.warn_events, n.warn_events);
            match (o.positivity_stop, n.stop) {
                (None, None) => {}
                (Some((row, field)), Some((nrow, nfield, kind))) => {
                    assert_eq!((row, field), (nrow, nfield));
                    assert_eq!(kind, STOP_NONPOSITIVE, "finite plane stops are positivity");
                }
                (o, n) => panic!("stop divergence: old={o:?} new={n:?}"),
            }
        }
        (o, n) => panic!("abstain divergence: old={o:?} new={n:?}"),
    }
}

proptest! {
    /// The differential law over the all-finite plane (screen on/off,
    /// mixed positive/non-positive prices, in-envelope timestamps).
    #[test]
    fn strict_equals_frozen_scan_on_finite_inputs(
        rows in proptest::collection::vec(
            (
                1e-3f64..1e6, // open
                1e-3f64..1e6, // high
                1e-3f64..1e6, // low
                1e-3f64..1e6, // close
            ),
            0..40,
        ),
        // A sprinkle of non-positive corruption at a random row/field.
        corrupt in proptest::option::of((0usize..40, 0usize..4, -5.0f64..0.0)),
        screen in any::<bool>(),
        gap_min in 1i64..2000, // stretches rows across session boundaries
    ) {
        let n = rows.len();
        let mut opens: Vec<f64> = rows.iter().map(|r| r.0).collect();
        let mut highs: Vec<f64> = rows.iter().map(|r| r.1).collect();
        let mut lows: Vec<f64> = rows.iter().map(|r| r.2).collect();
        let mut closes: Vec<f64> = rows.iter().map(|r| r.3).collect();
        if let Some((row, field, v)) = corrupt
            && row < n
        {
            match field {
                0 => opens[row] = v,
                1 => highs[row] = v,
                2 => lows[row] = v,
                _ => closes[row] = v,
            }
        }
        let ts_us: Vec<i64> = if screen {
            (0..n).map(|k| BASE + (k as i64) * gap_min * MIN_US).collect()
        } else {
            Vec::new()
        };
        assert_equiv_on_finite(&ts_us, &opens, &highs, &lows, &closes, screen, 4.0);
    }
}

#[test]
fn differential_holds_on_the_spike_shape() {
    // One session, a 10x spike at row 1 → skipped + one finish warn.
    let closes = [100.0, 1000.0, 100.5];
    let flat = [100.0, 100.0, 100.0];
    assert_equiv_on_finite(&ts(3), &flat, &[1e4; 3], &[1.0; 3], &closes, true, 4.0);
    let new = scan_bars_strict(&ts(3), &flat, &[1e4; 3], &[1.0; 3], &closes, true, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(new.kept_rows, vec![0, 2]);
    assert_eq!(new.warn_events, vec![(3, 0, 1, 0, 1)]);
    assert_eq!(new.stop, None);
}

// ── Finiteness-first edge matrix (the new plane) ────────────────────────

#[test]
fn nan_close_stops_nonfinite_at_the_close_field() {
    let out = scan_bars_strict(
        &[],
        &[100.0, 100.0],
        &[101.0, 101.0],
        &[99.0, 99.0],
        &[100.0, f64::NAN],
        false,
        4.0,
    )
    .unwrap()
    .unwrap();
    assert_eq!(out.kept_rows, vec![0]);
    assert_eq!(out.stop, Some((1, 3, STOP_NONFINITE)));
}

#[test]
fn positive_inf_high_stops_nonfinite() {
    let out = scan_bars_strict(
        &[],
        &[100.0],
        &[f64::INFINITY],
        &[99.0],
        &[100.0],
        false,
        4.0,
    )
    .unwrap()
    .unwrap();
    assert_eq!(out.kept_rows, Vec::<usize>::new());
    assert_eq!(out.stop, Some((0, 1, STOP_NONFINITE)));
}

#[test]
fn negative_inf_is_nonfinite_not_nonpositive() {
    // -inf is BOTH non-finite and <= 0 — finiteness is checked FIRST (the
    // deleted oracle's `isfinite` raise precedes the `<= 0` raise), so the
    // kind is NONFINITE, never NONPOSITIVE.
    let out = scan_bars_strict(
        &[],
        &[f64::NEG_INFINITY],
        &[1.0],
        &[1.0],
        &[1.0],
        false,
        4.0,
    )
    .unwrap()
    .unwrap();
    assert_eq!(out.stop, Some((0, 0, STOP_NONFINITE)));
}

#[test]
fn negative_zero_open_refuses_positivity() {
    // -0.0 is finite; `-0.0 <= 0.0` is true — the positivity kind.
    let out = scan_bars_strict(&[], &[-0.0], &[1.0], &[1.0], &[1.0], false, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.stop, Some((0, 0, STOP_NONPOSITIVE)));
}

#[test]
fn field_order_names_the_first_offender() {
    // Both low (NaN) and close (-1.0) offend at row 0 — low (field 2)
    // comes first in the fixed open → high → low → close order.
    let out = scan_bars_strict(&[], &[100.0], &[101.0], &[f64::NAN], &[-1.0], false, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.stop, Some((0, 2, STOP_NONFINITE)));
}

#[test]
fn nonfinite_stop_truncates_pending_warn_and_finish() {
    // Session 1 has a skipped spike (owes a warn); the non-finite row
    // arrives as the FIRST row of session 2 — the oracle raised BEFORE the
    // boundary flush, so no warn is recorded at the stop row.
    let t = vec![BASE, BASE + MIN_US, BASE + 86_400_000_000 + MIN_US];
    let out = scan_bars_strict(
        &t,
        &[100.0, 100.0, 100.0],
        &[1e4, 1e4, 1e4],
        &[1.0, 1.0, 1.0],
        &[100.0, 1000.0, f64::NAN],
        true,
        4.0,
    )
    .unwrap()
    .unwrap();
    assert_eq!(out.kept_rows, vec![0]);
    assert_eq!(
        out.warn_events,
        Vec::<(usize, usize, usize, usize, usize)>::new()
    );
    assert_eq!(out.stop, Some((2, 3, STOP_NONFINITE)));
}

#[test]
fn nonfinite_after_boundary_keeps_the_prior_sessions_flush() {
    // The boundary row itself is CLEAN (flush fires there); the non-finite
    // row comes one bar later — the already-flushed warn survives.
    let t = vec![
        BASE,
        BASE + MIN_US,
        BASE + 86_400_000_000 + MIN_US,
        BASE + 86_400_000_000 + 2 * MIN_US,
    ];
    let out = scan_bars_strict(
        &t,
        &[100.0; 4],
        &[1e4; 4],
        &[1.0; 4],
        &[100.0, 1000.0, 100.0, f64::INFINITY],
        true,
        4.0,
    )
    .unwrap()
    .unwrap();
    assert_eq!(out.kept_rows, vec![0, 2]);
    assert_eq!(out.warn_events, vec![(2, 0, 1, 0, 1)]);
    assert_eq!(out.stop, Some((3, 3, STOP_NONFINITE)));
}

// ── Preconditions + abstain (contract parity with the frozen kernel) ────

#[test]
fn length_mismatch_is_err() {
    assert!(scan_bars_strict(&[], &[1.0], &[1.0, 2.0], &[1.0], &[1.0], false, 4.0).is_err());
    assert!(scan_bars_strict(&[], &[1.0], &[1.0], &[1.0], &[1.0], true, 4.0).is_err());
    assert!(scan_bars_strict(&[BASE], &[1.0], &[1.0], &[1.0], &[1.0], false, 4.0).is_err());
}

#[test]
fn out_of_envelope_ts_abstains() {
    for bad in [-1, MAX_SAFE_TS_US] {
        let out = scan_bars_strict(&[bad], &[1.0], &[1.0], &[1.0], &[1.0], true, 4.0).unwrap();
        assert!(out.is_none(), "ts {bad} must abstain");
    }
}

#[test]
fn screen_off_never_consults_ts() {
    // Empty ts + screen off: every finite positive row keeps.
    let out = scan_bars_strict(&[], &[1.0; 5], &[2.0; 5], &[0.5; 5], &[1.0; 5], false, 4.0)
        .unwrap()
        .unwrap();
    assert_eq!(out.kept_rows, vec![0, 1, 2, 3, 4]);
    assert!(out.warn_events.is_empty());
    assert_eq!(out.stop, None);
}
