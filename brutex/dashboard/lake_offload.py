"""Dedicated, bounded AnyIO capacity limiter for LAKE-touching dashboard offloads.

DEC-DASHBOARD-LAKE-IO-LIMITER-001 — the #1522/#1622 named follow-up (audit M4).
Every ``run_in_threadpool`` call in the dashboard shares AnyIO's DEFAULT
40-token thread limiter. The lake lives on an external volume (CLAUDE.md §8);
its reads carry NO deadline guard (only the rate-bridge + sweep-progress memos
are deadline-bounded), so a WEDGED volume progressively pins tokens: each
poller's offloaded lake read blocks a worker forever, and once all 40 tokens
are stuck the LIVENESS routes cannot even START their offload — the measured
repro had the 41st offload queue 2701 ms behind 40 wedged workers, and the
deadline-guarded ``/api/v1/pull/rate`` (whose OWN read is bounded to 0.5 s)
starve indefinitely waiting for a token. #1522's honest envelope named exactly
this: "pool-wide wedge protection is OUT OF SCOPE here — a named follow-up".

This module is that follow-up: :class:`LakeOffload` — a tiny COMMON wrapper
(CLAUDE.md §11) around ``anyio.to_thread.run_sync`` carrying its OWN
:class:`anyio.CapacityLimiter` of :data:`DEFAULT_LAKE_OFFLOAD_TOKENS` (32)
tokens. ``build_app`` constructs one instance per app and every LAKE-touching
offload site calls it instead of ``run_in_threadpool``, so:

* a wedged lake pins AT MOST 32 lake-limiter threads — the DEFAULT 40-token
  pool is left entirely free for the liveness / in-memory / CPU paths
  (``/api/v1/pull/rate``'s bridge memo, ``/api/v1/sweep-progress``'s progress
  memo, the ``.xlsx``/``.zip`` CPU renders, the local log tail), which a lake
  outage therefore can no longer starve;
* the blast radius is also bounded FORWARD: request 33+ to a wedged lake
  queues on the lake limiter instead of growing an unbounded wedged-thread
  pile (AnyIO parks the waiting coroutines — no extra threads are created).

Sizing: 32 is deliberately BELOW the default pool's 40 so even a hypothetical
future path that chains a lake offload from inside a default-pool worker
cannot deadlock the whole pool, and comfortably above the dashboard's
realistic concurrent-reader count (a handful of pollers × a few tabs). Env
override :data:`LAKE_OFFLOAD_TOKENS_ENV` (fail-loud int ≥ 1 — the
DEC-RAM-BUDGETS-001 env doctrine).

Honest envelope (Golden Rule #10)
---------------------------------
* This is ISOLATION, not a deadline: a lake-limiter worker wedged on a stalled
  volume stays wedged until the volume recovers (the un-killable-syscall
  stance of DEC-FETCH-HUNG-WRITE-DEADLINE-001) — the guarantee is that lake
  wedges can no longer exhaust the DEFAULT pool, so liveness routes keep
  answering. Individual lake routes still block their own caller.
* The cold-waiter ``Event.wait()``s inside ``SummaryCache`` /
  ``MoversSnapshotCache`` now hold LAKE-limiter tokens (a waiter occupies the
  worker thread it runs on) — bounded by the same 32, by design.
* O(1) per call: one limiter acquire + one thread dispatch — the offloaded
  work's own cost is unchanged, and NO throughput claim is made (the limiter
  only ever bounds concurrency, it adds none).
"""

from __future__ import annotations

import functools
import math
import os
from collections.abc import Callable, Mapping
from typing import Final, TypeVar

import anyio
import anyio.to_thread

__all__ = [
    "DEFAULT_LAKE_OFFLOAD_TOKENS",
    "LAKE_OFFLOAD_TOKENS_ENV",
    "LakeOffload",
    "resolve_lake_offload_tokens",
]

_T = TypeVar("_T")

#: Concurrent lake-touching offloads per app — deliberately BELOW AnyIO's
#: default 40-token pool (see the module docstring's sizing rationale).
DEFAULT_LAKE_OFFLOAD_TOKENS: Final[int] = 32

#: Env override for :data:`DEFAULT_LAKE_OFFLOAD_TOKENS` (int ≥ 1, fail-loud).
LAKE_OFFLOAD_TOKENS_ENV: Final[str] = "BRUTEX_LAKE_OFFLOAD_TOKENS"


def resolve_lake_offload_tokens(env: Mapping[str, str] | None = None) -> int:
    """Resolve the lake-limiter size: env override, else the default.

    Fail-loud (the DEC-RAM-BUDGETS-001 env doctrine): a NON-BLANK malformed /
    non-integer / < 1 value raises :class:`ValueError` — never a silent
    default that would mask an operator typo (a ``0`` would wedge EVERY lake
    route shut, so it is refused, not honoured).

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Returns
    -------
    int
        The token count (≥ 1).

    Raises
    ------
    ValueError
        When the env var holds a non-blank value that is not an int ≥ 1.
    """
    source = os.environ if env is None else env
    raw = source.get(LAKE_OFFLOAD_TOKENS_ENV, "").strip()
    if not raw:
        return DEFAULT_LAKE_OFFLOAD_TOKENS
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"{LAKE_OFFLOAD_TOKENS_ENV}={raw!r} is not an integer (tokens >= 1 required)"
        ) from None
    if value < 1:
        raise ValueError(f"{LAKE_OFFLOAD_TOKENS_ENV}={raw!r} must be an integer >= 1")
    return value


class LakeOffload:
    """Run a blocking lake read on a worker thread under the app's OWN lake limiter.

    One instance per app (constructed by ``build_app`` — no module-global
    mutable state, CLAUDE.md §4). Call it exactly like
    ``fastapi.concurrency.run_in_threadpool``::

        payload = await _run_lake(_resolve)          # a closure
        rows = await _run_lake(store.list_x, arg, kw=1)  # args/kwargs forwarded

    The only difference from ``run_in_threadpool`` is the ``limiter=``: the
    dispatch rides this instance's :class:`anyio.CapacityLimiter` instead of
    AnyIO's default 40-token pool, so wedged lake reads can never starve the
    default pool (see the module docstring). Exceptions raised by *func*
    propagate unchanged (``HTTPException`` 400s included — the offload
    idiom's contract is identical).

    Parameters
    ----------
    tokens
        The limiter size; ``None`` resolves :func:`resolve_lake_offload_tokens`
        (env-overridable, fail-loud).
    """

    def __init__(self, tokens: int | None = None) -> None:
        self._tokens = resolve_lake_offload_tokens() if tokens is None else tokens
        if self._tokens < 1:
            raise ValueError(f"LakeOffload tokens must be >= 1, got {self._tokens}")
        # anyio ≥ 4 returns a loop-agnostic adapter here (binding lazily on
        # first acquire), so constructing OUTSIDE a running event loop — as
        # build_app does — is supported.
        self._limiter = anyio.CapacityLimiter(self._tokens)

    @property
    def total_tokens(self) -> int:
        """The configured limiter size (observability / test pin)."""
        return self._tokens

    @property
    def borrowed_tokens(self) -> int:
        """Tokens currently held (observability / test pin) — ``0`` when never used."""
        # statistics() on a never-acquired adapter is safe; math.floor keeps the
        # int contract if a float total ever leaks in (CapacityLimiter is float-typed).
        return math.floor(self._limiter.statistics().borrowed_tokens)

    async def __call__(self, func: Callable[..., _T], /, *args: object, **kwargs: object) -> _T:
        """Await *func(*args, **kwargs)* on a lake-limiter worker thread."""
        return await anyio.to_thread.run_sync(
            functools.partial(func, *args, **kwargs), limiter=self._limiter
        )
