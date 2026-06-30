//! Eval-path per-bar METADATA derivation + the near-month futures ROLL decision —
//! the shared Rust core consumed BIT-IDENTICALLY by BruteX (via PyO3) and
//! TickVault (native), per ADR-0012 D8 / D11 and DEC-NIFTY-ROLL-METADATA-001.
//!
//! ## Why this lives in Rust (the directive)
//!
//! The metadata that FEEDS condition bits — the bar's IST time-of-day (Group 8,
//! bits 44-47, consumed by the evaluator as `tod_seconds`), plus the weekday +
//! minute-of-session + is-expiry-day used by the time-of-day × combo expectancy
//! map (D6) and the expiry-day analysis — must be DERIVED in ONE place so the
//! backtested signal equals the live signal by construction (TickVault links this
//! crate natively + trades the near-month live). The DERIVATION (an IST civil
//! date-time → seconds-of-day / weekday / minute-of-session, and the near-month
//! at-or-after roll DECISION) is pure integer/date arithmetic; it lives here. The
//! Python side becomes a thin delegating shim.
//!
//! ## What stays in Python (BruteX orchestration)
//!
//! * The NSE/BSE holiday list + the prior-trading-day walk + the monthly-expiry
//!   calendar are DATA, not a kernel — they stay in `brutex.calendars` /
//!   `brutex.options`. Python computes the CANDIDATE (holiday-shifted) near-month
//!   expiry from that data and hands its day-ordinal to [`resolve_near_month`];
//!   the at-or-after DECISION (which contract a trade entering on a date uses, and
//!   the gap-day roll) is what lives here, shared with TickVault.
//! * The pull-time loop that stamps a WHOLE lake of bars is BruteX orchestration
//!   (it CALLS [`derive_bar_metadata`] per bar) and stays Python.
//!
//! ## No chrono dependency (the COMMON / dependency-free contract)
//!
//! The crate's pure core links no third-party calendar crate (TickVault's native
//! link stays dependency-free, mirroring the evaluator + indicators). The lake
//! ALREADY localizes every bar timestamp to IST (a FIXED +05:30, no-DST offset),
//! so the caller passes the IST CIVIL components (`hour`/`minute`/`second` and the
//! proleptic-Gregorian day-ordinal); the derivation is then exact integer
//! arithmetic with NO timezone library. The day-ordinal weekday uses the
//! proleptic-Gregorian rule (ordinal 1 == 0001-01-01 == Monday), identical to
//! Python's `date.toordinal()` / `date.weekday()`.

/// Seconds in one minute.
const SECONDS_PER_MINUTE: u32 = 60;
/// Seconds in one hour.
const SECONDS_PER_HOUR: u32 = 3600;
/// Days in one week (the weekday modulus).
const DAYS_PER_WEEK: i64 = 7;
/// Proleptic-Gregorian ordinal 1 (0001-01-01) is a Monday (weekday 0), so
/// `weekday = (ordinal - 1) mod 7`. This matches Python's `date.weekday()`
/// (`date(1,1,1).weekday() == 0`).
const ORDINAL_ONE_WEEKDAY_OFFSET: i64 = 1;

/// The derived eval-path metadata for ONE real bar (ADR-0012 D8). Money-free —
/// purely the date/time-derived fields the vocabulary's time-of-day group and the
/// D6 expectancy map read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BarMetadata {
    /// IST wall-clock time-of-day in seconds since midnight (the evaluator's
    /// `tod_seconds`; Group 8 reads only the local HH:MM:SS, never the UTC
    /// instant). Sub-second is truncated, matching the 1-minute bar grid.
    pub tod_seconds: u32,
    /// `0 = Monday … 6 = Sunday` (proleptic-Gregorian, == Python `date.weekday()`).
    pub weekday: u8,
    /// Whole minutes from the session OPEN to the bar's time-of-day. The open bar
    /// is minute 0; a bar BEFORE the open (a stray pre-open bar) yields a NEGATIVE
    /// minute — surfaced honestly (it signals a mis-fed bar), never clamped to 0.
    /// Signed so the pre-open case is representable; floor-division toward the
    /// open matches the 1-minute grid for either sign (Python `//`).
    pub minute_of_session: i32,
    /// `true` iff the bar's trading date equals the active contract's expiry — the
    /// bar is on the contract's final session (an intraday trade here force-exits
    /// at 15:20, no overnight roll; ADR-0012 D5/D11).
    pub is_expiry_day: bool,
}

/// Derive [`BarMetadata`] from a bar's IST civil components. O(1) — a few integer
/// operations, no allocation, no timezone library.
///
/// # Parameters
/// * `day_ordinal` — the bar's IST trading-date proleptic-Gregorian ordinal
///   (Python `date.toordinal()`; ordinal 1 == 0001-01-01).
/// * `hour` / `minute` / `second` — the bar's IST wall-clock time-of-day.
/// * `session_open_seconds` — the session's open time-of-day in seconds since
///   midnight (09:15 IST == 33300 for a regular session; the actual open for a
///   Muhurat / half-day session). The caller resolves this from the shared NSE
///   calendar.
/// * `expiry_ordinal` — the active near-month contract's (holiday-shifted) expiry
///   proleptic-Gregorian ordinal, for the `is_expiry_day` flag.
#[must_use]
pub fn derive_bar_metadata(
    day_ordinal: i64,
    hour: u32,
    minute: u32,
    second: u32,
    session_open_seconds: i64,
    expiry_ordinal: i64,
) -> BarMetadata {
    let tod_seconds = hour * SECONDS_PER_HOUR + minute * SECONDS_PER_MINUTE + second;
    BarMetadata {
        tod_seconds,
        weekday: weekday_from_ordinal(day_ordinal),
        minute_of_session: minute_of_session(i64::from(tod_seconds), session_open_seconds),
        is_expiry_day: day_ordinal == expiry_ordinal,
    }
}

/// The weekday `0 = Mon … 6 = Sun` of a proleptic-Gregorian day-ordinal. Uses
/// Euclidean modulo so a (defensive) non-positive ordinal still maps into
/// `0..=6` — matching Python's always-non-negative `%`.
#[inline]
#[must_use]
pub fn weekday_from_ordinal(day_ordinal: i64) -> u8 {
    // `(ordinal - 1) mod 7`, Euclidean so the result is always in 0..=6.
    ((day_ordinal - ORDINAL_ONE_WEEKDAY_OFFSET).rem_euclid(DAYS_PER_WEEK)) as u8
}

/// Whole minutes from `session_open_seconds` to `tod_seconds` (both seconds since
/// IST midnight). Floor-division toward the open (Euclidean) so a bar at/after the
/// open counts whole elapsed minutes and a pre-open bar floors to a NEGATIVE
/// minute — both in one expression, matching Python's floor `//` for either sign.
#[inline]
#[must_use]
pub fn minute_of_session(tod_seconds: i64, session_open_seconds: i64) -> i32 {
    ((tod_seconds - session_open_seconds).div_euclid(i64::from(SECONDS_PER_MINUTE))) as i32
}

/// Resolve which near-month future a trade ENTERING on `entry_ordinal` uses, given
/// the CANDIDATE (holiday-shifted) near-month expiry computed by the caller from
/// the shared NSE calendar. Returns the active contract's expiry ordinal — the
/// per-contract series identity (ADR-0012 D11). Pure, O(1).
///
/// The DECISION (shared with TickVault):
/// * If the candidate (holiday-shifted) expiry is **at or after** the entry date,
///   that is the active near month — INCLUDING on the expiry date ITSELF, where an
///   intraday trade stays on the expiring contract and force-exits at 15:20 (no
///   overnight roll; D5/D11). So an entry ON the expiry returns the expiring
///   contract's expiry.
/// * If the holiday shift pulled the candidate expiry **before** the entry date
///   (the entry sits in the 1-2 day gap between a holiday-shifted expiry and the
///   original calendar last-`<dow>`), the near contract has already expired, so the
///   caller's `next_month_shifted_ordinal` (the rolled month's holiday-shifted
///   expiry) is returned instead — the correct near month for that gap.
///
/// # Parameters
/// * `entry_ordinal` — the trade's IST entry-date ordinal.
/// * `candidate_shifted_ordinal` — the holiday-shifted expiry of the monthly
///   contract whose calendar expiry is at-or-after `entry_ordinal`.
/// * `next_month_shifted_ordinal` — the holiday-shifted expiry of the FOLLOWING
///   month (used only when the candidate has already expired). The caller computes
///   it eagerly; it is cheap and keeps this function pure (no callback).
#[must_use]
pub fn resolve_near_month(
    entry_ordinal: i64,
    candidate_shifted_ordinal: i64,
    next_month_shifted_ordinal: i64,
) -> i64 {
    if candidate_shifted_ordinal >= entry_ordinal {
        candidate_shifted_ordinal
    } else {
        next_month_shifted_ordinal
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // 09:15 IST as seconds-since-midnight (the regular session open).
    const OPEN_0915: i64 = 9 * 3600 + 15 * 60; // 33_300

    // ── weekday_from_ordinal ───────────────────────────────────────────────
    #[test]
    fn weekday_ordinal_one_is_monday() {
        // Proleptic-Gregorian ordinal 1 == 0001-01-01 == Monday (== Python).
        assert_eq!(weekday_from_ordinal(1), 0);
    }

    #[test]
    fn weekday_cycles_through_the_week() {
        // ordinals 1..=7 == Mon..Sun.
        let want = [0u8, 1, 2, 3, 4, 5, 6];
        for (i, w) in want.iter().enumerate() {
            assert_eq!(weekday_from_ordinal(1 + i as i64), *w);
        }
        // The 8th day wraps back to Monday.
        assert_eq!(weekday_from_ordinal(8), 0);
    }

    #[test]
    fn weekday_known_date_2026_06_30_is_tuesday() {
        // date(2026, 6, 30).toordinal() == 739797, a Tuesday (weekday 1).
        assert_eq!(weekday_from_ordinal(739_797), 1);
    }

    #[test]
    fn weekday_non_positive_ordinal_stays_in_range() {
        // Euclidean modulo keeps a defensive non-positive ordinal in 0..=6.
        for o in -14i64..=0 {
            assert!(weekday_from_ordinal(o) <= 6);
        }
        // ordinal 0 == (0-1).rem_euclid(7) == 6 (Sunday).
        assert_eq!(weekday_from_ordinal(0), 6);
    }

    // ── minute_of_session ──────────────────────────────────────────────────
    #[test]
    fn minute_zero_at_open() {
        assert_eq!(minute_of_session(OPEN_0915, OPEN_0915), 0);
    }

    #[test]
    fn minute_counts_whole_elapsed_minutes() {
        // 09:20 IST is minute 5 of a regular session.
        let t = 9 * 3600 + 20 * 60;
        assert_eq!(minute_of_session(t, OPEN_0915), 5);
    }

    #[test]
    fn minute_truncates_seconds_toward_open() {
        // 09:16:59 is still minute 1 (seconds floor toward the open).
        let t = 9 * 3600 + 16 * 60 + 59;
        assert_eq!(minute_of_session(t, OPEN_0915), 1);
    }

    #[test]
    fn minute_pre_open_is_negative_honest_signal() {
        // 09:00 IST (a stray pre-open bar) floors to -15, surfaced not clamped.
        let t = 9 * 3600;
        assert_eq!(minute_of_session(t, OPEN_0915), -15);
    }

    #[test]
    fn minute_custom_session_open_muhurat() {
        // A Muhurat session open at 18:00; a bar at 18:05 is minute 5.
        let open = 18 * 3600;
        let t = 18 * 3600 + 5 * 60;
        assert_eq!(minute_of_session(t, open), 5);
    }

    // ── derive_bar_metadata ────────────────────────────────────────────────
    #[test]
    fn derive_regular_bar() {
        // 2026-06-30 (Tue, ordinal 739797) 09:20:30 IST, regular open, not expiry.
        let m = derive_bar_metadata(739_797, 9, 20, 30, OPEN_0915, 739_800);
        assert_eq!(m.tod_seconds, 9 * 3600 + 20 * 60 + 30);
        assert_eq!(m.weekday, 1); // Tuesday
        assert_eq!(m.minute_of_session, 5);
        assert!(!m.is_expiry_day);
    }

    #[test]
    fn derive_sets_expiry_day_flag() {
        // The bar's date equals the contract expiry → is_expiry_day.
        let m = derive_bar_metadata(739_797, 10, 0, 0, OPEN_0915, 739_797);
        assert!(m.is_expiry_day);
        // A different expiry → not expiry day.
        let m2 = derive_bar_metadata(739_797, 10, 0, 0, OPEN_0915, 739_798);
        assert!(!m2.is_expiry_day);
    }

    #[test]
    fn derive_tod_seconds_floors_subsecond_via_grid() {
        // tod_seconds is whole seconds; the minute grid floors further.
        let m = derive_bar_metadata(739_797, 15, 19, 59, OPEN_0915, 0);
        assert_eq!(m.tod_seconds, 15 * 3600 + 19 * 60 + 59);
        // 15:19 is minute 364 of a regular session (09:15 + 364 min = 15:19).
        assert_eq!(m.minute_of_session, 364);
    }

    // ── resolve_near_month ─────────────────────────────────────────────────
    #[test]
    fn near_month_candidate_after_entry_is_active() {
        // Candidate expiry strictly after entry → candidate is the near month.
        assert_eq!(resolve_near_month(100, 110, 140), 110);
    }

    #[test]
    fn near_month_on_expiry_day_returns_expiring_contract() {
        // Entry ON the expiry (candidate == entry) returns the EXPIRING contract
        // (>= entry holds) — the intraday trade force-exits 15:20, no overnight roll.
        assert_eq!(resolve_near_month(110, 110, 140), 110);
    }

    #[test]
    fn near_month_gap_day_rolls_to_next_month() {
        // The holiday shift pulled the candidate before the entry (the 1-2 day gap
        // between a shifted expiry and the calendar last-<dow>) → roll to next month.
        assert_eq!(resolve_near_month(112, 110, 140), 140);
    }

    // ── property layer ─────────────────────────────────────────────────────
    use proptest::prelude::*;

    proptest! {
        /// The weekday is ALWAYS in 0..=6 for any ordinal, and advances by 1 (mod 7)
        /// per day — the calendar invariant.
        #[test]
        fn weekday_always_in_range_and_advances(o in -1_000_000i64..=4_000_000) {
            let w = weekday_from_ordinal(o);
            prop_assert!(w <= 6);
            let next = weekday_from_ordinal(o + 1);
            prop_assert_eq!(next, (w + 1) % 7);
        }

        /// minute-of-session is MONOTONIC non-decreasing in the bar's time-of-day
        /// (a later second is never an earlier minute), for any open.
        #[test]
        fn minute_monotonic_in_time(
            open in 0i64..=86_400,
            a in 0i64..=86_400,
            b in 0i64..=86_400,
        ) {
            let (lo, hi) = if a <= b { (a, b) } else { (b, a) };
            prop_assert!(minute_of_session(lo, open) <= minute_of_session(hi, open));
        }

        /// `is_expiry_day` is true IFF the bar's date ordinal equals the expiry
        /// ordinal — the exact D8 contract.
        #[test]
        fn is_expiry_day_iff_date_equals_expiry(
            day in 700_000i64..=740_000,
            expiry in 700_000i64..=740_000,
        ) {
            let m = derive_bar_metadata(day, 10, 0, 0, 33_300, expiry);
            prop_assert_eq!(m.is_expiry_day, day == expiry);
        }

        /// The near-month decision returns the candidate IFF it is at-or-after the
        /// entry, else the next month — never anything else.
        #[test]
        fn near_month_is_candidate_iff_not_expired(
            entry in 0i64..=1_000_000,
            cand in 0i64..=1_000_000,
            next in 0i64..=1_000_000,
        ) {
            let got = resolve_near_month(entry, cand, next);
            if cand >= entry {
                prop_assert_eq!(got, cand);
            } else {
                prop_assert_eq!(got, next);
            }
        }
    }
}
