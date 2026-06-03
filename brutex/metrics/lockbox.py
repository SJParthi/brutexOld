"""Lockbox held-out final-validation filter — Stage-4 filter #8.

Implements the Lockbox gate of ``docs/TRACK1_BACKTEST_SPEC.md`` §"Stage 4 —
Statistical filters" item #8 (spec lines 433-463), locked by **DEC-LOCK-001**
(adaptive-window revision, 2026-05-16) and ledgered under **DEC-STAT-LOCKBOX-MC-001**.

The lockbox is a slice of the most recent trades that NO prior stage (1A→3)
was allowed to see. A surviving candidate is re-measured on it ONCE; if its
out-of-sample behaviour degrades too far from training it is rejected. This
module is the pure, deterministic *gate computation* — the held-out-data
isolation itself (the "any access path outside Stage 4.8 raises" guarantee,
spec line 434) is the orchestrator's job, not this function's.

Adaptive window (DEC-LOCK-001)
------------------------------
The default window is the most recent ``default_window_days`` trading days
(~6 months). If that holds **fewer than** ``min_trades`` (500) trades, the
window auto-extends *backwards* one trading day at a time until it
accumulates ``>= min_trades`` (the extension consumes data from *before* the
training window, never the training window itself — preserving the held-out
invariant). If ``min_trades`` cannot be reached even using every available
trade, the candidate is rejected with ``status = INSUFFICIENT_N``.

Pass criteria (spec lines 451-454)
----------------------------------
* lockbox trade count ``>= min_trades`` (hard floor),
* lockbox Sharpe within ``sharpe_tolerance`` (30%) of training:
  ``lockbox_sharpe >= training_sharpe * (1 - sharpe_tolerance)``,
* lockbox max-drawdown within ``mdd_tolerance`` (50%) of training:
  ``lockbox_max_drawdown <= training_max_drawdown * (1 + mdd_tolerance)``.

Conventions
-----------
``trade_returns`` are per-trade returns in chronological order;
``trade_days`` is the matching trading-day ordinal of each trade's exit
(any monotonically non-decreasing integer day index — e.g. ``numpy``
``datetime64[D]`` cast to int, or a session counter). The Sharpe reuses the
COMMON :func:`brutex.metrics.cpcv.sharpe_of_returns` (CLAUDE.md §11 — one
source of truth: population std, zero risk-free, no annualisation). The
max-drawdown is the largest peak-to-trough decline of the cumulative-return
equity path, in return units — the caller supplies ``training_max_drawdown``
in the same units. All statistics are full-precision floats and are never
rounded (DEC-MONEY-2DP-001 is money-display only).

Honest envelope (Golden Rule #10)
---------------------------------
This is the *gate*, not the data isolation. It is ``O(n_trades)`` (one pass
for the window mask + Sharpe + drawdown) — fine for an offline Stage-4 batch.
The 6-month default and the 500-trade floor are the spec's institutional
defaults (López de Prado / Bailey pattern, spec line 456); no value is
invented here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final

import numpy as np

from brutex.metrics.cpcv import sharpe_of_returns

if TYPE_CHECKING:
    import numpy.typing as npt

__all__ = [
    "LockboxResult",
    "LockboxStatus",
    "lockbox_evaluation",
]

#: ~6 months of trading days — the spec's institutional default window
#: (spec line 433). NSE trades ~21 days/month ⇒ ~126 sessions, but the spec
#: states the window in *calendar* 6 months; the ordinal-day axis is the
#: caller's (calendar or session) so the default is expressed in days.
_DEFAULT_WINDOW_DAYS: Final[int] = 182

#: Hard floor on lockbox trade count (DEC-LOCK-001, spec line 452).
_DEFAULT_MIN_TRADES: Final[int] = 500

#: Lockbox Sharpe must stay within 30% of training (spec line 453).
_DEFAULT_SHARPE_TOLERANCE: Final[float] = 0.30

#: Lockbox max-drawdown must stay within 50% of training (spec line 454).
_DEFAULT_MDD_TOLERANCE: Final[float] = 0.50


class LockboxStatus(Enum):
    """Outcome of a lockbox evaluation.

    Attributes
    ----------
    PASS
        Trade-count, Sharpe, and drawdown gates all satisfied.
    FAIL
        Enough trades, but the Sharpe and/or drawdown gate failed.
    INSUFFICIENT_N
        Fewer than ``min_trades`` trades available even after extending the
        window across all history — rejected for insufficient evidence
        (spec line 443, ``lockbox_status = "insufficient_n"``).
    """

    # S105/B105 fire because the member is named PASS (in the linters' password
    # word-list) + a string value — it is a status label, not a credential.
    PASS = "pass"  # noqa: S105 # nosec B105
    FAIL = "fail"
    INSUFFICIENT_N = "insufficient_n"


@dataclass(frozen=True, slots=True)
class LockboxResult:
    """Result of :func:`lockbox_evaluation`.

    Attributes
    ----------
    status
        :class:`LockboxStatus` (``PASS`` / ``FAIL`` / ``INSUFFICIENT_N``).
    passed
        ``True`` iff ``status is LockboxStatus.PASS`` — the single boolean
        the cascade gates on.
    n_trades
        Number of trades in the (possibly extended) lockbox window.
    lockbox_sharpe
        Per-trade Sharpe of the lockbox returns, or ``None`` if undefined
        (fewer than two trades / zero variance).
    lockbox_max_drawdown
        Largest peak-to-trough decline of the lockbox cumulative-return
        equity path (return units, ``>= 0``).
    training_sharpe, training_max_drawdown
        The training-set references echoed for provenance.
    window_start_day
        Ordinal day the lockbox window starts at (the default-window start,
        or an earlier day if it was extended).
    extended
        ``True`` iff the default window was extended backwards to reach
        ``min_trades``.
    sharpe_gate_passed, mdd_gate_passed, count_gate_passed
        The three individual gate booleans.
    min_trades
        The trade-count floor used.
    """

    status: LockboxStatus
    passed: bool
    n_trades: int
    lockbox_sharpe: float | None
    lockbox_max_drawdown: float
    training_sharpe: float
    training_max_drawdown: float
    window_start_day: int
    extended: bool
    sharpe_gate_passed: bool
    mdd_gate_passed: bool
    count_gate_passed: bool
    min_trades: int


def _validate_inputs(
    trade_returns: npt.ArrayLike, trade_days: npt.ArrayLike
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.int64]]:
    """Coerce + validate the per-trade returns and day ordinals.

    Raises
    ------
    ValueError
        If shapes are not 1-D and equal-length, the arrays are empty,
        ``trade_returns`` is non-finite, or ``trade_days`` is not
        non-decreasing (trades must be chronological).
    """
    returns = np.asarray(trade_returns, dtype=np.float64)
    days = np.asarray(trade_days, dtype=np.int64)
    if returns.ndim != 1 or days.ndim != 1:
        raise ValueError("trade_returns and trade_days must both be 1-D")
    if returns.shape[0] != days.shape[0]:
        raise ValueError(
            f"trade_returns and trade_days must be equal length, "
            f"got {returns.shape[0]} and {days.shape[0]}"
        )
    if returns.shape[0] == 0:
        raise ValueError("trade_returns must be non-empty")
    if not bool(np.all(np.isfinite(returns))):
        raise ValueError("trade_returns must be finite (no NaN/inf)")
    if bool(np.any(np.diff(days) < 0)):
        raise ValueError("trade_days must be non-decreasing (trades chronological)")
    return returns, days


def _max_drawdown(returns: npt.NDArray[np.float64]) -> float:
    """Largest peak-to-trough decline of the cumulative-return equity path.

    Returns a non-negative magnitude in return units (``0.0`` for an empty
    or monotonically non-decreasing path).
    """
    if returns.size == 0:
        return 0.0
    equity = np.cumsum(returns)
    running_peak = np.maximum.accumulate(equity)
    return float(np.max(running_peak - equity))


def lockbox_evaluation(
    trade_returns: npt.ArrayLike,
    trade_days: npt.ArrayLike,
    *,
    training_sharpe: float,
    training_max_drawdown: float,
    default_window_days: int = _DEFAULT_WINDOW_DAYS,
    min_trades: int = _DEFAULT_MIN_TRADES,
    sharpe_tolerance: float = _DEFAULT_SHARPE_TOLERANCE,
    mdd_tolerance: float = _DEFAULT_MDD_TOLERANCE,
) -> LockboxResult:
    """Evaluate a candidate against the held-out lockbox window (spec #8).

    Parameters
    ----------
    trade_returns
        Per-trade returns, chronological.
    trade_days
        Trading-day ordinal of each trade (non-decreasing, same length as
        ``trade_returns``).
    training_sharpe
        Sharpe achieved on the training set (the reference for the 30% gate).
    training_max_drawdown
        Max-drawdown on the training set, in the same return units as
        :func:`_max_drawdown` (the reference for the 50% gate); must be
        ``>= 0``.
    default_window_days
        Default lockbox window length in trading-day ordinals (~6 months).
    min_trades
        Hard floor on lockbox trade count; below it the candidate is
        rejected ``INSUFFICIENT_N``.
    sharpe_tolerance
        Fractional Sharpe drop tolerated vs training (``0.30`` ⇒ within 30%).
    mdd_tolerance
        Fractional drawdown increase tolerated vs training (``0.50`` ⇒ 50%).

    Returns
    -------
    LockboxResult
        The status, the pass boolean, the lockbox metrics, and the three
        gate booleans.

    Raises
    ------
    ValueError
        If the inputs fail :func:`_validate_inputs`, or any of
        ``default_window_days < 1``, ``min_trades < 1``,
        ``sharpe_tolerance < 0``, ``mdd_tolerance < 0``, or
        ``training_max_drawdown < 0``.
    """
    if default_window_days < 1:
        raise ValueError(f"default_window_days must be >= 1, got {default_window_days}")
    if min_trades < 1:
        raise ValueError(f"min_trades must be >= 1, got {min_trades}")
    if sharpe_tolerance < 0.0:
        raise ValueError(f"sharpe_tolerance must be >= 0, got {sharpe_tolerance}")
    if mdd_tolerance < 0.0:
        raise ValueError(f"mdd_tolerance must be >= 0, got {mdd_tolerance}")
    # Finiteness of the training references: they are echoed into the result
    # row, which must never carry NaN/inf (spec line 746).
    if not math.isfinite(training_sharpe):
        raise ValueError(f"training_sharpe must be finite, got {training_sharpe}")
    if not math.isfinite(training_max_drawdown) or training_max_drawdown < 0.0:
        raise ValueError(
            f"training_max_drawdown must be a finite value >= 0, got {training_max_drawdown}"
        )

    returns, days = _validate_inputs(trade_returns, trade_days)
    n_total = int(returns.shape[0])
    max_day = int(days[-1])
    default_start = max_day - default_window_days + 1

    n_in_default = int(np.count_nonzero(days >= default_start))
    if n_in_default >= min_trades:
        # The default 6-month window already clears the trade floor.
        window_start = default_start
        extended = False
    elif n_total >= min_trades:
        # Extend backwards (day-aligned) to the day of the ``min_trades``-th
        # most-recent trade — the smallest extension that reaches the floor.
        window_start = int(days[n_total - min_trades])
        extended = True
    else:
        # Even every available trade is below the floor → insufficient.
        window_start = int(days[0])
        extended = True

    mask = days >= window_start
    lockbox_returns = returns[mask]
    n_trades = int(lockbox_returns.shape[0])
    lockbox_sharpe = sharpe_of_returns(lockbox_returns)
    lockbox_mdd = _max_drawdown(lockbox_returns)

    count_gate = n_trades >= min_trades
    sharpe_floor = training_sharpe * (1.0 - sharpe_tolerance)
    sharpe_gate = lockbox_sharpe is not None and lockbox_sharpe >= sharpe_floor
    mdd_ceiling = training_max_drawdown * (1.0 + mdd_tolerance)
    mdd_gate = lockbox_mdd <= mdd_ceiling

    if not count_gate:
        status = LockboxStatus.INSUFFICIENT_N
        passed = False
    elif sharpe_gate and mdd_gate:
        status = LockboxStatus.PASS
        passed = True
    else:
        status = LockboxStatus.FAIL
        passed = False

    return LockboxResult(
        status=status,
        passed=passed,
        n_trades=n_trades,
        lockbox_sharpe=lockbox_sharpe,
        lockbox_max_drawdown=lockbox_mdd,
        training_sharpe=training_sharpe,
        training_max_drawdown=training_max_drawdown,
        window_start_day=window_start,
        extended=extended,
        sharpe_gate_passed=sharpe_gate,
        mdd_gate_passed=mdd_gate,
        count_gate_passed=count_gate,
        min_trades=min_trades,
    )
