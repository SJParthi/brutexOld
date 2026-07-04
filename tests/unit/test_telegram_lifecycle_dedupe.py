"""Notifier lifecycle + dedupe-before-send hardening tests (GROUP 4 · Task 5).

Covers two confirmed defects in ``brutex.notifications.telegram``:

* **R2-OCLI-F2 exit-drain race** — ``TelegramNotifier.stop()`` claimed to
  drain but flipped ``_running = False`` BEFORE enqueueing the poison pill,
  so the worker's ``while self._running`` check exited early and silently
  DROPPED queued events (the CRITICAL pull-halt alert / sweep_complete
  published moments before CLI exit). Fixed order: pill FIRST → join with
  timeout → THEN ``_running = False``.

* **R2-OCLI-F4 dedupe-before-send** — the dedupe last-seen stamp was written
  at PUBLISH time, before the send outcome, so one FAILED send suppressed
  every identical alert for the whole dedupe window. Fixed: the stamp is
  recorded only AFTER a successful send (a failed send leaves no stamp).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest
from brutex.notifications.telegram import (
    TelegramConfig,
    TelegramEvent,
    TelegramEventKind,
    TelegramNotifier,
    TelegramSeverity,
)


@pytest.fixture
def enabled_config() -> TelegramConfig:
    return TelegramConfig(bot_token="test-token", chat_id="12345", enabled=True)


def _event(kind: str = TelegramEventKind.RUN_START, key: str = "", msg: str = "x") -> TelegramEvent:
    return TelegramEvent(kind=kind, severity=TelegramSeverity.INFO, message=msg, dedupe_key=key)


class MutableTransport:
    """Transport whose outcome can be flipped between calls (thread-safe enough
    for the single-worker notifier)."""

    def __init__(self, *, return_value: bool = True, delay: float = 0.0) -> None:
        self.return_value = return_value
        self.delay = delay
        self.sent: list[TelegramEvent] = []
        self.running_flags: list[bool] = []
        self.notifier: TelegramNotifier | None = None

    def __call__(self, config: TelegramConfig, event: TelegramEvent) -> bool:
        if self.notifier is not None:
            # Record whether the notifier was still "running" DURING the send —
            # with pill-first ordering the drain happens while _running is True.
            self.running_flags.append(self.notifier._running)
        if self.delay:
            time.sleep(self.delay)
        self.sent.append(event)
        return self.return_value


# ────────────────────────────────────────────────────────────────────────
# Task 5(a) — stop() drains queued events before exit
# ────────────────────────────────────────────────────────────────────────


class TestStopDrains:
    def test_stop_drains_all_queued_events(self, enabled_config: TelegramConfig) -> None:
        """N events published just before stop() are ALL sent, none dropped.

        With the pre-fix ordering (_running=False before the pill) the worker
        exits its loop after at most the in-flight send and the rest die in
        the queue — this test fails there.
        """
        transport = MutableTransport(delay=0.01)
        notifier = TelegramNotifier.start(config=enabled_config, transport=transport)
        transport.notifier = notifier
        events = [_event(key=f"k{i}", msg=f"m{i}") for i in range(10)]
        for ev in events:
            notifier.publish(ev)
        notifier.stop(timeout=10.0)

        assert [e.message for e in transport.sent] == [f"m{i}" for i in range(10)]
        assert notifier.stats()["sent"] == 10
        assert notifier._worker is None
        assert notifier._running is False

    def test_stop_pill_first_running_stays_true_during_drain(
        self, enabled_config: TelegramConfig
    ) -> None:
        """Pill-first ordering: every drained send observes _running=True.

        The old order flipped ``_running`` False first, so sends performed
        during stop() observed False (when they happened at all).
        """
        transport = MutableTransport(delay=0.005)
        notifier = TelegramNotifier.start(config=enabled_config, transport=transport)
        transport.notifier = notifier
        for i in range(5):
            notifier.publish(_event(key=f"k{i}"))
        notifier.stop(timeout=10.0)

        assert len(transport.running_flags) == 5
        assert all(transport.running_flags)

    def test_stop_is_idempotent(self, enabled_config: TelegramConfig) -> None:
        """A second stop() is a no-op (early return on not-running)."""
        transport = MutableTransport()
        notifier = TelegramNotifier.start(config=enabled_config, transport=transport)
        notifier.stop(timeout=2.0)
        notifier.stop(timeout=2.0)  # must not raise / hang
        assert notifier._running is False

    def test_stop_full_queue_pill_suppressed_still_flips_running(
        self, enabled_config: TelegramConfig
    ) -> None:
        """A full queue (pill cannot enqueue) still ends with _running=False.

        Covers the ``queue.Full`` suppress branch: the worker will exit on its
        next ``while self._running`` check instead of via the pill.
        """
        notifier = TelegramNotifier(
            config=enabled_config, transport=MutableTransport(), synchronous=False
        )
        notifier._running = True
        notifier._worker = None
        notifier._queue = queue.Queue(maxsize=1)
        notifier._queue.put_nowait(_event())  # full — the pill put times out

        start = time.monotonic()
        notifier.stop(timeout=0.1)
        elapsed = time.monotonic() - start

        assert notifier._running is False
        assert elapsed < 5.0  # bounded: ~0.5 s pill timeout, no join (worker None)

    def test_worker_exits_via_running_false_when_pill_lost(
        self, enabled_config: TelegramConfig
    ) -> None:
        """After stop() flips _running False, a still-alive worker exits on its
        next loop check even without ever seeing a pill."""
        transport = MutableTransport()
        notifier = TelegramNotifier(config=enabled_config, transport=transport, synchronous=False)
        notifier._running = True
        worker = threading.Thread(target=notifier._worker_loop, daemon=True)
        worker.start()
        # Simulate a lost pill: flip the flag directly (what stop() guarantees last).
        notifier._running = False
        worker.join(timeout=5.0)
        assert not worker.is_alive()


# ────────────────────────────────────────────────────────────────────────
# Task 5(b) — dedupe stamped only AFTER a successful send
# ────────────────────────────────────────────────────────────────────────


class TestDedupeAfterSend:
    def test_failed_send_does_not_suppress_next_identical_alert(
        self, enabled_config: TelegramConfig
    ) -> None:
        """A FAILED send leaves no dedupe stamp — the next identical alert sends."""
        transport = MutableTransport(return_value=False)
        notifier = TelegramNotifier(config=enabled_config, transport=transport, synchronous=True)
        heartbeat = _event(kind=TelegramEventKind.RUN_HEARTBEAT)
        notifier.publish(heartbeat)
        assert notifier.stats()["dropped_on_failure"] == 1
        # Rollback semantics: nothing was stamped for the failed send.
        assert notifier._bucket._dedupe_cache == {}

        transport.return_value = True  # network recovered
        notifier.publish(_event(kind=TelegramEventKind.RUN_HEARTBEAT))
        assert notifier.stats()["sent"] == 1
        assert notifier.stats()["dropped_on_dedupe"] == 0

    def test_successful_send_dedupes_within_window(self, enabled_config: TelegramConfig) -> None:
        """After ONE successful send the identical alert IS deduped in-window."""
        transport = MutableTransport(return_value=True)
        notifier = TelegramNotifier(config=enabled_config, transport=transport, synchronous=True)
        notifier.publish(_event(kind=TelegramEventKind.RUN_HEARTBEAT))
        notifier.publish(_event(kind=TelegramEventKind.RUN_HEARTBEAT))
        assert notifier.stats()["sent"] == 1
        assert notifier.stats()["dropped_on_dedupe"] == 1
        assert (
            TelegramEventKind.RUN_HEARTBEAT,
            "",
        ) in notifier._bucket._dedupe_cache

    def test_transport_exception_does_not_stamp(self, enabled_config: TelegramConfig) -> None:
        """A raising transport counts as failure — no stamp, next alert sends."""

        calls = {"n": 0}

        def flaky(config: TelegramConfig, event: TelegramEvent) -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return True

        notifier = TelegramNotifier(config=enabled_config, transport=flaky, synchronous=True)
        notifier.publish(_event(kind=TelegramEventKind.RUN_HEARTBEAT))
        notifier.publish(_event(kind=TelegramEventKind.RUN_HEARTBEAT))
        assert notifier.stats()["dropped_on_failure"] == 1
        assert notifier.stats()["sent"] == 1

    def test_send_time_recheck_drops_racing_duplicate(self, enabled_config: TelegramConfig) -> None:
        """Two identical events queued before the first sends → one sent, one
        dropped by the send-time re-check (the race the after-send stamp opens
        is collapsed to at most the in-flight send)."""
        transport = MutableTransport(return_value=True)
        notifier = TelegramNotifier(config=enabled_config, transport=transport, synchronous=False)
        # Worker NOT started yet — both publishes pass the publish-time check
        # (no stamp exists) and land in the queue.
        notifier.publish(_event(kind=TelegramEventKind.RUN_HEARTBEAT))
        notifier.publish(_event(kind=TelegramEventKind.RUN_HEARTBEAT))
        assert notifier._queue.qsize() == 2

        notifier._start_worker()
        notifier.drain(timeout=5.0)
        notifier.stop(timeout=5.0)

        assert notifier.stats()["sent"] == 1
        assert notifier.stats()["dropped_on_dedupe"] == 1
        assert len(transport.sent) == 1


# ────────────────────────────────────────────────────────────────────────
# Task 5(a) — guarded drain-on-exit for fire-and-forget CLI alert paths
# ────────────────────────────────────────────────────────────────────────


class TestAtexitDrain:
    def test_start_registers_and_stop_unregisters_exit_hook(
        self, enabled_config: TelegramConfig
    ) -> None:
        """_start_worker registers a bounded stop() with atexit; stop removes it."""
        from brutex.notifications import telegram as telegram_mod

        with mock.patch.object(telegram_mod, "atexit") as fake_atexit:
            notifier = TelegramNotifier.start(config=enabled_config, transport=MutableTransport())
            fake_atexit.register.assert_called_once_with(notifier.stop)
            notifier.stop(timeout=2.0)
            fake_atexit.unregister.assert_called_once_with(notifier.stop)

    def test_synchronous_notifier_never_registers_exit_hook(
        self, enabled_config: TelegramConfig
    ) -> None:
        from brutex.notifications import telegram as telegram_mod

        with mock.patch.object(telegram_mod, "atexit") as fake_atexit:
            notifier = TelegramNotifier(
                config=enabled_config, transport=MutableTransport(), synchronous=True
            )
            notifier.publish(_event())
            notifier.stop()
        fake_atexit.register.assert_not_called()

    def test_events_published_without_stop_survive_process_exit(self, tmp_path: Path) -> None:
        """END-TO-END exit-drain (R2-OCLI-F2): a process that publishes and
        exits WITHOUT calling stop() — the pull-halt-alert shape — still
        delivers every queued event via the atexit-registered drain."""
        out = tmp_path / "sent.txt"
        script = (
            "import sys\n"
            "from brutex.notifications.telegram import (\n"
            "    TelegramConfig, TelegramEvent, TelegramEventKind,\n"
            "    TelegramNotifier, TelegramSeverity,\n"
            ")\n"
            "out = sys.argv[1]\n"
            "def transport(config, event):\n"
            "    with open(out, 'a', encoding='utf-8') as f:\n"
            "        f.write(event.message + '\\n')\n"
            "    return True\n"
            "config = TelegramConfig(bot_token='t', chat_id='c', enabled=True)\n"
            "n = TelegramNotifier.start(config=config, transport=transport)\n"
            "for i in range(5):\n"
            "    n.publish(TelegramEvent(\n"
            "        kind=TelegramEventKind.PHASE_BLOCKED,\n"
            "        severity=TelegramSeverity.CRITICAL,\n"
            "        message=f'm{i}', dedupe_key=f'k{i}',\n"
            "    ))\n"
            "# NO stop() — fire-and-forget exit; the atexit hook must drain.\n"
        )
        subprocess.run(
            [sys.executable, "-c", script, str(out)],
            check=True,
            timeout=60,
        )
        assert out.read_text(encoding="utf-8").splitlines() == [f"m{i}" for i in range(5)]
