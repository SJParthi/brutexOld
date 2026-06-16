"""Tests for ``brutex.adapters.groww.ingest`` — window-by-window spot ingest.

100% line+branch: interval-tiered window tiling (30/90/180-day tiers), the pace
hook, transient-error counting + in-run auto-retry, terminal-error propagation
(auth included — no fail-fast latch), the ledger skip+record authority, the
session filter, current-day clamp, conflict logging, and the bars-appended
accounting. Driven by controllable FAKE adapters (no live Groww SDK) + a
recording fake sink.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.errors import (
    GrowwAuthError,
    GrowwBadRequestError,
    GrowwRateLimitError,
    GrowwTimeoutError,
)
from brutex.adapters.groww.ingest import (
    IngestReport,
    _default_retry_backoff,
    _pace_rps,
    clamp_window_end,
    ingest_spot,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import BarConflict, WriteResult
from brutex.data.sink import SinkAppendResult
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.observability.correlation import pull_id_scope
from brutex.orchestrator.universe_phases import ItemResult
from structlog.testing import capture_logs

# 04:05 UTC = 09:35 IST → in the [09:15, 15:30) session.
_IN_SESSION_TS = datetime(2021, 1, 4, 4, 5, tzinfo=UTC)
# 12:00 UTC = 17:30 IST → out of session.
_OUT_OF_SESSION_TS = datetime(2021, 1, 4, 12, 0, tzinfo=UTC)


def _bar(ts: datetime = _IN_SESSION_TS) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=1,
        open_interest=None,
    )


def _inst(symbol: str = "NIFTY") -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


_INST = _inst()


class _Sink:
    """Recording sink — appends the per-call bar count, returns a clean result."""

    def __init__(self) -> None:
        self.appends: list[int] = []

    def append(
        self, *, exchange: str, segment: str, symbol: str, interval: str, bars: Sequence[CandleBar]
    ) -> SinkAppendResult:
        del exchange, segment, symbol, interval
        self.appends.append(len(bars))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


class _Adapter:
    """Fake MarketAdapter: returns a fixed bar list per fetch (or raises *error*)."""

    def __init__(
        self, *, bars: Sequence[CandleBar] | None = None, error: Exception | None = None
    ) -> None:
        self._bars = list(bars) if bars is not None else [_bar()]
        self._error = error
        self.calls = 0

    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del symbol, exchange, interval, start, end
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._bars)


class _SeqAdapter:
    """Fake adapter raising per a fixed ERROR SEQUENCE (None = success), clamped to last."""

    def __init__(
        self, errors: Sequence[Exception | None], *, bars: Sequence[CandleBar] | None = None
    ) -> None:
        self._errors = list(errors)
        self._bars = list(bars) if bars is not None else [_bar()]
        self.calls = 0

    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del symbol, exchange, interval, start, end
        err = self._errors[min(self.calls, len(self._errors) - 1)]
        self.calls += 1
        if err is not None:
            raise err
        return list(self._bars)


def _sync_run_parallel[ItemT, ResultT](
    fn: Callable[[ItemT], ResultT],
    items: Sequence[ItemT],
) -> list[ItemResult[ItemT, ResultT]]:
    """Synchronous ``RunParallel`` fake: run *fn* inline IN ORDER, capture excs."""
    out: list[ItemResult[ItemT, ResultT]] = []
    for item in items:
        try:
            value = fn(item)
        except Exception as exc:  # mirror PoolRunParallel's capture
            out.append(ItemResult(item=item, ok=False, result=None, exc=exc))
        else:
            out.append(ItemResult(item=item, ok=True, result=value, exc=None))
    return out


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    """All captured events with ``event == name`` (in emission order)."""
    return [e for e in logs if e["event"] == name]


class TestIngestSpot:
    def test_daily_single_window(self) -> None:
        # 1day tiles at 180 days; ~212-day range → 2 windows.
        sink = _Sink()
        report = ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            sink,
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
        )
        assert report.windows == 2
        assert report.bars_appended == 2
        assert sink.appends == [1, 1]

    def test_intraday_tiles_into_30_day_windows(self) -> None:
        # 1minute tiles at 30 days; ~212-day range → 8 windows.
        sink = _Sink()
        report = ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            sink,
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
        )
        assert report.windows == 8
        assert report.bars_appended == 8
        assert sink.appends == [1] * 8

    def test_pace_called_once_per_window(self) -> None:
        calls = {"n": 0}

        def _pace() -> None:
            calls["n"] += 1

        ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            pace=_pace,
        )
        assert calls["n"] == 8

    def test_giftnifty_keeps_out_of_session_bars(self) -> None:
        gift = _inst("GIFTNIFTY")
        sink = _Sink()
        report = ingest_spot(
            _Adapter(bars=[_bar(_OUT_OF_SESSION_TS)]),  # type: ignore[arg-type]
            sink,
            instrument=gift,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 1, 20),  # 1 window
        )
        assert report.bars_appended == 1  # the out-of-session bar is KEPT
        assert sink.appends == [1]

    def test_non_exempt_drops_out_of_session_bars(self) -> None:
        sink = _Sink()
        report = ingest_spot(
            _Adapter(bars=[_bar(_OUT_OF_SESSION_TS)]),  # type: ignore[arg-type]
            sink,
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 1, 20),
        )
        assert report.bars_appended == 0  # dropped by the session filter
        assert sink.appends == [0]

    def test_intraday_mixed_session_bars_filtered(self) -> None:
        # One in-session + one out-of-session bar → only the in-session bar kept.
        sink = _Sink()
        report = ingest_spot(
            _Adapter(bars=[_bar(_IN_SESSION_TS), _bar(_OUT_OF_SESSION_TS)]),  # type: ignore[arg-type]
            sink,
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 1, 20),
        )
        assert report.windows == 1
        assert report.bars_appended == 1
        assert sink.appends == [1]

    def test_transient_error_counted_and_skipped(self) -> None:
        sink = _Sink()
        report = ingest_spot(
            _Adapter(error=GrowwRateLimitError("rate limited")),  # type: ignore[arg-type]
            sink,
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),  # 8 windows
            max_retry_passes=0,
        )
        assert report.windows == 8
        assert report.bars_appended == 0
        assert report.transient_failures == 8
        assert sink.appends == []

    def test_terminal_error_propagates(self) -> None:
        with pytest.raises(GrowwBadRequestError):
            ingest_spot(
                _Adapter(error=GrowwBadRequestError("bad")),  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 3, 1),
            )

    def test_auth_error_propagates_no_latch(self) -> None:
        # Groww divergence: an auth error is a plain terminal raise (no fail-fast
        # guard); it propagates to the outer quarantine like any terminal error.
        with pytest.raises(GrowwAuthError):
            ingest_spot(
                _Adapter(error=GrowwAuthError("token expired")),  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 3, 1),
            )


class TestIngestSpotLedger:
    """The unified WindowLedger is the SOLE skip + record authority."""

    def test_ledger_marks_then_skips_on_repull(self) -> None:
        from brutex.data.window_ledger import SUCCESS, WindowLedger

        led = WindowLedger()
        first = ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),  # 8 windows
            ledger=led,
        )
        assert first.windows == 8
        assert first.bars_appended == 8
        assert first.skipped_windows == 0
        assert {r.status for r in led.records()} == {SUCCESS}

        sink2 = _Sink()
        second = ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            sink2,
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            ledger=led,
        )
        assert second.windows == 8
        assert second.skipped_windows == 8
        assert second.bars_appended == 0
        assert sink2.appends == []

    def test_no_ledger_always_refetches(self) -> None:
        ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
        )
        second = ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
        )
        assert second.skipped_windows == 0
        assert second.bars_appended == 8

    def test_ledger_records_empty_on_zero_bar_response(self) -> None:
        from brutex.data.window_ledger import EMPTY, WindowLedger

        ledger = WindowLedger()
        ingest_spot(
            _Adapter(bars=[]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),  # 1 window (<180d)
            ledger=ledger,
        )
        assert ledger.records()[0].status == EMPTY

    def test_ledger_records_failed_on_rate_limit(self) -> None:
        from brutex.data.window_ledger import FAILED, WindowLedger

        ledger = WindowLedger()
        ingest_spot(
            _Adapter(error=GrowwRateLimitError("rate limited")),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            ledger=ledger,
            max_retry_passes=0,
        )
        rec = ledger.records()[0]
        assert rec.status == FAILED
        assert rec.last_error == "GrowwRateLimitError"


class TestIngestSpotInRunRetry:
    """In-run transient-window auto-retry (mirror of the Dhan spine)."""

    def test_transient_window_retried_then_succeeds(self) -> None:
        adapter = _SeqAdapter([GrowwTimeoutError("t"), None])
        report = ingest_spot(
            adapter,  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",  # 1 window (<180d)
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            retry_backoff=lambda _i: None,
        )
        assert report.bars_appended == 1
        assert report.transient_failures == 0
        assert report.still_failed_windows == 0
        assert report.retried_windows == 1

    def test_persists_after_budget_left_unmarked(self) -> None:
        adapter = _SeqAdapter([GrowwTimeoutError("t")])  # always fails
        report = ingest_spot(
            adapter,  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            max_retry_passes=1,
            retry_backoff=lambda _i: None,
        )
        assert report.transient_failures == 1
        assert report.still_failed_windows == 1
        assert report.retried_windows == 1

    def test_max_retry_passes_zero_disables_retry(self) -> None:
        with capture_logs() as logs:
            report = ingest_spot(
                _Adapter(error=GrowwTimeoutError("t")),  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 3, 1),
                max_retry_passes=0,
                retry_backoff=lambda _i: None,
            )
        assert report.transient_failures == 1
        assert report.retried_windows == 0
        assert _events(logs, "ingest.retry_pass") == []

    def test_retry_pass_and_window_transient_logged(self) -> None:
        adapter = _SeqAdapter([GrowwTimeoutError("t"), None])
        with capture_logs() as logs, pull_id_scope("retrypull01"):
            ingest_spot(
                adapter,  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 3, 1),
                retry_backoff=lambda _i: None,
            )
        (warn,) = _events(logs, "ingest.window.transient")
        assert warn["log_level"] == "warning"
        assert warn["pull_id"] == "retrypull01"
        assert warn["window_start"] == "2021-01-01"
        assert warn["error_type"] == "GrowwTimeoutError"
        assert warn["in_run_retry"] is True
        (rp,) = _events(logs, "ingest.retry_pass")
        assert rp["log_level"] == "info"
        assert rp["retry_pass"] == 1
        assert rp["windows"] == 1
        (series,) = _events(logs, "ingest.series")
        assert series["retried_windows"] == 1
        assert series["still_failed_windows"] == 0

    def test_backoff_invoked_once_per_retry_pass_with_pass_index(self) -> None:
        calls: list[int] = []
        adapter = _SeqAdapter([GrowwTimeoutError("t")])
        ingest_spot(
            adapter,  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            max_retry_passes=2,
            retry_backoff=calls.append,
        )
        assert calls == [0, 1]

    def test_default_backoff_is_bounded_exponential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        slept: list[float] = []
        monkeypatch.setattr("brutex.adapters.groww.ingest._time_sleep", slept.append)
        _default_retry_backoff(0)
        _default_retry_backoff(1)
        _default_retry_backoff(99)  # clamps to the ceiling
        assert slept == [1.0, 2.0, 8.0]


class TestIngestSpotRateFeedback:
    """The rate-controller feedback hooks."""

    def test_on_rate_limit_fires_on_rate_limit_not_on_timeout(self) -> None:
        hits: list[int] = []
        timeouts: list[int] = []
        ok: list[int] = []
        report = ingest_spot(
            _Adapter(error=GrowwRateLimitError("rl")),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            on_rate_limit=lambda: hits.append(1),
            on_timeout=lambda: timeouts.append(1),
            on_success=lambda: ok.append(1),
            max_retry_passes=0,
        )
        assert report.transient_failures == 1
        assert hits == [1]
        assert timeouts == []
        assert ok == []

    def test_on_timeout_fires_on_non_ratelimit_retryable(self) -> None:
        hits: list[int] = []
        timeouts: list[int] = []
        ingest_spot(
            _Adapter(error=GrowwTimeoutError("to")),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            on_rate_limit=lambda: hits.append(1),
            on_timeout=lambda: timeouts.append(1),
            max_retry_passes=0,
        )
        assert hits == []
        assert timeouts == [1]

    def test_on_success_fires_on_clean_window(self) -> None:
        ok: list[int] = []
        ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            on_success=lambda: ok.append(1),
        )
        assert ok == [1]

    def test_transient_with_no_hooks_still_counts(self) -> None:
        # No on_rate_limit / on_timeout wired → the retryable branch must still
        # count the failure (exercises the None-hook arms).
        report = ingest_spot(
            _SeqAdapter([GrowwRateLimitError("rl"), GrowwTimeoutError("to")]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            max_retry_passes=0,
        )
        assert report.transient_failures == 1


class TestIngestSpotLogging:
    """Every window / parse / lake-write / series event carries the ``pull_id``."""

    def test_fetched_window_lights_window_parse_lakewrite_series(self) -> None:
        sink = _Sink()
        with capture_logs() as logs, pull_id_scope("spotpull0001"):
            report = ingest_spot(
                _Adapter(),  # type: ignore[arg-type]
                sink,
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 3, 1),  # 1 window
            )
        assert report.windows == 1
        (window,) = _events(logs, "ingest.window")
        assert window["log_level"] == "debug"
        assert window["pull_id"] == "spotpull0001"
        assert window["symbol"] == "NIFTY"
        assert window["interval"] == "1day"
        assert window["window_start"] == "2021-01-01"
        assert window["action"] == "fetched"
        (parse,) = _events(logs, "ingest.parse")
        assert parse["pull_id"] == "spotpull0001"
        assert parse["raw_bars"] == 1
        assert parse["session_bars"] == 1
        (write,) = _events(logs, "ingest.lake_write")
        assert write["bars_appended"] == 1
        (series,) = _events(logs, "ingest.series")
        assert series["windows"] == 1
        assert series["bars_appended"] == 1
        assert series["skipped_windows"] == 0

    def test_skipped_window_lights_only_the_skip_event(self) -> None:
        from brutex.data.window_ledger import WindowLedger

        led = WindowLedger()
        ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            ledger=led,
        )
        with capture_logs() as logs, pull_id_scope("spotpull0003"):
            report = ingest_spot(
                _Adapter(),  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 3, 1),
                ledger=led,
            )
        assert report.skipped_windows == 1
        (window,) = _events(logs, "ingest.window")
        assert window["action"] == "skipped"
        assert window["pull_id"] == "spotpull0003"
        assert _events(logs, "ingest.parse") == []
        assert _events(logs, "ingest.lake_write") == []
        (series,) = _events(logs, "ingest.series")
        assert series["skipped_windows"] == 1

    def test_pull_id_is_empty_string_outside_a_scope(self) -> None:
        with capture_logs() as logs:
            ingest_spot(
                _Adapter(),  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 3, 1),
            )
        assert _events(logs, "ingest.window")[0]["pull_id"] == ""


# ── Conflict counting + logging ─────────────────────────────────────────


def _make_conflict(
    *,
    ts_epoch: int = 1609733100,
    stored_close: str = "105.00",
    vendor_close: str = "104.50",
) -> BarConflict:
    ts = datetime.fromtimestamp(ts_epoch, tz=UTC)
    stored = CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal(stored_close),
        volume=1,
        open_interest=None,
    )
    vendor = CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal(vendor_close),
        volume=1,
        open_interest=None,
    )
    return BarConflict(timestamp=ts, stored=stored, vendor=vendor)


class _ConflictSink:
    """A sink injecting pre-built conflicts into its :class:`SinkAppendResult`."""

    def __init__(self, conflicts: tuple[BarConflict, ...], bars_appended: int = 0) -> None:
        self._conflicts = conflicts
        self._bars_appended = bars_appended
        self._fake_path = Path("/dev/null")

    def append(
        self, *, exchange: str, segment: str, symbol: str, interval: str, bars: Sequence[CandleBar]
    ) -> SinkAppendResult:
        del exchange, segment, symbol, interval, bars
        ts = _IN_SESSION_TS
        wr = WriteResult(
            path=self._fake_path,
            row_count=1,
            raw_hash="a" * 64,
            first_ts=ts,
            last_ts=ts,
            bytes_written=0,
            conflicts=self._conflicts,
        )
        return SinkAppendResult(
            bars_appended=self._bars_appended,
            partitions_written=1,
            write_results=(wr,),
        )


class TestIngestSpotConflicts:
    def test_conflict_emits_summary_warning_and_counts_in_report(self) -> None:
        conflict = _make_conflict(stored_close="105.00", vendor_close="104.50")
        sink = _ConflictSink(conflicts=(conflict,), bars_appended=0)
        with capture_logs() as logs, pull_id_scope("conflict001"):
            report = ingest_spot(
                _Adapter(),  # type: ignore[arg-type]
                sink,
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 3, 1),
            )
        assert report.conflicts == 1
        (cl,) = _events(logs, "ingest.conflict")
        assert cl["log_level"] == "debug"
        assert cl["stored_close"] == "105.00"
        assert cl["vendor_close"] == "104.50"
        (summary,) = _events(logs, "ingest.conflict.summary")
        assert summary["log_level"] == "warning"
        assert summary["pull_id"] == "conflict001"
        assert summary["conflicts"] == 1
        assert summary["differing_fields"] == "close"
        assert summary["symbol"] == "NIFTY"

    def test_no_conflict_emits_no_warning(self) -> None:
        with capture_logs() as logs:
            report = ingest_spot(
                _Adapter(),  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 3, 1),
            )
        assert report.conflicts == 0
        assert _events(logs, "ingest.conflict") == []
        assert _events(logs, "ingest.conflict.summary") == []

    def test_conflict_count_appears_in_series_log(self) -> None:
        sink = _ConflictSink(conflicts=(_make_conflict(),), bars_appended=0)
        with capture_logs() as logs:
            ingest_spot(
                _Adapter(),  # type: ignore[arg-type]
                sink,
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 3, 1),
            )
        (series,) = _events(logs, "ingest.series")
        assert series["conflicts"] == 1


class TestPaceRps:
    def test_returns_controller_rps_when_valid(self) -> None:
        class _FakeController:
            current_rps: float = 3.5

        controller = _FakeController()

        def _pace() -> None:
            pass

        _pace.__self__ = controller  # type: ignore[attr-defined]
        assert _pace_rps(_pace) == 3.5  # type: ignore[arg-type]

    def test_falls_back_to_unverified_ceiling_when_no_controller(self) -> None:
        from brutex.adapters.groww.constants import GROWW_RATE_LIMIT_RPS_UNVERIFIED

        def _pace() -> None:
            pass

        assert _pace_rps(_pace) == GROWW_RATE_LIMIT_RPS_UNVERIFIED

    def test_falls_back_when_pace_is_none(self) -> None:
        from brutex.adapters.groww.constants import GROWW_RATE_LIMIT_RPS_UNVERIFIED

        assert _pace_rps(None) == GROWW_RATE_LIMIT_RPS_UNVERIFIED


class TestCurrentDayClamp:
    """clamp_window_end + its ingest_spot wiring (same NSE/BSE reality as Dhan)."""

    @staticmethod
    def _ist(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
        from zoneinfo import ZoneInfo

        return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("Asia/Kolkata"))

    def test_daily_never_requests_today(self) -> None:
        now = self._ist(2026, 6, 10, 22, 0)
        assert clamp_window_end("1day", date(2026, 6, 12), now_ist=now) == date(2026, 6, 10)

    def test_daily_past_end_untouched(self) -> None:
        now = self._ist(2026, 6, 10, 22, 0)
        assert clamp_window_end("1day", date(2026, 6, 9), now_ist=now) == date(2026, 6, 9)

    def test_intraday_before_close_excludes_today(self) -> None:
        now = self._ist(2026, 6, 10, 12, 0)  # mid-session
        assert clamp_window_end("1minute", date(2026, 6, 11), now_ist=now) == date(2026, 6, 10)

    def test_intraday_from_close_includes_today(self) -> None:
        now = self._ist(2026, 6, 10, 15, 30)  # close — session complete
        assert clamp_window_end("1minute", date(2026, 6, 11), now_ist=now) == date(2026, 6, 11)

    def test_ingest_clamps_daily_and_skips_empty_range(self) -> None:
        sink = _Sink()
        with capture_logs() as logs:
            report = ingest_spot(
                _Adapter(),  # type: ignore[arg-type]
                sink,
                instrument=_INST,
                interval="1day",
                start=date(2026, 6, 10),
                end=date(2026, 6, 11),
                now_ist=lambda: self._ist(2026, 6, 10, 22, 0),
            )
        assert report == IngestReport(windows=0, bars_appended=0, transient_failures=0)
        assert sink.appends == []
        # The clamp emitted its INFO notice.
        assert len(_events(logs, "ingest.window.clamped")) == 1

    def test_ingest_clamped_but_nonempty_range_still_pulls(self) -> None:
        sink = _Sink()
        report = ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            sink,
            instrument=_INST,
            interval="1day",
            start=date(2026, 6, 9),
            end=date(2026, 6, 12),
            now_ist=lambda: self._ist(2026, 6, 10, 22, 0),
        )
        assert report.windows == 1
        assert sink.appends == [1]

    def test_ingest_default_clock_no_op_for_past_ranges(self) -> None:
        sink = _Sink()
        report = ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            sink,
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
        )
        assert report.windows == 1


class TestParallelSeam:
    """The injectable run_parallel seam (synchronous fake → zero threads)."""

    def test_sync_run_parallel_drives_the_fan_out(self) -> None:
        adapter = _Adapter()
        report = ingest_spot(
            adapter,  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),  # 8 windows
            run_parallel=_sync_run_parallel,
        )
        assert report.windows == 8
        assert report.bars_appended == 8
        assert adapter.calls == 8


class TestOnWindowTick:
    """DEC-SPOT-WINDOW-PROGRESS-001: ``on_window`` fires once per PROCESSED window."""

    def test_fires_once_per_window(self) -> None:
        # 1day tiles at 180d; ~212-day range → 2 windows → 2 ticks.
        ticks: list[int] = []
        ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            run_parallel=_sync_run_parallel,
            on_window=lambda: ticks.append(1),
        )
        assert len(ticks) == 2

    def test_fires_for_a_skipped_window_too(self) -> None:
        # A window already settled in the ledger is SKIPPED (no fetch) but still ticks,
        # so a re-pull's bar reflects the skips (re-pulls are mostly skips).
        from brutex.data.fetch_coverage import WindowKey
        from brutex.data.window_ledger import WindowLedger

        ledger = WindowLedger()
        # Pre-settle both daily windows as SUCCESS so the pull skips them.
        for ws, we in ((date(2021, 1, 1), date(2021, 6, 30)), (date(2021, 6, 30), date(2021, 8, 1))):
            ledger.record(
                WindowKey(market="groww", security_id="NIFTY", interval="1day", start=ws, end=we),
                raw_bars=5,
                error_type=None,
                bars_appended=5,
                now=datetime(2026, 6, 8, tzinfo=UTC),
            )
        adapter = _Adapter()
        ticks: list[int] = []
        ingest_spot(
            adapter,  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            ledger=ledger,
            run_parallel=_sync_run_parallel,
            on_window=lambda: ticks.append(1),
        )
        assert adapter.calls == 0  # both windows skipped — no vendor hit
        assert len(ticks) == 2  # but the tick still fired for each skipped window

    def test_none_on_window_is_a_noop(self) -> None:
        # The default (CLI / tests) path passes no hook — must run cleanly.
        report = ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            run_parallel=_sync_run_parallel,
        )
        assert report.windows == 2
