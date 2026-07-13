# pyright: reportPrivateUsage=none
"""The ``scope=spot-only`` arm of ``/api/v1/universe/pull-pipeline``.

DEC-PULL-SCOPED-ALL-ONECLICK-001 (MIGRATION.md #1779): the additive ``scope``
token on the ONE launcher — default requests byte-identical (the
existing-button protection pin), the spot-only arm swapping EXACTLY the two
F&O phase closures + the two F&O movers warmers + the prebuild cascade, every
launcher rail (single-flight, market-hours start gate, dry-run inertness,
finish-detail note) inherited, and the crossverify TAIL riding the landed
``_crossverify_job`` producer trigger as the launcher's LAST step. Offline
fakes over the REAL report dataclasses (the pull-pipeline-route template);
never a network call.
"""

from __future__ import annotations

import importlib
import threading
import time
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.config.credentials import ENV_CREDENTIALS_FILE
from brutex.crossverify.run import CrossverifyReport
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.composite_pull import CompositePullReport
from brutex.dashboard.metadata_pull import MetadataPullReport
from brutex.dashboard.pipeline_prebuild import PrebuildOutcome
from brutex.dashboard.pull_controller import BulkSpotReport, SymbolPullResult
from brutex.dashboard.pull_jobs import BackgroundPullJob
from brutex.dashboard.pull_scope import CrossverifyTailOutcome
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

app_module = importlib.import_module("brutex.dashboard.app")

_ROUTE = "/api/v1/universe/pull-pipeline"


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


def _tail_spy() -> tuple[list[str], Any]:
    calls: list[str] = []

    def _tail() -> CrossverifyTailOutcome:
        calls.append("tail")
        return CrossverifyTailOutcome(
            status="started", human="producer started (poll /api/v1/crossverify/progress)"
        )

    return calls, _tail


def _make_client(
    store: InstrumentStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **build_kwargs: Any,
) -> TestClient:
    monkeypatch.setenv(ENV_CREDENTIALS_FILE, str(tmp_path / "credentials.json"))
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(tmp_path / "lake"))
    (tmp_path / "lake").mkdir(exist_ok=True)
    return TestClient(build_app(DashboardState(), instrument_store=store, **build_kwargs))


def _pin_creds_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "groww_credentials_error", lambda: None)


def _fake_spot(*args: Any, **kwargs: Any) -> BulkSpotReport:
    return BulkSpotReport(
        dry_run=kwargs["dry_run"],
        start_date="2020-01-01",
        end_date="2026-07-12",
        symbols=[SymbolPullResult(symbol="NIFTY", completed=1, failed=0, no_data=0)],
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
    return CompositePullReport(
        dry_run=kwargs["dry_run"],
        requested=("NIFTY",),
        tradable=("NIFTY",),
        skipped_reference_only=(),
        metadata=None,
        historical=None,
        note="offline fake",
    )


class _Spines:
    """Recording offline fakes for all three pull spines."""

    def __init__(self) -> None:
        self.spot_kwargs: list[dict[str, Any]] = []
        self.metadata_calls = 0
        self.historical_calls = 0

    def wire(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def spot(*args: Any, **kwargs: Any) -> BulkSpotReport:
            self.spot_kwargs.append(dict(kwargs))
            return _fake_spot(*args, **kwargs)

        def metadata(*args: Any, **kwargs: Any) -> MetadataPullReport:
            self.metadata_calls += 1
            return _fake_metadata(*args, **kwargs)

        def historical(*args: Any, **kwargs: Any) -> CompositePullReport:
            self.historical_calls += 1
            return _fake_historical(*args, **kwargs)

        monkeypatch.setattr(app_module, "pull_all_spot", spot)
        monkeypatch.setattr(app_module, "pull_expiries_contracts", metadata)
        monkeypatch.setattr(app_module, "pull_historical_for_underlyings", historical)


def _wait_for_job(client: TestClient, *, timeout: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert isinstance(snap, dict)
        if snap["state"] in {"done", "error"}:
            return snap
        time.sleep(0.01)
    raise AssertionError("background pipeline job did not settle within the timeout")


# ── the scope token contract ─────────────────────────────────────────────


def test_unknown_scope_400(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unknown scope is a clean 400 — never a widened/narrowed vendor pull."""
    _pin_creds_present(monkeypatch)
    with _make_client(store, monkeypatch, tmp_path) as client:
        resp = client.post(_ROUTE, params={"scope": "spotonly", "dry_run": "true"})
    assert resp.status_code == 400
    assert "unknown pull scope" in resp.json()["detail"]


def test_default_scope_payload_byte_identical(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[F/B] The existing-button protection pin: a scope-less request's payload
    carries every pre-existing key with its exact pre-change value; ``scope``
    is the ONE additive key (== "full")."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    with _make_client(store, monkeypatch, tmp_path) as client:
        resp = client.post(_ROUTE, params={"dry_run": "true"})
        assert resp.status_code == 200
        body = resp.json()
        years = app_module._resolve_metadata_years(None, None)
        assert body == {
            "started": True,
            "targets": 1,
            "total": 1,
            "tradable": 1,
            "target_source": "universe",
            "years": years,
            "dry_run": True,
            "scope": "full",
        }
        _wait_for_job(client)


def test_scope_full_explicit_equals_default(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[spy] ``?scope=full`` runs the SAME three-phase chain as the default."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    with _make_client(store, monkeypatch, tmp_path) as client:
        resp = client.post(_ROUTE, params={"scope": "full", "dry_run": "true"})
        assert resp.status_code == 200
        assert resp.json()["scope"] == "full"
        _wait_for_job(client)
    assert len(spines.spot_kwargs) == 1
    assert spines.metadata_calls == 1
    assert spines.historical_calls == 1


def test_scope_spot_only_swaps_exactly_two_fno_bindings(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[spy] spot-only: ``pull_all_spot`` bound kwarg-for-kwarg IDENTICALLY to
    the full arm; the two F&O spines are NEVER called."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    _calls, tail = _tail_spy()
    with _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client:
        # Full-scope reference run first (dry-run — same kwarg shape law).
        assert client.post(_ROUTE, params={"dry_run": "true"}).status_code == 200
        _wait_for_job(client)
        assert (
            client.post(_ROUTE, params={"scope": "spot-only", "dry_run": "true"}).status_code == 200
        )
        _wait_for_job(client)
    assert len(spines.spot_kwargs) == 2
    full_kwargs, spot_only_kwargs = spines.spot_kwargs
    # The SAME kwarg NAME set on both arms (until_clean / floor_store /
    # market_closed / session_rate / today-gate all inherited).
    assert set(full_kwargs) == set(spot_only_kwargs)
    assert spot_only_kwargs["until_clean"] is True
    assert spot_only_kwargs["auto_retry"] is True
    assert spot_only_kwargs["intervals"] == ("1minute",)
    assert callable(spot_only_kwargs["market_closed"])
    # The two F&O spines ran ONCE (the full reference run) — never again.
    assert spines.metadata_calls == 1
    assert spines.historical_calls == 1


def test_scope_spot_only_prebuild_empty_cascade(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[spy] The spot-only prebuild rides an EMPTY F&O cascade — the lake's
    F&O datasets are never even enumerated; the full arm still enumerates."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    cascade_args: list[Any] = []
    fno_walks: list[str] = []
    monkeypatch.setattr(
        app_module,
        "list_fno_candle_datasets",
        lambda _lake: fno_walks.append("walk") or [],
    )

    real_build = app_module.build_fno_cascade

    def spy_cascade(datasets: Any) -> Any:
        materialized = tuple(datasets)
        cascade_args.append(materialized)
        return real_build(materialized)

    monkeypatch.setattr(app_module, "build_fno_cascade", spy_cascade)

    def fake_prebuild(**kwargs: Any) -> PrebuildOutcome:
        kwargs["cascade_or_error"]()  # force the cascade factory
        return PrebuildOutcome(symbols_built=1, total_bars=5, failed=(), failed_count=0, ok=True)

    monkeypatch.setattr(app_module, "run_pipeline_prebuild_guarded", fake_prebuild)
    _calls, tail = _tail_spy()
    with _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client:
        assert (
            client.post(_ROUTE, params={"scope": "spot-only", "dry_run": "true"}).status_code == 200
        )
        _wait_for_job(client)
        assert cascade_args == [()]
        assert fno_walks == []  # the F&O dataset walk never ran on spot-only
        assert client.post(_ROUTE, params={"dry_run": "true"}).status_code == 200
        _wait_for_job(client)
    assert fno_walks == ["walk"]  # the full arm still enumerates


def test_scope_spot_only_segment_movers_spot_only(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the SPOT movers warmer is injected — futures/options segments are
    honestly ABSENT from the warm (never a fabricated 0-count segment)."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    monkeypatch.setattr(
        app_module,
        "run_pipeline_prebuild_guarded",
        lambda **kwargs: PrebuildOutcome(
            symbols_built=1, total_bars=5, failed=(), failed_count=0, ok=True
        ),
    )
    _calls, tail = _tail_spy()
    with _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client:
        with capture_logs() as logs:
            assert (
                client.post(_ROUTE, params={"scope": "spot-only", "dry_run": "true"}).status_code
                == 200
            )
            _wait_for_job(client)
        started = [e for e in logs if e["event"] == "pipeline.movers_warm.started"]
        assert len(started) == 1
        assert started[0]["segments"] == ["spot"]
        done = [e for e in logs if e["event"] == "pipeline.movers_warm.done"]
        assert len(done) == 1
        assert set(done[0]["warmed"]) == {"spot"}


# ── inherited launcher rails ─────────────────────────────────────────────


def test_route_spot_only_market_hours_start_gate_defers(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The market-hours START gate is inherited: a REAL spot-only launch at a
    frozen in-window instant (10:00 IST on a trading day) is deferred."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    monkeypatch.setenv("BRUTEX_MARKET_HOURS_GUARD", "1")
    monkeypatch.setattr(
        "brutex.calendars.market_hours.now_ist",
        lambda: datetime(2024, 1, 2, 10, 0),  # Tue, trading day, in-window
    )
    with _make_client(store, monkeypatch, tmp_path) as client:
        resp = client.post(_ROUTE, params={"scope": "spot-only"})
    assert resp.status_code == 200
    assert resp.json()["started"] is False
    assert spines.spot_kwargs == []  # no vendor phase ever ran


def test_route_second_start_refused_single_flight(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B A1: the shared single-flight job refuses a second concurrent start."""
    _pin_creds_present(monkeypatch)
    release = threading.Event()
    entered = threading.Event()

    def blocking_spot(*args: Any, **kwargs: Any) -> BulkSpotReport:
        entered.set()
        assert release.wait(timeout=15.0)
        return _fake_spot(*args, **kwargs)

    monkeypatch.setattr(app_module, "pull_all_spot", blocking_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)
    _calls, tail = _tail_spy()
    with _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client:
        try:
            first = client.post(_ROUTE, params={"scope": "spot-only", "dry_run": "true"})
            assert first.json()["started"] is True
            assert entered.wait(timeout=15.0)
            second = client.post(_ROUTE, params={"scope": "spot-only", "dry_run": "true"})
            assert second.json()["started"] is False
        finally:
            release.set()
        _wait_for_job(client)


def test_route_dry_run_inert(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """B H6: a dry-run spot-only pipeline never invokes the crossverify tail —
    the note reads the honest skipped (dry-run) clause."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    calls, tail = _tail_spy()
    with _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client:
        resp = client.post(_ROUTE, params={"scope": "spot-only", "dry_run": "true"})
        assert resp.status_code == 200
        assert resp.json()["dry_run"] is True
        snap = _wait_for_job(client)
    assert calls == []  # [spy] the tail was NEVER invoked
    assert "cross-verify: skipped (dry-run)" in snap["detail"]
    assert "Greeks: not run — no options in this pull" in snap["detail"]


# ── the crossverify tail (production binding + degrades) ─────────────────


def _cv_report(**overrides: object) -> CrossverifyReport:
    import dataclasses

    base = CrossverifyReport(
        day=date(2026, 7, 10),
        run_id="cv-20260710-161501-1",
        verdict="published",
        published=3,
        skipped_already=1,
        withheld=0,
        blocked=0,
        nothing=2,
        excluded=0,
        publish_failed=0,
        recovered_prior=0,
        derived=4,
        derive_failed=0,
        movers_warm="revision-propagated (headless)",
        detail="",
        exit_code=0,
    )
    return dataclasses.replace(base, **overrides) if overrides else base  # type: ignore[arg-type]


def _wait_cv_terminal(client: TestClient, *, timeout: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/v1/crossverify/progress").json()
        if snap["state"] in {"done", "error"}:
            return snap
        time.sleep(0.01)
    raise AssertionError("crossverify job did not settle within the timeout")


def test_route_tail_reuses_crossverify_job(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[spy] The PRODUCTION tail fires the landed ``_crossverify_job`` producer
    as the launcher's LAST step: the injected ``crossverify_runner`` seam is
    observed, the job start kwargs stay ``cross_process_lock=False`` +
    ``market_hours_exempt=True`` (the lock-safety inheritance pin — the
    runner's own bounded-wait trio is 100%-tested in the crossverify suites),
    and the pipeline job itself rides the default PULL job kind (B A5)."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    runner_calls: list[Any] = []

    def runner(config: Any) -> CrossverifyReport:
        runner_calls.append(config)
        return _cv_report()

    start_calls: list[dict[str, Any]] = []
    orig_start = BackgroundPullJob.start

    def spy_start(self: BackgroundPullJob, work: Any, **kwargs: Any) -> bool:
        start_calls.append(dict(kwargs))
        return orig_start(self, work, **kwargs)

    monkeypatch.setattr(BackgroundPullJob, "start", spy_start)
    with _make_client(store, monkeypatch, tmp_path, crossverify_runner=runner) as client:
        resp = client.post(_ROUTE, params={"scope": "spot-only"})
        assert resp.status_code == 200
        snap = _wait_for_job(client)
        cv_snap = _wait_cv_terminal(client)
    assert len(runner_calls) == 1  # the SAME runner seam the /crossverify route uses
    # The tail resolves the SAME store-derived lake root the pipeline pulls into
    # (_resolve_pull_lake_root — the instruments.duckdb parent in this fixture).
    assert runner_calls[0].lake_root == tmp_path
    assert cv_snap["state"] == "done"
    assert "cross-verify: producer started (poll /api/v1/crossverify/progress)" in snap["detail"]
    cv_starts = [c for c in start_calls if c.get("detail") == "cross-verify → TickVault"]
    assert len(cv_starts) == 1
    assert cv_starts[0]["cross_process_lock"] is False
    assert cv_starts[0]["market_hours_exempt"] is True
    # B A5: the pipeline start call keeps the DEFAULT PULL job kind (no
    # job_kind override), so the rate probe stays honest.
    pipeline_starts = [c for c in start_calls if "pipeline" in str(c.get("detail", ""))]
    assert len(pipeline_starts) == 1
    assert "job_kind" not in pipeline_starts[0]


def test_route_tail_already_running_noted(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A busy producer job yields the honest 'already running' clause."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    release = threading.Event()
    entered = threading.Event()

    def blocking_runner(config: Any) -> CrossverifyReport:
        entered.set()
        assert release.wait(timeout=20.0)
        return _cv_report()

    with _make_client(store, monkeypatch, tmp_path, crossverify_runner=blocking_runner) as client:
        try:
            # Occupy the producer job via its own route first.
            busy = client.post("/api/v1/crossverify/run")
            assert busy.json()["started"] is True
            assert entered.wait(timeout=15.0)
            resp = client.post(_ROUTE, params={"scope": "spot-only"})
            assert resp.status_code == 200
            snap = _wait_for_job(client)
        finally:
            release.set()
        _wait_cv_terminal(client)
    assert "cross-verify: already running" in snap["detail"]


def test_route_tail_env_junk_not_configured_fail_soft(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ruling 8: junk BRUTEX_CROSSVERIFY_* env → 'not configured (ValueError…)'
    — fail-SOFT, the spot pull itself completes untouched."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    monkeypatch.setenv("BRUTEX_CROSSVERIFY_PUBLISH", "junk-token")
    with _make_client(
        store, monkeypatch, tmp_path, crossverify_runner=lambda _c: _cv_report()
    ) as client:
        resp = client.post(_ROUTE, params={"scope": "spot-only"})
        assert resp.status_code == 200
        snap = _wait_for_job(client)
    assert snap["state"] == "done"  # the pull is NEVER failed by tail config junk
    assert "cross-verify: not configured (ValueError" in snap["detail"]
    assert "BRUTEX_CROSSVERIFY_PUBLISH" in snap["detail"]


def test_route_tail_disabled_noted(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    monkeypatch.setenv("BRUTEX_CROSSVERIFY", "off")
    with _make_client(
        store, monkeypatch, tmp_path, crossverify_runner=lambda _c: _cv_report()
    ) as client:
        resp = client.post(_ROUTE, params={"scope": "spot-only"})
        assert resp.status_code == 200
        snap = _wait_for_job(client)
    assert "cross-verify: disabled (BRUTEX_CROSSVERIFY=off)" in snap["detail"]


def test_route_tail_skipped_when_pipeline_stopped(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A spot-gate failure stops the chain — the tail is SKIPPED, never fired."""
    _pin_creds_present(monkeypatch)

    def failing_spot(*args: Any, **kwargs: Any) -> BulkSpotReport:
        return BulkSpotReport(
            dry_run=kwargs["dry_run"],
            start_date="2020-01-01",
            end_date="2026-07-12",
            symbols=[SymbolPullResult(symbol="NIFTY", completed=0, failed=0, no_data=0)],
            still_failed_windows=5,
        )

    monkeypatch.setattr(app_module, "pull_all_spot", failing_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)
    calls, tail = _tail_spy()
    with _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client:
        resp = client.post(_ROUTE, params={"scope": "spot-only"})
        assert resp.status_code == 200
        snap = _wait_for_job(client)
    assert calls == []  # the fence never invoked the tail
    assert "cross-verify: skipped (pipeline stopped: spot)" in snap["detail"]


def test_finish_detail_is_rewritten_note(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rail 11: the job finish detail IS the surgically-rewritten note —
    spot-only prefix + F&O-by-design + Greeks + cross-verify clauses."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    calls, tail = _tail_spy()
    with _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client:
        resp = client.post(_ROUTE, params={"scope": "spot-only"})
        assert resp.status_code == 200
        assert resp.json()["scope"] == "spot-only"
        snap = _wait_for_job(client)
    assert calls == ["tail"]
    detail = snap["detail"]
    assert detail.startswith("spot-only pipeline complete — ")
    assert ", F&O skipped by design (spot-only scope)" in detail
    assert "Greeks: not run — no options in this pull" in detail
    assert "cross-verify: producer started (poll /api/v1/crossverify/progress)" in detail
    assert "0 expiries" not in detail  # [neg — B H1]


def test_movers_gated_while_prebuild_runs(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The movers prebuild gate rides the spot-only arm unchanged: gated
    mid-prebuild, released after (incl. the finally backstop)."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    release = threading.Event()
    entered = threading.Event()

    def blocking_prebuild(**kwargs: Any) -> PrebuildOutcome:
        entered.set()
        assert release.wait(timeout=15.0)
        return PrebuildOutcome(symbols_built=1, total_bars=5, failed=(), failed_count=0, ok=True)

    monkeypatch.setattr(app_module, "run_pipeline_prebuild_guarded", blocking_prebuild)
    _calls, tail = _tail_spy()
    with _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client:
        try:
            resp = client.post(_ROUTE, params={"scope": "spot-only"})
            assert resp.status_code == 200
            assert entered.wait(timeout=15.0)
            gated = client.get("/api/v1/movers").json()
            assert gated["gated"] is True
        finally:
            release.set()
        _wait_for_job(client)
        after = client.get("/api/v1/movers").json()
        assert after.get("gated") is not True
