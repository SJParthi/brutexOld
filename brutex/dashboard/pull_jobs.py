"""Background pull job — run a pull off the request thread, stream live progress.

The glue between a long-running universe pull and the dashboard's **live**
progress view (DEC-PULL-PROGRESS-WIRING-001 follow-up). A :class:`BackgroundPullJob`
owns one :class:`~brutex.dashboard.pull_progress.PullProgress` and runs the pull
work in a background thread, so the HTTP route returns immediately and the page
polls :meth:`BackgroundPullJob.snapshot` (O(1)) for the per-window tick.

**Single-flight** (CLAUDE.md §2 Rule 7 idempotence at the job level): while a
pull is running, a second :meth:`start` is a no-op returning ``False`` — two
universe pulls can never race the same lake. The running flag flips under a
:class:`threading.Lock`, so the "already running?" check + the reader's
``running`` read are O(1) and torn-state-free.

The pull *work* is injected as ``Callable[[ProgressHook], object]`` — it
receives the positional progress hook the ingest drivers already emit
(``(label, bars, failed)`` — DEC-PULL-PROGRESS-WIRING-001) and the job adapts
it onto the tracker. **Any** exception in the worker is captured and surfaced as
the job's ``error`` state (never lost, never crashes the server thread) — the
one place a broad catch is correct, mirroring the route layer. The thread
spawner is injected so the whole engine is unit-testable synchronously with no
real threads.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

from brutex.dashboard.pull_progress import ProgressSnapshot, PullProgress
from brutex.dashboard.summary_cache import CoverageCounters
from brutex.notifications.sweep_alerts import emit_pull_halt_alert

#: The positional progress hook the ingest drivers emit
#: (``label, bars, failed, gap``) — identical to
#: ``brutex.adapters.groww.run_ingest.ProgressHook`` but redeclared here so this
#: layer carries no import from the adapter package. ``gap`` flags a benign
#: DH-905 no-data boundary (NOT a failure — DEC-DHAN-NO-DATA-GAP-COUNT-001).
ProgressHook = Callable[[str, int, bool, bool], None]

#: The injected pull work — given the progress hook, run the pull (return ignored).
PullWork = Callable[[ProgressHook], object]

#: The injected thread spawner — runs *fn* off the caller's thread. The default
#: spawns a daemon thread; tests inject a synchronous runner for determinism.
Spawner = Callable[[Callable[[], None]], None]


def _is_pull_halt(exc: BaseException) -> bool:
    """True iff *exc* is a pull-wide global-stop (halts every window), not a per-symbol failure.

    Mirrors the ingest spines' ``_GLOBAL_STOP_ERRORS`` set (DEC-LAKE-WRITE-HALT-001 /
    quota). The error classes are imported INSIDE the call — not at module top — so
    :mod:`brutex.dashboard.pull_jobs` stays wheel-free at import (``quota_tracker`` is
    part of the heavy PyO3 chain), and this check only runs on the rare
    worker-exception path.
    """
    from brutex.data.errors import LakeWriteHalt
    from brutex.orchestrator.quota_tracker import DailyQuotaExceeded, QuotaInvariantError

    return isinstance(exc, (LakeWriteHalt, DailyQuotaExceeded, QuotaInvariantError))


class _Invalidatable(Protocol):
    """Minimal structural type for the summary cache's write-invalidation hook.

    Only :meth:`mark_dirty` is needed here, so the job depends on the BEHAVIOUR
    (a cache to invalidate) not the concrete
    :class:`~brutex.dashboard.summary_cache.SummaryCache` — a test double with a
    ``mark_dirty`` method satisfies it (CLAUDE.md §4 dependency injection).
    """

    def mark_dirty(self) -> None:
        """Invalidate the cache so the next eligible read recomputes."""


def _daemon_spawn(fn: Callable[[], None]) -> None:  # pragma: no cover — real-thread default
    """Run *fn* in a fire-and-forget daemon thread (the live default)."""
    threading.Thread(target=fn, daemon=True).start()


class BackgroundPullJob:
    """A single-flight background pull with an O(1) live-progress snapshot.

    Parameters
    ----------
    progress
        The :class:`PullProgress` tracker to drive (a fresh one by default).
    spawn
        Thread spawner (defaults to a daemon thread). Inject a synchronous
        runner in tests for determinism.
    counters
        Optional :class:`~brutex.dashboard.summary_cache.CoverageCounters` to
        hook into the single write-progress funnel (DEC-DASHBOARD-FASTREAD-001):
        each window's POST-DEDUP newly-written bar count is added to its label's
        running tally for the O(1) coverage headline. A fresh one is created when
        omitted, exposed via :attr:`counters`. Because the funnel passes
        ``bars == 0`` for a duplicate re-pull, the tally is idempotent for free
        (CLAUDE.md §2 Rule 7); the audit hash-chain (Rule 8) is untouched.
    summary_cache
        Optional :class:`~brutex.dashboard.summary_cache.SummaryCache` to
        invalidate (``mark_dirty``) on every write tick, so the next eligible
        coverage poll recomputes its lake summary instead of serving stale data.
        ``None`` (the default) disables that hook (the cache then falls back to
        its own TTL refresh). Injected, never a global (CLAUDE.md §4).
    """

    def __init__(
        self,
        *,
        progress: PullProgress | None = None,
        spawn: Spawner = _daemon_spawn,
        counters: CoverageCounters | None = None,
        summary_cache: _Invalidatable | None = None,
    ) -> None:
        self._progress = progress if progress is not None else PullProgress()
        self._spawn = spawn
        self._counters = counters if counters is not None else CoverageCounters()
        self._summary_cache = summary_cache
        self._lock = threading.Lock()
        self._running = False
        #: When True the per-pair hook does NOT advance the bar — an authoritative
        #: per-WINDOW ledger sync drives the counters instead (DEC-SPOT-WINDOW-PROGRESS-001).
        self._ledger_synced = False

    def start(
        self, work: PullWork, *, total: int, detail: str = "", ledger_synced: bool = False
    ) -> bool:
        """Launch *work* in the background over *total* units; return whether it started.

        Single-flight: returns ``True`` and starts the job when idle; returns
        ``False`` (a no-op) when a pull is already running. O(1).

        When *ledger_synced* is True (the bulk spot pull, DEC-SPOT-WINDOW-PROGRESS-001)
        the per-pair :meth:`_on_progress` hook only refreshes the "current" label + the
        coverage counters — the bar's completed / total / bars / failed are driven
        instead by :meth:`sync_from_ledger` off the live WindowLedger, so the bar advances
        per WINDOW (~1 s cadence) rather than per finished symbol. Default ``False`` keeps
        the per-pair advance (every other caller — options, per-symbol — is unchanged).
        """
        with self._lock:
            if self._running:
                return False
            self._running = True
        self._ledger_synced = ledger_synced
        self._progress.start(total=total, detail=detail)
        self._spawn(lambda: self._run(work))
        return True

    def _run(self, work: PullWork) -> None:
        """Worker body: run *work*, then mark the tracker done / errored."""
        try:
            work(self._on_progress)
        except Exception as exc:  # report ANY failure to the live feed, never crash the thread
            self._progress.fail(f"{type(exc).__name__}: {exc}")
            if _is_pull_halt(exc):
                # A pull-wide global stop (disk-full/OOM LakeWriteHalt, or a quota
                # stop) halts EVERY remaining window, unlike a per-symbol failure —
                # fire a best-effort CRITICAL Telegram alert so the operator knows the
                # dashboard pull STOPPED (DEC-TELEGRAM-PULL-HALT-DASHBOARD-001, the
                # dashboard twin of the CLI cmd_fetch wiring). Best-effort: a disabled
                # or failing notifier is a silent no-op and never changes the error
                # state already reported above (a generic worker error never alerts).
                emit_pull_halt_alert(exc)
        else:
            self._progress.finish(detail="done")
        finally:
            with self._lock:
                self._running = False

    def _on_progress(self, label: str, bars: int, failed: bool, gap: bool = False) -> None:
        """Adapt the drivers' positional hook onto the tracker's kw advance.

        *gap* flags a benign DH-905 no-data boundary (NOT a failure — counted
        separately so the live tally stays honest, DEC-DHAN-NO-DATA-GAP-COUNT-001).

        Also feeds the SINGLE write funnel into the incremental coverage
        counters (DEC-DASHBOARD-FASTREAD-001): *bars* is the POST-DEDUP
        newly-written count, so :meth:`CoverageCounters.add` advances the
        headline in O(1) and a duplicate re-pull (``bars == 0``) is a no-op
        (idempotent — Rule 7). When a summary cache is wired, mark it dirty so
        the next eligible poll refreshes its lake summary; the cache's TTL floor
        collapses a 200-symbol fan-out of these ticks into at most one rescan per
        window (so the funnel can't trigger a scan storm).
        """
        # Ledger-synced pulls (bulk spot) drive completed/total/bars/failed from the
        # authoritative per-window ledger via sync_from_ledger (so the bar advances per
        # WINDOW, not per finished symbol — DEC-SPOT-WINDOW-PROGRESS-001); here the
        # per-pair hook only refreshes the "current" label. Every other caller keeps the
        # per-pair advance (byte-unchanged).
        if self._ledger_synced:
            self._progress.set_current(label)
        else:
            self._progress.advance(current=label, bars=bars, failed=failed, gap=gap)
        self._counters.add(label, bars)
        if self._summary_cache is not None:
            self._summary_cache.mark_dirty()

    def sync_from_ledger(
        self, *, completed: int, total: int, bars: int, failed: int = 0, current: str = ""
    ) -> None:
        """Drive the live bar from an authoritative WindowLedger snapshot — O(1), idempotent.

        DEC-SPOT-WINDOW-PROGRESS-001. The bulk spot pull's live-sync loop calls this on a
        ~1 s cadence with the ledger's :class:`~brutex.data.window_ledger.LedgerProgress`
        counts, so the units bar + percent advance per settled WINDOW (matching the
        /coverage "settled" headline) instead of freezing between symbol completions. The
        sync is absolute, so repeated calls never double-count; it also marks any wired
        summary cache dirty so the coverage poll refreshes mid-pull even while a long
        backfill is in flight (the per-pair tick that used to do that can stall for
        minutes).
        """
        self._progress.sync(
            completed=completed, total=total, bars=bars, failed=failed, current=current
        )
        if self._summary_cache is not None:
            self._summary_cache.mark_dirty()

    @property
    def running(self) -> bool:
        """``True`` while a pull is in flight — O(1)."""
        with self._lock:
            return self._running

    @property
    def counters(self) -> CoverageCounters:
        """The incremental coverage counters fed by the write funnel — O(1)."""
        return self._counters

    def snapshot(self) -> ProgressSnapshot:
        """Return the current O(1) progress snapshot for the polling route."""
        return self._progress.snapshot()


__all__ = [
    "BackgroundPullJob",
    "ProgressHook",
    "PullWork",
    "Spawner",
]
