"""Per-result trading metrics — the Track 1 Result Row Schema.

Computes the per-trial metrics enumerated in
``docs/TRACK1_BACKTEST_SPEC.md`` §"Result Row Schema (Every Single
Trial — 30+ Metrics)" (spec lines 512-579). Each metric's formula is
traceable to a schema line or an inline spec formula; see
:mod:`brutex.metrics.compute` for the per-field citations.

Metrics groups (all IMPLEMENTED here):

* **Return** (5): ``net_pnl_paisa``, ``total_return_pct``, ``cagr_pct``,
  ``gross_profit_paisa``, ``gross_loss_paisa``.
* **Risk-adjusted** (6): ``sharpe_ratio``, ``sortino_ratio``,
  ``calmar_ratio``, ``mar_ratio``, ``information_ratio``, ``omega_ratio``.
* **Drawdown** (8): ``max_drawdown_pct``, ``max_drawdown_paisa``,
  ``max_drawdown_duration_bars``, ``recovery_factor``, ``ulcer_index``,
  ``largest_losing_trade_paisa``, ``max_consecutive_losses``,
  ``max_consecutive_wins``.
* **Trade quality** (11): counts, ``win_rate``, ``profit_factor``,
  avg win / loss, ``win_loss_ratio``, ``expectancy_paisa``,
  ``avg_mfe_paisa``, ``avg_mae_paisa``.
* **Trade timing** (3): ``avg_holding_time_bars``,
  ``max_holding_time_bars``, ``time_in_market_pct``.
* **Distribution** (4): ``tail_ratio``, ``skewness``, ``kurtosis``,
  ``returns_std_dev``.

Money values are int64 paisa; ratios / percentages / statistical
moments keep full float precision and are NEVER rounded
(DEC-MONEY-2DP-001 is money-display-only). Edge cases (zero trades,
no losers, zero-variance returns, single trade) return ``None`` for the
undefined ratio rather than ``inf`` / ``NaN`` (spec line 746).

**DEFERRED — Stage-4 statistical-validity metrics (NOT implemented):**
``deflated_sharpe_ratio``, ``pbo_probability``, ``cpcv_pass_rate``,
``wfo_pass_rate``, ``lockbox_sharpe``, ``lockbox_mdd_pct`` (spec lines
581-587). The spec itself marks these "Stage 4 only, NULL before then":
they require the cross-validation / walk-forward / lockbox filter
cascade and are out of scope for this per-result module. Honest-envelope
deferral recorded in ``docs/MIGRATION.md`` entry #305 (DEC-METRICS-001),
per Golden Rule #10. The composite ranking score (spec lines 602-615)
and the hard rejection gates (spec lines 589-600) are also Stage-4
concerns and are likewise out of scope here.
"""

from __future__ import annotations

from brutex.metrics.compute import Moments, ResultMetrics, compute_metrics
from brutex.metrics.types import ClosedTrade, EquityCurve

__all__ = [
    "ClosedTrade",
    "EquityCurve",
    "Moments",
    "ResultMetrics",
    "compute_metrics",
]
