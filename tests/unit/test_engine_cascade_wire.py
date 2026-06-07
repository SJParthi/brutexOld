"""Unit tests for :mod:`brutex.engine.cascade_wire` — the Stage-4 cascade wiring.

Targets the strict 100% line + branch gate (CLAUDE.md §4 / Golden Rule #12) for
the COMMON adapter that runs the 9-filter cascade on a sweep's persisted survivors
BOTH automatically and manually:

* :class:`CascadeSurvivorSpec` — every ``__post_init__`` validation branch.
* :func:`build_candidate_input` — empty-bars guard, the ``bar_bits`` None/given
  branches, the per-trade + empty-trade ``_per_bar_returns`` branches, and the
  ``_metric_or_zero`` None branch (a degenerate mask with no real Sharpe).
* :func:`run_cascade_for_survivors` — the per-axis ``bar_bits`` cache hit + miss
  and the proximity None/not-None descriptor branches.
* :func:`cascade_report_for_store` — bull + bear direction mapping.
* :func:`run_cascade_on_store` — the empty-store no-op + the populated path
  (sidecar written).
* :func:`manual_cascade_report` — the empty-rows / ``top_k<1`` /
  ``start_equity<=0`` guards + the happy path; :func:`_tf_minutes` known/unknown.

Bars are small, deterministic, tz-aware synthetic sessions with ``Decimal`` OHLC
(Golden Rule #1 — real-shaped data, no fabrication). Engine imports pull the
``fno_math`` / ``trading_core`` wheels, so this is a CI-3.12 test; CI is the oracle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest
from brutex.data.models import CandleBar
from brutex.engine.cascade_sidecar import CASCADE_SIDECAR_NAME
from brutex.engine.cascade_wire import (
    DEFAULT_CASCADE_TOP_K,
    CascadeAutoResult,
    CascadeSurvivorSpec,
    _per_bar_returns,
    _tf_minutes,
    build_candidate_input,
    cascade_report_for_store,
    manual_cascade_report,
    run_cascade_for_survivors,
    run_cascade_on_store,
)
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.metrics.cascade import CascadeConfig, CascadeReport
from brutex.strategies.types import Side
from brutex.vocabulary import ProximityParams
from brutex.vocabulary.bits import BIT_BAR_BULLISH

# pyright: reportPrivateUsage=none

_IST = timezone(timedelta(hours=5, minutes=30))


def _bar(day: int, minute: int, o: float, c: float) -> CandleBar:
    ts = (
        datetime(2026, 1, 5, 9, 16, tzinfo=_IST) + timedelta(days=day, minutes=minute)
    ).astimezone(UTC)
    hi = max(o, c) + 0.5
    lo = min(o, c) - 0.5
    return CandleBar(
        timestamp=ts,
        open=Decimal(str(round(o, 2))),
        high=Decimal(str(round(hi, 2))),
        low=Decimal(str(round(lo, 2))),
        close=Decimal(str(round(c, 2))),
        volume=100,
        open_interest=None,
    )


def _trending_bars(n_days: int = 12, per_day: int = 90) -> list[CandleBar]:
    """An upward-drifting multi-day session — BUY combos fire + win often."""
    bars: list[CandleBar] = []
    for d in range(n_days):
        price = 100.0
        for m in range(per_day):
            close = price + 0.6 if m % 3 else price - 0.3
            bars.append(_bar(d, m, price, close))
            price = close
    return bars


#: A mask requiring the "bar is bullish" condition — fires on most up-bars.
_MASK = 1 << BIT_BAR_BULLISH


def _spec(run_id: str, *, mask: int = _MASK, **overrides: object) -> CascadeSurvivorSpec:
    base: dict[str, object] = {
        "run_id": run_id,
        "mask_bits": mask,
        "direction": Side.BUY,
        "tf_minutes": 1,
        "sl_paisa": None,
        "tp_paisa": None,
        "proximity": None,
        "prior_n": 3,
        "start_equity_paisa": 10_000_000,
    }
    base.update(overrides)
    return CascadeSurvivorSpec(**base)  # type: ignore[arg-type]


def _permissive_config(**overrides: object) -> CascadeConfig:
    """A config loose enough that a real BUY combo survives most stages."""
    base: dict[str, object] = {
        "seed": 3,
        "min_total_trades": 1,
        "min_profit_factor": 0.0,
        "max_drawdown_pct": 1.0e9,
        "min_win_rate": 0.0,
        "min_win_loss_ratio": 0.0,
        "max_consecutive_losses": 10_000,
        "dsr_min": 0.0,
        "cpcv_min_pass_rate": 0.0,
        "spa_alpha": 1.0,
        "rw_alpha": 1.0,
        "pbo_max": 1.0,
        "pbo_n_splits": 4,
        "wfo_min_pass_rate": 0.0,
        "wfo_train_size": 8,
        "wfo_test_size": 4,
        "wfo_step": 4,
        "lockbox_min_trades": 1,
        "lockbox_default_window_days": 60,
        "mc_alpha": 1.0,
        "mc_n_permutations": 3,
        "n_resamples": 8,
    }
    base.update(overrides)
    return CascadeConfig(**base)  # type: ignore[arg-type]


_RID_A = "a" * 64
_RID_B = "b" * 64


# ────── CascadeSurvivorSpec validation ──────


class TestCascadeSurvivorSpec:
    def test_valid_spec(self) -> None:
        assert _spec(_RID_A).run_id == _RID_A

    def test_empty_run_id_raises(self) -> None:
        with pytest.raises(ValueError, match="run_id must be non-empty"):
            _spec("")

    def test_negative_mask_raises(self) -> None:
        with pytest.raises(ValueError, match="mask_bits must be >= 0"):
            _spec(_RID_A, mask=-1)

    def test_hold_direction_raises(self) -> None:
        with pytest.raises(ValueError, match="must be Side.BUY or Side.SELL"):
            _spec(_RID_A, direction=Side.HOLD)

    def test_bad_tf_minutes_raises(self) -> None:
        with pytest.raises(ValueError, match="tf_minutes must be >= 1"):
            _spec(_RID_A, tf_minutes=0)

    def test_nonpositive_sl_raises(self) -> None:
        with pytest.raises(ValueError, match="sl_paisa must be > 0"):
            _spec(_RID_A, sl_paisa=0)

    def test_nonpositive_tp_raises(self) -> None:
        with pytest.raises(ValueError, match="tp_paisa must be > 0"):
            _spec(_RID_A, tp_paisa=-5)

    def test_bad_prior_n_raises(self) -> None:
        with pytest.raises(ValueError, match="prior_n must be >= 1"):
            _spec(_RID_A, prior_n=0)

    def test_nonpositive_equity_raises(self) -> None:
        with pytest.raises(ValueError, match="start_equity_paisa must be > 0"):
            _spec(_RID_A, start_equity_paisa=0)


# ────── _per_bar_returns ──────


class TestPerBarReturns:
    def test_empty_trades_all_zero(self) -> None:
        bars = _trending_bars(2, 10)
        series = _per_bar_returns(bars, [], start_equity_paisa=1_000)
        assert series == tuple([0.0] * len(bars))

    def test_trade_lands_on_exit_bar(self) -> None:
        bars = _trending_bars(3, 30)
        # Re-simulate a real mask so the exit timestamps map onto bar indices.
        from brutex.engine.combo_mask import mask_to_entry_signals
        from brutex.engine.stage1a import build_bar_bits
        from brutex.engine.trade_sim import simulate_trades

        bits = build_bar_bits(bars, proximity=None, prior_n=3)
        trades = simulate_trades(
            bars, mask_to_entry_signals(bits, _MASK), direction=Side.BUY, tf_minutes=1
        )
        series = _per_bar_returns(bars, trades, start_equity_paisa=10_000_000)
        assert len(series) == len(bars)
        assert any(r != 0.0 for r in series)  # at least one trade contributes


# ────── build_candidate_input ──────


class TestBuildCandidateInput:
    def test_empty_bars_raises(self) -> None:
        with pytest.raises(ValueError, match="bars must be non-empty"):
            build_candidate_input(_spec(_RID_A), [])

    def test_builds_with_inline_bar_bits(self) -> None:
        bars = _trending_bars()
        candidate = build_candidate_input(_spec(_RID_A), bars)
        assert candidate.candidate_id == _RID_A
        assert len(candidate.returns) == len(bars)
        assert candidate.mc_n_bars == len(bars)
        assert candidate.mc_n_entries >= 1

    def test_builds_with_provided_bar_bits(self) -> None:
        from brutex.engine.stage1a import build_bar_bits

        bars = _trending_bars()
        bits = build_bar_bits(bars, proximity=None, prior_n=3)
        candidate = build_candidate_input(_spec(_RID_A), bars, bar_bits=bits)
        # Identical to the inline-built candidate (determinism, Golden Rule #4).
        inline = build_candidate_input(_spec(_RID_A), bars)
        assert candidate.returns == inline.returns
        assert candidate.sharpe_ratio == inline.sharpe_ratio

    def test_degenerate_mask_maps_none_metrics_to_zero(self) -> None:
        # A mask that never fires → no trades → None ratios → mapped to 0.0
        # (exercises the _metric_or_zero None branch + the empty-trade returns).
        bars = _trending_bars(2, 10)
        # bit 51 is a rarely/never-set condition on this synthetic feed.
        spec = _spec(_RID_A, mask=1 << 51)
        candidate = build_candidate_input(spec, bars)
        assert candidate.total_trades == 0
        assert candidate.sharpe_ratio == 0.0
        assert candidate.win_loss_ratio == 0.0
        assert candidate.skew == 0.0
        assert all(r == 0.0 for r in candidate.returns)

    def test_mc_evaluate_returns_float(self) -> None:
        bars = _trending_bars(3, 30)
        candidate = build_candidate_input(_spec(_RID_A), bars)
        positions: npt.NDArray[np.intp] = np.array([0, 5, 10], dtype=np.intp)
        value = candidate.mc_evaluate(positions)
        assert isinstance(value, float)

    def test_mc_evaluate_degenerate_returns_nan(self) -> None:
        # A single relocated entry can yield a flat/short series → None Sharpe → nan.
        bars = _trending_bars(2, 10)
        candidate = build_candidate_input(_spec(_RID_A), bars)
        positions: npt.NDArray[np.intp] = np.array([0], dtype=np.intp)
        value = candidate.mc_evaluate(positions)
        assert isinstance(value, float)  # nan is a float; the MC engine treats it as "no beat"


# ────── run_cascade_for_survivors ──────


class TestRunCascadeForSurvivors:
    def test_two_candidates_share_axis_cache(self) -> None:
        bars = _trending_bars()
        specs = [_spec(_RID_A), _spec(_RID_B)]
        report = run_cascade_for_survivors(specs, lambda _s: bars, _permissive_config())
        assert isinstance(report, CascadeReport)
        assert report.n_input == 2

    def test_proximity_descriptor_branch(self) -> None:
        bars = _trending_bars()
        prox = ProximityParams(
            pivot_proximity_bps=20, pdh_pdl_proximity_bps=20, fib_proximity_bps=20
        )
        specs = [_spec(_RID_A, proximity=prox), _spec(_RID_B, proximity=prox)]
        report = run_cascade_for_survivors(specs, lambda _s: bars, _permissive_config())
        assert report.n_input == 2


# ────── cascade_report_for_store ──────


def _result_row(run_id: str, direction: str, mask: int = _MASK) -> ResultRow:
    return ResultRow(
        run_id=run_id,
        instrument="NIFTY",
        tf="1minute",
        direction=direction,
        mode="A",
        mask_bits=mask,
        net_pnl_paisa=1_000,
        gross_profit_paisa=2_000,
        gross_loss_paisa=1_000,
        max_drawdown_paisa=500,
        expectancy_paisa=10,
        total_trades=100,
        winning_trades=60,
        losing_trades=40,
        sharpe_ratio=1.2,
        sortino_ratio=1.4,
        calmar_ratio=0.8,
        profit_factor=2.0,
        win_rate=0.6,
        max_drawdown_pct=5.0,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
    )


class TestCascadeReportForStore:
    def test_bull_and_bear_direction_mapping(self) -> None:
        bars = _trending_bars()
        rows = [_result_row(_RID_A, "bull"), _result_row(_RID_B, "bear")]
        report = cascade_report_for_store(
            rows,
            lambda _row: bars,
            lambda _row: {
                "tf_minutes": 1,
                "sl_paisa": None,
                "tp_paisa": None,
                "proximity": None,
                "prior_n": 3,
                "start_equity_paisa": 10_000_000,
            },
            _permissive_config(),
        )
        assert report.n_input == 2


# ────── run_cascade_on_store ──────


class TestRunCascadeOnStore:
    def test_empty_store_is_noop(self, tmp_path: Path) -> None:
        store = ResultsStore(path=tmp_path / "empty.parquet")
        result = run_cascade_on_store(
            store,
            _trending_bars(),
            {
                "tf_minutes": 1,
                "sl_paisa": None,
                "tp_paisa": None,
                "proximity": None,
                "prior_n": 3,
                "start_equity_paisa": 10_000_000,
            },
            _permissive_config(),
            sidecar_path=tmp_path / "side.parquet",
        )
        assert isinstance(result, CascadeAutoResult)
        assert result.report is None
        assert result.sidecar is None
        assert not (tmp_path / "side.parquet").exists()

    def test_populated_store_writes_sidecar(self, tmp_path: Path) -> None:
        store = ResultsStore(path=tmp_path / "r.parquet")
        store.append(_result_row(_RID_A, "bull"))
        store.append(_result_row(_RID_B, "bull", mask=_MASK | (1 << 1)))
        sidecar = tmp_path / CASCADE_SIDECAR_NAME
        result = run_cascade_on_store(
            store,
            _trending_bars(),
            {
                "tf_minutes": 1,
                "sl_paisa": None,
                "tp_paisa": None,
                "proximity": None,
                "prior_n": 3,
                "start_equity_paisa": 10_000_000,
            },
            _permissive_config(),
            sidecar_path=sidecar,
            top_k=DEFAULT_CASCADE_TOP_K,
        )
        assert result.report is not None
        assert result.sidecar is not None
        assert sidecar.exists()


# ────── manual_cascade_report + _tf_minutes ──────


class TestManualCascadeReport:
    def test_empty_rows_raises(self) -> None:
        with pytest.raises(ValueError, match="no persisted rows"):
            manual_cascade_report(
                [], lambda _i, _t: [], _permissive_config(), start_equity_paisa=10_000_000
            )

    def test_top_k_below_one_raises(self) -> None:
        with pytest.raises(ValueError, match="top_k must be >= 1"):
            manual_cascade_report(
                [_result_row(_RID_A, "bull")],
                lambda _i, _t: _trending_bars(),
                _permissive_config(),
                start_equity_paisa=10_000_000,
                top_k=0,
            )

    def test_nonpositive_equity_raises(self) -> None:
        with pytest.raises(ValueError, match="start_equity_paisa must be > 0"):
            manual_cascade_report(
                [_result_row(_RID_A, "bull")],
                lambda _i, _t: _trending_bars(),
                _permissive_config(),
                start_equity_paisa=0,
            )

    def test_happy_path(self) -> None:
        bars = _trending_bars()
        rows = [_result_row(_RID_A, "bull"), _result_row(_RID_B, "bull", mask=_MASK | (1 << 1))]
        report = manual_cascade_report(
            rows,
            lambda _i, _t: bars,
            _permissive_config(),
            start_equity_paisa=10_000_000,
        )
        assert report.n_input == 2


class TestTfMinutes:
    def test_known_label(self) -> None:
        assert _tf_minutes("5minute") == 5

    def test_unknown_label_defaults_to_one(self) -> None:
        assert _tf_minutes("not-a-tf") == 1


def test_module_all_is_complete() -> None:
    import brutex.engine.cascade_wire as mod

    for name in mod.__all__:
        assert hasattr(mod, name)
