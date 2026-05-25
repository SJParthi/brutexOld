"""Coverage tests for :mod:`brutex.observability.monitor` — the
daemon-thread poller closing the operational gap from DEC-SLA-DETECTOR-001
+ DEC-WATCHDOG-001 + DEC-L3-REPLAY-ORACLE-001.

Per DEC-MONITOR-LOOP-001 (MIGRATION entry #242). Tests target 100%
line + 100% branch coverage. Threading tests use a real daemon thread
but with short poll intervals + explicit shutdown to keep the suite
fast + deterministic.
"""

from __future__ import annotations

import logging
import time

import pytest
from brutex.observability.monitor import MonitorLoop
from brutex.observability.sla import (
    SLADetector,
    SLAPredicate,
    SLARule,
)
from brutex.observability.watchdog import Watchdog

# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


_NOOP_SLA = lambda _event: None  # noqa: E731  test helper
_NOOP_WD = lambda _event: None  # noqa: E731  test helper


def _make_sla(provider_value: float, threshold: float = 100.0) -> SLADetector:
    return SLADetector(
        rules=(
            SLARule(
                name="r1",
                value_provider=lambda: provider_value,
                predicate=SLAPredicate.GREATER_THAN,
                threshold=threshold,
            ),
        ),
        on_breach=_NOOP_SLA,
        on_recovery=_NOOP_SLA,
    )


def _make_wd(*, timeout_seconds: float = 10.0) -> Watchdog:
    return Watchdog(
        timeout_seconds=timeout_seconds,
        on_hung=_NOOP_WD,
        on_recovery=_NOOP_WD,
    )


# ────────────────────────────────────────────────────────────────────────
# Construction validation
# ────────────────────────────────────────────────────────────────────────


class TestConstruction:
    def test_rejects_zero_poll_interval(self) -> None:
        with pytest.raises(ValueError, match="poll_interval_seconds must be > 0"):
            MonitorLoop(poll_interval_seconds=0)

    def test_rejects_negative_poll_interval(self) -> None:
        with pytest.raises(ValueError, match="poll_interval_seconds must be > 0"):
            MonitorLoop(poll_interval_seconds=-1.0)

    def test_rejects_zero_replay_cadence(self) -> None:
        with pytest.raises(ValueError, match="replay_check_every_n_ticks must be >= 1"):
            MonitorLoop(replay_check_every_n_ticks=0)

    def test_rejects_negative_replay_cadence(self) -> None:
        with pytest.raises(ValueError, match="replay_check_every_n_ticks must be >= 1"):
            MonitorLoop(replay_check_every_n_ticks=-1)

    def test_default_construction_all_none(self) -> None:
        loop = MonitorLoop()
        assert loop.sla_detector is None
        assert loop.watchdog is None
        assert loop.replay_check is None
        assert loop.is_running is False


# ────────────────────────────────────────────────────────────────────────
# tick_once — deterministic, no daemon thread
# ────────────────────────────────────────────────────────────────────────


class TestTickOnce:
    def test_no_detectors_is_safe_noop(self) -> None:
        loop = MonitorLoop()
        loop.tick_once()  # must not raise

    def test_sla_detector_check_once_is_called(self) -> None:
        sla = _make_sla(provider_value=150.0)
        loop = MonitorLoop(sla_detector=sla)
        # SLA at value=150 vs threshold=100 → breach on first tick
        loop.tick_once()
        assert sla.state_of("r1").value == "breach"

    def test_watchdog_check_once_is_called(self) -> None:
        wd = _make_wd(timeout_seconds=10.0)
        wd.register("w1")
        # Walk wall-clock past timeout
        time.sleep(0.01)
        loop = MonitorLoop(watchdog=wd, poll_interval_seconds=0.001)
        # Force the watchdog to think w1 is stale by manipulating its clock
        original_clock = wd.clock
        wd.clock = lambda: original_clock() + 100.0
        loop.tick_once()
        assert wd.state_of("w1").value == "hung"

    def test_replay_check_called_on_first_tick_by_default(self) -> None:
        calls: list[int] = []
        loop = MonitorLoop(replay_check=lambda: calls.append(1))
        loop.tick_once()
        assert calls == [1]

    def test_replay_check_skipped_when_cadence_not_aligned(self) -> None:
        calls: list[int] = []
        loop = MonitorLoop(
            replay_check=lambda: calls.append(1),
            replay_check_every_n_ticks=3,
        )
        loop.tick_once()  # tick 1: skip
        loop.tick_once()  # tick 2: skip
        assert calls == []
        loop.tick_once()  # tick 3: run
        assert calls == [1]

    @pytest.mark.parametrize("exc_cls", [OSError, ValueError, RuntimeError])
    def test_sla_check_once_exception_does_not_propagate(
        self,
        exc_cls: type[BaseException],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class _RaisingSLA:
            def check_once(self) -> None:
                raise exc_cls("synthetic")

        loop = MonitorLoop(sla_detector=_RaisingSLA())  # type: ignore[arg-type]
        with caplog.at_level(logging.ERROR, logger="brutex.observability.monitor"):
            loop.tick_once()
        assert "SLA detector check_once raised" in caplog.text

    @pytest.mark.parametrize("exc_cls", [OSError, ValueError, RuntimeError])
    def test_watchdog_check_once_exception_does_not_propagate(
        self,
        exc_cls: type[BaseException],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        class _RaisingWD:
            def check_once(self) -> None:
                raise exc_cls("synthetic")

        loop = MonitorLoop(watchdog=_RaisingWD())  # type: ignore[arg-type]
        with caplog.at_level(logging.ERROR, logger="brutex.observability.monitor"):
            loop.tick_once()
        assert "watchdog check_once raised" in caplog.text

    @pytest.mark.parametrize("exc_cls", [OSError, ValueError, RuntimeError])
    def test_replay_check_exception_does_not_propagate(
        self,
        exc_cls: type[BaseException],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _raises() -> None:
            raise exc_cls("synthetic")

        loop = MonitorLoop(replay_check=_raises)
        with caplog.at_level(logging.ERROR, logger="brutex.observability.monitor"):
            loop.tick_once()
        assert "replay_check raised" in caplog.text


# ────────────────────────────────────────────────────────────────────────
# Daemon-thread lifecycle
# ────────────────────────────────────────────────────────────────────────


class TestDaemonThreadLifecycle:
    def test_start_spawns_daemon_thread(self) -> None:
        loop = MonitorLoop(poll_interval_seconds=0.01)
        loop.start()
        try:
            # Brief settle window
            time.sleep(0.05)
            assert loop.is_running
        finally:
            loop.stop()

    def test_stop_joins_thread(self) -> None:
        loop = MonitorLoop(poll_interval_seconds=0.01)
        loop.start()
        time.sleep(0.05)
        loop.stop(timeout=2.0)
        assert loop.is_running is False

    def test_start_is_idempotent(self) -> None:
        """Calling start while already running is a no-op."""
        loop = MonitorLoop(poll_interval_seconds=0.01)
        loop.start()
        try:
            time.sleep(0.05)
            # Second start should not spawn a second thread
            loop.start()
            assert loop.is_running
        finally:
            loop.stop()

    def test_context_manager_starts_and_stops(self) -> None:
        loop = MonitorLoop(poll_interval_seconds=0.01)
        with loop:
            time.sleep(0.05)
            assert loop.is_running
        assert loop.is_running is False

    def test_thread_runs_tick_once_periodically(self) -> None:
        calls: list[int] = []
        loop = MonitorLoop(
            replay_check=lambda: calls.append(1),
            poll_interval_seconds=0.02,
        )
        loop.start()
        try:
            time.sleep(0.15)  # ~7 ticks worth
        finally:
            loop.stop()
        # Expect multiple ticks (allow some slack for scheduler jitter)
        assert len(calls) >= 3

    def test_stop_returns_quickly_via_event(self) -> None:
        """The loop should respond to stop() within one poll interval
        because it uses `Event.wait(timeout)` not `time.sleep`."""
        loop = MonitorLoop(poll_interval_seconds=5.0)  # long interval
        loop.start()
        start = time.monotonic()
        loop.stop(timeout=1.0)
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"stop() should return quickly; took {elapsed:.2f}s"

    def test_stop_without_start_is_safe(self) -> None:
        """Calling stop on a never-started loop is a no-op."""
        loop = MonitorLoop()
        loop.stop()  # must not raise
        assert loop.is_running is False
