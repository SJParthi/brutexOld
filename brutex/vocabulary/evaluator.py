"""The 52-condition evaluator.

Takes an `IndicatorSnapshot` (all per-bar indicator values) plus
proximity parameters and prior-bar history, and returns a u64 bitmask
where bit `i` is set iff condition `i` is true.

Per `docs/TRACK1_VOCABULARY.md` definitions. Each condition is a pure
function of inputs — no internal state — so the evaluator is
trivially streaming-safe (live tick path) AND batch-safe (backtest
path). Output is bit-identical regardless of which path called it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Final

from brutex.indicators.fib_levels import FibLevels
from brutex.indicators.pivots import PivotLevels
from brutex.vocabulary import bits as B

# ────── Input dataclasses ──────


@dataclass(frozen=True)
class IndicatorSnapshot:
    """All per-bar indicator values consumed by the evaluator.

    Money values are int64 paisa. Timestamps are timezone-aware IST.
    None values represent indicators that are still warming up (e.g.,
    EMA before bar 19/199).
    """

    # Bar OHLC + timestamp (IST timezone)
    timestamp_ist: datetime
    open_paisa: int
    high_paisa: int
    low_paisa: int
    close_paisa: int

    # EMA values (may be None during warm-up)
    ema20_paisa: int | None
    ema200_paisa: int | None

    # Standard Pivot Points for the day
    pivots: PivotLevels

    # Static Daily Fibonacci levels
    fib: FibLevels

    # Running day extremes (current bar's values incorporated)
    day_high_paisa: int
    day_low_paisa: int

    # Today's open price (computed at session start, constant intraday)
    open_of_day_paisa: int


@dataclass(frozen=True)
class ProximityParams:
    """Proximity tolerances (in basis points) for `near_*` conditions.

    `near` means `abs(close - level) <= close * bps / 10000`.

    Brute-force engine iterates over bps ∈ {5, 10, 25, 50}.
    """

    pivot_proximity_bps: int = 10
    pdh_pdl_proximity_bps: int = 10
    fib_proximity_bps: int = 10


@dataclass
class PriorBarHistory:
    """Rolling history of the last N bars' bullish/bearish flags.

    Used by Group 6 (prior bar pattern) conditions. The strategy
    engine maintains one of these per (instrument, TF) pair.

    `prior_n` = the look-back count parametrised ∈ {1, 2, 3, 4}.
    """

    prior_n: int = 3
    flags: list[bool] = field(default_factory=list)  # True = bullish, False = bearish

    def update(self, is_bullish: bool) -> None:
        """Record this bar's bullish/bearish flag. O(1) per call."""
        self.flags.append(is_bullish)
        if len(self.flags) > self.prior_n:
            self.flags = self.flags[-self.prior_n:]

    def all_bullish(self) -> bool:
        """True if the last `prior_n` bars are all bullish."""
        return len(self.flags) == self.prior_n and all(self.flags)

    def all_bearish(self) -> bool:
        """True if the last `prior_n` bars are all bearish."""
        return len(self.flags) == self.prior_n and not any(self.flags)

    def alternating(self) -> bool:
        """True if the last `prior_n` bars alternate bull/bear/bull/bear...

        N=1 trivially satisfies (always considered alternating).
        N=2+: bar[t-N] vs bar[t-N+1] differ, bar[t-N+1] vs bar[t-N+2] differ, ...
        """
        if len(self.flags) != self.prior_n:
            return False
        for i in range(len(self.flags) - 1):
            if self.flags[i] == self.flags[i + 1]:
                return False
        return True


# ────── Helper functions ──────


# Time-of-day cutoffs (IST). Per vocab spec Group 8.
_EARLY_MORNING_START: Final[time] = time(9, 16)
_EARLY_MORNING_END:   Final[time] = time(10, 0)
_MID_MORNING_END:     Final[time] = time(11, 30)
_MIDDAY_END:          Final[time] = time(13, 0)
_AFTERNOON_END:       Final[time] = time(15, 0)


def _is_near(close_paisa: int, level_paisa: int, bps: int) -> bool:
    """`abs(close - level) <= close * bps / 10000` — proximity check.

    O(1): 2 subtractions, 1 multiplication, 1 division, 1 comparison.
    """
    if close_paisa <= 0:
        return False
    tolerance = (close_paisa * bps) // 10000
    return abs(close_paisa - level_paisa) <= tolerance


def _set_bit(mask: int, bit: int, value: bool) -> int:
    """Set or clear `bit` in `mask` based on `value`."""
    if value:
        return mask | (1 << bit)
    return mask & ~(1 << bit)


# ────── The 52-condition evaluator ──────


def evaluate_atoms(
    snapshot: IndicatorSnapshot,
    proximity: ProximityParams,
    prior_history: PriorBarHistory,
) -> int:
    """Evaluate all 52 atomic conditions on one bar.

    Args:
        snapshot: All per-bar indicator values (OHLC + EMA + Pivots +
                  Fib + Day Hi/Lo + open_of_day).
        proximity: Tolerances for `near_*` conditions.
        prior_history: Rolling N-bar bullish/bearish flags for Group 6.

    Returns:
        u64 bitmask with bits 0..51 set per condition. Bits 52-63 always 0.

    O(1) per bar: ~50 cache-resident ops, no allocations, no I/O.
    """
    mask = 0
    close = snapshot.close_paisa

    # ──────── Group 1: EMA Position (bits 0-5) ────────
    if snapshot.ema20_paisa is not None:
        if close > snapshot.ema20_paisa:
            mask |= 1 << B.BIT_CLOSE_ABOVE_EMA20
        if close < snapshot.ema20_paisa:
            mask |= 1 << B.BIT_CLOSE_BELOW_EMA20
    if snapshot.ema200_paisa is not None:
        if close > snapshot.ema200_paisa:
            mask |= 1 << B.BIT_CLOSE_ABOVE_EMA200
        if close < snapshot.ema200_paisa:
            mask |= 1 << B.BIT_CLOSE_BELOW_EMA200
    if snapshot.ema20_paisa is not None and snapshot.ema200_paisa is not None:
        if snapshot.ema20_paisa > snapshot.ema200_paisa:
            mask |= 1 << B.BIT_EMA20_ABOVE_EMA200
        if snapshot.ema20_paisa < snapshot.ema200_paisa:
            mask |= 1 << B.BIT_EMA20_BELOW_EMA200

    # ──────── Group 2: Pivot Proximity (bits 6-12) ────────
    bps = proximity.pivot_proximity_bps
    if _is_near(close, snapshot.pivots.p_paisa, bps):
        mask |= 1 << B.BIT_NEAR_PIVOT_P
    if _is_near(close, snapshot.pivots.r1_paisa, bps):
        mask |= 1 << B.BIT_NEAR_PIVOT_R1
    if _is_near(close, snapshot.pivots.r2_paisa, bps):
        mask |= 1 << B.BIT_NEAR_PIVOT_R2
    if _is_near(close, snapshot.pivots.r3_paisa, bps):
        mask |= 1 << B.BIT_NEAR_PIVOT_R3
    if _is_near(close, snapshot.pivots.s1_paisa, bps):
        mask |= 1 << B.BIT_NEAR_PIVOT_S1
    if _is_near(close, snapshot.pivots.s2_paisa, bps):
        mask |= 1 << B.BIT_NEAR_PIVOT_S2
    if _is_near(close, snapshot.pivots.s3_paisa, bps):
        mask |= 1 << B.BIT_NEAR_PIVOT_S3

    # ──────── Group 3: PDH/PDL (bits 13-18) ────────
    pdh = snapshot.pivots.pdh_paisa
    pdl = snapshot.pivots.pdl_paisa
    if close > pdh:
        mask |= 1 << B.BIT_CLOSE_ABOVE_PDH
    if close < pdh:
        mask |= 1 << B.BIT_CLOSE_BELOW_PDH
    if close > pdl:
        mask |= 1 << B.BIT_CLOSE_ABOVE_PDL
    if close < pdl:
        mask |= 1 << B.BIT_CLOSE_BELOW_PDL
    bps_pp = proximity.pdh_pdl_proximity_bps
    if _is_near(close, pdh, bps_pp):
        mask |= 1 << B.BIT_NEAR_PDH
    if _is_near(close, pdl, bps_pp):
        mask |= 1 << B.BIT_NEAR_PDL

    # ──────── Group 4: Fib Proximity (bits 19-29) ────────
    bps_fib = proximity.fib_proximity_bps
    # Bits 19-25: retracements (use bearish convention as the canonical
    # naming for the 7 in-range levels). Bits 26-29: extensions.
    fib = snapshot.fib
    if _is_near(close, fib.retrace_level_at_pdh_paisa, bps_fib):
        mask |= 1 << B.BIT_NEAR_FIB_0
    if _is_near(close, fib.retrace_23_paisa, bps_fib):
        mask |= 1 << B.BIT_NEAR_FIB_236
    if _is_near(close, fib.retrace_38_paisa, bps_fib):
        mask |= 1 << B.BIT_NEAR_FIB_382
    if _is_near(close, fib.retrace_50_paisa, bps_fib):
        mask |= 1 << B.BIT_NEAR_FIB_50
    if _is_near(close, fib.retrace_62_paisa, bps_fib):
        mask |= 1 << B.BIT_NEAR_FIB_618
    if _is_near(close, fib.retrace_78_paisa, bps_fib):
        mask |= 1 << B.BIT_NEAR_FIB_786
    if _is_near(close, fib.retrace_level_at_pdl_paisa, bps_fib):
        mask |= 1 << B.BIT_NEAR_FIB_100
    # Extensions: try BOTH bullish (up) and bearish (down) variants;
    # if either is near, the bit fires. Strategy engine combines this
    # with direction parameter to interpret which side the level lies on.
    for ext_up, ext_down, bit_pos in (
        (fib.ext_up_127_paisa, fib.ext_down_127_paisa, B.BIT_NEAR_FIB_1272),
        (fib.ext_up_162_paisa, fib.ext_down_162_paisa, B.BIT_NEAR_FIB_1618),
        (fib.ext_up_200_paisa, fib.ext_down_200_paisa, B.BIT_NEAR_FIB_200),
        (fib.ext_up_262_paisa, fib.ext_down_262_paisa, B.BIT_NEAR_FIB_2618),
    ):
        if _is_near(close, ext_up, bps_fib) or _is_near(close, ext_down, bps_fib):
            mask |= 1 << bit_pos

    # ──────── Group 5: Candle Shape (bits 30-36) ────────
    o = snapshot.open_paisa
    h = snapshot.high_paisa
    l = snapshot.low_paisa
    c = close
    body = abs(c - o)
    total_range = h - l

    if c > o:
        mask |= 1 << B.BIT_BAR_BULLISH
    if c < o:
        mask |= 1 << B.BIT_BAR_BEARISH
    # Doji: body < 10% of total range (only meaningful if range > 0)
    if total_range > 0 and body * 10 < total_range:
        mask |= 1 << B.BIT_BAR_DOJI
    if total_range > 0:
        # Large body: body > 70% of range
        if body * 10 > total_range * 7:
            mask |= 1 << B.BIT_BAR_LARGE_BODY
        # Small body: body < 30% of range
        if body * 10 < total_range * 3:
            mask |= 1 << B.BIT_BAR_SMALL_BODY
        # Wicks
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        if upper_wick * 2 > total_range:
            mask |= 1 << B.BIT_LONG_UPPER_WICK
        if lower_wick * 2 > total_range:
            mask |= 1 << B.BIT_LONG_LOWER_WICK

    # ──────── Group 6: Prior Bar Pattern (bits 37-39) ────────
    # Uses prior_history (the last N bars BEFORE this bar — caller updates
    # AFTER evaluation so this bar's flag isn't in history yet).
    if prior_history.all_bullish():
        mask |= 1 << B.BIT_PRIOR_N_BULLISH
    if prior_history.all_bearish():
        mask |= 1 << B.BIT_PRIOR_N_BEARISH
    if prior_history.alternating():
        mask |= 1 << B.BIT_PRIOR_ALTERNATING

    # ──────── Group 7: Day Structure (bits 40-43) ────────
    if c == snapshot.day_high_paisa:
        mask |= 1 << B.BIT_CLOSE_AT_DAY_HIGH
    if c == snapshot.day_low_paisa:
        mask |= 1 << B.BIT_CLOSE_AT_DAY_LOW
    day_range = snapshot.day_high_paisa - snapshot.day_low_paisa
    if day_range > 0:
        # Upper third: close > day_low + range * 2/3
        if (c - snapshot.day_low_paisa) * 3 > day_range * 2:
            mask |= 1 << B.BIT_CLOSE_IN_UPPER_THIRD
        # Lower third: close < day_low + range * 1/3
        if (c - snapshot.day_low_paisa) * 3 < day_range:
            mask |= 1 << B.BIT_CLOSE_IN_LOWER_THIRD

    # ──────── Group 8: Time of Day (bits 44-47) ────────
    t = snapshot.timestamp_ist.time()
    if _EARLY_MORNING_START <= t < _EARLY_MORNING_END:
        mask |= 1 << B.BIT_EARLY_MORNING
    elif _EARLY_MORNING_END <= t < _MID_MORNING_END:
        mask |= 1 << B.BIT_MID_MORNING
    elif _MID_MORNING_END <= t < _MIDDAY_END:
        mask |= 1 << B.BIT_MIDDAY
    elif _MIDDAY_END <= t < _AFTERNOON_END:
        mask |= 1 << B.BIT_AFTERNOON

    # ──────── Group 9: Day Type (bits 48-51) ────────
    # Gap detection: today's open vs PDC (= previous day's close).
    pdc = snapshot.pivots.pdc_paisa
    if pdc > 0:
        # 0.5% threshold = 50 bps
        gap_threshold = (pdc * 50) // 10000  # 0.5% of pdc
        if snapshot.open_of_day_paisa > pdc + gap_threshold:
            mask |= 1 << B.BIT_GAP_UP_DAY
        if snapshot.open_of_day_paisa < pdc - gap_threshold:
            mask |= 1 << B.BIT_GAP_DOWN_DAY
    # Inside day: today's day_high < PDH AND day_low > PDL
    if snapshot.day_high_paisa < pdh and snapshot.day_low_paisa > pdl:
        mask |= 1 << B.BIT_INSIDE_DAY
    # Outside day: today's day_high > PDH AND day_low < PDL
    if snapshot.day_high_paisa > pdh and snapshot.day_low_paisa < pdl:
        mask |= 1 << B.BIT_OUTSIDE_DAY

    return mask
