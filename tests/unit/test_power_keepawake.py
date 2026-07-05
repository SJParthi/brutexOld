"""Unit tests for ``brutex.observability.power_keepawake`` (DEC-PULL-KEEPAWAKE-001).

The macOS keep-awake guard both production pull entrypoints wrap: on darwin it
spawns ``caffeinate -i -s -w <pid>`` and terminates it on exit; anywhere else —
or on any spawn ``OSError`` — it logs one ``power.keepawake.unavailable`` note
and no-ops. Every scenario runs against the injected spawn/platform seams (no
real ``caffeinate`` in CI); the guard must NEVER raise. 100% line+branch on the
module.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import MutableMapping
from typing import Any

import pytest
from brutex.observability.power_keepawake import (
    CAFFEINATE_BINARY,
    TERMINATE_TIMEOUT_S,
    keep_awake,
)
from structlog.testing import capture_logs


class _FakeProc:
    """A ``KeepAwakeProcess`` double recording every signal it receives."""

    def __init__(
        self,
        *,
        pid: int = 4242,
        wait_timeouts: int = 0,
        terminate_error: Exception | None = None,
    ) -> None:
        self.pid = pid
        self.calls: list[str] = []
        self.wait_timeouts_left = wait_timeouts
        self.terminate_error = terminate_error

    def terminate(self) -> None:
        self.calls.append("terminate")
        if self.terminate_error is not None:
            raise self.terminate_error

    def kill(self) -> None:
        self.calls.append("kill")

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append(f"wait({timeout})")
        if self.wait_timeouts_left > 0:
            self.wait_timeouts_left -= 1
            raise subprocess.TimeoutExpired(cmd=CAFFEINATE_BINARY, timeout=timeout or 0.0)
        return 0


def _events(logs: list[MutableMapping[str, Any]]) -> list[str]:
    return [str(e["event"]) for e in logs if str(e["event"]).startswith("power.keepawake")]


# ── darwin: engage + release ─────────────────────────────────────────────────


def test_darwin_spawns_caffeinate_watching_our_pid_and_releases() -> None:
    """On darwin the guard spawns ``caffeinate -i -s -w <os.getpid()>`` and
    terminates it on exit — one engaged + one released log."""
    spawned: list[list[str]] = []
    proc = _FakeProc()

    def _spawn(argv: list[str]) -> _FakeProc:
        spawned.append(argv)
        return proc

    with capture_logs() as logs, keep_awake("unit pull", spawn=_spawn, platform="darwin"):
        assert spawned == [[CAFFEINATE_BINARY, "-i", "-s", "-w", str(os.getpid())]]
    assert proc.calls == ["terminate", f"wait({TERMINATE_TIMEOUT_S})"]
    assert _events(logs) == ["power.keepawake.engaged", "power.keepawake.released"]
    engaged = next(e for e in logs if e["event"] == "power.keepawake.engaged")
    assert engaged["reason"] == "unit pull"
    assert engaged["pid"] == 4242


def test_darwin_release_runs_even_when_the_body_raises() -> None:
    """A pull crash still releases the assertion (finally), and propagates."""
    proc = _FakeProc()

    def _crash() -> None:
        raise RuntimeError("pull crashed")

    with pytest.raises(RuntimeError, match="pull crashed"):  # noqa: SIM117 — the guard must wrap the raise
        with keep_awake("crash", spawn=lambda _argv: proc, platform="darwin"):
            _crash()
    assert "terminate" in proc.calls


def test_darwin_wedged_process_is_killed_after_bounded_wait() -> None:
    """``terminate`` → wait TIMEOUT → ``kill`` → bounded re-wait (never hangs)."""
    proc = _FakeProc(wait_timeouts=1)
    with capture_logs() as logs, keep_awake("wedged", spawn=lambda _argv: proc, platform="darwin"):
        pass
    assert proc.calls == [
        "terminate",
        f"wait({TERMINATE_TIMEOUT_S})",
        "kill",
        f"wait({TERMINATE_TIMEOUT_S})",
    ]
    assert "power.keepawake.released" in _events(logs)


def test_darwin_release_error_is_swallowed_and_logged() -> None:
    """A raising ``terminate`` logs release_failed and NEVER propagates."""
    proc = _FakeProc(terminate_error=OSError("gone"))
    with (
        capture_logs() as logs,
        keep_awake("bad-release", spawn=lambda _argv: proc, platform="darwin"),
    ):
        pass  # exiting must not raise despite the terminate error
    events = [e for e in logs if e["event"] == "power.keepawake.release_failed"]
    assert len(events) == 1
    assert events[0]["error_type"] == "OSError"


# ── unavailable paths: non-darwin / missing binary / OSError ────────────────


def test_non_darwin_is_a_logged_noop() -> None:
    """Any non-darwin platform never spawns; one unavailable note, body runs."""
    ran = False

    def _spawn(_argv: list[str]) -> _FakeProc:  # pragma: no cover — must NOT be called
        raise AssertionError("spawn must not be called off darwin")

    with capture_logs() as logs, keep_awake("linux", spawn=_spawn, platform="linux"):
        ran = True
    assert ran
    assert _events(logs) == ["power.keepawake.unavailable"]
    (event,) = [e for e in logs if e["event"] == "power.keepawake.unavailable"]
    assert event["platform"] == "linux"


def test_missing_caffeinate_binary_is_a_logged_noop() -> None:
    """FileNotFoundError (no caffeinate on PATH) degrades to the honest no-op."""

    def _spawn(_argv: list[str]) -> _FakeProc:
        raise FileNotFoundError("caffeinate: not found")

    ran = False
    with capture_logs() as logs, keep_awake("no-binary", spawn=_spawn, platform="darwin"):
        ran = True
    assert ran
    assert _events(logs) == ["power.keepawake.unavailable"]
    (event,) = [e for e in logs if e["event"] == "power.keepawake.unavailable"]
    assert event["error_type"] == "FileNotFoundError"


def test_generic_oserror_on_spawn_is_a_logged_noop() -> None:
    """ANY OSError on spawn (perm, fork failure) is the same graceful path."""

    def _spawn(_argv: list[str]) -> _FakeProc:
        raise PermissionError("denied")

    with capture_logs() as logs, keep_awake("perm", spawn=_spawn, platform="darwin"):
        pass
    (event,) = [e for e in logs if e["event"] == "power.keepawake.unavailable"]
    assert event["error_type"] == "PermissionError"


def test_default_platform_is_sys_platform() -> None:
    """Omitting ``platform`` reads ``sys.platform`` (this CI host ≠ darwin ⇒ no-op
    unavailable; on a darwin host the injected spawn keeps it hermetic)."""
    proc = _FakeProc()
    with capture_logs() as logs, keep_awake("default-platform", spawn=lambda _argv: proc):
        pass
    if sys.platform == "darwin":  # pragma: no cover — CI is linux; kept for local macOS runs
        assert _events(logs) == ["power.keepawake.engaged", "power.keepawake.released"]
    else:
        assert _events(logs) == ["power.keepawake.unavailable"]


def test_default_spawn_launches_a_real_detached_process() -> None:
    """The production spawn seam works against a real short-lived process."""
    from brutex.observability.power_keepawake import (
        _default_spawn,  # pyright: ignore[reportPrivateUsage] — whitebox: the production seam default
    )

    proc = _default_spawn([sys.executable, "-c", "pass"])
    assert proc.wait(timeout=30) == 0
