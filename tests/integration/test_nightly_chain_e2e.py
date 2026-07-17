"""End-to-end nightly chain over a fixture lake (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

Full ``run_nightly`` with REAL locks, REAL backup snapshots and a REAL
hash-chained audit over injected pipeline/sweep fakes: the green path, the
pull-halt path (snapshot sealed + INTACT, later links BLOCKED, exit 2), the
collision path (a second flock holder → refusal + ``nightly_refused`` on a
recording alerter, exit 1), the crashed-peer recovery (a REAL subprocess
KILLED while holding the nightly flock + a stale ``.partial`` → the next run
acquires cleanly, prunes the orphan, and RESUMES the audit chain green), and
the SUSPECT path (tampered sweep counters ⇒ exit 2).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from brutex.adapters.groww import pull_singleflight as ps
from brutex.config.credentials_types import CredentialsError
from brutex.data.errors import LakeWriteHalt
from brutex.engine.sweep_audit import SweepAuditWriter, verify_chain
from brutex.engine.sweep_supervisor import SUPERVISOR_ACTION_COMPLETE
from brutex.nightly.chain import LinkStatus
from brutex.nightly.report import nightly_audit_path, nightly_dir
from brutex.nightly.run import NightlyPlan, NightlySeams, run_nightly


@pytest.fixture(autouse=True)
def _hermetic_disk_floors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin both disk floors to 0 so the e2e chain never reads the CI
    machine's REAL free disk (the 20 GiB lake default refused the DISK link
    on a low-disk GitHub runner and BLOCKED every downstream link — the
    2026-07-08 run-28948028949 red; test_cli_nightly.py already pins)."""
    monkeypatch.setenv("BRUTEX_NIGHTLY_MIN_FREE_LAKE_BYTES", "0")
    monkeypatch.setenv("BRUTEX_NIGHTLY_MIN_FREE_BACKUP_BYTES", "0")


class RecordingAlerter:
    def __init__(self) -> None:
        self.summaries: list[dict[str, Any]] = []
        self.refused: list[dict[str, Any]] = []

    def nightly_summary(self, **kwargs: Any) -> bool:
        self.summaries.append(kwargs)
        return True

    def nightly_refused(self, **kwargs: Any) -> bool:
        self.refused.append(kwargs)
        return True


@dataclass
class _FakeHistorical:
    bars_appended: int = 42


@dataclass
class _FakeComposite:
    historical: _FakeHistorical | None = field(default_factory=_FakeHistorical)


@dataclass
class _FakePipeReport:
    ok: bool = True
    note: str = "pipeline complete"
    stopped_after: str | None = None
    quarantined_count: int = 0
    historical: _FakeComposite | None = field(default_factory=_FakeComposite)
    prebuild_built: int | None = 2
    prebuild_crashed: str | None = None
    prebuild_disk_guarded: bool = False
    prebuild_failed: tuple[str, ...] = ()
    post_steps_strict_skipped: bool = False


class FakePipeline:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


@contextmanager
def _noop_keep_awake(reason: str) -> Generator[None]:
    yield


def _fixture_lake(tmp_path: Path) -> tuple[Path, Path]:
    lake = tmp_path / "lake"
    results = lake / "sweeps"
    (lake / "window_ledger" / "deltas").mkdir(parents=True)
    (lake / "window_ledger" / "ledger.parquet").write_bytes(b"LEDGER-BYTES")
    (lake / "window_ledger" / "deltas" / "d1.parquet").write_bytes(b"DELTA-1")
    (lake / "registry.duckdb").write_bytes(b"REGISTRY")
    (lake / "instruments.duckdb").write_bytes(b"INSTRUMENTS")
    (results / "NIFTY" / "5minute").mkdir(parents=True)
    (results / "NIFTY" / "5minute" / "BUY_k1_x.parquet").write_bytes(b"TUPLE")
    return lake, results


def _plan(lake: Path, results: Path, tmp_path: Path, **overrides: Any) -> NightlyPlan:
    base: dict[str, Any] = {
        "lake_root": lake,
        "results_root": results,
        "backup_root": tmp_path / "backups",
        "keep": 7,
        "from_date": None,
        "to_date": None,
        "sweep_kwargs": {"symbols": "NIFTY"},
    }
    base.update(overrides)
    return NightlyPlan(**base)


def _seams(
    monkeypatch: pytest.MonkeyPatch,
    results: Path,
    alerter: RecordingAlerter,
    *,
    execute_raises: BaseException | None = None,
    sweep_payload: dict[str, Any] | None = None,
) -> NightlySeams:
    pipeline = FakePipeline()

    def fake_execute(_hp: object) -> object:
        if execute_raises is not None:
            raise execute_raises
        return _FakePipeReport()

    monkeypatch.setattr("brutex.nightly.run.execute_headless_pipeline", fake_execute)

    payload = sweep_payload or {
        "tuples_total": 4,
        "tuples_run": 2,
        "skipped_cache": 2,
        "skipped_resume": 0,
        "tuples_skipped_no_bars": 0,
        "tuples_failed": 0,
    }

    def run_sweep(**_kwargs: Any) -> int:
        # DEC-SWEEP-LOCK-PULL-CONCURRENCY-001 (fails before): during the SWEEP
        # leg the LAKE pull lock must be FREE — a REAL EX acquire on a second
        # open file description succeeds, proving a daily pull/crossverify
        # would NOT defer behind the sweep's multi-day compute (pre-fix the
        # nightly held the lake flock through this leg, so this acquire raised
        # PullAlreadyRunningError — and the real cmd_sweep_all's G10 SH guard
        # self-conflicted the same hold at preload).
        with ps.pull_singleflight_lock(results.parent):
            pass
        # Production shape: the SUPERVISOR audit chain (SweepGuards.audit →
        # audit/sweep-all-supervisor.jsonl) with the REAL engine action
        # constant — never the ingest sweep-all.jsonl (review round 1).
        path = results / "audit" / "sweep-all-supervisor.jsonl"
        with SweepAuditWriter.open(path, actor="sweep", resume=path.exists()) as audit:
            audit.append(action=SUPERVISOR_ACTION_COMPLETE, subject="sweep:e2e", payload=payload)
        return 0

    def no_creds() -> Any:
        raise CredentialsError("no AWS in the e2e fixture")

    return NightlySeams(
        build_pipeline=cast(Any, lambda **_kw: pipeline),
        run_sweep=run_sweep,
        alerter=cast(Any, alerter),
        keep_awake_cm=_noop_keep_awake,
        credentials=no_creds,
    )


def test_e2e_green_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lake, results = _fixture_lake(tmp_path)
    alerter = RecordingAlerter()
    report = run_nightly(
        _plan(lake, results, tmp_path), seams=_seams(monkeypatch, results, alerter)
    )
    assert report.exit_code == 0
    assert report.ok is True
    # The counts were read from the SUPERVISOR audit (review round 1: the
    # reader pointed at the ingest sweep-all.jsonl and rendered "?" forever).
    assert report.counts["sweep_run"] == 2
    assert report.counts["sweep_skipped_cache"] == 2
    # Durable artifacts: report + bundle + a VERIFIED audit chain.
    run_dir = nightly_dir(results, report.nightly_id)
    on_disk = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert on_disk["exit_code"] == 0
    assert on_disk["schema"] == "brutex-nightly-report-v1"
    assert (run_dir / "bundle" / "versions.json").exists()
    assert verify_chain(nightly_audit_path(results)) >= 8  # start + links + done
    # The REAL snapshot sealed under the backup root.
    snapshot = tmp_path / "backups" / report.nightly_id
    assert (snapshot / "manifest.json").exists()
    assert (snapshot / "lake" / "window_ledger" / "ledger.parquet").read_bytes() == b"LEDGER-BYTES"
    assert [s["verdict"] for s in alerter.summaries] == ["OK"]
    assert alerter.refused == []
    # Gap-register row 4 pin: the nightly writes NO .parquet ANYWHERE under
    # <results_root>/nightly/ — /results discovery, the sweep-verify doctor
    # and results_report glob every *.parquet under the results tree, and a
    # foreign parquet there raises an unguarded KeyError → /results 500s.
    assert list((results / "nightly").rglob("*.parquet")) == []

    # Ruling 17 naming pin: no nightly report field is a bare "run_id" — an
    # ENGINE run id must be named sweep_run_id (nightly_id stays nightly_id).
    def _keys(node: object) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            for key, value in node.items():
                found.add(str(key))
                found |= _keys(value)
        elif isinstance(node, list):
            for value in node:
                found |= _keys(value)
        return found

    report_keys = _keys(on_disk)
    assert "nightly_id" in report_keys
    assert not any(
        k == "run_id" or (k.endswith("_run_id") and k != "sweep_run_id") for k in report_keys
    )


def test_e2e_pull_halt_snapshot_intact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lake, results = _fixture_lake(tmp_path)
    alerter = RecordingAlerter()
    halted: list[BaseException] = []
    monkeypatch.setattr("brutex.notifications.sweep_alerts.emit_pull_halt_alert", halted.append)
    report = run_nightly(
        _plan(lake, results, tmp_path),
        seams=_seams(monkeypatch, results, alerter, execute_raises=LakeWriteHalt("disk_full")),
    )
    assert report.exit_code == 2
    statuses = {o.link.value: o.status for o in report.links}
    assert statuses["backup"] is LinkStatus.OK  # tonight's snapshot landed BEFORE the halt
    assert statuses["pull"] is LinkStatus.FAILED
    assert statuses["prebuild"] is LinkStatus.BLOCKED
    assert statuses["sweep"] is LinkStatus.BLOCKED
    assert len(halted) == 1
    # The pre-mutation snapshot is sealed + intact — the rollback point stands.
    snapshot = tmp_path / "backups" / report.nightly_id
    assert (snapshot / "lake" / "window_ledger" / "ledger.parquet").read_bytes() == b"LEDGER-BYTES"
    assert [s["verdict"] for s in alerter.summaries] == ["FAILED"]


def test_e2e_collision_second_holder_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lake, results = _fixture_lake(tmp_path)
    alerter = RecordingAlerter()
    with ps.nightly_singleflight_lock(lake, holder_note="nightly:the-peer"):
        report = run_nightly(
            _plan(lake, results, tmp_path), seams=_seams(monkeypatch, results, alerter)
        )
    assert report.exit_code == 1
    assert report.links[0].status is LinkStatus.REFUSED
    assert "(holder: nightly:the-peer)" in report.links[0].detail
    [refusal] = alerter.refused
    assert refusal["link"] == "collision"
    assert [s["verdict"] for s in alerter.summaries] == ["REFUSED"]
    # Nothing was backed up / swept.
    assert not (tmp_path / "backups").exists()


def test_e2e_crashed_peer_recovery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A SIGKILLed peer leaves NO stale lock state; its orphaned .partial is pruned.

    The peer is a REAL process holding the nightly flock (the kernel releases
    it on death — no pid files anywhere in this design) that also left a
    stale ``.partial`` staging dir from its interrupted backup.
    """
    lake, results = _fixture_lake(tmp_path)
    backup_root = tmp_path / "backups"
    stale_partial = backup_root / "nightly-CRASHED.partial"
    stale_partial.mkdir(parents=True)
    old = time.time() - 100_000.0  # > the 24 h GC window
    os.utime(stale_partial, (old, old))

    child_src = textwrap.dedent(
        """
        import fcntl, os, sys
        from pathlib import Path

        lock = Path(sys.argv[1]) / ".locks" / "nightly_singleflight.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print("held", flush=True)
        import time as _t
        _t.sleep(300)
        """
    )
    with subprocess.Popen(  # fixed argv, test-owned script — no shell
        [sys.executable, "-c", child_src, str(lake)], stdout=subprocess.PIPE, text=True
    ) as child:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "held"
        child.send_signal(signal.SIGKILL)  # the 2 a.m. power cut
        child.wait(timeout=30)

    # A pre-existing audit chain from an earlier night RESUMES cleanly.
    with SweepAuditWriter.open(nightly_audit_path(results), actor="nightly") as audit:
        audit.append(action="NIGHTLY_START", subject="nightly:earlier", payload={})

    alerter = RecordingAlerter()
    report = run_nightly(
        _plan(lake, results, tmp_path), seams=_seams(monkeypatch, results, alerter)
    )
    assert report.exit_code == 0  # the kernel released the dead peer's flock
    assert not stale_partial.exists()  # the >24h orphan was garbage-collected
    assert verify_chain(nightly_audit_path(results)) >= 9  # resumed chain still green


def test_e2e_suspect_tampered_counts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lake, results = _fixture_lake(tmp_path)
    alerter = RecordingAlerter()
    tampered = {
        "tuples_total": 999,  # cannot equal the parts sum
        "tuples_run": 1,
        "skipped_cache": 0,
        "skipped_resume": 0,
        "tuples_skipped_no_bars": 0,
        "tuples_failed": 0,
    }
    report = run_nightly(
        _plan(lake, results, tmp_path),
        seams=_seams(monkeypatch, results, alerter, sweep_payload=tampered),
    )
    assert report.suspect is True
    assert report.exit_code == 2  # SUSPECT is cron-RED
    assert any(w.name == "sweep_counts_incoherent" for w in report.tripwires)
    assert [s["verdict"] for s in alerter.summaries] == ["SUSPECT"]
    on_disk = json.loads(
        (nightly_dir(results, report.nightly_id) / "report.json").read_text(encoding="utf-8")
    )
    assert on_disk["suspect"] is True
