//! Moneyness decision kernels (DEC-OPTCOST-TAILS-RUST-001).
//!
//! Rust twins of the pure decision math in `brutex/data/moneyness.py`:
//! [`optcost_moneyness_classify`] (`classify`, lines 68-103),
//! [`optcost_atm_offset`] (`atm_offset`, lines 238-261) and
//! [`optcost_strike_step`] (`derive_strike_step`, lines 106-141).
//!
//! ## Division of labour (the never-raises contract stays Python)
//!
//! The Python module is a TOTAL function surface — junk classifies
//! NA/UNKNOWN/None, it never raises. The seam keeps that: Python decides
//! NA (non-option type) and UNKNOWN/None (`_is_positive_finite` Decimal
//! validation, `moneyness.py:56-65`) BEFORE the FFI, and quantizes the
//! validated Decimals to i64 paisa; a Decimal NOT exactly representable as
//! integer paisa routes to the Python oracle (disclosed degrade — never
//! approximated). The kernels below therefore take pre-validated positive
//! i64 paisa; a violated precondition is `Err` → the seam degrades to the
//! oracle, so no new raise class can escape.
//!
//! ## `atm_offset` ROUND_HALF_DOWN — exact integer law (the proof)
//!
//! Python (`moneyness.py:261`) computes
//! `int(((strike - spot) / step).to_integral_value(rounding=ROUND_HALF_DOWN))`
//! under the DEFAULT 28-significant-digit decimal context. The kernel
//! computes round-half-toward-zero of the exact rational `n / d`
//! (`n = strike_paisa - spot_paisa`, `d = step_paisa > 0`) in pure integer
//! arithmetic. These agree for EVERY i64-paisa input:
//!
//! * If the exact quotient is a half-integer tie, `2n = (2k+1)·d` exactly;
//!   `|k| <= 2^63 / d · ...` has at most 21 significant digits <= 28, so the
//!   Decimal division is EXACT and `ROUND_HALF_DOWN` rounds the true tie
//!   toward zero — identical to the integer tie branch.
//! * Otherwise the exact quotient's distance to the nearest half-integer
//!   boundary is >= `1 / (2d)` (a nonzero integer over `2d`). The
//!   correctly-rounded 28-digit quotient errs by < `0.5 ulp <=
//!   0.5 · 10^(e-27)` where `10^e <= |q|`. Crossing (or landing on) a
//!   boundary would need `0.5 · 10^(e-27) >= 1/(2d)`, i.e.
//!   `|n| ≈ |q|·d >= 10^27` — impossible for `|n| < 2^64 < 1.9·10^19`.
//!
//! So the 28-digit rounding can never flip the integral value; the integer
//! law is bit-exact vs the Decimal oracle over the whole i64 paisa domain
//! (boundary pins below; the flag-ON hypothesis differential in the Python
//! stage re-proves it end-to-end).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::optcost::{CODE_NO_STEP, CODE_OK};

// ── Moneyness bucket codes (the graded buckets the kernel can return;
// NA=0 / UNKNOWN=1 are decided Python-side BEFORE the FFI) ──────────────

/// `Moneyness.ITM` wire code.
pub const MONEYNESS_ITM: i64 = 2;
/// `Moneyness.ATM` wire code.
pub const MONEYNESS_ATM: i64 = 3;
/// `Moneyness.OTM` wire code.
pub const MONEYNESS_OTM: i64 = 4;

/// Pure core of [`optcost_moneyness_classify`].
///
/// Transcribes `moneyness.py:97-103` over i64 paisa: `diff = spot - strike`;
/// `|diff| <= band` → ATM; CALL: `diff > 0` → ITM else OTM; PUT: the mirror.
/// (The `band` clamp `atm_band if (atm_band is not None and atm_band >= 0)
/// else 0` at line 97 is applied Python-side; the kernel requires
/// `band >= 0`.)
pub fn moneyness_classify_checked(
    is_put: bool,
    strike_paisa: i64,
    spot_paisa: i64,
    band_paisa: i64,
) -> Result<i64, String> {
    if strike_paisa <= 0 || spot_paisa <= 0 {
        return Err(format!(
            "optcost_moneyness_classify: strike/spot must be positive paisa \
             (got {strike_paisa} / {spot_paisa}) — validity is decided Python-side"
        ));
    }
    if band_paisa < 0 {
        return Err(format!(
            "optcost_moneyness_classify: band must be >= 0 (got {band_paisa}) — \
             the negative-band clamp is applied Python-side (moneyness.py:97)"
        ));
    }
    // moneyness.py:98 — diff = spot - strike (> 0 → spot above strike).
    // Both operands are positive i64, so the subtraction cannot overflow.
    let diff = spot_paisa - strike_paisa;
    // moneyness.py:99 — abs(diff) <= band → ATM (INCLUSIVE edge).
    if diff.abs() <= band_paisa {
        return Ok(MONEYNESS_ATM);
    }
    // moneyness.py:101-103 — CALL: ITM iff diff > 0; PUT: ITM iff diff < 0.
    let itm = if is_put { diff < 0 } else { diff > 0 };
    Ok(if itm { MONEYNESS_ITM } else { MONEYNESS_OTM })
}

/// Classify an option's moneyness bucket at a given spot — the
/// `brutex/data/moneyness.py::classify` decision kernel over i64 paisa.
///
/// Returns the wire code (ITM=2 / ATM=3 / OTM=4); NA/UNKNOWN are decided
/// Python-side before the FFI. Raises `ValueError` on a violated
/// precondition (non-positive strike/spot, negative band) — the seam
/// degrades to the Python oracle, preserving the module's never-raises
/// contract.
#[pyfunction]
pub fn optcost_moneyness_classify(
    is_put: bool,
    strike_paisa: i64,
    spot_paisa: i64,
    band_paisa: i64,
) -> PyResult<i64> {
    moneyness_classify_checked(is_put, strike_paisa, spot_paisa, band_paisa)
        .map_err(PyValueError::new_err)
}

/// Pure core of [`optcost_atm_offset`] — round-half-toward-zero of
/// `(strike - spot) / step` in exact integer arithmetic (the proof lives in
/// the module docs; `moneyness.py:238-261` is the oracle).
pub fn atm_offset_checked(
    strike_paisa: i64,
    spot_paisa: i64,
    step_paisa: i64,
) -> Result<i64, String> {
    if strike_paisa <= 0 || spot_paisa <= 0 || step_paisa <= 0 {
        return Err(format!(
            "optcost_atm_offset: strike/spot/step must be positive paisa \
             (got {strike_paisa} / {spot_paisa} / {step_paisa}) — the None \
             short-circuit is decided Python-side (moneyness.py:255-260)"
        ));
    }
    // n / d with ROUND_HALF_DOWN (half toward zero) — moneyness.py:261.
    let n = strike_paisa - spot_paisa; // both positive → cannot overflow
    let d = step_paisa;
    let q = n / d; // Rust `/` truncates toward zero
    let r = n % d; // same sign as n
    // Tie/bump decision on 2|r| vs d in i128 (2·|r| can exceed i64::MAX).
    let twice_abs_r = 2 * i128::from(r).abs();
    if twice_abs_r > i128::from(d) {
        // More than half a step away → bump AWAY from zero (r carries n's
        // sign, so its sign picks the direction).
        Ok(if r > 0 { q + 1 } else { q - 1 })
    } else {
        // Below half, or the EXACT half tie → toward zero (keep the
        // truncated quotient) — ROUND_HALF_DOWN's tie law, which makes
        // `atm_offset == 0` exactly equivalent to the classify ATM band
        // edge (the midpoint-agreement invariant, moneyness.py:247-253).
        Ok(q)
    }
}

/// Signed ATM±N strike offset — the `brutex/data/moneyness.py::atm_offset`
/// ROUND_HALF_DOWN law over i64 paisa (half toward zero; the exact-integer
/// twin of the Decimal expression at `moneyness.py:261`).
///
/// Returns the signed offset. Raises `ValueError` on a violated
/// precondition (non-positive strike/spot/step — Python returns `None` for
/// those BEFORE the FFI); the seam degrades to the Python oracle.
#[pyfunction]
pub fn optcost_atm_offset(strike_paisa: i64, spot_paisa: i64, step_paisa: i64) -> PyResult<i64> {
    atm_offset_checked(strike_paisa, spot_paisa, step_paisa).map_err(PyValueError::new_err)
}

/// Pure core of [`optcost_strike_step`] — sort the DISTINCT positive
/// strikes, take the MINIMUM positive consecutive gap
/// (`moneyness.py:136-141`).
pub fn strike_step_checked(strikes_paisa: &[i64]) -> Result<(i64, i64), String> {
    if let Some(&bad) = strikes_paisa.iter().find(|&&s| s <= 0) {
        // Python filters non-positive values via _is_positive_finite BEFORE
        // the FFI (moneyness.py:136) — a non-positive here is a seam bug.
        return Err(format!(
            "optcost_strike_step: strikes must be positive paisa (got {bad}) — \
             validity filtering is decided Python-side"
        ));
    }
    // moneyness.py:136 — sorted({...}): distinct + ascending.
    let mut unique: Vec<i64> = strikes_paisa.to_vec();
    unique.sort_unstable();
    unique.dedup();
    // moneyness.py:137-138 — fewer than 2 distinct strikes → no step.
    if unique.len() < 2 {
        return Ok((CODE_NO_STEP, 0));
    }
    // moneyness.py:141 — min consecutive gap of a distinct ascending ladder
    // (every gap strictly positive by construction).
    let mut min_gap = i64::MAX;
    for pair in unique.windows(2) {
        let gap = pair[1] - pair[0]; // ascending distinct positives → no overflow
        if gap < min_gap {
            min_gap = gap;
        }
    }
    Ok((CODE_OK, min_gap))
}

/// Derive the strike-grid step from a ladder of strikes — the
/// `brutex/data/moneyness.py::derive_strike_step` min-positive-gap law over
/// i64 paisa.
///
/// Returns `(CODE_OK, step_paisa)` or `(CODE_NO_STEP, 0)` when fewer than
/// two distinct strikes exist (Python materializes `None`). Raises
/// `ValueError` on a non-positive strike (validity filtering is
/// Python-side); the seam degrades to the Python oracle.
///
/// FFI cost (disclosed): one `O(K)` list→Vec copy + an `O(K log K)` sort
/// per call — the SAME asymptotics as the Python sort, once per
/// `(underlying, expiry)`.
#[pyfunction]
pub fn optcost_strike_step(strikes_paisa: Vec<i64>) -> PyResult<(i64, i64)> {
    strike_step_checked(&strikes_paisa).map_err(PyValueError::new_err)
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    // ── classify: Python-oracle pins (derive_oracle.py 2026-07-18) ───────
    // Rupee cases quantized ×100 to paisa.

    #[test]
    fn classify_pins_match_python_oracle() {
        // classify(CE, 24000, 24100) = ITM
        assert_eq!(
            moneyness_classify_checked(false, 2_400_000, 2_410_000, 0),
            Ok(MONEYNESS_ITM)
        );
        // classify(CE, 24100, 24000) = OTM
        assert_eq!(
            moneyness_classify_checked(false, 2_410_000, 2_400_000, 0),
            Ok(MONEYNESS_OTM)
        );
        // classify(PE, 24000, 24100) = OTM
        assert_eq!(
            moneyness_classify_checked(true, 2_400_000, 2_410_000, 0),
            Ok(MONEYNESS_OTM)
        );
        // classify(PE, 24100, 24000) = ITM
        assert_eq!(
            moneyness_classify_checked(true, 2_410_000, 2_400_000, 0),
            Ok(MONEYNESS_ITM)
        );
        // classify(CE, 24025, 24000, band=25) = ATM (INCLUSIVE band edge)
        assert_eq!(
            moneyness_classify_checked(false, 2_402_500, 2_400_000, 2_500),
            Ok(MONEYNESS_ATM)
        );
        // classify(CE, 24026, 24000, band=25) = OTM (one rupee past the edge)
        assert_eq!(
            moneyness_classify_checked(false, 2_402_600, 2_400_000, 2_500),
            Ok(MONEYNESS_OTM)
        );
        // classify(CE, 24000, 24000) = ATM (exact equality, zero band)
        assert_eq!(
            moneyness_classify_checked(false, 2_400_000, 2_400_000, 0),
            Ok(MONEYNESS_ATM)
        );
        // One paisa past the inclusive edge flips (kills `<=` → `<`).
        assert_eq!(
            moneyness_classify_checked(false, 2_402_501, 2_400_000, 2_500),
            Ok(MONEYNESS_OTM)
        );
    }

    #[test]
    fn classify_err_on_violated_preconditions() {
        assert!(moneyness_classify_checked(false, 0, 1, 0).is_err());
        assert!(moneyness_classify_checked(false, 1, 0, 0).is_err());
        assert!(moneyness_classify_checked(false, -5, 5, 0).is_err());
        assert!(moneyness_classify_checked(false, 5, 5, -1).is_err());
    }

    // ── atm_offset: Python-Decimal-oracle pins (derive_oracle.py) ────────

    #[test]
    fn atm_offset_pins_match_python_decimal_oracle() {
        // (strike, spot, step) rupees → paisa ×100; expected from the
        // Decimal ROUND_HALF_DOWN oracle run 2026-07-18.
        let pins: &[(i64, i64, i64, i64)] = &[
            (2_405_000, 2_400_000, 5_000, 1),   // 24050/24000/50 → +1
            (2_402_500, 2_400_000, 5_000, 0),   // exact +half tie → 0
            (2_397_500, 2_400_000, 5_000, 0),   // exact -half tie → 0
            (2_407_505, 2_400_000, 5_000, 2),   // 1.501 → 2
            (2_407_495, 2_400_000, 5_000, 1),   // 1.499 → 1
            (2_392_500, 2_400_000, 5_000, -1),  // -1.5 → -1 (half toward zero)
            (2_392_495, 2_400_000, 5_000, -2),  // -1.501 → -2
            (10_001, 10_000, 1, 1),             // 100.01/100.00/0.01 → +1
            (5, 10, 5, -1),                     // 0.05/0.10/0.05 → -1
            (5_000_000, 2_400_000, 5_000, 520), // 50000/24000/50 → +520
        ];
        for &(strike, spot, step, want) in pins {
            assert_eq!(
                atm_offset_checked(strike, spot, step),
                Ok(want),
                "atm_offset({strike}, {spot}, {step})"
            );
        }
    }

    #[test]
    fn atm_offset_half_tie_is_toward_zero_both_signs() {
        // ±(k + 1/2) steps: ROUND_HALF_DOWN keeps k (kills a bump-on-tie
        // mutant); one paisa past the tie bumps.
        assert_eq!(atm_offset_checked(2_407_500, 2_400_000, 5_000), Ok(1)); // +1.5 → 1
        assert_eq!(atm_offset_checked(2_392_500, 2_400_000, 5_000), Ok(-1)); // -1.5 → -1
        assert_eq!(atm_offset_checked(2_407_501, 2_400_000, 5_000), Ok(2)); // +1.5002 → 2
        assert_eq!(atm_offset_checked(2_392_499, 2_400_000, 5_000), Ok(-2)); // -1.5002 → -2
    }

    #[test]
    fn atm_offset_i64_extremes_do_not_overflow() {
        // The 2·|r| comparison runs in i128 — a step near i64::MAX with a
        // remainder near d would overflow a 2*r i64 (this is the extreme pin
        // the lane instruction asks for; unreachable for real strikes).
        let d = i64::MAX;
        // n = i64::MAX - 1 (strike near max, spot 1): q=0, r=n, 2|r| > d → 1.
        assert_eq!(atm_offset_checked(i64::MAX, 1, d), Ok(1));
        // Exact half of an even huge step ties toward zero.
        let d2 = i64::MAX - 1; // even
        assert_eq!(atm_offset_checked(1 + d2 / 2, 1, d2), Ok(0));
        assert_eq!(atm_offset_checked(2 + d2 / 2, 1, d2), Ok(1));
    }

    #[test]
    fn atm_offset_err_on_violated_preconditions() {
        assert!(atm_offset_checked(0, 1, 1).is_err());
        assert!(atm_offset_checked(1, 0, 1).is_err());
        assert!(atm_offset_checked(1, 1, 0).is_err());
        assert!(atm_offset_checked(1, 1, -5).is_err());
    }

    /// The midpoint-agreement invariant (moneyness.py:247-253):
    /// `atm_offset == 0` ⇔ classify(band = step/2) == ATM, for every
    /// on-grid spot at `strike ± step/2` — the reason ROUND_HALF_DOWN was
    /// chosen. Checked here for even steps (an exact half-step midpoint
    /// exists only when `step` is even in paisa).
    #[test]
    fn midpoint_agreement_invariant() {
        for step in [2i64, 10, 5_000, 10_000] {
            let strike = 2_400_000i64;
            for spot in [strike - step / 2, strike + step / 2] {
                let off = atm_offset_checked(strike, spot, step)
                    .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
                let bucket = moneyness_classify_checked(false, strike, spot, step / 2)
                    .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
                assert_eq!(off, 0, "midpoint offset (step {step}, spot {spot})");
                assert_eq!(bucket, MONEYNESS_ATM, "midpoint bucket (step {step})");
            }
        }
    }

    // ── strike_step: Python-oracle pins ──────────────────────────────────

    #[test]
    fn strike_step_pins_match_python_oracle() {
        // [24000, 24050, 24100] → 50 rupees = 5000 paisa
        assert_eq!(
            strike_step_checked(&[2_400_000, 2_405_000, 2_410_000]),
            Ok((CODE_OK, 5_000))
        );
        // [24000, 24100, 24200, 24250] → 50 (a wider gap is a missing
        // strike, never a finer grid)
        assert_eq!(
            strike_step_checked(&[2_400_000, 2_410_000, 2_420_000, 2_425_000]),
            Ok((CODE_OK, 5_000))
        );
        // Single strike → no step
        assert_eq!(strike_step_checked(&[2_400_000]), Ok((CODE_NO_STEP, 0)));
        // Duplicates collapse → still no step
        assert_eq!(
            strike_step_checked(&[2_400_000, 2_400_000]),
            Ok((CODE_NO_STEP, 0))
        );
        // [100.05, 100.10, 100.20] → 0.05 rupees = 5 paisa
        assert_eq!(
            strike_step_checked(&[10_005, 10_010, 10_020]),
            Ok((CODE_OK, 5))
        );
        // Empty ladder → no step
        assert_eq!(strike_step_checked(&[]), Ok((CODE_NO_STEP, 0)));
        // Unsorted input sorts first (Python's sorted({...}))
        assert_eq!(
            strike_step_checked(&[2_410_000, 2_400_000, 2_405_000]),
            Ok((CODE_OK, 5_000))
        );
    }

    #[test]
    fn strike_step_err_on_non_positive() {
        assert!(strike_step_checked(&[0, 5]).is_err());
        assert!(strike_step_checked(&[-5, 5]).is_err());
    }

    // ── properties ────────────────────────────────────────────────────

    proptest! {
        /// atm_offset differential vs an i128 exact-rational reference —
        /// an independently written nearest-multiple SEARCH (minimize
        /// |n - k·d| over candidate k, ties preferring the smaller |k|,
        /// i.e. toward zero), plus determinism.
        #[test]
        fn atm_offset_matches_exact_rational_reference(
            strike in 1i64..=i64::MAX,
            spot in 1i64..=i64::MAX,
            step in 1i64..=i64::MAX,
        ) {
            let got = atm_offset_checked(strike, spot, step)
                .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
            let n = i128::from(strike) - i128::from(spot);
            let d = i128::from(step);
            // Search a candidate window around the floor quotient; the true
            // nearest multiple is always within it. Distances compared on
            // 2·|n - k·d| in exact i128 — no rounding anywhere.
            let q0 = n.div_euclid(d);
            let mut best = q0 - 2;
            for cand in (q0 - 1)..=(q0 + 2) {
                let dist = (n - cand * d).abs();
                let best_dist = (n - best * d).abs();
                if dist < best_dist || (dist == best_dist && cand.abs() < best.abs()) {
                    best = cand;
                }
            }
            prop_assert_eq!(i128::from(got), best);
            let again = atm_offset_checked(strike, spot, step)
                .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
            prop_assert_eq!(got, again);
        }

        /// classify is total over valid inputs, agrees with a direct
        /// re-derivation, and respects the inclusive ATM band edge.
        #[test]
        fn classify_matches_direct_rederivation(
            strike in 1i64..=1_000_000_000,
            spot in 1i64..=1_000_000_000,
            band in 0i64..=1_000_000,
            is_put in proptest::bool::ANY,
        ) {
            let got = moneyness_classify_checked(is_put, strike, spot, band)
                .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
            let diff = spot - strike;
            let want = if diff.abs() <= band {
                MONEYNESS_ATM
            } else if (!is_put && diff > 0) || (is_put && diff < 0) {
                MONEYNESS_ITM
            } else {
                MONEYNESS_OTM
            };
            prop_assert_eq!(got, want);
        }

        /// strike_step: the returned gap divides no smaller observed gap
        /// (it IS the min), every consecutive distinct gap >= it, and the
        /// result is permutation-invariant.
        #[test]
        fn strike_step_is_min_gap_and_permutation_invariant(
            mut strikes in proptest::collection::vec(1i64..=10_000_000, 0..12),
        ) {
            let a = strike_step_checked(&strikes)
                .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
            strikes.reverse();
            let b = strike_step_checked(&strikes)
                .unwrap_or_else(|e| panic!("unexpected Err: {e}"));
            prop_assert_eq!(a, b);
            let mut unique = strikes.clone();
            unique.sort_unstable();
            unique.dedup();
            if unique.len() < 2 {
                prop_assert_eq!(a, (CODE_NO_STEP, 0));
            } else {
                let min_gap = unique.windows(2).map(|w| w[1] - w[0]).min()
                    .unwrap_or_else(|| unreachable!());
                prop_assert_eq!(a, (CODE_OK, min_gap));
            }
        }
    }
}
