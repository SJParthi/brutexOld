"""GrowwMarketAdapter — the :class:`~brutex.adapters.protocol.MarketAdapter` impl.

DEC-DHAN-GROWW-REMIGRATE-001 (ADR-0009 §5). Composes the Groww leaf layer into
the market-neutral contract the orchestrator talks to: ``capabilities``,
``list_instruments``, ``list_derivatives``, ``fetch_ohlcv``. Groww is the live
vendor (ADR-0009) that the CLI/dashboard drive through this adapter.

Scope (CLAUDE.md §3): this is the Track-1 **spot** adapter — the brute-force
engine universe is the 3 indices + India VIX (reference-only), all spot, so
``has_derivatives=False`` and ``list_derivatives`` is an empty iterator (the
orchestrator skips the expiry/contract phases). The options lake (DEC-P0-063) is
a separate future surface, not wired here.

Everything is injected (the :class:`~brutex.adapters.groww.client.GrowwClient`
and the prebuilt :class:`~brutex.adapters.groww.instruments.InstrumentIndex`), so
``fetch_ohlcv`` is fully testable with a fake client — no live API.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Final

from brutex.adapters.groww.chunks import plan_candle_windows
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import (
    EARLIEST_HISTORY,
    GROWW_RATE_LIMIT_RPS_UNVERIFIED,
    INTERVAL_TO_GROWW,
    NATIVE_TIMEZONE,
)
from brutex.adapters.groww.instruments import InstrumentIndex
from brutex.adapters.groww.parser import parse_candle_rows
from brutex.adapters.protocol import (
    DerivativeContract,
    InstrumentPage,
    MarketCapabilities,
    single_instrument_page,
)
from brutex.data.models import CandleBar
from brutex.models import Exchange, OutOfScopeError

#: Capabilities of the Track-1 Groww spot adapter (CLAUDE.md §3 scope).
GROWW_CAPABILITIES: Final[MarketCapabilities] = MarketCapabilities(
    market_id="groww-nse-bse",
    display_name="Groww (NSE/BSE spot)",
    exchanges=frozenset({Exchange.NSE, Exchange.BSE}),
    has_derivatives=False,
    has_expiries=False,
    has_option_chains=False,
    supports_intraday=True,
    earliest_history=EARLIEST_HISTORY,
    native_timezone=NATIVE_TIMEZONE,
    rate_limit_rps=GROWW_RATE_LIMIT_RPS_UNVERIFIED,
    supported_intervals=frozenset(INTERVAL_TO_GROWW),
)


@dataclass(frozen=True, slots=True)
class GrowwMarketAdapter:
    """MarketAdapter over an injected Groww client + prebuilt instrument index."""

    client: GrowwClient
    index: InstrumentIndex

    @property
    def capabilities(self) -> MarketCapabilities:
        """Stable Track-1 spot capabilities (read once by the orchestrator)."""
        return GROWW_CAPABILITIES

    def list_instruments(self, *, cursor: str | None = None) -> InstrumentPage:
        """Return the whole in-scope universe in one page (static universe)."""
        return single_instrument_page(self.index.universe, cursor)

    def list_derivatives(
        self, *, underlying_symbol: str, exchange: Exchange
    ) -> Iterator[DerivativeContract]:
        """Spot adapter — no derivatives (the orchestrator skips Phase B/C).

        ``underlying_symbol`` / ``exchange`` are part of the protocol signature
        but unused here: the Track-1 engine universe is spot-only (CLAUDE.md §3).
        """
        del underlying_symbol, exchange  # spot scope — intentionally no contracts
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
        """Fetch OHLCV bars for *symbol* over ``[start, end]`` at *interval*.

        Resolves the instrument via the O(1) index (reconstructing the spot
        ``EXCHANGE-SYMBOL`` groww_symbol when a bare trading symbol is passed),
        tiles the range into Groww-window chunks, fetches + parses each, and
        concatenates. Index bars drop volume (no traded volume). Raises
        :class:`~brutex.models.OutOfScopeError` for an unknown symbol and
        ``ValueError`` for an unsupported interval (fail-loud, Golden Rule #1).
        """
        if interval not in INTERVAL_TO_GROWW:
            raise ValueError(f"unsupported Groww interval: {interval!r}")
        groww_symbol = symbol if "-" in symbol else f"{exchange.value}-{symbol}"
        instrument = self.index.by_groww_symbol.get(groww_symbol)
        if instrument is None:
            raise OutOfScopeError(f"symbol not in Groww universe: {groww_symbol}")

        groww_segment = "FNO" if instrument.is_derivative else "CASH"
        groww_interval = INTERVAL_TO_GROWW[interval]
        windows = plan_candle_windows(
            datetime.combine(start, time()), datetime.combine(end, time()), interval=interval
        )
        bars: list[CandleBar] = []
        for window in windows:
            payload = self.client.historical_candles(
                exchange=exchange.value,
                segment=groww_segment,
                groww_symbol=groww_symbol,
                interval=groww_interval,
                start=window.start,
                end=window.end,
            )
            bars.extend(parse_candle_rows(payload, drop_volume=instrument.is_index))
        return bars


__all__ = ["GROWW_CAPABILITIES", "GrowwMarketAdapter"]
