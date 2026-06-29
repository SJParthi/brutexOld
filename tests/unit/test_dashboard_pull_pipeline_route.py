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


# ── the gated FINAL pre-build stage (DEC-UNIVERSE-PULL-CONSOLIDATE-001) ────────


def test_pull_pipeline_runs_prebuild_as_final_stage_on_clean_fetch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully-successful offline pipeline runs the FINAL pre-build stage.

    The route binds ``run_prebuild`` to a materialiser driven through
    ``brutex.data.tf_autoderive.run_autoderive`` (lazily imported inside the job
    body). We spy on it: a clean fetch (the offline fakes are all clean) must invoke
    it ONCE over the pulled target(s), proving the unified pre-build runs as the
    pipeline's automatic final stage — the operator never clicks it manually.
    """
    from brutex.data import tf_autoderive

    seen: list[tuple[str, ...]] = []
    real_report = tf_autoderive.AutoderiveReport(outcomes=())

    def _spy_run_autoderive(*, symbols: Any, materialize_one: Any) -> Any:
        seen.append(tuple(symbols))
        return real_report

    monkeypatch.setattr(tf_autoderive, "run_autoderive", _spy_run_autoderive)
    _force_dry_run(monkeypatch)
    _patch_spines(monkeypatch)
    resp = client.post("/api/v1/universe/pull-pipeline")
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    # The FINAL pre-build stage ran exactly once over the pulled target (NIFTY).
    assert len(seen) == 1, "the pre-build stage must run once on a clean fetch"
    assert "NIFTY" in seen[0], "pre-build must cover the pulled target symbol"


# ── spot from/to date range (DEC-UNIVERSE-PULL-DATERANGE-001) ─────────────────


def _patch_spines_capturing(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Wire the offline fakes but RECORD each phase's kwargs.

    Returns ``(spot_calls, metadata_calls)`` — the kwargs the route threaded into
    ``pull_all_spot`` / ``pull_expiries_contracts`` so a test can assert the
    operator-picked dates (or their absence) reached the right phase.
    """
    spot_calls: list[dict[str, Any]] = []
    metadata_calls: list[dict[str, Any]] = []

    def _spot(*args: Any, **kwargs: Any) -> BulkSpotReport:
        spot_calls.append(dict(kwargs))
        return _fake_spot(*args, **kwargs)

    def _metadata(*args: Any, **kwargs: Any) -> MetadataPullReport:
        metadata_calls.append(dict(kwargs))
        return _fake_metadata(*args, **kwargs)

    monkeypatch.setattr(app_module, "pull_all_spot", _spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)
    return spot_calls, metadata_calls


def test_pull_pipeline_blank_dates_thread_none_to_spot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blank from/to (the default) ⇒ the route passes ``None`` to the spot phase.

    DEFAULT-PRESERVATION (DEC-UNIVERSE-PULL-DATERANGE-001): a no-date pull must be
    byte-identical to the pre-picker behaviour — ``pull_all_spot`` receives
    ``from_date=None`` / ``to_date=None`` so its own ``resolve_candle_range``
    resolves the canonical ``[2020-01-01, previous trading day]`` default, and the
    metadata-year grid spans the full 2020 → current-year floor.
    """
    _force_dry_run(monkeypatch)
    spot_calls, metadata_calls = _patch_spines_capturing(monkeypatch)
    resp = client.post("/api/v1/universe/pull-pipeline")
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    assert len(spot_calls) == 1
    assert spot_calls[0]["from_date"] is None
    assert spot_calls[0]["to_date"] is None
    # Full year grid (2020 → current year) when no bound is given.
    assert metadata_calls[0]["years"][0] == 2020


def test_pull_pipeline_explicit_dates_thread_to_spot_and_metadata(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit valid from/to ⇒ threaded to the spot phase AND narrow the year grid.

    The spot phase receives the exact picked strings; the metadata-year grid is
    narrowed to those bounds' YEARS (the grid is year-granular) — so a 2021→2022
    range pulls metadata for exactly [2021, 2022].
    """
    _force_dry_run(monkeypatch)
    spot_calls, metadata_calls = _patch_spines_capturing(monkeypatch)
    resp = client.post(
        "/api/v1/universe/pull-pipeline",
        params={"from_date": "2021-03-01", "to_date": "2022-04-15"},
    )
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    assert spot_calls[0]["from_date"] == "2021-03-01"
    assert spot_calls[0]["to_date"] == "2022-04-15"
    # The metadata-year grid is narrowed to the picked bounds' years.
    assert list(metadata_calls[0]["years"]) == [2021, 2022]


def test_pull_pipeline_reversed_dates_is_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A from_date AFTER to_date is a clean 400, never a silent empty pull."""
    _force_dry_run(monkeypatch)
    _patch_spines(monkeypatch)
    resp = client.post(
        "/api/v1/universe/pull-pipeline",
        params={"from_date": "2023-06-01", "to_date": "2022-01-01"},
    )
    assert resp.status_code == 400


def test_pull_pipeline_below_2020_from_date_is_clamped_not_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A from_date BELOW the 2020 floor is clamped up (resolves fine), not a 400.

    ``resolve_candle_range`` clamps ``start`` up to ``GROWW_HISTORY_FLOOR``; only an
    impossible window (start > end) raises. A 2019 start with a valid end clamps to
    2020 and pulls — so the route accepts it and threads the raw string to the spot
    phase (the phase's own ``resolve_candle_range`` re-clamps identically).
    """
    _force_dry_run(monkeypatch)
    spot_calls, _ = _patch_spines_capturing(monkeypatch)
    resp = client.post(
        "/api/v1/universe/pull-pipeline",
        params={"from_date": "2019-01-01", "to_date": "2021-01-01"},
    )
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    assert spot_calls[0]["from_date"] == "2019-01-01"


def test_pull_pipeline_non_iso_date_is_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-ISO date string is a clean 400 (validated eagerly, never queued)."""
    _force_dry_run(monkeypatch)
    _patch_spines(monkeypatch)
    resp = client.post(
        "/api/v1/universe/pull-pipeline",
        params={"from_date": "01-03-2021"},
    )
    assert resp.status_code == 400


def test_pull_pipeline_whitespace_dates_behave_as_blank(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whitespace-only date params behave byte-identically to blank (200, not 500).

    ONE definition of "blank" (DEC-UNIVERSE-PULL-DATERANGE-001): a whitespace-only
    value is normalized to ``None`` at the top of the route, so it threads ``None``
    to the spot phase + the full 2020-origin metadata-year grid — exactly like an
    omitted param. Without the normalization the two helpers disagreed
    (``resolve_candle_range`` stripped ``"  "`` to None but ``_resolve_metadata_years``
    did not → ``date.fromisoformat("  ")`` raised → a 500). Unreachable from the
    date-input UI, but ``/pull-pipeline`` is a public API.
    """
    _force_dry_run(monkeypatch)
    spot_calls, metadata_calls = _patch_spines_capturing(monkeypatch)
    resp = client.post(
        "/api/v1/universe/pull-pipeline",
        params={"from_date": "  ", "to_date": " "},
    )
    assert resp.status_code == 200, "whitespace dates must NOT 500 — normalized to blank"
    assert _wait_for_job(client)["state"] == "done"
    # Threaded as blank: None to the spot phase + the full 2020-origin year grid.
    assert spot_calls[0]["from_date"] is None
    assert spot_calls[0]["to_date"] is None
    assert metadata_calls[0]["years"][0] == 2020
