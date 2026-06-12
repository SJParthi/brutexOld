"""``GrowwMarketAdapter`` — the market-neutral protocol implementation.

Ties the Groww pieces (client + parser + chunker + instruments registry)
into :class:`brutex.adapters.protocol.MarketAdapter`, so the orchestrator
talks to Groww only through ``capabilities`` / ``list_instruments`` /
``list_derivatives`` / ``fetch_ohlcv`` (CLAUDE.md §11 COMMON).

Track-1 scope (CLAUDE.md §1/§3): **spot only** — 3 brute-force indexes +
INDIA VIX (reference). ``has_derivatives=False`` → the orchestrator skips
the derivative phases; expired-option OHLC is handled by the separate
``option_ingest`` module (to be built in Phase 2).

Chunking / logical retry / re-queue stay in the orchestrator. ``fetch_ohlcv``
tiles the date range into Groww-legal chunks internally and merges the
results — the orchestrator sees a single list of bars for the entire window.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date

import structlog

from brutex.adapters.groww.chunks import iter_chunks
from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.constants import (
    ALL_INTERVALS,
    HISTORY_START,
    MARKET_CLOSE_IST,
    MARKET_OPEN_IST,
    RATE_LIMIT_INITIAL_RPS,
    TRACK1_DAILY_INTERVAL,
)
from brutex.adapters.groww.instruments import GrowwInstrument, InstrumentRegistry
from brutex.adapters.groww.parser import parse_candle_rows
from brutex.adapters.protocol import (
    DerivativeContract,
    InstrumentPage,
    MarketCapabilities,
    single_instrument_page,
)
from brutex.data.models import CandleBar
from brutex.models import Exchange, Instrument, InstrumentType, OutOfScopeError

_LOG = structlog.get_logger("brutex.adapters.groww.adapter")

#: Stable capabilities for the Track-1 Groww spot adapter.
_GROWW_CAPABILITIES: MarketCapabilities = MarketCapabilities(
    market_id="groww",
    display_name="Groww (NSE/BSE spot — Track 1)",
    exchanges=frozenset({Exchange.NSE, Exchange.BSE}),
    has_derivatives=False,    # spot only in Track 1; option_ingest handles F&O
    has_expiries=False,
    has_option_chains=False,
    supports_intraday=True,
    earliest_history=HISTORY_START,
    native_timezone="Asia/Kolkata",
    rate_limit_rps=RATE_LIMIT_INITIAL_RPS,
    supported_intervals=ALL_INTERVALS,
)


def _format_ist_time(day: date, time_str: str) -> str:
    """Return ``"YYYY-MM-DD HH:mm:ss"`` in IST for the Groww API."""
    return f"{day.isoformat()} {time_str}"


def _is_index(instrument: Instrument) -> bool:
    """Return ``True`` if *instrument* is an index type (``IDX``)."""
    return instrument.instrument is InstrumentType.IDX


@dataclass(frozen=True, slots=True)
class GrowwMarketAdapter:
    """``MarketAdapter`` over the Groww v1 backtesting API (Track-1 spot).

    Attributes
    ----------
    client
        Authenticated :class:`~brutex.adapters.groww.client.GrowwClient`.
    registry
        :class:`~brutex.adapters.groww.instruments.InstrumentRegistry`
        providing O(1) ``symbol_name → GrowwInstrument`` lookups.
    """

    client: GrowwClient
    registry: InstrumentRegistry
    _sym_index: dict[str, GrowwInstrument] = field(
        init=False, repr=False, compare=False
    )
    _instruments: tuple[Instrument, ...] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Build internal indices from the registry."""
        object.__setattr__(
            self, "_sym_index", dict(self.registry.by_symbol_name)
        )
        object.__setattr__(
            self,
            "_instruments",
            tuple(gi.instrument for gi in self.registry.all_instruments),
        )

    @property
    def capabilities(self) -> MarketCapabilities:
        """Return the stable Track-1 Groww spot capabilities."""
        return _GROWW_CAPABILITIES

    def list_instruments(self, *, cursor: str | None = None) -> InstrumentPage:
        """Return the whole static universe in one page (cursor-exhausted)."""
        return single_instrument_page(self._instruments, cursor)

    def list_derivatives(
        self,
        *,
        underlying_symbol: str,
        exchange: Exchange,
    ) -> Iterator[DerivativeContract]:
        """Yield nothing — Track-1 Groww adapter is spot (``has_derivatives=False``)."""
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
        """Fetch OHLCV bars for *symbol* over ``[start, end]``, chunked automatically.

        Tiles the date range into Groww-legal chunks (30 / 90 / 180 days per
        interval tier), issues one API call per chunk, and returns the
        concatenated list of :class:`~brutex.data.models.CandleBar` objects.

        Parameters
        ----------
        symbol:
            Engine symbol name (e.g. ``"NIFTY"``). Must be in the registry.
        exchange:
            Ignored — the instrument's registered exchange is authoritative.
        interval:
            BruteX canonical interval string (e.g. ``"1minute"``). Must be one
            of :data:`~brutex.adapters.groww.constants.ALL_INTERVALS`.
        start, end:
            Inclusive date range (calendar days, not trading days).

        Returns
        -------
        list[CandleBar]
            Bars in ascending timestamp order, timestamps UTC-aware.

        Raises
        ------
        OutOfScopeError
            If *symbol* is not in this adapter's registry, or *interval* is
            not in :data:`~brutex.adapters.groww.constants.ALL_INTERVALS`.
        """
        gi = self._sym_index.get(symbol)
        if gi is None:
            raise OutOfScopeError(
                f"symbol {symbol!r} not in the Groww universe; "
                f"registered symbols: {sorted(self._sym_index)}"
            )
        del exchange  # the registered exchange is authoritative

        if interval not in ALL_INTERVALS:
            raise OutOfScopeError(
                f"interval {interval!r} is not a recognised Groww interval. "
                f"Supported: {sorted(ALL_INTERVALS)}"
            )

        # Indices have no real volume — drop vendor noise.
        drop_vol = _is_index(gi.instrument)

        # Time strings for intraday vs daily bars differ at market boundaries.
        is_intraday = interval not in {TRACK1_DAILY_INTERVAL, "1week", "1month"}
        open_time = MARKET_OPEN_IST if is_intraday else "00:00:00"
        close_time = MARKET_CLOSE_IST if is_intraday else "23:59:59"

        all_bars: list[CandleBar] = []
        for chunk_start, chunk_end in iter_chunks(start, end, interval):
            start_time = _format_ist_time(chunk_start, open_time)
            end_time = _format_ist_time(chunk_end, close_time)
            _LOG.debug(
                "groww.adapter.fetch_chunk",
                symbol=symbol,
                interval=interval,
                start_time=start_time,
                end_time=end_time,
            )
            payload = self.client.get_candles(
                exchange=gi.api_exchange,
                segment=gi.api_segment,
                groww_symbol=gi.groww_symbol,
                start_time=start_time,
                end_time=end_time,
                candle_interval=interval,
            )
            raw_candles = payload.get("candles", [])
            chunk_bars = parse_candle_rows(raw_candles, drop_volume=drop_vol)
            all_bars.extend(chunk_bars)

        _LOG.info(
            "groww.adapter.fetch_done",
            symbol=symbol,
            interval=interval,
            start=str(start),
            end=str(end),
            total_bars=len(all_bars),
        )
        return all_bars


__all__ = ["GrowwMarketAdapter"]
