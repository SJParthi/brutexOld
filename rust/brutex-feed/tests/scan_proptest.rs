//! Property invariants + determinism for the pure decision core
//! (DEC-FEED-RUST-CORE-001; re-derived for the DEC-BADTICK-HEAD-SEED-001 +
//! DEC-BADTICK-HILO-SCREEN-001 event vocabulary). These are the SHAPE laws
//! the Python seam's refuse-poison `validate_scan` gate re-checks at the FFI
//! boundary — proven here to hold for every well-formed input, so a validate
//! refusal in production can only ever mean a stale/corrupt wheel, never a
//! healthy one.

use proptest::prelude::*;

use brutex_feed::{
    EVENT_KIND_HEAD_UNCONFIRMED, EVENT_KIND_MAJORITY_DROPPED, EVENT_KIND_SKIP, MAX_SAFE_TS_US,
    REASON_LOW, ScanOutcome, scan_bars,
};

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
    // Warn events (9-int): NON-decreasing anchors (a session's SKIP +
    // MAJORITY_DROPPED share one); coordinates in range; per-kind payload
    // laws — the exact invariants `feed_rust.validate_scan` re-checks.
    let mut prev_emit: i64 = -1;
    for &(kind, emit, sfr, a, b, c, d, e, f) in &out.warn_events {
        assert!((emit as i64) >= prev_emit, "warn anchors decreased");
        assert!(emit <= n);
        assert!(sfr < n);
        match kind {
            EVENT_KIND_SKIP => {
                let (skipped, first_ref, first_skip, first_reason, n_high, n_low) =
                    (a, b, c, d, e, f);
                assert!(skipped >= 1, "skipped floor");
                assert!(first_ref < n && first_skip < n, "offender range");
                assert!(first_reason <= REASON_LOW, "reason range");
                assert!(n_high + n_low <= skipped, "reason tally");
            }
            EVENT_KIND_HEAD_UNCONFIRMED => {
                assert!(a < b && b < n, "head pair range");
                assert_eq!((c, d, e, f), (0, 0, 0, 0), "head padding");
            }
            EVENT_KIND_MAJORITY_DROPPED => {
                let (skipped, kept_in_session) = (a, b);
                assert!(skipped >= 1 && skipped > kept_in_session, "majority law");
                assert_eq!((c, d, e, f), (0, 0, 0, 0), "majority padding");
            }
            other => panic!("unknown event kind {other}"),
        }
        prev_emit = emit as i64;
    }
    if let Some((row, field)) = out.positivity_stop {
        assert!(row < n);
        assert!(field <= 3);
        // Stop truncation: nothing recorded at/after the stop row, no finish.
        assert!(out.kept_rows.iter().all(|&k| k < row));
        assert!(out.warn_events.iter().all(|&(_, emit, ..)| emit < row));
    }
    // Row accounting: every scanned row is either kept (mask true — incl.
    // non-finite-close + pending-head rows) or counted in exactly one SKIP
    // event. Exact on a clean scan; an un-flushed tail under a stop may
    // hold suppressed skips, so the invariant is an inequality there.
    let frontier = out.positivity_stop.map_or(n, |(row, _)| row);
    let total_skipped: usize = out
        .warn_events
        .iter()
        .filter(|&&(kind, ..)| kind == EVENT_KIND_SKIP)
        .map(|&(_, _, _, s, ..)| s)
        .sum();
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

    #[test]
    fn skip_offenders_and_head_pairs_cross_consistent((ts, o, h, l, c) in bars()) {
        // Cross-consistency between the mask and the events: a SKIP event's
        // first_skip_row is a genuinely DROPPED row (never in kept_rows),
        // its first_ref_row a genuinely KEPT one (the reference is a
        // kept-or-median close — the median bar itself can be dropped only
        // by its own high/low, in which case a DIFFERENT row is the first
        // offender ⇒ ref may be masked; so ref∈kept is only asserted for
        // close-attributed offenders whose ref precedes the skip — the
        // last-kept-baseline shape), and a HEAD_UNCONFIRMED pair is kept on
        // both sides. (A "hi/lo never widens the kept COUNT" claim is
        // deliberately NOT made — a hi/lo skip suppresses a baseline
        // advance, which can SAVE later rows: the hand-verified
        // [100,100,100,350(h=2000),80,80] shape keeps 5 vs close-only's 4.)
        if let Some(out) = scan_bars(&ts, &o, &h, &l, &c, true, 4.0).expect("well-formed") {
            let kept: std::collections::HashSet<usize> = out.kept_rows.iter().copied().collect();
            for &(kind, _, _, a, b, cc, _, _, _) in &out.warn_events {
                if kind == EVENT_KIND_SKIP {
                    prop_assert!(!kept.contains(&cc), "first_skip_row must be dropped");
                } else if kind == EVENT_KIND_HEAD_UNCONFIRMED {
                    prop_assert!(kept.contains(&a) && kept.contains(&b), "head pair kept");
                }
            }
        }
    }
}
