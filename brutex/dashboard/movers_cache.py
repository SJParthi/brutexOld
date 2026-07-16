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

## Opt-in stale-while-revalidate (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001)

The adaptive floor above bounds how OFTEN a mid-pull discovery walk re-runs,
but PAST the floor a stale-entry reader still recomputed INLINE — and a reader
arriving while a foreign compute was in flight blocked on the single-flight
``wait_event.wait()``, pinning a 32-token lake-limiter worker for the whole
walk (the 8-tab poll-herd defect this flag closes). An instance constructed
with ``stale_while_revalidate=True`` adds ONE arm between the floor arm and
the single-flight loop: while a pull is active (or a revalidator for the key
is already in flight — the pull-end boundary), a PAST-floor stale entry is
served IMMEDIATELY and at most ONE tokenless background daemon revalidates the
key (the :class:`~brutex.dashboard.rate_bridge_cache.RateBridgeCache`
non-blocking single-flight posture, #1522). The revalidator's store is guarded
NEWEST-WINS: it lands only when the slot is empty or still holds the
generation observed at spawn — any OTHER stamped generation means a LATER
store (the pull-end ``warm_discovery_memos`` reseed / an inline recompute)
already landed, and store ORDER, not numeric order, is the freshness signal
(generations are counters OR digests), so the fresher entry is never
regressed. Staleness stays log-labelled (``movers_cache.stale_served`` gains a
``revalidate`` field) and escalation-visible: serving past the instance's
``adaptive_floor_cap_s`` WARNs ``movers_cache.stale_extended`` once per
episode + every :data:`_SWR_STALE_EXTENDED_REWARN_S`. The DEFAULT
(``False``) keeps every existing consumer byte-identical — the blocking
single-flight wait included (regression-pinned).

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
    "OUTAGE_GENERATION",
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

#: Sentinel generation stamped on an entry seeded by :meth:`MoversSnapshotCache.
#: serve_last_known`'s fresh-compute arm (a revision-probe OUTAGE with no
#: last-known entry to serve — DEC-CACHE-SERVE-LAST-KNOWN-001). Strictly
#: negative: :func:`revision_generation` yields non-negative 64-bit ints and
#: every production caller passes ``generation >= 0``, so a recovered probe's
#: first REAL generation can never MATCH it as clean. HONEST QUALIFIER
#: (review fold): on a NO-floor cache (the oneclick instance) that makes the
#: first post-outage read recompute fresh ABSOLUTELY; on an
#: ``adaptive_floor`` cache the first post-recovery poll while a pull is
#: genuinely running may still FLOOR-SERVE the seed within its bounded,
#: LOGGED window (``movers_cache.stale_served`` with ``entry_generation=-1``
#: — the same envelope every floored serve accepts), after which the
#: generation mismatch recomputes; the seed is never served as CLEAN.
OUTAGE_GENERATION: Final[int] = -1

#: Re-WARN cadence for the ``movers_cache.stale_extended`` escalation rail
#: (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001): a key serving stale PAST the
#: instance's ``adaptive_floor_cap_s`` WARNs once per episode, then at most
#: once per this many seconds while the episode persists (a wedged revalidate
#: is loud, never silent-forever); the episode ends when a store lands.
_SWR_STALE_EXTENDED_REWARN_S: Final[float] = 300.0

#: Re-log throttle for ``movers_cache.swr_revalidate_failed``: a repeatedly
#: failing background revalidate WARNs at most once per this many seconds per
#: key (failures stay COUNTED between logs); the episode resets on a
#: successful store.
_SWR_FAILURE_RELOG_S: Final[float] = 60.0

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


def _default_swr_runner(job: Callable[[], None]) -> None:
    """Default SWR revalidate runner — one UNPOOLED daemon thread per spawn.

    DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001: deliberately a RAW daemon
    thread (the #1360 census-daemon shape) so a background revalidate consumes
    ZERO lake-limiter / AnyIO tokens, and deliberately NO ``run_with_deadline``
    — a legitimate multi-minute cold discovery walk must not be killed (the
    #999 no-kill stance; a wedged revalidator is at most ONE zombie daemon per
    key, escalation-WARNed via ``movers_cache.stale_extended``). Tests inject a
    run-inline / capturing ``swr_runner`` instead — deterministic coverage, no
    real threads.
    """
    threading.Thread(target=job, daemon=True, name="bx-cache-revalidate").start()


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
    stale_while_revalidate
        Opt-in flag (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001 — see the
        module docstring's SWR section): ``True`` adds the serve-stale +
        non-blocking-background-revalidate arm BETWEEN the adaptive-floor arm
        and the single-flight loop, engaged for a PAST-floor stale entry
        while a pull is active (or a revalidator for the key is already in
        flight). ``False`` (the default) is byte-identical to the pre-flag
        behaviour — a past-floor stale reader still recomputes inline /
        blocks on the single-flight wait (regression-pinned).
    swr_runner
        Injectable revalidate runner (tests pass a run-inline or capturing
        callable — deterministic coverage, no real threads); ``None`` (the
        default) spawns one unpooled daemon thread per revalidate
        (:func:`_default_swr_runner`). A runner that RAISES (e.g.
        ``Thread.start()`` under thread exhaustion) is caught by the serving
        reader: the in-flight marker is released, ONE
        ``movers_cache.swr_spawn_failed`` WARN fires per key episode, and the
        stale entry is still served — never a 500, never a wedge (the #1522
        hostile-review MAJOR fold; ``KeyboardInterrupt``/``SystemExit``
        propagate with the marker released).
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
        stale_while_revalidate: bool = False,
        swr_runner: Callable[[Callable[[], None]], None] | None = None,
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
        self._swr = stale_while_revalidate
        self._swr_runner = swr_runner if swr_runner is not None else _default_swr_runner
        self._clock = clock
        self._lock = threading.Lock()
        #: Keys with a background revalidator in flight (DEC-DASHBOARD-PULL-
        #: CONTENTION-RESILIENCE-001) — guarded by ``_lock``; bounded by the
        #: in-flight count (≤ one revalidator per key, removed in a finally).
        self._revalidating: set[str] = set()
        #: Per-key ``stale_extended`` escalation stamps (last WARN clock) —
        #: cleared when a store lands for the key (episode end); bounded like
        #: ``_outage_latched`` (LRU, ``capacity``).
        self._swr_stale_warned_at: OrderedDict[str, float] = OrderedDict()
        #: Per-key revalidate-failure state ``(count, last_logged_at)`` —
        #: throttles ``swr_revalidate_failed`` to once per
        #: :data:`_SWR_FAILURE_RELOG_S` while counting EVERY failure; reset on
        #: a successful store. Bounded (LRU, ``capacity``).
        self._swr_fail_state: OrderedDict[str, tuple[int, float]] = OrderedDict()
        #: Keys whose spawn-failure WARN already fired this episode (ONE
        #: ``swr_spawn_failed`` per key episode — the #1522 fold); reset on a
        #: successful store. Bounded (LRU, ``capacity``).
        self._swr_spawn_warned: OrderedDict[str, None] = OrderedDict()
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

    def _store_entry_locked(
        self,
        key: str,
        value: object,
        generation: int,
        *,
        computed_at: float,
        compute_duration: float,
        entry_bytes: int,
    ) -> tuple[int, bool, int]:
        """Insert/replace *key* under the HELD lock — the ONE store block.

        DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001: extracted VERBATIM from
        :meth:`get_or_compute`'s tail (byte-equal — the tail now delegates
        here) so the SWR revalidator's guarded store shares the exact same
        bookkeeping: prior-bytes retirement, insert, MRU, capacity eviction,
        and the byte-watermark eviction + single-entry-over episode
        (DEC-MEMORY-FOOTPRINT-HARDENING-001 — the watermark accounting is
        threaded through EVERY insert path). Also ends the key's SWR episodes
        (stale-extended escalation / failure throttle / spawn-warn latch) — a
        landed store means the next staleness is a NEW episode. Returns
        ``(evicted_for_bytes, single_over, total_after)`` for the caller's
        out-of-lock logging. MUST be called with ``self._lock`` held.
        """
        evicted_for_bytes = 0
        single_over = False
        prior = self._entries.get(key)
        if prior is not None:
            # Replacing a stale entry: retire its bytes before re-adding.
            self._total_bytes -= prior.size_bytes
        self._entries[key] = _Entry(
            value=value,
            generation=generation,
            computed_at=computed_at,
            compute_duration=compute_duration,
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
            # payload is kept alone (warned once per breach episode by the
            # caller).
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
        # A landed store ends the key's SWR episodes — O(1) pops, no-ops for
        # non-SWR caches (the dicts stay empty there).
        self._swr_stale_warned_at.pop(key, None)
        self._swr_fail_state.pop(key, None)
        self._swr_spawn_warned.pop(key, None)
        return evicted_for_bytes, single_over, self._total_bytes

    def _ensure_revalidator(
        self,
        key: str,
        generation: int,
        compute: Callable[[], object],
        *,
        spawn_if_absent: bool,
    ) -> str | None:
        """Ensure at most ONE background revalidator for *key* — the SWR core.

        Returns the serve label for ``movers_cache.stale_served``'s
        ``revalidate`` field — ``"in_flight"`` (a revalidator is already
        running), ``"spawned"`` (this call started one), ``"spawn_failed"``
        (the runner raised — marker released, stale still served, the #1522
        fold) — or ``None`` when no revalidator is in flight AND
        *spawn_if_absent* is ``False`` (pull idle ⇒ the caller falls through
        to the inline single-flight). ``KeyboardInterrupt``/``SystemExit``
        from the runner propagate with the marker released.
        """
        with self._lock:
            if key in self._revalidating:
                return "in_flight"
            if not spawn_if_absent:
                return None
            observed = self._entries.get(key)
            observed_generation = observed.generation if observed is not None else None
            self._revalidating.add(key)

        def _revalidate() -> None:
            """Compute + guarded-store OUT of the request path (daemon thread)."""
            try:
                started_at = self._clock()
                try:
                    value = compute()
                except Exception as exc:  # pylint: disable=broad-except — logged; stale keeps serving
                    self._note_revalidate_failure(key, exc)
                    return
                self._store_revalidated(
                    key,
                    value,
                    generation,
                    observed_generation=observed_generation,
                    started_at=started_at,
                )
            finally:
                with self._lock:
                    self._revalidating.discard(key)

        try:
            self._swr_runner(_revalidate)
        except (KeyboardInterrupt, SystemExit):
            with self._lock:
                self._revalidating.discard(key)
            raise
        except Exception:  # pylint: disable=broad-except — spawn failure must never break a read
            with self._lock:
                self._revalidating.discard(key)
            self._warn_spawn_failed(key)
            return "spawn_failed"
        # NOTE: with a run-INLINE test runner the revalidate has already
        # completed here, so this "started" line can follow its "stored" line
        # — production (daemon-thread) ordering is started-then-stored.
        _LOG.info("movers_cache.swr_revalidate_started", key=key, generation=generation)
        return "spawned"

    def _store_revalidated(
        self,
        key: str,
        value: object,
        generation: int,
        *,
        observed_generation: int | None,
        started_at: float,
    ) -> None:
        """Store a completed background revalidate — guarded NEWEST-WINS.

        The judge-found regression window (DEC-DASHBOARD-PULL-CONTENTION-
        RESILIENCE-001 §0.6): a SLOW revalidator finishing AFTER the pull-end
        ``warm_discovery_memos`` reseed must never clobber the warm's fresh
        entry with its older result. Generations here are counters OR digests
        (``revision_generation`` sha256 tokens), so "newer" cannot be a
        numeric ``>`` — the guard keys on STORE ORDER instead: the store lands
        only when the slot is EMPTY or still holds the generation observed at
        spawn; ANY other stamped generation means a LATER store already landed
        (the warm reseed / an inline recompute) and this result is skipped
        with one ``movers_cache.swr_revalidate_superseded`` INFO (the next
        past-floor read re-spawns — convergent, never a regression).
        """
        entry_bytes = self._size_payload(value)
        finished_at = self._clock()
        superseded_by: int | None = None
        evicted_for_bytes = 0
        single_over = False
        total_after = 0
        with self._lock:
            current = self._entries.get(key)
            if current is not None and current.generation != observed_generation:
                superseded_by = current.generation
            else:
                evicted_for_bytes, single_over, total_after = self._store_entry_locked(
                    key,
                    value,
                    generation,
                    computed_at=finished_at,
                    compute_duration=finished_at - started_at,
                    entry_bytes=entry_bytes,
                )
        if superseded_by is not None:
            _LOG.info(
                "movers_cache.swr_revalidate_superseded",
                key=key,
                spawn_generation=generation,
                observed_generation=observed_generation,
                stored_generation=superseded_by,
            )
            return
        _LOG.info("movers_cache.swr_revalidate_stored", key=key, generation=generation)
        # The same out-of-lock watermark logging discipline as get_or_compute.
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

    def _note_revalidate_failure(self, key: str, exc: Exception) -> None:
        """Count + throttled-WARN a failed background revalidate (60 s/key relog)."""
        now = self._clock()
        should_log = False
        failures = 1
        with self._lock:
            prior = self._swr_fail_state.get(key)
            if prior is None:
                should_log = True
                self._swr_fail_state[key] = (1, now)
            else:
                count, last_logged = prior
                failures = count + 1
                if now - last_logged >= _SWR_FAILURE_RELOG_S:
                    should_log = True
                    self._swr_fail_state[key] = (failures, now)
                else:
                    self._swr_fail_state[key] = (failures, last_logged)
            self._swr_fail_state.move_to_end(key)
            while len(self._swr_fail_state) > self._capacity:
                self._swr_fail_state.popitem(last=False)
        if should_log:
            _LOG.warning(
                "movers_cache.swr_revalidate_failed",
                key=key,
                error=repr(exc),
                failures=failures,
            )

    def _warn_spawn_failed(self, key: str) -> None:
        """ONE ``swr_spawn_failed`` WARN per key episode (reset on a landed store)."""
        with self._lock:
            warn = key not in self._swr_spawn_warned
            self._swr_spawn_warned[key] = None
            self._swr_spawn_warned.move_to_end(key)
            while len(self._swr_spawn_warned) > self._capacity:
                self._swr_spawn_warned.popitem(last=False)
        if warn:
            _LOG.warning("movers_cache.swr_spawn_failed", key=key)

    def _maybe_warn_stale_extended(self, key: str, age: float) -> None:
        """Escalation rail: WARN when a key serves stale PAST the floor cap.

        Once per episode + re-WARN every :data:`_SWR_STALE_EXTENDED_REWARN_S`
        while the episode persists (a wedged revalidator / dead volume is
        LOUD, never silent-forever); the episode ends when a store lands for
        the key (:meth:`_store_entry_locked` clears the stamp). Strict ``>``
        — serving exactly AT the cap is the floor family's own boundary law.
        """
        if age <= self._adaptive_floor_cap_s:
            return
        now = self._clock()
        with self._lock:
            last = self._swr_stale_warned_at.get(key)
            if last is not None and now - last < _SWR_STALE_EXTENDED_REWARN_S:
                return
            self._swr_stale_warned_at[key] = now
            self._swr_stale_warned_at.move_to_end(key)
            while len(self._swr_stale_warned_at) > self._capacity:
                self._swr_stale_warned_at.popitem(last=False)
        _LOG.warning(
            "movers_cache.stale_extended",
            key=key,
            age_s=round(age, 3),
            cap_s=self._adaptive_floor_cap_s,
        )

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

        Known edge (review fold, documented not mechanised): recovery is
        observed only by the next successful :meth:`get_or_compute` for the
        prefix — an artifact DELETED during the outage window routes its
        post-recovery polls down the ABSENT fresh-uncached arm (which never
        calls :meth:`get_or_compute`), so this cache-level latch stays armed
        for that prefix until the artifact returns (the recovery INFO fires
        then; ``oneclick_artifacts``' module-level probe latch recovers
        immediately regardless). Bias: at worst one extra WARN later, never
        a missed one.
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
        * **Stale-while-revalidate** (opt-in only —
          DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001): PAST the floor (or
          with no floor armed), the flagged instance serves the stale entry
          IMMEDIATELY while a pull is active OR a revalidator for the key is
          already in flight, ensuring at most ONE tokenless background
          revalidator (``movers_cache.stale_served`` gains a ``revalidate``
          field; serving past ``adaptive_floor_cap_s`` escalates via
          ``movers_cache.stale_extended``). Never engaged on a MISS (no entry
          to serve) — the cold first read stays the blocking single-flight.
          BOTH stale-serving arms re-check the slot under the serve lock: a
          CLEAN entry that raced in since the fast-path miss (a foreign
          thread's store / a landed revalidator / the warm reseed) is served
          as a plain hit — never a one-store-older snapshot, never a
          ``stale_served`` log for a stale serve that did not happen
          (round-3 review; the SWR arm's re-check runs BEFORE the spawn, the
          post-spawn window is a disclosed residual).
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
                raced_clean = False
                with self._lock:
                    # Round-3 review: re-read under the serve lock — a racing
                    # store (a foreign thread's compute / an SWR revalidator /
                    # the warm reseed) may have landed a CLEAN entry for THIS
                    # generation between the fast-path miss and this serve.
                    # Serve the fresh value as a plain hit instead of a
                    # one-store-older snapshot — and never log a stale serve
                    # that did not happen (the stale_served law).
                    latest = self._entries.get(key)
                    if latest is not None and latest.generation == generation:
                        entry, raced_clean = latest, True
                    if key in self._entries:
                        self._entries.move_to_end(key)  # keep the served key MRU.
                    recovered = self._note_result_locked(key, mru_prefix)
                if not raced_clean:
                    _LOG.info(
                        "movers_cache.stale_served",
                        stale=True,
                        key=key,
                        age_s=round(age, 3),
                        floor_s=round(effective_floor, 3),
                        entry_generation=entry.generation,
                        generation=generation,
                    )
                if recovered:
                    self._log_probe_recovered(key, mru_prefix)
                return entry.value  # type: ignore[return-value]
        # Stale-while-revalidate arm (DEC-DASHBOARD-PULL-CONTENTION-RESILIENCE-001)
        # — reached only when the floor arm did NOT serve (age >= the effective
        # floor, or the floor is not armed): while a pull is active (or a
        # revalidator for this key is ALREADY in flight — the pull-end boundary,
        # so a reader never blocks behind an in-flight revalidate), serve the
        # stale entry IMMEDIATELY and ensure AT MOST ONE tokenless background
        # revalidator. The blocked-waiter defect (`wait_event.wait()` below
        # pinning a lake-limiter worker per waiter) is structurally unreachable
        # for a flagged cache holding an entry. Idle with no revalidator in
        # flight falls through to the untouched inline single-flight (an idle
        # stamp is stable ⇒ the recompute is the cheap manifest-first read).
        if entry is not None and self._swr and not force_fresh:
            recovered = False
            raced_clean = False
            with self._lock:
                # Round-3 review: the same racing-store re-check BEFORE the
                # revalidator spawn — a CLEAN entry that landed for THIS
                # generation since the fast-path miss is served as a plain hit
                # (no spawn, no stale_served log for a stale serve that did
                # not happen). The residual window between the spawn below and
                # the serve tail is disclosed, not closed: re-reading there
                # would swallow an inline test runner's own store and invert
                # the deterministic serve-stale seam the suite pins — the
                # revalidator's store converges the NEXT read either way.
                latest = self._entries.get(key)
                if latest is not None and latest.generation == generation:
                    entry, raced_clean = latest, True
                    self._entries.move_to_end(key)  # a plain hit — mark MRU.
                    recovered = self._note_result_locked(key, mru_prefix)
            if raced_clean:
                if recovered:
                    self._log_probe_recovered(key, mru_prefix)
                return entry.value  # type: ignore[return-value]
            revalidate_state = self._ensure_revalidator(
                key, generation, compute, spawn_if_absent=self._pull_active()
            )
            if revalidate_state is not None:
                age = self._clock() - entry.computed_at
                effective_floor = effective_adaptive_floor(
                    entry.compute_duration,
                    base_s=self._adaptive_base_floor_s,
                    cap_s=self._adaptive_floor_cap_s,
                )
                _LOG.info(
                    "movers_cache.stale_served",
                    stale=True,
                    key=key,
                    age_s=round(age, 3),
                    floor_s=round(effective_floor, 3),
                    entry_generation=entry.generation,
                    generation=generation,
                    revalidate=revalidate_state,
                )
                self._maybe_warn_stale_extended(key, age)
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
            # deliberately unchanged. `wait_event` IS bound on every path that
            # reaches here (not-hit ⇒ the else arm ran; not-break ⇒ a FOREIGN
            # in-flight owner assigned it) — pyright's flow analysis cannot
            # correlate `hit` with the assignment, hence the targeted ignore.
            wait_event.wait()  # pyright: ignore[reportPossiblyUnboundVariable]
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
        with self._lock:
            # The ONE extracted store block (`_store_entry_locked` — shared
            # with the SWR revalidator's guarded store, byte-equal tail
            # refactor); `compute_duration` — the #920 adaptive floor's input
            # — covers compute PLUS sizing whenever the deep walk runs
            # (hostile-review M1: the floor must throttle for the WHOLE cold
            # cycle it protects against, not just its compute half); a
            # non-monotonic clock's negative delta is neutralized by the
            # floor's max(base, …) — no clamp needed (mirrors #920).
            evicted_for_bytes, single_over, total_after = self._store_entry_locked(
                key,
                value,
                generation,
                computed_at=finished_at,
                compute_duration=finished_at - started_at,
                entry_bytes=entry_bytes,
            )
            recovered = self._note_result_locked(key, mru_prefix)
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
        # Byte accounting mirrors get_or_compute (merge-compose, DEC-MEMORY-
        # FOOTPRINT-HARDENING-001 x DEC-CACHE-SERVE-LAST-KNOWN-001): the seeded
        # entry is sized and every eviction retires its bytes, so an outage
        # seed can never leak tracked bytes; the max_bytes watermark itself is
        # enforced at the next regular get_or_compute store (outage seeds are
        # rare + bounded to the outage's first poll wave).
        entry_bytes = self._size_payload(value)
        with self._lock:
            prior = self._entries.get(prefix)
            if prior is not None:
                self._total_bytes -= prior.size_bytes
            self._entries[prefix] = _Entry(
                value=value,
                generation=OUTAGE_GENERATION,
                computed_at=finished_at,
                compute_duration=finished_at - started_at,
                size_bytes=entry_bytes,
            )
            self._total_bytes += entry_bytes
            self._entries.move_to_end(prefix)
            while len(self._entries) > self._capacity:
                _, removed = self._entries.popitem(last=False)  # evict the LRU entry.
                self._total_bytes -= removed.size_bytes
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
