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

import time
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
    # The honest LIMITS are stated in the note (Golden Rule #10) — including
    # the legacy-ledger-arm exception (review round 1, MAJOR 1.5: the prior
    # unconditional "no auto-resume on the next boot" claim was false under
    # the explicitly-enabled legacy arm).
    assert "phase boundaries" in body["note"]
    assert "hard stop" in body["note"]
    assert "Nothing was marked complete" in body["note"]
    assert "INTENT arm cannot re-fire" in body["note"]
    assert "BRUTEX_AUTO_RESUME=1 AND BRUTEX_AUTO_RESUME_ON_BOOT=1" in body["note"]
    # The INTENT-arm re-loop is dead: the intent file is gone…
    assert not intent_store.path.exists()
    assert intent_store.load() is None
    # …and the visible state is a NOTICE (amber), never a critical alarm —
    # truthful per-state wording (minors 13/14: an idle stop SAYS no pull ran;
    # the intent clause appears because an intent existed; the legacy-arm
    # exception is named, never the old absolute claim).
    banner = intent_store.banner.snapshot()
    assert banner is not None
    assert banner["level"] == "notice"
    assert "no pull was running" in banner["message"]
    assert "intent arm cannot re-fire" in banner["message"]
    assert "BRUTEX_AUTO_RESUME=1 AND BRUTEX_AUTO_RESUME_ON_BOOT=1" in banner["message"]
    assert "will NOT auto-resume" not in banner["message"]


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


def test_stop_during_live_run_with_intent_says_stops_at_phase_boundary(
    client: TestClient, intent_store: PullIntentStore
) -> None:
    """Minor 13 (review round 1) — the RUNNING-pull message arm, for real.

    A genuinely CLAIMED background run (the route's linearized
    ``request_stop()`` observes it → ``pull_running=True``) plus a wired
    intent store must yield the truthful "stops at the next phase boundary"
    wording on the JSON verdict AND the notice banner — never the idle
    "no pull was running" copy. The sibling tests each cover ONE of the two
    states (live run without an intent store / intent store while idle), so
    this combined-state pin is what actually executes the running-arm
    message under the intent-store branch.
    """
    import threading

    gate = threading.Event()
    release = threading.Event()

    def work(_hook: object) -> None:
        gate.set()
        assert release.wait(timeout=10.0), "test worker released"

    job = client.app.state.brutex_pull_job  # type: ignore[attr-defined]
    assert job.start(work, total=1) is True
    assert gate.wait(timeout=10.0), "the background run claimed + started"
    try:
        intent_store.write(_intent())
        body = client.post("/api/v1/pull/stop").json()
    finally:
        release.set()
    assert body["pull_running"] is True
    assert body["intent_cleared"] is True
    assert body["banner_set"] is True
    banner = intent_store.banner.snapshot()
    assert banner is not None
    assert "stops at the next phase boundary" in banner["message"]
    assert "no pull was running" not in banner["message"]
    # The promised stop genuinely reached the run (linearized set+read).
    assert job.stop_requested is True
    deadline = time.monotonic() + 10.0
    while job.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not job.running, "the released worker finished"


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


def test_stop_idle_no_intent_message_is_truthful(
    client: TestClient, intent_store: PullIntentStore
) -> None:
    # Minors 13/14 (review round 1): no run + no intent ⇒ the banner claims
    # NEITHER a running-pipeline stop NOR an intent clear.
    body = client.post("/api/v1/pull/stop").json()
    assert body["pull_running"] is False
    assert body["intent_cleared"] is False
    banner = intent_store.banner.snapshot()
    assert banner is not None
    assert "no pull was running" in banner["message"]
    assert "no crash-recovery intent was present" in banner["message"]
    assert "was cleared" not in banner["message"]


def test_stop_never_clobbers_a_standing_critical_banner(
    client: TestClient, intent_store: PullIntentStore
) -> None:
    # Minor 14 (review round 1): the crash-loop CRITICAL banner outranks the
    # stop acknowledgement — the notice is withheld (banner_set False) while
    # the JSON verdict still carries the whole truth.
    intent_store.write(_intent())
    intent_store.banner.set(level="critical", message="pull auto-resume HALTED: crash loop")
    body = client.post("/api/v1/pull/stop").json()
    assert body["stop_requested"] is True
    assert body["intent_cleared"] is True
    assert body["banner_set"] is False
    banner = intent_store.banner.snapshot()
    assert banner is not None
    assert banner["level"] == "critical"
    assert "crash loop" in banner["message"]


def test_stop_wording_names_the_legacy_arm_that_can_still_refire(
    client: TestClient, intent_store: PullIntentStore
) -> None:
    """MAJOR 1.5 (review round 1) — the legacy-arm exception, demonstrated.

    The stop kills the INTENT arm (pinned by the sibling no-refire test), but
    the LEGACY ledger-triggered arm — under its explicit double opt-in
    (`BRUTEX_AUTO_RESUME=1` per the separately-merged PR #1407 master gate,
    AND `BRUTEX_AUTO_RESUME_ON_BOOT=1`, modelled here by `enabled=True`) —
    re-fires a whole-universe pull off outstanding FAILED/PLANNED ledger
    windows. Every stop surface must therefore NAME the exception instead of
    the old unconditional "next boot will NOT auto-resume" claim.
    """
    from brutex.dashboard.pull_intent import run_legacy_resume
    from brutex.orchestrator.auto_resume import ResumeVerdict, maybe_resume_on_boot

    intent_store.write(_intent())
    body = client.post("/api/v1/pull/stop").json()
    assert intent_store.load() is None, "the intent arm is dead"
    # Every surface names the legacy-arm exception + both env vars.
    assert "BRUTEX_AUTO_RESUME=1 AND BRUTEX_AUTO_RESUME_ON_BOOT=1" in body["note"]
    banner = intent_store.banner.snapshot()
    assert banner is not None
    assert "BRUTEX_AUTO_RESUME=1 AND BRUTEX_AUTO_RESUME_ON_BOOT=1" in banner["message"]

    # The demonstrated re-fire: outstanding ledger windows + the enabled
    # legacy arm ⇒ a whole-universe launch fires on the next boot.
    class _Counts:
        total = 3
        planned = 1
        failed = 1
        settled = 1

    class _Ledger:
        def progress_counts(self) -> _Counts:
            return _Counts()

    launched: list[object] = []

    def _legacy_resume(_inc: object) -> None:
        # `maybe_resume_on_boot` expects a None-returning resume hook; the
        # legacy arm's own ResumeAction verdict is not consulted here.
        run_legacy_resume(intent_store, launch=launched.append, probe_singleflight=lambda: True)

    verdict = maybe_resume_on_boot(
        load_ledger=_Ledger,
        resume=_legacy_resume,
        enabled=True,  # the operator's explicit legacy opt-in
        intent_present=lambda: intent_store.load() is not None,
        resume_intent=lambda: None,
    )
    assert verdict is ResumeVerdict.RESUMED
    assert len(launched) == 1, "the legacy arm re-fired despite the stop — as documented"
