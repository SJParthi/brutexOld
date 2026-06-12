"""Parse Groww historical candle response → list[CandleBar].

Response shape (verified from docs + SDK source, 2026-06-12):

.. code-block:: json

    {
      "candles": [
          ["2025-09-24T10:30:00", 245.95, 246.15, 245.05, 245.60, 735060, null],
          ...
      ],
      "closing_price": 245.40,
      "start_time": "2025-09-24 10:30:00",
      "end_time": "2025-09-24 12:00:00",
      "interval_in_minutes": 30
    }

Each candle row: ``[timestamp, open, high, low, close, volume, open_interest]``

**Timestamp handling:** Groww returns naive IST strings (no timezone info).
The parser converts to UTC-aware datetimes using ``zoneinfo.ZoneInfo``.

**Price handling:** Groww prices are rupee floats. The parser converts to
``Decimal`` via ``str()`` to avoid float precision drift (DEC-MONEY-2DP-001;
lake stores Parquet Float64, parser delivers Decimal for CandleBar contract).

**Volume:** Groww returns ``null`` volume for pure index bars; normalised to
``0`` (CLAUDE.md §6 DEC-VENDOR-VOLUME-NORMALIZE-001 equivalent for Groww).
"""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from brutex.data.models import CandleBar

#: Groww timestamps are in IST with no timezone marker.
_IST: ZoneInfo = ZoneInfo("Asia/Kolkata")

#: All stored timestamps are UTC-aware.
_UTC: ZoneInfo = ZoneInfo("UTC")

#: Candle row column indices (0-based).
_IDX_TIMESTAMP: int = 0
_IDX_OPEN: int = 1
_IDX_HIGH: int = 2
_IDX_LOW: int = 3
_IDX_CLOSE: int = 4
_IDX_VOLUME: int = 5
_IDX_OI: int = 6

#: Minimum number of columns expected per candle row.
_MIN_COLUMNS: int = 6


def _to_decimal(value: Any) -> Decimal:
    """Convert a float/int/str price value → Decimal without precision drift."""
    return Decimal(str(value))


def _to_utc(ts_str: str) -> datetime:
    """Parse an IST naive timestamp string → UTC-aware datetime.

    Groww returns ``"2025-09-24T10:30:00"`` (ISO 8601 without tz suffix).
    We attach IST and convert to UTC so the lake always stores UTC.
    """
    naive = datetime.fromisoformat(ts_str)
    ist_aware = naive.replace(tzinfo=_IST)
    return ist_aware.astimezone(_UTC)


def parse_candle_rows(
    candles: list[Any],
    *,
    drop_volume: bool = False,
) -> list[CandleBar]:
    """Parse the ``candles`` array from a Groww historical response.

    Parameters
    ----------
    candles:
        The raw list from ``response["candles"]``. Each element is a list
        with at least 6 values: ``[timestamp, open, high, low, close, volume]``
        and an optional 7th ``open_interest`` (``null`` for non-FNO).
    drop_volume:
        If ``True``, the volume field is forced to ``0`` regardless of the
        vendor value. Set this for index instruments (NIFTY/BANKNIFTY/SENSEX/
        INDIA VIX) where Groww may return junk or null volume
        (same behaviour as Dhan's ``drop_volume`` flag).

    Returns
    -------
    list[CandleBar]
        One :class:`~brutex.data.models.CandleBar` per row, timestamps in UTC.

    Raises
    ------
    ValueError
        If a candle row has fewer than 6 columns or the timestamp cannot be
        parsed. This indicates a vendor format change that needs investigation.
    """
    result: list[CandleBar] = []
    for idx, row in enumerate(candles):
        if len(row) < _MIN_COLUMNS:
            raise ValueError(
                f"candle row {idx} has {len(row)} columns (expected ≥ {_MIN_COLUMNS}): {row!r}"
            )
        ts_raw = row[_IDX_TIMESTAMP]
        if not isinstance(ts_raw, str):
            raise ValueError(
                f"candle row {idx} timestamp is not a string: {ts_raw!r}"
            )
        ts_utc = _to_utc(ts_raw)
        open_ = _to_decimal(row[_IDX_OPEN])
        high = _to_decimal(row[_IDX_HIGH])
        low = _to_decimal(row[_IDX_LOW])
        close = _to_decimal(row[_IDX_CLOSE])
        raw_vol = row[_IDX_VOLUME]
        volume = 0 if drop_volume or raw_vol is None else int(raw_vol)
        oi_raw = row[_IDX_OI] if len(row) > _IDX_OI else None
        open_interest = None if oi_raw is None else int(oi_raw)
        result.append(
            CandleBar(
                timestamp=ts_utc,
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=volume,
                open_interest=open_interest,
            )
        )
    return result


__all__ = ["parse_candle_rows"]
