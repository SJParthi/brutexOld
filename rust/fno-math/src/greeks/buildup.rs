//! Option activity (buildup) classification from OI and price changes.
//!
//! Uses the standard 4-quadrant model:
//!
//! ```text
//!                  OI Increasing        OI Decreasing
//! Price Rising     Long Buildup         Short Covering
//! Price Falling    Short Buildup        Long Unwinding
//! ```
//!
//! # Data Sources
//! - Current OI: from WebSocket Full packet (live)
//! - Previous OI: from PrevClose packet or option chain API
//! - Current LTP: from WebSocket tick (live)
//! - Previous close: from PrevClose packet
//!
//! # Performance
//! O(1) — two comparisons.

/// Option activity classification.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BuildupType {
    /// OI ↑ + Price ↑ — new longs being created (bullish for CE, bearish for PE).
    LongBuildup,
    /// OI ↑ + Price ↓ — new shorts being created (bearish for CE, bullish for PE).
    ShortBuildup,
    /// OI ↓ + Price ↓ — existing longs closing out.
    LongUnwinding,
    /// OI ↓ + Price ↑ — existing shorts closing out.
    ShortCovering,
}

impl BuildupType {
    /// Returns a human-readable label matching Dhan's display.
    pub fn label(self) -> &'static str {
        match self {
            Self::LongBuildup => "Long Buildup",
            Self::ShortBuildup => "Short Buildup",
            Self::LongUnwinding => "Long Unwinding",
            Self::ShortCovering => "Short Covering",
        }
    }
}

/// Classifies option activity based on OI and price changes.
///
/// # Arguments
/// * `current_oi` — Current open interest.
/// * `previous_oi` — Previous day/session open interest.
/// * `current_price` — Current LTP.
/// * `previous_price` — Previous close price.
///
/// # Returns
/// `None` if OI or price is unchanged (no clear signal).
pub fn classify_buildup(
    current_oi: u32,
    previous_oi: u32,
    current_price: f64,
    previous_price: f64,
) -> Option<BuildupType> {
    let oi_increasing = current_oi > previous_oi;
    let oi_decreasing = current_oi < previous_oi;
    let price_rising = current_price > previous_price;
    let price_falling = current_price < previous_price;

    match (oi_increasing, oi_decreasing, price_rising, price_falling) {
        (true, _, true, _) => Some(BuildupType::LongBuildup),
        (true, _, _, true) => Some(BuildupType::ShortBuildup),
        (_, true, _, true) => Some(BuildupType::LongUnwinding),
        (_, true, true, _) => Some(BuildupType::ShortCovering),
        _ => None, // No change in OI or price — indeterminate.
    }
}

/// Computes OI change percentage.
///
/// Returns `None` if previous_oi is zero.
pub fn oi_change_pct(current_oi: u32, previous_oi: u32) -> Option<f64> {
    if previous_oi == 0 {
        return None;
    }
    Some(((current_oi as f64 - previous_oi as f64) / previous_oi as f64) * 100.0)
}

/// Computes LTP change percentage.
///
/// Returns `None` if previous_price is zero or non-positive.
pub fn ltp_change_pct(current_price: f64, previous_price: f64) -> Option<f64> {
    if previous_price <= 0.0 {
        return None;
    }
    Some(((current_price - previous_price) / previous_price) * 100.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    // --- Buildup classification (4 quadrants) ---

    #[test]
    fn test_long_buildup() {
        // OI up + price up → longs accumulating
        let result = classify_buildup(100_000, 80_000, 500.0, 450.0);
        assert_eq!(result, Some(BuildupType::LongBuildup));
    }

    #[test]
    fn test_short_buildup() {
        // OI up + price down → shorts accumulating
        let result = classify_buildup(100_000, 80_000, 400.0, 450.0);
        assert_eq!(result, Some(BuildupType::ShortBuildup));
    }

    #[test]
    fn test_long_unwinding() {
        // OI down + price down → longs exiting
        let result = classify_buildup(60_000, 80_000, 400.0, 450.0);
        assert_eq!(result, Some(BuildupType::LongUnwinding));
    }

    #[test]
    fn test_short_covering() {
        // OI down + price up → shorts exiting
        let result = classify_buildup(60_000, 80_000, 500.0, 450.0);
        assert_eq!(result, Some(BuildupType::ShortCovering));
    }

    #[test]
    fn test_no_change_oi() {
        // Same OI → indeterminate
        let result = classify_buildup(80_000, 80_000, 500.0, 450.0);
        assert_eq!(result, None);
    }

    #[test]
    fn test_no_change_price() {
        // Same price → indeterminate
        let result = classify_buildup(100_000, 80_000, 450.0, 450.0);
        assert_eq!(result, None);
    }

    #[test]
    fn test_no_change_both() {
        let result = classify_buildup(80_000, 80_000, 450.0, 450.0);
        assert_eq!(result, None);
    }

    // --- Real-world examples from screenshot ---

    #[test]
    fn test_nifty_23000_ce_short_covering() {
        // From screenshot: CE 23000 → LTP 296.50 (was higher prev), OI down
        // OI: 72,90,595 → current OI lower → Short Covering if price up
        let result = classify_buildup(72_90_595, 80_00_000, 296.50, 280.0);
        assert_eq!(result, Some(BuildupType::ShortCovering));
    }

    #[test]
    fn test_nifty_23000_pe_short_buildup() {
        // PE 23000 → OI increasing, price decreasing → Short Buildup
        let result = classify_buildup(9_62_065, 8_00_000, 173.75, 250.0);
        assert_eq!(result, Some(BuildupType::ShortBuildup));
    }

    // --- Label tests ---

    #[test]
    fn test_buildup_labels() {
        assert_eq!(BuildupType::LongBuildup.label(), "Long Buildup");
        assert_eq!(BuildupType::ShortBuildup.label(), "Short Buildup");
        assert_eq!(BuildupType::LongUnwinding.label(), "Long Unwinding");
        assert_eq!(BuildupType::ShortCovering.label(), "Short Covering");
    }

    // --- OI change % ---

    #[test]
    fn test_oi_change_pct_increase() {
        let pct = oi_change_pct(120_000, 100_000).unwrap();
        assert!((pct - 20.0).abs() < 0.01, "20% increase: {pct}");
    }

    #[test]
    fn test_oi_change_pct_decrease() {
        let pct = oi_change_pct(80_000, 100_000).unwrap();
        assert!((pct - (-20.0)).abs() < 0.01, "20% decrease: {pct}");
    }

    #[test]
    fn test_oi_change_pct_zero_previous() {
        assert!(oi_change_pct(100_000, 0).is_none());
    }

    // --- LTP change % ---

    #[test]
    fn test_ltp_change_pct_positive() {
        let pct = ltp_change_pct(550.0, 500.0).unwrap();
        assert!((pct - 10.0).abs() < 0.01, "10% increase: {pct}");
    }

    #[test]
    fn test_ltp_change_pct_negative() {
        let pct = ltp_change_pct(450.0, 500.0).unwrap();
        assert!((pct - (-10.0)).abs() < 0.01, "10% decrease: {pct}");
    }

    #[test]
    fn test_ltp_change_pct_zero_previous() {
        assert!(ltp_change_pct(500.0, 0.0).is_none());
    }

    // --- Trait tests ---

    #[test]
    fn test_buildup_type_eq() {
        assert_eq!(BuildupType::LongBuildup, BuildupType::LongBuildup);
        assert_ne!(BuildupType::LongBuildup, BuildupType::ShortBuildup);
    }

    #[test]
    fn test_buildup_type_copy() {
        let b = BuildupType::LongBuildup;
        let b2 = b; // Copy
        assert_eq!(b, b2);
    }

    #[test]
    fn test_buildup_type_debug() {
        let debug = format!("{:?}", BuildupType::ShortCovering);
        assert!(debug.contains("ShortCovering"));
    }
}
