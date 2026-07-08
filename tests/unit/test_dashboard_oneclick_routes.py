"""FastAPI route tests for the P4 one-click adapter routes (stages 2-3 of 6).

DEC-INSTITUTIONAL-ONECLICK-001. Mirrors the ``test_dashboard_sweep_run_route.py``
/ ``test_dashboard_results_route.py`` harness: ``pytest.importorskip`` guards,
a real ``InstrumentStore`` over a ``tmp_path`` DuckDB fixture, ``TestClient(
build_app(...))``. Every route is a thin closure over exactly one
``brutex.dashboard.oneclick_artifacts`` reader (already unit-tested standalone
in ``test_dashboard_oneclick_artifacts.py``), so these tests cover the ROUTE
GLUE only: query-param wiring, the honest ``awaiting_artifact`` envelope when
no fixture root is configured, the ``ok`` envelope against the checked-in
fixture tree, and the HTTP 400 path-traversal guard on every route that takes
a caller-supplied ``run_id``.

Covers the 6 routes wired in stage 2
(``/runs``, ``/run-status``, ``/preflight-estimate``, ``/estimator-calibration``,
``/timeline``, ``/top100``) plus ``/trades`` wired in stage 3 (backs the
``/results`` per-combo trade list). ``/verify-me`` (+ the ``/strategy`` and
``/verify-me`` HTML pages) remain a later stage's scope per the FINAL
APPROVED SPEC.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.dashboard import oneclick_artifacts as oa
from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

FIXTURE_ONECLICK_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "oneclick"
FIXTURE_SWEEP_RUNS = FIXTURE_ONECLICK_ROOT / "sweep_runs"

RUN_FULL = "run_2026-07-08T090000Z_nifty_full"
RUN_SMOKE = "run_2026-07-07T210000Z_nifty_smoke_complete"
RUN_CORRUPT_MANIFEST = "run_corrupt_manifest"

_TRAVERSAL_RUN_IDS = ("../../etc/passwd", "..", "a/b", "", "run id with spaces")


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


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let BRUTEX_ONECLICK_FIXTURE_ROOT leak between tests."""
    monkeypatch.delenv(oa.FIXTURE_ROOT_ENV, raising=False)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_nifty()], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def client(store: InstrumentStore) -> Iterator[TestClient]:
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


@pytest.fixture
def fixture_client(store: InstrumentStore, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client with BRUTEX_ONECLICK_FIXTURE_ROOT pointed at the checked-in tree."""
    monkeypatch.setenv(oa.FIXTURE_ROOT_ENV, str(FIXTURE_SWEEP_RUNS))
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


# ── /api/v1/sweep/runs ──────────────────────────────────────────────────────


def test_runs_awaiting_when_no_fixture_root(client: TestClient) -> None:
    resp = client.get("/api/v1/sweep/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "awaiting_artifact"
    assert body["runs"] == []


def test_runs_ok_against_fixture_tree(fixture_client: TestClient) -> None:
    resp = fixture_client.get("/api/v1/sweep/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ok"
    run_ids = {row["run_id"] for row in body["runs"]}
    assert RUN_FULL in run_ids
    assert RUN_SMOKE in run_ids


def test_runs_respects_limit(fixture_client: TestClient) -> None:
    resp = fixture_client.get("/api/v1/sweep/runs", params={"limit": 1})
    assert resp.status_code == 200
    assert len(resp.json()["runs"]) == 1


# ── /api/v1/sweep/run-status ────────────────────────────────────────────────


def test_run_status_awaiting_when_no_runs(client: TestClient) -> None:
    resp = client.get("/api/v1/sweep/run-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] is None
    assert body["chain_map"]["state"] == "awaiting_artifact"
    assert body["exit_status"]["state"] == "awaiting_artifact"
    assert body["manifest"]["state"] == "awaiting_artifact"


def test_run_status_ok_bundles_all_three_reads(fixture_client: TestClient) -> None:
    resp = fixture_client.get("/api/v1/sweep/run-status", params={"run_id": RUN_FULL})
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == RUN_FULL
    assert body["chain_map"]["state"] == "ok"
    assert set(body["chain_map"]["links"]) >= {
        "preflight",
        "sweep",
        "stage3",
        "cascade",
        "artifacts",
    }
    assert body["exit_status"]["state"] == "ok"
    assert body["exit_status"]["run_state"] == "running"
    assert body["manifest"]["state"] == "ok"
    assert "per_door_kills_by_name" in body["manifest"]
    # D-8 ABSENT-door law surfaces through the route unmodified.
    assert body["manifest"]["per_door_kills_by_name"]["dsr"]["observed"] is False
    assert body["manifest"]["per_door_kills_by_name"]["dsr"]["killed"] is None
    assert body["manifest"]["per_door_kills_by_name"]["min_hits"]["observed"] is True


def test_run_status_artifact_error_on_corrupt_manifest(fixture_client: TestClient) -> None:
    resp = fixture_client.get("/api/v1/sweep/run-status", params={"run_id": RUN_CORRUPT_MANIFEST})
    assert resp.status_code == 200
    body = resp.json()
    assert body["manifest"]["state"] == "artifact_error"


@pytest.mark.parametrize("run_id", _TRAVERSAL_RUN_IDS)
def test_run_status_rejects_bad_run_id(client: TestClient, run_id: str) -> None:
    resp = client.get("/api/v1/sweep/run-status", params={"run_id": run_id})
    assert resp.status_code == 400


# ── /api/v1/sweep/preflight-estimate ────────────────────────────────────────


def test_preflight_estimate_awaiting_in_production(client: TestClient) -> None:
    resp = client.get("/api/v1/sweep/preflight-estimate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "awaiting_artifact"


def test_preflight_estimate_default_fixture(fixture_client: TestClient) -> None:
    resp = fixture_client.get("/api/v1/sweep/preflight-estimate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["estimate_basis"] == "provisional"
    assert body["estimator"]["trials"] > 0


def test_preflight_estimate_refusal_fixture_by_symbol_match(fixture_client: TestClient) -> None:
    resp = fixture_client.get("/api/v1/sweep/preflight-estimate", params={"symbols": "BANKNIFTY"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["refusals"][0]["symbol"] == "BANKNIFTY"
    assert body["refusals"][0]["missing_spans"]
    assert body["refusals"][0]["reason"]


def test_preflight_estimate_intervals_param_selects_matching_fixture(
    fixture_client: TestClient,
) -> None:
    """Exercises the ``intervals=`` query-param -> ``selection["intervals"]`` wiring."""
    resp = fixture_client.get("/api/v1/sweep/preflight-estimate", params={"intervals": "15m"})
    assert resp.status_code == 200
    assert resp.json()["estimator"]["trials"] == 111


def test_preflight_estimate_intervals_param_splits_on_comma(fixture_client: TestClient) -> None:
    """A multi-value comma list must still overlap-match a single-value fixture."""
    resp = fixture_client.get("/api/v1/sweep/preflight-estimate", params={"intervals": "5m,15m"})
    assert resp.status_code == 200
    assert resp.json()["estimator"]["trials"] == 111


def test_preflight_estimate_directions_param_selects_matching_fixture(
    fixture_client: TestClient,
) -> None:
    """Exercises the ``directions=`` query-param -> ``selection["directions"]`` wiring."""
    resp = fixture_client.get("/api/v1/sweep/preflight-estimate", params={"directions": "short"})
    assert resp.status_code == 200
    assert resp.json()["estimator"]["trials"] == 222


def test_preflight_estimate_k_param_selects_matching_fixture(fixture_client: TestClient) -> None:
    """Exercises the ``k=`` query-param -> ``selection["k"]`` wiring (int coercion)."""
    resp = fixture_client.get("/api/v1/sweep/preflight-estimate", params={"k": 7})
    assert resp.status_code == 200
    assert resp.json()["estimator"]["trials"] == 333


# ── /api/v1/sweep/estimator-calibration ─────────────────────────────────────


def test_estimator_calibration_awaiting_in_production(client: TestClient) -> None:
    resp = client.get("/api/v1/sweep/estimator-calibration")
    assert resp.status_code == 200
    assert resp.json()["state"] == "awaiting_artifact"


def test_estimator_calibration_ok_against_fixture(fixture_client: TestClient) -> None:
    resp = fixture_client.get("/api/v1/sweep/estimator-calibration")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ok"
    assert body["calibrated"] is True
    assert body["trials_per_second"] == 41200000


# ── /api/v1/sweep/timeline ──────────────────────────────────────────────────


def test_timeline_awaiting_when_no_runs(client: TestClient) -> None:
    resp = client.get("/api/v1/sweep/timeline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "awaiting_artifact"
    assert body["records"] == []


def test_timeline_ok_cursor_read(fixture_client: TestClient) -> None:
    resp = fixture_client.get("/api/v1/sweep/timeline", params={"run_id": RUN_FULL, "limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ok"
    assert body["bounded_scan"] is False
    assert len(body["records"]) <= 5
    assert body["records"][0]["kind"] == "RUN_START"


def test_timeline_kind_filter_milestone(fixture_client: TestClient) -> None:
    resp = fixture_client.get(
        "/api/v1/sweep/timeline", params={"run_id": RUN_FULL, "kind": "MILESTONE"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ok"
    assert all(r["kind"] == "MILESTONE" for r in body["records"])
    assert len(body["records"]) >= 1


def test_timeline_search_sets_bounded_scan_true(fixture_client: TestClient) -> None:
    resp = fixture_client.get(
        "/api/v1/sweep/timeline", params={"run_id": RUN_FULL, "q": "preflight"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ok"
    assert body["bounded_scan"] is True


@pytest.mark.parametrize("run_id", _TRAVERSAL_RUN_IDS)
def test_timeline_rejects_bad_run_id(client: TestClient, run_id: str) -> None:
    resp = client.get("/api/v1/sweep/timeline", params={"run_id": run_id})
    assert resp.status_code == 400


# ── /api/v1/sweep/top100 ────────────────────────────────────────────────────


def test_top100_awaiting_when_no_runs(client: TestClient) -> None:
    resp = client.get("/api/v1/sweep/top100")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "awaiting_artifact"
    assert body["rows"] == []


def test_top100_awaiting_when_run_has_no_top100_yet(fixture_client: TestClient) -> None:
    """RUN_FULL is deliberately in-progress — P3 has not written top100.json."""
    resp = fixture_client.get("/api/v1/sweep/top100", params={"run_id": RUN_FULL})
    assert resp.status_code == 200
    assert resp.json()["state"] == "awaiting_artifact"


def test_top100_ok_decorated_rows_against_completed_run(fixture_client: TestClient) -> None:
    resp = fixture_client.get("/api/v1/sweep/top100", params={"run_id": RUN_SMOKE})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ok"
    assert len(body["rows"]) > 0
    row = body["rows"][0]
    for key in (
        "rank",
        "mask_hex",
        "instrument",
        "mode",
        "combo_id",
        "sweep_run_id",
        "join_key_inferred",
        "simple_sentence",
        "pro_sentence",
    ):
        assert key in row


@pytest.mark.parametrize("run_id", _TRAVERSAL_RUN_IDS)
def test_top100_rejects_bad_run_id(client: TestClient, run_id: str) -> None:
    resp = client.get("/api/v1/sweep/top100", params={"run_id": run_id})
    assert resp.status_code == 400


# ── /api/v1/sweep/trades (stage 3) ──────────────────────────────────────────


def test_trades_awaiting_when_no_fixture_root(client: TestClient) -> None:
    resp = client.get("/api/v1/sweep/trades", params={"rank": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "awaiting_artifact"
    assert body["rows"] == []
    assert body["total"] == 0


def test_trades_rank_is_required(client: TestClient) -> None:
    resp = client.get("/api/v1/sweep/trades")
    assert resp.status_code == 422


def test_trades_ok_against_fixture_tree(fixture_client: TestClient) -> None:
    resp = fixture_client.get("/api/v1/sweep/trades", params={"run_id": RUN_SMOKE, "rank": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ok"
    assert body["run_id"] == RUN_SMOKE
    assert body["rank"] == 1
    assert body["total"] == 12
    assert len(body["rows"]) == 12
    row = body["rows"][0]
    for key in (
        "entry_ts",
        "entry_px",
        "exit_ts",
        "exit_px",
        "direction",
        "lots",
        "exit_reason",
        "pnl_paisa",
        "costs_paisa",
        "mae",
        "mfe",
        "vix_entry",
        "vix_exit",
    ):
        assert key in row
    # gap-register finding #18: NULL-by-design until P5, never fabricated.
    assert row["lots"] is None
    assert row["costs_paisa"] is None


def test_trades_pagination(fixture_client: TestClient) -> None:
    resp = fixture_client.get(
        "/api/v1/sweep/trades",
        params={"run_id": RUN_SMOKE, "rank": 1, "offset": 5, "limit": 3},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ok"
    assert body["offset"] == 5
    assert body["limit"] == 3
    assert len(body["rows"]) == 3
    assert body["total"] == 12


def test_trades_combo_explanation_populated_when_mask_and_direction_and_tf_given(
    fixture_client: TestClient,
) -> None:
    resp = fixture_client.get(
        "/api/v1/sweep/trades",
        params={
            "run_id": RUN_SMOKE,
            "rank": 1,
            "mask_hex": "10a081400000000",
            "direction": "short",
            "tf": "3m",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["combo_explanation"] is not None
    assert body["combo_explanation"]["simple_sentence"]
    assert body["combo_explanation"]["pro_sentence"]
    assert body["combo_explanation"]["direction_label"] == "Sell"


def test_trades_combo_explanation_absent_without_all_three_params(
    fixture_client: TestClient,
) -> None:
    resp = fixture_client.get(
        "/api/v1/sweep/trades",
        params={"run_id": RUN_SMOKE, "rank": 1, "mask_hex": "10a081400000000"},
    )
    assert resp.status_code == 200
    assert resp.json()["combo_explanation"] is None


def test_trades_awaiting_for_rank_not_covered_by_sparse_fixture(
    fixture_client: TestClient,
) -> None:
    """RUN_SMOKE's trade fixtures are deliberately sparse (only ranks 1, 2, 37)."""
    resp = fixture_client.get("/api/v1/sweep/trades", params={"run_id": RUN_SMOKE, "rank": 99})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "awaiting_artifact"
    assert body["rows"] == []


def test_trades_limit_out_of_range_is_422(client: TestClient) -> None:
    assert client.get("/api/v1/sweep/trades", params={"rank": 1, "limit": 0}).status_code == 422
    assert client.get("/api/v1/sweep/trades", params={"rank": 1, "limit": 501}).status_code == 422


@pytest.mark.parametrize("run_id", _TRAVERSAL_RUN_IDS)
def test_trades_rejects_bad_run_id(client: TestClient, run_id: str) -> None:
    resp = client.get("/api/v1/sweep/trades", params={"run_id": run_id, "rank": 1})
    assert resp.status_code == 400


# ── no-lake-wired honesty (mirrors /api/v1/sweep/leaderboard's no-lake path) ──


def test_routes_degrade_honestly_with_no_lake_wired() -> None:
    """A store with no on-disk ``path`` never 500s — every route degrades to
    its honest ``awaiting_artifact``/empty envelope (never a crash), mirroring
    ``GET /api/v1/sweep/leaderboard``'s no-lake-root behaviour."""
    from types import SimpleNamespace

    fake_store = SimpleNamespace(path=None)
    with TestClient(build_app(DashboardState(), instrument_store=fake_store)) as c:
        for path, extra_params in (
            ("/api/v1/sweep/runs", {}),
            ("/api/v1/sweep/run-status", {}),
            ("/api/v1/sweep/preflight-estimate", {}),
            ("/api/v1/sweep/estimator-calibration", {}),
            ("/api/v1/sweep/timeline", {}),
            ("/api/v1/sweep/top100", {}),
            ("/api/v1/sweep/trades", {"rank": 1}),
        ):
            resp = c.get(path, params=extra_params)
            assert resp.status_code == 200, f"{path} must never 500 with no lake wired"
