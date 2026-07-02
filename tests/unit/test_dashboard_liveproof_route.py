"""The ``/api/v1/liveproof`` route + the zero-command autorun wiring in the app.

DEC-DOCTOR-LIVE-AUTORUN-001. Covers the dashboard half of the autorun: the
status route (wired / not wired), the loud-refusal gate mirroring its fix text
onto the banner verdict (agreement with the 503 by construction), the
first-REAL-pull probe trigger (and its absence on an explicit dry-run), the
async non-blocking startup (the app serves while the preflight thread runs),
and the no-interaction guarantee with an in-flight pull. All offline: real
:class:`~brutex.doctor_autorun.LiveProofAutorun` instances with fake runners,
creds pinned via the same seam production reads, pull spines replaced by the
suite's standard offline fakes. Skipped cleanly when the ``[ui]`` extra
(FastAPI) or duckdb is absent.
"""

from __future__ import annotations

import importlib
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex import doctor_autorun as da
from brutex import doctor_live as dl
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.pull_controller import BulkSpotReport, SymbolPullResult
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

app_module = importlib.import_module("brutex.dashboard.app")

_NOW = datetime(2026, 7, 2, 9, 30, tzinfo=UTC)


def _nifty() -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=13,
        symbol_name="NIFTY",
        display_name="NIFTY",
        instrument=InstrumentType.IDX,
        segment=Segment.INDEX,
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


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    instrument_store = InstrumentStore.open(tmp_path / "lake" / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield instrument_store
    instrument_store.close()


def _pass_report() -> dl.KitReport:
    return dl.KitReport(
        run_id="lp-test",
        transcript_path=Path("/tmp/t.jsonl"),
        outcomes=(dl.StageOutcome(stage=3, name="token", status=dl.STATUS_PASS, lines=("ok",)),),
    )


def _probes_pass_report() -> dl.KitReport:
    return dl.KitReport(
        run_id="lp-probes",
        transcript_path=Path("/tmp/t.jsonl"),
        outcomes=(
            dl.StageOutcome(stage=6, name="depth", status=dl.STATUS_PASS, lines=("ok",)),
            dl.StageOutcome(stage=7, name="vix", status=dl.STATUS_PASS, lines=("ok",)),
            dl.StageOutcome(stage=8, name="bhavcopy", status=dl.STATUS_PASS, lines=("ok",)),
        ),
    )


def _sync_starter(fn: Callable[[], None], _name: str) -> None:
    fn()


def _autorun(
    tmp_path: Path,
    *,
    probes_runner: Callable[[], dl.KitReport] | None = None,
    thread_starter: Callable[[Callable[[], None], str], None] = _sync_starter,
) -> da.LiveProofAutorun:
    """A real holder with fake runners (creds OK), rooted at the test lake."""
    return da.LiveProofAutorun(
        tmp_path / "lake",
        env={},
        credentials_error=lambda: None,
        preflight_runner=_pass_report,
        probes_runner=probes_runner if probes_runner is not None else _probes_pass_report,
        now_utc=lambda: _NOW,
        thread_starter=thread_starter,
    )


def _pin_creds_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the credential gate OPEN — these tests exercise the autorun wiring."""
    monkeypatch.setattr(app_module, "groww_credentials_error", lambda: None)


def _fake_spot(*args: Any, **kwargs: Any) -> BulkSpotReport:
    """Offline stand-in for ``pull_all_spot`` — clean, no network."""
    return BulkSpotReport(
        dry_run=kwargs["dry_run"],
        start_date="2020-01-01",
        end_date="2026-06-20",
        symbols=[SymbolPullResult(symbol="NIFTY", completed=1, failed=0, no_data=0)],
    )


def _wait_for(predicate: Callable[[], bool], timeout_s: float = 10.0) -> None:
    """Poll *predicate* until true or fail the test (deterministic bound)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("timed out waiting for the background autorun to finish")


# ── the status route ─────────────────────────────────────────────────────


def test_liveproof_route_not_wired_is_present_false() -> None:
    """A bare app (no autorun injected) reports present: false — no invented state."""
    with TestClient(build_app(DashboardState())) as client:
        body = client.get("/api/v1/liveproof").json()
    assert body == {"present": False}


def test_liveproof_route_serves_the_holder_status(tmp_path: Path) -> None:
    """A wired autorun's status (incl. the verdict) is served verbatim."""
    autorun = _autorun(tmp_path)
    autorun.start_preflight()
    with TestClient(build_app(DashboardState(), liveproof=autorun)) as client:
        body = client.get("/api/v1/liveproof").json()
    assert body["present"] is True
    assert body["enabled"] is True
    assert body["preflight"]["state"] == da.STATE_DONE
    assert body["preflight"]["verdict"]["kind"] == da.KIND_PASS
    assert body["preflight"]["verdict"]["headline"] == "live-proof: PASS"
    assert body["probes"]["state"] == da.STATE_IDLE
    assert body["probes"]["verdict"] is None


def test_liveproof_route_is_read_only(tmp_path: Path) -> None:
    """Polling the route never starts a kit run (display, not action)."""
    calls: list[str] = []

    def _spy() -> dl.KitReport:
        calls.append("ran")
        return _pass_report()

    autorun = da.LiveProofAutorun(
        tmp_path / "lake",
        env={},
        credentials_error=lambda: None,
        preflight_runner=_spy,
        probes_runner=_spy,
        now_utc=lambda: _NOW,
        thread_starter=_sync_starter,
    )
    with TestClient(build_app(DashboardState(), liveproof=autorun)) as client:
        for _ in range(3):
            assert client.get("/api/v1/liveproof").status_code == 200
    assert calls == []


# ── the first-REAL-pull probe trigger (the loud-refusal gate seam) ───────


def test_real_pull_triggers_the_one_time_probes(
    store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real pull admitted by the gate auto-runs the probes exactly once."""
    _pin_creds_present(monkeypatch)
    monkeypatch.setattr(app_module, "pull_all_spot", _fake_spot)
    runs: list[str] = []

    def _probes() -> dl.KitReport:
        runs.append("probe")
        return _probes_pass_report()

    autorun = _autorun(tmp_path, probes_runner=_probes)
    app = build_app(DashboardState(), instrument_store=store, liveproof=autorun)
    with TestClient(app) as client:
        first = client.post("/api/v1/universe/pull-all?kind=index")
        assert first.status_code == 200
        _wait_for(lambda: autorun.status()["probes"]["state"] == da.STATE_DONE)
        second = client.post("/api/v1/universe/pull-all?kind=index")
        assert second.status_code == 200
    # ONE probe run; the second pull read the cached settled verdict.
    assert runs == ["probe"]
    assert autorun.status()["probes"]["verdict"]["kind"] == da.KIND_PASS
    cached = da.load_verdict(da.probes_cache_path(tmp_path / "lake"))
    assert cached is not None and cached.kind == da.KIND_PASS


def test_dry_run_pull_never_triggers_probes(
    store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit synthetic dry-run is NOT a real pull — no probes fire."""
    _pin_creds_present(monkeypatch)
    monkeypatch.setattr(app_module, "pull_all_spot", _fake_spot)
    runs: list[str] = []

    def _probes() -> dl.KitReport:
        runs.append("probe")
        return _probes_pass_report()

    autorun = _autorun(tmp_path, probes_runner=_probes)
    app = build_app(DashboardState(), instrument_store=store, liveproof=autorun)
    with TestClient(app) as client:
        resp = client.post("/api/v1/universe/pull-all?kind=index&dry_run=true")
        assert resp.status_code == 200
    assert runs == []
    assert autorun.status()["probes"]["state"] == da.STATE_IDLE


def test_real_pull_without_wired_autorun_is_unchanged(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """liveproof=None keeps every pull route byte-for-byte as before."""
    _pin_creds_present(monkeypatch)
    monkeypatch.setattr(app_module, "pull_all_spot", _fake_spot)
    app = build_app(DashboardState(), instrument_store=store)
    with TestClient(app) as client:
        assert client.post("/api/v1/universe/pull-all?kind=index").status_code == 200
        assert client.get("/api/v1/liveproof").json() == {"present": False}


# ── refusal ↔ banner agreement (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001) ────


def test_credentials_refusal_mirrors_the_same_fix_text_onto_the_banner(
    store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 503 detail and the live-proof banner carry the SAME relayed text.

    Hostile-review MAJOR-4: a refused REAL pull attempt is a FAILURE of the
    live pipe, so the mirrored verdict is kind ``fail`` (the red banner
    opens) — never the grey ``not_applicable`` that used to paper over it.
    """
    fix = "Missing Groww credentials: seed /tickvault/prod/groww/api-key (region ap-south-1)"
    monkeypatch.setattr(app_module, "groww_credentials_error", lambda: fix)
    autorun = _autorun(tmp_path)
    app = build_app(DashboardState(), instrument_store=store, liveproof=autorun)
    with TestClient(app) as client:
        resp = client.post("/api/v1/universe/pull-all?kind=index")
    assert resp.status_code == 503
    verdict = autorun.status()["preflight"]["verdict"]
    assert verdict["kind"] == da.KIND_FAIL
    assert verdict["headline"] == da.HEADLINES[da.KIND_FAIL]
    # Agreement by construction: the banner line IS the 503 detail.
    assert verdict["lines"] == [resp.json()["detail"]]
    assert fix in resp.json()["detail"]


def test_credentials_refusal_without_autorun_still_refuses(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """liveproof=None: the loud refusal itself is untouched (no silent path back)."""
    monkeypatch.setattr(app_module, "groww_credentials_error", lambda: "no creds")
    app = build_app(DashboardState(), instrument_store=store)
    with TestClient(app) as client:
        resp = client.post("/api/v1/universe/pull-all?kind=index")
    assert resp.status_code == 503


# ── the gate must be UNABLE to raise into the pull (MAJOR-1 belt) ────────


def test_pull_survives_a_raising_probe_trigger(
    store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR-1 belt: even a holder BUG in on_real_pull cannot break the pull.

    The holder itself already contains spawn failures, so this guards the
    residual case (an unexpected holder exception) at the app call site —
    the admitted pull must still return 200, never a 500.
    """
    _pin_creds_present(monkeypatch)
    monkeypatch.setattr(app_module, "pull_all_spot", _fake_spot)
    autorun = _autorun(tmp_path)

    def _boom() -> bool:
        raise RuntimeError("holder bug")

    monkeypatch.setattr(autorun, "on_real_pull", _boom)
    app = build_app(DashboardState(), instrument_store=store, liveproof=autorun)
    with TestClient(app) as client:
        resp = client.post("/api/v1/universe/pull-all?kind=index")
    assert resp.status_code == 200


def test_refusal_survives_a_raising_banner_mirror(
    store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR-1 belt: a raising banner mirror never masks the 503 with a 500."""
    fix = "Missing Groww credentials: seed the SSM parameter"
    monkeypatch.setattr(app_module, "groww_credentials_error", lambda: fix)
    autorun = _autorun(tmp_path)

    def _boom(_fix_text: str) -> None:
        raise RuntimeError("holder bug")

    monkeypatch.setattr(autorun, "note_credentials_refusal", _boom)
    app = build_app(DashboardState(), instrument_store=store, liveproof=autorun)
    with TestClient(app) as client:
        resp = client.post("/api/v1/universe/pull-all?kind=index")
    assert resp.status_code == 503
    assert fix in resp.json()["detail"]


def test_pull_proceeds_when_the_probe_thread_cannot_start(
    store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAJOR-1 repro end-to-end: ``Thread.start`` raising never wedges the gate.

    With the PRODUCTION daemon-thread seam and ``Thread.start`` raising for
    the liveproof worker (the can't-start-new-thread condition), the old
    code raised INTO the admitted pull (a 500) and left the probes state
    wedged at ``running`` forever. Now: the pull returns 200, an honest
    ``error`` verdict lands, and the NEXT pull retries the probes.
    """
    _pin_creds_present(monkeypatch)
    monkeypatch.setattr(app_module, "pull_all_spot", _fake_spot)
    failures = {"on": True}
    real_start = threading.Thread.start

    def _flaky_start(self: threading.Thread) -> None:
        if failures["on"] and self.name.startswith("brutex-liveproof"):
            raise RuntimeError("can't start new thread")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", _flaky_start)
    runs: list[str] = []

    def _probes() -> dl.KitReport:
        runs.append("probe")
        return _probes_pass_report()

    autorun = _autorun(tmp_path, probes_runner=_probes, thread_starter=da.start_daemon_thread)
    app = build_app(DashboardState(), instrument_store=store, liveproof=autorun)
    with TestClient(app) as client:
        first = client.post("/api/v1/universe/pull-all?kind=index")
        assert first.status_code == 200  # the pull proceeded despite the spawn failure
        status = autorun.status()
        assert status["probes"]["state"] == da.STATE_IDLE  # never wedged
        assert status["probes"]["verdict"]["kind"] == da.KIND_ERROR
        assert runs == []
        # Threads recover → the NEXT real pull retries the one-time probes.
        failures["on"] = False
        second = client.post("/api/v1/universe/pull-all?kind=index")
        assert second.status_code == 200
        _wait_for(lambda: autorun.status()["probes"]["state"] == da.STATE_DONE)
    assert runs == ["probe"]
    assert autorun.status()["probes"]["verdict"]["kind"] == da.KIND_PASS


# ── async non-blocking startup ───────────────────────────────────────────


def test_app_serves_while_the_preflight_runs(tmp_path: Path) -> None:
    """The preflight runs on a daemon thread; every route serves meanwhile."""
    gate = threading.Event()
    started = threading.Event()

    def _blocking_preflight() -> dl.KitReport:
        started.set()
        assert gate.wait(timeout=10.0), "test gate never released"
        return _pass_report()

    autorun = da.LiveProofAutorun(
        tmp_path / "lake",
        env={},
        credentials_error=lambda: None,
        preflight_runner=_blocking_preflight,
        probes_runner=_probes_pass_report,
        now_utc=lambda: _NOW,
        thread_starter=da.start_daemon_thread,  # the REAL daemon-thread seam
    )
    with TestClient(build_app(DashboardState(), liveproof=autorun)) as client:
        assert autorun.start_preflight() is True  # what cmd_dashboard does at boot
        assert started.wait(timeout=10.0)
        # While the preflight is blocked mid-run, the app keeps serving.
        body = client.get("/api/v1/liveproof").json()
        assert body["preflight"]["state"] == da.STATE_RUNNING
        assert body["preflight"]["verdict"] is None
        assert client.get("/api/v1/state").status_code == 200
        gate.set()
        _wait_for(lambda: autorun.status()["preflight"]["state"] == da.STATE_DONE)
        done = client.get("/api/v1/liveproof").json()
    assert done["preflight"]["verdict"]["kind"] == da.KIND_PASS


# ── no interaction with an in-flight pull ────────────────────────────────


def test_probes_never_touch_the_pull_machinery(
    store: InstrumentStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocked probe run leaves pulls fully functional (and vice versa).

    The probes hold NO reference to the pull job: while the probe worker is
    blocked mid-run, /pull-progress still serves its own snapshot, a REAL
    pull completes normally, and the gate's second trigger is refused by the
    autorun's own single-flight (never by the pull's).
    """
    _pin_creds_present(monkeypatch)
    monkeypatch.setattr(app_module, "pull_all_spot", _fake_spot)
    gate = threading.Event()
    started = threading.Event()

    def _blocking_probes() -> dl.KitReport:
        started.set()
        assert gate.wait(timeout=10.0), "test gate never released"
        return _probes_pass_report()

    autorun = da.LiveProofAutorun(
        tmp_path / "lake",
        env={},
        credentials_error=lambda: None,
        preflight_runner=_pass_report,
        probes_runner=_blocking_probes,
        now_utc=lambda: _NOW,
        thread_starter=da.start_daemon_thread,
    )
    app = build_app(DashboardState(), instrument_store=store, liveproof=autorun)
    with TestClient(app) as client:
        # First REAL pull → probes start (and block); the pull itself completes.
        first = client.post("/api/v1/universe/pull-all?kind=index")
        assert first.status_code == 200
        assert started.wait(timeout=10.0)
        # The pull job's own snapshot serves untouched while probes run.
        progress = client.get("/api/v1/universe/pull-progress")
        assert progress.status_code == 200
        # A second REAL pull mid-probe still works; no second probe run starts.
        assert autorun.on_real_pull() is False
        second = client.post("/api/v1/universe/pull-all?kind=index")
        assert second.status_code == 200
        gate.set()
        _wait_for(lambda: autorun.status()["probes"]["state"] == da.STATE_DONE)
    assert autorun.status()["probes"]["verdict"]["kind"] == da.KIND_PASS
