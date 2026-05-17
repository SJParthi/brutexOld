"""Hardcoded Dhan options-trading constants.

Per `docs/track2/TRACK2_OPTIONS_SPEC.md` §2 (Universe) — Dhan facts
verified 2026-05-15.

| Underlying | Spot secId | Spot seg | Options seg | Trading symbol |
|---|---|---|---|---|
| NIFTY 50   | 13 | IDX_I | NSE_FNO / OPTIDX | NIFTY     |
| BANKNIFTY  | 25 | IDX_I | NSE_FNO / OPTIDX | BANKNIFTY |
| SENSEX     | 51 | IDX_I | BSE_FNO / OPTIDX | SENSEX    |

Hardcoded per CLAUDE.md §6 — for the locked 3-instrument Track 2 universe,
hardcoded constants are simpler than runtime CSV lookup. Smoke test
verifies quarterly against `web.dhan.co/Charts?secid=X`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

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
    spot_security_id=13,
    spot_exchange_segment="IDX_I",
    spot_instrument="INDEX",
    options_exchange_segment="NSE_FNO",
    options_instrument="OPTIDX",
    trading_symbol="NIFTY",
)

BANKNIFTY: Final[DhanOptionsContract] = DhanOptionsContract(
    underlying=OptionsUnderlying.BANKNIFTY,
    spot_security_id=25,
    spot_exchange_segment="IDX_I",
    spot_instrument="INDEX",
    options_exchange_segment="NSE_FNO",
    options_instrument="OPTIDX",
    trading_symbol="BANKNIFTY",
)

SENSEX: Final[DhanOptionsContract] = DhanOptionsContract(
    underlying=OptionsUnderlying.SENSEX,
    spot_security_id=51,
    spot_exchange_segment="IDX_I",
    spot_instrument="INDEX",
    options_exchange_segment="BSE_FNO",
    options_instrument="OPTIDX",
    trading_symbol="SENSEX",
)

ALL_CONTRACTS: Final[tuple[DhanOptionsContract, ...]] = (NIFTY, BANKNIFTY, SENSEX)

BY_UNDERLYING: Final[dict[OptionsUnderlying, DhanOptionsContract]] = {
    c.underlying: c for c in ALL_CONTRACTS
}


def get_contract(underlying: OptionsUnderlying) -> DhanOptionsContract:
    """Look up the Dhan options contract metadata by underlying."""
    return BY_UNDERLYING[underlying]
