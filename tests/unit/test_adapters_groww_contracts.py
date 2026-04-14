"""Unit tests for brutex.adapters.groww.contracts — Phase C runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from brutex.adapters.groww.client import GrowwAdapter
from brutex.adapters.groww.contracts import (
    ContractsResult,
    ContractsScopeError,
    PhaseCDriver,
    _content_hash,
)
from brutex.orchestrator.audit import AuditWriter
from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket
from brutex.orchestrator.state import IngestPhase
from brutex.orchestrator.store import WorkItemStore
from tests.unit.test_adapters_groww_client import FakeGrowwClient


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
    client.contracts_response = {
        "contracts": [
            "NSE-NIFTY-02Jan25-28500-PE",
            "NSE-NIFTY-02Jan25-24000-PE",
            "NSE-NIFTY-02Jan25-28150-CE",
        ]
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
        run_id="test-run-phase-c",
    )


EXAMPLE_EXPIRIES = {
    ("NSE", "NIFTY"): ["2025-01-02", "2025-01-09"],
    ("BSE", "SENSEX"): ["2025-09-25"],
}


# ────────────────────────────────────────────────────────────────────────
# Content hash
# ────────────────────────────────────────────────────────────────────────


class TestContentHash:
    def test_deterministic(self) -> None:
        h1 = _content_hash("NSE", "NIFTY", "2025-01-02")
        h2 = _content_hash("NSE", "NIFTY", "2025-01-02")
        assert h1 == h2

    def test_different_expiries_different_hashes(self) -> None:
        assert _content_hash("NSE", "NIFTY", "2025-01-02") != _content_hash(
            "NSE", "NIFTY", "2025-01-09"
        )


# ────────────────────────────────────────────────────────────────────────
# enumerate_items
# ────────────────────────────────────────────────────────────────────────


class TestEnumerateItems:
    def test_one_item_per_underlying_expiry_pair(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        driver = PhaseCDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            expiries=EXAMPLE_EXPIRIES,
        )
        items = list(driver.enumerate_items())
        # NIFTY × 2 expiries + SENSEX × 1 expiry = 3 items
        assert len(items) == 3
        for item in items:
            assert item.phase == IngestPhase.C_CONTRACTS

    def test_empty_expiries_input(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        driver = PhaseCDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            expiries={},
        )
        assert list(driver.enumerate_items()) == []


# ────────────────────────────────────────────────────────────────────────
# run() — happy path
# ────────────────────────────────────────────────────────────────────────


class TestRunHappyPath:
    def test_collects_contracts(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        driver = PhaseCDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            expiries=EXAMPLE_EXPIRIES,
        )
        phase_result = driver.run()
        assert phase_result.total == 3
        assert phase_result.success == 3

        collected = driver.collected_contracts
        # 3 (exchange, underlying, expiry) keys
        assert len(collected) == 3
        # Each gets the same 3 contracts from the fake
        for key, contracts in collected.items():
            assert len(contracts) == 3

    def test_last_result(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        driver = PhaseCDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            expiries=EXAMPLE_EXPIRIES,
        )
        driver.run()
        result = driver.last_result
        assert isinstance(result, ContractsResult)
        assert result.total_work_items == 3
        assert result.total_contracts == 9  # 3 expiries × 3 contracts
        assert result.underlyings_enumerated == 2

    def test_iter_all_contracts(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        driver = PhaseCDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            expiries=EXAMPLE_EXPIRIES,
        )
        driver.run()
        all_contracts = list(driver.iter_all_contracts())
        assert len(all_contracts) == 9  # 3 expiries × 3 contracts
        for exchange, underlying, expiry, symbol in all_contracts:
            assert exchange in ("NSE", "BSE")
            assert symbol.startswith(f"{exchange}-")


# ────────────────────────────────────────────────────────────────────────
# Empty + scope error paths
# ────────────────────────────────────────────────────────────────────────


class TestEmptyResponses:
    def test_all_empty_is_no_data(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
        fake_client: FakeGrowwClient,
    ) -> None:
        """Empty contracts list from the fake → NO_DATA terminal."""
        fake_client.contracts_response = {"contracts": []}
        driver = PhaseCDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            expiries=EXAMPLE_EXPIRIES,
        )
        phase_result = driver.run()
        assert phase_result.no_data == 3
        assert phase_result.success == 0
