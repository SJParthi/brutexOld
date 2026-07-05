"""Unit tests for the process supervisor (DEC-SUPERVISOR-AUTORESTART-001).

Covers the pure supervise loop in :mod:`brutex.orchestrator.supervisor` — the
*actor* the two detector-watchdogs left out: it spawns the sweep daemon as a
CHILD and AUTO-RELAUNCHES it on a hard crash. Every test uses a FAKE injected
child + an injected clock + an injected sleeper, so nothing spawns a real
process and no test ever sleeps.

The coverage contract (100% line + branch on the pure loop) exercises:

* clean child exit (code 0) → STOP, no relaunch;
* child crash (non-zero / signal death) → relaunch after backoff;
* exponential backoff timing via the injected sleeper;
* the max-restart cap → give up loudly (non-zero-mapped outcome);
* an operator stop (SIGTERM/SIGINT reaches the supervisor) → no relaunch, at
  every point it can arrive (pre-launch, WHILE THE CHILD IS ALIVE → the child is
  actually TERMINATED, post-exit, during-backoff);
* the interruptible wait + sliced backoff (an operator stop is prompt, not
  blocked until the child dies / the whole delay elapses);
* the backoff RESET after a child stays up past the stable-uptime window;
* every restart / clean-exit / operator-stop / give-up appends an audit row;
* O(1) / bounded state under many restarts;
* the graceful-degradation guarantee (a failing audit sink never crashes the
  supervisor);
* :class:`BackoffPolicy` validation + the exponential-with-cap delay math;
* the graceful :meth:`ProcessSupervisor.shutdown_child` (SIGTERM → wait → SIGKILL);
* the pure ``_SignalStop`` latch (whitebox — a private in-process flag).
"""

# Whitebox access to the private ``_SignalStop`` latch (a pure in-process flag
# with no OS side effect) — the repo convention for whitebox test files
# (DEC-P0-017); the latch's real-signal INSTALLATION stays pragma'd in the module.
# pyright: reportPrivateUsage=none
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from brutex.models import BruteXError
from brutex.orchestrator.supervisor import (
    AUDIT_ACTION_CLEAN_EXIT,
    AUDIT_ACTION_GIVE_UP,
    AUDIT_ACTION_OPERATOR_STOP,
    AUDIT_ACTION_RESTART,
    DEFAULT_ALERT_WINDOW_SECONDS,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_BACKOFF_CAP_SECONDS,
    DEFAULT_BACKOFF_FACTOR,
    DEFAULT_MAX_RESTARTS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_RESET_AFTER_UPTIME_SECONDS,
    DEFAULT_SHUTDOWN_GRACE_SECONDS,
    BackoffPolicy,
    ProcessSupervisor,
    SupervisorOutcome,
    _SignalStop,
)

# ────────────────────────────────────────────────────────────────────────
# Test doubles
# ────────────────────────────────────────────────────────────────────────


class FakeClock:
    """Deterministic monotonic clock the tests advance by hand.

    The supervisor calls the clock TWICE per child life-cycle (start + after
    ``wait``); a per-child uptime is injected via :class:`FakeChild.uptime`,
    which advances this clock inside ``wait``.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeChild:
    """A scripted child: the FIRST ``wait_for`` advances the clock by *uptime* then
    returns *code* (the child 'exits' on the first poll slice).

    The supervisor drives the child ONLY through the interruptible
    ``wait_for``-poll loop (no blocking ``wait``); modelling the child as
    exiting on its first poll keeps the injected-clock uptime math identical to
    the pre-interruptible design while exercising the real poll path. A
    ``stays_alive_polls`` count models a child that survives that many poll
    slices before exiting (used to test an operator stop landing WHILE the child
    is alive).
    """

    def __init__(
        self,
        *,
        code: int,
        uptime: float,
        clock: FakeClock,
        pid: int = 1234,
        stays_alive_polls: int = 0,
    ) -> None:
        self._code = code
        self._uptime = uptime
        self._clock = clock
        self._pid = pid
        self._stays_alive_polls = stays_alive_polls
        self._polls = 0
        self.terminated = False
        self.killed = False

    @property
    def pid(self) -> int:
        return self._pid

    def wait_for(self, timeout: float) -> int | None:
        # Model the child as still-alive for `stays_alive_polls` slices, then
        # exiting on the next poll (advancing the clock by its uptime once).
        if self._polls < self._stays_alive_polls:
            self._polls += 1
            return None
        self._clock.advance(self._uptime)
        return self._code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


@dataclass
class ScriptedLauncher:
    """Pops a pre-built :class:`FakeChild` per launch; records the launch count."""

    children: list[FakeChild]
    launches: int = 0

    def __call__(self) -> FakeChild:
        child = self.children[self.launches]
        self.launches += 1
        return child


@dataclass
class RecordingSleeper:
    """Captures every backoff delay slept; never actually sleeps.

    Optionally flips an operator-stop flag once a given number of sleeps have
    happened, to model a signal arriving DURING a backoff wait.
    """

    delays: list[float] = field(default_factory=list)
    stop_flag: list[bool] = field(default_factory=lambda: [False])
    stop_after_sleep: int | None = None

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        if self.stop_after_sleep is not None and len(self.delays) >= self.stop_after_sleep:
            self.stop_flag[0] = True


@dataclass
class RecordingAuditSink:
    """Captures every appended row; matches the supervisor's ``AuditSink`` surface."""

    rows: list[dict[str, Any]] = field(default_factory=list)

    def append(self, *, action: str, subject: str, payload: dict[str, Any]) -> dict[str, Any]:
        entry = {"action": action, "subject": subject, "payload": payload}
        self.rows.append(entry)
        return entry


class RaisingAuditSink:
    """Audit sink whose ``append`` always raises the given exception."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def append(self, *, action: str, subject: str, payload: dict[str, Any]) -> Any:
        raise self._exc


def _actions(sink: RecordingAuditSink) -> list[str]:
    """Return the ordered action names of every stamped audit row."""
    return [row["action"] for row in sink.rows]


# ────────────────────────────────────────────────────────────────────────
# BackoffPolicy — validation + delay math
# ────────────────────────────────────────────────────────────────────────


class TestBackoffPolicy:
    def test_defaults_match_module_constants(self) -> None:
        policy = BackoffPolicy()
        assert policy.base_seconds == DEFAULT_BACKOFF_BASE_SECONDS
        assert policy.factor == DEFAULT_BACKOFF_FACTOR
        assert policy.cap_seconds == DEFAULT_BACKOFF_CAP_SECONDS
        assert policy.reset_after_uptime_seconds == DEFAULT_RESET_AFTER_UPTIME_SECONDS
        assert policy.max_restarts == DEFAULT_MAX_RESTARTS

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_rejects_nonpositive_base(self, bad: float) -> None:
        with pytest.raises(ValueError, match="base_seconds must be > 0"):
            BackoffPolicy(base_seconds=bad)

    @pytest.mark.parametrize("bad", [0.5, 0.0, -1.0])
    def test_rejects_factor_below_one(self, bad: float) -> None:
        with pytest.raises(ValueError, match="factor must be >= 1"):
            BackoffPolicy(factor=bad)

    def test_rejects_cap_below_base(self) -> None:
        with pytest.raises(ValueError, match="must be >= base_seconds"):
            BackoffPolicy(base_seconds=10.0, cap_seconds=5.0)

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_rejects_nonpositive_reset_uptime(self, bad: float) -> None:
        with pytest.raises(ValueError, match="reset_after_uptime_seconds must be > 0"):
            BackoffPolicy(reset_after_uptime_seconds=bad)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_rejects_max_restarts_below_one(self, bad: int) -> None:
        with pytest.raises(ValueError, match="max_restarts must be >= 1"):
            BackoffPolicy(max_restarts=bad)

    def test_delay_is_exponential(self) -> None:
        policy = BackoffPolicy(base_seconds=1.0, factor=2.0, cap_seconds=1000.0)
        assert policy.delay_for(0) == 1.0
        assert policy.delay_for(1) == 2.0
        assert policy.delay_for(2) == 4.0
        assert policy.delay_for(3) == 8.0

    def test_delay_is_capped(self) -> None:
        policy = BackoffPolicy(base_seconds=1.0, factor=2.0, cap_seconds=5.0)
        assert policy.delay_for(0) == 1.0
        assert policy.delay_for(2) == 4.0
        assert policy.delay_for(3) == 5.0  # 8 capped to 5
        assert policy.delay_for(10) == 5.0

    def test_flat_backoff_with_factor_one(self) -> None:
        policy = BackoffPolicy(base_seconds=3.0, factor=1.0, cap_seconds=3.0)
        assert policy.delay_for(0) == 3.0
        assert policy.delay_for(5) == 3.0

    def test_delay_rejects_negative_index(self) -> None:
        with pytest.raises(ValueError, match="consecutive_index must be >= 0"):
            BackoffPolicy().delay_for(-1)


# ────────────────────────────────────────────────────────────────────────
# ProcessSupervisor construction
# ────────────────────────────────────────────────────────────────────────


class TestConstruction:
    def test_rejects_nonpositive_shutdown_grace(self) -> None:
        with pytest.raises(ValueError, match="shutdown_grace_seconds must be > 0"):
            ProcessSupervisor(
                launch=lambda: FakeChild(code=0, uptime=0.0, clock=FakeClock()),
                shutdown_grace_seconds=0.0,
            )

    def test_default_shutdown_grace(self) -> None:
        sup = ProcessSupervisor(launch=lambda: FakeChild(code=0, uptime=0.0, clock=FakeClock()))
        assert sup.shutdown_grace_seconds == DEFAULT_SHUTDOWN_GRACE_SECONDS

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_rejects_nonpositive_poll_interval(self, bad: float) -> None:
        with pytest.raises(ValueError, match="poll_interval_seconds must be > 0"):
            ProcessSupervisor(
                launch=lambda: FakeChild(code=0, uptime=0.0, clock=FakeClock()),
                poll_interval_seconds=bad,
            )

    def test_default_poll_interval(self) -> None:
        sup = ProcessSupervisor(launch=lambda: FakeChild(code=0, uptime=0.0, clock=FakeClock()))
        assert sup.poll_interval_seconds == DEFAULT_POLL_INTERVAL_SECONDS


# ────────────────────────────────────────────────────────────────────────
# Clean exit → stop
# ────────────────────────────────────────────────────────────────────────


class TestCleanExit:
    def test_clean_child_exit_stops_without_relaunch(self) -> None:
        clock = FakeClock()
        child = FakeChild(code=0, uptime=1.0, clock=clock)
        launcher = ScriptedLauncher([child])
        audit = RecordingAuditSink()
        sup = ProcessSupervisor(launch=launcher, audit=audit, clock=clock)

        result = sup.run()

        assert result.outcome is SupervisorOutcome.CLEAN_EXIT
        assert result.restarts == 0
        assert result.last_exit_code == 0
        assert launcher.launches == 1
        assert _actions(audit) == [AUDIT_ACTION_CLEAN_EXIT]

    def test_clean_exit_after_one_crash(self) -> None:
        clock = FakeClock()
        children = [
            FakeChild(code=1, uptime=0.5, clock=clock),  # crash
            FakeChild(code=0, uptime=0.5, clock=clock),  # then success
        ]
        launcher = ScriptedLauncher(children)
        sleeper = RecordingSleeper()
        audit = RecordingAuditSink()
        sup = ProcessSupervisor(launch=launcher, audit=audit, clock=clock, sleeper=sleeper)

        result = sup.run()

        assert result.outcome is SupervisorOutcome.CLEAN_EXIT
        assert result.restarts == 1
        assert launcher.launches == 2
        assert _actions(audit) == [AUDIT_ACTION_RESTART, AUDIT_ACTION_CLEAN_EXIT]


# ────────────────────────────────────────────────────────────────────────
# Crash → relaunch + exponential backoff timing
# ────────────────────────────────────────────────────────────────────────


class TestCrashRelaunch:
    @pytest.mark.parametrize("crash_code", [1, 2, 137, -9, -15])
    def test_nonzero_or_signal_exit_relaunches(self, crash_code: int) -> None:
        """Any non-zero code AND any signal-death (negative code) is a crash."""
        clock = FakeClock()
        children = [
            FakeChild(code=crash_code, uptime=0.1, clock=clock),
            FakeChild(code=0, uptime=0.1, clock=clock),
        ]
        launcher = ScriptedLauncher(children)
        sleeper = RecordingSleeper()
        sup = ProcessSupervisor(launch=launcher, clock=clock, sleeper=sleeper)

        result = sup.run()

        assert result.restarts == 1
        assert result.outcome is SupervisorOutcome.CLEAN_EXIT

    def test_exponential_backoff_timing(self) -> None:
        """Consecutive crashes back off 1, 2, 4 (base 1, factor 2) — via the sleeper."""
        clock = FakeClock()
        # 3 short-lived crashes (uptime below the reset window), then a success.
        children = [
            FakeChild(code=1, uptime=0.1, clock=clock),
            FakeChild(code=1, uptime=0.1, clock=clock),
            FakeChild(code=1, uptime=0.1, clock=clock),
            FakeChild(code=0, uptime=0.1, clock=clock),
        ]
        launcher = ScriptedLauncher(children)
        sleeper = RecordingSleeper()
        policy = BackoffPolicy(
            base_seconds=1.0,
            factor=2.0,
            cap_seconds=100.0,
            reset_after_uptime_seconds=60.0,
            max_restarts=10,
        )
        # A poll interval wider than any backoff so each backoff is ONE recorded
        # slice (the sliced-sleep interruptibility is tested separately).
        sup = ProcessSupervisor(
            launch=launcher,
            policy=policy,
            clock=clock,
            sleeper=sleeper,
            poll_interval_seconds=1000.0,
        )

        result = sup.run()

        assert result.restarts == 3
        assert sleeper.delays == [1.0, 2.0, 4.0]
        assert result.outcome is SupervisorOutcome.CLEAN_EXIT

    def test_backoff_is_capped(self) -> None:
        clock = FakeClock()
        children = [FakeChild(code=1, uptime=0.1, clock=clock) for _ in range(4)]
        children.append(FakeChild(code=0, uptime=0.1, clock=clock))
        launcher = ScriptedLauncher(children)
        sleeper = RecordingSleeper()
        policy = BackoffPolicy(base_seconds=1.0, factor=2.0, cap_seconds=3.0, max_restarts=10)
        sup = ProcessSupervisor(
            launch=launcher,
            policy=policy,
            clock=clock,
            sleeper=sleeper,
            poll_interval_seconds=1000.0,
        )

        sup.run()

        # 1, 2, 4→3, 8→3
        assert sleeper.delays == [1.0, 2.0, 3.0, 3.0]


# ────────────────────────────────────────────────────────────────────────
# Backoff reset after stable uptime
# ────────────────────────────────────────────────────────────────────────


class TestBackoffReset:
    def test_stable_uptime_resets_backoff(self) -> None:
        """A crash after a LONG uptime resets the streak, so the next backoff is base again."""
        clock = FakeClock()
        children = [
            FakeChild(code=1, uptime=0.1, clock=clock),  # streak 0 → sleep 1
            FakeChild(code=1, uptime=0.1, clock=clock),  # streak 1 → sleep 2
            FakeChild(code=1, uptime=100.0, clock=clock),  # up 100s ≥ 60 → RESET → sleep 1
            FakeChild(code=1, uptime=0.1, clock=clock),  # streak 1 again → sleep 2
            FakeChild(code=0, uptime=0.1, clock=clock),
        ]
        launcher = ScriptedLauncher(children)
        sleeper = RecordingSleeper()
        policy = BackoffPolicy(
            base_seconds=1.0, factor=2.0, cap_seconds=100.0, reset_after_uptime_seconds=60.0
        )
        sup = ProcessSupervisor(
            launch=launcher,
            policy=policy,
            clock=clock,
            sleeper=sleeper,
            poll_interval_seconds=1000.0,
        )

        sup.run()

        assert sleeper.delays == [1.0, 2.0, 1.0, 2.0]

    def test_reset_exactly_at_window_boundary(self) -> None:
        """Uptime EXACTLY == reset window resets (>=, inclusive)."""
        clock = FakeClock()
        children = [
            FakeChild(code=1, uptime=0.1, clock=clock),  # sleep 1
            FakeChild(code=1, uptime=60.0, clock=clock),  # exactly 60 → RESET → sleep 1
            FakeChild(code=0, uptime=0.1, clock=clock),
        ]
        launcher = ScriptedLauncher(children)
        sleeper = RecordingSleeper()
        policy = BackoffPolicy(base_seconds=1.0, factor=2.0, reset_after_uptime_seconds=60.0)
        sup = ProcessSupervisor(
            launch=launcher,
            policy=policy,
            clock=clock,
            sleeper=sleeper,
            poll_interval_seconds=1000.0,
        )

        sup.run()

        assert sleeper.delays == [1.0, 1.0]


# ────────────────────────────────────────────────────────────────────────
# Max-restarts cap → give up loudly
# ────────────────────────────────────────────────────────────────────────


class TestMaxRestartsCap:
    def test_crash_loop_gives_up_after_cap(self) -> None:
        clock = FakeClock()
        # Never-ending crash loop; cap=3 → give up on the 4th observed crash.
        children = [FakeChild(code=1, uptime=0.1, clock=clock) for _ in range(10)]
        launcher = ScriptedLauncher(children)
        sleeper = RecordingSleeper()
        audit = RecordingAuditSink()
        policy = BackoffPolicy(
            base_seconds=1.0,
            factor=2.0,
            cap_seconds=100.0,
            reset_after_uptime_seconds=60.0,
            max_restarts=3,
        )
        sup = ProcessSupervisor(
            launch=launcher,
            policy=policy,
            audit=audit,
            clock=clock,
            sleeper=sleeper,
            poll_interval_seconds=1000.0,
        )

        result = sup.run()

        assert result.outcome is SupervisorOutcome.GAVE_UP
        assert result.restarts == 3  # relaunched 3 times, the 4th crash gives up
        assert launcher.launches == 4  # 1 initial + 3 relaunches
        assert sleeper.delays == [1.0, 2.0, 4.0]
        assert _actions(audit) == [
            AUDIT_ACTION_RESTART,
            AUDIT_ACTION_RESTART,
            AUDIT_ACTION_RESTART,
            AUDIT_ACTION_GIVE_UP,
        ]

    def test_give_up_audit_carries_context(self) -> None:
        clock = FakeClock()
        children = [FakeChild(code=42, uptime=0.1, clock=clock) for _ in range(5)]
        launcher = ScriptedLauncher(children)
        sleeper = RecordingSleeper()
        audit = RecordingAuditSink()
        policy = BackoffPolicy(max_restarts=1, reset_after_uptime_seconds=60.0)
        sup = ProcessSupervisor(
            launch=launcher, policy=policy, audit=audit, clock=clock, sleeper=sleeper
        )

        sup.run()

        give_up = audit.rows[-1]
        assert give_up["action"] == AUDIT_ACTION_GIVE_UP
        assert give_up["payload"]["exit_code"] == 42
        assert give_up["payload"]["max_restarts"] == 1


# ────────────────────────────────────────────────────────────────────────
# Operator stop (SIGTERM/SIGINT) — no relaunch
# ────────────────────────────────────────────────────────────────────────


class TestOperatorStop:
    def test_stop_before_first_launch_never_spawns(self) -> None:
        clock = FakeClock()
        launcher = ScriptedLauncher([FakeChild(code=0, uptime=1.0, clock=clock)])
        audit = RecordingAuditSink()
        sup = ProcessSupervisor(
            launch=launcher, audit=audit, clock=clock, stop_requested=lambda: True
        )

        result = sup.run()

        assert result.outcome is SupervisorOutcome.OPERATOR_STOP
        assert result.restarts == 0
        assert result.last_exit_code is None
        assert launcher.launches == 0  # never spawned a child we would kill
        assert _actions(audit) == [AUDIT_ACTION_OPERATOR_STOP]
        assert audit.rows[0]["payload"]["phase"] == "pre_launch"

    def test_stop_while_child_alive_terminates_child_no_relaunch(self) -> None:
        """THE headline MAJOR-1 case: an operator stop arrives WHILE the child is
        still running → the supervisor TERMINATES the child (SIGTERM→…→SIGKILL)
        and returns OPERATOR_STOP with NO relaunch — the child is never orphaned.

        The fake child ``stays_alive_polls=2`` so the interruptible wait sees it
        still-alive for two poll slices; the stop flag flips before the child
        would exit on its own, so ``run`` takes the graceful-shutdown branch.
        """
        clock = FakeClock()
        stop = [False]

        # A child that stays alive during the run-loop poll (returns None) and
        # requests the operator stop, so the loop shuts it down WHILE alive; it
        # exits cleanly (code 0) once terminated.
        class _AliveThenTerminatedChild(FakeChild):
            def wait_for(self, timeout: float) -> int | None:
                if self.terminated:
                    return 0  # exits promptly on SIGTERM
                stop[0] = True  # a signal reached the supervisor while we run
                return None  # still alive during the run-loop poll

        child = _AliveThenTerminatedChild(code=0, uptime=1.0, clock=clock)
        # A SECOND child proves NO relaunch happens (it must never be launched).
        second = FakeChild(code=0, uptime=1.0, clock=clock)
        launcher = ScriptedLauncher([child, second])
        audit = RecordingAuditSink()
        sup = ProcessSupervisor(
            launch=launcher,
            audit=audit,
            clock=clock,
            stop_requested=lambda: stop[0],
            poll_interval_seconds=1000.0,
        )

        result = sup.run()

        assert result.outcome is SupervisorOutcome.OPERATOR_STOP
        assert result.restarts == 0
        assert child.terminated is True  # the live child WAS terminated (the fix)
        assert child.killed is False  # SIGTERM was enough; no SIGKILL needed
        assert result.last_exit_code == 0  # the shutdown observed the exit code
        assert launcher.launches == 1  # the second child was NEVER launched
        assert _actions(audit) == [AUDIT_ACTION_OPERATOR_STOP]
        assert audit.rows[0]["payload"]["phase"] == "shutdown"

    def test_stop_while_alive_escalates_to_kill_when_terminate_ignored(self) -> None:
        """If the shut-down child ignores SIGTERM, the supervisor escalates to
        SIGKILL — proving the graceful-shutdown ladder runs on the run()-level
        operator-stop path (not just via the standalone shutdown_child helper)."""
        clock = FakeClock()
        stop = [False]

        class _WedgedAliveChild(FakeChild):
            def __init__(self) -> None:
                super().__init__(code=0, uptime=1.0, clock=clock, stays_alive_polls=5)

            def wait_for(self, timeout: float) -> int | None:
                # While the run-loop polls for exit, it is alive; flip the stop.
                # During shutdown (after terminate()) it stays alive until kill().
                if self.terminated and not self.killed:
                    return None  # ignored SIGTERM
                if self.killed:
                    return -9  # died to SIGKILL
                stop[0] = True
                return None  # still alive during the run-loop poll

        child = _WedgedAliveChild()
        launcher = ScriptedLauncher([child])
        sup = ProcessSupervisor(
            launch=launcher,
            clock=clock,
            stop_requested=lambda: stop[0],
            shutdown_grace_seconds=5.0,
            poll_interval_seconds=1000.0,
        )

        result = sup.run()

        assert result.outcome is SupervisorOutcome.OPERATOR_STOP
        assert child.terminated is True
        assert child.killed is True  # SIGTERM ignored → escalated to SIGKILL
        assert result.last_exit_code == -9

    def test_stop_after_child_exits_no_relaunch(self) -> None:
        """A stop that races the child's own crash: honour it, do not relaunch.

        The stop flag was NOT set while the child was alive (so no
        graceful-shutdown fires), but flips True the moment the child's own
        crash is observed — modelling a SIGTERM delivered while the child was
        dying. The supervisor honours it in the ``post_exit`` phase.
        """
        clock = FakeClock()
        stop = [False]
        audit = RecordingAuditSink()

        def _stop_requested() -> bool:
            return stop[0]

        # A child whose exit-returning wait_for ALSO latches the stop, so the
        # child was alive (stop False) during the poll, then the post-exit check
        # sees True — the race this branch handles.
        class _StopOnExitChild(FakeChild):
            def wait_for(self, timeout: float) -> int | None:
                stop[0] = True
                return super().wait_for(timeout)

        children = [
            _StopOnExitChild(code=1, uptime=0.1, clock=clock),
            FakeChild(code=0, uptime=0.1, clock=clock),
        ]
        launcher = ScriptedLauncher(children)
        sup = ProcessSupervisor(
            launch=launcher, audit=audit, clock=clock, stop_requested=_stop_requested
        )

        result = sup.run()

        assert result.outcome is SupervisorOutcome.OPERATOR_STOP
        assert result.restarts == 0
        assert result.last_exit_code == 1
        assert launcher.launches == 1  # never relaunched
        assert children[0].terminated is False  # child exited on its own — no shutdown
        assert _actions(audit) == [AUDIT_ACTION_OPERATOR_STOP]
        assert audit.rows[0]["payload"]["phase"] == "post_exit"

    def test_stop_during_backoff_no_relaunch(self) -> None:
        """A signal arriving DURING the backoff wait aborts the relaunch."""
        clock = FakeClock()
        children = [
            FakeChild(code=1, uptime=0.1, clock=clock),
            FakeChild(code=0, uptime=0.1, clock=clock),  # would run, but stop first
        ]
        launcher = ScriptedLauncher(children)
        sleeper = RecordingSleeper(stop_after_sleep=1)
        audit = RecordingAuditSink()
        sup = ProcessSupervisor(
            launch=launcher,
            audit=audit,
            clock=clock,
            sleeper=sleeper,
            stop_requested=lambda: sleeper.stop_flag[0],
            # Poll wider than the backoff so the whole 1.0s is one recorded slice.
            poll_interval_seconds=1000.0,
        )

        result = sup.run()

        assert result.outcome is SupervisorOutcome.OPERATOR_STOP
        assert result.restarts == 1  # counted the relaunch attempt
        assert launcher.launches == 1  # but never actually relaunched
        assert sleeper.delays == [1.0]  # slept the backoff once
        assert _actions(audit) == [AUDIT_ACTION_RESTART, AUDIT_ACTION_OPERATOR_STOP]
        assert audit.rows[-1]["payload"]["phase"] == "during_backoff"


# ────────────────────────────────────────────────────────────────────────
# Audit every restart + graceful degradation
# ────────────────────────────────────────────────────────────────────────


class TestAudit:
    def test_no_audit_sink_is_silent_ok(self) -> None:
        clock = FakeClock()
        children = [
            FakeChild(code=1, uptime=0.1, clock=clock),
            FakeChild(code=0, uptime=0.1, clock=clock),
        ]
        sup = ProcessSupervisor(
            launch=ScriptedLauncher(children), clock=clock, sleeper=RecordingSleeper()
        )
        # No audit wired → no crash, still relaunches.
        result = sup.run()
        assert result.restarts == 1

    def test_restart_audit_carries_backoff_and_index(self) -> None:
        clock = FakeClock()
        children = [
            FakeChild(code=1, uptime=0.1, clock=clock),
            FakeChild(code=0, uptime=0.1, clock=clock),
        ]
        audit = RecordingAuditSink()
        sup = ProcessSupervisor(
            launch=ScriptedLauncher(children), audit=audit, clock=clock, sleeper=RecordingSleeper()
        )
        sup.run()
        restart = audit.rows[0]
        assert restart["action"] == AUDIT_ACTION_RESTART
        assert restart["subject"] == "supervisor"
        assert restart["payload"]["exit_code"] == 1
        assert restart["payload"]["backoff_seconds"] == DEFAULT_BACKOFF_BASE_SECONDS
        assert restart["payload"]["restart_index"] == 1
        assert restart["payload"]["consecutive"] == 1

    def test_failing_audit_sink_never_crashes_supervisor(self) -> None:
        clock = FakeClock()
        children = [
            FakeChild(code=1, uptime=0.1, clock=clock),
            FakeChild(code=0, uptime=0.1, clock=clock),
        ]
        sup = ProcessSupervisor(
            launch=ScriptedLauncher(children),
            audit=RaisingAuditSink(OSError("disk full")),
            clock=clock,
            sleeper=RecordingSleeper(),
        )
        # A failing audit write is swallowed; the run still completes.
        result = sup.run()
        assert result.outcome is SupervisorOutcome.CLEAN_EXIT
        assert result.restarts == 1

    def test_failing_audit_sink_brutex_error_swallowed(self) -> None:
        clock = FakeClock()
        children = [FakeChild(code=0, uptime=0.1, clock=clock)]
        sup = ProcessSupervisor(
            launch=ScriptedLauncher(children),
            audit=RaisingAuditSink(BruteXError("boom")),
            clock=clock,
        )
        result = sup.run()
        assert result.outcome is SupervisorOutcome.CLEAN_EXIT

    def test_custom_subject_stamped(self) -> None:
        clock = FakeClock()
        children = [FakeChild(code=0, uptime=0.1, clock=clock)]
        audit = RecordingAuditSink()
        sup = ProcessSupervisor(
            launch=ScriptedLauncher(children),
            audit=audit,
            subject="supervisor:sweep-daemon",
            clock=clock,
        )
        sup.run()
        assert audit.rows[0]["subject"] == "supervisor:sweep-daemon"


# ────────────────────────────────────────────────────────────────────────
# Bounded state under many restarts (O(1))
# ────────────────────────────────────────────────────────────────────────


class TestBoundedState:
    def test_many_restarts_bounded_state(self) -> None:
        """500 crashes then success — the supervisor keeps only O(1) counters."""
        clock = FakeClock()
        n = 500
        children = [FakeChild(code=1, uptime=0.001, clock=clock) for _ in range(n)]
        children.append(FakeChild(code=0, uptime=0.001, clock=clock))
        launcher = ScriptedLauncher(children)
        sleeper = RecordingSleeper()
        # A high cap so it never gives up; a big reset window so backoff climbs to the cap.
        policy = BackoffPolicy(
            base_seconds=0.01,
            factor=2.0,
            cap_seconds=1.0,
            reset_after_uptime_seconds=1000.0,
            max_restarts=n + 5,
        )
        sup = ProcessSupervisor(
            launch=launcher,
            policy=policy,
            clock=clock,
            sleeper=sleeper,
            poll_interval_seconds=1000.0,
        )

        result = sup.run()

        assert result.restarts == n
        assert result.outcome is SupervisorOutcome.CLEAN_EXIT
        # The supervisor is a slots dataclass: no per-restart accumulation field
        # exists — its entire state is the injected collaborators + scalars.
        state_fields = set(ProcessSupervisor.__slots__)
        assert "launch" in state_fields
        # Delays saturate at the cap — proving the backoff index math stayed bounded.
        assert sleeper.delays[-1] == 1.0


# ────────────────────────────────────────────────────────────────────────
# Graceful shutdown_child (SIGTERM → wait → SIGKILL)
# ────────────────────────────────────────────────────────────────────────


class _ControllableChild:
    """A child whose ``wait_for`` returns a scripted sequence of results."""

    def __init__(self, *, wait_for_results: list[int | None], pid: int = 999) -> None:
        self._results = list(wait_for_results)
        self._i = 0
        self._pid = pid
        self.terminated = False
        self.killed = False

    @property
    def pid(self) -> int:
        return self._pid

    def wait_for(self, timeout: float) -> int | None:
        result = self._results[self._i]
        self._i += 1
        return result

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class TestShutdownChild:
    def _supervisor(self) -> ProcessSupervisor:
        return ProcessSupervisor(
            launch=lambda: FakeChild(code=0, uptime=0.0, clock=FakeClock()),
            shutdown_grace_seconds=5.0,
        )

    def test_graceful_terminate_succeeds(self) -> None:
        child = _ControllableChild(wait_for_results=[0])
        code = self._supervisor().shutdown_child(child)
        assert child.terminated is True
        assert child.killed is False
        assert code == 0

    def test_escalates_to_kill_when_terminate_ignored(self) -> None:
        # First wait_for (after SIGTERM) → None (still alive); after SIGKILL → -9.
        child = _ControllableChild(wait_for_results=[None, -9])
        code = self._supervisor().shutdown_child(child)
        assert child.terminated is True
        assert child.killed is True
        assert code == -9

    def test_unkillable_child_returns_none(self) -> None:
        child = _ControllableChild(wait_for_results=[None, None])
        code = self._supervisor().shutdown_child(child)
        assert child.killed is True
        assert code is None


# ────────────────────────────────────────────────────────────────────────
# Interruptible backoff sleep — sliced + prompt on stop
# ────────────────────────────────────────────────────────────────────────


class TestSlicedBackoffSleep:
    def test_backoff_sleeps_in_poll_sized_slices(self) -> None:
        """A backoff LONGER than the poll interval is slept in poll-sized slices
        (interruptibility), with a short final remainder slice."""
        clock = FakeClock()
        children = [
            FakeChild(code=1, uptime=0.1, clock=clock),
            FakeChild(code=0, uptime=0.1, clock=clock),
        ]
        sleeper = RecordingSleeper()
        policy = BackoffPolicy(base_seconds=1.2, factor=2.0, reset_after_uptime_seconds=60.0)
        sup = ProcessSupervisor(
            launch=ScriptedLauncher(children),
            policy=policy,
            clock=clock,
            sleeper=sleeper,
            poll_interval_seconds=0.5,
        )

        sup.run()

        # 1.2s backoff at a 0.5s poll → 0.5 + 0.5 + 0.2 (the sum is the full delay).
        assert sleeper.delays == [0.5, 0.5, pytest.approx(0.2)]
        assert sum(sleeper.delays) == pytest.approx(1.2)

    def test_stop_mid_backoff_aborts_before_full_delay(self) -> None:
        """A stop that flips mid-backoff aborts the sleep after the CURRENT slice
        — it does NOT sleep the whole remaining delay first (promptness)."""
        clock = FakeClock()
        children = [
            FakeChild(code=1, uptime=0.1, clock=clock),
            FakeChild(code=0, uptime=0.1, clock=clock),  # would relaunch, but stop first
        ]
        # Flip the stop flag after the FIRST slice sleeps.
        sleeper = RecordingSleeper(stop_after_sleep=1)
        audit = RecordingAuditSink()
        policy = BackoffPolicy(base_seconds=10.0, factor=2.0, reset_after_uptime_seconds=60.0)
        sup = ProcessSupervisor(
            launch=ScriptedLauncher(children),
            policy=policy,
            audit=audit,
            clock=clock,
            sleeper=sleeper,
            stop_requested=lambda: sleeper.stop_flag[0],
            poll_interval_seconds=0.5,
        )

        result = sup.run()

        assert result.outcome is SupervisorOutcome.OPERATOR_STOP
        # Only ONE 0.5s slice slept out of a 10s backoff — the stop was prompt.
        assert sleeper.delays == [0.5]
        assert audit.rows[-1]["payload"]["phase"] == "during_backoff"


# ────────────────────────────────────────────────────────────────────────
# _SignalStop — the pure in-process latch (MINOR-2: un-pragma'd + tested)
# ────────────────────────────────────────────────────────────────────────


class TestSignalStopLatch:
    def test_starts_unstopped(self) -> None:
        stop = _SignalStop()
        assert stop() is False

    def test_trip_latches_true(self) -> None:
        stop = _SignalStop()
        stop.trip(15, None)  # SIGTERM
        assert stop() is True

    def test_latch_is_sticky_across_repeat_signals(self) -> None:
        """A second signal is a no-op — the latch stays True (idempotent)."""
        stop = _SignalStop()
        stop.trip(2, None)  # SIGINT
        stop.trip(15, None)  # SIGTERM again
        assert stop() is True

    def test_usable_as_stop_requested_predicate(self) -> None:
        """The latch plugs straight into the supervisor's stop_requested seam:
        tripped before launch → OPERATOR_STOP, never spawns a child."""
        stop = _SignalStop()
        stop.trip(15, None)
        launcher = ScriptedLauncher([FakeChild(code=0, uptime=1.0, clock=FakeClock())])
        sup = ProcessSupervisor(launch=launcher, stop_requested=stop)

        result = sup.run()

        assert result.outcome is SupervisorOutcome.OPERATOR_STOP
        assert launcher.launches == 0


# ────────────────────────────────────────────────────────────────────────
# Relaunch alert — rolling-window crash-loop escalation
# (DEC-SUPERVISOR-CRASHLOOP-ALERT-001)
# ────────────────────────────────────────────────────────────────────────


@dataclass
class RecordingAlerter:
    """Captures every alert call; matches the two-method ``RelaunchAlerter``."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    gave_up_calls: list[dict[str, Any]] = field(default_factory=list)

    def child_relaunched(
        self,
        *,
        child: str,
        exit_code: int,
        signal: int | None,
        relaunch_count: int,
        detail: str,
    ) -> bool:
        self.calls.append(
            {
                "child": child,
                "exit_code": exit_code,
                "signal": signal,
                "relaunch_count": relaunch_count,
                "detail": detail,
            }
        )
        return True

    def supervisor_gave_up(
        self,
        *,
        child: str,
        exit_code: int,
        signal: int | None,
        restarts: int,
        max_restarts: int,
    ) -> bool:
        self.gave_up_calls.append(
            {
                "child": child,
                "exit_code": exit_code,
                "signal": signal,
                "restarts": restarts,
                "max_restarts": max_restarts,
            }
        )
        return True


class RaisingAlerter:
    """An alerter whose every method always raises (best-effort proof)."""

    def child_relaunched(self, **_kwargs: Any) -> bool:
        raise RuntimeError("simulated alerter failure")

    def supervisor_gave_up(self, **_kwargs: Any) -> bool:
        raise RuntimeError("simulated give-up alerter failure")


class TestRelaunchAlert:
    def test_default_window_constant(self) -> None:
        """The rolling-window default is the module constant (10 minutes)."""
        assert DEFAULT_ALERT_WINDOW_SECONDS == 600.0
        sup = ProcessSupervisor(launch=lambda: FakeChild(code=0, uptime=0.0, clock=FakeClock()))
        assert sup.alert_window_seconds == DEFAULT_ALERT_WINDOW_SECONDS
        assert sup.alerter is None

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_rejects_nonpositive_alert_window(self, bad: float) -> None:
        with pytest.raises(ValueError, match="alert_window_seconds must be > 0"):
            ProcessSupervisor(
                launch=lambda: FakeChild(code=0, uptime=0.0, clock=FakeClock()),
                alert_window_seconds=bad,
            )

    def test_relaunch_fires_alert_with_fields(self) -> None:
        """One abnormal exit → one alert naming the subject, code, count, detail."""
        clock = FakeClock()
        children = [
            FakeChild(code=1, uptime=5.0, clock=clock),
            FakeChild(code=0, uptime=5.0, clock=clock),
        ]
        alerter = RecordingAlerter()
        sup = ProcessSupervisor(
            launch=ScriptedLauncher(children),
            subject="supervisor:sweep-daemon",
            clock=clock,
            sleeper=RecordingSleeper(),
            alerter=alerter,
        )

        result = sup.run()

        assert result.outcome is SupervisorOutcome.CLEAN_EXIT
        assert len(alerter.calls) == 1
        call = alerter.calls[0]
        assert call["child"] == "supervisor:sweep-daemon"
        assert call["exit_code"] == 1
        assert call["signal"] is None
        assert call["relaunch_count"] == 1
        assert "uptime 5.0s" in call["detail"]
        assert "restart #1 this run" in call["detail"]

    def test_signal_death_maps_negative_code_to_signal(self) -> None:
        """A POSIX signal death (negative returncode) surfaces the signal number."""
        clock = FakeClock()
        children = [
            FakeChild(code=-9, uptime=1.0, clock=clock),
            FakeChild(code=0, uptime=1.0, clock=clock),
        ]
        alerter = RecordingAlerter()
        sup = ProcessSupervisor(
            launch=ScriptedLauncher(children),
            clock=clock,
            sleeper=RecordingSleeper(),
            alerter=alerter,
        )

        sup.run()

        assert alerter.calls[0]["exit_code"] == -9
        assert alerter.calls[0]["signal"] == 9

    def test_in_window_counts_escalate(self) -> None:
        """Three crashes inside one window carry counts 1, 2, 3 — the alerter's
        escalation input (the WARN→CRITICAL decision itself lives in the alerter)."""
        clock = FakeClock()
        children = [
            FakeChild(code=-9, uptime=1.0, clock=clock),
            FakeChild(code=-9, uptime=1.0, clock=clock),
            FakeChild(code=-9, uptime=1.0, clock=clock),
            FakeChild(code=0, uptime=1.0, clock=clock),
        ]
        alerter = RecordingAlerter()
        sup = ProcessSupervisor(
            launch=ScriptedLauncher(children),
            clock=clock,
            sleeper=RecordingSleeper(),
            alerter=alerter,
        )

        sup.run()

        assert [c["relaunch_count"] for c in alerter.calls] == [1, 2, 3]

    def test_window_prunes_old_relaunches(self) -> None:
        """Relaunches spaced FURTHER apart than the window never accumulate —
        each alert counts 1, so no false CRITICAL from slow, isolated crashes."""
        clock = FakeClock()
        # Each child stays up 50s (clock advances inside wait), window is 10s,
        # so every prior relaunch timestamp has aged out by the next crash.
        children = [
            FakeChild(code=1, uptime=50.0, clock=clock),
            FakeChild(code=1, uptime=50.0, clock=clock),
            FakeChild(code=1, uptime=50.0, clock=clock),
            FakeChild(code=0, uptime=50.0, clock=clock),
        ]
        alerter = RecordingAlerter()
        sup = ProcessSupervisor(
            launch=ScriptedLauncher(children),
            clock=clock,
            sleeper=RecordingSleeper(),
            alerter=alerter,
            alert_window_seconds=10.0,
        )

        sup.run()

        assert [c["relaunch_count"] for c in alerter.calls] == [1, 1, 1]

    def test_raising_alerter_never_crashes_supervisor(self) -> None:
        """A raising alerter is swallowed + logged; relaunch/exit state unchanged."""
        clock = FakeClock()
        children = [
            FakeChild(code=1, uptime=1.0, clock=clock),
            FakeChild(code=0, uptime=1.0, clock=clock),
        ]
        audit = RecordingAuditSink()
        sup = ProcessSupervisor(
            launch=ScriptedLauncher(children),
            audit=audit,
            clock=clock,
            sleeper=RecordingSleeper(),
            alerter=RaisingAlerter(),
        )

        result = sup.run()

        assert result.outcome is SupervisorOutcome.CLEAN_EXIT
        assert result.restarts == 1
        assert _actions(audit) == [AUDIT_ACTION_RESTART, AUDIT_ACTION_CLEAN_EXIT]

    def test_no_alert_without_relaunch(self) -> None:
        """A clean first exit fires ZERO alerts (the alert lives ONLY in the
        relaunch branch)."""
        clock = FakeClock()
        alerter = RecordingAlerter()
        sup = ProcessSupervisor(
            launch=ScriptedLauncher([FakeChild(code=0, uptime=1.0, clock=clock)]),
            clock=clock,
            sleeper=RecordingSleeper(),
            alerter=alerter,
        )

        result = sup.run()

        assert result.outcome is SupervisorOutcome.CLEAN_EXIT
        assert alerter.calls == []

    def test_give_up_fires_terminal_alert_with_facts(self) -> None:
        """The give-up verdict fires ONE terminal ``supervisor_gave_up`` alert
        naming the child, the last death, and the exhausted cap — even when
        ``max_restarts`` is below the crash-loop escalation bar (the silent
        give-up gap the hostile review flagged, MINOR-3)."""
        clock = FakeClock()
        children = [
            FakeChild(code=1, uptime=1.0, clock=clock),
            FakeChild(code=1, uptime=1.0, clock=clock),  # hits the cap → give up
        ]
        alerter = RecordingAlerter()
        policy = BackoffPolicy(max_restarts=1)
        sup = ProcessSupervisor(
            launch=ScriptedLauncher(children),
            subject="supervisor:sweep-daemon",
            policy=policy,
            clock=clock,
            sleeper=RecordingSleeper(),
            alerter=alerter,
        )

        result = sup.run()

        assert result.outcome is SupervisorOutcome.GAVE_UP
        assert len(alerter.calls) == 1  # the single relaunch before the cap
        assert alerter.gave_up_calls == [
            {
                "child": "supervisor:sweep-daemon",
                "exit_code": 1,
                "signal": None,
                "restarts": 1,
                "max_restarts": 1,
            }
        ]

    def test_give_up_alert_maps_signal_death(self) -> None:
        """A give-up whose last death was by signal surfaces the signal number."""
        clock = FakeClock()
        children = [
            FakeChild(code=-9, uptime=1.0, clock=clock),
            FakeChild(code=-9, uptime=1.0, clock=clock),
        ]
        alerter = RecordingAlerter()
        sup = ProcessSupervisor(
            launch=ScriptedLauncher(children),
            policy=BackoffPolicy(max_restarts=1),
            clock=clock,
            sleeper=RecordingSleeper(),
            alerter=alerter,
        )

        result = sup.run()

        assert result.outcome is SupervisorOutcome.GAVE_UP
        assert alerter.gave_up_calls[0]["exit_code"] == -9
        assert alerter.gave_up_calls[0]["signal"] == 9

    def test_raising_give_up_alerter_never_changes_the_verdict(self) -> None:
        """A raising give-up alerter is swallowed + logged; the GAVE_UP verdict,
        audit stamps, and exit state stay byte-unchanged (best-effort proof)."""
        clock = FakeClock()
        children = [
            FakeChild(code=1, uptime=1.0, clock=clock),
            FakeChild(code=1, uptime=1.0, clock=clock),
        ]
        audit = RecordingAuditSink()
        sup = ProcessSupervisor(
            launch=ScriptedLauncher(children),
            policy=BackoffPolicy(max_restarts=1),
            audit=audit,
            clock=clock,
            sleeper=RecordingSleeper(),
            alerter=RaisingAlerter(),
        )

        result = sup.run()

        assert result.outcome is SupervisorOutcome.GAVE_UP
        assert result.restarts == 1
        assert result.last_exit_code == 1
        assert _actions(audit) == [AUDIT_ACTION_RESTART, AUDIT_ACTION_GIVE_UP]

    def test_give_up_without_alerter_is_silent_noop(self) -> None:
        """``alerter=None`` short-circuits the give-up alert — behaviour is
        byte-identical to the pre-alert supervisor."""
        clock = FakeClock()
        children = [
            FakeChild(code=1, uptime=1.0, clock=clock),
            FakeChild(code=1, uptime=1.0, clock=clock),
        ]
        sup = ProcessSupervisor(
            launch=ScriptedLauncher(children),
            policy=BackoffPolicy(max_restarts=1),
            clock=clock,
            sleeper=RecordingSleeper(),
        )

        result = sup.run()

        assert result.outcome is SupervisorOutcome.GAVE_UP
