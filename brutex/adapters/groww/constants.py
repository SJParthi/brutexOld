"""Groww SDK constant → BruteX enum mappings.

The adapter is the ONLY place in the codebase that imports ``growwapi``.
Every other layer uses BruteX's internal enums from :mod:`brutex.models`.

These mapping tables wire the two together. The golden-master test
``tests/golden/test_groww_sdk_constants_frozen.py`` asserts that the raw
string values on the right-hand side still match the live SDK — if
Groww ever renames a constant upstream, CI fails loudly and we update
this file deliberately.

See ``docs/groww-sdk/annexures.md`` for the authoritative constant list.
"""

from __future__ import annotations

from typing import Final

from brutex.models import Exchange, InstrumentType, Segment

# ────────────────────────────────────────────────────────────────────────
# String-value tables — do NOT import growwapi here (would create a
# hard runtime dependency for unit tests that don't need the SDK).
# The golden-master test introspects the live SDK and asserts equality.
# ────────────────────────────────────────────────────────────────────────

#: Raw string values of ``GrowwAPI.EXCHANGE_*`` constants (verified 2026-04-14).
GROWW_EXCHANGE_VALUES: Final[dict[Exchange, str]] = {
    Exchange.NSE: "NSE",
    Exchange.BSE: "BSE",
}

#: Raw string values of ``GrowwAPI.SEGMENT_*`` constants (verified 2026-04-14).
GROWW_SEGMENT_VALUES: Final[dict[Segment, str]] = {
    Segment.CASH: "CASH",
    Segment.FNO: "FNO",
}

#: Raw string values of ``GrowwAPI.CANDLE_INTERVAL_*`` constants (verified 2026-04-14).
#:
#: All 12 native intervals per ``docs/groww-sdk/annexures.md §4``. Note:
#: ``DAY``/``WEEK``/``MONTH`` do **not** have a ``_1`` suffix — the bare
#: name is canonical (see ``docs/groww-sdk/backtesting.md §9``).
GROWW_CANDLE_INTERVAL_VALUES: Final[dict[str, str]] = {
    "MIN_1": "1minute",
    "MIN_2": "2minute",
    "MIN_3": "3minute",
    "MIN_5": "5minute",
    "MIN_10": "10minute",
    "MIN_15": "15minute",
    "MIN_30": "30minute",
    "HOUR_1": "1hour",
    "HOUR_4": "4hour",
    "DAY": "1day",
    "WEEK": "1week",
    "MONTH": "1month",
}

#: SDK attribute names on ``growwapi.GrowwAPI`` for the candle-interval constants.
#: Used by the golden-master test to verify ``getattr(GrowwAPI, name)`` exists
#: and equals the expected string value.
GROWW_CANDLE_INTERVAL_ATTR_NAMES: Final[tuple[str, ...]] = (
    "CANDLE_INTERVAL_MIN_1",
    "CANDLE_INTERVAL_MIN_2",
    "CANDLE_INTERVAL_MIN_3",
    "CANDLE_INTERVAL_MIN_5",
    "CANDLE_INTERVAL_MIN_10",
    "CANDLE_INTERVAL_MIN_15",
    "CANDLE_INTERVAL_MIN_30",
    "CANDLE_INTERVAL_HOUR_1",
    "CANDLE_INTERVAL_HOUR_4",
    "CANDLE_INTERVAL_DAY",
    "CANDLE_INTERVAL_WEEK",
    "CANDLE_INTERVAL_MONTH",
)

# ────────────────────────────────────────────────────────────────────────
# Max duration per request (days) — see docs/groww-sdk/backtesting.md §6
# ────────────────────────────────────────────────────────────────────────

#: Max time-span in days per single ``get_historical_candles`` call,
#: keyed by the short interval code (same keys as
#: :data:`GROWW_CANDLE_INTERVAL_VALUES`).
GROWW_MAX_DURATION_DAYS: Final[dict[str, int]] = {
    "MIN_1": 30,
    "MIN_2": 30,
    "MIN_3": 30,
    "MIN_5": 30,
    "MIN_10": 90,
    "MIN_15": 90,
    "MIN_30": 90,
    "HOUR_1": 180,
    "HOUR_4": 180,
    "DAY": 180,
    "WEEK": 180,
    "MONTH": 180,
}

# ────────────────────────────────────────────────────────────────────────
# Instrument type — Groww CSV strings → BruteX enum
# See docs/groww-sdk/annexures.md §5
# ────────────────────────────────────────────────────────────────────────

GROWW_INSTRUMENT_TYPE_VALUES: Final[dict[InstrumentType, str]] = {
    InstrumentType.EQ: "EQ",
    InstrumentType.IDX: "IDX",
    InstrumentType.FUT: "FUT",
    InstrumentType.CE: "CE",
    InstrumentType.PE: "PE",
}

#: Reverse lookup used by the normaliser.
GROWW_INSTRUMENT_TYPE_BY_RAW: Final[dict[str, InstrumentType]] = {
    v: k for k, v in GROWW_INSTRUMENT_TYPE_VALUES.items()
}


#: Exception class names we need to import from ``growwapi.groww.exceptions``.
#: The retry wrapper (to be written in P1c) imports these; the golden-master
#: test asserts they exist on the upstream module.
GROWW_EXCEPTION_CLASS_NAMES: Final[tuple[str, ...]] = (
    "GrowwBaseException",
    "GrowwAPIException",
    "GrowwAPIAuthenticationException",
    "GrowwAPIAuthorisationException",  # British spelling — verified
    "GrowwAPIBadRequestException",
    "GrowwAPINotFoundException",
    "GrowwAPIRateLimitException",
    "GrowwAPITimeoutException",
)


__all__ = [
    "GROWW_CANDLE_INTERVAL_ATTR_NAMES",
    "GROWW_CANDLE_INTERVAL_VALUES",
    "GROWW_EXCEPTION_CLASS_NAMES",
    "GROWW_EXCHANGE_VALUES",
    "GROWW_INSTRUMENT_TYPE_BY_RAW",
    "GROWW_INSTRUMENT_TYPE_VALUES",
    "GROWW_MAX_DURATION_DAYS",
    "GROWW_SEGMENT_VALUES",
]
