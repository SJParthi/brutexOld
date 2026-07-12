"""Dhan spot ingest — pull the index series' 1-minute OHLCV into the ``feeds/dhan/`` lake.

DEC-DHAN-SPOT-PULL-001 (ADR-0022). The Dhan spot spine: plan the fetch windows for
each index series, fetch each via the COMMON
:meth:`~brutex.adapters.protocol.MarketAdapter.fetch_ohlcv`, calendar-session-filter
the bars, and append to a :class:`~brutex.data.sink.BarSink`. Only **3 vendor
couplings** — :func:`~brutex.adapters.dhan.chunks.window_days_for_interval` (the flat
90-day tile), the :mod:`~brutex.adapters.dhan.errors` classifiers, and the
module ledger namespace ``_MARKET="dhan"`` (own namespace → no Groww collision);
everything else is vendor-neutral (the window planner, the session filter, the sink).

**Honest degradation (Golden Rule #1, DEC-DHAN-HONEST-DEGRADE-001).** Two honest
states are structurally distinct:

* **empty-from-Dhan** — a holiday / no-session / too-old span returns empty bars: a
  recorded SUCCESS window with zero bars (clean-empty, never a failure).
* **UNVERIFIED-servability / unresolved securityId** — a symbol resolvable to NO
  securityId is loud-skipped BEFORE any fetch (``dhan.index_unverified`` WARN naming
  the index), its window left RE-PULLABLE (``skipped_unresolved``, zero bars), NEVER
  a fabricated bar and never a blind id.

**Honest envelope (Golden Rule #10).** This is the LEAN, single-threaded spot spine
(the "honest, simpler envelope" the Groww spine documents for its own auth latch): it
proves the Dhan feed end-to-end (resolve → plan → fetch → session-filter → sink →
bounded retry) with per-window outcomes. The FULL Groww-parity spine — the durable
:class:`~brutex.data.window_ledger.WindowLedger` as the sole skip authority, the
parallel fan-out, and the ``run_until_clean`` integration — is the merge-integration
surface work (DEC-FEED-SPINE-GENERALIZE-001, option b: drive the binding fields from
this spot spine), deferred with the dashboard route / CLI ``--feed dhan``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Final, Protocol

import structlog

from brutex.adapters.dhan.chunks import window_days_for_interval
from brutex.adapters.dhan.constants import (
    DHAN_COVERAGE_MARKET,
    DHAN_FETCH_INTERVAL,
    SENSEX_SYMBOL,
)
from brutex.adapters.dhan.errors import DhanError, is_retryable
from brutex.adapters.ingest_common import plan_windows
from brutex.data.models import CandleBar
from brutex.data.session import filter_to_trading_session
from brutex.data.sink import BarSink
from brutex.models import Exchange

_LOG = structlog.get_logger("brutex.adapters.dhan.ingest")


class FetchOhlcv(Protocol):
    """The narrow fetch surface the spot spine needs (the COMMON ``fetch_ohlcv`` waist).

    The production :class:`~brutex.adapters.dhan.adapter.DhanMarketAdapter` satisfies
    it; typing the dependency at this narrow Protocol (not the concrete class) keeps
    the spine testable with a fetch-only fake (CLAUDE.md §11 COMMON).
    """

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        exchange: Exchange,
        interval: str,
        start: date,
        end: date,
    ) -> list[CandleBar]:
        """Fetch OHLCV bars for *symbol* over ``[start, end)`` at *interval*."""
        ...


#: The window-ledger market namespace for Dhan windows (the ONE vendor coupling to the
#: ledger — own namespace so a Dhan window never collides with a Groww window).
_MARKET: Final[str] = DHAN_COVERAGE_MARKET

#: The lake partition segment for a Dhan index series (the neutral INDEX segment; the
#: Dhan lake nests under ``feeds/dhan/`` so this convention is self-contained).
_SEGMENT: Final[str] = "INDEX"

#: Per-window outcome status vocabulary.
STATUS_SUCCESS: Final[str] = "success"
STATUS_FAILED: Final[str] = "failed"
STATUS_SKIPPED_UNRESOLVED: Final[str] = "skipped_unresolved"


def _noop_pace() -> None:
    """Default no-op pace hook (a real controller.pace is injected in production)."""


def _noop_backoff(_round_index: int) -> None:
    """Default no-op inter-round back-off (a test injects a recording one)."""


def dhan_index_exchange(symbol: str) -> Exchange:
    """Map a Dhan index symbol to its exchange (SENSEX → BSE, else NSE).

    Both ride the SHARED ``IDX_I`` segment on the wire, but the lake partition path
    keys on the exchange dir, so SENSEX (a BSE index) is attributed to
    :attr:`~brutex.models.Exchange.BSE` and the NSE indices to
    :attr:`~brutex.models.Exchange.NSE`.
    """
    return Exchange.BSE if symbol == SENSEX_SYMBOL else Exchange.NSE


@dataclass(frozen=True, slots=True)
class DhanWorkItem:
    """One (symbol, exchange, securityId, window) unit of Dhan spot ingest work."""

    symbol: str
    exchange: Exchange
    security_id: int
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class DhanWindowOutcome:
    """The outcome of processing one :class:`DhanWorkItem` (or an unresolved symbol)."""

    symbol: str
    exchange: Exchange
    security_id: int | None
    start: date
    end: date
    status: str
    bars_appended: int
    retryable: bool = False
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class DhanIngestReport:
    """The result of a Dhan spot ingest run.

    Attributes
    ----------
    outcomes
        One :class:`DhanWindowOutcome` per processed window (latest per item) plus
        one ``skipped_unresolved`` outcome per unresolved symbol.
    resolved_symbols, unresolved_symbols
        The symbols that DID / did NOT resolve to a securityId.
    market
        The ledger market namespace (``"dhan"``).
    """

    outcomes: tuple[DhanWindowOutcome, ...]
    resolved_symbols: tuple[str, ...]
    unresolved_symbols: tuple[str, ...]
    market: str = _MARKET

    @property
    def total_bars(self) -> int:
        """Total bars appended across all SUCCESS windows."""
        return sum(o.bars_appended for o in self.outcomes)

    @property
    def failed_windows(self) -> int:
        """Number of windows that ended FAILED (re-pullable)."""
        return sum(1 for o in self.outcomes if o.status == STATUS_FAILED)


def plan_dhan_work(
    *,
    symbols: Sequence[str],
    security_ids: Mapping[str, int],
    interval: str,
    start: date,
    end: date,
) -> tuple[list[DhanWorkItem], list[str]]:
    """Plan the ingest work: resolve each symbol + tile ``[start, end)`` into windows.

    Returns ``(work_items, unresolved_symbols)``. A symbol ABSENT from *security_ids*
    is unresolved (no work item planned — the caller loud-skips it). ``[start, end)``
    is half-open (the ``ingest_common`` tiling convention); an inclusive operator
    range passes ``end + 1 day`` at the surface.
    """
    work: list[DhanWorkItem] = []
    unresolved: list[str] = []
    windows = plan_windows(
        datetime.combine(start, time.min),
        datetime.combine(end, time.min),
        window_days=window_days_for_interval(interval),
    )
    for symbol in symbols:
        security_id = security_ids.get(symbol)
        if security_id is None:
            unresolved.append(symbol)
            continue
        exchange = dhan_index_exchange(symbol)
        for window in windows:
            work.append(
                DhanWorkItem(
                    symbol=symbol,
                    exchange=exchange,
                    security_id=security_id,
                    # DateWindow bounds are ISO "YYYY-MM-DDTHH:MM:SS" strings — take the date part.
                    start=date.fromisoformat(window.start[:10]),
                    end=date.fromisoformat(window.end[:10]),
                )
            )
    return work, unresolved


def _warn_unresolved(symbol: str) -> DhanWindowOutcome:
    """Loud-skip an unresolved symbol (zero bars, re-pullable) — never a fabricated bar."""
    _LOG.warning(
        "dhan.index_unverified",
        symbol=symbol,
        reason="unresolved_security_id",
        remedy="resolve the scrip-master securityId (or seed the fallback map), then re-pull",
    )
    return DhanWindowOutcome(
        symbol=symbol,
        exchange=dhan_index_exchange(symbol),
        security_id=None,
        start=date.min,
        end=date.min,
        status=STATUS_SKIPPED_UNRESOLVED,
        bars_appended=0,
    )


def _process_item(
    item: DhanWorkItem,
    *,
    adapter: FetchOhlcv,
    sink: BarSink,
    interval: str,
    session_filter: Callable[..., object],
    pace: Callable[[], None],
) -> DhanWindowOutcome:
    """Fetch → session-filter → sink one window; classify a failure honestly."""
    pace()
    try:
        raw_bars = adapter.fetch_ohlcv(
            symbol=item.symbol,
            exchange=item.exchange,
            interval=interval,
            start=item.start,
            end=item.end,
        )
        kept = session_filter(raw_bars).kept  # type: ignore[attr-defined]
        result = sink.append(
            exchange=item.exchange.value,
            segment=_SEGMENT,
            symbol=item.symbol,
            interval=interval,
            bars=kept,
        )
    except DhanError as exc:
        retryable = is_retryable(exc)
        _LOG.warning(
            "dhan.window_failed",
            symbol=item.symbol,
            start=item.start.isoformat(),
            end=item.end.isoformat(),
            error_type=type(exc).__name__,
            retryable=retryable,
        )
        return DhanWindowOutcome(
            symbol=item.symbol,
            exchange=item.exchange,
            security_id=item.security_id,
            start=item.start,
            end=item.end,
            status=STATUS_FAILED,
            bars_appended=0,
            retryable=retryable,
            error_type=type(exc).__name__,
        )
    return DhanWindowOutcome(
        symbol=item.symbol,
        exchange=item.exchange,
        security_id=item.security_id,
        start=item.start,
        end=item.end,
        status=STATUS_SUCCESS,
        bars_appended=result.bars_net_new,
    )


def run_dhan_spot_ingest_with_retry(
    *,
    symbols: Sequence[str],
    adapter: FetchOhlcv,
    sink: BarSink,
    security_ids: Mapping[str, int],
    start: date,
    end: date,
    interval: str = DHAN_FETCH_INTERVAL,
    session_filter: Callable[..., object] = filter_to_trading_session,
    pace: Callable[[], None] = _noop_pace,
    max_rounds: int = 3,
    backoff: Callable[[int], None] = _noop_backoff,
) -> DhanIngestReport:
    """Run the Dhan spot ingest with bounded in-run retry of retryable-failed windows.

    Resolves + plans the work (:func:`plan_dhan_work`), loud-skips unresolved symbols,
    then processes each window; a RETRYABLE failure (throttle / empty body / timeout /
    token-stale) is re-processed in a later round up to *max_rounds* (a terminal
    failure is left FAILED — re-pullable by a future pull). Returns the aggregated
    :class:`DhanIngestReport`.

    Parameters
    ----------
    symbols
        The index symbols to pull (e.g. the Dhan spot index set).
    adapter, sink, security_ids
        The injected fetch adapter, write sink, and resolved ``{symbol → securityId}``.
    start, end
        The half-open ``[start, end)`` operator range (``end`` exclusive).
    interval
        The fetch interval (``"1minute"`` — the only Dhan spot interval).
    session_filter, pace, backoff
        Injected seams (default the calendar-aware filter / no-op pace / no-op
        back-off); a production caller injects the controller's ``pace`` + a real
        back-off.
    max_rounds
        The bounded retry-round budget (``>= 1``).
    """
    work, unresolved = plan_dhan_work(
        symbols=symbols,
        security_ids=security_ids,
        interval=interval,
        start=start,
        end=end,
    )
    resolved = tuple(dict.fromkeys(item.symbol for item in work))
    outcomes: list[DhanWindowOutcome] = [_warn_unresolved(symbol) for symbol in unresolved]
    latest: dict[DhanWorkItem, DhanWindowOutcome] = {}
    pending = list(work)
    rounds = max(1, max_rounds)
    for round_index in range(rounds):
        if not pending:
            break
        next_pending: list[DhanWorkItem] = []
        for item in pending:
            outcome = _process_item(
                item,
                adapter=adapter,
                sink=sink,
                interval=interval,
                session_filter=session_filter,
                pace=pace,
            )
            latest[item] = outcome
            if outcome.status == STATUS_FAILED and outcome.retryable:
                next_pending.append(item)
        pending = next_pending
        if pending and round_index < rounds - 1:
            backoff(round_index)
    outcomes.extend(latest.values())
    return DhanIngestReport(
        outcomes=tuple(outcomes),
        resolved_symbols=resolved,
        unresolved_symbols=tuple(unresolved),
    )


__all__ = [
    "STATUS_FAILED",
    "STATUS_SKIPPED_UNRESOLVED",
    "STATUS_SUCCESS",
    "DhanIngestReport",
    "DhanWindowOutcome",
    "DhanWorkItem",
    "dhan_index_exchange",
    "plan_dhan_work",
    "run_dhan_spot_ingest_with_retry",
]
