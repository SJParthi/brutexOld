"""Stage-3 CPCV robustness — does a combo work across MANY time-splits, not one?

The last Stage-3 layer. A combo can pass the out-of-sample holdout (ONE train/test
split) and survive the DSR (multiple-testing) yet still owe its whole edge to a
single lucky stretch. Combinatorial Purged Cross-Validation (López de Prado 2018,
Ch. 12) tests it across ALL ``C(16, 8) = 12,870`` held-out fold combinations: the
pass-rate is the fraction whose held-out Sharpe is positive. A genuinely robust
combo passes most folds; a one-lucky-period combo passes few.

This wires the pure :func:`brutex.metrics.cpcv.cpcv_pass_rate` to a combo's PER-TRADE
return series with a safe guard: a series too short to partition into ``n_groups``
CPCV groups (``< 16`` by default) is CPCV-UNDEFINED → returns ``None`` (not a crash,
Golden Rule #10), so the caller computes it only for combos with enough trades.
:func:`combo_returns` is the producer that turns a combo + bars into that per-trade
return series, so the end-to-end check is ``cpcv_robustness(combo_returns(bars, mask, …))``.

Convention (Golden Rule #2): the input ``returns`` are the combo's PER-TRADE returns
(one per closed trade, ``pnl_paisa / start_equity_paisa`` — the same series
:func:`brutex.metrics.compute.compute_metrics` uses for Sharpe), so the fold Sharpes
this computes match the rest of the pipeline. Pure + O(n) over the series + the fixed
``C(S, k)`` fold merges (O(1) per fold in the series length).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from brutex.data.models import CandleBar
from brutex.engine.combo_mask import mask_to_entry_signals
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.sweep_driver import DEFAULT_START_EQUITY_PAISA
from brutex.engine.trade_sim import simulate_trades
from brutex.metrics.cpcv import (
    DEFAULT_N_GROUPS,
    DEFAULT_N_TEST_GROUPS,
    cpcv_pass_rate,
)
from brutex.strategies.types import Side
from brutex.vocabulary import ProximityParams

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.engine.reference_series import TradeReferenceStamper


def combo_returns(
    bars: Sequence[CandleBar],
    required_mask: int,
    *,
    direction: Side,
    tf_minutes: int,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    proximity: ProximityParams | None = None,
    prior_n: int = 3,
    roll_schedule: tuple[int, ...] | None = None,
    apply_1day_mask: bool = False,
    reference_stamper: TradeReferenceStamper | None = None,
) -> list[float]:
    """Run a combo over bars → its PER-TRADE return series.

    The producer that connects the engine's closed trades to the returns-based
    validators (:func:`cpcv_robustness`, lockbox, …): builds the Stage-1A
    ``bar_bits``, projects the mask into entry signals, walks the trades, and emits
    one return per closed trade — ``pnl_paisa / start_equity_paisa``, the SAME series
    :func:`brutex.metrics.compute.compute_metrics` uses for Sharpe, so a downstream
    CPCV sees fold Sharpes consistent with the rest of the pipeline (Golden Rule #2).

    Parameters
    ----------
    bars, required_mask, direction, tf_minutes, stop_loss_paisa, take_profit_paisa, start_equity_paisa, proximity, prior_n
        As :func:`brutex.engine.sweep_driver.evaluate_combo`.
    roll_schedule, apply_1day_mask
        The ADR-0012 futures pass-throughs into
        :func:`~brutex.engine.stage1a.build_bar_bits`
        (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001, review round-2 finding 2).
        Defaults (``None`` / ``False``) are the byte-identical spot path.
    reference_stamper
        Optional India-VIX stamper (DEC-VIX-STAMP-WIRE-001) threaded to the
        trade sim — the closed trades behind this return series carry
        ``vix_at_entry`` / ``vix_at_exit``. REFERENCE ONLY: the returned
        series is byte-identical with or without it.

    Returns
    -------
    list[float]
        One return per closed trade, in close order (empty when the combo fires no
        trades).

    Raises
    ------
    ValueError
        If ``bars`` is empty.
    """
    if not bars:
        raise ValueError("combo_returns: bars must be non-empty")
    bar_bits = build_bar_bits(
        bars,
        proximity=proximity,
        prior_n=prior_n,
        roll_schedule=roll_schedule,
        apply_1day_mask=apply_1day_mask,
    )
    entry_signals = mask_to_entry_signals(bar_bits, required_mask)
    trades = simulate_trades(
        bars,
        entry_signals,
        direction=direction,
        tf_minutes=tf_minutes,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        reference_stamper=reference_stamper,
    )
    return [trade.pnl_paisa / start_equity_paisa for trade in trades]


def cpcv_robustness(
    returns: Sequence[float],
    *,
    n_groups: int = DEFAULT_N_GROUPS,
    n_test_groups: int = DEFAULT_N_TEST_GROUPS,
    min_sharpe: float = 0.0,
) -> float | None:
    """CPCV pass-rate of a combo's per-trade returns, or ``None`` if too short.

    Parameters
    ----------
    returns
        The combo's per-trade return series (``pnl_paisa / start_equity_paisa`` per
        closed trade — the convention :func:`brutex.metrics.compute.compute_metrics`
        uses).
    n_groups, n_test_groups, min_sharpe
        CPCV structural parameters (defaults ``16`` / ``8`` / ``0.0`` — "positive
        held-out Sharpe", matching the Stage-4 walk-forward criterion).

    Returns
    -------
    float | None
        The fraction of CPCV test folds whose held-out Sharpe ``> min_sharpe`` — a
        robustness score in ``[0, 1]`` (higher = robust across more time-splits).
        ``None`` when ``len(returns) < n_groups`` — too few returns to form the
        folds, so CPCV is undefined for this combo (never raised — Golden Rule #10).
    """
    if len(returns) < n_groups:
        return None
    return cpcv_pass_rate(
        returns,
        n_groups=n_groups,
        n_test_groups=n_test_groups,
        min_sharpe=min_sharpe,
    )


__all__ = ["combo_returns", "cpcv_robustness"]
