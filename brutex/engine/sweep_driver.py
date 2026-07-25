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

import json
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

import fno_math

from brutex.data.models import CandleBar
from brutex.engine.combo_mask import (
    count_combos,
    count_supermasks,
    iter_masks_k_of_bits,
    mask_to_entry_signals,
)
from brutex.engine.exec_grid import ExecGridMap, build_exec_grid_map, exec_window_slice
from brutex.engine.exit_index import build_ts_index, exit_indices_consistent
from brutex.engine.rust_metrics import (
    FLAT_DRAWDOWN_STATS,
    consume_self_check_sample,
    disable_kernel,
    drawdown_compare_key,
    drawdown_stats_via_kernel,
    exceeds_exact_equity_envelope,
    self_check_pending,
    sweep_rust_metrics_enabled,
    warn_fallback_once,
)
from brutex.engine.stage1a import (
    DEFAULT_WF_STEP_DAYS,
    DEFAULT_WF_TEST_DAYS,
    DEFAULT_WF_TRAIN_DAYS,
    WalkForwardFold,
    WalkForwardSchedule,
    build_bar_bits,
    build_walk_forward_schedule,
)
from brutex.engine.trade_sim import (
    STOP_LOSS_REASON_INDEX,
    simulate_trades_with_exit_counts,
)
from brutex.feed.errors import LookAheadError
from brutex.feed.lookahead import LookAheadBarrier, LookAheadReport, verify_with_barrier

# Intentional friend access (DEC-P0-017 per-site convention): `_drawdown_stats`
# IS the Python drawdown oracle the burn-in self-check bit-compares the kernel
# against — importing it (never copying) keeps ONE oracle source of truth
# (CLAUDE.md §11 COMMON).
from brutex.metrics.compute import (
    ResultMetrics,
    _drawdown_stats,  # pyright: ignore[reportPrivateUsage]
    compute_metrics,
    compute_metrics_from_scalars,
)
from brutex.metrics.types import ClosedTrade, EquityCurve
from brutex.ranking import MIN_TRADES, RankedCombo, passes_gates, rank_combos
from brutex.strategies.combo import reject_unit_separator
from brutex.strategies.types import Side
from brutex.vocabulary import ProximityParams

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.engine.reference_series import TradeReferenceStamper

#: Notional capital base (paisa) for the signal-only equity curve. ₹100,000.
#: Only the denominator of percentage metrics — absolute paisa P&L is unaffected.
DEFAULT_START_EQUITY_PAISA: Final[int] = 10_000_000

#: Minutes in a (Julian) year, for annualising the equity-curve span.
_MINUTES_PER_YEAR: Final[float] = 365.25 * 24.0 * 60.0

#: Hard ceiling on the masks a single :func:`rank_sweep_over_bits` will enumerate
#: without an explicit opt-in — the honest "this is a BIG sweep, raise the cap on
#: purpose" guardrail (Golden Rule #10, no-illusion limits). For scale (over the
#: 64 active bits — DEC-SUPERTREND-CONDITIONS-001): ``C(64,3)=41,664``;
#: ``C(64,4)=635,376``;
#: ``C(64,5)=7,624,512`` — so the default admits a full-vocab K≤3 sweep and
#: forces a conscious choice beyond that (K=4
#: over the full 64 already exceeds the cap and is loudly REFUSED — pinned by
#: ``test_default_bits_k4_hits_loud_refusal_cap``), rather than silently
#: launching a multi-100k-combo run.
DEFAULT_MAX_COMBOS: Final[int] = 100_000


def build_equity_curve(
    bars: Sequence[CandleBar],
    trades: Sequence[ClosedTrade],
    *,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    tf_minutes: int,
) -> EquityCurve:
    """Build the per-bar equity curve from a result's closed trades.

    Each trade's realised P&L lands on its OWNING bar — the LAST bar whose
    label is ``<= exit_ts`` (DEC-FILL-EXEC-GRID-1M-001 design §3.8: exits now
    resolve on the 1-minute execution grid, so a coarse-TF trade's ``exit_ts``
    is an INTERIOR instant of its owning decision bucket; the historical
    exact-timestamp match is the ``exit_ts == label`` special case of the same
    bisect, byte-identical for tf=1minute / label-aligned exits). Equity is
    the running sum of ``start_equity_paisa`` + realised P&L, sampled once per
    bar. The span is the wall-clock distance from the first to the last bar
    (floored to one bar's worth of time for a degenerate single-bar /
    zero-span window, so the :class:`EquityCurve` ``span_years > 0`` invariant
    always holds).

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)``
        (non-empty; unique timestamps).
    trades
        The closed trades from :func:`brutex.engine.trade_sim.simulate_trades`
        over the same decision ``bars`` (+ their 1m execution stream). Each
        ``exit_ts`` must be at-or-after the first bar's label.
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
        If ``bars`` is empty, or a trade's ``exit_ts`` precedes the first
        bar's label (out-of-window — refused LOUDLY, never folded).
    """
    if not bars:
        raise ValueError("build_equity_curve: bars must be non-empty")

    ts_keys = [bar.timestamp for bar in bars]
    realized_at_bar = [0] * len(bars)
    for trade in trades:
        idx = bisect_right(ts_keys, trade.exit_ts) - 1
        if idx < 0:
            raise ValueError(
                f"build_equity_curve: trade exit_ts {trade.exit_ts!r} precedes the "
                f"first bar's label {ts_keys[0]!r} — out of window"
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
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None = None,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    proximity: ProximityParams | None = None,
    prior_n: int = 3,
    roll_schedule: tuple[int, ...] | None = None,
    apply_1day_mask: bool = False,
    reference_stamper: TradeReferenceStamper | None = None,
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
        The strategy mask (which vocabulary conditions must fire). ``0`` enters
        on every bar.
    direction
        :data:`~brutex.strategies.types.Side.BUY` (long) or ``Side.SELL`` (short).
    tf_minutes
        Bar timeframe in whole minutes (``>= 1``).
    exec_bars_1m
        The 1-minute EXECUTION stream (DEC-FILL-EXEC-GRID-1M-001) — REQUIRED,
        no default (design §3.3: no single-feed call shape survives). At
        ``tf_minutes == 1`` pass the same sequence as ``bars``.
    exec_map
        Optional pre-built :class:`~brutex.engine.exec_grid.ExecGridMap` for
        this exact stream pair; ``None`` derives it once here.
    stop_loss_paisa, take_profit_paisa
        Stop / target distances from entry in paisa (``> 0``), or ``None`` to
        disable that leg.
    start_equity_paisa
        Notional capital base for the percentage-metric denominator.
    proximity
        Proximity tolerances for the ``near_*`` conditions (defaults to 10 bps).
    prior_n
        Prior-bar pattern look-back ∈ {1, 2, 3, 4} (defaults to 3).
    roll_schedule, apply_1day_mask
        The ADR-0012 futures pass-throughs into
        :func:`~brutex.engine.stage1a.build_bar_bits`
        (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001, review round-2 finding 2 —
        every evaluator entry point accepts them, so roll-stitched futures
        bars are never forced through a schedule-blind build). Defaults
        (``None`` / ``False``) are the byte-identical spot path.
    reference_stamper
        Optional India-VIX
        :class:`~brutex.engine.reference_series.TradeReferenceStamper`
        (DEC-VIX-STAMP-WIRE-001) threaded to the trade sim so every simulated
        trade carries ``vix_at_entry`` / ``vix_at_exit`` — REFERENCE ONLY,
        never a signal / metric / run-identity input; the returned metrics are
        byte-identical with or without it. ``None`` (default) = unstamped.

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

    bar_bits = build_bar_bits(
        bars,
        proximity=proximity,
        prior_n=prior_n,
        roll_schedule=roll_schedule,
        apply_1day_mask=apply_1day_mask,
    )
    return metrics_for_mask(
        bars,
        bar_bits,
        required_mask,
        direction=direction,
        tf_minutes=tf_minutes,
        exec_bars_1m=exec_bars_1m,
        exec_map=exec_map,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        start_equity_paisa=start_equity_paisa,
        reference_stamper=reference_stamper,
    )


def metrics_for_mask(
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    required_mask: int,
    *,
    direction: Side,
    tf_minutes: int,
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None = None,
    stop_loss_paisa: int | None,
    take_profit_paisa: int | None,
    start_equity_paisa: int,
    reference_stamper: TradeReferenceStamper | None = None,
    trailing_anchor: datetime | None = None,
) -> ResultMetrics:
    """Project one mask onto pre-built ``bar_bits`` → its metrics (the shared core).

    The per-mask body shared by :func:`evaluate_combo` (one mask),
    :func:`sweep_masks` (many masks over ONE ``bar_bits`` build), and
    :func:`brutex.engine.validation.validate_out_of_sample` (one build, sliced into
    train/test) — so callers build the Stage-1A matrix once and reuse it. Public so
    those callers need not duplicate the chain (CLAUDE.md §11 COMMON). The
    walker's certified exit indices are threaded into the metrics tail
    (DEC-SWEEP-EXIT-INDEX-FASTPATH-001) — a pure fast-path hint, results
    byte-identical with or without it — and so are the walker's exit-reason
    counters (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B), which feed the
    metrics' ``sl_hit_rate``. Every fill follows the ONE hardcoded
    worst-case adverse-max law (DEC-FILL-WORST-CASE-ONLY-001) — the retired
    ``FillPolicy`` threading is deleted with its modes. ``trailing_anchor``
    (DEC-METRICS-TRAILING-WINDOW-001) passes through to
    :func:`metrics_from_trades` — the GLOBAL recency anchor; ``None`` (the
    default) leaves every recency field the honest ``None``.
    """
    if exec_map is None:
        # Derived ONCE per call (the standalone-evaluator arm); the hot sweep
        # spine threads the tuple's memoised map instead (design §1.5).
        exec_map = build_exec_grid_map(bars, exec_bars_1m, tf_minutes=tf_minutes)
    trades, exit_indices, exit_reason_counts = _simulate_window(
        bars,
        bar_bits,
        required_mask,
        direction=direction,
        tf_minutes=tf_minutes,
        exec_bars_1m=exec_bars_1m,
        exec_map=exec_map,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        reference_stamper=reference_stamper,
    )
    return metrics_from_trades(
        bars,
        trades,
        start_equity_paisa=start_equity_paisa,
        tf_minutes=tf_minutes,
        exec_bars_1m=exec_bars_1m,
        exec_map=exec_map,
        exit_indices=exit_indices,
        exit_reason_counts=exit_reason_counts,
        trailing_anchor=trailing_anchor,
    )


def _simulate_window(
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    required_mask: int,
    *,
    direction: Side,
    tf_minutes: int,
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None = None,
    stop_loss_paisa: int | None,
    take_profit_paisa: int | None,
    reference_stamper: TradeReferenceStamper | None = None,
) -> tuple[list[ClosedTrade], list[int] | None, tuple[int, ...]]:
    """Project a mask onto ``bar_bits`` and simulate the trades (the shared seam).

    The signal→trade half of :func:`metrics_for_mask`, factored out so the
    walk-forward scorer can get a fold's realized trades (to run the P-06
    confinement guard over them) without duplicating the chain or re-simulating
    (CLAUDE.md §11 COMMON). Returns ``(trades, exit_indices,
    exit_reason_counts)`` — the walker's certified exit-bar indices (``None``
    for an uncertified window, DEC-SWEEP-EXIT-INDEX-FASTPATH-001) so the
    metrics tail can skip its per-mask ``exit_ts``→index dict build, plus the
    walker's per-reason exit counters in
    :data:`~brutex.engine.trade_sim.EXIT_REASONS` order
    (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B).
    """
    entry_signals = mask_to_entry_signals(bar_bits, required_mask)
    return simulate_trades_with_exit_counts(
        bars,
        entry_signals,
        direction=direction,
        tf_minutes=tf_minutes,
        exec_bars_1m=exec_bars_1m,
        exec_map=exec_map,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        reference_stamper=reference_stamper,
    )


def metrics_from_trades(
    bars: Sequence[CandleBar],
    trades: Sequence[ClosedTrade],
    *,
    start_equity_paisa: int,
    tf_minutes: int,
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None = None,
    exit_indices: Sequence[int] | None = None,
    exit_reason_counts: Sequence[int] | None = None,
    trailing_anchor: datetime | None = None,
) -> ResultMetrics:
    """Build the equity curve from already-simulated trades and compute metrics.

    The trade→metrics half of :func:`metrics_for_mask` — the ONE public COMMON
    seam (CLAUDE.md §11) for turning an already-simulated trade stream into a
    :class:`~brutex.metrics.compute.ResultMetrics`. Consumers: the walk-forward
    scorer (after its P-06 confinement guard), and the production sweep's wired
    per-mask metrics tail (:mod:`brutex.engine.sweep_run`,
    DEC-SWEEP-RUST-TRADESIM-WIRE-001 — the resident Rust kernel batch-simulates
    the trades, then each mask's stream flows through THIS identical tail, so
    the wired chain is byte-identical to :func:`metrics_for_mask`'s).

    **Flag-gated Rust metrics tail (DEC-SWEEP-RUST-METRICS-001; the default
    arm is the ONE constant**
    :data:`~brutex.engine.rust_metrics.SWEEP_RUST_METRICS_DEFAULT`**).**
    With the flag resolved ON, the drawdown family is computed by the fused
    Rust equity-fold + scan (``fno_math.sweep_drawdown_stats``) via
    :func:`_metrics_from_trades_via_kernel`, bit-identical to this Python
    tail inside its exactness envelope; ANY envelope/kernel miss falls back
    to the verbatim path below (same results, same error contracts — wheel
    state and flag state can never change behaviour). Because this seam is
    COMMON, the flag arms EVERY consumer together (sweep, walk-forward,
    validation, Stage 3) — safe precisely because the arms are bit-identical
    (golden + property pinned). OFF never touches the kernel machinery: one
    env read, then the pre-#1186 path byte-for-byte.

    ``exit_indices`` (DEC-SWEEP-EXIT-INDEX-FASTPATH-001 on the 1m grid) is an
    OPTIONAL fast-path hint — each trade's certified exit bar index WITHIN
    THE EXECUTION STREAM, as the trade simulators return alongside the
    trades. It is consumed ONLY by the kernel arm (validated per trade
    against ``exec_bars_1m``, translated to the owning decision bar via
    ``exec_map.decision_of``, demoted to the historical owning-bucket fold
    on any inconsistency — never to the Python oracle); the OFF arm ignores
    it entirely, so results are byte-identical with or without it on BOTH
    arms.

    ``exit_reason_counts`` (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B) is
    the trade stream's per-reason exit counters in
    :data:`~brutex.engine.trade_sim.EXIT_REASONS` order, as the counted
    simulators return alongside the trades. The stop-loss position
    (:data:`~brutex.engine.trade_sim.STOP_LOSS_REASON_INDEX` — extracted
    HERE, the engine layer, so the vocabulary position never duplicates
    into :mod:`brutex.metrics`) feeds the metrics' ``sl_hit_rate`` on BOTH
    the kernel arm and the Python tail. ``None`` (a legacy caller without
    reason attribution) yields the honest ``sl_hit_rate=None`` on a
    non-empty trade set — never a fabricated ``0.0``.

    ``exec_bars_1m`` is REQUIRED (DEC-FILL-EXEC-GRID-1M-001 §7.2): it is the
    ``time_in_market_pct`` denominator's bar count — ``holding_bars`` is a
    1-MINUTE count under the execution grid, so a decision-bar denominator
    would exceed 1 and be silently clamped. ``exec_map`` is the optional
    pre-built mapping (``None`` skips the exit-index fast path — the
    owning-bucket fold computes identical pairs).

    ``trailing_anchor`` (DEC-METRICS-TRAILING-WINDOW-001 Stage C) is the
    GLOBAL recency anchor — the LOADED bar range's end instant, one value
    per launch, threaded by the persistence spine
    (``sweep_run._iter_mask_metrics`` passes ``bars[-1].timestamp`` of the
    tuple's full loaded window). It reaches the metrics layer identically on
    BOTH the kernel arm and the Python tail (the recency pass is Python-side
    in either case), so results are byte-identical across arms. ``None``
    (the default — and deliberately the walk-forward fold scorer, whose
    ``bars`` are train/test SLICES: a slice-end anchor would violate the
    GLOBAL-anchor law) leaves every recency field the honest ``None``.
    """
    sl_exit_count = (
        None if exit_reason_counts is None else exit_reason_counts[STOP_LOSS_REASON_INDEX]
    )
    if sweep_rust_metrics_enabled():
        metrics = _metrics_from_trades_via_kernel(
            bars,
            trades,
            start_equity_paisa=start_equity_paisa,
            tf_minutes=tf_minutes,
            exec_bars_1m=exec_bars_1m,
            exec_map=exec_map,
            exit_indices=exit_indices,
            sl_exit_count=sl_exit_count,
            trailing_anchor=trailing_anchor,
        )
        if metrics is not None:
            return metrics
    equity = build_equity_curve(
        bars, trades, start_equity_paisa=start_equity_paisa, tf_minutes=tf_minutes
    )
    return compute_metrics(
        trades,
        equity,
        total_bars=len(exec_bars_1m),
        sl_exit_count=sl_exit_count,
        trailing_anchor=trailing_anchor,
    )


def _exit_pairs(
    bars: Sequence[CandleBar],
    trades: Sequence[ClosedTrade],
    exit_indices: Sequence[int] | None,
    *,
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None,
) -> list[tuple[int, int]] | None:
    """Map each trade to its ``(decision_exit_idx, pnl_paisa)`` kernel pair.

    The FAST PATH (DEC-SWEEP-EXIT-INDEX-FASTPATH-001 on the 1m grid): a
    supplied index stream — 1-MINUTE exit indices — is
    :func:`~brutex.engine.exit_index.exit_indices_consistent`-validated
    against the EXECUTION stream, then translated to each trade's OWNING
    decision bar via ``exec_map.decision_of`` (one O(1) array read per trade,
    design §3.8) — ``O(trades)`` per mask. An inconsistent stream (a
    producer/threading bug), or a missing ``exec_map``, is DEMOTED — one
    warn-once ``exit_indices_demoted`` — to the owning-bucket bisect fold
    over the decision labels (the exact :func:`build_equity_curve` law),
    NEVER to the Python oracle: the fold computes the identical pairs, so a
    demotion can never change results, only cost. Returns ``None`` iff a
    trade's ``exit_ts`` precedes the first decision label — the Python tail
    then raises ``build_equity_curve``'s exact ValueError (the
    ``_combo_error`` fingerprint feed), never a message duplicated here.
    """
    if exit_indices is not None:
        if exec_map is not None and exit_indices_consistent(exec_bars_1m, trades, exit_indices):
            decision_of = exec_map.decision_of
            pairs = [
                (decision_of[idx], trade.pnl_paisa)
                for idx, trade in zip(exit_indices, trades, strict=True)
            ]
            if all(idx >= 0 for idx, _ in pairs):
                return pairs
            # An exit 1m bar OWNED BY NO decision bar (decision_of == -1 —
            # an exec bar before the first decision label): out-of-window;
            # let the Python tail raise build_equity_curve's exact error.
            return None
        warn_fallback_once("exit_indices_demoted", n_trades=len(trades))
    ts_keys = [bar.timestamp for bar in bars]
    exits: list[tuple[int, int]] = []
    for trade in trades:
        idx = bisect_right(ts_keys, trade.exit_ts) - 1
        if idx < 0:
            return None
        exits.append((idx, trade.pnl_paisa))
    return exits


def _oracle_drawdown(
    n_bars: int, start_equity_paisa: int, exits: list[tuple[int, int]]
) -> tuple[int, float, int, float]:
    """Python-oracle drawdown 4-tuple for a kernel-shaped exit scatter.

    The burn-in self-check's reference arm: fold the exits into the
    materialised equity curve and run the REAL
    :func:`brutex.metrics.compute._drawdown_stats` scan over it — the exact
    oracle the kernel is specified against (``span_years`` is not read by
    the drawdown scan; ``1.0`` satisfies the curve invariant). ``O(N_bars)``
    — paid only for the first
    :data:`~brutex.engine.rust_metrics.SELF_CHECK_SAMPLES` kernel-served
    masks per process.
    """
    realized = [0] * n_bars
    for idx, pnl in exits:
        realized[idx] += pnl
    equity: list[int] = []
    running = start_equity_paisa
    for delta in realized:
        running += delta
        equity.append(running)
    curve = EquityCurve(
        equity_paisa=tuple(equity), start_equity_paisa=start_equity_paisa, span_years=1.0
    )
    return _drawdown_stats(curve)


def _self_checked_drawdown(
    n_bars: int, start_equity_paisa: int, exits: list[tuple[int, int]]
) -> tuple[int, float, int, float] | None:
    """Kernel drawdown with the burn-in bit-compare + autorevert latch.

    Runs the kernel seam; while the process still owes burn-in samples
    (:func:`~brutex.engine.rust_metrics.self_check_pending`), ALSO runs the
    Python oracle and bit-compares the two 4-tuples
    (:func:`~brutex.engine.rust_metrics.drawdown_compare_key` — ints exact,
    floats by ``struct.pack`` bit pattern). On a MISMATCH — a kernel
    numerical bug the domain validation cannot see — the kernel is latched
    OFF for the rest of the process
    (:func:`~brutex.engine.rust_metrics.disable_kernel`, one ERROR) and the
    ORACLE value ships (correctness first: a wrong kernel number is never
    persisted, not even the one that exposed the bug). A passed compare
    consumes one sample. Returns ``None`` iff the kernel seam itself
    declined (unavailable / raised / refused / latched) — the caller then
    serves the mask through the full Python tail.
    """
    kernel_stats = drawdown_stats_via_kernel(n_bars, start_equity_paisa, exits)
    if kernel_stats is None:
        return None
    if not self_check_pending():
        return kernel_stats
    oracle_stats = _oracle_drawdown(n_bars, start_equity_paisa, exits)
    if drawdown_compare_key(kernel_stats) != drawdown_compare_key(oracle_stats):
        disable_kernel(
            "self_check_mismatch",
            n_bars=n_bars,
            kernel=repr(kernel_stats),
            oracle=repr(oracle_stats),
        )
        return oracle_stats
    consume_self_check_sample()
    return kernel_stats


def _metrics_from_trades_via_kernel(
    bars: Sequence[CandleBar],
    trades: Sequence[ClosedTrade],
    *,
    start_equity_paisa: int,
    tf_minutes: int,
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None = None,
    exit_indices: Sequence[int] | None = None,
    sl_exit_count: int | None = None,
    trailing_anchor: datetime | None = None,
) -> ResultMetrics | None:
    """ON-arm metrics tail: fused Rust drawdown + curve-less compute_metrics.

    Mirrors :func:`build_equity_curve`'s semantics WITHOUT materialising the
    ``O(N_bars)`` equity tuple: each trade's exit bar index comes from the
    certified fast path or the SAME last-wins dict
    (:func:`_exit_pairs` — on a malformed duplicate-timestamp window the
    producers withhold indices, so both arms use the identical dict and
    cannot diverge), the SAME span expression computes ``span_years``, and
    the fused kernel folds ``start + Σ realized`` while scanning the
    drawdown family — bit-identical to ``_drawdown_stats`` inside the 2⁵²
    exactness envelope (see :mod:`brutex.engine.rust_metrics`), burn-in
    self-checked against it (:func:`_self_checked_drawdown`). A zero-trade
    mask short-circuits to the analytically-exact
    :data:`~brutex.engine.rust_metrics.FLAT_DRAWDOWN_STATS` with NO FFI call
    (the dominant share of the profiled fixture tail).

    Returns ``None`` whenever the Python tail must serve instead:

    * a CONTRACT input (empty ``bars``, non-positive ``start_equity_paisa``
      or ``tf_minutes``, an ``exit_ts`` that is no bar timestamp) — the
      Python tail then raises its OWN exact error, so messages/types are
      byte-identical on both arms and are never duplicated here;
    * the exactness envelope is exceeded, or the kernel is
      unavailable/raises/returns a refused result/is autorevert-latched
      (each logged once by :mod:`brutex.engine.rust_metrics`) — the Python
      tail then computes the bit-identical result.

    Honest envelope (Golden Rule #10): the win is the equity fold + drawdown
    scan + curve materialisation plus — on the certified fast path — the
    removed per-mask ``O(N_bars)`` dict build (``O(trades)`` instead); a
    demoted stream pays the historical dict build. All constant factors —
    the tail stays ``O(kept · N_bars)`` per tuple.
    """
    if not bars or start_equity_paisa <= 0:
        return None
    if exceeds_exact_equity_envelope(start_equity_paisa, trades):
        warn_fallback_once("equity_envelope_exceeded", n_trades=len(trades))
        return None
    span_seconds = (bars[-1].timestamp - bars[0].timestamp).total_seconds()
    span_years = span_seconds / (_MINUTES_PER_YEAR * 60.0)
    if span_years <= 0.0:
        span_years = tf_minutes / _MINUTES_PER_YEAR
    if span_years <= 0.0:
        # Only reachable for tf_minutes <= 0 — the Python tail raises the
        # exact EquityCurve span invariant error for it.
        return None
    if not trades:
        drawdown = FLAT_DRAWDOWN_STATS
    else:
        exits = _exit_pairs(
            bars, trades, exit_indices, exec_bars_1m=exec_bars_1m, exec_map=exec_map
        )
        if exits is None:
            # The Python tail raises build_equity_curve's exact ValueError
            # (whose message feeds _combo_error's fingerprint) — fall
            # through rather than reproduce the message here.
            return None
        kernel_stats = _self_checked_drawdown(len(bars), start_equity_paisa, exits)
        if kernel_stats is None:
            return None
        drawdown = kernel_stats
    return compute_metrics_from_scalars(
        trades,
        len(exec_bars_1m),
        start_equity_paisa=start_equity_paisa,
        span_years=span_years,
        drawdown=drawdown,
        sl_exit_count=sl_exit_count,
        trailing_anchor=trailing_anchor,
    )


def sweep_masks(
    bars: Sequence[CandleBar],
    masks: Sequence[int],
    *,
    direction: Side,
    tf_minutes: int,
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None = None,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    proximity: ProximityParams | None = None,
    prior_n: int = 3,
    min_hits: int = 0,
    roll_schedule: tuple[int, ...] | None = None,
    apply_1day_mask: bool = False,
    reference_stamper: TradeReferenceStamper | None = None,
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
    roll_schedule, apply_1day_mask
        The ADR-0012 futures pass-throughs into
        :func:`~brutex.engine.stage1a.build_bar_bits`
        (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001). Defaults (``None`` / ``False``)
        are the byte-identical spot path.

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
    if exec_map is None:
        # ONE map for the whole mask batch (mask-independent, design §1.5).
        exec_map = build_exec_grid_map(bars, exec_bars_1m, tf_minutes=tf_minutes)
    bar_bits = build_bar_bits(
        bars,
        proximity=proximity,
        prior_n=prior_n,
        roll_schedule=roll_schedule,
        apply_1day_mask=apply_1day_mask,
    )
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
                exec_bars_1m=exec_bars_1m,
                exec_map=exec_map,
                stop_loss_paisa=stop_loss_paisa,
                take_profit_paisa=take_profit_paisa,
                start_equity_paisa=start_equity_paisa,
                reference_stamper=reference_stamper,
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
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None = None,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    proximity: ProximityParams | None = None,
    prior_n: int = 3,
    min_hits: int = 0,
    roll_schedule: tuple[int, ...] | None = None,
    apply_1day_mask: bool = False,
    reference_stamper: TradeReferenceStamper | None = None,
) -> list[RankedCombo]:
    """Sweep a set of masks and return the **ranked leaderboard** — the top combos.

    :func:`sweep_masks` backtests every mask (skipping any pruned by the optional
    ``min_hits`` Stage-1B pre-filter), then :func:`brutex.ranking.rank_combos`
    applies the hard rejection gates + composite z-score and sorts. The head of the
    returned list is the top-performing combo for this
    ``(instrument, TF, direction, params)``. ``min_hits`` (default ``0`` = off) only
    skips masks that could never clear the trade-count gate, so it never changes the
    ranked result — see :func:`sweep_masks`. ``roll_schedule`` /
    ``apply_1day_mask`` are the ADR-0012 futures pass-throughs (defaults are the
    byte-identical spot path).
    ranked result — see :func:`sweep_masks`. ``reference_stamper`` is the pure
    conduit to :func:`sweep_masks` (DEC-VIX-STAMP-WIRE-001) — the ranked result is
    byte-identical with or without it (VIX is REFERENCE ONLY, never ranked on).
    """
    return rank_combos(
        sweep_masks(
            bars,
            masks,
            direction=direction,
            tf_minutes=tf_minutes,
            exec_bars_1m=exec_bars_1m,
            exec_map=exec_map,
            stop_loss_paisa=stop_loss_paisa,
            take_profit_paisa=take_profit_paisa,
            start_equity_paisa=start_equity_paisa,
            proximity=proximity,
            prior_n=prior_n,
            min_hits=min_hits,
            roll_schedule=roll_schedule,
            apply_1day_mask=apply_1day_mask,
            reference_stamper=reference_stamper,
        )
    )


def rank_sweep_over_bits(
    bars: Sequence[CandleBar],
    condition_bits: Sequence[int],
    k: int,
    *,
    direction: Side,
    tf_minutes: int,
    exec_bars_1m: Sequence[CandleBar],
    exec_map: ExecGridMap | None = None,
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    proximity: ProximityParams | None = None,
    prior_n: int = 3,
    max_combos: int = DEFAULT_MAX_COMBOS,
    min_hits: int = MIN_TRADES,
    roll_schedule: tuple[int, ...] | None = None,
    apply_1day_mask: bool = False,
    reference_stamper: TradeReferenceStamper | None = None,
) -> list[RankedCombo]:
    """Sweep + rank over the **K-of-the-chosen-condition-bits** mask space → top combos.

    The operator-facing *"find the top combo over MY chosen conditions"* entry: it
    enumerates exactly ``C(len(set(condition_bits)), k)`` masks — every ``k``-subset
    of the chosen conditions, via
    :func:`~brutex.engine.combo_mask.iter_masks_k_of_bits` (an ARBITRARY subset of
    the 60, e.g. only the pivot + Fibonacci bits — not the contiguous prefix that
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
        The condition bit positions (of the declared condition vocabulary) to
        brute-force over, each in ``[0, 64)``. Duplicates are ignored.
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
    roll_schedule, apply_1day_mask
        The ADR-0012 futures pass-throughs (DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001);
        defaults are the byte-identical spot path. See :func:`sweep_masks`.

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
        exec_bars_1m=exec_bars_1m,
        exec_map=exec_map,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        start_equity_paisa=start_equity_paisa,
        proximity=proximity,
        prior_n=prior_n,
        min_hits=min_hits,
        roll_schedule=roll_schedule,
        apply_1day_mask=apply_1day_mask,
        reference_stamper=reference_stamper,
    )


# ────────────────────────────────────────────────────────────────────────
# Walk-forward mode (DEC-WALK-FORWARD-OOS-001)
# ────────────────────────────────────────────────────────────────────────
#
# A single chronological OOS split (``validation.validate_out_of_sample``) lets a
# seasonal overfit hide in the one test block (audit Gap #4). Walk-forward mode
# scores a combo on EVERY held-out fold of a rolling
# :class:`~brutex.engine.stage1a.WalkForwardSchedule` (train 90d / test 5d /
# shift 1w by default) and aggregates the per-fold verdicts, so a combo that only
# worked in one season is exposed by the folds it fails.
#
# Two correctness pillars, both inherited from the proven OOS validator
# (``brutex.engine.validation``):
#
# 1. **No anchor artifact (the substantive causality guarantee).** ``bar_bits``
#    is built ONCE over the FULL stream, so every fold's first test day keeps its
#    real prior-day pivots/Fib/PDH-PDL (its prior day is the last train day).
#    Slicing the *trade simulation* at the IST day boundaries the schedule is
#    anchored to — never rebuilding per fold — avoids clearing those first-day
#    anchors. Each bar's truth-vector is causal by construction (Stage-1A reads
#    only bars ``<= N``), so a later fold's test window can never inform an earlier
#    one. This is the real no-look-ahead guarantee, and it is *proved* by the
#    counterfactual property test (perturbing bars after a fold's ``test_end_bar``
#    leaves that fold's metrics byte-identical).
# 2. **P-06 belt-and-suspenders (a guard, NOT the proof).** Two cheap runtime
#    re-checks via the named :class:`~brutex.feed.lookahead.LookAheadBarrier`,
#    honestly framed (Golden Rule #10): (a) :func:`verify_walk_forward_no_lookahead`
#    re-asserts the *schedule* keeps every training window before its test window
#    (``train_end_bar <= test_start_bar``) — a structural regression guard, since a
#    well-formed schedule satisfies it by construction; (b) :func:`_assert_trades_confined`
#    drives the barrier over each fold's *realized test trades*, proving none
#    references a bar outside its own fold window and that they are chronologically
#    ordered — a genuine guard on the scoring OUTPUT (it bites if a future slicing
#    regression let a trade escape its window). Neither replaces pillar 1; both
#    fail-closed (raise :class:`~brutex.feed.errors.LookAheadError`).
#
# Reproducibility (Golden Rule #4): :func:`_walk_forward_run_hash` folds the
# schedule's BLAKE3 hash into the run-hash, so a walk-forward result is tied to
# the exact fold layout it was scored on.

#: Walk-forward screening verdict threshold: a combo "holds up" out-of-sample
#: when it clears the 5 hard rejection gates on at least this fraction of the
#: held-out folds. A SCREENING heuristic (Golden Rule #10 — honest limits), not a
#: spec-locked gate: the spec's positive-Sharpe WFO pass-rate gate lives in
#: :mod:`brutex.metrics.wfo`; this is the engine-mode "robust across folds" signal
#: callers can tighten. Pinned here so it has one source of truth.
WALK_FORWARD_PASS_RATE_THRESHOLD: Final[float] = 0.6

#: Exit-mode-agnostic mode label fed to the walk-forward run-hash (distinguishes a
#: walk-forward run-identity from a single-shot ``evaluate_combo`` one).
_WALK_FORWARD_MODE_LABEL: Final[str] = "walk_forward"


@dataclass(frozen=True, slots=True)
class FoldResult:
    """One combo's metrics on a single walk-forward fold (train + held-out test).

    Attributes
    ----------
    fold_index
        The fold's zero-based position in the schedule.
    train_metrics, test_metrics
        The combo's metrics on the fold's training window and its held-out test
        window, both from the identical :func:`metrics_for_mask` core (directly
        comparable).
    test_gates_held
        ``True`` iff the test-window metrics clear the 5 hard rejection gates
        (:func:`brutex.ranking.passes_gates`) — the per-fold "passed" signal the
        aggregate pass-rate counts.
    """

    fold_index: int
    train_metrics: ResultMetrics
    test_metrics: ResultMetrics
    test_gates_held: bool


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    """A combo's full walk-forward outcome: per-fold metrics + the aggregate verdict.

    Attributes
    ----------
    schedule
        The fold schedule the combo was scored on (carries the ``schedule_hash``).
    fold_results
        One :class:`FoldResult` per fold, in chronological order.
    folds_passed
        Number of folds whose held-out test cleared the hard gates.
    pass_rate
        ``folds_passed / n_folds`` — the fraction of held-out folds that held up
        (in ``[0, 1]``; ``n_folds >= 1`` always).
    mean_test_sharpe, min_test_sharpe
        Mean and worst (minimum) of the defined per-fold test Sharpe ratios, or
        ``None`` if no fold produced a defined Sharpe. Full precision — never
        rounded (DEC-MONEY-2DP-001 is money-only). The mean is an unbiased
        summary only when ``step_days >= test_days`` (disjoint test windows); with
        ``step_days < test_days`` the test windows overlap and the mean
        double-counts the shared bars — a caller deliberately oversampling folds
        should prefer ``min_test_sharpe`` / ``pass_rate`` (Golden Rule #10).
    run_hash
        64-char BLAKE3 hex run-identity binding this combo + params to the fold
        schedule (:func:`_walk_forward_run_hash`).
    """

    schedule: WalkForwardSchedule
    fold_results: tuple[FoldResult, ...]
    folds_passed: int
    pass_rate: float
    mean_test_sharpe: float | None
    min_test_sharpe: float | None
    run_hash: str

    @property
    def n_folds(self) -> int:
        """Number of walk-forward folds scored (``>= 1``)."""
        return len(self.fold_results)

    @property
    def held_up(self) -> bool:
        """``True`` iff the held-out pass-rate clears the screening threshold.

        See :data:`WALK_FORWARD_PASS_RATE_THRESHOLD` for the honest-envelope
        caveat — this is a screening signal, not a spec-locked guarantee.
        """
        return self.pass_rate >= WALK_FORWARD_PASS_RATE_THRESHOLD


def verify_walk_forward_no_lookahead(schedule: WalkForwardSchedule) -> LookAheadReport:
    """Re-assert a fold schedule's train-before-test invariant via the P-06 barrier.

    The **structural** belt-and-suspenders guard (pillar 2a — see the module
    note). It drives the named **P-06**
    :class:`~brutex.feed.lookahead.LookAheadBarrier` across the folds: for each
    fold the barrier's cursor is set to the last bar the training side is
    permitted to use (``test_start_bar - 1``), then the fold's latest training bar
    (``train_end_bar - 1``) is accessed. A well-formed schedule builds contiguous
    windows (``train_end_bar == test_start_bar``), so the access lands exactly on
    the cursor and passes; a regression that let a training window reach into (or
    past) its own test window pushes the accessed index above the cursor and
    raises :class:`~brutex.feed.errors.LookAheadError`, which
    :func:`~brutex.feed.lookahead.verify_with_barrier` folds into the returned
    report (and logs one structured audit event — Golden Rule #8).

    **Honest scope (Golden Rule #10):** because the builder guarantees
    contiguity, this check is *structural* — it guards against a future regression
    in :func:`~brutex.engine.stage1a.build_walk_forward_schedule`, not against an
    indicator that peeks forward (that risk lives in the bit-matrix construction
    and is covered by pillar 1 + the counterfactual property test). A malformed
    hand-built schedule (a negative ``test_start_bar`` or folds whose cursors move
    backwards) is reported as a violation — ``passed=False`` — rather than raising
    an uncaught ``ValueError`` from the barrier, so the contract "the caller gets a
    pass/fail verdict, not a traceback" always holds.

    Parameters
    ----------
    schedule
        The :class:`~brutex.engine.stage1a.WalkForwardSchedule` to re-verify.

    Returns
    -------
    LookAheadReport
        ``passed=True`` with the barrier statistics when every fold's training
        window lies at or before its test window; ``passed=False`` carrying the
        violation string otherwise (including a malformed schedule).
    """
    barrier = LookAheadBarrier(record=True)

    def _walk() -> None:
        prev_cursor = -1
        for fold in schedule.folds:
            cursor = fold.test_start_bar - 1
            # A well-formed schedule has cursor >= 0 (test_start_bar >= 1) and
            # non-decreasing cursors (chronological folds). Map a malformed
            # schedule to a LookAheadError so verify_with_barrier reports
            # passed=False instead of leaking the barrier's ValueError.
            if cursor < 0 or cursor < prev_cursor or fold.train_end_bar - 1 < 0:
                raise LookAheadError(
                    f"malformed walk-forward schedule at fold {fold.fold_index}: "
                    f"test_start_bar={fold.test_start_bar}, train_end_bar={fold.train_end_bar}"
                )
            barrier.advance_to(cursor)
            barrier.access(fold.train_end_bar - 1)
            prev_cursor = cursor

    return verify_with_barrier(_walk, barrier)


def _assert_trades_confined(
    window_bars: Sequence[CandleBar],
    trades: Sequence[ClosedTrade],
) -> None:
    """Prove a fold's realized trades stay inside its window, via the P-06 barrier.

    The **scoring-output** guard (pillar 2b — see the module note). Drives a P-06
    :class:`~brutex.feed.lookahead.LookAheadBarrier` over the trades a fold's
    held-out test window produced: each trade's exit bar advances the cursor
    (forward-only — the single-position simulator emits chronological,
    non-overlapping trades) and its entry bar is then ``access``-ed, so the
    barrier proves every trade *entered no later than it exited* and *both bars lie
    inside this fold's window* (an out-of-window timestamp has no index → a
    violation). This bites if a future slicing regression let a trade reference a
    bar outside its fold — the genuine no-look-ahead guard on the scoring output
    (distinct from the structural schedule check). O(1) memory
    (``record=False``); O(trades) time.

    Raises
    ------
    LookAheadError
        If any trade's entry/exit bar is outside ``window_bars`` or the trades are
        not chronologically ordered by exit.
    """
    barrier = LookAheadBarrier(record=False)
    ts_to_index = build_ts_index(window_bars)
    for trade in trades:
        exit_index = ts_to_index.get(trade.exit_ts)
        entry_index = ts_to_index.get(trade.entry_ts)
        if exit_index is None or entry_index is None:
            raise LookAheadError(
                f"walk-forward fold: trade {trade.entry_ts!r}->{trade.exit_ts!r} "
                f"escapes its test window"
            )
        if exit_index < barrier.current_index:
            raise LookAheadError("walk-forward fold: trades not chronologically ordered by exit")
        barrier.advance_to(exit_index)
        barrier.access(entry_index)


def _walk_forward_run_hash(
    schedule_hash: str,
    *,
    required_mask: int,
    direction: Side,
    tf_minutes: int,
    stop_loss_paisa: int | None,
    take_profit_paisa: int | None,
    start_equity_paisa: int,
    proximity: ProximityParams | None,
    prior_n: int,
    roll_schedule: tuple[int, ...] | None = None,
    apply_1day_mask: bool = False,
) -> str:
    """Return the 64-char BLAKE3 run-hash for a walk-forward combo evaluation.

    Binds the combo (mask + direction + TF + exit/cost/proximity params) to the
    **fold schedule** (via ``schedule_hash``) so a result is reproducible only
    against the exact layout it was scored on (Golden Rule #4). Uses the same
    ``fno_math.blake3_hash`` primitive + the SAME canonical-JSON kwargs
    convention (``sort_keys`` / ``(",", ":")`` separators / ``ensure_ascii`` /
    ``allow_nan=False``) + ``\\x1f`` field join as
    :func:`brutex.strategies.combo.combo_hash` — deliberately PARALLEL, not
    shared: this payload is a fixed flat dict of int/str/None built right
    here, and routing it through the combo canonicaliser would bind this hash
    to that module's future-float encoding convention (the rejected
    consolidation — see DEC-RUN-IDENTITY-GUARD-PARITY-001; the equivalence is
    tripwire-pinned in the walk-forward unit suite). ``schedule_hash`` — the
    only injectable string token (the mode label is a module ``Final``, the
    JSON is ASCII-escaped) — is screened through the COMMON
    :func:`~brutex.strategies.combo.reject_unit_separator` guard; REJECT-ONLY,
    every currently-legal input's digest is byte-unchanged (golden-pinned).

    The ADR-0012 futures ``roll_schedule`` / ``apply_1day_mask`` enter the
    descriptor CONDITIONALLY (the ``canonical_trade_params`` precedent):
    ``bar_bits`` are a function of ``(bars, schedule)``, so a schedule-aware
    evaluation is a DIFFERENT computation and must hash apart — while a spot
    evaluation (both defaults) stays byte-identical to the pre-launch
    derivation (DEC-STAGE1A-ROLL-RESET-001).

    Raises
    ------
    ValueError
        If ``schedule_hash`` contains the ``\\x1f`` unit-separator byte, or a
        non-finite float is smuggled into the payload (``allow_nan=False``,
        unreachable for the typed int/str/None fields).
    """
    reject_unit_separator(
        "_walk_forward_run_hash",
        (("schedule_hash", schedule_hash),),
        error=ValueError,
    )
    prox = proximity if proximity is not None else ProximityParams()
    descriptor: dict[str, Any] = {
        "mask": required_mask,
        "direction": direction.name,
        "tf_minutes": tf_minutes,
        "stop_loss_paisa": stop_loss_paisa,
        "take_profit_paisa": take_profit_paisa,
        "start_equity_paisa": start_equity_paisa,
        "pivot_proximity_bps": prox.pivot_proximity_bps,
        "pdh_pdl_proximity_bps": prox.pdh_pdl_proximity_bps,
        "fib_proximity_bps": prox.fib_proximity_bps,
        "prior_n": prior_n,
    }
    if roll_schedule is not None or apply_1day_mask:
        descriptor["roll_schedule"] = list(roll_schedule or ())
        descriptor["apply_1day_mask"] = apply_1day_mask
    canonical = json.dumps(
        descriptor,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    payload = "\x1f".join([_WALK_FORWARD_MODE_LABEL, schedule_hash, canonical])
    return fno_math.blake3_hash(payload.encode("utf-8")).hex()


def _score_fold(
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    fold: WalkForwardFold,
    required_mask: int,
    *,
    direction: Side,
    tf_minutes: int,
    exec_bars_1m: Sequence[CandleBar],
    stop_loss_paisa: int | None,
    take_profit_paisa: int | None,
    start_equity_paisa: int,
    reference_stamper: TradeReferenceStamper | None = None,
) -> FoldResult:
    """Score one mask on one fold's train + held-out test windows.

    Slices the pre-built (full-stream) ``bar_bits`` + ``bars`` at the fold's bar
    boundaries and runs the SAME mask through the shared metrics core on each
    window — building once and slicing (never rebuilding per fold) preserves the
    test window's first-day anchors (see the module note). The held-out test
    window's realized trades are routed through the P-06
    :func:`_assert_trades_confined` guard before metrics, so a slicing regression
    that let a trade escape the fold raises rather than scoring silently.

    ``train_metrics`` is the in-sample side of the standard walk-forward
    in-sample-vs-out-of-sample table (a first-class deliverable a caller inspects,
    not discarded); the aggregate verdict uses the held-out ``test`` side.
    """
    train_bars = bars[fold.train_start_bar : fold.train_end_bar]
    # Dual-stream window cut (DEC-FILL-EXEC-GRID-1M-001, design §7.2/Q3): ONE
    # boundary per fold window applied to BOTH streams — the exec sub-window
    # runs from the window's first decision label through the end of its last
    # decision bar's IST session (loss-free: the slide law is session-bounded).
    train_lo, train_hi = exec_window_slice(train_bars, exec_bars_1m)
    train_exec = exec_bars_1m[train_lo:train_hi]
    train_metrics = metrics_for_mask(
        train_bars,
        bar_bits[fold.train_start_bar : fold.train_end_bar],
        required_mask,
        direction=direction,
        tf_minutes=tf_minutes,
        exec_bars_1m=train_exec,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        start_equity_paisa=start_equity_paisa,
        reference_stamper=reference_stamper,
    )
    test_bars = bars[fold.test_start_bar : fold.test_end_bar]
    test_lo, test_hi = exec_window_slice(test_bars, exec_bars_1m)
    test_exec = exec_bars_1m[test_lo:test_hi]
    test_map = build_exec_grid_map(test_bars, test_exec, tf_minutes=tf_minutes)
    test_trades, test_exit_indices, test_exit_counts = _simulate_window(
        test_bars,
        bar_bits[fold.test_start_bar : fold.test_end_bar],
        required_mask,
        direction=direction,
        tf_minutes=tf_minutes,
        exec_bars_1m=test_exec,
        exec_map=test_map,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        reference_stamper=reference_stamper,
    )
    # The P-06 confinement guard runs over the fold's EXECUTION sub-window —
    # every fill (entry AND exit) is a stored 1m bar of exactly this window.
    _assert_trades_confined(test_exec, test_trades)
    test_metrics = metrics_from_trades(
        test_bars,
        test_trades,
        start_equity_paisa=start_equity_paisa,
        tf_minutes=tf_minutes,
        exec_bars_1m=test_exec,
        exec_map=test_map,
        exit_indices=test_exit_indices,
        exit_reason_counts=test_exit_counts,
    )
    return FoldResult(
        fold_index=fold.fold_index,
        train_metrics=train_metrics,
        test_metrics=test_metrics,
        test_gates_held=passes_gates(test_metrics),
    )


def _aggregate_folds(
    schedule: WalkForwardSchedule,
    fold_results: list[FoldResult],
    run_hash: str,
) -> WalkForwardResult:
    """Fold the per-fold results into the aggregate :class:`WalkForwardResult`.

    Counts held-out gate passes for the pass-rate, and summarises the defined
    per-fold test Sharpe ratios (mean + worst). ``schedule.n_folds >= 1`` always,
    so the pass-rate denominator is never zero.
    """
    folds_passed = sum(1 for fr in fold_results if fr.test_gates_held)
    test_sharpes = [
        fr.test_metrics.sharpe_ratio
        for fr in fold_results
        if fr.test_metrics.sharpe_ratio is not None
    ]
    mean_test_sharpe = sum(test_sharpes) / len(test_sharpes) if test_sharpes else None
    min_test_sharpe = min(test_sharpes) if test_sharpes else None
    return WalkForwardResult(
        schedule=schedule,
        fold_results=tuple(fold_results),
        folds_passed=folds_passed,
        pass_rate=folds_passed / schedule.n_folds,
        mean_test_sharpe=mean_test_sharpe,
        min_test_sharpe=min_test_sharpe,
        run_hash=run_hash,
    )


def evaluate_combo_walk_forward(
    bars: Sequence[CandleBar],
    required_mask: int,
    *,
    direction: Side,
    tf_minutes: int,
    exec_bars_1m: Sequence[CandleBar],
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    proximity: ProximityParams | None = None,
    prior_n: int = 3,
    train_days: int = DEFAULT_WF_TRAIN_DAYS,
    test_days: int = DEFAULT_WF_TEST_DAYS,
    step_days: int = DEFAULT_WF_STEP_DAYS,
    roll_schedule: tuple[int, ...] | None = None,
    apply_1day_mask: bool = False,
    reference_stamper: TradeReferenceStamper | None = None,
) -> WalkForwardResult:
    """Backtest ONE combo across a rolling walk-forward schedule → its WF outcome.

    Builds the NSE-calendar-anchored fold schedule
    (:func:`~brutex.engine.stage1a.build_walk_forward_schedule`), re-proves its
    no-look-ahead invariant through the P-06 barrier
    (:func:`verify_walk_forward_no_lookahead`), then scores the combo on every
    fold's train + held-out test window from a SINGLE full-stream ``bar_bits``
    build (correct first-day anchors everywhere), and aggregates the per-fold
    verdicts. The result's ``run_hash`` binds the combo to the exact fold layout.

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)`` spanning
        at least ``train_days + test_days`` distinct IST trading days (non-empty).
    required_mask
        The u64 strategy combo to walk-forward (``0`` enters on every bar).
    direction, tf_minutes, stop_loss_paisa, take_profit_paisa, start_equity_paisa, proximity, prior_n
        As :func:`evaluate_combo`.
    train_days, test_days, step_days
        Rolling-window sizes in trading days (see
        :func:`~brutex.engine.stage1a.build_walk_forward_schedule`).
    roll_schedule, apply_1day_mask
        The ADR-0012 futures pass-throughs into the single full-stream
        ``bar_bits`` build (review round-2 finding 2); both enter the
        run-hash CONDITIONALLY, so spot results stay byte-identical.

    Returns
    -------
    WalkForwardResult
        The per-fold metrics + the aggregate held-out pass-rate, Sharpe summary,
        and reproducible run-hash.

    Raises
    ------
    ValueError
        If ``bars`` is empty or there are too few trading days for one fold.
    LookAheadError
        If the generated schedule fails the P-06 no-look-ahead re-check (a
        regression guard — a well-formed schedule never trips it).
    """
    if not bars:
        raise ValueError("evaluate_combo_walk_forward: bars must be non-empty")
    schedule = build_walk_forward_schedule(
        bars, train_days=train_days, test_days=test_days, step_days=step_days
    )
    report = verify_walk_forward_no_lookahead(schedule)
    if not report.passed:
        raise LookAheadError(f"walk-forward schedule failed P-06 re-check: {report.violation}")
    bar_bits = build_bar_bits(
        bars,
        proximity=proximity,
        prior_n=prior_n,
        roll_schedule=roll_schedule,
        apply_1day_mask=apply_1day_mask,
    )
    fold_results = [
        _score_fold(
            bars,
            bar_bits,
            fold,
            required_mask,
            direction=direction,
            tf_minutes=tf_minutes,
            exec_bars_1m=exec_bars_1m,
            stop_loss_paisa=stop_loss_paisa,
            take_profit_paisa=take_profit_paisa,
            start_equity_paisa=start_equity_paisa,
            reference_stamper=reference_stamper,
        )
        for fold in schedule.folds
    ]
    run_hash = _walk_forward_run_hash(
        schedule.schedule_hash,
        required_mask=required_mask,
        direction=direction,
        tf_minutes=tf_minutes,
        stop_loss_paisa=stop_loss_paisa,
        take_profit_paisa=take_profit_paisa,
        start_equity_paisa=start_equity_paisa,
        proximity=proximity,
        prior_n=prior_n,
        roll_schedule=roll_schedule,
        apply_1day_mask=apply_1day_mask,
    )
    return _aggregate_folds(schedule, fold_results, run_hash)


def walk_forward_sweep(
    bars: Sequence[CandleBar],
    masks: Sequence[int],
    *,
    direction: Side,
    tf_minutes: int,
    exec_bars_1m: Sequence[CandleBar],
    stop_loss_paisa: int | None = None,
    take_profit_paisa: int | None = None,
    start_equity_paisa: int = DEFAULT_START_EQUITY_PAISA,
    proximity: ProximityParams | None = None,
    prior_n: int = 3,
    train_days: int = DEFAULT_WF_TRAIN_DAYS,
    test_days: int = DEFAULT_WF_TEST_DAYS,
    step_days: int = DEFAULT_WF_STEP_DAYS,
    roll_schedule: tuple[int, ...] | None = None,
    apply_1day_mask: bool = False,
    reference_stamper: TradeReferenceStamper | None = None,
) -> list[tuple[int, WalkForwardResult]]:
    """Walk-forward MANY masks over the same bars → ``(mask, WalkForwardResult)`` pairs.

    Builds the fold schedule and the full-stream ``bar_bits`` matrix **once** (the
    per-bar truth-vectors and the fold layout are identical for every mask — only
    the AND-mask differs), re-proves the schedule's no-look-ahead invariant once,
    then scores each mask across all folds. O(1) per (mask, fold, bar) beyond the
    shared one-time build.

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)`` (non-empty).
    masks
        The u64 strategy masks to walk-forward.
    direction, tf_minutes, stop_loss_paisa, take_profit_paisa, start_equity_paisa, proximity, prior_n
        As :func:`evaluate_combo`.
    train_days, test_days, step_days
        Rolling-window sizes in trading days.
    roll_schedule, apply_1day_mask
        The ADR-0012 futures pass-throughs into the ONE shared ``bar_bits``
        build (review round-2 finding 2); conditional run-hash entry, spot
        byte-identical.

    Returns
    -------
    list[tuple[int, WalkForwardResult]]
        One ``(mask, WalkForwardResult)`` pair per input mask, in input order.

    Raises
    ------
    ValueError
        If ``bars`` is empty or there are too few trading days for one fold.
    LookAheadError
        If the generated schedule fails the P-06 no-look-ahead re-check.
    """
    if not bars:
        raise ValueError("walk_forward_sweep: bars must be non-empty")
    schedule = build_walk_forward_schedule(
        bars, train_days=train_days, test_days=test_days, step_days=step_days
    )
    report = verify_walk_forward_no_lookahead(schedule)
    if not report.passed:
        raise LookAheadError(f"walk-forward schedule failed P-06 re-check: {report.violation}")
    bar_bits = build_bar_bits(
        bars,
        proximity=proximity,
        prior_n=prior_n,
        roll_schedule=roll_schedule,
        apply_1day_mask=apply_1day_mask,
    )
    results: list[tuple[int, WalkForwardResult]] = []
    for mask in masks:
        fold_results = [
            _score_fold(
                bars,
                bar_bits,
                fold,
                mask,
                direction=direction,
                tf_minutes=tf_minutes,
                exec_bars_1m=exec_bars_1m,
                stop_loss_paisa=stop_loss_paisa,
                take_profit_paisa=take_profit_paisa,
                start_equity_paisa=start_equity_paisa,
                reference_stamper=reference_stamper,
            )
            for fold in schedule.folds
        ]
        run_hash = _walk_forward_run_hash(
            schedule.schedule_hash,
            required_mask=mask,
            direction=direction,
            tf_minutes=tf_minutes,
            stop_loss_paisa=stop_loss_paisa,
            take_profit_paisa=take_profit_paisa,
            start_equity_paisa=start_equity_paisa,
            proximity=proximity,
            prior_n=prior_n,
            roll_schedule=roll_schedule,
            apply_1day_mask=apply_1day_mask,
        )
        results.append((mask, _aggregate_folds(schedule, fold_results, run_hash)))
    return results


__all__ = [
    "DEFAULT_MAX_COMBOS",
    "DEFAULT_START_EQUITY_PAISA",
    "WALK_FORWARD_PASS_RATE_THRESHOLD",
    "FoldResult",
    "WalkForwardResult",
    "build_equity_curve",
    "evaluate_combo",
    "evaluate_combo_walk_forward",
    "metrics_for_mask",
    "metrics_from_trades",
    "rank_sweep",
    "rank_sweep_over_bits",
    "sweep_masks",
    "verify_walk_forward_no_lookahead",
    "walk_forward_sweep",
]
