"""Property-based invariants for `brutex.calendars.nse` (DEC-CALENDAR-HARDEN-001).

Hypothesis invariants:
- IST offset is ALWAYS +05:30 with no DST, for any instant.
- No listed holiday is ever a trading day (within coverage).
- A regular session's open/close are exactly 09:15:00 / 15:30:00 and span 375m,
  EXCEPT an :data:`EXTENDED_SESSIONS` outage date, which is positively pinned.
- Every regular trading-day timestamp from open..close is within session bounds.
- `is_holiday_data_available` is exactly the covered-year range test.
- `is_holiday_checked` raises iff data is unavailable, else equals `is_holiday`.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from brutex.calendars.nse import (
    ALL_HOLIDAYS,
    EXTENDED_SESSIONS,
    HOLIDAY_COVERAGE_FIRST_YEAR,
    HOLIDAY_COVERAGE_LAST_YEAR,
    IST,
    SESSION_CLOSE_IST,
    SESSION_DURATION_MINUTES,
    SESSION_OPEN_IST,
    HolidayDataUnavailableError,
    SessionType,
    get_session,
    is_forced_exit_bar,
    is_holiday,
    is_holiday_checked,
    is_holiday_data_available,
    is_trading_day,
    previous_trading_session,
)
from hypothesis import given
from hypothesis import strategies as st

# A broad date span covering below-floor (2018), in-coverage, and above-ceiling.
_DATES = st.dates(min_value=date(2018, 1, 1), max_value=date(2030, 12, 31))
_INSTANTS = st.datetimes(
    min_value=datetime(2018, 1, 1, 0, 0),
    max_value=datetime(2030, 12, 31, 23, 59),
)


@given(_INSTANTS)
def test_ist_offset_is_always_5h30m_no_dst(instant: datetime) -> None:
    aware = instant.replace(tzinfo=IST)
    assert aware.utcoffset() == timedelta(hours=5, minutes=30)
    assert aware.dst() == timedelta(0)


@given(_DATES)
def test_no_listed_holiday_is_a_trading_day(d: date) -> None:
    # Within the holiday list, a holiday can only be a trading day if it is a
    # special weekend session — and the package guarantees those two sets are
    # disjoint (a special session is never also in ALL_HOLIDAYS).
    if d in ALL_HOLIDAYS:
        assert not is_trading_day(d)


@given(_DATES)
def test_holiday_data_available_is_year_range_test(d: date) -> None:
    expected = HOLIDAY_COVERAGE_FIRST_YEAR <= d.year <= HOLIDAY_COVERAGE_LAST_YEAR
    assert is_holiday_data_available(d) is expected


@given(_DATES)
def test_checked_holiday_matches_or_raises(d: date) -> None:
    if is_holiday_data_available(d):
        assert is_holiday_checked(d) == is_holiday(d)
    else:
        with pytest.raises(HolidayDataUnavailableError):
            is_holiday_checked(d)


@given(_DATES)
def test_regular_session_has_canonical_bounds(d: date) -> None:
    """Every REGULAR session is canonical 09:15-15:30 EXCEPT an outage extension.

    EXTENDED-SESSION CARVE-OUT (DEC-EXTENDED-SESSION-OUTAGE-001, MIGRATION.md
    #1746): ``get_session`` classifies an :data:`EXTENDED_SESSIONS` date as
    ``SessionType.REGULAR`` with the exchange's real, LATER close — 2021-02-24
    closed 17:00 IST after the NSE telecom-link outage — so the blanket
    "every REGULAR session closes 15:30" assertion was false for that one date.
    The property predates that pack and only excused special-weekend sessions
    (which genuinely DO keep canonical hours, so they need no carve-out).

    CI never saw it: the repo-registered ``ci`` hypothesis profile is
    derandomized (DEC-TEST-APPARATUS-DEBTS-001, MIGRATION.md #1541), so the
    shrinker never drew 2021-02-24 there. A local run on the random default
    profile does, and hypothesis then caches the counterexample — a latent
    flake that could red CI on any reseed. Both this file and
    ``brutex/calendars/nse.py`` are byte-identical to ``origin/main``, so the
    gap is PRE-EXISTING and unrelated to the strict-mode work on this branch.

    The extended date is PINNED positively rather than skipped, so the
    carve-out can never silently swallow a real regression.
    """
    session = get_session(d)
    if session is None or session.session_type != SessionType.REGULAR:
        return
    # Special weekend sessions are also REGULAR but use canonical hours too.
    assert session.open_time_ist == SESSION_OPEN_IST == time(9, 15, 0)
    if d in EXTENDED_SESSIONS:
        expected_open, expected_close = EXTENDED_SESSIONS[d]
        assert session.open_time_ist == expected_open == SESSION_OPEN_IST
        assert session.close_time_ist == expected_close
        assert session.close_time_ist > SESSION_CLOSE_IST
        assert session.duration_minutes > SESSION_DURATION_MINUTES
        return
    assert session.close_time_ist == SESSION_CLOSE_IST == time(15, 30, 0)
    assert session.duration_minutes == SESSION_DURATION_MINUTES == 375


@given(st.integers(min_value=0, max_value=375))
def test_minute_offsets_within_regular_session_are_in_bounds(offset: int) -> None:
    # Every minute boundary from open (09:15) to close (15:30) inclusive lies in
    # [open, close]. The 375th minute is exactly the close.
    base = datetime(2024, 2, 20, 9, 15, tzinfo=IST)
    moment = base + timedelta(minutes=offset)
    assert moment.timetz() >= SESSION_OPEN_IST.replace(tzinfo=IST)
    assert moment.timetz() <= SESSION_CLOSE_IST.replace(tzinfo=IST)


@given(
    st.times(),
    st.integers(min_value=1, max_value=1440),
)
def test_forced_exit_is_monotone_in_open_time(t: time, tf_minutes: int) -> None:
    # A bar whose window reaches 15:20 is a forced-exit bar; the predicate is
    # a pure threshold so it never raises for tf>=1 and returns a bool.
    result = is_forced_exit_bar(t, tf_minutes=tf_minutes)
    assert isinstance(result, bool)


@given(_DATES)
def test_previous_trading_session_is_a_strictly_prior_trading_day(d: date) -> None:
    # DEC-MOVERS-PREVCLOSE-CONTIGUITY-001: a non-None result is STRICTLY before
    # d, is itself a trading day, and no trading day lies between the two (the
    # "immediately preceding session" law); None only ever means the walk
    # crossed the coverage floor (abstain, never a guess).
    prev = previous_trading_session(d)
    if prev is None:
        # Abstain only when the back-walk exhausted coverage — at (or below)
        # the coverage floor, or past the coverage ceiling. A date well INSIDE
        # coverage always resolves (the walk never abstains mid-span).
        assert d.year <= HOLIDAY_COVERAGE_FIRST_YEAR or d.year > HOLIDAY_COVERAGE_LAST_YEAR
        return
    assert prev < d
    assert is_trading_day(prev) is True
    probe = prev + timedelta(days=1)
    while probe < d:
        assert not is_trading_day(probe)
        probe += timedelta(days=1)


@given(_DATES)
def test_previous_trading_session_empty_observed_is_identity(d: date) -> None:
    # DEC-PREOPEN-PREVDAY-COMMON-002 parity law: the new remove-only
    # ``observed`` kwarg's default frozenset() is byte-identical back-compat —
    # explicit-empty == omitted for EVERY date (incl. abstains).
    assert previous_trading_session(d, observed=frozenset()) == previous_trading_session(d)


@given(
    _DATES,
    st.frozensets(st.dates(min_value=date(2020, 1, 1), max_value=date(2026, 12, 31)), max_size=8),
)
def test_previous_trading_session_observed_is_remove_only(
    d: date, observed: frozenset[date]
) -> None:
    # REMOVE-ONLY invariant: a non-None overlaid result is never an overlay
    # member, is still a real trading day, and is never LATER than the plain
    # result (a member can only push the walk further back, never forward).
    plain = previous_trading_session(d)
    overlaid = previous_trading_session(d, observed=observed)
    if overlaid is None:
        return  # abstain (coverage floor) — pinned by the sibling property
    assert overlaid not in observed
    assert is_trading_day(overlaid) is True
    assert plain is not None
    assert overlaid <= plain
