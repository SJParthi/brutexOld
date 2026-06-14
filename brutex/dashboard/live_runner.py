"""Dashboard-side ON/OFF lifecycle for the live-feed capture **subprocess**.

Honors the separation decree (``DEC-LIVE-DECOUPLE-001``): the dashboard NEVER
imports :mod:`brutex.live`. The ON/OFF toggle SPAWNS ``python -m brutex.live
capture …`` as a separate OS process and tracks it through a small JSON state
file, so ``brutex/live/`` stays imported-by-nothing and lifts to TickVault / AWS
unchanged.

Read-only market data, no orders. **Credentials bridge:** the Credentials page
saves the Groww TOTP key/secret to ``~/.brutex/credentials.json``; the spawned
capture reads them from its ENVIRONMENT. :func:`_load_cred_env` loads the saved
file and injects ``BRUTEX_GROWW_API_KEY`` / ``BRUTEX_GROWW_TOTP_SECRET`` into the
child's env (never on argv — so ``ps`` stays clean), so "save once in the
dashboard → Live ON just works" (``DEC-LIVE-CREDS-BRIDGE-001``).

**Crash-honest status:** when WE spawned the process this dashboard lifetime,
``Popen.poll()`` is authoritative — it reaps a crashed child so a zombie never
reads as "running" (bare ``os.kill(pid,0)`` would). After a dashboard restart we
no longer hold the handle and fall back to OS liveness for adoption.

This is the v1 lifecycle (exactly-once start, crash-honest status, terminate +
clear on stop). The heartbeat-silence watchdog (auto-restart on a real drop) +
the cross-process OS lock + the ``(pid, start_time)`` identity check are the
explicit Phase-2 follow-ups stacking on this PR.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess  # nosec B404 - spawns the trusted `python -m brutex.live` CLI, fixed argv
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from brutex.adapters.groww.auth import ENV_API_KEY, ENV_TOTP_SECRET
from brutex.config.credentials import CredentialsError, load_groww_credentials
from brutex.config.runtime import is_pid_alive

_LOG = structlog.get_logger("brutex.dashboard.live_runner")

PopenFactory = Callable[[list[str], Mapping[str, str]], Any]
Killer = Callable[[int, int], None]
PidAlive = Callable[[int], bool]
Clock = Callable[[], float]
CredEnv = Callable[[], dict[str, str]]


def _now_epoch() -> float:
    """Wall-clock epoch seconds (the production clock; injected in tests)."""
    return datetime.now(UTC).timestamp()


def _default_popen(argv: list[str], env: Mapping[str, str]) -> Any:  # pragma: no cover — real spawn
    """Spawn the capture subprocess with *env* (creds ride in env, never argv)."""
    return subprocess.Popen(argv, env=dict(env))  # noqa: S603  # nosec B603 - fixed trusted argv


def _load_cred_env() -> dict[str, str]:
    """Bridge the dashboard-saved Groww creds → the capture subprocess env.

    Loads ``~/.brutex/credentials.json`` (the Credentials page's sink) and returns
    ``{BRUTEX_GROWW_API_KEY, BRUTEX_GROWW_TOTP_SECRET}`` for the child's
    environment, or ``{}`` when no creds are saved / the file is unreadable (the
    capture then fails auth and the crash is surfaced honestly). Creds never touch
    argv — they ride in the child ENV so ``ps`` stays clean.
    """
    try:
        creds = load_groww_credentials()
    except CredentialsError:
        _LOG.warning("live_runner_creds_unreadable")
        return {}
    if creds is None:
        return {}
    return {ENV_API_KEY: creds.api_key, ENV_TOTP_SECRET: creds.totp_secret}


@dataclass(frozen=True, slots=True)
class LiveStatus:
    """A snapshot of the live-capture subprocess for the dashboard.

    Attributes
    ----------
    running
        True iff a live capture process is currently alive (poll-verified when we
        hold the handle, so a crashed/zombie child reads ``False``).
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
        popen_factory: PopenFactory = _default_popen,
        pid_alive: PidAlive = is_pid_alive,
        killer: Killer = os.kill,
        clock: Clock = _now_epoch,
        cred_env: CredEnv = _load_cred_env,
    ) -> None:
        """Wire the runner.

        Parameters
        ----------
        state_path
            JSON file recording ``{pid, start_ts, argv, out}`` for the live run.
        popen_factory
            Spawns the subprocess given ``(argv, env)`` (injected fake in tests).
        pid_alive
            OS liveness fallback (defaults to the shared ``is_pid_alive``).
        killer
            ``os.kill``-shaped signal sender (injected in tests).
        clock
            Epoch-seconds clock (injected for deterministic uptime in tests).
        cred_env
            Returns the Groww creds as env vars to inject into the child
            (defaults to loading the dashboard-saved file).
        """
        self._state_path = Path(state_path)
        self._popen = popen_factory
        self._pid_alive = pid_alive
        self._kill = killer
        self._clock = clock
        self._cred_env = cred_env
        self._proc: Any = None

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

    def _is_live(self, pid: int) -> bool:
        """True if *pid* is genuinely alive.

        When WE hold the spawned handle, ``Popen.poll()`` is authoritative: it
        reaps a crashed child, so a zombie reads ``False`` (bare ``os.kill(pid,0)``
        would wrongly read ``True``). With no handle (post-restart adoption) we
        fall back to OS liveness.
        """
        proc = self._proc
        if proc is not None and int(proc.pid) == pid:
            return proc.poll() is None
        return self._pid_alive(pid)

    # ── public API ────────────────────────────────────────────────
    def status(self) -> LiveStatus:
        """Return the current live-capture status, reaping a dead/crashed PID."""
        state = self._read_state()
        if state is None:
            return LiveStatus(running=False)
        pid = int(state["pid"])
        if not self._is_live(pid):
            self._clear_state()  # reap: the tracked process is gone/crashed
            self._proc = None
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
        feed: str = "both",
    ) -> LiveStatus:
        """Spawn the capture subprocess (idempotent — a no-op if already running).

        The dashboard-saved Groww creds are injected into the child's environment
        (``DEC-LIVE-CREDS-BRIDGE-001``) so the capture authenticates without the
        operator re-exporting them.
        """
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
        cred_env = self._cred_env()
        proc = self._popen(argv, {**os.environ, **cred_env})
        self._proc = proc
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
        _LOG.info(
            "live_runner_start",
            pid=int(proc.pid),
            symbols=symbols,
            feed=feed,
            out=out,
            creds_from_dashboard=bool(cred_env),
        )
        return self.status()

    def stop(self) -> LiveStatus:
        """Terminate the capture subprocess (SIGTERM→SIGKILL) and clear the state."""
        state = self._read_state()
        if state is not None and self._is_live(int(state["pid"])):
            self._terminate(int(state["pid"]))
            _LOG.info("live_runner_stop", pid=int(state["pid"]))
        self._clear_state()
        self._proc = None
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
