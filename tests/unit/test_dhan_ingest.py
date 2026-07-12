"""Tests for :mod:`brutex.adapters.dhan.ingest` — the lean spot spine + honest degrade."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

from brutex.adapters.dhan.errors import DhanSubscriptionError, DhanThrottleError
from brutex.adapters.dhan.ingest import (
    STATUS_FAILED,
    STATUS_SKIPPED_UNRESOLVED,
    STATUS_SUCCESS,
    DhanIngestReport,
    DhanWorkItem,
    dhan_index_exchange,
    plan_dhan_work,
    run_dhan_spot_ingest_with_retry,
    scan_resolved_all_empty,
)
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult
from brutex.models import Exchange
from structlog.testing import capture_logs

_EPOCH = datetime(2022, 6, 1, 3, 45, tzinfo=UTC)


def _bar() -> CandleBar:
    return CandleBar(
        timestamp=_EPOCH,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
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
            {"exchange": exchange, "segment": segment, "symbol": symbol, "n": len(bars)}
        )
        return SinkAppendResult(
            partitions_written=1,
            bars_appended=len(bars),
            write_results=(),
            bars_net_new=len(bars),
        )


class _FakeAdapter:
    """A fake MarketAdapter driven by a per-(symbol) scripted response queue."""

    def __init__(self, script: dict[str, list[object]]) -> None:
        self._script = script
        self.calls: list[str] = []

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        exchange: Exchange,
        interval: str,
        start: date,
        end: date,
    ) -> list[CandleBar]:
        _ = (exchange, interval, start, end)
        self.calls.append(symbol)
        response = self._script[symbol].pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]


def _no_filter(bars: Sequence[CandleBar]) -> SimpleNamespace:
    return SimpleNamespace(kept=list(bars))


def test_dhan_index_exchange_mapping() -> None:
    assert dhan_index_exchange("SENSEX") is Exchange.BSE
    assert dhan_index_exchange("NIFTY") is Exchange.NSE


def test_plan_work_resolves_and_omits_unresolved() -> None:
    work, unresolved = plan_dhan_work(
        symbols=["NIFTY", "MYSTERY"],
        security_ids={"NIFTY": 13},
        interval="1minute",
        start=date(2022, 6, 1),
        end=date(2022, 6, 20),
    )
    assert unresolved == ["MYSTERY"]
    assert all(isinstance(item, DhanWorkItem) for item in work)
    assert {item.symbol for item in work} == {"NIFTY"}


def test_run_success_appends_and_reports() -> None:
    adapter = _FakeAdapter({"NIFTY": [[_bar()]]})
    sink = _RecordingSink()
    report = run_dhan_spot_ingest_with_retry(
        symbols=["NIFTY"],
        adapter=adapter,
        sink=sink,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 1),
        end=date(2022, 6, 20),
        session_filter=_no_filter,
    )
    assert report.total_bars == 1
    assert report.failed_windows == 0
    assert report.resolved_symbols == ("NIFTY",)
    assert sink.appends[0]["segment"] == "INDEX"
    assert [o.status for o in report.outcomes] == [STATUS_SUCCESS]


def test_run_unresolved_symbol_is_loud_skip_zero_bars() -> None:
    adapter = _FakeAdapter({})
    sink = _RecordingSink()
    report = run_dhan_spot_ingest_with_retry(
        symbols=["MYSTERY"],
        adapter=adapter,
        sink=sink,
        security_ids={},
        start=date(2022, 6, 1),
        end=date(2022, 6, 20),
        session_filter=_no_filter,
    )
    assert report.unresolved_symbols == ("MYSTERY",)
    assert report.total_bars == 0
    assert sink.appends == []  # never fetched — no fabricated bar
    assert [o.status for o in report.outcomes] == [STATUS_SKIPPED_UNRESOLVED]
    assert report.outcomes[0].exchange is Exchange.NSE


def test_run_retryable_failure_retried_then_succeeds() -> None:
    adapter = _FakeAdapter({"NIFTY": [DhanThrottleError("slow"), [_bar()]]})
    sink = _RecordingSink()
    backoffs: list[int] = []
    report = run_dhan_spot_ingest_with_retry(
        symbols=["NIFTY"],
        adapter=adapter,
        sink=sink,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 1),
        end=date(2022, 6, 20),
        session_filter=_no_filter,
        max_rounds=3,
        backoff=backoffs.append,
    )
    assert report.failed_windows == 0
    assert report.total_bars == 1
    assert adapter.calls == ["NIFTY", "NIFTY"]  # one retry
    assert backoffs == [0]  # one inter-round back-off


def test_run_terminal_failure_stays_failed_repullable() -> None:
    adapter = _FakeAdapter({"NIFTY": [DhanSubscriptionError("no sub")]})
    sink = _RecordingSink()
    report = run_dhan_spot_ingest_with_retry(
        symbols=["NIFTY"],
        adapter=adapter,
        sink=sink,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 1),
        end=date(2022, 6, 20),
        session_filter=_no_filter,
        max_rounds=3,
    )
    assert report.failed_windows == 1
    assert adapter.calls == ["NIFTY"]  # NOT retried (terminal)
    assert report.outcomes[0].status == STATUS_FAILED
    assert report.outcomes[0].error_type == "DhanSubscriptionError"


def test_run_retryable_exhausts_rounds_and_stays_failed() -> None:
    adapter = _FakeAdapter({"NIFTY": [DhanThrottleError("a"), DhanThrottleError("b")]})
    sink = _RecordingSink()
    report = run_dhan_spot_ingest_with_retry(
        symbols=["NIFTY"],
        adapter=adapter,
        sink=sink,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 1),
        end=date(2022, 6, 20),
        session_filter=_no_filter,
        max_rounds=2,
    )
    assert report.failed_windows == 1
    assert report.outcomes[0].retryable is True
    assert adapter.calls == ["NIFTY", "NIFTY"]  # two rounds


def test_run_no_windows_when_range_empty() -> None:
    adapter = _FakeAdapter({"NIFTY": []})
    sink = _RecordingSink()
    report = run_dhan_spot_ingest_with_retry(
        symbols=["NIFTY"],
        adapter=adapter,
        sink=sink,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 1),
        end=date(2022, 6, 1),  # start >= end → no windows
        session_filter=_no_filter,
    )
    assert report.outcomes == ()
    assert adapter.calls == []


def test_default_pace_and_session_filter_paths() -> None:
    # Exercise the DEFAULT pace (no-op) + the DEFAULT calendar session filter.
    adapter = _FakeAdapter({"NIFTY": [[_bar()]]})
    sink = _RecordingSink()
    report = run_dhan_spot_ingest_with_retry(
        symbols=["NIFTY"],
        adapter=adapter,
        sink=sink,
        security_ids={"NIFTY": 13},
        start=date(2022, 6, 1),
        end=date(2022, 6, 20),
        # no session_filter / pace / backoff → defaults
    )
    # The 2022-06-01 03:45 UTC (09:15 IST) bar is in-session → kept.
    assert report.total_bars == 1


def test_resolved_all_empty_symbol_flagged_and_warned() -> None:
    # A RESOLVED index whose only window returns ZERO bars (all clean-empty) is
    # suspicious (wrong secId / unverified servability) — flagged + one WARN.
    adapter = _FakeAdapter({"NIFTY": [[]]})  # one window, empty bars
    sink = _RecordingSink()
    with capture_logs() as logs:
        report = run_dhan_spot_ingest_with_retry(
            symbols=["NIFTY"],
            adapter=adapter,
            sink=sink,
            security_ids={"NIFTY": 13},
            start=date(2022, 6, 1),
            end=date(2022, 6, 20),
            session_filter=_no_filter,
        )
    assert report.total_bars == 0
    assert report.failed_windows == 0
    assert report.resolved_symbols == ("NIFTY",)
    assert scan_resolved_all_empty(report) == ("NIFTY",)
    warns = [e for e in logs if e.get("event") == "dhan.symbol_all_empty"]
    assert len(warns) == 1
    assert warns[0]["symbol"] == "NIFTY"
    assert warns[0]["log_level"] == "warning"


def test_scan_all_empty_excludes_failed_unresolved_and_nonempty() -> None:
    # NIFTY: clean-empty → flagged. BANKNIFTY: has a bar → NOT flagged.
    # SENSEX: a terminal failure (a FAILED window) → NOT flagged (failure is the
    # story, already surfaced per-window). MYSTERY: unresolved → NOT flagged.
    adapter = _FakeAdapter(
        {
            "NIFTY": [[]],
            "BANKNIFTY": [[_bar()]],
            "SENSEX": [DhanSubscriptionError("no sub")],
        }
    )
    sink = _RecordingSink()
    with capture_logs() as logs:
        report = run_dhan_spot_ingest_with_retry(
            symbols=["NIFTY", "BANKNIFTY", "SENSEX", "MYSTERY"],
            adapter=adapter,
            sink=sink,
            security_ids={"NIFTY": 13, "BANKNIFTY": 25, "SENSEX": 51},
            start=date(2022, 6, 1),
            end=date(2022, 6, 20),
            session_filter=_no_filter,
        )
    assert scan_resolved_all_empty(report) == ("NIFTY",)
    warned = {e["symbol"] for e in logs if e.get("event") == "dhan.symbol_all_empty"}
    assert warned == {"NIFTY"}


def test_scan_all_empty_empty_report_is_empty() -> None:
    # No resolved symbols / no outcomes → nothing flagged (guards the empty path).
    adapter = _FakeAdapter({})
    sink = _RecordingSink()
    report = run_dhan_spot_ingest_with_retry(
        symbols=["MYSTERY"],
        adapter=adapter,
        sink=sink,
        security_ids={},
        start=date(2022, 6, 1),
        end=date(2022, 6, 20),
        session_filter=_no_filter,
    )
    assert scan_resolved_all_empty(report) == ()


def test_scan_all_empty_resolved_without_outcomes_is_skipped() -> None:
    # Defensive: a report claiming a resolved symbol that has NO matching outcome
    # (never produced by the run path — every resolved symbol is processed) is
    # skipped, not flagged. Covers the `if not windows` guard.
    report = DhanIngestReport(
        outcomes=(),
        resolved_symbols=("GHOST",),
        unresolved_symbols=(),
    )
    assert scan_resolved_all_empty(report) == ()
