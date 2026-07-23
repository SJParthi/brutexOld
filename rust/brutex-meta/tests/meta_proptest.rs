//! Property invariants for the pure meta kernels
//! (DEC-META-LABEL-ANNOTATE-001 + DEC-ABSTAIN-GATE-STREAM-001). These are
//! the shape laws the Python facade's refuse-poison gate re-checks at the
//! FFI boundary — proven here for every well-formed input, so a validate
//! refusal in production can only ever mean a stale/corrupt wheel.

use proptest::prelude::*;

use brutex_meta::{
    BIT_EVENT_DAY, BIT_LOW_CONFIDENCE, BIT_REGIME_MISMATCH, BIT_VIX_MISSING, BIT_VIX_OUT_OF_BAND,
    abstain_masks, score_logistic, sigmoid,
};

/// Finance-plausible values plus hostile specials the kernels must stay
/// total over (NaN / ±inf / subnormal / signed zeros / huge magnitudes).
fn any_f64() -> impl Strategy<Value = f64> {
    prop_oneof![
        8 => -1_000.0f64..1_000.0,
        1 => prop_oneof![
            Just(f64::NAN),
            Just(f64::INFINITY),
            Just(f64::NEG_INFINITY),
            Just(0.0),
            Just(-0.0),
            Just(f64::MIN_POSITIVE / 2.0), // subnormal
            Just(1e300),
            Just(-1e300),
        ],
    ]
}

proptest! {
    /// Finite inputs → every probability is finite and in [0, 1]; the
    /// output length always equals the row count.
    #[test]
    fn finite_inputs_yield_probabilities_in_unit_interval(
        rows in prop::collection::vec(-1_000.0f64..1_000.0, 0..64),
        w in -50.0f64..50.0,
        bias in -50.0f64..50.0,
    ) {
        let out = score_logistic(&rows, 1, &[w], bias).expect("valid shape");
        prop_assert_eq!(out.len(), rows.len());
        for p in out {
            prop_assert!(p.is_finite() && (0.0..=1.0).contains(&p));
        }
    }

    /// Determinism: the same batch twice is bit-identical (no hidden state).
    #[test]
    fn scorer_is_deterministic(
        rows in prop::collection::vec(any_f64(), 0..48),
        w in any_f64(),
        bias in any_f64(),
    ) {
        let a = score_logistic(&rows, 1, &[w], bias).expect("valid shape");
        let b = score_logistic(&rows, 1, &[w], bias).expect("valid shape");
        let bits =
            |v: &[f64]| v.iter().map(|x| x.to_bits()).collect::<Vec<_>>();
        prop_assert_eq!(bits(&a), bits(&b));
    }

    /// Batch scoring equals per-row scoring bit-for-bit (no cross-row state).
    #[test]
    fn batch_equals_per_row(
        rows in prop::collection::vec(any_f64(), 0..32),
        w0 in any_f64(),
        w1 in any_f64(),
        bias in any_f64(),
    ) {
        // Build 2-feature rows from consecutive pairs (drop a ragged tail).
        let flat: Vec<f64> = rows.chunks_exact(2).flatten().copied().collect();
        let w = [w0, w1];
        let batch = score_logistic(&flat, 2, &w, bias).expect("valid shape");
        for (i, chunk) in flat.chunks_exact(2).enumerate() {
            let one = score_logistic(chunk, 2, &w, bias).expect("valid shape");
            prop_assert_eq!(batch[i].to_bits(), one[0].to_bits());
        }
    }

    /// The sigmoid is monotone non-decreasing over finite ordered logits.
    #[test]
    fn sigmoid_is_monotone(a in -700.0f64..700.0, b in -700.0f64..700.0) {
        let (lo, hi) = if a <= b { (a, b) } else { (b, a) };
        prop_assert!(sigmoid(lo) <= sigmoid(hi));
    }

    /// The abstain kernel is total + deterministic over hostile inputs, its
    /// output length matches, and only the five defined bits ever appear.
    #[test]
    fn abstain_masks_use_only_defined_bits(
        regimes in prop::collection::vec(-3i64..8, 0..48),
        allowed in any::<u64>(),
        vix_seed in prop::collection::vec(any_f64(), 0..48),
        lo in any_f64(),
        hi in any_f64(),
        flags in prop::collection::vec(0u8..4, 0..48),
        block in any::<bool>(),
        conf_seed in prop::collection::vec(any_f64(), 0..48),
        tau in any_f64(),
    ) {
        let n = regimes.len();
        let mut vix = vix_seed;
        vix.resize(n, f64::NAN);
        let mut flags = flags;
        flags.resize(n, 0);
        let mut conf = conf_seed;
        conf.resize(n, f64::NAN);
        let a = abstain_masks(&regimes, allowed, &vix, lo, hi, &flags, block, &conf, tau)
            .expect("aligned lengths");
        let b = abstain_masks(&regimes, allowed, &vix, lo, hi, &flags, block, &conf, tau)
            .expect("aligned lengths");
        prop_assert_eq!(&a, &b);
        prop_assert_eq!(a.len(), n);
        let defined: u8 = BIT_REGIME_MISMATCH
            | BIT_VIX_OUT_OF_BAND
            | BIT_VIX_MISSING
            | BIT_EVENT_DAY
            | BIT_LOW_CONFIDENCE;
        for mask in &a {
            prop_assert_eq!(mask & !defined, 0);
        }
    }

    /// vix_missing and vix_out_of_band are mutually exclusive, and neither
    /// fires when no band side is configured.
    #[test]
    fn vix_doors_are_exclusive(
        vix in any_f64(),
        lo in any_f64(),
        hi in any_f64(),
    ) {
        let mask = abstain_masks(&[0], 1, &[vix], lo, hi, &[0], false, &[f64::NAN], f64::NAN)
            .expect("aligned")[0];
        let both = BIT_VIX_MISSING | BIT_VIX_OUT_OF_BAND;
        prop_assert_ne!(mask & both, both);
        if lo.is_nan() && hi.is_nan() {
            prop_assert_eq!(mask & both, 0);
        }
    }
}
