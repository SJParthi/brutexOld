"""Shared WP5 top-100/results-depth test fixtures (DEC-SWEEP-RESULTS-DEPTH-001).

Factories for valid, GATE-PASSING :class:`~brutex.engine.results_store.ResultRow`
+ :class:`~brutex.engine.combo_aggregates.ComboAggregate` pairs and pooled
:class:`~brutex.engine.top100_artifact.Top100Candidate` instances, with every
field overridable so a test can flip exactly one gate/rank input at a time.
A NEW uniquely-named fake module per the parallel-build discipline (no edits
to existing fakes).
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Any

from brutex.engine.combo_aggregates import ComboAggregate
from brutex.engine.results_store import ResultRow
from brutex.engine.top100_artifact import Top100Candidate

__all__ = ["make_agg", "make_candidate", "make_result_row", "run_id_of", "with_agg"]


def run_id_of(n: int) -> str:
    """A deterministic, valid 64-hex run_id — distinct per *n*, ordered by *n*."""
    return f"{n:064x}"


def make_result_row(run_id: str, **overrides: Any) -> ResultRow:
    """A valid ResultRow whose defaults PASS every hard gate."""
    base: dict[str, Any] = {
        "run_id": run_id,
        "instrument": "NIFTY",
        "tf": "1minute",
        "direction": "bull",
        "mode": "A",
        "mask_bits": 0b111,
        "net_pnl_paisa": 10_000,
        "gross_profit_paisa": 16_000,
        "gross_loss_paisa": 6_000,
        "max_drawdown_paisa": 2_000,
        "expectancy_paisa": 250,
        "total_trades": 40,
        "winning_trades": 25,
        "losing_trades": 15,
        "sharpe_ratio": 1.0,
        "sortino_ratio": 1.2,
        "calmar_ratio": 0.8,
        "profit_factor": 1.5,
        "win_rate": 0.625,
        "max_drawdown_pct": 10.0,
        "avg_entry_price": Decimal("100.00"),
        "avg_exit_price": Decimal("100.25"),
    }
    base.update(overrides)
    return ResultRow(**base)


def make_agg(run_id: str, *, fingerprint: str, **overrides: Any) -> ComboAggregate:
    """A valid ComboAggregate whose defaults PASS every hard gate."""
    base: dict[str, Any] = {
        "run_id": run_id,
        "n_trades": 40,
        "zero_trades": False,
        "parity_ok": True,
        "mae_avg_paisa": 120,
        "mae_worst_paisa": 400,
        "mae_p95_paisa": 350,
        "mfe_avg_paisa": 300,
        "mfe_best_paisa": 900,
        "mfe_p95_paisa": 800,
        "mae_1m_avg_paisa": None,
        "mae_1m_worst_paisa": None,
        "mae_1m_p95_paisa": None,
        "mfe_1m_avg_paisa": None,
        "mfe_1m_best_paisa": None,
        "mfe_1m_p95_paisa": None,
        "trades_with_1m": 0,
        "max_dd_minute_path_paisa": 1_500,
        "max_dd_minute_path_pct": 1.5,
        "equity_status": "no_1m_data",
        "worst5_days_json": '[["2024-01-02",-500]]',
        "vix_win_min": 12.0,
        "vix_win_avg": 14.5,
        "vix_win_max": 18.0,
        "vix_loss_min": 11.0,
        "vix_loss_avg": 13.0,
        "vix_loss_max": 16.0,
        "vix_band_lo": 12.5,
        "vix_band_hi": 17.0,
        "vix_stamped_trades": 40,
        "vix_missing_trades": 0,
        "pnl_by_year_json": '{"2024":10000}',
        "pnl_by_trend_json": '{"unknown":10000}',
        "one_regime_wonder": False,
        "regime_wonder_axis": None,
        "entry_hour_hist_json": '{"9":40}',
        "event_day_counts_json": "{}",
        "event_day_pnl_json": "{}",
        "exits_stop_loss": 5,
        "exits_take_profit": 10,
        "exits_signal": 20,
        "exits_forced_1520": 5,
        "exits_end_of_data": 0,
        "exits_session_gap": 0,
        "max_consecutive_losses": 3,
        "max_consecutive_wins": 6,
        "composite_score": 1.25,
        "win_loss_ratio": 2.5,
        "recovery_factor": 5.0,
        "fingerprint": fingerprint,
    }
    base.update(overrides)
    return ComboAggregate(**base)


def make_candidate(
    n: int,
    *,
    fingerprint: str,
    row_overrides: dict[str, Any] | None = None,
    agg_overrides: dict[str, Any] | None = None,
    sl_paisa: int | None = 80,
    tp_paisa: int | None = 50,
    tuple_ref: str = "NIFTY/1minute/bull_k1_deadbeefdeadbeef",
) -> Top100Candidate:
    """A pooled, gate-passing candidate keyed by the deterministic run_id *n*."""
    run_id = run_id_of(n)
    row = make_result_row(run_id, **(row_overrides or {}))
    agg = make_agg(run_id, fingerprint=fingerprint, **(agg_overrides or {}))
    return Top100Candidate(
        row=row, agg=agg, sl_paisa=sl_paisa, tp_paisa=tp_paisa, tuple_ref=tuple_ref
    )


def with_agg(candidate: Top100Candidate, **agg_overrides: Any) -> Top100Candidate:
    """Return *candidate* with its agg's fields replaced (frozen-safe)."""
    return replace(candidate, agg=replace(candidate.agg, **agg_overrides))
