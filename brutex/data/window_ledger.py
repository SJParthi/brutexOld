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
    "SKIPPED",
    "SUCCESS",
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
    #: CONSECUTIVE genuine-``EMPTY`` outcomes (DEC-WINDOW-LEDGER-001 R1). Bumped only
    #: when :meth:`WindowLedger.record` classifies the window ``EMPTY``; RESET to 0 by
    #: any ``SUCCESS`` / ``FAILED`` outcome. The EMPTY→``SKIPPED`` promotion fires on
    #: this streak, NOT on total ``attempts`` (which FAILED retries inflate), so a
    #: window that 429-failed twice then returned one transient ``EMPTY`` is NOT masked.
    empty_streak: int = 0


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
    ) -> str:
        """Record *key*'s fetch outcome; classify + bump the attempt count — **O(1)**, atomic.

        Mirrors the old ``WindowStatusLog.record`` plus the EMPTY-terminal promotion
        (DEC-EMPTY-TERMINAL-001 folded in): :func:`classify_window_status` of
        ``(raw_bars, error_type)`` gives ``SUCCESS`` / ``EMPTY`` / ``FAILED``; a
        clean ``EMPTY`` seen ``_threshold`` times (across re-pulls) is promoted to
        ``SKIPPED`` so it stops re-fetching forever. ``first_seen_at`` is preserved
        from the prior row (or stamped ``now`` for a never-planned window).
        Returns the final stored status.
        """
        classified = classify_window_status(raw_bars, error_type)
        wk = self._key(key)
        with self._lock:
            prior = self._by_window.get(wk)
            attempts = (prior.attempts if prior is not None else 0) + 1
            # R1 (DEC-WINDOW-LEDGER-001): promote on the CONSECUTIVE genuine-EMPTY
            # streak, NOT total ``attempts``. A SUCCESS / FAILED outcome RESETS the
            # streak to 0, so a window that 429-failed twice then returned one
            # transient EMPTY (attempts == 3 but streak == 1) is NOT masked — it
            # keeps being fetched until it is genuinely EMPTY ``_threshold`` pulls
            # running. The legacy DEC-EMPTY-TERMINAL-001 counted total attempts and
            # would have wrongly settled it; this is the strictly-more-correct rule.
            prior_streak = prior.empty_streak if prior is not None else 0
            empty_streak = prior_streak + 1 if classified == EMPTY else 0
            status = classified
            if classified == EMPTY and empty_streak >= self._threshold:
                status = SKIPPED  # genuinely empty _threshold pulls running → settle
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
            )
            return status

    def settle_known_gap(self, key: WindowKey, *, now: datetime, reason: str | None = None) -> str:
        """Settle *key* as ``SKIPPED`` — a ``DH-905`` known-gap that must NOT re-pull — **O(1)**.

        Folds DEC-DHAN-NODATA-SETTLED-001 (#597): a Dhan ``DH-905`` "no data
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
        planned AND its ``window_start`` is on the tiling AND its ``window_end``
        is STRICTLY shorter than the tile's planned end — ANY status (a settled
        SUCCESS partial, the clamp-stranded ``PLANNED``, a ``FAILED``/``EMPTY``/
        ``SKIPPED`` orphan) is superseded alike, because every retry surface
        re-derives its work from the tiling, never from ledger rows.

        **No-mask by construction (#598):** an absent row is never skippable
        (:meth:`is_skippable` → ``False`` → fetch), so deleting rows can only
        turn would-be skips into re-fetches (the idempotent keep-first sink
        absorbs them), NEVER a fetch into a skip. Equal-end rows (the current
        plan's own keys, including a same-day re-pull's settled trailing row)
        and longer-end rows (a shrunk-range re-run's history) are always kept.

        One O(rows) scan + O(pruned) deletes under the lock, once per pull
        start — NOT an O(1) claim (measured 2026-06-10: 0.48 s over a 1.2M-row
        ledger pruning 364k orphans, ~1.4 MB index; the per-window
        plan/probe/record stay O(1)). Doomed keys are collected first, then
        deleted — the map is never mutated mid-iteration, and no other
        lock-taking method is called while the lock is held.
        """
        doomed: list[tuple[str, str, str, str, str]] = []
        with self._lock:
            for wk in self._by_window:
                tiles = planned_tiles.get(wk[:3])
                if tiles is None:
                    continue
                planned_end = tiles.get(wk[3])
                if planned_end is not None and wk[4] < planned_end:
                    doomed.append(wk)
            for stale in doomed:
                del self._by_window[stale]
        return len(doomed)

    @property
    def count(self) -> int:
        """Number of distinct windows recorded (thread-safe)."""
        with self._lock:
            return len(self._by_window)

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

    def to_rows(
        self,
    ) -> list[tuple[str, str, str, str, str, str, int, int, str | None, str, str | None, int]]:
        """Serialise to a deterministic list of plain tuples (for the durable store)."""
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
            )
            for r in self.records()
        ]

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[
            tuple[str, str, str, str, str, str, int, int, str | None, str, str | None, int]
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
                )
            )
        return ledger
