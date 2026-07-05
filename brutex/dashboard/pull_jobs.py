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
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Final, Protocol

import structlog

from brutex.adapters.groww.pull_singleflight import (
    PullAlreadyRunningError,
    pull_singleflight_lock,
)
from brutex.config.paths import resolve_lake_root
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

#: The injected pull work — given the progress hook, run the pull. The return
#: value is inspected ONLY for the completion headline: a Mapping with a
#: non-empty string ``"note"`` (a pull-report ``to_dict``) becomes the finish
#: detail (:func:`_finish_detail`); anything else keeps the ``"done"``
#: back-compat (DEC-PIPELINE-AUTOPREBUILD-001 chunk 3).
PullWork = Callable[[ProgressHook], object]

#: An optional zero-arg success callback fired ONCE on the worker thread when the
#: work returns WITHOUT raising (never on a failed / hung run). Used by the
#: dashboard sweep route to bump ``SummaryCache.mark_dirty`` so a completed sweep
#: invalidates the ``/results`` leaderboard cache (DEC-DASHBOARD-RESULTS-PAGE-001).
SuccessHook = Callable[[], None]

#: An optional zero-arg completion callback fired ONCE in :meth:`BackgroundPullJob._run`'s
#: ``finally`` — on success AND failure — inside the run-identity-gated block and
#: strictly BEFORE the cross-process single-flight release, so a peer acquiring
#: the freed ``flock`` can never observe the pre-completion state the hook clears.
#: Used by the pipeline route to clear the durable ``.pull_intent.json`` crash-
#: recovery sidecar (DEC-PULL-INTENT-RESUME-001): the intent survives a SIGKILL
#: (this ``finally`` never runs) but is cleared on every worker-thread exit. A
#: raising hook is logged + swallowed (the release still happens).
FinallyHook = Callable[[], None]

#: The injected thread spawner — runs *fn* off the caller's thread. The default
#: spawns a daemon thread; tests inject a synchronous runner for determinism.
Spawner = Callable[[Callable[[], None]], None]

#: The injected stall-watchdog factory — given the ``on_hung`` edge callback, build a
#: :class:`~brutex.dashboard.pull_stall.PullWatchdog`. The default builds the real one
#: (a daemon timer + the audited timeout); tests inject a synchronous / capturing factory.
WatchdogFactory = Callable[[Callable[[WorkerEvent], None]], PullWatchdog]


#: The injected lake-root resolver — returns the lake root whose account-wide pull
#: single-flight lock guards this job, or ``None`` to DISABLE the cross-process guard
#: (a dry-run / test that never touches a real account). The default delegates to the
#: COMMON :func:`brutex.config.paths.resolve_lake_root` (``$BRUTEX_LAKE_ROOT`` →
#: ``$HOME/.brutex/lake``), the same root the production pull writes into — so the guard
#: is ACTIVE by default and a second concurrent pull against the same lake is refused.
LakeRootResolver = Callable[[], "Path | None"]

#: The injected single-flight lock factory — given the lake root, returns a context
#: manager that holds the account-wide cross-process pull lock for its lifetime (and
#: raises :class:`~brutex.adapters.groww.pull_singleflight.PullAlreadyRunningError` on
#: ``__enter__`` if a peer process already holds it). The default is the live
#: :func:`~brutex.adapters.groww.pull_singleflight.pull_singleflight_lock`; tests inject
#: a fake to exercise the held / contended paths without a real ``flock``.
SingleflightFactory = Callable[[Path], AbstractContextManager[None]]


def _default_watchdog_factory(on_hung: Callable[[WorkerEvent], None]) -> PullWatchdog:
    """Build the live :class:`PullWatchdog` (daemon timer + audited timeout)."""
    return PullWatchdog(on_hung=on_hung)


def _default_lake_root_resolver() -> Path | None:
    """Resolve the account-wide lock's lake root via the COMMON config resolver.

    Delegates to :func:`brutex.config.paths.resolve_lake_root`
    (``$BRUTEX_LAKE_ROOT`` → ``$HOME/.brutex/lake``) — the SAME root the production pull
    writes into — so the cross-process single-flight guard is ACTIVE by default and a
    second concurrent pull against the same lake is refused. O(1) (pure path
    computation; no disk touched).
    """
    return resolve_lake_root()


#: Defensive cap on a completion headline taken from a pull's returned note —
#: the snapshot is polled every few seconds, so an unexpectedly huge note must
#: never bloat every poll (the pipeline notes are one-liners in practice).
_FINISH_DETAIL_MAX: Final[int] = 500


def _finish_detail(result: object) -> str:
    """The job's completion headline, derived from the work's return value — pure.

    DEC-PIPELINE-AUTOPREBUILD-001 chunk 3 (headline honesty): a pull work that
    returns a Mapping carrying a non-empty string ``"note"`` (the pull-report
    ``to_dict`` shape — e.g. the pipeline's "pipeline complete — N symbol(s)
    quarantined: …") gets THAT note as the finish detail, defensively truncated
    to :data:`_FINISH_DETAIL_MAX` chars — so a quarantined symbol never hides
    behind a bare "done". Every other shape (``None``, a non-Mapping, a missing
    / empty / non-string note) keeps the back-compat ``"done"``.
    """
    if isinstance(result, Mapping):
        note = result.get("note")
        if isinstance(note, str) and note.strip():
            return note[:_FINISH_DETAIL_MAX]
    return "done"


def is_pull_halt(exc: BaseException) -> bool:
    """True iff *exc* is a pull-wide global-stop (halts every window), not a per-symbol failure.

    Mirrors the ingest spines' ``_GLOBAL_STOP_ERRORS`` set (DEC-LAKE-WRITE-HALT-001 /
    quota / DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001 / the TERMINAL Groww auth failure — a
    dead credential / an exhausted re-mint budget, DEC-GROWW-AUTH-HALT-001, consulted
    via :func:`~brutex.adapters.groww.errors.is_terminal_auth_failure` so the RETRYABLE
    auth-family subclasses NEVER read as a halt / DEC-VENDOR-DOWN-BREAKER-001 — the
    vendor-down error-storm + dead-credential stops, ``GrowwCredentialDeadError`` being
    the TERMINAL auth escalation the predicate also matches). The error classes are
    imported INSIDE the call — not at module top — so
    :mod:`brutex.dashboard.pull_jobs` stays wheel-free at import (``quota_tracker`` is
    part of the heavy PyO3 chain), and this check only runs on the rare
    worker-exception path.

    PUBLIC — the ONE COMMON global-stop predicate for the dashboard job layer
    (CLAUDE.md §11, no second hardcoded tuple):
    :func:`~brutex.dashboard.pipeline_prebuild.run_pipeline_prebuild_guarded`
    consults it so a global-stop raised during the prebuild stage RE-RAISES out
    of the guard and reaches :meth:`BackgroundPullJob._run`'s halt handling
    (the CRITICAL Telegram alert below) instead of being swallowed into a
    benign "built 0" outcome.
    """
    from brutex.adapters.empty_circuit_breaker import MassEmptySettleHalt
    from brutex.adapters.error_storm_breaker import VendorDownError
    from brutex.adapters.groww.errors import is_terminal_auth_failure
    from brutex.data.errors import LakeWriteHalt
    from brutex.orchestrator.quota_tracker import DailyQuotaExceeded, QuotaInvariantError

    return isinstance(
        exc,
        (
            LakeWriteHalt,
            DailyQuotaExceeded,
            QuotaInvariantError,
            MassEmptySettleHalt,
            VendorDownError,
        ),
    ) or is_terminal_auth_failure(exc)


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
        (a daemon timer + the audited timeout). A silent worker is then detected: the hung
        edge fires a CRITICAL pull-halt alert AND marks the job STALLED-SUSPECT — an honest
        DEGRADED observation (DEC-PIPELINE-STALL-HEARTBEAT-001) that deliberately does NOT
        fail the run or release the single-flight while the worker thread is still alive
        (the forensically-proven false-kill + double-run race of the prior release-on-hung
        design). A later heartbeat fires the recovery edge and clears the marker. Injected,
        never a global (CLAUDE.md §4); tests inject a synchronous / capturing factory for
        determinism.
    lake_root_resolver
        Optional resolver for the account-wide single-flight lock's lake root
        (DEC-ACCOUNT-PULL-SINGLEFLIGHT-001). The default delegates to the COMMON
        :func:`brutex.config.paths.resolve_lake_root` (``$BRUTEX_LAKE_ROOT`` →
        ``$HOME/.brutex/lake``), so the cross-process guard is ACTIVE in production: a
        second pull against the same lake root — e.g. a CLI ``brutex fetch`` while a
        dashboard pull is running — is REFUSED before it can double the Groww account
        request rate (Golden Rule #6). Returning ``None`` DISABLES the guard (a dry-run /
        test that never touches a real account). Injected, never a global (CLAUDE.md §4).
    singleflight_factory
        Optional factory building the account-wide cross-process single-flight lock
        context manager from the resolved lake root (default the live
        :func:`~brutex.adapters.groww.pull_singleflight.pull_singleflight_lock`, backed by
        a non-blocking ``fcntl.flock``). Tests inject a fake to exercise the held /
        contended paths deterministically. Injected, never a global (CLAUDE.md §4).
    """

    def __init__(
        self,
        *,
        progress: PullProgress | None = None,
        spawn: Spawner = _daemon_spawn,
        counters: CoverageCounters | None = None,
        summary_cache: _Invalidatable | None = None,
        watchdog_factory: WatchdogFactory = _default_watchdog_factory,
        lake_root_resolver: LakeRootResolver | None = None,
        singleflight_factory: SingleflightFactory = pull_singleflight_lock,
    ) -> None:
        self._progress = progress if progress is not None else PullProgress()
        self._spawn = spawn
        self._counters = counters if counters is not None else CoverageCounters()
        self._summary_cache = summary_cache
        self._watchdog_factory = watchdog_factory
        #: Resolver for the account-wide single-flight lock's lake root (or, when the
        #: resolver itself returns ``None``, disables the cross-process guard —
        #: DEC-ACCOUNT-PULL-SINGLEFLIGHT-001). ``lake_root_resolver=None`` (the default)
        #: falls back to the COMMON config resolver, so the guard is ACTIVE in production;
        #: tests inject their own resolver (e.g. ``lambda: tmp_path`` or ``lambda: None``).
        self._lake_root_resolver: LakeRootResolver = (
            _default_lake_root_resolver if lake_root_resolver is None else lake_root_resolver
        )
        #: Factory building the account-wide cross-process single-flight lock context
        #: manager (default the live ``fcntl.flock``-backed one); injected for tests.
        self._singleflight_factory = singleflight_factory
        self._lock = threading.Lock()
        self._running = False
        #: The CURRENT run's account-wide single-flight lock context manager (entered in
        #: :meth:`start`, exited in :meth:`_run`'s ``finally``). ``None`` between runs and
        #: when the guard is disabled (no resolvable lake root). Held for the WHOLE pull so
        #: a peer process cannot start a second concurrent pull (Golden Rule #6).
        self._singleflight: AbstractContextManager[None] | None = None
        #: When True the per-pair hook does NOT advance the bar — an authoritative
        #: per-WINDOW ledger sync drives the counters instead (DEC-SPOT-WINDOW-PROGRESS-001).
        self._ledger_synced = False
        #: Phase-scoped override of :attr:`_ledger_synced` (DEC-PIPELINE-STALL-HEARTBEAT-001,
        #: the frozen-counter fix): when True the per-pair hook DOES advance the counters
        #: (open-ended — the total grows with the completed count) even on a
        #: ``ledger_synced`` run, because the CURRENT phase has NO ledger-sync driver.
        #: The pipeline's F&O historical phase sets it via
        #: ``set_phase("historical", pair_progress=True)`` so the units + bars tallies
        #: keep moving instead of freezing at the metadata phase's counts.
        self._pair_progress = False
        #: The CURRENT run's stall watchdog (built per :meth:`start`, stopped in ``finally``).
        #: ``None`` between runs. Serves two jobs under :attr:`_lock`: (a) the worker thread's
        #: heartbeat (:meth:`beat`), and (b) per-RUN IDENTITY — both :meth:`_on_hung` and the
        #: worker's ``finally`` clear ``_running`` ONLY when ``self._watchdog is`` THEIR run's
        #: watchdog, so a stale (later-unwedged) worker can never clobber a NEWER run's state
        #: (DEC-PULL-STALL-DETECTION-001 hostile-review BLOCKER fix). A fresh per-run watchdog
        #: object is the run-identity token; no separate flag is needed.
        self._watchdog: PullWatchdog | None = None

    def start(
        self,
        work: PullWork,
        *,
        total: int,
        detail: str = "",
        ledger_synced: bool = False,
        on_success: SuccessHook | None = None,
        on_finally: FinallyHook | None = None,
    ) -> bool:
        """Launch *work* in the background over *total* units; return whether it started.

        Single-flight: returns ``True`` and starts the job when idle; returns
        ``False`` (a no-op) when a pull is already running. O(1).

        Two single-flight layers (DEC-ACCOUNT-PULL-SINGLEFLIGHT-001):

        1. **In-process** — the ``threading.Lock`` + ``_running`` flag refuses a second
           pull within THIS process (a second dashboard click).
        2. **Cross-process** — an account-wide ``fcntl.flock`` (resolved via
           *lake_root_resolver*, taken via *singleflight_factory*) refuses a second pull
           in ANOTHER process (e.g. a CLI ``brutex fetch`` while a dashboard pull runs).
           Without it each process gets its OWN rate-controller budget, so the Groww
           account would see the SUM of both rates → a 429 storm / ban risk (Golden
           Rule #6). On contention the acquire raises
           :class:`~brutex.adapters.groww.pull_singleflight.PullAlreadyRunningError`; this
           method then rolls back the in-process flag, marks the progress failed with a
           clear "a pull is already running" message, logs it, and returns ``False`` — so
           the second pull is refused, never silently doubling the account rate. When the
           resolver returns ``None`` (a dry-run / test) the cross-process guard is skipped.
           The lock is held for the WHOLE pull and released in :meth:`_run`'s ``finally``.

        When *ledger_synced* is True (the bulk spot pull, DEC-SPOT-WINDOW-PROGRESS-001)
        the per-pair :meth:`_on_progress` hook only refreshes the "current" label + the
        coverage counters — the bar's completed / total / bars / failed are driven
        instead by :meth:`sync_from_ledger` off the live WindowLedger, so the bar advances
        per WINDOW (~1 s cadence) rather than per finished symbol. Default ``False`` keeps
        the per-pair advance (every other caller — options, per-symbol — is unchanged).

        When *on_success* is given it is fired ONCE on the worker thread iff the work
        returns WITHOUT raising — never on a failed / hung run. The dashboard sweep
        route passes ``_summary_cache.mark_dirty`` so a SUCCESSFUL sweep bumps the
        write-generation and the next ``/results`` poll re-reads the new winners
        (DEC-DASHBOARD-RESULTS-PAGE-001). A success callback that raises is logged +
        swallowed so it can never crash the worker thread.

        When *on_finally* is given it is fired ONCE in :meth:`_run`'s ``finally`` —
        success AND failure alike — inside the run-identity-gated block, strictly
        BEFORE the single-flight release (DEC-PULL-INTENT-RESUME-001: the pipeline
        route clears its durable ``.pull_intent.json`` here, so a peer process
        acquiring the freed ``flock`` never sees a live intent). It does NOT fire
        when this ``start`` is refused (single-flight contention — no worker runs)
        nor after a SIGKILL (the durable-intent survival that powers the resume).
        A raising hook is logged + swallowed; the release still happens.
        """
        # Build the per-run stall watchdog BEFORE the lock (DEC-PULL-STALL-DETECTION-001):
        # the worker beats it on real progress (per window / per post-fetch unit via the
        # emit_progress_beat seams), and its daemon timer declares the worker HUNG if no
        # beat lands for the audited timeout — so a wedged worker is still detected +
        # alerted and the job is marked STALLED-SUSPECT (the single-flight stays HELD
        # while the thread is alive — DEC-PIPELINE-STALL-HEARTBEAT-001). Building it here
        # is side-effect-free
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
        # Cross-process account-wide single-flight gate (DEC-ACCOUNT-PULL-SINGLEFLIGHT-001).
        # Taken AFTER the in-process flag (so a same-process double-click is filtered by the
        # cheap flag first) but BEFORE any work is spawned. On contention a peer process is
        # already pulling: roll back the in-process flag we just set, refuse this pull, and
        # report it — never spawn a second pull that would double the account request rate.
        # A resolver returning None DISABLES the guard (dry-run / test). Done outside
        # ``self._lock`` — it does (fast, non-blocking) filesystem I/O, and the flag is
        # already claimed so no other start can race in.
        if not self._acquire_singleflight():
            with self._lock:
                # Roll back the in-process flag we just claimed. No identity gate is needed
                # here (unlike _run / _on_hung): _acquire_singleflight runs SYNCHRONOUSLY in
                # this same start() call and never spawns / publishes a worker, so no other
                # run can have superseded this one's watchdog between claiming the flag and
                # here — this run unconditionally still owns it.
                self._running = False
            return False
        self._ledger_synced = ledger_synced
        self._pair_progress = False  # per-run reset; a phase opts in via set_phase
        self._progress.start(total=total, detail=detail)
        # Wire the HUNG→HEALTHY recovery edge so a heartbeat landing after a HUNG
        # declaration clears the stalled-suspect marker (DEC-PIPELINE-STALL-HEARTBEAT-001).
        # Set post-construction so the injected factory signature stays (on_hung) —
        # back-compat with every existing factory.
        watchdog.on_recovery = lambda event: self._on_recovered(event, watchdog)
        watchdog.start()
        self._spawn(lambda: self._run(work, watchdog, on_success, on_finally))
        return True

    def _acquire_singleflight(self) -> bool:
        """Acquire the account-wide cross-process pull lock; return whether it was taken.

        Resolves the lake root via :attr:`_lake_root_resolver`; a ``None`` root DISABLES
        the guard (a dry-run / test) and returns ``True`` (nothing to hold). Otherwise it
        ``__enter__``-s the :attr:`_singleflight_factory` context manager and stashes it in
        :attr:`_singleflight` so :meth:`_run` can release it in its ``finally``. On
        :class:`~brutex.adapters.groww.pull_singleflight.PullAlreadyRunningError` (a peer
        process is mid-pull) it marks the progress failed with the actionable message,
        logs it, and returns ``False`` so the caller refuses the pull (Golden Rule #6).
        """
        lake_root = self._lake_root_resolver()
        if lake_root is None:
            self._singleflight = None
            return True
        guard = self._singleflight_factory(lake_root)
        try:
            guard.__enter__()
        except PullAlreadyRunningError as exc:
            self._singleflight = None
            self._progress.fail(f"{type(exc).__name__}: {exc}")
            _LOG.warning(
                "pull_job.singleflight_refused",
                lake_root=str(lake_root),
                reason="a pull is already running for this lake",
            )
            return False
        self._singleflight = guard
        return True

    def _release_singleflight(self) -> None:
        """Release the account-wide single-flight lock held by the current run, if any.

        Called from :meth:`_run`'s ``finally`` (and only there). ``__exit__``-s the held
        guard (closing the ``flock`` fd) so a peer process can start the next pull, then
        clears :attr:`_singleflight`. A ``None`` guard (disabled / never-taken) is a no-op.
        Best-effort: any error closing the lock is logged + swallowed so it can never
        crash the worker thread or leave the in-process flag stuck.
        """
        guard = self._singleflight
        self._singleflight = None
        if guard is None:
            return
        try:
            guard.__exit__(None, None, None)
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the worker
            _LOG.exception("pull_job.singleflight_release failed; the pull is unaffected")

    def _on_hung(self, event: WorkerEvent, watchdog: PullWatchdog) -> None:
        """Watchdog HEALTHY→HUNG edge for *watchdog*'s run: alert + mark STALLED-SUSPECT.

        DEC-PIPELINE-STALL-HEARTBEAT-001 (supersedes the release-on-hung arm of
        DEC-PULL-STALL-DETECTION-001). The pull worker has not beaten the heartbeat for
        the audited timeout. FORENSICALLY PROVEN: with the heartbeat wired only to the
        fetch phases, a healthy post-fetch stage (Greeks enrichment / timeframe prebuild /
        movers warm) was silent for > timeout, the old edge handler FAILED the progress
        AND cleared ``_running`` + released the account single-flight — while the worker
        thread kept running and SUCCEEDED. Two harms: a false FAILED headline on a
        succeeding run, and the released single-flight admitting a SECOND pull concurrent
        with the still-running zombie worker (a double-run race on the same lake).

        The honest edge now: (1) fire the best-effort CRITICAL pull-halt alert so the
        operator learns IN REAL TIME, (2) mark the progress STALLED-SUSPECT (an honest
        DEGRADED marker naming the silent phase — the state stays ``running``), and
        (3) deliberately do NOT clear ``_running`` / release the single-flight: the worker
        thread is (or may be) still alive and still owns the lake — releasing here is
        exactly the double-run race. The single-flight is released ONLY by :meth:`_run`'s
        ``finally`` when the thread actually exits; a wedge that never exits keeps the
        flag held (honest — the zombie still owns the lake; a process restart releases the
        ``flock`` on fd close, and the CRITICAL alert + marker make the wedge LOUD, never
        silent). The timer keeps polling so a later heartbeat (the greeks/prebuild/movers
        seams now beat — the false-positive class is closed) fires the recovery edge and
        :meth:`_on_recovered` clears the marker. Identity-gated: a late edge for a
        superseded run never marks a newer run.

        The whole body is wrapped in its own ``try/except`` (best-effort): an alert or
        progress call that raises is logged + swallowed so it can never kill the daemon
        timer — independent of the underlying watchdog's narrower catch.
        """
        try:
            phase = self._progress.snapshot().phase or "unknown"
            with self._lock:
                # Identity gate: mark ONLY when THIS run is still current — held across
                # the mark so a stale timer edge can never race a newer run's tracker.
                # (The progress lock nests inside the job lock; the reverse order never
                # occurs, so no deadlock.) A superseded run is silently dropped.
                if self._watchdog is not watchdog:
                    return
                self._progress.mark_stalled(
                    f"stalled-suspect — no heartbeat for {event.last_beat_seconds_ago:.0f}s "
                    f"(timeout {event.timeout_seconds:.0f}s) during phase '{phase}'; "
                    "worker still running (single-flight held)"
                )
            emit_pull_halt_alert(
                TimeoutError(
                    f"dashboard pull stalled-suspect: no heartbeat for "
                    f"{event.last_beat_seconds_ago:.0f}s (timeout {event.timeout_seconds:.0f}s) "
                    f"during phase '{phase}'; worker still running — NOT killed"
                )
            )
            _LOG.warning(
                "pull_stall.stalled_suspect",
                phase=phase,
                last_beat_seconds_ago=round(event.last_beat_seconds_ago, 1),
                timeout_seconds=event.timeout_seconds,
            )
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the timer
            _LOG.exception("pull_stall.on_hung failed; the pull / timer is unaffected")

    def _on_recovered(self, event: WorkerEvent, watchdog: PullWatchdog) -> None:
        """Watchdog HUNG→HEALTHY edge: clear the stalled-suspect marker (identity-gated).

        DEC-PIPELINE-STALL-HEARTBEAT-001: a heartbeat landed again after a HUNG
        declaration (e.g. the prebuild seam resumed beating), so the honest DEGRADED
        marker is lifted. Best-effort — a raising progress call is swallowed + logged.
        """
        try:
            with self._lock:
                if self._watchdog is not watchdog:
                    return
                self._progress.clear_stalled()
            _LOG.info(
                "pull_stall.stalled_recovered",
                last_beat_seconds_ago=round(event.last_beat_seconds_ago, 1),
            )
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the timer
            _LOG.exception("pull_stall.on_recovered failed; the pull / timer is unaffected")

    def _run(
        self,
        work: PullWork,
        watchdog: PullWatchdog,
        on_success: SuccessHook | None = None,
        on_finally: FinallyHook | None = None,
    ) -> None:
        """Worker body: run *work*, then mark the tracker done / errored.

        *watchdog* is THIS run's stall watchdog (passed in by :meth:`start`, so it is never
        ``None`` here), stopped in the ``finally`` once the worker has returned / raised.

        *on_success* (when given) fires ONCE on the success branch — after the work
        returns without raising and the progress is marked done — and never on the
        failure branch, so a completed sweep can invalidate the ``/results`` cache
        while a failed sweep leaves it untouched (DEC-DASHBOARD-RESULTS-PAGE-001).

        *on_finally* (when given) fires ONCE in the ``finally`` — success AND
        failure — inside the identity-gated block, BEFORE the single-flight
        release (DEC-PULL-INTENT-RESUME-001: clear the durable pull intent so a
        peer acquiring the freed lock never sees a live intent). Best-effort:
        a raising hook is logged + swallowed and the release still happens.
        """
        # Install the process-global liveness beat for this run (DEC-PULL-CONTENTION-
        # ANTISTALL-001 GAP #15): the bounded partition lock-wait + the rate pacer fire
        # ``emit_progress_beat`` on the FAN-OUT worker threads, which reaches this beat
        # and keeps the watchdog alive while a pull is slow-but-progressing under lock
        # contention — so the watchdog never false-positive-kills it. Removed in the
        # ``finally`` so a finished run leaves no stale beat.
        register_progress_beat(self._beat)
        try:
            result = work(self._on_progress)
        except Exception as exc:  # report ANY failure to the live feed, never crash the thread
            self._progress.fail(f"{type(exc).__name__}: {exc}")
            if is_pull_halt(exc):
                # A pull-wide global stop (disk-full/OOM LakeWriteHalt, or a quota
                # stop) halts EVERY remaining window, unlike a per-symbol failure —
                # fire a best-effort CRITICAL Telegram alert so the operator knows the
                # dashboard pull STOPPED (DEC-TELEGRAM-PULL-HALT-DASHBOARD-001, the
                # dashboard twin of the CLI cmd_fetch wiring). Best-effort: a disabled
                # or failing notifier is a silent no-op and never changes the error
                # state already reported above (a generic worker error never alerts).
                emit_pull_halt_alert(exc)
        else:
            # Honest completion headline (DEC-PIPELINE-AUTOPREBUILD-001 chunk 3):
            # a work returning a report dict with a non-empty "note" surfaces it
            # (e.g. "pipeline complete — N symbol(s) quarantined: …"); any other
            # return shape keeps the back-compat "done".
            self._progress.finish(detail=_finish_detail(result))
            if on_success is not None:
                # Fire the success-only completion hook (e.g. the sweep route's
                # mark_dirty). Best-effort: a raising callback is logged + swallowed
                # so it can never crash the worker thread or leave the job stuck with
                # _running set (the finally below still clears it).
                try:
                    on_success()
                except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the worker
                    _LOG.exception("pull_job.on_success failed; the completed pull is unaffected")
        finally:
            # Remove this run's liveness beat so a later poll can never fire into a dead
            # watchdog (idempotent — DEC-PULL-CONTENTION-ANTISTALL-001 GAP #15).
            unregister_progress_beat()
            # Stop this run's stall timer first (so it can never fire after the worker has
            # already returned / raised). Outside the lock: stop() only sets an Event,
            # never blocks. Idempotent with _on_hung's stop() (both target the SAME watchdog).
            watchdog.stop()
            with self._lock:
                # Identity gate (defence in depth): clear ``_running`` ONLY when THIS run
                # is still the current one. Under DEC-PIPELINE-STALL-HEARTBEAT-001 the
                # watchdog no longer clears the flag (a hung-declared run keeps it until
                # this finally actually executes), so this finally is the ONE release
                # path and the gate is normally always true — kept as a cheap invariant
                # guard so a future re-introduction of an out-of-band clear can never
                # let a stale worker clobber a newer run.
                if self._watchdog is watchdog:
                    self._running = False
                    # Fire the completion hook (DEC-PULL-INTENT-RESUME-001: the durable
                    # pull-intent clear) INSIDE the identity gate — a superseded stale
                    # worker must never clear a NEWER run's intent — and strictly BEFORE
                    # the single-flight release below, so a peer process acquiring the
                    # freed flock can never observe a live intent for a finished pull.
                    # Best-effort: a raising hook is logged + swallowed so it can never
                    # leave the flock held or the flag stuck.
                    if on_finally is not None:
                        try:
                            on_finally()
                        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the worker
                            _LOG.exception(
                                "pull_job.on_finally failed; the single-flight release continues"
                            )
                    # Release the account-wide cross-process single-flight lock under the
                    # SAME identity gate (DEC-ACCOUNT-PULL-SINGLEFLIGHT-001), so a peer
                    # process can start the next pull — released HERE and only here, when
                    # the worker thread actually exits (never by the watchdog while the
                    # thread may still be writing — the double-run race).
                    self._release_singleflight()

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
        # per-pair advance (byte-unchanged). A phase with NO ledger-sync driver (the
        # pipeline's F&O historical fill) opts back into per-pair counting via
        # ``set_phase(..., pair_progress=True)`` — OPEN-ENDED (the total grows with the
        # completed count, since the pair total is unknown at this layer) so the units +
        # bars tallies keep moving instead of freezing at the metadata phase's counts
        # (DEC-PIPELINE-STALL-HEARTBEAT-001, the frozen-counter fix). O(1) per event.
        if self._ledger_synced and not self._pair_progress:
            self._progress.set_current(label)
        elif self._ledger_synced:
            self._progress.advance(
                current=label, bars=bars, failed=failed, gap=gap, grow_total=True
            )
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
        self, *, completed: int, total: int, bars: int | None, failed: int = 0, current: str = ""
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

        ``bars=None`` preserves the cumulative bar tally (DEC-PIPELINE-STALL-HEARTBEAT-001):
        a sync source with no bar concept — the metadata pull — no longer wipes the bars an
        earlier phase already streamed.
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

    def set_phase(self, phase: str, *, pair_progress: bool = False) -> None:
        """Set the COARSE pipeline phase currently in flight — O(1) passthrough.

        DEC-UNIVERSE-PER-PHASE-PROGRESS-001. The one-click pipeline's ``_work`` calls
        this at each phase transition so the live Universe per-phase view highlights the
        running phase; it delegates straight to
        :meth:`~brutex.dashboard.pull_progress.PullProgress.set_phase` (a label only, no
        counter is keyed by it).

        *pair_progress* (DEC-PIPELINE-STALL-HEARTBEAT-001, the frozen-counter fix):
        ``True`` declares the ENTERING phase has NO ledger-sync driver, so the per-pair
        hook advances the counters open-endedly even on a ``ledger_synced`` run (the
        pipeline's F&O historical phase). Default ``False`` keeps every other phase /
        caller byte-identical (the ledger sync stays authoritative).
        """
        self._pair_progress = pair_progress
        self._progress.set_phase(phase)

    def snapshot(self) -> ProgressSnapshot:
        """Return the current O(1) progress snapshot for the polling route."""
        return self._progress.snapshot()


__all__ = [
    "BackgroundPullJob",
    "FinallyHook",
    "LakeRootResolver",
    "ProgressHook",
    "PullWork",
    "SingleflightFactory",
    "Spawner",
    "SuccessHook",
    "WatchdogFactory",
    "is_pull_halt",
]
