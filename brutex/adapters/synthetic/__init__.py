"""Synthetic, offline, deterministic market adapter (DEC-SYNTHETIC-ADAPTER-001).

The :class:`~brutex.adapters.synthetic.adapter.SyntheticMarketAdapter` is a
drop-in :class:`~brutex.adapters.protocol.MarketAdapter` that needs NO Groww
credentials and NO network: it generates seeded, reproducible OHLCV bars so the
engine / ingest / dashboard can run end-to-end fully offline.

HONESTY (Golden Rule #1/#10): the bars are GENERATED, not real market data —
the ``market_id`` is ``"synthetic"`` and the capabilities ``display_name`` says
so. Never present these bars as a real data source.
"""

from __future__ import annotations

from brutex.adapters.synthetic.adapter import (
    SYNTHETIC_CAPABILITIES,
    SyntheticMarketAdapter,
)
from brutex.adapters.synthetic.constants import (
    DEFAULT_SEED,
    MARKET_ID,
    SUPPORTED_INTERVALS,
    SYMBOL_SPECS,
    SyntheticSymbolSpec,
)
from brutex.adapters.synthetic.generator import generate_candles
from brutex.adapters.synthetic.instruments import (
    SyntheticInstrumentIndex,
    build_synthetic_index,
)

__all__ = [
    "DEFAULT_SEED",
    "MARKET_ID",
    "SUPPORTED_INTERVALS",
    "SYMBOL_SPECS",
    "SYNTHETIC_CAPABILITIES",
    "SyntheticInstrumentIndex",
    "SyntheticMarketAdapter",
    "SyntheticSymbolSpec",
    "build_synthetic_index",
    "generate_candles",
]
