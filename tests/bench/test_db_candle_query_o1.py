"""O(1)-as-the-lake-grows benchmark for the /db candle pager baseline.

DEC-DB-UNIVERSE-O1-LATENCY-001. The ``/db`` candle grid appends derived
``change_pct`` / ``open_gap_pct`` columns to every page; computing them needs the
prior trading day's last 1-minute close. The OLD reader aggregated the WHOLE
multi-year 1-minute partition (``GROUP BY`` over every row) on EVERY paging click,
so a click on a 6-year symbol scanned hundreds of thousands of rows. The new path
(:func:`brutex.dashboard.db_view._intraday_prev_close_by_page_date`) reads only the
page's OWN date span plus the single bar immediately before it.

The load-bearing property benched here is **page latency independent of how many
days of history the partition holds**: querying the same page against a partition
with 12× more trading days must cost essentially the same, because the baseline
read is bounded by the page's date span (~1 trading day for a dense intraday page),
not by the partition's total depth. A regression that re-introduced the
full-partition ``GROUP BY`` would scale with the stored row count and blow the
ratio. DuckDB still reads each matched Parquet file's footer metadata (cheap,
KB-scale), so this is O(page date-span) + O(partition file count) — flat in the
row count, NOT a literal O(1)-total claim (Golden Rule #10).

DuckDB + Parquet I/O is noisier than pure CPU, so the shared-CI ceiling matches the
other I/O-touching gates (``< 8×``); the M4-Pro strict tier tightens it via
:func:`ratio_ceiling`.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.bench._o1_tiers import ratio_ceiling

# The data layer (polars writer + duckdb reader) lives behind the ``[data]``
# extra; the lean ``bench`` job may lack it. Skip the whole module then, mirroring
# ``test_parquet_write_o1.py``. Data-layer symbols are imported only after the guard.
pytest.importorskip("polars")
pytest.importorskip("duckdb")

import duckdb
from brutex.dashboard.db_view import query_candles
from brutex.data import CandleBar
from brutex.data.parquet_writer import ParquetWriter

#: Trading-day depths the page latency must be independent of (12× difference).
_SMALL_DAYS = 10
_LARGE_DAYS = 120
#: Bars per trading day — enough that a single page lands inside one day, so the
#: bounded baseline reads ~one day regardless of total depth.
_BARS_PER_DAY = 200
#: Page the viewer requests (offset 0, the default first page).
_LIMIT = 100
#: How many page queries to time at each depth (averaged).
_QUERIES = 30


def _bar(ts: datetime, close: str) -> CandleBar:
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal(close),
        volume=10,
        open_interest=None,
    )


def _next_weekday(d: datetime) -> datetime:
    while d.weekday() >= 5:  # skip Sat/Sun → real trading-day gaps
        d += timedelta(days=1)
    return d


def _build_partition(root: Path, days: int) -> None:
    """Write *days* trading days of session 1-minute bars (09:15 IST = 03:45 UTC).

    The writer requires one ``(year, month)`` partition per call, so bars are
    grouped by month and written month-by-month (matching the production ingest).
    """
    writer = ParquetWriter(lake_root=root)
    by_month: dict[tuple[int, int], list[CandleBar]] = {}
    cursor = _next_weekday(datetime(2024, 1, 1, 3, 45, tzinfo=UTC))
    for _di in range(days):
        cursor = _next_weekday(cursor)
        for bi in range(_BARS_PER_DAY):
            # The close value is irrelevant to the timing — keep it in the fixed
            # [low=90, high=110] band so CandleBar's OHLC invariant holds.
            bar = _bar(cursor + timedelta(minutes=bi), "100.00")
            by_month.setdefault((cursor.year, cursor.month), []).append(bar)
        cursor += timedelta(days=1)
    for bars in by_month.values():
        writer.write(
            exchange="NSE", segment="CASH", symbol="NSE-NIFTY", interval="1minute", bars=bars
        )


def _query(conn: object, root: Path) -> None:
    query_candles(
        conn,
        root,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        limit=_LIMIT,
        offset=0,
    )


class TestCandlePageLatencyIsFlatAsLakeGrows:
    """``query_candles`` page latency is independent of the partition's depth."""

    def test_page_at_small_partition(self, benchmark, tmp_path: Path) -> None:
        _build_partition(tmp_path, _SMALL_DAYS)
        conn = duckdb.connect()
        benchmark(lambda: _query(conn, tmp_path))

    def test_page_at_large_partition(self, benchmark, tmp_path: Path) -> None:
        _build_partition(tmp_path, _LARGE_DAYS)
        conn = duckdb.connect()
        benchmark(lambda: _query(conn, tmp_path))

    def test_page_latency_ratio_below_8x(self, tmp_path: Path) -> None:
        """First-page latency at 120 vs 10 trading days stays flat (< 8×)."""

        def _time(root: Path, days: int) -> float:
            _build_partition(root, days)
            conn = duckdb.connect()
            for _ in range(5):  # warm DuckDB + the OS page cache
                _query(conn, root)
            t0 = time.perf_counter_ns()
            for _ in range(_QUERIES):
                _query(conn, root)
            return (time.perf_counter_ns() - t0) / _QUERIES

        t_small = _time(tmp_path / "s", _SMALL_DAYS)
        t_large = _time(tmp_path / "l", _LARGE_DAYS)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(8.0), (
            f"query_candles page latency ratio {ratio:.2f}× ({_LARGE_DAYS} vs "
            f"{_SMALL_DAYS} trading days) exceeds 8× — the derived-column baseline "
            f"may have regressed to a full-partition scan (O(stored rows)). "
            f"t_small={t_small:.0f}ns, t_large={t_large:.0f}ns."
        )
