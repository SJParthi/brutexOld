"""Rolling-ATM option plan — the PURE cartesian of requested rolling selectors.

DEC-DHAN-ROLLING-OPTIONS-001 (ADR-0022). The rolling-ATM track needs NO metadata
call — Dhan serves an ATM-relative ROLLING series, so there is NO ``get_expiries`` /
``get_contracts`` / ``registry.duckdb`` chain to resolve (contrast Groww, which bakes
a FIXED ``(expiry_date, strike)`` into each contract symbol). The pull surface is a
PURE cartesian of the requested selectors::

    {NIFTY, BANKNIFTY, SENSEX}          (3 underlyings)
    × {WEEK, MONTH}                     (2 expiry flags)
    × {0, 1, 2, 3}                      (4 expiry codes)
    × {ATM-10 … ATM+10}                 (21 strike offsets, band N=10)
    × {CALL, PUT}                       (2 option types)
    = 1008 keys

Each :class:`RollingOptionKey` IS the partition / dedup identity (see
:mod:`.rolling_option_writer`): every field is a REQUESTED selector, NONE is a date
or an absolute strike (those are per-ROW metadata). A key that Dhan does not serve
(e.g. SENSEX-WEEK, a wing offset beyond the served band) degrades to an honest empty
— clean-empty, re-pullable, NEVER a fabricated bar (§7 gate).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from brutex.adapters.dhan.constants import DHAN_INDEX_SECURITY_IDS
from brutex.adapters.dhan.rolling_option_constants import (
    DHAN_ROLLING_EXCHANGE_SEGMENTS,
    DHAN_ROLLING_EXCHANGES,
    DHAN_ROLLING_EXPIRY_CODES,
    DHAN_ROLLING_STRIKE_BAND,
    DHAN_ROLLING_UNDERLYINGS,
    EXPIRY_FLAG_MONTH,
    EXPIRY_FLAG_WEEK,
    OPTION_TYPE_CALL,
    OPTION_TYPE_PUT,
)

#: The expected key count of the default cartesian: 3 × 2 × 4 × 21 × 2 = 1008.
DHAN_ROLLING_PLAN_KEY_COUNT: Final[int] = 1008


def strike_offset_labels(band: int) -> tuple[str, ...]:
    """The ATM-relative strike-offset LABELS from ``ATM-band`` to ``ATM+band``.

    The honest, human-readable selector labels (``"ATM-10"`` … ``"ATM"`` …
    ``"ATM+10"``) sent verbatim as the wire ``strike`` field AND kept as the
    ``strike_offset`` column. The path-hazard-safe form (``ATMp1`` / ``ATMm1``)
    lives in :mod:`.rolling_option_writer`; here the label is the honest one.

    Parameters
    ----------
    band
        The band half-width (``>= 0``): ``2·band + 1`` offsets are returned,
        centred on ``ATM``.

    Raises
    ------
    ValueError
        If *band* is negative (fail-loud — Golden Rule #1).
    """
    if band < 0:
        raise ValueError(f"strike band must be non-negative, got {band}")
    labels: list[str] = []
    for offset in range(-band, band + 1):
        if offset == 0:
            labels.append("ATM")
        elif offset > 0:
            labels.append(f"ATM+{offset}")
        else:
            labels.append(f"ATM{offset}")  # negative sign already present
    return tuple(labels)


@dataclass(frozen=True, slots=True)
class RollingOptionKey:
    """One rolling-ATM pull key — the partition / dedup identity (all selectors).

    Every field is a REQUESTED selector (the six the path fixes) plus the wire
    addressing (``exchange_segment`` / ``security_id``). NONE is a date or an
    absolute strike — those are per-ROW metadata resolved by Dhan per bar.

    Attributes
    ----------
    underlying
        Engine index symbol (``"NIFTY"`` / ``"BANKNIFTY"`` / ``"SENSEX"``).
    exchange
        Partition-path exchange dir (``"NSE"`` / ``"BSE"``).
    exchange_segment
        Vendor ``exchangeSegment`` (``"NSE_FNO"`` / ``"BSE_FNO"``).
    security_id
        The UNDERLYING vendor ``securityId`` (NIFTY 13 / BANKNIFTY 25 / SENSEX 51).
    expiry_flag
        ``"WEEK"`` / ``"MONTH"``.
    expiry_code
        Expiry selector 0..3 (Near/Next/Far/Farther).
    strike_offset
        The requested ATM-relative selector label (``"ATM"`` / ``"ATM+3"`` /
        ``"ATM-2"``) — the honest label, distinct from the per-bar resolved
        absolute strike.
    option_type
        ``"CALL"`` / ``"PUT"``.
    """

    underlying: str
    exchange: str
    exchange_segment: str
    security_id: int
    expiry_flag: str
    expiry_code: int
    strike_offset: str
    option_type: str


def enumerate_rolling_option_keys(
    *, band: int = DHAN_ROLLING_STRIKE_BAND
) -> tuple[RollingOptionKey, ...]:
    """Enumerate the PURE cartesian of rolling-ATM pull keys (no I/O, no vendor call).

    Ordered deterministically: underlying → expiry_flag (WEEK, MONTH) →
    expiry_code (0..3) → strike_offset (ATM-band … ATM+band) → option_type
    (CALL, PUT). With the default ``band=10`` this yields exactly
    :data:`DHAN_ROLLING_PLAN_KEY_COUNT` (1008) keys.

    An underlying absent from :data:`DHAN_INDEX_SECURITY_IDS` /
    :data:`DHAN_ROLLING_EXCHANGES` is a wiring bug (fail-loud ``KeyError`` at
    enumeration, never a blind id) — the three constants are pinned in lockstep.
    """
    keys: list[RollingOptionKey] = []
    offsets = strike_offset_labels(band)
    # Stable enum orderings (frozensets are unordered — pin the wire order).
    flags = (EXPIRY_FLAG_WEEK, EXPIRY_FLAG_MONTH)
    option_types = (OPTION_TYPE_CALL, OPTION_TYPE_PUT)
    for underlying in DHAN_ROLLING_UNDERLYINGS:
        exchange = DHAN_ROLLING_EXCHANGES[underlying]
        exchange_segment = DHAN_ROLLING_EXCHANGE_SEGMENTS[underlying]
        security_id = DHAN_INDEX_SECURITY_IDS[underlying]
        for expiry_flag in flags:
            for expiry_code in DHAN_ROLLING_EXPIRY_CODES:
                for strike_offset in offsets:
                    for option_type in option_types:
                        keys.append(
                            RollingOptionKey(
                                underlying=underlying,
                                exchange=exchange,
                                exchange_segment=exchange_segment,
                                security_id=security_id,
                                expiry_flag=expiry_flag,
                                expiry_code=expiry_code,
                                strike_offset=strike_offset,
                                option_type=option_type,
                            )
                        )
    return tuple(keys)


__all__ = [
    "DHAN_ROLLING_PLAN_KEY_COUNT",
    "RollingOptionKey",
    "enumerate_rolling_option_keys",
    "strike_offset_labels",
]
