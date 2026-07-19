"""Runtime look-ahead barrier — Property **P-06**'s live enforcer.

This module promotes the named verification-pyramid invariant **P-06**
("for any bar index ``N``, ``evaluate(N)`` reads only bar indices ``≤ N``")
from a ``pytest.skip`` ROADMAP stub into a *live* runtime guard.

## Why this exists (the honest envelope — CLAUDE.md Golden Rule #10)

The no-look-ahead invariant is *already* enforced at runtime today by the
**feed-boundary guard** :meth:`brutex.feed.replay.ReplayFeed.ensure_no_lookahead`
(``brutex/feed/replay.py:184-208``) — a *timestamp*-based check wired into
:class:`~brutex.strategies.runner.StrategyRunner` (``runner.py:181``; the
deleted ``BacktestDriver`` wired the same guard pair until
DEC-EXECUTION-TRIO-DELETE-001).

:class:`LookAheadBarrier` is the **Phase-2 strengthening** of that guard, NOT
a new performance path and NOT a replacement. It is an *index*-based instrumented
accessor: it carries the engine's notion of "the current bar index ``N``",
records every bar index a consumer touches, and raises
:class:`~brutex.feed.errors.LookAheadError` the moment any access reaches an
index ``> N``.

**Honest envelope — what each site actually catches (Golden Rule #10):** the
timestamp guard (:meth:`~brutex.feed.replay.ReplayFeed.ensure_no_lookahead`,
wired at ``runner.py:207``) is the guard that catches a future *strategy
signal* — it is the real strategy-look-ahead defence. The barrier as wired into
the runner event loop asserts the **advance/access cursor invariant**:
the site ``advance_to(step)`` then ``guard(step, …)`` at the SAME ``step``, so
the barrier fires only if that index-vs-cursor relationship is ever violated (a
feed/barrier desync or a wiring regression) — it does NOT interpose on the
arbitrary bar reads a strategy performs off its own state, so a future-peeking
strategy is caught by ``ensure_no_lookahead``, not by the runner-loop barrier.
The barrier's index-``> N`` teeth are genuinely exercised by an ``access`` at a
future index — done by the property tests and by the manual
``brutex verify-lookahead`` recheck. So the two guards are complementary, not
literally redundant coverage of the same failure at the same site.

**The brute-force SWEEP path (DEC-SWEEP-LOOKAHEAD-GUARD-001):** the two guards
above wire only the library-strategy path (the runner); the sweep spine
(``run_persisted_sweep`` → Stage-1A → trade sim) historically had NO runtime
guard. It now has three layers: (1) **structural causality** (audit-verified:
Stage-1A updates prior-bar history only AFTER evaluation; the trade sim enters
at the NEXT bar's open); (2) **permanent suffix-independence property gates**
(``tests/property/test_sweep_suffix_independence.py`` — bar-bits / resample /
full-spine trades over ``bars[0..N]`` are bit-identical whether ``bars[N+1..]``
are absent, mutated, or extreme; these run in CI forever and are the layer that
catches a future-information flow through DERIVED state); and (3) an **opt-in
instrumented barrier** (:mod:`brutex.engine.sweep_lookahead`, env
``BRUTEX_SWEEP_LOOKAHEAD_GUARD=1``, default OFF) that wraps the sweep's bar
window in an index-guarded sequence REUSING this module's
:class:`LookAheadBarrier` — its honest envelope: it catches an indexed / sliced
/ reversed bar read AHEAD of the sweep's forward consumption front (strongest
during the first sequential pass), NOT a leak through already-derived per-bar
state, and when OFF the sweep receives its original bars object unchanged
(byte-identical, cost-unchanged hot path).

**O(1) discipline (CLAUDE.md Rule #5):** every :meth:`LookAheadBarrier.access`
/ :meth:`LookAheadBarrier.advance_to` call is **O(1) per operation** — a couple
of integer comparisons in the Rust core plus one constant-cost FFI hop (and, in
record mode, one Vec push). This is emphatically *not* an "O(1)-total" claim: a
full sweep touches ``Σ`` bars and is inherently ``O(Σ)``. With ``record=False``
(the default for the always-on sweep guard) the barrier holds **O(1) memory**;
``record=True`` retains the touched-index trail and is meant only for the
*bounded* manual-recheck slice and the property tests.

**DEC-FEED-RUST-MATERIALIZE-002 (the T17 zero-Python endgame):** the barrier's
STATE MACHINE (cursor + trail + every verdict) runs EXCLUSIVELY in the
``brutex_feed`` Rust wheel (``BarrierCore`` — verdict codes out, never
exceptions); this class survives as the thin exception-MATERIALIZING wrapper,
so the exception TYPE (:class:`~brutex.feed.errors.LookAheadError` /
``ValueError``) and every pinned message byte stay Python-owned and unchanged.
The wheel is REQUIRED: constructing a barrier on a wheel-less box raises a
typed :class:`~brutex.feed.errors.FeedError` naming the rebuild remediation.

## No global mutable state

The barrier is a plain dependency-injected instance. Each runner owns
its own barrier (or one is injected for the manual recheck); nothing here
touches module-level mutable state. :meth:`LookAheadBarrier.reset` returns an
instance to its just-constructed form so a runner can reuse it across
deterministic replays.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from brutex.feed.errors import LookAheadError
from brutex.feed_rust import require_feed_kernel

#: Verdict vocabulary shared with the Rust ``BarrierCore``
#: (DEC-FEED-RUST-MATERIALIZE-002) — mirror-pinned against the crate's
#: constants by the wheel FFI tests.
_ADVANCE_OK = 0
_ADVANCE_NEGATIVE = 1
_ADVANCE_BACKWARDS = 2
_ACCESS_OK = 0
_ACCESS_NO_CURSOR = 1
_ACCESS_NEGATIVE = 2
_ACCESS_LOOKAHEAD = 3

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

    __slots__ = ("_core",)

    def __init__(self, *, record: bool = True) -> None:
        # DEC-FEED-RUST-MATERIALIZE-002: the state machine lives in the
        # REQUIRED brutex_feed wheel (a missing wheel is a loud typed
        # FeedError at construction — never a silent Python fallback; the
        # Python core was deleted at the flip).
        self._core: _BarrierCoreLike = require_feed_kernel().BarrierCore(  # type: ignore[attr-defined]
            record
        )

    # ── Introspection ─────────────────────────────────────────────

    @property
    def current_index(self) -> int:
        """Current bar index ``N``. ``-1`` before the first :meth:`advance_to`."""
        return self._core.current_index

    @property
    def records(self) -> bool:
        """Whether this barrier retains the touched-index trail."""
        return self._core.records

    @property
    def touched_indices(self) -> tuple[int, ...]:
        """Every *permitted* index passed to :meth:`access`, in call order.

        A rejected look-ahead index (one ``> N`` that raised) is NOT
        recorded — the core appends only after the bounds check passes,
        so a FAIL never poisons the trail. Empty when ``record=False``
        (the barrier still validated each access, it just did not retain
        the trail).
        """
        return tuple(self._core.touched())

    @property
    def max_touched(self) -> int | None:
        """Largest *permitted* index touched, or ``None`` if nothing was recorded."""
        return self._core.max_touched()

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
        verdict = self._core.advance_to(index)
        if verdict == _ADVANCE_OK:
            return
        if verdict == _ADVANCE_NEGATIVE:
            raise ValueError(f"advance_to: bar index must be >= 0, got {index}")
        # _ADVANCE_BACKWARDS — the last code in the pinned verdict
        # vocabulary. An out-of-vocabulary code (a poisoned wheel) lands
        # here too and refuses loudly rather than silently succeeding
        # (the refuse-poison law — a broken core may only refuse, never
        # quietly pass a cursor move).
        raise ValueError(
            f"advance_to: cursor cannot move backwards "
            f"({index} < current {self._core.current_index})"
        )

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
        verdict = self._core.access(index)
        if verdict == _ACCESS_OK:
            return index
        if verdict == _ACCESS_NO_CURSOR:
            raise LookAheadError(
                f"no bar reached yet (cursor={self._core.current_index}); "
                f"any read at index {index} is a look-ahead (P-06)"
            )
        if verdict == _ACCESS_NEGATIVE:
            raise ValueError(f"access: bar index must be >= 0, got {index}")
        # _ACCESS_LOOKAHEAD — the last code in the pinned verdict
        # vocabulary. An out-of-vocabulary code (a poisoned wheel) lands
        # here too and refuses loudly rather than silently permitting the
        # read (the refuse-poison law — never weaker than P-06).
        raise LookAheadError(
            f"bar index {index} > current {self._core.current_index}: "
            f"look-ahead read forbidden (P-06 / FR-INT-004)"
        )

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
        self._core.reset()


class _BarrierCoreLike(Protocol):
    """Structural protocol of the verdict-coded Rust ``BarrierCore``.

    The wheel is REQUIRED — no Python core exists
    (DEC-FEED-RUST-MATERIALIZE-002); the Protocol exists for typing only.
    """

    @property
    def current_index(self) -> int:
        """Current cursor value ``N`` (``-1`` before the first advance)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    @property
    def records(self) -> bool:
        """Whether the core retains the touched-index trail."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def touched(self) -> list[int]:
        """Every permitted index passed to :meth:`access`, in call order."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def max_touched(self) -> int | None:
        """Largest permitted index touched, or ``None`` if none recorded."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def advance_to(self, index: int) -> int:
        """Advance the cursor; return an ``_ADVANCE_*`` verdict code."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def access(self, index: int) -> int:
        """Validate a read of *index*; return an ``_ACCESS_*`` verdict code."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed

    def reset(self) -> None:
        """Restore the just-constructed form (cursor ``-1``, empty trail)."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


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
