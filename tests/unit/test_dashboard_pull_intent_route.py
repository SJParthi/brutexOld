"""The ``/pull-pipeline`` durable pull-intent wiring (DEC-PULL-INTENT-RESUME-001).

Route-level pins over ``build_app`` + ``TestClient`` with the three pull spines
replaced by OFFLINE fakes (the REAL report dataclasses — never a network call):

* a REAL pipeline launch persists ``.pull_intent.json`` BEFORE the job starts
  and the job's ``on_finally`` clears it when the worker exits — on success AND
  on failure;
* a ``dry_run`` NEVER writes an intent and NEVER clears a crashed real pull's
  intent (pinned — the dry-run-erases-the-crash-record trap);
* ``GET /api/v1/liveproof`` carries the resume red-banner ``resume`` section
  iff the banner holds a payload (absent ⇒ byte-identical payload);
* ``app.state.brutex_resume_pipeline`` re-fires the SAME persisted quadruple
  through the SAME launch path, bumping the restart token; a stale selection
  clears the intent; an over-cap intent refuses with ONE CRITICAL alert + the
  banner; a peer-held single-flight ``flock`` refuses WITHOUT burning a token
  or failing the fresh job's progress; a credential 503 at boot keeps the
  intent for the next boot (cap-bounded).

Skipped cleanly when the ``[ui]`` extra (FastAPI) or duckdb is absent.
"""

from __future__ import annotations

import importlib
import threading
import time
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock
from brutex.config.credentials import ENV_CREDENTIALS_FILE
from brutex.dashboard import pull_intent as pull_intent_module
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.composite_pull import CompositePullReport
from brutex.dashboard.historical_pull import HistoricalPullReport
from brutex.dashboard.metadata_pull import MetadataPullReport
from brutex.dashboard.pull_controller import BulkSpotReport, SymbolPullResult
from brutex.dashboard.pull_intent import PullIntent, PullIntentStore
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

app_module = importlib.import_module("brutex.dashboard.app")


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
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def intent_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PullIntentStore:
    # The intent sidecar lives under BRUTEX_LAKE_ROOT; since
    # DEC-GREEKS-BULK-REENRICH-001 fix round 2 the JOB's cross-process flock
    # keys on the STORE-derived WRITE root (the instrument store's parent =
    # tmp_path here), and the resume probe follows it — deliberately DISTINCT
    # from this env root so a probe/lock split-brain regression fails loudly.
    lake = tmp_path / "lake"
    lake.mkdir()
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(lake))
    return PullIntentStore(lake)


@pytest.fixture
def client(
    store: InstrumentStore,
    intent_store: PullIntentStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[TestClient]:
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    app = build_app(DashboardState(), instrument_store=store, pull_intent_store=intent_store)
    with TestClient(app) as c:
        yield c


def _pin_creds_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "groww_credentials_error", lambda: None)


def _fake_spot(*args: Any, **kwargs: Any) -> BulkSpotReport:
    return BulkSpotReport(
        dry_run=kwargs["dry_run"],
        start_date="2020-01-01",
        end_date="2026-06-20",
        symbols=[SymbolPullResult(symbol="NIFTY", completed=2, failed=0, no_data=0)],
    )


def _fake_metadata(*args: Any, **kwargs: Any) -> MetadataPullReport:
    return MetadataPullReport(
        dry_run=kwargs["dry_run"],
        years=tuple(kwargs["years"]),
        underlyings=("NIFTY",),
        expiry_cells=1,
        cells_skipped=0,
        expiry_failures=0,
        expiries_persisted=4,
        contract_jobs=1,
        contract_failures=0,
        contracts_persisted=40,
        note="offline fake",
    )


def _fake_historical(*args: Any, **kwargs: Any) -> CompositePullReport:
    inner = HistoricalPullReport(
        dry_run=kwargs["dry_run"],
        intervals=("1minute",),
        contracts_selected=1,
        contracts_completed=1,
        bars_appended=100,
        windows=1,
        failed_pairs=0,
        still_failed_windows=0,
        auth_aborted=False,
        note="ok",
        phase1_clean=True,
        phase1_complete=("NIFTY24JAN20000CE",),
        start_dates={"NIFTY24JAN20000CE": "2024-01-01"},
        minute_requested=True,
        minute_skipped=False,
    )
    return CompositePullReport(
        dry_run=kwargs["dry_run"],
        requested=("NIFTY",),
        tradable=("NIFTY",),
        skipped_reference_only=(),
        metadata=_fake_metadata(**kwargs),
        historical=inner,
        note="offline fake",
    )


def _patch_spines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "pull_all_spot", _fake_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)


def _wait_for_job(client: TestClient, *, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert isinstance(snap, dict)
        if snap["state"] in {"done", "error"}:
            return snap
        time.sleep(0.01)
    raise AssertionError("background pipeline job did not settle within the timeout")


def _wait_for(predicate: Any, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached within the timeout")


# ── intent write + on_finally clear on the route ─────────────────────────────


def test_real_pull_writes_intent_and_clears_it_on_success(
    client: TestClient, intent_store: PullIntentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_creds_present(monkeypatch)
    gate = threading.Event()

    def _blocking_spot(*args: Any, **kwargs: Any) -> BulkSpotReport:
        opened = gate.wait(timeout=10.0)
        assert opened
        return _fake_spot(*args, **kwargs)

    monkeypatch.setattr(app_module, "pull_all_spot", _blocking_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)
    resp = client.post(
        "/api/v1/universe/pull-pipeline",
        params={"symbols": "NIFTY", "from_date": "2024-01-01", "to_date": "2024-02-01"},
    )
    assert resp.status_code == 200
    assert resp.json()["started"] is True
    # While the pull runs, the durable intent is on disk with the request quadruple.
    loaded = intent_store.load()
    assert loaded is not None
    assert (loaded.symbols, loaded.symbol, loaded.from_date, loaded.to_date) == (
        "NIFTY",
        None,
        "2024-01-01",
        "2024-02-01",
    )
    assert loaded.restarts == 0
    gate.set()
    assert _wait_for_job(client)["state"] == "done"
    _wait_for(lambda: not intent_store.path.exists())


def test_failed_pull_still_clears_the_intent(
    client: TestClient, intent_store: PullIntentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_creds_present(monkeypatch)

    def _boom_spot(*args: Any, **kwargs: Any) -> BulkSpotReport:
        raise RuntimeError("spot exploded")

    monkeypatch.setattr(app_module, "pull_all_spot", _boom_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)
    resp = client.post("/api/v1/universe/pull-pipeline")
    assert resp.json()["started"] is True
    assert _wait_for_job(client)["state"] == "error"
    # on_finally fires on FAILURE too — the worker exited, so the intent clears.
    _wait_for(lambda: not intent_store.path.exists())


def test_dry_run_never_writes_and_never_erases_a_crashed_intent(
    client: TestClient, intent_store: PullIntentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    # A crashed REAL pull left this intent behind…
    crashed = PullIntent(symbols="NIFTY", symbol=None, from_date=None, to_date=None, restarts=1)
    wrote = intent_store.write(crashed)
    assert wrote is True
    # …and a dry-run must neither overwrite nor clear it.
    resp = client.post("/api/v1/universe/pull-pipeline", params={"dry_run": "true"})
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    time.sleep(0.05)  # give a (wrong) clear every chance to fire before asserting
    assert intent_store.load() == crashed


# ── the liveproof resume-banner section ──────────────────────────────────────


def test_liveproof_carries_resume_banner_when_set(
    client: TestClient, intent_store: PullIntentStore
) -> None:
    baseline = client.get("/api/v1/liveproof").json()
    assert baseline == {"present": False}, "no banner ⇒ payload byte-identical"
    intent_store.banner.set(level="critical", message="pull auto-resume HALTED: cap")
    body = client.get("/api/v1/liveproof").json()
    assert body["present"] is False
    assert body["resume"] == {"level": "critical", "message": "pull auto-resume HALTED: cap"}
    intent_store.banner.clear()
    assert "resume" not in client.get("/api/v1/liveproof").json()


def test_liveproof_without_intent_store_is_untouched(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        assert c.get("/api/v1/liveproof").json() == {"present": False}


# ── the app.state.brutex_resume_pipeline seam ────────────────────────────────


def test_resume_singleflight_probe_keys_on_the_job_root(tmp_path: Path) -> None:
    """The peer probe contends on the RESOLVER root — held ⇒ False, free ⇒ True."""
    job_root = tmp_path / "job-root"
    job_root.mkdir()
    probe = app_module._resume_singleflight_probe(lambda: job_root)
    assert probe() is True, "a free job-root flock reads acquirable"
    with pull_singleflight_lock(job_root):
        assert probe() is False, "a held job-root flock reads a live peer"
    assert probe() is True, "released ⇒ acquirable again"


def test_resume_singleflight_probe_none_root_mirrors_the_lockless_job() -> None:
    """A ``None`` root means the jobs take NO lock — nothing to probe, resume may fire."""
    probe = app_module._resume_singleflight_probe(lambda: None)
    assert probe() is True


def test_resume_seam_absent_without_intent_store(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    app = build_app(DashboardState(), instrument_store=store)
    assert not hasattr(app.state, "brutex_resume_pipeline")


def test_resume_refires_the_same_quadruple_and_clears_on_completion(
    client: TestClient, intent_store: PullIntentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_creds_present(monkeypatch)
    spot_calls: list[dict[str, Any]] = []

    def _spot(*args: Any, **kwargs: Any) -> BulkSpotReport:
        spot_calls.append(dict(kwargs))
        return _fake_spot(*args, **kwargs)

    monkeypatch.setattr(app_module, "pull_all_spot", _spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)
    intent_store.write(
        PullIntent(symbols="NIFTY", symbol=None, from_date="2024-01-01", to_date="2024-02-01")
    )
    client.app.state.brutex_resume_pipeline()  # type: ignore[attr-defined]
    assert _wait_for_job(client)["state"] == "done"
    # The SAME persisted dates reached the spot phase (never re-guessed).
    assert spot_calls[0]["from_date"] == "2024-01-01"
    assert spot_calls[0]["to_date"] == "2024-02-01"
    assert spot_calls[0]["dry_run"] is False
    # The resumed run's completion cleared the intent (on_finally).
    _wait_for(lambda: not intent_store.path.exists())
    assert intent_store.banner.snapshot() is None


def test_resume_stale_selection_clears_intent_and_raises_banner(
    client: TestClient, intent_store: PullIntentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    intent_store.write(PullIntent(symbols="99999", symbol=None, from_date=None, to_date=None))
    client.app.state.brutex_resume_pipeline()  # type: ignore[attr-defined]
    assert not intent_store.path.exists(), "a stale selection clears the intent (no loop)"
    banner = intent_store.banner.snapshot()
    assert banner is not None
    assert banner["level"] == "critical"
    assert "99999" in banner["message"]
    body = client.get("/api/v1/liveproof").json()
    assert body["resume"]["level"] == "critical"


def test_resume_over_cap_refuses_with_alert_and_banner_no_launch(
    client: TestClient, intent_store: PullIntentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    alerts: list[PullIntent] = []
    monkeypatch.setattr(pull_intent_module, "emit_restart_cap_alert", alerts.append)
    intent_store.write(
        PullIntent(symbols="NIFTY", symbol=None, from_date=None, to_date=None, restarts=2)
    )
    client.app.state.brutex_resume_pipeline()  # type: ignore[attr-defined]
    assert len(alerts) == 1, "exactly ONE CRITICAL alert on the over-cap refusal"
    assert intent_store.path.exists(), "the over-cap intent is kept for forensics"
    snap = client.get("/api/v1/universe/pull-progress").json()
    assert snap["state"] == "idle", "no pull may start over the cap"
    assert client.get("/api/v1/liveproof").json()["resume"]["level"] == "critical"


def test_resume_peer_held_flock_burns_no_token_and_fails_no_progress(
    client: TestClient,
    store: InstrumentStore,
    intent_store: PullIntentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    intent_store.write(
        PullIntent(symbols="NIFTY", symbol=None, from_date=None, to_date=None, restarts=0)
    )
    # A live peer's BackgroundPullJob flocks the STORE-derived WRITE root
    # (`_job_lake_root_resolver` — the store's parent, DEC-GREEKS-BULK-REENRICH-001
    # fix round 2), NOT the env lake root; the resume probe must contend on the
    # SAME lock file or a healthy peer costs a restart token.
    with pull_singleflight_lock(store.path.parent):
        client.app.state.brutex_resume_pipeline()  # type: ignore[attr-defined]
    reloaded = intent_store.load()
    assert reloaded is not None
    assert reloaded.restarts == 0, "a peer-held flock must not burn a restart token"
    assert intent_store.banner.snapshot() is None
    snap = client.get("/api/v1/universe/pull-progress").json()
    assert snap["state"] == "idle", "the fresh boot's progress is never marked FAILED"


def test_resume_credential_refusal_keeps_intent_for_next_boot(
    client: TestClient, intent_store: PullIntentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Creds NOT pinned: the launch's loud-refusal gate raises a 503, which the
    # seam maps to a TRANSIENT failure — the intent survives (token burnt),
    # bounded by the cap, and the banner says why.
    _patch_spines(monkeypatch)
    intent_store.write(
        PullIntent(symbols="NIFTY", symbol=None, from_date=None, to_date=None, restarts=0)
    )
    client.app.state.brutex_resume_pipeline()  # type: ignore[attr-defined]
    reloaded = intent_store.load()
    assert reloaded is not None
    assert reloaded.restarts == 1, "the intent survives a credential 503 (retried ≤ cap)"
    banner = intent_store.banner.snapshot()
    assert banner is not None
    assert banner["level"] == "critical"
