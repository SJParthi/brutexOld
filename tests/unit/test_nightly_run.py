# pyright: reportPrivateUsage=none
"""Composition tests + fault injection for `brutex.nightly.run`
(DEC-SWEEP-NIGHTLY-AUTOMATION-001).

Lock acquisition ORDER (nightly → account → lake), BOTH pull locks released
after PULL+PREBUILD with the lake lock FREE during the SWEEP leg
(DEC-SWEEP-LOCK-PULL-CONCURRENCY-001 — the sweep leg's exclusion is
cmd_sweep_all's own G10+G6), CredentialsError → account
skip + WARN, pull-halt → `emit_pull_halt_alert` then FAILED, report + bundle
written in a ``finally`` even on a raise, the Telegram-disabled arm, dry-run
takes NO locks + mutates nothing (tree oracle), skip levers, the T2/T4/T5
tripwires, and the R6 commit-re-key honesty note.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import structlog.testing
from brutex.adapters.groww import pull_singleflight as ps
from brutex.config.credentials_types import CredentialsError
from brutex.data.errors import LakeWriteHalt
from brutex.engine.sweep_audit import SweepAuditWriteError, SweepAuditWriter, verify_chain
from brutex.engine.sweep_supervisor import SUPERVISOR_ACTION_COMPLETE
from brutex.nightly.chain import LinkStatus, NightlyLink
from brutex.nightly.report import nightly_audit_path, nightly_dir
from brutex.nightly.run import (
    R48_DIVERGENCE_NOTE,
    NightlyPlan,
    NightlySeams,
    run_nightly,
)


@pytest.fixture(autouse=True)
def _hermetic_disk_floors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin both disk floors to 0 so the chain never reads the CI machine's
    REAL free disk (the 20 GiB lake default refused the DISK link on a
    low-disk GitHub runner and BLOCKED every downstream link — the
    2026-07-08 run-28948028949 red; test_cli_nightly.py already pins).
    The floor-breach tests override with their own in-test ``setenv``."""
    monkeypatch.setenv("BRUTEX_NIGHTLY_MIN_FREE_LAKE_BYTES", "0")
    monkeypatch.setenv("BRUTEX_NIGHTLY_MIN_FREE_BACKUP_BYTES", "0")


class FakeAlerter:
    """Records nightly_summary / nightly_refused calls; can raise on summary."""

    def __init__(self, *, raise_on_summary: bool = False) -> None:
        self.summaries: list[dict[str, Any]] = []
        self.refused: list[dict[str, Any]] = []
        self.raise_on_summary = raise_on_summary

    def nightly_summary(self, **kwargs: Any) -> bool:
        if self.raise_on_summary:
            raise RuntimeError("summary explosion")
        self.summaries.append(kwargs)
        return True

    def nightly_refused(self, **kwargs: Any) -> bool:
        self.refused.append(kwargs)
        return True


@dataclass
class _FakeHistorical:
    bars_appended: int = 111


@dataclass
class _FakeComposite:
    historical: _FakeHistorical | None = field(default_factory=_FakeHistorical)


@dataclass
class _FakePipeReport:
    """The attribute surface decompose_pipeline_report reads."""

    ok: bool = True
    note: str = "pipeline complete"
    stopped_after: str | None = None
    quarantined_count: int = 0
    historical: _FakeComposite | None = field(default_factory=_FakeComposite)
    prebuild_built: int | None = 4
    prebuild_crashed: str | None = None
    prebuild_disk_guarded: bool = False
    prebuild_failed: tuple[str, ...] = ()
    post_steps_strict_skipped: bool = False


class FakePipeline:
    """A HeadlessPipeline stand-in; records close()."""

    def __init__(self) -> None:
        self.closed = 0
        self.strict_gate = False
        self.targets = 3

    def close(self) -> None:
        self.closed += 1


def _lake(tmp_path: Path) -> tuple[Path, Path]:
    lake = tmp_path / "lake"
    results = lake / "sweeps"
    (lake / "window_ledger" / "deltas").mkdir(parents=True)
    (lake / "window_ledger" / "ledger.parquet").write_bytes(b"LEDGER")
    (lake / "registry.duckdb").write_bytes(b"R")
    results.mkdir(parents=True)
    return lake, results


def _write_sweep_completion(
    results: Path, payload: dict[str, Any], *, ts_utc: float | None = None
) -> None:
    """Write the tally the way PRODUCTION does: the SUPERVISOR audit chain
    (``SweepGuards.audit`` → ``audit/sweep-all-supervisor.jsonl``) with the
    REAL engine action constant (value ``"SUPERVISOR_COMPLETE"``) — never the
    ingest ``sweep-all.jsonl`` (review round 1: a fixture shaped after the
    buggy reader masked both the wrong file AND the wrong action string).
    *ts_utc* overrides the entry clock (default: the writer's real
    ``time.time()`` stamp, which reads FRESH against the fake seam clock)."""
    path = results / "audit" / "sweep-all-supervisor.jsonl"
    resume = path.exists()
    with SweepAuditWriter.open(path, actor="sweep", resume=resume) as audit:
        audit.append(
            action=SUPERVISOR_ACTION_COMPLETE, subject="sweep:x", payload=payload, ts_utc=ts_utc
        )


_COHERENT = {
    "tuples_total": 10,
    "tuples_run": 5,
    "skipped_cache": 3,
    "skipped_resume": 1,
    "tuples_skipped_no_bars": 1,
    "tuples_failed": 0,
}


def _plan(lake: Path, results: Path, tmp_path: Path, **overrides: Any) -> NightlyPlan:
    base: dict[str, Any] = {
        "lake_root": lake,
        "results_root": results,
        "backup_root": tmp_path / "backups",
        "keep": 7,
        "from_date": None,
        "to_date": None,
        "sweep_kwargs": {"symbols": "NIFTY"},
        "skip_backup": False,
        "skip_sweep": False,
        "dry_run": False,
    }
    base.update(overrides)
    return NightlyPlan(**base)


@contextmanager
def _noop_keep_awake(reason: str) -> Generator[bool]:
    yield False


def _seams(
    monkeypatch: pytest.MonkeyPatch,
    results: Path,
    *,
    pipe_report: object | None = None,
    execute_raises: BaseException | None = None,
    sweep_exit: int = 0,
    sweep_payload: dict[str, Any] | None = _COHERENT,
    alerter: FakeAlerter | None = None,
    credentials: Callable[[], Any] | None = None,
    copy_file: Callable[[Path, Path], None] | None = None,
    now: Callable[[], float] | None = None,
) -> tuple[NightlySeams, FakeAlerter, FakePipeline, dict[str, Any]]:
    """Production-shaped seams over fakes; returns (seams, alerter, pipeline, calls)."""
    fake_alerter = alerter or FakeAlerter()
    pipeline = FakePipeline()
    calls: dict[str, Any] = {"sweep": 0, "build": 0}

    def build_pipeline(**kwargs: Any) -> FakePipeline:
        calls["build"] += 1
        calls["build_kwargs"] = kwargs
        return pipeline

    def fake_execute(hp: object) -> object:
        assert hp is pipeline
        if execute_raises is not None:
            raise execute_raises
        return pipe_report if pipe_report is not None else _FakePipeReport()

    monkeypatch.setattr("brutex.nightly.run.execute_headless_pipeline", fake_execute)

    def run_sweep(**kwargs: Any) -> int:
        calls["sweep"] += 1
        calls["sweep_kwargs"] = kwargs
        if sweep_payload is not None:
            _write_sweep_completion(results, sweep_payload)
        return sweep_exit

    def default_credentials() -> Any:
        raise CredentialsError("no AWS in tests")

    seams = NightlySeams(
        build_pipeline=cast(Any, build_pipeline),
        run_sweep=run_sweep,
        alerter=cast(Any, fake_alerter),
        keep_awake_cm=_noop_keep_awake,
        credentials=credentials or default_credentials,
        copy_file=copy_file,
        now=now or (lambda: 1000.0),
    )
    return seams, fake_alerter, pipeline, calls


# ── green path ───────────────────────────────────────────────────────────


def test_green_path_report_bundle_audit_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)
    seams, alerter, pipeline, calls = _seams(monkeypatch, results)
    with structlog.testing.capture_logs() as logs:
        report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert report.ok is True
    assert report.exit_code == 0
    assert report.suspect is False
    assert [o.status for o in report.links] == [LinkStatus.OK] * 6
    assert report.snapshot_id == report.nightly_id
    # Counts folded across links (sweep from the REAL audit tail).
    assert report.counts["pull_bars"] == 111
    assert report.counts["prebuild_built"] == 4
    assert report.counts["sweep_run"] == 5
    assert report.counts["sweep_skipped_cache"] == 3
    assert report.counts["backup_files"] is not None
    # The R48 divergence note rides EVERY report.
    assert R48_DIVERGENCE_NOTE in report.honesty_notes
    # Report + bundle on disk; audit chain green with the full event set.
    run_dir = nightly_dir(results, report.nightly_id)
    assert json.loads((run_dir / "report.json").read_text(encoding="utf-8"))["ok"] is True
    assert (run_dir / "bundle" / "report.json").exists()
    # The bundle's sweep tail is the SUPERVISOR chain — the file carrying the
    # completion tally the report's counts came from, incl. any TUPLE_FAILED
    # forensics (review round 2: the bundle copied the ingest sweep-all.jsonl,
    # which never holds the completion).
    sweep_tail = (run_dir / "bundle" / "sweep-audit-tail.jsonl").read_text(encoding="utf-8")
    assert SUPERVISOR_ACTION_COMPLETE in sweep_tail
    audit_file = nightly_audit_path(results)
    assert verify_chain(audit_file) > 0
    text = audit_file.read_text(encoding="utf-8")
    for action in ("NIGHTLY_START", "LINK_START", "LINK_DONE", "BACKUP_SNAPSHOT", "NIGHTLY_DONE"):
        assert action in text
    # Backup sealed; the snapshot dir exists under the backup root.
    assert (tmp_path / "backups" / report.nightly_id / "manifest.json").exists()
    # One summary, verdict OK, no refusals; the pipeline store was closed.
    assert [s["verdict"] for s in alerter.summaries] == ["OK"]
    assert alerter.refused == []
    assert pipeline.closed == 1
    assert calls["sweep"] == 1
    assert calls["sweep_kwargs"] == {"symbols": "NIFTY"}
    # The account-lock skip WARN fired (test credentials raise CredentialsError).
    assert any(log["event"] == "nightly.account_lock_skipped" for log in logs)
    # All locks released: both re-acquirable now.
    with ps.nightly_singleflight_lock(lake), ps.pull_singleflight_lock(lake):
        pass


def test_lock_order_nightly_account_lake_and_hold_spans(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)
    order: list[str] = []
    real_nightly = ps.nightly_singleflight_lock
    real_account = ps.account_pull_singleflight_lock
    real_pull = ps.pull_singleflight_lock

    @contextmanager
    def rec_nightly(root: Path, **kwargs: Any) -> Generator[None]:
        order.append("nightly")
        with real_nightly(root, **kwargs):
            yield

    @contextmanager
    def rec_account(api_key: str, **kwargs: Any) -> Generator[None]:
        order.append("account")
        with real_account(api_key, lock_dir=tmp_path / "account-locks"):
            yield
        order.append("account-released")

    @contextmanager
    def rec_pull(root: Path, **kwargs: Any) -> Generator[None]:
        order.append("lake")
        with real_pull(root, **kwargs):
            yield
        order.append("lake-released")

    monkeypatch.setattr(ps, "nightly_singleflight_lock", rec_nightly)
    monkeypatch.setattr(ps, "account_pull_singleflight_lock", rec_account)
    monkeypatch.setattr(ps, "pull_singleflight_lock", rec_pull)

    def probing_execute(hp: object) -> object:
        # DURING the PULL(+PREBUILD) leg BOTH pull locks are HELD: a peer
        # pull's EX acquire on a second open file description must be REFUSED
        # (pins the release point can't drift EARLIER than post-PREBUILD).
        with (
            pytest.raises(ps.PullAlreadyRunningError),
            real_pull(lake),  # pragma: no cover — must NOT acquire
        ):
            pass
        order.append("probed:lake-held-during-pull")
        return _FakePipeReport()

    def probing_sweep(**kwargs: Any) -> int:
        # By the SWEEP leg BOTH pull locks are RELEASED
        # (DEC-SWEEP-LOCK-PULL-CONCURRENCY-001 — supersedes the #1612
        # held-through-SWEEP clause): the leg's own cmd_sweep_all G10+G6 own
        # the exclusion, and a daily pull/crossverify can run CONCURRENTLY.
        assert "account-released" in order
        assert "lake-released" in order
        # LIVE probe: a second open file description takes the lake EX freely
        # — a pull started during the sweep leg would NOT defer.
        with real_pull(lake):
            pass
        order.append("probed:lake-free-during-sweep")
        _write_sweep_completion(results, _COHERENT)
        return 0

    seams, _alerter, _pipeline, _calls = _seams(
        monkeypatch,
        results,
        credentials=lambda: SimpleNamespace(api_key="k-1"),
    )
    # AFTER _seams (which installs its own fake): the hold-span probe is the
    # execute seam for THIS test — it runs while both pull locks are held.
    monkeypatch.setattr("brutex.nightly.run.execute_headless_pipeline", probing_execute)
    seams = NightlySeams(
        build_pipeline=seams.build_pipeline,
        run_sweep=probing_sweep,
        alerter=seams.alerter,
        keep_awake_cm=_noop_keep_awake,
        credentials=lambda: SimpleNamespace(api_key="k-1"),
        now=seams.now,
    )
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert report.ok is True
    assert order[:3] == ["nightly", "account", "lake"]  # the FIXED order
    # Held through PULL+PREBUILD (the probe ran BEFORE either release), then
    # BOTH released BEFORE the sweep leg's own probes.
    assert order.index("probed:lake-held-during-pull") < order.index("account-released")
    assert order.index("probed:lake-held-during-pull") < order.index("lake-released")
    assert order.index("account-released") < order.index("probed:lake-free-during-sweep")
    assert order.index("lake-released") < order.index("probed:lake-free-during-sweep")


def test_collision_second_nightly_refused_no_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)
    seams, alerter, _pipeline, calls = _seams(monkeypatch, results)
    with ps.nightly_singleflight_lock(lake, holder_note="nightly:peer"):
        report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert report.exit_code == 1
    assert report.blocker is NightlyLink.COLLISION
    assert "nightly_already_running" in report.links[0].detail
    assert "(holder: nightly:peer)" in report.links[0].detail
    assert all(o.status is LinkStatus.BLOCKED for o in report.links[1:])
    assert calls["build"] == 0  # the chain NEVER ran
    assert calls["sweep"] == 0
    [refusal] = alerter.refused
    assert refusal["link"] == "collision"
    assert [s["verdict"] for s in alerter.summaries] == ["REFUSED"]
    # The refusal report is still durably written.
    assert (nightly_dir(results, report.nightly_id) / "report.json").exists()


def test_collision_manual_pull_in_flight_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)
    seams, alerter, _pipeline, calls = _seams(monkeypatch, results)
    with ps.pull_singleflight_lock(lake):  # a live manual pull
        report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert report.exit_code == 1
    collision = report.links[0]
    assert collision.status is LinkStatus.REFUSED
    assert "pull_in_flight" in collision.detail
    assert calls["build"] == 0
    [refusal] = alerter.refused  # the INSTANT signal fired via on_event
    assert refusal["link"] == "collision"
    assert "pull_in_flight" in refusal["reason"]


def test_collision_account_lock_contention_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)
    lock_dir = tmp_path / "account-locks"
    monkeypatch.setattr(ps, "default_account_lock_dir", lambda: lock_dir)
    seams, _alerter, _pipeline, calls = _seams(
        monkeypatch, results, credentials=lambda: SimpleNamespace(api_key="k-1")
    )
    with ps.account_pull_singleflight_lock("k-1", lock_dir=lock_dir):
        report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert report.links[0].status is LinkStatus.REFUSED
    assert "pull_in_flight" in report.links[0].detail
    assert calls["build"] == 0


def test_collision_live_sweep_heuristic_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)
    progress = results / "progress" / "sweep-progress.json"
    progress.parent.mkdir(parents=True)
    progress.write_text('{"state": "running"}', encoding="utf-8")
    seams, _alerter, _pipeline, calls = _seams(
        monkeypatch,
        results,
        now=None,  # real clock ⇒ the fresh mtime reads live
    )
    seams = NightlySeams(
        build_pipeline=seams.build_pipeline,
        run_sweep=seams.run_sweep,
        alerter=seams.alerter,
        keep_awake_cm=_noop_keep_awake,
        credentials=seams.credentials,
    )
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert report.links[0].status is LinkStatus.REFUSED
    assert "sweep_alive(" in report.links[0].detail
    assert calls["build"] == 0


# ── disk link ────────────────────────────────────────────────────────────


def test_disk_floor_breach_refuses_with_exact_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)
    seams, alerter, _pipeline, calls = _seams(monkeypatch, results)
    monkeypatch.setenv("BRUTEX_NIGHTLY_MIN_FREE_LAKE_BYTES", str(10**18))  # a PB floor
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    disk = report.links[1]
    assert disk.status is LinkStatus.REFUSED
    assert "lake volume" in disk.detail
    assert report.exit_code == 1
    assert calls["build"] == 0
    [refusal] = alerter.refused
    assert refusal["link"] == "disk"


# ── backup link ──────────────────────────────────────────────────────────


def test_skip_backup_reads_skipped_and_chain_continues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)
    seams, alerter, _pipeline, calls = _seams(monkeypatch, results)
    report = run_nightly(_plan(lake, results, tmp_path, skip_backup=True), seams=seams)
    assert report.links[2].status is LinkStatus.SKIPPED
    assert report.exit_code == 0
    assert calls["sweep"] == 1
    assert report.snapshot_id is None
    [summary] = alerter.summaries
    assert summary["link_verdicts"]["backup"] == "skipped"


def test_backup_enospc_fails_and_blocks_rest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)

    def enospc(_source: Path, _dest: Path) -> None:
        raise OSError(28, "No space left on device")

    seams, alerter, _pipeline, calls = _seams(monkeypatch, results, copy_file=enospc)
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    backup = report.links[2]
    assert backup.status is LinkStatus.FAILED
    assert backup.error_type == "OSError"
    assert [o.status for o in report.links[3:]] == [LinkStatus.BLOCKED] * 3
    assert report.exit_code == 2
    assert calls["build"] == 0
    # Only the .partial staging exists — never a sealed snapshot.
    assert list((tmp_path / "backups").glob("*")) == [
        tmp_path / "backups" / f"{report.nightly_id}.partial"
    ]
    assert [s["verdict"] for s in alerter.summaries] == ["FAILED"]


def test_backup_t4_nested_manifest_json_is_not_a_false_tripwire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A steady-state results tree carries NESTED ``manifest.json`` files
    (``cascade/manifest.json``, ``runs/<id>/manifest.json``) — REAL backup
    entries counted in ``manifest.item_count``. Review round 1: the T4 re-stat
    excluded ``manifest.json`` by BASENAME at any depth, undercounted the disk
    and fired a FALSE ``snapshot_unreadable`` on every healthy night."""
    lake, results = _lake(tmp_path)
    (results / "cascade").mkdir()
    (results / "cascade" / "manifest.json").write_text("{}", encoding="utf-8")
    prior_run = results / "runs" / "run-prior"
    prior_run.mkdir(parents=True)
    (prior_run / "manifest.json").write_text("{}", encoding="utf-8")
    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results)
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert not any(w.name == "snapshot_unreadable" for w in report.tripwires)
    assert report.suspect is False
    assert report.exit_code == 0
    # Both nested manifests genuinely rode into the sealed snapshot.
    snapshot = tmp_path / "backups" / report.nightly_id
    assert (snapshot / "results" / "cascade" / "manifest.json").exists()
    assert (snapshot / "results" / "runs" / "run-prior" / "manifest.json").exists()


def test_sweep_counts_never_read_from_ingest_audit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tally in the ingest ``audit/sweep-all.jsonl`` is NOT the supervisor
    completion — production writes the counts ONLY to
    ``audit/sweep-all-supervisor.jsonl`` (``SweepGuards.audit``, cli.py), so a
    reader keyed on the ingest file reports honest-unknown, never counts
    (review round 1, finding 2 — the reader pointed at the wrong file)."""
    lake, results = _lake(tmp_path)

    def sweep_writes_only_ingest_audit(**_kwargs: Any) -> int:
        path = results / "audit" / "sweep-all.jsonl"
        with SweepAuditWriter.open(path, actor="sweep", resume=path.exists()) as audit:
            audit.append(action=SUPERVISOR_ACTION_COMPLETE, subject="sweep:x", payload=_COHERENT)
        return 0

    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results)
    seams = NightlySeams(
        build_pipeline=seams.build_pipeline,
        run_sweep=sweep_writes_only_ingest_audit,
        alerter=seams.alerter,
        keep_awake_cm=_noop_keep_awake,
        credentials=seams.credentials,
        now=seams.now,
    )
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert report.counts["sweep_run"] is None
    assert any("sweep counts unknown" in note for note in report.honesty_notes)


def test_backup_t4_extra_file_in_snapshot_marks_suspect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A copier smuggling an EXTRA file into the snapshot trips snapshot_unreadable."""
    lake, results = _lake(tmp_path)

    def sneaky_copy(source: Path, dest: Path) -> None:
        dest.write_bytes(source.read_bytes())
        (dest.parent / (dest.name + ".sneak")).write_bytes(b"EXTRA")

    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results, copy_file=sneaky_copy)
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert any(w.name == "snapshot_unreadable" for w in report.tripwires)
    assert report.suspect is True
    assert report.exit_code == 2  # SUSPECT is cron-RED even though links are green


# ── pull link ────────────────────────────────────────────────────────────


def test_capability_refusal_maps_to_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)
    from brutex.nightly.pipeline_binding import NightlyCapabilityError

    def refusing_build(**_kwargs: Any) -> Any:
        raise NightlyCapabilityError("no instrument master — load instruments first")

    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results)
    seams = NightlySeams(
        build_pipeline=refusing_build,
        run_sweep=seams.run_sweep,
        alerter=seams.alerter,
        keep_awake_cm=_noop_keep_awake,
        credentials=seams.credentials,
        now=seams.now,
    )
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    pull = report.links[3]
    assert pull.status is LinkStatus.REFUSED
    assert "no instrument master" in pull.detail
    assert report.exit_code == 1
    # Crash-safety (DEC-SWEEP-LOCK-PULL-CONCURRENCY-001): the refusal arm
    # releases the lake pull lock too — re-acquirable after the run.
    with ps.pull_singleflight_lock(lake):
        pass


def test_pull_halt_fires_alert_then_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lake, results = _lake(tmp_path)
    halted: list[BaseException] = []
    monkeypatch.setattr("brutex.notifications.sweep_alerts.emit_pull_halt_alert", halted.append)
    seams, _alerter, pipeline, calls = _seams(
        monkeypatch, results, execute_raises=LakeWriteHalt("disk_full")
    )
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    pull = report.links[3]
    assert pull.status is LinkStatus.FAILED
    assert pull.error_type == "LakeWriteHalt"
    assert len(halted) == 1  # the dedicated CRITICAL halt alert fired FIRST
    assert report.links[4].status is LinkStatus.BLOCKED  # prebuild
    assert report.links[5].status is LinkStatus.BLOCKED  # sweep
    assert report.exit_code == 2
    assert pipeline.closed == 1  # closed in the finally even on the raise
    assert calls["sweep"] == 0
    # Crash-safety (DEC-SWEEP-LOCK-PULL-CONCURRENCY-001): the pull-halt FAILED
    # path releases the lake pull lock (pull_fn's finally) — re-acquirable.
    with ps.pull_singleflight_lock(lake):
        pass


@pytest.mark.parametrize(
    "shape",
    [
        "backup_enospc",
        "capability_refused",
        "pull_halt",
        "pull_generic_error",
        "sweep_exit_one",
        "sweep_raise",
    ],
)
def test_every_failure_path_releases_both_pull_locks(
    shape: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DEC-SWEEP-LOCK-PULL-CONCURRENCY-001 crash-safety, over the failure
    POSITIONS: whatever link breaks — backup, pull refusal, pull halt, a
    generic pull bug, a failing or RAISING sweep — ``run_nightly`` returns
    with the LAKE pull lock and the nightly single-flight both re-acquirable
    (the ``lake_stack`` close in ``pull_fn``'s ``finally`` + the chain-lifetime
    ``locks`` backstop). A broken nightly never wedges the next pull."""
    lake, results = _lake(tmp_path)
    kwargs: dict[str, Any] = {}
    if shape == "pull_halt":
        kwargs["execute_raises"] = LakeWriteHalt("disk_full")
    elif shape == "pull_generic_error":
        kwargs["execute_raises"] = ValueError("bug")
    elif shape == "sweep_exit_one":
        kwargs["sweep_exit"] = 1
        kwargs["sweep_payload"] = None
    elif shape == "backup_enospc":

        def _enospc(_src: Path, _dst: Path) -> None:
            raise OSError(28, "No space left on device")

        kwargs["copy_file"] = _enospc
    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results, **kwargs)
    if shape == "capability_refused":
        from brutex.nightly.pipeline_binding import NightlyCapabilityError

        def refusing_build(**_kwargs: Any) -> Any:
            raise NightlyCapabilityError("no instrument master")

        seams = NightlySeams(
            build_pipeline=refusing_build,
            run_sweep=seams.run_sweep,
            alerter=seams.alerter,
            keep_awake_cm=_noop_keep_awake,
            credentials=seams.credentials,
            now=seams.now,
        )
    elif shape == "sweep_raise":

        def raising_sweep(**_kwargs: Any) -> int:
            raise RuntimeError("sweep exploded")

        seams = NightlySeams(
            build_pipeline=seams.build_pipeline,
            run_sweep=raising_sweep,
            alerter=seams.alerter,
            keep_awake_cm=_noop_keep_awake,
            credentials=seams.credentials,
            now=seams.now,
        )
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert report.ok is False  # every shape genuinely broke the chain
    # BOTH single-flight files re-acquirable — nothing left wedged.
    with ps.nightly_singleflight_lock(lake), ps.pull_singleflight_lock(lake):
        pass


def test_generic_pull_error_fails_without_halt_alert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)
    halted: list[BaseException] = []
    monkeypatch.setattr("brutex.notifications.sweep_alerts.emit_pull_halt_alert", halted.append)
    seams, _alerter, _pipeline, _calls = _seams(
        monkeypatch, results, execute_raises=ValueError("bug")
    )
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert report.links[3].status is LinkStatus.FAILED
    assert halted == []  # a generic error is NOT a pull halt


def test_keyboard_interrupt_still_writes_report_then_reraises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)
    seams, alerter, _pipeline, _calls = _seams(
        monkeypatch, results, execute_raises=KeyboardInterrupt()
    )
    with pytest.raises(KeyboardInterrupt):
        run_nightly(_plan(lake, results, tmp_path), seams=seams)
    # The finally wrote the (partial) report + emitted the summary.
    reports = list((results / "nightly").glob("*/report.json"))
    assert len(reports) == 1
    assert alerter.summaries  # summary still emitted
    # Review round 6 belt: the interrupted chain leaves NO outcomes — the
    # report must read SUSPECT (empty-outcomes tripwire), never a vacuous
    # all-green OK assembled from the empty initial tuples.
    written = json.loads(reports[0].read_text(encoding="utf-8"))
    assert written["ok"] is False
    assert written["suspect"] is True
    assert written["exit_code"] == 2
    assert any(w["name"] == "link_transition" for w in written["tripwires"])
    assert [s["verdict"] for s in alerter.summaries] == ["SUSPECT"]


# ── sweep link ───────────────────────────────────────────────────────────


def test_skip_sweep_reads_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lake, results = _lake(tmp_path)
    seams, _alerter, _pipeline, calls = _seams(monkeypatch, results)
    report = run_nightly(_plan(lake, results, tmp_path, skip_sweep=True), seams=seams)
    assert report.links[5].status is LinkStatus.SKIPPED
    assert calls["sweep"] == 0
    assert report.exit_code == 0


def test_sweep_exit_six_passes_through_with_coherent_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)
    payload = {**_COHERENT, "tuples_run": 4, "tuples_failed": 1}
    seams, _alerter, _pipeline, _calls = _seams(
        monkeypatch, results, sweep_exit=6, sweep_payload=payload
    )
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert report.links[5].status is LinkStatus.FAILED
    assert report.exit_code == 6  # the EXIT_SWEEP_TUPLE_FAILED pass-through
    assert not any(w.name == "sweep_counts_incoherent" for w in report.tripwires)


def test_sweep_refused_exit_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lake, results = _lake(tmp_path)
    seams, _alerter, _pipeline, _calls = _seams(
        monkeypatch, results, sweep_exit=1, sweep_payload=None
    )
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert report.links[5].status is LinkStatus.REFUSED
    assert report.exit_code == 1
    # No completion payload ⇒ the honest unknown note.
    assert any("sweep counts unknown" in note for note in report.honesty_notes)
    assert report.counts["sweep_run"] is None


@pytest.mark.parametrize(
    ("sweep_exit", "expected_status"),
    [
        pytest.param(1, LinkStatus.REFUSED, id="preflight-refusal-exit-1"),
        pytest.param(2, LinkStatus.FAILED, id="abort-exit-2"),
    ],
)
def test_sweep_without_own_completion_never_reports_prior_run_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sweep_exit: int,
    expected_status: LinkStatus,
) -> None:
    """Review round 3, findings 1+2: the supervisor chain is APPEND across
    nights, so night 1's SUPERVISOR_COMPLETE stays at the tail. Night 2's
    sweep is refused (exit 1 — e.g. the documented every-night SLA-overrun
    refusal, which returns BEFORE the supervisor audit is even opened) or
    aborted (exit 2, SUPERVISOR_ABORT, no COMPLETE): tonight's report must
    read honest-None ("?"), never night 1's tally presented as tonight's —
    which T5 could not catch (a prior night's numbers are internally
    coherent, and exit 1/2 with failed==0 trips nothing)."""
    lake, results = _lake(tmp_path)
    # Night 1's completion, stamped BEFORE tonight's fake clock (1000.0).
    prior = {**_COHERENT, "tuples_run": 100, "tuples_failed": 0, "tuples_total": 105}
    _write_sweep_completion(results, prior, ts_utc=500.0)
    # Tonight's sweep writes NO completion of its own.
    seams, _alerter, _pipeline, _calls = _seams(
        monkeypatch, results, sweep_exit=sweep_exit, sweep_payload=None
    )
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    sweep = report.links[5]
    assert sweep.status is expected_status
    assert sweep.counts == {}  # night 1's tally NEVER folded into tonight's link
    assert report.counts["sweep_run"] is None  # rendered "?", not 100
    assert report.counts["sweep_failed"] is None
    assert any("sweep counts unknown" in note for note in report.honesty_notes)
    # No fabricated-coherence tripwire either way — the counts are absent.
    assert not any(w.name == "sweep_counts_incoherent" for w in report.tripwires)


def test_sweep_t5_incoherent_total_marks_suspect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)
    payload = {**_COHERENT, "tuples_total": 99}
    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results, sweep_payload=payload)
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    wires = [w for w in report.tripwires if w.name == "sweep_counts_incoherent"]
    assert len(wires) == 1
    assert "99" in wires[0].detail
    assert report.exit_code == 2  # SUSPECT ⇒ cron-red


def test_sweep_t5_exit_zero_with_failures_marks_suspect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)
    payload = {**_COHERENT, "tuples_run": 4, "tuples_failed": 1}  # coherent total...
    seams, _alerter, _pipeline, _calls = _seams(
        monkeypatch, results, sweep_exit=0, sweep_payload=payload
    )
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    wires = [w for w in report.tripwires if w.name == "sweep_counts_incoherent"]
    assert len(wires) == 1
    assert "6 iff failed>0" in wires[0].detail


def test_sweep_commit_rekey_honesty_note(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lake, results = _lake(tmp_path)
    # A PRIOR sweep audit exists…
    _write_sweep_completion(results, _COHERENT)
    payload = {
        "tuples_total": 10,
        "tuples_run": 10,
        "skipped_cache": 0,  # …and tonight NOTHING cache-skipped.
        "skipped_resume": 0,
        "tuples_skipped_no_bars": 0,
        "tuples_failed": 0,
    }
    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results, sweep_payload=payload)
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert any("possible commit re-key" in note for note in report.honesty_notes)


def test_sweep_run_manifest_dedup_preferred(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)

    def sweep_with_manifest(**kwargs: Any) -> int:
        _write_sweep_completion(results, _COHERENT)
        run_dir = results / "runs" / "run-9"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(
            json.dumps({"run_id": "run-9", "dedup_skips": 7}), encoding="utf-8"
        )
        return 0

    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results)
    seams = NightlySeams(
        build_pipeline=seams.build_pipeline,
        run_sweep=sweep_with_manifest,
        alerter=seams.alerter,
        keep_awake_cm=_noop_keep_awake,
        credentials=seams.credentials,
        now=None or seams.now,
    )
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    sweep = report.links[5]
    assert sweep.counts["skipped_dedup_manifest"] == 7
    assert report.absent_artifacts == ("top100",)  # manifest found, top100 absent


# ── T2 audit reconcile (durable read-back) ──────────────────────────────


def test_t2_fires_when_an_audit_append_is_lost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)
    from brutex.nightly import report as report_mod

    real_open = report_mod.open_nightly_audit

    class _DroppingAudit:
        """Wraps the real writer but silently DROPS one LINK_DONE append."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.dropped = 0

        def append(self, *, action: str, subject: str, payload: dict[str, Any]) -> Any:
            if action == "LINK_DONE" and payload.get("link") == "disk" and not self.dropped:
                self.dropped += 1
                return None
            return self._inner.append(action=action, subject=subject, payload=payload)

        def close(self) -> None:
            self._inner.close()

    def dropping_open(results_root: Path) -> Any:
        return _DroppingAudit(real_open(results_root))

    monkeypatch.setattr("brutex.nightly.run.open_nightly_audit", dropping_open)
    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results)
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    wires = [w for w in report.tripwires if w.name == "audit_reconcile"]
    assert len(wires) == 1
    assert report.exit_code == 2  # SUSPECT is cron-red


@pytest.mark.parametrize(
    ("fail_action", "fail_link"),
    [
        pytest.param("LINK_DONE", "pull", id="raise-on-link-done"),
        # A lost LINK_START alone reconciles CLEAN under T2 (subset check) —
        # only the direct on_event tripwire catches it.
        pytest.param("LINK_START", "sweep", id="raise-on-link-start-t2-blind"),
    ],
)
def test_audit_append_failure_mid_chain_never_fabricates_green_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fail_action: str,
    fail_link: str,
) -> None:
    """Review round 6, MAJOR: `on_event` called `audit.append` UNGUARDED, and
    `run_chain` emits events OUTSIDE its per-link try — a mid-chain
    `SweepAuditWriteError` (disk-full LINK_DONE append) escaped `run_chain`,
    left `outcomes == ()`, and the `finally` assembled a fabricated all-green
    "✅ OK" Telegram while the exception then broke the always-returns-a-report
    contract. The lost line must degrade to SUSPECT, never a green OK."""
    lake, results = _lake(tmp_path)
    from brutex.nightly import report as report_mod

    real_open = report_mod.open_nightly_audit

    class _RaisingAudit:
        """Wraps the real writer; RAISES on one specific event append."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def append(self, *, action: str, subject: str, payload: dict[str, Any]) -> Any:
            if action == fail_action and payload.get("link") == fail_link:
                raise SweepAuditWriteError("audit append failed: No space left on device")
            return self._inner.append(action=action, subject=subject, payload=payload)

        def close(self) -> None:
            self._inner.close()

    monkeypatch.setattr(
        "brutex.nightly.run.open_nightly_audit",
        lambda results_root: _RaisingAudit(real_open(results_root)),
    )
    seams, alerter, _pipeline, calls = _seams(monkeypatch, results)
    with structlog.testing.capture_logs() as logs:
        report = run_nightly(_plan(lake, results, tmp_path), seams=seams)  # NO raise
    # The chain kept running past the failed append — every link executed.
    assert len(report.links) == 6
    assert calls["sweep"] == 1
    # The lost line is a SUSPECT verdict, never a green illusion.
    wires = [w for w in report.tripwires if w.name == "audit_reconcile"]
    assert any(f"audit append failed for {fail_action}" in w.detail for w in wires)
    assert report.ok is False
    assert report.suspect is True
    assert report.exit_code == 2  # cron-RED
    assert [s["verdict"] for s in alerter.summaries] == ["SUSPECT"]  # never "OK"
    assert any(log["event"] == "nightly.audit_append_failed" for log in logs)
    # The durable report still landed (the always-returns contract held).
    run_dir = nightly_dir(results, report.nightly_id)
    assert json.loads((run_dir / "report.json").read_text(encoding="utf-8"))["ok"] is False


def test_run_chain_raise_returns_suspect_report_never_green(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Belt for review round 6: `run_chain`'s contract is never-raise, but if
    it EVER does, `run_nightly` must still RETURN a non-green report (the
    empty initial `outcomes`/`tripwires` tuples previously assembled a vacuous
    all-green OK while the exception escaped past `cmd_nightly_run`)."""
    lake, results = _lake(tmp_path)

    def exploding_chain(**_kwargs: Any) -> Any:
        raise RuntimeError("sequencer defect")

    monkeypatch.setattr("brutex.nightly.run.run_chain", exploding_chain)
    seams, alerter, _pipeline, _calls = _seams(monkeypatch, results)
    with structlog.testing.capture_logs() as logs:
        report = run_nightly(_plan(lake, results, tmp_path), seams=seams)  # NO raise
    assert report.links == ()  # honest: nothing recorded
    wires = [w for w in report.tripwires if w.name == "link_transition"]
    assert any("run_chain raised RuntimeError" in w.detail for w in wires)
    assert report.ok is False
    assert report.suspect is True
    assert report.exit_code == 2
    assert [s["verdict"] for s in alerter.summaries] == ["SUSPECT"]
    assert any(log["event"] == "nightly.chain_aborted" for log in logs)
    run_dir = nightly_dir(results, report.nightly_id)
    assert json.loads((run_dir / "report.json").read_text(encoding="utf-8"))["suspect"] is True


# ── Telegram arms ────────────────────────────────────────────────────────


def test_disabled_telegram_chain_completes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """alerter=None resolves the real SweepAlerter; a None resolver ⇒ silent False."""
    lake, results = _lake(tmp_path)
    from brutex.notifications import sweep_alerts

    monkeypatch.setattr(sweep_alerts.SweepAlerter, "__init__", _disabled_alerter_init)
    seams, _fake, _pipeline, _calls = _seams(monkeypatch, results)
    seams = NightlySeams(
        build_pipeline=seams.build_pipeline,
        run_sweep=seams.run_sweep,
        alerter=None,  # exercises the lazy default arm
        keep_awake_cm=_noop_keep_awake,
        credentials=seams.credentials,
        now=seams.now,
    )
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert report.ok is True  # a silent Telegram never blocks the chain


def _disabled_alerter_init(self: Any, **kwargs: Any) -> None:
    self._explicit = None
    self._resolver = lambda: None


def test_summary_raise_is_suppressed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lake, results = _lake(tmp_path)
    seams, _alerter, _pipeline, _calls = _seams(
        monkeypatch, results, alerter=FakeAlerter(raise_on_summary=True)
    )
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert report.ok is True  # a broken alert path can never fail the chain


# ── dry run ──────────────────────────────────────────────────────────────


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_dry_run_takes_no_locks_and_mutates_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)
    before = _tree(tmp_path)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("dry-run must not touch locks")

    monkeypatch.setattr(ps, "nightly_singleflight_lock", forbidden)
    monkeypatch.setattr(ps, "pull_singleflight_lock", forbidden)
    monkeypatch.setattr(ps, "account_pull_singleflight_lock", forbidden)
    seams, alerter, _pipeline, calls = _seams(monkeypatch, results)
    report = run_nightly(_plan(lake, results, tmp_path, dry_run=True), seams=seams)
    assert report.dry_run is True
    assert report.exit_code == 0
    statuses = {o.link.value: o.status for o in report.links}
    assert statuses["collision"] is LinkStatus.OK
    assert statuses["disk"] is LinkStatus.OK
    assert statuses["backup"] is LinkStatus.SKIPPED
    assert statuses["pull"] is LinkStatus.SKIPPED
    assert statuses["prebuild"] is LinkStatus.SKIPPED
    assert statuses["sweep"] is LinkStatus.SKIPPED
    assert calls["build"] == 0
    assert calls["sweep"] == 0
    assert alerter.summaries == []  # the report is RETURNED, not broadcast
    assert _tree(tmp_path) == before  # ZERO mutation — the tree oracle


def test_dry_run_live_sweep_still_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lake, results = _lake(tmp_path)
    progress = results / "progress" / "sweep-progress.json"
    progress.parent.mkdir(parents=True)
    progress.write_text('{"state": "running"}', encoding="utf-8")
    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results)
    seams = NightlySeams(
        build_pipeline=seams.build_pipeline,
        run_sweep=seams.run_sweep,
        alerter=seams.alerter,
        keep_awake_cm=_noop_keep_awake,
        credentials=seams.credentials,
    )
    report = run_nightly(_plan(lake, results, tmp_path, dry_run=True), seams=seams)
    assert report.links[0].status is LinkStatus.REFUSED
    assert report.exit_code == 1


def test_dry_run_disk_floor_breach_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lake, results = _lake(tmp_path)
    monkeypatch.setenv("BRUTEX_NIGHTLY_MIN_FREE_LAKE_BYTES", str(10**18))
    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results)
    report = run_nightly(_plan(lake, results, tmp_path, dry_run=True), seams=seams)
    assert report.links[1].status is LinkStatus.REFUSED
    assert report.exit_code == 1


def test_keep_awake_wraps_the_whole_chain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lake, results = _lake(tmp_path)
    marks: list[str] = []

    @contextmanager
    def recording_keep_awake(reason: str) -> Generator[bool]:
        marks.append(f"enter:{reason}")
        yield False
        marks.append("exit")

    def marking_sweep(**kwargs: Any) -> int:
        marks.append("sweep")
        _write_sweep_completion(results, _COHERENT)
        return 0

    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results)
    seams = NightlySeams(
        build_pipeline=seams.build_pipeline,
        run_sweep=marking_sweep,
        alerter=seams.alerter,
        keep_awake_cm=recording_keep_awake,
        credentials=seams.credentials,
        now=seams.now,
    )
    run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert marks[0] == "enter:brutex nightly"
    assert marks[-1] == "exit"
    assert "sweep" in marks  # the SWEEP leg ran INSIDE the keep-awake window


def test_default_seams_are_production_shapes() -> None:
    """The zero-arg seams resolve to the real production callables."""
    from brutex.nightly.pipeline_binding import build_headless_pipeline
    from brutex.nightly.run import _resolve_alerter, _resolve_credentials, _resolve_run_sweep
    from brutex.notifications.sweep_alerts import SweepAlerter

    seams = NightlySeams()
    assert seams.build_pipeline is build_headless_pipeline
    assert isinstance(_resolve_alerter(seams), SweepAlerter)
    from brutex import cli

    assert _resolve_run_sweep(seams) is cli.cmd_sweep_all
    assert _resolve_credentials.__name__ == "_resolve_credentials"  # importable production seam


# ── remaining seam arms ─────────────────────────────────────────────────


def test_default_keep_awake_is_the_real_helper() -> None:
    from brutex.nightly.run import _default_keep_awake

    with _default_keep_awake("brutex nightly test"):
        pass  # non-darwin: a logged no-op — the production default is callable


def test_t4_manifest_reread_failure_marks_suspect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)

    class _BrokenManifest:
        @staticmethod
        def from_dict(_data: object) -> object:
            raise ValueError("manifest parse exploded")

    monkeypatch.setattr("brutex.nightly.run.BackupManifest", _BrokenManifest)
    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results)
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    wires = [w for w in report.tripwires if w.name == "snapshot_unreadable"]
    assert len(wires) == 1
    assert "re-read failed" in wires[0].detail
    assert report.exit_code == 2


def test_run_manifest_with_junk_dedup_not_folded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _lake(tmp_path)

    def sweep_with_junk_manifest(**kwargs: Any) -> int:
        _write_sweep_completion(results, _COHERENT)
        run_dir = results / "runs" / "run-x"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(
            json.dumps({"dedup_skips": "seven"}), encoding="utf-8"
        )
        return 0

    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results)
    seams = NightlySeams(
        build_pipeline=seams.build_pipeline,
        run_sweep=sweep_with_junk_manifest,
        alerter=seams.alerter,
        keep_awake_cm=_noop_keep_awake,
        credentials=seams.credentials,
        now=seams.now,
    )
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert "skipped_dedup_manifest" not in report.links[5].counts


def test_t5_partial_payload_still_checks_exit_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A payload missing an identity part skips the total check, keeps the exit law."""
    lake, results = _lake(tmp_path)
    payload = {"tuples_total": 5, "tuples_run": 5, "tuples_failed": 0}  # parts incomplete
    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results, sweep_payload=payload)
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert not any(w.name == "sweep_counts_incoherent" for w in report.tripwires)
    assert report.exit_code == 0


def test_own_audit_events_parser_arms(tmp_path: Path) -> None:
    from brutex.nightly.run import _own_audit_events

    path = tmp_path / "nightly.jsonl"
    path.write_text(
        '{"action": "LINK_START", "subject": "nightly:me", "payload": {"link": "disk"}}\n'
        "{torn line\n"
        '["not", "a", "dict"]\n'
        '{"action": "LINK_START", "subject": "nightly:OTHER", "payload": {"link": "pull"}}\n'
        '{"action": "NIGHTLY_START", "subject": "nightly:me", "payload": {}}\n',
        encoding="utf-8",
    )
    assert _own_audit_events(path, "me") == [("LINK_START", "disk"), ("NIGHTLY_START", None)]
    assert _own_audit_events(tmp_path / "absent.jsonl", "me") == []


def test_resolve_credentials_delegates_to_aws_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brutex.nightly.run import _resolve_credentials

    sentinel = SimpleNamespace(api_key="aws-key")
    monkeypatch.setattr("brutex.config.credentials.resolve_groww_aws_credentials", lambda: sentinel)
    assert _resolve_credentials() is sentinel


def test_box_update_block_rides_the_full_six_link_composition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """§10 test 13 — the box_update + env_preflight blocks ride a FULL
    ``run_nightly`` composition (all six links faked green) into the durable
    ``report.json``, and the six R42 links are BREAKER-FREE w.r.t. the leg —
    even a rolled_back block never blocks/blocks-by/refuses any link
    (DEC-BOX-AUTODEPLOY-LANE-001 §5/§6 — the leg is pre-chain, never a link).
    """
    from brutex.nightly.box_update import BoxUpdateOutcome, BoxUpdateStatus

    lake, results = _lake(tmp_path)
    seams, alerter, _pipeline, calls = _seams(monkeypatch, results)
    block = BoxUpdateOutcome(
        status=BoxUpdateStatus.ROLLED_BACK,
        from_sha="a" * 40,
        to_sha="b" * 40,
        running_sha="a" * 40,
        skip_reason=None,
        delta_rust=True,
        delta_pyproject=False,
        wheels=(),
        gate="stale",
        repair="rebuilt",
        pip_restore="not_needed",
        lock_restored=(),
        quarantined=(),
        alarm_signature="rolled_back||stale|rebuilt|not_needed|",
        alarm_repeats=0,
        reexec=False,
        duration_s=1.0,
        detail="",
    ).to_dict()
    preflight = {"flagged": [], "dotenv_errors": []}
    plan = _plan(lake, results, tmp_path, box_update=block, env_preflight=preflight)
    report = run_nightly(plan, seams=seams)
    # The six links are UNAFFECTED by the (non-green) leg block — breaker-free.
    assert [o.status for o in report.links] == [LinkStatus.OK] * 6
    assert report.ok is True
    assert report.exit_code == 0
    assert calls["sweep"] == 1
    # The blocks land VERBATIM in the durable report.json (the §5 contract).
    on_disk = json.loads(
        (nightly_dir(results, report.nightly_id) / "report.json").read_text(encoding="utf-8")
    )
    assert on_disk["box_update"] == block
    assert on_disk["env_preflight"] == preflight
    # The 📦 honesty-note line rides the report for the non-quiet status.
    assert any(n.startswith("📦 box-update: rolled_back") for n in report.honesty_notes)
    # The one Telegram summary still fired with its ordinary verdict.
    assert [s["verdict"] for s in alerter.summaries] == ["OK"]


def test_box_update_note_quiet_statuses_are_silent() -> None:
    """§5 — the 📦 note is SILENT on absent/up_to_date/disabled blocks (the
    quiet statuses carry no operator-actionable news)."""
    from brutex.nightly.run import _box_update_note

    assert _box_update_note(None) is None
    assert _box_update_note({"status": "up_to_date"}) is None
    assert _box_update_note({"status": "disabled"}) is None
    note = _box_update_note({"status": "updated", "wheels": "junk-not-a-list"})
    assert note == "📦 box-update: updated ?→? · wheels ok 0/0 · gate skipped"


# ── LANE W5: decay + champions adaptation legs (report-level blocks) ─────


def test_wet_run_carries_decay_and_champions_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A wet run RUNS both guarded legs post-chain and threads their blocks
    into the report + report.json (the #1939 report-level-block law). Over a
    lake with no top100 artifact both legs read the honest quiet skip — and
    the quiet statuses add NO honesty-note line."""
    lake, results = _lake(tmp_path)
    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results)
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert report.decay == {"status": "skipped", "reason": "no_top100_artifact"}
    assert report.champions == {"status": "skipped", "reason": "no_top100_artifact"}
    on_disk = json.loads(
        (nightly_dir(results, report.nightly_id) / "report.json").read_text(encoding="utf-8")
    )
    assert on_disk["decay"] == {"status": "skipped", "reason": "no_top100_artifact"}
    assert on_disk["champions"] == {"status": "skipped", "reason": "no_top100_artifact"}
    assert not any(n.startswith(("📉", "🏆")) for n in report.honesty_notes)


def test_dry_run_never_runs_the_adaptation_legs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Dry-runs carry None blocks — the legs are NEVER invoked (the
    box_update dry-run law)."""
    lake, results = _lake(tmp_path)
    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results)

    def _boom(**kwargs: Any) -> dict[str, object]:
        raise AssertionError("the decay leg must not run on a dry-run")

    monkeypatch.setattr("brutex.nightly.decay.run_decay_leg_guarded", _boom)
    monkeypatch.setattr("brutex.nightly.champions.run_champions_leg_guarded", _boom)
    report = run_nightly(_plan(lake, results, tmp_path, dry_run=True), seams=seams)
    assert report.decay is None
    assert report.champions is None


def test_decay_note_arms() -> None:
    """The 📉 one-liner: silent on absent/disabled/skipped/quiet-ok; fires on
    DECAYED entries and on error statuses."""
    from brutex.nightly.run import _decay_note

    assert _decay_note(None) is None
    assert _decay_note({"status": "disabled"}) is None
    assert _decay_note({"status": "skipped", "reason": "no_top100_artifact"}) is None
    assert _decay_note({"status": "ok", "decayed": 0, "checked": 3}) is None
    assert _decay_note({"status": "ok", "checked": 3}) is None  # decayed absent
    note = _decay_note(
        {
            "status": "ok",
            "decayed": 2,
            "checked": 5,
            "run_id": "a" * 64,
            "window_sessions": 63,
        }
    )
    assert note is not None
    assert note.startswith("📉 decay: 2/5")
    assert "trailing 63 sessions" in note
    err = _decay_note({"status": "error", "error": "boom"})
    assert err == "📉 decay leg: error — boom"


def test_champions_note_arms() -> None:
    """The 🏆 one-liner: silent on quiet statuses + transition-free nights;
    fires on promoted/seeded/retire-flagged and on error statuses."""
    from brutex.nightly.run import _champions_note

    assert _champions_note(None) is None
    assert _champions_note({"status": "disabled"}) is None
    assert _champions_note({"status": "skipped", "reason": "no_top100_artifact"}) is None
    assert _champions_note({"status": "ok", "transitions": []}) is None
    assert _champions_note({"status": "ok", "transitions": "junk"}) is None
    quiet = _champions_note({"status": "ok", "transitions": [{"action": "RETAINED"}, "junk"]})
    assert quiet is None  # RETAINED-only nights are the steady state
    note = _champions_note(
        {
            "status": "ok",
            "transitions": [
                {"action": "PROMOTED"},
                {"action": "SEEDED"},
                {"action": "RETIRE_FLAGGED"},
            ],
        }
    )
    assert note == (
        "🏆 champions: 1 promoted · 1 seeded · 1 retire-flagged (ledger champions/champions.jsonl)"
    )
    err = _champions_note({"status": "env_error", "error": "junk env"})
    assert err == "🏆 champions leg: env_error — junk env"


def test_wet_run_notes_surface_noteworthy_leg_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Noteworthy leg blocks land as honesty-note lines on the report."""
    lake, results = _lake(tmp_path)
    seams, _alerter, _pipeline, _calls = _seams(monkeypatch, results)
    monkeypatch.setattr(
        "brutex.nightly.decay.run_decay_leg_guarded",
        lambda **kwargs: {
            "status": "ok",
            "decayed": 1,
            "checked": 2,
            "run_id": "a" * 64,
            "window_sessions": 63,
        },
    )
    monkeypatch.setattr(
        "brutex.nightly.champions.run_champions_leg_guarded",
        lambda **kwargs: {"status": "ok", "transitions": [{"action": "SEEDED"}]},
    )
    report = run_nightly(_plan(lake, results, tmp_path), seams=seams)
    assert any(n.startswith("📉 decay: 1/2") for n in report.honesty_notes)
    assert any(n.startswith("🏆 champions: 0 promoted · 1 seeded") for n in report.honesty_notes)
