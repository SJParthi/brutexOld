"""O(1) benchmark for the `/universe` backend snapshot cache (DEC-UNIVERSE-BACKEND-CACHE-001).

The guarantee we prove: a **cache-served** ``/universe`` poll
(``UniverseSnapshotCache.summary`` / ``.instruments`` between two instrument
loads — the dominant interaction) has latency **independent of how many
underlyings the master holds**. A clean hit is a cheap content-token probe
(``count(*)`` + ``max(fetched_at)`` over the LEAN table, answered by DuckDB from
its row-group statistics — flat) plus a single O(1) dict handoff under one lock.
It must NOT re-run the ``O(candidates)`` base read + classification, so serving a
universe of 4000 underlyings is as fast as one of 40.

This is the read-side counterpart to the honest envelope in
``brutex/dashboard/universe_cache.py``: the per-interaction HIT is O(1); the first
read after a load (a miss) still pays the real ``O(candidates)`` lake read +
classification and is NOT what this bench measures (the cache is primed first, then
every timed call is a hit). A regression that re-read + re-classified on every poll
(the lag this PR fixes) would make the latency scale with the universe count and
blow past the ceiling.

Strategy mirrors ``tests/bench/test_movers_cache_o1.py``: warm up, then time many
clean-hit calls at a SMALL vs a LARGE universe and assert the latency ratio stays
under ``ratio_ceiling(5.0)`` — 5× on the shared GHA runner, tightened to the strict
1.1× M4-Pro tier via ``tests/bench/_o1_tiers.py``.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb", reason="the universe cache bench reads a real DuckDB store")

from brutex.dashboard.universe_cache import UniverseSnapshotCache
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment

from tests.bench._o1_tiers import ratio_ceiling


def _equity(security_id: int) -> Instrument:
    """One F&O-eligible NSE equity row (a ``stock`` candidate)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=f"S{security_id}",
        display_name=f"Stock {security_id}",
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=None,
        underlying_symbol=f"S{security_id}",
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@contextmanager
def _primed(
    tmp_path: Path, n: int, tag: str
) -> Generator[tuple[InstrumentStore, UniverseSnapshotCache], None, None]:
    """An *n*-underlying store + a cache PRIMED for both payload kinds (clean hits).

    A context manager so the store is closed via the ``with`` protocol at the call
    site, and the store itself is opened with ``with`` here (it is a context
    manager — ``__exit__`` closes it) so there is NO hand-rolled ``finally``
    anywhere (CodeQL ``py/should-use-with``).
    """
    with InstrumentStore.open(tmp_path / f"u_{tag}.duckdb") as store:
        store.upsert_many(
            [_equity(i) for i in range(1, n + 1)],
            snapshot_date=date(2024, 1, 2),
            run_id="r1",
            csv_source_sha256="0" * 64,
        )
        cache = UniverseSnapshotCache()
        cache.summary(store)  # one base read → memoised
        cache.instruments(store, kind=None)
        yield store, cache


class TestCachedHitIsO1:
    """A cache-served ``/universe`` poll is O(1) in the universe count (no re-read)."""

    def test_cached_hit_at_40(self, benchmark: Any, tmp_path: Path) -> None:
        with _primed(tmp_path, 40, "small") as (store, cache):
            result = benchmark(lambda: cache.summary(store))
            assert result["stock_count"] == 40

    def test_cached_hit_at_4000(self, benchmark: Any, tmp_path: Path) -> None:
        with _primed(tmp_path, 4000, "big") as (store, cache):
            result = benchmark(lambda: cache.summary(store))
            assert result["stock_count"] == 4000

    def test_cached_hit_ratio_below_ceiling(self, tmp_path: Path) -> None:
        """Empirical hit-latency ratio at 4000 / 40 underlyings must be < the ceiling.

        The ceiling is ``ratio_ceiling(5.0)`` — 5× on the shared GHA runner,
        tightened to the strict 1.1× M4-Pro tier (DEC-O1-STRICT-TIER-001). A
        regression that re-read + re-classified on every poll would scale with the
        universe count and blow past this bound — the explicit O(1) hot-path gate.
        """
        import time

        def _time(cache: UniverseSnapshotCache, store: InstrumentStore, iters: int) -> float:
            start = time.perf_counter()
            for _ in range(iters):
                cache.summary(store)
                cache.instruments(store, kind=None)
            return time.perf_counter() - start

        with (
            _primed(tmp_path, 40, "ratio_small") as (store_small, cache_small),
            _primed(tmp_path, 4000, "ratio_big") as (store_big, cache_big),
        ):
            _time(cache_small, store_small, 500)  # warm up
            _time(cache_big, store_big, 500)
            iterations = 5_000
            small = _time(cache_small, store_small, iterations)
            big = _time(cache_big, store_big, iterations)
            ratio = big / small if small > 0 else 1.0
            assert ratio < ratio_ceiling(5.0), (
                f"cached-hit latency ratio {ratio:.2f} scaled with size"
            )
