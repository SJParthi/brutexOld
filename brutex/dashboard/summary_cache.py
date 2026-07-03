"""Fast dashboard read core — O(1) summary cache + incremental coverage counters.

The SPEED CORE behind DEC-DASHBOARD-FASTREAD-001. The coverage page polls
``/api/v1/coverage/spot`` + ``/api/v1/coverage/options`` every 4 seconds; each
naively re-globs the WHOLE Parquet lake and re-aggregates every file
(``summarize_spot_datasets`` / ``summarize_option_datasets`` in
:mod:`brutex.dashboard.db_view`). During a 200-symbol pull that read serialised
behind the active writer's store lock AND blocked the event loop. This module
removes the *re-scan on every poll*:

* :class:`SummaryCache` — caches the last spot/option summary payloads behind a
  :class:`threading.Lock`, keyed by a monotonic ``generation`` counter. A poll
  returns the cached payload (an O(1) dict handoff) unless the cache is **dirty**
  (a write bumped the generation) AND an **ADAPTIVE TTL floor** has elapsed since
  the last recompute — so a 200-symbol write fan-out that calls :meth:`mark_dirty`
  hundreds of times can trigger at most one rescan per floor window, never
  hundreds of back-to-back full-lake scans. The floor is
  ``max(base 2 s, the last scan's own duration)`` capped at
  :data:`MAX_ADAPTIVE_FLOOR_S` (DEC-SUMMARY-CACHE-ADAPTIVE-FLOOR-001): a cheap
  scan keeps the snappy 2 s floor, but a scan that grows to N seconds (the
  full-lake completeness/spot aggregate during a long pull, which was observed
  reaching 30 s) self-throttles to a **≤50% duty cycle** — it never re-fires until
  it has been idle at least as long as it took — freeing the other half of the
  CPU for the concurrent pull instead of starving it with back-to-back 30 s scans.
* :class:`CoverageCounters` — a per-dataset-label int64 running bar tally bumped
  from the single write-progress funnel (``BackgroundPullJob._on_progress``).
  :meth:`snapshot` is genuinely O(1) in the lake size (one dict copy). Because
  the funnel passes the POST-DEDUP newly-written bar count, a duplicate re-pull
  passes ``bars == 0`` → no change → idempotent for free (CLAUDE.md §2 Rule 7).

No module-global state (CLAUDE.md §4): both classes are instantiated in
:func:`brutex.dashboard.app.build_app` and injected into the routes / the
background pull job.

Honest envelope (CLAUDE.md §2 Rule 10 — stated, not hidden)
-----------------------------------------------------------
* A cached summary **read** (the per-poll hot path) is **O(1)** — a dict handoff
  under one lock, independent of how many Parquet files the lake holds.
* The amortised **refresh** is still **O(files)** — it calls the real
  ``read_parquet`` aggregate over the lake. The TTL floor bounds how OFTEN that
  O(files) cost is paid (≤ once / 2 s while writes are landing), it does NOT make
  the refresh itself O(1). This is deliberately NOT claimed away.
* The running **coverage counter** add + :meth:`CoverageCounters.snapshot` are
  **O(1)** (one int add / one dict copy).
* An arbitrary user ``/db`` SQL query is inherently **O(rows scanned)** — this
  module does not touch it; it is made non-blocking + paginated elsewhere, NOT
  O(1).

Never claim "everything is O(1)": the headline counter + cached summary read are
O(1); the underlying lake aggregate is not.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

#: The two summary kinds the cache stores — the spot candle lake summary and the
#: expired-option lake summary. A :meth:`SummaryCache.get_or_refresh` call names
#: exactly one of these.
SPOT: Final[str] = "spot"
OPTIONS: Final[str] = "options"

#: The minimum number of seconds between two real recomputes of the SAME kind
#: while the cache is dirty. A 200-symbol write fan-out bumps the generation
#: hundreds of times; this floor collapses that storm into at most one full-lake
#: rescan per window, so the poll never amplifies a pull into hundreds of scans.
DEFAULT_TTL_FLOOR_S: Final[float] = 2.0

#: Hard cap on the ADAPTIVE floor (DEC-SUMMARY-CACHE-ADAPTIVE-FLOOR-001). The
#: effective floor grows to the LAST recompute's own duration (so a scan that
#: grows to 30 s throttles itself to a ≤50% duty cycle — it never re-fires until
#: it has been idle at least as long as it took, freeing the other half of the
#: CPU for a concurrent pull). This caps that self-throttle so a pathologically
#: slow scan can never freeze the detailed summary for longer than this — staleness
#: is bounded; the live headline counter (:class:`CoverageCounters`) is unaffected.
MAX_ADAPTIVE_FLOOR_S: Final[float] = 120.0

#: A summary payload is whatever the injected ``recompute`` returns — the list of
#: per-dataset dicts ``summarize_spot_datasets`` / ``summarize_option_datasets``
#: produce. Treated opaquely here (the cache never inspects it).
SummaryPayload = list[dict[str, Any]]

#: The zero-arg recompute callable the route injects — it builds + uses the
#: store's locked connection INSIDE the worker thread and returns the summary.
Recompute = Callable[[], SummaryPayload]


def _monotonic() -> float:
    """Default clock — the monotonic wall clock (injected for testability)."""
    import time

    return time.monotonic()


@dataclass(frozen=True, slots=True)
class _Entry:
    """One cached summary payload plus the generation + clock it was computed at.

    Attributes
    ----------
    payload
        The cached summary (the ``recompute`` result).
    generation
        The :class:`SummaryCache` generation counter value at compute time. The
        entry is *clean* iff this equals the cache's current generation.
    computed_at
        The injected clock value when the payload was computed — the TTL floor
        is measured against this.
    compute_duration
        How long the ``recompute`` that produced this payload took (clock delta
        across the call; a non-monotonic clock could make it negative, which the
        adaptive floor's ``max(base_floor, …)`` harmlessly neutralizes). The
        ADAPTIVE floor uses it: the next recompute of this kind is throttled for
        ``max(base_floor, compute_duration)`` (capped at
        :data:`MAX_ADAPTIVE_FLOOR_S`), so an expensive scan self-throttles to a
        ≤50% duty cycle while a cheap scan keeps the snappy base floor
        (DEC-SUMMARY-CACHE-ADAPTIVE-FLOOR-001).
    """

    payload: SummaryPayload
    generation: int
    computed_at: float
    compute_duration: float


class SummaryCache:
    """Thread-safe summary cache with write-invalidation + a TTL floor.

    A read calls :meth:`get_or_refresh` with the kind and a zero-arg
    ``recompute`` closure. The cached payload is returned unless the cache is
    dirty (its ``generation`` advanced past the entry's) AND at least
    ``ttl_floor_s`` seconds have elapsed since that kind was last recomputed —
    only then is ``recompute`` invoked and the fresh payload stored.

    Parameters
    ----------
    clock
        Zero-arg time source (defaults to :func:`time.monotonic`). Injected so
        tests are deterministic.
    ttl_floor_s
        Minimum seconds between two real recomputes of the same kind while
        dirty (defaults to :data:`DEFAULT_TTL_FLOOR_S`). A non-positive value
        means "recompute on every dirty read" (no floor).
    revision_probe
        Optional zero-arg **cross-process freshness probe**
        (DEC-HARDENING-LEFTOVERS-001; production wiring:
        :func:`brutex.dashboard.lake_revision.lake_revision` over the lake
        root). Called under the cache lock on every :attr:`generation` read and
        every :meth:`get_or_refresh` plan, it returns an opaque content token
        (``str``) or ``None`` ("unknown — no signal"). The FIRST non-``None``
        observation records the baseline WITHOUT bumping; a later token
        **change** bumps the generation exactly once (and records the new
        token); ``None`` is a no-op. This is how a CLI ``brutex fetch`` in a
        SEPARATE process (which can never call :meth:`mark_dirty`) invalidates
        every generation-keyed dashboard cache. The probe MUST be O(1) and MUST
        NOT raise (the production probe maps every :class:`OSError` to
        ``None``). ``None`` (the default) disables the probe — byte-identical
        to the pre-probe behaviour. Honest note: a dashboard-side pull may
        DOUBLE-bump (its own :meth:`mark_dirty` + the probe seeing the ledger
        move) — harmless, the keyed caches recompute lazily at most once per
        distinct generation actually read.

    Notes
    -----
    All state mutation + read is serialised by a single :class:`threading.Lock`.
    The ``recompute`` callable is invoked **outside** the lock so a slow
    full-lake aggregate never blocks a concurrent :meth:`mark_dirty` from the
    write thread or a :meth:`generation` read — the lock only guards the O(1)
    bookkeeping (the cache is a fast index over a slow source, mirroring
    :class:`brutex.dashboard.pull_progress.PullProgress`).
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = _monotonic,
        ttl_floor_s: float = DEFAULT_TTL_FLOOR_S,
        revision_probe: Callable[[], str | None] | None = None,
    ) -> None:
        self._clock = clock
        self._ttl_floor_s = ttl_floor_s
        self._revision_probe = revision_probe
        #: Last token the probe returned (``None`` until the first non-None
        #: observation — the baseline is recorded WITHOUT a bump).
        self._last_revision: str | None = None
        self._lock = threading.Lock()
        self._generation = 0
        self._entries: dict[str, _Entry] = {}
        #: Per-kind in-flight recompute guard so two concurrent dirty reads of
        #: the same kind don't both pay the O(files) scan (the second waits for
        #: the first's result via the post-recompute re-check).
        self._refreshing: set[str] = set()

    def mark_dirty(self) -> None:
        """Bump the generation so the next eligible read recomputes — O(1).

        Called from the write path (the background pull's per-label progress
        funnel). Cheap by design: a single guarded integer increment, so a
        200-symbol fan-out calling this hundreds of times is free — the TTL
        floor (not this method) bounds the rescan rate.
        """
        with self._lock:
            self._generation += 1

    def _observe_revision_locked(self) -> None:
        """Reconcile the generation against the cross-process probe — O(1).

        Caller MUST hold ``self._lock``. No probe / a ``None`` token → no-op.
        First non-``None`` token → record the baseline WITHOUT bumping (an app
        started over an existing lake must not invalidate its cold caches). A
        token CHANGE → record it + bump the generation exactly once, so every
        generation-keyed cache invalidates on the next key build
        (DEC-HARDENING-LEFTOVERS-001 — the CLI-write staleness fix).
        """
        if self._revision_probe is None:
            return
        token = self._revision_probe()
        if token is None:
            return
        if self._last_revision is None:
            self._last_revision = token
            return
        if token != self._last_revision:
            self._last_revision = token
            self._generation += 1

    @property
    def generation(self) -> int:
        """The current monotonic generation counter — O(1).

        Reconciles against the ``revision_probe`` first (one O(1) ``stat``
        under the lock), so a ledger checkpoint written by ANOTHER process (a
        CLI ``brutex fetch``) advances the generation on the very next read.
        """
        with self._lock:
            self._observe_revision_locked()
            return self._generation

    def get_or_refresh(self, kind: str, *, recompute: Recompute) -> SummaryPayload:
        """Return the cached *kind* payload, recomputing only if dirty past the TTL.

        Decision (all under the lock, O(1)):

        * **Hit** — a clean entry exists (its generation == the current
          generation): return it, no recompute.
        * **Stale-but-throttled** — the entry is dirty but recomputed less than
          the ADAPTIVE floor ago (``max(ttl_floor_s, last scan duration)`` capped
          at :data:`MAX_ADAPTIVE_FLOOR_S`): return the stale entry anyway (the
          floor wins, so a write storm — or an expensive scan — can't trigger a
          scan storm; an expensive scan self-throttles to a ≤50% duty cycle).
        * **Miss / eligible** — no entry yet, OR dirty and past the floor:
          invoke ``recompute`` (OUTSIDE the lock) and store the result stamped
          with the generation observed *before* the recompute.

        Parameters
        ----------
        kind
            One of :data:`SPOT` / :data:`OPTIONS` (any stable string key works;
            entries are namespaced by it).
        recompute
            Zero-arg closure that produces the fresh payload. MUST build + use
            the store's locked connection inside itself (it runs on the caller's
            thread — typically a threadpool worker — so the store lock is
            acquired+released there).

        Returns
        -------
        SummaryPayload
            The cached or freshly-computed summary for *kind*.
        """
        with self._lock:
            # Reconcile against the cross-process probe BEFORE planning, so the
            # SPOT/OPTIONS summaries themselves (not only the generation-KEYED
            # sibling caches) see a CLI-process ledger checkpoint as dirty.
            self._observe_revision_locked()
            decision = self._plan(kind)
        if decision is None:
            # Hit or stale-but-throttled: a payload is available without a scan.
            return self._entries[kind].payload
        gen_at_start = decision
        started_at = self._clock()
        try:
            payload = recompute()
        finally:
            with self._lock:
                self._refreshing.discard(kind)
        finished_at = self._clock()
        with self._lock:
            self._entries[kind] = _Entry(
                payload=payload,
                generation=gen_at_start,
                computed_at=finished_at,
                # The measured cost of THIS scan drives the adaptive floor for the
                # next one. A non-monotonic clock could make the delta negative, but the
                # `max(base_floor, duration)` in `_plan` neutralizes that (a negative can
                # never drive the floor below the base) — so no clamp is needed here.
                compute_duration=finished_at - started_at,
            )
        return payload

    def _plan(self, kind: str) -> int | None:
        """Decide whether *kind* needs a recompute — caller holds the lock.

        Returns the generation to stamp a fresh entry with when a recompute is
        needed (and marks the kind in-flight), or ``None`` when the cached entry
        should be served as-is (hit, throttled, or already being refreshed by a
        concurrent reader).
        """
        entry = self._entries.get(kind)
        if entry is not None and entry.generation == self._generation:
            # Clean hit — fast path, no scan.
            return None
        if entry is not None and kind in self._refreshing:
            # Another reader is already paying the scan AND a prior entry exists
            # to serve: return it instead of double-scanning (the warm in-flight
            # fast path). On a COLD cache (entry is None) we deliberately fall
            # THROUGH so this reader recomputes too — a transient double-scan on
            # first concurrent access is correct and idempotent, far better than
            # the `self._entries[kind]` KeyError → 500 that returning ``None``
            # here would cause when no entry has ever been written
            # (DEC-COMPLETENESS-O1-LATENCY-001 hostile-review MAJOR: this PR is
            # the first to SHARE the SPOT entry across two concurrently-polled
            # routes, widening the cold-start exposure of the latent race).
            return None
        if entry is not None:
            # ADAPTIVE floor (DEC-SUMMARY-CACHE-ADAPTIVE-FLOOR-001): throttle for at
            # least as long as the LAST scan took (capped), so an expensive full-lake
            # scan that grows to N seconds re-fires at most once per ~N seconds (a
            # ≤50% duty cycle) instead of back-to-back on every poll — freeing the
            # other half of the CPU for a concurrent pull. A cheap scan (duration <
            # base floor) keeps the snappy base floor, so idle/fast behaviour is
            # unchanged. ``ttl_floor_s <= 0`` (the test "no floor" mode) disables the
            # base but the adaptive component still bounds a genuinely slow scan.
            effective_floor = min(
                max(self._ttl_floor_s, entry.compute_duration), MAX_ADAPTIVE_FLOOR_S
            )
            if effective_floor > 0.0 and (self._clock() - entry.computed_at) < effective_floor:
                # Dirty but within the (adaptive) floor — throttle: serve the stale entry.
                return None
        self._refreshing.add(kind)
        return self._generation


class CoverageCounters:
    """Thread-safe per-label running bar tally — a truly O(1) coverage headline.

    Hooked into the single write-progress funnel
    (``BackgroundPullJob._on_progress(label, bars, failed)``): every successful
    window write calls :meth:`add` with the POST-DEDUP newly-written ``bars``,
    so the per-label tally and the grand total advance in O(1) without ever
    scanning the lake. :meth:`snapshot` is O(1) in the lake size (one dict copy).

    Idempotence for free (CLAUDE.md §2 Rule 7): a duplicate re-pull writes zero
    new bars, so the funnel passes ``bars == 0`` and :meth:`add` is a no-op — the
    counter is unchanged, mirroring the dedup'd lake. The audit hash-chain
    (Rule 8) is untouched: this only reads the count the funnel already emits.

    All state is guarded by a single :class:`threading.Lock` so the write thread
    and a reader request can never observe a torn tally.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_label: dict[str, int] = {}
        self._total: int = 0
        self._updated_count: int = 0

    def add(self, label: str, bars: int) -> None:
        """Add *bars* newly-written rows to *label*'s running tally — O(1).

        A ``bars <= 0`` call (a duplicate re-pull's post-dedup zero, or a failed
        window) is a **no-op** — neither the per-label tally, the grand total,
        nor the update counter move, so a re-pull leaves the headline identical
        (idempotent, CLAUDE.md §2 Rule 7). ``label`` is the dataset label the
        ingest funnel already emits (e.g. ``"NIFTY 1minute"``); it is namespaced
        as-is.
        """
        if bars <= 0:
            return
        with self._lock:
            self._by_label[label] = self._by_label.get(label, 0) + bars
            self._total += bars
            self._updated_count += 1

    def snapshot(self) -> CoverageCountersSnapshot:
        """Return an immutable, consistent O(1) view of the running tallies."""
        with self._lock:
            return CoverageCountersSnapshot(
                by_label=dict(self._by_label),
                total_bars=self._total,
                updates=self._updated_count,
            )


@dataclass(frozen=True, slots=True)
class CoverageCountersSnapshot:
    """An immutable point-in-time view of the incremental coverage counters.

    Attributes
    ----------
    by_label
        Per-dataset-label cumulative newly-written bar count.
    total_bars
        The grand total across every label — the headline number.
    updates
        How many non-zero :meth:`CoverageCounters.add` calls have landed (a
        duplicate-suppressing diagnostic; a re-pull does NOT advance it).
    """

    by_label: dict[str, int]
    total_bars: int
    updates: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for the headline counter route."""
        return {
            "by_label": dict(self.by_label),
            "total_bars": self.total_bars,
            "updates": self.updates,
        }


__all__ = [
    "DEFAULT_TTL_FLOOR_S",
    "MAX_ADAPTIVE_FLOOR_S",
    "OPTIONS",
    "SPOT",
    "CoverageCounters",
    "CoverageCountersSnapshot",
    "Recompute",
    "SummaryCache",
    "SummaryPayload",
]
