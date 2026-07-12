# pyright: reportPrivateUsage=none
"""Dashboard cross-verify trigger (DEC-CROSSVERIFY-PRODUCER-001).

Route glue via ``build_app`` + ``TestClient`` (the greeks-backfill-route
template): ``POST /api/v1/crossverify/run`` starts the SAME runner the CLI
uses on a dedicated ``BackgroundPullJob`` — started with
``cross_process_lock=False`` (the runner's own ``default_seams`` lock seam
takes the account→lake→lease trio; a job-level second flock acquire would
self-conflict) and ``market_hours_exempt=True`` (the runner owns the
market-hours law at its step E). ``GET /api/v1/crossverify/progress`` is the
O(1) job snapshot. Tests inject ``crossverify_runner`` (the build_app seam)
so no real machinery / locks / vendor calls ever run; the ``_work`` body's
verdict surfacing (done note on exit 0, honest ✕ error on exit != 0) is
executed for real on the job thread. Plus the ``universe.html`` panel pins
(button / dry-run checkbox / status span / both endpoints / textContent-only
discipline) with a ``node --check`` gate over the template's inline scripts.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 — fixed-argv node invocation, test-only
import threading
import time
from collections.abc import Iterator
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from brutex.crossverify.config import CrossverifyConfig
from brutex.crossverify.run import CrossverifyReport
from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

_TEMPLATES = Path(__file__).resolve().parents[2] / "brutex" / "dashboard" / "templates"
_NODE = shutil.which("node")


def _report(**overrides: object) -> CrossverifyReport:
    import dataclasses

    base = CrossverifyReport(
        day=date(2026, 7, 10),
        run_id="cv-20260710-161501-1",
        verdict="published",
        published=3,
        skipped_already=1,
        withheld=0,
        blocked=0,
        nothing=2,
        excluded=0,
        publish_failed=0,
        recovered_prior=0,
        derived=4,
        derive_failed=0,
        movers_warm="revision-propagated (headless)",
        detail="",
        exit_code=0,
    )
    return dataclasses.replace(base, **overrides) if overrides else base  # type: ignore[arg-type]


def _index(symbol: str, security_id: int) -> Instrument:
    from decimal import Decimal

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


@pytest.fixture
def store(tmp_path: Path) -> Iterator[InstrumentStore]:
    instrument_store = InstrumentStore.open(tmp_path / "instruments.duckdb")
    instrument_store.upsert_many(
        [_index("NIFTY", 13)],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield instrument_store
    instrument_store.close()


def _client(
    store: InstrumentStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner: Any,
) -> TestClient:
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(tmp_path))
    return TestClient(
        build_app(DashboardState(), instrument_store=store, crossverify_runner=runner)
    )


def _wait_terminal(client: TestClient, *, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/v1/crossverify/progress").json()
        assert isinstance(snap, dict)
        if snap["state"] in {"done", "error"}:
            return snap
        time.sleep(0.01)
    raise AssertionError("background crossverify run did not settle within the timeout")


class TestRunRoute:
    def test_dry_run_end_to_end_threads_config_and_surfaces_verdict(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen: dict[str, Any] = {}

        def runner(config: CrossverifyConfig) -> CrossverifyReport:
            seen["config"] = config
            return _report(verdict="staged_only", detail="dry-run — rendered in memory")

        with _client(store, monkeypatch, tmp_path, runner) as client:
            resp = client.post(
                "/api/v1/crossverify/run?dry_run=true&day=2026-07-10&no_publish=true"
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["started"] is True
            assert body["dry_run"] is True
            assert body["day"] == "2026-07-10"
            assert "brutex crossverify run" in body["note"]
            snap = _wait_terminal(client)
        assert snap["state"] == "done"
        assert "staged_only" in snap["detail"]
        assert "dry-run — rendered in memory" in snap["detail"]
        config = seen["config"]
        assert config.day == date(2026, 7, 10)
        assert config.dry_run is True
        assert config.skip_pull is False
        assert config.publish is False  # --no-publish threaded

    def test_nonzero_exit_verdict_reads_as_an_honest_error(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def runner(config: CrossverifyConfig) -> CrossverifyReport:
            return _report(verdict="blocked", blocked=1, exit_code=2, detail="NIFTY no_today_bars")

        with _client(store, monkeypatch, tmp_path, runner) as client:
            assert client.post("/api/v1/crossverify/run").json()["started"] is True
            snap = _wait_terminal(client)
        assert snap["state"] == "error"
        assert "blocked" in snap["detail"]
        assert "NIFTY no_today_bars" in snap["detail"]

    def test_junk_day_is_400(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        with _client(store, monkeypatch, tmp_path, lambda config: _report()) as client:
            resp = client.post("/api/v1/crossverify/run?day=tomorrow")
        assert resp.status_code == 400
        assert "day must be YYYY-MM-DD" in resp.json()["detail"]

    def test_junk_env_is_an_eager_400_never_a_job_thread_error(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("BRUTEX_CROSSVERIFY", "banana")
        with _client(store, monkeypatch, tmp_path, lambda config: _report()) as client:
            resp = client.post("/api/v1/crossverify/run")
        assert resp.status_code == 400
        assert "BRUTEX_CROSSVERIFY" in resp.json()["detail"]

    def test_junk_market_hours_env_is_an_eager_400_for_vendor_calling_runs(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Round-2 S3d: the market-hours guard env the runner's seams resolve
        # is validated EAGERLY in the route (the CLI's eager default_seams
        # validation, mirrored) — junk → 400, never a job-thread error.
        monkeypatch.setenv("BRUTEX_MARKET_HOURS_GUARD", "banana")
        with _client(store, monkeypatch, tmp_path, lambda config: _report()) as client:
            resp = client.post("/api/v1/crossverify/run")
        assert resp.status_code == 400
        assert "BRUTEX_MARKET_HOURS_GUARD" in resp.json()["detail"]

    def test_junk_market_hours_env_never_blocks_a_dry_run(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A dry-run makes no vendor calls — the seam threads None, so the
        # route mirrors that gate and starts the job (seam parity).
        monkeypatch.setenv("BRUTEX_MARKET_HOURS_GUARD", "banana")

        def runner(config: CrossverifyConfig) -> CrossverifyReport:
            return _report(verdict="staged_only", detail="dry-run")

        with _client(store, monkeypatch, tmp_path, runner) as client:
            resp = client.post("/api/v1/crossverify/run?dry_run=true")
            assert resp.status_code == 200
            assert resp.json()["started"] is True
            snap = _wait_terminal(client)
        assert snap["state"] == "done"

    def test_second_click_is_refused_by_the_in_process_single_flight(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        gate = threading.Event()

        def runner(config: CrossverifyConfig) -> CrossverifyReport:
            gate.wait(timeout=10.0)
            return _report()

        with _client(store, monkeypatch, tmp_path, runner) as client:
            assert client.post("/api/v1/crossverify/run").json()["started"] is True
            second = client.post("/api/v1/crossverify/run").json()
            assert second["started"] is False
            gate.set()
            snap = _wait_terminal(client)
        assert snap["state"] == "done"

    def test_progress_route_is_an_idle_snapshot_before_any_run(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        with _client(store, monkeypatch, tmp_path, lambda config: _report()) as client:
            snap = client.get("/api/v1/crossverify/progress").json()
        assert snap["state"] == "idle"

    def test_market_hours_window_never_defers_the_job_start(
        self, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The job-level market-hours gate is exempted — the RUNNER owns the
        law at its step E (a wrongly-deferring job start would kill the
        post-close button whenever the guard misfires; the runner's verdict
        is the honest surface). The injected runner returning the deferral
        verdict proves the round trip."""
        monkeypatch.setenv("BRUTEX_MARKET_HOURS_GUARD", "1")

        def runner(config: CrossverifyConfig) -> CrossverifyReport:
            return _report(
                verdict="deferred_market_hours",
                published=0,
                derived=0,
                exit_code=0,
                detail="market-hours window — deferred",
            )

        with _client(store, monkeypatch, tmp_path, runner) as client:
            body = client.post("/api/v1/crossverify/run").json()
            assert body["started"] is True  # never the job-level deferral refusal
            snap = _wait_terminal(client)
        assert snap["state"] == "done"
        assert "deferred_market_hours" in snap["detail"]


# ── universe.html panel pins ─────────────────────────────────────────────


def _tpl() -> str:
    text = (_TEMPLATES / "universe.html").read_text(encoding="utf-8")
    assert text
    return text


class TestUniversePanel:
    def test_panel_button_checkbox_and_status_span_exist(self) -> None:
        text = _tpl()
        assert 'id="crossverifyBtn"' in text
        assert 'id="cvDry"' in text
        assert 'id="cvstatus"' in text
        assert "Cross-verify → TickVault" in text

    def test_both_endpoints_are_wired(self) -> None:
        text = _tpl()
        assert "/api/v1/crossverify/run" in text
        assert "/api/v1/crossverify/progress" in text

    def test_status_writes_are_textcontent_only(self) -> None:
        """The cv status sink writes via textContent — never innerHTML."""
        text = _tpl()
        start = text.index("function cvStatus")
        block = text[start : text.index("}", start)]
        assert "textContent" in block
        assert "innerHTML" not in block

    def test_watcher_rides_the_common_gbfetchjson_helper(self) -> None:
        """No third copy of the hung-fetch abort boilerplate (CLAUDE.md §11)."""
        text = _tpl()
        start = text.index("── Cross-verify producer (DEC-CROSSVERIFY-PRODUCER-001)")
        tail = text[start:]
        assert "gbFetchJson('/api/v1/crossverify/progress')" in tail
        assert "new AbortController" not in tail  # only gbFetchJson owns that

    def test_greeks_na_disclosure_present(self) -> None:
        """Tonight-scope addendum item 3: the options-only Greeks stage has
        nothing to enrich on this options-excluding pull — said in-template."""
        assert "GREEKS N/A" in _tpl()


class _InlineScriptExtractor(HTMLParser):
    """Collect every src-less inline ``<script>`` body (the frontend-latency
    test's stdlib-parser idiom — never a tag regex)."""

    def __init__(self) -> None:
        super().__init__()
        self.bodies: list[str] = []
        self._in_inline_script = False
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._in_inline_script = all(name != "src" for name, _value in attrs)
            self._chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_inline_script:
            self.bodies.append("".join(self._chunks))
            self._in_inline_script = False
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._in_inline_script:
            self._chunks.append(data)


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_universe_inline_scripts_parse_under_node_check(tmp_path: Path) -> None:
    """Every src-less inline ``<script>`` block of universe.html parses."""
    parser = _InlineScriptExtractor()
    parser.feed(_tpl())
    parser.close()
    blocks = [body for body in parser.bodies if body.strip()]
    assert blocks, "universe.html: expected at least one inline script block"
    for i, body in enumerate(blocks):
        target = tmp_path / f"universe.{i}.js"
        target.write_text(body, encoding="utf-8")
        proc = subprocess.run(  # nosec B603 — fixed argv, test-only
            [str(_NODE), "--check", str(target)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, (
            f"universe.html inline script block #{i} failed node --check: {proc.stderr}"
        )
