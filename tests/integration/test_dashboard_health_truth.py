"""Route-level integration for DEC-OPERATOR-HEALTH-TRUTH-001 (MIGRATION.md #1747).

Drives the REAL routes via ``build_app`` + ``TestClient``:

* ``GET /api/v1/pull/rate`` — the additive ``last_pull`` outcome block, with
  every pull outcome state produced by a REAL :class:`BackgroundPullJob`
  (clean · completed-with-failures · halted-disk-full · halted-OOM ·
  halted-market-hours · halted-quota · market-hours START deferral ·
  running · idle/never-run · halt-masked-by-sweep [hostile-review M2] ·
  pipeline market-hours stop [M3] · sweep-then-deferred [m5] ·
  uncorrelated-verdict [M2]) flowing through the REAL retained-state law
  (``classify_retained_pull_outcome`` over ``job.last_pull_state()``) —
  never a hand-stubbed wire dict;
* ``GET /api/v1/health/hub`` — the unified fold: green / red / amber /
  not-wired lanes, a corrupt state file reading a NAMED degrade (never a
  500), and the production-default (un-injected) wiring.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from brutex.dashboard import pull_jobs
from brutex.dashboard.app import DashboardState, build_app
from brutex.dashboard.health_hub import (
    LANE_BACKUP,
    LANE_DOCTOR,
    LANE_MUTATION,
    LANE_NIGHTLY,
    LANE_PULL,
    HealthHub,
)
from brutex.dashboard.pull_jobs import JOB_KIND_SWEEP, BackgroundPullJob
from brutex.dashboard.pull_outcome import LastPullOutcome, classify_retained_pull_outcome
from brutex.data.errors import LakeWriteHalt, MarketHoursHalt
from brutex.orchestrator.quota_tracker import DailyQuotaExceeded

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _no_real_locks_or_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the REAL jobs hermetic: no ``~/.brutex/lake`` flock, no Telegram."""
    monkeypatch.setattr(pull_jobs, "_default_lake_root_resolver", lambda: None)
    monkeypatch.setattr(pull_jobs, "emit_pull_halt_alert", lambda exc: None)


def _sync_spawn(fn: Callable[[], None]) -> None:
    """Run the worker inline so every start() is deterministic."""
    fn()


def _job(
    *,
    spawn: Callable[[Callable[[], None]], None] = _sync_spawn,
    market_hours: bool = False,
) -> BackgroundPullJob:
    return BackgroundPullJob(
        lake_root_resolver=lambda: None,
        account_key_resolver=lambda: None,
        spawn=spawn,
        market_hours_guard=lambda: market_hours,
    )


def _holder(verdict: dict[str, Any] | None = None, *, pull_id: str = "") -> dict[str, Any]:
    """The app's verdict-holder shape (verdict + the stamping run's id)."""
    return {"verdict": verdict, "pull_id": pull_id}


def _probe_over(
    job: BackgroundPullJob, verdict_holder: dict[str, Any]
) -> Callable[[], LastPullOutcome]:
    """The REAL retained-state law over a REAL job — the production probe's body."""

    def probe() -> LastPullOutcome:
        return classify_retained_pull_outcome(
            job.last_pull_state(),
            verdict=verdict_holder["verdict"],
            verdict_pull_id=verdict_holder["pull_id"],
        )

    return probe


def _clean_verdict() -> dict[str, Any]:
    """A clean PullVerdict.to_dict()-shaped mapping."""
    return {
        "kind": "clean",
        "headline": "pull complete — all windows settled",
        "action": "",
        "pending_windows": 0,
        "needs_operator": False,
    }


def _client(probe: Callable[[], LastPullOutcome] | None = None) -> TestClient:
    return TestClient(build_app(DashboardState(), pull_outcome_probe=probe))


def _last_pull(client: TestClient) -> dict[str, Any]:
    body = client.get("/api/v1/pull/rate").json()
    assert "last_pull" in body, "the additive last_pull block must always be present"
    block = body["last_pull"]
    assert set(block) == {"outcome", "tone", "headline", "detail"}
    return block


class TestPullRateLastPullBlock:
    """Every pull outcome state, through a REAL job + the REAL route."""

    def test_never_run_production_default_probe(self) -> None:
        """A fresh app (NO injection) reads the honest neutral — never green."""
        with _client() as c:
            block = _last_pull(c)
        assert block["outcome"] == "never_run"
        assert block["tone"] == "idle"

    def test_clean_completion_reads_green_clean(self) -> None:
        """A clean pull whose route stamped the verdict WITH its run id."""
        job = _job()
        holder = _holder()
        assert job.start(lambda hook: {"note": ""}, total=1, pull_id="run-clean") is True
        holder["verdict"] = _clean_verdict()
        holder["pull_id"] = "run-clean"  # the route's _work stamps inside pull_id_scope
        with _client(_probe_over(job, holder)) as c:
            block = _last_pull(c)
        assert block["outcome"] == "clean"
        assert block["tone"] == "ok"
        assert block["headline"] == "pull complete — all windows settled"

    def test_completed_without_verdict_is_green_but_never_claims_cleanly(self) -> None:
        job = _job()
        assert job.start(lambda hook: None, total=1) is True
        with _client(_probe_over(job, _holder())) as c:
            block = _last_pull(c)
        assert block["outcome"] == "completed"
        assert block["tone"] == "ok"
        assert "cleanly" not in block["headline"]

    def test_completed_with_failures_reads_amber(self) -> None:
        job = _job()

        def work(hook: Any) -> None:
            hook("NSE-X", 0, True)  # the drivers' positional (label, bars, failed) hook

        assert job.start(work, total=2) is True
        with _client(_probe_over(job, _holder())) as c:
            block = _last_pull(c)
        assert block["outcome"] == "completed_with_failures"
        assert block["tone"] == "warn"

    @pytest.mark.parametrize(
        ("exc", "outcome", "tone"),
        [
            (LakeWriteHalt("disk_full"), "halted_disk_full", "bad"),
            (LakeWriteHalt("out_of_memory"), "halted_oom", "bad"),
            (MarketHoursHalt(), "halted_market_hours", "warn"),
            (DailyQuotaExceeded("daily quota spent"), "halted_quota", "bad"),
        ],
        ids=["disk_full", "oom", "market_hours", "quota"],
    )
    def test_halted_pull_reads_named_degraded_never_green(
        self, exc: Exception, outcome: str, tone: str
    ) -> None:
        """The headline gap: a HALTED pull must never share the clean green."""
        job = _job()

        def work(hook: Any) -> None:
            raise exc

        assert job.start(work, total=1) is True
        assert job.snapshot().state == "error"
        with _client(_probe_over(job, _holder())) as c:
            block = _last_pull(c)
        assert block["outcome"] == outcome
        assert block["tone"] == tone
        assert type(exc).__name__ in block["detail"]  # the recorded evidence rides along

    def test_market_hours_start_deferral_reads_warn(self) -> None:
        job = _job(market_hours=True)
        assert job.start(lambda hook: None, total=1) is False  # the START gate defers
        with _client(_probe_over(job, _holder())) as c:
            block = _last_pull(c)
        assert block["outcome"] == "deferred_market_hours"
        assert block["tone"] == "warn"
        assert "market hours" in block["headline"]

    def test_running_pull_reads_ok_running(self) -> None:
        captured: list[Callable[[], None]] = []
        job = _job(spawn=captured.append)  # captured, never run → stays RUNNING
        assert job.start(lambda hook: None, total=4) is True
        assert job.running is True
        with _client(_probe_over(job, _holder())) as c:
            block = _last_pull(c)
        assert block["outcome"] == "running"
        assert block["tone"] == "ok"

    def test_sweep_done_is_never_claimed_as_a_pull_outcome(self) -> None:
        """A finished SWEEP on the shared slot leaves the pull outcome never_run."""
        job = _job()
        assert job.start(lambda hook: None, total=1, job_kind=JOB_KIND_SWEEP) is True
        assert job.snapshot().state == "done"
        with _client(_probe_over(job, _holder())) as c:
            block = _last_pull(c)
        assert block["outcome"] == "never_run"
        assert block["tone"] == "idle"

    def test_halted_pull_stays_red_after_a_sweep_reuses_the_slot(self) -> None:
        """Hostile-review M2 (the reviewer's repro, dead): a disk-full HALT
        followed by a SWEEP — with an OLDER clean verdict still in the holder
        — must STAY red, never flip to the stale green."""
        job = _job()
        # An older pull completed clean and stamped its verdict…
        assert job.start(lambda hook: {"note": ""}, total=1, pull_id="run-old") is True
        holder = _holder(_clean_verdict(), pull_id="run-old")

        def _disk_full(hook: Any) -> None:
            raise LakeWriteHalt("disk_full")

        # …then a pull HALTS (raises → no stamp)…
        assert job.start(_disk_full, total=1, pull_id="run-halted") is True
        # …then a SWEEP reuses the shared slot (resetting the shared tracker).
        assert job.start(lambda hook: None, total=1, job_kind=JOB_KIND_SWEEP) is True
        assert job.snapshot().state == "done"  # the tracker now shows the sweep
        with _client(_probe_over(job, holder)) as c:
            block = _last_pull(c)
        assert block["outcome"] == "halted_disk_full"
        assert block["tone"] == "bad"
        assert "LakeWriteHalt" in block["detail"]

    def test_uncorrelated_stale_verdict_never_colours_a_later_run(self) -> None:
        """Hostile-review M2 (metadata-only shape): a pull that finishes WITHOUT
        stamping must not inherit an older run's clean verdict."""
        job = _job()
        assert job.start(lambda hook: {"note": ""}, total=1, pull_id="run-old") is True
        holder = _holder(_clean_verdict(), pull_id="run-old")
        # A later pull (metadata-only style — its route stamps no verdict).
        assert job.start(lambda hook: None, total=1, pull_id="run-meta") is True
        with _client(_probe_over(job, holder)) as c:
            block = _last_pull(c)
        assert block["outcome"] == "completed"  # honest — NOT the stale "clean"
        assert block["headline"] == "pull finished"

    def test_pipeline_market_hours_stop_reads_deferred_never_green(self) -> None:
        """Hostile-review M3 (the reviewer's repro, dead): the pipeline CATCHES
        a between-phases MarketHoursHalt and finishes ``done`` with the
        deferral note + a CLEAN spot verdict stamped by the SAME run — the
        outcome must read deferred, never 'completed cleanly'."""
        from brutex.dashboard.pipeline_pull import _market_hours_stop_note

        job = _job()
        note = _market_hours_stop_note()
        # The pipeline _work returns the report dict; its note becomes the
        # done finish detail via pull_jobs._finish_detail.
        assert job.start(lambda hook: {"note": note}, total=1, pull_id="run-pipeline") is True
        assert job.snapshot().state == "done"
        holder = _holder(_clean_verdict(), pull_id="run-pipeline")  # same-run stamp
        with _client(_probe_over(job, holder)) as c:
            block = _last_pull(c)
        assert block["outcome"] == "deferred_market_hours"
        assert block["tone"] == "warn"
        assert block["detail"] == note

    def test_deferred_pull_after_a_sweep_reads_deferred_not_idle(self) -> None:
        """Hostile-review m5 (the reviewer's repro, dead): a market-hours START
        deferral RIGHT AFTER a sweep run must read deferred — the deferral
        record is retained as the last PULL terminal even though the slot's
        stamped kind is still the sweep's."""
        captured: list[Callable[[], None]] = []
        guard = {"in_window": False}
        job = BackgroundPullJob(
            lake_root_resolver=lambda: None,
            account_key_resolver=lambda: None,
            spawn=lambda fn: (captured.append(fn), fn())[0],
            market_hours_guard=lambda: guard["in_window"],
        )
        assert job.start(lambda hook: None, total=1, job_kind=JOB_KIND_SWEEP) is True
        guard["in_window"] = True
        assert job.start(lambda hook: None, total=1) is False  # deferred
        with _client(_probe_over(job, _holder())) as c:
            block = _last_pull(c)
        assert block["outcome"] == "deferred_market_hours"
        assert block["tone"] == "warn"

    def test_every_preexisting_payload_key_survives(self) -> None:
        """The block is ADDITIVE — flow_state & friends stay byte-identical."""
        with _client() as c:
            body = c.get("/api/v1/pull/rate").json()
        assert body["flow_state"] == "idle"
        assert body["active"] is False
        assert body["req_per_sec_achieved"] == 0.0
        assert "bridge_source" in body


def _write_nightly_report(results_root: Path, *, ok: bool, blocker: str | None = None) -> None:
    run_dir = results_root / "nightly" / "nightly-20261008T120000Z-abc123"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "ok": ok,
                "suspect": False,
                "blocker": blocker,
                "ended_utc": 1.0e9,
                "dry_run": False,
            }
        ),
        encoding="utf-8",
    )


def _hub(tmp_path: Path, *, lake_root: Path | None = None) -> HealthHub:
    return HealthHub(
        lake_root_resolver=lambda: lake_root,
        pull_outcome_probe=lambda: LastPullOutcome(
            outcome="never_run", tone="idle", headline="no pull has run", detail=""
        ),
        env={"BRUTEX_BACKUP_STATE_DIR": str(tmp_path / "backup-state")},
        clock=lambda: 1.0e9 + 3600.0,
    )


class TestHealthHubRoute:
    """GET /api/v1/health/hub — fold + lanes through the REAL route."""

    def test_production_default_hub_serves_all_lanes(self) -> None:
        """No injection: the default hub answers 200 with the five lanes."""
        with TestClient(build_app(DashboardState())) as c:
            body = c.get("/api/v1/health/hub").json()
        assert set(body) == {"fold", "lanes", "generated_at", "ttl_s"}
        assert [lane["lane"] for lane in body["lanes"]] == [
            LANE_PULL,
            LANE_NIGHTLY,
            LANE_BACKUP,
            LANE_DOCTOR,
            LANE_MUTATION,
        ]
        assert body["fold"]["verdict"] in {"ok", "warn", "bad"}
        # The two GitHub-Actions-only lanes are honestly NOT WIRED, never green.
        assert _lane(body, LANE_DOCTOR)["verdict"] == "not_wired"
        assert _lane(body, LANE_MUTATION)["verdict"] == "not_wired"

    def test_green_nightly_lane_via_injected_hub(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _write_nightly_report(lake / "sweeps", ok=True)
        with TestClient(
            build_app(DashboardState(), health_hub=_hub(tmp_path, lake_root=lake))
        ) as c:
            body = c.get("/api/v1/health/hub").json()
        assert _lane(body, LANE_NIGHTLY)["verdict"] == "ok"
        assert body["fold"]["verdict"] == "ok"

    def test_failed_nightly_folds_red(self, tmp_path: Path) -> None:
        lake = tmp_path / "lake"
        _write_nightly_report(lake / "sweeps", ok=False, blocker="pull")
        with TestClient(
            build_app(DashboardState(), health_hub=_hub(tmp_path, lake_root=lake))
        ) as c:
            body = c.get("/api/v1/health/hub").json()
        assert _lane(body, LANE_NIGHTLY)["verdict"] == "bad"
        assert body["fold"]["verdict"] == "bad"

    def test_corrupt_state_files_read_named_degrades_never_a_500(self, tmp_path: Path) -> None:
        """Corrupt nightly report + corrupt backup state → 200 with named lanes."""
        lake = tmp_path / "lake"
        run_dir = lake / "sweeps" / "nightly" / "nightly-20261008T120000Z-abc123"
        run_dir.mkdir(parents=True)
        (run_dir / "report.json").write_text("{torn", encoding="utf-8")
        state_dir = tmp_path / "backup-state"
        state_dir.mkdir(parents=True)
        (state_dir / "state.json").write_text("also torn", encoding="utf-8")
        with TestClient(
            build_app(DashboardState(), health_hub=_hub(tmp_path, lake_root=lake))
        ) as c:
            resp = c.get("/api/v1/health/hub")
        assert resp.status_code == 200
        body = resp.json()
        assert _lane(body, LANE_NIGHTLY)["verdict"] == "unknown"
        assert "corrupt" in _lane(body, LANE_NIGHTLY)["detail"]
        assert _lane(body, LANE_BACKUP)["verdict"] == "bad"
        assert "state_corrupt" in _lane(body, LANE_BACKUP)["headline"]
        assert body["fold"]["verdict"] == "bad"

    def test_deep_nested_report_never_500s_the_route(self, tmp_path: Path) -> None:
        """Hostile-review M1 repro, verified dead: a 60k-bracket nesting bomb
        at report.json made ``json.loads`` raise ``RecursionError`` — which
        previously ESCAPED the parse clause and 500'd this route.  Now: 200
        with the named unknown degrade."""
        lake = tmp_path / "lake"
        run_dir = lake / "sweeps" / "nightly" / "nightly-20261008T120000Z-abc123"
        run_dir.mkdir(parents=True)
        (run_dir / "report.json").write_bytes(b"[" * 60_000)
        with TestClient(
            build_app(DashboardState(), health_hub=_hub(tmp_path, lake_root=lake))
        ) as c:
            resp = c.get("/api/v1/health/hub")
        assert resp.status_code == 200
        body = resp.json()
        assert _lane(body, LANE_NIGHTLY)["verdict"] == "unknown"
        assert "RecursionError" in _lane(body, LANE_NIGHTLY)["detail"]

    def test_unconfigured_box_reads_not_wired_lanes_and_honest_fold(self, tmp_path: Path) -> None:
        with TestClient(build_app(DashboardState(), health_hub=_hub(tmp_path))) as c:
            body = c.get("/api/v1/health/hub").json()
        assert _lane(body, LANE_NIGHTLY)["verdict"] == "not_wired"
        assert _lane(body, LANE_BACKUP)["verdict"] == "not_wired"
        assert body["fold"]["verdict"] == "ok"
        assert "not wired" in body["fold"]["headline"]


def _lane(body: dict[str, Any], lane: str) -> dict[str, Any]:
    matches = [entry for entry in body["lanes"] if entry["lane"] == lane]
    assert len(matches) == 1
    return matches[0]
