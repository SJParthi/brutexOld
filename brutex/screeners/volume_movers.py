"""Volume / volatility movers screener (DEC-MOVERS-VOLUME-001).

A **reference-only** screener that ranks the lake's instruments two ways over a
single time window:

* **Volume surge** — how loud an instrument's *recent* bars are versus its own
  *baseline* bars (a relative-volume / "RVOL" style ratio:
  ``mean(recent_volume) / mean(baseline_volume)``).
* **Realised intraday range volatility** — the mean per-bar relative high-low
  range (``mean((high - low) / close)``), a price-level-independent
  range-based volatility proxy.

Scope (CLAUDE.md §3 scope lock)
-------------------------------
This is a **lake / REFERENCE_ONLY** analytic. It NEVER touches the brute-force
engine, the 52-condition vocabulary, or option chains — it only reads OHLCV the
lake already holds and ranks it. Screening F&O-eligible / NIFTY-Total-Market
underlyings here does not admit them to the engine (DEC-NTM-GROWW-REPOINT-001).

Honesty envelope (Golden Rule #1 / #10)
---------------------------------------
* The numbers are only as good as the **ingested** lake — a thin/empty window
  yields thin/empty rankings. Instruments with no bars are reported in
  :attr:`MoversReport.skipped`, never fabricated.
* Per-bar metric accumulation is **O(1) per bar** (a fixed number of exact
  :class:`~decimal.Decimal` operations); the bench gate pins that. The *ranking*
  step is an ``O(m log m)`` sort over ``m`` instruments and the lake read is
  I/O-bound — neither is, nor is claimed to be, O(1)-total.
* Statistical metrics keep full precision and are computed in a **fixed**
  :class:`~decimal.Decimal` context so a run is reproducible bit-for-bit
  regardless of the caller's ambient context (Golden Rule #4 / #7). They are
  NOT rounded to 2dp — that rule is money-only (DEC-MONEY-2DP-001). The single
  money surface, the representative ``last_close``, goes through
  :mod:`brutex.money`.

Design
------
Two layers, so the pure ranking is testable with zero I/O and the orchestration
is testable with a fake reader (dependency injection):

* :func:`compute_mover_metrics` / :func:`rank_by_volume_surge` /
  :func:`rank_by_realised_volatility` / :func:`rank_movers` — **pure**: given
  per-instrument OHLCV, produce the ranked report. No I/O.
* :func:`screen_movers` — loads each instrument's bars via an injected
  :class:`LakeBarReader`, then delegates to :func:`rank_movers`. The reader is
  the ONLY seam that touches the lake; production binds it to the lake
  :class:`~brutex.data.catalog.Catalog` (deferred — UI/wiring is a later task).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, localcontext
from typing import Final, Protocol

from brutex.data.models import CandleBar
from brutex.money import format_inr, rupees_to_paisa

__all__ = [
    "LakeBarReader",
    "MoverMetrics",
    "MoversReport",
    "ScreenTarget",
    "compute_mover_metrics",
    "rank_by_realised_volatility",
    "rank_by_volume_surge",
    "rank_movers",
    "screen_movers",
]

#: Significant digits for the fixed metric :class:`~decimal.Decimal` context.
#: 28 (CPython's default) is "full precision" for a ratio while staying
#: platform- and ambient-context-independent, so a screen is reproducible
#: bit-for-bit (Golden Rule #4 / #7). Statistical metrics are NEVER rounded to
#: the money 2dp grid (DEC-MONEY-2DP-001).
_METRIC_PRECISION: Final[int] = 28

#: The fixed context every metric division runs inside (via
#: :func:`~decimal.localcontext`, which copies it — so it is thread-safe and
#: immune to a caller mutating the global context).
_METRIC_CONTEXT: Final[Context] = Context(prec=_METRIC_PRECISION)

#: A ``Decimal`` zero reused for the volatility accumulator.
_ZERO: Final[Decimal] = Decimal(0)


# ────────────────────────────────────────────────────────────────────────
# Validation helpers (one source of truth — §11 COMMON)
# ────────────────────────────────────────────────────────────────────────


def _require_positive(name: str, value: int) -> None:
    """Raise :class:`ValueError` unless *value* is a strictly positive int."""
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got {value}")


def _require_window(window_start: datetime, window_end: datetime) -> None:
    """Raise :class:`ValueError` unless the window is tz-aware and ordered."""
    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise ValueError("window_start and window_end must be timezone-aware")
    if window_end < window_start:
        raise ValueError(f"window_end ({window_end}) must be >= window_start ({window_start})")


def _require_non_negative_optional(name: str, value: Decimal | None) -> None:
    """Raise :class:`ValueError` if *value* is present and negative."""
    if value is not None and value < _ZERO:
        raise ValueError(f"{name} must be >= 0 when present, got {value}")


# ────────────────────────────────────────────────────────────────────────
# Target identity + injected reader protocol
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ScreenTarget:
    """One lake series to screen — the partition coordinates of an instrument.

    Frozen + hashable so it doubles as a mapping key in :func:`rank_movers`.

    Attributes
    ----------
    exchange
        Exchange token, e.g. ``"NSE"`` / ``"BSE"``.
    segment
        Lake segment, e.g. ``"INDEX"`` / ``"CASH"`` / ``"FNO"``.
    symbol
        Groww symbol, e.g. ``"NSE-NIFTY"`` (DEC-DHAN-GROWW-REMIGRATE-001 §6).
    interval
        Canonical interval label, e.g. ``"1minute"`` / ``"1day"``.

    Raises
    ------
    ValueError
        If any coordinate is an empty string.
    """

    exchange: str
    segment: str
    symbol: str
    interval: str

    def __post_init__(self) -> None:
        for field_name in ("exchange", "segment", "symbol", "interval"):
            if not getattr(self, field_name):
                raise ValueError(f"ScreenTarget.{field_name} must be a non-empty string")


class LakeBarReader(Protocol):
    """The injected lake-read seam — maps a target + window to its OHLCV bars.

    The ONE dependency :func:`screen_movers` touches. Production satisfies it
    by wrapping the lake :class:`~brutex.data.catalog.Catalog` (deferred —
    DEC-MOVERS-VOLUME-001 ships the core + tests, not the UI/wiring); tests
    satisfy it with an in-memory fake. A reader returns the bars in
    chronological order; an empty sequence means "no data for this window"
    (the target is then reported as skipped, never fabricated).
    """

    def read_bars(
        self, target: ScreenTarget, *, start: datetime, end: datetime
    ) -> Sequence[CandleBar]:
        """Return *target*'s chronological bars within ``[start, end]``."""
        raise NotImplementedError


# ────────────────────────────────────────────────────────────────────────
# Per-instrument metrics
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MoverMetrics:
    """One instrument's screener metrics over the window.

    A metric is ``None`` when it is **undefined** for the data (too few bars to
    split, a zero baseline volume, or no positive-close bar) — never a fabricated
    sentinel. ``None`` excludes the row from *that* ranking only.

    Attributes
    ----------
    target
        The screened series.
    bar_count
        Bars seen in the window (``>= 1``; zero-bar targets are skipped, not
        turned into metrics).
    recent_avg_volume, baseline_avg_volume
        Mean volume of the recent window (last ``recent_bars`` bars) and of the
        baseline window (everything before it), or ``None`` when the window is
        too short to split (``bar_count <= recent_bars``).
    volume_surge_ratio
        ``recent_avg_volume / baseline_avg_volume`` — the relative-volume surge.
        ``None`` when the window is too short to split OR the baseline volume is
        zero (an undefined ratio, not "infinite").
    volatility_bar_count
        How many bars had a positive close and so contributed to the volatility
        mean (``0 <= volatility_bar_count <= bar_count``).
    realised_range_volatility
        ``mean((high - low) / close)`` over the positive-close bars — a
        price-level-independent range volatility proxy. ``None`` when no bar had
        a positive close.
    last_close_paisa
        The window's last close as int64 **paisa** (money, via
        :func:`brutex.money.rupees_to_paisa`; ``>= 0``). The screener's only
        money surface — see :attr:`last_close_inr`.

    Raises
    ------
    ValueError
        On a malformed construction: non-positive ``bar_count``,
        ``volatility_bar_count`` outside ``[0, bar_count]``, negative
        ``last_close_paisa``, or a present metric that is negative.
    """

    target: ScreenTarget
    bar_count: int
    recent_avg_volume: Decimal | None
    baseline_avg_volume: Decimal | None
    volume_surge_ratio: Decimal | None
    volatility_bar_count: int
    realised_range_volatility: Decimal | None
    last_close_paisa: int

    def __post_init__(self) -> None:
        if self.bar_count < 1:
            raise ValueError(f"MoverMetrics.bar_count must be >= 1, got {self.bar_count}")
        if not 0 <= self.volatility_bar_count <= self.bar_count:
            raise ValueError(
                "MoverMetrics.volatility_bar_count must be in "
                f"[0, {self.bar_count}], got {self.volatility_bar_count}"
            )
        if self.last_close_paisa < 0:
            raise ValueError(
                f"MoverMetrics.last_close_paisa must be >= 0, got {self.last_close_paisa}"
            )
        _require_non_negative_optional("MoverMetrics.recent_avg_volume", self.recent_avg_volume)
        _require_non_negative_optional("MoverMetrics.baseline_avg_volume", self.baseline_avg_volume)
        _require_non_negative_optional("MoverMetrics.volume_surge_ratio", self.volume_surge_ratio)
        _require_non_negative_optional(
            "MoverMetrics.realised_range_volatility", self.realised_range_volatility
        )

    @property
    def has_volume_surge(self) -> bool:
        """Whether a finite volume-surge ratio is defined for this instrument."""
        return self.volume_surge_ratio is not None

    @property
    def has_realised_volatility(self) -> bool:
        """Whether a realised range volatility is defined for this instrument."""
        return self.realised_range_volatility is not None

    @property
    def last_close_inr(self) -> str:
        """The representative last close as a ``₹`` display string (2dp money)."""
        return format_inr(self.last_close_paisa)


def _volume_surge(
    bars: Sequence[CandleBar], recent_bars: int
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Return ``(recent_avg, baseline_avg, surge_ratio)`` for *bars*.

    The recent window is the last ``recent_bars`` bars; the baseline is every
    bar before it. All three are ``None`` when the window cannot be split
    (``len(bars) <= recent_bars``). When it can, the averages are real and the
    surge is ``recent_avg / baseline_avg`` — except a zero baseline volume
    leaves the surge ``None`` (undefined, not "infinite").
    """
    bar_count = len(bars)
    if bar_count <= recent_bars:
        return None, None, None
    split = bar_count - recent_bars
    baseline_total = sum(bar.volume for bar in bars[:split])
    recent_total = sum(bar.volume for bar in bars[split:])
    with localcontext(_METRIC_CONTEXT):
        recent_avg = Decimal(recent_total) / Decimal(recent_bars)
        baseline_avg = Decimal(baseline_total) / Decimal(split)
        # A zero baseline makes the surge undefined (never a division by zero).
        surge = recent_avg / baseline_avg if baseline_total > 0 else None
    return recent_avg, baseline_avg, surge


def _realised_range_volatility(
    bars: Sequence[CandleBar],
) -> tuple[int, Decimal | None]:
    """Return ``(used, mean_relative_range)`` over the positive-close bars.

    Each contributing bar adds ``(high - low) / close``; bars with a
    non-positive close are skipped (an undefined relative range, never a
    division by zero). The mean is ``None`` when no bar qualifies.
    """
    used = 0
    with localcontext(_METRIC_CONTEXT):
        total = _ZERO
        for bar in bars:
            if bar.close > _ZERO:
                total += (bar.high - bar.low) / bar.close
                used += 1
        volatility = total / Decimal(used) if used > 0 else None
    return used, volatility


def compute_mover_metrics(
    target: ScreenTarget, bars: Sequence[CandleBar], *, recent_bars: int
) -> MoverMetrics:
    """Compute one instrument's :class:`MoverMetrics` from its window OHLCV.

    Pure (no I/O). The bars must be in chronological order — the recent window
    is taken from the tail. This is the per-instrument hot path: a fixed number
    of exact :class:`~decimal.Decimal` operations **per bar** (O(1) per bar).

    Parameters
    ----------
    target
        The screened series identity.
    bars
        The instrument's chronological bars in the window. Must be non-empty —
        a zero-bar target is *skipped* by :func:`rank_movers`, never passed here.
    recent_bars
        Size of the recent window for the volume-surge split (``>= 1``).

    Returns
    -------
    MoverMetrics
        The instrument's metrics (with ``None`` for any undefined metric).

    Raises
    ------
    ValueError
        If ``recent_bars < 1`` or *bars* is empty.
    """
    _require_positive("recent_bars", recent_bars)
    if not bars:
        raise ValueError("compute_mover_metrics requires at least one bar")
    recent_avg, baseline_avg, surge = _volume_surge(bars, recent_bars)
    used, volatility = _realised_range_volatility(bars)
    return MoverMetrics(
        target=target,
        bar_count=len(bars),
        recent_avg_volume=recent_avg,
        baseline_avg_volume=baseline_avg,
        volume_surge_ratio=surge,
        volatility_bar_count=used,
        realised_range_volatility=volatility,
        last_close_paisa=rupees_to_paisa(bars[-1].close),
    )


# ────────────────────────────────────────────────────────────────────────
# Ranking (deterministic order + stable tiebreak)
# ────────────────────────────────────────────────────────────────────────


def _target_sort_key(target: ScreenTarget) -> tuple[str, str, str, str]:
    """The canonical, deterministic ordering key for a target (the tiebreak)."""
    return (target.exchange, target.segment, target.symbol, target.interval)


def _rank_by(
    metrics: Sequence[MoverMetrics],
    extractor: Callable[[MoverMetrics], Decimal | None],
    *,
    top_n: int,
) -> tuple[MoverMetrics, ...]:
    """Return the ``top_n`` metrics by *extractor*, descending, tiebroken stably.

    Rows whose extracted metric is ``None`` (undefined) are excluded. Equal
    metric values break ties by :func:`_target_sort_key` ascending, so the
    ordering is total and reproducible — no reliance on input order.
    """
    _require_positive("top_n", top_n)
    keyed: list[tuple[Decimal, tuple[str, str, str, str], MoverMetrics]] = []
    for metric in metrics:
        value = extractor(metric)
        if value is None:
            continue
        # Negate the metric so a plain ascending sort yields descending value,
        # with the target key as the ascending stable tiebreak.
        keyed.append((-value, _target_sort_key(metric.target), metric))
    keyed.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in keyed[:top_n])


def rank_by_volume_surge(
    metrics: Sequence[MoverMetrics], *, top_n: int
) -> tuple[MoverMetrics, ...]:
    """Return the ``top_n`` instruments by volume-surge ratio (descending).

    Instruments without a defined surge (too short a window, or a zero baseline)
    are excluded. Ties break by :func:`_target_sort_key`.

    Raises
    ------
    ValueError
        If ``top_n < 1``.
    """
    return _rank_by(metrics, lambda metric: metric.volume_surge_ratio, top_n=top_n)


def rank_by_realised_volatility(
    metrics: Sequence[MoverMetrics], *, top_n: int
) -> tuple[MoverMetrics, ...]:
    """Return the ``top_n`` instruments by realised range volatility (descending).

    Instruments without a defined volatility (no positive-close bar) are
    excluded. Ties break by :func:`_target_sort_key`.

    Raises
    ------
    ValueError
        If ``top_n < 1``.
    """
    return _rank_by(metrics, lambda metric: metric.realised_range_volatility, top_n=top_n)


@dataclass(frozen=True, slots=True)
class MoversReport:
    """The screener output: every instrument's metrics + the two rankings.

    Attributes
    ----------
    window_start, window_end
        The screened window (tz-aware, ``window_end >= window_start``).
    recent_bars
        The volume-surge recent-window size used.
    top_n
        The requested ranking depth.
    metrics
        Every screened instrument's :class:`MoverMetrics`, ordered by
        :func:`_target_sort_key` (deterministic, input-order-independent).
    top_volume_surge
        The ``top_n`` instruments by volume surge (descending).
    top_realised_volatility
        The ``top_n`` instruments by realised range volatility (descending).
    skipped
        Targets that had no bars in the window, ordered by
        :func:`_target_sort_key`. Reported honestly, never ranked.
    """

    window_start: datetime
    window_end: datetime
    recent_bars: int
    top_n: int
    metrics: tuple[MoverMetrics, ...]
    top_volume_surge: tuple[MoverMetrics, ...]
    top_realised_volatility: tuple[MoverMetrics, ...]
    skipped: tuple[ScreenTarget, ...]


def rank_movers(
    series: Mapping[ScreenTarget, Sequence[CandleBar]],
    *,
    window_start: datetime,
    window_end: datetime,
    recent_bars: int,
    top_n: int,
) -> MoversReport:
    """Rank a set of already-loaded per-instrument OHLCV series — pure, no I/O.

    The pure core: each target's bars become a :class:`MoverMetrics` (targets
    with an empty series are routed to :attr:`MoversReport.skipped`), then the
    two top-``N`` rankings are produced. ``metrics`` and ``skipped`` are sorted
    by :func:`_target_sort_key`, so the whole report is reproducible regardless
    of mapping iteration order (Golden Rule #4 / #7).

    Parameters
    ----------
    series
        Per-instrument chronological OHLCV, keyed by :class:`ScreenTarget`.
    window_start, window_end
        The window these bars cover (stamped on the report; tz-aware, ordered).
    recent_bars
        The volume-surge recent-window size (``>= 1``).
    top_n
        The ranking depth (``>= 1``).

    Returns
    -------
    MoversReport
        The metrics + both rankings + the skipped (empty) targets.

    Raises
    ------
    ValueError
        If the window is naive/unordered, or ``recent_bars`` / ``top_n`` is
        below 1.
    """
    _require_window(window_start, window_end)
    _require_positive("recent_bars", recent_bars)
    _require_positive("top_n", top_n)

    metrics: list[MoverMetrics] = []
    skipped: list[ScreenTarget] = []
    for target, bars in series.items():
        if not bars:
            skipped.append(target)
            continue
        metrics.append(compute_mover_metrics(target, bars, recent_bars=recent_bars))

    ordered = tuple(sorted(metrics, key=lambda metric: _target_sort_key(metric.target)))
    return MoversReport(
        window_start=window_start,
        window_end=window_end,
        recent_bars=recent_bars,
        top_n=top_n,
        metrics=ordered,
        top_volume_surge=rank_by_volume_surge(ordered, top_n=top_n),
        top_realised_volatility=rank_by_realised_volatility(ordered, top_n=top_n),
        skipped=tuple(sorted(skipped, key=_target_sort_key)),
    )


def screen_movers(
    *,
    reader: LakeBarReader,
    targets: Sequence[ScreenTarget],
    start: datetime,
    end: datetime,
    recent_bars: int,
    top_n: int,
) -> MoversReport:
    """Load each target's bars via *reader*, then rank them (orchestration).

    The thin I/O layer over :func:`rank_movers`: it validates inputs up front
    (failing fast before any read), pulls each target's window OHLCV through the
    injected :class:`LakeBarReader`, and delegates the pure ranking. Duplicate
    targets are rejected so an instrument can never be double-counted.

    Parameters
    ----------
    reader
        The injected lake reader (production wraps the lake ``Catalog``; tests
        pass a fake).
    targets
        The instruments to screen. Must be unique.
    start, end
        The window (tz-aware, ``end >= start``); passed to every read.
    recent_bars
        The volume-surge recent-window size (``>= 1``).
    top_n
        The ranking depth (``>= 1``).

    Returns
    -------
    MoversReport
        The ranked report (see :func:`rank_movers`).

    Raises
    ------
    ValueError
        If the window is naive/unordered, ``recent_bars`` / ``top_n`` is below
        1, or *targets* contains a duplicate.
    """
    _require_window(start, end)
    _require_positive("recent_bars", recent_bars)
    _require_positive("top_n", top_n)

    series: dict[ScreenTarget, Sequence[CandleBar]] = {}
    for target in targets:
        if target in series:
            raise ValueError(f"screen_movers: duplicate target {target}")
        series[target] = reader.read_bars(target, start=start, end=end)

    return rank_movers(
        series,
        window_start=start,
        window_end=end,
        recent_bars=recent_bars,
        top_n=top_n,
    )
