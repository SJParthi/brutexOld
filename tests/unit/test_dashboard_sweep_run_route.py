"""The ``/api/v1/sweep/run`` route (DEC-SWEEP-RUN-BUTTON-001).

Covers the route glue — the happy-path start (defaults + custom params), the
400 validation arms (delegated to the pure resolver), the "no lake" guard, the
single-flight no-op, and the /reports page wiring. The background ``_work`` body
(which calls ``run_sweep_from_request`` on the job thread) is replaced by an
offline fake — NEVER a real lake/sweep. Mirrors
``test_dashboard_pull_derivatives_route.py``: a real DuckDB-backed store + a
``TestClient``. Skipped cleanly when the ``[ui]`` extra (FastAPI) or duckdb is
absent.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

app_module = importlib.import_module("brutex.dashboard.app")


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


_CAPTURED: dict[str, Any] = {}


def _fake_run_sweep_from_request(
    request: Any,
    *,
    lake_root: Path,
    results_root: Path,
    on_summary: Any = None,
) -> int:
    """Offline stand-in for the grid sweep (no lake, returns at once).

    Accepts the additive ``on_summary`` seam (DEC-SWEEP-ACCEPTANCE-LIFECYCLE-001 /
    G5b) so the dashboard job's ``_work`` capture-and-note path drives cleanly; a
    clean run reports no summary, so the finish note stays "done".
    """
    _CAPTURED["request"] = request
    _CAPTURED["lake_root"] = lake_root
    _CAPTURED["results_root"] = results_root
    _CAPTURED["on_summary"] = on_summary
    return 0


def _wait_for_job(client: TestClient, *, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert isinstance(snap, dict)
        if snap["state"] in {"done", "error"}:
            return snap
        time.sleep(0.01)
    raise AssertionError("background sweep job did not settle within the timeout")


# ── happy paths ───────────────────────────────────────────────────────────────


def test_default_params_start_job(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _CAPTURED.clear()
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    resp = client.post("/api/v1/sweep/run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is True
    assert body["symbols"] == "NIFTY,BANKNIFTY"
    assert body["intervals"] == "1minute,5minute,15minute"
    assert body["start"] == "2020-01-01"
    assert body["exchange"] == "NSE"
    assert body["segment"] == "INDEX"
    assert _wait_for_job(client)["state"] == "done"
    # The background body received the resolved request + the lake-relative roots.
    assert _CAPTURED["request"].symbols == "NIFTY,BANKNIFTY"
    assert _CAPTURED["results_root"] == _CAPTURED["lake_root"] / "sweeps"
    # A clean run captures no summary → the finish note stays "done".
    assert _wait_for_job(client)["detail"] == "done"


def test_failed_tuples_surface_in_the_finish_detail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G5b: a sweep that SKIPPED failed tuples renders an honest finish note.

    The job stays "done" (the results ARE on disk — a warn-flavoured completion
    is more honest than a failure), but the finish detail now NAMES the skipped
    count instead of a bare "done" — driven by the ``on_summary`` capture in the
    dashboard ``_work``. The CLI/cron int exit code is unchanged upstream.
    """
    from brutex.engine.sweep_supervisor import SupervisedGridSummary

    def _fake_with_failures(
        request: Any, *, lake_root: Path, results_root: Path, on_summary: Any = None
    ) -> int:
        summary = SupervisedGridSummary(
            sweep_id="x",
            tuples_total=5,
            tuples_run=3,
            tuples_skipped_no_bars=0,
            skipped_cache=0,
            skipped_resume=0,
            enumerated=10,
            conflict_pruned=0,
            skipped_dedup=0,
            evaluated=10,
            ranked=3,
            persisted=3,
            tuples_failed=2,
            tuples_quarantined_prior=1,
        )
        if on_summary is not None:
            on_summary(summary)
        return 4  # sweep_all_exit_code(tuples_failed>0) — a non-zero cron-red exit

    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_with_failures)
    resp = client.post("/api/v1/sweep/run")
    assert resp.status_code == 200
    snap = _wait_for_job(client)
    assert snap["state"] == "done"  # results on disk — not a failure
    assert "2 tuple(s) FAILED" in snap["detail"]
    assert "1 tuple(s) quarantined from a prior run" in snap["detail"]


def test_custom_params_are_echoed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    resp = client.post(
        "/api/v1/sweep/run",
        params={
            "symbols": "NIFTY",
            "intervals": "5minute",
            "directions": "BUY",
            "k": "2",
            "start": "2022-01-01",
            "end": "2022-12-31",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbols"] == "NIFTY"
    assert body["intervals"] == "5minute"
    assert body["directions"] == "BUY"
    assert body["k"] == "2"
    assert body["start"] == "2022-01-01"
    assert body["end"] == "2022-12-31"
    assert _wait_for_job(client)["state"] == "done"


def test_progress_path_points_at_the_sweep_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a start, GET /api/v1/sweep-progress is served (idle until the file lands)."""
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    assert client.post("/api/v1/sweep/run").status_code == 200
    _wait_for_job(client)
    prog = client.get("/api/v1/sweep-progress").json()
    assert isinstance(prog, dict)
    assert "state" in prog  # the offline fake writes no file ⇒ idle snapshot


# ── 400 arms (delegated to the pure resolver) ─────────────────────────────────


def test_bad_interval_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    resp = client.post("/api/v1/sweep/run", params={"intervals": "7tick"})
    assert resp.status_code == 400
    assert "interval must be one of" in resp.json()["detail"]


def test_out_of_scope_symbol_400_with_the_scope_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEC-ENGINE-SCOPE-GUARD-001: an out-of-engine-scope dashboard launch is a 400.

    Route-level (the route tests fake ``run_sweep_from_request``, so the guard
    must be proven at the ROUTE, not just the pure resolver): the refusal
    carries the law citation, and NO sweep job is started.
    """
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    resp = client.post("/api/v1/sweep/run", params={"symbols": "RELIANCE"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "sweep REFUSED" in detail
    assert "RELIANCE" in detail
    assert "CLAUDE.md §2 Rule 3" in detail


def test_reference_only_vix_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    resp = client.post("/api/v1/sweep/run", params={"symbols": "INDIAVIX"})
    assert resp.status_code == 400
    assert "REFERENCE_ONLY" in resp.json()["detail"]


def test_route_has_no_scope_override_parameter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No dashboard override exists by design — an override-shaped param is inert."""
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    resp = client.post(
        "/api/v1/sweep/run",
        params={"symbols": "RELIANCE", "allow_out_of_scope": "true"},
    )
    assert resp.status_code == 400  # still refused — the param is not a route input


def test_end_before_start_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    resp = client.post("/api/v1/sweep/run", params={"start": "2024-01-01", "end": "2023-01-01"})
    assert resp.status_code == 400
    assert "must not precede start" in resp.json()["detail"]


def test_out_of_scope_400_carries_the_corp_action_rationale(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 400 detail carries the corporate-action rationale + both DEC ids.

    DEC-CORP-ACTION-GATE-001 / ADR-0016 — the extension of the
    DEC-ENGINE-SCOPE-GUARD-001 eager 400 proven above: the operator sees WHY
    an out-of-scope launch is dangerous (no corporate-action adjustment
    layer exists), not just that it is refused.
    """
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    resp = client.post("/api/v1/sweep/run", params={"symbols": "NIFTY,RELIANCE"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "corporate-action adjustment layer missing" in detail
    # repr-quoted (round-4 finding 3)
    assert "- 'RELIANCE':" in detail
    assert "DEC-CORP-ACTION-GATE-001" in detail
    assert "ADR-0016" in detail


# ── single-flight + no-lake guards ────────────────────────────────────────────


def test_no_lake_configured_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store with no on-disk path can't be swept → a clear 400 (not a crash)."""
    fake_store = SimpleNamespace(path=None)
    with TestClient(build_app(DashboardState(), instrument_store=fake_store)) as c:
        resp = c.post("/api/v1/sweep/run")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "no lake configured for sweep"


def test_second_click_while_running_is_a_noop(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While a sweep (or pull) holds the single-flight job, a second start no-ops."""
    started = __import__("threading").Event()
    release = __import__("threading").Event()

    def _blocking(
        request: Any, *, lake_root: Path, results_root: Path, on_summary: Any = None
    ) -> int:
        started.set()
        release.wait(timeout=10.0)
        return 0

    monkeypatch.setattr(app_module, "run_sweep_from_request", _blocking)
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        first = c.post("/api/v1/sweep/run")
        assert first.status_code == 200
        assert first.json()["started"] is True
        assert started.wait(timeout=10.0)
        second = c.post("/api/v1/sweep/run")
        assert second.status_code == 200
        assert second.json()["started"] is False  # single-flight no-op
        release.set()


def test_route_starts_job_with_cross_process_lock_false(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DEC-SWEEP-LOCK-PULL-CONCURRENCY-001 — the route wiring pin (fails before).

    The sweep route must start its job with ``cross_process_lock=False``:
    ``cmd_sweep_all`` owns the cross-process exclusion itself (the G10 lake
    read-guard ``LOCK_SH`` across the bar PRELOAD only + the G6 sweep
    single-flight). BEFORE the fix the route left the default ``True``, so the
    job held the lake pull flock EX for the whole run and cmd_sweep_all's own
    G10 SH acquire on the SAME file SELF-CONFLICTED (flock is per open file
    description) — every dashboard sweep died at preload with ``LakeBusyError``
    in its progress feed (dead-on-arrival since G10 landed, commit 3f32b3e),
    invisible to CI because these route tests fake ``run_sweep_from_request``.
    The kwargs spy is the route-level pin; the REAL lock composition is proven
    in ``test_pull_jobs_singleflight.py`` +
    ``tests/integration/test_sweep_pull_concurrency.py``.
    """
    from brutex.dashboard.pull_jobs import JOB_KIND_SWEEP, BackgroundPullJob

    _CAPTURED.clear()
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    captured_kwargs: dict[str, Any] = {}
    real_start = BackgroundPullJob.start

    def spy_start(self: BackgroundPullJob, work: Any, **kwargs: Any) -> bool:
        captured_kwargs.update(kwargs)
        return real_start(self, work, **kwargs)

    monkeypatch.setattr(BackgroundPullJob, "start", spy_start)
    resp = client.post("/api/v1/sweep/run")
    assert resp.status_code == 200
    assert resp.json()["started"] is True
    # `.get(...)` deliberately: on the pre-fix route the kwarg is ABSENT, so
    # the pin fails as a clean `None is not False` assertion (not an opaque
    # KeyError). The route's start() call is SYNCHRONOUS inside the handler —
    # `started: True` in the response proves the (spied) call already ran, so
    # there is nothing to wait for here.
    assert captured_kwargs.get("cross_process_lock") is False  # THE pin
    assert captured_kwargs.get("job_kind") == JOB_KIND_SWEEP
    assert _wait_for_job(client)["state"] == "done"


# ── page wiring ───────────────────────────────────────────────────────────────


def test_operator_page_carries_sweep_button(client: TestClient) -> None:
    """DEC-OPERATOR-PAGE-REAL-001 / R25: the ONE canonical Run button lives on
    the Operator page (/); /reports returned to the pre-open report ONLY."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "/api/v1/sweep/run" in resp.text
    assert 'id="run-sweep"' in resp.text
    assert 'id="swSymbols"' in resp.text
    assert "Run Sweep" in resp.text
    reports = client.get("/reports")
    assert reports.status_code == 200
    assert 'id="run-sweep"' not in reports.text
    assert 'id="run-cascade"' not in reports.text
