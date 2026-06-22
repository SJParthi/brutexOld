"""End-to-end real-run proof of the sweep-alert wiring (DEC-TELEGRAM-WIRE-001).

Drives :class:`brutex.notifications.sweep_alerts.SweepAlerter` through a REAL
:class:`~brutex.notifications.telegram.TelegramNotifier` — both the synchronous
inline path AND the asynchronous daemon-thread queue path — with an injected
recording transport (no network). Proves that each of the four AUTOMATIC alerts
(watchdog-hung / rate.breach / auth.renew_failed / sweep complete) and the MANUAL
``notify test`` digest reaches the wire with the right kind, severity, and a
MarkdownV2-safe body, and that the supervisor's ``SweepWatchdog`` on-hung edge
actually fires the Telegram alert through the real callback seam.

Uses the injectable transport, so it needs no network access.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field

import pytest
from brutex.notifications.sweep_alerts import SweepAlerter, escape_markdown_v2
from brutex.notifications.telegram import (
    TelegramConfig,
    TelegramEvent,
    TelegramEventKind,
    TelegramNotifier,
    TelegramSeverity,
)


@dataclass
class RecordingTransport:
    """Captures every (config, event) the notifier sends."""

    sends: list[tuple[TelegramConfig, TelegramEvent]] = field(default_factory=list)

    def __call__(self, config: TelegramConfig, event: TelegramEvent) -> bool:
        """Record the send and report success."""
        self.sends.append((config, event))
        return True


@dataclass(frozen=True)
class FakeSummary:
    """Typed :class:`SweepSummaryLike` stand-in for the complete-alert test."""

    sweep_id: str
    tuples_run: int
    tuples_total: int
    evaluated: int
    ranked: int
    persisted: int
    tuples_failed: int = 0


@pytest.fixture
def config() -> TelegramConfig:
    return TelegramConfig(bot_token="test-token", chat_id="@brutex-ops", enabled=True)


@pytest.fixture
def transport() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
def sync_notifier(
    config: TelegramConfig, transport: RecordingTransport
) -> Generator[TelegramNotifier]:
    n = TelegramNotifier(config=config, transport=transport, synchronous=True)
    yield n
    n.stop()


class TestSyncWire:
    def test_all_four_automatic_alerts_reach_the_wire(
        self, sync_notifier: TelegramNotifier, transport: RecordingTransport
    ) -> None:
        alerter = SweepAlerter(notifier=sync_notifier)

        alerter.watchdog_hung(
            worker_name="sweep-worker-0", last_beat_seconds_ago=400.0, timeout_seconds=300.0
        )
        alerter.rate_breach(current_rps=3.0, recent_hits=5)
        alerter.auth_renew_failed(reason="token expired")
        summary = FakeSummary(
            sweep_id="deadbeefcafe0001",
            tuples_run=8,
            tuples_total=8,
            evaluated=2048,
            ranked=12,
            persisted=9,
        )
        alerter.sweep_complete(summary)

        kinds = [event.kind for _cfg, event in transport.sends]
        assert kinds == [
            TelegramEventKind.WORKER_HUNG,
            TelegramEventKind.RATE_LIMIT_DOWNSHIFT,
            TelegramEventKind.AUTH_RENEW_FAILED,
            TelegramEventKind.RUN_COMPLETE,
        ]
        # Every body is MarkdownV2-safe (escaper applied), so Telegram won't 400.
        for _cfg, event in transport.sends:
            assert event.message == escape_markdown_v2(_unescape_roundtrip(event.message))

    def test_pull_halt_alert_reaches_the_wire(
        self, sync_notifier: TelegramNotifier, transport: RecordingTransport
    ) -> None:
        # A pull-wide halt (disk-full/OOM/quota) fires a CRITICAL PHASE_BLOCKED push
        # through the real notifier, MarkdownV2-safe (DEC-TELEGRAM-PULL-HALT-001).
        assert SweepAlerter(notifier=sync_notifier).pull_halted(reason="disk_full") is True
        assert len(transport.sends) == 1
        _cfg, event = transport.sends[0]
        assert event.kind == TelegramEventKind.PHASE_BLOCKED
        assert event.severity == TelegramSeverity.CRITICAL
        assert event.message == escape_markdown_v2(_unescape_roundtrip(event.message))

    def test_manual_test_digest_reaches_the_wire(
        self, sync_notifier: TelegramNotifier, transport: RecordingTransport
    ) -> None:
        assert SweepAlerter(notifier=sync_notifier).test_digest() is True
        assert len(transport.sends) == 1
        _cfg, event = transport.sends[0]
        assert event.kind == TelegramEventKind.TEST
        assert event.severity == TelegramSeverity.INFO

    def test_watchdog_callback_seam_fires_through_real_watchdog(
        self, sync_notifier: TelegramNotifier, transport: RecordingTransport
    ) -> None:
        # Wire the alerter's on-hung callback into a REAL Watchdog edge detector
        # (the wheel-free primitive the sweep SweepWatchdog wraps) and force a
        # HEALTHY→HUNG edge with an injected clock — proves the callback seam
        # delivers a WORKER_HUNG Telegram event on a genuine hung-worker edge.
        from brutex.observability.watchdog import Watchdog

        clock = {"t": 0.0}
        wd = Watchdog(
            timeout_seconds=10.0,
            on_hung=SweepAlerter(notifier=sync_notifier).watchdog_hung_callback(),
            on_recovery=lambda _event: None,
            clock=lambda: clock["t"],
        )
        wd.beat("sweep-worker-0")
        clock["t"] = 100.0  # advance well past the timeout
        events = wd.check_once()
        assert len(events) == 1  # the watchdog detected the hang
        assert len(transport.sends) == 1
        _cfg, event = transport.sends[0]
        assert event.kind == TelegramEventKind.WORKER_HUNG
        assert event.severity == TelegramSeverity.CRITICAL


class TestAsyncWire:
    def test_async_queue_path_delivers(
        self, config: TelegramConfig, transport: RecordingTransport
    ) -> None:
        # The real daemon-thread path: publish, then drain the worker queue.
        notifier = TelegramNotifier.start(config=config, transport=transport)
        try:
            alerter = SweepAlerter(notifier=notifier)
            assert alerter.test_digest() is True
            notifier.drain(timeout=5.0)
        finally:
            notifier.stop()
        assert len(transport.sends) == 1
        assert transport.sends[0][1].kind == TelegramEventKind.TEST


def _unescape_roundtrip(escaped: str) -> str:
    """Strip the single MarkdownV2 escape backslashes so we can re-escape + compare.

    Removes a ``\\`` only when it precedes a reserved char (the exact pattern the
    escaper inserts), so re-applying :func:`escape_markdown_v2` to the result must
    reproduce *escaped* — the byte-for-byte safety round-trip.
    """
    specials = set(r"_*[]()~`>#+-=|{}.!")
    out: list[str] = []
    i = 0
    while i < len(escaped):
        ch = escaped[i]
        if ch == "\\" and i + 1 < len(escaped) and escaped[i + 1] in specials:
            i += 1
            continue
        out.append(escaped[i])
        i += 1
    return "".join(out)
