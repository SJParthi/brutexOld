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
from dataclasses import dataclass, field
from datetime import date
from typing import Final

from brutex.adapters.groww.candles import RawPayloadHook, fetch_candle_windows
from brutex.adapters.groww.client import GrowwClientProtocol
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
    """MarketAdapter over an injected Groww client + prebuilt instrument index.

    ``client`` is the :class:`~brutex.adapters.groww.client.GrowwClientProtocol` — EITHER a
    single :class:`~brutex.adapters.groww.client.GrowwClient` OR a multi-connection
    :class:`~brutex.adapters.groww.pool.ClientPool` (DEC-GROWW-CLIENT-POOL-001); the adapter
    is identical for both (it only calls ``historical_candles`` / ``expiries`` / ``contracts``
    and exposes ``client.meter`` for the rate panel).

    ``on_raw_payload`` is the OPTIONAL raw-data integrity capture hook
    (DEC-RAW-DATA-INTEGRITY-001): when injected, every spot ``fetch_ohlcv`` window
    hands its RAW vendor payload (BEFORE parse) + the issued coordinates to the
    hook (which hashes + persists the sidecar record, best-effort). ``None`` (the
    default) keeps the adapter byte-identical to the pre-feature behaviour —
    purely additive, no protocol change (it is excluded from eq/repr like the
    client's ``meter``: it is shared observability state, not adapter identity).
    """

    client: GrowwClientProtocol
    index: InstrumentIndex
    on_raw_payload: RawPayloadHook | None = field(default=None, compare=False, repr=False)

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

        Resolves the instrument MASTER-AUTHORITATIVELY via the O(1) index
        (DEC-GROWW-HYPHEN-SYMBOL-RESOLVE-001), then delegates the tile → fetch →
        parse loop to the shared
        :func:`~brutex.adapters.groww.candles.fetch_candle_windows` (CLAUDE.md §11
        COMMON). Index bars drop volume (no traded volume). Raises
        :class:`~brutex.models.OutOfScopeError` for an unknown symbol and
        ``ValueError`` for an unsupported interval (fail-loud, Golden Rule #1).

        **Resolution (the BAJAJ-AUTO fix):** a BARE spot/index trading symbol —
        INCLUDING a HYPHENATED single-stock ticker like ``BAJAJ-AUTO`` — resolves via
        the vendor's own ``(exchange, trading_symbol)`` index, and the groww_symbol
        handed to the vendor is the master's REAL ``groww_symbol_by_pair`` value (NOT
        a reconstructed ``EXCH-symbol``) — so a VERBOSE-symbol index like
        ``MIDCPNIFTY`` (``trading_symbol`` ``"NIFTY MID SELECT"`` ≠ the ``MIDCPNIFTY``
        component of ``NSE-MIDCPNIFTY``) sends the correct ``NSE-MIDCPNIFTY``, never a
        garbage ``NSE-NIFTY MID SELECT``. An ALREADY-PREFIXED groww_symbol (a
        ``FUT``/``CE``/``PE`` contract, e.g. ``NSE-NIFTY-25Aug26-FUT``, whose
        ``trading_symbol`` is ``NIFTY25Aug26FUT``) misses the ``(exchange, symbol)``
        index and resolves via ``by_groww_symbol``, used AS-IS. The prior
        ``"-" in symbol`` heuristic mistook the hyphen in ``BAJAJ-AUTO`` for an
        already-prefixed symbol, looked the bare ticker up in ``by_groww_symbol``,
        missed, and raised — leaving every hyphenated F&O stock perpetually
        quarantined (the futile case DEC-UNTIL-CLEAN-ALL-OUTSTANDING-001 #844
        deferred). Resolving + sending by the vendor's own keys never reconstructs off
        a guess (Golden Rule #1).
        """
        instrument = self.index.by_exchange_symbol.get((exchange, symbol))
        if instrument is not None:
            # A bare spot/index trading symbol (incl. a hyphenated stock) — send the
            # master's REAL groww_symbol (in lockstep with by_exchange_symbol), NEVER
            # a reconstructed EXCH-symbol (wrong for a verbose-symbol index).
            groww_symbol = self.index.groww_symbol_by_pair[(exchange, symbol)]
        else:
            # An already-prefixed groww_symbol (a FUT/CE/PE contract) — used as-is.
            instrument = self.index.by_groww_symbol.get(symbol)
            groww_symbol = symbol
        if instrument is None:
            raise OutOfScopeError(f"symbol not in Groww universe: {symbol}")
        return fetch_candle_windows(
            self.client,
            exchange=exchange.value,
            segment="FNO" if instrument.is_derivative else "CASH",
            groww_symbol=groww_symbol,
            interval=interval,
            start=start,
            end=end,
            drop_volume=instrument.is_index,
            on_raw_payload=self.on_raw_payload,
        )


__all__ = ["GROWW_CAPABILITIES", "GrowwMarketAdapter"]
