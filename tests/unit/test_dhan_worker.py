"""Unit tests for :mod:`brutex.orchestrator.dhan_worker` (DEC-P0-045 / PR-F).

Coverage targets:

* All 11 :class:`Outcome` enum values routed to correct action.
* Bucket feedback (``record_success`` / ``record_rate_limit_hit``)
  fires for the correct outcome sub-set.
* Audit entries (INGEST_CHUNK_DONE / INGEST_CHUNK_RETRY /
  QUARANTINE_NEW) carry the documented payload schema.
* :class:`DailyQuotaExceeded` is caught and converted to a halt.
* :class:`DhanWorkerHaltError` carries non-trivial ``partial_stats``
  reflecting the work done before the halt boundary.
* ``dhan_worker_complete`` structlog event always fires, including on
  halt (try/finally semantics).
* Backoff sleep is called between (not after) attempts.
* ``max_retries`` cap is honoured; retry exhaustion → quarantine.
"""

from __future__ import annotations

import random
from datetime import date
from decimal import Decimal
from typing import cast
from unittest.mock import MagicMock

import pytest
from brutex.adapters.dhan.body import (
    HistoricalDailyRequest,
    IntradayChunkRequest,
)
from brutex.adapters.dhan.client import DhanClient
from brutex.adapters.dhan.constants import (
    BANKNIFTY,
    INDIAVIX,
    NIFTY,
    SENSEX,
)
from brutex.adapters.dhan.parser import OHLCBar
from brutex.adapters.dhan.types import FetchResult
from brutex.orchestrator.audit import AuditWriter
from brutex.orchestrator.dhan_errors import (
    DhanError,
    DhanError800,
    DhanError805,
    DhanError808,
    DhanError813,
    DhanError814,
    DhanErrorDH907,
    DhanErrorDH908,
    DhanErrorDH909,
)
from brutex.orchestrator.dhan_worker import (
    AUDIT_ACTION_CHUNK_DONE,
    AUDIT_ACTION_CHUNK_RETRY,
    AUDIT_ACTION_QUARANTINE_NEW,
    HALT_REASON_DAILY_QUOTA,
    QUARANTINE_REASON_RETRY_EXHAUSTED,
    DhanWorker,
    DhanWorkerHaltError,
    IngestStats,
    _extract_error_code,
    _subject_for,
)
from brutex.orchestrator.quota_tracker import DailyQuotaExceeded
from brutex.orchestrator.rate_limiter import AdaptiveTokenBucket
from brutex.orchestrator.state import MAX_SOFT_RETRIES, Outcome

# ────────────────────────────────────────────────────────────────────────
# Test fixtures + helpers
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def intraday_req() -> IntradayChunkRequest:
    """One NIFTY 50 1-minute chunk for 2024-04-01..04-15."""
    return IntradayChunkRequest(
        instrument=NIFTY,
        interval="1",
        from_date=date(2024, 4, 1),
        to_date=date(2024, 4, 15),
    )


@pytest.fixture
def daily_req() -> HistoricalDailyRequest:
    """One BANKNIFTY daily range for 2021 → 2024."""
    return HistoricalDailyRequest(
        instrument=BANKNIFTY,
        from_date=date(2021, 1, 1),
        to_date=date(2024, 12, 31),
    )


@pytest.fixture
def fake_bar() -> OHLCBar:
    """One synthetic OHLCBar for sink-assertion fixtures."""
    from datetime import UTC, datetime

    return OHLCBar(
        timestamp_utc=datetime(2024, 4, 1, 9, 15, tzinfo=UTC),
        open=Decimal("22500.00"),
        high=Decimal("22505.50"),
        low=Decimal("22498.25"),
        close=Decimal("22502.75"),
        volume=0,
    )


def _make_worker(
    *,
    fetch_returns: list[FetchResult | BaseException],
    max_retries: int = 3,
    sleep_recorder: list[float] | None = None,
) -> tuple[DhanWorker, MagicMock, MagicMock, MagicMock, MagicMock]:
    """Wire mocks and return (worker, client_mock, bucket_mock, audit_mock,
    logger_mock).

    ``fetch_returns`` is a list of FetchResult / exceptions to pop in
    sequence — covers both intraday and historical-daily fetches uniformly.
    """
    client = MagicMock(spec=DhanClient)
    client.fetch_intraday.side_effect = list(fetch_returns)
    client.fetch_historical_daily.side_effect = list(fetch_returns)

    bucket = MagicMock(spec=AdaptiveTokenBucket)
    bucket.record_rate_limit_hit.return_value = None

    audit = MagicMock(spec=AuditWriter)
    logger = MagicMock()

    def _capture_sleep(delay: float) -> None:
        if sleep_recorder is not None:
            sleep_recorder.append(delay)

    worker = DhanWorker(
        client=cast(DhanClient, client),
        bucket=cast(AdaptiveTokenBucket, bucket),
        audit=cast(AuditWriter, audit),
        logger=logger,
        sleep=_capture_sleep,
        rng=random.Random(0),
        max_retries=max_retries,
    )
    return worker, client, bucket, audit, logger


def _ok_with_data(
    req: IntradayChunkRequest | HistoricalDailyRequest,
    bars: tuple[OHLCBar, ...],
) -> FetchResult:
    return FetchResult(request=req, outcome=Outcome.OK_WITH_DATA, bars=bars, error=None)


def _result(
    req: IntradayChunkRequest | HistoricalDailyRequest,
    outcome: Outcome,
    error: DhanError | None = None,
) -> FetchResult:
    return FetchResult(request=req, outcome=outcome, bars=(), error=error)


# ────────────────────────────────────────────────────────────────────────
# IngestStats + DhanWorkerHaltError dataclass behaviour
# ────────────────────────────────────────────────────────────────────────


class TestIngestStats:
    def test_defaults_are_zero(self) -> None:
        stats = IngestStats()
        assert stats.ok_with_data == 0
        assert stats.ok_empty == 0
        assert stats.no_data == 0
        assert stats.quarantined == 0
        assert stats.retries_total == 0
        assert stats.bars_emitted == 0
        assert stats.halt_reason is None

    def test_chunks_total_sums_correctly(self) -> None:
        stats = IngestStats(ok_with_data=10, ok_empty=2, no_data=1, quarantined=3)
        # retries_total does NOT count as a chunk; bars_emitted is bar-count.
        assert stats.chunks_total() == 16

    def test_is_frozen(self) -> None:
        stats = IngestStats(ok_with_data=5)
        with pytest.raises((AttributeError, TypeError)):
            stats.ok_with_data = 999  # type: ignore[misc]


class TestDhanWorkerHaltError:
    def test_carries_reason_and_partial_stats(self) -> None:
        partial = IngestStats(ok_with_data=3, quarantined=1, halt_reason="AUTH_FAILED")
        err = DhanWorkerHaltError(reason="AUTH_FAILED", partial_stats=partial)
        assert err.reason == "AUTH_FAILED"
        assert err.partial_stats is partial
        assert err.partial_stats.ok_with_data == 3

    def test_safe_message_excludes_internal_detail(self) -> None:
        err = DhanWorkerHaltError(
            reason="daily_quota_exhausted",
            partial_stats=IngestStats(),
        )
        assert str(err) == "DhanWorker halted (daily_quota_exhausted)"


# ────────────────────────────────────────────────────────────────────────
# Subject + error-code helpers
# ────────────────────────────────────────────────────────────────────────


class TestSubjectAndExtractors:
    def test_subject_intraday_format(self, intraday_req: IntradayChunkRequest) -> None:
        assert _subject_for(intraday_req) == f"chunk:{NIFTY.security_id}:1:2024-04-01:2024-04-15"

    def test_subject_daily_format(self, daily_req: HistoricalDailyRequest) -> None:
        assert (
            _subject_for(daily_req) == f"chunk:{BANKNIFTY.security_id}:daily:2021-01-01:2024-12-31"
        )

    def test_extract_error_code_none_input(self) -> None:
        assert _extract_error_code(None) is None

    def test_extract_error_code_dhan_error(self) -> None:
        assert _extract_error_code(DhanError800()) == "800"

    def test_extract_error_code_non_dhan_exception(self) -> None:
        assert _extract_error_code(RuntimeError("plain")) is None


# ────────────────────────────────────────────────────────────────────────
# __repr__ redaction
# ────────────────────────────────────────────────────────────────────────


class TestRepr:
    def test_repr_redacts_token(self) -> None:
        worker, *_ = _make_worker(fetch_returns=[])
        text = repr(worker)
        assert "max_retries=" in text
        assert "token" not in text.lower()


# ────────────────────────────────────────────────────────────────────────
# Outcome routing — happy path
# ────────────────────────────────────────────────────────────────────────


class TestOkWithData:
    def test_emits_bars_records_success_writes_audit(
        self,
        intraday_req: IntradayChunkRequest,
        fake_bar: OHLCBar,
    ) -> None:
        worker, _client, bucket, audit, _logger = _make_worker(
            fetch_returns=[_ok_with_data(intraday_req, (fake_bar, fake_bar))],
        )
        sink_calls: list[tuple[int, Outcome]] = []

        def sink(bars: object, result: FetchResult) -> None:
            bars_seq = cast(tuple[OHLCBar, ...], bars)
            sink_calls.append((len(bars_seq), result.outcome))

        stats = worker.run([intraday_req], sink)

        assert stats.ok_with_data == 1
        assert stats.bars_emitted == 2
        assert sink_calls == [(2, Outcome.OK_WITH_DATA)]
        bucket.record_success.assert_called_once()
        bucket.record_rate_limit_hit.assert_not_called()
        audit.append.assert_called_once()
        kwargs = audit.append.call_args.kwargs
        assert kwargs["action"] == AUDIT_ACTION_CHUNK_DONE
        assert kwargs["payload"]["outcome"] == "OK_WITH_DATA"
        assert kwargs["payload"]["bars"] == 2
        assert kwargs["payload"]["attempts"] == 1


class TestOkEmpty:
    def test_no_bars_to_sink_audit_done(self, intraday_req: IntradayChunkRequest) -> None:
        worker, _client, bucket, audit, _logger = _make_worker(
            fetch_returns=[_result(intraday_req, Outcome.OK_EMPTY)],
        )
        sink = MagicMock()

        stats = worker.run([intraday_req], sink)

        assert stats.ok_empty == 1
        assert stats.ok_with_data == 0
        assert stats.bars_emitted == 0
        sink.assert_not_called()
        bucket.record_success.assert_called_once()
        bucket.record_rate_limit_hit.assert_not_called()
        kwargs = audit.append.call_args.kwargs
        assert kwargs["action"] == AUDIT_ACTION_CHUNK_DONE
        assert kwargs["payload"]["outcome"] == "OK_EMPTY"


class TestNoData:
    def test_dh907_route_separate_from_ok_empty(self, intraday_req: IntradayChunkRequest) -> None:
        worker, _client, bucket, audit, _logger = _make_worker(
            fetch_returns=[_result(intraday_req, Outcome.NO_DATA, DhanErrorDH907())],
        )
        sink = MagicMock()

        stats = worker.run([intraday_req], sink)

        assert stats.no_data == 1
        assert stats.ok_empty == 0
        sink.assert_not_called()
        bucket.record_success.assert_called_once()
        kwargs = audit.append.call_args.kwargs
        assert kwargs["action"] == AUDIT_ACTION_CHUNK_DONE
        assert kwargs["payload"]["outcome"] == "NO_DATA"
        assert kwargs["payload"]["error_code"] == "DH-907"


# ────────────────────────────────────────────────────────────────────────
# Outcome routing — retry path
# ────────────────────────────────────────────────────────────────────────


class TestRateLimitRetry:
    def test_one_rate_limit_then_ok(
        self, intraday_req: IntradayChunkRequest, fake_bar: OHLCBar
    ) -> None:
        sleeps: list[float] = []
        worker, _client, bucket, audit, _logger = _make_worker(
            fetch_returns=[
                _result(intraday_req, Outcome.RATE_LIMIT, DhanError805()),
                _ok_with_data(intraday_req, (fake_bar,)),
            ],
            max_retries=3,
            sleep_recorder=sleeps,
        )
        sink = MagicMock()

        stats = worker.run([intraday_req], sink)

        assert stats.ok_with_data == 1
        assert stats.retries_total == 1
        bucket.record_rate_limit_hit.assert_called_once()
        bucket.record_success.assert_called_once()
        # 2 audit calls: 1 retry + 1 done
        assert audit.append.call_count == 2
        retry_call_kwargs = audit.append.call_args_list[0].kwargs
        assert retry_call_kwargs["action"] == AUDIT_ACTION_CHUNK_RETRY
        assert retry_call_kwargs["payload"]["attempt"] == 1
        assert retry_call_kwargs["payload"]["max_retries"] == 3
        # Sleep called exactly once (before second attempt)
        assert len(sleeps) == 1
        assert sleeps[0] > 0.0  # backoff_delay schedule slot 0 = 2.0 ± jitter


class TestTimeoutRetry:
    def test_timeout_does_not_call_record_rate_limit_hit(
        self, intraday_req: IntradayChunkRequest, fake_bar: OHLCBar
    ) -> None:
        sleeps: list[float] = []
        worker, _client, bucket, _audit, _logger = _make_worker(
            fetch_returns=[
                _result(intraday_req, Outcome.TIMEOUT, DhanErrorDH909()),
                _ok_with_data(intraday_req, (fake_bar,)),
            ],
            max_retries=3,
            sleep_recorder=sleeps,
        )

        worker.run([intraday_req], lambda _b, _r: None)

        bucket.record_rate_limit_hit.assert_not_called()
        bucket.record_success.assert_called_once()
        assert len(sleeps) == 1


class TestGenericApiErrorRetry:
    def test_generic_api_error_retries(
        self, intraday_req: IntradayChunkRequest, fake_bar: OHLCBar
    ) -> None:
        worker, _client, bucket, _audit, _logger = _make_worker(
            fetch_returns=[
                _result(intraday_req, Outcome.GENERIC_API_ERROR, DhanErrorDH908()),
                _ok_with_data(intraday_req, (fake_bar,)),
            ],
            max_retries=3,
        )

        stats = worker.run([intraday_req], lambda _b, _r: None)

        assert stats.retries_total == 1
        assert stats.ok_with_data == 1
        bucket.record_rate_limit_hit.assert_not_called()


class TestRetryExhaustion:
    def test_max_retries_then_quarantine_retry_exhausted(
        self, intraday_req: IntradayChunkRequest
    ) -> None:
        # 5 RATE_LIMITs in a row with max_retries=5 → quarantine.
        sleeps: list[float] = []
        worker, _client, bucket, audit, _logger = _make_worker(
            fetch_returns=[
                _result(intraday_req, Outcome.RATE_LIMIT, DhanError805()) for _ in range(5)
            ],
            max_retries=5,
            sleep_recorder=sleeps,
        )

        stats = worker.run([intraday_req], lambda _b, _r: None)

        assert stats.quarantined == 1
        assert stats.retries_total == 5
        assert stats.halt_reason is None  # quarantine is NOT halt
        # Sleep N-1 times: between attempts only, not after final.
        assert len(sleeps) == 4
        # record_rate_limit_hit fires on every RATE_LIMIT (5 times).
        assert bucket.record_rate_limit_hit.call_count == 5
        bucket.record_success.assert_not_called()
        # 5 retry audits + 1 final QUARANTINE_NEW
        assert audit.append.call_count == 6
        final_kwargs = audit.append.call_args_list[-1].kwargs
        assert final_kwargs["action"] == AUDIT_ACTION_QUARANTINE_NEW
        assert final_kwargs["payload"]["reason"] == QUARANTINE_REASON_RETRY_EXHAUSTED
        assert final_kwargs["payload"]["attempts"] == 5

    def test_no_sleep_when_max_retries_is_one(self, intraday_req: IntradayChunkRequest) -> None:
        sleeps: list[float] = []
        worker, *_ = _make_worker(
            fetch_returns=[_result(intraday_req, Outcome.TIMEOUT, DhanErrorDH909())],
            max_retries=1,
            sleep_recorder=sleeps,
        )
        worker.run([intraday_req], lambda _b, _r: None)
        assert sleeps == []


# ────────────────────────────────────────────────────────────────────────
# Outcome routing — quarantine path
# ────────────────────────────────────────────────────────────────────────


class TestBadRequestQuarantine:
    def test_quarantines_writes_audit_no_success_recorded(
        self, intraday_req: IntradayChunkRequest
    ) -> None:
        worker, _client, bucket, audit, _logger = _make_worker(
            fetch_returns=[_result(intraday_req, Outcome.BAD_REQUEST, DhanError814())],
        )

        stats = worker.run([intraday_req], lambda _b, _r: None)

        assert stats.quarantined == 1
        assert stats.halt_reason is None
        bucket.record_success.assert_not_called()
        bucket.record_rate_limit_hit.assert_not_called()
        kwargs = audit.append.call_args.kwargs
        assert kwargs["action"] == AUDIT_ACTION_QUARANTINE_NEW
        assert kwargs["payload"]["outcome"] == "BAD_REQUEST"
        assert kwargs["payload"]["reason"] == "bad_request"
        assert kwargs["payload"]["error_code"] == "814"


class TestNotFoundQuarantine:
    def test_not_found_routes_to_quarantine(self, intraday_req: IntradayChunkRequest) -> None:
        # NOTE: NOT_FOUND is not produced by any Dhan code today (Hostile
        # agent #3 — forward-compat branch); we exercise it directly here.
        worker, _client, bucket, audit, _logger = _make_worker(
            fetch_returns=[_result(intraday_req, Outcome.NOT_FOUND)],
        )

        stats = worker.run([intraday_req], lambda _b, _r: None)

        assert stats.quarantined == 1
        bucket.record_success.assert_not_called()
        kwargs = audit.append.call_args.kwargs
        assert kwargs["payload"]["outcome"] == "NOT_FOUND"


# ────────────────────────────────────────────────────────────────────────
# Outcome routing — halt path
# ────────────────────────────────────────────────────────────────────────


class TestAuthHalt:
    def test_auth_failed_raises_halt_with_partial_stats(
        self, intraday_req: IntradayChunkRequest, fake_bar: OHLCBar
    ) -> None:
        # First request OK; second AUTH_FAILED → halt; third never runs.
        worker, client, _bucket, audit, _logger = _make_worker(
            fetch_returns=[
                _ok_with_data(intraday_req, (fake_bar,)),
                _result(intraday_req, Outcome.AUTH_FAILED, DhanError808()),
                _ok_with_data(intraday_req, (fake_bar,)),
            ],
        )

        with pytest.raises(DhanWorkerHaltError) as exc_info:
            worker.run(
                [intraday_req, intraday_req, intraday_req],
                lambda _b, _r: None,
            )

        err = exc_info.value
        assert err.reason == "AUTH_FAILED"
        assert err.partial_stats.ok_with_data == 1
        assert err.partial_stats.bars_emitted == 1
        assert err.partial_stats.quarantined == 1
        assert err.partial_stats.halt_reason == "AUTH_FAILED"
        # 3rd request did NOT execute — only 2 fetches happened.
        assert client.fetch_intraday.call_count == 2
        # Audit chain: 1 INGEST_CHUNK_DONE + 1 QUARANTINE_NEW
        actions = [call.kwargs["action"] for call in audit.append.call_args_list]
        assert actions == [AUDIT_ACTION_CHUNK_DONE, AUDIT_ACTION_QUARANTINE_NEW]


class TestAuthzHalt:
    def test_authz_failed_raises_halt(self, intraday_req: IntradayChunkRequest) -> None:
        worker, *_ = _make_worker(
            fetch_returns=[_result(intraday_req, Outcome.AUTHZ_FAILED, DhanError813())],
        )
        with pytest.raises(DhanWorkerHaltError) as exc_info:
            worker.run([intraday_req], lambda _b, _r: None)
        assert exc_info.value.reason == "AUTHZ_FAILED"


class TestFatalHalt:
    def test_fatal_raises_halt(self, intraday_req: IntradayChunkRequest) -> None:
        worker, *_ = _make_worker(
            fetch_returns=[_result(intraday_req, Outcome.FATAL, DhanError800())],
        )
        with pytest.raises(DhanWorkerHaltError) as exc_info:
            worker.run([intraday_req], lambda _b, _r: None)
        assert exc_info.value.reason == "FATAL"
        assert exc_info.value.partial_stats.quarantined == 1


class TestDailyQuotaHalt:
    def test_daily_quota_exceeded_caught_and_halted(
        self, intraday_req: IntradayChunkRequest
    ) -> None:
        worker, _client, _bucket, audit, _logger = _make_worker(
            fetch_returns=[DailyQuotaExceeded("count=100000")],
        )
        with pytest.raises(DhanWorkerHaltError) as exc_info:
            worker.run([intraday_req], lambda _b, _r: None)

        assert exc_info.value.reason == HALT_REASON_DAILY_QUOTA
        assert exc_info.value.partial_stats.quarantined == 1
        # QUARANTINE_NEW audit fires once.
        kwargs = audit.append.call_args.kwargs
        assert kwargs["action"] == AUDIT_ACTION_QUARANTINE_NEW
        assert kwargs["payload"]["reason"] == HALT_REASON_DAILY_QUOTA
        # The exception chain preserves the original DailyQuotaExceeded.
        assert isinstance(exc_info.value.__cause__, DailyQuotaExceeded)


# ────────────────────────────────────────────────────────────────────────
# Aggregate / cross-request behaviour
# ────────────────────────────────────────────────────────────────────────


class TestAggregateRun:
    def test_mixed_outcomes_accumulate(
        self, intraday_req: IntradayChunkRequest, fake_bar: OHLCBar
    ) -> None:
        # ok_with_data, ok_empty, no_data, bad_request, ok_with_data
        worker, _client, _bucket, _audit, _logger = _make_worker(
            fetch_returns=[
                _ok_with_data(intraday_req, (fake_bar, fake_bar)),
                _result(intraday_req, Outcome.OK_EMPTY),
                _result(intraday_req, Outcome.NO_DATA, DhanErrorDH907()),
                _result(intraday_req, Outcome.BAD_REQUEST, DhanError814()),
                _ok_with_data(intraday_req, (fake_bar,)),
            ],
        )
        stats = worker.run(
            [intraday_req] * 5,
            lambda _b, _r: None,
        )
        assert stats.ok_with_data == 2
        assert stats.ok_empty == 1
        assert stats.no_data == 1
        assert stats.quarantined == 1
        assert stats.bars_emitted == 3
        assert stats.chunks_total() == 5
        assert stats.halt_reason is None

    def test_dhan_worker_complete_event_always_fires_on_halt(
        self, intraday_req: IntradayChunkRequest
    ) -> None:
        worker, _client, _bucket, _audit, logger = _make_worker(
            fetch_returns=[_result(intraday_req, Outcome.FATAL, DhanError800())],
        )
        with pytest.raises(DhanWorkerHaltError):
            worker.run([intraday_req], lambda _b, _r: None)
        # logger.info called at least once with the completion event.
        # Find the call.
        complete_calls = [
            call
            for call in logger.info.call_args_list
            if call.args and call.args[0] == "dhan_worker_complete"
        ]
        assert len(complete_calls) == 1
        assert complete_calls[0].kwargs["halt_reason"] == "FATAL"
        assert complete_calls[0].kwargs["quarantined"] == 1


# ────────────────────────────────────────────────────────────────────────
# Daily-historical dispatch
# ────────────────────────────────────────────────────────────────────────


class TestHistoricalDailyDispatch:
    def test_daily_request_uses_fetch_historical_daily(
        self,
        daily_req: HistoricalDailyRequest,
        fake_bar: OHLCBar,
    ) -> None:
        worker, client, _bucket, audit, _logger = _make_worker(
            fetch_returns=[_ok_with_data(daily_req, (fake_bar,))],
        )
        worker.run([daily_req], lambda _b, _r: None)

        client.fetch_historical_daily.assert_called_once()
        client.fetch_intraday.assert_not_called()
        kwargs = audit.append.call_args.kwargs
        assert "daily" in kwargs["subject"]


# ────────────────────────────────────────────────────────────────────────
# Backoff schedule reuse (Rule #11 COMMON)
# ────────────────────────────────────────────────────────────────────────


class TestCanonicalBackoffReuse:
    def test_backoff_delays_match_canonical_schedule(
        self, intraday_req: IntradayChunkRequest, fake_bar: OHLCBar
    ) -> None:
        # 3 TIMEOUTs then OK. Backoff slots 1, 2, 3 from the canonical
        # 10-slot schedule (2s, 4s, 8s) ± 20% jitter.
        sleeps: list[float] = []
        worker, *_ = _make_worker(
            fetch_returns=[
                _result(intraday_req, Outcome.TIMEOUT, DhanErrorDH909()),
                _result(intraday_req, Outcome.TIMEOUT, DhanErrorDH909()),
                _result(intraday_req, Outcome.TIMEOUT, DhanErrorDH909()),
                _ok_with_data(intraday_req, (fake_bar,)),
            ],
            max_retries=4,
            sleep_recorder=sleeps,
        )
        worker.run([intraday_req], lambda _b, _r: None)
        # 3 retry attempts → 3 sleeps between attempts.
        assert len(sleeps) == 3
        # ±20% jitter on (2.0, 4.0, 8.0) → expected windows.
        assert 1.6 <= sleeps[0] <= 2.4
        assert 3.2 <= sleeps[1] <= 4.8
        assert 6.4 <= sleeps[2] <= 9.6


# ────────────────────────────────────────────────────────────────────────
# Default max_retries reuses FSM cap
# ────────────────────────────────────────────────────────────────────────


class TestDefaultMaxRetries:
    def test_default_max_retries_matches_max_soft_retries(self) -> None:
        # Construct with minimum dependencies; check default value.
        worker = DhanWorker(
            client=cast(DhanClient, MagicMock(spec=DhanClient)),
            bucket=cast(AdaptiveTokenBucket, MagicMock(spec=AdaptiveTokenBucket)),
            audit=cast(AuditWriter, MagicMock(spec=AuditWriter)),
            logger=MagicMock(),
        )
        assert worker.max_retries == MAX_SOFT_RETRIES


# ────────────────────────────────────────────────────────────────────────
# Empty request list
# ────────────────────────────────────────────────────────────────────────


class TestEmptyRequests:
    def test_empty_requests_returns_zero_stats(self) -> None:
        worker, client, bucket, audit, logger = _make_worker(fetch_returns=[])
        stats = worker.run([], lambda _b, _r: None)
        assert stats == IngestStats()
        client.fetch_intraday.assert_not_called()
        bucket.record_success.assert_not_called()
        audit.append.assert_not_called()
        # dhan_worker_complete still fires.
        complete_calls = [
            call
            for call in logger.info.call_args_list
            if call.args and call.args[0] == "dhan_worker_complete"
        ]
        assert len(complete_calls) == 1


# ────────────────────────────────────────────────────────────────────────
# Multiple instruments — security_id appears in subject
# ────────────────────────────────────────────────────────────────────────


class TestInstrumentSubjectIsolation:
    def test_each_instrument_distinct_subject(self, fake_bar: OHLCBar) -> None:
        reqs: list[IntradayChunkRequest | HistoricalDailyRequest] = [
            IntradayChunkRequest(
                instrument=inst,
                interval="1",
                from_date=date(2024, 4, 1),
                to_date=date(2024, 4, 15),
            )
            for inst in (NIFTY, BANKNIFTY, SENSEX, INDIAVIX)
        ]
        worker, _client, _bucket, audit, _logger = _make_worker(
            fetch_returns=[_ok_with_data(req, (fake_bar,)) for req in reqs],
        )
        worker.run(reqs, lambda _b, _r: None)

        subjects = {call.kwargs["subject"] for call in audit.append.call_args_list}
        assert len(subjects) == 4
        for inst in (NIFTY, BANKNIFTY, SENSEX, INDIAVIX):
            assert any(f"chunk:{inst.security_id}:" in s for s in subjects)
