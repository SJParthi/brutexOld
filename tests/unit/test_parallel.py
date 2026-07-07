"""Unit tests for brutex.orchestrator.parallel.PoolRunParallel.

Verifies the real thread-pool concurrency primitive upholds the
``RunParallel`` contract the two-phase pull relies on: one ItemResult per
item, success xor captured-exception, empty-input safety, and that a raising
fetcher never propagates (it is captured so the phase can't abort).
"""

from __future__ import annotations

from brutex.orchestrator.parallel import (
    DEFAULT_LATENCY_BUDGET_S,
    DEFAULT_MAX_WORKERS,
    MAX_FANOUT,
    PoolRunParallel,
    SerialRunParallel,
    workers_to_saturate,
)
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


class TestSerialRunParallel:
    """The in-thread serial twin (DEC-SPOT-CROSS-SYMBOL-FANOUT-001): the SAME
    ``RunParallel`` capture contract as the pool, but zero worker threads."""

    def test_maps_every_item_in_order(self) -> None:
        # Serial → deterministic emission order (unlike the pool's completion order).
        runner = SerialRunParallel()
        results = list(runner(lambda x: x * 2, [1, 2, 3, 4, 5]))
        assert [r.item for r in results] == [1, 2, 3, 4, 5]
        assert [r.result for r in results] == [2, 4, 6, 8, 10]
        assert all(r.ok for r in results)

    def test_empty_items_yields_nothing(self) -> None:
        runner = SerialRunParallel()
        assert list(runner(lambda x: x, [])) == []

    def test_exception_is_captured_not_raised(self) -> None:
        runner = SerialRunParallel()

        def boom(x: int) -> int:
            if x == 2:
                raise ValueError(f"bad {x}")
            return x

        results = {r.item: r for r in runner(boom, [1, 2, 3])}
        assert results[1].ok and results[1].result == 1
        assert results[3].ok and results[3].result == 3
        assert not results[2].ok
        assert isinstance(results[2].exc, ValueError)
        assert results[2].result is None

    def test_all_results_are_itemresult(self) -> None:
        runner = SerialRunParallel()
        for r in runner(lambda x: x, [1, 2, 3]):
            assert isinstance(r, ItemResult)

    def test_single_item(self) -> None:
        runner = SerialRunParallel()
        assert list(runner(lambda x: x.upper(), ["nifty"])) == [
            ItemResult(item="nifty", ok=True, result="NIFTY", exc=None)
        ]

    def test_runs_on_the_calling_thread(self) -> None:
        # No worker threads: every item executes on the caller's own thread.
        import threading

        runner = SerialRunParallel()
        seen: list[int] = []
        caller = threading.get_ident()

        def _record(x: int) -> int:
            seen.append(threading.get_ident())
            return x

        list(runner(_record, [1, 2, 3]))
        assert seen == [caller, caller, caller]

    def test_lazy_yield_does_not_run_ahead(self) -> None:
        # Generator semantics: pulling N results runs fn exactly N times.
        runner = SerialRunParallel()
        ran: list[int] = []

        def _track(x: int) -> int:
            ran.append(x)
            return x

        gen = runner(_track, [1, 2, 3])
        next(gen)
        assert ran == [1]  # only the first item has run after one pull
        next(gen)
        assert ran == [1, 2]


class TestWorkersToSaturate:
    """Little's law pool sizing (DEC-UNIVERSE-PULL-8RPS-001): enough in-flight
    workers that the rate bucket, not network latency, governs the achieved r/s."""

    def test_sizes_to_rate_times_latency(self) -> None:
        # 8 r/s x 3 s/call = 24 calls in flight to keep the bucket saturated.
        assert workers_to_saturate(8.0, 3.0) == 24

    def test_ceils_fractional_latency(self) -> None:
        # 8 x 2.9 = 23.2 → 24 (round UP, never under-provision the pool).
        assert workers_to_saturate(8.0, 2.9) == 24

    def test_uses_default_latency_budget(self) -> None:
        assert workers_to_saturate(8.0) == 24
        assert DEFAULT_LATENCY_BUDGET_S == 3.0

    def test_clamped_to_cap(self) -> None:
        # A high rate x latency would over-fan-out; the socket cap bounds it.
        assert workers_to_saturate(100.0, 3.0) == MAX_FANOUT
        assert MAX_FANOUT == 32

    def test_custom_cap(self) -> None:
        assert workers_to_saturate(8.0, 3.0, cap=10) == 10

    def test_non_positive_rate_sizes_to_one(self) -> None:
        assert workers_to_saturate(0.0, 3.0) == 1
        assert workers_to_saturate(-5.0, 3.0) == 1

    def test_non_positive_latency_sizes_to_one(self) -> None:
        # rate > 0 but latency <= 0 → still one worker (covers the OR's 2nd term).
        assert workers_to_saturate(8.0, 0.0) == 1
        assert workers_to_saturate(8.0, -1.0) == 1

    def test_tiny_positive_product_floors_to_one(self) -> None:
        # ceil(0.5 x 0.5)=ceil(0.25)=1 — never zero, always makes progress.
        assert workers_to_saturate(0.5, 0.5) == 1


class TestPoolContextPropagation:
    """DEC-PULL-BEAT-PERRUN-001 fix round: every pool task runs in a COPY of the
    SUBMITTING thread's contextvars context, so the driver's ambient ``pull_id``
    reaches the workers (the metadata phase's pace/reauth seam beats then attribute
    to the run's keyed channel instead of dropping) and no bind can leak across a
    reused pool thread."""

    def test_workers_inherit_the_drivers_pull_id(self) -> None:
        from brutex.observability.correlation import current_pull_id, pull_id_scope

        pool = PoolRunParallel(max_workers=4)
        with pull_id_scope("abc123def456"):
            results = list(pool(lambda _x: current_pull_id(), list(range(8))))
        assert {r.result for r in results} == {"abc123def456"}

    def test_no_ambient_context_propagates_empty(self) -> None:
        from brutex.observability.correlation import current_pull_id

        pool = PoolRunParallel(max_workers=2)
        results = list(pool(lambda _x: current_pull_id(), [1, 2, 3]))
        assert {r.result for r in results} == {""}

    def test_task_raw_bind_never_leaks_across_tasks(self) -> None:
        """A task that RAW-binds a pull_id inside its own context copy poisons only
        itself — every other task keeps the driver's (empty) context, even on the
        single reused worker thread."""
        import structlog.contextvars
        from brutex.observability.correlation import current_pull_id

        pool = PoolRunParallel(max_workers=1)  # one reused thread — the leak shape

        def fn(x: int) -> str:
            if x == 0:
                structlog.contextvars.bind_contextvars(pull_id="poisonpoison")
            return current_pull_id()

        by_item = {r.item: r.result for r in pool(fn, [0, 1, 2])}
        assert by_item[0] == "poisonpoison"
        assert by_item[1] == "" and by_item[2] == ""

    def test_pool_worker_beat_reaches_the_runs_keyed_channel(self) -> None:
        """The real anti-false-stall wire: a worker's ``emit_progress_beat`` (the
        rate ``pace`` return shape) routes to the run's KEYED channel with zero
        foreign drops — the metadata-phase mechanism pin."""
        from brutex.observability.correlation import pull_id_scope
        from brutex.observability.progress_beat import (
            emit_progress_beat,
            register_progress_beat,
            reset_progress_beats,
        )

        reset_progress_beats()
        beats: list[int] = []
        register_progress_beat(lambda: beats.append(1), pull_id="abc123def456")
        try:
            pool = PoolRunParallel(max_workers=4)
            with pull_id_scope("abc123def456"):
                results = list(pool(lambda _x: emit_progress_beat(), list(range(6))))
            assert all(r.ok for r in results)
            assert beats == [1] * 6  # every worker beat was delivered, none dropped
        finally:
            reset_progress_beats()
