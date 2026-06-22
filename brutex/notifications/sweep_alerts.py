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
from collections.abc import Callable
from typing import Final, Protocol

from brutex.notifications.telegram import (
    TelegramEvent,
    TelegramEventKind,
    TelegramSeverity,
)

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
            ``"out_of_memory"``, ``"DailyQuotaExceeded"``).
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
        """Alert that the supervised grid finished (INFO), with the run funnel tally."""
        message = escape_markdown_v2(
            f"✅ Sweep complete — {summary.tuples_run}/{summary.tuples_total} tuples run; "
            f"evaluated={summary.evaluated}, ranked={summary.ranked}, "
            f"persisted={summary.persisted} (sweep {summary.sweep_id[:12]})."
        )
        return self._publish(
            kind=TelegramEventKind.RUN_COMPLETE,
            severity=TelegramSeverity.INFO,
            message=message,
            dedupe_key=summary.sweep_id,
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


__all__ = [
    "NotifierResolver",
    "RateControllerLike",
    "SweepAlerter",
    "SweepSummaryLike",
    "SymbolFailureLike",
    "WorkerEventLike",
    "escape_markdown_v2",
]
