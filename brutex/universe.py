"""BruteX Track 1 universe — Indian spot indexes + India VIX.

**STATUS (2026-05-15):** rewritten for Track 1 / Dhan migration. The
prior universe (8 F&O indices + dynamic stock-derivative discovery) is
out of scope for Track 1. India VIX added after 4-agent verification on
2026-05-15 (late-day scope expansion, see
``docs/TRACK1_DATA_INGEST_DESIGN.md`` §2 and ``docs/MIGRATION.md``
decisions #71-72).

## Track 1 universe

Four Indian equity-index instruments:

* **NIFTY 50** (NSE spot)
* **NIFTY BANK** (NSE spot) — symbol ``BANKNIFTY``
* **BSE SENSEX** (BSE spot) — symbol ``SENSEX``
* **India VIX** (NSE volatility index) — symbol ``INDIAVIX``

All four are computed index values, not tradable directly. Track 1
produces signal-only research output (``paper_pnl_paisa``); execution-
realistic PnL is deferred to the Options Track (post-Track-1).

India VIX is a volatility index calculated from NIFTY 50 option mid-
quotes. It updates every 15 seconds (NOT tick-by-tick) and shares the
NSE trading window (09:15-15:30 IST) with the spot indexes.

## Scope filter

``is_in_scope(instrument)`` accepts a row iff:

1. ``instrument.instrument_type == IDX`` (spot index, not a derivative)
2. ``(exchange, trading_symbol)`` is one of the three Track 1 indexes.

All cash equity, all F&O derivatives, all non-Track-1 indices, and all
commodities are rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from brutex.models import Exchange, Instrument, InstrumentType

# ────────────────────────────────────────────────────────────────────────
# Spec dataclass
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UnderlyingSpec:
    """Spec for one named index underlying in the Track 1 universe."""

    exchange: Exchange
    underlying_symbol: str
    display_name: str
    spot_live_since_year: int

    def __str__(self) -> str:
        return f"{self.exchange}:{self.underlying_symbol}"


# ────────────────────────────────────────────────────────────────────────
# The 4 Track 1 named indexes (allow-list)
# ────────────────────────────────────────────────────────────────────────


#: The 4 named indexes. Iterated in order during enumeration.
V1_INDICES: Final[tuple[UnderlyingSpec, ...]] = (
    UnderlyingSpec(
        exchange=Exchange.NSE,
        underlying_symbol="NIFTY",
        display_name="NIFTY 50",
        spot_live_since_year=1996,
    ),
    UnderlyingSpec(
        exchange=Exchange.NSE,
        underlying_symbol="BANKNIFTY",
        display_name="NIFTY BANK",
        spot_live_since_year=2003,
    ),
    UnderlyingSpec(
        exchange=Exchange.BSE,
        underlying_symbol="SENSEX",
        display_name="BSE SENSEX",
        spot_live_since_year=1986,
    ),
    UnderlyingSpec(
        exchange=Exchange.NSE,
        underlying_symbol="INDIAVIX",
        display_name="India VIX",
        spot_live_since_year=2008,
    ),
)


#: Backwards-compatibility alias.
V1_UNDERLYINGS: Final[tuple[UnderlyingSpec, ...]] = V1_INDICES


# ────────────────────────────────────────────────────────────────────────
# Fast lookup sets — O(1) membership used by the scope filter
# ────────────────────────────────────────────────────────────────────────


def _build_index_symbols() -> frozenset[tuple[Exchange, str]]:
    """Return frozenset of (exchange, trading_symbol) tuples for the Track 1 universe."""
    return frozenset((u.exchange, u.underlying_symbol) for u in V1_INDICES)


#: ``(exchange, symbol)`` pairs for the 3 named spot indexes.
INDEX_UNDERLYINGS: Final[frozenset[tuple[Exchange, str]]] = _build_index_symbols()


#: Backwards-compat alias.
ALLOWED_UNDERLYINGS: Final[frozenset[tuple[Exchange, str]]] = INDEX_UNDERLYINGS


# ────────────────────────────────────────────────────────────────────────
# Scope classification
# ────────────────────────────────────────────────────────────────────────


def is_index_underlying(exchange: Exchange, underlying_symbol: str) -> bool:
    """Return True if ``(exchange, underlying_symbol)`` is one of the 3 named indexes."""
    return (exchange, underlying_symbol) in INDEX_UNDERLYINGS


def is_in_scope(instrument: Instrument) -> bool:
    """Return True if *instrument* belongs to the Track 1 spot-index universe.

    Accept rule (only one)
    ----------------------
    ``instrument_type == IDX`` AND ``(exchange, trading_symbol)`` is in
    :data:`INDEX_UNDERLYINGS`. Everything else (cash equity, derivatives,
    non-Track-1 indices, commodities) is rejected.
    """
    if instrument.instrument_type == InstrumentType.IDX:
        return (instrument.exchange, instrument.trading_symbol) in INDEX_UNDERLYINGS
    return False


def classify_underlying_kind(instrument: Instrument) -> str:
    """Return ``"index"`` for a Track 1 in-scope instrument.

    Track 1 contains only indexes — there are no stocks or derivatives.

    Raises
    ------
    ValueError
        If called on an instrument that is not an in-scope Track 1 index.
    """
    if not is_in_scope(instrument):
        raise ValueError(
            f"{instrument.trading_symbol}: not in Track 1 scope — "
            f"only NIFTY, BANKNIFTY, SENSEX, INDIAVIX are accepted"
        )
    return "index"


__all__ = [
    "ALLOWED_UNDERLYINGS",
    "INDEX_UNDERLYINGS",
    "V1_INDICES",
    "V1_UNDERLYINGS",
    "UnderlyingSpec",
    "classify_underlying_kind",
    "is_in_scope",
    "is_index_underlying",
]
