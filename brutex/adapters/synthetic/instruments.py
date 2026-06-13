"""Synthetic instrument universe + the O(1) lookup index.

Builds the fixed synthetic universe directly from
:data:`~brutex.adapters.synthetic.constants.SYMBOL_SPECS` — no CSV, no network
(DEC-SYNTHETIC-ADAPTER-001). Each spec becomes one vendor-neutral
:class:`~brutex.models.Instrument`, and the index exposes the two O(1) maps the
adapter needs:

* ``spec_by_market_symbol`` — ``"NSE-NIFTY" -> SyntheticSymbolSpec`` (drives generation)
* ``universe``            — the immutable :class:`Instrument` page for ``list_instruments``

Identity follows the Groww leaf convention: a non-numeric symbol gets a stable
``blake2b``-derived surrogate ``security_id`` (an internal surrogate, explicitly
NOT a real vendor id — Golden Rule #1), unique per ``(exchange, symbol)`` via the
distinct ``EXCHANGE-SYMBOL`` key.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from brutex.adapters.synthetic.constants import SYMBOL_SPECS, SyntheticSymbolSpec
from brutex.models import Instrument, InstrumentType, Segment

#: Width (bytes) of the surrogate ``security_id`` digest — 4 bytes = 32 bits.
_SECURITY_ID_BYTES: Final[int] = 4


def _security_id(market_symbol: str) -> int:
    """Return a stable surrogate ``security_id`` for a synthetic *market_symbol*.

    Deterministic ``blake2b`` 4-byte int keyed on the unique ``EXCHANGE-SYMBOL``
    string. An internal surrogate only — never a real vendor identifier.
    """
    digest = hashlib.blake2b(market_symbol.encode(), digest_size=_SECURITY_ID_BYTES)
    return int.from_bytes(digest.digest(), "big")


def instrument_for_spec(spec: SyntheticSymbolSpec) -> Instrument:
    """Build the vendor-neutral :class:`Instrument` for one synthetic *spec*.

    Indices take :attr:`~brutex.models.Segment.INDEX` (mirroring the Groww
    mapping); the synthetic equity takes :attr:`~brutex.models.Segment.CASH`.
    """
    segment = Segment.INDEX if spec.instrument_type == InstrumentType.IDX else Segment.CASH
    return Instrument(
        exch_id=spec.exchange,
        security_id=_security_id(spec.market_symbol),
        symbol_name=spec.symbol,
        display_name=spec.display_name,
        instrument=spec.instrument_type,
        segment=segment,
        series=spec.series,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=spec.lot_size,
        tick_size=spec.tick_size,
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        option_type=None,
        expiry_flag=None,
        asm_gsm_flag=False,
    )


@dataclass(frozen=True, slots=True)
class SyntheticInstrumentIndex:
    """O(1) synthetic instrument lookup, built in one pass over the spec table.

    ``spec_by_market_symbol`` keys on the ``EXCHANGE-SYMBOL`` string for O(1)
    resolution during ``fetch_ohlcv``; ``universe`` is the immutable
    :class:`Instrument` page surfaced by ``list_instruments``.
    """

    spec_by_market_symbol: dict[str, SyntheticSymbolSpec]
    universe: tuple[Instrument, ...]


def build_synthetic_index(
    specs: Iterable[SyntheticSymbolSpec] = SYMBOL_SPECS,
) -> SyntheticInstrumentIndex:
    """Build the O(1) index from *specs* (defaulting to the fixed universe).

    One O(n) pass; every downstream resolution is an O(1) dict access. Raises
    :class:`ValueError` on a duplicate ``EXCHANGE-SYMBOL`` key (fail-loud — a
    duplicate would make lookups ambiguous, Golden Rule #1).
    """
    spec_by_symbol: dict[str, SyntheticSymbolSpec] = {}
    universe: list[Instrument] = []
    for spec in specs:
        key = spec.market_symbol
        if key in spec_by_symbol:
            raise ValueError(f"duplicate synthetic symbol: {key}")
        spec_by_symbol[key] = spec
        universe.append(instrument_for_spec(spec))
    return SyntheticInstrumentIndex(
        spec_by_market_symbol=spec_by_symbol,
        universe=tuple(universe),
    )


__all__ = [
    "SyntheticInstrumentIndex",
    "build_synthetic_index",
    "instrument_for_spec",
]
