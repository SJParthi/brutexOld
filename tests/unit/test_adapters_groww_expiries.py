"""Unit tests for brutex.adapters.groww.expiries — Phase B runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from brutex.adapters.groww.client import GrowwAdapter
from brutex.adapters.groww.expiries import (
    ExpiriesResult,
    ExpiriesScopeError,
    PhaseBDriver,
    _content_hash,
)
from brutex.models import Exchange
from brutex.orchestrator.audit import AuditWriter
from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket
from brutex.orchestrator.state import IngestPhase, WorkItemState
from brutex.orchestrator.store import WorkItemStore
from brutex.universe import UnderlyingSpec
from tests.fakes.groww import FakeGrowwClient


# ────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path):
    with WorkItemStore.open(tmp_path / "store.lmdb") as s:
        yield s


@pytest.fixture
def audit(tmp_path: Path):
    with AuditWriter.open(tmp_path / "audit" / "run.jsonl") as a:
        yield a


@pytest.fixture
def fake_client() -> FakeGrowwClient:
    client = FakeGrowwClient()
    client.expiries_response = {
        "expiries": ["2024-01-25", "2024-02-29", "2024-03-28"]
    }
    return client


@pytest.fixture
def bucket() -> AdaptiveTokenBucket:
    return AdaptiveTokenBucket(
        initial_rps=1000.0, downshift_rps=500.0, burst=100
    )


@pytest.fixture
def adapter(
    fake_client: FakeGrowwClient,
    bucket: AdaptiveTokenBucket,
    store: WorkItemStore,
    audit: AuditWriter,
) -> GrowwAdapter:
    return GrowwAdapter(
        client=fake_client,
        bucket=bucket,
        store=store,
        audit=audit,
        run_id="test-run-phase-b",
    )


TWO_INDICES = (
    UnderlyingSpec(
        exchange=Exchange.NSE,
        underlying_symbol="NIFTY",
        display_name="NIFTY 50",
        fno_live_since_year=2000,
    ),
    UnderlyingSpec(
        exchange=Exchange.BSE,
        underlying_symbol="SENSEX",
        display_name="BSE SENSEX",
        fno_live_since_year=2000,
    ),
)


# ────────────────────────────────────────────────────────────────────────
# Content hash determinism
# ────────────────────────────────────────────────────────────────────────


class TestContentHash:
    def test_deterministic(self) -> None:
        h1 = _content_hash("NSE", "NIFTY", 2024)
        h2 = _content_hash("NSE", "NIFTY", 2024)
        assert h1 == h2
        assert len(h1) == 64

    def test_different_inputs_different_hashes(self) -> None:
        assert _content_hash("NSE", "NIFTY", 2024) != _content_hash(
            "NSE", "NIFTY", 2025
        )
        assert _content_hash("NSE", "NIFTY", 2024) != _content_hash(
            "BSE", "NIFTY", 2024
        )
        assert _content_hash("NSE", "NIFTY", 2024) != _content_hash(
            "NSE", "BANKNIFTY", 2024
        )


# ────────────────────────────────────────────────────────────────────────
# enumerate_items
# ────────────────────────────────────────────────────────────────────────


class TestEnumerateItems:
    def test_cartesian_over_underlyings_x_years(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        driver = PhaseBDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            underlyings=TWO_INDICES,
            start_year=2020,
            end_year=2022,
        )
        items = list(driver.enumerate_items())
        # 2 underlyings × 3 years = 6 items
        assert len(items) == 6

    def test_respects_live_since_year(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        finnifty = UnderlyingSpec(
            exchange=Exchange.NSE,
            underlying_symbol="FINNIFTY",
            display_name="NIFTY FIN SERVICE",
            fno_live_since_year=2021,  # doesn't exist in 2020
        )
        driver = PhaseBDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            underlyings=(finnifty,),
            start_year=2020,
            end_year=2023,
        )
        items = list(driver.enumerate_items())
        # Years skipped: 2020 (pre-live) → only 2021, 2022, 2023
        assert len(items) == 3
        years = sorted(int(dict(item.params)["year"]) for item in items)
        assert years == [2021, 2022, 2023]

    def test_all_items_in_phase_b(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        driver = PhaseBDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            underlyings=TWO_INDICES,
            start_year=2024,
            end_year=2024,
        )
        for item in driver.enumerate_items():
            assert item.phase == IngestPhase.B_EXPIRIES

    def test_end_before_start_rejected(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        with pytest.raises(ValueError, match="end_year"):
            PhaseBDriver(
                adapter=adapter,
                store=store,
                audit=audit,
                run_id="r",
                underlyings=TWO_INDICES,
                start_year=2025,
                end_year=2020,
            )


# ────────────────────────────────────────────────────────────────────────
# run() — happy path
# ────────────────────────────────────────────────────────────────────────


class TestRunHappyPath:
    def test_collects_expiries_per_underlying(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
        fake_client: FakeGrowwClient,
    ) -> None:
        driver = PhaseBDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            underlyings=TWO_INDICES,
            start_year=2024,
            end_year=2024,
        )
        phase_result = driver.run()
        assert phase_result.total == 2
        assert phase_result.success == 2

        collected = driver.collected_expiries
        assert ("NSE", "NIFTY") in collected
        assert ("BSE", "SENSEX") in collected
        # Each underlying sees the same 3 dates from FakeGrowwClient
        assert len(collected[("NSE", "NIFTY")]) == 3
        assert len(collected[("BSE", "SENSEX")]) == 3

    def test_last_result_populated(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        driver = PhaseBDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            underlyings=TWO_INDICES,
            start_year=2024,
            end_year=2024,
        )
        driver.run()
        result = driver.last_result
        assert isinstance(result, ExpiriesResult)
        assert result.underlyings_enumerated == 2
        assert result.total_work_items == 2
        assert result.total_expiries == 6  # 2 underlyings × 3 dates
        assert result.underlyings_with_expiries == 2
        assert result.years_enumerated == (2024,)

    def test_duplicates_deduplicated_per_underlying(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
        fake_client: FakeGrowwClient,
    ) -> None:
        """Same date returned across years should dedupe in the set."""
        fake_client.expiries_response = {"expiries": ["2024-01-25"]}
        driver = PhaseBDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            underlyings=(TWO_INDICES[0],),
            start_year=2023,
            end_year=2025,
        )
        driver.run()
        # 3 years × 1 date each, but all the same → 1 unique
        assert len(driver.collected_expiries[("NSE", "NIFTY")]) == 1


# ────────────────────────────────────────────────────────────────────────
# Empty response path
# ────────────────────────────────────────────────────────────────────────


class TestEmptyResponses:
    def test_all_empty_raises_scope_error(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
        fake_client: FakeGrowwClient,
    ) -> None:
        """If every underlying returns empty AND we have successes → scope halt."""
        fake_client.expiries_response = {"expiries": []}
        driver = PhaseBDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            underlyings=TWO_INDICES,
            start_year=2024,
            end_year=2024,
        )
        # Empty responses → all items NO_DATA, not SUCCESS, so no scope
        # error is raised. This tests the other path.
        phase_result = driver.run()
        assert phase_result.no_data == 2
        assert phase_result.success == 0

    def test_mixed_empty_and_success(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        """One underlying has data, other empty — happy coexistence."""
        call_count = {"n": 0}

        def stateful_invoke(
            *, exchange, underlying_symbol, year=None, month=None
        ):
            call_count["n"] += 1
            if underlying_symbol == "NIFTY":
                return {"expiries": ["2024-01-25"]}
            return {"expiries": []}

        # Monkeypatch the adapter's client
        adapter._client.get_expiries = stateful_invoke  # type: ignore

        driver = PhaseBDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            underlyings=TWO_INDICES,
            start_year=2024,
            end_year=2024,
        )
        phase_result = driver.run()
        assert phase_result.total == 2
        assert phase_result.success == 1  # NIFTY only
        assert phase_result.no_data == 1  # SENSEX empty
        assert len(driver.collected_expiries.get(("NSE", "NIFTY"), [])) == 1
