"""Stage-2 sweep driver — one strategy combo → its backtest result metrics.

The first assembled vertical slice of the brute-force funnel: the seam that turns
a strategy mask + real bars into a *measurable result*, joining the engine bricks
that already exist and are unit-tested (per the `DEC-SHARED-CORE-001` roadmap /
`docs/SHARED_CORE_COMPARISON.md`):

    bars --build_bar_bits--> bar_bits --mask_to_entry_signals--> entry_signals
         --simulate_trades--> trades --build_equity_curve--> equity
         --compute_metrics--> ResultMetrics

:func:`evaluate_combo` is the single-combo evaluator. The Stage-2 ranking (the
hard gates + composite score), the K-of-N mask loop, and the ``ResultsStore``
persistence layer on top of this contract without changing it — they consume the
``(mask, ResultMetrics)`` pairs this produces.

Money is int64 paisa throughout (CLAUDE.md §4). Signal-only spot has no real
capital base, so the equity curve uses a configurable notional
``start_equity_paisa`` (the denominator for the drawdown-% / return-% family);
this is a modelling choice recorded in ``docs/MIGRATION.md`` (DEC-SWEEP-DRIVER-001),
not a hidden assumption.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from brutex.data.models import CandleBar
from brutex.engine.combo_mask import mask_to_entry_signals
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.trade_sim import simulate_trades
from brutex.metrics.compute import ResultMetrics, compute_metrics
from brutex.metrics.types import ClosedTrade, EquityCurve
from brutex.strategies.types import Side
from brutex.vocabulary import ProximityParams

#: Notional capital base (paisa) for the signal-only equity curve. ₹100,000.
#: Only the denominator of percentage metrics — absolute paisa P&L is unaffected.
DEFAULT_START_EQUITY_PAISA: Final[int] = 10_000_000

#: Minutes in a (Julian) year, for annualising the equity-curve span.
_MINUTES_PER_YEAR: Final[float] = 365.25 * 24.0 * 60.0


def build_equity_curve(
    bars: Sequence[CandleBar],
    trades: Sequence[ClosedTrade],
    *,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    tf_minutes: int,
) -> EquityCurve:
    """Build the per-bar equity curve from a result's closed trades.

    Each trade's realised P&L lands on the bar at which it *closed* (matched by
    the trade's ``exit_ts`` to a bar ``timestamp``); equity is the running sum of
    ``start_equity_paisa`` + realised P&L, sampled once per bar. The span is the
    wall-clock distance from the first to the last bar (floored to one bar's worth
    of time for a degenerate single-bar / zero-span window, so the
    :class:`EquityCurve` ``span_years > 0`` invariant always holds).

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)``
        (non-empty; unique timestamps).
    trades
        The closed trades from :func:`brutex.engine.trade_sim.simulate_trades`
        over the same ``bars``. Each ``exit_ts`` must be one of the bar
        timestamps.
    start_equity_paisa
        Notional capital base (paisa, ``> 0``); the percentage-metric denominator.
    tf_minutes
        Bar timeframe in whole minutes (``>= 1``), used only for the single-bar
        span floor.

    Returns
    -------
    EquityCurve
        One equity value per bar, plus the base and annualised span.

    Raises
    ------
    ValueError
        If ``bars`` is empty, or a trade's ``exit_ts`` is not a bar timestamp.
    """
    if not bars:
        raise ValueError("build_equity_curve: bars must be non-empty")

    ts_to_index = {bar.timestamp: i for i, bar in enumerate(bars)}
    realized_at_bar = [0] * len(bars)
    for trade in trades:
        idx = ts_to_index.get(trade.exit_ts)
        if idx is None:
            raise ValueError(
                f"build_equity_curve: trade exit_ts {trade.exit_ts!r} is not a bar timestamp"
            )
        realized_at_bar[idx] += trade.pnl_paisa

    equity: list[int] = []
    running = start_equity_paisa
    for realized in realized_at_bar:
        running += realized
        equity.append(running)

    span_seconds = (bars[-1].timestamp - bars[0].timestamp).total_seconds()
    span_years = span_seconds / (_MINUTES_PER_YEAR * 60.0)
    if span_years <= 0.0:
        # Degenerate single-bar (or zero-span) window: floor at one bar of time
        # so EquityCurve's `span_years > 0` invariant holds.
        span_years = tf_minutes / _MINUTES_PER_YEAR

    return EquityCurve(
        equity_paisa=tuple(equity),
        start_equity_paisa=start_equity_paisa,
        span_years=span_years,
    )


def evaluate_combo(
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
) -> ResultMetrics:
    """Backtest ONE strategy combo over real bars → its full result metrics.

    The single-combo evaluator: builds the Stage-1A ``bar_bits``, projects the
    mask into per-bar entry signals, simulates the trades (next-bar-open fills,
    SL/TP, 15:20 forced exit), builds the equity curve, and computes every Track-1
    per-result metric. A no-trade combo yields an all-zero / ``None`` metrics row
    (never an error).

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)`` (non-empty).
    required_mask
        The u64 strategy mask (which of the 52 conditions must fire). ``0`` enters
        on every bar.
    direction
        :data:`~brutex.strategies.types.Side.BUY` (long) or ``Side.SELL`` (short).
    tf_minutes
        Bar timeframe in whole minutes (``>= 1``).
    stop_loss_paisa, take_profit_paisa
        Stop / target distances from entry in paisa (``> 0``), or ``None`` to
        disable that leg.
    start_equity_paisa
        Notional capital base for the percentage-metric denominator.
    proximity
        Proximity tolerances for the ``near_*`` conditions (defaults to 10 bps).
    prior_n
        Prior-bar pattern look-back ∈ {1, 2, 3, 4} (defaults to 3).

    Returns
    -------
    ResultMetrics
        The fully-populated (minus Stage-4) metrics row for this combo.

    Raises
    ------
    ValueError
        If ``bars`` is empty.
    """
    if not bars:
        raise ValueError("evaluate_combo: bars must be non-empty")

    bar_bits = build_bar_bits(bars, proximity=proximity, prior_n=prior_n)
    entry_signals = mask_to_entry_signals(bar_bits, required_mask)
    trades = simulate_trades(
        bars,
        entry_signals,
        direction=direction,
        tf_minutes=tf_minutes,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
    )
    equity = build_equity_curve(
        bars, trades, start_equity_paisa=start_equity_paisa, tf_minutes=tf_minutes
    )
    return compute_metrics(trades, equity, total_bars=len(bars))


__all__ = [
    "DEFAULT_START_EQUITY_PAISA",
    "build_equity_curve",
    "evaluate_combo",
]
