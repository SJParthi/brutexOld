"""Integration tests for the /api/v1/universe/mapping-integrity route.

DEC-MAPPING-INTEGRITY-RECONCILE-001 / DEC-MAPPING-INTEGRITY-ATTEMPTED-001 — exercises the
thin route over a real wired :class:`InstrumentStore`: an options-eligible spot that was
ATTEMPTED yet pulled zero expiries (M&M-style — a coverage row with ``expiries_pulled=0``)
turns the verdict RED; a never-attempted spot (no coverage row, a selective pull) stays
GREEN as ``not_attempted``; a fully-covered spot stays green; an unwired store → 503.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")

from brutex.dashboard.app import DashboardState, _coverage_years, build_app
from brutex.data.coverage_ledger import CoverageRecord, classify_cell
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient


def _eq(symbol: str) -> Instrument:
    """An NSE cash-equity row — F&O-eligible by construction in the lean store."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(symbol)) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


def _cov(underlying: str, year: int, *, expiries: int = 2) -> CoverageRecord:
    return CoverageRecord(
        exch_id="NSE",
        underlying_symbol=underlying,
        year=year,
        expiries_pulled=expiries,
        contracts_pulled=expiries * 10,
        status=classify_cell(expiries, expiries * 10, None),
        last_run_id="run-1",
        last_fetched_at=datetime(2026, 5, 30, 9, 15, tzinfo=UTC),
    )


@pytest.fixture
def store(tmp_path: Path):
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    # Two F&O-eligible equities: RELIANCE (will be fully covered) + M&M (will pull nothing).
    s.upsert_many(
        [_eq("RELIANCE"), _eq("M&M")],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


def test_mapping_integrity_flags_attempted_zero_coverage_spot(store: InstrumentStore) -> None:
    """RELIANCE covered; M&M ATTEMPTED yet zero expiries (a row) → RED, M&M gating none.

    The attempted-and-empty marker is a coverage row with ``expiries_pulled=0`` (what Phase-1
    expiry discovery writes for a silent-vanish underlying), so the gate still catches the real
    failure (DEC-MAPPING-INTEGRITY-ATTEMPTED-001).
    """
    for year in _coverage_years():
        store.upsert_coverage(_cov("RELIANCE", year))
    # M&M was attempted but discovered nothing — one zero-expiry cell records the attempt.
    store.upsert_coverage(_cov("M&M", _coverage_years()[0], expiries=0))
    client = TestClient(build_app(DashboardState(), instrument_store=store))

    body = client.get("/api/v1/universe/mapping-integrity").json()

    assert body["is_green"] is False
    assert body["total_eligible"] == 2 and body["fully_covered"] == 1
    assert body["gating_failures"] == 1
    assert body["not_attempted"] == 0
    [under] = body["under_pulled"]
    assert under["symbol"] == "M&M" and under["reason"] == "none"
    assert under["whitelisted"] is False
    assert body["expected_years"] == _coverage_years()


def test_mapping_integrity_green_on_clean_selective_pull(store: InstrumentStore) -> None:
    """The headline fix: a selective pull (only RELIANCE attempted) reads GREEN, not RED.

    M&M was NEVER attempted (no coverage row), so it is the neutral ``not_attempted`` state,
    never a gating ``none`` — the false-RED-on-selective-pull bug is closed
    (DEC-MAPPING-INTEGRITY-ATTEMPTED-001).
    """
    for year in _coverage_years():
        store.upsert_coverage(_cov("RELIANCE", year))
    client = TestClient(build_app(DashboardState(), instrument_store=store))

    body = client.get("/api/v1/universe/mapping-integrity").json()

    assert body["is_green"] is True
    assert body["total_eligible"] == 2 and body["fully_covered"] == 1
    assert body["gating_failures"] == 0
    assert body["not_attempted"] == 1
    [under] = body["under_pulled"]
    assert under["symbol"] == "M&M" and under["reason"] == "not_attempted"


def test_mapping_integrity_green_when_all_eligible_covered(store: InstrumentStore) -> None:
    """Both eligible spots fully covered → green, no under-pulled rows."""
    for underlying in ("RELIANCE", "M&M"):
        for year in _coverage_years():
            store.upsert_coverage(_cov(underlying, year))
    client = TestClient(build_app(DashboardState(), instrument_store=store))

    body = client.get("/api/v1/universe/mapping-integrity").json()

    assert body["is_green"] is True
    assert body["total_eligible"] == 2 and body["fully_covered"] == 2
    assert body["not_attempted"] == 0
    assert body["under_pulled"] == []


def test_mapping_integrity_503_when_store_unwired() -> None:
    """No store wired → 503 (consistent with the other universe/coverage routes)."""
    client = TestClient(build_app(DashboardState(), instrument_store=None))
    assert client.get("/api/v1/universe/mapping-integrity").status_code == 503


# ── DEC-MAPPING-INTEGRITY-FORM-KEY-001: the key_forms keyspace crossing ───────
# The coverage WRITER keys every cell on the PRIMARY F&O underlying form
# (metadata_pull._resolve_underlying_forms → primary_fno_underlying — "BAJAJ-AUTO"
# rows land under "BAJAJAUTO"; writer-truth independently pinned by
# tests/unit/test_metadata_pull.py::test_hyphenated_stock_resolves_to_fno_underlying),
# while the eligible set is SPOT-spelled — the route must cross the two keyspaces.


@pytest.fixture
def bajaj_store(tmp_path: Path):
    """A store whose ONE eligible spot is the hyphenated ``BAJAJ-AUTO``."""
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_eq("BAJAJ-AUTO")],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield s
    s.close()


def test_hyphenated_underlying_covered_via_primary_form_reads_green(
    bajaj_store: InstrumentStore,
) -> None:
    """Coverage keyed under the WRITER's ``BAJAJAUTO`` reads GREEN for spot ``BAJAJ-AUTO``.

    Pre-fix the exact-key compare missed the primary-form rows entirely, so a fully
    covered hyphenated name read ``not_attempted`` forever.
    """
    for year in _coverage_years():
        bajaj_store.upsert_coverage(_cov("BAJAJAUTO", year))
    client = TestClient(build_app(DashboardState(), instrument_store=bajaj_store))

    body = client.get("/api/v1/universe/mapping-integrity").json()

    assert body["is_green"] is True
    assert body["total_eligible"] == 1 and body["fully_covered"] == 1
    assert body["not_attempted"] == 0
    assert body["under_pulled"] == []


def test_hyphenated_underlying_attempted_zero_is_gating_red(
    bajaj_store: InstrumentStore,
) -> None:
    """THE headline regression: a ``BAJAJAUTO`` attempted-and-ZERO row gates RED.

    Pre-fix ``is_green`` stayed True — the silent-vanish alarm was structurally DEAD
    for every hyphenated/aliased underlying because the zero-expiry row sat under the
    writer's primary form while the compare probed the spot spelling.
    """
    bajaj_store.upsert_coverage(_cov("BAJAJAUTO", _coverage_years()[0], expiries=0))
    client = TestClient(build_app(DashboardState(), instrument_store=bajaj_store))

    body = client.get("/api/v1/universe/mapping-integrity").json()

    assert body["is_green"] is False
    assert body["gating_failures"] == 1
    assert body["not_attempted"] == 0
    [under] = body["under_pulled"]
    assert under["symbol"] == "BAJAJ-AUTO"  # display stays the SPOT spelling
    assert under["reason"] == "none"
    # The CSV download serialises the SAME verdict (the shared builder) — RED.
    csv_text = client.get("/api/v1/universe/mapping-integrity.csv").text
    assert "VERDICT" in csv_text
    assert "RED" in csv_text.split("VERDICT", 1)[1].splitlines()[1]


def test_partial_years_union_across_key_eras(bajaj_store: InstrumentStore) -> None:
    """Coverage split across the legacy RAW key era + the primary era unions into ONE partial row.

    2021 under legacy raw ``BAJAJ-AUTO`` + 2024 under ``BAJAJAUTO`` — the reconciler
    must union BOTH eras (DEC-PULL-FORENSIC-WAVE2-001: legacy raw-key coverage rows
    stay visible), never a primary-only read of the 2024 era alone.
    """
    bajaj_store.upsert_coverage(_cov("BAJAJ-AUTO", 2021))
    bajaj_store.upsert_coverage(_cov("BAJAJAUTO", 2024))
    client = TestClient(build_app(DashboardState(), instrument_store=bajaj_store))

    body = client.get("/api/v1/universe/mapping-integrity").json()

    assert body["is_green"] is True  # partial never gates
    [under] = body["under_pulled"]
    assert under["symbol"] == "BAJAJ-AUTO"
    assert under["reason"] == "partial"
    assert under["covered_years"] == [2021, 2024]  # the cross-era union
    assert 2021 not in under["missing_years"] and 2024 not in under["missing_years"]


def test_hyphenated_writer_roundtrip_reads_green(
    bajaj_store: InstrumentStore, tmp_path: Path
) -> None:
    """End-to-end: the REAL coverage writer → the route reads GREEN (the belt).

    Drives the REAL ``pull_expiries_contracts`` (the SAME fake-SDK harness as the
    writer pin ``tests/unit/test_metadata_pull.py::
    test_hyphenated_stock_resolves_to_fno_underlying``) over a ``BAJAJ-AUTO``
    instrument into the REAL route store, then asserts the route crosses the
    keyspace — the #1631 fixture-blindspot closer for this panel.
    """
    from datetime import date as date_cls

    from brutex.dashboard.metadata_pull import pull_expiries_contracts
    from brutex.data.registry_store import RegistryStore

    from tests.unit.test_metadata_pull import _factory, _FakeSDK, _sync_run_parallel

    years = _coverage_years()
    sdk = _FakeSDK(
        expiries={("NSE", "BAJAJAUTO", y): [f"{y}-06-25"] for y in years},
        contracts={
            ("NSE", "BAJAJAUTO", f"{y}-06-25"): [f"NSE-BAJAJAUTO-25Jun{y % 100:02d}-FUT"]
            for y in years
        },
    )
    registry = RegistryStore.open(tmp_path / "registry.duckdb")
    try:
        report = pull_expiries_contracts(
            [_eq("BAJAJ-AUTO")],
            registry=registry,
            coverage_store=bajaj_store,
            years=years,
            dry_run=False,
            run_parallel=_sync_run_parallel,
            client_factory=_factory(sdk),
            now=datetime.now(UTC),
            today=date_cls(years[-1], 1, 2),
            run_id="rt-1",
        )
    finally:
        registry.close()
    # The writer keyed every coverage cell on the PRIMARY form (the unit writer pin).
    assert report.underlyings == ("BAJAJAUTO",)
    assert {r.underlying_symbol for r in bajaj_store.list_coverage()} == {"BAJAJAUTO"}

    client = TestClient(build_app(DashboardState(), instrument_store=bajaj_store))
    body = client.get("/api/v1/universe/mapping-integrity").json()
    assert body["is_green"] is True
    assert body["fully_covered"] == 1
    assert body["under_pulled"] == []


def test_blank_symbol_row_never_poisons_trust_panel(tmp_path: Path) -> None:
    """REFUTER ROUND 1 port of ``attack1_metadata_blank.py`` — the panel stays honest.

    Pre-fix, a blank-symbol EQ row surviving store admission + ONE real metadata pull
    wrote durable ``failed`` coverage rows keyed ``('NSE','')`` (per-YEAR-cell
    ``forms[0]`` IndexErrors captured by the pool), which the mapping-integrity panel
    matched as an attempted-and-ZERO gating failure: ``is_green=False`` with an
    unnameable empty-string symbol, ``Status,RED`` on the CSV, and ``expiry_failures``
    re-failing every pull — falsifying the #1760a sub-(c) "inert key" disclosure
    (and, per refuter-B OBS-1, failing ``metadata_phase_ok`` so the full pipeline
    stopped after metadata). Post-fix the
    metadata grid SKIPS the blank row (one aggregate WARN): no coverage rows keyed
    ``''``, zero failures, and the panel verdict rides ONLY the real rows.
    """
    import structlog.testing
    from brutex.dashboard.metadata_pull import (
        METADATA_BLANK_SYMBOL_SKIPPED_EVENT,
        pull_expiries_contracts,
    )
    from brutex.data.registry_store import RegistryStore

    from tests.unit.test_metadata_pull import _factory, _FakeSDK, _sync_run_parallel

    years = _coverage_years()
    store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    try:
        store.upsert_many(
            [_eq("RELIANCE"), _eq("")],
            snapshot_date=date(2024, 1, 2),
            run_id="r-blank",
            csv_source_sha256="0" * 64,
        )
        sdk = _FakeSDK(
            expiries={("NSE", "RELIANCE", y): [f"{y}-06-25"] for y in years},
            contracts={
                ("NSE", "RELIANCE", f"{y}-06-25"): [f"NSE-RELIANCE-25Jun{y % 100:02d}-FUT"]
                for y in years
            },
        )
        registry = RegistryStore.open(tmp_path / "registry.duckdb")
        try:
            with structlog.testing.capture_logs() as logs:
                report = pull_expiries_contracts(
                    [_eq("RELIANCE"), _eq("")],
                    registry=registry,
                    coverage_store=store,
                    years=years,
                    dry_run=False,
                    run_parallel=_sync_run_parallel,
                    client_factory=_factory(sdk),
                    now=datetime.now(UTC),
                    today=date(years[-1], 1, 2),
                    run_id="rt-blank",
                )
        finally:
            registry.close()
        # The writer never minted a blank cell: zero failures, zero '' rows, one WARN.
        assert report.expiry_failures == 0
        assert [r for r in store.list_coverage() if r.underlying_symbol == ""] == []
        assert len([e for e in logs if e["event"] == METADATA_BLANK_SYMBOL_SKIPPED_EVENT]) == 1

        client = TestClient(build_app(DashboardState(), instrument_store=store))
        body = client.get("/api/v1/universe/mapping-integrity").json()
        # The blank eligible row degrades to the NEUTRAL not_attempted (the R2
        # `or (symbol,)` exact-key probe matches nothing) — never a gating "none".
        assert body["is_green"] is True
        assert body["gating_failures"] == 0
        blank_rows = [u for u in body["under_pulled"] if u["symbol"].strip() == ""]
        assert all(u["reason"] == "not_attempted" for u in blank_rows)
        csv_text = client.get("/api/v1/universe/mapping-integrity.csv").text
        assert "GREEN" in csv_text.split("VERDICT", 1)[1].splitlines()[1]
    finally:
        store.close()
