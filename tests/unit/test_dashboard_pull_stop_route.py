"""``POST /api/v1/pull/stop`` — the honest-minimum STOP control.

DEC-AUTO-RESUME-ZERO-SURPRISES-001: there was previously NO stop mechanism at
all (no ``BackgroundPullJob`` cancel, no route). Route-level pins over
``build_app`` + ``TestClient``:

* ONE action clears the durable ``.pull_intent.json`` (a subsequent
  kill/restart does NOT re-loop — the intent arm reads no intent), sets the
  job's cooperative stop flag, raises the amber NOTICE banner, and returns an
  HONEST verdict naming what it did and what it could not do;
* the verdict distinguishes a live pull (``pull_running``) from an idle stop
  (stale-intent cleanup only), and ``intent_present`` / ``intent_cleared``
  report the real file state — never a fabricated claim;
* stopping marks NOTHING as SUCCESS — the durable ledger is untouched by the
  route (the cooperative flag only skips future phases);
* with NO intent store wired the route still sets the flag and honestly
  reports ``banner_set: false`` (no surface to hang the notice on).

Skipped cleanly when the ``[ui]`` extra (FastAPI) or duckdb is absent.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.pull_intent import PullIntent, PullIntentStore
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient


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
    lake = tmp_path / "lake"
    lake.mkdir()
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(lake))
    return PullIntentStore(lake)


@pytest.fixture
def client(store: InstrumentStore, intent_store: PullIntentStore) -> Iterator[TestClient]:
    app = build_app(DashboardState(), instrument_store=store, pull_intent_store=intent_store)
    with TestClient(app) as c:
        yield c


def _intent() -> PullIntent:
    return PullIntent(
        symbols=None,
        symbol=None,
        from_date=None,
        to_date=None,
        restarts=0,
        created_at="2026-07-09T00:00:00+00:00",
    )


def test_stop_clears_intent_sets_flag_and_notice_banner(
    client: TestClient, intent_store: PullIntentStore
) -> None:
    intent_store.write(_intent())
    assert intent_store.path.exists()
    body = client.post("/api/v1/pull/stop").json()
    # The honest verdict: what happened, exactly.
    assert body["stop_requested"] is True
    assert body["pull_running"] is False
    assert body["intent_present"] is True
    assert body["intent_cleared"] is True
    assert body["banner_set"] is True
    # The honest LIMITS are stated in the note (Golden Rule #10).
    assert "phase boundaries" in body["note"]
    assert "hard stop" in body["note"]
    assert "Nothing was marked complete" in body["note"]
    # The re-loop is dead: the intent file is gone…
    assert not intent_store.path.exists()
    assert intent_store.load() is None
    # …and the visible state is a NOTICE (amber), never a critical alarm.
    banner = intent_store.banner.snapshot()
    assert banner is not None
    assert banner["level"] == "notice"
    assert "next boot will NOT auto-resume" in banner["message"]


def test_stop_next_boot_does_not_refire(client: TestClient, intent_store: PullIntentStore) -> None:
    # The whole point: after a stop, the boot decision reads NO intent —
    # run_auto_resume (the intent arm's core) returns NO_INTENT and fires
    # nothing.
    from brutex.dashboard.pull_intent import ResumeAction, run_auto_resume

    intent_store.write(_intent())
    client.post("/api/v1/pull/stop")
    launched: list[PullIntent] = []
    action = run_auto_resume(intent_store, launch=launched.append, probe_singleflight=lambda: True)
    assert action is ResumeAction.NO_INTENT
    assert launched == []


def test_stop_without_intent_reports_honestly(
    client: TestClient, intent_store: PullIntentStore
) -> None:
    body = client.post("/api/v1/pull/stop").json()
    assert body["intent_present"] is False
    assert body["intent_cleared"] is False, "nothing to clear — never a fabricated claim"
    assert body["stop_requested"] is True
    assert body["banner_set"] is True


def test_stop_notice_rides_the_liveproof_payload(
    client: TestClient, intent_store: PullIntentStore
) -> None:
    # The widget's data source carries the notice, so the amber strip renders
    # on every page without a dedicated poll.
    client.post("/api/v1/pull/stop")
    payload = client.get("/api/v1/liveproof").json()
    assert payload["resume"]["level"] == "notice"
    assert "pull stop requested" in payload["resume"]["message"]


def test_stop_flips_the_job_flag_visible_to_the_pipeline_binding(
    client: TestClient,
) -> None:
    # The route sets the SAME flag the pipeline's should_stop lambda reads
    # (app.py binds `lambda: _pull_job.stop_requested` into run_full_pipeline).
    job = client.app.state.brutex_pull_job  # type: ignore[attr-defined]
    assert job.stop_requested is False
    client.post("/api/v1/pull/stop")
    assert job.stop_requested is True


def test_stop_without_intent_store_sets_flag_only(store: InstrumentStore) -> None:
    # A bare app (no pull_intent_store) still stops cooperatively; the verdict
    # honestly reports there was no banner surface (never a faked banner).
    app = build_app(DashboardState(), instrument_store=store)
    with TestClient(app) as c:
        body = c.post("/api/v1/pull/stop").json()
    assert body["stop_requested"] is True
    assert body["intent_present"] is False
    assert body["intent_cleared"] is False
    assert body["banner_set"] is False


def test_pipeline_work_binds_should_stop_to_the_job_flag() -> None:
    # The one-line binding lives in the pragma-covered `_work` body (it runs on
    # the job thread against real spines), so pin it at SOURCE level: the
    # sequencer must receive the job's stop flag, or the route's flag would be
    # set and silently never consulted (the mutant this kills).
    import inspect

    from brutex.dashboard import app as app_module

    source = inspect.getsource(app_module)
    assert "should_stop=lambda: _pull_job.stop_requested" in source
