"""Composite ranking + hard rejection gates per Track 1 spec.

Hard rejection gates (applied BEFORE composite scoring):
- total_trades < 30 → REJECT (statistical noise)
- profit_factor < 1.2 → REJECT (no edge)
- max_drawdown_pct > 30% → REJECT (uninvestable)
- win_rate < 25% AND win_loss_ratio < 2.0 → REJECT
- max_consecutive_losses > 10 → REJECT (account-blower)
- lockbox_sharpe / training_sharpe < 0.5 → REJECT (overfit)

Composite ranking score (after gates pass):
    0.25 × Sharpe + 0.20 × Sortino + 0.15 × Calmar
  + 0.15 × (Profit_Factor - 1.0) + 0.10 × Recovery_Factor
  + 0.10 × (Win_Rate × Win_Loss_Ratio) + 0.05 × Lockbox_Sharpe

Each term z-score normalized per (instrument, TF) leaderboard, clamped [-3, +3].

## Honest envelope (Golden Rule #10) — the lockbox is Stage-3, deferred here

The 6th gate (overfit) and the 7th composite term both need a **lockbox Sharpe**
— a train/test (CPCV / walk-forward) split — which does **not exist yet** (Stage 3).
So this module implements the **5 single-result gates** + the **6 available
composite terms**; the lockbox gate is skipped and the lockbox term contributes
nothing (its 0.05 weight is simply absent) until the Stage-3 split lands. The
score's absolute scale is irrelevant — it is used only for the *relative* ordering
of a leaderboard (z-scores are relative), so dropping the lockbox term changes
ranking power, never correctness. This is recorded in ``docs/MIGRATION.md``
(DEC-RANKING-001).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from brutex.metrics.compute import ResultMetrics

# ── Hard rejection gate thresholds (the spec, above) ──
_MIN_TRADES = 30
_MIN_PROFIT_FACTOR = 1.2
_MAX_DRAWDOWN_PCT = 30.0
_MIN_WIN_RATE = 0.25
_MIN_WIN_LOSS_RATIO = 2.0
_MAX_CONSECUTIVE_LOSSES = 10

# ── Composite weights (the 6 available terms; the 0.05 lockbox term is Stage-3) ──
_W_SHARPE = 0.25
_W_SORTINO = 0.20
_W_CALMAR = 0.15
_W_PROFIT_FACTOR = 0.15
_W_RECOVERY = 0.10
_W_WIN_WLR = 0.10

#: Z-score clamp (spec: per-leaderboard z-score clamped to [-3, +3]).
_Z_CLAMP = 3.0


@dataclass(frozen=True, slots=True)
class RankedCombo:
    """One gate-passing combo with its leaderboard composite score.

    Attributes
    ----------
    mask
        The u64 strategy mask.
    metrics
        The combo's full :class:`~brutex.metrics.compute.ResultMetrics`.
    composite_score
        The weighted, z-normalised composite — higher is better. Only
        meaningful *relative* to the rest of the same leaderboard.
    """

    mask: int
    metrics: ResultMetrics
    composite_score: float


def passes_gates(m: ResultMetrics) -> bool:
    """Apply the hard rejection gates to one result (lockbox gate deferred).

    Returns ``True`` iff the result survives every applicable gate. A ``None``
    metric (mathematically undefined for the sample) is treated as failing —
    a combo with no real edge cannot be ranked above one with a measured edge.
    """
    if m.total_trades < _MIN_TRADES:
        return False
    if m.profit_factor is None or m.profit_factor < _MIN_PROFIT_FACTOR:
        return False
    if m.max_drawdown_pct > _MAX_DRAWDOWN_PCT:
        return False
    # win_rate < 25% AND win/loss-ratio < 2.0 → reject (a low hit-rate is only
    # acceptable if the winners are much bigger than the losers).
    wlr = m.win_loss_ratio if m.win_loss_ratio is not None else 0.0
    if m.win_rate < _MIN_WIN_RATE and wlr < _MIN_WIN_LOSS_RATIO:
        return False
    return m.max_consecutive_losses <= _MAX_CONSECUTIVE_LOSSES


def _f(value: float | None) -> float:
    """A ``None`` composite term contributes 0 (survivors have real metrics)."""
    return value if value is not None else 0.0


def _zscores(values: Sequence[float]) -> list[float]:
    """Population z-scores clamped to ``[-_Z_CLAMP, +_Z_CLAMP]``.

    Assumes a NON-empty input — :func:`rank_combos` only calls this once it has
    surviving combos. A zero-variance input (all values equal, incl. a single
    survivor) yields all-zeros: no term can discriminate.
    """
    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    if variance == 0.0:
        return [0.0] * n
    std = variance**0.5
    return [max(-_Z_CLAMP, min(_Z_CLAMP, (v - mean) / std)) for v in values]


def rank_combos(candidates: Sequence[tuple[int, ResultMetrics]]) -> list[RankedCombo]:
    """Rank a leaderboard: gate, z-normalise the composite terms, score, sort.

    The composite terms are z-normalised **across the surviving population** (the
    spec's "per (instrument, TF) leaderboard"), so the caller is responsible for
    passing one instrument/TF leaderboard at a time.

    Parameters
    ----------
    candidates
        ``(mask, metrics)`` pairs for one (instrument, TF) leaderboard.

    Returns
    -------
    list[RankedCombo]
        The gate-passing combos sorted by composite score (descending). Empty if
        no candidate survives the gates.
    """
    survivors = [(mask, m) for mask, m in candidates if passes_gates(m)]
    if not survivors:
        return []

    z_sharpe = _zscores([_f(m.sharpe_ratio) for _, m in survivors])
    z_sortino = _zscores([_f(m.sortino_ratio) for _, m in survivors])
    z_calmar = _zscores([_f(m.calmar_ratio) for _, m in survivors])
    z_pf = _zscores([_f(m.profit_factor) - 1.0 for _, m in survivors])
    z_recovery = _zscores([_f(m.recovery_factor) for _, m in survivors])
    z_win_wlr = _zscores([m.win_rate * _f(m.win_loss_ratio) for _, m in survivors])

    ranked = [
        RankedCombo(
            mask=mask,
            metrics=m,
            composite_score=(
                _W_SHARPE * z_sharpe[i]
                + _W_SORTINO * z_sortino[i]
                + _W_CALMAR * z_calmar[i]
                + _W_PROFIT_FACTOR * z_pf[i]
                + _W_RECOVERY * z_recovery[i]
                + _W_WIN_WLR * z_win_wlr[i]
            ),
        )
        for i, (mask, m) in enumerate(survivors)
    ]
    ranked.sort(key=lambda rc: rc.composite_score, reverse=True)
    return ranked


__all__ = [
    "RankedCombo",
    "passes_gates",
    "rank_combos",
]
