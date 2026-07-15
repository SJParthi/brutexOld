"""The NG-2 launcher intent-gate PIN (DEC-VENDOR-PROFILE-REGISTRY-001, WS-C).

ADR-0021 §2.5 ``.pull_intent`` row + fold #4 NG2-EDGE, driven through the
REAL shared launcher (``build_app`` + a REAL ``POST /pull-pipeline`` click
with the three pull spines replaced by offline fakes — never a chain-only
fake): a ``no_intent_persist`` (feed) profile's launch binds NO primary
intent store via :func:`~brutex.dashboard.vendor_profile.launch_intent_store`,
so the launcher structurally skips the primary ``launch_mutex`` + the persist
+ the clear/restore hooks. Pinned BOTH ways:

* a PRIOR Groww intent on disk survives BYTE-IDENTICAL through a feed-gated
  launch (arm A) and NO intent file is created when none existed (arm B);
* the primary store's ``launch_mutex`` is NEVER ENTERED by the feed-gated
  launch (a recording mutex on the real store object);
* the two-vendor crash-resume half this seam carries: after the feed-gated
  launch the primary singleton holds ONLY the Groww-persisted scope (arm A's
  byte-identity IS that pin — a boot-resume replays the Groww scope, never a
  feed-written one; the feed v1 chain, store-less, resumes nothing);
* the CONTROL arm: the SAME app wiring with the PRIMARY profile's gated
  store (identity) persists + clears the intent exactly like today —
  the A/B evidence that a Groww-only wiring is byte-identical.

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
from types import TracebackType
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.config.credentials import ENV_CREDENTIALS_FILE
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.composite_pull import CompositePullReport
from brutex.dashboard.historical_pull import HistoricalPullReport
from brutex.dashboard.metadata_pull import MetadataPullReport
from brutex.dashboard.pull_controller import BulkSpotReport, SymbolPullResult
from brutex.dashboard.pull_intent import PULL_INTENT_FILENAME, PullIntent, PullIntentStore
from brutex.dashboard.vendor_profile import groww_pull_profile, launch_intent_store
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

from tests.fakes.vendor_profiles import synthetic_feed_profile

app_module = importlib.import_module("brutex.dashboard.app")


class RecordingMutex:
    """A context-manager probe counting every ``launch_mutex`` entry."""

    def __init__(self) -> None:
        self.entries = 0
        self._lock = threading.Lock()

    def __enter__(self) -> RecordingMutex:
        self.entries += 1
        self._lock.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._lock.release()


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


def _fake_spot(*_args: Any, **kwargs: Any) -> BulkSpotReport:
    return BulkSpotReport(
        dry_run=kwargs["dry_run"],
        start_date="2020-01-01",
        end_date="2026-07-14",
        symbols=[SymbolPullResult(symbol="NIFTY", completed=2, failed=0, no_data=0)],
    )


def _fake_metadata(*_args: Any, **kwargs: Any) -> MetadataPullReport:
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


def _fake_historical(*_args: Any, **kwargs: Any) -> CompositePullReport:
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


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()], snapshot_date=date(2026, 7, 14), run_id="r1", csv_source_sha256="0" * 64
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    lake = tmp_path / "lake"
    lake.mkdir()
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(lake))
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    return lake


def _patch_spines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "groww_credentials_error", lambda: None)
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


def _feed_gated_client(
    store: InstrumentStore, lake: Path, primary_store: PullIntentStore
) -> TestClient:
    """Build the REAL app with the FEED profile's gated store (= ``None``)."""
    gated = launch_intent_store(synthetic_feed_profile(lake), primary_store)
    assert gated is None  # the NG-2 seam law — the launcher binds NO store
    return TestClient(build_app(DashboardState(), instrument_store=store, pull_intent_store=gated))


class TestFeedGatedLaunch:
    """A feed-gated launch through the REAL shared launcher (both arms + mutex)."""

    def test_prior_groww_intent_survives_byte_identical_and_mutex_untouched(
        self, store: InstrumentStore, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_spines(monkeypatch)
        primary_store = PullIntentStore(lake)
        mutex = RecordingMutex()
        primary_store.launch_mutex = mutex
        # Arm A seed: a PRIOR Groww-launcher-persisted intent on disk.
        prior = PullIntent(
            symbols="NIFTY",
            symbol=None,
            from_date="2024-01-01",
            to_date="2024-02-01",
            restarts=1,
            created_at="2026-07-14T18:00:00+00:00",
            scope=None,
        )
        assert primary_store.write(prior)
        intent_path = lake / PULL_INTENT_FILENAME
        before = intent_path.read_bytes()

        with _feed_gated_client(store, lake, primary_store) as client:
            resp = client.post("/api/v1/universe/pull-pipeline", params={"symbols": "NIFTY"})
            assert resp.status_code == 200
            assert resp.json()["started"] is True
            snap = _wait_for_job(client)
            assert snap["state"] == "done"

        # Arm A: the primary singleton is BYTE-UNTOUCHED (a crash+boot would
        # resume the GROWW scope only — the two-vendor crash-resume half).
        assert intent_path.read_bytes() == before
        # The primary launch mutex was NEVER ENTERED by the feed-gated launch.
        assert mutex.entries == 0

    def test_no_prior_intent_none_created(
        self, store: InstrumentStore, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_spines(monkeypatch)
        primary_store = PullIntentStore(lake)
        mutex = RecordingMutex()
        primary_store.launch_mutex = mutex
        intent_path = lake / PULL_INTENT_FILENAME
        assert not intent_path.exists()

        with _feed_gated_client(store, lake, primary_store) as client:
            resp = client.post("/api/v1/universe/pull-pipeline", params={"symbols": "NIFTY"})
            assert resp.status_code == 200
            assert resp.json()["started"] is True
            _wait_for_job(client)

        # Arm B: no intent file materialises + the mutex stays untouched.
        assert not intent_path.exists()
        assert mutex.entries == 0


class TestPrimaryGatedLaunchControl:
    """The CONTROL arm: the primary profile's gated store IS today's wiring."""

    def test_primary_gate_persists_and_clears_like_today(
        self, store: InstrumentStore, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_spines(monkeypatch)
        primary_store = PullIntentStore(lake)
        mutex = RecordingMutex()
        primary_store.launch_mutex = mutex
        gated = launch_intent_store(groww_pull_profile(lake), primary_store)
        assert gated is primary_store  # identity — byte-identical wiring
        app = build_app(DashboardState(), instrument_store=store, pull_intent_store=gated)
        with TestClient(app) as client:
            resp = client.post("/api/v1/universe/pull-pipeline", params={"symbols": "NIFTY"})
            assert resp.status_code == 200
            assert resp.json()["started"] is True
            snap = _wait_for_job(client)
            assert snap["state"] == "done"
        # The launcher entered the primary mutex (the legacy transaction) and
        # the completed job's on_finally CLEARED the persisted intent.
        assert mutex.entries >= 1
        assert not (lake / PULL_INTENT_FILENAME).exists()
