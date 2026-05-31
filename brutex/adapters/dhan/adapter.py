"""``DhanMarketAdapter`` — the market-neutral protocol implementation.

Ties the Dhan pieces (body builders + REST client + columnar parser) into
:class:`brutex.adapters.protocol.MarketAdapter`, so the orchestrator talks
to Dhan only through ``capabilities`` / ``list_instruments`` /
``list_derivatives`` / ``fetch_ohlcv`` (CLAUDE.md §11 COMMON).

Track-1 scope (CLAUDE.md §1/§3): **spot only** — 3 brute-force indexes +
INDIA VIX (reference) + F&O-stock spot. ``has_derivatives=False`` → the
orchestrator skips the derivative phases; expired-option OHLC comes from
the separate ``/charts/rollingoption`` path, not this adapter.

Pacing / chunking / retry stay in the orchestrator (the chunk planner is
`brutex.adapters.dhan.chunks`; retry semantics are `errors.is_retryable`):
``fetch_ohlcv`` performs **one** within-window request and returns parsed
bars. The universe (an immutable tuple of :class:`Instrument`) is indexed
by ``vendor_symbol`` (= Dhan ``SECURITY_ID``) for O(1) lookup.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time

from brutex.adapters.dhan.body import build_historical_body, build_intraday_body
from brutex.adapters.dhan.client import DhanClient
from brutex.adapters.dhan.constants import (
    DATA_RATE_LIMIT_RPS,
    ENDPOINT_HISTORICAL,
    ENDPOINT_INTRADAY,
    TRACK1_SPOT_INTRADAY_INTERVAL,
)
from brutex.adapters.dhan.parser import parse_candle_columns
from brutex.adapters.protocol import (
    DerivativeContract,
    InstrumentPage,
    MarketCapabilities,
)
from brutex.data.models import CandleBar
from brutex.models import Exchange, Instrument, InstrumentType, OutOfScopeError

#: Canonical interval → (endpoint, is_intraday). Track 1 fetches 1m + 1d ONLY;
#: every other timeframe is resampled from the 1m base (DEC-DHAN-1M-ONLY-001).
_INTERVAL_INTRADAY: str = "1minute"
_INTERVAL_DAILY: str = "1day"

#: Stable capabilities for the Track-1 Dhan spot adapter.
_DHAN_CAPABILITIES: MarketCapabilities = MarketCapabilities(
    market_id="dhan",
    display_name="Dhan (NSE/BSE spot)",
    exchanges=frozenset({Exchange.NSE, Exchange.BSE}),
    has_derivatives=False,
    has_expiries=False,
    has_option_chains=False,
    supports_intraday=True,
    earliest_history=date(2020, 1, 1),
    native_timezone="Asia/Kolkata",
    rate_limit_rps=float(DATA_RATE_LIMIT_RPS),
    supported_intervals=frozenset({_INTERVAL_INTRADAY, _INTERVAL_DAILY}),
)


def _dhan_instrument(instrument: Instrument) -> str:
    """Map a spot instrument's type → Dhan ``instrument`` enum (INDEX/EQUITY)."""
    return "INDEX" if instrument.instrument_type is InstrumentType.IDX else "EQUITY"


@dataclass(frozen=True, slots=True)
class DhanMarketAdapter:
    """``MarketAdapter`` over the Dhan v2 Data API (Track-1 spot)."""

    client: DhanClient
    instruments: tuple[Instrument, ...]
    _index: dict[str, Instrument] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Index the universe by ``vendor_symbol`` (Dhan SECURITY_ID) for O(1) lookup."""
        object.__setattr__(self, "_index", {i.vendor_symbol: i for i in self.instruments})

    @property
    def capabilities(self) -> MarketCapabilities:
        """Return the stable Track-1 Dhan spot capabilities."""
        return _DHAN_CAPABILITIES

    def list_instruments(self, *, cursor: str | None = None) -> InstrumentPage:
        """Return the whole static universe in one page (paginated cursors exhausted)."""
        if cursor is not None:
            return InstrumentPage(instruments=(), next_cursor=None)
        return InstrumentPage(instruments=self.instruments, next_cursor=None)

    def list_derivatives(
        self, *, underlying_symbol: str, exchange: Exchange
    ) -> Iterator[DerivativeContract]:
        """Yield nothing — Track-1 Dhan is spot (``has_derivatives=False``)."""
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
        """Fetch one window of spot OHLCV for *symbol* and return parsed bars.

        Raises
        ------
        OutOfScopeError
            If *symbol* is not in this adapter's universe, or *interval* is
            not a fetched Track-1 interval (1-minute / 1-day — others are
            resampled from 1m, never fetched).
        """
        instrument = self._index.get(symbol)
        if instrument is None:
            raise OutOfScopeError(f"symbol {symbol!r} not in the Dhan universe")
        del exchange  # the instrument's own exchange is authoritative
        exchange_segment = f"{instrument.exchange.value}_EQ"
        dhan_instrument = _dhan_instrument(instrument)
        from_dt = datetime.combine(start, time())
        to_dt = datetime.combine(end, time())
        if interval == _INTERVAL_INTRADAY:
            endpoint = ENDPOINT_INTRADAY
            body = build_intraday_body(
                security_id=symbol,
                exchange_segment=exchange_segment,
                instrument=dhan_instrument,
                from_date=from_dt,
                to_date=to_dt,
                interval=TRACK1_SPOT_INTRADAY_INTERVAL,
            )
        elif interval == _INTERVAL_DAILY:
            endpoint = ENDPOINT_HISTORICAL
            body = build_historical_body(
                security_id=symbol,
                exchange_segment=exchange_segment,
                instrument=dhan_instrument,
                from_date=from_dt,
                to_date=to_dt,
            )
        else:
            raise OutOfScopeError(
                f"interval {interval!r} is not fetched (Track 1 fetches 1minute/1day; "
                "others are resampled from 1m)"
            )
        payload = self.client.post_json(endpoint, body)
        candles = payload.get("data")
        source = candles if isinstance(candles, dict) else payload
        return parse_candle_columns(source)


__all__ = ["DhanMarketAdapter"]
