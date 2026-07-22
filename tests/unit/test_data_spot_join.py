"""Unit tests for :mod:`brutex.data.spot_join` (DEC-GREEKS-ENGINE-PR2B-001).

The COMMON same-minute spot join the Greeks enrichment consumes (plan §15.4):
probe order (INDEX then CASH), window bounding, null-close drop, the honest
empty join, and the exact-instant key contract.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from brutex.data.models import CandleBar
from brutex.data.spot_join import spot_close_by_ts_window, utc_instant

pl = pytest.importorskip("polars", reason="spot join reads Parquet via polars")

_TS = datetime(2024, 1, 15, 3, 45, tzinfo=UTC)


def _bar(ts: datetime, close: str) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=0,
        open_interest=None,
    )


def _write_spot(lake: Path, segment: str, symbol: str, bars: list[CandleBar]) -> None:
    from brutex.data.parquet_writer import ParquetWriter
    from brutex.data.sink import ParquetBarSink

    ParquetBarSink(writer=ParquetWriter(lake_root=lake)).append(
        exchange="NSE", segment=segment, symbol=symbol, interval="1minute", bars=bars
    )


class TestUtcInstant:
    def test_aware_normalised_to_utc(self) -> None:
        ist = _TS.astimezone(UTC).astimezone(datetime.now(UTC).tzinfo)
        assert utc_instant(ist) == _TS

    def test_naive_read_as_utc(self) -> None:
        assert utc_instant(_TS.replace(tzinfo=None)) == _TS


class TestSpotCloseByTsWindow:
    def test_index_segment_probed_first(self, tmp_path: Path) -> None:
        _write_spot(tmp_path, "INDEX", "NIFTY", [_bar(_TS, "21500")])
        _write_spot(tmp_path, "CASH", "NIFTY", [_bar(_TS, "999")])
        join = spot_close_by_ts_window(
            tmp_path,
            exchange="NSE",
            underlying="NIFTY",
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 20),
        )
        assert join.segment == "INDEX"
        assert join.close_by_ts == {_TS: Decimal("21500")}

    def test_cash_fallback_for_a_stock(self, tmp_path: Path) -> None:
        _write_spot(tmp_path, "CASH", "RELIANCE", [_bar(_TS, "2800")])
        join = spot_close_by_ts_window(
            tmp_path,
            exchange="NSE",
            underlying="RELIANCE",
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 20),
        )
        assert join.segment == "CASH"
        assert join.close_by_ts[_TS] == Decimal("2800")

    def test_absent_underlying_is_an_honest_empty_join(self, tmp_path: Path) -> None:
        join = spot_close_by_ts_window(
            tmp_path,
            exchange="NSE",
            underlying="GHOST",
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 20),
        )
        assert join.segment is None
        assert join.close_by_ts == {}

    def test_window_bounded_to_covered_months(self, tmp_path: Path) -> None:
        # A far-past month OUTSIDE the window must not be read at all.
        far_past = datetime(2023, 6, 1, 4, 0, tzinfo=UTC)
        _write_spot(tmp_path, "INDEX", "NIFTY", [_bar(far_past, "19000")])
        _write_spot(tmp_path, "INDEX", "NIFTY", [_bar(_TS, "21500")])
        join = spot_close_by_ts_window(
            tmp_path,
            exchange="NSE",
            underlying="NIFTY",
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 20),
        )
        assert far_past not in join.close_by_ts
        assert _TS in join.close_by_ts

    def test_null_close_rows_are_dropped(self, tmp_path: Path) -> None:
        base = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024"
        base.mkdir(parents=True)
        pl.DataFrame(
            {
                "timestamp": [_TS, _TS + timedelta(minutes=1)],
                "close": [21500.0, None],
            },
            schema={
                "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
                "close": pl.Float64,
            },
        ).write_parquet(base / "01.parquet")
        join = spot_close_by_ts_window(
            tmp_path,
            exchange="NSE",
            underlying="NIFTY",
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 20),
        )
        assert join.close_by_ts == {_TS: Decimal("21500.0")}

    def test_mixed_schema_months_read_cleanly(self, tmp_path: Path) -> None:
        # One month with an extra column (an enriched-shape file) + one plain:
        # the schema-tolerant scan must not raise (DEC-GREEKS-ENGINE-PR2B-001).
        base = tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024"
        base.mkdir(parents=True)
        pl.DataFrame(
            {"timestamp": [_TS], "close": [21500.0], "iv": [0.2]},
            schema={
                "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
                "close": pl.Float64,
                "iv": pl.Float64,
            },
        ).write_parquet(base / "01.parquet")
        feb_ts = datetime(2024, 2, 1, 4, 0, tzinfo=UTC)
        pl.DataFrame(
            {"timestamp": [feb_ts], "close": [21600.0]},
            schema={
                "timestamp": pl.Datetime(time_unit="us", time_zone="UTC"),
                "close": pl.Float64,
            },
        ).write_parquet(base / "02.parquet")
        join = spot_close_by_ts_window(
            tmp_path,
            exchange="NSE",
            underlying="NIFTY",
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 2, 10),
        )
        assert join.close_by_ts[_TS] == Decimal("21500.0")
        assert join.close_by_ts[feb_ts] == Decimal("21600.0")


class TestGlobMetacharUnderlyingDir:
    """DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001 (the #1594 round-15 metachar law)
    — FAILS-BEFORE: a `[`-carrying underlying dir's month-file list was
    glob-interpreted, joining the SIBLINGS' spot closes into this underlying's
    Greeks spot join."""

    def test_bracket_underlying_reads_literal_files_only(self, tmp_path: Path) -> None:
        import polars as pl

        _write_spot(tmp_path, "CASH", "NSE-X1", [_bar(_TS, "111")])
        _write_spot(tmp_path, "CASH", "NSE-X2", [_bar(_TS, "222")])
        # The bracket dir is planted RAW — the production writer now REFUSES to
        # mint it (partition._FORBIDDEN_SYMBOL_CHARS gained `[`/`]`), so this
        # models a hostile / legacy out-of-band dir exactly like the repro.
        target = (
            tmp_path
            / "bars"
            / "NSE"
            / "CASH"
            / "NSE-X[12]"
            / "1minute"
            / f"{_TS.year:04d}"
            / f"{_TS.month:02d}.parquet"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"timestamp": [_TS], "close": [555.0]}).write_parquet(target)
        join = spot_close_by_ts_window(
            tmp_path,
            exchange="NSE",
            underlying="NSE-X[12]",
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 20),
        )
        assert join.segment == "CASH"
        assert join.close_by_ts == {_TS: Decimal("555.0")}  # the literal dir's OWN close
