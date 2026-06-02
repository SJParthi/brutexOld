"""Tests for `brutex.adapters.dhan.run_ingest` — multi-instrument fan-out.

100% line+branch via a monkeypatched `ingest_spot`: success fan-out across
instruments × intervals, terminal-error quarantine (run continues), the
aggregate report properties, pace threading, and the empty-universe case.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from brutex.adapters.dhan import run_ingest as mod
from brutex.adapters.dhan.errors import DhanNotFoundError
from brutex.adapters.dhan.ingest import IngestReport
from brutex.adapters.dhan.run_ingest import (
    DhanIngestReport,
    PairFailure,
    run_dhan_spot_ingest,
)
from brutex.models import Exchange, Instrument, InstrumentType, Segment

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
        on_rate_limit=None,
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
        on_rate_limit=None,
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
        on_rate_limit=None,
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
        on_rate_limit=None,
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
        on_rate_limit=None,
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
    """run_dhan_spot_ingest threads on_rate_limit + on_success into every pair."""
    seen: list[tuple[object, object]] = []

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
        on_rate_limit=None,
        on_success=None,
    ):
        seen.append((on_rate_limit, on_success))
        return IngestReport(windows=1, bars_appended=1, transient_failures=0)

    monkeypatch.setattr(mod, "ingest_spot", _fake_ingest)
    rl = object()
    ok = object()
    run_dhan_spot_ingest(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        instruments=[_index("13")],
        intervals=["1minute", "1day"],
        start=_START,
        end=_END,
        on_rate_limit=rl,  # type: ignore[arg-type]
        on_success=ok,  # type: ignore[arg-type]
    )
    # Both pairs got the same hooks.
    assert seen == [(rl, ok), (rl, ok)]


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
