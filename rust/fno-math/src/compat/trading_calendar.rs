//! NSE Trading Calendar — holiday awareness and trading day classification.
//!
//! Built at startup from config. Provides O(1) `is_trading_day()` checks
//! via `HashSet<NaiveDate>`. No allocation on the hot path — the set is
//! constructed once during boot and queried read-only thereafter.

use std::collections::{HashMap, HashSet};

use anyhow::{Result, bail};
use chrono::{Datelike, FixedOffset, NaiveDate, Utc, Weekday};

use super::config::TradingConfig;
use super::constants::IST_UTC_OFFSET_SECONDS;

/// A holiday entry with parsed date and display name.
#[derive(Debug, Clone)]
pub struct HolidayInfo {
    /// The holiday date.
    pub date: NaiveDate,
    /// Human-readable name (e.g., "Republic Day").
    pub name: String,
    /// Whether this is a Muhurat Trading session (not a regular holiday).
    pub is_muhurat: bool,
    /// Whether this is an NSE mock trading session (Saturday, ~monthly).
    pub is_mock: bool,
}

/// Pre-built trading calendar with O(1) holiday lookups.
///
/// Constructed once at startup from `TradingConfig` holiday lists.
/// Immutable after creation — safe to share across threads via `Arc`.
#[derive(Debug, Clone)]
pub struct TradingCalendar {
    /// All NSE holidays loaded from config.
    holidays: HashSet<NaiveDate>,
    /// Holiday name lookup for display/persistence.
    holiday_names: HashMap<NaiveDate, String>,
    /// Muhurat Trading dates (special evening sessions on otherwise closed days).
    muhurat_dates: HashSet<NaiveDate>,
    /// Muhurat name lookup for display/persistence.
    muhurat_names: HashMap<NaiveDate, String>,
    /// NSE mock trading session dates (Saturdays, ~monthly).
    /// NOT real trading days — for system testing awareness only.
    mock_trading_dates: HashSet<NaiveDate>,
    /// Mock trading session name lookup for display/logging.
    mock_trading_names: HashMap<NaiveDate, String>,
}

impl TradingCalendar {
    /// Builds a `TradingCalendar` from validated config entries.
    ///
    /// # Errors
    /// Returns error if any date string fails to parse as `YYYY-MM-DD`.
    pub fn from_config(trading: &TradingConfig) -> Result<Self> {
        let mut holidays = HashSet::new();
        let mut holiday_names = HashMap::new();

        for entry in &trading.nse_holidays {
            let date = NaiveDate::parse_from_str(&entry.date, "%Y-%m-%d")
                .map_err(|e| anyhow::anyhow!("invalid NSE holiday date '{}': {}", entry.date, e))?;

            // Sanity: holidays should be weekdays (Sat/Sun are already non-trading).
            if matches!(date.weekday(), Weekday::Sat | Weekday::Sun) {
                bail!(
                    "NSE holiday '{}' ({}) falls on a weekend — only list weekday holidays",
                    entry.date,
                    date.weekday()
                );
            }

            holidays.insert(date);
            holiday_names.insert(date, entry.name.clone());
        }

        let mut muhurat_dates = HashSet::new();
        let mut muhurat_names = HashMap::new();
        for entry in &trading.muhurat_trading_dates {
            let date = NaiveDate::parse_from_str(&entry.date, "%Y-%m-%d").map_err(|e| {
                anyhow::anyhow!("invalid Muhurat trading date '{}': {}", entry.date, e)
            })?;
            muhurat_dates.insert(date);
            muhurat_names.insert(date, entry.name.clone());
        }

        let mut mock_trading_dates = HashSet::new();
        let mut mock_trading_names = HashMap::new();
        for entry in &trading.nse_mock_trading_dates {
            let date = NaiveDate::parse_from_str(&entry.date, "%Y-%m-%d").map_err(|e| {
                anyhow::anyhow!("invalid NSE mock trading date '{}': {}", entry.date, e)
            })?;

            // Sanity: mock trading sessions are always on Saturdays.
            if date.weekday() != Weekday::Sat {
                bail!(
                    "NSE mock trading date '{}' ({}) is not a Saturday — mock sessions are Saturday-only",
                    entry.date,
                    date.weekday()
                );
            }

            mock_trading_dates.insert(date);
            mock_trading_names.insert(date, entry.name.clone());
        }

        Ok(Self {
            holidays,
            holiday_names,
            muhurat_dates,
            muhurat_names,
            mock_trading_dates,
            mock_trading_names,
        })
    }

    /// Returns `true` if the given date is a regular NSE trading day.
    ///
    /// A date is a trading day if:
    /// 1. It is NOT Saturday or Sunday
    /// 2. It is NOT in the NSE holiday list
    ///
    /// Note: Muhurat Trading sessions on holidays are special — they have
    /// limited hours and are handled separately. This function returns `false`
    /// for Muhurat dates that are also holidays (e.g., Diwali).
    pub fn is_trading_day(&self, date: NaiveDate) -> bool {
        if matches!(date.weekday(), Weekday::Sat | Weekday::Sun) {
            return false;
        }
        !self.holidays.contains(&date)
    }

    /// Returns `true` if today (IST) is a regular NSE trading day.
    pub fn is_trading_day_today(&self) -> bool {
        self.is_trading_day(today_ist())
    }

    /// Returns `true` if the given date has a Muhurat Trading session.
    pub fn is_muhurat_trading_day(&self, date: NaiveDate) -> bool {
        self.muhurat_dates.contains(&date)
    }

    /// Returns `true` if today (IST) has a Muhurat Trading session.
    pub fn is_muhurat_trading_today(&self) -> bool {
        self.is_muhurat_trading_day(today_ist())
    }

    /// Returns `true` if the given date is an NSE holiday.
    pub fn is_holiday(&self, date: NaiveDate) -> bool {
        self.holidays.contains(&date)
    }

    /// Returns `true` if the given date is an NSE mock trading session.
    ///
    /// Mock trading sessions are Saturdays (~monthly) for system testing.
    /// NOT real trading days — no settlement, no pay-in/pay-out.
    pub fn is_mock_trading_day(&self, date: NaiveDate) -> bool {
        self.mock_trading_dates.contains(&date)
    }

    /// Returns `true` if today (IST) is an NSE mock trading session.
    pub fn is_mock_trading_today(&self) -> bool {
        self.is_mock_trading_day(today_ist())
    }

    /// Returns the name of the mock trading session for display/logging, if any.
    pub fn mock_trading_name(&self, date: NaiveDate) -> Option<&str> {
        self.mock_trading_names.get(&date).map(|s| s.as_str())
    }

    /// Returns the next regular trading day on or after the given date.
    ///
    /// Useful for scheduling: "when is the next day we should start up?"
    ///
    /// Bounded to 14 iterations (covers worst case: long weekend + consecutive
    /// holidays). Returns the starting date if no trading day found within the
    /// window — this should never happen with a valid NSE calendar.
    pub fn next_trading_day(&self, from: NaiveDate) -> NaiveDate {
        const MAX_LOOKAHEAD_DAYS: u32 = 14;
        let mut candidate = from;
        for _ in 0..MAX_LOOKAHEAD_DAYS {
            if self.is_trading_day(candidate) {
                return candidate;
            }
            candidate = match candidate.succ_opt() {
                Some(next) => next,
                None => return from, // date overflow — return original
            };
        }
        // Exhausted lookahead — should never happen with valid calendar.
        // Return the last candidate rather than looping forever.
        candidate
    }

    /// Total number of holidays loaded.
    pub fn holiday_count(&self) -> usize {
        self.holidays.len()
    }

    /// Total number of Muhurat trading dates loaded.
    pub fn muhurat_count(&self) -> usize {
        self.muhurat_dates.len()
    }

    /// Total number of NSE mock trading session dates loaded.
    pub fn mock_trading_count(&self) -> usize {
        self.mock_trading_dates.len()
    }

    /// Returns all holiday, Muhurat, and mock trading entries for persistence/display.
    /// Sorted by date ascending.
    pub fn all_entries(&self) -> Vec<HolidayInfo> {
        let total_capacity = self
            .holidays
            .len()
            .saturating_add(self.muhurat_dates.len())
            .saturating_add(self.mock_trading_dates.len());
        let mut entries: Vec<HolidayInfo> = Vec::with_capacity(total_capacity);

        for (&date, name) in &self.holiday_names {
            entries.push(HolidayInfo {
                date,
                name: name.clone(),
                is_muhurat: false,
                is_mock: false,
            });
        }

        for (&date, name) in &self.muhurat_names {
            entries.push(HolidayInfo {
                date,
                name: name.clone(),
                is_muhurat: true,
                is_mock: false,
            });
        }

        for (&date, name) in &self.mock_trading_names {
            entries.push(HolidayInfo {
                date,
                name: name.clone(),
                is_muhurat: false,
                is_mock: true,
            });
        }

        entries.sort_by_key(|e| e.date);
        entries
    }
}

/// Returns the IST timezone offset (+05:30 / UTC+19800s).
///
/// Use this instead of `FixedOffset::east_opt(IST_UTC_OFFSET_SECONDS).expect(...)` everywhere.
/// The offset value 19800 is within `FixedOffset`'s valid range (compile-time provable constant).
#[allow(clippy::expect_used)] // APPROVED: compile-time provable — IST_UTC_OFFSET_SECONDS (19800) is always valid
pub fn ist_offset() -> FixedOffset {
    FixedOffset::east_opt(IST_UTC_OFFSET_SECONDS).expect("IST offset 19800s is always valid") // APPROVED: compile-time provable constant
}

/// Returns today's date in IST.
fn today_ist() -> NaiveDate {
    Utc::now().with_timezone(&ist_offset()).date_naive()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use super::config::NseHolidayEntry;

    fn make_test_config() -> TradingConfig {
        TradingConfig {
            market_open_time: "09:00:00".to_string(),
            market_close_time: "15:30:00".to_string(),
            order_cutoff_time: "15:29:00".to_string(),
            data_collection_start: "09:00:00".to_string(),
            data_collection_end: "15:30:00".to_string(),
            timezone: "Asia/Kolkata".to_string(),
            max_orders_per_second: 10,
            nse_holidays: vec![
                NseHolidayEntry {
                    date: "2026-01-26".to_string(),
                    name: "Republic Day".to_string(),
                },
                NseHolidayEntry {
                    date: "2026-03-03".to_string(),
                    name: "Holi".to_string(),
                },
                NseHolidayEntry {
                    date: "2026-10-20".to_string(),
                    name: "Dussehra".to_string(),
                },
            ],
            muhurat_trading_dates: vec![NseHolidayEntry {
                date: "2026-11-08".to_string(),
                name: "Diwali 2026".to_string(),
            }],
            nse_mock_trading_dates: vec![
                NseHolidayEntry {
                    date: "2026-01-03".to_string(),
                    name: "Mock Trading Session 1".to_string(),
                },
                NseHolidayEntry {
                    date: "2026-03-07".to_string(),
                    name: "Mock Trading Session 3".to_string(),
                },
            ],
        }
    }

    #[test]
    fn test_from_config_builds_calendar() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        assert_eq!(cal.holiday_count(), 3);
        assert_eq!(cal.muhurat_count(), 1);
    }

    #[test]
    fn test_weekday_non_holiday_is_trading_day() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        // 2026-01-27 is Tuesday, not a holiday
        let date = NaiveDate::from_ymd_opt(2026, 1, 27).unwrap();
        assert!(cal.is_trading_day(date));
    }

    #[test]
    fn test_holiday_is_not_trading_day() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        // 2026-01-26 is Republic Day
        let date = NaiveDate::from_ymd_opt(2026, 1, 26).unwrap();
        assert!(!cal.is_trading_day(date));
    }

    #[test]
    fn test_saturday_is_not_trading_day() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        let date = NaiveDate::from_ymd_opt(2026, 3, 7).unwrap(); // Saturday
        assert!(!cal.is_trading_day(date));
    }

    #[test]
    fn test_sunday_is_not_trading_day() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        let date = NaiveDate::from_ymd_opt(2026, 3, 8).unwrap(); // Sunday
        assert!(!cal.is_trading_day(date));
    }

    #[test]
    fn test_muhurat_day_detected() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        let date = NaiveDate::from_ymd_opt(2026, 11, 8).unwrap();
        assert!(cal.is_muhurat_trading_day(date));
    }

    #[test]
    fn test_is_holiday() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        assert!(cal.is_holiday(NaiveDate::from_ymd_opt(2026, 1, 26).unwrap()));
        assert!(!cal.is_holiday(NaiveDate::from_ymd_opt(2026, 1, 27).unwrap()));
    }

    #[test]
    fn test_next_trading_day_skips_weekend() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        // Friday 2026-03-06 is a trading day
        let friday = NaiveDate::from_ymd_opt(2026, 3, 6).unwrap();
        assert_eq!(cal.next_trading_day(friday), friday);
        // Saturday 2026-03-07 → next trading day is Monday 2026-03-09
        let saturday = NaiveDate::from_ymd_opt(2026, 3, 7).unwrap();
        assert_eq!(
            cal.next_trading_day(saturday),
            NaiveDate::from_ymd_opt(2026, 3, 9).unwrap()
        );
    }

    #[test]
    fn test_next_trading_day_skips_holiday() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        // 2026-01-26 (Mon) is Republic Day, next trading day is 2026-01-27 (Tue)
        let holiday = NaiveDate::from_ymd_opt(2026, 1, 26).unwrap();
        assert_eq!(
            cal.next_trading_day(holiday),
            NaiveDate::from_ymd_opt(2026, 1, 27).unwrap()
        );
    }

    #[test]
    fn test_weekend_holiday_in_config_rejected() {
        let mut config = make_test_config();
        config.nse_holidays.push(NseHolidayEntry {
            date: "2026-03-07".to_string(), // Saturday
            name: "Test Weekend".to_string(),
        });
        let err = TradingCalendar::from_config(&config).unwrap_err();
        assert!(err.to_string().contains("weekend"));
    }

    #[test]
    fn test_invalid_date_string_rejected() {
        let mut config = make_test_config();
        config.nse_holidays.push(NseHolidayEntry {
            date: "not-a-date".to_string(),
            name: "Bad Date".to_string(),
        });
        let err = TradingCalendar::from_config(&config).unwrap_err();
        assert!(err.to_string().contains("invalid NSE holiday date"));
    }

    #[test]
    fn test_empty_holidays_all_weekdays_are_trading_days() {
        let mut config = make_test_config();
        config.nse_holidays.clear();
        config.muhurat_trading_dates.clear();
        let cal = TradingCalendar::from_config(&config).unwrap();
        assert_eq!(cal.holiday_count(), 0);
        // Any weekday should be a trading day
        let monday = NaiveDate::from_ymd_opt(2026, 3, 9).unwrap();
        assert!(cal.is_trading_day(monday));
    }

    #[test]
    fn test_holidays_loaded() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        // Republic Day 2026
        assert!(cal.is_holiday(NaiveDate::from_ymd_opt(2026, 1, 26).unwrap()));
        // Holi 2026
        assert!(cal.is_holiday(NaiveDate::from_ymd_opt(2026, 3, 3).unwrap()));
    }

    #[test]
    fn test_all_entries_returns_sorted() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        let entries = cal.all_entries();
        // 3 holidays + 1 muhurat + 2 mock = 6 entries
        assert_eq!(entries.len(), 6);
        // Should be sorted by date
        for window in entries.windows(2) {
            assert!(window[0].date <= window[1].date);
        }
        // First is Mock Trading Session 1 (Jan 3)
        assert_eq!(entries[0].name, "Mock Trading Session 1");
        assert!(entries[0].is_mock);
        // Last should be Diwali muhurat (Nov 8)
        assert_eq!(entries[5].name, "Diwali 2026");
        assert!(entries[5].is_muhurat);
    }

    // =====================================================================
    // Additional coverage: invalid muhurat date, is_trading_day_today,
    // is_muhurat_trading_today, next_trading_day across holiday+weekend,
    // ist_offset, today_ist
    // =====================================================================

    #[test]
    fn test_invalid_muhurat_date_string_rejected() {
        let mut config = make_test_config();
        config.muhurat_trading_dates.push(NseHolidayEntry {
            date: "garbage".to_string(),
            name: "Bad Muhurat".to_string(),
        });
        let err = TradingCalendar::from_config(&config).unwrap_err();
        assert!(err.to_string().contains("invalid Muhurat trading date"));
    }

    #[test]
    fn test_next_trading_day_skips_holiday_then_weekend() {
        // Set up: Friday 2026-10-16 normal, Mon 2026-10-19 normal
        // 2026-10-20 Tue = Dussehra (holiday in make_test_config)
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        // From Dussehra (Tue), next should be Wed 2026-10-21
        let dussehra = NaiveDate::from_ymd_opt(2026, 10, 20).unwrap();
        assert!(!cal.is_trading_day(dussehra));
        let next = cal.next_trading_day(dussehra);
        assert_eq!(next, NaiveDate::from_ymd_opt(2026, 10, 21).unwrap());
    }

    #[test]
    fn test_muhurat_day_not_a_regular_holiday_check() {
        // Muhurat day 2026-11-08 is a Sunday actually - check it
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        let muhurat = NaiveDate::from_ymd_opt(2026, 11, 8).unwrap();
        // It's a Sunday, so is_trading_day is false (weekend)
        assert!(!cal.is_trading_day(muhurat));
        // But it IS a muhurat trading day
        assert!(cal.is_muhurat_trading_day(muhurat));
    }

    #[test]
    fn test_non_muhurat_day_returns_false() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        let normal_day = NaiveDate::from_ymd_opt(2026, 3, 9).unwrap();
        assert!(!cal.is_muhurat_trading_day(normal_day));
    }

    #[test]
    fn test_ist_offset_returns_5h30m() {
        let offset = super::ist_offset();
        assert_eq!(offset.local_minus_utc(), 19_800);
    }

    #[test]
    fn test_today_ist_returns_a_date() {
        // Just verify it doesn't panic and returns a reasonable date
        let today = super::today_ist();
        assert!(today.year() >= 2026);
    }

    #[test]
    fn test_is_trading_day_today_returns_bool() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        // Just verify it doesn't panic
        let _is_trading = cal.is_trading_day_today();
    }

    #[test]
    fn test_is_muhurat_trading_today_returns_bool() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        // Just verify it doesn't panic
        let _is_muhurat = cal.is_muhurat_trading_today();
    }

    #[test]
    fn test_all_entries_empty_calendar() {
        let mut config = make_test_config();
        config.nse_holidays.clear();
        config.muhurat_trading_dates.clear();
        config.nse_mock_trading_dates.clear();
        let cal = TradingCalendar::from_config(&config).unwrap();
        assert!(cal.all_entries().is_empty());
    }

    // --- all_entries: mixed holidays and muhurat with interleaved dates ---

    #[test]
    fn test_all_entries_mixed_holidays_and_muhurat_sorted_by_date() {
        // Create a config with muhurat dates interleaved between holidays.
        // This tests that all_entries correctly merges and sorts both sets.
        let mut config = make_test_config();
        config.nse_mock_trading_dates.clear(); // isolate holiday/muhurat test
        config.nse_holidays = vec![
            NseHolidayEntry {
                date: "2026-08-19".to_string(), // Wed
                name: "Independence Day Obs".to_string(),
            },
            NseHolidayEntry {
                date: "2026-01-26".to_string(), // Mon
                name: "Republic Day".to_string(),
            },
            NseHolidayEntry {
                date: "2026-11-09".to_string(), // Mon
                name: "Diwali Holiday".to_string(),
            },
        ];
        config.muhurat_trading_dates = vec![
            NseHolidayEntry {
                date: "2026-11-08".to_string(), // Sun (Diwali evening)
                name: "Muhurat Trading Diwali".to_string(),
            },
            NseHolidayEntry {
                date: "2026-03-14".to_string(), // Sat (some hypothetical muhurat)
                name: "Muhurat Holi".to_string(),
            },
        ];

        let cal = TradingCalendar::from_config(&config).unwrap();
        let entries = cal.all_entries();

        // 3 holidays + 2 muhurat = 5 entries
        assert_eq!(entries.len(), 5);

        // Verify strict date ascending order
        for window in entries.windows(2) {
            assert!(window[0].date <= window[1].date);
        }

        // Verify exact order: Jan 26, Mar 14, Aug 19, Nov 8, Nov 9
        assert_eq!(
            entries[0].date,
            NaiveDate::from_ymd_opt(2026, 1, 26).unwrap()
        );
        assert!(!entries[0].is_muhurat);
        assert_eq!(entries[0].name, "Republic Day");

        assert_eq!(
            entries[1].date,
            NaiveDate::from_ymd_opt(2026, 3, 14).unwrap()
        );
        assert!(entries[1].is_muhurat);
        assert_eq!(entries[1].name, "Muhurat Holi");

        assert_eq!(
            entries[2].date,
            NaiveDate::from_ymd_opt(2026, 8, 19).unwrap()
        );
        assert!(!entries[2].is_muhurat);

        assert_eq!(
            entries[3].date,
            NaiveDate::from_ymd_opt(2026, 11, 8).unwrap()
        );
        assert!(entries[3].is_muhurat);
        assert_eq!(entries[3].name, "Muhurat Trading Diwali");

        assert_eq!(
            entries[4].date,
            NaiveDate::from_ymd_opt(2026, 11, 9).unwrap()
        );
        assert!(!entries[4].is_muhurat);
        assert_eq!(entries[4].name, "Diwali Holiday");
    }

    #[test]
    fn test_all_entries_only_muhurat_dates() {
        let mut config = make_test_config();
        config.nse_holidays.clear();
        config.nse_mock_trading_dates.clear();
        config.muhurat_trading_dates = vec![
            NseHolidayEntry {
                date: "2026-11-08".to_string(),
                name: "Muhurat A".to_string(),
            },
            NseHolidayEntry {
                date: "2026-03-14".to_string(),
                name: "Muhurat B".to_string(),
            },
        ];
        let cal = TradingCalendar::from_config(&config).unwrap();
        let entries = cal.all_entries();

        assert_eq!(entries.len(), 2);
        assert!(entries[0].is_muhurat);
        assert!(entries[1].is_muhurat);
        // Sorted: Mar 14 before Nov 8
        assert!(entries[0].date < entries[1].date);
        assert_eq!(entries[0].name, "Muhurat B");
        assert_eq!(entries[1].name, "Muhurat A");
    }

    #[test]
    fn test_all_entries_only_holidays() {
        let mut config = make_test_config();
        config.muhurat_trading_dates.clear();
        config.nse_mock_trading_dates.clear();
        let cal = TradingCalendar::from_config(&config).unwrap();
        let entries = cal.all_entries();

        assert_eq!(entries.len(), 3);
        for entry in &entries {
            assert!(!entry.is_muhurat);
        }
        // All sorted ascending
        for window in entries.windows(2) {
            assert!(window[0].date <= window[1].date);
        }
    }

    #[test]
    fn test_all_entries_holiday_and_muhurat_on_same_date() {
        // Edge case: a date is both a holiday and has muhurat trading
        // (e.g., Diwali is a holiday but has evening muhurat session)
        let mut config = make_test_config();
        config.nse_mock_trading_dates.clear();
        config.nse_holidays = vec![NseHolidayEntry {
            date: "2026-11-09".to_string(), // Mon
            name: "Diwali".to_string(),
        }];
        config.muhurat_trading_dates = vec![NseHolidayEntry {
            date: "2026-11-09".to_string(), // Same date
            name: "Muhurat Trading".to_string(),
        }];

        let cal = TradingCalendar::from_config(&config).unwrap();
        let entries = cal.all_entries();

        // Both entries should be present (2 entries on same date)
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].date, entries[1].date);
        // One is holiday, one is muhurat
        let has_holiday = entries.iter().any(|e| !e.is_muhurat);
        let has_muhurat = entries.iter().any(|e| e.is_muhurat);
        assert!(has_holiday);
        assert!(has_muhurat);
        // The date should be a holiday
        assert!(cal.is_holiday(NaiveDate::from_ymd_opt(2026, 11, 9).unwrap()));
        // And also a muhurat day
        assert!(cal.is_muhurat_trading_day(NaiveDate::from_ymd_opt(2026, 11, 9).unwrap()));
        // But NOT a regular trading day (it's a holiday)
        assert!(!cal.is_trading_day(NaiveDate::from_ymd_opt(2026, 11, 9).unwrap()));
    }

    #[test]
    fn test_all_entries_mixed_with_reverse_insertion_order() {
        // Holidays inserted in reverse chronological order to verify
        // all_entries still returns sorted output regardless of insertion order.
        let mut config = make_test_config();
        config.nse_mock_trading_dates.clear();
        config.nse_holidays = vec![
            NseHolidayEntry {
                date: "2026-12-25".to_string(), // Fri
                name: "Christmas".to_string(),
            },
            NseHolidayEntry {
                date: "2026-08-19".to_string(), // Wed
                name: "Independence Day Obs".to_string(),
            },
            NseHolidayEntry {
                date: "2026-01-26".to_string(), // Mon
                name: "Republic Day".to_string(),
            },
        ];
        config.muhurat_trading_dates = vec![
            NseHolidayEntry {
                date: "2026-11-08".to_string(), // Sun
                name: "Muhurat Diwali".to_string(),
            },
            NseHolidayEntry {
                date: "2026-03-14".to_string(), // Sat
                name: "Muhurat Holi".to_string(),
            },
        ];

        let cal = TradingCalendar::from_config(&config).unwrap();
        assert_eq!(cal.holiday_count(), 3);
        assert_eq!(cal.muhurat_count(), 2);

        let entries = cal.all_entries();
        assert_eq!(entries.len(), 5);

        // Verify strictly ascending date order
        for window in entries.windows(2) {
            assert!(window[0].date < window[1].date);
        }

        // Expected order: Jan 26, Mar 14, Aug 19, Nov 8, Dec 25
        assert_eq!(
            entries[0].date,
            NaiveDate::from_ymd_opt(2026, 1, 26).unwrap()
        );
        assert!(!entries[0].is_muhurat);
        assert_eq!(
            entries[1].date,
            NaiveDate::from_ymd_opt(2026, 3, 14).unwrap()
        );
        assert!(entries[1].is_muhurat);
        assert_eq!(
            entries[2].date,
            NaiveDate::from_ymd_opt(2026, 8, 19).unwrap()
        );
        assert!(!entries[2].is_muhurat);
        assert_eq!(
            entries[3].date,
            NaiveDate::from_ymd_opt(2026, 11, 8).unwrap()
        );
        assert!(entries[3].is_muhurat);
        assert_eq!(
            entries[4].date,
            NaiveDate::from_ymd_opt(2026, 12, 25).unwrap()
        );
        assert!(!entries[4].is_muhurat);
    }

    #[test]
    fn test_next_trading_day_skips_consecutive_holidays_and_weekend() {
        // Setup: Fri holiday, Sat weekend, Sun weekend, Mon holiday
        // next_trading_day from Fri should jump to Tue.
        let mut config = make_test_config();
        config.nse_holidays = vec![
            NseHolidayEntry {
                date: "2026-03-13".to_string(), // Fri
                name: "Holiday A".to_string(),
            },
            NseHolidayEntry {
                date: "2026-03-16".to_string(), // Mon
                name: "Holiday B".to_string(),
            },
        ];
        config.muhurat_trading_dates.clear();

        let cal = TradingCalendar::from_config(&config).unwrap();
        let friday = NaiveDate::from_ymd_opt(2026, 3, 13).unwrap();
        let expected_tuesday = NaiveDate::from_ymd_opt(2026, 3, 17).unwrap();
        assert_eq!(cal.next_trading_day(friday), expected_tuesday);
    }

    #[test]
    fn test_holiday_count_and_muhurat_count_independent() {
        // Verify counts are maintained independently
        let mut config = make_test_config();
        config.nse_holidays = vec![
            NseHolidayEntry {
                date: "2026-01-26".to_string(),
                name: "H1".to_string(),
            },
            NseHolidayEntry {
                date: "2026-03-03".to_string(),
                name: "H2".to_string(),
            },
            NseHolidayEntry {
                date: "2026-08-19".to_string(),
                name: "H3".to_string(),
            },
            NseHolidayEntry {
                date: "2026-10-20".to_string(),
                name: "H4".to_string(),
            },
            NseHolidayEntry {
                date: "2026-12-25".to_string(),
                name: "H5".to_string(),
            },
        ];
        config.muhurat_trading_dates = vec![
            NseHolidayEntry {
                date: "2026-11-08".to_string(),
                name: "M1".to_string(),
            },
            NseHolidayEntry {
                date: "2026-03-14".to_string(),
                name: "M2".to_string(),
            },
            NseHolidayEntry {
                date: "2026-06-06".to_string(),
                name: "M3".to_string(),
            },
        ];

        let cal = TradingCalendar::from_config(&config).unwrap();
        assert_eq!(cal.holiday_count(), 5);
        assert_eq!(cal.muhurat_count(), 3);
        assert_eq!(cal.mock_trading_count(), 2);
        // all_entries has all three sets
        assert_eq!(cal.all_entries().len(), 10);
    }

    #[test]
    fn test_all_entries_names_preserved() {
        // Verify that entry names match what was configured
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        let entries = cal.all_entries();

        let names: Vec<&str> = entries.iter().map(|e| e.name.as_str()).collect();
        assert!(names.contains(&"Republic Day"));
        assert!(names.contains(&"Holi"));
        assert!(names.contains(&"Dussehra"));
        assert!(names.contains(&"Diwali 2026"));
    }

    #[test]
    fn test_all_entries_is_muhurat_flag_correct() {
        // Verify is_muhurat flag is set only for muhurat entries, not holidays/mock
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        let entries = cal.all_entries();

        let muhurat_entries: Vec<&HolidayInfo> = entries.iter().filter(|e| e.is_muhurat).collect();
        let mock_entries: Vec<&HolidayInfo> = entries.iter().filter(|e| e.is_mock).collect();
        let holiday_entries: Vec<&HolidayInfo> = entries
            .iter()
            .filter(|e| !e.is_muhurat && !e.is_mock)
            .collect();

        assert_eq!(muhurat_entries.len(), 1);
        assert_eq!(mock_entries.len(), 2);
        assert_eq!(holiday_entries.len(), 3);
        assert_eq!(muhurat_entries[0].name, "Diwali 2026");
    }

    #[test]
    fn test_next_trading_day_on_trading_day_returns_same() {
        // When starting on a regular trading day, next_trading_day returns the same date
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        let tuesday = NaiveDate::from_ymd_opt(2026, 3, 10).unwrap();
        assert!(cal.is_trading_day(tuesday));
        assert_eq!(cal.next_trading_day(tuesday), tuesday);
    }

    // =====================================================================
    // Mock Trading Session Tests
    // =====================================================================

    #[test]
    fn test_mock_trading_day_detected() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        // 2026-01-03 Saturday — in mock trading dates
        let mock_day = NaiveDate::from_ymd_opt(2026, 1, 3).unwrap();
        assert!(cal.is_mock_trading_day(mock_day));
    }

    #[test]
    fn test_non_mock_saturday_not_mock_day() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        // 2026-01-10 Saturday — NOT in mock trading dates
        let normal_sat = NaiveDate::from_ymd_opt(2026, 1, 10).unwrap();
        assert!(!cal.is_mock_trading_day(normal_sat));
    }

    #[test]
    fn test_mock_trading_day_is_not_regular_trading_day() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        // Mock trading Saturday is still NOT a regular trading day
        let mock_day = NaiveDate::from_ymd_opt(2026, 1, 3).unwrap();
        assert!(cal.is_mock_trading_day(mock_day));
        assert!(!cal.is_trading_day(mock_day)); // Saturday → false
    }

    #[test]
    fn test_mock_trading_count() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        assert_eq!(cal.mock_trading_count(), 2);
    }

    #[test]
    fn test_mock_trading_name_returns_name() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        let mock_day = NaiveDate::from_ymd_opt(2026, 1, 3).unwrap();
        assert_eq!(
            cal.mock_trading_name(mock_day),
            Some("Mock Trading Session 1")
        );
    }

    #[test]
    fn test_mock_trading_name_returns_none_for_non_mock() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        let normal_day = NaiveDate::from_ymd_opt(2026, 3, 9).unwrap();
        assert!(cal.mock_trading_name(normal_day).is_none());
    }

    #[test]
    fn test_mock_trading_non_saturday_rejected() {
        let mut config = make_test_config();
        config.nse_mock_trading_dates.push(NseHolidayEntry {
            date: "2026-03-09".to_string(), // Monday
            name: "Bad Mock".to_string(),
        });
        let err = TradingCalendar::from_config(&config).unwrap_err();
        assert!(err.to_string().contains("not a Saturday"));
    }

    #[test]
    fn test_mock_trading_invalid_date_rejected() {
        let mut config = make_test_config();
        config.nse_mock_trading_dates.push(NseHolidayEntry {
            date: "garbage".to_string(),
            name: "Bad Date".to_string(),
        });
        let err = TradingCalendar::from_config(&config).unwrap_err();
        assert!(err.to_string().contains("invalid NSE mock trading date"));
    }

    #[test]
    fn test_mock_trading_empty_config() {
        let mut config = make_test_config();
        config.nse_mock_trading_dates.clear();
        let cal = TradingCalendar::from_config(&config).unwrap();
        assert_eq!(cal.mock_trading_count(), 0);
        assert!(!cal.is_mock_trading_day(NaiveDate::from_ymd_opt(2026, 1, 3).unwrap()));
    }

    #[test]
    fn test_is_mock_trading_today_returns_bool() {
        let config = make_test_config();
        let cal = TradingCalendar::from_config(&config).unwrap();
        // Just verify it doesn't panic
        let _is_mock = cal.is_mock_trading_today();
    }

    // =====================================================================
    // Coverage: next_trading_day edge cases (lines 185, 190)
    // =====================================================================

    #[test]
    fn test_next_trading_day_date_max_overflow() {
        // Line 185: NaiveDate::MAX.succ_opt() returns None → return from.
        // NaiveDate::MAX might be a weekday, so we need to add it as a holiday
        // to force the loop to try succ_opt() which returns None.
        use super::config::{NseHolidayEntry, TradingConfig};
        let max_date = chrono::NaiveDate::MAX;
        let config = TradingConfig {
            market_open_time: "09:15:00".to_string(),
            market_close_time: "15:30:00".to_string(),
            order_cutoff_time: "15:29:00".to_string(),
            data_collection_start: "09:00:00".to_string(),
            data_collection_end: "15:30:00".to_string(),
            timezone: "Asia/Kolkata".to_string(),
            max_orders_per_second: 10,
            nse_holidays: vec![NseHolidayEntry {
                date: max_date.format("%Y-%m-%d").to_string(),
                name: "Max date holiday".to_string(),
            }],
            muhurat_trading_dates: vec![],
            nse_mock_trading_dates: vec![],
        };
        let cal = TradingCalendar::from_config(&config).unwrap();
        // MAX is now a holiday → loop tries succ_opt() → None → return from.
        let result = cal.next_trading_day(max_date);
        assert_eq!(result, max_date);
    }

    #[test]
    fn test_next_trading_day_exhausted_lookahead() {
        // Line 190: 14 consecutive non-trading days exhausts the lookahead.
        // Create a calendar where every weekday in a 14-day window is a holiday.
        // Loop checks days 0..13 (14 iterations). All must be non-trading.
        use super::config::{NseHolidayEntry, TradingConfig};
        let start = NaiveDate::from_ymd_opt(2026, 6, 15).unwrap(); // Monday
        let mut holidays = Vec::new();
        // Need to cover 14 days from start (days 0-13). Weekends are auto non-trading.
        // Add all weekdays in the range as holidays.
        for i in 0..15 {
            let d = start + chrono::Duration::days(i);
            if d.weekday() != chrono::Weekday::Sat && d.weekday() != chrono::Weekday::Sun {
                holidays.push(NseHolidayEntry {
                    date: d.format("%Y-%m-%d").to_string(),
                    name: format!("Holiday {i}"),
                });
            }
        }
        let config = TradingConfig {
            market_open_time: "09:15:00".to_string(),
            market_close_time: "15:30:00".to_string(),
            order_cutoff_time: "15:29:00".to_string(),
            data_collection_start: "09:00:00".to_string(),
            data_collection_end: "15:30:00".to_string(),
            timezone: "Asia/Kolkata".to_string(),
            max_orders_per_second: 10,
            nse_holidays: holidays,
            muhurat_trading_dates: vec![],
            nse_mock_trading_dates: vec![],
        };
        let cal = TradingCalendar::from_config(&config).unwrap();
        // Verify all 14 days are non-trading
        for i in 0..14 {
            let d = start + chrono::Duration::days(i);
            assert!(
                !cal.is_trading_day(d),
                "Day {i} ({d}) should be non-trading"
            );
        }
        let result = cal.next_trading_day(start);
        // Exhausted lookahead → returns last candidate (start + 14, since
        // loop increments candidate after checking, ending at day 14).
        let expected_last = start + chrono::Duration::days(14);
        assert_eq!(result, expected_last);
    }
}
