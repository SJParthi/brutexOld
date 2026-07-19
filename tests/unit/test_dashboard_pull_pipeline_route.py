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
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.adapters.groww.symbol_form_preference import (
    PersistentSymbolFormPreference,
    form_winner_key,
)
from brutex.config.credentials import ENV_CREDENTIALS_FILE
from brutex.config.feed_scope import FEED_SCOPE_ENV as _FEED_SCOPE_ENV_1934
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.composite_pull import CompositePullReport
from brutex.dashboard.greeks_health import DEFAULT_GREEKS_FAILURE_THRESHOLD
from brutex.dashboard.historical_pull import HistoricalPullReport
from brutex.dashboard.metadata_pull import MetadataPullReport
from brutex.dashboard.prebuild_gate import PrebuildGate
from brutex.dashboard.pull_controller import BulkSpotReport, SymbolPullResult
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

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


def _pin_creds_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the credential gate OPEN (DEC-PULL-LOUD-CREDENTIAL-REFUSAL-001).

    These tests exercise pipeline sequencing/validation over fully-faked
    spines, not the credential gate (which has its own dedicated suite);
    pinning ``groww_credentials_error`` to ``None`` keeps every request
    deterministic regardless of the ambient environment. The old
    ``_force_dry_run`` (creds-absent → silent synthetic downgrade) is gone:
    a creds-absent real pull now REFUSES with a 503.
    """
    monkeypatch.setattr(app_module, "groww_credentials_error", lambda: None)


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
    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    resp = client.post("/api/v1/universe/pull-pipeline", params={"dry_run": "true"})
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
    _pin_creds_present(monkeypatch)
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
    _pin_creds_present(monkeypatch)
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
    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    body = client.post("/api/v1/universe/pull-pipeline", params={"symbols": "NIFTY"}).json()
    assert body["started"] is True
    assert body["targets"] == 1
    assert body["target_source"] == "selection"
    assert _wait_for_job(client)["state"] == "done"


def test_pull_pipeline_single_symbol_scopes_to_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    body = client.post("/api/v1/universe/pull-pipeline", params={"symbol": "NIFTY"}).json()
    assert body["targets"] == 1
    assert body["target_source"] == "single"
    assert _wait_for_job(client)["state"] == "done"


def test_pull_pipeline_launch_runs_off_the_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Round-1 review fold (the #1522 event-loop-freeze class): the launch body
    # acquires the cross-process pull guards SYNCHRONOUSLY (two flock opens +
    # the lease read + an fsync'd lease write, all lake-volume I/O), so it must
    # run on an AnyIO worker via run_in_threadpool — never on the uvicorn event
    # loop, where a wedged external volume would freeze the WHOLE dashboard.
    # FAILS BEFORE the fix: the route called _launch_pipeline inline in the
    # coroutine, so get_running_loop() succeeded inside start().
    import asyncio

    from brutex.dashboard.pull_jobs import BackgroundPullJob

    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    on_loop: list[bool] = []

    def _recording_start(self: BackgroundPullJob, *args: Any, **kwargs: Any) -> bool:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)  # executing ON the event loop — the freeze hazard
        except RuntimeError:
            on_loop.append(False)  # a threadpool worker — offloaded correctly
        return True

    monkeypatch.setattr(BackgroundPullJob, "start", _recording_start)
    resp = client.post("/api/v1/universe/pull-pipeline", params={"dry_run": "true"})
    assert resp.status_code == 200
    assert resp.json()["started"] is True
    assert on_loop == [False], (
        "the pull launch (singleflight flock + lease lake I/O) must be offloaded "
        "via run_in_threadpool, never executed on the event loop"
    )


def test_pull_pipeline_pre_first_beat_window_reads_running_and_refuses_second_start(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The STRICT pre-first-beat launch window reads ``running`` — never idle.

    Pins the launch-baseline liveness class found (and fixed) on the SWEEP
    surface: a just-launched job's initial progress baseline omitted the
    freshness stamp a downstream staleness gate keyed on, so between the
    launch POST returning and the FIRST real progress beat the job read as
    DEAD/idle (Stop invisible, cancel refused) — potentially for hours on a
    heavy first unit. On the PULL surface the class is structurally ABSENT
    by design: ``BackgroundPullJob.start`` stamps ``PullProgress.start()``
    (``state=running``) SYNCHRONOUSLY under its lock before the launch
    returns, and neither ``/pull-progress`` nor the frontend's
    ``s.state === 'running'`` gate applies any wall-clock freshness
    comparison (universe.html deliberately treats the snapshot's monotonic
    ``updated_at`` as useless for staleness). This test drives the REAL
    route pair — POST ``/pull-pipeline`` with the spot spine PARKED before
    any window settles/beats, then GET ``/pull-progress`` — so the window
    is exercised end-to-end, never via a hand-built snapshot. There is
    deliberately NO pull cancel route; the only control surface in the
    window is the single-flight refusal of a second start, asserted here.
    """
    _pin_creds_present(monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    def _parked_spot(*args: Any, **kwargs: Any) -> BulkSpotReport:
        # Park INSIDE the first phase, before any window settles or any
        # progress beat fires — the strict pre-first-beat window by
        # construction.
        entered.set()
        assert release.wait(timeout=30.0), "harness never released the parked worker"
        return _fake_spot(*args, **kwargs)

    monkeypatch.setattr(app_module, "pull_all_spot", _parked_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)

    try:
        resp = client.post("/api/v1/universe/pull-pipeline")
        assert resp.status_code == 200
        assert resp.json()["started"] is True

        # (a) The instant the POST returns — the worker thread may not even
        # be scheduled yet; zero beats have fired. The progress route must
        # already read running (state stamped synchronously in start()).
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert snap["state"] == "running", snap
        assert snap["running"] is True

        # (b) Deeper into the window: the worker is provably parked inside
        # the spot phase, still strictly before the first beat.
        assert entered.wait(timeout=10.0), "worker never reached the spot spine"
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert snap["state"] == "running", snap
        assert snap["running"] is True
        assert snap["stalled"] is False  # no false stall-suspect at launch

        # (c) The single-flight refusal — the pull surface's only control
        # gate — keys on the same synchronously-stamped state: a second
        # launch inside the window is refused, never a phantom "idle".
        second = client.post("/api/v1/universe/pull-pipeline").json()
        assert second["started"] is False
    finally:
        release.set()

    settled = _wait_for_job(client)
    assert settled["state"] == "done"


def test_pull_pipeline_unknown_selection_fails_closed_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unknown id never silently pulls a partial set — it fails CLOSED with a 400
    # naming the offender (Golden Rule #1; DEC-PULL-SELECTED-PIPELINE-001).
    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    resp = client.post("/api/v1/universe/pull-pipeline", params={"symbols": "99999"})
    assert resp.status_code == 400
    assert "99999" in resp.json()["detail"]


# ── the gated auto-prebuild stage (DEC-PIPELINE-AUTOPREBUILD-001) ──────────────


def test_pull_pipeline_runs_prebuild_after_clean_fetch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully-successful offline pipeline runs the automatic prebuild stage.

    The route binds ``run_prebuild`` to the pure crash-guarded
    ``run_pipeline_prebuild_guarded`` (DEC-PIPELINE-AUTOPREBUILD-001 — the old
    ``run_autoderive`` binding is gone). We spy on the app-module binding: a
    clean fetch (the offline fakes are all clean) must invoke it ONCE over the
    pulled target(s) with the target-shaped materialiser + cascade builder,
    proving the prebuild runs automatically — the operator never clicks it.
    """
    from brutex.dashboard.pipeline_prebuild import PrebuildOutcome

    seen: list[tuple[str, ...]] = []

    def _spy_prebuild(
        *, instruments: Any, cascade_or_error: Any, materialize: Any, **kwargs: Any
    ) -> PrebuildOutcome:
        seen.append(tuple(inst.symbol_name for inst in instruments))
        return PrebuildOutcome(symbols_built=1, total_bars=9, failed=(), failed_count=0, ok=True)

    monkeypatch.setattr(app_module, "run_pipeline_prebuild_guarded", _spy_prebuild)
    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    resp = client.post("/api/v1/universe/pull-pipeline")
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    # The prebuild stage ran exactly once over the pulled target (NIFTY).
    assert len(seen) == 1, "the pre-build stage must run once on a clean fetch"
    assert "NIFTY" in seen[0], "pre-build must cover the pulled target symbol"


def test_pull_pipeline_prebuild_crash_never_fails_the_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REAL guarded prebuild runs over an empty lake (NIFTY has no stored
    1-minute) — its per-symbol failure is quarantined, and the already-successful
    fetch job still settles ``done`` (the prebuild can never kill the job)."""
    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    resp = client.post("/api/v1/universe/pull-pipeline")
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"


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


def _patch_spines_capturing_historical(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Wire the offline fakes, RECORDING the spot AND historical phases' kwargs.

    Returns ``(spot_calls, historical_calls)`` so a test can assert BOTH that the
    spot phase received the operator dates verbatim AND that the F&O historical
    phase received them as the parsed EXPIRY scope (DEC-FNO-EXPIRY-SCOPE-001).
    """
    spot_calls: list[dict[str, Any]] = []
    historical_calls: list[dict[str, Any]] = []

    def _spot(*args: Any, **kwargs: Any) -> BulkSpotReport:
        spot_calls.append(dict(kwargs))
        return _fake_spot(*args, **kwargs)

    def _historical(*args: Any, **kwargs: Any) -> CompositePullReport:
        historical_calls.append(dict(kwargs))
        return _fake_historical(*args, **kwargs)

    monkeypatch.setattr(app_module, "pull_all_spot", _spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _historical)
    return spot_calls, historical_calls


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
    _pin_creds_present(monkeypatch)
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
    _pin_creds_present(monkeypatch)
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
    _pin_creds_present(monkeypatch)
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
    _pin_creds_present(monkeypatch)
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
    _pin_creds_present(monkeypatch)
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
    _pin_creds_present(monkeypatch)
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


# ── F&O expiry scoping via the operator dates (DEC-FNO-EXPIRY-SCOPE-001) ─────


def test_pull_pipeline_explicit_dates_spot_verbatim_and_historical_expiry_scope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Permutation (i): SPOT still gets from/to VERBATIM; historical gets the SCOPE.

    Explicit valid dates thread UNCHANGED to the spot phase (the pre-existing
    DEC-UNIVERSE-PULL-DATERANGE-001 contract — pinned so the scoping change can
    never disturb it) AND reach the F&O historical phase as the PARSED expiry
    scope bounds (``scope_start`` / ``scope_end`` as ``date`` objects) — never as
    the window-reshaping ``from_date`` / ``to_date``.
    """
    _pin_creds_present(monkeypatch)
    spot_calls, historical_calls = _patch_spines_capturing_historical(monkeypatch)
    resp = client.post(
        "/api/v1/universe/pull-pipeline",
        params={"from_date": "2021-03-01", "to_date": "2022-04-15"},
    )
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    # SPOT: the raw strings, verbatim (unchanged by the scoping feature).
    assert spot_calls[0]["from_date"] == "2021-03-01"
    assert spot_calls[0]["to_date"] == "2022-04-15"
    # HISTORICAL: the parsed EXPIRY scope — and NO window-reshaping dates.
    assert len(historical_calls) == 1
    assert historical_calls[0]["scope_start"] == date(2021, 3, 1)
    assert historical_calls[0]["scope_end"] == date(2022, 4, 15)
    assert "from_date" not in historical_calls[0]
    assert "to_date" not in historical_calls[0]


def test_pull_pipeline_blank_dates_thread_unbounded_scope_to_historical(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blank dates ⇒ the historical phase gets an UNBOUNDED (None/None) expiry scope.

    DEFAULT-PRESERVATION (DEC-FNO-EXPIRY-SCOPE-001): a no-date pull threads
    ``scope_start=None`` / ``scope_end=None`` — byte-identical to the unscoped
    pull (every expiry in scope).
    """
    _pin_creds_present(monkeypatch)
    _spot_calls, historical_calls = _patch_spines_capturing_historical(monkeypatch)
    resp = client.post("/api/v1/universe/pull-pipeline")
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    assert len(historical_calls) == 1
    assert historical_calls[0]["scope_start"] is None
    assert historical_calls[0]["scope_end"] is None


# ── live-edge expiry scope (DEC-EXPIRY-SCOPE-LIVE-EDGE-001) ───────────────────


def test_pull_pipeline_default_prefill_dates_thread_unbounded_scope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The picker's PRE-FILL defaults thread an UNBOUNDED expiry scope.

    THE live-edge bug (DEC-EXPIRY-SCOPE-LIVE-EDGE-001): the frontend sent the
    pre-filled ``[2020-01-01, prev-day]`` defaults whenever non-blank — ALWAYS —
    so every default one-click pull carried ``scope_end = prev-day`` and scoped
    OUT all live/unexpired contracts. The backend normalizer is AUTHORITATIVE:
    default-shaped bounds are lifted to ``None`` even when the frontend sends
    them, while the SPOT phase still receives the raw strings verbatim.
    """
    from brutex.dashboard.pull_controller import default_date_range

    _pin_creds_present(monkeypatch)
    spot_calls, historical_calls = _patch_spines_capturing_historical(monkeypatch)
    from_iso, to_iso = default_date_range()
    resp = client.post(
        "/api/v1/universe/pull-pipeline",
        params={"from_date": from_iso, "to_date": to_iso},
    )
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    # SPOT: the raw strings, verbatim (the spot window law is untouched).
    assert spot_calls[0]["from_date"] == from_iso
    assert spot_calls[0]["to_date"] == to_iso
    # HISTORICAL: the default-shaped bounds are LIFTED — live expiries in scope.
    assert len(historical_calls) == 1
    assert historical_calls[0]["scope_start"] is None
    assert historical_calls[0]["scope_end"] is None


def test_pull_pipeline_default_dates_keep_live_expiry_contract_ingested(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest-spy teeth: with default dates a LIVE-expiry contract IS in scope.

    Runs the REAL ``partition_contracts_by_scope`` law over a live/unexpired
    contract with EXACTLY the scope bounds the route threaded — before the
    live-edge fix the partition scoped it OUT (zero vendor calls); now it stays
    IN SCOPE, so the historical phase ingests it (DEC-EXPIRY-SCOPE-LIVE-EDGE-001).
    """
    from datetime import datetime, timedelta

    from brutex.adapters.groww.expiry_scope import partition_contracts_by_scope
    from brutex.dashboard.pull_controller import default_date_range
    from brutex.data.registry_store import ContractRow

    _pin_creds_present(monkeypatch)
    live_expiry = date.today() + timedelta(days=20)
    live_contract = ContractRow(
        exch="NSE",
        underlying="NIFTY",
        expiry_date=live_expiry,
        contract_symbol=f"NSE-NIFTY-{live_expiry.strftime('%d%b%y')}-21000-CE",
        strike=21000.0,
        side="CE",
        candle_status="none",
        last_run_id="live-edge-test",
        fetched_at=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
    )
    in_scope_counts: list[int] = []

    def _historical(*args: Any, **kwargs: Any) -> CompositePullReport:
        part = partition_contracts_by_scope(
            [live_contract],
            scope_start=kwargs["scope_start"],
            scope_end=kwargs["scope_end"],
        )
        in_scope_counts.append(len(part.in_scope))
        return _fake_historical(*args, **kwargs)

    monkeypatch.setattr(app_module, "pull_all_spot", _fake_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _historical)
    from_iso, to_iso = default_date_range()
    resp = client.post(
        "/api/v1/universe/pull-pipeline",
        params={"from_date": from_iso, "to_date": to_iso},
    )
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    assert in_scope_counts == [1], "the live-expiry contract must be IN scope (ingested)"


def test_pull_pipeline_narrowed_to_date_still_scopes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GENUINELY narrowed to_date (prev-day − 1) still scopes exactly as today.

    #1146 permutation-preservation (DEC-FNO-EXPIRY-SCOPE-001 untouched below the
    live edge): only a DEFAULT-shaped bound lifts; one day earlier passes
    through as the parsed scope bound.
    """
    from datetime import timedelta

    from brutex.dashboard.pull_controller import default_date_range

    _pin_creds_present(monkeypatch)
    _spot_calls, historical_calls = _patch_spines_capturing_historical(monkeypatch)
    narrowed = date.fromisoformat(default_date_range()[1]) - timedelta(days=1)
    resp = client.post(
        "/api/v1/universe/pull-pipeline",
        params={"from_date": "2021-03-01", "to_date": narrowed.isoformat()},
    )
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    assert historical_calls[0]["scope_start"] == date(2021, 3, 1)
    assert historical_calls[0]["scope_end"] == narrowed


def test_pull_pipeline_below_floor_from_date_scope_start_lifted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A from_date at/below the 2020 floor lifts ``scope_start`` to None.

    Behaviour-identical for the lake (nothing predates the floor) but the scope
    log stops naming a phantom bound (DEC-EXPIRY-SCOPE-LIVE-EDGE-001).
    """
    _pin_creds_present(monkeypatch)
    _spot_calls, historical_calls = _patch_spines_capturing_historical(monkeypatch)
    resp = client.post(
        "/api/v1/universe/pull-pipeline",
        params={"from_date": "2019-01-01", "to_date": "2021-01-01"},
    )
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    assert historical_calls[0]["scope_start"] is None
    assert historical_calls[0]["scope_end"] == date(2021, 1, 1)


# ── the movers PREBUILD GATE binding (DEC-PIPELINE-AUTOPREBUILD-001 chunk 2) ──


def _gated_client(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gate: PrebuildGate
) -> TestClient:
    """A TestClient whose app carries an INJECTED PrebuildGate (the build_app seam)."""
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    return TestClient(build_app(DashboardState(), instrument_store=store, prebuild_gate=gate))


def _patch_blocking_spot(
    monkeypatch: pytest.MonkeyPatch, *, still_failed_windows: int = 0
) -> tuple[threading.Event, threading.Event]:
    """Wire a spot fake that BLOCKS mid-phase until the test says proceed.

    Returns ``(entered, proceed)``: the fake sets *entered* when the pipeline is
    genuinely running its spot phase, then waits on *proceed* — so the test can
    deterministically observe the MID-RUN gate state (no sleep-and-hope timing).
    """
    entered = threading.Event()
    proceed = threading.Event()

    def _blocking_spot(*args: Any, **kwargs: Any) -> BulkSpotReport:
        entered.set()
        assert proceed.wait(timeout=10.0), "test never released the blocking spot fake"
        report = _fake_spot(*args, **kwargs)
        report.still_failed_windows = still_failed_windows
        return report

    monkeypatch.setattr(app_module, "pull_all_spot", _blocking_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)
    return entered, proceed


def test_pull_pipeline_fetch_phase_does_not_gate_movers(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REGRESSION (DEC-MOVERS-GATE-PREBUILD-SCOPE-001): the multi-hour FETCH
    phases must NOT gate the movers — only the prebuild phase does.

    Fail-before proven: on origin/main this test FAILS — the route called
    ``_prebuild_gate.begin()`` BEFORE the pipeline work, so mid-SPOT-fetch the
    gate read ``blocking=True`` and ``/movers`` returned ``gated: true`` (the
    prior ``test_pull_pipeline_gates_movers_while_running_then_releases_ok_true``
    pinned exactly that old behaviour and was rewritten into this test). After
    the fix ``begin()`` fires as the FIRST statement of ``_prebuild_timeframes``,
    so the fetch phases serve movers normally and the clean run still releases
    ``ok=True`` with ``runs == 1`` (the prebuild began exactly once)."""
    from brutex.dashboard.pipeline_prebuild import PrebuildOutcome

    _pin_creds_present(monkeypatch)
    entered, proceed = _patch_blocking_spot(monkeypatch)
    monkeypatch.setattr(
        app_module,
        "run_pipeline_prebuild_guarded",
        lambda **kwargs: PrebuildOutcome(
            symbols_built=1, total_bars=9, failed=(), failed_count=0, ok=True
        ),
    )
    gate = PrebuildGate()
    with _gated_client(store, monkeypatch, tmp_path, gate) as client:
        resp = client.post("/api/v1/universe/pull-pipeline")
        assert resp.status_code == 200
        try:
            assert entered.wait(timeout=10.0), "the pipeline job never started its spot phase"
            # MID-FETCH: the gate is IDLE and the movers route serves normally
            # (an honest non-gated payload over the empty tmp lake).
            assert gate.snapshot().blocking is False
            body = client.get("/api/v1/movers", params={"date": "2024-06-13"}).json()
            assert "gated" not in body
        finally:
            proceed.set()
        assert _wait_for_job(client)["state"] == "done"
        snap = gate.snapshot()
        assert snap.blocking is False  # released in the job's finally
        assert snap.last_ok is True  # the spied prebuild succeeded
        assert snap.runs == 1  # begin() fired exactly once — inside the prebuild


def test_pull_pipeline_gates_movers_during_prebuild_only(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """MID-PREBUILD the /movers route IS gated (honest ``gated: true`` with the
    ``phase``/``started_at``/``gated_for_s`` fields, ZERO candidate
    computation); after the clean run the gate is released ok=True and /movers
    serves normally again (DEC-MOVERS-GATE-PREBUILD-SCOPE-001)."""
    from brutex.dashboard.pipeline_prebuild import PrebuildOutcome

    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    entered = threading.Event()
    proceed = threading.Event()

    def _blocking_prebuild(**kwargs: Any) -> PrebuildOutcome:
        entered.set()
        assert proceed.wait(timeout=10.0), "test never released the blocking prebuild fake"
        return PrebuildOutcome(symbols_built=1, total_bars=9, failed=(), failed_count=0, ok=True)

    monkeypatch.setattr(app_module, "run_pipeline_prebuild_guarded", _blocking_prebuild)
    compute_calls: list[str] = []

    def _spy_spot(*args: Any, **kwargs: Any) -> Any:
        from brutex.dashboard.movers_snapshot import SpotCandidates

        if not proceed.is_set():
            # While the prebuild blocks, the gate must intercept BEFORE any scan.
            compute_calls.append("spot")
            raise AssertionError("no candidate computation may run while gated")
        # Post-release the movers route may compute again — honest empty set.
        return SpotCandidates(snapshots=(), skipped=0, date="2024-06-13")

    monkeypatch.setattr(app_module, "compute_spot_candidates", _spy_spot)
    gate = PrebuildGate()
    with _gated_client(store, monkeypatch, tmp_path, gate) as client:
        assert client.post("/api/v1/universe/pull-pipeline").status_code == 200
        try:
            assert entered.wait(timeout=10.0), "the pipeline job never reached its prebuild"
            # MID-PREBUILD: the gate blocks and the movers route answers honestly,
            # naming the phase + how long it has been gating.
            assert gate.snapshot().blocking is True
            body = client.get("/api/v1/movers", params={"date": "2024-06-13"}).json()
            assert body["gated"] is True
            assert body["phase"] == "prebuild"
            assert isinstance(body["started_at_epoch"], float)
            assert isinstance(body["gated_for_s"], float)
            assert body["gated_for_s"] >= 0.0
            assert "pull pipeline running" in body["note"]
            assert "(current stage: prebuild)" in body["note"]
            assert compute_calls == []  # ZERO lake-reading computation while gated
        finally:
            proceed.set()
        assert _wait_for_job(client)["state"] == "done"
        snap = gate.snapshot()
        assert snap.blocking is False  # released in the job's finally
        assert snap.last_ok is True
        assert snap.runs == 1
        # And the movers route serves the normal (non-gated) payload again.
        body = client.get("/api/v1/movers", params={"date": "2024-06-13"}).json()
        assert "gated" not in body


def test_pull_pipeline_spot_failure_releases_gate_movers_never_stuck(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pipeline that STOPS at the spot phase (still-failing windows) never
    gates at all (the prebuild never ran — DEC-MOVERS-GATE-PREBUILD-SCOPE-001)
    and the finally still records the honest ok=False release."""
    _pin_creds_present(monkeypatch)
    entered, proceed = _patch_blocking_spot(monkeypatch, still_failed_windows=1)
    gate = PrebuildGate()
    with _gated_client(store, monkeypatch, tmp_path, gate) as client:
        assert client.post("/api/v1/universe/pull-pipeline").status_code == 200
        try:
            assert entered.wait(timeout=10.0)
            # The fetch phase never gates (narrowed scope).
            assert gate.snapshot().blocking is False
        finally:
            proceed.set()
        assert _wait_for_job(client)["state"] == "done"
        snap = gate.snapshot()
        assert snap.blocking is False  # movers NEVER stuck behind a failed pull
        assert snap.last_ok is False  # the prebuild never ran → honest False
        assert snap.runs == 0  # begin() never fired — the gate covers only the prebuild
        # And the movers route serves the normal (non-gated) payload again.
        body = client.get("/api/v1/movers", params={"date": "2024-06-13"}).json()
        assert "gated" not in body


def test_pull_pipeline_dry_run_does_not_gate(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit dry_run pipeline writes no real data — it must NOT gate the
    movers, mid-run OR after (the gate's run counter never moves)."""
    _pin_creds_present(monkeypatch)
    entered, proceed = _patch_blocking_spot(monkeypatch)
    gate = PrebuildGate()
    with _gated_client(store, monkeypatch, tmp_path, gate) as client:
        assert (
            client.post("/api/v1/universe/pull-pipeline", params={"dry_run": "true"}).status_code
            == 200
        )
        try:
            assert entered.wait(timeout=10.0)
            # MID-RUN: not gated — /movers serves the normal payload.
            assert gate.snapshot().blocking is False
            body = client.get("/api/v1/movers", params={"date": "2024-06-13"}).json()
            assert "gated" not in body
        finally:
            proceed.set()
        assert _wait_for_job(client)["state"] == "done"
        assert gate.snapshot().runs == 0  # begin() was never called


def test_pull_pipeline_seeds_discovery_memos_after_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route runs the post-pipeline discovery warm ONCE after the pipeline
    (DEC-DB-DISCOVERY-ADAPTIVE-FLOOR-001 item 1b): the app-module binding is
    spied — a clean offline run must invoke it exactly once with the app's
    current write generation + all four memo caches wired (the seeding
    mechanics themselves are unit-pinned in ``test_discovery_warm.py``)."""
    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    warm_calls: list[dict[str, Any]] = []

    def _spy_warm(**kwargs: Any) -> int:
        warm_calls.append(dict(kwargs))
        return 5

    monkeypatch.setattr(app_module, "warm_discovery_memos", _spy_warm)
    assert client.post("/api/v1/universe/pull-pipeline").status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    assert len(warm_calls) == 1, "the discovery warm must run once per pipeline"
    kwargs = warm_calls[0]
    assert isinstance(kwargs["generation"], int)
    for cache_kw in (
        "db_datasets_cache",
        "spot_cascade_cache",
        "fno_cascade_cache",
        "latest_date_cache",
    ):
        assert kwargs[cache_kw] is not None
    # The symbol_for_id provider is lazy (called inside the warm's guard).
    # Typed local binding (the production `warm_discovery_memos` signature):
    # `callable()` on the raw `Any` narrows to `(...) -> object` under pyright,
    # which would reject the `dict(...)` call below.
    symbol_for_id_fn: Callable[[], Mapping[str, str]] = kwargs["symbol_for_id_fn"]
    assert callable(symbol_for_id_fn)
    assert dict(symbol_for_id_fn()) == {"13": "NIFTY"}


def test_pull_pipeline_prebuild_global_stop_fails_job_alerts_and_releases_gate(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """END-TO-END F1 wiring pin (DEC-PIPELINE-AUTOPREBUILD-001 hostile-review
    fold): a disk-full ``LakeWriteHalt`` inside the REAL guarded prebuild
    RE-RAISES through ``run_full_pipeline`` (no movers warm ever fires), the
    job settles ``error`` naming the halt, the CRITICAL pull-halt alert fires
    exactly once, and the movers gate is STILL released (``last_ok=False``) —
    the halt is loud, and movers are never wedged shut behind it."""
    from brutex.data.errors import LakeWriteHalt

    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)

    def _halting_materialize(*args: Any, **kwargs: Any) -> Any:
        raise LakeWriteHalt("disk_full")

    monkeypatch.setattr(app_module, "materialize_lake_timeframes", _halting_materialize)
    halts: list[BaseException] = []
    monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", halts.append)
    gate = PrebuildGate()
    with _gated_client(store, monkeypatch, tmp_path, gate) as client:
        assert client.post("/api/v1/universe/pull-pipeline").status_code == 200
        snap = _wait_for_job(client)
        assert snap["state"] == "error"  # the halt FAILS the job — never a fake "done"
        assert "LakeWriteHalt" in snap["detail"]
        assert len(halts) == 1  # the CRITICAL Telegram pull-halt alert fired once
        assert isinstance(halts[0], LakeWriteHalt)
        gate_snap = gate.snapshot()
        assert gate_snap.blocking is False  # the _work finally still released the gate
        assert gate_snap.last_ok is False  # honest provenance: the prebuild never succeeded
        # /movers serves the normal (non-gated) payload again — never stuck.
        body = client.get("/api/v1/movers", params={"date": "2024-06-13"}).json()
        assert "gated" not in body


# ── quarantined symbols surface in the finish detail (chunk 3 headline honesty) ──


def test_pull_pipeline_quarantined_symbol_shows_in_finish_detail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-symbol quarantine is NAMED in the job's completion headline.

    End-to-end through the real pipeline composition + BackgroundPullJob: a spot
    fake whose NIFTY carries a terminally-failed pair passes every phase gate
    (no pull-wide failure), the pipeline completes, and the /pull-progress
    ``detail`` reads the honest "pipeline complete — 1 symbol(s) quarantined:
    NIFTY — …" note instead of the bare "done".
    """
    _pin_creds_present(monkeypatch)

    def _spot_with_quarantine(*args: Any, **kwargs: Any) -> BulkSpotReport:
        return BulkSpotReport(
            dry_run=kwargs["dry_run"],
            start_date="2020-01-01",
            end_date="2026-06-20",
            symbols=[SymbolPullResult(symbol="NIFTY", completed=1, failed=1, no_data=0)],
        )

    monkeypatch.setattr(app_module, "pull_all_spot", _spot_with_quarantine)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)
    resp = client.post("/api/v1/universe/pull-pipeline")
    assert resp.status_code == 200
    snap = _wait_for_job(client)
    assert snap["state"] == "done"
    assert "1 symbol(s) quarantined: NIFTY — " in snap["detail"]
    assert snap["detail"].startswith("pipeline complete — ")


def test_pull_pipeline_clean_run_finish_detail_is_the_complete_note(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean pipeline's headline is the honest complete note (no quarantine)."""
    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    resp = client.post("/api/v1/universe/pull-pipeline")
    assert resp.status_code == 200
    snap = _wait_for_job(client)
    assert snap["state"] == "done"
    assert snap["detail"].startswith("pipeline complete — spot ")
    assert "quarantined" not in snap["detail"]
    assert "Greeks enrichment FAILED" not in snap["detail"]


# ── Greeks-enrichment failure surfaces in the finish detail ───────────────────
# (DEC-GREEKS-ENRICH-OBSERVE-001)


def test_pull_pipeline_greeks_failure_shows_in_finish_detail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A WHOLE-pass Greeks-enrichment failure is NAMED in the job's headline.

    End-to-end through the real pipeline composition + BackgroundPullJob: the
    historical fake's candle sub-report carries ``greeks_enrich_ok=False``
    (the fully-guarded pass failed whole — the pull itself completed), the
    pipeline settles ``done``, and the /pull-progress ``detail`` carries the
    ", Greeks enrichment FAILED (RuntimeError) — overlay re-attempted next
    pull" clause so
    the operator sees the failed pass instead of one swallowed log line.
    """
    _pin_creds_present(monkeypatch)

    def _historical_with_greeks_failure(*args: Any, **kwargs: Any) -> CompositePullReport:
        base = _fake_historical(*args, **kwargs)
        inner = base.historical
        assert inner is not None
        failing = replace(
            inner,
            greeks_enrich_ok=False,
            greeks_enrich_error="RuntimeError: misconfigured risk-free source",
        )
        return replace(base, historical=failing)

    monkeypatch.setattr(app_module, "pull_all_spot", _fake_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(
        app_module, "pull_historical_for_underlyings", _historical_with_greeks_failure
    )
    resp = client.post("/api/v1/universe/pull-pipeline")
    assert resp.status_code == 200
    snap = _wait_for_job(client)
    assert snap["state"] == "done"
    assert snap["detail"].startswith("pipeline complete — ")
    assert (
        ", Greeks enrichment FAILED (RuntimeError) — overlay re-attempted next pull"
        in snap["detail"]
    )


def test_pull_pipeline_work_feeds_greeks_health_counter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The /pull-pipeline ``_work`` closure REALLY feeds ``GreeksEnrichHealth``.

    Hostile-review MAJOR fold (the shipped-DORMANT failure mode of #935/#1014):
    the counter primitive is 100%-tested, but nothing pinned that a real pull
    job feeds it — deleting the ``_greeks_health.observe`` call site in
    ``app.py`` left every test green. This drives the REAL route + background
    job ``threshold`` times with a failing Greeks sub-report and asserts
    exactly ONE ``pull.greeks_enrich_failing`` WARNING fires through the
    production binding; a recovery pull then RESETS + RE-ARMS the latch, so a
    relapse warns again (streak + latch proven end-to-end, not on the unit).
    """
    _pin_creds_present(monkeypatch)
    monkeypatch.setattr(app_module, "pull_all_spot", _fake_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)

    def _historical_with_greeks(ok: bool) -> Any:
        def _factory(*args: Any, **kwargs: Any) -> CompositePullReport:
            base = _fake_historical(*args, **kwargs)
            inner = base.historical
            assert inner is not None
            patched = replace(
                inner,
                greeks_enrich_ok=ok,
                greeks_enrich_error=None if ok else "RuntimeError: broken risk-free source",
            )
            return replace(base, historical=patched)

        return _factory

    def _run_pipeline(*, greeks_ok: bool) -> None:
        monkeypatch.setattr(
            app_module, "pull_historical_for_underlyings", _historical_with_greeks(greeks_ok)
        )
        resp = client.post("/api/v1/universe/pull-pipeline")
        assert resp.status_code == 200
        assert _wait_for_job(client)["state"] == "done"

    with capture_logs() as logs:
        for _ in range(DEFAULT_GREEKS_FAILURE_THRESHOLD):
            _run_pipeline(greeks_ok=False)
    breaches = [r for r in logs if r["event"] == "pull.greeks_enrich_failing"]
    assert len(breaches) == 1  # ONE warning at the OK→BREACH crossing, not per pull
    assert breaches[0]["consecutive"] == DEFAULT_GREEKS_FAILURE_THRESHOLD
    assert breaches[0]["last_error"] == "RuntimeError: broken risk-free source"

    with capture_logs() as relapse_logs:
        _run_pipeline(greeks_ok=True)  # recovery resets the streak + re-arms the latch
        for _ in range(DEFAULT_GREEKS_FAILURE_THRESHOLD):
            _run_pipeline(greeks_ok=False)
    relapses = [r for r in relapse_logs if r["event"] == "pull.greeks_enrich_failing"]
    assert len(relapses) == 1  # the relapse warns AGAIN through the same binding


# ── cross-list selection (DEC-UNIVERSE-UI-POLISH-002) ─────────────────────────


def _adanient() -> Instrument:
    """A STOCK universe row — the cross-list partner of the ``_nifty`` index."""
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=25,
        symbol_name="ADANIENT",
        display_name="ADANI ENTERPRISES",
        instrument=InstrumentType.EQ,
        segment=Segment.CASH,
        series="EQ",
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


def test_pull_pipeline_cross_list_selection_scopes_both_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick on the STOCKS list + a tick on the INDEX list pull TOGETHER.

    DEC-UNIVERSE-UI-POLISH-002 (route half of the cross-list selection fix):
    the operator ticked ADANIENT (stocks) and NIFTY (index) yet the pull
    scoped only one — the frontend's tab-switch ``selMulti.clear()`` wiped the
    hidden list's ticks (fixed + pinned in
    ``test_dashboard_universe_crosslist_select.py``). This pins the BACKEND
    contract the fix relies on: ONE ``?symbols=`` comma list spanning BOTH
    instrument kinds resolves to BOTH targets (``resolve_pull_targets`` is
    kind-agnostic) and the spot phase receives BOTH instruments, in tick order.

    EXPLICIT full arm (DEC-UNIVERSE-INDEX-ONLY-001 — the §6 migration Law 2):
    the cross-list contract is index+STOCK by construction; under the shipped
    index4 default the P7 selection screen now refuses the stock tick (the
    400 arm is pinned in ``test_dashboard_pull_scope_routes.py``), so this
    mechanics pin runs under ``full`` (merge-base parity — the §5.6 widen
    path).
    """
    monkeypatch.setenv(_FEED_SCOPE_ENV_1934, "full")
    with InstrumentStore.open(tmp_path / "instruments.duckdb") as store:
        store.upsert_many(
            [_nifty(), _adanient()],
            snapshot_date=date(2024, 1, 2),
            run_id="r1",
            csv_source_sha256="0" * 64,
        )
        monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
        _pin_creds_present(monkeypatch)
        _patch_spines(monkeypatch)
        spot_target_sets: list[list[str]] = []

        def _capture_spot(targets: Any, *args: Any, **kwargs: Any) -> BulkSpotReport:
            spot_target_sets.append([inst.symbol_name for inst in targets])
            return _fake_spot(**kwargs)

        monkeypatch.setattr(app_module, "pull_all_spot", _capture_spot)
        with TestClient(build_app(DashboardState(), instrument_store=store)) as client:
            body = client.post(
                "/api/v1/universe/pull-pipeline", params={"symbols": "ADANIENT,NIFTY"}
            ).json()
            assert body["started"] is True
            assert body["targets"] == 2, "BOTH the stock and the index must be targeted"
            assert body["target_source"] == "selection"
            assert _wait_for_job(client)["state"] == "done"
        # The spot phase received the COMBINED cross-list selection, tick-ordered.
        assert spot_target_sets == [["ADANIENT", "NIFTY"]]


# ── the movers-warm UNBLOCK pack (DEC-MOVERS-WARM-UNBLOCK-001) ──────────────


def _patch_warm_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the pipeline's movers-warm phase actually RUN against fakes.

    Prebuild ok=True (the warm gate), a resolvable latest movers date, and
    graceful no-registry fakes for the options warm's calendar/chain loaders.
    """
    from brutex.dashboard.pipeline_prebuild import PrebuildOutcome

    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    monkeypatch.setattr(
        app_module,
        "run_pipeline_prebuild_guarded",
        lambda **kwargs: PrebuildOutcome(
            symbols_built=1, total_bars=9, failed=(), failed_count=0, ok=True
        ),
    )
    monkeypatch.setattr(app_module, "latest_movers_date", lambda lake_root: "2024-06-13")
    monkeypatch.setattr(app_module, "load_registry_expiries", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "make_registry_chain_loader", lambda *a, **k: None)


def test_pull_pipeline_blocked_warm_never_holds_the_movers_gate(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FIX-3 REGRESSION (DEC-MOVERS-WARM-UNBLOCK-001): a movers-warm job that
    BLOCKS (threading.Event never set while we probe) must NOT keep the
    PrebuildGate held — the gate releases the moment the prebuild outcome is
    known, so /api/v1/movers serves the normal (non-gated) payload WHILE the
    warm is still running. The 2026-07-04/05 incident (pull_id 22fc7739f719):
    the warm's 39 full-lake-walk keys (170-256 s each) held /movers hostage
    for hours behind the finally-only release."""
    from brutex.dashboard.movers_snapshot import SpotCandidates

    _patch_warm_runs(monkeypatch)
    warm_entered = threading.Event()
    warm_proceed = threading.Event()

    def _blocking_spot_compute(**kwargs: Any) -> SpotCandidates:
        if kwargs["date"] == "2024-06-13":  # a WARM job (the warm's target day)
            warm_entered.set()
            assert warm_proceed.wait(timeout=10.0), "test never released the warm"
        return SpotCandidates(snapshots=(), skipped=0, date=str(kwargs["date"]))

    monkeypatch.setattr(app_module, "compute_spot_candidates", _blocking_spot_compute)
    gate = PrebuildGate()
    try:
        with _gated_client(store, monkeypatch, tmp_path, gate) as client:
            assert client.post("/api/v1/universe/pull-pipeline").status_code == 200
            assert warm_entered.wait(timeout=10.0), "the movers warm never started"
            # THE PIN: the warm is BLOCKED right now, yet the gate is already
            # released (prebuild done) and /movers serves NON-gated.
            snap = gate.snapshot()
            assert snap.blocking is False
            assert snap.last_ok is True
            body = client.get("/api/v1/movers", params={"date": "2024-06-14"}).json()
            assert "gated" not in body
            warm_proceed.set()
            assert _wait_for_job(client)["state"] == "done"
    finally:
        warm_proceed.set()  # never leave the warm job thread wedged


def test_pull_pipeline_warm_shares_one_dataset_walk(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FIX-1 REGRESSION (DEC-MOVERS-WARM-UNBLOCK-001): the WHOLE 39-key warm
    run performs exactly ONE ``list_candle_datasets`` lake walk — the memoised
    ``datasets=`` seam (DEC-MOVERS-DISCOVERY-MEMO-001) is threaded into every
    warm compute. Before the fix each of the 39 jobs re-walked the whole lake
    (~3 min each on the options-exploded tree — the incident's dominant cost)."""
    _patch_warm_runs(monkeypatch)
    walks: list[int] = []

    def _counting_walk(lake_root: Path) -> list[dict[str, str]]:
        walks.append(1)
        return []

    monkeypatch.setattr(app_module, "list_candle_datasets", _counting_walk)
    gate = PrebuildGate()
    with _gated_client(store, monkeypatch, tmp_path, gate) as client:
        assert client.post("/api/v1/universe/pull-pipeline").status_code == 200
        snap = _wait_for_job(client)
        assert snap["state"] == "done"
    # ONE walk for the whole spot(3) + futures(9) + options(27) warm grid —
    # every compute received the memoised list via `datasets=` (an injected
    # empty list yields empty candidates WITHOUT scanning, pinned by the
    # discovery-memo suite), so a re-walk per job would be visible here.
    assert len(walks) == 1


def test_pull_pipeline_wedged_warm_times_out_and_job_completes(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FIX-2 REGRESSION (DEC-MOVERS-WARM-UNBLOCK-001): a warm job wedged past
    the per-job deadline (env ``BRUTEX_MOVERS_WARM_JOB_DEADLINE_S``) is cut,
    counted FAILED, and the pipeline job still settles ``done`` with the gate
    released — a wedged warm can never hold the pull open indefinitely."""
    from brutex.dashboard.movers_snapshot import SpotCandidates

    _patch_warm_runs(monkeypatch)
    monkeypatch.setenv("BRUTEX_MOVERS_WARM_JOB_DEADLINE_S", "0.05")
    release = threading.Event()

    def _wedged_spot_compute(**kwargs: Any) -> SpotCandidates:
        if kwargs["date"] == "2024-06-13":  # only the WARM jobs wedge
            release.wait(30.0)
        return SpotCandidates(snapshots=(), skipped=0, date=str(kwargs["date"]))

    monkeypatch.setattr(app_module, "compute_spot_candidates", _wedged_spot_compute)
    gate = PrebuildGate()
    try:
        with _gated_client(store, monkeypatch, tmp_path, gate) as client, capture_logs() as logs:
            assert client.post("/api/v1/universe/pull-pipeline").status_code == 200
            snap = _wait_for_job(client)
        assert snap["state"] == "done"  # the wedged warm NEVER fails the pull
        timeouts = [e for e in logs if e["event"] == "movers.warm.job_timeout"]
        assert len(timeouts) == 3  # all 3 spot-grid coordinates timed out
        assert all(e["segment"] == "spot" for e in timeouts)
        gate_snap = gate.snapshot()
        assert gate_snap.blocking is False
        assert gate_snap.last_ok is True
    finally:
        release.set()  # let the abandoned daemon workers exit promptly


# ── the strict post-step gate env knob (DEC-PULL-SIX-STEP-ORDER-001) ──────────


def test_pull_pipeline_garbage_strict_gate_env_is_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A garbage BRUTEX_PIPELINE_STRICT_GATE is a clean 400 AT LAUNCH.

    The knob is resolved ONCE, eagerly, inside ``_launch_pipeline``'s
    validation try — never a mid-pull 500 on the job thread; the detail
    names the variable (fail-loud, Golden Rule #1).
    """
    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    monkeypatch.setenv("BRUTEX_PIPELINE_STRICT_GATE", "banana")
    resp = client.post("/api/v1/universe/pull-pipeline")
    assert resp.status_code == 400
    assert "BRUTEX_PIPELINE_STRICT_GATE" in resp.json()["detail"]


def test_pull_pipeline_strict_gate_enabled_launches_and_logs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``BRUTEX_PIPELINE_STRICT_GATE=1`` launches normally + is logged on the request."""
    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    monkeypatch.setenv("BRUTEX_PIPELINE_STRICT_GATE", "1")
    with capture_logs() as logs:
        resp = client.post("/api/v1/universe/pull-pipeline")
        assert resp.status_code == 200
        assert resp.json()["started"] is True
        assert _wait_for_job(client)["state"] == "done"
    requested = [e for e in logs if e["event"] == "universe.pull_pipeline.requested"]
    assert len(requested) == 1
    assert requested[0]["strict_gate"] is True


def test_pull_pipeline_default_strict_gate_is_false(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset knob ⇒ strict_gate=False on the request log (the cleanset default)."""
    _pin_creds_present(monkeypatch)
    _patch_spines(monkeypatch)
    monkeypatch.delenv("BRUTEX_PIPELINE_STRICT_GATE", raising=False)
    with capture_logs() as logs:
        assert client.post("/api/v1/universe/pull-pipeline").status_code == 200
        assert _wait_for_job(client)["state"] == "done"
    requested = [e for e in logs if e["event"] == "universe.pull_pipeline.requested"]
    assert len(requested) == 1
    assert requested[0]["strict_gate"] is False


# ── durable &-form winner wiring (DEC-FORM-WINNER-PERSIST-001, round-5 pin) ──


def _patch_spines_capturing_form_preference(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]
) -> None:
    """Wire spine fakes that record each phase's ``form_preference`` kwarg.

    Fail-loud key access (the round-2 direct-selection discipline): deleting
    the ``form_preference=...`` kwarg at either phase call site in the
    /pull-pipeline ``_work`` closure KeyErrors these fakes, failing the tests
    below instead of passing silently.
    """

    def _metadata(*args: Any, **kwargs: Any) -> MetadataPullReport:
        captured["metadata_form_preference"] = kwargs["form_preference"]
        return _fake_metadata(*args, **kwargs)

    def _historical(*args: Any, **kwargs: Any) -> CompositePullReport:
        captured["historical_form_preference"] = kwargs["form_preference"]
        return _fake_historical(*args, **kwargs)

    monkeypatch.setattr(app_module, "pull_all_spot", _fake_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _historical)


def test_pull_pipeline_threads_one_durable_form_preference_to_both_phases(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ONE durable form-winner map per pipeline run, shared by BOTH phases.

    The round-5 regression pin for the headline DEC-FORM-WINNER-PERSIST-001
    claim "ONE instance per /pull-pipeline run shared by the metadata +
    historical phases" — previously asserted by ZERO tests, so reverting the
    pipeline to per-phase fresh instances (silently re-breaking the
    cross-phase amnesia this decision fixes) passed every suite. Drives the
    REAL route + ``run_full_pipeline`` with fakes one level down and pins:
    (a) the wired preference is a ``PersistentSymbolFormPreference`` whose
    winners persist into the REAL wired ``InstrumentStore`` on a non-dry-run,
    and (b) the metadata and historical phases receive the SAME instance (a
    metadata-phase %26 win must reorder the candle phase's first fetch).
    """
    _pin_creds_present(monkeypatch)
    captured: dict[str, Any] = {}
    _patch_spines_capturing_form_preference(monkeypatch, captured)
    resp = client.post("/api/v1/universe/pull-pipeline")
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    pref = captured["metadata_form_preference"]
    assert isinstance(pref, PersistentSymbolFormPreference)
    # (b) IDENTITY sharing — the historical phase got the SAME object, so a
    # metadata-phase win is visible to the candle phase within one run.
    assert captured["historical_form_preference"] is pref
    # (a) the preference is wired over the REAL InstrumentStore: a winner
    # recorded through the routed instance lands in its schema_meta.
    forms = ("M&M", "M%26M")
    assert pref.record_winner("M&M", forms, "M%26M") is True
    assert store.get_meta(form_winner_key("M&M")) == "M%26M"  # the REAL wired store


def test_pull_pipeline_dry_run_form_preference_is_store_less(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) a dry-run pipeline threads ``store=None`` — synthetic runs never persist."""
    _pin_creds_present(monkeypatch)
    captured: dict[str, Any] = {}
    _patch_spines_capturing_form_preference(monkeypatch, captured)
    resp = client.post("/api/v1/universe/pull-pipeline", params={"dry_run": "true"})
    assert resp.status_code == 200
    assert _wait_for_job(client)["state"] == "done"
    pref = captured["metadata_form_preference"]
    assert isinstance(pref, PersistentSymbolFormPreference)
    assert captured["historical_form_preference"] is pref  # still ONE shared instance
    forms = ("M&M", "M%26M")
    assert pref.record_winner("M&M", forms, "M%26M") is True  # in-memory learning only
    assert store.get_meta(form_winner_key("M&M")) is None  # store=None: no durable row
