"""Integration tests for the Dhan rolling-ATM option pull route (DEC-DHAN-ROLLING-OPTIONS-001).

End-to-end through FastAPI's :class:`TestClient` with an INJECTED
``dhan_rolling_option_pull`` seam (the ``build_app`` param): a fake pull writes synthetic
rolling-ATM bars into ``feeds/dhan/rolling_options/`` under a temp lake (dry-run isolation
— no real SSM / dhanhq / network). Asserts the route validates + starts on the SEPARATE
``_dhan_option_pull_job`` (own single-flight flock on ``feeds/dhan/``, no Groww collision),
is ``market_hours_exempt`` (Dhan uses its own account → ``market_closed=None``), nests the
write tree under ``feeds/dhan/rolling_options/`` (Groww's top lake untouched), and labels
everything "rolling-ATM" (NEVER "per-contract"). Mirrors the spot route test.
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")

from brutex.adapters.dhan.rolling_option_adapter import RollingOptionAdapter
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.dhan_pull import DhanPullRequest
from brutex.dashboard.dhan_rolling_pull import (
    DhanRollingOptionPullReport,
    default_dhan_rolling_option_pull,
)
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

app_module = importlib.import_module("brutex.dashboard.app")

_PAYLOAD: dict[str, list[object]] = {
    "timestamp": [1_717_386_300.0],
    "open": [100],
    "high": [102],
    "low": [99],
    "close": [101],
}


class _FakeRollingClient:
    """A rolling-option client that COUNTS calls — proves a dry-run issues zero."""

    def __init__(self) -> None:
        self.calls = 0

    def rolling_option_data(self, *, body: Mapping[str, object]) -> Mapping[str, object]:
        _ = body
        self.calls += 1
        return _PAYLOAD


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


class _FakeRollingPull:
    """An injected ``dhan_rolling_option_pull`` seam that DELEGATES to the REAL
    production ``default_dhan_rolling_option_pull`` with a SPY ``adapter_factory``.

    So the route → production → dry-run short-circuit chain runs end-to-end OFFLINE:
    on ``dry_run`` the real seam plans the pure cartesian and returns a zero-outcome
    report WITHOUT building an adapter (``adapter_built`` stays ``False``), issuing a
    vendor call (``client.calls`` stays 0), or writing. No AWS is ever touched — the
    spy ``adapter_factory`` stands in for the real SSM/``dhanhq`` build, which is never
    invoked on the dry-run path.
    """

    def __init__(self) -> None:
        self.done = threading.Event()
        self.request: DhanPullRequest | None = None
        self.adapter_built = False
        self.client = _FakeRollingClient()

    def _spy_factory(self) -> RollingOptionAdapter:
        self.adapter_built = True
        return RollingOptionAdapter(client=self.client)

    def __call__(self, request: DhanPullRequest) -> DhanRollingOptionPullReport:
        self.request = request
        try:
            # band=0 → 48 keys (fast if ever WET); the dry-run branch never fetches.
            return default_dhan_rolling_option_pull(
                request, adapter_factory=self._spy_factory, band=0
            )
        finally:
            self.done.set()


@pytest.fixture
def fake_pull() -> _FakeRollingPull:
    return _FakeRollingPull()


@pytest.fixture
def client(store: InstrumentStore, fake_pull: _FakeRollingPull) -> Iterator[TestClient]:
    with TestClient(
        build_app(DashboardState(), instrument_store=store, dhan_rolling_option_pull=fake_pull)
    ) as c:
        yield c


class TestDhanRollingPullRoute:
    def test_dry_run_is_offline_and_writes_nothing(
        self, client: TestClient, fake_pull: _FakeRollingPull, tmp_path: Path
    ) -> None:
        """HONEST dry-run: plan-only — NO adapter build, NO vendor call, NO write, NO AWS.

        Drives the REAL ``default_dhan_rolling_option_pull`` seam (via the injected
        fake's SPY ``adapter_factory``), so the route → production → dry-run
        short-circuit runs end-to-end OFFLINE. The credential gate short-circuits on
        ``dry_run`` and the rolling job start skips the cross-process guards
        (``cross_process_lock=False``), so the dry-run never resolves an SSM/AWS
        credential and never fires a vendor call — it returns the honest planned-keys
        report and writes nothing (Golden Rule #1). This SUPERSEDES the stale
        "dry-run pulls into rolling_options" assertion (a dry-run must never write).
        """
        resp = client.post("/api/v1/universe/pull-rolling-options-dhan", params={"dry_run": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["feed"] == "dhan"
        assert body["dataset"] == "rolling-ATM"  # HONESTY LAW — never per-contract
        assert body["dry_run"] is True
        assert body["started"] is True
        assert fake_pull.done.wait(timeout=10.0)
        assert fake_pull.request is not None
        assert fake_pull.request.lake_root == tmp_path
        assert fake_pull.request.dry_run is True
        # The real seam short-circuited: NO adapter build, NO vendor call.
        assert fake_pull.adapter_built is False
        assert fake_pull.client.calls == 0
        # A dry-run writes NOTHING — neither the Dhan feed tree nor the Groww top lake.
        assert not (tmp_path / "feeds").exists()
        assert not (tmp_path / "bars").exists()

    def test_market_hours_exempt(
        self,
        store: InstrumentStore,
        fake_pull: _FakeRollingPull,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        starts: list[dict[str, Any]] = []
        orig_start = app_module.BackgroundPullJob.start

        def _spy(self: Any, work: Any, **kwargs: Any) -> bool:
            starts.append(
                {"detail": kwargs.get("detail"), "exempt": kwargs.get("market_hours_exempt")}
            )
            return bool(orig_start(self, work, **kwargs))

        monkeypatch.setattr(app_module.BackgroundPullJob, "start", _spy)
        with TestClient(
            build_app(DashboardState(), instrument_store=store, dhan_rolling_option_pull=fake_pull)
        ) as c:
            c.post("/api/v1/universe/pull-rolling-options-dhan", params={"dry_run": True})
            fake_pull.done.wait(timeout=10.0)
        rolling_starts = [s for s in starts if "rolling-ATM" in str(s["detail"])]
        assert rolling_starts, f"no rolling-ATM job start captured: {starts}"
        assert rolling_starts[0]["exempt"] is True

    def test_bad_date_range_is_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/universe/pull-rolling-options-dhan",
            params={"from_date": "2024-06-30", "to_date": "2024-06-01", "dry_run": True},
        )
        assert resp.status_code == 400

    def test_universe_page_renders_rolling_button(self, client: TestClient) -> None:
        html = client.get("/universe").text
        assert 'id="pullDhanOptBtn"' in html
        assert "pull-rolling-options-dhan" in html
        assert "rolling-ATM" in html
        assert "NOT per-contract" in html


class TestDhanRollingUnifiedFloorRoute:
    """DEC-DHAN-ROLLING-FLOOR-UNIFY-001 — the route-level unified-floor surface.

    Frozen clock 2026-07-14 → rolling term 2021-07-21 < the requested 2022-06-01
    start → the unified floor resolves 2022-06-01 (the adoption value).
    """

    def test_whole_pre_floor_pick_is_400_naming_the_floor_zero_fetches(
        self,
        client: TestClient,
        fake_pull: _FakeRollingPull,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A range ending on/before the UNIFIED floor is a LOUD HTTP 400 naming
        both terms — zero vendor calls, zero job start (the spot route posture;
        ``dry_run=True`` keeps the test offline, the refusal applies identically)."""
        frozen = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
        monkeypatch.setattr(app_module, "now_ist", lambda: frozen)
        resp = client.post(
            "/api/v1/universe/pull-rolling-options-dhan",
            params={"from_date": "2020-01-01", "to_date": "2022-05-01", "dry_run": True},
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "2022-06-01" in detail  # the unified floor = the requested-start term today
        assert "rolling-options" in detail
        assert "nothing was pulled" in detail
        # ZERO fetches, ZERO job start: the fake pull was never invoked.
        assert fake_pull.request is None
        assert fake_pull.client.calls == 0

    def test_clamped_start_payload_gains_floor_keys_and_threads_today(
        self,
        client: TestClient,
        fake_pull: _FakeRollingPull,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A pre-floor start with a servable end → the start payload names the clamp
        # (EMIT-ON-CLAMP additive keys) and the request threads the SAME resolved
        # today (resolved ONCE per launch — the plan clamps from the same instant).
        frozen = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
        monkeypatch.setattr(app_module, "now_ist", lambda: frozen)
        resp = client.post(
            "/api/v1/universe/pull-rolling-options-dhan",
            params={"from_date": "2020-01-01", "dry_run": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["started"] is True
        assert body["floor"] == "2022-06-01"
        assert body["effective_start"] == "2022-06-01"
        assert fake_pull.done.wait(timeout=10.0)
        assert fake_pull.request is not None
        assert fake_pull.request.today == date(2026, 7, 14)

    def test_post_floor_pick_payload_has_no_floor_keys(
        self,
        client: TestClient,
        fake_pull: _FakeRollingPull,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        frozen = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
        monkeypatch.setattr(app_module, "now_ist", lambda: frozen)
        resp = client.post(
            "/api/v1/universe/pull-rolling-options-dhan",
            params={"from_date": "2024-01-01", "to_date": "2024-02-01", "dry_run": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "floor" not in body
        assert "effective_start" not in body
        assert fake_pull.done.wait(timeout=10.0)
        assert fake_pull.request is not None
        assert fake_pull.request.today == date(2026, 7, 14)
