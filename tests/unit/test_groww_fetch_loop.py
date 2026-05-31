"""Tests for `brutex.adapters.groww.fetch_loop` — the resilient fetch loop.

A fake fetcher simulates rate-limits, timeouts, empties, not-found, auth
rollover, terminal errors and crashes — proving the loop retries, settles
NO_DATA, re-auths, quarantines, and resumes without real sleep or SDK.
"""

from __future__ import annotations

from collections.abc import Sequence
from unittest import mock

from brutex.adapters.groww.fetch_loop import run_fetch
from brutex.data.watermark import ChunkKey, ChunkStatus, FetchWatermark


# Fake Groww exceptions — named exactly as classify_exception's table keys
# (it matches on type(exc).__name__), so these route like the real ones.
class VendorAPIRateLimitException(Exception): ...


class VendorAPINotFoundException(Exception): ...


class VendorAPIBadRequestException(Exception): ...


class VendorAPIAuthenticationException(Exception): ...


class _FakeFetcher:
    """Consumes a flat list of actions in call order.

    Each action is either an Exception instance (raised) or a sequence of
    bars (returned). Retries + multiple keys both consume sequentially.
    """

    def __init__(self, actions: list[object]) -> None:
        self._actions = actions
        self.calls = 0

    def fetch_chunk(self, key: ChunkKey) -> Sequence[object]:
        action = self._actions[self.calls]
        self.calls += 1
        if isinstance(action, Exception):
            raise action
        return action  # type: ignore[return-value]


def _key(symbol: str = "NSE-NIFTY") -> ChunkKey:
    return ChunkKey(symbol, "1m", "2021-01-01T09:15:00", "2021-01-31T15:30:00")


_ZERO_DELAY = lambda _retry: 0.0  # noqa: E731 — deterministic, no jitter


def _run(fetcher: _FakeFetcher, keys: list[ChunkKey], wm: FetchWatermark, **kw: object):
    return run_fetch(fetcher, keys, wm, sleep_fn=mock.Mock(), delay_fn=_ZERO_DELAY, **kw)  # type: ignore[arg-type]


class TestSuccessPaths:
    def test_success_with_bars_marks_completed(self) -> None:
        wm = FetchWatermark()
        out = _run(_FakeFetcher([["bar"]]), [_key()], wm)
        assert out.completed == 1
        assert wm.get(_key()) == ChunkStatus.COMPLETED

    def test_empty_response_marks_no_data(self) -> None:
        wm = FetchWatermark()
        out = _run(_FakeFetcher([[]]), [_key()], wm)
        assert out.no_data == 1
        assert wm.get(_key()) == ChunkStatus.NO_DATA


class TestResume:
    def test_settled_chunk_is_skipped(self) -> None:
        wm = FetchWatermark()
        wm.record(_key(), ChunkStatus.COMPLETED)
        fetcher = _FakeFetcher([])  # must never be called
        out = _run(fetcher, [_key()], wm)
        assert out.skipped == 1
        assert fetcher.calls == 0


class TestTransientRetry:
    def test_rate_limit_then_success(self) -> None:
        wm = FetchWatermark()
        sleep = mock.Mock()
        out = run_fetch(
            _FakeFetcher([VendorAPIRateLimitException(), ["bar"]]),
            [_key()],
            wm,
            sleep_fn=sleep,
            delay_fn=_ZERO_DELAY,
        )
        assert out.completed == 1
        sleep.assert_called_once()  # backed off exactly once before retry

    def test_first_retry_passes_one_based_count_to_delay_fn(self) -> None:
        # Regression (DEC-PULL-RETRY-OFFBYONE-001): the first retry passed the
        # pre-increment ``retry_count=0`` to ``delay_fn``, but ``backoff_delay``
        # is 1-based and raises ``ValueError`` on 0 — crashing the whole pull
        # the instant any transient error occurred. The delay count must start
        # at 1 and climb, never 0.
        wm = FetchWatermark()
        recorded: list[int] = []

        def _record_delay(retry_count: int) -> float:
            recorded.append(retry_count)
            return 0.0

        out = run_fetch(
            _FakeFetcher([VendorAPIRateLimitException(), VendorAPIRateLimitException(), ["bar"]]),
            [_key()],
            wm,
            sleep_fn=mock.Mock(),
            delay_fn=_record_delay,
        )
        assert out.completed == 1
        assert recorded == [1, 2]  # one-based, monotonic — never 0

    def test_real_backoff_delay_does_not_crash_on_first_retry(self) -> None:
        # The bug surfaced only with the PRODUCTION ``backoff_delay`` (the test
        # stub ignored its arg). Drive the loop with the real one to prove a
        # transient error backs off + retries instead of raising ``ValueError``.
        from brutex.orchestrator.retry import backoff_delay

        wm = FetchWatermark()
        out = run_fetch(
            _FakeFetcher([VendorAPIRateLimitException(), ["bar"]]),
            [_key()],
            wm,
            sleep_fn=mock.Mock(),
            delay_fn=backoff_delay,
        )
        assert out.completed == 1
        assert wm.get(_key()) == ChunkStatus.COMPLETED

    def test_exhausts_retries_then_quarantines(self) -> None:
        wm = FetchWatermark()
        actions: list[object] = [VendorAPIRateLimitException() for _ in range(3)]
        out = run_fetch(
            _FakeFetcher(actions),
            [_key()],
            wm,
            max_retries=2,
            sleep_fn=mock.Mock(),
            delay_fn=_ZERO_DELAY,
        )
        assert out.failed == 1
        assert wm.get(_key()) == ChunkStatus.FAILED


class TestEmptyAndNotFound:
    def test_not_found_marks_no_data(self) -> None:
        wm = FetchWatermark()
        out = _run(_FakeFetcher([VendorAPINotFoundException()]), [_key()], wm)
        assert out.no_data == 1
        assert wm.get(_key()) == ChunkStatus.NO_DATA


class TestAuthReauth:
    def test_auth_expiry_reauths_then_succeeds(self) -> None:
        wm = FetchWatermark()
        reauth = mock.Mock()
        out = _run(
            _FakeFetcher([VendorAPIAuthenticationException(), ["bar"]]),
            [_key()],
            wm,
            reauth=reauth,
        )
        assert out.completed == 1
        reauth.assert_called_once()

    def test_auth_with_no_reauth_quarantines(self) -> None:
        wm = FetchWatermark()
        out = _run(_FakeFetcher([VendorAPIAuthenticationException()]), [_key()], wm, reauth=None)
        assert out.failed == 1
        assert wm.get(_key()) == ChunkStatus.FAILED

    def test_auth_fails_again_after_reauth_quarantines(self) -> None:
        wm = FetchWatermark()
        reauth = mock.Mock()
        out = _run(
            _FakeFetcher([VendorAPIAuthenticationException(), VendorAPIAuthenticationException()]),
            [_key()],
            wm,
            reauth=reauth,
        )
        assert out.failed == 1
        reauth.assert_called_once()  # only one reauth attempt, then quarantine


class TestTerminal:
    def test_bad_request_quarantines(self) -> None:
        wm = FetchWatermark()
        out = _run(_FakeFetcher([VendorAPIBadRequestException()]), [_key()], wm)
        assert out.failed == 1
        assert wm.get(_key()) == ChunkStatus.FAILED


class TestUnexpectedErrorContainment:
    """A NON-vendor exception (one classify_exception can't map) must never
    crash the whole pull — it is quarantined per-chunk with the real type +
    message logged at error level, and the loop moves on.
    DEC-PULL-CHUNK-CONTAINMENT-001."""

    def test_non_vendor_exception_quarantines_not_crashes(self) -> None:
        wm = FetchWatermark()
        # A bare RuntimeError is NOT in BROKER_EXCEPTION_CLASSIFICATION, so
        # classify_exception raises UnknownExceptionError inside the loop.
        out = _run(_FakeFetcher([RuntimeError("corrupt watermark row")]), [_key()], wm)
        assert out.failed == 1
        assert wm.get(_key()) == ChunkStatus.FAILED

    def test_unexpected_error_logs_real_type_and_message(self) -> None:
        from structlog.testing import capture_logs

        wm = FetchWatermark()
        with capture_logs() as logs:
            _run(_FakeFetcher([KeyError("interval")]), [_key()], wm)
        rec = next(e for e in logs if e["event"] == "ingest.chunk.unexpected_error")
        assert rec["error_type"] == "KeyError"
        assert "interval" in rec["error"]
        assert rec["symbol"] == "NSE-NIFTY"
        assert rec["log_level"] == "error"

    def test_one_bad_chunk_does_not_abort_the_rest(self) -> None:
        # The blast-radius guarantee: a mid-run unexpected error on chunk B
        # must not stop chunks A (already done) or C (still to go).
        wm = FetchWatermark()
        k1, k2, k3 = _key("A"), _key("B"), _key("C")
        out = _run(
            _FakeFetcher([["bar"], ValueError("boom"), ["bar"]]),
            [k1, k2, k3],
            wm,
        )
        assert (out.completed, out.failed) == (2, 1)
        assert wm.get(k3) == ChunkStatus.COMPLETED


class TestMixedRun:
    def test_counts_across_multiple_chunks(self) -> None:
        wm = FetchWatermark()
        k1, k2, k3 = _key("A"), _key("B"), _key("C")
        out = _run(
            _FakeFetcher([["bar"], [], VendorAPIBadRequestException()]),
            [k1, k2, k3],
            wm,
        )
        assert (out.completed, out.no_data, out.failed, out.skipped) == (1, 1, 1, 0)


class TestFailureVisibility:
    """Every quarantined chunk must surface its REAL vendor reason (not just a
    count) — the silent 100%-failed pull was indistinguishable from no-data
    before this. DEC-PULL-ERROR-VISIBILITY-001 / observability."""

    def test_terminal_failure_logs_the_real_reason(self) -> None:
        from structlog.testing import capture_logs

        wm = FetchWatermark()
        fetcher = _FakeFetcher(
            [VendorAPIBadRequestException("400: invalid segment CASH for index NSE-NIFTY")]
        )
        with capture_logs() as logs:
            out = _run(fetcher, [_key()], wm)
        assert out.failed == 1
        failed = [e for e in logs if e["event"] == "ingest.chunk.failed"]
        assert len(failed) == 1
        rec = failed[0]
        assert rec["symbol"] == "NSE-NIFTY"
        assert rec["interval"] == "1m"
        assert rec["error_type"] == "VendorAPIBadRequestException"
        assert "invalid segment" in rec["error"]
        assert rec["retries_exhausted"] is None
        assert rec["log_level"] == "warning"

    def test_transient_exhaustion_logs_with_retries_exhausted(self) -> None:
        from structlog.testing import capture_logs

        wm = FetchWatermark()
        # max_retries=1 → one retry then quarantine; needs two rate-limit hits.
        fetcher = _FakeFetcher(
            [
                VendorAPIRateLimitException("429 too many"),
                VendorAPIRateLimitException("429 too many"),
            ]
        )
        with capture_logs() as logs:
            out = _run(fetcher, [_key()], wm, max_retries=1)
        assert out.failed == 1
        rec = next(e for e in logs if e["event"] == "ingest.chunk.failed")
        assert rec["outcome"] == "RATE_LIMIT"
        assert rec["retries_exhausted"] == 1
        assert "429" in rec["error"]

    def test_no_data_and_success_do_not_log_failure(self) -> None:
        from structlog.testing import capture_logs

        wm = FetchWatermark()
        with capture_logs() as logs:
            _run(_FakeFetcher([["bar"], []]), [_key("A"), _key("B")], wm)
        assert not [e for e in logs if e["event"] == "ingest.chunk.failed"]
