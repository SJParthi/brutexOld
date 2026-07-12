"""DhanMarketAdapter — the :class:`~brutex.adapters.protocol.MarketAdapter` impl.

DEC-DHAN-SPOT-PULL-001 (ADR-0022). Composes the Dhan leaf layer into the COMMON
market-neutral contract (Golden Rule #11 — the SAME
``fetch_ohlcv(*, symbol, exchange, interval, start, end) -> list[CandleBar]`` waist
Groww satisfies, NOT a rival Protocol). Scope: SPOT indices ONLY on the shared
``IDX_I`` / ``INDEX`` addressing at 1-minute granularity (NIFTY / BANKNIFTY / SENSEX
+ INDIA VIX reference-only) — ``has_derivatives=False``.

``fetch_ohlcv`` resolves the symbol's securityId from the injected
``security_ids`` map (built once by :func:`~brutex.adapters.dhan.scrip_master.\
resolve_security_ids`), tiles the range into ≤ 90-day windows
(:func:`~brutex.adapters.dhan.chunks.plan_candle_windows`), fetches each via the
injected :class:`~brutex.adapters.dhan.client.DhanClientProtocol`, and parses the
parallel-column payload (:func:`~brutex.adapters.dhan.parser.parse_candle_columns`,
``drop_volume=True`` — an index has no traded volume). An UNRESOLVED symbol raises
:class:`~brutex.models.OutOfScopeError` (defensive — the ingest driver loud-skips
unresolved symbols BEFORE the spine loop, so this never fires in a healthy pull, but
guards against a mis-wired call; Golden Rule #1 never fabricates a blind id).

Everything is injected (the client + the ``security_ids`` map), so ``fetch_ohlcv`` is
fully testable with a fake client + no live API.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Final

from brutex.adapters.dhan.chunks import plan_candle_windows
from brutex.adapters.dhan.client import DhanClientProtocol
from brutex.adapters.dhan.constants import (
    DHAN_FETCH_INTERVAL,
    DHAN_RATE_LIMIT_RPS,
    INSTRUMENT_INDEX,
    NATIVE_TIMEZONE,
    SEGMENT_INDEX,
)
from brutex.adapters.dhan.parser import parse_candle_columns
from brutex.adapters.protocol import (
    DerivativeContract,
    InstrumentPage,
    MarketCapabilities,
    single_instrument_page,
)
from brutex.data.models import CandleBar
from brutex.models import Exchange, OutOfScopeError

#: The capabilities of the Dhan SPOT-index adapter. ``earliest_history`` is the
#: operator's REQUESTED reachable-today start (2022-06-01, research-supported
#: PARTIAL) — it is DELIBERATELY NOT the honest history-floor authority: the feed
#: binding's ``history_floor`` is ``None`` (UNVERIFIED until a probe transcript,
#: Golden Rule #1) and drives the actual planning clamp, and the feed spine never
#: reads this structural field. Recorded honestly, never load-bearing.
DHAN_CAPABILITIES: Final[MarketCapabilities] = MarketCapabilities(
    market_id="dhan-idx",
    display_name="Dhan (NSE/BSE index spot)",
    exchanges=frozenset({Exchange.NSE, Exchange.BSE}),
    has_derivatives=False,
    has_expiries=False,
    has_option_chains=False,
    supports_intraday=True,
    earliest_history=date(2022, 6, 1),
    native_timezone=NATIVE_TIMEZONE,
    rate_limit_rps=DHAN_RATE_LIMIT_RPS,
    supported_intervals=frozenset({DHAN_FETCH_INTERVAL}),
)


@dataclass(frozen=True, slots=True)
class DhanMarketAdapter:
    """MarketAdapter over an injected Dhan client + a resolved ``{symbol → securityId}`` map.

    Attributes
    ----------
    client
        The :class:`~brutex.adapters.dhan.client.DhanClientProtocol` (a
        :class:`~brutex.adapters.dhan.client.DhanClient` or a fake).
    security_ids
        The resolved ``{symbol → securityId}`` map (from
        :func:`~brutex.adapters.dhan.scrip_master.resolve_security_ids`). A symbol
        ABSENT from this map is out-of-scope (the driver loud-skips it up-front).
    """

    client: DhanClientProtocol
    security_ids: Mapping[str, int]

    @property
    def capabilities(self) -> MarketCapabilities:
        """Stable Dhan spot-index capabilities (read once by the orchestrator)."""
        return DHAN_CAPABILITIES

    def list_instruments(self, *, cursor: str | None = None) -> InstrumentPage:
        """Return an empty single page — the Dhan spot universe is an explicit index list.

        The feed spine drives from the explicit index symbol list (option b — the
        spine consumes the binding fields directly), not a vendor-enumerated master,
        so this adapter does not synthesise :class:`~brutex.models.Instrument` rows.
        """
        return single_instrument_page((), cursor)

    def list_derivatives(
        self, *, underlying_symbol: str, exchange: Exchange
    ) -> Iterator[DerivativeContract]:
        """Yield nothing — the Dhan track is SPOT-only (``has_derivatives=False``)."""
        _ = (underlying_symbol, exchange)
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
        """Fetch 1-minute OHLCV bars for *symbol* over ``[start, end]``.

        Resolves the securityId from :attr:`security_ids` (an unresolved symbol raises
        :class:`~brutex.models.OutOfScopeError` — defensive; the driver loud-skips
        unresolved symbols up-front), tiles the range into ≤ 90-day windows, fetches
        each via the client, and concatenates the parsed bars. Index volume is dropped
        (no traded volume). Raises ``ValueError`` for an unsupported interval
        (fail-loud, Golden Rule #1).
        """
        _ = exchange  # index spot rides the shared IDX_I segment; exchange is inert here
        security_id = self.security_ids.get(symbol)
        if security_id is None:
            raise OutOfScopeError(f"symbol not resolvable to a Dhan securityId: {symbol}")
        windows = plan_candle_windows(
            datetime.combine(start, time.min),
            datetime.combine(end, time.min),
            interval=interval,
        )
        bars: list[CandleBar] = []
        for window in windows:
            payload = self.client.intraday_candles(
                security_id=security_id,
                from_date=window.start[:10],  # ISO "YYYY-MM-DDTHH:MM:SS" → Dhan "YYYY-MM-DD"
                to_date=window.end[:10],
                exchange_segment=SEGMENT_INDEX,
                instrument=INSTRUMENT_INDEX,
            )
            bars.extend(parse_candle_columns(payload, drop_volume=True))
        return bars


__all__ = ["DHAN_CAPABILITIES", "DhanMarketAdapter"]
