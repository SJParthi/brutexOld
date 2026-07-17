//! CPCV fold pass-count kernel — the C(S, k) Chan/Pébay moment-merge loop.
//!
//! DEC-METRICS-RUST-STATS-001 (slice 6, PR-1). The Rust port of the ONE
//! measured-hot region of `brutex.metrics.cpcv.cpcv_pass_rate`: the
//! `C(n_groups, n_test_groups)`-fold loop (12,870 folds at the production
//! S=16/k=8) over PRECOMPUTED per-group `(count, mean, M2)` triples —
//! measured ~55-59 ms of pure-Python merge work per candidate, flat in the
//! series length. Everything else deliberately STAYS in Python:
//! `_validate_returns` / `_validate_params` (every ValueError message is
//! byte-pinned and lands verbatim in `cascade-report.json` detail strings),
//! `_group_bounds`, `_group_moments` (numpy PAIRWISE 1-D reductions — a
//! sequential Rust sum would NOT be bit-identical at realistic n), and the
//! final exact `n_pass / n_total` division.
//!
//! ## Bit-identity claim: STRUCTURAL (Golden Rule #4)
//!
//! The fold transcribes `brutex/metrics/cpcv.py::_combine_moments`
//! operation-for-operation; the op order is fixed by the PYTHON SOURCE (a
//! sequential scalar loop), not by numpy internals, so the claim is stable
//! under numpy upgrades:
//!
//! * per group, in ascending combination order:
//!   `combined = n_total + n_b`; `delta = mean_b - mean_total`;
//!   `mean_total += delta * n_b / combined`;
//!   `m2_total += m2_b + delta * delta * n_total * n_b / combined`;
//!   `n_total = combined` — left-to-right multiply chain, division LAST,
//!   exactly as cpcv.py evaluates it. NO `mul_add`/FMA (an explicit FMA
//!   would fuse where CPython rounds per operation), NO Kahan, NO rayon.
//! * int→f64 conversions: CPython's `float(int)` and Rust's `as f64` are
//!   BOTH correctly rounded (round-to-nearest, ties-to-even) — identical
//!   for every i64, exact for |n| ≤ 2⁵³.
//! * `sqrt` is IEEE-754 correctly rounded on both sides; the division and
//!   comparison are single IEEE ops on identical operands.
//! * NO finiteness requirement on `means`/`m2s`: the Python oracle's
//!   overflow fail-safe (a ±inf/NaN merge term → non-finite variance → the
//!   fold is a non-pass, cpcv.py `_combine_moments` docstring) must flow
//!   through IEEE-identically, and it does — inf/NaN propagate through the
//!   same expressions to the same `!variance.is_finite()` verdict.
//!
//! ## Failure mode — `Result`, NEVER a panic
//!
//! Under the crate's `overflow-checks = true` + `panic = "abort"` release
//! profile an unchecked i64 overflow would SIGABRT the host interpreter.
//! The boundary arms below return `Result<_, String>`; post-Python-
//! validation they are structurally unreachable (the seam's Python caller
//! runs `_validate_params` first) — they exist for the fallback law: the
//! seam maps ANY kernel error to warn-once + the Python oracle, never a
//! raise the pure-Python arm would not have produced. The total-count
//! `checked_add` guard bounds every fold's `n_total` by the pre-validated
//! total, making the in-loop `n_total + n_b` provably overflow-free.
//!
//! ## Complexity (honest envelope — NOT O(1) total)
//!
//! `O(C(n_groups, n_test_groups) · n_test_groups)` fold work per call —
//! the SAME asymptotics as the Python loop (CPCV is combinatorially
//! expensive by construction); this kernel is a CONSTANT-FACTOR win only.
//! The FFI copies three `n_groups`-length Vecs per call (48 scalars at the
//! production S=16) — O(n_groups), disclosed, negligible.
//! The pyfunction holds the GIL with no signal check between folds (no
//! `allow_threads`): a pathological `(n_groups, n_test_groups)` makes the
//! call non-interruptible (Ctrl-C deferred, Python threads blocked) for its
//! duration, where the Python oracle stays interruptible between bytecodes
//! — sub-ms at the production C(16, 8), so immaterial there.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// A Sharpe needs at least this many samples (mirror of cpcv.py
/// `_MIN_SAMPLES`): a fold whose merged count is below it is undefined and
/// counts as a non-pass.
const MIN_SAMPLES: i64 = 2;

/// Sharpe of the union of the groups in `combo` via the Chan/Pébay merge,
/// or `None` when undefined — the operation-for-operation transcription of
/// `brutex.metrics.cpcv._combine_moments` (see the module docstring for the
/// STRUCTURAL bit-identity argument).
///
/// Precondition (upheld by [`cpcv_pass_count_checked`]'s validation): every
/// `combo` index is in range, every count ≥ 1, and the TOTAL count fits in
/// i64 — so `n_total + n_b` cannot overflow.
fn fold_sharpe(counts: &[i64], means: &[f64], m2s: &[f64], combo: &[usize]) -> Option<f64> {
    let mut n_total: i64 = 0;
    let mut mean_total: f64 = 0.0;
    let mut m2_total: f64 = 0.0;
    for &gid in combo {
        let n_b = counts[gid];
        let mean_b = means[gid];
        let m2_b = m2s[gid];
        let combined = n_total + n_b;
        let delta = mean_b - mean_total;
        // Left-to-right multiply chain, division LAST — cpcv.py's own order.
        mean_total += delta * (n_b as f64) / (combined as f64);
        m2_total += m2_b + delta * delta * (n_total as f64) * (n_b as f64) / (combined as f64);
        n_total = combined;
    }
    if n_total < MIN_SAMPLES {
        return None;
    }
    let variance = m2_total / (n_total as f64);
    if !variance.is_finite() || variance <= 0.0 {
        return None;
    }
    Some(mean_total / variance.sqrt())
}

/// Pure core of [`stats_cpcv_pass_count`] — `Result<_, String>` (NO
/// `PyErr`) so the no-libpython `cargo test` link exercises every branch
/// (the `metrics::drawdown_stats_checked` split).
///
/// Enumerates every `C(n_groups, n_test_groups)` combination of group ids in
/// lexicographic (`itertools.combinations`) order via a hand-rolled
/// index-increment enumerator, folds each combination's per-group
/// `(count, mean, M2)` triples with the Chan/Pébay merge, and counts the
/// folds whose Sharpe is defined AND STRICTLY greater than `min_sharpe`
/// (the cpcv.py pass predicate — an undefined fold is a non-pass).
///
/// The count is order-invariant over the enumeration, but the enumerator is
/// still pinned complete + duplicate-free by the in-file tests (a broken
/// enumerator changes the count).
pub fn cpcv_pass_count_checked(
    counts: &[i64],
    means: &[f64],
    m2s: &[f64],
    n_test_groups: usize,
    min_sharpe: f64,
) -> Result<u64, String> {
    let n_groups = counts.len();
    if means.len() != n_groups || m2s.len() != n_groups {
        return Err(format!(
            "stats_cpcv_pass_count: counts/means/m2s length mismatch ({n_groups} / {} / {})",
            means.len(),
            m2s.len()
        ));
    }
    if n_groups < 2 {
        return Err(format!(
            "stats_cpcv_pass_count: n_groups must be >= 2, got {n_groups}"
        ));
    }
    if n_test_groups < 1 || n_test_groups >= n_groups {
        return Err(format!(
            "stats_cpcv_pass_count: n_test_groups must be in [1, {}], got {n_test_groups}",
            n_groups - 1
        ));
    }
    // Every group must be non-empty (the Python `_validate_params`
    // guarantee), and the TOTAL count must fit in i64 so the in-fold
    // `n_total + n_b` (a subset sum of positive counts) is provably
    // overflow-free under the crate's overflow-checks profile.
    let mut total: i64 = 0;
    for &c in counts {
        if c < 1 {
            return Err(format!(
                "stats_cpcv_pass_count: every group count must be >= 1, got {c}"
            ));
        }
        total = total.checked_add(c).ok_or_else(|| {
            "stats_cpcv_pass_count: total observation count overflows i64".to_string()
        })?;
    }

    let k = n_test_groups;
    let mut combo: Vec<usize> = (0..k).collect();
    let mut n_pass: u64 = 0;
    loop {
        if let Some(sharpe) = fold_sharpe(counts, means, m2s, &combo) {
            // STRICT `>` — the cpcv.py pass predicate (a fold Sharpe exactly
            // equal to min_sharpe is a NON-pass).
            if sharpe > min_sharpe {
                n_pass += 1;
            }
        }
        // Advance to the next lexicographic combination — the
        // itertools.combinations index-increment algorithm.
        let mut i = k;
        let advanced = loop {
            if i == 0 {
                break false;
            }
            i -= 1;
            if combo[i] != i + n_groups - k {
                break true;
            }
        };
        if !advanced {
            return Ok(n_pass);
        }
        combo[i] += 1;
        for j in (i + 1)..k {
            combo[j] = combo[j - 1] + 1;
        }
    }
}

/// CPCV fold pass-count over precomputed per-group moment triples — the
/// slice-6 PR-1 kernel (see the module docstring).
///
/// `counts`/`means`/`m2s` are the per-group `(n, mean, M2)` triples that
/// `brutex.metrics.cpcv._group_moments` computed (numpy stays the moments
/// producer); returns how many of the `C(len(counts), n_test_groups)` test
/// folds have a defined merged Sharpe STRICTLY greater than `min_sharpe`.
/// Raises `ValueError` on a structurally invalid input; the Python seam
/// routes any error to the pure-Python oracle instead of surfacing it.
///
/// FFI cost (disclosed): three `O(n_groups)` list→Vec copies per call —
/// 48 scalars at the production S=16.
#[pyfunction]
pub fn stats_cpcv_pass_count(
    counts: Vec<i64>,
    means: Vec<f64>,
    m2s: Vec<f64>,
    n_test_groups: usize,
    min_sharpe: f64,
) -> PyResult<u64> {
    cpcv_pass_count_checked(&counts, &means, &m2s, n_test_groups, min_sharpe)
        .map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn bits(x: f64) -> u64 {
        x.to_bits()
    }

    /// Independent recursive combination enumerator + an independently
    /// written Chan re-fold — the in-crate oracle. Written as a SEPARATE
    /// recursion (not the index-increment loop) so an enumerator mutant and
    /// a fold mutant both diverge from it.
    fn naive_pass_count(
        counts: &[i64],
        means: &[f64],
        m2s: &[f64],
        k: usize,
        min_sharpe: f64,
    ) -> u64 {
        fn recurse(
            counts: &[i64],
            means: &[f64],
            m2s: &[f64],
            k: usize,
            start: usize,
            chosen: &mut Vec<usize>,
            min_sharpe: f64,
            n_pass: &mut u64,
        ) {
            if chosen.len() == k {
                if let Some(s) = naive_fold(counts, means, m2s, chosen) {
                    if s > min_sharpe {
                        *n_pass += 1;
                    }
                }
                return;
            }
            for gid in start..counts.len() {
                chosen.push(gid);
                recurse(counts, means, m2s, k, gid + 1, chosen, min_sharpe, n_pass);
                chosen.pop();
            }
        }
        let mut n_pass = 0u64;
        recurse(
            counts,
            means,
            m2s,
            k,
            0,
            &mut Vec::new(),
            min_sharpe,
            &mut n_pass,
        );
        n_pass
    }

    /// The same Chan/Pébay recurrence written independently (accumulators in
    /// a struct, an explicit temporaries style) — algebraically AND
    /// bit-identically the same sequential op order.
    fn naive_fold(counts: &[i64], means: &[f64], m2s: &[f64], combo: &[usize]) -> Option<f64> {
        struct Acc {
            n: i64,
            mean: f64,
            m2: f64,
        }
        let mut acc = Acc {
            n: 0,
            mean: 0.0,
            m2: 0.0,
        };
        for &gid in combo {
            let nb = counts[gid] as f64;
            let combined = acc.n + counts[gid];
            let delta = means[gid] - acc.mean;
            let mean_step = delta * nb / (combined as f64);
            let m2_step = m2s[gid] + delta * delta * (acc.n as f64) * nb / (combined as f64);
            acc.mean += mean_step;
            acc.m2 += m2_step;
            acc.n = combined;
        }
        if acc.n < 2 {
            return None;
        }
        let variance = acc.m2 / (acc.n as f64);
        if !variance.is_finite() || variance <= 0.0 {
            return None;
        }
        Some(acc.mean / variance.sqrt())
    }

    // ── known-value bit pins ──────────────────────────────────────────

    #[test]
    fn golden_fixture_moments_one_pass_of_six() {
        // The tests/golden/test_cpcv_wfo_golden.py fixture: returns
        // [1, 2, -3, -4], S=4 singleton groups, k=2 → exactly ONE of the
        // six folds (groups {0,1}: mean 1.5 > 0) passes.
        let counts = [1i64, 1, 1, 1];
        let means = [1.0f64, 2.0, -3.0, -4.0];
        let m2s = [0.0f64; 4];
        let n = cpcv_pass_count_checked(&counts, &means, &m2s, 2, 0.0)
            .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
        assert_eq!(n, 1);
    }

    #[test]
    fn fold_sharpe_known_value_bits() {
        // Singletons {1.0, 2.0}: merged n=2, mean=1.5, M2=0.5 → variance
        // 0.25, sqrt 0.5, sharpe 3.0 — every step exactly representable, so
        // the pin is a true bit compare.
        let counts = [1i64, 1];
        let means = [1.0f64, 2.0];
        let m2s = [0.0f64, 0.0];
        let s = fold_sharpe(&counts, &means, &m2s, &[0, 1]);
        assert_eq!(s.map(bits), Some(bits(3.0)));
        // And bit-identical to the independently written re-fold.
        let n = naive_fold(&counts, &means, &m2s, &[0, 1]);
        assert_eq!(s.map(bits), n.map(bits));
    }

    #[test]
    fn overflow_scale_moments_are_non_pass() {
        // 1e300-scale means: the delta² merge term overflows to inf →
        // variance non-finite → the fold is a NON-pass (the Python
        // fail-safe flows through IEEE-identically — no Err, no panic).
        let counts = [2i64, 2];
        let means = [1e300f64, -1e300];
        let m2s = [0.0f64, 0.0];
        assert_eq!(fold_sharpe(&counts, &means, &m2s, &[0, 1]), None);
        let n = cpcv_pass_count_checked(&counts, &means, &m2s, 1, f64::NEG_INFINITY)
            .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
        // k=1 folds: each single group has M2=0 → variance 0 → non-pass too.
        assert_eq!(n, 0);
    }

    #[test]
    fn nan_moments_are_non_pass_never_err() {
        let counts = [2i64, 2];
        let means = [f64::NAN, 1.0];
        let m2s = [0.0f64, 0.0];
        let n = cpcv_pass_count_checked(&counts, &means, &m2s, 1, f64::NEG_INFINITY)
            .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
        assert_eq!(n, 0);
    }

    #[test]
    fn n_total_below_two_is_undefined() {
        // k=1 over count-1 groups: every fold has n_total = 1 < 2 →
        // undefined → 0 passes even at min_sharpe = -inf.
        let counts = [1i64, 1, 1];
        let means = [5.0f64, 6.0, 7.0];
        let m2s = [1.0f64, 1.0, 1.0];
        let n = cpcv_pass_count_checked(&counts, &means, &m2s, 1, f64::NEG_INFINITY)
            .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
        assert_eq!(n, 0);
    }

    #[test]
    fn strict_gt_boundary_at_exact_min_sharpe() {
        // Singletons {0.0, 2.0} merge to sharpe EXACTLY 1.0 (n=2, mean=1.0,
        // M2=2.0 → variance 1.0). At min_sharpe=1.0 the fold is a NON-pass
        // (strict `>`); nudged one ulp below it passes. Groups (0,2) are a
        // zero-variance fold (means equal, M2 0) → always non-pass.
        let counts = [1i64, 1, 1];
        let means = [0.0f64, 2.0, 0.0];
        let m2s = [0.0f64; 3];
        let at = cpcv_pass_count_checked(&counts, &means, &m2s, 2, 1.0)
            .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
        assert_eq!(at, 0); // kills `>` → `>=`
        let below =
            cpcv_pass_count_checked(&counts, &means, &m2s, 2, f64::from_bits(bits(1.0) - 1))
                .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
        assert_eq!(below, 2); // folds (0,1) and (1,2), both sharpe 1.0
    }

    #[test]
    fn enumerator_is_complete_at_binomial_count() {
        // Distinct means over count-2 groups with positive M2 → every fold
        // defined; min_sharpe = -inf → every fold passes ⇒ the count IS the
        // number of enumerated combinations. C(6, 3) = 20.
        let counts = [2i64; 6];
        let means = [1.0f64, 2.0, 3.0, 4.0, 5.0, 6.0];
        let m2s = [1.0f64; 6];
        let n = cpcv_pass_count_checked(&counts, &means, &m2s, 3, f64::NEG_INFINITY)
            .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
        assert_eq!(n, 20);
    }

    // ── structural Err arms ───────────────────────────────────────────

    #[test]
    fn err_on_length_mismatch() {
        let e = cpcv_pass_count_checked(&[1, 1], &[0.0], &[0.0, 0.0], 1, 0.0);
        assert!(e.is_err_and(|m| m.contains("length mismatch")));
        let e = cpcv_pass_count_checked(&[1, 1], &[0.0, 0.0], &[0.0], 1, 0.0);
        assert!(e.is_err_and(|m| m.contains("length mismatch")));
    }

    #[test]
    fn err_on_too_few_groups() {
        let e = cpcv_pass_count_checked(&[5], &[0.0], &[0.0], 1, 0.0);
        assert!(e.is_err_and(|m| m.contains("n_groups must be >= 2")));
        let e = cpcv_pass_count_checked(&[], &[], &[], 1, 0.0);
        assert!(e.is_err_and(|m| m.contains("n_groups must be >= 2")));
    }

    #[test]
    fn err_on_n_test_groups_out_of_range() {
        let counts = [1i64, 1, 1];
        let means = [0.0f64; 3];
        let m2s = [0.0f64; 3];
        let e = cpcv_pass_count_checked(&counts, &means, &m2s, 0, 0.0);
        assert!(e.is_err_and(|m| m.contains("n_test_groups must be in [1, 2]")));
        let e = cpcv_pass_count_checked(&counts, &means, &m2s, 3, 0.0);
        assert!(e.is_err_and(|m| m.contains("n_test_groups must be in [1, 2]")));
    }

    #[test]
    fn err_on_non_positive_count() {
        let e = cpcv_pass_count_checked(&[1, 0], &[0.0, 0.0], &[0.0, 0.0], 1, 0.0);
        assert!(e.is_err_and(|m| m.contains("count must be >= 1, got 0")));
        let e = cpcv_pass_count_checked(&[1, -3], &[0.0, 0.0], &[0.0, 0.0], 1, 0.0);
        assert!(e.is_err_and(|m| m.contains("count must be >= 1, got -3")));
    }

    #[test]
    fn err_on_total_count_i64_overflow() {
        let e = cpcv_pass_count_checked(&[i64::MAX, i64::MAX], &[0.0, 0.0], &[0.0, 0.0], 1, 0.0);
        assert!(e.is_err_and(|m| m.contains("overflows i64")));
    }

    // ── properties ────────────────────────────────────────────────────

    fn binomial(n: u64, k: u64) -> u64 {
        // Small-domain exact binomial (n ≤ 8 in the property strategy).
        let mut acc: u128 = 1;
        for i in 0..k {
            acc = acc * u128::from(n - i) / u128::from(i + 1);
        }
        acc as u64
    }

    proptest! {
        /// The kernel's count equals the independent recursive-enumerator +
        /// independently-written-fold oracle, is deterministic, and never
        /// exceeds C(n_groups, k).
        #[test]
        fn pass_count_matches_naive_oracle(
            groups in proptest::collection::vec(
                (1i64..=50, -10.0f64..=10.0, 0.0f64..=100.0), 2..=8,
            ),
            k_seed in 0usize..7,
            min_sharpe in -3.0f64..=3.0,
        ) {
            let counts: Vec<i64> = groups.iter().map(|g| g.0).collect();
            let means: Vec<f64> = groups.iter().map(|g| g.1).collect();
            let m2s: Vec<f64> = groups.iter().map(|g| g.2).collect();
            let k = 1 + k_seed % (counts.len() - 1);
            let got = cpcv_pass_count_checked(&counts, &means, &m2s, k, min_sharpe)
                .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
            let want = naive_pass_count(&counts, &means, &m2s, k, min_sharpe);
            prop_assert_eq!(got, want);
            let again = cpcv_pass_count_checked(&counts, &means, &m2s, k, min_sharpe)
                .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
            prop_assert_eq!(got, again);
            prop_assert!(got <= binomial(counts.len() as u64, k as u64));
        }

        /// Fold bit-parity: the production fold and the independently
        /// written re-fold agree BITWISE on every combination, including
        /// near-zero-mean sign-flip territory.
        #[test]
        fn fold_bitwise_matches_independent_refold(
            groups in proptest::collection::vec(
                (1i64..=50, -1e-12f64..=1e-12, 0.0f64..=1e-6), 2..=6,
            ),
        ) {
            let counts: Vec<i64> = groups.iter().map(|g| g.0).collect();
            let means: Vec<f64> = groups.iter().map(|g| g.1).collect();
            let m2s: Vec<f64> = groups.iter().map(|g| g.2).collect();
            let combo: Vec<usize> = (0..counts.len()).collect();
            let a = fold_sharpe(&counts, &means, &m2s, &combo);
            let b = naive_fold(&counts, &means, &m2s, &combo);
            prop_assert_eq!(a.map(bits), b.map(bits));
        }
    }
}
