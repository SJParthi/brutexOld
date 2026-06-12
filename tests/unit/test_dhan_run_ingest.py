"""Tests for `brutex.adapters.dhan.run_ingest` — multi-instrument fan-out.

100% line+branch via a monkeypatched `ingest_spot`: success fan-out across
instruments × intervals, terminal-error quarantine (run continues), the
aggregate report properties, pace threading, and the empty-universe case.
"""

# Whitebox test of the module-private `_with_paced_client` (sanctioned per pyproject.toml).
# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

import pytest
from brutex.adapters.dhan import run_ingest as mod
from brutex.adapters.dhan.errors import (
    DhanAuthError,
    DhanBadRequestError,
    DhanNotFoundError,
)
from brutex.adapters.dhan.ingest import IngestReport
from brutex.adapters.dhan.run_ingest import (
    DhanIngestReport,
    PairFailure,
    SpotIngestWithRetry,
    run_dhan_spot_ingest,
    run_dhan_spot_ingest_with_retry,
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
        status_log=None,
        ledger=None,
        on_rate_limit=None,
        on_timeout=None,
        on_success=None,
        auth_guard=None,
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
        status_log=None,
        ledger=None,
        on_rate_limit=None,
        on_timeout=None,
        on_success=None,
        auth_guard=None,
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
        status_log=None,
        ledger=None,
        on_rate_limit=None,
        on_timeout=None,
        on_success=None,
        auth_guard=None,
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
        url: str,
        *,
        body: bytes,
        headers: Mapping[str, str],
        timeout: float,
        method: str = "POST",
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
    # non-Dhan adapter → returned unchanged
    assert mod._with_paced_client(sentinel, _pace) is sentinel  # type: ignore[arg-type]


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
        status_log=None,
        ledger=None,
        on_rate_limit=None,
        on_timeout=None,
        on_success=None,
        auth_guard=None,
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


def test_ledger_threaded_and_skipped_windows_aggregated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_dhan_spot_ingest threads the unified ledger + sums skipped_windows."""
    from brutex.data.window_ledger import WindowLedger

    seen_ledger: list[object] = []

    def _fake_ingest(
        adapter,
        sink,
        *,
        instrument,
        interval,
        start,
        end,
        pace=None,
        status_log=None,
        ledger=None,
        on_rate_limit=None,
        on_timeout=None,
        on_success=None,
        auth_guard=None,
    ):
        seen_ledger.append(ledger)
        return IngestReport(windows=3, bars_appended=0, transient_failures=0, skipped_windows=3)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    led = WindowLedger()
    report = run_dhan_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("13")],
        intervals=["1minute", "1day"],
        start=_START,
        end=_END,
        ledger=led,
    )
    # The same ledger was threaded into every pair.
    assert seen_ledger == [led, led]
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
        status_log=None,
        ledger=None,
        on_rate_limit=None,
        on_timeout=None,
        on_success=None,
        auth_guard=None,
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


def test_on_progress_fires_per_pair_success_failure_and_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_progress fires once per pair: (label, bars, failed, gap) — all branches.

    A clean pair → (bars, False, False); a real terminal error → (0, True, False);
    a benign DH-905 no-data boundary → (0, False, True) — NOT a failure
    (DEC-DHAN-NO-DATA-GAP-COUNT-001).
    """

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **_kw):
        if instrument.symbol_name == "BAD":
            raise DhanNotFoundError("DH-902 gone")
        if instrument.symbol_name == "GAP":
            raise DhanBadRequestError("HTTP 400 bad request: DH-905 no data present")
        return IngestReport(windows=1, bars_appended=7, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    events: list[tuple[str, int, bool, bool]] = []
    run_dhan_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("13"), _index("BAD"), _index("GAP")],
        intervals=["1minute"],
        start=_START,
        end=_END,
        on_progress=lambda label, bars, failed, gap: events.append((label, bars, failed, gap)),
    )
    # The fan-out is parallel → the tick ORDER is not deterministic; assert the SET.
    assert set(events) == {
        ("13 1minute", 7, False, False),
        ("BAD 1minute", 0, True, False),
        ("GAP 1minute", 0, False, True),
    }


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


class TestAuthFailFast:
    """The dead-token fail-fast flood control (DEC-DHAN-AUTH-FAILFAST-RENEW-001).

    A 401 (or DH-906 400) from one pair must: trip the shared guard, set
    ``auth_aborted`` + ``auth_cause`` on the report, emit EXACTLY ONE loud
    ``ingest.auth_aborted`` WARNING (not the per-symbol flood), and STILL record
    every failed pair (re-tried next pull, never masked). Non-auth terminal errors
    keep quarantining per-pair as before (no regression).
    """

    def test_auth_guard_threaded_into_every_pair(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The ONE per-run AuthGuard reaches every ingest_spot pair.
        seen: list[object] = []

        def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **kw):  # type: ignore[no-untyped-def]
            seen.append(kw.get("auth_guard"))
            return IngestReport(windows=1, bars_appended=1, transient_failures=0)

        monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
        run_dhan_spot_ingest(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("13"), _index("25")],
            intervals=["1minute", "1day"],
            start=_START,
            end=_END,
        )
        # 4 pairs, all the same (non-None) guard instance.
        assert len(seen) == 4
        assert all(g is seen[0] for g in seen)
        assert seen[0] is not None

    def test_auth_failure_sets_report_fields_and_alerts_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Every pair's ingest_spot raises an auth error (simulating the token dying
        # mid-pull: a window worker normally trips the guard + re-raises). The outer
        # loop must record every pair, set auth_aborted/auth_cause, and emit exactly
        # ONE ingest.auth_aborted alert — never a per-symbol flood.
        def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **kw):  # type: ignore[no-untyped-def]
            raise DhanAuthError(
                f"HTTP 401 auth failure: DH-901 token expired ({instrument.symbol_name})"
            )

        monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
        with capture_logs() as logs, pull_id_scope("authpull0001"):
            report = run_dhan_spot_ingest(
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                instruments=[_index("13"), _index("25"), _index("99", token=99)],
                intervals=["1minute"],
                start=_START,
                end=_END,
            )
        # All three pairs recorded (re-tried next pull, never masked).
        assert report.failed_pairs == 3
        assert report.auth_aborted is True
        assert report.auth_cause is not None
        assert "DH-901" in report.auth_cause
        # EXACTLY ONE loud alert (not one per symbol) — the flood is killed.
        alerts = _events(logs, "ingest.auth_aborted")
        assert len(alerts) == 1
        assert alerts[0]["log_level"] == "warning"
        assert alerts[0]["pull_id"] == "authpull0001"
        assert "web.dhan.co" in str(alerts[0]["note"])
        # No per-symbol quarantine WARNINGs for the auth failures.
        assert _events(logs, "ingest.quarantine") == []

    def test_dh906_400_is_treated_as_auth_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A DH-906 "Invalid Token" 400 masquerading as a bad request is still a
        # dead token → auth_aborted, not a per-pair quarantine.
        def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **kw):  # type: ignore[no-untyped-def]
            raise DhanBadRequestError("HTTP 400 bad request: DH-906 Invalid Token")

        monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
        with capture_logs() as logs:
            report = run_dhan_spot_ingest(
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                instruments=[_index("13")],
                intervals=["1minute"],
                start=_START,
                end=_END,
            )
        assert report.auth_aborted is True
        assert "DH-906" in str(report.auth_cause)
        assert len(_events(logs, "ingest.auth_aborted")) == 1
        assert _events(logs, "ingest.quarantine") == []

    def test_non_auth_terminal_still_quarantines_no_regression(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A plain 404 (not a dead token) keeps the per-pair quarantine + leaves
        # auth_aborted False — the existing behaviour is unchanged.
        def _fake_ingest(adapter, sink, *, instrument, interval, start, end, **kw):  # type: ignore[no-untyped-def]
            if instrument.symbol_name == "BAD":
                raise DhanNotFoundError("HTTP 404 not found: unknown security")
            return IngestReport(windows=1, bars_appended=5, transient_failures=0)

        monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
        with capture_logs() as logs:
            report = run_dhan_spot_ingest(
                object(),  # type: ignore[arg-type]
                object(),  # type: ignore[arg-type]
                instruments=[_index("13"), _index("BAD", token=99)],
                intervals=["1minute"],
                start=_START,
                end=_END,
            )
        assert report.auth_aborted is False
        assert report.auth_cause is None
        assert report.failed_pairs == 1
        assert _events(logs, "ingest.auth_aborted") == []
        # The non-auth terminal still logs its per-pair quarantine WARNING.
        assert len(_events(logs, "ingest.quarantine")) == 1


def _report(
    *,
    still_failed: int = 0,
    known_gap: bool = False,
    auth_aborted: bool = False,
) -> DhanIngestReport:
    """Build a DhanIngestReport with a chosen transient residual / known-gap / auth state.

    ``still_failed`` is surfaced via a single pair's ``IngestReport.still_failed_windows``
    (the report's property sums over pairs); a known-gap adds a ``DH-905`` PairFailure;
    ``auth_aborted`` sets the report flag (a dead token).
    """
    pairs = {
        ("X", "1minute"): IngestReport(
            windows=1,
            bars_appended=0,
            transient_failures=still_failed,
            still_failed_windows=still_failed,
        )
    }
    failures: tuple[PairFailure, ...] = ()
    if known_gap:
        failures = (PairFailure(symbol="X", interval="1day", error="DH-905 no data present"),)
    return DhanIngestReport(pairs=pairs, failures=failures, auth_aborted=auth_aborted)


class TestRunDhanSpotIngestWithRetry:
    """`run_dhan_spot_ingest_with_retry` — post-run auto-retry wrapper (DEC-INGEST-AUTO-RETRY-001)."""

    def _patch_passes(
        self, monkeypatch: pytest.MonkeyPatch, reports: list[DhanIngestReport]
    ) -> list[int]:
        """Patch ``mod.run_dhan_spot_ingest`` to return *reports* in order; return call log."""
        calls: list[int] = []

        def _fake(*_args: object, **_kw: object) -> DhanIngestReport:
            idx = len(calls)
            calls.append(idx)
            return reports[idx]

        monkeypatch.setattr(mod, "run_dhan_spot_ingest", _fake)
        return calls

    def test_clean_pull_does_not_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._patch_passes(monkeypatch, [_report(still_failed=0)])
        result = run_dhan_spot_ingest_with_retry(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("13")],
            intervals=["1minute"],
            start=_START,
            end=_END,
            retry_backoff=lambda _i: None,
        )
        assert calls == [0]  # only the original pass ran
        assert result.retry is None
        assert result.still_failed_windows == 0

    def test_auto_retry_off_keeps_single_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._patch_passes(monkeypatch, [_report(still_failed=7)])
        result = run_dhan_spot_ingest_with_retry(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("13")],
            intervals=["1minute"],
            start=_START,
            end=_END,
            auto_retry=False,
            retry_backoff=lambda _i: None,
        )
        assert calls == [0]  # the residual is NOT retried when auto_retry is off
        assert result.retry is None
        assert result.still_failed_windows == 7  # falls back to the original report

    def test_residual_triggers_bounded_rounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._patch_passes(
            monkeypatch,
            [_report(still_failed=84), _report(still_failed=10), _report(still_failed=0)],
        )
        result = run_dhan_spot_ingest_with_retry(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("13")],
            intervals=["1minute"],
            start=_START,
            end=_END,
            max_retry_rounds=5,
            retry_backoff=lambda _i: None,
        )
        assert calls == [0, 1, 2]  # original + 2 retry rounds (cleared)
        assert result.retry is not None
        assert result.retry.initial_still_failed == 10  # the FIRST retry round's residual
        assert result.retry.final_still_failed == 0
        assert result.retry.clean is True
        assert result.still_failed_windows == 0

    def test_known_gap_residual_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A pull whose only leftover is a known-gap (still_failed=0) is NOT retried.
        calls = self._patch_passes(monkeypatch, [_report(still_failed=0, known_gap=True)])
        result = run_dhan_spot_ingest_with_retry(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("13")],
            intervals=["1minute"],
            start=_START,
            end=_END,
            retry_backoff=lambda _i: None,
        )
        assert calls == [0]
        assert result.retry is None

    def test_retry_round_classifies_known_gap_via_injected_predicate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The original pull leaves a TRANSIENT residual (so a retry round runs); the
        # retry round's report carries a DH-905 PairFailure. The Dhan known-gap
        # predicate injected into the shared engine classifies it, so the round's
        # ``known_gaps`` count is 1 while the transient residual still drives the loop.
        calls = self._patch_passes(
            monkeypatch,
            [
                _report(still_failed=5),  # original: transient residual ⇒ retry runs
                _report(still_failed=0, known_gap=True),  # retry round: a DH-905 known-gap
            ],
        )
        result = run_dhan_spot_ingest_with_retry(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("13")],
            intervals=["1minute"],
            start=_START,
            end=_END,
            max_retry_rounds=3,
            retry_backoff=lambda _i: None,
        )
        assert calls == [0, 1]  # original + one retry round
        assert result.retry is not None
        assert result.retry.rounds[0].known_gaps == 1  # the DH-905 was classified
        assert result.retry.final_still_failed == 0
        assert result.retry.clean is True

    def test_still_failed_windows_property_prefers_retry(self) -> None:
        # Direct unit of the SpotIngestWithRetry.still_failed_windows property:
        # when a retry ran, the final residual wins over the original.
        from brutex.adapters.ingest_common import RetryOutcome

        original = _report(still_failed=84)
        retry = RetryOutcome(
            initial_still_failed=84,
            final_still_failed=3,
            rounds_run=3,
            rounds=(),
            clean=False,
            auth_aborted=False,
        )
        wrapped = SpotIngestWithRetry(report=original, retry=retry)
        assert wrapped.still_failed_windows == 3
        # ...and with no retry it falls back to the original.
        assert SpotIngestWithRetry(report=original, retry=None).still_failed_windows == 84


def test_audit_chain_records_run_and_pairs(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """DEC-INGEST-AUDIT-CHAIN-001: when an AuditWriter is wired, the spot pull
    appends a hash-chained RUN_START → per-pair → RUN_COMPLETE trail (Golden
    Rule #8). A success pair emits INGEST_PAIR_DONE; a terminal Dhan failure
    emits INGEST_PAIR_FAILED with its classification."""
    from brutex.orchestrator.audit import AuditWriter, read_entries, verify_chain

    def _fake_ingest(
        adapter,
        sink,
        *,
        instrument,
        interval,
        start,
        end,
        pace=None,
        status_log=None,
        ledger=None,
        on_rate_limit=None,
        on_timeout=None,
        on_success=None,
        auth_guard=None,
    ):
        if instrument.symbol_name == "FAIL":
            raise DhanBadRequestError("boom")
        return IngestReport(windows=2, bars_appended=10, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    path = tmp_path / "ingest-audit.jsonl"
    with pull_id_scope("pull-xyz"), AuditWriter.open(path) as audit:
        report = run_dhan_spot_ingest(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            instruments=[_index("OK"), _index("FAIL")],
            intervals=["1minute"],
            start=_START,
            end=_END,
            audit=audit,
        )

    assert report.failed_pairs == 1
    # The hash chain is intact and carries exactly the 4 expected actions.
    assert verify_chain(path) == 4
    entries = read_entries(path)
    actions = [e.action for e in entries]
    assert actions[0] == "INGEST_RUN_START"
    assert actions[-1] == "INGEST_RUN_COMPLETE"
    assert "INGEST_PAIR_DONE" in actions
    assert "INGEST_PAIR_FAILED" in actions
    start_entry = entries[0]
    assert start_entry.subject == "pull:pull-xyz"
    assert start_entry.payload["jobs"] == 2
    failed = next(e for e in entries if e.action == "INGEST_PAIR_FAILED")
    assert failed.payload["auth_failure"] is False
    assert failed.payload["known_gap"] is False
    complete = entries[-1]
    assert complete.payload["pairs_ok"] == 1
    assert complete.payload["pairs_failed"] == 1
    assert complete.payload["total_bars"] == 10
