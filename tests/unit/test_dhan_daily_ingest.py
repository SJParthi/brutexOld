"""Tests for :mod:`brutex.adapters.dhan.daily_ingest` — the NATIVE-daily spot stage.

DEC-DHAN-NATIVE-DAILY-001 (implementing `DEC-SCOPE-MINIMAL-CORE-001`): Dhan's
1-day spot bars are pulled NATIVELY from the daily endpoint as their OWN stage
(never derived from 1-minute — the Groww law `DEC-1MINUTE-ONLY-DERIVE-1DAY-001`
stays Groww-only), clamped to the FIXED ``DHAN_DAILY_HISTORY_FLOOR`` (a PROJECT
floor — never the rolling intraday floor) + the shared `DEC-DHAN-TODAY-CLAMP-001`
prev-day end clamp, session-filtered by TRADING DATE (never wall clock), and
written ``interval="1day"`` through the SAME keep-first sink.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from brutex.adapters.dhan.constants import (
    DHAN_DAILY_HISTORY_FLOOR,
    DHAN_DAILY_INTERVAL,
)
from brutex.adapters.dhan.daily_ingest import (
    filter_daily_to_trading_dates,
    plan_dhan_daily_work,
    run_dhan_spot_daily,
)
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanBadRequestError,
    DhanThrottleError,
)
from brutex.adapters.dhan.ingest import STATUS_FAILED, STATUS_SUCCESS
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult
from structlog.testing import capture_logs

_FROZEN_TODAY = date(2022, 7, 1)


def _epoch(day: date) -> int:
    """The 09:15 IST instant of *day* as epoch seconds (03:45 UTC)."""
    return int(datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC).timestamp())


def _payload(*days: date) -> dict[str, object]:
    """One parallel-column daily payload holding one bar per *day* (no volume — IDX)."""
    n = len(days)
    return {
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.5] * n,
        "timestamp": [_epoch(d) for d in days],
    }


def _midnight_ist_bar(day: date, *, close: str = "100.5") -> CandleBar:
    """A daily bar stamped midnight-IST stored UTC (18:30 UTC of the prior day)."""
    from datetime import timedelta, timezone

    ist = timezone(timedelta(hours=5, minutes=30))
    ts = datetime(day.year, day.month, day.day, tzinfo=ist).astimezone(UTC)
    price = Decimal(close)
    return CandleBar(
        timestamp=ts,
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=0,
        open_interest=None,
    )


class _RecordingSink:
    def __init__(self) -> None:
        self.appends: list[dict[str, object]] = []

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        self.appends.append(
            {
                "exchange": exchange,
                "segment": segment,
                "symbol": symbol,
                "interval": interval,
                "n": len(bars),
                "bars": list(bars),
            }
        )
        return SinkAppendResult(
            partitions_written=1,
            bars_appended=len(bars),
            write_results=(),
            bars_net_new=len(bars),
        )


class _ScriptedDailyClient:
    """A fake DailyCandles surface driven by a per-security scripted queue."""

    def __init__(self, script: dict[int, list[object]]) -> None:
        self._script = script
        self.calls: list[dict[str, object]] = []

    def daily_candles(self, *, security_id: int, from_date: str, to_date: str) -> dict[str, object]:
        self.calls.append({"security_id": security_id, "from": from_date, "to": to_date})
        response = self._script[security_id].pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]


# ─────────────────────────────────────────────────────────────────────────────
# filter_daily_to_trading_dates — the DATE-level daily session filter
# ─────────────────────────────────────────────────────────────────────────────


class TestFilterDailyToTradingDates:
    def test_empty_input_is_empty_result(self) -> None:
        result = filter_daily_to_trading_dates([])
        assert result.kept == []
        assert result.dropped_non_trading_date == 0

    def test_trading_dates_kept_weekend_dropped_and_counted(self) -> None:
        wed = _midnight_ist_bar(date(2022, 6, 1))  # a regular NSE session
        sat = _midnight_ist_bar(date(2022, 6, 4))  # a weekend vendor leak
        result = filter_daily_to_trading_dates([wed, sat])
        assert result.kept == [wed]
        assert result.dropped_non_trading_date == 1
        assert result.dropped_off_session == 0  # no wall-clock judgment on this path

    def test_same_date_resolves_session_once_via_cache(self) -> None:
        # Two bars on the SAME IST date exercise the per-date cache-hit arm.
        first = _midnight_ist_bar(date(2022, 6, 1), close="100.5")
        second = _midnight_ist_bar(date(2022, 6, 1), close="200.5")
        result = filter_daily_to_trading_dates([first, second])
        assert result.kept == [first, second]

    def test_same_non_trading_date_cache_hit_still_drops(self) -> None:
        first = _midnight_ist_bar(date(2022, 6, 4))
        second = _midnight_ist_bar(date(2022, 6, 4), close="200.5")
        result = filter_daily_to_trading_dates([first, second])
        assert result.kept == []
        assert result.dropped_non_trading_date == 2

    def test_out_of_coverage_date_abstains_and_keeps(self) -> None:
        # 2027-01-05 (a Tuesday) is past the published holiday-list span — the
        # calendar cannot judge it, so the bar is KEPT (Golden Rule #1: never
        # drop real vendor data on ignorance).
        future = _midnight_ist_bar(date(2027, 1, 5))
        result = filter_daily_to_trading_dates([future])
        assert result.kept == [future]
        assert result.dropped_non_trading_date == 0


# ─────────────────────────────────────────────────────────────────────────────
# plan_dhan_daily_work — the FIXED daily floor + the shared prev-day end clamp
# ─────────────────────────────────────────────────────────────────────────────


class TestPlanDhanDailyWork:
    def test_fixed_floor_clamps_start_with_one_info(self) -> None:
        with capture_logs() as logs:
            work, unresolved = plan_dhan_daily_work(
                symbols=["NIFTY"],
                security_ids={"NIFTY": 13},
                start=date(2019, 1, 1),  # before the FIXED 2020-01-01 project floor
                end=date(2020, 3, 1),
                today=_FROZEN_TODAY,
            )
        assert unresolved == []
        assert min(item.start for item in work) == DHAN_DAILY_HISTORY_FLOOR
        notes = [e for e in logs if e["event"] == "dhan.daily_window_floor_clamped"]
        assert len(notes) == 1
        assert notes[0]["effective_start"] == "2020-01-01"

    def test_fixed_floor_never_the_rolling_intraday_floor(self) -> None:
        # Two floors, two laws: at a frozen today of 2022-07-01 the ROLLING
        # intraday floor would sit in 2017; the daily plan must clamp to the
        # FIXED 2020-01-01 PROJECT floor instead — proven by a window that
        # starts exactly AT the fixed floor with zero clamping.
        with capture_logs() as logs:
            work, _ = plan_dhan_daily_work(
                symbols=["NIFTY"],
                security_ids={"NIFTY": 13},
                start=DHAN_DAILY_HISTORY_FLOOR,
                end=date(2020, 6, 1),
                today=_FROZEN_TODAY,
            )
        assert min(item.start for item in work) == DHAN_DAILY_HISTORY_FLOOR
        assert [e for e in logs if e["event"] == "dhan.daily_window_floor_clamped"] == []

    def test_entirely_pre_floor_plans_zero_windows_with_one_warning(self) -> None:
        with capture_logs() as logs:
            work, unresolved = plan_dhan_daily_work(
                symbols=["NIFTY"],
                security_ids={"NIFTY": 13},
                start=date(2019, 1, 1),
                end=date(2019, 12, 31),  # wholly before the floor
                today=_FROZEN_TODAY,
            )
        assert work == []
        assert unresolved == []
        warnings = [e for e in logs if e["event"] == "dhan.daily_pull_entirely_pre_floor"]
        assert len(warnings) == 1
        assert warnings[0]["floor"] == "2020-01-01"

    def test_prev_day_end_clamp_shared_law(self) -> None:
        # DEC-DHAN-TODAY-CLAMP-001: an explicit to_date=today (half-open end ==
        # today + 1) clamps through the SAME clamp_dhan_window_end helper.
        with capture_logs() as logs:
            work, _ = plan_dhan_daily_work(
                symbols=["NIFTY"],
                security_ids={"NIFTY": 13},
                start=date(2022, 6, 1),
                end=date(2022, 7, 2),  # today + 1 day
                today=_FROZEN_TODAY,
            )
        assert max(item.end for item in work) == _FROZEN_TODAY
        assert len([e for e in logs if e["event"] == "dhan.window_end_clamped"]) == 1

    def test_today_none_resolves_now_ist_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import brutex.adapters.dhan.daily_ingest as daily_mod

        frozen = datetime(2022, 7, 1, 12, 0, tzinfo=UTC)
        monkeypatch.setattr(daily_mod, "now_ist", lambda: frozen)
        work, _ = plan_dhan_daily_work(
            symbols=["NIFTY"],
            security_ids={"NIFTY": 13},
            start=date(2022, 6, 1),
            end=date(2022, 7, 2),  # past the frozen today → clamped to it
        )
        assert max(item.end for item in work) == date(2022, 7, 1)

    def test_unresolved_symbol_omitted(self) -> None:
        work, unresolved = plan_dhan_daily_work(
            symbols=["NIFTY", "UNKNOWN"],
            security_ids={"NIFTY": 13},
            start=date(2022, 6, 1),
            end=date(2022, 6, 30),
            today=_FROZEN_TODAY,
        )
        assert unresolved == ["UNKNOWN"]
        assert {item.symbol for item in work} == {"NIFTY"}

    def test_365_day_tiling(self) -> None:
        # ~2.4 years → 3 windows per symbol at the DEFENSIVE 365-day width.
        work, _ = plan_dhan_daily_work(
            symbols=["NIFTY"],
            security_ids={"NIFTY": 13},
            start=date(2020, 1, 1),
            end=date(2022, 6, 1),
            today=_FROZEN_TODAY,
        )
        assert len(work) == 3


# ─────────────────────────────────────────────────────────────────────────────
# run_dhan_spot_daily — the stage end-to-end over the scripted client
# ─────────────────────────────────────────────────────────────────────────────


class TestRunDhanSpotDaily:
    def test_success_writes_native_1day_through_the_sink(self) -> None:
        client = _ScriptedDailyClient({13: [_payload(date(2022, 6, 1), date(2022, 6, 2))]})
        sink = _RecordingSink()
        report = run_dhan_spot_daily(
            symbols=["NIFTY"],
            client=client,
            sink=sink,
            security_ids={"NIFTY": 13},
            start=date(2022, 6, 1),
            end=date(2022, 6, 10),
            today=_FROZEN_TODAY,
        )
        assert report.total_bars == 2
        assert report.failed_windows == 0
        assert [o.status for o in report.outcomes] == [STATUS_SUCCESS]
        assert sink.appends[0]["interval"] == DHAN_DAILY_INTERVAL
        assert sink.appends[0]["segment"] == "INDEX"
        # Midnight-IST stamps (the spot_daily_resample convention) reached the sink.
        bars = sink.appends[0]["bars"]
        assert isinstance(bars, list)
        assert bars[0].timestamp == datetime(2022, 5, 31, 18, 30, tzinfo=UTC)
        # The client saw the ISO window bounds.
        assert client.calls == [{"security_id": 13, "from": "2022-06-01", "to": "2022-06-10"}]
        # Report floor stamps carry the FIXED daily floor.
        assert report.floor == DHAN_DAILY_HISTORY_FLOOR
        assert report.effective_start == date(2022, 6, 1)
        assert report.entirely_pre_floor is False

    def test_non_trading_date_leak_dropped_counted_and_warned(self) -> None:
        # A Saturday (2022-06-04) vendor leak is dropped + WARNed; the trading
        # day lands.
        client = _ScriptedDailyClient({13: [_payload(date(2022, 6, 1), date(2022, 6, 4))]})
        sink = _RecordingSink()
        with capture_logs() as logs:
            report = run_dhan_spot_daily(
                symbols=["NIFTY"],
                client=client,
                sink=sink,
                security_ids={"NIFTY": 13},
                start=date(2022, 6, 1),
                end=date(2022, 6, 10),
                today=_FROZEN_TODAY,
            )
        assert report.total_bars == 1
        drops = [e for e in logs if e["event"] == "dhan.daily_non_trading_date_dropped"]
        assert len(drops) == 1
        assert drops[0]["dropped"] == 1

    def test_todate_belt_drops_bar_past_last_servable_day(self) -> None:
        # Half-open end 2022-06-03 → last servable day 2022-06-02; a returned
        # bar ON 2022-06-03 is dropped by the drop_bars_after BELT + WARNed
        # (covers BOTH toDate-inclusivity readings — never a stored forming bar).
        client = _ScriptedDailyClient({13: [_payload(date(2022, 6, 2), date(2022, 6, 3))]})
        sink = _RecordingSink()
        with capture_logs() as logs:
            report = run_dhan_spot_daily(
                symbols=["NIFTY"],
                client=client,
                sink=sink,
                security_ids={"NIFTY": 13},
                start=date(2022, 6, 1),
                end=date(2022, 6, 3),
                today=_FROZEN_TODAY,
            )
        assert report.total_bars == 1
        belts = [e for e in logs if e["event"] == "dhan.future_bar_dropped"]
        assert len(belts) == 1
        assert belts[0]["dropped"] == 1

    def test_retryable_failure_retries_next_round_under_defaults(self) -> None:
        # A throttle then success — the shared round engine retries under the
        # DEFAULT pace/backoff hooks (covering both no-op defaults).
        client = _ScriptedDailyClient(
            {13: [DhanThrottleError("805: slow down"), _payload(date(2022, 6, 1))]}
        )
        sink = _RecordingSink()
        report = run_dhan_spot_daily(
            symbols=["NIFTY"],
            client=client,
            sink=sink,
            security_ids={"NIFTY": 13},
            start=date(2022, 6, 1),
            end=date(2022, 6, 10),
            today=_FROZEN_TODAY,
        )
        assert len(client.calls) == 2
        assert [o.status for o in report.outcomes] == [STATUS_SUCCESS]
        assert report.total_bars == 1

    def test_non_retryable_failure_is_terminal_per_window(self) -> None:
        client = _ScriptedDailyClient({13: [DhanBadRequestError("813: bad security id")]})
        sink = _RecordingSink()
        with capture_logs() as logs:
            report = run_dhan_spot_daily(
                symbols=["NIFTY"],
                client=client,
                sink=sink,
                security_ids={"NIFTY": 13},
                start=date(2022, 6, 1),
                end=date(2022, 6, 10),
                today=_FROZEN_TODAY,
            )
        assert len(client.calls) == 1  # never re-fed — non-retryable
        assert report.failed_windows == 1
        assert report.outcomes[0].status == STATUS_FAILED
        assert report.outcomes[0].retryable is False
        assert report.outcomes[0].error_type == "DhanBadRequestError"
        fails = [e for e in logs if e["event"] == "dhan.daily_window_failed"]
        assert len(fails) == 1
        assert fails[0]["retryable"] is False

    def test_terminal_pull_stop_propagates_first_occurrence(self) -> None:
        client = _ScriptedDailyClient(
            {
                13: [DhanAuthError("re-read budget exhausted")],
                25: [_payload(date(2022, 6, 1))],
            }
        )
        sink = _RecordingSink()
        with pytest.raises(DhanAuthError):
            run_dhan_spot_daily(
                symbols=["NIFTY", "BANKNIFTY"],
                client=client,
                sink=sink,
                security_ids={"NIFTY": 13, "BANKNIFTY": 25},
                start=date(2022, 6, 1),
                end=date(2022, 6, 10),
                today=_FROZEN_TODAY,
            )
        # The stop propagated on FIRST occurrence — the sibling never ran.
        assert len(client.calls) == 1
        assert sink.appends == []

    def test_unresolved_symbol_loud_skip_in_report(self) -> None:
        client = _ScriptedDailyClient({13: [_payload(date(2022, 6, 1))]})
        with capture_logs() as logs:
            report = run_dhan_spot_daily(
                symbols=["NIFTY", "UNKNOWN"],
                client=client,
                sink=_RecordingSink(),
                security_ids={"NIFTY": 13},
                start=date(2022, 6, 1),
                end=date(2022, 6, 10),
                today=_FROZEN_TODAY,
            )
        assert report.unresolved_symbols == ("UNKNOWN",)
        assert report.resolved_symbols == ("NIFTY",)
        assert any(e["event"] == "dhan.index_unverified" for e in logs)

    def test_entirely_pre_floor_run_is_honest_noop(self) -> None:
        client = _ScriptedDailyClient({13: []})
        sink = _RecordingSink()
        with capture_logs() as logs:
            report = run_dhan_spot_daily(
                symbols=["NIFTY"],
                client=client,
                sink=sink,
                security_ids={"NIFTY": 13},
                start=date(2019, 1, 1),
                end=date(2019, 12, 31),
                today=_FROZEN_TODAY,
            )
        assert report.entirely_pre_floor is True
        assert report.effective_start is None
        assert report.outcomes == ()
        assert client.calls == []
        assert any(e["event"] == "dhan.daily_pull_entirely_pre_floor" for e in logs)

    def test_today_none_resolves_now_ist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import brutex.adapters.dhan.daily_ingest as daily_mod

        frozen = datetime(2022, 7, 1, 12, 0, tzinfo=UTC)
        monkeypatch.setattr(daily_mod, "now_ist", lambda: frozen)
        client = _ScriptedDailyClient({13: [_payload(date(2022, 6, 1))]})
        report = run_dhan_spot_daily(
            symbols=["NIFTY"],
            client=client,
            sink=_RecordingSink(),
            security_ids={"NIFTY": 13},
            start=date(2022, 6, 1),
            end=date(2022, 7, 2),  # past the frozen today → clamped
        )
        assert report.total_bars == 1
        assert client.calls[0]["to"] == "2022-07-01"
