"""End-to-end proof for the gainers/losers screener over a real lake (DEC-MOVERS-GAINERS-001).

The screener core is unit-tested with a fake reader; this INTEGRATION test is
the honest validation that a :class:`LakeReader` backed by the REAL Parquet +
DuckDB lake (``Catalog.get_bars``) feeds the screen correctly — it persists a
tiny two-instrument lake with the same ``ParquetWriter`` / ``ParquetBarSink``
ingest uses, then ranks it and inspects the result.

The in-test :class:`_CatalogLakeReader` is the deferred dashboard wiring's
stand-in: it reuses :func:`brutex.feed.replay._arrow_table_to_bars` (CLAUDE.md
§11 COMMON — no copy-paste of the Arrow→CandleBar conversion) so the only new
code is the partition-coordinate glue.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.catalog import Catalog
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.feed.replay import _arrow_table_to_bars
from brutex.screeners.gainers_losers import InstrumentRef, screen_gainers_losers

pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_INTERVAL = "1day"
_START = datetime(2024, 1, 1, tzinfo=UTC)
_END = datetime(2024, 3, 1, tzinfo=UTC)


def _bar(*, day: int, open_: str, close: str) -> CandleBar:
    """A valid daily bar on ``2024-01-<day>`` (09:15 IST ≈ 03:45 UTC)."""
    o = Decimal(open_)
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(2024, 1, day, 3, 45, tzinfo=UTC),
        open=o,
        high=max(o, c),
        low=min(o, c),
        close=c,
        volume=0,
        open_interest=None,
    )


@dataclass(slots=True)
class _CatalogLakeReader:
    """A real :class:`LakeReader` over the Parquet+DuckDB lake (deferred-UI glue)."""

    lake_root: Path
    catalog: Catalog
    refs: list[InstrumentRef]

    def list_instruments(self) -> list[InstrumentRef]:
        return list(self.refs)

    def read_window(
        self,
        ref: InstrumentRef,
        *,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[CandleBar]:
        table = self.catalog.get_bars(
            lake_root=self.lake_root,
            exchange=ref.exchange,
            segment=ref.segment,
            symbol=ref.symbol,
            interval=interval,
            start=start,
            end=end,
        )
        return _arrow_table_to_bars(table)


def _persist(tmp_path: Path) -> tuple[Path, Catalog]:
    """Write a NIFTY (gainer) + BANKNIFTY (loser) daily lake; return root+catalog."""
    lake_root = tmp_path / "lake"
    catalog = Catalog.open(tmp_path / "catalog.duckdb")
    writer = ParquetWriter(lake_root=lake_root)
    sink = ParquetBarSink(writer=writer, catalog=catalog)
    sink.append(
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol="NIFTY",
        interval=_INTERVAL,
        bars=[
            _bar(day=2, open_="20000.00", close="20100.00"),
            _bar(day=3, open_="20100.00", close="22000.00"),
        ],
    )
    sink.append(
        exchange=_EXCHANGE,
        segment=_SEGMENT,
        symbol="BANKNIFTY",
        interval=_INTERVAL,
        bars=[
            _bar(day=2, open_="45000.00", close="44000.00"),
            _bar(day=3, open_="44000.00", close="40500.00"),
        ],
    )
    return lake_root, catalog


def test_screen_ranks_a_real_parquet_lake(tmp_path: Path) -> None:
    lake_root, catalog = _persist(tmp_path)
    refs = [
        InstrumentRef(symbol="NIFTY", exchange=_EXCHANGE, segment=_SEGMENT),
        InstrumentRef(symbol="BANKNIFTY", exchange=_EXCHANGE, segment=_SEGMENT),
    ]
    reader = _CatalogLakeReader(lake_root=lake_root, catalog=catalog, refs=refs)
    try:
        result = screen_gainers_losers(reader, interval=_INTERVAL, start=_START, end=_END, top_n=5)
    finally:
        catalog.close()

    # NIFTY ran 20000 → 22000 (+10%); BANKNIFTY 45000 → 40500 (-10%).
    assert [m.ref.symbol for m in result.gainers] == ["NIFTY"]
    assert [m.ref.symbol for m in result.losers] == ["BANKNIFTY"]
    assert result.gainers[0].base_price == Decimal("20000.00")
    assert result.gainers[0].last_price == Decimal("22000.00")
    assert result.gainers[0].pct_change == Decimal("10")
    assert result.gainers[0].bar_count == 2
    assert result.losers[0].pct_change == Decimal("-10")
    assert result.skipped == ()


def test_screen_skips_an_instrument_absent_from_the_lake(tmp_path: Path) -> None:
    lake_root, catalog = _persist(tmp_path)
    refs = [
        InstrumentRef(symbol="NIFTY", exchange=_EXCHANGE, segment=_SEGMENT),
        # SENSEX was never persisted → its window is empty → skipped.
        InstrumentRef(symbol="SENSEX", exchange="BSE", segment=_SEGMENT),
    ]
    reader = _CatalogLakeReader(lake_root=lake_root, catalog=catalog, refs=refs)
    try:
        result = screen_gainers_losers(reader, interval=_INTERVAL, start=_START, end=_END, top_n=5)
    finally:
        catalog.close()

    assert [m.ref.symbol for m in result.gainers] == ["NIFTY"]
    assert result.skipped == (InstrumentRef(symbol="SENSEX", exchange="BSE", segment=_SEGMENT),)
