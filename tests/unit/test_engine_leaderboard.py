"""Unit tests for the self-explaining leaderboard (DEC-LEADERBOARD-EXPLAIN-001).

Covers ``explain_combos`` (pairing each ranked combo with its decoded conditions)
and ``render_leaderboard`` (rows / top-N / empty / no-condition mask) for the strict
100% ``brutex.engine`` package gate.
"""

from __future__ import annotations

from typing import Any

from brutex.engine.leaderboard import (
    ExplainedCombo,
    explain_combos,
    render_leaderboard,
)
from brutex.metrics.compute import ResultMetrics
from brutex.ranking import RankedCombo
from brutex.vocabulary.bits import (
    BIT_BAR_BULLISH,
    BIT_CLOSE_ABOVE_EMA20,
    BIT_CLOSE_ABOVE_VWAP,
    BIT_NEAR_FIB_618,
)


def _m(**overrides: Any) -> ResultMetrics:
    """A ResultMetrics with sensible defaults; override per test."""
    base: dict[str, Any] = {
        "net_pnl_paisa": 0,
        "total_return_pct": 0.0,
        "cagr_pct": 0.0,
        "gross_profit_paisa": 0,
        "gross_loss_paisa": 0,
        "sharpe_ratio": 1.0,
        "sortino_ratio": 1.0,
        "calmar_ratio": 1.0,
        "mar_ratio": 0.0,
        "information_ratio": 0.0,
        "omega_ratio": 0.0,
        "max_drawdown_pct": 5.0,
        "max_drawdown_paisa": 0,
        "max_drawdown_duration_bars": 0,
        "recovery_factor": 1.0,
        "ulcer_index": 0.0,
        "largest_losing_trade_paisa": 0,
        "max_consecutive_losses": 2,
        "max_consecutive_wins": 0,
        "total_trades": 50,
        "winning_trades": 30,
        "losing_trades": 20,
        "win_rate": 0.6,
        "profit_factor": 1.5,
        "avg_winning_trade_paisa": 0,
        "avg_losing_trade_paisa": 0,
        "win_loss_ratio": 1.5,
        "expectancy_paisa": 0,
        "avg_mfe_paisa": 0,
        "avg_mae_paisa": 0,
        "avg_holding_time_bars": 0.0,
        "max_holding_time_bars": 0,
        "time_in_market_pct": 0.0,
        "tail_ratio": 0.0,
        "skewness": 0.0,
        "kurtosis": 0.0,
        "returns_std_dev": 0.0,
        # The loss/growth trio (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 /
        # DEC-METRICS-KELLY-GROWTH-001): loss_rate consistent with the
        # counts above (losing 20 / total 50); the Kelly pair defaults
        # None (undefined) — override per test where the value matters.
        "loss_rate": 0.4,
        "kelly_fraction": None,
        "log_growth_at_half_kelly": None,
    }
    base.update(overrides)
    return ResultMetrics(**base)


def _ranked(mask: int, *, score: float = 1.0, **m_over: Any) -> RankedCombo:
    """A RankedCombo with a given mask + composite score."""
    return RankedCombo(mask=mask, metrics=_m(**m_over), composite_score=score)


class TestExplainCombos:
    def test_pairs_each_combo_with_its_conditions(self) -> None:
        mask = (1 << BIT_CLOSE_ABOVE_EMA20) | (1 << BIT_NEAR_FIB_618)
        explained = explain_combos([_ranked(mask)])
        assert len(explained) == 1
        ec = explained[0]
        assert isinstance(ec, ExplainedCombo)
        assert ec.ranked.mask == mask
        groups = {g.group: g.conditions for g in ec.groups}
        assert groups["EMA Position"] == ("close_above_ema20",)
        assert groups["Fibonacci"] == ("near_fib_618",)

    def test_empty_input_yields_empty(self) -> None:
        assert explain_combos([]) == []

    def test_retained_vwap_bit_reads_plain(self) -> None:
        # DEC-VOCAB-VWAP-RETAIN-001 (MIGRATION.md #2206): bit 52 is LIVE
        # again — it renders its PLAIN name on the CLI leaderboard and NO
        # "(retired)" suffix renders anywhere (RETIRED_MASK == 0; the
        # retired-arm teeth live at the ONE display authority's
        # monkeypatched pin in test_vocabulary_describe.py).
        mask = (1 << BIT_CLOSE_ABOVE_VWAP) | (1 << BIT_CLOSE_ABOVE_EMA20)
        explained = explain_combos([_ranked(mask)])
        groups = {g.group: g.conditions for g in explained[0].groups}
        assert groups["VWAP Position"] == ("close_above_vwap",)
        assert groups["EMA Position"] == ("close_above_ema20",)  # live bit plain
        rendered = render_leaderboard(explained)
        assert "close_above_vwap" in rendered
        assert "(retired)" not in rendered


class TestRenderLeaderboard:
    def test_renders_rows_with_score_and_conditions(self) -> None:
        mask = (1 << BIT_CLOSE_ABOVE_EMA20) | (1 << BIT_BAR_BULLISH)
        text = render_leaderboard(
            explain_combos(
                [_ranked(mask, score=1.234, total_trades=42, avg_mfe_paisa=900, avg_mae_paisa=300)]
            )
        )
        assert "#1" in text
        assert "score=+1.234" in text
        assert "risk_eff=3.00" in text
        assert "trades=42" in text
        assert "close_above_ema20" in text
        assert "bar_bullish" in text

    def test_row_shows_pnl_winrate_profit_factor(self) -> None:
        # The operator's headline question — profit + win-rate — answered in-line.
        text = render_leaderboard(
            explain_combos(
                [
                    _ranked(
                        1 << BIT_CLOSE_ABOVE_EMA20,
                        net_pnl_paisa=1_234_567,  # ₹12,345.67
                        win_rate=0.625,  # 62.5%
                        profit_factor=2.34,
                    )
                ]
            )
        )
        assert "pnl=₹12,345.67" in text
        assert "win=62.5%" in text
        assert "pf=2.34" in text

    def test_row_renders_negative_pnl_and_na_profit_factor(self) -> None:
        # net P&L can be negative; profit_factor is None when there were no losses.
        text = render_leaderboard(
            explain_combos(
                [_ranked(1 << BIT_BAR_BULLISH, net_pnl_paisa=-500_000, profit_factor=None)]
            )
        )
        assert "pnl=₹-5,000.00" in text
        assert "pf=n/a" in text

    def test_top_n_limits_rows(self) -> None:
        combos = explain_combos(
            [
                _ranked(1 << BIT_CLOSE_ABOVE_EMA20, score=2.0),
                _ranked(1 << BIT_BAR_BULLISH, score=1.0),
            ]
        )
        text = render_leaderboard(combos, top_n=1)
        assert "#1" in text
        assert "#2" not in text
        # top_n keeps the FIRST rows, not the tail: the kept row carries combo #1's
        # condition and combo #2's is absent (kills a `[:top_n]`→`[top_n:]` mutant).
        assert "close_above_ema20" in text
        assert "bar_bullish" not in text

    def test_top_n_zero_renders_placeholder(self) -> None:
        # top_n=0 → no rows → the empty placeholder (explicit, not just transitively
        # branch-covered) so a mutant dropping the slice is caught.
        combos = explain_combos([_ranked(1 << BIT_CLOSE_ABOVE_EMA20)])
        assert render_leaderboard(combos, top_n=0) == "(no combos cleared the gates)"

    def test_empty_leaderboard_placeholder(self) -> None:
        assert render_leaderboard([]) == "(no combos cleared the gates)"

    def test_no_condition_mask_renders_placeholder(self) -> None:
        text = render_leaderboard(explain_combos([_ranked(0)]))
        assert "(no conditions — enters every bar)" in text
