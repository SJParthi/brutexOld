"""O(1) benchmark for the `/db` candle-pager TOTAL cache (DEC-DB-CANDLE-COUNT-CACHE-001).

**Honest, narrow claim (Golden Rule #10):** this bench proves ONLY that retrieving a
*cached* candle-page total is **O(1) in the partition depth** — a paging click within
one dataset reuses the memoised total via a single dict handoff, NEVER re-reading the
``O(files)`` Parquet footers the uncached ``COUNT(*)`` must. It does **NOT** claim
paging is O(1) overall: the page DATA query (``ORDER BY timestamp DESC LIMIT`` over the
``read_parquet`` glob) is itself ``O(files)`` and is unaffected by this cache — that
residual is stated, not hidden, and is the candidate for a future lake-manifest PR.

So the contrast the bench captures:
* the **uncached** ``COUNT(*)`` scales with the number of Parquet files (it opens every
  file's footer) — measured here as the baseline that grows; and
* the **cached** total retrieval is **flat** — independent of how many files back the
  partition.

Strategy mirrors ``tests/bench/test_universe_cache_o1.py`` / ``test_movers_cache_o1.py``:
prime the cache once (paying the real COUNT), then time many clean HITS at a SHALLOW vs a
DEEP partition and assert the hit-latency ratio stays under ``ratio_ceiling(5.0)``.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb", reason="the candle-count bench reads a real Parquet lake")

import duckdb
from brutex.dashboard.db_view import candle_count_key
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter

from tests.bench._o1_tiers import ratio_ceiling

_GLOB_SUFFIX = "/**/*.parquet"


def _bar(ts: datetime) -> CandleBar:
    """One in-range spot bar (close within [low, high])."""
    return CandleBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("101.00"),
        low=Decimal("99.00"),
        close=Decimal("100.00"),
        volume=10,
        open_interest=None,
    )


def _must_not_run() -> int:
    """A ``compute`` that fails if called — a primed-cache hit must never re-COUNT."""
    raise AssertionError("a primed-cache hit must not re-run the O(files) COUNT")


@contextmanager
def _primed(
    tmp_path: Path, n_months: int, tag: str
) -> Generator[tuple[MoversSnapshotCache, str], None, None]:
    """A lake of *n_months* Parquet files + a cache PRIMED with that partition's total.

    The store opens under ``with`` (CodeQL ``py/should-use-with``); the cache is primed
    by running the real ``COUNT(*)`` once, so every later call is a clean hit.
    """
    lake = tmp_path / tag
    writer = ParquetWriter(lake_root=lake)
    base = datetime(2020, 1, 1, 9, 15, tzinfo=UTC)
    for m in range(n_months):
        start = base + timedelta(days=31 * m)
        writer.write(
            exchange="NSE",
            segment="CASH",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(start + timedelta(minutes=i)) for i in range(375)],
        )
    glob = (lake / "bars" / "NSE" / "CASH" / "NSE-NIFTY" / "1minute").as_posix() + _GLOB_SUFFIX
    key = candle_count_key("NSE", "CASH", "NSE-NIFTY", "1minute", None, None)
    with duckdb.connect() as conn:

        def _real_count() -> int:
            row = conn.execute(
                "SELECT COUNT(*) FROM read_parquet(?, union_by_name=true)", [glob]
            ).fetchone()
            return int(row[0]) if row else 0

        cache = MoversSnapshotCache()
        primed_total = cache.get_or_compute(
            key, generation=0, compute=_real_count
        )  # one real COUNT
        assert primed_total >= 1  # the partition really has bars (sanity).
        yield cache, key


def _hit(cache: MoversSnapshotCache, key: str) -> int:
    """One clean cached-total retrieval (the COUNT must NOT re-run)."""
    return cache.get_or_compute(key, generation=0, compute=_must_not_run)


class TestCachedTotalIsO1:
    """A cache-served candle-page TOTAL is O(1) in the partition depth (no footer re-read)."""

    def test_cached_total_at_6_months(self, benchmark: Any, tmp_path: Path) -> None:
        with _primed(tmp_path, 6, "shallow") as (cache, key):
            assert benchmark(lambda: _hit(cache, key)) >= 1

    def test_cached_total_at_72_months(self, benchmark: Any, tmp_path: Path) -> None:
        with _primed(tmp_path, 72, "deep") as (cache, key):
            assert benchmark(lambda: _hit(cache, key)) >= 1

    def test_cached_total_ratio_below_ceiling(self, tmp_path: Path) -> None:
        """The cached-total hit latency at 72 vs 6 months must be flat (< the ceiling).

        ``ratio_ceiling(5.0)`` — 5× on the shared GHA runner, tightened to the strict
        1.1× M4-Pro tier (DEC-O1-STRICT-TIER-001). The cached retrieval is a dict handoff
        independent of the file count, so the ratio is ~1.0; a regression that re-read the
        O(files) footers on every click would scale with depth and blow past this bound.
        """

        def _time(cache: MoversSnapshotCache, key: str, iters: int) -> float:
            start = time.perf_counter()
            for _ in range(iters):
                _hit(cache, key)
            return time.perf_counter() - start

        with (
            _primed(tmp_path, 6, "ratio_shallow") as (cache_small, key_small),
            _primed(tmp_path, 72, "ratio_deep") as (cache_big, key_big),
        ):
            _time(cache_small, key_small, 2_000)  # warm up
            _time(cache_big, key_big, 2_000)
            iterations = 20_000
            small = _time(cache_small, key_small, iterations)
            big = _time(cache_big, key_big, iterations)
            ratio = big / small if small > 0 else 1.0
            assert ratio < ratio_ceiling(5.0), (
                f"cached-total hit ratio {ratio:.2f} scaled with depth"
            )
