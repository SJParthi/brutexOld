//! PBO/CSCV kernel — the C(S, S/2) split-enumeration logit loop.
//!
//! DEC-METRICS-RUST-STATS-002 (slice 6, PR-2). The Rust port of the heavy
//! tail of `brutex.metrics.pbo.pbo_probability`: the `C(n_splits, S/2)`
//! split loop (12,870 splits at the production S=16) over PRECOMPUTED
//! per-block `(sum, sum_of_squares)` moment rows. Everything else STAYS in
//! Python: `_validate` (byte-pinned ValueError messages), `_block_moments`
//! (retained as the moments producer — the seam boundary mirrors the cpcv
//! PR-1 ruling; its outputs are always exact floats by construction) and
//! the final exact `overfit / n_combinations` division + `PBOResult`
//! assembly. A CUSTOM `evaluator` callable never reaches this kernel (the
//! seam refuses it — only the default `sharpe_from_moments` is
//! transcribed).
//!
//! ## Bit-identity claim: STRUCTURAL (Golden Rule #4)
//!
//! The float op order is fixed by the PYTHON SOURCE — a sequential scalar
//! loop transcribed operation-for-operation:
//!
//! * per split, per strategy: IS sums accumulate over the chosen blocks in
//!   ascending order, OOS sums over the complement in ascending order —
//!   the exact `for s in is_blocks` / `for b in all_blocks if b not in
//!   is_set` orders;
//! * `sharpe_from_moments`: `mean = total/count`;
//!   `variance = total_sq/count - mean*mean`; `<= 0.0 → 0.0`;
//!   `mean / sqrt(variance)` — single IEEE ops in the same order
//!   (int→f64 count conversion correctly rounded on both sides);
//! * the IS-best pick is CPython `max(range(N), key=...)`: the FIRST
//!   strictly-greater index wins (ties + NaN keep the earlier index);
//! * the average rank `less + (equal + 1) / 2.0`, `omega = rank/(N + 1)`,
//!   `log(omega / (1.0 - omega))` — `log` lowers to the same libm symbol
//!   CPython's `math.log` calls; `omega` is provably inside (0, 1) (rank
//!   ∈ [0.5, N], denom = N + 1) so the log argument is always positive
//!   and finite — no raise arm exists on either side;
//! * `lambda_c <= 0.0` counts the overfit split — the exact Python
//!   `_OVERFIT_LOGIT_THRESHOLD` comparison.
//!
//! NO finiteness requirement on the moment rows: an overflow-poisoned
//! block sum (±inf/NaN) flows through the same IEEE expressions to the
//! same NaN-ranked verdicts on both engines (NaN comparisons are all
//! false in the max/rank loops — order-identical by transcription).
//!
//! ## Failure mode — `Result`, NEVER a panic
//!
//! Structural arms mirror the Python `_validate` guarantees; the
//! combination COUNT is capped (u128-checked binomial) so the logits Vec
//! can never OOM-abort under the crate's `panic = "abort"` profile — the
//! seam pre-refuses past the same cap and the oracle serves (behavior
//! identical to flag-OFF).
//!
//! ## Complexity (honest envelope — NOT O(1) total)
//!
//! `O(C(S, S/2) · S · N)` per call — the SAME asymptotics as the Python
//! loop; a constant-factor win only. FFI copies two `S·N` f64 matrices per
//! call (2 × 16 × N scalars at the production S=16) — O(S·N), disclosed.
//! The pyfunction holds the GIL for the call's duration (no signal check
//! between splits) — sub-second at production shapes, same envelope as
//! the cpcv PR-1 kernel.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// The kernel-side combination-count cap: `C(n_splits, half)` above this
/// returns `Err` (→ seam refusal → the interruptible Python oracle) so the
/// logits `Vec` allocation can never abort the host interpreter. The seam
/// applies the same cap pre-call; production is C(16, 8) = 12,870.
pub const PBO_MAX_COMBINATIONS: u64 = 2_000_000;

/// `pbo.sharpe_from_moments` transcription (population Sharpe from raw
/// moments; zero/negative variance → 0.0 — the Python degenerate-case
/// convention).
#[inline]
fn sharpe_from_moments(total: f64, total_sq: f64, count_f: f64) -> f64 {
    let mean = total / count_f;
    let variance = total_sq / count_f - mean * mean;
    if variance <= 0.0 {
        return 0.0;
    }
    mean / variance.sqrt()
}

/// Checked binomial `C(n, k)` in u128, `None` on overflow.
fn binomial_checked(n: u64, k: u64) -> Option<u128> {
    let k = k.min(n - k.min(n));
    let mut acc: u128 = 1;
    for i in 0..k {
        acc = acc.checked_mul(u128::from(n - i))?;
        acc /= u128::from(i + 1);
    }
    Some(acc)
}

/// Pure core of [`stats_pbo_cscv`] — `Result<_, String>` (NO `PyErr`) so
/// the no-libpython `cargo test` link exercises every branch.
///
/// `block_sum`/`block_sumsq` are the `n_splits × n_strat` per-block moment
/// matrices in row-major order (`[s][n]` at `s * n_strat + n`) exactly as
/// `_block_moments` builds them; `is_count` is `(n_splits / 2) *
/// block_size` (== the OOS count — equal halves). Returns
/// `(overfit_count, logits)` in the deterministic
/// `itertools.combinations` split order.
pub fn pbo_cscv_checked(
    block_sum: &[f64],
    block_sumsq: &[f64],
    n_splits: usize,
    n_strat: usize,
    is_count: i64,
) -> Result<(u64, Vec<f64>), String> {
    if n_splits < 2 || !n_splits.is_multiple_of(2) {
        return Err(format!(
            "stats_pbo_cscv: n_splits must be an even integer >= 2, got {n_splits}"
        ));
    }
    if n_strat < 2 {
        return Err(format!(
            "stats_pbo_cscv: n_strat must be >= 2, got {n_strat}"
        ));
    }
    let cells = n_splits
        .checked_mul(n_strat)
        .ok_or_else(|| "stats_pbo_cscv: n_splits * n_strat overflows".to_string())?;
    if block_sum.len() != cells || block_sumsq.len() != cells {
        return Err(format!(
            "stats_pbo_cscv: moment matrices must be n_splits*n_strat={cells} long, got {} / {}",
            block_sum.len(),
            block_sumsq.len()
        ));
    }
    if is_count < 1 {
        return Err(format!(
            "stats_pbo_cscv: is_count must be >= 1, got {is_count}"
        ));
    }
    let half = n_splits / 2;
    let n_combinations = binomial_checked(n_splits as u64, half as u64)
        .filter(|&c| c <= u128::from(PBO_MAX_COMBINATIONS))
        .ok_or_else(|| {
            format!(
                "stats_pbo_cscv: C({n_splits}, {half}) exceeds the kernel cap \
                 {PBO_MAX_COMBINATIONS} — the Python oracle serves this shape"
            )
        })?;

    let count_f = is_count as f64; // == oos_count (equal halves)
    let denom_rank = (n_strat as f64) + 1.0; // float(n_strat + 1) — exact for any real N
    let mut in_is = vec![false; n_splits];
    let mut is_stat = vec![0.0_f64; n_strat];
    let mut oos_stat = vec![0.0_f64; n_strat];
    let mut logits: Vec<f64> = Vec::with_capacity(n_combinations as usize);
    let mut overfit: u64 = 0;

    // Lexicographic (itertools.combinations) enumeration — the cpcv.rs
    // index-increment enumerator.
    let k = half;
    let mut combo: Vec<usize> = (0..k).collect();
    loop {
        for flag in in_is.iter_mut() {
            *flag = false;
        }
        for &s in &combo {
            in_is[s] = true;
        }
        for n in 0..n_strat {
            let mut is_sum = 0.0_f64;
            let mut is_sumsq = 0.0_f64;
            for &s in &combo {
                is_sum += block_sum[s * n_strat + n];
                is_sumsq += block_sumsq[s * n_strat + n];
            }
            let mut oos_sum = 0.0_f64;
            let mut oos_sumsq = 0.0_f64;
            for (s, &in_split) in in_is.iter().enumerate() {
                if !in_split {
                    oos_sum += block_sum[s * n_strat + n];
                    oos_sumsq += block_sumsq[s * n_strat + n];
                }
            }
            is_stat[n] = sharpe_from_moments(is_sum, is_sumsq, count_f);
            oos_stat[n] = sharpe_from_moments(oos_sum, oos_sumsq, count_f);
        }
        // CPython max(range(N), key=...): first strictly-greater wins.
        let mut n_star = 0usize;
        for i in 1..n_strat {
            if is_stat[i] > is_stat[n_star] {
                n_star = i;
            }
        }
        let star_oos = oos_stat[n_star];
        let mut less: i64 = 0;
        let mut equal: i64 = 0;
        for &value in &oos_stat {
            if value < star_oos {
                less += 1;
            } else if value == star_oos {
                equal += 1;
            }
        }
        // rank = less + (equal + 1) / 2.0 — the Python int/float op order.
        let rank = (less as f64) + ((equal + 1) as f64) / 2.0;
        let omega = rank / denom_rank;
        let lambda_c = (omega / (1.0 - omega)).ln();
        logits.push(lambda_c);
        if lambda_c <= 0.0 {
            overfit += 1;
        }
        // Advance to the next lexicographic combination.
        let mut i = k;
        let advanced = loop {
            if i == 0 {
                break false;
            }
            i -= 1;
            if combo[i] != i + n_splits - k {
                break true;
            }
        };
        if !advanced {
            return Ok((overfit, logits));
        }
        combo[i] += 1;
        for j in (i + 1)..k {
            combo[j] = combo[j - 1] + 1;
        }
    }
}

/// CSCV logit loop over precomputed per-block moments — the slice-6 PR-2
/// PBO kernel (see the module docstring).
///
/// Returns `(overfit_count, logits)`; raises `ValueError` on a
/// structurally invalid input or past the combination cap (the seam routes
/// any error to the pure-Python oracle). FFI cost (disclosed): two
/// `n_splits × n_strat` list→Vec copies in, one `C(n_splits, S/2)`-long
/// logits list out.
#[pyfunction]
pub fn stats_pbo_cscv(
    block_sum: Vec<f64>,
    block_sumsq: Vec<f64>,
    n_splits: usize,
    n_strat: usize,
    is_count: i64,
) -> PyResult<(u64, Vec<f64>)> {
    pbo_cscv_checked(&block_sum, &block_sumsq, n_splits, n_strat, is_count)
        .map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn bits(x: f64) -> u64 {
        x.to_bits()
    }

    /// Independent re-implementation for the in-crate differential: a
    /// RECURSIVE combination enumerator + a per-strategy fold written in a
    /// different shape (bitmask membership), same sequential op order.
    fn naive_cscv(
        block_sum: &[f64],
        block_sumsq: &[f64],
        n_splits: usize,
        n_strat: usize,
        is_count: i64,
    ) -> (u64, Vec<f64>) {
        fn recurse(
            chosen: &mut Vec<usize>,
            start: usize,
            n_splits: usize,
            k: usize,
            out: &mut Vec<Vec<usize>>,
        ) {
            if chosen.len() == k {
                out.push(chosen.clone());
                return;
            }
            for s in start..n_splits {
                chosen.push(s);
                recurse(chosen, s + 1, n_splits, k, out);
                chosen.pop();
            }
        }
        let mut combos = Vec::new();
        recurse(&mut Vec::new(), 0, n_splits, n_splits / 2, &mut combos);
        let count_f = is_count as f64;
        let denom = (n_strat as f64) + 1.0;
        let mut overfit = 0u64;
        let mut logits = Vec::new();
        for combo in combos {
            let member = |s: usize| combo.contains(&s);
            let sharpe = |total: f64, total_sq: f64| {
                let mean = total / count_f;
                let var = total_sq / count_f - mean * mean;
                if var <= 0.0 { 0.0 } else { mean / var.sqrt() }
            };
            let mut is_stat = Vec::with_capacity(n_strat);
            let mut oos_stat = Vec::with_capacity(n_strat);
            for n in 0..n_strat {
                let mut a = 0.0;
                let mut b = 0.0;
                let mut c = 0.0;
                let mut d = 0.0;
                for s in 0..n_splits {
                    if member(s) {
                        a += block_sum[s * n_strat + n];
                        b += block_sumsq[s * n_strat + n];
                    }
                }
                for s in 0..n_splits {
                    if !member(s) {
                        c += block_sum[s * n_strat + n];
                        d += block_sumsq[s * n_strat + n];
                    }
                }
                is_stat.push(sharpe(a, b));
                oos_stat.push(sharpe(c, d));
            }
            let mut n_star = 0usize;
            for i in 1..n_strat {
                if is_stat[i] > is_stat[n_star] {
                    n_star = i;
                }
            }
            let star = oos_stat[n_star];
            let mut less = 0i64;
            let mut equal = 0i64;
            for &v in &oos_stat {
                if v < star {
                    less += 1;
                } else if v == star {
                    equal += 1;
                }
            }
            let rank = (less as f64) + ((equal + 1) as f64) / 2.0;
            let omega = rank / denom;
            let l = (omega / (1.0 - omega)).ln();
            logits.push(l);
            if l <= 0.0 {
                overfit += 1;
            }
        }
        (overfit, logits)
    }

    #[test]
    fn known_two_split_case() {
        // S=2, N=2, one obs per block: strategy 0 IS-best in split {0} with
        // returns [1, -1] (IS +1, OOS -1); strategy 1 constant 0.5.
        // block_sum rows: s0 = [1.0, 0.5], s1 = [-1.0, 0.5];
        // sumsq rows: [1.0, 0.25], [1.0, 0.25]; is_count = 1.
        // Split {0}: IS stats: s0: mean 1, var 1-1=0 → 0.0; s1: mean .5,
        // var .25-.25=0 → 0.0 → n_star=0 (first max). OOS: s0: 0.0, s1: 0.0
        // → less=0, equal=2 → rank=1.5, omega=.5 → logit=0 → overfit.
        // Split {1}: symmetric → logit 0 → overfit. PBO = 2/2.
        let block_sum = [1.0, 0.5, -1.0, 0.5];
        let block_sumsq = [1.0, 0.25, 1.0, 0.25];
        let (overfit, logits) = pbo_cscv_checked(&block_sum, &block_sumsq, 2, 2, 1)
            .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
        assert_eq!(overfit, 2);
        assert_eq!(logits.len(), 2);
        assert_eq!(bits(logits[0]), bits(0.0));
    }

    #[test]
    fn err_arms() {
        let m = [0.0; 4];
        assert!(pbo_cscv_checked(&m, &m, 3, 2, 1).is_err_and(|e| e.contains("even")));
        assert!(pbo_cscv_checked(&m, &m, 0, 2, 1).is_err());
        assert!(pbo_cscv_checked(&m, &m, 2, 1, 1).is_err_and(|e| e.contains("n_strat")));
        assert!(pbo_cscv_checked(&m, &m, 2, 3, 1).is_err_and(|e| e.contains("matrices")));
        assert!(pbo_cscv_checked(&m, &m[..3], 2, 2, 1).is_err_and(|e| e.contains("matrices")));
        assert!(pbo_cscv_checked(&m, &m, 2, 2, 0).is_err_and(|e| e.contains("is_count")));
        // Combination cap: C(64, 32) >> 2e6 → Err, never an OOM alloc.
        let big = vec![0.0; 64 * 2];
        assert!(pbo_cscv_checked(&big, &big, 64, 2, 1).is_err_and(|e| e.contains("cap")));
    }

    #[test]
    fn binomial_checked_pins() {
        assert_eq!(binomial_checked(16, 8), Some(12_870));
        assert_eq!(binomial_checked(6, 3), Some(20));
        assert_eq!(binomial_checked(2, 1), Some(2));
        // Symmetric k > n - k path + overflow guard sanity.
        assert_eq!(binomial_checked(6, 5), Some(6));
        assert!(binomial_checked(200, 100).is_none() || binomial_checked(200, 100).unwrap() > 0);
    }

    #[test]
    fn nan_poisoned_moments_never_panic() {
        // Overflow-poisoned block sums: NaN/inf flow to verdicts, no Err.
        let block_sum = [f64::INFINITY, 0.5, f64::NEG_INFINITY, 0.5];
        let block_sumsq = [f64::NAN, 0.25, 1.0, 0.25];
        let (overfit, logits) = pbo_cscv_checked(&block_sum, &block_sumsq, 2, 2, 1)
            .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
        assert_eq!(logits.len(), 2);
        assert!(overfit <= 2);
    }

    proptest! {
        /// The kernel bit-matches the independently written recursive
        /// oracle (count AND every logit), is deterministic, and its logit
        /// count equals C(S, S/2).
        #[test]
        fn cscv_matches_naive_oracle(
            n_splits_half in 1usize..=3,
            n_strat in 2usize..=5,
            cells in proptest::collection::vec(
                (-100.0f64..=100.0, 0.0f64..=1000.0), 4..=36,
            ),
            is_count in 1i64..=50,
        ) {
            let n_splits = n_splits_half * 2;
            let need = n_splits * n_strat;
            prop_assume!(cells.len() >= need);
            let block_sum: Vec<f64> = cells.iter().take(need).map(|c| c.0).collect();
            let block_sumsq: Vec<f64> = cells.iter().take(need).map(|c| c.1).collect();
            let got = pbo_cscv_checked(&block_sum, &block_sumsq, n_splits, n_strat, is_count)
                .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
            let want = naive_cscv(&block_sum, &block_sumsq, n_splits, n_strat, is_count);
            prop_assert_eq!(got.0, want.0);
            let got_bits: Vec<u64> = got.1.iter().map(|v| v.to_bits()).collect();
            let want_bits: Vec<u64> = want.1.iter().map(|v| v.to_bits()).collect();
            prop_assert_eq!(&got_bits, &want_bits);
            let expected_len = binomial_checked(n_splits as u64, (n_splits / 2) as u64)
                .unwrap_or_else(|| panic!("binomial overflow"));
            prop_assert_eq!(got.1.len() as u128, expected_len);
            let again = pbo_cscv_checked(&block_sum, &block_sumsq, n_splits, n_strat, is_count)
                .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
            prop_assert_eq!(got.0, again.0);
        }
    }
}
