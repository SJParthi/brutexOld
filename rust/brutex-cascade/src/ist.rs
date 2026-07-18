//! Fixed +05:30 IST calendar math over epoch-microsecond instants.
//!
//! The ONE UTC→IST conversion law for the crate — the Rust twin of the
//! deleted Python boundary (`ts.astimezone(IST).date()` /
//! `.astimezone(IST).hour` with `IST = ZoneInfo("Asia/Kolkata")`).
//!
//! ## Honesty envelope
//!
//! `Asia/Kolkata` has been fixed at +05:30 continuously since 1945, so for
//! every post-1970 epoch instant (the entire lake domain — the data floor is
//! 2020-01-01, `GROWW_HISTORY_FLOOR`) the fixed-offset arithmetic here is
//! EXACTLY the tzdb answer. Pre-1970 instants (negative epoch µs) would
//! diverge from tzdb history (+05:53:20 before 1942) — unreachable from the
//! lake, stated not hidden.

/// The IST UTC offset (+05:30) in microseconds.
pub const IST_OFFSET_US: i64 = 19_800 * 1_000_000;

/// Microseconds per civil day.
pub const DAY_US: i64 = 86_400 * 1_000_000;

/// The IST calendar day (days since 1970-01-01) of a UTC epoch-µs instant.
pub fn ist_epoch_day(ts_us: i64) -> i64 {
    (ts_us + IST_OFFSET_US).div_euclid(DAY_US)
}

/// The IST wall-clock hour (0..23) of a UTC epoch-µs instant.
pub fn ist_hour(ts_us: i64) -> i64 {
    (ts_us + IST_OFFSET_US).rem_euclid(DAY_US) / (3_600 * 1_000_000)
}

/// The UTC epoch-µs instant of IST midnight opening epoch day *day*.
pub fn ist_midnight_us(day: i64) -> i64 {
    day * DAY_US - IST_OFFSET_US
}

/// Civil (year, month, day) of an epoch day — Howard Hinnant's algorithm
/// (the `chrono`-class civil-from-days law, exact over the full i64 domain
/// used here).
pub fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097); // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// Epoch day of a civil (year, month, day) — the inverse of
/// [`civil_from_days`].
pub fn days_from_civil(y: i64, m: u32, d: u32) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y.rem_euclid(400);
    let mp = i64::from(if m > 2 { m - 3 } else { m + 9 });
    let doy = (153 * mp + 2) / 5 + i64::from(d) - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// The ISO calendar date string (`YYYY-MM-DD`) of an epoch day — the
/// `date.isoformat()` twin (4-digit-padded year; the `datetime` domain is
/// years 1..9999 so no sign/overflow arm is reachable from the shims).
pub fn iso_date(day: i64) -> String {
    let (y, m, d) = civil_from_days(day);
    format!("{y:04}-{m:02}-{d:02}")
}

/// The civil year of an epoch day (the `ist_trade_date(ts).year` bucket key).
pub fn year_of_epoch_day(day: i64) -> i64 {
    civil_from_days(day).0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn epoch_zero_is_1970() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(days_from_civil(1970, 1, 1), 0);
    }

    #[test]
    fn civil_roundtrip_wide_range() {
        for day in (-800_000..800_000).step_by(37) {
            let (y, m, d) = civil_from_days(day);
            assert_eq!(days_from_civil(y, m, d), day);
        }
    }

    #[test]
    fn ist_day_and_hour_of_a_known_instant() {
        // 2024-01-02 03:46:00 UTC == 2024-01-02 09:16 IST.
        let ts_us = days_from_civil(2024, 1, 2) * DAY_US + (3 * 3600 + 46 * 60) * 1_000_000;
        assert_eq!(civil_from_days(ist_epoch_day(ts_us)), (2024, 1, 2));
        assert_eq!(ist_hour(ts_us), 9);
        // 2024-01-02 22:30 UTC == 2024-01-03 04:00 IST — the date rolls.
        let late = days_from_civil(2024, 1, 2) * DAY_US + (22 * 3600 + 30 * 60) * 1_000_000;
        assert_eq!(civil_from_days(ist_epoch_day(late)), (2024, 1, 3));
        assert_eq!(ist_hour(late), 4);
    }

    #[test]
    fn ist_midnight_inverts_ist_epoch_day() {
        let day = days_from_civil(2024, 1, 2);
        let midnight = ist_midnight_us(day);
        assert_eq!(ist_epoch_day(midnight), day);
        assert_eq!(ist_epoch_day(midnight - 1), day - 1);
        assert_eq!(ist_epoch_day(midnight + DAY_US - 1), day);
    }

    #[test]
    fn iso_date_pads_to_four_digits() {
        assert_eq!(iso_date(days_from_civil(2024, 1, 2)), "2024-01-02");
        assert_eq!(iso_date(days_from_civil(987, 12, 31)), "0987-12-31");
    }

    #[test]
    fn year_of_epoch_day_matches_civil() {
        assert_eq!(year_of_epoch_day(days_from_civil(2023, 12, 31)), 2023);
        assert_eq!(year_of_epoch_day(days_from_civil(2024, 1, 1)), 2024);
    }
}
