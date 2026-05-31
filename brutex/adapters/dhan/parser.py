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

#: Columns that MUST be present + index-aligned in a candle payload.
_REQUIRED_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "timestamp")

#: Columns that MAY be absent (indices: no volume; spot: no OI).
_OPTIONAL_COLUMNS: tuple[str, ...] = ("volume", "open_interest")


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


def _column(payload: Mapping[str, object], name: str, *, required: bool) -> Sequence[object] | None:
    """Return the named column as a sequence, or ``None`` if absent & optional.

    Raises :class:`CandleParseError` if a required column is missing, or if
    a present column is not a list/tuple.
    """
    if name not in payload:
        if required:
            raise CandleParseError(f"missing required candle column {name!r}")
        return None
    col = payload[name]
    if not isinstance(col, (list, tuple)):
        raise CandleParseError(f"candle column {name!r} must be an array, got {type(col).__name__}")
    return col


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
    required = {name: _column(payload, name, required=True) for name in _REQUIRED_COLUMNS}
    optional = {name: _column(payload, name, required=False) for name in _OPTIONAL_COLUMNS}

    n = len(required["timestamp"])  # type: ignore[arg-type]  # required → never None
    for name, col in {**required, **optional}.items():
        if col is not None and len(col) != n:
            raise CandleParseError(
                f"candle column {name!r} length {len(col)} != timestamp length {n}"
            )

    bars: list[CandleBar] = []
    for i in range(n):
        try:
            bar = assemble_candle(
                ts_raw=required["timestamp"][i],  # type: ignore[index]
                parse_timestamp=_parse_timestamp_epoch,
                open_=required["open"][i],  # type: ignore[index]
                high=required["high"][i],  # type: ignore[index]
                low=required["low"][i],  # type: ignore[index]
                close=required["close"][i],  # type: ignore[index]
                volume=None if optional["volume"] is None else optional["volume"][i],
                open_interest=(
                    None if optional["open_interest"] is None else optional["open_interest"][i]
                ),
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
