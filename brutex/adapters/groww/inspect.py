"""Candle-shape inspection — "what does Groww actually provide?" (the data probe).

The operator's first-run question: for the 3 index spots across every Groww
timeframe, does the data include **pre-market** (before 09:15 IST) and
**post-market** (after 15:30 IST) bars, and are the **timestamps precise**
(aligned to the interval grid)? `brutex fetch --inspect` answers it.

:func:`inspect_bars` is a pure classifier over a list of
:class:`~brutex.data.models.CandleBar` (UTC) for one ``(symbol, interval)``:
it converts each timestamp to IST, buckets it pre / regular / post-market
(NSE session 09:15–15:30 IST — ``CLAUDE.md §7``), and — for intraday minute
intervals — checks every regular-session bar lands on the interval grid from
09:15. :class:`CollectingBarSink` is a :class:`~brutex.data.sink.BarSink` that
accumulates bars in memory (instead of writing Parquet) so the probe can
inspect what was fetched without touching the lake.

Pure module (only ``CandleBar`` + stdlib) → standalone-coverage-gateable.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import time
from typing import Final
from zoneinfo import ZoneInfo

from brutex.data.models import CandleBar

#: IST — NSE session times are IST (``CLAUDE.md §7``).
_IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")

#: NSE regular session bounds (IST). Bars outside this are pre/post-market.
_SESSION_OPEN: Final[time] = time(9, 15)
_SESSION_CLOSE: Final[time] = time(15, 30)
_OPEN_MINUTES: Final[int] = _SESSION_OPEN.hour * 60 + _SESSION_OPEN.minute  # 555

#: Lake-canonical interval → minutes, for the timestamp-grid alignment check.
#: Keyed by the LONG partition names (what the sink stores + passes here, not
#: the fetch layer's short keys). Day/week/month have no intraday grid
#: (absent → alignment N/A).
_INTERVAL_MINUTES: Final[dict[str, int]] = {
    "1minute": 1,
    "2minute": 2,
    "3minute": 3,
    "5minute": 5,
    "10minute": 10,
    "15minute": 15,
    "30minute": 30,
    "1hour": 60,
    "4hour": 240,
}


@dataclass(frozen=True, slots=True)
class CandleInspection:
    """What Groww returned for one ``(symbol, interval)`` — the probe answer."""

    symbol: str
    interval: str
    bar_count: int
    pre_market: int
    regular: int
    post_market: int
    first_tod_ist: str | None
    last_tod_ist: str | None
    aligned: bool

    def summary(self) -> str:
        """One-line operator-facing summary (printed by ``fetch --inspect``)."""
        if self.bar_count == 0:
            return f"{self.symbol:<14} {self.interval:<4} — NO DATA"
        grid = "aligned" if self.aligned else "MISALIGNED"
        return (
            f"{self.symbol:<14} {self.interval:<4} "
            f"bars={self.bar_count:<6} "
            f"pre={self.pre_market} reg={self.regular} post={self.post_market} "
            f"ist[{self.first_tod_ist}..{self.last_tod_ist}] {grid}"
        )


def inspect_bars(symbol: str, interval: str, bars: Sequence[CandleBar]) -> CandleInspection:
    """Classify *bars* (UTC) into pre/regular/post-market (IST) + check alignment."""
    if not bars:
        return CandleInspection(symbol, interval, 0, 0, 0, 0, None, None, aligned=True)

    interval_minutes = _INTERVAL_MINUTES.get(interval)
    pre = regular = post = 0
    aligned = True
    ist_times: list[time] = []
    for bar in bars:
        ist = bar.timestamp.astimezone(_IST)
        tod = ist.time()
        ist_times.append(tod)
        if tod < _SESSION_OPEN:
            pre += 1
        elif tod > _SESSION_CLOSE:
            post += 1
        else:
            regular += 1
            if interval_minutes is not None:
                minutes_from_open = tod.hour * 60 + tod.minute - _OPEN_MINUTES
                if minutes_from_open % interval_minutes != 0:
                    aligned = False

    ist_times.sort()
    return CandleInspection(
        symbol=symbol,
        interval=interval,
        bar_count=len(bars),
        pre_market=pre,
        regular=regular,
        post_market=post,
        first_tod_ist=ist_times[0].isoformat(),
        last_tod_ist=ist_times[-1].isoformat(),
        aligned=aligned,
    )


@dataclass(slots=True)
class CollectingBarSink:
    """A :class:`~brutex.data.sink.BarSink` that accumulates bars in memory.

    Used by ``fetch --inspect`` so the probe can examine what was fetched
    without writing Parquet. Keyed by ``(exchange, segment, symbol, interval)``.
    """

    bars: dict[tuple[str, str, str, str], list[CandleBar]] = field(default_factory=dict)

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
    ) -> None:
        """Accumulate *bars* under their partition key (no persistence)."""
        self.bars.setdefault((exchange, segment, symbol, interval), []).extend(bars)

    def inspections(self) -> list[CandleInspection]:
        """Return one :class:`CandleInspection` per collected (symbol, interval)."""
        grouped: dict[tuple[str, str], list[CandleBar]] = defaultdict(list)
        for (_exch, _seg, symbol, interval), group in self.bars.items():
            grouped[symbol, interval].extend(group)
        return [
            inspect_bars(symbol, interval, group)
            for (symbol, interval), group in sorted(grouped.items())
        ]


__all__ = [
    "CandleInspection",
    "CollectingBarSink",
    "inspect_bars",
]
