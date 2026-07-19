//! Deflated-Sharpe kernels — PSR + expected-max-Sharpe (Bailey & LdP).
//!
//! DEC-METRICS-RUST-STATS-002 (slice 6, PR-2). Operation-for-operation
//! transcriptions of the two post-validation computation tails of
//! `brutex/metrics/deflated_sharpe.py`:
//!
//! * [`stats_psr`] — the Probabilistic Sharpe Ratio tail of
//!   `probabilistic_sharpe_ratio` (radicand → z → `NormalDist().cdf(z)`).
//! * [`stats_expected_max_sharpe`] — the `SR_0` tail of
//!   `expected_max_sharpe_ratio` (two AS241 quantiles + the Gumbel
//!   combination).
//!
//! ## Bit-identity claim: STRUCTURAL + the erf symbol law
//!
//! The float op order is fixed by the PYTHON SOURCE (sequential scalar
//! expressions transcribed one-for-one; no FMA, no reassociation); `sqrt`
//! is a correctly rounded IEEE instruction on both sides; `log` lowers to
//! the same libm symbol CPython's `math.log` calls; `erf` is BOUND to the
//! same libm symbol CPython's `math.erf` calls ([`super::norm`]'s law).
//! Int→f64 conversions (`sample_length`, `n_trials`) are correctly rounded
//! on both sides (CPython `PyLong_AsDouble` ≡ Rust `as f64` for every i64).
//!
//! ## Failure mode — `Result`, NEVER a panic
//!
//! Every guard the Python source raises `ValueError`/`StatisticsError` for
//! is an `Err` arm here; post-Python-validation they are structurally
//! unreachable except the `inv_cdf` domain (`n_trials` ≳ 9e15 makes
//! `1 - 1/N` round to 1.0 — Python raises `StatisticsError`), where the
//! seam's refusal hands the call to the oracle so the identical exception
//! surfaces. No arithmetic here can overflow an integer (`sample_length -
//! 1` on a pre-validated `>= 2` i64; float overflow saturates to ±inf
//! exactly as CPython's float arithmetic does).
//!
//! ## Complexity (honest envelope)
//!
//! O(1) per call — a handful of scalar IEEE ops (the same as the Python
//! tail; the win is skipping the interpreter, material only because the
//! Stage-3 overfit gate calls this once per LEADERBOARD CANDIDATE). FFI
//! copies: five scalars per call — nil.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use super::norm::{std_norm_cdf, std_norm_inv_cdf_checked};

/// `deflated_sharpe._EULER_MASCHERONI` — the same decimal literal, hence
/// the same correctly-rounded f64 (bit-pinned by the in-file test).
const EULER_MASCHERONI: f64 = 0.5772156649015329;

/// `deflated_sharpe._MIN_SAMPLE_LENGTH` (PSR needs `n >= 2`).
const MIN_SAMPLE_LENGTH: i64 = 2;

/// Pure core of [`stats_psr`] — `Result<_, String>` so the no-libpython
/// `cargo test` link exercises every branch (the crate convention).
///
/// Transcribes `probabilistic_sharpe_ratio`'s guards + tail:
/// `radicand = 1.0 - skew*SR + (kurt - 1.0)/4.0*SR*SR` (left-to-right,
/// division before the multiply chain — the Python order), then
/// `z = (SR - SR*) * sqrt(n - 1) / sqrt(radicand)`, then
/// `0.5 * (1.0 + erf((z - 0.0) / (1.0 * sqrt2)))`.
pub fn psr_checked(
    observed_sharpe: f64,
    benchmark_sharpe: f64,
    sample_length: i64,
    skew: f64,
    kurtosis: f64,
) -> Result<f64, String> {
    if sample_length < MIN_SAMPLE_LENGTH {
        return Err(format!(
            "stats_psr: sample_length must be >= {MIN_SAMPLE_LENGTH}, got {sample_length}"
        ));
    }
    if !(observed_sharpe.is_finite()
        && benchmark_sharpe.is_finite()
        && skew.is_finite()
        && kurtosis.is_finite())
    {
        return Err("stats_psr: inputs must be finite".to_string());
    }
    let radicand =
        1.0 - skew * observed_sharpe + (kurtosis - 1.0) / 4.0 * observed_sharpe * observed_sharpe;
    if radicand <= 0.0 {
        return Err(format!(
            "stats_psr: estimator-variance radicand must be > 0, got {radicand}"
        ));
    }
    let z = (observed_sharpe - benchmark_sharpe) * ((sample_length - 1) as f64).sqrt()
        / radicand.sqrt();
    Ok(std_norm_cdf(z))
}

/// Pure core of [`stats_expected_max_sharpe`].
///
/// Transcribes `expected_max_sharpe_ratio`: the `n_trials >= 1` /
/// `variance >= 0` guards (a NaN variance PASSES `< 0.0` on both sides and
/// propagates — Python parity), the `n_trials == 1 → 0.0` limit, then
/// `std = sqrt(V)`; `q_main = inv_cdf(1.0 - 1.0/N)`;
/// `q_tail = inv_cdf(1.0 - 1.0/(N * e))`;
/// `std * ((1.0 - γ)*q_main + γ*q_tail)`.
pub fn expected_max_sharpe_checked(
    n_trials: i64,
    trial_sharpe_variance: f64,
) -> Result<f64, String> {
    if n_trials < 1 {
        return Err(format!(
            "stats_expected_max_sharpe: n_trials must be >= 1, got {n_trials}"
        ));
    }
    if trial_sharpe_variance < 0.0 {
        return Err(format!(
            "stats_expected_max_sharpe: trial_sharpe_variance must be >= 0, got \
             {trial_sharpe_variance}"
        ));
    }
    if n_trials == 1 {
        // E[max] of a single zero-mean draw is its mean = 0 (no deflation).
        return Ok(0.0);
    }
    let std = trial_sharpe_variance.sqrt();
    let quantile_main = std_norm_inv_cdf_checked(1.0 - 1.0 / (n_trials as f64))?;
    let quantile_tail =
        std_norm_inv_cdf_checked(1.0 - 1.0 / ((n_trials as f64) * std::f64::consts::E))?;
    Ok(std * ((1.0 - EULER_MASCHERONI) * quantile_main + EULER_MASCHERONI * quantile_tail))
}

/// Probabilistic Sharpe Ratio `PSR(SR*)` over pre-validated scalars — the
/// slice-6 PR-2 DSR kernel (see the module docstring).
///
/// Raises `ValueError` on a domain-invalid input (structurally unreachable
/// behind the Python validation); the seam routes any error to the
/// pure-Python oracle. FFI cost: five scalars per call.
#[pyfunction]
pub fn stats_psr(
    observed_sharpe: f64,
    benchmark_sharpe: f64,
    sample_length: i64,
    skew: f64,
    kurtosis: f64,
) -> PyResult<f64> {
    psr_checked(
        observed_sharpe,
        benchmark_sharpe,
        sample_length,
        skew,
        kurtosis,
    )
    .map_err(PyValueError::new_err)
}

/// Expected maximum Sharpe `SR_0` of `n_trials` zero-edge trials — the
/// slice-6 PR-2 deflation-benchmark kernel (see the module docstring).
///
/// Raises `ValueError` on a domain-invalid input OR on the `inv_cdf`
/// raise domain (`1 - 1/N` rounding to 1.0 at astronomical `N` — the seam
/// refusal makes the oracle raise the identical `StatisticsError`).
/// FFI cost: two scalars per call.
#[pyfunction]
pub fn stats_expected_max_sharpe(n_trials: i64, trial_sharpe_variance: f64) -> PyResult<f64> {
    expected_max_sharpe_checked(n_trials, trial_sharpe_variance).map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bits(x: f64) -> u64 {
        x.to_bits()
    }

    #[test]
    fn euler_mascheroni_bit_pin() {
        // The same decimal literal as deflated_sharpe._EULER_MASCHERONI —
        // pin the exact bits so a digit typo can never ship.
        assert_eq!(bits(EULER_MASCHERONI), 0x3FE2788CFC6FB619);
    }

    #[test]
    fn e_const_matches_python_math_e() {
        // math.e is the correctly rounded double of e — same as Rust's E
        // (bit pin captured from CPython: struct.pack('<d', math.e)).
        assert_eq!(bits(std::f64::consts::E), 0x4005BF0A8B145769);
    }

    #[test]
    fn psr_golden_bit_pin() {
        // Captured from CPython 3.12: probabilistic_sharpe_ratio(
        //   0.15, 0.0, 250, -0.6, 5.5) == 0.9874954488927481 on the CI
        // platform (glibc erf) — a full-chain bit pin.
        let got = psr_checked(0.15, 0.0, 250, -0.6, 5.5)
            .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
        assert_eq!(got, 0.9874954488927481_f64);
        assert_eq!(bits(got), bits(0.9874954488927481_f64));
    }

    #[test]
    fn psr_zero_edge_is_half() {
        // SR = SR* = 0 → z = 0 → cdf(0) = 0.5 exactly.
        let got =
            psr_checked(0.0, 0.0, 100, 0.0, 3.0).unwrap_or_else(|e| panic!("unexpected Err: {e}"));
        assert_eq!(bits(got), bits(0.5));
    }

    #[test]
    fn psr_err_arms() {
        assert!(psr_checked(0.1, 0.0, 1, 0.0, 3.0).is_err_and(|m| m.contains("sample_length")));
        assert!(psr_checked(f64::NAN, 0.0, 10, 0.0, 3.0).is_err_and(|m| m.contains("finite")));
        assert!(psr_checked(0.1, f64::INFINITY, 10, 0.0, 3.0).is_err());
        assert!(psr_checked(0.1, 0.0, 10, f64::NAN, 3.0).is_err());
        assert!(psr_checked(0.1, 0.0, 10, 0.0, f64::NEG_INFINITY).is_err());
        // Radicand <= 0: skew 10 at SR 1 → 1 - 10 + 0.5 < 0.
        assert!(psr_checked(1.0, 0.0, 10, 10.0, 3.0).is_err_and(|m| m.contains("radicand")));
    }

    #[test]
    fn expected_max_sharpe_golden_bit_pin() {
        // Captured from CPython 3.12: expected_max_sharpe_ratio(1000, 0.04)
        // == 0.6510243027305447 (AS241 + glibc log) — a full-chain bit pin.
        let got = expected_max_sharpe_checked(1000, 0.04)
            .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
        assert_eq!(got, 0.6510243027305447_f64);
        assert_eq!(bits(got), bits(0.6510243027305447_f64));
    }

    #[test]
    fn expected_max_sharpe_limits() {
        // N = 1 → exactly 0.0 (the Python early return).
        assert_eq!(
            expected_max_sharpe_checked(1, 123.0).map(bits),
            Ok(bits(0.0))
        );
        // V = 0 → exactly 0.0 (std = 0; quantiles finite).
        assert_eq!(
            expected_max_sharpe_checked(50, 0.0).map(bits),
            Ok(bits(0.0))
        );
        // NaN variance passes the `< 0.0` guard and propagates (Python
        // parity: sqrt(NaN) → NaN result, never a raise).
        let nan = expected_max_sharpe_checked(50, f64::NAN);
        assert!(nan.is_ok_and(f64::is_nan));
    }

    #[test]
    fn expected_max_sharpe_err_arms() {
        assert!(expected_max_sharpe_checked(0, 0.1).is_err_and(|m| m.contains("n_trials")));
        assert!(expected_max_sharpe_checked(-3, 0.1).is_err());
        assert!(expected_max_sharpe_checked(10, -0.5).is_err_and(|m| m.contains("variance")));
        // The inv_cdf raise domain: N huge enough that 1 - 1/N rounds to
        // 1.0 in f64 (the Python StatisticsError arm → seam refusal).
        assert!(expected_max_sharpe_checked(i64::MAX, 0.1).is_err());
    }

    #[test]
    fn monotone_in_n_trials() {
        // SR_0 is monotonically non-decreasing in N (the documented law).
        let mut prev =
            expected_max_sharpe_checked(2, 1.0).unwrap_or_else(|e| panic!("unexpected Err: {e}"));
        for n in [3_i64, 10, 100, 10_000, 1_000_000] {
            let cur = expected_max_sharpe_checked(n, 1.0)
                .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
            assert!(cur >= prev, "SR_0 must not decrease: n={n} {cur} < {prev}");
            prev = cur;
        }
    }
}
