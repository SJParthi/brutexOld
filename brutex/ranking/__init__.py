"""Composite ranking + hard rejection gates per Track 1 spec.

Implements ``docs/TRACK1_BACKTEST_SPEC.md`` §"Hard rejection gates"
(spec lines 589-600) and §"Composite ranking score (Stage 4)" (spec
lines 602-618). The gates run per-result BEFORE scoring; the composite
score is a per-(instrument, TF) leaderboard z-score operation.

Hard rejection gates (:mod:`brutex.ranking.gates`, spec lines 589-600) —
a result is REJECTED if ANY of:

* ``total_trades < 30`` (statistical noise);
* ``profit_factor < 1.2`` (no edge; ``None`` = no losers → PASS);
* ``max_drawdown_pct > 30`` (uninvestable);
* ``win_rate < 0.25`` AND ``win_loss_ratio < 2.0`` (uninvestable profile);
* ``max_consecutive_losses > 10`` (account-blower);
* ``lockbox_sharpe / sharpe < 0.5`` (overfit — Stage-4, skipped when
  ``lockbox_sharpe is None``).

Composite ranking score (:mod:`brutex.ranking.score`, spec lines
602-615), each term a per-(instrument, TF) leaderboard z-score clamped
``[-3, +3]``::

    0.25·norm(sharpe) + 0.20·norm(sortino) + 0.15·norm(calmar)
  + 0.15·norm(profit_factor − 1) + 0.10·norm(recovery_factor)
  + 0.10·norm(win_rate · win_loss_ratio) + 0.05·norm(lockbox_sharpe)

**Stage-4 lockbox deferral (DEC-RANKING-001, MIGRATION #307):** when no
survivor carries a ``lockbox_sharpe`` (pre-Stage-4), the overfit gate is
SKIPPED and the ``0.05·norm(lockbox_sharpe)`` term is DROPPED with the
six remaining weights renormalized to sum to ``1.0``. See the
:mod:`brutex.ranking.gates` and :mod:`brutex.ranking.score` docstrings
for the full conventions (incl. the ``None``-metric convention), all
disclosed per CLAUDE.md Golden Rule #10. The single canonical metrics
type :class:`brutex.metrics.ResultMetrics` is reused as the input
(Golden Rule #11 — no parallel metrics type).
"""

from __future__ import annotations

from brutex.ranking.gates import (
    RejectionReason,
    RejectionResult,
    passes_gates,
)
from brutex.ranking.score import (
    RankedResult,
    RankInput,
    rank_leaderboard,
)

__all__ = [
    "RankInput",
    "RankedResult",
    "RejectionReason",
    "RejectionResult",
    "passes_gates",
    "rank_leaderboard",
]
