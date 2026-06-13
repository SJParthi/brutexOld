"""Shared test doubles for the volume/volatility movers screener.

Kept in ``tests/fakes`` (not a single test module) so every screener test —
unit / property / golden / bench / stress — imports ONE candle builder + ONE
fake lake reader, never copy-pastes them (CLAUDE.md §11 COMMON, zero
duplication).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from brutex.data.models import CandleBar
from brutex.screeners.volume_movers import ScreenTarget

#: 2025-01-02 09:15 IST expressed in UTC — a valid intraday session open.
_EPOCH: datetime = datetime(2025, 1, 2, 3, 45, tzinfo=UTC)

#: The empty bar sequence a fake returns for an unknown target (→ skipped).
_EMPTY: tuple[CandleBar, ...] = ()


def make_candle(
    *,
    high: str | int,
    low: str | int,
    close: str | int,
    volume: int,
    minute: int = 0,
    open_: str | int | None = None,
    open_interest: int | None = None,
) -> CandleBar:
    """Build one valid :class:`~brutex.data.models.CandleBar` for tests.

    ``open_`` defaults to ``close`` (always within ``[low, high]`` when the
    caller passes a valid close), and the timestamp is ``_EPOCH + minute``.
    """
    open_price = close if open_ is None else open_
    return CandleBar(
        timestamp=_EPOCH + timedelta(minutes=minute),
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
        open_interest=open_interest,
    )


class FakeLakeBarReader:
    """An in-memory :class:`~brutex.screeners.volume_movers.LakeBarReader`.

    Returns the pre-seeded bars for a target (an empty sequence for an unknown
    target, which the screener reports as skipped) and records every call so a
    test can assert the orchestration read exactly what it should.
    """

    def __init__(self, data: Mapping[ScreenTarget, Sequence[CandleBar]]) -> None:
        self._data: dict[ScreenTarget, Sequence[CandleBar]] = dict(data)
        self.calls: list[tuple[ScreenTarget, datetime, datetime]] = []

    def read_bars(
        self, target: ScreenTarget, *, start: datetime, end: datetime
    ) -> Sequence[CandleBar]:
        """Record the call and return *target*'s seeded bars (or empty)."""
        self.calls.append((target, start, end))
        return self._data.get(target, _EMPTY)
