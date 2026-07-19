"""Production-ready composer that wires Telegram + SLA + Watchdog +
replay-oracle into a single :class:`MonitorLoop` per env / config.

Closes the "🟡 PARTIAL: CLI `brutex monitor` constructs an EMPTY
MonitorLoop" envelope from DEC-MONITOR-LOOP-001 (MIGRATION entry
#242). After this lands, ``brutex monitor`` is operator-runnable
with REAL detectors + REAL Telegram callback firing on SLA breach
+ REAL watchdog heartbeat events.

## Design — env-driven, zero new config-format dependency

A :class:`MonitorConfig` dataclass holds every tunable; it reads
defaults from env vars (no YAML / no TOML — the bigger config story
arrives with the Phase 3 operator-config PR). Tests pass an explicit
:class:`MonitorConfig` instance and skip env entirely.

The :func:`compose_standard_monitor` factory:

1. Resolves the config (explicit arg or env).
2. Builds the SLA detector with caller-supplied :class:`SLARule` list.
   The default list is **EMPTY** — operator-driven content lands when
   the metrics-registry schema settles (tracked as roadmap below).
3. Builds the watchdog with the configured timeout. Worker
   registration is delayed until first heartbeat (auto-registered).
4. Builds the replay-check closure if ``audit_path`` + ``live_subjects_file``
   are both set. Else the replay tick is skipped.
5. Wires the SLA + Watchdog callbacks to fire a Telegram event
   when ``telegram_enabled`` is True. Without Telegram, callbacks
   default to structlog-style logger.info — useful for dev / smoke.
6. Returns the assembled :class:`MonitorLoop` ready for
   :meth:`MonitorLoop.start`.

## Why no YAML dep

YAML is a heavy dep (PyYAML pulls in libyaml C lib). For this PR we
stay stdlib-only — env vars + caller-supplied SLARule list. The next
ratchet (post-adapter-live) can add a YAML or TOML loader that
constructs :class:`MonitorConfig` from a file.

## Decision: DEC-STANDARD-MONITOR-001 (MIGRATION entry #243).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from brutex.data.guarded_read import read_regular_text
from brutex.observability.monitor import MonitorLoop, ReplayCheck
from brutex.observability.sla import (
    SLADetector,
    SLAEvent,
    SLAEventCallback,
    SLARule,
)
from brutex.observability.watchdog import Watchdog, WorkerEvent, WorkerEventCallback

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Env-var names + defaults
# ────────────────────────────────────────────────────────────────────────


ENV_POLL_INTERVAL: Final[str] = "BRUTEX_MONITOR_POLL_INTERVAL"
ENV_WATCHDOG_TIMEOUT: Final[str] = "BRUTEX_WATCHDOG_TIMEOUT_SECONDS"
ENV_TELEGRAM_ENABLED: Final[str] = "BRUTEX_TELEGRAM_ENABLED"
ENV_AUDIT_PATH: Final[str] = "BRUTEX_AUDIT_PATH"
ENV_LIVE_SUBJECTS_FILE: Final[str] = "BRUTEX_LIVE_SUBJECTS_FILE"
ENV_REPLAY_ACTION_FILTER: Final[str] = "BRUTEX_REPLAY_ACTION_FILTER"
ENV_REPLAY_EVERY_N_TICKS: Final[str] = "BRUTEX_REPLAY_EVERY_N_TICKS"

DEFAULT_POLL_INTERVAL: Final[float] = 30.0
DEFAULT_WATCHDOG_TIMEOUT: Final[float] = 300.0  # 5 min
DEFAULT_REPLAY_ACTION_FILTER: Final[str] = "add"
DEFAULT_REPLAY_EVERY_N_TICKS: Final[int] = 1


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    """Production composer configuration.

    Every field has a sane default; explicit arg overrides env;
    env overrides default. Tests pass explicit instances and skip
    env reads entirely.

    Attributes
    ----------
    poll_interval_seconds
        :class:`MonitorLoop` tick period. Default 30s.
    watchdog_timeout_seconds
        :class:`Watchdog` HUNG threshold. Default 300s (5 min).
    telegram_enabled
        When True, SLA + Watchdog callbacks attempt Telegram publish
        via :func:`brutex.notifications.wiring.get_or_start_notifier`.
        When False, callbacks log only.
    sla_rules
        Operator-supplied :class:`SLARule` list. Default empty —
        the metrics-registry schema isn't standardised yet, so we
        ship the seam without prescriptive rules.
    audit_path
        Audit log file to feed the L3 replay oracle. ``None`` skips
        replay check.
    live_subjects_file
        Newline-separated live-dedup snapshot. ``None`` skips replay.
    replay_action_filter
        Audit action to project for replay. Default ``"add"``.
    replay_every_n_ticks
        Cadence multiplier for the replay check. Default 1.
    """

    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL
    watchdog_timeout_seconds: float = DEFAULT_WATCHDOG_TIMEOUT
    telegram_enabled: bool = False
    sla_rules: Sequence[SLARule] = field(default_factory=tuple)
    audit_path: Path | None = None
    live_subjects_file: Path | None = None
    replay_action_filter: str = DEFAULT_REPLAY_ACTION_FILTER
    replay_every_n_ticks: int = DEFAULT_REPLAY_EVERY_N_TICKS

    @classmethod
    def from_env(
        cls,
        *,
        sla_rules: Sequence[SLARule] = (),
    ) -> MonitorConfig:
        """Build config from environment variables.

        Each numeric env var is parsed strictly; bad values fall back
        to the default with a logged warning (Golden Rule #10 —
        misconfigured env never crashes the loop). ``sla_rules`` is
        always caller-supplied because there's no env-var schema for
        complex rule lists.
        """
        return cls(
            poll_interval_seconds=_parse_float_env(ENV_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
            watchdog_timeout_seconds=_parse_float_env(
                ENV_WATCHDOG_TIMEOUT, DEFAULT_WATCHDOG_TIMEOUT
            ),
            telegram_enabled=_parse_bool_env(ENV_TELEGRAM_ENABLED, default=False),
            sla_rules=tuple(sla_rules),
            audit_path=_parse_path_env(ENV_AUDIT_PATH),
            live_subjects_file=_parse_path_env(ENV_LIVE_SUBJECTS_FILE),
            replay_action_filter=os.environ.get(
                ENV_REPLAY_ACTION_FILTER, DEFAULT_REPLAY_ACTION_FILTER
            ),
            replay_every_n_ticks=_parse_int_env(
                ENV_REPLAY_EVERY_N_TICKS, DEFAULT_REPLAY_EVERY_N_TICKS
            ),
        )


# ────────────────────────────────────────────────────────────────────────
# Env-parsing helpers (private)
# ────────────────────────────────────────────────────────────────────────


def _parse_float_env(name: str, default: float) -> float:
    """Read env var *name* as float; fall back to *default* on missing/invalid."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "MonitorConfig.from_env: env %s=%r is not a float; using default %s",
            name,
            raw,
            default,
        )
        return default


def _parse_int_env(name: str, default: int) -> int:
    """Read env var *name* as int; fall back to *default* on missing/invalid."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "MonitorConfig.from_env: env %s=%r is not an int; using default %s",
            name,
            raw,
            default,
        )
        return default


def _parse_bool_env(name: str, *, default: bool) -> bool:
    """Strict env-bool decoder: env value WINS when set.

    * Unset → ``default``.
    * Set to ``"true"`` (case-insensitive, trimmed) → ``True``.
    * Set to ANY other value (including ``"1"``, ``"yes"``, ``"false"``)
      → ``False`` explicitly. The default does NOT apply when env is set.

    Matches DEC-M4PRO-ABSOLUTE-001's strict decoder pattern: no
    permissive coercion, only the literal ``"true"`` activates.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() == "true"


def _parse_path_env(name: str) -> Path | None:
    """Read env var *name* as a ``Path``; ``None`` if unset, empty, or whitespace."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return Path(raw.strip())


# ────────────────────────────────────────────────────────────────────────
# Composer
# ────────────────────────────────────────────────────────────────────────


def compose_standard_monitor(
    config: MonitorConfig | None = None,
) -> MonitorLoop:
    """Build a production-ready :class:`MonitorLoop` from *config*.

    When *config* is ``None``, :meth:`MonitorConfig.from_env` is used
    with no SLA rules (operator pre-instantiates if rules are needed).

    Returns
    -------
    MonitorLoop
        Assembled but NOT started. Caller invokes :meth:`start` or
        uses as a context manager.
    """
    cfg = config if config is not None else MonitorConfig.from_env()

    on_breach = _make_sla_callback(telegram_enabled=cfg.telegram_enabled)
    on_recovery = _make_sla_callback(telegram_enabled=cfg.telegram_enabled)
    sla = SLADetector(
        rules=tuple(cfg.sla_rules),
        on_breach=on_breach,
        on_recovery=on_recovery,
    )

    on_hung = _make_watchdog_callback(telegram_enabled=cfg.telegram_enabled)
    on_wd_recovery = _make_watchdog_callback(telegram_enabled=cfg.telegram_enabled)
    watchdog = Watchdog(
        timeout_seconds=cfg.watchdog_timeout_seconds,
        on_hung=on_hung,
        on_recovery=on_wd_recovery,
    )

    replay_check = _make_replay_check(
        audit_path=cfg.audit_path,
        live_subjects_file=cfg.live_subjects_file,
        action_filter=cfg.replay_action_filter,
    )

    return MonitorLoop(
        sla_detector=sla,
        watchdog=watchdog,
        replay_check=replay_check,
        poll_interval_seconds=cfg.poll_interval_seconds,
        replay_check_every_n_ticks=cfg.replay_every_n_ticks,
    )


# ────────────────────────────────────────────────────────────────────────
# Callback factories (private)
# ────────────────────────────────────────────────────────────────────────


def _make_sla_callback(*, telegram_enabled: bool) -> SLAEventCallback:
    """Return an SLA on-breach / on-recovery callback.

    When *telegram_enabled* is True, the closure attempts to fetch the
    singleton :class:`TelegramNotifier` and publish a TelegramEvent.
    Otherwise the closure logs at INFO level. Both paths swallow
    callback exceptions per Golden Rule #10.

    The body built here is RAW plain text (rule names / decimals /
    parentheses included); :func:`_publish_telegram` MarkdownV2-escapes
    it exactly once before the notifier — the log-only path keeps the
    readable raw text.
    """

    def _cb(event: SLAEvent) -> None:
        """SLA transition handler: log + optionally publish to Telegram."""
        message = (
            f"SLA {event.rule_name} → {event.new_state.value} "
            f"(value={event.observed_value}, threshold={event.threshold}, "
            f"predicate={event.predicate.value})"
        )
        if not telegram_enabled:
            logger.info("standard_monitor.sla_callback: %s", message)
            return
        _publish_telegram(
            kind="phase.blocked",
            severity=event.severity.value,
            message=message,
            dedupe_key=event.rule_name,
        )

    return _cb


def _make_watchdog_callback(*, telegram_enabled: bool) -> WorkerEventCallback:
    """Return a watchdog on-hung / on-recovery callback (Telegram or log-only).

    Like :func:`_make_sla_callback`, the body is raw plain text;
    :func:`_publish_telegram` escapes it for MarkdownV2 exactly once.
    """

    def _cb(event: WorkerEvent) -> None:
        """Watchdog transition handler: log + optionally publish to Telegram."""
        message = (
            f"Watchdog {event.worker_name} → {event.new_state.value} "
            f"(last_beat={event.last_beat_seconds_ago:.1f}s ago, "
            f"timeout={event.timeout_seconds:.1f}s)"
        )
        if not telegram_enabled:
            logger.info("standard_monitor.watchdog_callback: %s", message)
            return
        _publish_telegram(
            kind="worker.hung",
            severity="critical",
            message=message,
            dedupe_key=event.worker_name,
        )

    return _cb


def _publish_telegram(
    *,
    kind: str,
    severity: str,
    message: str,
    dedupe_key: str,
) -> None:
    """Best-effort Telegram publish via the lazy singleton notifier.

    *message* is the RAW plain-text body; this funnel escapes it for
    MarkdownV2 via the common
    :func:`brutex.notifications.sweep_alerts.escape_markdown_v2` before
    handing it to the notifier — :class:`TelegramEvent` requires a
    pre-escaped body and the transport sends it verbatim, so an
    unescaped ``.`` / ``-`` / ``(`` would make Telegram reject the alert
    with HTTP 400. Escape-once: callers pass raw text, ONLY this funnel
    escapes (never double-escaped).

    Swallows EVERY exception class (OSError, ValueError, RuntimeError,
    AttributeError) per Golden Rule #10 — a misconfigured Telegram
    must never crash the monitor.
    """
    try:
        # Local import keeps this module importable without
        # brutex.notifications being initialised at module-load time.
        from brutex.notifications.sweep_alerts import escape_markdown_v2
        from brutex.notifications.telegram import TelegramEvent
        from brutex.notifications.wiring import get_or_start_notifier
    except (ImportError, AttributeError):
        logger.exception(
            "standard_monitor: failed to import notifications layer; Telegram publish dropped"
        )
        return
    notifier = get_or_start_notifier()
    if notifier is None:
        return  # Telegram disabled — silent skip
    try:
        notifier.publish(
            TelegramEvent(
                kind=kind,
                severity=severity,
                message=escape_markdown_v2(message),
                dedupe_key=dedupe_key,
            )
        )
    except (OSError, ValueError, RuntimeError, AttributeError):
        logger.exception(
            "standard_monitor: Telegram publish failed for kind=%s dedupe_key=%s; event dropped",
            kind,
            dedupe_key,
        )


def _make_replay_check(
    *,
    audit_path: Path | None,
    live_subjects_file: Path | None,
    action_filter: str,
) -> ReplayCheck | None:
    """Build a closure that re-reads ``live_subjects_file`` and runs
    the L3 oracle. Returns ``None`` if either input is missing — the
    :class:`MonitorLoop` then skips the replay tick entirely.
    """
    if audit_path is None or live_subjects_file is None:
        return None

    def _replay() -> None:
        """Re-read the live-subjects file and assert L3 dedup consistency."""
        # Local import keeps this module orchestrator-independent at
        # the top level — DEC-TELEGRAM-WIRING-001 PyO3 ABI3 precedent.
        from brutex.orchestrator.replay import assert_dedup_consistent

        if not live_subjects_file.exists():
            logger.warning(
                "standard_monitor.replay_check: live_subjects_file %s does "
                "not exist; skipping this tick",
                live_subjects_file,
            )
            return
        live_subjects = frozenset(
            line
            for raw in read_regular_text(live_subjects_file).splitlines()
            if (line := raw.strip())
        )
        assert_dedup_consistent(
            audit_path=audit_path,
            live_subjects=live_subjects,
            action_filter=action_filter,
        )

    return _replay


__all__ = (
    "ENV_AUDIT_PATH",
    "ENV_LIVE_SUBJECTS_FILE",
    "ENV_POLL_INTERVAL",
    "ENV_REPLAY_ACTION_FILTER",
    "ENV_REPLAY_EVERY_N_TICKS",
    "ENV_TELEGRAM_ENABLED",
    "ENV_WATCHDOG_TIMEOUT",
    "MonitorConfig",
    "compose_standard_monitor",
)
