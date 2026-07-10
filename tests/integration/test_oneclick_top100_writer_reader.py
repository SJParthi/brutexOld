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


def _single_candidate_artifact(candidate: Any) -> engine_top100.Top100Artifact:
    """A REAL one-entry artifact around *candidate* (the engine's own builder)."""
    families = TwinFamilies(
        family_by_run_id={run_id_of(1): run_id_of(1)},
        members_by_family={run_id_of(1): (run_id_of(1),)},
    )
    return build_top100(
        [candidate],
        run_id=_GRID_RUN_ID,
        preset="min_dd_first",
        families=families,
        counts=PoolCounts(stores=1, excluded_no_aggs=0, zero_trade_excluded=0, parity_excluded=0),
        data_snapshot_hashes=["a" * 64],
        baselines={},
    )


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

    def test_instrument_flows_writer_to_reader_rows_untouched(self, tmp_path: Path) -> None:
        """DEC-ONECLICK-INSTRUMENT-DISPLAY-001: the writer's additive per-entry
        ``instrument`` (``_entry_payload`` — the winner's OWN market, from
        ``ResultRow.instrument``) passes through ``load_top100`` UNTOUCHED to
        every consumer row — the operator rank-1 card / combos Inst column /
        /strategy identity strip / /screener column all read this ONE key."""
        artifact = _engine_artifact()
        _write_engine_artifact(tmp_path / "lake", artifact)
        result = oc.load_top100((tmp_path / "lake" / "sweep_runs") / _GRID_RUN_ID)
        assert result.status == "ok"
        for row, entry in zip(result.rows, artifact.entries, strict=True):
            assert entry["instrument"] == "NIFTY"  # writer truth (ResultRow.instrument)
            assert row["instrument"] == entry["instrument"]  # verbatim passthrough

    def test_arm_dict_bridges_to_the_display_string_never_object_object(
        self, tmp_path: Path
    ) -> None:
        """Hostile-round F1: the writer emits ``arm`` as a DICT
        (``{"sl_paisa": 80, "tp_paisa": 50}``) while every display site
        ``String()``s the field (bx-oneclick.js r1_arm / strategy.html stArm /
        screener.html str-cell) — unbridged, EVERY real sweep card rendered
        ``"[object Object]"``. The reader bridge must hand the pages a STRING
        (house paisa→₹ 2dp per leg) and keep the raw dict additively."""
        artifact = _engine_artifact()
        _write_engine_artifact(tmp_path / "lake", artifact)
        result = oc.load_top100((tmp_path / "lake" / "sweep_runs") / _GRID_RUN_ID)
        assert result.status == "ok"
        for row, entry in zip(result.rows, artifact.entries, strict=True):
            assert entry["arm"] == {"sl_paisa": 80, "tp_paisa": 50}  # writer truth: a dict
            assert row["arm"] == "SL ₹0.80 · TP ₹0.50"  # the bridged display string
            assert isinstance(row["arm"], str)  # String(row.arm) is now honest
            assert row["arm_raw"] == entry["arm"]  # the raw dict rides additively

    def test_none_arm_legs_bridge_to_the_honest_dash(self, tmp_path: Path) -> None:
        """A candidate without a tuple arm (None legs) reads "SL — · TP —"."""
        artifact = _single_candidate_artifact(
            make_candidate(1, fingerprint="fp01" + "0" * 62, sl_paisa=None, tp_paisa=None)
        )
        _write_engine_artifact(tmp_path / "lake", artifact)
        result = oc.load_top100((tmp_path / "lake" / "sweep_runs") / _GRID_RUN_ID)
        assert result.status == "ok"
        assert result.rows[0]["arm"] == "SL — · TP —"  # never a fabricated amount

    def test_unstamped_vix_band_is_the_writer_object_with_null_fields(self, tmp_path: Path) -> None:
        """Hostile-round F4: the REAL writer's UNSTAMPED vix_band is an OBJECT
        whose value fields are null (``_vix_band_payload`` — never the JSON
        ``null`` root the fixture's display-robustness twin uses); the reader
        keeps it as-read through the shape gate (object-or-null law)."""
        unstamped = {
            "vix_band_lo": None,
            "vix_band_hi": None,
            "vix_win_min": None,
            "vix_win_avg": None,
            "vix_win_max": None,
            "vix_loss_min": None,
            "vix_loss_avg": None,
            "vix_loss_max": None,
            "vix_stamped_trades": 0,
            "vix_missing_trades": 40,
        }
        artifact = _single_candidate_artifact(
            make_candidate(1, fingerprint="fp01" + "0" * 62, agg_overrides=dict(unstamped))
        )
        _write_engine_artifact(tmp_path / "lake", artifact)
        result = oc.load_top100((tmp_path / "lake" / "sweep_runs") / _GRID_RUN_ID)
        assert result.status == "ok"
        band = result.rows[0]["vix_band"]
        assert band == {
            "lo": None,
            "hi": None,
            "stamped": 0,
            "missing": 40,
            "win": None,
            "loss": None,
        }

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

    def test_strategy_story_card_arm_is_the_bridged_string_on_the_wire(
        self, tmp_path: Path
    ) -> None:
        """[W3-R3] second-surface verdict: /strategy's story card obtains its
        row from THIS route's payload (``strategy.html`` ``loadRow`` fetches
        ``/api/v1/oneclick/top100`` and sets ``stArm`` from ``row.arm``) — the
        f1d271d4 reader-boundary bridge therefore covers /strategy too: every
        wire row's ``arm`` is the display STRING, so ``String(row.arm)`` can
        never render "[object Object]" on ANY consumer. Route-level proof
        (the earlier arm test asserted the reader only) + the template pin
        that no OTHER fetch feeds the arm."""
        lake = tmp_path / "lake"
        artifact = _engine_artifact()
        # Writer truth: the persisted entries carry arm as a DICT.
        path = _write_engine_artifact(lake, artifact)
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert all(isinstance(entry["arm"], dict) for entry in doc["entries"])
        client = self._client(lake)
        payload = client.get(f"/api/v1/oneclick/top100?run_id={_GRID_RUN_ID}").json()
        assert payload["status"] == "ok"
        for row in payload["rows"]:
            assert isinstance(row["arm"], str)  # bridged for EVERY consumer
            assert row["arm"] == "SL ₹0.80 · TP ₹0.50"
            assert row["arm_raw"] == {"sl_paisa": 80, "tp_paisa": 50}
        # /strategy consumes exactly this payload: stArm is fed from row.arm
        # of the top100 fetch — no second, unbridged arm source exists. The
        # wiring lives in the ONE shared strategy panel module
        # (DEC-OPERATOR-PAGE-CONSOLIDATE-001 — moved verbatim).
        panel = (
            Path(__file__).resolve().parents[2]
            / "brutex"
            / "dashboard"
            / "static"
            / "bx-strategy-panel.js"
        ).read_text(encoding="utf-8")
        assert "byId('stArm').textContent = String(row.arm);" in panel
        # The row source is the memo'd getTop100 promise — which fetches the
        # ONE /api/v1/oneclick/top100 route (pinned in bx-oneclick.js).
        assert "getTop100(state.runId)" in panel
        oneclick_js = (
            Path(__file__).resolve().parents[2]
            / "brutex"
            / "dashboard"
            / "static"
            / "bx-oneclick.js"
        ).read_text(encoding="utf-8")
        assert '"/api/v1/oneclick/top100"' in oneclick_js
        assert panel.count("arm") >= 1
        assert client.get("/strategy").status_code == 200

    def test_instrument_is_on_the_wire_for_every_display_consumer(self, tmp_path: Path) -> None:
        """DEC-ONECLICK-INSTRUMENT-DISPLAY-001 route twin: every wire row of
        ``/api/v1/oneclick/top100`` carries the writer's ``instrument``, and
        the three consumer surfaces are WIRED to read it (template/JS pins —
        the rank-1 card + combos Inst cell via ``bxInstrumentText``, the
        /strategy identity strip via ``BXOneclick.instrumentText``, the
        /screener column via ``BXScreener.COLUMNS``)."""
        lake = tmp_path / "lake"
        _write_engine_artifact(lake, _engine_artifact())
        client = self._client(lake)
        payload = client.get(f"/api/v1/oneclick/top100?run_id={_GRID_RUN_ID}").json()
        assert payload["status"] == "ok"
        assert all(row["instrument"] == "NIFTY" for row in payload["rows"])
        root = Path(__file__).resolve().parents[2] / "brutex" / "dashboard"
        strategy = (root / "templates" / "strategy.html").read_text(encoding="utf-8")
        assert 'id="stInstr"' in strategy
        # The fill line lives VERBATIM in the shared strategy panel module
        # (DEC-OPERATOR-PAGE-CONSOLIDATE-001 — moved out of the template).
        strategy_panel = (root / "static" / "bx-strategy-panel.js").read_text(encoding="utf-8")
        assert (
            "byId('stInstr').textContent = window.BXOneclick.instrumentText(row.instrument);"
            in strategy_panel
        )
        oneclick_js = (root / "static" / "bx-oneclick.js").read_text(encoding="utf-8")
        assert 'setOc("r1_instrument", bxInstrumentText(row.instrument));' in oneclick_js
        assert "td(bxInstrumentText(row.instrument)" in oneclick_js
        screener_js = (root / "static" / "bx-screener.js").read_text(encoding="utf-8")
        assert '{ key: "instrument", label: "Inst", type: "str"' in screener_js

    def test_equity_route_serves_the_real_sidecar_series(self, tmp_path: Path) -> None:
        """DEC-ONECLICK-WINNER-DETAIL-TRUTH-001 item 4C (the #48 overlap): the
        NEW ``/api/v1/oneclick/equity`` route pages the REAL results-depth
        equity sidecar — engine writers end-to-end (``build_top100`` +
        ``write_tuple_sidecars``) → the adapter's rank → ``equity_ref`` chain
        → the wire; before the sidecars land the same coordinates read the
        honest awaiting (never a fabricated curve)."""
        from datetime import date

        from brutex.dashboard import oneclick_artifacts as oc_mod
        from brutex.engine.minute_path import PathStatus, SessionEquityPoint
        from brutex.engine.trade_capture import ComboCapture
        from brutex.engine.trade_store import write_tuple_sidecars

        from tests.fakes.wp5_depth_fixtures import run_id_of as rid_of

        lake = tmp_path / "lake"
        artifact = _engine_artifact()
        _write_engine_artifact(lake, artifact)
        client = self._client(lake)
        # Pre-sidecars: honest awaiting, total null.
        before = client.get(f"/api/v1/oneclick/equity?run_id={_GRID_RUN_ID}&rank=1").json()
        assert before["status"] == "awaiting"
        assert before["total"] is None
        # Now land the REAL sidecars for the rank-1 entry's equity_ref.
        run_dir = (lake / "sweep_runs") / _GRID_RUN_ID
        rank1_ref = artifact.entries[0]["equity_ref"]
        assert isinstance(rank1_ref, dict)
        paths = oc_mod.sidecar_paths_from_ref(run_dir, str(rank1_ref["tuple"]))
        assert paths is not None
        combo = str(artifact.entries[0]["run_id"])
        assert combo == rid_of(1)  # min_dd_first: candidate 1 ranks first
        capture = ComboCapture(
            run_id=combo,
            trades=(),
            equity=(
                SessionEquityPoint(date(2026, 7, 1), 1_000, 900, 1),
                SessionEquityPoint(date(2026, 7, 2), 1_500, None, 1),
            ),
            equity_status=PathStatus.OK,
            vix_missing_trades=0,
            parity_ok=True,
            parity_detail="",
        )
        write_tuple_sidecars(paths, [capture], [], store_content_hash="0" * 64)
        payload = client.get(f"/api/v1/oneclick/equity?run_id={_GRID_RUN_ID}&rank=1").json()
        assert payload["status"] == "ok", payload["detail"]
        assert payload["source"] == "results_store"
        assert payload["total"] == 2
        assert [row["equity_close_paisa"] for row in payload["rows"]] == [1_000, 1_500]
        assert payload["rows"][1]["equity_intraday_low_paisa"] is None  # honest None low
        # The strategy panel module consumes exactly this route + the shared
        # series builder (minute-path label; per-trade cumsum stays the
        # fallback) — the page wiring moved verbatim into the module
        # (DEC-OPERATOR-PAGE-CONSOLIDATE-001).
        panel = (
            Path(__file__).resolve().parents[2]
            / "brutex"
            / "dashboard"
            / "static"
            / "bx-strategy-panel.js"
        ).read_text(encoding="utf-8")
        assert "/api/v1/oneclick/equity" in panel
        assert "window.BXChart.minutePathSeries(state.equity)" in panel
        assert "per-trade close equity (minute-path unavailable for this run)" in panel

    def test_strategy_link_contract_run_id_rank_params(self, tmp_path: Path) -> None:
        """Item 5 (BLOCKER-3, our half): the STABLE deep-link contract
        ``/strategy?run_id=<id>&rank=<n>`` — the route serves 200 with params,
        the wire payload carries the addressed rank, and the template resolves
        rank from the query (exact-match loop; honest rank-miss message;
        latest-run fallback when no run_id is given). The display-truth
        session builds the /results-side link against THIS contract."""
        lake = tmp_path / "lake"
        artifact = _engine_artifact()
        _write_engine_artifact(lake, artifact)
        client = self._client(lake)
        page = client.get(f"/strategy?run_id={_GRID_RUN_ID}&rank=3")
        assert page.status_code == 200
        payload = client.get(f"/api/v1/oneclick/top100?run_id={_GRID_RUN_ID}").json()
        ranks = [row["rank"] for row in payload["rows"]]
        assert 3 in ranks  # the addressed rank exists on the wire
        static = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "static"
        codec = (static / "bx-portal-state.js").read_text(encoding="utf-8")
        panel = (static / "bx-strategy-panel.js").read_text(encoding="utf-8")
        # The client-side contract, pinned (no browser harness — honest
        # envelope). The R2 documented split (DEC-OPERATOR-PAGE-CONSOLIDATE-
        # 001): the two PARSE expressions live in the pure BXPortalState
        # codec (executed for real by node goldens); the row-match loop, the
        # honest rank-miss message and the latest-run fallback live in the
        # strategy panel module — each string asserted verbatim in exactly
        # one file, never weakened.
        assert "state.runId = params.get('run_id');" in codec
        assert "state.rank = Math.max(1, parseInt(params.get('rank') || '1', 10) || 1);" in codec
        assert "if (Number(rows[i].rank) === state.rank) { row = rows[i]; break; }" in panel
        assert "is not in the stored top-100 (" in panel  # honest rank-miss
        assert "runs.latest_run_id" in panel  # no-run_id fallback
        # The strategy page loads both modules + self-mounts the panel.
        template = (
            Path(__file__).resolve().parents[2]
            / "brutex"
            / "dashboard"
            / "templates"
            / "strategy.html"
        ).read_text(encoding="utf-8")
        assert "/static/bx-portal-state.js" in template
        assert "/static/bx-strategy-panel.js" in template
        assert 'data-bx-autopanel="strategy"' in template

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
