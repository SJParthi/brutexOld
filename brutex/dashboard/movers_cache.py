"""Keyed point-in-time **movers snapshot** cache — the `/movers` speed core (DEC-MOVERS-O1-LATENCY-001).

The redesigned ``/movers`` page (:mod:`brutex.dashboard.movers_snapshot` +
``movers_futures`` + ``movers_options``) answers *"who are the movers as of a
single date, on one timeframe, at one time?"* by reading the lake and reducing
every instrument to a snapshot. That reduction — discover every dataset, read
each one's bars, compute the per-instrument metrics — is the **expensive** part
(``O(instruments × bars-read)``). It depends ONLY on the *snapshot coordinates*
``(asset, exchange, interval, date, time[, expiry tier/track])`` — NOT on
``top_n`` and NOT on which sub-filter pill (Gainers / Losers / Top Volume / Top
Value …) is active. Ranking the already-computed candidate set to a ``top_n`` per
category is **cheap** (an in-memory ``O(candidates·log candidates)`` sort).

This module caches that expensive candidate set so re-clicking a sub-filter,
nudging ``top_n``, or re-selecting a coordinate the operator already viewed is an
**O(1) cache hit + a cheap re-rank**, never a re-scan of the lake.

## Invalidation — reuse the existing pull/materialize signal (CLAUDE.md §11 COMMON)

The cache is keyed by the snapshot coordinates AND stamped with a monotonic
**generation** the *caller* passes in. The dashboard already owns exactly such a
signal: :attr:`brutex.dashboard.summary_cache.SummaryCache.generation`, bumped on
every window write (single pull, the background pull job, and timeframe
materialize — ``app.py`` ``mark_dirty`` call sites). By stamping each entry with
that generation and treating a generation change as *dirty*, the movers cache
piggybacks on the same, already-tested write-invalidation path with ZERO new
wiring at the write sites — a fresh pull silently invalidates every cached
snapshot, so the page never serves data from before the pull (Golden Rule #1: no
stale illusion).

## Honest envelope (Golden Rule #10 — stated, not hidden)

* A cached **hit** (the dominant interaction after the first view of a
  coordinate) is **O(1)** in the lake size — a single dict handoff under one
  lock, independent of how many Parquet files the lake holds. The bench
  ``tests/bench/test_movers_cache_o1.py`` pins this flat as the lake grows.
* The first computation of a coordinate (a **miss**, or the first read after a
  pull) still pays the real ``O(instruments × bars-read)`` lake scan — this is
  NOT claimed away. The cache bounds how OFTEN that cost is paid (once per
  coordinate per generation), it does not make the scan itself O(1).
* Memory is **bounded**: an LRU keeps at most ``capacity`` snapshots resident
  (``O(capacity)`` space), so a long session that views thousands of coordinates
  never grows without bound — the least-recently-used snapshot is evicted.

No module-global state (CLAUDE.md §4): the cache is instantiated in
:func:`brutex.dashboard.app.build_app` and closed over by the ``/api/v1/movers``
route.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, TypeVar

__all__ = [
    "DEFAULT_MOVERS_CACHE_CAPACITY",
    "MoversSnapshotCache",
    "candidate_key",
]

#: The default maximum number of distinct snapshot coordinates kept resident.
#: A markets-screen session realistically revisits a handful of dates × a few
#: timeframes × the four/seven pills; 64 covers that with headroom while keeping
#: the resident set tiny and bounded (Golden Rule #10 — O(capacity) space).
DEFAULT_MOVERS_CACHE_CAPACITY: Final[int] = 64

#: The candidate-key field separator. ``|`` cannot appear in any of the keyed
#: coordinates (an interval label, an ISO date, an ``HH:MM`` time, a lower-cased
#: ``near``/``next``/``far`` tier or ``all``/``monthly``/``weekly`` track, an
#: ``NSE``/``BSE`` exchange, a ``spot``/``futures``/``options`` asset), so the
#: join is unambiguous — two different coordinate tuples can never collide.
_KEY_SEP: Final[str] = "|"

_T = TypeVar("_T")


def candidate_key(
    *,
    asset: str,
    exchange: str | None,
    interval: str,
    date: str,
    time: str | None,
    expiry_tier: str | None = None,
    expiry_track: str | None = None,
) -> str:
    """Build the stable cache key for one snapshot's *candidate* coordinates.

    The key intentionally EXCLUDES ``top_n`` (and the active sub-filter pill):
    those are applied cheaply at rank time, so every ``top_n`` / pill of the same
    coordinates shares ONE cached candidate set. ``None`` fields render as an
    empty segment, so ``time=None`` (a daily snapshot) and ``time=""`` key
    identically, and a spot key (no expiry tier/track) is distinct from an
    options key with the same date/interval/time.

    Parameters
    ----------
    asset
        ``spot`` / ``futures`` / ``options``.
    exchange
        The exchange the candidate set is scoped to (``NSE`` / ``BSE``), or
        ``None`` for the all-exchange population.
    interval
        The lake timeframe label (e.g. ``1day`` / ``5minute``).
    date
        The NORMALISED ISO ``YYYY-MM-DD`` snapshot day (already parsed, so the
        key is canonical — ``date=None`` ⇒ today must be resolved by the caller
        before keying).
    time
        The intraday ``HH:MM`` IST cutoff, or ``None`` / ``""`` for a daily
        snapshot.
    expiry_tier, expiry_track
        Options-only — the lower-cased ``near``/``next``/``far`` tier and
        ``all``/``monthly``/``weekly`` cadence track; ``None`` for spot/futures.

    Returns
    -------
    str
        The ``|``-joined cache key.
    """
    parts = (
        asset,
        exchange or "",
        interval,
        date,
        time or "",
        expiry_tier or "",
        expiry_track or "",
    )
    return _KEY_SEP.join(parts)


@dataclass(frozen=True, slots=True)
class _Entry:
    """One cached candidate set plus the generation it was computed at.

    The entry is *clean* iff :attr:`generation` equals the generation the caller
    passes to :meth:`MoversSnapshotCache.get_or_compute`; a newer generation (a
    write landed) makes it dirty and forces a recompute.
    """

    value: object
    generation: int


class MoversSnapshotCache:
    """Thread-safe, capacity-bounded LRU cache of computed movers candidate sets.

    A read calls :meth:`get_or_compute` with a :func:`candidate_key`, the current
    *generation* (the dashboard's :class:`~brutex.dashboard.summary_cache.SummaryCache`
    generation), and a zero-arg ``compute`` closure that performs the real lake
    scan. The cached value is returned unless the entry is missing or its stamped
    generation is stale, in which case ``compute`` runs (OUTSIDE the lock) and the
    fresh value is stored.

    Parameters
    ----------
    capacity
        Maximum number of distinct keys kept resident; the least-recently-used
        entry is evicted once the count would exceed it. Must be ``>= 1``.

    Notes
    -----
    All bookkeeping is serialised by a single :class:`threading.Lock`; the
    ``compute`` callable runs OUTSIDE the lock (mirroring
    :class:`~brutex.dashboard.summary_cache.SummaryCache`) so a slow lake scan
    never blocks a concurrent hit or eviction. A ``compute`` that raises
    propagates cleanly — nothing is stored — so a malformed request (e.g. a bad
    ``HH:MM`` time) never poisons the cache. The cache does NOT serialise two
    concurrent cold *misses* of the same key: each computes independently and the
    last store wins (the candidate set is a deterministic function of the
    coordinate + generation, so the duplicate scan is idempotent — Golden Rule
    #7). For a click-driven page that window is rare and the scan is bounded; the
    simplicity is deliberately preferred to a cross-thread wait (Golden Rule #10
    — the duplicate-scan cost is stated, not hidden).
    """

    def __init__(self, *, capacity: int = DEFAULT_MOVERS_CACHE_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._capacity = capacity
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    @property
    def capacity(self) -> int:
        """The maximum number of resident entries — O(1)."""
        return self._capacity

    def size(self) -> int:
        """The number of entries currently resident — O(1)."""
        with self._lock:
            return len(self._entries)

    def get_or_compute(self, key: str, *, generation: int, compute: Callable[[], _T]) -> _T:
        """Return *key*'s cached candidate set for *generation*, computing on a miss.

        Decision (the O(1) bookkeeping is under the lock; ``compute`` is not):

        * **Hit** — an entry exists whose stamped generation equals *generation*:
          mark it most-recently-used and return its value (no scan).
        * **Miss / stale** — no entry, or the entry's generation is stale (a
          write landed): run ``compute`` (outside the lock), store the result
          stamped with *generation*, evict the LRU entry if over capacity, and
          return it.

        Parameters
        ----------
        key
            The :func:`candidate_key` for the snapshot coordinates.
        generation
            The current write-generation (the dashboard ``SummaryCache``
            generation). An entry computed at an older generation is dirty.
        compute
            Zero-arg closure that performs the real lake scan and returns the
            candidate set. Runs on the caller's thread (typically a threadpool
            worker). May raise — the exception propagates and nothing is cached.

        Returns
        -------
        The cached or freshly-computed candidate set.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.generation == generation:
                self._entries.move_to_end(key)  # mark most-recently-used.
                return entry.value  # type: ignore[return-value]
        value = compute()
        with self._lock:
            self._entries[key] = _Entry(value=value, generation=generation)
            self._entries.move_to_end(key)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)  # evict the LRU entry.
        return value
