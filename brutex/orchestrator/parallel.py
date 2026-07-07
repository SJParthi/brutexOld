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

import contextvars
import math
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Final

from brutex.orchestrator.universe_phases import ItemResult

#: Default worker count when callers do not size the pool to the rate budget.
#: NOTE (DEC-UNIVERSE-PULL-8RPS-001): 8 workers does NOT imply 8 r/s. Throughput
#: is ``in_flight / latency`` (Little's law) capped by the token bucket; a slow
#: per-call latency leaves 8 workers idle-waiting, so the achieved rate sits
#: *below* the bucket ceiling (the live expiries run measured 2.75 r/s at an
#: 8 r/s ceiling, ~2.9 s/call). Use :func:`workers_to_saturate` to size the pool
#: to the bucket ceiling; this default is the fallback for callers that don't.
DEFAULT_MAX_WORKERS = 8

#: Hard ceiling on pool fan-out — bounds the count of concurrent open sockets to
#: the vendor regardless of the latency budget. The token bucket caps the *rate*
#: (≤ ``max_rps``, Rule #6) independent of fan-out, so this only limits how many
#: requests are simultaneously *in flight*, never how many per second.
MAX_FANOUT: Final[int] = 32

#: Conservative per-call latency budget (seconds) used to size the pool. Set
#: from the live 2026-05-30 expiries run (~2.9 s mean ``get_expiries``
#: round-trip); the :mod:`~brutex.orchestrator.phase_throughput` telemetry now
#: measures the real value each run so this can be tuned from evidence.
DEFAULT_LATENCY_BUDGET_S: Final[float] = 3.0


def workers_to_saturate(
    max_rps: float,
    latency_budget_s: float = DEFAULT_LATENCY_BUDGET_S,
    *,
    cap: int = MAX_FANOUT,
) -> int:
    """Pool width that keeps the rate bucket — not network latency — the governor.

    Little's law: to *sustain* ``max_rps`` calls per second when each call takes
    ``latency_budget_s`` to return, ``max_rps × latency_budget_s`` calls must be
    in flight at once. Fewer workers than that and the pool is the bottleneck
    (latency-bound, achieved rate < ceiling); at or above it the token bucket is
    (rate-bound, achieved rate ≈ ceiling). The result is clamped to
    ``[1, cap]`` — never zero (always make progress), never more than ``cap``
    concurrent sockets. The bucket still enforces ``≤ max_rps`` (Rule #6), so
    over-provisioning workers only fills idle latency, never exceeds the rate.

    Parameters
    ----------
    max_rps
        The token bucket's target/ceiling rate (e.g. the adaptive limiter's
        ``max_rps``). A non-positive value sizes to a single worker.
    latency_budget_s
        Assumed per-call round-trip seconds. Non-positive sizes to one worker.
    cap
        Upper bound on concurrent in-flight requests (default :data:`MAX_FANOUT`).

    Returns
    -------
    int
        Worker count in ``[1, cap]``.
    """
    if max_rps <= 0.0 or latency_budget_s <= 0.0:
        return 1
    needed = math.ceil(max_rps * latency_budget_s)
    return max(1, min(cap, needed))


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

        Each task runs inside a COPY of the SUBMITTING thread's contextvars
        context (``contextvars.copy_context().run``, one fresh copy per task —
        DEC-PULL-BEAT-PERRUN-001 fix round): the driver's ambient ``pull_id``
        therefore reaches EVERY pool worker automatically, so a paced fetcher's
        ``emit_progress_beat`` (rate ``pace`` return / reauth wait — the
        metadata phase's ``get_expiries``/``get_contracts`` fan-out) attributes
        to the run's keyed channel instead of dropping as an unattributed
        foreign beat and false-stalling a heavily-throttled run. A driver with
        NO bound context propagates an empty copy — byte-identical to the
        pre-change fresh-thread behaviour — and per-task copies mean one task's
        binds can never leak into a later task on the reused pool thread.
        """
        if not items:
            return
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            future_to_item = {
                pool.submit(contextvars.copy_context().run, fn, item): item for item in items
            }
            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    result = future.result()
                except Exception as exc:
                    yield ItemResult(item=item, ok=False, result=None, exc=exc)
                else:
                    yield ItemResult(item=item, ok=True, result=result, exc=None)


class SerialRunParallel:
    """A ``RunParallel`` that maps *fn* over *items* IN-THREAD — zero worker threads.

    The serial twin of :class:`PoolRunParallel`: the SAME exception-capture
    contract (a raise → ``ItemResult(ok=False, exc=...)``; a clean return →
    ``ItemResult(ok=True, result=...)``), but every item runs on the CALLING
    thread, in order, with no pool.

    Why it exists (DEC-SPOT-CROSS-SYMBOL-FANOUT-001): the bulk spot pull fans ONE
    bounded outer pool across symbols, and each symbol's windows then run through
    THIS serial runner — one in-flight call per symbol, ``len(jobs)`` symbols
    concurrent. The previous design nested a second per-symbol *window* pool inside
    every outer worker, multiplying into ``outer_width × inner_width`` threads (a
    measured 24 × 30 = up to 720) that only thrashed the GIL on the Parquet write
    render without raising throughput. Collapsing the inner level to a serial
    in-thread map keeps a SINGLE bounded pool as the only concurrency source; the
    shared rate-limit token bucket remains the sole issue governor (Rule #6 — this
    raises no ceiling). Yields lazily so a caller draining as items complete sees
    no buffering difference from the pool runner.
    """

    __slots__ = ()

    def __call__[ItemT, ResultT](
        self,
        fn: Callable[[ItemT], ResultT],
        items: Sequence[ItemT],
    ) -> Iterator[ItemResult[ItemT, ResultT]]:
        """Map *fn* over *items* in order on the calling thread, one result each.

        A fetcher raising is CAPTURED into ``ItemResult(ok=False, exc=...)`` — never
        propagated — byte-for-byte matching :class:`PoolRunParallel`, so the bulk
        quarantine path behaves identically whether windows run serial or pooled.
        """
        for item in items:
            try:
                result = fn(item)
            except Exception as exc:  # mirror PoolRunParallel's capture contract
                yield ItemResult(item=item, ok=False, result=None, exc=exc)
            else:
                yield ItemResult(item=item, ok=True, result=result, exc=None)


__all__ = [
    "DEFAULT_LATENCY_BUDGET_S",
    "DEFAULT_MAX_WORKERS",
    "MAX_FANOUT",
    "PoolRunParallel",
    "SerialRunParallel",
    "workers_to_saturate",
]
