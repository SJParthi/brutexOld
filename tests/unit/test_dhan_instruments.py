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
        "INSTRUMENT_TYPE": "EQUITY",
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
        assert inst.exchange is Exchange.NSE
        assert inst.exchange_token == 13
        assert inst.vendor_symbol == "13"
        assert inst.trading_symbol == "RELIANCE"
        assert inst.instrument_type is InstrumentType.EQ
        assert inst.segment is Segment.CASH
        assert inst.lot_size == 1
        assert inst.tick_size == Decimal("0.05")
        assert inst.freeze_quantity == 100000

    def test_index_zero_lot_tick(self) -> None:
        inst = parse_instrument_row(
            _row(INSTRUMENT_TYPE="INDEX", SYMBOL_NAME="NIFTY", LOT_SIZE="", TICK_SIZE="")
        )
        assert inst.instrument_type is InstrumentType.IDX
        assert inst.lot_size == 0
        assert inst.tick_size == Decimal("0")
        assert inst.freeze_quantity is None

    def test_option_row(self) -> None:
        inst = parse_instrument_row(
            _row(
                EXCH_ID="NSE",
                INSTRUMENT_TYPE="OPTIDX",
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
        assert inst.instrument_type is InstrumentType.CE
        assert inst.segment is Segment.FNO
        assert inst.expiry_date == date(2025, 1, 2)
        assert inst.strike_price == Decimal("28500")
        assert inst.underlying_exchange_token == 13

    def test_put_option(self) -> None:
        inst = parse_instrument_row(
            _row(
                INSTRUMENT_TYPE="OPTSTK",
                OPTION_TYPE="PE",
                STRIKE_PRICE="100",
                UNDERLYING_SYMBOL="RELIANCE",
                SM_EXPIRY_DATE="2025-01-30",
                LOT_SIZE="250",
            )
        )
        assert inst.instrument_type is InstrumentType.PE

    def test_future_row(self) -> None:
        inst = parse_instrument_row(
            _row(
                INSTRUMENT_TYPE="FUTSTK",
                SM_EXPIRY_DATE="2025-01-30 14:30:00",
                UNDERLYING_SYMBOL="RELIANCE",
                LOT_SIZE="250",
            )
        )
        assert inst.instrument_type is InstrumentType.FUT
        assert inst.segment is Segment.FNO
        assert inst.expiry_date == date(2025, 1, 30)

    def test_lot_size_accepts_float_string(self) -> None:
        assert parse_instrument_row(_row(LOT_SIZE="50.0")).lot_size == 50

    def test_reserved_from_asm_flag(self) -> None:
        assert parse_instrument_row(_row(ASM_GSM_FLAG="ASM")).is_reserved is True
        assert parse_instrument_row(_row(ASM_GSM_FLAG="")).is_reserved is False

    def test_absent_optional_columns_become_none(self) -> None:
        row = _row()
        del row["SERIES"]
        del row["DISPLAY_NAME"]
        inst = parse_instrument_row(row)
        assert inst.series is None
        assert inst.name is None

    @pytest.mark.parametrize(
        ("overrides", "match"),
        [
            ({"EXCH_ID": "MCX"}, "out-of-scope exchange"),
            ({"SECURITY_ID": ""}, "missing SECURITY_ID"),
            ({"INSTRUMENT_TYPE": "WEIRD"}, "unknown INSTRUMENT_TYPE"),
            ({"INSTRUMENT_TYPE": "OPTIDX", "OPTION_TYPE": "XX"}, "invalid OPTION_TYPE"),
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
                INSTRUMENT_TYPE="INDEX",
                SYMBOL_NAME="NIFTY",
                SECURITY_ID="13",
                LOT_SIZE="",
                TICK_SIZE="",
            ),
            _row(
                EXCH_ID="BSE",
                INSTRUMENT_TYPE="INDEX",
                SYMBOL_NAME="SENSEX",
                SECURITY_ID="51",
                LOT_SIZE="",
                TICK_SIZE="",
            ),
            _row(
                INSTRUMENT_TYPE="EQUITY", SYMBOL_NAME="RELIANCE", SECURITY_ID="2885"
            ),  # F&O eligible
            _row(INSTRUMENT_TYPE="EQUITY", SYMBOL_NAME="TINYCO", SECURITY_ID="9999"),  # NOT F&O
            _row(
                INSTRUMENT_TYPE="OPTSTK",
                OPTION_TYPE="CE",
                SYMBOL_NAME="RELIANCE-OPT",
                UNDERLYING_SYMBOL="RELIANCE",
                SECURITY_ID="3001",
                STRIKE_PRICE="2500",
                SM_EXPIRY_DATE="2025-01-30",
            ),
            _row(
                EXCH_ID="MCX", INSTRUMENT_TYPE="FUTCOM", SYMBOL_NAME="GOLD", SECURITY_ID="7777"
            ),  # skipped
        ]

    def test_fno_underlying_set(self) -> None:
        assert fno_underlying_symbols(self._rows()) == frozenset({"RELIANCE"})

    def test_select_universe_keeps_indices_and_fno_spot(self) -> None:
        universe = select_universe(self._rows())
        symbols = sorted(i.trading_symbol for i in universe)
        assert symbols == ["NIFTY", "RELIANCE", "SENSEX"]
        # Non-F&O equity dropped; option legs dropped; MCX skipped.
        assert all(i.instrument_type in {InstrumentType.IDX, InstrumentType.EQ} for i in universe)

    def test_select_universe_report_accounting(self) -> None:
        report = select_universe_report(self._rows())
        # In-scope NSE/BSE rows: NIFTY, SENSEX, RELIANCE, TINYCO, RELIANCE-OPT.
        assert report.parsed == 5
        # Persisted universe: NIFTY, SENSEX, RELIANCE.
        assert report.persisted == 3
        assert sorted(i.trading_symbol for i in report.universe) == ["NIFTY", "RELIANCE", "SENSEX"]
        # Dropped in-scope: TINYCO (non-F&O equity) + RELIANCE-OPT (option leg).
        assert report.legs_dropped == 2
        # Out-of-scope: the MCX GOLD future.
        assert report.filtered == 1
        # Accounting invariant: every in-scope row is either persisted or dropped.
        assert report.parsed == report.persisted + report.legs_dropped
