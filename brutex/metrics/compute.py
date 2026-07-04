"""Per-result trading metrics computation.

Implements the metrics enumerated in ``TRACK1_BACKTEST_SPEC.md``
§"Result Row Schema (Every Single Trial — 30+ Metrics)" (spec lines
512-579). Every metric below cites the schema line or the inline
formula the spec gives for it. Where the spec only *names* a metric
(Sharpe, Sortino, skewness, kurtosis, ulcer-index time weighting) and
does not pin the exact convention, a textbook definition is used and
the convention is documented on the field; the chosen conventions are
recorded in ``docs/MIGRATION.md`` entry #305 (DEC-METRICS-001) under the
honest-envelope clause.

**Stage-4 statistical-validity metrics (spec lines 581-587).** Two of the
six are now implemented (DEC-STAT-DSR-PBO-001): ``deflated_sharpe_ratio``
(Bailey & LdP 2014) and ``pbo_probability`` (Bailey et al. 2017, CSCV).
They are computed by :func:`with_stage4_statistics` from the pure functions
in :mod:`brutex.metrics.deflated_sharpe` and :mod:`brutex.metrics.pbo`,
because they need cross-trial inputs (the leaderboard's trial count, the
Sharpe dispersion, and the CSCV performance matrix) that the per-result
:func:`compute_metrics` pass does not have — so ``compute_metrics`` leaves
them ``None`` (NULL, "Stage 4 only, NULL before then" per the spec) and the
Stage-4 step fills them in. The remaining four (``cpcv_pass_rate``,
``wfo_pass_rate``, ``lockbox_sharpe``, ``lockbox_mdd_pct`` — spec lines
584-587) remain deferred to the filter-cascade work.

**Precision rule (DEC-MONEY-2DP-001, money-only):** money fields stay
int64 paisa; ratios / percentages / statistical moments keep full
float precision and are NEVER rounded — the 2-decimal rule applies to
money *display* only and is deliberately not imported here.

**Numerical stability:** moments (mean, variance, skewness, kurtosis)
are accumulated in one streaming pass using Welford's online algorithm
extended to the third and fourth central moments per Pébay 2008,
*"Formulas for Robust, One-Pass Parallel Computation of Covariances and
Arbitrary-Order Statistical Moments"* (Sandia SAND2008-6212). This
avoids the catastrophic cancellation of the naive
``E[x²] − E[x]²`` form.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

from brutex.metrics.deflated_sharpe import GAUSSIAN_KURTOSIS, deflated_sharpe_ratio
from brutex.metrics.pbo import pbo_probability
from brutex.metrics.types import ClosedTrade, EquityCurve

__all__ = [
    "Moments",
    "ResultMetrics",
    "compute_metrics",
    "compute_metrics_from_scalars",
    "with_stage4_statistics",
]

#: Number of trades below which moment-based ratios (Sharpe, Sortino,
#: skewness, kurtosis) are mathematically undefined and returned as
#: ``None``. Variance needs >= 2 samples; skewness >= 2; kurtosis >= 2.
_MIN_SAMPLES_VARIANCE: Final[int] = 2

#: Finite cap for the annualised CAGR (%). Tiny backtest spans annualise to
#: meaningless magnitudes (a sub-day window can even overflow float64 in the
#: ``growth ** (1/span)`` step); capping keeps the result row inf-free
#: (spec line 746) and ranking-stable — the Stage-4 z-score is clamped to
#: [-3, +3] regardless. Recorded in DEC-METRICS-001 (MIGRATION #305).
_CAGR_CAP_PCT: Final[float] = 1.0e9


@dataclass(frozen=True, slots=True)
class Moments:
    """Streaming central moments of a return series.

    Computed by :func:`_accumulate_moments` in one pass (Welford +
    Pébay 2008). All values are population moments (divide by ``n``),
    which is the convention used for the strategy-comparison ratios
    here; the choice is documented in DEC-METRICS-001.

    Attributes
    ----------
    n
        Sample count.
    mean
        Arithmetic mean of the series.
    m2
        Sum of squared deviations from the mean (``Σ (x − mean)²``).
    m3
        Sum of cubed deviations (``Σ (x − mean)³``).
    m4
        Sum of fourth-power deviations (``Σ (x − mean)⁴``).
    """

    n: int
    mean: float
    m2: float
    m3: float
    m4: float

    @property
    def variance(self) -> float | None:
        """Return the population variance, or ``None`` if ``n < 2``."""
        if self.n < _MIN_SAMPLES_VARIANCE:
            return None
        return self.m2 / self.n

    @property
    def std_dev(self) -> float | None:
        """Return the population standard deviation, or ``None`` if ``n < 2``."""
        var = self.variance
        if var is None:
            return None
        return math.sqrt(var)


def _accumulate_moments(values: Sequence[float]) -> Moments:
    """Accumulate the first four central moments in one streaming pass.

    Uses Welford's online mean/variance update extended to the third
    and fourth moments per Pébay 2008 (SAND2008-6212, eqs. for
    arbitrary-order central moments). Numerically stable: no
    ``E[x²] − E[x]²`` cancellation.

    Parameters
    ----------
    values
        The return series (per-trade returns or per-bar returns).

    Returns
    -------
    Moments
        The accumulated moments. ``n == 0`` yields all-zero moments.
    """
    n = 0
    mean = 0.0
    m2 = 0.0
    m3 = 0.0
    m4 = 0.0
    for x in values:
        n1 = n
        n += 1
        delta = x - mean
        delta_n = delta / n
        delta_n2 = delta_n * delta_n
        term1 = delta * delta_n * n1
        mean += delta_n
        m4 += term1 * delta_n2 * (n * n - 3 * n + 3) + 6 * delta_n2 * m2 - 4 * delta_n * m3
        m3 += term1 * delta_n * (n - 2) - 3 * delta_n * m2
        m2 += term1
    return Moments(n=n, mean=mean, m2=m2, m3=m3, m4=m4)


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    """Return ``numerator / denominator``, or ``None`` if the denominator is zero.

    Used for every ratio whose denominator can legitimately be zero
    (zero variance, zero losses, zero drawdown). Returning ``None``
    (rather than ``inf``) makes the "undefined" case explicit and
    JSON-serialisable, and keeps the result row free of ``inf`` per the
    spec's "No NaN/inf in result rows" gate (spec line 746).
    """
    if denominator == 0.0:
        return None
    return numerator / denominator


@dataclass(frozen=True, slots=True)
class ResultMetrics:
    """Every per-result metric defined in the Track 1 Result Row Schema.

    Money fields are int64 paisa; ratio / percentage / moment fields are
    full-precision floats (or ``None`` when mathematically undefined for
    the given sample). The Stage-4 statistical-validity metrics
    ``deflated_sharpe_ratio`` + ``pbo_probability`` (spec lines 582-583)
    default to ``None`` and are filled by :func:`with_stage4_statistics`;
    the other four Stage-4 fields (spec lines 584-587) remain deferred and
    are absent — see the module docstring.

    Field groups mirror ``TRACK1_BACKTEST_SPEC.md`` §"Result Row Schema".
    """

    # ── Return metrics (spec lines 532-537) ──────────────────────────
    net_pnl_paisa: int
    total_return_pct: float
    cagr_pct: float
    gross_profit_paisa: int
    gross_loss_paisa: int  # absolute value, per spec line 537

    # ── Risk-adjusted return (spec lines 539-545) ────────────────────
    sharpe_ratio: float | None
    sortino_ratio: float | None
    calmar_ratio: float | None
    mar_ratio: float | None
    information_ratio: float | None
    omega_ratio: float | None

    # ── Drawdown (spec lines 547-555) ────────────────────────────────
    max_drawdown_pct: float
    max_drawdown_paisa: int
    max_drawdown_duration_bars: int
    recovery_factor: float | None
    ulcer_index: float
    largest_losing_trade_paisa: int
    max_consecutive_losses: int
    max_consecutive_wins: int

    # ── Trade quality (spec lines 557-568) ───────────────────────────
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float | None
    avg_winning_trade_paisa: int
    avg_losing_trade_paisa: int  # absolute, per spec line 564
    win_loss_ratio: float | None
    expectancy_paisa: int
    avg_mfe_paisa: int
    avg_mae_paisa: int

    # ── Trade timing (spec lines 570-573) ────────────────────────────
    avg_holding_time_bars: float
    max_holding_time_bars: int
    time_in_market_pct: float

    # ── Distribution robustness (spec lines 575-579) ─────────────────
    tail_ratio: float | None
    skewness: float | None
    kurtosis: float | None
    returns_std_dev: float | None

    # ── Statistical validity (Stage 4 only — NULL before then; spec 581-583) ──
    # Filled by with_stage4_statistics(); left None (NULL) by compute_metrics,
    # whose per-result pass lacks the cross-trial inputs (trial count, Sharpe
    # dispersion, CSCV matrix). The other four Stage-4 fields (cpcv_pass_rate,
    # wfo_pass_rate, lockbox_sharpe, lockbox_mdd_pct — spec 584-587) remain
    # deferred (DEC-STAT-DSR-PBO-001 scope = DSR + PBO only).
    # pragma justification (DEC-MUTATION-GATE-COMPUTE-001): the only
    # unkillable mutants here are the annotation-only `| None` -> `& None`
    # flips — under `from __future__ import annotations` (PEP 563) field
    # annotations are never evaluated, so the flip is a runtime no-op. The
    # REAL mutants these lines also carry (default `None` -> `""`) stay
    # pinned by test_compute_rows_stage4_defaults_are_none.
    deflated_sharpe_ratio: float | None = None  # pragma: no mutate
    pbo_probability: float | None = None  # pragma: no mutate


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Return the linear-interpolation percentile of a pre-sorted series.

    Parameters
    ----------
    sorted_values
        Non-empty, ascending-sorted series.
    pct
        Percentile in ``[0, 100]``.

    Returns
    -------
    float
        The interpolated percentile (NumPy ``"linear"`` convention).
    """
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (pct / 100.0) * (len(sorted_values) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]
    frac = rank - lower
    return sorted_values[lower] * (1.0 - frac) + sorted_values[upper] * frac


def _drawdown_stats(equity: EquityCurve) -> tuple[int, float, int, float]:
    """Compute drawdown statistics from the equity curve.

    Returns a 4-tuple ``(max_dd_paisa, max_dd_pct, max_dd_duration_bars,
    ulcer_index)``:

    * ``max_dd_paisa`` — largest peak-to-trough equity drop in paisa
      (``>= 0``), per spec line 549.
    * ``max_dd_pct`` — that drop as a fraction-of-peak percentage,
      per spec line 548.
    * ``max_dd_duration_bars`` — longest run of bars spent below a
      prior peak (the longest drawdown stretch), per spec line 550.
    * ``ulcer_index`` — the Ulcer Index, the RMS of the percentage
      drawdown series (Martin & McCann 1989, "time-weighted drawdown",
      spec line 552).

    The percentage drawdowns are taken relative to the running peak
    equity (the standard definition); when the running peak is zero or
    negative the percentage contribution is treated as zero (it cannot
    be expressed as a fraction-of-peak). The percentage is deliberately
    NOT clamped at 100%: if realised losses drive equity below zero,
    a peak-to-trough drop can exceed the peak, and the resulting
    ``> 100%`` figure is real, gate-relevant information (the
    ``max_drawdown_pct > 30%`` rejection gate, spec line 597).
    """
    peak = equity.equity_paisa[0]
    max_dd_paisa = 0
    max_dd_pct = 0.0
    sum_sq_pct = 0.0
    # pragma justification (DEC-MUTATION-GATE-COMPUTE-001): dead store — the
    # first loop iteration always has drop == 0 (peak is initialised to the
    # first sample), so the else-branch reassigns current_dd_bars before any
    # read; no initial value is observable.
    current_dd_bars = 0  # pragma: no mutate
    max_dd_bars = 0
    for value in equity.equity_paisa:
        peak = max(peak, value)
        drop = peak - value
        if drop > 0:
            current_dd_bars += 1
        else:
            current_dd_bars = 0
        max_dd_bars = max(max_dd_bars, current_dd_bars)
        max_dd_paisa = max(max_dd_paisa, drop)
        if peak > 0:
            dd_pct = drop / peak
            max_dd_pct = max(max_dd_pct, dd_pct)
            sum_sq_pct += dd_pct * dd_pct
    ulcer = math.sqrt(sum_sq_pct / len(equity.equity_paisa))
    return max_dd_paisa, max_dd_pct * 100.0, max_dd_bars, ulcer * 100.0


def _consecutive_runs(trades: Sequence[ClosedTrade]) -> tuple[int, int]:
    """Return ``(max_consecutive_losses, max_consecutive_wins)``.

    A "win" is ``pnl > 0`` and a "loss" is ``pnl < 0`` (scratch trades,
    ``pnl == 0``, break both streaks). Per spec lines 554-555.
    """
    max_losses = 0
    max_wins = 0
    cur_losses = 0
    cur_wins = 0
    for trade in trades:
        if trade.is_win:
            cur_wins += 1
            cur_losses = 0
        elif trade.is_loss:
            cur_losses += 1
            cur_wins = 0
        else:
            cur_wins = 0
            cur_losses = 0
        max_wins = max(max_wins, cur_wins)
        max_losses = max(max_losses, cur_losses)
    return max_losses, max_wins


def _empty_metrics(drawdown: tuple[int, float, int, float]) -> ResultMetrics:
    """Build the metrics row for a zero-trade result.

    Every count is zero; every ratio that needs trades is ``None``;
    money fields are zero. The drawdown family comes from the supplied
    ``drawdown`` 4-tuple — :func:`_drawdown_stats` output computed by the
    caller (a flat curve has zero drawdown). This keeps the row
    inf/NaN-free per spec line 746.
    """
    max_dd_paisa, max_dd_pct, max_dd_bars, ulcer = drawdown
    return ResultMetrics(
        net_pnl_paisa=0,
        total_return_pct=0.0,
        cagr_pct=0.0,
        gross_profit_paisa=0,
        gross_loss_paisa=0,
        sharpe_ratio=None,
        sortino_ratio=None,
        calmar_ratio=None,
        mar_ratio=None,
        information_ratio=None,
        omega_ratio=None,
        max_drawdown_pct=max_dd_pct,
        max_drawdown_paisa=max_dd_paisa,
        max_drawdown_duration_bars=max_dd_bars,
        recovery_factor=None,
        ulcer_index=ulcer,
        largest_losing_trade_paisa=0,
        max_consecutive_losses=0,
        max_consecutive_wins=0,
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
        win_rate=0.0,
        profit_factor=None,
        avg_winning_trade_paisa=0,
        avg_losing_trade_paisa=0,
        win_loss_ratio=None,
        expectancy_paisa=0,
        avg_mfe_paisa=0,
        avg_mae_paisa=0,
        avg_holding_time_bars=0.0,
        max_holding_time_bars=0,
        time_in_market_pct=0.0,
        tail_ratio=None,
        skewness=None,
        kurtosis=None,
        returns_std_dev=None,
    )


def _skewness(moments: Moments) -> float | None:
    """Return the population skewness, or ``None`` if undefined.

    Population (Fisher-Pearson) skewness ``g1 = m3/n / (m2/n)**1.5``.
    Undefined when ``n < 2`` or the variance is zero (a constant
    series has no shape).
    """
    if moments.n < _MIN_SAMPLES_VARIANCE or moments.m2 == 0.0:
        return None
    m2_n = moments.m2 / moments.n
    m3_n = moments.m3 / moments.n
    return m3_n / math.pow(m2_n, 1.5)


def _excess_kurtosis(moments: Moments) -> float | None:
    """Return the population *excess* kurtosis, or ``None`` if undefined.

    Excess kurtosis ``g2 = m4/n / (m2/n)**2 − 3`` (normal-distribution
    baseline subtracted). Undefined when ``n < 2`` or variance is zero.
    """
    if moments.n < _MIN_SAMPLES_VARIANCE or moments.m2 == 0.0:
        return None
    m2_n = moments.m2 / moments.n
    m4_n = moments.m4 / moments.n
    return m4_n / (m2_n * m2_n) - 3.0


def compute_metrics(
    trades: Sequence[ClosedTrade],
    equity: EquityCurve,
    total_bars: int,
    benchmark_return_pct: float = 0.0,
    *,
    drawdown: tuple[int, float, int, float] | None = None,
) -> ResultMetrics:
    """Compute every per-result metric in the Track 1 Result Row Schema.

    Parameters
    ----------
    trades
        The closed trades of this result, in chronological order. May
        be empty (a no-trade result yields an all-zero / ``None`` row).
    equity
        The :class:`~brutex.metrics.types.EquityCurve` for the result,
        used for the drawdown family, CAGR and total return.
    total_bars
        Total number of in-scope bars in the backtest window, used as
        the denominator of ``time_in_market_pct`` (spec line 573).
        Must be ``>= 1``.
    benchmark_return_pct
        Buy-and-hold total return (%) of the instrument over the same
        window, used as the excess-return baseline for
        ``information_ratio`` (spec line 544, "excess vs buy-and-hold").
        Defaults to ``0.0`` (treat raw return as excess) when no
        benchmark is supplied.
    drawdown
        Optional precomputed :func:`_drawdown_stats` 4-tuple
        ``(max_dd_paisa, max_dd_pct, max_dd_duration_bars, ulcer_index)``
        for ``equity``. ``None`` (the default) computes it here exactly
        as before — the default path is byte-identical. The injection
        seam exists for DEC-SWEEP-RUST-METRICS-001 (MIGRATION.md #1186):
        the flag-gated sweep tail supplies the bit-identical fused Rust
        fold's output so the O(N_bars) Python scan is not re-run. A
        caller-supplied tuple is trusted to BE ``_drawdown_stats``
        output for this curve; the sweep seam validates the kernel
        result before injecting (never a fabricated number,
        Golden Rule #1).

    Returns
    -------
    ResultMetrics
        The fully-populated (minus Stage-4) metrics row.

    Raises
    ------
    ValueError
        If ``total_bars < 1``.

    Notes
    -----
    Spec citations are on each :class:`ResultMetrics` field group and in
    the inline comments of :func:`_compute_metrics_impl` (the shared
    body — one source of truth, CLAUDE.md §11 COMMON). Conventions
    chosen where the spec is silent (zero risk-free rate;
    per-trade-return Sharpe/Sortino with no annualisation multiplier;
    population moments) are recorded in DEC-METRICS-001 (MIGRATION.md
    entry #305).
    """
    if total_bars < 1:
        raise ValueError(f"total_bars must be >= 1, got {total_bars}")
    if drawdown is None:
        drawdown = _drawdown_stats(equity)
    return _compute_metrics_impl(
        trades,
        total_bars,
        benchmark_return_pct,
        start_equity_paisa=equity.start_equity_paisa,
        span_years=equity.span_years,
        drawdown=drawdown,
    )


def compute_metrics_from_scalars(
    trades: Sequence[ClosedTrade],
    total_bars: int,
    benchmark_return_pct: float = 0.0,
    *,
    start_equity_paisa: int,
    span_years: float,
    drawdown: tuple[int, float, int, float],
) -> ResultMetrics:
    """Curve-less :func:`compute_metrics`: scalars in place of an :class:`EquityCurve`.

    DEC-SWEEP-RUST-METRICS-001 (MIGRATION.md #1186). :func:`compute_metrics`
    reads exactly three things from its ``equity`` argument — the
    ``start_equity_paisa`` denominator, the ``span_years`` annualiser, and
    the :func:`_drawdown_stats` 4-tuple. The flag-gated sweep tail computes
    the drawdown family in the fused Rust kernel WITHOUT materialising the
    ``O(N_bars)`` equity tuple, so this entry accepts those three directly.
    The body is the SAME :func:`_compute_metrics_impl` the curve entry uses
    (no duplication, CLAUDE.md §11 COMMON): for equal scalars the two
    entries are byte-identical by construction.

    Because the :class:`~brutex.metrics.types.EquityCurve` constructor is
    bypassed, its invariants are RE-ASSERTED here (belt — the sweep seam
    pre-checks them and falls back to the curve path, which raises the
    curve's own messages):

    Raises
    ------
    ValueError
        If ``start_equity_paisa <= 0``, ``span_years`` is not a strictly
        positive finite float, or ``total_bars < 1``.
    """
    if start_equity_paisa <= 0:
        raise ValueError(
            f"compute_metrics_from_scalars: start_equity_paisa must be > 0, "
            f"got {start_equity_paisa}"
        )
    if not math.isfinite(span_years) or span_years <= 0.0:
        raise ValueError(f"compute_metrics_from_scalars: span_years must be > 0, got {span_years}")
    if total_bars < 1:
        raise ValueError(f"total_bars must be >= 1, got {total_bars}")
    return _compute_metrics_impl(
        trades,
        total_bars,
        benchmark_return_pct,
        start_equity_paisa=start_equity_paisa,
        span_years=span_years,
        drawdown=drawdown,
    )


def _compute_metrics_impl(
    trades: Sequence[ClosedTrade],
    total_bars: int,
    benchmark_return_pct: float,
    *,
    start_equity_paisa: int,
    span_years: float,
    drawdown: tuple[int, float, int, float],
) -> ResultMetrics:
    """Shared metric body behind :func:`compute_metrics` / the scalar entry.

    The verbatim pre-#1186 :func:`compute_metrics` body with the three
    ``equity`` reads replaced by the already-extracted scalars and the
    :func:`_drawdown_stats` call replaced by the supplied ``drawdown``
    tuple — a pure parameterisation, no behaviour change (the golden +
    equality tests pin it). Callers validate their own boundary; this
    body assumes ``total_bars >= 1`` and a valid scalar set.
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
    # pragma justification (DEC-MUTATION-GATE-COMPUTE-001): every trade has
    # holding_bars >= 1 (ClosedTrade validation) and trades is non-empty on
    # this path (the zero-trade row returned above), so the running max is
    # >= 1 regardless of an initial 0 or 1 — the initialiser is unobservable.
    max_holding = 0  # pragma: no mutate
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
    # total_return_pct: net P&L as a percentage of the starting capital.
    total_return_pct = 100.0 * net_pnl / start_equity_paisa
    # cagr_pct: annualised geometric growth (spec line 535 "annualized").
    # ending/starting ratio; start_equity_paisa > 0 by EquityCurve invariant
    # (re-asserted by compute_metrics_from_scalars on the curve-less entry).
    end_equity = start_equity_paisa + net_pnl
    # "< 1" is identical to "<= 0" on ints; phrased this way every comparison
    # mutant of the ruin boundary is observable (at end_equity == 0 the
    # else-branch would compute growth 0.0 -> exactly -100.0 anyway, making a
    # "<= 0" vs "< 0" flip unkillable — DEC-MUTATION-GATE-COMPUTE-001).
    if end_equity < 1:
        # Total ruin — CAGR of a non-positive terminal equity is not a
        # real number; clamp to -100% (capital fully lost p.a.).
        cagr_pct = -100.0
    else:
        growth = end_equity / start_equity_paisa
        # Annualisation explodes for tiny spans (a sub-day backtest annualises
        # to a meaningless magnitude and can raise OverflowError). Guard the
        # overflow and clamp to a finite cap so the row stays inf-free
        # (spec line 746); the Stage-4 z-score clamp [-3, +3] absorbs the rest.
        try:
            annualised_growth = growth ** (1.0 / span_years)
        except OverflowError:
            annualised_growth = math.inf
        uncapped_cagr = min(100.0 * (annualised_growth - 1.0), _CAGR_CAP_PCT)
        # pragma justification (DEC-MUTATION-GATE-COMPUTE-001): the lower
        # clamp is defensive-only — annualised_growth >= 0.0 (a positive
        # base to a positive power, or 0.0 on underflow), so the uncapped
        # value is always >= -100.0 after rounding and any bound <= -100 is
        # behaviourally identical. The cap arithmetic above stays mutable.
        cagr_pct = max(-100.0, uncapped_cagr)  # pragma: no mutate

    # ── Drawdown (spec lines 547-555) ────────────────────────────────
    max_dd_paisa, max_dd_pct, max_dd_bars, ulcer = drawdown
    max_losses, max_wins = _consecutive_runs(trades)
    # recovery_factor = net profit / MDD (spec line 551). None if MDD == 0.
    recovery_factor = _safe_ratio(float(net_pnl), float(max_dd_paisa))

    # ── Trade quality (spec lines 557-568) ───────────────────────────
    win_rate = winners / n_trades  # fraction, per spec line 561
    # profit_factor = gross_profit / gross_loss (spec line 562). None when
    # there are no losing trades (no real denominator).
    profit_factor = _safe_ratio(float(gross_profit), float(gross_loss_abs))
    avg_win = gross_profit // winners if winners else 0
    avg_loss_abs = gross_loss_abs // losers if losers else 0
    # win_loss_ratio = avg_win / avg_loss (spec line 565). _safe_ratio already
    # yields None when avg_loss_abs == 0 (which holds iff there are no losers).
    win_loss_ratio = _safe_ratio(float(avg_win), float(avg_loss_abs))
    # expectancy_paisa = money per trade (spec line 566) = net P&L / n.
    expectancy = net_pnl // n_trades
    avg_mfe = sum_mfe // n_trades
    avg_mae = sum_mae // n_trades

    # ── Trade timing (spec lines 570-573) ────────────────────────────
    avg_holding = sum_holding / n_trades
    # time_in_market_pct: fraction of in-scope bars spent in a position
    # (spec line 573). Overlapping holds cannot exceed 100%; Track 1 is
    # single-position so bars are non-overlapping, but we clamp for safety.
    time_in_market_pct = min(100.0, 100.0 * sum_holding / total_bars)

    # ── Risk-adjusted return (spec lines 539-545) ────────────────────
    # Return series for the moment-based ratios = per-trade returns as a
    # fraction of starting capital. Zero risk-free rate (DEC-METRICS-001).
    returns = [t.pnl_paisa / start_equity_paisa for t in trades]
    moments = _accumulate_moments(returns)
    std_dev = moments.std_dev
    # Sharpe = mean(return) / std(return), no annualisation multiplier
    # (DEC-METRICS-001: per-trade-return ratio; spec lists cagr_pct
    # separately as the annualised figure). None when std is 0/undefined.
    sharpe = None if std_dev is None or std_dev == 0.0 else moments.mean / std_dev
    # Sortino = mean(return) / downside-deviation, where downside
    # deviation is the RMS of negative returns about a zero target
    # (spec line 541 "downside-only volatility").
    # pragma justification (DEC-MUTATION-GATE-COMPUTE-001): the strict "<"
    # boundary is unobservable — an r == 0.0 admitted by a "<=" flip would
    # contribute r * r == 0.0 to the sum. The filter threshold's REAL
    # mutants (0.0 -> 1.0 would admit positive returns) stay pinned by
    # test_sortino_known_value's exact-value assertion. The RMS arithmetic
    # below stays mutable; n_trades >= 1 here (zero-trade rows returned
    # above), so no division guard is needed.
    downside = [r for r in returns if r < 0.0]  # pragma: no mutate
    downside_dev = math.sqrt(sum(r * r for r in downside) / n_trades)
    sortino = None if downside_dev == 0.0 else moments.mean / downside_dev
    # calmar = CAGR / MaxDD (spec line 542). MaxDD as a percentage.
    calmar = _safe_ratio(cagr_pct, max_dd_pct)
    # mar = Calmar alternative (spec line 543). Identical definition
    # (CAGR / MaxDD%); kept as a distinct column per the schema.
    mar = calmar
    # information_ratio = excess return vs buy-and-hold (spec line 544), kept on
    # a per-trade horizon so it stays comparable across results with different
    # trade counts: (mean per-trade return − per-trade share of the buy-and-hold
    # return) / per-trade-return std. With the default zero benchmark it
    # collapses to the Sharpe ratio. None when std is 0/undefined. (DEC-METRICS-001.)
    benchmark_per_trade = (benchmark_return_pct / 100.0) / n_trades
    information = (
        None
        if std_dev is None or std_dev == 0.0
        else (moments.mean - benchmark_per_trade) / std_dev
    )
    # omega_ratio at 0% threshold (spec line 545): Σ gains / Σ |losses|
    # over the return series, with the threshold at zero. Equivalent to
    # gross_profit / gross_loss here (same partition), None when no losers.
    omega = _safe_ratio(float(gross_profit), float(gross_loss_abs))

    # ── Distribution robustness (spec lines 575-579) ─────────────────
    # tail_ratio = p95 / |p5| (spec line 576) of the per-trade return series.
    sorted_returns = sorted(returns)
    p95 = _percentile(sorted_returns, 95.0)
    p5 = _percentile(sorted_returns, 5.0)
    tail_ratio = _safe_ratio(p95, abs(p5))
    skewness = _skewness(moments)
    kurtosis = _excess_kurtosis(moments)

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
    )


def with_stage4_statistics(
    metrics: ResultMetrics,
    *,
    n_trials: int,
    trial_sharpe_variance: float,
    pbo_performance: Sequence[Sequence[float]] | None = None,
    pbo_n_splits: int = 16,
) -> ResultMetrics:
    """Return a copy of ``metrics`` with the Stage-4 DSR + PBO fields filled.

    Fills ``deflated_sharpe_ratio`` (spec line 582, Bailey & LdP 2014) and
    ``pbo_probability`` (spec line 583, Bailey et al. 2017 CSCV) using the
    pure functions in :mod:`brutex.metrics.deflated_sharpe` and
    :mod:`brutex.metrics.pbo`. The other four Stage-4 fields
    (``cpcv_pass_rate``, ``wfo_pass_rate``, ``lockbox_sharpe``,
    ``lockbox_mdd_pct`` — spec lines 584-587) remain deferred.

    A field stays ``None`` (NULL) when its inputs are undefined:

    * ``deflated_sharpe_ratio`` is ``None`` unless the per-result Sharpe,
      skewness and (excess) kurtosis are all defined and the row has at
      least two trades — exactly the rows for which :func:`compute_metrics`
      produced a usable Sharpe and shape moments.
    * ``pbo_probability`` is ``None`` unless ``pbo_performance`` (the CSCV
      ``(T, N)`` matrix for this row's leaderboard) is supplied.

    Parameters
    ----------
    metrics
        A per-result row from :func:`compute_metrics`. Its
        ``sharpe_ratio`` / ``skewness`` / ``kurtosis`` / ``total_trades``
        supply the DSR inputs. The stored kurtosis is *excess*, so
        :data:`~brutex.metrics.deflated_sharpe.GAUSSIAN_KURTOSIS` is added
        back to recover the non-excess kurtosis the PSR formula expects.
    n_trials
        ``N`` — the number of trials in this row's (instrument, TF)
        leaderboard, the multiple-testing count the Sharpe is deflated
        against (spec line 383, per-leaderboard N).
    trial_sharpe_variance
        ``V`` — the variance of the Sharpe ratios across those ``n_trials``.
    pbo_performance
        Optional CSCV ``(T, N)`` performance matrix for the leaderboard.
        Left ``None`` ⇒ ``pbo_probability`` stays NULL.
    pbo_n_splits
        ``S`` for CSCV; defaults to ``16`` (spec line 387).

    Returns
    -------
    ResultMetrics
        A new row (``metrics`` is frozen) with the two Stage-4 fields set.
        The remaining metrics are copied unchanged.
    """
    # pragma justification (DEC-MUTATION-GATE-COMPUTE-001, here and on pbo
    # below): the only unkillable mutants are the annotation-only
    # `| None` -> `& None` flips — local variable annotations are never
    # evaluated at runtime. The REAL `None -> ""` mutants sharing the lines
    # stay pinned by test_stage4_dsr_none_when_sharpe_missing /
    # test_stage4_pbo_none_without_performance_matrix (both assert `is None`).
    dsr: float | None = None  # pragma: no mutate
    if (
        metrics.sharpe_ratio is not None
        and metrics.skewness is not None
        and metrics.kurtosis is not None
        and metrics.total_trades >= _MIN_SAMPLES_VARIANCE
    ):
        dsr = deflated_sharpe_ratio(
            metrics.sharpe_ratio,
            n_trials=n_trials,
            trial_sharpe_variance=trial_sharpe_variance,
            sample_length=metrics.total_trades,
            skew=metrics.skewness,
            kurtosis=metrics.kurtosis + GAUSSIAN_KURTOSIS,
        ).deflated_sharpe_ratio

    pbo: float | None = None  # pragma: no mutate
    if pbo_performance is not None:
        pbo = pbo_probability(pbo_performance, n_splits=pbo_n_splits).pbo

    return replace(metrics, deflated_sharpe_ratio=dsr, pbo_probability=pbo)
