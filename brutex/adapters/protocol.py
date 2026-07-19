"""Market-neutral adapter contract.

This module defines the abstract contract every BruteX market adapter
must satisfy, so adding a new market (Polygon, Binance, Groww, Zerodha,
IBKR, Coinbase, …) is a **one-file job** — not a rewrite.

## Why a market-neutral protocol?

In PR #1 we shipped :class:`the deleted broker adapter`,
whose method names were lifted verbatim from the Groww API
(``get_expiries``, ``get_contracts``, ``get_historical_candles``). That's
fine when BruteX only knows about the prior broker. But:

* **US equities via Polygon** have no "expiries" — they're continuous.
* **Crypto via Binance/Coinbase** have no "contracts" — they're spot pairs.
* **Zerodha Kite** uses ``instruments`` / ``historical`` / ``orders`` with
  a completely different shape.
* **IBKR** uses ``reqContractDetails`` / ``reqHistoricalData`` with
  delta protocol semantics.

If every adapter had to implement the prior-broker-shaped protocol, new markets
would be forced to fake data they don't have. This module breaks that
coupling by defining a **semantic** contract — what BruteX needs the
adapter to *do*, not what the adapter's upstream SDK *calls the methods*.

## The three questions every market must answer

1. **What instruments are tradable?**
   — :meth:`MarketAdapter.list_instruments` returns a page of normalised
   :class:`~brutex.models.Instrument` objects. Markets with static
   universes (index futures, top-N equities) can return the whole set
   in one page. Markets with dynamic universes (crypto) page through.

2. **What derivative contracts exist for an underlying (if any)?**
   — :meth:`MarketAdapter.list_derivatives` returns contracts keyed by
   ``(underlying, expiry_or_none)``. For F&O: expiry-dated options +
   futures. For spot markets (US equities, crypto spot): returns an
   empty iterator — BruteX skips the Phase B/C expiry/contract dance
   entirely.

3. **Give me OHLCV bars for this symbol + range.**
   — :meth:`MarketAdapter.fetch_ohlcv` returns parsed :class:`CandleBar`
   rows. Every market has some notion of candles; this is the one
   method every adapter MUST implement.

## Orthogonal concerns — not in the protocol

Things that are NOT adapter concerns (the orchestrator handles them):

* Rate limiting (``AdaptiveTokenBucket``)
* Retry policy (``adapters/groww/errors.is_retryable`` + client ``max_attempts``)
* Dedup + persistence (the engine's ``sweep_cache`` LMDB tiers)
* Audit log (``AuditWriter``)
* Data lake writer (``BarSink``)
* Dashboard + Telegram + metrics

Those live in the Python control plane and are market-agnostic. The
adapter only needs to answer the three questions above.

## Capabilities

Different markets support different things. An adapter declares its
:class:`MarketCapabilities` at construction time so the orchestrator
knows which phases apply:

* ``has_derivatives`` — True for F&O (NSE/BSE/CME), False for spot only
* ``has_expiries`` — True for dated options/futures, False for perps/spot
* ``has_option_chains`` — True if options exist (CE/PE per strike)
* ``supports_intraday`` — True if sub-day intervals are available
* ``earliest_history`` — a :class:`datetime.date` naming the earliest
  bar the market serves

The orchestrator reads capabilities and skips inapplicable phases:
for a spot-only crypto adapter, Phase B (expiries) and Phase C
(contracts) are no-ops, and Phase D fetches bars directly from the
instrument list returned by Phase A.

## Mapping existing broker adapter (deleted) onto this protocol

The existing :class:`VendorAdapter` in ``(deleted)/client.py``
stays intact for backward compatibility. A thin wrapper
:class:`the deleted broker adapter` translates the
three semantic methods onto the existing SDK-shaped calls, so all the
hard plumbing (rate limit, dedup, audit) keeps working unchanged.

## Current adapter (Track 1)

::

    brutex/adapters/groww/          # Groww historical-data adapter (Track 1 spot indices)

## Future markets (out-of-scope for Track 1 — illustrative only)

Per CLAUDE.md §3 scope-lock, Track 1 is locked to the Groww adapter for
4 Indian spot indices (NIFTY 50 / BANKNIFTY / SENSEX / INDIA VIX). The
adapter Protocol is broker-neutral so future tracks could add::

    brutex/adapters/polygon/       # (hypothetical) US equities + options
    brutex/adapters/binance/       # (hypothetical) Crypto perps + spot
    brutex/adapters/zerodha/       # (hypothetical) Kite Connect
    brutex/adapters/ibkr/          # (hypothetical) Interactive Brokers

— but none of these are in scope today; the Protocol shape exists so
the broker-neutral L1 boundary survives a future scope expansion
without re-architecting.

Each is a single file + tests. The rest of BruteX is untouched.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

from brutex.data.models import CandleBar
from brutex.models import Exchange, Instrument

# ────────────────────────────────────────────────────────────────────────
# Capabilities descriptor
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MarketCapabilities:
    """What a market can and can't do.

    The orchestrator reads this at startup to plan which phases to run.
    Markets without derivatives skip Phase B/C entirely. Markets without
    intraday data use day bars for Phase D.

    Attributes
    ----------
    market_id
        Stable short identifier used in partition paths, audit
        logs, and dashboard labels. Examples: ``nse-fno``, ``bse-fno``,
        ``polygon-us``, ``binance-perps``, ``coinbase-spot``.
    display_name
        Human-readable label shown in dashboards and Telegram alerts.
    exchanges
        Frozen set of :class:`Exchange` enum values this adapter serves.
        An adapter MAY serve multiple exchanges (e.g. tickvault's
        ``nse_bse`` serves both) or one.
    has_derivatives
        True if the market has F&O or any derivative contract layer.
    has_expiries
        True if derivatives have a defined expiry date. False for
        perpetual futures (crypto) or spot-only markets.
    has_option_chains
        True if the market has option CE/PE strikes per expiry.
    supports_intraday
        True if sub-day candle intervals are available.
    earliest_history
        The earliest date the market will serve historical bars for.
        Used by the orchestrator to clamp Phase D requests.
    native_timezone
        Timezone the market's bar timestamps are reported in.
        Adapter converts to UTC on ingest.
    rate_limit_rps
        Per-account rate limit in requests per second (documented, not
        probed). ``AdaptiveTokenBucket`` is configured to use 60% of
        this as its steady-state rate.
    supported_intervals
        Frozen set of interval identifiers the market supports. These
        are BruteX canonical names (``1minute``, ``5minute``, ``1day``,
        etc.) — the adapter translates to the upstream SDK's naming.
    """

    market_id: str
    display_name: str
    exchanges: frozenset[Exchange]
    has_derivatives: bool
    has_expiries: bool
    has_option_chains: bool
    supports_intraday: bool
    earliest_history: date
    native_timezone: str
    rate_limit_rps: float
    supported_intervals: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "1minute",
                "5minute",
                "15minute",
                "1hour",
                "1day",
            }
        )
    )

    def __post_init__(self) -> None:
        if not self.market_id or "/" in self.market_id:
            raise ValueError(
                f"market_id must be a non-empty identifier without '/', got {self.market_id!r}"
            )
        if self.has_expiries and not self.has_derivatives:
            raise ValueError("has_expiries=True requires has_derivatives=True")
        if self.has_option_chains and not self.has_derivatives:
            raise ValueError("has_option_chains=True requires has_derivatives=True")
        if self.rate_limit_rps <= 0:
            raise ValueError(f"rate_limit_rps must be > 0, got {self.rate_limit_rps}")


# ────────────────────────────────────────────────────────────────────────
# Return shapes
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class InstrumentPage:
    """A page of instruments returned by :meth:`MarketAdapter.list_instruments`.

    Static-universe markets (NSE F&O, equity indices) return everything
    in one page with ``next_cursor=None``. Dynamic-universe markets
    (crypto exchanges with thousands of pairs) paginate — the caller
    passes ``next_cursor`` back into the next call until it's ``None``.
    """

    instruments: tuple[Instrument, ...]
    next_cursor: str | None


def single_instrument_page(
    instruments: tuple[Instrument, ...], cursor: str | None
) -> InstrumentPage:
    """Return *instruments* as one page; a non-``None`` *cursor* is exhausted.

    The static-universe ``list_instruments`` idiom shared by every single-page
    adapter (CLAUDE.md §11 COMMON) — markets that surface their whole universe
    in one shot have no real cursor, so any non-``None`` cursor is already
    past the (single) page and yields an empty, terminal page.
    """
    if cursor is not None:
        return InstrumentPage(instruments=(), next_cursor=None)
    return InstrumentPage(instruments=instruments, next_cursor=None)


@dataclass(frozen=True, slots=True)
class DerivativeContract:
    """One contract in a derivative chain.

    Attributes
    ----------
    underlying_symbol
        Symbol of the underlying spot/index (e.g. ``NIFTY``, ``AAPL``,
        ``BTC``).
    expiry
        Expiry date for dated contracts. ``None`` for perpetuals.
    symbol
        Full contract symbol as the upstream market reports it — this
        is what :meth:`MarketAdapter.fetch_ohlcv` expects.
    contract_type
        One of ``FUT``, ``CE``, ``PE``, or ``PERP``.
    strike
        Strike price for options, ``None`` for futures/perpetuals.
    """

    underlying_symbol: str
    expiry: date | None
    symbol: str
    contract_type: str
    strike: float | None = None

    def __post_init__(self) -> None:
        if self.contract_type not in {"FUT", "CE", "PE", "PERP"}:
            raise ValueError(f"contract_type must be FUT/CE/PE/PERP, got {self.contract_type}")
        if self.contract_type in {"CE", "PE"} and self.strike is None:
            raise ValueError(f"{self.contract_type} contracts must have a strike")
        if self.contract_type == "PERP" and self.expiry is not None:
            raise ValueError("PERP contracts must have expiry=None")


# ────────────────────────────────────────────────────────────────────────
# The market-neutral protocol
# ────────────────────────────────────────────────────────────────────────


@runtime_checkable
class MarketAdapter(Protocol):
    """Market-neutral adapter interface.

    Every BruteX adapter (Groww, Polygon, Binance, …) implements
    this protocol. The orchestrator talks ONLY through these methods —
    no market-specific code leaks upstream.

    Implementations are free to use whatever rate limiter, retry policy,
    audit logger, and persistence backend they want, but the
    **recommended** pattern is to reuse BruteX's control plane:

    * :class:`~brutex.orchestrator.rate_limiter.AdaptiveTokenBucket`
    * :class:`~brutex.orchestrator.audit.AuditWriter`
    * :mod:`brutex.orchestrator.events` (the pipeline event shapes)

    That's what :class:`VendorAdapter` does, and it's what every future
    adapter should do too.
    """

    @property
    def capabilities(self) -> MarketCapabilities:
        """Return the capabilities this adapter supports.

        Must be stable across the adapter's lifetime — the orchestrator
        reads this once at startup to plan phases.
        """
        ...

    def list_instruments(self, *, cursor: str | None = None) -> InstrumentPage:
        """Return one page of in-scope instruments.

        This is the market-neutral analogue of Phase A. Static-universe
        markets return everything in one page with ``next_cursor=None``;
        dynamic-universe markets paginate via the cursor.

        The adapter MUST filter to in-scope instruments only — out-of-
        scope rows (commodity segment, reserved series, etc.) are the
        adapter's responsibility, not the orchestrator's.
        """
        ...

    def list_derivatives(
        self, *, underlying_symbol: str, exchange: Exchange
    ) -> Iterator[DerivativeContract]:
        """Yield every derivative contract for the given underlying.

        This is the market-neutral analogue of Phase B + C combined.
        Adapters whose market has ``has_derivatives=False`` return an
        empty iterator — the orchestrator will skip Phase B and C.

        For F&O markets this means: enumerate expiries → fetch each
        expiry's contracts → yield the full (underlying, expiry,
        symbol, type, strike) records. Callers can group the results
        however they want.
        """
        ...

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        exchange: Exchange,
        interval: str,
        start: date,
        end: date,
    ) -> list[CandleBar]:
        """Fetch OHLCV bars for the given symbol and date range.

        Returns a list of normalised :class:`CandleBar` objects with
        UTC-aware timestamps. The adapter is responsible for:

        * Converting the market's native timezone to UTC
        * Chunking the date range if the upstream API has per-call
          limits (e.g. a per-call intraday-spot window and option window)
        * Retrying transient errors (429, timeout, 5xx) via the
          adapter's own rate limiter + retry policy
        * Raising :class:`~brutex.models.OutOfScopeError` if the symbol
          is not in scope for this adapter

        The orchestrator's Phase D driver calls this once per
        ``(symbol, interval)`` pair and feeds the result to
        :class:`~brutex.data.sink.BarSink` for persistence.
        """
        ...


__all__ = [
    "DerivativeContract",
    "InstrumentPage",
    "MarketAdapter",
    "MarketCapabilities",
    "single_instrument_page",
]
