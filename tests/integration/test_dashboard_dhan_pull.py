"""Integration tests for the Dhan SPOT pull route (DEC-DHAN-PULL-SURFACE-001, ADR-0022).

End-to-end through FastAPI's :class:`TestClient` with an INJECTED ``dhan_spot_pull``
seam (the ``build_app`` param): a fake pull writes synthetic bars into ``feeds/dhan/``
under a temp lake (dry-run isolation — no real SSM / dhanhq / network). Asserts the
route validates + starts on the SEPARATE ``_dhan_pull_job`` (own single-flight flock,
no Groww collision), is ``market_hours_exempt`` (Dhan uses its own account →
``market_closed=None``), and nests the write tree under ``feeds/dhan/`` (Groww's top
lake untouched).
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")

from brutex.dashboard.app import (
    DashboardState,
    _dhan_feed_root_resolver,
    _dhan_job_lake_root_resolver,
    _job_lake_root_resolver,
    build_app,
)
from brutex.dashboard.dhan_pull import (
    DhanPrebuildOutcome,
    DhanPullReport,
    DhanPullRequest,
    run_dhan_spot_pull,
)
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

app_module = importlib.import_module("brutex.dashboard.app")


def _idx(symbol: str) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=abs(hash(symbol)) % 100_000,
        symbol_name=symbol,
        display_name=symbol,
        instrument=InstrumentType.IDX,
        segment=Segment.CASH,
        series=None,
        isin=None,
        underlying_symbol=None,
        underlying_security_id=None,
        lot_size=1,
        tick_size=Decimal("0.05"),
        sm_freeze_qty=None,
        sm_expiry_date=None,
        strike_price=None,
        asm_gsm_flag=False,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    s = InstrumentStore.open(tmp_path / "instruments.duckdb")
    s.upsert_many(
        [_idx("NIFTY")], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield s
    s.close()


def _bar() -> CandleBar:
    return CandleBar(
        timestamp=datetime(2022, 6, 2, 3, 45, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=0,
        open_interest=None,
    )


class _FakeAdapter:
    def fetch_ohlcv(
        self, *, symbol: str, exchange: Any, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        _ = (symbol, exchange, interval, start, end)
        return [_bar()]


def _no_filter(bars: list[CandleBar]) -> SimpleNamespace:
    return SimpleNamespace(kept=list(bars))


class _FakeDhanPull:
    """An injected ``dhan_spot_pull`` seam that writes a synthetic bar into feeds/dhan/."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.request: DhanPullRequest | None = None

    def __call__(self, request: DhanPullRequest) -> DhanPullReport:
        self.request = request
        spot = run_dhan_spot_pull(
            lake_root=request.lake_root,
            adapter=_FakeAdapter(),
            security_ids={"NIFTY": 13},
            start=request.start,
            end=request.end,
            symbols=["NIFTY"],
            session_filter=_no_filter,
        )
        report = DhanPullReport(
            spot=spot,
            prebuild=DhanPrebuildOutcome(materialized={"NIFTY": 20}, failed={}),
            dry_run=request.dry_run,
        )
        self.done.set()
        return report


@pytest.fixture
def fake_pull() -> _FakeDhanPull:
    return _FakeDhanPull()


@pytest.fixture
def client(store: InstrumentStore, fake_pull: _FakeDhanPull) -> Iterator[TestClient]:
    with TestClient(
        build_app(DashboardState(), instrument_store=store, dhan_spot_pull=fake_pull)
    ) as c:
        yield c


class TestDhanPullRoute:
    def test_dry_run_pulls_into_feeds_dhan(
        self, client: TestClient, fake_pull: _FakeDhanPull, tmp_path: Path
    ) -> None:
        resp = client.post("/api/v1/universe/pull-pipeline-dhan", params={"dry_run": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["feed"] == "dhan"
        assert body["dry_run"] is True
        assert body["started"] is True
        # The background job ran the injected fake pull.
        assert fake_pull.done.wait(timeout=10.0)
        assert fake_pull.request is not None
        assert fake_pull.request.dry_run is True
        assert fake_pull.request.lake_root == tmp_path
        # Bars landed under the NESTED feeds/dhan/ tree — Groww's top lake untouched.
        assert (tmp_path / "feeds" / "dhan" / "bars" / "NSE" / "INDEX" / "NIFTY").exists()
        assert not (tmp_path / "bars").exists()

    def test_market_hours_exempt(
        self,
        store: InstrumentStore,
        fake_pull: _FakeDhanPull,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Spy on BackgroundPullJob.start to capture the Dhan call's kwargs.
        starts: list[dict[str, Any]] = []
        orig_start = app_module.BackgroundPullJob.start

        def _spy(self: Any, work: Any, **kwargs: Any) -> bool:
            starts.append(
                {"detail": kwargs.get("detail"), "exempt": kwargs.get("market_hours_exempt")}
            )
            return bool(orig_start(self, work, **kwargs))

        monkeypatch.setattr(app_module.BackgroundPullJob, "start", _spy)
        with TestClient(
            build_app(DashboardState(), instrument_store=store, dhan_spot_pull=fake_pull)
        ) as c:
            c.post("/api/v1/universe/pull-pipeline-dhan", params={"dry_run": True})
            fake_pull.done.wait(timeout=10.0)
        dhan_starts = [s for s in starts if str(s["detail"]).find("dhan") != -1]
        assert dhan_starts, f"no dhan job start captured: {starts}"
        # Dhan uses its own account → market_closed=None → the start gate is exempt.
        assert dhan_starts[0]["exempt"] is True

    def test_bad_date_range_is_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/universe/pull-pipeline-dhan",
            params={"from_date": "2024-06-30", "to_date": "2024-06-01", "dry_run": True},
        )
        assert resp.status_code == 400

    def test_universe_page_renders_dhan_button(self, client: TestClient) -> None:
        html = client.get("/universe").text
        assert 'id="pullDhanBtn"' in html
        assert "pull-pipeline-dhan" in html
        assert "feeds/dhan/" in html


class TestDhanJobIsolation:
    def test_dhan_job_flock_root_distinct_from_groww(self, store: InstrumentStore) -> None:
        # The Dhan job flocks feeds/dhan/, Groww flocks the top root → no collision.
        top = _job_lake_root_resolver(store)()
        dhan = _dhan_job_lake_root_resolver(store)()
        assert dhan is not None and top is not None
        assert dhan == top / "feeds" / "dhan"
        assert dhan != top

    def test_dhan_feed_root_resolver_nests_and_propagates_none(self) -> None:
        # Both arms of the pure nesting wrapper.
        assert _dhan_feed_root_resolver(lambda: Path("/lake"))() == Path("/lake/feeds/dhan")
        assert _dhan_feed_root_resolver(lambda: None)() is None
