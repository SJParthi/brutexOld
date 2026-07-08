"""Production-scale HONESTY bench for the /db candle pager (DEC-DB-OPAGE-HONESTY-001).

The sibling :mod:`tests.bench.test_db_candle_query_o1` pins the DERIVED-COLUMN
baseline read (``_intraday_prev_close_by_page_date``) flat as the partition
deepens — and at its ~24k-row top it passes because the page DATA query's scan
is not yet the dominant cost. This module tops that off at **≥500k rows** — the
scale of a real multi-year 1-minute spot partition — and MEASURES the page DATA
query's OWN cost, which is the load-bearing MAJOR-11 finding:

    ``query_candles`` is **NOT O(page) in partition depth**. Its page query is
    ``SELECT … FROM read_parquet(glob) WHERE <date> QUALIFY row_number() OVER
    (PARTITION BY timestamp …)=1 ORDER BY "timestamp" LIMIT ? OFFSET ?`` — the
    QUALIFY dedup window hash-partitions the WHOLE matched row set and the
    ``ORDER BY`` sorts it, and only THEN does ``LIMIT``/``OFFSET`` slice the
    page. So the DuckDB engine cost is **O(total matched partition rows)**;
    ``LIMIT`` bounds ONLY the Python materialisation (the first page is not
    cheaper than the whole scan) and a deep ``OFFSET`` is
    O(row-groups-up-to-offset) on top.

These pins DOCUMENT that real curve so a future regression — or a false
O(page)/O(1) claim slipped into the docstring — is caught. They deliberately
assert the HONEST scaling, NOT a false flatness: (a) the page latency GROWS with
the stored row count (a genuinely O(page) reader would be flat here), (b) a deep
``OFFSET`` is not cheaper than the first page (both pay the full scan), and
(c) the whole scan stays bounded by a generous ABSOLUTE ceiling (the
``test_lake_invariants_scan_cost.py`` measured-slack discipline — a super-linear
regression fails loudly; CI boxes are slower, never looser in shape).

Measured 2026-07-08 (sandbox, shared-CI class), warm DuckDB + OS page cache:

* first page (offset 0): ~20 ms @ 20k rows vs ~140 ms @ 520k rows → **6.99×**
  for 26× the rows (dominated by the columnar scan, NOT flat — the anti-O(page)
  teeth);
* deep last page @ 520k rows: ~196 ms → **1.40×** the first page (the
  ``OFFSET`` walk term, same order as the scan — NOT an O(1)-vs-O(offset)
  split);
* both ≪ the 8 s absolute per-query ceiling.

Every threshold below carries generous measured slack so shared CI stays
non-flaky; NONE is a literal O(1)/O(page) claim (Golden Rule #10).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

# The data layer (polars writer + duckdb reader) lives behind the ``[data]``
# extra; the lean ``bench`` job may lack it. Skip the whole module then, mirroring
# ``test_db_candle_query_o1.py``. Data-layer symbols are imported only after the guard.
pytest.importorskip("polars")
pytest.importorskip("duckdb")

import duckdb
from brutex.dashboard.db_view import query_candles
from brutex.data import CandleBar
from brutex.data.parquet_writer import ParquetWriter

#: A small reference partition and a ≥500k-row production-scale partition. The
#: 26× row gap makes the columnar scan the dominant term at the big end, so the
#: latency ratio is genuinely non-flat (a regression to a true O(page) reader
#: would collapse it toward 1×).
_SMALL_ROWS = 20_000
_BIG_ROWS = 520_000
#: A full NSE session is 09:15-15:30 IST = 375 one-minute bars.
_BARS_PER_DAY = 375
#: Page the viewer requests (the default first page).
_LIMIT = 100
#: Warmups + timed iterations (kept modest — each big-partition query scans
#: ~500k columnar rows, so the wall-clock is intentionally not tiny).
_WARMUP = 3
_ITERS = 12
#: Generous ABSOLUTE per-query ceiling — ~40× the measured 196 ms deep page, so
#: a slower CI box passes while a super-linear (e.g. O(rows^2)) regression fails.
_ABS_CEILING_S = 8.0


def _bar(ts: datetime) -> CandleBar:
    # Value irrelevant to the timing — keep it inside [low=90, high=110] so
    # CandleBar's OHLC ordering invariant holds.
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("100.00"),
        volume=10,
        open_interest=None,
    )


def _next_weekday(d: datetime) -> datetime:
    while d.weekday() >= 5:  # skip Sat/Sun → real trading-day gaps
        d += timedelta(days=1)
    return d


def _build_partition(root: Path, rows: int) -> int:
    """Write *rows* session 1-minute bars (09:15 IST = 03:45 UTC) as one
    ``(exchange, segment, symbol, interval)`` partition, grouped month-by-month
    exactly as the production ingest does. Returns the exact row count written."""
    writer = ParquetWriter(lake_root=root)
    by_month: dict[tuple[int, int], list[CandleBar]] = {}
    cursor = _next_weekday(datetime(2020, 1, 1, 3, 45, tzinfo=UTC))
    written = 0
    while written < rows:
        cursor = _next_weekday(cursor)
        for bi in range(_BARS_PER_DAY):
            by_month.setdefault((cursor.year, cursor.month), []).append(
                _bar(cursor + timedelta(minutes=bi))
            )
            written += 1
            if written >= rows:
                break
        cursor += timedelta(days=1)
    for bars in by_month.values():
        writer.write(
            exchange="NSE", segment="CASH", symbol="NSE-NIFTY", interval="1minute", bars=bars
        )
    return written


def _query(conn: object, root: Path, offset: int) -> dict[str, object]:
    return query_candles(
        conn,
        root,
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        interval="1minute",
        limit=_LIMIT,
        offset=offset,
    )


def _time_page(root: Path, offset: int) -> float:
    """Mean wall-clock (ns) of one ``query_candles`` page at *offset*, warmed."""
    conn = duckdb.connect()
    try:
        for _ in range(_WARMUP):  # warm DuckDB + the OS page cache
            _query(conn, root, offset)
        t0 = time.perf_counter_ns()
        for _ in range(_ITERS):
            _query(conn, root, offset)
        return (time.perf_counter_ns() - t0) / _ITERS
    finally:
        conn.close()


class TestProductionScalePageLatencyIsHonestlyOTotalRows:
    """``query_candles`` page latency GROWS with stored rows — NOT O(page)."""

    def test_first_page_scales_with_row_count_not_flat(self, tmp_path: Path) -> None:
        """MAJOR-11 anti-O(page) teeth: the FIRST page (offset 0) at a ≥500k-row
        partition is meaningfully slower than at a 20k-row partition, because the
        page query pays a full O(total matched rows) columnar scan BEFORE the
        LIMIT slices the page. A genuinely O(page)/O(1)-in-depth reader would be
        flat here (ratio ≈ 1×). Measured 2026-07-08 (sandbox): 20.0 ms @ 20k vs
        140.2 ms @ 520k → 6.99× for 26× the rows. The floor (>1.5×) and the
        ABSOLUTE ceiling (<8 s) both carry generous slack so shared CI stays
        non-flaky while a false-flat regression fails."""
        small_rows = _build_partition(tmp_path / "small", _SMALL_ROWS)
        big_rows = _build_partition(tmp_path / "big", _BIG_ROWS)
        assert big_rows >= 500_000

        # The total is the honest deduped row count — proves we really stored
        # a production-scale partition (not a footer illusion).
        conn = duckdb.connect()
        big_page = _query(conn, tmp_path / "big", 0)
        conn.close()
        assert big_page["total"] == big_rows
        assert len(big_page["rows"]) == _LIMIT  # one real page materialised

        t_small = _time_page(tmp_path / "small", 0)
        t_big = _time_page(tmp_path / "big", 0)
        ratio = t_big / max(t_small, 1.0)

        assert t_big / 1e9 < _ABS_CEILING_S, (
            f"first-page latency {t_big / 1e6:.1f} ms @ {big_rows} rows exceeds the "
            f"{_ABS_CEILING_S:.0f} s absolute scan ceiling (measured ~140 ms on "
            f"2026-07-08) — the page scan may have regressed to super-linear work."
        )
        assert ratio > 1.5, (
            f"first-page latency ratio {ratio:.2f}× ({big_rows} vs {small_rows} rows) "
            f"is unexpectedly FLAT — the page DATA query is O(total matched rows) "
            f"(QUALIFY window + ORDER BY before LIMIT), so it MUST scale with stored "
            f"rows (measured 6.99× on 2026-07-08). A near-1× ratio means either the "
            f"honest O(total-rows) scaling was silently made O(page) (great — then "
            f"update the docstring + this pin) or the fixture failed to scale. "
            f"t_small={t_small / 1e6:.1f}ms, t_big={t_big / 1e6:.1f}ms."
        )
        assert ratio < 40.0, (
            f"first-page latency ratio {ratio:.2f}× ({big_rows} vs {small_rows} rows, "
            f"a 26× row gap) is far above linear — a super-linear page-scan regression. "
            f"t_small={t_small / 1e6:.1f}ms, t_big={t_big / 1e6:.1f}ms."
        )

    def test_deep_offset_not_cheaper_than_first_page(self, tmp_path: Path) -> None:
        """The honest LIMIT/OFFSET envelope: a DEEP ``OFFSET`` at a ≥500k-row
        partition is NOT cheaper than the FIRST page — both pay the full
        O(total rows) scan+sort, and the deep offset adds an
        O(row-groups-up-to-offset) walk on top. If paging were truly O(page),
        offset 0 would be ~O(1)-cheap and a deep offset dramatically more
        expensive; instead the first page already pays the whole scan. Measured
        2026-07-08 (sandbox): first page 140.2 ms vs deep last page 195.7 ms →
        1.40×. The band [0.5×, 8×] documents "same order, both scan-bound" with
        generous slack."""
        big_rows = _build_partition(tmp_path / "big", _BIG_ROWS)
        assert big_rows >= 500_000
        deep_offset = big_rows - _LIMIT  # the last ascending page

        conn = duckdb.connect()
        deep_page = _query(conn, tmp_path / "big", deep_offset)
        conn.close()
        assert deep_page["total"] == big_rows
        assert len(deep_page["rows"]) == _LIMIT  # a full last page

        t_first = _time_page(tmp_path / "big", 0)
        t_deep = _time_page(tmp_path / "big", deep_offset)
        premium = t_deep / max(t_first, 1.0)

        assert t_deep / 1e9 < _ABS_CEILING_S, (
            f"deep-page latency {t_deep / 1e6:.1f} ms @ offset {deep_offset} exceeds the "
            f"{_ABS_CEILING_S:.0f} s absolute scan ceiling (measured ~196 ms on 2026-07-08)."
        )
        assert 0.5 < premium < 8.0, (
            f"deep-vs-first page premium {premium:.2f}× is outside the honest "
            f"same-order band [0.5×, 8×] — the first page should already pay the full "
            f"scan (so a deep offset is only modestly dearer: measured 1.40× on "
            f"2026-07-08), never an O(1)-vs-O(offset) split. "
            f"t_first={t_first / 1e6:.1f}ms, t_deep={t_deep / 1e6:.1f}ms."
        )
