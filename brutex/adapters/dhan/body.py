"""Dhan request-body builders — pure JSON payload construction.

One place that turns BruteX call parameters into the exact request bodies
Dhan's Data APIs expect (``docs/dhan-ref/05`` spot, ``06`` options). These
are **pure functions** (no I/O, no state) so they are unit-testable to
100% and reusable by the client + the adapter without a network.

Endpoint date formats (locked here, once — CLAUDE.md §11 COMMON):

* intraday — ``"YYYY-MM-DD HH:MM:SS"``
* historical (daily) — ``"YYYY-MM-DD"``
* rolling option — ``"YYYY-MM-DD"`` (toDate non-inclusive)

Every enum argument is validated against the verified constant sets so a
typo (wrong ``exchangeSegment`` / ``instrument`` / ``interval``) raises at
build time instead of round-tripping a silent vendor 400.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from brutex.adapters.dhan.constants import (
    DRV_OPTION_TYPES,
    EXCHANGE_SEGMENTS,
    EXPIRY_FLAGS,
    INSTRUMENT_TYPES,
    SUPPORTED_INTRADAY_INTERVALS,
    TRACK1_OPTIONS_INTERVAL,
    TRACK1_SPOT_INTRADAY_INTERVAL,
)
from brutex.models import BruteXError

_INTRADAY_FMT = "%Y-%m-%d %H:%M:%S"
_DAILY_FMT = "%Y-%m-%d"

#: Allowed values for the rolling-option ``requiredData`` array.
_REQUIRED_DATA_FIELDS: frozenset[str] = frozenset(
    {"open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"}
)


class DhanBodyError(BruteXError):
    """A request-body argument is outside the verified Dhan vocabulary."""


def _validate(value: str, allowed: frozenset[str] | tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise DhanBodyError(f"invalid {field} {value!r}; allowed: {sorted(allowed)}")
    return value


def build_intraday_body(
    *,
    security_id: int | str,
    exchange_segment: str,
    instrument: str,
    from_date: datetime,
    to_date: datetime,
    interval: str = TRACK1_SPOT_INTRADAY_INTERVAL,
) -> dict[str, object]:
    """Build the ``/charts/intraday`` request body (SPOT underlying only)."""
    return {
        "securityId": str(security_id),
        "exchangeSegment": _validate(exchange_segment, EXCHANGE_SEGMENTS, "exchangeSegment"),
        "instrument": _validate(instrument, INSTRUMENT_TYPES, "instrument"),
        "interval": _validate(interval, SUPPORTED_INTRADAY_INTERVALS, "interval"),
        "fromDate": from_date.strftime(_INTRADAY_FMT),
        "toDate": to_date.strftime(_INTRADAY_FMT),
    }


def build_historical_body(
    *,
    security_id: int | str,
    exchange_segment: str,
    instrument: str,
    from_date: datetime,
    to_date: datetime,
) -> dict[str, object]:
    """Build the ``/charts/historical`` (daily) request body (SPOT only)."""
    return {
        "securityId": str(security_id),
        "exchangeSegment": _validate(exchange_segment, EXCHANGE_SEGMENTS, "exchangeSegment"),
        "instrument": _validate(instrument, INSTRUMENT_TYPES, "instrument"),
        "fromDate": from_date.strftime(_DAILY_FMT),
        "toDate": to_date.strftime(_DAILY_FMT),
    }


def build_rolling_option_body(
    *,
    security_id: int | str,
    exchange_segment: str,
    instrument: str,
    expiry_flag: str,
    expiry_code: int,
    strike: str,
    drv_option_type: str,
    required_data: Sequence[str],
    from_date: datetime,
    to_date: datetime,
    interval: str = TRACK1_OPTIONS_INTERVAL,
) -> dict[str, object]:
    """Build the ``/charts/rollingoption`` request body (ALL option OHLC).

    ``required_data`` must be a non-empty subset of the documented fields
    (``open high low close iv volume strike oi spot``).
    """
    if not required_data:
        raise DhanBodyError("required_data must be a non-empty array")
    bad = [f for f in required_data if f not in _REQUIRED_DATA_FIELDS]
    if bad:
        raise DhanBodyError(
            f"invalid required_data {bad}; allowed: {sorted(_REQUIRED_DATA_FIELDS)}"
        )
    return {
        "securityId": str(security_id),
        "exchangeSegment": _validate(exchange_segment, EXCHANGE_SEGMENTS, "exchangeSegment"),
        "instrument": _validate(instrument, INSTRUMENT_TYPES, "instrument"),
        "interval": _validate(interval, SUPPORTED_INTRADAY_INTERVALS, "interval"),
        "expiryFlag": _validate(expiry_flag, EXPIRY_FLAGS, "expiryFlag"),
        "expiryCode": expiry_code,
        "strike": strike,
        "drvOptionType": _validate(drv_option_type, DRV_OPTION_TYPES, "drvOptionType"),
        "requiredData": list(required_data),
        "fromDate": from_date.strftime(_DAILY_FMT),
        "toDate": to_date.strftime(_DAILY_FMT),
    }


__all__ = [
    "DhanBodyError",
    "build_historical_body",
    "build_intraday_body",
    "build_rolling_option_body",
]
