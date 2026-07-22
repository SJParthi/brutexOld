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
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.metrics.compute_rust import FoldWire, RecencyWire

from brutex.metrics.deflated_sharpe import GAUSSIAN_KURTOSIS, deflated_sharpe_ratio
from brutex.metrics.pbo import pbo_probability
from brutex.metrics.types import ClosedTrade, EquityCurve

__all__ = [
    "DECAY_BUCKET_DAYS",
    "DECAY_HALF_LIFE_MONTHS",
    "TRAILING_WINDOW",
    "Moments",
    "RecencyFolds",
    "RecencyMetrics",
    "ResultMetrics",
    "TradeFolds",
    "combine_metrics_from_folds",
    "combine_recency_from_folds",
    "compute_metrics",
    "compute_metrics_from_scalars",
    "fold_recency",
    "percentile",
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

#: The trailing-recency window (DEC-METRICS-TRAILING-WINDOW-001): 24 months,
#: implemented as a FIXED 730-day span (24 months (730 days) — a fixed
#: day-count, not a calendar-month walk, so the cutoff instant is a pure
#: ``anchor − timedelta`` subtraction that a Rust fold twin mirrors
#: mechanically with zero calendar logic). Membership is judged by each
#: trade's EXIT instant: ``exit_ts >= anchor − TRAILING_WINDOW`` (see
#: :func:`fold_recency`).
TRAILING_WINDOW: Final[timedelta] = timedelta(days=730)

#: Default half-life (in age-month buckets) of the exponential recency decay
#: (DEC-METRICS-TRAILING-WINDOW-001): a trade 12 bucket-months old at the
#: global anchor contributes half its P&L to ``decay_weighted_net_paisa``.
#: Configurable per call via the ``decay_half_life_months`` kw-only
#: parameter (NO env var — an explicit parameter, never ambient state).
DECAY_HALF_LIFE_MONTHS: Final[float] = 12.0

#: Days per decay age bucket (DEC-METRICS-TRAILING-WINDOW-001): a bucket is
#: one whole 30-day "month" of age at exit relative to the GLOBAL anchor —
#: ``max(0, (anchor − exit_ts).days) // 30``, a pure-integer, deterministic
#: key (deliberately NOT a calendar-month key: the integer form is exact,
#: total, and trivially mirrored by the Rust fold twin). The 2020→2026 data
#: span bounds the live bucket count at ~80.
DECAY_BUCKET_DAYS: Final[int] = 30


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
    #: The mirror of ``win_rate`` for the LOSING side
    #: (DEC-METRICS-LOSSRATE-FIRSTCLASS-001): ``losing_trades /
    #: total_trades`` as a fraction — first-class so ranking/display never
    #: re-derive it (scratch trades make ``1 − win_rate`` WRONG). ``0.0``
    #: for the zero-trade row, exactly like ``win_rate``.
    loss_rate: float
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

    # ── Growth metrics (DEC-METRICS-KELLY-GROWTH-001) ────────────────
    #: Kelly criterion fraction ``p − q/b`` (p = win probability over ALL
    #: trades, q = 1 − p, b = avg win / avg loss payoff ratio). ``None``
    #: when degenerate — no winners, no losers, or a zero avg-win/avg-loss
    #: magnitude (no real payoff ratio exists). COMBINE-side by the
    #: fold/combine libm law (DEC-METRICS-RUST-COMPUTE-FOLD-001): derived
    #: from fold scalars only, NO new fold state.
    kelly_fraction: float | None
    #: Expected per-trade log growth at HALF Kelly staking — the scalar
    #: closed form ``g(f) = p·ln(1 + f·b) + q·ln(1 − f)`` at ``f = f*/2``.
    #: The fractional-staking growth rate the ``growth`` ranking preset
    #: orders on. ``None`` when ``kelly_fraction`` is ``None`` or either
    #: log argument is non-positive (a defensive totality arm — provably
    #: dead for real fold outputs, see :func:`_kelly_growth`). COMBINE-side:
    #: ``math.log`` is libm territory and MUST stay Python (the
    #: fold/combine split's exactness law).
    log_growth_at_half_kelly: float | None

    # ── Exit quality (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B) ────
    #: Stop-loss-exit share of all closed trades: ``sl_exit_count /
    #: total_trades`` when the trade-sim's per-mask exit-reason counters were
    #: threaded in (the sweep spine always threads them — the position under
    #: ``brutex.engine.trade_sim.STOP_LOSS_REASON_INDEX``); ``0.0`` for the
    #: zero-trade row (a vacuous truth — the ``win_rate`` precedent); and
    #: ``None`` (the default) when the counters were NOT supplied — a caller
    #: without reason attribution reads the honest unknown, never a
    #: fabricated ``0.0`` (Golden Rule #1). COMBINE-side pure arithmetic
    #: over the threaded scalar — NO new fold state (the Rust fold wire is
    #: untouched). HONESTY NOTE: an SL exit is provably a LOSS only under
    #: ``LevelFill.AT_LEVEL`` (the default policy — the worst-case fill law
    #: clamps the fill at or beyond the level, so pnl <= −sl_distance < 0); a
    #: ``LevelFill.NEXT_OPEN`` SL fill can gap PROFITABLE, so
    #: ``sl_count <= losing_trades`` is NOT a universal invariant and is
    #: deliberately not enforced here (property-pinned for the default
    #: policy only).
    sl_hit_rate: float | None = None

    # ── Recency metrics (DEC-METRICS-TRAILING-WINDOW-001) ────────────
    #: Sharpe ratio over ONLY the trades whose ``exit_ts`` falls inside the
    #: trailing :data:`TRAILING_WINDOW` (24 months (730 days)) ending at the
    #: GLOBAL ``trailing_anchor`` — the run's data-end instant, threaded as
    #: a per-run parameter, NEVER derived per-combo (a combo that died in
    #: 2023 must not read recent-strong). The SAME per-trade-return Sharpe
    #: law as ``sharpe_ratio`` (windowed ``moments.mean / moments.std_dev``,
    #: zero risk-free rate, NO annualisation multiplier — DEC-METRICS-001;
    #: same ``start_equity_paisa`` return denominator, so the two figures
    #: differ only in trade membership). ``None`` when the anchor was not
    #: threaded (the honest legacy arm), the result has zero trades, the
    #: window holds zero trades, or the windowed std-dev is 0/undefined.
    trailing_sharpe_ratio: float | None = None
    #: Profit factor over the same trailing window — the main
    #: ``profit_factor`` law token-for-token on the windowed folds
    #: (``None`` when the windowed gross loss is zero). ``None`` also when
    #: the anchor is absent or the window holds zero trades.
    trailing_profit_factor: float | None = None
    #: Net P&L (int paisa, exact) over the same trailing window. ``None``
    #: when the anchor is absent or the window holds zero trades (honest
    #: absence — a windowed 0 means measured cancellation, never "no data").
    trailing_net_pnl_paisa: int | None = None
    #: WHOLE-HISTORY exponentially decay-weighted net P&L (paisa, float):
    #: ``Σ_buckets pnl · 0.5^(age_months / half_life)`` over the integer
    #: age-month buckets of :func:`fold_recency` (age from the GLOBAL
    #: anchor; half-life :data:`DECAY_HALF_LIFE_MONTHS` by default). Decay
    #: over the WHOLE history is a deliberate ALTERNATIVE to the hard
    #: trailing cutoff — decaying only within the window would double-count
    #: recency (window membership AND weight). ``None`` when the anchor is
    #: absent or the result has zero trades.
    decay_weighted_net_paisa: float | None = None

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


def percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Return the linear-interpolation percentile of a pre-sorted series.

    The ONE COMMON percentile convention (Golden Rule #11): the NumPy
    ``"linear"`` interpolation over a PRE-SORTED sequence. Promoted from the
    module-private ``_percentile`` so downstream observers
    (:mod:`brutex.observability.soak_drift`) share the exact convention instead
    of adding a fourth copy — a p99-drift RATIO built on a mismatched
    nearest-rank vs linear pair would compare apples to oranges
    (DEC-SOAK-REAL-TIER-001). ``_percentile`` remains a back-compat alias.

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


#: Back-compat alias — the pre-DEC-SOAK-REAL-TIER-001 module-private name kept
#: byte-stable for its two internal callers + the existing unit tests.
_percentile = percentile


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
        loss_rate=0.0,
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
        kelly_fraction=None,
        log_growth_at_half_kelly=None,
        # Zero trades ⇒ zero SL exits of zero trades — a vacuous truth, so
        # 0.0 is honest here regardless of counter availability (the
        # win_rate=0.0 precedent; DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B).
        sl_hit_rate=0.0,
    )


def _kelly_growth(
    n_trades: int,
    winners: int,
    losers: int,
    gross_profit_paisa: int,
    gross_loss_abs_paisa: int,
) -> tuple[float | None, float | None]:
    """Return ``(kelly_fraction, log_growth_at_half_kelly)`` from fold scalars.

    DEC-METRICS-KELLY-GROWTH-001. The Kelly criterion in the map's PINNED
    sizing-overlay form ``f* = W − (1−W)/R`` with ``W`` the win rate and
    ``R`` the win/loss (payoff) ratio — computed here in the algebraically
    identical fold-scalar spelling: ``p = winners / n`` (ALL trades in the
    denominator — scratch trades dilute the win probability exactly as they
    dilute ``win_rate``), ``q = 1 − p``, ``b = (gross_profit / winners) /
    (gross_loss / losers)``, ``kelly_fraction = p − q / b``. The companion
    growth figure is the SCALAR CLOSED FORM ``g(f) = p·ln(1 + f·b) +
    q·ln(1 − f)`` evaluated at half-Kelly ``f = f*/2`` (the standard
    estimation-error safety factor) — O(1) per combo BY CONSTRUCTION, never
    an O(trades) pass.

    Degenerate arms (honest ``None``, never a fabricated number — Golden
    Rule #1):

    * no winners OR no losers → no payoff ratio ``b`` exists →
      ``(None, None)``;
    * a zero avg-win or avg-loss magnitude (reachable only through the
      PUBLIC :func:`combine_metrics_from_folds` on a hand-built
      :class:`TradeFolds` — :func:`_fold_trades` guarantees a winner has
      ``pnl > 0`` and a loser ``pnl < 0``) → ``(None, None)``;
    * either half-Kelly log argument non-positive →
      ``(kelly_fraction, None)``. PROVABLY DEAD for any real fold output
      (``p ∈ [0, 1]``, ``b > 0`` ⇒ ``1 + (f/2)·b = (1 + p + p·b)/2 ≥ 1/2``
      and ``1 − f/2 ≥ 1/2`` since ``f ≤ p ≤ 1``) — kept as a DEFENSIVE
      totality guard because :func:`combine_metrics_from_folds` is PUBLIC
      over arbitrary hand-built folds (e.g. ``winners > n_trades`` puts
      ``p > 1`` and can push either argument non-positive).

    COMBINE-side by the fold/combine split's exactness law
    (DEC-METRICS-RUST-COMPUTE-FOLD-001): ``math.log`` is libm territory
    with no cross-libm bit guarantee, so it MUST stay Python; every input
    is an existing fold scalar — NO new fold state, the Rust fold wire is
    untouched.
    """
    if winners == 0 or losers == 0:
        return None, None
    avg_win = gross_profit_paisa / winners
    avg_loss = gross_loss_abs_paisa / losers
    if avg_win == 0.0 or avg_loss == 0.0:
        return None, None
    b = avg_win / avg_loss
    p = winners / n_trades
    q = 1.0 - p
    kelly = p - q / b
    half = kelly / 2.0
    arg_win = 1.0 + half * b
    arg_loss = 1.0 - half
    if arg_win <= 0.0 or arg_loss <= 0.0:
        return kelly, None
    return kelly, p * math.log(arg_win) + q * math.log(arg_loss)


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
    sl_exit_count: int | None = None,
    trailing_anchor: datetime | None = None,
    decay_half_life_months: float = DECAY_HALF_LIFE_MONTHS,
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
    sl_exit_count
        Optional stop-loss-exit counter for ``trades``
        (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B — threaded like
        ``drawdown``; see :func:`combine_metrics_from_folds`). ``None``
        (the default) yields the honest ``sl_hit_rate=None`` on a
        non-empty trade set — never a fabricated ``0.0``.
    trailing_anchor
        The GLOBAL recency anchor (DEC-METRICS-TRAILING-WINDOW-001) — the
        run's data-end instant, threaded as a per-run parameter (never
        derived per-combo from the trades). ``None`` (the default) leaves
        every recency field ``None`` — the honest legacy arm. Must be
        tz-aware when supplied (see :func:`fold_recency`).
    decay_half_life_months
        The decay composite's half-life in age-month buckets
        (:data:`DECAY_HALF_LIFE_MONTHS` by default; see
        :func:`combine_recency_from_folds`). Read only when
        ``trailing_anchor`` is supplied.

    Returns
    -------
    ResultMetrics
        The fully-populated (minus Stage-4) metrics row.

    Raises
    ------
    ValueError
        If ``total_bars < 1`` or ``sl_exit_count`` is out of range (see
        :func:`combine_metrics_from_folds`).

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
        sl_exit_count=sl_exit_count,
        trailing_anchor=trailing_anchor,
        decay_half_life_months=decay_half_life_months,
    )


def compute_metrics_from_scalars(
    trades: Sequence[ClosedTrade],
    total_bars: int,
    benchmark_return_pct: float = 0.0,
    *,
    start_equity_paisa: int,
    span_years: float,
    drawdown: tuple[int, float, int, float],
    sl_exit_count: int | None = None,
    trailing_anchor: datetime | None = None,
    decay_half_life_months: float = DECAY_HALF_LIFE_MONTHS,
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

    ``sl_exit_count`` threads exactly as on :func:`compute_metrics`
    (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B — the sweep's kernel-arm
    tail passes the mask's stop-loss counter here), and so do
    ``trailing_anchor`` / ``decay_half_life_months``
    (DEC-METRICS-TRAILING-WINDOW-001 — the GLOBAL recency anchor +
    half-life; ``None`` anchor = every recency field ``None``, the honest
    legacy arm).

    Raises
    ------
    ValueError
        If ``start_equity_paisa <= 0``, ``span_years`` is not a strictly
        positive finite float, ``total_bars < 1``, or ``sl_exit_count`` is
        out of range (see :func:`combine_metrics_from_folds`).
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
        sl_exit_count=sl_exit_count,
        trailing_anchor=trailing_anchor,
        decay_half_life_months=decay_half_life_months,
    )


@dataclass(frozen=True, slots=True)
class TradeFolds:
    """Per-trade FOLD outputs of the metric body (DEC-SWEEP-COMPUTE-FOLD-DECOMP-001).

    The single-pass / per-trade accumulations of
    :func:`_compute_metrics_impl`, extracted verbatim into ONE value object
    so the metric body reads ``combine(fold(trades))`` — the Slice-0
    fold/combine parameterisation of the Rust sweep-core conversion
    (design pack ``docs/design/sweep_core/`` on branch
    ``claude/rust-sweep-core``). Behaviour-identical: every field is the
    exact intermediate the pre-decomposition body computed inline, in the
    same operation order (the byte-parity golden suite pins it).

    Field groups mirror the design's fold units:

    * **Integer trade aggregates** (exact int arithmetic) —
      ``n_trades`` / ``net_pnl_paisa`` / ``gross_profit_paisa`` /
      ``gross_loss_abs_paisa`` / ``winners`` / ``losers`` /
      ``sum_mfe_paisa`` / ``sum_mae_paisa`` / ``sum_holding_bars`` /
      ``max_holding_bars`` / ``largest_loss_paisa`` (most-negative pnl,
      stored signed) plus the streak folds ``max_consecutive_losses`` /
      ``max_consecutive_wins`` (:func:`_consecutive_runs`).
    * **Welford/Pébay moments** over the per-trade return series —
      ``moments`` (:func:`_accumulate_moments` output, untouched).
    * **Downside Σr²** — ``downside_sum_sq``, the raw
      ``sum(r * r for r in downside)`` term of the Sortino denominator
      (the ``sqrt(Σ/n)`` finish stays combine-side; ``builtins.sum`` over
      an empty downside yields the int ``0``, kept as-is — ``0 / n``
      reproduces the original ``0.0`` bit-for-bit).
    * **Percentile folds** — ``p95`` / ``p5`` of the sorted return series
      (:func:`percentile`, NumPy ``"linear"`` convention).
    """

    n_trades: int
    net_pnl_paisa: int
    gross_profit_paisa: int
    gross_loss_abs_paisa: int  # accumulated as a non-negative magnitude
    winners: int
    losers: int
    sum_mfe_paisa: int
    sum_mae_paisa: int
    sum_holding_bars: int
    max_holding_bars: int
    largest_loss_paisa: int  # most-negative pnl, stored signed
    max_consecutive_losses: int
    max_consecutive_wins: int
    moments: Moments
    downside_sum_sq: float
    p95: float
    p5: float


def _fold_trades(trades: Sequence[ClosedTrade], start_equity_paisa: int) -> TradeFolds:
    """Run the per-trade FOLD pass and return its :class:`TradeFolds`.

    The verbatim per-trade accumulations of the pre-decomposition
    :func:`_compute_metrics_impl` body — the single-pass integer
    aggregates, the streak folds, the per-trade return series with its
    Welford/Pébay moments, the downside Σr² term, and the p95/p5
    percentile folds. No numeric change, no reordering of any summation
    (the byte-parity golden suite pins it). Callers guarantee ``trades``
    is non-empty (the zero-trade row is :func:`_empty_metrics`, the
    caller's arm) and ``start_equity_paisa > 0``.
    """
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
    # this path (the zero-trade row is the caller's arm), so the running max
    # is >= 1 regardless of an initial 0 or 1 — the initialiser is unobservable.
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
    max_losses, max_wins = _consecutive_runs(trades)

    # Return series for the moment-based ratios = per-trade returns as a
    # fraction of starting capital. Zero risk-free rate (DEC-METRICS-001).
    returns = [t.pnl_paisa / start_equity_paisa for t in trades]
    moments = _accumulate_moments(returns)
    # Downside Σr² for the Sortino denominator (spec line 541 "downside-only
    # volatility"): the RMS finish (÷ n, sqrt) stays combine-side.
    # pragma justification (DEC-MUTATION-GATE-COMPUTE-001): the strict "<"
    # boundary is unobservable — an r == 0.0 admitted by a "<=" flip would
    # contribute r * r == 0.0 to the sum. The filter threshold's REAL
    # mutants (0.0 -> 1.0 would admit positive returns) stay pinned by
    # test_sortino_known_value's exact-value assertion.
    downside = [r for r in returns if r < 0.0]  # pragma: no mutate
    downside_sum_sq = sum(r * r for r in downside)
    # tail_ratio inputs = p95 / p5 (spec line 576) of the per-trade returns.
    sorted_returns = sorted(returns)
    p95 = _percentile(sorted_returns, 95.0)
    p5 = _percentile(sorted_returns, 5.0)

    return TradeFolds(
        n_trades=n_trades,
        net_pnl_paisa=net_pnl,
        gross_profit_paisa=gross_profit,
        gross_loss_abs_paisa=gross_loss_abs,
        winners=winners,
        losers=losers,
        sum_mfe_paisa=sum_mfe,
        sum_mae_paisa=sum_mae,
        sum_holding_bars=sum_holding,
        max_holding_bars=max_holding,
        largest_loss_paisa=largest_loss,
        max_consecutive_losses=max_losses,
        max_consecutive_wins=max_wins,
        moments=moments,
        downside_sum_sq=downside_sum_sq,
        p95=p95,
        p5=p5,
    )


def combine_metrics_from_folds(
    folds: TradeFolds,
    *,
    total_bars: int,
    benchmark_return_pct: float,
    start_equity_paisa: int,
    span_years: float,
    drawdown: tuple[int, float, int, float],
    sl_exit_count: int | None = None,
) -> ResultMetrics:
    """COMBINE a :class:`TradeFolds` into the :class:`ResultMetrics` row.

    The verbatim scalar tail of the pre-decomposition
    :func:`_compute_metrics_impl` body — every pre-existing expression below
    is token-for-token the original, reading the fold intermediates from
    ``folds`` instead of inline locals (the byte-parity golden suite pins
    bit-identity; DEC-SWEEP-COMPUTE-FOLD-DECOMP-001). The ADDITIVE
    ``loss_rate`` + Kelly/growth expressions
    (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 / DEC-METRICS-KELLY-GROWTH-001) are
    combine-side by the same law — pure functions of existing fold scalars
    (NO new fold state; the Rust fold wire is untouched), with the libm
    ``math.log`` staying Python per the fold/combine exactness split. Requires
    ``folds.n_trades >= 1`` — the zero-trade row is
    :func:`_empty_metrics`, the caller's own arm (mirroring the sweep
    core's ``zero_trade`` arm in the design) — and that requirement is
    enforced FAIL-LOUD below (a Slice-0 round-1 review fold: this is
    now PUBLIC API, so contract misuse raises a typed :class:`ValueError`
    naming the caller's arm instead of an untyped ``ZeroDivisionError``
    deep in the body; the guard sits BEFORE the verbatim tail, so every
    valid input's bytes are untouched). The same scalar validity contract
    as :func:`_compute_metrics_impl` (``total_bars >= 1``,
    ``start_equity_paisa > 0``, finite positive ``span_years``) stays the
    caller's own boundary.

    ``sl_exit_count`` (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B) is the
    mask's stop-loss-exit counter — position
    ``brutex.engine.trade_sim.STOP_LOSS_REASON_INDEX`` of the trade-sim's
    per-mask exit-reason counters, extracted by the ENGINE caller (this
    module stays below the engine layer, so the vocabulary position never
    duplicates here). Threaded like the ``drawdown`` 4-tuple: a pure
    COMBINE-side scalar, NO new fold state. ``None`` (the default — a
    caller without reason attribution) yields the honest
    ``sl_hit_rate=None``, never a fabricated ``0.0``.

    Raises
    ------
    ValueError
        If ``folds.n_trades < 1`` — the zero-trade row is
        :func:`_empty_metrics`, computed by the caller — or a supplied
        ``sl_exit_count`` is outside ``[0, folds.n_trades]`` (the counters
        sum to the trade count by construction, so an out-of-range value is
        a threading bug, refused loudly — never a silently wrong rate).

    Spec citations live on the inline comments; conventions where the
    spec is silent are recorded in DEC-METRICS-001 (MIGRATION.md #305).
    """
    if folds.n_trades < 1:
        raise ValueError(
            f"combine_metrics_from_folds: folds.n_trades must be >= 1, got "
            f"{folds.n_trades} — the zero-trade row is _empty_metrics, the caller's arm"
        )
    if sl_exit_count is not None and not 0 <= sl_exit_count <= folds.n_trades:
        raise ValueError(
            f"combine_metrics_from_folds: sl_exit_count={sl_exit_count} is outside "
            f"[0, {folds.n_trades}] (n_trades) — the exit-reason counters sum to the "
            f"trade count by construction, so this is a threading bug"
        )
    n_trades = folds.n_trades
    net_pnl = folds.net_pnl_paisa
    gross_profit = folds.gross_profit_paisa
    gross_loss_abs = folds.gross_loss_abs_paisa
    winners = folds.winners
    losers = folds.losers
    sum_mfe = folds.sum_mfe_paisa
    sum_mae = folds.sum_mae_paisa
    sum_holding = folds.sum_holding_bars
    max_holding = folds.max_holding_bars
    largest_loss = folds.largest_loss_paisa
    moments = folds.moments

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
    max_losses = folds.max_consecutive_losses
    max_wins = folds.max_consecutive_wins
    # recovery_factor = net profit / MDD (spec line 551). None if MDD == 0.
    recovery_factor = _safe_ratio(float(net_pnl), float(max_dd_paisa))

    # ── Trade quality (spec lines 557-568) ───────────────────────────
    win_rate = winners / n_trades  # fraction, per spec line 561
    # loss_rate: the losing-side mirror of win_rate — same denominator, same
    # fraction convention (DEC-METRICS-LOSSRATE-FIRSTCLASS-001; scratch
    # trades count in neither numerator, so 1 − win_rate would be WRONG).
    loss_rate = losers / n_trades
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
    # The per-trade return series' moments come from the fold pass
    # (`TradeFolds.moments` — zero risk-free rate, DEC-METRICS-001).
    std_dev = moments.std_dev
    # Sharpe = mean(return) / std(return), no annualisation multiplier
    # (DEC-METRICS-001: per-trade-return ratio; spec lists cagr_pct
    # separately as the annualised figure). None when std is 0/undefined.
    sharpe = None if std_dev is None or std_dev == 0.0 else moments.mean / std_dev
    # Sortino = mean(return) / downside-deviation, where downside
    # deviation is the RMS of negative returns about a zero target
    # (spec line 541 "downside-only volatility"); the Σr² term is the
    # fold's `downside_sum_sq`. The RMS arithmetic stays mutable;
    # n_trades >= 1 here (zero-trade rows are the caller's arm), so no
    # division guard is needed.
    downside_dev = math.sqrt(folds.downside_sum_sq / n_trades)
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
    # tail_ratio = p95 / |p5| (spec line 576) of the per-trade return
    # series — both percentiles are fold outputs.
    tail_ratio = _safe_ratio(folds.p95, abs(folds.p5))
    skewness = _skewness(moments)
    kurtosis = _excess_kurtosis(moments)

    # ── Growth metrics (DEC-METRICS-KELLY-GROWTH-001) ────────────────
    # Kelly fraction + half-Kelly log growth from the SAME fold scalars —
    # combine-side by the libm law (math.log; see _kelly_growth).
    kelly_fraction, log_growth_at_half_kelly = _kelly_growth(
        n_trades, winners, losers, gross_profit, gross_loss_abs
    )

    # ── Exit quality (DEC-METRICS-LOSSRATE-FIRSTCLASS-001 Stage B) ───
    # sl_hit_rate = SL exits / all closed trades — pure COMBINE arithmetic
    # over the threaded counter; None (absent counters) stays the honest
    # unknown, never a fabricated 0.0 (Golden Rule #1).
    sl_hit_rate = None if sl_exit_count is None else sl_exit_count / n_trades

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
        sl_hit_rate=sl_hit_rate,
    )


def _resolve_folds(trades: Sequence[ClosedTrade], start_equity_paisa: int) -> TradeFolds:
    """Serve the FOLD half from the flag-gated Rust kernel or the Python arm.

    The ONE dispatch chokepoint of DEC-METRICS-RUST-COMPUTE-FOLD-001: with
    ``BRUTEX_METRICS_RUST_FOLD`` OFF (the default) this is exactly the
    verbatim :func:`_fold_trades` call the pre-seam composition made —
    byte-identical by construction (the never-routes bomb-kernel spy pins
    it). With the flag ON, :func:`brutex.metrics.compute_rust.
    fold_wire_via_kernel` serves a bit-identical plain-data wire
    (:func:`_folds_from_wire` builds the :class:`TradeFolds` HERE — the
    seam leaf imports nothing from this module)
    (burn-in self-checked, autorevert-latched) and ANY kernel fault —
    missing/stale wheel, kernel raise, structurally invalid result, the
    autorevert latch — degrades fail-closed to the SAME Python arm
    (``MemoryError`` re-raises raw). The COMBINE tail is Python on both
    arms, so the split's bit-parity is the fold's alone (the seam module
    docstring carries the exactness argument).
    """
    # LAZY import (CodeQL py/cyclic-import — the #1533 lake_revision
    # precedent): the seam leaf imports NOTHING from this module in any
    # form (the plain-data FoldWire boundary + the injected oracle below),
    # so this single lazy edge is cycle-free by construction. Function-
    # local resolution is a sys.modules dict hit after the first call —
    # the same per-call order as the env read the seam already performs.
    from brutex.metrics import compute_rust

    if not compute_rust.metrics_rust_fold_enabled():
        return _fold_trades(trades, start_equity_paisa)
    wire = compute_rust.fold_wire_via_kernel(
        trades,
        start_equity_paisa,
        # The burn-in oracle, INJECTED (dependency inversion — the leaf
        # never imports this module): the Python fold's wire projection
        # for exactly these arguments.
        oracle_wire=lambda: _fold_wire(_fold_trades(trades, start_equity_paisa)),
    )
    if wire is None:
        return _fold_trades(trades, start_equity_paisa)
    return _folds_from_wire(wire)


def _fold_wire(folds: TradeFolds) -> FoldWire:
    """Project a :class:`TradeFolds` onto the seam's plain-data wire shape.

    The pure inverse pair of :func:`_folds_from_wire` (round-trip pinned by
    the pair suite). ``downside_sum_sq`` is float-normalised here: the
    Python fold legitimately holds the int ``0`` on an empty downside
    (``sum([])``) where the kernel wire carries ``+0.0``; the two are
    combine-equivalent (``0 / n == 0.0 / n == +0.0``, pinned by the golden
    suite's ``scratch_only`` corpus case) and ``float(0)`` is ``+0.0``, so
    the projection equates exactly that pair and nothing else.
    """
    moments = folds.moments
    return (
        (
            folds.n_trades,
            folds.winners,
            folds.losers,
            folds.max_holding_bars,
            folds.largest_loss_paisa,
            folds.max_consecutive_losses,
            folds.max_consecutive_wins,
        ),
        (
            folds.net_pnl_paisa,
            folds.gross_profit_paisa,
            folds.gross_loss_abs_paisa,
            folds.sum_mfe_paisa,
            folds.sum_mae_paisa,
            folds.sum_holding_bars,
        ),
        (moments.n, moments.mean, moments.m2, moments.m3, moments.m4),
        (float(folds.downside_sum_sq), folds.p95, folds.p5),
    )


def _folds_from_wire(wire: FoldWire) -> TradeFolds:
    """Build the :class:`TradeFolds` a seam-served wire denotes.

    THIS module owns the ``TradeFolds`` construction (the plain-data
    boundary of DEC-METRICS-RUST-COMPUTE-FOLD-001 — the seam leaf speaks
    only tuples and imports nothing from here). Values pass through
    UNCHANGED — bit-identity is the seam's burn-in + the golden suites'
    proof, never a re-derivation here.
    """
    counts, sums, moments_wire, floats = wire
    return TradeFolds(
        n_trades=counts[0],
        net_pnl_paisa=sums[0],
        gross_profit_paisa=sums[1],
        gross_loss_abs_paisa=sums[2],
        winners=counts[1],
        losers=counts[2],
        sum_mfe_paisa=sums[3],
        sum_mae_paisa=sums[4],
        sum_holding_bars=sums[5],
        max_holding_bars=counts[3],
        largest_loss_paisa=counts[4],
        max_consecutive_losses=counts[5],
        max_consecutive_wins=counts[6],
        moments=Moments(
            n=moments_wire[0],
            mean=moments_wire[1],
            m2=moments_wire[2],
            m3=moments_wire[3],
            m4=moments_wire[4],
        ),
        downside_sum_sq=floats[0],
        p95=floats[1],
        p5=floats[2],
    )


@dataclass(frozen=True, slots=True)
class RecencyFolds:
    """FOLD outputs of the recency pass (DEC-METRICS-TRAILING-WINDOW-001).

    The Stage-C fold state, kept a SEPARATE additive structure — the frozen
    :class:`TradeFolds` is untouched (the #1605 field-pairing obligation
    binds its shape to the Rust fold wire; extending it would force a wire
    re-key for state only the recency pass reads).

    Attributes
    ----------
    trailing
        A plain :class:`TradeFolds` over ONLY the trades whose ``exit_ts``
        falls inside the trailing window (``exit_ts >= anchor −``
        :data:`TRAILING_WINDOW`), or ``None`` when the window holds zero
        trades. The SAME fold the main pass runs — a Rust twin reuses the
        existing fold kernel over the filtered subsequence verbatim.
    decay_buckets
        The decay pass's ONLY new fold state: ``(age_month_bucket,
        net_pnl_paisa)`` pairs, SORTED ascending by bucket key. Pure integer
        accumulation (the fold/combine exactness law — no libm fold-side):
        bucket key ``max(0, (anchor − exit_ts).days) //``
        :data:`DECAY_BUCKET_DAYS`, value the exact int sum of the bucket's
        ``pnl_paisa``. Sorted so the combine's float summation order is
        DETERMINISTIC and independent of trade order / any future hash-map
        Rust twin's iteration order.
    """

    trailing: TradeFolds | None
    decay_buckets: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class RecencyMetrics:
    """COMBINE outputs of the recency pass — the four additive row values.

    Field semantics are documented on the matching :class:`ResultMetrics`
    fields (DEC-METRICS-TRAILING-WINDOW-001).
    """

    trailing_sharpe_ratio: float | None
    trailing_profit_factor: float | None
    trailing_net_pnl_paisa: int | None
    decay_weighted_net_paisa: float


def fold_recency(
    trades: Sequence[ClosedTrade],
    start_equity_paisa: int,
    *,
    trailing_anchor: datetime,
) -> RecencyFolds:
    """Run the recency FOLD pass (DEC-METRICS-TRAILING-WINDOW-001).

    Window membership is judged on each trade's EXIT instant — the exact
    timestamp compare ``exit_ts >= trailing_anchor − TRAILING_WINDOW``
    (the simplest defensible law: exit-date semantics like the
    year fold ``combo_aggregates`` keys on ``ist_trade_date(exit_ts)``, but
    as an instant compare — no IST date truncation, so the cutoff boundary
    is exact and tz-arithmetic-free; a trade exiting exactly AT the cutoff
    instant is IN the window). ``trailing_anchor`` is the GLOBAL per-run
    anchor (the loaded bar range's end), never a per-combo maximum.

    The decay buckets accumulate exact INTEGER P&L sums keyed by whole
    30-day months of age at exit (:data:`DECAY_BUCKET_DAYS`;
    ``timedelta.days`` floors, and a defensively negative age — an exit at
    or after the anchor — clamps to bucket 0). The exponential weights are
    applied COMBINE-side (:func:`combine_recency_from_folds`) so the fold
    stays libm-free and IEEE-exact — the bit-identity law the Rust fold
    twin must satisfy.

    Callers guarantee ``trades`` is non-empty and ``start_equity_paisa > 0``
    (the zero-trade row is :func:`_empty_metrics`, the caller's arm —
    mirroring :func:`_fold_trades`' contract), with ONE sanctioned
    exception: :func:`_compute_metrics_impl`'s zero-trade arm invokes this
    fold VALIDATION-ONLY over the empty series (the empty fold is
    well-defined — ``trailing=None``, no buckets — and the result is
    discarded) so the tz-aware anchor law fires on both arms.

    Raises
    ------
    ValueError
        If ``trailing_anchor`` is naive — ``ClosedTrade.exit_ts`` is
        validated tz-aware, and a naive/aware comparison would raise a bare
        ``TypeError`` deep in the loop; refused loudly at the boundary
        instead.
    """
    if trailing_anchor.tzinfo is None:
        raise ValueError(
            f"fold_recency: trailing_anchor must be tz-aware (ClosedTrade.exit_ts is), "
            f"got naive {trailing_anchor!r}"
        )
    cutoff = trailing_anchor - TRAILING_WINDOW
    windowed = [trade for trade in trades if trade.exit_ts >= cutoff]
    trailing = _fold_trades(windowed, start_equity_paisa) if windowed else None
    buckets: dict[int, int] = {}
    for trade in trades:
        age_days = (trailing_anchor - trade.exit_ts).days
        key = max(0, age_days) // DECAY_BUCKET_DAYS
        buckets[key] = buckets.get(key, 0) + trade.pnl_paisa
    return RecencyFolds(trailing=trailing, decay_buckets=tuple(sorted(buckets.items())))


def _resolve_recency_folds(
    trades: Sequence[ClosedTrade],
    start_equity_paisa: int,
    *,
    trailing_anchor: datetime,
) -> RecencyFolds:
    """Serve the recency FOLD from the flag-gated Rust twin or the Python arm.

    The recency dispatch chokepoint (DEC-METRICS-TRAILING-WINDOW-001
    Stage-C2) — the exact :func:`_resolve_folds` shape over the SAME
    ``BRUTEX_METRICS_RUST_FOLD`` flag: OFF (the default) is the verbatim
    :func:`fold_recency` call — byte-identical by construction (the
    never-routes bomb-kernel spy pins it); ON routes through
    :func:`brutex.metrics.compute_rust.recency_wire_via_kernel`
    (burn-in self-checked on its own budget, SHARED autorevert latch) and
    ANY kernel fault degrades fail-closed to the SAME Python arm
    (``MemoryError`` re-raises raw). A NAIVE anchor takes the Python arm
    unconditionally so :func:`fold_recency`'s own boundary ``ValueError``
    law holds verbatim (the seam's epoch conversion would raise a bare
    ``TypeError`` instead). The window bounds cross as exact epoch µs —
    ``cutoff = anchor − TRAILING_WINDOW`` computed HERE with the verbatim
    Python expression, then converted on the seam's one epoch basis.
    """
    from brutex.metrics import compute_rust

    if trailing_anchor.tzinfo is None or not compute_rust.metrics_rust_fold_enabled():
        return fold_recency(trades, start_equity_paisa, trailing_anchor=trailing_anchor)
    anchor_us = compute_rust.datetime_to_epoch_us(trailing_anchor)
    cutoff_us = compute_rust.datetime_to_epoch_us(trailing_anchor - TRAILING_WINDOW)
    wire = compute_rust.recency_wire_via_kernel(
        trades,
        start_equity_paisa,
        anchor_us=anchor_us,
        cutoff_us=cutoff_us,
        # The burn-in oracle, INJECTED (dependency inversion — the leaf
        # never imports this module): the Python recency fold's wire
        # projection for exactly these arguments.
        oracle_wire=lambda: _recency_wire(
            fold_recency(trades, start_equity_paisa, trailing_anchor=trailing_anchor)
        ),
    )
    if wire is None:
        return fold_recency(trades, start_equity_paisa, trailing_anchor=trailing_anchor)
    return _recency_folds_from_wire(wire)


def _recency_wire(folds: RecencyFolds) -> RecencyWire:
    """Project a :class:`RecencyFolds` onto the seam's plain-data wire shape.

    The pure inverse pair of :func:`_recency_folds_from_wire` (round-trip
    pinned by the pair suite): the trailing half through :func:`_fold_wire`
    (``None`` passes through), the decay buckets verbatim — already the
    wire's sorted exact-int tuple form.
    """
    return (
        None if folds.trailing is None else _fold_wire(folds.trailing),
        folds.decay_buckets,
    )


def _recency_folds_from_wire(wire: RecencyWire) -> RecencyFolds:
    """Build the :class:`RecencyFolds` a seam-served recency wire denotes.

    THIS module owns the construction (the plain-data boundary — the seam
    leaf speaks only tuples). Values pass through UNCHANGED — bit-identity
    is the seam's burn-in + the differential suites' proof.
    """
    trailing_wire, buckets = wire
    return RecencyFolds(
        trailing=None if trailing_wire is None else _folds_from_wire(trailing_wire),
        decay_buckets=buckets,
    )


def combine_recency_from_folds(
    folds: RecencyFolds,
    *,
    decay_half_life_months: float = DECAY_HALF_LIFE_MONTHS,
) -> RecencyMetrics:
    """COMBINE a :class:`RecencyFolds` into the four recency row values.

    The trailing trio mirrors the main combine's expressions token-for-token
    on the WINDOWED folds (DEC-METRICS-TRAILING-WINDOW-001):

    * ``trailing_net_pnl_paisa`` — the windowed ``net_pnl_paisa`` (exact int).
    * ``trailing_profit_factor`` — ``_safe_ratio(gross_profit,
      gross_loss_abs)``, the main ``profit_factor`` law (``None`` when the
      windowed gross loss is zero).
    * ``trailing_sharpe_ratio`` — the main Sharpe law on the windowed
      moments: ``None if std_dev is None or std_dev == 0.0 else mean /
      std_dev`` (per-trade-return ratio, zero risk-free rate, NO
      annualisation multiplier — DEC-METRICS-001 verbatim, so no span choice
      arises; the windowed return series shares the full history's
      ``start_equity_paisa`` denominator by construction of the fold).

    All three are ``None`` when the window held zero trades
    (``folds.trailing is None``) — expected and honest, never fabricated.

    The decay composite applies the exponential weights libm-side (here —
    the fold stays integer-exact): ``Σ pnl · 0.5^(bucket / half_life)``
    over the SORTED buckets, whose fixed ascending-key order makes the IEEE
    float summation deterministic.

    Raises
    ------
    ValueError
        If ``decay_half_life_months`` is not a FINITE positive float
        (NaN/±inf/zero/negative refused). ``math.inf`` — formerly accepted
        as the weight-1.0 plain-net limit — is REFUSED since the round-1
        refuter (DEC-METRICS-TRAILING-WINDOW-001): validation symmetry —
        ``NaN`` was already refused via the ``> 0`` check, so a silently
        accepted ``inf`` that degenerates decay to the plain net was the
        asymmetric gap. A caller wanting the plain net reads
        ``net_pnl_paisa``; a near-plain-net decay uses a large FINITE
        half-life (the documented tolerance story).
    """
    if not (math.isfinite(decay_half_life_months) and decay_half_life_months > 0.0):
        raise ValueError(
            f"combine_recency_from_folds: decay_half_life_months must be finite and > 0, "
            f"got {decay_half_life_months}"
        )
    trailing = folds.trailing
    if trailing is None:
        trailing_sharpe: float | None = None
        trailing_pf: float | None = None
        trailing_net: int | None = None
    else:
        trailing_net = trailing.net_pnl_paisa
        # The main profit_factor law token-for-token on the windowed folds.
        trailing_pf = _safe_ratio(
            float(trailing.gross_profit_paisa), float(trailing.gross_loss_abs_paisa)
        )
        # The main Sharpe law token-for-token on the windowed moments.
        std_dev = trailing.moments.std_dev
        trailing_sharpe = (
            None if std_dev is None or std_dev == 0.0 else trailing.moments.mean / std_dev
        )
    decay = 0.0
    for bucket, pnl in folds.decay_buckets:
        decay += pnl * 0.5 ** (bucket / decay_half_life_months)
    return RecencyMetrics(
        trailing_sharpe_ratio=trailing_sharpe,
        trailing_profit_factor=trailing_pf,
        trailing_net_pnl_paisa=trailing_net,
        decay_weighted_net_paisa=decay,
    )


def _compute_metrics_impl(
    trades: Sequence[ClosedTrade],
    total_bars: int,
    benchmark_return_pct: float,
    *,
    start_equity_paisa: int,
    span_years: float,
    drawdown: tuple[int, float, int, float],
    sl_exit_count: int | None = None,
    trailing_anchor: datetime | None = None,
    decay_half_life_months: float = DECAY_HALF_LIFE_MONTHS,
) -> ResultMetrics:
    """Shared metric body behind :func:`compute_metrics` / the scalar entry.

    The verbatim pre-#1186 :func:`compute_metrics` body with the three
    ``equity`` reads replaced by the already-extracted scalars and the
    :func:`_drawdown_stats` call replaced by the supplied ``drawdown``
    tuple — since DEC-SWEEP-COMPUTE-FOLD-DECOMP-001 (Slice 0 of the Rust
    sweep-core conversion) expressed as the pure composition
    ``combine_metrics_from_folds(_fold_trades(...))`` over the SAME
    expressions, still a pure parameterisation with no behaviour change
    (the golden + byte-parity suites pin it) — the fold half served
    through the :func:`_resolve_folds` dispatch since
    DEC-METRICS-RUST-COMPUTE-FOLD-001 (flag OFF ⇒ the verbatim
    ``_fold_trades`` call; flag ON ⇒ the bit-identical Rust fold with a
    fail-closed degrade to the same arm). Callers validate their own
    boundary; this body assumes ``total_bars >= 1`` and a valid scalar
    set. The zero-trade row stays this composition root's own arm
    (:func:`_empty_metrics`), mirroring the sweep core's ``zero_trade``
    arm — :func:`combine_metrics_from_folds` requires ``n_trades >= 1``.
    With a supplied ``trailing_anchor`` the recency pass
    (DEC-METRICS-TRAILING-WINDOW-001) runs as a SECOND fold/combine —
    :func:`fold_recency` → :func:`combine_recency_from_folds` — and its four
    values replace the base row's ``None`` defaults; an absent anchor is
    the honest legacy arm (every recency field ``None``).
    """
    if not trades:
        if sl_exit_count not in (None, 0):
            # The counters sum to the trade count by construction, so a
            # positive SL counter over ZERO trades is a threading bug —
            # refused loudly, mirroring combine_metrics_from_folds' bound.
            raise ValueError(
                f"sl_exit_count={sl_exit_count} supplied for a zero-trade result — "
                f"the exit-reason counters sum to the trade count, so this is a "
                f"threading bug"
            )
        if trailing_anchor is not None:
            # The recency validators run on the zero-trade arm too (a naive
            # anchor / non-positive half-life must fail loudly on BOTH arms,
            # never masked by the empty return): the REAL owners are invoked
            # over the empty series (well-defined — ``trailing=None``, no
            # buckets; O(1), result discarded) so the boundary laws + their
            # messages keep exactly ONE home each.
            combine_recency_from_folds(
                fold_recency(trades, start_equity_paisa, trailing_anchor=trailing_anchor),
                decay_half_life_months=decay_half_life_months,
            )
        return _empty_metrics(drawdown)
    base = combine_metrics_from_folds(
        _resolve_folds(trades, start_equity_paisa),
        total_bars=total_bars,
        benchmark_return_pct=benchmark_return_pct,
        start_equity_paisa=start_equity_paisa,
        span_years=span_years,
        drawdown=drawdown,
        sl_exit_count=sl_exit_count,
    )
    if trailing_anchor is None:
        # The honest legacy arm (DEC-METRICS-TRAILING-WINDOW-001): a caller
        # without a global anchor reads every recency field as None — never
        # a per-combo max-exit fabrication.
        return base
    recency = combine_recency_from_folds(
        _resolve_recency_folds(trades, start_equity_paisa, trailing_anchor=trailing_anchor),
        decay_half_life_months=decay_half_life_months,
    )
    return replace(
        base,
        trailing_sharpe_ratio=recency.trailing_sharpe_ratio,
        trailing_profit_factor=recency.trailing_profit_factor,
        trailing_net_pnl_paisa=recency.trailing_net_pnl_paisa,
        decay_weighted_net_paisa=recency.decay_weighted_net_paisa,
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
