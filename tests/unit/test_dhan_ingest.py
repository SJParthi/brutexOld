"""Tests for `brutex.adapters.dhan.ingest` — window-by-window spot ingest.

100% line+branch: daily single-window vs intraday multi-window tiling, the
pace hook, transient-error counting, terminal-error propagation, and the
bars-appended accounting. Driven by a real DhanMarketAdapter over a
controllable fake transport + a recording fake sink.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

import pytest
from brutex.adapters.dhan.adapter import DhanMarketAdapter
from brutex.adapters.dhan.client import DhanClient
from brutex.adapters.dhan.errors import DhanBadRequestError
from brutex.adapters.dhan.ingest import IngestReport, ingest_spot
from brutex.data.models import CandleBar
from brutex.data.sink import SinkAppendResult
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.observability.correlation import pull_id_scope
from structlog.testing import capture_logs

_CANDLES = {
    "open": [100.0],
    "high": [110.0],
    "low": [90.0],
    "close": [105.0],
    "timestamp": [1609733100],
}


class _Transport:
    def __init__(self, status: int = 200, payload: object = _CANDLES) -> None:
        self.status = status
        self.payload = payload

    def __call__(
        self, url: str, *, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        return self.status, json.dumps(self.payload).encode()


class _Sink:
    def __init__(self) -> None:
        self.appends: list[int] = []

    def append(
        self, *, exchange: str, segment: str, symbol: str, interval: str, bars: Sequence[CandleBar]
    ) -> SinkAppendResult:
        del exchange, segment, symbol, interval
        self.appends.append(len(bars))
        return SinkAppendResult(bars_appended=len(bars), partitions_written=1, write_results=())


_INST = Instrument(
    exch_id=Exchange.NSE,
    security_id=13,
    symbol_name="NIFTY",
    display_name="NIFTY",
    instrument=InstrumentType.IDX,
    segment=Segment.CASH,
    series=None,
    isin=None,
    underlying_symbol=None,
    underlying_security_id=None,
    lot_size=0,
    tick_size=Decimal("0"),
    sm_freeze_qty=None,
    sm_expiry_date=None,
    strike_price=None,
    asm_gsm_flag=False,
)


def _adapter(status: int = 200, payload: object = _CANDLES) -> DhanMarketAdapter:
    client = DhanClient(
        access_token="t",
        client_id="c",
        transport=_Transport(status, payload),  # type: ignore[arg-type]
        base_url="https://api.dhan.co/v2/",
    )
    return DhanMarketAdapter(client=client, instruments=(_INST,))


class TestIngestSpot:
    def test_daily_single_window(self) -> None:
        sink = _Sink()
        report = ingest_spot(
            _adapter(),
            sink,
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
        )
        assert report == IngestReport(windows=1, bars_appended=1, transient_failures=0)
        assert sink.appends == [1]

    def test_intraday_tiles_into_90_day_windows(self) -> None:
        sink = _Sink()
        report = ingest_spot(
            _adapter(),
            sink,
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),  # ~212 days → 3 windows
        )
        assert report.windows == 3
        assert report.bars_appended == 3
        assert sink.appends == [1, 1, 1]

    def test_pace_called_once_per_window(self) -> None:
        calls = {"n": 0}

        def _pace() -> None:
            calls["n"] += 1

        ingest_spot(
            _adapter(),
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            pace=_pace,
        )
        assert calls["n"] == 3

    def test_giftnifty_keeps_out_of_session_bars(self) -> None:
        # GIFT Nifty (NSE IX, ~21h) is EXEMPT from the 09:15-15:30 filter, so an
        # out-of-session bar (17:30 IST = 12:00 UTC) is KEPT (DEC-DHAN-GIFTNIFTY-SESSION-001).
        out_of_session = {**_CANDLES, "timestamp": [1609761600]}  # 2021-01-04 17:30 IST
        gift = Instrument(
            exch_id=Exchange.NSE,
            security_id=37,
            symbol_name="GIFTNIFTY",
            display_name="GIFT Nifty",
            instrument=InstrumentType.IDX,
            segment=Segment.CASH,
            series=None,
            isin=None,
            underlying_symbol=None,
            underlying_security_id=None,
            lot_size=0,
            tick_size=Decimal("0"),
            sm_freeze_qty=None,
            sm_expiry_date=None,
            strike_price=None,
            asm_gsm_flag=False,
        )
        client = DhanClient(
            access_token="t",
            client_id="c",
            transport=_Transport(payload=out_of_session),  # type: ignore[arg-type]
            base_url="https://api.dhan.co/v2/",
        )
        adapter = DhanMarketAdapter(client=client, instruments=(gift,))
        sink = _Sink()
        report = ingest_spot(
            adapter,
            sink,
            instrument=gift,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 1, 2),  # 1 window
        )
        assert report.bars_appended == 1  # the out-of-session bar is KEPT
        assert sink.appends == [1]

    def test_non_exempt_drops_out_of_session_bars(self) -> None:
        # A normal symbol (NIFTY) still has out-of-session bars dropped.
        out_of_session = {**_CANDLES, "timestamp": [1609761600]}  # 17:30 IST
        sink = _Sink()
        report = ingest_spot(
            _adapter(payload=out_of_session),
            sink,
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 1, 2),
        )
        assert report.bars_appended == 0  # dropped by the session filter
        assert sink.appends == [0]

    def test_transient_error_counted_and_skipped(self) -> None:
        sink = _Sink()
        report = ingest_spot(
            _adapter(status=429),
            sink,
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
        )
        assert report.windows == 3
        assert report.bars_appended == 0
        assert report.transient_failures == 3
        assert sink.appends == []

    def test_intraday_drops_out_of_session_bars(self) -> None:
        # Two bars: 03:30 UTC = 09:00 IST (pre-open, dropped) + 04:05 UTC =
        # 09:35 IST (in-session, kept). Single 1-day-ish range → 1 window.
        payload = {
            "open": [100.0, 101.0],
            "high": [110.0, 111.0],
            "low": [90.0, 91.0],
            "close": [105.0, 106.0],
            "timestamp": [1609731000, 1609733100],  # 03:30 UTC, 04:05 UTC
        }
        sink = _Sink()
        report = ingest_spot(
            _adapter(payload=payload),
            sink,
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 3, 1),  # 1 window
        )
        assert report.windows == 1
        assert report.bars_appended == 1  # pre-open bar filtered out
        assert sink.appends == [1]

    def test_terminal_error_propagates(self) -> None:
        with pytest.raises(DhanBadRequestError):
            ingest_spot(
                _adapter(status=400),
                _Sink(),
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 2, 1),
            )


class TestIngestSpotCoverage:
    """The fetch-coverage watermark — skip already-pulled windows (DEC-FETCH-COVERAGE-001)."""

    def test_coverage_marks_then_skips_on_repull(self) -> None:
        from brutex.data.fetch_coverage import FetchCoverage

        cov = FetchCoverage()
        # First pull (empty coverage) → fetches all 3 windows + marks them.
        first = ingest_spot(
            _adapter(),
            _Sink(),
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),  # ~212 days → 3 windows
            coverage=cov,
        )
        assert first.windows == 3
        assert first.bars_appended == 3
        assert first.skipped_windows == 0
        assert cov.completed_count == 3

        # Re-pull with the SAME coverage → every window is skipped before the
        # vendor call: no fetch, no append, skipped_windows == 3.
        sink2 = _Sink()
        second = ingest_spot(
            _adapter(),
            sink2,
            instrument=_INST,
            interval="1minute",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            coverage=cov,
        )
        assert second.windows == 3
        assert second.skipped_windows == 3
        assert second.bars_appended == 0
        assert sink2.appends == []  # nothing re-fetched / re-written


class TestIngestSpotRateFeedback:
    """The rate-controller feedback hooks (DEC-DHAN-ADAPTIVE-RPS-001)."""

    def test_on_rate_limit_fires_on_429(self) -> None:
        hits: list[int] = []
        ok: list[int] = []
        report = ingest_spot(
            _adapter(status=429),  # DhanRateLimitError — retryable + is_rate_limit
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            on_rate_limit=lambda: hits.append(1),
            on_success=lambda: ok.append(1),
        )
        assert report.transient_failures == 1
        assert hits == [1]  # the 429 stepped the controller
        assert ok == []  # no clean window

    def test_on_rate_limit_not_fired_on_non_ratelimit_retryable(self) -> None:
        hits: list[int] = []
        ingest_spot(
            _adapter(status=500),  # DhanServerError — retryable but NOT rate-limit
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            on_rate_limit=lambda: hits.append(1),
        )
        assert hits == []  # a 5xx is transient but not a rate-limit signal

    def test_on_success_fires_on_clean_window(self) -> None:
        ok: list[int] = []
        ingest_spot(
            _adapter(),  # clean 200
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            on_success=lambda: ok.append(1),
        )
        assert ok == [1]  # one clean window → one recovery signal


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    """All captured events with ``event == name`` (in emission order)."""
    return [e for e in logs if e["event"] == name]


class TestIngestSpotLogging:
    """The spot spine must be VISIBLE — every window / parse / lake-write / series.

    DEC-DHAN-INGEST-LOGGING-001: each ``ingest.*`` event carries the pull's
    ``pull_id`` (bound by the controller's ``pull_id_scope``) so an operator can
    grep one pull end-to-end. Observability only — the IngestReport is unchanged.
    """

    def test_fetched_window_lights_window_parse_lakewrite_series(self) -> None:
        sink = _Sink()
        with capture_logs() as logs, pull_id_scope("spotpull0001"):
            report = ingest_spot(
                _adapter(),
                sink,
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 8, 1),
            )
        assert report == IngestReport(windows=1, bars_appended=1, transient_failures=0)
        # ── ingest.window (fetched) ──
        (window,) = _events(logs, "ingest.window")
        assert window["log_level"] == "debug"
        assert window["pull_id"] == "spotpull0001"
        assert window["symbol"] == "NIFTY"
        assert window["interval"] == "1day"
        assert window["window_start"] == "2021-01-01"
        assert window["window_end"] == "2021-08-01"
        assert window["action"] == "fetched"
        # ── ingest.parse ── DEBUG, carries the (symbol, interval) coordinate.
        (parse,) = _events(logs, "ingest.parse")
        assert parse["log_level"] == "debug"
        assert parse["pull_id"] == "spotpull0001"
        assert parse["symbol"] == "NIFTY"
        assert parse["interval"] == "1day"
        assert parse["raw_bars"] == 1
        assert parse["session_bars"] == 1
        # ── ingest.lake_write ── DEBUG, full coordinate.
        (write,) = _events(logs, "ingest.lake_write")
        assert write["log_level"] == "debug"
        assert write["pull_id"] == "spotpull0001"
        assert write["symbol"] == "NIFTY"
        assert write["interval"] == "1day"
        assert write["bars_appended"] == 1
        # ── ingest.series ── DEBUG.
        (series,) = _events(logs, "ingest.series")
        assert series["log_level"] == "debug"
        assert series["pull_id"] == "spotpull0001"
        assert series["symbol"] == "NIFTY"
        assert series["interval"] == "1day"
        assert series["windows"] == 1
        assert series["bars_appended"] == 1
        assert series["transient_failures"] == 0
        assert series["skipped_windows"] == 0

    def test_parse_event_reports_session_drop(self) -> None:
        # Two bars: one pre-open (dropped), one in-session (kept) — raw 2, session 1.
        payload = {
            "open": [100.0, 101.0],
            "high": [110.0, 111.0],
            "low": [90.0, 91.0],
            "close": [105.0, 106.0],
            "timestamp": [1609731000, 1609733100],  # 03:30 UTC (pre-open), 04:05 UTC (in)
        }
        with capture_logs() as logs, pull_id_scope("spotpull0002"):
            ingest_spot(
                _adapter(payload=payload),
                _Sink(),
                instrument=_INST,
                interval="1minute",
                start=date(2021, 1, 1),
                end=date(2021, 3, 1),  # 1 window
            )
        (parse,) = _events(logs, "ingest.parse")
        assert parse["pull_id"] == "spotpull0002"
        assert parse["raw_bars"] == 2  # both vendor bars decoded
        assert parse["session_bars"] == 1  # only the in-session bar survived
        # The lake-write reflects the post-filter count.
        (write,) = _events(logs, "ingest.lake_write")
        assert write["pull_id"] == "spotpull0002"
        assert write["bars_appended"] == 1

    def test_skipped_window_lights_only_the_skip_event(self) -> None:
        from brutex.data.fetch_coverage import FetchCoverage

        cov = FetchCoverage()
        # First pull marks the single window complete (not asserted here).
        ingest_spot(
            _adapter(),
            _Sink(),
            instrument=_INST,
            interval="1day",
            start=date(2021, 1, 1),
            end=date(2021, 8, 1),
            coverage=cov,
        )
        # Re-pull → the window is skipped before any vendor call.
        with capture_logs() as logs, pull_id_scope("spotpull0003"):
            report = ingest_spot(
                _adapter(),
                _Sink(),
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 8, 1),
                coverage=cov,
            )
        assert report.skipped_windows == 1
        (window,) = _events(logs, "ingest.window")
        assert window["action"] == "skipped"
        assert window["pull_id"] == "spotpull0003"
        assert window["window_start"] == "2021-01-01"
        # A skipped window fetches nothing → no parse / lake-write events.
        assert _events(logs, "ingest.parse") == []
        assert _events(logs, "ingest.lake_write") == []
        # The series summary still fires, recording the skip.
        (series,) = _events(logs, "ingest.series")
        assert series["pull_id"] == "spotpull0003"
        assert series["windows"] == 1
        assert series["bars_appended"] == 0
        assert series["transient_failures"] == 0
        assert series["skipped_windows"] == 1

    def test_pull_id_is_empty_string_outside_a_scope(self) -> None:
        # Called with no enclosing scope → events carry a benign pull_id="".
        with capture_logs() as logs:
            ingest_spot(
                _adapter(),
                _Sink(),
                instrument=_INST,
                interval="1day",
                start=date(2021, 1, 1),
                end=date(2021, 8, 1),
            )
        assert _events(logs, "ingest.window")[0]["pull_id"] == ""
