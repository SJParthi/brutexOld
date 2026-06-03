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

**Stage-4 statistical-validity metrics (spec lines 581-587).** Implemented
here as pure, deterministic functions:

* ``deflated_sharpe_ratio`` + ``probabilistic_sharpe_ratio`` +
  ``expected_max_sharpe_ratio`` (Bailey & LdP 2014 / 2012) in
  :mod:`brutex.metrics.deflated_sharpe`; ``pbo_probability`` (Bailey et al.
  2017, CSCV) in :mod:`brutex.metrics.pbo` — ``DEC-STAT-DSR-PBO-001``
  (``docs/MIGRATION.md`` entry #430). :func:`compute_metrics` leaves the
  ``ResultMetrics`` DSR / PBO fields NULL; :func:`with_stage4_statistics`
  fills them once the leaderboard's trial count / Sharpe dispersion / CSCV
  matrix are known.
* the CPCV + walk-forward *split generation + pass-rate* surfaces —
  :mod:`brutex.metrics.cpcv` (:func:`cpcv_splits`, :func:`cpcv_pass_rate`)
  and :mod:`brutex.metrics.wfo` (:func:`walk_forward_splits`,
  :func:`wfo_pass_rate`, :func:`wfo_pass_rates`), pure functions over a
  returns series with purging + embargo (CPCV) and
  rolling/anchored/expanding windows (WFO) — ``DEC-STAT-CPCV-WFO-001``
  (``docs/MIGRATION.md`` entry #428).
* the **data-snooping multiple-testing** tests over a matrix of strategy
  excess returns — :mod:`brutex.metrics.spa` (:func:`reality_check` —
  White 2000; :func:`hansen_spa` — Hansen 2005; :func:`romano_wolf` —
  Romano-Wolf 2005/2016), deterministic seeded block-bootstrap p-values +
  FWER rejection set (spec Stage-4 filters #4 + #6) — ``DEC-STAT-SPA-001``
  (``docs/MIGRATION.md`` entry #431).
* the **held-out Lockbox** final-validation gate (:mod:`brutex.metrics.lockbox`,
  :func:`lockbox_evaluation` — adaptive ``>=`` 500-trade window + Sharpe/MDD
  gates, spec filter #8) and the **Monte-Carlo entry-permutation** guard
  (:mod:`brutex.metrics.monte_carlo`, :func:`monte_carlo_permutation_test` —
  relocate the entries, re-run the injected exit logic, one-sided permutation
  p-value, spec filter #9) — ``DEC-STAT-LOCKBOX-MC-001``.
* the **Stage-4 cascade orchestrator** (:mod:`brutex.metrics.cascade`,
  :func:`run_stage4_cascade`) — chains the hard rejection gates (spec lines
  589-600), the nine filters in spec order with short-circuit, and the
  composite ranking (spec lines 602-615) over an injected per-candidate input;
  returns the ranked survivor set + per-candidate verdicts — ``DEC-STAT-CASCADE-001``.

**Still DEFERRED (honest envelope, Golden Rule #10):** wiring the
``cpcv_pass_rate`` / ``wfo_pass_rate`` / ``lockbox_sharpe`` / ``lockbox_mdd_pct``
results into the per-result ``ResultMetrics`` columns (spec lines 586-587), and
the production adapter that builds the cascade's ``CandidateInput``\\s by
re-running the Stage-2 trade-sim + reading the lake (it needs real bars). The
nine filter engines + the orchestrator that chains them now all exist as pure
functions.
"""

from __future__ import annotations

from brutex.metrics.cascade import (
    CandidateInput,
    CandidateVerdict,
    CascadeConfig,
    CascadeReport,
    FilterStage,
    run_stage4_cascade,
)
from brutex.metrics.compute import (
    Moments,
    ResultMetrics,
    compute_metrics,
    with_stage4_statistics,
)
from brutex.metrics.cpcv import (
    DEFAULT_N_GROUPS,
    DEFAULT_N_TEST_GROUPS,
    CPCVSplit,
    cpcv_pass_rate,
    cpcv_splits,
    sharpe_of_returns,
)
from brutex.metrics.deflated_sharpe import (
    DeflatedSharpeResult,
    deflated_sharpe_ratio,
    expected_max_sharpe_ratio,
    probabilistic_sharpe_ratio,
)
from brutex.metrics.lockbox import LockboxResult, LockboxStatus, lockbox_evaluation
from brutex.metrics.monte_carlo import MonteCarloResult, monte_carlo_permutation_test
from brutex.metrics.pbo import PBOResult, pbo_probability, sharpe_from_moments
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
    "CandidateInput",
    "CandidateVerdict",
    "CascadeConfig",
    "CascadeReport",
    "ClosedTrade",
    "DeflatedSharpeResult",
    "EquityCurve",
    "FilterStage",
    "LockboxResult",
    "LockboxStatus",
    "Moments",
    "MonteCarloResult",
    "PBOResult",
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
    "deflated_sharpe_ratio",
    "expected_block_length",
    "expected_max_sharpe_ratio",
    "hansen_spa",
    "lockbox_evaluation",
    "monte_carlo_permutation_test",
    "pbo_probability",
    "probabilistic_sharpe_ratio",
    "reality_check",
    "romano_wolf",
    "run_stage4_cascade",
    "sharpe_from_moments",
    "sharpe_of_returns",
    "walk_forward_splits",
    "wfo_pass_rate",
    "wfo_pass_rates",
    "with_stage4_statistics",
]
