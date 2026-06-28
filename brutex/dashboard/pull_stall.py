"""Hung / stalled dashboard-pull detector (DEC-PULL-STALL-DETECTION-001).

The dashboard background pull (:class:`brutex.dashboard.pull_jobs.BackgroundPullJob`)
runs the pull *work* off the request thread and clears its single-flight ``_running``
flag ONLY when the worker returns or raises. A worker that HANGS — e.g. wedged on the
no-timeout partition ``flock`` (``brutex/data/partition_lock.py``), a deadlocked socket,
an infinite vendor retry — never reaches that ``finally``, so ``_running`` stays ``True``
forever and the single-flight guard then SILENTLY refuses every future pull. The proven
:class:`brutex.observability.watchdog.Watchdog` heartbeat primitive existed but was wired
ONLY into the sweep engine, never the dashboard pull — so a hung pull was undetectable
(the failure hunt's GAP 1, MAJOR).

This module closes that gap by mirroring the sweep-engine watchdog wiring
(:func:`brutex.engine.sweep_supervisor._drive_watchdog`) at the dashboard-pull level:

* :class:`PullWatchdog` owns the :class:`~brutex.observability.watchdog.Watchdog`
  hysteresis state machine (HEALTHY↔HUNG, monotonic clock, edge-only callbacks) for ONE
  pull worker. The pull worker calls :meth:`PullWatchdog.beat` on **real progress** (per
  fetched window), and a background timer thread calls :meth:`PullWatchdog.check_once` on
  a cadence so a hung worker — which cannot poll itself — is still detected.
* On a HEALTHY→HUNG edge the watchdog fires the operator-supplied ``on_hung`` callback
  (the dashboard wires a CRITICAL Telegram alert + transitions the job to a stalled state
  so ``_running`` is cleared and future pulls are not silently refused).

## Why a separate timer thread

The pull worker thread is exactly the thread that hangs, so it cannot detect its own
stall. A daemon timer thread ticking :meth:`check_once` is the only place a hung worker
can be observed — identical to the sweep supervisor's ``sweep-watchdog`` daemon.

## Honest envelope (Golden Rule #10)

This DETECTS + alerts a stall (the operator's real-time guarantee) — it does NOT itself
PREVENT the underlying hang (the no-timeout ``flock`` is a separate MINOR follow-up). The
timeout is an operator-tunable heuristic (constant / env), not a vendor limit; a breach
is an OBSERVATION + alert, never auto-remediation of the wedged I/O. Every callback is
best-effort: a raising ``on_hung`` is swallowed + logged by the underlying watchdog and
can never crash the timer or the pull. ``beat`` / ``check_once`` are O(1) (one worker).

## Decision: DEC-PULL-STALL-DETECTION-001 (MIGRATION.md entry #945).
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Final

from brutex.observability.watchdog import Watchdog, WorkerEvent, WorkerState

logger = logging.getLogger(__name__)

#: The single worker name the dashboard pull beats under. One job runs at a time
#: (single-flight), so one worker name is enough.
PULL_WORKER_NAME: Final[str] = "dashboard-pull"

#: Default stall timeout (seconds): a dashboard pull with no fetched-window heartbeat for
#: this long is declared HUNG. Generous — a healthy pull beats every settled window
#: (~1 s cadence under load), so only a true wedge (deadlocked flock / socket) trips it,
#: never a slow-but-progressing backfill. Operator-tunable via :data:`PULL_STALL_ENV`.
DEFAULT_STALL_TIMEOUT_SECONDS: Final[float] = 600.0

#: Environment override for the stall timeout (operator escape hatch, no code change).
PULL_STALL_ENV: Final[str] = "BRUTEX_PULL_STALL_TIMEOUT_SECONDS"


def resolve_stall_timeout_seconds() -> float:
    """Resolve the pull stall timeout: env override, else the audited default.

    Reads :data:`PULL_STALL_ENV` (:data:`BRUTEX_PULL_STALL_TIMEOUT_SECONDS`); falls back to
    :data:`DEFAULT_STALL_TIMEOUT_SECONDS` when unset, blank, non-numeric, or non-positive —
    a bad value can never crash a pull or silently disable the detector (fail-safe to the
    audited default, mirroring :func:`brutex.adapters.groww.constants.resolve_pool_size`).
    O(1).
    """
    raw = os.environ.get(PULL_STALL_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_STALL_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_STALL_TIMEOUT_SECONDS
    if value <= 0.0:
        return DEFAULT_STALL_TIMEOUT_SECONDS
    return value


#: The HEALTHY→HUNG edge callback (one worker → one event). Best-effort: a raising
#: callback is swallowed + logged by the underlying :class:`Watchdog`.
OnHung = Callable[[WorkerEvent], None]

#: Background timer spawner: runs *fn* (the poll loop) off the caller's thread. The default
#: spawns a daemon thread; tests inject a synchronous / capturing runner for determinism.
TimerSpawn = Callable[[Callable[[], None]], None]


def _daemon_spawn(fn: Callable[[], None]) -> None:  # pragma: no cover — real-thread default
    """Run *fn* in a fire-and-forget daemon thread (the live default)."""
    threading.Thread(target=fn, name="dashboard-pull-watchdog", daemon=True).start()


class PullWatchdog:
    """Heartbeat watchdog for ONE dashboard pull worker — a thin, value-adding wrapper.

    Mirrors :class:`brutex.engine.sweep_watchdog.SweepWatchdog`: the underlying
    :class:`~brutex.observability.watchdog.Watchdog` owns the HEALTHY↔HUNG hysteresis +
    monotonic clock; this wrapper adds (a) pull-appropriate defaults, (b) the background
    poll loop + ``stop`` lifecycle so a hung worker (which blocks its own thread) is still
    detected, and (c) thread-safety so the worker thread can ``beat`` while the timer
    thread ``check_once``s.

    Parameters
    ----------
    on_hung
        Edge callback fired once per HEALTHY→HUNG transition. The dashboard wires a
        CRITICAL Telegram alert + the job's stalled-transition. Best-effort — a raising
        callback is swallowed by the underlying watchdog.
    timeout_seconds
        Stall timeout (defaults to :func:`resolve_stall_timeout_seconds`). Must be ``> 0``.
    spawn
        Background timer spawner (defaults to a daemon thread). Inject a capturing runner
        in tests for determinism.
    clock
        Monotonic-seconds source forwarded to the underlying :class:`Watchdog` (defaults
        to :func:`time.monotonic`); injected for deterministic tests.
    """

    def __init__(
        self,
        *,
        on_hung: OnHung,
        timeout_seconds: float | None = None,
        spawn: TimerSpawn = _daemon_spawn,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._timeout = (
            resolve_stall_timeout_seconds() if timeout_seconds is None else timeout_seconds
        )
        self._spawn = spawn
        self._lock = threading.Lock()
        self._stop = threading.Event()
        wd_kwargs: dict[str, object] = {
            "timeout_seconds": self._timeout,
            "on_hung": on_hung,
            # A recovery edge is informational only — the dashboard never resumes a
            # declared-hung worker (it has already cleared _running), so there is nothing
            # to un-do; log it and move on.
            "on_recovery": self._log_recovery,
        }
        if clock is not None:
            wd_kwargs["clock"] = clock
        self._wd = Watchdog(**wd_kwargs)  # type: ignore[arg-type]

    @property
    def timeout_seconds(self) -> float:
        """The configured stall timeout (seconds)."""
        return self._timeout

    @staticmethod
    def _log_recovery(event: WorkerEvent) -> None:
        """Log a HUNG→HEALTHY edge (informational — no resume, no alert)."""
        logger.info(
            "pull_stall.recovery worker=%s last_beat_seconds_ago=%.1f",
            event.worker_name,
            event.last_beat_seconds_ago,
        )

    def start(self, *, poll_seconds: float | None = None) -> None:
        """Register the worker HEALTHY + launch the background poll loop.

        *poll_seconds* defaults to ``timeout / 3`` (the sweep-watchdog cadence) so a stall
        is detected within ~one extra poll of the timeout. The worker is registered up
        front so the very first :meth:`check_once` after the timeout can fire even if no
        ``beat`` ever landed (a worker that hangs before its first window).
        """
        with self._lock:
            self._wd.register(PULL_WORKER_NAME)
        interval = self._timeout / 3.0 if poll_seconds is None else poll_seconds
        self._stop.clear()
        self._spawn(lambda: self._drive(interval))

    def _drive(self, interval: float) -> None:
        """Timer-loop body: :meth:`check_once` every *interval* s until stopped.

        Exits promptly when :meth:`stop` sets the event (``Event.wait`` returns ``True``);
        otherwise (a timeout → ``False``) it polls once and loops. Mirrors
        :func:`brutex.engine.sweep_supervisor._drive_watchdog`.
        """
        while not self._stop.wait(interval):
            self.check_once()

    def beat(self) -> None:
        """Record a heartbeat (real progress — a fetched window) — O(1), thread-safe."""
        with self._lock:
            self._wd.beat(PULL_WORKER_NAME)

    def check_once(self) -> tuple[WorkerEvent, ...]:
        """Poll the heartbeat, firing the hung edge if the worker wedged — O(1)."""
        with self._lock:
            return self._wd.check_once()

    def stop(self) -> None:
        """Stop the background poll loop (called from the worker's ``finally``)."""
        self._stop.set()

    def state(self) -> WorkerState:
        """Return the worker's current state (HEALTHY / HUNG) — test accessor."""
        with self._lock:
            return self._wd.state_of(PULL_WORKER_NAME)


__all__ = [
    "DEFAULT_STALL_TIMEOUT_SECONDS",
    "PULL_STALL_ENV",
    "PULL_WORKER_NAME",
    "OnHung",
    "PullWatchdog",
    "TimerSpawn",
    "resolve_stall_timeout_seconds",
]
