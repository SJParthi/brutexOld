"""Integration suite for the ``/api/v1/oneclick/*`` routes (DEC-OPERATOR-PAGE-REAL-001).

Drives the six JSON routes end-to-end through ``build_app`` + ``TestClient``
over a tmp_path lake carrying the CONTRACT-shaped fixture corpus
(``tests/fixtures/oneclick``), per the T-M4 per-route branch checklist:
happy · awaiting (empty lake) · corrupt → 200 named status never a 500 ·
400 missing/unsafe run_id · 400 junk fixture env · ``verify?check=chain``
present AND absent (cached-last-verdict branch primed and cold) ·
``artifact_revision None`` stat-outage → fresh-uncached branch · fixture-root
active/inactive · ``offset=-1`` through the route · empty + short top100
through the route · concurrent-poll consistency · the hostile XSS twin never
500s (its strings survive as DATA only).

Fails-before framing: the routes are NEW in this change; the pins here are
the honesty laws — a poll can never error-loop (unknown runs are 200
``awaiting``), and production trades stay ``awaiting`` unless the fail-loud
fixture root (or an injected reader) is active.
"""

# The fixture-root log-once reset seam is exercised by design.
# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing

pytest.importorskip("fastapi")

from brutex.dashboard import oneclick_artifacts as oc
from brutex.dashboard.app import DashboardState, build_app
from fastapi.testclient import TestClient

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "oneclick"
RUN_ID = "r-20260707-fixt01"
ALL_RUN_IDS = {RUN_ID, "r-empty01", "r-short01", "r-corrupt01", "r-hostile01"}


@pytest.fixture(autouse=True)
def _clean_fixture_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Every test starts with the fixture env UNSET + the log-once seam re-armed."""
    monkeypatch.delenv(oc.FIXTURE_ROOT_ENV, raising=False)
    oc._reset_fixture_root_logged()
    yield
    oc._reset_fixture_root_logged()


def _lake_with_corpus(tmp_path: Path) -> Path:
    """A tmp lake whose Ruling-4 ``<lake>/sweep_runs`` is the committed fixture corpus."""
    lake = tmp_path / "lake"
    shutil.copytree(_FIXTURES / "sweep_runs", lake / "sweep_runs")
    return lake


def _client(lake: Path | None, **kwargs: Any) -> TestClient:
    """A TestClient over a SimpleNamespace store (the house lake-root idiom)."""
    store = SimpleNamespace(path=lake / "instruments.duckdb") if lake is not None else None
    return TestClient(build_app(DashboardState(), instrument_store=store, **kwargs))


# ── /api/v1/oneclick/runs ───────────────────────────────────────────────


class TestRunsRoute:
    def test_happy_lists_the_corpus_runs(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        payload = client.get("/api/v1/oneclick/runs").json()
        assert payload["status"] == "ok"
        assert {row["run_id"] for row in payload["runs"]} == ALL_RUN_IDS
        assert payload["latest_run_id"] in ALL_RUN_IDS
        assert payload["fixture_root_active"] is False
        by_id = {row["run_id"]: row for row in payload["runs"]}
        assert by_id["r-hostile01"]["suspect"] is True
        assert by_id[RUN_ID]["suspect"] is False
        assert by_id["r-empty01"]["suspect"] is None  # no manifest — never fabricated

    def test_empty_lake_is_loud_awaiting(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        lake.mkdir()
        payload = _client(lake).get("/api/v1/oneclick/runs").json()
        assert payload["status"] == "awaiting"
        assert payload["runs"] == []
        assert "awaiting real run artifact" in payload["detail"]

    def test_store_without_path_degrades_to_awaiting(self) -> None:
        client = TestClient(
            build_app(DashboardState(), instrument_store=SimpleNamespace(path=None))
        )
        payload = client.get("/api/v1/oneclick/runs").json()
        assert payload["status"] == "awaiting"

    def test_no_store_is_200_awaiting_never_503(self) -> None:
        """No instrument store wired ⇒ honest awaiting — the 503 regression pin."""
        response = _client(None).get("/api/v1/oneclick/runs")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "awaiting"
        assert payload["runs"] == []
        assert payload["fixture_root_active"] is False

    def test_cold_probe_outage_computes_fresh_with_one_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C-r2-5: an outage with NO last-known listing computes fresh — LOGGED."""
        client = _client(_lake_with_corpus(tmp_path))
        monkeypatch.setattr(oc, "runs_revision_probe", lambda _root: oc.ArtifactProbe(None, True))
        with structlog.testing.capture_logs() as logs:
            payload = client.get("/api/v1/oneclick/runs").json()
        assert payload["status"] == "ok"
        assert {row["run_id"] for row in payload["runs"]} == ALL_RUN_IDS
        outages = [e for e in logs if e["event"] == "oneclick.probe_outage"]
        assert len(outages) == 1
        assert outages[0]["last_known"] is False

    def test_probe_outage_serves_the_last_known_listing_with_one_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C-r2-5: a stat outage serves the LAST-KNOWN listing (no re-walk, one WARN)."""
        client = _client(_lake_with_corpus(tmp_path))
        first = client.get("/api/v1/oneclick/runs").json()  # primes the cache
        monkeypatch.setattr(oc, "runs_revision_probe", lambda _root: oc.ArtifactProbe(None, True))

        def _must_not_walk(_root: Path, *, limit: int = 50) -> Any:
            raise AssertionError("an outage with a cached listing must never re-walk")

        monkeypatch.setattr(oc, "discover_runs", _must_not_walk)
        with structlog.testing.capture_logs() as logs:
            second = client.get("/api/v1/oneclick/runs").json()
            third = client.get("/api/v1/oneclick/runs").json()
        assert second == first
        assert third == first
        outages = [e for e in logs if e["event"] == "oneclick.probe_outage"]
        assert len(outages) == 1  # once per outage, never per poll
        assert outages[0]["last_known"] is True

    def test_probe_recovery_resumes_fresh_keying(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C-r2-5: when the probe answers again, the route re-keys on the NEW revision."""
        lake = _lake_with_corpus(tmp_path)
        client = _client(lake)
        real_probe = oc.runs_revision_probe
        state = {"outage": False}

        def _probe(root: Path) -> oc.ArtifactProbe:
            if state["outage"]:
                return oc.ArtifactProbe(None, True)
            return real_probe(root)

        monkeypatch.setattr(oc, "runs_revision_probe", _probe)
        first = client.get("/api/v1/oneclick/runs").json()  # primes healthy
        state["outage"] = True
        assert client.get("/api/v1/oneclick/runs").json() == first  # last-known serve
        state["outage"] = False
        (lake / "sweep_runs" / "r-fresh01").mkdir()  # a NEW run landed meanwhile
        with structlog.testing.capture_logs() as logs:
            recovered = client.get("/api/v1/oneclick/runs").json()
        assert "r-fresh01" in {row["run_id"] for row in recovered["runs"]}
        assert any(e["event"] == "oneclick.probe_recovered" for e in logs)

    def test_junk_fixture_env_is_400_naming_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oc.FIXTURE_ROOT_ENV, str(tmp_path / "nope"))
        response = _client(_lake_with_corpus(tmp_path)).get("/api/v1/oneclick/runs")
        assert response.status_code == 400
        assert oc.FIXTURE_ROOT_ENV in response.json()["detail"]

    def test_fixture_root_serves_without_any_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fixture override wins BEFORE the store requirement (badge stamped)."""
        monkeypatch.setenv(oc.FIXTURE_ROOT_ENV, str(_FIXTURES))
        payload = _client(None).get("/api/v1/oneclick/runs").json()
        assert payload["status"] == "ok"
        assert payload["fixture_root_active"] is True
        assert RUN_ID in {row["run_id"] for row in payload["runs"]}


# ── /api/v1/oneclick/manifest ───────────────────────────────────────────


class TestManifestRoute:
    def test_missing_run_id_is_400(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        assert client.get("/api/v1/oneclick/manifest").status_code == 400
        assert client.get("/api/v1/oneclick/manifest?run_id=%20").status_code == 400

    def test_traversal_run_id_is_400(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        response = client.get("/api/v1/oneclick/manifest?run_id=..%2Fevil")
        assert response.status_code == 400
        assert "safe path segment" in response.json()["detail"]

    def test_happy_manifest_with_complete_pill(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        payload = client.get(f"/api/v1/oneclick/manifest?run_id={RUN_ID}").json()
        assert payload["status"] == "ok"
        assert payload["manifest"]["run_id"] == RUN_ID
        assert payload["pill"]["state"] == "complete"
        assert payload["revision"] is not None
        assert payload["fixture_root_active"] is False

    def test_suspect_manifest_reads_suspect_pill(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        payload = client.get("/api/v1/oneclick/manifest?run_id=r-hostile01").json()
        assert payload["pill"]["state"] == "suspect"
        assert payload["manifest"]["suspect"] is True

    def test_unknown_run_is_200_awaiting_never_404(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        response = client.get("/api/v1/oneclick/manifest?run_id=r-nope")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "awaiting"
        assert payload["pill"]["state"] == "awaiting"

    def test_no_lake_root_is_awaiting(self) -> None:
        client = TestClient(
            build_app(DashboardState(), instrument_store=SimpleNamespace(path=None))
        )
        payload = client.get(f"/api/v1/oneclick/manifest?run_id={RUN_ID}").json()
        assert payload["status"] == "awaiting"

    def test_repeat_poll_serves_the_revision_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Parse-once-per-revision: after priming, a poisoned loader is never called."""
        client = _client(_lake_with_corpus(tmp_path))
        first = client.get(f"/api/v1/oneclick/manifest?run_id={RUN_ID}").json()

        def _must_not_run(_run_dir: Path) -> Any:
            raise AssertionError("a primed revision must not re-parse the manifest")

        monkeypatch.setattr(oc, "load_manifest", _must_not_run)
        second = client.get(f"/api/v1/oneclick/manifest?run_id={RUN_ID}").json()
        assert second == first


# ── /api/v1/oneclick/top100 ─────────────────────────────────────────────


class TestTop100Route:
    def test_happy_100_rows_full_precision(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        payload = client.get(f"/api/v1/oneclick/top100?run_id={RUN_ID}").json()
        assert payload["status"] == "ok"
        assert payload["row_count"] == 100
        assert len(payload["rows"]) == 100
        assert payload["empty_note"] is None
        assert payload["rows"][0]["rank"] == 1
        # The fixture carries the ENGINE metric name (sharpe_ratio); the wire
        # row carries BOTH it and the reader's display alias, full precision.
        assert payload["rows"][0]["metrics"]["sharpe"] == 3.2
        assert payload["rows"][0]["metrics"]["sharpe_ratio"] == 3.2
        assert payload["preset"] == "min_dd_first"  # the sort-basis label source

    def test_valid_empty_artifact_is_ok_with_note(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        payload = client.get("/api/v1/oneclick/top100?run_id=r-empty01").json()
        assert payload["status"] == "ok"
        assert payload["row_count"] == 0
        assert payload["empty_note"] == "0 winners stored for this run"

    def test_short_artifact_reports_true_row_count(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        payload = client.get("/api/v1/oneclick/top100?run_id=r-short01").json()
        assert payload["status"] == "ok"
        assert payload["row_count"] == 17  # never assumed 100 (T-M2)

    def test_corrupt_artifact_is_200_named_corrupt(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        response = client.get("/api/v1/oneclick/top100?run_id=r-corrupt01")
        assert response.status_code == 200  # never a 500
        payload = response.json()
        assert payload["status"] == "corrupt"
        assert payload["row_count"] == 0

    def test_missing_run_id_is_400(self, tmp_path: Path) -> None:
        assert (
            _client(_lake_with_corpus(tmp_path)).get("/api/v1/oneclick/top100").status_code == 400
        )

    def test_fixture_root_stamps_the_badge(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(oc.FIXTURE_ROOT_ENV, str(_FIXTURES))
        payload = _client(None).get(f"/api/v1/oneclick/top100?run_id={RUN_ID}").json()
        assert payload["fixture_root_active"] is True
        assert payload["row_count"] == 100

    def test_unknown_run_is_awaiting(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        payload = client.get("/api/v1/oneclick/top100?run_id=r-nope").json()
        assert payload["status"] == "awaiting"
        assert payload["row_count"] == 0

    def test_no_lake_root_is_awaiting(self) -> None:
        client = TestClient(
            build_app(DashboardState(), instrument_store=SimpleNamespace(path=None))
        )
        payload = client.get(f"/api/v1/oneclick/top100?run_id={RUN_ID}").json()
        assert payload["status"] == "awaiting"
        assert payload["empty_note"] is None

    def test_probe_outage_serves_the_last_known_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C-r2-5 on a representative artifact route: outage ⇒ last-known + WARN."""
        client = _client(_lake_with_corpus(tmp_path))
        first = client.get(f"/api/v1/oneclick/top100?run_id={RUN_ID}").json()
        assert first["status"] == "ok"
        monkeypatch.setattr(oc, "artifact_probe", lambda _path: oc.ArtifactProbe(None, True))

        def _must_not_run(_run_dir: Path) -> Any:
            raise AssertionError("an outage with a cached payload must never re-parse")

        monkeypatch.setattr(oc, "load_top100", _must_not_run)
        with structlog.testing.capture_logs() as logs:
            second = client.get(f"/api/v1/oneclick/top100?run_id={RUN_ID}").json()
        assert second == first
        assert any(e["event"] == "oneclick.probe_outage" for e in logs)


# ── /api/v1/oneclick/events ─────────────────────────────────────────────


class TestEventsRoute:
    def test_happy_first_page(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        payload = client.get(f"/api/v1/oneclick/events?run_id={RUN_ID}&limit=5").json()
        assert payload["status"] == "ok"
        assert len(payload["records"]) == 5
        assert payload["window_filtered"] is False
        assert payload["truncated_tail"] is False
        assert 0 < payload["next_offset"] < payload["total_bytes"]

    def test_tail_anchor_through_the_route(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        payload = client.get(f"/api/v1/oneclick/events?run_id={RUN_ID}&offset=-1").json()
        assert payload["status"] == "ok"
        assert payload["records"]  # the tail window serves records

    def test_kind_filter_marks_window_filtered(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        payload = client.get(
            f"/api/v1/oneclick/events?run_id={RUN_ID}&kind=checkpoint.saved&limit=1000"
        ).json()
        assert payload["window_filtered"] is True
        assert [record["kind"] for record in payload["records"]] == ["checkpoint.saved"]

    def test_half_written_tail_is_honest(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        payload = client.get("/api/v1/oneclick/events?run_id=r-corrupt01&limit=1000").json()
        assert payload["status"] == "ok"
        assert payload["truncated_tail"] is True
        assert len(payload["records"]) == 2

    def test_eof_poll_short_circuits_with_stable_cursor(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        full = client.get(f"/api/v1/oneclick/events?run_id={RUN_ID}&limit=1000").json()
        assert full["next_offset"] == full["total_bytes"]  # caught up
        eof = f"/api/v1/oneclick/events?run_id={RUN_ID}&offset={full['next_offset']}"
        first = client.get(eof).json()
        second = client.get(eof).json()  # the cached short-circuit poll
        assert first == second
        assert first["records"] == []
        assert first["next_offset"] == full["total_bytes"]

    def test_unknown_run_is_awaiting(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        payload = client.get("/api/v1/oneclick/events?run_id=r-nope").json()
        assert payload["status"] == "awaiting"

    def test_offset_below_minus_one_is_422(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        response = client.get(f"/api/v1/oneclick/events?run_id={RUN_ID}&offset=-2")
        assert response.status_code == 422  # FastAPI bound validation

    def test_no_lake_root_is_awaiting(self) -> None:
        client = TestClient(
            build_app(DashboardState(), instrument_store=SimpleNamespace(path=None))
        )
        payload = client.get(f"/api/v1/oneclick/events?run_id={RUN_ID}&q=x").json()
        assert payload["status"] == "awaiting"
        assert payload["window_filtered"] is True


# ── /api/v1/oneclick/trades ─────────────────────────────────────────────


class TestTradesRoute:
    def test_production_default_is_honest_awaiting(self, tmp_path: Path) -> None:
        """C-M5 / DEC-ONECLICK-SWEEP-RUNS-REBIND-001: no fixture root ⇒ the
        production TradeStoreReader — a run without results-depth references
        (the corpus top100 predates #1275) degrades to honest awaiting, NEVER
        fabricated rows."""
        client = _client(_lake_with_corpus(tmp_path))
        payload = client.get(f"/api/v1/oneclick/trades?run_id={RUN_ID}&rank=1").json()
        assert payload["status"] == "awaiting"
        assert payload["rows"] == []
        assert payload["total"] is None
        assert payload["source"] == "none"
        assert payload["fixture_root_active"] is False
        assert "pre-#1275" in payload["detail"]

    def test_fixture_root_serves_paged_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(oc.FIXTURE_ROOT_ENV, str(_FIXTURES))
        client = _client(None)
        payload = client.get(
            f"/api/v1/oneclick/trades?run_id={RUN_ID}&rank=1&offset=10&limit=5"
        ).json()
        assert payload["status"] == "ok"
        assert payload["source"] == "fixture"
        assert payload["total"] == 412
        assert len(payload["rows"]) == 5
        assert payload["offset"] == 10
        assert payload["fixture_root_active"] is True
        # R31 honest NULL: the corpus's every-13th row carries null VIX.
        page0 = client.get(
            f"/api/v1/oneclick/trades?run_id={RUN_ID}&rank=1&offset=0&limit=1"
        ).json()
        assert page0["rows"][0]["vix_entry"] is None

    def test_missing_rank_file_is_awaiting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(oc.FIXTURE_ROOT_ENV, str(_FIXTURES))
        payload = _client(None).get(f"/api/v1/oneclick/trades?run_id={RUN_ID}&rank=42").json()
        assert payload["status"] == "awaiting"
        assert payload["total"] is None

    def test_injected_reader_memo_survives_across_requests(self, tmp_path: Path) -> None:
        """The typed seam: an injected reader is used verbatim; one parse for N pages."""
        reader = oc.FixtureTradesReader(_FIXTURES)
        client = _client(_lake_with_corpus(tmp_path), oneclick_trades_reader=reader)
        for offset in (0, 100, 200):
            payload = client.get(
                f"/api/v1/oneclick/trades?run_id={RUN_ID}&rank=1&offset={offset}&limit=50"
            ).json()
            assert payload["status"] == "ok"
            assert payload["source"] == "fixture"
            # env is UNSET: the badge stays honest even with an injected reader.
            assert payload["fixture_root_active"] is False
        assert reader.parse_count == 1

    def test_junk_fixture_env_is_400(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(oc.FIXTURE_ROOT_ENV, str(tmp_path / "nope"))
        response = _client(_lake_with_corpus(tmp_path)).get(
            f"/api/v1/oneclick/trades?run_id={RUN_ID}"
        )
        assert response.status_code == 400
        assert oc.FIXTURE_ROOT_ENV in response.json()["detail"]

    def test_missing_run_id_is_400(self, tmp_path: Path) -> None:
        assert (
            _client(_lake_with_corpus(tmp_path)).get("/api/v1/oneclick/trades").status_code == 400
        )


# ── /api/v1/oneclick/verify ─────────────────────────────────────────────


class TestEquityRoute:
    """GET /api/v1/oneclick/equity (DEC-ONECLICK-WINNER-DETAIL-TRUTH-001 4C).

    The ok-path off REAL engine-written sidecars lives in the writer→reader
    golden (test_oneclick_top100_writer_reader.py); here: the honesty
    degrades through the route — never a 500, never a fabricated curve.
    """

    def test_production_default_without_sidecars_is_awaiting(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        payload = client.get(f"/api/v1/oneclick/equity?run_id={RUN_ID}&rank=1").json()
        assert payload["status"] == "awaiting"
        assert payload["rows"] == []
        assert payload["total"] is None
        assert payload["source"] == "none"
        assert "pre-#1275" in payload["detail"]

    def test_fixture_root_is_honest_awaiting_never_synthesized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fixture tree carries trades pages but NO equity sidecars — the
        route says so instead of synthesizing a curve from the trades."""
        monkeypatch.setenv(oc.FIXTURE_ROOT_ENV, str(_FIXTURES))
        payload = _client(None).get(f"/api/v1/oneclick/equity?run_id={RUN_ID}&rank=1").json()
        assert payload["status"] == "awaiting"
        assert "no results-depth equity sidecars" in payload["detail"]
        assert payload["fixture_root_active"] is True

    def test_no_lake_configuration_is_awaiting(self, tmp_path: Path) -> None:
        payload = _client(None).get("/api/v1/oneclick/equity?run_id=r-x&rank=1").json()
        assert payload["status"] == "awaiting"
        assert "no lake root wired for the equity series" in payload["detail"]

    def test_missing_and_traversal_run_id_are_400(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        assert client.get("/api/v1/oneclick/equity").status_code == 400
        assert client.get("/api/v1/oneclick/equity?run_id=..%2Fevil&rank=1").status_code == 400

    def test_hostile_query_bounds_are_422_by_validation(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        assert client.get(f"/api/v1/oneclick/equity?run_id={RUN_ID}&rank=0").status_code == 422
        assert (
            client.get(f"/api/v1/oneclick/equity?run_id={RUN_ID}&rank=1&limit=999999").status_code
            == 422
        )


class TestVerifyRoute:
    def test_cold_peek_reads_awaiting_chain(self, tmp_path: Path) -> None:
        """T-M4 cold branch: never verified ⇒ awaiting — no surprise O(file) hash."""
        client = _client(_lake_with_corpus(tmp_path))
        payload = client.get(f"/api/v1/oneclick/verify?run_id={RUN_ID}").json()
        checks = {check["id"]: check for check in payload["checks"]}
        assert checks[2]["state"] == "awaiting"
        assert "(algorithm assumed until P2 merges)" in checks[2]["detail"]
        assert payload["funnel_reconcile"]["ok"] is True  # the corpus reconciles

    def test_check_chain_verifies_and_then_serves_the_cached_verdict(self, tmp_path: Path) -> None:
        """T-M4 primed branch: the click verifies; the next poll serves the verdict."""
        client = _client(_lake_with_corpus(tmp_path))
        clicked = client.get(f"/api/v1/oneclick/verify?run_id={RUN_ID}&check=chain").json()
        row = {check["id"]: check for check in clicked["checks"]}[2]
        assert row["state"] == "pass"
        assert row["records"] == 118  # 8 starts + 100 tuples + tripwire + checkpoint + 8 dones
        assert "(algorithm assumed until P2 merges)" in row["detail"]
        polled = client.get(f"/api/v1/oneclick/verify?run_id={RUN_ID}").json()
        polled_row = {check["id"]: check for check in polled["checks"]}[2]
        assert polled_row["state"] == "pass"
        assert polled_row["records"] == 118

    def test_repeat_poll_serves_the_revision_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F1 (DEC-ONECLICK-PERF-CLUB-001): after priming, a poisoned manifest
        loader is never called — the verify payload is revision-cached like
        the sibling routes (the pre-fix route re-parsed per poll)."""
        client = _client(_lake_with_corpus(tmp_path))
        first = client.get(f"/api/v1/oneclick/verify?run_id={RUN_ID}").json()

        def _must_not_run(_run_dir: Path) -> Any:
            raise AssertionError("a primed verify revision must not re-parse the manifest")

        monkeypatch.setattr(oc, "load_manifest", _must_not_run)
        second = client.get(f"/api/v1/oneclick/verify?run_id={RUN_ID}").json()
        assert second == first

    def test_chain_click_moves_the_key_then_polls_serve_the_clicked_payload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F1: the chain verified-state is IN the key — a click re-keys once,
        then plain polls serve the clicked payload from the cache."""
        client = _client(_lake_with_corpus(tmp_path))
        client.get(f"/api/v1/oneclick/verify?run_id={RUN_ID}").json()  # primes chain=0
        clicked = client.get(f"/api/v1/oneclick/verify?run_id={RUN_ID}&check=chain").json()

        def _must_not_run(_run_dir: Path) -> Any:
            raise AssertionError("a primed post-click revision must not re-parse")

        monkeypatch.setattr(oc, "load_manifest", _must_not_run)
        polled = client.get(f"/api/v1/oneclick/verify?run_id={RUN_ID}").json()
        assert polled == clicked

    def test_probe_outage_serves_the_last_known_payload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C-r2-5: a manifest stat outage serves the last-known verify payload
        with ONE loud WARN — never a silent per-poll re-parse."""
        client = _client(_lake_with_corpus(tmp_path))
        first = client.get(f"/api/v1/oneclick/verify?run_id={RUN_ID}").json()
        monkeypatch.setattr(oc, "artifact_probe", lambda _path: oc.ArtifactProbe(None, True))

        def _must_not_run(_run_dir: Path) -> Any:
            raise AssertionError("an outage with a cached payload must never re-parse")

        monkeypatch.setattr(oc, "load_manifest", _must_not_run)
        with structlog.testing.capture_logs() as logs:
            second = client.get(f"/api/v1/oneclick/verify?run_id={RUN_ID}").json()
            third = client.get(f"/api/v1/oneclick/verify?run_id={RUN_ID}").json()
        assert second == first
        assert third == first
        outages = [e for e in logs if e["event"] == "oneclick.probe_outage"]
        assert len(outages) == 1

    def test_manifestless_run_computes_fresh_per_poll(self, tmp_path: Path) -> None:
        """The ABSENT arm: a manifest-less run is honest awaiting, fresh per
        poll (cheap — no manifest to parse), never cached under a stale key."""
        client = _client(_lake_with_corpus(tmp_path))
        for _ in range(2):
            payload = client.get("/api/v1/oneclick/verify?run_id=r-empty01").json()
            assert len(payload["checks"]) == 7
            assert payload["funnel_reconcile"]["ok"] is None

    def test_tampered_chain_fails_with_the_break_line(self, tmp_path: Path) -> None:
        lake = _lake_with_corpus(tmp_path)
        events = lake / "sweep_runs" / RUN_ID / "events.jsonl"
        lines = events.read_text(encoding="utf-8").splitlines()
        tampered = json.loads(lines[4])
        tampered["kind"] = "tampered"
        lines[4] = json.dumps(tampered, sort_keys=True, separators=(",", ":"))
        events.write_text("\n".join(lines) + "\n", encoding="utf-8")
        client = _client(lake)
        payload = client.get(f"/api/v1/oneclick/verify?run_id={RUN_ID}&check=chain").json()
        row = {check["id"]: check for check in payload["checks"]}[2]
        assert row["state"] == "fail"
        assert row["first_break_line"] == 5

    def test_no_events_file_stays_awaiting_even_on_click(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        payload = client.get("/api/v1/oneclick/verify?run_id=r-empty01&check=chain").json()
        row = {check["id"]: check for check in payload["checks"]}[2]
        assert row["state"] == "awaiting"

    def test_empty_events_file_click_is_awaiting_never_a_vacuous_pass(self, tmp_path: Path) -> None:
        """Regression: a run killed before the recorder's first write leaves a
        ZERO-byte events.jsonl whose chain replays "clean" over 0 records —
        the click must read awaiting (verified nothing), never a green PASS."""
        lake = _lake_with_corpus(tmp_path)
        events_path = lake / "sweep_runs" / RUN_ID / "events.jsonl"
        events_path.write_bytes(b"")
        payload = _client(lake).get(f"/api/v1/oneclick/verify?run_id={RUN_ID}&check=chain").json()
        row = {check["id"]: check for check in payload["checks"]}[2]
        assert row["state"] == "awaiting"
        assert row["records"] == 0
        assert "verifies nothing" in row["detail"]

    def test_empty_funnel_is_awaiting_never_a_vacuous_pass(self, tmp_path: Path) -> None:
        """Regression: an empty actual.funnel + per_door_kills used to read a
        green check #7 having reconciled nothing — it must abstain."""
        lake = _lake_with_corpus(tmp_path)
        manifest_path = lake / "sweep_runs" / RUN_ID / "manifest.json"
        body = json.loads(manifest_path.read_text(encoding="utf-8"))
        body["actual"]["funnel"] = []
        body["per_door_kills"] = []
        manifest_path.write_text(json.dumps(body), encoding="utf-8")
        payload = _client(lake).get(f"/api/v1/oneclick/verify?run_id={RUN_ID}").json()
        checks = {check["id"]: check for check in payload["checks"]}
        assert checks[7]["state"] == "awaiting"
        assert payload["funnel_reconcile"]["ok"] is None
        assert "actual.funnel is empty" in payload["funnel_reconcile"]["lines"][0]

    def test_truth_table_caps_hold_through_the_route(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1/#5/#6 not_wired always; #3/#4 cap at ready (fixture trades on)."""
        monkeypatch.setenv(oc.FIXTURE_ROOT_ENV, str(_FIXTURES))
        payload = _client(None).get(f"/api/v1/oneclick/verify?run_id={RUN_ID}").json()
        checks = {check["id"]: check for check in payload["checks"]}
        for check_id in (1, 5, 6):
            assert checks[check_id]["state"] == "not_wired"
        assert checks[3]["state"] == "ready"
        assert checks[4]["state"] == "ready"
        assert payload["fixture_root_active"] is True

    def test_without_trades_operator_checks_await(self, tmp_path: Path) -> None:
        payload = (
            _client(_lake_with_corpus(tmp_path))
            .get(f"/api/v1/oneclick/verify?run_id={RUN_ID}")
            .json()
        )
        checks = {check["id"]: check for check in payload["checks"]}
        assert checks[3]["state"] == "awaiting"
        assert checks[4]["state"] == "awaiting"

    def test_broken_funnel_fails_check_7(self, tmp_path: Path) -> None:
        lake = _lake_with_corpus(tmp_path)
        manifest_path = lake / "sweep_runs" / RUN_ID / "manifest.json"
        body = json.loads(manifest_path.read_text(encoding="utf-8"))
        body["actual"]["enumerated"] = 999_999
        manifest_path.write_text(json.dumps(body), encoding="utf-8")
        payload = _client(lake).get(f"/api/v1/oneclick/verify?run_id={RUN_ID}").json()
        checks = {check["id"]: check for check in payload["checks"]}
        assert checks[7]["state"] == "fail"
        assert payload["funnel_reconcile"]["ok"] is False

    def test_no_lake_root_is_awaiting_shape(self) -> None:
        client = TestClient(
            build_app(DashboardState(), instrument_store=SimpleNamespace(path=None))
        )
        payload = client.get(f"/api/v1/oneclick/verify?run_id={RUN_ID}").json()
        assert len(payload["checks"]) == 7
        assert payload["funnel_reconcile"]["ok"] is None

    def test_missing_run_id_is_400(self, tmp_path: Path) -> None:
        assert (
            _client(_lake_with_corpus(tmp_path)).get("/api/v1/oneclick/verify").status_code == 400
        )


# ── no instrument store wired: honest awaiting, never a 503 ────────────


class TestNoStoreHonestAwaiting:
    """``build_app`` WITHOUT an instrument store: every oneclick route is a
    200 honest ``awaiting`` payload — the ``_oneclick_roots`` docstring promise
    (regression: ``_require_store`` used to 503 every route here)."""

    def test_runs_awaits(self) -> None:
        response = _client(None).get("/api/v1/oneclick/runs")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "awaiting"
        assert payload["runs"] == []
        assert payload["fixture_root_active"] is False
        assert "awaiting real run artifact" in payload["detail"]

    def test_manifest_awaits(self) -> None:
        response = _client(None).get(f"/api/v1/oneclick/manifest?run_id={RUN_ID}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "awaiting"
        assert payload["pill"]["state"] == "awaiting"
        assert payload["fixture_root_active"] is False

    def test_top100_awaits(self) -> None:
        response = _client(None).get(f"/api/v1/oneclick/top100?run_id={RUN_ID}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "awaiting"
        assert payload["row_count"] == 0
        assert payload["fixture_root_active"] is False

    def test_events_awaits(self) -> None:
        response = _client(None).get(f"/api/v1/oneclick/events?run_id={RUN_ID}")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "awaiting"
        assert payload["records"] == []
        assert payload["fixture_root_active"] is False

    def test_trades_awaits(self) -> None:
        response = _client(None).get(f"/api/v1/oneclick/trades?run_id={RUN_ID}&rank=1")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "awaiting"
        assert payload["rows"] == []
        assert payload["total"] is None
        assert payload["source"] == "none"
        assert payload["fixture_root_active"] is False

    def test_verify_awaits(self) -> None:
        response = _client(None).get(f"/api/v1/oneclick/verify?run_id={RUN_ID}")
        assert response.status_code == 200
        payload = response.json()
        assert len(payload["checks"]) == 7
        assert payload["funnel_reconcile"]["ok"] is None
        assert payload["fixture_root_active"] is False
        # No un-earned green: an unwired lake can never read "pass".
        assert all(check["state"] != "pass" for check in payload["checks"])


# ── concurrency + hostile twin ──────────────────────────────────────────


class TestConcurrentPolls:
    def test_eight_concurrent_polls_agree(self, tmp_path: Path) -> None:
        """R29 worst case: concurrent polls mid-run — one payload, no error-loop."""
        client = _client(_lake_with_corpus(tmp_path))

        def _poll(_i: int) -> dict[str, Any]:
            response = client.get(f"/api/v1/oneclick/events?run_id={RUN_ID}&limit=50")
            assert response.status_code == 200
            return dict(response.json())

        with ThreadPoolExecutor(max_workers=8) as pool:
            payloads = list(pool.map(_poll, range(8)))
        assert all(payload == payloads[0] for payload in payloads)


class TestHostileFixtureNever500:
    """F-M2 backend half: the XSS twin serves 200 everywhere, strings as DATA."""

    def test_every_route_serves_the_hostile_twin(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        for url in (
            "/api/v1/oneclick/manifest?run_id=r-hostile01",
            "/api/v1/oneclick/top100?run_id=r-hostile01",
            "/api/v1/oneclick/events?run_id=r-hostile01",
            "/api/v1/oneclick/verify?run_id=r-hostile01",
        ):
            assert client.get(url).status_code == 200

    def test_hostile_strings_survive_as_json_data(self, tmp_path: Path) -> None:
        client = _client(_lake_with_corpus(tmp_path))
        rows = client.get("/api/v1/oneclick/top100?run_id=r-hostile01").json()["rows"]
        assert rows[0]["conditions"][0] == "<script>alert(1)</script>"
        events = client.get("/api/v1/oneclick/events?run_id=r-hostile01").json()["records"]
        assert events[0]["kind"] == "<script>alert(1)</script>"
        assert events[0]["payload"]["note"] == "<svg onload=alert(1)>"


# ── the production trades rebind (DEC-ONECLICK-SWEEP-RUNS-REBIND-001) ───


_COMBO = "combo-0001"
_TUPLE_REF = "NSE-NIFTY/5m/long_k3_deadbeef"


def _rebind_lake(tmp_path: Path, *, n_trades: int) -> Path:
    """A tmp lake whose run carries REAL writer-built results-depth sidecars.

    The top100.json is the CONTRACT wire shape — the writer's OBJECT root
    (DEC-ONECLICK-TOP100-OBJECT-ROOT-001) — and the sidecar trio + stamp are
    built via trade_store's OWN public writer; internal formats are never
    hand-forged.
    """
    pytest.importorskip("polars")
    from datetime import UTC, datetime
    from decimal import Decimal

    from brutex.engine.minute_path import PathStatus
    from brutex.engine.trade_capture import CapturedTrade, ComboCapture
    from brutex.engine.trade_store import write_tuple_sidecars
    from brutex.strategies.types import Side

    lake = tmp_path / "lake"
    run_dir = lake / "sweep_runs" / RUN_ID
    row = {
        "rank": 1,
        "run_id": _COMBO,
        "mask_hex": "0x05",
        "conditions": ["PDH_CROSS_UP"],
        "tf": "5m",
        "direction": "long",
        "arm": {"sl_paisa": None, "tp_paisa": None},
        "metrics": {"sharpe": 1.25},
        "regime": {},
        "vix_band": None,
        "family_id": "fam-0001",
        "equity_ref": {"tuple": _TUPLE_REF, "sidecar": "equity"},
    }
    run_dir.mkdir(parents=True)
    doc = {
        "schema_version": oc.TOP100_SCHEMA_VERSION,
        "run_id": RUN_ID,
        "preset": "min_dd_first",
        "source": {"pool": 1},
        "baselines": {},
        "entries": [row],
    }
    (run_dir / oc.TOP100_FILENAME).write_text(json.dumps(doc), encoding="utf-8")

    statuses = [PathStatus.OK, PathStatus.PARTIAL, PathStatus.NO_1M_DATA]
    trades = []
    for i in range(n_trades):
        status = statuses[i % 3]
        with_1m = status is not PathStatus.NO_1M_DATA
        trades.append(
            CapturedTrade(
                entry_ts=datetime(2026, 7, 1, 4, i % 60, i // 60 % 60, tzinfo=UTC),
                entry_px=Decimal("100.00"),
                exit_ts=datetime(2026, 7, 1, 6, i % 60, i // 60 % 60, tzinfo=UTC),
                exit_px=Decimal("101.00"),
                direction=Side.BUY,
                lots=None,  # Ruling 18 NULL-by-design
                exit_reason="signal",
                pnl_paisa=100 + i,
                costs_paisa=None,  # Ruling 18 NULL-by-design
                mae_paisa=50 + i,
                mfe_paisa=150 + i,
                mae_1m_paisa=(40 + i) if with_1m else None,
                mfe_1m_paisa=(160 + i) if with_1m else None,
                path_1m_status=status,
                vix_entry=None,
                vix_exit=None,
            )
        )
    capture = ComboCapture(
        run_id=_COMBO,
        trades=tuple(trades),
        equity=(),
        equity_status=PathStatus.OK,
        vix_missing_trades=0,
        parity_ok=True,
        parity_detail="",
    )
    paths = oc.sidecar_paths_from_ref(run_dir, _TUPLE_REF)
    assert paths is not None
    write_tuple_sidecars(paths, [capture], [], store_content_hash="0" * 64)
    return lake


class TestTradesRebindEndToEnd:
    """The real-reader activation: sidecars exist ⇒ source='results_store'."""

    def test_real_reader_activates_when_sidecars_exist(self, tmp_path: Path) -> None:
        client = _client(_rebind_lake(tmp_path, n_trades=3))
        payload = client.get(f"/api/v1/oneclick/trades?run_id={RUN_ID}&rank=1").json()
        assert payload["status"] == "ok"
        assert payload["source"] == "results_store"
        assert payload["fixture_root_active"] is False
        assert payload["total"] == 3
        rows = payload["rows"]
        assert [row["trade_seq"] for row in rows] == [0, 1, 2]
        # Ruling 18: lots/costs pass through as honest null — never 1/0.
        assert rows[0]["lots"] is None
        assert rows[0]["costs_paisa"] is None
        # The additive granularity flag says which values mae/mfe carry.
        assert rows[0]["path_1m_status"] == "ok"
        assert rows[0]["mae_granularity"] == "1m"
        assert (rows[0]["mae"], rows[0]["mfe"]) == (40, 160)
        assert rows[1]["mae_granularity"] == "tf"
        assert (rows[1]["mae"], rows[1]["mfe"]) == (51, 151)
        assert rows[2]["path_1m_status"] == "no_1m_data"
        assert rows[2]["mae_granularity"] == "tf"

    def test_page_clamp_rides_the_trade_store_law(self, tmp_path: Path) -> None:
        """limit=1000 through the route clamps at PAGE_LIMIT_MAX=500, named."""
        from brutex.engine.trade_store import PAGE_LIMIT_MAX

        client = _client(_rebind_lake(tmp_path, n_trades=PAGE_LIMIT_MAX + 40))
        payload = client.get(f"/api/v1/oneclick/trades?run_id={RUN_ID}&rank=1&limit=1000").json()
        assert payload["status"] == "ok"
        assert len(payload["rows"]) == PAGE_LIMIT_MAX
        assert payload["total"] == PAGE_LIMIT_MAX + 40
        assert f"limit clamped to {PAGE_LIMIT_MAX}" in payload["detail"]
        tail = client.get(
            f"/api/v1/oneclick/trades?run_id={RUN_ID}&rank=1&offset={PAGE_LIMIT_MAX}"
        ).json()
        assert [row["trade_seq"] for row in tail.get("rows")] == list(
            range(PAGE_LIMIT_MAX, PAGE_LIMIT_MAX + 40)
        )

    def test_fixture_env_still_overrides_the_real_reader(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The explicit fixture override outranks the production rebind (loud badge)."""
        lake = _rebind_lake(tmp_path, n_trades=3)
        monkeypatch.setenv(oc.FIXTURE_ROOT_ENV, str(_FIXTURES))
        client = _client(lake)
        payload = client.get(f"/api/v1/oneclick/trades?run_id={RUN_ID}&rank=1").json()
        assert payload["source"] == "fixture"
        assert payload["fixture_root_active"] is True

    def test_verify_panel_reads_trades_wired_from_the_real_reader(self, tmp_path: Path) -> None:
        client = _client(_rebind_lake(tmp_path, n_trades=3))
        payload = client.get(f"/api/v1/oneclick/verify?run_id={RUN_ID}").json()
        checks = {check["id"]: check for check in payload["checks"]}
        assert checks[3]["state"] == "ready"  # operator-judged cap — never pass
        assert checks[4]["state"] == "ready"
