"""Unit tests for ``brutex.adapters.dhan.constants``.

Pins every field of every Track 1 spot instrument. The 4-tuple is locked
by CLAUDE.md §3 (scope-lock rule); these tests fail loud if any of the
4 securityIds, segments, exchanges, or symbol forms changes silently.

The frozen-dataclass + slots discipline is asserted explicitly so
``mypy --strict`` + ``ruff`` agree the singletons cannot be mutated at
runtime.
"""

from __future__ import annotations

import dataclasses

import pytest
from brutex.adapters.dhan.constants import (
    ALL_SPOT_INSTRUMENTS,
    BANKNIFTY,
    BY_SECURITY_ID,
    BY_SYMBOL,
    INDIAVIX,
    NIFTY,
    SENSEX,
    DhanSpotInstrument,
)


class TestDhanSpotInstrumentDataclass:
    """The DhanSpotInstrument dataclass is frozen + slots."""

    def test_is_frozen(self) -> None:
        """Mutating a field raises FrozenInstanceError."""
        with pytest.raises(dataclasses.FrozenInstanceError):
            NIFTY.security_id = 999  # type: ignore[misc]

    def test_uses_slots(self) -> None:
        """The dataclass declares __slots__ — no __dict__ overhead."""
        assert not hasattr(NIFTY, "__dict__")
        assert hasattr(DhanSpotInstrument, "__slots__")

    def test_cannot_add_new_attribute(self) -> None:
        """Slots + frozen together reject any unknown attribute set.

        The combination raises a low-level TypeError from the dataclass
        __setattr__ (frozen path interacts with slots-MRO) rather than
        the usual AttributeError; either form must reject the assignment.
        """
        with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
            NIFTY.extra = "boom"  # type: ignore[attr-defined]


class TestSpotInstrumentSecurityIds:
    """Pin the 4 verified securityIds from CLAUDE.md §6."""

    def test_nifty_security_id_is_13(self) -> None:
        assert NIFTY.security_id == 13

    def test_banknifty_security_id_is_25(self) -> None:
        assert BANKNIFTY.security_id == 25

    def test_sensex_security_id_is_51(self) -> None:
        assert SENSEX.security_id == 51

    def test_indiavix_security_id_is_21(self) -> None:
        assert INDIAVIX.security_id == 21


class TestSpotInstrumentSymbols:
    """Pin the dual symbol fields (internal vs Dhan CSV)."""

    def test_nifty_symbol_pair(self) -> None:
        assert NIFTY.symbol == "NIFTY"
        assert NIFTY.dhan_csv_symbol == "NIFTY 50"

    def test_banknifty_symbol_pair(self) -> None:
        assert BANKNIFTY.symbol == "BANKNIFTY"
        assert BANKNIFTY.dhan_csv_symbol == "NIFTY BANK"

    def test_sensex_symbol_pair(self) -> None:
        assert SENSEX.symbol == "SENSEX"
        assert SENSEX.dhan_csv_symbol == "SENSEX"

    def test_indiavix_symbol_pair(self) -> None:
        """Critical pin: internal form has NO space, CSV form has ONE space."""
        assert INDIAVIX.symbol == "INDIAVIX"
        assert INDIAVIX.dhan_csv_symbol == "INDIA VIX"
        assert " " not in INDIAVIX.symbol
        assert " " in INDIAVIX.dhan_csv_symbol


class TestSpotInstrumentExchanges:
    """Per universe.py, SENSEX is the only BSE instrument."""

    def test_nifty_exchange_is_nse(self) -> None:
        assert NIFTY.exchange == "NSE"

    def test_banknifty_exchange_is_nse(self) -> None:
        assert BANKNIFTY.exchange == "NSE"

    def test_sensex_exchange_is_bse(self) -> None:
        """SENSEX trades on BSE, not NSE — matches universe.py:93."""
        assert SENSEX.exchange == "BSE"

    def test_indiavix_exchange_is_nse(self) -> None:
        assert INDIAVIX.exchange == "NSE"


class TestSpotInstrumentSegmentAndType:
    """All 4 Track 1 spot instruments share segment + instrument_type."""

    @pytest.mark.parametrize(
        "instrument",
        [NIFTY, BANKNIFTY, SENSEX, INDIAVIX],
    )
    def test_segment_is_idx_i(self, instrument: DhanSpotInstrument) -> None:
        assert instrument.segment == "IDX_I"

    @pytest.mark.parametrize(
        "instrument",
        [NIFTY, BANKNIFTY, SENSEX, INDIAVIX],
    )
    def test_instrument_type_is_index(self, instrument: DhanSpotInstrument) -> None:
        assert instrument.instrument_type == "INDEX"


class TestAllSpotInstrumentsTuple:
    """The 4-tuple is locked and immutable."""

    def test_has_exactly_four_entries(self) -> None:
        """CLAUDE.md §3: Track 1 scope is locked at 4 instruments."""
        assert len(ALL_SPOT_INSTRUMENTS) == 4

    def test_is_a_tuple_not_list(self) -> None:
        assert isinstance(ALL_SPOT_INSTRUMENTS, tuple)

    def test_order_matches_universe_v1_indices(self) -> None:
        """Order: NIFTY, BANKNIFTY, SENSEX, INDIAVIX — matches universe.py:79."""
        assert ALL_SPOT_INSTRUMENTS[0] is NIFTY
        assert ALL_SPOT_INSTRUMENTS[1] is BANKNIFTY
        assert ALL_SPOT_INSTRUMENTS[2] is SENSEX
        assert ALL_SPOT_INSTRUMENTS[3] is INDIAVIX

    def test_security_ids_are_unique(self) -> None:
        """No two instruments share a securityId — invariant for BY_SECURITY_ID."""
        ids = [inst.security_id for inst in ALL_SPOT_INSTRUMENTS]
        assert len(set(ids)) == len(ids)

    def test_internal_symbols_are_unique(self) -> None:
        symbols = [inst.symbol for inst in ALL_SPOT_INSTRUMENTS]
        assert len(set(symbols)) == len(symbols)


class TestReverseLookupTables:
    """BY_SECURITY_ID + BY_SYMBOL invariants."""

    def test_by_security_id_size(self) -> None:
        assert len(BY_SECURITY_ID) == 4

    def test_by_security_id_round_trip(self) -> None:
        for inst in ALL_SPOT_INSTRUMENTS:
            assert BY_SECURITY_ID[inst.security_id] is inst

    def test_by_symbol_size(self) -> None:
        assert len(BY_SYMBOL) == 4

    def test_by_symbol_round_trip(self) -> None:
        for inst in ALL_SPOT_INSTRUMENTS:
            assert BY_SYMBOL[inst.symbol] is inst

    def test_by_security_id_known_ids(self) -> None:
        assert BY_SECURITY_ID[13] is NIFTY
        assert BY_SECURITY_ID[25] is BANKNIFTY
        assert BY_SECURITY_ID[51] is SENSEX
        assert BY_SECURITY_ID[21] is INDIAVIX

    def test_by_symbol_known_symbols(self) -> None:
        assert BY_SYMBOL["NIFTY"] is NIFTY
        assert BY_SYMBOL["BANKNIFTY"] is BANKNIFTY
        assert BY_SYMBOL["SENSEX"] is SENSEX
        assert BY_SYMBOL["INDIAVIX"] is INDIAVIX

    def test_unknown_security_id_raises(self) -> None:
        with pytest.raises(KeyError):
            _ = BY_SECURITY_ID[999]

    def test_unknown_symbol_raises(self) -> None:
        with pytest.raises(KeyError):
            _ = BY_SYMBOL["UNKNOWN"]


class TestCoexistenceWithOptionsConstants:
    """The spot constants module shares names with options_constants.

    options_constants.py exports NIFTY / BANKNIFTY / SENSEX as
    ``DhanOptionsContract`` (Track 2 options). constants.py exports the
    same names as ``DhanSpotInstrument`` (Track 1 spot). Both live under
    ``brutex.adapters.dhan`` and callers disambiguate via full module
    path; this test guards against silent securityId drift between them.
    """

    def test_same_name_different_types_per_module(self) -> None:
        """Both modules export NIFTY but as distinct dataclass types."""
        from brutex.adapters.dhan import options_constants

        assert type(NIFTY).__name__ == "DhanSpotInstrument"
        assert type(options_constants.NIFTY).__name__ == "DhanOptionsContract"

    def test_spot_security_ids_match_options_spot_ids(self) -> None:
        """Per CLAUDE.md §6: Track 2 reuses SAME 4 securityIds — no per-strike CSV.

        If these diverge, both modules are out of sync with the verified
        Dhan facts table.
        """
        from brutex.adapters.dhan import options_constants

        assert NIFTY.security_id == options_constants.NIFTY.spot_security_id
        assert BANKNIFTY.security_id == options_constants.BANKNIFTY.spot_security_id
        assert SENSEX.security_id == options_constants.SENSEX.spot_security_id
