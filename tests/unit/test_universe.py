"""Unit tests for brutex.universe — the locked v1 F&O indices universe."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.universe import (
    ALLOWED_UNDERLYINGS,
    INDEX_UNDERLYINGS,
    V1_INDICES,
    V1_UNDERLYINGS,
    classify_underlying_kind,
    is_in_scope,
    is_index_underlying,
)


class TestV1IndicesComposition:
    """V1_INDICES is the 8 NAMED F&O indices allow-list.

    Stock underlyings are NOT in this tuple — they are discovered at
    runtime from Groww's CSV per FD-09d.
    """

    def test_has_exactly_eight_named_indices(self) -> None:
        assert len(V1_INDICES) == 8

    def test_contains_all_five_nse_indices(self) -> None:
        nse_syms = {u.underlying_symbol for u in V1_INDICES if u.exchange == Exchange.NSE}
        assert nse_syms == {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}

    def test_contains_all_three_bse_indices(self) -> None:
        bse_syms = {u.underlying_symbol for u in V1_INDICES if u.exchange == Exchange.BSE}
        assert bse_syms == {"SENSEX", "BANKEX", "SENSEX50"}

    def test_nse_comes_first(self) -> None:
        """Ordering is deliberate: NSE first (deeper history) then BSE."""
        exchanges_seen = [u.exchange for u in V1_INDICES]
        first_bse_idx = exchanges_seen.index(Exchange.BSE)
        last_nse_idx = max(i for i, ex in enumerate(exchanges_seen) if ex == Exchange.NSE)
        assert last_nse_idx < first_bse_idx

    def test_fno_live_years(self) -> None:
        expected = {
            "NIFTY": 2000,
            "BANKNIFTY": 2005,
            "FINNIFTY": 2021,
            "MIDCPNIFTY": 2023,
            "NIFTYNXT50": 2024,
            "SENSEX": 2000,
            "BANKEX": 2023,
            "SENSEX50": 2023,
        }
        actual = {u.underlying_symbol: u.fno_live_since_year for u in V1_INDICES}
        assert actual == expected

    def test_is_tuple_not_list_frozen(self) -> None:
        assert isinstance(V1_INDICES, tuple)

    def test_index_underlyings_is_frozenset(self) -> None:
        assert isinstance(INDEX_UNDERLYINGS, frozenset)
        assert len(INDEX_UNDERLYINGS) == 8

    def test_backwards_compat_aliases(self) -> None:
        """Old code using V1_UNDERLYINGS and ALLOWED_UNDERLYINGS still works."""
        assert V1_UNDERLYINGS is V1_INDICES
        assert ALLOWED_UNDERLYINGS is INDEX_UNDERLYINGS


class TestUnderlyingSpec:
    def test_str_repr(self) -> None:
        spec = V1_UNDERLYINGS[0]
        assert str(spec) == f"{spec.exchange}:{spec.underlying_symbol}"

    def test_frozen(self) -> None:
        spec = V1_UNDERLYINGS[0]
        with pytest.raises(Exception):
            spec.display_name = "something-else"  # type: ignore[misc]


# ────────────────────────────────────────────────────────────────────────
# is_in_scope — the adapter's filter gate
# ────────────────────────────────────────────────────────────────────────


def _make(
    *,
    exchange: Exchange = Exchange.NSE,
    instrument_type: InstrumentType = InstrumentType.EQ,
    trading_symbol: str = "RELIANCE",
    groww_symbol: str = "NSE-RELIANCE",
    underlying_symbol: str | None = None,
    segment: Segment = Segment.CASH,
) -> Instrument:
    """Tiny factory — only the fields that affect scope filtering vary."""
    return Instrument(
        exchange=exchange,
        exchange_token=1,
        trading_symbol=trading_symbol,
        groww_symbol=groww_symbol,
        name=None,
        instrument_type=instrument_type,
        segment=segment,
        series=None,
        isin=None,
        underlying_symbol=underlying_symbol,
        underlying_exchange_token=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        freeze_quantity=None,
        expiry_date=(
            date(2025, 12, 25)
            if instrument_type in {InstrumentType.FUT, InstrumentType.CE, InstrumentType.PE}
            else None
        ),
        strike_price=(
            Decimal("25000") if instrument_type in {InstrumentType.CE, InstrumentType.PE} else None
        ),
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


class TestIsInScopeSpotIndices:
    @pytest.mark.parametrize(
        ("exchange", "trading_symbol"),
        [
            (Exchange.NSE, "NIFTY"),
            (Exchange.NSE, "BANKNIFTY"),
            (Exchange.NSE, "FINNIFTY"),
            (Exchange.NSE, "MIDCPNIFTY"),
            (Exchange.NSE, "NIFTYNXT50"),
            (Exchange.BSE, "SENSEX"),
            (Exchange.BSE, "BANKEX"),
            (Exchange.BSE, "SENSEX50"),
        ],
    )
    def test_all_eight_spot_indices_in_scope(self, exchange: Exchange, trading_symbol: str) -> None:
        spot = _make(
            exchange=exchange,
            instrument_type=InstrumentType.IDX,
            trading_symbol=trading_symbol,
            groww_symbol=f"{exchange.value}-{trading_symbol}",
            segment=Segment.CASH,
        )
        assert is_in_scope(spot) is True

    def test_unknown_index_out_of_scope(self) -> None:
        spot = _make(
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.IDX,
            trading_symbol="NIFTYIT",
            groww_symbol="NSE-NIFTYIT",
            segment=Segment.CASH,
        )
        assert is_in_scope(spot) is False


class TestIsInScopeIndexDerivatives:
    @pytest.mark.parametrize(
        ("exchange", "underlying"),
        [
            (Exchange.NSE, "NIFTY"),
            (Exchange.NSE, "BANKNIFTY"),
            (Exchange.BSE, "SENSEX"),
            (Exchange.BSE, "BANKEX"),
        ],
    )
    def test_index_futures_in_scope(self, exchange: Exchange, underlying: str) -> None:
        fut = _make(
            exchange=exchange,
            instrument_type=InstrumentType.FUT,
            trading_symbol=f"{underlying}25DECFUT",
            groww_symbol=f"{exchange.value}-{underlying}-Dec25-FUT",
            underlying_symbol=underlying,
            segment=Segment.FNO,
        )
        assert is_in_scope(fut) is True

    @pytest.mark.parametrize("side", [InstrumentType.CE, InstrumentType.PE])
    def test_index_options_in_scope(self, side: InstrumentType) -> None:
        opt = _make(
            exchange=Exchange.NSE,
            instrument_type=side,
            trading_symbol=f"NIFTY25DEC25000{side.value}",
            groww_symbol=f"NSE-NIFTY-25Dec25-25000-{side.value}",
            underlying_symbol="NIFTY",
            segment=Segment.FNO,
        )
        assert is_in_scope(opt) is True


class TestIsInScopeStockDerivatives:
    """v1.0.1 scope expansion — stock F&O is now IN scope."""

    @pytest.mark.parametrize(
        "stock",
        ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "TATAMOTORS"],
    )
    def test_stock_futures_in_scope(self, stock: str) -> None:
        fut = _make(
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.FUT,
            trading_symbol=f"{stock}25DECFUT",
            groww_symbol=f"NSE-{stock}-Dec25-FUT",
            underlying_symbol=stock,
            segment=Segment.FNO,
        )
        assert is_in_scope(fut) is True

    @pytest.mark.parametrize(
        ("stock", "strike"),
        [("RELIANCE", 3000), ("TCS", 4000), ("HDFCBANK", 1700)],
    )
    def test_stock_options_in_scope(self, stock: str, strike: int) -> None:
        opt = _make(
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.CE,
            trading_symbol=f"{stock}25DEC{strike}CE",
            groww_symbol=f"NSE-{stock}-25Dec25-{strike}-CE",
            underlying_symbol=stock,
            segment=Segment.FNO,
        )
        assert is_in_scope(opt) is True


class TestIsInScopeRejects:
    def test_cash_equity_out_of_scope(self) -> None:
        """Cash equity standalone backtests are still v1.1."""
        eq = _make()
        assert is_in_scope(eq) is False

    def test_non_v1_index_out_of_scope(self) -> None:
        """NIFTY IT is an index but not one of the 8 F&O-tradeable ones."""
        spot = _make(
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.IDX,
            trading_symbol="NIFTYIT",
            groww_symbol="NSE-NIFTYIT",
            segment=Segment.CASH,
        )
        assert is_in_scope(spot) is False


# ────────────────────────────────────────────────────────────────────────
# classify_underlying_kind — "index" vs "stock"
# ────────────────────────────────────────────────────────────────────────


class TestClassifyUnderlyingKind:
    def test_nifty_option_is_index(self) -> None:
        opt = _make(
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.CE,
            trading_symbol="NIFTY25DEC25000CE",
            groww_symbol="NSE-NIFTY-25Dec25-25000-CE",
            underlying_symbol="NIFTY",
            segment=Segment.FNO,
        )
        assert classify_underlying_kind(opt) == "index"

    def test_sensex_option_is_index(self) -> None:
        opt = _make(
            exchange=Exchange.BSE,
            instrument_type=InstrumentType.PE,
            trading_symbol="SENSEX25SEP79500PE",
            groww_symbol="BSE-SENSEX-25Sep25-79500-PE",
            underlying_symbol="SENSEX",
            segment=Segment.FNO,
        )
        assert classify_underlying_kind(opt) == "index"

    def test_reliance_option_is_stock(self) -> None:
        opt = _make(
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.CE,
            trading_symbol="RELIANCE25DEC3000CE",
            groww_symbol="NSE-RELIANCE-25Dec25-3000-CE",
            underlying_symbol="RELIANCE",
            segment=Segment.FNO,
        )
        assert classify_underlying_kind(opt) == "stock"

    def test_tcs_future_is_stock(self) -> None:
        fut = _make(
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.FUT,
            trading_symbol="TCS25DECFUT",
            groww_symbol="NSE-TCS-Dec25-FUT",
            underlying_symbol="TCS",
            segment=Segment.FNO,
        )
        assert classify_underlying_kind(fut) == "stock"

    def test_raises_on_equity(self) -> None:
        eq = _make()
        with pytest.raises(ValueError, match="not an FNO derivative"):
            classify_underlying_kind(eq)

    def test_raises_on_index_spot(self) -> None:
        spot = _make(
            exchange=Exchange.NSE,
            instrument_type=InstrumentType.IDX,
            trading_symbol="NIFTY",
            groww_symbol="NSE-NIFTY",
            segment=Segment.CASH,
        )
        with pytest.raises(ValueError, match="not an FNO derivative"):
            classify_underlying_kind(spot)


# ────────────────────────────────────────────────────────────────────────
# is_index_underlying
# ────────────────────────────────────────────────────────────────────────


class TestIsIndexUnderlying:
    @pytest.mark.parametrize(
        ("exchange", "symbol"),
        [
            (Exchange.NSE, "NIFTY"),
            (Exchange.NSE, "BANKNIFTY"),
            (Exchange.BSE, "SENSEX"),
            (Exchange.BSE, "BANKEX"),
            (Exchange.BSE, "SENSEX50"),
        ],
    )
    def test_named_indices(self, exchange: Exchange, symbol: str) -> None:
        assert is_index_underlying(exchange, symbol) is True

    @pytest.mark.parametrize(
        ("exchange", "symbol"),
        [
            (Exchange.NSE, "RELIANCE"),
            (Exchange.NSE, "TCS"),
            (Exchange.NSE, "NIFTYIT"),
            (Exchange.BSE, "NIFTY"),  # wrong exchange
            (Exchange.NSE, "SENSEX"),  # wrong exchange
        ],
    )
    def test_stocks_and_wrong_combos(self, exchange: Exchange, symbol: str) -> None:
        assert is_index_underlying(exchange, symbol) is False
