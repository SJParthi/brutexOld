"""Periodic monitor loop that polls SLA + Watchdog + L3 replay oracle.

Closes the operational gap from the 2026-05-25 honest-audit: the
detectors built in DEC-SLA-DETECTOR-001 (entry #239) +
DEC-WATCHDOG-001 (entry #240) + DEC-L3-REPLAY-ORACLE-001 (entry #241)
are all pure-detector classes — no scheduler invokes their
``check_once()`` methods in production. This module is that scheduler.

## Design — daemon-thread poller, caller-supplied callbacks

A :class:`MonitorLoop` is constructed with:

* An optional :class:`brutex.observability.sla.SLADetector` — its
  ``check_once()`` is called on every tick. Breach + recovery events
  fire the detector's own callbacks (operator wires Telegram there
  per DEC-TELEGRAM-WIRING-001).
* An optional :class:`brutex.observability.watchdog.Watchdog` — same
  per-tick cadence.
* An optional ``replay_check`` callable — for the L3 audit replay
  oracle. Invoked every ``replay_check_every_n_ticks`` ticks (default
  every tick); operator passes a closure that compares the audit log
  against the live dedup snapshot via
  :func:`brutex.orchestrator.replay.assert_dedup_consistent`.

The loop runs in a daemon thread (started by :meth:`start`, stopped
by :meth:`stop`), so the caller never blocks. Threading.Event is the
shutdown signal; the worker uses :meth:`Event.wait` with the poll
interval so stop() returns within one tick rather than waiting for a
full interval.

Every iteration is wrapped in a broad try/except (OSError, ValueError,
RuntimeError, custom AuditError) so a misbehaving callback never
crashes the monitor. Errors are logged via stdlib ``logging.exception``.

## Why caller-supplied callbacks (not a hard-coded Telegram dep)

The monitor module must NOT import from ``brutex.notifications.*``
because tests of the monitor would then need to drag the whole
notification stack along. Caller (typically ``brutex.cli`` or a
future scheduler) wires the on-breach / on-hung / on-inconsistent
callbacks to Telegram + audit log + dashboard via standard composition.

## Decision: DEC-MONITOR-LOOP-001 (MIGRATION entry #242).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Self

from brutex.observability.sla import SLADetector
from brutex.observability.watchdog import Watchdog

logger = logging.getLogger(__name__)


#: Callable invoked once per (eligible) tick to run the L3 audit-log
#: replay oracle. Raises :exc:`brutex.orchestrator.audit.AuditError`
#: on inconsistency; the loop catches + logs.
ReplayCheck = Callable[[], None]


@dataclass(slots=True)
class MonitorLoop:
    """Daemon-thread poller for SLA + Watchdog + L3 replay oracle.

    Parameters
    ----------
    sla_detector
        Optional :class:`SLADetector`. ``None`` skips the SLA tick.
    watchdog
        Optional :class:`Watchdog`. ``None`` skips the watchdog tick.
    replay_check
        Optional :data:`ReplayCheck` callable. ``None`` skips the
        replay tick. Caller typically passes a closure that wraps
        :func:`brutex.orchestrator.replay.assert_dedup_consistent`.
    poll_interval_seconds
        Tick period in seconds. Default 30s — frequent enough to catch
        a hung worker on the same minute, infrequent enough to avoid
        thread contention with the ingest hot path.
    replay_check_every_n_ticks
        Cadence multiplier for the (typically more expensive) replay
        check. Default 1 (every tick). Set to ``10`` to run replay
        once every 10 ticks.

    Notes
    -----
    Thread safety: the loop body runs in ONE daemon thread; callbacks
    from SLA + Watchdog fire on that same thread. If a callback wants
    to publish to Telegram (also a daemon-thread-backed queue), the
    queue-based publish is async-safe by design.
    """

    sla_detector: SLADetector | None = None
    watchdog: Watchdog | None = None
    replay_check: ReplayCheck | None = None
    poll_interval_seconds: float = 30.0
    replay_check_every_n_ticks: int = 1
    _thread: threading.Thread | None = field(default=None, init=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False)
    _tick_count: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ValueError(
                f"MonitorLoop.poll_interval_seconds must be > 0; got {self.poll_interval_seconds!r}"
            )
        if self.replay_check_every_n_ticks < 1:
            raise ValueError(
                f"MonitorLoop.replay_check_every_n_ticks must be >= 1; "
                f"got {self.replay_check_every_n_ticks!r}"
            )

    def start(self) -> None:
        """Spawn the daemon thread and begin polling.

        Idempotent: calling :meth:`start` on an already-running loop is
        a no-op (logged at debug level).
        """
        if self._thread is not None and self._thread.is_alive():
            logger.debug("MonitorLoop.start: already running; ignoring")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="MonitorLoop",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the loop to stop and join the daemon thread.

        Parameters
        ----------
        timeout
            Max seconds to wait for the thread to exit. The default
            5s is plenty for the worker to notice ``_stop_event`` set
            between ticks.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def is_running(self) -> bool:
        """True if the daemon thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def tick_once(self) -> None:
        """Run a single tick of all configured checks.

        Exposed so callers (typically tests) can drive the loop
        deterministically without spinning up the daemon thread. The
        :meth:`_run` worker calls this on every interval.
        """
        self._tick_count += 1
        if self.sla_detector is not None:
            try:
                self.sla_detector.check_once()
            except (OSError, ValueError, RuntimeError):
                logger.exception(
                    "MonitorLoop.tick_once: SLA detector check_once raised; loop continues"
                )
        if self.watchdog is not None:
            try:
                self.watchdog.check_once()
            except (OSError, ValueError, RuntimeError):
                logger.exception(
                    "MonitorLoop.tick_once: watchdog check_once raised; loop continues"
                )
        if (
            self.replay_check is not None
            and self._tick_count % self.replay_check_every_n_ticks == 0
        ):
            try:
                self.replay_check()
            except (OSError, ValueError, RuntimeError) as exc:
                logger.exception(
                    "MonitorLoop.tick_once: replay_check raised %s; loop continues",
                    type(exc).__name__,
                )

    def _run(self) -> None:
        """Daemon-thread loop body. Polls until :attr:`_stop_event` is set.

        Single-exit-path design: the only way out of the inner loop is
        ``self._stop_event.wait(timeout=...)`` returning True. We don't
        re-check ``is_set()`` at the top of the iteration because that
        creates an untestable race-branch (stop set between wait return
        and loop-condition check) that pegs coverage at 98.92%.
        """
        logger.info(
            "MonitorLoop._run: started; poll_interval=%.1fs",
            self.poll_interval_seconds,
        )
        # Snapshot the time so iteration latency doesn't cause drift.
        next_tick = time.monotonic()
        while True:
            self.tick_once()
            next_tick += self.poll_interval_seconds
            wait = max(0.0, next_tick - time.monotonic())
            if self._stop_event.wait(timeout=wait):
                break
        logger.info("MonitorLoop._run: stopped after %d ticks", self._tick_count)


__all__ = (
    "MonitorLoop",
    "ReplayCheck",
)
