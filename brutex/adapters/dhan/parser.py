"""Dhan candle parser — columnar parallel arrays → :class:`CandleBar` rows.

Dhan's ``/charts/intraday``, ``/charts/historical`` and (per side)
``/charts/rollingoption`` return OHLCV as **columnar parallel arrays**
(``docs/dhan-ref/05-historical-data.md``)::

    { "open":[…], "high":[…], "low":[…], "close":[…],
      "volume":[…], "timestamp":[…], "open_interest":[…] }

with all arrays index-aligned (``open[i] … timestamp[i]`` is one candle).
This is the inverse of Groww's row-of-arrays, so this module **transposes**
columns → rows and hands each to the shared
:func:`~brutex.data.candle_fields.assemble_candle` (CLAUDE.md §11 COMMON —
field rules live once). The only Dhan-specific glue is the **UNIX-epoch
(UTC seconds)** timestamp rule (``docs/dhan-ref/05``).

No-trade bars (all OHLC absent/zero) are skipped gracefully — a Dhan
response for an illiquid minute, or a null option side, yields fewer rows
than ``len(timestamp)`` rather than raising (DEC-CANDLE-EMPTY-BAR-001).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from brutex.data.candle_fields import (
    CandleParseError,
    EmptyCandleRow,
    assemble_candle,
)
from brutex.data.models import CandleBar


def _parse_timestamp_epoch(raw: object) -> datetime:
    """Parse a Dhan UNIX-epoch timestamp (UTC seconds) to a tz-aware UTC datetime."""
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        raise CandleParseError(f"candle timestamp must be epoch seconds, got bool: {raw!r}")
    if not isinstance(raw, (int, float, str)):
        raise CandleParseError(
            f"candle timestamp must be epoch seconds, got {type(raw).__name__}: {raw!r}"
        )
    try:
        epoch = float(raw)
    except (ValueError, OverflowError) as exc:
        raise CandleParseError(f"unparseable candle timestamp: {raw!r}") from exc
    if not math.isfinite(epoch):
        raise CandleParseError(f"non-finite candle timestamp: {raw!r}")
    try:
        return datetime.fromtimestamp(epoch, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise CandleParseError(f"out-of-range candle timestamp: {raw!r}") from exc


def _as_array(payload: Mapping[str, object], name: str) -> Sequence[object]:
    """Return present column ``name`` as a sequence, else raise CandleParseError."""
    col = payload[name]
    if not isinstance(col, (list, tuple)):
        raise CandleParseError(f"candle column {name!r} must be an array, got {type(col).__name__}")
    return col


def _require_column(payload: Mapping[str, object], name: str) -> Sequence[object]:
    """Return a required column as a sequence; raise if missing or non-array."""
    if name not in payload:
        raise CandleParseError(f"missing required candle column {name!r}")
    return _as_array(payload, name)


def _optional_column(payload: Mapping[str, object], name: str) -> Sequence[object] | None:
    """Return an optional column as a sequence, or ``None`` if absent."""
    if name not in payload:
        return None
    return _as_array(payload, name)


def parse_candle_columns(payload: Mapping[str, object]) -> list[CandleBar]:
    """Transpose a Dhan columnar candle payload into validated rows.

    Parameters
    ----------
    payload
        A mapping with the ``open/high/low/close/timestamp`` arrays (and
        optionally ``volume`` / ``open_interest``). For ``rollingoption``
        pass one side's object (``data["ce"]`` or ``data["pe"]``).

    Returns
    -------
    list[CandleBar]
        One row per non-empty candle, in input order. No-trade bars are
        skipped, so the result may be shorter than ``len(timestamp)``.

    Raises
    ------
    CandleParseError
        On a missing/non-array required column, a length mismatch, or any
        unparseable (non-empty) field.
    """
    timestamps = _require_column(payload, "timestamp")
    opens = _require_column(payload, "open")
    highs = _require_column(payload, "high")
    lows = _require_column(payload, "low")
    closes = _require_column(payload, "close")
    volumes = _optional_column(payload, "volume")
    open_interests = _optional_column(payload, "open_interest")

    n = len(timestamps)
    columns: tuple[tuple[str, Sequence[object] | None], ...] = (
        ("open", opens),
        ("high", highs),
        ("low", lows),
        ("close", closes),
        ("volume", volumes),
        ("open_interest", open_interests),
    )
    for name, col in columns:
        if col is not None and len(col) != n:
            raise CandleParseError(
                f"candle column {name!r} length {len(col)} != timestamp length {n}"
            )

    bars: list[CandleBar] = []
    for i in range(n):
        try:
            bar = assemble_candle(
                ts_raw=timestamps[i],
                parse_timestamp=_parse_timestamp_epoch,
                open_=opens[i],
                high=highs[i],
                low=lows[i],
                close=closes[i],
                volume=None if volumes is None else volumes[i],
                open_interest=None if open_interests is None else open_interests[i],
            )
        except EmptyCandleRow:
            continue  # graceful skip — no-trade / null-side minute
        bars.append(bar)
    return bars


__all__ = [
    "CandleParseError",
    "EmptyCandleRow",
    "parse_candle_columns",
]
