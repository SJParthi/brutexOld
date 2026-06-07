"""Runtime look-ahead barrier — Property **P-06**'s live enforcer.

This module promotes the named verification-pyramid invariant **P-06**
("for any bar index ``N``, ``evaluate(N)`` reads only bar indices ``≤ N``")
from a ``pytest.skip`` ROADMAP stub into a *live* runtime guard.

## Why this exists (the honest envelope — CLAUDE.md Golden Rule #10)

The no-look-ahead invariant is *already* enforced at runtime today by the
**feed-boundary guard** :meth:`brutex.feed.replay.ReplayFeed.ensure_no_lookahead`
(``brutex/feed/replay.py:184-208``) — a *timestamp*-based check wired into
:class:`~brutex.strategies.runner.StrategyRunner` (``runner.py:181``) and
:class:`~brutex.execution.driver.BacktestDriver` (``driver.py:184``).

:class:`LookAheadBarrier` is the **Phase-2 strengthening** of that guard, NOT
a new performance path and NOT a replacement. It is an *index*-based instrumented
accessor: it carries the engine's notion of "the current bar index ``N``",
records every bar index a consumer touches, and raises
:class:`~brutex.feed.errors.LookAheadError` the moment any access reaches an
index ``> N``. The two guards are belt-and-suspenders — the timestamp guard
catches a future *signal*; the index barrier catches a future *bar read*.

**O(1) discipline (CLAUDE.md Rule #5):** every :meth:`LookAheadBarrier.access`
/ :meth:`LookAheadBarrier.advance_to` call is **O(1) per operation** — a couple
of integer comparisons and (optionally) one list append. This is emphatically
*not* an "O(1)-total" claim: a full sweep touches ``Σ`` bars and is inherently
``O(Σ)``. With ``record=False`` (the default for the always-on sweep guard) the
barrier holds **O(1) memory**; ``record=True`` retains the touched-index trail
and is meant only for the *bounded* manual-recheck slice and the property tests.

## No global mutable state

The barrier is a plain dependency-injected instance. Each runner/driver owns
its own barrier (or one is injected for the manual recheck); nothing here
touches module-level mutable state. :meth:`LookAheadBarrier.reset` returns an
instance to its just-constructed form so a runner can reuse it across
deterministic replays.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from brutex.feed.errors import LookAheadError

#: Pass-through value type for :meth:`LookAheadBarrier.guard` — the barrier
#: validates the *index* and returns the caller's *value* unchanged, so the
#: bar's concrete type is preserved through the guard.
_T = TypeVar("_T")

__all__ = [
    "LookAheadBarrier",
    "LookAheadReport",
    "verify_with_barrier",
]


class LookAheadBarrier:
    """Instrumented bar accessor enforcing **P-06** at runtime.

    The barrier tracks a single forward-only cursor — the **current bar
    index ``N``** — and validates every bar access against it. An access
    to an index ``> N`` is a look-ahead and raises
    :class:`~brutex.feed.errors.LookAheadError`.

    Parameters
    ----------
    record
        When ``True`` (the default) every touched index is appended to an
        internal trail exposed via :attr:`touched_indices` — used by the
        manual ``brutex verify-lookahead`` recheck and the property tests
        to *prove* which indices a strategy reached. When ``False`` the
        barrier still validates every access but retains **O(1) memory**,
        which is the mode the always-on sweep guard uses.

    Notes
    -----
    No global mutable state: each instance is self-contained and
    dependency-injected. :meth:`reset` restores the just-constructed form
    so a runner can reuse one barrier across back-to-back deterministic
    replays without leaking the prior run's cursor or trail.
    """

    __slots__ = ("_current_index", "_record", "_touched")

    def __init__(self, *, record: bool = True) -> None:
        self._record = record
        self._current_index = -1
        self._touched: list[int] = []

    # ── Introspection ─────────────────────────────────────────────

    @property
    def current_index(self) -> int:
        """Current bar index ``N``. ``-1`` before the first :meth:`advance_to`."""
        return self._current_index

    @property
    def records(self) -> bool:
        """Whether this barrier retains the touched-index trail."""
        return self._record

    @property
    def touched_indices(self) -> tuple[int, ...]:
        """Every index passed to :meth:`access`, in call order.

        Empty when ``record=False`` (the barrier still validated each
        access, it just did not retain the trail).
        """
        return tuple(self._touched)

    @property
    def max_touched(self) -> int | None:
        """Largest index ever touched, or ``None`` if nothing was recorded."""
        return max(self._touched) if self._touched else None

    # ── Cursor + access ───────────────────────────────────────────

    def advance_to(self, index: int) -> None:
        """Advance the current-bar cursor to *index* (forward-only).

        Parameters
        ----------
        index
            The bar index now being evaluated. Must be ``>= 0`` and must
            not move the cursor backwards (a replay feed only ever moves
            forward; a backwards move signals a wiring bug, not a
            look-ahead).

        Raises
        ------
        ValueError
            If *index* is negative or strictly less than the current
            cursor.
        """
        if index < 0:
            raise ValueError(f"advance_to: bar index must be >= 0, got {index}")
        if index < self._current_index:
            raise ValueError(
                f"advance_to: cursor cannot move backwards "
                f"({index} < current {self._current_index})"
            )
        self._current_index = index

    def access(self, index: int) -> int:
        """Validate (and record) a read of bar *index*; return it unchanged.

        This is the instrumented "bar access" of **P-06**: route every bar
        read through it and the barrier guarantees the read never reaches
        the future.

        Parameters
        ----------
        index
            The bar index a consumer intends to read.

        Returns
        -------
        int
            *index* unchanged, so callers can write ``bars[barrier.access(i)]``.

        Raises
        ------
        LookAheadError
            If no bar has been reached yet (``current_index < 0``) or if
            *index* is strictly greater than the current cursor.
        ValueError
            If *index* is negative.
        """
        if self._current_index < 0:
            raise LookAheadError(
                f"no bar reached yet (cursor={self._current_index}); "
                f"any read at index {index} is a look-ahead (P-06)"
            )
        if index < 0:
            raise ValueError(f"access: bar index must be >= 0, got {index}")
        if self._record:
            self._touched.append(index)
        if index > self._current_index:
            raise LookAheadError(
                f"bar index {index} > current {self._current_index}: "
                f"look-ahead read forbidden (P-06 / FR-INT-004)"
            )
        return index

    def guard(self, index: int, value: _T) -> _T:
        """Convenience: :meth:`access` the *index*, then return *value*.

        Lets a runner write ``bar = barrier.guard(step, event.bar)`` so the
        bar it hands to the strategy is proven to be at index ``≤ N``.
        """
        self.access(index)
        return value

    def reset(self) -> None:
        """Restore the just-constructed form (cursor ``-1``, empty trail).

        Called by the runner/driver before every replay so the barrier
        carries no state across deterministic re-runs.
        """
        self._current_index = -1
        self._touched.clear()


@dataclass(frozen=True, slots=True)
class LookAheadReport:
    """Outcome of a manual look-ahead recheck over a bounded slice.

    Attributes
    ----------
    passed
        ``True`` iff the run completed without a
        :class:`~brutex.feed.errors.LookAheadError`.
    indices_checked
        Number of bar accesses the barrier validated (length of the
        recorded trail; ``0`` when the barrier was not in ``record`` mode).
    max_index_touched
        Largest bar index reached, or ``None`` if nothing was recorded.
    final_cursor
        The barrier's current-bar cursor when the run ended.
    violation
        ``None`` on success; otherwise the ``str`` of the
        :class:`~brutex.feed.errors.LookAheadError` that fired.
    """

    passed: bool
    indices_checked: int
    max_index_touched: int | None
    final_cursor: int
    violation: str | None


def verify_with_barrier(
    run: Callable[[], object],
    barrier: LookAheadBarrier,
) -> LookAheadReport:
    """Drive *run* and report whether it ever looked ahead — the manual core.

    This is the pure, fully-tested core behind the ``brutex verify-lookahead``
    CLI recheck. *run* is any zero-argument callable that drives a bounded
    slice through *barrier* (typically a wired
    :meth:`~brutex.strategies.runner.StrategyRunner.run`). A look-ahead
    surfaces as a :class:`~brutex.feed.errors.LookAheadError`, which this
    function catches and folds into a :class:`LookAheadReport` rather than
    propagating — so the operator gets a pass/fail verdict, not a traceback.

    The verification run is a *material action*: it emits one structured
    ``structlog`` event (CLAUDE.md Golden Rule #8 / Rule #5 observability)
    recording the verdict and the barrier statistics.

    Parameters
    ----------
    run
        Zero-arg callable that exercises *barrier*. Should be configured to
        run over a **bounded** slice — the recorded trail is held in memory.
    barrier
        The :class:`LookAheadBarrier` *run* drives. Use ``record=True`` so
        :attr:`LookAheadReport.indices_checked` and
        :attr:`LookAheadReport.max_index_touched` are populated.

    Returns
    -------
    LookAheadReport
        The pass/fail verdict plus barrier statistics.
    """
    # structlog is a core runtime dep, lazy-imported here so the rest of
    # this module — and the whole pure ``brutex.feed`` package — stays
    # import-light (the hot accessor never touches it).
    import structlog

    violation: str | None = None
    try:
        run()
    except LookAheadError as exc:
        violation = str(exc)

    report = LookAheadReport(
        passed=violation is None,
        indices_checked=len(barrier.touched_indices),
        max_index_touched=barrier.max_touched,
        final_cursor=barrier.current_index,
        violation=violation,
    )
    structlog.get_logger("brutex.feed.lookahead").info(
        "lookahead.verify",
        passed=report.passed,
        indices_checked=report.indices_checked,
        max_index_touched=report.max_index_touched,
        final_cursor=report.final_cursor,
        violation=report.violation,
    )
    return report
