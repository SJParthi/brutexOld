"""A fake crypto-spot adapter that satisfies :class:`MarketAdapter`.

This exists to **prove** the market-neutral protocol works for shapes
that are nothing like Groww/F&O:

* No expiries — crypto spot is continuous
* No option chains — just spot pairs
* Dynamic universe — paginated instrument list
* 24/7 market — no session cutoffs
* UTC-native timestamps — no timezone conversion needed
* Perpetual futures shape as a second test — has derivatives but no
  expiries

If these tests pass, the protocol is market-neutral. If they fail
(i.e. the protocol forces F&O-shaped fields on a spot market), we
know the abstraction is leaky and needs fixing.

This module lives under ``tests/fakes/`` and is not shipped in the
production package — it's a test-only artifact.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from brutex.adapters.protocol import (
    DerivativeContract,
    InstrumentPage,
    MarketAdapter,
    MarketCapabilities,
)
from brutex.data.models import CandleBar
from brutex.models import (
    Exchange,
    Instrument,
    InstrumentType,
    Segment,
)

# ────────────────────────────────────────────────────────────────────────
# DemoSpotCryptoAdapter — no derivatives, paginated, 24/7
# ────────────────────────────────────────────────────────────────────────


_SPOT_PAIRS: tuple[str, ...] = (
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT",
    "XRP-USDT",
    "DOGE-USDT",
    "ADA-USDT",
    "AVAX-USDT",
    "DOT-USDT",
    "LINK-USDT",
    "LTC-USDT",
)


@dataclass
class DemoSpotCryptoAdapter:
    """A minimal spot-crypto adapter with no derivatives."""

    page_size: int = 4
    _capabilities: MarketCapabilities = field(init=False)

    def __post_init__(self) -> None:
        self._capabilities = MarketCapabilities(
            market_id="demo-crypto-spot",
            display_name="Demo Crypto Spot",
            exchanges=frozenset({Exchange.NSE}),  # Using NSE as a stand-in
            has_derivatives=False,
            has_expiries=False,
            has_option_chains=False,
            supports_intraday=True,
            earliest_history=date(2020, 1, 1),
            native_timezone="UTC",
            rate_limit_rps=10.0,
            supported_intervals=frozenset({"1minute", "5minute", "1hour", "1day"}),
        )

    @property
    def capabilities(self) -> MarketCapabilities:
        return self._capabilities

    def list_instruments(self, *, cursor: str | None = None) -> InstrumentPage:
        """Return one page of spot pairs.

        Paginated to exercise the cursor path — static markets can
        just return everything in one page with ``next_cursor=None``.
        """
        start_idx = int(cursor) if cursor else 0
        end_idx = start_idx + self.page_size
        page_pairs = _SPOT_PAIRS[start_idx:end_idx]

        instruments = tuple(
            Instrument(
                exch_id=Exchange.NSE,  # stand-in
                security_id=start_idx + i,
                symbol_name=pair,
                display_name=pair,
                instrument=InstrumentType.EQ,  # closest fit
                segment=Segment.CASH,
                series=None,
                isin=None,
                underlying_symbol=None,
                underlying_security_id=None,
                lot_size=1,
                tick_size=Decimal("0.00000001"),
                sm_freeze_qty=None,
                sm_expiry_date=None,
                strike_price=None,
                asm_gsm_flag=False,
            )
            for i, pair in enumerate(page_pairs)
        )

        next_cursor = str(end_idx) if end_idx < len(_SPOT_PAIRS) else None
        return InstrumentPage(instruments=instruments, next_cursor=next_cursor)

    def list_derivatives(
        self, *, underlying_symbol: str, exchange: Exchange
    ) -> Iterator[DerivativeContract]:
        """Spot-only — always empty.

        Proves that the protocol gracefully handles markets with no
        derivatives. The orchestrator should skip Phase B/C entirely
        based on ``capabilities.has_derivatives=False``.
        """
        del underlying_symbol, exchange
        return iter(())

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        exchange: Exchange,
        interval: str,
        start: date,
        end: date,
    ) -> list[CandleBar]:
        """Generate deterministic fake bars.

        Timestamps are already UTC (crypto markets are 24/7 UTC-native),
        so no timezone conversion happens.
        """
        del exchange, start, end  # Simplified: one bar per call
        # One bar with Decimal prices, positive volume, zero OI
        return [
            CandleBar(
                timestamp=datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC),
                open=Decimal("42000.00"),
                high=Decimal("42500.00"),
                low=Decimal("41900.00"),
                close=Decimal("42300.00"),
                volume=1500,
                open_interest=None,
            ),
        ]


# ────────────────────────────────────────────────────────────────────────
# DemoPerpCryptoAdapter — has derivatives but no expiries (perpetual)
# ────────────────────────────────────────────────────────────────────────


@dataclass
class DemoPerpCryptoAdapter:
    """Perpetual-futures adapter — derivatives without expiries.

    This is the interesting corner case: ``has_derivatives=True`` AND
    ``has_expiries=False``. The protocol has to support this shape
    cleanly (Binance, Bybit, dYdX, Hyperliquid all use perps).
    """

    _capabilities: MarketCapabilities = field(init=False)

    def __post_init__(self) -> None:
        self._capabilities = MarketCapabilities(
            market_id="demo-crypto-perps",
            display_name="Demo Crypto Perpetuals",
            exchanges=frozenset({Exchange.NSE}),  # stand-in
            has_derivatives=True,
            has_expiries=False,  # perpetuals never expire
            has_option_chains=False,
            supports_intraday=True,
            earliest_history=date(2022, 1, 1),
            native_timezone="UTC",
            rate_limit_rps=20.0,
        )

    @property
    def capabilities(self) -> MarketCapabilities:
        return self._capabilities

    def list_instruments(self, *, cursor: str | None = None) -> InstrumentPage:
        del cursor
        return InstrumentPage(instruments=(), next_cursor=None)

    def list_derivatives(
        self, *, underlying_symbol: str, exchange: Exchange
    ) -> Iterator[DerivativeContract]:
        """Yield one perpetual contract per underlying.

        Perpetuals have ``expiry=None`` and ``contract_type="PERP"``.
        """
        del exchange
        yield DerivativeContract(
            underlying_symbol=underlying_symbol,
            expiry=None,
            symbol=f"{underlying_symbol}-PERP",
            contract_type="PERP",
            strike=None,
        )

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        exchange: Exchange,
        interval: str,
        start: date,
        end: date,
    ) -> list[CandleBar]:
        del exchange, start, end
        return [
            CandleBar(
                timestamp=datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC),
                open=Decimal("42000.00"),
                high=Decimal("42500.00"),
                low=Decimal("41900.00"),
                close=Decimal("42300.00"),
                volume=10000,
                open_interest=50000,  # perps have OI
            ),
        ]


# Protocol satisfaction is enforced at test time via isinstance()
# checks — both adapters must pass ``isinstance(adapter, MarketAdapter)``.
_: MarketAdapter = DemoSpotCryptoAdapter()  # type: ignore[misc]
_ = DemoPerpCryptoAdapter()  # type: ignore[misc]
