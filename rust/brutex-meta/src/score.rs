//! The meta-label P(win) batch scorer (DEC-META-LABEL-ANNOTATE-001).
//!
//! One pure function, [`score_logistic`]: for each row of a flattened
//! row-major feature matrix, compute the logistic probability
//! `sigmoid(bias + Σ_j weights[j] · features[row·n_features + j])` with a
//! numerically-stable sigmoid. The accumulation order is PINNED (ascending
//! feature index, one running `f64` accumulator) and mirrored
//! token-for-token by the retained Python oracle
//! (`brutex/meta/logistic.py::score_logistic`) so the two arms are
//! bit-comparable: `+`/`*` are IEEE-754-defined and deterministic; the ONE
//! platform-dependent primitive is `exp` (Rust `f64::exp` vs CPython
//! `math.exp`, both delegating to the platform libm) — the facade's 16-call
//! burn-in bit-compare + the hypothesis parity property are the runtime
//! teeth for that seam (`brutex/meta/meta_rust.py`).
//!
//! ## Complexity (Golden Rule #5 — honest envelope)
//!
//! O(n_features) per trade = **O(1)/trade** for the FIXED documented feature
//! vector (`brutex/meta/features.py::FEATURE_NAMES` — a small constant
//! independent of trade count, bar count and history depth). The batch call
//! is O(n_rows · n_features) total, by construction — never claimed O(1)
//! total. Training (IRLS) stays offline Python and never enters this crate.
//!
//! ## Non-finite inputs
//!
//! NaN/±inf features or weights propagate IEEE-style through the dot product
//! and the sigmoid (a NaN logit yields a NaN probability; a +inf logit
//! yields exactly 1.0, a −inf logit exactly 0.0). The Python facade's
//! refuse-poison gate treats a non-finite probability from all-finite
//! inputs as a poisoned wheel and degrades to the oracle.

/// Numerically-stable logistic sigmoid — the ONE shared expression.
///
/// The branch avoids `exp` overflow for large `|z|`:
/// `z >= 0` → `1 / (1 + exp(-z))`; `z < 0` → `exp(z) / (1 + exp(z))`.
/// Mirrored token-for-token by `brutex/meta/logistic.py::_sigmoid` (the
/// parity oracle). A NaN `z` takes the negative branch (`NaN >= 0.0` is
/// false) and returns NaN — identical in CPython.
#[inline]
#[must_use]
pub fn sigmoid(z: f64) -> f64 {
    if z >= 0.0 {
        1.0 / (1.0 + (-z).exp())
    } else {
        let ez = z.exp();
        ez / (1.0 + ez)
    }
}

/// Score a flattened row-major feature batch against one logistic model.
///
/// # Arguments
///
/// * `features` — row-major `n_rows × n_features` matrix, flattened.
/// * `n_features` — the fixed feature-vector width (must be ≥ 1).
/// * `weights` — model weights, length exactly `n_features`.
/// * `bias` — additive bias term (the Python caller passes `0.0` and carries
///   the intercept as feature 0 = `1.0`; the parameter exists so the kernel
///   is a complete logistic scorer, not a convention hostage).
///
/// # Returns
///
/// One probability per row, in row order.
///
/// # Errors
///
/// A `String` describing the structural violation when `n_features == 0`,
/// `weights.len() != n_features`, or `features.len()` is not a multiple of
/// `n_features` — the caller (the PyO3 edge → the Python facade) degrades
/// to the oracle on the mapped `ValueError`.
pub fn score_logistic(
    features: &[f64],
    n_features: usize,
    weights: &[f64],
    bias: f64,
) -> Result<Vec<f64>, String> {
    if n_features == 0 {
        return Err("score_logistic: n_features must be >= 1".to_owned());
    }
    if weights.len() != n_features {
        return Err(format!(
            "score_logistic: weights length {} != n_features {}",
            weights.len(),
            n_features
        ));
    }
    if !features.len().is_multiple_of(n_features) {
        return Err(format!(
            "score_logistic: features length {} is not a multiple of n_features {}",
            features.len(),
            n_features
        ));
    }
    let n_rows = features.len() / n_features;
    let mut out = Vec::with_capacity(n_rows);
    for row in 0..n_rows {
        let base = row * n_features;
        // PINNED accumulation order: ascending feature index over ONE
        // running f64 — the Python oracle's exact loop shape.
        let mut z = bias;
        for j in 0..n_features {
            z += weights[j] * features[base + j];
        }
        out.push(sigmoid(z));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_batch_scores_empty() {
        let out = score_logistic(&[], 3, &[0.1, 0.2, 0.3], 0.0).expect("valid");
        assert!(out.is_empty());
    }

    #[test]
    fn zero_weights_zero_bias_is_half() {
        let out = score_logistic(&[1.0, 2.0, 3.0], 3, &[0.0, 0.0, 0.0], 0.0).expect("valid");
        assert_eq!(out, vec![0.5]);
    }

    #[test]
    fn positive_and_negative_logits_bracket_half() {
        let out = score_logistic(&[1.0, 1.0], 1, &[2.0], 0.0).expect("valid");
        assert!(out[0] > 0.5 && out[1] > 0.5);
        let out = score_logistic(&[-1.0], 1, &[2.0], 0.0).expect("valid");
        assert!(out[0] < 0.5);
    }

    #[test]
    fn bias_shifts_the_logit() {
        // z = bias exactly when the single feature is 0.
        let out = score_logistic(&[0.0], 1, &[1.0], 3.0).expect("valid");
        assert_eq!(out[0], sigmoid(3.0));
    }

    #[test]
    fn large_logits_saturate_without_overflow() {
        let out = score_logistic(&[1.0, -1.0], 1, &[1000.0], 0.0).expect("valid");
        assert_eq!(out[0], 1.0);
        assert_eq!(out[1], 0.0);
        // Extreme but finite weights never produce NaN from overflow.
        let out = score_logistic(&[1.0], 1, &[f64::MAX], 0.0).expect("valid");
        assert_eq!(out[0], 1.0);
    }

    #[test]
    fn infinite_logit_is_exact_zero_or_one() {
        let out = score_logistic(&[1.0, 1.0], 1, &[f64::INFINITY], 0.0).expect("valid");
        assert_eq!(out[0], 1.0);
        let out = score_logistic(&[-1.0], 1, &[f64::INFINITY], 0.0).expect("valid");
        assert_eq!(out[0], 0.0);
    }

    #[test]
    fn nan_feature_propagates_nan_probability() {
        let out = score_logistic(&[f64::NAN], 1, &[1.0], 0.0).expect("valid");
        assert!(out[0].is_nan());
    }

    #[test]
    fn signed_zero_feature_scores_exact_half() {
        // -0.0 * w == -0.0; bias 0.0 + -0.0 == 0.0 → sigmoid(0.0) == 0.5,
        // taking the non-negative branch exactly like CPython.
        let out = score_logistic(&[-0.0], 1, &[5.0], 0.0).expect("valid");
        assert_eq!(out[0], 0.5);
    }

    #[test]
    fn subnormal_inputs_stay_finite() {
        let sub = f64::MIN_POSITIVE / 2.0;
        let out = score_logistic(&[sub], 1, &[sub], 0.0).expect("valid");
        assert_eq!(out[0], 0.5); // sub*sub underflows to 0 → sigmoid(0) = 0.5
    }

    #[test]
    fn zero_n_features_is_err() {
        assert!(score_logistic(&[], 0, &[], 0.0).is_err());
    }

    #[test]
    fn weight_length_mismatch_is_err() {
        assert!(score_logistic(&[1.0, 2.0], 2, &[1.0], 0.0).is_err());
    }

    #[test]
    fn ragged_features_is_err() {
        assert!(score_logistic(&[1.0, 2.0, 3.0], 2, &[1.0, 1.0], 0.0).is_err());
    }

    #[test]
    fn accumulation_order_is_ascending_index() {
        // Catastrophic-cancellation probe: (1e16 + 1) - 1e16 orders differ.
        // Ascending-index accumulation: ((0 + 1e16) + 1.0) + -1e16 = 0.0 →
        // sigmoid(0.0) = 0.5 exactly. A reversed order would give 1.0 first.
        let feats = [1.0, 1.0, 1.0];
        let w = [1e16, 1.0, -1e16];
        let out = score_logistic(&feats, 3, &w, 0.0).expect("valid");
        assert_eq!(out[0], 0.5);
    }

    #[test]
    fn multi_row_batch_matches_per_row_calls() {
        let w = [0.3, -0.7];
        let rows = [0.5, 1.5, -2.0, 0.25];
        let batch = score_logistic(&rows, 2, &w, 0.1).expect("valid");
        for (i, chunk) in rows.chunks(2).enumerate() {
            let one = score_logistic(chunk, 2, &w, 0.1).expect("valid");
            assert_eq!(batch[i].to_bits(), one[0].to_bits());
        }
    }
}
