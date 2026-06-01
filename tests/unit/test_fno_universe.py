"""Tests for `brutex.data.fno_universe` — F&O universe reconciliation transforms.

100% line+branch of the pure parser/cross-check/serialiser, plus a golden check
against the operator's REAL NSE ``fo_mktlots.csv`` (5 index underlyings + 211
single-stock underlyings — DEC-FNO-UNIVERSE-BUILDER-001).
"""

from __future__ import annotations

from pathlib import Path

from brutex.data.fno_universe import (
    OUTPUT_COLUMNS,
    FnoUnderlying,
    common_symbols,
    cross_check,
    parse_fo_mktlots,
    to_csv,
)

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "fo_mktlots_real.csv"

# A tiny, space-padded slice mirroring the real fo_mktlots layout.
_SAMPLE = (
    "UNDERLYING                          ,SYMBOL    ,JUN-26     ,JUL-26     \n"
    "NIFTY 50                            ,NIFTY     ,65         ,65         \n"
    "NIFTY BANK                          ,BANKNIFTY ,30         ,30         \n"
    "Derivatives on Individual Securities,Symbol    ,JUN-26     ,JUL-26     \n"
    "RELIANCE INDUSTRIES LTD             ,RELIANCE  ,500        ,500        \n"
    "BAJAJ HOLDINGS & INVS LTD           ,BAJAJHLDNG,50         ,75         \n"  # near=50
    "ONLY NEXT MONTH LTD                 ,ONLYNEXT  ,           ,300        \n"  # near empty → 300
)


class TestParseFoMktLots:
    def test_splits_index_and_stock_sections(self) -> None:
        m = parse_fo_mktlots(_SAMPLE)
        assert m.index_lots == {"NIFTY": 65, "BANKNIFTY": 30}
        assert m.stock_lots == {"RELIANCE": 500, "BAJAJHLDNG": 50, "ONLYNEXT": 300}

    def test_header_blank_and_lotless_rows_skipped(self) -> None:
        text = (
            "UNDERLYING,SYMBOL,JUN-26\n"  # header → skipped (SYMBOL)
            "\n"  # blank (len<3) → skipped
            ",,\n"  # empty underlying+symbol → skipped
            "NO LOTS LTD,NOLOT,,\n"  # no parseable lot → skipped
            "Derivatives on Individual Securities,Symbol,JUN-26\n"
            "JUNK LOT LTD,JUNK,abc,125\n"  # first cell non-int → _first_lot None → skipped
            "GOOD LTD,GOOD,125,125\n"
        )
        m = parse_fo_mktlots(text)
        assert m.index_lots == {}
        assert m.stock_lots == {"GOOD": 125}

    def test_empty_input(self) -> None:
        m = parse_fo_mktlots("")
        assert m.index_lots == {} and m.stock_lots == {}


class TestRealFixtureGolden:
    def test_real_fo_mktlots_counts(self) -> None:
        m = parse_fo_mktlots(_FIXTURE.read_text())
        # The locked F&O universe: 5 index underlyings + 211 single stocks.
        assert sorted(m.index_lots) == [
            "BANKNIFTY",
            "FINNIFTY",
            "MIDCPNIFTY",
            "NIFTY",
            "NIFTYNXT50",
        ]
        assert len(m.stock_lots) == 211
        assert m.stock_lots["RELIANCE"] == 500
        assert m.index_lots["NIFTY"] == 65


class TestCrossCheck:
    def test_only_in_each_source(self) -> None:
        result = cross_check(
            {
                "nse": {"A", "B", "C"},
                "mktlots": {"B", "C", "D"},
                "dhan": {"C", "D", "E"},
            }
        )
        assert result == {"nse": {"A"}, "mktlots": set(), "dhan": {"E"}}

    def test_all_agree_no_extras(self) -> None:
        result = cross_check({"x": {"A", "B"}, "y": {"A", "B"}})
        assert result == {"x": set(), "y": set()}


class TestCommonSymbols:
    def test_intersection(self) -> None:
        assert common_symbols({"a": {"A", "B", "C"}, "b": {"B", "C", "D"}}) == {"B", "C"}

    def test_no_sources_is_empty(self) -> None:
        assert common_symbols({}) == set()


class TestToCsv:
    def test_header_and_sorted_rows(self) -> None:
        rows = [
            FnoUnderlying("RELIANCE", 2885, "OPTSTK", "NSE", 500),
            FnoUnderlying("SENSEX", 51, "OPTIDX", "BSE", 20),
            FnoUnderlying("NIFTY", 13, "OPTIDX", "NSE", 65),
        ]
        text = to_csv(rows, source="dhan+nse", fetched_at_utc="2026-06-01T00:00:00Z")
        lines = text.strip().splitlines()
        assert lines[0] == ",".join(OUTPUT_COLUMNS)
        # Sorted by (exchange, symbol): BSE-SENSEX, then NSE-NIFTY, NSE-RELIANCE.
        assert lines[1].startswith("SENSEX,51,OPTIDX,BSE,20,dhan+nse,2026-06-01T00:00:00Z")
        assert lines[2].startswith("NIFTY,13,OPTIDX,NSE,65")
        assert lines[3].startswith("RELIANCE,2885,OPTSTK,NSE,500")
