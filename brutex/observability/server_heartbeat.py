"""Idle heartbeat for the long-lived dashboard server — a dark gap becomes diagnosable.

DEC-AUDIT-ALERT-CLAMP-OBS-001 (the serve-observability addendum). Production
forensics of a real overnight incident found the dashboard server went
completely DARK for 8h10m — the pull finished, ``keep_awake`` released, and then
ZERO log lines until a fresh process appeared. The mechanism (dead process vs a
sleeping Mac vs a wedged loop) was UNDIAGNOSABLE precisely because an IDLE
healthy server emits nothing: silence looks identical to death.

This module closes that blind spot with ONE low-noise heartbeat: while the
server runs, :class:`ServerHeartbeat` ticks every
:data:`DEFAULT_HEARTBEAT_INTERVAL_S` on a background daemon and logs ONE
``server.heartbeat`` line carrying the process uptime + a couple of cheap gauges
(pid, RSS when trivially available). A dark gap in the log then means the
process is genuinely dead/frozen — not merely idle — so "dead vs sleeping vs
wedged" is answerable from the log alone.

Design (mirrors :class:`~brutex.dashboard.rate_sampler.RatePanelSampler`):

* **Zero I/O beyond the log line.** Each beat reads ``time.monotonic`` + one
  ``/proc/self/status`` RSS probe (already the COMMON
  :func:`~brutex.observability.process_probe.read_rss_kib` source, ``None`` on
  abstain — never fabricated) and emits ONE structured event. No files, no
  network, no lake reads.
* **Env-tunable interval, documented default, off-switch.**
  :data:`HEARTBEAT_INTERVAL_ENV` (``BRUTEX_SERVER_HEARTBEAT``) is the interval
  in SECONDS; a value of ``0`` (or any non-positive / unset-to-``0``) DISABLES
  the heartbeat entirely. Junk is FAIL-SAFE: it falls back to the default and
  logs one note, never crashing the serve boot (an observability aid must never
  take the server down).
* **Fail-safe beat.** A beat that raises (a probe glitch, a logging backend
  hiccup) is swallowed + noted — a heartbeat error can NEVER crash the server it
  exists to watch (Golden Rule #10).

The spawn / clock / RSS-probe seams are injectable so the loop is 100%
unit-testable with no real thread and no real clock.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from typing import TYPE_CHECKING, Final

import structlog

from brutex.observability.process_probe import read_rss_kib

if TYPE_CHECKING:
    from collections.abc import Callable

    from structlog.stdlib import BoundLogger

_LOG = structlog.get_logger("brutex.observability.server_heartbeat")

__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "HEARTBEAT_INTERVAL_ENV",
    "ServerHeartbeat",
    "resolve_heartbeat_interval",
    "start_server_heartbeat",
]

#: The env var carrying the heartbeat interval in SECONDS. ``0`` disables.
HEARTBEAT_INTERVAL_ENV: Final[str] = "BRUTEX_SERVER_HEARTBEAT"

#: Default cadence (seconds) when the env is unset — low-noise (~1/min) so the
#: log stays readable while a >1 min dark gap is still obvious.
DEFAULT_HEARTBEAT_INTERVAL_S: Final[float] = 60.0


def resolve_heartbeat_interval(raw: str | None = None) -> float:
    """Resolve the heartbeat interval (seconds) from *raw* / the environment.

    Parameters
    ----------
    raw
        The raw env value (``None`` reads :data:`HEARTBEAT_INTERVAL_ENV` from
        ``os.environ``; pass explicitly in tests).

    Returns
    -------
    float
        The interval in seconds. ``0.0`` means DISABLED (an unset env keeps the
        :data:`DEFAULT_HEARTBEAT_INTERVAL_S` default; ``"0"`` / any non-positive
        value disables). A non-numeric junk value is FAIL-SAFE — it falls back
        to the default (never raises: the heartbeat must never break serve
        boot).
    """
    value = os.environ.get(HEARTBEAT_INTERVAL_ENV) if raw is None else raw
    if value is None or value.strip() == "":
        return DEFAULT_HEARTBEAT_INTERVAL_S
    try:
        parsed = float(value.strip())
    except ValueError:
        _LOG.warning(
            "server.heartbeat.bad_interval",
            env=HEARTBEAT_INTERVAL_ENV,
            value=value,
            note=f"non-numeric — falling back to the {DEFAULT_HEARTBEAT_INTERVAL_S}s default",
        )
        return DEFAULT_HEARTBEAT_INTERVAL_S
    if parsed <= 0:
        return 0.0  # explicit off-switch (0 / negative)
    return parsed


def _daemon_spawn(fn: Callable[[], None]) -> None:
    """Run *fn* on a named daemon thread (the production background-loop spawner)."""
    threading.Thread(target=fn, name="dashboard-server-heartbeat", daemon=True).start()


class ServerHeartbeat:
    """A background daemon that logs ONE ``server.heartbeat`` per *interval* seconds.

    Best-effort + fail-safe: a beat that raises is swallowed (a probe / logging
    glitch must never crash the server). The clock, RSS probe, and spawner are
    injectable for deterministic unit tests.
    """

    def __init__(
        self,
        *,
        interval: float,
        clock: Callable[[], float] = time.monotonic,
        rss_probe: Callable[[], int | None] = read_rss_kib,
        spawn: Callable[[Callable[[], None]], None] = _daemon_spawn,
        log: BoundLogger | None = None,
    ) -> None:
        """Build a heartbeat that ticks every *interval* seconds.

        Parameters
        ----------
        interval
            Seconds between beats. Must be positive (a caller disables the
            heartbeat by simply not constructing / not starting one — see
            :func:`start_server_heartbeat`).
        clock
            Monotonic clock source (injectable for tests). Uptime is measured
            from the moment :meth:`start` is called.
        rss_probe
            Resident-set-size probe (KiB, ``None`` = abstain) — the COMMON
            :func:`~brutex.observability.process_probe.read_rss_kib` default.
        spawn
            Background-loop spawner (default a daemon thread). Tests inject a
            capturing fake so no real thread runs.
        log
            Structured logger (defaults to the module logger).
        """
        self._interval = interval
        self._clock = clock
        self._rss_probe = rss_probe
        self._spawn = spawn
        self._log = log if log is not None else _LOG
        self._stop = threading.Event()
        self._running = False
        self._started_at = 0.0

    def _beat(self) -> None:
        """Emit ONE ``server.heartbeat`` — fail-safe (never raises)."""
        try:
            uptime_s = round(self._clock() - self._started_at, 1)
            rss_kib = self._rss_probe()
            rss_mb = round(rss_kib / 1024, 1) if rss_kib is not None else None
            self._log.info(
                "server.heartbeat",
                uptime_s=uptime_s,
                pid=os.getpid(),
                rss_mb=rss_mb,
                interval_s=self._interval,
            )
        except Exception as exc:  # pylint: disable=broad-except — a beat must never crash serve
            # Even the error-note log is best-effort: a wedged logging backend
            # must not crash the watcher (contextlib.suppress swallows it).
            with contextlib.suppress(Exception):
                self._log.warning(
                    "server.heartbeat.error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    def _run(self) -> None:
        """Beat every *interval* until :meth:`stop`.

        ``Event.wait`` returns ``True`` immediately when :meth:`stop` sets the
        event (prompt shutdown) and ``False`` on timeout (a due beat), so a
        stopped heartbeat emits NOTHING further.
        """
        while not self._stop.wait(self._interval):
            self._beat()

    def start(self) -> None:
        """Spawn the heartbeat daemon (idempotent — a second call is a no-op)."""
        if self._running:
            return
        self._running = True
        self._stop.clear()
        self._started_at = self._clock()
        self._spawn(self._run)

    def stop(self) -> None:
        """Signal the daemon to exit (idempotent, best-effort)."""
        self._stop.set()
        self._running = False

    def __enter__(self) -> ServerHeartbeat:
        """Start the heartbeat on context entry."""
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop the heartbeat on context exit (never suppresses the exception)."""
        self.stop()


def start_server_heartbeat(
    *,
    raw_interval: str | None = None,
    log: BoundLogger | None = None,
    spawn: Callable[[Callable[[], None]], None] = _daemon_spawn,
) -> ServerHeartbeat | None:
    """Resolve the interval and start a heartbeat — or ``None`` when disabled.

    The ONE serve-path entry point: resolves :data:`HEARTBEAT_INTERVAL_ENV`
    (``0`` disables), and on a positive interval constructs + starts a
    :class:`ServerHeartbeat` (returning it so the caller can :meth:`~ServerHeartbeat.stop`
    it on shutdown). When disabled it logs ONE ``server.heartbeat.disabled`` note
    and returns ``None``. Best-effort: it never raises into the serve boot.

    Parameters
    ----------
    raw_interval
        Raw env override (``None`` reads the environment; pass in tests).
    log
        Structured logger (defaults to the module logger).
    spawn
        Background-loop spawner (default a daemon thread; injectable for tests).
    """
    resolved_log = log if log is not None else _LOG
    interval = resolve_heartbeat_interval(raw_interval)
    if interval <= 0:
        resolved_log.info(
            "server.heartbeat.disabled",
            env=HEARTBEAT_INTERVAL_ENV,
            note="idle heartbeat OFF — a dark log gap is NOT distinguishable from a live idle server",
        )
        return None
    heartbeat = ServerHeartbeat(interval=interval, spawn=spawn, log=resolved_log)
    heartbeat.start()
    resolved_log.info(
        "server.heartbeat.started",
        interval_s=interval,
        env=HEARTBEAT_INTERVAL_ENV,
    )
    return heartbeat
