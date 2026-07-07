"""Tests for :mod:`brutex.dashboard.metadata_pull` (DEC-DERIV-METADATA-ORCHESTRATION-001).

PR-2 of the metadata/historical split: the metadata-only "Pull expiries + contracts"
orchestration that wires the dormant two-phase engine
(:mod:`brutex.orchestrator.universe_phases`) to the metadata sinks — the
:class:`~brutex.data.registry_store.RegistryStore` (expiry DATES + contract SYMBOLS)
+ the shared ``coverage_ledger`` rollup — and pulls **ZERO** historical candles.

Every path runs FULLY OFFLINE: a fake ``GrowwSDK`` (canned expiries + contracts,
NO ``get_historical_candles`` even reachable), a synchronous fake ``run_parallel``,
a REAL temp :class:`RegistryStore`, a tiny in-memory coverage sink, and an injected
``now`` / ``today``. The whole-of-life invariant — *no candle is ever fetched* — is
asserted directly (the fake SDK has no candle method, and the module imports neither
the sink nor the candle ingest). 100% line+branch on
``brutex.dashboard.metadata_pull``.
"""
# pyright: reportPrivateUsage=none

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import structlog
from brutex.adapters.groww.client import GrowwClient
from brutex.dashboard import metadata_pull as mp
from brutex.dashboard.metadata_pull import (
    GROWW_METADATA_HISTORY_FLOOR,
    MetadataClientFactory,
    pull_expiries_contracts,
)
from brutex.data.coverage_ledger import (
    STATUS_COMPLETE,
    STATUS_EXPIRIES_ONLY,
    STATUS_FAILED,
    STATUS_PENDING,
    CoverageRecord,
)
from brutex.data.errors import StoreWriteDeadlineExceeded
from brutex.data.mapping_integrity import (
    attempted_pairs_from_records,
    coverage_years_from_records,
)
from brutex.data.registry_store import RegistryStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.orchestrator.universe_phases import ItemResult

_NOW = datetime(2026, 6, 14, 9, 15, tzinfo=UTC)
_TODAY = date(2026, 6, 14)


# ── offline fakes ────────────────────────────────────────────────────────────


class _FakeSDK:
    """A metadata-only ``GrowwSDK``-shaped fake — canned expiries + contracts.

    Deliberately carries NO ``get_historical_candles`` method: the metadata pull
    must never reach for a candle, so the absence is the load-bearing assertion.
    ``calls`` records every method invocation so a test can prove what was (and was
    NOT) called.
    """

    def __init__(
        self,
        *,
        expiries: Mapping[tuple[str, str, int], Sequence[str]] | None = None,
        contracts: Mapping[tuple[str, str, str], Sequence[str]] | None = None,
        raise_expiry_for: frozenset[tuple[str, str]] = frozenset(),
        raise_contract_for: frozenset[tuple[str, str, str]] = frozenset(),
    ) -> None:
        self._expiries = dict(expiries or {})
        self._contracts = dict(contracts or {})
        self._raise_expiry_for = raise_expiry_for
        self._raise_contract_for = raise_contract_for
        self.calls: list[str] = []
        #: The DISTINCT underlying-symbol forms queried, in first-seen order — lets a
        #: test prove WHICH F&O forms reached the vendor (the fallback-form axis of
        #: DEC-FNO-UNDERLYING-RESOLVE-001), independent of the inner empty-retry budget.
        self.expiry_underlying_forms: list[str] = []
        self.contract_underlying_forms: list[str] = []

    def get_expiries(self, **kw: object) -> Mapping[str, object]:
        self.calls.append("get_expiries")
        exchange = str(kw["exchange"])
        underlying = str(kw["underlying_symbol"])
        if underlying not in self.expiry_underlying_forms:
            self.expiry_underlying_forms.append(underlying)
        if (exchange, underlying) in self._raise_expiry_for:
            raise RuntimeError(f"boom-expiries {exchange}-{underlying}")
        # The module always passes an int year (GrowwClient omits a None year), so
        # the fake key is (exchange, underlying, int) mirroring the canned map.
        year = kw["year"]
        assert isinstance(year, int)
        key = (exchange, underlying, year)
        return {"expiries": list(self._expiries.get(key, ()))}

    def get_contracts(self, **kw: object) -> Mapping[str, object]:
        self.calls.append("get_contracts")
        underlying = str(kw["underlying_symbol"])
        if underlying not in self.contract_underlying_forms:
            self.contract_underlying_forms.append(underlying)
        key = (
            str(kw["exchange"]),
            underlying,
            str(kw["expiry_date"]),
        )
        if key in self._raise_contract_for:
            raise RuntimeError(f"boom-contracts {key}")
        return {"contracts": list(self._contracts.get(key, ()))}


class _CoverageSink:
    """A tiny in-memory ``coverage_ledger`` rollup sink (PK upsert, list)."""

    def __init__(self, *, seed: Sequence[CoverageRecord] = ()) -> None:
        self._by_cell: dict[tuple[str, str, int], CoverageRecord] = {
            (r.exch_id, r.underlying_symbol, r.year): r for r in seed
        }

    def upsert_coverage(self, record: CoverageRecord) -> None:
        self._by_cell[(record.exch_id, record.underlying_symbol, record.year)] = record

    def list_coverage(self) -> list[CoverageRecord]:
        return list(self._by_cell.values())


class _SyncRunParallel:
    """A synchronous, deterministic ``RunParallel`` fake — zero threads.

    Maps *fn* over *items* IN ORDER, capturing a raise into ``ItemResult.exc``
    exactly like the production :class:`~brutex.orchestrator.parallel.PoolRunParallel`
    (so the two-phase engine's record-and-resume path is exercised offline). A
    generic ``__call__`` so the SAME instance serves both phases, matching the
    :class:`~brutex.orchestrator.universe_phases.RunParallel` Protocol.
    """

    def __call__[ItemT, ResultT](
        self, fn: Callable[[ItemT], ResultT], items: Sequence[ItemT]
    ) -> list[ItemResult[ItemT, ResultT]]:
        out: list[ItemResult[ItemT, ResultT]] = []
        for item in items:
            try:
                result = fn(item)
            except Exception as exc:  # the fake mirrors the pool's capture-not-propagate
                out.append(ItemResult(item=item, ok=False, result=None, exc=exc))
            else:
                out.append(ItemResult(item=item, ok=True, result=result, exc=None))
        return out


_sync_run_parallel = _SyncRunParallel()


def _instrument(symbol: str, *, exch: Exchange = Exchange.NSE) -> Instrument:
    return Instrument(
        exch_id=exch,
        security_id=1,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=0,
        tick_size=Decimal("0"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def _registry(tmp_path: Path) -> RegistryStore:
    return RegistryStore.open(tmp_path / "registry.duckdb")


def _factory(sdk: _FakeSDK) -> MetadataClientFactory:
    """A ``() -> GrowwClient`` factory wrapping *sdk* (no backoff sleep in tests)."""
    return lambda: GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)  # type: ignore[arg-type]


# ── happy persist (expiries + contracts land, ZERO candle calls) ─────────────


def test_happy_persist_expiries_and_contracts(tmp_path: Path) -> None:
    sdk = _FakeSDK(
        expiries={("NSE", "NIFTY", 2026): ["2026-06-25", "2026-07-30"]},
        contracts={
            ("NSE", "NIFTY", "2026-06-25"): [
                "NSE-NIFTY-25Jun26-24000-CE",
                "NSE-NIFTY-25Jun26-FUT",
            ],
            ("NSE", "NIFTY", "2026-07-30"): ["NSE-NIFTY-30Jul26-24500-PE"],
        },
    )
    registry = _registry(tmp_path)
    coverage = _CoverageSink()

    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
        run_id="r-happy",
    )

    # The metadata landed in the registry.
    assert registry.count_expiries() == 2
    assert registry.count_contracts() == 3
    contracts = registry.list_contracts()
    by_symbol = {c.contract_symbol: c for c in contracts}
    assert by_symbol["NSE-NIFTY-25Jun26-24000-CE"].side == "CE"
    assert by_symbol["NSE-NIFTY-25Jun26-24000-CE"].strike == 24000.0
    assert by_symbol["NSE-NIFTY-25Jun26-FUT"].side == "FUT"
    assert by_symbol["NSE-NIFTY-25Jun26-FUT"].strike is None
    assert by_symbol["NSE-NIFTY-30Jul26-24500-PE"].side == "PE"

    # The coverage rollup graduates to COMPLETE with the REAL enumerated contract count
    # (DEC-METADATA-CONTRACTS-COUNT-001): Phase 1 wrote expiries_only/0, Phase 2 enumerated
    # 3 contracts (2 for 25Jun + 1 for 30Jul) → rewritten complete/3.
    cov = coverage.list_coverage()
    assert len(cov) == 1
    assert cov[0].status == STATUS_COMPLETE
    assert cov[0].expiries_pulled == 2
    assert cov[0].contracts_pulled == 3
    assert cov[0].last_run_id == "r-happy"

    # The report is honest.
    assert report.dry_run is False
    assert report.expiry_cells == 1
    assert report.cells_skipped == 0
    assert report.expiry_failures == 0
    assert report.expiries_persisted == 2
    assert report.contract_jobs == 2
    assert report.contract_failures == 0
    assert report.contracts_persisted == 3
    assert report.store_write_deferrals == 0  # healthy path: zero deferrals (pinned)
    assert report.years == (2026,)
    assert report.underlyings == ("NIFTY",)
    assert "persisted 2 expiry" in report.note

    # THE WHOLE POINT: no candle was ever fetched (the fake SDK has no such method).
    assert "get_historical_candles" not in sdk.calls
    assert sdk.calls.count("get_expiries") == 1
    assert sdk.calls.count("get_contracts") == 2
    assert not hasattr(sdk, "get_historical_candles")
    registry.close()


def test_report_to_dict_is_json_safe(tmp_path: Path) -> None:
    sdk = _FakeSDK(expiries={("NSE", "NIFTY", 2026): ["2026-06-25"]})
    registry = _registry(tmp_path)
    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=_CoverageSink(),
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
    )
    payload = report.to_dict()
    assert payload["expiries_persisted"] == 1
    assert payload["store_write_deferrals"] == 0
    assert payload["years"] == [2026]
    assert payload["underlyings"] == ["NIFTY"]
    assert payload["dry_run"] is False
    registry.close()


# ── immutable-past skip (a settled past year is NOT re-fetched) ───────────────


def test_immutable_past_year_skipped(tmp_path: Path) -> None:
    sdk = _FakeSDK(expiries={("NSE", "NIFTY", 2024): ["2024-06-27"]})
    registry = _registry(tmp_path)
    # A PAST year (2024 < current 2026) already settled as expiries_only.
    coverage = _CoverageSink(
        seed=[
            CoverageRecord(
                exch_id="NSE",
                underlying_symbol="NIFTY",
                year=2024,
                expiries_pulled=5,
                contracts_pulled=0,
                status=STATUS_EXPIRIES_ONLY,
                last_run_id="old",
                last_fetched_at=_NOW,
            )
        ]
    )

    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2024],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
    )

    # The settled past cell was skipped — NO API call, NO new registry rows.
    assert report.expiry_cells == 0
    assert report.cells_skipped == 1
    assert report.expiries_persisted == 0
    assert sdk.calls == []
    assert registry.count_expiries() == 0
    registry.close()


def test_unsettled_past_year_is_pulled(tmp_path: Path) -> None:
    """A past year that is NOT settled (e.g. previously failed) is still pulled."""
    sdk = _FakeSDK(expiries={("NSE", "NIFTY", 2024): ["2024-06-27"]})
    registry = _registry(tmp_path)
    coverage = _CoverageSink(
        seed=[
            CoverageRecord(
                exch_id="NSE",
                underlying_symbol="NIFTY",
                year=2024,
                expiries_pulled=0,
                contracts_pulled=0,
                status=STATUS_FAILED,
                last_run_id="old",
                last_fetched_at=_NOW,
                error_message="earlier boom",
            )
        ]
    )

    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2024],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
    )

    assert report.expiry_cells == 1
    assert report.cells_skipped == 0
    assert report.expiries_persisted == 1
    # The previously-failed cell now settles as expiries_only.
    assert coverage.list_coverage()[0].status == STATUS_EXPIRIES_ONLY
    registry.close()


# ── current-year re-pull (always fetched even when settled) ──────────────────


def test_current_year_always_repulled(tmp_path: Path) -> None:
    sdk = _FakeSDK(expiries={("NSE", "NIFTY", 2026): ["2026-06-25", "2026-12-31"]})
    registry = _registry(tmp_path)
    # The CURRENT year already settled — but current-year is always re-pulled
    # (new expiries appear as the year runs).
    coverage = _CoverageSink(
        seed=[
            CoverageRecord(
                exch_id="NSE",
                underlying_symbol="NIFTY",
                year=2026,
                expiries_pulled=1,
                contracts_pulled=0,
                status=STATUS_EXPIRIES_ONLY,
                last_run_id="old",
                last_fetched_at=_NOW,
            )
        ]
    )

    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
    )

    assert report.expiry_cells == 1
    assert report.cells_skipped == 0
    assert report.expiries_persisted == 2
    assert registry.count_expiries() == 2
    registry.close()


def test_expired_expiry_contract_job_skipped(tmp_path: Path) -> None:
    """Phase-2: an EXPIRED expiry whose contracts are already persisted is skipped."""
    sdk = _FakeSDK(
        expiries={("NSE", "NIFTY", 2026): ["2026-01-30"]},
        contracts={("NSE", "NIFTY", "2026-01-30"): ["NSE-NIFTY-30Jan26-24000-CE"]},
    )
    registry = _registry(tmp_path)
    # Pre-persist a contract for the (now expired) 2026-01-30 expiry, so the Phase-2
    # settled set already contains the job; current today is 2026-06-14 → expired.
    registry.upsert_contract(
        "NSE",
        "NIFTY",
        date(2026, 1, 30),
        "NSE-NIFTY-30Jan26-24000-CE",
        side="CE",
        strike=24000.0,
        run_id="old",
        fetched_at=_NOW,
    )
    coverage = _CoverageSink()

    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
    )

    # Phase 1 still ran (current year), but Phase 2 skipped the expired+settled job.
    assert report.expiry_cells == 1
    assert report.contract_jobs == 0
    assert report.contracts_persisted == 0
    # No second get_contracts call for the skipped expiry.
    assert sdk.calls.count("get_contracts") == 0
    registry.close()


# ── failed cell (recorded, run resumes) ──────────────────────────────────────


def test_failed_cell_recorded_and_run_resumes(tmp_path: Path) -> None:
    sdk = _FakeSDK(
        expiries={("NSE", "BANKNIFTY", 2026): ["2026-06-25"]},
        contracts={("NSE", "BANKNIFTY", "2026-06-25"): ["NSE-BANKNIFTY-25Jun26-52000-CE"]},
        raise_expiry_for=frozenset({("NSE", "NIFTY")}),
    )
    registry = _registry(tmp_path)
    coverage = _CoverageSink()

    report = pull_expiries_contracts(
        [_instrument("NIFTY"), _instrument("BANKNIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
    )

    # The dead NIFTY cell is recorded as failed; BANKNIFTY still completed.
    assert report.expiry_cells == 2
    assert report.expiry_failures == 1
    assert report.expiries_persisted == 1
    assert report.contracts_persisted == 1

    by_underlying = {c.underlying_symbol: c for c in coverage.list_coverage()}
    assert by_underlying["NIFTY"].status == STATUS_FAILED
    assert by_underlying["NIFTY"].error_message is not None
    assert "boom-expiries" in by_underlying["NIFTY"].error_message
    # BANKNIFTY enumerated 1 contract → graduates to COMPLETE with the real count
    # (DEC-METADATA-CONTRACTS-COUNT-001). The failed NIFTY cell is never rewritten (it has
    # no discovered expiries, so it is not in the Phase-2 contract-count fold).
    assert by_underlying["BANKNIFTY"].status == STATUS_COMPLETE
    assert by_underlying["BANKNIFTY"].contracts_pulled == 1
    registry.close()


def test_zero_expiries_cell_is_pending(tmp_path: Path) -> None:
    """A cell that runs cleanly but discovers zero expiries → 'pending', no rows."""
    sdk = _FakeSDK(expiries={})  # NIFTY/2026 not present → empty list
    registry = _registry(tmp_path)
    coverage = _CoverageSink()

    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
    )

    assert report.expiry_cells == 1
    assert report.expiry_failures == 0
    assert report.expiries_persisted == 0
    assert report.contract_jobs == 0
    assert coverage.list_coverage()[0].status == STATUS_PENDING
    assert registry.count_expiries() == 0
    registry.close()


def test_attempted_empty_cell_records_a_coverage_row(tmp_path: Path) -> None:
    """DEC-MAPPING-INTEGRITY-ATTEMPTED-001: an attempted-and-EMPTY cell writes a coverage row.

    The defect-2 marker — an underlying whose expiry pull RAN but returned zero (the M&M
    silent-vanish mode) must persist a ``pending`` ``expiries_pulled=0`` cell, so that
    ``attempted_pairs_from_records`` includes it (distinguishable from a never-pulled name,
    which has NO row). This is what lets the mapping-integrity gate flag the real failure.
    """
    sdk = _FakeSDK(expiries={})  # NIFTY/2026 attempted but empty
    registry = _registry(tmp_path)
    coverage = _CoverageSink()

    pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
    )

    rows = coverage.list_coverage()
    assert len(rows) == 1  # the attempt left a row even though it was empty
    [row] = rows
    assert row.expiries_pulled == 0 and row.status == STATUS_PENDING
    # The attempted set (any row) includes the empty pair; covered_years (years>0) excludes it.
    assert attempted_pairs_from_records(rows) == {("NSE", "NIFTY")}
    assert coverage_years_from_records(rows) == {}
    registry.close()


# ── contracts-count persistence (DEC-METADATA-CONTRACTS-COUNT-001) ───────────


def test_expiries_but_zero_contracts_stays_expiries_only_by_design(tmp_path: Path) -> None:
    """A cell with ≥1 expiry but NO enumerated contracts stays expiries_only with 0 contracts.

    This is the honest metadata-only "by design" state — the contract fetch ran (the chain
    legitimately has no contracts in the fake) so the count is 0 and the cell never graduates
    to complete (covers the ``count == 0`` branch of ``_rewrite_contract_counts``).
    """
    sdk = _FakeSDK(
        expiries={("NSE", "NIFTY", 2026): ["2026-06-25"]},
        contracts={},  # the expiry resolves to an EMPTY contract set
    )
    registry = _registry(tmp_path)
    coverage = _CoverageSink()

    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
    )

    # A Phase-2 job ran (the expiry exists) but enumerated 0 contracts.
    assert report.contract_jobs == 1
    assert report.contracts_persisted == 0
    cov = coverage.list_coverage()[0]
    assert cov.status == STATUS_EXPIRIES_ONLY  # never graduates to complete at 0 contracts
    assert cov.contracts_pulled == 0
    assert cov.expiries_pulled == 1
    registry.close()


def test_year_boundary_expiry_credits_discovering_cell(tmp_path: Path) -> None:
    """A year=2025 cell whose ``get_expiries`` returns a 2026-Jan expiry credits the 2025 cell.

    Year-key correctness regression (DEC-METADATA-CONTRACTS-EXPIRY-FOLD-001): ``get_expiries
    (year=2025)`` is NOT filtered to 2025 by the vendor, so it can legitimately surface an
    NSE January weekly/monthly expiry dated ``2026-01-XX``. The contracts enumerated for
    that expiry MUST be credited to the cell that DISCOVERED it (the 2025 cell), not to a
    ``(NSE, NIFTY, 2026)`` cell that was never pulled in this run. Before the fix the count
    folded under ``expiry.year == 2026`` while ``_rewrite_contract_counts`` looked up the
    2025 cell → 0 → the 2025 cell stayed ``expiries_only`` / 0 contracts FOREVER (it is a
    past, settled year, so it is SKIPPED on every re-run and never self-heals).

    The only pulled year is 2025; the 2026 expiry it surfaces yields 2 contracts. Assert the
    2025 cell ends ``complete`` with the REAL count (2) — NOT ``expiries_only`` / 0.
    """
    sdk = _FakeSDK(
        expiries={("NSE", "NIFTY", 2025): ["2026-01-01"]},  # year=2025 query → 2026-Jan expiry
        contracts={
            ("NSE", "NIFTY", "2026-01-01"): [
                "NSE-NIFTY-01Jan26-24000-CE",
                "NSE-NIFTY-01Jan26-FUT",
            ],
        },
    )
    registry = _registry(tmp_path)
    coverage = _CoverageSink()

    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2025],  # ONLY 2025 is pulled — there is no (NSE, NIFTY, 2026) cell at all
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
        run_id="r-boundary",
    )

    # The contracts were enumerated + persisted under the (cross-year) expiry.
    assert report.contract_jobs == 1
    assert report.contracts_persisted == 2

    # The discovering 2025 cell — and ONLY the 2025 cell — carries the REAL count.
    cov = coverage.list_coverage()
    assert len(cov) == 1
    cell = cov[0]
    assert cell.year == 2025
    assert cell.status == STATUS_COMPLETE  # was permanently STATUS_EXPIRIES_ONLY before the fix
    assert cell.contracts_pulled == 2  # was permanently 0 before the fix
    assert cell.expiries_pulled == 1
    assert cell.last_run_id == "r-boundary"
    registry.close()


def test_shared_expiry_credits_every_discovering_cell(tmp_path: Path) -> None:
    """A single expiry surfaced by TWO pulled cells credits BOTH (cross-cell dedup robust).

    ``contract_jobs_from`` dedups a shared ``(exchange, underlying, expiry)`` to ONE Phase-2
    job (so its contracts are fetched once), discarding the year. The expiry-keyed fold
    (DEC-METADATA-CONTRACTS-EXPIRY-FOLD-001) attributes that one count to EVERY cell whose
    discovered-expiries list contains the expiry — here both the 2025 and 2026 cells surface
    the same ``2026-01-01`` expiry, so both graduate to ``complete`` with the count, instead
    of the count landing on only one cell (the year-key bug's compounding failure mode).
    """
    sdk = _FakeSDK(
        expiries={
            ("NSE", "NIFTY", 2025): ["2026-01-01"],
            ("NSE", "NIFTY", 2026): ["2026-01-01"],  # SAME expiry surfaced by both cells
        },
        contracts={("NSE", "NIFTY", "2026-01-01"): ["NSE-NIFTY-01Jan26-24000-CE"]},
    )
    registry = _registry(tmp_path)
    coverage = _CoverageSink()

    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2025, 2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
    )

    # The shared expiry is deduped to ONE contract job (fetched once).
    assert report.contract_jobs == 1
    assert report.contracts_persisted == 1

    # BOTH cells that discovered the expiry carry the count + graduate to complete.
    by_year = {c.year: c for c in coverage.list_coverage()}
    assert by_year[2025].status == STATUS_COMPLETE
    assert by_year[2025].contracts_pulled == 1
    assert by_year[2026].status == STATUS_COMPLETE
    assert by_year[2026].contracts_pulled == 1
    registry.close()


def test_failed_contract_job_contributes_zero_count(tmp_path: Path) -> None:
    """A Phase-2 contract fetch that RAISES contributes nothing to the cell's contract count.

    The cell keeps its Phase-1 expiries_only/0 rollup (the failed job's count is not folded —
    covers the ``exc is None`` False branch of ``_on_contract``).
    """
    sdk = _FakeSDK(
        expiries={("NSE", "NIFTY", 2026): ["2026-06-25"]},
        raise_contract_for=frozenset({("NSE", "NIFTY", "2026-06-25")}),
    )
    registry = _registry(tmp_path)
    coverage = _CoverageSink()

    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
    )

    assert report.contract_jobs == 1
    assert report.contract_failures == 1
    assert report.contracts_persisted == 0
    cov = coverage.list_coverage()[0]
    # The failed job contributed 0, so the cell stays expiries_only with 0 contracts.
    assert cov.status == STATUS_EXPIRIES_ONLY
    assert cov.contracts_pulled == 0
    registry.close()


# ── dry-run empty chain (no creds → 0, honest) ───────────────────────────────


def test_dry_run_empty_chain(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    coverage = _CoverageSink()

    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2026],
        dry_run=True,
        run_parallel=_sync_run_parallel,
        now=_NOW,
        today=_TODAY,
    )

    # The dry-run default client enumerates an EMPTY chain — 0 persisted, honest note.
    assert report.dry_run is True
    assert report.expiries_persisted == 0
    assert report.contracts_persisted == 0
    assert report.expiry_cells == 1
    assert registry.count_expiries() == 0
    assert registry.count_contracts() == 0
    # The cell still ran + got 0 expiries → pending coverage row.
    assert coverage.list_coverage()[0].status == STATUS_PENDING
    assert "dry-run: set Groww credentials" in report.note
    registry.close()


# ── the register_pull_meter WIRING at the metadata build site ─────────────────
# (DEC-RATE-METER-LIFECYCLE-001, audit C1: the severed call site left every test
#  green — these drive the REAL pull_expiries_contracts and assert the effect)


def test_live_pull_registers_exact_client_meter(tmp_path: Path) -> None:
    """MUTANT KILLED: ``register_pull_meter(session_rate, client, ...)`` at the
    metadata build site severed to ``pass`` → ``peek_meter()`` stays ``None``
    (the exact audit-C1 sed-mutant site)."""
    from brutex.dashboard.pull_controller import SessionRateController

    sdk = _FakeSDK(expiries={("NSE", "NIFTY", 2026): ["2026-06-25"]})
    client = GrowwClient(sdk=sdk, sleep=lambda _s: None, max_attempts=1)  # type: ignore[arg-type]
    holder = SessionRateController()
    registry = _registry(tmp_path)
    pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=_CoverageSink(),
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=lambda: client,
        session_rate=holder,
        now=_NOW,
        today=_TODAY,
    )
    assert holder.peek_meter() is client.meter, (
        "the metadata build site must register the EXACT client CallMeter"
    )
    registry.close()


def test_dry_run_never_registers_and_clears_stale_meter(tmp_path: Path) -> None:
    """The D1 pin (FAILS on pre-#1544 HEAD): the metadata DRY-RUN never registers.

    The real ``_build_dry_run_client`` GrowwClient carries a REAL ``CallMeter``
    (``client.py`` ``default_factory=CallMeter`` — the pre-#1544 in-code "no
    ``.meter``" claim was FALSE), so without the gate the dry-run would register
    its own synthetic meter (a fabricated MEASURED "flowing" in a warm process)
    AND overwrite a prior real pull's meter. MUTANT KILLED: the ``dry_run=dry_run``
    kwarg dropped at the site / the ``register_pull_meter`` dry-run branch severed
    → ``peek_meter()`` would be non-None (the dry-run client's meter or the stale
    one).
    """
    from brutex.adapters.call_meter import CallMeter
    from brutex.dashboard.pull_controller import SessionRateController

    holder = SessionRateController()
    stale = CallMeter()
    holder.register_meter(stale)  # an earlier real pull's meter in a warm process
    registry = _registry(tmp_path)
    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=_CoverageSink(),
        years=[2026],
        dry_run=True,  # NO client_factory: the REAL offline _build_dry_run_client
        run_parallel=_sync_run_parallel,
        session_rate=holder,
        now=_NOW,
        today=_TODAY,
    )
    assert report.dry_run is True
    assert holder.peek_meter() is None, (
        "a dry-run must DEREGISTER — never its own synthetic meter, never the stale one"
    )
    registry.close()


# ── default run_parallel + clock defaults (production seams) ─────────────────


def test_defaults_run_parallel_and_clock(tmp_path: Path) -> None:
    """No run_parallel / now / today injected → production pool + UTC-now defaults.

    Drives the dry-run empty chain so the default :class:`PoolRunParallel` runs a
    real (tiny) pool offline and the clock defaults resolve, without any network.
    """
    registry = _registry(tmp_path)
    coverage = _CoverageSink()

    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2026],
        dry_run=True,
    )

    assert report.dry_run is True
    assert report.expiries_persisted == 0
    # The default clock resolved to the real current year for the cell grid.
    assert report.years == (2026,)
    registry.close()


def test_dedup_underlyings_in_grid(tmp_path: Path) -> None:
    """Duplicate instruments collapse to one (exchange, underlying) grid cell."""
    sdk = _FakeSDK(expiries={("NSE", "NIFTY", 2026): ["2026-06-25"]})
    registry = _registry(tmp_path)
    coverage = _CoverageSink()

    report = pull_expiries_contracts(
        [_instrument("NIFTY"), _instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
    )

    assert report.underlyings == ("NIFTY",)
    assert report.expiry_cells == 1
    registry.close()


def test_history_floor_constant() -> None:
    """The metadata floor mirrors the FNO 'available from 2020' depth."""
    assert date(2020, 1, 1) == GROWW_METADATA_HISTORY_FLOOR


# ── module-internal seams (dry-run SDK + factory/rate live wiring, no network) ──


def test_empty_chain_sdk_is_metadata_only() -> None:
    """The dry-run SDK returns empty expiries/contracts and has NO candle method."""
    sdk = mp._EmptyChainSDK()
    assert sdk.get_expiries(exchange="NSE", underlying_symbol="NIFTY") == {"expiries": []}
    assert sdk.get_contracts(exchange="NSE", underlying_symbol="NIFTY") == {"contracts": []}
    # The whole point: the dry-run client can never even reach for a candle.
    assert not hasattr(sdk, "get_historical_candles")


def test_resolve_client_factory_injected_wins() -> None:
    """An injected client_factory is returned untouched (tests' seam)."""
    sentinel: MetadataClientFactory = lambda: GrowwClient(sdk=mp._EmptyChainSDK())  # type: ignore[arg-type]  # noqa: E731
    resolved = mp._resolve_client_factory(sentinel, dry_run=False, session_rate=None)
    assert resolved is sentinel


def test_resolve_client_factory_dry_run_default() -> None:
    """No factory + dry_run → the honest empty-chain client builder."""
    resolved = mp._resolve_client_factory(None, dry_run=True, session_rate=None)
    assert resolved is mp._build_dry_run_client
    client = resolved()
    assert isinstance(client, GrowwClient)


def test_resolve_client_factory_live_wires_rate_hooks() -> None:
    """No factory + live → a factory built over the session controller's hooks.

    Builds offline with a fake :class:`SessionRateController` (a real
    :class:`RateController`, no network), so the live ``_rate_hooks`` branch +
    factory construction are exercised without touching Groww. The returned
    ``_live`` closure body itself is live-only (``# pragma: no cover``).
    """
    from brutex.adapters.rate_common import RateController
    from brutex.dashboard.pull_controller import SessionRateController

    session = SessionRateController(factory=lambda: RateController(ceiling_rps=5.0))
    resolved = mp._resolve_client_factory(None, dry_run=False, session_rate=session)
    assert callable(resolved)
    assert resolved is not mp._build_dry_run_client


def test_rate_hooks_offline_returns_none() -> None:
    """Dry-run / no controller → all-None hooks (no pacing offline)."""
    assert mp._rate_hooks(None, dry_run=True) == (None, None, None)
    assert mp._rate_hooks(None, dry_run=False) == (None, None, None)


def test_rate_hooks_live_returns_controller_hooks() -> None:
    """Live + a controller → its pace / on_rate_limit / on_success hooks."""
    from brutex.adapters.rate_common import RateController
    from brutex.dashboard.pull_controller import SessionRateController

    controller = RateController(ceiling_rps=5.0)
    session = SessionRateController(factory=lambda: controller)
    pace, on_rate_limit, on_success = mp._rate_hooks(session, dry_run=False)
    assert pace == controller.pace
    assert on_rate_limit == controller.on_rate_limit
    assert on_success == controller.on_success


# ── LIVE incremental progress (DEC-METADATA-LIVE-PROGRESS-001) ────────────────


class _ProgressCollector:
    """A fake absolute ``on_progress`` hook — records every ``(completed, total, current)``.

    Proves the metadata pull EMITS incremental progress (the bar no longer freezes at
    ``0/N``): the spot-pull progress tests' style, but for the absolute 3-arg metadata
    hook (DEC-METADATA-LIVE-PROGRESS-001).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, str]] = []

    def __call__(self, completed: int, total: int, current: str) -> None:
        self.calls.append((completed, total, current))


def test_progress_emitted_incrementally_per_cell_and_per_contract(tmp_path: Path) -> None:
    """The pull fires progress per Phase-1 cell + per Phase-2 contract job, 0 → total.

    Two expiry cells (2025, 2026) each discover one expiry; each expiry yields one
    contract job. So total metadata UNITS = 2 cells + 2 contract jobs = 4, and
    ``completed`` must climb 1 → 4 monotonically — the exact movement that was frozen
    at ``0/N`` before this fix.
    """
    sdk = _FakeSDK(
        expiries={
            ("NSE", "NIFTY", 2025): ["2025-06-26"],
            ("NSE", "NIFTY", 2026): ["2026-06-25"],
        },
        contracts={
            ("NSE", "NIFTY", "2025-06-26"): ["NSE-NIFTY-26Jun25-24000-CE"],
            ("NSE", "NIFTY", "2026-06-25"): ["NSE-NIFTY-25Jun26-24500-PE"],
        },
    )
    registry = _registry(tmp_path)
    collector = _ProgressCollector()

    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=_CoverageSink(),
        years=[2025, 2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        on_progress=collector,
        now=_NOW,
        today=_TODAY,
    )

    # The hook fired once per unit (2 cells + 2 contract jobs) — never silent.
    assert len(collector.calls) == 4
    completed_seq = [c for c, _t, _cur in collector.calls]
    total_seq = [t for _c, t, _cur in collector.calls]
    # completed climbs 1 → 4 monotonically (the bar advances, never frozen at 0).
    assert completed_seq == [1, 2, 3, 4]
    # total is re-scaled from cells-only (2) to cells + the discovered jobs (4) at the
    # phase boundary: the LAST Phase-1 cell already reports the final total of 4.
    assert total_seq == [2, 4, 4, 4]
    # The final tick is 100% (completed == total) and labels the last contract job.
    final_completed, final_total, final_current = collector.calls[-1]
    assert final_completed == final_total == 4
    assert "contracts" in final_current
    # The Phase-1 ticks carry an "expiries" label; the Phase-2 ticks carry "contracts".
    assert collector.calls[0][2].endswith("expiries")
    assert collector.calls[2][2].endswith("contracts")
    # The report's own counts agree with the progress total denominator.
    assert report.expiry_cells == 2
    assert report.contract_jobs == 2
    registry.close()


def test_progress_total_rescales_to_phase2_job_count(tmp_path: Path) -> None:
    """One cell with TWO expiries → total re-scales 1 (cell) → 3 (cell + 2 jobs)."""
    sdk = _FakeSDK(
        expiries={("NSE", "NIFTY", 2026): ["2026-06-25", "2026-07-30"]},
        contracts={
            ("NSE", "NIFTY", "2026-06-25"): ["NSE-NIFTY-25Jun26-24000-CE"],
            ("NSE", "NIFTY", "2026-07-30"): ["NSE-NIFTY-30Jul26-24500-PE"],
        },
    )
    registry = _registry(tmp_path)
    collector = _ProgressCollector()

    pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=_CoverageSink(),
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        on_progress=collector,
        now=_NOW,
        today=_TODAY,
    )

    # 1 expiry cell, then 2 contract jobs → 3 progress ticks; total re-scales 1 → 3.
    assert [c for c, _t, _cur in collector.calls] == [1, 2, 3]
    assert [t for _c, t, _cur in collector.calls] == [3, 3, 3]
    registry.close()


def test_progress_failed_cell_ticks_but_adds_no_contract_jobs(tmp_path: Path) -> None:
    """A failed Phase-1 cell still advances the bar but contributes ZERO Phase-2 jobs.

    BANKNIFTY's expiry discovery succeeds (1 cell → 1 job); NIFTY's raises (1 cell, 0
    jobs). So total = 2 cells + 1 job = 3, and the failed cell ticks the bar without
    inflating the Phase-2 denominator (honest progress, never a phantom unit).
    """
    sdk = _FakeSDK(
        expiries={("NSE", "BANKNIFTY", 2026): ["2026-06-25"]},
        contracts={("NSE", "BANKNIFTY", "2026-06-25"): ["NSE-BANKNIFTY-25Jun26-52000-CE"]},
        raise_expiry_for=frozenset({("NSE", "NIFTY")}),
    )
    registry = _registry(tmp_path)
    collector = _ProgressCollector()

    pull_expiries_contracts(
        [_instrument("BANKNIFTY"), _instrument("NIFTY")],
        registry=registry,
        coverage_store=_CoverageSink(),
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        on_progress=collector,
        now=_NOW,
        today=_TODAY,
    )

    # 2 cells (one failed) + 1 contract job (only the successful cell) = 3 ticks.
    assert [c for c, _t, _cur in collector.calls] == [1, 2, 3]
    # total settles at 3 (2 cells + 1 job); the failed cell added no phantom job.
    assert collector.calls[-1][1] == 3
    registry.close()


def test_progress_no_cells_emits_nothing(tmp_path: Path) -> None:
    """When every cell is skipped (nothing to pull), the hook is never fired — total 0.

    An immutable past year already settled is skipped (Phase 1), leaving zero cells, so
    there is no work and no progress to report (the bar stays honestly at 0/0).
    """
    sdk = _FakeSDK(expiries={("NSE", "NIFTY", 2024): ["2024-06-27"]})
    registry = _registry(tmp_path)
    coverage = _CoverageSink(
        seed=[
            CoverageRecord(
                exch_id="NSE",
                underlying_symbol="NIFTY",
                year=2024,
                expiries_pulled=5,
                contracts_pulled=0,
                status=STATUS_EXPIRIES_ONLY,
                last_run_id="old",
                last_fetched_at=_NOW,
            )
        ]
    )
    collector = _ProgressCollector()

    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2024],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        on_progress=collector,
        now=_NOW,
        today=_TODAY,
    )

    assert collector.calls == []  # nothing pulled → nothing reported
    assert report.expiry_cells == 0
    assert report.cells_skipped == 1
    registry.close()


# ── DEC-FNO-UNDERLYING-RESOLVE-001: F&O underlying resolution + zero-expiry audit ──


def _equity(symbol: str, *, exch: Exchange = Exchange.NSE) -> Instrument:
    """An F&O-eligible CASH equity row with a SPOT trading symbol (no leg underlying).

    Mirrors the lean-universe persisted shape: ``underlying_symbol`` is ``None`` (only
    derivative legs carry it; the legs are discarded), so the F&O underlying for the
    expiry chain MUST be DERIVED from ``symbol_name`` (DEC-FNO-UNDERLYING-RESOLVE-001).
    """
    return Instrument(
        exch_id=exch,
        security_id=2,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=375,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
    )


def test_hyphenated_stock_resolves_to_fno_underlying(tmp_path: Path) -> None:
    """REGRESSION: spot ``BAJAJ-AUTO`` queries the F&O underlying ``BAJAJAUTO``.

    Before the fix the pull keyed ``get_expiries`` on the spot ``trading_symbol``
    (``BAJAJ-AUTO``) → a clean-but-EMPTY reply → 0 expiries → the whole underlying
    silently missed. After the fix the grid resolves the F&O underlying first, so the
    fake SDK (which only knows ``BAJAJAUTO``) returns the real chain.
    """
    sdk = _FakeSDK(
        expiries={("NSE", "BAJAJAUTO", 2026): ["2026-06-25"]},
        contracts={
            ("NSE", "BAJAJAUTO", "2026-06-25"): ["NSE-BAJAJAUTO-25Jun26-FUT"],
        },
    )
    registry = _registry(tmp_path)
    coverage = _CoverageSink()
    report = pull_expiries_contracts(
        [_equity("BAJAJ-AUTO")],
        registry=registry,
        coverage_store=coverage,
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
        run_id="r-bajaj",
    )

    # The chain was discovered against the F&O underlying spelling — NOT empty.
    assert registry.count_expiries() == 1
    assert registry.count_contracts() == 1
    contracts = registry.list_contracts()
    assert contracts[0].contract_symbol == "NSE-BAJAJAUTO-25Jun26-FUT"
    # The coverage rollup + report key on the RESOLVED F&O underlying.
    assert report.underlyings == ("BAJAJAUTO",)
    cov = coverage.list_coverage()
    assert cov[0].underlying_symbol == "BAJAJAUTO"
    assert cov[0].status == STATUS_COMPLETE
    registry.close()


def test_zero_expiry_audit_fires_when_underlying_empty(tmp_path: Path) -> None:
    """The observe-only WARNING fires for a clean-but-empty whole underlying.

    A pulled underlying that errors on NO cell yet discovers ZERO expiries is the
    silent-vanish signal — surfaced as ONE ``metadata.underlying_no_expiries``
    WARNING naming the resolved F&O underlying (DEC-FNO-UNDERLYING-RESOLVE-001 /
    DEC-SUSPICIOUS-EMPTY-FLAG-001 #919).
    """
    sdk = _FakeSDK(expiries={})  # every get_expiries returns empty
    registry = _registry(tmp_path)
    coverage = _CoverageSink()
    with structlog.testing.capture_logs() as logs:
        report = pull_expiries_contracts(
            [_equity("BAJAJ-AUTO")],
            registry=registry,
            coverage_store=coverage,
            years=[2026],
            dry_run=False,
            run_parallel=_sync_run_parallel,
            client_factory=_factory(sdk),
            now=_NOW,
            today=_TODAY,
            run_id="r-empty",
        )

    assert report.expiries_persisted == 0
    warnings = [e for e in logs if e["event"] == mp.METADATA_UNDERLYING_NO_EXPIRIES_EVENT]
    assert len(warnings) == 1
    assert warnings[0]["count"] == 1
    assert warnings[0]["sample"] == ["NSE-BAJAJAUTO"]
    assert warnings[0]["run_id"] == "r-empty"
    registry.close()


def test_zero_expiry_audit_silent_on_success(tmp_path: Path) -> None:
    """No WARNING when the underlying discovers ≥1 expiry (the healthy pull)."""
    sdk = _FakeSDK(expiries={("NSE", "NIFTY", 2026): ["2026-06-25"]})
    registry = _registry(tmp_path)
    with structlog.testing.capture_logs() as logs:
        pull_expiries_contracts(
            [_instrument("NIFTY")],
            registry=registry,
            coverage_store=_CoverageSink(),
            years=[2026],
            dry_run=False,
            run_parallel=_sync_run_parallel,
            client_factory=_factory(sdk),
            now=_NOW,
            today=_TODAY,
        )
    assert [e for e in logs if e["event"] == mp.METADATA_UNDERLYING_NO_EXPIRIES_EVENT] == []
    registry.close()


def test_zero_expiry_audit_excludes_errored_underlying(tmp_path: Path) -> None:
    """An underlying whose cell ERRORED is NOT flagged (already a recorded failure).

    The audit predicate excludes an errored underlying so a transient/auth failure is
    never double-counted as a silent miss — proven directly against the pure helper.
    """
    sdk = _FakeSDK(
        expiries={},
        raise_expiry_for=frozenset({("NSE", "BAJAJAUTO")}),
    )
    registry = _registry(tmp_path)
    with structlog.testing.capture_logs() as logs:
        report = pull_expiries_contracts(
            [_equity("BAJAJ-AUTO")],
            registry=registry,
            coverage_store=_CoverageSink(),
            years=[2026],
            dry_run=False,
            run_parallel=_sync_run_parallel,
            client_factory=_factory(sdk),
            now=_NOW,
            today=_TODAY,
        )
    assert report.expiry_failures == 1
    # The errored underlying is excluded from the zero-expiry audit.
    assert [e for e in logs if e["event"] == mp.METADATA_UNDERLYING_NO_EXPIRIES_EVENT] == []
    registry.close()


def test_underlyings_with_no_expiries_pure_predicate() -> None:
    """Direct unit cover of the pure predicate's three branches + _CellOutcome.merge."""
    outcomes = {
        ("NSE", "EMPTY"): mp._CellOutcome(any_expiry=False, errored=False),
        ("NSE", "HASEXP"): mp._CellOutcome(any_expiry=True, errored=False),
        ("NSE", "ERRORED"): mp._CellOutcome(any_expiry=False, errored=True),
    }
    assert mp.underlyings_with_no_expiries(outcomes) == [("NSE", "EMPTY")]
    # merge OR-folds across cells.
    base = mp._CellOutcome()
    assert base.merge(any_expiry=True, errored=False).any_expiry is True
    assert base.merge(any_expiry=False, errored=True).errored is True
    merged = base.merge(any_expiry=False, errored=False)
    assert merged.any_expiry is False and merged.errored is False


def test_fallback_form_tried_when_primary_empty(tmp_path: Path) -> None:
    """When the PRIMARY F&O form is empty, a FALLBACK form's chain is used.

    DEC-FNO-UNDERLYING-RESOLVE-001 wire-through: ``BAJAJ-AUTO`` resolves to the
    ordered forms ``('BAJAJAUTO', 'BAJAJ-AUTO')``. Here the fake SDK has NO chain for
    the PRIMARY ``BAJAJAUTO`` but a real chain for the FALLBACK spot spelling
    ``BAJAJ-AUTO`` — so the metadata pull must try the primary first, see it empty,
    then try the fallback and discover the chain. Proves the fallback forms genuinely
    reach the vendor (both expiries AND contracts) instead of only the primary.
    """
    sdk = _FakeSDK(
        # Primary form is empty; fallback spot spelling carries the chain.
        expiries={("NSE", "BAJAJ-AUTO", 2026): ["2026-06-25"]},
        contracts={("NSE", "BAJAJ-AUTO", "2026-06-25"): ["NSE-BAJAJ-AUTO-25Jun26-FUT"]},
    )
    registry = _registry(tmp_path)
    coverage = _CoverageSink()
    report = pull_expiries_contracts(
        [_equity("BAJAJ-AUTO")],
        registry=registry,
        coverage_store=coverage,
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
        run_id="r-fallback",
    )

    # The fallback form discovered the real chain — NOT a silent empty.
    assert registry.count_expiries() == 1
    assert registry.count_contracts() == 1
    # Both forms were tried for expiries, PRIMARY first then the FALLBACK spot spelling
    # (proving the wire-through reaches the fallback when the primary is empty).
    assert sdk.expiry_underlying_forms == ["BAJAJAUTO", "BAJAJ-AUTO"]
    # The contract fetch likewise falls back to the spelling that has the chain.
    assert sdk.contract_underlying_forms == ["BAJAJAUTO", "BAJAJ-AUTO"]
    # The registry/report key stays the stable PRIMARY form regardless of which answered.
    assert report.underlyings == ("BAJAJAUTO",)
    cov = coverage.list_coverage()
    assert cov[0].underlying_symbol == "BAJAJAUTO"
    assert cov[0].status == STATUS_COMPLETE
    # No silent-vanish warning — the chain WAS found (via the fallback form).
    registry.close()


def test_ampersand_fallback_composes_inside_each_form(tmp_path: Path) -> None:
    """The ``&`` → ``%26`` ampersand fallback still rides INSIDE each underlying form.

    DEC-GROWW-AMPERSAND-FALLBACK-001 must stay intact under the new wire-through:
    ``M&M`` resolves to a SINGLE F&O form (``('M&M',)`` — no alias/normalization
    bridge), but ``resolve_expiries`` composes the ``&`` → ``%26`` fallback on TOP, so
    a vendor that only answers the percent-encoded ``M%26M`` is still resolved. Here
    the fake SDK has the chain ONLY under ``M%26M`` (plain ``M&M`` empty), proving the
    ampersand axis is preserved.
    """
    sdk = _FakeSDK(
        expiries={("NSE", "M%26M", 2026): ["2026-06-25"]},
        contracts={("NSE", "M%26M", "2026-06-25"): ["NSE-M&M-25Jun26-FUT"]},
    )
    registry = _registry(tmp_path)
    coverage = _CoverageSink()
    report = pull_expiries_contracts(
        [_equity("M&M")],
        registry=registry,
        coverage_store=coverage,
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
        run_id="r-mm",
    )
    assert registry.count_expiries() == 1
    assert registry.count_contracts() == 1
    # The persisted underlying is the canonical primary ``M&M`` (the %26 form is a
    # FETCH-only encoding, never the stored key).
    assert report.underlyings == ("M&M",)
    assert coverage.list_coverage()[0].status == STATUS_COMPLETE
    registry.close()


def test_canonical_symbol_single_form_one_call(tmp_path: Path) -> None:
    """A canonical symbol (NIFTY) collapses to ONE form → exactly one get_expiries call.

    DEC-FNO-UNDERLYING-RESOLVE-001 byte-unchanged guarantee: NIFTY/BANKNIFTY/SENSEX
    carry a single F&O form, so the wire-through issues exactly ONE SDK expiry call —
    no extra fallback calls, identical to the pre-fix behaviour.
    """
    sdk = _FakeSDK(expiries={("NSE", "NIFTY", 2026): ["2026-06-25"]})
    registry = _registry(tmp_path)
    pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=_CoverageSink(),
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
    )
    # Exactly one underlying form was ever queried — the single canonical NIFTY.
    assert sdk.expiry_underlying_forms == ["NIFTY"]
    assert sdk.calls.count("get_expiries") == 1
    registry.close()


def test_first_nonempty_over_forms_helper() -> None:
    """Direct unit cover of the pure ``_first_nonempty_over_forms`` helper.

    First non-empty wins; the LAST (empty) result is returned when ALL forms are empty
    (so a genuinely-absent chain records the same empty as before — Golden Rule #1).
    """
    calls: list[str] = []

    def _fetch(form: str) -> list[str]:
        calls.append(form)
        return ["hit"] if form == "B" else []

    # A → empty, B → non-empty: stops at B (C never called).
    assert mp._first_nonempty_over_forms(("A", "B", "C"), _fetch, is_empty=lambda r: not r) == [
        "hit"
    ]
    assert calls == ["A", "B"]
    # All empty: every form tried, the last empty result returned.
    calls.clear()
    assert mp._first_nonempty_over_forms(("X", "Y"), _fetch, is_empty=lambda r: not r) == []
    assert calls == ["X", "Y"]


# ── Phase-2 failure → re-run re-pull (DEC-PHASE2-REGISTRY-REBUILD-001) ────────


def test_phase2_failure_then_rerun_repulls_contracts(tmp_path: Path) -> None:
    """THE re-pull regression (forensic-audit BLOCKER #4): failed contracts are re-pulled.

    Run 1 (a PAST year): Phase 1 discovers + persists the expiry — the cell settles
    ``expiries_only`` — and Phase 2's contract fetch RAISES, so zero contracts persist.
    Run 2: the settled past-year cell is skipped by the Phase-1 incremental skip, so the
    in-memory discoveries carry no expiry for it; before the fix ``contract_jobs_from``
    (in-memory only) rebuilt NO job — the failed contracts had NO re-pull path and the
    year's contracts were lost forever. After the fix the Phase-2 worklist is rebuilt
    from the REGISTRY's persisted expiries too, so run 2 re-pulls exactly the failed
    contracts while STILL making zero expiry calls (the Phase-1 skip is preserved).
    """
    registry = _registry(tmp_path)
    coverage = _CoverageSink()

    # Run 1 — the expiry lands, the contract job raises.
    sdk1 = _FakeSDK(
        expiries={("NSE", "NIFTY", 2024): ["2024-06-27"]},
        raise_contract_for=frozenset({("NSE", "NIFTY", "2024-06-27")}),
    )
    report1 = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2024],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk1),
        now=_NOW,
        today=_TODAY,
        run_id="r-one",
    )
    assert report1.contract_failures == 1
    assert registry.count_expiries() == 1
    assert registry.count_contracts() == 0
    # The cell settled expiries_only — exactly the state that trapped the year.
    assert coverage.list_coverage()[0].status == STATUS_EXPIRIES_ONLY

    # Run 2 — the vendor recovers; the settled cell is skipped but the job is rebuilt.
    sdk2 = _FakeSDK(
        contracts={("NSE", "NIFTY", "2024-06-27"): ["NSE-NIFTY-27Jun24-24000-CE"]},
    )
    report2 = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2024],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk2),
        now=_NOW,
        today=_TODAY,
        run_id="r-two",
    )
    # Phase 1 stays skipped (the incremental skip is intact) …
    assert report2.expiry_cells == 0
    assert report2.cells_skipped == 1
    assert sdk2.calls.count("get_expiries") == 0
    # … yet the failed contracts ARE re-pulled via the registry-rebuilt job.
    assert report2.contract_jobs == 1
    assert report2.contracts_persisted == 1
    assert sdk2.calls.count("get_contracts") == 1
    assert registry.count_contracts() == 1

    # Run 3 — converged: the contracts are captured + expired, nothing re-pulls.
    sdk3 = _FakeSDK()
    report3 = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2024],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk3),
        now=_NOW,
        today=_TODAY,
        run_id="r-three",
    )
    assert report3.contract_jobs == 0
    assert sdk3.calls == []
    registry.close()


def test_registry_expiry_outside_grid_never_rebuilt(tmp_path: Path) -> None:
    """A persisted expiry for an underlying OUTSIDE this run's grid is never rebuilt.

    The registry may hold other underlyings' chains (an earlier whole-universe pull);
    a selected-symbols pull must not resurrect their jobs — the rebuilt worklist is
    restricted to this run's (exchange, underlying) grid (scope stays the selection).
    """
    sdk = _FakeSDK()  # NIFTY's chain is empty; BANKNIFTY is not in this run at all
    registry = _registry(tmp_path)
    registry.upsert_expiry("NSE", "BANKNIFTY", date(2024, 6, 27), run_id="old", fetched_at=_NOW)

    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=_CoverageSink(),
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
    )

    # BANKNIFTY's persisted expiry rebuilt NO job — no contract call ever issued.
    assert report.contract_jobs == 0
    assert sdk.calls.count("get_contracts") == 0
    registry.close()


def test_persisted_and_rediscovered_expiry_fetched_once(tmp_path: Path) -> None:
    """A current-year expiry both persisted (run 1) and re-discovered → ONE fetch.

    The union must de-dup: the registry row and this run's in-memory discovery name
    the SAME (exchange, underlying, expiry), so exactly one Phase-2 job runs.
    """
    sdk = _FakeSDK(
        expiries={("NSE", "NIFTY", 2026): ["2026-06-25"]},
        contracts={("NSE", "NIFTY", "2026-06-25"): ["NSE-NIFTY-25Jun26-24000-CE"]},
    )
    registry = _registry(tmp_path)
    registry.upsert_expiry("NSE", "NIFTY", date(2026, 6, 25), run_id="old", fetched_at=_NOW)

    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=_CoverageSink(),
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
    )

    assert report.contract_jobs == 1
    assert sdk.calls.count("get_contracts") == 1
    registry.close()


def test_progress_counts_registry_rebuilt_jobs_when_all_cells_skipped(tmp_path: Path) -> None:
    """All cells skipped + one registry-rebuilt job → the bar reads 1/1, never 1/0.

    With zero Phase-1 cells the boundary rescale never fires, so the up-front seed
    must size ``total`` to the rebuilt Phase-2 worklist — the denominator can never
    read 0 while a rebuilt contract job ticks the bar.
    """
    sdk = _FakeSDK(
        contracts={("NSE", "NIFTY", "2024-06-27"): ["NSE-NIFTY-27Jun24-24000-CE"]},
    )
    registry = _registry(tmp_path)
    registry.upsert_expiry("NSE", "NIFTY", date(2024, 6, 27), run_id="old", fetched_at=_NOW)
    # The past-year cell is already settled expiries_only → skipped in Phase 1.
    coverage = _CoverageSink(
        seed=[
            CoverageRecord(
                exch_id="NSE",
                underlying_symbol="NIFTY",
                year=2024,
                expiries_pulled=1,
                contracts_pulled=0,
                status=STATUS_EXPIRIES_ONLY,
                last_run_id="old",
                last_fetched_at=_NOW,
            )
        ]
    )
    collector = _ProgressCollector()

    report = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2024],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        on_progress=collector,
        now=_NOW,
        today=_TODAY,
    )

    assert report.expiry_cells == 0
    assert report.contract_jobs == 1
    # Exactly one tick — the rebuilt contract job — with an honest 1/1 denominator.
    assert collector.calls == [(1, 1, "NSE-NIFTY 2024-06-27 contracts")]
    registry.close()


# ── store-write deadline deferral (DEC-STORE-WRITE-DEADLINE-001) ──────────────
#
# The metadata twin of the #999 window defer: a WEDGED registry/coverage DuckDB
# write is bounded by the store-write deadline and the affected UNDERLYING's
# cell/job is DEFERRED to the next pull — the pull COMPLETES (no raise, no
# freeze), the deferral is loudly WARNed + counted on the report, and the
# deferred unit re-attempts on the next pull. The hang is DETERMINISTIC (a proxy
# conn blocking on a gate with a 30 s safety, released in ``finally``).


class _RegistryGateConn:
    """Proxy over the registry's DuckDB conn that HANGS on a matching statement."""

    def __init__(self, real: object, *, hang_on: str) -> None:
        self._real = real
        self._hang_on = hang_on
        self.gate = threading.Event()
        self.entered = threading.Event()

    def execute(self, sql: str, params: list[object] | None = None) -> object:
        if self._hang_on in sql:
            self.entered.set()
            self.gate.wait(timeout=30.0)
        real: Any = self._real
        if params is None:
            return real.execute(sql)
        return real.execute(sql, params)

    def close(self) -> None:
        real: Any = self._real
        real.close()


class _DeferringCoverageSink:
    """A coverage sink whose Nth upsert raises the TRANSIENT store-write defer."""

    def __init__(self, *, fail_on_calls: frozenset[int]) -> None:
        self.inner = _CoverageSink()
        self.calls = 0
        self._fail_on_calls = fail_on_calls

    def upsert_coverage(self, record: CoverageRecord) -> None:
        self.calls += 1
        if self.calls in self._fail_on_calls:
            raise StoreWriteDeadlineExceeded(0.2, "coverage", "upsert_coverage")
        self.inner.upsert_coverage(record)

    def list_coverage(self) -> list[CoverageRecord]:
        return self.inner.list_coverage()


def _wedge_registry(
    registry: RegistryStore, monkeypatch: pytest.MonkeyPatch, *, hang_on: str
) -> _RegistryGateConn:
    """Swap the registry's conn for a gate proxy + shrink the deadline to 0.2 s."""
    monkeypatch.setenv("BRUTEX_REGISTRY_WRITE_DEADLINE_S", "0.2")
    proxy = _RegistryGateConn(registry._conn, hang_on=hang_on)
    registry._conn = proxy
    return proxy


def _wait_registry_healed(registry: RegistryStore, *, timeout: float = 10.0) -> None:
    """Poll the wedge latch until the zombie completes (never hangs the suite)."""
    deadline_at = time.monotonic() + timeout
    while registry._write_guard.wedged:
        if time.monotonic() > deadline_at:  # pragma: no cover - only on a real hang
            pytest.fail("registry did not self-heal within the watchdog bound")
        time.sleep(0.005)


def test_phase1_expiry_write_defer_completes_pull_and_defers_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The today-freeze/crash regression: a wedged Phase-1 registry write no longer
    fails (or hangs) the WHOLE pull — the cell is deferred, loudly, per underlying."""
    sdk = _FakeSDK(
        expiries={("NSE", "NIFTY", 2026): ["2026-06-25"]},
        contracts={("NSE", "NIFTY", "2026-06-25"): ["NSE-NIFTY-25Jun26-24000-CE"]},
    )
    registry = _registry(tmp_path)
    proxy = _wedge_registry(registry, monkeypatch, hang_on="INSERT OR REPLACE INTO expiries")
    coverage = _CoverageSink()
    start = time.monotonic()
    try:
        with structlog.testing.capture_logs() as logs:
            report = pull_expiries_contracts(
                [_instrument("NIFTY")],
                registry=registry,
                coverage_store=coverage,
                years=[2026],
                dry_run=False,
                run_parallel=_sync_run_parallel,
                client_factory=_factory(sdk),
                now=_NOW,
                today=_TODAY,
                run_id="r-defer",
            )
        elapsed = time.monotonic() - start
        assert proxy.entered.is_set()  # the wedge genuinely engaged
        assert elapsed < 10.0  # the pull COMPLETED within one small deadline
        # The deferral is counted + honest: nothing durably persisted. The engine
        # ALSO counts the cell as a Phase-1 failure (the fetcher raised), so the
        # pipeline gate is doubly red.
        assert report.store_write_deferrals == 1
        assert report.expiry_failures == 1
        assert report.expiries_persisted == 0
        assert report.to_dict()["store_write_deferrals"] == 1
        # A deferred cell contributes NO Phase-2 jobs (no vendor calls against
        # unpersisted expiries).
        assert report.contract_jobs == 0
        assert "get_contracts" not in sdk.calls
        # THE SETTLE-CORRECTNESS RULE: the cell records ``failed`` — NEVER settled
        # — with an honest 0 expiries_pulled, so the next pull re-attempts it.
        (cell,) = coverage.list_coverage()
        assert cell.status == STATUS_FAILED
        assert cell.expiries_pulled == 0
        assert cell.error_message is not None
        assert "wedged store volume" in cell.error_message
        # The deferral is LOUD.
        deferred = [r for r in logs if r["event"] == mp.METADATA_EXPIRY_WRITE_DEFERRED_EVENT]
        assert len(deferred) == 1
        assert deferred[0]["exchange"] == "NSE"
        assert deferred[0]["underlying"] == "NIFTY"
        assert deferred[0]["year"] == 2026
        assert deferred[0]["error_type"] == "StoreWriteDeadlineExceeded"
        assert deferred[0]["defer_seconds"] == 0.2
        assert deferred[0]["latched"] is False
    finally:
        proxy.gate.set()
    # RE-ATTEMPT ON THE NEXT PULL (the #999 recovery pin): the volume heals, the
    # failed cell is re-pulled, and everything lands + settles.
    _wait_registry_healed(registry)
    report2 = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
        run_id="r-recovered",
    )
    assert report2.store_write_deferrals == 0
    assert registry.count_expiries() == 1
    assert registry.count_contracts() == 1
    (cell2,) = coverage.list_coverage()
    assert cell2.status == STATUS_COMPLETE
    assert cell2.last_run_id == "r-recovered"
    registry.close()


def test_phase2_contract_write_defer_fails_job_latches_sibling_and_repulls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged Phase-2 contract batch defers PER JOB: the breaching job burns ONE
    deadline, the sibling fails fast on the latch (no deadline stacking), both are
    counted + WARNed, and the un-persisted job re-pulls on the next run."""
    sdk = _FakeSDK(
        expiries={("NSE", "NIFTY", 2026): ["2026-06-25", "2026-07-30"]},
        contracts={
            ("NSE", "NIFTY", "2026-06-25"): ["NSE-NIFTY-25Jun26-24000-CE"],
            ("NSE", "NIFTY", "2026-07-30"): ["NSE-NIFTY-30Jul26-24500-PE"],
        },
    )
    registry = _registry(tmp_path)
    proxy = _wedge_registry(registry, monkeypatch, hang_on="INSERT OR REPLACE INTO contracts")
    coverage = _CoverageSink()
    start = time.monotonic()
    try:
        with structlog.testing.capture_logs() as logs:
            report = pull_expiries_contracts(
                [_instrument("NIFTY")],
                registry=registry,
                coverage_store=coverage,
                years=[2026],
                dry_run=False,
                run_parallel=_sync_run_parallel,
                client_factory=_factory(sdk),
                now=_NOW,
                today=_TODAY,
                run_id="r-p2",
            )
        assert time.monotonic() - start < 10.0  # ONE deadline, not one per job
        # Both jobs deferred: the first burned the 0.2 s deadline, the second hit
        # the wedge latch instantly.
        assert report.contract_failures == 2
        assert report.store_write_deferrals == 2
        assert report.contracts_persisted == 0
        deferred = [r for r in logs if r["event"] == mp.METADATA_CONTRACT_WRITE_DEFERRED_EVENT]
        assert len(deferred) == 2
        assert [r["latched"] for r in deferred] == [False, True]
        # Phase 1 was healthy: the cell keeps its honest expiries_only rollup.
        (cell,) = coverage.list_coverage()
        assert cell.status == STATUS_EXPIRIES_ONLY
        assert cell.expiries_pulled == 2
    finally:
        proxy.gate.set()
    # RE-ATTEMPT: the healed registry re-pulls the un-persisted job (the breached
    # job's zombie batch landed late + idempotently — harmless by construction).
    _wait_registry_healed(registry)
    report2 = pull_expiries_contracts(
        [_instrument("NIFTY")],
        registry=registry,
        coverage_store=coverage,
        years=[2026],
        dry_run=False,
        run_parallel=_sync_run_parallel,
        client_factory=_factory(sdk),
        now=_NOW,
        today=_TODAY,
        run_id="r-p2-recovered",
    )
    assert report2.store_write_deferrals == 0
    assert report2.contract_failures == 0
    assert registry.count_contracts() == 2  # both legs present after recovery
    registry.close()


def test_phase1_coverage_write_defer_is_swallowed_and_counted(tmp_path: Path) -> None:
    """A deferred Phase-1 coverage ROLLUP write is swallowed (safe: the cell's
    persisted status is already non-settled) + counted; the phase continues."""
    sdk = _FakeSDK(
        expiries={("NSE", "NIFTY", 2026): ["2026-06-25"]},
        contracts={("NSE", "NIFTY", "2026-06-25"): ["NSE-NIFTY-25Jun26-24000-CE"]},
    )
    registry = _registry(tmp_path)
    # Call 1 = the Phase-1 cell rollup (deferred); call 2 = the post-Phase-2
    # rewrite (healthy — so the final rollup still graduates).
    coverage = _DeferringCoverageSink(fail_on_calls=frozenset({1}))
    with structlog.testing.capture_logs() as logs:
        report = pull_expiries_contracts(
            [_instrument("NIFTY")],
            registry=registry,
            coverage_store=coverage,
            years=[2026],
            dry_run=False,
            run_parallel=_sync_run_parallel,
            client_factory=_factory(sdk),
            now=_NOW,
            today=_TODAY,
            run_id="r-cov",
        )
    assert report.store_write_deferrals == 1
    deferred = [r for r in logs if r["event"] == mp.METADATA_COVERAGE_WRITE_DEFERRED_EVENT]
    assert len(deferred) == 1
    assert deferred[0]["underlying"] == "NIFTY"
    # The registry write was HEALTHY, so the cell still seeded Phase 2 and the
    # rewrite (call 2, healthy) graduated the rollup.
    assert report.expiries_persisted == 1
    assert report.contracts_persisted == 1
    (cell,) = coverage.list_coverage()
    assert cell.status == STATUS_COMPLETE
    registry.close()


def test_rewrite_counts_defer_breaks_and_counts(tmp_path: Path) -> None:
    """The post-Phase-2 rewrite BREAKS on the first defer (the rest would only
    fail fast on the latch), WARNs with the remaining-cell count, and folds ONE
    deferral into the report."""
    sdk = _FakeSDK(
        expiries={
            ("NSE", "AAA", 2026): ["2026-06-25"],
            ("NSE", "BBB", 2026): ["2026-06-25"],
        },
        contracts={
            ("NSE", "AAA", "2026-06-25"): ["NSE-AAA-25Jun26-100-CE"],
            ("NSE", "BBB", "2026-06-25"): ["NSE-BBB-25Jun26-100-CE"],
        },
    )
    registry = _registry(tmp_path)
    # Calls 1+2 = the two Phase-1 rollups (healthy); calls 3+4 would be the two
    # rewrites — 3 defers, so the loop must BREAK and never attempt call 4.
    coverage = _DeferringCoverageSink(fail_on_calls=frozenset({3, 4}))
    with structlog.testing.capture_logs() as logs:
        report = pull_expiries_contracts(
            [_instrument("AAA"), _instrument("BBB")],
            registry=registry,
            coverage_store=coverage,
            years=[2026],
            dry_run=False,
            run_parallel=_sync_run_parallel,
            client_factory=_factory(sdk),
            now=_NOW,
            today=_TODAY,
            run_id="r-rw",
        )
    assert report.store_write_deferrals == 1  # ONE counted defer — then the break
    assert coverage.calls == 3  # the second rewrite was never attempted
    deferred = [r for r in logs if r["event"] == mp.METADATA_COUNTS_WRITE_DEFERRED_EVENT]
    assert len(deferred) == 1
    assert deferred[0]["cells_remaining"] == 2
    assert deferred[0]["latched"] is False
    # Fail-safe: the un-rewritten cells keep their honest Phase-1 rollup.
    by_underlying = {r.underlying_symbol: r for r in coverage.list_coverage()}
    assert by_underlying["AAA"].status == STATUS_EXPIRIES_ONLY
    assert by_underlying["BBB"].status == STATUS_EXPIRIES_ONLY
    registry.close()


# ── metadata pool workers carry the run id (DEC-PULL-BEAT-PERRUN-001 fix round) ──


def test_metadata_pool_pace_beat_reaches_the_runs_keyed_channel(tmp_path: Path) -> None:
    """The DEFAULT :class:`PoolRunParallel` propagates the driver's ambient run id to
    its workers (contextvars copy per task), so a paced fetcher's
    ``emit_progress_beat`` during the metadata phase attributes to the run's KEYED
    channel — zero ``foreign_beat_dropped`` WARNs on a healthy pipeline pull (the
    fix-round regression: pre-fix these beats resolved to "" and DROPPED, re-opening
    the DEC-PULL-CONTENTION-ANTISTALL-001 false-stall class)."""
    import logging

    from brutex.observability.correlation import pull_id_scope
    from brutex.observability.progress_beat import (
        register_progress_beat,
        reset_progress_beats,
    )

    class _BeatingSDK(_FakeSDK):
        """The fake SDK, emitting the pace-return-shaped beat per vendor call."""

        def get_expiries(self, **kw: object) -> Mapping[str, object]:
            from brutex.observability.progress_beat import emit_progress_beat

            emit_progress_beat()  # the RateController.pace return shape
            return super().get_expiries(**kw)

        def get_contracts(self, **kw: object) -> Mapping[str, object]:
            from brutex.observability.progress_beat import emit_progress_beat

            emit_progress_beat()
            return super().get_contracts(**kw)

    drop_records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if "foreign_beat_dropped" in record.getMessage():
                drop_records.append(record)

    handler = _Capture()
    beat_logger = logging.getLogger("brutex.observability.progress_beat")
    beat_logger.addHandler(handler)
    reset_progress_beats()
    beats: list[int] = []
    register_progress_beat(lambda: beats.append(1), pull_id="abc123def456")
    sdk = _BeatingSDK(
        expiries={("NSE", "NIFTY", 2026): ["2026-06-25"]},
        contracts={("NSE", "NIFTY", "2026-06-25"): ["NIFTY26JUN25000CE"]},
    )
    registry = _registry(tmp_path)
    coverage = _CoverageSink()
    try:
        with pull_id_scope("abc123def456"):  # the job's run scope shape
            report = pull_expiries_contracts(
                [_instrument("NIFTY")],
                registry=registry,
                coverage_store=coverage,
                years=[2026],
                dry_run=False,
                client_factory=_factory(sdk),  # DEFAULT run_parallel → the real pool
                now=_NOW,
                today=_TODAY,
            )
    finally:
        reset_progress_beats()
        beat_logger.removeHandler(handler)
        registry.close()
    assert report.expiries_persisted == 1
    assert len(beats) >= 2  # one per vendor call — expiries + contracts, all delivered
    assert drop_records == []  # a healthy metadata pull emits ZERO foreign-drop WARNs
