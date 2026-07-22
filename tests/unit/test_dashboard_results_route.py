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
    total_trades: int = 10,
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
        total_trades=total_trades,
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


def test_leaderboard_revision_cache(
    client: TestClient, store: InstrumentStore, summary_cache: SummaryCache
) -> None:
    """The leaderboard keys on the sweeps-tree CONTENT revision, not the generation.

    DEC-LEDGER-DELTA-O-PAGE-001 (flips the pre-change generation-keyed pin): a
    store written into the tree — with NO ``mark_dirty`` at all, the CLI
    ``brutex sweep-all`` case the old keying never covered — is seen on the very
    next poll because the write moves ``sweep_results_revision``; a repeat poll
    at an UNCHANGED tree is a cache hit (spy-pinned in
    ``test_dashboard_results_revision_key.py``).
    """
    lake_root = store.path.parent
    _write_sweeps(lake_root, [_row(1)])
    first = client.get("/api/v1/sweep/leaderboard").json()
    assert first["total_rows"] == 1

    # Add a second store WITHOUT any generation bump → the revision token moved,
    # so the next poll re-reads (the cross-process CLI-sweep honesty fix).
    _write_sweeps(lake_root, [_row(2)], tf="15m")
    fresh = client.get("/api/v1/sweep/leaderboard").json()
    assert fresh["total_rows"] == 2

    # A mark_dirty storm at the UNCHANGED tree changes nothing (same payload —
    # the pull's per-window generation bumps no longer thrash the leaderboard).
    summary_cache.mark_dirty()
    again = client.get("/api/v1/sweep/leaderboard").json()
    assert again == fresh


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
        # Keep the account-wide single-flight lock inside the tmp lake (not the real
        # ~/.brutex/lake) — DEC-ACCOUNT-PULL-SINGLEFLIGHT-001.
        lake_root_resolver=lambda: lake_root,
        # Hermeticity (DEC-TEST-AWS-HERMETICITY-001): a real lake root would
        # otherwise invoke the DEFAULT account-key resolver, whose production
        # binding reads the Groww api_key from LIVE AWS SSM. This sweep makes
        # zero vendor calls, so the account lock is skipped explicitly (the
        # established `lambda: None` idiom — test_pull_jobs_singleflight.py).
        account_key_resolver=lambda: None,
    )
    assert job.start(_work, total=1, on_success=summary_cache.mark_dirty) is True

    # 3) The NEXT poll re-reads (cache invalidated by the completed sweep) and SEES it.
    fresh = client.get("/api/v1/sweep/leaderboard").json()
    assert fresh["available"] is True
    assert fresh["total_rows"] == 1


def test_failed_sweep_written_store_still_visible(
    client: TestClient, store: InstrumentStore, summary_cache: SummaryCache
) -> None:
    """A FAILED sweep never fires on_success, but a store it DID write is shown.

    The on_success success-only contract (MAJOR-1) is unchanged — the hook does
    not fire on a crash (asserted via the generation staying put). Under
    DEC-LEDGER-DELTA-O-PAGE-001's revision keying the /results payload reflects
    the ACTUAL tree content regardless: the store the crashing sweep flushed
    before dying moved ``sweep_results_revision``, so the next poll honestly
    shows it (Golden Rule #1 — data that exists is never hidden behind a cache;
    flips the pre-change generation-keyed staleness pin).
    """
    from brutex.dashboard.pull_jobs import BackgroundPullJob, ProgressHook

    # Warm the cache at the empty state.
    assert client.get("/api/v1/sweep/leaderboard").json()["available"] is False
    lake_root = store.path.parent

    def _work(on_progress: ProgressHook) -> None:
        # The store IS written, but the sweep then CRASHES — on_success must NOT fire.
        _write_sweeps(lake_root, [_row(1)])
        raise RuntimeError("sweep boom")

    job = BackgroundPullJob(
        spawn=lambda fn: fn(),
        summary_cache=summary_cache,
        lake_root_resolver=lambda: lake_root,  # DEC-ACCOUNT-PULL-SINGLEFLIGHT-001
        # Hermeticity (DEC-TEST-AWS-HERMETICITY-001): skip the account lock —
        # the default resolver would read the api_key from live AWS SSM.
        account_key_resolver=lambda: None,
    )
    generation_before = summary_cache.generation
    assert job.start(_work, total=1, on_success=summary_cache.mark_dirty) is True

    # The success-only hook contract holds: the failed sweep bumped NO generation…
    assert summary_cache.generation == generation_before
    # …yet the written store is honestly visible (the revision token moved).
    fresh = client.get("/api/v1/sweep/leaderboard").json()
    assert fresh["available"] is True
    assert fresh["total_rows"] == 1


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


# ── DEC-RESULTS-CASCADE-VERDICT-JOIN-001: the cascade join on the wire ──


def _seed_wire_report(lake_root: Path, *, sweep_id: str = "s" * 64) -> Path:
    from brutex.engine.cascade_wire import CascadeWireReport, write_report
    from brutex.metrics.cascade import CandidateVerdict, CascadeReport, FilterStage

    inner = CascadeReport(
        verdicts=(
            CandidateVerdict(
                candidate_id=f"{1:064x}",
                passed=True,
                rejected_at=None,
                detail="survived all Stage-4 filters",
                composite_score=2.0,
                cpcv_pass_rate=0.9,
            ),
            CandidateVerdict(
                candidate_id=f"{2:064x}",
                passed=False,
                rejected_at=FilterStage.PBO,
                detail="pool PBO 0.7500 > 0.5",
                cpcv_pass_rate=0.65,
            ),
        ),
        survivor_ids=(f"{1:064x}",),
        n_input=2,
        n_survived=1,
        stage_survivors={},
    )
    report = CascadeWireReport(sweep_id=sweep_id, groups=(("NIFTY", "5m", inner),))
    return write_report(lake_root / "sweeps", report)


class TestLeaderboardCascadeJoin:
    def test_no_report_payload_carries_no_new_keys(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        payload = client.get("/api/v1/sweep/leaderboard").json()
        assert "cascade_join" not in payload
        assert "cascade_flagged" not in payload
        assert not any(k.startswith("cascade") for k in payload["rows"][0])

    def test_report_annotates_rows_and_meta_on_the_wire(
        self, client: TestClient, store: InstrumentStore, summary_cache: SummaryCache
    ) -> None:
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1, sharpe=2.5), _row(2, sharpe=0.5)])
        _seed_wire_report(lake_root)
        summary_cache.mark_dirty()
        payload = client.get("/api/v1/sweep/leaderboard").json()
        assert payload["cascade_join"]["state"] == "joined"
        assert payload["cascade_join"]["sweep_id"] == "s" * 64
        assert payload["cascade_flagged"] == 1
        rows = {r["run_id"]: r for r in payload["rows"]}
        assert rows[f"{1:064x}"]["cascade_status"] == "pass"
        assert rows[f"{1:064x}"]["cpcv_pass_pct"] == 90.0
        assert rows[f"{2:064x}"]["cascade_status"] == "fail"
        assert rows[f"{2:064x}"]["cascade_status_text"] == "FAIL @ pbo"
        # The FAILED row stays RANKED (rank 2 by sharpe) — flagged, never hidden.
        assert rows[f"{2:064x}"]["rank"] == 2

    def test_malformed_report_never_500s(
        self, client: TestClient, store: InstrumentStore, summary_cache: SummaryCache
    ) -> None:
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        report = lake_root / "sweeps" / "cascade" / "cascade-report.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_bytes(b"{torn half-write")
        summary_cache.mark_dirty()
        resp = client.get("/api/v1/sweep/leaderboard")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["cascade_join"]["state"] == "malformed"
        assert not any(k.startswith("cascade_status") for k in payload["rows"][0])

    def test_report_rewrite_moves_the_cache_key(
        self, client: TestClient, store: InstrumentStore, summary_cache: SummaryCache
    ) -> None:
        import os

        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        path = _seed_wire_report(lake_root)
        first = client.get("/api/v1/sweep/leaderboard").json()
        assert first["cascade_join"]["sweep_id"] == "s" * 64
        # An IN-PLACE rewrite (a new sweep overwrote the one-per-root report)
        # moves NO directory mtime the sweeps digest sees and bumps NO
        # generation: the dedicated casc stat token ALONE must re-key the
        # decorated payload (the sweep_id flips on the wire).
        _seed_wire_report(lake_root, sweep_id="t" * 64)
        os.utime(path, ns=(2_000_000_000, 2_000_000_000))
        second = client.get("/api/v1/sweep/leaderboard").json()
        assert second["cascade_join"]["sweep_id"] == "t" * 64

    def test_composite_join_meta_from_aggs(
        self, client: TestClient, store: InstrumentStore, summary_cache: SummaryCache
    ) -> None:
        import polars as pl

        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        depth = lake_root / "sweep_runs" / ("grid" + "0" * 60) / "results-depth" / "NIFTY" / "5m"
        depth.mkdir(parents=True)
        pl.DataFrame(
            {
                "run_id": [f"{1:064x}"],
                "composite_score": pl.Series([1.75], dtype=pl.Float64),
            }
        ).write_parquet(depth / "bull_k1_aaaaaaaaaaaaaaaa.aggs.parquet")
        summary_cache.mark_dirty()
        payload = client.get("/api/v1/sweep/leaderboard").json()
        assert payload["composite_join"]["state"] == "joined"
        assert payload["composite_join"]["scored_rows"] == 1
        assert payload["rows"][0]["composite_score"] == 1.75
        # The ?preset=composite axis is now honestly ACTIVE.
        preset = client.get("/api/v1/sweep/leaderboard?preset=composite").json()
        assert preset["preset_inert_axes"] == [
            "max_consecutive_losses",
            "alpha_pct",
            "net_after_cost_paisa",
        ]

    def test_aggs_landing_after_a_poll_surfaces_without_a_sweeps_write(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        """Fix round 1 (refuter-confirmed MAJOR killing regression): the depth
        pass writes the aggs sidecars AFTER the sweep's last store flush /
        audit append, into the ``sweep_runs/`` sibling tree that
        ``sweep_results_revision`` never stats — so a poll landing mid-pass
        cached an EMPTY composite index that the aggs writes could never
        invalidate (the pre-fix "can never hide behind a stale index" comment
        was false; the stale index persisted until the NEXT sweep). The
        dedicated ``composite_index_revision`` token must surface the aggs on
        the very next poll with NO sweeps-tree write, NO audit append and NO
        ``mark_dirty`` following them (``cmd_sweep_all``'s real ordering).
        """
        import polars as pl

        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        run_dir = lake_root / "sweep_runs" / ("grid" + "0" * 60)
        run_dir.mkdir(parents=True)
        # Prime every cache MID-depth-pass: the run dir exists, no aggs yet.
        first = client.get("/api/v1/sweep/leaderboard").json()
        assert first["composite_join"]["state"] == "joined"
        assert first["composite_join"]["scored_rows"] == 0
        assert "composite_score" not in first["rows"][0]
        # The depth pass then lands the aggs — nothing else moves.
        depth = run_dir / "results-depth" / "NIFTY" / "5m"
        depth.mkdir(parents=True)
        pl.DataFrame(
            {
                "run_id": [f"{1:064x}"],
                "composite_score": pl.Series([1.75], dtype=pl.Float64),
            }
        ).write_parquet(depth / "bull_k1_aaaaaaaaaaaaaaaa.aggs.parquet")
        second = client.get("/api/v1/sweep/leaderboard").json()
        assert second["composite_join"]["scored_rows"] == 1
        assert second["rows"][0]["composite_score"] == 1.75
        # The activation reaches the preset axis too — still no sweeps write.
        preset = client.get("/api/v1/sweep/leaderboard?preset=composite").json()
        assert preset["preset_inert_axes"] == [
            "max_consecutive_losses",
            "alpha_pct",
            "net_after_cost_paisa",
        ]

    def test_composite_index_reads_once_per_token(
        self, client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix round 1 memo law: repeat polls at an UNCHANGED aggs tree pay ONE
        ``load_composite_index`` (the token-keyed slot), and an aggs write pays
        exactly one more — never a per-poll O(aggs files) re-read.
        """
        import polars as pl
        from brutex.dashboard import app as app_module

        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        depth = lake_root / "sweep_runs" / ("grid" + "0" * 60) / "results-depth" / "NIFTY" / "5m"
        depth.mkdir(parents=True)
        pl.DataFrame(
            {"run_id": [f"{1:064x}"], "composite_score": pl.Series([1.75], dtype=pl.Float64)}
        ).write_parquet(depth / "bull_k1_aaaaaaaaaaaaaaaa.aggs.parquet")
        calls = {"n": 0}
        real = app_module.load_composite_index

        def _spy(root: Path) -> object:
            calls["n"] += 1
            return real(root)

        monkeypatch.setattr(app_module, "load_composite_index", _spy)
        for _ in range(3):
            assert (
                client.get("/api/v1/sweep/leaderboard").json()["composite_join"]["scored_rows"] == 1
            )
        assert calls["n"] == 1
        # A second sidecar lands in a NEW tf dir → the token moves → ONE more read.
        sibling = depth.parent / "15m"
        sibling.mkdir()
        pl.DataFrame(
            {"run_id": [f"{2:064x}"], "composite_score": pl.Series([2.0], dtype=pl.Float64)}
        ).write_parquet(sibling / "bull_k1_bbbbbbbbbbbbbbbb.aggs.parquet")
        assert client.get("/api/v1/sweep/leaderboard").json()["composite_join"]["scored_rows"] == 2
        assert calls["n"] == 2


class TestServeFilterParams:
    """DEC-RESULTS-TRUST-HONESTY-001 · B2a — the serving-side floor params."""

    def test_min_trades_floor_reaches_the_reader(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1, total_trades=10), _row(2, total_trades=60)])
        payload = client.get("/api/v1/sweep/leaderboard?min_trades=30").json()
        assert [r["total_trades"] for r in payload["rows"]] == [60]
        assert payload["total_rows"] == 1

    def test_blank_filters_are_byte_identical_to_absent(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        """Blank params = inactive = the EXACT legacy payload (the pinned arm)."""
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1), _row(2)])
        legacy = client.get("/api/v1/sweep/leaderboard").json()
        blank = client.get(
            "/api/v1/sweep/leaderboard?min_trades=&min_pf=&min_dsr=&max_abs_dd_pct="
        ).json()
        assert blank == legacy

    @pytest.mark.parametrize(
        "query",
        [
            "min_trades=abc",
            "min_trades=-1",
            "min_trades=30.5",
            "min_pf=junk",
            "min_pf=-0.1",
            "min_dsr=nan",
            "max_abs_dd_pct=inf",
        ],
    )
    def test_junk_filter_values_are_a_clean_400(self, client: TestClient, query: str) -> None:
        resp = client.get(f"/api/v1/sweep/leaderboard?{query}")
        assert resp.status_code == 400
        # The refusal names the offending parameter (the preset refusal law).
        assert query.split("=", maxsplit=1)[0] in resp.json()["detail"]

    def test_min_dsr_excludes_undemonstrable_rows(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        """The fixture rows persist NO deflated_sharpe — an active min_dsr
        floor excludes them (fail-closed on the operator's explicit request);
        the unfiltered view still serves every row."""
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        assert client.get("/api/v1/sweep/leaderboard").json()["total_rows"] == 1
        assert client.get("/api/v1/sweep/leaderboard?min_dsr=0").json()["total_rows"] == 0

    def test_hard_gates_recheck_marks_never_drops(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1, total_trades=10), _row(2, total_trades=60)])
        payload = client.get("/api/v1/sweep/leaderboard").json()
        by_trades = {r["total_trades"]: r["hard_gates_recheck"] for r in payload["rows"]}
        assert by_trades == {10: "marked", 60: "ok"}
        assert payload["total_rows"] == 2


class TestLeaderboardTierJoin:
    """DEC-RANKING-SPLUS-TIER-001: the tier keys on the /results wire."""

    def test_tier_keys_from_the_newest_runs_top100(
        self, client: TestClient, store: InstrumentStore, summary_cache: SummaryCache
    ) -> None:
        import json as _json

        import polars as pl

        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        run_dir = lake_root / "sweep_runs" / ("grid" + "0" * 60)
        depth = run_dir / "results-depth" / "NIFTY" / "5m"
        depth.mkdir(parents=True)
        pl.DataFrame(
            {
                "run_id": [f"{1:064x}"],
                "composite_score": pl.Series([1.75], dtype=pl.Float64),
            }
        ).write_parquet(depth / "bull_k1_aaaaaaaaaaaaaaaa.aggs.parquet")
        doc = {
            # Fold re-derivation (train-b 2026-07-21, PR #1578 reconcile — the
            # L1 class): the woven #1959 reader hard-gates schema_version == 2;
            # the member's v1 pin would read NAMED corrupt → an empty tier map.
            "schema_version": 2,
            "run_id": "grid" + "0" * 60,
            "preset": "min_dd_first",
            "source": {"pool": 1},
            "baselines": {},
            "entries": [
                {
                    "rank": 1,
                    "run_id": f"{1:064x}",
                    "mask_hex": "0x05",
                    "conditions": ["PDH_CROSS_UP"],
                    "tf": "5m",
                    "direction": "long",
                    "arm": "SL1.0_TP2.0",
                    "metrics": {"sharpe": 1.0},
                    "regime": {},
                    "vix_band": None,
                    "family_id": "fam-0001",
                    "tier": {"label": "S", "blocked_next": ["win_rate"]},
                }
            ],
        }
        (run_dir / "top100.json").write_text(_json.dumps(doc), encoding="utf-8")
        summary_cache.mark_dirty()
        payload = client.get("/api/v1/sweep/leaderboard").json()
        (row,) = payload["rows"]
        assert row["tier"] == "S"
        assert row["tier_blocked_next"] == ["win_rate"]
        assert row["tier_status_text"] == "S — blocked from S+: win_rate"

    def test_no_artifact_wire_payload_carries_no_tier_keys(
        self, client: TestClient, store: InstrumentStore
    ) -> None:
        lake_root = store.path.parent
        _write_sweeps(lake_root, [_row(1)])
        payload = client.get("/api/v1/sweep/leaderboard").json()
        assert all(not any(k.startswith("tier") for k in row) for row in payload["rows"])
