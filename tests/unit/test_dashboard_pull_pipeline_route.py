"""The ``/api/v1/universe/pull-pipeline`` route (DEC-ONE-CLICK-PIPELINE-001).

The one-click "▶ Pull EVERYTHING" hero button chains spot → metadata → historical
over the whole admitted universe on the single-flight background job. This covers
the route glue — the full-universe target selection, the background-job start, and
the response envelope — with the three pull spines replaced by OFFLINE fakes (the
REAL report dataclasses, never a network call; Golden Rule #1). A real
DuckDB-backed store + a ``TestClient``, creds pinned dry-run. Skipped cleanly when
the ``[ui]`` extra (FastAPI) or duckdb is absent.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
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
def client(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[TestClient]:
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


def _force_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "groww_credentials_present", lambda: False)


def _fake_spot(*args: Any, **kwargs: Any) -> BulkSpotReport:
    """Offline stand-in for ``pull_all_spot`` — clean, no network."""
    return BulkSpotReport(
        dry_run=kwargs["dry_run"],
        start_date="2020-01-01",
        end_date="2026-06-20",
        symbols=[SymbolPullResult(symbol="NIFTY", completed=2, failed=0, no_data=0)],
    )


def _fake_metadata(*args: Any, **kwargs: Any) -> MetadataPullReport:
    """Offline stand-in for ``pull_expiries_contracts`` — clean, no network."""
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
    """Offline stand-in for ``pull_historical_for_underlyings`` — clean, no network."""
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
    """Wire all three offline pull fakes so the route never touches the network."""
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


def test_pull_pipeline_starts_full_universe_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    _patch_spines(monkeypatch)
    resp = client.post("/api/v1/universe/pull-pipeline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is True
    assert body["dry_run"] is True
    assert body["targets"] >= 1
    # The background job runs all three offline phases to a clean settle.
    snap = _wait_for_job(client)
    assert snap["state"] == "done"


def test_pull_pipeline_completes_and_records_verdict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    _patch_spines(monkeypatch)
    resp = client.post("/api/v1/universe/pull-pipeline")
    assert resp.status_code == 200
    snap = _wait_for_job(client)
    assert snap["state"] == "done"
    # The finished spot verdict is surfaced to the verdict banner endpoint.
    verdict = client.get("/api/v1/universe/pull-verdict").json()
    assert isinstance(verdict, dict)
    assert verdict["present"] is True


# ── selection-scoped pipeline (DEC-PULL-SELECTED-PIPELINE-001) ────────────────


def test_pull_pipeline_no_selection_is_whole_universe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No ?symbols= / ?symbol= → the whole admitted universe (backward-compatible
    # with the "▶ Pull EVERYTHING" hero); target_source echoes "universe".
    _force_dry_run(monkeypatch)
    _patch_spines(monkeypatch)
    body = client.post("/api/v1/universe/pull-pipeline").json()
    assert body["started"] is True
    assert body["target_source"] == "universe"
    assert _wait_for_job(client)["state"] == "done"


def test_pull_pipeline_symbols_scopes_to_selection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ?symbols= the ticked id → scoped to the selection (one name here);
    # target_source echoes "selection", and the chain still settles clean offline.
    _force_dry_run(monkeypatch)
    _patch_spines(monkeypatch)
    body = client.post("/api/v1/universe/pull-pipeline", params={"symbols": "NIFTY"}).json()
    assert body["started"] is True
    assert body["targets"] == 1
    assert body["target_source"] == "selection"
    assert _wait_for_job(client)["state"] == "done"


def test_pull_pipeline_single_symbol_scopes_to_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_dry_run(monkeypatch)
    _patch_spines(monkeypatch)
    body = client.post("/api/v1/universe/pull-pipeline", params={"symbol": "NIFTY"}).json()
    assert body["targets"] == 1
    assert body["target_source"] == "single"
    assert _wait_for_job(client)["state"] == "done"


def test_pull_pipeline_unknown_selection_fails_closed_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unknown id never silently pulls a partial set — it fails CLOSED with a 400
    # naming the offender (Golden Rule #1; DEC-PULL-SELECTED-PIPELINE-001).
    _force_dry_run(monkeypatch)
    _patch_spines(monkeypatch)
    resp = client.post("/api/v1/universe/pull-pipeline", params={"symbols": "99999"})
    assert resp.status_code == 400
    assert "99999" in resp.json()["detail"]
