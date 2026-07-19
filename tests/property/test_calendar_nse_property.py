"""Property tests for brutex.calendars.nse (DEC-CALENDAR-HARDEN-001).

Hypothesis-driven invariants for the forced-exit, bar-count, IST-offset and
trading-day rules. Lives under tests/property/ (the `property` CI job has
hypothesis); the per-module coverage gate is satisfied by the unit suite.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from fractions import Fraction

from brutex.calendars.nse import (
    IST,
    SESSION_DURATION_MINUTES,
    expected_bars_for_session,
    expected_bars_per_day_for_tf,
    get_session,
    is_forced_exit_bar,
    is_holiday,
    is_special_weekend_session,
    is_trading_day,
    is_weekend,
)
from hypothesis import given, settings
from hypothesis import strategies as st

_FORCED_EXIT_SECONDS = 15 * 3600 + 20 * 60


@given(tf=st.integers(min_value=1, max_value=10_000))
@settings(deadline=None)
def test_expected_bars_in_valid_range(tf: int) -> None:
    bars = expected_bars_per_day_for_tf(tf)
    assert 1 <= bars <= SESSION_DURATION_MINUTES


@given(tf=st.integers(min_value=1, max_value=1_000))
@settings(deadline=None)
def test_expected_bars_is_exact_ceiling(tf: int) -> None:
    """Session D PR-3 delta (DEC-CALENDAR-EXPECTED-BARS-DAILY-OPEN-001):
    pin the EXACT value, not just the 1..375 range — ``ceil(375 / tf)`` via
    ``Fraction`` (no float division as the oracle-of-the-oracle). Kills the
    restored-floor mutant the range property alone tolerates (``375 // tf``
    stays inside 1..375); an over-eager ``//tf + 1`` mutant already trips the
    range property at its tf=1 shrink (376 > 375) — this property kills it at
    EVERY tf, not just the boundary (review-precision wording; the original
    "both tolerated" claim overstated what the range property missed)."""
    assert expected_bars_per_day_for_tf(tf) == math.ceil(Fraction(SESSION_DURATION_MINUTES, tf))


@given(tf=st.integers(max_value=0))
@settings(deadline=None)
def test_expected_bars_rejects_non_positive(tf: int) -> None:
    try:
        expected_bars_per_day_for_tf(tf)
    except ValueError:
        return
    raise AssertionError("expected ValueError for tf <= 0")


@given(
    t=st.times(),
    tf=st.integers(min_value=1, max_value=600),
)
@settings(deadline=None)
def test_forced_exit_matches_oracle(t: time, tf: int) -> None:
    open_seconds = t.hour * 3600 + t.minute * 60 + t.second
    close_seconds = open_seconds + tf * 60
    assert is_forced_exit_bar(t, tf) == (close_seconds > _FORCED_EXIT_SECONDS)


@given(
    t=st.times(),
    tf=st.integers(min_value=1, max_value=599),
)
@settings(deadline=None)
def test_forced_exit_monotonic_in_timeframe(t: time, tf: int) -> None:
    # A wider window can only ever ADD a forced-exit, never remove one.
    if is_forced_exit_bar(t, tf):
        assert is_forced_exit_bar(t, tf + 1)


@given(d=st.dates(min_value=date(2020, 1, 1), max_value=date(2026, 12, 31)))
@settings(deadline=None)
def test_ist_offset_fixed_no_dst_in_scope(d: date) -> None:
    dt = datetime.combine(d, time(12, 0), tzinfo=IST)
    assert dt.utcoffset() == timedelta(hours=5, minutes=30)
    assert dt.dst() == timedelta(0)


@given(d=st.dates(min_value=date(2021, 1, 1), max_value=date(2026, 12, 31)))
@settings(deadline=None)
def test_trading_day_biconditional(d: date) -> None:
    if not is_weekend(d) and not is_holiday(d):
        assert is_trading_day(d)
    if (is_weekend(d) or is_holiday(d)) and not is_special_weekend_session(d):
        assert not is_trading_day(d)


# ── expected_bars_for_session — the session-aware twin (DEC-SESSION-DENSITY-CENSUS-001) ──

# The canonical 21-TF minute grid (1m + 19 derived intraday + the 375-min 1day law).
_TF_MINUTES_21 = (
    1,
    2,
    3,
    4,
    5,
    10,
    15,
    20,
    25,
    30,
    45,
    60,
    75,
    90,
    120,
    150,
    180,
    240,
    300,
    360,
    375,
)


@given(tf=st.integers(min_value=1, max_value=1_000))
@settings(deadline=None)
def test_session_aware_twin_equals_date_blind_law_on_regular_session(tf: int) -> None:
    """DEC-SESSION-DENSITY-CENSUS-001: for a REGULAR 375-minute session the
    session-aware twin equals the byte-untouched date-blind law at every tf —
    the DEC-CALENDAR-BARS-CEIL-001 resampler oracle is preserved."""
    session = get_session(date(2024, 1, 2))  # a regular Tuesday
    assert session is not None
    assert expected_bars_for_session(session, tf) == expected_bars_per_day_for_tf(tf)


def test_session_aware_twin_regular_parity_all_21_tfs() -> None:
    """The enumerated 21-TF grid (the lake vocabulary) — exact parity."""
    session = get_session(date(2024, 1, 2))
    assert session is not None
    for tf in _TF_MINUTES_21:
        assert expected_bars_for_session(session, tf) == expected_bars_per_day_for_tf(tf)


@given(tf=st.integers(min_value=1, max_value=1_000))
@settings(deadline=None)
def test_session_aware_twin_is_exact_ceiling_on_extended_session(tf: int) -> None:
    """The ceil law over the session's OWN duration — pinned on the 465-minute
    2021-02-24 extended day via ``Fraction`` (no float division)."""
    session = get_session(date(2021, 2, 24))
    assert session is not None
    assert session.duration_minutes == 465
    if tf < 465:
        assert expected_bars_for_session(session, tf) == math.ceil(Fraction(465, tf))
    else:
        assert expected_bars_for_session(session, tf) == 1


@given(tf=st.integers(max_value=0))
@settings(deadline=None)
def test_session_aware_twin_rejects_non_positive(tf: int) -> None:
    session = get_session(date(2024, 1, 2))
    assert session is not None
    try:
        expected_bars_for_session(session, tf)
    except ValueError:
        return
    raise AssertionError("expected ValueError for tf <= 0")
