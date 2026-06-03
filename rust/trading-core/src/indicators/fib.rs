//! Static Daily Fibonacci levels — a bit-for-bit port of
//! `brutex/indicators/fib_levels.py::compute_fib_levels`.
//!
//! THE bit-identity hot spot: every retracement / extension multiplies the
//! integer `range` by an IEEE-754 double literal (`0.236`, `0.272`, …) and
//! rounds half-to-even. The Rust literals parse to the *same* IEEE-754 bit
//! patterns as Python's, and the operation order (`pdh - 0.236*range`) is
//! mirrored token-for-token, so the rounded results are identical.

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
/// Mirrors `fib_levels.py:259-282`. `range` is the integer `pdh - pdl`, taken
/// to `f64` once; each level is `round_half_even(anchor ± ratio * range)` with
/// the ratio applied before the add/sub (matching Python operator precedence).
/// `*_200` uses `± range` directly (`1.000 * range == range` exactly in IEEE).
#[must_use]
pub fn compute_fib_levels(pdh_paisa: i64, pdl_paisa: i64) -> FibLevels {
    debug_assert!(pdh_paisa >= pdl_paisa, "PDH must be >= PDL");

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
