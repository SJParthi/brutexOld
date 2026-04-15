"""Unit tests for brutex.notifications.telegram.

All tests use synchronous mode + injected transport — zero real
network calls, zero thread races.
"""

from __future__ import annotations

import pytest
from brutex.notifications.telegram import (
    ENV_BOT_TOKEN,
    ENV_CHAT_ID,
    ENV_ENABLED,
    TelegramBucket,
    TelegramConfig,
    TelegramConfigError,
    TelegramEvent,
    TelegramEventKind,
    TelegramNotifier,
    TelegramSeverity,
)

# ────────────────────────────────────────────────────────────────────────
# TelegramConfig.from_env
# ────────────────────────────────────────────────────────────────────────


class TestConfigFromEnv:
    def test_disabled_when_env_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in (ENV_ENABLED, ENV_BOT_TOKEN, ENV_CHAT_ID):
            monkeypatch.delenv(k, raising=False)
        config = TelegramConfig.from_env()
        assert config.enabled is False

    def test_disabled_when_enabled_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_ENABLED, "false")
        config = TelegramConfig.from_env()
        assert config.enabled is False

    def test_enabled_true_requires_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_ENABLED, "true")
        monkeypatch.delenv(ENV_BOT_TOKEN, raising=False)
        monkeypatch.setenv(ENV_CHAT_ID, "12345")
        with pytest.raises(TelegramConfigError, match=ENV_BOT_TOKEN):
            TelegramConfig.from_env()

    def test_enabled_true_requires_chat_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_ENABLED, "true")
        monkeypatch.setenv(ENV_BOT_TOKEN, "bot-token")
        monkeypatch.delenv(ENV_CHAT_ID, raising=False)
        with pytest.raises(TelegramConfigError, match=ENV_CHAT_ID):
            TelegramConfig.from_env()

    def test_enabled_true_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_ENABLED, "true")
        monkeypatch.setenv(ENV_BOT_TOKEN, "bot-token")
        monkeypatch.setenv(ENV_CHAT_ID, "12345")
        config = TelegramConfig.from_env()
        assert config.enabled is True
        assert config.bot_token == "bot-token"
        assert config.chat_id == "12345"


# ────────────────────────────────────────────────────────────────────────
# TelegramBucket dedupe logic
# ────────────────────────────────────────────────────────────────────────


class TestBucketDedupe:
    def test_no_dedupe_window_never_dedupes(self) -> None:
        """RUN_START has dedupe_window=0 so every call passes through."""
        bucket = TelegramBucket()
        with bucket._lock:
            assert not bucket.check_dedupe_locked(TelegramEventKind.RUN_START, "", 1000.0)
            assert not bucket.check_dedupe_locked(TelegramEventKind.RUN_START, "", 1000.1)

    def test_dedupe_within_window(self) -> None:
        """First HEARTBEAT at t=1000 is unique; second at t=1005 is dup."""
        bucket = TelegramBucket()
        with bucket._lock:
            assert not bucket.check_dedupe_locked(TelegramEventKind.RUN_HEARTBEAT, "", 1000.0)
            assert bucket.check_dedupe_locked(TelegramEventKind.RUN_HEARTBEAT, "", 1005.0)

    def test_dedupe_outside_window_passes(self) -> None:
        """HEARTBEAT window is 600s. A second call 601s later should pass."""
        bucket = TelegramBucket()
        with bucket._lock:
            assert not bucket.check_dedupe_locked(TelegramEventKind.RUN_HEARTBEAT, "", 1000.0)
            assert not bucket.check_dedupe_locked(TelegramEventKind.RUN_HEARTBEAT, "", 1601.0)

    def test_different_dedupe_keys_not_conflated(self) -> None:
        """QUARANTINE_NEW dedupes per item_id, not globally."""
        bucket = TelegramBucket()
        with bucket._lock:
            assert not bucket.check_dedupe_locked(
                TelegramEventKind.QUARANTINE_NEW, "item-A", 1000.0
            )
            assert not bucket.check_dedupe_locked(
                TelegramEventKind.QUARANTINE_NEW, "item-B", 1000.0
            )

    def test_evict_expired(self) -> None:
        bucket = TelegramBucket()
        with bucket._lock:
            bucket._dedupe_cache[("kind-x", "key-x")] = 1000.0
            bucket.evict_expired_dedupe_locked(1000.0 + 10_000.0)
            assert ("kind-x", "key-x") not in bucket._dedupe_cache


# ────────────────────────────────────────────────────────────────────────
# TelegramNotifier (sync mode)
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def enabled_config() -> TelegramConfig:
    return TelegramConfig(
        bot_token="test-token",
        chat_id="12345",
        enabled=True,
    )


@pytest.fixture
def disabled_config() -> TelegramConfig:
    return TelegramConfig(
        bot_token="",
        chat_id="",
        enabled=False,
    )


class RecordingTransport:
    """Inline transport used by sync-mode tests."""

    def __init__(self, *, return_value: bool = True) -> None:
        self.return_value = return_value
        self.sent: list[TelegramEvent] = []
        self.call_count = 0

    def __call__(self, config: TelegramConfig, event: TelegramEvent) -> bool:
        self.call_count += 1
        self.sent.append(event)
        return self.return_value


class TestNotifierPublish:
    def test_disabled_notifier_drops_events(self, disabled_config: TelegramConfig) -> None:
        transport = RecordingTransport()
        notifier = TelegramNotifier(
            config=disabled_config,
            transport=transport,
            synchronous=True,
        )
        notifier.publish(
            TelegramEvent(
                kind=TelegramEventKind.RUN_START,
                severity=TelegramSeverity.INFO,
                message="hi",
            )
        )
        assert transport.call_count == 0
        assert notifier.stats()["dropped_on_disabled"] == 1

    def test_enabled_success(self, enabled_config: TelegramConfig) -> None:
        transport = RecordingTransport(return_value=True)
        notifier = TelegramNotifier(
            config=enabled_config,
            transport=transport,
            synchronous=True,
        )
        notifier.publish(
            TelegramEvent(
                kind=TelegramEventKind.RUN_START,
                severity=TelegramSeverity.INFO,
                message="hi",
            )
        )
        assert transport.call_count == 1
        assert notifier.stats()["sent"] == 1
        assert notifier.stats()["dropped_on_failure"] == 0

    def test_transport_failure_drops_gracefully(self, enabled_config: TelegramConfig) -> None:
        transport = RecordingTransport(return_value=False)
        notifier = TelegramNotifier(
            config=enabled_config,
            transport=transport,
            synchronous=True,
        )
        # Publish should not raise
        notifier.publish(
            TelegramEvent(
                kind=TelegramEventKind.RUN_START,
                severity=TelegramSeverity.INFO,
                message="hi",
            )
        )
        assert notifier.stats()["dropped_on_failure"] == 1

    def test_transport_exception_drops_gracefully(self, enabled_config: TelegramConfig) -> None:
        def bad_transport(config: TelegramConfig, event: TelegramEvent) -> bool:
            raise RuntimeError("simulated network error")

        notifier = TelegramNotifier(
            config=enabled_config,
            transport=bad_transport,
            synchronous=True,
        )
        # Publish should not raise
        notifier.publish(
            TelegramEvent(
                kind=TelegramEventKind.RUN_START,
                severity=TelegramSeverity.INFO,
                message="hi",
            )
        )
        assert notifier.stats()["dropped_on_failure"] == 1

    def test_dedupe_drops_duplicates(self, enabled_config: TelegramConfig) -> None:
        transport = RecordingTransport()
        notifier = TelegramNotifier(
            config=enabled_config,
            transport=transport,
            synchronous=True,
        )
        # Heartbeats dedupe on empty key within 600s
        for _ in range(5):
            notifier.publish(
                TelegramEvent(
                    kind=TelegramEventKind.RUN_HEARTBEAT,
                    severity=TelegramSeverity.INFO,
                    message="♥",
                )
            )
        # Only the first one gets through
        assert transport.call_count == 1
        assert notifier.stats()["sent"] == 1
        assert notifier.stats()["dropped_on_dedupe"] == 4

    def test_different_dedupe_keys_all_pass(self, enabled_config: TelegramConfig) -> None:
        transport = RecordingTransport()
        notifier = TelegramNotifier(
            config=enabled_config,
            transport=transport,
            synchronous=True,
        )
        for i in range(5):
            notifier.publish(
                TelegramEvent(
                    kind=TelegramEventKind.QUARANTINE_NEW,
                    severity=TelegramSeverity.ERROR,
                    message=f"🚨 item-{i}",
                    dedupe_key=f"item-{i}",
                )
            )
        assert transport.call_count == 5

    def test_never_raises_even_when_all_paths_fail(self, enabled_config: TelegramConfig) -> None:
        """Per FR-ING-016: Telegram must never block the ingest."""

        def terrible_transport(config: TelegramConfig, event: TelegramEvent) -> bool:
            raise SystemError("catastrophic")

        notifier = TelegramNotifier(
            config=enabled_config,
            transport=terrible_transport,
            synchronous=True,
        )
        # 100 publishes of chaos should not propagate any exception
        for _ in range(100):
            notifier.publish(
                TelegramEvent(
                    kind=TelegramEventKind.RUN_START,
                    severity=TelegramSeverity.INFO,
                    message="hi",
                )
            )
        assert notifier.stats()["dropped_on_failure"] == 100


# ────────────────────────────────────────────────────────────────────────
# Async mode (background thread) — smoke test
# ────────────────────────────────────────────────────────────────────────


class TestNotifierAsync:
    def test_start_stop(self, enabled_config: TelegramConfig) -> None:
        transport = RecordingTransport()
        notifier = TelegramNotifier.start(
            config=enabled_config,
            transport=transport,
        )
        try:
            notifier.publish(
                TelegramEvent(
                    kind=TelegramEventKind.RUN_START,
                    severity=TelegramSeverity.INFO,
                    message="hi",
                )
            )
            notifier.drain(timeout=1.0)
            assert transport.call_count == 1
        finally:
            notifier.stop(timeout=1.0)

    def test_context_manager(self, enabled_config: TelegramConfig) -> None:
        transport = RecordingTransport()
        with TelegramNotifier(
            config=enabled_config,
            transport=transport,
        ) as notifier:
            notifier.publish(
                TelegramEvent(
                    kind=TelegramEventKind.RUN_START,
                    severity=TelegramSeverity.INFO,
                    message="hi",
                )
            )
            notifier.drain(timeout=1.0)
        assert transport.call_count == 1
