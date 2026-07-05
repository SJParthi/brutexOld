"""Unit + look-ahead tests for the rolling-weekly + weekly-static Fib anchors (PR-2).

Covers ADR-0012 D2/§4.5: trailing-5-session + last-completed-ISO-week anchors,
cold-start ``None`` (combo ineligible), Muhurat-session exclusion, and — the hard
part (§4.5 #13) — a Property P-06 look-ahead proof that the anchor for day D never
reads any bar dated >= D (driven through the ``LookAheadBarrier`` index accessor).
100% line + branch on ``brutex.indicators.weekly_anchors``.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from brutex.calendars.nse import has_muhurat_session, is_trading_day
from brutex.feed.errors import LookAheadError
from brutex.feed.lookahead import LookAheadBarrier
from brutex.indicators import weekly_anchors
from brutex.indicators.prev_day_ohlc import DailyCandle


def _trading_days(start: date, count: int) -> list[date]:
    """The next `count` trading days at or after `start` (skips weekends/holidays)."""
    out: list[date] = []
    d = start
    while len(out) < count:
        if is_trading_day(d) and not has_muhurat_session(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _candles(days: list[date]) -> dict[date, DailyCandle]:
    """Build a daily-candle dict with distinct, increasing H/L per day."""
    out: dict[date, DailyCandle] = {}
    for i, d in enumerate(days):
        base = 1_000_000 + i * 10_000
        out[d] = DailyCandle(
            day=d,
            open_paisa=base,
            high_paisa=base + 5_000,
            low_paisa=base - 5_000,
            close_paisa=base + 1_000,
        )
    return out


# ─────────────────────────── rolling-weekly ───────────────────────────


def test_rolling_weekly_anchor_uses_trailing_5_sessions() -> None:
    days = _trading_days(date(2024, 1, 2), 7)  # 7 sessions
    candles = _candles(days)
    today = days[6]
    anchor = weekly_anchors.rolling_weekly_anchor(today, candles)
    assert anchor is not None
    # The 5 sessions strictly before `today` are days[1..6).
    window = days[1:6]
    assert anchor.source_days == tuple(sorted(window))
    assert anchor.high_paisa == max(candles[d].high_paisa for d in window)
    assert anchor.low_paisa == min(candles[d].low_paisa for d in window)


def test_rolling_weekly_cold_start_returns_none() -> None:
    days = _trading_days(date(2024, 1, 2), 4)  # only 3 prior sessions before days[3]
    candles = _candles(days)
    assert weekly_anchors.rolling_weekly_anchor(days[3], candles) is None


def test_rolling_weekly_skips_missing_candle() -> None:
    # 6 sessions; drop one of the 5 prior-session candles before today=days[5] so
    # only 4 of the required 5 are available → cold-start None (the
    # `candle is None` continue path).
    days = _trading_days(date(2024, 1, 2), 6)
    candles = _candles(days)
    del candles[days[3]]
    assert weekly_anchors.rolling_weekly_anchor(days[5], candles) is None


def test_rolling_weekly_excludes_muhurat_session() -> None:
    # A Muhurat date is NOT a regular trading day (is_trading_day False), so the
    # calendar walk never visits it — it can never enter the trailing window even
    # if a candle is (wrongly) present for it (ADR-0012 §4.5 #7).
    from brutex.calendars.nse import MUHURAT_SESSIONS, is_trading_day

    muhurat_day = next(iter(MUHURAT_SESSIONS))
    assert not is_trading_day(muhurat_day)
    days = _trading_days(muhurat_day + timedelta(days=1), 6)
    candles = _candles(days)
    candles[muhurat_day] = DailyCandle(
        day=muhurat_day,
        open_paisa=9_000_000,
        high_paisa=9_999_999,
        low_paisa=1,
        close_paisa=9_000_000,
    )
    anchor = weekly_anchors.rolling_weekly_anchor(days[5], candles)
    assert anchor is not None
    assert muhurat_day not in anchor.source_days


def test_rolling_weekly_rejects_non_trading_today() -> None:
    # 2024-01-06 is a Saturday.
    with pytest.raises(ValueError, match="not a trading day"):
        weekly_anchors.rolling_weekly_anchor(date(2024, 1, 6), {})


def test_rolling_weekly_rejects_bad_sessions() -> None:
    today = _trading_days(date(2024, 1, 2), 1)[0]
    with pytest.raises(ValueError, match="sessions must be"):
        weekly_anchors.rolling_weekly_anchor(today, {}, sessions=0)


def test_rolling_weekly_custom_session_window() -> None:
    days = _trading_days(date(2024, 1, 2), 6)
    candles = _candles(days)
    anchor = weekly_anchors.rolling_weekly_anchor(days[5], candles, sessions=3)
    assert anchor is not None
    assert anchor.source_days == tuple(sorted(days[2:5]))


def test_rolling_weekly_no_prior_history_returns_none() -> None:
    # `today` with an EMPTY candle dict → find_prev_trading_day walks back but no
    # candle present → cold-start None (the `candle is None` / out-of-history path).
    days = _trading_days(date(2024, 1, 2), 1)
    assert weekly_anchors.rolling_weekly_anchor(days[0], {}) is None


def test_rolling_weekly_breaks_when_no_prior_trading_day(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the defensive `prev is None` break (no trading day in the lookback).
    today = _trading_days(date(2024, 1, 2), 1)[0]
    monkeypatch.setattr(
        weekly_anchors, "find_prev_trading_day", lambda _d, observed=frozenset(): None
    )
    assert weekly_anchors.rolling_weekly_anchor(today, {}) is None


# ─────────────────────────── weekly-static ───────────────────────────


def test_weekly_static_anchor_uses_last_completed_week() -> None:
    # Two full ISO weeks of trading days, today in the SECOND week.
    week1 = _trading_days(date(2024, 1, 2), 4)  # Tue-Fri of ISO week 1
    week2 = _trading_days(date(2024, 1, 8), 2)  # Mon-Tue of ISO week 2
    candles = _candles(week1 + week2)
    today = week2[1]
    anchor = weekly_anchors.weekly_static_anchor(today, candles)
    assert anchor is not None
    # The last completed week is week1.
    assert anchor.source_days == tuple(sorted(week1))
    assert anchor.high_paisa == max(candles[d].high_paisa for d in week1)
    assert anchor.low_paisa == min(candles[d].low_paisa for d in week1)


def test_weekly_static_frozen_across_current_week() -> None:
    week1 = _trading_days(date(2024, 1, 2), 4)
    week2 = _trading_days(date(2024, 1, 8), 3)
    candles = _candles(week1 + week2)
    a0 = weekly_anchors.weekly_static_anchor(week2[0], candles)
    a2 = weekly_anchors.weekly_static_anchor(week2[2], candles)
    assert a0 is not None and a2 is not None
    # Same frozen anchor for every session of the current week.
    assert (a0.high_paisa, a0.low_paisa) == (a2.high_paisa, a2.low_paisa)


def test_weekly_static_cold_start_returns_none() -> None:
    # Only the current week's candles → no completed prior week → None.
    week1 = _trading_days(date(2024, 1, 2), 3)
    candles = _candles(week1)
    assert weekly_anchors.weekly_static_anchor(week1[2], candles) is None


def test_weekly_static_rejects_non_trading_today() -> None:
    with pytest.raises(ValueError, match="not a trading day"):
        weekly_anchors.weekly_static_anchor(date(2024, 1, 6), {})


def test_weekly_static_stops_at_target_week_boundary() -> None:
    # THREE ISO weeks of data: today in week3 → the completed week is week2; the
    # walk must STOP at the week2→week1 boundary (the `prev_week != target_week`
    # break), so week1 highs never leak into the anchor.
    week1 = _trading_days(date(2024, 1, 2), 4)  # ISO week 1
    week2 = _trading_days(date(2024, 1, 8), 5)  # ISO week 2 (Mon-Fri)
    week3 = _trading_days(date(2024, 1, 15), 2)  # ISO week 3
    candles = _candles(week1 + week2 + week3)
    # Make a week1 day extreme so a leak would be visible.
    candles[week1[0]] = DailyCandle(
        day=week1[0], open_paisa=1, high_paisa=9_999_999, low_paisa=1, close_paisa=1
    )
    anchor = weekly_anchors.weekly_static_anchor(week3[1], candles)
    assert anchor is not None
    assert anchor.source_days == tuple(sorted(week2))
    assert anchor.high_paisa < 9_999_999  # week1 spike did not leak


def test_weekly_static_breaks_when_no_prior_trading_day(monkeypatch: pytest.MonkeyPatch) -> None:
    today = _trading_days(date(2024, 1, 2), 1)[0]
    monkeypatch.setattr(
        weekly_anchors, "find_prev_trading_day", lambda _d, observed=frozenset(): None
    )
    assert weekly_anchors.weekly_static_anchor(today, {}) is None


def test_weekly_static_guard_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the defensive `guard < max_steps` loop to exhaust: a
    # find_prev_trading_day that always returns a date IN the current ISO week →
    # every step `continue`s, never landing in a completed prior week, so the
    # guard runs out and the cold-start `None` is returned.
    today = _trading_days(date(2024, 1, 9), 1)[0]  # mid-week (Tue)
    monkeypatch.setattr(
        weekly_anchors, "find_prev_trading_day", lambda _d, observed=frozenset(): today
    )  # same-week forever
    assert weekly_anchors.weekly_static_anchor(today, {}) is None


def test_weekly_static_ignores_today_and_future() -> None:
    week1 = _trading_days(date(2024, 1, 2), 4)
    week2 = _trading_days(date(2024, 1, 8), 3)
    candles = _candles(week1 + week2)
    today = week2[0]
    # A future-dated candle must never be read.
    future = week2[2]
    anchor = weekly_anchors.weekly_static_anchor(today, candles)
    assert anchor is not None
    assert future not in anchor.source_days
    assert all(d < today for d in anchor.source_days)


# ─────────────────────────── Muhurat exclusion ───────────────────────────


def test_muhurat_session_excluded_automatically() -> None:
    # The Muhurat session is excluded from BOTH anchors automatically: it is not a
    # regular trading day, so find_prev_trading_day never visits it. Even an
    # extreme injected candle on the Muhurat date never enters either window.
    from brutex.calendars.nse import MUHURAT_SESSIONS, is_trading_day

    muhurat_day = next(iter(MUHURAT_SESSIONS))
    assert not is_trading_day(muhurat_day)
    # Build a week of real trading days straddling the muhurat date.
    days = _trading_days(muhurat_day - timedelta(days=10), 12)
    candles = _candles(days)
    candles[muhurat_day] = DailyCandle(
        day=muhurat_day, open_paisa=9_000_000, high_paisa=9_999_999, low_paisa=1, close_paisa=1
    )
    today = days[-1]
    r_anchor = weekly_anchors.rolling_weekly_anchor(today, candles)
    s_anchor = weekly_anchors.weekly_static_anchor(today, candles)
    assert r_anchor is not None
    assert muhurat_day not in r_anchor.source_days
    assert r_anchor.high_paisa < 9_999_999  # the muhurat spike never entered
    if s_anchor is not None:
        assert muhurat_day not in s_anchor.source_days


# ─────────────────────────── look-ahead (Property P-06) ───────────────────────────

#: Fixed epoch the barrier maps a date's ordinal against (a stable bar index).
_BARRIER_EPOCH = date(2023, 1, 1)


class _BarrierDict(dict[date, DailyCandle]):
    """A daily-candle dict that routes every key access through a
    :class:`LookAheadBarrier`, recording the accessed date's ORDINAL (offset from a
    fixed epoch) as the bar index.

    The barrier cursor sits at `today`'s ordinal index; any ``.get()`` of a date
    at index >= today's raises ``LookAheadError`` — the index-based P-06 proof that
    the anchor never reads today or any future date. Using the calendar ordinal
    (not a list position) means a date the backward walk visits but that is absent
    from the candle dict still maps to a well-defined index, so the proof covers
    EVERY date the algorithm touches, present or not.
    """

    def __init__(self, data: dict[date, DailyCandle], barrier: LookAheadBarrier) -> None:
        super().__init__(data)
        self._barrier = barrier

    def _index(self, key: date) -> int:
        return (key - _BARRIER_EPOCH).days

    def get(  # type: ignore[override]
        self, key: date, default: DailyCandle | None = None
    ) -> DailyCandle | None:
        self._barrier.access(self._index(key))
        return super().get(key, default)


def _barrier_setup(days: list[date], today: date) -> tuple[_BarrierDict, LookAheadBarrier, int]:
    candles = _candles(days)
    barrier = LookAheadBarrier(record=True)
    today_index = (today - _BARRIER_EPOCH).days
    # The cursor sits at today's ordinal index; only dates < today are readable.
    barrier.advance_to(today_index)
    return _BarrierDict(candles, barrier), barrier, today_index


def test_rolling_weekly_is_lookahead_clean() -> None:
    days = _trading_days(date(2024, 1, 2), 7)
    today = days[6]
    bdict, barrier, today_index = _barrier_setup(days, today)
    anchor = weekly_anchors.rolling_weekly_anchor(today, bdict)
    assert anchor is not None
    # No bar at index >= today's was ever touched.
    assert barrier.max_touched is not None
    assert barrier.max_touched < today_index


def test_weekly_static_is_lookahead_clean() -> None:
    week1 = _trading_days(date(2024, 1, 2), 4)
    week2 = _trading_days(date(2024, 1, 8), 3)
    days = week1 + week2
    today = week2[1]
    bdict, barrier, today_index = _barrier_setup(days, today)
    anchor = weekly_anchors.weekly_static_anchor(today, bdict)
    assert anchor is not None
    assert barrier.max_touched is not None
    assert barrier.max_touched < today_index


def test_barrier_rejects_a_deliberate_future_read() -> None:
    # Sanity: the barrier instrumentation actually CATCHES a future read, so the
    # two clean tests above are a real proof, not a vacuous one.
    days = _trading_days(date(2024, 1, 2), 5)
    today = days[2]
    bdict, _barrier, _ti = _barrier_setup(days, today)
    with pytest.raises(LookAheadError):
        bdict.get(days[4])  # days[4] is in the future relative to `today`


def test_fib_anchor_is_frozen_dataclass() -> None:
    anchor = weekly_anchors.FibAnchor(
        today=date(2024, 1, 5), high_paisa=10, low_paisa=5, source_days=()
    )
    with pytest.raises((AttributeError, TypeError)):
        anchor.high_paisa = 99  # type: ignore[misc]


# ─────────── observed-sessions overlay (DEC-CALENDAR-OBSERVED-SESSIONS-001) ───────────


def test_rolling_weekly_anchor_observed_subtraction() -> None:
    # An overlaid day is excluded from the trailing-5 collection — the window
    # extends one session earlier instead (remove-only).
    days = _trading_days(date(2026, 5, 4), 7)
    candles = _candles(days)
    today = days[6]
    overlaid = days[5]  # the most recent prior session becomes an observed holiday
    base = weekly_anchors.rolling_weekly_anchor(today, candles)
    shifted = weekly_anchors.rolling_weekly_anchor(today, candles, observed=frozenset({overlaid}))
    assert base is not None and shifted is not None
    assert overlaid in base.source_days
    assert overlaid not in shifted.source_days
    assert shifted.source_days == tuple(days[0:5])  # one session earlier on both ends


def test_weekly_static_anchor_observed_subtraction() -> None:
    # Anchor week = the last completed ISO week; an overlaid member of that week
    # cannot contribute to its High/Low.
    days = _trading_days(date(2026, 5, 4), 8)  # Mon..Fri (wk1) + Mon..Wed (wk2)
    candles = _candles(days)
    today = days[6]  # inside week 2
    overlaid = days[4]  # Friday of the completed week — the max-High holder
    base = weekly_anchors.weekly_static_anchor(today, candles)
    shifted = weekly_anchors.weekly_static_anchor(today, candles, observed=frozenset({overlaid}))
    assert base is not None and shifted is not None
    assert overlaid in base.source_days
    assert overlaid not in shifted.source_days
    assert shifted.high_paisa < base.high_paisa  # the overlaid day's high dropped out


def test_anchor_today_overlaid_raises_valueerror() -> None:
    days = _trading_days(date(2026, 5, 4), 6)
    candles = _candles(days)
    today = days[5]
    with pytest.raises(ValueError, match="not a trading day"):
        weekly_anchors.rolling_weekly_anchor(today, candles, observed=frozenset({today}))
    with pytest.raises(ValueError, match="not a trading day"):
        weekly_anchors.weekly_static_anchor(today, candles, observed=frozenset({today}))
