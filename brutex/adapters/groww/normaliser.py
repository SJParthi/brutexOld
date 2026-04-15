"""Convert raw Groww instrument dicts / CSV rows into canonical ``Instrument``.

This is the ONLY place that decodes Groww's 19/20-column format into
BruteX's broker-neutral model. Every other layer sees only
:class:`brutex.models.Instrument`.

See ``docs/groww-sdk/instruments.md §4`` for the authoritative schema and
``§12`` for the undocumented observations (the 20th column
``internal_trading_symbol``, the ``tick_size`` paise/rupees drift, and
BANKNIFTY's temporal lot-size change).
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from brutex.models import (
    Exchange,
    Instrument,
    InstrumentFormatError,
    InstrumentType,
    Segment,
    normalise_tick_size,
)

# ────────────────────────────────────────────────────────────────────────
# Scalar helpers — Groww rows come from pandas so `nan` is common
# ────────────────────────────────────────────────────────────────────────


def _is_missing(value: object) -> bool:
    """Return True if *value* is None, NaN, or an empty string.

    Groww's pandas DataFrame uses ``float('nan')`` for missing numeric
    fields and empty strings for missing text fields.
    """
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def _opt_str(value: object) -> str | None:
    if _is_missing(value):
        return None
    return str(value).strip() or None


def _required_str(value: object, field: str, row_id: str) -> str:
    s = _opt_str(value)
    if s is None:
        raise InstrumentFormatError(f"{row_id}: missing required string field {field!r}")
    return s


def _opt_int(value: object, field: str, row_id: str) -> int | None:
    if _is_missing(value):
        return None
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        raise InstrumentFormatError(f"{row_id}: field {field!r} is not an integer: {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise InstrumentFormatError(
            f"{row_id}: field {field!r} is not an integer: {value!r}"
        ) from e


def _required_int(value: object, field: str, row_id: str) -> int:
    i = _opt_int(value, field, row_id)
    if i is None:
        raise InstrumentFormatError(f"{row_id}: missing required integer field {field!r}")
    return i


def _opt_decimal(value: object, field: str, row_id: str) -> Decimal | None:
    if _is_missing(value):
        return None
    try:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, bool):  # bool is a subclass of int — reject
            raise InstrumentFormatError(f"{row_id}: field {field!r} cannot be bool: {value!r}")
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as e:
        raise InstrumentFormatError(f"{row_id}: field {field!r} is not a decimal: {value!r}") from e


def _required_date(value: object, field: str, row_id: str) -> date:
    if _is_missing(value):
        raise InstrumentFormatError(f"{row_id}: missing required date field {field!r}")
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as e:
            raise InstrumentFormatError(
                f"{row_id}: field {field!r} is not an ISO date: {value!r}"
            ) from e
    raise InstrumentFormatError(f"{row_id}: field {field!r} is not a date: {type(value).__name__}")


def _opt_date(value: object, field: str, row_id: str) -> date | None:
    if _is_missing(value):
        return None
    return _required_date(value, field, row_id)


def _truthy(value: object, field: str, row_id: str) -> bool:
    """Groww uses ``0``/``1`` (sometimes bool) for permission fields."""
    if _is_missing(value):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "y"}:
            return True
        if v in {"0", "false", "no", "n", ""}:
            return False
    raise InstrumentFormatError(f"{row_id}: field {field!r} is not a boolean-like value: {value!r}")


# ────────────────────────────────────────────────────────────────────────
# Public: one-row normaliser
# ────────────────────────────────────────────────────────────────────────


def normalise_instrument(row: dict[str, Any]) -> Instrument:
    """Convert one raw Groww row (dict) into an :class:`Instrument`.

    Parameters
    ----------
    row
        One row from ``growwapi.GrowwAPI.get_all_instruments()`` (a dict
        with the 19 documented columns plus the undocumented 20th
        ``internal_trading_symbol``) or from
        ``get_instrument_by_groww_symbol`` (same shape).

    Returns
    -------
    Instrument
        The canonical, broker-neutral record.

    Raises
    ------
    InstrumentFormatError
        If the row is missing required fields, has the wrong types,
        or violates the ``internal_trading_symbol == trading_symbol``
        invariant (see ``docs/groww-sdk/instruments.md §12.1``).
    TickSizeFormatError
        If ``tick_size`` is in an unexpected format.
    """
    # Use the groww_symbol as the row identifier for error messages
    row_id = _opt_str(row.get("groww_symbol")) or "<unknown>"

    # Exchange — constrained to NSE/BSE in v1 (MCX is out of scope)
    raw_exchange = _required_str(row.get("exchange"), "exchange", row_id)
    try:
        exchange = Exchange(raw_exchange)
    except ValueError as e:
        raise InstrumentFormatError(
            f"{row_id}: unsupported exchange {raw_exchange!r} (v1 supports NSE, BSE)"
        ) from e

    # Segment — constrained to CASH/FNO in v1 (COMMODITY is out of scope)
    raw_segment = _required_str(row.get("segment"), "segment", row_id)
    try:
        segment = Segment(raw_segment)
    except ValueError as e:
        raise InstrumentFormatError(
            f"{row_id}: unsupported segment {raw_segment!r} (v1 supports CASH, FNO)"
        ) from e

    # Instrument type — EQ / IDX / FUT / CE / PE
    raw_itype = _required_str(row.get("instrument_type"), "instrument_type", row_id)
    try:
        instrument_type = InstrumentType(raw_itype)
    except ValueError as e:
        raise InstrumentFormatError(
            f"{row_id}: unsupported instrument_type {raw_itype!r} (valid: EQ, IDX, FUT, CE, PE)"
        ) from e

    # Identity fields
    trading_symbol = _required_str(row.get("trading_symbol"), "trading_symbol", row_id)
    groww_symbol = _required_str(row.get("groww_symbol"), "groww_symbol", row_id)
    exchange_token = _required_int(row.get("exchange_token"), "exchange_token", row_id)

    # Undocumented 20th column — if present, must equal trading_symbol
    internal_trading_symbol = _opt_str(row.get("internal_trading_symbol"))
    if internal_trading_symbol is not None and internal_trading_symbol != trading_symbol:
        raise InstrumentFormatError(
            f"{row_id}: internal_trading_symbol {internal_trading_symbol!r} "
            f"does not equal trading_symbol {trading_symbol!r} — "
            f"see docs/groww-sdk/instruments.md §12.1"
        )

    # Economics
    lot_size = _required_int(row.get("lot_size"), "lot_size", row_id)
    tick_size = normalise_tick_size(row.get("tick_size"))  # type: ignore[arg-type]
    freeze_qty = _opt_int(row.get("freeze_quantity"), "freeze_quantity", row_id)

    # Derivatives fields
    expiry_date: date | None
    if instrument_type in {InstrumentType.FUT, InstrumentType.CE, InstrumentType.PE}:
        expiry_date = _required_date(row.get("expiry_date"), "expiry_date", row_id)
    else:
        expiry_date = _opt_date(row.get("expiry_date"), "expiry_date", row_id)

    strike_price = _opt_decimal(row.get("strike_price"), "strike_price", row_id)
    if instrument_type in {InstrumentType.CE, InstrumentType.PE} and strike_price is None:
        raise InstrumentFormatError(f"{row_id}: option missing strike_price")

    underlying_symbol = _opt_str(row.get("underlying_symbol"))
    underlying_token = _opt_int(
        row.get("underlying_exchange_token"), "underlying_exchange_token", row_id
    )

    return Instrument(
        exchange=exchange,
        exchange_token=exchange_token,
        trading_symbol=trading_symbol,
        groww_symbol=groww_symbol,
        name=_opt_str(row.get("name")),
        instrument_type=instrument_type,
        segment=segment,
        series=_opt_str(row.get("series")),
        isin=_opt_str(row.get("isin")),
        underlying_symbol=underlying_symbol,
        underlying_exchange_token=underlying_token,
        lot_size=lot_size,
        tick_size=tick_size,
        freeze_quantity=freeze_qty,
        expiry_date=expiry_date,
        strike_price=strike_price,
        is_reserved=_truthy(row.get("is_reserved"), "is_reserved", row_id),
        buy_allowed=_truthy(row.get("buy_allowed"), "buy_allowed", row_id),
        sell_allowed=_truthy(row.get("sell_allowed"), "sell_allowed", row_id),
        internal_trading_symbol=internal_trading_symbol,
    )


__all__ = ["normalise_instrument"]
