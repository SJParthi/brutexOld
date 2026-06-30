//! Evaluate-time indicators the condition vocabulary consumes: Standard Pivot
//! Points (now incl. R5/S5), Static Daily Fibonacci levels, the session-
//! anchored intraday futures VWAP state (PR-2 — DEC-NIFTY-INDICATORS-001), and
//! the BOS/CHoCH market-structure state (DEC-NIFTY-BOS-CHOCH-001). (EMA is NOT
//! here — the evaluator consumes precomputed `ema20`/`ema200` paisa from the
//! snapshot, so EMA recurrence is a later phase. VWAP + market structure are
//! STATE objects whose resolved values are fed into the snapshot, mirroring EMA.)

pub mod fib;
pub mod market_structure;
pub mod pivots;
pub mod vwap;

/// Round-half-to-even (banker's rounding) to `i64` — the exact match for
/// Python's built-in `round()` used by `_round_half_even` in
/// `brutex/indicators/{pivots,fib_levels}.py`.
///
/// Rust's `f64::round()` rounds half AWAY from zero, which would diverge on a
/// genuine `.5` tie (e.g. the fixture's `retrace_50 = 3346656.5`). `round_ties_even`
/// (stable since Rust 1.77) is Python's IEEE-754 default mode.
#[inline]
#[must_use]
pub fn round_half_even(x: f64) -> i64 {
    x.round_ties_even() as i64
}

#[cfg(test)]
mod tests {
    use super::round_half_even;

    #[test]
    fn matches_python_bankers_rounding() {
        // Round-half-to-even: ties go to the nearest EVEN integer.
        assert_eq!(round_half_even(2.5), 2);
        assert_eq!(round_half_even(0.5), 0);
        assert_eq!(round_half_even(1.5), 2);
        assert_eq!(round_half_even(3.5), 4);
        assert_eq!(round_half_even(-0.5), 0);
        assert_eq!(round_half_even(-1.5), -2);
        assert_eq!(round_half_even(-2.5), -2);
        // Non-ties round normally.
        assert_eq!(round_half_even(2.4), 2);
        assert_eq!(round_half_even(2.6), 3);
        // The live fixture half-tie (retrace_50): 3346656.5 -> even -> 3346656.
        assert_eq!(round_half_even(3_346_656.5), 3_346_656);
    }

    /// Document the `f64 as i64` cast's saturating boundary behaviour. This path
    /// is UNREACHABLE for real prices (the inner integer arithmetic overflows and
    /// panics far before a non-finite `f64` could reach here), but Rust's
    /// float→int cast has been *saturating* (never UB, never a panic) since 1.45,
    /// so the worst case is a defined clamp — proven here, not relied on by faith.
    #[test]
    fn non_finite_and_extreme_inputs_saturate_not_panic() {
        // NaN maps to 0.
        assert_eq!(round_half_even(f64::NAN), 0);
        // ±Infinity saturates to the i64 extremes.
        assert_eq!(round_half_even(f64::INFINITY), i64::MAX);
        assert_eq!(round_half_even(f64::NEG_INFINITY), i64::MIN);
        // A finite magnitude far above i64::MAX also saturates (no wrap, no panic).
        assert_eq!(round_half_even(1e30), i64::MAX);
        assert_eq!(round_half_even(-1e30), i64::MIN);
        // Finite mid-range still rounds normally (the saturation is edge-only).
        assert_eq!(round_half_even(123.5), 124);
    }
}
