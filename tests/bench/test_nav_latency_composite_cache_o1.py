"""O(1) benchmark for the nav-latency composite cache (DEC-O1-BENCH-GAP-CLOSURE-001).

Gates the load-bearing claim of DEC-DASHBOARD-NAV-LATENCY-001: the three
previously-uncached dashboard pollers — ``/api/v1/coverage``,
``/api/v1/universe/completeness`` and ``/api/v1/universe/mapping-integrity`` — are
memoised behind a COMPOSITE ``(write-generation, master-revision)`` token, so a
repeat poll is an **O(1) handoff** instead of a full O(datasets)+ledger projection.

Two components of that repeat poll are gated (the module's correctness tests cover
neither at scale):

1. **The freshness PROBE — ``universe_cache.master_revision``.** Every repeat poll
   re-probes the instrument master's ``(count(*), max(fetched_at))`` token to decide
   hit-vs-miss. If that probe were O(rows) the "repeat poll is O(1)" claim would be
   false. Measured against a REAL in-process DuckDB ``instruments`` table at a small
   vs a large row count and asserted flat under ``ratio_ceiling(5.0)``.
2. **The cached HANDOFF — ``MoversSnapshotCache.get_or_compute``.** The three routes
   memoise their computed report in this cache keyed by the composite generation, so
   a same-generation repeat is a dict handoff. Measured flat vs the report's dataset
   count.

Together they pin the full repeat-poll path flat vs dataset/row count.

HONEST envelope (Golden Rule #10): ``master_revision`` is a ``COUNT(*)`` + ``MAX``
aggregate — DuckDB serves ``COUNT`` from metadata and ``MAX`` from column zonemaps,
so it is bounded (near-constant) but not a literal O(1); at 100k synthetic rows it
measures ~1.2×. The PRODUCTION master is the LEAN table (~hundreds of rows), where
this cost is trivially flat — the bench probes far beyond production scale to prove
the shape stays bounded. Best-of-N-MIN timing (DEC-CANARY-MINTIME-001).
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from brutex.dashboard.movers_cache import MoversSnapshotCache, candidate_key
from brutex.dashboard.universe_cache import master_revision

from tests.bench._o1_tiers import ratio_ceiling

duckdb = pytest.importorskip("duckdb")


class _FakeStore:
    """The minimal surface ``master_revision`` needs — a DuckDB ``connection``."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection


def _store_with_rows(n_rows: int) -> _FakeStore:
    """A real in-memory DuckDB ``instruments`` table populated with *n_rows* rows."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE instruments(security_id BIGINT, fetched_at TIMESTAMP)")
    conn.execute(
        "INSERT INTO instruments SELECT range, TIMESTAMP '2026-07-01 00:00:00' FROM range(?)",
        [n_rows],
    )
    return _FakeStore(conn)


class TestMasterRevisionProbeIsBounded:
    """The per-poll freshness probe is flat (bounded) vs the master row count."""

    def test_master_revision_correct(self) -> None:
        store = _store_with_rows(123)
        assert master_revision(store) == (123, "2026-07-01 00:00:00")

    def test_master_revision_flat_vs_row_count(self) -> None:
        small = _store_with_rows(100)
        big = _store_with_rows(100_000)

        def _per_call(store: _FakeStore, iterations: int) -> float:
            best = float("inf")
            for _ in range(5):  # best-of-N-MIN (DEC-CANARY-MINTIME-001)
                t0 = time.perf_counter_ns()
                for _ in range(iterations):
                    master_revision(store)
                best = min(best, (time.perf_counter_ns() - t0) / iterations)
            return best

        _per_call(small, 2_000)  # warm
        t_small = _per_call(small, 10_000)
        t_big = _per_call(big, 10_000)
        ratio = t_big / max(t_small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"master_revision probe ratio {ratio:.2f}× (100k vs 100 rows) exceeds the 5× CI "
            f"ceiling — the per-poll freshness token may have regressed to an O(rows) scan. "
            f"small={t_small:.0f}ns big={t_big:.0f}ns."
        )


def _report(n_datasets: int) -> list[dict[str, Any]]:
    """A composite-report payload the size of the dataset count (the memoised value)."""
    return [{"symbol": f"SYM{i}", "verdict": "full", "bars": i} for i in range(n_datasets)]


def _primed_cache(n_datasets: int) -> tuple[MoversSnapshotCache, str]:
    """A cache primed with an *n_datasets* composite report at generation 0."""
    cache = MoversSnapshotCache()
    key = candidate_key(
        asset="spot", exchange=None, interval="coverage", date="2026-07-01", time=None
    )
    payload = _report(n_datasets)
    cache.get_or_compute(key, generation=0, compute=lambda: payload)  # one projection → cached
    return cache, key


def _must_not_recompute() -> list[dict[str, Any]]:
    """A compute that a same-generation repeat poll must never call."""
    raise AssertionError("same-generation repeat poll must not re-project the report")


class TestCompositeHandoffIsO1:
    """A same-generation repeat poll returns the memoised report via an O(1) handoff."""

    def test_handoff_flat_vs_dataset_count(self) -> None:
        cache_small, key_small = _primed_cache(10)
        cache_big, key_big = _primed_cache(1_000)

        def _per_call(cache: MoversSnapshotCache, key: str, iterations: int) -> float:
            best = float("inf")
            for _ in range(5):  # best-of-N-MIN
                t0 = time.perf_counter_ns()
                for _ in range(iterations):
                    cache.get_or_compute(key, generation=0, compute=_must_not_recompute)
                best = min(best, (time.perf_counter_ns() - t0) / iterations)
            return best

        _per_call(cache_small, key_small, 5_000)  # warm
        small = _per_call(cache_small, key_small, 30_000)
        big = _per_call(cache_big, key_big, 30_000)
        ratio = big / max(small, 1.0)
        assert ratio < ratio_ceiling(5.0), (
            f"composite-cache handoff ratio {ratio:.2f}× (1000 vs 10 datasets) exceeds the "
            f"5× CI ceiling — a repeat nav poll may have regressed to a re-projection. "
            f"small={small:.0f}ns big={big:.0f}ns."
        )
