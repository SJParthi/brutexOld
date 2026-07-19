//! Property suite for the tripwire laws T2-T6 + T9 — never-panics over
//! raw-bit f64s × gated i64 ranges, plus the per-law invariants
//! (DEC-RUST-OBSERVABILITY-TRIPWIRES-002 §5).

use brutex_observability::{
    CounterOverflow, RssOverflow, TW_CLEAR, TW_FIRE, TW_NONE, normalized_ru_maxrss_bytes,
    tripwire_memory_budget, tripwire_memory_slope, tripwire_reconcile_counts,
    tripwire_stage_missing, tripwire_summary_classify, tripwire_throughput_watch,
};
use proptest::prelude::*;

/// Every f64 bit pattern — NaNs (all payloads), ±inf, ±0.0, subnormals.
fn raw_f64() -> impl Strategy<Value = f64> {
    any::<u64>().prop_map(f64::from_bits)
}

/// A small token vocabulary plus arbitrary strings (unicode included).
fn token() -> impl Strategy<Value = String> {
    prop_oneof![
        Just("stage2".to_string()),
        Just("cascade_manifest".to_string()),
        Just("caf\u{e9}".to_string()),
        any::<String>(),
    ]
}

proptest! {
    /// T2 never panics over the FULL i64 plane and the bitmask equals the
    /// per-bit reference boolean expressions (i128 reference for bit 4).
    #[test]
    fn t2_bitmask_matches_reference(
        in_flight in any::<i64>(),
        enumerated in any::<i64>(),
        evaluated in any::<i64>(),
        skipped in any::<i64>(),
        survivors in any::<i64>(),
        total in any::<i64>(),
        prev in proptest::option::of(any::<i64>()),
        is_done in any::<bool>(),
    ) {
        let mask = tripwire_reconcile_counts(
            in_flight, enumerated, evaluated, skipped, survivors, total, prev, is_done,
        );
        let mut expected: u32 = 0;
        expected |= u32::from(in_flight < 0);
        expected |= u32::from(enumerated != total) << 1;
        expected |= u32::from(skipped > evaluated) << 2;
        expected |= u32::from(survivors > evaluated) << 3;
        expected |=
            u32::from(i128::from(evaluated) + i128::from(in_flight) > i128::from(total)) << 4;
        expected |= u32::from(matches!(prev, Some(p) if evaluated < p)) << 5;
        expected |= u32::from(is_done && evaluated != total) << 6;
        prop_assert_eq!(mask, expected);
        prop_assert!(mask < 1 << 7);
    }

    /// T3 never panics over the raw-f64 plane × gated counters; the state
    /// invariants hold: `new_below == 0 ⟺ code == CLEAR` in the
    /// positive-peak arm, `code == FIRE ⟹ new_below == dwell`, and the
    /// zero-peak arm carries `below` through untouched with code NONE.
    #[test]
    fn t3_law_invariants(
        rate in raw_f64(),
        peak in raw_f64(),
        below in 0_i64..=(i64::MAX - 1),
        fraction in raw_f64(),
        dwell in 1_i64..=i64::MAX,
    ) {
        let (new_peak, new_below, floor, code) =
            tripwire_throughput_watch(rate, peak, below, fraction, dwell)
                .expect("below < i64::MAX never overflows");
        // The peak fold is the argument-order max mirror.
        let expected_peak = if rate > peak { rate } else { peak };
        prop_assert_eq!(new_peak.to_bits(), expected_peak.to_bits());
        if new_peak <= 0.0 {
            prop_assert_eq!((new_below, floor.to_bits(), code), (below, 0.0_f64.to_bits(), TW_NONE));
        } else {
            prop_assert_eq!(floor.to_bits(), (fraction * new_peak).to_bits());
            if rate < floor {
                prop_assert_eq!(new_below, below + 1);
                prop_assert_eq!(code, if new_below == dwell { TW_FIRE } else { TW_NONE });
            } else {
                prop_assert_eq!((new_below, code), (0, TW_CLEAR));
            }
        }
    }

    /// T3's sole Err arm is exactly the below-floor increment at i64::MAX.
    #[test]
    fn t3_err_arm_is_exactly_the_saturated_counter(
        rate in raw_f64(),
        peak in raw_f64(),
        fraction in raw_f64(),
        dwell in 1_i64..=i64::MAX,
    ) {
        let result = tripwire_throughput_watch(rate, peak, i64::MAX, fraction, dwell);
        let new_peak = if rate > peak { rate } else { peak };
        let expect_err = new_peak > 0.0 && rate < fraction * new_peak;
        prop_assert_eq!(result.is_err(), expect_err);
        if let Err(err) = result {
            prop_assert_eq!(err, CounterOverflow);
        }
    }

    /// T4a never panics; the budget/code pair matches the reference law
    /// bitwise (i64→f64 conversion + strict `>`).
    #[test]
    fn t4a_budget_matches_reference(
        rss in any::<i64>(),
        ram_total in any::<i64>(),
        fraction in raw_f64(),
    ) {
        let (budget, code) = tripwire_memory_budget(rss, ram_total, fraction);
        let expected = fraction * (ram_total as f64);
        prop_assert_eq!(budget.to_bits(), expected.to_bits());
        prop_assert_eq!(code, if (rss as f64) > expected { TW_FIRE } else { TW_CLEAR });
    }

    /// T4b never panics over the FULL i64 plane (i128 diffs); the dwell
    /// invariants mirror T3's and the over arm never clears.
    #[test]
    fn t4b_law_invariants(
        rss in any::<i64>(),
        evaluated in any::<i64>(),
        prev_rss in any::<i64>(),
        prev_evaluated in any::<i64>(),
        threshold in raw_f64(),
        high in 0_i64..=(i64::MAX - 1),
        dwell in 1_i64..=i64::MAX,
    ) {
        let (slope, new_high, code) = tripwire_memory_slope(
            rss, evaluated, prev_rss, prev_evaluated, threshold, high, dwell,
        )
        .expect("high < i64::MAX never overflows");
        let expected_slope = ((i128::from(rss) - i128::from(prev_rss)) as f64)
            / ((i128::from(evaluated) - i128::from(prev_evaluated)) as f64);
        prop_assert_eq!(slope.to_bits(), expected_slope.to_bits());
        if slope > threshold {
            prop_assert_eq!(new_high, high + 1);
            prop_assert_eq!(code, if new_high == dwell { TW_FIRE } else { TW_NONE });
        } else {
            prop_assert_eq!((new_high, code), (0, TW_CLEAR));
        }
    }

    /// T5 never panics; lengths are preserved, the code vocabulary is
    /// closed, and every element matches the per-element reference law.
    #[test]
    fn t5_classification_matches_reference(
        entries in proptest::collection::vec((raw_f64(), any::<bool>()), 0..12),
        threshold in raw_f64(),
    ) {
        let values: Vec<f64> = entries.iter().map(|(v, _)| *v).collect();
        let screened: Vec<bool> = entries.iter().map(|(_, s)| *s).collect();
        let (nan, absurd) =
            tripwire_summary_classify(&values, &screened, threshold).expect("aligned");
        prop_assert_eq!(nan.len(), values.len());
        prop_assert_eq!(absurd.len(), values.len());
        for i in 0..values.len() {
            let v = values[i];
            if !v.is_finite() {
                prop_assert_eq!((nan[i], absurd[i]), (TW_FIRE, TW_NONE));
            } else {
                prop_assert_eq!(nan[i], TW_CLEAR);
                let expected = if screened[i] {
                    if v.abs() > threshold { TW_FIRE } else { TW_CLEAR }
                } else {
                    TW_NONE
                };
                prop_assert_eq!(absurd[i], expected);
            }
        }
    }

    /// T6 never panics over arbitrary unicode token lists; the mask equals
    /// per-token non-membership and preserves the required order/length.
    #[test]
    fn t6_mask_matches_membership(
        required in proptest::collection::vec(token(), 0..6),
        present in proptest::collection::vec(token(), 0..6),
    ) {
        let mask = tripwire_stage_missing(&required, &present);
        prop_assert_eq!(mask.len(), required.len());
        for (tok, missing) in required.iter().zip(mask.iter()) {
            prop_assert_eq!(*missing, !present.contains(tok));
        }
    }

    /// T9 never panics over the FULL i64 plane × arbitrary platform tokens;
    /// darwin is verbatim, everything else is checked × 1024 with the Err
    /// arm exactly at |value| beyond i64::MAX / 1024.
    #[test]
    fn t9_platform_law(ru_maxrss in any::<i64>(), platform in token()) {
        let result = normalized_ru_maxrss_bytes(ru_maxrss, &platform);
        if platform == "darwin" {
            prop_assert_eq!(result, Ok(ru_maxrss));
        } else {
            match ru_maxrss.checked_mul(1024) {
                Some(product) => prop_assert_eq!(result, Ok(product)),
                None => prop_assert_eq!(result, Err(RssOverflow)),
            }
        }
    }

    /// Determinism — bitwise field-by-field over repeated calls (the NaN
    /// `PartialEq` lesson from the detector suite).
    #[test]
    fn tripwire_laws_deterministic(
        rate in raw_f64(),
        peak in raw_f64(),
        below in 0_i64..=(i64::MAX - 1),
        fraction in raw_f64(),
        dwell in 1_i64..=i64::MAX,
    ) {
        let a = tripwire_throughput_watch(rate, peak, below, fraction, dwell).expect("in-domain");
        let b = tripwire_throughput_watch(rate, peak, below, fraction, dwell).expect("in-domain");
        prop_assert_eq!(a.0.to_bits(), b.0.to_bits());
        prop_assert_eq!(a.1, b.1);
        prop_assert_eq!(a.2.to_bits(), b.2.to_bits());
        prop_assert_eq!(a.3, b.3);
    }
}
