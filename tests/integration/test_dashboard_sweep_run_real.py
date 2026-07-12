"""REAL-path proof of the one-click dashboard sweep (DEC-SWEEP-JOB-LOCK-POSTURE-001).

The FAILS-BEFORE regression net for the sweep-job lock-posture fix
(MIGRATION.md #1770): on unfixed main the ``/api/v1/sweep/run`` background job
held the lake PULL-WRITER ``flock`` ``LOCK_EX`` for its whole duration
(``BackgroundPullJob.start`` default ``cross_process_lock=True``), then
``cmd_sweep_all``'s G10 ``lake_read_guard`` tried ``LOCK_SH | LOCK_NB`` on the
SAME lock file from a SECOND fd — which fails against the job's own held EX —
so every REAL one-click sweep died ~1 s after the click with
:class:`~brutex.engine.sweep_singleflight.LakeBusyError` ("a pull/heal is
running against this lake"). CI stayed green because every route test faked
``run_sweep_from_request`` (the fixture blindspot this file closes — the #1445
round-3 "the run-core covers the button" claim was fixture-blind).

This suite drives the REAL chain end-to-end — ``build_app`` + ``TestClient`` →
``POST /api/v1/sweep/run`` → ``BackgroundPullJob`` → the REAL
``run_sweep_from_request`` → ``cmd_sweep_all`` → ``sweep_run_core`` — over a
tiny REAL lake (real ``ParquetWriter`` bars + catalog + DuckDB instrument
store). ``run_sweep_from_request`` is deliberately NOT monkeypatched anywhere
in this file.

Exclusion proofs kept honest (nothing weakened by the fix):

* a REAL pull/heal holding the lake EX lock still refuses the sweep (the G10
  SH guard raises ``LakeBusyError`` — pull-then-sweep direction);
* while the sweep-kind job runs, a second job start is still refused by the
  in-process single-flight (pinned at the unit level in
  ``tests/unit/test_pull_jobs_singleflight.py`` alongside the writer-guard
  posture pins).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("polars")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")
pytest.importorskip("lmdb")

from brutex.adapters.groww.pull_singleflight import pull_singleflight_lock
from brutex.dashboard.app import DashboardState, build_app
from brutex.data.catalog import Catalog
from brutex.data.instrument_store import InstrumentStore
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import ParquetWriter
from brutex.data.sink import ParquetBarSink
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

_IST = timezone(timedelta(hours=5, minutes=30))
_EXCHANGE = "NSE"
_SEGMENT = "INDEX"
_INTERVAL = "1minute"

#: The exact form params the Operator page's Run button could send for a tiny
#: real run — one symbol, one interval, one direction, k=1 over the seeded day.
_TINY_SWEEP_PARAMS: dict[str, str] = {
    "symbols": "NIFTY",
    "intervals": _INTERVAL,
    "directions": "BUY",
    "k": "1",
    "start": "2024-01-02",
    "end": "2024-01-03",
    "top": "5",
}


@pytest.fixture(autouse=True)
def _sequential_sweep_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the sequential sweep path explicitly (the sweep-CLI suite precedent)."""
    monkeypatch.setenv("BRUTEX_SWEEP_WORKERS", "1")


def _bars() -> list[CandleBar]:
    """A short in-session minute window (stored UTC), valid OHLC."""
    out: list[CandleBar] = []
    for i in range(8):
        ts = datetime(2024, 1, 2, 9, 16 + i, tzinfo=_IST).astimezone(UTC)
        out.append(
            CandleBar(
                timestamp=ts,
                open=Decimal("100.00"),
                high=Decimal("102.00"),
                low=Decimal("99.00"),
                close=Decimal("100.00") + Decimal(i) / 10,
                volume=1000,
                open_interest=None,
            )
        )
    return out


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


@pytest.fixture
def lake_root(tmp_path: Path) -> Path:
    """A tiny REAL lake at *tmp_path*: NIFTY 1-minute bars + the catalog.

    The sweep route derives the lake root as ``store.path.parent``, so the
    bars/catalog live at ``tmp_path`` itself — exactly where ``cmd_sweep_all``
    (``catalog_path=None`` → ``<lake>/catalog.duckdb``) will look.
    """
    writer = ParquetWriter(lake_root=tmp_path)
    with Catalog.open(tmp_path / "catalog.duckdb") as cat:
        sink = ParquetBarSink(writer=writer, catalog=cat)
        sink.append(
            exchange=_EXCHANGE,
            segment=_SEGMENT,
            symbol="NIFTY",
            interval=_INTERVAL,
            bars=_bars(),
        )
    return tmp_path


@pytest.fixture
def client(lake_root: Path) -> Iterator[TestClient]:
    store = InstrumentStore.open(lake_root / "instruments.duckdb")
    store.upsert_many(
        [_nifty()], snapshot_date=date(2024, 1, 2), run_id="r1", csv_source_sha256="0" * 64
    )
    try:
        with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
            yield c
    finally:
        store.close()


def _wait_for_job(client: TestClient, *, timeout: float = 300.0) -> dict[str, Any]:
    """Poll the shared job's progress feed until it settles (done/error)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert isinstance(snap, dict)
        if snap["state"] in {"done", "error"}:
            return snap
        time.sleep(0.05)
    raise AssertionError("the background sweep job did not settle within the timeout")


def test_one_click_sweep_runs_to_completion_on_the_real_path(
    client: TestClient, lake_root: Path
) -> None:
    """The REAL one-click sweep completes — the permanent fails-before regression.

    On unfixed main this job dies ~1 s in with
    ``LakeBusyError: a pull/heal is running against this lake …`` because the
    sweep-kind job itself held the pull EX flock its own G10 SH read-guard then
    contended with (the self-deadlock DEC-SWEEP-JOB-LOCK-POSTURE-001 fixes).
    """
    resp = client.post("/api/v1/sweep/run", params=_TINY_SWEEP_PARAMS)
    assert resp.status_code == 200
    assert resp.json()["started"] is True

    snap = _wait_for_job(client)
    # The exact pre-fix failure shape, asserted absent by name (Golden Rule #10).
    assert "LakeBusyError" not in snap.get("detail", "")
    assert snap["state"] == "done", f"real sweep failed: {snap.get('detail', '')!r}"

    # A REAL artifact landed: at least one per-tuple results store under the
    # route-derived <lake>/sweeps root plus the live progress file
    # cmd_sweep_all publishes (the route pointed /api/v1/sweep-progress at it).
    sweeps = lake_root / "sweeps"
    assert any(sweeps.rglob("*.parquet")), "no per-tuple results store was written"
    assert (sweeps / "progress" / "sweep-progress.json").exists()


def test_sweep_still_refused_while_a_real_pull_holds_the_lake_ex_lock(
    client: TestClient, lake_root: Path
) -> None:
    """Exclusion preserved (pull-then-sweep): a held pull EX lock refuses the sweep.

    Simulates a concurrent pull/heal by holding the REAL
    ``pull_singleflight_lock`` (a separate fd — flock is per open file
    description, so this is exactly what a peer CLI ``brutex fetch`` holds).
    The sweep job must error LOUDLY with the G10 ``LakeBusyError`` refusal —
    the fix removes the job's SELF-conflict, never the real reader-vs-writer
    exclusion.
    """
    with pull_singleflight_lock(lake_root):
        resp = client.post("/api/v1/sweep/run", params=_TINY_SWEEP_PARAMS)
        assert resp.status_code == 200
        assert resp.json()["started"] is True
        snap = _wait_for_job(client)
    assert snap["state"] == "error"
    assert "LakeBusyError" in snap["detail"]
    assert "a pull/heal is running against this lake" in snap["detail"]
