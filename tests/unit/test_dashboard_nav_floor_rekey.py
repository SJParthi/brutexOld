"""The floorless-family re-key (DEC-NAV-CACHES-FLOOR-REKEY-001).

The R8 nav-latency trio (`/api/v1/coverage`, `/api/v1/universe/completeness`,
`/api/v1/universe/mapping-integrity`) and the R9 `/results` leaderboard cache
folded their freshness token into the cache KEY at ``generation=0`` — so the
pull-time adaptive floor was DOUBLY unreachable (no probe wired AND a cold key
minted per mid-pull poll). This suite is the REACHABILITY PROOF the old keying
failed: during a simulated pull, a ``mark_dirty``-storm poll run is FLOORED
(one compute + logged ``movers_cache.stale_served`` serves — fails-before: one
full rebuild per poll), while a NO-pull generation move still recomputes
immediately and an instrument-master move still invalidates (the composite
token semantics are preserved through the ``revision_generation`` bridge).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")
pytest.importorskip("polars", reason="the leaderboard reads per-tuple Parquet stores")

from brutex.dashboard import app as app_module
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.summary_cache import SummaryCache
from brutex.data.instrument_store import InstrumentStore
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient
from structlog.testing import capture_logs


def _instrument(symbol: str, security_id: int) -> Instrument:
    return Instrument(
        exch_id=Exchange.NSE,
        security_id=security_id,
        symbol_name=symbol,
        display_name=symbol,
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


def _row(seed: int) -> ResultRow:
    return ResultRow(
        run_id=f"{seed:064x}",
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        mode="A",
        mask_bits=1,
        net_pnl_paisa=100_00,
        gross_profit_paisa=200_00,
        gross_loss_paisa=100_00,
        max_drawdown_paisa=50_00,
        expectancy_paisa=10_00,
        total_trades=10,
        winning_trades=6,
        losing_trades=4,
        sharpe_ratio=1.0,
        sortino_ratio=1.2,
        calmar_ratio=0.9,
        profit_factor=2.0,
        win_rate=0.6,
        max_drawdown_pct=0.05,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
    )


def _write_sweeps(lake_root: Path, rows: list[ResultRow], *, tf: str = "5m") -> Path:
    dest = lake_root / "sweeps" / "NIFTY" / tf / f"bull_k1_{'a' * 16}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    store = ResultsStore(path=dest)
    store.extend(rows)
    store.flush()
    return dest


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_instrument("NIFTY", 13)],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def summary_cache() -> SummaryCache:
    return SummaryCache()


@pytest.fixture
def pull_flag() -> dict[str, bool]:
    return {"on": False}


@pytest.fixture
def client(
    store: InstrumentStore, summary_cache: SummaryCache, pull_flag: dict[str, bool]
) -> Iterator[TestClient]:
    app = build_app(
        DashboardState(),
        instrument_store=store,
        summary_cache=summary_cache,
        pull_active_probe=lambda: pull_flag["on"],
    )
    with TestClient(app) as c:
        yield c


def _stale_served(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == "movers_cache.stale_served"]


def _spy(monkeypatch: pytest.MonkeyPatch, name: str) -> dict[str, int]:
    """Count calls through ``app_module.<name>`` (the route's resolution site)."""
    calls = {"n": 0}
    real = getattr(app_module, name)

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(app_module, name, _wrapped)
    return calls


class TestCoverageFloor:
    def test_mid_pull_mark_dirty_storm_is_floored(
        self,
        client: TestClient,
        summary_cache: SummaryCache,
        pull_flag: dict[str, bool],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REACHABILITY PROOF (fails-before: the old token-in-key fold minted a
        cold key per mid-pull poll — 6 rebuilds for 6 polls; now ONE build +
        floored serves, each logged)."""
        calls = _spy(monkeypatch, "build_coverage_payload")
        first = client.get("/api/v1/coverage").json()
        assert calls["n"] == 1
        pull_flag["on"] = True
        with capture_logs() as logs:
            for _ in range(5):
                summary_cache.mark_dirty()  # the mid-pull window-tick storm
                assert client.get("/api/v1/coverage").json() == first
        assert calls["n"] == 1  # floored — zero rebuilds across the storm
        assert len(_stale_served(logs)) == 5  # every stale serve is LOGGED

    def test_no_pull_generation_move_recomputes(
        self,
        client: TestClient,
        summary_cache: SummaryCache,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without a running pull the floor never engages — a generation move
        recomputes immediately (the pre-change invalidation semantics)."""
        calls = _spy(monkeypatch, "build_coverage_payload")
        client.get("/api/v1/coverage")
        summary_cache.mark_dirty()
        client.get("/api/v1/coverage")
        assert calls["n"] == 2

    def test_master_revision_move_still_invalidates(
        self,
        client: TestClient,
        store: InstrumentStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The composite token's master-revision leg survives the re-key: an
        instrument load (which bumps NO write generation) still recomputes."""
        calls = _spy(monkeypatch, "build_coverage_payload")
        client.get("/api/v1/coverage")
        store.upsert_many(
            [_instrument("BANKNIFTY", 25)],
            snapshot_date=date(2024, 1, 3),
            run_id="r2",
            csv_source_sha256="1" * 64,
        )
        client.get("/api/v1/coverage")
        assert calls["n"] == 2

    def test_pull_end_closes_the_staleness_window(
        self,
        client: TestClient,
        summary_cache: SummaryCache,
        pull_flag: dict[str, bool],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = _spy(monkeypatch, "build_coverage_payload")
        client.get("/api/v1/coverage")
        pull_flag["on"] = True
        summary_cache.mark_dirty()
        client.get("/api/v1/coverage")
        assert calls["n"] == 1  # floored while the pull runs
        pull_flag["on"] = False
        client.get("/api/v1/coverage")
        assert calls["n"] == 2  # probe False → fresh recompute


class TestUniverseCompletenessFloor:
    def test_mid_pull_storm_is_floored(
        self,
        client: TestClient,
        summary_cache: SummaryCache,
        pull_flag: dict[str, bool],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = _spy(monkeypatch, "per_symbol_completeness")
        client.get("/api/v1/universe/completeness")
        assert calls["n"] == 1
        pull_flag["on"] = True
        with capture_logs() as logs:
            for _ in range(3):
                summary_cache.mark_dirty()
                client.get("/api/v1/universe/completeness")
        assert calls["n"] == 1  # floored (fails-before: 4)
        assert len(_stale_served(logs)) >= 1
        pull_flag["on"] = False
        summary_cache.mark_dirty()
        client.get("/api/v1/universe/completeness")
        assert calls["n"] == 2  # no pull → recompute


class TestMappingIntegrityFloor:
    def test_mid_pull_storm_is_floored(
        self,
        client: TestClient,
        summary_cache: SummaryCache,
        pull_flag: dict[str, bool],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = _spy(monkeypatch, "reconcile_mapping_integrity")
        client.get("/api/v1/universe/mapping-integrity")
        assert calls["n"] == 1
        pull_flag["on"] = True
        with capture_logs() as logs:
            for _ in range(3):
                summary_cache.mark_dirty()
                client.get("/api/v1/universe/mapping-integrity")
        assert calls["n"] == 1  # floored (fails-before: 4)
        assert len(_stale_served(logs)) >= 1
        pull_flag["on"] = False
        summary_cache.mark_dirty()
        client.get("/api/v1/universe/mapping-integrity")
        assert calls["n"] == 2  # no pull → recompute


class TestResultsLeaderboardFloor:
    def test_mid_job_revision_moves_are_floored_then_recompute(
        self,
        client: TestClient,
        store: InstrumentStore,
        pull_flag: dict[str, bool],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R9: a RUNNING in-process job's per-flush `sweep_results_revision`
        moves are floored (bounded, logged) instead of a full O(stores×rows)
        re-scan per poll; the job's end recomputes fresh (fails-before: the
        rev-in-key fold re-scanned on every poll)."""
        calls = _spy(monkeypatch, "load_sweep_scan")
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        assert client.get("/api/v1/sweep/leaderboard").json()["total_rows"] == 1
        assert calls["n"] == 1
        pull_flag["on"] = True  # the sweep-inclusive in-process job signal
        _write_sweeps(lake_root, [_row(2)], tf="15m")  # a mid-job store flush
        with capture_logs() as logs:
            floored = client.get("/api/v1/sweep/leaderboard").json()
        assert floored["total_rows"] == 1  # the floored (stale, bounded) scan
        assert calls["n"] == 1
        assert len(_stale_served(logs)) >= 1  # never a silent stale serve
        pull_flag["on"] = False
        fresh = client.get("/api/v1/sweep/leaderboard").json()
        assert fresh["total_rows"] == 2  # job over → fresh scan
        assert calls["n"] == 2

    def test_outage_and_floor_compose(
        self,
        client: TestClient,
        store: InstrumentStore,
        pull_flag: dict[str, bool],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The commit-1 keystone and the re-key compose: probe None →
        serve-last-known (marker); probe OK + generation moved during a job →
        floored serve (no marker, logged); probe OK + no job → recompute."""
        calls = _spy(monkeypatch, "load_sweep_scan")
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        assert client.get("/api/v1/sweep/leaderboard").json()["total_rows"] == 1
        # Leg 1 — outage: serve-last-known, labelled.
        outage = pytest.MonkeyPatch()
        try:
            outage.setattr(app_module, "sweep_results_revision", lambda _root: None)
            during = client.get("/api/v1/sweep/leaderboard").json()
            assert during["revision_probe"] == "outage — serving last-known"
        finally:
            outage.undo()
        # Leg 2 — probe OK, revision moved, job running: floored, NO marker.
        pull_flag["on"] = True
        _write_sweeps(lake_root, [_row(2)], tf="15m")
        floored = client.get("/api/v1/sweep/leaderboard").json()
        assert "revision_probe" not in floored
        assert floored["total_rows"] == 1
        assert calls["n"] == 1
        # Leg 3 — probe OK, no job: fresh recompute.
        pull_flag["on"] = False
        fresh = client.get("/api/v1/sweep/leaderboard").json()
        assert "revision_probe" not in fresh
        assert fresh["total_rows"] == 2
        assert calls["n"] == 2
