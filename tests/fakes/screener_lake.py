"""In-memory fake :class:`LakeReader` for the gainers/losers screener tests.

A single, shared test double (CLAUDE.md §11 COMMON — one fake, reused by the
unit / property / golden / stress suites) that satisfies
:class:`brutex.screeners.gainers_losers.LakeReader` without any Parquet /
DuckDB dependency. It maps each :class:`InstrumentRef` to a canned list of
:class:`~brutex.data.models.CandleBar` and, like the real
``Catalog.get_bars``, returns only the bars whose timestamp falls inside the
requested ``[start, end]`` window. Every ``read_window`` call is recorded so a
test can assert the interval / window propagation.

This module lives under ``tests/fakes/`` and is never shipped in the wheel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.screeners.gainers_losers import InstrumentRef

#: A fixed UTC anchor for deterministic test timestamps.
T0: datetime = datetime(2024, 1, 2, 4, 0, 0, tzinfo=UTC)


def make_bar(
    *,
    minute: int,
    open_: str,
    close: str,
    high: str | None = None,
    low: str | None = None,
    volume: int = 0,
) -> CandleBar:
    """Build a valid :class:`CandleBar` at ``T0 + minute`` with the given prices.

    ``high`` / ``low`` default to the max / min of ``open_`` / ``close`` so the
    OHLC ordering invariant always holds.
    """
    o = Decimal(open_)
    c = Decimal(close)
    hi = Decimal(high) if high is not None else max(o, c)
    lo = Decimal(low) if low is not None else min(o, c)
    return CandleBar(
        timestamp=T0 + timedelta(minutes=minute),
        open=o,
        high=hi,
        low=lo,
        close=c,
        volume=volume,
        open_interest=None,
    )


@dataclass(slots=True)
class FakeLakeReader:
    """A :class:`LakeReader` backed by an in-memory ``ref -> bars`` mapping.

    Attributes
    ----------
    data
        Maps each :class:`InstrumentRef` to its full bar list (any timestamps).
    calls
        Records every ``read_window`` call as
        ``(ref, interval, start, end)`` — for propagation assertions.
    """

    data: dict[InstrumentRef, list[CandleBar]]
    calls: list[tuple[InstrumentRef, str, datetime, datetime]] = field(default_factory=list)

    def list_instruments(self) -> list[InstrumentRef]:
        """Return the configured instruments (insertion order)."""
        return list(self.data)

    def read_window(
        self,
        ref: InstrumentRef,
        *,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[CandleBar]:
        """Return *ref*'s bars inside ``[start, end]`` (inclusive), ascending."""
        self.calls.append((ref, interval, start, end))
        bars = [b for b in self.data.get(ref, []) if start <= b.timestamp <= end]
        return sorted(bars, key=lambda b: b.timestamp)
