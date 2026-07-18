"""Dhan expired-options orchestration — rolling-option fetch (injected client) → parse.

DEC-DHAN-ADAPTER-001. Thin fetch→parse glue over the injected
:class:`~brutex.adapters.dhan.client.DhanClientProtocol` for the ATM-relative
rolling-option (expired) endpoint. Expired options use MACRO addressing — a strike
selector ``"ATM"`` / ``"ATM+1"`` / ``"ATM-1"`` (never a resolved dated-contract
security_id) plus an ``expiryFlag`` / ``expiryCode`` — so this module builds that
request shape and delegates the pure parse to
:func:`brutex.adapters.dhan.parser.parse_rolling_option`.

**Honest scope (Golden Rule #10):** expired FUTURES history is ABSENT/undocumented on
Dhan (``_UNVERIFIED``-absent — no fetch path is provided, deliberately). Coverage is
Index + Stock OPTIONS only, last ~5 years, minute-level, up to 10 strikes above/below
ATM, ≤ 30 days per call. **LAKE / reference-only + GATED ON the Data-API
subscription** — nothing production wires a live client into it.
"""

from __future__ import annotations

from collections.abc import Sequence

from brutex.adapters.dhan.client import DhanClientProtocol
from brutex.adapters.dhan.parser import parse_rolling_option
from brutex.data.models import OptionBar

#: Fields requested from the rolling-option endpoint (``requiredData``). VERIFIED —
#: DhanHQ v2 docs (open/high/low/close/volume). ``iv`` / ``oi`` / ``strike`` / ``spot``
#: are stamped per bar by the endpoint regardless.
DEFAULT_REQUIRED_DATA: tuple[str, ...] = ("open", "high", "low", "close", "volume")

#: Option-instrument tokens the rolling-option endpoint accepts. VERIFIED — DhanHQ v2
#: docs (``OPTIDX`` for index options, ``OPTSTK`` for stock options).
ROLLING_OPTION_INSTRUMENTS: frozenset[str] = frozenset({"OPTIDX", "OPTSTK"})


def fetch_rolling_option(
    client: DhanClientProtocol,
    *,
    exchange_segment: str,
    security_id: str,
    instrument: str,
    expiry_flag: str,
    expiry_code: int,
    strike: str,
    drv_option_type: str,
    interval: str,
    from_date: str,
    to_date: str,
    required_data: Sequence[str] = DEFAULT_REQUIRED_DATA,
) -> list[OptionBar]:
    """Fetch + parse one ATM-relative rolling-option window into :class:`OptionBar` rows.

    Parameters mirror the endpoint body: *security_id* is the UNDERLYING's id (not a
    dated contract), *strike* is a macro selector (``"ATM"`` / ``"ATM+N"`` /
    ``"ATM-N"``), *drv_option_type* is ``"CALL"`` / ``"PUT"``, and *instrument* is one
    of :data:`ROLLING_OPTION_INSTRUMENTS`. Raises :class:`ValueError` for an
    out-of-set instrument (fail-loud, Golden Rule #1); expired FUTURES have no path.
    """
    if instrument not in ROLLING_OPTION_INSTRUMENTS:
        raise ValueError(
            f"rolling-option instrument must be one of {sorted(ROLLING_OPTION_INSTRUMENTS)} "
            f"(expired futures history is UNVERIFIED-absent on Dhan), got {instrument!r}"
        )
    payload = client.rolling_option(
        exchange_segment=exchange_segment,
        security_id=security_id,
        instrument=instrument,
        expiry_flag=expiry_flag,
        expiry_code=expiry_code,
        strike=strike,
        drv_option_type=drv_option_type,
        interval=interval,
        from_date=from_date,
        to_date=to_date,
        required_data=required_data,
    )
    return parse_rolling_option(payload)


__all__ = [
    "DEFAULT_REQUIRED_DATA",
    "ROLLING_OPTION_INSTRUMENTS",
    "fetch_rolling_option",
]
