"""Universe ingest orchestration — instruments → expiries → contracts.

This is the LAKE-surface companion to :mod:`brutex.adapters.groww.ingest`
(which pulls index *candles*). It wires the three discovery primitives —
:class:`~brutex.adapters.groww.instruments.InstrumentMasterFetcher` (Surface
B), :class:`~brutex.adapters.groww.expiries.ExpiryFetcher` (Surface C) and
:class:`~brutex.adapters.groww.contracts.ContractFetcher` (Surface D) — into
the single "pull the whole universe" pass that ``brutex fetch --universe``
runs *before* the candle ingest.

Scope authority: DEC-P0-063 (``docs/MIGRATION.md`` entry #200) authorises
ingesting the instrument master + expiries + contracts into the LAKE. The
brute-force ENGINE is unchanged (still the 3 indices) — this module only
populates the lake.

:func:`run_universe_ingest` is the testable core: the three fetchers are
injected (each already takes its own injected store/sink/SDK), so the whole
orchestration unit-tests with fakes — no DuckDB, no SDK, no network. It
refreshes the instrument master once, then for every ``(underlying, year)``
discovers expiries, and for every discovered expiry discovers contracts,
aggregating the per-surface counts into a :class:`UniverseIngestReport`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import structlog

_LOG = structlog.get_logger(__name__)


class _InstrumentRefreshResultLike(Protocol):
    """The instrument-refresh result surface the orchestrator reads + re-exports.

    Members are read-only so frozen dataclasses (the real
    ``InstrumentRefreshResult``) and plain test fakes both conform.
    """

    @property
    def parsed(self) -> int:
        """Count of instruments parsed + upserted."""
        ...

    @property
    def filtered(self) -> int:
        """Count of rows skipped as out of scope."""
        ...

    @property
    def quarantined(self) -> int:
        """Count of rows that failed to parse + were dropped."""
        ...


class _ExpiryRecordLike(Protocol):
    """One expiry record — the orchestrator only reads its date."""

    @property
    def expiry_date(self) -> date:
        """The expiry calendar date."""
        ...


class _ExpiryResultLike(Protocol):
    """The expiry-fetch result surface — a read-only sequence of expiry records."""

    @property
    def records(self) -> Sequence[_ExpiryRecordLike]:
        """The discovered expiry records."""
        ...


class _ContractResultLike(Protocol):
    """The contract-fetch result surface — a read-only sequence of contract records."""

    @property
    def records(self) -> Sequence[object]:
        """The discovered contract records."""
        ...


class _InstrumentRefresher(Protocol):
    """The slice of :class:`InstrumentMasterFetcher` used by the orchestrator."""

    def refresh(self) -> _InstrumentRefreshResultLike:
        """Download + parse + upsert the instrument master; return the result."""
        ...


class _ExpiryDiscoverer(Protocol):
    """The slice of :class:`ExpiryFetcher` used by the orchestrator."""

    def fetch(self, underlying: str, exchange: str, *, year: int) -> _ExpiryResultLike:
        """Discover + upsert the expiry dates for *underlying* in *year*."""
        ...


class _ContractDiscoverer(Protocol):
    """The slice of :class:`ContractFetcher` used by the orchestrator."""

    def fetch(self, underlying: str, exchange: str, expiry_date: date) -> _ContractResultLike:
        """Discover + enrich + upsert the contracts for one ``(underlying, expiry)``."""
        ...


@dataclass(frozen=True, slots=True)
class UniverseIngestReport:
    """Aggregate outcome of one :func:`run_universe_ingest` pass.

    Attributes
    ----------
    instruments
        The instrument-master refresh result (parsed / filtered / quarantined).
    underlyings
        Number of ``(underlying, exchange)`` pairs whose expiries were pulled.
    expiries
        Total expiry-date rows discovered across all underlyings/years.
    contracts
        Total contract rows discovered + enriched across all expiries.
    """

    instruments: _InstrumentRefreshResultLike
    underlyings: int
    expiries: int
    contracts: int


def run_universe_ingest(
    instrument_fetcher: _InstrumentRefresher,
    expiry_fetcher: _ExpiryDiscoverer,
    contract_fetcher: _ContractDiscoverer,
    underlyings: Iterable[tuple[str, str]],
    years: Sequence[int],
) -> UniverseIngestReport:
    """Refresh the instrument master, then discover all expiries + contracts.

    Parameters
    ----------
    instrument_fetcher
        Refreshes the Groww instrument-master CSV into the lake (Surface B).
    expiry_fetcher
        Discovers expiry dates per ``(underlying, exchange, year)`` (Surface C).
    contract_fetcher
        Discovers contracts per ``(underlying, exchange, expiry_date)`` (Surface D).
    underlyings
        ``(underlying_symbol, exchange)`` pairs to discover expiries/contracts for.
    years
        Calendar years to pull expiries for (Groww serves F&O from 2020).

    Returns
    -------
    UniverseIngestReport
        Per-surface counts. The instrument refresh runs first (the contract
        fetcher's metadata enrichment depends on a fresh master), then every
        underlying's expiries, then every expiry's contracts.

    Notes
    -----
    Pure orchestration: the three fetchers (each with its own injected store)
    are passed in, so this unit-tests with fakes — no DuckDB / SDK / network.
    """
    inst_result = instrument_fetcher.refresh()
    _LOG.info(
        "universe.instruments.done",
        parsed=inst_result.parsed,
        filtered=inst_result.filtered,
        quarantined=inst_result.quarantined,
    )

    underlying_count = 0
    expiry_total = 0
    contract_total = 0
    for underlying, exchange in underlyings:
        underlying_count += 1
        for year in years:
            expiry_result = expiry_fetcher.fetch(underlying, exchange, year=year)
            expiry_total += len(expiry_result.records)
            for record in expiry_result.records:
                contract_result = contract_fetcher.fetch(underlying, exchange, record.expiry_date)
                contract_total += len(contract_result.records)
        _LOG.info(
            "universe.underlying.done",
            underlying=underlying,
            exchange=exchange,
        )

    _LOG.info(
        "universe.done",
        underlyings=underlying_count,
        expiries=expiry_total,
        contracts=contract_total,
    )
    return UniverseIngestReport(
        instruments=inst_result,
        underlyings=underlying_count,
        expiries=expiry_total,
        contracts=contract_total,
    )


# ────────────────────────────────────────────────────────────────────────
# Synthetic instrument CSV for ``brutex fetch --universe --dry-run``
# ────────────────────────────────────────────────────────────────────────

#: A minimal, valid instrument-master CSV (one index + one option row) that
#: exercises the real :class:`InstrumentMasterFetcher` parse path offline. The
#: synthetic expiry/contract sources live in :mod:`brutex.cli` (the dry-run
#: wiring layer) because they depend on the heavy DuckDB store record types.
SYNTHETIC_INSTRUMENT_CSV: str = (
    "exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,"
    "segment,series,isin,underlying_symbol,underlying_exchange_token,expiry_date,"
    "strike_price,lot_size,tick_size,freeze_quantity,is_reserved,buy_allowed,"
    "sell_allowed\n"
    "NSE,26000,NIFTY,NSE-NIFTY,NIFTY 50,IDX,CASH,,,,,,,1,0.05,,0,1,1\n"
    "NSE,40001,NIFTY24650CE,NSE-NIFTY-30Jan25-24650-CE,NIFTY,CE,FNO,,,NIFTY,"
    "26000,2025-01-30,24650,75,0.05,1800,0,1,1\n"
)


__all__ = [
    "SYNTHETIC_INSTRUMENT_CSV",
    "UniverseIngestReport",
    "run_universe_ingest",
]
