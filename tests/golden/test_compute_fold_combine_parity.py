"""Byte-parity golden for the Slice-0 fold/combine decomposition of compute.py.

DEC-SWEEP-COMPUTE-FOLD-DECOMP-001 (Slice 0 of the Rust sweep-core
conversion — design pack ``docs/design/sweep_core/`` on branch
``claude/rust-sweep-core``). The metric body ``_compute_metrics_impl``
became the pure composition ``combine_metrics_from_folds(_fold_trades(...))``
with ZERO numeric change: every moved expression is token-for-token the
pre-decomposition body. This suite is the SHIP CONDITION's proof — its
oracle is BODY-level-independent ONLY (see the independence scope note in
layer 1) — in three layers:

1. **Frozen oracle.** :func:`_oracle_metrics_pre_slice0` is the VERBATIM
   pre-Slice-0 ``_compute_metrics_impl`` body (origin/main ``95955a71``),
   inlined here so it can never drift with the refactor. Since
   DEC-METRICS-LOSSRATE-FIRSTCLASS-001 / DEC-METRICS-KELLY-GROWTH-001 the
   oracle additionally carries the ADDITIVE loss/growth block (three new
   required ``ResultMetrics`` fields) re-stated INLINE — clearly marked in
   the body; every pre-existing expression stays the verbatim frozen text,
   and the parity compare now also pins the additive expressions. It reuses ONLY
   helpers this slice does not touch (``_empty_metrics`` /
   ``_consecutive_runs`` / ``_accumulate_moments`` / ``_percentile`` /
   ``_safe_ratio`` / ``_skewness`` / ``_excess_kurtosis`` /
   ``_CAGR_CAP_PCT`` — ``git diff`` shows them byte-unchanged).
   INDEPENDENCE SCOPE (Verifier B Slice-0 re-pin, 2026-07-10): because
   those leaf helpers are IMPORTED from live ``compute.py``, the oracle
   is independent at the BODY level only — a defect INSIDE a shared leaf
   hits both arms identically and is invisible to every oracle
   comparison in this file and in the C23 differential gate. Measured
   blindness example: an injected Pébay-m3 delta-sign mutant in
   ``_accumulate_moments`` passed the differential 4/4 and every
   byte-parity comparison here, killed only by the ``float.fromhex``
   exact-value pins. Correct AND disclosed for Slice-0 (the helpers were
   not moved; these gates verify the SPLIT) — but Slice 2's Rust arm
   must bind to a GENUINELY independent oracle (design §6 mandate on
   ``claude/rust-sweep-core``).
2. **Bit-compare harness.** :func:`_pack_metrics` encodes every
   :class:`ResultMetrics` field via ``struct.pack("<d", ...)`` for floats
   (signed zeros and every ULP distinguished — the harness proves its own
   teeth in :class:`TestPackHarnessTeeth`) and exact ints elsewhere.
3. **Hostile corpus.** Zero-trade · one-trade (win/loss/scratch) ·
   all-loss · all-win · negative-expectancy floor-division (``-7 // 2``
   class) · signed-zero / symmetric returns · zero-variance ·
   denormal-adjacent magnitudes (tiny returns + subnormal drawdown
   percentages — truly subnormal MOMENTS are unreachable from int64-paisa
   inputs, stated honestly) · CAGR-overflow span · total-ruin clamp ·
   dense 1500-trade masks · real-shaped NIFTY-scale fixtures — each
   asserted bit-identical old-vs-new through BOTH public entries and the
   private composition root.

Plus exact-value pins (:class:`TestFoldExactValuePins` /
:class:`TestCombineExactValuePins`) for every moved expression, with
float literals embedded as ``float.fromhex`` — mutation teeth for the
fold/combine split itself.
"""

from __future__ import annotations

import math
import random
import struct
from dataclasses import fields, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from brutex.metrics.compute import (
    _CAGR_CAP_PCT,  # pyright: ignore[reportPrivateUsage]
    ResultMetrics,
    TradeFolds,
    _accumulate_moments,  # pyright: ignore[reportPrivateUsage]
    _compute_metrics_impl,  # pyright: ignore[reportPrivateUsage]
    _consecutive_runs,  # pyright: ignore[reportPrivateUsage]
    _drawdown_stats,  # pyright: ignore[reportPrivateUsage]
    _empty_metrics,  # pyright: ignore[reportPrivateUsage]
    _excess_kurtosis,  # pyright: ignore[reportPrivateUsage]
    _fold_trades,  # pyright: ignore[reportPrivateUsage]
    _percentile,  # pyright: ignore[reportPrivateUsage]
    _safe_ratio,  # pyright: ignore[reportPrivateUsage]
    _skewness,  # pyright: ignore[reportPrivateUsage]
    combine_metrics_from_folds,
    compute_metrics,
    compute_metrics_from_scalars,
)
from brutex.metrics.types import ClosedTrade, EquityCurve
from brutex.strategies.types import Side

if TYPE_CHECKING:
    from collections.abc import Sequence

_TS = datetime(2024, 1, 2, 4, 0, tzinfo=UTC)


def _trade(pnl: int, mfe: int = 0, mae: int = 0, hold: int = 1) -> ClosedTrade:
    """Build a minimal :class:`ClosedTrade` for the corpus."""
    return ClosedTrade(
        pnl_paisa=pnl,
        mfe_paisa=mfe,
        mae_paisa=mae,
        holding_bars=hold,
        entry_ts=_TS,
        exit_ts=_TS,
        direction=Side.BUY,
    )


def _oracle_metrics_pre_slice0(
    trades: Sequence[ClosedTrade],
    total_bars: int,
    benchmark_return_pct: float,
    *,
    start_equity_paisa: int,
    span_years: float,
    drawdown: tuple[int, float, int, float],
) -> ResultMetrics:
    """The VERBATIM pre-Slice-0 ``_compute_metrics_impl`` body (frozen oracle).

    Copied token-for-token from ``origin/main`` commit ``95955a71``
    (``brutex/metrics/compute.py``), minus its docstring. Any numeric
    drift in the fold/combine decomposition fails the bit-compare below.
    """
    if not trades:
        return _empty_metrics(drawdown)

    # ── Per-trade aggregates (single pass) ───────────────────────────
    net_pnl = 0
    gross_profit = 0
    gross_loss_abs = 0  # accumulated as a non-negative magnitude
    winners = 0
    losers = 0
    sum_mfe = 0
    sum_mae = 0
    sum_holding = 0
    max_holding = 0
    largest_loss = 0  # most-negative pnl, stored signed
    for trade in trades:
        net_pnl += trade.pnl_paisa
        sum_mfe += trade.mfe_paisa
        sum_mae += trade.mae_paisa
        sum_holding += trade.holding_bars
        max_holding = max(max_holding, trade.holding_bars)
        if trade.is_win:
            winners += 1
            gross_profit += trade.pnl_paisa
        elif trade.is_loss:
            losers += 1
            gross_loss_abs += -trade.pnl_paisa
            largest_loss = min(largest_loss, trade.pnl_paisa)

    n_trades = len(trades)

    # ── Return metrics (spec lines 532-537) ──────────────────────────
    total_return_pct = 100.0 * net_pnl / start_equity_paisa
    end_equity = start_equity_paisa + net_pnl
    if end_equity < 1:
        cagr_pct = -100.0
    else:
        growth = end_equity / start_equity_paisa
        try:
            annualised_growth = growth ** (1.0 / span_years)
        except OverflowError:
            annualised_growth = math.inf
        uncapped_cagr = min(100.0 * (annualised_growth - 1.0), _CAGR_CAP_PCT)
        cagr_pct = max(-100.0, uncapped_cagr)

    # ── Drawdown (spec lines 547-555) ────────────────────────────────
    max_dd_paisa, max_dd_pct, max_dd_bars, ulcer = drawdown
    max_losses, max_wins = _consecutive_runs(trades)
    recovery_factor = _safe_ratio(float(net_pnl), float(max_dd_paisa))

    # ── Trade quality (spec lines 557-568) ───────────────────────────
    win_rate = winners / n_trades  # fraction, per spec line 561
    profit_factor = _safe_ratio(float(gross_profit), float(gross_loss_abs))
    avg_win = gross_profit // winners if winners else 0
    avg_loss_abs = gross_loss_abs // losers if losers else 0
    win_loss_ratio = _safe_ratio(float(avg_win), float(avg_loss_abs))
    expectancy = net_pnl // n_trades
    avg_mfe = sum_mfe // n_trades
    avg_mae = sum_mae // n_trades

    # ── Trade timing (spec lines 570-573) ────────────────────────────
    avg_holding = sum_holding / n_trades
    time_in_market_pct = min(100.0, 100.0 * sum_holding / total_bars)

    # ── Risk-adjusted return (spec lines 539-545) ────────────────────
    returns = [t.pnl_paisa / start_equity_paisa for t in trades]
    moments = _accumulate_moments(returns)
    std_dev = moments.std_dev
    sharpe = None if std_dev is None or std_dev == 0.0 else moments.mean / std_dev
    downside = [r for r in returns if r < 0.0]
    downside_dev = math.sqrt(sum(r * r for r in downside) / n_trades)
    sortino = None if downside_dev == 0.0 else moments.mean / downside_dev
    calmar = _safe_ratio(cagr_pct, max_dd_pct)
    mar = calmar
    benchmark_per_trade = (benchmark_return_pct / 100.0) / n_trades
    information = (
        None
        if std_dev is None or std_dev == 0.0
        else (moments.mean - benchmark_per_trade) / std_dev
    )
    omega = _safe_ratio(float(gross_profit), float(gross_loss_abs))

    # ── Distribution robustness (spec lines 575-579) ─────────────────
    sorted_returns = sorted(returns)
    p95 = _percentile(sorted_returns, 95.0)
    p5 = _percentile(sorted_returns, 5.0)
    tail_ratio = _safe_ratio(p95, abs(p5))
    skewness = _skewness(moments)
    kurtosis = _excess_kurtosis(moments)

    # ── ADDITIVE loss/growth block (NOT part of the frozen pre-Slice-0
    # body — DEC-METRICS-LOSSRATE-FIRSTCLASS-001 / DEC-METRICS-KELLY-GROWTH-001):
    # ResultMetrics gained three required fields, so the oracle re-states the
    # additive expressions INLINE (never imported from compute.py) — the
    # parity compare therefore ALSO pins the new expressions at body level,
    # while every pre-existing expression above stays the verbatim 95955a71
    # body.
    loss_rate = losers / n_trades
    kelly_fraction: float | None = None
    log_growth_at_half_kelly: float | None = None
    if winners != 0 and losers != 0:
        avg_win_f = gross_profit / winners
        avg_loss_f = gross_loss_abs / losers
        if avg_win_f != 0.0 and avg_loss_f != 0.0:
            b = avg_win_f / avg_loss_f
            p_win = winners / n_trades
            q_loss = 1.0 - p_win
            kelly_fraction = p_win - q_loss / b
            half = kelly_fraction / 2.0
            arg_win = 1.0 + half * b
            arg_loss = 1.0 - half
            if arg_win > 0.0 and arg_loss > 0.0:
                log_growth_at_half_kelly = p_win * math.log(arg_win) + q_loss * math.log(arg_loss)

    return ResultMetrics(
        net_pnl_paisa=net_pnl,
        total_return_pct=total_return_pct,
        cagr_pct=cagr_pct,
        gross_profit_paisa=gross_profit,
        gross_loss_paisa=gross_loss_abs,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        mar_ratio=mar,
        information_ratio=information,
        omega_ratio=omega,
        max_drawdown_pct=max_dd_pct,
        max_drawdown_paisa=max_dd_paisa,
        max_drawdown_duration_bars=max_dd_bars,
        recovery_factor=recovery_factor,
        ulcer_index=ulcer,
        largest_losing_trade_paisa=largest_loss,
        max_consecutive_losses=max_losses,
        max_consecutive_wins=max_wins,
        total_trades=n_trades,
        winning_trades=winners,
        losing_trades=losers,
        win_rate=win_rate,
        loss_rate=loss_rate,
        profit_factor=profit_factor,
        avg_winning_trade_paisa=avg_win,
        avg_losing_trade_paisa=avg_loss_abs,
        win_loss_ratio=win_loss_ratio,
        expectancy_paisa=expectancy,
        avg_mfe_paisa=avg_mfe,
        avg_mae_paisa=avg_mae,
        avg_holding_time_bars=avg_holding,
        max_holding_time_bars=max_holding,
        time_in_market_pct=time_in_market_pct,
        tail_ratio=tail_ratio,
        skewness=skewness,
        kurtosis=kurtosis,
        returns_std_dev=std_dev,
        kelly_fraction=kelly_fraction,
        log_growth_at_half_kelly=log_growth_at_half_kelly,
    )


def _pack_metrics(metrics: ResultMetrics) -> bytes:
    """Bit-exact byte encoding of every :class:`ResultMetrics` field.

    Floats via ``struct.pack("<d", ...)`` (signed zeros + every ULP
    distinguished), ints exact, ``None`` a distinct tag — so equality of
    the packed bytes IS bit-identity of the row.
    """
    out = bytearray()
    for field in fields(metrics):
        value = getattr(metrics, field.name)
        if value is None:
            out += b"N"
        elif isinstance(value, float):
            out += b"f" + struct.pack("<d", value)
        else:
            out += b"i" + repr(value).encode("ascii") + b";"
    return bytes(out)


def _dense_trades(count: int, seed: int) -> list[ClosedTrade]:
    """Seeded pseudo-random dense trade list (the 1400+-trade mask class)."""
    rng = random.Random(seed)
    return [
        _trade(
            rng.randint(-5000, 5000),
            rng.randint(0, 8000),
            rng.randint(0, 8000),
            rng.randint(1, 375),
        )
        for _ in range(count)
    ]


#: The hostile corpus: (name, trades, total_bars, benchmark, start_equity,
#: span_years, drawdown 4-tuple).
_CorpusCase = tuple[str, list[ClosedTrade], int, float, int, float, tuple[int, float, int, float]]

_CORPUS: list[_CorpusCase] = [
    ("zero_trade", [], 100, 0.0, 1_000_000, 1.0, (0, 0.0, 0, 0.0)),
    ("one_trade_win", [_trade(500, 600, 100, 3)], 10, 5.0, 1_000_000, 0.5, (100, 0.01, 2, 0.005)),
    ("one_trade_loss", [_trade(-500, 10, 600, 2)], 10, 5.0, 1_000_000, 0.5, (600, 0.06, 5, 0.03)),
    ("one_trade_scratch", [_trade(0, 50, 50, 1)], 10, 5.0, 1_000_000, 0.5, (50, 0.005, 1, 0.001)),
    (
        "all_loss",
        [_trade(-100, 0, 200, 2), _trade(-250, 5, 300, 3), _trade(-1, 0, 1, 1)],
        50,
        12.0,
        500_000,
        2.0,
        (351, 0.07, 6, 0.04),
    ),
    (
        "all_win",
        [_trade(100, 200, 0, 1), _trade(250, 300, 5, 2), _trade(1, 1, 0, 1)],
        50,
        12.0,
        500_000,
        2.0,
        (0, 0.0, 0, 0.0),
    ),
    # net -7 over 2 trades → expectancy -7 // 2 == -4 (Python floor, not
    # C trunc -3) — the floor-division class the Rust port must mirror.
    ("negative_expectancy_floor", [_trade(-3), _trade(-4)], 10, 0.0, 999, 0.001, (7, 0.7, 2, 0.3)),
    # gross_loss 7 // 3 losers == 2; net -5 // 4 == -2 (floor again).
    (
        "floor_div_avg_loss",
        [_trade(-3), _trade(-3), _trade(-1), _trade(2)],
        10,
        0.0,
        1000,
        1.0,
        (7, 0.7, 3, 0.3),
    ),
    # Symmetric ± returns: mean cancels exactly; downside/percentile arms
    # see genuine negative + zero returns (signed-zero class — the packed
    # compare distinguishes -0.0 from 0.0 wherever one arises).
    (
        "signed_zero_symmetric",
        [_trade(250), _trade(-250), _trade(0)],
        30,
        0.0,
        1_000_000,
        1.0,
        (250, 0.025, 1, 0.01),
    ),
    # Constant returns → zero variance → sharpe/information None arm.
    ("zero_variance", [_trade(7), _trade(7), _trade(7)], 9, 100.0, 100, 100.0, (0, 0.0, 0, 0.0)),
    # Scratch-only: no winners, no losers, empty downside (int-0 Σr² arm).
    ("scratch_only", [_trade(0), _trade(0)], 5, 1.0, 100, 1.0, (0, 0.0, 0, 0.0)),
    # Denormal-adjacent: 1-paisa pnl over a 2**62 base (returns ~2e-19 —
    # the smallest int64-paisa-reachable magnitudes; truly subnormal
    # moments are unreachable from int inputs) + SUBNORMAL drawdown
    # percentages exercising denormal division in calmar/mar.
    (
        "denormal_adjacent",
        [_trade(1), _trade(-1), _trade(1)],
        1,
        1e-300,
        2**62,
        1e-9,
        (1, 5e-324, 1, 2.2e-308),
    ),
    # growth ** (1/span) overflows → the OverflowError → inf → cap arm.
    ("cagr_overflow_span", [_trade(500)], 5, 0.0, 1000, 1e-300, (1, 0.001, 1, 0.0005)),
    # Total ruin: end_equity < 1 → the -100% clamp arm.
    ("total_ruin", [_trade(-999), _trade(-1)], 5, -10.0, 1000, 2.0, (1000, 1.0, 5, 0.5)),
    # Huge magnitudes near int64 (i128-accumulation class for the port).
    (
        "huge_magnitudes",
        [_trade(-(10**13), 0, 10**13, 375)] * 4 + [_trade(10**13, 10**13, 0, 375)],
        1500,
        -50.0,
        10**6,
        6.0,
        (10**13, 4000.0, 1500, 250.0),
    ),
    (
        "dense_1500",
        _dense_trades(1500, 42),
        100_000,
        62.4,
        10_00_00_000,
        5.5,
        (250_000, 12.5, 900, 3.2),
    ),
    (
        "dense_1401",
        _dense_trades(1401, 7),
        375 * 1400,
        18.0,
        10_00_00_000,
        6.0,
        (99_999, 9.9, 375, 1.1),
    ),
    # Real-shaped: NIFTY-scale paisa, ₹10L start, 5.5y span, real-ish dd.
    (
        "real_shaped_nifty",
        [
            _trade(12_550, 15_000, 2_500, 47),
            _trade(-8_275, 1_200, 9_800, 112),
            _trade(21_005, 22_000, 500, 9),
            _trade(-3_450, 4_000, 5_600, 63),
            _trade(0, 900, 900, 15),
            _trade(9_999, 10_100, 1, 375),
        ],
        500_000,
        62.4,
        10_00_00_000,
        5.5,
        (48_750, 4.875, 512, 1.62),
    ),
]

_CORPUS_IDS = [case[0] for case in _CORPUS]


class TestPackHarnessTeeth:
    """The bit-compare harness itself must have teeth."""

    def test_pack_distinguishes_signed_zero(self) -> None:
        base = _empty_metrics((0, 0.0, 0, 0.0))
        flipped = replace(base, total_return_pct=-0.0)
        assert _pack_metrics(base) != _pack_metrics(flipped)

    def test_pack_distinguishes_one_ulp(self) -> None:
        base = _empty_metrics((0, 0.0, 0, 0.0))
        bumped = replace(base, ulcer_index=math.nextafter(base.ulcer_index, math.inf))
        assert _pack_metrics(base) != _pack_metrics(bumped)

    def test_pack_distinguishes_none_from_zero(self) -> None:
        base = _empty_metrics((0, 0.0, 0, 0.0))
        assert base.sharpe_ratio is None
        filled = replace(base, sharpe_ratio=0.0)
        assert _pack_metrics(base) != _pack_metrics(filled)


class TestFoldCombineByteParity:
    """Old-vs-new bit-identity over the hostile corpus (the ship condition)."""

    @pytest.mark.parametrize(
        ("name", "trades", "total_bars", "bench_pct", "start_equity", "span", "drawdown"),
        _CORPUS,
        ids=_CORPUS_IDS,
    )
    def test_impl_bit_identical_to_frozen_oracle(
        self,
        name: str,
        trades: list[ClosedTrade],
        total_bars: int,
        bench_pct: float,
        start_equity: int,
        span: float,
        drawdown: tuple[int, float, int, float],
    ) -> None:
        expected = _oracle_metrics_pre_slice0(
            trades,
            total_bars,
            bench_pct,
            start_equity_paisa=start_equity,
            span_years=span,
            drawdown=drawdown,
        )
        actual = _compute_metrics_impl(
            trades,
            total_bars,
            bench_pct,
            start_equity_paisa=start_equity,
            span_years=span,
            drawdown=drawdown,
        )
        assert _pack_metrics(actual) == _pack_metrics(expected), name

    @pytest.mark.parametrize(
        ("name", "trades", "total_bars", "bench_pct", "start_equity", "span", "drawdown"),
        _CORPUS,
        ids=_CORPUS_IDS,
    )
    def test_scalar_entry_bit_identical_to_frozen_oracle(
        self,
        name: str,
        trades: list[ClosedTrade],
        total_bars: int,
        bench_pct: float,
        start_equity: int,
        span: float,
        drawdown: tuple[int, float, int, float],
    ) -> None:
        expected = _oracle_metrics_pre_slice0(
            trades,
            total_bars,
            bench_pct,
            start_equity_paisa=start_equity,
            span_years=span,
            drawdown=drawdown,
        )
        actual = compute_metrics_from_scalars(
            trades,
            total_bars,
            bench_pct,
            start_equity_paisa=start_equity,
            span_years=span,
            drawdown=drawdown,
        )
        assert _pack_metrics(actual) == _pack_metrics(expected), name

    def test_curve_entry_bit_identical_to_frozen_oracle(self) -> None:
        """The public curve entry (default drawdown path) is bit-identical too."""
        trades = [_trade(500, 600, 100, 3), _trade(-300, 50, 400, 2), _trade(0, 0, 0, 1)]
        equity = EquityCurve(
            equity_paisa=(1_000_000, 1_000_500, 1_000_200, 1_000_200),
            start_equity_paisa=1_000_000,
            span_years=1.5,
        )
        expected = _oracle_metrics_pre_slice0(
            trades,
            100,
            12.5,
            start_equity_paisa=equity.start_equity_paisa,
            span_years=equity.span_years,
            drawdown=_drawdown_stats(equity),
        )
        actual = compute_metrics(trades, equity, 100, 12.5)
        assert _pack_metrics(actual) == _pack_metrics(expected)

    def test_zero_trade_row_is_empty_metrics_arm(self) -> None:
        """The zero-trade row stays the composition root's own arm."""
        drawdown = (400, 0.04, 5, 0.02)
        row = _compute_metrics_impl(
            [], 100, 0.0, start_equity_paisa=1_000_000, span_years=1.0, drawdown=drawdown
        )
        assert _pack_metrics(row) == _pack_metrics(_empty_metrics(drawdown))


#: The pinned fold fixture: w, w, l, l, l, scratch, w — streaks 2/3.
_PIN_TRADES: list[ClosedTrade] = [
    _trade(500, 600, 100, 3),
    _trade(700, 800, 50, 2),
    _trade(-300, 50, 400, 2),
    _trade(-400, 10, 500, 4),
    _trade(-300, 20, 350, 1),
    _trade(0, 0, 0, 1),
    _trade(900, 950, 60, 5),
]


class TestFoldExactValuePins:
    """Exact-value pins for every FOLD expression (mutation teeth).

    Literals hand-captured from the fixture on 2026-07-09 (float pins as
    ``float.fromhex`` — bit-exact, not approximate).
    """

    def test_fold_integer_aggregates_exact(self) -> None:
        folds = _fold_trades(_PIN_TRADES, 1_000_000)
        assert folds.n_trades == 7
        assert folds.net_pnl_paisa == 1100
        assert folds.gross_profit_paisa == 2100
        assert folds.gross_loss_abs_paisa == 1000
        assert folds.winners == 3
        assert folds.losers == 3
        assert folds.sum_mfe_paisa == 2430
        assert folds.sum_mae_paisa == 1460
        assert folds.sum_holding_bars == 18
        assert folds.max_holding_bars == 5
        assert folds.largest_loss_paisa == -400
        assert folds.max_consecutive_losses == 3
        assert folds.max_consecutive_wins == 2

    def test_fold_moments_exact(self) -> None:
        folds = _fold_trades(_PIN_TRADES, 1_000_000)
        assert folds.moments.n == 7
        assert struct.pack("<d", folds.moments.mean) == struct.pack(
            "<d", float.fromhex("0x1.498d6dd4d1037p-13")
        )
        assert struct.pack("<d", folds.moments.m2) == struct.pack(
            "<d", float.fromhex("0x1.ccf1289b920b0p-20")
        )
        assert struct.pack("<d", folds.moments.m3) == struct.pack(
            "<d", float.fromhex("0x1.0a70dac47959ap-32")
        )
        assert struct.pack("<d", folds.moments.m4) == struct.pack(
            "<d", float.fromhex("0x1.4bdb1ae58a600p-41")
        )

    def test_fold_downside_and_percentiles_exact(self) -> None:
        folds = _fold_trades(_PIN_TRADES, 1_000_000)
        assert struct.pack("<d", folds.downside_sum_sq) == struct.pack(
            "<d", float.fromhex("0x1.6d127d05394fdp-22")
        )
        assert struct.pack("<d", folds.p95) == struct.pack(
            "<d", float.fromhex("0x1.b866e43aa79bap-11")
        )
        assert struct.pack("<d", folds.p5) == struct.pack(
            "<d", float.fromhex("-0x1.83f91e646f156p-12")
        )

    def test_fold_empty_downside_keeps_int_zero_sum(self) -> None:
        """``sum([])`` is the int ``0`` — kept as-is so ``0 / n`` stays exact."""
        folds = _fold_trades([_trade(7), _trade(7)], 100)
        assert folds.downside_sum_sq == 0
        assert isinstance(folds.downside_sum_sq, int)

    def test_fold_matches_oracle_intermediates(self) -> None:
        """The fold's moment/percentile values equal freshly-derived ones."""
        folds = _fold_trades(_PIN_TRADES, 1_000_000)
        returns = [t.pnl_paisa / 1_000_000 for t in _PIN_TRADES]
        assert folds.moments == _accumulate_moments(returns)
        srt = sorted(returns)
        assert struct.pack("<d", folds.p95) == struct.pack("<d", _percentile(srt, 95.0))
        assert struct.pack("<d", folds.p5) == struct.pack("<d", _percentile(srt, 5.0))


class TestCombineExactValuePins:
    """Exact-value pins for every COMBINE expression (mutation teeth)."""

    @staticmethod
    def _combined() -> ResultMetrics:
        return combine_metrics_from_folds(
            _fold_trades(_PIN_TRADES, 1_000_000),
            total_bars=100,
            benchmark_return_pct=12.5,
            start_equity_paisa=1_000_000,
            span_years=1.5,
            drawdown=(1500, 0.15, 7, 0.05),
        )

    def test_combine_money_and_count_fields_exact(self) -> None:
        m = self._combined()
        assert m.net_pnl_paisa == 1100
        assert m.gross_profit_paisa == 2100
        assert m.gross_loss_paisa == 1000
        assert m.largest_losing_trade_paisa == -400
        assert m.max_consecutive_losses == 3
        assert m.max_consecutive_wins == 2
        assert m.total_trades == 7
        assert m.winning_trades == 3
        assert m.losing_trades == 3
        assert m.avg_winning_trade_paisa == 700  # 2100 // 3
        assert m.avg_losing_trade_paisa == 333  # 1000 // 3 (floor)
        assert m.expectancy_paisa == 157  # 1100 // 7 (floor)
        assert m.avg_mfe_paisa == 347  # 2430 // 7
        assert m.avg_mae_paisa == 208  # 1460 // 7
        assert m.max_holding_time_bars == 5
        assert m.max_drawdown_paisa == 1500
        assert m.max_drawdown_duration_bars == 7

    def test_combine_ratio_fields_bit_exact(self) -> None:
        m = self._combined()
        pins: list[tuple[str, str]] = [
            ("total_return_pct", "0x1.c28f5c28f5c29p-4"),
            ("cagr_pct", "0x1.2c517b922db40p-4"),
            ("sharpe_ratio", "0x1.44e49e470d2a7p-2"),
            ("sortino_ratio", "0x1.6d117e8a5d454p-1"),
            ("calmar_ratio", "0x1.f487cdf3a1816p-2"),
            ("mar_ratio", "0x1.f487cdf3a1816p-2"),
            ("information_ratio", "-0x1.1de58a8d6ead1p+5"),
            ("omega_ratio", "0x1.0cccccccccccdp+1"),
            ("max_drawdown_pct", "0x1.3333333333333p-3"),
            ("recovery_factor", "0x1.7777777777777p-1"),
            ("ulcer_index", "0x1.999999999999ap-5"),
            ("win_rate", "0x1.b6db6db6db6dbp-2"),
            ("profit_factor", "0x1.0cccccccccccdp+1"),
            ("win_loss_ratio", "0x1.0d11ae82b0d12p+1"),
            ("avg_holding_time_bars", "0x1.4924924924925p+1"),
            ("time_in_market_pct", "0x1.2p+4"),
            ("tail_ratio", "0x1.22983759f2297p+1"),
            ("skewness", "0x1.23c50ff10b5bbp-2"),
            ("kurtosis", "-0x1.99bc0e27b3dbap+0"),
            ("returns_std_dev", "0x1.03abe08ec0344p-11"),
        ]
        for field_name, hex_literal in pins:
            value = getattr(m, field_name)
            assert isinstance(value, float), field_name
            assert struct.pack("<d", value) == struct.pack("<d", float.fromhex(hex_literal)), (
                field_name
            )

    def test_combine_stage4_fields_stay_none(self) -> None:
        m = self._combined()
        assert m.deflated_sharpe_ratio is None
        assert m.pbo_probability is None

    def test_combine_negative_expectancy_floor_division(self) -> None:
        """``-7 // 2 == -4`` (Python floor) survives the decomposition."""
        m = combine_metrics_from_folds(
            _fold_trades([_trade(-3), _trade(-4)], 999),
            total_bars=10,
            benchmark_return_pct=0.0,
            start_equity_paisa=999,
            span_years=0.001,
            drawdown=(7, 0.7, 2, 0.3),
        )
        assert m.expectancy_paisa == -4
        assert m.avg_losing_trade_paisa == 3  # 7 // 2


class TestTradeFoldsContract:
    """Shape pins the future Rust core mirrors term-for-term."""

    def test_trade_folds_field_order_frozen(self) -> None:
        assert [f.name for f in fields(TradeFolds)] == [
            "n_trades",
            "net_pnl_paisa",
            "gross_profit_paisa",
            "gross_loss_abs_paisa",
            "winners",
            "losers",
            "sum_mfe_paisa",
            "sum_mae_paisa",
            "sum_holding_bars",
            "max_holding_bars",
            "largest_loss_paisa",
            "max_consecutive_losses",
            "max_consecutive_wins",
            "moments",
            "downside_sum_sq",
            "p95",
            "p5",
        ]

    def test_streaks_delegate_to_consecutive_runs(self) -> None:
        folds = _fold_trades(_PIN_TRADES, 1_000_000)
        assert (folds.max_consecutive_losses, folds.max_consecutive_wins) == _consecutive_runs(
            _PIN_TRADES
        )
