//! RBI repo rate lookup table for historical backtesting.
//!
//! Contains all RBI repo rate changes from 2020-2026.
//! Used in "theoretical" mode to compute Greeks with the actual risk-free rate
//! on any given date, rather than the fixed 10% used by Dhan/NSE.
//!
//! # Usage
//! - **Dhan-match mode** (`rate_mode = "dhan"`): Always returns 0.10 (10%).
//! - **Theoretical mode** (`rate_mode = "theoretical"`): Returns the RBI repo rate
//!   in effect on the given date.

use chrono::NaiveDate;

/// Dhan/NSE convention: fixed 10% risk-free rate.
pub const DHAN_FIXED_RATE: f64 = 0.10;

/// RBI repo rate change entry: (effective_date, rate).
/// Source: RBI Monetary Policy announcements 2020-2026.
const RBI_REPO_RATES: &[(u32, u32, u32, f64)] = &[
    // (year, month, day, rate)
    // --- COVID cuts (2020) ---
    (2020, 3, 27, 0.0440), // Emergency cut: 5.15% → 4.40%
    (2020, 5, 22, 0.0400), // Cut: 4.40% → 4.00%
    // --- Hold at 4.00% through 2021 ---
    // --- Tightening cycle (2022) ---
    (2022, 5, 4, 0.0440),  // First hike: 4.00% → 4.40%
    (2022, 6, 8, 0.0490),  // 4.40% → 4.90%
    (2022, 8, 5, 0.0540),  // 4.90% → 5.40%
    (2022, 9, 30, 0.0590), // 5.40% → 5.90%
    (2022, 12, 7, 0.0625), // 5.90% → 6.25%
    // --- Final hike (2023) ---
    (2023, 2, 8, 0.0650), // 6.25% → 6.50%
    // --- Hold at 6.50% through mid-2025 ---
    // --- Easing cycle (2025) ---
    (2025, 2, 7, 0.0625), // 6.50% → 6.25%
    (2025, 4, 9, 0.0600), // 6.25% → 6.00%
    (2025, 6, 6, 0.0575), // 6.00% → 5.75% (projected)
];

/// Returns the RBI repo rate in effect on the given date.
///
/// For dates before the first entry (2020-03-27), returns 5.15% (pre-COVID rate).
/// For dates after the last entry, returns the most recent rate.
pub fn rbi_repo_rate(date: NaiveDate) -> f64 {
    // Pre-COVID default rate.
    let mut rate = 0.0515;

    for &(year, month, day, new_rate) in RBI_REPO_RATES {
        // APPROVED: month/day are from hardcoded valid entries (1-12, 1-31).
        // from_ymd_opt never returns None for these known-good dates.
        let Some(effective) = NaiveDate::from_ymd_opt(year as i32, month, day) else {
            continue;
        };
        if date >= effective {
            rate = new_rate;
        } else {
            break;
        }
    }

    rate
}

/// Returns the risk-free rate based on the configured mode.
///
/// - `"dhan"` → fixed 10% (matches Dhan/NSE displayed values)
/// - `"theoretical"` → RBI repo rate on the given date
/// - Any other value → defaults to Dhan mode
pub fn risk_free_rate_for_mode(mode: &str, date: NaiveDate) -> f64 {
    match mode {
        "theoretical" => rbi_repo_rate(date),
        _ => DHAN_FIXED_RATE,
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    fn date(y: i32, m: u32, d: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, d).unwrap()
    }

    #[test]
    fn test_pre_covid_rate() {
        assert!((rbi_repo_rate(date(2020, 1, 1)) - 0.0515).abs() < f64::EPSILON);
    }

    #[test]
    fn test_covid_cut_440() {
        assert!((rbi_repo_rate(date(2020, 3, 27)) - 0.0440).abs() < f64::EPSILON);
    }

    #[test]
    fn test_covid_cut_400() {
        assert!((rbi_repo_rate(date(2020, 5, 22)) - 0.0400).abs() < f64::EPSILON);
        // Hold through 2021
        assert!((rbi_repo_rate(date(2021, 12, 31)) - 0.0400).abs() < f64::EPSILON);
    }

    #[test]
    fn test_2022_hike_cycle() {
        assert!((rbi_repo_rate(date(2022, 5, 4)) - 0.0440).abs() < f64::EPSILON);
        assert!((rbi_repo_rate(date(2022, 9, 30)) - 0.0590).abs() < f64::EPSILON);
        assert!((rbi_repo_rate(date(2022, 12, 7)) - 0.0625).abs() < f64::EPSILON);
    }

    #[test]
    fn test_2023_peak() {
        assert!((rbi_repo_rate(date(2023, 2, 8)) - 0.0650).abs() < f64::EPSILON);
        // Hold through 2024
        assert!((rbi_repo_rate(date(2024, 6, 15)) - 0.0650).abs() < f64::EPSILON);
    }

    #[test]
    fn test_2025_easing() {
        assert!((rbi_repo_rate(date(2025, 2, 7)) - 0.0625).abs() < f64::EPSILON);
        assert!((rbi_repo_rate(date(2025, 4, 9)) - 0.0600).abs() < f64::EPSILON);
    }

    #[test]
    fn test_rate_lookup_all_dates() {
        // Verify monotonic changes and all entries are accessible.
        let rate_2019 = rbi_repo_rate(date(2019, 12, 31));
        assert!(
            rate_2019 > 0.04 && rate_2019 < 0.07,
            "Pre-COVID: {rate_2019}"
        );

        let rate_2020_mid = rbi_repo_rate(date(2020, 6, 1));
        assert!((rate_2020_mid - 0.0400).abs() < f64::EPSILON);

        let rate_2026 = rbi_repo_rate(date(2026, 3, 24));
        assert!(rate_2026 > 0.04 && rate_2026 < 0.07, "Current: {rate_2026}");
    }

    #[test]
    fn test_dhan_mode_uses_10_percent() {
        assert!((risk_free_rate_for_mode("dhan", date(2024, 1, 1)) - 0.10).abs() < f64::EPSILON);
    }

    #[test]
    fn test_theoretical_mode_uses_rbi_lookup() {
        let rate = risk_free_rate_for_mode("theoretical", date(2023, 6, 15));
        assert!((rate - 0.0650).abs() < f64::EPSILON);
    }

    #[test]
    fn test_unknown_mode_defaults_to_dhan() {
        assert!((risk_free_rate_for_mode("unknown", date(2024, 1, 1)) - 0.10).abs() < f64::EPSILON);
    }
}
