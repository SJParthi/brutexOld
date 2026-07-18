"""Watchdog — hung-worker detector + heartbeat timer.

Closes the 2026-05-25 honest-audit row 17 (watchdog was MISSING)
and is the third of the operator's "AUTOMATED" pillar per CLAUDE.md §11
("Release Please + auto-merge + watchdog + Telegram + auto-resume").

## Design — heartbeat registry + timeout check

The watchdog is conceptually the dead-man's-switch on the train: every
worker registers a heartbeat-name on startup, then calls
:meth:`Watchdog.beat` periodically (after each work item, every N
seconds, etc.). The :meth:`Watchdog.check_once` method walks the
registry and identifies any worker whose last heartbeat is older than
the configured timeout — those are HUNG. State transitions
(HEALTHY → HUNG and HUNG → HEALTHY) fire the operator-supplied callbacks.

Like :class:`brutex.observability.sla.SLADetector` (DEC-SLA-DETECTOR-001),
the design is:

* **Pure-detector, hysteresis-aware** — only edge transitions fire callbacks
  so a hung worker doesn't spam alerts on every check.
* **No threads / no daemons** — the caller picks the cadence (scheduler,
  PhaseRunner subscriber, periodic timer in the dashboard, etc.).
* **Caller-supplied clock** — :class:`Watchdog` accepts a ``clock``
  callable (defaults to :func:`time.monotonic`) so tests use a fake
  clock and avoid sleeping.
* **Caller-supplied callbacks** — sink-agnostic; operator wires to
  Telegram via :func:`brutex.notifications.wiring.make_pipeline_event_subscriber`
  or to the audit log or to a Prometheus alert.

## Why monotonic clock

Wall-clock (`time.time`) can jump backwards on NTP sync, daylight-saving
changes, or operator-mode clock-drift. The watchdog compares "now"
against "last heartbeat" — a backwards jump would falsely declare every
worker healthy. :func:`time.monotonic` is guaranteed non-decreasing.

## Decision: DEC-WATCHDOG-001 (MIGRATION entry #240).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from brutex.observability import observability_rust

logger = logging.getLogger(__name__)


def _worker_decide_python(
    now: float,
    last_beat: float,
    timeout_seconds: float,
    prev_hung: bool,
) -> tuple[float, bool, bool]:
    """The per-worker HEALTHY↔HUNG decision — the LIVE parity oracle.

    A disclosed COMPOSITION of the loop-inlined lines of
    :meth:`Watchdog.check_once` (DEC-OBS-RUST-DETECTOR-CORE-001 — the
    Slice-0 exemption): ``elapsed = now - last_beat`` (NO clamp — negative
    elapsed is legal and preserved), strict ``>`` (elapsed exactly AT the
    timeout is NOT hung), and the two-state edge law
    (``is_hung != prev_hung`` ≡ ``new_state != prev_state`` over the
    2-member :class:`WorkerState`). Flag-OFF the seam returns this
    function's result DIRECTLY — the default path is byte-identical.
    """
    elapsed = now - last_beat
    is_hung = elapsed > timeout_seconds
    return elapsed, is_hung, is_hung != prev_hung


# ────────────────────────────────────────────────────────────────────────
# Enums + dataclasses
# ────────────────────────────────────────────────────────────────────────


class WorkerState(StrEnum):
    """Per-worker state. HUNG = last heartbeat older than the timeout.

    Hysteresis: state changes ONLY on edge transitions, so callbacks
    don't spam every check pass while a worker stays hung.
    """

    HEALTHY = "healthy"
    HUNG = "hung"


@dataclass(frozen=True, slots=True)
class WorkerEvent:
    """Emitted on every state transition (HEALTHY ↔ HUNG)."""

    worker_name: str
    new_state: WorkerState
    last_beat_seconds_ago: float
    timeout_seconds: float


#: Callback signature for hung + recovered events.
WorkerEventCallback = Callable[[WorkerEvent], None]

#: Clock callable — returns monotonic seconds. Default is
#: :func:`time.monotonic`; tests pass a fake to avoid sleeping.
Clock = Callable[[], float]


# ────────────────────────────────────────────────────────────────────────
# Watchdog
# ────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Watchdog:
    """Hung-worker detector.

    Parameters
    ----------
    timeout_seconds
        A worker is HUNG if its last heartbeat is older than this many
        seconds.
    on_hung
        Callback fired when a worker transitions HEALTHY → HUNG.
    on_recovery
        Callback fired when a worker transitions HUNG → HEALTHY.
    clock
        Monotonic-seconds source. Default is :func:`time.monotonic`.

    Notes
    -----
    Callback exceptions are caught + logged. A buggy callback never
    crashes the detector — Golden Rule #10.

    The watchdog tracks workers by name; calling :meth:`beat` for a
    previously-unknown name registers the worker (lazy registration).
    Callers can also explicitly :meth:`register` to declare a worker
    ahead of its first heartbeat.
    """

    timeout_seconds: float
    on_hung: WorkerEventCallback
    on_recovery: WorkerEventCallback
    clock: Clock = time.monotonic
    _last_beat: dict[str, float] = field(default_factory=dict)
    _state: dict[str, WorkerState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError(f"Watchdog.timeout_seconds must be > 0; got {self.timeout_seconds!r}")

    def register(self, worker_name: str) -> None:
        """Explicitly declare a worker. Sets state to HEALTHY and
        initialises the last-beat timestamp to ``now``.

        Useful when the caller wants every registered worker to be
        eligible for the HUNG check before its first real heartbeat.
        """
        now = self.clock()
        self._last_beat[worker_name] = now
        self._state.setdefault(worker_name, WorkerState.HEALTHY)

    def beat(self, worker_name: str) -> None:
        """Record a heartbeat for *worker_name*.

        Auto-registers the worker if it's not already known (state
        starts as HEALTHY). If the worker was HUNG, the
        :meth:`check_once` call after this beat will fire the
        recovery callback.
        """
        now = self.clock()
        self._last_beat[worker_name] = now
        self._state.setdefault(worker_name, WorkerState.HEALTHY)

    def state_of(self, worker_name: str) -> WorkerState:
        """Return the current per-worker state (HEALTHY or HUNG)."""
        if worker_name not in self._state:
            raise KeyError(f"No worker named {worker_name!r}")
        return self._state[worker_name]

    def workers(self) -> tuple[str, ...]:
        """Return the sorted tuple of registered worker names."""
        return tuple(sorted(self._state.keys()))

    def check_once(self) -> tuple[WorkerEvent, ...]:
        """Walk the heartbeat registry, fire callbacks on state transitions.

        Returns
        -------
        tuple[WorkerEvent, ...]
            The events fired this pass (may be empty if no worker
            transitioned). Useful for tests + audit logs.
        """
        emitted: list[WorkerEvent] = []
        now = self.clock()
        for name in tuple(self._state.keys()):
            # The decision routes through the default-OFF observability_rust
            # facade with `_worker_decide_python` as the live oracle
            # (DEC-OBS-RUST-DETECTOR-CORE-001; branch-free here — all
            # flag/guard/burn-in branching lives facade-side). prev_state is
            # read BEFORE the decision (a side-effect-free dict read; the
            # ONE disclosed re-ordering vs the pre-seam loop).
            prev_state = self._state[name]
            elapsed, is_hung, edge = observability_rust.routed_watchdog_decide(
                now,
                self._last_beat[name],
                self.timeout_seconds,
                prev_state == WorkerState.HUNG,
                python_law=_worker_decide_python,
            )
            new_state = WorkerState.HUNG if is_hung else WorkerState.HEALTHY
            if not edge:
                continue  # hysteresis: steady state, no edge
            self._state[name] = new_state
            event = WorkerEvent(
                worker_name=name,
                new_state=new_state,
                last_beat_seconds_ago=elapsed,
                timeout_seconds=self.timeout_seconds,
            )
            emitted.append(event)
            try:
                cb = self.on_hung if new_state == WorkerState.HUNG else self.on_recovery
                cb(event)
            except (OSError, ValueError, RuntimeError):
                logger.exception(
                    "Watchdog.check_once: callback raised for worker %r → %s; watchdog continues",
                    name,
                    new_state.value,
                )
        return tuple(emitted)


__all__ = (
    "Clock",
    "Watchdog",
    "WorkerEvent",
    "WorkerEventCallback",
    "WorkerState",
    "make_heartbeat_subscriber",
)


# ────────────────────────────────────────────────────────────────────────
# Pipeline-integration helper
# ────────────────────────────────────────────────────────────────────────

# Local import inside the function body so this module stays
# import-cycle safe: brutex.orchestrator.pipeline transitively imports
# bloom → fastbloom_rs, and we don't want every Watchdog construction
# to drag that in unless the caller actually uses the helper.


def make_heartbeat_subscriber(
    watchdog: Watchdog,
    worker_name: str,
) -> Callable[[object], None]:
    """Return a pipeline `EventSubscriber` that beats *watchdog* on every
    pipeline event.

    The pipeline driver (`brutex.orchestrator.pipeline.PhaseRunner`)
    fans every `PipelineEvent` out to its registered subscribers. The
    callable returned here records a heartbeat for ``worker_name`` on
    EACH event — phase start, per-item complete, phase complete,
    everything. That keeps the watchdog timer fresh whenever the
    pipeline is making forward progress; a stall (no events for >
    ``watchdog.timeout_seconds``) will eventually trip the
    HEALTHY → HUNG transition.

    Parameters
    ----------
    watchdog
        The :class:`Watchdog` instance to beat.
    worker_name
        The name registered (or auto-registered) by the heartbeat.

    Returns
    -------
    Callable[[object], None]
        A `Sequence[EventSubscriber]`-compatible callable. Annotated
        as ``Callable[[object], None]`` to avoid importing
        ``PipelineEvent`` at module load time (would trigger the
        orchestrator package init chain — same fastbloom_rs PyO3 ABI3
        constraint that DEC-TELEGRAM-WIRING-001 documented).

    Notes
    -----
    Exceptions raised by :meth:`Watchdog.beat` would never occur in
    practice (beat is dict-write only), but Golden Rule #10 says the
    pipeline must not crash on a misbehaving subscriber — the
    pipeline's own subscriber fanout already swallows callback errors.
    """

    def _subscriber(_event: object) -> None:
        watchdog.beat(worker_name)

    return _subscriber
