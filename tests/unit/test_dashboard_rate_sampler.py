"""Unit tests for the live-rate panel sampler (DEC-PULL-RATE-WIDGET-VISIBILITY-001).

The sampler keeps the dashboard PULL RATE widget fed through EVERY pull phase — not just
the spot phase — by periodically pushing the SHARED rate controller's snapshot to the
panel via the existing ``make_rate_observer`` callback. These tests drive every branch of
:class:`~brutex.dashboard.rate_sampler.RatePanelSampler` deterministically (no real
threads, no real clock) with fakes for the session controller + observer + spawner.

DEC-RATE-WIDGET-FNO-MEASURED-001 extends the sampler: when the pull build sites have
REGISTERED the live client pool's shared transport ``CallMeter`` into the session
controller, ``sample_once`` reads it via ``peek_meter`` and pushes the meter's REAL
measured ``snapshot`` (``meter_present`` defaults ``True`` in the observer); when no meter
is registered it falls back to the honest ``{"meter_present": False}`` sentinel of
DEC-RATE-WIDGET-FNO-HONEST-001 (no fabricated ``effective_rps``).
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


class _FakeMeter:
    """A stand-in transport :class:`CallMeter` returning a fixed measured snapshot."""

    def __init__(self, snapshot: Mapping[str, object]) -> None:
        self._snapshot = dict(snapshot)
        self.snapshot_calls = 0

    def snapshot(self) -> dict[str, object]:
        self.snapshot_calls += 1
        return dict(self._snapshot)


class _FakeSession:
    """A stand-in :class:`SessionRateController` whose ``peek``/``peek_meter`` are fixed.

    ``peek`` returns a fixed controller (or ``None``); ``peek_meter`` returns a fixed
    meter (or ``None`` — the honest no-meter path). Both count their calls so a test can
    assert the sampler observed without building anything (DEC-RATE-WIDGET-FNO-MEASURED-001).
    """

    def __init__(
        self,
        controller: _FakeController | None,
        *,
        meter: _FakeMeter | None = None,
    ) -> None:
        self._controller = controller
        self._meter = meter
        self.peek_calls = 0
        self.peek_meter_calls = 0

    def peek(self) -> _FakeController | None:
        self.peek_calls += 1
        return self._controller

    def peek_meter(self) -> _FakeMeter | None:
        self.peek_meter_calls += 1
        return self._meter


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
    def test_no_meter_registered_pushes_honest_absent_sentinel(self) -> None:
        """A built controller but NO registered meter → NO fabricated effective_rps.

        DEC-RATE-WIDGET-FNO-HONEST-001 (the fallback path preserved by
        DEC-RATE-WIDGET-FNO-MEASURED-001): when no meter has been registered (a dry-run / a
        phase before the client pool exists), the sampler must NOT synthesize an achieved
        completed-rate from the controller's admission count. It pushes the controller
        snapshot VERBATIM + a meter snapshot carrying ONLY a ``meter_present=False`` sentinel
        (no ``effective_rps`` key), so the view keeps "achieved" an honest 0 this phase.
        """
        controller = _FakeController(
            {"current_rps": 6.0, "recent_hits": 2, "recent_minute": 300, "ceiling_per_min": 500}
        )
        session = _FakeSession(controller, meter=None)  # NO meter registered
        observer = _CapturingObserver()
        sampler = _sampler(session, observer)

        fired = sampler.sample_once()

        assert fired is True
        assert len(observer.calls) == 1
        rate, meter = observer.calls[0]
        # The controller snapshot is forwarded VERBATIM (recent_minute / ceiling_per_min etc.).
        assert rate["recent_minute"] == 300
        assert rate["current_rps"] == 6.0
        # NO fabricated completed-rate — the meter snapshot carries the honest absence sentinel
        # and never an ``effective_rps`` admission-derived figure.
        assert meter == {"meter_present": False}
        assert "effective_rps" not in meter
        assert session.peek_meter_calls == 1  # it looked for a meter, found none

    def test_registered_meter_pushes_measured_snapshot(self) -> None:
        """A REGISTERED meter → the sampler pushes the meter's REAL measured snapshot.

        DEC-RATE-WIDGET-FNO-MEASURED-001: once the pull build sites register the pool's
        shared ``CallMeter`` into the session controller, the sampler reads it via
        ``peek_meter`` and pushes its MEASURED ``snapshot`` (``effective_rps`` is the true
        completed rate, NOT the admission average). A genuine ``CallMeter`` snapshot carries
        no ``meter_present`` key, so the observer defaults it ``True`` downstream — this test
        asserts the sampler forwards the measured snapshot verbatim without a ``False`` sentinel.
        """
        controller = _FakeController({"current_rps": 8.0, "recent_minute": 204})
        meter = _FakeMeter(
            {"effective_rps": 2.82, "rps_1s": 3, "rps_1s_max": 5, "latency_ms_p95": 190.0}
        )
        session = _FakeSession(controller, meter=meter)
        observer = _CapturingObserver()
        sampler = _sampler(session, observer)

        fired = sampler.sample_once()

        assert fired is True
        rate, pushed_meter = observer.calls[0]
        assert rate["current_rps"] == 8.0
        # The MEASURED completed-rate is forwarded (2.82), NOT the admission avg (204/60 = 3.4).
        assert pushed_meter["effective_rps"] == 2.82
        assert pushed_meter["rps_1s"] == 3
        # No honest-absence sentinel — a real meter snapshot lets the observer default
        # ``meter_present=True`` (measured completed-rate present this phase).
        assert "meter_present" not in pushed_meter
        assert meter.snapshot_calls == 1

    def test_no_controller_built_pushes_nothing(self) -> None:
        """A session with no built controller (dry-run / pre-pull) → no push, returns False."""
        session = _FakeSession(None)
        observer = _CapturingObserver()
        sampler = _sampler(session, observer)

        fired = sampler.sample_once()

        assert fired is False
        assert observer.calls == []
        assert session.peek_calls == 1  # it peeked, found nothing, pushed nothing
        assert session.peek_meter_calls == 0  # short-circuited before looking for a meter

    def test_absent_sentinel_independent_of_recent_minute(self) -> None:
        """With no meter, the sentinel is pushed regardless of recent_minute (no admission rate).

        DEC-RATE-WIDGET-FNO-HONEST-001: even a snapshot WITH a large ``recent_minute`` yields
        no ``effective_rps`` when no meter is registered — the sampler never derives a
        completed-rate from admissions.
        """
        controller = _FakeController({"current_rps": 4.0, "recent_minute": 240})
        observer = _CapturingObserver()
        sampler = _sampler(_FakeSession(controller, meter=None), observer)

        assert sampler.sample_once() is True
        _rate, meter = observer.calls[0]
        assert meter == {"meter_present": False}

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

    def test_context_manager_stops_on_exception(self) -> None:
        """An exception raised in the with-body still stops the daemon + propagates.

        __exit__ never suppresses (it returns None), so a body error must flow OUT of the
        ``with`` unchanged while the sampler is still cleanly stopped — the daemon never
        leaks past a failing pull phase.
        """
        spawned: list[Callable[[], None]] = []
        session = _FakeSession(_FakeController({"recent_minute": 90}))
        observer = _CapturingObserver()
        sampler = _sampler(session, observer, spawn=spawned.append)

        class _BodyError(RuntimeError):
            """A sentinel error raised inside the with-body."""

        # PT012: the block asserts live in-context state before raising; SIM117: the
        # nested `with` is deliberate — `pytest.raises` must wrap the sampler `with` so the
        # body error flows through __exit__.
        with pytest.raises(_BodyError):  # noqa: PT012, SIM117
            with sampler:
                assert len(spawned) == 1  # entered → the daemon was spawned
                assert not sampler._stop.is_set()
                raise _BodyError("phase blew up")

        # The context manager stopped the loop on the way out despite the exception.
        assert sampler._stop.is_set()
        assert not sampler._running

    def test_double_start_does_not_spawn_a_second_daemon(self) -> None:
        """A redundant start() while already running is a no-op (idempotent re-arm).

        The first start spawns one daemon; a second start while still running must NOT
        spawn another (it would share the single _stop event and leak). After stop() the
        sampler re-arms — a fresh start spawns again. Asserting the observable spawn COUNT
        (not the private _running flag) keeps the guard's real contract under test.
        """
        spawned: list[Callable[[], None]] = []
        session = _FakeSession(_FakeController({"recent_minute": 90}))
        observer = _CapturingObserver()
        sampler = _sampler(session, observer, spawn=spawned.append)

        sampler.start()
        assert len(spawned) == 1  # first arm spawned one daemon

        sampler.start()  # already running → guarded no-op, no second daemon
        assert len(spawned) == 1

        sampler.stop()  # clears the running flag so the sampler can be re-armed
        sampler.start()  # a fresh arm after stop spawns again
        assert len(spawned) == 2

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
