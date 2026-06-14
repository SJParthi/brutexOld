"""Unit tests for the dashboard live-capture subprocess lifecycle.

Every collaborator (process spawn, PID-liveness, signal send, clock) is injected
with a fake — no real subprocess, no real signals, deterministic time. Covers
:mod:`brutex.dashboard.live_runner`.
"""

from __future__ import annotations

import json
import signal
import sys

from brutex.dashboard.live_runner import LiveRunner, _now_epoch


class _FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid


def _mk(tmp_path, *, alive, killer=lambda p, s: None, clock=lambda: 1000.0, pid=4242):
    return LiveRunner(
        state_path=tmp_path / "live.json",
        popen_factory=lambda argv: _FakeProc(pid),
        pid_alive=alive,
        killer=killer,
        clock=clock,
    )


# ───────────────────────────── status ────────────────────────────


def test_status_stopped_when_no_state(tmp_path) -> None:
    r = _mk(tmp_path, alive=lambda p: True)
    s = r.status()
    assert s.running is False and s.pid is None and s.as_dict()["running"] is False


def test_start_then_status_uptime(tmp_path) -> None:
    now = {"t": 1000.0}
    r = _mk(tmp_path, alive=lambda p: True, clock=lambda: now["t"])
    s = r.start(out="o.jsonl", log_file="l.log", seconds=60)
    assert s.running and s.pid == 4242
    assert s.argv[:4] == [sys.executable, "-m", "brutex.live", "capture"]
    now["t"] = 1007.5
    s2 = r.status()
    assert s2.running and s2.uptime_s == 7.5 and s2.out == "o.jsonl"


def test_start_is_idempotent_when_running(tmp_path) -> None:
    r = _mk(tmp_path, alive=lambda p: True, pid=111)
    r.start(out="a", log_file="la", seconds=60)
    s = r.start(out="b", log_file="lb", seconds=99)  # already running → no 2nd spawn
    assert s.pid == 111 and s.out == "a"  # unchanged


def test_status_reaps_dead_pid(tmp_path) -> None:
    flag = {"alive": True}
    r = _mk(tmp_path, alive=lambda p: flag["alive"])
    r.start(out="o", log_file="l", seconds=60)
    flag["alive"] = False
    s = r.status()
    assert s.running is False and not (tmp_path / "live.json").exists()  # reaped


# ───────────────────────────── stop ──────────────────────────────


def test_stop_escalates_sigterm_then_sigkill(tmp_path) -> None:
    sent: list[int] = []
    r = _mk(tmp_path, alive=lambda p: True, killer=lambda p, s: sent.append(s))
    r.start(out="o", log_file="l", seconds=60)
    s = r.stop()
    assert s.running is False and not (tmp_path / "live.json").exists()
    assert sent == [signal.SIGTERM, signal.SIGKILL]  # still alive after TERM → KILL


def test_stop_sigterm_only_when_process_dies(tmp_path) -> None:
    state = {"alive": True}
    sent: list[int] = []

    def killer(p: int, sig: int) -> None:
        sent.append(sig)
        if sig == signal.SIGTERM:
            state["alive"] = False  # TERM worked → no escalation

    r = _mk(tmp_path, alive=lambda p: state["alive"], killer=killer)
    r.start(out="o", log_file="l", seconds=60)
    r.stop()
    assert sent == [signal.SIGTERM]


def test_stop_when_not_running_is_noop(tmp_path) -> None:
    sent: list[int] = []
    r = _mk(tmp_path, alive=lambda p: True, killer=lambda p, s: sent.append(s))
    s = r.stop()
    assert s.running is False and sent == [] and not (tmp_path / "live.json").exists()


def test_stop_clears_dead_state_without_signal(tmp_path) -> None:
    sent: list[int] = []
    (tmp_path / "live.json").write_text(
        json.dumps({"pid": 99, "start_ts": 1.0, "argv": [], "out": "o"}), encoding="utf-8"
    )
    r = _mk(tmp_path, alive=lambda p: False, killer=lambda p, s: sent.append(s))
    s = r.stop()
    assert s.running is False and sent == [] and not (tmp_path / "live.json").exists()


def test_terminate_sigterm_process_lookup(tmp_path) -> None:
    def killer(p: int, s: int) -> None:
        raise ProcessLookupError

    r = _mk(tmp_path, alive=lambda p: True, killer=killer)
    r.start(out="o", log_file="l", seconds=1)
    assert r.stop().running is False  # SIGTERM raises → handled, never propagates


def test_terminate_sigkill_process_lookup(tmp_path) -> None:
    sent: list[int] = []

    def killer(p: int, sig: int) -> None:
        sent.append(sig)
        if sig == signal.SIGKILL:
            raise ProcessLookupError

    r = _mk(tmp_path, alive=lambda p: True, killer=killer)
    r.start(out="o", log_file="l", seconds=1)
    r.stop()
    assert sent == [signal.SIGTERM, signal.SIGKILL]  # KILL raises → handled


# ─────────────────────── state-file resilience ───────────────────


def test_corrupt_state_is_treated_as_stopped(tmp_path) -> None:
    (tmp_path / "live.json").write_text("not-json{", encoding="utf-8")
    r = _mk(tmp_path, alive=lambda p: True)
    assert r.status().running is False


def test_non_dict_state_is_treated_as_stopped(tmp_path) -> None:
    (tmp_path / "live.json").write_text("[1, 2]", encoding="utf-8")
    r = _mk(tmp_path, alive=lambda p: True)
    assert r.status().running is False


def test_unreadable_state_is_treated_as_stopped(tmp_path) -> None:
    (tmp_path / "live.json").mkdir()  # a directory → read_text raises OSError
    r = _mk(tmp_path, alive=lambda p: True)
    assert r.status().running is False


# ─────────────────────────── default clock ───────────────────────


def test_default_clock_runs(tmp_path) -> None:
    """The production ``_now_epoch`` default is exercised end-to-end."""
    assert isinstance(_now_epoch(), float)
    r = LiveRunner(
        state_path=tmp_path / "live.json",
        popen_factory=lambda argv: _FakeProc(7),
        pid_alive=lambda p: True,
    )  # clock + killer defaulted
    s = r.start(out="o", log_file="l", seconds=1)
    assert s.running and isinstance(s.uptime_s, float)
