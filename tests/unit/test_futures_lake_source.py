"""Unit tests for the futures F&O candle-lake bar source (DEC-FUTURES-EXEC-TRANSLATION-001).

Exercised against a REAL tmp lake written with the PRODUCTION writers
(:class:`~brutex.data.sink.ParquetBarSink` / ``ParquetWriter``): exact-minute
matching, honest ``None`` (no nearest-bar substitution, R15), keep-FIRST dedup,
corrupt-partition quarantine-skip, and schema-tolerant reads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("polars")
pytest.importorskip("pyarrow")

import polars as pl
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.futures.lake_source import PARQUET_SKIP_EVENT, LakeFuturesBarSource
from structlog.testing import capture_logs

_IST = ZoneInfo("Asia/Kolkata")
_ENTRY = datetime(2025, 1, 2, 9, 16, tzinfo=_IST)
_EXIT = datetime(2025, 1, 2, 15, 20, tzinfo=_IST)
_SYMBOL = "NSE-NIFTY-30Jan25-FUT"


def _bar(ts_ist: datetime, close: str, *, oi: int | None = 5_000, volume: int = 50) -> CandleBar:
    c = Decimal(close)
    return CandleBar(
        timestamp=ts_ist.astimezone(UTC),
        open=c,
        high=c + Decimal("1.00"),
        low=c - Decimal("1.00"),
        close=c,
        volume=volume,
        open_interest=oi,
    )


def _write_contract(lake: Path, *, symbol: str = _SYMBOL, bars: list[CandleBar]) -> None:
    ParquetBarSink(writer=ParquetWriter(lake_root=lake)).append(
        exchange=symbol.split("-", maxsplit=1)[0],
        segment="FNO",
        symbol=symbol,
        interval="1minute",
        bars=bars,
    )


def _session_bars() -> list[CandleBar]:
    return [
        _bar(_ENTRY, "22000.00"),
        _bar(_ENTRY + timedelta(minutes=1), "22010.00"),
        # a genuine liquidity hole: 09:18 is NOT printed
        _bar(_ENTRY + timedelta(minutes=3), "22020.00"),
        _bar(_EXIT, "22100.00"),
    ]


class TestLoadWindow:
    def test_exact_minute_window_with_path(self, tmp_path: Path) -> None:
        _write_contract(tmp_path, bars=_session_bars())
        source = LakeFuturesBarSource(tmp_path)
        window = source.load_window(contract_symbol=_SYMBOL, entry_ts=_ENTRY, exit_ts=_EXIT)
        assert window is not None
        assert window.entry_bar.open == Decimal("22000.00")
        assert window.exit_bar.open == Decimal("22100.00")
        # The path is every PRINTED bar in [entry, exit] — the 09:18 hole is
        # honestly absent, never interpolated.
        assert len(window.path) == 4
        assert window.path[0] is window.entry_bar
        assert window.path[-1] is window.exit_bar
        assert window.entry_bar.open_interest == 5_000

    def test_r15_missing_entry_minute_returns_none(self, tmp_path: Path) -> None:
        # [R15] the entry minute never printed → honest None; the 09:17 bar
        # one minute LATER must NOT be substituted.
        bars = [_bar(_ENTRY + timedelta(minutes=1), "22010.00"), _bar(_EXIT, "22100.00")]
        _write_contract(tmp_path, bars=bars)
        source = LakeFuturesBarSource(tmp_path)
        assert source.load_window(contract_symbol=_SYMBOL, entry_ts=_ENTRY, exit_ts=_EXIT) is None

    def test_r15_missing_exit_minute_returns_none(self, tmp_path: Path) -> None:
        bars = [_bar(_ENTRY, "22000.00"), _bar(_EXIT - timedelta(minutes=1), "22090.00")]
        _write_contract(tmp_path, bars=bars)
        source = LakeFuturesBarSource(tmp_path)
        assert source.load_window(contract_symbol=_SYMBOL, entry_ts=_ENTRY, exit_ts=_EXIT) is None

    def test_absent_contract_returns_none(self, tmp_path: Path) -> None:
        source = LakeFuturesBarSource(tmp_path)
        assert source.load_window(contract_symbol=_SYMBOL, entry_ts=_ENTRY, exit_ts=_EXIT) is None

    def test_bar_cache_one_read_per_contract(self, tmp_path: Path) -> None:
        _write_contract(tmp_path, bars=_session_bars())
        source = LakeFuturesBarSource(tmp_path)
        first = source.load_window(contract_symbol=_SYMBOL, entry_ts=_ENTRY, exit_ts=_EXIT)
        assert first is not None
        # Remove the partition — the cached map still serves (ONE lake read).
        import shutil

        shutil.rmtree(tmp_path / "bars")
        second = source.load_window(contract_symbol=_SYMBOL, entry_ts=_ENTRY, exit_ts=_EXIT)
        assert second is not None
        assert second.entry_bar == first.entry_bar

    def test_null_open_interest_preserved(self, tmp_path: Path) -> None:
        _write_contract(tmp_path, bars=[_bar(_ENTRY, "22000.00", oi=None), _bar(_EXIT, "22100.00")])
        source = LakeFuturesBarSource(tmp_path)
        window = source.load_window(contract_symbol=_SYMBOL, entry_ts=_ENTRY, exit_ts=_EXIT)
        assert window is not None
        assert window.entry_bar.open_interest is None


class TestUniversalHardening:
    def test_universal_poisoned_duplicate_keeps_first(self, tmp_path: Path) -> None:
        # [chaos] a poisoned duplicate timestamp in a SECOND file of the same
        # month dir must keep-FIRST (never last-wins overwrite the real bar).
        _write_contract(tmp_path, bars=_session_bars())
        month_dir = tmp_path / "bars" / "NSE" / "FNO" / _SYMBOL / "1minute" / "2025"
        poison = pl.DataFrame(
            {
                "timestamp": [_ENTRY.astimezone(UTC)],
                "open": [99999.0],
                "high": [99999.0],
                "low": [99999.0],
                "close": [99999.0],
                "volume": [1],
                "open_interest": [1],
            }
        ).with_columns(pl.col("timestamp").dt.cast_time_unit("us").dt.convert_time_zone("UTC"))
        # Sorts AFTER 01.parquet in the glob walk → the REAL bar stays first.
        poison.write_parquet(month_dir / "01_poison.parquet")
        source = LakeFuturesBarSource(tmp_path)
        window = source.load_window(contract_symbol=_SYMBOL, entry_ts=_ENTRY, exit_ts=_EXIT)
        assert window is not None
        assert window.entry_bar.open == Decimal("22000.00")  # not 99999

    def test_universal_corrupt_file_quarantine_skipped(self, tmp_path: Path) -> None:
        # [chaos] one corrupt Parquet file: the fast whole-tree scan fails,
        # the fallback serves the readable months + counts + WARNs the skip.
        _write_contract(tmp_path, bars=_session_bars())
        month_dir = tmp_path / "bars" / "NSE" / "FNO" / _SYMBOL / "1minute" / "2025"
        (month_dir / "00_corrupt.parquet").write_bytes(b"NOT A PARQUET FILE")
        source = LakeFuturesBarSource(tmp_path)
        with capture_logs() as logs:
            window = source.load_window(contract_symbol=_SYMBOL, entry_ts=_ENTRY, exit_ts=_EXIT)
        assert window is not None
        assert window.entry_bar.open == Decimal("22000.00")
        assert source.corrupt_skipped == 1
        assert PARQUET_SKIP_EVENT in [x["event"] for x in logs]

    def test_universal_all_corrupt_returns_none(self, tmp_path: Path) -> None:
        month_dir = tmp_path / "bars" / "NSE" / "FNO" / _SYMBOL / "1minute" / "2025"
        month_dir.mkdir(parents=True)
        (month_dir / "01.parquet").write_bytes(b"garbage")
        source = LakeFuturesBarSource(tmp_path)
        with capture_logs():
            assert (
                source.load_window(contract_symbol=_SYMBOL, entry_ts=_ENTRY, exit_ts=_EXIT) is None
            )
        assert source.corrupt_skipped == 1

    def test_universal_schema_tolerant_mixed_columns(self, tmp_path: Path) -> None:
        # [chaos] a 7-column month beside an extra-column month (the enriched
        # 17-col shape's shadow) reads without a crash; the extra column is
        # ignored and the missing one inserts NULL.
        _write_contract(tmp_path, bars=[_bar(_ENTRY, "22000.00")])
        month_dir = tmp_path / "bars" / "NSE" / "FNO" / _SYMBOL / "1minute" / "2025"
        extra = pl.DataFrame(
            {
                "timestamp": [_EXIT.astimezone(UTC)],
                "open": [22100.0],
                "high": [22101.0],
                "low": [22099.0],
                "close": [22100.0],
                "volume": [10],
                "open_interest": [100],
                "iv": [0.15],  # an overlay-style extra column
            }
        ).with_columns(pl.col("timestamp").dt.cast_time_unit("us").dt.convert_time_zone("UTC"))
        extra.write_parquet(month_dir / "01_extra.parquet")
        source = LakeFuturesBarSource(tmp_path)
        window = source.load_window(contract_symbol=_SYMBOL, entry_ts=_ENTRY, exit_ts=_EXIT)
        assert window is not None
        assert window.exit_bar.open == Decimal("22100.0")
