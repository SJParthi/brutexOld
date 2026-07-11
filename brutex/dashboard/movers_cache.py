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

## Honest envelope (Golden Rule #10 — stated, not hidden)

* A cached **hit** (the dominant interaction after the first view of a
  coordinate) is **O(1)** in the lake size — a single dict handoff under one
  lock, independent of how many Parquet files the lake holds. The bench
  ``tests/bench/test_movers_cache_o1.py`` pins this flat as the lake grows.
* The first computation of a coordinate (a **miss**, or the first read after a
  pull) still pays the real ``O(instruments × bars-read)`` lake scan — this is
  NOT claimed away. The cache bounds how OFTEN that cost is paid (once per
  coordinate per generation), it does not make the scan itself O(1).
* Memory is bounded in **ENTRY COUNT** (``capacity``) by default, plus an
  OPT-IN **byte watermark** (``max_bytes`` — DEC-MEMORY-FOOTPRINT-HARDENING-001,
  claiming the follow-up DEC-LATENCY-HONESTY-BATCH-001 named): an LRU keeps
  at most ``capacity`` snapshots resident — ``O(capacity)`` ENTRIES, so a
  long session that views thousands of coordinates never grows without bound
  in count — but each entry retains its WHOLE payload, so total resident
  bytes are ``capacity × payload-size``: large payloads can retain gigabytes
  at the default capacity (measured 84.9 MB at capacity 256 with 750-row
  dict payloads — 339.7 KiB/entry; sweep scans are far larger, ~1.1 KB/row ×
  100k rows per resident scan). An instance constructed with
  ``max_bytes=<budget>`` additionally evicts LRU-first until the tracked
  total fits the budget (the just-inserted entry is NEVER evicted — a single
  over-budget entry is kept alone + ONE WARNING per breach episode). HONEST
  (Golden Rule #10): the per-entry figure is a best-effort DEEP-SIZE
  **approximation** (:func:`deep_size_bytes` — a recursive
  ``sys.getsizeof`` walk over containers/dataclass-likes, cycle-guarded,
  depth-capped), NOT exact RSS; shared sub-objects double-count across
  entries and native (polars/duckdb) buffers under-count. A caller whose
  payloads are LARGE and self-describing can pass a cheap ``size_of``
  SIZING HINT (hostile-review M1 on DEC-MEMORY-FOOTPRINT-HARDENING-001):
  it is consulted FIRST per insert and a non-``None`` return replaces the
  deep walk — the reviewer measured the walk at **5.28 s** (plus a
  transient ~100+ MB ``seen`` id-set) over the 841k-row production ledger
  payload, a per-stamp cold-serve CPU/transient-memory regression the O(1)
  hint removes — while ``None`` / a raising hint fall back to
  :func:`deep_size_bytes`. The hint is itself an ESTIMATE feeding an
  estimate; its accuracy is the caller's contract (the ledger hint is
  pinned within 3× of the deep walk). The default ``max_bytes=None`` keeps
  every pre-watermark behaviour byte-identical (regression-pinned: neither
  the sizer nor the hint is ever called).
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
import sys
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
    "MoversSnapshotCache",
    "candidate_key",
    "deep_size_bytes",
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


#: Recursion-depth cap for :func:`deep_size_bytes`. Payloads here are shallow
#: (dataclass rows inside dicts/tuples inside a container — ≤ ~5 levels in
#: practice); the cap bounds a pathological deep nest to O(width^depth) work
#: and guarantees the walk can never hit ``RecursionError`` on its own.
_DEEP_SIZE_MAX_DEPTH: Final[int] = 16

#: Conservative per-object fallback when even ``sys.getsizeof`` fails (an
#: exotic ``__sizeof__`` override raising) — the CPython small-object floor,
#: so a sizing fault under-counts modestly instead of raising or reading 0.
_DEEP_SIZE_FALLBACK_BYTES: Final[int] = 64


def _shallow_size(obj: object) -> int:
    """One object's ``sys.getsizeof``, degraded to a constant — NEVER a raise."""
    try:
        return sys.getsizeof(obj)
    except Exception:  # pylint: disable=broad-except — sizing must never break an insert
        return _DEEP_SIZE_FALLBACK_BYTES


def deep_size_bytes(value: object) -> int:
    """Best-effort DEEP byte size of *value* — an APPROXIMATION, never exact RSS.

    DEC-MEMORY-FOOTPRINT-HARDENING-001 — the per-entry figure behind the
    opt-in ``max_bytes`` watermark: a small recursive ``sys.getsizeof`` walk
    over ``dict`` / ``list`` / ``tuple`` / ``set`` / ``frozenset`` containers
    and dataclass-like objects (their ``__dict__`` plus every MRO
    ``__slots__`` attribute), with scalars (``str`` / ``bytes`` /
    ``bytearray`` / numbers / ``None``) counted shallow (``getsizeof``
    already includes their payload). Cycle-guarded by object identity (a
    shared/self-referencing sub-object counts ONCE per call) and depth-capped
    at :data:`_DEEP_SIZE_MAX_DEPTH` (a deeper nest counts its remaining
    subtree shallow). ANY sizing fault — a raising ``__sizeof__``, a
    container mutating mid-walk, an exotic ``__dict__`` — degrades to the
    shallow ``sys.getsizeof`` of the whole value (never a raise: sizing can
    never break an insert — Golden Rule #1's never-crash posture for
    accounting).

    HONEST envelope (Golden Rule #10): the figure is a HEURISTIC — objects
    shared BETWEEN two cached entries double-count (each entry's walk sees
    them once), native library buffers (polars/duckdb/pyarrow) under-count
    (``getsizeof`` sees only the Python shell), and interned scalars
    over-count. Good enough to bound a cache's resident payload bytes; not a
    memory profiler.
    """
    seen: set[int] = set()

    def _walk(obj: object, depth: int) -> int:
        oid = id(obj)
        if oid in seen:
            return 0  # cycle / shared sub-object: counted once per call.
        seen.add(oid)
        total = _shallow_size(obj)
        if depth >= _DEEP_SIZE_MAX_DEPTH:
            return total  # deeper subtree counted shallow (bounded work).
        if obj is None or isinstance(obj, (str, bytes, bytearray, int, float, complex, bool)):
            return total  # scalars: getsizeof already includes the payload.
        if isinstance(obj, dict):
            for key, item in list(obj.items()):
                total += _walk(key, depth + 1) + _walk(item, depth + 1)
            return total
        if isinstance(obj, (list, tuple, set, frozenset)):
            for item in list(obj):
                total += _walk(item, depth + 1)
            return total
        # Dataclass-like objects: instance __dict__ and/or per-class __slots__.
        attrs = getattr(obj, "__dict__", None)
        if isinstance(attrs, dict):
            total += _walk(attrs, depth + 1)
        for klass in type(obj).__mro__:
            slots = getattr(klass, "__slots__", ())
            if isinstance(slots, str):  # a bare-string __slots__ is ONE name.
                slots = (slots,)
            for name in slots:
                try:
                    slot_value = getattr(obj, name)
                except AttributeError:
                    continue  # an unset slot holds nothing to size.
                total += _walk(slot_value, depth + 1)
        return total

    try:
        return _walk(value, 0)
    except Exception:  # pylint: disable=broad-except — sizing must never break an insert
        return _shallow_size(value)


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
    #: The entry's best-effort deep payload size (DEC-MEMORY-FOOTPRINT-
    #: HARDENING-001) — 0 when the instance carries no ``max_bytes`` watermark
    #: (the sizer is never called there; the default stays byte-identical).
    size_bytes: int = 0


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
    max_bytes
        Optional byte WATERMARK over the tracked payload total
        (DEC-MEMORY-FOOTPRINT-HARDENING-001): when set (``>= 1``), every
        insert sizes its payload via :func:`deep_size_bytes` (a best-effort
        deep approximation — see its honesty envelope) and LRU entries are
        evicted until the running total fits the watermark — the
        just-inserted entry is NEVER evicted, so a single over-budget payload
        is kept alone with ONE ``movers_cache.byte_watermark_single_entry_over``
        WARNING per breach episode (the episode ends when an insert lands the
        total back at/below the watermark). Eviction bursts log ONE
        ``movers_cache.byte_watermark_evicted`` INFO with counts, never a
        line per entry. The default ``None`` disables the watermark entirely
        — no sizing call, no accounting drift, behaviour byte-identical to
        the pre-watermark cache (regression-pinned).
    size_of
        Optional O(1)/cheap SIZING HINT consulted FIRST for each stored
        payload when a watermark is active (hostile-review M1 on
        DEC-MEMORY-FOOTPRINT-HARDENING-001 — the deep walk measured 5.28 s
        on the 841k-row production ledger payload, recurring per stamp
        move): a non-``None`` return (clamped ``>= 0``) is used as the
        entry's byte size; ``None`` — an unrecognized payload shape — and
        ANY raising hint fall back to :func:`deep_size_bytes` (a hint fault
        must never break an insert). Ignored entirely without a watermark
        (``max_bytes=None``); the default ``None`` keeps every watermark
        instance on the deep walk. The hint is an ESTIMATE — the watermark
        already tracks approximations, never exact RSS (see
        :func:`deep_size_bytes`'s honesty envelope); a hint's accuracy
        contract belongs to its caller.
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
        max_bytes: int | None = None,
        size_of: Callable[[object], int | None] | None = None,
        pull_active_probe: Callable[[], bool | None] | None = None,
        adaptive_floor: bool = False,
        adaptive_base_floor_s: float = DEFAULT_ADAPTIVE_BASE_FLOOR_S,
        adaptive_floor_cap_s: float = MAX_ADAPTIVE_FLOOR_CAP_S,
        clock: Callable[[], float] = _monotonic,
    ) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        if max_bytes is not None and max_bytes < 1:
            raise ValueError(f"max_bytes must be >= 1 (or None to disable), got {max_bytes}")
        self._capacity = capacity
        self._max_bytes = max_bytes
        self._size_of = size_of
        #: Running best-effort payload total (bytes) — 0 forever without a
        #: watermark (entries then carry size_bytes=0). Guarded by ``_lock``.
        self._total_bytes = 0
        #: One WARNING per single-entry-over-budget breach EPISODE; reset the
        #: moment an insert lands the total back at/below the watermark.
        self._single_over_warned = False
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

    def _size_payload(self, value: object) -> int:
        """Best-effort payload byte size for the watermark accounting — never a raise.

        Hostile-review M1 (DEC-MEMORY-FOOTPRINT-HARDENING-001): without a
        watermark, ``0`` with ZERO sizing work (the byte-identical default —
        neither the hint nor the deep walk runs, spy-pinned). With one, the
        optional ``size_of`` HINT is consulted FIRST — a non-``None`` return
        (clamped ``>= 0``) short-circuits the O(payload) deep walk that the
        reviewer measured at 5.28 s (plus a ~100+ MB transient ``seen``
        id-set) on the 841k-row production ledger payload; ``None`` (an
        unrecognized shape) and ANY hint fault fall back to
        :func:`deep_size_bytes` (which itself never raises), so sizing can
        never break an insert (Golden Rule #1's never-crash posture for
        accounting).
        """
        if self._max_bytes is None:
            return 0
        if self._size_of is not None:
            hinted: int | None
            try:
                raw = self._size_of(value)
                # int() inside the guard: a junk non-numeric hint return is a
                # hint fault too — fall back, never raise.
                hinted = None if raw is None else int(raw)
            except Exception:  # pylint: disable=broad-except — a hint fault falls back
                hinted = None  # deep-size fallback below — never a skipped insert.
            if hinted is not None:
                return max(0, hinted)
        return deep_size_bytes(value)

    @property
    def capacity(self) -> int:
        """The maximum number of resident entries — O(1)."""
        return self._capacity

    def size(self) -> int:
        """The number of entries currently resident — O(1)."""
        with self._lock:
            return len(self._entries)

    @property
    def max_bytes(self) -> int | None:
        """The byte watermark, or ``None`` when disabled — O(1)."""
        return self._max_bytes

    def total_bytes(self) -> int:
        """The tracked best-effort payload total in bytes — O(1).

        Always ``0`` without a watermark (no sizing runs there); with one, the
        sum of every resident entry's :func:`deep_size_bytes` figure — an
        approximation, never exact RSS (see the sizer's honesty envelope).
        """
        with self._lock:
            return self._total_bytes

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
        my_ident = threading.get_ident()
        # Fast path + pull-time adaptive-floor stale-serve, checked ONCE before
        # joining the single-flight loop (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001):
        # a clean hit returns immediately; otherwise, while a pull is active in
        # this process, a still-fresh-enough stale entry is served instead of
        # recomputing (or waiting on an in-flight scan). The `_pull_active`
        # probe runs OUTSIDE the lock (it takes the pull job's own lock).
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.generation == generation:
                self._entries.move_to_end(key)  # mark most-recently-used.
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
                return entry.value  # type: ignore[return-value]
        # Single-flight (DEC-SCAN-SINGLE-FLIGHT-001): exactly one thread pays a
        # cold miss for a key; the rest wait on a per-key event, then re-plan.
        while True:
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and entry.generation == generation:
                    self._entries.move_to_end(key)  # mark most-recently-used.
                    return entry.value  # type: ignore[return-value]
                inflight = self._inflight.get(key)
                if inflight is None:
                    event = threading.Event()
                    self._inflight[key] = (event, my_ident)
                    break  # this thread computes.
                wait_event, owner = inflight
                if owner == my_ident:
                    # Re-entrant miss (the in-flight compute re-entered the same
                    # key on this thread): waiting would self-deadlock — compute
                    # WITHOUT re-registering; the completion pop below releases
                    # the OUTER registration only after a value is stored.
                    event = wait_event
                    break
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
        # Size the payload OUTSIDE the lock (the value is still thread-local
        # here) so a large deep walk never blocks a concurrent hit; skipped
        # entirely without a watermark — the default stays byte-identical
        # (DEC-MEMORY-FOOTPRINT-HARDENING-001; the hint-first seam is the
        # hostile-review M1 fold — see `_size_payload`).
        entry_bytes = self._size_payload(value)
        # `finished_at` is captured AFTER sizing (hostile-review M1, honest
        # floor accounting): `compute_duration` — the #920 adaptive floor's
        # input — must cover compute PLUS sizing whenever the deep walk runs
        # (the pre-fix capture point excluded a multi-second walk, so the
        # floor under-throttled a genuinely ~7 s cold cycle to its ~2 s
        # compute half). Lock-wait is still excluded — the lock below is
        # taken only after this capture.
        finished_at = self._clock()
        evicted_for_bytes = 0
        single_over = False
        total_after = 0
        with self._lock:
            prior = self._entries.get(key)
            if prior is not None:
                # Replacing a stale entry: retire its bytes before re-adding.
                self._total_bytes -= prior.size_bytes
            self._entries[key] = _Entry(
                value=value,
                generation=generation,
                computed_at=finished_at,
                # The measured walk cost — compute PLUS sizing (hostile-review
                # M1: the floor must throttle for the WHOLE cold cycle it
                # protects against, not just its compute half) — drives the
                # (opt-in) floor for the next read; a non-monotonic clock's
                # negative delta is neutralized by the floor's max(base, …) —
                # no clamp needed (mirrors #920).
                compute_duration=finished_at - started_at,
                size_bytes=entry_bytes,
            )
            self._total_bytes += entry_bytes
            self._entries.move_to_end(key)
            while len(self._entries) > self._capacity:
                _, removed = self._entries.popitem(last=False)  # evict the LRU entry.
                self._total_bytes -= removed.size_bytes
            if self._max_bytes is not None:
                # Byte-watermark eviction: LRU-first until the tracked total
                # fits — but NEVER the just-inserted entry (it is MRU, and the
                # loop stops at one resident entry), so a single over-budget
                # payload is kept alone (warned once per breach episode below).
                while self._total_bytes > self._max_bytes and len(self._entries) > 1:
                    _, removed = self._entries.popitem(last=False)
                    self._total_bytes -= removed.size_bytes
                    evicted_for_bytes += 1
                if self._total_bytes > self._max_bytes:
                    if not self._single_over_warned:
                        self._single_over_warned = True
                        single_over = True
                else:
                    self._single_over_warned = False
            total_after = self._total_bytes
            # Store BEFORE waking waiters (same lock block) so a woken waiter
            # always finds the fresh entry — never a duplicate scan.
            self._inflight.pop(key, None)
        event.set()
        # Logging OUTSIDE the lock (the stale_served precedent): one line per
        # eviction BURST with counts — never a line per evicted entry — and
        # ONE single-entry-over WARNING per breach episode.
        if evicted_for_bytes:
            _LOG.info(
                "movers_cache.byte_watermark_evicted",
                evicted=evicted_for_bytes,
                total_bytes=total_after,
                max_bytes=self._max_bytes,
                key=key,
            )
        if single_over:
            _LOG.warning(
                "movers_cache.byte_watermark_single_entry_over",
                key=key,
                entry_bytes=entry_bytes,
                max_bytes=self._max_bytes,
            )
        return value
