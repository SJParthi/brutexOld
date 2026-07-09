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

from brutex.dashboard.universe_query import list_underlyings, universe_summary
from brutex.models import Exchange, Instrument

__all__ = ["UniverseSnapshotCache", "master_revision"]

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
    row = store.connection.execute(_REVISION_SQL).fetchone()
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
    :class:`~brutex.dashboard.movers_cache.MoversSnapshotCache`): all bookkeeping
    is serialised by one :class:`threading.Lock`; the bounded base reads and the
    classification run OUTSIDE the lock so a slow read never blocks a concurrent
    hit. Two concurrent cold misses each recompute independently and the last store
    wins — the reads are a deterministic function of the revision (idempotent,
    Golden Rule #7), so the duplicate work is harmless. A ``compute`` that raises
    (e.g. a bad ``kind``) propagates cleanly and nothing is stored.

    Returns are READ-ONLY: :meth:`summary` / :meth:`instruments` hand back the LIVE
    memoised dict / list (no defensive copy — mirrors ``MoversSnapshotCache`` /
    ``SummaryCache``). The only callers are the two routes, which pass them straight
    to ``JSONResponse`` without mutation; a caller that mutated a returned payload
    would poison the resident snapshot, so treat them as immutable.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: _Snapshot | None = None

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

    def _current(self, store: Any) -> _Snapshot:
        """Return the snapshot for the store's current revision, rebuilding on a miss.

        The three base reads run OUTSIDE the lock; the resident snapshot is then
        swapped in under the lock (last-write-wins on a concurrent miss).
        """
        revision = self._revision(store)
        with self._lock:
            snap = self._snapshot
            if snap is not None and snap.revision == revision:
                return snap
        # Miss (no snapshot, or the master moved): rebuild the base reads. These
        # are the three lean projections — never the option/future legs.
        rebuilt = _Snapshot(
            revision=revision,
            eligible=store.fno_eligible_underlyings(),
            candidates=store.list_index_equity_instruments(),
            reference=store.reference_only_security_ids(),
        )
        with self._lock:
            # Re-check under the lock: only adopt `rebuilt` if no concurrent miss
            # already installed THIS revision (keep its memoised payloads then).
            snap = self._snapshot
            if snap is not None and snap.revision == revision:
                return snap
            self._snapshot = rebuilt
            return rebuilt

    def summary(self, store: Any) -> dict[str, int]:
        """Return ``/api/v1/universe/summary``'s payload, cached per revision.

        On a hit the memoised dict is returned directly; on a miss the KPI counts
        are computed via :func:`~brutex.dashboard.universe_query.universe_summary`
        over the snapshot's candidates + eligible set and memoised. The snapshot's
        already-read ``reference`` id set is threaded so the payload carries the
        ADDITIVE ``reference_stock_count`` / ``stock_count_total`` keys the Stocks
        badge splits on (DEC-LABEL-TRUTH-001) — no extra store query, and every
        legacy key stays byte-identical.
        """
        snap = self._current(store)
        with self._lock:
            if snap.summary is not None:
                return snap.summary
        value = universe_summary(
            snap.candidates, fno_eligible=snap.eligible, reference_only_ids=snap.reference
        )
        with self._lock:
            if snap.summary is None:
                snap.summary = value
            return snap.summary

    def instruments(self, store: Any, *, kind: str | None) -> list[dict[str, Any]]:
        """Return ``/api/v1/universe/instruments``'s rows for *kind*, cached per revision.

        On a hit the memoised row list for *kind* is returned; on a miss the rows
        are built via :func:`~brutex.dashboard.universe_query.list_underlyings`
        (which raises :class:`ValueError` for a bad *kind* — propagated, never
        cached) and memoised. ``None`` / ``"index"`` / ``"stock"`` are each cached
        independently, so the explorer's "F&O only" toggle and tab switches share
        one base read.
        """
        snap = self._current(store)
        with self._lock:
            cached = snap.instruments.get(kind)
            if cached is not None:  # values are always lists, so this means "hit".
                return cached
        rows = list_underlyings(
            snap.candidates,
            kind=kind,
            fno_eligible=snap.eligible,
            reference_only_ids=snap.reference,
        )
        with self._lock:
            return snap.instruments.setdefault(kind, rows)
