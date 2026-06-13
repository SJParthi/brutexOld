"""GrowwMarketAdapter — the :class:`~brutex.adapters.protocol.MarketAdapter` impl.

DEC-DHAN-GROWW-REMIGRATE-001 (ADR-0009 §5). Composes the Groww leaf layer into
the market-neutral contract the orchestrator talks to: ``capabilities``,
``list_instruments``, ``list_derivatives``, ``fetch_ohlcv``. Groww is the live
vendor (ADR-0009) that the CLI/dashboard drive through this adapter.

Scope (CLAUDE.md §3): this is the Track-1 **spot** adapter — the brute-force
engine universe is the 3 indices + India VIX (reference-only), all spot, so
:data:`GROWW_CAPABILITIES` keeps ``has_derivatives=False``: that flag describes
the brute-force ENGINE scope, NOT the Groww market (which does have F&O). The
engine builds its worklist from an explicit spot instrument list, never from
``list_derivatives`` (nothing reads ``has_derivatives`` to fan options into the
engine), so the flag and the enumerator below are consistent in practice.

``list_derivatives`` IS fleshed out (DEC-GROWW-OPTION-INGEST-001) — it enumerates
the **LAKE / REFERENCE_ONLY** option+future chain (DEC-P0-063) via the SDK's
expired-contract addressing, ready for the option lake pull
(:mod:`brutex.adapters.groww.option_ingest`). Spot ``fetch_ohlcv`` and the
index-free option fetch share ONE tiling-fetch primitive
(:func:`~brutex.adapters.groww.candles.fetch_candle_windows`, CLAUDE.md §11 COMMON).

Everything is injected (the :class:`~brutex.adapters.groww.client.GrowwClient`
and the prebuilt :class:`~brutex.adapters.groww.instruments.InstrumentIndex`), so
``fetch_ohlcv`` + ``list_derivatives`` are fully testable with a fake client — no
live API.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Final

from brutex.adapters.groww.candles import fetch_candle_windows
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import (
    EARLIEST_HISTORY,
    GROWW_RATE_LIMIT_RPS_UNVERIFIED,
    INTERVAL_TO_GROWW,
    NATIVE_TIMEZONE,
)
from brutex.adapters.groww.instruments import InstrumentIndex
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
        """Yield every Groww derivative contract for *underlying_symbol* (LAKE / reference-only).

        Resolves the full FUT/CE/PE chain via the SDK's expired-contract
        addressing (``get_expiries`` per historical year → ``get_contracts`` per
        expiry → the listed Groww symbols, parsed by
        :func:`~brutex.adapters.groww.contracts.parse_contract_symbol`) —
        DEC-GROWW-OPTION-INGEST-001. The year span is the documented FNO history
        (:data:`~brutex.adapters.groww.constants.EARLIEST_HISTORY` → the current
        calendar year), so one call enumerates every contract the vendor lists.

        **Scope (CLAUDE.md §3):** this is the **LAKE / REFERENCE_ONLY** surface
        (DEC-P0-063) — the BRUTE-FORCE ENGINE is spot-only and NEVER ingests these
        contracts (which is why :data:`GROWW_CAPABILITIES` keeps
        ``has_derivatives=False``; the engine's worklist is an explicit spot
        instrument list, never this enumerator).

        **Honest envelope (Golden Rule #10):** network-bound — one ``get_expiries``
        per year + one ``get_contracts`` per discovered expiry — NOT O(1)-total; the
        per-call rate gate is the injected ``client.pace`` (the option pull wires the
        shared :class:`~brutex.adapters.rate_common.RateController`).
        """
        # Local import avoids a module-load cycle (contracts → client; this method
        # is paid once per enumeration call, never on a hot path).
        from brutex.adapters.groww.contracts import enumerate_contracts

        years = range(EARLIEST_HISTORY.year, date.today().year + 1)
        return enumerate_contracts(
            self.client,
            exchange=exchange.value,
            underlying_symbol=underlying_symbol,
            years=years,
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
        """Fetch OHLCV bars for *symbol* over ``[start, end]`` at *interval*.

        Resolves the instrument via the O(1) index (reconstructing the spot
        ``EXCHANGE-SYMBOL`` groww_symbol when a bare trading symbol is passed),
        then delegates the tile → fetch → parse loop to the shared
        :func:`~brutex.adapters.groww.candles.fetch_candle_windows` (CLAUDE.md §11
        COMMON). Index bars drop volume (no traded volume). Raises
        :class:`~brutex.models.OutOfScopeError` for an unknown symbol and
        ``ValueError`` for an unsupported interval (fail-loud, Golden Rule #1).
        """
        groww_symbol = symbol if "-" in symbol else f"{exchange.value}-{symbol}"
        instrument = self.index.by_groww_symbol.get(groww_symbol)
        if instrument is None:
            raise OutOfScopeError(f"symbol not in Groww universe: {groww_symbol}")
        return fetch_candle_windows(
            self.client,
            exchange=exchange.value,
            segment="FNO" if instrument.is_derivative else "CASH",
            groww_symbol=groww_symbol,
            interval=interval,
            start=start,
            end=end,
            drop_volume=instrument.is_index,
        )


__all__ = ["GROWW_CAPABILITIES", "GrowwMarketAdapter"]
