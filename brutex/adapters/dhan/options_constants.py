"""Hardcoded Dhan options-trading constants.

Per ``docs/track2/TRACK2_OPTIONS_SPEC.md`` §2 (Universe) — Dhan facts
verified 2026-05-15.

| Underlying | Spot secId | Spot seg | Options seg | Trading symbol |
|---|---|---|---|---|
| NIFTY 50   | 13 | IDX_I | NSE_FNO / OPTIDX | NIFTY     |
| BANKNIFTY  | 25 | IDX_I | NSE_FNO / OPTIDX | BANKNIFTY |
| SENSEX     | 51 | IDX_I | BSE_FNO / OPTIDX | SENSEX    |

Hardcoded per CLAUDE.md §6 — for the locked 3-instrument Track 2
universe, hardcoded constants are simpler than runtime CSV lookup.
Smoke test verifies quarterly against ``web.dhan.co/Charts?secid=X``.

**Single source of truth (Rule #11 COMMON):** the `spot_security_id`
integers + `spot_exchange_segment` + `spot_instrument` strings are
inherited from :mod:`brutex.adapters.dhan.constants`. They appear in
exactly one place in the codebase; this module annotates each Track 1
spot instrument with the Track 2 options-chain wiring (``options_exchange_segment``
+ ``options_instrument``) plus the ``OptionsUnderlying`` enum hookup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from brutex.adapters.dhan.constants import (
    BANKNIFTY as _SPOT_BANKNIFTY,
)
from brutex.adapters.dhan.constants import (
    NIFTY as _SPOT_NIFTY,
)
from brutex.adapters.dhan.constants import (
    SENSEX as _SPOT_SENSEX,
)
from brutex.options.lot_size_history import OptionsUnderlying


@dataclass(frozen=True)
class DhanOptionsContract:
    """All hardcoded Dhan API metadata for one options underlying."""

    underlying: OptionsUnderlying

    # Spot index (used to find ATM and Track 1 spot data — same as Track 1 spec §6)
    spot_security_id: int
    spot_exchange_segment: str  # "IDX_I"
    spot_instrument: str  # "INDEX"

    # Options chain (Track 2 fetch via /v2/charts/rollingoption)
    options_exchange_segment: str  # "NSE_FNO" or "BSE_FNO"
    options_instrument: str  # "OPTIDX"

    # Trading symbol used in API requests (e.g., "NIFTY", "BANKNIFTY", "SENSEX")
    trading_symbol: str


NIFTY: Final[DhanOptionsContract] = DhanOptionsContract(
    underlying=OptionsUnderlying.NIFTY,
    spot_security_id=_SPOT_NIFTY.security_id,
    spot_exchange_segment=_SPOT_NIFTY.segment,
    spot_instrument=_SPOT_NIFTY.instrument_type,
    options_exchange_segment="NSE_FNO",
    options_instrument="OPTIDX",
    trading_symbol=_SPOT_NIFTY.symbol,
)

BANKNIFTY: Final[DhanOptionsContract] = DhanOptionsContract(
    underlying=OptionsUnderlying.BANKNIFTY,
    spot_security_id=_SPOT_BANKNIFTY.security_id,
    spot_exchange_segment=_SPOT_BANKNIFTY.segment,
    spot_instrument=_SPOT_BANKNIFTY.instrument_type,
    options_exchange_segment="NSE_FNO",
    options_instrument="OPTIDX",
    trading_symbol=_SPOT_BANKNIFTY.symbol,
)

SENSEX: Final[DhanOptionsContract] = DhanOptionsContract(
    underlying=OptionsUnderlying.SENSEX,
    spot_security_id=_SPOT_SENSEX.security_id,
    spot_exchange_segment=_SPOT_SENSEX.segment,
    spot_instrument=_SPOT_SENSEX.instrument_type,
    options_exchange_segment="BSE_FNO",
    options_instrument="OPTIDX",
    trading_symbol=_SPOT_SENSEX.symbol,
)

ALL_CONTRACTS: Final[tuple[DhanOptionsContract, ...]] = (NIFTY, BANKNIFTY, SENSEX)

BY_UNDERLYING: Final[dict[OptionsUnderlying, DhanOptionsContract]] = {
    c.underlying: c for c in ALL_CONTRACTS
}


def get_contract(underlying: OptionsUnderlying) -> DhanOptionsContract:
    """Look up the Dhan options contract metadata by underlying."""
    return BY_UNDERLYING[underlying]
