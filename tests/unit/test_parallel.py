"""Unit tests for brutex.orchestrator.parallel.PoolRunParallel.

Verifies the real thread-pool concurrency primitive upholds the
``RunParallel`` contract the two-phase pull relies on: one ItemResult per
item, success xor captured-exception, empty-input safety, and that a raising
fetcher never propagates (it is captured so the phase can't abort).
"""

from __future__ import annotations

from brutex.orchestrator.parallel import DEFAULT_MAX_WORKERS, PoolRunParallel
from brutex.orchestrator.universe_phases import ItemResult


class TestPoolRunParallel:
    def test_maps_every_item(self) -> None:
        pool = PoolRunParallel(max_workers=4)
        results = list(pool(lambda x: x * 2, [1, 2, 3, 4, 5]))
        assert len(results) == 5
        # Completion order is non-deterministic; compare as a set by item.
        by_item = {r.item: r for r in results}
        assert all(by_item[i].ok for i in range(1, 6))
        assert {r.item: r.result for r in results} == {1: 2, 2: 4, 3: 6, 4: 8, 5: 10}

    def test_empty_items_yields_nothing(self) -> None:
        pool = PoolRunParallel()
        assert list(pool(lambda x: x, [])) == []

    def test_exception_is_captured_not_raised(self) -> None:
        pool = PoolRunParallel(max_workers=2)

        def boom(x: int) -> int:
            if x == 2:
                raise ValueError(f"bad {x}")
            return x

        results = {r.item: r for r in pool(boom, [1, 2, 3])}
        assert results[1].ok and results[1].result == 1
        assert results[3].ok and results[3].result == 3
        assert not results[2].ok
        assert isinstance(results[2].exc, ValueError)
        assert results[2].result is None

    def test_all_results_are_itemresult(self) -> None:
        pool = PoolRunParallel(max_workers=3)
        for r in pool(lambda x: x, [1, 2, 3]):
            assert isinstance(r, ItemResult)

    def test_max_workers_floored_to_one(self) -> None:
        # A zero/negative worker count must not crash — floored to 1.
        pool = PoolRunParallel(max_workers=0)
        results = list(pool(lambda x: x + 1, [10]))
        assert results == [ItemResult(item=10, ok=True, result=11, exc=None)]

    def test_default_workers_constant(self) -> None:
        assert DEFAULT_MAX_WORKERS == 8

    def test_single_item(self) -> None:
        pool = PoolRunParallel()
        results = list(pool(lambda x: x.upper(), ["nifty"]))
        assert results == [ItemResult(item="nifty", ok=True, result="NIFTY", exc=None)]
