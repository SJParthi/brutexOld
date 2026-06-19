"""The unified per-window ledger — ONE source of truth for skip + retry + visibility.

DEC-WINDOW-LEDGER-001. The pull once kept the per-window state in TWO stores that
had to agree but were wired so the WRONG one drove the skip:

* the legacy ``FetchCoverage`` — a flat *set* of completed
  windows (RETIRED in Stage e). This was what the ingest probed to skip a vendor
  call, but it was populated COARSELY (the dataset min/max span enclosing a
  window), so an internal-gap ``FAILED`` window was marked "complete" and skipped
  forever (the #598 bug).
* :class:`~brutex.data.window_status.WindowStatusLog` — the PRECISE
  ``SUCCESS``/``EMPTY``/``FAILED`` record. Visibility only; it does not drive the
  skip.

This module is the unification: ONE ledger that records EVERY planned window
(``PLANNED`` at pull start, then the landed outcome) and IS the authority for the
skip decision — a window is skipped **only** if its row is settled
(``SUCCESS``/``SKIPPED``); any ``FAILED``/``PLANNED``/``EMPTY``-below-threshold or
MISSING window is fetched. So a window can never be silently masked: the skip
falls out of the primary record instead of a coarse derived set patched every
pull (#597/#598). "Every planned window" means every window of the CURRENT
pull's tiling: a SUPERSEDED trailing row — one whose key the advancing ``end``
re-tiled away, so the tiler can never emit it again for a same-or-longer range —
is pruned at plan time (:meth:`WindowLedger.prune_superseded`,
DEC-WINDOW-TRAILING-GC-001), not kept as a permanent orphan.

This file is the PURE, in-memory model (PR-1 of the staged rollout): the durable
DuckDB/Parquet backing + the pull wiring land in later slices
(``window_ledger_store`` / the ingest switch). Every mutate / read is **O(1)**
under one lock (DEC-DHAN-PARALLEL-INGEST-001 — one ledger is threaded into every
parallel window worker); the snapshot is O(N) over the recorded windows.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_status import EMPTY, FAILED, SUCCESS, classify_window_status

__all__ = [
    "EMPTY",
    "EMPTY_TERMINAL_THRESHOLD",
    "FAILED",
    "PLANNED",
    "SETTLED_STATUSES",
    "SKIPPED",
    "SUCCESS",
    "LedgerProgress",
    "WindowLedger",
    "WindowLedgerRecord",
]

#: A window the tiler will attempt this pull — upserted at pull start, no outcome
#: yet. Never skippable (it has not been fetched), so a PLANNED-but-never-recorded
#: window is always re-attempted (the no-mask guarantee).
PLANNED: str = "PLANNED"
#: A SETTLED window that must NOT be re-fetched even though it is not literally
#: ``SUCCESS``: an EMPTY seen ``EMPTY_TERMINAL_THRESHOLD`` times (genuinely empty,
#: folds DEC-EMPTY-TERMINAL-001) or a ``DH-905`` known-gap (folds
#: DEC-DHAN-NODATA-SETTLED-001 #597). A distinct status (not overloaded onto
#: ``EMPTY``) keeps the skip rule a pure status check and the table honest on /db.
SKIPPED: str = "SKIPPED"

#: A window seen genuinely ``EMPTY`` on this many SEPARATE pulls is treated as
#: terminal (``SKIPPED``) so it stops costing a re-fetch every run — the canonical
#: home of the DEC-EMPTY-TERMINAL-001 threshold (``coverage_reconcile`` migrates to
#: import this in the wiring slice). Three attempts give a transient empty (a
#: load-induced ``{}`` that later fills) a few chances first.
EMPTY_TERMINAL_THRESHOLD: int = 3

#: The two settled statuses — a window in either is skipped on the next pull.
_SKIPPABLE: frozenset[str] = frozenset({SUCCESS, SKIPPED})

#: PUBLIC alias of the settled set (DEC-SPOT-COVERAGE-COMPLETENESS-001): read-only
#: consumers (the /coverage spot completeness rollup) classify "settled vs still
#: outstanding" with the LEDGER'S OWN skip authority instead of minting another
#: hand-typed status list — anything NOT in this set (and not FAILED) is pending
#: work the next pull re-fetches.
SETTLED_STATUSES: frozenset[str] = _SKIPPABLE

#: REPORTING projection (DEC-EMPTY-RESOLVED-REPORTING-001) — DELIBERATELY WIDER than
#: the skip set :data:`SETTLED_STATUSES`. A window is RESOLVED for completion/progress
#: reporting once it has a terminal answer: ``SUCCESS`` / ``SKIPPED`` OR a genuine
#: ``EMPTY`` (no data this pull). EMPTY is added HERE but NOT to the skip set: a
#: below-threshold EMPTY is still re-fetched next pull (no data is ever masked — the
#: skip authority is unchanged), yet it must NOT count as "pending work" in the
#: completion headline, because on a wholly-PAST date range an empty window can never
#: gain data, so "re-pull to finish" would be dishonest. This asymmetry — the
#: completion authority being wider than the skip authority — IS the fix; do not
#: collapse the two sets.
RESOLVED_STATUSES: frozenset[str] = frozenset({SUCCESS, SKIPPED, EMPTY})


@dataclass(frozen=True, slots=True)
class LedgerProgress:
    """A live, O(N) progress snapshot of the ledger — the spot pull's per-window bar source.

    DEC-SPOT-WINDOW-PROGRESS-001. The bulk spot pull used to advance the dashboard bar
    + flush the /coverage view ONLY when a whole ``(symbol, interval)`` pair finished its
    entire multi-year backfill (``run_ingest`` fires ``on_progress`` once per pair). With
    a modest pool sharing one adaptively-stepped-down rate bucket a wave of symbols takes
    minutes, so BOTH surfaces sat frozen for minutes mid-pull — the operator-observed
    "stuck at 24/779" even though windows were landing the whole time. This snapshot lets
    the dashboard drive the bar from the WINDOW-level ledger on a ~1 s cadence instead
    (the spot twin of the metadata pull's per-call tick).

    Every field is derived from the AUTHORITATIVE per-window records, so a repeated poll is
    **idempotent** — it can never double-count a window a retry round re-touches (unlike a
    naive per-fire increment): ``settled`` / ``failed`` / ``bars`` are absolute folds over
    the live records, not running tallies.
    """

    #: Distinct windows recorded so far (the bar's denominator — the full planned tiling).
    total: int
    #: Windows in a SETTLED+SKIP status (``SUCCESS`` / ``SKIPPED``) — the SKIP authority's
    #: count (same :data:`SETTLED_STATUSES`). NOT the bar numerator anymore — that is
    #: :attr:`resolved` (DEC-EMPTY-RESOLVED-REPORTING-001); kept for skip-authority readers.
    settled: int
    #: Windows with a TERMINAL answer for REPORTING — ``SUCCESS`` / ``SKIPPED`` / ``EMPTY``
    #: (:data:`RESOLVED_STATUSES`). The progress-bar NUMERATOR: a genuine EMPTY (no data
    #: this pull) is resolved, not "pending" — so a complete historical pull reads ~100%
    #: instead of crying "re-pull to finish" (DEC-EMPTY-RESOLVED-REPORTING-001).
    resolved: int
    #: Windows that returned a genuine ``EMPTY`` (no data) — surfaced as its OWN honest
    #: bucket (a subset of :attr:`resolved`), never folded into "pending".
    no_data: int
    #: Windows still ``PLANNED`` (never fetched) — the genuinely-outstanding count a
    #: re-pull WILL attempt. Together with :attr:`failed` this is the only "re-pull helps"
    #: work; EMPTY is excluded.
    planned: int
    #: Windows currently in the ``FAILED`` (transient) status — the live "failed" count.
    failed: int
    #: Sum of ``bars`` across every recorded window (the live lake-bar total for the pull's
    #: selection; on a re-pull this reflects the already-stored rows the skip authority keeps).
    bars: int
    #: ``"<symbol> <interval>"`` of the most-recently-ATTEMPTED window (max ``last_attempt_at``)
    #: — the live "current" label, so the bar names the symbol being fetched RIGHT NOW even
    #: while a long backfill is mid-flight. Empty string when nothing has been attempted yet.
    current: str


@dataclass(frozen=True, slots=True)
class WindowLedgerRecord:
    """One window's full record — the unit of the browsable ledger table.

    Immutable. ``security_id`` is the engine ``symbol_name`` (the same field
    :class:`~brutex.data.fetch_coverage.WindowKey` uses), so a row keys to the
    exact window the ingest skip-probe tests. ``first_seen_at`` is stamped once on
    the first ``PLANNED`` upsert and never overwritten; ``last_attempt_at`` updates
    on every recorded outcome (``None`` until the first attempt lands).
    """

    market: str
    security_id: str
    interval: str
    window_start: str  # ISO YYYY-MM-DD (serialisation-friendly)
    window_end: str
    status: str
    bars: int
    attempts: int
    last_error: str | None
    first_seen_at: str  # ISO-8601 timestamp
    last_attempt_at: str | None  # ISO-8601 timestamp, None until first attempt
    #: Genuine-``EMPTY`` observations counted toward the terminal promotion
    #: (DEC-WINDOW-LEDGER-001 R1 + DEC-EMPTY-STREAK-RATELIMIT-001 R2). Bumped on an
    #: ``EMPTY`` outcome; RESET to 0 only by a ``SUCCESS`` (the window has data);
    #: PRESERVED across a ``FAILED`` (a 429 / timeout is NOT evidence of data, so it
    #: must not sabotage convergence under a rate-limit storm — the old reset-on-FAILED
    #: was why a pre-listing window never settled and the full pull hung "pending").
    #: The EMPTY→``SKIPPED`` promotion fires on this count, NOT on total ``attempts``
    #: (which FAILED retries inflate), so a real-data window is never masked before
    #: ``_threshold`` genuine empties.
    empty_streak: int = 0
    #: Vendor fetch calls made for this window during its MOST-RECENT pull
    #: (DEC-WINDOW-INPULL-FETCHES-001). ``attempts`` counts *pulls* (one per
    #: :meth:`record`); this counts the *network fetches inside one pull* — the
    #: in-pull empty/transient retry (DEC-EMPTY-INPULL-RETRY-001) re-fetches a clean
    #: 0-bar window up to 3× WITHIN a single pull, and that count was previously only
    #: logged at DEBUG, so the operator saw ``attempts 1`` with NO sign the 3× retry
    #: fired. ``1`` = a first-try win; ``>1`` = the retry fired (up to
    #: ``empty_retry_attempts``); ``0`` = a never-fetched ``PLANNED`` row. OVERWRITTEN
    #: each pull (the last pull's count, like ``last_error`` / ``last_attempt_at``) —
    #: NOT cumulative, so a re-run of the SAME pull is bit-identical (Golden Rule #7).
    in_pull_fetches: int = 0


@dataclass(slots=True)
class WindowLedger:
    """An O(1) per-window ledger — the single source of truth for skip + retry.

    Thread-safe (DEC-DHAN-PARALLEL-INGEST-001): one ledger is threaded into every
    parallel ``(instrument, interval)`` worker, so :meth:`plan` / :meth:`record` /
    :meth:`settle_known_gap` are called concurrently. A :class:`threading.Lock`
    guards the map so two workers stamping different windows never tear it; the
    lock is held only for the O(1) dict op, never across I/O. ``attempts``
    accumulates across re-pulls, giving the cross-run history the flat skip-set
    cannot.
    """

    _by_window: dict[tuple[str, str, str, str, str], WindowLedgerRecord] = field(
        default_factory=dict
    )
    _threshold: int = EMPTY_TERMINAL_THRESHOLD
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @staticmethod
    def _key(key: WindowKey) -> tuple[str, str, str, str, str]:
        """The 5-tuple identity of a window (market, symbol, interval, start, end)."""
        return (
            key.market,
            key.security_id,
            key.interval,
            key.start.isoformat(),
            key.end.isoformat(),
        )

    def plan(self, key: WindowKey, *, now: datetime) -> None:
        """Upsert *key* as ``PLANNED`` if absent — **O(1)**, atomic, idempotent.

        Called once per window at pull start. If the window is already recorded
        (``SUCCESS`` / ``FAILED`` / ``EMPTY`` / ``SKIPPED`` / ``PLANNED``) this is a
        **no-op** — it must NEVER reset a settled row's status or attempts, or every
        pull would re-fetch everything. A genuinely-new window is inserted with
        ``first_seen_at = now`` and no outcome yet.
        """
        wk = self._key(key)
        with self._lock:
            if wk in self._by_window:
                return
            self._by_window[wk] = WindowLedgerRecord(
                market=key.market,
                security_id=key.security_id,
                interval=key.interval,
                window_start=key.start.isoformat(),
                window_end=key.end.isoformat(),
                status=PLANNED,
                bars=0,
                attempts=0,
                last_error=None,
                first_seen_at=now.isoformat(),
                last_attempt_at=None,
                empty_streak=0,
                in_pull_fetches=0,  # never fetched — a pure PLANNED row
            )

    def seed_lake_success(self, key: WindowKey, *, bars: int, now: datetime) -> bool:
        """Seed *key* as ``SUCCESS`` ONLY if it is ABSENT — the lake cold-start — **O(1)**, atomic.

        Cold-start bridge (DEC-WINDOW-LEDGER-001): on a fresh clone / lost ledger,
        the lake's Parquet already holds bars but the ledger has no row, so a re-pull
        would re-fetch everything (the old ``reconcile_coverage_with_lake`` rescue).
        This seeds a ``SUCCESS`` row for a window the lake proves it already has —
        but ONLY when the window is genuinely ABSENT. A window with ANY existing row
        (``FAILED`` / ``PLANNED`` / ``EMPTY`` / ``SKIPPED`` / ``SUCCESS``) is left
        untouched, so this can NEVER mask a FAILED window that needs a re-pull or
        overwrite a settled state. Returns ``True`` iff a row was newly seeded.
        """
        wk = self._key(key)
        with self._lock:
            if wk in self._by_window:
                return False
            self._by_window[wk] = WindowLedgerRecord(
                market=key.market,
                security_id=key.security_id,
                interval=key.interval,
                window_start=key.start.isoformat(),
                window_end=key.end.isoformat(),
                status=SUCCESS,
                bars=bars,
                attempts=1,
                last_error=None,
                first_seen_at=now.isoformat(),
                last_attempt_at=now.isoformat(),
                empty_streak=0,
                in_pull_fetches=1,  # the lake proves one successful fetch's data
            )
            return True

    def is_skippable(self, key: WindowKey) -> bool:
        """Return ``True`` iff *key* is SETTLED (``SUCCESS`` / ``SKIPPED``) — **O(1)**, atomic.

        The single skip rule: a window is skipped on a re-pull ONLY when its row is
        settled. A ``FAILED`` / ``PLANNED`` / ``EMPTY``-below-threshold row, or a
        window with no row at all, is NOT skippable → it is fetched. This is the
        no-mask guarantee (#598) expressed in the primary store.
        """
        wk = self._key(key)
        with self._lock:
            record = self._by_window.get(wk)
            return record is not None and record.status in _SKIPPABLE

    def record(
        self,
        key: WindowKey,
        *,
        raw_bars: int,
        error_type: str | None,
        bars_appended: int,
        now: datetime,
        in_pull_fetches: int = 1,
        settle_empty: bool = False,
    ) -> str:
        """Record *key*'s fetch outcome; classify + bump the attempt count — **O(1)**, atomic.

        Mirrors the old ``WindowStatusLog.record`` plus the EMPTY-terminal promotion
        (DEC-EMPTY-TERMINAL-001 folded in): :func:`classify_window_status` of
        ``(raw_bars, error_type)`` gives ``SUCCESS`` / ``EMPTY`` / ``FAILED``; a
        clean ``EMPTY`` seen ``_threshold`` times (across re-pulls) is promoted to
        ``SKIPPED`` so it stops re-fetching forever. ``first_seen_at`` is preserved
        from the prior row (or stamped ``now`` for a never-planned window).
        Returns the final stored status.

        *in_pull_fetches* (DEC-WINDOW-INPULL-FETCHES-001) is the number of vendor
        fetch calls the spine made for this window WITHIN this single pull — ``1`` for
        a first-try outcome, ``>1`` when the in-pull empty/transient retry fired (up to
        ``empty_retry_attempts``). It is STORED as-is (the last pull's count), not
        accumulated, so re-running the same pull is bit-identical (Golden Rule #7). The
        default ``1`` keeps every existing single-fetch caller honest. ``attempts`` still
        counts *pulls*; this exposes the *network fetches inside* one — the count that
        was previously DEBUG-only, so the operator now SEES that ``attempts 1`` already
        meant up to 3 fetches.

        *settle_empty* (DEC-OLD-GAP-FAST-SETTLE-001) is the OLD-gap fast-settle hint:
        when the classified status is ``EMPTY`` and this is ``True``, the window is
        settled ``SKIPPED`` in ONE pull (status ``SKIPPED`` + ``empty_streak`` forced to
        ``_threshold``, exactly as a normal across-pull threshold settle leaves it),
        instead of needing ``_threshold`` separate EMPTY pulls. The spine passes ``True``
        ONLY for a window that is both in-pull-confirmed empty (re-hit ≥2× this pull) AND
        OLD enough that vendor data can never appear (``window_end`` older than
        :data:`~brutex.adapters.ingest_common.OLD_GAP_HORIZON_DAYS`). When *settle_empty*
        is ``True`` but the classification is ``SUCCESS`` or ``FAILED`` it is IGNORED (a
        no-op — a success has data and is never an empty; a 429/timeout is NOT evidence
        the window is empty). The default ``False`` is byte-identical to the prior
        behaviour. It is a TRANSIENT per-call decision — NOT a stored field; the resulting
        ``SKIPPED`` status + ``empty_streak`` persist through the existing columns.
        """
        classified = classify_window_status(raw_bars, error_type)
        wk = self._key(key)
        with self._lock:
            prior = self._by_window.get(wk)
            attempts = (prior.attempts if prior is not None else 0) + 1
            # R1 (DEC-WINDOW-LEDGER-001) + R2 (DEC-EMPTY-STREAK-RATELIMIT-001):
            # promote on the genuine-EMPTY count, NOT total ``attempts``, with these
            # per-outcome streak transitions:
            #   * EMPTY   → bump (one more genuine no-data observation).
            #   * SUCCESS → reset to 0 (the window HAS data — it is not empty).
            #   * FAILED  → PRESERVE (a 429 / timeout / transport error is NOT
            #     evidence the window has data, so it must not throw away the
            #     genuine-empty progress). Resetting on FAILED was the convergence
            #     BUG: under a sustained 429 storm a pre-listing window never strung
            #     ``_threshold`` clean empties together, so it re-fetched FOREVER and
            #     the full universe pull never settled (the operator-observed "stuck").
            #     Preserving keeps the pull-until-success contract intact — a window
            #     with real data is still re-fetched every pull (FAILED and
            #     EMPTY-below-threshold are never skippable) until it returns bars; we
            #     only ever settle a window seen genuinely EMPTY ``_threshold`` times.
            prior_streak = prior.empty_streak if prior is not None else 0
            if classified == EMPTY:
                empty_streak = prior_streak + 1
            elif classified == FAILED:
                empty_streak = prior_streak  # rate-limit/timeout — preserve, never reset
            else:  # SUCCESS — the window has real data
                empty_streak = 0
            status = classified
            if classified == EMPTY and settle_empty:
                # OLD-gap fast-settle (DEC-OLD-GAP-FAST-SETTLE-001): an in-pull-confirmed
                # empty window OLD enough that vendor data can never appear is a PERMANENT
                # gap — settle it SKIPPED in ONE pull, forcing the streak to the threshold
                # so the persisted row is identical to a normal across-pull settle. Only
                # ever reached for EMPTY (a SUCCESS/FAILED ignores the hint), so it can
                # never mask a window with data. RECENT windows (settle_empty False) keep
                # the conservative across-pull streak below.
                empty_streak = self._threshold
                status = SKIPPED
            elif classified == EMPTY and empty_streak >= self._threshold:
                status = SKIPPED  # genuinely empty _threshold CLEAN times → settle
            first_seen = prior.first_seen_at if prior is not None else now.isoformat()
            self._by_window[wk] = WindowLedgerRecord(
                market=key.market,
                security_id=key.security_id,
                interval=key.interval,
                window_start=key.start.isoformat(),
                window_end=key.end.isoformat(),
                status=status,
                bars=bars_appended,
                attempts=attempts,
                last_error=error_type,
                first_seen_at=first_seen,
                last_attempt_at=now.isoformat(),
                empty_streak=empty_streak,
                in_pull_fetches=in_pull_fetches,
            )
            return status

    def settle_known_gap(self, key: WindowKey, *, now: datetime, reason: str | None = None) -> str:
        """Settle *key* as ``SKIPPED`` — a ``DH-905`` known-gap that must NOT re-pull — **O(1)**.

        Folds DEC-DHAN-NODATA-SETTLED-001 (#597): a vendor ``DH-905`` "no data
        present" is the vendor's DEFINITIVE answer that the range has no bars, so it
        can never return a 200 — settling it ``SKIPPED`` stops it pinning the pull
        forever. A genuine retryable error is NEVER routed here (it goes through
        :meth:`record` as ``FAILED`` and keeps retrying). ``attempts`` is bumped and
        ``last_attempt_at`` stamped; ``reason`` (a non-secret note) is stored as
        ``last_error``. Returns ``SKIPPED``.
        """
        wk = self._key(key)
        with self._lock:
            prior = self._by_window.get(wk)
            attempts = (prior.attempts if prior is not None else 0) + 1
            first_seen = prior.first_seen_at if prior is not None else now.isoformat()
            bars = prior.bars if prior is not None else 0
            empty_streak = prior.empty_streak if prior is not None else 0
            self._by_window[wk] = WindowLedgerRecord(
                market=key.market,
                security_id=key.security_id,
                interval=key.interval,
                window_start=key.start.isoformat(),
                window_end=key.end.isoformat(),
                status=SKIPPED,
                bars=bars,
                attempts=attempts,
                last_error=reason,
                first_seen_at=first_seen,
                last_attempt_at=now.isoformat(),
                empty_streak=empty_streak,
                in_pull_fetches=1,  # the one fetch that returned the known-gap
            )
            return SKIPPED

    def prune_superseded(
        self, planned_tiles: Mapping[tuple[str, str, str], Mapping[str, str]]
    ) -> int:
        """Delete every SUPERSEDED trailing row of the given plan — returns the count.

        DEC-WINDOW-TRAILING-GC-001. The tiler clips the trailing window of
        ``[start, end)`` to *end*, so as *end* advances across daily pulls the
        trailing window's KEY changes and yesterday's row becomes a permanent
        orphan: the tiler re-emits that key only for the SAME end (an identical
        re-pull — preserved by the equal-end rule below), never for a later one,
        so the row is never probed again and only accumulates (~4.4M rows/year
        at universe cadence, MIGRATION.md #632 follow-up ④).

        *planned_tiles* maps each series identity ``(market, security_id,
        interval)`` the CURRENT pull just planned to that pull's tiling
        ``{window_start_iso: window_end_iso}`` (ISO ``YYYY-MM-DD`` — zero-padded,
        so string order == date order). A row is pruned iff its series is
        planned AND its ``window_start`` is on the tiling AND either:

        * its ``window_end`` is STRICTLY shorter than the tile's planned end —
          ANY status (a settled SUCCESS partial, a clamp-stranded ``PLANNED``,
          a ``FAILED``/``EMPTY``/``SKIPPED`` orphan) is superseded alike,
          because every retry surface re-derives its work from the tiling,
          never from ledger rows; or
        * its ``window_end`` is STRICTLY longer AND the row is ``PLANNED`` with
          ``attempts == 0`` (DEC-PLAN-CLAMP-PARITY-001): pure stale INTENT a
          previous planner minted past the clamped fetch bound — zero fetch
          history, unfetchable under the current tiling, and invisible to the
          shorter-end rule because every later plan's end is SHORTER than the
          ghost's. Longer-end rows with ANY recorded attempt (a shrunk-range
          re-run's settled/failed history) are always kept.

        **No-mask by construction (#598):** an absent row is never skippable
        (:meth:`is_skippable` → ``False`` → fetch), so deleting rows can only
        turn would-be skips into re-fetches (the idempotent keep-first sink
        absorbs them), NEVER a fetch into a skip (a ``PLANNED`` row was never
        skippable to begin with). Equal-end rows (the current plan's own keys,
        including a same-day re-pull's settled trailing row) are always kept.

        One O(rows) scan + O(pruned) deletes under the lock, once per pull
        start — NOT an O(1) claim (measured 2026-06-10: 0.48 s over a 1.2M-row
        ledger pruning 364k orphans, ~1.4 MB index; the per-window
        plan/probe/record stay O(1)). Doomed keys are collected first, then
        deleted — the map is never mutated mid-iteration, and no other
        lock-taking method is called while the lock is held.
        """
        doomed: list[tuple[str, str, str, str, str]] = []
        with self._lock:
            for wk, row in self._by_window.items():
                tiles = planned_tiles.get(wk[:3])
                if tiles is None:
                    continue
                planned_end = tiles.get(wk[3])
                if planned_end is None:
                    continue
                superseded = wk[4] < planned_end
                # Longer-end PURE-INTENT ghost (DEC-PLAN-CLAMP-PARITY-001):
                # PLANNED + zero attempts past the clamped bound — unfetchable.
                ghost = wk[4] > planned_end and row.status == PLANNED and row.attempts == 0
                if superseded or ghost:
                    doomed.append(wk)
            for stale in doomed:
                del self._by_window[stale]
        return len(doomed)

    @property
    def count(self) -> int:
        """Number of distinct windows recorded (thread-safe)."""
        with self._lock:
            return len(self._by_window)

    def progress_counts(self) -> LedgerProgress:
        """Return a live O(N) :class:`LedgerProgress` snapshot — thread-safe, idempotent.

        Folds the per-window records into the dashboard bar's numbers
        (DEC-SPOT-WINDOW-PROGRESS-001): ``total`` windows, ``settled`` (``SUCCESS`` /
        ``SKIPPED`` — the SAME :data:`SETTLED_STATUSES` authority the skip rule + the
        /coverage headline use), ``failed`` (transient), summed ``bars``, and the
        ``"<symbol> <interval>"`` of the most-recently-attempted window (the live
        "current" label). The lock is held ONLY for an O(N) copy of the (immutable,
        frozen) record refs; the fold then runs lock-free, so a ~1 s-cadence poll never
        blocks the O(1) per-window :meth:`plan` / :meth:`record` / :meth:`is_skippable`
        on the hot path. Polling it is idempotent — every count is an absolute fold over
        the live records, so re-touched windows are never double-counted.
        """
        with self._lock:
            records = list(self._by_window.values())
        total = len(records)
        settled = resolved = no_data = planned = failed = bars = 0
        latest_attempt: str | None = None
        current = ""
        for record in records:
            bars += record.bars
            # Independent folds (not an elif chain) — each bucket's status set is distinct,
            # so every count is a clean True/False branch with no dead arc, and a future
            # status can't be silently miscounted (DEC-EMPTY-RESOLVED-REPORTING-001). settled
            # is the SKIP authority (SUCCESS/SKIPPED); resolved is the WIDER completion
            # authority (+ EMPTY); no_data/planned/failed are their own honest buckets.
            if record.status in _SKIPPABLE:
                settled += 1
            if record.status in RESOLVED_STATUSES:
                resolved += 1
            if record.status == EMPTY:
                no_data += 1  # a genuine no-data window — resolved, NOT pending
            if record.status == PLANNED:
                planned += 1
            if record.status == FAILED:
                failed += 1
            if record.last_attempt_at is not None and (
                latest_attempt is None or record.last_attempt_at > latest_attempt
            ):
                latest_attempt = record.last_attempt_at
                current = f"{record.security_id} {record.interval}"
        return LedgerProgress(
            total=total,
            settled=settled,
            resolved=resolved,
            no_data=no_data,
            planned=planned,
            failed=failed,
            bars=bars,
            current=current,
        )

    def records(self) -> list[WindowLedgerRecord]:
        """A deterministic, sorted snapshot of every recorded window (atomic)."""
        with self._lock:
            return sorted(
                self._by_window.values(),
                key=lambda r: (
                    r.market,
                    r.security_id,
                    r.interval,
                    r.window_start,
                    r.window_end,
                ),
            )

    def streak_total(self) -> int:
        """Sum of ``empty_streak`` across every recorded window — **O(N)**, one lock, no sort.

        The secondary progress signal for the outstanding-windows until-clean loop
        (DEC-UNTIL-CLEAN-ALL-OUTSTANDING-001): an EMPTY window stepping toward its
        SKIPPED threshold advances this fold BEFORE :attr:`LedgerProgress.settled`
        rises, so a round that only progressed empty streaks still counts as
        progress. A dedicated single-locked O(N) fold over the live values — NOT a
        sorted :meth:`records` snapshot (which would be O(N log N) + a second lock) —
        called once per OUTER round (≤ ``AUTO_RESUME_MAX_ROUNDS``), never on the
        per-bar hot path; the bound is O(N) per round, NOT an O(1)-total claim.
        """
        with self._lock:
            return sum(record.empty_streak for record in self._by_window.values())

    def to_rows(
        self,
    ) -> list[tuple[str, str, str, str, str, str, int, int, str | None, str, str | None, int, int]]:
        """Serialise to a deterministic list of plain tuples (for the durable store).

        ``in_pull_fetches`` (DEC-WINDOW-INPULL-FETCHES-001) is the LAST tuple element —
        appended after ``empty_streak`` exactly as that column was, so a legacy 12-tuple
        Parquet (written before it existed) still loads (the store fills it per-row).
        """
        return [
            (
                r.market,
                r.security_id,
                r.interval,
                r.window_start,
                r.window_end,
                r.status,
                r.bars,
                r.attempts,
                r.last_error,
                r.first_seen_at,
                r.last_attempt_at,
                r.empty_streak,
                r.in_pull_fetches,
            )
            for r in self.records()
        ]

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[
            tuple[str, str, str, str, str, str, int, int, str | None, str, str | None, int, int]
        ],
        *,
        threshold: int = EMPTY_TERMINAL_THRESHOLD,
    ) -> WindowLedger:
        """Restore from :meth:`to_rows` output (round-trips exactly)."""
        ledger = cls(_threshold=threshold)
        for (
            market,
            security_id,
            interval,
            window_start,
            window_end,
            status,
            bars,
            attempts,
            last_error,
            first_seen_at,
            last_attempt_at,
            empty_streak,
            in_pull_fetches,
        ) in rows:
            ledger._by_window[(market, security_id, interval, window_start, window_end)] = (
                WindowLedgerRecord(
                    market=market,
                    security_id=security_id,
                    interval=interval,
                    window_start=window_start,
                    window_end=window_end,
                    status=status,
                    bars=bars,
                    attempts=attempts,
                    last_error=last_error,
                    first_seen_at=first_seen_at,
                    last_attempt_at=last_attempt_at,
                    empty_streak=empty_streak,
                    in_pull_fetches=in_pull_fetches,
                )
            )
        return ledger
