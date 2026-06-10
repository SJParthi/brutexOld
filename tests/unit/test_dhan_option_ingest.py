"""Tests for `brutex.adapters.dhan.option_ingest` — rolling-option series pull.

100% line+branch via an injected fake JSON poster + fake sink (no network,
no lake): window tiling (single + multi), pacing, session filtering,
`data`-wrapper vs flat-payload fallback, transient-skip, and terminal-raise.
"""

# Whitebox use of the module-private `_default_retry_backoff` (the bounded-backoff
# oracle) is sanctioned for tests, mirroring `test_dhan_ingest.py`.
# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

import pytest
from brutex.adapters.dhan.auth_guard import AuthGuard
from brutex.adapters.dhan.constants import ENDPOINT_ROLLING_OPTION
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanBadRequestError,
    DhanRateLimitError,
    DhanServerError,
)
from brutex.adapters.dhan.option_ingest import (
    ALL_REQUIRED_DATA,
    OptionIngestReport,
    ingest_rolling_option_series,
)
from brutex.data.models import OptionBar
from brutex.data.window_ledger import WindowLedger
from brutex.observability.correlation import pull_id_scope
from structlog.testing import capture_logs

# 1609732800 = 2021-01-04 04:00:00 UTC = 09:30 IST → in session [09:15,15:30).
_TS_IN = 1609732800
# 1609729200 = 2021-01-04 03:00:00 UTC = 08:30 IST → pre-open, out of session.
_TS_OUT = 1609729200


def _side() -> dict[str, object]:
    """A side with one in-session bar + one pre-open bar (filtered out)."""
    return {
        "timestamp": [_TS_IN, _TS_OUT],
        "open": [100, 100],
        "high": [110, 110],
        "low": [90, 90],
        "close": [105, 105],
        "volume": [250, 250],
        "oi": [1200, 1200],
        "strike": [17500, 17500],
        "spot": [17480.5, 17480.5],
        "iv": [0.15, 0.15],
    }


class _FakeClient:
    def __init__(self, responses: list[dict[str, object] | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
        self.calls.append((endpoint, dict(body)))
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeSink:
    def __init__(self) -> None:
        self.batches: list[list[OptionBar]] = []
        self.kwargs: list[dict[str, object]] = []

    def append_option_bars(
        self,
        *,
        security_id: str,
        exch_id: str,
        underlying_symbol: str,
        exchange_segment: str,
        expiry_flag: str,
        expiry_code: int,
        strike: str,
        drv_option_type: str,
        bars: Sequence[OptionBar],
    ) -> int:
        self.kwargs.append(
            {
                "security_id": security_id,
                "exch_id": exch_id,
                "underlying_symbol": underlying_symbol,
                "exchange_segment": exchange_segment,
                "expiry_flag": expiry_flag,
                "expiry_code": expiry_code,
                "strike": strike,
                "drv_option_type": drv_option_type,
            }
        )
        self.batches.append(list(bars))
        return len(bars)


def _run(
    client: _FakeClient,
    sink: _FakeSink,
    *,
    start: date = date(2021, 1, 4),
    end: date = date(2021, 1, 20),
    pace: object = None,
    on_rate_limit: object = None,
    on_timeout: object = None,
    on_success: object = None,
    max_retry_passes: int = 0,
    retry_backoff: object = lambda _i: None,
    auth_guard: AuthGuard | None = None,
) -> OptionIngestReport:
    # ``max_retry_passes`` defaults to 0 here so a single error response is a
    # single attempt (the in-run resume-cursor retry, DEC-OPTION-429-RECOVERY-001,
    # is exercised by its own dedicated tests with multi-response clients). The
    # injected no-op ``retry_backoff`` guarantees NO real sleep in any test.
    return ingest_rolling_option_series(
        client,
        sink,
        security_id="13",
        exch_id="NSE",
        underlying_symbol="NIFTY",
        exchange_segment="NSE_FNO",
        instrument="OPTIDX",
        expiry_flag="WEEK",
        expiry_code=0,
        strike="ATM",
        drv_option_type="CALL",
        start=start,
        end=end,
        pace=pace,  # type: ignore[arg-type]
        on_rate_limit=on_rate_limit,  # type: ignore[arg-type]
        on_timeout=on_timeout,  # type: ignore[arg-type]
        on_success=on_success,  # type: ignore[arg-type]
        max_retry_passes=max_retry_passes,
        retry_backoff=retry_backoff,  # type: ignore[arg-type]
        auth_guard=auth_guard,
    )


def test_happy_single_window_filters_to_session() -> None:
    client = _FakeClient([{"data": {"ce": _side(), "pe": None}}])
    sink = _FakeSink()
    report = _run(client, sink)
    assert report == OptionIngestReport(windows=1, bars_appended=1, transient_failures=0)
    # Only the in-session bar survived; the pre-open bar was dropped.
    (batch,) = sink.batches
    assert [b.side for b in batch] == ["CE"]
    assert batch[0].strike == Decimal("17500")
    # The request went to the rolling-option endpoint with the full field set.
    endpoint, body = client.calls[0]
    assert endpoint == ENDPOINT_ROLLING_OPTION
    assert body["interval"] == "1"
    assert body["requiredData"] == list(ALL_REQUIRED_DATA)
    # Sink received the series coordinates.
    assert sink.kwargs[0]["drv_option_type"] == "CALL"


def test_multiple_windows_tiled_30d() -> None:
    # ~60-day range → 2 thirty-day windows → 2 fetches.
    client = _FakeClient([{"data": {"ce": _side(), "pe": None}} for _ in range(2)])
    sink = _FakeSink()
    report = _run(client, sink, start=date(2021, 1, 4), end=date(2021, 3, 5))
    assert report.windows == 2
    assert len(client.calls) == 2
    assert report.bars_appended == 2  # one in-session bar per window


def test_pace_called_once_per_window() -> None:
    client = _FakeClient([{"data": {"ce": _side(), "pe": None}} for _ in range(2)])
    sink = _FakeSink()
    ticks: list[int] = []
    _run(client, sink, start=date(2021, 1, 4), end=date(2021, 3, 5), pace=lambda: ticks.append(1))
    assert len(ticks) == 2


def test_flat_payload_without_data_wrapper() -> None:
    # No "data" key → the parser reads the payload object itself.
    client = _FakeClient([{"ce": _side(), "pe": None}])
    sink = _FakeSink()
    report = _run(client, sink)
    assert report.bars_appended == 1


def test_data_not_a_mapping_falls_back_to_payload() -> None:
    # "data" present but not an object → fall back to the payload (no sides).
    client = _FakeClient([{"data": [1, 2, 3], "ce": _side()}])
    sink = _FakeSink()
    report = _run(client, sink)
    assert report.bars_appended == 1  # parsed from the payload-level "ce"


def test_transient_error_aborts_without_committing() -> None:
    # A transient cursor that outruns its (here 0) retry budget ABORTS the whole
    # series, committing NOTHING — the partition stays EMPTY, never partial
    # (DEC-OPTION-429-RECOVERY-001). The residual is reported as still-failing so
    # the next pull re-fetches it (the no-skip guarantee).
    client = _FakeClient([DhanServerError("HTTP 503")])
    sink = _FakeSink()
    report = _run(client, sink)
    assert report == OptionIngestReport(
        windows=1,
        bars_appended=0,
        transient_failures=1,
        skipped_windows=0,
        retried_windows=0,
        still_failed_windows=1,
    )
    assert report.complete is False
    assert sink.batches == []


def test_terminal_error_propagates() -> None:
    client = _FakeClient([DhanBadRequestError("HTTP 400")])
    sink = _FakeSink()
    with pytest.raises(DhanBadRequestError):
        _run(client, sink)


def test_auth_failure_trips_guard_at_source() -> None:
    # DEC-OPTIONS-UNTIL-CLEAN-001 (the options arm of
    # DEC-DHAN-AUTH-FAILFAST-RENEW-001): a dead-token terminal error trips the
    # SHARED latch INSIDE the series — at the failure source, before the fan-out
    # drain — so not-yet-dispatched sibling series short-circuit without waiting
    # out the pool-width doomed-call window. The error still propagates (empty
    # partition, quarantined upstream).
    guard = AuthGuard()
    client = _FakeClient([DhanAuthError("HTTP 401 unauthorized: token expired")])
    sink = _FakeSink()
    with pytest.raises(DhanAuthError):
        _run(client, sink, auth_guard=guard)
    assert guard.tripped() is True
    cause = guard.cause()
    assert cause is not None and "401" in cause
    assert sink.batches == []  # all-or-nothing: nothing committed


def test_non_auth_terminal_never_trips_guard() -> None:
    # A genuine terminal error (real 400, not a dead token) must NOT trip the
    # dead-token latch — the rest of the fan-out keeps pulling.
    guard = AuthGuard()
    client = _FakeClient([DhanBadRequestError("HTTP 400")])
    sink = _FakeSink()
    with pytest.raises(DhanBadRequestError):
        _run(client, sink, auth_guard=guard)
    assert guard.tripped() is False


def test_on_success_fires_per_clean_window() -> None:
    # The rate-feedback recover-up hook (DEC-DHAN-TIMEOUT-BACKOFF-001) fires once
    # per successfully-fetched window, and NOT on a window that errors.
    client = _FakeClient([{"data": {"ce": _side(), "pe": None}}])
    sink = _FakeSink()
    oks: list[int] = []
    report = _run(client, sink, on_success=lambda: oks.append(1))
    assert report.windows == 1
    assert oks == [1]


def test_timeout_routes_to_on_timeout_not_on_rate_limit() -> None:
    # A retryable NON-429 stall (a 5xx / timeout — a struggling Dhan) is routed to
    # the GENTLE timeout ladder via on_timeout, NOT the 429 hook; on_success must
    # not fire for the errored window (DEC-DHAN-TIMEOUT-BACKOFF-001).
    client = _FakeClient([DhanServerError("HTTP 503")])
    sink = _FakeSink()
    rl: list[int] = []
    to: list[int] = []
    oks: list[int] = []
    report = _run(
        client,
        sink,
        on_rate_limit=lambda: rl.append(1),
        on_timeout=lambda: to.append(1),
        on_success=lambda: oks.append(1),
    )
    assert report.transient_failures == 1
    assert report.complete is False
    assert to == [1]
    assert rl == []
    assert oks == []


def test_rate_limit_routes_to_on_rate_limit_not_on_timeout() -> None:
    # A swallowed 429 drives the 429 ladder (on_rate_limit), never the timeout one.
    client = _FakeClient([DhanRateLimitError("HTTP 429")])
    sink = _FakeSink()
    rl: list[int] = []
    to: list[int] = []
    report = _run(client, sink, on_rate_limit=lambda: rl.append(1), on_timeout=lambda: to.append(1))
    assert report.transient_failures == 1
    assert report.complete is False
    assert rl == [1]
    assert to == []


def test_swallowed_429_without_a_hook_aborts_cleanly() -> None:
    # A 429 with NO on_rate_limit hook wired (e.g. a feedback-free pull) still
    # aborts the series cleanly via the hook guard's None branch — nothing is
    # committed (empty, never partial).
    client = _FakeClient([DhanRateLimitError("HTTP 429")])
    sink = _FakeSink()
    report = _run(client, sink)  # no feedback hooks
    assert report == OptionIngestReport(
        windows=1,
        bars_appended=0,
        transient_failures=1,
        skipped_windows=0,
        retried_windows=0,
        still_failed_windows=1,
    )
    assert sink.batches == []


class TestResumeCursorAndTransactionalCommit:
    """A 429 mid-pagination is RESUMED to completion, or the partition is left EMPTY.

    DEC-OPTION-429-RECOVERY-001 (audit Gap #2): the option series ingest is
    transactional — a transient (429 / 5xx / timeout) cursor is re-fetched in-run
    up to ``max_retry_passes`` extra attempts, and the sink is written + the ledger
    recorded ONLY after EVERY cursor is fetched cleanly. So the partition the lake
    holds is always COMPLETE (all windows committed) or EMPTY (nothing committed),
    never a page-holed partial.
    """

    # date(2021,1,4) → date(2021,3,5) tiles into exactly two 30-day cursors.
    _START = date(2021, 1, 4)
    _END = date(2021, 3, 5)

    def test_429_at_first_cursor_heals_on_retry_completes(self) -> None:
        # cursor 0: 429 then OK (resumed); cursor 1: OK. The whole series commits.
        client = _FakeClient(
            [
                DhanRateLimitError("HTTP 429"),
                {"data": {"ce": _side(), "pe": None}},
                {"data": {"ce": _side(), "pe": None}},
            ]
        )
        sink = _FakeSink()
        report = _run(client, sink, start=self._START, end=self._END, max_retry_passes=2)
        assert report.windows == 2
        assert report.bars_appended == 2  # both cursors committed
        assert report.retried_windows == 1
        assert report.still_failed_windows == 0
        assert report.complete is True
        assert len(sink.batches) == 2  # both windows written

    def test_permanent_429_at_second_cursor_leaves_partition_empty(self) -> None:
        # cursor 0 succeeds, cursor 1 429s past its retry budget → the series aborts
        # and commits NOTHING — NOT even the already-fetched cursor 0 (transactional).
        client = _FakeClient(
            [
                {"data": {"ce": _side(), "pe": None}},  # cursor 0 fetched + staged
                DhanRateLimitError("HTTP 429"),  # cursor 1 attempt 1
                DhanRateLimitError("HTTP 429"),  # cursor 1 retry 1
                DhanRateLimitError("HTTP 429"),  # cursor 1 retry 2 → budget spent
            ]
        )
        sink = _FakeSink()
        report = _run(client, sink, start=self._START, end=self._END, max_retry_passes=2)
        assert report.bars_appended == 0
        assert report.complete is False
        assert report.still_failed_windows == 2  # BOTH windows deferred (none committed)
        assert report.retried_windows == 1
        assert sink.batches == []  # the staged cursor-0 bars were NEVER committed

    def test_abort_does_not_commit(self) -> None:
        # On abort, NOTHING is committed so a re-pull re-fetches every window
        # (the no-skip guarantee — the partition advances all-or-nothing).
        client = _FakeClient(
            [
                {"data": {"ce": _side(), "pe": None}},
                DhanRateLimitError("HTTP 429"),
            ]
        )
        sink = _FakeSink()
        report = ingest_rolling_option_series(
            client,
            sink,
            security_id="13",
            exch_id="NSE",
            underlying_symbol="NIFTY",
            exchange_segment="NSE_FNO",
            instrument="OPTIDX",
            expiry_flag="WEEK",
            expiry_code=1,
            strike="ATM",
            drv_option_type="CALL",
            start=self._START,
            end=self._END,
            max_retry_passes=0,
            retry_backoff=lambda _i: None,
        )
        assert report.complete is False
        assert sink.batches == []  # NOTHING committed

    def test_complete_series_records_every_window_in_the_ledger(self) -> None:
        # DEC-WINDOW-LEDGER-001 (PR-4b): each committed option cursor is recorded
        # into the unified ledger DB as SUCCESS (same table as spot).
        from brutex.data.window_ledger import SUCCESS, WindowLedger

        ledger = WindowLedger()
        client = _FakeClient([{"data": {"ce": _side(), "pe": None}} for _ in range(2)])
        ingest_rolling_option_series(
            client,
            _FakeSink(),
            security_id="13",
            exch_id="NSE",
            underlying_symbol="NIFTY",
            exchange_segment="NSE_FNO",
            instrument="OPTIDX",
            expiry_flag="WEEK",
            expiry_code=1,
            strike="ATM",
            drv_option_type="CALL",
            start=self._START,
            end=self._END,
            ledger=ledger,
            max_retry_passes=2,
            retry_backoff=lambda _i: None,
        )
        records = ledger.records()
        assert len(records) == 2  # both committed cursors recorded
        assert {r.status for r in records} == {SUCCESS}

    def test_dh905_terminal_settles_skipped_in_ledger_and_reraises(self) -> None:
        # DEC-WINDOW-LEDGER-001 Stage c2: a Dhan DH-905 "no data present" for an
        # option window is settled SKIPPED in the ledger (so it stops aborting the
        # series forever) BUT still re-raised to the run_dhan_options_ingest
        # quarantine — behaviour unchanged, only the ledger is enriched.
        from brutex.data.window_ledger import SKIPPED, WindowLedger

        ledger = WindowLedger()
        client = _FakeClient([DhanBadRequestError("HTTP 400 DH-905 no data present")])
        with pytest.raises(DhanBadRequestError):
            ingest_rolling_option_series(
                client,
                _FakeSink(),
                security_id="13",
                exch_id="NSE",
                underlying_symbol="NIFTY",
                exchange_segment="NSE_FNO",
                instrument="OPTIDX",
                expiry_flag="WEEK",
                expiry_code=1,
                strike="ATM",
                drv_option_type="CALL",
                start=self._START,
                end=self._END,
                ledger=ledger,
                max_retry_passes=0,
                retry_backoff=lambda _i: None,
            )
        # The first window is settled SKIPPED (the known gap), not left pending.
        assert ledger.records()[0].status == SKIPPED

    def test_non_dh905_terminal_does_not_settle(self) -> None:
        # A non-DH-905 terminal 400 (a genuine malformed request) is re-raised
        # WITHOUT a ledger settle — only a real no-data gap is settled.
        from brutex.data.window_ledger import WindowLedger

        ledger = WindowLedger()
        client = _FakeClient([DhanBadRequestError("HTTP 400 malformed body")])
        with pytest.raises(DhanBadRequestError):
            ingest_rolling_option_series(
                client,
                _FakeSink(),
                security_id="13",
                exch_id="NSE",
                underlying_symbol="NIFTY",
                exchange_segment="NSE_FNO",
                instrument="OPTIDX",
                expiry_flag="WEEK",
                expiry_code=1,
                strike="ATM",
                drv_option_type="CALL",
                start=self._START,
                end=self._END,
                ledger=ledger,
                max_retry_passes=0,
                retry_backoff=lambda _i: None,
            )
        assert ledger.count == 0  # nothing settled — a real error, not a gap

    def test_backoff_invoked_with_zero_based_attempt_indices(self) -> None:
        # Two retries before success → backoff(0), backoff(1) (bounded exponential).
        seen: list[int] = []
        client = _FakeClient(
            [
                DhanRateLimitError("HTTP 429"),
                DhanRateLimitError("HTTP 429"),
                {"data": {"ce": _side(), "pe": None}},
            ]
        )
        report = _run(
            client,
            _FakeSink(),
            start=date(2021, 1, 4),
            end=date(2021, 1, 20),  # single cursor
            max_retry_passes=5,
            retry_backoff=seen.append,
        )
        assert seen == [0, 1]
        assert report.complete is True
        assert report.retried_windows == 1

    def test_default_backoff_is_bounded_exponential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # _default_retry_backoff: base 1.0 × 2**k, clamped to the 8.0 s ceiling —
        # no real sleep (the worst-case far-attempt index must NOT sleep unbounded).
        from brutex.adapters.dhan.option_ingest import (
            _default_retry_backoff,
        )

        slept: list[float] = []
        monkeypatch.setattr("brutex.adapters.dhan.option_ingest._time_sleep", slept.append)
        _default_retry_backoff(0)
        _default_retry_backoff(1)
        _default_retry_backoff(99)  # clamps to the ceiling
        assert slept == [1.0, 2.0, 8.0]

    def test_terminal_error_mid_series_commits_nothing(self) -> None:
        # A TERMINAL (non-retryable) error on cursor 1 raises straight out — and
        # because the commit is deferred, the already-fetched cursor 0 is NEVER
        # written (the partition stays empty; the series is quarantined upstream).
        client = _FakeClient(
            [
                {"data": {"ce": _side(), "pe": None}},
                DhanBadRequestError("HTTP 400"),
            ]
        )
        sink = _FakeSink()
        with pytest.raises(DhanBadRequestError):
            _run(client, sink, start=self._START, end=self._END, max_retry_passes=2)
        assert sink.batches == []


def _run_cov(
    client: _FakeClient,
    sink: _FakeSink,
    *,
    ledger: object = None,
) -> OptionIngestReport:
    """Drive a single-window option series threading the ledger skip authority (DEC-WINDOW-LEDGER-001)."""
    return ingest_rolling_option_series(
        client,
        sink,
        security_id="13",
        exch_id="NSE",
        underlying_symbol="NIFTY",
        exchange_segment="NSE_FNO",
        instrument="OPTIDX",
        expiry_flag="WEEK",
        expiry_code=1,
        strike="ATM",
        drv_option_type="CALL",
        start=date(2021, 1, 4),
        end=date(2021, 1, 5),  # 1-day range → exactly one rolling window
        ledger=ledger,  # type: ignore[arg-type]
    )


def test_ledger_marks_then_skips_already_pulled_window() -> None:
    """First pull records the window SUCCESS in the ledger; a re-pull skips it (no client call)."""
    from brutex.data.window_ledger import SUCCESS, WindowLedger

    led = WindowLedger()
    sink = _FakeSink()
    first = _run_cov(_FakeClient([{"data": {"ce": _side(), "pe": None}}]), sink, ledger=led)
    assert first.windows == 1
    assert first.bars_appended == 1  # one in-session bar survived the filter
    assert first.skipped_windows == 0
    assert {r.status for r in led.records()} == {SUCCESS}

    # Re-pull with the SAME ledger → the window is settled SUCCESS → skipped.
    # The client has ZERO responses, so any vendor call would IndexError (proving
    # the skip happens BEFORE the network hit — no wasted Dhan budget).
    second_client = _FakeClient([])
    second = _run_cov(second_client, sink, ledger=led)
    assert second.windows == 1
    assert second.skipped_windows == 1
    assert second.bars_appended == 0
    assert second_client.calls == []  # never hit the vendor


class TestAbortRecordsFailedCursor:
    """DEC-OPTION-WINDOW-PLAN-001 C1 — the aborting cursor is recorded FAILED, precisely.

    The hostile review's blocker: recording an aborted cursor with
    ``error_type=None`` classifies EMPTY and SKIP-masks the window after
    ``EMPTY_TERMINAL_THRESHOLD=3`` aborted pulls — the exact #598 masking class
    the ledger exists to kill. The error TYPE must survive the abort.
    """

    def _abort_once(self, ledger: WindowLedger, *, windows_end: date = date(2021, 1, 5)) -> None:
        client = _FakeClient([DhanRateLimitError("HTTP 429 too many requests")])
        ingest_rolling_option_series(
            client,
            _FakeSink(),
            security_id="13",
            exch_id="NSE",
            underlying_symbol="NIFTY",
            exchange_segment="NSE_FNO",
            instrument="OPTIDX",
            expiry_flag="WEEK",
            expiry_code=1,
            strike="ATM",
            drv_option_type="CALL",
            start=date(2021, 1, 4),
            end=windows_end,
            ledger=ledger,
            max_retry_passes=0,
            retry_backoff=lambda _i: None,
        )

    def test_aborted_cursor_recorded_failed_with_its_error_type(self) -> None:
        from brutex.data.window_ledger import WindowLedger

        ledger = WindowLedger()
        self._abort_once(ledger)
        (record,) = ledger.records()
        assert record.status == "FAILED"
        assert record.last_error == "DhanRateLimitError"

    def test_three_consecutive_aborts_stay_failed_never_skipped(self) -> None:
        # THE C1 regression: with error_type=None these three records would
        # classify EMPTY → empty_streak 3 → promoted SKIPPED → the storm-stuck
        # cursor permanently masked from every future pull.
        from brutex.data.window_ledger import WindowLedger

        ledger = WindowLedger()
        for _ in range(3):
            self._abort_once(ledger)
        (record,) = ledger.records()
        assert record.status == "FAILED"
        assert record.attempts == 3
        # Still re-fetched: a FAILED row is never skippable — the next pull's
        # client IS called (one response consumed, proving the vendor hit).
        retry_client = _FakeClient([{"data": {"ce": _side(), "pe": None}}])
        ingest_rolling_option_series(
            retry_client,
            _FakeSink(),
            security_id="13",
            exch_id="NSE",
            underlying_symbol="NIFTY",
            exchange_segment="NSE_FNO",
            instrument="OPTIDX",
            expiry_flag="WEEK",
            expiry_code=1,
            strike="ATM",
            drv_option_type="CALL",
            start=date(2021, 1, 4),
            end=date(2021, 1, 5),
            ledger=ledger,
            max_retry_passes=0,
            retry_backoff=lambda _i: None,
        )
        assert len(retry_client.calls) == 1
        (record,) = ledger.records()
        assert record.status == "SUCCESS"

    def test_multiwindow_abort_marks_only_the_failing_cursor(self) -> None:
        # Window 1 fetches clean (staged), window 2 exhausts its budget → the
        # series aborts committing NOTHING; ONLY window 2 is FAILED — the
        # staged-but-discarded window 1 stays absent/PLANNED (it did not fail).
        from brutex.data.window_ledger import WindowLedger

        ledger = WindowLedger()
        client = _FakeClient(
            [
                {"data": {"ce": _side(), "pe": None}},
                DhanRateLimitError("HTTP 429 too many requests"),
            ]
        )
        sink = _FakeSink()
        report = ingest_rolling_option_series(
            client,
            sink,
            security_id="13",
            exch_id="NSE",
            underlying_symbol="NIFTY",
            exchange_segment="NSE_FNO",
            instrument="OPTIDX",
            expiry_flag="WEEK",
            expiry_code=1,
            strike="ATM",
            drv_option_type="CALL",
            start=date(2021, 1, 4),
            end=date(2021, 2, 10),  # two 30-day windows
            ledger=ledger,
            max_retry_passes=0,
            retry_backoff=lambda _i: None,
        )
        assert report.complete is False
        assert sink.batches == []  # all-or-nothing: nothing committed
        (record,) = ledger.records()  # ONLY the failing cursor was recorded
        assert record.status == "FAILED"
        assert record.window_start == "2021-02-03"


def test_no_ledger_always_refetches_options() -> None:
    """Without a ledger there is no skip authority — an option re-pull always re-fetches."""
    _run_cov(_FakeClient([{"data": {"ce": _side(), "pe": None}}]), _FakeSink())
    second_client = _FakeClient([{"data": {"ce": _side(), "pe": None}}])
    second = _run_cov(second_client, _FakeSink())
    assert second.skipped_windows == 0
    assert second.bars_appended == 1
    assert second_client.calls != []  # the vendor WAS hit


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    """All captured events with ``event == name`` (in emission order)."""
    return [e for e in logs if e["event"] == name]


class TestOptionIngestLogging:
    """The rolling-option spine must be VISIBLE — window / parse / lake-write / series.

    DEC-DHAN-INGEST-LOGGING-001: each ``ingest.*`` event carries the pull's
    ``pull_id`` + the full series coordinate, all DEBUG (a 16k-series fan-out must
    not flood INFO). Observability only — the OptionIngestReport is unchanged.
    """

    def test_fetched_window_lights_all_nooks(self) -> None:
        client = _FakeClient([{"data": {"ce": _side(), "pe": None}}])
        sink = _FakeSink()
        with capture_logs() as logs, pull_id_scope("optpull00001"):
            _run(client, sink)
        # ── ingest.window (fetched) ── carries the full series coordinate.
        (window,) = _events(logs, "ingest.window")
        assert window["log_level"] == "debug"
        assert window["pull_id"] == "optpull00001"
        assert window["security_id"] == "13"
        assert window["expiry_flag"] == "WEEK"
        assert window["expiry_code"] == 0
        assert window["strike"] == "ATM"
        assert window["drv_option_type"] == "CALL"
        assert window["action"] == "fetched"
        assert window["window_start"] == "2021-01-04"
        # ── ingest.parse ── 2 decoded, 1 survives the session gate.
        (parse,) = _events(logs, "ingest.parse")
        assert parse["pull_id"] == "optpull00001"
        assert parse["raw_bars"] == 2
        assert parse["session_bars"] == 1
        # ── ingest.lake_write ──
        (write,) = _events(logs, "ingest.lake_write")
        assert write["pull_id"] == "optpull00001"
        assert write["bars_appended"] == 1
        # ── ingest.series ──
        (series,) = _events(logs, "ingest.series")
        assert series["log_level"] == "debug"
        assert series["pull_id"] == "optpull00001"
        assert series["windows"] == 1
        assert series["bars_appended"] == 1
        assert series["skipped_windows"] == 0
        # Every spine event carries the FULL series coordinate (so an operator can
        # pinpoint the exact strike/expiry that produced N bars).
        coords = {
            "security_id": "13",
            "expiry_flag": "WEEK",
            "expiry_code": 0,
            "strike": "ATM",
            "drv_option_type": "CALL",
        }
        for ev in (window, parse, write, series):
            assert {k: ev[k] for k in coords} == coords

    def test_skipped_window_lights_only_the_skip_event(self) -> None:
        from brutex.data.window_ledger import WindowLedger

        led = WindowLedger()
        # First pull records the single window SUCCESS in the ledger.
        _run_cov(_FakeClient([{"data": {"ce": _side(), "pe": None}}]), _FakeSink(), ledger=led)
        # Re-pull → the window is settled SUCCESS → skipped before any vendor call.
        with capture_logs() as logs, pull_id_scope("optpull00002"):
            _run_cov(_FakeClient([]), _FakeSink(), ledger=led)
        (window,) = _events(logs, "ingest.window")
        assert window["action"] == "skipped"
        assert window["pull_id"] == "optpull00002"
        assert window["strike"] == "ATM"
        # Nothing fetched → no parse / lake-write events.
        assert _events(logs, "ingest.parse") == []
        assert _events(logs, "ingest.lake_write") == []
        (series,) = _events(logs, "ingest.series")
        assert series["pull_id"] == "optpull00002"
        assert series["windows"] == 1
        assert series["bars_appended"] == 0
        assert series["skipped_windows"] == 1

    def test_pull_id_is_empty_string_outside_a_scope(self) -> None:
        client = _FakeClient([{"data": {"ce": _side(), "pe": None}}])
        with capture_logs() as logs:
            _run(client, _FakeSink())
        assert _events(logs, "ingest.window")[0]["pull_id"] == ""
