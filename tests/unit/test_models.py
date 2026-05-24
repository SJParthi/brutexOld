"""Unit tests for brutex.models — enums, Instrument, tick-size normaliser."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from brutex.models import (
    VALID_TICK_SIZES_RUPEES,
    BruteXError,
    Exchange,
    Instrument,
    InstrumentType,
    OptionSide,
    Segment,
    TickSizeFormatError,
    normalise_tick_size,
)

# ────────────────────────────────────────────────────────────────────────
# Enum values — pinned to broker SDK values (https://dhanhq.co/docs/v2/annexure)
# ────────────────────────────────────────────────────────────────────────


class TestExchangeEnum:
    def test_has_nse_and_bse(self) -> None:
        assert Exchange.NSE.value == "NSE"
        assert Exchange.BSE.value == "BSE"

    def test_mcx_is_not_in_enum(self) -> None:
        """MCX is out of scope for v1 — must not be accidentally added."""
        assert "MCX" not in {e.value for e in Exchange}

    def test_string_semantics(self) -> None:
        """StrEnum means you can compare directly with strings."""
        assert Exchange.NSE == "NSE"

    def test_count(self) -> None:
        assert len(list(Exchange)) == 2


class TestSegmentEnum:
    def test_has_cash_and_fno(self) -> None:
        assert Segment.CASH.value == "CASH"
        assert Segment.FNO.value == "FNO"

    def test_commodity_not_in_enum(self) -> None:
        assert "COMMODITY" not in {s.value for s in Segment}


class TestInstrumentTypeEnum:
    def test_all_five_values(self) -> None:
        assert {i.value for i in InstrumentType} == {"EQ", "IDX", "FUT", "CE", "PE"}

    def test_idx_not_index(self) -> None:
        """The broker SDK uses 3-letter IDX, not the word INDEX."""
        assert InstrumentType.IDX.value == "IDX"
        assert "INDEX" not in {i.value for i in InstrumentType}


class TestOptionSide:
    def test_values(self) -> None:
        assert OptionSide.CALL.value == "CE"
        assert OptionSide.PUT.value == "PE"


# ────────────────────────────────────────────────────────────────────────
# Instrument dataclass — properties + invariants
# ────────────────────────────────────────────────────────────────────────


def _minimal_equity(**overrides: object) -> Instrument:
    defaults: dict[str, object] = {
        "exchange": Exchange.NSE,
        "exchange_token": 1,
        "trading_symbol": "RELIANCE",
        "vendor_symbol": "NSE-RELIANCE",
        "name": "Reliance",
        "instrument_type": InstrumentType.EQ,
        "segment": Segment.CASH,
        "series": "EQ",
        "isin": "INE002A01018",
        "underlying_symbol": None,
        "underlying_exchange_token": None,
        "lot_size": 1,
        "tick_size": Decimal("0.05"),
        "freeze_quantity": None,
        "expiry_date": None,
        "strike_price": None,
        "is_reserved": False,
        "buy_allowed": True,
        "sell_allowed": True,
    }
    defaults.update(overrides)
    return Instrument(**defaults)  # type: ignore[arg-type]


def _minimal_option(**overrides: object) -> Instrument:
    defaults: dict[str, object] = {
        "exchange": Exchange.NSE,
        "exchange_token": 1,
        "trading_symbol": "NIFTY25MAR29050PE",
        "vendor_symbol": "NSE-NIFTY-27Mar25-29050-PE",
        "name": None,
        "instrument_type": InstrumentType.PE,
        "segment": Segment.FNO,
        "series": None,
        "isin": None,
        "underlying_symbol": "NIFTY",
        "underlying_exchange_token": 26000,
        "lot_size": 25,
        "tick_size": Decimal("0.05"),
        "freeze_quantity": 7200,
        "expiry_date": date(2025, 3, 27),
        "strike_price": Decimal("29050"),
        "is_reserved": False,
        "buy_allowed": True,
        "sell_allowed": True,
    }
    defaults.update(overrides)
    return Instrument(**defaults)  # type: ignore[arg-type]


class TestInstrumentClassification:
    def test_equity_is_not_derivative(self) -> None:
        i = _minimal_equity()
        assert i.is_derivative is False
        assert i.is_option is False
        assert i.option_side is None

    def test_future_is_derivative_not_option(self) -> None:
        i = _minimal_option(
            instrument_type=InstrumentType.FUT,
            strike_price=None,
            trading_symbol="NIFTY25MARFUT",
            vendor_symbol="NSE-NIFTY-27Mar25-FUT",
        )
        assert i.is_derivative is True
        assert i.is_option is False
        assert i.option_side is None

    def test_call_option(self) -> None:
        i = _minimal_option(
            instrument_type=InstrumentType.CE,
            trading_symbol="NIFTY25MAR29050CE",
            vendor_symbol="NSE-NIFTY-27Mar25-29050-CE",
        )
        assert i.is_derivative is True
        assert i.is_option is True
        assert i.option_side == OptionSide.CALL

    def test_put_option(self) -> None:
        i = _minimal_option()  # default is PE
        assert i.is_derivative is True
        assert i.is_option is True
        assert i.option_side == OptionSide.PUT

    def test_index_is_not_derivative_not_option(self) -> None:
        i = _minimal_equity(
            instrument_type=InstrumentType.IDX,
            segment=Segment.CASH,
            trading_symbol="NIFTY",
            vendor_symbol="NSE-NIFTY",
        )
        assert i.is_derivative is False
        assert i.is_option is False
        assert i.option_side is None


class TestInstrumentInvariants:
    def test_future_requires_underlying(self) -> None:
        with pytest.raises(ValueError, match="underlying_symbol"):
            _minimal_option(
                instrument_type=InstrumentType.FUT,
                underlying_symbol=None,
                strike_price=None,
            )

    def test_option_requires_strike(self) -> None:
        with pytest.raises(ValueError, match="strike_price"):
            _minimal_option(strike_price=None)

    def test_option_strike_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            _minimal_option(strike_price=Decimal("-1"))

    def test_lot_size_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="lot_size"):
            _minimal_equity(lot_size=0)

    def test_tick_size_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="tick_size"):
            _minimal_equity(tick_size=Decimal("0"))

    def test_future_requires_expiry(self) -> None:
        with pytest.raises(ValueError, match="expiry_date"):
            _minimal_option(
                instrument_type=InstrumentType.FUT,
                expiry_date=None,
                strike_price=None,
            )


class TestInstrumentImmutability:
    def test_frozen_dataclass(self) -> None:
        i = _minimal_equity()
        with pytest.raises(Exception):  # FrozenInstanceError is dataclasses-specific
            i.trading_symbol = "OTHER"  # type: ignore[misc]

    def test_hashable(self) -> None:
        i1 = _minimal_equity()
        i2 = _minimal_equity()
        assert hash(i1) == hash(i2)
        assert {i1, i2} == {i1}


# ────────────────────────────────────────────────────────────────────────
# Error hierarchy
# ────────────────────────────────────────────────────────────────────────


class TestErrorHierarchy:
    def test_tick_size_format_error_is_brutex_error(self) -> None:
        assert issubclass(TickSizeFormatError, BruteXError)

    def test_brutex_error_str_returns_safe_message_when_set(self) -> None:
        err = BruteXError("internal detail with PII", safe_message="generic public message")
        assert str(err) == "generic public message"

    def test_brutex_error_str_falls_back_to_args_when_safe_message_none(self) -> None:
        err = BruteXError("public-safe arg")
        assert str(err) == "public-safe arg"


# ────────────────────────────────────────────────────────────────────────
# normalise_tick_size — the format drift handler
# See https://dhanhq.co/docs/v2/annexure
# ────────────────────────────────────────────────────────────────────────


class TestTickSizeNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (0.05, Decimal("0.05")),
            (0.10, Decimal("0.10")),
            (0.25, Decimal("0.25")),
            (0.50, Decimal("0.50")),
            (1.00, Decimal("1.00")),
            ("0.05", Decimal("0.05")),
            (Decimal("0.05"), Decimal("0.05")),
        ],
    )
    def test_rupees_float_form(self, raw: float | str | Decimal, expected: Decimal) -> None:
        assert normalise_tick_size(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (5, Decimal("0.05")),  # the famous Reliance dict example
            (10, Decimal("0.10")),
            (25, Decimal("0.25")),
            (50, Decimal("0.50")),
        ],
    )
    def test_paise_integer_form(self, raw: int, expected: Decimal) -> None:
        assert normalise_tick_size(raw) == expected

    def test_integer_float_is_treated_as_paise(self) -> None:
        # 5.0 is an integer-valued float — treat as paise
        assert normalise_tick_size(5.0) == Decimal("0.05")

    def test_bool_rejected(self) -> None:
        with pytest.raises(TickSizeFormatError, match="bool"):
            normalise_tick_size(True)  # type: ignore[arg-type]

    def test_unparseable_string_rejected(self) -> None:
        with pytest.raises(TickSizeFormatError, match="parse"):
            normalise_tick_size("not-a-number")

    def test_unsupported_type_rejected(self) -> None:
        with pytest.raises(TickSizeFormatError, match="Unsupported"):
            normalise_tick_size([0.05])  # type: ignore[arg-type]

    def test_out_of_range_paise_rejected(self) -> None:
        with pytest.raises(TickSizeFormatError, match="range"):
            normalise_tick_size(9999)

    def test_weird_value_rejected_against_known_set(self) -> None:
        # 0.07 is a parseable value but not in the valid NSE/BSE set
        with pytest.raises(TickSizeFormatError, match="not a known"):
            normalise_tick_size(0.07)

    def test_valid_set_is_frozen(self) -> None:
        assert Decimal("0.05") in VALID_TICK_SIZES_RUPEES
        assert Decimal("5.00") in VALID_TICK_SIZES_RUPEES
