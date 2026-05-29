"""Hard rejection gates — applied per-result BEFORE composite scoring.

Implements the six hard rejection gates defined in
``docs/TRACK1_BACKTEST_SPEC.md`` §"Hard rejection gates (applied BEFORE
Stage 4 composite scoring)" (spec lines 589-600). A result is REJECTED
(excluded from the top-10 leaderboard) if ANY gate trips:

============================  =======================  ===================
Gate                          Threshold                Spec line
============================  =======================  ===================
``total_trades``              ``< 30``                 595
``profit_factor``             ``< 1.2``                596
``max_drawdown_pct``          ``> 30``                 597
``win_rate`` AND              ``< 0.25`` AND ``< 2.0`` 598
``win_loss_ratio``
``max_consecutive_losses``    ``> 10``                 599
``lockbox_sharpe/sharpe``     ``< 0.5``                600
============================  =======================  ===================

The thresholds above are quoted verbatim from the spec; no threshold is
invented (CLAUDE.md Golden Rule #1).

**Stage-4 lockbox deferral (DEC-RANKING-001 honest envelope, mirrors
DEC-METRICS-001 #305):** ``lockbox_sharpe`` is a Stage-4 out-of-sample
metric the spec marks "Stage 4 only, NULL before then" (spec line 586).
The overfit gate (line 600) is therefore applied ONLY when a
``lockbox_sharpe`` is supplied; pre-Stage-4 (``lockbox_sharpe is None``)
the overfit gate is SKIPPED — never silently failed. This is disclosed
in ``docs/MIGRATION.md`` entry #307 per Golden Rule #10.

**None-metric convention (disclosed):** the gate inputs that can be
``None`` on :class:`~brutex.metrics.ResultMetrics` are ``profit_factor``
and ``win_loss_ratio`` (both are ``None`` iff the result has NO losing
trades — see ``_safe_ratio`` in :mod:`brutex.metrics.compute`). A result
with no losers has, by definition, an unbounded edge, so:

* ``profit_factor is None`` → the ``< 1.2`` gate PASSES (no loss leg to
  fall short of the threshold).
* ``win_loss_ratio is None`` → the win-rate/win-loss compound gate's
  second clause is FALSE (``None < 2.0`` is undefined; a no-loss result
  cannot have a sub-2.0 win/loss ratio), so the compound AND-gate cannot
  trip on it.

The spec is silent on the ``None`` case; the conservative,
spec-consistent reading (a no-loss result is the strongest possible
edge, so it must not be rejected by a profit/loss-ratio gate) is chosen
and disclosed here + in MIGRATION #307.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final

from brutex.metrics import ResultMetrics

__all__ = [
    "MAX_CONSECUTIVE_LOSSES_LIMIT",
    "MAX_DRAWDOWN_PCT_LIMIT",
    "MIN_PROFIT_FACTOR",
    "MIN_TOTAL_TRADES",
    "OVERFIT_RATIO_MIN",
    "WIN_LOSS_RATIO_FLOOR",
    "WIN_RATE_FLOOR",
    "RejectionReason",
    "RejectionResult",
    "passes_gates",
]

#: Minimum trade count; ``total_trades < 30`` rejects (spec line 595).
MIN_TOTAL_TRADES: Final[int] = 30
#: Minimum profit factor; ``profit_factor < 1.2`` rejects (spec line 596).
MIN_PROFIT_FACTOR: Final[float] = 1.2
#: Maximum drawdown %; ``max_drawdown_pct > 30`` rejects (spec line 597).
MAX_DRAWDOWN_PCT_LIMIT: Final[float] = 30.0
#: Win-rate floor for the compound gate (spec line 598).
WIN_RATE_FLOOR: Final[float] = 0.25
#: Win/loss-ratio floor for the compound gate (spec line 598).
WIN_LOSS_RATIO_FLOOR: Final[float] = 2.0
#: Max consecutive losses; ``> 10`` rejects (spec line 599).
MAX_CONSECUTIVE_LOSSES_LIMIT: Final[int] = 10
#: Overfit floor; ``lockbox_sharpe / sharpe < 0.5`` rejects (spec line 600).
OVERFIT_RATIO_MIN: Final[float] = 0.5


class RejectionReason(enum.Flag):
    """Which hard rejection gate(s) a result tripped (spec lines 589-600).

    A :class:`enum.Flag` so multiple simultaneous failures combine with
    ``|`` and can be reported together; :data:`RejectionReason.NONE` is
    the empty (passing) value.
    """

    #: No gate tripped — the result passes all gates.
    NONE = 0
    #: ``total_trades < 30`` (spec line 595).
    INSUFFICIENT_TRADES = enum.auto()
    #: ``profit_factor < 1.2`` (spec line 596).
    LOW_PROFIT_FACTOR = enum.auto()
    #: ``max_drawdown_pct > 30`` (spec line 597).
    EXCESSIVE_DRAWDOWN = enum.auto()
    #: ``win_rate < 0.25`` AND ``win_loss_ratio < 2.0`` (spec line 598).
    POOR_WIN_PROFILE = enum.auto()
    #: ``max_consecutive_losses > 10`` (spec line 599).
    LOSS_STREAK = enum.auto()
    #: ``lockbox_sharpe / sharpe < 0.5`` (spec line 600).
    OVERFIT = enum.auto()


@dataclass(frozen=True, slots=True)
class RejectionResult:
    """Outcome of applying the hard rejection gates to one result.

    Attributes
    ----------
    reason
        The combined :class:`RejectionReason` flags that tripped;
        :data:`RejectionReason.NONE` (falsy) iff the result passes.
    overfit_gate_evaluated
        ``True`` iff a ``lockbox_sharpe`` was supplied AND a non-``None``
        training ``sharpe_ratio`` existed, so the overfit gate (spec line
        600) was actually evaluated. ``False`` means the Stage-4 overfit
        gate was SKIPPED (pre-Stage-4 deferral, DEC-RANKING-001) — not
        that it passed. Lets callers distinguish "passed the overfit
        gate" from "overfit gate not yet applicable".
    """

    reason: RejectionReason
    overfit_gate_evaluated: bool

    @property
    def passed(self) -> bool:
        """Return ``True`` iff no gate tripped (the result survives)."""
        return self.reason is RejectionReason.NONE


def passes_gates(
    metrics: ResultMetrics,
    *,
    lockbox_sharpe: float | None = None,
) -> RejectionResult:
    """Apply the six hard rejection gates to a single result.

    Spec lines 589-600. Every gate is evaluated (they do not short
    circuit) so the returned :class:`RejectionReason` reports *all*
    tripped gates, not just the first.

    Parameters
    ----------
    metrics
        The per-result :class:`~brutex.metrics.ResultMetrics` row.
    lockbox_sharpe
        The Stage-4 out-of-sample (lockbox) Sharpe, or ``None`` when not
        yet computed (pre-Stage-4). When ``None`` the overfit gate (spec
        line 600) is SKIPPED — see the module docstring + MIGRATION #307.

    Returns
    -------
    RejectionResult
        The combined gate outcome. ``result.passed`` is ``True`` iff the
        result survives all applicable gates.
    """
    reason = RejectionReason.NONE

    # Gate 1 — insufficient sample (spec line 595).
    if metrics.total_trades < MIN_TOTAL_TRADES:
        reason |= RejectionReason.INSUFFICIENT_TRADES

    # Gate 2 — no real edge (spec line 596). A None profit_factor means
    # no losing trades (unbounded edge) → PASS (module docstring).
    if metrics.profit_factor is not None and metrics.profit_factor < MIN_PROFIT_FACTOR:
        reason |= RejectionReason.LOW_PROFIT_FACTOR

    # Gate 3 — uninvestable capital risk (spec line 597).
    if metrics.max_drawdown_pct > MAX_DRAWDOWN_PCT_LIMIT:
        reason |= RejectionReason.EXCESSIVE_DRAWDOWN

    # Gate 4 — psychologically uninvestable (spec line 598): BOTH the
    # low win rate AND the low win/loss ratio must hold. A None
    # win_loss_ratio (no losers) makes the second clause False, so the
    # compound gate cannot trip (module docstring).
    low_win_rate = metrics.win_rate < WIN_RATE_FLOOR
    low_win_loss = (
        metrics.win_loss_ratio is not None and metrics.win_loss_ratio < WIN_LOSS_RATIO_FLOOR
    )
    if low_win_rate and low_win_loss:
        reason |= RejectionReason.POOR_WIN_PROFILE

    # Gate 5 — account-blower risk (spec line 599).
    if metrics.max_consecutive_losses > MAX_CONSECUTIVE_LOSSES_LIMIT:
        reason |= RejectionReason.LOSS_STREAK

    # Gate 6 — overfit / out-of-sample collapse (spec line 600). Stage-4
    # only: evaluated solely when a lockbox_sharpe is supplied AND the
    # training sharpe is defined and non-zero (a None/zero training
    # Sharpe has no ratio). Skipped otherwise (pre-Stage-4 deferral).
    sharpe = metrics.sharpe_ratio
    overfit_evaluated = False
    if lockbox_sharpe is not None and sharpe is not None and sharpe != 0.0:
        overfit_evaluated = True
        if lockbox_sharpe / sharpe < OVERFIT_RATIO_MIN:
            reason |= RejectionReason.OVERFIT

    return RejectionResult(reason=reason, overfit_gate_evaluated=overfit_evaluated)
