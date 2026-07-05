"""Unit coverage for :mod:`brutex.data.lake_keys` (DEC-LAKE-HEAL-001).

100% line + branch on ``brutex/data/lake_keys.py``. The #911 drift-recovery
ladder (DEC-COMPLETENESS-SPOT-KEY-RECOVER-001) moved VERBATIM from
``brutex/dashboard/completeness.py`` to its COMMON pure data-side home so the
lake scanner/healer reuse it without a ``data → dashboard`` import; the
byte-equivalence matrix at the bottom pins that the move is lossless (the
``completeness._resolve_spot_category`` wrapper delegates here).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import pytest
from brutex.data.lake_keys import (
    FNO_INDEX_ALIASES_ENV,
    fno_index_alias_pairs,
    resolve_spot_key,
    spot_key_candidates,
    spot_recovery_candidates,
)

_CATS: dict[str, str] = {
    "M&M": "equity",
    "BAJAJ-AUTO": "equity",
    "NIFTY": "index",
    "INDIA VIX": "reference",
}


class TestResolveSpotKey:
    def test_exact_key_resolves_first(self) -> None:
        assert resolve_spot_key("M&M", "NSE", _CATS) == ("M&M", "equity")

    def test_whitespace_drift_resolves(self) -> None:
        assert resolve_spot_key("M&M ", "NSE", _CATS) == ("M&M", "equity")
        assert resolve_spot_key("  M&M", "NSE", _CATS) == ("M&M", "equity")

    def test_exchange_prefix_drift_resolves(self) -> None:
        assert resolve_spot_key("NSE-M&M", "NSE", _CATS) == ("M&M", "equity")

    def test_whitespace_plus_prefix_drift_resolves(self) -> None:
        # Both ladder rungs in sequence: strip first, then the prefix class.
        assert resolve_spot_key("NSE-M&M ", "NSE", _CATS) == ("M&M", "equity")

    def test_hyphenated_symbol_never_mangled(self) -> None:
        # BAJAJ-AUTO does not start with "<exch>-", so the prefix class never fires.
        assert resolve_spot_key("BAJAJ-AUTO", "NSE", _CATS) == ("BAJAJ-AUTO", "equity")
        # A FOREIGN exchange prefix is NOT stripped — the ladder is data-driven
        # off the row's OWN exchange, never a hardcoded exchange list.
        assert resolve_spot_key("BSE-M&M", "NSE", _CATS) is None

    def test_interior_space_symbol_is_exact(self) -> None:
        assert resolve_spot_key("INDIA VIX", "NSE", _CATS) == ("INDIA VIX", "reference")

    def test_unknown_stray_returns_none(self) -> None:
        assert resolve_spot_key("FOOBAR", "NSE", _CATS) is None
        assert resolve_spot_key("NSE-FOOBAR", "NSE", _CATS) is None
        assert resolve_spot_key("FOOBAR ", "NSE", _CATS) is None

    def test_blank_exchange_skips_prefix_class(self) -> None:
        assert resolve_spot_key("NSE-M&M", "", _CATS) is None
        assert resolve_spot_key("NSE-M&M", "   ", _CATS) is None

    def test_none_value_reads_as_absent(self) -> None:
        # The #911 semantics generalised: a None VALUE is treated as missing.
        cats: dict[str, str | None] = {"M&M": None}
        assert resolve_spot_key("M&M", "NSE", cats) is None

    def test_generic_value_type_round_trips(self) -> None:
        canonical: dict[str, int] = {"NIFTY": 7}
        assert resolve_spot_key("NSE-NIFTY", "NSE", canonical) == ("NIFTY", 7)


class TestSpotKeyCandidates:
    def test_candidates_order_and_exclusion(self) -> None:
        # Ladder order: (1) whitespace-stripped, (2) prefix-stripped-off-stripped;
        # the identity is never a candidate.
        assert spot_key_candidates("NSE-M&M ", "NSE") == ("NSE-M&M", "M&M")
        assert spot_key_candidates("M&M ", "NSE") == ("M&M",)
        assert spot_key_candidates("NSE-M&M", "NSE") == ("M&M",)

    def test_clean_symbol_has_no_candidates(self) -> None:
        assert spot_key_candidates("M&M", "NSE") == ()
        assert spot_key_candidates("BAJAJ-AUTO", "NSE") == ()
        assert spot_key_candidates("INDIA VIX", "NSE") == ()

    def test_blank_exchange_skips_prefix_class(self) -> None:
        assert spot_key_candidates("NSE-M&M", "  ") == ()
        assert spot_key_candidates("NSE-M&M ", "") == ("NSE-M&M",)

    def test_empty_forms_are_dropped(self) -> None:
        # An all-whitespace symbol strips to "" (never a real key); the
        # pathological "<exch>-" dir strips to an empty candidate — both dropped.
        assert spot_key_candidates("   ", "NSE") == ()
        assert spot_key_candidates("NSE-", "NSE") == ()

    def test_foreign_exchange_prefix_not_a_candidate(self) -> None:
        assert spot_key_candidates("BSE-M&M", "NSE") == ()


class TestByteEquivalenceWithCompletenessWrapper:
    def test_byte_equivalence_with_completeness_wrapper(self) -> None:
        """The move is lossless: the #911 wrapper == the moved ladder on the full matrix."""
        from brutex.dashboard.completeness import _resolve_spot_category

        matrix = [
            ("M&M", "NSE"),
            ("M&M ", "NSE"),
            ("  M&M", "NSE"),
            ("NSE-M&M", "NSE"),
            ("NSE-M&M ", "NSE"),
            ("BAJAJ-AUTO", "NSE"),
            ("NSE-BAJAJ-AUTO", "NSE"),
            ("BSE-M&M", "NSE"),
            ("INDIA VIX", "NSE"),
            ("FOOBAR", "NSE"),
            ("NSE-FOOBAR", "NSE"),
            ("OTHER ", "NSE"),
            ("NSE-M&M", ""),
            ("NSE-M&M", "   "),
            ("NSE-", "NSE"),
            ("   ", "NSE"),
        ]
        cats = dict(_CATS)
        for symbol, exchange in matrix:
            assert _resolve_spot_category(symbol, exchange, cats) == resolve_spot_key(
                symbol, exchange, cats
            ), f"divergence for {symbol!r} / {exchange!r}"


class TestFnoIndexAliasPairs:
    """The sourced + env-override alias pairs (moved from ``greeks_enrich``)."""

    def test_sourced_defaults_when_env_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(FNO_INDEX_ALIASES_ENV, raising=False)
        pairs = dict(fno_index_alias_pairs())
        assert pairs == {"NIFTYJR": "NIFTYNXT50", "NIFTYMIDSELECT": "MIDCPNIFTY"}

    def test_blank_env_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FNO_INDEX_ALIASES_ENV, "")
        assert dict(fno_index_alias_pairs()) == {
            "NIFTYJR": "NIFTYNXT50",
            "NIFTYMIDSELECT": "MIDCPNIFTY",
        }

    def test_well_formed_override_extends_and_uppercases(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(FNO_INDEX_ALIASES_ENV, '{" foo ": " BAR "}')
        pairs = dict(fno_index_alias_pairs())
        assert pairs["FOO"] == "BAR"  # key stripped + uppercased, value stripped.
        assert pairs["NIFTYJR"] == "NIFTYNXT50"  # sourced defaults kept.

    @pytest.mark.parametrize("raw", ["not json", "[1, 2]", '"a string"'])
    def test_non_object_payload_is_ignored(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(FNO_INDEX_ALIASES_ENV, raw)
        assert dict(fno_index_alias_pairs()) == {
            "NIFTYJR": "NIFTYNXT50",
            "NIFTYMIDSELECT": "MIDCPNIFTY",
        }

    def test_malformed_entries_are_dropped_wellformed_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Non-str value / blank key / blank value all dropped; the one
        # well-formed pair survives (never a crash — a lost override only
        # costs the extra recovery rungs).
        monkeypatch.setenv(FNO_INDEX_ALIASES_ENV, '{"A": 3, "": "X", "B": "  ", "OK": "NIFTY"}')
        pairs = dict(fno_index_alias_pairs())
        assert pairs["OK"] == "NIFTY"
        assert "A" not in pairs and "B" not in pairs and "" not in pairs


class TestSpotRecoveryCandidates:
    """The FULL F&O→spot drift ladder (moved verbatim from ``greeks_enrich``)."""

    def test_sourced_reverse_and_forward_directions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(FNO_INDEX_ALIASES_ENV, raising=False)
        # Reverse: the chain names the F&O symbol, the spot dir the trading one.
        assert "NIFTYMIDSELECT" in spot_recovery_candidates("MIDCPNIFTY", "NSE")
        assert "NIFTYJR" in spot_recovery_candidates("NIFTYNXT50", "NSE")
        # Forward: the chain names the spot spelling, the dir the F&O one.
        assert "NIFTYNXT50" in spot_recovery_candidates("NIFTYJR", "NSE")

    def test_911_form_and_lake_keys_arm(self) -> None:
        candidates = spot_recovery_candidates("M&M", "NSE")
        assert "NSE-M&M" in candidates
        # A drifted-SHAPED underlying walks the spot_key_candidates ladder back down.
        assert "M&M" in spot_recovery_candidates("NSE-M&M", "NSE")

    def test_env_override_feeds_the_ladder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(FNO_INDEX_ALIASES_ENV, '{"SPOTKEY": "NIFTY"}')
        assert "SPOTKEY" in spot_recovery_candidates("NIFTY", "NSE")

    def test_dedup_and_identity_exclusion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Reverse candidate collides with the #911 form → appears exactly once.
        monkeypatch.setenv(FNO_INDEX_ALIASES_ENV, '{"NSE-FOO": "FOO", "BAZ": "BAZ"}')
        candidates = spot_recovery_candidates("FOO", "NSE")
        assert candidates.count("NSE-FOO") == 1
        # A self-alias never yields the identity as a candidate.
        assert "BAZ" not in spot_recovery_candidates("BAZ", "NSE")

    def test_greeks_enrich_alias_is_the_same_object(self) -> None:
        """The back-compat ``greeks_enrich._spot_recovery_candidates`` delegates here
        (one source of truth — DEC-DB-MONEYNESS-KEY-RECOVER-001)."""
        from brutex.data.greeks_enrich import _spot_recovery_candidates

        assert _spot_recovery_candidates is spot_recovery_candidates
