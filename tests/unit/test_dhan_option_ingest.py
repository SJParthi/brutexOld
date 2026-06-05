"""Tests for `brutex.adapters.dhan.option_ingest` — rolling-option series pull.

100% line+branch via an injected fake JSON poster + fake sink (no network,
no lake): window tiling (single + multi), pacing, session filtering,
`data`-wrapper vs flat-payload fallback, transient-skip, and terminal-raise.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

import pytest
from brutex.adapters.dhan.constants import ENDPOINT_ROLLING_OPTION
from brutex.adapters.dhan.errors import (
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
) -> OptionIngestReport:
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


def test_transient_error_skips_window() -> None:
    client = _FakeClient([DhanServerError("HTTP 503")])
    sink = _FakeSink()
    report = _run(client, sink)
    assert report == OptionIngestReport(windows=1, bars_appended=0, transient_failures=1)
    assert sink.batches == []


def test_terminal_error_propagates() -> None:
    client = _FakeClient([DhanBadRequestError("HTTP 400")])
    sink = _FakeSink()
    with pytest.raises(DhanBadRequestError):
        _run(client, sink)


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
    assert rl == [1]
    assert to == []


def test_swallowed_429_without_a_hook_is_a_clean_skip() -> None:
    # A 429 with NO on_rate_limit hook wired (e.g. a feedback-free pull) still
    # just skips the window — the hook guard's None branch.
    client = _FakeClient([DhanRateLimitError("HTTP 429")])
    sink = _FakeSink()
    report = _run(client, sink)  # no feedback hooks
    assert report == OptionIngestReport(windows=1, bars_appended=0, transient_failures=1)


def _run_cov(client: _FakeClient, sink: _FakeSink, coverage: object) -> OptionIngestReport:
    """Drive a single-window series with a coverage ledger (DEC-FETCH-COVERAGE-001)."""
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
        coverage=coverage,  # type: ignore[arg-type]
    )


def test_coverage_marks_then_skips_already_pulled_window() -> None:
    """First pull marks the window complete; a re-pull skips it with no client call."""
    from brutex.data.fetch_coverage import FetchCoverage

    cov = FetchCoverage()
    sink = _FakeSink()
    first = _run_cov(_FakeClient([{"data": {"ce": _side(), "pe": None}}]), sink, cov)
    assert first.windows == 1
    assert first.bars_appended == 1  # one in-session bar survived the filter
    assert first.skipped_windows == 0
    assert cov.completed_count == 1

    # Re-pull with the SAME ledger → the window is already complete → skipped.
    # The client has ZERO responses, so any vendor call would IndexError (proving
    # the skip happens BEFORE the network hit — no wasted Dhan budget).
    second_client = _FakeClient([])
    second = _run_cov(second_client, sink, cov)
    assert second.windows == 1
    assert second.skipped_windows == 1
    assert second.bars_appended == 0
    assert second_client.calls == []  # never hit the vendor


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
        from brutex.data.fetch_coverage import FetchCoverage

        cov = FetchCoverage()
        # First pull marks the single window complete.
        _run_cov(_FakeClient([{"data": {"ce": _side(), "pe": None}}]), _FakeSink(), cov)
        # Re-pull → the window is skipped before any vendor call.
        with capture_logs() as logs, pull_id_scope("optpull00002"):
            _run_cov(_FakeClient([]), _FakeSink(), cov)
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
