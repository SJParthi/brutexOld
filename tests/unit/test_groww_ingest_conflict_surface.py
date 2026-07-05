"""Spot-spine vendor-surface threading (DEC-VENDOR-CONFLICT-SURFACE-001).

The spot ingest spine folds each window's
:attr:`~brutex.data.sink.SinkAppendResult.vendor_dup_rows_dropped` (sum) +
``max_close_delta`` (exact-Decimal max) into the :class:`IngestReport` on the
SYNC path, and hands them to ``FetchPipeline.fold_totals`` on the ASYNC path —
so the counters that previously died in ``lake.vendor_duplicate_rows`` /
``ingest.conflict.summary`` log lines reach the phase result.

Fail-before proof: on origin/main ``IngestReport`` has neither field
(``AttributeError`` below) and ``fold_totals`` receives neither kwarg —
this suite cannot pass there.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.fetch_pipeline import RawFetchResult
from brutex.adapters.groww.ingest import ingest_spot
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.orchestrator.universe_phases import ItemResult

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

# 04:05 / 04:06 UTC = 09:35 / 09:36 IST → inside the [09:15, 15:30) session.
_TS0 = datetime(2021, 1, 4, 4, 5, tzinfo=UTC)
_TS1 = datetime(2021, 1, 4, 4, 6, tzinfo=UTC)


def _bar(ts: datetime, *, close: str = "100.00") -> CandleBar:
    c = Decimal(close)
    return CandleBar(timestamp=ts, open=c, high=c, low=c, close=c, volume=5, open_interest=None)


def _inst() -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY",
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


class _Adapter:
    def __init__(self, bars: Sequence[CandleBar]) -> None:
        self._bars = list(bars)

    def fetch_ohlcv(
        self, *, symbol: str, exchange: object, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        del symbol, exchange, interval, start, end
        return list(self._bars)


def _sync_run_parallel[ItemT, ResultT](
    fn: Callable[[ItemT], ResultT],
    items: Sequence[ItemT],
) -> list[ItemResult[ItemT, ResultT]]:
    out: list[ItemResult[ItemT, ResultT]] = []
    for item in items:
        try:
            value = fn(item)
        except Exception as exc:  # mirror PoolRunParallel's capture
            out.append(ItemResult(item=item, ok=False, result=None, exc=exc))
        else:
            out.append(ItemResult(item=item, ok=True, result=value, exc=None))
    return out


def _pull(sink: ParquetBarSink, bars: Sequence[CandleBar]) -> object:
    return ingest_spot(
        _Adapter(bars),  # type: ignore[arg-type]
        sink,
        instrument=_inst(),
        interval="1minute",
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
        run_parallel=_sync_run_parallel,
    )


def test_sync_report_carries_dup_and_delta(tmp_path: Path) -> None:
    """A re-pull with a restated close + a duplicated row reaches the report.

    Pull 1 stores clean bars; pull 2's payload carries a WITHIN-BATCH duplicate
    (dropped, counted) AND re-states TS0's close 100.00 → 144.70 (keep-first
    conflict, exact ₹44.70 delta). Previously both counters died in log lines.
    """
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))
    first = _pull(sink, [_bar(_TS0), _bar(_TS1, close="101.00")])
    assert first.vendor_dup_rows_dropped == 0  # type: ignore[attr-defined]
    assert first.max_close_delta is None  # type: ignore[attr-defined]

    second = _pull(
        sink,
        [
            _bar(_TS0, close="144.70"),  # restates the stored 100.00 → conflict
            _bar(_TS1, close="101.00"),
            _bar(_TS1, close="101.00"),  # within-batch duplicate → dropped + counted
        ],
    )
    assert second.conflicts == 1  # type: ignore[attr-defined]
    assert second.vendor_dup_rows_dropped == 1  # type: ignore[attr-defined]
    assert second.max_close_delta == Decimal("44.70")  # type: ignore[attr-defined]


def test_async_tail_folds_vendor_surface_into_pipeline(tmp_path: Path) -> None:
    """The async tail hands BOTH counters to ``fold_totals`` (the SeriesTotals seam)."""

    class _CapturePipeline:
        def __init__(self) -> None:
            self.captured: RawFetchResult | None = None
            self.folded: dict[str, object] | None = None

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
            self.folded = {
                "bars_appended": bars_appended,
                "conflicts": conflicts,
                "series_key": series_key,
                "vendor_dup_rows_dropped": vendor_dup_rows_dropped,
                "max_close_delta": max_close_delta,
            }

    sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))
    _pull(sink, [_bar(_TS0)])  # seed the stored bar the async re-pull conflicts against

    pipeline = _CapturePipeline()
    ingest_spot(
        _Adapter([_bar(_TS0, close="102.50"), _bar(_TS1), _bar(_TS1)]),  # type: ignore[arg-type]
        sink,
        instrument=_inst(),
        interval="1minute",
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),
        run_parallel=_sync_run_parallel,
        fetch_pipeline=pipeline,  # type: ignore[arg-type]
    )
    captured = pipeline.captured
    assert captured is not None
    captured.tail.process(captured.tail)  # drive the consumer tail deterministically
    assert pipeline.folded is not None
    assert pipeline.folded["conflicts"] == 1
    assert pipeline.folded["vendor_dup_rows_dropped"] == 1
    assert pipeline.folded["max_close_delta"] == Decimal("2.50")
    assert pipeline.folded["series_key"] == ("NIFTY", "1minute")
