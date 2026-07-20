"""Route integration — ``GET /api/v1/sweep/temporal`` + the /results temporal envelope
(DEC-SWEEP-TEMPORAL-ROBUSTNESS-001).

Real routes via ``build_app`` + ``TestClient`` over REAL writer-produced
artifacts (``ResultsStore`` stores, ``write_tuple_sidecars`` sidecars,
``cascade_wire.write_report`` reports). The day/week buckets are verified
against an INDEPENDENT test-side fold (the #1154 surface-trial discipline);
the hostile-run_id 400 is spy-pinned to make ZERO disk touches.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")
pytest.importorskip("polars", reason="stores + sidecars read through polars")

import brutex.engine.cascade_wire as cw
from brutex.calendars.nse import IST
from brutex.dashboard import app as app_module
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.summary_cache import SummaryCache
from brutex.data.instrument_store import InstrumentStore
from brutex.engine.minute_path import PathStatus, SessionEquityPoint
from brutex.engine.period_stats import encode_judged_years, encode_period_stats
from brutex.engine.results_store import ResultRow, ResultsStore
from brutex.engine.trade_capture import CapturedTrade, ComboCapture
from brutex.engine.trade_store import sidecar_paths_for, write_tuple_sidecars
from brutex.metrics.cascade import CandidateVerdict, CascadeReport, FilterStage
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from brutex.strategies.types import Side
from fastapi.testclient import TestClient

_RUN_TEMPORAL = "aa" * 32
_RUN_BARE = "bb" * 32
_DAY_1 = date(2024, 1, 5)  # Friday — ISO 2024-W01
_DAY_2 = date(2024, 1, 8)  # Monday — ISO 2024-W02


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


def _row(run_id: str, *, temporal: bool) -> ResultRow:
    kwargs: dict[str, str] = {}
    if temporal:
        kwargs = {
            "stats_by_year_json": encode_period_stats({"2021": (3, 300, 1, 300)}),
            "stats_by_quarter_json": encode_period_stats(
                {"2021Q1": (2, 500, 1, 100), "2021Q3": (1, -200, 0, 200)}
            ),
            "stats_by_month_json": encode_period_stats(
                {"2021-01": (2, 500, 1, 100), "2021-07": (1, -200, 0, 200)}
            ),
            "temporal_judged_years_json": encode_judged_years([2021]),
        }
    return ResultRow(
        run_id=run_id,
        instrument="NIFTY",
        tf="5m",
        direction="bull",
        mode="A",
        mask_bits=1,
        net_pnl_paisa=300,
        gross_profit_paisa=500,
        gross_loss_paisa=200,
        max_drawdown_paisa=300,
        expectancy_paisa=100,
        total_trades=3,
        winning_trades=1,
        losing_trades=2,
        sharpe_ratio=1.0,
        sortino_ratio=1.2,
        calmar_ratio=0.9,
        profit_factor=2.5,
        win_rate=0.33,
        max_drawdown_pct=0.05,
        avg_entry_price=Decimal("100.00"),
        avg_exit_price=Decimal("101.00"),
        **kwargs,  # type: ignore[arg-type]
    )


def _write_store(lake_root: Path, rows: list[ResultRow]) -> Path:
    dest = lake_root / "sweeps" / "NIFTY" / "5m" / f"bull_k1_{'a' * 16}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    store = ResultsStore(path=dest)
    store.extend(rows)
    store.flush()
    return dest


def _ts(day: date, hh: int, mm: int) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=IST).astimezone(UTC)


def _trade(day: date, seq: int, *, pnl: int) -> CapturedTrade:
    return CapturedTrade(
        entry_ts=_ts(day, 9, 16 + 2 * seq),
        entry_px=Decimal("100.00"),
        exit_ts=_ts(day, 9, 17 + 2 * seq),
        exit_px=Decimal("100.50") if pnl >= 0 else Decimal("99.70"),
        direction=Side.BUY,
        lots=None,
        exit_reason="take_profit",
        pnl_paisa=pnl,
        costs_paisa=None,
        mae_paisa=10,
        mfe_paisa=60,
        mae_1m_paisa=None,
        mfe_1m_paisa=None,
        path_1m_status=PathStatus.NO_1M_DATA,
        vix_entry=None,
        vix_exit=None,
    )


_TRADES = (
    _trade(_DAY_1, 0, pnl=50),
    _trade(_DAY_1, 1, pnl=-30),
    _trade(_DAY_2, 2, pnl=20),
)


def _write_sidecars(lake_root: Path) -> None:
    store = lake_root / "sweeps" / "NIFTY" / "1minute" / f"bull_k1_{'0' * 16}.parquet"
    paths = sidecar_paths_for(store, lake_root / "sweeps", run_id="grid-route-test")
    capture = ComboCapture(
        run_id=_RUN_TEMPORAL,
        trades=_TRADES,
        equity=(SessionEquityPoint(_DAY_1, 10_000_020, 9_999_970, 3),),
        equity_status=PathStatus.OK,
        vix_missing_trades=3,
        parity_ok=True,
        parity_detail="",
    )
    write_tuple_sidecars(paths, [capture], [], store_content_hash="12" * 32)


def _write_report(lake_root: Path) -> None:
    group = CascadeReport(
        verdicts=(
            CandidateVerdict(
                candidate_id=_RUN_TEMPORAL,
                passed=False,
                rejected_at=FilterStage.WALK_FORWARD,
                detail="wfo fail",
                composite_score=None,
            ),
        ),
        survivor_ids=(),
        n_input=1,
        n_survived=0,
        stage_survivors={},
        pool_pbo=None,
    )
    cw.write_report(
        lake_root / "sweeps",
        cw.CascadeWireReport(sweep_id="a" * 64, groups=(("NIFTY", "5m", group),)),
    )


@pytest.fixture
def lake(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def client(lake: Path) -> Iterator[TestClient]:
    instrument_store = InstrumentStore.open(lake / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    app = build_app(
        DashboardState(), instrument_store=instrument_store, summary_cache=SummaryCache()
    )
    with TestClient(app) as c:
        yield c
    instrument_store.close()


class TestValidation:
    def test_unknown_horizon_is_400(self, client: TestClient) -> None:
        r = client.get("/api/v1/sweep/temporal", params={"run_id": _RUN_TEMPORAL, "horizon": "yr"})
        assert r.status_code == 400
        assert "unknown horizon" in r.json()["detail"]

    def test_hostile_run_id_is_400_with_zero_disk_touches(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The spy pin: a hostile run_id must be refused BEFORE any lake probe."""

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("disk probe reached with a hostile run_id")

        monkeypatch.setattr(app_module, "sweep_results_revision", _boom)
        monkeypatch.setattr(app_module, "sweep_runs_token", _boom)
        monkeypatch.setattr(app_module, "load_sweep_scan", _boom)
        monkeypatch.setattr(app_module, "build_sidecar_index", _boom)
        for hostile in ("../../etc/passwd", "AA" * 32, "a" * 63, "%2e%2e"):
            r = client.get(
                "/api/v1/sweep/temporal", params={"run_id": hostile, "horizon": "quarter"}
            )
            assert r.status_code == 400
            assert "64-character lowercase-hex" in r.json()["detail"]


class TestStoredHorizons:
    def test_quarter_serves_cells_and_half_year(self, client: TestClient, lake: Path) -> None:
        _write_store(lake, [_row(_RUN_TEMPORAL, temporal=True)])
        r = client.get(
            "/api/v1/sweep/temporal", params={"run_id": _RUN_TEMPORAL, "horizon": "quarter"}
        )
        assert r.status_code == 200
        payload = r.json()
        assert payload["status"] == "ok"
        assert payload["source"] == "stored"
        assert payload["cells"] == {"2021Q1": [2, 500, 1, 100], "2021Q3": [1, -200, 0, 200]}
        assert payload["half_year"] == {"2021H1": [2, 500, 1], "2021H2": [1, -200, 0]}

    def test_month_repeat_click_is_cached(self, client: TestClient, lake: Path) -> None:
        """A repeat drill within one revision performs zero fresh scans (the
        #1533 spy discipline at the route level)."""
        _write_store(lake, [_row(_RUN_TEMPORAL, temporal=True)])
        first = client.get(
            "/api/v1/sweep/temporal", params={"run_id": _RUN_TEMPORAL, "horizon": "month"}
        ).json()
        assert first["status"] == "ok"
        calls = {"scan": 0}
        real = app_module.load_sweep_scan

        def _spy(lake_root: Path) -> Any:
            calls["scan"] += 1
            return real(lake_root)

        from unittest import mock

        with mock.patch.object(app_module, "load_sweep_scan", _spy):
            second = client.get(
                "/api/v1/sweep/temporal", params={"run_id": _RUN_TEMPORAL, "horizon": "month"}
            ).json()
        assert second == first
        assert calls["scan"] == 0

    def test_pre_extension_row_reads_absent(self, client: TestClient, lake: Path) -> None:
        _write_store(lake, [_row(_RUN_BARE, temporal=False)])
        r = client.get("/api/v1/sweep/temporal", params={"run_id": _RUN_BARE, "horizon": "quarter"})
        payload = r.json()
        assert payload["status"] == "absent"
        assert "re-sweep to populate" in payload["detail"]

    def test_unknown_run_id_reads_absent(self, client: TestClient, lake: Path) -> None:
        _write_store(lake, [_row(_RUN_BARE, temporal=False)])
        r = client.get("/api/v1/sweep/temporal", params={"run_id": "cc" * 32, "horizon": "month"})
        payload = r.json()
        assert payload["status"] == "absent"
        assert "unknown run_id" in payload["detail"]


class TestSidecarHorizons:
    def test_day_and_week_from_real_sidecars_match_oracle(
        self, client: TestClient, lake: Path
    ) -> None:
        _write_sidecars(lake)
        day = client.get(
            "/api/v1/sweep/temporal", params={"run_id": _RUN_TEMPORAL, "horizon": "day"}
        ).json()
        assert day["status"] == "ok"
        assert day["source"] == "results_depth_sidecar"
        oracle: dict[str, list[int]] = {}
        for trade in _TRADES:
            key = trade.exit_ts.astimezone(IST).date().isoformat()
            cell = oracle.setdefault(key, [0, 0, 0])
            cell[0] += 1
            cell[1] += trade.pnl_paisa
            cell[2] += 1 if trade.pnl_paisa > 0 else 0
        assert day["cells"] == oracle
        week = client.get(
            "/api/v1/sweep/temporal", params={"run_id": _RUN_TEMPORAL, "horizon": "week"}
        ).json()
        assert week["cells"] == {"2024-W01": [2, 20, 1], "2024-W02": [1, 20, 1]}

    def test_missing_artifacts_read_awaiting(self, client: TestClient, lake: Path) -> None:
        _write_store(lake, [_row(_RUN_TEMPORAL, temporal=True)])
        r = client.get("/api/v1/sweep/temporal", params={"run_id": _RUN_TEMPORAL, "horizon": "day"})
        payload = r.json()
        assert payload["status"] == "awaiting"
        assert "BRUTEX_RESULTS_DEPTH" in payload["detail"]


class TestDegradeArms:
    """The route's remaining honest-degrade arms (no lake / no signal / store fault)."""

    def test_no_lake_root_reads_absent(self) -> None:
        class _NoPathStore:
            path = None

            def list_index_equity_instruments(self) -> list[object]:
                return []

        with TestClient(
            build_app(DashboardState(), instrument_store=_NoPathStore())  # type: ignore[arg-type]
        ) as c:
            resp = c.get(
                "/api/v1/sweep/temporal", params={"run_id": _RUN_TEMPORAL, "horizon": "quarter"}
            )
            assert resp.status_code == 200
            payload = resp.json()
            assert payload["status"] == "absent"
            assert "no lake root wired" in payload["detail"]

    def test_revision_outage_computes_fresh_uncached(
        self, client: TestClient, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """rev=None → fresh compute, cache NOTHING: two polls scan twice."""
        _write_store(lake, [_row(_RUN_TEMPORAL, temporal=True)])
        monkeypatch.setattr(app_module, "sweep_results_revision", lambda _root: None)
        calls = {"scan": 0}
        real = app_module.load_sweep_scan

        def _spy(lake_root: Path) -> Any:
            calls["scan"] += 1
            return real(lake_root)

        monkeypatch.setattr(app_module, "load_sweep_scan", _spy)
        for _ in range(2):
            payload = client.get(
                "/api/v1/sweep/temporal", params={"run_id": _RUN_TEMPORAL, "horizon": "quarter"}
            ).json()
            assert payload["status"] == "ok"
        assert calls["scan"] == 2

    def test_sweep_runs_token_outage_computes_fresh_uncached(
        self, client: TestClient, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_sidecars(lake)
        monkeypatch.setattr(app_module, "sweep_runs_token", lambda _root: None)
        payload = client.get(
            "/api/v1/sweep/temporal", params={"run_id": _RUN_TEMPORAL, "horizon": "week"}
        ).json()
        assert payload["status"] == "ok"
        assert payload["cells"] == {"2024-W01": [2, 20, 1], "2024-W02": [1, 20, 1]}

    def test_store_fault_degrades_to_error_payload(
        self, client: TestClient, lake: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A typed store fault → the sanitized C3 error payload, never a 500."""
        from brutex.engine.results_store import ResultsStoreError

        _write_store(lake, [_row(_RUN_TEMPORAL, temporal=True)])

        def _boom(_root: Path) -> Any:
            raise ResultsStoreError("synthetic store fault — path/secret detail")

        monkeypatch.setattr(app_module, "load_sweep_scan", _boom)
        resp = client.get(
            "/api/v1/sweep/temporal", params={"run_id": _RUN_TEMPORAL, "horizon": "month"}
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "error"
        assert "results store unavailable" in payload["detail"]
        assert "secret" not in payload["detail"]  # sanitized — str(exc) never on the wire


class TestLeaderboardTemporalEnvelope:
    def test_rows_and_envelope_carry_the_new_keys(self, client: TestClient, lake: Path) -> None:
        _write_store(lake, [_row(_RUN_TEMPORAL, temporal=True), _row(_RUN_BARE, temporal=False)])
        _write_report(lake)
        board = client.get("/api/v1/sweep/leaderboard").json()
        assert board["temporal_coverage"] == {"rows_with_data": 1, "rows_total": 2}
        assert board["cascade_report"]["present"] is True
        assert board["cascade_report"]["groups"] == 1
        by_id = {r["run_id"]: r for r in board["rows"]}
        assert by_id[_RUN_TEMPORAL]["cascade_verdict"] == "rejected"
        # ``cascade_rejected_at`` is join-owned (club S2 fold): the route loads
        # the landed DEC-RESULTS-CASCADE-VERDICT-JOIN-001 join over the SAME
        # (versioned) report, so the stage token rides ``_cascade_row_keys``.
        assert by_id[_RUN_TEMPORAL]["cascade_rejected_at"] == "walk_forward"
        assert by_id[_RUN_TEMPORAL]["temporal_judged_years"] == ["2021"]
        # Emit-on-present: an unjudged row carries NO cascade marker key.
        assert "cascade_verdict" not in by_id[_RUN_BARE]
        assert by_id[_RUN_BARE]["stats_by_year"] is None

    def test_hostile_stage_token_from_unversioned_report_never_reaches_the_wire(
        self, client: TestClient, lake: Path
    ) -> None:
        """XSS posture (club S2 fold): a hostile ``rejected_at`` token lives in
        the join-owned ``cascade_rejected_at`` now — the landed
        DEC-RESULTS-CASCADE-VERDICT-JOIN-001 join is STRICT (it REJECTS an
        unversioned/malformed report, logging ``cascade_report_load_failed``),
        so a hand-forged version-less report's hostile token NEVER reaches the
        wire; the lenient marker still serves only the fixed-vocabulary
        ``cascade_verdict`` chip (never attacker data). A WELL-FORMED versioned
        report's ``rejected_at`` is XSS-tested as JSON data in
        ``test_sweep_results_view_cascade.py`` on the join emitter.
        """
        _write_store(lake, [_row(_RUN_TEMPORAL, temporal=True)])
        hostile = '<img src=x onerror=alert(1)>"onmouseover="evil'
        report_file = lake / "sweeps" / "cascade" / "cascade-report.json"
        report_file.parent.mkdir(parents=True)
        report_file.write_text(
            json.dumps(
                {
                    "groups": [
                        {
                            "report": {
                                "verdicts": [
                                    {
                                        "candidate_id": _RUN_TEMPORAL,
                                        "passed": False,
                                        "rejected_at": hostile,
                                    }
                                ]
                            }
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        board = client.get("/api/v1/sweep/leaderboard").json()
        row = next(r for r in board["rows"] if r["run_id"] == _RUN_TEMPORAL)
        # The lenient marker still serves the safe fixed-vocab verdict chip …
        assert row["cascade_verdict"] == "rejected"
        # … but the hostile token from the unversioned report is DROPPED (the
        # strict join rejected the malformed report — it never reaches the wire).
        assert "cascade_rejected_at" not in row

    def test_report_write_invalidates_the_cached_leaderboard(
        self, client: TestClient, lake: Path
    ) -> None:
        """The revision extension end-to-end: a cascade report landing AFTER a
        cached poll appears on the very next poll (no restart, no stale chip).
        """
        _write_store(lake, [_row(_RUN_TEMPORAL, temporal=True)])
        before = client.get("/api/v1/sweep/leaderboard").json()
        assert before["cascade_report"]["present"] is False
        # Emit-on-present: pre-report, the row carries NO cascade marker key.
        assert "cascade_verdict" not in before["rows"][0]
        _write_report(lake)
        after = client.get("/api/v1/sweep/leaderboard").json()
        assert after["cascade_report"]["present"] is True
        assert after["rows"][0]["cascade_verdict"] == "rejected"
