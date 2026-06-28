"""The ``/results`` page + ``/api/v1/sweep/leaderboard`` route (DEC-DASHBOARD-RESULTS-PAGE-001).

Covers the route glue — the page returns 200 with the nav + section markers, the
JSON endpoint over a synthesized ``<lake>/sweeps`` tree (happy path, filters, top
cap, empty state), the generation cache (a repeat poll is served from cache, a
``mark_dirty`` bump re-reads), and the no-lake-root degrade. A real DuckDB-backed
``InstrumentStore`` + a ``TestClient``; skipped cleanly when the ``[ui]`` extra or
duckdb is absent.
"""
# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")
pytest.importorskip("polars", reason="the leaderboard reads per-tuple Parquet stores")

from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.summary_cache import SummaryCache
from brutex.data.instrument_store import InstrumentStore
from brutex.engine.results_store import ResultRow, ResultsStore
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


def _row(
    seed: int,
    *,
    instrument: str = "NIFTY",
    tf: str = "5m",
    direction: str = "bull",
    sharpe: float = 1.0,
) -> ResultRow:
    return ResultRow(
        run_id=f"{seed:064x}",
        instrument=instrument,
        tf=tf,
        direction=direction,
        mode="A",
        mask_bits=0,
        net_pnl_paisa=100_00,
        gross_profit_paisa=200_00,
        gross_loss_paisa=100_00,
        max_drawdown_paisa=50_00,
        expectancy_paisa=10_00,
        total_trades=10,
        winning_trades=6,
        losing_trades=4,
        sharpe_ratio=sharpe,
        sortino_ratio=1.2,
        calmar_ratio=0.9,
        profit_factor=2.0,
        win_rate=0.6,
        max_drawdown_pct=0.05,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
    )


def _write_sweeps(
    lake_root: Path,
    rows: list[ResultRow],
    *,
    instrument: str = "NIFTY",
    tf: str = "5m",
    direction: str = "bull",
) -> None:
    dest = lake_root / "sweeps" / instrument / tf / f"{direction}_k1_{'a' * 16}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    store = ResultsStore(path=dest)
    store.extend(rows)
    store.flush()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def summary_cache() -> SummaryCache:
    return SummaryCache()


@pytest.fixture
def client(store: InstrumentStore, summary_cache: SummaryCache) -> Iterator[TestClient]:
    app = build_app(DashboardState(), instrument_store=store, summary_cache=summary_cache)
    with TestClient(app) as c:
        yield c


def test_render_results_html_caches_template() -> None:
    """The template loader returns identical bytes on the 2nd call (cached branch)."""
    from brutex.dashboard import app as app_module

    app_module._RESULTS_TEMPLATE_CACHE = None  # reset so the first call takes the load branch
    first = app_module._render_results_html()
    second = app_module._render_results_html()  # the cached branch (no second read)
    assert first == second
    assert "Sweep leaderboard" in first


def test_results_page_renders_nav_and_sections(client: TestClient) -> None:
    """GET /results returns 200 with the canonical nav + the page markers."""
    resp = client.get("/results")
    assert resp.status_code == 200
    body = resp.text
    assert '<nav class="topnav"' in body
    assert 'href="/results" aria-current="page"' in body
    assert 'href="/universe"' in body
    assert "/api/v1/sweep/leaderboard" in body
    assert "Sweep leaderboard" in body


def test_leaderboard_empty_when_no_sweeps(client: TestClient) -> None:
    """No sweep tree → honest empty payload (available False), never a 500."""
    resp = client.get("/api/v1/sweep/leaderboard")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["available"] is False
    assert payload["rows"] == []
    assert payload["store_count"] == 0


def test_leaderboard_happy_path(client: TestClient, store: InstrumentStore) -> None:
    """A synthesized sweep tree ranks descending by Sharpe with the option sets."""
    lake_root = store.path.parent
    _write_sweeps(lake_root, [_row(1, sharpe=0.5), _row(2, sharpe=2.5)])
    payload = client.get("/api/v1/sweep/leaderboard").json()
    assert payload["available"] is True
    assert [r["sharpe_ratio"] for r in payload["rows"]] == [2.5, 0.5]
    assert payload["instruments"] == ["NIFTY"]
    assert payload["rank_by"] == "sharpe_ratio"


def test_leaderboard_filters_and_top(client: TestClient, store: InstrumentStore) -> None:
    """The query filters + top cap reach the reader."""
    lake_root = store.path.parent
    _write_sweeps(lake_root, [_row(1), _row(2), _row(3)])
    _write_sweeps(
        lake_root,
        [_row(4, instrument="BANKNIFTY", tf="15m", direction="bear")],
        instrument="BANKNIFTY",
        tf="15m",
        direction="bear",
    )
    payload = client.get("/api/v1/sweep/leaderboard?instrument=NIFTY&top=2").json()
    assert len(payload["rows"]) == 2
    assert {r["instrument"] for r in payload["rows"]} == {"NIFTY"}


def test_leaderboard_top_param_validated(client: TestClient) -> None:
    """``top`` outside [1, 500] is a 422 (FastAPI ge/le), never a deep crash."""
    assert client.get("/api/v1/sweep/leaderboard?top=0").status_code == 422
    assert client.get("/api/v1/sweep/leaderboard?top=99999").status_code == 422


def test_leaderboard_generation_cache(
    client: TestClient, store: InstrumentStore, summary_cache: SummaryCache
) -> None:
    """A repeat poll is served from cache; a write-generation bump re-reads."""
    lake_root = store.path.parent
    _write_sweeps(lake_root, [_row(1)])
    first = client.get("/api/v1/sweep/leaderboard").json()
    assert first["total_rows"] == 1

    # Add a second store WITHOUT bumping the generation → cache still serves the old result.
    _write_sweeps(lake_root, [_row(2)], tf="15m")
    cached = client.get("/api/v1/sweep/leaderboard").json()
    assert cached["total_rows"] == 1  # stale (cache hit) — proves the cache is active

    # Bump the write generation (the same signal a real sweep/pull bumps) → re-read.
    summary_cache.mark_dirty()
    fresh = client.get("/api/v1/sweep/leaderboard").json()
    assert fresh["total_rows"] == 2


def test_completed_sweep_refreshes_leaderboard_cache(
    client: TestClient, store: InstrumentStore, summary_cache: SummaryCache
) -> None:
    """MAJOR-1: a SUCCESSFULLY-completed dashboard sweep bumps the generation so the
    NEXT ``/results`` poll re-reads the new winners instead of the stale empty board.

    Drives the REAL sweep-completion path: a :class:`BackgroundPullJob` started with
    the SAME ``on_success=summary_cache.mark_dirty`` callback the ``/api/v1/sweep/run``
    route wires. Before the fix the sweep ``_work`` never reported per-bar progress, so
    nothing ever bumped the leaderboard cache and the page kept serving ``available:
    false``.
    """
    from brutex.dashboard.pull_jobs import BackgroundPullJob, ProgressHook

    # 1) A first poll with no sweep tree warms the cache at the empty / unavailable state.
    empty = client.get("/api/v1/sweep/leaderboard").json()
    assert empty["available"] is False

    # 2) A sweep "runs": the engine writes a per-tuple store, then the job COMPLETES
    #    successfully and fires the route's on_success hook (mark_dirty).
    lake_root = store.path.parent

    def _work(on_progress: ProgressHook) -> None:
        _write_sweeps(lake_root, [_row(1)])

    job = BackgroundPullJob(
        spawn=lambda fn: fn(),  # synchronous: the success path runs inline
        summary_cache=summary_cache,
    )
    assert job.start(_work, total=1, on_success=summary_cache.mark_dirty) is True

    # 3) The NEXT poll re-reads (cache invalidated by the completed sweep) and SEES it.
    fresh = client.get("/api/v1/sweep/leaderboard").json()
    assert fresh["available"] is True
    assert fresh["total_rows"] == 1


def test_failed_sweep_does_not_refresh_leaderboard_cache(
    client: TestClient, store: InstrumentStore, summary_cache: SummaryCache
) -> None:
    """A FAILED sweep never bumps the generation, so the stale cache is preserved
    (the success-only contract of the on_success hook — MAJOR-1).
    """
    from brutex.dashboard.pull_jobs import BackgroundPullJob, ProgressHook

    # Warm the cache at the empty state.
    assert client.get("/api/v1/sweep/leaderboard").json()["available"] is False
    lake_root = store.path.parent

    def _work(on_progress: ProgressHook) -> None:
        # The store IS written, but the sweep then CRASHES — on_success must NOT fire.
        _write_sweeps(lake_root, [_row(1)])
        raise RuntimeError("sweep boom")

    job = BackgroundPullJob(spawn=lambda fn: fn(), summary_cache=summary_cache)
    assert job.start(_work, total=1, on_success=summary_cache.mark_dirty) is True

    # The cache is NOT invalidated by the failed sweep → the next poll is still stale.
    stale = client.get("/api/v1/sweep/leaderboard").json()
    assert stale["available"] is False


def test_leaderboard_no_lake_root_degrades(store: InstrumentStore) -> None:
    """A store with no Path lake root returns the honest empty payload, not a 500."""

    class _NoPathStore:
        """A store stand-in whose ``path`` is not a Path (the no-lake-root arm)."""

        path = None

        def list_index_equity_instruments(self) -> list[object]:
            return []

    with TestClient(build_app(DashboardState(), instrument_store=_NoPathStore())) as c:  # type: ignore[arg-type]
        resp = c.get("/api/v1/sweep/leaderboard")
        assert resp.status_code == 200
        assert resp.json()["available"] is False
