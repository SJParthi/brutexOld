"""Unit tests for the live-rate panel sampler (DEC-PULL-RATE-WIDGET-VISIBILITY-001).

The sampler keeps the dashboard PULL RATE widget fed through EVERY pull phase — not just
the spot phase — by periodically pushing the SHARED rate controller's snapshot to the
panel via the existing ``make_rate_observer`` callback. These tests drive every branch of
:class:`~brutex.dashboard.rate_sampler.RatePanelSampler` deterministically (no real
threads, no real clock) with fakes for the session controller + observer + spawner.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from brutex.dashboard.rate_sampler import (
    DEFAULT_SAMPLE_INTERVAL_S,
    RatePanelSampler,
)


class _FakeController:
    """A stand-in :class:`RateController` returning a fixed snapshot mapping."""

    def __init__(self, snapshot: Mapping[str, object], *, raises: bool = False) -> None:
        self._snapshot = dict(snapshot)
        self._raises = raises
        self.snapshot_calls = 0

    def snapshot(self) -> dict[str, object]:
        self.snapshot_calls += 1
        if self._raises:
            raise RuntimeError("boom")
        return dict(self._snapshot)


class _FakeSession:
    """A stand-in :class:`SessionRateController` whose ``peek`` returns a fixed controller."""

    def __init__(self, controller: _FakeController | None) -> None:
        self._controller = controller
        self.peek_calls = 0

    def peek(self) -> _FakeController | None:
        self.peek_calls += 1
        return self._controller


class _CapturingObserver:
    """Capture every ``(controller_snapshot, meter_snapshot)`` pair the sampler pushes."""

    def __init__(self) -> None:
        self.calls: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []

    def __call__(self, rate: Mapping[str, Any], meter: Mapping[str, Any]) -> None:
        self.calls.append((rate, meter))


def _sampler(
    session: _FakeSession,
    observer: _CapturingObserver,
    *,
    spawn: Callable[[Callable[[], None]], None] | None = None,
    interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_S,
) -> RatePanelSampler:
    """Build a sampler with the test fakes (default spawn captures the loop body)."""
    return RatePanelSampler(
        session_rate=session,  # type: ignore[arg-type]
        observer=observer,  # type: ignore[arg-type]
        interval_seconds=interval_seconds,
        spawn=spawn if spawn is not None else (lambda _fn: None),
    )


class TestSampleOnce:
    def test_pushes_controller_snapshot_with_derived_achieved_rate(self) -> None:
        """A built controller → one observer push with effective_rps = recent_minute / 60."""
        controller = _FakeController(
            {"current_rps": 6.0, "recent_hits": 2, "recent_minute": 300, "ceiling_per_min": 500}
        )
        session = _FakeSession(controller)
        observer = _CapturingObserver()
        sampler = _sampler(session, observer)

        fired = sampler.sample_once()

        assert fired is True
        assert len(observer.calls) == 1
        rate, meter = observer.calls[0]
        # The controller snapshot is forwarded VERBATIM (recent_minute / ceiling_per_min etc.).
        assert rate["recent_minute"] == 300
        assert rate["current_rps"] == 6.0
        # Achieved r/s is the MEASURED rolling-minute count ÷ 60 (300 / 60 = 5.0).
        assert meter["effective_rps"] == pytest.approx(5.0)

    def test_no_controller_built_pushes_nothing(self) -> None:
        """A session with no built controller (dry-run / pre-pull) → no push, returns False."""
        session = _FakeSession(None)
        observer = _CapturingObserver()
        sampler = _sampler(session, observer)

        fired = sampler.sample_once()

        assert fired is False
        assert observer.calls == []
        assert session.peek_calls == 1  # it peeked, found nothing, pushed nothing

    def test_missing_recent_minute_defaults_to_zero_achieved(self) -> None:
        """A snapshot without recent_minute → 0 achieved r/s (no crash, honest zero)."""
        controller = _FakeController({"current_rps": 4.0})  # no recent_minute key
        observer = _CapturingObserver()
        sampler = _sampler(_FakeSession(controller), observer)

        assert sampler.sample_once() is True
        _rate, meter = observer.calls[0]
        assert meter["effective_rps"] == 0.0

    def test_snapshot_error_is_swallowed(self) -> None:
        """A raising controller snapshot is best-effort swallowed → returns False, no push."""
        controller = _FakeController({"recent_minute": 100}, raises=True)
        observer = _CapturingObserver()
        sampler = _sampler(_FakeSession(controller), observer)

        assert sampler.sample_once() is False
        assert observer.calls == []


class TestLifecycle:
    def test_start_spawns_the_drive_loop(self) -> None:
        """start() clears the stop flag and hands the drive body to the spawner."""
        spawned: list[Callable[[], None]] = []
        session = _FakeSession(_FakeController({"recent_minute": 60}))
        observer = _CapturingObserver()
        sampler = _sampler(session, observer, spawn=spawned.append)

        sampler.start()

        assert len(spawned) == 1
        # Running the captured body once (stop set first) must exit immediately without a tick.
        sampler.stop()
        spawned[0]()
        assert observer.calls == []

    def test_drive_loop_ticks_until_stopped(self) -> None:
        """The drive loop calls sample_once each interval and exits when stopped."""
        session = _FakeSession(_FakeController({"recent_minute": 120}))
        observer = _CapturingObserver()
        # Tiny interval so the real Event.wait returns quickly; stop after the first tick.
        sampler = _sampler(session, observer, interval_seconds=0.01)

        def _stop_after_first() -> None:
            # Patch sample_once to stop the loop the first time it actually fires.
            original = sampler.sample_once

            def _wrapped() -> bool:
                result = original()
                sampler.stop()
                return result

            sampler.sample_once = _wrapped  # type: ignore[method-assign]

        _stop_after_first()
        sampler.start()  # default spawn is a no-op here → drive the loop inline
        sampler._drive()

        assert len(observer.calls) == 1

    def test_context_manager_starts_and_stops(self) -> None:
        """The context manager starts the loop on enter and stops it on exit."""
        spawned: list[Callable[[], None]] = []
        session = _FakeSession(_FakeController({"recent_minute": 90}))
        observer = _CapturingObserver()
        sampler = _sampler(session, observer, spawn=spawned.append)

        with sampler as entered:
            assert entered is sampler
            assert len(spawned) == 1
            assert not sampler._stop.is_set()
        assert sampler._stop.is_set()

    def test_default_spawn_runs_on_a_daemon_thread(self) -> None:
        """The production default spawn launches a daemon thread that the loop exits cleanly."""
        session = _FakeSession(_FakeController({"recent_minute": 60}))
        observer = _CapturingObserver()
        # No spawn override → the real _daemon_spawn; tiny interval so it ticks promptly.
        sampler = RatePanelSampler(
            session_rate=session,  # type: ignore[arg-type]
            observer=observer,  # type: ignore[arg-type]
            interval_seconds=0.01,
        )
        sampler.start()
        # Give the daemon a moment to fire at least one tick, then stop + let it drain.
        deadline = threading.Event()
        deadline.wait(0.1)
        sampler.stop()
        deadline.wait(0.05)
        assert len(observer.calls) >= 1
