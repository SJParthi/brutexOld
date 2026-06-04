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
from brutex.engine.combo_mask import (
    count_combos,
    count_supermasks,
    iter_masks_k_of_bits,
    mask_to_entry_signals,
)
from brutex.engine.stage1a import build_bar_bits
from brutex.engine.trade_sim import simulate_trades
from brutex.metrics.compute import ResultMetrics, compute_metrics
from brutex.metrics.types import ClosedTrade, EquityCurve
from brutex.ranking import MIN_TRADES, RankedCombo, rank_combos
from brutex.strategies.types import Side
from brutex.vocabulary import ProximityParams

#: Notional capital base (paisa) for the signal-only equity curve. ₹100,000.
#: Only the denominator of percentage metrics — absolute paisa P&L is unaffected.
DEFAULT_START_EQUITY_PAISA: Final[int] = 10_000_000

#: Minutes in a (Julian) year, for annualising the equity-curve span.
_MINUTES_PER_YEAR: Final[float] = 365.25 * 24.0 * 60.0

#: Hard ceiling on the masks a single :func:`rank_sweep_over_bits` will enumerate
#: without an explicit opt-in — the honest "this is a BIG sweep, raise the cap on
#: purpose" guardrail (Golden Rule #10, no-illusion limits). For scale:
#: ``C(52,3)=22,100``; ``C(52,4)=270,725``; ``C(52,5)=2,598,960`` — so the default
#: admits a full-vocab K≤4 sweep and forces a conscious choice beyond that, rather
#: than silently launching a multi-million-combo run.
DEFAULT_MAX_COMBOS: Final[int] = 100_000


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
    return metrics_for_mask(
        bars,
        bar_bits,
        required_mask,
        direction=direction,
        tf_minutes=tf_minutes,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        start_equity_paisa=start_equity_paisa,
    )


def metrics_for_mask(
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    required_mask: int,
    *,
    direction: Side,
    tf_minutes: int,
    stop_loss_paisa: int | None,
    take_profit_paisa: int | None,
    start_equity_paisa: int,
) -> ResultMetrics:
    """Project one mask onto pre-built ``bar_bits`` → its metrics (the shared core).

    The per-mask body shared by :func:`evaluate_combo` (one mask),
    :func:`sweep_masks` (many masks over ONE ``bar_bits`` build), and
    :func:`brutex.engine.validation.validate_out_of_sample` (one build, sliced into
    train/test) — so callers build the Stage-1A matrix once and reuse it. Public so
    those callers need not duplicate the chain (CLAUDE.md §11 COMMON).
    """
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


def sweep_masks(
    bars: Sequence[CandleBar],
    masks: Sequence[int],
    *,
    direction: Side,
    tf_minutes: int,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    proximity: ProximityParams | None = None,
    prior_n: int = 3,
    min_hits: int = 0,
) -> list[tuple[int, ResultMetrics]]:
    """Backtest MANY masks over the same bars → ``(mask, metrics)`` pairs.

    Builds the Stage-1A ``bar_bits`` matrix **once** (the per-bar truth-vectors
    are identical for every mask — only the AND-mask differs), then projects each
    mask through the shared per-mask core. O(1) per (mask, bar): one AND + one
    CMP, then the trade walk + metrics.

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)`` (non-empty).
    masks
        The u64 strategy masks to evaluate (e.g. from
        :func:`brutex.engine.combo_mask.iter_masks_k_of_n`).
    direction, tf_minutes, stop_loss_paisa, take_profit_paisa, start_equity_paisa, proximity, prior_n
        As :func:`evaluate_combo`.
    min_hits
        **Stage-1B coarse pre-filter** (DEC-STAGE1B-WIRE-001). When ``> 0``, a mask
        is dropped BEFORE the expensive trade sim if it fires (is a superset) on
        fewer than ``min_hits`` bars — counted cheaply via the batched Rust kernel
        :func:`~brutex.engine.combo_mask.count_supermasks`. Because the trade
        simulator opens at most one trade per entry signal (no pyramiding), a mask
        with fewer hits than the ``MIN_TRADES`` gate can never be rankable, so
        pruning it does not change any *ranked* leaderboard — it only skips dead
        work. ``0`` (default) disables the pre-filter (every mask is simulated).

    Returns
    -------
    list[tuple[int, ResultMetrics]]
        One ``(mask, metrics)`` pair per **surviving** mask, in input order (every
        input mask when ``min_hits == 0``).

    Raises
    ------
    ValueError
        If ``bars`` is empty.
    """
    if not bars:
        raise ValueError("sweep_masks: bars must be non-empty")
    bar_bits = build_bar_bits(bars, proximity=proximity, prior_n=prior_n)
    swept = masks
    if min_hits > 0:
        # Count each mask's superset hits once (one batched Rust call), then keep
        # only masks that could possibly reach the trade-count gate.
        hits = count_supermasks(bar_bits, masks)
        swept = [mask for mask, hit in zip(masks, hits, strict=True) if hit >= min_hits]
    return [
        (
            mask,
            metrics_for_mask(
                bars,
                bar_bits,
                mask,
                direction=direction,
                tf_minutes=tf_minutes,
                stop_loss_paisa=stop_loss_paisa,
                take_profit_paisa=take_profit_paisa,
                start_equity_paisa=start_equity_paisa,
            ),
        )
        for mask in swept
    ]


def rank_sweep(
    bars: Sequence[CandleBar],
    masks: Sequence[int],
    *,
    direction: Side,
    tf_minutes: int,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    proximity: ProximityParams | None = None,
    prior_n: int = 3,
    min_hits: int = 0,
) -> list[RankedCombo]:
    """Sweep a set of masks and return the **ranked leaderboard** — the top combos.

    :func:`sweep_masks` backtests every mask (skipping any pruned by the optional
    ``min_hits`` Stage-1B pre-filter), then :func:`brutex.ranking.rank_combos`
    applies the hard rejection gates + composite z-score and sorts. The head of the
    returned list is the top-performing combo for this
    ``(instrument, TF, direction, params)``. ``min_hits`` (default ``0`` = off) only
    skips masks that could never clear the trade-count gate, so it never changes the
    ranked result — see :func:`sweep_masks`.
    """
    return rank_combos(
        sweep_masks(
            bars,
            masks,
            direction=direction,
            tf_minutes=tf_minutes,
            stop_loss_paisa=stop_loss_paisa,
            take_profit_paisa=take_profit_paisa,
            start_equity_paisa=start_equity_paisa,
            proximity=proximity,
            prior_n=prior_n,
            min_hits=min_hits,
        )
    )


def rank_sweep_over_bits(
    bars: Sequence[CandleBar],
    condition_bits: Sequence[int],
    k: int,
    *,
    direction: Side,
    tf_minutes: int,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    proximity: ProximityParams | None = None,
    prior_n: int = 3,
    max_combos: int = DEFAULT_MAX_COMBOS,
    min_hits: int = MIN_TRADES,
) -> list[RankedCombo]:
    """Sweep + rank over the **K-of-the-chosen-condition-bits** mask space → top combos.

    The operator-facing *"find the top combo over MY chosen conditions"* entry: it
    enumerates exactly ``C(len(set(condition_bits)), k)`` masks — every ``k``-subset
    of the chosen conditions, via
    :func:`~brutex.engine.combo_mask.iter_masks_k_of_bits` (an ARBITRARY subset of
    the 52, e.g. only the pivot + Fibonacci bits — not the contiguous prefix that
    :func:`~brutex.engine.combo_mask.iter_masks_k_of_n` enumerates) — then ranks them
    with :func:`rank_sweep`. The head of the returned leaderboard is the
    top-performing combo for this ``(instrument, TF, direction, params)``.

    Refuses to start a sweep larger than ``max_combos`` (raising BEFORE any
    ``bar_bits`` build or trade simulation — the honest "you asked for a huge sweep,
    opt in explicitly" guardrail, Golden Rule #10); pass a larger ``max_combos``
    deliberately to run one. The ``bar_bits`` matrix is built once for the whole
    sweep (:func:`sweep_masks`); each mask is then one AND+CMP per bar.

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)`` (non-empty).
    condition_bits
        The condition bit positions (of the 52) to brute-force over, each in
        ``[0, 64)``. Duplicates are ignored.
    k
        How many of the chosen conditions each combo requires (``0 <= k <= n``,
        where ``n`` is the unique-bit count).
    direction, tf_minutes, stop_loss_paisa, take_profit_paisa, start_equity_paisa, proximity, prior_n
        As :func:`evaluate_combo`.
    max_combos
        Refuse (raise) rather than enumerate more than this many masks
        (default :data:`DEFAULT_MAX_COMBOS`).
    min_hits
        Stage-1B coarse pre-filter threshold (DEC-STAGE1B-WIRE-001). Defaults to
        :data:`brutex.ranking.MIN_TRADES` — a mask firing on fewer bars than the
        trade-count gate can never be rankable, so it is pruned before the trade sim
        (identical leaderboard, less work). Pass ``0`` to disable (simulate every
        enumerated mask). See :func:`sweep_masks`.

    Returns
    -------
    list[RankedCombo]
        The gate-passing combos sorted by composite score (descending); empty if
        none clears the hard gates.

    Raises
    ------
    ValueError
        If ``bars`` is empty, any bit is out of range, ``k`` is outside
        ``[0, n_unique]``, or ``C(n_unique, k)`` exceeds ``max_combos``.
    """
    if not bars:
        raise ValueError("rank_sweep_over_bits: bars must be non-empty")
    n_unique = len(set(condition_bits))
    n_masks = count_combos(k, n_unique)  # C(n, k); validates 0 <= k <= n
    if n_masks > max_combos:
        msg = (
            f"rank_sweep_over_bits: {n_masks} combos (C({n_unique},{k})) "
            f"exceeds max_combos={max_combos}; raise max_combos explicitly "
            f"to run a sweep this large"
        )
        raise ValueError(msg)
    masks = list(iter_masks_k_of_bits(condition_bits, k))
    return rank_sweep(
        bars,
        masks,
        direction=direction,
        tf_minutes=tf_minutes,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        start_equity_paisa=start_equity_paisa,
        proximity=proximity,
        prior_n=prior_n,
        min_hits=min_hits,
    )


__all__ = [
    "DEFAULT_MAX_COMBOS",
    "DEFAULT_START_EQUITY_PAISA",
    "build_equity_curve",
    "evaluate_combo",
    "metrics_for_mask",
    "rank_sweep",
    "rank_sweep_over_bits",
    "sweep_masks",
]
