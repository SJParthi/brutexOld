"""O(1) thread-safe live progress tracker for a brute-force sweep (DEC-SWEEP-REALTIME-GUARANTEE-001).

The shared, real-time state a dashboard / CLI status line reads while a sweep is
in flight. A background sweep worker pushes O(1) updates
(:meth:`SweepProgress.start` → :meth:`SweepProgress.advance` × N →
:meth:`SweepProgress.finish` / :meth:`SweepProgress.fail`); a reader takes an
immutable :class:`SweepProgressSnapshot` via :meth:`SweepProgress.snapshot` and
derives percent / ETA / combos-per-sec from it.

Mirrors the design of :mod:`brutex.dashboard.pull_progress` (one
:class:`threading.Lock`, injected ``clock``, reusable across re-runs) and
tailors it to the sweep funnel's three counters:

* **enumerated** — combos the sweep intends to evaluate (the denominator).
* **evaluated** — combos whose backtest has finished (clean or skipped).
* **survivors** — combos that cleared the Stage-2 hard gates (the leaderboard
  candidates).
* **skipped** — combos short-circuited by the dedup cache (a subset of
  *evaluated* — counted as processed, but no backtest ran).

Every mutation + read is **O(1)** — a handful of integer writes under one lock,
no scans, no per-combo allocation (CLAUDE.md §2 Rule 5), so the tracker never
becomes the bottleneck of a multi-million-combo sweep. It is **thread-safe** —
the lock guards the counters so a worker thread and a reader request can never
observe a torn update — and the ``clock`` is injected so ETA / rate are
deterministic under test.

## Honest envelope (Golden Rule #10)

This is O(1) **per operation** and O(1) **space** (a fixed set of counters). It
makes **no** claim about the sweep's total wall time — ETA is a *linear
extrapolation* from the observed combos-per-sec, not a guarantee; a sweep whose
per-combo cost varies (different masks, different gate cuts) will see the ETA
drift, exactly as any rate-based estimate does.

State machine (one sweep): ``idle → running → done`` (clean) or
``idle → running → error`` (terminal failure). :meth:`start` resets to
``running`` so the same instance is reusable across re-runs (idempotent —
CLAUDE.md §2 Rule 7).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

#: The five lifecycle states a sweep moves through.
STATE_IDLE: Final[str] = "idle"
STATE_RUNNING: Final[str] = "running"
STATE_DONE: Final[str] = "done"
STATE_ERROR: Final[str] = "error"
#: Operator cooperative cancel (DEC-JOB-COOPERATIVE-CANCEL-001): the sweep
#: stopped CLEANLY at the tuple boundary — a resumable checkpoint, never a
#: failure (``error`` stays reserved for genuine aborts).
STATE_CANCELLED: Final[str] = "cancelled"


@dataclass(frozen=True, slots=True)
class SweepProgressSnapshot:
    """An immutable point-in-time view of a sweep's progress.

    Attributes
    ----------
    state
        One of :data:`STATE_IDLE` / :data:`STATE_RUNNING` / :data:`STATE_DONE`
        / :data:`STATE_ERROR` / :data:`STATE_CANCELLED`.
    enumerated
        Total combos the sweep intends to evaluate (``0`` before
        :meth:`SweepProgress.start`). The percent / ETA denominator.
    evaluated
        Combos processed so far (finished backtests + cache skips).
    survivors
        Combos that cleared the Stage-2 hard gates so far.
    skipped
        Combos short-circuited by the dedup cache so far (⊆ *evaluated*).
    current
        Label of the combo most recently processed (free-form).
    detail
        Free-text status / error message (empty unless set).
    started_at
        The injected clock's value at :meth:`SweepProgress.start`.
    updated_at
        The injected clock's value at the last mutation.
    """

    state: str
    enumerated: int
    evaluated: int
    survivors: int
    skipped: int
    current: str
    detail: str
    started_at: float
    updated_at: float

    @property
    def running(self) -> bool:
        """``True`` iff the sweep is currently in flight."""
        return self.state == STATE_RUNNING

    @property
    def percent(self) -> float:
        """Completion percentage in ``[0, 100]`` — ``0.0`` when nothing enumerated."""
        if self.enumerated <= 0:
            return 0.0
        return min(100.0, round(self.evaluated / self.enumerated * 100.0, 1))

    @property
    def elapsed_seconds(self) -> float:
        """Seconds between :meth:`SweepProgress.start` and the last mutation."""
        elapsed = self.updated_at - self.started_at
        return elapsed if elapsed > 0.0 else 0.0

    @property
    def combos_per_sec(self) -> float:
        """Observed throughput (evaluated combos / elapsed seconds).

        ``0.0`` until at least one combo has been evaluated over a positive
        elapsed span (avoids a divide-by-zero on the very first snapshot).
        """
        elapsed = self.elapsed_seconds
        if elapsed <= 0.0 or self.evaluated <= 0:
            return 0.0
        return self.evaluated / elapsed

    @property
    def eta_seconds(self) -> float | None:
        """Linear-extrapolation ETA to completion, in seconds.

        ``None`` when it cannot be estimated yet (no measured rate) or is not
        meaningful (not running, or already at/over the enumerated total).
        Honest envelope: this is an extrapolation from the *current* rate, not
        a guarantee (see the module docstring).
        """
        rate = self.combos_per_sec
        if rate <= 0.0 or self.enumerated <= 0:
            return None
        remaining = self.enumerated - self.evaluated
        if remaining <= 0:
            return 0.0
        return remaining / rate

    @property
    def survivor_rate(self) -> float:
        """Survivors as a fraction of evaluated combos — ``0.0`` when none evaluated."""
        if self.evaluated <= 0:
            return 0.0
        return self.survivors / self.evaluated

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for an SSE / polling progress route."""
        return {
            "state": self.state,
            "enumerated": self.enumerated,
            "evaluated": self.evaluated,
            "survivors": self.survivors,
            "skipped": self.skipped,
            "current": self.current,
            "detail": self.detail,
            "percent": self.percent,
            "running": self.running,
            "elapsed_seconds": self.elapsed_seconds,
            "combos_per_sec": self.combos_per_sec,
            "eta_seconds": self.eta_seconds,
            "survivor_rate": self.survivor_rate,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }


def _monotonic() -> float:
    """Default clock — the monotonic wall clock (injected for testability)."""
    import time

    return time.monotonic()


class SweepProgress:
    """Thread-safe, O(1) live-progress state for one sweep.

    Parameters
    ----------
    clock
        Zero-arg time source stamped on every mutation (defaults to
        :func:`time.monotonic`). Injected so ETA / rate are deterministic
        under test.
    """

    def __init__(self, *, clock: Callable[[], float] = _monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._state = STATE_IDLE
        self._enumerated = 0
        self._evaluated = 0
        self._survivors = 0
        self._skipped = 0
        self._current = ""
        self._detail = ""
        self._started_at = 0.0
        self._updated_at = 0.0

    def start(self, *, enumerated: int, detail: str = "") -> None:
        """Begin a sweep of *enumerated* combos — resets all counters to ``running``.

        Reusable: calling :meth:`start` again resets the same instance for a
        fresh sweep (idempotent re-run, CLAUDE.md §2 Rule 7). A negative
        *enumerated* is clamped to ``0``. O(1).
        """
        with self._lock:
            now = self._clock()
            self._state = STATE_RUNNING
            self._enumerated = max(0, enumerated)
            self._evaluated = 0
            self._survivors = 0
            self._skipped = 0
            self._current = ""
            self._detail = detail
            self._started_at = now
            self._updated_at = now

    def advance(
        self,
        *,
        current: str = "",
        survived: bool = False,
        skipped: bool = False,
    ) -> None:
        """Record one processed combo — O(1).

        Increments ``evaluated`` (clamped to ``enumerated``), bumps
        ``survivors`` when *survived* is true, bumps ``skipped`` when the combo
        was short-circuited by the dedup cache, and stamps *current* as the
        most-recent label.
        """
        with self._lock:
            self._evaluated = min(self._enumerated, self._evaluated + 1)
            if survived:
                self._survivors += 1
            if skipped:
                self._skipped += 1
            if current:
                self._current = current
            self._updated_at = self._clock()

    def finish(self, *, detail: str = "") -> None:
        """Mark the sweep cleanly complete (``done``) — O(1)."""
        with self._lock:
            self._state = STATE_DONE
            self._detail = detail
            self._updated_at = self._clock()

    def fail(self, detail: str) -> None:
        """Mark the sweep terminally failed (``error``) with a reason — O(1)."""
        with self._lock:
            self._state = STATE_ERROR
            self._detail = detail
            self._updated_at = self._clock()

    def cancelled(self, detail: str) -> None:
        """Mark the sweep operator-CANCELLED (``cancelled``) with a reason — O(1).

        DEC-JOB-COOPERATIVE-CANCEL-001: the cooperative twin of :meth:`fail` —
        the run stopped CLEANLY at the tuple boundary with a resumable
        checkpoint, so the terminal state is honest ``cancelled``, never a red
        ``error`` (and never a fabricated ``done``).
        """
        with self._lock:
            self._state = STATE_CANCELLED
            self._detail = detail
            self._updated_at = self._clock()

    def snapshot(self) -> SweepProgressSnapshot:
        """Return an immutable, consistent O(1) view of the current state."""
        with self._lock:
            return SweepProgressSnapshot(
                state=self._state,
                enumerated=self._enumerated,
                evaluated=self._evaluated,
                survivors=self._survivors,
                skipped=self._skipped,
                current=self._current,
                detail=self._detail,
                started_at=self._started_at,
                updated_at=self._updated_at,
            )


__all__ = [
    "STATE_CANCELLED",
    "STATE_DONE",
    "STATE_ERROR",
    "STATE_IDLE",
    "STATE_RUNNING",
    "SweepProgress",
    "SweepProgressSnapshot",
]
