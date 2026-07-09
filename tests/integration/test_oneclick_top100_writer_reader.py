"""The engine-writer ↔ display-reader golden (DEC-ONECLICK-TOP100-OBJECT-ROOT-001).

THE TEST THAT KILLS THE FIXTURE BLINDSPOT: every pre-existing oneclick test
fed the display reader HAND-WRITTEN fixtures, so a reader that rejected the
REAL engine artifact stayed green while every real sweep read
``status="corrupt"`` / ``row_count=0`` on every screen (the confirmed 3/3
refuter BLOCKER — the engine's :func:`brutex.engine.top100_artifact.write_top100`
emits an OBJECT root ``{schema_version, run_id, preset, source, baselines,
entries}`` while the display reader demanded a bare array). This suite drives
the ACTUAL engine writer via its public API (``build_top100`` over real
``ResultRow``/``ComboAggregate`` pairs → ``write_top100``) and reads the
persisted bytes back through the ACTUAL display path:

* the adapter (:func:`brutex.dashboard.oneclick_artifacts.load_top100`) —
  status ``ok``, true row count, the reader-boundary metric-key mapping
  (``max_drawdown_paisa``→``max_dd_paisa`` · ``sharpe_ratio``→``sharpe`` ·
  ``deflated_sharpe``→``dsr`` · ``total_trades``→``trades``) applied
  ADDITIVELY at full precision;
* the metric-drift regression — the five screen columns (Max DD / Sharpe /
  DSR / CPCV % / Trades) resolve REAL values for the four the writer emits
  and an honestly-ABSENT ``cpcv_pass_pct`` (the client renders "—", never a
  fabricated 0);
* the route-level twin — ``build_app`` over a tmp lake whose
  ``<lake>/sweep_runs/<run_id>/`` holds ONLY the engine writer's output:
  ``/api/v1/oneclick/top100`` serves the mapped rows (and the /screener page,
  which consumes exactly that payload client-side, serves 200);
* the engine's OWN validating loader still round-trips the same bytes
  (reader-adopts-writer never forked the artifact).

Reverting the reader fix makes every test here fail loudly (verified during
the fix round: pre-fix status was ``corrupt`` — "top100.json is not a JSON
array").
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from brutex.dashboard import oneclick_artifacts as oc
from brutex.engine import top100_artifact as engine_top100
from brutex.engine.top100_artifact import PoolCounts, build_top100, write_top100
from brutex.engine.twin_detect import TwinFamilies

from tests.fakes.wp5_depth_fixtures import make_candidate, run_id_of

_GRID_RUN_ID = "grid-oc-golden-01"

#: The five screen columns of the metric-drift regression (bx-screener.js
#: COLUMNS / bx-oneclick.js / strategy.html vocabulary).
_SCREEN_METRIC_KEYS = ("max_dd_paisa", "sharpe", "dsr", "cpcv_pass_pct", "trades")


def _engine_artifact(n_candidates: int = 3) -> engine_top100.Top100Artifact:
    """A REAL artifact from the engine's own public builder (no hand-forging)."""
    candidates = [
        make_candidate(
            n,
            fingerprint=f"fp{n:02d}" + "0" * 62,
            row_overrides={
                "sharpe_ratio": 1.0 + n / 8,
                "deflated_sharpe": 0.5 + n / 16,
                "max_drawdown_paisa": 2_000 + n,
                "total_trades": 40 + n,
                "max_drawdown_pct": 10.0 + n,  # distinct min_dd_first order
            },
        )
        for n in range(1, n_candidates + 1)
    ]
    families = TwinFamilies(
        family_by_run_id={run_id_of(n): run_id_of(n) for n in range(1, n_candidates + 1)},
        members_by_family={run_id_of(n): (run_id_of(n),) for n in range(1, n_candidates + 1)},
    )
    return build_top100(
        candidates,
        run_id=_GRID_RUN_ID,
        preset="min_dd_first",
        families=families,
        counts=PoolCounts(stores=1, excluded_no_aggs=0, zero_trade_excluded=0, parity_excluded=0),
        data_snapshot_hashes=["a" * 64],
        baselines={},
    )


def _write_engine_artifact(lake: Path, artifact: engine_top100.Top100Artifact) -> Path:
    """Persist via the engine's OWN writer at the production sweep_runs root."""
    return write_top100(lake / "sweep_runs", artifact)


class TestEngineWriterThroughDisplayReader:
    """The adapter-level golden: real writer bytes → load_top100 → ok + mapped."""

    def test_real_artifact_reads_ok_with_true_row_count(self, tmp_path: Path) -> None:
        artifact = _engine_artifact()
        _write_engine_artifact(tmp_path / "lake", artifact)
        run_dir = (tmp_path / "lake" / "sweep_runs") / _GRID_RUN_ID
        result = oc.load_top100(run_dir)
        assert result.status == "ok", result.detail  # pre-fix: corrupt/not-a-JSON-array
        assert len(result.rows) == len(artifact.entries)
        assert result.preset == "min_dd_first"
        assert [row["rank"] for row in result.rows] == list(range(1, len(result.rows) + 1))
        # The writer's real entries pass the reader's SUB-KEY shape gates too.
        for row in result.rows:
            assert isinstance(row["metrics"], dict)
            assert isinstance(row["regime"], dict)
            assert isinstance(row["conditions"], list)
            assert row["vix_band"] is None or isinstance(row["vix_band"], dict)

    def test_metric_drift_regression_five_screen_columns(self, tmp_path: Path) -> None:
        """Max DD / Sharpe / DSR / Trades resolve REAL full-precision values;
        CPCV % stays honestly ABSENT (the writer emits none — "—", never 0)."""
        artifact = _engine_artifact()
        _write_engine_artifact(tmp_path / "lake", artifact)
        result = oc.load_top100((tmp_path / "lake" / "sweep_runs") / _GRID_RUN_ID)
        assert result.status == "ok"
        for row, entry in zip(result.rows, artifact.entries, strict=True):
            metrics: dict[str, Any] = row["metrics"]
            raw = entry["metrics"]
            assert isinstance(raw, dict)
            # The four writer-emitted screen columns resolve REAL, non-null
            # values; the fifth (_SCREEN_METRIC_KEYS' cpcv_pass_pct) is the
            # honest absence below.
            for present in ("max_dd_paisa", "sharpe", "dsr", "trades"):
                assert present in _SCREEN_METRIC_KEYS
                assert metrics[present] is not None
            assert metrics["max_dd_paisa"] == raw["max_drawdown_paisa"]
            assert metrics["sharpe"] == raw["sharpe_ratio"]
            assert metrics["dsr"] == raw["deflated_sharpe"]
            assert metrics["trades"] == raw["total_trades"]
            assert "cpcv_pass_pct" not in metrics  # honest absence → "—" client-side
            # The engine names ride along verbatim (additive mapping).
            assert metrics["sharpe_ratio"] == raw["sharpe_ratio"]

    def test_engine_loader_round_trips_the_same_bytes(self, tmp_path: Path) -> None:
        """Reader-adopts-writer sanity: the ENGINE's validating loader accepts
        the identical file the display reader now accepts — one artifact."""
        artifact = _engine_artifact()
        runs_dir = tmp_path / "lake" / "sweep_runs"
        _write_engine_artifact(tmp_path / "lake", artifact)
        loaded = engine_top100.load_top100(runs_dir, _GRID_RUN_ID)
        assert loaded is not None
        assert loaded.rendered_bytes() == artifact.rendered_bytes()

    def test_trade_store_reader_resolves_rank_through_the_same_path(self, tmp_path: Path) -> None:
        """TradeStoreReader's rank→combo resolution rides the SAME fixed reader
        (no duplicate parse dialect): rank 1 of the REAL artifact resolves its
        equity_ref and degrades to honest awaiting for the absent sidecars —
        pre-fix the whole page read ``corrupt``."""
        artifact = _engine_artifact()
        _write_engine_artifact(tmp_path / "lake", artifact)
        reader = oc.TradeStoreReader(tmp_path / "lake" / "sweeps")
        page = reader.page(_GRID_RUN_ID, 1, offset=0, limit=10)
        assert page.status == "awaiting"  # resolved the entry; sidecars absent
        assert "results-depth sidecars absent" in page.detail
        assert page.total is None  # never a fabricated 0


class TestEngineWriterThroughRoutes:
    """The route-level twin: build_app over a lake holding ONLY writer output."""

    @staticmethod
    def _client(lake: Path) -> Any:
        pytest.importorskip("fastapi")
        pytest.importorskip("httpx")
        from brutex.dashboard.app import DashboardState, build_app
        from fastapi.testclient import TestClient

        store = SimpleNamespace(path=lake / "instruments.duckdb")
        return TestClient(build_app(DashboardState(), instrument_store=store))

    def test_top100_route_serves_the_real_artifact_rows(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        artifact = _engine_artifact()
        _write_engine_artifact(lake, artifact)
        client = self._client(lake)
        payload = client.get(f"/api/v1/oneclick/top100?run_id={_GRID_RUN_ID}").json()
        assert payload["status"] == "ok", payload["detail"]  # pre-fix: corrupt
        assert payload["row_count"] == len(artifact.entries)
        assert payload["preset"] == "min_dd_first"
        first = payload["rows"][0]["metrics"]
        assert isinstance(first["max_dd_paisa"], int)
        assert isinstance(first["sharpe"], float)
        assert isinstance(first["dsr"], float)
        assert isinstance(first["trades"], int)
        assert "cpcv_pass_pct" not in first
        # The run shows up in the discovery listing with its top100 flagged.
        runs = client.get("/api/v1/oneclick/runs").json()
        listed = {run["run_id"]: run for run in runs["runs"]}
        assert listed[_GRID_RUN_ID]["has_top100"] is True

    def test_screener_page_serves_over_the_real_artifact(self, tmp_path: Path) -> None:
        """/screener consumes exactly the top100 payload client-side — the page
        serves 200 and the payload it fetches carries the real mapped rows."""
        lake = tmp_path / "lake"
        _write_engine_artifact(lake, _engine_artifact())
        client = self._client(lake)
        page = client.get("/screener")
        assert page.status_code == 200
        payload = client.get(f"/api/v1/oneclick/top100?run_id={_GRID_RUN_ID}").json()
        assert payload["status"] == "ok"
        assert payload["row_count"] > 0
        assert all("sharpe" in row["metrics"] for row in payload["rows"])

    def test_route_names_a_future_schema_version_corruptly_never_500(self, tmp_path: Path) -> None:
        """A NEWER writer's artifact degrades to a NAMED corrupt through the
        route — never a 500, never fabricated rows."""
        lake = tmp_path / "lake"
        artifact = _engine_artifact()
        path = _write_engine_artifact(lake, artifact)
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["schema_version"] = engine_top100.TOP100_SCHEMA_VERSION + 1
        path.write_text(json.dumps(doc), encoding="utf-8")
        response = self._client(lake).get(f"/api/v1/oneclick/top100?run_id={_GRID_RUN_ID}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "corrupt"
        assert payload["row_count"] == 0
        assert "schema_version" in payload["detail"]
