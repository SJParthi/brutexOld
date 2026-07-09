"""The cooperative-cancel routes (DEC-JOB-COOPERATIVE-CANCEL-001).

Covers the two POST cancel routes + the additive ``cancelling`` pull-progress
key: ``POST /api/v1/pull/cancel`` (kind-gated: only a running PULL job) and
``POST /api/v1/sweep/cancel`` (in-process sweep, detached launch-page sweep,
nothing running, and the no-lake guard). The running-job state is simulated by
monkeypatching the ``BackgroundPullJob`` kind/cancel seams + the sweep-cancel
sidecar writer — NEVER a real lake/sweep/pull. Skipped cleanly when the ``[ui]``
extra (FastAPI) or duckdb is absent.
"""

from __future__ import annotations

import importlib
import threading
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
from brutex.dashboard.pull_jobs import (
    JOB_KIND_PULL,
    JOB_KIND_SWEEP,
    BackgroundPullJob,
)
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


def _force_running(monkeypatch: pytest.MonkeyPatch, *, kind: str, cancelled: bool = True) -> None:
    """Make every BackgroundPullJob report a live run of *kind*.

    The cancel routes read ``_pull_job.running_job_kind`` + call
    ``_pull_job.request_cancel(expected_kind=…)`` / ``.cancel_requested()``; the
    app owns the job internally, so the state is simulated at the class level.
    The ``request_cancel`` stub HONOURS the kind gate exactly like the real
    compare-and-set (review fold — the gate now lives INSIDE ``request_cancel``,
    so a stub blind to ``expected_kind`` would mask the kind-gating behaviour;
    the real gate's own unit tests live in ``test_pull_jobs.py``).
    """
    monkeypatch.setattr(BackgroundPullJob, "running_job_kind", property(lambda _self: kind))

    def _fake_request_cancel(_self: Any, *, expected_kind: str | None = None) -> bool:
        return expected_kind is None or expected_kind == kind

    monkeypatch.setattr(BackgroundPullJob, "request_cancel", _fake_request_cancel)
    monkeypatch.setattr(BackgroundPullJob, "cancel_requested", lambda _self: cancelled)


# ── the additive pull-progress cancelling key ─────────────────────────────────


def test_pull_progress_carries_cancelling_key_idle(client: TestClient) -> None:
    snap = client.get("/api/v1/universe/pull-progress").json()
    assert snap["cancelling"] is False


def test_pull_progress_cancelling_true_when_token_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(BackgroundPullJob, "cancel_requested", lambda _self: True)
    snap = client.get("/api/v1/universe/pull-progress").json()
    assert snap["cancelling"] is True


def test_pull_progress_carries_job_kind(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ADDITIVE ``job_kind`` key (review fold — the on-load Stop-button attach).

    ``null`` when idle; the running kind otherwise — the /universe page-load
    attach gates on ``job_kind === 'pull'`` so a running dashboard SWEEP on the
    shared single-flight job never fabricates a pull watch.
    """
    assert client.get("/api/v1/universe/pull-progress").json()["job_kind"] is None
    monkeypatch.setattr(
        BackgroundPullJob, "running_job_kind", property(lambda _self: JOB_KIND_PULL)
    )
    assert client.get("/api/v1/universe/pull-progress").json()["job_kind"] == "pull"
    monkeypatch.setattr(
        BackgroundPullJob, "running_job_kind", property(lambda _self: JOB_KIND_SWEEP)
    )
    assert client.get("/api/v1/universe/pull-progress").json()["job_kind"] == "sweep"


# ── POST /api/v1/pull/cancel ──────────────────────────────────────────────────


def test_pull_cancel_idle_is_false_noop(client: TestClient) -> None:
    resp = client.post("/api/v1/pull/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelling": False}


def test_pull_cancel_running_pull_job(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _force_running(monkeypatch, kind=JOB_KIND_PULL)
    resp = client.post("/api/v1/pull/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelling": True}


def test_pull_cancel_ignores_a_running_sweep(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SWEEP-kind job is never cancelled through the pull route (kind-gated)."""
    _force_running(monkeypatch, kind=JOB_KIND_SWEEP)
    resp = client.post("/api/v1/pull/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelling": False}


# ── POST /api/v1/sweep/cancel ─────────────────────────────────────────────────


def test_sweep_cancel_in_process(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _force_running(monkeypatch, kind=JOB_KIND_SWEEP)
    written: dict[str, Any] = {}
    monkeypatch.setattr(
        "brutex.engine.sweep_cancel.request_cancel",
        lambda root, *, source: written.update(root=root, source=source) or root,
    )
    resp = client.post("/api/v1/sweep/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelling": True, "target": "in-process"}
    assert written["source"] == "route"
    # The sidecar ROOT law (review fold — the detached-arm twin of the
    # fabricated-ack class): the dashboard's in-process sweep runs
    # cmd_sweep_all(out_root=default_results_root(lake)) and probes
    # <lake>/sweeps/.sweep_cancel.json at every tuple boundary, so the route
    # MUST write there — a repointed root would ack a cancel the run never sees.
    lake = store.path.parent  # type: ignore[union-attr]
    assert written["root"] == lake / "sweeps"


def test_sweep_cancel_detached_running(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No in-process sweep, but a DETACHED launch-page sweep is live → target detached.

    A LEGACY launch state with NO recorded ``results_root`` falls back to the
    DEFAULT ``<lake>/sweeps`` root (the pre-fix behaviour, kept only as the
    no-better-information fallback).
    """
    # The job is idle (no monkeypatch of running_job_kind → real None).
    monkeypatch.setattr(
        "brutex.dashboard.launch_button.read_launch_state", lambda _lake: {"pid": 4242}
    )
    monkeypatch.setattr("brutex.dashboard.launch_button.state_liveness", lambda _state: "running")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "brutex.engine.sweep_cancel.request_cancel",
        lambda root, *, source: captured.update(root=root, source=source) or root,
    )
    resp = client.post("/api/v1/sweep/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelling": True, "target": "detached"}
    assert captured["source"] == "route-detached"
    lake = store.path.parent  # type: ignore[union-attr]
    assert captured["root"] == lake / "sweeps"


def test_sweep_cancel_detached_writes_sidecar_at_the_recorded_launch_root(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sidecar lands at the LAUNCH STATE's results_root — the root the run PROBES.

    Review fold (fails-before): the launch page spawns ``brutex sweep-all
    --results-root <lake>/sweeps/launch-<stamp>-<card>`` and ``cmd_sweep_all``
    stats ``<out_root>/.sweep_cancel.json`` — so a sidecar written at the
    default ``<lake>/sweeps`` was NEVER observed by the detached run (a
    fabricated ``{"cancelling": true}``). The route must target the recorded
    root exactly where ``cmd_sweep_all(out_root=that root)`` probes.
    """
    lake = store.path.parent  # type: ignore[union-attr]
    launch_root = lake / "sweeps" / "launch-20260709-120000-fast"
    monkeypatch.setattr(
        "brutex.dashboard.launch_button.read_launch_state",
        lambda _lake: {"pid": 4242, "results_root": str(launch_root)},
    )
    monkeypatch.setattr("brutex.dashboard.launch_button.state_liveness", lambda _state: "running")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "brutex.engine.sweep_cancel.request_cancel",
        lambda root, *, source: captured.update(root=root, source=source) or root,
    )
    resp = client.post("/api/v1/sweep/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelling": True, "target": "detached"}
    assert captured["root"] == launch_root  # NOT the default <lake>/sweeps


def test_sweep_cancel_detached_hostile_root_falls_back_to_default(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch-state results_root ESCAPING <lake>/sweeps is refused → default root.

    The route never writes a sidecar outside the lake's sweeps tree off a
    hostile / relocated state file — confinement, then the default fallback.
    """
    monkeypatch.setattr(
        "brutex.dashboard.launch_button.read_launch_state",
        lambda _lake: {"pid": 4242, "results_root": "/tmp/evil-elsewhere"},
    )
    monkeypatch.setattr("brutex.dashboard.launch_button.state_liveness", lambda _state: "running")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "brutex.engine.sweep_cancel.request_cancel",
        lambda root, *, source: captured.update(root=root, source=source) or root,
    )
    resp = client.post("/api/v1/sweep/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelling": True, "target": "detached"}
    lake = store.path.parent  # type: ignore[union-attr]
    assert captured["root"] == lake / "sweeps"


def test_detached_sweep_cancel_root_helper_branches(tmp_path: Path) -> None:
    """The pure helper's confinement branches (non-string · blank · confined · escape)."""
    lake = tmp_path / "lake"
    default = lake / "sweeps"
    helper = app_module._detached_sweep_cancel_root
    # Non-string / absent / blank → default.
    assert helper({}, lake, default=default) == default
    assert helper({"results_root": 42}, lake, default=default) == default
    assert helper({"results_root": "   "}, lake, default=default) == default
    # Confined under <lake>/sweeps (the exact sweeps root AND a launch subdir) → kept.
    launch = default / "launch-20260709-x"
    assert helper({"results_root": str(launch)}, lake, default=default) == launch
    assert helper({"results_root": str(default)}, lake, default=default) == default
    # Escapes (a sibling dir, a traversal) → default, never an out-of-tree write.
    assert helper({"results_root": str(tmp_path / "evil")}, lake, default=default) == default
    assert (
        helper({"results_root": str(default / ".." / ".." / "evil")}, lake, default=default)
        == default
    )


def test_detached_root_resolve_oserror_falls_back_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pathological ``Path.resolve`` OSError on the RECORDED root → the default root.

    Combined-PR fix round 2 (the #1614/#1524 killable-mutant-suppression purge): this
    branch previously rode a ``# pragma: no cover`` — now directly tested. The fake
    raises ONLY for the recorded candidate path (an ELOOP-style failure); the earlier
    ``<lake>/sweeps`` resolve outside the try must still succeed.
    """
    lake = tmp_path / "lake"
    default = lake / "sweeps"
    hostile = default / "launch-oserror"
    real_resolve = Path.resolve

    def _resolve(self: Path, strict: bool = False) -> Path:
        if self == hostile:
            raise OSError("simulated ELOOP symlink cycle")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", _resolve)
    assert (
        app_module._detached_sweep_cancel_root(
            {"results_root": str(hostile)}, lake, default=default
        )
        == default
    )


def test_sweep_cancel_nothing_running(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing running (no in-process sweep, no live detached state) → cancelling False."""
    monkeypatch.setattr("brutex.dashboard.launch_button.read_launch_state", lambda _lake: None)
    resp = client.post("/api/v1/sweep/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelling": False}


def _publish_running_bridge(lake: Path, *, wall_time: float | None = None) -> None:
    """Publish a RUNNING progress snapshot at the DEFAULT-root bridge file."""
    from brutex.dashboard.sweep_runner import default_results_root, sweep_progress_file
    from brutex.engine.sweep_progress import SweepProgress
    from brutex.engine.sweep_supervisor import publish_progress

    progress = SweepProgress()
    progress.start(enumerated=10, detail="cli sweep")
    kwargs: dict[str, Any] = {}
    if wall_time is not None:
        kwargs["wall_time"] = lambda: wall_time
    publish_progress(sweep_progress_file(default_results_root(lake)), progress.snapshot(), **kwargs)


def test_sweep_cancel_bridge_visible_cli_sweep(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FRESH running default-root bridge (a terminal CLI sweep) IS cancellable.

    Review fold (fails-before — the false-refusal class): a sequential
    default-root ``brutex sweep-all`` is visible ONLY through the progress file
    bridge — exactly what the dashboard Stop button keys on — yet the route
    resolved neither an in-process job nor a launch state and answered
    "No running sweep to cancel." while a sidecar at the default root WOULD
    cancel that run (``cmd_sweep_all`` probes it at every tuple boundary).
    """
    monkeypatch.setattr("brutex.dashboard.launch_button.read_launch_state", lambda _lake: None)
    lake = store.path.parent  # type: ignore[union-attr]
    _publish_running_bridge(lake)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "brutex.engine.sweep_cancel.request_cancel",
        lambda root, *, source: captured.update(root=root, source=source) or root,
    )
    resp = client.post("/api/v1/sweep/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelling": True, "target": "bridge"}
    assert captured["root"] == lake / "sweeps"  # the root the CLI run probes
    assert captured["source"] == "route-bridge"


def test_sweep_cancel_stale_bridge_is_honest_false(
    client: TestClient, store: InstrumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A STALE running bridge (SIGKILLed writer) is dead evidence → no fabricated ack.

    Mirrors the Stop button's own decayed view: past ``SWEEP_PROGRESS_MAX_AGE_S``
    the frozen ``running=True`` file is ≡ absent, so the route answers an honest
    ``{"cancelling": false}`` and writes NO sidecar (a stray request must never
    pre-poison the next launch).
    """
    from brutex.engine.sweep_supervisor import SWEEP_PROGRESS_MAX_AGE_S

    monkeypatch.setattr("brutex.dashboard.launch_button.read_launch_state", lambda _lake: None)
    lake = store.path.parent  # type: ignore[union-attr]
    _publish_running_bridge(lake, wall_time=time.time() - SWEEP_PROGRESS_MAX_AGE_S - 60.0)

    def _never(_root: Path, *, source: str) -> Path:
        raise AssertionError("a stale bridge must never write a cancel sidecar")

    monkeypatch.setattr("brutex.engine.sweep_cancel.request_cancel", _never)
    resp = client.post("/api/v1/sweep/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelling": False}


def test_universe_template_carries_the_on_load_pull_attach(client: TestClient) -> None:
    """The /universe page probes pull-progress ON LOAD and gates on the pull kind.

    Review fold (honest substring-presence pin — the repo's no-headless-JS
    envelope): a freshly-opened page during a multi-hour pull previously had NO
    Stop control until a Run re-click (setBulkDisabled fired only from
    startAndPoll).
    """
    html = client.get("/universe").text
    assert "pullAttachIfRunning" in html
    assert "s.job_kind !== 'pull'" in html  # the sweep-on-shared-job gate
    # The scoped honesty wording (review fold — the cancel-blind spans).
    assert "metadata phase is honored only at the next phase boundary" in html.lower()


def test_sweep_cancel_no_lake_configured() -> None:
    """A store whose ``path`` is not a real lake path → a clean 400, never a 500."""
    fake_store = SimpleNamespace(path="not-a-real-path", get=lambda _k: None)
    with TestClient(build_app(DashboardState(), instrument_store=fake_store)) as c:
        resp = c.post("/api/v1/sweep/cancel")
    assert resp.status_code == 400
    assert "no lake configured" in resp.json()["detail"]


# ── the pull work closures thread the cancel probe (review fold) ──────────────
#
# The cancel route can only be honest if the RUNNING spine actually observes
# the token: each surface's work closure must pass the job's bound
# ``cancel_requested`` as ``should_stop``. Before the fix, only the
# /pull-pipeline closures were wired — cancelling a pull-all / retry-failed /
# composite / direct-selection run returned {"cancelling": true} while the
# spine ran to completion (fabricated success).


class _FakeVerdict:
    """Minimal verdict payload for the spot report's banner slot."""

    def to_dict(self) -> dict[str, Any]:
        return {"status": "clean"}


class _FakeSpotReport:
    """Stub spot pull report — only ``.verdict`` is read on the job thread."""

    @property
    def verdict(self) -> _FakeVerdict:
        return _FakeVerdict()


def _drain_job(client: TestClient, *, timeout: float = 10.0) -> None:
    """Poll /pull-progress until the single-flight background job settles."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert isinstance(snap, dict)
        if snap["state"] in {"done", "error"}:
            return
        time.sleep(0.01)
    raise AssertionError("background job did not settle within the timeout")


def _assert_job_cancel_probe(probe: Any) -> None:
    """The captured ``should_stop`` is the job's bound running-gated probe."""
    assert callable(probe)
    assert getattr(probe, "__name__", "") == "cancel_requested"
    # Idle after settle → the running gate reads an honest False.
    assert probe() is False


def test_pull_all_spot_receives_the_cancel_probe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /universe/pull-all threads should_stop into pull_all_spot (fails-before)."""
    captured: dict[str, Any] = {}

    def _fake_pull_all_spot(instruments: list[Any], **kwargs: Any) -> _FakeSpotReport:
        captured["should_stop"] = kwargs["should_stop"]
        return _FakeSpotReport()

    monkeypatch.setattr(app_module, "pull_all_spot", _fake_pull_all_spot)
    resp = client.post("/api/v1/universe/pull-all", params={"symbols": "NIFTY", "dry_run": "true"})
    assert resp.status_code == 200
    assert resp.json()["started"] is True
    _drain_job(client)
    _assert_job_cancel_probe(captured["should_stop"])


def test_retry_failed_receives_the_cancel_probe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /universe/retry-failed threads should_stop into pull_all_spot (fails-before)."""
    captured: dict[str, Any] = {}

    def _fake_pull_all_spot(instruments: list[Any], **kwargs: Any) -> _FakeSpotReport:
        captured["should_stop"] = kwargs["should_stop"]
        return _FakeSpotReport()

    monkeypatch.setattr(app_module, "pull_all_spot", _fake_pull_all_spot)
    resp = client.post(
        "/api/v1/universe/retry-failed", params={"symbols": "NIFTY", "dry_run": "true"}
    )
    assert resp.status_code == 200
    assert resp.json()["started"] is True
    _drain_job(client)
    _assert_job_cancel_probe(captured["should_stop"])


def test_metadata_pull_cancel_is_an_honest_false(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RUNNING metadata pull refuses the cancel — its spine has NO cancel seam.

    Fails-before: the route read kind==PULL and returned {"cancelling": true}
    while ``pull_expiries_contracts`` (no ``should_stop`` param) ran to full
    completion — a fabricated success. The metadata surface now starts
    ``cancellable=False`` so the route answers honestly.
    """
    entered = threading.Event()
    release = threading.Event()

    def _fake_metadata(instruments: list[Any], **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(10.0), "test orchestration: release never fired"
        return SimpleNamespace(to_dict=lambda: {"ok": True})

    monkeypatch.setattr(app_module, "pull_expiries_contracts", _fake_metadata)
    resp = client.post(
        "/api/v1/universe/pull-expiries-contracts",
        params={"symbols": "NIFTY", "dry_run": "true"},
    )
    assert resp.status_code == 200
    assert resp.json()["started"] is True
    assert entered.wait(10.0), "the metadata work never started"
    try:
        cancel = client.post("/api/v1/pull/cancel")
        assert cancel.status_code == 200
        assert cancel.json() == {"cancelling": False}  # honest — the run cannot observe it
        snap = client.get("/api/v1/universe/pull-progress").json()
        assert snap["cancelling"] is False  # the token was never set
    finally:
        release.set()
    _drain_job(client)
