//! Options Greeks engine — Black-Scholes pricing, IV solver, Greeks, PCR, Buildup.
//!
//! All calculations are O(1) per option contract (pure math, zero allocation).
//! Designed for real-time computation on every tick AND historical backtesting.
//!
//! # Modules
//! - `aggregator` — Candle-aligned Greeks aggregation from live tick data
//! - `black_scholes` — Core pricing: BS price, Jaeckel IV solver, all 13 Greeks
//! - `calibration` — Parameter calibration to match Dhan's exact Greeks
//! - `historical_rates` — RBI repo rate lookup table (2020-2026) for backtesting
//! - `pcr` — Put-Call Ratio from live OI data
//! - `buildup` — Option activity classification (Long/Short Buildup/Unwinding)

pub mod aggregator;
pub mod black_scholes;
pub mod buildup;
pub mod calibration;
pub mod historical_rates;
pub mod inline_computer;
pub mod pcr;

// ---------------------------------------------------------------------------
// Neumaier compensated summation for portfolio Greeks aggregation
// ---------------------------------------------------------------------------

/// Neumaier compensated summation — prevents floating-point cancellation
/// when summing many Greeks values (e.g., portfolio-level delta/gamma).
///
/// Standard f64 summation loses precision when adding many values with
/// alternating signs or vastly different magnitudes. Neumaier's algorithm
/// tracks the lost low-order bits in a compensation variable.
///
/// # Example
/// Summing 1e16 + 1.0 + (-1e16) gives 0.0 with naive sum, 1.0 with Neumaier.
pub fn neumaier_sum(values: &[f64]) -> f64 {
    let mut sum = 0.0_f64;
    let mut compensation = 0.0_f64;

    for &v in values {
        let temp = sum + v;
        if sum.abs() >= v.abs() {
            // sum is bigger: low-order digits of v are lost.
            compensation += (sum - temp) + v;
        } else {
            // v is bigger: low-order digits of sum are lost.
            compensation += (v - temp) + sum;
        }
        sum = temp;
    }

    sum + compensation
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_neumaier_cancellation_resistance() {
        // Classic cancellation test: 1e16 + 1.0 + (-1e16) = 1.0 (not 0.0).
        let values = [1e16, 1.0, -1e16];
        let naive: f64 = values.iter().sum();
        let compensated = neumaier_sum(&values);
        // Naive sum gives 0.0 due to catastrophic cancellation.
        assert!((naive - 0.0).abs() < f64::EPSILON, "Naive: {naive}");
        // Neumaier gives the correct answer: 1.0.
        assert!(
            (compensated - 1.0).abs() < f64::EPSILON,
            "Neumaier: {compensated}"
        );
    }

    #[test]
    fn test_neumaier_empty() {
        assert!((neumaier_sum(&[]) - 0.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_neumaier_single() {
        assert!((neumaier_sum(&[42.0]) - 42.0).abs() < f64::EPSILON);
    }

    #[test]
    fn test_neumaier_simple_sum() {
        let values = [1.0, 2.0, 3.0, 4.0, 5.0];
        assert!((neumaier_sum(&values) - 15.0).abs() < f64::EPSILON);
    }
}
