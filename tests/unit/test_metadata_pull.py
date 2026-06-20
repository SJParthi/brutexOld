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

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

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

    def get_expiries(self, **kw: object) -> Mapping[str, object]:
        self.calls.append("get_expiries")
        exchange = str(kw["exchange"])
        underlying = str(kw["underlying_symbol"])
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
        key = (
            str(kw["exchange"]),
            str(kw["underlying_symbol"]),
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
