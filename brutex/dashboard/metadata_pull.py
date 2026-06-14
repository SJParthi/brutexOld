"""Metadata-only F&O pull — "Pull expiries + contracts" WITHOUT any candles.

DEC-DERIV-METADATA-ORCHESTRATION-001 (PR-2 of the metadata/historical split,
builds on DEC-DERIV-METADATA-HISTORICAL-SPLIT-001 #732 + DEC-UNIVERSE-PULL-PHASED-001).
This is the orchestration that finally WIRES the dormant two-phase engine
(:mod:`brutex.orchestrator.universe_phases`, DEC-UNIVERSE-PULL-PHASED-001) to the
metadata sinks PR-1 shipped — the :class:`~brutex.data.registry_store.RegistryStore`
(expiry DATES + contract SYMBOLS) and the shared ``coverage_ledger`` rollup the
``/coverage`` page reads.

## Why a NEW sibling (the metadata HALF of the metadata/historical split)

The retired fused bulk chain pull enumerated the chain AND pulled historical
candles into the ``bars/`` lake (the heavy, weeks-scale minute fill) in one click
— the runaway behaviour PR-6 (DEC-DERIV-DECOMMISSION-FUSED-001) decommissioned.
This module is the OPPOSITE half: it enumerates + PERSISTS the metadata (expiries
+ contracts) and pulls **ZERO candles** — so the cheap, NSE-verifiable chain SHAPE
lands in minutes and the heavy historical fill stays behind its own separate
button (:func:`~brutex.dashboard.historical_pull.pull_contract_candles`). Keeping
them as two siblings (CLAUDE.md §11 COMMON — one job each) means this module NEVER
constructs a :class:`~brutex.data.sink.BarSink` and NEVER calls
``get_historical_candles`` / ``run_groww_option_ingest``; that separation is the
whole point and is asserted by the tests.

## The two phases (delegated to the dormant engine)

:func:`pull_expiries_contracts` builds the Phase-1 ``(exch, underlying, year)``
cells via :func:`~brutex.orchestrator.universe_phases.phase1_cells`, computes the
incremental skip-sets from the registry + coverage ledger
(:func:`~brutex.orchestrator.universe_phases.cells_to_pull` /
:func:`~brutex.orchestrator.universe_phases.contract_jobs_to_pull` via
``settled`` frozensets), and drives
:func:`~brutex.orchestrator.universe_phases.run_two_phase`:

* **Phase 1** — ``fetch_expiries(cell)`` calls
  :func:`~brutex.adapters.groww.contracts.resolve_expiries` (metadata only); the
  ``on_expiry_cell`` callback PERSISTS each discovered date via
  :meth:`~brutex.data.registry_store.RegistryStore.upsert_expiry` and writes the
  per-``(underlying, year)`` coverage rollup
  (:data:`~brutex.data.coverage_ledger.STATUS_EXPIRIES_ONLY` on success — the
  expiries are captured but contracts are a separate sub-phase here).
* **Phase 2** — ``fetch_contracts(job)`` calls
  :func:`~brutex.adapters.groww.contracts.resolve_contracts` (metadata only) and
  the ``on_contract`` callback PERSISTS each contract via
  :meth:`~brutex.data.registry_store.RegistryStore.upsert_contract`, parsing the
  ``strike`` / ``side`` from the already-parsed
  :class:`~brutex.adapters.protocol.DerivativeContract` (never re-invented).

## Honest envelope (Golden Rule #1 + #10)

* **No candles** — metadata only; the historical OHLC stays behind the separate
  "Pull historical" button. This module imports neither the sink nor the candle
  ingest.
* **Honest dry-run** (Golden Rule #1) — without live Groww credentials the F&O
  chain (which strikes/expiries exist) is unknown REAL vendor data we refuse to
  fabricate. The default dry-run client is the SAME empty-chain SDK
  :mod:`brutex.dashboard.derivatives_pull` uses, so a no-creds pull persists 0
  expiries + 0 contracts and reports an honest "set credentials" note — never an
  invented strike.
* **Network-bound, not O(1)-total** — one ``get_expiries`` per cell + one
  ``get_contracts`` per discovered expiry. The incremental skip
  (DEC-UNIVERSE-PULL-PHASED-001) keeps a re-click O(N) membership checks issuing
  only genuinely-new calls; the upsert PKs are the second dedup line (rerun-safe,
  CLAUDE.md §7).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Final, Protocol

from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.contracts import resolve_contracts, resolve_expiries
from brutex.data.coverage_ledger import (
    STATUS_EXPIRIES_ONLY,
    CoverageRecord,
    classify_cell,
)
from brutex.data.registry_store import RegistryStore
from brutex.orchestrator.parallel import PoolRunParallel
from brutex.orchestrator.universe_phases import (
    ContractJob,
    ExpiryCell,
    RunParallel,
    cells_to_pull,
    phase1_cells,
    run_two_phase,
    settled_contract_jobs,
    settled_expiry_cells,
)

if TYPE_CHECKING:
    from brutex.dashboard.pull_controller import SessionRateController
    from brutex.models import Instrument

#: FNO history floor — Groww FNO data is "available from 2020" (CLAUDE.md §6); the
#: metadata pull mirrors the derivatives pull's floor so the year grid agrees.
GROWW_METADATA_HISTORY_FLOOR: Final[date] = date(2020, 1, 1)

#: The default ``run_id`` stamped on persisted rows when the caller injects none —
#: a stable, honest marker (the dashboard route injects a per-click id).
DEFAULT_METADATA_RUN_ID: Final[str] = "metadata-pull"

#: Factory: ``() -> GrowwClient`` — injected in tests; the default returns the
#: honest empty-chain dry-run client or (live) the credentialed client.
MetadataClientFactory = Callable[[], GrowwClient]


class _CoverageSink(Protocol):
    """The coverage-ledger rollup surface this module needs (a structural subset).

    :class:`~brutex.data.instrument_store.InstrumentStore` satisfies it in
    production; tests pass a tiny fake. Kept to exactly the two methods used
    (CLAUDE.md §11 COMMON — depend on the minimum) so the metadata pull never
    couples to the full store: ``upsert_coverage`` writes the per-cell rollup the
    ``/coverage`` page reads, and ``list_coverage`` seeds the Phase-1 incremental
    skip.
    """

    def upsert_coverage(self, record: CoverageRecord) -> None:
        """Insert or replace one ``coverage_ledger`` cell (idempotent on the PK)."""
        ...

    def list_coverage(self) -> list[CoverageRecord]:
        """Return every persisted coverage cell."""
        ...


@dataclass(frozen=True, slots=True)
class MetadataPullReport:
    """The honest outcome of one metadata-only expiries+contracts pull.

    Mirrors :class:`~brutex.dashboard.historical_pull.HistoricalPullReport`'s style
    — display + audit — but counts METADATA, never bars (there are none).

    Attributes
    ----------
    dry_run
        ``True`` when the empty-chain dry-run client was used (no creds).
    years
        The calendar years the Phase-1 grid covered.
    underlyings
        The underlying symbols whose chains were enumerated.
    expiry_cells
        Phase-1 cells attempted after the incremental skip.
    cells_skipped
        Phase-1 cells dropped by the incremental skip (immutable past years
        already settled) — ``len(all_cells) - expiry_cells``.
    expiry_failures
        Phase-1 cells whose ``resolve_expiries`` raised (recorded, resumed).
    expiries_persisted
        Total expiry dates upserted into the registry.
    contract_jobs
        Phase-2 ``(exch, underlying, expiry)`` jobs attempted after Phase-2 skip.
    contract_failures
        Phase-2 jobs whose ``resolve_contracts`` raised.
    contracts_persisted
        Total contract legs upserted into the registry.
    note
        An honest one-line outcome note (dry-run / persisted counts).
    """

    dry_run: bool
    years: tuple[int, ...]
    underlyings: tuple[str, ...]
    expiry_cells: int
    cells_skipped: int
    expiry_failures: int
    expiries_persisted: int
    contract_jobs: int
    contract_failures: int
    contracts_persisted: int
    note: str

    def to_dict(self) -> dict[str, object]:
        """JSON-safe view for the metadata-pull route + the live panel."""
        return {
            "dry_run": self.dry_run,
            "years": list(self.years),
            "underlyings": list(self.underlyings),
            "expiry_cells": self.expiry_cells,
            "cells_skipped": self.cells_skipped,
            "expiry_failures": self.expiry_failures,
            "expiries_persisted": self.expiries_persisted,
            "contract_jobs": self.contract_jobs,
            "contract_failures": self.contract_failures,
            "contracts_persisted": self.contracts_persisted,
            "note": self.note,
        }


class _EmptyChainSDK:
    """An honest dry-run SDK: the F&O chain is unknown without creds, so it is EMPTY.

    Refuses to fabricate strikes/expiries (Golden Rule #1). It carries ONLY the two
    metadata methods this module ever calls (``get_expiries`` / ``get_contracts``) —
    deliberately NO ``get_historical_candles``, because the metadata pull must never
    reach for a candle even in the dry-run default.
    """

    def get_expiries(self, **_kwargs: object) -> dict[str, object]:
        """No expiries without live creds."""
        return {"expiries": []}

    def get_contracts(self, **_kwargs: object) -> dict[str, object]:
        """No contracts without live creds."""
        return {"contracts": []}


def _build_dry_run_client() -> GrowwClient:
    """Honest dry-run client: an empty-chain SDK (no fabricated chain, no candles)."""
    return GrowwClient(sdk=_EmptyChainSDK())  # type: ignore[arg-type]


def _build_live_client(  # pragma: no cover - live Groww TOTP token + SDK + network
    *,
    pace: Callable[[], None] | None,
    on_rate_limit: Callable[[], None] | None,
    on_success: Callable[[], None] | None,
) -> GrowwClient:
    """Build the LIVE credentialed :class:`GrowwClient` from stored TOTP creds.

    LIVE ONLY (network + TOTP); tests inject a fake ``client_factory``. Mirrors the
    historical pull's live-client creds chain
    (:func:`brutex.dashboard.historical_pull._build_live_client`, same rate hooks at
    the one choke point), MINUS the candle sink — the metadata pull has no lake sink
    at all.
    """
    from brutex.adapters.groww.auth import GrowwCredentials
    from brutex.adapters.groww.live import build_live_client
    from brutex.config.credentials import resolve_groww_file_credentials

    file_creds = resolve_groww_file_credentials()
    creds = GrowwCredentials(api_key=file_creds.api_key, totp_secret=file_creds.totp_secret)
    return build_live_client(creds, pace=pace, on_rate_limit=on_rate_limit, on_success=on_success)


def _rate_hooks(
    session_rate: SessionRateController | None, *, dry_run: bool
) -> tuple[Callable[[], None] | None, Callable[[], None] | None, Callable[[], None] | None]:
    """Resolve ``(pace, on_rate_limit, on_success)`` from the session rate controller.

    Dry-run or no controller → all ``None`` (no pacing offline). Live with a
    :class:`~brutex.dashboard.pull_controller.SessionRateController` → its single
    persistent controller's hooks, so the 500/min governor spans the whole pull
    (the metadata calls ride the SAME bucket as the spot/derivatives pulls).
    """
    if dry_run or session_rate is None:
        return None, None, None
    controller = session_rate.get()
    return controller.pace, controller.on_rate_limit, controller.on_success


def _resolve_client_factory(
    client_factory: MetadataClientFactory | None,
    *,
    dry_run: bool,
    session_rate: SessionRateController | None,
) -> MetadataClientFactory:
    """Pick the ``() -> GrowwClient`` factory, wiring the live limiter when live.

    An injected *client_factory* (tests) wins untouched. Otherwise dry-run → the
    honest empty-chain client; live → a factory that injects the shared rate
    controller's hooks so the metadata calls can never bypass the limiter.
    """
    if client_factory is not None:
        return client_factory
    if dry_run:
        return _build_dry_run_client
    pace, on_rate_limit, on_success = _rate_hooks(session_rate, dry_run=dry_run)

    def _live() -> GrowwClient:  # pragma: no cover - live network
        return _build_live_client(pace=pace, on_rate_limit=on_rate_limit, on_success=on_success)

    return _live


def _underlying_grid(instruments: Sequence[Instrument]) -> list[tuple[str, str]]:
    """Project instruments to the de-duplicated ``(exchange, underlying_symbol)`` grid.

    The Phase-1 ``phase1_cells`` keys on ``(exchange, underlying)``; an index/stock
    underlying IS the symbol the expiry discovery queries, so ``symbol_name`` is the
    underlying for the chain enumeration. Order-preserving, first-wins dedup so the
    pull order is reproducible (CLAUDE.md §4).
    """
    grid: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for instrument in instruments:
        pair = (str(instrument.exch_id), instrument.symbol_name)
        if pair not in seen:
            seen.add(pair)
            grid.append(pair)
    return grid


def _build_note(*, dry_run: bool, expiries_persisted: int, contracts_persisted: int) -> str:
    """Honest one-line outcome note for the report / live panel."""
    if dry_run:
        return (
            "dry-run: set Groww credentials to enumerate + persist the live F&O metadata "
            "(the chain's strikes/expiries are real vendor data, never fabricated offline)"
        )
    return (
        f"persisted {expiries_persisted} expiry date(s) + {contracts_persisted} contract(s) "
        "to the registry (metadata only — no candles; rerun-safe, PK-idempotent)"
    )


def pull_expiries_contracts(
    instruments: Sequence[Instrument],
    *,
    registry: RegistryStore,
    coverage_store: _CoverageSink,
    years: Sequence[int],
    dry_run: bool,
    run_parallel: RunParallel | None = None,
    client_factory: MetadataClientFactory | None = None,
    session_rate: SessionRateController | None = None,
    now: datetime | None = None,
    today: date | None = None,
    run_id: str = DEFAULT_METADATA_RUN_ID,
) -> MetadataPullReport:
    """Enumerate + PERSIST F&O expiries + contracts for *instruments* — NO candles.

    Wires the dormant two-phase engine
    (:func:`~brutex.orchestrator.universe_phases.run_two_phase`) to the metadata
    sinks: Phase 1 discovers + persists expiry DATES (registry + an
    ``expiries_only`` coverage rollup), Phase 2 discovers + persists contract
    SYMBOLS (registry). It pulls ZERO historical candles — that stays behind the
    separate "Pull historical" button — so this module constructs no
    :class:`~brutex.data.sink.BarSink` and calls no ``get_historical_candles``.

    The incremental skip (DEC-UNIVERSE-PULL-PHASED-001) means a re-click does only
    NEW work: an immutable PAST year already settled in the coverage ledger is
    skipped (Phase 1); an EXPIRED expiry whose contracts are already persisted is
    skipped (Phase 2). The current year + not-yet-expired expiries always re-pull
    (new expiries/strikes appear as time passes).

    Parameters
    ----------
    instruments
        The underlyings whose chains to enumerate (indices and/or F&O stocks); the
        ``(exch_id, symbol_name)`` pairs form the Phase-1 grid.
    registry
        The metadata sink — expiry dates + contract symbols (idempotent on its PKs).
    coverage_store
        The per-``(underlying, year)`` rollup sink (``upsert_coverage`` +
        ``list_coverage``) so the ``/coverage`` page keeps working; its
        ``list_coverage`` also seeds the Phase-1 incremental skip.
    years
        Calendar years to cover (e.g. ``2020 → today``).
    dry_run
        When ``True`` (and no ``client_factory``), the honest empty-chain client is
        used (0 expiries, 0 contracts) — never a fabricated strike.
    run_parallel
        The concurrency mapper for BOTH phases; ``None`` selects the production
        :class:`~brutex.orchestrator.parallel.PoolRunParallel`. Tests inject a
        synchronous fake so the wiring is provable with zero threads.
    client_factory
        Injected ``() -> GrowwClient`` (tests pass a fake SDK); ``None`` selects the
        dry-run / live default (live wires the session rate hooks).
    session_rate
        The dashboard's ``SessionRateController`` (live pacing); ``None`` offline.
    now
        Injected clock for the persisted ``fetched_at`` timestamp (defaults to
        ``datetime.now(UTC)``).
    today
        Injected date for the Phase-2 expired-expiry skip + the Phase-1 current-year
        cutoff (defaults to ``now``'s UTC date).
    run_id
        The reproducibility id stamped on every persisted row (defaults to
        :data:`DEFAULT_METADATA_RUN_ID`; the route injects a per-click id).

    Returns
    -------
    MetadataPullReport
        Honest metadata counts (expiries/contracts persisted, cells skipped,
        failures) — never a bar count (there are none).
    """
    now = now or datetime.now(UTC)
    today = today or now.astimezone(UTC).date()
    if run_parallel is None:
        run_parallel = PoolRunParallel()
    client = _resolve_client_factory(client_factory, dry_run=dry_run, session_rate=session_rate)()

    grid = _underlying_grid(instruments)
    all_cells = phase1_cells(grid, years)
    expiry_skip = settled_expiry_cells(
        (rec.exch_id, rec.underlying_symbol, rec.year, rec.status)
        for rec in coverage_store.list_coverage()
    )
    cells = cells_to_pull(all_cells, settled=expiry_skip, current_year=today.year)
    contract_skip = settled_contract_jobs(registry.list_contract_jobs())

    # Per-cell expiry tally so the coverage rollup carries the real discovered count
    # (the classify_cell rule keys completeness off expiries_pulled).
    expiries_persisted = 0
    contracts_persisted = 0

    def _fetch_expiries(cell: ExpiryCell) -> Sequence[date]:
        """Phase-1 fetcher: resolve one cell's expiry dates (metadata only)."""
        exchange, underlying, year = cell
        return resolve_expiries(client, exchange=exchange, underlying_symbol=underlying, year=year)

    def _on_expiry_cell(
        cell: ExpiryCell, expiries: Sequence[date], exc: BaseException | None
    ) -> None:
        """Persist each discovered expiry + write the per-cell coverage rollup."""
        nonlocal expiries_persisted
        exchange, underlying, year = cell
        for expiry in expiries:
            registry.upsert_expiry(exchange, underlying, expiry, run_id=run_id, fetched_at=now)
        expiries_persisted += len(expiries)
        error_message = None if exc is None else (str(exc) or repr(exc))
        # A failed cell ⇒ classify_cell → "failed"; a success with ≥1 expiry ⇒
        # "expiries_only" (captured but contracts are the separate Phase 2 here);
        # a success with 0 expiries ⇒ classify_cell → "pending" (ran, found none).
        status = (
            STATUS_EXPIRIES_ONLY
            if (exc is None and expiries)
            else classify_cell(len(expiries), 0, error_message)
        )
        coverage_store.upsert_coverage(
            CoverageRecord(
                exch_id=exchange,
                underlying_symbol=underlying,
                year=year,
                expiries_pulled=len(expiries),
                contracts_pulled=0,
                status=status,
                last_run_id=run_id,
                last_fetched_at=now,
                error_message=error_message,
            )
        )

    def _fetch_contracts(job: ContractJob) -> int:
        """Phase-2 fetcher: resolve + persist one expiry's contracts (metadata only)."""
        nonlocal contracts_persisted
        exchange, underlying, expiry = job
        contracts = resolve_contracts(
            client, exchange=exchange, underlying_symbol=underlying, expiry=expiry
        )
        for contract in contracts:
            registry.upsert_contract(
                exchange,
                underlying,
                expiry,
                contract.symbol,
                side=contract.contract_type,
                strike=contract.strike,
                run_id=run_id,
                fetched_at=now,
            )
        contracts_persisted += len(contracts)
        return len(contracts)

    def _on_contract(job: ContractJob, count: int, exc: BaseException | None) -> None:
        """Per-job hook — the persistence already happened in the fetcher.

        ``run_two_phase`` separates the fetch (which persists) from this result
        callback; the counts are accumulated in the fetcher, so this is the audit
        seam (a structlog line in the live route). ``del`` keeps the unused params
        explicit without a lint suppression.
        """
        del job, count, exc

    report = run_two_phase(
        cells,
        fetch_expiries=_fetch_expiries,
        fetch_contracts=_fetch_contracts,
        run_parallel=run_parallel,
        on_expiry_cell=_on_expiry_cell,
        on_contract=_on_contract,
        contract_skip=contract_skip,
        today=today,
    )

    note = _build_note(
        dry_run=dry_run,
        expiries_persisted=expiries_persisted,
        contracts_persisted=contracts_persisted,
    )
    return MetadataPullReport(
        dry_run=dry_run,
        years=tuple(years),
        underlyings=tuple(underlying for _exch, underlying in grid),
        expiry_cells=report.expiry_cells,
        cells_skipped=len(all_cells) - len(cells),
        expiry_failures=report.expiry_failures,
        expiries_persisted=expiries_persisted,
        contract_jobs=report.contract_jobs,
        contract_failures=report.contract_failures,
        contracts_persisted=contracts_persisted,
        note=note,
    )
