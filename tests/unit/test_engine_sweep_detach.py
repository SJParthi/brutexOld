"""Unit suite for :mod:`brutex.engine.sweep_detach` (R7 tail — DEC-SWEEP-UNKILLABLE-CORE-001).

Pins the detached spawn contract with a REAL stub child —
``start_new_session=True`` (the child is its own session leader), stdin
``DEVNULL``, stdout+stderr redirected to the ``runs/`` log, the atomic
pidfile — and the fail-closed ``probe_handle`` cross-check (pid + progress
wall-clock; an alive pid alone is never RUNNING).
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Final

import pytest
from brutex.dashboard.sweep_runner import sweep_progress_file
from brutex.engine.sweep_detach import (
    DETACH_LOG_FILENAME,
    DETACH_PIDFILE_NAME,
    DETACHED_ENV_SENTINEL,
    DetachedSweepHandle,
    default_detach_command,
    default_detach_log_path,
    default_progress_path,
    detach_pidfile_path,
    probe_handle,
    running_as_detached_child,
    spawn_detached_sweep,
)
from brutex.engine.sweep_liveness import RunVerdict

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r7_spawn_stub_child_is_own_session_leader": ("R7",),
    "test_r7_log_redirection_captures_stdout_and_stderr": ("R7",),
    "test_r7_log_append_mode_preserves_prior_content": ("R7",),
    "test_r7_pidfile_contents_exact": ("R7",),
    "test_r7_explicit_log_path_and_run_id_hint": ("R7",),
    "test_r7_default_command_is_module_entry": ("R7",),
    "test_r7_default_command_spawn_runs_brutex_version": ("R7",),
    "test_r7_spawn_failure_is_loud": ("R7",),
    "test_r7_handle_is_frozen": ("R7",),
    "test_r7_progress_path_mirrors_dashboard_helper": ("R7",),
    "test_r7_probe_running_when_progress_matches_pid": ("R7",),
    "test_r7_probe_pid_mismatch_is_unknown": ("R7",),
    "test_r7_probe_no_progress_dead_pid_not_running": ("R7",),
    "test_r7_probe_no_progress_alive_pid_unknown": ("R7",),
    "test_r7_probe_corrupt_progress_unknown_fail_closed": ("R7",),
    "test_r7_probe_non_object_progress_unknown": ("R7",),
    "test_r7_probe_forwards_progress_verdicts": ("R7",),
    "test_r7_probe_explicit_progress_path_wins": ("R7",),
    "test_r7_probe_default_now_wall_is_wall_clock": ("R7",),
    "test_r7_child_env_carries_detach_sentinel": ("R7",),
    "test_r7_running_as_detached_child_true_from_mapping": ("R7",),
    "test_r7_running_as_detached_child_false_absent": ("R7",),
    "test_r7_running_as_detached_child_false_wrong_value": ("R7",),
    "test_r7_running_as_detached_child_default_reads_os_environ": ("R7",),
    "test_r7_spawn_env_override_base_still_stamps_sentinel": ("R7",),
}

#: Stub child that reports the value of the re-detach sentinel env var — proves
#: the child's environment carries BRUTEX_SWEEP_DETACHED=1 (fork-bomb guard).
_SENTINEL_STUB_CHILD: Final[str] = """
import os, sys
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    fh.write(os.environ.get(sys.argv[2], "<UNSET>"))
"""

#: Stub child: prove session leadership + argv passthrough, exercise both
#: output streams, then exit — the R7 spawn contract without a real sweep.
_STUB_CHILD: Final[str] = """
import os, sys
print("CHILD-OUT", flush=True)
print("CHILD-ERR", file=sys.stderr, flush=True)
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    fh.write(f"{os.getpid()} {os.getsid(0)} {sys.argv[2]}")
"""


def _spawn_stub(results_root: Path, out_file: Path) -> DetachedSweepHandle:
    return spawn_detached_sweep(
        (str(out_file), "tok"),
        results_root=results_root,
        command=(sys.executable, "-c", _STUB_CHILD),
    )


def _wait_for(path: Path, *, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not path.exists():
        assert time.monotonic() < deadline, f"timed out waiting for {path}"
        time.sleep(0.01)


def _reap(pid: int) -> None:
    """Wait the direct child so the test never leaks a zombie."""
    os.waitpid(pid, 0)


def _write_progress(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_r7_spawn_stub_child_is_own_session_leader(tmp_path: Path) -> None:
    """The child runs with start_new_session=True: its sid == its own pid."""
    out_file = tmp_path / "child-report.txt"
    handle = _spawn_stub(tmp_path, out_file)
    _wait_for(out_file)
    _reap(handle.pid)
    child_pid, child_sid, token = out_file.read_text(encoding="utf-8").split()
    assert int(child_pid) == handle.pid
    assert int(child_sid) == handle.pid  # session LEADER — survives parent death
    assert int(child_sid) != os.getsid(0)  # …and NOT our session
    assert token == "tok"  # argv passthrough intact


def test_r7_log_redirection_captures_stdout_and_stderr(tmp_path: Path) -> None:
    """stdout AND stderr both land in the default runs/ log (stderr → STDOUT)."""
    out_file = tmp_path / "child-report.txt"
    handle = _spawn_stub(tmp_path, out_file)
    _wait_for(out_file)
    _reap(handle.pid)
    log = default_detach_log_path(tmp_path)
    assert log == tmp_path / "runs" / DETACH_LOG_FILENAME
    content = log.read_text(encoding="utf-8")
    assert "CHILD-OUT" in content
    assert "CHILD-ERR" in content


def test_r7_log_append_mode_preserves_prior_content(tmp_path: Path) -> None:
    """The log opens in APPEND mode — a second spawn never truncates the first."""
    log = default_detach_log_path(tmp_path)
    log.parent.mkdir(parents=True)
    log.write_text("PRIOR-RUN-LINE\n", encoding="utf-8")
    out_file = tmp_path / "child-report.txt"
    handle = _spawn_stub(tmp_path, out_file)
    _wait_for(out_file)
    _reap(handle.pid)
    content = log.read_text(encoding="utf-8")
    assert content.startswith("PRIOR-RUN-LINE\n")
    assert "CHILD-OUT" in content


def test_r7_pidfile_contents_exact(tmp_path: Path) -> None:
    """The pidfile carries pid + full cmdline + hint + root (atomic rewrite)."""
    out_file = tmp_path / "child-report.txt"
    handle = spawn_detached_sweep(
        (str(out_file), "tok"),
        results_root=tmp_path,
        command=(sys.executable, "-c", _STUB_CHILD),
        run_id_hint="c" * 64,
        clock=lambda: 123.5,
    )
    _wait_for(out_file)
    _reap(handle.pid)
    pidfile = detach_pidfile_path(tmp_path)
    assert pidfile == tmp_path / "runs" / DETACH_PIDFILE_NAME
    doc = json.loads(pidfile.read_text(encoding="utf-8"))
    assert doc == {
        "pid": handle.pid,
        "cmdline": [sys.executable, "-c", _STUB_CHILD, str(out_file), "tok"],
        "run_id_hint": "c" * 64,
        "results_root": str(tmp_path),
        "started_utc": 123.5,
    }
    assert handle.cmdline == (sys.executable, "-c", _STUB_CHILD, str(out_file), "tok")
    assert handle.run_id_hint == "c" * 64
    assert handle.results_root == tmp_path


def test_r7_explicit_log_path_and_run_id_hint(tmp_path: Path) -> None:
    """An explicit log_path override is honoured (the default log stays absent)."""
    out_file = tmp_path / "child-report.txt"
    custom_log = tmp_path / "custom" / "my.log"
    handle = spawn_detached_sweep(
        (str(out_file), "tok"),
        results_root=tmp_path,
        log_path=custom_log,
        command=(sys.executable, "-c", _STUB_CHILD),
    )
    _wait_for(out_file)
    _reap(handle.pid)
    assert "CHILD-OUT" in custom_log.read_text(encoding="utf-8")
    assert not default_detach_log_path(tmp_path).exists()


def test_r7_default_command_is_module_entry() -> None:
    """The default launcher prefix is (sys.executable, -m, brutex)."""
    assert default_detach_command() == (sys.executable, "-m", "brutex")


def test_r7_default_command_spawn_runs_brutex_version(tmp_path: Path) -> None:
    """The default-command branch genuinely launches ``python -m brutex`` detached.

    ``--version`` is the CLI's cheap fast path; the child's output lands in
    the runs/ log and the cmdline prefix is the module entry.
    """
    handle = spawn_detached_sweep(("--version",), results_root=tmp_path)
    assert handle.cmdline == (sys.executable, "-m", "brutex", "--version")
    _, status = os.waitpid(handle.pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    log_content = default_detach_log_path(tmp_path).read_text(encoding="utf-8")
    assert log_content != ""


def test_r7_spawn_failure_is_loud(tmp_path: Path) -> None:
    """A missing executable raises OSError — a loud launch-time refusal."""
    with pytest.raises(OSError):
        spawn_detached_sweep(
            ("x",),
            results_root=tmp_path,
            command=(str(tmp_path / "no-such-binary"),),
        )


def test_r7_handle_is_frozen(tmp_path: Path) -> None:
    """The handle is an immutable record (frozen slots dataclass)."""
    handle = DetachedSweepHandle(pid=1, run_id_hint=None, results_root=tmp_path, cmdline=("x",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        handle.pid = 2  # type: ignore[misc]


def test_r7_child_env_carries_detach_sentinel(tmp_path: Path) -> None:
    """The spawned child's environment carries BRUTEX_SWEEP_DETACHED=1.

    This is the fork-bomb guard (hostile-review round 3, #1594): the child
    consults the sentinel (NOT its raw argv) before re-detaching, so an
    abbreviated ``--det`` that survives the argv filter can never fork another
    detached generation.
    """
    out_file = tmp_path / "sentinel-report.txt"
    handle = spawn_detached_sweep(
        (str(out_file), DETACHED_ENV_SENTINEL),
        results_root=tmp_path,
        command=(sys.executable, "-c", _SENTINEL_STUB_CHILD),
    )
    _wait_for(out_file)
    _reap(handle.pid)
    assert out_file.read_text(encoding="utf-8") == "1"


def test_r7_spawn_env_override_base_still_stamps_sentinel(tmp_path: Path) -> None:
    """An explicit base env is honoured but the sentinel is ALWAYS stamped on top.

    The child sees the injected marker AND the sentinel — the sentinel wins even
    if the caller's base env omitted (or mis-set) it.
    """
    out_file = tmp_path / "sentinel-report.txt"
    marker_file = tmp_path / "marker-report.txt"
    base = {**os.environ, "BRUTEX_TEST_MARKER": "present", DETACHED_ENV_SENTINEL: "0"}
    handle = spawn_detached_sweep(
        (str(out_file), DETACHED_ENV_SENTINEL),
        results_root=tmp_path,
        command=(sys.executable, "-c", _SENTINEL_STUB_CHILD),
        env=base,
    )
    _wait_for(out_file)
    _reap(handle.pid)
    # The base env's DETACHED_ENV_SENTINEL="0" is overridden to "1".
    assert out_file.read_text(encoding="utf-8") == "1"
    # And a distinct base-env marker survived (env override merged, not replaced).
    handle2 = spawn_detached_sweep(
        (str(marker_file), "BRUTEX_TEST_MARKER"),
        results_root=tmp_path,
        command=(sys.executable, "-c", _SENTINEL_STUB_CHILD),
        env=base,
    )
    _wait_for(marker_file)
    _reap(handle2.pid)
    assert marker_file.read_text(encoding="utf-8") == "present"


def test_r7_running_as_detached_child_true_from_mapping() -> None:
    """The sentinel == "1" in the supplied mapping → True."""
    assert running_as_detached_child({DETACHED_ENV_SENTINEL: "1"}) is True


def test_r7_running_as_detached_child_false_absent() -> None:
    """An absent sentinel → False (a normal, non-detached invocation)."""
    assert running_as_detached_child({}) is False


def test_r7_running_as_detached_child_false_wrong_value() -> None:
    """A non-"1" sentinel value → False (only the exact "1" flips it)."""
    assert running_as_detached_child({DETACHED_ENV_SENTINEL: "0"}) is False


def test_r7_running_as_detached_child_default_reads_os_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no mapping the default reads os.environ."""
    monkeypatch.delenv(DETACHED_ENV_SENTINEL, raising=False)
    assert running_as_detached_child() is False
    monkeypatch.setenv(DETACHED_ENV_SENTINEL, "1")
    assert running_as_detached_child() is True


def test_r7_progress_path_mirrors_dashboard_helper(tmp_path: Path) -> None:
    """default_progress_path is byte-equal to the dashboard's sweep_progress_file.

    The engine may not import brutex.dashboard, so the path law is mirrored;
    this pin makes silent drift impossible.
    """
    assert default_progress_path(tmp_path) == sweep_progress_file(tmp_path)


def test_r7_probe_running_when_progress_matches_pid(tmp_path: Path) -> None:
    """Fresh progress naming the handle's own pid ⇒ RUNNING."""
    handle = DetachedSweepHandle(
        pid=os.getpid(), run_id_hint=None, results_root=tmp_path, cmdline=("x",)
    )
    _write_progress(
        default_progress_path(tmp_path),
        {"running": True, "wall_time_utc": 1000.0, "pid": os.getpid()},
    )
    assert probe_handle(handle, now_wall=1010.0) is RunVerdict.RUNNING


def test_r7_probe_pid_mismatch_is_unknown(tmp_path: Path) -> None:
    """A RUNNING progress file owned by a DIFFERENT pid ⇒ UNKNOWN (not provably ours)."""
    other_alive = os.getppid()
    handle = DetachedSweepHandle(
        pid=os.getpid(), run_id_hint=None, results_root=tmp_path, cmdline=("x",)
    )
    _write_progress(
        default_progress_path(tmp_path),
        {"running": True, "wall_time_utc": 1000.0, "pid": other_alive},
    )
    assert probe_handle(handle, now_wall=1010.0) is RunVerdict.UNKNOWN


def test_r7_probe_no_progress_dead_pid_not_running(tmp_path: Path) -> None:
    """No progress evidence + dead pid ⇒ NOT_RUNNING."""
    handle = DetachedSweepHandle(
        pid=999_999, run_id_hint=None, results_root=tmp_path, cmdline=("x",)
    )
    verdict = probe_handle(handle, now_wall=1.0, pid_alive=lambda pid: False)
    assert verdict is RunVerdict.NOT_RUNNING


def test_r7_probe_no_progress_alive_pid_unknown(tmp_path: Path) -> None:
    """No progress evidence + ALIVE pid ⇒ UNKNOWN (pid recycling — fail-closed)."""
    handle = DetachedSweepHandle(
        pid=os.getpid(), run_id_hint=None, results_root=tmp_path, cmdline=("x",)
    )
    verdict = probe_handle(handle, now_wall=1.0, pid_alive=lambda pid: True)
    assert verdict is RunVerdict.UNKNOWN


def test_r7_probe_corrupt_progress_unknown_fail_closed(tmp_path: Path) -> None:
    """Garbage progress bytes read as no-evidence: alive ⇒ UNKNOWN, dead ⇒ NOT_RUNNING."""
    path = default_progress_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{{{torn", encoding="utf-8")
    handle = DetachedSweepHandle(
        pid=os.getpid(), run_id_hint=None, results_root=tmp_path, cmdline=("x",)
    )
    assert probe_handle(handle, now_wall=1.0, pid_alive=lambda pid: True) is RunVerdict.UNKNOWN
    assert probe_handle(handle, now_wall=1.0, pid_alive=lambda pid: False) is RunVerdict.NOT_RUNNING


def test_r7_probe_non_object_progress_unknown(tmp_path: Path) -> None:
    """A JSON array body is corrupt-for-us: no evidence ⇒ UNKNOWN when alive."""
    path = default_progress_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("[1, 2]", encoding="utf-8")
    handle = DetachedSweepHandle(
        pid=os.getpid(), run_id_hint=None, results_root=tmp_path, cmdline=("x",)
    )
    assert probe_handle(handle, now_wall=1.0, pid_alive=lambda pid: True) is RunVerdict.UNKNOWN


def test_r7_probe_forwards_progress_verdicts(tmp_path: Path) -> None:
    """STALE / NOT_RUNNING / legacy-UNKNOWN verdicts pass through unchanged."""
    handle = DetachedSweepHandle(
        pid=os.getpid(), run_id_hint=None, results_root=tmp_path, cmdline=("x",)
    )
    path = default_progress_path(tmp_path)
    _write_progress(path, {"running": True, "wall_time_utc": 1000.0, "pid": os.getpid()})
    assert probe_handle(handle, now_wall=999_999.0) is RunVerdict.STALE
    _write_progress(path, {"running": False, "wall_time_utc": 1000.0, "pid": os.getpid()})
    assert probe_handle(handle, now_wall=1010.0) is RunVerdict.NOT_RUNNING
    _write_progress(path, {"state": "running", "updated_at": 4.2})  # legacy shape
    assert probe_handle(handle, now_wall=1010.0) is RunVerdict.UNKNOWN


def test_r7_probe_explicit_progress_path_wins(tmp_path: Path) -> None:
    """An explicit progress_path override is read instead of the default."""
    handle = DetachedSweepHandle(
        pid=os.getpid(), run_id_hint=None, results_root=tmp_path, cmdline=("x",)
    )
    custom = tmp_path / "elsewhere" / "progress.json"
    _write_progress(custom, {"running": True, "wall_time_utc": 1000.0, "pid": os.getpid()})
    assert probe_handle(handle, now_wall=1010.0, progress_path=custom) is RunVerdict.RUNNING


def test_r7_probe_default_now_wall_is_wall_clock(tmp_path: Path) -> None:
    """now_wall=None reads the real wall clock — a just-written stamp is fresh."""
    handle = DetachedSweepHandle(
        pid=os.getpid(), run_id_hint=None, results_root=tmp_path, cmdline=("x",)
    )
    _write_progress(
        default_progress_path(tmp_path),
        {"running": True, "wall_time_utc": time.time(), "pid": os.getpid()},
    )
    assert probe_handle(handle) is RunVerdict.RUNNING
