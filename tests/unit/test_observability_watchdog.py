"""Coverage tests for :mod:`brutex.observability.watchdog` — the
hung-worker detector closing the 2026-05-25 audit row 17 (watchdog
was MISSING).

Per DEC-WATCHDOG-001 (MIGRATION entry #240), this is the pure
detector — caller-supplied callbacks decide the sink. Tests use a
fake clock (no `time.sleep`) so the suite is fast + deterministic.
100% line + 100% branch coverage discipline.
"""

from __future__ import annotations

import logging

import pytest
from brutex.observability.watchdog import (
    Watchdog,
    WorkerEvent,
    WorkerState,
    make_heartbeat_subscriber,
)

# ────────────────────────────────────────────────────────────────────────
# Fake-clock helper
# ────────────────────────────────────────────────────────────────────────


class FakeClock:
    """Mutable monotonic clock for deterministic tests."""

    def __init__(self, initial: float = 0.0) -> None:
        self._now = initial

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


_HUNG_LOG: list[WorkerEvent] = []
_RECOVERY_LOG: list[WorkerEvent] = []


def _on_hung(event: WorkerEvent) -> None:
    _HUNG_LOG.append(event)


def _on_recovery(event: WorkerEvent) -> None:
    _RECOVERY_LOG.append(event)


@pytest.fixture(autouse=True)
def _clear_logs() -> None:
    _HUNG_LOG.clear()
    _RECOVERY_LOG.clear()


def _make_watchdog(
    *,
    timeout_seconds: float = 10.0,
    clock: FakeClock | None = None,
) -> Watchdog:
    return Watchdog(
        timeout_seconds=timeout_seconds,
        on_hung=_on_hung,
        on_recovery=_on_recovery,
        clock=clock or FakeClock(),
    )


# ────────────────────────────────────────────────────────────────────────
# Construction
# ────────────────────────────────────────────────────────────────────────


class TestConstruction:
    def test_rejects_zero_timeout(self) -> None:
        with pytest.raises(ValueError, match="must be > 0"):
            _make_watchdog(timeout_seconds=0)

    def test_rejects_negative_timeout(self) -> None:
        with pytest.raises(ValueError, match="must be > 0"):
            _make_watchdog(timeout_seconds=-1.0)

    def test_no_workers_initially(self) -> None:
        wd = _make_watchdog()
        assert wd.workers() == ()


# ────────────────────────────────────────────────────────────────────────
# Registration + heartbeat + state
# ────────────────────────────────────────────────────────────────────────


class TestRegistrationAndBeat:
    def test_register_sets_state_to_healthy(self) -> None:
        wd = _make_watchdog()
        wd.register("w1")
        assert wd.state_of("w1") == WorkerState.HEALTHY
        assert wd.workers() == ("w1",)

    def test_beat_auto_registers(self) -> None:
        wd = _make_watchdog()
        wd.beat("w1")
        assert wd.state_of("w1") == WorkerState.HEALTHY
        assert wd.workers() == ("w1",)

    def test_state_of_unknown_raises(self) -> None:
        wd = _make_watchdog()
        with pytest.raises(KeyError, match="No worker"):
            wd.state_of("ghost")

    def test_workers_returned_sorted(self) -> None:
        wd = _make_watchdog()
        wd.register("zeta")
        wd.register("alpha")
        wd.register("mu")
        assert wd.workers() == ("alpha", "mu", "zeta")


# ────────────────────────────────────────────────────────────────────────
# check_once — transitions + hysteresis
# ────────────────────────────────────────────────────────────────────────


class TestCheckOnceTransitions:
    def test_no_event_when_workers_recent_beat(self) -> None:
        clk = FakeClock(0.0)
        wd = _make_watchdog(timeout_seconds=10.0, clock=clk)
        wd.beat("w1")
        clk.advance(5.0)  # well under timeout
        emitted = wd.check_once()
        assert emitted == ()
        assert _HUNG_LOG == []

    def test_hung_event_fires_when_beat_older_than_timeout(self) -> None:
        clk = FakeClock(0.0)
        wd = _make_watchdog(timeout_seconds=10.0, clock=clk)
        wd.beat("w1")
        clk.advance(15.0)
        emitted = wd.check_once()
        assert len(emitted) == 1
        assert emitted[0].new_state == WorkerState.HUNG
        assert emitted[0].worker_name == "w1"
        assert emitted[0].last_beat_seconds_ago == 15.0
        assert emitted[0].timeout_seconds == 10.0
        assert len(_HUNG_LOG) == 1

    def test_steady_hung_fires_only_once(self) -> None:
        clk = FakeClock(0.0)
        wd = _make_watchdog(timeout_seconds=10.0, clock=clk)
        wd.beat("w1")
        clk.advance(15.0)
        wd.check_once()  # fires HUNG
        clk.advance(5.0)  # still hung
        emitted = wd.check_once()
        assert emitted == ()
        assert len(_HUNG_LOG) == 1

    def test_recovery_fires_on_beat_after_hung(self) -> None:
        clk = FakeClock(0.0)
        wd = _make_watchdog(timeout_seconds=10.0, clock=clk)
        wd.beat("w1")
        clk.advance(15.0)
        wd.check_once()  # HUNG fires
        clk.advance(1.0)
        wd.beat("w1")  # heartbeat returns
        emitted = wd.check_once()
        assert len(emitted) == 1
        assert emitted[0].new_state == WorkerState.HEALTHY
        assert len(_RECOVERY_LOG) == 1

    def test_oscillation_emits_alternating_edges(self) -> None:
        clk = FakeClock(0.0)
        wd = _make_watchdog(timeout_seconds=10.0, clock=clk)
        wd.beat("w1")
        for _cycle in range(3):
            clk.advance(15.0)
            wd.check_once()  # HUNG
            wd.beat("w1")  # recovers
            wd.check_once()
        assert len(_HUNG_LOG) == 3
        assert len(_RECOVERY_LOG) == 3

    def test_multiple_workers_tracked_independently(self) -> None:
        clk = FakeClock(0.0)
        wd = _make_watchdog(timeout_seconds=10.0, clock=clk)
        wd.beat("alive")
        wd.beat("hung")
        clk.advance(15.0)
        wd.beat("alive")  # one keeps beating; one stays silent
        emitted = wd.check_once()
        assert len(emitted) == 1
        assert emitted[0].worker_name == "hung"

    def test_register_then_no_beat_triggers_hung(self) -> None:
        clk = FakeClock(0.0)
        wd = _make_watchdog(timeout_seconds=10.0, clock=clk)
        wd.register("w1")
        clk.advance(15.0)
        emitted = wd.check_once()
        assert len(emitted) == 1
        assert emitted[0].new_state == WorkerState.HUNG


# ────────────────────────────────────────────────────────────────────────
# Callback failure isolation
# ────────────────────────────────────────────────────────────────────────


class TestCallbackFailuresIsolated:
    @pytest.mark.parametrize("exc_cls", [OSError, ValueError, RuntimeError])
    def test_on_hung_callback_exception_does_not_propagate(
        self,
        exc_cls: type[BaseException],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _bad(_event: WorkerEvent) -> None:
            raise exc_cls("synthetic")

        clk = FakeClock(0.0)
        wd = Watchdog(
            timeout_seconds=10.0,
            on_hung=_bad,
            on_recovery=_on_recovery,
            clock=clk,
        )
        wd.beat("w1")
        clk.advance(15.0)
        with caplog.at_level(logging.ERROR, logger="brutex.observability.watchdog"):
            emitted = wd.check_once()
        # State still transitioned even though callback failed
        assert len(emitted) == 1
        assert "callback raised" in caplog.text

    @pytest.mark.parametrize("exc_cls", [OSError, ValueError, RuntimeError])
    def test_on_recovery_callback_exception_does_not_propagate(
        self,
        exc_cls: type[BaseException],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def _bad(_event: WorkerEvent) -> None:
            raise exc_cls("synthetic")

        clk = FakeClock(0.0)
        wd = Watchdog(
            timeout_seconds=10.0,
            on_hung=_on_hung,
            on_recovery=_bad,
            clock=clk,
        )
        wd.beat("w1")
        clk.advance(15.0)
        wd.check_once()
        wd.beat("w1")
        with caplog.at_level(logging.ERROR, logger="brutex.observability.watchdog"):
            wd.check_once()
        assert "callback raised" in caplog.text


# ────────────────────────────────────────────────────────────────────────
# make_heartbeat_subscriber pipeline-integration helper
# ────────────────────────────────────────────────────────────────────────


class TestMakeHeartbeatSubscriber:
    def test_returns_callable(self) -> None:
        wd = _make_watchdog()
        subscriber = make_heartbeat_subscriber(wd, "w1")
        assert callable(subscriber)

    def test_calling_subscriber_records_heartbeat(self) -> None:
        clk = FakeClock(0.0)
        wd = _make_watchdog(clock=clk)
        subscriber = make_heartbeat_subscriber(wd, "w1")
        # Before any beat, w1 is unknown
        assert "w1" not in wd.workers()
        # Calling the subscriber with any "event" payload beats w1
        subscriber(object())
        assert wd.state_of("w1") == WorkerState.HEALTHY
        assert "w1" in wd.workers()

    def test_repeated_calls_keep_worker_healthy(self) -> None:
        clk = FakeClock(0.0)
        wd = _make_watchdog(timeout_seconds=10.0, clock=clk)
        subscriber = make_heartbeat_subscriber(wd, "w1")
        for _i in range(5):
            clk.advance(3.0)
            subscriber(object())
            assert wd.check_once() == ()  # never trips
        assert wd.state_of("w1") == WorkerState.HEALTHY

    def test_subscriber_for_different_worker_names_are_independent(self) -> None:
        wd = _make_watchdog()
        sub_a = make_heartbeat_subscriber(wd, "alpha")
        sub_b = make_heartbeat_subscriber(wd, "beta")
        sub_a(object())
        sub_b(object())
        assert wd.workers() == ("alpha", "beta")

    def test_no_beat_eventually_trips_hung(self) -> None:
        """If the subscriber is never called, the worker is never beaten,
        so once it's been registered the watchdog can flag it. This test
        validates the subscriber + register interaction."""
        clk = FakeClock(0.0)
        wd = _make_watchdog(timeout_seconds=10.0, clock=clk)
        # Call subscriber once to register
        subscriber = make_heartbeat_subscriber(wd, "w1")
        subscriber(object())
        # Then advance time WITHOUT further beats
        clk.advance(15.0)
        emitted = wd.check_once()
        assert len(emitted) == 1
        assert emitted[0].worker_name == "w1"
        assert emitted[0].new_state == WorkerState.HUNG
