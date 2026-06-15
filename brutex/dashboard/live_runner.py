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
clear on stop). The cross-process OS lock + the ``(pid, start_time)`` identity
check are the explicit Phase-2 follow-ups stacking on this PR.

**N-connection capture (``DEC-LIVE-DASHBOARD-WIRE-001``):** the ON toggle now
WRITES the ``{spot, legs}`` subscription manifest (the dashboard's exact 775 +
the F&O legs, via the injected ``manifest_writer``) and spawns ``capture-all
--manifest <path> --report <path> --feed both`` — opening connection #0 = spot
(dual-feed) + #1..N = legs, and writing the per-connection proof the ``/live``
page renders — instead of the single-connection ``capture``.

**Restart watchdog (``DEC-LIVE-DASHBOARD-WIRE-001``):** :meth:`LiveRunner.watch`
is a calendar-free supervisor — while the toggle is ON it detects the capture
subprocess DIED (``status().running`` flips False with the spawn args still
remembered) and RESTARTS it with bounded exponential backoff (the operator's
rule: always restart while ON; no session-calendar gate). A clean operator
``stop()`` clears the remembered spawn, so the watchdog does NOT resurrect a
deliberately-stopped feed.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess  # nosec B404 - spawns the trusted `python -m brutex.live` CLI, fixed argv
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.adapters.groww.auth import ENV_API_KEY, ENV_TOTP_SECRET
from brutex.config.credentials import CredentialsError, load_groww_credentials
from brutex.config.runtime import is_pid_alive

_LOG = structlog.get_logger("brutex.dashboard.live_runner")

#: Watchdog backoff floor (seconds) — the first restart waits this long.
_WATCH_BASE_BACKOFF_S: Final[float] = 1.0
#: Watchdog backoff ceiling (seconds) — the exponential backoff never exceeds it,
#: so a persistent feed failure retries steadily (never a thundering hammer, never
#: an unbounded wait).
_WATCH_MAX_BACKOFF_S: Final[float] = 60.0

PopenFactory = Callable[[list[str], Mapping[str, str]], Any]
Killer = Callable[[int, int], None]
PidAlive = Callable[[int], bool]
Clock = Callable[[], float]
CredEnv = Callable[[], dict[str, str]]
#: Writes the ``{spot, legs}`` subscription manifest to the given path. Injected
#: by the dashboard (it composes the manifest from the instrument store + the F&O
#: registry); a default no-op keeps ``brutex.live`` DECOUPLED — this module never
#: imports the data plane (``DEC-LIVE-DECOUPLE-001``).
ManifestWriter = Callable[[str], None]
#: Sleeps for the given seconds (the watchdog's backoff; injected in tests).
Sleeper = Callable[[float], None]


def _noop_manifest_writer(_path: str) -> None:
    """Default manifest writer — a no-op (the real store-backed writer is injected).

    With the no-op, ``capture-all --manifest`` reads an absent file and the
    capture resolves the spot from the instrument master itself (its documented
    no-manifest fallback); the dashboard injects the real writer so the manifest
    carries the EXACT persisted 775 + the F&O legs.
    """


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
    pid, started_at, uptime_s, argv, out, feed, report
        The tracked process identity + spawn details (all ``None`` when stopped).
        ``feed`` is the actual feed mode the capture runs (``"both"`` for the
        N-connection capture — the Live-bar chip reflects it); ``report`` is the
        per-connection proof path the ``/live`` page reads.
    """

    running: bool
    pid: int | None = None
    started_at: str | None = None
    uptime_s: float | None = None
    argv: list[str] | None = None
    out: str | None = None
    feed: str | None = None
    report: str | None = None

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
        manifest_writer: ManifestWriter = _noop_manifest_writer,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        """Wire the runner.

        Parameters
        ----------
        state_path
            JSON file recording ``{pid, start_ts, argv, out, feed, report}`` for
            the live run.
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
        manifest_writer
            Writes the ``{spot, legs}`` subscription manifest to a path (injected
            by the dashboard; the default is a no-op so ``brutex.live`` stays
            decoupled). Called on every ``start`` before the spawn.
        sleeper
            Sleeps the watchdog's backoff (injected in tests so no real wall-time
            passes).
        """
        self._state_path = Path(state_path)
        self._popen = popen_factory
        self._pid_alive = pid_alive
        self._kill = killer
        self._clock = clock
        self._cred_env = cred_env
        self._manifest_writer = manifest_writer
        self._sleeper = sleeper
        self._proc: Any = None
        #: The kwargs of the most recent ``start`` — the watchdog re-spawns with
        #: these on a death. ``None`` after a clean ``stop`` (so the watchdog never
        #: resurrects a deliberately-stopped feed).
        self._last_start: dict[str, Any] | None = None

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
            feed=state.get("feed"),
            report=state.get("report"),
        )

    def start(
        self,
        *,
        out: str,
        log_file: str,
        report: str,
        manifest: str,
        seconds: float,
        symbols: str = "all",
        feed: str = "both",
        lake_root: str | None = None,
    ) -> LiveStatus:
        """Spawn the N-connection capture subprocess (idempotent — no-op if running).

        Writes the ``{spot, legs}`` subscription manifest (via the injected
        ``manifest_writer``) to *manifest*, then spawns ``capture-all --manifest
        <manifest> --report <report> --feed both`` — connection #0 = spot
        (dual-feed) + #1..N = the F&O legs (``DEC-LIVE-DASHBOARD-WIRE-001``). The
        per-connection proof lands at *report* for the ``/live`` page. The
        dashboard-saved Groww creds are injected into the child's environment
        (``DEC-LIVE-CREDS-BRIDGE-001``) so the capture authenticates without the
        operator re-exporting them.

        When *lake_root* is given, ``--lake-root <lake_root> --roll-max-seconds 5
        --run-id live`` is appended so the capture MATERIALISES the live Parquet
        lake continuously (time-rotate + fold every ~5s — DEC-LIVE-REALTIME-FOLD-001),
        filling the dashboard's ``live_ticks`` / ``live_candles_1m`` tables in
        near-real-time. The lake root MUST be the same path the dashboard reads
        (``instrument_store.path.parent``) or the tables stay empty.

        The spawn args are remembered (:attr:`_last_start`) so :meth:`watch` can
        re-spawn on a crash.
        """
        self._last_start = {
            "out": out,
            "log_file": log_file,
            "report": report,
            "manifest": manifest,
            "seconds": seconds,
            "symbols": symbols,
            "feed": feed,
            "lake_root": lake_root,
        }
        current = self.status()
        if current.running:
            return current
        self._manifest_writer(manifest)
        # NOTE: ``capture-all`` is inherently dual-feed (spot connection #0 routes
        # indices→index-value + stocks→ltp; leg connections #1..N are ltp), so it
        # has NO ``--feed`` option — passing one makes argparse reject the whole
        # spawn (``error: unrecognized arguments: --feed both``) and the capture
        # crashes on every launch. ``feed`` is kept ONLY for the status/chip
        # display (it is ``both``), never forwarded to the subprocess.
        argv = [
            sys.executable,
            "-m",
            "brutex.live",
            "capture-all",
            "--out",
            out,
            "--report",
            report,
            "--manifest",
            manifest,
            "--symbols",
            symbols,
            "--seconds",
            str(seconds),
            "--log-file",
            log_file,
        ]
        if lake_root is not None:
            # Near-real-time materialisation: capture-all folds rotated segments to
            # this lake root every ~5s so the dashboard tables fill live. Only
            # capture-all-recognised flags are appended (verified by the runner
            # test feeding the exact argv through the real parser — the #743/#744
            # "rejected flag crashes the launch" lesson).
            argv += ["--lake-root", lake_root, "--roll-max-seconds", "5", "--run-id", "live"]
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
                "feed": feed,
                "report": report,
            }
        )
        _LOG.info(
            "live_runner_start",
            pid=int(proc.pid),
            symbols=symbols,
            feed=feed,
            out=out,
            report=report,
            manifest=manifest,
            creds_from_dashboard=bool(cred_env),
        )
        return self.status()

    def stop(self) -> LiveStatus:
        """Terminate the capture subprocess (SIGTERM→SIGKILL) and clear the state.

        Clears :attr:`_last_start` so the watchdog never resurrects a
        deliberately-stopped feed (the operator turned the toggle OFF).
        """
        state = self._read_state()
        if state is not None and self._is_live(int(state["pid"])):
            self._terminate(int(state["pid"]))
            _LOG.info("live_runner_stop", pid=int(state["pid"]))
        self._clear_state()
        self._proc = None
        self._last_start = None
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

    # ── watchdog ──────────────────────────────────────────────────
    def watch(
        self,
        *,
        max_restarts: int,
        base_backoff_s: float = _WATCH_BASE_BACKOFF_S,
        max_backoff_s: float = _WATCH_MAX_BACKOFF_S,
    ) -> int:
        """Restart the capture subprocess whenever it DIES while the feed is ON.

        The calendar-free supervisor (``DEC-LIVE-DASHBOARD-WIRE-001``, the
        operator's rule): while a :meth:`start` is in effect (``_last_start`` set,
        i.e. the toggle is ON) and the tracked subprocess is no longer running
        (a crash — feed drop, auth failure, OOM kill), it RESTARTS with bounded
        EXPONENTIAL backoff (``base_backoff_s`` doubling each consecutive restart,
        capped at ``max_backoff_s``) — no session-calendar gate, always restart
        while ON. A clean operator :meth:`stop` (``_last_start`` cleared) ends the
        loop: the watchdog never resurrects a deliberately-stopped feed.

        Bounded by *max_restarts* so a runaway crash-loop (and any test) always
        terminates — the production supervisor calls this in a thread with a high
        bound; each restart logs ``live_runner_watchdog_restart`` so a flapping
        feed is visible, never silently hammered (Golden Rule #10 honest envelope).

        Parameters
        ----------
        max_restarts
            Maximum restarts to perform before returning (the loop bound).
        base_backoff_s
            First-restart wait; doubles each consecutive restart.
        max_backoff_s
            Backoff ceiling — the wait never exceeds this.

        Returns
        -------
        int
            How many restarts were performed (``0`` if the feed stayed up / was
            stopped before any death).
        """
        restarts = 0
        backoff = base_backoff_s
        while restarts < max_restarts:
            last = self._last_start
            if last is None:
                break  # the operator stopped the feed — do not resurrect it.
            if self.status().running:
                break  # the feed is healthy — nothing to restart.
            _LOG.warning(
                "live_runner_watchdog_restart",
                restart=restarts + 1,
                backoff_s=backoff,
            )
            self._sleeper(backoff)
            # Re-check after the backoff: a stop() during the wait must abort.
            if self._last_start is None:
                break
            self.start(**last)
            restarts += 1
            backoff = min(backoff * 2.0, max_backoff_s)
        return restarts


__all__ = ["LiveRunner", "LiveStatus"]
