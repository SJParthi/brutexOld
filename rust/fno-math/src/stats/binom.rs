//! Exact binomial upper-tail kernel — the low-support rescue lane's gate.
//!
//! DEC-STAGE1B-LOW-SUPPORT-RESCUE-001 (LANE W3, the 2026-07-18 operator
//! approve-all ruling). BORN-RUST per the standing Rust-O(1) totality law:
//! the rescue lane's exact binomial sign-test gate is implemented here from
//! day one — Python carries NO production copy (the transcription oracle
//! lives tests-side only, `tests/property/test_binom_tail_parity.py`).
//!
//! The statistic: for a rescued candidate with `n` observable trades of
//! which `k` were wins, the one-sided upper-tail p-value under the null
//! "win/loss are fair coin flips" is `P(X >= k)` for `X ~ Binomial(n, p)`
//! (production `p = 0.5` — the exact sign test; the kernel is general in
//! `p` for the hostile-input differential). Returned in LOG space
//! (`ln P(X >= k)`) so a deep tail (`n = 29, k = 29` → `p ≈ 1.9e-9`)
//! never underflows through an intermediate.
//!
//! ## Algorithm (log-space, numerically stable, sequential)
//!
//! 1. `ln C(n, k)` via the exact product form `Σ_{i=1..k} ln((n-k+i)/i)` —
//!    O(k), no `lgamma` (not in stable `std`), no factorial overflow.
//! 2. `ln pmf(k) = ln C(n,k) + k·ln(p) + (n-k)·ln(1-p)`.
//! 3. The tail accumulates `pmf(i)` for `i = k..n` via the pmf ratio
//!    `pmf(i+1)/pmf(i) = ((n-i)/(i+1)) · (p/(1-p))` in log space with a
//!    streaming two-operand log-sum-exp — a FIXED left-to-right fold order
//!    (deterministic bit-for-bit on one platform: pure `ln`/`exp`/`ln_1p`
//!    IEEE-754 binary64 ops in a source-fixed sequence).
//! 4. The accumulated log-probability is clamped to `<= 0.0` (float
//!    accumulation may land ε above an exact 1.0 — a probability never
//!    exceeds 1).
//!
//! ## Honest parity envelope (Golden Rule #10)
//!
//! Cross-LANGUAGE bit-identity is NOT claimed: `ln`/`exp`/`ln_1p` are libm
//! calls whose last-ULP rounding may differ between Rust's libm and
//! CPython's — exactly why the cpcv sibling keeps Python's libm calls
//! Python-side. This kernel is the PRODUCTION value (the gate consumes it
//! directly); the tests-side oracles bound it instead of bit-pinning it:
//! an exact `fractions.Fraction` oracle at the production `p = 0.5`
//! (dyadic — the tail is an exact rational) plus an op-for-op Python
//! transcription for general `p`, both within 1e-9 log-units (hypothesis
//! differential over hostile `n`/`k`/`p`). A candidate within 1e-9
//! log-units of the `ln(1e-4)` gate threshold could verdict-differ between
//! kernel and oracle — a measure-zero disclosed band, never silent.
//!
//! ## Complexity (honest envelope — NOT O(1) total)
//!
//! O(n) per call (`O(k)` for the binomial coefficient + `O(n-k)` tail
//! terms), O(1) space. The rescue lane calls it once per rescued candidate
//! with `n < MIN_TRADES = 30`, so the production cost is a bounded handful
//! of libm ops — never on the per-bar/per-mask hot path. The bench pin
//! (`tests/bench/test_rescue_binom_bench.py`) asserts the LINEAR envelope;
//! O(1)-total is deliberately NOT claimed.
//!
//! ## Failure mode — `Result`, NEVER a panic
//!
//! Under the crate's `overflow-checks = true` + `panic = "abort"` release
//! profile a panic would SIGABRT the host interpreter; every invalid input
//! (`n < 0`, `k < 0`, `p` outside `[0, 1]` or NaN) returns `Err(String)`
//! mapped to `ValueError` at the pyfunction boundary. The checked/py split
//! mirrors `stats::cpcv` so the no-libpython `cargo test` link exercises
//! every branch.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// `ln C(n, k)` via the exact product form — O(k), overflow-free.
///
/// Precondition (upheld by [`log_binom_sf_checked`]): `0 <= k <= n`.
fn ln_choose(n: i64, k: i64) -> f64 {
    // C(n, k) == C(n, n-k): fold over the smaller side for fewer libm calls
    // (the value is symmetric; the fold order is source-fixed either way).
    let k = k.min(n - k);
    let mut acc = 0.0_f64;
    for i in 1..=k {
        // ln((n - k + i) / i), left-to-right — the source-fixed fold order.
        acc += (((n - k + i) as f64) / (i as f64)).ln();
    }
    acc
}

/// Pure core of [`stats_binom_tail_log_sf`] — `Result<_, String>` (NO
/// `PyErr`) so the no-libpython `cargo test` link exercises every branch.
///
/// Returns `ln P(X >= k)` for `X ~ Binomial(n, p)`; `-inf` for an empty
/// tail (`k > n`, or `p == 0` with `k >= 1`), `0.0` for a certain one
/// (`k <= 0` — the whole support — or `p == 1` with `k <= n`).
pub fn log_binom_sf_checked(n: i64, k: i64, p: f64) -> Result<f64, String> {
    if n < 0 {
        return Err(format!("n must be >= 0, got {n}"));
    }
    if k < 0 {
        return Err(format!("k must be >= 0, got {k}"));
    }
    if !p.is_finite() || !(0.0..=1.0).contains(&p) {
        return Err(format!("p must be a finite probability in [0, 1], got {p}"));
    }
    if k == 0 {
        return Ok(0.0); // P(X >= 0) == 1 exactly.
    }
    if k > n {
        return Ok(f64::NEG_INFINITY); // Empty tail: P(X > n) == 0.
    }
    if p == 0.0 {
        return Ok(f64::NEG_INFINITY); // k >= 1 but X ≡ 0.
    }
    if p == 1.0 {
        return Ok(0.0); // X ≡ n and k <= n ⇒ the tail is certain.
    }
    let ln_p = p.ln();
    let ln_q = (1.0 - p).ln();
    // ln pmf(k), then walk i = k..n accumulating the tail via the pmf ratio.
    let mut ln_term = ln_choose(n, k) + (k as f64) * ln_p + ((n - k) as f64) * ln_q;
    let mut acc = ln_term;
    let ln_odds = ln_p - ln_q; // ln(p / (1-p)) — one libm subtraction, hoisted.
    for i in k..n {
        // pmf(i+1)/pmf(i) = ((n - i) / (i + 1)) · (p / (1 - p)), in logs.
        ln_term += (((n - i) as f64) / ((i + 1) as f64)).ln() + ln_odds;
        // Streaming two-operand log-sum-exp, larger operand anchored — the
        // source-fixed fold order (deterministic on one platform).
        let (hi, lo) = if acc >= ln_term {
            (acc, ln_term)
        } else {
            (ln_term, acc)
        };
        acc = hi + (lo - hi).exp().ln_1p();
    }
    // A log-probability: float accumulation may land ε above an exact 1.0.
    Ok(acc.min(0.0))
}

/// `ln P(X >= k)` for `X ~ Binomial(n, p)` — the rescue lane's exact
/// binomial sign-test gate (DEC-STAGE1B-LOW-SUPPORT-RESCUE-001).
///
/// O(n) per call, O(1) space (see the module docstring's honest envelope).
/// Raises `ValueError` on `n < 0`, `k < 0`, or `p` outside `[0, 1]`/NaN.
#[pyfunction]
pub fn stats_binom_tail_log_sf(n: i64, k: i64, p: f64) -> PyResult<f64> {
    log_binom_sf_checked(n, k, p).map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::{ln_choose, log_binom_sf_checked};

    const TOL: f64 = 1e-12;

    fn sf(n: i64, k: i64, p: f64) -> f64 {
        log_binom_sf_checked(n, k, p).expect("valid input")
    }

    #[test]
    fn ln_choose_exact_small_values() {
        assert!((ln_choose(5, 0) - 0.0).abs() < TOL);
        assert!((ln_choose(5, 5) - 0.0).abs() < TOL);
        assert!((ln_choose(5, 2) - 10.0_f64.ln()).abs() < TOL);
        assert!((ln_choose(30, 15) - 155117520.0_f64.ln()).abs() < 1e-9);
    }

    #[test]
    fn all_wins_at_half_is_exact_power_of_two() {
        // P(X >= 10 | n=10, p=0.5) = 1/1024 exactly.
        assert!((sf(10, 10, 0.5) - (1.0_f64 / 1024.0).ln()).abs() < 1e-10);
        // P(X >= 1 | n=1, p=0.5) = 1/2.
        assert!((sf(1, 1, 0.5) - 0.5_f64.ln()).abs() < TOL);
    }

    #[test]
    fn symmetric_median_tail_is_half() {
        // P(X >= 3 | n=5, p=0.5) = (10+5+1)/32 = 0.5 exactly.
        assert!((sf(5, 3, 0.5) - 0.5_f64.ln()).abs() < 1e-12);
    }

    #[test]
    fn rescue_band_reference_value() {
        // n=30, k=25, p=0.5: tail = (142506+27405+4060+435+30+1)/2^30
        //                          = 174437 / 1073741824 ≈ 1.6245e-4.
        let exact = (174437.0_f64 / 1073741824.0).ln();
        assert!((sf(30, 25, 0.5) - exact).abs() < 1e-10);
    }

    #[test]
    fn asymmetric_p_reference_value() {
        // P(X >= 2 | n=3, p=0.25) = 3·(1/16)·(3/4) + 1/64 = 10/64.
        let exact = (10.0_f64 / 64.0).ln();
        assert!((sf(3, 2, 0.25) - exact).abs() < 1e-12);
    }

    #[test]
    fn edge_laws() {
        assert_eq!(sf(10, 0, 0.5), 0.0); // whole support ⇒ P = 1
        assert_eq!(sf(0, 0, 0.5), 0.0);
        assert_eq!(sf(10, 11, 0.5), f64::NEG_INFINITY); // empty tail
        assert_eq!(sf(10, 3, 0.0), f64::NEG_INFINITY); // X ≡ 0, k >= 1
        assert_eq!(sf(10, 0, 0.0), 0.0); // k == 0 short-circuits first
        assert_eq!(sf(10, 10, 1.0), 0.0); // X ≡ n
        assert_eq!(sf(7, 0, 1.0), 0.0);
    }

    #[test]
    fn never_positive_and_monotone_in_k() {
        for n in [1_i64, 2, 7, 29, 30, 200] {
            let mut prev = f64::INFINITY;
            for k in 0..=n {
                let v = sf(n, k, 0.5);
                assert!(v <= 0.0, "log-probability must be <= 0, got {v}");
                assert!(v <= prev + 1e-12, "tail must be non-increasing in k");
                prev = v;
            }
        }
    }

    #[test]
    fn validation_errors_are_results_never_panics() {
        assert!(log_binom_sf_checked(-1, 0, 0.5).is_err());
        assert!(log_binom_sf_checked(5, -1, 0.5).is_err());
        assert!(log_binom_sf_checked(5, 2, f64::NAN).is_err());
        assert!(log_binom_sf_checked(5, 2, 1.5).is_err());
        assert!(log_binom_sf_checked(5, 2, -0.1).is_err());
        assert!(log_binom_sf_checked(5, 2, f64::INFINITY).is_err());
    }

    #[test]
    fn deep_tail_does_not_underflow_to_neg_inf() {
        // n=200, k=200, p=0.5 ⇒ ln(2^-200) = -200·ln2 ≈ -138.63 — a value
        // whose LINEAR-space probability (≈6.2e-61) is representable, but the
        // log path must land on it without drama either way.
        let v = sf(200, 200, 0.5);
        assert!((v - (-(200.0_f64) * 2.0_f64.ln())).abs() < 1e-8);
        assert!(v.is_finite());
    }
}
