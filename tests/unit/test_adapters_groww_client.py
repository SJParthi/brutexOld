"""Unit tests for brutex.adapters.groww.client.

Uses a FakeGrowwClient implementing GrowwClientProtocol so the tests
run without the real growwapi package installed. Every test exercises
the full pipeline: dedup probe → rate-limit acquire → dispatch → SDK
call → classify → transition → persist → audit.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brutex.adapters.groww.client import (
    GrowwAdapter,
    GrowwClientProtocol,
)
from brutex.orchestrator.audit import AuditWriter, verify_chain
from brutex.orchestrator.rate_limiter import (
    AdaptiveTokenBucket,
    RateLimiterEvent,
)
from brutex.orchestrator.state import (
    IngestPhase,
    QuarantineReason,
    WorkItem,
    WorkItemState,
)
from brutex.orchestrator.store import WorkItemStore
from tests.fakes.groww import (
    FakeDataFrame,
    FakeGrowwClient,
    GrowwAPIAuthenticationException,
    GrowwAPIAuthorisationException,
    GrowwAPIBadRequestException,
    GrowwAPIException,
    GrowwAPINotFoundException,
    GrowwAPIRateLimitException,
    GrowwAPITimeoutException,
    GrowwBaseException,
)


# ────────────────────────────────────────────────────────────────────────
# Fake clock for deterministic rate limiter
# ────────────────────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self) -> None:
        self._t = 1000.0

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        self._t += seconds


# ────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_client() -> FakeGrowwClient:
    return FakeGrowwClient()


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def bucket(fake_clock: FakeClock) -> AdaptiveTokenBucket:
    return AdaptiveTokenBucket(
        initial_rps=100.0,  # Fast for tests
        downshift_rps=50.0,
        downshift_trigger=3,
        burst=10,
        clock=fake_clock.now,
        sleep=fake_clock.sleep,
    )


@pytest.fixture
def store(tmp_path: Path):
    path = tmp_path / "store.lmdb"
    with WorkItemStore.open(path) as s:
        yield s


@pytest.fixture
def audit(tmp_path: Path):
    path = tmp_path / "audit" / "run.jsonl"
    with AuditWriter.open(path) as a:
        yield a


@pytest.fixture
def audit_path(audit: AuditWriter, tmp_path: Path) -> Path:
    return tmp_path / "audit" / "run.jsonl"


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
        run_id="test-run-abc123",
    )


def _make_work_item(
    method: str, phase: IngestPhase = IngestPhase.A_INSTRUMENTS
) -> WorkItem:
    """Construct a fresh PENDING work item with a deterministic hash."""
    content_hash = hashlib.sha256(method.encode()).hexdigest()
    return WorkItem.new(
        phase=phase,
        content_hash=content_hash,
        params={"method": method},
    )


# ────────────────────────────────────────────────────────────────────────
# Protocol check
# ────────────────────────────────────────────────────────────────────────


class TestProtocol:
    def test_fake_client_satisfies_protocol(
        self, fake_client: FakeGrowwClient
    ) -> None:
        """@runtime_checkable lets us check with isinstance."""
        assert isinstance(fake_client, GrowwClientProtocol)


# ────────────────────────────────────────────────────────────────────────
# Success path — pull_all_instruments
# ────────────────────────────────────────────────────────────────────────


class TestPullAllInstrumentsSuccess:
    def test_happy_path(
        self, adapter: GrowwAdapter, fake_client: FakeGrowwClient
    ) -> None:
        item = _make_work_item("get_all_instruments")
        updated, response = adapter.pull_all_instruments(work_item=item)

        assert updated.state == WorkItemState.SUCCESS
        assert fake_client.get_all_instruments_calls == 1
        assert response is not None
        assert len(response) == 1

    def test_empty_dataframe_is_no_data(
        self, adapter: GrowwAdapter, fake_client: FakeGrowwClient
    ) -> None:
        fake_client.instruments_response = FakeDataFrame([])
        item = _make_work_item("get_all_instruments")
        updated, _ = adapter.pull_all_instruments(work_item=item)
        assert updated.state == WorkItemState.NO_DATA

    def test_persists_to_store(
        self, adapter: GrowwAdapter, store: WorkItemStore
    ) -> None:
        item = _make_work_item("get_all_instruments")
        updated, _ = adapter.pull_all_instruments(work_item=item)
        fetched = store.get(item.phase, item.content_hash)
        assert fetched is not None
        assert fetched.state == WorkItemState.SUCCESS


# ────────────────────────────────────────────────────────────────────────
# Success path — pull_expiries / pull_contracts / pull_historical_candles
# ────────────────────────────────────────────────────────────────────────


class TestOtherPullMethods:
    def test_pull_expiries_happy(
        self, adapter: GrowwAdapter, fake_client: FakeGrowwClient
    ) -> None:
        item = _make_work_item("get_expiries", IngestPhase.B_EXPIRIES)
        updated, response = adapter.pull_expiries(
            work_item=item,
            exchange="NSE",
            underlying_symbol="NIFTY",
            year=2024,
        )
        assert updated.state == WorkItemState.SUCCESS
        assert fake_client.get_expiries_calls == 1
        assert response == {"expiries": ["2024-01-25"]}

    def test_pull_expiries_empty(
        self, adapter: GrowwAdapter, fake_client: FakeGrowwClient
    ) -> None:
        fake_client.expiries_response = {"expiries": []}
        item = _make_work_item("get_expiries", IngestPhase.B_EXPIRIES)
        updated, _ = adapter.pull_expiries(
            work_item=item,
            exchange="NSE",
            underlying_symbol="NIFTY",
            year=2020,
        )
        assert updated.state == WorkItemState.NO_DATA

    def test_pull_contracts_happy(
        self, adapter: GrowwAdapter, fake_client: FakeGrowwClient
    ) -> None:
        item = _make_work_item("get_contracts", IngestPhase.C_CONTRACTS)
        updated, response = adapter.pull_contracts(
            work_item=item,
            exchange="NSE",
            underlying_symbol="NIFTY",
            expiry_date="2024-01-25",
        )
        assert updated.state == WorkItemState.SUCCESS
        assert response is not None
        assert len(response["contracts"]) == 1

    def test_pull_historical_candles_happy(
        self, adapter: GrowwAdapter, fake_client: FakeGrowwClient
    ) -> None:
        item = _make_work_item("get_historical_candles", IngestPhase.D_CANDLES)
        updated, response = adapter.pull_historical_candles(
            work_item=item,
            exchange="NSE",
            segment="FNO",
            groww_symbol="NSE-NIFTY-25Jan24-22000-CE",
            start_time="2024-01-25 09:15:00",
            end_time="2024-01-25 15:30:00",
            candle_interval="1minute",
        )
        assert updated.state == WorkItemState.SUCCESS
        assert response is not None
        assert len(response["candles"]) == 1


# ────────────────────────────────────────────────────────────────────────
# Exception path — every Groww exception type
# ────────────────────────────────────────────────────────────────────────


class TestExceptionPaths:
    def test_rate_limit_goes_to_retrying(
        self,
        adapter: GrowwAdapter,
        fake_client: FakeGrowwClient,
        bucket: AdaptiveTokenBucket,
    ) -> None:
        fake_client.instruments_exception = GrowwAPIRateLimitException("too fast")
        item = _make_work_item("get_all_instruments")
        updated, response = adapter.pull_all_instruments(work_item=item)
        assert updated.state == WorkItemState.RETRYING
        assert updated.retry_count == 1
        assert response is None

    def test_timeout_goes_to_retrying(
        self, adapter: GrowwAdapter, fake_client: FakeGrowwClient
    ) -> None:
        fake_client.instruments_exception = GrowwAPITimeoutException("slow")
        item = _make_work_item("get_all_instruments")
        updated, _ = adapter.pull_all_instruments(work_item=item)
        assert updated.state == WorkItemState.RETRYING

    def test_generic_api_error_goes_to_retrying(
        self, adapter: GrowwAdapter, fake_client: FakeGrowwClient
    ) -> None:
        fake_client.instruments_exception = GrowwAPIException("unknown")
        item = _make_work_item("get_all_instruments")
        updated, _ = adapter.pull_all_instruments(work_item=item)
        assert updated.state == WorkItemState.RETRYING

    def test_not_found_goes_to_no_data(
        self, adapter: GrowwAdapter, fake_client: FakeGrowwClient
    ) -> None:
        """NOT_FOUND is a recorded terminal, never a skip."""
        fake_client.expiries_exception = GrowwAPINotFoundException("no such sym")
        item = _make_work_item("get_expiries", IngestPhase.B_EXPIRIES)
        updated, _ = adapter.pull_expiries(
            work_item=item,
            exchange="NSE",
            underlying_symbol="NONEXISTENT",
            year=2020,
        )
        assert updated.state == WorkItemState.NO_DATA

    def test_bad_request_quarantines(
        self, adapter: GrowwAdapter, fake_client: FakeGrowwClient
    ) -> None:
        fake_client.instruments_exception = GrowwAPIBadRequestException("bad params")
        item = _make_work_item("get_all_instruments")
        updated, _ = adapter.pull_all_instruments(work_item=item)
        assert updated.state == WorkItemState.QUARANTINED
        assert updated.quarantine_reason == QuarantineReason.BAD_REQUEST

    def test_auth_failed_quarantines(
        self, adapter: GrowwAdapter, fake_client: FakeGrowwClient
    ) -> None:
        fake_client.instruments_exception = GrowwAPIAuthenticationException("bad token")
        item = _make_work_item("get_all_instruments")
        updated, _ = adapter.pull_all_instruments(work_item=item)
        assert updated.state == WorkItemState.QUARANTINED
        assert updated.quarantine_reason == QuarantineReason.AUTH_FAILED

    def test_authz_failed_quarantines(
        self, adapter: GrowwAdapter, fake_client: FakeGrowwClient
    ) -> None:
        fake_client.instruments_exception = GrowwAPIAuthorisationException("missing scope")
        item = _make_work_item("get_all_instruments")
        updated, _ = adapter.pull_all_instruments(work_item=item)
        assert updated.state == WorkItemState.QUARANTINED
        assert updated.quarantine_reason == QuarantineReason.AUTHZ_FAILED


# ────────────────────────────────────────────────────────────────────────
# Dedup behaviour — the O(1) skip
# ────────────────────────────────────────────────────────────────────────


class TestDedup:
    def test_success_cache_skips_second_call(
        self, adapter: GrowwAdapter, fake_client: FakeGrowwClient
    ) -> None:
        item = _make_work_item("get_all_instruments")
        adapter.pull_all_instruments(work_item=item)
        adapter.pull_all_instruments(work_item=item)
        # Second call should NOT hit the fake
        assert fake_client.get_all_instruments_calls == 1

    def test_no_data_cache_also_skips(
        self, adapter: GrowwAdapter, fake_client: FakeGrowwClient
    ) -> None:
        fake_client.instruments_response = FakeDataFrame([])
        item = _make_work_item("get_all_instruments")
        adapter.pull_all_instruments(work_item=item)
        adapter.pull_all_instruments(work_item=item)
        assert fake_client.get_all_instruments_calls == 1

    def test_retrying_cache_is_retried(
        self, adapter: GrowwAdapter, fake_client: FakeGrowwClient
    ) -> None:
        """Non-terminal RETRYING state in cache should NOT dedup —
        the worker loop re-dispatches."""
        fake_client.instruments_exception = GrowwAPITimeoutException("first")
        item = _make_work_item("get_all_instruments")
        adapter.pull_all_instruments(work_item=item)

        # Clear the exception, try again — should call the fake again
        fake_client.instruments_exception = None
        adapter.pull_all_instruments(work_item=item)
        assert fake_client.get_all_instruments_calls == 2

    def test_quarantined_cache_skips(
        self, adapter: GrowwAdapter, fake_client: FakeGrowwClient
    ) -> None:
        """Quarantined items are skipped (phase will halt)."""
        fake_client.instruments_exception = GrowwAPIBadRequestException("bad")
        item = _make_work_item("get_all_instruments")
        adapter.pull_all_instruments(work_item=item)

        # Second call should not re-hit the SDK
        adapter.pull_all_instruments(work_item=item)
        assert fake_client.get_all_instruments_calls == 1


# ────────────────────────────────────────────────────────────────────────
# Audit chain integration
# ────────────────────────────────────────────────────────────────────────


class TestAuditIntegration:
    def test_success_writes_two_audit_entries(
        self, adapter: GrowwAdapter, audit_path: Path
    ) -> None:
        item = _make_work_item("get_all_instruments")
        adapter.pull_all_instruments(work_item=item)

        entry_count = verify_chain(audit_path)
        # 1 entry for GROWW_CALL_START, 1 for GROWW_CALL_SUCCESS
        assert entry_count == 2

    def test_exception_writes_two_audit_entries(
        self,
        adapter: GrowwAdapter,
        fake_client: FakeGrowwClient,
        audit_path: Path,
    ) -> None:
        fake_client.instruments_exception = GrowwAPIRateLimitException("x")
        item = _make_work_item("get_all_instruments")
        adapter.pull_all_instruments(work_item=item)

        entry_count = verify_chain(audit_path)
        # START + RETRY
        assert entry_count == 2

    def test_dedup_writes_one_entry(
        self, adapter: GrowwAdapter, audit_path: Path
    ) -> None:
        item = _make_work_item("get_all_instruments")
        adapter.pull_all_instruments(work_item=item)  # 2 entries
        adapter.pull_all_instruments(work_item=item)  # +1 entry (DEDUP_SKIP)

        entry_count = verify_chain(audit_path)
        assert entry_count == 3

    def test_chain_remains_valid(
        self, adapter: GrowwAdapter, audit_path: Path
    ) -> None:
        """After many calls, the chain stays verifiable."""
        for method in ["get_all_instruments"] * 5:
            item = WorkItem.new(
                phase=IngestPhase.A_INSTRUMENTS,
                content_hash=hashlib.sha256(
                    f"{method}:{datetime.now(UTC).isoformat()}".encode()
                ).hexdigest(),
                params={"method": method},
            )
            adapter.pull_all_instruments(work_item=item)
        verify_chain(audit_path)  # Should not raise


# ────────────────────────────────────────────────────────────────────────
# Rate limit event callback
# ────────────────────────────────────────────────────────────────────────


class TestRateLimitEventCallback:
    def test_on_rate_limit_event_fires(
        self,
        fake_client: FakeGrowwClient,
        bucket: AdaptiveTokenBucket,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        captured: list[RateLimiterEvent] = []
        adapter = GrowwAdapter(
            client=fake_client,
            bucket=bucket,
            store=store,
            audit=audit,
            run_id="test-run",
            on_rate_limit_event=captured.append,
        )

        # Drive a single rate limit hit
        fake_client.instruments_exception = GrowwAPIRateLimitException("slow down")
        for i in range(3):
            item = WorkItem.new(
                phase=IngestPhase.A_INSTRUMENTS,
                content_hash=hashlib.sha256(f"call{i}".encode()).hexdigest(),
                params={"i": str(i)},
            )
            adapter.pull_all_instruments(work_item=item)

        # 3 rate limit hits: 2 with kind="rate_limit_hit", then 1 with "downshift"
        assert len(captured) == 3
        assert captured[0].kind == "rate_limit_hit"
        assert captured[1].kind == "rate_limit_hit"
        assert captured[2].kind == "downshift"


# ────────────────────────────────────────────────────────────────────────
# Never-skip invariant at the adapter level
# ────────────────────────────────────────────────────────────────────────


class TestNeverSkipInvariant:
    """Drive every exception type through the adapter. All should end
    in terminal or retrying — none should return the same PENDING/
    IN_FLIGHT state or vanish."""

    @pytest.mark.parametrize(
        ("exc_class", "expected_state"),
        [
            (GrowwAPIRateLimitException, WorkItemState.RETRYING),
            (GrowwAPITimeoutException, WorkItemState.RETRYING),
            (GrowwAPIException, WorkItemState.RETRYING),
            (GrowwBaseException, WorkItemState.RETRYING),
            (GrowwAPINotFoundException, WorkItemState.NO_DATA),
            (GrowwAPIBadRequestException, WorkItemState.QUARANTINED),
            (GrowwAPIAuthenticationException, WorkItemState.QUARANTINED),
            (GrowwAPIAuthorisationException, WorkItemState.QUARANTINED),
        ],
    )
    def test_every_exception_resolves_to_expected_state(
        self,
        adapter: GrowwAdapter,
        fake_client: FakeGrowwClient,
        exc_class: type[Exception],
        expected_state: WorkItemState,
    ) -> None:
        fake_client.instruments_exception = exc_class("test")
        item = _make_work_item(f"get_all_instruments_{exc_class.__name__}")
        updated, _ = adapter.pull_all_instruments(work_item=item)
        assert updated.state == expected_state


# ────────────────────────────────────────────────────────────────────────
# Unknown exception raises UnknownExceptionError
# ────────────────────────────────────────────────────────────────────────


class TestUnknownExceptionBubblesUp:
    def test_value_error_raises_unknown(
        self, adapter: GrowwAdapter, fake_client: FakeGrowwClient
    ) -> None:
        """Non-Groww exceptions indicate a bug. They must not be
        silently swallowed."""
        from brutex.orchestrator.retry import UnknownExceptionError

        fake_client.instruments_exception = ValueError("unrelated")
        item = _make_work_item("get_all_instruments")
        with pytest.raises(UnknownExceptionError):
            adapter.pull_all_instruments(work_item=item)


# ────────────────────────────────────────────────────────────────────────
# from_token factory — import error path
# ────────────────────────────────────────────────────────────────────────


class TestFromTokenFactory:
    def test_missing_growwapi_raises_helpful_error(
        self,
        bucket: AdaptiveTokenBucket,
        store: WorkItemStore,
        audit: AuditWriter,
    ) -> None:
        """If growwapi isn't installed, from_token raises with a clear msg."""
        import sys

        # Temporarily hide growwapi if it happens to be installed
        saved = sys.modules.get("growwapi")
        sys.modules["growwapi"] = None  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError, match="growwapi"):
                GrowwAdapter.from_token(
                    token="fake-token",
                    bucket=bucket,
                    store=store,
                    audit=audit,
                    run_id="test",
                )
        finally:
            if saved is not None:
                sys.modules["growwapi"] = saved
            else:
                sys.modules.pop("growwapi", None)
