"""The frozen ``brutex.models`` TYPE contract (slice T35 PR-2).

The MECHANIZED no-go evidence for the models pyclass-hoist verdict
(recorded in :mod:`brutex.models_rust`'s docstring + the T35 PR-2 ledger
entry): every pin here is a semantic a pyo3 pyclass CANNOT reproduce —
``StrEnum`` str-subclass identity, the ``dataclasses`` protocol on
``Instrument``, the ``BruteXError`` custom-``__str__`` exception root —
frozen byte-for-byte so any future hoist attempt must consciously confront
this contract (and so an accidental refactor cannot silently break the
~105 importers that rely on these behaviors).

Behavioral-parity battery per the fleet ruling: enum values byte-stable +
member ORDER; dataclass field ORDER; equality/hash/repr semantics; pickle
round-trips; isinstance/str-subclass laws; ``dataclasses.fields``
introspection parity.
"""

from __future__ import annotations

import copy
import dataclasses
import enum
import json
import pickle
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from brutex.models import (
    MAX_TICK_PAISE,
    MIN_TICK_PAISE,
    VALID_TICK_SIZES_RUPEES,
    BruteXError,
    Exchange,
    Instrument,
    InstrumentFormatError,
    InstrumentType,
    OptionSide,
    OutOfScopeError,
    Segment,
    TickSizeFormatError,
)


def _nifty() -> Instrument:
    """The canonical reference instance (NIFTY spot index)."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="Nifty 50",
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


# ── Enum taxonomy pins — member ORDER + value BYTES ────────────────────────


class TestEnumTaxonomyFrozen:
    """The four StrEnum taxonomies: exact member order + exact value bytes.

    The enum string VALUES flow into lake partition path components +
    ``instrument_store`` rows via ``.exch_id.value`` — byte-stability is
    load-bearing even though models.py is NOT in run identity.
    """

    def test_exchange_members_and_values(self) -> None:
        assert [(m.name, m.value) for m in Exchange] == [("NSE", "NSE"), ("BSE", "BSE")]

    def test_segment_members_and_values(self) -> None:
        assert [(m.name, m.value) for m in Segment] == [
            ("INDEX", "INDEX"),
            ("CASH", "CASH"),
            ("FNO", "FNO"),
        ]

    def test_instrument_type_members_and_values(self) -> None:
        assert [(m.name, m.value) for m in InstrumentType] == [
            ("EQ", "EQ"),
            ("IDX", "IDX"),
            ("FUT", "FUT"),
            ("CE", "CE"),
            ("PE", "PE"),
        ]

    def test_option_side_members_and_values(self) -> None:
        assert [(m.name, m.value) for m in OptionSide] == [("CALL", "CE"), ("PUT", "PE")]


class TestStrEnumSemantics:
    """The str-subclass laws a pyo3 pyclass cannot reproduce (the no-go
    core): every member IS a str, JSON-serializes as its value, compares
    equal to the bare string, and reconstructs by value lookup."""

    @pytest.mark.parametrize("enum_cls", [Exchange, Segment, InstrumentType, OptionSide])
    def test_members_are_str_and_enum_instances(self, enum_cls: type[enum.StrEnum]) -> None:
        for member in enum_cls:
            assert isinstance(member, str)
            assert isinstance(member, enum.StrEnum)
            assert type(member) is enum_cls

    def test_str_and_fstring_render_the_value(self) -> None:
        assert str(Exchange.NSE) == "NSE"
        assert f"{Exchange.BSE}" == "BSE"
        assert f"bars/{Exchange.NSE}/{Segment.CASH}" == "bars/NSE/CASH"  # the lake-path law

    def test_equality_and_hash_against_bare_strings(self) -> None:
        assert Exchange.NSE == "NSE"
        assert hash(Exchange.NSE) == hash("NSE")
        assert Exchange.NSE in {"NSE", "BSE"}
        assert "NSE" in {Exchange.NSE}  # symmetric set membership

    def test_json_dumps_serializes_as_the_value(self) -> None:
        payload = json.dumps({"exch": Exchange.NSE, "seg": Segment.FNO, "it": InstrumentType.CE})
        assert payload == '{"exch": "NSE", "seg": "FNO", "it": "CE"}'

    @pytest.mark.parametrize(
        ("enum_cls", "value"),
        [(Exchange, "NSE"), (Segment, "CASH"), (InstrumentType, "PE"), (OptionSide, "CE")],
    )
    def test_value_lookup_constructor(self, enum_cls: type[enum.StrEnum], value: str) -> None:
        member = enum_cls(value)
        assert member.value == value  # the instrument_store row-hydration law

    def test_unknown_value_lookup_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="MCX"):
            Exchange("MCX")

    @pytest.mark.parametrize("enum_cls", [Exchange, Segment, InstrumentType, OptionSide])
    def test_pickle_round_trip_preserves_identity(self, enum_cls: type[enum.StrEnum]) -> None:
        for member in enum_cls:
            clone = pickle.loads(pickle.dumps(member))  # noqa: S301 — self-produced bytes
            assert clone is member  # enum members round-trip to the SAME object

    def test_sorting_interleaves_with_plain_strings(self) -> None:
        assert sorted(["AAA", Exchange.NSE, "ZZZ", Exchange.BSE]) == [
            "AAA",
            Exchange.BSE,
            Exchange.NSE,
            "ZZZ",
        ]


# ── Instrument dataclass-protocol pins ─────────────────────────────────────


class TestInstrumentDataclassProtocol:
    """The ``dataclasses`` laws a pyo3 pyclass cannot satisfy (the no-go
    core): production calls ``dataclasses.replace`` on ``Instrument``
    (``brutex/dashboard/instrument_loader.py`` leg-economics fold)."""

    def test_is_a_frozen_slots_dataclass(self) -> None:
        assert dataclasses.is_dataclass(Instrument)
        params = Instrument.__dataclass_params__  # pyright: ignore[reportAttributeAccessIssue]
        assert params.frozen is True
        assert not hasattr(_nifty(), "__dict__")  # slots=True — no instance dict

    def test_field_name_order_frozen(self) -> None:
        assert tuple(f.name for f in dataclasses.fields(Instrument)) == (
            "exch_id",
            "security_id",
            "symbol_name",
            "display_name",
            "instrument",
            "segment",
            "series",
            "isin",
            "underlying_symbol",
            "underlying_security_id",
            "lot_size",
            "tick_size",
            "sm_freeze_qty",
            "sm_expiry_date",
            "strike_price",
            "option_type",
            "expiry_flag",
            "asm_gsm_flag",
            "feed_token",
        )

    def test_defaulted_tail_values_frozen(self) -> None:
        defaults = {
            f.name: f.default
            for f in dataclasses.fields(Instrument)
            if f.default is not dataclasses.MISSING
        }
        assert defaults == {
            "option_type": None,
            "expiry_flag": None,
            "asm_gsm_flag": False,
            "feed_token": None,
        }

    def test_match_args_mirror_field_order(self) -> None:
        assert Instrument.__match_args__ == tuple(f.name for f in dataclasses.fields(Instrument))

    def test_frozen_rejects_mutation(self) -> None:
        with pytest.raises(FrozenInstanceError):
            _nifty().lot_size = 75  # type: ignore[misc]

    def test_replace_law(self) -> None:
        """The production ``replace(inst, lot_size=..., sm_freeze_qty=...)``
        call shape (instrument_loader leg-economics fold) works and only
        moves the named fields."""
        inst = _nifty()
        folded = dataclasses.replace(inst, lot_size=75, sm_freeze_qty=1800)
        assert folded.lot_size == 75
        assert folded.sm_freeze_qty == 1800
        assert folded.symbol_name == inst.symbol_name
        assert folded != inst
        assert dataclasses.replace(folded, lot_size=0, sm_freeze_qty=None) == inst

    def test_equality_and_hash_by_value(self) -> None:
        a, b = _nifty(), _nifty()
        assert a == b
        assert a is not b
        assert hash(a) == hash(b)
        assert len({a, b}) == 1

    def test_pickle_and_deepcopy_round_trip(self) -> None:
        inst = _nifty()
        clone = pickle.loads(pickle.dumps(inst))  # noqa: S301 — self-produced bytes
        assert clone == inst
        assert type(clone) is Instrument  # class identity survives (spawn workers)
        assert copy.deepcopy(inst) == inst

    def test_asdict_shape(self) -> None:
        shaped = dataclasses.asdict(_nifty())
        assert shaped["symbol_name"] == "NIFTY"
        assert len(shaped) == len(dataclasses.fields(Instrument))

    def test_repr_golden(self) -> None:
        assert repr(_nifty()) == (
            "Instrument(exch_id=<Exchange.NSE: 'NSE'>, security_id=13, "
            "symbol_name='NIFTY', display_name='Nifty 50', "
            "instrument=<InstrumentType.IDX: 'IDX'>, segment=<Segment.INDEX: 'INDEX'>, "
            "series=None, isin=None, underlying_symbol=None, "
            "underlying_security_id=None, lot_size=0, tick_size=Decimal('0'), "
            "sm_freeze_qty=None, sm_expiry_date=None, strike_price=None, "
            "option_type=None, expiry_flag=None, asm_gsm_flag=False, feed_token=None)"
        )


# ── BruteXError exception-root pins ────────────────────────────────────────


class TestBruteXErrorContract:
    """The exception-root laws (79 importer files derive from this class)."""

    def test_subclass_tree(self) -> None:
        assert issubclass(OutOfScopeError, BruteXError)
        assert issubclass(InstrumentFormatError, BruteXError)
        assert issubclass(TickSizeFormatError, InstrumentFormatError)
        assert issubclass(BruteXError, Exception)

    def test_safe_message_redaction_law(self) -> None:
        exc = BruteXError("raw secret token=abc", safe_message="redacted")
        assert str(exc) == "redacted"
        assert exc.args == ("raw secret token=abc",)

    def test_without_safe_message_str_is_positional(self) -> None:
        assert str(BruteXError("plain")) == "plain"
        assert BruteXError().safe_message is None

    def test_pickle_preserves_args_and_safe_message(self) -> None:
        """Probed CPython behavior 2026-07-17: exception pickling carries
        the instance ``__dict__`` state, so ``safe_message`` — and the
        redacted ``str()`` — SURVIVE a spawn-worker round trip."""
        exc = BruteXError("raw secret", safe_message="redacted")
        clone = pickle.loads(pickle.dumps(exc))  # noqa: S301 — self-produced bytes
        assert clone.args == ("raw secret",)
        assert clone.safe_message == "redacted"
        assert str(clone) == "redacted"

    def test_catch_via_the_root(self) -> None:
        with pytest.raises(BruteXError):
            raise TickSizeFormatError("drift")


# ── Module-constant pins ───────────────────────────────────────────────────


class TestConstantPins:
    def test_tick_paise_bounds(self) -> None:
        assert MIN_TICK_PAISE == 1
        assert MAX_TICK_PAISE == 1000

    def test_valid_tick_sizes_literal(self) -> None:
        assert (
            frozenset(Decimal(s) for s in ("0.01", "0.05", "0.10", "0.25", "0.50", "1.00", "5.00"))
            == VALID_TICK_SIZES_RUPEES
        )
