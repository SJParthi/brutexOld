"""End-to-end wiring proof for the SLA → Telegram chain.

Closes the verification gap on `docs/STANDARDS_100PCT_HONEST_AUDIT.md` §9
Gap #1 ("Telegram alerts not wired"). The audit row was written before
`compose_standard_monitor` (PR #131) + `cmd_monitor` (PR #133) landed.
Per Golden Rule #10 (honest envelope), Gap #1 is now PARTIAL not HIGH:
the SLA + Watchdog + Replay → Telegram paths ARE wired, but the
pipeline-event subscriber path (`make_pipeline_event_subscriber`) is
still dormant pending Phase 2 drivers.

This integration test drives the full SLA chain end-to-end:

    SLARule (breach) → SLADetector.check_once → on_breach callback
        → _publish_telegram → wiring.get_or_start_notifier
        → TelegramNotifier.publish → recording transport

It is the single cite-able test the honest-audit can point at to
demonstrate the chain works under realistic synchronous semantics.

Decision: DEC-TELEGRAM-WIRING-VERIFICATION-001 (MIGRATION entry #248).
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field

import pytest
from brutex.notifications import wiring
from brutex.notifications.telegram import (
    TelegramConfig,
    TelegramEvent,
    TelegramNotifier,
)
from brutex.observability.sla import (
    SLAPredicate,
    SLARule,
    SLASeverity,
)
from brutex.observability.standard_monitor import (
    MonitorConfig,
    compose_standard_monitor,
)


@dataclass
class RecordingTransport:
    """Captures every outbound Telegram call.

    Mirrors the helper in ``tests/integration/test_telegram_wire_format.py``
    so both files share the same recording shape — the wire-format
    test asserts on payload schema; this test asserts on call sequence.
    """

    sends: list[tuple[TelegramConfig, TelegramEvent]] = field(default_factory=list)

    def __call__(self, config: TelegramConfig, event: TelegramEvent) -> bool:
        self.sends.append((config, event))
        return True


@pytest.fixture
def transport() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
def telegram_config() -> TelegramConfig:
    return TelegramConfig(
        bot_token="test-token",
        chat_id="@test-channel",
        enabled=True,
    )


@pytest.fixture
def primed_notifier(
    telegram_config: TelegramConfig,
    transport: RecordingTransport,
) -> Generator[TelegramNotifier]:
    """Inject a synchronous notifier into the wiring singleton.

    Standard-monitor callbacks call ``get_or_start_notifier()`` at
    runtime; pre-seeding the singleton lets us skip env reads + real
    HTTP startup while still proving the chain reaches the transport.
    """
    notifier = TelegramNotifier(
        config=telegram_config,
        transport=transport,
        synchronous=True,
    )
    wiring.reset_notifier_for_tests()
    wiring._state["notifier"] = notifier
    wiring._state["attempted"] = True
    try:
        yield notifier
    finally:
        wiring.reset_notifier_for_tests()


class TestSLABreachReachesTelegram:
    """The headline assertion: an SLA breach triggers exactly one
    Telegram publish through the chain assembled by
    ``compose_standard_monitor``.
    """

    def test_breach_then_recovery_publishes_two_events(
        self,
        primed_notifier: TelegramNotifier,
        transport: RecordingTransport,
    ) -> None:
        observed_value = {"x": 100.0}  # mutable closure — flips between breach + recovery

        def provider() -> float:
            return observed_value["x"]

        rule = SLARule(
            name="ingest_lag_p99_seconds",
            value_provider=provider,
            predicate=SLAPredicate.GREATER_THAN,
            threshold=50.0,
            severity=SLASeverity.WARN,
            description="ingest p99 lag must be under 50s",
        )

        cfg = MonitorConfig(
            telegram_enabled=True,
            sla_rules=(rule,),
        )
        loop = compose_standard_monitor(cfg)
        assert loop.sla_detector is not None  # compose builds one when rules are passed

        # Tick 1 — value > threshold → breach → Telegram publish
        loop.sla_detector.check_once()

        # Tick 2 — value back below threshold → recovery → second publish
        observed_value["x"] = 5.0
        loop.sla_detector.check_once()

        assert len(transport.sends) == 2
        first_send_event = transport.sends[0][1]
        second_send_event = transport.sends[1][1]
        # Both events carry the rule name as the dedupe key so re-fires
        # within the dedupe window collapse correctly.
        assert first_send_event.dedupe_key == "ingest_lag_p99_seconds"
        assert second_send_event.dedupe_key == "ingest_lag_p99_seconds"
        # The first send is the breach (state transition NORMAL → BREACHED).
        # The body is MarkdownV2-escaped at the _publish_telegram funnel
        # (R2-OCLI-F1) — the raw rule name arrives with `_` escaped.
        assert "ingest_lag_p99_seconds" not in first_send_event.message
        assert r"ingest\_lag\_p99\_seconds" in first_send_event.message

    def test_no_publish_when_value_stays_normal(
        self,
        primed_notifier: TelegramNotifier,
        transport: RecordingTransport,
    ) -> None:
        rule = SLARule(
            name="cpu_pct",
            value_provider=lambda: 5.0,  # well below threshold
            predicate=SLAPredicate.GREATER_THAN,
            threshold=80.0,
            severity=SLASeverity.WARN,
        )

        cfg = MonitorConfig(
            telegram_enabled=True,
            sla_rules=(rule,),
        )
        loop = compose_standard_monitor(cfg)
        assert loop.sla_detector is not None

        for _ in range(3):
            loop.sla_detector.check_once()

        # No state transitions = no callbacks = no Telegram publish.
        assert transport.sends == []

    def test_telegram_disabled_means_no_publish_even_on_breach(
        self,
        primed_notifier: TelegramNotifier,
        transport: RecordingTransport,
    ) -> None:
        rule = SLARule(
            name="error_rate",
            value_provider=lambda: 0.5,
            predicate=SLAPredicate.GREATER_THAN,
            threshold=0.01,
            severity=SLASeverity.ERROR,
        )

        # `telegram_enabled=False` means the SLA callback logs the
        # event but does NOT call _publish_telegram → no transport hit.
        cfg = MonitorConfig(
            telegram_enabled=False,
            sla_rules=(rule,),
        )
        loop = compose_standard_monitor(cfg)
        assert loop.sla_detector is not None
        loop.sla_detector.check_once()

        assert transport.sends == []
