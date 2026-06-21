"""Unit tests for the post-pull movers warm (DEC-PULL-MOVERS-WARM-001).

`latest_movers_date` over a real seeded Parquet+DuckDB lake (latest day / empty → None),
plus the pure `warm_movers_snapshots` best-effort job runner (counts non-empty, a 0-count
job is not counted, a raising job is logged + counted 0 — never re-raised).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from brutex.dashboard.movers_warm import latest_movers_date, warm_movers_snapshots
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter


def _day_bar(day: date, close: str) -> CandleBar:
    c = Decimal(close)
    return CandleBar(
        timestamp=datetime(day.year, day.month, day.day, 3, 45, tzinfo=UTC),  # 09:15 IST
        open=c,
        high=c,
        low=c,
        close=c,
        volume=100,
        open_interest=None,
    )


def test_latest_movers_date_returns_newest_ist_date(tmp_path: Path) -> None:
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1day",
        bars=[_day_bar(date(2024, 6, 12), "20000"), _day_bar(date(2024, 6, 13), "20100")],
    )
    assert latest_movers_date(tmp_path) == "2024-06-13"


def test_latest_movers_date_spans_index_and_cash(tmp_path: Path) -> None:
    # The newest day may live in the CASH segment (a stock), not INDEX — both are scanned.
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="INDEX",
        symbol="NIFTY",
        interval="1day",
        bars=[_day_bar(date(2024, 6, 10), "20000")],
    )
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="CASH",
        symbol="RELIANCE",
        interval="1day",
        bars=[_day_bar(date(2024, 6, 14), "1300")],
    )
    assert latest_movers_date(tmp_path) == "2024-06-14"


def test_latest_movers_date_none_on_empty_lake(tmp_path: Path) -> None:
    assert latest_movers_date(tmp_path) is None


def test_latest_movers_date_none_when_only_non_spot_segments(tmp_path: Path) -> None:
    # An FNO-only lake (no INDEX/CASH 1day) → no spot movers date.
    ParquetWriter(lake_root=tmp_path).write(
        exchange="NSE",
        segment="FNO",
        symbol="NIFTY24JUN20000CE",
        interval="1day",
        bars=[_day_bar(date(2024, 6, 13), "100")],
    )
    assert latest_movers_date(tmp_path) is None


def test_warm_counts_only_non_empty_jobs() -> None:
    # 3 jobs: spot warmed 5, futures warmed 0 (empty), options warmed 2 → 2 non-empty.
    assert warm_movers_snapshots([lambda: 5, lambda: 0, lambda: 2]) == 2


def test_warm_empty_job_list_is_zero() -> None:
    assert warm_movers_snapshots([]) == 0


def test_warm_swallows_a_raising_job_counts_it_zero() -> None:
    # A transient lake-read race in one warmer is logged + counted 0, never re-raised —
    # the already-successful pull is never failed by a warm hiccup.
    def _boom() -> int:
        raise RuntimeError("transient lake read race")

    assert warm_movers_snapshots([lambda: 3, _boom, lambda: 1]) == 2
