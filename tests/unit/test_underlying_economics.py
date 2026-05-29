"""Unit tests for ``brutex.universe.build_underlying_economics``.

DEC-UNDERLYING-ECONOMICS-001 (FIX 1): in the Groww instrument master, IDX and
EQ rows carry BLANK / placeholder economics; the REAL ``lot_size`` /
``tick_size`` / ``freeze_quantity`` live only on the derivative legs. This
module pins the pure helper that harvests those economics from the legs so the
lean ingest can back-fill each persisted underlying.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.universe import UnderlyingEconomics, build_underlying_economics


def _idx(symbol: str, *, exchange: Exchange = Exchange.NSE) -> Instrument:
    """Build a minimal IDX row (blank economics, as the master ships)."""
    return Instrument(
        exchange=exchange,
        exchange_token=0,
        trading_symbol=symbol,
        vendor_symbol=f"{exchange}-{symbol}",
        name=symbol,
        instrument_type=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_exchange_token=None,
        lot_size=0,
        tick_size=Decimal("0"),
        freeze_quantity=None,
        expiry_date=None,
        strike_price=None,
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


def _eq(symbol: str, *, lot_size: int = 1) -> Instrument:
    """Build a minimal EQ underlying row (blank economics, as the master ships)."""
    return Instrument(
        exchange=Exchange.NSE,
        exchange_token=1,
        trading_symbol=symbol,
        vendor_symbol=f"NSE-{symbol}",
        name=symbol,
        instrument_type=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin="INE000A01000",
        underlying_symbol=None,
        underlying_exchange_token=None,
        lot_size=lot_size,
        tick_size=Decimal("0.05"),
        freeze_quantity=None,
        expiry_date=None,
        strike_price=None,
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


def _leg(
    symbol: str,
    underlying: str,
    itype: InstrumentType,
    *,
    lot_size: int,
    tick_size: Decimal,
    freeze_quantity: int | None,
    exchange: Exchange = Exchange.NSE,
    strike: Decimal | None = None,
) -> Instrument:
    """Build a derivative leg carrying explicit economics."""
    return Instrument(
        exchange=exchange,
        exchange_token=2,
        trading_symbol=symbol,
        vendor_symbol=f"{exchange}-{symbol}",
        name=symbol,
        instrument_type=itype,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_exchange_token=None,
        lot_size=lot_size,
        tick_size=tick_size,
        freeze_quantity=freeze_quantity,
        expiry_date=date(2025, 1, 30),
        strike_price=strike,
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


def test_harvests_from_legs() -> None:
    """Economics map carries each underlying's leg-derived lot/tick/freeze."""
    legs = [
        _leg(
            "RELIANCE25JANFUT",
            "RELIANCE",
            InstrumentType.FUT,
            lot_size=500,
            tick_size=Decimal("0.05"),
            freeze_quantity=5000,
        ),
        _leg(
            "NIFTY25JAN24000CE",
            "NIFTY",
            InstrumentType.CE,
            lot_size=75,
            tick_size=Decimal("0.05"),
            freeze_quantity=1800,
            strike=Decimal("24000"),
        ),
    ]
    econ = build_underlying_economics(legs)
    assert econ[(Exchange.NSE, "RELIANCE")] == (500, Decimal("0.05"), 5000)
    assert econ[(Exchange.NSE, "NIFTY")] == (75, Decimal("0.05"), 1800)


def test_first_leg_wins() -> None:
    """The first leg seen for an underlying wins (deterministic, order-stable)."""
    legs = [
        _leg(
            "RELIANCE25JANFUT",
            "RELIANCE",
            InstrumentType.FUT,
            lot_size=500,
            tick_size=Decimal("0.05"),
            freeze_quantity=5000,
        ),
        _leg(
            "RELIANCE25JAN3000CE",
            "RELIANCE",
            InstrumentType.CE,
            lot_size=999,
            tick_size=Decimal("0.10"),
            freeze_quantity=1,
            strike=Decimal("3000"),
        ),
    ]
    econ = build_underlying_economics(legs)
    assert econ[(Exchange.NSE, "RELIANCE")] == (500, Decimal("0.05"), 5000)


def test_ignores_non_derivatives() -> None:
    """IDX / EQ rows never contribute — only derivative legs do."""
    rows = [_idx("NIFTY"), _eq("RELIANCE", lot_size=1)]
    assert build_underlying_economics(rows) == {}


def test_empty_input() -> None:
    """No instruments → empty economics map."""
    result: dict[tuple[Exchange, str], UnderlyingEconomics] = build_underlying_economics([])
    assert result == {}


def test_skips_leg_without_underlying() -> None:
    """A malformed derivative with no underlying_symbol is skipped, not raised.

    ``Instrument.__post_init__`` forbids a real derivative with a None
    underlying, so we use a duck-typed fake exposing only the surface the helper
    reads. This exercises the ``underlying is None`` guard branch.
    """

    class _FakeLeg:
        underlying_symbol = None
        is_derivative = True
        exchange = Exchange.NSE
        lot_size = 75
        tick_size = Decimal("0.05")
        freeze_quantity = 1800

    assert build_underlying_economics([_FakeLeg()]) == {}  # type: ignore[list-item]
