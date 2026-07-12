"""Heartbeat + hung-combo watchdog for a running sweep (DEC-SWEEP-REALTIME-GUARANTEE-001).

The dead-man's-switch for the brute-force sweep: every sweep worker calls
:meth:`SweepWatchdog.beat` after each combo it finishes, and a supervisor calls
:meth:`SweepWatchdog.check_once` on a cadence to detect any worker whose last
heartbeat is older than the configured timeout — a **hung combo** (an infinite
loop / wedged kernel / deadlocked I/O). It is the "watchdog" pillar of the
operator's AUTOMATED decree (CLAUDE.md §11).

It is a thin, value-adding composition over the proven
:class:`brutex.observability.watchdog.Watchdog` primitive (DEC-WATCHDOG-001):
that primitive owns the HEALTHY↔HUNG hysteresis state machine + the monotonic
clock discipline; this wrapper adds (a) sweep-appropriate defaults, (b) O(1)
healthy / hung tallies for a real-time status snapshot, and (c) thread-safety so
many worker threads can ``beat`` while one supervisor thread ``check_once``s.

## Why a monotonic clock (inherited from the primitive)

Wall-clock time can jump backwards (NTP / DST), which would falsely declare a
hung worker healthy. :class:`Watchdog` uses :func:`time.monotonic` (injectable
for tests); this wrapper passes it straight through.

## Honest envelope (Golden Rule #10)

:meth:`beat` / :meth:`register` / :meth:`snapshot` are O(1). :meth:`check_once`
is O(W) in the number of registered workers (it must scan every heartbeat) —
this is inherent to a timeout sweep and is bounded by the worker-pool size
(≤ a few dozen per CLAUDE.md §8), **not** the combo count. The watchdog detects
a hang and fires callbacks; it does **not** kill the worker — termination /
restart is the supervisor's policy decision, wired in a Wave-2 follow-up.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Final

from brutex.observability.watchdog import Watchdog, WorkerEvent, WorkerState

#: Default hung-combo timeout. A single spot combo over the full 2020→today 1m
#: history is sub-second; 300 s is a generous ceiling that only trips on a true
#: wedge, not a slow-but-progressing combo. Tune per hardware envelope.
#: G2 (DEC-SWEEP-UNKILLABLE-CORE-001): the PRODUCTION launch resolves its
#: timeout through :func:`brutex.engine.sweep_stall_policy.resolve_stall_policy`
#: (env ``BRUTEX_SWEEP_STALL_SECONDS`` / raise-only estimate scaling over this
#: SAME base — the policy module imports this constant, ONE source of truth)
#: and injects it via ``timeout_seconds=``; this default stays the exact 300 s
#: identity for every other caller.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 300.0

#: Optional per-event callback (HEALTHY→HUNG or HUNG→HEALTHY).
SweepWatchdogCallback = Callable[[WorkerEvent], None]


class SweepWatchdog:
    """Thread-safe hung-combo detector for a running sweep.

    Parameters
    ----------
    timeout_seconds
        A worker is HUNG when its last heartbeat is older than this many
        seconds (defaults to :data:`DEFAULT_TIMEOUT_SECONDS`). Must be ``> 0``.
    on_hung
        Optional callback fired once per HEALTHY→HUNG edge.
    on_recovery
        Optional callback fired once per HUNG→HEALTHY edge.
    clock
        Monotonic-seconds source forwarded to the underlying
        :class:`~brutex.observability.watchdog.Watchdog` (defaults to
        :func:`time.monotonic`); injected for deterministic tests.

    Raises
    ------
    ValueError
        If ``timeout_seconds <= 0`` (propagated from :class:`Watchdog`).
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        on_hung: SweepWatchdogCallback | None = None,
        on_recovery: SweepWatchdogCallback | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._on_hung = on_hung
        self._on_recovery = on_recovery
        self._lock = threading.Lock()
        self._known: set[str] = set()
        self._hung_count = 0
        # The primitive requires concrete callbacks; route both through our
        # tally-then-forward handlers so the O(1) snapshot stays accurate.
        wd_kwargs: dict[str, Any] = {
            "timeout_seconds": timeout_seconds,
            "on_hung": self._handle_hung,
            "on_recovery": self._handle_recovery,
        }
        if clock is not None:
            wd_kwargs["clock"] = clock
        self._wd = Watchdog(**wd_kwargs)

    @property
    def timeout_seconds(self) -> float:
        """The configured hung-combo timeout (seconds)."""
        return self._wd.timeout_seconds

    def register(self, worker_name: str) -> None:
        """Declare *worker_name* HEALTHY ahead of its first heartbeat — O(1)."""
        with self._lock:
            self._wd.register(worker_name)
            self._known.add(worker_name)

    def beat(self, worker_name: str) -> None:
        """Record a heartbeat for *worker_name* (auto-registers it) — O(1)."""
        with self._lock:
            self._wd.beat(worker_name)
            self._known.add(worker_name)

    def check_once(self) -> tuple[WorkerEvent, ...]:
        """Scan every heartbeat, firing edge callbacks — O(W).

        Returns the events fired this pass (empty if no worker transitioned).
        The HEALTHY↔HUNG hysteresis lives in the underlying primitive, so a
        worker that stays hung does not re-fire on every pass.
        """
        with self._lock:
            return self._wd.check_once()

    def state_of(self, worker_name: str) -> WorkerState:
        """Return *worker_name*'s current state (HEALTHY / HUNG).

        Raises
        ------
        KeyError
            If no worker by that name is registered.
        """
        with self._lock:
            return self._wd.state_of(worker_name)

    def workers(self) -> tuple[str, ...]:
        """Return the sorted tuple of registered worker names — O(W log W)."""
        with self._lock:
            return self._wd.workers()

    def hung_workers(self) -> tuple[str, ...]:
        """Return the sorted tuple of currently-HUNG worker names — O(W)."""
        with self._lock:
            return tuple(
                name for name in sorted(self._known) if self._wd.state_of(name) is WorkerState.HUNG
            )

    def snapshot(self) -> dict[str, Any]:
        """Return an O(1) JSON-friendly health summary.

        ``hung`` is the live hung-edge tally; ``healthy = total − hung``. Use
        :meth:`hung_workers` for the (O(W)) list of names.
        """
        with self._lock:
            total = len(self._known)
            return {
                "total": total,
                "hung": self._hung_count,
                "healthy": total - self._hung_count,
                "timeout_seconds": self._wd.timeout_seconds,
            }

    # ── Internal edge handlers (called under the primitive's check_once) ─────

    def _handle_hung(self, event: WorkerEvent) -> None:
        """Tally a HEALTHY→HUNG edge, then forward to the optional callback."""
        self._hung_count += 1
        if self._on_hung is not None:
            self._on_hung(event)

    def _handle_recovery(self, event: WorkerEvent) -> None:
        """Tally a HUNG→HEALTHY edge, then forward to the optional callback."""
        if self._hung_count > 0:
            self._hung_count -= 1
        if self._on_recovery is not None:
            self._on_recovery(event)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "SweepWatchdog",
    "SweepWatchdogCallback",
]
