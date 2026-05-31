"""Dhan rolling-option parser — per-side columnar payload → ``OptionBar`` rows.

``/charts/rollingoption`` (``docs/dhan-ref/06-expired-options-data.md``)
returns expired-option OHLC as **per-side** columnar parallel arrays::

    { "ce": { "open":[…], "high":[…], "low":[…], "close":[…],
              "volume":[…], "timestamp":[…],
              "oi":[…], "strike":[…], "spot":[…], "iv":[…] },
      "pe": null }

This module dispatches the ``ce`` / ``pe`` sides (a ``null`` side means no
data for that leg/window — skipped, never an error), then transposes each
side's columns → :class:`~brutex.data.models.OptionBar` rows. The OHLCV
fields reuse the **COMMON** :func:`~brutex.data.candle_fields.assemble_candle`
(CLAUDE.md §11 — field rules live once); the option-specific extras
(``strike`` / ``spot`` / ``iv``) are parsed here and stamped per bar so the
lake loses nothing (operator decree 2026-05-31).

Two shape differences from the spot parser
(:mod:`brutex.adapters.dhan.parser`):

* open interest arrives as ``oi`` (not ``open_interest``), and
* the per-bar ``strike`` / ``spot`` / ``iv`` arrays are option-only.

No-trade bars (all OHLC absent/zero) are skipped gracefully, so a side may
yield fewer rows than ``len(timestamp)`` rather than raising
(DEC-CANDLE-EMPTY-BAR-001).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from brutex.adapters.dhan.parser import parse_timestamp_epoch
from brutex.data.candle_fields import (
    CandleParseError,
    EmptyCandleRow,
    assemble_candle,
    is_absent,
    to_price,
)
from brutex.data.models import OptionBar

#: Rolling-option response key → neutral :class:`OptionBar` side label.
_SIDES: tuple[tuple[str, str], ...] = (("ce", "CE"), ("pe", "PE"))


def _as_array(side: Mapping[str, object], name: str) -> Sequence[object]:
    """Return column *name* as a sequence, or raise :class:`CandleParseError`."""
    col = side[name]
    if not isinstance(col, (list, tuple)):
        raise CandleParseError(
            f"rollingoption column {name!r} must be an array, got {type(col).__name__}"
        )
    return col


def _require(side: Mapping[str, object], name: str) -> Sequence[object]:
    """Return a required column as a sequence; raise if missing or non-array."""
    if name not in side:
        raise CandleParseError(f"missing required rollingoption column {name!r}")
    return _as_array(side, name)


def _optional(side: Mapping[str, object], name: str) -> Sequence[object] | None:
    """Return an optional column as a sequence, or ``None`` if absent."""
    if name not in side:
        return None
    return _as_array(side, name)


def _opt_price(col: Sequence[object] | None, index: int, field: str) -> Decimal | None:
    """Parse an optional per-bar price (``strike`` / ``spot`` / ``iv``)."""
    if col is None:
        return None
    raw = col[index]
    return None if is_absent(raw) else to_price(raw, field)


def _parse_side(side: Mapping[str, object], label: str) -> list[OptionBar]:
    """Transpose one ``ce`` / ``pe`` side's columnar arrays into OptionBars."""
    timestamps = _require(side, "timestamp")
    opens = _require(side, "open")
    highs = _require(side, "high")
    lows = _require(side, "low")
    closes = _require(side, "close")
    volumes = _optional(side, "volume")
    open_interests = _optional(side, "oi")
    strikes = _optional(side, "strike")
    spots = _optional(side, "spot")
    ivs = _optional(side, "iv")

    n = len(timestamps)
    columns: tuple[tuple[str, Sequence[object] | None], ...] = (
        ("open", opens),
        ("high", highs),
        ("low", lows),
        ("close", closes),
        ("volume", volumes),
        ("oi", open_interests),
        ("strike", strikes),
        ("spot", spots),
        ("iv", ivs),
    )
    for name, col in columns:
        if col is not None and len(col) != n:
            raise CandleParseError(
                f"rollingoption column {name!r} length {len(col)} != timestamp length {n}"
            )

    bars: list[OptionBar] = []
    for i in range(n):
        try:
            candle = assemble_candle(
                ts_raw=timestamps[i],
                parse_timestamp=parse_timestamp_epoch,
                open_=opens[i],
                high=highs[i],
                low=lows[i],
                close=closes[i],
                volume=None if volumes is None else volumes[i],
                open_interest=None if open_interests is None else open_interests[i],
            )
        except EmptyCandleRow:
            continue  # graceful skip — no-trade minute
        bars.append(
            OptionBar(
                timestamp=candle.timestamp,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                open_interest=candle.open_interest,
                side=label,
                strike=_opt_price(strikes, i, "strike"),
                spot=_opt_price(spots, i, "spot"),
                iv=_opt_price(ivs, i, "iv"),
            )
        )
    return bars


def parse_rolling_option_sides(payload: Mapping[str, object]) -> list[OptionBar]:
    """Parse a ``/charts/rollingoption`` ``data`` object into OptionBars.

    Parameters
    ----------
    payload
        The ``data`` mapping with optional ``ce`` / ``pe`` side objects. A
        ``null`` side (``"pe": null``) is skipped — no data for that leg.

    Returns
    -------
    list[OptionBar]
        CE rows first, then PE rows, each in input order. No-trade bars are
        skipped, so the result may be shorter than the input arrays.

    Raises
    ------
    CandleParseError
        On a non-mapping side, a missing/non-array required column, a length
        mismatch, or any unparseable (non-empty) field.
    """
    bars: list[OptionBar] = []
    for key, label in _SIDES:
        side = payload.get(key)
        if side is None:
            continue  # null / absent side — no data for this leg
        if not isinstance(side, Mapping):
            raise CandleParseError(
                f"rollingoption side {key!r} must be an object, got {type(side).__name__}"
            )
        bars.extend(_parse_side(side, label))
    return bars


__all__ = [
    "parse_rolling_option_sides",
]
