"""Content-keyed **universe snapshot** cache — the `/universe` backend speed core (DEC-UNIVERSE-BACKEND-CACHE-001).

The ``/universe`` (F&O Explorer) page answers two read questions on every visit:

* ``GET /api/v1/universe/summary`` — the KPI band (index / stock / total counts).
* ``GET /api/v1/universe/instruments?kind=index|stock`` — the explorer rows.

Both are PURE functions of the persisted instrument master: each route reads the
three lean store projections —
:meth:`~brutex.data.instrument_store.InstrumentStore.fno_eligible_underlyings`
(a SQL ``DISTINCT``),
:meth:`~brutex.data.instrument_store.InstrumentStore.list_index_equity_instruments`
(the ~hundreds of IDX + EQ candidate rows) and
:meth:`~brutex.data.instrument_store.InstrumentStore.reference_only_security_ids`
— then classifies them via the COMMON
:mod:`brutex.dashboard.universe_query` helpers. That base read +
classification is ``O(candidates)`` (~750 underlyings → measured ~25 ms/call) and
is RE-RUN on every poll today, even though the master changes only when a pull /
instrument load lands. The page re-polls on tab switches, the "F&O only" toggle,
search, and the 30 s coverage refresh, so the SAME deterministic scan is paid over
and over.

This module caches the base reads + the derived payloads, keyed by a **content
revision token** of the master, so a repeat poll between two loads is an
**O(1) dict handoff** instead of a re-scan + re-classification.

## Invalidation — a content-exact revision token (CLAUDE.md §11 COMMON, Golden Rule #1)

The token is ``(count(*), max(fetched_at))`` over the **``instruments``** table
itself — the EXACT table the three cached reads project from, NOT a bookkeeping
sidecar. Every
:meth:`~brutex.data.instrument_store.InstrumentStore.upsert_many` stamps every row
it writes with ``fetched_at = datetime.now(UTC)`` (the keyless ``load-instruments``
path AND every later refresh both upsert, folding in the leg-economics in the same
write). So ANY write to the master strictly advances ``max(fetched_at)`` and/or
moves ``count(*)`` → the token changes → the cache rebuilds. Keying off the
``instruments`` content directly (rather than the ``instrument_runs`` audit
sidecar, which is written by a SEPARATE ``record_run`` call and can lag an upsert)
means a write can never slip past the cache. An empty store keys at ``(0, "")`` (a
stable token), so an empty universe is cached and served just like a populated one.
The token is a ``COUNT`` + ``MAX`` aggregate over the LEAN table (~hundreds of IDX
+ EQ + reference rows — the legs are never persisted), strictly cheaper than the
``list_index_equity_instruments`` read it guards (which JOINs + builds ``Instrument``
objects), so the freshness check itself is light.

This is a STRICTLY-CONSERVATIVE freshness signal: it can only ever miss-then-
recompute (correct), never serve across a real master change (no stale illusion —
Golden Rule #1). It does NOT depend on the dashboard write-generation (the keyless
load path does not bump that generation); the table's own ``(count, max fetched_at)``
is the master's content fingerprint and the single source of truth here. The one
honestly-stated residual: two DISTINCT-content writes within the SAME microsecond
that ALSO preserve the exact row count would share a token — impossible for a
click-driven dashboard fed by network pulls seconds apart, and a *re-read* (the
conservative direction), never a corruption.

## Honest envelope (Golden Rule #10 — stated, not hidden)

* A cached **hit** (the dominant interaction between two loads) is **O(1)** in the
  universe size — a single cheap token query (COUNT+MAX over the tiny runs table)
  plus a dict handoff under one lock, independent of how many underlyings the
  master holds. The bench ``tests/bench/test_universe_cache_o1.py`` pins this flat
  as the universe grows.
* The first read after a load (a **miss**) still pays the real ``O(candidates)``
  base read + classification — this is NOT claimed away. The cache bounds how
  OFTEN that cost is paid (once per revision), it does not make the scan itself
  O(1).
* Memory is **O(candidates)** for ONE resident snapshot — the cache holds exactly
  the current revision's reads + derived payloads and drops the prior revision
  wholesale on a token change, so a long session never accumulates snapshots.

No module-global state (CLAUDE.md §4): the cache is instantiated in
:func:`brutex.dashboard.app.build_app` and closed over by the
``/api/v1/universe/summary`` + ``/api/v1/universe/instruments`` routes. The
brute-force ENGINE is untouched — this is a read-only dashboard accelerator.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Final

import duckdb
import structlog

from brutex.dashboard.universe_query import list_underlyings, universe_summary
from brutex.models import Exchange, Instrument

__all__ = [
    "STORE_FAULT_TYPES",
    "UniverseSnapshotCache",
    "UniverseStoreOutageError",
    "coverage_revision",
    "master_revision",
]

_LOG = structlog.get_logger(__name__)

#: The STORE-fault class (DEC-CACHE-SERVE-LAST-KNOWN-001, the 8th keystone site):
#: the exceptions a wedged/unreachable instrument store raises from the revision
#: probe or the three base reads — ``duckdb.Error`` (every DuckDB exception; NONE
#: of them subclasses ``ValueError``, scout-verified, so the routes' bad-``kind``
#: 400 arm is unaffected) plus ``OSError`` (a stalled volume). Deliberately NARROW:
#: a genuine code bug (``ValueError``/``KeyError``/…) still propagates raw — never
#: silently swallowed (the ``movers_read_skip_errors`` posture). ONE source of
#: truth (CLAUDE.md §11 COMMON) — ``app.py``'s nav-latency probe guard imports it.
STORE_FAULT_TYPES: Final[tuple[type[BaseException], ...]] = (duckdb.Error, OSError)


class UniverseStoreOutageError(RuntimeError):
    """The instrument store is unreachable AND no last-known snapshot is resident.

    DEC-CACHE-SERVE-LAST-KNOWN-001: raised by
    :meth:`UniverseSnapshotCache._current` when a store fault
    (:data:`STORE_FAULT_TYPES`) hits the revision probe or the base reads on a
    COLD cache — there is nothing last-known to serve, so the caller must map
    this to an HONEST degrade payload (the ``store_error_payload`` precedent:
    explicit error markers + 503, never a raw 500, never a fabricated empty
    universe). Always ``raise … from exc`` — the original store fault stays
    chained for server-side diagnosis; the wire never carries ``str(exc)``.
    """


#: The content-revision probe: the row-count + newest write time over the
#: ``instruments`` table — the exact table the cached reads project from. A COUNT +
#: MAX aggregate over the LEAN row set (the legs are never persisted), content-exact
#: because every ``upsert_many`` stamps a fresh ``fetched_at`` on every written row,
#: so any master change moves ``max(fetched_at)`` and/or ``count(*)``.
_REVISION_SQL: Final[str] = "SELECT count(*), max(fetched_at) FROM instruments"


def master_revision(store: Any) -> tuple[int, str]:
    """Probe the instrument master's content-revision token — ``(count, str(max_fetched_at))``.

    The ONE source of truth (CLAUDE.md §11 COMMON) for the instrument-master content
    fingerprint: the row-count + newest ``fetched_at`` over the ``instruments`` table —
    the exact table the cached ``/universe`` reads project from. An empty store returns
    ``(0, None)`` from the SQL, normalised to ``(0, "")`` — a stable token, so an empty
    universe keys deterministically. The timestamp is stringified so the token is a
    hashable, comparable value independent of the DuckDB row type. Read on the store's
    READ cursor (``store.connection``), which DuckDB MVCC keeps consistent with the
    writer's committed snapshot (the token advances right after a committed
    ``upsert_many``).

    A keyless ``load-instruments`` populates the master WITHOUT bumping the dashboard
    write-generation, so any cache whose payload depends on the instrument master (the
    ``/coverage`` grid, the ``/universe`` completeness + mapping-integrity reports) MUST
    fold this token into its key alongside the generation, or it would serve pre-load
    rows after an instrument refresh (DEC-DASHBOARD-NAV-LATENCY-001).

    Parameters
    ----------
    store
        Anything exposing a DuckDB-style ``connection`` with ``execute(...).fetchone()``.

    Returns
    -------
    tuple[int, str]
        ``(count, str(max_fetched_at))``; ``(0, "")`` for an empty / never-loaded store.
    """
    return _revision_token(store, _REVISION_SQL)


#: The coverage-ledger content-revision probe — the ``master_revision`` twin over
#: the ``coverage_ledger`` table (DEC-STALENESS-CACHE-TRUTH-001, MIGRATION.md
#: #1660). Content-exact for the same reason: every ``upsert_coverage`` writes a
#: fresh ``last_fetched_at`` on the cell it replaces, so any coverage write moves
#: ``max(last_fetched_at)`` and/or ``count(*)``. A COUNT + MAX aggregate over the
#: PK-indexed ~universe×years table (at most a few thousand rows) — strictly
#: lighter than the coverage/mapping-integrity projections it guards.
_COVERAGE_REVISION_SQL: Final[str] = "SELECT count(*), max(last_fetched_at) FROM coverage_ledger"


def coverage_revision(store: Any) -> tuple[int, str]:
    """Probe the coverage-ledger's content-revision token — ``(count, str(max_last_fetched_at))``.

    The exact :func:`master_revision` TWIN over the ``coverage_ledger`` table
    (DEC-STALENESS-CACHE-TRUTH-001): the row-count + newest ``last_fetched_at``
    over the table :meth:`~brutex.data.instrument_store.InstrumentStore.upsert_coverage`
    writes. An empty ledger normalises to the stable ``(0, "")`` token, exactly
    like an empty master. Read on the store's READ cursor (``store.connection``) —
    DuckDB MVCC keeps it consistent with the writer's committed snapshot, so the
    token advances right after a committed ``upsert_coverage``, INCLUDING one from
    another process sharing the same ``instruments.duckdb`` (the cross-process
    freshness signal a ``mark_dirty`` hook can never provide — the #1149
    content-token doctrine).

    A metadata (expiries/contracts) pull writes coverage cells WITHOUT bumping the
    dashboard write-generation and WITHOUT touching the ``instruments`` table or
    the window-ledger Parquet, so any cache whose payload depends on the coverage
    ledger (the ``/coverage`` grid, the ``/universe`` completeness +
    mapping-integrity reports) MUST fold this token into its key alongside the
    generation + :func:`master_revision` — the ``_nav_latency_key`` 4th component
    (DEC-STALENESS-CACHE-TRUTH-001, closing the mid-metadata-phase +
    out-of-process stale-serve gap of DEC-DASHBOARD-NAV-LATENCY-001).

    Parameters
    ----------
    store
        Anything exposing a DuckDB-style ``connection`` with ``execute(...).fetchone()``.

    Returns
    -------
    tuple[int, str]
        ``(count, str(max_last_fetched_at))``; ``(0, "")`` for an empty ledger.
    """
    return _revision_token(store, _COVERAGE_REVISION_SQL)


def _revision_token(store: Any, sql: str) -> tuple[int, str]:
    """Run one ``(COUNT, MAX)`` revision probe and normalise it to ``(int, str)``.

    The ONE shared normalisation (CLAUDE.md §11 COMMON) behind
    :func:`master_revision` and :func:`coverage_revision`: an empty table (or a
    paranoid ``None`` row) keys at the stable ``(0, "")``; the timestamp is
    stringified so the token is hashable + comparable independent of the DuckDB
    row type.
    """
    row = store.connection.execute(sql).fetchone()
    if row is None:  # defensive: COUNT(*) always returns a row, but never trust it.
        return (0, "")
    count = int(row[0]) if row[0] is not None else 0
    latest = row[1]
    return (count, "" if latest is None else str(latest))


@dataclass(slots=True)
class _Snapshot:
    """One revision's base reads + lazily-derived payloads.

    ``eligible`` / ``candidates`` / ``reference`` are the three store projections,
    read ONCE per revision. ``summary`` and the per-``kind`` ``instruments`` entries
    are derived from them on first request and memoised here, all guarded by the
    cache lock. A ``kind`` the classifier rejects raises before it is stored, so a
    bad request never poisons the snapshot.
    """

    revision: tuple[int, str]
    eligible: frozenset[tuple[Exchange, str]]
    candidates: list[Instrument]
    reference: frozenset[int]
    summary: dict[str, int] | None = None
    #: Keyed by the explorer ``kind`` (``None`` / ``"index"`` / ``"stock"``);
    #: each value is the ``list_underlyings`` row list (always a list, never None,
    #: so ``.get(kind) is None`` unambiguously means "not yet computed").
    instruments: dict[str | None, list[dict[str, Any]]] = field(default_factory=dict)


class UniverseSnapshotCache:
    """Thread-safe, single-resident cache of the ``/universe`` backend reads.

    A route calls :meth:`summary` or :meth:`instruments`; both first resolve the
    current master revision (cheap token query) and rebuild the base reads only
    when the token moved. The derived payload is then memoised on the snapshot, so
    every later poll of the same revision is an O(1) hit.

    Concurrency (mirrors
    :class:`~brutex.dashboard.movers_cache.MoversSnapshotCache` /
    :meth:`~brutex.dashboard.summary_cache.SummaryCache._plan`): all bookkeeping
    is serialised by one :class:`threading.Lock`; the bounded base reads and the
    classification run OUTSIDE the lock so a slow read never blocks a concurrent
    hit. Cold misses are SINGLE-FLIGHTED per revision
    (DEC-DASHBOARD-EVENTLOOP-OFFLOAD-002): exactly ONE reader computes a given
    revision's three base reads while every concurrent cold reader WAITS on its
    event and then serves the published snapshot, so N simultaneous cold pollers
    pay ONE scan instead of N (the ``summary``/``instruments`` reads run on a
    threadpool worker, so a stalled lake volume can pin a worker but never the
    event loop). The reads are a deterministic function of the revision
    (idempotent, Golden Rule #7). A non-store-fault ``compute`` raise (a genuine
    code bug) releases the single-flight event and propagates RAW — nothing is
    stored and no waiter is left hanging; a bad ``kind`` (a pure post-``_current``
    classifier error) likewise propagates and nothing is cached. A STORE fault
    (:data:`STORE_FAULT_TYPES` — a wedged volume / unreachable DuckDB) from the
    revision probe OR the base reads instead serves the RESIDENT snapshot
    last-known (flagged to the caller, WARNed once per outage, recovered with one
    INFO) or raises the typed :class:`UniverseStoreOutageError` on a cold cache
    (DEC-CACHE-SERVE-LAST-KNOWN-001 — the pre-fix probe-BEFORE-snapshot ordering
    made serve-last-known impossible by construction).

    Returns are READ-ONLY: :meth:`summary` / :meth:`instruments` hand back the LIVE
    memoised dict / list (no defensive copy — mirrors ``MoversSnapshotCache`` /
    ``SummaryCache``). The only callers are the two routes, which pass them straight
    to ``JSONResponse`` without mutation; a caller that mutated a returned payload
    would poison the resident snapshot, so treat them as immutable.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: _Snapshot | None = None
        #: In-flight base-read events keyed by the revision being computed
        #: (DEC-DASHBOARD-EVENTLOOP-OFFLOAD-002 single-flight). The FIRST cold
        #: miss for a revision registers its event + computes the three base
        #: reads; concurrent cold misses for the SAME revision wait on it, then
        #: serve the published snapshot, so N cold pollers pay ONE scan. The
        #: entry is popped by the leader on both the success and the failure path.
        self._refreshing: dict[tuple[int, str], threading.Event] = {}
        #: The once-per-outage WARN latch (DEC-CACHE-SERVE-LAST-KNOWN-001 — the
        #: ``movers_cache``/``rate_bridge_cache`` ``_degraded`` posture): the FIRST
        #: store-fault serve/raise of an outage WARNs; repeats stay silent until
        #: the next fully-successful serve logs the recovery INFO and re-arms it.
        #: One instance-wide flag (this cache fronts exactly ONE store).
        self._outage_latched: bool = False

    def _latch_outage(self, *, served: str, error: BaseException) -> None:
        """WARN once per outage flip (repeats silent until :meth:`_note_recovered`).

        *served* names the degrade arm (``"last_known"`` — a resident snapshot
        was served labelled — or ``"cold_fault"`` — nothing resident, the typed
        :class:`UniverseStoreOutageError` is about to raise). The full store
        fault detail is logged HERE, server-side only (the wire carries stable
        markers, never ``str(exc)`` — the ``store_error_payload`` sanitization
        law).
        """
        with self._lock:
            first_flip = not self._outage_latched
            self._outage_latched = True
        if first_flip:
            _LOG.warning(
                "universe_cache.store_probe_outage",
                served=served,
                error=str(error),
                error_type=type(error).__name__,
            )

    def _note_recovered(self) -> None:
        """One INFO per outage→recovery flip (called on every fully-good serve)."""
        with self._lock:
            recovered = self._outage_latched
            self._outage_latched = False
        if recovered:
            _LOG.info("universe_cache.store_probe_recovered")

    def size(self) -> int:
        """``1`` if a revision is currently resident, else ``0`` — O(1)."""
        with self._lock:
            return 0 if self._snapshot is None else 1

    @staticmethod
    def _revision(store: Any) -> tuple[int, str]:
        """Probe the master's content-revision token — delegates to :func:`master_revision`.

        Thin wrapper over the module-level :func:`master_revision` so the master
        content fingerprint lives in exactly ONE place (CLAUDE.md §11 COMMON); the
        ``/coverage`` + ``/universe`` latency caches reuse the SAME helper.
        """
        return master_revision(store)

    def _serve_last_known_or_raise(self, exc: BaseException, *, where: str) -> _Snapshot:
        """The store-fault degrade arm: resident snapshot → serve it, else typed raise.

        DEC-CACHE-SERVE-LAST-KNOWN-001 (the 8th keystone site): a store fault
        (:data:`STORE_FAULT_TYPES`) from the revision probe or the base reads
        serves the RESIDENT snapshot last-known (the caller labels the payload —
        never a silent stale serve, Golden Rule #1) with one latched WARN; a COLD
        cache raises the typed :class:`UniverseStoreOutageError` (chained) the
        routes map to an honest 503 degrade — never a raw 500, never a fabricated
        empty universe.
        """
        with self._lock:
            snap = self._snapshot
        if snap is not None:
            self._latch_outage(served="last_known", error=exc)
            return snap
        self._latch_outage(served="cold_fault", error=exc)
        raise UniverseStoreOutageError(
            f"instrument store unreachable at the {where} with no resident snapshot"
        ) from exc

    def _current(self, store: Any) -> tuple[_Snapshot, bool]:
        """Return ``(snapshot, served_last_known)`` for the store's current revision.

        The cheap revision token is probed FIRST — but a store fault there no
        longer makes serve-last-known impossible by construction (the pre-fix
        probe-before-snapshot ordering, DEC-CACHE-SERVE-LAST-KNOWN-001): a
        :data:`STORE_FAULT_TYPES` fault from the probe OR the base-read rebuild
        serves the RESIDENT snapshot as ``(snap, True)`` (the caller labels it)
        with one latched WARN, and raises the typed
        :class:`UniverseStoreOutageError` when the cache is COLD. The healthy
        path is byte-identical to the pre-fix behaviour and returns
        ``(snap, False)`` (logging the one recovery INFO when a prior outage was
        latched).

        On a healthy miss the three base reads run OUTSIDE the lock behind a
        per-revision single-flight (DEC-DASHBOARD-EVENTLOOP-OFFLOAD-002): exactly
        ONE cold reader computes a given revision while concurrent cold readers
        WAIT on its event and then serve the published snapshot, so N
        simultaneous cold pollers pay ONE scan instead of N (mirrors
        :meth:`SummaryCache._plan`'s cold single-flight). A non-store-fault
        ``compute`` raise (a genuine code bug) releases the event and propagates
        RAW — nothing is stored and no waiter is left hanging.
        """
        try:
            revision = self._revision(store)
        except STORE_FAULT_TYPES as exc:
            return self._serve_last_known_or_raise(exc, where="revision probe"), True
        while True:
            with self._lock:
                snap = self._snapshot
                if snap is not None and snap.revision == revision:
                    # Hit — also the wake path for a follower once the leader
                    # published THIS revision. An O(1) dict handoff (the recovery
                    # INFO logs OUTSIDE the lock, below the loop).
                    break
                event = self._refreshing.get(revision)
                if event is None:
                    # We are the cold leader for this revision: register the event
                    # (so concurrent readers wait) and compute below.
                    event = threading.Event()
                    self._refreshing[revision] = event
                    leader = True
                else:
                    leader = False
            if not leader:
                # Cold follower: another reader is already computing THIS revision.
                # Wait for it, then re-loop to serve its published snapshot (the hit
                # branch above); a mid-flight revision change re-elects a leader.
                event.wait()
                continue
            # Cold leader: rebuild the three lean base reads OUTSIDE the lock (never
            # the option/future legs), publish under the lock, and ALWAYS release the
            # single-flight event so waiters wake — on success (they then serve the
            # fresh snapshot) or on failure (they retry).
            published = False
            try:
                try:
                    rebuilt = _Snapshot(
                        revision=revision,
                        eligible=store.fno_eligible_underlyings(),
                        candidates=store.list_index_equity_instruments(),
                        reference=store.reference_only_security_ids(),
                    )
                except STORE_FAULT_TYPES as exc:
                    # Partial outage (probe answered, base read wedged): the SAME
                    # degrade arm. `published` stays False, so the finally below
                    # pops the registration + wakes waiters — each retries and
                    # lands here too (bounded by the concurrent poller count).
                    return self._serve_last_known_or_raise(exc, where="base read"), True
                with self._lock:
                    # Re-check under the lock: only adopt `rebuilt` if no concurrent
                    # miss already installed THIS revision (keep its memoised payloads
                    # then — belt-and-suspenders beside the single-flight).
                    snap = self._snapshot
                    if snap is not None and snap.revision == revision:
                        self._refreshing.pop(revision, None)
                        published = True
                        adopted = snap
                    else:
                        self._snapshot = rebuilt
                        self._refreshing.pop(revision, None)
                        published = True
                        adopted = rebuilt
                self._note_recovered()
                return adopted, False
            finally:
                if not published:
                    # The compute raised — drop the registration so a waiter (or the
                    # next caller) can retry this revision instead of hanging.
                    with self._lock:
                        self._refreshing.pop(revision, None)
                event.set()
        # The ONLY loop exit is the hit `break` above — `snap` is the resident
        # snapshot for THIS revision (leader/follower paths return inside).
        self._note_recovered()
        return snap, False

    def summary_view(self, store: Any) -> tuple[dict[str, int], bool]:
        """Return ``(summary payload, served_last_known)``, cached per revision.

        On a hit the memoised dict is returned directly; on a miss the KPI counts
        are computed via :func:`~brutex.dashboard.universe_query.universe_summary`
        over the snapshot's candidates + eligible set and memoised. The snapshot's
        already-read ``reference`` id set is threaded so the payload carries the
        ADDITIVE ``reference_stock_count`` / ``stock_count_total`` keys the Stocks
        badge splits on (DEC-LABEL-TRUTH-001) — no extra store query, and every
        legacy key stays byte-identical.
        ``served_last_known`` is ``True`` iff a store OUTAGE served the resident
        snapshot (DEC-CACHE-SERVE-LAST-KNOWN-001) — the route MUST label the
        payload (the derivation itself is pure over the resident snapshot, so an
        outage serve still memoises correctly); a COLD outage raises
        :class:`UniverseStoreOutageError` out of :meth:`_current`.
        """
        snap, served_last_known = self._current(store)
        with self._lock:
            if snap.summary is not None:
                return snap.summary, served_last_known
        value = universe_summary(
            snap.candidates, fno_eligible=snap.eligible, reference_only_ids=snap.reference
        )
        with self._lock:
            if snap.summary is None:
                snap.summary = value
            return snap.summary, served_last_known

    def summary(self, store: Any) -> dict[str, int]:
        """Back-compat :meth:`summary_view` delegator (payload only, flag dropped)."""
        return self.summary_view(store)[0]

    def instruments_view(
        self, store: Any, *, kind: str | None
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return ``(rows for kind, served_last_known)``, cached per revision.

        On a hit the memoised row list for *kind* is returned; on a miss the rows
        are built via :func:`~brutex.dashboard.universe_query.list_underlyings`
        (which raises :class:`ValueError` for a bad *kind* — propagated, never
        cached) and memoised. ``None`` / ``"index"`` / ``"stock"`` are each cached
        independently, so the explorer's "F&O only" toggle and tab switches share
        one base read. ``served_last_known`` is ``True`` iff a store OUTAGE served
        the resident snapshot (DEC-CACHE-SERVE-LAST-KNOWN-001) — the route MUST
        label its envelope; a COLD outage raises
        :class:`UniverseStoreOutageError` out of :meth:`_current`.
        """
        snap, served_last_known = self._current(store)
        with self._lock:
            cached = snap.instruments.get(kind)
            if cached is not None:  # values are always lists, so this means "hit".
                return cached, served_last_known
        rows = list_underlyings(
            snap.candidates,
            kind=kind,
            fno_eligible=snap.eligible,
            reference_only_ids=snap.reference,
        )
        with self._lock:
            return snap.instruments.setdefault(kind, rows), served_last_known

    def instruments(self, store: Any, *, kind: str | None) -> list[dict[str, Any]]:
        """Back-compat :meth:`instruments_view` delegator (rows only, flag dropped)."""
        return self.instruments_view(store, kind=kind)[0]
