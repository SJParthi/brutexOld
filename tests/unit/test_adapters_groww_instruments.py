"""Unit tests for brutex.adapters.groww.instruments — Phase A runner.

Uses the FakeGrowwClient from test_adapters_groww_client to drive
the full pipeline: fetch → normalise → filter → persist.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from brutex.adapters.groww.client import GrowwAdapter
from brutex.adapters.groww.instruments import (
    InstrumentsResult,
    InstrumentsScopeError,
    PhaseADriver,
)
from brutex.orchestrator.audit import AuditWriter
from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket
from brutex.orchestrator.state import IngestPhase
from brutex.orchestrator.store import WorkItemStore

from tests.fakes.groww import FakeDataFrame, FakeGrowwClient

# ────────────────────────────────────────────────────────────────────────
# Canonical row shapes
# ────────────────────────────────────────────────────────────────────────


def _nifty_spot_row() -> dict:
    return {
        "exchange": "NSE",
        "exchange_token": 26000,
        "trading_symbol": "NIFTY",
        "groww_symbol": "NSE-NIFTY",
        "name": "Nifty 50",
        "instrument_type": "IDX",
        "segment": "CASH",
        "series": "",
        "isin": "",
        "underlying_symbol": "",
        "underlying_exchange_token": float("nan"),
        "lot_size": 1,
        "expiry_date": "",
        "strike_price": float("nan"),
        "tick_size": 0.05,
        "freeze_quantity": float("nan"),
        "is_reserved": 0,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "NIFTY",
    }


def _banknifty_pe_row() -> dict:
    return {
        "exchange": "NSE",
        "exchange_token": 49445,
        "trading_symbol": "BANKNIFTY25DEC27000PE",
        "groww_symbol": "NSE-BANKNIFTY-24Dec25-27000-PE",
        "name": float("nan"),
        "instrument_type": "PE",
        "segment": "FNO",
        "series": float("nan"),
        "isin": float("nan"),
        "underlying_symbol": "BANKNIFTY",
        "underlying_exchange_token": 26009,
        "lot_size": 35,
        "expiry_date": "2025-12-24",
        "strike_price": 27000,
        "tick_size": 0.05,
        "freeze_quantity": 601,
        "is_reserved": 1,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "BANKNIFTY25DEC27000PE",
    }


def _reliance_equity_row() -> dict:
    """Out-of-scope — plain cash equity, should be filtered out."""
    return {
        "exchange": "NSE",
        "exchange_token": 2885,
        "trading_symbol": "RELIANCE",
        "groww_symbol": "NSE-RELIANCE",
        "name": "Reliance Industries",
        "instrument_type": "EQ",
        "segment": "CASH",
        "series": "EQ",
        "isin": "INE002A01018",
        "underlying_symbol": float("nan"),
        "underlying_exchange_token": float("nan"),
        "lot_size": 1,
        "expiry_date": float("nan"),
        "strike_price": float("nan"),
        "tick_size": 5,
        "freeze_quantity": float("nan"),
        "is_reserved": 0,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "RELIANCE",
    }


def _reliance_stock_option_row() -> dict:
    """In-scope (v1.0.2 expansion) — stock F&O option."""
    return {
        "exchange": "NSE",
        "exchange_token": 12345,
        "trading_symbol": "RELIANCE25DEC3000CE",
        "groww_symbol": "NSE-RELIANCE-25Dec25-3000-CE",
        "name": float("nan"),
        "instrument_type": "CE",
        "segment": "FNO",
        "series": float("nan"),
        "isin": float("nan"),
        "underlying_symbol": "RELIANCE",
        "underlying_exchange_token": 2885,
        "lot_size": 250,
        "expiry_date": "2025-12-25",
        "strike_price": 3000,
        "tick_size": 0.05,
        "freeze_quantity": 5000,
        "is_reserved": 0,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "RELIANCE25DEC3000CE",
    }


def _sensex_ce_row() -> dict:
    return {
        "exchange": "BSE",
        "exchange_token": 86000,
        "trading_symbol": "SENSEX25SEP79500CE",
        "groww_symbol": "BSE-SENSEX-25Sep25-79500-CE",
        "name": float("nan"),
        "instrument_type": "CE",
        "segment": "FNO",
        "series": float("nan"),
        "isin": float("nan"),
        "underlying_symbol": "SENSEX",
        "underlying_exchange_token": 1,
        "lot_size": 20,
        "expiry_date": "2025-09-25",
        "strike_price": 79500,
        "tick_size": 0.05,
        "freeze_quantity": 1000,
        "is_reserved": 0,
        "buy_allowed": 1,
        "sell_allowed": 1,
        "internal_trading_symbol": "SENSEX25SEP79500CE",
    }


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
    return FakeGrowwClient()


@pytest.fixture
def bucket() -> AdaptiveTokenBucket:
    # Fast + non-restrictive for tests
    return AdaptiveTokenBucket(
        initial_rps=1000.0,
        downshift_rps=500.0,
        burst=100,
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
        run_id="test-run-phase-a",
    )


@pytest.fixture
def driver(adapter: GrowwAdapter, store: WorkItemStore, audit: AuditWriter) -> PhaseADriver:
    return PhaseADriver(
        adapter=adapter,
        store=store,
        audit=audit,
        run_id="test-run-phase-a",
        snapshot_date=date(2026, 4, 14),
    )


# ────────────────────────────────────────────────────────────────────────
# enumerate_items
# ────────────────────────────────────────────────────────────────────────


class TestEnumerateItems:
    def test_one_item_per_snapshot(self, driver: PhaseADriver) -> None:
        items = list(driver.enumerate_items())
        assert len(items) == 1
        assert items[0].phase == IngestPhase.A_INSTRUMENTS

    def test_hash_is_deterministic(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        d1 = PhaseADriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            snapshot_date=date(2026, 4, 14),
        )
        d2 = PhaseADriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            snapshot_date=date(2026, 4, 14),
        )
        h1 = next(iter(d1.enumerate_items())).content_hash
        h2 = next(iter(d2.enumerate_items())).content_hash
        assert h1 == h2


# ────────────────────────────────────────────────────────────────────────
# run() — happy paths
# ────────────────────────────────────────────────────────────────────────


class TestRunHappyPath:
    def test_mixed_rows(
        self,
        driver: PhaseADriver,
        fake_client: FakeGrowwClient,
    ) -> None:
        fake_client.instruments_response = FakeDataFrame(
            [
                _nifty_spot_row(),
                _banknifty_pe_row(),
                _reliance_equity_row(),  # out of scope
                _reliance_stock_option_row(),
                _sensex_ce_row(),
            ]
        )
        phase_result = driver.run()
        assert phase_result.success == 1  # single work item (the snapshot)
        result = driver.last_result
        assert isinstance(result, InstrumentsResult)
        assert result.total_raw_rows == 5
        assert result.integrity_failures == 0
        # 4 in scope (NIFTY spot + BANKNIFTY PE + RELIANCE CE + SENSEX CE)
        assert result.in_scope_count == 4
        assert result.out_of_scope_count == 1  # RELIANCE equity
        assert result.index_count == 1  # NIFTY spot
        assert result.options_count == 3  # BANKNIFTY + RELIANCE + SENSEX
        assert result.futures_count == 0
        assert result.equity_count == 0  # no EQ in scope
        assert result.nse_count == 3  # NIFTY + BANKNIFTY PE + RELIANCE CE
        assert result.bse_count == 1  # SENSEX CE

    def test_last_instruments_accessor(
        self, driver: PhaseADriver, fake_client: FakeGrowwClient
    ) -> None:
        fake_client.instruments_response = FakeDataFrame([_nifty_spot_row()])
        driver.run()
        instruments = driver.last_instruments
        assert len(instruments) == 1
        assert instruments[0].trading_symbol == "NIFTY"


# ────────────────────────────────────────────────────────────────────────
# Integrity failures
# ────────────────────────────────────────────────────────────────────────


class TestIntegrityFailures:
    def test_bad_rows_counted_separately(
        self, driver: PhaseADriver, fake_client: FakeGrowwClient
    ) -> None:
        bad_row = _banknifty_pe_row()
        bad_row["exchange"] = "MCX"  # unsupported
        fake_client.instruments_response = FakeDataFrame(
            [_nifty_spot_row(), bad_row, _sensex_ce_row()]
        )
        driver.run()
        result = driver.last_result
        assert result is not None
        assert result.total_raw_rows == 3
        assert result.integrity_failures == 1
        # 2 in-scope after filtering: NIFTY + SENSEX CE
        assert result.in_scope_count == 2

    def test_all_bad_rows_raises_scope_error(
        self, driver: PhaseADriver, fake_client: FakeGrowwClient
    ) -> None:
        """Every row fails normalise AND no in-scope → scope error."""
        bad_row = _banknifty_pe_row()
        bad_row["exchange"] = "MCX"
        fake_client.instruments_response = FakeDataFrame([bad_row])
        with pytest.raises(InstrumentsScopeError):
            driver.run()


# ────────────────────────────────────────────────────────────────────────
# Empty CSV path
# ────────────────────────────────────────────────────────────────────────


class TestEmptyCsv:
    def test_empty_csv_no_halt(self, driver: PhaseADriver, fake_client: FakeGrowwClient) -> None:
        """Empty response is OK_EMPTY → NO_DATA terminal, no halt."""
        fake_client.instruments_response = FakeDataFrame([])
        phase_result = driver.run()
        assert phase_result.no_data == 1
        assert phase_result.quarantined == 0
        # No InstrumentsScopeError since total_raw_rows == 0
        result = driver.last_result
        assert result is not None
        assert result.total_raw_rows == 0
        assert result.in_scope_count == 0


# ────────────────────────────────────────────────────────────────────────
# Scope error path — CSV has rows but zero in-scope
# ────────────────────────────────────────────────────────────────────────


class TestScopeError:
    def test_all_out_of_scope_raises(
        self, driver: PhaseADriver, fake_client: FakeGrowwClient
    ) -> None:
        """If every row is out of scope (e.g., upstream rename), halt."""
        fake_client.instruments_response = FakeDataFrame(
            [_reliance_equity_row()]  # only out-of-scope row
        )
        with pytest.raises(InstrumentsScopeError, match="ZERO matched"):
            driver.run()
