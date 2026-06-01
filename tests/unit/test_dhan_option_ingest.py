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
from brutex.adapters.dhan.errors import DhanBadRequestError, DhanServerError
from brutex.adapters.dhan.option_ingest import (
    ALL_REQUIRED_DATA,
    OptionIngestReport,
    ingest_rolling_option_series,
)
from brutex.data.models import OptionBar

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
) -> OptionIngestReport:
    return ingest_rolling_option_series(
        client,
        sink,
        security_id="13",
        exchange_segment="NSE_FNO",
        instrument="OPTIDX",
        expiry_flag="WEEK",
        expiry_code=0,
        strike="ATM",
        drv_option_type="CALL",
        start=start,
        end=end,
        pace=pace,  # type: ignore[arg-type]
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


def _run_cov(client: _FakeClient, sink: _FakeSink, coverage: object) -> OptionIngestReport:
    """Drive a single-window series with a coverage ledger (DEC-FETCH-COVERAGE-001)."""
    return ingest_rolling_option_series(
        client,
        sink,
        security_id="13",
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
