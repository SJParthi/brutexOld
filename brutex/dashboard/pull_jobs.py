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

import structlog

from brutex.dashboard.pull_progress import ProgressSnapshot, PullProgress
from brutex.dashboard.pull_stall import PullWatchdog
from brutex.dashboard.summary_cache import CoverageCounters
from brutex.notifications.sweep_alerts import emit_pull_halt_alert
from brutex.observability.progress_beat import (
    register_progress_beat,
    unregister_progress_beat,
)
from brutex.observability.watchdog import WorkerEvent

_LOG = structlog.get_logger(__name__)

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

#: The injected stall-watchdog factory — given the ``on_hung`` edge callback, build a
#: :class:`~brutex.dashboard.pull_stall.PullWatchdog`. The default builds the real one
#: (a daemon timer + the audited timeout); tests inject a synchronous / capturing factory.
WatchdogFactory = Callable[[Callable[[WorkerEvent], None]], PullWatchdog]


def _default_watchdog_factory(on_hung: Callable[[WorkerEvent], None]) -> PullWatchdog:
    """Build the live :class:`PullWatchdog` (daemon timer + audited timeout)."""
    return PullWatchdog(on_hung=on_hung)


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
    watchdog_factory
        Optional factory building the per-run stall watchdog (DEC-PULL-STALL-DETECTION-001).
        Given the ``on_hung`` edge callback it returns a
        :class:`~brutex.dashboard.pull_stall.PullWatchdog`; the default builds the live one
        (a daemon timer + the audited timeout). A HUNG worker — which never reaches the
        worker's ``finally`` to clear ``_running`` — is then detected: the hung edge fires a
        CRITICAL pull-halt alert AND transitions the job to its error state + clears
        ``_running`` so future pulls are not silently refused (the single-flight guard's
        forever-stuck failure mode). Injected, never a global (CLAUDE.md §4); tests inject
        a synchronous / capturing factory for determinism.
    """

    def __init__(
        self,
        *,
        progress: PullProgress | None = None,
        spawn: Spawner = _daemon_spawn,
        counters: CoverageCounters | None = None,
        summary_cache: _Invalidatable | None = None,
        watchdog_factory: WatchdogFactory = _default_watchdog_factory,
    ) -> None:
        self._progress = progress if progress is not None else PullProgress()
        self._spawn = spawn
        self._counters = counters if counters is not None else CoverageCounters()
        self._summary_cache = summary_cache
        self._watchdog_factory = watchdog_factory
        self._lock = threading.Lock()
        self._running = False
        #: When True the per-pair hook does NOT advance the bar — an authoritative
        #: per-WINDOW ledger sync drives the counters instead (DEC-SPOT-WINDOW-PROGRESS-001).
        self._ledger_synced = False
        #: The CURRENT run's stall watchdog (built per :meth:`start`, stopped in ``finally``).
        #: ``None`` between runs. Serves two jobs under :attr:`_lock`: (a) the worker thread's
        #: heartbeat (:meth:`beat`), and (b) per-RUN IDENTITY — both :meth:`_on_hung` and the
        #: worker's ``finally`` clear ``_running`` ONLY when ``self._watchdog is`` THEIR run's
        #: watchdog, so a stale (later-unwedged) worker can never clobber a NEWER run's state
        #: (DEC-PULL-STALL-DETECTION-001 hostile-review BLOCKER fix). A fresh per-run watchdog
        #: object is the run-identity token; no separate flag is needed.
        self._watchdog: PullWatchdog | None = None

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
        # Build the per-run stall watchdog BEFORE the lock (DEC-PULL-STALL-DETECTION-001):
        # the worker beats it on real progress (per window), and its daemon timer declares
        # the worker HUNG if no beat lands for the audited timeout — so a wedged worker (which
        # never reaches the finally below to clear ``_running``) is still detected + alerted
        # and the single-flight flag is released. Building it here is side-effect-free
        # (``.start()`` is NOT called on the ``return False`` path → no leaked timer); each run
        # gets a fresh heartbeat / timer with no cross-run state. The watchdog object IS this
        # run's identity token: both _on_hung and _run's finally gate their ``_running`` clear
        # on ``self._watchdog is`` this watchdog (under the lock), so they only ever act on
        # the CURRENT run (a stale worker can never clobber a newer run — the BLOCKER fix).
        watchdog = self._watchdog_factory(lambda event: self._on_hung(event, watchdog))
        with self._lock:
            if self._running:
                return False
            self._running = True
            # Publish the run-identity token ATOMICALLY with the single-flight flag, under
            # the SAME lock the gates read it under. Otherwise a stale worker A's finally
            # could acquire the lock in the gap between ``_running = True`` and a later
            # unlocked ``self._watchdog = watchdog``, still see ``self._watchdog is wd_A`` →
            # clear ``_running`` → admit a third pull concurrently (the residual race).
            self._watchdog = watchdog
        self._ledger_synced = ledger_synced
        self._progress.start(total=total, detail=detail)
        watchdog.start()
        self._spawn(lambda: self._run(work, watchdog))
        return True

    def _on_hung(self, event: WorkerEvent, watchdog: PullWatchdog) -> None:
        """Watchdog HEALTHY→HUNG edge for *watchdog*'s run: alert + release the stuck pull.

        The pull worker has not beaten the heartbeat for the audited timeout — it is wedged
        (e.g. on the no-timeout partition flock, a deadlocked socket). Unlike a per-symbol
        failure this leaves the worker thread blocked forever, so it can never reach
        :meth:`_run`'s ``finally`` to clear ``_running`` — the single-flight guard would
        then SILENTLY refuse every future pull. On the timer thread we (1) stop *watchdog*'s
        own poll loop so its daemon thread cannot keep firing forever (the timer leaks on the
        one path it exists for otherwise), (2) fire a best-effort CRITICAL pull-halt alert so
        the operator learns IN REAL TIME, (3) mark the progress failed so the dashboard
        reflects the stall, and (4) clear ``_running`` — but ONLY if THIS run is still the
        current one (``self._watchdog is watchdog`` under the lock), so a late-fired timer for
        a run already superseded by a newer pull can never clobber the newer run.

        The whole body is wrapped in its own ``try/except`` (best-effort): a ``progress.fail``
        or alert that raises is logged + swallowed so it can never kill the daemon timer or
        leave a half-applied state — independent of the underlying watchdog's narrower catch.
        """
        try:
            watchdog.stop()  # MAJOR-2: stop this run's timer so its daemon does not leak
            emit_pull_halt_alert(
                TimeoutError(
                    f"dashboard pull stalled: no progress for {event.last_beat_seconds_ago:.0f}s "
                    f"(timeout {event.timeout_seconds:.0f}s)"
                )
            )
            self._progress.fail(
                f"pull stalled — no progress for {event.last_beat_seconds_ago:.0f}s "
                f"(timeout {event.timeout_seconds:.0f}s)"
            )
            with self._lock:
                # Identity gate: clear ``_running`` ONLY when THIS run is still current. A
                # newer pull may already own the flag (this timer fired late) — never touch it.
                if self._watchdog is watchdog:
                    self._running = False
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the timer
            _LOG.exception("pull_stall.on_hung failed; the pull / timer is unaffected")

    def _run(self, work: PullWork, watchdog: PullWatchdog) -> None:
        """Worker body: run *work*, then mark the tracker done / errored.

        *watchdog* is THIS run's stall watchdog (passed in by :meth:`start`, so it is never
        ``None`` here), stopped in the ``finally`` once the worker has returned / raised.
        """
        # Install the process-global liveness beat for this run (DEC-PULL-CONTENTION-
        # ANTISTALL-001 GAP #15): the bounded partition lock-wait + the rate pacer fire
        # ``emit_progress_beat`` on the FAN-OUT worker threads, which reaches this beat
        # and keeps the watchdog alive while a pull is slow-but-progressing under lock
        # contention — so the watchdog never false-positive-kills it. Removed in the
        # ``finally`` so a finished run leaves no stale beat.
        register_progress_beat(self._beat)
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
            # Remove this run's liveness beat so a later poll can never fire into a dead
            # watchdog (idempotent — DEC-PULL-CONTENTION-ANTISTALL-001 GAP #15).
            unregister_progress_beat()
            # Stop this run's stall timer first (so it can never fire after the worker has
            # already returned / raised). Outside the lock: stop() only sets an Event,
            # never blocks. Idempotent with _on_hung's stop() (both target the SAME watchdog).
            watchdog.stop()
            with self._lock:
                # Identity gate (the BLOCKER fix): clear ``_running`` ONLY when THIS run is
                # still the current one. If the watchdog already declared this run hung it has
                # cleared ``_running`` and a NEWER pull may now be in flight owning the flag —
                # ``self._watchdog is watchdog`` is False then, so a stale (later-unwedged)
                # worker can never clobber the newer run. When this IS the current run the
                # worker owns the flag and clears it normally.
                if self._watchdog is watchdog:
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
        # Heartbeat the stall watchdog on REAL progress (a fetched pair / window). A
        # slow-but-progressing pull keeps beating, so it never false-positives; only a
        # worker with NO progress for the timeout is declared hung (DEC-PULL-STALL-DETECTION-001).
        self._beat()

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
        # A per-window ledger sync is the bulk pull's freshest progress signal — beat the
        # stall watchdog so a long-but-advancing backfill never trips the hung detector
        # (DEC-PULL-STALL-DETECTION-001).
        self._beat()

    def _beat(self) -> None:
        """Beat the current run's stall watchdog, if one is wired — O(1)."""
        if self._watchdog is not None:
            self._watchdog.beat()

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
    "WatchdogFactory",
]
