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
    DiscoveredIndex,
    IndexDiscoveryError,
    build_leg_economics,
    discover_index,
    fno_underlying_security_ids,
    fno_underlying_symbols,
    options_underlying_security_ids,
    options_underlying_symbols,
    parse_instrument_row,
    parse_nse_equities,
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
            # In-scope NSE FUTSTK leg — census-only (futures_count); empty LOT_SIZE
            # so it's skipped by leg-economics exactly like the OPTSTK above, and
            # dropped from the lean universe (DEC-INSTRUMENT-RUN-AUDIT-001).
            _row(
                INSTRUMENT="FUTSTK",
                SYMBOL_NAME="RELIANCE-FUT",
                UNDERLYING_SYMBOL="RELIANCE",
                SECURITY_ID="3002",
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
        # In-scope NSE/BSE rows: NIFTY, SENSEX, RELIANCE, TINYCO, RELIANCE-OPT, RELIANCE-FUT.
        assert report.parsed == 6
        # Persisted universe: NIFTY, SENSEX, RELIANCE.
        assert report.persisted == 3
        assert sorted(i.symbol_name for i in report.universe) == ["NIFTY", "RELIANCE", "SENSEX"]
        # Dropped in-scope: TINYCO (non-F&O equity) + RELIANCE-OPT + RELIANCE-FUT legs.
        assert report.legs_dropped == 3

    # ── REFERENCE_ONLY admission (DEC-NIFTY-TOTAL-MARKET-ADMIT-001) ──

    def test_reference_security_id_admits_non_fno_equity(self) -> None:
        # TINY CO LTD (SECURITY_ID 9999) is non-F&O → normally dropped; admit it
        # by its resolved SECURITY_ID as a REFERENCE_ONLY constituent.
        universe = select_universe(self._rows(), reference_security_ids=frozenset({9999}))
        symbols = sorted(i.symbol_name for i in universe)
        assert symbols == ["NIFTY", "RELIANCE", "SENSEX", "TINY CO LTD"]
        # It is admitted by id; its symbol_name stays the long company name.
        tiny = next(i for i in universe if i.security_id == 9999)
        assert tiny.instrument is InstrumentType.EQ
        assert tiny.symbol_name == "TINY CO LTD"

    def test_reference_empty_is_byte_identical(self) -> None:
        rows = self._rows()
        default = sorted(i.security_id for i in select_universe(rows))
        explicit = sorted(
            i.security_id for i in select_universe(rows, reference_security_ids=frozenset())
        )
        assert default == explicit  # empty set never changes the selection

    def test_reference_id_does_not_admit_a_non_equity_row(self) -> None:
        # 3001 is the OPTSTK leg, not an EQUITY — a reference id must NOT admit it.
        universe = select_universe(self._rows(), reference_security_ids=frozenset({3001}))
        assert sorted(i.symbol_name for i in universe) == ["NIFTY", "RELIANCE", "SENSEX"]

    def test_reference_admission_report_counts(self) -> None:
        report = select_universe_report(self._rows(), reference_security_ids=frozenset({9999}))
        # TINYCO now persists → 4 universe rows, one fewer dropped leg.
        assert report.persisted == 4
        assert report.legs_dropped == 2
        # Out-of-scope: the MCX GOLD future.
        assert report.filtered == 1
        # Accounting invariant: every in-scope row is either persisted or dropped.
        assert report.parsed == report.persisted + report.legs_dropped
        # Census for the instrument_runs audit row (DEC-INSTRUMENT-RUN-AUDIT-001),
        # counted over IN-SCOPE rows only.
        assert report.total_raw_rows == 7  # 6 in-scope + 1 MCX
        assert report.integrity_failures == 0  # parser fails closed (no recoverable failures)
        assert report.in_scope_count == 6
        assert report.out_of_scope_count == 1
        assert report.index_count == 2  # NIFTY + SENSEX
        assert report.futures_count == 1  # RELIANCE-FUT
        assert report.options_count == 1  # RELIANCE-OPT
        assert report.nse_count == 5  # NIFTY, RELIANCE, TINYCO, RELIANCE-OPT, RELIANCE-FUT
        assert report.bse_count == 1  # SENSEX
        # In-scope exchange split reconciles exactly.
        assert report.nse_count + report.bse_count == report.in_scope_count


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


class TestOptionsEligible:
    """OPTIONS-ONLY eligibility detectors (DEC-OPTIONS-ELIGIBLE-GUARD-001): the
    symbol arm (``options_underlying_symbols``) + the string-free id arm
    (``options_underlying_security_ids``) of the option-pull quota guard. Both
    collect from OPTSTK/OPTIDX legs ONLY — futures are deliberately excluded
    (Track 1 never pulls futures).
    """

    def test_symbols_collect_stock_and_index_options_uppercased(self) -> None:
        rows = [
            _row(INSTRUMENT="OPTSTK", SYMBOL_NAME="REL-OPT", UNDERLYING_SYMBOL="reliance"),
            _row(INSTRUMENT="OPTIDX", SYMBOL_NAME="NIFTY-OPT", UNDERLYING_SYMBOL="NIFTY"),
        ]
        assert options_underlying_symbols(rows) == frozenset({"RELIANCE", "NIFTY"})

    def test_symbols_exclude_futures(self) -> None:
        # FUTSTK / FUTIDX legs are NOT options — we never pull futures.
        rows = [
            _row(INSTRUMENT="FUTSTK", SYMBOL_NAME="REL-FUT", UNDERLYING_SYMBOL="RELIANCE"),
            _row(INSTRUMENT="FUTIDX", SYMBOL_NAME="NIFTY-FUT", UNDERLYING_SYMBOL="NIFTY"),
        ]
        assert options_underlying_symbols(rows) == frozenset()
        assert options_underlying_security_ids(rows) == frozenset()

    def test_symbols_skip_off_exchange_and_blank_and_test_scrips(self) -> None:
        rows = [
            # Off-exchange (MCX) option leg → skipped.
            _row(
                EXCH_ID="MCX",
                INSTRUMENT="OPTSTK",
                SYMBOL_NAME="GOLD-OPT",
                UNDERLYING_SYMBOL="GOLD",
            ),
            # Blank underlying → contributes nothing.
            _row(INSTRUMENT="OPTSTK", SYMBOL_NAME="X-OPT", UNDERLYING_SYMBOL=""),
            # TEST scrip (Dhan "NSETEST" marker) → excluded.
            _row(INSTRUMENT="OPTSTK", SYMBOL_NAME="NSETEST-OPT", UNDERLYING_SYMBOL="NSETEST"),
        ]
        assert options_underlying_symbols(rows) == frozenset()

    def test_security_ids_collect_canonical_spot_ids(self) -> None:
        rows = [
            _row(
                INSTRUMENT="OPTIDX",
                SYMBOL_NAME="NIFTY-OPT",
                UNDERLYING_SYMBOL="NIFTY",
                UNDERLYING_SECURITY_ID="13",
            ),
            _row(
                INSTRUMENT="OPTSTK",
                SYMBOL_NAME="REL-OPT",
                UNDERLYING_SYMBOL="RELIANCE",
                UNDERLYING_SECURITY_ID="2885",
            ),
        ]
        assert options_underlying_security_ids(rows) == frozenset({13, 2885})

    def test_security_ids_skip_off_exchange_blank_and_test_scrips(self) -> None:
        rows = [
            # Off-exchange → skipped.
            _row(
                EXCH_ID="MCX",
                INSTRUMENT="OPTSTK",
                SYMBOL_NAME="GOLD-OPT",
                UNDERLYING_SYMBOL="GOLD",
                UNDERLYING_SECURITY_ID="999",
            ),
            # TEST scrip (Dhan "NSETEST" marker) → excluded.
            _row(
                INSTRUMENT="OPTSTK",
                SYMBOL_NAME="NSETEST-OPT",
                UNDERLYING_SYMBOL="NSETEST",
                UNDERLYING_SECURITY_ID="888",
            ),
            # No UNDERLYING_SECURITY_ID → contributes nothing.
            _row(INSTRUMENT="OPTIDX", SYMBOL_NAME="NIFTY-OPT", UNDERLYING_SYMBOL="NIFTY"),
        ]
        assert options_underlying_security_ids(rows) == frozenset()

    def test_report_exposes_both_options_sets(self) -> None:
        rows = [
            _row(INSTRUMENT="INDEX", SYMBOL_NAME="NIFTY", SECURITY_ID="13", LOT_SIZE="0"),
            _row(
                INSTRUMENT="OPTIDX",
                SYMBOL_NAME="NIFTY-OPT",
                SECURITY_ID="3002",
                UNDERLYING_SYMBOL="NIFTY",
                UNDERLYING_SECURITY_ID="13",
            ),
        ]
        report = select_universe_report(rows)
        assert report.options_eligible_symbols == frozenset({"NIFTY"})
        assert report.options_eligible_security_ids == frozenset({13})


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


class TestBuildLegEconomics:
    """Full branch coverage for ``build_leg_economics`` (DEC-UNDERLYING-LEG-ECONOMICS-001)."""

    def test_nearest_expiry_leg_wins_and_keys_by_underlying(self) -> None:
        rows = [
            # Far-month FUTSTK first, near-month second → near must win (lot can
            # differ across months; first-seen would be wrong).
            _row(
                INSTRUMENT="FUTSTK",
                UNDERLYING_SYMBOL="TCS",
                SYMBOL_NAME="TCS-Sep",
                SECURITY_ID="1",
                LOT_SIZE="225",
                SM_EXPIRY_DATE="2026-09-24",
                SM_FREEZE_QTY="9000",
            ),
            _row(
                INSTRUMENT="FUTSTK",
                UNDERLYING_SYMBOL="TCS",
                SYMBOL_NAME="TCS-Jun",
                SECURITY_ID="2",
                LOT_SIZE="175",
                SM_EXPIRY_DATE="2026-06-25",
                SM_FREEZE_QTY="8925",
            ),
        ]
        econ = build_leg_economics(rows)
        assert econ[(Exchange.NSE, "TCS")] == (175, 8925)  # nearest-expiry lot+freeze

    def test_non_leg_bad_exchange_no_underlying_and_nonpositive_lot_skipped(self) -> None:
        rows = [
            _row(INSTRUMENT="EQUITY", UNDERLYING_SYMBOL="ABB", SECURITY_ID="1"),  # not a leg
            _row(
                INSTRUMENT="FUTCOM",
                EXCH_ID="MCX",
                UNDERLYING_SYMBOL="GOLD",
                SECURITY_ID="2",
                LOT_SIZE="1",
            ),  # not an FNO leg type AND bad exchange
            _row(
                INSTRUMENT="FUTSTK",
                EXCH_ID="MCX",
                UNDERLYING_SYMBOL="X",
                SECURITY_ID="3",
                LOT_SIZE="10",
            ),  # leg but out-of-scope exchange
            _row(
                INSTRUMENT="FUTSTK",
                UNDERLYING_SYMBOL="",
                SYMBOL_NAME="orphan",
                SECURITY_ID="4",
                LOT_SIZE="10",
            ),  # leg, no underlying
            _row(
                INSTRUMENT="FUTSTK",
                UNDERLYING_SYMBOL="ZERO",
                SECURITY_ID="5",
                LOT_SIZE="0",
            ),  # leg, non-positive lot
        ]
        assert build_leg_economics(rows) == {}

    def test_nonpositive_freeze_becomes_none_and_no_expiry_sorts_last(self) -> None:
        rows = [
            # Dated leg with a real freeze.
            _row(
                INSTRUMENT="OPTSTK",
                UNDERLYING_SYMBOL="INFY",
                SECURITY_ID="1",
                LOT_SIZE="400",
                SM_EXPIRY_DATE="2026-06-25",
                SM_FREEZE_QTY="20000",
            ),
            # Undated leg (sorts LAST) with a non-positive freeze → freeze None;
            # it must NOT override the dated near leg above.
            _row(
                INSTRUMENT="OPTSTK",
                UNDERLYING_SYMBOL="INFY",
                SECURITY_ID="2",
                LOT_SIZE="999",
                SM_EXPIRY_DATE="",
                SM_FREEZE_QTY="0",
            ),
        ]
        assert build_leg_economics(rows)[(Exchange.NSE, "INFY")] == (400, 20000)

    def test_undated_leg_used_when_only_option(self) -> None:
        rows = [
            _row(
                INSTRUMENT="OPTIDX",
                UNDERLYING_SYMBOL="NIFTY",
                SECURITY_ID="1",
                LOT_SIZE="65",
                SM_EXPIRY_DATE="",
                SM_FREEZE_QTY="0",
            ),
        ]
        # Only leg → used despite no expiry; freeze 0 → None.
        assert build_leg_economics(rows)[(Exchange.NSE, "NIFTY")] == (65, None)


class TestDiscoverIndex:
    """Runtime index discovery (DEC-NIFTY-TOTAL-MARKET-DISCOVER-001).

    Reads the LIVE master and returns the real (symbol_name, security_id) for the
    one matching INDEX row — fail-loud on zero / ambiguous / no-id, so BruteX
    never hardcodes a guessed id (Golden Rule #1).
    """

    def test_finds_single_match(self) -> None:
        rows = [
            _row(INSTRUMENT="INDEX", SYMBOL_NAME="NIFTY TOTAL MKT", SECURITY_ID="46"),
            _row(INSTRUMENT="EQUITY", SYMBOL_NAME="RELIANCE", SECURITY_ID="2"),
        ]
        found = discover_index(rows, pattern=r"TOTAL\s*M(?:AR)?K(?:E)?T")
        assert found == DiscoveredIndex(
            symbol_name="NIFTY TOTAL MKT", security_id=46, exchange=Exchange.NSE
        )

    def test_matches_market_spelling_too(self) -> None:
        rows = [_row(INSTRUMENT="INDEX", SYMBOL_NAME="NIFTY TOTAL MARKET", SECURITY_ID="46")]
        assert discover_index(rows, pattern=r"TOTAL\s*M(?:AR)?K(?:E)?T").security_id == 46

    def test_zero_match_raises_with_scan_size(self) -> None:
        rows = [_row(INSTRUMENT="INDEX", SYMBOL_NAME="NIFTY 50", SECURITY_ID="13")]
        with pytest.raises(IndexDiscoveryError, match=r"no NSE INDEX row matched.*among 1"):
            discover_index(rows, pattern=r"TOTAL\s*MKT")

    def test_ambiguous_match_lists_candidates(self) -> None:
        rows = [
            _row(INSTRUMENT="INDEX", SYMBOL_NAME="NIFTY TOTAL MKT", SECURITY_ID="46"),
            _row(INSTRUMENT="INDEX", SYMBOL_NAME="NIFTY TOTAL MARKET 2", SECURITY_ID="47"),
        ]
        with pytest.raises(IndexDiscoveryError, match=r"ambiguous.*matched 2"):
            discover_index(rows, pattern=r"TOTAL\s*M")

    def test_matched_row_without_security_id_raises(self) -> None:
        rows = [_row(INSTRUMENT="INDEX", SYMBOL_NAME="NIFTY TOTAL MKT", SECURITY_ID="")]
        with pytest.raises(IndexDiscoveryError, match=r"no\s+SECURITY_ID|carries no"):
            discover_index(rows, pattern=r"TOTAL\s*MKT")

    def test_non_index_rows_ignored(self) -> None:
        # An EQUITY (or any non-INDEX) row that matches the name pattern is NOT a
        # candidate — only INSTRUMENT==INDEX rows are scanned.
        rows = [
            _row(INSTRUMENT="EQUITY", SYMBOL_NAME="NIFTY TOTAL MKT LTD", SECURITY_ID="9"),
        ]
        with pytest.raises(IndexDiscoveryError, match=r"among 0"):
            discover_index(rows, pattern=r"TOTAL\s*MKT")

    def test_other_exchange_rows_ignored(self) -> None:
        # exchange filter: a BSE INDEX matching the pattern is skipped when the
        # default NSE scan is requested.
        rows = [
            _row(EXCH_ID="BSE", INSTRUMENT="INDEX", SYMBOL_NAME="TOTAL MKT", SECURITY_ID="5"),
        ]
        with pytest.raises(IndexDiscoveryError, match=r"no NSE INDEX row matched.*among 0"):
            discover_index(rows, pattern=r"TOTAL\s*MKT", exchange=Exchange.NSE)
        # ...but found when the BSE exchange is scanned.
        assert discover_index(rows, pattern=r"TOTAL\s*MKT", exchange=Exchange.BSE).security_id == 5


class TestExtraIndexSymbols:
    """``extra_index_symbols`` admits runtime-discovered indices (NTM) as
    REFERENCE_ONLY IDX rows without leaking into the brute-force engine."""

    def test_default_empty_does_not_admit_ntm(self) -> None:
        rows = [_row(INSTRUMENT="INDEX", SYMBOL_NAME="NIFTY TOTAL MKT", SECURITY_ID="46")]
        # Not on the curated allow-list → dropped by default (byte-identical).
        assert select_universe(rows) == []

    def test_extra_symbol_admits_the_index(self) -> None:
        rows = [_row(INSTRUMENT="INDEX", SYMBOL_NAME="NIFTY TOTAL MKT", SECURITY_ID="46")]
        uni = select_universe(rows, extra_index_symbols=frozenset({"NIFTY TOTAL MKT"}))
        assert len(uni) == 1
        assert uni[0].security_id == 46
        assert uni[0].instrument is InstrumentType.IDX

    def test_extra_symbol_lowercase_is_normalised(self) -> None:
        rows = [_row(INSTRUMENT="INDEX", SYMBOL_NAME="NIFTY TOTAL MKT", SECURITY_ID="46")]
        # Tokens are upper-cased before matching, so a lower-case token still admits.
        uni = select_universe(rows, extra_index_symbols=frozenset({"nifty total mkt"}))
        assert [i.security_id for i in uni] == [46]

    def test_blank_token_ignored(self) -> None:
        rows = [_row(INSTRUMENT="INDEX", SYMBOL_NAME="NIFTY 50", SECURITY_ID="13")]
        # A whitespace-only token is filtered out → no spurious admission.
        assert select_universe(rows, extra_index_symbols=frozenset({"   "})) == []


class TestParseNseEquities:
    """``parse_nse_equities`` returns ONLY the NSE EQUITY rows (the subset the
    constituent resolver matches against), skipping indices + derivative legs."""

    def test_keeps_only_nse_equities(self) -> None:
        rows = [
            _row(INSTRUMENT="EQUITY", SYMBOL_NAME="RELIANCE", SECURITY_ID="2", ISIN="INE002A01018"),
            _row(INSTRUMENT="INDEX", SYMBOL_NAME="NIFTY 50", SECURITY_ID="13"),
            _row(INSTRUMENT="OPTSTK", SYMBOL_NAME="RELIANCE-OPT", SECURITY_ID="3"),
            _row(EXCH_ID="BSE", INSTRUMENT="EQUITY", SYMBOL_NAME="TCS", SECURITY_ID="4"),
        ]
        out = parse_nse_equities(rows)
        assert [i.security_id for i in out] == [2]
        assert out[0].isin == "INE002A01018"

    def test_empty_when_no_equities(self) -> None:
        rows = [_row(INSTRUMENT="INDEX", SYMBOL_NAME="NIFTY 50", SECURITY_ID="13")]
        assert parse_nse_equities(rows) == []
