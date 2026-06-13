"""SyntheticMarketAdapter — an offline, deterministic :class:`MarketAdapter`.

DEC-SYNTHETIC-ADAPTER-001 (K2). A drop-in market adapter that needs **no Groww
credentials and no network**: it generates deterministic, seeded OHLCV bars so
the engine / ingest / dashboard can run end-to-end fully offline. It mirrors the
public surface of :class:`~brutex.adapters.groww.adapter.GrowwMarketAdapter`
(``capabilities`` / ``list_instruments`` / ``list_derivatives`` / ``fetch_ohlcv``)
so it slots into the same vendor-neutral spine.

Scope (CLAUDE.md §3): spot-only — ``has_derivatives=False`` and
``list_derivatives`` is an empty iterator, exactly like the live Track-1 spot
adapter (the orchestrator skips the expiry/contract phases). This keeps the
surface minimal and fully testable; a synthetic derivatives chain would add a
generation surface nothing in the engine path consumes.

HONESTY (Golden Rule #1/#10): the bars are GENERATED, never real. The
``market_id`` is ``"synthetic"`` and the ``display_name`` says so plainly — this
adapter must never be presented as a real market-data source.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Final

from brutex.adapters.protocol import (
    DerivativeContract,
    InstrumentPage,
    MarketCapabilities,
    single_instrument_page,
)
from brutex.adapters.synthetic.constants import (
    DEFAULT_SEED,
    DISPLAY_NAME,
    EARLIEST_HISTORY,
    MARKET_ID,
    NATIVE_TIMEZONE,
    RATE_LIMIT_RPS,
    SUPPORTED_INTERVALS,
)
from brutex.adapters.synthetic.generator import generate_candles
from brutex.adapters.synthetic.instruments import (
    SyntheticInstrumentIndex,
    build_synthetic_index,
)
from brutex.data.models import CandleBar
from brutex.models import Exchange, OutOfScopeError

#: Honest capabilities of the synthetic offline adapter (spot-only).
SYNTHETIC_CAPABILITIES: Final[MarketCapabilities] = MarketCapabilities(
    market_id=MARKET_ID,
    display_name=DISPLAY_NAME,
    exchanges=frozenset({Exchange.NSE, Exchange.BSE}),
    has_derivatives=False,
    has_expiries=False,
    has_option_chains=False,
    supports_intraday=True,
    earliest_history=EARLIEST_HISTORY,
    native_timezone=NATIVE_TIMEZONE,
    rate_limit_rps=RATE_LIMIT_RPS,
    supported_intervals=SUPPORTED_INTERVALS,
)


@dataclass(frozen=True, slots=True)
class SyntheticMarketAdapter:
    """MarketAdapter over a prebuilt synthetic index + an injected seed.

    The *seed* is an explicit field (never read from the wall clock or the
    environment), so a given adapter is fully reproducible: the same
    ``(seed, symbol, interval, start, end)`` always yields bit-identical bars.
    """

    index: SyntheticInstrumentIndex = field(default_factory=build_synthetic_index)
    seed: int = DEFAULT_SEED

    @property
    def capabilities(self) -> MarketCapabilities:
        """Stable offline spot capabilities (read once by the orchestrator)."""
        return SYNTHETIC_CAPABILITIES

    def list_instruments(self, *, cursor: str | None = None) -> InstrumentPage:
        """Return the whole synthetic universe in one page (static universe)."""
        return single_instrument_page(self.index.universe, cursor)

    def list_derivatives(
        self, *, underlying_symbol: str, exchange: Exchange
    ) -> Iterator[DerivativeContract]:
        """Spot adapter — no derivatives (the orchestrator skips Phase B/C)."""
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
        """Generate deterministic OHLCV bars for *symbol* over ``[start, end)``.

        Resolves the instrument via the O(1) index (reconstructing the
        ``EXCHANGE-SYMBOL`` key when a bare symbol is passed), then delegates to
        the seeded generator. Raises :class:`~brutex.models.OutOfScopeError` for
        an unknown symbol and ``ValueError`` for an unsupported interval
        (fail-loud, Golden Rule #1).
        """
        market_symbol = symbol if "-" in symbol else f"{exchange.value}-{symbol}"
        spec = self.index.spec_by_market_symbol.get(market_symbol)
        if spec is None:
            raise OutOfScopeError(f"symbol not in synthetic universe: {market_symbol}")
        return generate_candles(seed=self.seed, spec=spec, interval=interval, start=start, end=end)


__all__ = ["SYNTHETIC_CAPABILITIES", "SyntheticMarketAdapter"]
