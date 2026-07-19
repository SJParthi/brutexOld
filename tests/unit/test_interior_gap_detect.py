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
from brutex.calendars.nse import IST, is_trading_day
from brutex.data.interior_gap_detect import (
    INTERIOR_GAP,
    INTERIOR_GAP_WINDOW_ERROR_EVENT,
    INTERIOR_GAP_WINDOW_EVENT,
    NO_INTERIOR_GAP,
    InteriorGapVerdict,
    _largest_interior_session_gap,
    classify_interior_gap,
    classify_interior_gap_dates,
    interior_gap_runs,
    observe_interior_gap,
)
from hypothesis import given
from hypothesis import strategies as st

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


def _trading_days_from(seed: date, count: int) -> list[date]:
    """The first *count* real NSE trading days on/after *seed* (calendar-derived)."""
    out: list[date] = []
    cursor = seed
    while len(out) < count:
        if is_trading_day(cursor):
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def _trading_day_count(first: date, last: date) -> int:
    """NSE trading days in ``[first, last]`` inclusive (calendar-derived)."""
    return sum(
        1 for i in range((last - first).days + 1) if is_trading_day(first + timedelta(days=i))
    )


class TestInteriorGapRuns:
    """`interior_gap_runs` — the DATASET-wide confirmed-run API for the cold-start
    seed's continuity gate (DEC-DATA-HONESTY-ALLFILTERED-SEED-001)."""

    def test_confirmed_run_bounds_are_exact(self) -> None:
        t = _trading_days_from(date(2021, 6, 1), 8)
        covered = [t[0], t[7]]  # 6 interior bar-less sessions > tolerance 5
        runs = interior_gap_runs(covered_dates=covered)
        assert runs == ((t[1], t[6]),)

    def test_hole_equal_to_tolerance_is_not_confirmed(self) -> None:
        t = _trading_days_from(date(2021, 6, 1), 7)
        covered = [t[0], t[6]]  # exactly 5 interior sessions == tolerance → not a run
        assert interior_gap_runs(covered_dates=covered) == ()

    def test_multiple_holes_reported_in_date_order(self) -> None:
        t = _trading_days_from(date(2021, 6, 1), 20)
        covered = [t[0], t[7], t[8], t[16]]  # holes t1..t6 (6) and t9..t15 (7)
        runs = interior_gap_runs(covered_dates=covered)
        assert runs == ((t[1], t[6]), (t[9], t[15]))

    def test_sub_tolerance_blip_stays_unreported(self) -> None:
        t = _trading_days_from(date(2021, 6, 1), 11)
        covered = [t[0], t[3], t[10]]  # blip t1..t2 (2 ≤ 5); hole t4..t9 (6 > 5)
        assert interior_gap_runs(covered_dates=covered) == ((t[4], t[9]),)

    def test_too_few_covered_dates_returns_empty(self) -> None:
        assert interior_gap_runs(covered_dates=[]) == ()
        assert interior_gap_runs(covered_dates=[date(2021, 6, 1)]) == ()

    def test_out_of_calendar_coverage_abstains_none(self) -> None:
        # 2019 is outside the NSE holiday-list coverage → None (abstain, never
        # guess — the SAME rule as classify_interior_gap_dates), including when
        # only ONE end is out of coverage.
        assert interior_gap_runs(covered_dates=[date(2019, 3, 1), date(2019, 3, 22)]) is None
        assert interior_gap_runs(covered_dates=[date(2019, 12, 20), date(2021, 3, 22)]) is None

    def test_custom_tolerance_confirms_smaller_hole(self) -> None:
        t = _trading_days_from(date(2021, 6, 1), 4)
        covered = [t[0], t[3]]  # 2 interior sessions
        assert interior_gap_runs(covered_dates=covered, tolerance_sessions=1) == ((t[1], t[2]),)
        assert interior_gap_runs(covered_dates=covered) == ()  # default 5 → not confirmed

    def test_env_tunable_shared_with_classifier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ONE knob (CLAUDE.md §11 COMMON): the env tolerance moves BOTH the
        # fetch-time classifier and the seed-time runs API.
        monkeypatch.setenv("BRUTEX_INTERIOR_GAP_TOLERANCE_SESSIONS", "1")
        t = _trading_days_from(date(2021, 6, 1), 4)
        covered = [t[0], t[3]]
        assert interior_gap_runs(covered_dates=covered) == ((t[1], t[2]),)
        assert classify_interior_gap_dates(covered_dates=covered).is_interior_gap()

    def test_observed_overlay_shrinks_a_run(self) -> None:
        # An observed holiday (DEC-CALENDAR-OBSERVED-SESSIONS-001) is not a
        # session — remove-only: subtracting it can only SHRINK a run.
        t = _trading_days_from(date(2021, 6, 1), 4)
        covered = [t[0], t[3]]
        assert interior_gap_runs(covered_dates=covered, tolerance_sessions=1) == ((t[1], t[2]),)
        shrunk = interior_gap_runs(
            covered_dates=covered, tolerance_sessions=1, observed=frozenset({t[1]})
        )
        assert shrunk == ()  # the surviving 1-session run is within tolerance

    @given(
        offsets=st.sets(st.integers(min_value=0, max_value=90), min_size=0, max_size=10),
        tolerance=st.integers(min_value=0, max_value=6),
    )
    def test_property_runs_parity_with_largest_gap(self, offsets: set[int], tolerance: int) -> None:
        # PARITY (pins the shared-run-walker refactor): over in-coverage date sets,
        # (1) a confirmed run exists ⇔ the classifier flags INTERIOR_GAP at the
        # same tolerance; (2) each run's recomputed trading-session length is
        # > tolerance and its max equals `_largest_interior_session_gap` at
        # tolerance 0; (3) no run contains a covered TRADING day (a covered
        # NON-trading date — e.g. a weekend bar — never splits a run: only
        # trading sessions participate in the walk).
        covered = sorted(date(2021, 1, 4) + timedelta(days=o) for o in offsets)
        runs = interior_gap_runs(covered_dates=covered, tolerance_sessions=tolerance)
        assert runs is not None  # 2021 is fully in coverage — never an abstain
        verdict = classify_interior_gap_dates(covered_dates=covered, tolerance_sessions=tolerance)
        assert bool(runs) == verdict.is_interior_gap()
        covered_set = set(covered)
        for run_first, run_last in runs:
            assert covered[0] < run_first <= run_last < covered[-1]
            length = _trading_day_count(run_first, run_last)
            assert length > tolerance
            span = {run_first + timedelta(days=i) for i in range((run_last - run_first).days + 1)}
            assert not any(is_trading_day(d) for d in span & covered_set)
        all_runs = interior_gap_runs(covered_dates=covered, tolerance_sessions=0)
        assert all_runs is not None
        lengths = [_trading_day_count(f, last) for f, last in all_runs]
        assert max(lengths, default=0) == _largest_interior_session_gap(covered)


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

    def test_stale_overlay_on_covered_session_never_merges_runs(self) -> None:
        # FAIL-BEFORE/PASS-AFTER (DEC-SEED-OBSERVED-OVERLAY-001 review fold): a
        # STALE overlay entry over a since-BACKFILLED covered session (an entry
        # self-heals only when `brutex detect-session-drift` next re-runs, NOT
        # the moment bars appear, so ``observed ∩ covered != ∅`` IS reachable)
        # must NOT merge the two flanking sub-tolerance runs into one
        # super-tolerance hole — the covered session's bars are the heal
        # EVIDENCE and always break the run. Pre-fix the walker consulted the
        # overlay FIRST, skipped the covered day, and read one 8-session run
        # (> tolerance 5 → INTERIOR_GAP / a new seed refusal).
        t = _trading_days_from(date(2021, 6, 1), 11)
        covered = [t[0], t[5], t[10]]  # two flanking 4-session bar-less runs
        base = classify_interior_gap_dates(covered_dates=covered)  # tolerance 5
        assert base.verdict == NO_INTERIOR_GAP
        assert base.largest_gap_sessions == 4
        stale = classify_interior_gap_dates(covered_dates=covered, observed=frozenset({t[5]}))
        assert stale == base  # the stale entry is INERT — the runs did not merge to 8
        assert interior_gap_runs(covered_dates=covered, observed=frozenset({t[5]})) == ()

    def test_covered_static_nonsession_date_stays_inert(self) -> None:
        # BYTE-COMPAT: a bar on a STATIC non-session date (a legacy weekend
        # leak) still neither breaks nor extends a run — only the overlaid
        # covered-SESSION shape changed, never the static-calendar shape.
        t = _trading_days_from(date(2021, 6, 1), 8)
        saturday = date(2021, 6, 5)
        assert not is_trading_day(saturday)
        covered = [t[0], t[7]]  # 6 interior bar-less sessions
        with_leak = [t[0], saturday, t[7]]
        assert (
            _largest_interior_session_gap(with_leak) == _largest_interior_session_gap(covered) == 6
        )
