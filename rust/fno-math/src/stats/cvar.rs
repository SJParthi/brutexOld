//! CVaR (expected-shortfall) tail fold — the worst-k integer selection kernel.
//!
//! DEC-RANK-CVAR-TAIL-GATE-001. The Rust twin of the pure-Python tail fold in
//! `brutex/ranking/cvar_gate.py::_cvar_tail_fold_python`: given one
//! candidate's per-trade P&L list in **int paisa** and a tail fraction
//! `alpha_bps` (basis points of the distribution, e.g. 500 = the worst 5%),
//! return the INTEGER-EXACT triple the rejection gate decides on:
//!
//! * `k = max(1, ceil(n * alpha_bps / 10_000))` — integer ceiling division
//!   (`(n * alpha_bps + 9_999) / 10_000` in u128, overflow-free by
//!   construction);
//! * `var_paisa` — the k-th smallest P&L (the α-quantile / Value-at-Risk
//!   bound: the LARGEST member of the worst-k tail);
//! * `tail_sum_paisa` — the exact i128 sum of the worst k P&Ls (CVaR ×​ k).
//!
//! The REJECT decision itself (`tail_sum < -threshold * k`) lives in Python
//! on exact unbounded ints — this kernel only performs the O(n) selection +
//! sum, so Rust↔Python parity is trivially BIT-IDENTICAL: every quantity is
//! an integer, no float ever participates (claim class STRUCTURAL — there is
//! no rounding to drift).
//!
//! ## Tie law (permutation invariance)
//!
//! The worst-k tail is the multiset of the k smallest VALUES. With duplicate
//! values, WHICH physical element index is selected is irrelevant — equal
//! values contribute equal summands — so `var_paisa` and `tail_sum_paisa`
//! are invariant under any permutation of `pnls`, exactly like the Python
//! oracle's `sorted(pnls)[:k]` (property-pinned on both sides).
//!
//! ## Overflow (provably absent)
//!
//! `tail_sum` accumulates at most `n` values of magnitude `< 2^63` into an
//! i128: `|sum| < n · 2^63 < 2^127` for every `n < 2^64` — unreachable
//! (a `Vec<i64>` of 2^64 elements cannot exist in memory). `k`'s ceiling
//! division runs in u128 (`n ≤ usize::MAX < 2^64`, `alpha_bps ≤ 10_000 <
//! 2^14`, so the product is `< 2^78`). No `checked_add` fallback is needed
//! because no reachable input can overflow — stated, not assumed silently.
//!
//! ## Complexity (honest envelope — NOT O(1))
//!
//! `O(n_trades)` average per call (`select_nth_unstable` quickselect + one
//! O(k) partition sum) and O(1) in the CANDIDATE count — the gate cost per
//! candidate is flat, the total gate cost across a sweep is
//! O(Σ n_trades). One O(n) `Vec` copy per call (the FFI already copied the
//! list; the in-kernel copy keeps the input immutable for the caller's
//! burn-in oracle replay). The Python oracle is `O(n log n)` (a full sort);
//! the kernel is a constant-factor + asymptotic win, never O(1)-total.
//!
//! ## Failure mode — `Result`, NEVER a panic
//!
//! Empty input and an out-of-range `alpha_bps` return `Err(String)`; the
//! `#[pyfunction]` maps it to `ValueError`, which the
//! `brutex.ranking.cvar_gate` seam routes to the Python oracle (warn-once)
//! — the seam additionally guards BOTH conditions Python-side before the
//! FFI, so the arms are defense-in-depth, structurally unreachable from the
//! production seam.
//!
//! ## Strict-mode error taxonomy (DEC-RUST-STRICT-NO-FALLBACK-001)
//!
//! Every way this call can fail is classified for the facade's strict gate,
//! and the classification is carried IN THE MESSAGE — never guessed
//! Python-side from an exception class the two populations share:
//!
//! * **CLASS C — the input is outside this crate's envelope** (the Python
//!   oracle was always the intended handler, so strict mode must keep
//!   degrading): the FFI extractions. `pnls` is not a sequence of ints, an
//!   element is a non-int (`TypeError`), or an element is an int past i64
//!   range (`OverflowError` — the Python oracle's ints are unbounded);
//!   `alpha_bps` is a non-int the Python `1 <= alpha_bps <= 10_000` gate
//!   nonetheless admits (e.g. the float `500.0`). Both extractions are
//!   performed EXPLICITLY (rather than via the argument signature) so their
//!   failures can be re-stamped with
//!   [`crate::input_refused::INPUT_REFUSED_MARKER`]; the extracted values
//!   are otherwise byte-identical to the previous signature-level
//!   extraction.
//! * **CLASS A/B — a crate FAULT** (strict mode REFUSES): anything
//!   UNMARKED — a stale wheel whose call shape drifted (a PyO3 arity /
//!   keyword `TypeError` raised before this body runs), a foreign or
//!   byzantine module bound to the symbol, broken internals.
//!
//! The two structural `Err(String)` arms below are deliberately left
//! UNMARKED (⇒ CLASS A/B ⇒ refused). They are NOT the "Python was the
//! intended handler" shape: the seam raises `ValueError` on the identical
//! two conditions BEFORE the FFI, so the Python oracle refuses them too and
//! no legitimate, pinned call can reach them. Refusing a Rust-side "I ran
//! and said no" is therefore the conservative posture that crashes nothing.

use crate::input_refused::mark_input_refusal;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// The tail-fraction domain: basis points of the trade distribution.
/// `1` = the worst 0.01% (still ≥ 1 trade via the `max(1, …)` floor);
/// `10_000` = the whole distribution.
const ALPHA_BPS_MAX: u32 = 10_000;

/// Pure fold core — `(k, var_paisa, tail_sum_paisa)` or a structural error.
///
/// Kept separate from the `#[pyfunction]` wrapper so `cargo test` links it
/// without a Python runtime (the crate convention).
pub fn cvar_tail_fold_checked(pnls: &[i64], alpha_bps: u32) -> Result<(usize, i64, i128), String> {
    if pnls.is_empty() {
        return Err(
            "stats_cvar_tail_fold: pnls must be non-empty (the Python seam abstains on an \
             empty trade list before the FFI)"
                .to_string(),
        );
    }
    if alpha_bps < 1 || alpha_bps > ALPHA_BPS_MAX {
        return Err(format!(
            "stats_cvar_tail_fold: alpha_bps must be in [1, {ALPHA_BPS_MAX}], got {alpha_bps}"
        ));
    }
    let n = pnls.len();
    // k = max(1, ceil(n * alpha_bps / 10_000)) — u128 arithmetic so the
    // product can never overflow (n < 2^64, alpha_bps < 2^14 ⇒ < 2^78).
    // `div_ceil` == the Python oracle's `(n * alpha_bps + 9_999) // 10_000`
    // for every non-negative operand pair (clippy's canonical spelling).
    // With alpha_bps ≥ 1 and n ≥ 1 the ceiling is already ≥ 1; the
    // `.max(1)` floor mirrors the Python law token-for-token anyway.
    let k_wide = ((n as u128) * (alpha_bps as u128)).div_ceil(10_000);
    let k = (k_wide as usize).max(1);
    // Worst-k selection: quickselect the (k-1)-th order statistic, so
    // buf[..k] holds the k smallest values (in arbitrary internal order)
    // and buf[k-1] is the k-th smallest — the α-quantile VaR bound.
    let mut buf = pnls.to_vec();
    buf.select_nth_unstable(k - 1);
    let var_paisa = buf[k - 1];
    let tail_sum_paisa: i128 = buf[..k].iter().map(|&v| v as i128).sum();
    Ok((k, var_paisa, tail_sum_paisa))
}

/// CVaR tail fold over one candidate's per-trade P&L list (int paisa).
///
/// Returns `(k, var_paisa, tail_sum_paisa)` — the worst-k tail size, the
/// k-th smallest P&L (VaR bound) and the exact sum of the worst k P&Ls
/// (`tail_sum` crosses as a Python int via i128; exact, never rounded).
/// Raises `ValueError` on an empty list or `alpha_bps` outside
/// `[1, 10_000]`; the `brutex.ranking.cvar_gate` seam routes any error to
/// the Python oracle instead of surfacing it.
///
/// The `Vec<i64>` / `u32` extractions are performed EXPLICITLY (rather than
/// via the argument signature) so their failures — a non-int element, an
/// int past i64 range, a non-sequence `pnls`, a non-int `alpha_bps` — are
/// re-stamped with [`crate::input_refused::INPUT_REFUSED_MARKER`]: they are
/// inputs outside this crate's envelope, not crate faults, so the facade
/// must keep degrading to the Python oracle even under strict mode
/// (DEC-RUST-STRICT-NO-FALLBACK-001; module header's taxonomy). The
/// extracted values are byte-identical to the previous signature-level
/// extraction, and the argument NAMES (`pnls`, `alpha_bps`) are unchanged.
///
/// FFI cost (disclosed): one `O(n)` list→Vec copy in plus one in-kernel
/// `O(n)` working copy; the return is three scalars.
#[pyfunction]
pub fn stats_cvar_tail_fold(
    py: Python<'_>,
    pnls: &Bound<'_, PyAny>,
    alpha_bps: &Bound<'_, PyAny>,
) -> PyResult<(u64, i64, i128)> {
    let owned: Vec<i64> = pnls
        .extract()
        .map_err(|err| mark_input_refusal(py, "stats_cvar_tail_fold: pnls", &err))?;
    let alpha: u32 = alpha_bps
        .extract()
        .map_err(|err| mark_input_refusal(py, "stats_cvar_tail_fold: alpha_bps", &err))?;
    cvar_tail_fold_checked(&owned, alpha)
        .map(|(k, var_paisa, tail_sum)| (k as u64, var_paisa, tail_sum))
        .map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    /// The independent naive reference: full sort, take k, sum — the exact
    /// Python-oracle law transcribed, used ONLY as the parity oracle here.
    fn naive(pnls: &[i64], alpha_bps: u32) -> (usize, i64, i128) {
        let n = pnls.len();
        let k = (((n as u128) * (alpha_bps as u128)).div_ceil(10_000) as usize).max(1);
        let mut sorted = pnls.to_vec();
        sorted.sort_unstable();
        let tail = &sorted[..k];
        (k, tail[k - 1], tail.iter().map(|&v| v as i128).sum())
    }

    // ── exact-value pins (hand-derived; each arithmetic step audited) ──

    #[test]
    fn hand_derived_worst_five_percent_of_100() {
        // n=100, alpha=500 ⇒ k = ceil(100·500/10000) = ceil(5.0) = 5.
        // pnls = [-1000, -900, ..., +8900] (step +100): worst 5 =
        // [-1000, -900, -800, -700, -600]; var = -600 (5th smallest);
        // tail_sum = -(1000+900+800+700+600) = -4000.
        let pnls: Vec<i64> = (0..100).map(|i| -1000 + 100 * i).collect();
        assert_eq!(
            cvar_tail_fold_checked(&pnls, 500).unwrap(),
            (5, -600, -4000)
        );
    }

    #[test]
    fn hand_derived_alpha_rounding_boundary() {
        // n=41, alpha=500 ⇒ 41·500 = 20_500 ⇒ ceil(2.05) = 3 (the +9_999
        // trick: (20_500 + 9_999) / 10_000 = 30_499 / 10_000 = 3).
        // n=40 ⇒ 20_000 ⇒ exactly 2 (no round-up on the exact boundary).
        let pnls41: Vec<i64> = (0..41).map(|i| i * 10).collect();
        let (k41, var41, sum41) = cvar_tail_fold_checked(&pnls41, 500).unwrap();
        assert_eq!((k41, var41, sum41), (3, 20, 30)); // worst 3 = [0,10,20]
        let pnls40: Vec<i64> = (0..40).map(|i| i * 10).collect();
        let (k40, var40, sum40) = cvar_tail_fold_checked(&pnls40, 500).unwrap();
        assert_eq!((k40, var40, sum40), (2, 10, 10)); // worst 2 = [0,10]
    }

    #[test]
    fn single_trade_k_floors_at_one() {
        // n=1, alpha=1 ⇒ ceil(1/10000) = 1 (the max(1, …) floor holds).
        assert_eq!(cvar_tail_fold_checked(&[-77], 1).unwrap(), (1, -77, -77));
    }

    #[test]
    fn whole_distribution_alpha_10000() {
        // alpha=10000 ⇒ k=n: var = max element, tail_sum = total sum.
        let pnls = [5, -3, 9, -8];
        assert_eq!(cvar_tail_fold_checked(&pnls, 10_000).unwrap(), (4, 9, 3));
    }

    #[test]
    fn all_equal_values_tie_law() {
        let pnls = [-50; 10];
        // k = ceil(10·5000/10000) = 5; every member is -50.
        assert_eq!(
            cvar_tail_fold_checked(&pnls, 5_000).unwrap(),
            (5, -50, -250)
        );
    }

    #[test]
    fn all_positive_pnls_tail_is_positive() {
        let pnls = [10, 20, 30, 40];
        // k = ceil(4·2500/10000) = 1: worst trade is +10 — a positive tail.
        assert_eq!(cvar_tail_fold_checked(&pnls, 2_500).unwrap(), (1, 10, 10));
    }

    #[test]
    fn extreme_magnitudes_sum_exactly_in_i128() {
        let pnls = [i64::MIN, i64::MIN, i64::MAX];
        let (k, var, sum) = cvar_tail_fold_checked(&pnls, 6_667).unwrap();
        // k = ceil(3·6667/10000) = ceil(2.0001) = 3 ⇒ whole distribution.
        assert_eq!(k, 3);
        assert_eq!(var, i64::MAX);
        assert_eq!(sum, 2 * (i64::MIN as i128) + (i64::MAX as i128));
    }

    // ── error arms ──

    #[test]
    fn empty_input_is_err() {
        let err = cvar_tail_fold_checked(&[], 500).unwrap_err();
        assert!(err.contains("non-empty"), "unexpected message: {err}");
    }

    #[test]
    fn alpha_zero_is_err() {
        let err = cvar_tail_fold_checked(&[1], 0).unwrap_err();
        assert!(err.contains("alpha_bps"), "unexpected message: {err}");
    }

    #[test]
    fn alpha_above_10000_is_err() {
        let err = cvar_tail_fold_checked(&[1], 10_001).unwrap_err();
        assert!(err.contains("alpha_bps"), "unexpected message: {err}");
    }

    /// DEC-RUST-STRICT-NO-FALLBACK-001: the two structural arms are the
    /// CLASS-A/B remainder — deliberately UNMARKED, so the facade's
    /// `kernel_error` reason keeps them and strict mode REFUSES. (The
    /// CLASS-C population is the FFI extraction, stamped in the
    /// `#[pyfunction]` wrapper; its marker text is pinned in
    /// `crate::input_refused`.) A future edit that stamped these arms would
    /// silently move them into the strict-exempt half — this pin fails first.
    #[test]
    fn structural_arms_are_unmarked_class_a_b() {
        for err in [
            cvar_tail_fold_checked(&[], 500).unwrap_err(),
            cvar_tail_fold_checked(&[1], 0).unwrap_err(),
            cvar_tail_fold_checked(&[1], 10_001).unwrap_err(),
        ] {
            assert!(
                !err.contains(crate::input_refused::INPUT_REFUSED_MARKER),
                "structural arm must stay UNMARKED (CLASS A/B): {err}"
            );
        }
    }

    // ── properties ──

    proptest! {
        /// Parity vs the naive sort-based reference over hostile int inputs.
        #[test]
        fn parity_with_naive_reference(
            pnls in proptest::collection::vec(any::<i64>(), 1..200),
            alpha_bps in 1u32..=10_000,
        ) {
            prop_assert_eq!(
                cvar_tail_fold_checked(&pnls, alpha_bps).unwrap(),
                naive(&pnls, alpha_bps)
            );
        }

        /// CVaR ≤ VaR: the mean of the worst-k tail can never exceed its
        /// largest member — `tail_sum ≤ var · k` exactly.
        #[test]
        fn tail_mean_at_most_var(
            pnls in proptest::collection::vec(-1_000_000i64..1_000_000, 1..200),
            alpha_bps in 1u32..=10_000,
        ) {
            let (k, var, sum) = cvar_tail_fold_checked(&pnls, alpha_bps).unwrap();
            prop_assert!(sum <= (var as i128) * (k as i128));
        }

        /// Permutation invariance: the fold reads the multiset, never the order.
        #[test]
        fn permutation_invariant(
            pnls in proptest::collection::vec(any::<i64>(), 1..100),
            alpha_bps in 1u32..=10_000,
        ) {
            let mut reversed = pnls.clone();
            reversed.reverse();
            prop_assert_eq!(
                cvar_tail_fold_checked(&pnls, alpha_bps).unwrap(),
                cvar_tail_fold_checked(&reversed, alpha_bps).unwrap()
            );
        }

        /// Translation equivariance: shifting every P&L by `c` shifts the
        /// VaR bound by `c` and the tail sum by `c · k` exactly.
        #[test]
        fn translation_equivariance(
            pnls in proptest::collection::vec(-1_000_000i64..1_000_000, 1..100),
            alpha_bps in 1u32..=10_000,
            c in -1_000_000i64..1_000_000,
        ) {
            let shifted: Vec<i64> = pnls.iter().map(|&v| v + c).collect();
            let (k0, var0, sum0) = cvar_tail_fold_checked(&pnls, alpha_bps).unwrap();
            let (k1, var1, sum1) = cvar_tail_fold_checked(&shifted, alpha_bps).unwrap();
            prop_assert_eq!(k0, k1);
            prop_assert_eq!(var1, var0 + c);
            prop_assert_eq!(sum1, sum0 + (c as i128) * (k0 as i128));
        }

        /// Monotone tail size in alpha: a larger alpha never shrinks k, and
        /// the per-trade tail mean is non-decreasing in alpha (widening the
        /// tail only ever ADMITS less-extreme trades): compare the exact
        /// integer cross-products `sum_a · k_b ≤ sum_b · k_a` for
        /// `alpha_a ≤ alpha_b` (mean_a ≤ mean_b without any division).
        #[test]
        fn monotone_in_alpha(
            pnls in proptest::collection::vec(-1_000_000i64..1_000_000, 1..100),
            alpha_a in 1u32..=10_000,
            alpha_b in 1u32..=10_000,
        ) {
            let (lo, hi) = if alpha_a <= alpha_b { (alpha_a, alpha_b) } else { (alpha_b, alpha_a) };
            let (k_lo, _, sum_lo) = cvar_tail_fold_checked(&pnls, lo).unwrap();
            let (k_hi, _, sum_hi) = cvar_tail_fold_checked(&pnls, hi).unwrap();
            prop_assert!(k_lo <= k_hi);
            prop_assert!(sum_lo * (k_hi as i128) <= sum_hi * (k_lo as i128));
        }
    }
}
