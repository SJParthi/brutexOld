"""O(page)-as-the-registry-grows benchmark for the /db registry pager.

DEC-DB-VIEWER-PAGINATE-ALL-001. The registry expiries/contracts ``/db`` tabs used to
load EVERY persisted leg (a chain can hold tens of thousands); they now read ONE
``LIMIT``/``OFFSET`` page with a cheap FILTERED ``COUNT(*)``. The load-bearing
property benched here is **page latency independent of how many legs the registry
holds** — querying the first page against a registry with 10× more contracts must
cost essentially the same, because the page read is ``LIMIT``-bounded (the index on
``(exch, underlying, expiry_date)`` streams the ordered head). A regression that
re-introduced the load-everything read would scale with the stored leg count and blow
the ratio.

DuckDB I/O is noisier than pure CPU, so the shared-CI ceiling matches the other
I/O-touching gates (``< 8×``); the M4-Pro strict tier tightens it via
:func:`ratio_ceiling`.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tests.bench._o1_tiers import ratio_ceiling

pytest.importorskip("duckdb")

from brutex.data.registry_store import RegistryStore

#: Leg counts the first-page latency must be independent of (10× difference).
_SMALL = 200
_LARGE = 2000
#: Page the viewer requests (first page, default limit).
_LIMIT = 100
#: How many page reads to time at each size (averaged).
_QUERIES = 50

_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)


def _build(path: Path, legs: int) -> RegistryStore:
    """Seed a registry with *legs* NIFTY contract legs across 1 expiry."""
    store = RegistryStore.open(path)
    exp = date(2020, 1, 30)
    store.upsert_expiry("NSE", "NIFTY", exp, run_id="r1", fetched_at=_NOW)
    for i in range(legs):
        store.upsert_contract(
            "NSE",
            "NIFTY",
            exp,
            f"NSE-NIFTY-30Jan20-{i:05d}-CE",
            side="CE",
            strike=10000.0 + i,
            run_id="r1",
            fetched_at=_NOW,
        )
    return store


def _page(store: RegistryStore) -> None:
    store.count_contracts(underlying="NIFTY")
    store.list_contracts(underlying="NIFTY", limit=_LIMIT, offset=0)


class TestRegistryPageLatencyIsFlat:
    """``list_contracts`` first-page latency is independent of the registry's size."""

    def test_page_at_small_registry(self, benchmark, tmp_path: Path) -> None:
        store = _build(tmp_path / "s.duckdb", _SMALL)
        benchmark(lambda: _page(store))
        store.close()

    def test_page_at_large_registry(self, benchmark, tmp_path: Path) -> None:
        store = _build(tmp_path / "l.duckdb", _LARGE)
        benchmark(lambda: _page(store))
        store.close()

    def test_page_latency_ratio_below_8x(self, tmp_path: Path) -> None:
        """First-page latency at 2000 vs 200 legs stays flat (< 8×)."""

        def _time(path: Path, legs: int) -> float:
            store = _build(path, legs)
            for _ in range(5):  # warm
                _page(store)
            t0 = time.perf_counter_ns()
            for _ in range(_QUERIES):
                _page(store)
            elapsed = (time.perf_counter_ns() - t0) / _QUERIES
            store.close()
            return elapsed

        t_small = _time(tmp_path / "rs.duckdb", _SMALL)
        t_large = _time(tmp_path / "rl.duckdb", _LARGE)
        ratio = t_large / max(t_small, 1.0)
        assert ratio < ratio_ceiling(8.0), (
            f"registry first-page latency ratio {ratio:.2f}× ({_LARGE} vs {_SMALL} "
            f"legs) exceeds 8× — the page read may have regressed to a load-everything "
            f"scan. t_small={t_small:.0f}ns, t_large={t_large:.0f}ns."
        )
