"""Unit tests for the market-neutral adapter protocol.

Verifies that :class:`brutex.adapters.protocol.MarketAdapter` is truly
market-neutral — the same contract can be satisfied by:

* A spot-only adapter with no derivatives, paginated instruments,
  24/7 UTC timestamps (:class:`DemoSpotCryptoAdapter`)
* A perpetual-futures adapter with derivatives but no expiries
  (:class:`DemoPerpCryptoAdapter`)

If these tests pass, the protocol is architecturally sound for
adding Polygon (US equities + options), Binance (crypto perps),
Groww/Zerodha (Indian F&O, second source), IBKR, Coinbase, …
"""

from __future__ import annotations

from datetime import date

import pytest
from brutex.adapters.protocol import (
    DerivativeContract,
    InstrumentPage,
    MarketAdapter,
    MarketCapabilities,
    single_instrument_page,
)
from brutex.models import Exchange, Instrument

from tests.fakes.demo_crypto_adapter import (
    DemoPerpCryptoAdapter,
    DemoSpotCryptoAdapter,
)


class TestSingleInstrumentPage:
    """The shared static-universe ``list_instruments`` idiom (CLAUDE.md §11)."""

    def test_first_page_returns_all_instruments(self, nifty_option_instance: Instrument) -> None:
        page = single_instrument_page((nifty_option_instance,), None)
        assert page.instruments == (nifty_option_instance,)
        assert page.next_cursor is None

    def test_non_none_cursor_is_exhausted(self, nifty_option_instance: Instrument) -> None:
        page = single_instrument_page((nifty_option_instance,), "next")
        assert page.instruments == ()
        assert page.next_cursor is None


# ────────────────────────────────────────────────────────────────────────
# MarketCapabilities validation
# ────────────────────────────────────────────────────────────────────────


class TestMarketCapabilities:
    def test_happy_path(self) -> None:
        caps = MarketCapabilities(
            market_id="nse-fno",
            display_name="NSE F&O",
            exchanges=frozenset({Exchange.NSE}),
            has_derivatives=True,
            has_expiries=True,
            has_option_chains=True,
            supports_intraday=True,
            earliest_history=date(2020, 1, 1),
            native_timezone="Asia/Kolkata",
            rate_limit_rps=15.0,
        )
        assert caps.market_id == "nse-fno"
        assert caps.has_option_chains

    def test_rejects_empty_market_id(self) -> None:
        with pytest.raises(ValueError, match="market_id"):
            MarketCapabilities(
                market_id="",
                display_name="x",
                exchanges=frozenset({Exchange.NSE}),
                has_derivatives=False,
                has_expiries=False,
                has_option_chains=False,
                supports_intraday=True,
                earliest_history=date(2020, 1, 1),
                native_timezone="UTC",
                rate_limit_rps=1.0,
            )

    def test_rejects_slash_in_market_id(self) -> None:
        with pytest.raises(ValueError, match="market_id"):
            MarketCapabilities(
                market_id="nse/fno",
                display_name="x",
                exchanges=frozenset({Exchange.NSE}),
                has_derivatives=False,
                has_expiries=False,
                has_option_chains=False,
                supports_intraday=True,
                earliest_history=date(2020, 1, 1),
                native_timezone="UTC",
                rate_limit_rps=1.0,
            )

    def test_rejects_expiries_without_derivatives(self) -> None:
        with pytest.raises(ValueError, match="has_expiries.*has_derivatives"):
            MarketCapabilities(
                market_id="x",
                display_name="x",
                exchanges=frozenset({Exchange.NSE}),
                has_derivatives=False,
                has_expiries=True,  # invalid combo
                has_option_chains=False,
                supports_intraday=True,
                earliest_history=date(2020, 1, 1),
                native_timezone="UTC",
                rate_limit_rps=1.0,
            )

    def test_rejects_option_chains_without_derivatives(self) -> None:
        with pytest.raises(ValueError, match="has_option_chains.*has_derivatives"):
            MarketCapabilities(
                market_id="x",
                display_name="x",
                exchanges=frozenset({Exchange.NSE}),
                has_derivatives=False,
                has_expiries=False,
                has_option_chains=True,  # invalid combo
                supports_intraday=True,
                earliest_history=date(2020, 1, 1),
                native_timezone="UTC",
                rate_limit_rps=1.0,
            )

    def test_rejects_non_positive_rate_limit(self) -> None:
        with pytest.raises(ValueError, match="rate_limit_rps"):
            MarketCapabilities(
                market_id="x",
                display_name="x",
                exchanges=frozenset({Exchange.NSE}),
                has_derivatives=False,
                has_expiries=False,
                has_option_chains=False,
                supports_intraday=True,
                earliest_history=date(2020, 1, 1),
                native_timezone="UTC",
                rate_limit_rps=0.0,  # invalid
            )

    def test_is_frozen(self) -> None:
        caps = MarketCapabilities(
            market_id="x",
            display_name="x",
            exchanges=frozenset({Exchange.NSE}),
            has_derivatives=False,
            has_expiries=False,
            has_option_chains=False,
            supports_intraday=True,
            earliest_history=date(2020, 1, 1),
            native_timezone="UTC",
            rate_limit_rps=1.0,
        )
        with pytest.raises((AttributeError, TypeError)):
            caps.market_id = "y"  # type: ignore[misc]


# ────────────────────────────────────────────────────────────────────────
# DerivativeContract validation
# ────────────────────────────────────────────────────────────────────────


class TestDerivativeContract:
    def test_futures_contract(self) -> None:
        c = DerivativeContract(
            underlying_symbol="NIFTY",
            expiry=date(2025, 1, 30),
            symbol="NIFTY25JAN-FUT",
            contract_type="FUT",
        )
        assert c.contract_type == "FUT"
        assert c.strike is None

    def test_call_option_requires_strike(self) -> None:
        with pytest.raises(ValueError, match="strike"):
            DerivativeContract(
                underlying_symbol="NIFTY",
                expiry=date(2025, 1, 30),
                symbol="NIFTY25JAN22000CE",
                contract_type="CE",
                strike=None,  # missing
            )

    def test_put_option_requires_strike(self) -> None:
        with pytest.raises(ValueError, match="strike"):
            DerivativeContract(
                underlying_symbol="NIFTY",
                expiry=date(2025, 1, 30),
                symbol="NIFTY25JAN22000PE",
                contract_type="PE",
                strike=None,
            )

    def test_perpetual_must_have_null_expiry(self) -> None:
        with pytest.raises(ValueError, match="PERP.*expiry"):
            DerivativeContract(
                underlying_symbol="BTC",
                expiry=date(2025, 1, 30),  # PERP should have None
                symbol="BTC-PERP",
                contract_type="PERP",
            )

    def test_rejects_unknown_contract_type(self) -> None:
        with pytest.raises(ValueError, match="contract_type"):
            DerivativeContract(
                underlying_symbol="NIFTY",
                expiry=date(2025, 1, 30),
                symbol="NIFTY-X",
                contract_type="SWAP",  # not in the enum
            )


# ────────────────────────────────────────────────────────────────────────
# DemoSpotCryptoAdapter satisfies MarketAdapter
# ────────────────────────────────────────────────────────────────────────


class TestSpotCryptoAdapter:
    @pytest.fixture
    def adapter(self) -> DemoSpotCryptoAdapter:
        return DemoSpotCryptoAdapter()

    def test_protocol_satisfaction(self, adapter: DemoSpotCryptoAdapter) -> None:
        """isinstance check via @runtime_checkable."""
        assert isinstance(adapter, MarketAdapter)

    def test_capabilities_are_spot_only(self, adapter: DemoSpotCryptoAdapter) -> None:
        caps = adapter.capabilities
        assert caps.market_id == "demo-crypto-spot"
        assert caps.has_derivatives is False
        assert caps.has_expiries is False
        assert caps.has_option_chains is False

    def test_pagination_first_page(self, adapter: DemoSpotCryptoAdapter) -> None:
        page = adapter.list_instruments(cursor=None)
        assert isinstance(page, InstrumentPage)
        assert len(page.instruments) == 4  # page_size default
        assert page.next_cursor is not None

    def test_pagination_walks_to_end(self, adapter: DemoSpotCryptoAdapter) -> None:
        """Walking through all cursors must enumerate every instrument
        exactly once with no duplicates and no gaps."""
        seen: set[str] = set()
        cursor: str | None = None
        pages = 0
        while True:
            page = adapter.list_instruments(cursor=cursor)
            pages += 1
            for inst in page.instruments:
                assert inst.trading_symbol not in seen, (
                    f"duplicate {inst.trading_symbol} across pages"
                )
                seen.add(inst.trading_symbol)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert len(seen) == 10  # _SPOT_PAIRS length
        assert pages >= 2  # paginated, not one-shot

    def test_list_derivatives_is_empty(self, adapter: DemoSpotCryptoAdapter) -> None:
        """Spot-only market has no derivatives — iterator must be empty."""
        contracts = list(
            adapter.list_derivatives(
                underlying_symbol="BTC-USDT",
                exchange=Exchange.NSE,
            )
        )
        assert contracts == []

    def test_fetch_ohlcv_returns_utc_bars(self, adapter: DemoSpotCryptoAdapter) -> None:
        bars = adapter.fetch_ohlcv(
            symbol="BTC-USDT",
            exchange=Exchange.NSE,
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 1),
        )
        assert len(bars) == 1
        bar = bars[0]
        assert bar.timestamp.tzinfo is not None
        utcoffset = bar.timestamp.utcoffset()
        assert utcoffset is not None
        assert utcoffset.total_seconds() == 0
        # Spot bars have no open interest
        assert bar.open_interest is None


# ────────────────────────────────────────────────────────────────────────
# DemoPerpCryptoAdapter satisfies MarketAdapter
# ────────────────────────────────────────────────────────────────────────


class TestPerpCryptoAdapter:
    @pytest.fixture
    def adapter(self) -> DemoPerpCryptoAdapter:
        return DemoPerpCryptoAdapter()

    def test_protocol_satisfaction(self, adapter: DemoPerpCryptoAdapter) -> None:
        assert isinstance(adapter, MarketAdapter)

    def test_capabilities_are_perp(self, adapter: DemoPerpCryptoAdapter) -> None:
        caps = adapter.capabilities
        assert caps.market_id == "demo-crypto-perps"
        # Perpetuals have derivatives WITHOUT expiries — this is the
        # corner case the protocol must support cleanly.
        assert caps.has_derivatives is True
        assert caps.has_expiries is False
        assert caps.has_option_chains is False

    def test_yields_perpetual_contract(self, adapter: DemoPerpCryptoAdapter) -> None:
        contracts = list(
            adapter.list_derivatives(
                underlying_symbol="BTC",
                exchange=Exchange.NSE,
            )
        )
        assert len(contracts) == 1
        c = contracts[0]
        assert c.contract_type == "PERP"
        assert c.expiry is None
        assert c.strike is None
        assert c.symbol == "BTC-PERP"

    def test_perp_fetch_returns_oi(self, adapter: DemoPerpCryptoAdapter) -> None:
        """Perpetuals have open interest even though they don't expire."""
        bars = adapter.fetch_ohlcv(
            symbol="BTC-PERP",
            exchange=Exchange.NSE,
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 1, 1),
        )
        assert len(bars) == 1
        assert bars[0].open_interest == 50000


# ────────────────────────────────────────────────────────────────────────
# Architectural invariants
# ────────────────────────────────────────────────────────────────────────


class TestArchitecturalInvariants:
    """These are the guarantees the protocol makes the user is asking about."""

    def test_both_adapters_use_same_protocol(self) -> None:
        """Both adapter shapes satisfy the SAME protocol — proving the
        abstraction is actually market-neutral."""
        spot = DemoSpotCryptoAdapter()
        perp = DemoPerpCryptoAdapter()
        assert isinstance(spot, MarketAdapter)
        assert isinstance(perp, MarketAdapter)

    def test_orchestrator_can_branch_on_capabilities(self) -> None:
        """The orchestrator inspects capabilities — NOT the adapter
        class — to decide which phases to run.

        This test documents the contract: any code that checks
        ``adapter.capabilities.has_derivatives`` is doing the right thing
        and will work for every current and future market.
        """
        spot = DemoSpotCryptoAdapter()
        perp = DemoPerpCryptoAdapter()

        # Phase B/C should run iff has_derivatives
        assert spot.capabilities.has_derivatives is False
        assert perp.capabilities.has_derivatives is True

        # Phase B (expiry enumeration) should run iff has_expiries
        assert spot.capabilities.has_expiries is False
        assert perp.capabilities.has_expiries is False

        # Phase D always runs — every market has bars
        assert spot.capabilities.supports_intraday
        assert perp.capabilities.supports_intraday
