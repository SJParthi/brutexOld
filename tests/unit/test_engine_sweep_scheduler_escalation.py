"""Unit tests — the G13 escalation kernel in :mod:`brutex.engine.sweep_scheduler`.

DEC-SWEEP-UNKILLABLE-CORE-001 design D-5: the daemon's escalation DECISION
logic (consecutive-failure counter + pure ``escalate_after`` + the
alerter-first ``daemon_init_guard``) lives in this 100%-tested kernel; the
coverage-omitted ``cmd_sweep_daemon`` shim stays a thin adapter. Edge matrix
pinned exactly: 2 cycles no-escalate / 3 cycles escalate (once, at the edge) /
reset-on-success.
"""

from __future__ import annotations

import pytest
from brutex.engine.sweep_scheduler import (
    ESCALATION_THRESHOLD,
    CycleFailureTracker,
    SchedulerError,
    daemon_init_guard,
    escalate_after,
)
from structlog.testing import capture_logs

#: D-6 traceability — test name → requirement/gap tokens.
TRACEABILITY: dict[str, tuple[str, ...]] = {
    "test_g13_two_cycles_do_not_escalate": ("G13",),
    "test_g13_three_cycles_escalate_exactly_at_the_edge": ("G13",),
    "test_g13_reset_on_success": ("G13",),
    "test_g13_escalate_after_level_decision_exact": ("G13",),
    "test_g13_threshold_validation_fail_loud": ("G13",),
    "test_g13_threshold_one_escalates_on_first_failure": ("G13",),
    "test_g13_threshold_matches_crash_loop_bar": ("G13",),
    "test_g13_edge_equals_level_transition": ("G13",),
    "test_g13_daemon_init_guard_alerts_then_reraises": ("G13",),
    "test_g13_daemon_init_guard_clean_body_no_alert": ("G13",),
    "test_g13_daemon_init_guard_raising_alerter_never_masks": ("G13",),
    "test_g13_daemon_init_guard_operator_stop_no_alert": ("G13",),
}


class FakeHaltAlerter:
    """Records every ``sweep_halted`` call; can simulate a raising alerter."""

    def __init__(self, *, raise_on_call: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.raise_on_call = raise_on_call

    def sweep_halted(self, *, run_id: str, reason: str) -> bool:
        """Record (or raise, exercising the best-effort swallow path)."""
        if self.raise_on_call:
            raise RuntimeError("simulated alerter failure")
        self.calls.append((run_id, reason))
        return True


# ────────────────────────────────────────────────────────────────────────
# CycleFailureTracker — the edge
# ────────────────────────────────────────────────────────────────────────


def test_g13_two_cycles_do_not_escalate() -> None:
    tracker = CycleFailureTracker()
    assert tracker.record_failure() is False
    assert tracker.record_failure() is False
    assert tracker.consecutive_failures == 2


def test_g13_three_cycles_escalate_exactly_at_the_edge() -> None:
    """The 3rd consecutive failure fires the edge ONCE; the 4th does not re-fire."""
    tracker = CycleFailureTracker()
    outcomes = [tracker.record_failure() for _ in range(4)]
    assert outcomes == [False, False, True, False]
    assert tracker.consecutive_failures == 4


def test_g13_reset_on_success() -> None:
    """A success resets the streak — the NEXT streak escalates at its own 3rd."""
    tracker = CycleFailureTracker()
    assert tracker.record_failure() is False
    assert tracker.record_failure() is False
    tracker.record_success()
    assert tracker.consecutive_failures == 0
    assert tracker.record_failure() is False
    assert tracker.record_failure() is False
    assert tracker.record_failure() is True


def test_g13_escalate_after_level_decision_exact() -> None:
    """escalate_after is the pure LEVEL: >= threshold, exact at the boundary."""
    assert escalate_after(0) is False
    assert escalate_after(2) is False
    assert escalate_after(3) is True
    assert escalate_after(4) is True
    assert escalate_after(1, threshold=2) is False
    assert escalate_after(2, threshold=2) is True


def test_g13_threshold_validation_fail_loud() -> None:
    with pytest.raises(SchedulerError, match="escalation threshold must be >= 1, got 0"):
        CycleFailureTracker(threshold=0)
    with pytest.raises(SchedulerError, match="escalation threshold must be >= 1, got -1"):
        escalate_after(5, threshold=-1)


def test_g13_threshold_one_escalates_on_first_failure() -> None:
    tracker = CycleFailureTracker(threshold=1)
    assert tracker.record_failure() is True
    assert tracker.record_failure() is False


def test_g13_threshold_matches_crash_loop_bar() -> None:
    """The kernel's default bar equals the notifications crash-loop bar (repeated, pinned)."""
    from brutex.notifications.sweep_alerts import CRASH_LOOP_RELAUNCH_THRESHOLD

    assert ESCALATION_THRESHOLD == 3
    assert ESCALATION_THRESHOLD == CRASH_LOOP_RELAUNCH_THRESHOLD


def test_g13_edge_equals_level_transition() -> None:
    """Documented relation: the edge fires iff the LEVEL just flipped."""
    tracker = CycleFailureTracker(threshold=2)
    for _ in range(5):
        edge = tracker.record_failure()
        n = tracker.consecutive_failures
        assert edge == (escalate_after(n, threshold=2) and not escalate_after(n - 1, threshold=2))


# ────────────────────────────────────────────────────────────────────────
# daemon_init_guard — alerter-first ordering (D-5)
# ────────────────────────────────────────────────────────────────────────


def test_g13_daemon_init_guard_alerts_then_reraises() -> None:
    """An init failure phones sweep_halted(reason='daemon_init') then re-raises unchanged."""
    alerter = FakeHaltAlerter()
    boom = RuntimeError("init exploded")
    with pytest.raises(RuntimeError) as excinfo, daemon_init_guard(alerter, run_id="r1"):
        raise boom
    assert excinfo.value is boom  # the ORIGINAL exception, never replaced
    assert alerter.calls == [("r1", "daemon_init")]


def test_g13_daemon_init_guard_clean_body_no_alert() -> None:
    alerter = FakeHaltAlerter()
    with daemon_init_guard(alerter):
        pass
    assert alerter.calls == []


def test_g13_daemon_init_guard_raising_alerter_never_masks() -> None:
    """A raising alerter is swallowed + logged; the REAL init failure still propagates."""
    alerter = FakeHaltAlerter(raise_on_call=True)
    boom = ValueError("real init failure")
    with (
        capture_logs() as logs,
        pytest.raises(ValueError, match="real init failure") as excinfo,
        daemon_init_guard(alerter),
    ):
        raise boom
    assert excinfo.value is boom
    assert any(e["event"] == "sweep_scheduler.daemon_init_alert_failed" for e in logs)


def test_g13_daemon_init_guard_operator_stop_no_alert() -> None:
    """KeyboardInterrupt (operator stop) propagates WITHOUT an alert."""
    alerter = FakeHaltAlerter()
    with pytest.raises(KeyboardInterrupt), daemon_init_guard(alerter, run_id="r2"):
        raise KeyboardInterrupt
    assert alerter.calls == []


def test_g13_daemon_init_guard_default_run_id_is_empty() -> None:
    """At daemon init no grid identity exists yet — the default binds ''."""
    alerter = FakeHaltAlerter()
    with pytest.raises(RuntimeError), daemon_init_guard(alerter):
        raise RuntimeError("pre-identity failure")
    assert alerter.calls == [("", "daemon_init")]


TRACEABILITY["test_g13_daemon_init_guard_default_run_id_is_empty"] = ("G13",)
