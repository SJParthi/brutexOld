//! Strategy-decay + champion/challenger verdict kernels (LANE W5).
//!
//! DEC-NIGHTLY-DECAY-R48-001 + DEC-CHAMPION-CHALLENGER-001 — the BORN-RUST
//! verdict/fold math of the nightly continuous-adaptation loop (the
//! rust-o1-totality-law BORN-RUST rule: new adaptation mechanisms are
//! implemented in Rust from day one, never Python-first). Three kernels:
//!
//! 1. [`decay_trailing_fold`] — one sequential pass over a per-trade return
//!    series → `(sharpe, max_drawdown, net)`: population-std Sharpe (zero
//!    risk-free, no annualisation — the `sharpe_of_returns` FORMULA) and the
//!    largest peak-to-trough decline of the cumulative-return equity path
//!    (the lockbox `_max_drawdown` law), both in return units.
//! 2. [`decay_verdict`] — the R48 decay verdict over
//!    `(trailing, discovery)` metric pairs under the LOCKBOX tolerance law
//!    (`DEC-LOCK-001`: trailing Sharpe `< (1 − sharpe_tolerance) ×
//!    discovery` OR trailing max-DD `> (1 + mdd_tolerance) × discovery`
//!    ⇒ DECAYED), with honest low-N / undefined ABSTAIN codes that are
//!    NEVER a DECAYED verdict.
//! 3. [`champion_dominates`] — the challenger-vs-champion domination
//!    compare (strictly higher lockbox Sharpe AND max-DD no worse).
//!
//! ## Bit-identity claim: STRUCTURAL vs the Python oracle (Golden Rule #4)
//!
//! The Python oracle (`brutex/nightly/decay_math.py`) is written as PURE
//! sequential Python loops — deliberately NOT numpy — and this module
//! transcribes it operation-for-operation: left-to-right sequential sums,
//! two-pass mean/variance, per-element `if x > peak` peak tracking (NaN
//! comparisons false on both sides), single IEEE ops on identical operands,
//! NO FMA / Kahan / rayon. CPython float ops and Rust f64 ops are both
//! single correctly-rounded IEEE-754 operations, so equal op order ⇒ equal
//! bits. The burn-in in `decay_math` bit-compares the first calls per
//! process and autoreverts to the oracle on ANY mismatch (ship-oracle law).
//!
//! HONEST divergence note: `brutex.metrics.cpcv.sharpe_of_returns` computes
//! the SAME formula with numpy PAIRWISE reductions; a sequential sum can
//! differ from it by ULPs at large n. The decay verdict is a 30%-tolerance
//! compare, so ULP-level divergence is immaterial — disclosed, never hidden.
//!
//! ## Failure mode — `Result`, NEVER a panic
//!
//! Verdict/compare inputs are validated at the boundary (`Result<_,
//! String>` cores → `PyValueError` at the pyfunction edge) exactly like the
//! sibling `stats::cpcv` kernel: under the crate's `overflow-checks = true`
//! + `panic = "abort"` release profile a panic would SIGABRT the host
//! interpreter, so nothing here panics on ANY input.
//!
//! ## Complexity (honest envelope)
//!
//! [`decay_trailing_fold`] is `O(n)` in the return-series length (a trailing
//! window holds tens–hundreds of trades — trivial); the verdict/domination
//! kernels are `O(1)` scalar compares. The FFI copies one `Vec<f64>` per
//! fold call — `O(n)`, disclosed.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// A Sharpe needs at least this many samples (mirror of cpcv.py
/// `_MIN_SAMPLES`): below it the fold's Sharpe is `None` (undefined).
const MIN_SAMPLES: usize = 2;

/// Decay verdict code: trailing metrics within tolerance of discovery.
pub const VERDICT_HEALTHY: u8 = 0;
/// Decay verdict code: trailing Sharpe below the tolerance floor OR
/// trailing max-DD above the tolerance ceiling.
pub const VERDICT_DECAYED: u8 = 1;
/// Decay verdict code: too few trailing trades to judge (`n_trades <
/// min_trades`) — an honest abstain, NEVER a decay claim.
pub const VERDICT_ABSTAIN_LOW_N: u8 = 2;
/// Decay verdict code: the trailing Sharpe is undefined (`None` /
/// non-finite) or the trailing max-DD is non-finite — abstain.
pub const VERDICT_ABSTAIN_UNDEFINED: u8 = 3;

/// Pure fold core: `(sharpe, max_drawdown, net)` over one return series.
///
/// Op-order transcription of the Python oracle
/// (`decay_math._oracle_trailing_fold`): sequential left-to-right sum for
/// the mean, a second sequential pass for the population variance, and one
/// sequential cumulative-equity walk with `if equity > peak` /
/// `if peak - equity > max_dd` updates (NaN-safe: both comparisons are
/// false for NaN on both languages). Never errors — any f64 input folds
/// (non-finite values propagate IEEE-identically on both sides).
pub fn trailing_fold_core(returns: &[f64]) -> (Option<f64>, f64, f64) {
    let n = returns.len();
    // Sequential net (also the mean numerator) — left-to-right, no pairwise.
    let mut total = 0.0_f64;
    for &r in returns {
        total += r;
    }
    let sharpe = if n < MIN_SAMPLES {
        None
    } else {
        let mean = total / n as f64;
        let mut sq_sum = 0.0_f64;
        for &r in returns {
            let delta = r - mean;
            sq_sum += delta * delta;
        }
        let variance = sq_sum / n as f64;
        if !variance.is_finite() || variance <= 0.0 {
            None
        } else {
            Some(mean / variance.sqrt())
        }
    };
    // Max drawdown of the cumulative-return equity path (lockbox law):
    // equity = cumsum(returns); peak = running max; mdd = max(peak - equity).
    let mut equity = 0.0_f64;
    let mut peak = f64::NEG_INFINITY;
    let mut max_dd = 0.0_f64;
    for &r in returns {
        equity += r;
        if equity > peak {
            peak = equity;
        }
        let dd = peak - equity;
        if dd > max_dd {
            max_dd = dd;
        }
    }
    (sharpe, max_dd, total)
}

/// Pure verdict core — `Result` so the boundary arms are testable without
/// Python symbols (the `stats::cpcv` split).
#[allow(clippy::too_many_arguments)]
pub fn decay_verdict_core(
    trailing_sharpe: Option<f64>,
    discovery_sharpe: f64,
    trailing_max_dd: f64,
    discovery_max_dd: f64,
    n_trades: u64,
    min_trades: u64,
    sharpe_tolerance: f64,
    mdd_tolerance: f64,
) -> Result<u8, String> {
    if !sharpe_tolerance.is_finite() || sharpe_tolerance < 0.0 {
        return Err(format!(
            "decay_verdict: sharpe_tolerance must be finite and >= 0, got {sharpe_tolerance}"
        ));
    }
    if !mdd_tolerance.is_finite() || mdd_tolerance < 0.0 {
        return Err(format!(
            "decay_verdict: mdd_tolerance must be finite and >= 0, got {mdd_tolerance}"
        ));
    }
    if !discovery_sharpe.is_finite() {
        return Err(format!(
            "decay_verdict: discovery_sharpe must be finite, got {discovery_sharpe}"
        ));
    }
    if !discovery_max_dd.is_finite() || discovery_max_dd < 0.0 {
        return Err(format!(
            "decay_verdict: discovery_max_dd must be finite and >= 0, got {discovery_max_dd}"
        ));
    }
    if n_trades < min_trades {
        return Ok(VERDICT_ABSTAIN_LOW_N);
    }
    let sharpe = match trailing_sharpe {
        None => return Ok(VERDICT_ABSTAIN_UNDEFINED),
        Some(s) if !s.is_finite() => return Ok(VERDICT_ABSTAIN_UNDEFINED),
        Some(s) => s,
    };
    if !trailing_max_dd.is_finite() {
        return Ok(VERDICT_ABSTAIN_UNDEFINED);
    }
    let sharpe_floor = discovery_sharpe * (1.0 - sharpe_tolerance);
    let mdd_ceiling = discovery_max_dd * (1.0 + mdd_tolerance);
    if sharpe < sharpe_floor || trailing_max_dd > mdd_ceiling {
        Ok(VERDICT_DECAYED)
    } else {
        Ok(VERDICT_HEALTHY)
    }
}

/// Pure domination core: challenger strictly beats the champion on Sharpe
/// with a max-DD no worse. Non-finite inputs are refused (a promotion must
/// never ride an undefined metric).
pub fn champion_dominates_core(
    challenger_sharpe: f64,
    challenger_max_dd: f64,
    champion_sharpe: f64,
    champion_max_dd: f64,
) -> Result<bool, String> {
    for (name, value) in [
        ("challenger_sharpe", challenger_sharpe),
        ("challenger_max_dd", challenger_max_dd),
        ("champion_sharpe", champion_sharpe),
        ("champion_max_dd", champion_max_dd),
    ] {
        if !value.is_finite() {
            return Err(format!(
                "champion_dominates: {name} must be finite, got {value}"
            ));
        }
    }
    Ok(challenger_sharpe > champion_sharpe && challenger_max_dd <= champion_max_dd)
}

/// PyO3 face of [`trailing_fold_core`] — `(sharpe | None, max_drawdown, net)`.
#[pyfunction]
pub fn decay_trailing_fold(returns: Vec<f64>) -> (Option<f64>, f64, f64) {
    trailing_fold_core(&returns)
}

/// PyO3 face of [`decay_verdict_core`] — the verdict code (`0` healthy /
/// `1` decayed / `2` abstain-low-N / `3` abstain-undefined).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn decay_verdict(
    trailing_sharpe: Option<f64>,
    discovery_sharpe: f64,
    trailing_max_dd: f64,
    discovery_max_dd: f64,
    n_trades: u64,
    min_trades: u64,
    sharpe_tolerance: f64,
    mdd_tolerance: f64,
) -> PyResult<u8> {
    decay_verdict_core(
        trailing_sharpe,
        discovery_sharpe,
        trailing_max_dd,
        discovery_max_dd,
        n_trades,
        min_trades,
        sharpe_tolerance,
        mdd_tolerance,
    )
    .map_err(PyValueError::new_err)
}

/// PyO3 face of [`champion_dominates_core`].
#[pyfunction]
pub fn champion_dominates(
    challenger_sharpe: f64,
    challenger_max_dd: f64,
    champion_sharpe: f64,
    champion_max_dd: f64,
) -> PyResult<bool> {
    champion_dominates_core(
        challenger_sharpe,
        challenger_max_dd,
        champion_sharpe,
        champion_max_dd,
    )
    .map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;

    const TOL_SHARPE: f64 = 0.30;
    const TOL_MDD: f64 = 0.50;

    // ── trailing_fold_core ─────────────────────────────────────────────

    #[test]
    fn fold_empty_series() {
        assert_eq!(trailing_fold_core(&[]), (None, 0.0, 0.0));
    }

    #[test]
    fn fold_single_sample_has_no_sharpe() {
        let (sharpe, mdd, net) = trailing_fold_core(&[0.25]);
        assert_eq!(sharpe, None); // n < MIN_SAMPLES
        assert_eq!(mdd, 0.0);
        assert_eq!(net, 0.25);
    }

    #[test]
    fn fold_flat_series_has_no_sharpe() {
        // 0.25 is exactly representable, so mean == every sample and the
        // population variance is EXACTLY zero → Sharpe undefined. (A flat
        // series of a non-representable value like 0.1 has a tiny but REAL
        // variance — the cpcv.py `[3.3] * n` note — and a large finite
        // Sharpe; that is the formula, not a bug.)
        let (sharpe, mdd, net) = trailing_fold_core(&[0.25, 0.25, 0.25]);
        assert_eq!(sharpe, None); // zero variance
        assert_eq!(mdd, 0.0);
        assert_eq!(net, 0.75);
    }

    #[test]
    fn fold_exact_values_hand_derived() {
        // returns [0.1, -0.2, 0.3]: total = 0.20000000000000004 (exact IEEE
        // left-to-right); mean = total/3; two-pass population variance;
        // equity path [0.1, -0.1, 0.2] → peaks [0.1, 0.1, 0.2] →
        // drawdowns [0.0, 0.2, 0.0] → mdd = 0.2 (exact: 0.1 - (-0.1)).
        let returns = [0.1_f64, -0.2, 0.3];
        let (sharpe, mdd, net) = trailing_fold_core(&returns);
        let total = 0.1_f64 + -0.2 + 0.3;
        assert_eq!(net, total);
        let mean = total / 3.0;
        let mut sq = 0.0_f64;
        for r in returns {
            let d = r - mean;
            sq += d * d;
        }
        let expected_sharpe = mean / (sq / 3.0).sqrt();
        assert_eq!(sharpe, Some(expected_sharpe));
        let expected_mdd = 0.1_f64 - (0.1_f64 + -0.2);
        assert_eq!(mdd, expected_mdd);
    }

    #[test]
    fn fold_monotonic_up_has_zero_drawdown() {
        let (_, mdd, _) = trailing_fold_core(&[0.1, 0.2, 0.3]);
        assert_eq!(mdd, 0.0);
    }

    #[test]
    fn fold_monotonic_down_drawdown_is_full_decline() {
        let (_, mdd, _) = trailing_fold_core(&[-0.1, -0.2, -0.3]);
        // equity [-0.1, -0.3(0000..4), -0.6]; peak = -0.1 (first element —
        // the running peak starts at the FIRST equity point, mirroring
        // np.maximum.accumulate over cumsum, NOT at 0.0).
        let e1 = -0.1_f64;
        let e3 = -0.1_f64 + -0.2 + -0.3;
        assert_eq!(mdd, e1 - e3);
    }

    #[test]
    fn fold_nan_propagates_to_abstainable_outputs() {
        let (sharpe, mdd, net) = trailing_fold_core(&[0.1, f64::NAN, 0.2]);
        assert_eq!(sharpe, None); // NaN variance is not finite
        // NaN poisons equity from index 1 on; comparisons with NaN are
        // false, so peak freezes at 0.1 and dd stays 0.0 (never NaN>0.0).
        assert_eq!(mdd, 0.0);
        assert!(net.is_nan());
    }

    // ── decay_verdict_core ─────────────────────────────────────────────

    #[test]
    fn verdict_healthy_inside_both_tolerances() {
        // floor = 1.0*0.7 = 0.7; ceiling = 0.1*1.5 = 0.15000000000000002
        let v = decay_verdict_core(Some(0.7), 1.0, 0.15, 0.1, 30, 10, TOL_SHARPE, TOL_MDD);
        assert_eq!(v, Ok(VERDICT_HEALTHY)); // 0.7 >= 0.7 exactly (kills < → <=)
    }

    #[test]
    fn verdict_decayed_below_sharpe_floor() {
        let floor = 1.0_f64 * (1.0 - TOL_SHARPE);
        let below = f64::from_bits(floor.to_bits() - 1); // one ULP below
        let v = decay_verdict_core(Some(below), 1.0, 0.0, 0.1, 30, 10, TOL_SHARPE, TOL_MDD);
        assert_eq!(v, Ok(VERDICT_DECAYED));
    }

    #[test]
    fn verdict_decayed_above_mdd_ceiling() {
        let ceiling = 0.1_f64 * (1.0 + TOL_MDD);
        let above = f64::from_bits(ceiling.to_bits() + 1); // one ULP above
        let v = decay_verdict_core(Some(2.0), 1.0, above, 0.1, 30, 10, TOL_SHARPE, TOL_MDD);
        assert_eq!(v, Ok(VERDICT_DECAYED));
        // exactly AT the ceiling stays healthy (kills > → >=)
        let at = decay_verdict_core(Some(2.0), 1.0, ceiling, 0.1, 30, 10, TOL_SHARPE, TOL_MDD);
        assert_eq!(at, Ok(VERDICT_HEALTHY));
    }

    #[test]
    fn verdict_negative_discovery_sharpe_floor_law() {
        // A NEGATIVE discovery Sharpe's floor is (1-tol)*negative — HIGHER
        // (less negative) than discovery itself; trailing at discovery is
        // below the floor ⇒ decayed. This mirrors the lockbox formula
        // verbatim (no special-casing; disclosed in decay_math).
        let v = decay_verdict_core(Some(-1.0), -1.0, 0.0, 0.1, 30, 10, TOL_SHARPE, TOL_MDD);
        assert_eq!(v, Ok(VERDICT_DECAYED));
    }

    #[test]
    fn verdict_abstains_low_n_before_everything() {
        let v = decay_verdict_core(Some(-99.0), 1.0, 9.9, 0.1, 9, 10, TOL_SHARPE, TOL_MDD);
        assert_eq!(v, Ok(VERDICT_ABSTAIN_LOW_N));
        // boundary: n == min_trades judges (kills < → <=)
        let at = decay_verdict_core(Some(-99.0), 1.0, 9.9, 0.1, 10, 10, TOL_SHARPE, TOL_MDD);
        assert_eq!(at, Ok(VERDICT_DECAYED));
    }

    #[test]
    fn verdict_abstains_on_undefined_trailing() {
        for ts in [None, Some(f64::NAN), Some(f64::INFINITY)] {
            let v = decay_verdict_core(ts, 1.0, 0.0, 0.1, 30, 10, TOL_SHARPE, TOL_MDD);
            assert_eq!(v, Ok(VERDICT_ABSTAIN_UNDEFINED));
        }
        let v = decay_verdict_core(Some(1.0), 1.0, f64::NAN, 0.1, 30, 10, TOL_SHARPE, TOL_MDD);
        assert_eq!(v, Ok(VERDICT_ABSTAIN_UNDEFINED));
    }

    #[test]
    fn verdict_boundary_validation_errors() {
        let cases: [(Option<f64>, f64, f64, f64, f64, f64); 5] = [
            (Some(1.0), 1.0, 0.0, 0.1, -0.1, TOL_MDD), // negative sharpe tol
            (Some(1.0), 1.0, 0.0, 0.1, f64::NAN, TOL_MDD), // NaN sharpe tol
            (Some(1.0), 1.0, 0.0, 0.1, TOL_SHARPE, -1.0), // negative mdd tol
            (Some(1.0), f64::NAN, 0.0, 0.1, TOL_SHARPE, TOL_MDD), // NaN discovery
            (Some(1.0), 1.0, 0.0, -0.1, TOL_SHARPE, TOL_MDD), // negative discovery mdd
        ];
        for (ts, ds, tmdd, dmdd, stol, mtol) in cases {
            assert!(decay_verdict_core(ts, ds, tmdd, dmdd, 30, 10, stol, mtol).is_err());
        }
        assert!(
            decay_verdict_core(
                Some(1.0),
                1.0,
                0.0,
                f64::INFINITY,
                30,
                10,
                TOL_SHARPE,
                TOL_MDD
            )
            .is_err()
        );
    }

    // ── champion_dominates_core ────────────────────────────────────────

    #[test]
    fn domination_requires_strictly_higher_sharpe() {
        assert_eq!(champion_dominates_core(1.1, 0.1, 1.0, 0.1), Ok(true));
        assert_eq!(champion_dominates_core(1.0, 0.1, 1.0, 0.1), Ok(false)); // tie loses
        assert_eq!(champion_dominates_core(0.9, 0.1, 1.0, 0.1), Ok(false));
    }

    #[test]
    fn domination_requires_mdd_no_worse() {
        assert_eq!(champion_dominates_core(2.0, 0.2, 1.0, 0.1), Ok(false));
        assert_eq!(champion_dominates_core(2.0, 0.1, 1.0, 0.1), Ok(true)); // equal mdd ok
    }

    #[test]
    fn domination_refuses_non_finite() {
        assert!(champion_dominates_core(f64::NAN, 0.1, 1.0, 0.1).is_err());
        assert!(champion_dominates_core(1.0, f64::INFINITY, 1.0, 0.1).is_err());
        assert!(champion_dominates_core(1.0, 0.1, f64::NEG_INFINITY, 0.1).is_err());
        assert!(champion_dominates_core(1.0, 0.1, 1.0, f64::NAN).is_err());
    }
}
