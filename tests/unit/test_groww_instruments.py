"""Tests for `brutex.adapters.groww.instruments` — CSV parsing + registry.

100% line+branch: CSV row → GrowwInstrument, null sentinel handling,
segment mapping (IDX→INDEX), exchange mapping, brute-force hardcoded
registry, build_registry filter (instrument_types + exchanges), and
load_registry_from_csv with injected _csv_text.
"""

# pyright: reportPrivateUsage=none
from __future__ import annotations

from decimal import Decimal

import pytest
from brutex.adapters.groww.instruments import (
    _opt_date,
    _opt_decimal,
    _opt_int,
    _opt_str,
    _parse_exchange,
    _parse_instrument_type,
    _parse_segment,
    _row_to_groww_instrument,
    build_brute_force_registry,
    build_registry,
    load_registry_from_csv,
)
from brutex.models import Exchange, InstrumentType, Segment

# ── minimal CSV fixture ───────────────────────────────────────────────────

_CSV_HEADER = (
    "exchange,exchange_token,trading_symbol,groww_symbol,name,"
    "instrument_type,segment,series,isin,underlying_symbol,"
    "underlying_exchange_token,expiry_date,strike_price,lot_size,"
    "tick_size,freeze_quantity,is_reserved,buy_allowed,sell_allowed\n"
)

_NIFTY_ROW = (
    "NSE,99926004,NIFTY,NSE-NIFTY,Nifty 50,"
    "IDX,CASH,EQ,nan,nan,nan,nan,nan,0,0.05,nan,0,True,True\n"
)

_BANKNIFTY_ROW = (
    "NSE,99926009,BANKNIFTY,NSE-BANKNIFTY,Nifty Bank,"
    "IDX,CASH,EQ,nan,nan,nan,nan,nan,0,0.05,nan,0,True,True\n"
)

_RELIANCE_ROW = (
    "NSE,2885,RELIANCE,NSE-RELIANCE,Reliance Industries,"
    "EQ,CASH,EQ,INE002A01018,nan,nan,nan,nan,1,0.05,5000,0,True,True\n"
)

_FNO_ROW = (
    "NSE,12345,NIFTY23JAN13000CE,NSE-NIFTY-23Jan21-13000-CE,NIFTY Jan 13000 CE,"
    "CE,FNO,nan,nan,NIFTY,99926004,2021-01-28,13000,75,0.05,nan,0,True,True\n"
)

_BSE_SENSEX_ROW = (
    "BSE,1,SENSEX,BSE-SENSEX,S&P BSE Sensex,"
    "IDX,CASH,EQ,nan,nan,nan,nan,nan,0,0.01,nan,0,True,True\n"
)

_MINIMAL_CSV = _CSV_HEADER + _NIFTY_ROW + _BANKNIFTY_ROW + _RELIANCE_ROW + _FNO_ROW + _BSE_SENSEX_ROW


# ── null sentinel helpers ─────────────────────────────────────────────────

class TestOptStr:
    @pytest.mark.parametrize("null", ["nan", "NaN", "", "None", "none"])
    def test_null_sentinels_return_none(self, null: str) -> None:
        assert _opt_str(null) is None

    def test_non_null_returns_stripped(self) -> None:
        assert _opt_str("  hello  ") == "hello"

    def test_whitespace_only_returns_none(self) -> None:
        assert _opt_str("   ") is None


class TestOptInt:
    @pytest.mark.parametrize("null", ["nan", "NaN", ""])
    def test_null_sentinels_return_none(self, null: str) -> None:
        assert _opt_int(null) is None

    def test_integer_string_parsed(self) -> None:
        assert _opt_int("75") == 75

    def test_float_string_truncated_to_int(self) -> None:
        assert _opt_int("75.0") == 75

    def test_unparseable_string_returns_none(self) -> None:
        assert _opt_int("notanumber") is None


class TestOptDecimal:
    @pytest.mark.parametrize("null", ["nan", "NaN", ""])
    def test_null_sentinels_return_none(self, null: str) -> None:
        assert _opt_decimal(null) is None

    def test_decimal_string_parsed(self) -> None:
        assert _opt_decimal("0.05") == Decimal("0.05")

    def test_unparseable_returns_none(self) -> None:
        assert _opt_decimal("notanumber") is None


class TestOptDate:
    @pytest.mark.parametrize("null", ["nan", "NaN", ""])
    def test_null_sentinels_return_none(self, null: str) -> None:
        assert _opt_date(null) is None

    def test_valid_iso_date_parsed(self) -> None:
        from datetime import date
        assert _opt_date("2021-01-28") == date(2021, 1, 28)

    def test_invalid_date_returns_none(self) -> None:
        assert _opt_date("not-a-date") is None


class TestParseExchange:
    def test_nse(self) -> None:
        assert _parse_exchange("NSE") is Exchange.NSE

    def test_bse(self) -> None:
        assert _parse_exchange("BSE") is Exchange.BSE

    def test_lowercase_nse(self) -> None:
        assert _parse_exchange("nse") is Exchange.NSE

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported exchange"):
            _parse_exchange("MCX")


class TestParseInstrumentType:
    @pytest.mark.parametrize(
        ("val", "expected"),
        [
            ("EQ", InstrumentType.EQ),
            ("IDX", InstrumentType.IDX),
            ("FUT", InstrumentType.FUT),
            ("CE", InstrumentType.CE),
            ("PE", InstrumentType.PE),
        ],
    )
    def test_known_types(self, val: str, expected: InstrumentType) -> None:
        assert _parse_instrument_type(val) is expected

    def test_lowercase_works(self) -> None:
        assert _parse_instrument_type("eq") is InstrumentType.EQ

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown instrument_type"):
            _parse_instrument_type("OPT")


class TestParseSegment:
    def test_idx_maps_to_index_regardless_of_csv_segment(self) -> None:
        assert _parse_segment("CASH", InstrumentType.IDX) is Segment.INDEX
        assert _parse_segment("FNO", InstrumentType.IDX) is Segment.INDEX

    def test_eq_cash_maps_to_cash(self) -> None:
        assert _parse_segment("CASH", InstrumentType.EQ) is Segment.CASH

    def test_fno_maps_to_fno(self) -> None:
        assert _parse_segment("FNO", InstrumentType.CE) is Segment.FNO

    def test_unknown_segment_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown segment"):
            _parse_segment("DERIVATIVES", InstrumentType.EQ)


class TestRowToGrowwInstrument:
    def _nifty_row(self) -> dict[str, str]:
        return {
            "exchange": "NSE",
            "exchange_token": "99926004",
            "trading_symbol": "NIFTY",
            "groww_symbol": "NSE-NIFTY",
            "name": "Nifty 50",
            "instrument_type": "IDX",
            "segment": "CASH",
            "series": "EQ",
            "isin": "nan",
            "underlying_symbol": "nan",
            "underlying_exchange_token": "nan",
            "expiry_date": "nan",
            "strike_price": "nan",
            "lot_size": "0",
            "tick_size": "0.05",
            "freeze_quantity": "nan",
            "is_reserved": "0",
        }

    def test_nifty_idx_happy_path(self) -> None:
        gi = _row_to_groww_instrument(self._nifty_row())
        assert gi is not None
        assert gi.groww_symbol == "NSE-NIFTY"
        assert gi.api_exchange == "NSE"
        assert gi.api_segment == "CASH"
        assert gi.instrument.symbol_name == "NIFTY"
        assert gi.instrument.segment is Segment.INDEX
        assert gi.instrument.instrument is InstrumentType.IDX

    def test_security_id_from_exchange_token(self) -> None:
        gi = _row_to_groww_instrument(self._nifty_row())
        assert gi is not None
        assert gi.instrument.security_id == 99926004

    def test_bad_exchange_returns_none(self) -> None:
        row = self._nifty_row()
        row["exchange"] = "UNKNOWN"
        gi = _row_to_groww_instrument(row)
        assert gi is None

    def test_fno_segment_maps_to_fno(self) -> None:
        row = self._nifty_row()
        row["instrument_type"] = "CE"
        row["segment"] = "FNO"
        row["groww_symbol"] = "NSE-NIFTY-23Jan21-13000-CE"
        row["trading_symbol"] = "NIFTY23JAN13000CE"
        row["underlying_symbol"] = "NIFTY"
        row["expiry_date"] = "2021-01-28"
        row["strike_price"] = "13000"
        row["lot_size"] = "75"
        gi = _row_to_groww_instrument(row)
        assert gi is not None
        assert gi.api_segment == "FNO"
        assert gi.instrument.segment is Segment.FNO


class TestBuildRegistry:
    def test_parses_all_valid_rows(self) -> None:
        reg = build_registry(_MINIMAL_CSV)
        assert len(reg.all_instruments) == 5

    def test_filter_by_idx_only(self) -> None:
        reg = build_registry(_MINIMAL_CSV, instrument_types=frozenset({"IDX"}))
        assert all(gi.instrument.instrument is InstrumentType.IDX for gi in reg.all_instruments)
        assert len(reg.all_instruments) == 3  # NIFTY, BANKNIFTY, SENSEX

    def test_filter_by_eq_only(self) -> None:
        reg = build_registry(_MINIMAL_CSV, instrument_types=frozenset({"EQ"}))
        assert len(reg.all_instruments) == 1
        assert reg.all_instruments[0].instrument.symbol_name == "RELIANCE"

    def test_filter_by_nse_exchange(self) -> None:
        reg = build_registry(_MINIMAL_CSV, exchanges=frozenset({"NSE"}))
        assert all(gi.api_exchange == "NSE" for gi in reg.all_instruments)

    def test_by_symbol_name_lookup(self) -> None:
        reg = build_registry(_MINIMAL_CSV)
        assert "NIFTY" in reg.by_symbol_name
        gi = reg.by_symbol_name["NIFTY"]
        assert gi.groww_symbol == "NSE-NIFTY"

    def test_by_groww_symbol_lookup(self) -> None:
        reg = build_registry(_MINIMAL_CSV)
        assert "NSE-NIFTY" in reg.by_groww_symbol
        gi = reg.by_groww_symbol["NSE-NIFTY"]
        assert gi.instrument.symbol_name == "NIFTY"

    def test_all_instruments_is_tuple(self) -> None:
        reg = build_registry(_MINIMAL_CSV)
        assert isinstance(reg.all_instruments, tuple)

    def test_empty_csv_returns_empty_registry(self) -> None:
        reg = build_registry(_CSV_HEADER)
        assert len(reg.all_instruments) == 0


class TestBuildBruteForceRegistry:
    def test_four_instruments(self) -> None:
        reg = build_brute_force_registry()
        assert len(reg.all_instruments) == 4

    def test_nifty_present(self) -> None:
        reg = build_brute_force_registry()
        assert "NIFTY" in reg.by_symbol_name
        assert "NSE-NIFTY" in reg.by_groww_symbol

    def test_all_are_idx(self) -> None:
        reg = build_brute_force_registry()
        for gi in reg.all_instruments:
            assert gi.instrument.instrument is InstrumentType.IDX
            assert gi.instrument.segment is Segment.INDEX

    def test_security_id_is_placeholder_zero(self) -> None:
        reg = build_brute_force_registry()
        for gi in reg.all_instruments:
            assert gi.instrument.security_id == 0

    def test_sensex_is_bse(self) -> None:
        reg = build_brute_force_registry()
        gi = reg.by_symbol_name["SENSEX"]
        assert gi.api_exchange == "BSE"

    def test_indiavix_present(self) -> None:
        reg = build_brute_force_registry()
        assert "INDIAVIX" in reg.by_symbol_name


class TestLoadRegistryFromCsv:
    def test_injected_csv_text_used_directly(self) -> None:
        reg = load_registry_from_csv(
            instrument_types=frozenset({"IDX"}),
            _csv_text=_MINIMAL_CSV,
        )
        assert len(reg.all_instruments) == 3

    def test_filter_passes_through(self) -> None:
        reg = load_registry_from_csv(
            instrument_types=frozenset({"EQ"}),
            _csv_text=_MINIMAL_CSV,
        )
        assert len(reg.all_instruments) == 1
        assert reg.all_instruments[0].instrument.symbol_name == "RELIANCE"


class TestGrowwInstrumentDataclass:
    def test_frozen_and_slots(self) -> None:
        reg = build_brute_force_registry()
        gi = reg.by_symbol_name["NIFTY"]
        with pytest.raises(AttributeError):
            gi.groww_symbol = "mutated"  # type: ignore[misc]


class TestInstrumentRegistryDataclass:
    def test_frozen(self) -> None:
        reg = build_brute_force_registry()
        with pytest.raises(AttributeError):
            reg.all_instruments = ()  # type: ignore[misc]
