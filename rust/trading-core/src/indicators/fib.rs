//! Static Daily Fibonacci levels — a bit-for-bit port of
//! `brutex/indicators/fib_levels.py::compute_fib_levels`.
//!
//! THE bit-identity hot spot: every retracement / extension multiplies the
//! integer `range` by an IEEE-754 double literal (`0.236`, `0.272`, …) and
//! rounds half-to-even. The Rust literals parse to the *same* IEEE-754 bit
//! patterns as Python's, and the operation order (`pdh - 0.236*range`) is
//! mirrored token-for-token, so the rounded results are identical.
//!
//! ## The BULLISH retracement ladder (`retrace_bull_*`)
//!
//! The bullish ladder measures UP from PDL (`pdl + ratio * range`) — it is
//! NOT a relabeling of the bearish PDH-anchored ladder (`pdh - ratio *
//! range`): Fibonacci ratios are asymmetric (`0.236 + 0.786 = 1.022 ≠
//! 1.000`), so bullish 23%/78% sit at DIFFERENT prices than bearish 78%/23%
//! (the 38/62 pair coincides only because `0.382 + 0.618 = 1.000` in IEEE —
//! an observation, never relied on; DEC-FIB-BULLISH-RETRACE-001). Chart
//! basis: ES Trading Academy AmiBroker, NIFTY 07-May-2026 (bullish gap-up).
//! The 0%/100% bullish anchors are `pdl`/`pdh`, the bullish midpoint IS the
//! shared `retrace_50_paisa` (exact integer-sum midpoint — no separate
//! `bull_50`), and the extensions above PDH are the existing `ext_up_*`. The
//! 52→60-condition EVALUATOR does NOT read the four `retrace_bull_*` fields.

use crate::indicators::round_half_even;

/// The static daily Fibonacci anchor levels, all int64 paisa. (The evaluator
/// uses the 7 retracements + the 127/162/200/262 up+down extensions; the 424
/// extensions are computed for parity with the Python struct but unused.)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FibLevels {
    pub pdh_paisa: i64,
    pub pdl_paisa: i64,
    pub range_paisa: i64,
    pub retrace_level_at_pdh_paisa: i64,
    pub retrace_23_paisa: i64,
    pub retrace_38_paisa: i64,
    pub retrace_50_paisa: i64,
    pub retrace_62_paisa: i64,
    pub retrace_78_paisa: i64,
    pub retrace_level_at_pdl_paisa: i64,
    /// Bullish 23% retracement: `round_half_even(pdl + 0.236 * range)`.
    pub retrace_bull_23_paisa: i64,
    /// Bullish 38% retracement: `round_half_even(pdl + 0.382 * range)`.
    pub retrace_bull_38_paisa: i64,
    /// Bullish 62% retracement: `round_half_even(pdl + 0.618 * range)`.
    pub retrace_bull_62_paisa: i64,
    /// Bullish 78% retracement: `round_half_even(pdl + 0.786 * range)`.
    pub retrace_bull_78_paisa: i64,
    pub ext_up_127_paisa: i64,
    pub ext_up_162_paisa: i64,
    pub ext_up_200_paisa: i64,
    pub ext_up_262_paisa: i64,
    pub ext_up_424_paisa: i64,
    pub ext_down_127_paisa: i64,
    pub ext_down_162_paisa: i64,
    pub ext_down_200_paisa: i64,
    pub ext_down_262_paisa: i64,
    pub ext_down_424_paisa: i64,
}

/// Compute the static daily Fibonacci levels from the previous day's high/low.
///
/// This kernel IS the single source of the Fibonacci math —
/// `fib_levels.py::compute_fib_levels` validates its inputs and then DELEGATES
/// here (DEC-SHARED-CORE-001). `range` is the integer `pdh - pdl`, taken
/// to `f64` once; each level is `round_half_even(anchor ± ratio * range)` with
/// the ratio applied before the add/sub (matching Python operator precedence).
/// `*_200` uses `± range` directly (`1.000 * range == range` exactly in IEEE).
#[must_use]
pub fn compute_fib_levels(pdh_paisa: i64, pdl_paisa: i64) -> FibLevels {
    // `assert!` (always-on) so corrupt prev-day OHLC fails LOUD in release too,
    // matching the `ValueError` guard in `fib_levels.py::compute_fib_levels`.
    // The PyO3 boundary pre-validates and raises a clean `ValueError` instead
    // (`python.rs::py_compute_fib_levels`); that boundary is behind the
    // optional `python` feature, so a native rlib consumer (TickVault) reaches
    // this `assert!` directly.
    assert!(pdh_paisa >= pdl_paisa, "PDH must be >= PDL");

    let range_paisa = pdh_paisa - pdl_paisa;
    let r = range_paisa as f64;
    let pdh_f = pdh_paisa as f64;
    let pdl_f = pdl_paisa as f64;

    FibLevels {
        pdh_paisa,
        pdl_paisa,
        range_paisa,
        retrace_level_at_pdh_paisa: pdh_paisa,
        retrace_23_paisa: round_half_even(pdh_f - 0.236 * r),
        retrace_38_paisa: round_half_even(pdh_f - 0.382 * r),
        retrace_50_paisa: round_half_even((pdh_paisa + pdl_paisa) as f64 / 2.0),
        retrace_62_paisa: round_half_even(pdh_f - 0.618 * r),
        retrace_78_paisa: round_half_even(pdh_f - 0.786 * r),
        retrace_level_at_pdl_paisa: pdl_paisa,
        retrace_bull_23_paisa: round_half_even(pdl_f + 0.236 * r),
        retrace_bull_38_paisa: round_half_even(pdl_f + 0.382 * r),
        retrace_bull_62_paisa: round_half_even(pdl_f + 0.618 * r),
        retrace_bull_78_paisa: round_half_even(pdl_f + 0.786 * r),
        ext_up_127_paisa: round_half_even(pdh_f + 0.272 * r),
        ext_up_162_paisa: round_half_even(pdh_f + 0.618 * r),
        ext_up_200_paisa: round_half_even(pdh_f + r),
        ext_up_262_paisa: round_half_even(pdh_f + 1.618 * r),
        ext_up_424_paisa: round_half_even(pdh_f + 3.236 * r),
        ext_down_127_paisa: round_half_even(pdl_f - 0.272 * r),
        ext_down_162_paisa: round_half_even(pdl_f - 0.618 * r),
        ext_down_200_paisa: round_half_even(pdl_f - r),
        ext_down_262_paisa: round_half_even(pdl_f - 1.618 * r),
        ext_down_424_paisa: round_half_even(pdl_f - 3.236 * r),
    }
}

#[cfg(test)]
mod tests {
    use super::compute_fib_levels;

    /// EXACT-VALUE pins for the four `retrace_bull_*` ratio literals, mirroring
    /// the frozen Python goldens (`tests/unit/test_fib_levels.py::
    /// TestBullishRetracementLadder::test_frozen_kernel_goldens`) so the crate
    /// is self-verifying for its NATIVE consumer (TickVault,
    /// DEC-SHARED-CORE-001) and `cargo-mutants` can kill a wrong-ratio mutant
    /// (e.g. `0.382 -> 0.352`) that the ordering/inequality properties alone
    /// cannot — the evaluator's 400-case golden vectors deliberately never read
    /// the bull fields (DEC-FIB-BULLISH-RETRACE-001).
    ///
    /// Anchor: NIFTY 07-May-2026 (bullish gap-up), PDH=24,313.00 /
    /// PDL=24,056.00 rupees -> range 25,700 paisa. bull_78 is chart-verified;
    /// bull_23/38/62 are formula-derived, kernel-frozen.
    #[test]
    fn bullish_retracement_exact_values_nifty_anchor() {
        let levels = compute_fib_levels(2_431_300, 2_405_600);
        // pdl + 0.236 * range = 2,405,600 + 6,065.2 -> 2,411,665
        assert_eq!(levels.retrace_bull_23_paisa, 2_411_665);
        // pdl + 0.382 * range = 2,405,600 + 9,817.4 -> 2,415,417
        assert_eq!(levels.retrace_bull_38_paisa, 2_415_417);
        // pdl + 0.618 * range = 2,405,600 + 15,882.6 -> 2,421,483
        assert_eq!(levels.retrace_bull_62_paisa, 2_421_483);
        // pdl + 0.786 * range = 2,405,600 + 20,200.2 -> 2,425,800 (chart-verified)
        assert_eq!(levels.retrace_bull_78_paisa, 2_425_800);
    }

    /// The bullish rungs are NOT the reversed bearish rungs: on the BANKNIFTY
    /// 30-Apr-2026 anchor (range 85,200 paisa) the asymmetry gap is exactly
    /// `0.022 * range = 1,874.4 -> 1,874` paisa on BOTH pairs — an exact-value
    /// pin (not a mere inequality), the Rust twin of the Python
    /// `test_asymmetry_teeth_on_second_anchor`.
    #[test]
    fn bullish_bearish_asymmetry_gap_exact_banknifty_anchor() {
        let levels = compute_fib_levels(5_650_200, 5_565_000);
        assert_eq!(
            levels.retrace_bull_78_paisa - levels.retrace_23_paisa,
            1_874
        );
        assert_eq!(
            levels.retrace_bull_23_paisa - levels.retrace_78_paisa,
            1_874
        );
    }
}
