"""Tests for `brutex.adapters.dhan.run_ingest` — multi-instrument fan-out.

100% line+branch via a monkeypatched `ingest_spot`: success fan-out across
instruments × intervals, terminal-error quarantine (run continues), the
aggregate report properties, pace threading, and the empty-universe case.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

import pytest
from brutex.adapters.dhan import run_ingest as mod
from brutex.adapters.dhan.errors import DhanBadRequestError, DhanNotFoundError
from brutex.adapters.dhan.ingest import IngestReport
from brutex.adapters.dhan.run_ingest import (
    DhanIngestReport,
    PairFailure,
    run_dhan_spot_ingest,
)
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.observability.correlation import pull_id_scope
from structlog.testing import capture_logs

_START = date(2021, 1, 1)
_END = date(2021, 2, 1)


def _index(symbol: str, *, token: int = 1) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=token,
        symbol_name=symbol,
        display_name=None,
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


def test_fan_out_all_pairs_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def _fake_ingest(
        adapter,
        sink,
        *,
        instrument,
        interval,
        start,
        end,
        pace=None,
        coverage=None,
        status_log=None,
        on_rate_limit=None,
        on_timeout=None,
        on_success=None,
    ):
        calls.append((instrument.symbol_name, interval))
        return IngestReport(windows=2, bars_appended=10, transient_failures=1)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    report = run_dhan_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("13"), _index("25")],
        intervals=["1minute", "1day"],
        start=_START,
        end=_END,
    )
    # 2 instruments × 2 intervals = 4 pairs, all succeeded.
    assert len(report.pairs) == 4
    assert report.failed_pairs == 0
    assert report.windows == 8  # 4 pairs × 2 windows
    assert report.bars_appended == 40
    assert report.transient_failures == 4
    assert set(calls) == {
        ("13", "1minute"),
        ("13", "1day"),
        ("25", "1minute"),
        ("25", "1day"),
    }


def test_terminal_error_quarantines_pair_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_ingest(
        adapter,
        sink,
        *,
        instrument,
        interval,
        start,
        end,
        pace=None,
        coverage=None,
        status_log=None,
        on_rate_limit=None,
        on_timeout=None,
        on_success=None,
    ):
        if instrument.symbol_name == "BAD":
            raise DhanNotFoundError("HTTP 404 unknown security")
        return IngestReport(windows=1, bars_appended=5, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    report = run_dhan_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("13"), _index("BAD", token=99)],
        intervals=["1minute"],
        start=_START,
        end=_END,
    )
    assert report.failed_pairs == 1
    assert report.failures == (
        PairFailure(symbol="BAD", interval="1minute", error="HTTP 404 unknown security"),
    )
    # The good pair still ran + aggregated.
    assert ("13", "1minute") in report.pairs
    assert report.bars_appended == 5


def test_pace_threaded_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []

    def _fake_ingest(
        adapter,
        sink,
        *,
        instrument,
        interval,
        start,
        end,
        pace=None,
        coverage=None,
        status_log=None,
        on_rate_limit=None,
        on_timeout=None,
        on_success=None,
    ):
        seen.append(pace)
        return IngestReport(windows=0, bars_appended=0, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    sentinel = object()
    run_dhan_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("13")],
        intervals=["1minute"],
        start=_START,
        end=_END,
        pace=sentinel,  # type: ignore[arg-type]
    )
    assert seen == [sentinel]


def test_with_paced_client_injects_pace_into_live_dhan_adapter() -> None:
    """`_with_paced_client` wires `pace` into a real DhanMarketAdapter's client.

    Rate ≤5 GUARANTEE (DEC-DHAN-RATE-REALTIME-GUARANTEE-001): covers all three
    branches — pace None (unchanged), non-Dhan adapter (unchanged), and the live
    DhanMarketAdapter (client rebuilt with the bucket `pace`).
    """
    from brutex.adapters.dhan.adapter import DhanMarketAdapter
    from brutex.adapters.dhan.client import DhanClient

    def _transport(
        _url: str, *, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        return 200, b"{}"

    def _pace() -> None: ...

    client = DhanClient(access_token="t", client_id="c", transport=_transport)
    adapter = DhanMarketAdapter(client=client, instruments=())

    paced = mod._with_paced_client(adapter, _pace)
    assert paced is not adapter
    assert isinstance(paced, DhanMarketAdapter) and paced.client.pace is _pace

    assert mod._with_paced_client(adapter, None) is adapter  # pace None → unchanged
    sentinel = object()
    assert mod._with_paced_client(sentinel, _pace) is sentinel  # non-Dhan → unchanged


def test_non_dhan_exception_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only DhanError is quarantined; a genuine bug (e.g. ValueError) must NOT
    # be swallowed — it propagates so the orchestrator never hides a defect.
    def _fake_ingest(
        adapter,
        sink,
        *,
        instrument,
        interval,
        start,
        end,
        pace=None,
        coverage=None,
        status_log=None,
        on_rate_limit=None,
        on_timeout=None,
        on_success=None,
    ):
        raise ValueError("a real bug, not a vendor error")

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    with pytest.raises(ValueError, match="a real bug"):
        run_dhan_spot_ingest(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("13")],
            intervals=["1minute"],
            start=_START,
            end=_END,
        )


def test_empty_universe_yields_empty_report() -> None:
    report = run_dhan_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[],
        intervals=["1minute"],
        start=_START,
        end=_END,
    )
    assert report == DhanIngestReport(pairs={}, failures=())
    assert report.windows == 0
    assert report.bars_appended == 0
    assert report.transient_failures == 0
    assert report.skipped_windows == 0
    assert report.retried_windows == 0
    assert report.still_failed_windows == 0
    assert report.failed_pairs == 0


def test_coverage_threaded_and_skipped_windows_aggregated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_dhan_spot_ingest threads the coverage ledger + sums skipped_windows."""
    from brutex.data.fetch_coverage import FetchCoverage

    seen_coverage: list[object] = []

    def _fake_ingest(
        adapter,
        sink,
        *,
        instrument,
        interval,
        start,
        end,
        pace=None,
        coverage=None,
        status_log=None,
        on_rate_limit=None,
        on_timeout=None,
        on_success=None,
    ):
        seen_coverage.append(coverage)
        return IngestReport(windows=3, bars_appended=0, transient_failures=0, skipped_windows=3)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    cov = FetchCoverage()
    report = run_dhan_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("13")],
        intervals=["1minute", "1day"],
        start=_START,
        end=_END,
        coverage=cov,
    )
    # The same coverage ledger was threaded into every pair.
    assert seen_coverage == [cov, cov]
    # 2 pairs × 3 skipped windows each.
    assert report.skipped_windows == 6
    assert report.bars_appended == 0


def test_rate_feedback_hooks_threaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_dhan_spot_ingest threads on_rate_limit + on_timeout + on_success into every pair."""
    seen: list[tuple[object, object, object]] = []

    def _fake_ingest(
        adapter,
        sink,
        *,
        instrument,
        interval,
        start,
        end,
        pace=None,
        coverage=None,
        status_log=None,
        on_rate_limit=None,
        on_timeout=None,
        on_success=None,
    ):
        seen.append((on_rate_limit, on_timeout, on_success))
        return IngestReport(windows=1, bars_appended=1, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    rl = object()
    to = object()
    ok = object()
    run_dhan_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("13")],
        intervals=["1minute", "1day"],
        start=_START,
        end=_END,
        on_rate_limit=rl,  # type: ignore[arg-type]
        on_timeout=to,  # type: ignore[arg-type]
        on_success=ok,  # type: ignore[arg-type]
    )
    # Both pairs got the same hooks (now including on_timeout — DEC-DHAN-SPOT-TIMEOUT-BACKOFF-001).
    assert seen == [(rl, to, ok), (rl, to, ok)]


def test_on_progress_fires_per_pair_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """on_progress fires once per pair: (label, bars, failed) — both branches."""

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
        if instrument.symbol_name == "BAD":
            raise DhanNotFoundError("DH-902 gone")
        return IngestReport(windows=1, bars_appended=7, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    events: list[tuple[str, int, bool]] = []
    run_dhan_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("13"), _index("BAD")],
        intervals=["1minute"],
        start=_START,
        end=_END,
        on_progress=lambda label, bars, failed: events.append((label, bars, failed)),
    )
    assert events == [("13 1minute", 7, False), ("BAD 1minute", 0, True)]


def _events(logs: Sequence[Mapping[str, object]], name: str) -> list[Mapping[str, object]]:
    """All captured events with ``event == name`` (in emission order)."""
    return [e for e in logs if e["event"] == name]


class TestRunIngestLogging:
    """The spot fan-out must roll up per-instrument (INFO) + name quarantine causes.

    DEC-DHAN-INGEST-LOGGING-001: ``ingest.underlying`` (INFO, one per instrument)
    + ``ingest.quarantine`` (WARNING, one per terminal failure), both carrying the
    pull's ``pull_id``. Observability only — the aggregate report is unchanged.
    """

    def test_underlying_rollup_per_instrument(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
            return IngestReport(windows=2, bars_appended=10, transient_failures=0)

        monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
        with capture_logs() as logs, pull_id_scope("runpull00001"):
            run_dhan_spot_ingest(
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                instruments=[_index("13"), _index("25")],
                intervals=["1minute", "1day"],
                start=_START,
                end=_END,
            )
        rollups = _events(logs, "ingest.underlying")
        assert len(rollups) == 2  # one per instrument
        first = rollups[0]
        assert first["log_level"] == "info"
        assert first["pull_id"] == "runpull00001"
        assert first["symbol"] == "13"
        assert first["series_completed"] == 2  # both intervals succeeded
        assert first["series_failed"] == 0
        assert first["bars_appended"] == 20  # 2 intervals × 10
        assert first["windows"] == 4  # 2 intervals × 2 windows
        assert _events(logs, "ingest.quarantine") == []

    def test_quarantine_event_names_the_cause(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
            if instrument.symbol_name == "BAD":
                raise DhanNotFoundError("HTTP 404 unknown security")
            return IngestReport(windows=1, bars_appended=5, transient_failures=0)

        monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
        with capture_logs() as logs, pull_id_scope("runpull00002"):
            run_dhan_spot_ingest(
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                instruments=[_index("13"), _index("BAD", token=99)],
                intervals=["1minute"],
                start=_START,
                end=_END,
            )
        (quarantine,) = _events(logs, "ingest.quarantine")
        assert quarantine["log_level"] == "warning"
        assert quarantine["pull_id"] == "runpull00002"
        assert quarantine["symbol"] == "BAD"
        assert quarantine["interval"] == "1minute"
        assert quarantine["error"] == "HTTP 404 unknown security"
        # The failed instrument's rollup records the quarantine.
        bad_rollup = [e for e in _events(logs, "ingest.underlying") if e["symbol"] == "BAD"]
        assert bad_rollup[0]["series_failed"] == 1
        assert bad_rollup[0]["series_completed"] == 0

    def test_no_data_logs_known_gap_not_quarantine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A Dhan DH-905 "no data" 400 (e.g. NIFTY NEXT 50 daily) is a KNOWN_GAP, not
        # a scary quarantine: it logs ``ingest.known_gap`` (INFO), NOT
        # ``ingest.quarantine`` (DEC-DHAN-NO-DATA-KNOWN-GAP-001). The pair is still
        # isolated + re-tried next pull — data is never silently masked.
        def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
            if instrument.symbol_name == "GAP":
                raise DhanBadRequestError("HTTP 400 bad request: DH-905 no data present")
            return IngestReport(windows=1, bars_appended=5, transient_failures=0)

        monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
        with capture_logs() as logs, pull_id_scope("runpull00003"):
            run_dhan_spot_ingest(
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                instruments=[_index("13"), _index("GAP", token=98)],
                intervals=["1day"],
                start=_START,
                end=_END,
            )
        assert _events(logs, "ingest.quarantine") == []  # NOT an alarming quarantine
        (gap,) = _events(logs, "ingest.known_gap")
        assert gap["log_level"] == "info"
        assert gap["pull_id"] == "runpull00003"
        assert gap["symbol"] == "GAP"
        assert gap["interval"] == "1day"
        assert "DH-905" in str(gap["reason"])
        # Still isolated + re-tried: the rollup records the failure (not dropped).
        gap_rollup = [e for e in _events(logs, "ingest.underlying") if e["symbol"] == "GAP"]
        assert gap_rollup[0]["series_failed"] == 1
        assert gap_rollup[0]["series_completed"] == 0

    def test_events_carry_empty_pull_id_outside_a_scope(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Called with no enclosing scope → the rollup carries a benign pull_id="".
        def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
            return IngestReport(windows=1, bars_appended=1, transient_failures=0)

        monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
        with capture_logs() as logs:
            run_dhan_spot_ingest(
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                instruments=[_index("13")],
                intervals=["1minute"],
                start=_START,
                end=_END,
            )
        assert _events(logs, "ingest.underlying")[0]["pull_id"] == ""


def test_status_log_threaded_through_to_ingest_spot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's ``status_log`` reaches ``ingest_spot`` unchanged (DEC-WINDOW-STATUS-001).

    Guards the wiring: ``run_dhan_spot_ingest`` must pass the SAME log object it
    is handed into every per-pair ``ingest_spot`` call, so the per-window SUCCESS
    / EMPTY / FAILED records all land in the one durable log.
    """
    from brutex.data.window_status import WindowStatusLog

    seen: list[object] = []

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **kw):  # type: ignore[no-untyped-def]
        seen.append(kw.get("status_log"))
        return IngestReport(windows=1, bars_appended=1, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    log = WindowStatusLog()
    run_dhan_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("13"), _index("25")],
        intervals=["1minute", "1day"],
        start=_START,
        end=_END,
        status_log=log,
    )
    # Every pair (2 instruments × 2 intervals) saw the exact same log object.
    assert seen == [log, log, log, log]
