//! The market-hours guard predicate — the pure clockless DECISION core.
//!
//! Law (DEC-GROWW-MARKET-HOURS-GUARD-001, MIGRATION.md #1692): a vendor pull
//! is allowed UNLESS the date is an NSE trading day AND the IST wall-clock
//! time sits in `[08:45, 15:35)` — the window opens 30 minutes before the
//! 09:15 session (ahead of the exchange's pre-open storm) and closes 5
//! minutes after the 15:30 close (the shared account demonstrably quiet
//! before a pull may resume). On a non-trading day (weekend / holiday) a
//! pull is ALWAYS allowed regardless of the time.
//!
//! The time crosses the FFI as MICROSECOND-OF-DAY (`i64` in
//! `[0, 86_400_000_000)`): Python `datetime.time` comparison is
//! lexicographic over `(hour, minute, second, microsecond)`, which is
//! order-isomorphic to microsecond-of-day, so
//! `BLOCK_START <= t < BLOCK_END` in Python is EXACTLY
//! `BLOCK_START_US <= us < BLOCK_END_US` here — the boundary law is
//! bit-identical for every representable wall-clock instant (pinned by the
//! frozen golden + the hypothesis differential on the Python side, and the
//! exhaustive boundary tests here).
//!
//! The trading-day flag is resolved by the CALLER (Python's
//! `brutex.calendars.nse.is_trading_day` — holiday sets, special weekend
//! sessions, the out-of-coverage fail-safe all stay in the one Python
//! source of truth; this core never re-mints the calendar).

/// The pull-blocked window START in IST microseconds-of-day, INCLUSIVE
/// (08:45:00 — mirrors `brutex.calendars.market_hours.BLOCK_START`).
pub const BLOCK_START_US: i64 = ((8 * 60 + 45) * 60) * 1_000_000;

/// The pull-blocked window END in IST microseconds-of-day, EXCLUSIVE
/// (15:35:00 — mirrors `brutex.calendars.market_hours.BLOCK_END`).
pub const BLOCK_END_US: i64 = ((15 * 60 + 35) * 60) * 1_000_000;

/// Microseconds in one civil day — the exclusive upper bound of a valid
/// microsecond-of-day (Python `datetime.time` cannot represent 24:00:00).
pub const DAY_US: i64 = 86_400_000_000;

/// `true` iff a VENDOR pull is allowed at the given IST instant — the PURE
/// predicate (the `pull_allowed_now` law, integers-only).
///
/// # Errors
///
/// A `us_of_day` outside `[0, DAY_US)` is a caller defect (no valid
/// `datetime.time` maps there) and is REFUSED with a message — never
/// silently clamped; the Python facade re-raises it as the typed
/// `PullStateKernelError` (fail-closed).
pub fn pull_allowed_us(us_of_day: i64, is_trading_day: bool) -> Result<bool, String> {
    if !(0..DAY_US).contains(&us_of_day) {
        return Err(format!(
            "us_of_day {us_of_day} out of range [0, {DAY_US}) — not a valid wall-clock instant"
        ));
    }
    if !is_trading_day {
        return Ok(true);
    }
    Ok(!(BLOCK_START_US..BLOCK_END_US).contains(&us_of_day))
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECOND_US: i64 = 1_000_000;

    #[test]
    fn window_constants_mirror_python() {
        // 08:45:00 / 15:35:00 IST — the DEC-GROWW-MARKET-HOURS-GUARD-001 law.
        assert_eq!(BLOCK_START_US, 31_500_000_000);
        assert_eq!(BLOCK_END_US, 56_100_000_000);
        assert_eq!(DAY_US, 86_400_000_000);
    }

    #[test]
    fn trading_day_boundaries_are_exact() {
        // One microsecond before open → allowed; at open → blocked
        // (inclusive); one microsecond before close → blocked; at close →
        // allowed (exclusive).
        assert_eq!(pull_allowed_us(BLOCK_START_US - 1, true), Ok(true));
        assert_eq!(pull_allowed_us(BLOCK_START_US, true), Ok(false));
        assert_eq!(pull_allowed_us(BLOCK_END_US - 1, true), Ok(false));
        assert_eq!(pull_allowed_us(BLOCK_END_US, true), Ok(true));
        // The golden's second-granular boundary probes (08:44:59 / 15:34:59).
        assert_eq!(pull_allowed_us(BLOCK_START_US - SECOND_US, true), Ok(true));
        assert_eq!(pull_allowed_us(BLOCK_END_US - SECOND_US, true), Ok(false));
    }

    #[test]
    fn representative_trading_day_instants() {
        for (us, allowed) in [
            (0, true),                                         // midnight
            (3 * 3600 * SECOND_US + 1800 * SECOND_US, true),   // 03:30 nightly slot
            (9 * 3600 * SECOND_US + 900 * SECOND_US, false),   // 09:15 session open
            (12 * 3600 * SECOND_US, false),                    // mid-session
            (15 * 3600 * SECOND_US + 1800 * SECOND_US, false), // 15:30 close
            (20 * 3600 * SECOND_US, true),                     // evening
            (DAY_US - 1, true),                                // 23:59:59.999999
        ] {
            assert_eq!(pull_allowed_us(us, true), Ok(allowed), "us={us}");
        }
    }

    #[test]
    fn non_trading_day_always_allowed() {
        for us in [
            0,
            BLOCK_START_US - 1,
            BLOCK_START_US,
            12 * 3600 * SECOND_US,
            BLOCK_END_US,
            DAY_US - 1,
        ] {
            assert_eq!(pull_allowed_us(us, false), Ok(true), "us={us}");
        }
    }

    #[test]
    fn out_of_range_is_refused_never_clamped() {
        for us in [-1, i64::MIN, DAY_US, i64::MAX] {
            let err = pull_allowed_us(us, true).unwrap_err();
            assert!(err.contains("out of range"), "err={err}");
            assert!(err.contains(&us.to_string()), "err={err}");
            // A non-trading flag never bypasses the range refusal (the
            // validation runs FIRST — total over the input domain).
            assert!(pull_allowed_us(us, false).is_err(), "us={us}");
        }
    }
}
