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

import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

#: The four lifecycle states a sweep moves through.
STATE_IDLE: Final[str] = "idle"
STATE_RUNNING: Final[str] = "running"
STATE_DONE: Final[str] = "done"
STATE_ERROR: Final[str] = "error"

#: Default freshness window for the cross-process liveness check
#: (:func:`progress_running_fresh`): a ``running`` snapshot whose wall-clock
#: heartbeat is younger than this is treated as a LIVE sweep (e.g. the
#: ``sweep-heal`` C6 refusal). 600 s tolerates a slow poll cadence yet bounds
#: the post-crash false-live window.
HEAL_FRESH_SECONDS: Final[float] = 600.0


@dataclass(frozen=True, slots=True)
class SweepProgressSnapshot:
    """An immutable point-in-time view of a sweep's progress.

    Attributes
    ----------
    state
        One of :data:`STATE_IDLE` / :data:`STATE_RUNNING` / :data:`STATE_DONE`
        / :data:`STATE_ERROR`.
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
        The injected MONOTONIC clock's value at :meth:`SweepProgress.start`
        (for in-process elapsed / ETA math — NOT comparable across processes).
    updated_at
        The injected MONOTONIC clock's value at the last mutation (in-process
        ETA only).
    updated_wall
        The injected WALL clock's value (``time.time()``) at the last mutation.
        Unlike :attr:`updated_at` this IS comparable across processes / hosts,
        so a cross-process reader (``sweep-heal``'s C6 liveness gate) can judge
        whether a durable ``sweep-progress.json`` is a live sweep or a crashed
        leftover — the monotonic ``updated_at`` is meaningless across the
        process boundary (its epoch differs per process). See the
        :mod:`brutex.data.rate_bridge` "wall-clock across processes" precedent.
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
    updated_wall: float

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
            "updated_wall": self.updated_wall,
        }


def _monotonic() -> float:
    """Default MONOTONIC clock — for in-process elapsed / ETA (injected for testability)."""
    import time

    return time.monotonic()


def _wall_clock() -> float:
    """Default WALL clock (``time.time()``) — the ONLY clock comparable across processes."""
    import time

    return time.time()


class SweepProgress:
    """Thread-safe, O(1) live-progress state for one sweep.

    Parameters
    ----------
    clock
        Zero-arg MONOTONIC time source stamped on every mutation (defaults to
        :func:`time.monotonic`). Injected so ETA / rate are deterministic
        under test. Drives ``started_at`` / ``updated_at`` (in-process only).
    wall_clock
        Zero-arg WALL time source (defaults to :func:`time.time`) stamped on
        every mutation as ``updated_wall`` — the cross-process liveness stamp
        (:func:`progress_running_fresh`). Injected so tests can pin a realistic
        epoch independently of the monotonic ``clock``.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = _monotonic,
        wall_clock: Callable[[], float] = _wall_clock,
    ) -> None:
        self._clock = clock
        self._wall_clock = wall_clock
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
        self._updated_wall = 0.0

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
            self._updated_wall = self._wall_clock()

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
            self._updated_wall = self._wall_clock()

    def finish(self, *, detail: str = "") -> None:
        """Mark the sweep cleanly complete (``done``) — O(1)."""
        with self._lock:
            self._state = STATE_DONE
            self._detail = detail
            self._updated_at = self._clock()
            self._updated_wall = self._wall_clock()

    def fail(self, detail: str) -> None:
        """Mark the sweep terminally failed (``error``) with a reason — O(1)."""
        with self._lock:
            self._state = STATE_ERROR
            self._detail = detail
            self._updated_at = self._clock()
            self._updated_wall = self._wall_clock()

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
                updated_wall=self._updated_wall,
            )


def progress_running_fresh(
    snapshot: Mapping[str, Any],
    *,
    now_wall: float,
    fresh_seconds: float = HEAL_FRESH_SECONDS,
) -> bool:
    """Is *snapshot* a LIVE (running + freshly-heartbeating) sweep? — pure, O(1).

    The cross-process liveness verdict a durable ``sweep-progress.json`` reader
    (``sweep-heal``'s C6 refusal) needs: ``True`` iff the snapshot's ``running``
    flag is set AND its WALL-clock heartbeat ``updated_wall`` is younger than
    *fresh_seconds* against *now_wall* (also wall clock). Freshness MUST use
    ``updated_wall``, never the monotonic ``updated_at`` — the two epochs are
    incomparable across process boundaries (``time.time()`` ≈ 1.7e9 vs a
    per-process ``time.monotonic()``), the exact defect this replaces (a
    ``time.time() - updated_at`` compare was ALWAYS stale ⇒ never detected a
    live sweep).

    Safety-first for a guard that PREVENTS corruption (Golden Rule #1): when the
    sweep is ``running`` but no VALID wall-clock stamp is present — an
    old-format snapshot with no ``updated_wall`` key, or a malformed / non-finite
    value — this returns ``True`` (treat as live → refuse the heal). A false
    REFUSAL is safe; a false PERMIT is the exact live-sweep corruption this gate
    exists to prevent.

    Parameters
    ----------
    snapshot
        The progress dict (as read from ``sweep-progress.json``).
    now_wall
        The current wall-clock instant (``time.time()``), injected for tests.
    fresh_seconds
        Freshness window (defaults to :data:`HEAL_FRESH_SECONDS`).
    """
    if not bool(snapshot.get("running")):
        return False
    raw_wall = snapshot.get("updated_wall")
    if raw_wall is None:
        # Missing key (old-format file) or an explicit null: a running sweep with
        # no wall stamp is treated as LIVE (safety-first refuse).
        return True
    try:
        updated_wall = float(raw_wall)
    except (TypeError, ValueError):
        # A non-numeric value (hand-edited / alien file) — same safety-first rule.
        return True
    if not math.isfinite(updated_wall):
        # nan / inf stamp — same safety-first rule.
        return True
    return (now_wall - updated_wall) < fresh_seconds


__all__ = [
    "HEAL_FRESH_SECONDS",
    "STATE_DONE",
    "STATE_ERROR",
    "STATE_IDLE",
    "STATE_RUNNING",
    "SweepProgress",
    "SweepProgressSnapshot",
    "progress_running_fresh",
]
