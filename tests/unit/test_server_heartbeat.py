"""Unit tests for the idle server heartbeat (DEC-AUDIT-ALERT-CLAMP-OBS-001).

The heartbeat makes a dark log gap diagnosable: while the server runs it logs
ONE ``server.heartbeat`` per interval; the off-switch silences it; a beat error
is swallowed (never crashes the server).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from brutex.observability import server_heartbeat as sh
from structlog.testing import capture_logs


class TestResolveHeartbeatInterval:
    def test_unset_uses_documented_default(self) -> None:
        assert sh.resolve_heartbeat_interval(None) == sh.DEFAULT_HEARTBEAT_INTERVAL_S

    def test_empty_uses_default(self) -> None:
        assert sh.resolve_heartbeat_interval("   ") == sh.DEFAULT_HEARTBEAT_INTERVAL_S

    def test_zero_disables(self) -> None:
        assert sh.resolve_heartbeat_interval("0") == 0.0

    def test_negative_disables(self) -> None:
        assert sh.resolve_heartbeat_interval("-5") == 0.0

    def test_valid_positive_float(self) -> None:
        assert sh.resolve_heartbeat_interval("2.5") == 2.5

    def test_junk_is_fail_safe_to_default_with_warn(self) -> None:
        with capture_logs() as logs:
            got = sh.resolve_heartbeat_interval("banana")
        assert got == sh.DEFAULT_HEARTBEAT_INTERVAL_S
        assert any(e["event"] == "server.heartbeat.bad_interval" for e in logs)

    def test_reads_environment_when_raw_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(sh.HEARTBEAT_INTERVAL_ENV, "7")
        assert sh.resolve_heartbeat_interval() == 7.0


class _Clock:
    """A controllable monotonic clock."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class TestServerHeartbeat:
    @staticmethod
    def _capture_spawn() -> tuple[list[Callable[[], None]], Callable[[Callable[[], None]], None]]:
        captured: list[Callable[[], None]] = []

        def spawn(fn: Callable[[], None]) -> None:
            captured.append(fn)

        return captured, spawn

    def test_beat_emits_uptime_pid_rss(self) -> None:
        clock = _Clock()
        hb = sh.ServerHeartbeat(
            interval=5.0, clock=clock, rss_probe=lambda: 2048, spawn=lambda fn: None
        )
        hb.start()
        clock.now = 112.5  # 12.5s of uptime
        with capture_logs() as logs:
            hb._beat()
        beat = next(e for e in logs if e["event"] == "server.heartbeat")
        assert beat["uptime_s"] == 12.5
        assert beat["rss_mb"] == 2.0  # 2048 KiB → 2.0 MB
        assert beat["interval_s"] == 5.0
        assert isinstance(beat["pid"], int)

    def test_beat_honest_none_rss_when_probe_abstains(self) -> None:
        hb = sh.ServerHeartbeat(interval=5.0, rss_probe=lambda: None, spawn=lambda fn: None)
        hb.start()
        with capture_logs() as logs:
            hb._beat()
        beat = next(e for e in logs if e["event"] == "server.heartbeat")
        assert beat["rss_mb"] is None  # never fabricated

    def test_beat_is_fail_safe(self) -> None:
        def boom() -> int | None:
            raise RuntimeError("probe exploded")

        hb = sh.ServerHeartbeat(interval=5.0, rss_probe=boom, spawn=lambda fn: None)
        hb.start()
        with capture_logs() as logs:
            hb._beat()  # must NOT raise
        assert any(e["event"] == "server.heartbeat.error" for e in logs)

    def test_run_loop_beats_then_stops(self) -> None:
        # Drive the real _run loop with a stubbed _stop event: first wait ->
        # False (a due beat), second wait -> True (stop). Exactly one beat.
        captured, spawn = self._capture_spawn()
        hb = sh.ServerHeartbeat(interval=0.01, rss_probe=lambda: 1024, spawn=spawn)
        hb.start()
        run_fn = captured[0]
        waits = iter([False, True])
        hb._stop.wait = lambda _timeout: next(waits)  # type: ignore[method-assign]
        with capture_logs() as logs:
            run_fn()
        assert sum(1 for e in logs if e["event"] == "server.heartbeat") == 1

    def test_double_start_is_a_noop(self) -> None:
        captured, spawn = self._capture_spawn()
        hb = sh.ServerHeartbeat(interval=5.0, spawn=spawn)
        hb.start()
        hb.start()  # second call must NOT spawn again
        assert len(captured) == 1

    def test_stop_sets_the_event(self) -> None:
        hb = sh.ServerHeartbeat(interval=5.0, spawn=lambda fn: None)
        hb.start()
        assert not hb._stop.is_set()
        hb.stop()
        assert hb._stop.is_set()

    def test_context_manager_starts_and_stops(self) -> None:
        captured, spawn = self._capture_spawn()
        hb = sh.ServerHeartbeat(interval=5.0, spawn=spawn)
        with hb as entered:
            assert entered is hb
            assert len(captured) == 1  # started
        assert hb._stop.is_set()  # stopped on exit

    def test_beat_swallows_even_a_logging_failure(self) -> None:
        # The inner except: a probe raises AND the warning log also raises —
        # the outermost swallow keeps the server alive.
        class _ExplodingLog:
            def info(self, *a: object, **k: object) -> None:
                raise RuntimeError("info boom")

            def warning(self, *a: object, **k: object) -> None:
                raise RuntimeError("warning boom")

        hb = sh.ServerHeartbeat(
            interval=5.0, rss_probe=lambda: 1, spawn=lambda fn: None, log=_ExplodingLog()
        )
        hb.start()
        hb._beat()  # must NOT raise despite both log calls exploding


def test_default_spawn_runs_fn_on_a_daemon_thread() -> None:
    """The production spawn seam actually runs *fn* on a background thread."""
    import threading

    ran = threading.Event()
    sh._daemon_spawn(ran.set)
    assert ran.wait(timeout=5.0)  # the daemon executed the callable


class TestStartServerHeartbeat:
    def test_disabled_returns_none_and_logs(self) -> None:
        with capture_logs() as logs:
            hb = sh.start_server_heartbeat(raw_interval="0")
        assert hb is None
        assert any(e["event"] == "server.heartbeat.disabled" for e in logs)

    def test_enabled_starts_and_returns_heartbeat(self) -> None:
        captured: list[Callable[[], None]] = []

        def spawn(fn: Callable[[], None]) -> None:
            captured.append(fn)

        with capture_logs() as logs:
            hb = sh.start_server_heartbeat(raw_interval="3", spawn=spawn)
        assert hb is not None
        assert len(captured) == 1  # daemon spawned
        assert any(e["event"] == "server.heartbeat.started" for e in logs)
        hb.stop()
