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
    """A tmp lake whose ``<lake>/sweeps/runs`` is the committed fixture corpus."""
    lake = tmp_path / "lake"
    shutil.copytree(_FIXTURES / "runs", lake / "sweeps" / "runs")
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

    def test_no_store_is_503(self) -> None:
        response = _client(None).get("/api/v1/oneclick/runs")
        assert response.status_code == 503

    def test_revision_outage_computes_fresh_uncached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T-M4: a ``runs_revision`` None (stat outage) takes the fresh branch."""
        client = _client(_lake_with_corpus(tmp_path))
        monkeypatch.setattr(oc, "runs_revision", lambda _root: None)
        payload = client.get("/api/v1/oneclick/runs").json()
        assert payload["status"] == "ok"
        assert {row["run_id"] for row in payload["runs"]} == ALL_RUN_IDS

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
        assert payload["rows"][0]["metrics"]["sharpe"] == 3.2  # passthrough, untouched

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
        """C-M5: no fixture root ⇒ AwaitingTradesReader — NEVER fabricated rows."""
        client = _client(_lake_with_corpus(tmp_path))
        payload = client.get(f"/api/v1/oneclick/trades?run_id={RUN_ID}&rank=1").json()
        assert payload["status"] == "awaiting"
        assert payload["rows"] == []
        assert payload["total"] is None
        assert payload["source"] == "none"
        assert payload["fixture_root_active"] is False
        assert "merge-integration" in payload["detail"]

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

    def test_tampered_chain_fails_with_the_break_line(self, tmp_path: Path) -> None:
        lake = _lake_with_corpus(tmp_path)
        events = lake / "sweeps" / "runs" / RUN_ID / "events.jsonl"
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
        manifest_path = lake / "sweeps" / "runs" / RUN_ID / "manifest.json"
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
