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

Trade-evidence mode (DEC-STATS-GATE-INTEGRITY-001, R2-SR-F2)
------------------------------------------------------------
When the caller's series is *per-bar* (the cascade wire feeds the per-bar
equity-change series, not per-trade returns), the ``>= min_trades`` count
gate would count BARS — a ~182-calendar-day 1-minute window holds ~47k
bars (~125 NSE sessions × 375 bars), so the DEC-LOCK-001 500-**trade**
floor could never bind. The keyword-only
``evidence_days`` input fixes that: it carries the TRUE per-trade exit-day
ordinals, and both the count gate AND the adaptive window extension are
driven by that evidence while the Sharpe/drawdown are still measured over
the (bar) ``trade_returns`` inside the resolved window. With
``evidence_days=None`` the legacy behaviour is byte-identical (the
observations themselves are counted — honest only when the caller's rows
ARE trades). :func:`resolve_lockbox_window` is the ONE shared resolver of
the train/holdout boundary (CLAUDE.md §11 COMMON) so a caller deriving its
training reference from the complement uses EXACTLY the gate's window.

Honest envelope (Golden Rule #10)
---------------------------------
This is the *gate*, not the data isolation. It is ``O(n_trades)`` (one pass
for the window mask + Sharpe + drawdown) — fine for an offline Stage-4 batch.
The 6-month default and the 500-trade floor are the spec's institutional
defaults (López de Prado / Bailey pattern, spec line 456); no value is
invented here. Holdout-from-SELECTION is NOT enforced pre-Stage-3
(DEC-STATS-GATE-INTEGRITY-001, R2-SR-F1): Stage-1A/2 selection evaluates the
FULL sweep window — lockbox days included — by design (ADR-0007 Amendment
A2; the sweep window is deliberately unchanged). Filter #8 is the gate, and
its *training reference* genuinely excludes the holdout tail only when the
caller computes it over the window complement this module resolves (the
cascade wire does, via :func:`resolve_lockbox_window`).
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
    "DEFAULT_MDD_TOLERANCE",
    "DEFAULT_SHARPE_TOLERANCE",
    "LOCKBOX_DEFAULT_MIN_TRADES",
    "LOCKBOX_DEFAULT_WINDOW_DAYS",
    "LockboxResult",
    "LockboxStatus",
    "LockboxWindow",
    "lockbox_evaluation",
    "resolve_lockbox_window",
]

#: ~6 months of trading days — the spec's institutional default window
#: (spec line 433). NSE trades ~21 days/month ⇒ ~126 sessions, but the spec
#: states the window in *calendar* 6 months; the ordinal-day axis is the
#: caller's (calendar or session) so the default is expressed in days.
_DEFAULT_WINDOW_DAYS: Final[int] = 182

#: Public alias of the default lockbox window (DEC-STAGE3-WIRE-001 / ADR-0007
#: condition 7 + Amendment A1): the Stage-3 train-slice helper
#: (:func:`brutex.engine.stage3_wire.train_slice_bars`) must exclude EXACTLY
#: this window, so it imports THIS constant — one source of truth, never a
#: re-derived boundary (CLAUDE.md §11 COMMON).
LOCKBOX_DEFAULT_WINDOW_DAYS: Final[int] = _DEFAULT_WINDOW_DAYS

#: Hard floor on lockbox trade count (DEC-LOCK-001, spec line 452).
_DEFAULT_MIN_TRADES: Final[int] = 500

#: Public alias of the default lockbox trade floor (DEC-STATS-GATE-INTEGRITY-001,
#: the twin of :data:`LOCKBOX_DEFAULT_WINDOW_DAYS`): downstream config defaults
#: (:class:`brutex.metrics.cascade.CascadeConfig`) and the cascade wire import
#: THIS constant — one source of truth, never a re-typed literal (CLAUDE.md §11
#: COMMON).
LOCKBOX_DEFAULT_MIN_TRADES: Final[int] = _DEFAULT_MIN_TRADES

#: Lockbox Sharpe must stay within 30% of training (spec line 453).
_DEFAULT_SHARPE_TOLERANCE: Final[float] = 0.30

#: Lockbox max-drawdown must stay within 50% of training (spec line 454).
_DEFAULT_MDD_TOLERANCE: Final[float] = 0.50

#: PUBLIC aliases of the two tolerance defaults — the ONE source consumers
#: outside this module derive from (CLAUDE.md §11 COMMON; never a re-typed
#: literal). Added by DEC-NIGHTLY-DECAY-R48-001: the nightly decay verdict
#: (``brutex.nightly.decay_math``) reuses EXACTLY the lockbox degradation
#: law — trailing Sharpe below ``(1 - DEFAULT_SHARPE_TOLERANCE) ×``
#: discovery (the 0.70× floor) / trailing max-DD above
#: ``(1 + DEFAULT_MDD_TOLERANCE) ×`` discovery (the 1.50× ceiling).
DEFAULT_SHARPE_TOLERANCE: Final[float] = _DEFAULT_SHARPE_TOLERANCE

#: Public alias of :data:`_DEFAULT_MDD_TOLERANCE` (see above).
DEFAULT_MDD_TOLERANCE: Final[float] = _DEFAULT_MDD_TOLERANCE


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
        Evidence count in the (possibly extended) lockbox window — the
        TRADE count when ``evidence_days`` was supplied to
        :func:`lockbox_evaluation` (DEC-STATS-GATE-INTEGRITY-001, R2-SR-F2),
        else the number of ``trade_returns`` observations in the window
        (legacy mode: honest only when the caller's rows are trades — a
        per-BAR caller without evidence counts bars).
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


@dataclass(frozen=True, slots=True)
class LockboxWindow:
    """The resolved filter-#8 train/holdout boundary (:func:`resolve_lockbox_window`).

    Attributes
    ----------
    window_start
        Ordinal day the lockbox window starts at (the default-window start,
        or an earlier day when extended). Every day ``>= window_start`` is
        holdout; every day strictly before it is the training complement.
    extended
        ``True`` iff the default window was extended backwards to reach
        ``min_trades`` evidence (or could not reach it at all).
    n_evidence_in_window
        Count of evidence entries (trades when ``evidence_days`` was
        supplied, else series observations) on days ``>= window_start``.
    """

    window_start: int
    extended: bool
    n_evidence_in_window: int


def resolve_lockbox_window(
    days: npt.ArrayLike,
    *,
    evidence_days: npt.ArrayLike | None = None,
    default_window_days: int,
    min_trades: int,
) -> LockboxWindow:
    """Resolve the filter-#8 lockbox window boundary — the ONE shared resolver.

    The single source of truth for the train/holdout split
    (DEC-STATS-GATE-INTEGRITY-001, CLAUDE.md §11 COMMON): both
    :func:`lockbox_evaluation` (the gate) and the cascade wire's
    training-reference complement delegate here, so the boundary can never
    drift between the two sides.

    Semantics: with ``max_day = days[-1]`` the default window starts at
    ``max_day - default_window_days + 1``. The COUNT source is
    ``evidence_days`` when supplied (the true per-trade exit-day ordinals,
    R2-SR-F2), else ``days`` itself (legacy — byte-identical to the
    pre-resolver window logic). If the default window holds ``>= min_trades``
    evidence it is kept; else the window extends backwards to the day of the
    ``min_trades``-th most-recent evidence entry; else (not enough evidence
    anywhere) it spans all of ``days`` and the caller's count gate rejects.

    Parameters
    ----------
    days
        The series' day ordinals (1-D, non-empty, non-decreasing integers).
        ``max_day`` is ALWAYS anchored on ``days[-1]``, never on the
        evidence.
    evidence_days
        Optional true-evidence day ordinals (1-D, non-decreasing; may be
        EMPTY — a zero-trade candidate is legal and simply fails the count
        gate). When non-empty every value must lie within
        ``[days[0], days[-1]]`` (evidence cannot exit outside the series
        span — a violation is caller misalignment, rejected loudly).
    default_window_days, min_trades
        The window length and evidence floor; both ``>= 1`` (REQUIRED
        keywords — no silent default drift between the two delegating
        sides).

    Returns
    -------
    LockboxWindow
        The resolved boundary + the in-window evidence count.

    Raises
    ------
    ValueError
        On a non-positive ``default_window_days``/``min_trades``, a
        malformed ``days`` (not 1-D / empty / decreasing), or a malformed
        ``evidence_days`` (not 1-D / decreasing / outside the ``days``
        span).
    """
    if default_window_days < 1:
        raise ValueError(f"default_window_days must be >= 1, got {default_window_days}")
    if min_trades < 1:
        raise ValueError(f"min_trades must be >= 1, got {min_trades}")
    days_arr = np.asarray(days, dtype=np.int64)
    if days_arr.ndim != 1:
        raise ValueError("days must be 1-D")
    if days_arr.shape[0] == 0:
        raise ValueError("days must be non-empty")
    if bool(np.any(np.diff(days_arr) < 0)):
        raise ValueError("days must be non-decreasing (chronological)")
    if evidence_days is None:
        source = days_arr
    else:
        # Same int64 coercion as ``days`` (symmetric — a float ordinal
        # truncates identically on both axes, never an asymmetric rule).
        source = np.asarray(evidence_days, dtype=np.int64)
        if source.ndim != 1:
            raise ValueError("evidence_days must be 1-D")
        if bool(np.any(np.diff(source) < 0)):
            raise ValueError("evidence_days must be non-decreasing (chronological)")
        if source.shape[0] > 0 and (
            int(source[0]) < int(days_arr[0]) or int(source[-1]) > int(days_arr[-1])
        ):
            raise ValueError(
                f"evidence_days must lie within the days span "
                f"[{int(days_arr[0])}, {int(days_arr[-1])}], got "
                f"[{int(source[0])}, {int(source[-1])}]"
            )

    max_day = int(days_arr[-1])
    default_start = max_day - default_window_days + 1
    n_in_default = int(np.count_nonzero(source >= default_start))
    n_source = int(source.shape[0])
    if n_in_default >= min_trades:
        # The default window already clears the evidence floor.
        window_start = default_start
        extended = False
    elif n_source >= min_trades:
        # Extend backwards (day-aligned) to the day of the ``min_trades``-th
        # most-recent evidence entry — the smallest extension reaching the floor.
        window_start = int(source[n_source - min_trades])
        extended = True
    else:
        # Even every available evidence entry is below the floor → the window
        # spans everything and the caller's count gate rejects INSUFFICIENT_N.
        window_start = int(days_arr[0])
        extended = True
    n_evidence = int(np.count_nonzero(source >= window_start))
    return LockboxWindow(
        window_start=window_start, extended=extended, n_evidence_in_window=n_evidence
    )


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
    evidence_days: npt.ArrayLike | None = None,
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
    evidence_days
        Optional TRUE-evidence day ordinals (DEC-STATS-GATE-INTEGRITY-001,
        R2-SR-F2): the per-trade exit-day ordinals when ``trade_returns`` is
        a per-BAR series. When supplied, the count gate counts evidence
        entries in-window (real trades, so the DEC-LOCK-001 500-trade floor
        actually binds) AND the adaptive window extension is driven by the
        evidence; the Sharpe/drawdown stay measured over ``trade_returns``
        inside the resolved window. May be EMPTY (a zero-trade candidate ⇒
        ``INSUFFICIENT_N`` with ``n_trades == 0``, never a crash); when
        non-empty must be non-decreasing and lie within
        ``[trade_days[0], trade_days[-1]]``. ``None`` (the default) is the
        exact legacy behaviour, byte-identical.

    Returns
    -------
    LockboxResult
        The status, the pass boolean, the lockbox metrics, and the three
        gate booleans.

    Raises
    ------
    ValueError
        If the inputs fail :func:`_validate_inputs` /
        :func:`resolve_lockbox_window` (incl. ``default_window_days < 1``,
        ``min_trades < 1``, and a malformed ``evidence_days``), or any of
        ``sharpe_tolerance < 0``, ``mdd_tolerance < 0``, or
        ``training_max_drawdown < 0``.
    """
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
    # The ONE shared boundary resolver (DEC-STATS-GATE-INTEGRITY-001):
    # ``evidence_days=None`` reproduces the legacy window logic byte-for-byte;
    # supplied evidence drives the count + extension by TRUE trades.
    window = resolve_lockbox_window(
        days,
        evidence_days=evidence_days,
        default_window_days=default_window_days,
        min_trades=min_trades,
    )
    mask = days >= window.window_start
    lockbox_returns = returns[mask]
    # With evidence: the trade count in-window; without: the observation
    # count — identical to ``len(lockbox_returns)`` since the count source
    # was ``days`` itself.
    n_trades = window.n_evidence_in_window
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
        window_start_day=window.window_start,
        extended=window.extended,
        sharpe_gate_passed=sharpe_gate,
        mdd_gate_passed=mdd_gate,
        count_gate_passed=count_gate,
        min_trades=min_trades,
    )
