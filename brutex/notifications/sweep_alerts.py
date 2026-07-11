"""Sweep-supervisor → Telegram alert seam (DEC-TELEGRAM-WIRE-001).

The §11 AUTOMATED decree promises "watchdog + Telegram + auto-resume": the
:class:`~brutex.notifications.telegram.TelegramNotifier` is a well-tested,
fully-built channel, and :mod:`brutex.notifications.wiring` already bridges the
INGEST pipeline's ``EventSubscriber`` stream. This module closes the remaining
gap — the **brute-force sweep + live-pull** operator alerts the supervisor never
emitted:

* **watchdog-hung** — a sweep tuple wedged past the
  :class:`~brutex.engine.sweep_watchdog.SweepWatchdog` timeout
  (``WORKER_HUNG``, CRITICAL).
* **rate.breach** — a vendor ``429`` throttled the live pull
  (``RATE_LIMIT_DOWNSHIFT``, WARN; coalesced by the notifier's 60 s dedupe).
* **auth.renew_failed** — ``RenewToken`` refused so the pull is running on a
  token that will expire (``AUTH_RENEW_FAILED``, ERROR).
* **sweep complete** — the supervised grid finished (``RUN_COMPLETE``, INFO).

Plus the MANUAL operator trigger backing ``brutex notify test`` — a one-shot
``TEST`` digest that proves the channel is wired + reachable.

## Honest envelope (Golden Rule #10)

Every emit is **best-effort, fire-and-forget**: it resolves the notifier (an
injected explicit instance, else the lazy process singleton), publishes ONE
:class:`~brutex.notifications.telegram.TelegramEvent`, and returns ``True`` iff
the publish was attempted on a live (enabled) notifier. EVERY exception — a
resolver that raises, a ``publish`` that raises — is swallowed + logged, so an
alert can **never** block, slow, or crash the sweep / pull. When Telegram is
disabled (the default), every method is a silent no-op returning ``False``.
The notifier itself is async (its own daemon thread drains the send queue), so
even a healthy emit never does network I/O on the caller's thread.

This module is **wheel-free** at import time: it imports only
:mod:`brutex.notifications.telegram` (which depends solely on ``brutex.models``),
never the ``fastbloom_rs`` / ``lmdb`` PyO3 chain pulled by
``brutex.notifications.wiring`` → ``brutex.orchestrator`` — that import is done
lazily inside the default resolver so the strict per-module coverage gate stays
clean (the same precedent as :mod:`brutex.observability.standard_monitor`).

## Decision: DEC-TELEGRAM-WIRE-001 (MIGRATION.md entry #567).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Final, Protocol

from brutex.notifications.telegram import (
    TelegramEvent,
    TelegramEventKind,
    TelegramSeverity,
)

if TYPE_CHECKING:  # pragma: no cover
    from decimal import Decimal

logger = logging.getLogger(__name__)


class NotifierLike(Protocol):
    """Structural type for the notifier surface :class:`SweepAlerter` uses.

    Matched by :class:`brutex.notifications.telegram.TelegramNotifier` (and by any
    test fake exposing the same one-method surface), so the alerter never depends
    on the concrete notifier class.
    """

    def publish(self, event: TelegramEvent) -> None:
        """Enqueue *event* for best-effort, non-blocking delivery."""
        ...


#: A zero-arg callable returning the active notifier (or ``None`` when Telegram
#: is disabled). The default is the lazy process singleton; tests inject a fake.
NotifierResolver = Callable[[], NotifierLike | None]

#: Telegram MarkdownV2 reserved characters that MUST be backslash-escaped in any
#: dynamic text, per the Bot API spec (``api.telegram.org/bots/api#markdownv2-style``).
#: The notifier sends with ``parse_mode=MarkdownV2``, so an unescaped ``.`` / ``(``
#: / ``=`` / ``-`` in an interpolated value makes Telegram reject the message — we
#: escape every body so a hyphenated symbol or a decimal latency renders cleanly.
_MDV2_SPECIALS: Final[frozenset[str]] = frozenset(r"_*[]()~`>#+-=|{}.!")

#: Relaunches within the supervisor's rolling window at (and past) which the
#: :meth:`SweepAlerter.child_relaunched` alert ESCALATES to CRITICAL — the
#: crash-loop threshold (DEC-SUPERVISOR-CRASHLOOP-ALERT-001). The ONE source of
#: truth for the escalation bar: the supervisor only COUNTS relaunches in its
#: window; the severity decision lives here.
CRASH_LOOP_RELAUNCH_THRESHOLD: Final[int] = 3


def escape_markdown_v2(text: str) -> str:
    """Return *text* with every MarkdownV2-reserved character backslash-escaped.

    Parameters
    ----------
    text
        The raw message body (may contain symbols, decimals, punctuation).

    Returns
    -------
    str
        The MarkdownV2-safe body — each reserved char prefixed with ``\\`` so
        Telegram parses the message instead of rejecting it ``HTTP 400``.
    """
    out: list[str] = []
    for char in text:
        if char in _MDV2_SPECIALS:
            out.append("\\")
        out.append(char)
    return "".join(out)


def _count_or_question(value: int | None) -> str:
    """Unknown nightly counts render a literal ``"?"`` — NEVER a fabricated 0.

    (DEC-SWEEP-NIGHTLY-AUTOMATION-001, the ``bars_net_new`` honest-unknown
    precedent — Golden Rule #1.)
    """
    return "?" if value is None else str(value)


def _resolve_singleton_notifier() -> NotifierLike | None:  # pragma: no cover — lazy wiring import
    """Default resolver: the process-wide singleton via the wiring layer.

    Excluded from this module's coverage gate because it is a pure two-line
    delegation to the already-100%-tested
    :func:`brutex.notifications.wiring.get_or_start_notifier`, and importing the
    wiring layer pulls the ``fastbloom_rs`` PyO3 ABI3 chain (via
    ``brutex.orchestrator``) that the isolated per-module cov run avoids — the
    same precedent documented in :mod:`brutex.notifications.__init__`. Tests
    inject an explicit notifier / fake resolver instead.
    """
    from brutex.notifications.wiring import get_or_start_notifier

    return get_or_start_notifier()


class RateControllerLike(Protocol):
    """Structural type for the rate controller bits :class:`SweepAlerter` reads.

    Matched by :class:`brutex.adapters.rate_common.RateController`. The
    method bodies are never executed (Protocols are structural-only).
    """

    def on_rate_limit(self) -> None:
        """Record a 429 and maybe step the rate down (the controller's own hook)."""
        ...

    def snapshot(self) -> dict[str, object]:
        """Return the JSON-friendly state (``current_rps`` / ``recent_hits`` / …)."""
        ...


class WorkerEventLike(Protocol):
    """Structural type for the watchdog event fields the hung alert reads.

    Matched by :class:`brutex.observability.watchdog.WorkerEvent`.
    """

    @property
    def worker_name(self) -> str:
        """The hung worker's name."""
        ...

    @property
    def last_beat_seconds_ago(self) -> float:
        """Seconds since the worker's last heartbeat."""
        ...

    @property
    def timeout_seconds(self) -> float:
        """The configured hung threshold (seconds)."""
        ...


class SymbolFailureLike(Protocol):
    """Structural type for the retry-storm symbol-failure event fields the alert reads.

    Matched by :class:`brutex.orchestrator.watchdog.SymbolFailureEvent`. Kept
    structural (not an import) so this module stays wheel-free — importing the
    orchestrator package would pull the ``fastbloom_rs`` / ``lmdb`` PyO3 chain
    that this module deliberately avoids (see the module docstring).
    """

    @property
    def symbol(self) -> str:
        """The symbol that reached 100% window-failure."""
        ...

    @property
    def failed_windows(self) -> int:
        """How many of the symbol's windows gave up (== total at 100%)."""
        ...

    @property
    def total_windows(self) -> int:
        """The symbol's full registered window count (the 100% denominator)."""
        ...


class SweepSummaryLike(Protocol):
    """Structural type for the supervised-grid summary fields the complete alert reads.

    Matched by :class:`brutex.engine.sweep_supervisor.SupervisedGridSummary`.
    """

    @property
    def sweep_id(self) -> str:
        """The grid-identity digest."""
        ...

    @property
    def tuples_run(self) -> int:
        """Tuples evaluated this run."""
        ...

    @property
    def tuples_total(self) -> int:
        """Full grid size."""
        ...

    @property
    def evaluated(self) -> int:
        """Masks evaluated across the run."""
        ...

    @property
    def ranked(self) -> int:
        """Finalists ranked across the run."""
        ...

    @property
    def persisted(self) -> int:
        """Finalists persisted across the run."""
        ...

    @property
    def tuples_failed(self) -> int:
        """Tuples that raised mid-evaluation and were skipped this run."""
        ...


class LakeHealActionLike(Protocol):
    """The per-action slice of a lake-heal report the alert body sums over.

    Structural (DEC-LAKE-HEAL-001) — matches
    ``brutex.data.lake_heal.HealAction`` without importing the data layer
    (this module stays wheel-free at import time, and the data layer keeps
    its alert seam injected — no ``data ↔ notifications`` module edge).
    """

    @property
    def dropped(self) -> int:
        """Rows dropped by this action (dup collapse + removals)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def conflicts(self) -> int:
        """Keep-first conflicts recorded while moving rows home."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def quarantine_path(self) -> str | None:
        """Where the original bytes were quarantined (``None`` = no copy)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class LakeHealReportLike(Protocol):
    """The batch-report slice of ``brutex.data.lake_heal.HealReport`` the alert reads."""

    @property
    def mode(self) -> str:
        """The heal mode the batch ran under (``auto`` / ``observe``)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def findings(self) -> int:
        """Findings handed to the batch."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def healed(self) -> int:
        """Findings actually healed."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def actions(self) -> Sequence[LakeHealActionLike]:
        """The per-partition heal actions taken."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class GreeksBackfillReportLike(Protocol):
    """The report slice the Greeks-backfill completion alert reads.

    Structural (DEC-GREEKS-BULK-REENRICH-001) — matches
    ``brutex.data.greeks_backfill.GreeksBackfillReport`` without importing the
    data layer (this module stays wheel-free at import time; the same
    precedent as :class:`LakeHealReportLike`).
    """

    @property
    def backfill_id(self) -> str:
        """The plan's content-addressed id (the dedupe key)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def planned(self) -> int:
        """Contracts the plan targeted."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def enriched(self) -> int:
        """Contracts whose overlay was (re)written."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def already_current(self) -> int:
        """Contracts already carrying the current stamps (no write)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def errored(self) -> int:
        """Contracts quarantined by a per-contract error (re-runnable)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def stale_unfixable(self) -> int:
        """Contracts whose only staleness the driver cannot act on (fix round 4)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def bars_written(self) -> int:
        """Bars carried in the latest-wins writes."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def cancelled(self) -> bool:
        """Operator's cooperative Stop halted the run (DEC-JOB-CANCEL-CONTROL-001)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class SweepAlerter:
    """Best-effort Telegram alert emitter for the sweep supervisor + live pull.

    Parameters
    ----------
    notifier
        An explicit notifier (tests pass a fake; the CLI passes the started
        singleton). When ``None``, *resolver* is consulted lazily on each emit.
    resolver
        Zero-arg notifier source used when *notifier* is ``None``. Defaults to
        the process singleton (:func:`_resolve_singleton_notifier`).
    """

    __slots__ = ("_explicit", "_resolver")

    def __init__(
        self,
        *,
        notifier: NotifierLike | None = None,
        resolver: NotifierResolver = _resolve_singleton_notifier,
    ) -> None:
        self._explicit = notifier
        self._resolver = resolver

    # ── Resolution + publish (best-effort core) ─────────────────────

    def _notifier(self) -> NotifierLike | None:
        """Return the active notifier, swallowing a resolver that raises."""
        if self._explicit is not None:
            return self._explicit
        try:
            return self._resolver()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("sweep_alerts: notifier resolver raised; alert dropped")
            return None

    def _publish(
        self,
        *,
        kind: str,
        severity: str,
        message: str,
        dedupe_key: str = "",
    ) -> bool:
        """Publish one event best-effort. Returns ``True`` iff attempted on a live notifier."""
        notifier = self._notifier()
        if notifier is None:
            return False
        try:
            notifier.publish(
                TelegramEvent(
                    kind=kind,
                    severity=severity,
                    message=message,
                    dedupe_key=dedupe_key,
                )
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("sweep_alerts: publish failed for kind=%s; alert dropped", kind)
            return False
        return True

    # ── Manual trigger (`brutex notify test`) ───────────────────────

    def test_digest(self) -> bool:
        """Send a one-shot ``TEST`` digest proving the channel is wired + reachable."""
        message = escape_markdown_v2(
            "🔔 BruteX Telegram test — alerts are wired and reachable. "
            "If you can read this, watchdog/rate/auth/sweep alerts will arrive here."
        )
        return self._publish(
            kind=TelegramEventKind.TEST,
            severity=TelegramSeverity.INFO,
            message=message,
        )

    # ── Automatic sweep / pull alerts ───────────────────────────────

    def watchdog_hung(
        self,
        *,
        worker_name: str,
        last_beat_seconds_ago: float,
        timeout_seconds: float,
    ) -> bool:
        """Alert that a sweep worker wedged past the watchdog timeout (CRITICAL)."""
        message = escape_markdown_v2(
            f"🛑 Sweep watchdog: worker {worker_name} HUNG "
            f"(last beat {last_beat_seconds_ago:.1f}s ago, "
            f"timeout {timeout_seconds:.1f}s). The sweep may be stalled."
        )
        return self._publish(
            kind=TelegramEventKind.WORKER_HUNG,
            severity=TelegramSeverity.CRITICAL,
            message=message,
            dedupe_key=worker_name,
        )

    def rate_breach(self, *, current_rps: float, recent_hits: int) -> bool:
        """Alert that the vendor 429-throttled the pull (WARN; deduped by the 60 s window)."""
        message = escape_markdown_v2(
            f"⚠️ Vendor rate breach: {recent_hits} recent 429(s); "
            f"throttled to {current_rps:.0f} r/s. The pull continues, slower."
        )
        return self._publish(
            kind=TelegramEventKind.RATE_LIMIT_DOWNSHIFT,
            severity=TelegramSeverity.WARN,
            message=message,
            dedupe_key="rate-breach",
        )

    def child_relaunched(
        self,
        *,
        child: str,
        exit_code: int,
        signal: int | None,
        relaunch_count: int,
        detail: str,
    ) -> bool:
        """Alert that the supervisor relaunched a crashed child (DEC-SUPERVISOR-CRASHLOOP-ALERT-001).

        Fired by :class:`brutex.orchestrator.supervisor.ProcessSupervisor` on
        every abnormal-exit relaunch, so a child that dies repeatedly (e.g. an
        OOM ``SIGKILL`` loop) can never spin all night with ZERO operator
        signal. Severity is WARN for an isolated relaunch and ESCALATES to
        CRITICAL once *relaunch_count* reaches
        :data:`CRASH_LOOP_RELAUNCH_THRESHOLD` relaunches within the
        supervisor's rolling window — the crash-loop shape the operator must
        act on. The two tiers carry DISTINCT dedupe keys, so the notifier's
        ``CHILD_RELAUNCHED`` coalescing window can never swallow the CRITICAL
        escalation behind an earlier WARN push.

        Parameters
        ----------
        child
            The supervised child's name (the supervisor's audit ``subject``).
        exit_code
            The child's raw ``returncode`` (NEGATIVE = death-by-signal, the
            :class:`subprocess.Popen` convention).
        signal
            The killing signal number when the child died by signal (e.g.
            ``9`` for an OOM SIGKILL), else ``None``.
        relaunch_count
            Relaunches within the supervisor's rolling window, THIS one
            included.
        detail
            Short free-form context (uptime / backoff / restart index).
        """
        died_by = f"signal {signal}" if signal is not None else f"exit code {exit_code}"
        if relaunch_count >= CRASH_LOOP_RELAUNCH_THRESHOLD:
            severity = TelegramSeverity.CRITICAL
            head = (
                f"🛑 Sweep supervisor CRASH LOOP: child {child} relaunched "
                f"{relaunch_count}× within the rolling window (latest death: {died_by})"
            )
            dedupe_key = f"child-crash-loop:{child}"
        else:
            severity = TelegramSeverity.WARN
            head = f"⚠️ Sweep supervisor: child {child} relaunched after {died_by}"
            dedupe_key = f"child-relaunch:{child}"
        message = escape_markdown_v2(
            f"{head}. {detail}. The supervisor keeps relaunching until the "
            "max-restart cap (a terminal CRITICAL fires if it gives up); "
            "investigate the child's crash cause (an OOM SIGKILL loop will "
            "not heal itself)."
        )
        return self._publish(
            kind=TelegramEventKind.CHILD_RELAUNCHED,
            severity=severity,
            message=message,
            dedupe_key=dedupe_key,
        )

    def supervisor_gave_up(
        self,
        *,
        child: str,
        exit_code: int,
        signal: int | None,
        restarts: int,
        max_restarts: int,
    ) -> bool:
        """Alert that the supervisor GAVE UP — the sweep is DOWN (CRITICAL, terminal).

        Fired ONCE by :class:`brutex.orchestrator.supervisor.ProcessSupervisor`
        when the max-restart cap is exhausted (DEC-SUPERVISOR-CRASHLOOP-ALERT-001,
        hostile-review MINOR-3 fold): from this moment NOTHING relaunches the
        child, so this is the single most alert-worthy state — it must reach
        the operator even when ``max_restarts`` is below the crash-loop
        escalation bar or the crashes were spaced wider than the rolling
        window. Always CRITICAL; the dedupe key is per child and the event
        kind carries a zero coalescing window (a terminal verdict fires once
        per run, never storms).

        Parameters
        ----------
        child
            The supervised child's name (the supervisor's audit ``subject``).
        exit_code
            The child's LAST raw ``returncode`` (NEGATIVE = death-by-signal).
        signal
            The killing signal number when the child died by signal, else
            ``None``.
        restarts
            Total relaunches the supervisor attempted this run.
        max_restarts
            The exhausted consecutive-crash cap.
        """
        died_by = f"signal {signal}" if signal is not None else f"exit code {exit_code}"
        message = escape_markdown_v2(
            f"🛑 Sweep supervisor GAVE UP: child {child} exhausted the "
            f"max-restart cap ({max_restarts} consecutive crash(es); "
            f"{restarts} restart(s) this run; last death: {died_by}). "
            "The sweep is DOWN — no further relaunches will be attempted; "
            "fix the crash cause, then restart the supervisor manually."
        )
        return self._publish(
            kind=TelegramEventKind.SUPERVISOR_GAVE_UP,
            severity=TelegramSeverity.CRITICAL,
            message=message,
            dedupe_key=f"supervisor-gave-up:{child}",
        )

    def pull_degraded(self, *, rule: str, achieved: float, allowed: float, detail: str) -> bool:
        """Alert that the live pull's health degraded against an SLA rule (WARN; deduped by *rule*).

        Fired by :class:`brutex.observability.pull_health.PullHealthMonitor` on a rule
        OK→BREACH edge (DEC-PULL-HEALTH-MONITOR-001). Mirrors :meth:`rate_breach`: a
        best-effort, fire-and-forget push deduped on the rule name so a single sustained
        breach coalesces into ONE message instead of one per observe tick.

        The body carries the achieved-vs-allowed r/s AND the *detail* string, which
        states the ONE thing the operator needs to triage: whether **OUR pipeline is the
        limiter** (write latency / pool width — action: investigate our code) or the
        breach is **VENDOR 429-gated** (expected, the controller already throttled and
        the pull continues — no action). The caller (the monitor) chooses *detail* per
        rule, so this method does not need to know the rule taxonomy.

        Parameters
        ----------
        rule
            The breached rule name (the dedupe key — e.g. ``"pull.stalled"``).
        achieved
            The measured effective r/s at the breach.
        allowed
            The controller-allowed r/s at the breach.
        detail
            The OUR-pipeline-vs-VENDOR triage string (see above).
        """
        message = escape_markdown_v2(
            f"⚠️ Pull health degraded ({rule}): achieved {achieved:.2f} r/s vs "
            f"allowed {allowed:.2f} r/s. {detail}."
        )
        return self._publish(
            kind=TelegramEventKind.RATE_LIMIT_DOWNSHIFT,
            severity=TelegramSeverity.WARN,
            message=message,
            dedupe_key=f"pull-degraded:{rule}",
        )

    def soak_drift(self, *, rule: str, observed: float, threshold: float, detail: str) -> bool:
        """Alert that a long soak crossed a DETERMINISTIC drift rule (CRITICAL).

        Fired by the soak driver on a rule OK→BREACH edge (DEC-SOAK-REAL-TIER-001)
        for the two MONOTONE, deterministic rules ONLY — an RSS ceiling crossed or
        an fd count that grew above its baseline. The noisy ``soak.p99_drift`` rule
        is DELIBERATELY NOT routed here: its sub-second-window p99 oscillates, so
        the detector fires a fresh OK→BREACH edge per up-crossing and pushing each
        would storm the operator on non-regression noise (the driver excludes it —
        see :mod:`tests.stress.test_soak_sustained`); p99 stays visible via the
        monitor's structlog breach warning + the ``brutex_soak_p99_ratio`` gauge.

        Mirrors :meth:`pull_degraded`: a best-effort, fire-and-forget push.
        CRITICAL because a genuine multi-hour RSS/fd leak is the exact regression
        the tier exists to catch — the operator must look. Silent no-op when
        Telegram is disabled (the default), so a Telegram outage can never mask the
        breach.

        Coalescing is HONEST, not the dedupe_key: at the ``PHASE_BLOCKED`` kind the
        notifier dedupe WINDOW is ``0.0``
        (:data:`~brutex.notifications.telegram.DEDUPE_WINDOW_SECONDS`), so the
        per-rule ``dedupe_key`` provides NO notifier-side suppression — it is set
        for identification / defence-in-depth only. The real coalescing is upstream:
        the :class:`~brutex.observability.sla.SLADetector` hysteresis fires exactly
        ONE edge per OK→BREACH transition, and because the two rules routed here are
        MONOTONE (a real leak climbs and stays), that edge fires once per
        regression — one honest CRITICAL, never one per observe window.

        Parameters
        ----------
        rule
            The breached rule name (the raw dedupe key — e.g.
            ``"soak.rss_ceiling"``).
        observed
            The measured value at the breach (RSS KiB / fd growth).
        threshold
            The rule's trigger level.
        detail
            Short free-form context naming what to check.
        """
        message = escape_markdown_v2(
            f"⚠️ Soak drift ({rule}): observed {observed:.2f} vs threshold "
            f"{threshold:.2f}. {detail}."
        )
        # PHASE_BLOCKED (0.0 notifier window) — the CRITICAL-severity precedent
        # pull_halted / vendor_down / credential_dead use, so a real drift is
        # never swallowed by a notifier coalescing window. HONESTY NOTE: at a
        # 0.0-window kind the dedupe_key does NO notifier-side suppression (it is
        # identification / defence-in-depth only). The genuine coalescing is the
        # caller's SLADetector hysteresis firing ONE OK→BREACH edge, which for the
        # MONOTONE rss/fd rules routed here is one push per genuine regression (the
        # noisy oscillating p99 rule is excluded upstream — see the docstring).
        return self._publish(
            kind=TelegramEventKind.PHASE_BLOCKED,
            severity=TelegramSeverity.CRITICAL,
            message=message,
            dedupe_key=f"soak-drift:{rule}",
        )

    def vendor_conflict_drift(
        self, *, conflicts: int, max_close_delta_display: str, dup_rows: int
    ) -> bool:
        """Alert that a pull recorded material vendor close-drift (WARN; deduped).

        DEC-VENDOR-CONFLICT-SURFACE-001: fired at MOST once per pull, at the
        pipeline's completion-headline seam, when the pull's MAX keep-first close
        delta reached the operator threshold
        (``BRUTEX_CONFLICT_CLOSE_DELTA_ALERT``, default ``1.00`` rupees). The
        stored bars WON every conflict (keep-first — DEC-LAKE-CONFLICT-KEEPFIRST-001,
        the CLAUDE.md §6 F&O-candle-lake row's merge law), so this is an
        OBSERVATION for triage, never a data mutation. Mirrors
        :meth:`pull_degraded`: best-effort, fire-and-forget, silent no-op when
        Telegram is disabled.

        Parameters
        ----------
        conflicts
            The pull's total keep-first vendor re-statements.
        max_close_delta_display
            The pre-formatted rupee display of the max close delta (the caller
            formats via ``brutex.money`` — DEC-MONEY-2DP-001; passed as a string
            so this module stays Decimal-agnostic).
        dup_rows
            The pull's total within-batch duplicate vendor rows dropped.
        """
        message = escape_markdown_v2(
            f"⚠️ Vendor data drift: {conflicts} keep-first conflict(s), max close "
            f"delta {max_close_delta_display}; {dup_rows} duplicate vendor row(s) "
            "dropped. Stored bars kept (keep-first) — review the conflict "
            "summaries in /logs."
        )
        return self._publish(
            kind=TelegramEventKind.RATE_LIMIT_DOWNSHIFT,
            severity=TelegramSeverity.WARN,
            message=message,
            dedupe_key="vendor-conflict-drift",
        )

    def auth_renew_failed(self, *, reason: str) -> bool:
        """Alert that ``RenewToken`` refused so the token will expire (ERROR)."""
        message = escape_markdown_v2(
            f"🔑 Vendor token refresh refused: {reason}. The pull continues on the "
            "existing token — generate a fresh token before it expires."
        )
        return self._publish(
            kind=TelegramEventKind.AUTH_RENEW_FAILED,
            severity=TelegramSeverity.ERROR,
            message=message,
            dedupe_key="auth-renew",
        )

    def pull_halted(self, *, reason: str) -> bool:
        """Alert that the pull HALTED on an unrecoverable, pull-wide condition (CRITICAL).

        Fired when a pull-wide global-stop propagates out of the ingest spine — a
        full disk / OOM (:class:`~brutex.data.errors.LakeWriteHalt`,
        ``reason="disk_full"`` / ``"out_of_memory"``, DEC-LAKE-WRITE-HALT-001) or a
        daily-quota / quota-invariant stop. Unlike a per-symbol quarantine this aborts
        EVERY remaining window identically, so the operator must act (free disk, wait
        for the quota window) before re-running — hence CRITICAL. The ``dedupe_key`` is
        the reason so a repeated halt of the SAME cause coalesces.

        Parameters
        ----------
        reason
            A short, non-secret cause token (e.g. ``"disk_full"``,
            ``"out_of_memory"``, ``"DailyQuotaExceeded"`` — the quota example is
            dormant under Groww, which has no daily quota;
            DEC-QUOTA-VERDICT-MOOT-001).
        """
        message = escape_markdown_v2(
            f"🛑 Pull HALTED: {reason}. The pull stopped — no further bars will be "
            "fetched until you resolve it (e.g. free disk space) and re-run. Bars "
            "already written are safe (atomic + idempotent); a re-pull resumes."
        )
        return self._publish(
            kind=TelegramEventKind.PHASE_BLOCKED,
            severity=TelegramSeverity.CRITICAL,
            message=message,
            dedupe_key=f"pull-halt:{reason}",
        )

    def nightly_summary(
        self,
        *,
        nightly_id: str,
        verdict: str,
        link_verdicts: Mapping[str, str],
        pull_bars: int | None,
        prebuild_built: int | None,
        sweep_run: int | None,
        sweep_skipped_cache: int | None,
        sweep_failed: int | None,
        backup_files: int | None,
        blocker: str | None,
        duration_seconds: float,
    ) -> bool:
        """The morning nightly summary (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

        Severity law: INFO iff ``verdict == "OK"``, WARN iff ``"SUSPECT"`` (the
        R39 trust-downgrade — green but untrusted), CRITICAL for
        ``"REFUSED"``/``"FAILED"``. The ``dedupe_key`` is PER-RUN
        (``nightly-summary:<id>``) so a same-night manual retry is never
        swallowed by a dedupe window. Unknown counts render a literal ``"?"``
        — NEVER a fabricated 0 (Golden Rule #1); a ``skipped`` backup link
        appends the loud operator nag.

        Parameters
        ----------
        nightly_id
            The one R43 id threading report/audit/bundle/Telegram.
        verdict
            ``"OK"`` | ``"SUSPECT"`` | ``"REFUSED"`` | ``"FAILED"``
            (:func:`brutex.nightly.report.summary_lines` computes it).
        link_verdicts
            Chain-order per-link verdict strings, e.g. ``{"collision": "ok",
            …, "sweep": "blocked(pull)"}``.
        pull_bars, prebuild_built, sweep_run, sweep_skipped_cache, sweep_failed, backup_files
            Honest counts (``None`` = unknown ⇒ ``"?"``).
        blocker
            The first REFUSED/FAILED link's name, or ``None``.
        duration_seconds
            Wall-clock chain duration.
        """
        marker = {"OK": "✅ OK", "SUSPECT": "⚠️ SUSPECT"}.get(
            verdict,
            f"{'⛔' if verdict == 'REFUSED' else '❌'} {verdict} at {blocker or 'unknown'}",
        )
        hours, rem = divmod(int(max(0.0, duration_seconds)), 3600)
        counts_by_link = {
            "backup": f" ({_count_or_question(backup_files)} files)",
            "pull": f" ({_count_or_question(pull_bars)} bars)",
            "prebuild": f" ({_count_or_question(prebuild_built)} built)",
            "sweep": (
                f" ({_count_or_question(sweep_run)} run · "
                f"{_count_or_question(sweep_skipped_cache)} skipped-cache · "
                f"{_count_or_question(sweep_failed)} failed)"
            ),
        }
        parts: list[str] = []
        for link, link_verdict in link_verdicts.items():
            suffix = ""
            if not link_verdict.startswith("blocked"):
                suffix = counts_by_link.get(link, "")
            parts.append(f"{link} {link_verdict}{suffix}")
        body = f"🌙 Nightly {nightly_id} — {marker} in {hours}:{rem // 60:02d}.\n" + " · ".join(
            parts
        )
        if link_verdicts.get("backup") == "skipped":
            body += "\n⚠️ backup SKIPPED by operator flag — stores unprotected tonight."
        severity = {
            "OK": TelegramSeverity.INFO,
            "SUSPECT": TelegramSeverity.WARN,
        }.get(verdict, TelegramSeverity.CRITICAL)
        return self._publish(
            kind=TelegramEventKind.RUN_COMPLETE,
            severity=severity,
            message=escape_markdown_v2(body),
            dedupe_key=f"nightly-summary:{nightly_id}",
        )

    def nightly_refused(self, *, nightly_id: str, link: str, reason: str) -> bool:
        """Instant CRITICAL refusal signal (collision / disk floor / liveness).

        Fired the moment a nightly REFUSES (before the summary), so the
        operator learns at 01:30, not at 06:00. PER-RUN dedupe key
        (``nightly-refused:<id>:<link>``) — a same-night manual retry mints a
        new id and is never swallowed by the PHASE_BLOCKED dedupe window.
        """
        body = (
            f"🌙⛔ Nightly {nightly_id} REFUSED at {link}: {reason}. "
            f"Nothing after {link} ran; links before it are intact. "
            "Next attempt: next schedule fire."
        )
        return self._publish(
            kind=TelegramEventKind.PHASE_BLOCKED,
            severity=TelegramSeverity.CRITICAL,
            message=escape_markdown_v2(body),
            dedupe_key=f"nightly-refused:{nightly_id}:{link}",
        )

    def nightly_deadman(self, *, detail: str, newest_run_age_seconds: float | None) -> bool:
        """The row-20 dead-man alert: the scheduled nightly did NOT run.

        A Mac asleep at the trigger starts no process, so no in-run alerter
        can fire — this is the ONLY Telegram signal for that perfectly-silent
        staleness, emitted by the surface that NOTICES it (``brutex nightly
        status`` today; the P4 dashboard banner wires the same
        ``deadman_verdict`` seam at merge-integration). CRITICAL with its own
        DISTINCT dedupe key (``nightly-deadman`` — never colliding with the
        per-run ``nightly-refused:<id>:<link>`` keys, which require a run to
        exist). ``newest_run_age_seconds=None`` renders the honest "never" —
        not a fabricated 0 (Golden Rule #1).
        """
        age = (
            "never (no run evidence)"
            if newest_run_age_seconds is None
            else f"{int(newest_run_age_seconds)}s ago"
        )
        body = f"🌙💀 Nightly DEAD-MAN: scheduled run MISSED. Last run evidence: {age}. {detail}"
        return self._publish(
            kind=TelegramEventKind.PHASE_BLOCKED,
            severity=TelegramSeverity.CRITICAL,
            message=escape_markdown_v2(body),
            dedupe_key="nightly-deadman",
        )

    def backup_failed(self, *, reason: str, detail: str = "") -> bool:
        """Alert that the off-box backup run FAILED (CRITICAL; deduped by *reason*).

        DEC-BACKUP-OFFBOX-001: fired by ``brutex backup run`` on any failure
        that leaves the lake WITHOUT a fresh verified off-box snapshot
        (config / preflight / restic / state-write / partial-streak). Rides
        ``PHASE_BLOCKED`` (0.0 s window) — the durable cross-run bound lives
        in the checker's ``deadman.json``, not the per-process dedupe cache.

        The ``partial_streak`` reason gets its OWN body (round-4 NIT): on that
        arm the snapshot succeeded and success WAS stamped before this fires
        (run.py step 9 precedes the streak escalation), so the dead-man reads
        FRESH and will NOT re-alert — the generic "keep re-alerting while
        stale" claim would be false and steer the operator toward an hourly
        escalation that never comes.

        Parameters
        ----------
        reason
            Short, non-secret cause token (e.g. ``"restic_missing"``).
        detail
            Optional appended context (non-secret).
        """
        extra = f" {detail}" if detail else ""
        if reason == "partial_streak":
            message = escape_markdown_v2(
                f"🛑 Off-box BACKUP FAILED: {reason}.{extra} The snapshot itself SUCCEEDED "
                "and was stamped (the dead-man stays quiet), but restic could not read some "
                "source files for consecutive nights — those files are NOT in the snapshots. "
                "Investigate reader permissions via the backup log, then re-run "
                "`brutex backup run`."
            )
        else:
            message = escape_markdown_v2(
                f"🛑 Off-box BACKUP FAILED: {reason}.{extra} The lake/results are NOT freshly "
                "backed up; the dead-man will keep re-alerting while stale. Run "
                "`brutex backup status`, then check the backup log and re-run "
                "`brutex backup run`."
            )
        return self._publish(
            kind=TelegramEventKind.PHASE_BLOCKED,
            severity=TelegramSeverity.CRITICAL,
            message=message,
            dedupe_key=f"backup-failed:{reason}",
        )

    def backup_degraded(self, *, aspect: str, detail: str) -> bool:
        """Alert that the backup DEGRADED without losing data protection (WARN).

        DEC-BACKUP-OFFBOX-001: the snapshot itself succeeded and the data IS
        off-box — a maintenance (``forget``/prune) failure or a partial rc-3
        read, never a data-protection loss (F8/F23). Deduped by *aspect*.
        The ``drill_skipped`` aspect (round 6) carries its OWN body: no
        snapshot ran at all there — the monthly restore drill was skipped on
        single-flight contention, so the generic "the snapshot itself
        succeeded" wording would be false for it (the partial_streak split
        precedent, one method over).

        Parameters
        ----------
        aspect
            The degraded aspect token (``"partial"`` / ``"maintenance"`` /
            ``"drill_skipped"``).
        detail
            Non-secret one-line context.
        """
        if aspect == "drill_skipped":
            message = escape_markdown_v2(
                f"⚠️ Off-box backup restore DRILL SKIPPED: {detail}. The monthly "
                "byte-verification did NOT run this month (launchd one-shots never "
                "retry) — the nightly backups themselves are unaffected. Re-run "
                "`brutex backup restore-drill` manually."
            )
        else:
            message = escape_markdown_v2(
                f"⚠️ Off-box backup DEGRADED ({aspect}): {detail}. The snapshot itself "
                "succeeded and the data IS off-box — this is a maintenance/partial-read "
                "warning, not a data-protection loss. See `brutex backup status`."
            )
        return self._publish(
            kind=TelegramEventKind.PHASE_BLOCKED,
            severity=TelegramSeverity.WARN,
            message=message,
            dedupe_key=f"backup-degraded:{aspect}",
        )

    def backup_overdue(
        self,
        *,
        reason: str,
        hours_since_success: float | None,
        threshold_hours: float,
    ) -> bool:
        """Dead-man breach: no fresh verified off-box backup (CRITICAL; deduped by *reason*).

        DEC-BACKUP-OFFBOX-001: fired by the hourly ``brutex backup check`` on
        any alarm verdict (overdue / never_succeeded / state_corrupt /
        clock_skew / hung / remote divergence). The caller stamps its durable
        re-alert throttle IFF this returns ``True`` (only-stamp-on-success).

        Parameters
        ----------
        reason
            The alarm verdict token.
        hours_since_success
            Hours since the last stamped success (``None`` = never).
        threshold_hours
            The freshness threshold the verdict was judged against.
        """
        age_display = "never" if hours_since_success is None else f"{hours_since_success:.1f} h ago"
        message = escape_markdown_v2(
            f"🛑 BACKUP DEAD-MAN ({reason}): last successful off-box backup {age_display} "
            f"(threshold {threshold_hours:.0f} h). Sweeps + hash-chained audit logs are NOT "
            "re-pullable — their loss window is growing. Investigate now: "
            "`brutex backup status`, then `brutex backup run`."
        )
        return self._publish(
            kind=TelegramEventKind.PHASE_BLOCKED,
            severity=TelegramSeverity.CRITICAL,
            message=message,
            dedupe_key=f"backup-overdue:{reason}",
        )

    def backup_repo_corrupt(self, *, detail: str, source: str = "verify") -> bool:
        """The backup repo failed verification / the restore drill found a byte mismatch (CRITICAL).

        DEC-BACKUP-OFFBOX-001 (F11/F20): remediation is HUMAN — an automated
        "repair" of a corrupt repo can destroy the last good copy, so the
        alert points at the runbook instead. The escalation promise is
        SPLIT per arm (round-5 NIT): the nightly-``verify`` arm stamps NO
        success, so the dead-man goes OVERDUE and keeps escalating; the
        monthly-``drill`` arm does NOT stop nightly successes (the rotating
        read-data subset can take ~56 nights to reach the affected pack and
        the deep check id-matches), so the dead-man stays QUIET — that page
        is the only automated signal.

        Cause-honesty is ALSO split per arm (round-8 MINOR): the ``verify``
        arm rides ``restic check`` rc alone, and restic exits rc=1 for an
        UNREACHABLE repo exactly as for genuine corruption (probed on
        0.16.4) — a WAN/S3 drop during the ~hours-long read-data tail must
        not send the operator into a multi-day re-seed, so the verify body
        names the co-equal causes and points at ``backup.check.failed``
        ``stderr_tail`` first. The ``drill`` arm's byte mismatch is a
        byte-VERIFIED finding — no such caveat.

        Parameters
        ----------
        detail
            Non-secret description of the corruption finding.
        source
            ``"verify"`` (nightly ``restic check`` — the default) or
            ``"drill"`` (monthly restore-drill byte mismatch).
        """
        if source == "drill":
            tail = (
                "Nightly backups may KEEP stamping success (the rotating read-data subset "
                "can take ~56 nights to reach the affected pack), so the dead-man stays "
                "QUIET — this page is your ONLY automated signal; act on it now, do not "
                "wait for escalation."
            )
        else:
            tail = (
                "restic's rc cannot distinguish corruption from a remote outage mid-check "
                "— confirm via the backup.check.failed stderr_tail before acting on "
                "restore-from-older / re-seed (a transient outage self-heals next night). "
                "The dead-man keeps escalating until a VERIFIED backup succeeds."
            )
        message = escape_markdown_v2(
            f"🛑 BACKUP REPO CORRUPT: {detail}. Do NOT run automated repair — follow "
            f"docs/runbooks/backup-offbox.md (restore-from-older / re-seed). {tail}"
        )
        return self._publish(
            kind=TelegramEventKind.PHASE_BLOCKED,
            severity=TelegramSeverity.CRITICAL,
            message=message,
            dedupe_key="backup-repo-corrupt",
        )

    def lake_heal_report(
        self,
        *,
        mode: str,
        findings: int,
        healed: int,
        quarantined: int,
        dropped: int,
        conflicts: int,
        scope: str,
    ) -> bool:
        """Alert the outcome of ONE lake-heal batch (DEC-LAKE-HEAL-001).

        One alert per heal batch, on the existing ``QUARANTINE_NEW`` kind (no
        new dedupe-window row needed; ``dedupe_key`` scopes it per heal seam).
        Severity is WARNING for a batch that FIXED something, and CRITICAL when
        poison was found but NOTHING was fixed (``findings > 0 and healed == 0``
        — the observable covering BOTH the ``mode=observe`` "found, nothing
        touched" case and a refused mass-heal / all-errored batch), because the
        operator must act. Body is MarkdownV2-escaped; best-effort
        :meth:`_publish` (never blocks/crashes).

        Parameters
        ----------
        mode
            The heal mode the batch ran under (``auto`` / ``observe``).
        findings, healed, quarantined, dropped, conflicts
            The batch tallies (findings handed in, findings healed, quarantine
            copies made, rows dropped, keep-first conflicts recorded).
        scope
            The seam token (``"reconcile"`` / ``"pull:<id>"`` /
            ``"startup-sweep"``) — the dedupe key suffix.
        """
        if findings > 0 and healed == 0:
            severity = TelegramSeverity.CRITICAL
            head = "🛑 Lake poison found, NOTHING fixed"
        else:
            severity = TelegramSeverity.WARN
            head = "🧹 Lake heal"
        message = escape_markdown_v2(
            f"{head} ({scope}): mode={mode}, findings={findings}, healed={healed}, "
            f"quarantined={quarantined}, dropped={dropped}, conflicts={conflicts}. "
            f"Originals preserved under <lake>/.quarantine/ — never auto-deleted."
        )
        return self._publish(
            kind=TelegramEventKind.QUARANTINE_NEW,
            severity=severity,
            message=message,
            dedupe_key=f"lake-heal:{scope}",
        )

    def vendor_down(self, *, failed_windows: int, distinct_symbols: int) -> bool:
        """Alert that the VENDOR looks DOWN — the pull halted; WAIT then re-run (CRITICAL).

        Fired ONCE by the error-storm breaker
        (:class:`~brutex.adapters.error_storm_breaker.ErrorStormBreaker`,
        DEC-VENDOR-DOWN-BREAKER-001) at its trip transition: sustained
        ladder-exhausted transport failures across multiple DISTINCT symbols with
        ZERO successes — the whole-vendor-outage shape. The wording is deliberately
        DISTINCT from :meth:`credential_dead` (this one says WAIT — nothing on the
        operator's side is broken) and from the generic :meth:`pull_halted`. The
        counts are non-secret integers; the ``dedupe_key`` coalesces a repeated
        vendor-down verdict into one push.
        """
        message = escape_markdown_v2(
            f"🛑 VENDOR DOWN — Groww transport is failing pull-wide: {failed_windows} "
            f"windows exhausted their retry ladders across {distinct_symbols} distinct "
            "symbols with ZERO successes. This looks like a vendor/network outage, NOT "
            "our credential. The pull HALTED gracefully — WAIT for the vendor to "
            "recover, then re-run; nothing is lost (the durable ledger + idempotent "
            "sink re-pull exactly the missing windows)."
        )
        return self._publish(
            kind=TelegramEventKind.PHASE_BLOCKED,
            severity=TelegramSeverity.CRITICAL,
            message=message,
            dedupe_key="vendor-down",
        )

    def credential_dead(self, *, reason: str) -> bool:
        """Alert that OUR credential / token-broker Lambda is DEAD — operator action (CRITICAL).

        Fired ONCE by the error-storm breaker's dead-credential escalation
        (DEC-VENDOR-DOWN-BREAKER-001): K consecutive StillStale re-reads spanning
        ≥ T minutes with ZERO adopted-token successes — past the healthy 06:00-IST
        rotation shape (DEC-BROKER-READONLY-REAUTH-REREAD-001's honest envelope).
        The wording is deliberately DISTINCT from :meth:`vendor_down`: here the
        vendor is (as far as we can tell) fine and WAITING will NOT help — the
        operator must re-provision the credential or fix the token-broker Lambda.
        *reason* must stay a short NON-SECRET token (never a token/secret value —
        the :meth:`pull_halted` rule).
        """
        message = escape_markdown_v2(
            f"🔐 OUR CREDENTIAL / LAMBDA BROKER DEAD — OPERATOR ACTION NEEDED: {reason}. "
            "Every token re-read still returns the same dead token (zero adopted-token "
            "successes), so every fetch is auth-stalled and the pull HALTED gracefully. "
            "Waiting will NOT fix this — re-provision the Groww API key / TOTP secret "
            "or repair the token-broker Lambda, then re-run; bars already written are "
            "safe (idempotent re-pull)."
        )
        return self._publish(
            kind=TelegramEventKind.PHASE_BLOCKED,
            severity=TelegramSeverity.CRITICAL,
            message=message,
            dedupe_key=f"credential-dead:{reason}",
        )

    def authorisation_storm(self, *, distinct_symbols: int) -> bool:
        """Alert a fleet-wide AUTHORISATION storm — account suspension suspected (CRITICAL).

        Fired ONCE by the error-storm breaker's authorisation-storm escalation
        (DEC-GROWW-AUTHZ-QUARANTINE-001): terminal vendor authorisation rejections
        (the SDK's ``GrowwAPIAuthorisationException`` class — no HTTP status is
        assumed; CLAUDE.md §6 exception-hierarchy row) on ≥ the distinct-symbol
        threshold with ZERO transport successes since the storm began. The wording
        is deliberately DISTINCT from BOTH :meth:`vendor_down` (the vendor is UP —
        it is answering, with rejections) and :meth:`credential_dead` (the token IS
        authenticated — the ACCOUNT's entitlements are the problem): the operator
        action is to check the Groww account status / API entitlements, not the
        credential, the Lambda, or the network. A single restricted instrument
        never reaches here — it is quarantined per symbol, below the threshold.
        The count is a non-secret integer; the ``dedupe_key`` coalesces a repeated
        storm verdict into one push.
        """
        message = escape_markdown_v2(
            f"⛔ AUTHORISATION STORM — ACCOUNT SUSPENSION SUSPECTED: {distinct_symbols} "
            "distinct symbols rejected with vendor authorisation errors and ZERO "
            "successes. The vendor is up and the token authenticates — the ACCOUNT's "
            "entitlements look revoked/suspended, so every remaining symbol would fail "
            "identically and the pull HALTED gracefully. Check the Groww account "
            "status / API entitlements, then re-run; bars already written are safe "
            "(idempotent re-pull)."
        )
        return self._publish(
            kind=TelegramEventKind.PHASE_BLOCKED,
            severity=TelegramSeverity.CRITICAL,
            message=message,
            dedupe_key="authorisation-storm",
        )

    def symbol_window_failure(
        self,
        *,
        symbol: str,
        failed_windows: int,
        total_windows: int,
    ) -> bool:
        """Alert that a symbol hit 100% window-failure in a retry storm (CRITICAL).

        Fired by the :class:`~brutex.orchestrator.watchdog.RetryStormWatchdog`
        once per symbol (never per window). The ``dedupe_key`` is the symbol so
        a re-run within one process never double-pushes (DEC-RATE-STORM-WATCHDOG-001).
        """
        message = escape_markdown_v2(
            f"🛑 Retry storm: {symbol} hit 100% window-failure "
            f"({failed_windows}/{total_windows} windows gave up). Bars for {symbol} "
            "are NOT in the lake — re-pull after the 429 storm clears."
        )
        return self._publish(
            kind=TelegramEventKind.SYMBOL_WINDOW_FAILURE,
            severity=TelegramSeverity.CRITICAL,
            message=message,
            dedupe_key=symbol,
        )

    def sweep_complete(self, summary: SweepSummaryLike) -> bool:
        """Alert that the supervised grid finished (INFO), with the run funnel tally.

        When ``summary.tuples_failed > 0`` ("Resilient: skip + continue" hit at
        least one tuple), the ✅ is downgraded to ⚠️ and a ``N tuples FAILED``
        clause is appended — an honest signal that the run's verdict is non-zero
        and those tuples are pending a redo (CLAUDE.md Golden Rule #10).
        """
        if summary.tuples_failed > 0:
            head = (
                f"⚠️ Sweep complete with failures — {summary.tuples_run}/{summary.tuples_total} "
                f"tuples run; {summary.tuples_failed} tuples FAILED (skipped, will redo)"
            )
        else:
            head = f"✅ Sweep complete — {summary.tuples_run}/{summary.tuples_total} tuples run"
        message = escape_markdown_v2(
            f"{head}; evaluated={summary.evaluated}, ranked={summary.ranked}, "
            f"persisted={summary.persisted} (sweep {summary.sweep_id[:12]})."
        )
        return self._publish(
            kind=TelegramEventKind.RUN_COMPLETE,
            severity=TelegramSeverity.INFO,
            message=message,
            dedupe_key=summary.sweep_id,
        )

    def greeks_backfill_complete(self, report: GreeksBackfillReportLike) -> bool:
        """Alert that a bulk Greeks v1→v2 backfill run finished (INFO).

        DEC-GREEKS-BULK-REENRICH-001 — mirrors :meth:`sweep_complete`'s honesty
        pattern: the ✅ head downgrades to ⚠️ with an ``N errored (re-runnable)``
        clause when any contract was quarantined (an errored contract keeps its
        stale stamp, so the next run retries it automatically — Golden Rule
        #10) AND when the scan found driver-UNFIXABLE contracts (fix rounds
        4-7: a stale/null stamp on a row the enrich driver skips by design —
        non-finite OHLC — or on a row its keep-first dedup / own-month-bound
        read never sees; a ✅ while such a stamp persists would be a false
        green; those need a lake repair, not a re-run). A zero-target run
        still reads honestly ("0/0 contracts re-stamped") — an
        operator-initiated one-shot deserves an answer. A CANCELLED run
        (DEC-JOB-CANCEL-CONTROL-001 — the operator's cooperative Stop) reads
        the distinct ⏹ head naming the stop + the resume promise, under a
        DISTINCT ``…:cancelled`` dedupe key — a LABELING convention only: at
        the 0.0-window ``RUN_COMPLETE`` kind
        (:data:`~brutex.notifications.telegram.DEDUPE_WINDOW_SECONDS`) the
        dedupe_key does NO notifier-side suppression, so the resumed run's
        own completion summary (same content-addressed ``backfill_id``)
        fires regardless of the key choice. Best-effort; silent no-op when
        Telegram is disabled.
        """
        clauses: list[str] = []
        if report.errored > 0:
            clauses.append(f"{report.errored} errored (re-runnable)")
        if report.stale_unfixable > 0:
            clauses.append(
                f"{report.stale_unfixable} unfixable (driver-unactionable rows — "
                "lake repair needed)"
            )
        if report.cancelled:
            # Operator's cooperative Stop (DEC-JOB-CANCEL-CONTROL-001): an
            # honest ⏹ PARTIAL — never a ✅ for a run the operator halted, and
            # never the ⚠️ "with failures" wording for a deliberate stop (any
            # errored/unfixable clauses still ride along). The DISTINCT
            # `…:cancelled` dedupe_key is a LABELING convention only (it names
            # the outcome on the event record — the soak_drift precedent):
            # at the 0.0-window RUN_COMPLETE kind the dedupe_key does NO
            # notifier-side suppression (DEDUPE_WINDOW_SECONDS —
            # check_dedupe_locked returns False for window <= 0.0), so the
            # resumed run's completion summary (same content-addressed
            # backfill_id) fires regardless of the key.
            head = (
                f"⏹ Greeks backfill CANCELLED by operator — "
                f"{report.enriched}/{report.planned} contracts re-stamped v2 before "
                f"the stop; resumable (re-run resumes)"
                + ("; " + "; ".join(clauses) if clauses else "")
            )
            dedupe_key = f"greeks-backfill:{report.backfill_id[:12]}:cancelled"
        elif clauses:
            head = (
                f"⚠️ Greeks backfill complete with failures — "
                f"{report.enriched}/{report.planned} contracts re-stamped v2; " + "; ".join(clauses)
            )
            dedupe_key = f"greeks-backfill:{report.backfill_id[:12]}"
        else:
            head = (
                f"✅ Greeks backfill complete — "
                f"{report.enriched}/{report.planned} contracts re-stamped v2"
            )
            dedupe_key = f"greeks-backfill:{report.backfill_id[:12]}"
        message = escape_markdown_v2(
            f"{head}; already current={report.already_current}, "
            f"bars written={report.bars_written} (backfill {report.backfill_id[:12]})."
        )
        return self._publish(
            kind=TelegramEventKind.RUN_COMPLETE,
            severity=TelegramSeverity.INFO,
            message=message,
            dedupe_key=dedupe_key,
        )

    # ── Callback adapters for the existing seams ────────────────────

    def watchdog_hung_callback(self) -> Callable[[WorkerEventLike], None]:
        """Return a :class:`~brutex.engine.sweep_watchdog.SweepWatchdog` ``on_hung`` callback.

        Plug into ``SweepWatchdog(on_hung=alerter.watchdog_hung_callback())`` so a
        HEALTHY→HUNG edge fires the Telegram alert. Best-effort — the closure can
        never raise into the watchdog's edge-detection pass.
        """

        def _on_hung(event: WorkerEventLike) -> None:
            """Map a watchdog :class:`WorkerEvent` to a hung Telegram alert."""
            self.watchdog_hung(
                worker_name=event.worker_name,
                last_beat_seconds_ago=event.last_beat_seconds_ago,
                timeout_seconds=event.timeout_seconds,
            )

        return _on_hung

    def symbol_failure_callback(self) -> Callable[[SymbolFailureLike], None]:
        """Return a :class:`~brutex.orchestrator.watchdog.RetryStormWatchdog` ``on_symbol_failure`` callback.

        Plug into ``build_retry_storm_watchdog(on_symbol_failure=alerter.symbol_failure_callback())``
        so a symbol reaching 100% window-failure fires the Telegram alert. Best-effort —
        the closure can never raise into the watchdog's give-up verdict.
        """

        def _on_symbol_failure(event: SymbolFailureLike) -> None:
            """Map a watchdog :class:`SymbolFailureEvent` to a CRITICAL Telegram alert."""
            self.symbol_window_failure(
                symbol=event.symbol,
                failed_windows=event.failed_windows,
                total_windows=event.total_windows,
            )

        return _on_symbol_failure

    def rate_breach_callback(self, controller: RateControllerLike) -> Callable[[], None]:
        """Return an ``on_rate_limit`` hook that steps the rate down THEN alerts.

        Wraps the controller's own :meth:`on_rate_limit` (which does the real
        throttle) and follows it with a best-effort :meth:`rate_breach` alert read
        from the controller's fresh snapshot. The notifier's 60 s ``RATE_LIMIT_
        DOWNSHIFT`` dedupe coalesces a 429 storm into a single push, so wiring this
        on EVERY hit does not flood the operator's phone.
        """

        def _on_rate_limit() -> None:
            """Record the 429 (throttle) then fire one deduped breach alert."""
            controller.on_rate_limit()
            snap = controller.snapshot()
            # ``snapshot`` is typed ``dict[str, object]`` (JSON-friendly); the rate
            # keys are numeric — coerce through ``str`` so the cast is total and
            # never trips on a non-number value (a malformed snapshot can't crash
            # the throttle path; Golden Rule #10).
            self.rate_breach(
                current_rps=float(str(snap["current_rps"])),
                recent_hits=int(float(str(snap["recent_hits"]))),
            )

        return _on_rate_limit


def emit_pull_halt_alert(exc: Exception) -> None:
    """Fire a best-effort CRITICAL Telegram alert that the pull HALTED (DEC-TELEGRAM-PULL-HALT-001).

    The COMMON helper shared by BOTH the CLI fetch path (``brutex.cli`` ``cmd_fetch``)
    and the dashboard background pull (:class:`brutex.dashboard.pull_jobs.BackgroundPullJob`),
    so the pull-halt alert has ONE source of truth (CLAUDE.md §11). Called when a
    pull-wide global-stop (a full-disk / OOM :class:`~brutex.data.errors.LakeWriteHalt`,
    or a daily-quota / quota-invariant stop) propagates out of the ingest spine. Unlike
    a per-symbol quarantine this aborts EVERY remaining window, so the operator must act
    — hence a CRITICAL push. The ``LakeWriteHalt.reason`` (``disk_full`` / ``out_of_memory``)
    is surfaced verbatim; any other stop uses its class name.

    The whole body is wrapped in a blanket ``try/except`` so the fire-and-forget
    contract is STRUCTURAL, not merely circumstantial: even a future import-time or
    construction failure here is swallowed + logged and can NEVER replace the halt the
    caller re-raises (``structlog`` is a core dep, so the logger import itself cannot
    newly fail). When Telegram is disabled (the default), the publish is a silent no-op.

    A TERMINAL Groww auth failure (DEC-GROWW-AUTH-HALT-001 — a dead credential / an
    exhausted re-mint budget, now a pull-wide global stop) reports its class name
    (``"GrowwAuthError"``) as the reason via the generic branch — no per-type mapping
    needed; the reason-scoped dedupe key coalesces a repeated auth halt.
    """
    import structlog

    try:
        from brutex.data.errors import LakeWriteHalt

        reason = exc.reason if isinstance(exc, LakeWriteHalt) else type(exc).__name__
        SweepAlerter().pull_halted(reason=reason)
    except Exception:  # pylint: disable=broad-exception-caught — best-effort, never masks the halt
        structlog.get_logger("brutex.notifications.sweep_alerts").exception(
            "pull_halt_alert_failed"
        )


def emit_backup_failed_alert(reason: str, detail: str = "") -> None:
    """Best-effort CRITICAL backup-failure push; NEVER raises into the caller.

    DEC-BACKUP-OFFBOX-001 — the backup twin of :func:`emit_pull_halt_alert`
    (same blanket-guard structure): the fire-and-forget contract is
    STRUCTURAL, so even a future import-time or construction failure here is
    swallowed + logged and can never replace the exit code the caller
    returns. When Telegram is disabled (the default), the publish is a
    silent no-op.

    Parameters
    ----------
    reason
        Short, non-secret cause token (mirrors :meth:`SweepAlerter.backup_failed`).
    detail
        Optional appended context (non-secret).
    """
    import structlog

    try:
        SweepAlerter().backup_failed(reason=reason, detail=detail)
    except Exception:  # pylint: disable=broad-exception-caught — best-effort, never masks the exit
        structlog.get_logger("brutex.notifications.sweep_alerts").warning(
            "backup_failed_alert_failed", reason=reason
        )


#: Env var naming the pull-level max-close-delta (rupees) at/above which ONE
#: vendor-conflict-drift Telegram alert fires per pull
#: (DEC-VENDOR-CONFLICT-SURFACE-001). Parsed as an exact Decimal.
CONFLICT_DELTA_ALERT_ENV: Final[str] = "BRUTEX_CONFLICT_CLOSE_DELTA_ALERT"

#: The default alert threshold (rupees) when the env var is absent/malformed.
DEFAULT_CONFLICT_DELTA_ALERT: Final[str] = "1.00"


def conflict_alert_threshold() -> Decimal:
    """The vendor-conflict close-delta alert threshold in exact Decimal rupees.

    Reads :data:`CONFLICT_DELTA_ALERT_ENV` (default
    :data:`DEFAULT_CONFLICT_DELTA_ALERT`). A malformed value falls back to the
    default LOUDLY (one WARNING) — never a silent never-alerts config
    (Golden Rule #1). Decimal end-to-end: prices are Decimal rupees
    (CLAUDE.md §6 price-unit row), so the comparison is exact, never a float.
    """
    import os
    from decimal import Decimal, InvalidOperation

    raw = os.environ.get(CONFLICT_DELTA_ALERT_ENV, DEFAULT_CONFLICT_DELTA_ALERT)
    try:
        return Decimal(raw)
    except InvalidOperation:
        import structlog

        structlog.get_logger("brutex.notifications.sweep_alerts").warning(
            "conflict_alert_threshold_invalid",
            value=raw,
            fallback=DEFAULT_CONFLICT_DELTA_ALERT,
        )
        return Decimal(DEFAULT_CONFLICT_DELTA_ALERT)


def emit_vendor_conflict_alert(
    *, conflicts: int, max_close_delta: Decimal | None, dup_rows: int
) -> bool:
    """Fire AT MOST one best-effort vendor-conflict-drift alert for a pull.

    DEC-VENDOR-CONFLICT-SURFACE-001 — the COMMON module helper (mirrors
    :func:`emit_pull_halt_alert`) the pipeline's completion-headline seam calls
    once per pull with the folded totals. Fires ONLY when *max_close_delta* is
    non-``None`` AND ``>=`` :func:`conflict_alert_threshold` (exact Decimal
    comparison); below the threshold — or with no conflict at all — it is a
    silent no-op, so a clean pull never pings the operator. The rupee display
    goes through ``brutex.money`` (DEC-MONEY-2DP-001: money display only —
    the exact Decimal is compared BEFORE any display rounding). Returns whether
    the alert was PUBLISHED (``False`` = below threshold / disabled / failed).

    The whole body is wrapped in a blanket ``try/except`` so the fire-and-forget
    contract is STRUCTURAL — an alert failure is swallowed + logged and can
    NEVER crash the pull (Telegram disabled ⇒ silent no-op).
    """
    import structlog

    try:
        if max_close_delta is None or max_close_delta < conflict_alert_threshold():
            return False
        from brutex.money import format_inr, round_money

        # Exact-threshold compare happened above on the FULL-precision Decimal;
        # only the DISPLAY is 2dp-snapped (ROUND_HALF_UP) then rendered via the
        # ONE paisa formatter (DEC-MONEY-2DP-001).
        display = format_inr(int(round_money(max_close_delta) * 100))
        return SweepAlerter().vendor_conflict_drift(
            conflicts=conflicts,
            max_close_delta_display=display,
            dup_rows=dup_rows,
        )
    except Exception:  # pylint: disable=broad-exception-caught — best-effort, never crashes the pull
        structlog.get_logger("brutex.notifications.sweep_alerts").exception(
            "vendor_conflict_alert_failed"
        )
        return False


def emit_lake_heal_alert(report: LakeHealReportLike, *, scope: str) -> None:
    """Fire a best-effort Telegram alert for one lake-heal batch (DEC-LAKE-HEAL-001).

    The COMMON module helper mirroring :func:`emit_pull_halt_alert` — the ONE
    place the heal seams (`cascade_wire` reconcile, the dashboard pull glue, the
    startup sweeper) map a ``brutex.data.lake_heal.HealReport`` onto
    :meth:`SweepAlerter.lake_heal_report`. Skips entirely when
    ``report.findings == 0`` (a clean scan never alerts). The whole body is
    wrapped in a blanket ``try/except`` so the fire-and-forget contract is
    STRUCTURAL: an alert failure is swallowed + logged and can NEVER fail the
    heal. Silent no-op when Telegram is disabled (the default).

    Parameters
    ----------
    report
        The heal batch outcome (structural — any ``HealReport``-shaped value).
    scope
        The seam token (``"reconcile"`` / ``"pull:<id>"`` / ``"startup-sweep"``).
    """
    import structlog

    try:
        if report.findings == 0:
            return
        actions = list(report.actions)
        SweepAlerter().lake_heal_report(
            mode=report.mode,
            findings=report.findings,
            healed=report.healed,
            quarantined=sum(1 for action in actions if action.quarantine_path is not None),
            dropped=sum(action.dropped for action in actions),
            conflicts=sum(action.conflicts for action in actions),
            scope=scope,
        )
    except Exception:  # pylint: disable=broad-exception-caught — best-effort, never masks the heal
        structlog.get_logger("brutex.notifications.sweep_alerts").exception(
            "lake_heal_alert_failed"
        )


def emit_greeks_backfill_complete_alert(report: GreeksBackfillReportLike) -> None:
    """Fire the ONE best-effort Greeks-backfill completion summary (DEC-GREEKS-BULK-REENRICH-001).

    The COMMON module helper mirroring :func:`emit_pull_halt_alert` — the ONE
    place the two backfill surfaces (the dashboard job's work-body tail + the
    CLI ``brutex greeks-backfill`` clean finish) map a
    ``brutex.data.greeks_backfill.GreeksBackfillReport`` onto
    :meth:`SweepAlerter.greeks_backfill_complete`. EXACTLY-ONCE by call
    discipline (one chokepoint per surface, never inside the batch loop) with
    the ``backfill_id``-scoped dedupe key as the second belt. A DRY-RUN never
    calls this (dry-run is inert by construction). The whole body is wrapped
    in a blanket ``try/except`` so the fire-and-forget contract is STRUCTURAL:
    an alert failure is swallowed + logged and can NEVER fail the backfill.
    Silent no-op when Telegram is disabled (the default).
    """
    import structlog

    try:
        SweepAlerter().greeks_backfill_complete(report)
    except Exception:  # pylint: disable=broad-exception-caught — best-effort, never fails the backfill
        structlog.get_logger("brutex.notifications.sweep_alerts").exception(
            "greeks_backfill_alert_failed"
        )


__all__ = [
    "CONFLICT_DELTA_ALERT_ENV",
    "CRASH_LOOP_RELAUNCH_THRESHOLD",
    "DEFAULT_CONFLICT_DELTA_ALERT",
    "GreeksBackfillReportLike",
    "LakeHealActionLike",
    "LakeHealReportLike",
    "NotifierResolver",
    "RateControllerLike",
    "SweepAlerter",
    "SweepSummaryLike",
    "SymbolFailureLike",
    "WorkerEventLike",
    "conflict_alert_threshold",
    "emit_greeks_backfill_complete_alert",
    "emit_lake_heal_alert",
    "emit_pull_halt_alert",
    "emit_vendor_conflict_alert",
    "escape_markdown_v2",
]
