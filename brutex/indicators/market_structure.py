"""Market structure — Break of Structure (BOS) + Change of Character (CHoCH).

Per ADR-0012 D3/D4 (DEC-NIFTY-BOS-CHOCH-001): the brute-force NIFTY-futures track
needs market-structure conditions for the ``bos_bullish`` / ``bos_bearish`` /
``choch_bullish`` / ``choch_bearish`` conditions (bits 56-59).

## ONE indicator, ONE source of truth (shared with TickVault)

This is the **bit-for-bit Python twin** of the Rust ``trading-core``
``indicators::market_structure::MarketStructureState`` (the native TickVault
path). Both work off CONFIRMED swing points (fractal pivots) with a running
trend:

* **BOS** (Break of Structure — trend CONTINUATION): in an up-trend the close
  takes out the most recent confirmed swing HIGH (bullish BOS); in a down-trend
  the close takes out the most recent confirmed swing LOW (bearish BOS). With no
  trend yet, the first such break also sets the trend and counts as a BOS.
* **CHoCH** (Change of Character — trend REVERSAL): the FIRST break OPPOSITE the
  prevailing trend — in a DOWN-trend the close takes out the last confirmed swing
  HIGH (bullish CHoCH → trend flips up); in an UP-trend the close takes out the
  last confirmed swing LOW (bearish CHoCH → trend flips down).

A single bar fires at most ONE of ``{bos_bullish, choch_bullish}`` and at most ONE
of ``{bos_bearish, choch_bearish}``; the up-side and down-side are independent (a
large engulfing bar can fire one of each).

## No look-ahead (Property P-06) — the crux, ADR-0012 §4.5 #13

A fractal swing at the centre of a ``(2k+1)``-bar window is only CONFIRMED once
the ``k`` bars AFTER it arrive. ``update`` confirms the CENTRE bar of the window
only when the window is full — i.e. when the bar ``k`` steps later has been fed.
So when evaluating bar ``N`` the caller has fed bars ``0..=N``, and the newest
swing the machine can expose sits at index ``N − k`` (its confirmation needed
bars ``N−k+1 ..= N``, all ``≤ N``). The CURRENT bar's own level is NEVER treated
as a confirmed swing — only a strictly past, fully-windowed bar is. This is
structurally identical to the existing streaming indicators (:class:`VwapState`,
``DayExtremesState``): bars are fed once, in arrival order, nothing future is read.

## O(1) per bar (CLAUDE.md Rule #5)

State = a fixed ``(2k+1)``-element ring of ``(high, low)`` + a handful of ints +
a trend enum. Each ``update`` does one ring write, an O(k)-with-k-CONSTANT
local-extreme scan over the fixed window, and a few comparisons — O(1) per bar (k
is a small fixed parameter, NOT the bar count). No growing history, O(1) memory.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Trend(Enum):
    """The prevailing market-structure trend."""

    NONE = "none"
    UP = "up"
    DOWN = "down"


@dataclass(frozen=True)
class MarketStructureSignal:
    """The four resolved BOS/CHoCH booleans for one bar.

    Fed into the evaluator (and the Rust ``evaluate_with_levels`` ABI) exactly
    like the resolved :class:`~brutex.vocabulary.evaluator.PriorBarHistory` flags.
    All-false during warm-up (before any confirmed swing) so the Group-12 bits
    simply do not fire.
    """

    bos_bullish: bool = False
    bos_bearish: bool = False
    choch_bullish: bool = False
    choch_bearish: bool = False


@dataclass
class MarketStructureState:
    """Streaming BOS/CHoCH detector with fractal half-window ``k`` (``k >= 1``).

    Hold once per (instrument, timeframe, contract); call :meth:`update` per new
    bar in arrival order. Bit-identical to the Rust ``trading-core``
    ``MarketStructureState`` (the TickVault-native path), so the backtested
    BOS/CHoCH equals the live one by construction (DEC-SHARED-CORE-001 / D7).

    ``k = 2`` (a 5-bar fractal) is the production default: a swing high is a bar
    strictly higher than its 2 neighbours on each side.

    Attributes:
        k: Fractal half-window — a swing needs ``k`` bars on EACH side to confirm.
        window: Ring of the last ``2k+1`` bars' ``(high, low)``, oldest→newest.
        count: Total bars fed (so the centre of a full window is at ``count-1-k``).
        last_swing_high: Most recent CONFIRMED swing high (paisa), or ``None``.
        last_swing_low: Most recent CONFIRMED swing low (paisa), or ``None``.
        trend: The prevailing trend.
    """

    k: int = 2
    window: deque[tuple[int, int]] = field(default_factory=deque)
    count: int = 0
    last_swing_high: int | None = None
    last_swing_low: int | None = None
    trend: Trend = Trend.NONE

    def __post_init__(self) -> None:
        """Validate ``k`` and bound the ring to ``2k+1`` (O(1) memory)."""
        if self.k < 1:
            raise ValueError(f"fractal half-window k must be >= 1, got {self.k}")
        # A maxlen deque drops its oldest element on append once full — the O(1)
        # ring. Re-wrap so a deserialized/hand-built window inherits the bound.
        self.window = deque(self.window, maxlen=2 * self.k + 1)

    def update(self, high: int, low: int, close: int) -> MarketStructureSignal:
        """Feed one bar (high/low/close in int64 paisa); return its BOS/CHoCH signal.

        The order of operations is the load-bearing look-ahead invariant:

        1. push the bar into the confirmation ring (the window now ends at the
           CURRENT bar);
        2. if the window is full, CONFIRM its CENTRE bar (a STRICTLY PAST bar) as a
           swing high/low — never the current bar;
        3. test the CURRENT bar's ``close`` against the levels confirmed in step 2
           (or earlier) and resolve BOS/CHoCH + update the trend.

        O(1) per bar (``k`` is a fixed constant).

        Args:
            high: The bar's high (int64 paisa).
            low: The bar's low (int64 paisa).
            close: The bar's close (int64 paisa).

        Returns:
            This bar's :class:`MarketStructureSignal`.
        """
        self.window.append((high, low))
        self.count += 1
        self._confirm_centre_swing()
        return self._resolve_break(close)

    def _confirm_centre_swing(self) -> None:
        """If the ring is full, confirm its CENTRE bar as a swing high/low.

        The centre is index ``k`` of the ``2k+1``-element ring — a bar ``k`` steps
        in the past relative to the just-pushed current bar. A strict local maximum
        over the ``2k`` neighbours is a confirmed swing high; a strict local minimum
        is a confirmed swing low (a bar can be neither). The current bar (ring index
        ``2k``) is never the centre, so its level is never confirmed here — the
        look-ahead-clean guarantee.
        """
        if len(self.window) < 2 * self.k + 1:
            return  # not enough future bars yet to confirm the centre
        bars = list(self.window)
        centre_high, centre_low = bars[self.k]
        is_high = all(b[0] < centre_high for i, b in enumerate(bars) if i != self.k)
        is_low = all(b[1] > centre_low for i, b in enumerate(bars) if i != self.k)
        if is_high:
            self.last_swing_high = centre_high
        if is_low:
            self.last_swing_low = centre_low

    def _resolve_break(self, close: int) -> MarketStructureSignal:
        """Test the current ``close`` against the confirmed swings → BOS/CHoCH + trend.

        Up-side and down-side are resolved independently (a single engulfing bar can
        break both). Whether a break is a BOS (continuation) or a CHoCH (reversal)
        depends ONLY on the prevailing trend at entry: a bullish break in a
        Down-trend is a CHoCH; otherwise a BOS. The trend is then moved to match the
        break.
        """
        entry_trend = self.trend
        bos_bullish = False
        bos_bearish = False
        choch_bullish = False
        choch_bearish = False

        if self.last_swing_high is not None and close > self.last_swing_high:
            if entry_trend is Trend.DOWN:
                choch_bullish = True
            else:
                bos_bullish = True
            self.trend = Trend.UP
        if self.last_swing_low is not None and close < self.last_swing_low:
            if entry_trend is Trend.UP:
                choch_bearish = True
            else:
                bos_bearish = True
            self.trend = Trend.DOWN

        return MarketStructureSignal(
            bos_bullish=bos_bullish,
            bos_bearish=bos_bearish,
            choch_bullish=choch_bullish,
            choch_bearish=choch_bearish,
        )

    def reset(self) -> None:
        """Forget all history. Used at start-of-session-replay or in tests."""
        self.window = deque(maxlen=2 * self.k + 1)
        self.count = 0
        self.last_swing_high = None
        self.last_swing_low = None
        self.trend = Trend.NONE

    def to_dict(self) -> dict[str, Any]:
        """Serialize for checkpoint persistence."""
        return {
            "k": self.k,
            "window": [list(b) for b in self.window],
            "count": self.count,
            "last_swing_high": self.last_swing_high,
            "last_swing_low": self.last_swing_low,
            "trend": self.trend.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MarketStructureState:
        """Restore from :meth:`to_dict` output (checkpoint resume)."""
        return cls(
            k=int(data["k"]),
            window=deque((int(b[0]), int(b[1])) for b in data["window"]),
            count=int(data["count"]),
            last_swing_high=(
                int(data["last_swing_high"]) if data["last_swing_high"] is not None else None
            ),
            last_swing_low=(
                int(data["last_swing_low"]) if data["last_swing_low"] is not None else None
            ),
            trend=Trend(data["trend"]),
        )
