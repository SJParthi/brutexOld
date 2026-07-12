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
    DhanPullReport,
    DhanPullRequest,
    default_dhan_spot_pull,
)
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.timeframe_materialize import MaterializeResult
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
    """A fetch adapter that COUNTS calls — proves a dry-run issues zero vendor calls."""

    def __init__(self) -> None:
        self.calls = 0

    def fetch_ohlcv(
        self, *, symbol: str, exchange: Any, interval: str, start: date, end: date
    ) -> list[CandleBar]:
        _ = (symbol, exchange, interval, start, end)
        self.calls += 1
        return [_bar()]


def _no_filter(bars: list[CandleBar]) -> SimpleNamespace:
    return SimpleNamespace(kept=list(bars))


class _FakeDhanPull:
    """An injected ``dhan_spot_pull`` seam that DELEGATES to the REAL production
    ``default_dhan_spot_pull`` with a SPY ``adapter_factory`` + a SPY ``materialize``.

    So the route → production → dry-run short-circuit chain runs end-to-end OFFLINE:
    on ``dry_run`` the real seam resolves the securityId map and returns a zero-outcome
    report WITHOUT building an adapter (``adapter_built`` stays ``False``), issuing a
    vendor call (``client.calls`` stays 0), materialising, or writing. No AWS is ever
    touched — the spy ``adapter_factory`` stands in for the real SSM/``dhanhq`` build,
    which is never invoked on the dry-run path.
    """

    def __init__(self) -> None:
        self.done = threading.Event()
        self.request: DhanPullRequest | None = None
        self.adapter_built = False
        self.materialize_calls = 0
        self.client = _FakeAdapter()

    def _spy_factory(self, **kwargs: Any) -> _FakeAdapter:
        _ = kwargs  # security_ids=...
        self.adapter_built = True
        return self.client

    def _spy_materialize(self, **kwargs: Any) -> MaterializeResult:
        _ = kwargs
        self.materialize_calls += 1
        return MaterializeResult(written={}, skipped=())

    def __call__(self, request: DhanPullRequest) -> DhanPullReport:
        self.request = request
        try:
            return default_dhan_spot_pull(
                request,
                adapter_factory=self._spy_factory,
                materialize=self._spy_materialize,
            )
        finally:
            self.done.set()


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
    def test_dry_run_is_offline_and_writes_nothing(
        self, client: TestClient, fake_pull: _FakeDhanPull, tmp_path: Path
    ) -> None:
        """HONEST dry-run: plan-only — NO adapter build, NO vendor call, NO write, NO AWS.

        Drives the REAL ``default_dhan_spot_pull`` seam (via the injected fake's SPY
        ``adapter_factory``), so the route → production → dry-run short-circuit runs
        end-to-end OFFLINE. The credential gate short-circuits on ``dry_run`` and the
        spot job start skips the cross-process guards (``cross_process_lock=False``), so
        the dry-run never resolves an SSM/AWS credential and never fires a vendor call —
        it returns the honest zero-outcome report and writes nothing (Golden Rule #1).
        This SUPERSEDES the stale "dry-run pulls into feeds/dhan/" assertion (a dry-run
        must never write).
        """
        resp = client.post("/api/v1/universe/pull-pipeline-dhan", params={"dry_run": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["feed"] == "dhan"
        assert body["dry_run"] is True
        assert body["started"] is True
        # The background job ran the injected fake, which delegated to the real seam.
        assert fake_pull.done.wait(timeout=10.0)
        assert fake_pull.request is not None
        assert fake_pull.request.dry_run is True
        assert fake_pull.request.lake_root == tmp_path
        # The real seam short-circuited: NO adapter build, NO vendor call, NO materialise.
        assert fake_pull.adapter_built is False
        assert fake_pull.client.calls == 0
        assert fake_pull.materialize_calls == 0
        # A dry-run writes NOTHING — neither the Dhan feed tree nor the Groww top lake.
        assert not (tmp_path / "feeds").exists()
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
                {
                    "detail": kwargs.get("detail"),
                    "exempt": kwargs.get("market_hours_exempt"),
                    "lock": kwargs.get("cross_process_lock"),
                }
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
        # A dry-run start skips the cross-process guards (offline: no AWS lock resolved).
        assert dhan_starts[0]["lock"] is False

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
