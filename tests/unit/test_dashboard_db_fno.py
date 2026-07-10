"""Tests for the ``/db`` F&O candle lake surface (DEC-FNO-CANDLE-LAKE-001).

:func:`brutex.dashboard.db_view.list_fno_candle_datasets` discovers the F&O candle
partition sets the Groww historical pull writes (``bars/<exch>/FNO/<groww_symbol>/…``),
decoded per contract, and EXCLUDES spot; :func:`fno_candle_lake_schema` projects the
documented logical-table columns. Exercised against a real lake written by the
production :class:`~brutex.data.sink.ParquetBarSink`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("polars")

from brutex.dashboard.db_view import fno_candle_lake_schema, list_fno_candle_datasets
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink


def _write(lake: Path, *, exchange: str, segment: str, symbol: str, oi: int | None) -> None:
    """Write one 1-minute bar to ``bars/<exchange>/<segment>/<symbol>/1minute/…``."""
    sink = ParquetBarSink(writer=ParquetWriter(lake_root=lake))
    bar = CandleBar(
        timestamp=datetime(2025, 1, 2, 9, 15, tzinfo=UTC),
        open=Decimal("100.10"),
        high=Decimal("101.20"),
        low=Decimal("99.50"),
        close=Decimal("100.80"),
        volume=375,
        open_interest=oi,
    )
    sink.append(exchange=exchange, segment=segment, symbol=symbol, interval="1minute", bars=[bar])


class TestListFnoCandleDatasets:
    def test_empty_lake_returns_empty(self, tmp_path: Path) -> None:
        assert list_fno_candle_datasets(tmp_path) == []

    def test_lists_fno_only_decoded_excludes_spot(self, tmp_path: Path) -> None:
        _write(
            tmp_path, exchange="NSE", segment="FNO", symbol="NSE-NIFTY-02Jan25-24000-CE", oi=12000
        )
        _write(tmp_path, exchange="NSE", segment="FNO", symbol="NSE-NIFTY-30Jan25-FUT", oi=9000)
        _write(tmp_path, exchange="NSE", segment="CASH", symbol="NSE-NIFTY", oi=None)

        rows = list_fno_candle_datasets(tmp_path)
        # spot CASH excluded; only the two FNO sets, sorted by symbol.
        assert [r["symbol"] for r in rows] == [
            "NSE-NIFTY-02Jan25-24000-CE",
            "NSE-NIFTY-30Jan25-FUT",
        ]
        ce, fut = rows
        assert ce["kind"] == "fno"
        assert ce["segment"] == "FNO"
        assert ce["underlying"] == "NIFTY"
        assert ce["expiry"] == "2025-01-02"
        assert ce["strike"] == "24000"
        assert ce["instrument_type"] == "CE"
        assert ce["option_type"] == "CE"
        # the future leg: no strike / option side
        assert fut["instrument_type"] == "FUT"
        assert fut["strike"] is None
        assert fut["option_type"] is None

    def test_undecodable_fno_dir_still_listed_with_none_identity(self, tmp_path: Path) -> None:
        # A present-but-undecodable FNO partition (bad expiry token) is surfaced
        # honestly, never hidden (Golden Rule #1): the lake is the source of truth.
        _write(tmp_path, exchange="NSE", segment="FNO", symbol="NSE-NIFTY-BADX-FUT", oi=10)
        (row,) = list_fno_candle_datasets(tmp_path)
        assert row["symbol"] == "NSE-NIFTY-BADX-FUT"
        assert row["underlying"] is None
        assert row["expiry"] is None
        assert row["strike"] is None
        assert row["instrument_type"] is None
        assert row["option_type"] is None


class TestFnoCandleLakeSchema:
    def test_projects_the_documented_columns(self) -> None:
        schema = fno_candle_lake_schema()
        assert [c["name"] for c in schema] == [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
            # STORED Greeks overlay (DEC-GREEKS-ENGINE-PR2B-001, plan §15.3) — the ten
            # nullable physical columns the post-pull enrichment writes for options.
            "iv",
            "delta",
            "gamma",
            "theta",
            "vega",
            "rho",
            "spot_at_bar",
            "t_years_used",
            "rate_used",
            "greeks_provenance_id",
            "exchange",
            "segment",
            "underlying",
            "expiry_date",
            "instrument_type",
            "strike",
            "option_type",
            "interval",
            # READ-time derived combined moneyness label (DEC-FNO-MONEYNESS-001) + the
            # value columns (DEC-DB-FNO-COLUMNS-001).
            "moneyness",
            "spot",
            "intrinsic",
            "time_value",
            "days_to_expiry",
            # READ-time derived decoded Greeks status (DEC-DB-FNO-DISPLAY-TRUTH-001).
            "greeks_status",
        ]
        # each entry is a flat JSON-friendly dict
        assert all({"name", "dtype", "source", "nullable", "doc"} == set(c) for c in schema)
        derived = {c["name"]: c for c in schema if c["source"] == "derived"}
        assert set(derived) == {
            "moneyness",
            "spot",
            "intrinsic",
            "time_value",
            "days_to_expiry",
            "greeks_status",
        }
