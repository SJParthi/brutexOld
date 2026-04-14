"""BruteX v1 universe — the full FNO segment.

Expanded 2026-04-14 per product-owner decision: v1 scope includes the
entire Groww FNO segment, which is:

* **8 named F&O indices** (5 NSE + 3 BSE) — see :data:`V1_INDICES`
* **All F&O-eligible NSE stocks** — roughly ~215 underlyings as of late
  2024/2025, **discovered at runtime** from the Groww instrument CSV
  (not hardcoded; SEBI revises the list periodically)

The adapter filters every Groww row via :func:`is_in_scope`. Three
accept conditions:

1. Row is a named v1 spot index (``IDX`` in CASH segment, matched by
   ``(exchange, trading_symbol)`` against :data:`V1_INDICES`). These
   rows give us the underlying reference price for index options.
2. Row is an index derivative (FUT/CE/PE in FNO segment whose
   underlying is one of the 8 named indices).
3. Row is a stock derivative (FUT/CE/PE in FNO segment whose underlying
   is NOT one of the 8 named indices — inferred to be a stock).

Rejected: cash equity (``EQ``), non-v1 indices (e.g., NIFTY IT, NIFTY
AUTO), COMMODITY segment, MCX exchange (exchange enum does not include
MCX so it cannot even be constructed).

Scope lock status: **FROZEN at v1.0.1** — changing this requires a
version bump and new product-owner sign-off per ``REQUIREMENTS.md §27``.

See ``REQUIREMENTS.md §1.2 / §3 FD-09 / FD-09a / FD-09b / FD-09d``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from brutex.models import Exchange, Instrument, InstrumentType, Segment

# ────────────────────────────────────────────────────────────────────────
# Named F&O indices (explicit allow-list) — FD-09a + FD-09b
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UnderlyingSpec:
    """Spec for one named F&O-tradeable index in the v1 universe.

    Only the 8 indices are named explicitly. Stock underlyings are
    discovered at runtime and do NOT get an ``UnderlyingSpec``.
    """

    exchange: Exchange
    underlying_symbol: str  # matches Groww's get_expiries underlying_symbol arg
    display_name: str
    fno_live_since_year: int

    def __str__(self) -> str:
        return f"{self.exchange}:{self.underlying_symbol}"


#: The 8 named F&O indices. Iterated in order during enumeration.
V1_INDICES: Final[tuple[UnderlyingSpec, ...]] = (
    # ─── NSE (FD-09a) ─────────────────────────────────────────────────
    UnderlyingSpec(
        exchange=Exchange.NSE,
        underlying_symbol="NIFTY",
        display_name="NIFTY 50",
        fno_live_since_year=2000,
    ),
    UnderlyingSpec(
        exchange=Exchange.NSE,
        underlying_symbol="BANKNIFTY",
        display_name="NIFTY BANK",
        fno_live_since_year=2005,
    ),
    UnderlyingSpec(
        exchange=Exchange.NSE,
        underlying_symbol="FINNIFTY",
        display_name="NIFTY FIN SERVICE",
        fno_live_since_year=2021,
    ),
    UnderlyingSpec(
        exchange=Exchange.NSE,
        underlying_symbol="MIDCPNIFTY",
        display_name="NIFTY MIDCAP SELECT",
        fno_live_since_year=2023,
    ),
    UnderlyingSpec(
        exchange=Exchange.NSE,
        underlying_symbol="NIFTYNXT50",
        display_name="NIFTY NEXT 50",
        fno_live_since_year=2024,
    ),
    # ─── BSE (FD-09b) ─────────────────────────────────────────────────
    UnderlyingSpec(
        exchange=Exchange.BSE,
        underlying_symbol="SENSEX",
        display_name="BSE SENSEX",
        fno_live_since_year=2000,
    ),
    UnderlyingSpec(
        exchange=Exchange.BSE,
        underlying_symbol="BANKEX",
        display_name="BSE BANKEX",
        fno_live_since_year=2023,
    ),
    UnderlyingSpec(
        exchange=Exchange.BSE,
        underlying_symbol="SENSEX50",
        display_name="BSE SENSEX 50",
        fno_live_since_year=2023,
    ),
)


# Backwards-compatibility alias so older references to V1_UNDERLYINGS
# still resolve. New code should use V1_INDICES for clarity.
V1_UNDERLYINGS: Final[tuple[UnderlyingSpec, ...]] = V1_INDICES


# ────────────────────────────────────────────────────────────────────────
# Fast lookup sets — O(1) membership used by the scope filter
# ────────────────────────────────────────────────────────────────────────


def _build_index_symbols() -> frozenset[tuple[Exchange, str]]:
    return frozenset((u.exchange, u.underlying_symbol) for u in V1_INDICES)


#: ``(exchange, symbol)`` pairs for the 8 named index spots.
#: Used to match IDX rows in CASH segment, and to classify an FNO
#: derivative's underlying as "index" vs "stock".
INDEX_UNDERLYINGS: Final[frozenset[tuple[Exchange, str]]] = _build_index_symbols()


#: Backwards-compat alias so older references to ALLOWED_UNDERLYINGS
#: still resolve.
ALLOWED_UNDERLYINGS: Final[frozenset[tuple[Exchange, str]]] = INDEX_UNDERLYINGS


# ────────────────────────────────────────────────────────────────────────
# Scope classification
# ────────────────────────────────────────────────────────────────────────


def is_index_underlying(exchange: Exchange, underlying_symbol: str) -> bool:
    """Return True if ``(exchange, underlying_symbol)`` is one of the 8 named indices."""
    return (exchange, underlying_symbol) in INDEX_UNDERLYINGS


def is_in_scope(instrument: Instrument) -> bool:
    """Return True if *instrument* belongs to the locked v1 FNO universe.

    Accept rules
    ------------
    1. **Named spot index** — ``instrument_type == IDX`` AND
       ``(exchange, trading_symbol)`` in :data:`INDEX_UNDERLYINGS`.
       Provides the underlying reference price for index options.
    2. **Any FNO derivative** — ``segment == FNO`` AND
       ``instrument_type in {FUT, CE, PE}``. This is the broad v1
       expansion: includes ALL index and stock F&O contracts. The
       specific underlying is discovered at runtime from the CSV.
    3. **Nothing else** — ``EQ`` rows rejected, ``IDX`` rows outside
       the 8 named indices rejected.
    """
    # Rule 1: named spot indices
    if instrument.instrument_type == InstrumentType.IDX:
        return (instrument.exchange, instrument.trading_symbol) in INDEX_UNDERLYINGS

    # Rule 2: any FNO derivative (futures + options for BOTH index and stock)
    if instrument.segment == Segment.FNO and instrument.is_derivative:
        if instrument.underlying_symbol is None:
            return False
        return True

    # Rule 3: everything else → out of scope
    return False


def classify_underlying_kind(instrument: Instrument) -> str:
    """Return ``"index"`` or ``"stock"`` for an in-scope FNO derivative.

    Raises
    ------
    ValueError
        If called on an instrument that is not an in-scope FNO derivative.
    """
    if not (instrument.segment == Segment.FNO and instrument.is_derivative):
        raise ValueError(
            f"{instrument.groww_symbol}: not an FNO derivative — "
            f"segment={instrument.segment}, type={instrument.instrument_type}"
        )
    if instrument.underlying_symbol is None:
        raise ValueError(f"{instrument.groww_symbol}: missing underlying_symbol")
    if is_index_underlying(instrument.exchange, instrument.underlying_symbol):
        return "index"
    return "stock"


__all__ = [
    "ALLOWED_UNDERLYINGS",
    "INDEX_UNDERLYINGS",
    "UnderlyingSpec",
    "V1_INDICES",
    "V1_UNDERLYINGS",
    "classify_underlying_kind",
    "is_in_scope",
    "is_index_underlying",
]
