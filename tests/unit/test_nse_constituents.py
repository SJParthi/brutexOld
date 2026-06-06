"""Unit tests for :mod:`brutex.dashboard.nse_constituents`.

DEC-UNIVERSE-NSE-CONSTITUENTS-001 (MIGRATION.md #445). 100% line+branch
on the module EXCEPT the ``# pragma: no cover`` live urllib opener
(``_default_opener``) — NSE bot-blocks those endpoints and this build
environment network-blocks ``nseindia.com``, so the live fetch is
UNVERIFIABLE in CI. Every other path is exercised here with a PURE CSV
fixture + an injected fake opener (no network).
"""

from __future__ import annotations

import json

import pytest
from brutex.dashboard import nse_constituents as nc
from brutex.dashboard.nse_constituents import (
    INDEX_CSV_FILENAMES,
    NSE_CONSTITUENTS_BASE_URL,
    ConstituentRow,
    constituent_url,
    fetch_constituent_symbols,
    intersect_with_universe,
    members_in_universe,
    parse_constituents,
    parse_symbols,
)

# A realistic NSE constituent CSV (cols: Company Name, Industry, Symbol,
# Series, ISIN Code) including `&`-bearing symbols (M&M, GVT&D), a
# duplicate row (RELIANCE twice), a blank-symbol row, and trailing blank
# lines — every edge parse_symbols must survive.
_FIXTURE_CSV = (
    "Company Name,Industry,Symbol,Series,ISIN Code\n"
    "Reliance Industries Ltd.,Refineries,RELIANCE,EQ,INE002A01018\n"
    "Mahindra & Mahindra Ltd.,Automobile,M&M,EQ,INE101A01026\n"
    "GE Vernova T&D India Ltd.,Heavy Electrical Equipment,GVT&D,EQ,INE200A01026\n"
    "HDFC Bank Ltd.,Banks,HDFCBANK,EQ,INE040A01034\n"
    "Reliance Industries Ltd.,Refineries,RELIANCE,EQ,INE002A01018\n"
    ",,, ,\n"
    "\n"
    "\n"
)


class TestParseSymbols:
    def test_parses_symbol_column_dedup_order_preserved(self) -> None:
        assert parse_symbols(_FIXTURE_CSV) == [
            "RELIANCE",
            "M&M",
            "GVT&D",
            "HDFCBANK",
        ]

    def test_empty_text_returns_empty(self) -> None:
        assert parse_symbols("") == []

    def test_header_only_returns_empty(self) -> None:
        assert parse_symbols("Company Name,Industry,Symbol,Series,ISIN Code\n") == []

    def test_no_symbol_column_returns_empty(self) -> None:
        assert parse_symbols("Company Name,Industry,Series\nFoo,Bar,EQ\n") == []

    def test_header_whitespace_tolerated(self) -> None:
        # A " Symbol " header (leading/trailing space) still resolves.
        csv_text = "Company Name, Symbol ,Series\nReliance, RELIANCE ,EQ\n"
        assert parse_symbols(csv_text) == ["RELIANCE"]

    def test_lowercase_symbols_upcased(self) -> None:
        csv_text = "Company Name,Symbol,Series\nReliance,reliance,EQ\n"
        assert parse_symbols(csv_text) == ["RELIANCE"]

    def test_row_with_missing_symbol_field_skipped(self) -> None:
        # A short row (no Symbol value) → DictReader yields None for the key.
        csv_text = "Company Name,Symbol,Series\nReliance\nHDFC,HDFCBANK,EQ\n"
        assert parse_symbols(csv_text) == ["HDFCBANK"]


class TestParseConstituents:
    """DEC-NIFTY-TOTAL-MARKET-MAP-001 — symbol + ISIN parsing."""

    def test_parses_symbol_and_isin(self) -> None:
        rows = parse_constituents(_FIXTURE_CSV)
        assert rows[0] == ConstituentRow(symbol="RELIANCE", isin="INE002A01018")
        assert rows[1] == ConstituentRow(symbol="M&M", isin="INE101A01026")
        assert [r.symbol for r in rows] == ["RELIANCE", "M&M", "GVT&D", "HDFCBANK"]

    def test_dedup_by_symbol_first_seen_wins(self) -> None:
        # RELIANCE appears twice in the fixture; first row's ISIN is kept.
        rows = parse_constituents(_FIXTURE_CSV)
        reliance = [r for r in rows if r.symbol == "RELIANCE"]
        assert len(reliance) == 1

    def test_isin_header_variant_plain_isin(self) -> None:
        csv_text = "Symbol,ISIN\nRELIANCE,INE002A01018\n"
        assert parse_constituents(csv_text) == [ConstituentRow("RELIANCE", "INE002A01018")]

    def test_missing_isin_column_yields_none(self) -> None:
        csv_text = "Company Name,Symbol,Series\nReliance,RELIANCE,EQ\n"
        assert parse_constituents(csv_text) == [ConstituentRow("RELIANCE", None)]

    def test_blank_isin_value_yields_none(self) -> None:
        csv_text = "Symbol,ISIN Code\nRELIANCE, \n"
        assert parse_constituents(csv_text) == [ConstituentRow("RELIANCE", None)]

    def test_isin_upcased_and_trimmed(self) -> None:
        csv_text = "Symbol,ISIN Code\nRELIANCE, ine002a01018 \n"
        assert parse_constituents(csv_text) == [ConstituentRow("RELIANCE", "INE002A01018")]

    def test_empty_and_no_symbol_column(self) -> None:
        assert parse_constituents("") == []
        assert parse_constituents("Company Name,Industry\nFoo,Bar\n") == []

    def test_parse_symbols_delegates_identically(self) -> None:
        # parse_symbols is now a thin wrapper — must match the symbols exactly.
        assert parse_symbols(_FIXTURE_CSV) == [r.symbol for r in parse_constituents(_FIXTURE_CSV)]


class TestConstituentUrl:
    def test_known_index_builds_full_url(self) -> None:
        assert constituent_url("NIFTY") == (f"{NSE_CONSTITUENTS_BASE_URL}/ind_nifty50list.csv")

    def test_case_insensitive_and_trimmed(self) -> None:
        assert constituent_url("  banknifty  ") == (
            f"{NSE_CONSTITUENTS_BASE_URL}/ind_niftybanklist.csv"
        )

    def test_alias_resolves(self) -> None:
        assert constituent_url("NIFTY NEXT 50") == constituent_url("NIFTYNXT50")

    def test_unknown_index_returns_none(self) -> None:
        assert constituent_url("NIFTY IT") is None

    def test_nifty_total_market_aliases_resolve(self) -> None:
        # DEC-NIFTY-TOTAL-MARKET-URL-001: the ~750-name basket selector resolves
        # to the OPERATOR-VERIFIED full URL on the NSE-Indices host (a DIFFERENT
        # host from NSE_CONSTITUENTS_BASE_URL), used verbatim — NOT joined to the
        # base. All three spellings (display, bare, underscore) resolve the same,
        # case-insensitive + trimmed.
        expected = "https://www.niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv"
        assert constituent_url("NIFTY TOTAL MARKET") == expected
        assert constituent_url("  niftytotalmarket ") == expected
        assert constituent_url("NIFTY_TOTAL_MARKET") == expected
        # It is a full URL on its own host — NOT prefixed with the NSE base.
        assert not expected.startswith(NSE_CONSTITUENTS_BASE_URL)

    def test_nifty_total_market_is_registered(self) -> None:
        # The basket selector must be present in the built-in map so the
        # dashboard offers it without an operator override.
        assert "NIFTY TOTAL MARKET" in INDEX_CSV_FILENAMES

    @pytest.mark.parametrize("symbol", sorted(INDEX_CSV_FILENAMES))
    def test_every_mapped_index_resolves(self, symbol: str) -> None:
        assert constituent_url(symbol) is not None

    @pytest.mark.parametrize(
        ("symbol", "filename"),
        [
            ("NIFTY 100", "ind_nifty100list.csv"),
            ("NIFTY100", "ind_nifty100list.csv"),
            ("NIFTY 200", "ind_nifty200list.csv"),
            ("NIFTY 500", "ind_nifty500list.csv"),
        ],
    )
    def test_broad_market_baskets_resolve(self, symbol: str, filename: str) -> None:
        """NIFTY 100/200/500 broad-market baskets map to their NSE CSVs
        (DEC-CONSTITUENTS-CACHE-001)."""
        assert constituent_url(symbol) == f"{NSE_CONSTITUENTS_BASE_URL}/{filename}"


class TestFetchConstituentSymbols:
    def test_success_with_injected_opener(self) -> None:
        captured: dict[str, str] = {}

        def fake_opener(url: str) -> str:
            captured["url"] = url
            return _FIXTURE_CSV

        out = fetch_constituent_symbols("NIFTY", opener=fake_opener)
        assert out == ["RELIANCE", "M&M", "GVT&D", "HDFCBANK"]
        assert captured["url"].endswith("/ind_nifty50list.csv")

    def test_unknown_index_returns_empty_without_calling_opener(self) -> None:
        def boom(url: str) -> str:  # pragma: no cover - must NOT be called
            raise AssertionError("opener should not be called for unknown index")

        assert fetch_constituent_symbols("NIFTY IT", opener=boom) == []

    def test_opener_error_propagates(self) -> None:
        def failing(url: str) -> str:
            raise OSError("network blocked")

        with pytest.raises(OSError, match="network blocked"):
            fetch_constituent_symbols("NIFTY", opener=failing)


class TestMembersInUniverse:
    def _opener(self, _url: str) -> str:
        return _FIXTURE_CSV

    def test_intersection_case_insensitive_order_preserved(self) -> None:
        # We hold reliance (lower), HDFCBANK, M&M (held casing differs) + a
        # non-member (INFY). Result keeps INDEX order + HELD casing.
        held = ["reliance", "HDFCBANK", "m&m", "INFY"]
        out = members_in_universe("NIFTY", held, opener=self._opener)
        assert out == ["reliance", "m&m", "HDFCBANK"]

    def test_non_members_dropped(self) -> None:
        # GVT&D is in the index but NOT held → dropped.
        held = ["RELIANCE"]
        out = members_in_universe("NIFTY", held, opener=self._opener)
        assert out == ["RELIANCE"]

    def test_unknown_index_returns_empty(self) -> None:
        out = members_in_universe("NIFTY IT", ["RELIANCE"], opener=self._opener)
        assert out == []

    def test_blank_held_symbols_ignored(self) -> None:
        # Blank / whitespace-only held symbols are skipped (no key collision).
        held = ["", "   ", "RELIANCE"]
        out = members_in_universe("NIFTY", held, opener=self._opener)
        assert out == ["RELIANCE"]

    def test_duplicate_held_first_casing_wins(self) -> None:
        held = ["Reliance", "RELIANCE"]
        out = members_in_universe("NIFTY", held, opener=self._opener)
        assert out == ["Reliance"]

    def test_no_eligible_constituents_returns_empty(self) -> None:
        out = members_in_universe("NIFTY", ["NOTINDEX"], opener=self._opener)
        assert out == []


class TestIntersectWithUniverse:
    """The pure intersection helper shared by the live + cache paths (COMMON)."""

    def test_keeps_held_casing_and_constituent_order(self) -> None:
        # Constituents arrive upper-cased (from a fetch or the cache); the held
        # symbols carry the casing the dashboard renders. Output = held casing,
        # ordered by the constituent order, non-members dropped.
        constituents = ["HDFCBANK", "RELIANCE", "ZZZNOTHELD"]
        held = ["reliance", "HDFCBANK", "m&m"]
        assert intersect_with_universe(constituents, held) == ["HDFCBANK", "reliance"]

    def test_blank_held_skipped_and_first_casing_wins(self) -> None:
        assert intersect_with_universe(["RELIANCE"], ["", "  ", "Reliance", "RELIANCE"]) == [
            "Reliance"
        ]

    def test_empty_inputs_return_empty(self) -> None:
        assert intersect_with_universe([], ["RELIANCE"]) == []
        assert intersect_with_universe(["RELIANCE"], []) == []


def test_module_exports_are_public() -> None:
    for name in nc.__all__:
        assert hasattr(nc, name)


class TestConstituentUrlOverride:
    """DEC-CONSTITUENTS-OVERRIDE-001: the env override makes ANY index pickable.

    The operator supplies VERIFIED NSE filenames / BSE full-URLs (this build env
    403-bot-blocks NSE, so we never hardcode unverified names — Golden Rule #1).
    """

    _ENV = nc.INDEX_CSV_FILENAMES_OVERRIDE_ENV

    def test_override_adds_a_new_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # "NIFTY IT" has no built-in mapping (returns None) — the override adds it.
        assert constituent_url("NIFTY IT") is None
        monkeypatch.setenv(self._ENV, json.dumps({"NIFTY IT": "ind_niftyitlist.csv"}))
        assert constituent_url("NIFTY IT") == f"{NSE_CONSTITUENTS_BASE_URL}/ind_niftyitlist.csv"

    def test_override_full_url_used_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A BSE index points at a full URL on another host → used as-is, not joined.
        url = "https://bse.example/indices/sensex.csv"
        monkeypatch.setenv(self._ENV, json.dumps({"SENSEX": url}))
        assert constituent_url("SENSEX") == url

    def test_override_wins_over_builtin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A renamed CSV can be corrected in place — the override beats the built-in.
        monkeypatch.setenv(self._ENV, json.dumps({"NIFTY": "ind_nifty50_renamed.csv"}))
        assert constituent_url("NIFTY") == f"{NSE_CONSTITUENTS_BASE_URL}/ind_nifty50_renamed.csv"

    def test_override_key_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(self._ENV, json.dumps({"nifty it": "ind_niftyitlist.csv"}))
        assert constituent_url("NIFTY IT") == f"{NSE_CONSTITUENTS_BASE_URL}/ind_niftyitlist.csv"

    def test_no_override_builtin_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(self._ENV, raising=False)
        assert constituent_url("NIFTY") == f"{NSE_CONSTITUENTS_BASE_URL}/ind_nifty50list.csv"
        assert constituent_url("NIFTY IT") is None

    def test_malformed_json_degrades_to_builtins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(self._ENV, "not json {{{")
        assert nc._override_filenames() == {}
        assert constituent_url("NIFTY") == f"{NSE_CONSTITUENTS_BASE_URL}/ind_nifty50list.csv"
        assert constituent_url("NIFTY IT") is None

    def test_non_object_json_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(self._ENV, json.dumps([1, 2, 3]))  # valid JSON, not a dict
        assert nc._override_filenames() == {}

    def test_blank_and_non_string_entries_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            self._ENV,
            json.dumps({"GOOD": "f.csv", "BAD": "", "   ": "x.csv", "NUM": 123, "K": None}),
        )
        assert nc._override_filenames() == {"GOOD": "f.csv"}

    def test_empty_env_value_is_no_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(self._ENV, "")
        assert nc._override_filenames() == {}
