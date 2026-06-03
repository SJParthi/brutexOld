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

**Stage-4 cross-validation primitives (IMPLEMENTED here):** the CPCV and
walk-forward *split generation + pass-rate* surfaces — :mod:`brutex.metrics.cpcv`
(:func:`cpcv_splits`, :func:`cpcv_pass_rate`) and :mod:`brutex.metrics.wfo`
(:func:`walk_forward_splits`, :func:`wfo_pass_rate`, :func:`wfo_pass_rates`).
These are pure, deterministic functions over a returns series, with purging +
embargo (CPCV) and rolling/anchored/expanding windows (WFO). Decision
``DEC-STAT-CPCV-WFO-001`` (``docs/MIGRATION.md`` entry #428).

**Stage-4 multiple-testing tests (IMPLEMENTED in** :mod:`brutex.metrics.spa`
**):** White's Reality Check, Hansen's SPA, and the Romano-Wolf step-down
(spec Stage-4 filters #4 + #6; DEC-STAT-SPA-001, ``docs/MIGRATION.md``
entry #429) are pure bootstrap functions over a matrix of strategy excess
returns and are re-exported here (:func:`reality_check`, :func:`hansen_spa`,
:func:`romano_wolf`). They are independent of the per-result row schema above.

**STILL DEFERRED — Stage-4 cascade wiring + sibling metrics (NOT implemented):**
``deflated_sharpe_ratio``, ``pbo_probability``, ``lockbox_sharpe``,
``lockbox_mdd_pct`` (spec lines 581-587), and the wiring of the
``cpcv_pass_rate`` / ``wfo_pass_rate`` *columns* (and the Reality-Check / SPA /
Romano-Wolf gates) into the result-row schema + the end-to-end Stage-4
cascade. The spec marks the result-row statistical fields "Stage 4 only, NULL
before then"; these PRs ship the CV/WFO + data-snooping *engines* the cascade
will consume, not the full cascade. Honest-envelope deferral recorded in
``docs/MIGRATION.md`` entries #305 (DEC-METRICS-001), #428 (DEC-STAT-CPCV-WFO-001)
and #429 (DEC-STAT-SPA-001), per Golden Rule #10. The composite ranking score
(spec lines 602-615) and the hard rejection gates (spec lines 589-600) remain
out of scope here.
"""

from __future__ import annotations

from brutex.metrics.compute import Moments, ResultMetrics, compute_metrics
from brutex.metrics.cpcv import (
    DEFAULT_N_GROUPS,
    DEFAULT_N_TEST_GROUPS,
    CPCVSplit,
    cpcv_pass_rate,
    cpcv_splits,
    sharpe_of_returns,
)
from brutex.metrics.spa import (
    BootstrapConfig,
    BootstrapMethod,
    RealityCheckResult,
    RomanoWolfResult,
    SPAResult,
    expected_block_length,
    hansen_spa,
    reality_check,
    romano_wolf,
)
from brutex.metrics.types import ClosedTrade, EquityCurve
from brutex.metrics.wfo import (
    WFOPassRates,
    WFOScheme,
    WFOWalk,
    walk_forward_splits,
    wfo_pass_rate,
    wfo_pass_rates,
)

__all__ = [
    "DEFAULT_N_GROUPS",
    "DEFAULT_N_TEST_GROUPS",
    "BootstrapConfig",
    "BootstrapMethod",
    "CPCVSplit",
    "ClosedTrade",
    "EquityCurve",
    "Moments",
    "RealityCheckResult",
    "ResultMetrics",
    "RomanoWolfResult",
    "SPAResult",
    "WFOPassRates",
    "WFOScheme",
    "WFOWalk",
    "compute_metrics",
    "cpcv_pass_rate",
    "cpcv_splits",
    "expected_block_length",
    "hansen_spa",
    "reality_check",
    "romano_wolf",
    "sharpe_of_returns",
    "walk_forward_splits",
    "wfo_pass_rate",
    "wfo_pass_rates",
]
