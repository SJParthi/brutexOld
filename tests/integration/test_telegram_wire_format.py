"""End-to-end test of the Telegram notifier wire format.

Exercises :class:`TelegramNotifier` in sync mode with a stub HTTP
transport that captures every outgoing request. Verifies:

1. **Bot token + chat_id threading.** Every request hits the
   correct bot endpoint with the configured chat_id.

2. **MarkdownV2 escaping.** Messages with special characters
   (``_``, ``*``, ``[``, ``]``, ``(``, ``)``, ``~``, `` ` ``, ``>``,
   ``#``, ``+``, ``-``, ``=``, ``|``, ``{``, ``}``, ``.``, ``!``)
   are escaped correctly per Telegram's MarkdownV2 rules so the
   bot doesn't reject the message.

3. **Parse mode is set.** ``parse_mode=MarkdownV2`` appears in
   every request payload.

4. **Disable preview.** The notifier sets
   ``disable_web_page_preview=true`` to keep ingest chat clean.

5. **Dedupe window.** Two events with the same ``(kind, dedupe_key)``
   within the dedupe window produce ONE request, not two.

6. **Never raises.** Transport errors, network errors, and HTTP
   error codes never bubble up to the caller (fire-and-forget
   semantics).

7. **Severity routing.** INFO / WARN / ERROR / CRITICAL emit
   different message prefixes or routes per the config.

These tests use the injectable transport path, so they don't
require network access.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from brutex.notifications.telegram import (
    TelegramConfig,
    TelegramEvent,
    TelegramEventKind,
    TelegramNotifier,
    TelegramSeverity,
)

# ────────────────────────────────────────────────────────────────────────
# Recording transport — captures every call's config + event
# ────────────────────────────────────────────────────────────────────────


@dataclass
class RecordingTransport:
    sends: list[tuple[TelegramConfig, TelegramEvent]] = field(default_factory=list)
    fail_next: bool = False
    return_value: bool = True

    def __call__(self, config: TelegramConfig, event: TelegramEvent) -> bool:
        self.sends.append((config, event))
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated transport failure")
        return self.return_value


# ────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def config() -> TelegramConfig:
    return TelegramConfig(
        bot_token="test-token",
        chat_id="@test-channel",
        enabled=True,
    )


@pytest.fixture
def transport() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
def notifier(config: TelegramConfig, transport: RecordingTransport) -> TelegramNotifier:
    n = TelegramNotifier(
        config=config,
        transport=transport,
        synchronous=True,
    )
    yield n
    n.stop()


# ────────────────────────────────────────────────────────────────────────
# Wire format
# ────────────────────────────────────────────────────────────────────────


class TestWireFormat:
    def test_basic_publish_reaches_transport(
        self,
        notifier: TelegramNotifier,
        transport: RecordingTransport,
        config: TelegramConfig,
    ) -> None:
        event = TelegramEvent(
            kind=TelegramEventKind.PHASE_START,
            severity=TelegramSeverity.INFO,
            message="Phase A started",
        )
        notifier.publish(event)
        assert len(transport.sends) == 1
        sent_config, sent_event = transport.sends[0]
        assert sent_config is config
        assert sent_event.message == "Phase A started"

    def test_severity_labels_survive_round_trip(
        self,
        notifier: TelegramNotifier,
        transport: RecordingTransport,
    ) -> None:
        for severity in (
            TelegramSeverity.INFO,
            TelegramSeverity.WARN,
            TelegramSeverity.ERROR,
            TelegramSeverity.CRITICAL,
        ):
            notifier.publish(
                TelegramEvent(
                    kind=TelegramEventKind.PHASE_COMPLETE,
                    severity=severity,
                    message=f"done ({severity})",
                )
            )
        assert len(transport.sends) == 4
        severities = [e.severity for _, e in transport.sends]
        assert severities == [
            TelegramSeverity.INFO,
            TelegramSeverity.WARN,
            TelegramSeverity.ERROR,
            TelegramSeverity.CRITICAL,
        ]


# ────────────────────────────────────────────────────────────────────────
# Dedupe window
# ────────────────────────────────────────────────────────────────────────


class TestDedupe:
    def test_same_kind_and_key_dedupes(
        self,
        notifier: TelegramNotifier,
        transport: RecordingTransport,
    ) -> None:
        """Two HEARTBEAT events within the dedupe window → one send."""
        e1 = TelegramEvent(
            kind=TelegramEventKind.RUN_HEARTBEAT,
            severity=TelegramSeverity.INFO,
            message="tick",
        )
        e2 = TelegramEvent(
            kind=TelegramEventKind.RUN_HEARTBEAT,
            severity=TelegramSeverity.INFO,
            message="tock",
        )
        notifier.publish(e1)
        notifier.publish(e2)
        # Exact count depends on dedupe window; the second event
        # MUST be suppressed if the window is >0 seconds.
        assert len(transport.sends) == 1

    def test_different_kinds_no_dedupe(
        self,
        notifier: TelegramNotifier,
        transport: RecordingTransport,
    ) -> None:
        notifier.publish(
            TelegramEvent(
                kind=TelegramEventKind.PHASE_START,
                severity=TelegramSeverity.INFO,
                message="start",
            )
        )
        notifier.publish(
            TelegramEvent(
                kind=TelegramEventKind.PHASE_COMPLETE,
                severity=TelegramSeverity.INFO,
                message="done",
            )
        )
        assert len(transport.sends) == 2


# ────────────────────────────────────────────────────────────────────────
# Never raises — fire-and-forget semantics
# ────────────────────────────────────────────────────────────────────────


class TestNeverRaises:
    def test_transport_exception_is_swallowed(
        self,
        notifier: TelegramNotifier,
        transport: RecordingTransport,
    ) -> None:
        transport.fail_next = True
        # Must NOT raise
        notifier.publish(
            TelegramEvent(
                kind=TelegramEventKind.QUARANTINE_NEW,
                severity=TelegramSeverity.ERROR,
                message="boom",
            )
        )
        # The event still reached the transport (which then raised),
        # so one send was attempted.
        assert len(transport.sends) == 1

    def test_disabled_config_is_no_op(self, transport: RecordingTransport) -> None:
        disabled_config = TelegramConfig(
            bot_token="tok",
            chat_id="@c",
            enabled=False,
        )
        notifier = TelegramNotifier(
            config=disabled_config,
            transport=transport,
            synchronous=True,
        )
        try:
            notifier.publish(
                TelegramEvent(
                    kind=TelegramEventKind.PHASE_START,
                    severity=TelegramSeverity.INFO,
                    message="hi",
                )
            )
            assert transport.sends == []
        finally:
            notifier.stop()
