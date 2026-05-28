"""Groww candle-row parser — raw API row → validated :class:`CandleBar`.

Groww's ``get_historical_candles`` returns each bar as a 7-element row
(``docs/groww/api-reference/03-backtesting.md``)::

    [timestamp, open, high, low, close, volume, open_interest_or_null]

This module converts one such row into the canonical
:class:`~brutex.data.models.CandleBar`, applying the two correctness
rules the operator cares most about ("no data mismatch"):

* **Timestamps** — Groww emits IST market times (``CLAUDE.md §6``:
  "convert to UTC at ingest"). A naive ``"yyyy-MM-dd HH:mm:ss"`` /
  ISO string is localized to Asia/Kolkata then converted to UTC, so the
  whole lake is UTC (matching the resampler's ``ts_utc`` convention).
* **Prices** — parsed as :class:`~decimal.Decimal` via ``str(...)`` to
  avoid binary-float drift (``NFR-DET-001`` bit-exactness).

The :class:`CandleBar` constructor itself validates OHLC sanity
(``high >= low``, ``open``/``close`` within ``[low, high]``, non-negative
volume/OI) — so a corrupt row raises rather than silently entering the
lake. That validation IS a mismatch guard, not just a convenience.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Final
from zoneinfo import ZoneInfo

from brutex.data.models import CandleBar
from brutex.models import BruteXError

#: India Standard Time — Groww candle timestamps are IST market times.
_IST: Final[ZoneInfo] = ZoneInfo("Asia/Kolkata")

#: A Groww candle row has exactly 7 fields.
_ROW_LEN: Final[int] = 7


class CandleParseError(BruteXError):
    """A Groww candle row violates the documented shape or value rules."""


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


def _to_price(raw: object, field: str) -> Decimal:
    """Parse a price field to exact Decimal rupees (via str, no float drift).

    Rejects non-finite values: ``float('inf')`` / ``float('nan')`` round-trip
    through ``Decimal`` as ``Infinity`` / ``NaN`` (no parse error), so we guard
    explicitly — a non-finite price is a data mismatch, never a valid bar.
    """
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise CandleParseError(f"unparseable {field} price: {raw!r}") from exc
    if not value.is_finite():
        raise CandleParseError(f"non-finite {field} price: {raw!r}")
    return value


def _to_int(raw: object, field: str) -> int:
    """Parse an integer field (volume / OI); reject bools + non-numerics."""
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        raise CandleParseError(f"{field} must be an integer, got bool: {raw!r}")
    if not isinstance(raw, (int, float, str)):
        raise CandleParseError(f"unparseable {field}: {raw!r}")
    try:
        return int(raw)
    except (ValueError, OverflowError) as exc:
        # OverflowError covers ``int(float('inf'))``; ValueError covers
        # ``int('xyz')`` / ``int(float('nan'))``.
        raise CandleParseError(f"unparseable {field}: {raw!r}") from exc


def parse_candle_row(row: Sequence[object]) -> CandleBar:
    """Convert one Groww candle row to a validated :class:`CandleBar`.

    Raises
    ------
    CandleParseError
        If the row is the wrong length, or any field is unparseable.
    ValueError
        Propagated from :class:`CandleBar` when OHLC values are
        internally inconsistent (high < low, open/close out of range,
        negative volume/OI) — a data-mismatch guard.
    """
    if len(row) != _ROW_LEN:
        raise CandleParseError(
            f"candle row must have {_ROW_LEN} fields "
            f"[ts, o, h, l, c, vol, oi], got {len(row)}: {row!r}"
        )
    ts_raw, open_raw, high_raw, low_raw, close_raw, vol_raw, oi_raw = row
    open_interest = None if oi_raw is None else _to_int(oi_raw, "open_interest")
    return CandleBar(
        timestamp=_parse_timestamp(ts_raw),
        open=_to_price(open_raw, "open"),
        high=_to_price(high_raw, "high"),
        low=_to_price(low_raw, "low"),
        close=_to_price(close_raw, "close"),
        volume=_to_int(vol_raw, "volume"),
        open_interest=open_interest,
    )


__all__ = [
    "CandleParseError",
    "parse_candle_row",
]
