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


class TestDhanRollingFloorRoute:
    """DEC-DHAN-ROLLING-FLOOR-001 — the route-level floor surface."""

    def test_whole_pre_floor_pick_is_400_naming_the_floor_zero_fetches(
        self,
        client: TestClient,
        fake_pull: _FakeDhanPull,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit range ending on/before the floor is a LOUD HTTP 400 naming
        the floor — zero vendor calls, zero job start (the operator picked a
        structurally-unservable range; a silent no-op would hide that).

        ``dry_run=True`` keeps the test offline: the CREDENTIAL gate runs FIRST on
        a wet pull (a creds-less box 503s before date validation — the documented
        gate order), and the floor refusal applies to dry-run and wet identically
        (it sits after ``resolve_candle_range``, before any job start).
        """
        frozen = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
        monkeypatch.setattr(app_module, "now_ist", lambda: frozen)
        resp = client.post(
            "/api/v1/universe/pull-pipeline-dhan",
            params={"from_date": "2020-01-01", "to_date": "2020-06-01", "dry_run": True},
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "2021-07-21" in detail  # today−5y+7d, from the frozen clock
        assert "rolling 5-year floor" in detail
        assert "nothing was pulled" in detail
        # ZERO fetches, ZERO job start: the fake pull was never invoked.
        assert fake_pull.request is None
        assert fake_pull.client.calls == 0

    def test_clamped_start_payload_gains_floor_keys(
        self,
        client: TestClient,
        fake_pull: _FakeDhanPull,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A pre-floor start with a servable end → the start payload names the clamp
        # (EMIT-ON-CLAMP additive keys; the threaded today keys the same floor).
        frozen = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
        monkeypatch.setattr(app_module, "now_ist", lambda: frozen)
        resp = client.post(
            "/api/v1/universe/pull-pipeline-dhan",
            params={"from_date": "2020-01-01", "dry_run": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["started"] is True
        assert body["floor"] == "2021-07-21"
        assert body["effective_start"] == "2021-07-21"
        # The request threads the SAME resolved today (resolved ONCE per launch).
        assert fake_pull.done.wait(timeout=10.0)
        assert fake_pull.request is not None
        assert fake_pull.request.today == date(2026, 7, 14)

    def test_post_floor_pick_payload_has_no_floor_keys(
        self,
        client: TestClient,
        fake_pull: _FakeDhanPull,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        frozen = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
        monkeypatch.setattr(app_module, "now_ist", lambda: frozen)
        resp = client.post(
            "/api/v1/universe/pull-pipeline-dhan",
            params={"from_date": "2024-01-01", "to_date": "2024-02-01", "dry_run": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "floor" not in body  # EMIT-ON-CLAMP — absent when untouched
        assert "effective_start" not in body
        assert fake_pull.done.wait(timeout=10.0)


class TestDhanWetPathRoute:
    """The WET (dry_run=False) route arms (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001).

    The sibling classes above keep every start offline via ``dry_run=True``; these
    two drive the wet-only branches — the credential 503 refusal and the
    non-synthetic detail label + kept cross-process lock — with the credential
    resolver monkeypatched (no AWS is ever touched; the injected fake seam keeps
    the vendor offline).
    """

    def test_wet_pull_without_credentials_is_503_with_fix_text(
        self,
        client: TestClient,
        fake_pull: _FakeDhanPull,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A REAL pull with unresolvable SSM creds is a LOUD HTTP 503 relaying the
        loader's own fix text — zero vendor calls, zero job start, never a silent
        synthetic downgrade (the gate order the floor-route docstring documents).
        """
        fix_text = "Dhan SSM credentials missing — seed /tickvault/prod/dhan/* first"
        monkeypatch.setattr(app_module, "dhan_credentials_error", lambda **_kw: fix_text)
        resp = client.post("/api/v1/universe/pull-pipeline-dhan")  # dry_run defaults False
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert fix_text in detail  # the loader's fix text is relayed VERBATIM
        assert "NEVER silently degrades to synthetic" in detail
        # ZERO fetches, ZERO job start: the fake pull was never invoked.
        assert fake_pull.request is None
        assert fake_pull.client.calls == 0

    def test_wet_start_detail_is_not_synthetic_and_keeps_cross_process_lock(
        self,
        store: InstrumentStore,
        fake_pull: _FakeDhanPull,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A creds-resolving WET start labels the job with the PLAIN detail (no
        synthetic marker — ``label_synthetic`` is the dry-run-only arm) and KEEPS
        the cross-process single-flight lock (the dry-run skips it)."""
        monkeypatch.setattr(app_module, "dhan_credentials_error", lambda **_kw: None)
        frozen = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
        monkeypatch.setattr(app_module, "now_ist", lambda: frozen)
        starts: list[dict[str, Any]] = []
        orig_start = app_module.BackgroundPullJob.start

        def _spy(self: Any, work: Any, **kwargs: Any) -> bool:
            starts.append(
                {"detail": kwargs.get("detail"), "lock": kwargs.get("cross_process_lock")}
            )
            return bool(orig_start(self, work, **kwargs))

        monkeypatch.setattr(app_module.BackgroundPullJob, "start", _spy)
        with TestClient(
            build_app(DashboardState(), instrument_store=store, dhan_spot_pull=fake_pull)
        ) as c:
            resp = c.post(
                "/api/v1/universe/pull-pipeline-dhan",
                params={"from_date": "2024-01-01", "to_date": "2024-02-01"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["started"] is True
            assert body["dry_run"] is False
            assert fake_pull.done.wait(timeout=10.0)
        dhan_starts = [s for s in starts if "dhan" in str(s["detail"])]
        assert dhan_starts, f"no dhan job start captured: {starts}"
        # The PLAIN detail — the synthetic label is the dry-run-only arm.
        assert dhan_starts[0]["detail"] == "dhan spot pipeline"
        # The WET run keeps the cross-process single-flight lock.
        assert dhan_starts[0]["lock"] is True
        # The wet request genuinely reached the seam and built the (fake) adapter;
        # everything nested under feeds/dhan/ — the Groww top lake untouched.
        assert fake_pull.request is not None
        assert fake_pull.request.dry_run is False
        assert fake_pull.adapter_built is True
        assert (tmp_path / "feeds" / "dhan").exists()
        assert not (tmp_path / "bars").exists()


class TestDhanGrowwInterplayNegativePins:
    """Merge-interplay negative pins (design §11, the #1779 round-7 style)."""

    def test_scheduler_never_arms_or_fires_the_dhan_route(
        self, store: InstrumentStore, fake_pull: _FakeDhanPull, tmp_path: Path
    ) -> None:
        """A Dhan pull under an ARMED wired scheduler never marks the Groww day's
        ``last_attempt_date`` / ``last_success_date`` — the auto-pull scheduler is
        Groww-spot-EXCLUSIVE (its launcher IS the Groww ``_launch_pipeline``; the
        Dhan route carries no ``auto_arm`` and never calls ``note_run_complete``).
        """
        from brutex.calendars.auto_pull_gate import AutoGateVerdict
        from brutex.dashboard.auto_pull_scheduler import (
            AutoPullScheduler,
            AutoPullStateStore,
        )

        sched_dir = tmp_path / "sched"
        sched_dir.mkdir()
        scheduler = AutoPullScheduler(
            AutoPullStateStore(sched_dir),
            tick_seconds=3600.0,
            # A permanently-CLOSED gate: the armed ticker can never launch a real
            # Groww pull inside the test (deterministic, wall-clock-free).
            gate=lambda instant: AutoGateVerdict(
                allowed=False, reason="test_closed", detail="closed for the pin"
            ),
            env={},
        )
        scheduler.arm()
        with TestClient(
            build_app(
                DashboardState(),
                instrument_store=store,
                dhan_spot_pull=fake_pull,
                auto_pull_scheduler=scheduler,
            )
        ) as c:
            resp = c.post("/api/v1/universe/pull-pipeline-dhan", params={"dry_run": True})
            assert resp.status_code == 200
            assert fake_pull.done.wait(timeout=10.0)
            status = scheduler.status()
            assert status["armed"] is True
            assert status["last_attempt_date"] is None  # the Dhan run marked nothing
            assert status["last_success_date"] is None

    def test_dhan_route_source_has_no_auto_arm_or_scheduler_reference(self) -> None:
        # Structural pin: the Dhan block (banner → the pull_intent resume block)
        # references neither the scheduler nor the auto_arm token.
        app_file = app_module.__file__
        assert app_file is not None  # a regular (non-namespace) module always has one
        source = Path(app_file).read_text(encoding="utf-8")
        start = source.index("# DHAN SPOT feed pull (DEC-DHAN-PULL-SURFACE-001")
        end = source.index("if pull_intent_store is not None:", start)
        dhan_block = source[start:end]
        assert "auto_arm" not in dhan_block
        assert "auto_pull_scheduler" not in dhan_block
        assert "note_run_complete" not in dhan_block

    def test_dhan_pull_never_clobbers_the_groww_pull_verdict(
        self, client: TestClient, fake_pull: _FakeDhanPull
    ) -> None:
        # The Groww /pull-verdict banner survives a Dhan pull byte-equal (the Dhan
        # block never writes _last_pull_verdict — a prior red banner must survive).
        before = client.get("/api/v1/universe/pull-verdict")
        assert before.status_code == 200
        resp = client.post("/api/v1/universe/pull-pipeline-dhan", params={"dry_run": True})
        assert resp.status_code == 200
        assert fake_pull.done.wait(timeout=10.0)
        after = client.get("/api/v1/universe/pull-verdict")
        assert after.status_code == 200
        assert after.json() == before.json()

    def test_set_bulk_disabled_stays_groww_scoped_template_pin(self, client: TestClient) -> None:
        # Template pin: the Dhan button carries its OWN id + handler; the Groww
        # bulk-disable helper does not list the Dhan button (a Dhan pull must not
        # grey out the Groww pipeline buttons — disjoint accounts + trees).
        html = client.get("/universe").text
        assert 'id="pullDhanBtn"' in html
        bulk_fn_start = html.index("function setBulkDisabled")
        bulk_fn_end = html.index("}", bulk_fn_start)
        assert "pullDhanBtn" not in html[bulk_fn_start:bulk_fn_end]

    def test_rate_widget_meter_lifecycle_vacuous_hold_for_meterless_dhan_pull(
        self, client: TestClient, fake_pull: _FakeDhanPull
    ) -> None:
        """DEC-DHAN-RATE-VISIBILITY-001 deferral (design §7): the Dhan pull
        registers NO CallMeter, so the meter-lifecycle law ("no in-process pull ⇒
        no meter registered") holds VACUOUSLY — /api/v1/pull/rate reads
        meter-absent during AND after a Dhan pull (the Groww-keyed widget never
        fabricates a Dhan rate; interim visibility = the Dhan job progress route).
        """
        resp = client.post("/api/v1/universe/pull-pipeline-dhan", params={"dry_run": True})
        assert resp.status_code == 200
        assert fake_pull.done.wait(timeout=10.0)
        rate = client.get("/api/v1/pull/rate")
        assert rate.status_code == 200
        body = rate.json()
        # No meter was registered by the Dhan pull: the achieved slot reads the
        # honest meter-absent shape, never a fabricated "flowing".
        assert body.get("meter_present") in (False, None)
