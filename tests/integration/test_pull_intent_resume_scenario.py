# pyright: reportPrivateUsage=none
"""End-to-end pull-intent resume scenarios (DEC-PULL-INTENT-RESUME-001).

The crash-recovery story through REAL ``build_app`` instances + ``TestClient``
with offline fake pull spines (the real report dataclasses — never a network
call), a REAL ``PullIntentStore`` and the REAL account-wide single-flight
``fcntl.flock`` on a tmp lake root:

* a SIGKILL-survivor intent (the durable file a dead process leaves behind —
  the kernel released its ``flock``) re-fires the SAME (symbols, from, to)
  quadruple on a FRESH app, and the resumed run is the FULL pipeline: the
  automatic timeframe PREBUILD stage runs, the movers warm fires after it, the
  ``/api/v1/movers`` screen ends NOT gated, and the intent clears on completion
  — the correction-(b) success criteria, never a "movers gated" placeholder;
* while a PEER app's pull genuinely holds the flock, a sibling boot's resume
  refuses WITHOUT burning a restart token — and once the peer completes (its
  ``on_finally`` clears the shared intent), the next boot finds nothing to
  resume (the work was done).
"""

from __future__ import annotations

import importlib
import threading
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.config.credentials import ENV_CREDENTIALS_FILE
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.movers_warm import MoversWarmResult
from brutex.dashboard.pipeline_prebuild import PrebuildOutcome
from brutex.dashboard.pull_intent import PullIntent, PullIntentStore
from brutex.data.instrument_store import InstrumentStore
from fastapi.testclient import TestClient

from tests.unit.test_dashboard_pull_intent_route import (
    _fake_historical,
    _fake_metadata,
    _fake_spot,
    _nifty,
    _patch_spines,
    _pin_creds_present,
    _wait_for,
    _wait_for_job,
)

app_module = importlib.import_module("brutex.dashboard.app")


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
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    return PullIntentStore(lake)


def _fresh_client(store: InstrumentStore, intent_store: PullIntentStore) -> TestClient:
    app = build_app(DashboardState(), instrument_store=store, pull_intent_store=intent_store)
    return TestClient(app)


def test_sigkill_survivor_intent_resumes_full_pipeline_on_fresh_app(
    store: InstrumentStore, intent_store: PullIntentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole story: a dead pull's intent → a fresh boot re-fires the FULL
    pipeline (prebuild ran, movers warmed after it, /movers NOT gated, same
    quadruple, intent cleared) — the correction-(b) success criteria."""
    _pin_creds_present(monkeypatch)
    spot_calls: list[dict[str, Any]] = []
    prebuilds: list[Any] = []
    warmed: list[int] = []

    def _spot(*args: Any, **kwargs: Any) -> Any:
        spot_calls.append(dict(kwargs))
        return _fake_spot(*args, **kwargs)

    def _spy_prebuild(**kwargs: Any) -> PrebuildOutcome:
        prebuilds.append(kwargs)
        return PrebuildOutcome(symbols_built=1, total_bars=9, failed=(), failed_count=0, ok=True)

    def _spy_warm(jobs: Any, **_kwargs: Any) -> MoversWarmResult:
        # DEC-MOVERS-WARM-PROGRESS-001 (MIGRATION.md #1290 pack): the real
        # warm_movers_snapshots takes segment/labels/on_progress kwargs and
        # returns the per-coordinate tally, not an int.
        warmed.append(len(list(jobs)))
        return MoversWarmResult(warmed=0, failed=0, total=0)

    monkeypatch.setattr(app_module, "pull_all_spot", _spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)
    monkeypatch.setattr(app_module, "run_pipeline_prebuild_guarded", _spy_prebuild)
    monkeypatch.setattr(app_module, "warm_movers_snapshots", _spy_warm)
    # An empty tmp lake has no spot day — pin the warm-target date so the warm
    # closures reach the (spied) warmer instead of early-returning 0.
    monkeypatch.setattr(app_module, "latest_movers_date", lambda _root: "2024-01-15")

    # The SIGKILL survivor: the durable file a dead pull left behind (its
    # in-process on_finally never ran; the kernel released its flock).
    intent_store.write(
        PullIntent(symbols="NIFTY", symbol=None, from_date="2024-01-01", to_date="2024-02-01")
    )
    with _fresh_client(store, intent_store) as client:
        client.app.state.brutex_resume_pipeline()  # type: ignore[attr-defined]
        assert _wait_for_job(client)["state"] == "done"
        # (b) success criteria — the resumed run is the FULL pipeline:
        assert len(prebuilds) == 1, "the automatic timeframe PREBUILD stage must run"
        assert len(warmed) >= 1, "the movers warm must fire AFTER the prebuild"
        movers = client.get("/api/v1/movers").json()
        assert movers.get("gated") is not True, "the resumed run must never end movers-gated"
        # The SAME persisted quadruple reached the spot phase.
        assert spot_calls[0]["from_date"] == "2024-01-01"
        assert spot_calls[0]["to_date"] == "2024-02-01"
        assert spot_calls[0]["dry_run"] is False
        # The completed resume cleared the durable intent.
        _wait_for(lambda: not intent_store.path.exists())
        # ANNOUNCE-ON-LAUNCH (DEC-AUTO-RESUME-ZERO-SURPRISES-001): the
        # successful resume is no longer silent — the NON-critical amber
        # notice names the launch (supersedes the pre-fix "no banner on
        # success" pin; the next real pull's clear_banner_after_start or a
        # newer payload retires it).
        banner = intent_store.banner.snapshot()
        assert banner is not None
        assert banner["level"] == "notice"
        assert "pull auto-resume LAUNCHED" in banner["message"]


def test_peer_running_pull_defers_resume_then_completion_clears_the_intent(
    store: InstrumentStore, intent_store: PullIntentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a PEER app's pull holds the real flock, a sibling boot's resume is
    refused with NO token burnt; the peer's completion clears the shared
    intent, so the next boot honestly finds nothing to resume."""
    _pin_creds_present(monkeypatch)
    gate = threading.Event()

    def _blocking_spot(*args: Any, **kwargs: Any) -> Any:
        opened = gate.wait(timeout=10.0)
        assert opened
        return _fake_spot(*args, **kwargs)

    monkeypatch.setattr(app_module, "pull_all_spot", _blocking_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)

    with _fresh_client(store, intent_store) as peer:
        peer_resp = peer.post("/api/v1/universe/pull-pipeline", params={"symbols": "NIFTY"})
        assert peer_resp.json()["started"] is True
        # The peer's pull is mid-flight: the intent is durable + the flock held.
        assert intent_store.path.exists()
        with _fresh_client(store, intent_store) as sibling:
            sibling.app.state.brutex_resume_pipeline()  # type: ignore[attr-defined]
            surviving = intent_store.load()
            assert surviving is not None
            assert surviving.restarts == 0, "a live peer must not cost a restart token"
            assert sibling.get("/api/v1/universe/pull-progress").json()["state"] == "idle"
        gate.set()
        assert _wait_for_job(peer)["state"] == "done"
        _wait_for(lambda: not intent_store.path.exists())

    # The next boot: nothing to resume — the work was completed by the peer.
    with _fresh_client(store, intent_store) as next_boot:
        _patch_spines(monkeypatch)
        next_boot.app.state.brutex_resume_pipeline()  # type: ignore[attr-defined]
        time.sleep(0.05)
        assert next_boot.get("/api/v1/universe/pull-progress").json()["state"] == "idle"
        assert intent_store.banner.snapshot() is None
