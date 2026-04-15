"""Unit tests for brutex.adapters.groww.candles — Phase D runner + chunker."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from brutex.adapters.groww.candles import (
    MAX_DURATION_DAYS,
    CandleChunk,
    CandleChunkerError,
    CandleRequest,
    CandlesResult,
    PhaseDDriver,
    _content_hash,
    chunk_date_range,
)
from brutex.adapters.groww.client import GrowwAdapter
from brutex.orchestrator.audit import AuditWriter
from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket
from brutex.orchestrator.state import IngestPhase
from brutex.orchestrator.store import WorkItemStore

from tests.fakes.groww import FakeGrowwClient

# ────────────────────────────────────────────────────────────────────────
# chunk_date_range — the pure function
# ────────────────────────────────────────────────────────────────────────


class TestChunkDateRange:
    def test_single_day_yields_one_chunk(self) -> None:
        chunks = chunk_date_range(
            start=date(2024, 1, 1),
            end=date(2024, 1, 1),
            interval="1minute",
        )
        assert len(chunks) == 1
        assert chunks[0].start == date(2024, 1, 1)
        assert chunks[0].end == date(2024, 1, 1)

    def test_range_under_max_yields_one_chunk(self) -> None:
        chunks = chunk_date_range(
            start=date(2024, 1, 1),
            end=date(2024, 1, 15),
            interval="1minute",  # 30-day limit
        )
        assert len(chunks) == 1
        assert chunks[0].start == date(2024, 1, 1)
        assert chunks[0].end == date(2024, 1, 15)

    def test_1minute_splits_30_day_boundaries(self) -> None:
        chunks = chunk_date_range(
            start=date(2024, 1, 1),
            end=date(2024, 3, 31),  # 91 days
            interval="1minute",
        )
        # 30-day chunks → need 4 chunks (30+30+30+1)
        assert len(chunks) >= 3
        # First chunk must start on start date
        assert chunks[0].start == date(2024, 1, 1)
        # Last chunk must end on end date
        assert chunks[-1].end == date(2024, 3, 31)

    def test_15minute_uses_90_day_limit(self) -> None:
        chunks = chunk_date_range(
            start=date(2024, 1, 1),
            end=date(2024, 6, 30),  # ~181 days
            interval="15minute",  # 90 days
        )
        # ~2 chunks
        assert 2 <= len(chunks) <= 3

    def test_1day_uses_180_day_limit(self) -> None:
        chunks = chunk_date_range(
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            interval="1day",  # 180 days
        )
        # ~2 chunks for a full year
        assert 2 <= len(chunks) <= 3

    def test_chunks_are_contiguous(self) -> None:
        """No gaps or overlaps between consecutive chunks."""
        chunks = chunk_date_range(
            start=date(2024, 1, 1),
            end=date(2024, 6, 30),
            interval="1minute",
        )
        for i in range(1, len(chunks)):
            prev_end = chunks[i - 1].end
            this_start = chunks[i].start
            # Next chunk starts exactly one day after prev ends
            assert (this_start - prev_end).days == 1

    def test_unknown_interval_raises(self) -> None:
        with pytest.raises(CandleChunkerError, match="Unknown interval"):
            chunk_date_range(
                start=date(2024, 1, 1),
                end=date(2024, 1, 31),
                interval="2hour",  # not in the table
            )

    def test_end_before_start_raises(self) -> None:
        with pytest.raises(CandleChunkerError, match="must be >="):
            chunk_date_range(
                start=date(2024, 12, 31),
                end=date(2024, 1, 1),
                interval="1day",
            )

    def test_max_duration_table_has_all_12_intervals(self) -> None:
        """Every Groww interval from annexures §4 must be in the table."""
        expected = {
            "1minute",
            "2minute",
            "3minute",
            "5minute",
            "10minute",
            "15minute",
            "30minute",
            "1hour",
            "4hour",
            "1day",
            "1week",
            "1month",
        }
        assert set(MAX_DURATION_DAYS.keys()) == expected


# ────────────────────────────────────────────────────────────────────────
# CandleChunk formatting
# ────────────────────────────────────────────────────────────────────────


class TestCandleChunk:
    def test_start_time_format(self) -> None:
        chunk = CandleChunk(start=date(2024, 1, 1), end=date(2024, 1, 31))
        assert chunk.start_time() == "2024-01-01 09:15:00"

    def test_end_time_format(self) -> None:
        chunk = CandleChunk(start=date(2024, 1, 1), end=date(2024, 1, 31))
        assert chunk.end_time() == "2024-01-31 15:30:00"


# ────────────────────────────────────────────────────────────────────────
# Content hash
# ────────────────────────────────────────────────────────────────────────


class TestContentHash:
    def test_deterministic(self) -> None:
        h1 = _content_hash(
            "NSE", "FNO", "NSE-NIFTY", "1minute", date(2024, 1, 1), date(2024, 1, 31)
        )
        h2 = _content_hash(
            "NSE", "FNO", "NSE-NIFTY", "1minute", date(2024, 1, 1), date(2024, 1, 31)
        )
        assert h1 == h2

    def test_different_chunks_different_hashes(self) -> None:
        h1 = _content_hash(
            "NSE", "FNO", "NSE-NIFTY", "1minute", date(2024, 1, 1), date(2024, 1, 31)
        )
        h2 = _content_hash(
            "NSE", "FNO", "NSE-NIFTY", "1minute", date(2024, 2, 1), date(2024, 2, 29)
        )
        assert h1 != h2


# ────────────────────────────────────────────────────────────────────────
# PhaseDDriver integration
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
    return AdaptiveTokenBucket(initial_rps=1000.0, downshift_rps=500.0, burst=100)


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
        run_id="test-run-phase-d",
    )


def _nifty_request() -> CandleRequest:
    return CandleRequest(
        exchange="NSE",
        segment="FNO",
        groww_symbol="NSE-NIFTY-02Jan25-28500-PE",
        interval="1minute",
        start=date(2024, 1, 1),
        end=date(2024, 1, 10),
    )


class TestEnumerateItems:
    def test_single_chunk_request(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        driver = PhaseDDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            requests=[_nifty_request()],
        )
        items = list(driver.enumerate_items())
        # 10 days < 30-day limit → 1 chunk
        assert len(items) == 1
        for item in items:
            assert item.phase == IngestPhase.D_CANDLES

    def test_multi_chunk_request(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        """A 100-day range with 1m interval → 4 chunks."""
        req = CandleRequest(
            exchange="NSE",
            segment="FNO",
            groww_symbol="NSE-NIFTY",
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 4, 10),  # ~100 days
        )
        driver = PhaseDDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            requests=[req],
        )
        items = list(driver.enumerate_items())
        assert len(items) >= 3

    def test_multiple_requests(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        requests = [_nifty_request(), _nifty_request()]
        # Note: two identical requests will produce duplicate chunks
        # (same content hash) — but since enumerate_items just yields,
        # we still get 2 items. Dedup happens inside the adapter on put.
        driver = PhaseDDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            requests=requests,
        )
        items = list(driver.enumerate_items())
        assert len(items) == 2


class TestRunHappyPath:
    def test_collects_candles(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        driver = PhaseDDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            requests=[_nifty_request()],
        )
        phase_result = driver.run()
        assert phase_result.total == 1
        assert phase_result.success == 1

        collected = driver.collected_candles
        assert "NSE-NIFTY-02Jan25-28500-PE" in collected
        assert len(collected["NSE-NIFTY-02Jan25-28500-PE"]) == 1

    def test_last_result(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        driver = PhaseDDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            requests=[_nifty_request()],
        )
        driver.run()
        result = driver.last_result
        assert isinstance(result, CandlesResult)
        assert result.total_requests == 1
        assert result.total_chunks == 1
        assert result.total_candle_rows == 1
        assert result.contracts_with_data == 1
        assert result.contracts_empty == 0

    def test_multi_chunk_run_aggregates_rows(
        self,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        """2 chunks × 1 row each → 2 rows in collected_candles."""
        req = CandleRequest(
            exchange="NSE",
            segment="FNO",
            groww_symbol="NSE-NIFTY",
            interval="1minute",
            start=date(2024, 1, 1),
            end=date(2024, 2, 15),  # ~46 days → 2 chunks
        )
        driver = PhaseDDriver(
            adapter=adapter,
            store=store,
            audit=audit,
            run_id="r",
            requests=[req],
        )
        driver.run()
        assert len(driver.collected_candles["NSE-NIFTY"]) == 2
