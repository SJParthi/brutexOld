"""Tests for the `/db` candle-pager TOTAL cache (DEC-DB-CANDLE-COUNT-CACHE-001).

The page ``total`` is a ``COUNT(*)`` over ``read_parquet(glob)`` — DuckDB answers it
from each Parquet file's FOOTER, so it is **O(files)** and was recomputed on every
paging click. ``query_candles`` now accepts an optional generation-stamped
``count_cache`` (the COMMON :class:`~brutex.dashboard.movers_cache.MoversSnapshotCache`
reused as a generic LRU): the total is invariant across paging / sort / column
subsetting of one ``(coords, start, end)``, so a prev/next click within a dataset is
an **O(1) hit**, and a pull bumps the generation → recompute (never a stale total).

These tests pin: the key helper (paging-invariant, bound-aware, coord-distinct), the
cached total is **byte-identical** to the uncached path, the COUNT query runs **once**
across pages of the same dataset+generation (proven with a COUNT-counting connection),
generation invalidation, distinct datasets cached independently, and the empty
partition early-return never touches the cache. Offline + hermetic (real Parquet via
the production writer + a real DuckDB connection; no FastAPI, no network).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb", reason="the candle-count cache reads a real Parquet lake")

import duckdb
from brutex.dashboard.db_view import CandleCountCache, candle_count_key, query_candles
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter


def _bar(ts: datetime) -> CandleBar:
    """One spot bar (2-dp money in [low, high], OI null for CASH)."""
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.00"),
        volume=10,
        open_interest=None,
    )


def _write(lake: Path, *, n_days: int = 2, symbol: str = "NSE-NIFTY") -> None:
    """Write *n_days* single-bar daily partitions (2024-03-01, 02, …) for *symbol*."""
    bars = [_bar(datetime(2024, 3, d, 9, 15, tzinfo=UTC)) for d in range(1, n_days + 1)]
    ParquetWriter(lake_root=lake).write(
        exchange="NSE", segment="CASH", symbol=symbol, interval="1minute", bars=bars
    )


def _candles(
    conn: Any,
    lake: Path,
    *,
    symbol: str = "NSE-NIFTY",
    limit: int = 1,
    offset: int = 0,
    descending: bool = False,
    count_cache: CandleCountCache | None = None,
    generation: int = 0,
) -> dict[str, Any]:
    """Call ``query_candles`` over the fixed NSE/CASH/1minute partition with typed kwargs.

    A typed wrapper (NOT a ``dict`` spread) so the int/bool/cache params keep their
    static types — pyright cannot prove a ``dict[str, str]`` spread won't fill them.
    """
    return query_candles(
        conn,
        lake,
        exchange="NSE",
        segment="CASH",
        symbol=symbol,
        interval="1minute",
        limit=limit,
        offset=offset,
        descending=descending,
        count_cache=count_cache,
        generation=generation,
    )


class _CountingConn:
    """A DuckDB connection wrapper that counts ``COUNT(*)`` executions.

    Delegates everything to a real connection; only the candle-pager total query
    (``SELECT COUNT(*) …``) increments :attr:`count_execs`, so a test can prove the
    total is computed ONCE across paging clicks of the same dataset + generation.
    """

    def __init__(self, real: Any) -> None:
        self._real = real
        self.count_execs = 0

    def execute(self, sql: str, parameters: Any = None) -> Any:
        if "COUNT(*)" in sql:
            self.count_execs += 1
        return (
            self._real.execute(sql) if parameters is None else self._real.execute(sql, parameters)
        )


@pytest.fixture
def conn() -> Any:
    """A real in-process DuckDB connection (read_parquet over the tmp lake)."""
    return duckdb.connect()


# ── candle_count_key ────────────────────────────────────────────────────────────


def test_candle_count_key_format_and_none_bounds() -> None:
    """The key joins coords + bounds + mtime with ``|``; ``None`` bounds render empty.

    The trailing ``mtime_ns`` segment (default ``0``) is the cross-process freshness
    token (DEC-CROSS-PROCESS-CACHE-FRESH-001), mirroring ``window-counts|…|{mtime_ns}``.
    """
    assert (
        candle_count_key("NSE", "CASH", "NSE-NIFTY", "1minute", None, None)
        == "NSE|CASH|NSE-NIFTY|1minute|||0"
    )
    assert (
        candle_count_key("BSE", "FNO", "BSE-SENSEX", "5minute", "2024-01-01", "2024-06-30")
        == "BSE|FNO|BSE-SENSEX|5minute|2024-01-01|2024-06-30|0"
    )


def test_candle_count_key_mtime_segment_distinguishes_partition_recency() -> None:
    """A different partition mtime → a different key (the cross-process freshness fold).

    DEC-CROSS-PROCESS-CACHE-FRESH-001: a cross-process append rewrites a month file
    in place, moving the partition mtime; folding that mtime into the key forces a
    recount instead of serving a stale total (the bug-fix regression at the key level).
    """
    a = candle_count_key("NSE", "CASH", "NSE-NIFTY", "1minute", None, None, 111)
    b = candle_count_key("NSE", "CASH", "NSE-NIFTY", "1minute", None, None, 222)
    assert a != b
    assert a == "NSE|CASH|NSE-NIFTY|1minute|||111"


def test_candle_count_key_is_paging_invariant() -> None:
    """The key depends only on coords+bounds — NOT limit/offset/sort/columns.

    (Those are not parameters of the helper, so two pages of the same dataset
    necessarily build the identical key — the property the cache relies on.)
    """
    k1 = candle_count_key("NSE", "CASH", "NSE-NIFTY", "1minute", "2024-03-01", "2024-03-02")
    k2 = candle_count_key("NSE", "CASH", "NSE-NIFTY", "1minute", "2024-03-01", "2024-03-02")
    assert k1 == k2


def test_candle_count_key_distinct_per_coords_and_bounds() -> None:
    """Different coordinates OR different date bounds yield different keys."""
    base = candle_count_key("NSE", "CASH", "NSE-NIFTY", "1minute", None, None)
    assert base != candle_count_key("NSE", "CASH", "NSE-BANKNIFTY", "1minute", None, None)
    assert base != candle_count_key("NSE", "CASH", "NSE-NIFTY", "5minute", None, None)
    assert base != candle_count_key("NSE", "CASH", "NSE-NIFTY", "1minute", "2024-03-01", None)


# ── byte-identical total ────────────────────────────────────────────────────────


def test_cached_total_is_byte_identical_to_uncached(conn: Any, tmp_path: Path) -> None:
    """The cached total equals the inline (no-cache) total exactly."""
    _write(tmp_path, n_days=3)
    uncached = _candles(conn, tmp_path, limit=1)
    cached = _candles(conn, tmp_path, limit=1, count_cache=MoversSnapshotCache(), generation=0)
    assert uncached["total"] == cached["total"] == 3


# ── O(1) hit: the COUNT runs once across pages ──────────────────────────────────


def test_count_runs_once_across_pages_same_generation(conn: Any, tmp_path: Path) -> None:
    """Paging the same dataset at one generation computes the COUNT exactly once."""
    _write(tmp_path, n_days=4)
    counting = _CountingConn(conn)
    cache = MoversSnapshotCache()
    # Page 1 (offset 0) — a miss → one COUNT.
    p1 = _candles(counting, tmp_path, limit=2, offset=0, count_cache=cache, generation=7)
    # Page 2 (offset 2), and a sort flip — all HITs → no new COUNT.
    p2 = _candles(counting, tmp_path, limit=2, offset=2, count_cache=cache, generation=7)
    p3 = _candles(
        counting, tmp_path, limit=2, offset=0, descending=True, count_cache=cache, generation=7
    )
    assert p1["total"] == p2["total"] == p3["total"] == 4
    assert counting.count_execs == 1, "the O(files) COUNT must run once, then serve cached"


def test_generation_bump_recomputes_the_total(conn: Any, tmp_path: Path) -> None:
    """A new generation (a pull landed) invalidates the cached total → recount."""
    _write(tmp_path, n_days=2)
    counting = _CountingConn(conn)
    cache = MoversSnapshotCache()
    first = _candles(counting, tmp_path, limit=1, count_cache=cache, generation=1)
    assert first["total"] == 2
    assert counting.count_execs == 1
    # A pull writes a third day AND bumps the generation → the cache is stale → recount.
    _write(tmp_path, n_days=3)
    second = _candles(counting, tmp_path, limit=1, count_cache=cache, generation=2)
    assert second["total"] == 3, "a fresh generation must reflect the new bars"
    assert counting.count_execs == 2, "the bumped generation must force a recount"


def test_cross_process_append_without_generation_bump_recounts(conn: Any, tmp_path: Path) -> None:
    """A cross-process append (mtime moves, generation does NOT) → the total recounts.

    DEC-CROSS-PROCESS-CACHE-FRESH-001 (GAP #12 regression): a CLI ``brutex fetch``
    running alongside the dashboard appends bars to a partition WITHOUT bumping the
    dashboard's in-process generation. The candle-count key now folds the partition's
    newest Parquet ``st_mtime_ns``, so the SAME generation but a newer partition keys
    differently → recount, never the stale 2-bar total a generation-only cache served.
    """
    _write(tmp_path, n_days=2)
    counting = _CountingConn(conn)
    cache = MoversSnapshotCache()
    first = _candles(counting, tmp_path, limit=1, count_cache=cache, generation=5)
    assert first["total"] == 2
    assert counting.count_execs == 1
    # Simulate the cross-process writer: append a third day, atomically rewriting the
    # month file (bumps its mtime) — but DO NOT bump the generation (it's another
    # process; the dashboard never saw it). A generation-only cache would stay stale.
    _write(tmp_path, n_days=3)
    second = _candles(counting, tmp_path, limit=1, count_cache=cache, generation=5)
    assert second["total"] == 3, "the moved partition mtime must force a fresh recount"
    assert counting.count_execs == 2, "the cross-process append must invalidate the cached total"


def test_distinct_datasets_cached_independently(conn: Any, tmp_path: Path) -> None:
    """Two datasets keep separate cached totals (the key includes the symbol)."""
    _write(tmp_path, n_days=2, symbol="NSE-NIFTY")
    _write(tmp_path, n_days=3, symbol="NSE-BANKNIFTY")
    cache = MoversSnapshotCache()
    nifty = _candles(conn, tmp_path, symbol="NSE-NIFTY", limit=1, count_cache=cache, generation=0)
    bank = _candles(
        conn, tmp_path, symbol="NSE-BANKNIFTY", limit=1, count_cache=cache, generation=0
    )
    assert nifty["total"] == 2
    assert bank["total"] == 3
    assert cache.size() == 2  # one entry per dataset.


def test_empty_partition_never_touches_the_cache(conn: Any, tmp_path: Path) -> None:
    """An empty/not-yet-pulled partition early-returns total 0 without caching."""
    cache = MoversSnapshotCache()
    out = _candles(conn, tmp_path, limit=1, count_cache=cache, generation=0)
    assert out["total"] == 0
    assert cache.size() == 0, "the empty-partition early-return must not populate the cache"


def test_count_cache_is_optional_default_is_pure(conn: Any, tmp_path: Path) -> None:
    """With no cache injected (the default) the COUNT runs inline every call."""
    _write(tmp_path, n_days=2)
    counting = _CountingConn(conn)
    _candles(counting, tmp_path, limit=1)
    _candles(counting, tmp_path, limit=1)
    assert counting.count_execs == 2, (
        "no cache → the COUNT runs on every call (unchanged behaviour)"
    )
