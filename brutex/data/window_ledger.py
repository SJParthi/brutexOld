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
from brutex.data.interior_gap_detect import INTERIOR_GAP
from brutex.data.partial_window_detect import SUSPECTED_PARTIAL
from brutex.data.session import ALL_FILTERED
from brutex.data.window_status import EMPTY, FAILED, SUCCESS, classify_window_status

__all__ = [
    "CONFIRMED_EMPTY_MIN_FETCHES",
    "DEMOTED_SETTLE_REASONS",
    "EMPTY",
    "EMPTY_TERMINAL_THRESHOLD",
    "EXPIRED_WINDOW_SETTLE_SUFFIX",
    "FAILED",
    "PLANNED",
    "REOPENED_MARKER",
    "SETTLED_STATUSES",
    "SKIPPED",
    "SUCCESS",
    "LedgerProgress",
    "WindowLedger",
    "WindowLedgerRecord",
    "expired_window_settle_reason",
    "is_expired_window_settle_reason",
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
#: load-induced ``{}`` that later fills, OR a recent daily/option bar the vendor has
#: not published yet) a few separate re-pull chances before the window is settled.
#: This is the 10→3 REVERT of DEC-REALDATA-ONLY-NO-1M-TO-1D-001 (MIGRATION.md #852):
#: the operator-raised 3→10 persistence of DEC-RETRY-PERSIST-10-001 only existed to
#: keep retrying recent-stock ``1day`` empties long enough for the 1m→1d resample to
#: fill them; with that resample removed (real Groww data only — never compute 1d
#: from 1m) a recent-stock ``1day`` empty is honestly terminal, so the now-futile
#: 10-round grind is reverted back to 3 — the empties settle fast + honestly.
#: Genuinely OLD pre-listing windows still fast-settle on the FIRST empty via the
#: old-gap settle path (DEC-OLD-GAP-FAST-SETTLE-001), so this never re-hammers an
#: ancient dead window — only recent maybe-late windows get the few extra chances.
EMPTY_TERMINAL_THRESHOLD: int = 3

#: Minimum CLEAN-EMPTY confirmations an old empty window needs before its empty is
#: trusted as "confirmed" for the in-pull OLD-gap fast-settle HINT computed by the
#: spines (DEC-OLD-GAP-FAST-SETTLE-001 ``settle_empty``). ``2`` means the empty was
#: observed at least twice — historically both had to land WITHIN one pull (the
#: in-pull empty retry re-hit it), which a REDUCED per-strike empty-retry budget of 1
#: (DEC-FETCH-PER-WINDOW-GUARANTEE-001) made STRUCTURALLY unreachable;
#: DEC-DEAD-STRIKE-LEDGER-SETTLE-001 then accumulated confirmations ACROSS pulls.
#: **DEC-OLD-EMPTY-SINGLE-CONFIRM-001 (operator-decreed 2026-07-04) SUPERSEDES both
#: arms for the LEDGER settle:** an OLD window (``record(old_window=True)``) now
#: settles ``SKIPPED`` on a SINGLE genuine, unsuppressed clean-EMPTY fetch — the
#: 2026-07-03 production run spent HOURS in a round-2 until-clean sweep re-confirming
#: every deep-wing empty this threshold forced. This constant still governs the
#: spines' in-pull ``settle_empty`` confirmation hint (and stays re-exported from
#: ``brutex.adapters.ingest_common`` — one source of truth, CLAUDE.md §11 COMMON;
#: the canonical home is here because ``ingest_common`` already imports from
#: ``brutex.data``); it no longer gates the ledger's old-window settle.
CONFIRMED_EMPTY_MIN_FETCHES: int = 2

#: The recognisable tail of an expired-window settle reason
#: (:func:`expired_window_settle_reason`) — the ONE marker
#: ``brutex.dashboard.db_view.window_status_label`` keys the honest UI label on
#: (DEC-OLD-EMPTY-SINGLE-CONFIRM-001; CLAUDE.md §11 COMMON — no second spelling).
EXPIRED_WINDOW_SETTLE_SUFFIX: str = ", expired window)"

#: Every ``degraded_reason`` tag the data-completeness demotion channel
#: (:meth:`WindowLedger.record` ``degraded=True``) can stamp as a row's
#: ``last_error`` — the ONE canonical set (CLAUDE.md §11 COMMON;
#: DEC-DATA-HONESTY-ALLFILTERED-SEED-001) so the ``/coverage/windows`` label
#: (:func:`brutex.dashboard.db_view.window_status_label`) can render a
#: demotion-settled ``SKIPPED`` honestly instead of the "vendor confirmed no
#: trades" wording (factually WRONG for these rows: the vendor RETURNED bars —
#: they were boundary-filtered / hole-flagged / truncation-flagged, never a
#: clean no-trades answer). Each tag's detector module stays its source of
#: truth; this set only ENUMERATES them beside the channel that stamps them.
DEMOTED_SETTLE_REASONS: frozenset[str] = frozenset({ALL_FILTERED, INTERIOR_GAP, SUSPECTED_PARTIAL})


def expired_window_settle_reason(clean_attempts: int) -> str:
    """The honest ``last_error`` reason stamped on an expired-window settle — pure.

    DEC-OLD-EMPTY-SINGLE-CONFIRM-001: an OLD window settled ``SKIPPED`` off a
    single clean-empty must SAY so on the UI — the generic streak-derived label
    would claim ``3 clean attempt(s)`` (the forced-threshold streak), a lie.
    ``clean_attempts`` is the genuine count of distinct clean-EMPTY pull
    observations (``prior.empty_streak + 1``); the normal fresh settle reads
    exactly the operator-decreed ``"vendor confirmed no trades (1 clean
    attempt, expired window)"``.
    """
    plural = "" if clean_attempts == 1 else "s"
    return f"vendor confirmed no trades ({clean_attempts} clean attempt{plural}, expired window)"


def is_expired_window_settle_reason(reason: str | None) -> bool:
    """``True`` iff *reason* is an :func:`expired_window_settle_reason` stamp — pure, O(1)."""
    return (
        reason is not None
        and reason.startswith("vendor confirmed no trades (")
        and reason.endswith(EXPIRED_WINDOW_SETTLE_SUFFIX)
    )


#: The ``last_error`` marker :meth:`WindowLedger.reopen_skipped` stamps on a
#: re-opened row (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001). A ``FAILED`` row carrying
#: this marker is a DELIBERATE operator re-check (``brutex reopen-skipped``), so
#: :meth:`WindowLedger.is_expected_empty_recheck` reports it expected-empty and the
#: mass-empty breaker never counts its re-verification — a reopen + re-pull can
#: never re-trip the breaker endlessly on the very windows the operator chose to
#: re-verify.
REOPENED_MARKER: str = "REOPENED"

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
    #: DEDUPED bars the sink actually NET-ADDED to the lake on the MOST-RECENT
    #: attempt (DEC-INGEST-BOUNDARY-INTEGRITY-001) — the persisted twin of
    #: :attr:`~brutex.data.sink.SinkAppendResult.bars_net_new`, which
    #: DEC-SINK-DEDUP-COUNT-001 (#1031) computed but never persisted. ``bars``
    #: stays the INPUT batch size the spine appended (several ``>0`` gates read
    #: it); this is the keep-first-deduped count, so a re-pull whose bars are all
    #: already stored reads ``bars > 0, bars_net_new == 0``. ``None`` = honestly
    #: UNKNOWN — a legacy row written before the column existed, or a path with
    #: no sink result (a lake cold-start seed) — NEVER fabricated to ``0``
    #: (Golden Rule #1: ``0`` would falsely claim "that pull added nothing new").
    bars_net_new: int | None = None


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
    #: LOUD reload-failure flag (DEC-WINDOW-LEDGER-RESCUE-001): set ``True`` by
    #: :func:`~brutex.data.window_ledger_store.load_window_ledger` when the persisted
    #: Parquet EXISTED but could not be read (corrupt / wrong-shape / IO error) and the
    #: load degraded to this EMPTY ledger. The pull drivers thread it into the pull
    #: report / note ("window ledger reload FAILED — running full re-verify pull (no
    #: skip)") so a lost cross-run memory is ANNOUNCED, never silent (Golden Rule #10).
    #: A missing file (a genuine fresh lake) does NOT set it. Never serialised.
    load_failed: bool = False
    #: Keys :meth:`prune_superseded` deleted during THIS process's lifetime
    #: (DEC-WINDOW-LEDGER-RESCUE-001). The merge-on-save
    #: (:func:`~brutex.data.window_ledger_store.save_window_ledger`) folds the ON-DISK
    #: ledger back in so a concurrent writer's rows are never clobbered — but a row this
    #: ledger DELIBERATELY pruned must not resurrect from the disk copy on every flush
    #: (that would defeat the DEC-WINDOW-TRAILING-GC-001 orphan GC). Tombstones apply
    #: only to disk-ONLY rows; a key later re-created in memory always wins. Never
    #: serialised (per-pull lifetime is exactly the window between load and save).
    #: Cross-process caveat: a key concurrently RE-PLANNED by ANOTHER process's plan
    #: can be transiently dropped by THIS process's tombstone at merge time; the drop
    #: self-heals — the other process re-writes the row on its next flush (its own
    #: in-memory copy always wins its own merge), or a future pull simply re-plans it,
    #: and the keep-first sink makes any resulting re-fetch idempotent.
    _pruned: set[tuple[str, str, str, str, str]] = field(
        default_factory=set, repr=False, compare=False
    )
    #: Windows NEWLY settled ``SKIPPED`` by THIS ledger instance (DEC-EMPTY-WINDOW-
    #: CIRCUIT-BREAKER-001 — honest-verdict requirement). A ledger instance is loaded
    #: fresh per pull (``begin_pull_ledger``), so this counts the CURRENT pull's
    #: settles across every path that can produce one (the old-gap fast-settle, the
    #: across-pull EMPTY-streak promotion, the bounded degraded-settle, and
    #: :meth:`settle_known_gap`) — a re-record of an already-``SKIPPED`` row never
    #: double-counts. NOT serialised (a per-run tally, not history).
    _settled_skipped_count: int = field(default=0, repr=False)
    #: Keys :meth:`reopen_skipped` DELIBERATELY demoted ``SKIPPED`` → ``FAILED`` during
    #: THIS process's lifetime (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 × the
    #: DEC-WINDOW-LEDGER-RESCUE-001 merge-on-save). The merge-on-save's same-key
    #: conflict rule prefers the MORE-SETTLED status, so without this tombstone the
    #: on-disk ``SKIPPED`` copy (rank 3) would beat the reopened ``FAILED`` row
    #: (rank 1) on the very next save and SILENTLY UNDO the operator's re-open —
    #: exactly the resurrect-from-disk class the ``_pruned`` tombstones close for the
    #: orphan GC. For a key in this set the IN-MEMORY row wins the merge outright.
    #: Never serialised (per-instance lifetime — the window between load and save;
    #: the ``brutex reopen-skipped`` CLI runs under the account-wide pull
    #: single-flight lock, so no concurrent pull is mid-flight while it saves).
    _reopened: set[tuple[str, str, str, str, str]] = field(
        default_factory=set, repr=False, compare=False
    )
    #: Keys :meth:`record` DEMOTED ``SUCCESS`` → ``FAILED`` for a DATA-COMPLETENESS
    #: reason (``degraded=True`` — an interior multi-session hole / trailing
    #: truncation, DEC-INTERIOR-GAP-DETECT-001 / DEC-PARTIAL-WINDOW-REPULL-001)
    #: during THIS process's lifetime (DEC-WINDOW-LEDGER-DEGRADED-TOMBSTONE-001 ×
    #: the DEC-WINDOW-LEDGER-RESCUE-001 merge-on-save). The merge-on-save's
    #: same-key conflict rule prefers the MORE-SETTLED status, so without this
    #: tombstone a concurrent pull's on-disk ``SUCCESS`` copy (rank 3) would beat
    #: the demoted ``FAILED`` row (rank 1) on the very next save and SILENTLY UNDO
    #: the demotion — the window would read settled/skippable forever WITH the
    #: confirmed gap, exactly the resurrect-from-disk class the ``_reopened``
    #: tombstones close for the operator re-open. For a key in this set the
    #: IN-MEMORY row wins the merge outright. The accept-gap ``SKIPPED`` settle at
    #: the threshold is deliberately NOT tombstoned (it is a legitimately terminal
    #: rank-3 row a concurrent ``SUCCESS`` may honestly displace) — and a later
    #: SETTLED re-record of the same key (a genuine complete ``SUCCESS`` re-pull /
    #: the ``SKIPPED`` settle) DISCARDS its earlier tombstone (the demotion was
    #: superseded in-process; a re-pullable ``FAILED``/``EMPTY`` re-record keeps
    #: it — hostile-review fold). Never serialised (per-instance lifetime — the
    #: window between load and save).
    _degraded: set[tuple[str, str, str, str, str]] = field(
        default_factory=set, repr=False, compare=False
    )

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
                bars_net_new=0,  # nothing appended yet (mirrors bars=0)
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
                # The historical write's keep-first-deduped count is UNRECORDED —
                # honest unknown, never fabricated (DEC-INGEST-BOUNDARY-INTEGRITY-001).
                bars_net_new=None,
            )
            return True

    def has_window(self, key: WindowKey) -> bool:
        """True iff ANY row exists for *key* (any status) — **O(1)**, atomic, read-only.

        The cold-start seed's existence probe (DEC-DATA-HONESTY-ALLFILTERED-SEED-001):
        :func:`~brutex.data.window_ledger_store.seed_enclosed_windows` skips its
        density cross-check + refusal WARNING for a window that already HAS a row,
        because the only-if-absent :meth:`seed_lake_success` would no-op anyway —
        without this probe every steady-state reconcile re-warned about windows
        that were long since fetched/settled. Never mutates anything.
        """
        wk = self._key(key)
        with self._lock:
            return wk in self._by_window

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

    def is_expected_empty_recheck(self, key: WindowKey) -> bool:
        """``True`` iff an EMPTY re-fetch of *key* is EXPECTED, not throttle evidence — **O(1)**.

        DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 — the ledger-knowledge feed for the
        mass-empty breaker's per-call ``expected`` hint. Two prior-row shapes make a
        fresh empty response UNINFORMATIVE for throttle detection:

        * the row is already ``EMPTY`` — a prior pull saw this window empty, so a
          re-confirmation is the expected outcome (the empty-dominated RESUME shape:
          a worklist made of prior pulls' pre-listing prefixes must not read as a
          throttled universe);
        * the row is a ``FAILED`` stamped :data:`REOPENED_MARKER` — a DELIBERATE
          operator re-check via ``brutex reopen-skipped``; re-verifying it empty is
          the whole point, so it is breaker-exempt by design (a bulk reopen +
          re-pull can never re-trip the breaker endlessly).

        Any other row (``PLANNED`` / ordinary ``FAILED`` / absent) leaves the empty
        fully counted — a 429-retried or never-fetched window coming back empty IS
        informative. The breaker still counts these windows as OBSERVED (only the
        trip tally is exempt), and it still refuses their SETTLE after a trip.
        """
        wk = self._key(key)
        with self._lock:
            record = self._by_window.get(wk)
            if record is None:
                return False
            return record.status == EMPTY or (
                record.status == FAILED and record.last_error == REOPENED_MARKER
            )

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
        old_window: bool = False,
        degraded: bool = False,
        degraded_reason: str | None = None,
        suppress_settle: bool = False,
        bars_net_new: int | None = None,
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

        *old_window* (DEC-DEAD-STRIKE-LEDGER-SETTLE-001) is the pure "vendor data can
        never appear" fact (``window_end`` older than
        :data:`~brutex.adapters.ingest_common.OLD_GAP_HORIZON_DAYS`). Since
        **DEC-OLD-EMPTY-SINGLE-CONFIRM-001** (operator-decreed 2026-07-04, superseding
        #1172's budget-aware cumulative accumulation) an OLD window settles ``SKIPPED``
        off a SINGLE genuine, unsuppressed clean-EMPTY fetch in THIS pull — the streak
        is forced to the threshold and the honest
        :func:`expired_window_settle_reason` stamp becomes the ``last_error`` (see the
        settle arm below). The data-safety law is untouched ("if uncertain, fetch"): a
        ``SUCCESS`` resets the streak, a ``FAILED`` (429/timeout) never counts and
        never settles, a breaker-suppressed empty neither bumps nor settles, a RECENT
        window (*old_window* ``False``) keeps the conservative across-pull streak, and
        :meth:`reopen_skipped` resets the streak so an operator re-open regains the
        full confirmation budget. The default ``False`` is byte-identical to the prior
        behaviour. HONEST envelope (DEC-DATA-HONESTY-ALLFILTERED-SEED-001 doc
        reconcile): BOTH spines' ``empty_streak`` may now include degraded-demotion
        bumps — the F&O spine passes *degraded* (an ``ALL_FILTERED`` boundary verdict)
        AND *old_window* on the SAME record call. Because the old-empty settle is
        single-confirm (it fires off THIS pull's own genuine clean EMPTY), a prior
        degraded bump can never SUBSTITUTE for clean-EMPTY evidence — but the
        :func:`expired_window_settle_reason` stamp's clean-attempt COUNT is
        streak-derived (``prior_streak + 1``), so a prior degraded bump INFLATES that
        display count by one (e.g. one all-filtered demotion then one clean EMPTY
        reads "2 clean attempts"). Accepted + regression-pinned: the settle decision
        is sound (a real clean EMPTY landed), only the count conflates flagged
        attempts with clean ones; ``brutex reopen-skipped`` stays the recovery.

        *degraded* (DEC-INTERIOR-GAP-DETECT-001 / DEC-PARTIAL-WINDOW-REPULL-001) is the
        DATA-COMPLETENESS demotion hint: a clean bars-returning fetch whose stored bars
        carry a CONFIRMED interior multi-session hole or a trailing truncation is NOT a
        complete window, so it must NOT be marked terminal/skippable on a ``SUCCESS``.
        When ``True`` AND the natural classification is ``SUCCESS`` the window is recorded
        ``FAILED`` (re-pullable — exactly like a transient) with *degraded_reason* (a
        non-secret tag from :data:`DEMOTED_SETTLE_REASONS` — ``"INTERIOR_GAP"`` /
        ``"SUSPECTED_PARTIAL"`` / ``"ALL_FILTERED"``) stamped as the
        ``last_error``, and the ``empty_streak`` is BUMPED so the re-pull is BOUNDED: after
        :data:`EMPTY_TERMINAL_THRESHOLD` consecutive degraded pulls the window settles
        ``SKIPPED`` (the gap is accepted after the operator has been warned that many
        times), exactly as a genuine across-pull EMPTY settles — so the demotion can never
        loop forever and the universe pull still converges. A re-pull that comes back
        COMPLETE records a clean ``SUCCESS`` (the streak resets, the window settles). The
        bars stay durably stored (idempotent keep-first sink) the whole time — the
        demotion only RE-OPENS the window for completion, never deletes data. ``degraded``
        is IGNORED for a ``FAILED`` (already re-pullable) or an ``EMPTY`` (no bars to be
        incomplete); the default ``False`` is byte-identical to the prior behaviour.
        Each demoted key is tombstoned in :meth:`degraded_keys`
        (DEC-WINDOW-LEDGER-DEGRADED-TOMBSTONE-001) so the
        DEC-WINDOW-LEDGER-RESCUE-001 merge-on-save cannot resurrect a concurrent
        pull's on-disk ``SUCCESS`` copy over the demotion (its more-settled rank
        would otherwise silently re-mask the confirmed gap on the very next flush)
        — exactly as :meth:`reopen_skipped` tombstones its ``SKIPPED`` → ``FAILED``
        demotions. The accept-gap ``SKIPPED`` settle at the threshold is NOT
        tombstoned (a legitimately terminal row), and a later SETTLED re-record of
        the same key (a genuine complete ``SUCCESS`` re-pull / the ``SKIPPED``
        settle) DISCARDS its earlier same-process tombstone — a re-pullable
        ``FAILED``/``EMPTY`` re-record keeps it (no completeness evidence either
        way, so the demotion stands).

        *suppress_settle* (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001) marks an ``EMPTY``
        outcome as **UNTRUSTED** — recorded while the pull's mass-empty circuit
        breaker is TRIPPED (a suspected vendor soft-throttle [R2] / mis-addressing
        [R3] episode, when a clean-empty 200 is NOT evidence of no-data). The window
        is recorded ``EMPTY`` (retryable, honest about what the vendor returned) but
        its ``empty_streak`` is PRESERVED (not bumped — a suspect empty must not step
        the window toward the across-pull ``SKIPPED`` promotion) and it can NEVER
        settle ``SKIPPED`` on this record (both the fast-settle and the streak
        promotion are gated off). IGNORED for ``SUCCESS`` / ``FAILED`` / degraded
        outcomes; the default ``False`` is byte-identical to the prior behaviour.

        *bars_net_new* (DEC-INGEST-BOUNDARY-INTEGRITY-001) is the sink's
        keep-first-DEDUPED net-new bar count for this append
        (:attr:`~brutex.data.sink.SinkAppendResult.bars_net_new`), stored
        alongside ``bars`` (the INPUT batch size, unchanged — the ``>0`` gates
        keep reading it). ``None`` (the default) records an honest UNKNOWN for a
        caller with no sink result — never fabricated to ``0``.
        """
        classified = classify_window_status(raw_bars, error_type, degraded=degraded)
        # A demoted SUCCESS is recorded FAILED with a completeness reason (not a vendor
        # error) so /coverage/windows shows WHY it re-pulls. Only meaningful when the demotion
        # actually fired (a clean SUCCESS → FAILED); for any other path the natural
        # ``error_type`` is preserved unchanged.
        degraded_success = degraded and error_type is None and raw_bars > 0
        if degraded_success:
            error_type = degraded_reason or "DEGRADED"
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
                # An UNTRUSTED empty (the pull's mass-empty breaker is tripped —
                # DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001) preserves the streak: a
                # throttle-shaped empty is NOT evidence of no-data, so it must not
                # step the window toward the across-pull SKIPPED promotion.
                empty_streak = prior_streak if suppress_settle else prior_streak + 1
            elif degraded_success:
                # A demoted-SUCCESS (interior gap / truncation) re-pull is BOUNDED: bump the
                # streak so it settles SKIPPED at the threshold, exactly like a genuine EMPTY,
                # rather than re-pulling forever on a window the vendor will never complete.
                empty_streak = prior_streak + 1
            elif classified == FAILED:
                empty_streak = prior_streak  # rate-limit/timeout — preserve, never reset
            else:  # SUCCESS — the window has real data
                empty_streak = 0
            status = classified
            if degraded_success and empty_streak >= self._threshold:
                # The window has been flagged incomplete ``_threshold`` consecutive pulls —
                # accept the gap and settle SKIPPED so the pull converges (the operator has
                # been warned each time; the stored bars are kept).
                status = SKIPPED
            elif classified == EMPTY and not suppress_settle and (settle_empty or old_window):
                # OLD-EMPTY SINGLE-CONFIRM (DEC-OLD-EMPTY-SINGLE-CONFIRM-001,
                # operator-decreed 2026-07-04 — SUPERSEDES the budget-aware
                # cumulative arm of DEC-DEAD-STRIKE-LEDGER-SETTLE-001 #1172): an
                # EMPTY window OLD enough that vendor data can never appear
                # (``old_window`` — ``clamped_end`` older than
                # OLD_GAP_HORIZON_DAYS) settles SKIPPED off this SINGLE genuine,
                # unsuppressed clean-empty fetch, in THIS pass — effectively
                # ``min_fetches = 1`` for expired windows. The 2026-07-03
                # production run spent HOURS in a round-2 until-clean sweep
                # re-confirming every deep-wing empty the old 2-confirmation bar
                # forced. The streak is still FORCED to the threshold (the
                # persisted row keeps the same settled shape as an across-pull
                # settle) and the honest reason is stamped as ``last_error`` so
                # the UI says "1 clean attempt, expired window" instead of the
                # streak-derived lie. Only ever reached for EMPTY (SUCCESS/FAILED
                # ignore the hints — data always wins; a 429/timeout is never
                # evidence), RECENT windows (old_window False) keep the
                # conservative across-pull streak below, and the arm is GATED OFF
                # while the pull's mass-empty breaker is tripped (suppress_settle
                # — a throttle-shaped empty must never cement,
                # DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001; the #1129 breaker + the
                # #919 all-empty detector remain the systemic-glitch guards, and
                # ``brutex reopen-skipped`` is the manual recovery).
                empty_streak = self._threshold
                status = SKIPPED
                error_type = expired_window_settle_reason(prior_streak + 1)
            elif classified == EMPTY and not suppress_settle and empty_streak >= self._threshold:
                status = SKIPPED  # genuinely empty _threshold CLEAN times → settle
            if degraded_success and status == FAILED:
                # Tombstone the demotion (DEC-WINDOW-LEDGER-DEGRADED-TOMBSTONE-001):
                # the merge-on-save must keep this in-memory FAILED outright, or a
                # concurrent pull's on-disk SUCCESS (more-settled rank) would silently
                # re-mask the confirmed gap on the very next flush. The guard is exact:
                # ``degraded_success`` selects the clean-SUCCESS-forced-to-FAILED case,
                # and ``status == FAILED`` excludes the accept-gap SKIPPED settle above
                # (legitimately terminal — never wrongly pinned against a SUCCESS).
                self._degraded.add(wk)
            elif status in (SUCCESS, SKIPPED):
                # Hostile-review fold: a later SETTLED outcome for the SAME key — a
                # genuine complete re-pull (SUCCESS) or the accept-gap settle
                # (SKIPPED) — SUPERSEDES any earlier same-process demotion, so its
                # stale tombstone is discarded and the merge's more-settled rule
                # applies to the new row unconditionally (the "a SKIPPED settle is
                # never tombstoned" contract now holds even when a prior FAILED
                # demotion of the same window tombstoned it earlier in this
                # process). A re-pullable FAILED/EMPTY re-record deliberately KEEPS
                # the tombstone — a 429/timeout/empty gives NO completeness
                # evidence, and discarding on it would let a concurrent pull's disk
                # SUCCESS re-mask the confirmed gap on our own save.
                self._degraded.discard(wk)
            first_seen = prior.first_seen_at if prior is not None else now.isoformat()
            if status == SKIPPED and (prior is None or prior.status != SKIPPED):
                # Honest-verdict tally (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001): count every
                # window THIS instance newly settles SKIPPED, whatever the path (fast-settle,
                # streak promotion, degraded accept-gap) — the verdict must SAY it.
                self._settled_skipped_count += 1
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
                bars_net_new=bars_net_new,
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
            # No fetch/append happened here — preserve the prior net-new (like ``bars``).
            bars_net_new = prior.bars_net_new if prior is not None else None
            if prior is None or prior.status != SKIPPED:
                # Honest-verdict tally (DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001): a known-gap
                # settle is a newly-SKIPPED window too — the verdict must SAY it.
                self._settled_skipped_count += 1
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
                bars_net_new=bars_net_new,
            )
            return SKIPPED

    @property
    def newly_settled_skipped(self) -> int:
        """Windows newly settled ``SKIPPED`` by THIS instance (per-pull) — **O(1)**.

        DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 (honest verdicts): a pull that settled
        ANY window as terminal ``SKIPPED`` must SAY so — the verdict layer reads this
        after the pull and appends the count + the ``brutex reopen-skipped``
        re-verify hint. Counts only prior-status transitions (a re-record of an
        already-``SKIPPED`` row never double-counts), across every settle path.
        """
        with self._lock:
            return self._settled_skipped_count

    def reopen_skipped(
        self,
        *,
        market: str | None = None,
        security_id: str | None = None,
        limit: int | None = None,
        reason: str = REOPENED_MARKER,
    ) -> int:
        """Re-open ``SKIPPED`` windows for re-verification — returns the count re-opened.

        DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 (un-cementable settles, register R2 fix:
        "make SKIPPED reversible via an operator re-open action"). Every matching
        ``SKIPPED`` row is demoted to ``FAILED`` with ``empty_streak`` RESET to 0 and
        *reason* stamped as ``last_error`` — so :meth:`is_skippable` turns ``False``
        and the next pull re-fetches it with the FULL fresh confirmation budget (a
        genuinely-empty window re-settles honestly on that pull, now under the
        breaker's guard; a throttle-poisoned one recovers its data). The default
        *reason* is :data:`REOPENED_MARKER`, which
        :meth:`is_expected_empty_recheck` recognises — the re-verify pull treats the
        re-opened windows as DELIBERATE re-checks, exempt from the mass-empty
        breaker's trip tally (a bulk reopen + re-pull can never re-trip endlessly).
        ``bars`` / ``attempts`` / ``first_seen_at`` / ``last_attempt_at`` are
        preserved (no fetch happened here); the stored lake bars are untouched (the
        idempotent keep-first sink absorbs the re-fetch) — re-opening is always
        DATA-SAFE. Each re-opened key is tombstoned in :meth:`reopened_keys` so the
        DEC-WINDOW-LEDGER-RESCUE-001 merge-on-save cannot resurrect the on-disk
        ``SKIPPED`` copy (its more-settled rank would otherwise silently undo the
        demotion on the very next flush).

        *market* / *security_id* filter the selection (``None`` = any); *limit*
        bounds it to the FIRST *limit* rows in the deterministic
        (market, symbol, interval, start, end) sort order — the bounded-sample
        re-verify path. Each re-open is an **O(1)** row swap; the selection pass is
        **O(rows log rows)** once per operator action (the ``sorted`` scan that buys
        the deterministic ``--limit`` sample; the ``prune_superseded`` envelope,
        never the per-window hot path). Idempotent: re-running re-opens nothing new
        (``FAILED`` rows no longer match).
        """
        doomed: list[tuple[str, str, str, str, str]] = []
        with self._lock:
            for wk in sorted(self._by_window):
                row = self._by_window[wk]
                if row.status != SKIPPED:
                    continue
                if market is not None and row.market != market:
                    continue
                if security_id is not None and row.security_id != security_id:
                    continue
                if limit is not None and len(doomed) >= limit:
                    break
                doomed.append(wk)
            for wk in doomed:
                row = self._by_window[wk]
                self._by_window[wk] = WindowLedgerRecord(
                    market=row.market,
                    security_id=row.security_id,
                    interval=row.interval,
                    window_start=row.window_start,
                    window_end=row.window_end,
                    status=FAILED,
                    bars=row.bars,
                    attempts=row.attempts,
                    last_error=reason,
                    first_seen_at=row.first_seen_at,
                    last_attempt_at=row.last_attempt_at,
                    empty_streak=0,
                    in_pull_fetches=row.in_pull_fetches,
                    bars_net_new=row.bars_net_new,  # no fetch here — preserved
                )
            self._reopened.update(doomed)
        return len(doomed)

    def reopened_keys(self) -> frozenset[tuple[str, str, str, str, str]]:
        """The keys :meth:`reopen_skipped` demoted this process — thread-safe snapshot.

        DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 × DEC-WINDOW-LEDGER-RESCUE-001.
        Consumed by the merge-on-save
        (:func:`~brutex.data.window_ledger_store.save_window_ledger`) so a
        DELIBERATELY re-opened row's on-disk ``SKIPPED`` copy is not resurrected by
        the more-settled-status conflict rule (which would silently undo the
        operator's ``brutex reopen-skipped``); for these keys the in-memory row wins
        the merge outright. The twin of :meth:`pruned_keys`. O(reopened) copy under
        the lock.
        """
        with self._lock:
            return frozenset(self._reopened)

    def degraded_keys(self) -> frozenset[tuple[str, str, str, str, str]]:
        """The keys :meth:`record` demoted ``SUCCESS`` → ``FAILED`` — thread-safe snapshot.

        DEC-WINDOW-LEDGER-DEGRADED-TOMBSTONE-001 × DEC-WINDOW-LEDGER-RESCUE-001.
        Consumed by the merge-on-save
        (:func:`~brutex.data.window_ledger_store.save_window_ledger`) so a
        data-completeness demotion (``degraded=True`` — a confirmed interior gap /
        trailing truncation recorded ``FAILED`` for re-pull) is not resurrected to
        a concurrent pull's on-disk ``SUCCESS`` copy by the more-settled-status
        conflict rule (which would silently re-mask the gap); for these keys the
        in-memory row wins the merge outright. The third instance of the
        per-decision tombstone shape (:meth:`pruned_keys` / :meth:`reopened_keys`).
        O(degraded) copy under the lock.
        """
        with self._lock:
            return frozenset(self._degraded)

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
            # Tombstone the pruned keys (DEC-WINDOW-LEDGER-RESCUE-001) so the
            # merge-on-save never resurrects them from the on-disk copy — pruning
            # would otherwise be undone by the very next flush's read-merge-write.
            self._pruned.update(doomed)
        return len(doomed)

    def pruned_keys(self) -> frozenset[tuple[str, str, str, str, str]]:
        """The keys :meth:`prune_superseded` deleted this process — thread-safe snapshot.

        DEC-WINDOW-LEDGER-RESCUE-001. Consumed by the merge-on-save
        (:func:`~brutex.data.window_ledger_store.save_window_ledger`) so a
        DELIBERATELY-pruned row on the on-disk ledger is dropped instead of
        resurrected, while a concurrent writer's genuinely-new rows are kept. A key
        later re-created in memory is unaffected (the tombstone applies only to
        disk-ONLY rows). O(pruned) copy under the lock.
        """
        with self._lock:
            return frozenset(self._pruned)

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
    ) -> list[
        tuple[
            str,
            str,
            str,
            str,
            str,
            str,
            int,
            int,
            str | None,
            str,
            str | None,
            int,
            int,
            int | None,
        ]
    ]:
        """Serialise to a deterministic list of plain tuples (for the durable store).

        ``in_pull_fetches`` (DEC-WINDOW-INPULL-FETCHES-001) then ``bars_net_new``
        (DEC-INGEST-BOUNDARY-INTEGRITY-001) were each appended LAST — after
        ``empty_streak`` exactly as that column was — so a legacy 12/13-tuple
        Parquet (written before either existed) still loads (the store fills the
        missing column per-row; an absent ``bars_net_new`` reads ``None``).
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
                r.bars_net_new,
            )
            for r in self.records()
        ]

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[
            tuple[
                str,
                str,
                str,
                str,
                str,
                str,
                int,
                int,
                str | None,
                str,
                str | None,
                int,
                int,
                int | None,
            ]
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
            bars_net_new,
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
                    bars_net_new=bars_net_new,
                )
            )
        return ledger
