"""Tests for :mod:`brutex.adapters.dhan.scrip_master` — resolution, fallback, honest omit."""

from __future__ import annotations

from collections.abc import Mapping

from brutex.adapters.dhan.constants import DHAN_SPOT_INDEX_SYMBOLS
from brutex.adapters.dhan.scrip_master import ScripResolution, resolve_security_ids


def test_fallback_only_resolves_verified_ids() -> None:
    result = resolve_security_ids(DHAN_SPOT_INDEX_SYMBOLS)
    assert result.security_ids == {"NIFTY": 13, "BANKNIFTY": 25, "SENSEX": 51, "INDIAVIX": 21}
    assert result.unresolved == ()
    assert result.used_live is False


def test_live_wins_over_fallback() -> None:
    def live() -> Mapping[str, int]:
        return {"NIFTY": 13, "BANKNIFTY": 25, "SENSEX": 51, "INDIAVIX": 21}

    result = resolve_security_ids(DHAN_SPOT_INDEX_SYMBOLS, live_resolver=live)
    assert result.used_live is True
    assert result.security_ids["NIFTY"] == 13


def test_live_partial_falls_back_per_symbol() -> None:
    def live() -> Mapping[str, int]:
        return {"NIFTY": 13}  # only NIFTY live; rest via fallback

    result = resolve_security_ids(("NIFTY", "SENSEX"), live_resolver=live)
    assert result.security_ids == {"NIFTY": 13, "SENSEX": 51}
    assert result.used_live is True


def test_live_fetch_failure_degrades_to_fallback() -> None:
    def live() -> Mapping[str, int]:
        raise RuntimeError("scrip master 503")

    result = resolve_security_ids(("NIFTY",), live_resolver=live)
    assert result.security_ids == {"NIFTY": 13}
    assert result.used_live is False  # the fetch failed → fallback took over


def test_unresolvable_symbol_is_honestly_omitted() -> None:
    result = resolve_security_ids(("NIFTY", "MYSTERYINDEX"))
    assert result.security_ids == {"NIFTY": 13}
    assert result.unresolved == ("MYSTERYINDEX",)  # never a blind id


def test_scrip_resolution_is_frozen_dataclass() -> None:
    result = ScripResolution(security_ids={}, unresolved=(), used_live=False)
    assert result.security_ids == {}
