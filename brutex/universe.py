"""BruteX v1 universe — the locked 8 F&O indices.

This module defines the scope-locked universe from ``REQUIREMENTS.md``
``FD-09a`` and ``FD-09b``. The adapter filters every Groww row against
this allow-list — any row whose underlying is not in :data:`V1_UNDERLYINGS`
raises :exc:`~brutex.models.OutOfScopeError` and is never written to the
canonical data lake.

Scope lock status: **FROZEN at v1.0** — changing this list requires a
version bump and new product-owner sign-off per ``REQUIREMENTS.md §27``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from brutex.models import Exchange, Instrument, InstrumentType

# ────────────────────────────────────────────────────────────────────────
# The 8 F&O indices
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UnderlyingSpec:
    """Spec for one F&O-tradeable index in the v1 universe."""

    exchange: Exchange
    underlying_symbol: str  # matches Groww's get_expiries underlying_symbol arg
    display_name: str
    fno_live_since_year: int

    def __str__(self) -> str:
        return f"{self.exchange}:{self.underlying_symbol}"


# Order is intentional — NSE first (deeper history), then BSE.
# The adapter iterates this list in order during P1a/P1b enumeration.
V1_UNDERLYINGS: Final[tuple[UnderlyingSpec, ...]] = (
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


# ────────────────────────────────────────────────────────────────────────
# Fast lookup sets — O(1) membership checks used by the adapter filter
# ────────────────────────────────────────────────────────────────────────


def _build_allowed_underlyings() -> frozenset[tuple[Exchange, str]]:
    return frozenset((u.exchange, u.underlying_symbol) for u in V1_UNDERLYINGS)


def _build_allowed_index_tokens() -> frozenset[tuple[Exchange, str]]:
    """Spot index rows are matched by (exchange, trading_symbol)."""
    return frozenset((u.exchange, u.underlying_symbol) for u in V1_UNDERLYINGS)


ALLOWED_UNDERLYINGS: Final[frozenset[tuple[Exchange, str]]] = _build_allowed_underlyings()
ALLOWED_INDEX_SPOTS: Final[frozenset[tuple[Exchange, str]]] = _build_allowed_index_tokens()


def is_in_scope(instrument: Instrument) -> bool:
    """Return True if *instrument* belongs to the locked v1 universe.

    Rules
    -----
    * Spot indices (``IDX``): ``(exchange, trading_symbol)`` must be in
      :data:`ALLOWED_INDEX_SPOTS`.
    * Derivatives (``FUT``/``CE``/``PE``): ``(exchange, underlying_symbol)``
      must be in :data:`ALLOWED_UNDERLYINGS`.
    * Equities (``EQ``): out of scope in v1.
    """
    if instrument.instrument_type == InstrumentType.IDX:
        return (instrument.exchange, instrument.trading_symbol) in ALLOWED_INDEX_SPOTS
    if instrument.is_derivative:
        if instrument.underlying_symbol is None:
            return False
        return (instrument.exchange, instrument.underlying_symbol) in ALLOWED_UNDERLYINGS
    # EQ and anything else → out of scope in v1.
    return False


__all__ = [
    "ALLOWED_INDEX_SPOTS",
    "ALLOWED_UNDERLYINGS",
    "UnderlyingSpec",
    "V1_UNDERLYINGS",
    "is_in_scope",
]
