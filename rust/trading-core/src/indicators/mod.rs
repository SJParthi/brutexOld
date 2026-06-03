//! Evaluate-time indicators the 52-condition vocabulary consumes: Standard
//! Pivot Points and Static Daily Fibonacci levels. (EMA is NOT here — the
//! evaluator consumes precomputed `ema20`/`ema200` paisa from the snapshot,
//! so EMA recurrence is a later phase.)

pub mod fib;
pub mod pivots;

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
}
