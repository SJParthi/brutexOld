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
import os
import signal
import threading
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
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


@contextmanager
def _boot_hang_watchdog(seconds: int = 10) -> Generator[None]:
    """Turn an indefinite block inside the body into a LOUD failure.

    The fails-before shape of the round-7 P-1 defect is a boot / click that
    blocks FOREVER (a FIFO read waits for a writer — no exception for any
    belt to catch), so a plain regression would HANG rather than fail; the
    ``SIGALRM`` handler interrupts the blocked syscall and fails the test
    instead (the #1614 round-6 watchdog idiom).
    """

    def _alarm(_signum: int, _frame: object) -> None:
        raise AssertionError(f"boot/click blocked > {seconds}s (non-regular-inode hang)")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


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


def test_scope_spot_only_composes_with_selection_and_dates_narrowing_only(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[refuter r3 F-D2 pin] ``scope=spot-only`` composes with ``symbols=`` +
    ``from_date=``/``to_date=`` on the public route as a SUPPORTED NARROWING:
    the selection + the date range thread into the spot phase verbatim while
    the two F&O spines stay structurally unreachable — the extra params can
    narrow a spot-only pull but can never widen one (the B H9 law holds with
    the combination too). A future refactor must not silently turn the
    combination into a 400 (the intent RESUME re-fires through the SAME
    launch path and may legitimately carry it) or into a widening."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    with _make_client(store, monkeypatch, tmp_path) as client:
        resp = client.post(
            _ROUTE,
            params={
                "scope": "spot-only",
                "symbols": "NIFTY",
                "from_date": "2024-01-02",
                "to_date": "2024-02-01",
                "dry_run": "true",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["started"] is True
        assert body["scope"] == "spot-only"
        assert body["target_source"] == "selection"
        _wait_for_job(client)
    assert len(spines.spot_kwargs) == 1
    assert spines.spot_kwargs[0]["from_date"] == "2024-01-02"
    assert spines.spot_kwargs[0]["to_date"] == "2024-02-01"
    # Widening impossible regardless of the extra params: F&O spines at ZERO.
    assert spines.metadata_calls == 0
    assert spines.historical_calls == 0


def test_scope_spot_only_market_closed_is_composed_muhurat_belt(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[F/B — the R4 M6 mutant killer] The CAPTURED spot-only ``market_closed``
    predicate is the REAL composed Muhurat belt: called at a frozen instant
    INSIDE the 2025-10-21 afternoon session it returns True (blocked) while
    the full-arm predicate (the bare guard) returns False (the guard's
    structural Muhurat blindness). Severing the ``compose_market_closed``
    wiring in ``_launch_pipeline._work`` (the M6 mutant — bare resolve on the
    spot-only arm) fails EXACTLY this assertion; ``callable(...)`` alone
    could never catch it. The guard kill switch is armed (r1 #2 coupling —
    guard off would honestly disarm the belt), and BOTH clocks are frozen
    OUTSIDE the session for the launch itself (10:00 on the listed holiday —
    the start gate allows, the sequencer's between-phase belt stays open, the
    spot spy captures its kwargs) then re-frozen INSIDE it for the verdict."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    monkeypatch.setenv("BRUTEX_MARKET_HOURS_GUARD", "1")
    launch = datetime(2025, 10, 21, 10, 0)  # holiday; before the 13:15 margin
    monkeypatch.setattr("brutex.calendars.market_hours.now_ist", lambda: launch)
    monkeypatch.setattr("brutex.calendars.auto_pull_gate.now_ist", lambda: launch)
    _calls, tail = _tail_spy()
    with _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client:
        assert client.post(_ROUTE, params={"dry_run": "true"}).status_code == 200
        _wait_for_job(client)
        assert (
            client.post(_ROUTE, params={"scope": "spot-only", "dry_run": "true"}).status_code == 200
        )
        _wait_for_job(client)
    assert len(spines.spot_kwargs) == 2
    full_kwargs, spot_only_kwargs = spines.spot_kwargs
    bare = full_kwargs["market_closed"]
    composed = spot_only_kwargs["market_closed"]
    # Re-freeze INSIDE the 13:45-14:45 session (margined [13:15, 14:50)).
    frozen = datetime(2025, 10, 21, 14, 0)
    monkeypatch.setattr("brutex.calendars.market_hours.now_ist", lambda: frozen)
    monkeypatch.setattr("brutex.calendars.auto_pull_gate.now_ist", lambda: frozen)
    assert bare() is False  # the bare guard is provably BLIND (fails-before)
    assert composed() is True  # the dashboard-wired belt fires (passes-after)


def test_scope_spot_only_guard_off_market_closed_is_bare_base(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[refuter-r1 MINOR-B — kills the M-I hardcoded-env mutant, dashboard
    arm] With ``BRUTEX_MARKET_HOURS_GUARD=0`` the spot-only launch's CAPTURED
    ``market_closed`` IS the guard's own never-blocked base (identity — the
    compose returned it UNCHANGED) and reads False even at a frozen Muhurat
    instant: the operator's kill switch genuinely disarms the belt at the
    WIRING level. A compose call site hardcoding ``env={"…GUARD": "1"}``
    would wrap the base and read True here."""
    from brutex.calendars.market_hours import _market_never_blocked

    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    monkeypatch.setenv("BRUTEX_MARKET_HOURS_GUARD", "0")
    _calls, tail = _tail_spy()
    with _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client:
        assert (
            client.post(_ROUTE, params={"scope": "spot-only", "dry_run": "true"}).status_code == 200
        )
        _wait_for_job(client)
    assert len(spines.spot_kwargs) == 1
    captured = spines.spot_kwargs[0]["market_closed"]
    assert captured is _market_never_blocked  # compose returned the base UNCHANGED
    frozen = datetime(2024, 11, 1, 18, 30)  # inside the listed evening session
    monkeypatch.setattr("brutex.calendars.auto_pull_gate.now_ist", lambda: frozen)
    monkeypatch.setattr("brutex.calendars.market_hours.now_ist", lambda: frozen)
    assert captured() is False  # guard off ⇒ belt off — never a Muhurat block


def test_scope_spot_only_evening_muhurat_deferral_wording_and_attribution(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[refuter-r1 r2 MINOR-1 — the reproduced evening instant] a spot-only
    launch at frozen 2024-11-01 18:30 IST (inside the listed 18:00-19:00
    Muhurat session) defers via the composed belt, and the finish detail no
    longer makes the FALSE "inside 08:45-15:35" clock claim — it reads the
    both-arms wording — while the ``auto_pull_gate.muhurat_belt_halt`` WARN
    attributes the halt to the Muhurat arm in the logs."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    monkeypatch.setenv("BRUTEX_MARKET_HOURS_GUARD", "1")
    frozen = datetime(2024, 11, 1, 18, 30)  # evening session; OUT of 08:45-15:35
    monkeypatch.setattr("brutex.calendars.market_hours.now_ist", lambda: frozen)
    monkeypatch.setattr("brutex.calendars.auto_pull_gate.now_ist", lambda: frozen)
    _calls, tail = _tail_spy()
    with (
        _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client,
        capture_logs() as logs,
    ):
        resp = client.post(_ROUTE, params={"scope": "spot-only", "dry_run": "true"})
        assert resp.status_code == 200
        # 18:30 on a holiday is outside the START gate's 08:45-15:35 window,
        # so the job STARTS; the sequencer's top belt then defers on the
        # composed predicate's Muhurat arm.
        assert resp.json()["started"] is True
        snap = _wait_for_job(client)
    assert spines.spot_kwargs == []  # ZERO vendor phases ran during the session
    detail = snap["detail"]
    assert (
        "market-session protection window active "
        "(NSE hours 08:45-15:35 IST or a Muhurat session)" in detail
    )
    assert "inside NSE market-hours window (08:45-15:35 IST)" not in detail  # the false claim
    assert "F&O not applicable — spot-only scope" in detail
    assert "resume on the next out-of-hours run" not in detail  # self-contradictory at 18:30
    assert "cross-verify: skipped (dry-run)" in detail  # dry-run wins the fence order
    belt = [e for e in logs if e["event"] == "auto_pull_gate.muhurat_belt_halt"]
    assert len(belt) == 1  # the machine attribution — logs stop misattributing
    assert belt[0]["instant"] == frozen.isoformat()


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


def test_scope_spot_only_dry_run_threads_into_noop_reports(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[refuter-r1 r2 NIT-6, spy] the launcher threads its dry_run flag into
    the bound no-op builders — a dry-run job's nested F&O sub-reports never
    read ``dry_run: false`` inside an overall ``dry_run: true`` payload."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    captured: list[dict[str, Any]] = []
    real_run = app_module.run_full_pipeline

    def spy_run(**kwargs: Any) -> Any:
        captured.append(dict(kwargs))
        return real_run(**kwargs)

    monkeypatch.setattr(app_module, "run_full_pipeline", spy_run)
    _calls, tail = _tail_spy()
    with _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client:
        assert (
            client.post(_ROUTE, params={"scope": "spot-only", "dry_run": "true"}).status_code == 200
        )
        _wait_for_job(client)
    assert len(captured) == 1
    meta = captured[0]["run_metadata"]()
    hist = captured[0]["run_historical"](None)
    assert meta.dry_run is True  # echoes the launch flag, never a constant False
    assert hist.dry_run is True
    assert meta.note.startswith("spot-only scope — ")  # still the designed no-op
    assert hist.historical is None


# ── the fno-only mirror (round-6 two-button split) ───────────────────────


def test_scope_fno_only_swaps_exactly_the_spot_binding(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[spy] fno-only: the SPOT spine is NEVER called while the two F&O
    spines run for REAL (bound kwarg-for-kwarg identically to the full arm) —
    the exact mirror of the spot-only swap."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    with _make_client(store, monkeypatch, tmp_path) as client:
        resp = client.post(_ROUTE, params={"scope": "fno-only", "dry_run": "true"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["started"] is True
        assert body["scope"] == "fno-only"
        assert "auto_pull" not in body  # the additive block stays spot-only-scoped
        _wait_for_job(client)
    assert spines.spot_kwargs == []  # the spot spine is structurally unreachable
    assert spines.metadata_calls == 1
    assert spines.historical_calls == 1


def test_scope_fno_only_noop_spot_note_surgery_e2e(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[e2e negative pin — B H1, the spot axis] the REAL sequencer's note over
    the no-op spot report is surgically rewritten: the finish detail reads
    the by-design clause, never the fake-empty "spot clean (0 pair(s))"; no
    Greeks-not-run claim (options ARE pulled here) and no cross-verify clause
    (the tail is Spot-exclusive by ruling)."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    with _make_client(store, monkeypatch, tmp_path) as client:
        assert (
            client.post(_ROUTE, params={"scope": "fno-only", "dry_run": "true"}).status_code == 200
        )
        snap = _wait_for_job(client)
    detail = snap["detail"]
    assert "F&O-only pipeline complete — " in detail
    assert "spot skipped by design (F&O-only scope)" in detail
    assert "spot clean (0 pair(s))" not in detail  # the fake-empty render, dead
    assert "Greeks: not run" not in detail
    assert "cross-verify" not in detail


def test_scope_fno_only_tail_never_fires_on_a_real_run(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[negative pin — the coordinator ruling] the crossverify tail is
    Spot-EXCLUSIVE: a REAL (non-dry) fno-only run never invokes it, while the
    SAME client's real spot-only run does (the teeth — the spy provably
    fires on the spot arm)."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    calls, tail = _tail_spy()
    with _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client:
        assert client.post(_ROUTE, params={"scope": "fno-only"}).status_code == 200
        _wait_for_job(client)
        assert calls == []  # [spy] never invoked on fno-only
        assert client.post(_ROUTE, params={"scope": "spot-only"}).status_code == 200
        snap = _wait_for_job(client)
    assert calls == ["tail"]  # the SAME spy fires on the spot-only arm
    assert "cross-verify: producer started" in snap["detail"]
    assert spines.metadata_calls == 1  # fno-only ran the real F&O spines once
    assert spines.historical_calls == 1


def test_owed_marker_written_only_by_a_real_spot_only_launch(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[DEC-CROSSVERIFY-OWED-SENTINEL-001] the durable owed marker is written
    at pull-tail-commit ENTRY of a REAL spot-only run ONLY — a dry-run /
    real fno-only / real full-scope run writes nothing — and it SURVIVES the
    finished pull when the started producer never completes (the injected
    tail spy starts no runner — exactly the crash-sentinel shape; the
    runner's clear-on-completed-verdict is pinned in
    ``test_crossverify_run.py::TestOwedMarkerClear``)."""
    from brutex.calendars.nse import now_ist
    from brutex.crossverify.owed import list_owed_days

    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    _calls, tail = _tail_spy()
    # The launcher marks against the STORE-derived write root (the
    # instruments.duckdb parent — `_resolve_pull_lake_root`'s live law).
    lake = tmp_path
    with _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client:
        # A dry-run spot-only launch owes nothing.
        assert (
            client.post(_ROUTE, params={"scope": "spot-only", "dry_run": "true"}).status_code == 200
        )
        _wait_for_job(client)
        assert list_owed_days(lake) == []
        # A REAL fno-only run owes nothing (the tail is Spot-EXCLUSIVE).
        assert client.post(_ROUTE, params={"scope": "fno-only"}).status_code == 200
        _wait_for_job(client)
        assert list_owed_days(lake) == []
        # A REAL full-scope run owes nothing (no crossverify tail by design).
        assert client.post(_ROUTE).status_code == 200
        _wait_for_job(client)
        assert list_owed_days(lake) == []
        # The REAL spot-only run writes the launch-day marker — and it
        # PERSISTS past the finished pull (the crash window's sentinel).
        assert client.post(_ROUTE, params={"scope": "spot-only"}).status_code == 200
        _wait_for_job(client)
    assert list_owed_days(lake) == [now_ist().date()]


def test_owed_marker_skipped_under_the_crossverify_kill_switch(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[DEC-CROSSVERIFY-OWED-SENTINEL-001 kill-switch coherence] with
    ``BRUTEX_CROSSVERIFY=off`` a REAL spot-only run marks NOTHING — owed
    markers must never accumulate while the feature is deliberately off."""
    from brutex.crossverify.owed import list_owed_days

    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    monkeypatch.setenv("BRUTEX_CROSSVERIFY", "off")
    _calls, tail = _tail_spy()
    lake = tmp_path  # the store-derived write root
    with _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client:
        assert client.post(_ROUTE, params={"scope": "spot-only"}).status_code == 200
        _wait_for_job(client)
    assert list_owed_days(lake) == []


def test_scope_fno_only_auto_arm_400_never_arms(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[the coordinator ruling] the scheduler NEVER arms/fires fno-only:
    ``auto_arm=1`` on the fno-only scope is a clean 400 and the wired
    scheduler stays disarmed with zero launches."""
    from brutex.calendars.auto_pull_gate import AutoGateVerdict as _Verdict
    from brutex.dashboard.auto_pull_scheduler import AutoPullScheduler as _Sched
    from brutex.dashboard.auto_pull_scheduler import AutoPullStateStore as _SchedStore

    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    lake = tmp_path / "lake"
    lake.mkdir(exist_ok=True)
    scheduler = _Sched(
        _SchedStore(lake),
        gate=lambda _instant: _Verdict(allowed=True, reason="ok", detail="ok"),
        tick_seconds=3600.0,
    )
    with _make_client(store, monkeypatch, tmp_path, auto_pull_scheduler=scheduler) as client:
        resp = client.post(_ROUTE, params={"scope": "fno-only", "auto_arm": "1"})
        assert resp.status_code == 400
        assert "auto_arm requires scope=spot-only" in resp.json()["detail"]
    assert scheduler.status()["armed"] is False
    assert spines.metadata_calls == 0  # the refused click launched nothing


def test_scope_fno_only_prebuild_spot_excluded_real_cascade(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[spy] the fno-only prebuild is the spot-exclusion mirror: the guarded
    runner receives ``include_spot=False`` with the REAL F&O cascade walk
    still enumerated; the full arm AND the spot-only arm keep
    ``include_spot=True``."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    fno_walks: list[str] = []
    monkeypatch.setattr(
        app_module,
        "list_fno_candle_datasets",
        lambda _lake: fno_walks.append("walk") or [],
    )
    prebuild_kwargs: list[dict[str, Any]] = []

    def fake_prebuild(**kwargs: Any) -> PrebuildOutcome:
        kwargs["cascade_or_error"]()  # force the cascade factory
        prebuild_kwargs.append(dict(kwargs))
        return PrebuildOutcome(symbols_built=1, total_bars=5, failed=(), failed_count=0, ok=True)

    monkeypatch.setattr(app_module, "run_pipeline_prebuild_guarded", fake_prebuild)
    _calls, tail = _tail_spy()
    with _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client:
        assert (
            client.post(_ROUTE, params={"scope": "fno-only", "dry_run": "true"}).status_code == 200
        )
        _wait_for_job(client)
        assert fno_walks == ["walk"]  # the REAL F&O cascade IS enumerated
        assert prebuild_kwargs[-1]["include_spot"] is False
        assert client.post(_ROUTE, params={"dry_run": "true"}).status_code == 200
        _wait_for_job(client)
        assert prebuild_kwargs[-1]["include_spot"] is True
        assert (
            client.post(_ROUTE, params={"scope": "spot-only", "dry_run": "true"}).status_code == 200
        )
        _wait_for_job(client)
    assert prebuild_kwargs[-1]["include_spot"] is True
    assert fno_walks == ["walk", "walk"]  # spot-only never walked (the empty cascade)


def test_scope_fno_only_segment_movers_futures_options_only(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the FUTURES + OPTIONS movers warmers are injected — the spot
    segment is honestly ABSENT (never a fabricated 0-count segment); the
    exact mirror of the spot-only warm law."""
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
    with _make_client(store, monkeypatch, tmp_path) as client:
        with capture_logs() as logs:
            assert (
                client.post(_ROUTE, params={"scope": "fno-only", "dry_run": "true"}).status_code
                == 200
            )
            _wait_for_job(client)
        started = [e for e in logs if e["event"] == "pipeline.movers_warm.started"]
        assert len(started) == 1
        assert started[0]["segments"] == ["futures", "options"]
        done = [e for e in logs if e["event"] == "pipeline.movers_warm.done"]
        assert len(done) == 1
        assert set(done[0]["warmed"]) == {"futures", "options"}


def test_scope_fno_only_market_closed_is_composed_muhurat_belt(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[the round-6 fno-only twin of the R4 M6 mutant killer] the fno-only
    arm's ``market_closed`` predicate (captured from the REAL metadata spine
    kwargs — the spot spine never runs here) is the composed Muhurat belt:
    True inside the frozen 2025-10-21 afternoon session while the full arm's
    bare guard reads False (its structural Muhurat blindness)."""
    _pin_creds_present(monkeypatch)
    metadata_kwargs: list[dict[str, Any]] = []

    def spy_metadata(*args: Any, **kwargs: Any) -> MetadataPullReport:
        metadata_kwargs.append(dict(kwargs))
        return _fake_metadata(*args, **kwargs)

    monkeypatch.setattr(app_module, "pull_all_spot", _fake_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", spy_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)
    monkeypatch.setenv("BRUTEX_MARKET_HOURS_GUARD", "1")
    launch = datetime(2025, 10, 21, 10, 0)  # holiday; before the 13:15 margin
    monkeypatch.setattr("brutex.calendars.market_hours.now_ist", lambda: launch)
    monkeypatch.setattr("brutex.calendars.auto_pull_gate.now_ist", lambda: launch)
    with _make_client(store, monkeypatch, tmp_path) as client:
        # NOTE: the dry-run metadata binding threads market_closed=None, so a
        # REAL run captures the launcher-composed predicate.
        assert client.post(_ROUTE, params={"scope": "fno-only"}).status_code == 200
        _wait_for_job(client)
        assert client.post(_ROUTE).status_code == 200
        _wait_for_job(client)
    assert len(metadata_kwargs) == 2
    composed = metadata_kwargs[0]["market_closed"]
    bare = metadata_kwargs[1]["market_closed"]
    frozen = datetime(2025, 10, 21, 14, 0)  # inside the margined Muhurat session
    monkeypatch.setattr("brutex.calendars.market_hours.now_ist", lambda: frozen)
    monkeypatch.setattr("brutex.calendars.auto_pull_gate.now_ist", lambda: frozen)
    assert bare() is False  # the bare guard is provably BLIND
    assert composed() is True  # the fno-only belt fires


def test_scope_fno_only_market_hours_start_gate_defers(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The market-hours START gate is inherited by the fno-only arm too."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    monkeypatch.setenv("BRUTEX_MARKET_HOURS_GUARD", "1")
    monkeypatch.setattr(
        "brutex.calendars.market_hours.now_ist",
        lambda: datetime(2024, 1, 2, 10, 0),  # Tue, trading day, in-window
    )
    with _make_client(store, monkeypatch, tmp_path) as client:
        resp = client.post(_ROUTE, params={"scope": "fno-only"})
    assert resp.status_code == 200
    assert resp.json()["started"] is False
    assert spines.metadata_calls == 0  # no vendor phase ever ran


def test_scope_fno_only_dry_run_threads_into_noop_spot_report(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[spy — the r2 NIT-6 law on the spot axis] the launcher threads its
    dry_run flag into the bound no-op SPOT builder."""
    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    captured: list[dict[str, Any]] = []
    real_run = app_module.run_full_pipeline

    def spy_run(**kwargs: Any) -> Any:
        captured.append(dict(kwargs))
        return real_run(**kwargs)

    monkeypatch.setattr(app_module, "run_full_pipeline", spy_run)
    with _make_client(store, monkeypatch, tmp_path) as client:
        assert (
            client.post(_ROUTE, params={"scope": "fno-only", "dry_run": "true"}).status_code == 200
        )
        _wait_for_job(client)
    assert len(captured) == 1
    spot = captured[0]["run_spot"]()
    assert spot.dry_run is True  # echoes the launch flag, never a constant False
    assert spot.symbols == []  # still the designed no-op
    assert captured[0]["warm_spot_movers"] is None  # the spot warm is honestly absent


def test_scope_fno_only_intent_resumes_fno_only(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[the resume-pin extension] a persisted ``scope="fno-only"`` intent
    resumes FNO-ONLY through the production boot-resume seam: the spot spine
    stays at ZERO calls while the F&O spines run — never a silently-widened
    (or spot-widened) pull."""
    from brutex.dashboard.pull_intent import PullIntent, PullIntentStore

    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    lake = tmp_path / "lake"
    lake.mkdir(exist_ok=True)
    intent_store = PullIntentStore(lake)
    intent_store.write(
        PullIntent(symbols=None, symbol=None, from_date=None, to_date=None, scope="fno-only")
    )
    with _make_client(store, monkeypatch, tmp_path, pull_intent_store=intent_store) as client:
        client.app.state.brutex_resume_pipeline()  # type: ignore[attr-defined]
        snap = _wait_for_job(client)
    assert snap["state"] == "done"
    assert spines.spot_kwargs == []  # the resumed run never widened onto spot
    assert spines.metadata_calls == 1
    assert spines.historical_calls == 1
    assert "F&O-only pipeline complete" in snap["detail"]


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
        publish_strict=3,
        publish_observe=0,
        withheld=0,
        blocked=0,
        nothing=2,
        excluded=0,
        publish_failed=0,
        recovered_prior=0,
        regated=0,
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


# ── the self-arming daily auto-pull (round-5 fold) ───────────────────────────
#
# DEC-PULL-SCOPED-ALL-ONECLICK-001 round-5 fold: the ▶ button's auto_arm=1
# arms the AutoPullScheduler through the SAME launcher, the scheduler's fire
# seam IS `_launch_pipeline(scope=spot-only)` (all rails inherited by
# construction), and every real spot-only run reports its completion so a
# successful manual click counts as the day's auto pull.

from brutex.calendars.auto_pull_gate import AutoGateVerdict  # noqa: E402
from brutex.calendars.nse import now_ist  # noqa: E402
from brutex.dashboard.auto_pull_scheduler import (  # noqa: E402
    AutoPullScheduler,
    AutoPullState,
    AutoPullStateStore,
)

_AUTO_ROUTE = "/api/v1/universe/pull-all-spot-auto"


def _wired_scheduler(tmp_path: Path) -> AutoPullScheduler:
    """A scheduler over the SAME lake root `_make_client` provisions —
    always-open injected gate (the gate module has its own 100% suite), an
    hour-scale tick so the ticker thread stays inert during the test."""
    lake = tmp_path / "lake"
    lake.mkdir(exist_ok=True)
    return AutoPullScheduler(
        AutoPullStateStore(lake),
        gate=lambda _instant: AutoGateVerdict(allowed=True, reason="ok", detail="ok"),
        tick_seconds=3600.0,
    )


class TestAutoArmRoute:
    def test_auto_arm_requires_spot_only_400(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _pin_creds_present(monkeypatch)
        scheduler = _wired_scheduler(tmp_path)
        with _make_client(store, monkeypatch, tmp_path, auto_pull_scheduler=scheduler) as client:
            resp = client.post(_ROUTE, params={"auto_arm": "1", "dry_run": "true"})
        assert resp.status_code == 400
        assert "auto_arm requires scope=spot-only" in resp.json()["detail"]
        assert scheduler.status()["armed"] is False  # a refused arm arms nothing

    def test_auto_arm_dry_run_400(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _pin_creds_present(monkeypatch)
        scheduler = _wired_scheduler(tmp_path)
        with _make_client(store, monkeypatch, tmp_path, auto_pull_scheduler=scheduler) as client:
            resp = client.post(
                _ROUTE, params={"scope": "spot-only", "auto_arm": "1", "dry_run": "true"}
            )
        assert resp.status_code == 400
        assert "dry-run must never arm" in resp.json()["detail"]
        assert scheduler.status()["armed"] is False

    def test_auto_arm_without_scheduler_503(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _pin_creds_present(monkeypatch)
        spines = _Spines()
        spines.wire(monkeypatch)
        with _make_client(store, monkeypatch, tmp_path) as client:
            resp = client.post(_ROUTE, params={"scope": "spot-only", "auto_arm": "1"})
        assert resp.status_code == 503
        assert "auto-pull scheduler unavailable" in resp.json()["detail"]
        assert spines.spot_kwargs == []  # the refusal precedes any launch

    def test_button_click_arms_and_runs_manual_success_marks_the_day(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The operator semantics: first click = run now + arm forever; the
        successful MANUAL run counts as the day's auto pull (no redundant
        re-fire), and the armed state is durable on disk."""
        _pin_creds_present(monkeypatch)
        spines = _Spines()
        spines.wire(monkeypatch)
        _calls, tail = _tail_spy()
        scheduler = _wired_scheduler(tmp_path)
        with _make_client(
            store,
            monkeypatch,
            tmp_path,
            crossverify_tail=tail,
            auto_pull_scheduler=scheduler,
        ) as client:
            resp = client.post(_ROUTE, params={"scope": "spot-only", "auto_arm": "1"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["started"] is True
            assert body["auto_pull"]["armed"] is True  # the response confirms the arming
            _wait_for_job(client)
        today = now_ist().date().isoformat()
        on_disk = AutoPullStateStore(tmp_path / "lake").load()
        assert on_disk.armed is True  # durable — survives a dashboard restart
        assert on_disk.last_success_date == today  # manual success marks the day
        assert scheduler.tick_once() == "already_succeeded"  # no redundant auto re-fire

    def test_market_hours_refused_click_still_arms(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A 10:00 IST click is start-REFUSED by the inherited guard — but the
        arming took effect and the response says BOTH."""
        _pin_creds_present(monkeypatch)
        spines = _Spines()
        spines.wire(monkeypatch)
        monkeypatch.setenv("BRUTEX_MARKET_HOURS_GUARD", "1")
        monkeypatch.setattr(
            "brutex.calendars.market_hours.now_ist",
            lambda: datetime(2024, 1, 2, 10, 0),  # Tue, trading day, in-window
        )
        scheduler = _wired_scheduler(tmp_path)
        with _make_client(store, monkeypatch, tmp_path, auto_pull_scheduler=scheduler) as client:
            resp = client.post(_ROUTE, params={"scope": "spot-only", "auto_arm": "1"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["started"] is False  # the guard refused the immediate run
        assert body["auto_pull"]["armed"] is True  # …but the arming stands
        assert AutoPullStateStore(tmp_path / "lake").load().armed is True
        assert spines.spot_kwargs == []

    def test_default_scope_response_carries_no_auto_pull_key(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Even with a scheduler wired, the default/full payload keeps the
        pinned pre-fold shape — auto_pull is spot-only-arm additive."""
        _pin_creds_present(monkeypatch)
        spines = _Spines()
        spines.wire(monkeypatch)
        scheduler = _wired_scheduler(tmp_path)
        with _make_client(store, monkeypatch, tmp_path, auto_pull_scheduler=scheduler) as client:
            resp = client.post(_ROUTE, params={"dry_run": "true"})
            assert resp.status_code == 200
            assert "auto_pull" not in resp.json()
            _wait_for_job(client)

    def test_toggle_route_disarms_and_rearms_durably(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _pin_creds_present(monkeypatch)
        scheduler = _wired_scheduler(tmp_path)
        with _make_client(store, monkeypatch, tmp_path, auto_pull_scheduler=scheduler) as client:
            armed = client.post(_AUTO_ROUTE, params={"armed": "1"})
            assert armed.status_code == 200
            assert armed.json()["auto_pull"]["armed"] is True
            assert AutoPullStateStore(tmp_path / "lake").load().armed is True
            disarmed = client.post(_AUTO_ROUTE, params={"armed": "0"})
            assert disarmed.status_code == 200
            assert disarmed.json()["auto_pull"]["armed"] is False
            assert AutoPullStateStore(tmp_path / "lake").load().armed is False
        assert scheduler.tick_once() == "disarmed"  # disarm stops firing

    def test_toggle_route_absent_without_scheduler(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _pin_creds_present(monkeypatch)
        with _make_client(store, monkeypatch, tmp_path) as client:
            resp = client.post(_AUTO_ROUTE, params={"armed": "1"})
        assert resp.status_code == 404  # the route registers only when wired

    def test_pull_progress_carries_auto_pull_when_wired(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _pin_creds_present(monkeypatch)
        scheduler = _wired_scheduler(tmp_path)
        with _make_client(store, monkeypatch, tmp_path, auto_pull_scheduler=scheduler) as client:
            snap = client.get("/api/v1/universe/pull-progress").json()
        assert snap["auto_pull"]["armed"] is False
        assert "next_eligible" in snap["auto_pull"]
        with _make_client(store, monkeypatch, tmp_path) as client:
            bare = client.get("/api/v1/universe/pull-progress").json()
        assert "auto_pull" not in bare  # scheduler-less apps stay byte-identical

    def test_scheduler_fire_rides_the_real_launcher_and_success_marks(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The fire seam IS `_launch_pipeline(scope=spot-only)`: a tick launches
        the REAL job (spot spine runs, F&O spines never), completion marks
        success, and the next tick is the once-per-day no-op."""
        _pin_creds_present(monkeypatch)
        spines = _Spines()
        spines.wire(monkeypatch)
        _calls, tail = _tail_spy()
        scheduler = _wired_scheduler(tmp_path)
        with _make_client(
            store,
            monkeypatch,
            tmp_path,
            crossverify_tail=tail,
            auto_pull_scheduler=scheduler,
        ) as client:
            scheduler.arm()
            assert scheduler.tick_once() == "fired"
            _wait_for_job(client)
            assert scheduler.tick_once() == "already_succeeded"
        assert len(spines.spot_kwargs) == 1  # the real spot spine ran exactly once
        assert spines.metadata_calls == 0  # spot-only: the F&O spines never ran
        assert spines.historical_calls == 0
        today = now_ist().date().isoformat()
        on_disk = AutoPullStateStore(tmp_path / "lake").load()
        assert on_disk.last_attempt_date == today
        assert on_disk.last_success_date == today

    def test_scheduler_fire_while_job_busy_is_refused_retry(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The sweep/pull coexistence law: a held job slot refuses the fire —
        the scheduler defers and retries (no attempt burned), and the retry
        after release fires."""
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
        scheduler = _wired_scheduler(tmp_path)
        with _make_client(
            store,
            monkeypatch,
            tmp_path,
            crossverify_tail=tail,
            auto_pull_scheduler=scheduler,
        ) as client:
            scheduler.arm()
            try:
                first = client.post(_ROUTE, params={"scope": "spot-only"})
                assert first.json()["started"] is True
                assert entered.wait(timeout=15.0)
                # The job slot is held mid-run — the tick's launch is refused,
                # NOT an attempt (retried on later ticks: the deferral catch-up).
                assert scheduler.tick_once() == "refused_retry"
                assert AutoPullStateStore(tmp_path / "lake").load().last_attempt_date is None
            finally:
                release.set()
            _wait_for_job(client)
            # The manual run completed ok → the day is marked done; the
            # post-release tick needs no redundant re-fire.
            assert scheduler.tick_once() == "already_succeeded"

    def test_stopped_pipeline_marks_no_success(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A spot-gate-stopped run reports ok=False → last_success_date stays
        unset (the scheduler retries on the next eligible day)."""
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
        _calls, tail = _tail_spy()
        scheduler = _wired_scheduler(tmp_path)
        with _make_client(
            store,
            monkeypatch,
            tmp_path,
            crossverify_tail=tail,
            auto_pull_scheduler=scheduler,
        ) as client:
            scheduler.arm()
            resp = client.post(_ROUTE, params={"scope": "spot-only"})
            assert resp.status_code == 200
            _wait_for_job(client)
        assert AutoPullStateStore(tmp_path / "lake").load().last_success_date is None

    def test_scheduler_fire_credential_refusal_maps_to_started_false(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A credential 503 raised SYNCHRONOUSLY by ``_launch_pipeline`` during
        the scheduler's fire is caught by ``_auto_spot_launcher``'s
        ``except HTTPException`` arm → the fire reads ``started=False`` with the
        honest ``launch rejected (503)`` reason, so the tick is a NON-attempt
        deferral (``refused_retry``) the scheduler retries once the credential
        outage heals — never an unhandled raise that would kill the ticker
        thread (app.py lines 5256-5257, the fire seam's rejection arm)."""
        # Deliberately NOT `_pin_creds_present`: the LOUD resolver reports the
        # fix text (creds missing), so the real-pull credential gate refuses.
        monkeypatch.setattr(
            app_module,
            "groww_credentials_error",
            lambda: "groww credentials unavailable (test): fix the AWS SSM params",
        )
        scheduler = _wired_scheduler(tmp_path)
        with _make_client(store, monkeypatch, tmp_path, auto_pull_scheduler=scheduler):
            scheduler.arm()
            assert scheduler.tick_once() == "refused_retry"
        on_disk = AutoPullStateStore(tmp_path / "lake").load()
        assert on_disk.last_attempt_date is None  # a 503-refused fire is not an attempt
        assert on_disk.last_success_date is None  # …and marks no success

    def test_boot_lifespan_resumes_armed_ticker(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """BOOT CATCH-UP wiring: a durably-armed state file + a fresh app ⇒ the
        lifespan resumes the ticker (thread alive inside the context, stopped
        after); a disarmed boot spawns nothing."""
        _pin_creds_present(monkeypatch)
        lake = tmp_path / "lake"
        lake.mkdir(exist_ok=True)
        AutoPullStateStore(lake).save(AutoPullState(armed=True, armed_at="2026-07-13T18:00:00"))
        scheduler = _wired_scheduler(tmp_path)
        with _make_client(store, monkeypatch, tmp_path, auto_pull_scheduler=scheduler):
            names = [t.name for t in threading.enumerate()]
            assert "brutex-auto-pull-scheduler" in names
        disarmed_scheduler = _wired_scheduler(tmp_path)
        disarmed_scheduler.disarm()
        with _make_client(store, monkeypatch, tmp_path, auto_pull_scheduler=disarmed_scheduler):
            names = [t.name for t in threading.enumerate()]
            assert "brutex-auto-pull-scheduler" not in names

    @pytest.mark.parametrize(
        "corruption",
        ["invalid_utf8", "nesting_bomb", "oversized"],
    )
    def test_boot_lifespan_survives_every_state_corruption_shape(
        self,
        store: InstrumentStore,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        corruption: str,
    ) -> None:
        """[F/B — refuter round 6 N-1, the REAL build_app lifespan] a
        bit-rotted / nesting-bomb / oversized ``.pull_all_spot_auto.json``
        boots CLEAN and DISARMED with ONE ``auto_pull.state_corrupt`` WARN.
        Executed pre-fix at the round-5 head: the invalid-UTF-8 byte CRASHED
        BOOT (uvicorn "Application startup failed", ``UnicodeDecodeError``)
        and every restart re-crashed until the sidecar was hand-deleted;
        the nesting bomb raised ``RecursionError`` the same way."""
        from brutex.dashboard.auto_pull_scheduler import (
            AUTO_PULL_STATE_FILENAME,
            MAX_STATE_BYTES,
        )

        _pin_creds_present(monkeypatch)
        lake = tmp_path / "lake"
        lake.mkdir(exist_ok=True)
        state_path = lake / AUTO_PULL_STATE_FILENAME
        if corruption == "invalid_utf8":
            state_path.write_bytes(b'\xff{"schema": 1, "armed": true}')
        elif corruption == "nesting_bomb":
            depth = 200_000
            state_path.write_text("[" * depth + "]" * depth, encoding="utf-8")
        else:
            body = b'{"schema": 1, "armed": true}'
            state_path.write_bytes(body + b" " * (MAX_STATE_BYTES + 1 - len(body)))
        scheduler = _wired_scheduler(tmp_path)
        with (
            capture_logs() as logs,
            _make_client(store, monkeypatch, tmp_path, auto_pull_scheduler=scheduler) as client,
        ):
            # Boot survived; the corrupt file reads DISARMED (zero threads).
            names = [t.name for t in threading.enumerate()]
            assert "brutex-auto-pull-scheduler" not in names
            snap = client.get("/api/v1/universe/pull-progress").json()
            assert snap["auto_pull"]["armed"] is False
        corrupt = [e for e in logs if e["event"] == "auto_pull.state_corrupt"]
        assert len(corrupt) == 1

    def test_boot_lifespan_belt_survives_a_raising_scheduler(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """[refuter round 6 N-1, the defense-in-depth belt] even a scheduler
        whose ``start_if_armed`` RAISES cannot kill dashboard boot — the
        lifespan guard logs ``auto_pull.boot_start_failed`` and serves (the
        run_auto_resume never-crash-boot posture)."""
        _pin_creds_present(monkeypatch)
        scheduler = _wired_scheduler(tmp_path)

        def _boom() -> bool:
            raise RuntimeError("scheduler exploded at boot")

        monkeypatch.setattr(scheduler, "start_if_armed", _boom)
        with (
            capture_logs() as logs,
            _make_client(store, monkeypatch, tmp_path, auto_pull_scheduler=scheduler) as client,
        ):
            assert client.get("/api/v1/universe/pull-progress").status_code == 200
        failed = [e for e in logs if e["event"] == "auto_pull.boot_start_failed"]
        assert len(failed) == 1
        assert failed[0]["error_type"] == "RuntimeError"

    def test_boot_lifespan_survives_fifo_state_file(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """[F/B — refuter round 7 P-1, the REAL build_app lifespan] a FIFO
        (non-regular inode) planted at ``.pull_all_spot_auto.json`` boots
        CLEAN and DISARMED with ONE ``auto_pull.state_corrupt`` WARN naming
        the shape. Fails-before (executed at 68d18dea in a bounded
        subprocess): the boot hook's plain ``read_bytes`` blocked FOREVER
        waiting for a FIFO writer — NO exception, so the round-6 belt never
        fired and uvicorn never reached "Application startup complete" (a
        SILENT, permanent boot hang, strictly worse than the N-1 crash);
        the SIGALRM watchdog turns a regression back into that hang into a
        loud failure."""
        from brutex.dashboard.auto_pull_scheduler import AUTO_PULL_STATE_FILENAME

        _pin_creds_present(monkeypatch)
        lake = tmp_path / "lake"
        lake.mkdir(exist_ok=True)
        os.mkfifo(lake / AUTO_PULL_STATE_FILENAME)
        scheduler = _wired_scheduler(tmp_path)
        with (
            _boot_hang_watchdog(),
            capture_logs() as logs,
            _make_client(store, monkeypatch, tmp_path, auto_pull_scheduler=scheduler) as client,
        ):
            names = [t.name for t in threading.enumerate()]
            assert "brutex-auto-pull-scheduler" not in names  # disarmed: zero threads
            snap = client.get("/api/v1/universe/pull-progress").json()
            assert snap["auto_pull"]["armed"] is False
        corrupt = [e for e in logs if e["event"] == "auto_pull.state_corrupt"]
        assert len(corrupt) == 1
        assert "not a regular file" in str(corrupt[0]["detail"])

    def test_scope_fno_only_never_marks_the_days_auto_pull(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """[the round-6 ruling's MISSING teeth — refuter round 7 Q-1, mutant
        M-Q8 killer] a SUCCESSFUL real fno-only run must NEVER count as the
        day's spot auto-pull: with an ARMED wired scheduler,
        ``note_run_complete`` does not fire on the fno-only arm — the on-disk
        ``last_success_date`` / ``last_attempt_date`` stay unset, so the
        scheduler still owes today's SPOT pull (under the mutant the armed
        once-per-day guard would read ``already_succeeded`` and silently skip
        the day's spot data — the exact freshness gap the ruling forbids)."""
        _pin_creds_present(monkeypatch)
        spines = _Spines()
        spines.wire(monkeypatch)
        scheduler = _wired_scheduler(tmp_path)
        with _make_client(store, monkeypatch, tmp_path, auto_pull_scheduler=scheduler) as client:
            scheduler.arm()
            resp = client.post(_ROUTE, params={"scope": "fno-only"})
            assert resp.status_code == 200
            snap = _wait_for_job(client)
        assert snap["state"] == "done"
        assert "F&O-only pipeline complete" in snap["detail"]  # the run WAS a success
        assert spines.metadata_calls == 1
        on_disk = AutoPullStateStore(tmp_path / "lake").load()
        assert on_disk.armed is True  # the F&O run never touched the arming either
        assert on_disk.last_success_date is None  # today's SPOT pull is still owed
        assert on_disk.last_attempt_date is None
        assert scheduler.status()["last_success_date"] is None


def test_scope_fno_only_never_clobbers_pull_verdict(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[the round-6 ruling's MISSING teeth — refuter round 7 Q-1, mutant M-Q6
    killer] the fno-only arm's DESIGNED no-op spot report must never clobber
    ``/api/v1/universe/pull-verdict``: a prior REAL spot-only run's RED
    ``transient_residual`` banner survives a completed fno-only run
    byte-for-byte (under the mutant the no-op's vacuous "clean" verdict would
    mask the red banner — the B H1 honesty class)."""

    def failing_spot(*args: Any, **kwargs: Any) -> BulkSpotReport:
        return BulkSpotReport(
            dry_run=kwargs["dry_run"],
            start_date="2020-01-01",
            end_date="2026-07-12",
            symbols=[SymbolPullResult(symbol="NIFTY", completed=0, failed=0, no_data=0)],
            still_failed_windows=5,
        )

    _pin_creds_present(monkeypatch)
    monkeypatch.setattr(app_module, "pull_all_spot", failing_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)
    _calls, tail = _tail_spy()
    with _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client:
        assert client.post(_ROUTE, params={"scope": "spot-only"}).status_code == 200
        _wait_for_job(client)
        before = client.get("/api/v1/universe/pull-verdict").json()
        assert before["present"] is True
        assert before["kind"] == "transient_residual"  # the REAL red banner
        assert client.post(_ROUTE, params={"scope": "fno-only"}).status_code == 200
        snap = _wait_for_job(client)
        after = client.get("/api/v1/universe/pull-verdict").json()
    assert snap["state"] == "done"
    assert "F&O-only pipeline complete" in snap["detail"]  # the fno run DID finish
    assert after == before  # byte-equal: the no-op spot verdict never lands


def test_scope_fno_only_failure_never_clobbers_pull_verdict(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[refuter round 8 S-1 — the FAILURE arm's teeth, mutant M-S1 killer] a
    FAILED fno-only run must never clobber the verdict either: the round-7
    pin drives a SUCCESSFUL fno-only run, so a failure-conditioned regression
    (``and (not fno_only or not report.ok)`` — clobber only when the fno-only
    run FAILED) survived it plus the wider battery (205 tests). Under that
    mutant a stopped-after-metadata fno-only run overwrites a prior REAL red
    ``transient_residual`` banner with the no-op spot report's vacuous
    verdict (the same B H1 class, failure-conditioned). Here the fno-only
    run's metadata phase FAILS (``expiry_failures=1`` → the chain stops,
    ``report.ok`` False) and the prior banner must survive byte-for-byte."""

    def failing_spot(*args: Any, **kwargs: Any) -> BulkSpotReport:
        return BulkSpotReport(
            dry_run=kwargs["dry_run"],
            start_date="2020-01-01",
            end_date="2026-07-12",
            symbols=[SymbolPullResult(symbol="NIFTY", completed=0, failed=0, no_data=0)],
            still_failed_windows=5,
        )

    def failing_metadata(*args: Any, **kwargs: Any) -> MetadataPullReport:
        return MetadataPullReport(
            dry_run=kwargs["dry_run"],
            years=tuple(kwargs["years"]),
            underlyings=("NIFTY",),
            expiry_cells=1,
            cells_skipped=0,
            expiry_failures=1,  # the metadata gate fails → report.ok is False
            expiries_persisted=0,
            contract_jobs=0,
            contract_failures=0,
            contracts_persisted=0,
            note="offline failing fake",
        )

    _pin_creds_present(monkeypatch)
    monkeypatch.setattr(app_module, "pull_all_spot", failing_spot)
    monkeypatch.setattr(app_module, "pull_expiries_contracts", failing_metadata)
    monkeypatch.setattr(app_module, "pull_historical_for_underlyings", _fake_historical)
    _calls, tail = _tail_spy()
    with _make_client(store, monkeypatch, tmp_path, crossverify_tail=tail) as client:
        assert client.post(_ROUTE, params={"scope": "spot-only"}).status_code == 200
        _wait_for_job(client)
        before = client.get("/api/v1/universe/pull-verdict").json()
        assert before["present"] is True
        assert before["kind"] == "transient_residual"  # the REAL red banner
        assert client.post(_ROUTE, params={"scope": "fno-only"}).status_code == 200
        snap = _wait_for_job(client)
        after = client.get("/api/v1/universe/pull-verdict").json()
    assert snap["state"] == "done"
    # The fno-only run genuinely FAILED — the arm the round-7 pin never drove.
    assert "stopped after metadata" in snap["detail"]
    assert after == before  # byte-equal: a FAILED fno run never lands its no-op verdict


def test_click_with_fifo_intent_file_recovers(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[F/B — refuter round 7 P-1, the REAL click path] a FIFO planted at
    ``.pull_intent.json`` no longer wedges a REAL spot-only /pull-pipeline
    click: the launcher's held-mutex ``prior_intent = store.load()`` snapshot
    reads it as corrupt (one ``pull_intent.corrupt`` WARN naming the shape),
    unlinks it, and the pull runs to completion with a fresh regular intent
    lifecycle. Fails-before (executed at 68d18dea in a bounded subprocess):
    the POST never returned — the request thread blocked forever inside the
    held mutex."""
    from brutex.dashboard.pull_intent import PullIntentStore

    _pin_creds_present(monkeypatch)
    spines = _Spines()
    spines.wire(monkeypatch)
    lake = tmp_path / "lake"
    lake.mkdir(exist_ok=True)
    intent_store = PullIntentStore(lake)
    os.mkfifo(intent_store.path)
    with (
        capture_logs() as logs,
        _make_client(store, monkeypatch, tmp_path, pull_intent_store=intent_store) as client,
    ):
        with _boot_hang_watchdog():
            # The pre-fix hang site: the POST's held-mutex intent snapshot.
            resp = client.post(_ROUTE, params={"scope": "spot-only"})
        assert resp.status_code == 200
        assert resp.json()["started"] is True
        snap = _wait_for_job(client)
    assert snap["state"] == "done"
    corrupt = [e for e in logs if e["event"] == "pull_intent.corrupt"]
    assert len(corrupt) == 1
    assert corrupt[0]["shape"] == "not_regular"
    assert not intent_store.path.exists()  # cleared by the finished run's on_finally
    assert len(spines.spot_kwargs) == 1  # the click genuinely pulled
