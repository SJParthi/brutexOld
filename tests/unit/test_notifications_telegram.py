"""Unit tests for brutex.notifications.telegram.

All tests use synchronous mode + injected transport — zero real
network calls, zero thread races.

Per DEC-P0-017 (PR #39): this is a WHITEBOX test suite for the dedupe
TokenBucket internals (`_lock`, `_dedupe_cache`). The file-level
`reportPrivateUsage=none` directive below is intentional — we
deliberately probe the private state to verify mutex acquisition order
and cache invalidation timing. Production code reaches these symbols
ONLY via the public surface; only this test file is permitted whitebox
access.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import queue
import urllib.error
from unittest.mock import MagicMock, patch

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
        """A recorded HEARTBEAT at t=1000 makes a check at t=1005 a dup.

        Since the dedupe-before-send fix (R2-OCLI-F4) the check is PURE —
        the stamp is written by record_dedupe_locked after a successful send.
        """
        bucket = TelegramBucket()
        with bucket._lock:
            assert not bucket.check_dedupe_locked(TelegramEventKind.RUN_HEARTBEAT, "", 1000.0)
            bucket.record_dedupe_locked(TelegramEventKind.RUN_HEARTBEAT, "", 1000.0)
            assert bucket.check_dedupe_locked(TelegramEventKind.RUN_HEARTBEAT, "", 1005.0)

    def test_check_is_pure_without_record(self) -> None:
        """check_dedupe_locked alone never stamps — repeated checks all pass."""
        bucket = TelegramBucket()
        with bucket._lock:
            assert not bucket.check_dedupe_locked(TelegramEventKind.RUN_HEARTBEAT, "", 1000.0)
            assert not bucket.check_dedupe_locked(TelegramEventKind.RUN_HEARTBEAT, "", 1005.0)
            assert bucket._dedupe_cache == {}

    def test_dedupe_outside_window_passes(self) -> None:
        """HEARTBEAT window is 600s. A second call 601s later should pass."""
        bucket = TelegramBucket()
        with bucket._lock:
            assert not bucket.check_dedupe_locked(TelegramEventKind.RUN_HEARTBEAT, "", 1000.0)
            bucket.record_dedupe_locked(TelegramEventKind.RUN_HEARTBEAT, "", 1000.0)
            assert not bucket.check_dedupe_locked(TelegramEventKind.RUN_HEARTBEAT, "", 1601.0)

    def test_different_dedupe_keys_not_conflated(self) -> None:
        """QUARANTINE_NEW dedupes per item_id, not globally."""
        bucket = TelegramBucket()
        with bucket._lock:
            assert not bucket.check_dedupe_locked(
                TelegramEventKind.QUARANTINE_NEW, "item-A", 1000.0
            )
            bucket.record_dedupe_locked(TelegramEventKind.QUARANTINE_NEW, "item-A", 1000.0)
            assert not bucket.check_dedupe_locked(
                TelegramEventKind.QUARANTINE_NEW, "item-B", 1000.0
            )

    def test_record_skips_zero_window_kind(self) -> None:
        """record_dedupe_locked no-ops for a zero-window kind (cache stays bounded)."""
        bucket = TelegramBucket()
        with bucket._lock:
            bucket.record_dedupe_locked(TelegramEventKind.RUN_START, "", 1000.0)
            assert bucket._dedupe_cache == {}

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


# ────────────────────────────────────────────────────────────────────────
# DEC-COV-001 step 17 — surgical backfill of brutex.notifications.telegram
# Covers the missing lines/branches identified by --cov-report=term-missing:
#   - _default_transport (308-339): HTTP success / HTTPError / URLError / OSError / disabled
#   - _start_worker early-return when already running (402)
#   - stop early-return when not running (414) + 419→exit branch (worker None)
#   - __enter__ branch (424→426)
#   - publish queue.Full branch (463-466)
#   - _worker_loop queue.Empty branch + while-exit (472→exit, 475-476)
#   - drain synchronous early-return + queue-empty exit (519, 521→exit)
# ────────────────────────────────────────────────────────────────────────


class TestDefaultTransport:
    """Exercise the urllib-based default transport's success + error paths."""

    @pytest.fixture
    def transport_event(self) -> TelegramEvent:
        return TelegramEvent(
            kind=TelegramEventKind.RUN_START,
            severity=TelegramSeverity.INFO,
            message="hello",
        )

    def test_disabled_config_returns_false(
        self, disabled_config: TelegramConfig, transport_event: TelegramEvent
    ) -> None:
        """Covers line 308: ``if not config.enabled: return False``."""
        from brutex.notifications import telegram as telegram_mod

        assert (
            telegram_mod._default_transport(  # pyright: ignore[reportPrivateUsage]
                disabled_config, transport_event
            )
            is False
        )

    def test_http_200_returns_true(
        self, enabled_config: TelegramConfig, transport_event: TelegramEvent
    ) -> None:
        """Covers lines 326-330: successful HTTP 200 path."""
        from brutex.notifications import telegram as telegram_mod

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=None)
        with patch(
            "brutex.notifications.telegram.urllib.request.urlopen",
            return_value=mock_resp,
        ):
            assert (
                telegram_mod._default_transport(  # pyright: ignore[reportPrivateUsage]
                    enabled_config, transport_event
                )
                is True
            )

    def test_http_error_returns_false(
        self, enabled_config: TelegramConfig, transport_event: TelegramEvent
    ) -> None:
        """Covers lines 331-333: urllib.error.HTTPError caught → False."""
        from brutex.notifications import telegram as telegram_mod

        http_err = urllib.error.HTTPError(
            url="https://api.telegram.org/test",
            code=500,
            msg="Internal Server Error",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )
        with patch(
            "brutex.notifications.telegram.urllib.request.urlopen",
            side_effect=http_err,
        ):
            assert (
                telegram_mod._default_transport(  # pyright: ignore[reportPrivateUsage]
                    enabled_config, transport_event
                )
                is False
            )

    def test_url_error_returns_false(
        self, enabled_config: TelegramConfig, transport_event: TelegramEvent
    ) -> None:
        """Covers lines 334-336: urllib.error.URLError caught → False."""
        from brutex.notifications import telegram as telegram_mod

        with patch(
            "brutex.notifications.telegram.urllib.request.urlopen",
            side_effect=urllib.error.URLError("name resolution failed"),
        ):
            assert (
                telegram_mod._default_transport(  # pyright: ignore[reportPrivateUsage]
                    enabled_config, transport_event
                )
                is False
            )

    def test_os_error_returns_false(
        self, enabled_config: TelegramConfig, transport_event: TelegramEvent
    ) -> None:
        """Covers lines 337-339: OSError / TimeoutError caught → False."""
        from brutex.notifications import telegram as telegram_mod

        with patch(
            "brutex.notifications.telegram.urllib.request.urlopen",
            side_effect=OSError("connection refused"),
        ):
            assert (
                telegram_mod._default_transport(  # pyright: ignore[reportPrivateUsage]
                    enabled_config, transport_event
                )
                is False
            )


class TestNotifierLifecycle:
    """Exercise the worker thread start/stop + queue-management edges."""

    def test_start_worker_no_op_when_already_running(self, enabled_config: TelegramConfig) -> None:
        """Covers line 402: ``if self._synchronous or self._running: return``."""
        notifier = TelegramNotifier(
            config=enabled_config,
            transport=RecordingTransport().__call__,
            synchronous=False,
        )
        try:
            notifier._start_worker()  # pyright: ignore[reportPrivateUsage]
            first_thread = notifier._worker  # pyright: ignore[reportPrivateUsage]
            assert first_thread is not None
            # Second call should be a no-op — worker stays the same instance.
            notifier._start_worker()  # pyright: ignore[reportPrivateUsage]
            assert notifier._worker is first_thread  # pyright: ignore[reportPrivateUsage]
        finally:
            notifier.stop(timeout=0.5)

    def test_stop_no_op_when_not_running(self, enabled_config: TelegramConfig) -> None:
        """Covers line 414: ``if not self._running: return``."""
        notifier = TelegramNotifier(
            config=enabled_config,
            transport=RecordingTransport().__call__,
            synchronous=True,  # synchronous → _running stays False
        )
        notifier.stop()  # must not raise

    def test_stop_when_worker_is_none(self, enabled_config: TelegramConfig) -> None:
        """Covers 419→exit: worker is None, skip the join block."""
        notifier = TelegramNotifier(
            config=enabled_config,
            transport=RecordingTransport().__call__,
            synchronous=False,
        )
        # Force _running=True but _worker=None to exercise that exit branch.
        notifier._running = True  # pyright: ignore[reportPrivateUsage]
        notifier._worker = None  # pyright: ignore[reportPrivateUsage]
        notifier.stop()  # walks the `if self._worker is not None:` False branch
        assert notifier._running is False  # pyright: ignore[reportPrivateUsage]

    def test_context_manager_no_op_when_already_running(
        self, enabled_config: TelegramConfig
    ) -> None:
        """Covers 424→426: __enter__ skips _start_worker when already running."""
        notifier = TelegramNotifier(
            config=enabled_config,
            transport=RecordingTransport().__call__,
            synchronous=False,
        )
        try:
            notifier._start_worker()  # pyright: ignore[reportPrivateUsage]
            # Now __enter__ is called → False branch (synchronous=False but
            # running=True) → skips _start_worker and returns self.
            assert notifier.__enter__() is notifier
        finally:
            notifier.stop(timeout=0.5)


class TestNotifierPublishFull:
    """Exercise the publish queue.Full path + worker loop queue.Empty path."""

    def test_publish_increments_queue_full_counter(self, enabled_config: TelegramConfig) -> None:
        """Covers lines 463-466: queue.Full path increments dropped_on_queue_full."""
        notifier = TelegramNotifier(
            config=enabled_config,
            transport=RecordingTransport().__call__,
            synchronous=False,
        )
        try:
            # Replace the internal queue with a zero-capacity (always-full) one.
            notifier._queue = queue.Queue(maxsize=1)  # pyright: ignore[reportPrivateUsage]
            # Fill it.
            notifier._queue.put_nowait(  # pyright: ignore[reportPrivateUsage]
                TelegramEvent(
                    kind=TelegramEventKind.RUN_START,
                    severity=TelegramSeverity.INFO,
                    message="filler",
                )
            )
            # This publish triggers queue.Full.
            notifier.publish(
                TelegramEvent(
                    kind=TelegramEventKind.PHASE_PROGRESS,
                    severity=TelegramSeverity.INFO,
                    message="should be dropped",
                )
            )
            assert notifier._bucket.dropped_on_queue_full == 1  # pyright: ignore[reportPrivateUsage]
        finally:
            notifier.stop(timeout=0.5)


class TestWorkerLoopAndDrain:
    """Exercise the worker loop's queue.Empty branch + drain's edge cases."""

    def test_worker_loop_handles_queue_empty(self, enabled_config: TelegramConfig) -> None:
        """Covers lines 472→exit + 475-476: queue.Empty triggers continue,
        then the next iteration's while-check exits when running goes False.

        Tested via the regular start/stop lifecycle — the daemon worker
        runs with a 0.5s queue.get(timeout=0.5), and when no events are
        published, the queue.Empty exception fires the `continue` branch.
        We just need to keep the notifier alive long enough for ONE
        timeout cycle.
        """
        notifier = TelegramNotifier(
            config=enabled_config,
            transport=RecordingTransport().__call__,
            synchronous=False,
        )
        try:
            notifier._start_worker()  # pyright: ignore[reportPrivateUsage]
            # No publish — let the worker hit at least one queue.Empty.
            import time as _time

            _time.sleep(0.6)  # > queue.get(timeout=0.5)
        finally:
            notifier.stop(timeout=1.0)
        # Worker should have exited cleanly.
        assert notifier._running is False  # pyright: ignore[reportPrivateUsage]

    def test_drain_no_op_when_synchronous(self, enabled_config: TelegramConfig) -> None:
        """Covers line 519: ``if self._synchronous: return``."""
        notifier = TelegramNotifier(
            config=enabled_config,
            transport=RecordingTransport().__call__,
            synchronous=True,
        )
        # No-op + must not raise.
        notifier.drain(timeout=1.0)

    def test_drain_returns_when_queue_empties(self, enabled_config: TelegramConfig) -> None:
        """Covers 521→exit: when queue empties before timeout, drain returns."""
        notifier = TelegramNotifier(
            config=enabled_config,
            transport=RecordingTransport().__call__,
            synchronous=False,
        )
        try:
            notifier._start_worker()  # pyright: ignore[reportPrivateUsage]
            # Queue is already empty — drain should return immediately.
            notifier.drain(timeout=2.0)
        finally:
            notifier.stop(timeout=0.5)

    def test_worker_loop_exits_when_running_false_at_check(
        self, enabled_config: TelegramConfig
    ) -> None:
        """Covers 472→exit: while-condition False causes loop exit (vs break).

        Direct-call the loop with _running=False to exercise the while-False
        exit branch. Without this, the only exit path is via the ``break``
        statement (poison pill), and coverage flags 472→exit as partial.
        """
        notifier = TelegramNotifier(
            config=enabled_config,
            transport=RecordingTransport().__call__,
            synchronous=False,
        )
        notifier._running = False  # pyright: ignore[reportPrivateUsage]
        # Direct call — no thread, no queue interaction; loop immediately exits.
        notifier._worker_loop()  # pyright: ignore[reportPrivateUsage]

    def test_drain_exits_on_deadline(self, enabled_config: TelegramConfig) -> None:
        """Covers 521→exit: drain's while-condition False (deadline reached).

        Keep the queue non-empty so the ``if self._queue.empty(): return``
        early-return never fires; let the timeout elapse so the while-check
        on ``time.monotonic() < deadline`` goes False.
        """
        notifier = TelegramNotifier(
            config=enabled_config,
            transport=RecordingTransport().__call__,
            synchronous=False,
        )
        # Don't start the worker — that way nothing drains the queue.
        notifier._queue.put_nowait(  # pyright: ignore[reportPrivateUsage]
            TelegramEvent(
                kind=TelegramEventKind.RUN_START,
                severity=TelegramSeverity.INFO,
                message="never drained",
            )
        )
        # Tiny timeout so we exit via deadline, not via queue-empty.
        notifier.drain(timeout=0.05)
        # Queue still has the event — confirms the deadline-exit path fired.
        assert notifier._queue.qsize() == 1  # pyright: ignore[reportPrivateUsage]
