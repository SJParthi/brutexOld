"""DhanMarketAdapter — the :class:`~brutex.adapters.protocol.MarketAdapter` impl.

DEC-DHAN-ADAPTER-001. Composes the Dhan leaf layer into the market-neutral contract:
``capabilities`` / ``list_instruments`` / ``list_derivatives`` / ``fetch_ohlcv``.
Everything is injected (the :class:`~brutex.adapters.dhan.client.DhanClientProtocol`
and a prebuilt :class:`~brutex.adapters.dhan.instruments.DhanInstrumentIndex`), so
``fetch_ohlcv`` is fully testable with a fake client — no live API.

**Scope (CLAUDE.md §3 + Golden Rule #10):** this is the SECOND vendor adapter, built
**LAKE / REFERENCE-ONLY + GATED ON the ₹499/mo Data-API subscription** — INERT until
the operator provisions credentials. The brute-force ENGINE is UNCHANGED
(NIFTY/BANKNIFTY/SENSEX spot). ``has_derivatives=False`` describes the ENGINE scope,
not the Dhan market; ``list_derivatives`` returns an empty iterator (Dhan expired
options are macro/ATM-relative — there is no dated-contract enumeration to fan into
the engine).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Final

from brutex.adapters.dhan.chunks import plan_candle_windows
from brutex.adapters.dhan.client import DhanClientProtocol
from brutex.adapters.dhan.constants import (
    DHAN_DAILY_INTERVAL,
    DHAN_RATE_LIMIT_RPS_CEILING,
    DHAN_SUPPORTED_INTERVALS,
    EARLIEST_HISTORY_UNVERIFIED,
    INTRADAY_INTERVAL_TO_DHAN,
    NATIVE_TIMEZONE,
)
from brutex.adapters.dhan.instruments import DhanInstrumentIndex, DhanScripRow
from brutex.adapters.dhan.parser import parse_candle_columns
from brutex.adapters.protocol import (
    DerivativeContract,
    InstrumentPage,
    MarketCapabilities,
    single_instrument_page,
)
from brutex.calendars.nse import IST_FIXED_OFFSET
from brutex.data.models import CandleBar
from brutex.data.session import SESSION_CLOSE, SESSION_OPEN
from brutex.models import Exchange, Instrument, OutOfScopeError

#: Capabilities of the Dhan reference adapter (CLAUDE.md §3 scope; INERT).
DHAN_CAPABILITIES: Final[MarketCapabilities] = MarketCapabilities(
    market_id="dhan-nse-bse",
    display_name="Dhan (NSE/BSE — LAKE/reference, subscription-gated)",
    exchanges=frozenset({Exchange.NSE, Exchange.BSE}),
    has_derivatives=False,
    has_expiries=False,
    has_option_chains=False,
    supports_intraday=True,
    earliest_history=EARLIEST_HISTORY_UNVERIFIED,
    native_timezone=NATIVE_TIMEZONE,
    rate_limit_rps=DHAN_RATE_LIMIT_RPS_CEILING,
    supported_intervals=frozenset(DHAN_SUPPORTED_INTERVALS),
)

#: Candidate exchange-segment tokens per BruteX :class:`Exchange`, tried in order
#: when resolving a bare spot/index symbol (index first — the Track-1 shape).
_SEGMENT_CANDIDATES: Final[dict[Exchange, tuple[str, ...]]] = {
    Exchange.NSE: ("IDX_I", "NSE_EQ", "NSE_FNO"),
    Exchange.BSE: ("IDX_I", "BSE_EQ", "BSE_FNO"),
}

#: NSE/BSE session open/close — the CANONICAL session-time constants (imported per
#: DEC-TIMESTAMP-IST-SINGLESOURCE-001 §11 COMMON, never a re-minted ``time(9, 15)``
#: copy), used to build the window datetime bounds.
_SESSION_OPEN: Final[time] = SESSION_OPEN
_SESSION_CLOSE: Final[time] = SESSION_CLOSE


@dataclass(frozen=True, slots=True)
class DhanMarketAdapter:
    """MarketAdapter over an injected Dhan client + prebuilt scrip index.

    Attributes
    ----------
    client
        The :class:`~brutex.adapters.dhan.client.DhanClientProtocol` (a single client
        or a pool).
    index
        The prebuilt :class:`~brutex.adapters.dhan.instruments.DhanInstrumentIndex`.
    universe
        The engine :class:`~brutex.models.Instrument` list ``list_instruments``
        returns (injected; defaults empty — the reference adapter does not synthesise
        engine instruments from arbitrary scrip rows).
    """

    client: DhanClientProtocol
    index: DhanInstrumentIndex
    universe: tuple[Instrument, ...] = field(default=())

    @property
    def capabilities(self) -> MarketCapabilities:
        """Stable Dhan reference-adapter capabilities (read once by the orchestrator)."""
        return DHAN_CAPABILITIES

    def list_instruments(self, *, cursor: str | None = None) -> InstrumentPage:
        """Return the injected engine universe in one page (static universe)."""
        return single_instrument_page(self.universe, cursor)

    def list_derivatives(
        self, *, underlying_symbol: str, exchange: Exchange
    ) -> Iterator[DerivativeContract]:
        """Yield nothing — Dhan expired options are macro/ATM-relative (no dated chain).

        The rolling-option endpoint addresses expired options by an ``ATM±N`` selector,
        never a dated-contract security_id, so there is no per-contract chain to
        enumerate (Golden Rule #1 — never fabricate contracts). *underlying_symbol* /
        *exchange* are unused; returns an empty iterator honestly.
        """
        _ = (underlying_symbol, exchange)
        return iter(())

    def _resolve_row(self, exchange: Exchange, symbol: str) -> tuple[DhanScripRow, str]:
        """Resolve a bare symbol to its scrip row + exchange-segment, else raise OutOfScope."""
        for segment in _SEGMENT_CANDIDATES[exchange]:
            security_id = self.index.resolve_security_id(segment, symbol)
            if security_id is not None:
                return self.index.by_security_id[security_id], segment
        raise OutOfScopeError(f"symbol not in Dhan scrip index: {exchange.value}:{symbol}")

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

        Resolves the scrip row via the injected index (raises
        :class:`~brutex.models.OutOfScopeError` for an unknown symbol), picks the daily
        endpoint for ``"1day"`` else the intraday endpoint (raises ``ValueError`` for an
        unsupported interval), tiles the range into vendor-sized windows, fetches each,
        and parses the column arrays. Index rows drop volume (no traded volume).
        """
        if interval != DHAN_DAILY_INTERVAL and interval not in INTRADAY_INTERVAL_TO_DHAN:
            raise ValueError(f"unsupported Dhan interval: {interval!r}")
        row, segment = self._resolve_row(exchange, symbol)
        drop_volume = row.instrument_type == "INDEX"
        security_id = str(row.security_id)
        # The bounds are IST wall-clock session times, so they carry the IST fixed
        # offset (round-1 review: tagging them UTC produced no wrong bytes — the
        # serializers slice the wall-clock prefix — but was a latent tz-semantics
        # trap for any future tz-aware comparison/astimezone on these datetimes).
        start_dt = datetime.combine(start, _SESSION_OPEN, tzinfo=IST_FIXED_OFFSET)
        end_dt = datetime.combine(end, _SESSION_CLOSE, tzinfo=IST_FIXED_OFFSET)
        windows = plan_candle_windows(start_dt, end_dt, interval=interval)

        bars: list[CandleBar] = []
        for window in windows:
            if interval == DHAN_DAILY_INTERVAL:
                payload = self.client.historical_candles(
                    security_id=security_id,
                    exchange_segment=segment,
                    instrument=row.instrument_type,
                    from_date=_fmt_daily(window.start),
                    to_date=_fmt_daily(window.end),
                    oi=False,
                )
            else:
                payload = self.client.intraday_candles(
                    security_id=security_id,
                    exchange_segment=segment,
                    instrument=row.instrument_type,
                    interval=INTRADAY_INTERVAL_TO_DHAN[interval],
                    from_date=_fmt_intraday(window.start),
                    to_date=_fmt_intraday(window.end),
                    oi=False,
                )
            bars.extend(parse_candle_columns(payload, drop_volume=drop_volume))
        return bars


def _fmt_daily(iso: str) -> str:
    """Format a window ISO datetime string as a ``YYYY-MM-DD`` daily bound."""
    return iso[:10]


def _fmt_intraday(iso: str) -> str:
    """Format a window ISO datetime string as a ``YYYY-MM-DD HH:MM:SS`` intraday bound."""
    return iso[:19].replace("T", " ")


__all__ = ["DHAN_CAPABILITIES", "DhanMarketAdapter"]
