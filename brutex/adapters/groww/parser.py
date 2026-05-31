"""Groww candle-row parser — raw API row → validated :class:`CandleBar`.

Groww's ``get_historical_candles`` returns each bar as a 7-element row
(``docs/groww/api-reference/03-backtesting.md``)::

    [timestamp, open, high, low, close, volume, open_interest_or_null]

The field-level rules (Decimal prices, empty-bar detection, int volume/OI,
OHLC sanity) live ONCE in :mod:`brutex.data.candle_fields` (CLAUDE.md §11
COMMON) and are shared with the Dhan parser. This module keeps only the
Groww-specific glue: the 7-element row shape and the **IST→UTC** timestamp
rule (``CLAUDE.md §6``: "convert to UTC at ingest").

:class:`CandleParseError` and :class:`EmptyCandleRow` are re-exported here
so existing importers (``persist``, tests) keep working unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final
from zoneinfo import ZoneInfo

from brutex.data.candle_fields import (
    CandleParseError,
    EmptyCandleRow,
    assemble_candle,
)
from brutex.data.models import CandleBar

#: India Standard Time — Groww candle timestamps are IST market times.
_IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")

#: A Groww candle row has exactly 7 fields.
_ROW_LEN: Final[int] = 7


def _parse_timestamp(raw: object) -> datetime:
    """Parse a Groww candle timestamp to a UTC tz-aware datetime.

    Accepts ``"yyyy-MM-dd HH:mm:ss"`` or ISO ``"...THH:mm:ss"``. A naive
    value is assumed IST and converted to UTC; an already-aware value is
    converted to UTC as-is.
    """
    if not isinstance(raw, str):
        raise CandleParseError(f"candle timestamp must be a string, got {type(raw).__name__}")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise CandleParseError(f"unparseable candle timestamp: {raw!r}") from exc
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    return parsed.replace(tzinfo=_IST).astimezone(UTC)


def parse_candle_row(row: Sequence[object]) -> CandleBar:
    """Convert one Groww candle row to a validated :class:`CandleBar`.

    Raises
    ------
    CandleParseError
        If the row is the wrong length, or any field is unparseable.
    EmptyCandleRow
        If every OHLC price is absent / empty / zero (graceful skip).
    ValueError
        Propagated from :class:`CandleBar` on inconsistent OHLC.
    """
    if len(row) != _ROW_LEN:
        raise CandleParseError(
            f"candle row must have {_ROW_LEN} fields "
            f"[ts, o, h, l, c, vol, oi], got {len(row)}: {row!r}"
        )
    ts_raw, open_raw, high_raw, low_raw, close_raw, vol_raw, oi_raw = row
    return assemble_candle(
        ts_raw=ts_raw,
        parse_timestamp=_parse_timestamp,
        open_=open_raw,
        high=high_raw,
        low=low_raw,
        close=close_raw,
        volume=vol_raw,
        open_interest=oi_raw,
    )


__all__ = [
    "CandleBar",
    "CandleParseError",
    "EmptyCandleRow",
    "parse_candle_row",
]
