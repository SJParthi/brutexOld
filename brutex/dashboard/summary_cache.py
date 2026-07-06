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

#: The summary kinds the cache stores — the spot candle lake summary, the
#: expired-option lake summary, and the lake duplicate-census (the ``/universe``
#: lake-uniqueness chip, DEC-LAKE-HEAL-001 — a full bounded lake scan whose own
#: duration drives the adaptive TTL floor, exactly the #920 machinery). A
#: :meth:`SummaryCache.get_or_refresh` call names exactly one of these.
SPOT: Final[str] = "spot"
OPTIONS: Final[str] = "options"
LAKE_UNIQUENESS: Final[str] = "lake_uniqueness"

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

#: PULL-time duty-degrade multiplier (DEC-COMPLETENESS-PULL-DUTY-DEGRADE-001):
#: while the in-process pull probe reads ``True``, the effective floor is
#: WIDENED to ``min(this × the last scan's duration, MAX_PULL_DUTY_FLOOR_S)``
#: (never below the existing adaptive floor) — a measured 27 s full-lake scan
#: (the observed p50 68 ms → 27.1 s degradation during a pull) then re-fires at
#: most once per ~243 s instead of every ~27 s, a ≤10% duty cycle that stops
#: the poll starving the concurrent pull. The non-pull path keeps today's
#: exact semantics byte-identically.
PULL_DUTY_FLOOR_MULTIPLIER: Final[float] = 9.0

#: Hard cap on the pull-widened floor — a pathologically slow scan can never
#: freeze the detailed summary for longer than 10 min even mid-pull; staleness
#: stays bounded AND is surfaced honestly (``duty_degraded`` /
#: ``snapshot_age_s`` via :meth:`SummaryCache.stamp_freshness` — never silent).
MAX_PULL_DUTY_FLOOR_S: Final[float] = 600.0

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


@dataclass(frozen=True, slots=True)
class SummaryEntryMeta:
    """Immutable freshness snapshot of one cached summary entry — the honesty surface.

    DEC-COMPLETENESS-PULL-DUTY-DEGRADE-001: the pull-time duty-degrade may serve
    a deliberately-stale payload while a pull runs, so the operator must be able
    to SEE that staleness (never silent — Golden Rule #1). Returned by
    :meth:`SummaryCache.entry_meta` and stamped into the coverage/completeness
    JSON by :meth:`SummaryCache.stamp_freshness`.

    Attributes
    ----------
    age_s
        Seconds since this kind's payload was last recomputed (clock delta,
        clamped ≥ 0 against a non-monotonic injected clock).
    duty_degraded
        ``True`` iff the most recent read of this kind served a STALE payload
        while the in-process pull probe read ``True`` (the pull-widened floor
        OR the base floor held it back mid-pull); reset ``False`` by the next
        fresh recompute or clean hit.
    """

    age_s: float
    duty_degraded: bool


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
        root). Called **OUTSIDE the cache lock** on every :attr:`generation`
        read and every :meth:`get_or_refresh` plan
        (DEC-SURFACE-TRIAL-001 — the probe is one ``os.stat`` of a lake that
        may live on a stalled external volume, and a wedged syscall holding
        the lock would block the pull's :meth:`mark_dirty` write funnel; the
        token is *reconciled* under the lock), it returns an opaque content
        token (``str``) or ``None`` ("unknown — no signal"). The FIRST
        non-``None`` observation records the baseline WITHOUT bumping; a later
        token **change** bumps the generation exactly once (and records the new
        token); ``None`` is a no-op. This is how a CLI ``brutex fetch`` in a
        SEPARATE process (which can never call :meth:`mark_dirty`) invalidates
        every generation-keyed dashboard cache. The probe MUST be O(1) and MUST
        NOT raise (the production probe maps every :class:`OSError` to
        ``None``). ``None`` (the default) disables the probe — byte-identical
        to the pre-probe behaviour. Honest notes: (a) a dashboard-side pull may
        DOUBLE-bump (its own :meth:`mark_dirty` + the probe seeing the ledger
        move) — harmless, the keyed caches recompute lazily at most once per
        distinct generation actually read; (b) because tokens are observed
        outside the lock, two concurrent readers may reconcile OUT OF ORDER —
        the consequence is only ever a spurious EXTRA bump (over-invalidation),
        never a missed change: the next probe of the final on-disk state always
        differs from any stale baseline, and the generation stays monotonic;
        (c) cold-start-during-outage recovery
        (DEC-SUMMARY-CACHE-COLD-OUTAGE-BUMP-001, mechanising the MINOR-2
        envelope note of DEC-SURFACE-TRIAL-001): if the app STARTS while the
        probe is erroring (``None``) and entries are populated during the
        outage, the FIRST recovered token bumps the generation ONCE (a CLI
        write may have landed unseen), then records the baseline — at most
        ONE spurious bump per process (the baseline None→non-None transition
        happens exactly once), pure over-invalidation, never a missed change.
        A healthy warm start (first probe call already non-``None``) still
        records the baseline WITHOUT bumping, and an outage-recovery with NO
        populated entries is a plain no-bump baseline; the outage window is
        loudly logged per probe call.

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
        #: Late-bound in-process pull signal (DEC-COMPLETENESS-PULL-DUTY-DEGRADE-001):
        #: ``None`` (the default) keeps today's semantics byte-identical; the app
        #: wiring assigns the real probe via :meth:`set_pull_active_probe` AFTER the
        #: pull job exists (the build_app construction order is circular — the cache
        #: is built before the job that the production probe reads).
        self._pull_active_probe: Callable[[], bool | None] | None = None
        #: Last token the probe returned (``None`` until the first non-None
        #: observation — the baseline is recorded WITHOUT a bump).
        self._last_revision: str | None = None
        #: True iff a WIRED probe returned ``None`` (an outage) BEFORE the
        #: baseline was ever recorded — the cold-start-during-outage window
        #: (DEC-SUMMARY-CACHE-COLD-OUTAGE-BUMP-001). Consumed (and cleared)
        #: by the baseline branch of :meth:`_reconcile_revision_locked`.
        self._cold_probe_outage = False
        self._lock = threading.Lock()
        self._generation = 0
        self._entries: dict[str, _Entry] = {}
        #: Per-kind honest served-stale-under-pull flag (feeds
        #: :meth:`entry_meta` / :meth:`stamp_freshness`).
        self._duty_degraded: dict[str, bool] = {}
        #: Per-kind in-flight recompute guard: ``kind → (event, owner thread
        #: ident)``. A WARM concurrent dirty read serves the stale entry (as
        #: before); a COLD concurrent read now WAITS on the event instead of
        #: duplicating the scan (DEC-SCAN-SINGLE-FLIGHT-001 — the operator's
        #: restart log measured SIX concurrent 58-68 min lake-uniqueness census
        #: scans, one per cold poll, because the pre-fix cold path deliberately
        #: fell through to a "transient double-scan"). The owner thread ident
        #: preserves the one legal fall-through: a RE-ENTRANT read from inside
        #: the in-flight recompute itself (same thread) must recompute rather
        #: than self-deadlock waiting on its own event (the
        #: DEC-COMPLETENESS-O1-LATENCY-001 cold-reentrancy case, still pinned).
        self._refreshing: dict[str, tuple[threading.Event, int]] = {}

    def set_pull_active_probe(self, probe: Callable[[], bool | None] | None) -> None:
        """LATE-BIND the in-process pull probe (DEC-COMPLETENESS-PULL-DUTY-DEGRADE-001).

        The production probe reads this app's own ``BackgroundPullJob.running``,
        which is constructed AFTER this cache in ``build_app`` (the circular
        construction order the ledger entry names) — hence a setter, not a
        constructor parameter. ``None`` disables the duty-degrade (today's exact
        semantics). The probe is called OUTSIDE the cache lock on every
        :meth:`get_or_refresh` (the job's own lock is never nested under this
        cache's) and a RAISING probe is swallowed to ``False`` — a probe error
        must never break a read, only disable the widening. DASHBOARD pulls
        only: a CLI ``brutex fetch`` in a separate process has no in-process
        job signal, so the widened floor simply never applies there (honest
        envelope, Golden Rule #10).
        """
        self._pull_active_probe = probe

    def _probe_pull_active(self) -> bool:
        """``True`` iff the wired pull probe reports a pull running — NO lock held."""
        if self._pull_active_probe is None:
            return False
        try:
            return self._pull_active_probe() is True
        except Exception:  # pylint: disable=broad-except — probe must never break a read
            return False

    def mark_dirty(self) -> None:
        """Bump the generation so the next eligible read recomputes — O(1).

        Called from the write path (the background pull's per-label progress
        funnel). Cheap by design: a single guarded integer increment, so a
        200-symbol fan-out calling this hundreds of times is free — the TTL
        floor (not this method) bounds the rescan rate.
        """
        with self._lock:
            self._generation += 1

    def _probe_token(self) -> str | None:
        """Run the cross-process revision probe — NO lock held (O(1)).

        The probe is one ``os.stat`` of the window-ledger Parquet, which on
        the production 8 TB external-SSD lake can WEDGE if the volume stalls
        (the observed mode DEC-FETCH-HUNG-WRITE-DEADLINE-001 guards the write
        path against). Running it outside ``self._lock`` guarantees a hung
        probe can never block a concurrent :meth:`mark_dirty` from the pull's
        write funnel or a :meth:`generation` read (DEC-SURFACE-TRIAL-001 —
        restores the module's own "no I/O under the lock" design intent).
        Returns ``None`` when no probe is wired.
        """
        if self._revision_probe is None:
            return None
        return self._revision_probe()

    def _reconcile_revision_locked(self, token: str | None) -> None:
        """Reconcile the generation against a probed *token* — O(1).

        Caller MUST hold ``self._lock``; *token* was probed OUTSIDE it. A
        ``None`` token → no-op for the generation, but a WIRED probe's
        pre-baseline ``None`` arms the cold-start-outage flag
        (DEC-SUMMARY-CACHE-COLD-OUTAGE-BUMP-001). First non-``None`` token →
        record the baseline WITHOUT bumping (an app started over an existing
        lake must not invalidate its cold caches) — UNLESS the flag is armed
        AND entries were populated during the outage, in which case the
        recovery bumps exactly once (a CLI write may have landed unseen).
        A token CHANGE → record it + bump the generation exactly once, so
        every generation-keyed cache invalidates on the next key build
        (DEC-HARDENING-LEFTOVERS-001 — the CLI-write staleness fix). Tokens observed outside the lock may be applied out of
        order under concurrency — the worst case is a spurious extra bump
        (pure over-invalidation), never a missed change, and ``_generation``
        only ever increments (see the ``revision_probe`` parameter notes).
        """
        if token is None:
            if self._revision_probe is not None and self._last_revision is None:
                # A WIRED probe erroring BEFORE any baseline == the cold-start
                # outage window (DEC-SUMMARY-CACHE-COLD-OUTAGE-BUMP-001). An
                # UNwired cache also reconciles ``None`` (``_probe_token``
                # returns it without calling anything), so the wired-ness
                # check is the load-bearing discriminator — an unwired cache
                # must never arm the recovery bump.
                self._cold_probe_outage = True
            return
        if self._last_revision is None:
            self._last_revision = token
            if self._cold_probe_outage and self._entries:
                # Recovery from a cold-start outage with caches ALREADY
                # populated: a CLI write may have landed UNSEEN while the
                # probe was erroring, so invalidate ONCE — at most one
                # spurious bump per process, structurally: ``_last_revision``
                # transitions None→non-None exactly once, and the flag is
                # cleared below. An outage-recovery with NO entries records
                # the plain baseline (nothing was cached over stale data).
                self._generation += 1
            self._cold_probe_outage = False
            return
        if token != self._last_revision:
            self._last_revision = token
            self._generation += 1

    @property
    def generation(self) -> int:
        """The current monotonic generation counter — O(1).

        Reconciles against the ``revision_probe`` first (one O(1) ledger
        ``stat`` plus the O(dirs) sweeps-tree digest —
        DEC-RESULTS-SURFACE-HONESTY-001 — taken OUTSIDE the lock so a stalled
        volume can never block the write funnel — DEC-SURFACE-TRIAL-001), so a
        ledger checkpoint OR a sweep-store write from ANOTHER process (a CLI
        ``brutex fetch`` / ``sweep-all``) advances the generation on the very
        next read.
        """
        token = self._probe_token()
        with self._lock:
            self._reconcile_revision_locked(token)
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
        while True:
            # Probe OUTSIDE the lock (a hung stat must never block mark_dirty —
            # DEC-SURFACE-TRIAL-001), then reconcile + plan under it: the
            # SPOT/OPTIONS summaries themselves (not only the generation-KEYED
            # sibling caches) see a CLI-process ledger checkpoint as dirty. The
            # pull probe is ALSO hoisted outside the lock (it takes the pull
            # job's own lock — never nested under this cache's,
            # DEC-COMPLETENESS-PULL-DUTY-DEGRADE-001).
            token = self._probe_token()
            pull_active = self._probe_pull_active()
            with self._lock:
                self._reconcile_revision_locked(token)
                decision = self._plan(kind, pull_active=pull_active)
            if decision is None:
                # Hit or stale-but-throttled: a payload is available without a scan.
                return self._entries[kind].payload
            if isinstance(decision, threading.Event):
                # COLD single-flight (DEC-SCAN-SINGLE-FLIGHT-001): another
                # thread is already paying the first-ever scan of this kind —
                # wait for it (no lock held), then re-plan: the stored entry
                # serves as a hit/throttled read; if the scan RAISED instead,
                # exactly one woken waiter re-registers and becomes the new
                # computer. Unbounded wait is deliberate + honest: pre-fix,
                # every cold poller duplicated the wedged scan and hung anyway
                # — now at most ONE scan is in flight to wedge.
                decision.wait()
                continue
            gen_at_start = decision
            started_at = self._clock()
            try:
                payload = recompute()
            except BaseException:
                with self._lock:
                    aborted = self._refreshing.pop(kind, None)
                if aborted is not None:
                    aborted[0].set()  # wake waiters so one can take over
                raise
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
                # A fresh recompute is by definition NOT duty-degraded (the honest
                # flag tracks the LAST served read of this kind).
                self._duty_degraded[kind] = False
                # Entry stored BEFORE the event is set (same lock block), so a
                # woken waiter always finds a servable payload — never a second
                # scan. A RE-ENTRANT recompute (see `_plan`) is not registered,
                # so this pop may release the OUTER registration early — safe:
                # the inner entry was just stored, and the outer's own
                # completion pops nothing (`None` → no double-set concern).
                done = self._refreshing.pop(kind, None)
            if done is not None:
                done[0].set()
            return payload

    def entry_meta(self, kind: str) -> SummaryEntryMeta | None:
        """Return *kind*'s freshness snapshot, or ``None`` when never computed — O(1).

        The honesty accessor (DEC-COMPLETENESS-PULL-DUTY-DEGRADE-001): the
        coverage/completeness routes stamp its ``age_s`` / ``duty_degraded``
        into their JSON via :meth:`stamp_freshness` so a pull-time stale
        payload is VISIBLE, never silent (Golden Rule #1).
        """
        now = self._clock()
        with self._lock:
            entry = self._entries.get(kind)
            if entry is None:
                return None
            return SummaryEntryMeta(
                age_s=max(now - entry.computed_at, 0.0),
                duty_degraded=self._duty_degraded.get(kind, False),
            )

    def stamp_freshness(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Stamp ``duty_degraded`` + ``snapshot_age_s`` onto *payload* (in place) — O(1).

        The ONE place the honest freshness fields are written (CLAUDE.md §11
        COMMON — the /coverage/spot, /coverage/options and completeness
        scoreboard routes all call it, no copy-paste). ``snapshot_age_s`` is
        the served payload's age WHILE duty-degraded and ``0.0`` otherwise —
        deliberately CONDITIONAL: an always-ticking age would make every poll
        body differ, defeating the DEC-DASHBOARD-JS-PERF-001 exact-body-text
        skip-render pollers in the un-degraded steady state (the honesty
        surface is the pull-time staleness, which IS stamped, growing per poll
        while it lasts). A *kind* with no cached entry yet (a no-lake degrade
        payload) reads an honest ``duty_degraded=False`` /
        ``snapshot_age_s=0.0`` — the payload was built fresh for this request,
        not served from the cache.
        """
        meta = self.entry_meta(kind)
        degraded = meta.duty_degraded if meta is not None else False
        payload["duty_degraded"] = degraded
        payload["snapshot_age_s"] = round(meta.age_s, 3) if meta is not None and degraded else 0.0
        return payload

    def _plan(self, kind: str, *, pull_active: bool = False) -> int | threading.Event | None:
        """Decide whether *kind* needs a recompute — caller holds the lock.

        Returns the generation to stamp a fresh entry with when THIS reader
        should recompute (registering the kind in-flight, except on the
        re-entrant escape), a :class:`threading.Event` when a COLD read should
        WAIT for another thread's in-flight first scan
        (DEC-SCAN-SINGLE-FLIGHT-001), or ``None`` when the cached entry should
        be served as-is (hit, throttled, or warm-in-flight). *pull_active* is
        the pull probe's verdict, taken by the caller OUTSIDE the lock
        (DEC-COMPLETENESS-PULL-DUTY-DEGRADE-001).
        """
        entry = self._entries.get(kind)
        if entry is not None and entry.generation == self._generation:
            # Clean hit — fast path, no scan. The served payload is FRESH for
            # this generation, so the honest flag reads not-degraded.
            self._duty_degraded[kind] = False
            return None
        inflight = self._refreshing.get(kind)
        if inflight is not None:
            if entry is not None:
                # Warm in-flight: another reader is already paying the scan AND
                # a prior entry exists to serve — serve it, no double-scan.
                return None
            event, owner = inflight
            if owner == threading.get_ident():
                # COLD RE-ENTRANT read — the in-flight recompute itself
                # re-entered ``get_or_refresh`` for the same kind on the SAME
                # thread (the DEC-COMPLETENESS-O1-LATENCY-001 cold-start case).
                # Waiting on our own event would self-deadlock, so recompute
                # WITHOUT re-registering: the inner scan stores an entry (and
                # releases the outer registration on completion), never a
                # `self._entries[kind]` KeyError → 500, never a hang.
                return self._generation
            # COLD cross-thread read: exactly ONE scan may be in flight
            # (DEC-SCAN-SINGLE-FLIGHT-001 — pre-fix, every cold concurrent
            # poller duplicated the whole-lake census; the 2026-07-05 restart
            # log measured SIX concurrent 58-68 min duplicate scans). Wait.
            return event
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
            if pull_active:
                # PULL duty-degrade (DEC-COMPLETENESS-PULL-DUTY-DEGRADE-001): while a
                # pull runs IN THIS PROCESS, widen the floor to
                # min(9 × the last scan's duration, 600 s) — never BELOW the existing
                # adaptive floor (max), so the non-pull path and a fast scan are
                # byte-identical. A 27 s full-lake scan then re-fires at most once
                # per ~243 s during the pull (a ≤10% duty cycle) instead of every
                # ~27 s, and the served staleness is SURFACED via duty_degraded /
                # snapshot_age_s (stamp_freshness) — bounded + visible, never silent.
                effective_floor = max(
                    effective_floor,
                    min(
                        PULL_DUTY_FLOOR_MULTIPLIER * entry.compute_duration,
                        MAX_PULL_DUTY_FLOOR_S,
                    ),
                )
            if effective_floor > 0.0 and (self._clock() - entry.computed_at) < effective_floor:
                # Dirty but within the (adaptive) floor — throttle: serve the stale
                # entry. Record honestly WHETHER a pull held it back (the operator
                # -visible duty_degraded flag; False for a plain non-pull throttle).
                self._duty_degraded[kind] = pull_active
                return None
        self._refreshing[kind] = (threading.Event(), threading.get_ident())
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
    "LAKE_UNIQUENESS",
    "MAX_ADAPTIVE_FLOOR_S",
    "MAX_PULL_DUTY_FLOOR_S",
    "OPTIONS",
    "PULL_DUTY_FLOOR_MULTIPLIER",
    "SPOT",
    "CoverageCounters",
    "CoverageCountersSnapshot",
    "Recompute",
    "SummaryCache",
    "SummaryEntryMeta",
    "SummaryPayload",
]
