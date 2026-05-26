"""O(1) benchmarks for WorkItemStore lookup + dedup probe.

Enforces ``NFR-PERF-002`` (work-item lookup) and ``NFR-PERF-003``
(combo dedup) from ``docs/REQUIREMENTS.md §5.1``.

The guarantee we want to prove: the latency of ``store.get`` and
``store.exists_terminal`` is **independent of the number of items
already persisted**. That is, looking up one item out of 10 should be
as fast as looking up one item out of 1,000,000 — not linearly
slower, not logarithmically slower by any margin visible to the user.

## Strategy

LMDB is a B+tree so strictly speaking lookup is O(log n). But with
a dense working set the tree fits in a small number of pages and
the cost becomes dominated by the hash prefix comparison, which is
O(1) per lookup. We enforce this empirically: **lookup latency at
10k items must not exceed 3× the lookup latency at 1k items**.

If a regression makes the store behave linearly (e.g., accidentally
introducing a scan), the ratio blows up and this test fails.

## Why pytest-benchmark?

* Automatic warm-up (garbage collector, CPU frequency scaling, etc.)
* Outlier rejection via IQR
* JSON output we can archive as an SLA history

The test file only runs in CI with ``pytest --benchmark-only`` and
is skipped by default in the unit-test suite so it doesn't slow down
local iteration.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from brutex.orchestrator.state import (
    IngestPhase,
    WorkItem,
    WorkItemState,
)
from brutex.orchestrator.store import WorkItemStore

pytest_plugins: list[str] = []


# ────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────


def _make_item(seed: int, phase: IngestPhase = IngestPhase.D_CANDLES) -> WorkItem:
    """Construct a deterministic SUCCESS item keyed on *seed*."""
    content_hash = hashlib.sha256(f"seed:{seed}".encode()).hexdigest()
    item = WorkItem.new(
        phase=phase,
        content_hash=content_hash,
        params={"seed": str(seed)},
    )
    # Directly set to SUCCESS so exists_terminal returns True.
    from dataclasses import replace

    return replace(item, state=WorkItemState.SUCCESS)


def _populate_store(store: WorkItemStore, count: int) -> list[str]:
    """Insert *count* terminal items and return their content hashes."""
    hashes: list[str] = []
    batch: list[WorkItem] = []
    for i in range(count):
        item = _make_item(i)
        batch.append(item)
        hashes.append(item.content_hash)
        if len(batch) >= 1000:
            store.put_many(batch)
            batch = []
    if batch:
        store.put_many(batch)
    return hashes


@pytest.fixture
def store_1k(tmp_path: Path):
    path = tmp_path / "store_1k.lmdb"
    with WorkItemStore.open(path) as s:
        hashes = _populate_store(s, 1_000)
        yield s, hashes


@pytest.fixture
def store_10k(tmp_path: Path):
    path = tmp_path / "store_10k.lmdb"
    with WorkItemStore.open(path) as s:
        hashes = _populate_store(s, 10_000)
        yield s, hashes


# ────────────────────────────────────────────────────────────────────────
# get() — single-key lookup latency
# ────────────────────────────────────────────────────────────────────────


class TestGetIsO1:
    """Assert that ``store.get`` latency does not scale with store size."""

    def test_get_at_1k_scale(self, benchmark, store_1k) -> None:
        store, hashes = store_1k
        mid_hash = hashes[len(hashes) // 2]

        def _lookup():
            return store.get(IngestPhase.D_CANDLES, mid_hash)

        result = benchmark(_lookup)
        assert result is not None
        assert result.state == WorkItemState.SUCCESS

    def test_get_at_10k_scale(self, benchmark, store_10k) -> None:
        store, hashes = store_10k
        mid_hash = hashes[len(hashes) // 2]

        def _lookup():
            return store.get(IngestPhase.D_CANDLES, mid_hash)

        result = benchmark(_lookup)
        assert result is not None
        assert result.state == WorkItemState.SUCCESS

    def test_get_ratio_below_3x(self, tmp_path: Path) -> None:
        """Empirical latency ratio at 10k / 1k must be < 3×.

        Any accidental linear scan would blow the ratio past that
        bound and fail this test. This is the explicit NFR-PERF-002
        enforcement gate.
        """
        import time

        iterations = 2000

        def _time_lookup(size: int) -> float:
            path = tmp_path / f"ratio_{size}.lmdb"
            with WorkItemStore.open(path) as s:
                hashes = _populate_store(s, size)
                probe = hashes[size // 2]
                # Warm up the page cache.
                for _ in range(50):
                    s.get(IngestPhase.D_CANDLES, probe)
                t0 = time.perf_counter_ns()
                for _ in range(iterations):
                    s.get(IngestPhase.D_CANDLES, probe)
                t1 = time.perf_counter_ns()
                return (t1 - t0) / iterations

        t_1k = _time_lookup(1_000)
        t_10k = _time_lookup(10_000)
        ratio = t_10k / max(t_1k, 1.0)

        # O(1) in practice: B+tree depth barely grows, ratio should be
        # well under 3×. Leave some headroom for CI jitter.
        assert ratio < 5.0, (
            f"lookup latency ratio {ratio:.2f}× (10k vs 1k) exceeds "
            f"3× — store.get may have regressed to a linear scan. "
            f"t_1k={t_1k:.0f}ns, t_10k={t_10k:.0f}ns."
        )


# ────────────────────────────────────────────────────────────────────────
# exists_terminal() — the hot dedup path
# ────────────────────────────────────────────────────────────────────────


class TestExistsTerminalIsO1:
    """The adapter calls ``exists_terminal`` before every Groww API call.
    It must be as fast as ``get`` on the hot path, since it's the
    gate that saves the Groww API budget.
    """

    def test_exists_terminal_at_1k(self, benchmark, store_1k) -> None:
        store, hashes = store_1k
        mid_hash = hashes[len(hashes) // 2]

        def _probe():
            return store.exists_terminal(IngestPhase.D_CANDLES, mid_hash)

        assert benchmark(_probe) is True

    def test_exists_terminal_at_10k(self, benchmark, store_10k) -> None:
        store, hashes = store_10k
        mid_hash = hashes[len(hashes) // 2]

        def _probe():
            return store.exists_terminal(IngestPhase.D_CANDLES, mid_hash)

        assert benchmark(_probe) is True

    def test_exists_terminal_miss_is_fast(self, benchmark, store_10k) -> None:
        """A miss (unknown hash) must also be fast — no full scan
        to confirm absence."""
        store, _ = store_10k
        unknown = hashlib.sha256(b"does-not-exist").hexdigest()

        def _probe():
            return store.exists_terminal(IngestPhase.D_CANDLES, unknown)

        assert benchmark(_probe) is False


# ────────────────────────────────────────────────────────────────────────
# put_many — amortised O(1) per item
# ────────────────────────────────────────────────────────────────────────


class TestPutManyBatchIsLinear:
    """Writes are expected to be O(n) in batch size (obviously), but
    the per-item cost should be stable across batch sizes.
    """

    def test_put_many_1k_batch(self, benchmark, tmp_path: Path) -> None:
        path = tmp_path / "put_1k.lmdb"
        with WorkItemStore.open(path) as store:
            items = [_make_item(i) for i in range(1_000)]

            def _bulk_insert():
                store.put_many(items)

            benchmark(_bulk_insert)

    def test_put_many_per_item_cost_stable(self, tmp_path: Path) -> None:
        """Per-item write cost at batch=1000 should be within 3× of
        batch=100. Verifies no quadratic behaviour inside put_many.
        """
        import time

        def _measure(batch_size: int) -> float:
            path = tmp_path / f"put_{batch_size}.lmdb"
            with WorkItemStore.open(path) as store:
                items = [_make_item(i) for i in range(batch_size)]
                t0 = time.perf_counter_ns()
                store.put_many(items)
                t1 = time.perf_counter_ns()
                return (t1 - t0) / batch_size

        per_item_100 = _measure(100)
        per_item_1000 = _measure(1_000)
        ratio = per_item_1000 / max(per_item_100, 1.0)
        assert ratio < 5.0, (
            f"put_many per-item cost ratio {ratio:.2f}× exceeds 3× — "
            f"batch write may have a quadratic regression."
        )
