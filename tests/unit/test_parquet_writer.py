"""Tests for ``brutex.data.parquet_writer`` 2dp tick-grid rounding.

DEC-SPOT-PRICE-2DP-001 — the spot lake stores OHLC snapped to the 2-decimal
rupee tick grid (``ROUND_HALF_UP``, never truncation), mirroring the option
writer, so ``/db`` shows clean values and re-pulls stay bit-idempotent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter, _tick_round


def _bar(
    minute: int,
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: int = 0,
) -> CandleBar:
    return CandleBar(
        timestamp=datetime(2021, 1, 4, 4, minute, tzinfo=UTC),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        open_interest=None,
    )


class TestTickRound:
    def test_rounds_half_up_not_truncated(self) -> None:
        """14020.7999 → 14020.80 — proper round-half-up, NOT truncation to .79."""
        out = _tick_round(
            _bar(0, open_="14020.7999", high="14020.7999", low="14020.7999", close="14020.7999")
        )
        assert out.open == Decimal("14020.80")
        assert out.high == Decimal("14020.80")
        assert out.low == Decimal("14020.80")
        assert out.close == Decimal("14020.80")

    def test_exact_half_rounds_up(self) -> None:
        """A clean .005 rounds UP to .01 (HALF_UP, not bankers'/HALF_EVEN)."""
        out = _tick_round(_bar(0, open_="1.005", high="1.005", low="1.005", close="1.005"))
        assert out.open == Decimal("1.01")

    def test_below_half_rounds_down(self) -> None:
        out = _tick_round(_bar(0, open_="1.004", high="1.004", low="1.004", close="1.004"))
        assert out.open == Decimal("1.00")

    def test_volume_oi_timestamp_untouched(self) -> None:
        src = _bar(7, open_="100.126", high="100.126", low="100.126", close="100.126", volume=42)
        out = _tick_round(src)
        assert out.volume == 42
        assert out.timestamp == src.timestamp
        assert out.open == Decimal("100.13")


class TestWriterRoundTrip:
    def test_lake_stores_two_decimals(self, tmp_path: Path) -> None:
        pl = pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[
                _bar(0, open_="14020.7999", high="14021.0004", low="14019.4996", close="14020.5001")
            ],
        )
        files = list(tmp_path.rglob("*.parquet"))
        assert len(files) == 1
        df = pl.read_parquet(files[0])
        assert df["open"][0] == 14020.80
        assert df["high"][0] == 14021.00
        assert df["low"][0] == 14019.50
        assert df["close"][0] == 14020.50

    def test_repull_of_raw_4dp_is_idempotent(self, tmp_path: Path) -> None:
        """Re-pulling the SAME raw 4dp bar is a no-op — no false conflict."""
        pytest.importorskip("polars")
        writer = ParquetWriter(lake_root=tmp_path)
        bars = [
            _bar(0, open_="14020.7999", high="14021.0004", low="14019.4996", close="14020.5001")
        ]
        first = writer.write(
            exchange="NSE", segment="CASH", symbol="NSE-NIFTY", interval="1minute", bars=bars
        )
        second = writer.write(
            exchange="NSE", segment="CASH", symbol="NSE-NIFTY", interval="1minute", bars=bars
        )
        assert second.row_count == 1
        assert first.raw_hash == second.raw_hash  # byte-identical → idempotent
