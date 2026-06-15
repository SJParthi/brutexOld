"""Unit tests for the dashboard live-capture subprocess lifecycle.

Every collaborator (process spawn, PID-liveness, signal send, clock, creds) is
injected with a fake — no real subprocess, no real signals, deterministic time,
no real credentials file. Covers :mod:`brutex.dashboard.live_runner`.
"""

from __future__ import annotations

import json
import signal
import sys
from collections.abc import Mapping

from brutex.dashboard import live_runner as lr
from brutex.dashboard.live_runner import LiveRunner, _now_epoch


class _FakeProc:
    """A stand-in for ``subprocess.Popen`` — ``poll()`` drives crash-detection."""

    def __init__(self, pid: int, *, returncode: int | None = None) -> None:
        self.pid = pid
        self._returncode = returncode

    def poll(self) -> int | None:
        return self._returncode  # None → still running; an int → exited


def _mk(
    tmp_path,
    *,
    alive,
    killer=lambda p, s: None,
    clock=lambda: 1000.0,
    pid=4242,
    returncode=None,
    cred_env=lambda: {},
    manifest_writer=lambda path: None,
    sleeper=lambda s: None,
):
    return LiveRunner(
        state_path=tmp_path / "live.json",
        popen_factory=lambda argv, env: _FakeProc(pid, returncode=returncode),
        pid_alive=alive,
        killer=killer,
        clock=clock,
        cred_env=cred_env,
        manifest_writer=manifest_writer,
        sleeper=sleeper,
    )


def _start(runner, **overrides):
    """Call ``runner.start`` with the now-required report/manifest args filled in.

    The N-connection capture (DEC-LIVE-DASHBOARD-WIRE-001) made ``report`` +
    ``manifest`` required; this helper centralises the defaults so each test only
    overrides what it asserts on.
    """
    kwargs = {
        "out": "o",
        "log_file": "l",
        "report": "r.md",
        "manifest": "m.json",
        "seconds": 60,
    }
    kwargs.update(overrides)
    return runner.start(**kwargs)


# ───────────────────────────── status ────────────────────────────


def test_status_stopped_when_no_state(tmp_path) -> None:
    r = _mk(tmp_path, alive=lambda p: True)
    s = r.status()
    assert s.running is False and s.pid is None and s.as_dict()["running"] is False


def test_start_then_status_uptime(tmp_path) -> None:
    now = {"t": 1000.0}
    r = _mk(tmp_path, alive=lambda p: True, clock=lambda: now["t"])
    s = _start(r, out="o.jsonl", log_file="l.log")
    assert s.running and s.pid == 4242
    assert s.argv is not None
    # The toggle now spawns the N-connection `capture-all` (DEC-LIVE-DASHBOARD-WIRE-001).
    assert s.argv[:4] == [sys.executable, "-m", "brutex.live", "capture-all"]
    assert s.feed == "both" and s.report == "r.md"  # feed/report surfaced for the chip + proof
    now["t"] = 1007.5
    s2 = r.status()
    assert s2.running and s2.uptime_s == 7.5 and s2.out == "o.jsonl"
    assert s2.feed == "both" and s2.report == "r.md"


def test_start_writes_manifest_and_passes_capture_all_flags(tmp_path) -> None:
    """start() writes the manifest then spawns capture-all --manifest/--report.

    REGRESSION (the toggle launched a CRASHING command): the spawned argv MUST be
    parseable by the real ``brutex.live`` CLI parser. The old wiring passed
    ``--feed both`` to ``capture-all`` (which has no ``--feed`` option), so argparse
    rejected the whole spawn and the capture crashed on every launch. This test
    feeds the EXACT spawned args through the real parser, so a flag that the
    subcommand does not accept fails here instead of only at runtime on the Mac.
    """
    written: list[str] = []
    captured_argv: list[str] = []

    def popen(argv, env):
        captured_argv.extend(argv)
        return _FakeProc(9)

    r = LiveRunner(
        state_path=tmp_path / "live.json",
        popen_factory=popen,
        pid_alive=lambda p: True,
        clock=lambda: 1.0,
        cred_env=lambda: {},
        manifest_writer=written.append,
    )
    _start(r, manifest="man.json", report="rep.md")
    assert written == ["man.json"]  # manifest written exactly once, before the spawn
    assert "capture-all" in captured_argv
    assert captured_argv[captured_argv.index("--manifest") + 1] == "man.json"
    assert captured_argv[captured_argv.index("--report") + 1] == "rep.md"
    # capture-all is inherently dual-feed → it has NO --feed option; the toggle must
    # NOT pass one (the bug that crashed every launch).
    assert "--feed" not in captured_argv
    # And the EXACT spawned args parse cleanly through the real CLI parser (the guard
    # that would have caught the --feed crash before it ever reached the operator).
    from brutex.live.__main__ import _build_parser

    cli_args = captured_argv[captured_argv.index("brutex.live") + 1 :]
    parsed = _build_parser().parse_args(cli_args)
    assert parsed.command == "capture-all"


def test_start_with_lake_root_appends_realtime_fold_flags(tmp_path) -> None:
    """start(lake_root=…) appends --lake-root/--roll-max-seconds/--run-id (real-time fold).

    REGRESSION GUARD (#743/#744 lesson — a flag capture-all REJECTS crashes the
    launch): the EXACT spawned argv is fed through the REAL CLI parser and must
    parse to ``command == "capture-all"`` with the lake-root resolved
    (DEC-LIVE-REALTIME-FOLD-001).
    """
    captured_argv: list[str] = []

    def popen(argv, env):
        captured_argv.extend(argv)
        return _FakeProc(9)

    r = LiveRunner(
        state_path=tmp_path / "live.json",
        popen_factory=popen,
        pid_alive=lambda p: True,
        clock=lambda: 1.0,
        cred_env=lambda: {},
        manifest_writer=lambda path: None,
    )
    _start(r, lake_root="data/lake")
    assert captured_argv[captured_argv.index("--lake-root") + 1] == "data/lake"
    assert captured_argv[captured_argv.index("--roll-max-seconds") + 1] == "5"
    assert captured_argv[captured_argv.index("--run-id") + 1] == "live"
    # The EXACT spawned args parse cleanly through the real capture-all parser, so a
    # rejected flag fails HERE, not at runtime on the operator's Mac.
    from brutex.live.__main__ import _build_parser

    cli_args = captured_argv[captured_argv.index("brutex.live") + 1 :]
    parsed = _build_parser().parse_args(cli_args)
    assert parsed.command == "capture-all"
    assert parsed.lake_root == "data/lake" and parsed.roll_max_seconds == 5.0


def test_start_without_lake_root_omits_fold_flags(tmp_path) -> None:
    """start() with no lake_root spawns plain capture-all (no --lake-root flag)."""
    captured_argv: list[str] = []

    def popen(argv, env):
        captured_argv.extend(argv)
        return _FakeProc(9)

    r = LiveRunner(
        state_path=tmp_path / "live.json",
        popen_factory=popen,
        pid_alive=lambda p: True,
        clock=lambda: 1.0,
        cred_env=lambda: {},
        manifest_writer=lambda path: None,
    )
    _start(r)  # lake_root defaults to None
    assert "--lake-root" not in captured_argv
    assert "--roll-max-seconds" not in captured_argv
    # Still a valid capture-all spawn.
    from brutex.live.__main__ import _build_parser

    cli_args = captured_argv[captured_argv.index("brutex.live") + 1 :]
    assert _build_parser().parse_args(cli_args).lake_root is None


def test_watch_respawns_with_remembered_lake_root(tmp_path) -> None:
    """The watchdog re-spawn carries the lake_root from _last_start (re-fold on crash)."""
    spawns: list[list[str]] = []

    def popen(argv, env):
        spawns.append(list(argv))
        return _DeadProc(700 + len(spawns))

    r = LiveRunner(
        state_path=tmp_path / "live.json",
        popen_factory=popen,
        pid_alive=lambda p: False,
        clock=lambda: 1.0,
        cred_env=lambda: {},
        manifest_writer=lambda path: None,
        sleeper=lambda s: None,
    )
    _start(r, lake_root="data/lake")  # toggle ON with a lake root; child is dead
    assert r.watch(max_restarts=1) == 1
    # The watchdog re-spawn (the 2nd argv) still carries --lake-root data/lake.
    respawn = spawns[-1]
    assert respawn[respawn.index("--lake-root") + 1] == "data/lake"
    r = _mk(tmp_path, alive=lambda p: True, pid=111)
    _start(r, out="a", log_file="la")
    s = _start(r, out="b", log_file="lb", seconds=99)  # already running → no 2nd spawn
    assert s.pid == 111 and s.out == "a"  # unchanged


def test_status_reaps_crashed_child(tmp_path) -> None:
    """A child that exits (poll → returncode) reads STOPPED — never a zombie 'running'."""
    r = _mk(tmp_path, alive=lambda p: True, returncode=1)  # the capture crashed
    _start(r)
    s = r.status()
    assert s.running is False and not (tmp_path / "live.json").exists()  # reaped


def test_status_adopts_running_after_restart(tmp_path) -> None:
    """No in-process handle (dashboard restart) → fall back to OS liveness = running."""
    (tmp_path / "live.json").write_text(
        json.dumps({"pid": 99, "start_ts": 1.0, "started_at": "x", "argv": ["a"], "out": "o"}),
        encoding="utf-8",
    )
    r = _mk(tmp_path, alive=lambda p: True, clock=lambda: 1010.0)
    s = r.status()
    assert s.running and s.pid == 99 and s.uptime_s == 1009.0


def test_status_reaps_dead_pid_after_restart(tmp_path) -> None:
    """No handle + OS says the pid is gone → reaped (stopped)."""
    (tmp_path / "live.json").write_text(
        json.dumps({"pid": 99, "start_ts": 1.0, "started_at": "x", "argv": [], "out": "o"}),
        encoding="utf-8",
    )
    r = _mk(tmp_path, alive=lambda p: False)
    s = r.status()
    assert s.running is False and not (tmp_path / "live.json").exists()


# ───────────────────────── credentials bridge ────────────────────


def test_start_injects_dashboard_creds_into_child_env(tmp_path) -> None:
    """The saved Groww creds ride in the child ENV (never argv → ps stays clean)."""
    captured_argv: list[str] = []
    captured_env: dict[str, str] = {}

    def popen(argv: list[str], env: Mapping[str, str]) -> _FakeProc:
        captured_argv.extend(argv)
        captured_env.update(env)
        return _FakeProc(5)

    r = LiveRunner(
        state_path=tmp_path / "live.json",
        popen_factory=popen,
        pid_alive=lambda p: True,
        clock=lambda: 1.0,
        cred_env=lambda: {"BRUTEX_GROWW_API_KEY": "K", "BRUTEX_GROWW_TOTP_SECRET": "S"},
        manifest_writer=lambda path: None,
    )
    _start(r, seconds=1)
    assert captured_env["BRUTEX_GROWW_API_KEY"] == "K"
    assert captured_env["BRUTEX_GROWW_TOTP_SECRET"] == "S"
    # creds are NOT on argv (ps hygiene).
    assert "K" not in captured_argv and "S" not in captured_argv


def test_load_cred_env_branches(monkeypatch) -> None:
    """`_load_cred_env`: no creds → {}; present → env vars; unreadable → {} (swallowed)."""
    monkeypatch.setattr(lr, "load_groww_credentials", lambda: None)
    assert lr._load_cred_env() == {}

    class _Creds:
        api_key = "K"
        totp_secret = "S"

    monkeypatch.setattr(lr, "load_groww_credentials", _Creds)
    env = lr._load_cred_env()
    assert env == {"BRUTEX_GROWW_API_KEY": "K", "BRUTEX_GROWW_TOTP_SECRET": "S"}

    def _raise():
        raise lr.CredentialsError("bad perms")

    monkeypatch.setattr(lr, "load_groww_credentials", _raise)
    assert lr._load_cred_env() == {}


# ───────────────────────────── stop ──────────────────────────────


def test_stop_escalates_sigterm_then_sigkill(tmp_path) -> None:
    sent: list[int] = []
    r = _mk(tmp_path, alive=lambda p: True, killer=lambda p, s: sent.append(s))
    _start(r)
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
    _start(r)
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
    _start(r, seconds=1)
    assert r.stop().running is False  # SIGTERM raises → handled, never propagates


def test_terminate_sigkill_process_lookup(tmp_path) -> None:
    sent: list[int] = []

    def killer(p: int, sig: int) -> None:
        sent.append(sig)
        if sig == signal.SIGKILL:
            raise ProcessLookupError

    r = _mk(tmp_path, alive=lambda p: True, killer=killer)
    _start(r, seconds=1)
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
        popen_factory=lambda argv, env: _FakeProc(7),
        pid_alive=lambda p: True,
        cred_env=lambda: {},
        manifest_writer=lambda path: None,
    )  # clock + killer + sleeper defaulted
    s = _start(r, seconds=1)
    assert s.running and isinstance(s.uptime_s, float)


def test_default_manifest_writer_is_noop(tmp_path) -> None:
    """The default ``manifest_writer`` no-op runs end-to-end (no real store needed)."""
    assert lr._noop_manifest_writer("anything.json") is None
    r = LiveRunner(
        state_path=tmp_path / "live.json",
        popen_factory=lambda argv, env: _FakeProc(8),
        pid_alive=lambda p: True,
        clock=lambda: 1.0,
        cred_env=lambda: {},
    )  # manifest_writer + sleeper defaulted
    assert _start(r, seconds=1).running


# ───────────────────────────── watchdog ──────────────────────────


class _DeadProc:
    """A subprocess that polls DEAD immediately (the crash the watchdog catches)."""

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> int | None:
        return 1  # exited → status().running is False


def test_watch_restarts_dead_capture_with_backoff(tmp_path) -> None:
    """While ON, a dead capture is restarted with doubling backoff (bounded)."""
    spawns: list[int] = []
    waits: list[float] = []

    def popen(argv, env):
        spawns.append(1)
        return _DeadProc(700 + len(spawns))

    r = LiveRunner(
        state_path=tmp_path / "live.json",
        popen_factory=popen,
        pid_alive=lambda p: False,  # the OS confirms the crashed pid is gone
        clock=lambda: 1.0,
        cred_env=lambda: {},
        manifest_writer=lambda path: None,
        sleeper=waits.append,
    )
    _start(r)  # toggle ON (1 spawn) — but the child is immediately dead
    restarts = r.watch(max_restarts=3, base_backoff_s=1.0, max_backoff_s=4.0)
    assert restarts == 3
    assert waits == [1.0, 2.0, 4.0]  # doubling, capped at 4.0
    assert len(spawns) == 1 + 3  # the initial start + 3 watchdog restarts


def test_watch_stops_when_feed_turned_off(tmp_path) -> None:
    """A clean stop() clears the spawn — the watchdog does NOT resurrect it."""
    r = _mk(tmp_path, alive=lambda p: True)
    _start(r)
    r.stop()  # operator turned the toggle OFF
    assert r.watch(max_restarts=5) == 0  # nothing resurrected


def test_watch_noop_when_feed_healthy(tmp_path) -> None:
    """A running capture is left alone (no restart)."""
    r = _mk(tmp_path, alive=lambda p: True)  # _FakeProc.poll → None = running
    _start(r)
    assert r.watch(max_restarts=5) == 0


def test_watch_never_ran_returns_zero(tmp_path) -> None:
    """watch() with no start in effect (toggle never ON) restarts nothing."""
    r = _mk(tmp_path, alive=lambda p: True)
    assert r.watch(max_restarts=5) == 0


def test_watch_aborts_when_stopped_during_backoff(tmp_path) -> None:
    """A stop() during the backoff wait aborts the pending restart (no resurrection)."""
    r = _mk(tmp_path, alive=lambda p: False, returncode=1)  # child crashes immediately

    def stop_during_wait(_seconds: float) -> None:
        r.stop()  # operator turns OFF mid-backoff

    r._sleeper = stop_during_wait
    _start(r)
    assert r.watch(max_restarts=3) == 0  # the wait's stop() aborts before any respawn
