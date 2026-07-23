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
# Enum values — pinned to broker SDK values (Dhan instrument-master schema, CLAUDE.md §6)
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
        "exch_id": Exchange.NSE,
        "security_id": 1,
        "symbol_name": "RELIANCE",
        "display_name": "Reliance",
        "instrument": InstrumentType.EQ,
        "segment": Segment.CASH,
        "series": "EQ",
        "isin": "INE002A01018",
        "underlying_symbol": None,
        "underlying_security_id": None,
        "lot_size": 1,
        "tick_size": Decimal("0.05"),
        "sm_freeze_qty": None,
        "sm_expiry_date": None,
        "strike_price": None,
        "asm_gsm_flag": False,
    }
    defaults.update(overrides)
    return Instrument(**defaults)  # type: ignore[arg-type]


def _minimal_option(**overrides: object) -> Instrument:
    defaults: dict[str, object] = {
        "exch_id": Exchange.NSE,
        "security_id": 1,
        "symbol_name": "NIFTY25MAR29050PE",
        "display_name": None,
        "instrument": InstrumentType.PE,
        "segment": Segment.FNO,
        "series": None,
        "isin": None,
        "underlying_symbol": "NIFTY",
        "underlying_security_id": 26000,
        "lot_size": 25,
        "tick_size": Decimal("0.05"),
        "sm_freeze_qty": 7200,
        "sm_expiry_date": date(2025, 3, 27),
        "strike_price": Decimal("29050"),
        "option_type": "PE",
        "expiry_flag": "W",
        "asm_gsm_flag": False,
    }
    defaults.update(overrides)
    return Instrument(**defaults)  # type: ignore[arg-type]


class TestInstrumentClassification:
    def test_equity_is_not_derivative(self) -> None:
        i = _minimal_equity()
        assert i.is_derivative is False
        assert i.is_option is False
        assert i.is_index is False
        assert i.option_side is None

    def test_future_is_derivative_not_option(self) -> None:
        i = _minimal_option(
            instrument=InstrumentType.FUT,
            strike_price=None,
            symbol_name="NIFTY25MARFUT",
        )
        assert i.is_derivative is True
        assert i.is_option is False
        assert i.option_side is None

    def test_call_option(self) -> None:
        i = _minimal_option(
            instrument=InstrumentType.CE,
            symbol_name="NIFTY25MAR29050CE",
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
            instrument=InstrumentType.IDX,
            segment=Segment.CASH,
            symbol_name="NIFTY",
        )
        assert i.is_derivative is False
        assert i.is_option is False
        assert i.is_index is True  # DEC-INDEX-PARSE-FIX-001
        assert i.option_side is None


class TestInstrumentInvariants:
    def test_future_requires_underlying(self) -> None:
        with pytest.raises(ValueError, match="underlying_symbol"):
            _minimal_option(
                instrument=InstrumentType.FUT,
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
        with pytest.raises(ValueError, match="sm_expiry_date"):
            _minimal_option(
                instrument=InstrumentType.FUT,
                sm_expiry_date=None,
                strike_price=None,
            )


class TestInstrumentImmutability:
    def test_frozen_dataclass(self) -> None:
        i = _minimal_equity()
        with pytest.raises(Exception):  # FrozenInstanceError is dataclasses-specific
            i.symbol_name = "OTHER"  # type: ignore[misc]

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
# See the Dhan instrument-master schema (CLAUDE.md §6)
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


# ────────────────────────────────────────────────────────────────────────
# The BRUTEX_CORE_RUST_MODELS seam wiring (slice T35 PR-2)
# ────────────────────────────────────────────────────────────────────────


class TestModelsRustSeamWiring:
    """Flag-OFF is the verbatim Python law with the crate UNTOUCHED; flag-ON
    delegates to :mod:`brutex.models_rust` (the full facade battery lives in
    ``tests/unit/test_models_rust.py`` — these are the stem-matched wiring
    pins for the models.py gate)."""

    @pytest.fixture(autouse=True)
    def _fresh_seam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from brutex import models_rust

        monkeypatch.setenv(models_rust.MODELS_RUST_ENV, "0")
        models_rust.reset_models_rust_for_tests()

    def test_flag_off_never_imports_or_calls_the_crate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys
        import types

        bomb = types.ModuleType("brutex_core")

        def _boom(_kind: str, _payload: str) -> str:
            raise AssertionError("the crate must not be called flag-OFF")

        bomb.models_normalise_tick_size_str = _boom  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "brutex_core", bomb)
        assert normalise_tick_size(Decimal("0.05")) == Decimal("0.05")
        assert normalise_tick_size(5) == Decimal("0.05")
        with pytest.raises(TickSizeFormatError):
            normalise_tick_size(0.07)

    def test_public_law_equals_extracted_oracle_flag_off(self) -> None:
        from brutex.models import (
            _normalise_tick_size_python,  # pyright: ignore[reportPrivateUsage]
        )

        for raw in (Decimal("0.05"), 5, 5.0, "0.10", 1.0, 100):
            assert normalise_tick_size(raw) == _normalise_tick_size_python(raw)

    def test_flag_on_routes_through_the_facade(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys
        import types

        from brutex import models_rust

        calls: list[tuple[str, str]] = []
        crate = types.ModuleType("brutex_core")

        def _spy(kind: str, payload: str) -> str:
            calls.append((kind, payload))
            return "0.05"

        crate.models_normalise_tick_size_str = _spy  # type: ignore[attr-defined]
        crate.__version__ = "0.0-fake"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "brutex_core", crate)
        monkeypatch.setenv(models_rust.MODELS_RUST_ENV, "1")
        assert normalise_tick_size(Decimal("0.05")) == Decimal("0.05")
        assert calls == [("decimal", "0.05")]
