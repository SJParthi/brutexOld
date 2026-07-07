"""The ``/api/v1/universe/greeks-backfill`` routes (DEC-GREEKS-BULK-REENRICH-001).

Route glue via ``build_app`` + ``TestClient`` (the sibling
``test_dashboard_pull_historical_route`` pattern): the POST trigger (param
validation 400s, dry-run inertness, background ``_work`` body over offline
fakes — never a real lake scan), the single-flight refusal, the halt→error
state (one CRITICAL pull-halt alert through the job's existing handling), the
exactly-once completion Telegram, the O(1) progress route, and the
``universe.html`` frontend block (textContent-only — CodeQL XSS guard).
Skipped cleanly when the ``[ui]`` extra (FastAPI) or duckdb is absent.
"""

from __future__ import annotations

import importlib
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")
pytest.importorskip("trading_core", reason="the backfill module needs the trading_core wheel")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.errors import LakeWriteHalt
from brutex.data.greeks_backfill import (
    BackfillPlan,
    BackfillScan,
    GreeksBackfillReport,
    dry_run_backfill_report,
)
from brutex.data.greeks_enrich import GreeksEnrichTarget
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

app_module = importlib.import_module("brutex.dashboard.app")

_TARGET = GreeksEnrichTarget(
    exchange="NSE",
    symbol="NSE-NIFTY-25Jan24-21500-CE",
    start=date(2024, 1, 1),
    end=date(2024, 2, 2),
)
_SCAN = BackfillScan(targets=(_TARGET,), stale_v1=1, files_probed=1)
_PLAN = BackfillPlan(
    backfill_id="b" * 64, targets=(_TARGET,), scan=_SCAN, force=False, expired_only=False
)


def _report(**overrides: Any) -> GreeksBackfillReport:
    base = replace(
        dry_run_backfill_report(_PLAN),
        dry_run=False,
        batches=1,
        enriched=1,
        bars_written=3,
        note="Greeks backfill bbbbbbbbbbbb: 1 contract(s) re-stamped v2, 0 already current, "
        "0 errored (re-runnable); 3 bars written",
    )
    return replace(base, **overrides)


def _index(symbol: str, security_id: int) -> Instrument:
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


@pytest.fixture
def client(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[TestClient]:
    # Pin the job's single-flight flock root into the tmp lake so parallel test
    # workers never contend on a shared ~/.brutex path.
    monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(tmp_path))
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


def _patch_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, Any],
    *,
    run_result: GreeksBackfillReport | None = None,
    run_exc: Exception | None = None,
    gate: threading.Event | None = None,
) -> list[Any]:
    """Wire offline scan/plan/run fakes + capture the completion alert."""

    def _fake_scan(lake_root: Path, **kwargs: Any) -> BackfillScan:
        captured["scan_lake_root"] = lake_root
        captured["scan_kwargs"] = kwargs
        return _SCAN

    def _fake_plan(scan: BackfillScan, **kwargs: Any) -> BackfillPlan:
        captured["plan_force"] = kwargs["force"]
        captured["plan_expired_only"] = kwargs["expired_only"]
        return _PLAN

    def _fake_run(lake_root: Path, **kwargs: Any) -> GreeksBackfillReport:
        if gate is not None:
            gate.wait(timeout=10.0)
        captured["run_kwargs"] = kwargs
        if run_exc is not None:
            raise run_exc
        kwargs["on_progress"](1, 1, 3, 0, "contracts 1/1 (batch 1)")
        assert run_result is not None
        return run_result

    emitted: list[Any] = []
    monkeypatch.setattr(app_module, "scan_stale_greeks_targets", _fake_scan)
    monkeypatch.setattr(app_module, "plan_greeks_backfill", _fake_plan)
    monkeypatch.setattr(app_module, "run_greeks_backfill", _fake_run)
    monkeypatch.setattr(app_module, "emit_greeks_backfill_complete_alert", emitted.append)
    return emitted


def _wait_for_backfill(client: TestClient, *, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/v1/universe/greeks-backfill-progress").json()
        assert isinstance(snap, dict)
        if snap["state"] in {"done", "error"}:
            return snap
        time.sleep(0.01)
    raise AssertionError("background greeks backfill did not settle within the timeout")


class TestPostTrigger:
    def test_wet_run_end_to_end(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}
        report = _report()
        emitted = _patch_pipeline(monkeypatch, captured, run_result=report)
        resp = client.post(
            "/api/v1/universe/greeks-backfill",
            params={"force": "true", "expired_only": "true", "batch": "7", "limit": "2"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["started"] is True and body["dry_run"] is False
        assert body["force"] is True and body["expired_only"] is True
        assert body["batch"] == 7 and body["limit"] == 2
        snap = _wait_for_backfill(client)
        assert snap["state"] == "done"
        assert snap["detail"] == report.note  # the note IS the completion headline
        assert snap["total"] == 1 and snap["completed"] == 1 and snap["bars"] == 3
        assert emitted == [report]  # EXACTLY one completion summary
        assert captured["scan_kwargs"] == {
            "rate_source": captured["scan_kwargs"]["rate_source"],
            "expired_only": True,
            "force": True,
            "limit": 2,
        }
        assert captured["plan_force"] is True and captured["plan_expired_only"] is True
        assert captured["run_kwargs"]["batch_size"] == 7

    def test_dry_run_is_inert_no_run_no_alert(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        emitted = _patch_pipeline(monkeypatch, captured, run_result=_report())
        resp = client.post("/api/v1/universe/greeks-backfill", params={"dry_run": "true"})
        assert resp.status_code == 200 and resp.json()["dry_run"] is True
        snap = _wait_for_backfill(client)
        assert snap["state"] == "done"
        assert "DRY RUN" in snap["detail"]
        assert "run_kwargs" not in captured  # the runner never fired
        assert emitted == []  # zero Telegram on a dry-run

    def test_batch_below_one_is_400(self, client: TestClient) -> None:
        resp = client.post("/api/v1/universe/greeks-backfill", params={"batch": "0"})
        assert resp.status_code == 400
        assert "batch must be >= 1" in resp.json()["detail"]

    def test_limit_below_one_is_400(self, client: TestClient) -> None:
        resp = client.post("/api/v1/universe/greeks-backfill", params={"limit": "-3"})
        assert resp.status_code == 400
        assert "limit must be >= 1" in resp.json()["detail"]

    def test_second_trigger_is_refused_single_flight(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gate = threading.Event()
        _patch_pipeline(monkeypatch, {}, run_result=_report(), gate=gate)
        try:
            first = client.post("/api/v1/universe/greeks-backfill")
            assert first.json()["started"] is True
            second = client.post("/api/v1/universe/greeks-backfill")
            assert second.json()["started"] is False  # single-flight refusal
        finally:
            gate.set()
        assert _wait_for_backfill(client)["state"] == "done"

    def test_halt_lands_as_error_state_with_one_pull_halt_alert(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        halted: list[Exception] = []
        monkeypatch.setattr("brutex.dashboard.pull_jobs.emit_pull_halt_alert", halted.append)
        halt = LakeWriteHalt("disk_full")
        emitted = _patch_pipeline(monkeypatch, {}, run_exc=halt)
        resp = client.post("/api/v1/universe/greeks-backfill")
        assert resp.json()["started"] is True
        snap = _wait_for_backfill(client)
        assert snap["state"] == "error"
        assert "LakeWriteHalt" in snap["detail"]
        assert halted == [halt]  # the job's existing is_pull_halt handling fired ONCE
        assert emitted == []  # no completion summary on a halted run


class TestProgressRoute:
    def test_idle_snapshot_shape(self, client: TestClient) -> None:
        snap = client.get("/api/v1/universe/greeks-backfill-progress").json()
        assert snap["state"] == "idle"
        for key in ("total", "completed", "failed", "bars", "current", "detail", "percent"):
            assert key in snap


def _universe_template() -> str:
    module_file = app_module.__file__
    assert module_file is not None
    return (Path(module_file).parent / "templates" / "universe.html").read_text(encoding="utf-8")


class TestFrontend:
    def test_universe_template_carries_the_backfill_block(self) -> None:
        template = _universe_template()
        assert 'id="greeksBackfillBtn"' in template
        assert 'id="gbDry"' in template
        assert "/api/v1/universe/greeks-backfill-progress" in template
        assert "startGreeksBackfill" in template

    def test_backfill_js_writes_via_textcontent_only(self) -> None:
        """The new block's one text sink is gbStatus (textContent) — never innerHTML."""
        template = _universe_template()
        start = template.index("function gbStatus")
        end = template.index("const verdictRefreshBtn")
        block = template[start:end]
        assert "textContent" in block
        assert "innerHTML" not in block  # CodeQL XSS guard
