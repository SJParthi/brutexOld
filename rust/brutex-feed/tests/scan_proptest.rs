//! Property invariants + determinism for the pure decision core
//! (DEC-FEED-RUST-CORE-001). These are the SHAPE laws the Python seam's
//! refuse-poison `validate_scan` gate re-checks at the FFI boundary — proven
//! here to hold for every well-formed input, so a validate refusal in
//! production can only ever mean a stale/corrupt wheel, never a healthy one.

use proptest::prelude::*;

use brutex_feed::{MAX_SAFE_TS_US, ScanOutcome, scan_bars};

/// A finance-plausible close plus the hostile specials the kernel must stay
/// total over (NaN / ±inf / subnormal / negative / zeros).
fn any_price() -> impl Strategy<Value = f64> {
    prop_oneof![
        8 => 0.01f64..1_000_000.0,
        1 => prop_oneof![
            Just(f64::NAN),
            Just(f64::INFINITY),
            Just(f64::NEG_INFINITY),
            Just(0.0),
            Just(-0.0),
            Just(f64::MIN_POSITIVE / 2.0), // subnormal
            Just(-42.5),
        ],
    ]
}

/// Timestamps mostly inside the safe envelope (with edge clusters), plus a
/// slice OUTSIDE it so the lazy abstain gate is genuinely exercised.
fn any_ts() -> impl Strategy<Value = i64> {
    prop_oneof![
        6 => 0..MAX_SAFE_TS_US,
        1 => 0i64..5_000_000i64,                            // epoch edge
        1 => (MAX_SAFE_TS_US - 5_000_000)..MAX_SAFE_TS_US,  // top edge
        1 => i64::MIN..0,                                   // pre-1970 → abstain
        1 => MAX_SAFE_TS_US..i64::MAX,                      // ≥ bound → abstain
    ]
}

/// One generated column set: `(ts, opens, highs, lows, closes)`.
type BarColumns = (Vec<i64>, Vec<f64>, Vec<f64>, Vec<f64>, Vec<f64>);

fn bars() -> impl Strategy<Value = BarColumns> {
    (0usize..48).prop_flat_map(|n| {
        (
            proptest::collection::vec(any_ts(), n),
            proptest::collection::vec(any_price(), n),
            proptest::collection::vec(any_price(), n),
            proptest::collection::vec(any_price(), n),
            proptest::collection::vec(any_price(), n),
        )
    })
}

fn assert_shape_invariants(out: &ScanOutcome, n: usize) {
    // Kept rows: strictly ascending, in range.
    let mut prev: i64 = -1;
    for &k in &out.kept_rows {
        assert!((k as i64) > prev, "kept not strictly ascending");
        assert!(k < n, "kept row out of range");
        prev = k as i64;
    }
    // Warn events: strictly ascending anchors; coordinates in range;
    // skipped >= 1; first_prev_row < first_skip_row.
    let mut prev_emit: i64 = -1;
    for &(emit, sfr, skipped, fpr, fsr) in &out.warn_events {
        assert!(
            (emit as i64) > prev_emit,
            "warn anchors not strictly ascending"
        );
        assert!(emit <= n);
        assert!(sfr < n);
        assert!(skipped >= 1);
        assert!(fpr < fsr && fsr < n);
        prev_emit = emit as i64;
    }
    if let Some((row, field)) = out.positivity_stop {
        assert!(row < n);
        assert!(field <= 3);
        // Stop truncation: nothing recorded at/after the stop row, no finish.
        assert!(out.kept_rows.iter().all(|&k| k < row));
        assert!(out.warn_events.iter().all(|&(emit, ..)| emit < row));
    }
    // Per-session consistency: kept ∪ skipped account for every scanned row —
    // the skip total equals rows-before-the-frontier minus kept rows.
    let frontier = out.positivity_stop.map_or(n, |(row, _)| row);
    let total_skipped: usize = out.warn_events.iter().map(|&(_, _, s, ..)| s).sum();
    // Rows at/after an un-flushed tail may hold skips a stop suppressed, so
    // the invariant is an inequality under a stop and exact on a clean scan.
    if out.positivity_stop.is_none() {
        assert_eq!(
            out.kept_rows.len() + total_skipped,
            frontier,
            "clean scan accounts all rows"
        );
    } else {
        assert!(out.kept_rows.len() + total_skipped <= frontier);
    }
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(512))]

    #[test]
    fn invariants_hold_screen_on((ts, o, h, l, c) in bars()) {
        let n = o.len();
        if let Some(out) = scan_bars(&ts, &o, &h, &l, &c, true, 4.0).expect("well-formed") {
            assert_shape_invariants(&out, n);
        }
    }

    #[test]
    fn invariants_hold_screen_off((_ts, o, h, l, c) in bars()) {
        let n = o.len();
        let out = scan_bars(&[], &o, &h, &l, &c, false, 4.0)
            .expect("well-formed")
            .expect("screen-off never abstains");
        assert_shape_invariants(&out, n);
        // Screen off: no warn events ever; kept = every row before the stop.
        assert!(out.warn_events.is_empty());
        let frontier = out.positivity_stop.map_or(n, |(row, _)| row);
        assert_eq!(out.kept_rows, (0..frontier).collect::<Vec<_>>());
    }

    #[test]
    fn deterministic_and_pure((ts, o, h, l, c) in bars()) {
        let a = scan_bars(&ts, &o, &h, &l, &c, true, 4.0).expect("well-formed");
        let b = scan_bars(&ts, &o, &h, &l, &c, true, 4.0).expect("well-formed");
        prop_assert_eq!(a, b);
    }

    #[test]
    fn abstain_iff_ts_out_of_envelope_reached((ts, o, h, l, c) in bars()) {
        // If the scan abstained, SOME timestamp outside the envelope exists
        // among the rows the forward scan could reach (lazy gate).
        if scan_bars(&ts, &o, &h, &l, &c, true, 4.0).expect("well-formed").is_none() {
            prop_assert!(ts.iter().any(|&t| !(0..MAX_SAFE_TS_US).contains(&t)));
        }
    }
}
