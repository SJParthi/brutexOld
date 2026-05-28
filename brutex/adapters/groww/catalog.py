"""Full-F&O catalog driver — discover every expiry + contract (§13.9 steps 4–5).

Ties Surfaces C + D into the operator's "pull all contracts + all expiries for
the entire F&O" run: for each F&O-eligible underlying (from the Surface-B
instrument master) and each year, pull its expiries, then for each expiry pull
its contracts. The discovered contract symbols are exactly what the candle
stack later fetches.

:func:`run_catalog` is the testable, resilient core: a single underlying/year
or a single expiry that fails (a Groww error surfaced as a neutral
:class:`~brutex.adapters.groww.client.VendorSdkError`) is recorded and the run
**continues** — one bad underlying never kills the whole catalog. Counts +
failures are aggregated into a :class:`CatalogReport`.

Pure orchestration: the two fetchers are injected (so it unit-tests with fakes,
no SDK / no network); the fetchers themselves own the pace → call → translate
spine.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import structlog

from brutex.adapters.groww.client import VendorSdkError
from brutex.adapters.groww.contracts import ContractFetchResult
from brutex.adapters.groww.expiries import ExpiryFetchResult

_LOG = structlog.get_logger(__name__)


class _ExpiryFetcher(Protocol):
    """Structural type for the expiry-discovery fetcher the driver consumes."""

    def fetch(
        self,
        underlying: str,
        exchange: str,
        *,
        year: int,
        month: int | None = ...,
        snapshot_date: date | None = ...,
    ) -> ExpiryFetchResult:
        """Discover expiries for one ``(underlying, exchange, year[, month])``."""
        ...


class _ContractFetcher(Protocol):
    """Structural type for the contract-discovery fetcher the driver consumes."""

    def fetch(
        self,
        underlying: str,
        exchange: str,
        expiry_date: date,
        *,
        snapshot_date: date | None = ...,
    ) -> ContractFetchResult:
        """Discover contracts for one ``(underlying, exchange, expiry_date)``."""
        ...


@dataclass(frozen=True, slots=True)
class CatalogReport:
    """Aggregate outcome of a :func:`run_catalog` pass."""

    underlyings: int
    expiries: int
    contracts: int
    expiry_failures: int
    contract_failures: int


def run_catalog(
    expiry_fetcher: _ExpiryFetcher,
    contract_fetcher: _ContractFetcher,
    underlyings: Iterable[tuple[str, str]],
    years: Iterable[int],
    *,
    snapshot_date: date | None = None,
) -> CatalogReport:
    """Discover expiries + contracts for every ``(exchange, underlying) × year``.

    Resilient: a failed underlying/year (expiry pull) or a failed expiry
    (contract pull) is counted and skipped — never aborts the whole catalog.

    Parameters
    ----------
    expiry_fetcher, contract_fetcher
        The Surface-C / Surface-D fetchers.
    underlyings
        ``(exchange, underlying_symbol)`` pairs — typically from
        :func:`~brutex.universe.build_fno_eligible_underlyings`.
    years
        Years to pull expiries for (Groww FNO data is from 2020).
    snapshot_date
        Stamped on every record (temporal history); defaults per-fetcher.
    """
    years_list = list(years)
    underlying_count = 0
    expiries_total = 0
    contracts_total = 0
    expiry_failures = 0
    contract_failures = 0

    for exchange, underlying in underlyings:
        underlying_count += 1
        for year in years_list:
            try:
                expiry_result = expiry_fetcher.fetch(
                    underlying, exchange, year=year, snapshot_date=snapshot_date
                )
            except VendorSdkError:
                expiry_failures += 1
                _LOG.warning("catalog.expiry.failed", underlying=underlying, year=year)
                continue
            expiries_total += len(expiry_result.records)
            for expiry in expiry_result.records:
                try:
                    contract_result = contract_fetcher.fetch(
                        underlying, exchange, expiry.expiry_date, snapshot_date=snapshot_date
                    )
                except VendorSdkError:
                    contract_failures += 1
                    _LOG.warning(
                        "catalog.contract.failed",
                        underlying=underlying,
                        expiry=expiry.expiry_date.isoformat(),
                    )
                    continue
                contracts_total += len(contract_result.records)
        _LOG.info("catalog.underlying.done", exchange=exchange, underlying=underlying)

    return CatalogReport(
        underlyings=underlying_count,
        expiries=expiries_total,
        contracts=contracts_total,
        expiry_failures=expiry_failures,
        contract_failures=contract_failures,
    )


__all__ = [
    "CatalogReport",
    "run_catalog",
]
