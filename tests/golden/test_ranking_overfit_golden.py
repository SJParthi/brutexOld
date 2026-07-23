"""Golden byte-equality fixture for the Stage-3 overfit gate (DEC-RANKING-OVERFIT-001).

Freezes the EXACT overfit-gated leaderboard a fixed candidate pool produces — the
surviving masks, their full-precision composite scores, and their Deflated Sharpe
values, in order — so any drift in the deflation maths, the gate threshold, the
DSR z-term, or the composite weighting is caught bit-for-bit (Golden Rule #4).

The pool has four gate-passing combos with distinct Sharpes; the weakest
(``mask=8``, Sharpe 1.0) is deflated below the 0.95 bar and dropped, while the
three strong combos survive and are ranked by their composite (which now carries
the Deflated-Sharpe term).
"""

from __future__ import annotations

import json
from typing import Any

from brutex.metrics.compute import ResultMetrics
from brutex.ranking import OverfitGateConfig, rank_combos


def _m(**overrides: Any) -> ResultMetrics:
    """A gate-passing ResultMetrics; override per scenario."""
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


#: The frozen leaderboard. Floats are stored as their exact ``repr`` so the
#: comparison is bit-for-bit (no tolerance) — the determinism oracle.
_GOLDEN: str = json.dumps(
    [
        {
            "mask": 4,
            "composite_score": "0.7927106202284727",
            "deflated_sharpe": "0.9999999999834508",
        },
        {
            "mask": 2,
            "composite_score": "-0.018293363200750326",
            "deflated_sharpe": "0.9999999985288788",
        },
        {
            "mask": 1,
            "composite_score": "-0.7744172569977615",
            "deflated_sharpe": "0.9999996062310741",
        },
    ],
    indent=2,
)


def _render(ranked: object) -> str:
    rows = [
        {
            "mask": rc.mask,
            "composite_score": repr(rc.composite_score),
            "deflated_sharpe": repr(rc.deflated_sharpe),
        }
        for rc in ranked  # type: ignore[attr-defined]
    ]
    return json.dumps(rows, indent=2)


def test_overfit_leaderboard_is_byte_frozen() -> None:
    cand = [
        (0b0001, _m(sharpe_ratio=2.0, sortino_ratio=1.8, calmar_ratio=1.2)),
        (0b0010, _m(sharpe_ratio=2.5, sortino_ratio=2.0, calmar_ratio=1.5)),
        (0b0100, _m(sharpe_ratio=3.0, sortino_ratio=2.4, calmar_ratio=1.8)),
        (0b1000, _m(sharpe_ratio=1.0, sortino_ratio=0.9, calmar_ratio=0.5)),  # deflated out
    ]
    ranked = rank_combos(cand, overfit=OverfitGateConfig(dsr_min=0.95))
    assert _render(ranked) == _GOLDEN


def test_overfit_leaderboard_is_reproducible() -> None:
    # Same inputs → byte-identical render across repeated calls (Golden Rule #4).
    cand = [
        (0b0001, _m(sharpe_ratio=2.0, sortino_ratio=1.8, calmar_ratio=1.2)),
        (0b0010, _m(sharpe_ratio=2.5, sortino_ratio=2.0, calmar_ratio=1.5)),
        (0b0100, _m(sharpe_ratio=3.0, sortino_ratio=2.4, calmar_ratio=1.8)),
        (0b1000, _m(sharpe_ratio=1.0, sortino_ratio=0.9, calmar_ratio=0.5)),
    ]
    cfg = OverfitGateConfig(dsr_min=0.95)
    assert _render(rank_combos(cand, overfit=cfg)) == _render(rank_combos(cand, overfit=cfg))
