"""Dhan candle parser — parallel COLUMN arrays payload → :class:`CandleBar` rows.

Dhan's ``/charts/intraday`` returns OHLCV as parallel COLUMN arrays (VERIFIED-live
shape; the SDK does ZERO reshaping/validation)::

    {"open": [...], "high": [...], "low": [...], "close": [...],
     "volume": [...], "timestamp": [...]}

This module extracts the columns and hands each index-aligned row to the shared
:func:`~brutex.data.candle_fields.assemble_candle` (CLAUDE.md §11 COMMON — the
field-level rules live once, snapping / OHLC-clamp / empty-skip identical to
Groww). The only Dhan-specific glue is:

* **Column arrays, not rows.** The lengths of ``open/high/low/close/volume/timestamp``
  are ASSERTED equal before zipping; a mismatch means the vendor payload is
  internally inconsistent (SDK does no validation) — a
  :class:`~brutex.adapters.dhan.errors.DhanVendorDriftError` that REJECTS the chunk
  (never partially stored) and re-pulls (Golden Rule #1: never silently mis-pair a
  price with a wrong timestamp).
* **Unix-EPOCH-seconds UTC timestamps.** ``timestamp[i]`` is Unix epoch seconds
  (UTC POSIX); stored UTC via ``datetime.fromtimestamp(epoch, tz=UTC)`` — which is
  TIMEZONE-INVARIANT of the host clock (a naive ``fromtimestamp`` would read the
  box's local zone). Rendered IST by a FIXED +05:30 downstream (09:15 IST ⇔ 03:45
  UTC, no DST).
* **Index spot volume.** An index carries no traded volume; the adapter passes
  ``drop_volume=True`` so every bar's volume → 0 (an inherent market fact, never a
  gap). Prices are snapped to the 2-dp tick grid at the shared lake-write boundary
  (not here — byte-identical to Groww).

An EMPTY payload (no columns, or all-empty arrays — a holiday / no-session / too-old
span) is honest NO-DATA → ``[]`` (never an error, never a fabricated bar).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime

from brutex.adapters.dhan.errors import DhanVendorDriftError
from brutex.data.candle_fields import (
    CandleParseError,
    EmptyCandleRow,
    assemble_candle,
)
from brutex.data.models import CandleBar

#: The four required OHLC price columns.
_OHLC_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")

#: Every column the parser reads (OHLC + volume + timestamp). ``open_interest`` is
#: never present for an index intraday pull (``oi=false``) — bars carry OI ``None``.
_ALL_COLUMNS: tuple[str, ...] = (*_OHLC_COLUMNS, "volume", "timestamp")


def parse_epoch_utc(raw: object) -> datetime:
    """Parse a Dhan candle timestamp (Unix epoch seconds, UTC) to a tz-aware UTC datetime.

    ``datetime.fromtimestamp(epoch, tz=UTC)`` is TIMEZONE-INVARIANT of the host clock
    (the #1 apples-to-apples footgun the research names — a naive ``fromtimestamp``
    reads the box's local zone). Rejects bools, non-finite, and out-of-range values
    (raises :class:`~brutex.data.candle_fields.CandleParseError`, a
    :class:`~brutex.models.BruteXError` so ONE bad row quarantines the window instead
    of crashing the pull).
    """
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        raise CandleParseError(f"Dhan candle timestamp must not be bool: {raw!r}")
    if isinstance(raw, (int, float)):
        epoch = float(raw)
        if not math.isfinite(epoch):
            raise CandleParseError(f"non-finite Dhan candle timestamp: {raw!r}")
        try:
            return datetime.fromtimestamp(epoch, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise CandleParseError(f"out-of-range Dhan candle timestamp: {raw!r}") from exc
    raise CandleParseError(
        f"Dhan candle timestamp must be epoch seconds, got {type(raw).__name__}: {raw!r}"
    )


def parse_candle_columns(
    payload: Mapping[str, object], *, drop_volume: bool = False
) -> list[CandleBar]:
    """Parse a Dhan parallel-column candle payload into validated rows.

    Parameters
    ----------
    payload
        The ``data`` sub-object of a Dhan intraday success response — a mapping of
        column arrays ``{"open", "high", "low", "close", "volume", "timestamp"}``.
    drop_volume
        When ``True`` the per-row volume is ignored (every bar's volume → 0). An
        index (NIFTY / BANKNIFTY / SENSEX / INDIA VIX) carries no traded volume; the
        adapter passes ``drop_volume=True``. Under ``drop_volume=True`` the
        ``volume`` column is additionally OPTIONAL (design §5): what the vendor
        returns in an IDX_I ``volume`` array is UNDOCUMENTED (probe gate G-J —
        zeros? nulls? absent?) and the values are never read on this path, so an
        ABSENT volume array must not wedge every index window as vendor drift.
        When PRESENT it still rides the length check; every other missing column
        stays :class:`DhanVendorDriftError`.

    Returns
    -------
    list[CandleBar]
        One row per non-empty candle, in input order. No-trade rows are skipped; an
        empty payload returns ``[]`` (honest no-data).

    Raises
    ------
    DhanVendorDriftError
        A PARTIAL payload (some required columns present, some absent) or column
        arrays of UNEQUAL length — the vendor payload is internally inconsistent, so
        the chunk is rejected + re-pulled rather than silently mis-paired.
    CandleParseError
        A column is not an array, or a non-empty field is unparseable.
    """
    present = {name: payload[name] for name in _ALL_COLUMNS if name in payload}
    if not present:
        return []  # no columns at all — a holiday / no-session / too-old span (honest no-data)
    # Under drop_volume the volume column is OPTIONAL (its values are never read
    # and the IDX_I volume content is UNVERIFIED, gate G-J); when present it still
    # rides the array/length checks below. All other columns stay REQUIRED.
    required = (
        tuple(name for name in _ALL_COLUMNS if name != "volume") if drop_volume else _ALL_COLUMNS
    )
    missing = [name for name in required if name not in present]
    if missing:
        raise DhanVendorDriftError(
            f"Dhan payload missing required column(s) {missing} (partial vendor payload)"
        )
    for name, column in present.items():
        if not isinstance(column, (list, tuple)):
            raise CandleParseError(
                f"Dhan column {name!r} must be an array, got {type(column).__name__}"
            )
    columns: dict[str, list[object] | tuple[object, ...]] = {
        name: present[name]  # type: ignore[misc]  # narrowed to list/tuple above
        for name in present
    }
    lengths = {name: len(col) for name, col in columns.items()}
    if len(set(lengths.values())) > 1:
        raise DhanVendorDriftError(
            f"Dhan column arrays have unequal lengths {lengths} — vendor drift, chunk rejected"
        )
    count = next(iter(lengths.values()))
    if count == 0:
        return []  # all-empty arrays — honest no-data
    bars: list[CandleBar] = []
    for index in range(count):
        try:
            bar = assemble_candle(
                ts_raw=columns["timestamp"][index],
                parse_timestamp=parse_epoch_utc,
                open_=columns["open"][index],
                high=columns["high"][index],
                low=columns["low"][index],
                close=columns["close"][index],
                volume=None if drop_volume else columns["volume"][index],
                open_interest=None,
            )
        except EmptyCandleRow:
            continue  # graceful skip — no-trade minute
        bars.append(bar)
    return bars


__all__ = [
    "CandleParseError",
    "DhanVendorDriftError",
    "EmptyCandleRow",
    "parse_candle_columns",
    "parse_epoch_utc",
]
