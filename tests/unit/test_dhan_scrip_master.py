"""Tests for :mod:`brutex.adapters.dhan.scrip_master` — hardcoded-map resolution, honest omit."""

from __future__ import annotations

from brutex.adapters.dhan.constants import DHAN_SPOT_INDEX_SYMBOLS
from brutex.adapters.dhan.scrip_master import ScripResolution, resolve_security_ids


def test_hardcoded_map_resolves_verified_ids() -> None:
    result = resolve_security_ids(DHAN_SPOT_INDEX_SYMBOLS)
    assert result.security_ids == {"NIFTY": 13, "BANKNIFTY": 25, "SENSEX": 51, "INDIAVIX": 21}
    assert result.unresolved == ()


def test_explicit_map_override_resolves_and_omits() -> None:
    result = resolve_security_ids(("NIFTY", "SENSEX"), security_id_map={"NIFTY": 99})
    assert result.security_ids == {"NIFTY": 99}
    assert result.unresolved == ("SENSEX",)  # absent from the map → honest omission


def test_unresolvable_symbol_is_honestly_omitted() -> None:
    result = resolve_security_ids(("NIFTY", "MYSTERYINDEX"))
    assert result.security_ids == {"NIFTY": 13}
    assert result.unresolved == ("MYSTERYINDEX",)  # never a blind id


def test_scrip_resolution_is_frozen_dataclass() -> None:
    result = ScripResolution(security_ids={}, unresolved=())
    assert result.security_ids == {}
    assert result.unresolved == ()
