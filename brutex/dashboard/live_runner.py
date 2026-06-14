"""Dashboard-side ON/OFF lifecycle for the live-feed capture **subprocess**.

Honors the separation decree (``DEC-LIVE-DECOUPLE-001``): the dashboard NEVER
imports :mod:`brutex.live`. The ON/OFF toggle SPAWNS ``python -m brutex.live
capture …`` as a separate OS process and tracks it through a small JSON state
file + :func:`brutex.config.runtime.is_pid_alive`, so ``brutex/live/`` stays
imported-by-nothing and lifts to TickVault / AWS unchanged.

Read-only market data, no orders. Credentials are inherited from the dashboard's
environment (never passed on the command line — so ``ps`` stays clean).

This is the v1 lifecycle: exactly-once start (idempotent), reap-or-report on
``status``, terminate-then-clear on ``stop``. The heartbeat-silence watchdog +
the cross-process OS lock + the ``(pid, start_time)`` identity check (PID reuse)
are the explicit Phase-1 hardening follow-ups from the 4-agent review.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess  # nosec B404 - spawns the trusted `python -m brutex.live` CLI, fixed argv
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from brutex.config.runtime import is_pid_alive

_LOG = structlog.get_logger("brutex.dashboard.live_runner")

PopenFactory = Callable[[list[str]], Any]
Killer = Callable[[int, int], None]
PidAlive = Callable[[int], bool]
Clock = Callable[[], float]


def _now_epoch() -> float:
    """Wall-clock epoch seconds (the production clock; injected in tests)."""
    return datetime.now(UTC).timestamp()


@dataclass(frozen=True, slots=True)
class LiveStatus:
    """A snapshot of the live-capture subprocess for the dashboard.

    Attributes
    ----------
    running
        True iff a live capture process is currently alive.
    pid, started_at, uptime_s, argv, out
        The tracked process identity + spawn details (all ``None`` when stopped).
    """

    running: bool
    pid: int | None = None
    started_at: str | None = None
    uptime_s: float | None = None
    argv: list[str] | None = None
    out: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable mapping for the ``/api/v1/live/status`` route."""
        return asdict(self)


class LiveRunner:
    """Start / stop / inspect the live-capture subprocess via a JSON state file."""

    def __init__(
        self,
        *,
        state_path: str | Path,
        popen_factory: PopenFactory = subprocess.Popen,
        pid_alive: PidAlive = is_pid_alive,
        killer: Killer = os.kill,
        clock: Clock = _now_epoch,
    ) -> None:
        """Wire the runner.

        Parameters
        ----------
        state_path
            JSON file recording ``{pid, start_ts, argv, out}`` for the live run.
        popen_factory
            Spawns the subprocess (injected fake in tests — no real process).
        pid_alive
            Liveness check (defaults to the shared ``is_pid_alive``).
        killer
            ``os.kill``-shaped signal sender (injected in tests).
        clock
            Epoch-seconds clock (injected for deterministic uptime in tests).
        """
        self._state_path = Path(state_path)
        self._popen = popen_factory
        self._pid_alive = pid_alive
        self._kill = killer
        self._clock = clock

    # ── state file ────────────────────────────────────────────────
    def _read_state(self) -> dict[str, Any] | None:
        """Load the state file, or ``None`` if absent/unreadable."""
        if not self._state_path.exists():
            return None
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def _write_state(self, state: dict[str, Any]) -> None:
        """Persist *state* atomically (temp + replace) so a reader never tears."""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(self._state_path)

    def _clear_state(self) -> None:
        """Remove the state file (idempotent)."""
        self._state_path.unlink(missing_ok=True)

    # ── public API ────────────────────────────────────────────────
    def status(self) -> LiveStatus:
        """Return the current live-capture status, reaping a dead/stale PID."""
        state = self._read_state()
        if state is None:
            return LiveStatus(running=False)
        pid = int(state["pid"])
        if not self._pid_alive(pid):
            self._clear_state()  # reap: the tracked process is gone
            return LiveStatus(running=False)
        return LiveStatus(
            running=True,
            pid=pid,
            started_at=state.get("started_at"),
            uptime_s=round(self._clock() - float(state["start_ts"]), 1),
            argv=state.get("argv"),
            out=state.get("out"),
        )

    def start(
        self,
        *,
        out: str,
        log_file: str,
        seconds: float,
        symbols: str = "all",
        feed: str = "ltp",
    ) -> LiveStatus:
        """Spawn the capture subprocess (idempotent — a no-op if already running)."""
        current = self.status()
        if current.running:
            return current
        argv = [
            sys.executable,
            "-m",
            "brutex.live",
            "capture",
            "--out",
            out,
            "--symbols",
            symbols,
            "--feed",
            feed,
            "--seconds",
            str(seconds),
            "--log-file",
            log_file,
        ]
        proc = self._popen(argv)
        now = self._clock()
        self._write_state(
            {
                "pid": int(proc.pid),
                "start_ts": now,
                "started_at": datetime.fromtimestamp(now, UTC).isoformat(),
                "argv": argv,
                "out": out,
            }
        )
        _LOG.info("live_runner_start", pid=int(proc.pid), symbols=symbols, feed=feed, out=out)
        return self.status()

    def stop(self) -> LiveStatus:
        """Terminate the capture subprocess (SIGTERM→SIGKILL) and clear the state."""
        state = self._read_state()
        if state is not None and self._pid_alive(int(state["pid"])):
            self._terminate(int(state["pid"]))
            _LOG.info("live_runner_stop", pid=int(state["pid"]))
        self._clear_state()
        return LiveStatus(running=False)

    def _terminate(self, pid: int) -> None:
        """SIGTERM the pid; escalate to SIGKILL if it is still alive after the grace."""
        try:
            self._kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        if self._pid_alive(pid):
            try:
                self._kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                return


__all__ = ["LiveRunner", "LiveStatus"]
