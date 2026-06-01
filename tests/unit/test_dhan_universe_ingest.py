"""Tests for `brutex.adapters.dhan.universe_ingest` — refresh + pull compose.

100% line+branch via monkeypatched phase functions: verifies the two phases
run in order (refresh THEN ingest), the adapter's universe + every param are
threaded through, and the combined report carries both outcomes.
"""

from __future__ import annotations

from datetime import date

from brutex.adapters.dhan import universe_ingest as mod
from brutex.adapters.dhan.instrument_refresh import RefreshResult
from brutex.adapters.dhan.run_ingest import DhanIngestReport, PairFailure
from brutex.adapters.dhan.universe_ingest import (
    UniverseIngestReport,
    run_dhan_universe_ingest,
)

_SNAP = date(2026, 6, 1)
_START = date(2021, 1, 1)
_END = date(2021, 2, 1)


class _Adapter:
    instruments = ("inst-13", "inst-25")  # sentinel universe


def _refresh_result() -> RefreshResult:
    return RefreshResult(
        parsed=5,
        persisted=3,
        legs_dropped=2,
        filtered=1,
        snapshot_date=_SNAP,
        csv_sha256="abc",
        pruned=0,
    )


def _ingest_report() -> DhanIngestReport:
    return DhanIngestReport(
        pairs={}, failures=(PairFailure(symbol="BAD", interval="1minute", error="404"),)
    )


def test_composes_refresh_then_ingest_in_order(monkeypatch) -> None:
    order: list[str] = []
    seen: dict[str, object] = {}

    def _fake_refresh(store, http_get, *, snapshot_date, run_id, url):
        order.append("refresh")
        seen.update(
            store=store, http_get=http_get, snapshot_date=snapshot_date, run_id=run_id, url=url
        )
        return _refresh_result()

    def _fake_ingest(adapter, sink, *, instruments, intervals, start, end, pace=None):
        order.append("ingest")
        seen.update(instruments=instruments, intervals=intervals, start=start, end=end, pace=pace)
        return _ingest_report()

    monkeypatch.setattr(mod, "refresh_instruments", _fake_refresh)
    monkeypatch.setattr(mod, "run_dhan_spot_ingest", _fake_ingest)

    adapter = _Adapter()
    pace = object()
    report = run_dhan_universe_ingest(
        adapter,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        "store-obj",  # type: ignore[arg-type]
        "http-get",  # type: ignore[arg-type]
        snapshot_date=_SNAP,
        run_id="run-1",
        start=_START,
        end=_END,
        intervals=["1minute", "1day"],
        pace=pace,  # type: ignore[arg-type]
        instrument_url="https://images.dhan.co/x.csv",
    )

    # Phase order: refresh BEFORE ingest.
    assert order == ["refresh", "ingest"]
    # The adapter's universe + every param threaded through.
    assert seen["store"] == "store-obj"
    assert seen["http_get"] == "http-get"
    assert seen["url"] == "https://images.dhan.co/x.csv"
    assert seen["run_id"] == "run-1"
    assert seen["instruments"] == ("inst-13", "inst-25")  # adapter.instruments
    assert seen["intervals"] == ["1minute", "1day"]
    assert seen["start"] == _START and seen["end"] == _END
    assert seen["pace"] is pace
    # Combined report carries both outcomes.
    assert report == UniverseIngestReport(refresh=_refresh_result(), ingest=_ingest_report())
    assert report.refresh.persisted == 3
    assert report.ingest.failed_pairs == 1


def test_default_instrument_url_used(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_refresh(store, http_get, *, snapshot_date, run_id, url):
        captured["url"] = url
        return _refresh_result()

    def _fake_ingest(adapter, sink, *, instruments, intervals, start, end, pace=None):
        return _ingest_report()

    monkeypatch.setattr(mod, "refresh_instruments", _fake_refresh)
    monkeypatch.setattr(mod, "run_dhan_spot_ingest", _fake_ingest)

    run_dhan_universe_ingest(
        _Adapter(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        snapshot_date=_SNAP,
        run_id="r",
        start=_START,
        end=_END,
        intervals=["1minute"],
    )
    assert captured["url"] == mod.INSTRUMENT_MASTER_DETAILED_URL
