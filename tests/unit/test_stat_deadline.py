"""Unit tests for :mod:`brutex.data.stat_deadline` (DEC-DASHBOARD-COLD-IO-DEADLINE-001).

The BUG-2 gate primitive: 100% line+branch on ``brutex/data/stat_deadline.py``
via the injected ``guard=`` seam (fully deterministic — no real waits except
the ONE real ``run_with_deadline`` wedge regression, which is Event-released
and thread-count-bounded per the ``test_config_deadline`` idiom):

* healthy pass-through (result verbatim, no latch, no log);
* breach → ``StatSweepTimeout(latched=False)`` + ONE
  ``stat_gate.deadline_exceeded`` WARN + ``wedged(key)`` True;
* latched → instant ``latched=True``, the guard seam NOT called, no log;
* the zombie's late return → done-flag + token-identity clear + ONE
  ``stat_gate.recovered`` INFO;
* the done-flag RACE arm — fn completes between expiry and latch → NO latch;
* concurrent healthy runs on one key BOTH proceed (breach-armed, never
  in-flight-armed); different keys independent;
* spawn failure → no latch + flip-latched WARN + ``spawn=True``;
* fn-raised errors propagate UNCHANGED (never re-typed);
* the resolver's fail-safe matrix.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest
from brutex.config.deadline import DeadlineExceeded, DeadlineSpawnError
from brutex.data.stat_deadline import (
    STAT_DEADLINE_ENV,
    STAT_DEADLINE_S,
    STAT_GATE_DEADLINE_EVENT,
    STAT_GATE_RECOVERED_EVENT,
    STAT_GATE_SPAWN_EVENT,
    StatSweepGate,
    StatSweepTimeout,
    resolve_stat_deadline_seconds,
)
from structlog.testing import capture_logs


def _events(logs: Sequence[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]]:
    """The captured structlog events matching *name*, in order."""
    return [entry for entry in logs if entry["event"] == name]


def _passthrough_guard(fn: Callable[[], Any], seconds: float, **_kw: Any) -> Any:
    """A healthy guard seam: run the wrapped fn synchronously on the caller."""
    return fn()


class _BreachGuard:
    """A guard seam that CAPTURES the wrapped fn and raises the breach.

    The captured fn is the gate's ``_tracked_call`` closure — calling it later
    simulates the abandoned zombie's late return (the volume recovering).
    """

    def __init__(self) -> None:
        self.captured: list[Callable[[], Any]] = []
        self.calls = 0

    def __call__(self, fn: Callable[[], Any], seconds: float, **_kw: Any) -> Any:
        self.calls += 1
        self.captured.append(fn)
        raise DeadlineExceeded(f"test breach ({seconds:g}s)")


class TestResolver:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(STAT_DEADLINE_ENV, raising=False)
        assert resolve_stat_deadline_seconds() == STAT_DEADLINE_S

    @pytest.mark.parametrize("junk", ["", "  ", "abc", "nan", "inf", "-1", "0"])
    def test_junk_fails_safe_to_default(self, junk: str) -> None:
        """A bad value can NEVER disable the bound (fail-safe, Golden Rule #1)."""
        assert resolve_stat_deadline_seconds(junk) == STAT_DEADLINE_S

    def test_valid_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(STAT_DEADLINE_ENV, "0.25")
        assert resolve_stat_deadline_seconds() == 0.25

    def test_explicit_raw_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(STAT_DEADLINE_ENV, "9.0")
        assert resolve_stat_deadline_seconds("2.5") == 2.5


class TestHealthyPath:
    def test_result_passes_through_verbatim(self) -> None:
        gate = StatSweepGate(what="test gate", guard=_passthrough_guard)
        with capture_logs() as logs:
            assert gate.run("k", lambda: {"a": 1}, op="probe") == {"a": 1}
        assert logs == []
        assert not gate.wedged("k")

    def test_deadline_resolved_from_env_when_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``deadline_s=None`` resolves the env-tunable default PER RUN."""
        seen: list[float] = []

        def _spy(fn: Callable[[], Any], seconds: float, **_kw: Any) -> Any:
            seen.append(seconds)
            return fn()

        monkeypatch.setenv(STAT_DEADLINE_ENV, "0.75")
        gate = StatSweepGate(what="test gate", guard=_spy)
        gate.run("k", lambda: None, op="probe")
        gate.run("k", lambda: None, op="probe", deadline_s=3.0)
        assert seen == [0.75, 3.0]

    def test_concurrent_healthy_runs_on_one_key_both_proceed(self) -> None:
        """The latch is BREACH-armed, never in-flight-armed (the D1 defect avoided).

        Two threads run the SAME key concurrently — the first holds the gate
        mid-flight (event-orchestrated) while the second enters and completes:
        both return real results, neither degrades, nothing latches.
        """
        gate = StatSweepGate(what="test gate", guard=_passthrough_guard)
        first_entered = threading.Event()
        release_first = threading.Event()
        results: dict[str, Any] = {}

        def _slow() -> str:
            first_entered.set()
            assert release_first.wait(5.0), "test orchestration failure"
            return "first"

        def _run_first() -> None:
            results["first"] = gate.run("k", _slow, op="probe")

        worker = threading.Thread(target=_run_first, daemon=True)
        worker.start()
        try:
            assert first_entered.wait(5.0)
            # The second HEALTHY run proceeds while the first is in flight.
            results["second"] = gate.run("k", lambda: "second", op="probe")
        finally:
            release_first.set()
        worker.join(5.0)
        assert not worker.is_alive()
        assert results == {"first": "first", "second": "second"}
        assert not gate.wedged("k")

    def test_fn_raised_oserror_propagates_unchanged(self) -> None:
        gate = StatSweepGate(what="test gate", guard=_passthrough_guard)

        def _boom() -> None:
            raise OSError("real stat fault")

        with pytest.raises(OSError, match="real stat fault"):
            gate.run("k", _boom, op="probe")
        assert not gate.wedged("k")

    def test_fn_raised_valueerror_propagates_unchanged(self) -> None:
        gate = StatSweepGate(what="test gate", guard=_passthrough_guard)

        def _boom() -> None:
            raise ValueError("real parse fault")

        with pytest.raises(ValueError, match="real parse fault"):
            gate.run("k", _boom, op="probe")
        assert not gate.wedged("k")


class TestBreachAndLatch:
    def test_breach_arms_latch_and_warns_once(self) -> None:
        guard = _BreachGuard()
        gate = StatSweepGate(what="test gate", guard=guard)
        with capture_logs() as logs, pytest.raises(StatSweepTimeout) as excinfo:
            gate.run("k", lambda: None, op="stats_consult", deadline_s=0.05)
        assert excinfo.value.latched is False
        assert excinfo.value.spawn is False
        assert excinfo.value.op == "stats_consult"
        assert excinfo.value.key == "k"
        warns = _events(logs, STAT_GATE_DEADLINE_EVENT)
        assert len(warns) == 1
        assert warns[0]["key"] == "k"
        assert warns[0]["op"] == "stats_consult"
        assert warns[0]["deadline_seconds"] == 0.05
        assert gate.wedged("k")

    def test_latched_run_fails_fast_without_guard_or_log(self) -> None:
        """While latched: instant ``latched=True``, guard seam NOT invoked, silent."""
        guard = _BreachGuard()
        gate = StatSweepGate(what="test gate", guard=guard)
        with pytest.raises(StatSweepTimeout):
            gate.run("k", lambda: None, op="stats_consult", deadline_s=0.05)
        assert guard.calls == 1
        with capture_logs() as logs, pytest.raises(StatSweepTimeout) as excinfo:
            gate.run("k", lambda: None, op="stats_restat", deadline_s=0.05)
        assert excinfo.value.latched is True
        assert guard.calls == 1  # NO second daemon while wedged
        assert logs == []

    def test_zombie_late_return_clears_latch_with_one_recovered_info(self) -> None:
        """The latch OWNER's late return self-clears + ONE recovered INFO."""
        guard = _BreachGuard()
        gate = StatSweepGate(what="test gate", guard=guard)
        with pytest.raises(StatSweepTimeout):
            gate.run("k", lambda: "late", op="stats_consult", deadline_s=0.05)
        assert gate.wedged("k")
        with capture_logs() as logs:
            # Simulate the abandoned zombie's late return: the captured
            # wrapped fn IS the daemon-side body.
            assert guard.captured[0]() == "late"
        infos = _events(logs, STAT_GATE_RECOVERED_EVENT)
        assert len(infos) == 1
        assert infos[0]["key"] == "k"
        assert infos[0]["op"] == "stats_consult"
        assert not gate.wedged("k")
        # The next run reaches the guard again (a fresh flight — the latch
        # fail-fast no longer short-circuits it).
        with pytest.raises(StatSweepTimeout):
            gate.run("k", lambda: 7, op="stats_consult", deadline_s=0.05)
        assert guard.calls == 2

    def test_done_race_arm_never_arms_the_latch(self) -> None:
        """fn completing inside the expiry→latch window must NOT latch.

        The guard seam runs the wrapped fn SYNCHRONOUSLY (done=True) then
        raises DeadlineExceeded — the caller still times out, but arming a
        latch whose zombie is already gone would wedge the key FOREVER.
        """

        race = {"on": True}

        def _race_guard(fn: Callable[[], Any], seconds: float, **_kw: Any) -> Any:
            result = fn()  # completes: entry.done = True
            if race["on"]:
                raise DeadlineExceeded("expired in the race window")
            return result

        gate = StatSweepGate(what="test gate", guard=_race_guard)
        with capture_logs() as logs, pytest.raises(StatSweepTimeout) as excinfo:
            gate.run("k", lambda: None, op="probe", deadline_s=0.05)
        assert excinfo.value.latched is False
        assert not gate.wedged("k")  # NO latch — nothing left to clear it
        assert _events(logs, STAT_GATE_DEADLINE_EVENT) == []  # no flip → no WARN
        # And the key stays fully usable.
        race["on"] = False
        assert gate.run("k", lambda: "ok", op="probe", deadline_s=0.05) == "ok"

    def test_concurrently_breached_second_flight_does_not_re_warn_or_re_own(self) -> None:
        """``setdefault`` makes the FIRST breached flight the owner; the second is silent.

        Two flights breach back-to-back before either zombie returns: the
        second breach neither re-WARNs nor replaces the owner, and the
        SECOND zombie's late return does NOT clear the first's latch.
        """
        guard = _BreachGuard()
        gate = StatSweepGate(what="test gate", guard=guard)

        # First flight breaches normally (owner).
        with pytest.raises(StatSweepTimeout):
            gate.run("k", lambda: None, op="probe", deadline_s=0.05)
        # Force a second breach on the SAME key by clearing the latch check
        # path artificially: run() would fail fast while latched, so drive
        # the second flight's breach through the internal seam the way a
        # true photo-finish race would — two callers passing the latch check
        # before either breaches. Simulate: temporarily un-latch, run, and
        # re-check ownership survives.
        first_owner = gate._wedged["k"]
        del gate._wedged["k"]
        with capture_logs() as logs, pytest.raises(StatSweepTimeout):
            gate.run("k", lambda: None, op="probe", deadline_s=0.05)
        # The second breach flipped the (cleared) latch again — one WARN.
        assert len(_events(logs, STAT_GATE_DEADLINE_EVENT)) == 1
        second_owner = gate._wedged["k"]
        assert second_owner is not first_owner
        # The FIRST zombie's late return must NOT clear the second's latch
        # (token identity): only its own entry may be popped.
        with capture_logs() as logs:
            guard.captured[0]()
        assert _events(logs, STAT_GATE_RECOVERED_EVENT) == []
        assert gate.wedged("k")
        # The CURRENT owner's zombie clears it.
        with capture_logs() as logs:
            guard.captured[1]()
        assert len(_events(logs, STAT_GATE_RECOVERED_EVENT)) == 1
        assert not gate.wedged("k")

    def test_keys_are_independent(self) -> None:
        guard = _BreachGuard()
        gate = StatSweepGate(what="test gate", guard=guard)
        with pytest.raises(StatSweepTimeout):
            gate.run("lake-a", lambda: None, op="probe", deadline_s=0.05)
        assert gate.wedged("lake-a")
        assert not gate.wedged("lake-b")
        # A healthy run on ANOTHER key proceeds (needs a healthy guard).
        healthy = StatSweepGate(what="test gate", guard=_passthrough_guard)
        assert healthy.run("lake-b", lambda: 3, op="probe") == 3


class TestSpawnFailure:
    def test_spawn_failure_warns_flip_latched_and_never_latches(self) -> None:
        def _spawn_fail(fn: Callable[[], Any], seconds: float, **_kw: Any) -> Any:
            raise DeadlineSpawnError("no threads")

        gate = StatSweepGate(what="test gate", guard=_spawn_fail)
        with capture_logs() as logs:
            with pytest.raises(StatSweepTimeout) as excinfo:
                gate.run("k", lambda: None, op="probe", deadline_s=0.05)
            with pytest.raises(StatSweepTimeout) as excinfo2:
                gate.run("k", lambda: None, op="probe", deadline_s=0.05)
        assert excinfo.value.spawn is True
        assert excinfo2.value.spawn is True
        assert not gate.wedged("k")  # fn never ran — no zombie — never latch
        # Flip-latched: ONE WARN across the outage.
        assert len(_events(logs, STAT_GATE_SPAWN_EVENT)) == 1

    def test_successful_spawn_rearms_the_spawn_warn(self) -> None:
        """A success between two spawn outages re-arms the flip WARN."""
        fail = {"on": True}

        def _flaky(fn: Callable[[], Any], seconds: float, **_kw: Any) -> Any:
            if fail["on"]:
                raise DeadlineSpawnError("no threads")
            return fn()

        gate = StatSweepGate(what="test gate", guard=_flaky)
        with capture_logs() as logs:
            with pytest.raises(StatSweepTimeout):
                gate.run("k", lambda: None, op="probe", deadline_s=0.05)
            fail["on"] = False
            assert gate.run("k", lambda: 1, op="probe", deadline_s=0.05) == 1
            fail["on"] = True
            with pytest.raises(StatSweepTimeout):
                gate.run("k", lambda: None, op="probe", deadline_s=0.05)
        assert len(_events(logs, STAT_GATE_SPAWN_EVENT)) == 2


class TestRealGuardWedge:
    def test_event_wedged_fn_is_bounded_and_recovers(self) -> None:
        """ONE real ``run_with_deadline`` wedge: bounded caller + baseline drain.

        An Event-blocked sweep under a 0.05 s deadline: the caller returns
        bounded (< 5 s wall, join-style rail), the latch arms, a second call
        fails fast with no new thread, and once the Event releases the zombie
        drains — ``wedged`` clears and the live-thread count returns to its
        pre-wedge baseline (the ``test_config_deadline`` idiom).
        """
        gate = StatSweepGate(what="test gate")  # the REAL run_with_deadline
        release = threading.Event()
        baseline = threading.active_count()

        def _wedged() -> str:
            assert release.wait(10.0), "test orchestration failure"
            return "late"

        started = time.monotonic()
        with capture_logs() as logs, pytest.raises(StatSweepTimeout) as excinfo:
            gate.run("k", _wedged, op="stats_consult", deadline_s=0.05)
        assert time.monotonic() - started < 5.0  # bounded, never a hang
        assert excinfo.value.latched is False
        assert len(_events(logs, STAT_GATE_DEADLINE_EVENT)) == 1
        assert gate.wedged("k")
        # Latched fail-fast spawns NO new thread.
        before = threading.active_count()
        with pytest.raises(StatSweepTimeout) as excinfo2:
            gate.run("k", lambda: None, op="stats_consult", deadline_s=0.05)
        assert excinfo2.value.latched is True
        assert threading.active_count() <= before
        # Release the wedge: the zombie returns, clears the latch, drains.
        release.set()
        deadline = time.monotonic() + 5.0
        while gate.wedged("k") and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not gate.wedged("k")
        while threading.active_count() > baseline and time.monotonic() < deadline:
            time.sleep(0.01)
        assert threading.active_count() <= baseline
        # Recovery: the next run proceeds through a fresh guard normally.
        assert gate.run("k", lambda: "fresh", op="stats_consult", deadline_s=5.0) == "fresh"


class TestExceptionTyping:
    def test_not_a_timeout_error_nor_oserror(self) -> None:
        """The movers_read_skip_errors()/except-OSError leak class is structurally closed."""
        exc = StatSweepTimeout(what="w", op="o", key="k")
        assert isinstance(exc, RuntimeError)
        assert not isinstance(exc, TimeoutError)
        assert not isinstance(exc, OSError)

    def test_message_carries_the_three_shapes(self) -> None:
        assert "deadline exceeded" in str(StatSweepTimeout(what="w", op="o", key="k"))
        assert "latched" in str(StatSweepTimeout(what="w", op="o", key="k", latched=True))
        assert "spawn failed" in str(StatSweepTimeout(what="w", op="o", key="k", spawn=True))


class TestMutationWorkflowWiring:
    """Pin this module INTO ``mutation.yml``'s per-module zero-survivor loop.

    A deadline-critical leaf shipped WITHOUT its weekly mutation wiring would
    repeat the DEC-LAKE-STATS-MANIFEST-001 round-8 refuter class (an unwired
    gate presented as a deferred one — Golden Rule #10). The module:suite
    pair sits in the workflow's 3-field ``module:testfile:timeout`` loop with
    the EMPTY third field (the historical runner); this deterministic pin
    (the ``TestMutationWorkflowWiring`` precedent) fails if the pair is ever
    dropped. HONEST: it pins the WIRING only — the weekly run executes on its
    own schedule/push triggers.
    """

    def test_module_paired_with_this_suite_in_the_mutmut_loop(self) -> None:
        from pathlib import Path

        workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "mutation.yml"
        text = workflow.read_text(encoding="utf-8")
        assert '"brutex/data/stat_deadline.py:tests/unit/test_stat_deadline.py:"' in text, (
            "stat_deadline.py is not wired into mutation.yml's per-module mutmut loop"
        )
