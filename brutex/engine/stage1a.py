"""Stage-1A bitset-matrix builder — the adapter that drives ``evaluate_atoms``.

Per ``docs/TRACK1_BACKTEST_SPEC.md`` the brute-force funnel's first stage
precomputes, for one ``(instrument, interval)``, the 60 atomic conditions on
**every** bar so the downstream supermask kernel
(:func:`brutex.engine.combo_mask.count_supermasks`) can count mask hits over a
resident ``u64[N]`` bitset (u64-limb: bits 0..59 fit u64 today; the u128 mask
law + fallback live in ``combo_mask.py``). This module is **that producer**: it walks an
iterable of :class:`~brutex.data.models.CandleBar` in chronological order,
constructs one :class:`~brutex.vocabulary.evaluator.IndicatorSnapshot` per bar,
and calls the **existing**, unit-tested
:func:`~brutex.vocabulary.evaluator.evaluate_atoms` (it does **not** reimplement
the 60 conditions — DEC-ENGINE-DRIVER-001 / ``docs/VERIFICATION_2026-06-03_ENGINE_SCOPING.md``).

What this module owns is the **adapter layer** between the lake's bar
representation and the evaluator's snapshot contract:

* **Decimal-rupees → int64 paisa** at the lake boundary via
  ``int(round_money(price) * 100)`` (``ROUND_HALF_UP``, 2-dp tick grid —
  ``DEC-MONEY-2DP-001`` / ``DEC-SPOT-PRICE-2DP-001``). ``brutex.money`` has no
  rupees→paisa converter, so :func:`_price_to_paisa` is the canonical seam
  (one source of truth, CLAUDE.md §11 COMMON).
* **UTC → IST** for ``timestamp_ist`` via the shared
  :data:`brutex.calendars.nse.IST` zone (never a hard-coded offset).
* **EMA(20) / EMA(200)** causal running state (:class:`brutex.indicators.ema.EMAState`),
  ``None`` during warm-up, the float result snapped to paisa through the same
  :func:`_price_to_paisa` seam.
* **Running day extremes** (:class:`brutex.indicators.day_extremes.DayExtremesState`),
  reset at each new IST trading day.
* **open-of-day** captured at each IST day's first bar.
* **Daily anchors** — pivots / Fib / PDH-PDL computed from the **prior** IST
  trading day's OHLC and broadcast across that day's intraday bars (recomputed
  at each new day). The day's OHLC is accumulated from the intraday bars
  themselves, so the builder is self-contained and the anchors are strictly
  causal (the prior day is fully completed before its OHLC is read).
* **Prior-bar history** (:class:`brutex.vocabulary.evaluator.PriorBarHistory`),
  updated **after** each bar's evaluation with the bullish flag.

## No look-ahead (CLAUDE.md §4, Golden Rule #5)

At bar ``N`` only bars ``0..N`` are read: EMA and day-extremes are causal
streaming states; the daily anchors use the **prior** completed day; the
prior-bar history is updated only *after* the current bar is evaluated. There
is no global state, no wall-clock, and no RNG — given the same bars the matrix
is bit-identical (Golden Rule #4).

## First IST day (no prior-day anchors)

The anchor-dependent condition groups — Group 2 (pivot proximity, bits 6-12),
Group 3 (PDH/PDL, bits 13-18), Group 4 (Fib proximity, bits 19-29) and Group 9
(day type, bits 48-51, which compares against the prior day's close/high/low) —
are **undefined** until a prior trading day has completed. For bars on the very
first day of the stream the builder emits a mask with exactly those bits
cleared (a strategy referencing an undefined anchor simply does not fire). The
EMA, candle-shape, prior-bar, day-structure and time-of-day groups remain fully
defined from bar ``0``. This keeps the output 1:1 with the input bars.

## Contiguity assumption

The builder treats the most-recently-completed IST day in the stream as the
"prior day" for the next day's anchors. It does **not** consult the trading
calendar to verify calendar-adjacency — the lake / :class:`~brutex.feed.replay.ReplayFeed`
deliver contiguous trading-day bars, and re-deriving the calendar here would
risk silently dropping legitimate special sessions (CLAUDE.md §11 COMMON: the
calendar lives in exactly one place). A new IST date always starts a new day.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Final

import fno_math
import trading_core

from brutex.calendars.nse import IST, is_pre_open
from brutex.indicators.day_extremes import DayExtremesState
from brutex.indicators.ema import EMAState
from brutex.indicators.fib_levels import FibLevels, compute_fib_levels
from brutex.indicators.market_structure import MarketStructureSignal
from brutex.indicators.pivots import PivotLevels, compute_pivot_levels
from brutex.indicators.prev_day_ohlc import DailyCandle
from brutex.money import round_money
from brutex.strategies.fib_definitions import (
    ROLLING_WEEKLY_SESSIONS,
    FibAnchorKind,
    resolve_fib_anchor_levels,
)
from brutex.vocabulary import bits as B  # noqa: N812 — B = bit-position namespace
from brutex.vocabulary.evaluator import (
    IndicatorSnapshot,
    PriorBarHistory,
    ProximityParams,
    evaluate_atoms,
)

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.data.models import CandleBar

#: Paisa per rupee — the lake stores Decimal rupees; the evaluator wants int64
#: paisa (``DEC-PAISA-DOC-RECONCILE-001``).
_PAISA_PER_RUPEE: Final[int] = 100

#: EMA periods Track 1 evaluates (``docs/TRACK1_VOCABULARY.md`` Group 1).
_EMA_FAST_PERIOD: Final[int] = 20
_EMA_SLOW_PERIOD: Final[int] = 200

#: Default look-back for the prior-bar pattern group when the caller does not
#: pin one. Matches :class:`PriorBarHistory`'s own default (``prior_n = 3``).
_DEFAULT_PRIOR_N: Final[int] = 3

#: The HLC3 "typical price" divisor — ``(high + low + close) / 3`` — the standard
#: per-bar VWAP proxy. The ONE source of truth for this divisor is
#: :data:`brutex.dashboard.movers_snapshot._HLC3_DIVISOR`; the value is restated
#: here (not imported) only to keep this engine module free of a dashboard import,
#: and the typical price is routed through the SAME :func:`_price_to_paisa`
#: ``round_money``-snap (``(h + l + c) / 3`` snapped to the 2-dp tick grid), so the
#: snap rule has one definition (CLAUDE.md §11 COMMON).
_HLC3_DIVISOR: Final[Decimal] = Decimal(3)

#: Engine-injected contract/session key fed to :class:`VwapState`. The SPOT
#: path (``roll_schedule=None``) feeds this CONSTANT key, so :class:`VwapState`
#: resets only on the IST-date change (its ``session_day`` argument) —
#: byte-identical to the pre-B2 behaviour for the continuous spot-index series.
#: B2 is CLOSED (DEC-STAGE1A-ROLL-RESET-001): the futures track passes a
#: ``roll_schedule`` of owning-expiry day-ordinals and the per-bar key becomes
#: the owning contract's expiry ordinal — a key CHANGE is a roll boundary and
#: triggers the TOTAL per-contract hard reset (:meth:`_roll_to_new_contract`),
#: never only the VWAP key (ADR-0012 D11: every session/level indicator
#: recomputes per-contract). A 2020+ date ordinal (≈ 737,000+) can never
#: collide with this constant 0 (also the Rust cold-sentinel key).
_DEFAULT_SESSION_KEY: Final[int] = 0


def _fib_dependent_bits() -> int:
    """Build the u128 mask of the Group-4 Fib-proximity bits (19-29).

    These bits depend on the SWEPT Fib anchor (DEC-NIFTY-FIB-AXIS-001). They are
    cleared on a day whose Fib anchor is cold (no prior daily candle for prev-day;
    < 5 prior sessions for rolling-weekly; no completed prior ISO week for
    weekly-static) EVEN WHEN the prev-day pivot/PDH anchor is warm — so a cold
    rolling-weekly week never fabricates a Fib level (Golden Rule #1) yet keeps its
    (warm) pivots (R-2).

    Returns
    -------
    int
        u128 bitmask with every Group-4 Fib-proximity bit set.
    """
    fib_bits = (
        B.BIT_NEAR_FIB_0,
        B.BIT_NEAR_FIB_236,
        B.BIT_NEAR_FIB_382,
        B.BIT_NEAR_FIB_50,
        B.BIT_NEAR_FIB_618,
        B.BIT_NEAR_FIB_786,
        B.BIT_NEAR_FIB_100,
        B.BIT_NEAR_FIB_1272,
        B.BIT_NEAR_FIB_1618,
        B.BIT_NEAR_FIB_200,
        B.BIT_NEAR_FIB_2618,
    )
    mask = 0
    for bit in fib_bits:
        mask |= 1 << bit
    return mask


def _pivot_dependent_bits() -> int:
    """Build the u128 mask of the prev-day pivot/PDH/day-type bits.

    Groups 2 (pivot proximity, 6-12), 3 (PDH/PDL, 13-18) and 9 (day type, 48-51)
    are anchored to the PRIOR completed IST day and are INDEPENDENT of the swept
    Fib anchor (the Fib axis moves ONLY the Group-4 levels, R-2). They are cleared
    only when there is no prior day at all (the first day of the stream).

    Returns
    -------
    int
        u128 bitmask with every prev-day pivot/PDH/day-type bit set.
    """
    pivot_bits = (
        # Group 2 — pivot proximity (bits 6-12).
        B.BIT_NEAR_PIVOT_P,
        B.BIT_NEAR_PIVOT_R1,
        B.BIT_NEAR_PIVOT_R2,
        B.BIT_NEAR_PIVOT_R3,
        B.BIT_NEAR_PIVOT_S1,
        B.BIT_NEAR_PIVOT_S2,
        B.BIT_NEAR_PIVOT_S3,
        # Group 3 — PDH/PDL (bits 13-18).
        B.BIT_CLOSE_ABOVE_PDH,
        B.BIT_CLOSE_BELOW_PDH,
        B.BIT_CLOSE_ABOVE_PDL,
        B.BIT_CLOSE_BELOW_PDL,
        B.BIT_NEAR_PDH,
        B.BIT_NEAR_PDL,
        # Group 9 — day type (bits 48-51).
        B.BIT_GAP_UP_DAY,
        B.BIT_GAP_DOWN_DAY,
        B.BIT_INSIDE_DAY,
        B.BIT_OUTSIDE_DAY,
    )
    mask = 0
    for bit in pivot_bits:
        mask |= 1 << bit
    return mask


#: u128 mask of the Group-4 Fib-proximity bits (cleared on a Fib-cold day).
FIB_DEPENDENT_MASK: Final[int] = _fib_dependent_bits()

#: u128 mask of the prev-day pivot/PDH/day-type bits (cleared on the first day).
PIVOT_DEPENDENT_MASK: Final[int] = _pivot_dependent_bits()

#: u128 mask of every bit cleared on the first (no-prior-day) IST day — the union
#: of the prev-day pivot/PDH/day-type bits AND the Fib-proximity bits. Preserved
#: (same name + value) so the first-day clear is byte-identical to pre-axis and
#: the golden fixtures (:mod:`tests.golden.test_engine_nifty_day_golden`) still
#: pin the exact same matrix.
ANCHOR_DEPENDENT_MASK: Final[int] = PIVOT_DEPENDENT_MASK | FIB_DEPENDENT_MASK

#: Complement used to keep only the defined bits on the first day.
_NON_ANCHOR_MASK: Final[int] = ~ANCHOR_DEPENDENT_MASK

#: Complement used to clear ONLY the Fib-proximity bits (a Fib-cold, pivot-warm
#: day — a cold rolling-weekly / weekly-static week whose prev-day pivots ARE
#: warm; DEC-NIFTY-FIB-AXIS-001 R-2).
_NON_FIB_MASK: Final[int] = ~FIB_DEPENDENT_MASK

#: Bounded rolling daily-candle history kept for the two multi-day Fib anchors.
#: Rolling-weekly needs the trailing 5 completed sessions; weekly-static needs the
#: last COMPLETED ISO week (up to ~5 sessions in a week strictly before today's).
#: Keeping the last ``_DAILY_HISTORY_MAX`` completed days covers both with slack
#: for holiday-dense spans, preserving the O(1)-per-bar SCALABLE property
#: (Golden Rule #5 / R-4 — never the full O(days) history).
_DAILY_HISTORY_MAX: Final[int] = ROLLING_WEEKLY_SESSIONS * 3

#: Neutral placeholder anchors used ONLY so :func:`evaluate_atoms` can run on
#: the first day; every bit they could influence is cleared via
#: :data:`ANCHOR_DEPENDENT_MASK`, so the placeholder values never reach output.
_PLACEHOLDER_DAY: Final[date] = date(1970, 1, 1)
_PLACEHOLDER_PIVOTS: Final[PivotLevels] = compute_pivot_levels(_PLACEHOLDER_DAY, 0, 0, 0)
_PLACEHOLDER_FIB: Final[FibLevels] = compute_fib_levels(_PLACEHOLDER_DAY, 0, 0)


def _price_to_paisa(price: Decimal) -> int:
    """Convert a Decimal-rupee price to int64 paisa at the lake boundary.

    The canonical rupees→paisa seam (``brutex.money`` only converts the other
    direction). The price is first snapped to the 2-decimal tick grid via
    :func:`brutex.money.round_money` (``ROUND_HALF_UP``, never truncation —
    ``DEC-SPOT-PRICE-2DP-001``); the ``× 100`` is exact in :class:`~decimal.Decimal`,
    so the final ``int`` is loss-free.

    Parameters
    ----------
    price
        A price in rupees (exact :class:`~decimal.Decimal`, as stored in the
        lake) or a float-derived rupee value already wrapped via ``str``.

    Returns
    -------
    int
        The price in int64 paisa.
    """
    return int(round_money(price) * _PAISA_PER_RUPEE)


def _ema_to_paisa(value: float | None) -> int | None:
    """Snap a float EMA (rupees) to int64 paisa, preserving the warm-up ``None``.

    The EMA recurrence runs in float (matching TradingView ``ta.ema`` —
    :mod:`brutex.indicators.ema`); the result is routed through the SAME
    :func:`_price_to_paisa` seam as OHLC so the price→paisa rule has one source
    of truth (CLAUDE.md §11 COMMON). The float is wrapped via ``str`` before
    becoming a :class:`~decimal.Decimal` to avoid binary-float drift (mirrors
    :mod:`brutex.data.candle_fields`), keeping the conversion deterministic
    (Golden Rule #4).

    Parameters
    ----------
    value
        The EMA value in rupees, or ``None`` while the EMA is warming up.

    Returns
    -------
    int | None
        The EMA in int64 paisa, or ``None`` during warm-up.
    """
    if value is None:
        return None
    return _price_to_paisa(Decimal(str(value)))


@dataclass
class _DayOHLC:
    """Running OHLC accumulator for one IST trading day (int64 paisa).

    The open is the first bar's open; the high/low expand across the day; the
    close is the most-recent bar's close. Used to feed the *next* day's pivot /
    Fib / PDH-PDL anchors once the day has completed.
    """

    open_paisa: int
    high_paisa: int
    low_paisa: int
    close_paisa: int

    def absorb(self, *, high_paisa: int, low_paisa: int, close_paisa: int) -> None:
        """Fold a later same-day bar into the running OHLC (O(1))."""
        self.high_paisa = max(self.high_paisa, high_paisa)
        self.low_paisa = min(self.low_paisa, low_paisa)
        self.close_paisa = close_paisa


def _day_ohlc_to_candle(day: date, ohlc: _DayOHLC) -> DailyCandle:
    """Project a completed :class:`_DayOHLC` into the :class:`DailyCandle` the
    multi-day Fib anchor primitives consume (one COMMON daily type — the same
    ``prev_day_ohlc.DailyCandle`` ``rolling_weekly_anchor`` / ``weekly_static_anchor``
    read; CLAUDE.md §11)."""
    return DailyCandle(
        day=day,
        open_paisa=ohlc.open_paisa,
        high_paisa=ohlc.high_paisa,
        low_paisa=ohlc.low_paisa,
        close_paisa=ohlc.close_paisa,
    )


@dataclass
class _DayAnchors:
    """The daily-anchor bundle broadcast across one IST day's intraday bars.

    ``None`` on the first day of a stream (no prior day yet); otherwise the
    prev-day pivots plus the SWEPT-anchor Fib levels for the day. The pivots are
    always prev-day (they never move with the Fib axis, R-2); ``fib`` is ``None``
    when the swept Fib anchor is COLD (a cold rolling-weekly / weekly-static week
    whose prev-day pivots are nonetheless warm), in which case the builder clears
    ONLY the Group-4 Fib bits and keeps the (warm) pivots
    (DEC-NIFTY-FIB-AXIS-001 R-2).
    """

    pivots: PivotLevels
    fib: FibLevels | None


class Stage1AMatrixBuilder:
    """Drive :func:`evaluate_atoms` across one ``(instrument, interval)`` bar feed.

    Streaming-first: feed bars one at a time with :meth:`push` (the live /
    incremental path) or hand an iterable to :meth:`build` (the batch path).
    Both share the same per-bar logic, so the matrix is identical regardless of
    which entry point produced it. The builder is stateful across bars (EMA,
    day extremes, prior-bar history, the accumulating day OHLC) but holds **no**
    global, wall-clock or random state, so a fresh builder fed the same bars
    yields a bit-identical matrix (Golden Rule #4).

    Parameters
    ----------
    proximity
        Proximity tolerances for the ``near_*`` conditions. Defaults to
        :class:`ProximityParams`' own defaults (10 bps each); the brute-force
        engine sweeps bps ∈ {5, 10, 25, 50} by running one builder per value.
    prior_n
        Look-back for the prior-bar pattern group (Group 6), ∈ {1, 2, 3, 4}.
        Defaults to 3 (matches :class:`PriorBarHistory`).
    fib_anchor
        The SWEPT Fibonacci anchor (DEC-NIFTY-FIB-AXIS-001) the Group-4 Fib
        proximity bits (19-29) are evaluated against — one of the three families
        (prev-day / rolling-weekly / weekly-static). Defaults to
        :attr:`FibAnchorKind.PREV_DAY`, so a builder constructed without this
        argument is byte-identical to the pre-axis prev-day-only builder
        (DEFAULT-PRESERVATION — the goldens still pin the same matrix). Only the
        Group-4 Fib levels move with this anchor; the pivot / PDH / day-type
        groups stay prev-day-anchored regardless (R-2).
    roll_schedule
        ADR-0012 NIFTY-futures roll schedule (DEC-STAGE1A-ROLL-RESET-001):
        strictly increasing owning-expiry day-ordinals from
        :func:`brutex.data.futures_series.resolve_futures_series`. Per bar the
        session key becomes the first schedule entry ``>=`` the bar's IST day
        ordinal (the ``resolve_near_month`` ``>=`` law over the vendor
        schedule); a key CHANGE triggers the TOTAL per-contract hard reset
        (:meth:`_roll_to_new_contract`); a bar beyond the last entry raises
        :class:`ValueError` (loader/schedule desync — never a silent
        constant-key fallback). ``None`` (the default — every spot caller)
        feeds the constant :data:`_DEFAULT_SESSION_KEY`, byte-identical to the
        pre-B2 builder.
    apply_1day_mask
        D12 honesty flag for a volume-bearing futures ``1day`` grid: when True
        the FINAL mask is ANDed with
        :data:`brutex.vocabulary.bits.APPLIES_TO_1DAY_MASK` (clears exactly
        the time-of-day bits 44-47 + the VWAP-position bits 52-53). Default
        False — spot is deliberately NOT flipped (its 1day ToD-bit behaviour
        is unaudited; the spot-side unification is a named follow-up).

    Raises
    ------
    ValueError
        If *roll_schedule* is an empty tuple or not strictly increasing
        non-negative day ordinals.

    Notes
    -----
    "Bullish" for the prior-bar history is ``close > open`` (a doji where
    ``close == open`` is recorded as **bearish** — the same not-strictly-up
    tie-break the evaluator's Group 5 uses, where ``BIT_BAR_BULLISH`` requires
    a strict ``close > open``). Documented so the tie-break has one definition
    (DEC-ENGINE-DRIVER-001).
    """

    def __init__(
        self,
        proximity: ProximityParams | None = None,
        prior_n: int = _DEFAULT_PRIOR_N,
        fib_anchor: FibAnchorKind = FibAnchorKind.PREV_DAY,
        *,
        roll_schedule: tuple[int, ...] | None = None,
        apply_1day_mask: bool = False,
    ) -> None:
        self._proximity = proximity if proximity is not None else ProximityParams()
        self._prior_n = prior_n  # stored for the roll-reset re-construction (D11)
        self._prior_history = PriorBarHistory(prior_n=prior_n)
        self._fib_anchor = fib_anchor
        # ADR-0012 roll schedule (DEC-STAGE1A-ROLL-RESET-001): sorted owning-
        # expiry day-ordinals for the NIFTY-futures track. None (the default —
        # every spot caller) keeps the constant-key path byte-identical.
        if roll_schedule is not None:
            if not roll_schedule:
                raise ValueError("Stage1AMatrixBuilder: roll_schedule must be non-empty")
            if list(roll_schedule) != sorted(set(roll_schedule)) or roll_schedule[0] < 0:
                raise ValueError(
                    "Stage1AMatrixBuilder: roll_schedule must be strictly increasing "
                    "non-negative day ordinals"
                )
        self._roll_schedule = roll_schedule
        self._roll_cursor = 0
        self._current_session_key: int | None = None
        self._apply_1day_mask = apply_1day_mask
        self._ema_fast = EMAState.new(_EMA_FAST_PERIOD)
        self._ema_slow = EMAState.new(_EMA_SLOW_PERIOD)
        self._day_extremes = DayExtremesState()
        # Session-anchored intraday VWAP (Group 10, bits 52-53) and BOS/CHoCH market
        # structure (Group 12, bits 56-59) are the SHARED Rust ``trading_core`` state
        # objects — the per-bar compute (the VWAP ``cum(price·vol)/cum(vol)`` math and
        # the look-ahead-clean fractal-swing BOS/CHoCH logic) runs IN RUST, never
        # re-implemented in Python, so the backtest is bit-identical to TickVault's
        # native path (DEC-STAGE1A-WIRE-VWAP-BOS-001 — the Rust pyclasses ported in
        # this PR; same delegation pattern as the EMA recurrence DEC-EMA-RUST-PORT-001).
        # VWAP resets at each new IST trading day via its ``session_day`` argument; the
        # market-structure state is continuous (look-ahead-clean, non-repainting).
        self._vwap = trading_core.VwapState()
        self._structure = trading_core.MarketStructureState()
        self._current_day: date | None = None
        # OHLC of the IST day currently being walked (None before the 1st bar).
        self._today_ohlc: _DayOHLC | None = None
        # OHLC of the most-recently-completed IST day → source of today's prev-day
        # pivot / PDH anchors (unchanged; never moves with the Fib axis).
        self._prev_day_ohlc: _DayOHLC | None = None
        # Bounded rolling history of completed IST days → source of the multi-day
        # rolling-weekly / weekly-static Fib anchors (DEC-NIFTY-FIB-AXIS-001). Only
        # populated for a non-prev-day anchor (prev-day needs only `_prev_day_ohlc`,
        # so the prev-day path stays byte-identical + allocation-free). Bounded to
        # `_DAILY_HISTORY_MAX` completed days (LRU-by-insertion, O(1) per bar — R-4).
        self._daily_history: OrderedDict[date, DailyCandle] = OrderedDict()
        # Anchors broadcast across the current day, or None on the first day.
        self._anchors: _DayAnchors | None = None

    def build(self, bars: Iterable[CandleBar]) -> list[int]:
        """Consume an iterable of bars and return the full ``bar_bits`` matrix.

        Parameters
        ----------
        bars
            Chronologically ordered bars for one ``(instrument, interval)``.

        Returns
        -------
        list[int]
            One u64 condition mask per input bar, in input order (1:1).
        """
        return list(self.stream(bars))

    def stream(self, bars: Iterable[CandleBar]) -> Iterator[int]:
        """Lazily yield one u64 condition mask per bar (the streaming path).

        Parameters
        ----------
        bars
            Chronologically ordered bars for one ``(instrument, interval)``.

        Yields
        ------
        int
            The u64 condition mask for each bar, in input order.
        """
        for bar in bars:
            yield self.push(bar)

    def push(self, bar: CandleBar) -> int:
        """Evaluate one bar and return its u128 condition mask (O(1) per bar).

        Advances every running state, handles the IST day rollover (rotating
        the completed day's OHLC into the prior-day anchors), constructs the
        :class:`IndicatorSnapshot`, calls :func:`evaluate_atoms`, then records
        the bar's bullish flag in the prior-bar history (after evaluation, so
        the current bar is not in its own look-back).

        Parameters
        ----------
        bar
            One :class:`~brutex.data.models.CandleBar` (UTC timestamp, Decimal
            rupees), the next bar in chronological order.

        Returns
        -------
        int
            The u128 condition mask (bits 0-59) for this bar — including the
            Group-10 VWAP-position bits (52-53) and the Group-12 BOS/CHoCH bits
            (56-59), now populated from the running :class:`VwapState` /
            :class:`MarketStructureState` (DEC-STAGE1A-WIRE-VWAP-BOS-001).
        """
        ts_ist = bar.timestamp.astimezone(IST)
        bar_day = ts_ist.date()

        open_paisa = _price_to_paisa(bar.open)
        high_paisa = _price_to_paisa(bar.high)
        low_paisa = _price_to_paisa(bar.low)
        close_paisa = _price_to_paisa(bar.close)

        # Per-bar roll key (DEC-STAGE1A-ROLL-RESET-001) — O(1) amortized: a
        # monotone cursor over the sorted schedule (bars are chronological per
        # the stream contract; <= C ≈ 80 total advances). Runs BEFORE the day
        # rollover so the contract reset composes with `_roll_to_new_day` (a
        # boundary can only fall on an IST-day change — ownership is
        # date-granular).
        if self._roll_schedule is None:
            session_key = _DEFAULT_SESSION_KEY  # spot: byte-identical constant 0
        else:
            day_ordinal = bar_day.toordinal()
            while (
                self._roll_cursor < len(self._roll_schedule)
                and self._roll_schedule[self._roll_cursor] < day_ordinal
            ):
                self._roll_cursor += 1  # advance past expired contracts
            if self._roll_cursor >= len(self._roll_schedule):
                raise ValueError(
                    f"Stage1AMatrixBuilder: bar at {bar.timestamp.isoformat()} (IST day "
                    f"{bar_day}) is beyond the last roll_schedule expiry — loader/schedule "
                    "desync (a genuine bug, never a silent constant-key fallback)"
                )
            session_key = self._roll_schedule[self._roll_cursor]  # owning expiry ORDINAL
            if self._current_session_key is not None and session_key != self._current_session_key:
                self._roll_to_new_contract()  # ADR-0012 D11: TOTAL hard reset
            self._current_session_key = session_key

        if self._today_ohlc is None or bar_day != self._current_day:
            today_ohlc = self._roll_to_new_day(
                bar_day, open_paisa, high_paisa, low_paisa, close_paisa
            )
        else:
            # Same IST day: the `or` above guarantees _today_ohlc is set here,
            # so mypy narrows it to _DayOHLC (no assert needed).
            today_ohlc = self._today_ohlc
            today_ohlc.absorb(high_paisa=high_paisa, low_paisa=low_paisa, close_paisa=close_paisa)

        # Both EMAs are close-driven (vocabulary Group 1: "EMA 20 close, EMA
        # 200 close"). The float close is taken directly from the Decimal bar;
        # the EMA recurrence runs in float, then snaps to paisa once.
        close_float = float(bar.close)
        ema20_paisa = _ema_to_paisa(self._ema_fast.update(close_float))
        ema200_paisa = _ema_to_paisa(self._ema_slow.update(close_float))

        day_high_paisa, day_low_paisa = self._day_extremes.update(
            ts_ist=ts_ist, high_paisa=high_paisa, low_paisa=low_paisa
        )

        # ── Group 10: session-anchored VWAP (bits 52-53) ──
        # The ONLY arithmetic here is the typical price = HLC3 = (high + low + close)
        # / 3, ``round_money``-snapped to the 2-dp tick grid via the SAME
        # :func:`_price_to_paisa` seam as OHLC/EMA (one snap definition, CLAUDE.md §11
        # COMMON, equal to the dashboard's turnover proxy). The VWAP itself — the
        # ``cum(price·vol)/cum(vol)`` accumulation, the 09:15 reset, the 09:00-09:15
        # auction exclusion — is computed IN RUST by ``trading_core.VwapState`` (this
        # builder only feeds the bar in and reads the resolved level out). ``None``
        # during warm-up → the bits do not fire. O(1) per bar, O(1) Rust-side state.
        # tod_seconds comes from the SAME shared Rust seam the vocabulary
        # evaluator uses (DEC-TOD-SECONDS-RUST-SEAM-001) — no inline `h*3600`
        # arithmetic on the eval path. KNOWN deferred debt: the evaluator
        # re-derives the same value for this bar (+1 crossing) — threading one
        # value through is a larger refactor, out of scope here.
        tod_seconds = int(trading_core.seconds_of_day(ts_ist.hour, ts_ist.minute, ts_ist.second))
        typical_paisa = _price_to_paisa((bar.high + bar.low + bar.close) / _HLC3_DIVISOR)
        vwap_paisa = self._vwap.update(
            typical_paisa,
            bar.volume,
            is_pre_open(ts_ist.time()),
            session_key,
            bar_day.toordinal(),
            tod_seconds,
        )

        # ── Group 12: BOS/CHoCH market structure (bits 56-59) ──
        # The look-ahead-clean (a swing is confirmed only k bars LATER, all ≤ N),
        # non-repainting fractal-swing detection runs IN RUST by
        # ``trading_core.MarketStructureState`` (this builder feeds the bar in and
        # reads the four resolved booleans out). They thread into the snapshot exactly
        # like the resolved PriorBarHistory flags. O(1) per bar (k is a fixed constant),
        # O(1) Rust-side state (a bounded 2k+1 ring).
        bos_bull, bos_bear, choch_bull, choch_bear = self._structure.update(
            high_paisa, low_paisa, close_paisa
        )
        market_structure = MarketStructureSignal(
            bos_bullish=bos_bull,
            bos_bearish=bos_bear,
            choch_bullish=choch_bull,
            choch_bearish=choch_bear,
        )

        # Two INDEPENDENT warm flags (DEC-NIFTY-FIB-AXIS-001 R-2): the prev-day
        # pivot/PDH/day-type groups are warm once a prior day exists; the Group-4
        # Fib group is warm only when the SWEPT Fib anchor also resolved (a cold
        # rolling-weekly / weekly-static week has warm pivots but a `None` Fib).
        has_pivot_anchor = self._anchors is not None
        has_fib_anchor = self._anchors is not None and self._anchors.fib is not None
        pivots = self._anchors.pivots if self._anchors is not None else _PLACEHOLDER_PIVOTS
        fib = (
            self._anchors.fib
            if self._anchors is not None and self._anchors.fib is not None
            else _PLACEHOLDER_FIB
        )

        snapshot = IndicatorSnapshot(
            timestamp_ist=ts_ist,
            open_paisa=open_paisa,
            high_paisa=high_paisa,
            low_paisa=low_paisa,
            close_paisa=close_paisa,
            ema20_paisa=ema20_paisa,
            ema200_paisa=ema200_paisa,
            pivots=pivots,
            fib=fib,
            day_high_paisa=day_high_paisa,
            day_low_paisa=day_low_paisa,
            open_of_day_paisa=today_ohlc.open_paisa,
            vwap_paisa=vwap_paisa,
            market_structure=market_structure,
        )

        mask = evaluate_atoms(snapshot, self._proximity, self._prior_history)
        if not has_pivot_anchor:
            # First IST day: EVERY anchor-dependent group (pivots + Fib + PDH +
            # day-type) is undefined → clear them all (byte-identical to pre-axis).
            mask &= _NON_ANCHOR_MASK
        elif not has_fib_anchor:
            # Pivots warm but the swept Fib anchor is COLD (< 5 sessions for
            # rolling-weekly, no completed prior week for weekly-static): clear
            # ONLY the Group-4 Fib bits, keeping the (warm) pivots (R-2). Never
            # reached for a warm prev-day anchor (its Fib is warm whenever pivots
            # are — both derive from the same single prior day).
            mask &= _NON_FIB_MASK

        if self._apply_1day_mask:
            # D12 honesty for a volume-bearing 1day grid (ADR-0012 /
            # DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001): each 1day bar is its own
            # VWAP session, so the intraday-only bits — time-of-day 44-47 +
            # VWAP-position 52-53 — are HONESTLY cleared via the vocabulary's
            # own APPLIES_TO_1DAY_MASK (one bit-set source of truth).
            mask &= B.APPLIES_TO_1DAY_MASK

        # Record this bar's flag AFTER evaluation (close == open → bearish).
        self._prior_history.update(close_paisa > open_paisa)
        return mask

    def _roll_to_new_contract(self) -> None:
        """ADR-0012 D11 hard reset — the roll boundary is a per-contract series restart.

        "Every session/level indicator recomputes per-contract"
        (``futures_roll.py`` roll policy). VWAP would also reset via its own
        key-change branch (``vwap.rs``, pinned by the twin-state golden); it is
        recreated anyway so the invariant "post-roll state == fresh-builder
        state" is EXACT rather than argued — the roll-segmentation equivalence
        oracle depends on it. Clearing ``_current_day`` / ``_today_ohlc``
        forces the EXISTING day-rollover branch to seed a pristine day-1 state
        on this bar, and the cleared ``_prev_day_ohlc`` / ``_daily_history`` /
        ``_anchors`` make the first post-roll day carry the EXISTING
        ``_NON_ANCHOR_MASK`` clear (the honest ADR-0012 §4.5 #6 cold-start
        semantics, reusing the machinery — a level is never compared across
        the basis gap, DEC-STAGE1A-ROLL-RESET-001).
        """
        self._vwap = trading_core.VwapState()
        self._structure = trading_core.MarketStructureState()  # no FFI reset() exists
        self._ema_fast = EMAState.new(_EMA_FAST_PERIOD)
        self._ema_slow = EMAState.new(_EMA_SLOW_PERIOD)
        self._prior_history = PriorBarHistory(prior_n=self._prior_n)
        self._day_extremes = DayExtremesState()
        self._current_day = None  # forces _roll_to_new_day on this bar
        self._today_ohlc = None
        self._prev_day_ohlc = None  # → first-day _NON_ANCHOR_MASK clear fires
        self._daily_history.clear()  # weekly Fib anchors go cold → _NON_FIB_MASK
        self._anchors = None

    def _roll_to_new_day(
        self,
        bar_day: date,
        open_paisa: int,
        high_paisa: int,
        low_paisa: int,
        close_paisa: int,
    ) -> _DayOHLC:
        """Rotate state at an IST day boundary and recompute the day's anchors.

        The day that just ended (if any) becomes the prior day; its OHLC drives
        the new day's pivot / Fib anchors. The new day's OHLC accumulator is
        seeded with this first bar's values.

        Parameters
        ----------
        bar_day
            The new IST trading day.
        open_paisa, high_paisa, low_paisa, close_paisa
            The first bar of the new day, in int64 paisa.

        Returns
        -------
        _DayOHLC
            The freshly seeded OHLC accumulator for the new day (a definite,
            non-``None`` value the caller uses without re-narrowing).
        """
        # The day we were accumulating is now complete → it is the prior day.
        if self._today_ohlc is not None:
            self._prev_day_ohlc = self._today_ohlc
            # Record the just-completed day into the bounded rolling history — the
            # source of the multi-day rolling-weekly / weekly-static Fib anchors.
            # Only needed (and paid) for a non-prev-day anchor; the prev-day path
            # keeps `_daily_history` empty (byte-identical + allocation-free).
            if self._fib_anchor is not FibAnchorKind.PREV_DAY and self._current_day is not None:
                self._daily_history[self._current_day] = _day_ohlc_to_candle(
                    self._current_day, self._today_ohlc
                )
                # Evict the oldest completed day past the bound (O(1) — R-4).
                while len(self._daily_history) > _DAILY_HISTORY_MAX:
                    self._daily_history.popitem(last=False)

        self._current_day = bar_day
        new_ohlc = _DayOHLC(
            open_paisa=open_paisa,
            high_paisa=high_paisa,
            low_paisa=low_paisa,
            close_paisa=close_paisa,
        )
        self._today_ohlc = new_ohlc
        self._anchors = self._compute_anchors(bar_day, self._prev_day_ohlc)
        return new_ohlc

    def _compute_anchors(self, today: date, prev_ohlc: _DayOHLC | None) -> _DayAnchors | None:
        """Compute the prev-day pivots + the SWEPT-anchor Fib levels for ``today``.

        The pivots (Groups 2/3/9) are ALWAYS derived from the prior completed day —
        the Fib axis never moves them (R-2). The Group-4 Fib levels are resolved
        against the builder's :attr:`_fib_anchor` via the COMMON
        :func:`~brutex.strategies.fib_definitions.resolve_fib_anchor_levels`
        dispatcher (never re-implemented here; CLAUDE.md §11 COMMON) — prev-day
        reads :attr:`_prev_day_ohlc` directly (calendar-free, byte-identical to
        pre-axis), while rolling-weekly / weekly-static read the bounded
        :attr:`_daily_history`.

        Parameters
        ----------
        today
            The IST trading day the anchors apply to.
        prev_ohlc
            The prior completed day's OHLC, or ``None`` if ``today`` is the
            first day of the stream.

        Returns
        -------
        _DayAnchors | None
            The pivots + (possibly ``None``) Fib for ``today``, or ``None`` when
            there is no prior day at all (the caller then clears every
            anchor-dependent bit). A non-``None`` bundle with ``fib is None`` means
            the pivots are warm but the swept Fib anchor is cold (R-2).
        """
        if prev_ohlc is None:
            return None
        pivots = compute_pivot_levels(
            today,
            pdh_paisa=prev_ohlc.high_paisa,
            pdl_paisa=prev_ohlc.low_paisa,
            pdc_paisa=prev_ohlc.close_paisa,
        )
        if self._fib_anchor is FibAnchorKind.PREV_DAY:
            # Calendar-free prev-day Fib straight off the completed prior day —
            # byte-identical to the pre-axis builder (the goldens still pin it).
            fib: FibLevels | None = compute_fib_levels(
                today,
                pdh_paisa=prev_ohlc.high_paisa,
                pdl_paisa=prev_ohlc.low_paisa,
            )
        else:
            # Multi-day anchor: dispatch through the COMMON resolver over the
            # bounded rolling history. Returns None on a cold week (< 5 sessions
            # for rolling-weekly / no completed prior week for weekly-static) — the
            # caller then clears ONLY the Group-4 Fib bits, keeping the warm pivots.
            fib = resolve_fib_anchor_levels(self._fib_anchor, today, self._daily_history)
        return _DayAnchors(pivots=pivots, fib=fib)


def build_bar_bits(
    bars: Iterable[CandleBar],
    proximity: ProximityParams | None = None,
    prior_n: int = _DEFAULT_PRIOR_N,
    fib_anchor: FibAnchorKind = FibAnchorKind.PREV_DAY,
    *,
    roll_schedule: tuple[int, ...] | None = None,
    apply_1day_mask: bool = False,
) -> list[int]:
    """Build the Stage-1A ``bar_bits`` matrix for one ``(instrument, interval)``.

    Thin functional wrapper over :class:`Stage1AMatrixBuilder` for the common
    "one feed in, full matrix out" batch call.

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)``.
    proximity
        Proximity tolerances for the ``near_*`` conditions (defaults to 10 bps).
    prior_n
        Prior-bar pattern look-back ∈ {1, 2, 3, 4} (defaults to 3).
    fib_anchor
        The SWEPT Fibonacci anchor (DEC-NIFTY-FIB-AXIS-001). Defaults to
        :attr:`FibAnchorKind.PREV_DAY` so an unset call is byte-identical to the
        pre-axis prev-day-only build.
    roll_schedule
        ADR-0012 futures roll schedule (DEC-STAGE1A-ROLL-RESET-001) — see
        :class:`Stage1AMatrixBuilder`. ``None`` (the default) is the
        byte-identical spot path.
    apply_1day_mask
        D12 honesty flag for a volume-bearing futures ``1day`` grid — see
        :class:`Stage1AMatrixBuilder`. Default False (byte-identical).

    Returns
    -------
    list[int]
        One u128 condition mask per input bar, in input order (1:1).
    """
    return Stage1AMatrixBuilder(
        proximity=proximity,
        prior_n=prior_n,
        fib_anchor=fib_anchor,
        roll_schedule=roll_schedule,
        apply_1day_mask=apply_1day_mask,
    ).build(bars)


def apply_1day_mask_for(instrument: str, tf_label: str) -> bool:
    """The ONE D12 predicate: mask the intraday-only bits on a futures ``1day`` grid?

    A pure function of ``(instrument, tf_label)`` — both already run-identity
    fields, so the flag needs NO ``canonical_trade_params`` key (no
    double-carry). True iff *instrument* is the pinned NIFTY-futures series
    alias (:data:`brutex.universe.NIFTY_FUTURES_SERIES_SYMBOL`) AND *tf_label*
    is the lake ``"1day"`` interval — futures 1day partitions carry REAL
    volume, so bits 52/53 would otherwise fire each 1day bar against its own
    single-bar VWAP session (a live D12 violation). Spot is deliberately NOT
    flipped (named follow-up — DEC-NIFTY-FUTURES-SWEEP-LAUNCH-001).
    """
    from brutex.universe import NIFTY_FUTURES_SERIES_SYMBOL

    return instrument == NIFTY_FUTURES_SERIES_SYMBOL and tf_label == "1day"


# ────────────────────────────────────────────────────────────────────────
# Walk-forward fold schedule (DEC-WALK-FORWARD-OOS-001)
# ────────────────────────────────────────────────────────────────────────
#
# Today the engine has a SINGLE chronological out-of-sample split
# (:func:`brutex.engine.validation.validate_out_of_sample` — one train block +
# one OOS block). A lone split lets a seasonal overfit (an earnings cluster, an
# index reconstitution week) hide inside the one test period (audit Gap #4). A
# *rolling walk-forward* schedule scores a combo on MANY held-out folds, so a
# combo that only worked in one season is exposed by the folds it fails.
#
# This section owns the **deterministic, NSE-calendar-anchored fold schedule**:
# rolling windows of ``train_days`` trading days immediately before
# ``test_days`` held-out trading days, shifted ``step_days`` trading days per
# fold. "Anchored to the NSE calendar" means the windows are sized in **actual
# IST trading days present in the stream** (:func:`trading_day_starts`), never in
# wall-clock days — weekends/holidays are simply absent, so a fold's 5 test days
# are 5 real sessions. The per-fold scoring + aggregation lives in
# :mod:`brutex.engine.sweep_driver`; this is the schedule it consumes.
#
# Reproducibility (Golden Rule #4): the schedule carries a BLAKE3
# :attr:`WalkForwardSchedule.schedule_hash` over its fold boundaries + window
# parameters, which the sweep driver folds into the run-hash so a result is tied
# to the exact fold layout it was scored on.

#: Walk-forward window defaults: a 90-trading-day train window, a 5-trading-day
#: (≈ one week) held-out test window, shifted one trading week (5 days) per fold.
#: With ``step_days == test_days`` the test windows tile the covered range with
#: no overlap and no gap (a clean partition); the property tests pin this.
DEFAULT_WF_TRAIN_DAYS: Final[int] = 90
DEFAULT_WF_TEST_DAYS: Final[int] = 5
DEFAULT_WF_STEP_DAYS: Final[int] = 5

#: Bumped if the canonical encoding fed to :func:`_walk_forward_schedule_hash`
#: ever changes, so an old hash can never be silently reinterpreted under new
#: encoding rules (mirrors the ``indicator_version`` discipline of the combo
#: identity hash — DEC-WALK-FORWARD-OOS-001).
_WF_SCHEDULE_HASH_VERSION: Final[int] = 1


def trading_day_starts(bars: Sequence[CandleBar]) -> list[int]:
    """Return the bar index at which each distinct IST trading day begins.

    The canonical IST trading-day segmenter for the engine: one pass over the
    bars recording the index of the first bar of every new IST calendar date —
    the same day-rollover rule :class:`Stage1AMatrixBuilder` applies internally.
    The result is strictly increasing; ``starts[k]`` is the first bar of trading
    day ``k`` and ``starts[k + 1]`` (or ``len(bars)`` for the last day) is one
    past its last bar. ``len(starts)`` is the number of distinct trading days.

    O(N) one pass, O(days) memory. Bars must be in chronological order (the lake
    / :class:`~brutex.feed.replay.ReplayFeed` contract).

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)``.

    Returns
    -------
    list[int]
        The first-bar index of each distinct IST trading day, in order.
    """
    starts: list[int] = []
    prev_date: date | None = None
    for i, bar in enumerate(bars):
        ist_date = bar.timestamp.astimezone(IST).date()
        if ist_date != prev_date:
            starts.append(i)
            prev_date = ist_date
    return starts


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """One rolling walk-forward fold: a train window strictly before a test window.

    Each bound comes in two coordinate systems, both half-open ``[start, end)``:

    * **day** indices into :func:`trading_day_starts` (the NSE-calendar anchor);
    * **bar** indices into the bar stream (the slice the scorer actually uses).

    ``train_end_day == test_start_day`` and ``train_end_bar == test_start_bar``
    (contiguous, no embargo): the training window ends exactly where the held-out
    test window begins, so no test bar can ever inform the training side — the
    no-look-ahead invariant the property tests assert (and the
    :class:`~brutex.feed.lookahead.LookAheadBarrier` re-checks in the sweep
    driver).

    Attributes
    ----------
    fold_index
        Zero-based position of this fold in the schedule (chronological).
    train_start_day, train_end_day
        The fold's training window in trading-day indices ``[start, end)``.
    test_start_day, test_end_day
        The fold's held-out test window in trading-day indices ``[start, end)``.
    train_start_bar, train_end_bar
        The training window mapped to bar indices ``[start, end)``.
    test_start_bar, test_end_bar
        The test window mapped to bar indices ``[start, end)``.
    """

    fold_index: int
    train_start_day: int
    train_end_day: int
    test_start_day: int
    test_end_day: int
    train_start_bar: int
    train_end_bar: int
    test_start_bar: int
    test_end_bar: int


@dataclass(frozen=True, slots=True)
class WalkForwardSchedule:
    """A deterministic rolling walk-forward fold schedule + its identity hash.

    Produced by :func:`build_walk_forward_schedule`; consumed by the per-fold
    scorer in :mod:`brutex.engine.sweep_driver`. Holds the ordered folds plus the
    window parameters and the stream shape they were derived from, and a BLAKE3
    :attr:`schedule_hash` over the fold boundaries + parameters so a downstream
    run-hash can pin the exact layout (Golden Rule #4 reproducibility).

    Attributes
    ----------
    folds
        The walk-forward folds in chronological order (at least one — the builder
        raises rather than return an empty schedule).
    train_days, test_days, step_days
        The window parameters the schedule was built with (trading days).
    n_trading_days
        Distinct IST trading days in the source stream.
    n_bars
        Total bars in the source stream.
    schedule_hash
        64-char BLAKE3 hex digest of the canonical fold layout — see
        :func:`_walk_forward_schedule_hash`.
    """

    folds: tuple[WalkForwardFold, ...]
    train_days: int
    test_days: int
    step_days: int
    n_trading_days: int
    n_bars: int
    schedule_hash: str

    @property
    def n_folds(self) -> int:
        """Number of walk-forward folds in the schedule (``>= 1``)."""
        return len(self.folds)


def _day_to_bar(day_starts: Sequence[int], n_bars: int, day_index: int) -> int:
    """Map a half-open trading-day index to its bar index.

    For an in-range day this is its first-bar index; for the past-the-end day
    index (``== len(day_starts)``, used as an exclusive upper bound) it is
    ``n_bars`` (one past the last bar). :func:`build_walk_forward_schedule` only
    ever passes day indices in ``[0, len(day_starts)]``, so no other case arises.
    """
    if day_index < len(day_starts):
        return day_starts[day_index]
    return n_bars


def _walk_forward_schedule_hash(
    folds: tuple[WalkForwardFold, ...],
    train_days: int,
    test_days: int,
    step_days: int,
    n_trading_days: int,
    n_bars: int,
) -> str:
    """Return the 64-char BLAKE3 hex digest of a fold schedule's identity.

    Hashes a canonical JSON encoding of the window parameters, the source
    stream's shape (``n_trading_days`` + ``n_bars``), and every fold's **bar**
    boundaries (the executed contract — the day boundaries are implied by them)
    under a version tag. Including the stream shape closes the otherwise-possible
    aliasing where two streams of different length yielding coincidentally
    identical fold bar-bounds would share a hash. Assuming BLAKE3's
    collision-resistance, two schedules then share a digest only when their
    parameters, stream shape, and every fold's ``[train_start_bar, train_end_bar,
    test_start_bar, test_end_bar]`` all match. Uses the same
    ``fno_math.blake3_hash`` primitive and canonical-JSON rules as the combo
    identity hash (:func:`brutex.strategies.combo.combo_hash`) — one hashing
    source of truth (CLAUDE.md §11 COMMON).
    """
    payload = json.dumps(
        {
            "v": _WF_SCHEDULE_HASH_VERSION,
            "train_days": train_days,
            "test_days": test_days,
            "step_days": step_days,
            "n_trading_days": n_trading_days,
            "n_bars": n_bars,
            "folds": [
                [f.train_start_bar, f.train_end_bar, f.test_start_bar, f.test_end_bar]
                for f in folds
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return fno_math.blake3_hash(payload.encode("utf-8")).hex()


def build_walk_forward_schedule(
    bars: Sequence[CandleBar],
    *,
    train_days: int = DEFAULT_WF_TRAIN_DAYS,
    test_days: int = DEFAULT_WF_TEST_DAYS,
    step_days: int = DEFAULT_WF_STEP_DAYS,
) -> WalkForwardSchedule:
    """Build the deterministic rolling walk-forward schedule for a bar stream.

    Segments the stream into IST trading days (:func:`trading_day_starts`), then
    lays rolling folds of ``train_days`` training days immediately before
    ``test_days`` held-out test days, shifting ``step_days`` trading days per
    fold. The rolling-window arithmetic reuses the proven
    :func:`brutex.metrics.wfo.walk_forward_splits` (``ROLLING`` scheme, no
    embargo) — one source of truth for window stepping + the
    ``train_stop <= test_start`` no-look-ahead guarantee (CLAUDE.md §11 COMMON) —
    over the trading-day axis, and maps each day window to bar-index slices.

    The splitter is imported lazily so importing this module does not pull in the
    statistics stack (numpy via ``brutex.metrics``); the schedule build is a
    once-per-sweep call, never a per-bar hot path.

    Parameters
    ----------
    bars
        Chronologically ordered bars for one ``(instrument, interval)``
        (non-empty).
    train_days
        Trading days in each fold's training window (``>= 1``).
    test_days
        Trading days in each fold's held-out test window (``>= 1``).
    step_days
        Trading days to shift the windows between successive folds (``>= 1``).
        ``step_days == test_days`` tiles the test windows with no overlap/gap.

    Returns
    -------
    WalkForwardSchedule
        The ordered folds + window parameters + the BLAKE3 schedule hash.

    Raises
    ------
    ValueError
        If ``bars`` is empty, any window parameter is ``< 1``, or the stream has
        too few trading days to fit even one fold
        (``n_trading_days < train_days + test_days``).
    """
    if not bars:
        raise ValueError("build_walk_forward_schedule: bars must be non-empty")
    # Lazy import keeps this module's load free of the numpy-backed statistics
    # package; the rolling-window stepping itself is numpy-free.
    from brutex.metrics.wfo import WFOScheme, walk_forward_splits

    day_starts = trading_day_starts(bars)
    n_days = len(day_starts)
    n_bars = len(bars)
    walks = walk_forward_splits(
        n_days,
        WFOScheme.ROLLING,
        train_size=train_days,
        test_size=test_days,
        step=step_days,
    )
    if not walks:
        raise ValueError(
            f"build_walk_forward_schedule: {n_days} trading day(s) is too short for "
            f"train_days={train_days} + test_days={test_days} "
            f"(need >= {train_days + test_days}); widen the history or shrink the windows"
        )
    folds = tuple(
        WalkForwardFold(
            fold_index=i,
            train_start_day=walk.train_start,
            train_end_day=walk.train_stop,
            test_start_day=walk.test_start,
            test_end_day=walk.test_stop,
            train_start_bar=_day_to_bar(day_starts, n_bars, walk.train_start),
            train_end_bar=_day_to_bar(day_starts, n_bars, walk.train_stop),
            test_start_bar=_day_to_bar(day_starts, n_bars, walk.test_start),
            test_end_bar=_day_to_bar(day_starts, n_bars, walk.test_stop),
        )
        for i, walk in enumerate(walks)
    )
    return WalkForwardSchedule(
        folds=folds,
        train_days=train_days,
        test_days=test_days,
        step_days=step_days,
        n_trading_days=n_days,
        n_bars=n_bars,
        schedule_hash=_walk_forward_schedule_hash(
            folds, train_days, test_days, step_days, n_days, n_bars
        ),
    )


__all__ = [
    "ANCHOR_DEPENDENT_MASK",
    "DEFAULT_WF_STEP_DAYS",
    "DEFAULT_WF_TEST_DAYS",
    "DEFAULT_WF_TRAIN_DAYS",
    "FIB_DEPENDENT_MASK",
    "PIVOT_DEPENDENT_MASK",
    "FibAnchorKind",
    "Stage1AMatrixBuilder",
    "WalkForwardFold",
    "WalkForwardSchedule",
    "build_bar_bits",
    "build_walk_forward_schedule",
    "trading_day_starts",
]
