"""Parse raw 7-element candle rows into :class:`CandleBar` instances.

The legacy 7-element row candle format is::

    [timestamp_iso, open, high, low, close, volume, open_interest_or_none]

* ``timestamp_iso`` — ``YYYY-MM-DDTHH:mm:ss`` (no timezone suffix, but
  these are **IST** market times and the adapter MUST convert to UTC
  on ingest).
* ``open/high/low/close`` — floats (price in rupees).
* ``volume`` — integer trade volume.
* ``open_interest`` — integer OI for F&O instruments, ``None`` for cash
  equity bars.

This module is **pure** — no imports of polars / duckdb / pyarrow —
so it participates in the unit-test suite without the P2 data-lake
dependency extras installed.

Note: the live vendor path uses the parser in
``brutex/adapters/groww/parser.py``; this row parser is the
canonical/legacy 7-field shape.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timezone
from decimal import Decimal
from typing import Any, Final

from brutex.calendars.nse import IST_UTC_OFFSET
from brutex.data.errors import DataLakeError
from brutex.data.models import CandleBar

#: India Standard Time offset (+05:30). No DST — IST is fixed. The offset is
#: DERIVED from the ONE canonical :data:`brutex.calendars.nse.IST_UTC_OFFSET`
#: root (DEC-TIMESTAMP-IST-SINGLESOURCE-001); the ``name="IST"`` tzname is this
#: module's pre-existing, test-pinned representation detail (kept byte-identical
#: — deliberately NOT the unnamed shared :data:`~brutex.calendars.nse.IST_FIXED_OFFSET`,
#: whose ``tzname()`` reads ``'UTC+05:30'``).
_IST: Final[timezone] = timezone(IST_UTC_OFFSET, name="IST")

#: Number of fields in a 7-element candle row: 7-tuple
#: ``[timestamp, open, high, low, close, volume, open_interest]``.
BROKER_CANDLE_ROW_FIELDS: Final[int] = 7


class CandleRowError(DataLakeError):
    """Raised when a raw 7-element candle row cannot be parsed.

    The error message always names the offending field so debugging
    a bad upstream payload is a single log-line lookup.
    """


def parse_candle_row(row: Sequence[Any]) -> CandleBar:
    """Convert one raw 7-element candle row into a :class:`CandleBar`.

    Parameters
    ----------
    row
        A 7-element sequence ``[ts_iso, o, h, l, c, v, oi]``.

    Returns
    -------
    CandleBar
        Normalised bar with a UTC timestamp and Decimal prices.

    Raises
    ------
    CandleRowError
        If the row is wrong length, has a malformed timestamp, or
        contains a non-numeric price / volume field.
    """
    if len(row) != BROKER_CANDLE_ROW_FIELDS:
        raise CandleRowError(
            f"the candle row must have {BROKER_CANDLE_ROW_FIELDS} elements "
            f"[ts, o, h, l, c, v, oi], got {len(row)}: {row!r}"
        )
    ts_raw, open_raw, high_raw, low_raw, close_raw, vol_raw, oi_raw = row

    # ── Timestamp: IST ISO → UTC ──────────────────────────────────────
    if not isinstance(ts_raw, str):
        raise CandleRowError(f"timestamp must be an ISO string, got {type(ts_raw).__name__}")
    try:
        # The row uses 'T' separator; fromisoformat handles both 'T' and ' '.
        naive = datetime.fromisoformat(ts_raw)
    except ValueError as exc:
        raise CandleRowError(f"timestamp {ts_raw!r} is not valid ISO-8601") from exc
    # The row omits the offset; attach IST then convert to UTC.
    if naive.tzinfo is None:
        naive = naive.replace(tzinfo=_IST)
    ts_utc = naive.astimezone(UTC)

    # ── Prices → Decimal via str (avoid binary-float drift) ──────────
    try:
        open_px = _to_decimal(open_raw, "open")
        high_px = _to_decimal(high_raw, "high")
        low_px = _to_decimal(low_raw, "low")
        close_px = _to_decimal(close_raw, "close")
    except (TypeError, ValueError) as exc:
        raise CandleRowError(str(exc)) from exc

    # ── Volume + Open Interest ──────────────────────────────────────
    try:
        volume = int(vol_raw)
    except (TypeError, ValueError) as exc:
        raise CandleRowError(f"volume {vol_raw!r} is not an integer") from exc

    open_interest: int | None
    if oi_raw is None:
        open_interest = None
    else:
        try:
            open_interest = int(oi_raw)
        except (TypeError, ValueError) as exc:
            raise CandleRowError(f"open_interest {oi_raw!r} is not an integer or None") from exc

    try:
        return CandleBar(
            timestamp=ts_utc,
            open=open_px,
            high=high_px,
            low=low_px,
            close=close_px,
            volume=volume,
            open_interest=open_interest,
        )
    except ValueError as exc:
        # Re-raise as CandleRowError so callers only need to catch one type.
        raise CandleRowError(f"invalid bar content: {exc}") from exc


def parse_candle_rows(
    rows: Sequence[Sequence[Any]],
) -> list[CandleBar]:
    """Parse a list of 7-element candle rows, raising on the first error.

    The driver should call this inside a try/except so a single
    malformed row doesn't crash the whole batch — it can be routed
    to the quarantine queue instead.
    """
    return [parse_candle_row(row) for row in rows]


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _to_decimal(value: Any, field_name: str) -> Decimal:
    """Convert a raw int/float/str/Decimal to Decimal via str() to preserve precision."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value)
    raise ValueError(f"{field_name} must be numeric, got {type(value).__name__}: {value!r}")


__all__ = [
    "CandleRowError",
    "parse_candle_row",
    "parse_candle_rows",
]
