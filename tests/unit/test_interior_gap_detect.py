"""Unit tests for the interior multi-session gap detector (DEC-INTERIOR-GAP-DETECT-001).

The pure classifier + the observe-only wiring must be false-positive-safe (a continuous
or sparse-but-continuous window NEVER flags) and fire only on a confirmed contiguous
interior hole bookended by real bars. 100% line + branch on
``brutex/data/interior_gap_detect.py``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import ClassVar

import pytest
from brutex.calendars.nse import IST
from brutex.data.interior_gap_detect import (
    INTERIOR_GAP,
    INTERIOR_GAP_WINDOW_ERROR_EVENT,
    INTERIOR_GAP_WINDOW_EVENT,
    NO_INTERIOR_GAP,
    InteriorGapVerdict,
    _largest_interior_session_gap,
    classify_interior_gap,
    classify_interior_gap_dates,
    observe_interior_gap,
)

# 04:05 UTC = 09:35 IST → inside the [09:15, 15:30) session for that trading date.
_SESSION_UTC_HOUR = 4
_SESSION_UTC_MIN = 5


def _ts(y: int, m: int, d: int) -> datetime:
    """An in-session UTC timestamp on the given IST trading date."""
    return datetime(y, m, d, _SESSION_UTC_HOUR, _SESSION_UTC_MIN, tzinfo=UTC)


def _weekday_run(start: datetime, sessions: int) -> list[datetime]:
    """A run of *sessions* consecutive WEEKDAY in-session timestamps from *start*."""
    out: list[datetime] = []
    cursor = start
    while len(out) < sessions:
        if cursor.weekday() < 5:  # Mon-Fri (weekends are non-sessions on the NSE grid)
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


class _SpyLog:
    """A duck-typed structlog logger capturing ``.warning(event, **fields)`` calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def warning(self, event: str, **fields: object) -> None:
        self.calls.append((event, fields))


class TestClassifyInteriorGap:
    def test_empty_is_too_few_sessions(self) -> None:
        v = classify_interior_gap(bar_timestamps=[])
        assert v.verdict == NO_INTERIOR_GAP
        assert v.reason == "too_few_sessions"
        assert v.first_bar_date is None
        assert v.last_bar_date is None
        assert v.largest_gap_sessions == 0
        assert not v.is_interior_gap()

    def test_single_session_is_too_few_sessions(self) -> None:
        v = classify_interior_gap(bar_timestamps=[_ts(2021, 3, 1), _ts(2021, 3, 1)])
        assert v.verdict == NO_INTERIOR_GAP
        assert v.reason == "too_few_sessions"
        assert v.first_bar_date == v.last_bar_date

    def test_contiguous_sessions_are_continuous(self) -> None:
        # March 2021 weekdays 1..12 (no holiday in that span) — fully continuous.
        ts = _weekday_run(_ts(2021, 3, 1), 10)
        v = classify_interior_gap(bar_timestamps=ts)
        assert v.verdict == NO_INTERIOR_GAP
        assert v.reason == "continuous"
        assert v.largest_gap_sessions == 0

    def test_small_interior_gap_within_tolerance_is_continuous(self) -> None:
        # Two bracketing sessions with a 2-session hole between (≤ default tolerance 5).
        ts = [_ts(2021, 3, 1), _ts(2021, 3, 4)]  # Mon then Thu — Tue+Wed missing (2 sessions)
        v = classify_interior_gap(bar_timestamps=ts)
        assert v.verdict == NO_INTERIOR_GAP
        assert v.reason == "continuous"
        assert v.largest_gap_sessions == 2

    def test_large_interior_gap_is_flagged(self) -> None:
        # A long contiguous hole bookended by real bars: Mar 1 then Mar 22 (>5 missing sessions).
        ts = [_ts(2021, 3, 1), _ts(2021, 3, 22)]
        v = classify_interior_gap(bar_timestamps=ts)
        assert v.verdict == INTERIOR_GAP
        assert v.reason == "interior_gap"
        assert v.largest_gap_sessions > 5
        assert v.is_interior_gap()
        assert v.first_bar_date is not None and v.first_bar_date.isoformat() == "2021-03-01"
        assert v.last_bar_date is not None and v.last_bar_date.isoformat() == "2021-03-22"

    def test_leading_trailing_density_does_not_create_false_interior_gap(self) -> None:
        # Dense at both ends, continuous through — no interior hole even though intraday-dense.
        ts = _weekday_run(_ts(2021, 3, 1), 15)
        v = classify_interior_gap(bar_timestamps=ts)
        assert v.verdict == NO_INTERIOR_GAP
        assert v.reason == "continuous"

    def test_tolerance_override_can_flag_a_smaller_hole(self) -> None:
        ts = [_ts(2021, 3, 1), _ts(2021, 3, 4)]  # 2-session hole
        v = classify_interior_gap(bar_timestamps=ts, tolerance_sessions=1)
        assert v.verdict == INTERIOR_GAP
        assert v.largest_gap_sessions == 2

    def test_out_of_calendar_coverage_abstains(self) -> None:
        # 2019 is outside the NSE holiday-list coverage span → abstain, never guess.
        ts = [_ts(2019, 3, 1), _ts(2019, 3, 22)]
        v = classify_interior_gap(bar_timestamps=ts)
        assert v.verdict == NO_INTERIOR_GAP
        assert v.reason == "out_of_calendar_coverage"
        assert v.largest_gap_sessions == 0

    def test_env_tolerance_used_when_override_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRUTEX_INTERIOR_GAP_TOLERANCE_SESSIONS", "1")
        v = classify_interior_gap(bar_timestamps=[_ts(2021, 3, 1), _ts(2021, 3, 4)])
        assert v.verdict == INTERIOR_GAP

    def test_env_tolerance_malformed_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRUTEX_INTERIOR_GAP_TOLERANCE_SESSIONS", "not-an-int")
        # A 2-session hole stays continuous at the default tolerance (5).
        v = classify_interior_gap(bar_timestamps=[_ts(2021, 3, 1), _ts(2021, 3, 4)])
        assert v.verdict == NO_INTERIOR_GAP

    def test_env_tolerance_nonpositive_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BRUTEX_INTERIOR_GAP_TOLERANCE_SESSIONS", "0")
        v = classify_interior_gap(bar_timestamps=[_ts(2021, 3, 1), _ts(2021, 3, 4)])
        assert v.verdict == NO_INTERIOR_GAP


class TestClassifyInteriorGapDates:
    """The extracted DATE-SET core (DEC-COLDSTART-SEED-INTERIOR-GAP-001) — the ONE
    interior-hole classifier shared by the fetch-time detector AND the cold-start
    ledger seed, driven here directly on covered dates."""

    def test_interior_gap_dates_flagged(self) -> None:
        # A >tolerance contiguous hole bracketed by covered sessions → INTERIOR_GAP.
        v = classify_interior_gap_dates(covered_dates=[date(2021, 3, 1), date(2021, 3, 22)])
        assert v.verdict == INTERIOR_GAP
        assert v.reason == "interior_gap"
        assert v.largest_gap_sessions > 5
        assert v.first_bar_date == date(2021, 3, 1)
        assert v.last_bar_date == date(2021, 3, 22)

    def test_continuous_dates_not_flagged(self) -> None:
        covered = [ts.astimezone(IST).date() for ts in _weekday_run(_ts(2021, 3, 1), 10)]
        v = classify_interior_gap_dates(covered_dates=covered)
        assert v.verdict == NO_INTERIOR_GAP
        assert v.reason == "continuous"
        assert v.largest_gap_sessions == 0

    def test_fewer_than_two_dates_is_too_few(self) -> None:
        assert classify_interior_gap_dates(covered_dates=[]).reason == "too_few_sessions"
        one = classify_interior_gap_dates(covered_dates=[date(2021, 3, 1)])
        assert one.verdict == NO_INTERIOR_GAP
        assert one.reason == "too_few_sessions"

    def test_out_of_calendar_year_abstains(self) -> None:
        # 2019 is outside the NSE holiday-list coverage → abstain, never guess.
        v = classify_interior_gap_dates(covered_dates=[date(2019, 3, 1), date(2019, 3, 22)])
        assert v.verdict == NO_INTERIOR_GAP
        assert v.reason == "out_of_calendar_coverage"

    def test_custom_tolerance_flags_smaller_hole(self) -> None:
        v = classify_interior_gap_dates(
            covered_dates=[date(2021, 3, 1), date(2021, 3, 4)], tolerance_sessions=1
        )
        assert v.verdict == INTERIOR_GAP
        assert v.largest_gap_sessions == 2

    def test_golden_timestamp_delegator_equals_date_core(self) -> None:
        # GOLDEN (COMMON, CLAUDE.md §11): the timestamp facade is EXACTLY the date
        # core over the IST-converted dates — byte-identical verdicts, so the
        # fetch-time detector and the cold-start seed can never drift.
        for ts_set in (
            [],
            [_ts(2021, 3, 1)],
            [_ts(2021, 3, 1), _ts(2021, 3, 4)],
            [_ts(2021, 3, 1), _ts(2021, 3, 22)],
            [_ts(2019, 3, 1), _ts(2019, 3, 22)],
            _weekday_run(_ts(2021, 3, 1), 10),
        ):
            assert classify_interior_gap(bar_timestamps=ts_set) == classify_interior_gap_dates(
                covered_dates=[t.astimezone(IST).date() for t in ts_set]
            )


class TestObserveInteriorGap:
    def test_warns_and_returns_verdict_on_interior_gap(self) -> None:
        log = _SpyLog()
        verdict = observe_interior_gap(
            log,
            bar_timestamps=[_ts(2021, 3, 1), _ts(2021, 3, 22)],
            symbol="NIFTY",
            interval="1minute",
            pull_id="pull-1",
        )
        assert verdict is not None and verdict.is_interior_gap()
        assert len(log.calls) == 1
        event, fields = log.calls[0]
        assert event == INTERIOR_GAP_WINDOW_EVENT
        assert fields["symbol"] == "'NIFTY'"
        assert fields["first_bar_date"] == "2021-03-01"
        assert fields["last_bar_date"] == "2021-03-22"
        gap = fields["largest_gap_sessions"]
        assert isinstance(gap, int) and gap > 5

    def test_silent_on_continuous_window(self) -> None:
        log = _SpyLog()
        verdict = observe_interior_gap(
            log,
            bar_timestamps=_weekday_run(_ts(2021, 3, 1), 10),
            symbol="NIFTY",
            interval="1minute",
            pull_id="pull-1",
        )
        assert verdict is not None and not verdict.is_interior_gap()
        assert log.calls == []

    def test_swallows_scan_error_and_returns_none(self) -> None:
        log = _SpyLog()

        class _Boom:
            """A timestamp-like that raises on ``.astimezone`` to force a scan failure."""

            def astimezone(self, *_a: object, **_k: object) -> object:
                raise RuntimeError("boom")

        verdict = observe_interior_gap(
            log,
            bar_timestamps=[_Boom()],  # type: ignore[list-item]
            symbol="NIFTY",
            interval="1minute",
            pull_id="pull-err",
        )
        assert verdict is None
        assert len(log.calls) == 1
        event, fields = log.calls[0]
        assert event == INTERIOR_GAP_WINDOW_ERROR_EVENT
        assert fields["pull_id"] == "pull-err"
        assert "boom" in str(fields["error"])


class TestLargestInteriorSessionGap:
    def test_fewer_than_two_covered_returns_zero(self) -> None:
        # The defensive guard inside the private helper (the public classifier short-circuits
        # earlier, but the helper itself must still be safe at the boundary).
        assert _largest_interior_session_gap([]) == 0
        assert _largest_interior_session_gap([_ts(2021, 3, 1).date()]) == 0

    def test_two_holes_bracketed_by_a_covered_session_resets_the_run(self) -> None:
        # Mar 1 (Mon) … Mar 4 (Thu) covered … then a LONG hole to Mar 22: the mid covered
        # session (Mar 4) RESETS the run, so the largest hole is the SECOND span, not the sum.
        covered = [_ts(2021, 3, 1).date(), _ts(2021, 3, 4).date(), _ts(2021, 3, 22).date()]
        gap = _largest_interior_session_gap(covered)
        # Mar 4 → Mar 22 has more missing sessions than Mar 1 → Mar 4 (2), so the run reset
        # is exercised and the larger second hole wins.
        assert gap > 2


def test_verdict_is_frozen() -> None:
    v = InteriorGapVerdict(
        verdict=NO_INTERIOR_GAP,
        first_bar_date=None,
        last_bar_date=None,
        largest_gap_sessions=0,
        reason="too_few_sessions",
    )
    with pytest.raises((AttributeError, TypeError)):
        v.verdict = INTERIOR_GAP  # type: ignore[misc]


# ─────────── observed-sessions overlay (DEC-CALENDAR-OBSERVED-SESSIONS-001) ───────────


class TestObservedOverlayMargin:
    """The overlay corrects the interior-hole MARGIN: an observed holiday is not a hole."""

    # Bars bracket a 6-trading-session interior hole (2026-05-05..2026-05-08 +
    # 2026-05-11..2026-05-12), strictly > the 5-session tolerance.
    _BRACKET: ClassVar[list[datetime]] = [_ts(2026, 5, 4), _ts(2026, 5, 13)]

    def test_overlaid_day_not_counted_in_hole(self) -> None:
        # FAIL-BEFORE/PASS-AFTER: 6 bar-less sessions flag; overlaying ONE of them
        # shrinks the hole to 5 <= tolerance -> NO_INTERIOR_GAP.
        from datetime import date

        flagged = classify_interior_gap(bar_timestamps=self._BRACKET)
        assert flagged.verdict == INTERIOR_GAP
        assert flagged.largest_gap_sessions == 6
        cleared = classify_interior_gap(
            bar_timestamps=self._BRACKET, observed=frozenset({date(2026, 5, 6)})
        )
        assert cleared.verdict == NO_INTERIOR_GAP
        assert cleared.largest_gap_sessions == 5

    def test_default_empty_overlay_byte_identical(self) -> None:
        default = classify_interior_gap(bar_timestamps=self._BRACKET)
        explicit = classify_interior_gap(bar_timestamps=self._BRACKET, observed=frozenset())
        assert default == explicit

    def test_observe_threads_observed(self) -> None:
        from datetime import date

        import structlog
        from structlog.testing import capture_logs

        with capture_logs() as logs:
            verdict = observe_interior_gap(
                structlog.get_logger("test"),
                bar_timestamps=self._BRACKET,
                symbol="NIFTY",
                interval="1minute",
                pull_id=None,
                observed=frozenset({date(2026, 5, 6)}),
            )
        assert verdict is not None
        assert verdict.verdict == NO_INTERIOR_GAP
        assert [e for e in logs if e["event"] == INTERIOR_GAP_WINDOW_EVENT] == []

    def test_overlay_remove_only_never_grows_gap(self) -> None:
        # Overlaying a WEEKEND / already-non-session date changes nothing.
        from datetime import date

        base = _largest_interior_session_gap([_ts(2026, 5, 4).date(), _ts(2026, 5, 13).date()])
        with_weekend = _largest_interior_session_gap(
            [_ts(2026, 5, 4).date(), _ts(2026, 5, 13).date()],
            frozenset({date(2026, 5, 9)}),  # a Saturday
        )
        assert with_weekend == base == 6
