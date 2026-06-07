"""Unit tests for the sweep watchdog (`brutex.engine.sweep_watchdog`).

100% line + branch coverage of the heartbeat + hung-combo detector
(DEC-SWEEP-REALTIME-GUARANTEE-001, CLAUDE.md §11 AUTOMATED pillar):

* construction (default + injected clock, ``timeout_seconds <= 0`` raise),
  ``register`` / ``beat`` / ``timeout_seconds``;
* :meth:`check_once` firing the HEALTHY→HUNG and HUNG→HEALTHY edges, with and
  without user callbacks (the ``on_hung``/``on_recovery`` None branches);
* ``state_of`` (known + unknown KeyError), ``workers``, ``hung_workers``
  (mixed health), and the O(1) ``snapshot`` tally;
* the defensive ``hung_count == 0`` recovery branch via a direct handler call.
"""

from __future__ import annotations

import pytest
from brutex.engine.sweep_watchdog import (
    DEFAULT_TIMEOUT_SECONDS,
    SweepWatchdog,
)
from brutex.observability.watchdog import WorkerEvent, WorkerState


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_default_timeout_and_clock() -> None:
    wd = SweepWatchdog()  # default (None) clock → real monotonic; default timeout
    assert wd.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
    wd.register("w0")
    wd.beat("w0")
    # With the real clock and a generous timeout, nothing is hung immediately.
    assert wd.check_once() == ()
    assert wd.snapshot() == {
        "total": 1,
        "hung": 0,
        "healthy": 1,
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
    }


def test_invalid_timeout_raises() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        SweepWatchdog(timeout_seconds=0)


def test_hung_then_recovery_with_callbacks() -> None:
    clk = _Clock()
    hung: list[str] = []
    recovered: list[str] = []
    wd = SweepWatchdog(
        timeout_seconds=10.0,
        on_hung=lambda e: hung.append(e.worker_name),
        on_recovery=lambda e: recovered.append(e.worker_name),
        clock=clk,
    )
    wd.beat("w0")
    clk.t = 20.0
    events = wd.check_once()
    assert [(e.worker_name, e.new_state) for e in events] == [("w0", WorkerState.HUNG)]
    assert hung == ["w0"]
    assert wd.snapshot()["hung"] == 1
    assert wd.state_of("w0") is WorkerState.HUNG

    # A fresh heartbeat recovers it.
    wd.beat("w0")
    events = wd.check_once()
    assert [(e.worker_name, e.new_state) for e in events] == [("w0", WorkerState.HEALTHY)]
    assert recovered == ["w0"]
    snap = wd.snapshot()
    assert snap["hung"] == 0
    assert snap["healthy"] == 1


def test_hung_without_callbacks() -> None:
    clk = _Clock()
    wd = SweepWatchdog(timeout_seconds=5.0, clock=clk)  # on_hung / on_recovery None
    wd.beat("w0")
    clk.t = 10.0
    events = wd.check_once()
    assert events[0].new_state is WorkerState.HUNG
    assert wd.snapshot()["hung"] == 1
    # Recovery path with on_recovery None.
    wd.beat("w0")
    events = wd.check_once()
    assert events[0].new_state is WorkerState.HEALTHY
    assert wd.snapshot()["hung"] == 0


def test_register_marks_known() -> None:
    clk = _Clock()
    wd = SweepWatchdog(timeout_seconds=5.0, clock=clk)
    wd.register("w0")
    assert wd.snapshot()["total"] == 1
    assert wd.state_of("w0") is WorkerState.HEALTHY


def test_state_of_unknown_raises() -> None:
    wd = SweepWatchdog(timeout_seconds=5.0, clock=_Clock())
    with pytest.raises(KeyError):
        wd.state_of("ghost")


def test_workers_and_hung_workers_mixed() -> None:
    clk = _Clock()
    wd = SweepWatchdog(timeout_seconds=10.0, clock=clk)
    wd.beat("w0")
    clk.t = 5.0
    wd.beat("w1")  # w1 beats later, w0 will age out first
    clk.t = 16.0  # now - w0(0) = 16 > 10 hung; now - w1(5) = 11 > 10 hung
    wd.check_once()
    assert wd.workers() == ("w0", "w1")
    # Re-beat w1 so only w0 stays hung.
    wd.beat("w1")
    wd.check_once()
    assert wd.hung_workers() == ("w0",)
    snap = wd.snapshot()
    assert snap["total"] == 2
    assert snap["hung"] == 1
    assert snap["healthy"] == 1


def test_recovery_handler_floor_at_zero() -> None:
    # Defensive branch: a recovery edge when the hung tally is already 0 must
    # NOT underflow (hung_count stays 0), and still forwards to the callback.
    recovered: list[str] = []
    wd = SweepWatchdog(
        timeout_seconds=5.0,
        on_recovery=lambda e: recovered.append(e.worker_name),
        clock=_Clock(),
    )
    event = WorkerEvent(
        worker_name="w0",
        new_state=WorkerState.HEALTHY,
        last_beat_seconds_ago=1.0,
        timeout_seconds=5.0,
    )
    wd._handle_recovery(event)
    assert wd.snapshot()["hung"] == 0
    assert recovered == ["w0"]


def test_check_once_no_transition_returns_empty() -> None:
    clk = _Clock()
    wd = SweepWatchdog(timeout_seconds=10.0, clock=clk)
    wd.beat("w0")
    assert wd.check_once() == ()  # still healthy, no edge
