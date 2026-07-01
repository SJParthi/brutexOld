"""Unit tests for the cost-scope predicate — DEC-COST-SCOPE-INDEX-SIGNAL-ONLY-001.

The operator-locked rule (MIGRATION.md #1060):

* COST-FREE (signal-only): **INDEX SPOT** + **INDEX FUTURES** ONLY.
* COST-BEARING (full brokerage/STT/slippage/liquidity/fill stack): every stock
  (spot / future / option) AND every INDEX OPTION.

These tests pin ``brutex.costs.scope.is_cost_free`` / ``is_cost_bearing`` /
``is_index_future`` for every instrument class and exercise the predicate at
100% line + branch. A companion integration test
(``tests/integration/test_cost_scope_engine_signal_only.py``) proves the ENGINE
index-spot / index-futures path applies zero cost while a cost-bearing
instrument path applies the full stack.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from brutex.costs.scope import is_cost_bearing, is_cost_free, is_index_future
from brutex.models import Exchange, Instrument, InstrumentType, Segment

# ────────────────────────────────────────────────────────────────────────
# Fixtures — one instance per instrument class
# ────────────────────────────────────────────────────────────────────────


def _index_spot(*, exchange: Exchange = Exchange.NSE, symbol: str = "NIFTY") -> Instrument:
    """A brute-force spot INDEX (IDX) — cost-free."""
    return Instrument(
        exch_id=exchange,
        security_id=13,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,  # indices carry no lot size
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def _index_future(
    *,
    exchange: Exchange = Exchange.NSE,
    underlying: str = "NIFTY",
    symbol: str = "NIFTY25MARFUT",
) -> Instrument:
    """A FUTURE on a brute-force index underlying — cost-free."""
    return Instrument(
        exch_id=exchange,
        security_id=100,
        symbol_name=symbol,
        display_name=None,
        instrument=InstrumentType.FUT,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_security_id=26000,
        lot_size=75,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=1800,
        sm_expiry_date=date(2025, 3, 27),
        strike_price=None,
    )


def _index_option(*, option_type: InstrumentType = InstrumentType.CE) -> Instrument:
    """An OPTION on an index underlying (NIFTY CE/PE) — COST-BEARING."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=200,
        symbol_name="NIFTY25MAR24000CE",
        display_name=None,
        instrument=option_type,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="NIFTY",
        underlying_security_id=26000,
        lot_size=75,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=1800,
        sm_expiry_date=date(2025, 3, 27),
        strike_price=Decimal("24000"),
    )


def _stock_spot() -> Instrument:
    """A cash-equity STOCK (RELIANCE EQ) — COST-BEARING."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=2885,
        symbol_name="RELIANCE",
        display_name="Reliance",
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin="INE002A01018",
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def _stock_future() -> Instrument:
    """A single-stock FUTURE (RELIANCE FUT) — COST-BEARING."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=300,
        symbol_name="RELIANCE25MARFUT",
        display_name=None,
        instrument=InstrumentType.FUT,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="RELIANCE",
        underlying_security_id=2885,
        lot_size=500,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=10000,
        sm_expiry_date=date(2025, 3, 27),
        strike_price=None,
    )


def _stock_option() -> Instrument:
    """A single-stock OPTION (RELIANCE CE) — COST-BEARING."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=400,
        symbol_name="RELIANCE25MAR3000CE",
        display_name=None,
        instrument=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol="RELIANCE",
        underlying_security_id=2885,
        lot_size=500,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=10000,
        sm_expiry_date=date(2025, 3, 27),
        strike_price=Decimal("3000"),
    )


# ────────────────────────────────────────────────────────────────────────
# is_cost_free — the headline predicate
# ────────────────────────────────────────────────────────────────────────


class TestIsCostFree:
    def test_index_spot_is_cost_free(self) -> None:
        assert is_cost_free(_index_spot()) is True

    def test_all_three_brute_force_indices_are_cost_free(self) -> None:
        assert is_cost_free(_index_spot(exchange=Exchange.NSE, symbol="NIFTY")) is True
        assert is_cost_free(_index_spot(exchange=Exchange.NSE, symbol="BANKNIFTY")) is True
        assert is_cost_free(_index_spot(exchange=Exchange.BSE, symbol="SENSEX")) is True

    def test_index_future_is_cost_free(self) -> None:
        assert is_cost_free(_index_future()) is True

    def test_index_option_is_cost_bearing(self) -> None:
        # An INDEX option is NOT cost-free — the crux of the operator correction.
        assert is_cost_free(_index_option(option_type=InstrumentType.CE)) is False
        assert is_cost_free(_index_option(option_type=InstrumentType.PE)) is False

    def test_stock_spot_is_cost_bearing(self) -> None:
        assert is_cost_free(_stock_spot()) is False

    def test_stock_future_is_cost_bearing(self) -> None:
        assert is_cost_free(_stock_future()) is False

    def test_stock_option_is_cost_bearing(self) -> None:
        assert is_cost_free(_stock_option()) is False

    def test_non_brute_force_index_spot_is_still_cost_free(self) -> None:
        # A spot index that is NOT brute-forced (e.g. FINNIFTY) is still an IDX:
        # signal-only by TYPE. ``is_index`` (IDX) short-circuits cost-free.
        assert is_cost_free(_index_spot(exchange=Exchange.NSE, symbol="FINNIFTY")) is True


# ────────────────────────────────────────────────────────────────────────
# is_index_future — the FUT discriminator
# ────────────────────────────────────────────────────────────────────────


class TestIsIndexFuture:
    def test_index_future_true(self) -> None:
        assert is_index_future(_index_future()) is True

    def test_sensex_future_true(self) -> None:
        assert (
            is_index_future(
                _index_future(exchange=Exchange.BSE, underlying="SENSEX", symbol="SENSEX25MARFUT")
            )
            is True
        )

    def test_stock_future_false(self) -> None:
        # FUT type, but the underlying is not a brute-force index.
        assert is_index_future(_stock_future()) is False

    def test_index_spot_is_not_a_future(self) -> None:
        # IDX type — not a FUT — so the FUT-specific predicate is False.
        assert is_index_future(_index_spot()) is False

    def test_index_option_is_not_a_future(self) -> None:
        assert is_index_future(_index_option()) is False

    def test_index_future_on_wrong_exchange_is_false(self) -> None:
        # NIFTY underlying but exch BSE is not the (BSE, NIFTY) brute-force pair,
        # so the (exchange, underlying) membership guard rejects it.
        assert (
            is_index_future(
                _index_future(exchange=Exchange.BSE, underlying="NIFTY", symbol="NIFTY25MARFUT")
            )
            is False
        )


# ────────────────────────────────────────────────────────────────────────
# is_cost_bearing — the exact complement
# ────────────────────────────────────────────────────────────────────────


class TestIsCostBearing:
    def test_is_exact_complement_of_cost_free(self) -> None:
        for inst in (
            _index_spot(),
            _index_future(),
            _index_option(),
            _stock_spot(),
            _stock_future(),
            _stock_option(),
        ):
            assert is_cost_bearing(inst) is (not is_cost_free(inst))

    def test_cost_bearing_true_for_all_tradable_classes(self) -> None:
        assert is_cost_bearing(_index_option()) is True
        assert is_cost_bearing(_stock_spot()) is True
        assert is_cost_bearing(_stock_future()) is True
        assert is_cost_bearing(_stock_option()) is True

    def test_cost_bearing_false_for_cost_free_classes(self) -> None:
        assert is_cost_bearing(_index_spot()) is False
        assert is_cost_bearing(_index_future()) is False
