"""Thread-pool–backed :class:`~brutex.orchestrator.universe_phases.RunParallel`.

The two-phase universe pull (DEC-UNIVERSE-PULL-PHASED-001) takes a
``run_parallel`` concurrency primitive so its ordering + barrier logic stays
pure + 100% unit-testable with a synchronous fake. This module is the *real*
production primitive: a bounded ``ThreadPoolExecutor`` that maps a fetcher over
a work list and yields one :class:`ItemResult` per item **as each future
completes** (``concurrent.futures.as_completed``).

It is deliberately tiny + dependency-light (stdlib only) so it imports without
native deps and is itself directly unit-testable: run it over a pure function
and assert every item produced an ``ItemResult`` (success xor captured
exception). A fetcher exception is CAPTURED into ``ItemResult.exc`` — never
propagated — so one bad item can never abort the phase (the orchestrator
records it and moves on).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed

from brutex.orchestrator.universe_phases import ItemResult

#: Default worker count — matches the adapter's start-8 r/s budget
#: (DEC-UNIVERSE-PULL-PARALLEL-001): a handful of in-flight requests saturates
#: the shared token bucket, which (not the pool) is the rate governor.
DEFAULT_MAX_WORKERS = 8


class PoolRunParallel:
    """A ``RunParallel`` backed by a bounded :class:`ThreadPoolExecutor`.

    One instance serves both phases (its ``__call__`` is generic over the item
    + result types), so the same pool/worker budget drives expiry discovery and
    contract discovery in turn. The pool is created + torn down per call
    (``with`` block) so no threads leak between phases or after the run.

    Parameters
    ----------
    max_workers
        Upper bound on concurrent in-flight fetches. Defaults to
        :data:`DEFAULT_MAX_WORKERS`. The shared rate-limit token bucket inside
        each fetcher is the true governor; this just bounds fan-out.
    """

    __slots__ = ("_max_workers",)

    def __init__(self, max_workers: int = DEFAULT_MAX_WORKERS) -> None:
        self._max_workers = max(1, max_workers)

    def __call__[ItemT, ResultT](
        self,
        fn: Callable[[ItemT], ResultT],
        items: Sequence[ItemT],
    ) -> Iterator[ItemResult[ItemT, ResultT]]:
        """Map *fn* over *items* concurrently, yielding one result each.

        Yields an :class:`ItemResult` as each future completes (order is
        completion-order, which the two-phase orchestrator does not depend on —
        it fully drains the iterator before the next phase). A fetcher raising
        is captured into ``ItemResult(ok=False, exc=...)``; a clean return is
        ``ItemResult(ok=True, result=...)``.
        """
        if not items:
            return
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            future_to_item = {pool.submit(fn, item): item for item in items}
            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    result = future.result()
                except Exception as exc:
                    yield ItemResult(item=item, ok=False, result=None, exc=exc)
                else:
                    yield ItemResult(item=item, ok=True, result=result, exc=None)


__all__ = ["DEFAULT_MAX_WORKERS", "PoolRunParallel"]
