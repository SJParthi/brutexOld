"""Synthetic Dhan adapter — the dry-run :class:`MarketAdapter` (offline source).

The dashboard/cli pull spine (:func:`~brutex.adapters.dhan.run_ingest.run_dhan_spot_ingest`
→ :func:`~brutex.adapters.dhan.ingest.ingest_spot`) talks to its data source
only through :meth:`MarketAdapter.fetch_ohlcv`. The real
:class:`~brutex.adapters.dhan.adapter.DhanMarketAdapter` needs a token + the
network; this synthetic stand-in (DEC-VENDOR-PIVOT-001) returns valid
in-session :class:`~brutex.data.models.CandleBar` rows from
:func:`~brutex.adapters.dhan.synthetic.synthetic_spot_bars` instead, so the
whole parse → session-filter → sink pipeline runs **offline** — the dry-run
proof the pipe works before any credentials are set.

It is the Dhan analog of the legacy ``groww.ingest.SyntheticCandleFetcher``,
reshaped to the market-neutral :class:`~brutex.adapters.protocol.MarketAdapter`
protocol (CLAUDE.md §11 COMMON) so the *same* spine drives the live and the
dry-run pull with zero branching in the orchestrator. Pure (no I/O): every
call is deterministic, so it is 100% unit-testable and side-effect-free.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

from brutex.adapters.dhan.synthetic import synthetic_spot_bars
from brutex.adapters.protocol import (
    DerivativeContract,
    InstrumentPage,
    MarketCapabilities,
)
from brutex.data.models import CandleBar
from brutex.models import Exchange, Instrument

#: Stable capabilities for the synthetic (dry-run) Dhan adapter — a faithful
#: mirror of the live adapter (spot only, 1m + 1d fetched, others resampled),
#: so the orchestrator plans the dry-run pull exactly as it would the live one.
_SYNTHETIC_CAPABILITIES: MarketCapabilities = MarketCapabilities(
    market_id="dhan",
    display_name="Dhan (synthetic dry-run)",
    exchanges=frozenset({Exchange.NSE, Exchange.BSE}),
    has_derivatives=False,
    has_expiries=False,
    has_option_chains=False,
    supports_intraday=True,
    earliest_history=date(2020, 1, 1),
    native_timezone="Asia/Kolkata",
    rate_limit_rps=5.0,
    supported_intervals=frozenset({"1minute", "1day"}),
)


@dataclass(frozen=True, slots=True)
class SyntheticDhanAdapter:
    """A :class:`~brutex.adapters.protocol.MarketAdapter` backed by synthetic bars.

    Drop-in for :class:`~brutex.adapters.dhan.adapter.DhanMarketAdapter` on the
    dry-run path: same protocol, but :meth:`fetch_ohlcv` returns
    :func:`~brutex.adapters.dhan.synthetic.synthetic_spot_bars` instead of
    hitting the network.

    Parameters
    ----------
    instruments
        The static universe surfaced by :meth:`list_instruments`. Empty by
        default — the pull spine fans out over an explicit instrument list, so
        the adapter need not carry one.
    bars_per_window
        Number of synthetic in-session bars emitted per fetched window
        (default 5).
    """

    instruments: tuple[Instrument, ...] = ()
    bars_per_window: int = 5

    @property
    def capabilities(self) -> MarketCapabilities:
        """Return the stable synthetic (dry-run) Dhan capabilities."""
        return _SYNTHETIC_CAPABILITIES

    def list_instruments(self, *, cursor: str | None = None) -> InstrumentPage:
        """Return the whole static universe in one page (cursors exhausted)."""
        if cursor is not None:
            return InstrumentPage(instruments=(), next_cursor=None)
        return InstrumentPage(instruments=self.instruments, next_cursor=None)

    def list_derivatives(
        self, *, underlying_symbol: str, exchange: Exchange
    ) -> Iterator[DerivativeContract]:
        """Yield nothing — the dry-run adapter is spot (``has_derivatives=False``)."""
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
        """Return *bars_per_window* synthetic in-session bars for *start*'s day.

        The arguments other than *start* are accepted to satisfy the
        :class:`~brutex.adapters.protocol.MarketAdapter` contract but do not
        affect the synthetic output — the bars are a deterministic offline
        stand-in, partitioned downstream by the caller's ``(symbol, interval)``.
        """
        del symbol, exchange, interval, end
        return synthetic_spot_bars(start, count=self.bars_per_window)


__all__ = ["SyntheticDhanAdapter"]
