//! NSE/BSE calendar VERDICT engine — the frozen decision logic, data-free.
//!
//! DEC-CALENDAR-VERDICT-RUST-001 (slice 9, ADR-0019 / MIGRATION.md #1727):
//! the Rust port of `brutex/calendars/nse.py`'s verdict LOGIC. Per the
//! ADR-0012 D8-compatible reading, calendar DATA does NOT move: the holiday
//! ordinals, Muhurat + special-weekend sessions, coverage span and session
//! bounds are supplied by Python AT RUNTIME (as `date.toordinal()`
//! proleptic-Gregorian ordinals + seconds-of-day) when
//! [`CalendarVerdictEngine`] is constructed — never baked at build time (a
//! baked calendar desyncs the moment `HOLIDAYS_2027` lands and cannot see
//! the runtime observed-sessions overlay).
//!
//! ## Verdict encoding (checked-triage as data, raises stay Python)
//!
//! The `*_checked` functions return an `i8` triage code instead of raising:
//! `0` = False/None, `1` = True/Some, `2` = RAISE (out-of-coverage — the
//! Python seam routes code `2` through the SAME `_raise_unavailable` helper
//! the Python oracle uses, so `HolidayDataUnavailableError` messages and the
//! `calendar.holiday_data_unavailable` WARN fields can never drift).
//! Exchange STRING validation (`ValueError`) also stays Python, run EAGERLY
//! before any Rust call; the engine takes `sel: u8` — `0` = ALL (`None`),
//! `1` = NSE, `2` = BSE (any other value is treated as ALL; the Python
//! wrapper can never send one).
//!
//! ## Date arithmetic — NO chrono
//!
//! Two pure-integer helpers (the trading-core `weekday_from_ordinal`
//! precedent): `weekday(ordinal)` (Monday = 0; Python
//! `date(1,1,1).toordinal() == 1` is a Monday) and `year_of_ordinal` via the
//! Hinnant `civil_from_days` algorithm — needed ONLY for the coverage-span
//! check. Both are differential-pinned against `datetime.date` in
//! `tests/property/test_calendar_rust_parity.py` (1900..2100).
//!
//! Frozen-divergence honesty: `daily_derive_cutoff` keeps the STRICT `>`
//! at-close law (DEC-SESSION-CLOSE-BOUNDARY-PIN-001 — at exactly 15:30:00
//! the session is still "forming"); any "unification" with the pull side's
//! `>=` would be a defect, not a cleanup.

use std::collections::{HashMap, HashSet};

use pyo3::prelude::*;

/// Triage code: the verdict is `False` / `None`.
pub const VERDICT_FALSE: i8 = 0;
/// Triage code: the verdict is `True` / `Some(session)`.
pub const VERDICT_TRUE: i8 = 1;
/// Triage code: out-of-coverage — the Python seam raises
/// `HolidayDataUnavailableError` through the shared helper.
pub const VERDICT_RAISE: i8 = 2;

/// Session-type code for a REGULAR (incl. special-weekend) session.
pub const SESSION_REGULAR: u8 = 0;
/// Session-type code for a Diwali MUHURAT session.
pub const SESSION_MUHURAT: u8 = 1;

/// `date.weekday()` twin over a proleptic-Gregorian ordinal (Monday = 0).
/// Python `date(1, 1, 1).toordinal() == 1` is a Monday — pinned in tests.
#[inline]
pub fn weekday_of_ordinal(ordinal: i32) -> u8 {
    (i64::from(ordinal) - 1).rem_euclid(7) as u8
}

/// `date.fromordinal(o).year` twin — Hinnant `civil_from_days` (year only).
/// `days-since-1970-01-01 = ordinal − 719_163`; the algorithm's day-0 is
/// 0000-03-01, `719_468` days before 1970-01-01, so `z = ordinal + 305`.
#[inline]
pub fn year_of_ordinal(ordinal: i32) -> i32 {
    let z: i64 = i64::from(ordinal) - 719_163 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097); // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let m = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    (y + i64::from(m <= 2)) as i32
}

/// The pure verdict engine (no PyO3 types — fully `cargo test`-able).
/// [`CalendarVerdictEngine`] is its frozen PyO3 wrapper.
pub struct CalendarEngine {
    common_holidays: HashSet<i32>,
    nse_only: HashSet<i32>,
    bse_only: HashSet<i32>,
    muhurat: HashMap<i32, (u32, u32)>,
    special_weekend: HashMap<i32, (u32, u32)>,
    /// Per-date EXTENDED sessions (DEC-EXTENDED-SESSION-OUTAGE-001 — the
    /// 2021-02-24 NSE exchange-outage day, close extended to 17:00 IST).
    /// Mirrors `nse.EXTENDED_SESSIONS`: REGULAR type, resolved AFTER the
    /// special-weekend tier and BEFORE the regular arm in both session twins.
    extended: HashMap<i32, (u32, u32)>,
    coverage_first_year: i32,
    coverage_last_year: i32,
    session_open_s: u32,
    session_close_s: u32,
}

impl CalendarEngine {
    /// Build from Python-supplied config (see the module docs — the data
    /// stays owned by `brutex.calendars`; this is a per-process snapshot).
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        common_holidays: Vec<i32>,
        nse_only: Vec<i32>,
        bse_only: Vec<i32>,
        muhurat: Vec<(i32, u32, u32)>,
        special_weekend: Vec<(i32, u32, u32)>,
        extended: Vec<(i32, u32, u32)>,
        coverage_first_year: i32,
        coverage_last_year: i32,
        session_open_s: u32,
        session_close_s: u32,
    ) -> Self {
        Self {
            common_holidays: common_holidays.into_iter().collect(),
            nse_only: nse_only.into_iter().collect(),
            bse_only: bse_only.into_iter().collect(),
            muhurat: muhurat.into_iter().map(|(d, o, c)| (d, (o, c))).collect(),
            special_weekend: special_weekend
                .into_iter()
                .map(|(d, o, c)| (d, (o, c)))
                .collect(),
            extended: extended.into_iter().map(|(d, o, c)| (d, (o, c))).collect(),
            coverage_first_year,
            coverage_last_year,
            session_open_s,
            session_close_s,
        }
    }

    /// Holiday-set membership under the exchange selector (`0` ALL / `1`
    /// NSE / `2` BSE). ALL = the union — byte-identical to Python's
    /// `ALL_HOLIDAYS = NSE_HOLIDAYS | BSE_HOLIDAYS`.
    fn holiday_member(&self, ordinal: i32, sel: u8) -> bool {
        let common = self.common_holidays.contains(&ordinal);
        match sel {
            1 => common || self.nse_only.contains(&ordinal),
            2 => common || self.bse_only.contains(&ordinal),
            _ => common || self.nse_only.contains(&ordinal) || self.bse_only.contains(&ordinal),
        }
    }

    /// `is_holiday_data_available` twin — the inclusive coverage-year span.
    pub fn coverage_available(&self, ordinal: i32) -> bool {
        let year = year_of_ordinal(ordinal);
        self.coverage_first_year <= year && year <= self.coverage_last_year
    }

    /// Unguarded `is_holiday` — silent `false` out of coverage.
    pub fn is_holiday(&self, ordinal: i32, sel: u8) -> bool {
        self.holiday_member(ordinal, sel)
    }

    /// `is_holiday_checked` triage: RAISE out of coverage, else the verdict.
    pub fn is_holiday_checked(&self, ordinal: i32, sel: u8) -> i8 {
        if !self.coverage_available(ordinal) {
            return VERDICT_RAISE;
        }
        if self.holiday_member(ordinal, sel) {
            VERDICT_TRUE
        } else {
            VERDICT_FALSE
        }
    }

    /// `is_weekend` twin (Saturday = 5, Sunday = 6).
    pub fn is_weekend(&self, ordinal: i32) -> bool {
        weekday_of_ordinal(ordinal) >= 5
    }

    /// Unguarded `is_trading_day`: special weekend wins BEFORE the weekend
    /// test; a Muhurat date is NOT a trading day (its date is a holiday /
    /// weekend in the data).
    pub fn is_trading_day(&self, ordinal: i32, sel: u8) -> bool {
        if self.special_weekend.contains_key(&ordinal) {
            return true;
        }
        !self.is_weekend(ordinal) && !self.holiday_member(ordinal, sel)
    }

    /// `is_trading_day_checked` triage: special weekend → TRUE and weekend →
    /// FALSE without raising even out of coverage (data-complete answers);
    /// an out-of-coverage WEEKDAY is the genuinely-ambiguous case → RAISE.
    pub fn is_trading_day_checked(&self, ordinal: i32, sel: u8) -> i8 {
        if self.special_weekend.contains_key(&ordinal) {
            return VERDICT_TRUE;
        }
        if self.is_weekend(ordinal) {
            return VERDICT_FALSE;
        }
        match self.is_holiday_checked(ordinal, sel) {
            VERDICT_RAISE => VERDICT_RAISE,
            VERDICT_TRUE => VERDICT_FALSE,
            _ => VERDICT_TRUE,
        }
    }

    /// `has_muhurat_session` twin.
    pub fn has_muhurat(&self, ordinal: i32) -> bool {
        self.muhurat.contains_key(&ordinal)
    }

    /// `is_special_weekend_session` twin.
    pub fn is_special_weekend(&self, ordinal: i32) -> bool {
        self.special_weekend.contains_key(&ordinal)
    }

    /// Unguarded `get_session` → `(type, open_s, close_s)` or `None`.
    /// Resolution order: Muhurat → special weekend → extended →
    /// regular-if-trading → `None` (the `_py_get_session` tier order —
    /// DEC-EXTENDED-SESSION-OUTAGE-001 added the extended tier on main).
    /// An out-of-coverage weekday FABRICATES a regular session —
    /// the documented, load-bearing unguarded behaviour, preserved verbatim.
    pub fn get_session(&self, ordinal: i32, sel: u8) -> Option<(u8, u32, u32)> {
        if let Some(&(open_s, close_s)) = self.muhurat.get(&ordinal) {
            return Some((SESSION_MUHURAT, open_s, close_s));
        }
        if let Some(&(open_s, close_s)) = self.special_weekend.get(&ordinal) {
            return Some((SESSION_REGULAR, open_s, close_s));
        }
        if let Some(&(open_s, close_s)) = self.extended.get(&ordinal) {
            return Some((SESSION_REGULAR, open_s, close_s));
        }
        if !self.is_trading_day(ordinal, sel) {
            return None;
        }
        Some((SESSION_REGULAR, self.session_open_s, self.session_close_s))
    }

    /// `get_session_checked` → `(verdict, type, open_s, close_s)`; verdict
    /// `0` None / `1` Some / `2` RAISE (type/open/close are 0 for 0 and 2).
    /// The extended tier is data-complete — it resolves TRUE without raising
    /// even out of coverage, exactly like Muhurat / special weekend
    /// (the `_py_get_session_checked` tier order).
    pub fn get_session_checked(&self, ordinal: i32, sel: u8) -> (i8, u8, u32, u32) {
        if let Some(&(open_s, close_s)) = self.muhurat.get(&ordinal) {
            return (VERDICT_TRUE, SESSION_MUHURAT, open_s, close_s);
        }
        if let Some(&(open_s, close_s)) = self.special_weekend.get(&ordinal) {
            return (VERDICT_TRUE, SESSION_REGULAR, open_s, close_s);
        }
        if let Some(&(open_s, close_s)) = self.extended.get(&ordinal) {
            return (VERDICT_TRUE, SESSION_REGULAR, open_s, close_s);
        }
        match self.is_trading_day_checked(ordinal, sel) {
            VERDICT_RAISE => (VERDICT_RAISE, 0, 0, 0),
            VERDICT_TRUE => (
                VERDICT_TRUE,
                SESSION_REGULAR,
                self.session_open_s,
                self.session_close_s,
            ),
            _ => (VERDICT_FALSE, 0, 0, 0),
        }
    }

    /// `previous_trading_session` walk → `(1, prev_ordinal)` on success or
    /// `(2, probe_ordinal_that_raised)` when the back-walk crosses the
    /// coverage floor (the Python seam then emits the ONE
    /// `calendar.previous_session_coverage_exhausted` WARN and returns
    /// `None`). Predicate: `is_trading_day_checked(probe) == TRUE` AND
    /// `probe ∉ observed` (the REMOVE-ONLY overlay). Termination is the
    /// checked variant's coverage-floor raise, exactly as in Python.
    pub fn previous_trading_session(
        &self,
        ordinal: i32,
        sel: u8,
        observed: &HashSet<i32>,
    ) -> (i8, i32) {
        let mut probe = ordinal - 1;
        loop {
            match self.is_trading_day_checked(probe, sel) {
                VERDICT_RAISE => return (VERDICT_RAISE, probe),
                VERDICT_TRUE if !observed.contains(&probe) => return (VERDICT_TRUE, probe),
                _ => probe -= 1,
            }
        }
    }

    /// Unguarded `trading_days_between` — inclusive `[start, end]`.
    pub fn trading_days_between(&self, start: i32, end: i32, sel: u8) -> Vec<i32> {
        let mut days = Vec::new();
        let mut d = start;
        while d <= end {
            if self.is_trading_day(d, sel) {
                days.push(d);
            }
            d += 1;
        }
        days
    }

    /// `trading_days_between_checked` → `(verdict, days, offending_ordinal)`.
    /// On the FIRST out-of-coverage weekday the walk stops with verdict `2`
    /// and the offending ordinal (the Python seam raises with THAT date);
    /// the partial day list is discarded (Python's raise discards it too).
    pub fn trading_days_between_checked(
        &self,
        start: i32,
        end: i32,
        sel: u8,
    ) -> (i8, Vec<i32>, i32) {
        let mut days = Vec::new();
        let mut d = start;
        while d <= end {
            match self.is_trading_day_checked(d, sel) {
                VERDICT_RAISE => return (VERDICT_RAISE, Vec::new(), d),
                VERDICT_TRUE => days.push(d),
                _ => {}
            }
            d += 1;
        }
        (VERDICT_TRUE, days, 0)
    }

    /// `daily_derive_cutoff` twin: the STRICT `>` at-close law
    /// (DEC-SESSION-CLOSE-BOUNDARY-PIN-001 — at exactly close the session
    /// is still forming). `now_us` is MICROSECONDS-of-day so a
    /// `15:30:00.000001` instant is strictly after a whole-second close —
    /// exactly the Python `datetime.time` comparison. Uses the UNGUARDED
    /// `get_session` (out-of-coverage fabrication is proven harmless there).
    pub fn daily_derive_cutoff(&self, ordinal: i32, now_us: u64, sel: u8) -> i32 {
        match self.get_session(ordinal, sel) {
            None => ordinal + 1,
            Some((_, _, close_s)) => {
                if now_us > u64::from(close_s) * 1_000_000 {
                    ordinal + 1
                } else {
                    ordinal
                }
            }
        }
    }
}

// ────────────────────────────────────────────────────────────────────────
// Module-level pure verdict functions (no engine state)
// ────────────────────────────────────────────────────────────────────────

/// Regular NSE/BSE session length in minutes (09:15–15:30). A structural
/// session LAW (not holiday data — D8 governs data); mirrored from
/// `nse.SESSION_DURATION_MINUTES` and pinned by the calendar golden.
pub const SESSION_DURATION_MINUTES: i64 = 375;

/// `expected_bars_per_day_for_tf` core: `ceil(375 / tf)`; `tf >= 375` → 1;
/// `tf < 1` → `-1` (the Python wrapper raises `ValueError` BEFORE calling —
/// the `-1` verdict is the defensive twin, never a fabricated count).
pub fn expected_bars_for_tf(tf_minutes: i64) -> i64 {
    if tf_minutes < 1 {
        return -1;
    }
    if tf_minutes >= SESSION_DURATION_MINUTES {
        return 1;
    }
    (SESSION_DURATION_MINUTES + tf_minutes - 1) / tf_minutes
}

/// `is_forced_exit_bar` core over seconds-of-day: a bar covering
/// `[open_s, open_s + tf·60)` force-exits when its close is STRICTLY after
/// the boundary. `tf < 1` → `-1` (Python raises before calling); else
/// `1`/`0`. Python drops sub-second components exactly the same way
/// (`t.hour*3600 + t.minute*60 + t.second`).
///
/// SATURATING arithmetic (the `forced_exit_seconds_of_close` precedent —
/// keeps the release `overflow-checks = true` / `panic = "abort"` profile
/// abort-free on junk input): for any `tf_minutes >= 1` whose `tf·60` (or
/// the `open_s +` sum) exceeds `i64::MAX`, the close saturates to
/// `i64::MAX`, which is still STRICTLY greater than every `u32` boundary —
/// so the verdict is `1`, bit-identical to the Python oracle's bigint
/// `open + tf*60 > boundary` (always true there too). Parity therefore
/// holds for the WHOLE i64-crossable domain; a `tf_minutes` beyond
/// `i64::MAX` never reaches this fn (PyO3 raises `OverflowError` at
/// argument conversion — the Python seam routes that domain to the oracle
/// before calling, see `nse.py`'s `_RUST_TF_MAX` guard).
pub fn forced_exit_bar_verdict(open_s: u32, tf_minutes: i64, boundary_s: u32) -> i8 {
    if tf_minutes < 1 {
        return -1;
    }
    let close_s = i64::from(open_s).saturating_add(tf_minutes.saturating_mul(60));
    if close_s > i64::from(boundary_s) {
        1
    } else {
        0
    }
}

/// `forced_exit_seconds_for_session` core: the session close − 10 minutes,
/// in seconds-of-day. Saturating (a close before 00:10 cannot occur in
/// NSE/BSE data — the earliest in-scope close is 14:45; saturation keeps
/// the release `overflow-checks = true` profile abort-free on junk input).
pub fn forced_exit_seconds_of_close(close_s: u32) -> u32 {
    close_s.saturating_sub(600)
}

/// `session_last_bar_complete` core over SAME-DATE seconds-of-day:
/// `last_bar + 1 minute >= close`. The `+60` is load-bearing (the ingest
/// filter is half-open `[open, close)`, so a complete session's last stored
/// 1-minute bar is close − 1 minute). Sub-second components cannot flip
/// this law for a whole-second close (proof in the Python wrapper), and
/// the Python wrapper only delegates SAME-DATE pairs — cross-date inputs
/// stay on the Python `datetime` arm.
pub fn last_bar_complete(close_s: u32, last_bar_s: u32) -> bool {
    u64::from(last_bar_s) + 60 >= u64::from(close_s)
}

// ────────────────────────────────────────────────────────────────────────
// PyO3 wrappers
// ────────────────────────────────────────────────────────────────────────

/// The frozen PyO3 calendar-verdict engine — built ONCE per process by
/// `brutex.calendars.nse._build_rust_calendar` from the live module
/// constants. See the module docs for the verdict encoding.
#[pyclass(frozen, name = "CalendarVerdictEngine")]
pub struct CalendarVerdictEngine {
    inner: CalendarEngine,
}

#[pymethods]
impl CalendarVerdictEngine {
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(
        common_holidays: Vec<i32>,
        nse_only: Vec<i32>,
        bse_only: Vec<i32>,
        muhurat: Vec<(i32, u32, u32)>,
        special_weekend: Vec<(i32, u32, u32)>,
        extended: Vec<(i32, u32, u32)>,
        coverage_first_year: i32,
        coverage_last_year: i32,
        session_open_s: u32,
        session_close_s: u32,
    ) -> Self {
        Self {
            inner: CalendarEngine::new(
                common_holidays,
                nse_only,
                bse_only,
                muhurat,
                special_weekend,
                extended,
                coverage_first_year,
                coverage_last_year,
                session_open_s,
                session_close_s,
            ),
        }
    }

    /// Unguarded `is_holiday` (silent `False` out of coverage).
    fn is_holiday(&self, ordinal: i32, sel: u8) -> bool {
        self.inner.is_holiday(ordinal, sel)
    }

    /// `is_holiday_checked` triage code (0/1/2 — 2 = RAISE).
    fn is_holiday_checked(&self, ordinal: i32, sel: u8) -> i8 {
        self.inner.is_holiday_checked(ordinal, sel)
    }

    /// `is_weekend` twin.
    fn is_weekend(&self, ordinal: i32) -> bool {
        self.inner.is_weekend(ordinal)
    }

    /// Unguarded `is_trading_day`.
    fn is_trading_day(&self, ordinal: i32, sel: u8) -> bool {
        self.inner.is_trading_day(ordinal, sel)
    }

    /// `is_trading_day_checked` triage code (0/1/2 — 2 = RAISE).
    fn is_trading_day_checked(&self, ordinal: i32, sel: u8) -> i8 {
        self.inner.is_trading_day_checked(ordinal, sel)
    }

    /// `has_muhurat_session` twin.
    fn has_muhurat(&self, ordinal: i32) -> bool {
        self.inner.has_muhurat(ordinal)
    }

    /// `is_special_weekend_session` twin.
    fn is_special_weekend(&self, ordinal: i32) -> bool {
        self.inner.is_special_weekend(ordinal)
    }

    /// Unguarded `get_session` → `(type, open_s, close_s)` or `None`.
    fn get_session(&self, ordinal: i32, sel: u8) -> Option<(u8, u32, u32)> {
        self.inner.get_session(ordinal, sel)
    }

    /// `get_session_checked` → `(verdict, type, open_s, close_s)`.
    fn get_session_checked(&self, ordinal: i32, sel: u8) -> (i8, u8, u32, u32) {
        self.inner.get_session_checked(ordinal, sel)
    }

    /// `previous_trading_session` → `(1, prev)` | `(2, raising_probe)`.
    fn previous_trading_session(&self, ordinal: i32, sel: u8, observed: Vec<i32>) -> (i8, i32) {
        let observed: HashSet<i32> = observed.into_iter().collect();
        self.inner.previous_trading_session(ordinal, sel, &observed)
    }

    /// Unguarded `trading_days_between` (inclusive).
    fn trading_days_between(&self, start: i32, end: i32, sel: u8) -> Vec<i32> {
        self.inner.trading_days_between(start, end, sel)
    }

    /// `trading_days_between_checked` → `(verdict, days, offending)`.
    fn trading_days_between_checked(&self, start: i32, end: i32, sel: u8) -> (i8, Vec<i32>, i32) {
        self.inner.trading_days_between_checked(start, end, sel)
    }

    /// `daily_derive_cutoff` → the cutoff ORDINAL (strict `>` at close).
    fn daily_derive_cutoff(&self, ordinal: i32, now_us: u64, sel: u8) -> i32 {
        self.inner.daily_derive_cutoff(ordinal, now_us, sel)
    }
}

/// `expected_bars_per_day_for_tf` verdict (`-1` = the Python-side raise).
#[pyfunction(name = "expected_bars_per_day_for_tf")]
pub fn expected_bars_per_day_for_tf_py(tf_minutes: i64) -> i64 {
    expected_bars_for_tf(tf_minutes)
}

/// `is_forced_exit_bar` verdict over seconds-of-day (`-1` = the raise).
#[pyfunction(name = "is_forced_exit_bar_s")]
pub fn is_forced_exit_bar_s_py(open_s: u32, tf_minutes: i64, boundary_s: u32) -> i8 {
    forced_exit_bar_verdict(open_s, tf_minutes, boundary_s)
}

/// `forced_exit_seconds_for_session` core (close − 600 s).
#[pyfunction(name = "forced_exit_seconds_s")]
pub fn forced_exit_seconds_s_py(close_s: u32) -> u32 {
    forced_exit_seconds_of_close(close_s)
}

/// `session_last_bar_complete` core (same-date seconds-of-day law).
#[pyfunction(name = "session_last_bar_complete_s")]
pub fn session_last_bar_complete_s_py(close_s: u32, last_bar_s: u32) -> bool {
    last_bar_complete(close_s, last_bar_s)
}

// ────────────────────────────────────────────────────────────────────────
// In-crate tests (pure engine — the full 2,557-day parity proof lives in
// tests/calendar_verdict_golden.rs + the Python golden/property suites)
// ────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    /// 2026-07-06 (a regular Monday) as a Python proleptic ordinal.
    const MON_2026_07_06: i32 = 739_803;
    /// 2026-01-26 Republic Day (Monday holiday).
    const REPUBLIC_2026: i32 = 739_642;
    /// 2025-10-21 Diwali Muhurat (Tuesday; also a listed holiday).
    const MUHURAT_2025: i32 = 739_545;
    /// 2025-02-01 Budget Saturday (special weekend session).
    const BUDGET_SAT_2025: i32 = 739_283;
    /// 2027-01-01 (Friday, out of coverage).
    const OOC_FRI_2027: i32 = 739_982;
    /// 2026-12-31 (Thursday, last covered trading day).
    const THU_2026_12_31: i32 = 739_981;
    /// 2021-02-24 (Wednesday) — the NSE exchange-outage EXTENDED session
    /// (close 17:00 IST; DEC-EXTENDED-SESSION-OUTAGE-001).
    const OUTAGE_WED_2021: i32 = 737_845;

    fn engine() -> CalendarEngine {
        CalendarEngine::new(
            vec![REPUBLIC_2026, MUHURAT_2025],
            vec![],
            vec![],
            vec![(MUHURAT_2025, 49_500, 53_100)], // 13:45–14:45
            vec![(BUDGET_SAT_2025, 33_300, 55_800)],
            vec![(OUTAGE_WED_2021, 33_300, 61_200)], // 09:15–17:00
            2020,
            2026,
            33_300, // 09:15
            55_800, // 15:30
        )
    }

    #[test]
    fn weekday_pins() {
        assert_eq!(weekday_of_ordinal(1), 0); // 0001-01-01 is a Monday
        assert_eq!(weekday_of_ordinal(MON_2026_07_06), 0);
        assert_eq!(weekday_of_ordinal(BUDGET_SAT_2025), 5); // Saturday
        assert_eq!(weekday_of_ordinal(OOC_FRI_2027), 4); // Friday
    }

    #[test]
    fn year_of_ordinal_pins() {
        assert_eq!(year_of_ordinal(1), 1); // 0001-01-01
        assert_eq!(year_of_ordinal(719_163), 1970); // 1970-01-01
        assert_eq!(year_of_ordinal(737_425), 2020); // 2020-01-01
        assert_eq!(year_of_ordinal(737_424), 2019); // 2019-12-31
        assert_eq!(year_of_ordinal(THU_2026_12_31), 2026);
        assert_eq!(year_of_ordinal(OOC_FRI_2027), 2027);
        // Leap-day adjacency (2024-02-29 = 738_945).
        assert_eq!(year_of_ordinal(738_945), 2024);
    }

    #[test]
    fn trading_day_verdicts() {
        let eng = engine();
        assert!(eng.is_trading_day(MON_2026_07_06, 0));
        assert!(!eng.is_trading_day(REPUBLIC_2026, 0)); // holiday
        assert!(!eng.is_trading_day(MON_2026_07_06 + 5, 0)); // Saturday
        assert!(eng.is_trading_day(BUDGET_SAT_2025, 0)); // special weekend wins
        assert!(!eng.is_trading_day(MUHURAT_2025, 0)); // Muhurat is NOT a trading day
        // Out-of-coverage weekday: unguarded silently reads trading.
        assert!(eng.is_trading_day(OOC_FRI_2027, 0));
    }

    #[test]
    fn checked_triage_three_way() {
        let eng = engine();
        assert_eq!(eng.is_trading_day_checked(MON_2026_07_06, 0), VERDICT_TRUE);
        assert_eq!(eng.is_trading_day_checked(REPUBLIC_2026, 0), VERDICT_FALSE);
        assert_eq!(eng.is_trading_day_checked(OOC_FRI_2027, 0), VERDICT_RAISE);
        // Weekend out of coverage: data-complete FALSE, never a raise.
        assert_eq!(
            eng.is_trading_day_checked(OOC_FRI_2027 + 1, 0),
            VERDICT_FALSE
        );
        // Special weekend answers TRUE without raising even out of coverage
        // (synthetic: pretend coverage ends 2024 — rebuild).
        let narrow = CalendarEngine::new(
            vec![],
            vec![],
            vec![],
            vec![],
            vec![(BUDGET_SAT_2025, 33_300, 55_800)],
            vec![],
            2020,
            2024,
            33_300,
            55_800,
        );
        assert_eq!(
            narrow.is_trading_day_checked(BUDGET_SAT_2025, 0),
            VERDICT_TRUE
        );
    }

    #[test]
    fn muhurat_precedence_over_holiday() {
        let eng = engine();
        // Same-day holiday + Muhurat → session IS Muhurat with dict times…
        assert_eq!(
            eng.get_session(MUHURAT_2025, 0),
            Some((SESSION_MUHURAT, 49_500, 53_100))
        );
        assert_eq!(
            eng.get_session_checked(MUHURAT_2025, 0),
            (VERDICT_TRUE, SESSION_MUHURAT, 49_500, 53_100)
        );
        // …while is_trading_day stays False and the prev-session walk steps over it.
        assert!(!eng.is_trading_day(MUHURAT_2025, 0));
        let (verdict, prev) = eng.previous_trading_session(MUHURAT_2025 + 1, 0, &HashSet::new());
        assert_eq!(verdict, VERDICT_TRUE);
        assert_eq!(prev, MUHURAT_2025 - 1); // the Monday before (2025-10-20)
    }

    #[test]
    fn get_session_shapes() {
        let eng = engine();
        assert_eq!(
            eng.get_session(MON_2026_07_06, 0),
            Some((SESSION_REGULAR, 33_300, 55_800))
        );
        assert_eq!(eng.get_session(REPUBLIC_2026, 0), None);
        assert_eq!(
            eng.get_session(BUDGET_SAT_2025, 0),
            Some((SESSION_REGULAR, 33_300, 55_800))
        );
        // Out-of-coverage weekday FABRICATES a regular session (unguarded law)…
        assert_eq!(
            eng.get_session(OOC_FRI_2027, 0),
            Some((SESSION_REGULAR, 33_300, 55_800))
        );
        // …and the checked twin refuses instead.
        assert_eq!(
            eng.get_session_checked(OOC_FRI_2027, 0),
            (VERDICT_RAISE, 0, 0, 0)
        );
        assert_eq!(
            eng.get_session_checked(REPUBLIC_2026, 0),
            (VERDICT_FALSE, 0, 0, 0)
        );
    }

    #[test]
    fn extended_session_resolution() {
        // DEC-EXTENDED-SESSION-OUTAGE-001: 2021-02-24 (a plain Wednesday
        // trading day) carries the extended 09:15–17:00 close on BOTH
        // session twins — REGULAR type, the tier between special-weekend
        // and the regular arm (`_py_get_session` order).
        let eng = engine();
        assert_eq!(
            eng.get_session(OUTAGE_WED_2021, 0),
            Some((SESSION_REGULAR, 33_300, 61_200))
        );
        assert_eq!(
            eng.get_session_checked(OUTAGE_WED_2021, 0),
            (VERDICT_TRUE, SESSION_REGULAR, 33_300, 61_200)
        );
        // Trading-day verdicts are untouched by the extended tier (the date
        // is a plain weekday non-holiday either way).
        assert!(eng.is_trading_day(OUTAGE_WED_2021, 0));
        assert_eq!(eng.is_trading_day_checked(OUTAGE_WED_2021, 0), VERDICT_TRUE);
        // `daily_derive_cutoff` inherits the extended close through
        // `get_session`: at exactly 17:00 the session is still forming
        // (strict `>`); one microsecond later it derives.
        let extended_close_us = 61_200_u64 * 1_000_000;
        assert_eq!(
            eng.daily_derive_cutoff(OUTAGE_WED_2021, extended_close_us, 0),
            OUTAGE_WED_2021
        );
        assert_eq!(
            eng.daily_derive_cutoff(OUTAGE_WED_2021, extended_close_us + 1, 0),
            OUTAGE_WED_2021 + 1
        );
        // The data-complete checked resolution mirrors muhurat/special
        // weekend: a narrow-coverage engine still answers TRUE for the
        // extended date instead of raising.
        let narrow = CalendarEngine::new(
            vec![],
            vec![],
            vec![],
            vec![],
            vec![],
            vec![(OUTAGE_WED_2021, 33_300, 61_200)],
            2023,
            2026,
            33_300,
            55_800,
        );
        assert_eq!(
            narrow.get_session_checked(OUTAGE_WED_2021, 0),
            (VERDICT_TRUE, SESSION_REGULAR, 33_300, 61_200)
        );
    }

    #[test]
    fn previous_session_walk_and_observed_overlay() {
        let eng = engine();
        // From the Tuesday after the regular Monday: prev = Monday.
        let (v, prev) = eng.previous_trading_session(MON_2026_07_06 + 1, 0, &HashSet::new());
        assert_eq!((v, prev), (VERDICT_TRUE, MON_2026_07_06));
        // Observed overlay removes the Monday → the walk steps to Friday.
        let observed: HashSet<i32> = [MON_2026_07_06].into_iter().collect();
        let (v, prev) = eng.previous_trading_session(MON_2026_07_06 + 1, 0, &observed);
        assert_eq!((v, prev), (VERDICT_TRUE, MON_2026_07_06 - 3));
        // Coverage-floor abstain: walking back from 2020-01-01 hits the
        // out-of-coverage 2019 weekdays → verdict RAISE with the probe.
        let (v, probe) = eng.previous_trading_session(737_425, 0, &HashSet::new());
        assert_eq!(v, VERDICT_RAISE);
        assert_eq!(year_of_ordinal(probe), 2019);
    }

    #[test]
    fn trading_days_between_both_arms() {
        let eng = engine();
        // 2026-07-06 (Mon) .. 2026-07-12 (Sun): Mon–Fri are trading days.
        let days = eng.trading_days_between(MON_2026_07_06, MON_2026_07_06 + 6, 0);
        assert_eq!(days.len(), 5);
        assert_eq!(days[0], MON_2026_07_06);
        let (v, checked_days, offending) =
            eng.trading_days_between_checked(MON_2026_07_06, MON_2026_07_06 + 6, 0);
        assert_eq!((v, offending), (VERDICT_TRUE, 0));
        assert_eq!(checked_days, days);
        // A range crossing the coverage horizon raises at the FIRST
        // out-of-coverage weekday (2027-01-01, Friday).
        let (v, _, offending) =
            eng.trading_days_between_checked(THU_2026_12_31, OOC_FRI_2027 + 3, 0);
        assert_eq!(v, VERDICT_RAISE);
        assert_eq!(offending, OOC_FRI_2027);
        // Empty range → empty, verdict TRUE.
        let (v, days, _) = eng.trading_days_between_checked(10, 5, 0);
        assert_eq!((v, days.len()), (VERDICT_TRUE, 0));
    }

    #[test]
    fn daily_cutoff_strict_gt_at_close() {
        let eng = engine();
        let close_us = 55_800_u64 * 1_000_000;
        // AT exactly 15:30:00.000000 → still forming (strict >): today.
        assert_eq!(
            eng.daily_derive_cutoff(MON_2026_07_06, close_us, 0),
            MON_2026_07_06
        );
        // One microsecond later → tomorrow.
        assert_eq!(
            eng.daily_derive_cutoff(MON_2026_07_06, close_us + 1, 0),
            MON_2026_07_06 + 1
        );
        // Holiday / weekend: no session → tomorrow regardless of clock.
        assert_eq!(
            eng.daily_derive_cutoff(REPUBLIC_2026, 0, 0),
            REPUBLIC_2026 + 1
        );
        // Muhurat afternoon close 14:45: pre-close stays, post-close advances.
        let muhurat_close_us = 53_100_u64 * 1_000_000;
        assert_eq!(
            eng.daily_derive_cutoff(MUHURAT_2025, muhurat_close_us, 0),
            MUHURAT_2025
        );
        assert_eq!(
            eng.daily_derive_cutoff(MUHURAT_2025, muhurat_close_us + 1, 0),
            MUHURAT_2025 + 1
        );
    }

    #[test]
    fn exchange_selector_layering() {
        let nse_div = MON_2026_07_06 + 7; // a Monday, NSE-only holiday
        let eng = CalendarEngine::new(
            vec![REPUBLIC_2026],
            vec![nse_div],
            vec![],
            vec![],
            vec![],
            vec![],
            2020,
            2026,
            33_300,
            55_800,
        );
        assert!(eng.is_holiday(nse_div, 1)); // NSE sees it
        assert!(!eng.is_holiday(nse_div, 2)); // BSE does not
        assert!(eng.is_holiday(nse_div, 0)); // ALL = union sees it
        assert!(!eng.is_trading_day(nse_div, 1));
        assert!(eng.is_trading_day(nse_div, 2));
    }

    #[test]
    fn scalar_family_laws() {
        // Ceiling law landmarks (the frozen fixture cells).
        assert_eq!(expected_bars_for_tf(1), 375);
        assert_eq!(expected_bars_for_tf(5), 75);
        assert_eq!(expected_bars_for_tf(30), 13);
        assert_eq!(expected_bars_for_tf(60), 7);
        assert_eq!(expected_bars_for_tf(374), 2);
        assert_eq!(expected_bars_for_tf(375), 1);
        assert_eq!(expected_bars_for_tf(376), 1);
        assert_eq!(expected_bars_for_tf(0), -1);
        assert_eq!(expected_bars_for_tf(-5), -1);
        // Forced-exit strict > (boundary 15:20 = 55_200 s).
        assert_eq!(forced_exit_bar_verdict(55_140, 1, 55_200), 0); // close ON 15:20
        assert_eq!(forced_exit_bar_verdict(55_141, 1, 55_200), 1);
        assert_eq!(forced_exit_bar_verdict(54_900, 5, 55_200), 0); // 15:15+5m ON
        assert_eq!(forced_exit_bar_verdict(54_960, 5, 55_200), 1);
        assert_eq!(forced_exit_bar_verdict(0, 0, 55_200), -1);
        // Overflow inputs SATURATE, never abort (release overflow-checks =
        // true / panic = "abort"): tf·60 past i64::MAX clamps to i64::MAX,
        // which is still strictly > every u32 boundary — verdict 1, matching
        // the Python oracle's bigint arithmetic (slice-9 review, edge
        // MINOR-1: `10**18 * 60` previously SIGABRTed the whole process).
        assert_eq!(
            forced_exit_bar_verdict(33_300, 1_000_000_000_000_000_000, 55_200),
            1
        );
        assert_eq!(forced_exit_bar_verdict(33_300, i64::MAX, 55_200), 1);
        assert_eq!(
            forced_exit_bar_verdict(u32::MAX, i64::MAX / 60, u32::MAX),
            1
        );
        // Forced-exit seconds: 15:30 close → 15:20; Muhurat 14:45 → 14:35.
        assert_eq!(forced_exit_seconds_of_close(55_800), 55_200);
        assert_eq!(forced_exit_seconds_of_close(53_100), 52_500);
        assert_eq!(forced_exit_seconds_of_close(0), 0); // saturating, junk-safe
        // Last-bar-complete +1min >= law: 15:29 bar completes a 15:30 close.
        assert!(last_bar_complete(55_800, 55_740));
        assert!(!last_bar_complete(55_800, 55_679));
        assert!(last_bar_complete(55_800, 55_741));
    }
}
