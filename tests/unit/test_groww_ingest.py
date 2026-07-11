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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.all_filtered_breaker import AllFilteredBreaker
from brutex.adapters.groww.errors import (
    GrowwAuthError,
    GrowwBadRequestError,
    GrowwRateLimitError,
    GrowwTimeoutError,
    GrowwTokenIssuanceThrottled,
    GrowwTokenStillStaleError,
)
from brutex.adapters.groww.ingest import (
    IngestReport,
    _default_retry_backoff,
    _pace_rps,
    clamp_window_end,
    ingest_spot,
)
from brutex.data.errors import PartitionLockTimeout
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import BarConflict, WriteResult
from brutex.data.partial_window_detect import last_completed_session_on_or_before
from brutex.data.sink import SinkAppendResult
from brutex.data.window_ledger import WindowLedger as LedgerWindowLedger
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
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        del exchange, segment, symbol, interval
        self.appends.append(len(bars))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


class _LockTimeoutSink:
    """Sink whose ``append`` raises :class:`PartitionLockTimeout` (a wedged peer)."""

    def __init__(self, timeout_seconds: float = 0.5) -> None:
        self._timeout_seconds = timeout_seconds
        self.calls = 0

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
        del exchange, segment, symbol, interval, bars, merge_mode
        self.calls += 1
        raise PartitionLockTimeout(self._timeout_seconds)


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


class _CompleteWindowAdapter:
    """Fake adapter returning ONE in-session bar at the LAST completed session of each window.

    Unlike :class:`_Adapter` (a fixed Jan-2021 bar for every window), this places the bar at
    the window's own trailing edge, so the data-completeness scans
    (DEC-PARTIAL-WINDOW-REPULL-001 / DEC-INTERIOR-GAP-DETECT-001) see a COMPLETE window and
    never demote it — letting the ledger SKIP-mechanism tests pin the skip path without the
    fixed-bar fixture spuriously tripping the truncation flag (which a 1-bar-in-January window
    over a 7-month span legitimately would).
    """

    def __init__(self) -> None:
        self.calls = 0

    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del symbol, exchange, interval, start
        self.calls += 1
        last_session = last_completed_session_on_or_before(end - timedelta(days=1))
        # 04:05 UTC = 09:35 IST → inside the [09:15, 15:30) session for that trading date.
        ts = datetime(last_session.year, last_session.month, last_session.day, 4, 5, tzinfo=UTC)
        return [_bar(ts)]


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


class _BarSeqAdapter:
    """Fake adapter returning a SCRIPTED sequence of bar-lists per call (clamped to last).

    Drives the in-pull EMPTY-retry (DEC-EMPTY-INPULL-RETRY-001): e.g. ``[[], [], [bar]]``
    is a transient/hallucinated empty that recovers on the 3rd attempt; ``[[]]`` is a
    genuinely-empty window re-tried up to the budget.
    """

    def __init__(self, responses: Sequence[Sequence[CandleBar]]) -> None:
        self._responses = [list(r) for r in responses]
        self.calls = 0

    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del symbol, exchange, interval, start, end
        bars = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return list(bars)


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


class TestMarketHoursGuard:
    """DEC-GROWW-MARKET-HOURS-GUARD-001 — the per-window market-hours halt."""

    def test_blocked_guard_raises_before_any_fetch(self) -> None:
        from brutex.data.errors import MarketHoursHalt

        adapter = _Adapter()
        sink = _Sink()
        with pytest.raises(MarketHoursHalt) as excinfo:
            ingest_spot(
                adapter,  # type: ignore[arg-type]
                sink,
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 8, 1),
                market_closed=lambda: True,
            )
        assert excinfo.value.reason == "market_hours"
        # The guard fires BEFORE the vendor fetch — no call, no write.
        assert adapter.calls == 0
        assert sink.appends == []

    def test_open_market_guard_does_not_gate(self) -> None:
        """market_closed returning False (the A-True/B-False branch) never halts."""
        adapter = _Adapter()
        sink = _Sink()
        report = ingest_spot(
            adapter,  # type: ignore[arg-type]
            sink,
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            market_closed=lambda: False,
        )
        assert report.windows == 2
        assert adapter.calls == 2

    def test_none_guard_is_pre_guard_behaviour(self) -> None:
        """The default (None) guard never consults a clock — byte-identical to before."""
        adapter = _Adapter()
        sink = _Sink()
        report = ingest_spot(
            adapter,  # type: ignore[arg-type]
            sink,
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            market_closed=None,
        )
        assert report.windows == 2


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

    def test_truncated_spot_window_fires_partial_warning(self) -> None:
        # DEC-PARTIAL-WINDOW-DETECT-001 (spot wiring): a 1day window [2022-01-01, 2022-06-01)
        # whose only bar lands 2022-01-04 — months before the window end, with "now" far past
        # it — is a TRUNCATED SUCCESS spot window → ONE observe-only WARNING. Spot indices
        # trade densely every session, so this is a genuine, reliably-detectable fetch gap.
        sink = _Sink()
        early_bar = _bar(datetime(2022, 1, 4, 4, 5, tzinfo=UTC))
        with capture_logs() as logs:
            report = ingest_spot(
                _Adapter(bars=[early_bar]),  # type: ignore[arg-type]
                sink,
                instrument=_INST,
                interval="1day",
                start=date(2022, 1, 1),
                end=date(2022, 6, 1),
            )
        assert report.bars_appended == 1
        warns = _events(logs, "window.suspected_partial")
        assert len(warns) == 1
        assert warns[0]["symbol"] == repr("NIFTY")
        assert warns[0]["last_bar_date"] == "2022-01-04"
        assert warns[0]["expiry"] is None  # spot has no expiry

    def test_complete_spot_window_no_partial_warning(self) -> None:
        # A spot window whose bar reaches its end (here end-of-day before the window end) → no
        # truncation WARNING (the dense-session complete case).
        sink = _Sink()
        last_session_bar = _bar(datetime(2022, 1, 31, 4, 5, tzinfo=UTC))
        with capture_logs() as logs:
            ingest_spot(
                _Adapter(bars=[last_session_bar]),  # type: ignore[arg-type]
                sink,
                instrument=_INST,
                interval="1day",
                start=date(2022, 1, 1),
                end=date(2022, 2, 1),
            )
        assert _events(logs, "window.suspected_partial") == []

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

    def test_drops_future_dated_phantom_bar(self) -> None:
        # DEC-CANDLE-DROP-FUTURE-BAR-001: a vendor bar dated AFTER the window end (a clock-skew /
        # out-of-window phantom) is dropped before the lake write, never writing a future partition.
        # 2021-01-25 09:30 IST is IN-SESSION (survives the session filter) but PAST the Jan-20 end.
        future_ts = datetime(2021, 1, 25, 4, 5, tzinfo=UTC)
        sink = _Sink()
        report = ingest_spot(
            _Adapter(bars=[_bar(_IN_SESSION_TS), _bar(future_ts)]),  # type: ignore[arg-type]
            sink,
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 1, 20),  # 1 window; the Jan-25 bar is past the end
        )
        assert report.bars_appended == 1  # only the in-window Jan-4 bar; the Jan-25 phantom dropped
        assert sink.appends == [1]

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

    def test_partition_lock_timeout_defers_window_not_quarantine(self) -> None:
        """A wedged-peer lock timeout on write DEFERS the window like a 429 (GAP #3).

        The sink's append raises ``PartitionLockTimeout``; the window must be recorded
        FAILED + counted transient (deferred to the next pull), NOT a symbol-wide
        quarantine and NOT an infinite block (DEC-PULL-CONTENTION-ANTISTALL-001).
        """
        from brutex.data.window_ledger import FAILED, WindowLedger

        led = WindowLedger()
        sink = _LockTimeoutSink()
        report = ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            sink,
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),  # 8 windows
            ledger=led,
            max_retry_passes=0,
        )
        assert report.windows == 8
        assert report.bars_appended == 0
        assert report.transient_failures == 8  # every window deferred, not quarantined
        # The ledger marked each window FAILED (re-tried next pull, never masked).
        assert {r.status for r in led.records()} == {FAILED}
        assert sink.calls == 8  # the fetch ran, the write hit the wedged lock per window

    def test_partition_lock_timeout_logs_write_deferred_without_ledger(self) -> None:
        """The defer path works with NO ledger too (the ``ledger is None`` branch)."""
        with capture_logs() as logs:
            report = ingest_spot(
                _Adapter(),  # type: ignore[arg-type]
                _LockTimeoutSink(),
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 3, 1),
                ledger=None,
                max_retry_passes=0,
            )
        assert report.transient_failures >= 1
        deferred = [r for r in logs if r["event"] == "ingest.window.write_deferred"]
        assert deferred  # the unified write-defer WARNING fired
        # A lock-acquire timeout is distinguished from a write-deadline by ``error_type``.
        assert deferred[0]["error_type"] == "PartitionLockTimeout"

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
        # Use the COMPLETE-window adapter (a bar at each window's trailing edge) so the
        # data-completeness demotion (DEC-PARTIAL-WINDOW-REPULL-001) does NOT fire — this test
        # pins the SUCCESS→skip mechanism, not the truncation re-pull.
        first = ingest_spot(
            _CompleteWindowAdapter(),  # type: ignore[arg-type]
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
            _CompleteWindowAdapter(),  # type: ignore[arg-type]
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
            _CompleteWindowAdapter(),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
        )
        second = ingest_spot(
            _CompleteWindowAdapter(),  # type: ignore[arg-type]
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
            # A RECENT clock (just after the window) so the old-gap fast-settle
            # (DEC-OLD-GAP-FAST-SETTLE-001) does NOT fire — this test pins the
            # conservative across-pull EMPTY classification, which is unchanged for
            # recent windows.
            now_ist=lambda: datetime(2021, 3, 10, 12, 0, tzinfo=UTC),
        )
        assert ledger.records()[0].status == EMPTY

    def test_empty_then_data_recovers_to_success(self) -> None:
        # DEC-EMPTY-INPULL-RETRY-001: a 0-bar response that is a TRANSIENT vendor glitch
        # (data DOES exist) is recovered — the in-pull retry re-fetches and the attempt
        # that returns bars WINS, so the window is recorded SUCCESS, not a false EMPTY.
        from brutex.data.window_ledger import SUCCESS, WindowLedger

        ledger = WindowLedger()
        adapter = _BarSeqAdapter([[], [], [_bar()]])  # empty, empty, then DATA on attempt 3
        report = ingest_spot(
            adapter,  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),  # 1 window
            ledger=ledger,
            empty_retry_attempts=3,
        )
        assert adapter.calls == 3  # re-fetched until data appeared
        assert report.bars_appended == 1
        rec = ledger.records()[0]
        assert rec.status == SUCCESS  # recovered, NOT a false no-data
        # DEC-WINDOW-INPULL-FETCHES-001: ONE pull, but 3 network fetches — the count
        # the page now shows so the 3× retry is no longer invisible behind "attempts 1".
        assert rec.attempts == 1
        assert rec.in_pull_fetches == 3

    def test_genuine_empty_exhausts_attempts_then_records_empty(self) -> None:
        # A window that is empty on EVERY attempt is recorded EMPTY with confidence — and
        # the retry is BOUNDED (exactly empty_retry_attempts fetches, never infinite).
        from brutex.data.window_ledger import EMPTY, WindowLedger

        ledger = WindowLedger()
        adapter = _BarSeqAdapter([[]])  # always empty
        backoffs: list[int] = []
        ingest_spot(
            adapter,  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            ledger=ledger,
            empty_retry_attempts=3,
            empty_retry_backoff=backoffs.append,
            # RECENT clock (just after the window) → conservative EMPTY, NOT the old-gap
            # fast-settle (DEC-OLD-GAP-FAST-SETTLE-001 is exercised by its own test below).
            now_ist=lambda: datetime(2021, 3, 10, 12, 0, tzinfo=UTC),
        )
        assert adapter.calls == 3  # bounded — exactly the budget, no infinite loop
        assert backoffs == [0, 1]  # paused between attempts 1→2 and 2→3, NOT after the last
        rec = ledger.records()[0]
        assert rec.status == EMPTY
        # The genuine-gap window was re-fetched the full budget this pull — the count
        # the operator asked to SEE (a SENSEX/FINNIFTY early-year EMPTY row, ↻ 3).
        assert rec.in_pull_fetches == 3

    def test_empty_attempts_zero_clamps_to_single_fetch(self) -> None:
        # Out-of-box guard (hostile-review NIT, DEC-WINDOW-INPULL-FETCHES-001): a degenerate
        # empty_retry_attempts=0 must still fetch ONCE — never record EMPTY without calling
        # the vendor. Mirrors the option spine's max(1, …) clamp; in_pull_fetches stays ≥1.
        from brutex.data.window_ledger import EMPTY, WindowLedger

        ledger = WindowLedger()
        adapter = _BarSeqAdapter([[]])  # always empty
        ingest_spot(
            adapter,  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            ledger=ledger,
            empty_retry_attempts=0,
        )
        assert adapter.calls == 1  # clamped to one real fetch, never zero
        rec = ledger.records()[0]
        assert rec.status == EMPTY
        assert rec.in_pull_fetches == 1  # one fetch happened — not a misleading 0/"—"

    def test_empty_after_retries_logs_an_info_summary(self) -> None:
        # DEC-WINDOW-INPULL-FETCHES-001: the per-attempt empty_retry log is DEBUG, so a
        # pull at INFO showed NO sign the 3× retry ran. One INFO summary now surfaces it.
        adapter = _BarSeqAdapter([[]])  # always empty → exhausts the 3 fetches
        with capture_logs() as logs, pull_id_scope("emptypull01"):
            ingest_spot(
                adapter,  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 3, 1),
                empty_retry_attempts=3,
            )
        summary = [r for r in logs if r["event"] == "ingest.window.empty_after_retries"]
        assert len(summary) == 1
        assert summary[0]["fetches"] == 3  # the count the operator can finally SEE
        assert summary[0]["log_level"] == "info"

    def test_first_try_win_logs_no_empty_summary(self) -> None:
        # A clean first-try fetch stays quiet — the INFO summary fires ONLY on a
        # retry-then-still-empty window (the False side of the guard).
        adapter = _Adapter(bars=[_bar()])
        with capture_logs() as logs:
            ingest_spot(
                adapter,  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 3, 1),
                empty_retry_attempts=3,
            )
        assert not [r for r in logs if r["event"] == "ingest.window.empty_after_retries"]

    def test_data_on_first_attempt_does_not_retry(self) -> None:
        # The happy path is unchanged: bars on the first fetch → one call, no retry.
        from brutex.data.window_ledger import WindowLedger

        ledger = WindowLedger()
        adapter = _Adapter(bars=[_bar()])
        ingest_spot(
            adapter,  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            empty_retry_attempts=3,
            ledger=ledger,
        )
        assert adapter.calls == 1  # broke on the first attempt — no wasted re-fetch
        assert ledger.records()[0].in_pull_fetches == 1  # first-try win → 1 fetch

    def test_old_genuine_empty_fast_settles_in_one_pull(self) -> None:
        # DEC-OLD-GAP-FAST-SETTLE-001: an OLD genuinely-empty window (end in 2021, with
        # an injected 2026 clock) that exhausts the in-pull retry settles SKIPPED in ONE
        # pull — vendor data can never appear for a gap that old, so the conservative
        # 3-pull streak is wasted re-fetching.
        from brutex.data.window_ledger import SKIPPED, WindowLedger

        ledger = WindowLedger()
        adapter = _BarSeqAdapter([[]])  # always empty
        ingest_spot(
            adapter,  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),  # > 60 days before the injected now
            ledger=ledger,
            empty_retry_attempts=3,
            now_ist=lambda: datetime(2026, 6, 19, 8, 0, tzinfo=UTC),
        )
        rec = ledger.records()[0]
        assert rec.status == SKIPPED  # settled terminal in this single pull
        assert rec.empty_streak == 3  # forced to the threshold (a normal settle's shape)
        assert rec.attempts == 1  # ONE pull
        assert rec.in_pull_fetches == 3  # re-hit the full in-pull budget, all empty

    def test_recent_genuine_empty_keeps_conservative_streak(self) -> None:
        # DEC-OLD-GAP-FAST-SETTLE-001: a RECENT empty window (within OLD_GAP_HORIZON_DAYS
        # of the injected now) is NOT fast-settled — it might still be a just-missed
        # backfill, so it stays EMPTY 1/3, re-checked on a later pull.
        from brutex.data.window_ledger import EMPTY, WindowLedger

        ledger = WindowLedger()
        adapter = _BarSeqAdapter([[]])  # always empty
        ingest_spot(
            adapter,  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2026, 5, 1),
            end=date(2026, 6, 1),  # within 60 days of the injected now
            ledger=ledger,
            empty_retry_attempts=3,
            now_ist=lambda: datetime(2026, 6, 19, 8, 0, tzinfo=UTC),
        )
        rec = ledger.records()[0]
        assert rec.status == EMPTY  # NOT fast-settled — recent gap may still fill
        assert rec.empty_streak == 1  # conservative across-pull streak
        assert rec.in_pull_fetches == 3

    def test_old_empty_without_inpull_retry_is_not_fast_settled(self) -> None:
        # DEC-OLD-GAP-FAST-SETTLE-001: confirmed_empty_after_retry requires fetch_count>=2.
        # An OLD empty window with empty_retry_attempts=1 (no real retry → ONE fetch) is a
        # single glitch-prone read, NOT in-pull-confirmed, so it stays EMPTY (the degenerate
        # single-fetch case is explicitly excluded).
        from brutex.data.window_ledger import EMPTY, WindowLedger

        ledger = WindowLedger()
        adapter = _BarSeqAdapter([[]])  # always empty
        ingest_spot(
            adapter,  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),  # OLD window
            ledger=ledger,
            empty_retry_attempts=1,  # no in-pull retry → one fetch only
            now_ist=lambda: datetime(2026, 6, 19, 8, 0, tzinfo=UTC),
        )
        rec = ledger.records()[0]
        assert rec.status == EMPTY  # one fetch is not enough to fast-settle
        assert rec.in_pull_fetches == 1

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
        # The transient FAILED row carries the in-pull fetch count too (1 — it 429'd on
        # the first fetch this pull; DEC-WINDOW-INPULL-FETCHES-001).
        assert rec.in_pull_fetches == 1


class TestIngestSpotCompletenessRepull:
    """DEC-INTERIOR-GAP-DETECT-001 / DEC-PARTIAL-WINDOW-REPULL-001: a SUCCESS spot window
    whose stored bars are INCOMPLETE (interior multi-session hole OR trailing truncation) is
    DEMOTED to a re-pullable ledger status instead of silently settling SKIP-able SUCCESS."""

    def test_interior_gap_window_is_demoted_to_repullable(self) -> None:
        from brutex.data.window_ledger import SUCCESS, WindowLedger

        # One 1-minute window [2021-03-01, 2021-03-30) (< the 30-day tile, so ONE window) whose
        # bars cover the start, then a LONG contiguous interior hole (>5 sessions), then resume
        # near the end — a real interior completeness hole bookended by data.
        gappy = [
            _bar(datetime(2021, 3, 1, 4, 5, tzinfo=UTC)),
            _bar(datetime(2021, 3, 2, 4, 5, tzinfo=UTC)),
            _bar(datetime(2021, 3, 26, 4, 5, tzinfo=UTC)),
            _bar(datetime(2021, 3, 29, 4, 5, tzinfo=UTC)),
        ]
        led = WindowLedger()
        with capture_logs() as logs:
            report = ingest_spot(
                _Adapter(bars=gappy),  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1minute",
                start=date(2021, 3, 1),
                end=date(2021, 3, 30),
                ledger=led,
                detect_completeness_gaps=True,
            )
        assert report.windows == 1
        rec = led.records()[0]
        assert rec.status != SUCCESS  # demoted — NOT silently settled (so NOT skippable)
        assert rec.status not in {"SUCCESS", "SKIPPED"}  # the skip-authority set
        assert rec.last_error == "INTERIOR_GAP"
        assert any(e["event"] == "window.interior_gap" for e in logs)

    def test_truncated_window_is_demoted_to_repullable(self) -> None:
        from brutex.data.window_ledger import SUCCESS, WindowLedger

        # Bars stop on day 2 of a 30-day 1-minute window (a trailing truncation), far from the
        # expected end and from the live edge (the 2026 default clock) → demoted, not SUCCESS.
        truncated = [
            _bar(datetime(2021, 3, 1, 4, 5, tzinfo=UTC)),
            _bar(datetime(2021, 3, 2, 4, 5, tzinfo=UTC)),
        ]
        led = WindowLedger()
        with capture_logs() as logs:
            ingest_spot(
                _Adapter(bars=truncated),  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1minute",
                start=date(2021, 3, 1),
                end=date(2021, 4, 1),
                ledger=led,
                detect_completeness_gaps=True,
            )
        rec = led.records()[0]
        assert rec.status != SUCCESS
        assert rec.last_error == "SUSPECTED_PARTIAL"
        assert any(e["event"] == "window.suspected_partial" for e in logs)

    def test_complete_window_settles_success_no_demotion(self) -> None:
        from brutex.data.window_ledger import SUCCESS, WindowLedger

        # Bars run continuously to the window's last completed session → COMPLETE, settles
        # SUCCESS (skippable), with NO completeness warning fired.
        led = WindowLedger()
        with capture_logs() as logs:
            ingest_spot(
                _CompleteWindowAdapter(),  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1minute",
                start=date(2021, 3, 1),
                end=date(2021, 4, 1),
                ledger=led,
                detect_completeness_gaps=True,
            )
        rec = led.records()[0]
        assert rec.status == SUCCESS
        assert rec.last_error is None
        assert not any(e["event"] == "window.interior_gap" for e in logs)
        assert not any(e["event"] == "window.suspected_partial" for e in logs)

    def test_derived_1day_window_is_never_demoted(self) -> None:
        from brutex.data.window_ledger import SUCCESS, WindowLedger

        # The derived 1day interval is RESAMPLED from 1-minute, never vendor-fetched, so a
        # sparse daily window must NOT be demoted (it would re-pull a derived partition).
        led = WindowLedger()
        ingest_spot(
            _Adapter(bars=[_bar(datetime(2021, 3, 1, 4, 5, tzinfo=UTC))]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 3, 1),
            end=date(2021, 4, 1),
            ledger=led,
            detect_completeness_gaps=True,
        )
        rec = led.records()[0]
        assert rec.status == SUCCESS  # 1day is exempt from the re-pull demotion

    def test_default_no_detect_does_not_demote_a_dry_run_gappy_window(self) -> None:
        # The demotion is OPT-IN (detect_completeness_gaps defaults False) so a dry-run /
        # synthetic / offline pull (whose adapter returns first-day-only bars) is NEVER demoted
        # — it would otherwise false-red the dry-run. The WARNING still fires (visibility), but
        # the ledger row stays SUCCESS. This pins the production gate's default-OFF contract.
        from brutex.data.window_ledger import SUCCESS, WindowLedger

        gappy = [
            _bar(datetime(2021, 3, 1, 4, 5, tzinfo=UTC)),
            _bar(datetime(2021, 3, 2, 4, 5, tzinfo=UTC)),
            _bar(datetime(2021, 3, 26, 4, 5, tzinfo=UTC)),
            _bar(datetime(2021, 3, 29, 4, 5, tzinfo=UTC)),
        ]
        led = WindowLedger()
        with capture_logs() as logs:
            ingest_spot(
                _Adapter(bars=gappy),  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,  # an INDEX (is_index True) — only the opt-in flag is missing
                interval="1minute",
                start=date(2021, 3, 1),
                end=date(2021, 3, 30),
                ledger=led,
                # detect_completeness_gaps defaults False (dry-run / synthetic / test contract)
            )
        rec = led.records()[0]
        assert rec.status == SUCCESS  # NOT demoted — the opt-in gate is off
        assert rec.last_error is None
        # The observe-only WARNING STILL fires (visibility is never gated, only the re-pull).
        assert any(e["event"] == "window.interior_gap" for e in logs)

    def test_non_index_equity_is_warned_but_not_demoted(self) -> None:
        # A non-index (REFERENCE_ONLY equity) spot symbol can be legitimately sparse
        # (an illiquid micro-cap / a delisted name), so an interior hole there is NOT a
        # genuine completeness gap — it is OBSERVE-only WARNed but NEVER demoted (no re-pull
        # storm). The demotion gate is ``instrument.is_index``.
        from brutex.data.window_ledger import SUCCESS, WindowLedger

        equity = _inst("RELIANCE")
        equity = Instrument(
            exch_id=equity.exch_id,
            security_id=equity.security_id,
            symbol_name=equity.symbol_name,
            display_name=equity.display_name,
            instrument=InstrumentType.EQ,  # a non-index equity
            segment=Segment.CASH,
            series=None,
            isin=None,
            underlying_symbol=None,
            underlying_security_id=None,
            lot_size=1,  # EQ requires a positive lot size
            tick_size=Decimal("0.05"),
            sm_freeze_qty=None,
            sm_expiry_date=None,
            strike_price=None,
            asm_gsm_flag=False,
        )
        assert equity.is_index is False
        gappy = [
            _bar(datetime(2021, 3, 1, 4, 5, tzinfo=UTC)),
            _bar(datetime(2021, 3, 2, 4, 5, tzinfo=UTC)),
            _bar(datetime(2021, 3, 26, 4, 5, tzinfo=UTC)),
            _bar(datetime(2021, 3, 29, 4, 5, tzinfo=UTC)),
        ]
        led = WindowLedger()
        with capture_logs() as logs:
            ingest_spot(
                _Adapter(bars=gappy),  # type: ignore[arg-type]
                _Sink(),
                instrument=equity,
                interval="1minute",
                start=date(2021, 3, 1),
                end=date(2021, 3, 30),
                ledger=led,
                detect_completeness_gaps=True,
            )
        rec = led.records()[0]
        assert rec.status == SUCCESS  # NOT demoted — the equity stays settled
        assert rec.last_error is None
        # The observe-only WARNING STILL fires (visibility unchanged) even though it is not demoted.
        assert any(e["event"] == "window.interior_gap" for e in logs)


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


class _BreakerFeed:
    """Recording ``ErrorStormBreaker`` stand-in — captures the spine's feeds verbatim.

    Records the exact ``observe_transport_failure`` arguments (symbol, error_type,
    window_key, still_stale) so the auth-ladder-exclusion tests can pin the breaker
    feed BYTE-IDENTICAL to the pre-guard behaviour (DEC-AUTH-LADDER-EXCLUDE-001 —
    the guard withholds only the ``on_timeout`` step, never the containment chain).
    """

    def __init__(self) -> None:
        self.fed: list[tuple[str, str, object, bool]] = []
        self.successes: list[str] = []

    def observe_transport_failure(
        self, symbol: str, *, error_type: str, window_key: object, still_stale: bool = False
    ) -> None:
        self.fed.append((symbol, error_type, window_key, still_stale))

    def observe_success(self, symbol: str) -> None:
        self.successes.append(symbol)


class TestAuthLadderExclusion:
    """DEC-AUTH-LADDER-EXCLUDE-001 — retryable auth-family failures feed NEITHER rate hook.

    The AIMD timeout ladder is a CANDLE-endpoint server-stress signal; a token-stale /
    mint-throttle failure is credential state, so it must not step the pull rate down
    toward the 1 r/s deep floor — while the FAILED ledger record + the storm-breaker
    feed (the dead-credential escalation's evidence stream) stay byte-identical.
    """

    @pytest.mark.parametrize(
        "error",
        [
            GrowwTokenStillStaleError("same dead token"),
            GrowwTokenIssuanceThrottled("mint throttled"),
        ],
    )
    def test_retryable_auth_family_feeds_neither_hook(self, error: GrowwAuthError) -> None:
        from brutex.adapters.ingest_common import GROWW_COVERAGE_MARKET
        from brutex.data.fetch_coverage import WindowKey
        from brutex.data.window_ledger import FAILED, WindowLedger

        hits: list[int] = []
        timeouts: list[int] = []
        ledger = WindowLedger()
        breaker = _BreakerFeed()
        report = ingest_spot(
            _Adapter(error=error),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            on_rate_limit=lambda: hits.append(1),
            on_timeout=lambda: timeouts.append(1),
            ledger=ledger,
            storm_breaker=breaker,  # type: ignore[arg-type]
            max_retry_passes=0,
        )
        # NEITHER controller hook fires — no ladder step, no rate step (the regression:
        # before the guard, `on_timeout()` fired here and pinned the pull at 1 r/s).
        assert timeouts == []
        assert hits == []
        # Containment is BYTE-IDENTICAL: still counted transient (deferred, re-pullable)…
        assert report.transient_failures == 1
        # …the ledger row is FAILED with the honest error type + in-pull fetch count…
        [row] = ledger.records()
        assert row.status == FAILED
        assert row.last_error == type(error).__name__
        assert row.in_pull_fetches == 1
        # …and the storm breaker was fed EXACTLY once with the same key/exc shape
        # (the still_stale flag keeps routing the dead-credential streak).
        expected_key = WindowKey(
            market=GROWW_COVERAGE_MARKET,
            security_id="NIFTY",
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
        )
        expected_stale = isinstance(error, GrowwTokenStillStaleError)
        assert breaker.fed == [("NIFTY", type(error).__name__, expected_key, expected_stale)]
        assert breaker.successes == []

    def test_genuine_timeout_still_steps_ladder_with_identical_feed(self) -> None:
        """MUTATION-KILL: a real ``GrowwTimeoutError`` STILL steps the timeout ladder down.

        An inverted (``not steps_timeout_ladder``) or weakened (always-``False``)
        predicate fails HERE (``timeouts == []``); a dropped guard fails the
        auth-family test above. The ledger + breaker feeds on the stepping path
        are pinned identical too.
        """
        from brutex.data.window_ledger import FAILED, WindowLedger

        timeouts: list[int] = []
        ledger = WindowLedger()
        breaker = _BreakerFeed()
        ingest_spot(
            _Adapter(error=GrowwTimeoutError("stall")),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            on_timeout=lambda: timeouts.append(1),
            ledger=ledger,
            storm_breaker=breaker,  # type: ignore[arg-type]
            max_retry_passes=0,
        )
        assert timeouts == [1]  # the ladder step still fires for genuine server stress
        [row] = ledger.records()
        assert row.status == FAILED
        assert row.last_error == "GrowwTimeoutError"
        assert breaker.fed[0][1] == "GrowwTimeoutError" and breaker.fed[0][3] is False


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
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
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


class TestIngestSpotAsyncPipeline:
    """DEC-GROWW-FETCH-ASYNC-SEPARATION-001 — the async-separation branch of ``_ingest_one_window``.

    When a :class:`FetchPipeline` is wired, the SUCCESS-path tail (session-filter +
    ``sink.append`` + conflict-aggregate + ``ledger.record``) runs on a BACKGROUND consumer,
    NOT the fetch worker. The producer submits the raw bars and returns a DEFERRED zero-bar
    window; the consumer folds the real bars/conflicts onto the pipeline's pull-level +
    per-series totals. Covers lines 598-637 (submit + ``_run_tail`` fold) + 647 (the spot
    series shard-key the tail routes by).
    """

    def test_deferred_window_folds_bars_onto_pipeline_totals(self) -> None:
        from brutex.adapters.groww.fetch_pipeline import FetchPipeline

        sink = _Sink()
        pipeline = FetchPipeline(workers=1, queue_maxsize=8)
        try:
            # 1day tiles at 180 days; ~212-day range → 2 windows, each a single in-session bar.
            report = ingest_spot(
                _Adapter(),  # type: ignore[arg-type]
                sink,
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 8, 1),
                run_parallel=_sync_run_parallel,
                fetch_pipeline=pipeline,
            )
        finally:
            pipeline.drain()
        # Producer DEFERRED: 2 windows fetched, but the per-series report reads zero bars
        # (the durable work + count land on the pipeline, not this synchronous report).
        assert report.windows == 2
        assert report.bars_appended == 0
        assert report.transient_failures == 0 and report.still_failed_windows == 0
        # The BACKGROUND consumer durably appended both windows (one bar each) and folded
        # them onto the pipeline's pull-level + per-series totals (lines 623-627 + 647 route).
        assert sink.appends == [1, 1]
        processed, bars, conflicts = pipeline.totals
        assert (processed, bars, conflicts) == (2, 2, 0)
        series = pipeline.per_series_totals()
        assert series[("NIFTY", "1day")].bars_appended == 2
        assert series[("NIFTY", "1day")].conflicts == 0

    def test_deferred_window_folds_conflicts_onto_pipeline_totals(self) -> None:
        from brutex.adapters.groww.fetch_pipeline import FetchPipeline

        sink = _ConflictSink(conflicts=(_make_conflict(),), bars_appended=0)
        pipeline = FetchPipeline(workers=1, queue_maxsize=8)
        try:
            report = ingest_spot(
                _Adapter(),  # type: ignore[arg-type]
                sink,
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 3, 1),
                run_parallel=_sync_run_parallel,
                fetch_pipeline=pipeline,
            )
        finally:
            pipeline.drain()
        assert report.windows == 1
        # The consumer folded the keep-first conflict onto the pipeline (the conflicts arm of
        # fold_totals, line 623-627), not the producer's deferred report.
        assert report.conflicts == 0
        series = pipeline.per_series_totals()
        assert series[("NIFTY", "1day")].conflicts == 1
        _, _, pool_conflicts = pipeline.totals
        assert pool_conflicts == 1

    def test_tail_lock_timeout_folds_transient_onto_pipeline(self) -> None:
        from brutex.adapters.groww.fetch_pipeline import FetchPipeline

        sink = _LockTimeoutSink()
        pipeline = FetchPipeline(workers=1, queue_maxsize=8)
        try:
            # A clean fetch, but the BACKGROUND tail's sink.append hits a wedged-peer lock
            # timeout → result_outcome.transient → pipeline.fold_tail_transient (lines 615-622).
            report = ingest_spot(
                _Adapter(),  # type: ignore[arg-type]
                sink,
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 3, 1),
                run_parallel=_sync_run_parallel,
                fetch_pipeline=pipeline,
            )
        finally:
            pipeline.drain()
        # Producer DEFERRED a clean fetch; the transient is owned by the consumer.
        assert report.windows == 1
        assert report.transient_failures == 0 and report.still_failed_windows == 0
        assert sink.calls == 1  # the fetch ran; the async tail hit the wedged lock once
        # The consumer folded ONE tail-transient (the async still_failed_windows for the
        # across-pull retry round), attributed to the right series key.
        assert pipeline.tail_transients == 1
        series = pipeline.per_series_totals()
        assert series[("NIFTY", "1day")].transient == 1

    def test_spine_wires_a_rehydrate_hook_that_swaps_bars_in_place(self) -> None:
        """DEC-FETCH-NONBLOCKING-HANDOFF-001 — the spot spine wires the spill ``rehydrate`` hook.

        The async spine builds a :class:`~brutex.adapters.groww.fetch_pipeline.FetchTail` whose
        ``rehydrate`` swaps the window's bars in place in the captured ``raw`` (the pipeline calls
        it ``rehydrate(())`` on spill / ``rehydrate(loaded)`` on reload). Captured via a fake
        pipeline so the swap is asserted DETERMINISTICALLY (no slow-disk race): calling the wired
        ``rehydrate`` mutates the SAME ``raw_bars`` list the consumer's ``process`` reads, so after
        a reload ``_process_window_tail`` appends the RESTORED bars (covers ``ingest.py`` line 635).
        """
        from brutex.adapters.groww.fetch_pipeline import RawFetchResult

        class _CapturePipeline:
            """Captures the submitted tail (no thread) so the spill hook can be driven by hand."""

            def __init__(self) -> None:
                self.captured: RawFetchResult | None = None
                self.folded: tuple[int, int] | None = None

            def submit(self, result: RawFetchResult) -> None:
                self.captured = result

            def fold_totals(
                self,
                *,
                bars_appended: int,
                conflicts: int,
                series_key: object,
                vendor_dup_rows_dropped: int = 0,
                max_close_delta: object = None,
            ) -> None:
                del series_key, vendor_dup_rows_dropped, max_close_delta
                self.folded = (bars_appended, conflicts)

        sink = _Sink()
        pipeline = _CapturePipeline()
        report = ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            sink,
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            run_parallel=_sync_run_parallel,
            fetch_pipeline=pipeline,  # type: ignore[arg-type]
        )
        # Producer DEFERRED: one window submitted, zero bars on the synchronous report.
        assert report.windows == 1
        assert report.bars_appended == 0
        captured = pipeline.captured
        assert captured is not None
        tail = captured.tail
        assert tail.rehydrate is not None
        # The pipeline drops the bulk on spill, then RESTORES them on reload. Drive the wired
        # hook by hand: drop, then restore TWO fresh bars (a different count proves the swap).
        tail.rehydrate(())  # spill: drop the in-memory bulk
        assert list(tail.raw_bars) == []
        restored = [_bar(), _bar(datetime(2021, 1, 5, 4, 5, tzinfo=UTC))]
        tail.rehydrate(restored)  # reload: restore before the consumer processes
        assert list(tail.raw_bars) == restored
        # Now the consumer's ``process`` reads the RESTORED bars (both in-session) + appends them,
        # then folds the real totals onto the pipeline (proving the rehydrated bars reached the
        # durable write, not a stale captured copy).
        tail.process(tail)
        assert sink.appends == [2]
        assert pipeline.folded == (2, 0)


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
        for ws, we in (
            (date(2021, 1, 1), date(2021, 6, 30)),
            (date(2021, 6, 30), date(2021, 8, 1)),
        ):
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


class TestEmptyBreakerSpotSpine:
    """DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 — the mass-empty breaker on the spot spine."""

    def test_trip_halts_pull_and_leaves_windows_retryable(self) -> None:
        # Always-empty adapter over an 8-window intraday range + a trip-at-the-2nd-
        # observation FRACTION breaker (the throttle-from-start signature — an
        # all-empty fresh pull can only trip the fraction rule; the consecutive rule
        # requires a post-data empty): window 1 records (settled), window 2's empty
        # TRIPS → MassEmptySettleHalt propagates out of ingest_spot (the global
        # stop), window 2 is NEVER recorded (stays PLANNED-absent → retryable) and
        # window 3 is never fetched.
        from brutex.adapters.empty_circuit_breaker import EmptySettleBreaker, MassEmptySettleHalt
        from brutex.data.window_ledger import SKIPPED, WindowLedger

        ledger = WindowLedger()
        adapter = _BarSeqAdapter([[]])  # always empty
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=1.0,
            min_observations=2,
            fraction_min_series=1,
        )
        with pytest.raises(MassEmptySettleHalt) as excinfo:
            ingest_spot(
                adapter,  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 8, 1),  # 30-day 1-minute tiles → 8 windows
                ledger=ledger,
                empty_retry_attempts=2,
                run_parallel=_sync_run_parallel,
                now_ist=lambda: datetime(2026, 6, 19, 8, 0, tzinfo=UTC),
                empty_breaker=breaker,
            )
        assert excinfo.value.consecutive_empties == 2
        assert breaker.tripped
        # Window 1 settled BEFORE the trip (old + confirmed) — reversible via
        # reopen_skipped, and the verdict layer surfaces its count.
        records = ledger.records()
        assert len(records) == 1  # ONLY window 1 has a row — 2 was never recorded
        assert records[0].status == SKIPPED
        assert ledger.newly_settled_skipped == 1
        # The un-recorded windows are NOT skippable → a re-pull re-fetches them.
        from brutex.data.fetch_coverage import WindowKey

        w2 = WindowKey(
            market="groww",
            security_id="NIFTY",
            interval="1minute",
            start=date(2021, 1, 31),
            end=date(2021, 3, 2),
        )
        assert not ledger.is_skippable(w2)

    def test_resume_after_trip_refetches_unsettled_windows(self) -> None:
        # Recovery: after a tripped pull halts, the next pull (a FRESH breaker — no
        # state leak) re-fetches every un-settled window; with the throttle cleared
        # (the adapter now returns bars) they settle SUCCESS.
        from brutex.adapters.empty_circuit_breaker import EmptySettleBreaker, MassEmptySettleHalt
        from brutex.data.window_ledger import SUCCESS, WindowLedger

        ledger = WindowLedger()
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=1.0,
            min_observations=2,
            fraction_min_series=1,
        )
        with pytest.raises(MassEmptySettleHalt):
            ingest_spot(
                _BarSeqAdapter([[]]),  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 8, 1),
                ledger=ledger,
                empty_retry_attempts=2,
                run_parallel=_sync_run_parallel,
                now_ist=lambda: datetime(2026, 6, 19, 8, 0, tzinfo=UTC),
                empty_breaker=breaker,
            )
        # Next pull: fresh breaker, throttle over — data flows again.
        adapter = _CompleteWindowAdapter()
        report = ingest_spot(
            adapter,  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            ledger=ledger,
            run_parallel=_sync_run_parallel,
            now_ist=lambda: datetime(2026, 6, 19, 8, 0, tzinfo=UTC),
            empty_breaker=EmptySettleBreaker(
                min_series=1,
                consecutive_limit=0,
                fraction_limit=1.0,
                min_observations=2,
                fraction_min_series=1,
            ),
        )
        # Window 1 settled SKIPPED in pull 1 → skipped; the 7 others fetched + SUCCESS.
        assert report.skipped_windows == 1
        assert adapter.calls == 7
        statuses = sorted(r.status for r in ledger.records())
        assert statuses.count(SUCCESS) == 7

    def test_below_threshold_empties_still_settle(self) -> None:
        # A breaker that never trips leaves the old-gap fast-settle byte-unchanged —
        # the mixed real+empty below-threshold pull settles (the LOUD verdict is the
        # controller layer's job, pinned in test_dashboard_pull_controller.py).
        from brutex.adapters.empty_circuit_breaker import EmptySettleBreaker
        from brutex.data.window_ledger import SKIPPED, WindowLedger

        ledger = WindowLedger()
        ingest_spot(
            _BarSeqAdapter([[]]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            ledger=ledger,
            empty_retry_attempts=3,
            now_ist=lambda: datetime(2026, 6, 19, 8, 0, tzinfo=UTC),
            empty_breaker=EmptySettleBreaker(
                min_series=1, consecutive_limit=40, fraction_limit=0.8, min_observations=50
            ),
        )
        rec = ledger.records()[0]
        assert rec.status == SKIPPED  # the fast-settle still fires below thresholds
        assert ledger.newly_settled_skipped == 1  # …and is COUNTED for the verdict

    def test_tripped_breaker_suppresses_settle_at_record_time(self) -> None:
        # An in-flight window whose settle was computed BEFORE the trip but recorded
        # AFTER it (async tail / sibling worker) must NOT settle: the tail re-checks
        # allow_settle() and records an UNTRUSTED empty — EMPTY status, streak
        # PRESERVED (no poisoned progress toward the across-pull promotion).
        from brutex.adapters.empty_circuit_breaker import EmptySettleBreaker, MassEmptySettleHalt
        from brutex.adapters.groww.ingest import _process_window_tail, _RawWindow
        from brutex.data.fetch_coverage import WindowKey
        from brutex.data.window_ledger import EMPTY, WindowLedger

        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=1.0,
            min_observations=1,
            fraction_min_series=1,
        )
        with pytest.raises(MassEmptySettleHalt):
            breaker.observe_empty("NIFTY:1minute", symbol="NIFTY", window_end=date(2021, 4, 1))
        assert not breaker.allow_settle()
        ledger = WindowLedger()
        key = WindowKey(
            market="groww",
            security_id="NIFTY",
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 4, 1),
        )
        raw = _RawWindow(
            instrument=_INST,
            interval="1minute",
            window=(date(2021, 1, 1), date(2021, 4, 1)),
            key=key,
            raw_bars=[],
            fetch_count=2,
            settle_empty=True,  # computed pre-trip on the producer
        )
        outcome = _process_window_tail(
            _Sink(),
            raw,
            ledger=ledger,
            on_success=None,
            pull_id="p",
            series_fields={"symbol": "NIFTY", "interval": "1minute"},
            empty_breaker=breaker,
        )
        assert outcome.bars_appended == 0
        rec = ledger.records()[0]
        assert rec.status == EMPTY  # NOT SKIPPED — the settle was suppressed
        assert rec.empty_streak == 0  # UNTRUSTED empty: streak preserved, not bumped
        assert not ledger.is_skippable(key)  # fully retryable
        assert ledger.newly_settled_skipped == 0

    def test_pre_listing_predicate_excludes_windows_from_trip(self) -> None:
        # A breaker with a listing-date source: PROVABLY pre-listing empty windows are
        # observed but never counted, so an all-pre-listing symbol cannot trip it and
        # its old empties still settle (a genuine pre-listing gap, register rule 4).
        from brutex.adapters.empty_circuit_breaker import EmptySettleBreaker
        from brutex.data.window_ledger import SKIPPED, WindowLedger

        ledger = WindowLedger()
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=0.5,  # would trip at the 2nd counted empty…
            min_observations=2,
            fraction_min_series=1,
            expected_empty=lambda _symbol, window_end: window_end < date(2023, 1, 1),
        )
        report = ingest_spot(
            _BarSeqAdapter([[]]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),  # 8 windows, ALL provably pre-listing
            ledger=ledger,
            empty_retry_attempts=2,
            run_parallel=_sync_run_parallel,
            now_ist=lambda: datetime(2026, 6, 19, 8, 0, tzinfo=UTC),
            empty_breaker=breaker,
        )
        assert not breaker.tripped  # …but every empty was provably pre-listing
        assert report.windows == 8
        assert breaker.counts() == (8, 0, 0)  # observed, never counted
        assert all(r.status == SKIPPED for r in ledger.records())  # settled honestly

    def test_empty_dominated_resume_does_not_retrip(self) -> None:
        # REGRESSION (hostile review MAJOR-1, scenario "empty-dominated resume"):
        # pull 1 records the symbol's windows EMPTY (below the SKIPPED threshold —
        # single-fetch, no in-pull confirmation). Pull 2 re-fetches them and they
        # re-confirm empty — the spine passes expected=True from the prior-EMPTY
        # ledger rows (`is_expected_empty_recheck`), so a breaker that WOULD trip on
        # two counted empties (the pre-redesign behaviour) never counts them.
        from brutex.adapters.empty_circuit_breaker import EmptySettleBreaker
        from brutex.data.window_ledger import EMPTY, WindowLedger

        ledger = WindowLedger()
        ingest_spot(
            _BarSeqAdapter([[]]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),  # 8 windows, all EMPTY (streak 1, unsettled)
            ledger=ledger,
            empty_retry_attempts=1,  # single fetch → no fast-settle confirmation
            run_parallel=_sync_run_parallel,
            now_ist=lambda: datetime(2026, 6, 19, 8, 0, tzinfo=UTC),
        )
        assert all(r.status == EMPTY for r in ledger.records())
        # The RESUME: every window re-fetches (EMPTY is not skippable) and
        # re-confirms empty. This breaker trips at 2 counted empties (0.5 of 2) —
        # exactly what the old design did here — but the prior-EMPTY hint keeps
        # every re-confirmation uncounted.
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=0.5,
            min_observations=2,
            fraction_min_series=1,
        )
        report = ingest_spot(
            _BarSeqAdapter([[]]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            ledger=ledger,
            empty_retry_attempts=1,
            run_parallel=_sync_run_parallel,
            now_ist=lambda: datetime(2026, 6, 19, 8, 0, tzinfo=UTC),
            empty_breaker=breaker,
        )
        assert not breaker.tripped
        assert report.windows == 8
        assert breaker.counts() == (8, 0, 0)  # observed, never counted

    def test_reopened_windows_are_breaker_exempt_on_reverify(self) -> None:
        # Minor M-1: after `brutex reopen-skipped`, the re-verify pull re-fetches the
        # REOPENED windows deliberately — their re-confirmed empties are exempt from
        # the trip tally, so reopen + re-pull can never re-trip endlessly, and the
        # genuinely-empty windows re-settle honestly.
        from brutex.adapters.empty_circuit_breaker import EmptySettleBreaker
        from brutex.data.window_ledger import SKIPPED, WindowLedger

        ledger = WindowLedger()
        ingest_spot(
            _BarSeqAdapter([[]]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            ledger=ledger,
            empty_retry_attempts=2,  # confirmed + old → all 8 fast-settle SKIPPED
            run_parallel=_sync_run_parallel,
            now_ist=lambda: datetime(2026, 6, 19, 8, 0, tzinfo=UTC),
        )
        assert all(r.status == SKIPPED for r in ledger.records())
        assert ledger.reopen_skipped() == 8  # the operator re-opens everything
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=0,
            fraction_limit=0.5,  # would trip at the 2nd counted empty…
            min_observations=2,
            fraction_min_series=1,
        )
        ingest_spot(
            _BarSeqAdapter([[]]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            ledger=ledger,
            empty_retry_attempts=2,
            run_parallel=_sync_run_parallel,
            now_ist=lambda: datetime(2026, 6, 19, 8, 0, tzinfo=UTC),
            empty_breaker=breaker,
        )
        assert not breaker.tripped  # …but REOPENED re-checks are breaker-exempt
        assert breaker.counts() == (8, 0, 0)
        # The deliberate re-verify re-settled the genuinely-empty windows honestly.
        assert all(r.status == SKIPPED for r in ledger.records())

    def test_recent_listing_prefix_never_trips_consecutive(self) -> None:
        # REGRESSION (hostile review MAJOR-1, scenario "targeted pull of recent
        # listings"): a recently-listed symbol's 2020→today backfill starts with a
        # long run of pre-listing empties BEFORE its first bar. The pre-redesign
        # consecutive rule tripped inside that prefix; the post-data requirement
        # makes the prefix structurally ineligible, data then retro-clears it.
        from brutex.adapters.empty_circuit_breaker import EmptySettleBreaker
        from brutex.data.window_ledger import SKIPPED, SUCCESS, WindowLedger

        listing = date(2021, 5, 20)

        class _ListedAdapter:
            """Empty before *listing*, one trailing in-session bar after."""

            def fetch_ohlcv(
                self, *, symbol: str, exchange: object, interval: str, start: date, end: date
            ) -> list[CandleBar]:
                del symbol, exchange, interval, start
                if end <= listing:
                    return []
                last_session = last_completed_session_on_or_before(end - timedelta(days=1))
                ts = datetime(
                    last_session.year, last_session.month, last_session.day, 4, 5, tzinfo=UTC
                )
                return [_bar(ts)]

        ledger = WindowLedger()
        breaker = EmptySettleBreaker(
            min_series=1,
            consecutive_limit=3,  # the OLD rule tripped on the 3rd prefix empty
            fraction_limit=1.0,
            min_observations=999,
        )
        report = ingest_spot(
            _ListedAdapter(),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),  # 8 windows: 4 pre-listing empties then data
            ledger=ledger,
            empty_retry_attempts=2,
            run_parallel=_sync_run_parallel,
            now_ist=lambda: datetime(2026, 6, 19, 8, 0, tzinfo=UTC),
            empty_breaker=breaker,
        )
        assert not breaker.tripped
        assert report.windows == 8
        _observed, empties, consecutive = breaker.counts()
        assert empties == 0  # first data retro-cleared the whole leading prefix
        assert consecutive == 0
        statuses = [r.status for r in ledger.records()]
        assert statuses.count(SKIPPED) == 4  # the genuine pre-listing gap settled
        assert statuses.count(SUCCESS) == 4  # …and the listed range landed bars


class TestWriteHealthBreakerSpotSpine:
    """DEC-WRITE-HEALTH-BREAKER-001 — the spot spine's write-health breaker wiring."""

    def test_wired_breaker_escalates_multi_partition_wedge_to_volume_halt(self) -> None:
        """A wedged VOLUME (defers across >= 3 distinct partitions) HALTS the pull.

        Regression for the pre-breaker envelope: every deferred window was independent,
        so a dead volume burned the whole pull deferring window after window. With the
        breaker wired, the third distinct wedged partition raises
        ``LakeWriteHalt("volume_wedged")`` out of the spine.
        """
        from brutex.data.errors import LakeWriteHalt
        from brutex.data.window_ledger import FAILED, WindowLedger
        from brutex.data.write_breaker import WriteHealthBreaker

        breaker = WriteHealthBreaker(consecutive_limit=1, distinct_open_limit=3)
        led = WindowLedger()
        with pytest.raises(LakeWriteHalt) as excinfo:
            ingest_spot(
                _Adapter(),  # type: ignore[arg-type]
                _LockTimeoutSink(),
                instrument=_INST,
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 8, 1),  # 8 windows -> 8 distinct month partitions
                ledger=led,
                max_retry_passes=0,
                write_breaker=breaker,
            )
        assert excinfo.value.reason == "volume_wedged"
        # Every window deferred BEFORE the trip is durably FAILED — never lost: the
        # ledger row is recorded BEFORE the breaker observes the defer.
        recorded = list(led.records())
        assert recorded
        assert {r.status for r in recorded} == {FAILED}
        assert len(breaker.open_partitions) >= 3

    def test_shipped_default_limits_escalate_via_retry_passes(self) -> None:
        """SHIPPED-DEFAULTS regression (review MAJOR P3-1): spot trips at 2 consecutive / 3 distinct.

        No injected limits — ``WriteHealthBreaker()`` carries the shipped defaults and
        ``max_retry_passes`` stays at its default (2). A spot month key reaches the
        consecutive limit via the bounded IN-RUN retry pass: pass 0 defers every window
        (streak 1 per month key), the retry pass defers them again (streak 2 → open), and
        the third opened key raises ``LakeWriteHalt("volume_wedged")``.
        """
        from brutex.data.errors import LakeWriteHalt
        from brutex.data.window_ledger import FAILED, WindowLedger
        from brutex.data.write_breaker import WriteHealthBreaker

        breaker = WriteHealthBreaker()  # shipped default limits — nothing injected
        led = WindowLedger()
        with pytest.raises(LakeWriteHalt) as excinfo:
            ingest_spot(
                _Adapter(),  # type: ignore[arg-type]
                _LockTimeoutSink(),
                instrument=_INST,
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 8, 1),  # 8 windows -> 8 distinct month keys
                ledger=led,
                retry_backoff=lambda _i: None,  # no real sleep between passes
                write_breaker=breaker,
            )
        assert excinfo.value.reason == "volume_wedged"
        recorded = list(led.records())
        assert recorded
        assert {r.status for r in recorded} == {FAILED}
        assert len(breaker.open_partitions) >= 3

    def test_below_distinct_limit_keeps_todays_defer_behaviour(self) -> None:
        """Defers on FEWER distinct partitions than the limit never halt (deferred only)."""
        from brutex.data.window_ledger import FAILED, WindowLedger
        from brutex.data.write_breaker import WriteHealthBreaker

        breaker = WriteHealthBreaker(consecutive_limit=1, distinct_open_limit=3)
        led = WindowLedger()
        report = ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            _LockTimeoutSink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            # 3 windows starting 2021-01-01 / 01-31 / 03-02 -> 2 DISTINCT month keys.
            end=date(2021, 4, 1),
            ledger=led,
            max_retry_passes=0,
            write_breaker=breaker,
        )
        assert report.transient_failures == 3
        assert {r.status for r in led.records()} == {FAILED}
        assert len(breaker.open_partitions) == 2

    def test_successful_write_resets_partition_streak(self) -> None:
        """A durable write calls ``observe_success`` on the partition's health key."""
        from brutex.data.write_breaker import WriteHealthBreaker

        class _SpyBreaker(WriteHealthBreaker):
            def __init__(self) -> None:
                super().__init__()
                self.defers: list[str] = []
                self.successes: list[str] = []

            def observe_defer(self, partition_key: str) -> None:
                self.defers.append(partition_key)
                super().observe_defer(partition_key)

            def observe_success(self, partition_key: str) -> None:
                self.successes.append(partition_key)
                super().observe_success(partition_key)

        spy = _SpyBreaker()
        report = ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            write_breaker=spy,
        )
        assert report.bars_appended > 0
        assert spy.defers == []
        assert spy.successes  # every durable window write reset its partition streak
        assert all("/NIFTY/1day/" in key for key in spy.successes)


class _NetNewSink:
    """Recording sink whose result carries an EXPLICIT deduped ``bars_net_new``.

    ``bars_net_new = bars_appended - dedup_hidden`` so the two counts DIFFER —
    the threading test proves the ledger stores the sink's deduped count, not a
    mirror of the input count.
    """

    def __init__(self, *, dedup_hidden: int = 1) -> None:
        self._dedup_hidden = dedup_hidden
        self.appends: list[int] = []

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
        del exchange, segment, symbol, interval
        self.appends.append(len(bars))
        return SinkAppendResult(
            bars_appended=len(bars),
            partitions_written=1,
            write_results=(),
            bars_net_new=max(len(bars) - self._dedup_hidden, 0),
        )


class TestIngestSpotBoundaryIntegrity:
    """DEC-INGEST-BOUNDARY-INTEGRITY-001 on the SPOT spine: the calendar-aware
    boundary filter (one WARN per window, muhurat KEPT, holiday dropped), the
    ALL_FILTERED demotion (gated on detect_completeness_gaps), the
    dedup-orthogonality contract, and the persisted ``bars_net_new``."""

    # 2021-01-26 (Republic Day, Tuesday) 10:05 IST — in-window wall clock, NO session.
    _HOLIDAY_TS = datetime(2021, 1, 26, 4, 35, tzinfo=UTC)
    # 2021-01-04 09:00 IST — pre-open on a regular trading day.
    _PRE_OPEN_TS = datetime(2021, 1, 4, 3, 30, tzinfo=UTC)
    # 2021-11-04 18:30 IST (= 13:00 UTC) — inside the Diwali muhurat 18:15–19:15.
    _MUHURAT_TS = datetime(2021, 11, 4, 13, 0, tzinfo=UTC)

    def test_session_filter_warns_exactly_once_per_window_with_counts(self) -> None:
        # One window, three bars: kept / pre-open (off-session) / holiday (non-trading
        # date). EXACTLY ONE aggregated WARNING carrying the window identity + counts.
        bars = [_bar(_IN_SESSION_TS), _bar(self._PRE_OPEN_TS), _bar(self._HOLIDAY_TS)]
        sink = _Sink()
        with capture_logs() as logs:
            report = ingest_spot(
                _Adapter(bars=bars),  # type: ignore[arg-type]
                sink,
                instrument=_INST,
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 1, 31),  # exactly one 30-day window
            )
        assert report.bars_appended == 1
        assert sink.appends == [1]
        warns = _events(logs, "ingest.session_filter.dropped")
        assert len(warns) == 1
        assert warns[0]["dropped"] == 2
        assert warns[0]["off_session"] == 1
        assert warns[0]["non_trading_date"] == 1
        assert warns[0]["window_start"] == "2021-01-01"
        assert warns[0]["window_end"] == "2021-01-31"
        assert warns[0]["symbol"] == "NIFTY"

    def test_clean_window_emits_no_session_filter_warn(self) -> None:
        with capture_logs() as logs:
            ingest_spot(
                _Adapter(bars=[_bar(_IN_SESSION_TS)]),  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 1, 31),
            )
        assert _events(logs, "ingest.session_filter.dropped") == []

    def test_muhurat_evening_bar_is_now_stored(self) -> None:
        # SANCTIONED BEHAVIOR CHANGE (DEC-INGEST-BOUNDARY-INTEGRITY-001): the
        # date-blind filter wrongly DROPPED an evening Diwali-muhurat bar; the
        # calendar-aware boundary now stores it.
        sink = _Sink()
        with capture_logs() as logs:
            report = ingest_spot(
                _Adapter(bars=[_bar(self._MUHURAT_TS)]),  # type: ignore[arg-type]
                sink,
                instrument=_INST,
                interval="1minute",
                start=date(2021, 11, 1),
                end=date(2021, 11, 30),  # one window
            )
        assert report.bars_appended == 1  # KEPT (was 0 under the old filter)
        assert sink.appends == [1]
        assert _events(logs, "ingest.session_filter.dropped") == []

    def test_holiday_bar_is_dropped_and_counted(self) -> None:
        # A bar on a published holiday whose wall clock LOOKS in-session was
        # previously stored (the date-blind defect) — now excluded + counted.
        sink = _Sink()
        with capture_logs() as logs:
            report = ingest_spot(
                _Adapter(bars=[_bar(self._HOLIDAY_TS)]),  # type: ignore[arg-type]
                sink,
                instrument=_INST,
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 1, 31),
            )
        assert report.bars_appended == 0
        assert sink.appends == [0]
        warns = _events(logs, "ingest.session_filter.dropped")
        assert len(warns) == 1
        assert warns[0]["non_trading_date"] == 1

    def test_all_filtered_window_is_demoted_to_repullable(self) -> None:
        # GAP (c): raw bars > 0 but EVERY one session-filtered → the would-be
        # SUCCESS (raw_bars judges it) is demoted FAILED + ALL_FILTERED so the
        # window is re-examinable, riding the #1031 degraded machinery.
        from brutex.data.session import ALL_FILTERED
        from brutex.data.window_ledger import WindowLedger

        led = WindowLedger()
        ingest_spot(
            _Adapter(bars=[_bar(self._HOLIDAY_TS)]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 1, 31),
            ledger=led,
            detect_completeness_gaps=True,
        )
        rec = led.records()[0]
        assert rec.status == "FAILED"  # re-pullable, NOT skippable
        assert rec.last_error == ALL_FILTERED
        assert rec.empty_streak == 1  # the bounded-settle machinery is engaged

    def test_all_filtered_settles_skipped_after_threshold(self) -> None:
        # BOUNDED (data-safe): after EMPTY_TERMINAL_THRESHOLD consecutive
        # all-filtered pulls the window settles SKIPPED so the pull converges.
        from brutex.data.window_ledger import EMPTY_TERMINAL_THRESHOLD, SKIPPED, WindowLedger

        led = WindowLedger()
        for _ in range(EMPTY_TERMINAL_THRESHOLD):
            ingest_spot(
                _Adapter(bars=[_bar(self._HOLIDAY_TS)]),  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 1, 31),
                ledger=led,
                detect_completeness_gaps=True,
            )
        assert led.records()[0].status == SKIPPED

    def test_all_filtered_not_demoted_without_detect_flag(self) -> None:
        # The demotion is gated on detect_completeness_gaps (like #1031) because
        # the dry-run synthetic adapter anchors bars on the window-START day
        # regardless of the calendar — a weekend-start dry-run window would
        # otherwise false-red. The WARN still fires (visibility ungated).
        from brutex.data.window_ledger import SUCCESS, WindowLedger

        led = WindowLedger()
        with capture_logs() as logs:
            ingest_spot(
                _Adapter(bars=[_bar(self._HOLIDAY_TS)]),  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 1, 31),
                ledger=led,
                # detect_completeness_gaps defaults False — dry-run/synthetic contract
            )
        rec = led.records()[0]
        assert rec.status == SUCCESS
        assert rec.last_error is None
        assert len(_events(logs, "ingest.session_filter.dropped")) == 1

    def test_dedup_to_zero_net_new_stays_success(self) -> None:
        # ORTHOGONALITY (GAP c): a re-pull whose bars all dedup to ZERO net-new in
        # the sink still passes bars THROUGH the filter, so it stays SUCCESS —
        # the all-filtered discriminator reads the FILTER survivors, never the
        # sink's dedup count. (_Sink models the full-dedup case: bars_net_new=0.)
        from brutex.data.window_ledger import SUCCESS, WindowLedger

        led = WindowLedger()
        ingest_spot(
            _CompleteWindowAdapter(),  # type: ignore[arg-type]
            _Sink(),  # returns bars_appended=N, bars_net_new=0 (all deduped)
            instrument=_INST,
            interval="1minute",
            start=date(2021, 3, 1),
            end=date(2021, 4, 1),
            ledger=led,
            detect_completeness_gaps=True,
        )
        rec = led.records()[0]
        assert rec.status == SUCCESS  # never demoted by a dedup-to-zero write
        assert rec.bars == 1
        assert rec.bars_net_new == 0  # GAP (d): the deduped count is persisted

    def test_genuine_empty_window_is_byte_unchanged(self) -> None:
        # raw_bars == 0 stays EMPTY (the all-filtered discriminator requires
        # raw bars) — the genuine-EMPTY path is untouched by this pack.
        from brutex.data.window_ledger import EMPTY, WindowLedger

        led = WindowLedger()
        ingest_spot(
            _Adapter(bars=[]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 1, 31),
            ledger=led,
            detect_completeness_gaps=True,
            # RECENT clock → conservative EMPTY, not the old-gap fast-settle.
            now_ist=lambda: datetime(2021, 2, 5, 12, 0, tzinfo=UTC),
        )
        rec = led.records()[0]
        assert rec.status == EMPTY
        assert rec.last_error is None

    def test_bars_net_new_threaded_from_sink_to_ledger(self) -> None:
        # GAP (d): the ledger row stores the SINK's deduped net-new count — a
        # value deliberately DIFFERENT from bars_appended so a mirror would fail.
        from brutex.data.window_ledger import WindowLedger

        led = WindowLedger()
        bars = [
            _bar(datetime(2021, 1, 4, 4, 5, tzinfo=UTC)),
            _bar(datetime(2021, 1, 4, 4, 6, tzinfo=UTC)),
        ]
        ingest_spot(
            _Adapter(bars=bars),  # type: ignore[arg-type]
            _NetNewSink(dedup_hidden=1),  # bars_appended=2, bars_net_new=1
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 1, 31),
            ledger=led,
        )
        rec = led.records()[0]
        assert rec.bars == 2
        assert rec.bars_net_new == 1  # the DEDUPED count, not the input count

    def test_write_deferred_record_carries_known_zero_net_new(self) -> None:
        # A deferred write (lock timeout) stored nothing — bars_net_new is a
        # KNOWN 0 (not an unknown None): the write demonstrably did not land.
        from brutex.data.window_ledger import WindowLedger

        led = WindowLedger()
        ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            _LockTimeoutSink(),  # type: ignore[arg-type]
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 1, 31),
            ledger=led,
        )
        rec = led.records()[0]
        assert rec.last_error == "PartitionLockTimeout"
        assert rec.bars_net_new == 0


class TestIngestSpotAllFilteredBoundaryWiden:
    """DEC-DATA-HONESTY-ALLFILTERED-SEED-001 on the SPOT spine: the all-filtered
    verdict now reads the kept count AFTER the session filter AND the future-bar
    drop (widening #1294's session-filter-only check), plus the dedicated
    always-on ``ingest.window.all_bars_filtered`` WARNING."""

    # 2021-02-01 (Monday, a regular NSE trading day) 09:35 IST — in-session for
    # its OWN date, but STRICTLY AFTER the [2021-01-01, 2021-01-31) window end,
    # so the session filter KEEPS it and drop_bars_after removes it.
    _FUTURE_TRADING_TS = datetime(2021, 2, 1, 4, 5, tzinfo=UTC)
    # 2021-01-26 (Republic Day, Tuesday) 10:05 IST — session-filtered (holiday).
    _HOLIDAY_TS = datetime(2021, 1, 26, 4, 35, tzinfo=UTC)

    def test_all_future_dropped_window_is_demoted(self) -> None:
        # FAILS-BEFORE / PASSES-AFTER (the boundary-widen headline): every vendor
        # bar survives the session filter but is dropped by drop_bars_after —
        # BEFORE this change the window recorded a silent-green SUCCESS with zero
        # stored bars (the session-only discriminator never saw the drop stage).
        from brutex.data.session import ALL_FILTERED
        from brutex.data.window_ledger import WindowLedger

        led = WindowLedger()
        sink = _Sink()
        with capture_logs() as logs:
            ingest_spot(
                _Adapter(bars=[_bar(self._FUTURE_TRADING_TS)]),  # type: ignore[arg-type]
                sink,
                instrument=_INST,
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 1, 31),
                ledger=led,
                detect_completeness_gaps=True,
            )
        assert sink.appends == [0]  # nothing stored — the future phantom never lands
        rec = led.records()[0]
        assert rec.status == "FAILED"
        assert rec.last_error == ALL_FILTERED
        assert rec.empty_streak == 1
        warns = _events(logs, "ingest.window.all_bars_filtered")
        assert len(warns) == 1
        assert warns[0]["raw_bars"] == 1
        assert warns[0]["kept_bars"] == 0
        # No session-filter warn: the bar was in-session for its own date — ONLY
        # the drop stage removed it (pins that the two stages are distinguishable).
        assert _events(logs, "ingest.session_filter.dropped") == []

    def test_exempt_symbol_all_future_dropped_is_demoted(self) -> None:
        # The session-EXEMPT branch (GIFT Nifty) skips the session filter, so the
        # old session-only discriminator could NEVER flag it; the whole-boundary
        # predicate catches its all-future-dropped window too.
        from brutex.data.session import ALL_FILTERED
        from brutex.data.window_ledger import WindowLedger

        led = WindowLedger()
        ingest_spot(
            _Adapter(bars=[_bar(self._FUTURE_TRADING_TS)]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_inst("GIFTNIFTY"),
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 1, 31),
            ledger=led,
            detect_completeness_gaps=True,
        )
        rec = led.records()[0]
        assert rec.status == "FAILED"
        assert rec.last_error == ALL_FILTERED

    def test_all_future_dropped_daily_warns_but_never_demotes(self) -> None:
        # The derived-1day gate: a DAILY window emptied at the boundary still
        # records SUCCESS even when armed (its completeness follows the 1-minute),
        # but the visibility WARN fires — never gated.
        from brutex.data.window_ledger import SUCCESS, WindowLedger

        led = WindowLedger()
        with capture_logs() as logs:
            ingest_spot(
                _Adapter(bars=[_bar(self._FUTURE_TRADING_TS)]),  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 1, 31),
                ledger=led,
                detect_completeness_gaps=True,
            )
        rec = led.records()[0]
        assert rec.status == SUCCESS
        assert rec.last_error is None
        assert len(_events(logs, "ingest.window.all_bars_filtered")) == 1

    def test_all_session_filtered_window_emits_dedicated_warn(self) -> None:
        # The session-filtered all-filtered shape now emits BOTH the per-cause
        # session_filter WARN and the ONE grep-able all-filtered event, carrying
        # the window bounds + series coordinates.
        with capture_logs() as logs:
            ingest_spot(
                _Adapter(bars=[_bar(self._HOLIDAY_TS)]),  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 1, 31),
            )
        warns = _events(logs, "ingest.window.all_bars_filtered")
        assert len(warns) == 1
        assert warns[0]["window_start"] == "2021-01-01"
        assert warns[0]["window_end"] == "2021-01-31"
        assert warns[0]["symbol"] == "NIFTY"
        assert len(_events(logs, "ingest.session_filter.dropped")) == 1

    def test_kept_bars_emit_no_all_filtered_warn(self) -> None:
        # A window with ANY boundary survivor stays silent on the dedicated event
        # (even when other bars were dropped by the session filter).
        with capture_logs() as logs:
            ingest_spot(
                _Adapter(bars=[_bar(), _bar(self._HOLIDAY_TS)]),  # type: ignore[arg-type]
                _Sink(),
                instrument=_INST,
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 1, 31),
            )
        assert _events(logs, "ingest.window.all_bars_filtered") == []
        assert len(_events(logs, "ingest.session_filter.dropped")) == 1


def _march_2024_trading_days() -> list[date]:
    """Every NSE trading day in [2024-03-01, 2024-03-30], ascending (real calendar)."""
    from brutex.calendars.nse import is_trading_day

    return [
        date(2024, 3, 1) + timedelta(days=i)
        for i in range(30)
        if is_trading_day(date(2024, 3, 1) + timedelta(days=i))
    ]


def _in_session_bar(day: date) -> CandleBar:
    """An in-session (09:35 IST) bar on *day* — 04:05 UTC on the same calendar date."""
    return _bar(datetime(day.year, day.month, day.day, 4, 5, tzinfo=UTC))


class TestSeedRefusalFetchComposition:
    """DEC-DATA-HONESTY-ALLFILTERED-SEED-001 — the two gates COMPOSE into one loop:
    the cold-start continuity gate refuses the seed (window stays absent), the pull
    then FETCHES it, and the fetch-time #1031 detector demotes the confirmed hole."""

    _SPAN_KEY = ("NSE", "INDEX", "NIFTY", "1minute")

    def _seed(self, ledger: object, session_dates: frozenset | None) -> int:
        from brutex.data.fetch_coverage import WindowKey
        from brutex.data.window_ledger import WindowLedger
        from brutex.data.window_ledger_store import index_lake_spans, seed_enclosed_windows

        assert isinstance(ledger, WindowLedger)
        span: dict[str, object] = {
            "exchange": "NSE",
            "segment": "INDEX",
            "symbol": "NIFTY",
            "interval": "1minute",
            "bars": 1000,
            "first_date": "2024-03-01",
            "last_date": "2024-03-30",
        }
        if session_dates is not None:
            span["session_dates"] = session_dates
        # The EXACT window key ingest_spot will fetch/record: market "groww"
        # (GROWW_COVERAGE_MARKET), one 30-day 1minute tile [2024-03-01, 2024-03-31).
        key = WindowKey(
            market="groww",
            security_id="NIFTY",
            interval="1minute",
            start=date(2024, 3, 1),
            end=date(2024, 3, 31),
        )
        return seed_enclosed_windows(
            ledger,
            index_lake_spans([span]),
            [(key, self._SPAN_KEY)],
            now=datetime(2026, 6, 8, 12, 0, tzinfo=UTC),
            today_ist=date(2026, 6, 8),
        )

    def test_refused_seed_then_fetch_demotes_interior_gap(self) -> None:
        # CLOSED LOOP (hole CONTAINED in the one window): refuse → fetch → the
        # fetch-time detector demotes FAILED + INTERIOR_GAP (bounded → SKIPPED
        # after EMPTY_TERMINAL_THRESHOLD armed pulls — pinned in #1031's suite).
        from brutex.data.interior_gap_detect import INTERIOR_GAP
        from brutex.data.window_ledger import WindowLedger

        t = _march_2024_trading_days()
        holed = frozenset(t[:2]) | frozenset(t[-2:])  # giant interior hole
        ledger = WindowLedger()
        assert self._seed(ledger, holed) == 0  # REFUSED — no row minted
        assert ledger.count == 0
        adapter = _Adapter(bars=[_in_session_bar(t[0]), _in_session_bar(t[-1])])
        ingest_spot(
            adapter,  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2024, 3, 1),
            end=date(2024, 3, 31),
            ledger=ledger,
            detect_completeness_gaps=True,
        )
        assert adapter.calls == 1  # the refused window was FETCHED, not skipped
        rec = ledger.records()[0]
        assert rec.status == "FAILED"  # re-pullable — the hole is never masked
        assert rec.last_error == INTERIOR_GAP

    def test_counterfactual_span_only_seed_masks_the_hole(self) -> None:
        # FAILS-BEFORE teeth: WITHOUT the density signal (a span-only seed, e.g.
        # the legacy shape) the same holed window seeds SUCCESS and the next pull
        # SKIPS it — zero vendor calls, the hole permanent. This is exactly the
        # blind spot the gate exists to close.
        from brutex.data.window_ledger import SUCCESS, WindowLedger

        ledger = WindowLedger()
        assert self._seed(ledger, None) == 1  # span-only → seeded
        assert ledger.records()[0].status == SUCCESS
        adapter = _Adapter()
        ingest_spot(
            adapter,  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2024, 3, 1),
            end=date(2024, 3, 31),
            ledger=ledger,
            detect_completeness_gaps=True,
        )
        assert adapter.calls == 0  # skipped — never fetched, never re-examined

    def test_straddling_hole_envelope_refuse_then_fetch_records_what_vendor_returns(
        self,
    ) -> None:
        # HONEST-envelope pin (refuter F-5): for a hole STRADDLING two windows the
        # refusal guarantees a REAL vendor fetch re-verifies each window — but the
        # per-window fetch verdict is NOT necessarily a demotion (each half reads
        # as an edge, not an interior hole). Here the leading window's bars stop
        # at the hole → the trailing-truncation detector demotes SUSPECTED_PARTIAL
        # or the window records SUCCESS; either way the state was re-verified by a
        # fetch, never a blind span stamp. Pin the ACTUAL composed outcome.
        from brutex.data.window_ledger import WindowLedger

        t = _march_2024_trading_days()
        ledger = WindowLedger()
        # Window A = [03-01, mid); its bars stop early (the hole's leading edge).
        adapter = _Adapter(bars=[_in_session_bar(t[0]), _in_session_bar(t[1])])
        ingest_spot(
            adapter,  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2024, 3, 1),
            end=date(2024, 3, 16),
            ledger=ledger,
            detect_completeness_gaps=True,
        )
        assert adapter.calls == 1
        rec = ledger.records()[0]
        # The trailing-truncation detector (DEC-PARTIAL-WINDOW-REPULL-001) sees
        # bars stopping ~8 sessions before the window end → demotes re-pullable.
        assert rec.status == "FAILED"
        assert rec.last_error == "SUSPECTED_PARTIAL"


class _SelfReportingClientStub:
    """A client stub advertising the SELF-REPORTS surfaces (the live-client shape)."""

    self_paces = False  # keep the window pace path unchanged for these pins
    self_reports_rate_limits = True
    self_reports_successes = True


class _SelfHookedAdapter(_Adapter):
    """Fake adapter whose ``.client`` self-reports — the rebound live shape."""

    def __init__(
        self, *, bars: Sequence[CandleBar] | None = None, error: Exception | None = None
    ) -> None:
        super().__init__(bars=bars, error=error)
        self.client = _SelfReportingClientStub()


class TestIngestSpotFeedbackStandDown:
    """DEC-SUSPICION-SWEEP-FIXES-001 (D1 companion): when the adapter's client
    SELF-REPORTS (``run_groww_spot_ingest`` binds the controller's
    on_rate_limit/on_success into the live client, which fires them per 429
    OCCURRENCE / per clean call), the spine's per-window hooks stand DOWN — the
    propagated final occurrence is never double-counted and the per-window
    recovery is never double-credited. ``on_timeout`` stays spine-owned (the
    client never fires the timeout ladder)."""

    def test_spine_on_rate_limit_stands_down_for_self_reporting_client(self) -> None:
        hits: list[int] = []
        report = ingest_spot(
            _SelfHookedAdapter(error=GrowwRateLimitError("rl")),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            on_rate_limit=lambda: hits.append(1),
            max_retry_passes=0,
        )
        assert report.transient_failures == 1  # the window still counted/deferred
        assert hits == []  # the client already reported every occurrence

    def test_spine_on_success_stands_down_for_self_reporting_client(self) -> None:
        ok: list[int] = []
        ingest_spot(
            _SelfHookedAdapter(),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            on_success=lambda: ok.append(1),
        )
        assert ok == []  # the client already recovered per clean call

    def test_spine_on_timeout_never_stands_down(self) -> None:
        timeouts: list[int] = []
        ingest_spot(
            _SelfHookedAdapter(error=GrowwTimeoutError("to")),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            on_timeout=lambda: timeouts.append(1),
            max_retry_passes=0,
        )
        assert timeouts == [1]  # the spine remains the timeout ladder's only feeder


class TestMassAllFilteredBreaker:
    """DEC-ALLFILTERED-MASS-BREAKER-001 — the spot spine's mass-event guard:
    trip → suppress further ALL_FILTERED settles + rescind same-pull pre-trip
    settles + exactly ONE count-naming alarm."""

    # 2021-01-26 (Republic Day) 10:05 IST — in-window wall clock, NO session.
    _HOLIDAY_TS = datetime(2021, 1, 26, 4, 35, tzinfo=UTC)

    @staticmethod
    def _tiny_breaker() -> AllFilteredBreaker:
        # FRACTION rule armed at 3 observations across 3 series (the engine-pull
        # shape); the SPREAD rule disabled so the trip point is deterministic.
        return AllFilteredBreaker(
            spread_series_limit=0,
            fraction_limit=0.5,
            min_observations=3,
            fraction_min_series=3,
        )

    def _pull_symbol(
        self,
        symbol: str,
        led: LedgerWindowLedger,
        breaker: AllFilteredBreaker | None,
        *,
        detect: bool = True,
        bars: list[CandleBar] | None = None,
    ) -> None:
        ingest_spot(
            _Adapter(bars=bars if bars is not None else [_bar(self._HOLIDAY_TS)]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_inst(symbol),
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 1, 31),
            ledger=led,
            detect_completeness_gaps=detect,
            all_filtered_breaker=breaker,
        )

    def test_mass_all_filtered_suppresses_settles_and_alarms_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # FAILS-BEFORE / PASSES-AFTER: a >=3-series all-filtered event through the
        # REAL tail. Symbol A is pre-seeded at streak 2, so its record IN this pull
        # settles SKIPPED BEFORE the counters cross (the pre-trip settle); the trip
        # on the 3rd series then RESCINDS it, suppresses the trip-crossing window,
        # and alarms exactly once (structlog ERROR + one Telegram push).
        from brutex.data.session import ALL_FILTERED
        from brutex.data.window_ledger import (
            ALL_FILTERED_MASS_RESCINDED,
            WindowLedger,
        )
        from brutex.notifications.sweep_alerts import SweepAlerter

        pushes: list[dict] = []

        def _spy(self: SweepAlerter, **kw: object) -> bool:
            pushes.append(dict(kw))
            return True

        monkeypatch.setattr(SweepAlerter, "mass_all_filtered", _spy)
        led = WindowLedger()
        breaker = self._tiny_breaker()
        # Pre-seed A at streak 2 (two prior pulls' ALL_FILTERED demotions).
        for _ in range(2):
            self._pull_symbol("AAA", led, None)
        assert led.records()[0].empty_streak == 2
        with capture_logs() as logs, pull_id_scope("massaf1"):
            self._pull_symbol("AAA", led, breaker)  # settles SKIPPED pre-trip
            assert led.newly_settled_skipped == 1
            self._pull_symbol("BBB", led, breaker)  # obs 2 — below min_obs
            self._pull_symbol("CCC", led, breaker)  # obs 3 — TRIPS + rescinds
        by_symbol = {r.security_id: r for r in led.records()}
        # The pre-trip settle was RESCINDED — FAILED + the mass-rescind marker,
        # streak/provenance reset, tombstoned, verdict counter decremented.
        assert by_symbol["AAA"].status == "FAILED"
        assert by_symbol["AAA"].last_error == ALL_FILTERED_MASS_RESCINDED
        assert by_symbol["AAA"].empty_streak == 0
        assert by_symbol["AAA"].streak_provenance == ""
        assert led.newly_settled_skipped == 0
        aaa_key = ("groww", "AAA", "1minute", "2021-01-01", "2021-01-31")
        assert aaa_key in led.reopened_keys()
        assert aaa_key in led.degraded_keys()
        # BBB recorded BEFORE the trip — the normal demotion (streak bumped).
        assert by_symbol["BBB"].status == "FAILED"
        assert by_symbol["BBB"].last_error == ALL_FILTERED
        assert by_symbol["BBB"].empty_streak == 1
        # CCC — the trip-crossing window — SUPPRESSED ITSELF: FAILED + reason,
        # streak NOT bumped, provenance untouched.
        assert by_symbol["CCC"].status == "FAILED"
        assert by_symbol["CCC"].last_error == ALL_FILTERED
        assert by_symbol["CCC"].empty_streak == 0
        assert by_symbol["CCC"].streak_provenance == ""
        # EXACTLY ONE loud alarm: one structlog ERROR + one Telegram push, both
        # naming the counts + the rescind.
        errors = _events(logs, "allfiltered_breaker.tripped")
        assert len(errors) == 1
        assert errors[0]["trip_rule"] == "fraction"
        assert errors[0]["all_filtered_windows"] == 3
        assert errors[0]["observed_windows"] == 3
        assert errors[0]["distinct_series"] == 3
        assert errors[0]["rescinded_settles"] == 1
        assert errors[0]["pull_id"] == "massaf1"
        assert len(pushes) == 1
        assert pushes[0]["rescinded_settles"] == 1
        assert pushes[0]["pull_id"] == "massaf1"
        assert pushes[0]["window_samples"] == (
            "AAA:1minute 2021-01-01→2021-01-31",
            "BBB:1minute 2021-01-01→2021-01-31",
            "CCC:1minute 2021-01-01→2021-01-31",
        )

    def test_below_threshold_settles_byte_identical(self) -> None:
        # The task's explicit below-threshold requirement: 1-2 series fully
        # all-filtered over 3 pulls settle SKIPPED exactly as today even with a
        # LIVE (default-config) breaker present — both floors are above 2 series.
        from brutex.adapters.all_filtered_breaker import resolve_all_filtered_breaker
        from brutex.data.window_ledger import EMPTY_TERMINAL_THRESHOLD, SKIPPED, WindowLedger

        led = WindowLedger()
        breaker = resolve_all_filtered_breaker()
        for _ in range(EMPTY_TERMINAL_THRESHOLD):
            self._pull_symbol("AAA", led, breaker)
            self._pull_symbol("BBB", led, breaker)
        by_symbol = {r.security_id: r for r in led.records()}
        assert by_symbol["AAA"].status == SKIPPED
        assert by_symbol["BBB"].status == SKIPPED
        assert not breaker.tripped

    def test_runner_resolves_and_threads_the_breaker_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The runner ALWAYS builds a breaker (env-configured) and threads it into
        # every pair — proven by an env-lowered trip through run_groww_spot_ingest.
        from brutex.adapters.all_filtered_breaker import (
            ALLFILTERED_BREAKER_FRACTION_MIN_SERIES_ENV,
            ALLFILTERED_BREAKER_MIN_OBSERVATIONS_ENV,
        )
        from brutex.adapters.groww.run_ingest import run_groww_spot_ingest
        from brutex.data.session import ALL_FILTERED
        from brutex.data.window_ledger import WindowLedger
        from brutex.orchestrator.parallel import SerialRunParallel

        monkeypatch.setenv(ALLFILTERED_BREAKER_MIN_OBSERVATIONS_ENV, "3")
        monkeypatch.setenv(ALLFILTERED_BREAKER_FRACTION_MIN_SERIES_ENV, "3")
        led = WindowLedger()
        with capture_logs() as logs:
            run_groww_spot_ingest(
                _Adapter(bars=[_bar(self._HOLIDAY_TS)]),  # type: ignore[arg-type]
                _Sink(),
                instruments=[_inst("AAA"), _inst("BBB"), _inst("CCC")],
                intervals=["1minute"],
                start=date(2021, 1, 1),
                end=date(2021, 1, 31),
                ledger=led,
                run_parallel=SerialRunParallel(),
                detect_completeness_gaps=True,
            )
        assert len(_events(logs, "allfiltered_breaker.tripped")) == 1
        # Every window stays FAILED + re-pullable (the trip suppressed the third).
        assert {r.status for r in led.records()} == {"FAILED"}
        assert {r.last_error for r in led.records()} == {ALL_FILTERED}

    def test_ineligible_windows_never_feed_the_breaker(self) -> None:
        # dry-run (detect off) / daily interval / exempt symbol / genuine EMPTY —
        # none joins the numerator OR the denominator.
        from brutex.data.window_ledger import WindowLedger

        breaker = self._tiny_breaker()
        led = WindowLedger()
        # (a) detect_completeness_gaps=False — the dry-run/synthetic contract.
        self._pull_symbol("AAA", led, breaker, detect=False)
        # (b) genuine EMPTY (raw_bars == 0).
        self._pull_symbol("BBB", led, breaker, bars=[])
        # (c) session-exempt symbol (never session-filtered).
        self._pull_symbol("GIFTNIFTY", led, breaker)
        assert breaker.counts() == (0, 0, 0)
        assert not breaker.tripped

    def test_daily_interval_never_feeds_the_breaker(self) -> None:
        from brutex.data.window_ledger import WindowLedger

        breaker = self._tiny_breaker()
        ingest_spot(
            _Adapter(bars=[_bar(self._HOLIDAY_TS)]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 1, 31),
            ledger=WindowLedger(),
            detect_completeness_gaps=True,
            all_filtered_breaker=breaker,
        )
        assert breaker.counts() == (0, 0, 0)

    def test_kept_bars_count_the_denominator_only(self) -> None:
        from brutex.data.window_ledger import WindowLedger

        breaker = self._tiny_breaker()
        self._pull_symbol("AAA", WindowLedger(), breaker, bars=[_bar()])
        assert breaker.counts() == (1, 0, 0)

    def test_trip_with_no_ledger_rescinds_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The rescind ternary's ledger-None arm: the alarm still fires, rescinded=0.
        from brutex.notifications.sweep_alerts import SweepAlerter

        pushes: list[dict] = []
        monkeypatch.setattr(
            SweepAlerter,
            "mass_all_filtered",
            lambda self, **kw: pushes.append(dict(kw)) or True,
        )
        breaker = self._tiny_breaker()
        with capture_logs() as logs:
            for symbol in ("AAA", "BBB", "CCC"):
                ingest_spot(
                    _Adapter(bars=[_bar(self._HOLIDAY_TS)]),  # type: ignore[arg-type]
                    _Sink(),
                    instrument=_inst(symbol),
                    interval="1minute",
                    start=date(2021, 1, 1),
                    end=date(2021, 1, 31),
                    ledger=None,
                    detect_completeness_gaps=True,
                    all_filtered_breaker=breaker,
                )
        errors = _events(logs, "allfiltered_breaker.tripped")
        assert len(errors) == 1
        assert errors[0]["rescinded_settles"] == 0
        assert pushes and pushes[0]["rescinded_settles"] == 0

    def test_genuine_empty_channel_unaffected_while_tripped(self) -> None:
        # The EMPTY channel is untouched by the mass-AF suppression (renamed in
        # review round 1, finding F6 — this test drives a genuine clean EMPTY,
        # NOT an INTERIOR_GAP demotion; the real orthogonal-channel spine pin is
        # test_interior_gap_demotion_unaffected_while_tripped below).
        from brutex.data.window_ledger import WindowLedger

        breaker = self._tiny_breaker()
        led = WindowLedger()
        for symbol in ("AAA", "BBB", "CCC"):
            self._pull_symbol(symbol, led, breaker)
        assert breaker.tripped
        # A genuine clean EMPTY in the tripped pull still bumps + settles (here
        # via the old-window single-confirm — the mass-AF suppression reaches
        # ONLY the ALL_FILTERED degraded channel, never the EMPTY channels).
        self._pull_symbol("DDD", led, breaker, bars=[])
        rec = {r.security_id: r for r in led.records()}["DDD"]
        assert rec.status == "SKIPPED"
        assert rec.streak_provenance is not None
        assert rec.streak_provenance.startswith("EMPTY@")

    def test_interior_gap_demotion_unaffected_while_tripped(self) -> None:
        # The REAL orthogonal-channel pin (review round 1, finding F6): an
        # INTERIOR_GAP demotion driven through the spine WITH a tripped breaker
        # still BUMPS the streak — the `all_filtered` conjunct in the tail's
        # `allow_degraded_settle` construction keeps the spot-only completeness
        # channels byte-unchanged while tripped (kills the drop-the-conjunct
        # mutant the EMPTY-shaped test above cannot: suppress is IGNORED for
        # EMPTY, so only a degraded non-ALL_FILTERED record has teeth here).
        from brutex.data.interior_gap_detect import INTERIOR_GAP
        from brutex.data.window_ledger import WindowLedger

        breaker = self._tiny_breaker()
        led = WindowLedger()
        for symbol in ("AAA", "BBB", "CCC"):
            self._pull_symbol(symbol, led, breaker)
        assert breaker.tripped
        # A holed-but-in-session March-2024 window (the #1031 interior-gap
        # fixture shape) for a fourth symbol, pulled while tripped.
        t = _march_2024_trading_days()
        ingest_spot(
            _Adapter(bars=[_in_session_bar(t[0]), _in_session_bar(t[-1])]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_inst("DDD"),
            interval="1minute",
            start=date(2024, 3, 1),
            end=date(2024, 3, 31),
            ledger=led,
            detect_completeness_gaps=True,
            all_filtered_breaker=breaker,
        )
        rec = {r.security_id: r for r in led.records()}["DDD"]
        assert rec.status == "FAILED"
        assert rec.last_error == INTERIOR_GAP
        assert rec.empty_streak == 1  # BUMPED — never suppressed by the tripped breaker
        assert rec.streak_provenance is not None
        assert rec.streak_provenance.startswith("INTERIOR_GAP@")

    def test_tail_consults_the_breaker_inside_the_ledger_record(self) -> None:
        # Review round 1 (the F2/F4/F5/F7/F8 TOCTOU): the suppression decision is
        # evaluated INSIDE record() UNDER the ledger lock — never pre-computed in
        # the tail — so it serializes with the trip's one-shot rescind and a
        # settle in flight at the trip instant can never leak un-rescinded.
        # Ordering spy: allow_settle fires AFTER record() was entered (pre-fix
        # the order was allow_settle THEN record — the leakable window).
        from typing import Any

        from brutex.data.window_ledger import WindowLedger

        calls: list[str] = []

        class _SpyBreaker(AllFilteredBreaker):
            def allow_settle(self) -> bool:
                calls.append("allow_settle")
                return super().allow_settle()

        class _SpyLedger(WindowLedger):
            def record(self, *args: Any, **kwargs: Any) -> str:
                calls.append("record_enter")
                return super().record(*args, **kwargs)

        breaker = _SpyBreaker(
            spread_series_limit=0,
            fraction_limit=0.5,
            min_observations=99,  # never trips — the allow path is the point
            fraction_min_series=3,
        )
        led = _SpyLedger()
        self._pull_symbol("AAA", led, breaker)
        assert calls == ["record_enter", "allow_settle"]

    def test_with_retry_latches_one_breaker_across_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Review round 4 (the v3-round-2 MAJOR): run_groww_spot_ingest_with_retry
        # resolves the breaker ONCE (when None) and threads the SAME instance
        # into EVERY pass, so a pass-1 trip stays latched through the retry
        # rounds. Fail-before: it forwarded the None verbatim and each pass
        # resolved its OWN fresh instance.
        from brutex.adapters.groww import run_ingest as ri

        breakers: list[object] = []
        still_failed = ri.GrowwIngestReport(
            pairs={
                ("AAA", "1minute"): IngestReport(
                    windows=1, bars_appended=0, transient_failures=1, still_failed_windows=1
                )
            },
            failures=(),
        )

        def _spy(
            *_args: object, all_filtered_breaker: object = None, **_kw: object
        ) -> ri.GrowwIngestReport:
            breakers.append(all_filtered_breaker)
            return still_failed

        monkeypatch.setattr(ri, "run_groww_spot_ingest", _spy)
        ri.run_groww_spot_ingest_with_retry(
            _Adapter(),  # type: ignore[arg-type]
            _Sink(),
            instruments=[_inst("AAA")],
            intervals=["1minute"],
            start=date(2021, 1, 1),
            end=date(2021, 1, 31),
            max_retry_rounds=2,
            retry_backoff=lambda _i: None,
        )
        # The original pass + 2 retry rounds (still-failed never clears).
        assert len(breakers) == 3
        assert all(isinstance(b, AllFilteredBreaker) for b in breakers)
        assert all(b is breakers[0] for b in breakers)  # ONE latched instance

    def test_with_retry_forwards_an_injected_breaker_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The other resolve arm: an injected instance (a pull entrypoint's
        # pull-scoped breaker, or a test's) reaches every pass unwrapped.
        from brutex.adapters.groww import run_ingest as ri

        sentinel = self._tiny_breaker()
        breakers: list[object] = []

        def _spy(
            *_args: object, all_filtered_breaker: object = None, **_kw: object
        ) -> ri.GrowwIngestReport:
            breakers.append(all_filtered_breaker)
            return ri.GrowwIngestReport(pairs={}, failures=())

        monkeypatch.setattr(ri, "run_groww_spot_ingest", _spy)
        ri.run_groww_spot_ingest_with_retry(
            _Adapter(),  # type: ignore[arg-type]
            _Sink(),
            instruments=[_inst("AAA")],
            intervals=["1minute"],
            start=date(2021, 1, 1),
            end=date(2021, 1, 31),
            all_filtered_breaker=sentinel,
        )
        assert breakers == [sentinel]  # clean pass — no retry; the sentinel, not a wrap

    def test_trip_latch_holds_across_retry_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # FAILS-BEFORE / PASSES-AFTER (review round 4 — the demonstrated leak):
        # pass 1 observes 6 eligible windows (3 all-filtered across 3 series →
        # the FRACTION rule trips at AAA's own observation with the floor
        # env-set to 6) and the CRITICAL alarm promises settles are halted for
        # the pull; a transient-timeout symbol (GGG) leaves still_failed > 0,
        # arming the auto-retry round. That round re-fetches every
        # non-skippable window — the 3 suppressed AF FAILED ones included —
        # but observes only 3 eligible windows, BELOW the 6-observation floor:
        # pre-fix its FRESH per-pass breaker never tripped and AAA (prior
        # streak 2, preserved by the pass-1 suppression) bumped to threshold
        # and settled SKIPPED minutes AFTER the alarm, un-rescinded and
        # un-alarmed. The latched pull-scoped breaker keeps every AF window
        # FAILED + re-pullable.
        from brutex.adapters.all_filtered_breaker import (
            ALLFILTERED_BREAKER_MIN_OBSERVATIONS_ENV,
        )
        from brutex.adapters.groww import ingest as ingest_mod
        from brutex.adapters.groww.run_ingest import run_groww_spot_ingest_with_retry
        from brutex.data.session import ALL_FILTERED
        from brutex.data.window_ledger import WindowLedger
        from brutex.notifications.sweep_alerts import SweepAlerter
        from brutex.orchestrator.parallel import SerialRunParallel

        pushes: list[dict] = []
        monkeypatch.setattr(
            SweepAlerter,
            "mass_all_filtered",
            lambda self, **kw: pushes.append(dict(kw)) or True,
        )
        # No real sleeps: neutralize the in-pull transient-retry backoff GGG burns.
        monkeypatch.setattr(ingest_mod, "_time_sleep", lambda _s: None)
        monkeypatch.setenv(ALLFILTERED_BREAKER_MIN_OBSERVATIONS_ENV, "6")
        led = WindowLedger()
        # Pre-seed AAA at streak 2 (EMPTY_TERMINAL_THRESHOLD - 1) — two prior
        # pulls' ALL_FILTERED demotions: ONE more allowed bump settles it.
        for _ in range(2):
            self._pull_symbol("AAA", led, None)
        assert led.records()[0].empty_streak == 2
        af_symbols = frozenset({"AAA", "BBB", "CCC"})
        holiday_ts = self._HOLIDAY_TS

        class _MixedAdapter:
            """AF symbols → a holiday bar (all-filtered); GGG → a persistent
            transient timeout (arms the auto-retry round without ever feeding
            the breaker); others → a complete window (SUCCESS, never re-pulled)."""

            def fetch_ohlcv(
                self, *, symbol: str, exchange: object, interval: str, start: date, end: date
            ) -> list[CandleBar]:
                del exchange, interval, start
                if symbol == "GGG":
                    raise GrowwTimeoutError("transient — arms the retry round")
                if symbol in af_symbols:
                    return [_bar(holiday_ts)]
                last_session = last_completed_session_on_or_before(end - timedelta(days=1))
                return [_in_session_bar(last_session)]

        with capture_logs() as logs:
            run_groww_spot_ingest_with_retry(
                _MixedAdapter(),  # type: ignore[arg-type]
                _Sink(),
                # AAA is the 6th ELIGIBLE observation — the trip-crossing one
                # (GGG's raised fetch never reaches the tail, so it observes
                # nothing in either pass).
                instruments=[_inst(s) for s in ("BBB", "CCC", "DDD", "EEE", "FFF", "AAA", "GGG")],
                intervals=["1minute"],
                start=date(2021, 1, 1),
                end=date(2021, 1, 31),
                ledger=led,
                run_parallel=SerialRunParallel(),
                detect_completeness_gaps=True,
                max_retry_rounds=1,
                retry_backoff=lambda _i: None,
            )
        by_symbol = {r.security_id: r for r in led.records()}
        # THE LEAK PIN: AAA stays FAILED with its streak PRESERVED — pre-fix the
        # retry pass settled it SKIPPED (streak 2 → 3) post-alarm.
        assert by_symbol["AAA"].status == "FAILED"
        assert by_symbol["AAA"].last_error == ALL_FILTERED
        assert by_symbol["AAA"].empty_streak == 2
        assert led.newly_settled_skipped == 0
        # BBB/CCC recorded pre-trip in pass 1 (streak 1); the latched breaker
        # suppresses their retry-pass records too (streaks preserved).
        assert by_symbol["BBB"].empty_streak == 1
        assert by_symbol["CCC"].empty_streak == 1
        # The clean symbols settled SUCCESS in pass 1 and were never re-pulled;
        # the transient symbol is still honestly FAILED (a NEXT pull's problem).
        assert by_symbol["DDD"].status == "SUCCESS"
        assert by_symbol["GGG"].status == "FAILED"
        # EXACTLY ONE alarm for the whole pull — the pass-1 trip transition.
        assert len(_events(logs, "allfiltered_breaker.tripped")) == 1
        assert len(pushes) == 1


class _ScriptAdapter:
    """Fake adapter scripting bars OR exceptions per call (clamped to the last entry).

    The DEC-RETRY-COUNTER-SPLIT-001 driver: e.g. ``[[], GrowwRateLimitError(...)]`` is
    a clean empty followed by a transient — the mixed ladder whose split the ledger
    must record honestly.
    """

    def __init__(self, script: Sequence[Sequence[CandleBar] | Exception]) -> None:
        self._script = list(script)
        self.calls = 0

    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del symbol, exchange, interval, start, end
        item = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return list(item)


class TestRetryCounterSplitSpot:
    """DEC-RETRY-COUNTER-SPLIT-001 (spot spine): the ledger rows carry the
    empty/transient SPLIT of ``in_pull_fetches``, and the spine invariant
    ``empty + transient + (1 if bars) == in_pull_fetches`` holds at every record."""

    def test_first_try_success_records_known_zero_split(self) -> None:
        from brutex.data.window_ledger import SUCCESS, WindowLedger

        ledger = WindowLedger()
        ingest_spot(
            _Adapter(),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            ledger=ledger,
        )
        rec = ledger.records()[0]
        assert rec.status == SUCCESS
        assert rec.in_pull_fetches == 1
        assert rec.empty_retries == 0  # a KNOWN zero — the ladder ran, nothing retried
        assert rec.transient_retries == 0

    def test_all_empty_run_records_the_empty_half(self) -> None:
        from brutex.data.window_ledger import EMPTY, WindowLedger

        ledger = WindowLedger()
        ingest_spot(
            _BarSeqAdapter([[]]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            ledger=ledger,
            empty_retry_attempts=3,
            now_ist=lambda: datetime(2021, 3, 10, 12, 0, tzinfo=UTC),  # RECENT — no settle
        )
        rec = ledger.records()[0]
        assert rec.status == EMPTY
        assert rec.in_pull_fetches == 3
        assert rec.empty_retries == 3  # every fetch was a clean empty
        assert rec.transient_retries == 0
        # The decomposition invariant (no data-bearing fetch on an EMPTY row).
        assert rec.empty_retries + rec.transient_retries == rec.in_pull_fetches

    def test_empty_then_data_splits_the_recovery(self) -> None:
        from brutex.data.window_ledger import SUCCESS, WindowLedger

        ledger = WindowLedger()
        ingest_spot(
            _BarSeqAdapter([[], [], [_bar()]]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            ledger=ledger,
            empty_retry_attempts=3,
        )
        rec = ledger.records()[0]
        assert rec.status == SUCCESS
        assert rec.in_pull_fetches == 3
        assert rec.empty_retries == 2  # two clean empties before the data landed
        assert rec.transient_retries == 0
        assert rec.empty_retries + rec.transient_retries + 1 == rec.in_pull_fetches

    def test_first_fetch_transient_records_the_transient_half(self) -> None:
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
        assert rec.in_pull_fetches == 1
        assert rec.empty_retries == 0
        assert rec.transient_retries == 1  # the one fetch WAS the transient
        assert rec.empty_retries + rec.transient_retries == rec.in_pull_fetches

    def test_empty_then_transient_carries_both_halves(self) -> None:
        # The mixed ladder #924/#1355 could not express: one clean empty, then a
        # 429 — the FAILED row now says exactly which class each fetch was.
        from brutex.data.window_ledger import FAILED, WindowLedger

        ledger = WindowLedger()
        ingest_spot(
            _ScriptAdapter([[], GrowwRateLimitError("429")]),  # type: ignore[arg-type]
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),
            ledger=ledger,
            empty_retry_attempts=3,
            max_retry_passes=0,
        )
        rec = ledger.records()[0]
        assert rec.status == FAILED
        assert rec.in_pull_fetches == 2  # the empty re-fetch AND the transient
        assert rec.empty_retries == 1
        assert rec.transient_retries == 1
        assert rec.empty_retries + rec.transient_retries == rec.in_pull_fetches
