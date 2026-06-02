"""Tests for `brutex.adapters.dhan.instruments` — CSV row → Instrument + universe.

100% line+branch: header validation, every numeric/decimal/date parse branch
(via crafted rows), instrument-type mapping, index/equity/option parsing,
exchange + security-id guards, F&O-eligible-set collection, and the two-pass
universe selection.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from brutex.adapters.dhan.instruments import (
    DhanInstrumentError,
    fno_underlying_security_ids,
    fno_underlying_symbols,
    parse_instrument_row,
    select_universe,
    select_universe_report,
    validate_header,
)
from brutex.models import Exchange, InstrumentType, Segment


def _row(**overrides: str) -> dict[str, str]:
    base = {
        "EXCH_ID": "NSE",
        "SEGMENT": "E",
        "SECURITY_ID": "13",
        "ISIN": "INE000A01010",
        # The detailed CSV carries BOTH: INSTRUMENT (long vocab — what we
        # classify on) and INSTRUMENT_TYPE (short code — present but unused;
        # set realistically so tests prove classification ignores it).
        "INSTRUMENT": "EQUITY",
        "INSTRUMENT_TYPE": "ES",
        "UNDERLYING_SECURITY_ID": "",
        "UNDERLYING_SYMBOL": "",
        "SYMBOL_NAME": "RELIANCE",
        "DISPLAY_NAME": "Reliance Industries",
        "SERIES": "EQ",
        "LOT_SIZE": "1",
        "SM_EXPIRY_DATE": "",
        "STRIKE_PRICE": "",
        "OPTION_TYPE": "",
        "TICK_SIZE": "0.05",
        "EXPIRY_FLAG": "",
        "ASM_GSM_FLAG": "",
        "SM_FREEZE_QTY": "",
    }
    base.update(overrides)
    return base


class TestValidateHeader:
    def test_ok(self) -> None:
        validate_header(list(_row().keys()))  # no raise

    def test_missing_raises(self) -> None:
        with pytest.raises(DhanInstrumentError, match="missing required columns"):
            validate_header(["EXCH_ID", "SECURITY_ID"])

    def test_none_header_raises(self) -> None:
        with pytest.raises(DhanInstrumentError, match="missing required columns"):
            validate_header(None)


class TestParseRow:
    def test_equity_spot(self) -> None:
        inst = parse_instrument_row(_row(SM_FREEZE_QTY="100000"))
        assert inst.exch_id is Exchange.NSE
        assert inst.security_id == 13  # raw Dhan SECURITY_ID (identity = security_id + exch_id)
        assert inst.symbol_name == "RELIANCE"
        assert inst.instrument is InstrumentType.EQ
        assert inst.segment is Segment.CASH
        assert inst.lot_size == 1
        assert inst.tick_size == Decimal("0.05")
        assert inst.sm_freeze_qty == 100000

    def test_index_zero_lot_tick(self) -> None:
        inst = parse_instrument_row(
            _row(INSTRUMENT="INDEX", SYMBOL_NAME="NIFTY", LOT_SIZE="", TICK_SIZE="")
        )
        assert inst.instrument is InstrumentType.IDX
        assert inst.lot_size == 0
        assert inst.tick_size == Decimal("0")
        assert inst.sm_freeze_qty is None

    def test_option_row(self) -> None:
        inst = parse_instrument_row(
            _row(
                EXCH_ID="NSE",
                INSTRUMENT="OPTIDX",
                OPTION_TYPE="CE",
                SYMBOL_NAME="NIFTY",
                UNDERLYING_SYMBOL="NIFTY",
                UNDERLYING_SECURITY_ID="13",
                SM_EXPIRY_DATE="2025-01-02",
                STRIKE_PRICE="28500",
                LOT_SIZE="50",
                TICK_SIZE="0.05",
            )
        )
        assert inst.instrument is InstrumentType.CE
        assert inst.segment is Segment.FNO
        assert inst.sm_expiry_date == date(2025, 1, 2)
        assert inst.strike_price == Decimal("28500")
        assert inst.underlying_security_id == 13

    def test_put_option(self) -> None:
        inst = parse_instrument_row(
            _row(
                INSTRUMENT="OPTSTK",
                OPTION_TYPE="PE",
                STRIKE_PRICE="100",
                UNDERLYING_SYMBOL="RELIANCE",
                SM_EXPIRY_DATE="2025-01-30",
                LOT_SIZE="250",
            )
        )
        assert inst.instrument is InstrumentType.PE

    def test_future_row(self) -> None:
        inst = parse_instrument_row(
            _row(
                INSTRUMENT="FUTSTK",
                SM_EXPIRY_DATE="2025-01-30 14:30:00",
                UNDERLYING_SYMBOL="RELIANCE",
                LOT_SIZE="250",
            )
        )
        assert inst.instrument is InstrumentType.FUT
        assert inst.segment is Segment.FNO
        assert inst.sm_expiry_date == date(2025, 1, 30)

    def test_lot_size_accepts_float_string(self) -> None:
        assert parse_instrument_row(_row(LOT_SIZE="50.0")).lot_size == 50

    def test_identity_is_security_id_plus_exch_id(self) -> None:
        # Regression for the live "duplicate key 13" crash (ADR-0003 successor
        # to DEC-DHAN-VENDOR-SYMBOL-UNIQUE-001): Dhan SECURITY_ID is unique only
        # within an exchange-segment, so an NSE row and a BSE row can share the
        # same numeric id. Identity is the (security_id, exch_id) PAIR, so the
        # shared id is disambiguated by exchange — no PRIMARY KEY collision.
        nse = parse_instrument_row(
            _row(INSTRUMENT="INDEX", EXCH_ID="NSE", SYMBOL_NAME="NIFTY", SECURITY_ID="13")
        )
        bse = parse_instrument_row(
            _row(INSTRUMENT="INDEX", EXCH_ID="BSE", SYMBOL_NAME="SENSEX", SECURITY_ID="13")
        )
        # Same raw SECURITY_ID on both exchanges …
        assert nse.security_id == bse.security_id == 13
        # … but DISTINCT (security_id, exch_id) identities → no PK collision.
        assert (nse.security_id, nse.exch_id) == (13, Exchange.NSE)
        assert (bse.security_id, bse.exch_id) == (13, Exchange.BSE)
        assert (nse.security_id, nse.exch_id) != (bse.security_id, bse.exch_id)

    def test_reserved_from_asm_flag(self) -> None:
        assert parse_instrument_row(_row(ASM_GSM_FLAG="ASM")).asm_gsm_flag is True
        assert parse_instrument_row(_row(ASM_GSM_FLAG="")).asm_gsm_flag is False

    def test_absent_optional_columns_become_none(self) -> None:
        row = _row()
        del row["SERIES"]
        del row["DISPLAY_NAME"]
        inst = parse_instrument_row(row)
        assert inst.series is None
        assert inst.display_name is None

    @pytest.mark.parametrize(
        ("overrides", "match"),
        [
            ({"EXCH_ID": "MCX"}, "out-of-scope exchange"),
            ({"SECURITY_ID": ""}, "missing SECURITY_ID"),
            ({"INSTRUMENT": "WEIRD"}, "unknown INSTRUMENT"),
            ({"INSTRUMENT": "OPTIDX", "OPTION_TYPE": "XX"}, "invalid OPTION_TYPE"),
            ({"LOT_SIZE": "abc"}, "unparseable LOT_SIZE"),
            ({"TICK_SIZE": "xyz"}, "unparseable TICK_SIZE"),
            ({"TICK_SIZE": "inf"}, "non-finite TICK_SIZE"),
            ({"SM_EXPIRY_DATE": "not-a-date"}, "unparseable SM_EXPIRY_DATE"),
        ],
    )
    def test_invalid_rows_raise(self, overrides: dict[str, str], match: str) -> None:
        with pytest.raises(DhanInstrumentError, match=match):
            parse_instrument_row(_row(**overrides))


class TestUniverse:
    def _rows(self) -> list[dict[str, str]]:
        return [
            _row(
                INSTRUMENT="INDEX",
                SYMBOL_NAME="NIFTY",
                SECURITY_ID="13",
                LOT_SIZE="",
                TICK_SIZE="",
            ),
            _row(
                EXCH_ID="BSE",
                INSTRUMENT="INDEX",
                SYMBOL_NAME="SENSEX",
                SECURITY_ID="51",
                LOT_SIZE="",
                TICK_SIZE="",
            ),
            # F&O-eligible equity: real CSV carries the LONG company name in
            # SYMBOL_NAME and the TICKER in UNDERLYING_SYMBOL — the join must
            # key on the ticker (DEC-DHAN-FNO-JOIN-KEY-001), and the admitted
            # spot's trading_symbol resolves to that ticker.
            _row(
                INSTRUMENT="EQUITY",
                SYMBOL_NAME="RELIANCE INDUSTRIES LTD",
                UNDERLYING_SYMBOL="RELIANCE",
                SECURITY_ID="2885",
            ),
            _row(
                INSTRUMENT="EQUITY", SYMBOL_NAME="TINY CO LTD", SECURITY_ID="9999"
            ),  # NOT F&O (no UNDERLYING_SYMBOL)
            _row(
                INSTRUMENT="OPTSTK",
                OPTION_TYPE="CE",
                SYMBOL_NAME="RELIANCE-OPT",
                UNDERLYING_SYMBOL="RELIANCE",
                SECURITY_ID="3001",
                STRIKE_PRICE="2500",
                SM_EXPIRY_DATE="2025-01-30",
            ),
            _row(
                EXCH_ID="MCX", INSTRUMENT="FUTCOM", SYMBOL_NAME="GOLD", SECURITY_ID="7777"
            ),  # skipped
        ]

    def test_fno_underlying_set(self) -> None:
        assert fno_underlying_symbols(self._rows()) == frozenset({"RELIANCE"})

    def test_select_universe_keeps_indices_and_fno_spot(self) -> None:
        universe = select_universe(self._rows())
        symbols = sorted(i.symbol_name for i in universe)
        assert symbols == ["NIFTY", "RELIANCE", "SENSEX"]
        # Non-F&O equity dropped; option legs dropped; MCX skipped.
        assert all(i.instrument in {InstrumentType.IDX, InstrumentType.EQ} for i in universe)

    def test_select_universe_report_accounting(self) -> None:
        report = select_universe_report(self._rows())
        # In-scope NSE/BSE rows: NIFTY, SENSEX, RELIANCE, TINYCO, RELIANCE-OPT.
        assert report.parsed == 5
        # Persisted universe: NIFTY, SENSEX, RELIANCE.
        assert report.persisted == 3
        assert sorted(i.symbol_name for i in report.universe) == ["NIFTY", "RELIANCE", "SENSEX"]
        # Dropped in-scope: TINYCO (non-F&O equity) + RELIANCE-OPT (option leg).
        assert report.legs_dropped == 2
        # Out-of-scope: the MCX GOLD future.
        assert report.filtered == 1
        # Accounting invariant: every in-scope row is either persisted or dropped.
        assert report.parsed == report.persisted + report.legs_dropped


class TestClassifyOffInstrumentColumn:
    """Regression for the "120 indices / 1 stock" bug (DEC-DHAN-INSTRUMENT-CLASSIFY-001).

    The real Dhan detailed CSV carries the long vocabulary in **INSTRUMENT**
    (EQUITY / INDEX / OPTSTK / FUTSTK …) and short codes in **INSTRUMENT_TYPE**
    (ES / OP / FUT …). Classification MUST read INSTRUMENT — reading
    INSTRUMENT_TYPE left every option/future leg unmatched, the F&O-eligible set
    empty, and so admitted ~0 stocks while every INDEX row still counted.
    """

    def _real_shape_rows(self) -> list[dict[str, str]]:
        # Derivative legs carry the REAL split: INSTRUMENT=OPTSTK/FUTSTK but
        # INSTRUMENT_TYPE=OP/FUT (the codes that fooled the old parser).
        return [
            _row(
                INSTRUMENT="INDEX", INSTRUMENT_TYPE="INDEX", SYMBOL_NAME="NIFTY", SECURITY_ID="13"
            ),
            # Real namespace split: equity SYMBOL_NAME = long company name,
            # UNDERLYING_SYMBOL = ticker (the join key + trading_symbol source).
            _row(
                INSTRUMENT="EQUITY",
                INSTRUMENT_TYPE="ES",
                SYMBOL_NAME="RELIANCE INDUSTRIES LTD",
                UNDERLYING_SYMBOL="RELIANCE",
                SECURITY_ID="2885",
            ),
            _row(
                INSTRUMENT="EQUITY",
                INSTRUMENT_TYPE="ES",
                SYMBOL_NAME="TATA CONSULTANCY SERVICES LTD",
                UNDERLYING_SYMBOL="TCS",
                SECURITY_ID="11536",
            ),
            _row(
                INSTRUMENT="EQUITY",
                INSTRUMENT_TYPE="ES",
                SYMBOL_NAME="NO F&O LTD",
                SECURITY_ID="9999",
            ),
            _row(
                INSTRUMENT="OPTSTK",
                INSTRUMENT_TYPE="OP",
                OPTION_TYPE="CE",
                SYMBOL_NAME="RELIANCE26JAN2800CE",
                UNDERLYING_SYMBOL="RELIANCE",
                UNDERLYING_SECURITY_ID="2885",
                SECURITY_ID="40001",
                STRIKE_PRICE="2800",
                SM_EXPIRY_DATE="2026-01-29",
            ),
            _row(
                INSTRUMENT="FUTSTK",
                INSTRUMENT_TYPE="FUT",
                SYMBOL_NAME="TCS26JANFUT",
                UNDERLYING_SYMBOL="TCS",
                UNDERLYING_SECURITY_ID="11536",
                SECURITY_ID="40002",
                SM_EXPIRY_DATE="2026-01-29",
            ),
        ]

    def test_fno_set_reads_instrument_not_instrument_type(self) -> None:
        # The old code keyed on INSTRUMENT_TYPE (OP/FUT) → empty set. Now it
        # reads INSTRUMENT (OPTSTK/FUTSTK) → both underlyings resolve.
        assert fno_underlying_symbols(self._real_shape_rows()) == frozenset({"RELIANCE", "TCS"})

    def test_universe_admits_the_fno_stocks(self) -> None:
        report = select_universe_report(self._real_shape_rows())
        symbols = sorted(i.symbol_name for i in report.universe)
        # 1 index + 2 F&O stocks admitted (NOT "1 stock"); NOFNO + 2 legs dropped.
        # trading_symbol resolves to the TICKER (UNDERLYING_SYMBOL), not the
        # long SYMBOL_NAME ("RELIANCE INDUSTRIES LTD").
        assert symbols == ["NIFTY", "RELIANCE", "TCS"]
        assert report.persisted == 3
        assert report.legs_dropped == 3  # NOFNO equity + OPTSTK + FUTSTK legs

    def test_join_keys_on_underlying_ticker_not_symbol_name(self) -> None:
        # Regression for DEC-DHAN-FNO-JOIN-KEY-001: in the real CSV the equity's
        # SYMBOL_NAME is the long company name, while the F&O-eligible set is
        # built from derivative UNDERLYING_SYMBOL tickers. An equity must be
        # admitted on its OWN UNDERLYING_SYMBOL ticker, never on a coincidental
        # SYMBOL_NAME == ticker match (the old key admitted only ~19/229).
        rows = [
            _row(
                INSTRUMENT="OPTSTK",
                INSTRUMENT_TYPE="OP",
                OPTION_TYPE="PE",
                SYMBOL_NAME="RELIANCE26JAN2800PE",
                UNDERLYING_SYMBOL="RELIANCE",
                SECURITY_ID="40001",
                STRIKE_PRICE="2800",
                SM_EXPIRY_DATE="2026-01-29",
            ),
            # Decoy: SYMBOL_NAME happens to equal the ticker but there is NO
            # UNDERLYING_SYMBOL → the OLD code admitted this; the fixed code
            # rejects it (a lone spot row carries no F&O linkage).
            _row(
                INSTRUMENT="EQUITY",
                INSTRUMENT_TYPE="ES",
                SYMBOL_NAME="RELIANCE",
                UNDERLYING_SYMBOL="",
                SECURITY_ID="55555",
            ),
            # Real F&O equity: long name in SYMBOL_NAME, ticker in UNDERLYING_SYMBOL.
            _row(
                INSTRUMENT="EQUITY",
                INSTRUMENT_TYPE="ES",
                SYMBOL_NAME="RELIANCE INDUSTRIES LTD",
                UNDERLYING_SYMBOL="RELIANCE",
                SECURITY_ID="2885",
            ),
        ]
        report = select_universe_report(rows)
        assert [i.symbol_name for i in report.universe] == ["RELIANCE"]
        # the real spot (raw SECURITY_ID 2885), not the decoy (55555); identity
        # is the (security_id, exch_id) pair (ADR-0003).
        assert [i.security_id for i in report.universe] == [2885]
        assert [(i.exch_id.value, i.symbol_name) for i in report.universe] == [("NSE", "RELIANCE")]


class TestUniverseDedup:
    """Regression for the live `duplicate key "NSE-MOTHERSON"` crash
    (DEC-DHAN-UNIVERSE-DEDUP-001).

    The real Dhan master lists some F&O tickers under MORE THAN ONE NSE equity
    row (an old + a current listing of the same company, different SECURITY_IDs)
    — both resolve to the same `(exch_id, symbol_name)` key, which used to
    collide on the store PK. Selection must admit exactly ONE canonical spot per
    key, mirroring tickvault's `UNDERLYING_SECURITY_ID` → spot resolution.
    """

    def _opt(self, **over: str) -> dict[str, str]:
        base = {
            "INSTRUMENT": "OPTSTK",
            "OPTION_TYPE": "CE",
            "SYMBOL_NAME": "MOTHERSON26JAN100CE",
            "UNDERLYING_SYMBOL": "MOTHERSON",
            "STRIKE_PRICE": "100",
            "SM_EXPIRY_DATE": "2026-01-29",
        }
        base.update(over)
        return _row(**base)

    def test_underlying_security_ids_collects_canonical_spot_ids(self) -> None:
        rows = [self._opt(SECURITY_ID="40001", UNDERLYING_SECURITY_ID="4204")]
        assert fno_underlying_security_ids(rows) == frozenset({4204})
        # An option with no UNDERLYING_SECURITY_ID contributes nothing.
        assert fno_underlying_security_ids([self._opt(SECURITY_ID="40002")]) == frozenset()

    def test_duplicate_listing_dedups_to_canonical_spot(self) -> None:
        # The derivative points at the canonical spot id 4204; the duplicate
        # listing (99999) is admitted first, then REPLACED by the canonical one.
        rows = [
            _row(
                INSTRUMENT="EQUITY",
                SYMBOL_NAME="SAMVRDHNA MTHRSN INTL LTD",
                UNDERLYING_SYMBOL="MOTHERSON",
                SECURITY_ID="99999",
            ),
            _row(
                INSTRUMENT="EQUITY",
                SYMBOL_NAME="SAMVARDHANA MOTHERSON INT",
                UNDERLYING_SYMBOL="MOTHERSON",
                SECURITY_ID="4204",
            ),
            self._opt(SECURITY_ID="40001", UNDERLYING_SECURITY_ID="4204"),
        ]
        report = select_universe_report(rows)
        # ONE spot admitted (no PK collision), and it is the canonical 4204.
        assert [(i.exch_id.value, i.symbol_name) for i in report.universe] == [("NSE", "MOTHERSON")]
        assert [i.security_id for i in report.universe] == [4204]
        assert report.persisted == 1
        assert report.legs_dropped == 2  # the duplicate listing + the option leg

    def test_same_security_id_under_two_symbol_names_collapses_to_one(self) -> None:
        # DEC-DHAN-UNIVERSE-PK-DEDUP-001: the vendor lists ONE security_id under
        # TWO admitted SYMBOL_NAMEs of the SAME instrument type (here the NIFTY
        # index id 13 as both "NIFTY" and "BANKNIFTY"-shaped names). They survive
        # the (exch_id, symbol_name) dedup (different names) but share the table
        # identity (security_id, exch_id, segment) and would crash the bulk
        # INSERT. The second collapse must merge them to exactly ONE row. (Both
        # are INDEX → segment INDEX per DEC-INDEX-SEGMENT-PK-FIX-001 #406, so the
        # identity is genuinely shared and the collapse is correct — contrast
        # TestIndexEquitySharedSecurityId, where an INDEX + an EQUITY share the
        # id but have DISTINCT segments and must BOTH survive.)
        rows = [
            _row(INSTRUMENT="INDEX", SYMBOL_NAME="NIFTY", SECURITY_ID="13", LOT_SIZE="0"),
            _row(INSTRUMENT="INDEX", SYMBOL_NAME="BANKNIFTY", SECURITY_ID="13", LOT_SIZE="0"),
        ]
        report = select_universe_report(rows)
        # ONE row, the canonical identity, no PK collision possible downstream.
        assert [(i.security_id, i.exch_id.value, i.segment.value) for i in report.universe] == [
            (13, "NSE", "INDEX")
        ]
        assert report.persisted == 1
        assert report.legs_dropped == 1  # the duplicate-identity row

    def test_duplicate_listing_keeps_first_when_no_canonical_link(self) -> None:
        # No UNDERLYING_SECURITY_ID on the derivative → no canonical hint →
        # first-seen wins, still exactly ONE spot (no collision, no count loss).
        rows = [
            _row(
                INSTRUMENT="EQUITY",
                SYMBOL_NAME="MOTHERSON A",
                UNDERLYING_SYMBOL="MOTHERSON",
                SECURITY_ID="111",
            ),
            _row(
                INSTRUMENT="EQUITY",
                SYMBOL_NAME="MOTHERSON B",
                UNDERLYING_SYMBOL="MOTHERSON",
                SECURITY_ID="222",
            ),
            self._opt(SECURITY_ID="40001"),
        ]
        report = select_universe_report(rows)
        assert [i.security_id for i in report.universe] == [111]
        assert report.persisted == 1


class TestTickvaultParity:
    """tickvault-faithful gates (DEC-DHAN-UNIVERSE-TICKVAULT-PARITY-001): the
    F&O-stock universe is built ONLY from NSE FUTSTK/OPTSTK rows, TEST scrips are
    excluded, BSE single-stock F&O is out of scope — while indices keep all-NSE
    + BSE SENSEX.
    """

    def test_test_scrip_excluded_from_eligibility(self) -> None:
        rows = [
            _row(
                INSTRUMENT="OPTSTK",
                EXCH_ID="NSE",
                SYMBOL_NAME="11NSETEST",
                UNDERLYING_SYMBOL="11NSETEST",
                UNDERLYING_SECURITY_ID="555",
                SECURITY_ID="40001",
            ),
        ]
        assert fno_underlying_symbols(rows) == frozenset()
        assert fno_underlying_security_ids(rows) == frozenset()

    def test_bse_stock_derivative_not_eligible(self) -> None:
        # BSE single-stock F&O is out of scope — its ticker must NOT become eligible.
        rows = [
            _row(
                INSTRUMENT="OPTSTK",
                EXCH_ID="BSE",
                SYMBOL_NAME="XYZOPT",
                UNDERLYING_SYMBOL="XYZ",
                UNDERLYING_SECURITY_ID="900",
                SECURITY_ID="40002",
            ),
        ]
        assert fno_underlying_symbols(rows) == frozenset()
        assert fno_underlying_security_ids(rows) == frozenset()

    def test_bse_equity_not_admitted_as_stock_spot(self) -> None:
        # An NSE OPTSTK makes RELIANCE eligible; only the NSE equity spot is
        # admitted — the BSE equity listing is dropped (NSE_EQ-only stock universe).
        rows = [
            _row(
                INSTRUMENT="OPTSTK",
                EXCH_ID="NSE",
                SYMBOL_NAME="RELOPT",
                UNDERLYING_SYMBOL="RELIANCE",
                UNDERLYING_SECURITY_ID="2885",
                SECURITY_ID="40003",
            ),
            _row(
                INSTRUMENT="EQUITY",
                EXCH_ID="BSE",
                SYMBOL_NAME="RELIANCE INDUSTRIES LTD",
                UNDERLYING_SYMBOL="RELIANCE",
                SECURITY_ID="500325",
            ),
            _row(
                INSTRUMENT="EQUITY",
                EXCH_ID="NSE",
                SYMBOL_NAME="RELIANCE INDUSTRIES LTD",
                UNDERLYING_SYMBOL="RELIANCE",
                SECURITY_ID="2885",
            ),
        ]
        report = select_universe_report(rows)
        assert [(i.exch_id.value, i.symbol_name) for i in report.universe] == [("NSE", "RELIANCE")]
        assert [i.security_id for i in report.universe] == [2885]

    def test_test_scrip_equity_not_admitted(self) -> None:
        # Ticker ABC is eligible (real NSE OPTSTK), but the equity row is a TEST
        # scrip → excluded from admission by the test-scrip guard.
        rows = [
            _row(
                INSTRUMENT="OPTSTK",
                EXCH_ID="NSE",
                SYMBOL_NAME="ABCOPT",
                UNDERLYING_SYMBOL="ABC",
                UNDERLYING_SECURITY_ID="700",
                SECURITY_ID="40005",
            ),
            _row(
                INSTRUMENT="EQUITY",
                EXCH_ID="NSE",
                SYMBOL_NAME="ABC11NSETEST",
                UNDERLYING_SYMBOL="ABC",
                SECURITY_ID="700",
            ),
        ]
        assert select_universe_report(rows).universe == []

    def test_bse_sensex_index_still_admitted(self) -> None:
        # Indices are NOT gated to NSE — BSE SENSEX is a brute-force index.
        rows = [
            _row(INSTRUMENT="INDEX", EXCH_ID="NSE", SYMBOL_NAME="NIFTY", SECURITY_ID="13"),
            _row(INSTRUMENT="INDEX", EXCH_ID="BSE", SYMBOL_NAME="SENSEX", SECURITY_ID="51"),
        ]
        report = select_universe_report(rows)
        assert sorted((i.exch_id.value, i.symbol_name) for i in report.universe) == [
            ("BSE", "SENSEX"),
            ("NSE", "NIFTY"),
        ]


class TestIndexAllowList:
    """Only the curated headline indices are admitted — the 31 NSE Index-tab
    symbols + BSE SENSEX — NOT all ~194 INDEX rows (DEC-DHAN-INDEX-ALLOWLIST-001).
    """

    def test_obscure_nse_index_dropped(self) -> None:
        # NIFTY is admitted; an obscure NSE index is dropped (not on the list).
        rows = [
            _row(INSTRUMENT="INDEX", EXCH_ID="NSE", SYMBOL_NAME="NIFTY", SECURITY_ID="13"),
            _row(
                INSTRUMENT="INDEX", EXCH_ID="NSE", SYMBOL_NAME="NIFTY ALPHA 50", SECURITY_ID="900"
            ),
        ]
        report = select_universe_report(rows)
        assert [(i.exch_id.value, i.symbol_name) for i in report.universe] == [("NSE", "NIFTY")]

    def test_bse_non_sensex_index_dropped(self) -> None:
        # Only BSE SENSEX is admitted; other BSE indices (e.g. BANKEX) are dropped.
        rows = [
            _row(INSTRUMENT="INDEX", EXCH_ID="BSE", SYMBOL_NAME="SENSEX", SECURITY_ID="51"),
            _row(INSTRUMENT="INDEX", EXCH_ID="BSE", SYMBOL_NAME="BANKEX", SECURITY_ID="52"),
        ]
        report = select_universe_report(rows)
        assert [(i.exch_id.value, i.symbol_name) for i in report.universe] == [("BSE", "SENSEX")]

    def test_is_admitted_index_helper(self) -> None:
        from brutex.adapters.dhan.instruments import _is_admitted_index

        assert _is_admitted_index("NSE", "NIFTY") is True
        assert _is_admitted_index("NSE", "niftyit") is True  # case-insensitive
        assert _is_admitted_index("NSE", "NIFTY ALPHA 50") is False
        assert _is_admitted_index("BSE", "SENSEX") is True
        assert _is_admitted_index("BSE", "BANKEX") is False
        assert _is_admitted_index("MCX", "GOLD") is False  # neither NSE nor BSE

    def test_nifty_midcap100_admitted_31_nse(self) -> None:
        # DEC-DHAN-INDEX-ALLOWLIST-002 (tickvault parity): Nifty Midcap 100 is
        # admitted → 31 NSE tokens + BSE SENSEX = 32 total.
        from brutex.adapters.dhan.instruments import (
            _ADMITTED_BSE_INDEX_SYMBOLS,
            _ADMITTED_NSE_INDEX_SYMBOLS,
            _is_admitted_index,
        )

        assert _is_admitted_index("NSE", "NIFTY MID100 FREE") is True
        assert len(_ADMITTED_NSE_INDEX_SYMBOLS) == 31
        assert len(_ADMITTED_NSE_INDEX_SYMBOLS | _ADMITTED_BSE_INDEX_SYMBOLS) == 32


class TestIndexEquitySharedSecurityId:
    """Regression for DEC-INDEX-SEGMENT-PK-FIX-001 (#406).

    In the real Dhan master, NSE ``SECURITY_ID 13`` is TWO genuinely
    different instruments — NIFTY 50 (INDEX, Dhan ``IDX_I``) AND ABB India
    (EQUITY, ``NSE_EQ``) — because Dhan reuses the numeric id across its
    distinct exchange-segments. ``segment='INDEX'`` for indices keeps their
    identity ``(13, NSE, INDEX)`` distinct from the equity's ``(13, NSE,
    CASH)``, so the universe-level identity collapse (#405) does NOT drop one
    of them. Without the distinct segment both are ``CASH`` → the identity
    dedup silently drops one (data loss: NIFTY 50 or ABB vanishes)."""

    def _rows(self) -> list[dict[str, str]]:
        return [
            # NIFTY 50 — INDEX, security_id 13 (Dhan IDX_I).
            _row(
                INSTRUMENT="INDEX",
                INSTRUMENT_TYPE="INDEX",
                EXCH_ID="NSE",
                SECURITY_ID="13",
                SYMBOL_NAME="NIFTY",
                UNDERLYING_SYMBOL="NIFTY",
                LOT_SIZE="1",
                TICK_SIZE="0.05",
            ),
            # ABB India — EQUITY, ALSO security_id 13 (Dhan NSE_EQ), F&O-eligible.
            _row(
                INSTRUMENT="EQUITY",
                INSTRUMENT_TYPE="ES",
                EXCH_ID="NSE",
                SECURITY_ID="13",
                SYMBOL_NAME="ABB INDIA LIMITED",
                UNDERLYING_SYMBOL="ABB",
                LOT_SIZE="1",
                TICK_SIZE="0.05",
            ),
            # ABB FUTSTK leg → makes ABB F&O-eligible (admitted to the universe).
            _row(
                INSTRUMENT="FUTSTK",
                INSTRUMENT_TYPE="FUT",
                EXCH_ID="NSE",
                SECURITY_ID="90001",
                SYMBOL_NAME="ABB-Jun2026-FUT",
                UNDERLYING_SYMBOL="ABB",
                LOT_SIZE="125",
                TICK_SIZE="0.05",
                SM_EXPIRY_DATE="2026-06-25",
            ),
        ]

    def test_index_and_equity_sharing_security_id_both_survive(self) -> None:
        universe = select_universe_report(self._rows()).universe
        by_sym = {i.symbol_name: i for i in universe}
        # BOTH id-13 instruments survive — no data loss.
        assert "NIFTY" in by_sym, "NIFTY 50 (id 13 INDEX) was dropped from the universe"
        assert "ABB" in by_sym, "ABB (id 13 EQUITY) was dropped from the universe"
        nifty = by_sym["NIFTY"]
        abb = by_sym["ABB"]
        # Same numeric SECURITY_ID, distinct segment → distinct PK identity.
        assert nifty.security_id == 13
        assert abb.security_id == 13
        assert nifty.segment is Segment.INDEX
        assert abb.segment is Segment.CASH
        assert nifty.instrument is InstrumentType.IDX
        assert abb.instrument is InstrumentType.EQ
