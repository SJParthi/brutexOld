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
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

import structlog

from brutex.adapters.groww.pull_singleflight import (
    PullAlreadyRunningError,
    account_pull_singleflight_lock,
    pull_lease_guard,
    pull_singleflight_lock,
)
from brutex.calendars.market_hours import guard_active_default
from brutex.config.paths import resolve_lake_root
from brutex.dashboard.pull_progress import ProgressSnapshot, PullProgress
from brutex.dashboard.pull_stall import ALIVE_LOG_MIN_INTERVAL_S, PullWatchdog
from brutex.dashboard.summary_cache import CoverageCounters
from brutex.notifications.sweep_alerts import emit_pull_halt_alert
from brutex.observability.correlation import current_pull_id, new_pull_id, pull_id_scope
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

#: Job-kind labels for :meth:`BackgroundPullJob.start` (DEC-RATE-METER-LIFECYCLE-001).
#: The single-flight job runs BOTH vendor pulls AND the zero-vendor-call brute-force
#: sweep; the kind lets the ``/api/v1/pull/rate`` probe distinguish them so a 2-4-day
#: sweep reads an honest "idle" on the PULL RATE widget instead of a site-wide red
#: "stalled" (the #1593 C5 fix). Every pull-shaped ``start`` keeps the default
#: :data:`JOB_KIND_PULL`; only the sweep route passes :data:`JOB_KIND_SWEEP`.
JOB_KIND_PULL: Final[str] = "pull"
JOB_KIND_SWEEP: Final[str] = "sweep"

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

#: The injected ACCOUNT-keyed single-flight lock factory (DEC-ACCOUNT-KEY-SINGLEFLIGHT-001)
#: — given the Groww api_key, returns a context manager holding the account-wide lock
#: (``~/.brutex/locks/pull_account<pbkdf2[:16]>.flock``) for its lifetime, raising
#: :class:`~brutex.adapters.groww.pull_singleflight.PullAlreadyRunningError` on
#: ``__enter__`` when a peer process (possibly against a DIFFERENT lake root) already
#: holds it. Default the live
#: :func:`~brutex.adapters.groww.pull_singleflight.account_pull_singleflight_lock`;
#: tests inject a fake.
AccountSingleflightFactory = Callable[[str], AbstractContextManager[None]]

#: The injected cross-host pull-LEASE guard factory (DEC-ACCOUNT-LEASE-CROSSHOST-001)
#: — given the lake root, returns a context manager holding the advisory foreign-pull
#: lease (heartbeat file on the SHARED lake storage; best-effort DETECTION across
#: hosts, honestly NOT mutual exclusion) for its lifetime, raising
#: :class:`~brutex.adapters.groww.pull_singleflight.ForeignPullActiveError` (a
#: :class:`PullAlreadyRunningError`) on ``__enter__`` when a FRESH lease from a
#: DIFFERENT host is present. Entered strictly AFTER both flocks (the fixed order
#: that makes ignoring a same-host lease sound). Default the live
#: :func:`~brutex.adapters.groww.pull_singleflight.pull_lease_guard`; tests inject
#: a fake. Same ``Path -> CM`` shape as :data:`SingleflightFactory`.
LeaseFactory = Callable[[Path], AbstractContextManager[None]]

#: The injected account-key resolver — returns the Groww api_key the account lock keys
#: on, or ``None`` to SKIP the account lock gracefully (credentials unavailable — a
#: dry-run / credential-less run makes zero vendor calls, so there is no account rate to
#: protect). The default resolves via the AWS-only credential source
#: (DEC-AWS-ONLY-CREDS-001), mapping a ``CredentialsError`` to ``None`` with one loud
#: log line.
AccountKeyResolver = Callable[[], "str | None"]


def _default_account_key_resolver() -> str | None:
    """Resolve the account-lock key from the AWS-only credential source, or ``None``.

    DEC-ACCOUNT-KEY-SINGLEFLIGHT-001: delegates to
    :func:`brutex.config.credentials.resolve_groww_aws_credentials` (the SOLE credential
    source — the retired env-var path is NEVER read) and returns the ``api_key``. A
    :class:`~brutex.config.credentials_types.CredentialsError` (AWS unreachable / not
    configured) returns ``None`` — the account lock is then SKIPPED with one loud
    ``pull_job.account_lock_skipped`` log line rather than blocking a credential-less
    run behind a network dependency (the lake-root lock still guards same-lake peers).
    Lazy import keeps this module light at import time (mirrors the #889 pattern).
    """
    from brutex.config.credentials import resolve_groww_aws_credentials
    from brutex.config.credentials_types import CredentialsError

    try:
        return resolve_groww_aws_credentials().api_key
    except CredentialsError as exc:
        _LOG.warning(
            "pull_job.account_lock_skipped",
            reason="credentials unavailable — account-keyed single-flight not taken",
            error=str(exc),
        )
        return None


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
    the TERMINAL auth escalation the predicate also matches / the Greeks-enrichment
    pool-broken halt ``GreeksEnrichPoolBroken`` — a worker process died abruptly,
    DEC-GREEKS-PARALLEL-001 / the DHAN terminal pull-stop family — terminal auth,
    still-stale SSM token, inactive Data-API subscription — consulted via
    :func:`~brutex.adapters.dhan.errors.is_terminal_pull_stop`,
    DEC-DHAN-SPOT-FEED-001). The error classes are
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
    from brutex.adapters.dhan.errors import is_terminal_pull_stop as _dhan_terminal_pull_stop
    from brutex.adapters.empty_circuit_breaker import MassEmptySettleHalt
    from brutex.adapters.error_storm_breaker import VendorDownError
    from brutex.adapters.groww.errors import is_terminal_auth_failure
    from brutex.data.errors import GreeksEnrichPoolBroken, LakeWriteHalt, MarketHoursHalt
    from brutex.orchestrator.quota_tracker import DailyQuotaExceeded, QuotaInvariantError

    return (
        isinstance(
            exc,
            (
                LakeWriteHalt,
                # DEC-GROWW-MARKET-HOURS-GUARD-001: a pull crossed into the NSE
                # market-hours window and halted to free the shared Groww account for
                # TickVault's live feed — a pull-wide graceful stop, alerted like a
                # LakeWriteHalt if it escapes to the job layer.
                MarketHoursHalt,
                # The two quota classes are DORMANT under Groww (no daily quota, no
                # tracker constructed anywhere — DEC-QUOTA-VERDICT-MOOT-001) but
                # deliberately KEPT: they are the raise-path phone-alert net that
                # covers a future capped vendor's re-wired tracker with zero new code.
                DailyQuotaExceeded,
                QuotaInvariantError,
                MassEmptySettleHalt,
                VendorDownError,
                # DEC-GREEKS-PARALLEL-001: a Greeks pool worker died abruptly —
                # a HOST-resource condition (OOM SIGKILL / crash, honestly not
                # distinguishable), alerted like a LakeWriteHalt.
                GreeksEnrichPoolBroken,
            ),
        )
        or is_terminal_auth_failure(exc)
        # DEC-DHAN-SPOT-FEED-001 (design §3): the Dhan terminal pull-stop family —
        # terminal auth (re-read budget exhausted) / still-stale SSM token /
        # inactive Data-API subscription. The spine re-raises the FIRST occurrence
        # past its per-window quarantine; this arm makes the job layer alert it
        # CRITICAL (the same emit_pull_halt_alert path, zero further wiring).
        or _dhan_terminal_pull_stop(exc)
    )


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


@dataclass(frozen=True, slots=True)
class RetainedPullState:
    """The last PULL-kind run's snapshot + its run correlation id (immutable).

    DEC-OPERATOR-HEALTH-TRUTH-001 (hostile-review M2 fix): the shared job slot
    also runs the SWEEP, whose ``PullProgress.start`` RESETS the shared tracker
    — so the last pull's terminal state must be retained SEPARATELY or a halted
    pull's red outcome is wiped by the next sweep. ``pull_id`` is the run's
    correlation id (``""`` for a run-less deferral/refusal record), letting the
    outcome probe correlate the un-timestamped verdict holder with the EXACT
    run this snapshot describes — never a stale verdict over a different run.
    """

    snapshot: ProgressSnapshot
    pull_id: str


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
    account_singleflight_factory
        Optional factory building the ACCOUNT-keyed single-flight lock context manager
        from the resolved api_key (DEC-ACCOUNT-KEY-SINGLEFLIGHT-001; default the live
        :func:`~brutex.adapters.groww.pull_singleflight.account_pull_singleflight_lock`).
        Taken strictly BEFORE the lake-root lock — one fixed order everywhere, so two
        processes can never deadlock-invert across the pair. Injected for tests.
    account_key_resolver
        Optional resolver of the account-lock key (default
        :func:`_default_account_key_resolver` — the AWS-only credential source, mapping
        a :class:`~brutex.config.credentials_types.CredentialsError` to ``None``).
        ``None`` from the resolver SKIPS the account lock gracefully (logged) — a
        credential-less run makes no vendor calls, so there is no account rate to
        protect; the lake-root lock still guards same-lake peers.
    lease_factory
        Optional factory building the cross-host pull-LEASE guard from the resolved
        lake root (DEC-ACCOUNT-LEASE-CROSSHOST-001; default the live
        :func:`~brutex.adapters.groww.pull_singleflight.pull_lease_guard`). Entered
        strictly AFTER both flocks; a fresh FOREIGN-host lease on the shared lake
        storage refuses the pull. Advisory detection only — NOT cross-host mutual
        exclusion (the guard's documented honest envelope). Injected for tests.
    on_run_end
        Optional zero-arg completion hook (DEC-RATE-METER-LIFECYCLE-001) fired ONCE per
        run — EVERY kind: success, failure, dry-run, sweep — inside :meth:`_run`'s
        identity-gated ``finally``, strictly AFTER the per-start ``on_finally`` and
        strictly BEFORE ``_release_singleflight()``. It runs while ``self._lock`` is
        HELD (non-reentrant) — the hook must NEVER call back into this job. Best-effort
        (its own ``try/except`` — a raising hook never skips the single-flight release,
        and a raising ``on_finally`` never skips this hook). The production wiring is
        ``SessionRateController.clear_meter`` (a controller-lock LEAF; lock order
        job → controller, pinned) — the JOB-RUN arm of the meter-lifecycle law: for
        pulls run ON this job, run end clears the registered meter. HONEST BLAST
        RADIUS (DEC-RATE-METER-LIFECYCLE-001 round-4 leftover, Golden Rule #10): the
        wired clear is UNCONDITIONAL — this hook holds no handle to compare — so a
        job run of ANY kind (a sweep or a job dry-run included) finishing while a
        concurrent OFF-job direct ``POST /api/v1/universe/pull`` REAL pull is in
        flight EVICTS that pull's registered meter for the remainder of its run: the
        rate widget degrades to the honest ``meter_present=False`` admission fallback
        (never a fabricated rate) and the direct pull's own identity-gated clear
        later no-ops on the already-evicted handle — the job-end MIRROR of the
        dry-run-gate eviction disclosed at
        :func:`~brutex.dashboard.pull_controller.register_pull_meter`. The direct
        (off-job) ``POST /api/v1/universe/pull`` route never fires this hook and is
        covered by ``pull_candles``' OWN paired identity-gated clear (the spine
        compare-and-clears the exact handle it registered — rounds 2/4 — plus a
        no-holder dry-run arm), so
        only TOGETHER do the arms make "no in-process pull running ⇒ no meter
        registered" hold (DEC-RATE-METER-LIFECYCLE-001 rounds 2/4). ``None`` (the
        default) disables it.

    journal_heartbeat_interval_s
        Minimum seconds between two DURABLE pull-journal heartbeat records
        (DEC-PULL-TELEMETRY-TRIAD-001; default
        :data:`~brutex.dashboard.pull_stall.ALIVE_LOG_MIN_INTERVAL_S` — the same
        ≤ 18/hour bound the watchdog alive-beat log derives from the check cadence).
        Each throttled record appends via
        :func:`~brutex.observability.pull_journal.record_pull_heartbeat` under the
        resolved lake root, carrying the pull's bound ``pull_id`` so
        :func:`~brutex.observability.pull_journal.last_run` groups it with the run's
        ``started`` record. O(1) + best-effort — a write failure never affects the pull.
    journal_clock
        Monotonic source for the heartbeat throttle (default :func:`time.monotonic`);
        injected for deterministic tests.
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
        account_singleflight_factory: AccountSingleflightFactory = account_pull_singleflight_lock,
        account_key_resolver: AccountKeyResolver | None = None,
        lease_factory: LeaseFactory = pull_lease_guard,
        on_run_end: FinallyHook | None = None,
        journal_heartbeat_interval_s: float = ALIVE_LOG_MIN_INTERVAL_S,
        journal_clock: Callable[[], float] = time.monotonic,
        market_hours_guard: Callable[[], bool] = guard_active_default,
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
        #: Factory building the ACCOUNT-keyed single-flight lock context manager
        #: (DEC-ACCOUNT-KEY-SINGLEFLIGHT-001); injected for tests.
        self._account_singleflight_factory = account_singleflight_factory
        #: Factory building the cross-host pull-LEASE guard (advisory detection —
        #: DEC-ACCOUNT-LEASE-CROSSHOST-001); injected for tests.
        self._lease_factory = lease_factory
        #: Resolver of the account-lock key (``None`` result ⇒ graceful skip, logged).
        self._account_key_resolver: AccountKeyResolver = (
            _default_account_key_resolver if account_key_resolver is None else account_key_resolver
        )

        #: DURABLE watchdog-liveness heartbeat (DEC-PULL-TELEMETRY-TRIAD-001): each real
        #: progress beat that feeds the stall watchdog ALSO — throttled to at most once
        #: per this interval (default the SAME ≤ 18/hour bound the alive-beat log uses) —
        #: appends one :func:`~brutex.observability.pull_journal.record_pull_heartbeat`
        #: record, so a post-mortem can SEE the run was alive (and ``sleep_gaps`` gets
        #: narrower windows) instead of inferring it from a start-without-finish. O(1),
        #: best-effort: a write failure can NEVER affect the pull. The clock is injected
        #: for deterministic tests.
        self._journal_heartbeat_interval_s = journal_heartbeat_interval_s
        self._journal_clock = journal_clock
        #: Dedicated tiny lock for the heartbeat throttle stamp so concurrent worker
        #: beats never race the baseline; the journal WRITE happens OUTSIDE it (a slow
        #: disk must never serialise the progress hooks behind a held lock).
        self._journal_lock = threading.Lock()
        self._last_journal_heartbeat = float("-inf")
        #: Per-JOB (not per-start) completion hook (DEC-RATE-METER-LIFECYCLE-001): fired
        #: once per run in :meth:`_run`'s identity-gated ``finally``, after ``on_finally``
        #: and before the single-flight release, in its OWN try/except. Production wiring:
        #: ``SessionRateController.clear_meter`` (the C0 cross-pull meter-leak fix).
        self._on_run_end = on_run_end
        #: Market-hours pull guard (DEC-GROWW-MARKET-HOURS-GUARD-001): a zero-arg
        #: ``() -> bool`` predicate returning ``True`` when a vendor pull must be
        #: DEFERRED because the clock is inside the NSE market-hours window
        #: ``[08:45, 15:35)`` IST on a trading day AND the guard is enabled. The
        #: default resolves the live clock + env kill switch; tests inject a lambda.
        #: Consulted ONLY for :data:`JOB_KIND_PULL` starts (the Greeks-backfill /
        #: sweep kinds make no vendor calls and stay exempt).
        self._market_hours_guard = market_hours_guard
        self._lock = threading.Lock()
        self._running = False
        #: The CURRENT run's job kind (DEC-RATE-METER-LIFECYCLE-001) — :data:`JOB_KIND_PULL`
        #: (the default; every vendor pull) or :data:`JOB_KIND_SWEEP` (the sweep route).
        #: Initialised here so :attr:`running_job_kind` on a never-started job can never
        #: AttributeError; stamped in :meth:`start` under the claim lock, strictly AFTER
        #: the single-flight refusal (a refused start never overwrites the running kind).
        self._job_kind: str = JOB_KIND_PULL
        #: The last PULL-kind run's RETAINED terminal snapshot + run id
        #: (DEC-OPERATOR-HEALTH-TRUTH-001, hostile-review M2/m5): survives the
        #: shared tracker being reset by a later SWEEP start, so a halted
        #: pull's red outcome is never wiped into a masked green. Stamped in
        #: :meth:`_run`'s identity-gated ``finally`` (real runs) and via
        #: :meth:`_retain_pull_terminal` (the run-less deferral/refusal
        #: records); read by :meth:`last_pull_state`. ``None`` until the first
        #: PULL-kind start/deferral.
        self._last_pull_terminal: RetainedPullState | None = None
        #: The CURRENT run's account-wide single-flight lock context manager (entered in
        #: :meth:`start`, exited in :meth:`_run`'s ``finally``). ``None`` between runs and
        #: when the guard is disabled (no resolvable lake root). Held for the WHOLE pull so
        #: a peer process cannot start a second concurrent pull (Golden Rule #6).
        self._singleflight: AbstractContextManager[None] | None = None
        #: The CURRENT run's ACCOUNT-keyed single-flight lock (DEC-ACCOUNT-KEY-SINGLEFLIGHT-001;
        #: entered in :meth:`start` strictly BEFORE the lake-root lock, exited in
        #: :meth:`_run`'s ``finally`` strictly AFTER it — LIFO). ``None`` between runs,
        #: when the guard is disabled, and when the key resolver skipped (no credentials).
        self._account_singleflight: AbstractContextManager[None] | None = None
        #: The CURRENT run's cross-host pull-LEASE guard (DEC-ACCOUNT-LEASE-CROSSHOST-001;
        #: entered in :meth:`start` strictly AFTER both flocks, released in :meth:`_run`'s
        #: ``finally`` strictly BEFORE them — LIFO). ``None`` between runs and when the
        #: guard is disabled (no resolvable lake root).
        self._lease: AbstractContextManager[None] | None = None
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
        #: The CURRENT run's 12-hex ``pull_id`` (DEC-PULL-BEAT-PERRUN-001) — the key of
        #: the run's per-run progress-beat channel AND the correlation id the whole
        #: worker body runs under (``_run`` opens ``pull_id_scope`` on it). ``""``
        #: before the first :meth:`start`; published under :attr:`_lock` alongside the
        #: run-identity watchdog.
        self._pull_id: str = ""
        #: Once-per-run throttle for the DIRECT-hook foreign-beat drop WARN
        #: (``pull_job.foreign_beat_dropped`` — the :meth:`_beat` ambient run-id gate,
        #: DEC-PULL-BEAT-PERRUN-001 fix round). Reset on every :meth:`start`; guarded by
        #: :attr:`_lock`.
        self._foreign_beat_warned = False
        #: Count of EMPTY-ambient direct-hook beats ACCEPTED while the run is LIVE —
        #: the documented, reviewer-sanctioned legacy envelope of :meth:`_beat`
        #: (unscoped threads / tests / unrebound hooks). OBSERVABILITY ONLY (review
        #: fold MINOR — the acceptance behaviour itself is deliberately UNCHANGED):
        #: flushed to ONE debug ``pull_job.empty_ambient_beat_summary`` line when the
        #: run finishes (mirrors ``progress_beat.drop_summary``'s epoch-close flush;
        #: 0 = silent) so drift toward systematically-unattributed direct emitters
        #: stays VISIBLE. Reset on every :meth:`start`; guarded by :attr:`_lock`.
        self._empty_ambient_beats = 0

    def start(
        self,
        work: PullWork,
        *,
        total: int,
        detail: str = "",
        ledger_synced: bool = False,
        cross_process_lock: bool = True,
        on_success: SuccessHook | None = None,
        on_finally: FinallyHook | None = None,
        job_kind: str = JOB_KIND_PULL,
        pull_id: str | None = None,
        market_hours_exempt: bool = False,
    ) -> bool:
        """Launch *work* in the background over *total* units; return whether it started.

        *pull_id* (DEC-PULL-BEAT-PERRUN-001) is the run's correlation id — the key of
        the run's per-run progress-beat channel and the ``pull_id_scope`` the whole
        worker body runs under. ``None`` AND ``""`` both MINT a fresh 12-hex id (an
        empty id must never key the legacy back-compat channel); callers pass an
        explicit id only to correlate with an external job id / a deterministic test.

        Single-flight: returns ``True`` and starts the job when idle; returns
        ``False`` (a no-op) when a pull is already running. O(1).

        When *cross_process_lock* is ``False`` the cross-process guards (layer 2
        below) are SKIPPED for this run — the sanctioned uses are (a) a
        ZERO-WRITE run (the dashboard's dry-run Greeks-backfill scan,
        DEC-GREEKS-BULK-REENRICH-001 fix round 2): a read-only report has no
        writes to protect and no vendor calls to rate-guard, so it must never
        exclude a REAL pull on the same lake for its whole O(files) scan (the
        CLI ``--dry-run`` lock-free posture, kept behaviourally equal across the
        two surfaces); and (b) a run whose CALLEE owns its own cross-process
        lock discipline on the SAME flock file(s) — the crossverify route (the
        runner's ``default_seams`` takes the account→lake→lease trio itself,
        DEC-CROSSVERIFY-PRODUCER-001) and the SWEEP route (``cmd_sweep_all``
        takes the G10 lake read-guard ``LOCK_SH`` across the bar PRELOAD only +
        the G6 sweep single-flight — DEC-SWEEP-LOCK-PULL-CONCURRENCY-001): a
        job-level second acquire of the same file would SELF-CONFLICT (flock is
        per open file description), and a whole-run EX hold would lock the
        daily pull/crossverify out for the callee's wall time. The IN-process
        single-flight (layer 1) still applies — a second click on the same job
        is refused either way. Default ``True`` keeps every existing caller
        byte-identical.

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

        *job_kind* (DEC-RATE-METER-LIFECYCLE-001) labels WHAT runs on this shared
        single-flight job: :data:`JOB_KIND_PULL` (the default — every vendor pull,
        dry-run included) or :data:`JOB_KIND_SWEEP` (the zero-vendor-call brute-force
        sweep route). Read via :attr:`running_job_kind` by the ``/api/v1/pull/rate``
        probe so a 2-4-day sweep reads an honest "idle", never a site-wide red
        "stalled" (the #1593 C5 fix). It changes NOTHING else — ``running``, the
        progress feed and the single-flight are byte-identical for either kind.

        *market_hours_exempt* (DEC-GROWW-MARKET-HOURS-GUARD-001) exempts THIS start
        from the market-hours START gate below — set ``True`` by callers that make
        ZERO Groww API calls and are therefore safe to run inside the NSE trading
        window: the dashboard Greeks-backfill start (a stamp-driven re-enrich over
        ALREADY-STORED bars) and every ``--dry-run`` pull start (a synthetic adapter,
        no vendor call). Default ``False`` gates a REAL ``JOB_KIND_PULL`` vendor pull.
        Orthogonal to *job_kind*: a sweep is already exempt by kind; this flag covers
        the vendor-free operations that still run under ``JOB_KIND_PULL``.
        """
        # Market-hours START gate (DEC-GROWW-MARKET-HOURS-GUARD-001): refuse to BEGIN a
        # VENDOR pull inside the NSE market-hours window ``[08:45, 15:35)`` IST on a
        # trading day, so a manual/auto-resume click during the session never competes
        # with TickVault's live feed for the shared Groww account. THREE exemptions —
        # each makes ZERO Groww API calls, so each is safe in market hours:
        #   (a) the sweep route (``job_kind != JOB_KIND_PULL`` — a brute-force sweep
        #       reads the lake, never the vendor);
        #   (b) any caller that sets ``market_hours_exempt=True`` — the dashboard's
        #       Greeks-backfill start (a stamp-driven re-enrich over stored bars) AND
        #       every ``--dry-run`` pull start (a synthetic adapter, no vendor call).
        # Only a REAL (``JOB_KIND_PULL``, non-exempt) vendor pull is deferred here.
        # Returns ``False`` with a distinct "deferred" detail + log, exactly like a
        # refused single-flight (no watchdog built, no lock taken, no worker spawned →
        # a clean no-op). The pull resumes on the next out-of-hours run.
        if job_kind == JOB_KIND_PULL and not market_hours_exempt and self._market_hours_guard():
            self._progress.fail(
                "deferred — inside NSE market-hours window (08:45-15:35 IST); "
                "pulls run outside market hours only"
            )
            # Retain the deferral as the last PULL terminal (DEC-OPERATOR-HEALTH-TRUTH-001,
            # hostile-review m5): the gate fires BEFORE ``_job_kind`` is stamped, so
            # without this a deferral following a SWEEP run was invisible to the
            # outcome probe (the shared tracker's record was gated out as the
            # sweep's). Passed the ATTEMPTED kind explicitly; no run id exists.
            self._retain_pull_terminal(job_kind=job_kind)
            _LOG.warning(
                "pull.deferred.market_hours",
                job_kind=job_kind,
                detail=detail,
                reason="inside NSE market-hours window (08:45-15:35 IST)",
            )
            return False
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
        # Resolve the run's correlation id (DEC-PULL-BEAT-PERRUN-001): mint on None AND
        # on "" so a run can never accidentally key the LEGACY ("") beat channel.
        run_pull_id = pull_id or new_pull_id()
        with self._lock:
            if self._running:
                return False
            self._running = True
            # Capture the PRE-CLAIM identity state — the PRIOR finished run's RETAINED
            # token (``pull_id`` property contract) or the fresh-job defaults — so the
            # singleflight-REFUSED rollback below can RESTORE it exactly (review fold
            # NIT-2): the refused run never existed, so it must not wipe the retained
            # prior identity the between-runs gates (``_on_hung`` / ``_beat``) key on.
            prior_watchdog = self._watchdog
            prior_pull_id = self._pull_id
            prior_foreign_beat_warned = self._foreign_beat_warned
            prior_empty_ambient_beats = self._empty_ambient_beats
            # Publish the run-identity token ATOMICALLY with the single-flight flag, under
            # the SAME lock the gates read it under. Otherwise a stale worker A's finally
            # could acquire the lock in the gap between ``_running = True`` and a later
            # unlocked ``self._watchdog = watchdog``, still see ``self._watchdog is wd_A`` →
            # clear ``_running`` → admit a third pull concurrently (the residual race).
            self._watchdog = watchdog
            # Stamp THIS run's job kind under the SAME lock, strictly AFTER the
            # ``self._running`` refusal above — a refused start never overwrites the
            # running job's kind (DEC-RATE-METER-LIFECYCLE-001).
            self._job_kind = job_kind
            # Publish the run's pull_id under the SAME lock (the property reads it; the
            # stall-edge handlers stamp the race-free ``watchdog.run_id`` twin on their
            # events) + re-arm the once-per-run direct-hook foreign-beat drop WARN and
            # the per-run empty-ambient direct-beat tally (defensive: the run-end flush
            # normally already zeroed it).
            self._pull_id = run_pull_id
            self._foreign_beat_warned = False
            self._empty_ambient_beats = 0
        # Cross-process account-wide single-flight gate (DEC-ACCOUNT-PULL-SINGLEFLIGHT-001).
        # Taken AFTER the in-process flag (so a same-process double-click is filtered by the
        # cheap flag first) but BEFORE any work is spawned. On contention a peer process is
        # already pulling: roll back the in-process flag we just set, refuse this pull, and
        # report it — never spawn a second pull that would double the account request rate.
        # A resolver returning None DISABLES the guard (dry-run / test). Done outside
        # ``self._lock`` — it does (fast, non-blocking) filesystem I/O, and the flag is
        # already claimed so no other start can race in.
        if cross_process_lock:
            if not self._acquire_singleflight():
                with self._lock:
                    # Roll back the in-process flag we just claimed. No identity gate is needed
                    # here (unlike _run / _on_hung): _acquire_singleflight runs SYNCHRONOUSLY in
                    # this same start() call and never spawns / publishes a worker, so no other
                    # run can have superseded this one's watchdog between claiming the flag and
                    # here — this run unconditionally still owns it. ALSO RESTORE the pre-claim
                    # identity state captured above (review fold NIT-2, superseding the fix
                    # round's unconditional wipe to ``(None, "")``): the refused run never
                    # existed, so ``job.pull_id`` must not report a phantom id, a stale timer
                    # edge must never stamp one, AND a PRIOR finished run's deliberately
                    # RETAINED identity token (the ``pull_id`` property contract; the
                    # ``_on_hung`` / ``_beat`` between-runs gates key on it) must survive the
                    # refusal untouched — including its once-per-run WARN throttle and its
                    # empty-ambient tally.
                    self._running = False
                    self._watchdog = prior_watchdog
                    self._pull_id = prior_pull_id
                    self._foreign_beat_warned = prior_foreign_beat_warned
                    self._empty_ambient_beats = prior_empty_ambient_beats
                # Retain the refusal record as the last PULL terminal
                # (DEC-OPERATOR-HEALTH-TRUTH-001): the outcome probe then reads
                # the honest ``refused_busy`` even after a later sweep reuses
                # the shared tracker. ATTEMPTED kind — a refused sweep start
                # never masquerades as a pull outcome; no run id was minted.
                self._retain_pull_terminal(job_kind=job_kind)
                return False
        else:
            # ZERO-WRITE run (see the docstring): neither cross-process lock is
            # taken, so a concurrent REAL pull on the same lake is unaffected.
            # Both guard slots are pinned to None so _run's finally release is a
            # no-op for this run (they are already None between runs; this makes
            # the skip explicit rather than incidental).
            self._singleflight = None
            self._account_singleflight = None
        self._ledger_synced = ledger_synced
        self._pair_progress = False  # per-run reset; a phase opts in via set_phase
        self._progress.start(total=total, detail=detail)
        # Wire the HUNG→HEALTHY recovery edge so a heartbeat landing after a HUNG
        # declaration clears the stalled-suspect marker (DEC-PIPELINE-STALL-HEARTBEAT-001).
        # Set post-construction so the injected factory signature stays (on_hung) —
        # back-compat with every existing factory.
        watchdog.on_recovery = lambda event: self._on_recovered(event, watchdog)
        # Stamp the run id onto the watchdog (DEC-PULL-BEAT-PERRUN-001) — assigned
        # post-construction exactly like on_recovery/on_wedged so the injected
        # ``watchdog_factory`` signature stays ``(on_hung)``. The watchdog's
        # ``pull_stall.watchdog_alive`` event then carries the run correlation.
        watchdog.run_id = run_pull_id
        # Wire the wedged-worker escalation (DEC-PULL-WEDGE-ESCALATE-001, STAGE 1): the
        # terminal WEDGED verdict + ONE CRITICAL alert when the DUAL signal holds (zero
        # heartbeats for the wedge multiple AND no lake-revision movement since the HUNG
        # edge). Set post-construction like on_recovery (factory back-compat). A None
        # revision probe (no resolvable lake root) makes the escalation ABSTAIN — the
        # honest probe-less envelope, never a blind heartbeat-only WEDGED.
        watchdog.on_wedged = lambda event: self._on_wedged(event, watchdog)
        watchdog.revision_probe = self._build_revision_probe()
        watchdog.start()
        self._spawn(lambda: self._run(work, watchdog, run_pull_id, on_success, on_finally))
        return True

    @property
    def pull_id(self) -> str:
        """The LAST STARTED run's correlation id (``""`` before the first start) — O(1).

        NOT cleared when the run finishes: the id is deliberately RETAINED alongside
        :attr:`_watchdog` as the finished run's identity token (the ``_on_hung`` /
        superseded-run gates + :meth:`_beat`'s ambient gate key on it), so between runs
        this reports the finished run's id with :attr:`running` ``False`` — honest
        envelope (review fold, round 3), never "the current run" while none is live.
        A singleflight-REFUSED :meth:`start` RESTORES the retained token it briefly
        claimed over (review fold NIT-2), so this contract also holds across a refusal.
        """
        with self._lock:
            return self._pull_id

    def _acquire_singleflight(self) -> bool:
        """Acquire the cross-process pull guards (account flock → lake flock → lease).

        Resolves the lake root via :attr:`_lake_root_resolver`; a ``None`` root DISABLES
        ALL guards (a dry-run / test) and returns ``True`` (nothing to hold). Otherwise:

        1. **Account-keyed lock FIRST** (DEC-ACCOUNT-KEY-SINGLEFLIGHT-001): the key
           resolver supplies the api_key; a ``None`` key SKIPS this lock gracefully (the
           resolver already logged why — no credentials ⇒ no vendor calls ⇒ no account
           rate to protect). Contention (the SAME account mid-pull, possibly against a
           DIFFERENT lake root) refuses the pull.
        2. **Lake-root lock second** (:attr:`_singleflight_factory`). Contention rolls
           the account lock back (LIFO) and refuses the pull.
        3. **Cross-host lease LAST** (:attr:`_lease_factory`,
           DEC-ACCOUNT-LEASE-CROSSHOST-001): a fresh FOREIGN-host lease on the shared
           lake storage refuses the pull, rolling BOTH flocks back (LIFO). Taken after
           the flocks so ignoring a same-host lease is sound (advisory detection only
           — not cross-host mutual exclusion).

        Held guards are stashed in :attr:`_account_singleflight` / :attr:`_singleflight`
        / :attr:`_lease` so :meth:`_run` releases them in its ``finally``. On refusal the
        progress is failed with the actionable message + one warning log (Golden Rule #6
        — never double the account request rate).
        """
        lake_root = self._lake_root_resolver()
        if lake_root is None:
            self._singleflight = None
            self._account_singleflight = None
            self._lease = None
            return True
        account_key = self._account_key_resolver()
        if account_key is None:
            # Graceful skip (already logged by the resolver): the lake-root lock below
            # still guards same-lake peers.
            self._account_singleflight = None
        else:
            account_guard = self._account_singleflight_factory(account_key)
            try:
                account_guard.__enter__()
            except PullAlreadyRunningError as exc:
                self._account_singleflight = None
                self._singleflight = None
                self._progress.fail(f"{type(exc).__name__}: {exc}")
                _LOG.warning(
                    "pull_job.account_singleflight_refused",
                    reason="a pull is already running for this Groww account",
                )
                return False
            self._account_singleflight = account_guard
        guard = self._singleflight_factory(lake_root)
        try:
            guard.__enter__()
        except PullAlreadyRunningError as exc:
            self._singleflight = None
            self._lease = None
            # Roll back the account lock we just took (LIFO) — refusing the pull must
            # not leave the account gate held with no run owning it.
            self._release_account_singleflight()
            self._progress.fail(f"{type(exc).__name__}: {exc}")
            _LOG.warning(
                "pull_job.singleflight_refused",
                lake_root=str(lake_root),
                reason="a pull is already running for this lake",
            )
            return False
        self._singleflight = guard
        lease = self._lease_factory(lake_root)
        try:
            lease.__enter__()
        except PullAlreadyRunningError as exc:
            self._lease = None
            # Roll back BOTH flocks (LIFO) — refusing the pull must not leave any
            # guard held with no run owning it.
            self._release_singleflight()
            self._progress.fail(f"{type(exc).__name__}: {exc}")
            _LOG.warning(
                "pull_job.pull_lease_refused",
                lake_root=str(lake_root),
                reason="a pull appears to be running on another host for this lake",
            )
            return False
        self._lease = lease
        return True

    def _release_singleflight(self) -> None:
        """Release the cross-process pull guards held by the current run, if any (LIFO).

        Called from :meth:`_run`'s ``finally`` (and from the lease-refused rollback in
        :meth:`_acquire_singleflight`). ``__exit__``-s the held cross-host LEASE first,
        then the lake-root guard, then the account-keyed guard — the reverse of the
        acquire order — so a peer process can start the next pull. ``None`` guards
        (disabled / never-taken / skipped) are no-ops. Best-effort: any error closing a
        guard is logged + swallowed so it can never crash the worker thread or leave
        the in-process flag stuck.
        """
        self._release_lease()
        guard = self._singleflight
        self._singleflight = None
        if guard is not None:
            try:
                guard.__exit__(None, None, None)
            except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the worker
                _LOG.exception("pull_job.singleflight_release failed; the pull is unaffected")
        self._release_account_singleflight()

    def _release_lease(self) -> None:
        """Release the held cross-host lease guard, if any — best-effort, never raises."""
        guard = self._lease
        self._lease = None
        if guard is not None:
            try:
                guard.__exit__(None, None, None)
            except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the worker
                _LOG.exception("pull_job.lease_release failed; the pull is unaffected")

    def _release_account_singleflight(self) -> None:
        """Release the held account-keyed lock, if any — best-effort, never raises."""
        guard = self._account_singleflight
        self._account_singleflight = None
        if guard is None:
            return
        try:
            guard.__exit__(None, None, None)
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the worker
            _LOG.exception("pull_job.account_singleflight_release failed; the pull is unaffected")

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
        silent). The timer keeps polling so a later heartbeat fires the recovery edge and
        :meth:`_on_recovered` clears the marker. HONESTY RECONCILE
        (DEC-HEAL-SCAN-LIVENESS-BEATS-001): the earlier "the greeks/prebuild/movers seams
        now beat — the false-positive class is closed" claim here was FALSIFIED by
        production forensics — the L2 post-pull HEAL SCAN (O(files), 36 min over 101,682
        files) and the synchronous drain-time window-ledger FULL SAVE (a 12.96 s
        ``save_slow`` over 68,234 rows) emitted ZERO beats, so this edge falsely fired
        ``stalled_suspect`` for ~27 min on a provably-healthy run. Those two surfaces now
        beat too (per-target + per-file inside ``lake_heal.heal_after_pull``'s scan +
        per healed finding; entry/completion beats around
        ``window_ledger_store.save_window_ledger``). The class is NARROWED, not closed:
        any FUTURE >timeout silent stage on the worker re-opens it — the safe failure
        direction stays a loud false SUSPECT, never a masked true stall.
        Identity-gated: a late edge for a superseded run never marks a newer run.

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
                # The race-free per-run id (``watchdog.run_id``, stamped once in start()):
                # ``self._pull_id`` is read here OUTSIDE the identity-gated lock, so a
                # stale edge could stamp a NEWER run's id in the gap (fix round MINOR).
                pull_id=watchdog.run_id,
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
                # Race-free per-run id (see _on_hung) — never a newer run's ``_pull_id``.
                pull_id=watchdog.run_id,
            )
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the timer
            _LOG.exception("pull_stall.on_recovered failed; the pull / timer is unaffected")

    def _build_revision_probe(self) -> Callable[[], str | None] | None:
        """Build the wedge-escalation lake-revision probe, or ``None`` when no lake root.

        DEC-PULL-WEDGE-ESCALATE-001: the DUAL wedge signal's second half — an O(1)
        content token off the durable window-ledger sidecar
        (:func:`brutex.dashboard.lake_revision.lake_revision`, DEC-HARDENING-LEFTOVERS-001)
        that MOVES whenever a production pull checkpoints the ledger. A worker that is
        silent on heartbeats but still checkpointing is NOT wedged. ``None`` (no
        resolvable lake root — a dry-run / test) means the escalation abstains entirely
        (documented honest envelope). Lazy import keeps this module wheel-free at import.
        """
        lake_root = self._lake_root_resolver()
        if lake_root is None:
            return None

        def _probe() -> str | None:
            from brutex.dashboard.lake_revision import lake_revision

            return lake_revision(lake_root)

        return _probe

    def _on_wedged(self, event: WorkerEvent, watchdog: PullWatchdog) -> None:
        """Wedge escalation (DEC-PULL-WEDGE-ESCALATE-001, STAGE 1): terminal mark + ONE alert.

        The watchdog confirmed the DUAL signal — zero heartbeats for the wedge multiple
        of the stall timeout AND no lake-revision movement since the HUNG edge — so this
        run is judged WEDGED: (1) mark the progress WEDGED + ``restart_required`` (an
        honest TERMINAL verdict for the run; the state stays ``running`` — the thread is
        not killed), (2) fire ONE CRITICAL pull-halt alert naming the restart requirement.
        Deliberately does NOT clear ``_running`` / release the single-flight — the zombie
        worker still owns the lake (the DEC-PIPELINE-STALL-HEARTBEAT-001 double-run-race
        fix is preserved); a PROCESS RESTART releases the ``flock`` on fd close.
        AUTO-restart (supervisor kill + respawn) is a NAMED follow-up, NOT built here.
        Identity-gated + best-effort, mirroring :meth:`_on_hung`.
        """
        try:
            phase = self._progress.snapshot().phase or "unknown"
            with self._lock:
                if self._watchdog is not watchdog:
                    return
                self._progress.mark_wedged(
                    f"WEDGED — no heartbeat for {event.last_beat_seconds_ago:.0f}s "
                    f"(>= {watchdog.wedge_multiplier:g}x the {event.timeout_seconds:.0f}s "
                    f"stall timeout) AND no lake-revision movement during phase '{phase}'; "
                    "restart required (worker NOT killed; single-flight held)"
                )
            emit_pull_halt_alert(
                TimeoutError(
                    f"dashboard pull WEDGED: no heartbeat for "
                    f"{event.last_beat_seconds_ago:.0f}s AND no lake-revision movement "
                    f"during phase '{phase}' — restart required; worker NOT killed "
                    "(auto-restart is a named follow-up)"
                )
            )
            _LOG.error(
                "pull_stall.wedged",
                phase=phase,
                last_beat_seconds_ago=round(event.last_beat_seconds_ago, 1),
                timeout_seconds=event.timeout_seconds,
                restart_required=True,
                # Race-free per-run id (see _on_hung) — never a newer run's ``_pull_id``.
                pull_id=watchdog.run_id,
            )
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the timer
            _LOG.exception("pull_stall.on_wedged failed; the pull / timer is unaffected")

    def _beat_for(self, watchdog: PullWatchdog) -> Callable[[], None]:
        """The per-run seam beat callback, bound to THIS run's *watchdog* — O(1).

        DEC-PULL-BEAT-PERRUN-001: the closure registered on the run's keyed
        progress-beat channel. Identity-gated (mirrors the ``_on_hung`` /
        ``_on_recovered`` / ``_on_wedged`` / ``finally`` gates): it beats ONLY while
        *watchdog* is still the CURRENT run's — so even a somehow-leaked channel can
        only ever beat its OWN, already-stopped watchdog, never a newer run's. The
        journal throttle stays COMMON via :meth:`_maybe_journal_heartbeat`, and the
        journal append carries the SAME live-run guard :meth:`_beat` received in
        round 3 (review fold NIT-1): ``(watchdog, running)`` are read atomically
        under :attr:`_lock` and the beat lands OUTSIDE it (the same job-lock →
        watchdog-lock ordering discipline as :meth:`_beat` — the timer thread holds
        the watchdog lock while calling the edge handlers, which take the job lock).
        """

        def _run_beat() -> None:
            with self._lock:
                if self._watchdog is not watchdog:
                    return
                running = self._running
            watchdog.beat()
            if not running:
                # Between runs (the finished run's identity token is RETAINED — see
                # :attr:`pull_id`): a LAST in-flight keyed beat delivered after the
                # run's ``finally`` hit the already-STOPPED watchdog above (inert),
                # but a durable journal heartbeat would land AFTER the run's finish
                # record — skip it, exactly like :meth:`_beat`'s round-3 guard
                # (review fold NIT-1).
                return
            # The routed channel key IS the run id — pass it so the journal never
            # pays a second ambient contextvars scan per delivered beat (fix round
            # NIT) and is guaranteed identical to the channel key.
            self._maybe_journal_heartbeat(pull_id=watchdog.run_id)

        return _run_beat

    def _run(
        self,
        work: PullWork,
        watchdog: PullWatchdog,
        run_pull_id: str,
        on_success: SuccessHook | None = None,
        on_finally: FinallyHook | None = None,
    ) -> None:
        """Worker body: run *work*, then mark the tracker done / errored.

        *watchdog* is THIS run's stall watchdog (passed in by :meth:`start`, so it is never
        ``None`` here), stopped in the ``finally`` once the worker has returned / raised.

        *run_pull_id* is THIS run's correlation id (DEC-PULL-BEAT-PERRUN-001): the whole
        body runs inside ``pull_id_scope(run_pull_id)`` — so every pipeline phase
        (metadata / F&O / prebuild / movers / greeks) shares ONE run id on the worker
        thread, the entrypoints' inner :func:`inherited_pull_id_scope` inherits it, the
        spine fan-out workers' capture-then-rebind carries it, and the durable journal
        heartbeat records it (the "no bound pull_id" skip no longer fires here) — and
        the run's progress-beat channel is registered/unregistered KEYED on it.

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
        # The run's correlation scope is the OUTERMOST context of the worker body
        # (DEC-PULL-BEAT-PERRUN-001): every phase the work drives (metadata / F&O /
        # prebuild / movers / greeks) runs under ONE run id on this thread, closing the
        # documented pipeline scope gap — and the seam beats fired from those phases
        # route to THIS run's keyed channel below.
        with pull_id_scope(run_pull_id):
            # Install this run's KEYED liveness beat (DEC-PULL-CONTENTION-ANTISTALL-001
            # GAP #15 + DEC-PULL-BEAT-PERRUN-001): the bounded partition lock-wait + the
            # rate pacer fire ``emit_progress_beat`` on the FAN-OUT worker threads, whose
            # capture-then-rebind carries this run's id — so the beat routes to THIS
            # run's channel and keeps the watchdog alive while a pull is
            # slow-but-progressing under lock contention. A FOREIGN thread's beat (no /
            # another run's id) can never reach it — a real stall is never masked.
            # Removed in the ``finally`` (own channel only) so a finished run leaves no
            # stale beat.
            run_beat = self._beat_for(watchdog)
            register_progress_beat(run_beat, pull_id=run_pull_id)
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
                        _LOG.exception(
                            "pull_job.on_success failed; the completed pull is unaffected"
                        )
            finally:
                # Remove this run's liveness beat — its OWN keyed channel ONLY, and only
                # if the channel still holds THIS run's closure (the identity guard), so
                # a stale worker's unregister can never starve a newer run's beat — even
                # a duplicate-id survivor's — and a later poll can never fire into a dead
                # watchdog (idempotent — DEC-PULL-CONTENTION-ANTISTALL-001 GAP #15 /
                # DEC-PULL-BEAT-PERRUN-001).
                unregister_progress_beat(pull_id=run_pull_id, beat=run_beat)
                # Stop this run's stall timer first (so it can never fire after the worker has
                # already returned / raised). Outside the lock: stop() only sets an Event,
                # never blocks. Idempotent with _on_hung's stop() (both target the SAME watchdog).
                watchdog.stop()
                empty_ambient_beats = 0
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
                        # Retain THIS run's terminal snapshot when it was a PULL
                        # (DEC-OPERATOR-HEALTH-TRUTH-001, hostile-review M2): the
                        # shared tracker is reset by the NEXT start (a sweep
                        # included), so the honest last-pull outcome — a halted
                        # pull's red included — must survive here, keyed with the
                        # run id so the outcome probe can correlate the verdict
                        # holder with EXACTLY this run. Inline (self._lock is held
                        # and non-reentrant — _retain_pull_terminal would deadlock);
                        # lock order job → progress, a pinned-safe leaf read.
                        if self._job_kind == JOB_KIND_PULL:
                            self._last_pull_terminal = RetainedPullState(
                                self._progress.snapshot(), run_pull_id
                            )
                        # Collect the run's ACCEPTED empty-ambient direct-beat tally for
                        # the observability-only debug flush below (review fold MINOR —
                        # logged OUTSIDE the lock, mirroring
                        # ``progress_beat.drop_summary``'s epoch-close flush; the
                        # identity gate keeps a superseded stale worker from flushing a
                        # NEWER run's tally under this run's id).
                        empty_ambient_beats = self._empty_ambient_beats
                        self._empty_ambient_beats = 0
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
                        # Fire the per-JOB run-end hook (DEC-RATE-METER-LIFECYCLE-001 — the
                        # production wiring clears the session's registered CallMeter, the C0
                        # cross-pull leak fix) INSIDE the identity gate — a superseded stale
                        # worker never clears a NEWER run's meter (the scoped-token property
                        # for free) — strictly AFTER on_finally and BEFORE the single-flight
                        # release, in its OWN try/except so a raising on_finally never skips
                        # this clear and a raising clear never skips the release. Lock-order
                        # note: self._lock is HELD here; the hook must be a LEAF (the wired
                        # clear_meter takes only the controller lock — job → controller,
                        # never the reverse) and must never call back into this job.
                        if self._on_run_end is not None:
                            try:
                                self._on_run_end()
                            except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crash the worker
                                _LOG.exception(
                                    "pull_job.on_run_end failed; the single-flight release continues"
                                )
                        # Release the account-wide cross-process single-flight lock under the
                        # SAME identity gate (DEC-ACCOUNT-PULL-SINGLEFLIGHT-001), so a peer
                        # process can start the next pull — released HERE and only here, when
                        # the worker thread actually exits (never by the watchdog while the
                        # thread may still be writing — the double-run race).
                        self._release_singleflight()
                if empty_ambient_beats:
                    # ONE debug summary per run (0 = silent) — the empty-ambient
                    # direct-hook magnitude signal (review fold MINOR: observability
                    # only; every counted beat WAS accepted — the sanctioned envelope).
                    _LOG.debug(
                        "pull_job.empty_ambient_beat_summary",
                        empty_ambient_beats=empty_ambient_beats,
                        pull_id=run_pull_id,
                        reason=(
                            "direct-hook heartbeats from threads with NO bound pull_id "
                            "were ACCEPTED during this run (the documented legacy "
                            "envelope — unscoped callers / unrebound hooks); flushed "
                            "once per run so drift toward systematically-unattributed "
                            "direct emitters stays visible, never silent"
                        ),
                    )

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
        """Beat the CURRENT run's stall watchdog iff the emitting thread belongs to it.

        DEC-PULL-BEAT-PERRUN-001 fix round (the direct-hook anti-masking gate): the
        direct heartbeats (:meth:`_on_progress` / :meth:`sync_from_ledger` /
        :meth:`add_bars`) funnel here from LONG-LIVED job methods that a STALE run's
        abandoned thread can still invoke — e.g. a drain-abandoned fetch-pipeline
        consumer un-wedging after run A ended and folding its backlog through the
        ``on_bars`` → :meth:`add_bars` hook while run B is live. The keyed seam channels
        never see those calls, so this gate mirrors them at the direct path: the
        emitting thread's ambient ``pull_id`` (the consumers/fan-out workers carry their
        OWN run's re-bound id) must be EMPTY (legacy callers / tests / unrebound hooks —
        pre-gate behaviour preserved; while the run is LIVE each such ACCEPTED
        empty-ambient beat is additionally COUNTED and debug-flushed once at run end as
        ``pull_job.empty_ambient_beat_summary`` — observability only, review fold MINOR)
        or match the CURRENT run's id; a non-empty
        mismatch is DROPPED + WARNed once per run (``pull_job.foreign_beat_dropped``) —
        a stale run's thread can never refresh a newer run's heartbeat and mask a real
        stall. The gate reads ``(watchdog, pull_id, running)`` atomically under
        :attr:`_lock` and beats OUTSIDE it (the job-lock → watchdog-lock order would
        otherwise invert the timer thread's watchdog-lock → job-lock edge). Also appends
        the THROTTLED durable pull-journal heartbeat (DEC-PULL-TELEMETRY-TRIAD-001) —
        only while the run is LIVE (:attr:`_running`): the finished run's identity
        token (:attr:`_watchdog` + :attr:`_pull_id`) is deliberately RETAINED after
        the ``finally`` (the ``_on_hung`` / superseded-run identity gates need it), so
        a stale thread still bound to the FINISHED run's id passes the ambient gate
        between runs and inertly beats the already-STOPPED watchdog — but journals
        NOTHING, so a durable heartbeat record can never land AFTER the run's finish
        record (review fold, round 3). Cost honesty (Golden Rule #10): the ambient
        read is one contextvars-context copy + scan
        (:func:`~brutex.observability.correlation.current_pull_id` — O(bound
        contextvars), ≤ ~3 keys), skipped entirely by the UNLOCKED
        no-watchdog fast exit below (before the FIRST ``start()`` a beat costs one
        attribute read — the pre-gate short-circuit, restored).
        """
        # Unlocked fast exit: before the FIRST start() there is no watchdog — skip the
        # contextvars scan + the job-lock acquire entirely. The unlocked read is the
        # same benign race the pre-gate code carried: a beat straddling start() is
        # already tolerated (register()/start() seed the watchdog's heartbeat).
        watchdog = self._watchdog
        if watchdog is None:
            return
        ambient = current_pull_id()
        with self._lock:
            # Re-read under the lock: the unlocked read above can lose the race with
            # a refused start()'s identity rollback (the singleflight-refused branch
            # RESTORES the pre-claim token — ``None`` on a fresh job, review fold
            # NIT-2) — nothing to beat then.
            watchdog = self._watchdog
            if watchdog is None:
                return
            run_pull_id = self._pull_id
            running = self._running
            foreign = bool(ambient) and ambient != run_pull_id
            if running and not ambient:
                # Observability-ONLY tally (review fold MINOR): an empty-ambient
                # direct beat during a LIVE run rides the documented legacy-acceptance
                # envelope below — count it (debug-flushed once at run end by
                # :meth:`_run`'s ``finally``); the acceptance itself is deliberately
                # UNCHANGED (reviewer-sanctioned back-compat, never a drop).
                self._empty_ambient_beats += 1
            warn = foreign and not self._foreign_beat_warned
            if warn:
                self._foreign_beat_warned = True
        if foreign:
            if warn:
                _LOG.warning(
                    "pull_job.foreign_beat_dropped",
                    ambient_pull_id=ambient,
                    run_pull_id=run_pull_id,
                    reason=(
                        "a direct-hook heartbeat arrived from a thread bound to a "
                        "DIFFERENT run's pull_id (a stale/abandoned worker) — dropped, "
                        "never routed into the current run (anti-masking); worst case "
                        "is a loud false stalled-SUSPECT, never a masked true stall"
                    ),
                )
            return
        watchdog.beat()
        if not running:
            # Between runs (finished-run identity token retained — see the docstring):
            # the beat above hit an already-STOPPED watchdog (inert), but a durable
            # journal heartbeat would land AFTER the run's finish record — skip it
            # (review fold, round 3: the between-runs forensic-journal noise).
            return
        # ``ambient`` is either the run's own id or "" (the legacy skip) — pass it so
        # the journal never pays a second contextvars scan (fix round NIT).
        self._maybe_journal_heartbeat(pull_id=ambient)

    def _maybe_journal_heartbeat(self, pull_id: str | None = None) -> None:
        """Durably journal ONE throttled watchdog-liveness heartbeat — O(1), best-effort.

        DEC-PULL-TELEMETRY-TRIAD-001 (wiring the existing, previously call-site-less
        :func:`~brutex.observability.pull_journal.record_pull_heartbeat`). Every real
        progress beat funnels here; at most one journal append lands per
        :attr:`_journal_heartbeat_interval_s`. The record carries the pull's run id —
        the explicit *pull_id* when the caller already resolved it (the per-run beat
        closure passes ``watchdog.run_id``; :meth:`_beat` passes its already-read
        ambient id — one contextvars scan per beat, never two: fix round NIT), else the
        BOUND ambient :func:`~brutex.observability.correlation.current_pull_id` (the
        progress hooks fire inside the pull's ``pull_id_scope``, re-bound per worker
        thread by the ingest spine) — so
        :func:`~brutex.observability.pull_journal.last_run` groups it with the run's
        ``started`` record and ``sleep_gaps`` gets narrower suspend windows. Honest
        envelope (Golden Rule #10): a beat with NO run id (it would never group) and a
        ``None`` lake root each skip the write — a missing heartbeat is a weaker
        forensic signal, never a wrong one; the write itself is the journal's
        never-raise append, and this whole path is wrapped so ANY failure is logged +
        swallowed — the pull is NEVER affected. The lazy journal import mirrors
        :func:`is_pull_halt`'s import-hygiene discipline.
        """
        if pull_id is None:
            pull_id = current_pull_id()
        if not pull_id:
            return
        with self._journal_lock:
            now = self._journal_clock()
            if now - self._last_journal_heartbeat < self._journal_heartbeat_interval_s:
                return
            self._last_journal_heartbeat = now
        try:
            lake_root = self._lake_root_resolver()
            if lake_root is None:
                return
            from brutex.observability.pull_journal import record_pull_heartbeat

            record_pull_heartbeat(lake_root, pull_id=pull_id)
        except Exception:  # pylint: disable=broad-exception-caught — best-effort, never affect the pull
            _LOG.exception("pull_job.journal_heartbeat failed; the pull is unaffected")

    @property
    def running(self) -> bool:
        """``True`` while a pull is in flight — O(1)."""
        with self._lock:
            return self._running

    @property
    def running_job_kind(self) -> str | None:
        """The RUNNING job's kind, else ``None`` — one lock, atomic, O(1).

        DEC-RATE-METER-LIFECYCLE-001. Returns :data:`JOB_KIND_PULL` /
        :data:`JOB_KIND_SWEEP` while the matching :meth:`start` is in flight and
        ``None`` when idle. Reads ``_running`` AND ``_job_kind`` under the ONE
        ``self._lock`` acquisition so the pair can never tear (a probe can never see
        a stale kind against a fresh running flag). The ``/api/v1/pull/rate``
        production probe compares this against :data:`JOB_KIND_PULL`, so a running
        sweep honestly reads "no PULL active" while :attr:`running` (which every
        duty-degrade consumer keeps using) stays sweep-INCLUSIVE.
        """
        with self._lock:
            return self._job_kind if self._running else None

    def last_pull_state(self) -> RetainedPullState | None:
        """The last PULL-kind run's snapshot + run id — O(1), sweep-proof.

        DEC-OPERATOR-HEALTH-TRUTH-001 (hostile-review M2/m5 fix): the honest
        last-pull-outcome projection must survive the shared job slot being
        REUSED by the zero-vendor-call SWEEP — ``PullProgress.start`` resets the
        shared tracker, so a halted pull's ``error`` snapshot would otherwise be
        WIPED by the next sweep and the classifier could fall back to a
        possibly-stale verdict (the masked-green class). This returns:

        * the LIVE snapshot + run id while a PULL-kind run is in flight;
        * otherwise the RETAINED terminal snapshot of the last PULL-kind run
          (stamped in :meth:`_run`'s identity-gated ``finally``), which also
          covers the market-hours START deferral and the single-flight
          refusals (each stamps via :meth:`_retain_pull_terminal` with an
          EMPTY run id — no run ever existed);
        * ``None`` when no pull has ever run/deferred on this slot.

        The carried ``pull_id`` lets the outcome probe consult the
        (un-timestamped) last-pull-verdict holder ONLY when the verdict was
        stamped by the SAME run the snapshot describes — a stale verdict can
        never colour a different run's outcome.
        """
        with self._lock:
            if self._running and self._job_kind == JOB_KIND_PULL:
                return RetainedPullState(self._progress.snapshot(), self._pull_id)
            return self._last_pull_terminal

    def _retain_pull_terminal(self, *, job_kind: str, pull_id: str = "") -> None:
        """Retain the just-recorded snapshot as the last PULL terminal (O(1)).

        DEC-OPERATOR-HEALTH-TRUTH-001: called at the run-less refusal sites —
        the market-hours START deferral and the cross-process single-flight
        refusal — with the ATTEMPTED *job_kind* (a refused SWEEP start must
        never masquerade as a pull outcome). *pull_id* stays ``""`` there: no
        run was minted, so no verdict can ever correlate with it.
        """
        if job_kind != JOB_KIND_PULL:
            return
        with self._lock:
            self._last_pull_terminal = RetainedPullState(self._progress.snapshot(), pull_id)

    @property
    def counters(self) -> CoverageCounters:
        """The incremental coverage counters fed by the write funnel — O(1)."""
        return self._counters

    def set_phase(
        self, phase: str, *, pair_progress: bool = False, open_ended: bool = False
    ) -> None:
        """Set the COARSE pipeline phase currently in flight — O(1) passthrough.

        DEC-UNIVERSE-PER-PHASE-PROGRESS-001. The one-click pipeline's ``_work`` calls
        this at each phase transition so the live Universe per-phase view highlights the
        running phase; it delegates straight to
        :meth:`~brutex.dashboard.pull_progress.PullProgress.set_phase` (a label only, no
        counter is keyed by it).

        *pair_progress* (DEC-PIPELINE-STALL-HEARTBEAT-001, the frozen-counter fix):
        ``True`` declares the ENTERING phase has NO ledger-sync driver, so the per-pair
        hook advances the counters open-endedly even on a ``ledger_synced`` run. Default
        ``False`` keeps every other phase / caller byte-identical (the ledger sync stays
        authoritative). The pipeline's F&O historical phase NO LONGER uses it
        (DEC-PULL-PROGRESS-HONESTY-001 — it now drives absolute market-filtered ledger
        counts through :meth:`sync_from_ledger` and declares itself *open_ended*
        instead of pinning percent at 100); the mechanism stays for any phase that
        genuinely has no ledger at all.

        *open_ended* (DEC-PULL-PROGRESS-HONESTY-001): forwarded to
        :meth:`~brutex.dashboard.pull_progress.PullProgress.set_phase` — ``True``
        declares the entering phase's unit total NON-FINAL (the F&O historical
        phase's ledger denominator grows as contracts are planned mid-phase), so the
        frontend shows "N units · total unknown" and ETA is suppressed. STICKY:
        the default ``False`` never clears a marker an earlier phase set.
        """
        self._pair_progress = pair_progress
        self._progress.set_phase(phase, open_ended=open_ended)

    def add_bars(self, delta: int) -> None:
        """Fold a live bars DELTA into the progress tally — O(1), thread-safe.

        DEC-PULL-PROGRESS-HONESTY-001 (the frozen-bars fix): the async F&O fetch
        pipeline invokes this (via its ``on_bars`` seam) once per background-folded
        window with the window's REAL appended-bar count — the producer-side per-pair
        hook honestly reports 0 bars in async mode, so without this the headline bars
        counter froze at the spot phase's count while millions of option bars
        streamed in. Delegates to the O(1)
        :meth:`~brutex.dashboard.pull_progress.PullProgress.add_bars` (each folded
        window contributes its delta exactly once — the no-double-count invariant),
        marks any wired summary cache dirty (a durable window landed — the same
        funnel contract as :meth:`_on_progress`), and beats the stall watchdog
        (bars landing IS real progress, so a long single-pair stream never
        false-trips the hung detector).
        """
        self._progress.add_bars(delta)
        if self._summary_cache is not None:
            self._summary_cache.mark_dirty()
        self._beat()

    def snapshot(self) -> ProgressSnapshot:
        """Return the current O(1) progress snapshot for the polling route."""
        return self._progress.snapshot()


__all__ = [
    "JOB_KIND_PULL",
    "JOB_KIND_SWEEP",
    "BackgroundPullJob",
    "FinallyHook",
    "LakeRootResolver",
    "ProgressHook",
    "PullWork",
    "RetainedPullState",
    "SingleflightFactory",
    "Spawner",
    "SuccessHook",
    "WatchdogFactory",
    "is_pull_halt",
]
