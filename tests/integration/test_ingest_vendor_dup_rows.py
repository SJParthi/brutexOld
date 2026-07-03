"""Vendor duplicate rows through the REAL ingest spine + F&O sink (DEC-LAKE-WRITE-UNIQUE-001).

The measured failure mode: Groww's payload physically carried duplicate
1-minute rows (135 in one ADANIENT January window, reproduced across two
pulls) and the lake's FRESH render stored them verbatim. These tests drive a
duplicated vendor batch through:

* the REAL spot ingest (:func:`brutex.adapters.groww.ingest.ingest_spot` with a
  fake adapter + the REAL :class:`~brutex.data.sink.ParquetBarSink` +
  :class:`~brutex.data.parquet_writer.ParquetWriter` on a tmp lake) — the fresh
  partition file must hold UNIQUE timestamps, the aggregated
  ``lake.vendor_duplicate_rows`` WARNING must fire, and a differing-value dup
  pair must surface as a keep-first conflict through the EXISTING
  ``ingest.conflict.summary`` machinery;
* the SAME sink under the F&O candle-lake coordinates (``segment="FNO"``,
  a decoded contract symbol, ``open_interest`` carried) — the F&O lake rides
  the identical writer, so the invariant must hold there too.

Skipped when ``polars`` / ``pyarrow`` are absent (the ``[data]`` extra).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.adapters.groww.ingest import ingest_spot
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.orchestrator.universe_phases import ItemResult
from structlog.testing import capture_logs

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

# 04:05 / 04:06 UTC = 09:35 / 09:36 IST → inside the [09:15, 15:30) session.
_TS0 = datetime(2021, 1, 4, 4, 5, tzinfo=UTC)
_TS1 = datetime(2021, 1, 4, 4, 6, tzinfo=UTC)


def _bar(ts: datetime, *, close: str = "100.00", oi: int | None = None) -> CandleBar:
    c = Decimal(close)
    return CandleBar(
        timestamp=ts,
        open=c,
        high=c,
        low=c,
        close=c,
        volume=5,
        open_interest=oi,
    )


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


class _DupAdapter:
    """Fake vendor adapter replaying a payload that carries duplicate rows."""

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
    return [e for e in logs if e["event"] == name]


def test_spot_ingest_dup_vendor_batch_stores_unique_warns_and_conflicts(
    tmp_path: Path,
) -> None:
    """FAILS on unfixed main: the fresh partition physically stored 4 rows (the
    duplicates verbatim), no ``lake.vendor_duplicate_rows`` WARNING, no conflict."""
    import polars as pl

    payload = [
        _bar(_TS0, close="100.00"),
        _bar(_TS0, close="100.00"),  # identical duplicate (the measured ADANIENT shape)
        _bar(_TS0, close="999.00"),  # differing duplicate → keep-first conflict
        _bar(_TS1, close="101.00"),
    ]
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))
    with capture_logs() as logs:
        report = ingest_spot(
            _DupAdapter(payload),  # type: ignore[arg-type]
            sink,
            instrument=_inst(),
            interval="1minute",
            start=date(2021, 1, 4),
            end=date(2021, 1, 5),
            run_parallel=_sync_run_parallel,
        )
    assert report.windows == 1
    assert report.conflicts == 1  # the differing dup, via the existing conflict channel
    partition = tmp_path / "bars" / "NSE" / "CASH" / "NIFTY" / "1minute" / "2021" / "01.parquet"
    stored = pl.read_parquet(partition).sort("timestamp")
    assert stored["timestamp"].to_list() == [_TS0, _TS1]  # UNIQUE — the poison never lands
    assert stored["close"].to_list() == [100.0, 101.0]  # first occurrence won
    warns = _events(logs, "lake.vendor_duplicate_rows")
    assert len(warns) == 1
    assert warns[0]["symbol"] == "NIFTY"
    assert warns[0]["interval"] == "1minute"
    assert warns[0]["dropped"] == 2
    # The in-batch keep-first conflict rides the EXISTING aggregated summary WARNING.
    summaries = _events(logs, "ingest.conflict.summary")
    assert len(summaries) == 1


def test_fno_sink_dup_batch_stores_unique_with_open_interest(tmp_path: Path) -> None:
    """FAILS on unfixed main: the F&O partition stored the duplicate row verbatim."""
    import polars as pl

    sink = ParquetBarSink(writer=ParquetWriter(lake_root=tmp_path))
    contract = "NSE-NIFTY-30Jan25-FUT"
    with capture_logs() as logs:
        result = sink.append(
            exchange="NSE",
            segment="FNO",
            symbol=contract,
            interval="1minute",
            bars=[
                _bar(_TS0, close="100.00", oi=500),
                _bar(_TS0, close="100.00", oi=500),
                _bar(_TS1, close="101.00", oi=510),
            ],
        )
    assert result.bars_appended == 3  # the historical INPUT-count contract
    assert result.bars_net_new == 2  # the honest deduped count
    partition = tmp_path / "bars" / "NSE" / "FNO" / contract / "1minute" / "2021" / "01.parquet"
    stored = pl.read_parquet(partition).sort("timestamp")
    assert stored["timestamp"].to_list() == [_TS0, _TS1]
    assert stored["open_interest"].to_list() == [500, 510]
    warns = _events(logs, "lake.vendor_duplicate_rows")
    assert len(warns) == 1
    assert warns[0]["symbol"] == contract
    assert warns[0]["dropped"] == 1
