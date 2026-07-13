"""Tests for ``brutex.data.today_completeness`` — the pre-settle TODAY gate classifier.

DEC-PULL-TODAY-COMPLETENESS-GATE-001. 100% line+branch STANDALONE on the module:
the covers-today predicate, the per-class criteria table (INDEX count+close-edge ·
CASH all-zero-volume shape · FUT OI-null/all-zero-volume shape + observe-only count
· CE/PE OI-null shape), the calendar arms (Muhurat 60-minute session, Budget
special weekend, weekend/holiday vacuous pass, out-of-coverage abstain), the IST
split law, the guarded fail-open wrapper with its capped events, and the fail-loud
env kill switch. Tests inject explicit ``clamp_today`` dates + ``env=`` mappings —
fully armed on both arms regardless of the suite-wide conftest kill switch.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from brutex.calendars.nse import SESSION_DURATION_MINUTES, get_session
from brutex.data import today_completeness as tc
from brutex.data.models import CandleBar
from brutex.data.today_completeness import (
    TODAY_COMPLETENESS_GATE_ENV,
    TODAY_INCOMPLETE,
    InstrumentClass,
    classify_today_completeness,
    observe_today_completeness,
    reset_today_gate_log_state,
    resolve_today_completeness_enabled,
    split_today_bars,
)
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs

_IST = ZoneInfo("Asia/Kolkata")

#: A regular NSE trading day squarely inside the 2020-2026 holiday coverage.
_TODAY = date(2021, 3, 5)  # Friday
#: The covers-today window end (end-exclusive ⇒ today + 1).
_END = _TODAY + timedelta(days=1)


def _bar(
    ts: datetime,
    *,
    volume: int = 1,
    oi: int | None = None,
) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=volume,
        open_interest=oi,
    )


def _minute(day: date, minute_index: int, *, volume: int = 1, oi: int | None = None) -> CandleBar:
    """The day's session bar at ``open + minute_index`` minutes (IST), stored UTC."""
    session = get_session(day)
    assert session is not None
    open_dt = datetime.combine(day, session.open_time_ist, tzinfo=_IST)
    return _bar((open_dt + timedelta(minutes=minute_index)).astimezone(UTC), volume=volume, oi=oi)


def _full_day(day: date, *, volume: int = 1, oi: int | None = None) -> list[CandleBar]:
    """Every session minute of *day* — the complete stored-day shape."""
    session = get_session(day)
    assert session is not None
    return [_minute(day, i, volume=volume, oi=oi) for i in range(session.duration_minutes)]


@pytest.fixture(autouse=True)
def _fresh_gate_log_state() -> None:
    """Reset the module's per-pull capped-event counters between tests."""
    reset_today_gate_log_state()


class _Log:
    """A recording duck-typed structlog stand-in (warning + info)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.events.append(("warning", event, fields))

    def info(self, event: str, **fields: Any) -> None:
        self.events.append(("info", event, fields))

    def names(self) -> list[str]:
        return [event for _lvl, event, _f in self.events]


def _classify(
    bars: Sequence[CandleBar],
    *,
    instrument_class: InstrumentClass,
    clamp_today: date = _TODAY,
    window_end: date = _END,
) -> tc.TodayCompletenessVerdict:
    return classify_today_completeness(
        bars_today=bars,
        clamp_today=clamp_today,
        window_end=window_end,
        instrument_class=instrument_class,
    )


class TestSplitTodayBars:
    def test_split_today_bars_ist_law(self) -> None:
        # A 09:15 IST bar is stored 03:45 UTC — it must split as TODAY (the
        # astimezone(IST).date() law), never as the prior UTC date.
        opening = _bar(datetime(2021, 3, 5, 3, 45, tzinfo=UTC))
        prior = _bar(datetime(2021, 3, 4, 4, 5, tzinfo=UTC))
        later = _bar(datetime(2021, 3, 6, 4, 5, tzinfo=UTC))
        split = split_today_bars([prior, opening, later], _TODAY)
        assert split.prior == [prior]
        assert split.today == [opening]
        assert split.later == [later]
        assert split.unchanged is False

    def test_unchanged_true_when_nothing_withheld(self) -> None:
        prior = _bar(datetime(2021, 3, 4, 4, 5, tzinfo=UTC))
        split = split_today_bars([prior], _TODAY)
        assert split.unchanged is True

    @given(
        st.permutations(
            [
                datetime(2021, 3, 4, 4, 5, tzinfo=UTC),
                datetime(2021, 3, 4, 9, 55, tzinfo=UTC),
                datetime(2021, 3, 5, 3, 45, tzinfo=UTC),
                datetime(2021, 3, 5, 9, 59, tzinfo=UTC),
                datetime(2021, 3, 6, 4, 5, tzinfo=UTC),
            ]
        )
    )
    def test_partition_law_permutation_free(self, order: list[datetime]) -> None:
        # prior ∪ today ∪ later is EXACTLY the input multiset, whatever the
        # input order — the split is a pure partition, never a filter.
        bars = [_bar(ts) for ts in order]
        split = split_today_bars(bars, _TODAY)
        assert len(split.prior) + len(split.today) + len(split.later) == len(bars)
        assert sorted(b.timestamp for b in split.prior + split.today + split.later) == sorted(
            b.timestamp for b in bars
        )
        assert all(b.timestamp.astimezone(_IST).date() < _TODAY for b in split.prior)
        assert all(b.timestamp.astimezone(_IST).date() == _TODAY for b in split.today)
        assert all(b.timestamp.astimezone(_IST).date() > _TODAY for b in split.later)


class TestIndexCriteria:
    def test_index_full_day_complete(self) -> None:
        verdict = _classify(_full_day(_TODAY), instrument_class=InstrumentClass.INDEX_SPOT)
        assert verdict.covers_today is True
        assert verdict.incomplete is False
        assert verdict.reason == tc.REASON_COMPLETE
        assert verdict.expected_minutes == SESSION_DURATION_MINUTES == 375
        assert verdict.distinct_minutes == 375
        assert verdict.last_bar_ist == "15:29"

    def test_index_count_short(self) -> None:
        bars = _full_day(_TODAY)[:-1]  # 374 distinct minutes
        verdict = _classify(bars, instrument_class=InstrumentClass.INDEX_SPOT)
        assert verdict.incomplete is True
        assert verdict.reason == tc.REASON_COUNT_SHORT
        assert verdict.distinct_minutes == 374

    def test_index_duplicate_minute_counts_distinct(self) -> None:
        # 375 physical rows but only 374 DISTINCT minute floors — the
        # distinct-count teeth (a duplicated minute can never fake a full day).
        bars = _full_day(_TODAY)[:-1]
        bars.append(_minute(_TODAY, 0))  # duplicate the opening minute
        assert len(bars) == 375
        verdict = _classify(bars, instrument_class=InstrumentClass.INDEX_SPOT)
        assert verdict.incomplete is True
        assert verdict.reason == tc.REASON_COUNT_SHORT
        assert verdict.distinct_minutes == 374

    def test_index_count_excess(self) -> None:
        bars = _full_day(_TODAY)
        # One extra distinct minute floor past the session grid (the session
        # filter would normally drop it — this pins the defensive arm).
        bars.append(_bar(datetime.combine(_TODAY, time(15, 45), tzinfo=_IST).astimezone(UTC)))
        verdict = _classify(bars, instrument_class=InstrumentClass.INDEX_SPOT)
        assert verdict.incomplete is True
        assert verdict.reason == tc.REASON_COUNT_EXCESS

    def test_index_last_bar_incomplete_unreachable_at_full_count_so_count_wins(self) -> None:
        # A 374-bar day ending 14:57: BOTH the count and the close edge fail —
        # the documented reason precedence is count FIRST (a short day cannot
        # prove its close edge; a full-count day with a bad last bar is
        # impossible by distinctness over the [open, close) grid).
        bars = [_minute(_TODAY, i) for i in range(343)]  # ends 14:57 IST
        verdict = _classify(bars, instrument_class=InstrumentClass.INDEX_SPOT)
        assert verdict.incomplete is True
        assert verdict.reason == tc.REASON_COUNT_SHORT
        assert verdict.last_bar_ist == "14:57"

    def test_index_last_bar_incomplete(self) -> None:
        # 375 DISTINCT minute floors whose LAST is 15:28 (two bars share no
        # minute; one interior minute is replaced by a pre-open leak at 09:14
        # — impossible post-session-filter, but the classifier is pure and the
        # close-edge law must have its own teeth): count passes, close edge fails.
        bars = [_minute(_TODAY, i) for i in range(374)]  # 09:15..15:28
        bars.append(_bar(datetime.combine(_TODAY, time(9, 14), tzinfo=_IST).astimezone(UTC)))
        verdict = _classify(bars, instrument_class=InstrumentClass.INDEX_SPOT)
        assert verdict.distinct_minutes == 375
        assert verdict.incomplete is True
        assert verdict.reason == tc.REASON_LAST_BAR_INCOMPLETE
        assert verdict.last_bar_ist == "15:28"

    def test_index_zero_today_bars_fails_via_count(self) -> None:
        verdict = _classify([], instrument_class=InstrumentClass.INDEX_SPOT)
        assert verdict.incomplete is True
        assert verdict.reason == tc.REASON_COUNT_SHORT
        assert verdict.distinct_minutes == 0
        assert verdict.last_bar_ist is None

    @given(st.sets(st.integers(min_value=0, max_value=374), min_size=1, max_size=374))
    def test_index_strict_subset_always_count_short(self, minutes: set[int]) -> None:
        # ANY strict subset of the 375-minute grid is count_short — adding
        # missing minutes monotonically approaches the full grid, which passes
        # (test_index_full_day_complete); a failing set can never flip to a
        # DIFFERENT failure class by adding a missing minute.
        bars = [_minute(_TODAY, i) for i in sorted(minutes)]
        verdict = _classify(bars, instrument_class=InstrumentClass.INDEX_SPOT)
        assert verdict.incomplete is True
        assert verdict.reason == tc.REASON_COUNT_SHORT


class TestCalendarArms:
    def test_muhurat_2025_expected_60(self) -> None:
        muhurat = date(2025, 10, 21)  # 13:45-14:45 IST — 60 minutes
        full = _full_day(muhurat)
        assert len(full) == 60
        verdict = _classify(
            full,
            instrument_class=InstrumentClass.INDEX_SPOT,
            clamp_today=muhurat,
            window_end=muhurat + timedelta(days=1),
        )
        assert verdict.incomplete is False
        assert verdict.expected_minutes == 60
        assert verdict.last_bar_ist == "14:44"

    def test_muhurat_59_bars_count_short(self) -> None:
        muhurat = date(2025, 10, 21)
        verdict = _classify(
            _full_day(muhurat)[:-1],
            instrument_class=InstrumentClass.INDEX_SPOT,
            clamp_today=muhurat,
            window_end=muhurat + timedelta(days=1),
        )
        assert verdict.incomplete is True
        assert verdict.reason == tc.REASON_COUNT_SHORT

    def test_muhurat_60_distinct_ending_early_last_bar_incomplete(self) -> None:
        muhurat = date(2025, 10, 21)
        bars = _full_day(muhurat)[:-1]  # 13:45..14:43 — 59 minutes
        bars.append(_bar(datetime.combine(muhurat, time(13, 44), tzinfo=_IST).astimezone(UTC)))
        verdict = _classify(
            bars,
            instrument_class=InstrumentClass.INDEX_SPOT,
            clamp_today=muhurat,
            window_end=muhurat + timedelta(days=1),
        )
        assert verdict.distinct_minutes == 60
        assert verdict.incomplete is True
        assert verdict.reason == tc.REASON_LAST_BAR_INCOMPLETE

    def test_budget_weekend_full_375(self) -> None:
        budget_sunday = date(2026, 2, 1)  # special weekend session, full hours
        verdict = _classify(
            _full_day(budget_sunday),
            instrument_class=InstrumentClass.INDEX_SPOT,
            clamp_today=budget_sunday,
            window_end=budget_sunday + timedelta(days=1),
        )
        assert verdict.incomplete is False
        assert verdict.expected_minutes == 375

    def test_non_trading_today_vacuous(self) -> None:
        saturday = date(2021, 3, 6)
        verdict = _classify(
            [],
            instrument_class=InstrumentClass.INDEX_SPOT,
            clamp_today=saturday,
            window_end=saturday + timedelta(days=1),
        )
        assert verdict.covers_today is True
        assert verdict.incomplete is False
        assert verdict.reason == tc.REASON_NON_TRADING

    def test_out_of_coverage_abstains(self) -> None:
        future = date(2027, 1, 4)  # a 2027 weekday — holiday table silent
        verdict = _classify(
            [],
            instrument_class=InstrumentClass.INDEX_SPOT,
            clamp_today=future,
            window_end=future + timedelta(days=1),
        )
        assert verdict.covers_today is True
        assert verdict.incomplete is False
        assert verdict.reason == tc.REASON_ABSTAIN_COVERAGE
        assert verdict.expected_minutes is None  # never a fabricated 375 demand

    def test_not_covering_inert(self) -> None:
        verdict = _classify(
            _full_day(_TODAY),
            instrument_class=InstrumentClass.INDEX_SPOT,
            window_end=_TODAY,  # end == today: does NOT cover today (end-exclusive)
        )
        assert verdict.covers_today is False
        assert verdict.incomplete is False
        assert verdict.reason == tc.REASON_NOT_COVERING


class TestFutCriteria:
    def test_fut_any_oi_null_fails(self) -> None:
        # ONE OI-None bar among an otherwise-settled full day fails — the
        # ANY-bar law (an all-bars mutant MUST fail this test: a progressively
        # settling mixed day would otherwise pass and freeze its null minutes).
        bars = _full_day(_TODAY, oi=7)
        bars[100] = _minute(_TODAY, 100, oi=None)
        verdict = _classify(bars, instrument_class=InstrumentClass.FUT)
        assert verdict.incomplete is True
        assert verdict.reason == tc.REASON_OI_NULL_SETTLING
        assert verdict.oi_null_count == 1

    def test_fut_all_zero_volume_fails(self) -> None:
        bars = [_minute(_TODAY, i, volume=0, oi=7) for i in range(200)]
        verdict = _classify(bars, instrument_class=InstrumentClass.FUT)
        assert verdict.incomplete is True
        assert verdict.reason == tc.REASON_VOLUME_ALL_ZERO
        assert verdict.zero_volume_count == 200

    def test_fut_sparse_settled_passes_with_count_observe(self) -> None:
        # 200 bars, OI non-null, MIXED volume (some genuine zero-volume printed
        # minutes) ⇒ complete + the observe-only count-shortfall flag (never a
        # demotion — a far-serial-month future is legitimately sparse).
        bars = [_minute(_TODAY, i, volume=(0 if i % 2 else 5), oi=7) for i in range(200)]
        verdict = _classify(bars, instrument_class=InstrumentClass.FUT)
        assert verdict.incomplete is False
        assert verdict.reason == tc.REASON_COMPLETE
        assert verdict.count_short_observed is True

    def test_fut_full_settled_day_no_count_observe(self) -> None:
        verdict = _classify(_full_day(_TODAY, oi=7), instrument_class=InstrumentClass.FUT)
        assert verdict.incomplete is False
        assert verdict.count_short_observed is False

    def test_fno_zero_today_bars_abstains(self) -> None:
        for cls in (InstrumentClass.FUT, InstrumentClass.OPTION):
            verdict = _classify([], instrument_class=cls)
            assert verdict.incomplete is False
            assert verdict.reason == tc.REASON_NO_TODAY_BARS


class TestOptionCriteria:
    def test_option_sparse_settled_passes(self) -> None:
        # 12 OI-non-null bars ⇒ complete — the illiquid-but-legit pin
        # (sparsity ≈ truncation, never demanded on options; #1031 stance).
        bars = [_minute(_TODAY, i * 20, volume=0, oi=3) for i in range(12)]
        verdict = _classify(bars, instrument_class=InstrumentClass.OPTION)
        assert verdict.incomplete is False
        assert verdict.reason == tc.REASON_COMPLETE

    def test_option_oi_null_fails(self) -> None:
        bars = [_minute(_TODAY, i, volume=5, oi=None) for i in range(12)]
        verdict = _classify(bars, instrument_class=InstrumentClass.OPTION)
        assert verdict.incomplete is True
        assert verdict.reason == tc.REASON_OI_NULL_SETTLING


class TestCashCriteria:
    def test_cash_all_zero_volume_fails(self) -> None:
        bars = [_minute(_TODAY, i, volume=0) for i in range(50)]
        verdict = _classify(bars, instrument_class=InstrumentClass.CASH_SPOT)
        assert verdict.incomplete is True
        assert verdict.reason == tc.REASON_VOLUME_ALL_ZERO

    def test_cash_mixed_volume_passes(self) -> None:
        # An illiquid microcap whose last trade printed mid-day: short + early
        # last bar are NOT demanded on CASH (the #1031 re-pull-storm lesson).
        bars = [_minute(_TODAY, i, volume=(3 if i == 0 else 0)) for i in range(50)]
        verdict = _classify(bars, instrument_class=InstrumentClass.CASH_SPOT)
        assert verdict.incomplete is False
        assert verdict.reason == tc.REASON_COMPLETE

    def test_cash_zero_today_bars_passes(self) -> None:
        verdict = _classify([], instrument_class=InstrumentClass.CASH_SPOT)
        assert verdict.incomplete is False
        assert verdict.reason == tc.REASON_NO_TODAY_BARS


class TestObserveWrapper:
    """The guarded fail-open wiring — inert arms, capped events, the returned pair."""

    def _observe(
        self,
        log: _Log,
        bars: Sequence[CandleBar],
        *,
        instrument_class: InstrumentClass = InstrumentClass.INDEX_SPOT,
        clamp_today: date | None = _TODAY,
        window_end: date = _END,
        interval: str = "1minute",
        raw_count: int | None = None,
        pull_id: str | None = "pull-1",
    ) -> tuple[tc.TodayBarSplit, tc.TodayCompletenessVerdict] | None:
        return observe_today_completeness(
            log,
            bars=bars,
            clamp_today=clamp_today,
            window_end=window_end,
            instrument_class=instrument_class,
            symbol="NIFTY",
            interval=interval,
            pull_id=pull_id,
            raw_count=len(bars) if raw_count is None else raw_count,
            # ``symbol`` deliberately COLLIDES with a wrapper-owned event key —
            # the tails' series field dicts do exactly this; the wrapper's own
            # value must win (never a duplicate-kwarg TypeError).
            fields={"window_start": "2021-03-04", "symbol": "SHADOWED"},
        )

    def test_clamp_today_none_inert(self) -> None:
        log = _Log()
        assert self._observe(log, _full_day(_TODAY)[:10], clamp_today=None) is None
        assert log.events == []

    def test_non_minute_interval_inert(self) -> None:
        log = _Log()
        assert self._observe(log, _full_day(_TODAY)[:10], interval="1day") is None
        assert log.events == []

    def test_not_covering_inert_classifier_not_consulted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []
        real = tc.classify_today_completeness

        def _spy(**kw: Any) -> tc.TodayCompletenessVerdict:
            calls.append(kw)
            return real(**kw)

        monkeypatch.setattr(tc, "classify_today_completeness", _spy)
        log = _Log()
        assert self._observe(log, _full_day(_TODAY)[:10], window_end=_TODAY) is None
        assert calls == []  # the non-today early-out never touches the classifier
        assert log.events == []

    def test_raw_empty_emits_today_empty_and_owns_nothing(self) -> None:
        # INDEX + FUT get the observe-only visibility WARN; the wrapper returns
        # None so the EMPTY machinery (which ignores ``degraded`` at raw 0)
        # stays the sole owner.
        log = _Log()
        assert (
            self._observe(log, [], raw_count=0, instrument_class=InstrumentClass.INDEX_SPOT) is None
        )
        assert self._observe(log, [], raw_count=0, instrument_class=InstrumentClass.FUT) is None
        assert log.names().count(tc.TODAY_EMPTY_EVENT) == 2

    def test_raw_empty_silent_for_cash_and_option(self) -> None:
        log = _Log()
        assert (
            self._observe(log, [], raw_count=0, instrument_class=InstrumentClass.CASH_SPOT) is None
        )
        assert self._observe(log, [], raw_count=0, instrument_class=InstrumentClass.OPTION) is None
        assert log.events == []

    def test_raw_empty_warn_capped(self) -> None:
        log = _Log()
        for _ in range(25):
            self._observe(log, [], raw_count=0, instrument_class=InstrumentClass.INDEX_SPOT)
        assert log.names().count(tc.TODAY_EMPTY_EVENT) == 20

    def test_abstain_coverage_info_once_per_pull(self) -> None:
        future = date(2027, 1, 4)
        log = _Log()
        for _ in range(3):
            result = self._observe(
                log, [], clamp_today=future, window_end=future + timedelta(days=1), raw_count=5
            )
            assert result is None
        assert log.names().count(tc.TODAY_GATE_ABSTAIN_EVENT) == 1

    def test_non_trading_and_no_today_bars_silent_none(self) -> None:
        saturday = date(2021, 3, 6)
        log = _Log()
        assert (
            self._observe(
                log, [], clamp_today=saturday, window_end=saturday + timedelta(days=1), raw_count=5
            )
            is None
        )
        assert self._observe(log, [], instrument_class=InstrumentClass.OPTION, raw_count=5) is None
        assert log.events == []

    def test_complete_day_returns_pair_no_events(self) -> None:
        log = _Log()
        bars = _full_day(_TODAY)
        result = self._observe(log, bars)
        assert result is not None
        split, verdict = result
        assert verdict.incomplete is False
        assert split.today == bars  # the pass path withholds nothing
        assert log.events == []  # silence = health; the SUCCESS settle is the signal

    def test_incomplete_emits_warn_with_withheld_count(self) -> None:
        log = _Log()
        prior = [_bar(datetime(2021, 3, 4, 4, 5, tzinfo=UTC))]
        bars = prior + _full_day(_TODAY)[:-1]
        result = self._observe(log, bars)
        assert result is not None
        split, verdict = result
        assert verdict.incomplete is True
        assert split.prior == prior
        warn = next(f for _lvl, e, f in log.events if e == tc.TODAY_INCOMPLETE_EVENT)
        assert warn["withheld_bars"] == 374
        assert warn["reason"] == tc.REASON_COUNT_SHORT
        assert warn["session_date"] == "2021-03-05"
        assert warn["instrument_class"] == "index_spot"
        assert warn["window_start"] == "2021-03-04"  # caller fields threaded
        assert warn["symbol"] == "NIFTY"  # the wrapper-owned key wins the collision

    def test_warn_cap_20_and_suppressed_summary(self) -> None:
        log = _Log()
        bars = _full_day(_TODAY)[:-1]
        for _ in range(25):
            result = self._observe(log, bars)
            assert result is not None and result[1].incomplete
        names = log.names()
        assert names.count(tc.TODAY_INCOMPLETE_EVENT) == 20
        assert names.count(tc.TODAY_INCOMPLETE_SUPPRESSED_EVENT) == 1
        summary = next(f for _lvl, e, f in log.events if e == tc.TODAY_INCOMPLETE_SUPPRESSED_EVENT)
        # Flushed on observation #21 — the counts are FLUSH-time counts, a
        # FLOOR on the pull's final total by contract (round-1 R3-MINOR-1
        # disclosure): after 25 demotions the summary still reads 1-of-21,
        # NOT 5-of-25 — the TRUE per-pull total is the window ledger's
        # FAILED + TODAY_INCOMPLETE rows, one per demoted window.
        assert summary["suppressed_count"] == 1
        assert summary["total_incomplete"] == 21
        # The internal per-pull counter keeps counting past the flush (the
        # "later demotions stay counted internally but unlogged" clause) —
        # only the LOG surface is a floor, never the count itself.
        assert tc._INCOMPLETE_COUNTS["pull-1"] == 25

    def test_fut_count_short_observe_only_info(self) -> None:
        log = _Log()
        bars = [_minute(_TODAY, i, volume=5, oi=7) for i in range(200)]
        result = self._observe(log, bars, instrument_class=InstrumentClass.FUT)
        assert result is not None
        assert result[1].incomplete is False  # never demotes
        info = next(f for _lvl, e, f in log.events if e == tc.TODAY_COUNT_SHORT_EVENT)
        assert info["demoted"] is False
        assert info["distinct_minutes"] == 200
        assert info["expected_minutes"] == 375

    def test_fut_count_short_info_capped(self) -> None:
        log = _Log()
        bars = [_minute(_TODAY, i, volume=5, oi=7) for i in range(10)]
        for _ in range(25):
            self._observe(log, bars, instrument_class=InstrumentClass.FUT)
        assert log.names().count(tc.TODAY_COUNT_SHORT_EVENT) == 20

    def test_fields_default_none(self) -> None:
        log = _Log()
        result = observe_today_completeness(
            log,
            bars=_full_day(_TODAY)[:-1],
            clamp_today=_TODAY,
            window_end=_END,
            instrument_class=InstrumentClass.INDEX_SPOT,
            symbol="NIFTY",
            interval="1minute",
            pull_id=None,  # no pull scope — dedup under the "" key
            raw_count=374,
        )
        assert result is not None and result[1].incomplete

    def test_observe_wrapper_fail_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(**_kw: Any) -> tc.TodayCompletenessVerdict:
            raise RuntimeError("classifier bug")

        monkeypatch.setattr(tc, "classify_today_completeness", _boom)
        log = _Log()
        result = self._observe(log, _full_day(_TODAY))
        assert result is None  # fail-open — the caller appends the original bars
        assert log.names() == [tc.TODAY_GATE_ERROR_EVENT]

    def test_observe_wrapper_logs_real_structlog(self) -> None:
        # The production tails pass a real structlog logger — prove the event
        # names land through capture_logs too (the ingest suites grep them).
        import structlog

        log = structlog.get_logger("test.today_gate")
        with capture_logs() as logs:
            observe_today_completeness(
                log,
                bars=_full_day(_TODAY)[:-1],
                clamp_today=_TODAY,
                window_end=_END,
                instrument_class=InstrumentClass.INDEX_SPOT,
                symbol="NIFTY",
                interval="1minute",
                pull_id="p",
                raw_count=374,
            )
        assert any(e["event"] == tc.TODAY_INCOMPLETE_EVENT for e in logs)


class TestResolveEnabled:
    def test_resolve_enabled_default_on(self) -> None:
        assert resolve_today_completeness_enabled(env={}) is True
        assert resolve_today_completeness_enabled(env={TODAY_COMPLETENESS_GATE_ENV: ""}) is True
        assert resolve_today_completeness_enabled(env={TODAY_COMPLETENESS_GATE_ENV: " ON "}) is True
        assert resolve_today_completeness_enabled(env={TODAY_COMPLETENESS_GATE_ENV: "yes"}) is True

    def test_resolve_off_tokens(self) -> None:
        for token in ("0", "off", "FALSE", " no "):
            assert (
                resolve_today_completeness_enabled(env={TODAY_COMPLETENESS_GATE_ENV: token})
                is False
            )

    def test_resolve_junk_raises_valueerror(self) -> None:
        with pytest.raises(ValueError, match=TODAY_COMPLETENESS_GATE_ENV):
            resolve_today_completeness_enabled(env={TODAY_COMPLETENESS_GATE_ENV: "banana"})

    def test_resolve_env_none_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TODAY_COMPLETENESS_GATE_ENV, "1")
        assert resolve_today_completeness_enabled() is True
        monkeypatch.setenv(TODAY_COMPLETENESS_GATE_ENV, "0")
        assert resolve_today_completeness_enabled() is False


class TestModuleConstants:
    def test_reason_tag_value(self) -> None:
        # The tag IS the persisted ledger last_error / the UI key — frozen.
        assert TODAY_INCOMPLETE == "TODAY_INCOMPLETE"

    def test_streak_exempt_registry_membership(self) -> None:
        from brutex.data.window_ledger import (
            DEMOTED_SETTLE_REASONS,
            STREAK_EXEMPT_DEGRADED_REASONS,
        )

        assert TODAY_INCOMPLETE in STREAK_EXEMPT_DEGRADED_REASONS
        assert TODAY_INCOMPLETE in DEMOTED_SETTLE_REASONS


class TestMutationWorkflowWiring:
    """Pin this module INTO ``mutation.yml``'s per-module zero-survivor loop.

    Round-1 review (R2-M3, MIGRATION.md #1755): the classifier is a pure
    criteria table whose membership/boundary mutants (the covers-today
    equality, the per-class any-vs-all folds, the streak-exempt reason
    token) are exactly the class the hand-applied ledger mutants proved
    killable — so the module:suite pair sits in the workflow's per-module
    ``mutmut`` loop, and this deterministic pin (the
    ``test_lake_stats_manifest.py`` / ``TestBenchWorkflowWiring`` precedent)
    fails if the pair is ever dropped. HONEST: it pins the WIRING only —
    the weekly run executes on its own schedule/push triggers.

    Pair format: the loop parses 3-field ``module:testfile:timeout`` pairs
    (DEC-MUTATION-RUNTIME-003, MIGRATION.md #1597) — the trailing ``:``
    (empty timeout) selects the exact historical runner; a 2-field form
    would mis-parse the TEST PATH as the timeout.
    """

    _WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"

    def test_module_paired_with_this_suite_in_the_mutmut_loop(self) -> None:
        text = self._WORKFLOW.read_text(encoding="utf-8")
        assert (
            '"brutex/data/today_completeness.py:tests/unit/test_today_completeness.py:"' in text
        ), "today_completeness.py is not wired into mutation.yml's per-module mutmut loop"
