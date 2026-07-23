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

# DEC-RUST-SWEEP-OPS-001 (slice 1b): the default-OFF Rust routing facade. The
# five snapshot derived-metric properties gate on `sweep_ops_rust_enabled()`
# FIRST so the flag-OFF default path is byte-identical; the verbatim Python
# property bodies live ONCE below as the `_*_python` module-level laws (the
# live oracles) consumed by BOTH the OFF branches and the oracle closure.
# The lock/state machine stays Python (threading is not a law).
from brutex.engine.sweep_ops_rust import routed_progress_derived, sweep_ops_rust_enabled

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
        if sweep_ops_rust_enabled():
            return _routed_derived(self)[0]
        return _percent_python(self.enumerated, self.evaluated)

    @property
    def elapsed_seconds(self) -> float:
        """Seconds between :meth:`SweepProgress.start` and the last mutation."""
        if sweep_ops_rust_enabled():
            return _routed_derived(self)[1]
        return _elapsed_python(self.started_at, self.updated_at)

    @property
    def combos_per_sec(self) -> float:
        """Observed throughput (evaluated combos / elapsed seconds).

        ``0.0`` until at least one combo has been evaluated over a positive
        elapsed span (avoids a divide-by-zero on the very first snapshot).
        """
        if sweep_ops_rust_enabled():
            return _routed_derived(self)[2]
        return _rate_python(self.evaluated, self.started_at, self.updated_at)

    @property
    def eta_seconds(self) -> float | None:
        """Linear-extrapolation ETA to completion, in seconds.

        ``None`` when it cannot be estimated yet (no measured rate) or is not
        meaningful (not running, or already at/over the enumerated total).
        Honest envelope: this is an extrapolation from the *current* rate, not
        a guarantee (see the module docstring).
        """
        if sweep_ops_rust_enabled():
            return _routed_derived(self)[3]
        return _eta_python(self.enumerated, self.evaluated, self.started_at, self.updated_at)

    @property
    def survivor_rate(self) -> float:
        """Survivors as a fraction of evaluated combos — ``0.0`` when none evaluated."""
        if sweep_ops_rust_enabled():
            return _routed_derived(self)[4]
        return _survivor_rate_python(self.survivors, self.evaluated)

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


# ── DEC-RUST-SWEEP-OPS-001 (slice 1b): the verbatim derived-metric laws ──
# Each `_*_python` body is the pre-conversion property body moved verbatim
# (the live parity oracle, retained until the final slice's default flip);
# the helpers call EACH OTHER — never the routed properties — so the oracle
# closure can never re-enter the routed path.


def _percent_python(enumerated: int, evaluated: int) -> float:
    """The verbatim ``percent`` law — the live parity oracle."""
    if enumerated <= 0:
        return 0.0
    return min(100.0, round(evaluated / enumerated * 100.0, 1))


def _elapsed_python(started_at: float, updated_at: float) -> float:
    """The verbatim ``elapsed_seconds`` law — the live parity oracle."""
    elapsed = updated_at - started_at
    return elapsed if elapsed > 0.0 else 0.0


def _rate_python(evaluated: int, started_at: float, updated_at: float) -> float:
    """The verbatim ``combos_per_sec`` law — the live parity oracle."""
    elapsed = _elapsed_python(started_at, updated_at)
    if elapsed <= 0.0 or evaluated <= 0:
        return 0.0
    return evaluated / elapsed


def _eta_python(
    enumerated: int, evaluated: int, started_at: float, updated_at: float
) -> float | None:
    """The verbatim ``eta_seconds`` law — the live parity oracle."""
    rate = _rate_python(evaluated, started_at, updated_at)
    if rate <= 0.0 or enumerated <= 0:
        return None
    remaining = enumerated - evaluated
    if remaining <= 0:
        return 0.0
    return remaining / rate


def _survivor_rate_python(survivors: int, evaluated: int) -> float:
    """The verbatim ``survivor_rate`` law — the live parity oracle."""
    if evaluated <= 0:
        return 0.0
    return survivors / evaluated


def _derived_python(
    snapshot: SweepProgressSnapshot,
) -> tuple[float, float, float, float | None, float]:
    """The full five-metric oracle tuple the routed law burn-in compares."""
    return (
        _percent_python(snapshot.enumerated, snapshot.evaluated),
        _elapsed_python(snapshot.started_at, snapshot.updated_at),
        _rate_python(snapshot.evaluated, snapshot.started_at, snapshot.updated_at),
        _eta_python(
            snapshot.enumerated, snapshot.evaluated, snapshot.started_at, snapshot.updated_at
        ),
        _survivor_rate_python(snapshot.survivors, snapshot.evaluated),
    )


def _routed_derived(
    snapshot: SweepProgressSnapshot,
) -> tuple[float, float, float, float | None, float]:
    """Route the derived-metrics cluster through the sweep-ops facade (flag-ON)."""
    return routed_progress_derived(
        snapshot.enumerated,
        snapshot.evaluated,
        snapshot.survivors,
        snapshot.started_at,
        snapshot.updated_at,
        python_law=lambda: _derived_python(snapshot),
    )


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
