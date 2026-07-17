"""CLI wiring tests for the box-update lane (DEC-BOX-AUTODEPLOY-LANE-001 §6/§8).

Covers the standalone ``brutex box-update`` mouth (exit-code law, flock
posture, fail-loud junk env pre-git) and the ``cmd_nightly_run`` pre-chain
threading (plan blocks, adopt path, re-exec wiring, junk-env pre-chain
refusal, dry-run inertness).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from brutex import cli
from brutex.nightly import box_update as bu
from brutex.nightly.box_update import (
    BOX_UPDATE_STATE_ENV,
    BoxUpdateOutcome,
    BoxUpdateStatus,
)
from brutex.nightly.chain import LINK_ORDER, LinkOutcome, LinkStatus, NightlyReport
from structlog.testing import capture_logs


def _mini_lake(tmp_path: Path) -> Path:
    lake = tmp_path / "lake"
    (lake / "sweeps").mkdir(parents=True)
    return lake


def _outcome(status: BoxUpdateStatus) -> BoxUpdateOutcome:
    return BoxUpdateOutcome(
        status=status,
        from_sha="a" * 40,
        to_sha="b" * 40,
        running_sha="a" * 40,
        skip_reason="dirty_tree" if status is BoxUpdateStatus.SKIPPED else None,
        delta_rust=False,
        delta_pyproject=False,
        wheels=(),
        gate="skipped",
        repair="not_needed",
        pip_restore="not_needed",
        lock_restored=(),
        quarantined=(),
        alarm_signature=None,
        alarm_repeats=0,
        reexec=False,
        duration_s=0.1,
        detail="",
    )


def _green_report(results_root: Path) -> NightlyReport:
    return NightlyReport(
        schema_version=1,
        nightly_id="nightly-box-test",
        lake_root="/lake",
        results_root=str(results_root),
        backup_root="/backups",
        started_utc=0.0,
        ended_utc=61.0,
        links=tuple(
            LinkOutcome(
                link=link,
                status=LinkStatus.OK,
                started_utc=0.0,
                ended_utc=1.0,
                detail="",
                counts={},
                error_type=None,
                blocked_by=None,
                exit_hint=None,
            )
            for link in LINK_ORDER
        ),
        tripwires=(),
        counts={},
        snapshot_id=None,
        absent_artifacts=(),
        honesty_notes=(),
        dry_run=False,
    )


class TestStandaloneCmd:
    def test_parser_has_box_update(self) -> None:
        with mock.patch.object(cli, "cmd_box_update", return_value=0) as spy:
            rc = cli.main(["box-update", "--lake-root", "/tmp/x"])
        assert rc == 0
        assert spy.call_args.args[0].lake_root == Path("/tmp/x")

    def test_junk_kill_switch_fails_loud_before_any_git(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # §10 test 22(d) — junk BRUTEX_BOX_UPDATE ⇒ EXIT_USER_ERROR + stderr
        # BEFORE any git argv (run_box_update never invoked).
        lake = _mini_lake(tmp_path)
        monkeypatch.setenv("BRUTEX_BOX_UPDATE", "banana")
        with mock.patch.object(bu, "run_box_update") as leg:
            rc = cli.main(["box-update", "--lake-root", str(lake)])
        assert rc == cli.EXIT_USER_ERROR
        assert leg.call_count == 0
        assert "BRUTEX_BOX_UPDATE" in capsys.readouterr().err

    def test_disabled_kill_switch_is_exit_ok(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # §10 test 22(d) — an honored kill switch is not an error.
        lake = _mini_lake(tmp_path)
        monkeypatch.setenv("BRUTEX_BOX_UPDATE", "off")
        with mock.patch.object(
            bu, "run_box_update", return_value=_outcome(BoxUpdateStatus.DISABLED)
        ):
            rc = cli.main(["box-update", "--lake-root", str(lake)])
        assert rc == cli.EXIT_OK
        assert '"status": "disabled"' in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (BoxUpdateStatus.UPDATED, cli.EXIT_OK),
            (BoxUpdateStatus.UP_TO_DATE, cli.EXIT_OK),
            (BoxUpdateStatus.DISABLED, cli.EXIT_OK),
            (BoxUpdateStatus.SKIPPED, cli.EXIT_USER_ERROR),
            (BoxUpdateStatus.ROLLED_BACK, cli.EXIT_INTERNAL_ERROR),
            (BoxUpdateStatus.ERROR, cli.EXIT_INTERNAL_ERROR),
        ],
    )
    def test_exit_code_law(self, tmp_path: Path, status: BoxUpdateStatus, expected: int) -> None:
        lake = _mini_lake(tmp_path)
        with mock.patch.object(bu, "run_box_update", return_value=_outcome(status)):
            rc = cli.main(["box-update", "--lake-root", str(lake)])
        assert rc == expected

    def test_missing_lake_is_user_error(self, tmp_path: Path) -> None:
        rc = cli.main(["box-update", "--lake-root", str(tmp_path / "absent")])
        assert rc == cli.EXIT_USER_ERROR

    def test_flock_contention_refuses_loudly_zero_leg(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # §10 test 22(a)/(c) — a held nightly single-flight lock ⇒ loud
        # EXIT_USER_ERROR, the leg never runs (zero git argv), never a hang.
        from brutex.adapters.groww.pull_singleflight import nightly_singleflight_lock

        lake = _mini_lake(tmp_path)
        with (
            nightly_singleflight_lock(lake, holder_note="test-competitor"),
            mock.patch.object(bu, "run_box_update") as leg,
        ):
            rc = cli.main(["box-update", "--lake-root", str(lake)])
        assert rc == cli.EXIT_USER_ERROR
        assert leg.call_count == 0
        assert "single-flight" in capsys.readouterr().err

    def test_lock_released_after_run(self, tmp_path: Path) -> None:
        # §10 test 22(b) — a follow-on acquisition of the same lock succeeds.
        from brutex.adapters.groww.pull_singleflight import nightly_singleflight_lock

        lake = _mini_lake(tmp_path)
        with mock.patch.object(
            bu, "run_box_update", return_value=_outcome(BoxUpdateStatus.UP_TO_DATE)
        ):
            assert cli.main(["box-update", "--lake-root", str(lake)]) == cli.EXIT_OK
        with nightly_singleflight_lock(lake, holder_note="post-run"):
            pass  # acquires cleanly — the cmd released on exit

    def test_state_dir_is_the_lake_sidecar(self, tmp_path: Path) -> None:
        lake = _mini_lake(tmp_path)
        with mock.patch.object(
            bu, "run_box_update", return_value=_outcome(BoxUpdateStatus.UP_TO_DATE)
        ) as leg:
            cli.main(["box-update", "--lake-root", str(lake)])
        assert leg.call_args.kwargs["state_dir"] == lake / ".box_update"
        assert leg.call_args.kwargs["runner"] is bu.default_command_runner


class TestNightlyThreading:
    def _run_nightly_spy(self, monkeypatch: pytest.MonkeyPatch, results: Path) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def fake_run_nightly(plan: Any, *, seams: Any = None) -> NightlyReport:
            captured["plan"] = plan
            return _green_report(results)

        monkeypatch.setattr("brutex.nightly.run.run_nightly", fake_run_nightly)
        return captured

    def test_disabled_leg_block_and_preflight_thread_into_the_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The conftest belt keeps BRUTEX_BOX_UPDATE=0 — the leg runs its
        # zero-action DISABLED arm and the honest block still lands (§7).
        lake = _mini_lake(tmp_path)
        captured = self._run_nightly_spy(monkeypatch, lake / "sweeps")
        rc = cli.main(["nightly", "run", "--lake-root", str(lake), "--skip-sweep"])
        assert rc == 0
        plan = captured["plan"]
        assert plan.box_update is not None
        assert plan.box_update["status"] == "disabled"
        assert plan.box_update["gate"] == "skipped"
        assert plan.env_preflight is not None
        assert set(plan.env_preflight) == {"flagged", "dotenv_errors"}

    def test_junk_kill_switch_is_a_clean_pre_chain_refusal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        lake = _mini_lake(tmp_path)
        monkeypatch.setenv("BRUTEX_BOX_UPDATE", "banana")
        called = {"n": 0}
        monkeypatch.setattr(
            "brutex.nightly.run.run_nightly",
            lambda *_a, **_k: called.__setitem__("n", called["n"] + 1),
        )
        rc = cli.main(["nightly", "run", "--lake-root", str(lake)])
        assert rc == cli.EXIT_USER_ERROR
        assert called["n"] == 0  # nothing ran — the fail-loud pre-chain block
        assert "BRUTEX_BOX_UPDATE" in capsys.readouterr().err

    def test_adopt_path_consumes_the_sentinel_and_skips_the_leg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # §6 step 2 — sentinel present ⇒ we ARE the child: adopt, never re-run.
        lake = _mini_lake(tmp_path)
        captured = self._run_nightly_spy(monkeypatch, lake / "sweeps")
        parent = _outcome(BoxUpdateStatus.UPDATED)
        monkeypatch.setenv("BRUTEX_BOX_UPDATE", "1")
        monkeypatch.setenv(BOX_UPDATE_STATE_ENV, json.dumps(parent.to_dict()))
        with mock.patch.object(bu, "run_box_update") as leg:
            rc = cli.main(["nightly", "run", "--lake-root", str(lake)])
        assert rc == 0
        assert leg.call_count == 0  # the sentinel is the loop brake
        import os

        assert BOX_UPDATE_STATE_ENV not in os.environ  # consumed + deleted
        plan = captured["plan"]
        assert plan.box_update["reexec"] is True
        assert plan.box_update["running_sha"] == "b" * 40  # the child runs to_sha

    def test_updated_leg_reexecs_with_the_sentinel_stamped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # §6 steps 3-4 — flock released, then maybe_reexec → os.execv with the
        # SAME plain argv and the outcome stamped into the sentinel.
        lake = _mini_lake(tmp_path)
        self._run_nightly_spy(monkeypatch, lake / "sweeps")
        monkeypatch.setenv("BRUTEX_BOX_UPDATE", "1")
        seen: dict[str, Any] = {}

        def _fake_execv(prog: str, argv: list[str]) -> None:
            seen["prog"] = prog
            seen["argv"] = argv
            # execv never returns in production; the test lets the flow
            # continue so the run completes deterministically.

        monkeypatch.setattr(bu.os, "execv", _fake_execv)
        import os

        monkeypatch.setattr(bu.sys, "argv", ["brutex", "nightly", "run", "--lake-root", str(lake)])
        with mock.patch.object(
            bu, "run_box_update", return_value=_outcome(BoxUpdateStatus.UPDATED)
        ):
            rc = cli.main(["nightly", "run", "--lake-root", str(lake)])
        assert rc == 0
        assert seen["argv"][-4:] == ["nightly", "run", "--lake-root", str(lake)]
        assert BOX_UPDATE_STATE_ENV in os.environ
        os.environ.pop(BOX_UPDATE_STATE_ENV, None)

    def test_dry_run_never_runs_the_leg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The nightly --dry-run zero-mutation tree-oracle contract outranks:
        # neither adopt nor the leg runs; the block is honestly absent.
        lake = _mini_lake(tmp_path)
        monkeypatch.setenv("BRUTEX_BOX_UPDATE", "1")
        monkeypatch.setenv("BRUTEX_NIGHTLY_MIN_FREE_LAKE_BYTES", "0")
        monkeypatch.setenv("BRUTEX_NIGHTLY_MIN_FREE_BACKUP_BYTES", "0")
        with mock.patch.object(bu, "run_box_update") as leg:
            rc = cli.main(["nightly", "run", "--lake-root", str(lake), "--dry-run"])
        assert rc == 0
        assert leg.call_count == 0
        assert not (lake / ".box_update").exists()

    def test_report_carries_the_blocks_and_the_summary_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end through the REAL run_nightly assembly: the report block
        # + the 📦 honesty-note line for a non-quiet status (§5).
        from brutex.nightly.run import NightlyPlan, _assemble_report

        block = _outcome(BoxUpdateStatus.ROLLED_BACK).to_dict()
        plan = NightlyPlan(
            lake_root=tmp_path,
            results_root=tmp_path / "sweeps",
            backup_root=tmp_path / "b",
            keep=7,
            from_date=None,
            to_date=None,
            sweep_kwargs={},
            box_update=block,
            env_preflight={"flagged": [], "dotenv_errors": []},
        )
        report = _assemble_report(
            plan,
            nightly_id="n1",
            started=0.0,
            ended=1.0,
            links=(),
            tripwires=(),
            snapshot_id=None,
            absent_artifacts=(),
            extra_notes=(),
        )
        d = report.to_dict()
        assert d["box_update"] == block
        assert d["env_preflight"] == {"flagged": [], "dotenv_errors": []}
        notes = [n for n in report.honesty_notes if n.startswith("📦 box-update:")]
        assert notes == [
            "📦 box-update: rolled_back aaaaaaa→bbbbbbb · wheels ok 0/0 · gate skipped"
        ]

    def test_summary_note_silent_on_up_to_date_and_disabled(self, tmp_path: Path) -> None:
        from brutex.nightly.run import _box_update_note

        assert _box_update_note(None) is None
        assert _box_update_note(_outcome(BoxUpdateStatus.UP_TO_DATE).to_dict()) is None
        assert _box_update_note(_outcome(BoxUpdateStatus.DISABLED).to_dict()) is None
        note = _box_update_note(_outcome(BoxUpdateStatus.UPDATED).to_dict())
        assert note is not None and note.startswith("📦 box-update: updated")

    def test_plan_is_frozen_against_mutation_style_refactors(self, tmp_path: Path) -> None:
        # §10 test 15 — attribute assignment on a built plan raises.
        import dataclasses

        from brutex.nightly.run import NightlyPlan

        plan = NightlyPlan(
            lake_root=tmp_path,
            results_root=tmp_path / "sweeps",
            backup_root=tmp_path / "b",
            keep=7,
            from_date=None,
            to_date=None,
            sweep_kwargs={},
        )
        assert plan.box_update is None and plan.env_preflight is None
        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.box_update = {}  # type: ignore[misc]

    def test_health_hub_tolerates_the_new_blocks(self, tmp_path: Path) -> None:
        # §10 test 10 — a report CARRYING the blocks still folds identically.
        from brutex.dashboard.health_hub import nightly_lane

        results = tmp_path / "sweeps"
        nightly_dir = results / "nightly" / "nightly-20260717T013000Z-abc123"
        nightly_dir.mkdir(parents=True)
        report = {
            "schema": "brutex-nightly-report-v1",
            "schema_version": 1,
            "ended_utc": 1_800_000_000.0,
            "ok": True,
            "suspect": False,
            "blocker": None,
            "dry_run": False,
            "box_update": _outcome(BoxUpdateStatus.UPDATED).to_dict(),
            "env_preflight": {"flagged": [], "dotenv_errors": []},
        }
        (nightly_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
        lane = nightly_lane(results, now=1_800_000_100.0)
        assert lane.verdict == "ok"


class TestDefensiveLegWrap:
    """§25 deviation 12 — the never-block law reaches CODING BUGS in the leg."""

    def _spy(self, monkeypatch: pytest.MonkeyPatch, results: Path) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def fake_run_nightly(plan: Any, *, seams: Any = None) -> NightlyReport:
            captured["plan"] = plan
            return _green_report(results)

        monkeypatch.setattr("brutex.nightly.run.run_nightly", fake_run_nightly)
        return captured

    def test_bombing_leg_logs_error_and_the_nightly_proceeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A coding bug in run_box_update: one box_update.leg_error ERROR,
        # box_update block honestly None, run_nightly still invoked + the
        # report summary written — the chain is never blocked.
        from brutex.adapters.groww.pull_singleflight import nightly_singleflight_lock

        lake = _mini_lake(tmp_path)
        captured = self._spy(monkeypatch, lake / "sweeps")
        monkeypatch.setenv("BRUTEX_BOX_UPDATE", "1")
        with (
            mock.patch.object(bu, "run_box_update", side_effect=RuntimeError("leg bug")),
            capture_logs() as logs,
        ):
            rc = cli.main(["nightly", "run", "--lake-root", str(lake)])
        assert rc == 0
        assert captured["plan"].box_update is None  # honest absence, never fabricated
        events = [e for e in logs if e["event"] == "box_update.leg_error"]
        assert len(events) == 1
        assert events[0]["log_level"] == "error"
        assert events[0]["error"] == repr("leg bug")
        assert events[0]["error_type"] == "RuntimeError"
        assert capsys.readouterr().out.strip()  # the report summary was written
        # The flock released on the bomb path too (the inner finally).
        with nightly_singleflight_lock(lake, holder_note="post-bomb"):
            pass

    def test_execv_oserror_is_the_same_continuation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A RAISING os.execv (OSError) folds into the SAME continuation:
        # leg_error logged, block None, the nightly proceeds. (A SUCCESSFUL
        # exec never returns — the wrap cannot affect it, pinned by
        # TestFlockReleaseBeforeExec / test_updated_leg_reexecs... above.)
        import os

        lake = _mini_lake(tmp_path)
        captured = self._spy(monkeypatch, lake / "sweeps")
        monkeypatch.setenv("BRUTEX_BOX_UPDATE", "1")
        monkeypatch.setattr(bu.os, "execv", mock.Mock(side_effect=OSError("exec format error")))
        with (
            mock.patch.object(bu, "run_box_update", return_value=_outcome(BoxUpdateStatus.UPDATED)),
            capture_logs() as logs,
        ):
            rc = cli.main(["nightly", "run", "--lake-root", str(lake)])
        os.environ.pop(BOX_UPDATE_STATE_ENV, None)  # the pre-exec stamp survives the raise
        assert rc == 0
        assert captured["plan"].box_update is None
        events = [e for e in logs if e["event"] == "box_update.leg_error"]
        assert len(events) == 1 and events[0]["error_type"] == "OSError"

    def test_memory_error_is_explicitly_reraised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # MemoryError is Exception-derived — the wrap must NEVER swallow it
        # into a silent leg skip (the explicit re-raise arm).
        lake = _mini_lake(tmp_path)
        self._spy(monkeypatch, lake / "sweeps")
        monkeypatch.setenv("BRUTEX_BOX_UPDATE", "1")
        with (
            mock.patch.object(bu, "run_box_update", side_effect=MemoryError),
            pytest.raises(MemoryError),
        ):
            cli.main(["nightly", "run", "--lake-root", str(lake)])

    def test_keyboard_interrupt_propagates_raw(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # KeyboardInterrupt is structurally excluded by `except Exception`.
        lake = _mini_lake(tmp_path)
        self._spy(monkeypatch, lake / "sweeps")
        monkeypatch.setenv("BRUTEX_BOX_UPDATE", "1")
        with (
            mock.patch.object(bu, "run_box_update", side_effect=KeyboardInterrupt),
            pytest.raises(KeyboardInterrupt),
        ):
            cli.main(["nightly", "run", "--lake-root", str(lake)])


class TestFlockReleaseBeforeExec:
    """§10 test 11a — the flock release-before-exec law, probed cross-process."""

    def test_second_process_acquires_flock_during_pre_exec_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A GENUINELY DIFFERENT process takes LOCK_EX|LOCK_NB on the nightly
        # single-flight lock file INSIDE the monkeypatched os.execv — i.e.
        # DURING the pre-exec window — proving the leg's flock was FULLY
        # released (LOCK_UN + fd close EXECUTED) strictly before the exec
        # point, so the re-exec'd child can never self-deadlock on the lock
        # its parent still held (the §6 step-3 explicit-release invariant).
        import subprocess
        import sys as _sys

        from brutex.adapters.groww.pull_singleflight import nightly_singleflight_lock_path

        lake = _mini_lake(tmp_path)
        results = lake / "sweeps"
        captured: dict[str, Any] = {}

        def fake_run_nightly(plan: Any, *, seams: Any = None) -> Any:
            captured["plan"] = plan
            return _green_report(results)

        monkeypatch.setattr("brutex.nightly.run.run_nightly", fake_run_nightly)
        monkeypatch.setenv("BRUTEX_BOX_UPDATE", "1")
        lock_path = nightly_singleflight_lock_path(lake)
        probe_code = (
            "import fcntl, sys\n"
            "fh = open(sys.argv[1], 'a+')\n"
            "try:\n"
            "    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "except OSError:\n"
            "    sys.exit(3)\n"
            "fcntl.flock(fh.fileno(), fcntl.LOCK_UN)\n"
            "sys.exit(0)\n"
        )
        probe_rcs: list[int] = []

        def _fake_execv(prog: str, argv: list[str]) -> None:
            completed = subprocess.run(
                [_sys.executable, "-c", probe_code, str(lock_path)],
                timeout=30,
                check=False,
            )
            probe_rcs.append(completed.returncode)
            # execv never returns in production; the test lets the flow
            # continue so the run completes deterministically.

        monkeypatch.setattr(bu.os, "execv", _fake_execv)
        with mock.patch.object(
            bu, "run_box_update", return_value=_outcome(BoxUpdateStatus.UPDATED)
        ):
            rc = cli.main(["nightly", "run", "--lake-root", str(lake)])
        assert rc == 0
        # The second-process probe acquired cleanly IN the pre-exec window —
        # rc 3 here would mean the parent still held the flock at exec time.
        assert probe_rcs == [0]
        import os

        assert BOX_UPDATE_STATE_ENV in os.environ  # the exec sentinel was stamped
        os.environ.pop(BOX_UPDATE_STATE_ENV, None)

    def test_contention_at_leg_start_skips_into_standard_collision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # §10 test 11a competitor arm — a sibling holding the lock when the
        # nightly mouth starts: the leg is SKIPPED (never run, never a hang)
        # and the REAL run_nightly takes its standard COLLISION refusal
        # moments later; the refusal report carries the honest box_update
        # null (the leg never ran under contention).
        from brutex.adapters.groww.pull_singleflight import nightly_singleflight_lock

        lake = _mini_lake(tmp_path)
        monkeypatch.setenv("BRUTEX_BOX_UPDATE", "1")
        monkeypatch.setenv("BRUTEX_NIGHTLY_MIN_FREE_LAKE_BYTES", "0")
        monkeypatch.setenv("BRUTEX_NIGHTLY_MIN_FREE_BACKUP_BYTES", "0")
        with (
            nightly_singleflight_lock(lake, holder_note="nightly:peer"),
            mock.patch.object(bu, "run_box_update") as leg,
        ):
            rc = cli.main(["nightly", "run", "--lake-root", str(lake)])
        assert rc == 1  # run_nightly's own COLLISION refusal exit law
        assert leg.call_count == 0  # the leg was skipped, never nested/blocked
        reports = sorted((lake / "sweeps" / "nightly").glob("*/report.json"))
        assert len(reports) == 1
        payload = json.loads(reports[0].read_text(encoding="utf-8"))
        assert payload["blocker"] == "collision"
        assert payload["box_update"] is None  # honest absence — never a fake block
