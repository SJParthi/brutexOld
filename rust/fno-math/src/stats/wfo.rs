//! WFO pass-count kernel — per-walk test-window Sharpe over a returns
//! series, replicating numpy's pairwise reduction bit-for-bit.
//!
//! DEC-METRICS-RUST-STATS-002 (slice 6, PR-2). The Rust port of
//! `brutex.metrics.wfo.wfo_pass_rate`'s per-walk loop: for each
//! walk-forward test window, `sharpe_of_returns(arr[start:stop])` and the
//! STRICT `sharpe > min_sharpe` pass predicate. Walk SCHEDULING
//! (`walk_forward_splits` — pure integer arithmetic) and every validation
//! raise stay in Python.
//!
//! ## Bit-identity claim: REPLICATED (the mod.rs taxonomy)
//!
//! `sharpe_of_returns` computes `arr.mean()` and `np.mean((arr - mean) **
//! 2)` — numpy PAIRWISE reductions. [`pairwise_sum`] mirrors numpy's
//! `pairwise_sum` algorithm (numpy `_core/src/umath/loops.c.src`: n < 8
//! sequential; n <= 128 the 8-accumulator blocked loop with the fixed
//! `((r0+r1)+(r2+r3)) + ((r4+r5)+(r6+r7))` combine; else recursive halving
//! with `n2 -= n2 % 8`), and `(arr - mean) ** 2` is element-wise
//! `(v - mean) * (v - mean)` (numpy's power-2 fast path). This mirrors a
//! NUMPY-INTERNAL order — not fixed by the Python source — so the claim is
//! REPLICATED and fenced empirically: probed bit-equal on the CI platform
//! (numpy 2.5, 4,000 hostile trials over sizes 1..3000, zero mismatches),
//! re-proven per platform by the seam's differential/property suites, and
//! the burn-in autorevert latch is the runtime fence if a numpy upgrade or
//! a different SIMD dispatch ever reorders the reduction (the kernel then
//! serves nothing; the oracle serves every call — bit-exactness is never
//! at risk, only the constant-factor win).
//!
//! ## Failure mode — `Result`, NEVER a panic
//!
//! Structural arms (bounds, empty windows) mirror states the Python
//! validation makes unreachable; recursion depth is `log2(n)` — bounded.
//!
//! ## Complexity (honest envelope — NOT O(1) total)
//!
//! `O(Σ test-window sizes)` per call — the SAME asymptotics as the Python
//! loop; a constant-factor win only. FFI copies the whole returns series +
//! two walk-bound vectors per call — O(n + walks), disclosed. One
//! `O(window)` scratch Vec per walk (the centered-squares intermediate —
//! the same intermediate numpy materialises).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// A Sharpe needs at least this many samples (`cpcv._MIN_SAMPLES` — the
/// same constant `sharpe_of_returns` guards with).
const MIN_SAMPLES: usize = 2;

/// numpy `pairwise_sum` (DOUBLE) — see the module docstring for the
/// algorithm provenance. Operates on a slice; the recursion splits at
/// `n/2` rounded DOWN to a multiple of 8, exactly as numpy does.
fn pairwise_sum(a: &[f64]) -> f64 {
    let n = a.len();
    if n < 8 {
        let mut res = 0.0_f64;
        for &v in a {
            res += v;
        }
        return res;
    }
    if n <= 128 {
        let mut r = [a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]];
        let mut i = 8usize;
        while i < n - (n % 8) {
            for (j, acc) in r.iter_mut().enumerate() {
                *acc += a[i + j];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += a[i];
            i += 1;
        }
        return res;
    }
    let mut n2 = n / 2;
    n2 -= n2 % 8;
    pairwise_sum(&a[..n2]) + pairwise_sum(&a[n2..])
}

/// `cpcv.sharpe_of_returns` transcription over one window: numpy-pairwise
/// mean, element-wise centered squares, numpy-pairwise variance, the
/// non-finite/zero-variance `None` fail-safe, `mean / sqrt(variance)`.
fn sharpe_of_window(window: &[f64]) -> Option<f64> {
    if window.len() < MIN_SAMPLES {
        return None;
    }
    let n_f = window.len() as f64;
    let mean = pairwise_sum(window) / n_f;
    let centered_sq: Vec<f64> = window
        .iter()
        .map(|&v| {
            let d = v - mean;
            d * d
        })
        .collect();
    let variance = pairwise_sum(&centered_sq) / n_f;
    if !variance.is_finite() || variance <= 0.0 {
        return None;
    }
    Some(mean / variance.sqrt())
}

/// Pure core of [`stats_wfo_pass_count`] — `Result<_, String>` so the
/// no-libpython `cargo test` link exercises every branch.
///
/// Counts the walks whose test-window Sharpe is defined AND STRICTLY
/// greater than `min_sharpe` (an undefined window — too short, zero or
/// non-finite variance — is a non-pass, the Python fail-safe).
pub fn wfo_pass_count_checked(
    returns: &[f64],
    test_starts: &[i64],
    test_stops: &[i64],
    min_sharpe: f64,
) -> Result<u64, String> {
    if test_starts.len() != test_stops.len() {
        return Err(format!(
            "stats_wfo_pass_count: test_starts/test_stops length mismatch ({} / {})",
            test_starts.len(),
            test_stops.len()
        ));
    }
    if test_starts.is_empty() {
        // wfo_pass_rate raises BEFORE the seam on zero walks — Err arm for
        // the fallback law only.
        return Err("stats_wfo_pass_count: no walks".to_string());
    }
    let n = returns.len() as i64;
    let mut n_pass: u64 = 0;
    for (&start, &stop) in test_starts.iter().zip(test_stops.iter()) {
        if start < 0 || stop <= start || stop > n {
            // walk_forward_splits never emits an empty/out-of-bounds test
            // window — structurally unreachable behind the Python caller.
            return Err(format!(
                "stats_wfo_pass_count: invalid test window [{start}, {stop}) over {n} returns"
            ));
        }
        let window = &returns[start as usize..stop as usize];
        if let Some(sharpe) = sharpe_of_window(window) {
            // STRICT `>` — the wfo.py pass predicate.
            if sharpe > min_sharpe {
                n_pass += 1;
            }
        }
    }
    Ok(n_pass)
}

/// Walk-forward pass-count over a returns series — the slice-6 PR-2 WFO
/// kernel (see the module docstring).
///
/// `test_starts`/`test_stops` are the walks' half-open test windows in
/// chronological order (from `walk_forward_splits`). Raises `ValueError`
/// on a structurally invalid input; the seam routes any error to the
/// pure-Python oracle. FFI cost (disclosed): one O(n) returns copy + two
/// O(walks) bound vectors per call.
#[pyfunction]
pub fn stats_wfo_pass_count(
    returns: Vec<f64>,
    test_starts: Vec<i64>,
    test_stops: Vec<i64>,
    min_sharpe: f64,
) -> PyResult<u64> {
    wfo_pass_count_checked(&returns, &test_starts, &test_stops, min_sharpe)
        .map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn bits(x: f64) -> u64 {
        x.to_bits()
    }

    /// Independent pairwise re-implementation (index-range recursion
    /// instead of slice recursion) — the in-crate differential twin.
    fn naive_pairwise(a: &[f64], lo: usize, n: usize) -> f64 {
        if n < 8 {
            let mut res = 0.0;
            for i in 0..n {
                res += a[lo + i];
            }
            return res;
        }
        if n <= 128 {
            let mut r: Vec<f64> = (0..8).map(|j| a[lo + j]).collect();
            let mut i = 8;
            while i < n - (n % 8) {
                for (j, acc) in r.iter_mut().enumerate() {
                    *acc += a[lo + i + j];
                }
                i += 8;
            }
            let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
            while i < n {
                res += a[lo + i];
                i += 1;
            }
            return res;
        }
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        naive_pairwise(a, lo, n2) + naive_pairwise(a, lo + n2, n - n2)
    }

    #[test]
    fn known_value_pins() {
        // Window [1, 2, 3, 4]: mean 2.5, centered squares [2.25, .25, .25,
        // 2.25] → variance 1.25 — every step exactly representable.
        let s = sharpe_of_window(&[1.0, 2.0, 3.0, 4.0]);
        let want = 2.5_f64 / 1.25_f64.sqrt();
        assert_eq!(s.map(bits), Some(bits(want)));
        // Undefined windows: too short, zero variance.
        assert_eq!(sharpe_of_window(&[1.0]), None);
        assert_eq!(sharpe_of_window(&[2.0, 2.0, 2.0]), None);
        // Overflow-to-inf variance → None (the fail-safe).
        assert_eq!(sharpe_of_window(&[1e300, -1e300]), None);
    }

    #[test]
    fn pass_count_strict_gt_boundary() {
        // One walk over [0, 4): sharpe exactly 2.5/sqrt(1.25).
        let r = [1.0, 2.0, 3.0, 4.0];
        let achieved = 2.5_f64 / 1.25_f64.sqrt();
        let at = wfo_pass_count_checked(&r, &[0], &[4], achieved)
            .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
        assert_eq!(at, 0); // kills `>` → `>=`
        let below = wfo_pass_count_checked(&r, &[0], &[4], f64::from_bits(bits(achieved) - 1))
            .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
        assert_eq!(below, 1);
    }

    #[test]
    fn err_arms() {
        let r = [1.0, 2.0, 3.0];
        assert!(
            wfo_pass_count_checked(&r, &[0, 1], &[2], 0.0).is_err_and(|e| e.contains("mismatch"))
        );
        assert!(wfo_pass_count_checked(&r, &[], &[], 0.0).is_err_and(|e| e.contains("no walks")));
        assert!(wfo_pass_count_checked(&r, &[-1], &[2], 0.0).is_err());
        assert!(wfo_pass_count_checked(&r, &[1], &[1], 0.0).is_err()); // empty window
        assert!(wfo_pass_count_checked(&r, &[0], &[4], 0.0).is_err()); // past the end
    }

    #[test]
    fn pairwise_matches_independent_twin_at_size_boundaries() {
        // Sizes straddling every algorithm boundary: 7/8, 127/128/129, the
        // recursive split, and an odd tail.
        for n in [
            1usize, 7, 8, 9, 15, 16, 127, 128, 129, 255, 256, 1000, 4096, 46_000,
        ] {
            let a: Vec<f64> = (0..n)
                .map(|i| ((i * 2_654_435_761 % 1000) as f64 - 500.0) * 1e-3)
                .collect();
            assert_eq!(
                bits(pairwise_sum(&a)),
                bits(naive_pairwise(&a, 0, n)),
                "size {n}"
            );
        }
    }

    proptest! {
        /// Pairwise bit-parity vs the independent twin over hostile
        /// magnitudes, and determinism of the pass count.
        #[test]
        fn pairwise_bitwise_matches_twin(
            vals in proptest::collection::vec(-1e12f64..=1e12, 0..=600),
        ) {
            prop_assert_eq!(
                bits(pairwise_sum(&vals)),
                bits(naive_pairwise(&vals, 0, vals.len()))
            );
        }

        /// The pass count is bounded by the walk count and deterministic.
        #[test]
        fn pass_count_bounded_and_deterministic(
            vals in proptest::collection::vec(-10.0f64..=10.0, 24..=120),
            min_sharpe in -2.0f64..=2.0,
        ) {
            let n = vals.len() as i64;
            let starts: Vec<i64> = (0..n - 8).step_by(8).collect();
            let stops: Vec<i64> = starts.iter().map(|s| s + 8).collect();
            let a = wfo_pass_count_checked(&vals, &starts, &stops, min_sharpe)
                .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
            let b = wfo_pass_count_checked(&vals, &starts, &stops, min_sharpe)
                .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
            prop_assert_eq!(a, b);
            prop_assert!(a <= starts.len() as u64);
        }
    }
}
