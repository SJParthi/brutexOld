"""Telegram ⇄ pipeline wiring — closes the 2026-05-25 honest-audit
gap row 16 (Telegram coded but never `.publish()`-ed).

Per the 5-agent audit deliverable ``docs/STANDARDS_100PCT_HONEST_AUDIT.md``
the ``TelegramNotifier`` in ``brutex/notifications/telegram.py`` is a
546-LOC well-tested module that has **zero callers** in the
orchestrator hot path. CLAUDE.md §11 AUTOMATED rule explicitly
promises "Release Please + auto-merge + watchdog + Telegram +
auto-resume", so the Telegram absence is a real gap, not a
documentation-only one.

This module is the single seam that bridges:

* The :mod:`brutex.orchestrator.events` ``EventSubscriber`` protocol
  (``Callable[[PipelineEvent], None]``) — the live pipeline event shapes:
  PHASE_START / PHASE_PROGRESS / PHASE_COMPLETE / PHASE_HALTED /
  ITEM_COMPLETE / RUN_START / RUN_COMPLETE.

* The :class:`brutex.notifications.telegram.TelegramNotifier`
  ``publish(TelegramEvent)`` API.

The mapping is intentionally narrow: only events that an OPERATOR
needs to see on their phone (run start, phase done, phase blocked,
run complete) generate a Telegram push. Per-item progress events stay
in the audit log + dashboard.

## Design — single seam, lazy lifecycle, graceful no-op

* **Singleton lifecycle.** :func:`get_or_start_notifier` reads env once
  (idempotent) and either returns an already-started notifier or
  starts a new one. If ``BRUTEX_TELEGRAM_ENABLED`` is unset / not
  ``"true"``, returns ``None``.
* **Subscriber factory.** :func:`make_pipeline_event_subscriber`
  returns an ``EventSubscriber`` callable that performs the
  PipelineEvent → TelegramEvent mapping. Caller plugs it into
  any pipeline event producer's subscriber list.
* **Graceful no-op when disabled.** If the notifier is ``None``
  (Telegram off), the subscriber is a literal no-op — zero overhead,
  zero errors.
* **PII-safe formatting.** Messages contain run_id, phase, counts,
  durations — never bot token, chat_id, or raw stack traces.

## Why this is the right seam (Golden Rule #11 COMMON discipline)

Producers emit events via a Sequence[EventSubscriber]. The
dashboard subscribes (DEC-DASHBOARD-LIVE-1). Adding another subscriber
requires NO change to the event shapes in
:mod:`brutex.orchestrator.events`. This is the same DI-friendly pattern
the spec mandates — one source of truth (the event stream), many sinks.

## Decision: DEC-TELEGRAM-WIRING-001 (MIGRATION entry #238).
"""

from __future__ import annotations

import logging
import threading
from typing import Final, TypedDict

from brutex.notifications.sweep_alerts import escape_markdown_v2
from brutex.notifications.telegram import (
    TelegramConfig,
    TelegramConfigError,
    TelegramEvent,
    TelegramEventKind,
    TelegramNotifier,
    TelegramSeverity,
)
from brutex.orchestrator.events import (
    EventSubscriber,
    PipelineEvent,
    PipelineEventKind,
)

logger = logging.getLogger(__name__)


class _State(TypedDict):
    """Process-wide notifier-singleton state container.

    Held in a single module-level dict so we don't need the ``global``
    statement (ruff PLW0603 forbids it). Encoded as a TypedDict so
    mypy strict mode can narrow the value types without runtime
    isinstance checks.
    """

    notifier: TelegramNotifier | None
    attempted: bool


# ────────────────────────────────────────────────────────────────────────
# PipelineEventKind → TelegramEventKind mapping
# ────────────────────────────────────────────────────────────────────────

#: Direct mapping from pipeline events that should trigger a Telegram push
#: to the corresponding TelegramEventKind. Pipeline events NOT in this map
#: are silently dropped from the Telegram channel (still reach the audit
#: log + dashboard via other subscribers).
#:
#: Rationale per REQUIREMENTS.md §15.6 table:
#:
#:   * ``RUN_START`` / ``RUN_COMPLETE`` are top-of-funnel — operator wants
#:     to know the run started + when it finished.
#:   * ``PHASE_START`` / ``PHASE_COMPLETE`` are coarse-grained progress;
#:     PHASE_PROGRESS would flood with per-item updates so we drop it.
#:   * ``PHASE_HALTED`` is the critical safety event — must fire.
#:   * ``ITEM_COMPLETE`` is too noisy for the chat; stays in audit.
_PIPELINE_TO_TELEGRAM_KIND: Final[dict[str, str]] = {
    PipelineEventKind.RUN_START: TelegramEventKind.RUN_START,
    PipelineEventKind.PHASE_START: TelegramEventKind.PHASE_START,
    PipelineEventKind.PHASE_COMPLETE: TelegramEventKind.PHASE_COMPLETE,
    PipelineEventKind.PHASE_HALTED: TelegramEventKind.PHASE_BLOCKED,
    PipelineEventKind.RUN_COMPLETE: TelegramEventKind.RUN_COMPLETE,
}

#: Severity floor per kind — events default to INFO except for
#: blocked / halt which jumps to CRITICAL so operator's Telegram client
#: pops a louder notification.
_KIND_TO_SEVERITY: Final[dict[str, str]] = {
    PipelineEventKind.RUN_START: TelegramSeverity.INFO,
    PipelineEventKind.PHASE_START: TelegramSeverity.INFO,
    PipelineEventKind.PHASE_COMPLETE: TelegramSeverity.INFO,
    PipelineEventKind.PHASE_HALTED: TelegramSeverity.CRITICAL,
    PipelineEventKind.RUN_COMPLETE: TelegramSeverity.INFO,
}


# ────────────────────────────────────────────────────────────────────────
# Singleton lifecycle
# ────────────────────────────────────────────────────────────────────────

# State held in a single module-level container so we don't need the
# `global` statement (ruff PLW0603 forbids it). Keys: "notifier"
# (TelegramNotifier|None), "attempted" (bool — has from_env / start
# already been tried this process?).
_lock: Final[threading.Lock] = threading.Lock()
_state: _State = {"notifier": None, "attempted": False}


def get_or_start_notifier(
    *,
    config: TelegramConfig | None = None,
) -> TelegramNotifier | None:
    """Return the process-wide :class:`TelegramNotifier`, starting it lazily.

    Thread-safe — the first caller acquires the lock + starts the
    notifier; subsequent callers receive the cached instance.

    Returns ``None`` (and caches that decision) when Telegram is
    disabled via env. ``TelegramConfigError`` is logged + swallowed so
    a misconfigured Telegram channel never blocks the ingest loop —
    Golden Rule #10 (graceful degradation).

    Parameters
    ----------
    config
        Optional explicit config (tests inject this). When ``None``,
        ``TelegramConfig.from_env()`` is used.
    """
    with _lock:
        if _state["attempted"]:
            return _state["notifier"]

        _state["attempted"] = True
        try:
            cfg = config if config is not None else TelegramConfig.from_env()
        except TelegramConfigError:
            logger.exception(
                "get_or_start_notifier: TelegramConfig.from_env failed; "
                "Telegram channel disabled for this process"
            )
            return None

        if not cfg.enabled:
            logger.info("get_or_start_notifier: Telegram disabled via env")
            return None

        try:
            _state["notifier"] = TelegramNotifier.start(config=cfg)
        except OSError:
            logger.exception(
                "get_or_start_notifier: TelegramNotifier.start failed; "
                "Telegram channel disabled for this process"
            )
            _state["notifier"] = None

        return _state["notifier"]


def reset_notifier_for_tests() -> None:
    """Reset the singleton — TEST-ONLY hook.

    Production code never calls this. Tests use it to isolate
    notifier lifecycle between cases (start fresh, verify behaviour,
    stop). Calling :meth:`TelegramNotifier.stop` on the current
    instance if any.
    """
    with _lock:
        current = _state["notifier"]
        if current is not None:
            current.stop(timeout=1.0)
        _state["notifier"] = None
        _state["attempted"] = False


# ────────────────────────────────────────────────────────────────────────
# Message formatting
# ────────────────────────────────────────────────────────────────────────


def _format_message(event: PipelineEvent) -> str:
    """Render a Telegram-friendly message for *event*.

    The result is PLAIN text — NOT yet MarkdownV2-escaped. The notifier
    does NOT escape: :class:`TelegramEvent` requires a pre-escaped
    MarkdownV2 body and the transport sends it verbatim, so THIS source
    module escapes via :func:`escape_markdown_v2` at the publish site in
    :func:`make_pipeline_event_subscriber` (escape-once — never here AND
    there, so a body is never double-escaped). Keeps to one line per
    event to avoid Telegram's reply-block visual clutter.
    """
    phase_str = f" phase={event.phase.value}" if event.phase is not None else ""
    run_str = f" run={event.run_id}" if event.run_id else ""

    if event.kind == PipelineEventKind.RUN_START:
        return f"BruteX run started.{run_str}"
    if event.kind == PipelineEventKind.RUN_COMPLETE:
        elapsed = (
            f" elapsed={event.elapsed_seconds:.1f}s" if event.elapsed_seconds is not None else ""
        )
        return f"BruteX run complete.{run_str}{elapsed}"
    if event.kind == PipelineEventKind.PHASE_START:
        total = f" total={event.total}" if event.total is not None else ""
        return f"Phase start.{run_str}{phase_str}{total}"
    if event.kind == PipelineEventKind.PHASE_COMPLETE:
        success = f" success={event.success}" if event.success is not None else ""
        quar = f" quarantined={event.quarantined}" if event.quarantined is not None else ""
        return f"Phase complete.{run_str}{phase_str}{success}{quar}"
    if event.kind == PipelineEventKind.PHASE_HALTED:
        msg = f" reason={event.message}" if event.message else ""
        return f"Phase HALTED.{run_str}{phase_str}{msg}"
    # Other event kinds aren't pushed (we only enter this function for
    # the 5 mapped kinds), but keep the catch-all so a future kind addition
    # produces a sane fallback string instead of an exception.
    return f"Pipeline event {event.kind}.{run_str}{phase_str}"


def _dedupe_key(event: PipelineEvent) -> str:
    """Compute the dedupe key for *event* — pulls the TelegramBucket
    cache key apart so two PHASE_START events for the same (run, phase)
    don't double-fire.
    """
    parts: list[str] = []
    if event.run_id:
        parts.append(event.run_id)
    if event.phase is not None:
        parts.append(event.phase.value)
    return ":".join(parts)


# ────────────────────────────────────────────────────────────────────────
# Subscriber factory
# ────────────────────────────────────────────────────────────────────────


def make_pipeline_event_subscriber(
    *,
    notifier: TelegramNotifier | None = None,
) -> EventSubscriber:
    """Return an :data:`EventSubscriber` that publishes mapped events
    to Telegram.

    Caller pattern::

        from brutex.notifications.wiring import (
            make_pipeline_event_subscriber,
        )
        subscribers = [
            dashboard_subscriber,
            make_pipeline_event_subscriber(),
        ]

    Parameters
    ----------
    notifier
        Optional explicit notifier. When ``None``, the process
        singleton is used (started lazily). Tests pass an explicit
        fake to assert on `.publish` calls.

    Returns
    -------
    EventSubscriber
        A callable taking a :class:`PipelineEvent` that either
        publishes a :class:`TelegramEvent` or no-ops. Never raises —
        per Golden Rule #10 a broken Telegram must NOT crash the
        ingest loop.
    """
    explicit_notifier = notifier  # capture for closure; may be None

    def _subscriber(event: PipelineEvent) -> None:
        active = explicit_notifier if explicit_notifier is not None else get_or_start_notifier()
        if active is None:
            return  # Telegram disabled — graceful no-op
        target_kind = _PIPELINE_TO_TELEGRAM_KIND.get(event.kind)
        if target_kind is None:
            return  # Pipeline kind not mapped — silently drop
        try:
            # TelegramEvent.message must be pre-escaped MarkdownV2 (the
            # notifier transport sends it VERBATIM with parse_mode=
            # MarkdownV2) — escape the plain-text body exactly once here.
            tg_event = TelegramEvent(
                kind=target_kind,
                severity=_KIND_TO_SEVERITY.get(event.kind, TelegramSeverity.INFO),
                message=escape_markdown_v2(_format_message(event)),
                dedupe_key=_dedupe_key(event),
            )
            active.publish(tg_event)
        except (OSError, ValueError, RuntimeError):
            logger.exception(
                "make_pipeline_event_subscriber: publish failed for "
                "kind=%s run_id=%s phase=%s — Telegram channel will "
                "drop this event, ingest continues",
                event.kind,
                event.run_id,
                event.phase,
            )

    return _subscriber


# Re-exports for backwards-compat with tests that import these names
# directly from this module (in case future refactors move them).
__all__ = (
    "get_or_start_notifier",
    "make_pipeline_event_subscriber",
    "reset_notifier_for_tests",
)
