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
        exchange=Exchange.NSE,
        exchange_token=token,
        trading_symbol=f"SYM{symbol}",
        vendor_symbol=symbol,
        name=None,
        instrument_type=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_exchange_token=None,
        lot_size=0,
        tick_size=Decimal("0"),
        freeze_quantity=None,
        expiry_date=None,
        strike_price=None,
        is_reserved=False,
        buy_allowed=True,
        sell_allowed=True,
    )


def test_fan_out_all_pairs_succeed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, pace=None):
        calls.append((instrument.vendor_symbol, interval))
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
    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, pace=None):
        if instrument.vendor_symbol == "BAD":
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

    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, pace=None):
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
    def _fake_ingest(adapter, sink, *, instrument, interval, start, end, pace=None):
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
