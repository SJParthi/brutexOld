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

A generation MISMATCH normally forces an immediate recompute. The original
design (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001) deliberately kept the movers
candidate-set instance on that strict rule ("a pull must never serve pre-pull
movers rows") and floored only the discovery-walk memos; that carve-out is
SUPERSEDED by DEC-LATENCY-HONESTY-BATCH-001 — the production ``/movers``
candidate cache now ALSO opts in, so during an active pull a poll may serve a
snapshot up to the floor window stale (bounded, logged, pull-scoped — never
pre-pull data beyond the floor window; see the sanctioned-change comment at
its ``app.py`` construction site). The strict always-recompute rule remains
the DEFAULT for any instance that does not opt in. The floor exists because a
mismatch is punishing for the **discovery-walk memos** that ride this same class (the
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

## Serve-last-known on a revision-probe OUTAGE (DEC-CACHE-SERVE-LAST-KNOWN-001)

Callers that key freshness on a lake CONTENT token (``lake_revision`` /
artifact stat tokens) receive ``None`` from the probe when the volume is
wedged — and the pre-change doctrine ("compute fresh, cache NOTHING") made
every poll re-pay the full build exactly while the volume was least able to
serve it (the probe-outage compute storm). :meth:`MoversSnapshotCache.
serve_last_known` + the ``mru_prefix=`` bookkeeping on
:meth:`~MoversSnapshotCache.get_or_compute` port the
:class:`~brutex.dashboard.rate_bridge_cache.RateBridgeCache` posture here:
during an outage the most-recent entry for the caller's stable key PREFIX is
served — LABELLED by the caller, WARNed once per outage (latched), recovered
with one INFO — and a prefix with no last-known entry computes once and
seeds the slot (:data:`OUTAGE_GENERATION`), so a persistent outage pays the
build once, not per poll. Absent-artifact ``None``\\ s (a merely missing
file) are the CALLER's branch — they stay cheap fresh computes, never a
stale serve of a deleted artifact.

## Honest envelope (Golden Rule #10 — stated, not hidden)

* A cached **hit** (the dominant interaction after the first view of a
  coordinate) is **O(1)** in the lake size — a single dict handoff under one
  lock, independent of how many Parquet files the lake holds. The bench
  ``tests/bench/test_movers_cache_o1.py`` pins this flat as the lake grows.
* The first computation of a coordinate (a **miss**, or the first read after a
  pull) still pays the real ``O(instruments × bars-read)`` lake scan — this is
  NOT claimed away. The cache bounds how OFTEN that cost is paid (once per
  coordinate per generation), it does not make the scan itself O(1).
* Memory is bounded in **ENTRY COUNT** (``capacity``), NOT in bytes
  (DEC-LATENCY-HONESTY-BATCH-001 corrects the prior unqualified "memory is
  bounded" wording): an LRU keeps at most ``capacity`` snapshots resident —
  ``O(capacity)`` ENTRIES, so a long session that views thousands of
  coordinates never grows without bound in count (the least-recently-used
  snapshot is evicted) — but each entry retains its WHOLE payload, so total
  resident bytes are ``capacity × payload-size``: large payloads (e.g. sweep
  scans keyed per revision) can retain gigabytes at the default capacity
  (≈256 × 4 MiB ≈ 1 GiB — the latency audit's probe figure, arithmetically
  derived, not re-measured on this branch). A byte-watermark / size-aware
  eviction is a NAMED follow-up, not claimed.
* An opted-in instance may serve a **bounded-stale** entry while a pull runs
  (age < the adaptive floor, capped at ``adaptive_floor_cap_s``); the staleness
  window closes the moment the pull ends (probe ``False`` → recompute) and the
  post-pipeline discovery warm force-refreshes at the end generation.

No module-global state (CLAUDE.md §4): the cache is instantiated in
:func:`brutex.dashboard.app.build_app` and closed over by the ``/api/v1/movers``
route.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, TypeVar

import structlog

from brutex.dashboard.summary_cache import effective_adaptive_floor

_LOG = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_ADAPTIVE_BASE_FLOOR_S",
    "DEFAULT_MOVERS_CACHE_CAPACITY",
    "MAX_ADAPTIVE_FLOOR_CAP_S",
    "OUTAGE_GENERATION",
    "MoversSnapshotCache",
    "candidate_key",
    "revision_generation",
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

#: Sentinel generation stamped on an entry seeded by :meth:`MoversSnapshotCache.
#: serve_last_known`'s fresh-compute arm (a revision-probe OUTAGE with no
#: last-known entry to serve — DEC-CACHE-SERVE-LAST-KNOWN-001). Strictly
#: negative: :func:`revision_generation` yields non-negative 64-bit ints and
#: every production caller passes ``generation >= 0``, so a recovered probe's
#: first REAL generation can never equal it — the first post-outage read
#: always recomputes fresh (never a lingering outage seed served as clean).
OUTAGE_GENERATION: Final[int] = -1

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


def revision_generation(token: str) -> int:
    """Map a content token onto a :class:`MoversSnapshotCache` generation int — one digest bridge.

    DEC-LEDGER-DELTA-O-PAGE-001: :class:`MoversSnapshotCache` keys freshness on
    an ``int`` *generation*, while the lake content signals are opaque string
    tokens (a ledger stamp token, a :mod:`~brutex.dashboard.lake_revision`
    composite). This bridge digests the token to the first 8 bytes of its
    SHA-256 — a 64-bit int, so ANY token change moves the generation. HONEST
    envelope (Golden Rule #10): two distinct tokens collide with probability
    ~2\\ :sup:`-64`; a collision would serve ONE stale entry for that colliding
    pair and self-heals on the next token move — documented, not hidden. Pure,
    O(len(token)).

    Homed HERE — beside the cache whose generations it feeds — and NOT in
    :mod:`~brutex.dashboard.lake_revision` (the token producer), because a
    module-level ``db_view → lake_revision`` import closes a real import cycle
    (CodeQL ``py/cyclic-import`` alert 690): ``lake_revision`` lazily imports
    ``brutex.data.window_ledger_store``, whose cold-start reconcile lazily
    imports ``brutex.dashboard.coverage_reconcile``, which lazily imports
    ``db_view`` back. This module is a leaf (only the stdlib +
    :mod:`~brutex.dashboard.summary_cache`), so ``db_view``'s existing
    module-level import stays cycle-free — AST-pinned by
    ``test_db_view_no_lake_revision_import``.
    """
    return int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big")


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
    ``HH:MM`` time) never poisons the cache. Two concurrent cold *misses* of the
    SAME key are now SINGLE-FLIGHT (DEC-SCAN-SINGLE-FLIGHT-001): exactly one
    thread runs ``compute`` while the others wait on a per-key event and then
    serve the stored result — the pre-fix "each computes independently" design
    let ten concurrent cold ``/movers`` coordinates each pay the full O(files)
    discovery walk on the 2026-07-05 post-restart lake (~29.7 min each,
    measured). A waiter whose *generation* is NEWER than the in-flight compute
    re-checks after the wake and recomputes at its own generation (never a
    stale serve — Golden Rule #1); a ``compute`` that RAISES wakes the waiters
    so exactly one takes over; a RE-ENTRANT miss (the in-flight ``compute``
    itself re-entering the same key on the same thread) recomputes instead of
    self-deadlocking. Misses of DIFFERENT keys still compute in parallel.
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
        #: Per-key in-flight guard (DEC-SCAN-SINGLE-FLIGHT-001): ``key →
        #: (event, owner thread ident)``. Concurrent cold misses of one key
        #: wait on the event instead of each paying the lake scan; the owner
        #: ident lets a re-entrant miss recompute instead of self-deadlock.
        self._inflight: dict[str, tuple[threading.Event, int]] = {}
        #: Per-PREFIX most-recently-stored full-key pointer
        #: (DEC-CACHE-SERVE-LAST-KNOWN-001): ``prefix → key``, recorded by
        #: :meth:`get_or_compute` when the caller threads ``mru_prefix=`` and
        #: read by :meth:`serve_last_known` during a revision-probe outage.
        #: Bounded like ``_entries`` (LRU, ``capacity``) so revision-folded
        #: key families can never grow it without bound.
        self._mru_by_prefix: OrderedDict[str, str] = OrderedDict()
        #: Prefixes currently in a revision-probe OUTAGE (an ordered SET —
        #: values unused). Drives the once-per-outage WARN latch (the
        #: ``rate_bridge_cache`` ``_degraded`` pattern): the FIRST
        #: :meth:`serve_last_known` per prefix WARNs, repeats stay silent,
        #: and the next successful :meth:`get_or_compute` for that prefix
        #: clears it (one INFO) so the NEXT outage WARNs again. Bounded by
        #: ``capacity`` (same eviction discipline).
        self._outage_latched: OrderedDict[str, None] = OrderedDict()

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

    def _note_result_locked(self, key: str, mru_prefix: str | None) -> bool:
        """Record *key* as *mru_prefix*'s last-known pointer + clear its outage latch.

        Called with ``self._lock`` HELD on every successful
        :meth:`get_or_compute` resolution (clean hit, floor stale-serve,
        fresh store) — O(1) dict ops. Returns ``True`` iff the prefix WAS
        outage-latched (a genuine recovery flip — the caller logs the one
        ``movers_cache.revision_probe_recovered`` INFO outside the lock).
        """
        if mru_prefix is not None:
            self._mru_by_prefix[mru_prefix] = key
            self._mru_by_prefix.move_to_end(mru_prefix)
            while len(self._mru_by_prefix) > self._capacity:
                self._mru_by_prefix.popitem(last=False)
        latch_key = mru_prefix if mru_prefix is not None else key
        if latch_key not in self._outage_latched:
            return False  # the common (no-outage) path — one O(1) membership check.
        del self._outage_latched[latch_key]
        return True

    def _latch_outage_locked(self, prefix: str) -> bool:
        """Latch *prefix* as in-outage; ``True`` iff this is the FIRST flip.

        Called with ``self._lock`` HELD by :meth:`serve_last_known`. The
        latch is a bounded ordered set (``capacity``) — a pathological
        prefix flood evicts the oldest latched prefix (its next serve
        re-WARNs: louder, never silent — Golden Rule #1).
        """
        if prefix in self._outage_latched:
            self._outage_latched.move_to_end(prefix)
            return False
        self._outage_latched[prefix] = None
        while len(self._outage_latched) > self._capacity:
            self._outage_latched.popitem(last=False)
        return True

    @property
    def capacity(self) -> int:
        """The maximum number of resident entries — O(1)."""
        return self._capacity

    def size(self) -> int:
        """The number of entries currently resident — O(1)."""
        with self._lock:
            return len(self._entries)

    def get_or_compute(
        self,
        key: str,
        *,
        generation: int,
        compute: Callable[[], _T],
        force_fresh: bool = False,
        mru_prefix: str | None = None,
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
        mru_prefix
            Optional stable prefix (DEC-CACHE-SERVE-LAST-KNOWN-001): every
            successful resolution (hit / floor-serve / store) records ``key``
            as this prefix's last-known pointer, so a later
            :meth:`serve_last_known` under a revision-probe OUTAGE can serve
            the most-recent entry for the prefix even though the outage
            yields no key to look up. Also clears the prefix's outage latch
            (one ``movers_cache.revision_probe_recovered`` INFO). Default
            ``None`` — byte-identical pre-change behaviour (a bare ``key``
            still clears its OWN latch, covering callers whose key IS the
            prefix).

        Returns
        -------
        The cached or freshly-computed candidate set.
        """
        my_ident = threading.get_ident()
        # Fast path + pull-time adaptive-floor stale-serve, checked ONCE before
        # joining the single-flight loop (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001):
        # a clean hit returns immediately; otherwise, while a pull is active in
        # this process, a still-fresh-enough stale entry is served instead of
        # recomputing (or waiting on an in-flight scan). The `_pull_active`
        # probe runs OUTSIDE the lock (it takes the pull job's own lock); the
        # once-per-outage recovery INFO logs outside it too.
        with self._lock:
            entry = self._entries.get(key)
            hit = entry is not None and entry.generation == generation
            if hit:
                self._entries.move_to_end(key)  # mark most-recently-used.
                recovered = self._note_result_locked(key, mru_prefix)
            else:
                recovered = False
        if hit and entry is not None:
            if recovered:
                self._log_probe_recovered(key, mru_prefix)
            return entry.value  # type: ignore[return-value]
        if entry is not None and self._adaptive_floor and not force_fresh and self._pull_active():
            age = self._clock() - entry.computed_at
            # Delegates to the ONE extracted #920 formula (byte-equal arithmetic
            # — DEC-LEDGER-DELTA-O-PAGE-001, CLAUDE.md §11 COMMON).
            effective_floor = effective_adaptive_floor(
                entry.compute_duration,
                base_s=self._adaptive_base_floor_s,
                cap_s=self._adaptive_floor_cap_s,
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
                    recovered = self._note_result_locked(key, mru_prefix)
                if recovered:
                    self._log_probe_recovered(key, mru_prefix)
                return entry.value  # type: ignore[return-value]
        # Single-flight (DEC-SCAN-SINGLE-FLIGHT-001): exactly one thread pays a
        # cold miss for a key; the rest wait on a per-key event, then re-plan.
        while True:
            with self._lock:
                entry = self._entries.get(key)
                hit = entry is not None and entry.generation == generation
                if hit:
                    self._entries.move_to_end(key)  # mark most-recently-used.
                    recovered = self._note_result_locked(key, mru_prefix)
                else:
                    recovered = False
                    inflight = self._inflight.get(key)
                    if inflight is None:
                        event = threading.Event()
                        self._inflight[key] = (event, my_ident)
                        break  # this thread computes.
                    wait_event, owner = inflight
                    if owner == my_ident:
                        # Re-entrant miss (the in-flight compute re-entered the
                        # same key on this thread): waiting would self-deadlock —
                        # compute WITHOUT re-registering; the completion pop
                        # below releases the OUTER registration only after a
                        # value is stored.
                        event = wait_event
                        break
            if hit and entry is not None:
                if recovered:
                    self._log_probe_recovered(key, mru_prefix)
                return entry.value  # type: ignore[return-value]
            # SINGLE-FLIGHT wait: another thread is paying the scan for this
            # exact key — wait (no lock held), then loop: a stored
            # same-generation entry serves as a hit; a raised / stale-generation
            # compute makes THIS thread the next computer. When this runs on a
            # dashboard offload worker, the waiting THREAD holds a LAKE-limiter
            # token (DEC-DASHBOARD-LAKE-IO-LIMITER-001): wedged waiters are
            # bounded by the 32-token lake pool and can no longer exhaust
            # AnyIO's default 40-token pool — the waiter SEMANTICS here are
            # deliberately unchanged.
            wait_event.wait()
        started_at = self._clock()
        try:
            value = compute()
        except BaseException:
            with self._lock:
                self._inflight.pop(key, None)
            event.set()  # wake waiters so exactly one takes over.
            raise
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
            recovered = self._note_result_locked(key, mru_prefix)
            # Store BEFORE waking waiters (same lock block) so a woken waiter
            # always finds the fresh entry — never a duplicate scan.
            self._inflight.pop(key, None)
        event.set()
        if recovered:
            self._log_probe_recovered(key, mru_prefix)
        return value

    def serve_last_known(self, prefix: str, *, compute: Callable[[], _T]) -> tuple[_T, bool]:
        """Serve *prefix*'s most-recent entry during a revision-probe OUTAGE.

        DEC-CACHE-SERVE-LAST-KNOWN-001 — the keystone replacing the probe-``None``
        "compute fresh, cache NOTHING" doctrine at the poll routes: when the
        caller's revision probe returned ``None`` because of a genuine probe
        OUTAGE (a stalled lake volume — NOT a merely-absent artifact, which
        the caller distinguishes), recomputing per poll re-ran the full
        O(stores × rows)/O(artifact) build back-to-back exactly when the
        volume was least able to serve it. This ports the
        :class:`~brutex.dashboard.rate_bridge_cache.RateBridgeCache`
        serve-last-known posture onto this cache:

        * **Last-known serve** — the per-prefix MRU pointer (recorded by
          :meth:`get_or_compute` ``mru_prefix=``, with a direct
          ``key == prefix`` fallback for stable-key callers) resolves the
          most-recent entry for the prefix REGARDLESS of its stamped
          generation; it is marked MRU and served. Returns ``(value, True)``
          — the caller MUST label the payload (an additive degrade field,
          Golden Rule #1: a stale serve is never silent).
        * **Fresh seed** — no entry exists for the prefix: ``compute`` runs
          (outside the lock; may raise — nothing stored) and the result is
          stored at ``key = prefix`` stamped :data:`OUTAGE_GENERATION`, so a
          PERSISTENT outage pays the build ONCE and later outage polls serve
          the seed. The sentinel generation can never equal a real caller
          generation, so the first post-recovery read always recomputes.
          Returns ``(value, False)``.

        One ``movers_cache.revision_probe_outage`` WARN per prefix per outage
        (the ``rate_bridge_cache`` ``_degraded`` latch pattern — never
        per-poll); the next successful :meth:`get_or_compute` for the prefix
        logs ``movers_cache.revision_probe_recovered`` and re-arms the latch.
        Concurrent fresh-seed misses of one prefix may each compute (the
        single-flight machinery is deliberately not engaged here — the seed
        store bounds the storm to the outage's first poll wave).

        Parameters
        ----------
        prefix
            The stable (revision-free) key prefix identifying the payload
            family.
        compute
            Zero-arg closure building the fresh payload when no last-known
            entry exists. May raise — the exception propagates, nothing is
            stored, the latch stays armed (the next poll retries).

        Returns
        -------
        tuple
            ``(value, served_last_known)`` — ``True`` iff a previously-stored
            entry was served rather than freshly computed.
        """
        first_flip = False
        with self._lock:
            entry: _Entry | None = None
            served_key: str | None = None
            for cand in (self._mru_by_prefix.get(prefix), prefix):
                if cand is None:
                    continue
                found = self._entries.get(cand)
                if found is not None:
                    entry, served_key = found, cand
                    break
            if entry is not None and served_key is not None:
                self._entries.move_to_end(served_key)  # keep the served key MRU.
                first_flip = self._latch_outage_locked(prefix)
        if entry is not None:
            if first_flip:
                _LOG.warning(
                    "movers_cache.revision_probe_outage",
                    prefix=prefix,
                    served="last_known",
                    stale=True,
                )
            return entry.value, True  # type: ignore[return-value]
        started_at = self._clock()
        value = compute()  # outside the lock; a raise propagates, nothing stored.
        finished_at = self._clock()
        with self._lock:
            self._entries[prefix] = _Entry(
                value=value,
                generation=OUTAGE_GENERATION,
                computed_at=finished_at,
                compute_duration=finished_at - started_at,
            )
            self._entries.move_to_end(prefix)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)  # evict the LRU entry.
            self._mru_by_prefix[prefix] = prefix
            self._mru_by_prefix.move_to_end(prefix)
            while len(self._mru_by_prefix) > self._capacity:
                self._mru_by_prefix.popitem(last=False)
            first_flip = self._latch_outage_locked(prefix)
        if first_flip:
            _LOG.warning(
                "movers_cache.revision_probe_outage",
                prefix=prefix,
                served="fresh_seeded",
                stale=False,
            )
        return value, False

    @staticmethod
    def _log_probe_recovered(key: str, mru_prefix: str | None) -> None:
        """One INFO per outage→recovery flip (the ``rate_bridge`` ``recovered`` twin)."""
        _LOG.info(
            "movers_cache.revision_probe_recovered",
            prefix=mru_prefix if mru_prefix is not None else key,
        )
