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

## Opt-in PULL-TIME adaptive floor (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001)

A generation MISMATCH normally forces an immediate recompute — correct for the
movers candidate sets (a pull must never serve pre-pull movers rows), but
punishing for the **discovery-walk memos** that ride this same class (the
``/db`` datasets triple-walk, the spot + F&O cascades, the latest-movers-date
resolve): during a pull the write generation bumps on EVERY window tick, so a
poll re-paid the full ``O(datasets)`` ``bars/`` walk back-to-back — the exact
scan-storm class DEC-SUMMARY-CACHE-ADAPTIVE-FLOOR-001 (#920) fixed for the
``SummaryCache``. An instance constructed with ``adaptive_floor=True`` AND a
``pull_active_probe`` therefore serves the STALE entry on a generation
mismatch — but ONLY while the probe reports a pull is genuinely running in this
process AND the entry is younger than ``min(max(base_floor, its own compute
duration), cap)`` (the ported #920 pattern) — logging one
``movers_cache.stale_served`` event per stale serve (``stale=True`` — never a
silent illusion, Golden Rule #1). The DEFAULT (no opt-in) behaviour is
byte-identical to before; ``force_fresh=True`` (the post-pipeline discovery
warm) bypasses the floor so the end-of-pull re-seed can never itself be served
stale. A CLI pull in a SEPARATE process has no in-process job signal, so the
probe reads ``False`` there and the floor simply never applies (stated, not
hidden — Golden Rule #10).

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
* An opted-in instance may serve a **bounded-stale** entry while a pull runs
  (age < the adaptive floor, capped at ``adaptive_floor_cap_s``); the staleness
  window closes the moment the pull ends (probe ``False`` → recompute) and the
  post-pipeline discovery warm force-refreshes at the end generation.

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

import structlog

_LOG = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_ADAPTIVE_BASE_FLOOR_S",
    "DEFAULT_MOVERS_CACHE_CAPACITY",
    "MAX_ADAPTIVE_FLOOR_CAP_S",
    "MoversSnapshotCache",
    "candidate_key",
]

#: The default maximum number of distinct snapshot coordinates kept resident.
#: A markets-screen session realistically revisits a handful of dates × a few
#: timeframes × the four/seven pills, AND the post-pull warmer now pre-warms the
#: whole default-day grid (DEC-MOVERS-WARM-GRID-001): options
#: {NSE,BSE,all}×{near,next,far}×{all,weekly,monthly} = 27 keys, futures
#: {NSE,BSE,all}×{near,next,far} = 9, spot {NSE,BSE,all} = 3 → 39 warmed keys for
#: ONE day. 256 holds that whole grid PLUS the operator's live interactive coordinates
#: (other dates / timeframes / exchanges) without evicting the just-warmed entries,
#: while keeping the resident set tiny + bounded (Golden Rule #10 — O(capacity) space).
DEFAULT_MOVERS_CACHE_CAPACITY: Final[int] = 256

#: The BASE of the opt-in pull-time adaptive floor
#: (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001 — the ported #920 pattern): an opted-in
#: entry younger than ``max(this base, its own compute duration)`` is served
#: stale on a generation mismatch WHILE a pull runs. Mirrors the
#: ``SummaryCache`` ``DEFAULT_TTL_FLOOR_S`` (2 s) so a cheap walk stays snappy.
DEFAULT_ADAPTIVE_BASE_FLOOR_S: Final[float] = 2.0

#: Hard cap on the opt-in adaptive floor. 600 s (not the SummaryCache's 120 s)
#: because the floor here applies ONLY while a pull is genuinely running in
#: this process (the probe gate) AND the post-pipeline discovery warm re-seeds
#: every memo when the pull ends — so a post-prebuild multi-minute discovery
#: walk (the dataset count multiplied by the 19 materialised TFs) is re-paid at
#: most once per 10 min DURING the pull, never after it. Matches the item-2
#: pull-duty cap (``summary_cache.MAX_PULL_DUTY_FLOOR_S``) for one consistent
#: pull-time staleness bound.
MAX_ADAPTIVE_FLOOR_CAP_S: Final[float] = 600.0

#: The candidate-key field separator. ``|`` cannot appear in any of the keyed
#: coordinates (an interval label, an ISO date, an ``HH:MM`` time, a lower-cased
#: ``near``/``next``/``far`` tier or ``all``/``monthly``/``weekly`` track, an
#: ``NSE``/``BSE`` exchange, a ``spot``/``futures``/``options`` asset), so the
#: join is unambiguous — two different coordinate tuples can never collide.
_KEY_SEP: Final[str] = "|"

_T = TypeVar("_T")


def _monotonic() -> float:
    """Default clock — the monotonic wall clock (injected for testability)."""
    import time

    return time.monotonic()


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
        The lower-cased ``near``/``next``/``far`` serial-expiry tier (Options AND
        Futures — DEC-MOVERS-FUTURES-TIER-001) and the ``all``/``monthly``/``weekly``
        cadence track (Options-only); ``None`` for spot (and ``expiry_track``
        ``None`` for futures).

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
    """One cached candidate set plus the generation + clock it was computed at.

    The entry is *clean* iff :attr:`generation` equals the generation the caller
    passes to :meth:`MoversSnapshotCache.get_or_compute`; a newer generation (a
    write landed) makes it dirty and forces a recompute — unless the instance's
    opt-in pull-time adaptive floor serves it stale
    (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001). :attr:`computed_at` /
    :attr:`compute_duration` feed that floor: the entry may be served stale
    while a pull runs only if it is younger than ``max(base, its own compute
    duration)`` capped — a slow walk earns a proportionally longer stale
    window (the #920 ≤50%-duty pattern). A non-monotonic clock could make the
    duration negative, which the floor's ``max(base, …)`` harmlessly
    neutralizes (mirrors ``summary_cache._Entry``).
    """

    value: object
    generation: int
    computed_at: float = 0.0
    compute_duration: float = 0.0


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
    pull_active_probe
        Optional zero-arg in-process pull signal for the OPT-IN adaptive floor
        (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001; the production wiring reads this
        app's own ``BackgroundPullJob.running``). ``True`` means a pull is
        genuinely running; ``False``/``None`` means it is not; a RAISING probe
        is swallowed to ``False`` (a probe error must never break a read). The
        probe is called OUTSIDE the cache lock (no nested-lock chain with the
        job's own lock). ``None`` (the default) disables the floor entirely.
    adaptive_floor
        Opt-in flag: ``True`` enables the pull-time stale-serve floor described
        in the module docstring (requires a wired *pull_active_probe* to ever
        fire). ``False`` (the default) keeps every generation mismatch an
        immediate recompute — byte-identical to the pre-floor behaviour
        (regression-pinned).
    adaptive_base_floor_s, adaptive_floor_cap_s
        The floor's base / hard cap (defaults
        :data:`DEFAULT_ADAPTIVE_BASE_FLOOR_S` / :data:`MAX_ADAPTIVE_FLOOR_CAP_S`);
        effective floor = ``min(max(base, entry.compute_duration), cap)`` — the
        ported #920 pattern.
    clock
        Zero-arg time source (defaults to :func:`time.monotonic`). Injected so
        the floor tests are deterministic.

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

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_MOVERS_CACHE_CAPACITY,
        pull_active_probe: Callable[[], bool | None] | None = None,
        adaptive_floor: bool = False,
        adaptive_base_floor_s: float = DEFAULT_ADAPTIVE_BASE_FLOOR_S,
        adaptive_floor_cap_s: float = MAX_ADAPTIVE_FLOOR_CAP_S,
        clock: Callable[[], float] = _monotonic,
    ) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._capacity = capacity
        self._pull_active_probe = pull_active_probe
        self._adaptive_floor = adaptive_floor
        self._adaptive_base_floor_s = adaptive_base_floor_s
        self._adaptive_floor_cap_s = adaptive_floor_cap_s
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()

    def _pull_active(self) -> bool:
        """``True`` iff the wired probe reports a pull genuinely running — O(1).

        Called OUTSIDE the cache lock (the production probe takes the pull
        job's own lock — never nested under this cache's). A raising probe is
        swallowed to ``False`` (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001 — a probe
        error must never break a read, only disable the stale-serve).
        """
        if self._pull_active_probe is None:
            return False
        try:
            return self._pull_active_probe() is True
        except Exception:  # pylint: disable=broad-except — probe must never break a read
            return False

    @property
    def capacity(self) -> int:
        """The maximum number of resident entries — O(1)."""
        return self._capacity

    def size(self) -> int:
        """The number of entries currently resident — O(1)."""
        with self._lock:
            return len(self._entries)

    def get_or_compute(
        self, key: str, *, generation: int, compute: Callable[[], _T], force_fresh: bool = False
    ) -> _T:
        """Return *key*'s cached candidate set for *generation*, computing on a miss.

        Decision (the O(1) bookkeeping is under the lock; ``compute`` is not):

        * **Hit** — an entry exists whose stamped generation equals *generation*:
          mark it most-recently-used and return its value (no scan).
        * **Stale-under-pull** (opt-in only —
          DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001): the entry's generation is stale
          BUT this instance opted into the adaptive floor, *force_fresh* is
          ``False``, the pull probe reads ``True``, AND the entry is younger
          than ``min(max(base, its compute duration), cap)``: serve the STALE
          entry (one ``movers_cache.stale_served`` log, ``stale=True``) — the
          pull is never starved by back-to-back discovery walks, and the
          staleness is bounded + pull-scoped (the probe reads ``False`` the
          moment the pull ends).
        * **Miss / stale** — no entry, or the entry's generation is stale (a
          write landed) past the floor / with no floor: run ``compute``
          (outside the lock), store the result stamped with *generation*, evict
          the LRU entry if over capacity, and return it.

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
        force_fresh
            ``True`` bypasses the stale-under-pull floor (NEVER the clean-hit
            fast path): the post-pipeline discovery warm passes it so the
            end-of-pull re-seed always lands fresh even though the pull job is
            still ``running`` while it executes. Default ``False``.

        Returns
        -------
        The cached or freshly-computed candidate set.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.generation == generation:
                self._entries.move_to_end(key)  # mark most-recently-used.
                return entry.value  # type: ignore[return-value]
        # Opt-in pull-time adaptive floor (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001).
        # The probe runs OUTSIDE the lock (it takes the pull job's own lock);
        # `entry` is the snapshot read under the lock above — a concurrent
        # replacement only means we serve an equally-valid value for this key.
        if entry is not None and self._adaptive_floor and not force_fresh and self._pull_active():
            age = self._clock() - entry.computed_at
            effective_floor = min(
                max(self._adaptive_base_floor_s, entry.compute_duration),
                self._adaptive_floor_cap_s,
            )
            if age < effective_floor:
                _LOG.info(
                    "movers_cache.stale_served",
                    stale=True,
                    key=key,
                    age_s=round(age, 3),
                    floor_s=round(effective_floor, 3),
                    entry_generation=entry.generation,
                    generation=generation,
                )
                with self._lock:
                    if key in self._entries:
                        self._entries.move_to_end(key)  # keep the served key MRU.
                return entry.value  # type: ignore[return-value]
        started_at = self._clock()
        value = compute()
        finished_at = self._clock()
        with self._lock:
            self._entries[key] = _Entry(
                value=value,
                generation=generation,
                computed_at=finished_at,
                # The measured walk cost drives the (opt-in) floor for the next
                # read; a non-monotonic clock's negative delta is neutralized by
                # the floor's max(base, …) — no clamp needed (mirrors #920).
                compute_duration=finished_at - started_at,
            )
            self._entries.move_to_end(key)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)  # evict the LRU entry.
        return value
