"""Hardcoded Dhan API metadata for the Track 1 4-instrument spot universe.

Single source of truth for the (securityId, segment, instrument, exchange,
symbol) 5-tuple of every Track 1 spot index. Per CLAUDE.md §6:247 the
4-tuple is permanently locked — runtime CSV lookup adds complexity for no
benefit, so hardcoded constants live here and are verified quarterly by
the opt-in smoke test (`tests/smoke/test_dhan_securityid_verification.py`).

Verified facts (CLAUDE.md §6 Verified Dhan Facts table):

| Instrument | symbol (internal) | dhan_csv_symbol | securityId | exchange | segment | instrument |
|---|---|---|---|---|---|---|
| NIFTY 50    | NIFTY     | NIFTY 50    | 13 | NSE | IDX_I | INDEX |
| BANKNIFTY   | BANKNIFTY | NIFTY BANK  | 25 | NSE | IDX_I | INDEX |
| SENSEX      | SENSEX    | SENSEX      | 51 | BSE | IDX_I | INDEX |
| INDIA VIX   | INDIAVIX  | INDIA VIX   | 21 | NSE | IDX_I | INDEX |

The dual ``symbol`` (internal canonical, no space) + ``dhan_csv_symbol`` (Dhan
master CSV form, with spaces) fields are mandated by 4-agent pre-impl review
(2026-05-18 Agent 3 §B1): ``universe.py`` already commits to ``INDIAVIX`` as
the internal canonical form (see `brutex/universe.py:100`), but the Dhan
master CSV (https://images.dhan.co/api-data/api-scrip-master-detailed.csv)
publishes it as ``INDIA VIX`` with a space — so we keep both and let the
adapter use whichever form the endpoint expects.

The module-level constants are plain ``NIFTY`` / ``BANKNIFTY`` / ``SENSEX``
/ ``INDIAVIX``. The sibling ``options_constants.py`` module also exports
``NIFTY`` / ``BANKNIFTY`` / ``SENSEX`` (as ``DhanOptionsContract``), but
the two never collide in practice because callers import via full module
path (``from brutex.adapters.dhan.constants import NIFTY`` vs ``from
brutex.adapters.dhan.options_constants import NIFTY``). The
``test_dhan_constants.py::TestCoexistenceWithOptionsConstants`` suite
asserts both modules agree on the shared securityIds so silent drift is
caught at test time.

Per Golden Rule #11 COMMON: this file is the canonical store for the
securityId integers. Downstream callers (Dhan adapter, smoke tests,
golden tests) import these constants — they do NOT re-declare
``13`` / ``25`` / ``51`` / ``21`` inline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class DhanSpotInstrument:
    """Hardcoded Dhan API metadata for one Track 1 spot index.

    Frozen + slots — instances are module-level singletons, never mutated
    after construction. Slots prevent accidental attribute set via
    monkeypatch in tests; frozen rejects assignment with FrozenInstanceError.

    Attributes
    ----------
    symbol
        Internal canonical symbol (no spaces): ``NIFTY``, ``BANKNIFTY``,
        ``SENSEX``, ``INDIAVIX``. Matches ``UnderlyingSpec.underlying_symbol``
        in ``brutex/universe.py``.
    dhan_csv_symbol
        Symbol as it appears in the Dhan master CSV
        (``api-scrip-master-detailed.csv``). May contain spaces.
    security_id
        Integer Dhan ``securityId`` for the spot instrument. Verified
        against ``web.dhan.co/Charts?exch=IDX&seg=I&secid=N`` per
        CLAUDE.md §6 Verified Dhan Facts table.
    exchange
        ``"NSE"`` for NIFTY / BANKNIFTY / INDIAVIX, ``"BSE"`` for SENSEX
        (SENSEX is a BSE index per universe.py:93).
    segment
        Dhan ``ExchangeSegment`` enum value as a string. For all 4
        Track 1 spot instruments this is ``"IDX_I"`` (index segment).
    instrument_type
        Dhan ``InstrumentType`` enum value as a string. For all 4
        Track 1 spot instruments this is ``"INDEX"``.
    """

    symbol: str
    dhan_csv_symbol: str
    security_id: int
    exchange: str
    segment: str
    instrument_type: str


NIFTY: Final[DhanSpotInstrument] = DhanSpotInstrument(
    symbol="NIFTY",
    dhan_csv_symbol="NIFTY 50",
    security_id=13,
    exchange="NSE",
    segment="IDX_I",
    instrument_type="INDEX",
)

BANKNIFTY: Final[DhanSpotInstrument] = DhanSpotInstrument(
    symbol="BANKNIFTY",
    dhan_csv_symbol="NIFTY BANK",
    security_id=25,
    exchange="NSE",
    segment="IDX_I",
    instrument_type="INDEX",
)

SENSEX: Final[DhanSpotInstrument] = DhanSpotInstrument(
    symbol="SENSEX",
    dhan_csv_symbol="SENSEX",
    security_id=51,
    exchange="BSE",
    segment="IDX_I",
    instrument_type="INDEX",
)

INDIAVIX: Final[DhanSpotInstrument] = DhanSpotInstrument(
    symbol="INDIAVIX",
    dhan_csv_symbol="INDIA VIX",
    security_id=21,
    exchange="NSE",
    segment="IDX_I",
    instrument_type="INDEX",
)


#: All 4 Track 1 spot instruments in deterministic order
#: (NIFTY, BANKNIFTY, SENSEX, INDIAVIX). Order matches
#: ``brutex/universe.py::V1_INDICES`` so tests pinning one pin the other.
ALL_SPOT_INSTRUMENTS: Final[tuple[DhanSpotInstrument, ...]] = (
    NIFTY,
    BANKNIFTY,
    SENSEX,
    INDIAVIX,
)


#: O(1) reverse lookup: securityId → DhanSpotInstrument.
#: The adapter uses this when an API response carries only the security_id;
#: tests use it to assert no two instruments share an ID.
BY_SECURITY_ID: Final[dict[int, DhanSpotInstrument]] = {
    inst.security_id: inst for inst in ALL_SPOT_INSTRUMENTS
}


#: O(1) reverse lookup: internal canonical symbol → DhanSpotInstrument.
#: Callers reaching the adapter from ``UnderlyingSpec.underlying_symbol``
#: (which uses the internal form) look up here.
BY_SYMBOL: Final[dict[str, DhanSpotInstrument]] = {
    inst.symbol: inst for inst in ALL_SPOT_INSTRUMENTS
}
