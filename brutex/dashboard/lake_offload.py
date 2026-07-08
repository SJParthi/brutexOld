"""Dedicated bounded thread-offload pool for the dashboard's heavy lake reads.

DEC-DASHBOARD-READPATH-WEDGE-001 — the "pool-wide wedge protection" named
follow-up of DEC-PULL-RATE-BRIDGE-NONBLOCK-CACHE-001 (MIGRATION.md #1522).

The problem (forensically diagnosed from the operator's live pull log)
---------------------------------------------------------------------
Every blocking lake/DuckDB read in :mod:`brutex.dashboard.app` is offloaded off
the uvicorn event loop with Starlette's :func:`fastapi.concurrency.run_in_threadpool`
(DEC-DASHBOARD-FASTREAD-001). But ``run_in_threadpool`` routes EVERY offload
through ONE process-wide AnyIO limiter — ``anyio.to_thread.current_default_thread_limiter()``,
a DEFAULT of 40 tokens SHARED by all callers. During a write-heavy options pull
the external SSD (CLAUDE.md §8) went write-bound and the heavy full-lake read
offloads (the completeness SPOT scan, the pull-progress ledger fold, the
``/coverage/windows`` ledger read, the mapping-integrity reconciliation) each
wedged for tens of seconds waiting on the contended disk. Enough of them stacked
to drain all 40 default tokens, so EVERY threadpool-backed route — even a cheap
one — then queued behind the wedged reads waiting for a token. At 05:02:52Z the
page-serving path hard-wedged and stayed frozen ~37 minutes while the pull
itself stayed healthy.

The fix — a SEPARATE bounded limiter for the heavy lake reads
------------------------------------------------------------
This pool wraps :func:`anyio.to_thread.run_sync` with its OWN
:class:`anyio.CapacityLimiter` (default :data:`DEFAULT_LAKE_OFFLOAD_LIMIT` = 8
tokens), DISJOINT from the process-wide default 40-token limiter. The heavy
full-lake read routes are re-pointed from ``run_in_threadpool`` onto
:meth:`LakeOffloadPool.run`, so a wedged SSD can consume at most this pool's
tokens and can NEVER drain the default limiter. The light / pure-async poll
routes (``/api/v1/state``, ``/api/v1/pull/rate``, ``/api/v1/universe/pull-progress``)
keep the default limiter's full 40-token budget and answer promptly even while
every lake-offload token is stuck on the contended volume.

Honest envelope (Golden Rule #10)
---------------------------------
* This is ISOLATION, not a kill: a wedged read syscall still cannot be
  interrupted (the DEC-FETCH-HUNG-WRITE-DEADLINE-001 / #1522 stance). The
  guarantee is that a wedge is CONTAINED to this pool's tokens so the poll
  routes' default-limiter budget is never starved — never that the wedged
  worker thread is reaped.
* A latency/liveness fix, NO throughput claim. Under a healthy disk the pool is
  a thin pass-through: one ``functools.partial`` plus the SAME thread hop
  ``run_in_threadpool`` already paid, just accounted against a different
  limiter.
* If MORE than :data:`DEFAULT_LAKE_OFFLOAD_LIMIT` heavy reads wedge at once this
  pool itself saturates and a further heavy read queues — but the poll routes
  (on the default limiter) stay live regardless, which is the whole point.
  Composing a per-read wall-clock deadline + honest stale-serve on top (the
  #1522 ``rate_bridge_cache`` pattern) for the individual heavy routes is a
  named follow-up, orthogonal to this isolation.
* Remaining default-pool heavy reads: the /db DuckDB pagers + the /results
  XLSX builders (app.py:5610/5696/6087/6163/6446/6559) still offload via plain
  ``run_in_threadpool`` — the named follow-up DEC-LAKE-OFFLOAD-COMPLETE-002,
  which also covers the sibling coverage pages' missing client-side no-overlap
  poll guard (the ``guardedPollJson`` pattern universe.html gained here).
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable, Mapping
from typing import Final, TypeVar

import anyio.to_thread
from anyio import CapacityLimiter

T = TypeVar("T")

__all__ = [
    "DEFAULT_LAKE_OFFLOAD_LIMIT",
    "LAKE_OFFLOAD_LIMIT_ENV",
    "LakeOffloadPool",
    "resolve_lake_offload_limit",
]

#: Default concurrent-heavy-read budget for the dedicated lake-offload pool.
#: Deliberately well below the process-wide default 40-token AnyIO limiter so a
#: full wedge of this pool leaves the default limiter's budget intact for the
#: light poll routes. 8 comfortably covers the handful of distinct heavy full-
#: lake read routes a dashboard serves concurrently.
DEFAULT_LAKE_OFFLOAD_LIMIT: Final[int] = 8

#: Env override for :data:`DEFAULT_LAKE_OFFLOAD_LIMIT` (an integer >= 1).
LAKE_OFFLOAD_LIMIT_ENV: Final[str] = "BRUTEX_LAKE_OFFLOAD_LIMIT"


def resolve_lake_offload_limit(env: Mapping[str, str] | None = None) -> int:
    """Resolve the pool's token budget: :data:`LAKE_OFFLOAD_LIMIT_ENV`, else the default.

    Fail-loud (the DEC-RAM-BUDGETS-001 env doctrine): a NON-BLANK value that is
    not an integer >= 1 raises :class:`ValueError` — never a silent default that
    would mask an operator typo. A blank / unset var uses
    :data:`DEFAULT_LAKE_OFFLOAD_LIMIT`.

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Returns
    -------
    int
        The token budget (>= 1).

    Raises
    ------
    ValueError
        When the env var holds a non-blank value that is not an integer >= 1.
    """
    source = os.environ if env is None else env
    raw = source.get(LAKE_OFFLOAD_LIMIT_ENV, "").strip()
    if not raw:
        return DEFAULT_LAKE_OFFLOAD_LIMIT
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"{LAKE_OFFLOAD_LIMIT_ENV}={raw!r} is not an integer (a token budget >= 1 required)"
        ) from None
    if value < 1:
        raise ValueError(f"{LAKE_OFFLOAD_LIMIT_ENV}={raw!r} must be an integer >= 1")
    return value


class LakeOffloadPool:
    """Offload heavy lake reads through a dedicated bounded :class:`anyio.CapacityLimiter`.

    One instance per app (injected by ``build_app`` — no module-global mutable
    state, CLAUDE.md §4). The limiter is DISJOINT from the process-wide default
    AnyIO thread limiter that plain :func:`~fastapi.concurrency.run_in_threadpool`
    uses, so a wedge of the heavy reads cannot starve the light poll routes.

    The limiter is instantiated eagerly at ``build_app`` time; AnyIO binds it to
    the running event loop on first use, and the dashboard runs a single uvicorn
    loop, so eager construction is safe.

    Parameters
    ----------
    limit
        Token budget; ``None`` resolves :func:`resolve_lake_offload_limit`
        (env-overridable). Ignored when *limiter* is supplied.
    limiter
        An explicit :class:`anyio.CapacityLimiter` (tests may inject a shared /
        pre-sized one, or a 1-token limiter to exercise saturation). ``None``
        (the default) builds a fresh one sized by *limit*.
    """

    def __init__(
        self,
        *,
        limit: int | None = None,
        limiter: CapacityLimiter | None = None,
    ) -> None:
        self._limit = resolve_lake_offload_limit() if limit is None else int(limit)
        self._limiter = CapacityLimiter(self._limit) if limiter is None else limiter

    @property
    def limit(self) -> int:
        """The configured token budget (the size passed to a self-built limiter)."""
        return self._limit

    @property
    def limiter(self) -> CapacityLimiter:
        """The underlying :class:`anyio.CapacityLimiter` (for assertions / reuse)."""
        return self._limiter

    async def run(self, func: Callable[..., T], /, *args: object, **kwargs: object) -> T:
        """Run blocking *func* on a worker thread under THIS pool's limiter.

        A drop-in for :func:`fastapi.concurrency.run_in_threadpool` that accounts
        the offload against the dedicated lake limiter instead of the process-wide
        default. Binds ``*args`` / ``**kwargs`` with :func:`functools.partial`
        (AnyIO's ``run_sync`` takes no keyword args for the callable), exactly as
        Starlette's ``run_in_threadpool`` does.

        Parameters
        ----------
        func
            The blocking callable (a route's lake/store read closure).
        *args, **kwargs
            Bound onto *func* before the thread hop.

        Returns
        -------
        T
            Whatever *func* returns.
        """
        call = functools.partial(func, *args, **kwargs)
        return await anyio.to_thread.run_sync(call, limiter=self._limiter)
