"""Tests for `brutex.dashboard.universe_pull.select_pull_instruments`.

100% line+branch: index vs stock selection (reusing the COMMON classifier),
the derive-vs-injected `fno_eligible` paths, the stable `trading_symbol` sort,
exclusion of derivative legs + non-F&O equities, and the bad-kind guard.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from brutex.dashboard.universe_pull import select_pull_instruments
from brutex.models import Exchange, Instrument, InstrumentType, Segment


def _index(symbol: str, *, exchange: Exchange = Exchange.NSE) -> Instrument:
    """An NSE/BSE spot index (IDX → classified ``index``)."""
    return Instrument(
        exchange=exchange,
        exchange_token=hash(symbol) % 100000,
        trading_symbol=symbol,
        vendor_symbol=f"{exchange.value}-{symbol}",
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


def _equity(symbol: str) -> Instrument:
    """An NSE cash equity (EQ → classified ``stock`` iff F&O-eligible)."""
    return Instrument(
        exchange=Exchange.NSE,
        exchange_token=hash(symbol) % 100000,
        trading_symbol=symbol,
        vendor_symbol=f"NSE-{symbol}",
        name=symbol,
        instrument_type=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=f"INE{abs(hash(symbol)) % 10000:04d}A01010",
        underlying_symbol=None,
        underlying_exchange_token=None,
        lot_size=550,
        tick_size=Decimal("0.05"),
        freeze_quantity=None,
        expiry_date=None,
        strike_price=None,
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


def _option_leg(underlying: str) -> Instrument:
    """A CE leg on *underlying* — makes the equity F&O-eligible; itself excluded."""
    return Instrument(
        exchange=Exchange.NSE,
        exchange_token=hash(underlying + "CE") % 100000,
        trading_symbol=f"{underlying}25MAR1000CE",
        vendor_symbol=f"NSE-{underlying}-27Mar25-1000-CE",
        name=None,
        instrument_type=InstrumentType.CE,
        segment=Segment.FNO,
        series=None,
        isin=None,
        underlying_symbol=underlying,
        underlying_exchange_token=hash(underlying) % 100000,
        lot_size=550,
        tick_size=Decimal("0.05"),
        freeze_quantity=7200,
        expiry_date=date(2025, 3, 27),
        strike_price=Decimal("1000"),
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


def _full_snapshot() -> list[Instrument]:
    """Two indices + one F&O-eligible equity (+ its leg) + one non-F&O equity."""
    return [
        _index("NIFTY"),
        _index("BANKNIFTY"),
        _equity("HDFCBANK"),
        _option_leg("HDFCBANK"),  # makes HDFCBANK F&O-eligible
        _equity("NONFNOCO"),  # no derivative → not eligible → excluded
    ]


def test_selects_only_indices() -> None:
    selected = select_pull_instruments(_full_snapshot(), kind="index")
    assert [i.trading_symbol for i in selected] == ["BANKNIFTY", "NIFTY"]  # sorted
    assert all(i.instrument_type is InstrumentType.IDX for i in selected)


def test_selects_only_fno_eligible_stocks() -> None:
    selected = select_pull_instruments(_full_snapshot(), kind="stock")
    # Only HDFCBANK (has a leg); NONFNOCO is excluded (no derivative).
    assert [i.trading_symbol for i in selected] == ["HDFCBANK"]
    assert selected[0].instrument_type is InstrumentType.EQ


def test_injected_fno_eligible_skips_derivation() -> None:
    # Candidates-only snapshot (no legs) + an injected eligible set — the
    # DEC-INSTRUMENT-UNIVERSE-FAST-001 fast path.
    candidates = [_index("NIFTY"), _equity("HDFCBANK")]
    eligible = frozenset({(Exchange.NSE, "HDFCBANK")})
    selected = select_pull_instruments(candidates, kind="stock", fno_eligible=eligible)
    assert [i.trading_symbol for i in selected] == ["HDFCBANK"]


def test_sort_is_stable_alphabetical() -> None:
    snapshot = [_index("SENSEX", exchange=Exchange.BSE), _index("AXISIDX"), _index("NIFTY")]
    selected = select_pull_instruments(snapshot, kind="index")
    assert [i.trading_symbol for i in selected] == ["AXISIDX", "NIFTY", "SENSEX"]


def test_empty_snapshot_is_empty() -> None:
    assert select_pull_instruments([], kind="index") == []


def test_bad_kind_raises() -> None:
    with pytest.raises(ValueError, match="kind must be one of"):
        select_pull_instruments(_full_snapshot(), kind="options")
