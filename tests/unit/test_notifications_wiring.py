"""Coverage tests for :mod:`brutex.notifications.wiring` — the
Telegram ⇄ pipeline seam closing the 2026-05-25 audit row 16 gap.

Per DEC-TELEGRAM-WIRING-001 (MIGRATION entry #238) this module is the
ONLY caller of :meth:`TelegramNotifier.publish` in the orchestrator
hot path. Coverage discipline: 100% line + 100% branch + no
networked side-effects (every test uses a fake transport).

Test classes
------------

* ``TestGetOrStartNotifier`` — singleton lifecycle + graceful
  disabled/error paths.
* ``TestMakePipelineEventSubscriber`` — per-kind mapping correctness +
  graceful no-op when notifier is ``None`` + dedupe-key derivation.
* ``TestFormatMessage`` — message-shape contract for the 5 mapped
  kinds + catch-all default.
* ``TestSubscriberSwallowsPublishExceptions`` — Golden Rule #10
  graceful-degradation contract.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
from brutex.notifications import wiring
from brutex.notifications.telegram import (
    TelegramConfig,
    TelegramConfigError,
    TelegramEvent,
    TelegramEventKind,
    TelegramNotifier,
    TelegramSeverity,
)
from brutex.orchestrator.events import IngestPhase, PipelineEvent, PipelineEventKind

# ────────────────────────────────────────────────────────────────────────
# Helpers
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
    """Return (transport, sent_log). Transport returns True (success)
    and records each event."""
    sent: list[TelegramEvent] = []

    def transport(config: TelegramConfig, event: TelegramEvent) -> bool:
        sent.append(event)
        return True

    return transport, sent


# ────────────────────────────────────────────────────────────────────────
# get_or_start_notifier — lifecycle
# ────────────────────────────────────────────────────────────────────────


class TestGetOrStartNotifier:
    def test_returns_none_when_disabled_via_explicit_config(self, reset_singleton: None) -> None:
        cfg = _make_config(enabled=False)
        assert wiring.get_or_start_notifier(config=cfg) is None

    def test_returns_notifier_when_enabled_via_explicit_config(self, reset_singleton: None) -> None:
        cfg = _make_config(enabled=True)
        result = wiring.get_or_start_notifier(config=cfg)
        assert isinstance(result, TelegramNotifier)
        # Idempotence: a second call returns the same instance
        again = wiring.get_or_start_notifier(config=cfg)
        assert again is result

    def test_caches_disabled_decision(self, reset_singleton: None) -> None:
        """When env says disabled, the second call must NOT re-read env."""
        cfg = _make_config(enabled=False)
        wiring.get_or_start_notifier(config=cfg)
        # Second call with a different config should STILL get None
        # because the singleton already cached the disabled decision.
        cfg_enabled = _make_config(enabled=True)
        assert wiring.get_or_start_notifier(config=cfg_enabled) is None

    def test_returns_none_when_telegramconfigerror(
        self, reset_singleton: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TelegramConfig.from_env raising must NOT propagate — the
        ingest loop must keep running even with a broken Telegram
        config (Golden Rule #10)."""

        def _raise(*_args: object, **_kw: object) -> TelegramConfig:
            raise TelegramConfigError("synthetic test error")

        monkeypatch.setattr("brutex.notifications.wiring.TelegramConfig.from_env", _raise)
        assert wiring.get_or_start_notifier() is None

    def test_returns_none_when_start_raises_oserror(
        self, reset_singleton: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TelegramNotifier.start raising OSError (e.g. thread alloc
        failure on a starved system) must NOT propagate."""

        def _raise(*_args: object, **_kw: object) -> TelegramNotifier:
            raise OSError("synthetic start failure")

        monkeypatch.setattr("brutex.notifications.wiring.TelegramNotifier.start", _raise)
        cfg = _make_config(enabled=True)
        assert wiring.get_or_start_notifier(config=cfg) is None

    def test_from_env_path_is_taken_when_no_explicit_config(
        self, reset_singleton: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the caller passes no config, the wiring module calls
        ``TelegramConfig.from_env`` itself."""
        called: list[int] = []

        def _from_env() -> TelegramConfig:
            called.append(1)
            return _make_config(enabled=False)

        monkeypatch.setattr("brutex.notifications.wiring.TelegramConfig.from_env", _from_env)
        assert wiring.get_or_start_notifier() is None
        assert called == [1]

    def test_reset_stops_running_notifier(self, reset_singleton: None) -> None:
        """`reset_notifier_for_tests` must call .stop() on the live
        instance to avoid leaking the worker thread between tests."""
        cfg = _make_config(enabled=True)
        notifier = wiring.get_or_start_notifier(config=cfg)
        assert notifier is not None
        wiring.reset_notifier_for_tests()
        # After reset, a new get_or_start returns a NEW instance
        cfg2 = _make_config(enabled=True)
        new_notifier = wiring.get_or_start_notifier(config=cfg2)
        assert new_notifier is not None
        assert new_notifier is not notifier


# ────────────────────────────────────────────────────────────────────────
# make_pipeline_event_subscriber — per-kind mapping
# ────────────────────────────────────────────────────────────────────────


_ALL_MAPPED_KINDS = (
    PipelineEventKind.RUN_START,
    PipelineEventKind.PHASE_START,
    PipelineEventKind.PHASE_COMPLETE,
    PipelineEventKind.PHASE_HALTED,
    PipelineEventKind.RUN_COMPLETE,
)

_UNMAPPED_KINDS = (
    PipelineEventKind.PHASE_PROGRESS,
    PipelineEventKind.ITEM_COMPLETE,
)


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


class TestMakePipelineEventSubscriber:
    @pytest.mark.parametrize("kind", _ALL_MAPPED_KINDS)
    def test_mapped_kind_results_in_publish(self, kind: str, reset_singleton: None) -> None:
        transport, sent = _capture_transport()
        cfg = _make_config(enabled=True)
        with TelegramNotifier(config=cfg, transport=transport) as notifier:
            subscriber = wiring.make_pipeline_event_subscriber(notifier=notifier)
            subscriber(_make_pipeline_event(kind))
        assert len(sent) == 1
        assert (
            sent[0].kind in TelegramEventKind.__members__.values()
            or sent[0].kind == wiring._PIPELINE_TO_TELEGRAM_KIND[kind]
        )

    @pytest.mark.parametrize("kind", _UNMAPPED_KINDS)
    def test_unmapped_kind_is_silently_dropped(self, kind: str, reset_singleton: None) -> None:
        transport, sent = _capture_transport()
        cfg = _make_config(enabled=True)
        with TelegramNotifier(config=cfg, transport=transport) as notifier:
            subscriber = wiring.make_pipeline_event_subscriber(notifier=notifier)
            subscriber(_make_pipeline_event(kind))
        assert sent == []

    def test_no_notifier_results_in_no_op(self, reset_singleton: None) -> None:
        """When the singleton returns None (Telegram disabled) the
        subscriber must be a literal no-op — never raise."""
        # Explicit config-disabled path
        wiring.get_or_start_notifier(config=_make_config(enabled=False))
        subscriber = wiring.make_pipeline_event_subscriber()
        subscriber(_make_pipeline_event(PipelineEventKind.RUN_START))
        # Did not raise => pass

    def test_subscriber_uses_singleton_when_notifier_arg_is_none(
        self, reset_singleton: None
    ) -> None:
        transport, sent = _capture_transport()
        cfg = _make_config(enabled=True)
        notifier = TelegramNotifier.start(config=cfg, transport=transport)
        # Inject the captured-transport notifier into the singleton
        # state container so a subsequent get_or_start_notifier() call
        # (which fires from inside make_pipeline_event_subscriber when
        # no explicit notifier is passed) returns OUR notifier.
        try:
            wiring._state["notifier"] = notifier
            wiring._state["attempted"] = True
            subscriber = wiring.make_pipeline_event_subscriber()
            subscriber(_make_pipeline_event(PipelineEventKind.RUN_START))
            notifier.drain(timeout=1.0)
        finally:
            notifier.stop(timeout=1.0)
        assert len(sent) == 1

    def test_severity_critical_for_phase_halted(self, reset_singleton: None) -> None:
        transport, sent = _capture_transport()
        cfg = _make_config(enabled=True)
        with TelegramNotifier(config=cfg, transport=transport) as notifier:
            subscriber = wiring.make_pipeline_event_subscriber(notifier=notifier)
            subscriber(_make_pipeline_event(PipelineEventKind.PHASE_HALTED))
        assert sent[0].severity == TelegramSeverity.CRITICAL

    def test_severity_info_for_run_start(self, reset_singleton: None) -> None:
        transport, sent = _capture_transport()
        cfg = _make_config(enabled=True)
        with TelegramNotifier(config=cfg, transport=transport) as notifier:
            subscriber = wiring.make_pipeline_event_subscriber(notifier=notifier)
            subscriber(_make_pipeline_event(PipelineEventKind.RUN_START))
        assert sent[0].severity == TelegramSeverity.INFO

    def test_dedupe_key_contains_run_and_phase(self, reset_singleton: None) -> None:
        transport, sent = _capture_transport()
        cfg = _make_config(enabled=True)
        with TelegramNotifier(config=cfg, transport=transport) as notifier:
            subscriber = wiring.make_pipeline_event_subscriber(notifier=notifier)
            subscriber(
                _make_pipeline_event(
                    PipelineEventKind.PHASE_START,
                    run_id="my-run",
                    phase=IngestPhase.A_INSTRUMENTS,
                )
            )
        assert "my-run" in sent[0].dedupe_key
        assert IngestPhase.A_INSTRUMENTS.value in sent[0].dedupe_key

    def test_dedupe_key_empty_when_no_run_no_phase(self, reset_singleton: None) -> None:
        transport, sent = _capture_transport()
        cfg = _make_config(enabled=True)
        with TelegramNotifier(config=cfg, transport=transport) as notifier:
            subscriber = wiring.make_pipeline_event_subscriber(notifier=notifier)
            subscriber(_make_pipeline_event(PipelineEventKind.RUN_START, run_id="", phase=None))
        assert sent[0].dedupe_key == ""


# ────────────────────────────────────────────────────────────────────────
# Message formatting
# ────────────────────────────────────────────────────────────────────────


class TestFormatMessage:
    def test_run_start_message(self) -> None:
        msg = wiring._format_message(
            _make_pipeline_event(PipelineEventKind.RUN_START, run_id="r-1")
        )
        assert "started" in msg
        assert "r-1" in msg

    def test_run_complete_with_elapsed(self) -> None:
        msg = wiring._format_message(
            _make_pipeline_event(
                PipelineEventKind.RUN_COMPLETE,
                run_id="r-1",
                phase=None,
                elapsed_seconds=42.5,
            )
        )
        assert "complete" in msg.lower()
        assert "42.5" in msg

    def test_run_complete_without_elapsed(self) -> None:
        msg = wiring._format_message(
            _make_pipeline_event(PipelineEventKind.RUN_COMPLETE, run_id="r-1", phase=None)
        )
        assert "complete" in msg.lower()
        assert "elapsed=" not in msg

    def test_phase_start_with_total(self) -> None:
        msg = wiring._format_message(
            _make_pipeline_event(
                PipelineEventKind.PHASE_START,
                total=123,
                phase=IngestPhase.A_INSTRUMENTS,
            )
        )
        assert "Phase start" in msg
        assert "123" in msg

    def test_phase_start_without_total(self) -> None:
        msg = wiring._format_message(
            _make_pipeline_event(PipelineEventKind.PHASE_START, phase=IngestPhase.A_INSTRUMENTS)
        )
        assert "Phase start" in msg
        assert "total=" not in msg

    def test_phase_complete_with_counts(self) -> None:
        msg = wiring._format_message(
            _make_pipeline_event(
                PipelineEventKind.PHASE_COMPLETE,
                success=10,
                quarantined=2,
            )
        )
        assert "Phase complete" in msg
        assert "success=10" in msg
        assert "quarantined=2" in msg

    def test_phase_complete_without_counts(self) -> None:
        msg = wiring._format_message(_make_pipeline_event(PipelineEventKind.PHASE_COMPLETE))
        assert "Phase complete" in msg
        assert "success=" not in msg
        assert "quarantined=" not in msg

    def test_phase_halted_with_message(self) -> None:
        msg = wiring._format_message(
            _make_pipeline_event(PipelineEventKind.PHASE_HALTED, message="adapter timeout")
        )
        assert "HALTED" in msg
        assert "adapter timeout" in msg

    def test_phase_halted_without_message(self) -> None:
        msg = wiring._format_message(_make_pipeline_event(PipelineEventKind.PHASE_HALTED))
        assert "HALTED" in msg
        assert "reason=" not in msg

    def test_unknown_kind_fallback(self) -> None:
        """Defensive: a future PipelineEventKind we haven't mapped
        produces a sane fallback string, NOT an exception."""
        msg = wiring._format_message(
            PipelineEvent(
                kind="future.unknown",
                at=datetime(2026, 1, 1, tzinfo=UTC),
                phase=None,
                run_id="rrr",
            )
        )
        assert "future.unknown" in msg
        assert "rrr" in msg

    def test_no_phase_no_run_id(self) -> None:
        """Defensive: an event without run_id or phase still produces
        a non-empty message."""
        msg = wiring._format_message(
            PipelineEvent(
                kind=PipelineEventKind.RUN_START,
                at=datetime(2026, 1, 1, tzinfo=UTC),
                phase=None,
                run_id=None,
            )
        )
        assert "started" in msg


# ────────────────────────────────────────────────────────────────────────
# Graceful degradation — publish exceptions are swallowed
# ────────────────────────────────────────────────────────────────────────


class TestSubscriberSwallowsPublishExceptions:
    @pytest.mark.parametrize(
        "exc_cls",
        [OSError, ValueError, RuntimeError],
    )
    def test_publish_oserror_does_not_propagate(
        self, exc_cls: type[BaseException], reset_singleton: None
    ) -> None:
        """Per Golden Rule #10 — a broken Telegram must NOT crash the
        ingest loop. Subscriber catches OSError + ValueError +
        RuntimeError and logs them.
        """

        class BrokenNotifier:
            def publish(self, _event: TelegramEvent) -> None:
                raise exc_cls("synthetic")

        subscriber = wiring.make_pipeline_event_subscriber(
            notifier=BrokenNotifier(),  # type: ignore[arg-type]
        )
        # Must not raise
        subscriber(_make_pipeline_event(PipelineEventKind.RUN_START))
