"""The ONE-CLICK institutional sweep routes (R4/R20/R5, gap-hunt rulings 1/2/9).

``GET /api/v1/sweep/oneclick/preflight`` (estimate-only, never launches) and
``POST /api/v1/sweep/oneclick/run`` (preflight-gated launch, multi-exchange
fan-out). Mirrors ``test_dashboard_sweep_run_route.py``'s fixtures + offline
``run_sweep_from_request`` fake — never a real lake/sweep.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi", reason="dashboard routes need the [ui] extra")
pytest.importorskip("duckdb", reason="the wired-store routes need duckdb")

from brutex.dashboard.app import DashboardState, build_app
from brutex.data.instrument_store import InstrumentStore
from brutex.models import Exchange, Instrument, InstrumentType, Segment
from fastapi.testclient import TestClient

app_module = importlib.import_module("brutex.dashboard.app")


def _index_instrument(symbol: str, exch: Exchange) -> Instrument:
    return Instrument(
        exch_id=exch,
        security_id=13,
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
        [_index_instrument("NIFTY", Exchange.NSE)],
        snapshot_date=date(2024, 1, 2),
        run_id="r1",
        csv_source_sha256="0" * 64,
    )
    yield instrument_store
    instrument_store.close()


@pytest.fixture
def client(store: InstrumentStore) -> Iterator[TestClient]:
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        yield c


_CAPTURED: dict[str, Any] = {}


def _fake_run_sweep_from_request(request: Any, *, lake_root: Path, results_root: Path) -> int:
    _CAPTURED.setdefault("requests", []).append(request)
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


# ── GET /api/v1/sweep/oneclick/preflight ─────────────────────────────────────


def test_preflight_route_never_launches(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    _CAPTURED.clear()
    resp = client.get("/api/v1/sweep/oneclick/preflight")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"sweep_run_id", "ok", "refusals", "clamps", "estimator"}
    assert "requests" not in _CAPTURED  # nothing launched


def test_preflight_route_reports_estimator(client: TestClient) -> None:
    resp = client.get("/api/v1/sweep/oneclick/preflight", params={"symbols": "NIFTY"})
    assert resp.status_code == 200
    estimator = resp.json()["estimator"]
    assert estimator["trials"] > 0
    assert estimator["estimate_basis"] == "provisional"


def test_preflight_route_mints_a_fresh_run_id_by_default(client: TestClient) -> None:
    """Round-9 adversarial-refuter finding: without a caller-supplied
    ``run_id``, two independent preflight calls (or a preflight followed by
    a launch) each mint their OWN fresh id — the default, pre-existing
    behavior, preserved byte-for-byte when the new ``run_id`` param is
    omitted."""
    first = client.get("/api/v1/sweep/oneclick/preflight", params={"symbols": "NIFTY"})
    second = client.get("/api/v1/sweep/oneclick/preflight", params={"symbols": "NIFTY"})
    assert first.json()["sweep_run_id"] != second.json()["sweep_run_id"]


def test_preflight_route_honors_a_caller_supplied_run_id(client: TestClient) -> None:
    """Round-9 adversarial-refuter finding: a future UI that wants the id
    shown at preflight time to be THE id an eventual launch uses must be
    able to mint one client-side and pass it to both calls."""
    resp = client.get(
        "/api/v1/sweep/oneclick/preflight",
        params={"symbols": "NIFTY", "run_id": "operator-chosen-id-123"},
    )
    assert resp.status_code == 200
    assert resp.json()["sweep_run_id"] == "operator-chosen-id-123"


def test_preflight_route_bad_interval_400(client: TestClient) -> None:
    resp = client.get("/api/v1/sweep/oneclick/preflight", params={"intervals": "7tick"})
    assert resp.status_code == 400
    assert "interval must be one of" in resp.json()["detail"]


def test_preflight_route_out_of_scope_symbol_400(client: TestClient) -> None:
    resp = client.get("/api/v1/sweep/oneclick/preflight", params={"symbols": "RELIANCE"})
    assert resp.status_code == 400
    assert "sweep REFUSED" in resp.json()["detail"]


def test_preflight_route_no_lake_configured_400() -> None:
    from types import SimpleNamespace

    fake_store = SimpleNamespace(path=None)
    with TestClient(build_app(DashboardState(), instrument_store=fake_store)) as c:
        resp = c.get("/api/v1/sweep/oneclick/preflight")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "no lake configured for sweep"


def test_preflight_route_multi_exchange_selection_reports_both_groups(client: TestClient) -> None:
    resp = client.get("/api/v1/sweep/oneclick/preflight", params={"symbols": "NIFTY,SENSEX"})
    assert resp.status_code == 200
    body = resp.json()
    # Both NSE (NIFTY) and BSE (SENSEX) coverage gaps are named — one logical verdict.
    symbols_named = {r["symbol"] for r in body["refusals"]}
    assert "NIFTY" in symbols_named
    assert "SENSEX" in symbols_named


# ── POST /api/v1/sweep/oneclick/run ──────────────────────────────────────────


def test_run_route_refuses_without_launching_on_empty_lake(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    _CAPTURED.clear()
    resp = client.post("/api/v1/sweep/oneclick/run", params={"symbols": "NIFTY"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is False
    assert body["ok"] is False
    assert "requests" not in _CAPTURED


def test_run_route_bad_field_400(client: TestClient) -> None:
    resp = client.post("/api/v1/sweep/oneclick/run", params={"symbols": "RELIANCE"})
    assert resp.status_code == 400
    assert "sweep REFUSED" in resp.json()["detail"]


def test_run_route_no_lake_configured_400() -> None:
    from types import SimpleNamespace

    fake_store = SimpleNamespace(path=None)
    with TestClient(build_app(DashboardState(), instrument_store=fake_store)) as c:
        resp = c.post("/api/v1/sweep/oneclick/run")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "no lake configured for sweep"


def test_run_route_launches_every_group_when_preflight_ok(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkeypatch the preflight itself to force ``ok=True`` so the launch path runs."""
    _CAPTURED.clear()
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    monkeypatch.setattr(
        app_module,
        "oneclick_preflight_for_plan",
        lambda plan, **kw: {
            "sweep_run_id": plan.sweep_run_id,
            "ok": True,
            "refusals": [],
            "clamps": [],
            "estimator": {
                "trials": 1,
                "est_seconds_provisional": 0.1,
                "est_bytes": 1,
                "estimate_basis": "provisional",
            },
        },
    )
    resp = client.post("/api/v1/sweep/oneclick/run", params={"symbols": "NIFTY,SENSEX"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is True
    assert body["ok"] is True
    assert _wait_for_job(client)["state"] == "done"
    assert len(_CAPTURED["requests"]) == 2  # NSE (NIFTY) + BSE (SENSEX), sequential
    assert {r.exchange for r in _CAPTURED["requests"]} == {"NSE", "BSE"}


def test_preflight_then_run_share_the_same_id_when_caller_supplies_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-9 adversarial-refuter finding, end-to-end: a preflight poll
    followed by the actual launch, both carrying the SAME caller-supplied
    ``run_id``, must report the identical ``sweep_run_id`` in both
    responses — the continuity the default (server-minted) behavior cannot
    provide across two independently-stateless calls."""
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    monkeypatch.setattr(
        app_module,
        "oneclick_preflight_for_plan",
        lambda plan, **kw: {
            "sweep_run_id": plan.sweep_run_id,
            "ok": True,
            "refusals": [],
            "clamps": [],
            "estimator": {
                "trials": 1,
                "est_seconds_provisional": 0.1,
                "est_bytes": 1,
                "estimate_basis": "provisional",
            },
        },
    )
    pre = client.get(
        "/api/v1/sweep/oneclick/preflight",
        params={"symbols": "NIFTY", "run_id": "shared-continuity-id"},
    )
    run = client.post(
        "/api/v1/sweep/oneclick/run",
        params={"symbols": "NIFTY", "run_id": "shared-continuity-id"},
    )
    assert pre.json()["sweep_run_id"] == "shared-continuity-id"
    assert run.json()["sweep_run_id"] == "shared-continuity-id"


def test_run_route_surfaces_a_per_group_failure_not_a_false_done(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-5 adversarial-refuter finding: a bare non-zero exit code from
    one exchange group's real ``cmd_sweep_all`` run used to be silently
    discarded by ``BackgroundPullJob`` — the finish detail stayed the
    generic "done" even though a group genuinely failed, and a sibling
    group's real success could mask it entirely (worse than the
    single-group case, where a lone failure was at least the only signal).
    The fan-out must surface a failed group's exit code in the finish
    detail instead of a false "done"."""
    _CAPTURED.clear()

    def _one_group_fails(request: Any, *, lake_root: Path, results_root: Path) -> int:
        _CAPTURED.setdefault("requests", []).append(request)
        return 2 if request.exchange == "BSE" else 0

    monkeypatch.setattr(app_module, "run_sweep_from_request", _one_group_fails)
    monkeypatch.setattr(
        app_module,
        "oneclick_preflight_for_plan",
        lambda plan, **kw: {
            "sweep_run_id": plan.sweep_run_id,
            "ok": True,
            "refusals": [],
            "clamps": [],
            "estimator": {
                "trials": 1,
                "est_seconds_provisional": 0.1,
                "est_bytes": 1,
                "estimate_basis": "provisional",
            },
        },
    )
    resp = client.post("/api/v1/sweep/oneclick/run", params={"symbols": "NIFTY,SENSEX"})
    assert resp.status_code == 200
    assert resp.json()["started"] is True
    final = _wait_for_job(client)
    assert final["state"] == "done"  # the job thread itself never raises
    assert "FAILED" in final["detail"]
    assert "BSE" in final["detail"]
    assert len(_CAPTURED["requests"]) == 2  # both groups still ran — no group silently skipped


def test_run_route_exception_in_one_group_does_not_abort_the_rest(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-6 adversarial-refuter finding: a group that RAISES (rather than
    returning a non-zero exit code) used to propagate straight out of the
    fan-out loop, silently aborting every REMAINING group. Group order is
    deterministic (alphabetical by exchange, so BSE runs before NSE) — a
    BSE-specific crash would systematically starve the NSE (NIFTY/BANKNIFTY)
    group every time they were requested together. Each group's call must
    now be isolated so a crash in one never skips its siblings."""
    _CAPTURED.clear()
    calls: list[str] = []

    def _bse_raises(request: Any, *, lake_root: Path, results_root: Path) -> int:
        calls.append(request.exchange)
        if request.exchange == "BSE":
            raise RuntimeError("boom: disk full mid-BSE-group")
        _CAPTURED.setdefault("requests", []).append(request)
        return 0

    monkeypatch.setattr(app_module, "run_sweep_from_request", _bse_raises)
    monkeypatch.setattr(
        app_module,
        "oneclick_preflight_for_plan",
        lambda plan, **kw: {
            "sweep_run_id": plan.sweep_run_id,
            "ok": True,
            "refusals": [],
            "clamps": [],
            "estimator": {
                "trials": 1,
                "est_seconds_provisional": 0.1,
                "est_bytes": 1,
                "estimate_basis": "provisional",
            },
        },
    )
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        resp = c.post("/api/v1/sweep/oneclick/run", params={"symbols": "NIFTY,SENSEX"})
        assert resp.status_code == 200
        assert resp.json()["started"] is True
        final = _wait_for_job(c)
    assert final["state"] == "done"
    assert "FAILED" in final["detail"]
    assert "BSE" in final["detail"]
    assert calls == ["BSE", "NSE"]  # both groups attempted despite BSE's crash
    assert len(_CAPTURED["requests"]) == 1  # only NSE's real call recorded (BSE raised)


def test_run_route_does_not_mark_cache_dirty_on_total_failure(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-6 adversarial-refuter finding: ``on_success`` used to fire
    unconditionally whenever the job's work function returned without
    raising — including the new all-groups-failed ``{"note": ...}`` shape —
    wrongly marking the results cache dirty when NO new data had actually
    landed. Must only fire when at least one group genuinely succeeded."""
    from brutex.dashboard.summary_cache import SummaryCache

    _CAPTURED.clear()
    mark_dirty_calls: list[int] = []
    cache = SummaryCache()
    monkeypatch.setattr(cache, "mark_dirty", lambda: mark_dirty_calls.append(1))

    def _always_fails(request: Any, *, lake_root: Path, results_root: Path) -> int:
        return 1

    monkeypatch.setattr(app_module, "run_sweep_from_request", _always_fails)
    monkeypatch.setattr(
        app_module,
        "oneclick_preflight_for_plan",
        lambda plan, **kw: {
            "sweep_run_id": plan.sweep_run_id,
            "ok": True,
            "refusals": [],
            "clamps": [],
            "estimator": {
                "trials": 1,
                "est_seconds_provisional": 0.1,
                "est_bytes": 1,
                "estimate_basis": "provisional",
            },
        },
    )
    with TestClient(build_app(DashboardState(), instrument_store=store, summary_cache=cache)) as c:
        resp = c.post("/api/v1/sweep/oneclick/run", params={"symbols": "NIFTY,SENSEX"})
        assert resp.status_code == 200
        final = _wait_for_job(c)
    assert final["state"] == "done"
    assert "FAILED" in final["detail"]
    assert mark_dirty_calls == []


def test_run_route_long_exception_from_one_group_does_not_hide_the_other(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-7 adversarial-refuter finding: the job framework's finish
    detail is truncated at ``_FINISH_DETAIL_MAX`` (500 chars) — an
    unusually verbose exception message from the alphabetically-FIRST
    failing group (BSE) used to be able to push the note past that cap,
    silently dropping the LATER (NSE) group's identity/detail from what the
    operator ever sees. Each per-group detail must now be capped BEFORE
    joining, so both groups' identities always survive regardless of how
    verbose either exception's message is."""
    _CAPTURED.clear()

    def _bse_raises_verbosely(request: Any, *, lake_root: Path, results_root: Path) -> int:
        if request.exchange == "BSE":
            raise RuntimeError("x" * 400)
        return 2  # NSE fails too, with a short, ordinary exit code

    monkeypatch.setattr(app_module, "run_sweep_from_request", _bse_raises_verbosely)
    monkeypatch.setattr(
        app_module,
        "oneclick_preflight_for_plan",
        lambda plan, **kw: {
            "sweep_run_id": plan.sweep_run_id,
            "ok": True,
            "refusals": [],
            "clamps": [],
            "estimator": {
                "trials": 1,
                "est_seconds_provisional": 0.1,
                "est_bytes": 1,
                "estimate_basis": "provisional",
            },
        },
    )
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        resp = c.post("/api/v1/sweep/oneclick/run", params={"symbols": "NIFTY,SENSEX"})
        assert resp.status_code == 200
        final = _wait_for_job(c)
    assert final["state"] == "done"
    assert "BSE" in final["detail"]
    assert "NSE" in final["detail"]
    assert "exit 2" in final["detail"]


def test_run_route_many_failing_groups_stays_within_the_finish_detail_cap(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-8 adversarial-refuter finding: capping each per-group detail
    (round 7) bounds ONE verbose group, but nothing bounded the NUMBER of
    failing groups named — with today's real 2-group ceiling this can never
    fire, but the round-7 fix's own docstring warned "never assume that
    stays true". This directly constructs a fake plan with 8 exchange
    groups (bypassing ``resolve_oneclick_sweep_plan``, which cannot produce
    more than 2 groups today) to prove the note stays informative and
    within the job framework's overall length cap regardless of how many
    groups ever exist."""
    from brutex.dashboard.pull_jobs import _FINISH_DETAIL_MAX
    from brutex.dashboard.sweep_runner import DEFAULT_MODE, OneClickSweepPlan, SweepRunRequest

    _CAPTURED.clear()
    today = date.today().isoformat()
    many_groups = OneClickSweepPlan(
        sweep_run_id="many-groups-id",
        requests=tuple(
            SweepRunRequest(
                symbols="NIFTY",
                intervals="1minute",
                directions="BUY",
                k="1",
                sl="50",
                tp="100",
                exchange=f"EXCH{i:02d}",
                segment="INDEX",
                start=today,
                end=today,
                mode=DEFAULT_MODE,
                top=10,
            )
            for i in range(8)
        ),
    )

    def _all_raise(request: Any, *, lake_root: Path, results_root: Path) -> int:
        raise RuntimeError("x" * 100)

    monkeypatch.setattr(app_module, "run_sweep_from_request", _all_raise)
    monkeypatch.setattr(app_module, "resolve_oneclick_sweep_plan", lambda **kw: many_groups)
    monkeypatch.setattr(
        app_module,
        "oneclick_preflight_for_plan",
        lambda plan, **kw: {
            "sweep_run_id": plan.sweep_run_id,
            "ok": True,
            "refusals": [],
            "clamps": [],
            "estimator": {
                "trials": 1,
                "est_seconds_provisional": 0.1,
                "est_bytes": 1,
                "estimate_basis": "provisional",
            },
        },
    )
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        resp = c.post("/api/v1/sweep/oneclick/run", params={"symbols": "NIFTY"})
        assert resp.status_code == 200
        final = _wait_for_job(c)
    assert final["state"] == "done"
    assert "8/8 exchange group(s) FAILED" in final["detail"]
    assert "+4 more" in final["detail"]
    assert len(final["detail"]) <= _FINISH_DETAIL_MAX


def test_run_route_second_click_while_running_is_a_noop(
    store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    started_evt = threading.Event()
    release_evt = threading.Event()

    def _blocking(request: Any, *, lake_root: Path, results_root: Path) -> int:
        started_evt.set()
        release_evt.wait(timeout=10.0)
        return 0

    monkeypatch.setattr(app_module, "run_sweep_from_request", _blocking)
    monkeypatch.setattr(
        app_module,
        "oneclick_preflight_for_plan",
        lambda plan, **kw: {
            "sweep_run_id": plan.sweep_run_id,
            "ok": True,
            "refusals": [],
            "clamps": [],
            "estimator": {
                "trials": 1,
                "est_seconds_provisional": 0.1,
                "est_bytes": 1,
                "estimate_basis": "provisional",
            },
        },
    )
    with TestClient(build_app(DashboardState(), instrument_store=store)) as c:
        first = c.post("/api/v1/sweep/oneclick/run", params={"symbols": "NIFTY"})
        assert first.status_code == 200
        assert first.json()["started"] is True
        assert started_evt.wait(timeout=10.0)
        second = c.post("/api/v1/sweep/oneclick/run", params={"symbols": "NIFTY"})
        assert second.status_code == 200
        assert second.json()["started"] is False
        release_evt.set()


# ── legacy /api/v1/sweep/run still works, now with acknowledge_sla_overrun ──


def test_legacy_route_accepts_acknowledge_sla_overrun(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    resp = client.post("/api/v1/sweep/run", params={"acknowledge_sla_overrun": "true"})
    assert resp.status_code == 200
    assert resp.json()["started"] is True


# ── R54 none-leg coverage on the one-click surfaces (operator ruling ─────────
# 2026-07-08: none cells first-class RUNNABLE; the preflight payload carries
# the stage3_enable clamp disclosure — route-level pins so a future
# route-side re-validation cannot silently re-violate R54).


def test_preflight_route_none_leg_carries_stage3_downgrade_clamp_r54(client: TestClient) -> None:
    """GET preflight with ``tp=none`` must be a 200 verdict (never the
    pre-R54 400 hard refusal) whose ``clamps`` channel — the operator
    proceed-dialog's data source — carries the ``stage3_enable``
    requested→applied downgrade row."""
    resp = client.get("/api/v1/sweep/oneclick/preflight", params={"symbols": "NIFTY", "tp": "none"})
    assert resp.status_code == 200
    body = resp.json()
    stage3_clamps = [c for c in body["clamps"] if c["field"] == "stage3_enable"]
    assert len(stage3_clamps) == 1
    assert stage3_clamps[0]["requested"] == "true"
    assert stage3_clamps[0]["applied"] == "false"
    assert "R54" in stage3_clamps[0]["reason"]


def test_run_route_mixed_none_leg_launch_accepted_r54(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/v1/sweep/oneclick/run with the mixed both-arms grid shape
    (``sl="none,50"``) resolves + launches (preflight forced ``ok`` — the
    hermetic empty lake would otherwise refuse on coverage, which is a
    DATA refusal, not the leg refusal R54 outlaws)."""
    _CAPTURED.clear()
    monkeypatch.setattr(app_module, "run_sweep_from_request", _fake_run_sweep_from_request)
    monkeypatch.setattr(
        app_module,
        "oneclick_preflight_for_plan",
        lambda plan, **kw: {
            "sweep_run_id": plan.sweep_run_id,
            "ok": True,
            "refusals": [],
            "clamps": [],
            "estimator": {
                "trials": 1,
                "est_seconds_provisional": 0.1,
                "est_bytes": 1,
                "estimate_basis": "provisional",
            },
        },
    )
    resp = client.post("/api/v1/sweep/oneclick/run", params={"symbols": "NIFTY", "sl": "none,50"})
    assert resp.status_code == 200
    assert resp.json()["started"] is True
    assert _wait_for_job(client)["state"] == "done"
    assert [r.sl for r in _CAPTURED["requests"]] == ["none,50"]
