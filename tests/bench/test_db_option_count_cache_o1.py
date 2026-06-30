"""O(1) benchmark for the `/db` expired-option pager TOTAL cache (DEC-LAKE-READ-O1-AUDIT-001).

The options-lake twin of ``test_db_candle_count_cache_o1.py`` (DEC-DB-CANDLE-COUNT-CACHE-001).

**Honest, narrow claim (Golden Rule #10):** this bench proves ONLY that retrieving a
*cached* option-page total is **O(1) in the series depth** — a paging click within one
series reuses the memoised total via a single dict handoff, NEVER re-reading the
``O(files)`` Parquet footers the uncached ``COUNT(*)`` must. It does **NOT** claim paging
is O(1) overall: the page DATA query (``ORDER BY timestamp … LIMIT`` over the
``read_parquet`` glob) is itself ``O(files)`` and is unaffected by this cache — that
residual is stated, not hidden, mirroring the spot side.

Strategy mirrors the spot bench: prime the cache once (paying the real COUNT), then time
many clean HITS at a SHALLOW vs a DEEP series and assert the hit-latency ratio stays under
``ratio_ceiling(5.0)``.
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

pytest.importorskip("duckdb", reason="the option-count bench reads a real Parquet lake")

import duckdb
from brutex.dashboard.db_view import option_candle_count_key
from brutex.dashboard.movers_cache import MoversSnapshotCache
from brutex.data.models import OptionBar
from brutex.data.option_parquet_writer import OptionParquetWriter, OptionSeriesCoordinate

from tests.bench._o1_tiers import ratio_ceiling

_OPT_PK = "NSE_FNO|13|WEEK|0|ATM|CALL"
_OPT_SERIES = "NSE_FNO__13__WEEK__0__ATM__CALL"


def _coord() -> OptionSeriesCoordinate:
    seg, sec, flag, code, offset, _typ = _OPT_PK.split("|")
    return OptionSeriesCoordinate(
        exch_id="NSE",
        exchange_segment=seg,
        security_id=int(sec),
        underlying_symbol="NIFTY",
        expiry_flag=flag,
        expiry_code=int(code),
        strike_offset=offset,
    )


def _obar(ts: datetime) -> OptionBar:
    return OptionBar(
        timestamp=ts,
        open=Decimal("100.00"),
        high=Decimal("110.00"),
        low=Decimal("90.00"),
        close=Decimal("105.00"),
        volume=250,
        open_interest=1200,
        side="CE",
        strike=Decimal("17500.00"),
        spot=Decimal("17480.00"),
        iv=Decimal("0.1523"),
    )


def _must_not_run() -> int:
    """A ``compute`` that fails if called — a primed-cache hit must never re-COUNT."""
    raise AssertionError("a primed-cache hit must not re-run the O(files) COUNT")


@contextmanager
def _primed(
    tmp_path: Path, n_months: int, tag: str
) -> Generator[tuple[MoversSnapshotCache, str], None, None]:
    """A lake of *n_months* option Parquet files + a cache PRIMED with that series' total.

    Each ``OptionParquetWriter.write`` lands in its own month partition file, so
    *n_months* writes back the series glob with *n_months* footers to scan.
    """
    lake = tmp_path / tag
    writer = OptionParquetWriter(lake_root=lake)
    coord = _coord()
    base = datetime(2024, 1, 1, 4, 0, tzinfo=UTC)
    for m in range(n_months):
        start = base + timedelta(days=31 * m)
        writer.write(
            partition_key=_OPT_PK,
            coordinate=coord,
            bars=[_obar(start + timedelta(minutes=i)) for i in range(375)],
        )
    glob = (lake / "options" / _OPT_SERIES).as_posix() + "/*.parquet"
    key = option_candle_count_key(_OPT_SERIES, None, None)
    with duckdb.connect() as conn:

        def _real_count() -> int:
            row = conn.execute(
                "SELECT COUNT(*) FROM read_parquet(?, union_by_name=true)", [glob]
            ).fetchone()
            return int(row[0]) if row else 0

        cache = MoversSnapshotCache()
        primed_total = cache.get_or_compute(key, generation=0, compute=_real_count)
        assert primed_total >= 1  # the series really has bars (sanity).
        yield cache, key


def _hit(cache: MoversSnapshotCache, key: str) -> int:
    """One clean cached-total retrieval (the COUNT must NOT re-run)."""
    return cache.get_or_compute(key, generation=0, compute=_must_not_run)


class TestCachedOptionTotalIsO1:
    """A cache-served option-page TOTAL is O(1) in the series depth (no footer re-read)."""

    def test_cached_total_at_6_months(self, benchmark: Any, tmp_path: Path) -> None:
        with _primed(tmp_path, 6, "shallow") as (cache, key):
            assert benchmark(lambda: _hit(cache, key)) >= 1

    def test_cached_total_at_72_months(self, benchmark: Any, tmp_path: Path) -> None:
        with _primed(tmp_path, 72, "deep") as (cache, key):
            assert benchmark(lambda: _hit(cache, key)) >= 1

    def test_cached_total_ratio_below_ceiling(self, tmp_path: Path) -> None:
        """The cached-total hit latency at 72 vs 6 months must be flat (< the ceiling).

        The cached retrieval is a dict handoff independent of the file count, so the
        ratio is ~1.0; a regression that re-read the O(files) footers on every click
        would scale with depth and blow past ``ratio_ceiling(5.0)``.
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
