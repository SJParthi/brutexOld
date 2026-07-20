"""Regression tests for the MarkdownV2 escaping hole (R2-OCLI-F1).

``TelegramEvent.message`` requires a PRE-escaped MarkdownV2 body — the
transport (`telegram._default_transport`) sends it VERBATIM with
``parse_mode=MarkdownV2``, so an unescaped reserved char
(``_ * [ ] ( ) ~ ` > # + - = | { } . !``) in a dynamic field (symbol,
decimal, parenthesised reason) makes Telegram reject the alert with
HTTP 400 and the event is silently dropped.

These tests pin that BOTH source modules escape at the publish seam,
via the common :func:`brutex.notifications.sweep_alerts.escape_markdown_v2`:

* :mod:`brutex.notifications.wiring` — the pipeline-event subscriber
  escapes ``_format_message`` output before ``notifier.publish``.
* :mod:`brutex.observability.standard_monitor` — the SLA + watchdog
  callback bodies are escaped exactly once at the ``_publish_telegram``
  funnel (the log-only path keeps raw readable text).

Every assertion spies on the body actually handed to the notifier /
transport and checks the reserved chars arrive backslash-escaped —
and exactly ONCE (no double-escaping).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from brutex.notifications import wiring
from brutex.notifications.sweep_alerts import escape_markdown_v2
from brutex.notifications.telegram import (
    TelegramConfig,
    TelegramEvent,
    TelegramNotifier,
)
from brutex.observability.sla import (
    SLAEvent,
    SLAPredicate,
    SLASeverity,
    SLAState,
)
from brutex.observability.standard_monitor import (
    _make_sla_callback,
    _make_watchdog_callback,
    _publish_telegram,
)
from brutex.observability.watchdog import WorkerEvent, WorkerState
from brutex.orchestrator.events import IngestPhase, PipelineEvent, PipelineEventKind

# MarkdownV2 reserved set (mirrors sweep_alerts._MDV2_SPECIALS — re-stated
# here so a silent narrowing of the common helper fails these tests).
_RESERVED: frozenset[str] = frozenset(r"_*[]()~`>#+-=|{}.!")


def _count_reserved(text: str) -> int:
    return sum(1 for ch in text if ch in _RESERVED)


# ────────────────────────────────────────────────────────────────────────
# Shared helpers (mirror tests/unit/test_notifications_wiring.py)
# ────────────────────────────────────────────────────────────────────────


def _make_config(*, enabled: bool = True) -> TelegramConfig:
    return TelegramConfig(
        bot_token="t" * 30,  # synthetic test token, never sent over network
        chat_id="12345",
        enabled=enabled,
    )


@pytest.fixture
def reset_singleton() -> Iterator[None]:
    """Reset the wiring-module singleton before AND after every test."""
    wiring.reset_notifier_for_tests()
    yield
    wiring.reset_notifier_for_tests()


def _capture_transport() -> tuple[
    Callable[[TelegramConfig, TelegramEvent], bool], list[TelegramEvent]
]:
    """Return (transport, sent_log). Transport records + reports success."""
    sent: list[TelegramEvent] = []

    def transport(config: TelegramConfig, event: TelegramEvent) -> bool:
        sent.append(event)
        return True

    return transport, sent


def _make_pipeline_event(
    kind: str,
    *,
    run_id: str = "run-123",
    phase: IngestPhase | None = IngestPhase.A_INSTRUMENTS,
    total: int | None = None,
    success: int | None = None,
    quarantined: int | None = None,
    elapsed_seconds: float | None = None,
    message: str | None = None,
) -> PipelineEvent:
    return PipelineEvent(
        kind=kind,
        at=datetime(2026, 5, 25, 12, 0, tzinfo=UTC),
        phase=phase,
        run_id=run_id,
        total=total,
        success=success,
        quarantined=quarantined,
        elapsed_seconds=elapsed_seconds,
        message=message,
    )


# ────────────────────────────────────────────────────────────────────────
# wiring.py — subscriber bodies arrive escaped at the transport
# ────────────────────────────────────────────────────────────────────────


class TestWiringSubscriberBodiesEscaped:
    def test_phase_halted_reason_with_reserved_chars_escaped(self, reset_singleton: None) -> None:
        """A halt reason carrying symbol-like + parenthesised + decimal
        text arrives backslash-escaped at the transport."""
        transport, sent = _capture_transport()
        with TelegramNotifier(config=_make_config(), transport=transport) as notifier:
            subscriber = wiring.make_pipeline_event_subscriber(notifier=notifier)
            subscriber(
                _make_pipeline_event(
                    PipelineEventKind.PHASE_HALTED,
                    run_id="A.B-C",
                    message="adapter timeout (code=3.5)!",
                )
            )
        assert len(sent) == 1
        body = sent[0].message
        # The raw reserved sequences must NOT appear unescaped.
        assert "A.B-C" not in body
        assert "(code=3.5)!" not in body
        # ... and DO appear escaped.
        assert r"A\.B\-C" in body
        assert r"\(code\=3\.5\)\!" in body

    def test_run_complete_elapsed_decimal_escaped(self, reset_singleton: None) -> None:
        transport, sent = _capture_transport()
        with TelegramNotifier(config=_make_config(), transport=transport) as notifier:
            subscriber = wiring.make_pipeline_event_subscriber(notifier=notifier)
            subscriber(
                _make_pipeline_event(
                    PipelineEventKind.RUN_COMPLETE,
                    run_id="r-1",
                    phase=None,
                    elapsed_seconds=42.5,
                )
            )
        body = sent[0].message
        assert r"42\.5s" in body
        assert "42.5s" not in body

    @pytest.mark.parametrize(
        "kind",
        [
            PipelineEventKind.RUN_START,
            PipelineEventKind.PHASE_START,
            PipelineEventKind.PHASE_COMPLETE,
            PipelineEventKind.PHASE_HALTED,
            PipelineEventKind.RUN_COMPLETE,
        ],
    )
    def test_published_body_is_exactly_escape_of_format_message(
        self, kind: str, reset_singleton: None
    ) -> None:
        """For every mapped kind: published body == escape_markdown_v2(raw)
        — escaped exactly ONCE (backslash count == reserved-char count)."""
        transport, sent = _capture_transport()
        event = _make_pipeline_event(
            kind,
            run_id="run_1.x",
            total=3,
            success=2,
            quarantined=1,
            elapsed_seconds=1.5,
            message="halt (why=slow)",
        )
        with TelegramNotifier(config=_make_config(), transport=transport) as notifier:
            subscriber = wiring.make_pipeline_event_subscriber(notifier=notifier)
            subscriber(event)
        raw = wiring._format_message(event)
        body = sent[0].message
        assert body == escape_markdown_v2(raw)
        assert body.count("\\") == _count_reserved(raw)


# ────────────────────────────────────────────────────────────────────────
# standard_monitor.py — SLA / watchdog bodies escaped at the funnel
# ────────────────────────────────────────────────────────────────────────


def _install_fake_notifier(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Any]:
    """Monkeypatch the wiring singleton getter with a capturing fake."""
    captured: list[Any] = []

    class _FakeNotifier:
        def publish(self, event: Any) -> None:
            captured.append(event)

    monkeypatch.setattr(
        "brutex.notifications.wiring.get_or_start_notifier",
        _FakeNotifier,
    )
    return captured


class TestStandardMonitorBodiesEscaped:
    def test_sla_breach_body_reserved_chars_escaped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _install_fake_notifier(monkeypatch)
        cb = _make_sla_callback(telegram_enabled=True)
        cb(
            SLAEvent(
                rule_name="lag.p99-rule",
                severity=SLASeverity.CRITICAL,
                description="synthetic",
                observed_value=3.5,
                threshold=100.0,
                predicate=SLAPredicate.GREATER_THAN,
                new_state=SLAState.BREACH,
            )
        )
        assert len(captured) == 1
        body = captured[0].message
        # Rule name with '.' and '-' escaped; decimals + parens + '=' escaped.
        assert r"lag\.p99\-rule" in body
        assert "lag.p99-rule" not in body
        assert r"\(value\=3\.5" in body
        assert r"threshold\=100\.0" in body
        # Dedupe key stays RAW — it is never sent as a message body.
        assert captured[0].dedupe_key == "lag.p99-rule"

    def test_watchdog_hung_body_reserved_chars_escaped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_fake_notifier(monkeypatch)
        cb = _make_watchdog_callback(telegram_enabled=True)
        cb(
            WorkerEvent(
                worker_name="w-1.a",
                new_state=WorkerState.HUNG,
                last_beat_seconds_ago=15.0,
                timeout_seconds=10.0,
            )
        )
        assert len(captured) == 1
        body = captured[0].message
        assert r"w\-1\.a" in body
        assert "w-1.a" not in body
        assert r"last\_beat\=15\.0s" in body
        assert r"timeout\=10\.0s" in body
        assert captured[0].dedupe_key == "w-1.a"

    def test_publish_telegram_escapes_exactly_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The funnel applies escape_markdown_v2 exactly once to the raw
        body — one backslash per reserved char, nothing more."""
        captured = _install_fake_notifier(monkeypatch)
        raw = "A.B-C (x=3.5)!"
        _publish_telegram(kind="k", severity="info", message=raw, dedupe_key="d")
        assert len(captured) == 1
        body = captured[0].message
        assert body == escape_markdown_v2(raw)
        assert body.count("\\") == _count_reserved(raw) == 7

    def test_log_only_path_keeps_raw_readable_text(self, caplog: pytest.LogCaptureFixture) -> None:
        """telegram_enabled=False logs the RAW body — escaping is only
        for the Telegram wire, never the operator's log."""
        cb = _make_sla_callback(telegram_enabled=False)
        with caplog.at_level(logging.INFO, logger="brutex.observability.standard_monitor"):
            cb(
                SLAEvent(
                    rule_name="lag.p99-rule",
                    severity=SLASeverity.WARN,
                    description="synthetic",
                    observed_value=3.5,
                    threshold=100.0,
                    predicate=SLAPredicate.GREATER_THAN,
                    new_state=SLAState.BREACH,
                )
            )
        assert "lag.p99-rule" in caplog.text
        assert r"lag\.p99\-rule" not in caplog.text
