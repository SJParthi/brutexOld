//! Put-Call Ratio (PCR) calculation from live OI data.
//!
//! PCR = Total Put OI / Total Call OI (per expiry or aggregate).
//!
//! # Interpretation
//! - PCR > 1.2 → Bearish sentiment (more puts than calls)
//! - PCR 0.8–1.2 → Neutral
//! - PCR < 0.8 → Bullish sentiment (more calls than puts)
//!
//! # Performance
//! O(1) per computation (simple division).

/// Computes Put-Call Ratio from total put and call open interest.
///
/// Returns `None` if call OI is zero (division by zero).
///
/// # Arguments
/// * `total_put_oi` — Sum of OI across all put strikes for an expiry.
/// * `total_call_oi` — Sum of OI across all call strikes for an expiry.
pub fn compute_pcr(total_put_oi: u64, total_call_oi: u64) -> Option<f64> {
    if total_call_oi == 0 {
        return None;
    }
    Some(total_put_oi as f64 / total_call_oi as f64)
}

/// Classifies PCR into sentiment.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PcrSentiment {
    /// PCR > 1.2 — bearish (put-heavy).
    Bearish,
    /// PCR 0.8–1.2 — neutral.
    Neutral,
    /// PCR < 0.8 — bullish (call-heavy).
    Bullish,
}

/// PCR thresholds (configurable in future, hardcoded for now).
const PCR_BEARISH_THRESHOLD: f64 = 1.2;
const PCR_BULLISH_THRESHOLD: f64 = 0.8;

/// Classifies a PCR value into sentiment.
pub fn classify_pcr(pcr: f64) -> PcrSentiment {
    if pcr > PCR_BEARISH_THRESHOLD {
        PcrSentiment::Bearish
    } else if pcr < PCR_BULLISH_THRESHOLD {
        PcrSentiment::Bullish
    } else {
        PcrSentiment::Neutral
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_pcr_basic() {
        let pcr = compute_pcr(72_90_595, 28_12_485).unwrap();
        assert!(pcr > 2.0, "High put OI → high PCR: {pcr}");
    }

    #[test]
    fn test_pcr_equal_oi() {
        let pcr = compute_pcr(100_000, 100_000).unwrap();
        assert!((pcr - 1.0).abs() < f64::EPSILON, "Equal OI → PCR=1.0");
    }

    #[test]
    fn test_pcr_zero_call_oi() {
        assert!(compute_pcr(100_000, 0).is_none(), "Zero call OI → None");
    }

    #[test]
    fn test_pcr_zero_both() {
        assert!(compute_pcr(0, 0).is_none(), "Zero both → None");
    }

    #[test]
    fn test_pcr_zero_put_oi() {
        let pcr = compute_pcr(0, 100_000).unwrap();
        assert!((pcr - 0.0).abs() < f64::EPSILON, "Zero put OI → PCR=0.0");
    }

    #[test]
    fn test_pcr_nifty_example() {
        // From screenshot: PCR = 0.78 (approximate)
        // Typical NIFTY weekly: ~40L puts, ~50L calls
        let pcr = compute_pcr(40_00_000, 51_28_205).unwrap();
        assert!(pcr > 0.7 && pcr < 0.9, "NIFTY-like PCR: {pcr}");
    }

    #[test]
    fn test_classify_bearish() {
        assert_eq!(classify_pcr(1.5), PcrSentiment::Bearish);
        assert_eq!(classify_pcr(1.21), PcrSentiment::Bearish);
    }

    #[test]
    fn test_classify_neutral() {
        assert_eq!(classify_pcr(1.0), PcrSentiment::Neutral);
        assert_eq!(classify_pcr(0.8), PcrSentiment::Neutral);
        assert_eq!(classify_pcr(1.2), PcrSentiment::Neutral);
    }

    #[test]
    fn test_classify_bullish() {
        assert_eq!(classify_pcr(0.5), PcrSentiment::Bullish);
        assert_eq!(classify_pcr(0.79), PcrSentiment::Bullish);
    }

    #[test]
    fn test_pcr_sentiment_eq() {
        assert_eq!(PcrSentiment::Bearish, PcrSentiment::Bearish);
        assert_ne!(PcrSentiment::Bearish, PcrSentiment::Bullish);
    }
}
