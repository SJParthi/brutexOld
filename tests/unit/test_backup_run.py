"""Unit tests for :mod:`brutex.backup.run` (DEC-BACKUP-OFFBOX-001 §6).

The nightly orchestrator over an injected scripted runner: the LOAD-BEARING
ordering pins (preflight before ANY restic verb — the gate-before-forget pin;
success stamp BEFORE maintenance), every §1 exit-code triple reachable from
the flow (config F6, preflight F1/F2, busy F15, restic F3/F4/F5/F17a, state
F10, corruption F11, maintenance F23), the rc-3 partial streak (F8), the
rotating read-data-subset cursor law, the IST prune-weekday gate, and the
dry-run zero-restic/zero-state pin.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import fcntl
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import structlog.testing
from brutex.backup.errors import (
    EXIT_BUSY,
    EXIT_CONFIG,
    EXIT_CORRUPTION,
    EXIT_MAINTENANCE,
    EXIT_OK,
    EXIT_PREFLIGHT,
    EXIT_RESTIC,
)
from brutex.backup.plan import BackupPlan, PreflightVerdict
from brutex.backup.restic_runner import ResticInvocation, ResticRunner
from brutex.backup.run import PARTIAL_STREAK_CRITICAL, run_backup
from brutex.backup.state import (
    BackupState,
    empty_backup_state,
    load_state,
    save_state,
    state_path,
)
from brutex.config.backup_env import BackupEnvConfig, resolve_backup_env
from brutex.config.paths import LakeRootIdentity
from brutex.notifications.sweep_alerts import SweepAlerter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from brutex.notifications.telegram import TelegramEvent

#: 2026-07-06 12:00 UTC = 17:30 IST Monday (IST weekday 0).
NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)

SUMMARY_OUT = '{"message_type": "summary", "snapshot_id": "snap0001"}\n'


def _clock() -> datetime:
    return NOW


def _inv(
    op: str,
    *,
    rc: int = 0,
    stdout: str = "",
    timed_out: bool = False,
    binary_missing: bool = False,
) -> ResticInvocation:
    """Build one scripted invocation outcome."""
    return ResticInvocation(
        op=op,
        argv=(op,),
        ok=rc == 0 and not timed_out and not binary_missing,
        returncode=None if timed_out or binary_missing else rc,
        stdout_tail=stdout[-2000:],
        stderr_tail="",
        stdout_full=stdout,
        timed_out=timed_out,
        binary_missing=binary_missing,
    )


class ScriptedRunner:
    """Duck-typed :class:`ResticRunner` — scripted outcomes + an ordered call log."""

    def __init__(
        self,
        script: dict[str, ResticInvocation] | None = None,
        *,
        on_call: Callable[[str], None] | None = None,
    ) -> None:
        self.script = script or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._on_call = on_call

    def _get(self, key: str, **kwargs: Any) -> ResticInvocation:
        self.calls.append((key, kwargs))
        if self._on_call is not None:
            self._on_call(key)
        if key in self.script:
            return self.script[key]
        if key == "backup":
            return _inv("backup", stdout=SUMMARY_OUT)
        return _inv(key)

    def cat_config(self) -> ResticInvocation:
        """Scripted ``cat config``."""
        return self._get("cat-config")

    def init(self) -> ResticInvocation:
        """Scripted ``init``."""
        return self._get("init")

    def unlock(self) -> ResticInvocation:
        """Scripted ``unlock``."""
        return self._get("unlock")

    def backup(self, paths: Any, *, excludes: Any, tags: Any) -> ResticInvocation:
        """Scripted ``backup``."""
        return self._get("backup", paths=tuple(paths), excludes=tuple(excludes), tags=tuple(tags))

    def forget(
        self, *, keep_daily: int, keep_weekly: int, keep_monthly: int, prune: bool
    ) -> ResticInvocation:
        """Scripted ``forget``."""
        return self._get(
            "forget",
            keep_daily=keep_daily,
            keep_weekly=keep_weekly,
            keep_monthly=keep_monthly,
            prune=prune,
        )

    def check(self, *, read_data_subset: str | None = None) -> ResticInvocation:
        """Scripted ``check`` (structure) / ``check-data`` (read-data tier)."""
        key = "check" if read_data_subset is None else "check-data"
        return self._get(key, read_data_subset=read_data_subset)


class FakeNotifier:
    """Records every published TelegramEvent."""

    def __init__(self) -> None:
        self.published: list[TelegramEvent] = []

    def publish(self, event: TelegramEvent) -> None:
        """Record *event*."""
        self.published.append(event)


def _identity(lake: Path, *, device: int | None = 111) -> LakeRootIdentity:
    return LakeRootIdentity(
        path=lake, exists=True, volume_parent=None, volume_mounted=None, device=device
    )


def _ok_preflight(lake: Path, *, allow_empty: bool = False) -> PreflightVerdict:
    del allow_empty
    return PreflightVerdict(ok=True, reason="", identity=_identity(lake))


def _plan(cfg: BackupEnvConfig, lake: Path, **_kwargs: Any) -> BackupPlan:
    del cfg
    return BackupPlan(
        lake_root=lake,
        paths=(lake,),
        excludes=(".locks",),
        tags=("brutex", "pull-quiet"),
        pull_active=False,
    )


class Harness:
    """One run_backup invocation's wired seams + result probes."""

    def __init__(self, tmp_path: Path, *, env: dict[str, str] | None = None) -> None:
        self.lake = tmp_path / "lake"
        self.lake.mkdir(exist_ok=True)
        full_env = {
            "BRUTEX_BACKUP_REPO": "s3:host/bucket",
            "BRUTEX_BACKUP_PASSWORD_COMMAND": "echo pw",
            "BRUTEX_BACKUP_STATE_DIR": str(tmp_path / "bstate"),
        }
        if env:
            full_env.update(env)
        self.cfg = resolve_backup_env(full_env, home=tmp_path)
        self.runner = ScriptedRunner()
        self.notifier = FakeNotifier()
        self.alerter = SweepAlerter(notifier=self.notifier)

    def run(self, **kwargs: Any) -> int:
        """Invoke run_backup with the harness seams (overridable per test)."""
        params: dict[str, Any] = {
            "lake_root": self.lake,
            "alerter": self.alerter,
            "clock": _clock,
            "runner_factory": cast(
                "Callable[[BackupEnvConfig], ResticRunner]", lambda _cfg: self.runner
            ),
            "plan_fn": _plan,
            "preflight_fn": _ok_preflight,
        }
        params.update(kwargs)
        return run_backup(self.cfg, **params)

    def state(self) -> BackupState | None:
        """The persisted state after the run."""
        return load_state(state_path(self.cfg))

    def dedupe_keys(self) -> list[str]:
        """Every published alert's dedupe key, in order."""
        return [event.dedupe_key for event in self.notifier.published]

    def ops(self) -> list[str]:
        """The scripted runner's op sequence."""
        return [op for op, _ in self.runner.calls]


class TestHappyPath:
    def test_happy_path_stamps_state_and_exits_0(self, tmp_path: Path) -> None:
        """§6 full flow: every state field asserted; op order pinned."""
        harness = Harness(tmp_path)
        with structlog.testing.capture_logs() as logs:
            assert harness.run() == EXIT_OK
        state = harness.state()
        assert state is not None
        assert state.running_since is None  # cleared at exit (F17b)
        assert state.last_attempt_at == NOW
        assert state.last_success_at == NOW
        assert state.last_snapshot_id == "snap0001"
        assert state.last_failure_at is None
        assert state.last_failure_reason is None
        assert state.consecutive_failures == 0
        assert state.consecutive_partials == 0
        assert state.maintenance_ok is True
        assert state.verify_subset_cursor == 1  # auto subset passed → advanced
        assert state.lake_device == 111
        assert harness.ops() == ["cat-config", "unlock", "backup", "check", "check-data", "forget"]
        assert harness.dedupe_keys() == []  # no success Telegram (INFO noise rule)
        assert any(entry["event"] == "backup.run.ok" for entry in logs)

    def test_pull_active_logged(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)

        def _active_plan(cfg: BackupEnvConfig, lake: Path, **kwargs: Any) -> BackupPlan:
            return replace(_plan(cfg, lake), pull_active=True, tags=("brutex", "pull-active"))

        with structlog.testing.capture_logs() as logs:
            assert harness.run(plan_fn=_active_plan) == EXIT_OK
        assert any(entry["event"] == "backup.run.pull_active" for entry in logs)


class TestConfigGate:
    def test_config_missing_repo_exit_2_alerts(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path, env={"BRUTEX_BACKUP_REPO": ""})
        assert harness.run() == EXIT_CONFIG
        assert harness.dedupe_keys() == ["backup-failed:config"]
        assert harness.ops() == []  # no restic verb ever

    def test_ambiguous_password_exit_2(self, tmp_path: Path) -> None:
        """Both sources set → resolve normalises to None/None → F6 refusal."""
        harness = Harness(
            tmp_path,
            env={
                "BRUTEX_BACKUP_PASSWORD_FILE": str(tmp_path / "pw"),
                "BRUTEX_BACKUP_PASSWORD_COMMAND": "echo pw",
            },
        )
        assert harness.run() == EXIT_CONFIG
        assert harness.dedupe_keys() == ["backup-failed:config"]

    def test_no_password_source_exit_2(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path, env={"BRUTEX_BACKUP_PASSWORD_COMMAND": ""})
        assert harness.run() == EXIT_CONFIG

    def test_permissive_password_file_warns_and_proceeds(self, tmp_path: Path) -> None:
        """A group/other-readable password file WARNs but never blocks (F19)."""
        pw = tmp_path / "pw.txt"
        pw.write_text("secret", encoding="utf-8")
        pw.chmod(0o644)
        harness = Harness(
            tmp_path,
            env={
                "BRUTEX_BACKUP_PASSWORD_FILE": str(pw),
                "BRUTEX_BACKUP_PASSWORD_COMMAND": "",
            },
        )
        with structlog.testing.capture_logs() as logs:
            assert harness.run() == EXIT_OK
        assert any(entry["event"] == "backup.password.permissive_mode" for entry in logs)

    def test_strict_password_file_no_warn(self, tmp_path: Path) -> None:
        pw = tmp_path / "pw.txt"
        pw.write_text("secret", encoding="utf-8")
        pw.chmod(0o600)
        harness = Harness(
            tmp_path,
            env={"BRUTEX_BACKUP_PASSWORD_FILE": str(pw), "BRUTEX_BACKUP_PASSWORD_COMMAND": ""},
        )
        with structlog.testing.capture_logs() as logs:
            assert harness.run() == EXIT_OK
        assert not any(entry["event"] == "backup.password.permissive_mode" for entry in logs)

    def test_missing_password_file_stat_skips_warn(self, tmp_path: Path) -> None:
        """A missing password file surfaces via restic itself, not the mode warn."""
        harness = Harness(
            tmp_path,
            env={
                "BRUTEX_BACKUP_PASSWORD_FILE": str(tmp_path / "absent-pw"),
                "BRUTEX_BACKUP_PASSWORD_COMMAND": "",
            },
        )
        with structlog.testing.capture_logs() as logs:
            assert harness.run() == EXIT_OK
        assert not any(entry["event"] == "backup.password.permissive_mode" for entry in logs)


class TestPreflightGate:
    def test_preflight_refusal_precedes_all_restic_verbs(self, tmp_path: Path) -> None:
        """THE ordering pin (F1/graft B): unmounted lake → runner call log EMPTY —
        the mount gate runs before forget --prune could ever age real snapshots out."""
        harness = Harness(tmp_path)

        def _refuse(lake: Path, *, allow_empty: bool = False) -> PreflightVerdict:
            del allow_empty
            return PreflightVerdict(
                ok=False,
                reason="lake_unmounted",
                identity=LakeRootIdentity(
                    path=lake,
                    exists=True,
                    volume_parent=Path("/Volumes/T7"),
                    volume_mounted=False,
                    device=7,
                ),
            )

        factory_calls: list[int] = []

        def _factory(_cfg: BackupEnvConfig) -> ResticRunner:
            factory_calls.append(1)
            return cast("ResticRunner", harness.runner)

        with structlog.testing.capture_logs() as logs:
            code = harness.run(preflight_fn=_refuse, runner_factory=_factory)
        assert code == EXIT_PREFLIGHT
        assert harness.runner.calls == []  # NO restic verb — the pin's teeth
        assert factory_calls == []  # not even a runner was built
        assert harness.dedupe_keys() == ["backup-failed:lake_unmounted"]
        assert any(entry["event"] == "backup.preflight.lake_unmounted" for entry in logs)
        assert harness.state() is None  # nothing stamped

    def test_empty_set_exit_3(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)

        def _refuse(lake: Path, *, allow_empty: bool = False) -> PreflightVerdict:
            del allow_empty
            return PreflightVerdict(ok=False, reason="backup_set_empty", identity=_identity(lake))

        with structlog.testing.capture_logs() as logs:
            assert harness.run(preflight_fn=_refuse) == EXIT_PREFLIGHT
        assert harness.dedupe_keys() == ["backup-failed:backup_set_empty"]
        assert any(entry["event"] == "backup.preflight.empty_set" for entry in logs)

    def test_lake_missing_exit_3(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)

        def _refuse(lake: Path, *, allow_empty: bool = False) -> PreflightVerdict:
            del allow_empty
            return PreflightVerdict(
                ok=False,
                reason="lake_missing",
                identity=LakeRootIdentity(
                    path=lake, exists=False, volume_parent=None, volume_mounted=None, device=None
                ),
            )

        with structlog.testing.capture_logs() as logs:
            assert harness.run(preflight_fn=_refuse) == EXIT_PREFLIGHT
        assert any(entry["event"] == "backup.preflight.lake_missing" for entry in logs)

    def test_allow_empty_proceeds(self, tmp_path: Path) -> None:
        """--allow-empty threads through to the preflight seam and proceeds."""
        harness = Harness(tmp_path)
        seen: list[bool] = []

        def _spy(lake: Path, *, allow_empty: bool = False) -> PreflightVerdict:
            seen.append(allow_empty)
            return _ok_preflight(lake)

        assert harness.run(preflight_fn=_spy, allow_empty=True) == EXIT_OK
        assert seen == [True]

    def test_device_drift_warns(self, tmp_path: Path) -> None:
        """A recorded lake_device ≠ current device → WARN (F1 second gate)."""
        harness = Harness(tmp_path)
        save_state(state_path(harness.cfg), replace(empty_backup_state(), lake_device=999))
        with structlog.testing.capture_logs() as logs:
            assert harness.run() == EXIT_OK
        drift = [e for e in logs if e["event"] == "backup.preflight.device_drift"]
        assert len(drift) == 1
        assert drift[0]["recorded_device"] == 999
        assert drift[0]["current_device"] == 111

    def test_corrupt_prior_state_resets_loudly(self, tmp_path: Path) -> None:
        """The RUNNER may rewrite its own corrupt file — loudly (the checker never does)."""
        harness = Harness(tmp_path)
        state_file = state_path(harness.cfg)
        state_file.parent.mkdir(parents=True)
        state_file.write_text("{broken", encoding="utf-8")
        with structlog.testing.capture_logs() as logs:
            assert harness.run() == EXIT_OK
        assert any(entry["event"] == "backup.run.state_corrupt_reset" for entry in logs)
        state = harness.state()
        assert state is not None
        assert state.consecutive_failures == 0


class TestBusyAndLockErrors:
    def test_busy_exit_5_no_alert_no_state_touch(self, tmp_path: Path) -> None:
        """F15: a held single-flight lock → exit 5, NO alert (benign overlap)."""
        harness = Harness(tmp_path)
        harness.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(harness.cfg.state_dir / "backup.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with structlog.testing.capture_logs() as logs:
                assert harness.run() == EXIT_BUSY
        finally:
            os.close(fd)
        assert harness.dedupe_keys() == []
        assert harness.state() is None
        assert any(entry["event"] == "backup.singleflight.busy" for entry in logs)

    def test_lock_oserror_exit_4_alerts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ENOSPC creating the lock/state dir → F10 direction (exit 4, CRITICAL)."""
        import brutex.backup.run as run_mod

        def _boom(state_dir: Path) -> Iterator[None]:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(run_mod, "backup_singleflight_lock", _boom)
        harness = Harness(tmp_path)
        assert harness.run() == EXIT_RESTIC
        assert harness.dedupe_keys() == ["backup-failed:state_write"]


class TestRepoProbe:
    def test_init_needed_then_proceeds(self, tmp_path: Path) -> None:
        """cat-config rc≠0 → init once → proceed (F5 idempotent first run)."""
        harness = Harness(tmp_path)
        harness.runner.script["cat-config"] = _inv("cat-config", rc=1)
        with structlog.testing.capture_logs() as logs:
            assert harness.run() == EXIT_OK
        assert harness.ops()[:3] == ["cat-config", "init", "unlock"]
        assert any(entry["event"] == "backup.repo.initialized" for entry in logs)

    def test_init_failure_exit_4(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        harness.runner.script["cat-config"] = _inv("cat-config", rc=1)
        harness.runner.script["init"] = _inv("init", rc=1)
        assert harness.run() == EXIT_RESTIC
        assert harness.dedupe_keys() == ["backup-failed:auth_or_repo"]
        state = harness.state()
        assert state is not None
        assert state.last_failure_reason == "auth_or_repo"
        assert state.running_since is None  # cleared on the FAIL path too

    def test_probe_binary_missing_exit_4(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        harness.runner.script["cat-config"] = _inv("cat-config", binary_missing=True)
        assert harness.run() == EXIT_RESTIC
        assert harness.dedupe_keys() == ["backup-failed:restic_missing"]

    def test_probe_timeout_exit_4(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        harness.runner.script["cat-config"] = _inv("cat-config", timed_out=True)
        assert harness.run() == EXIT_RESTIC
        assert harness.dedupe_keys() == ["backup-failed:timeout"]

    def test_init_binary_missing_exit_4(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        harness.runner.script["cat-config"] = _inv("cat-config", rc=1)
        harness.runner.script["init"] = _inv("init", binary_missing=True)
        assert harness.run() == EXIT_RESTIC
        assert harness.dedupe_keys() == ["backup-failed:restic_missing"]

    def test_init_timeout_exit_4(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        harness.runner.script["cat-config"] = _inv("cat-config", rc=1)
        harness.runner.script["init"] = _inv("init", timed_out=True)
        assert harness.run() == EXIT_RESTIC
        assert harness.dedupe_keys() == ["backup-failed:timeout"]

    def test_unlock_failure_ignored_logged(self, tmp_path: Path) -> None:
        """Stale-unlock hygiene is best-effort — rc≠0 logs + continues."""
        harness = Harness(tmp_path)
        harness.runner.script["unlock"] = _inv("unlock", rc=1)
        with structlog.testing.capture_logs() as logs:
            assert harness.run() == EXIT_OK
        assert any(entry["event"] == "backup.unlock.failed" for entry in logs)


class TestBackupStep:
    def test_running_since_set_at_start_cleared_on_success_and_failure(
        self, tmp_path: Path
    ) -> None:
        """The F17b arm: running_since is live WHILE restic runs, cleared at exit."""
        harness = Harness(tmp_path)
        seen_running: list[datetime | None] = []

        def _observe(op: str) -> None:
            if op == "backup":
                state = load_state(state_path(harness.cfg))
                assert state is not None
                seen_running.append(state.running_since)

        harness.runner = ScriptedRunner(on_call=_observe)
        assert harness.run() == EXIT_OK
        assert seen_running == [NOW]  # set while running
        state = harness.state()
        assert state is not None
        assert state.running_since is None  # cleared on success

        # Failure path clears it too.
        harness2 = Harness(tmp_path, env={"BRUTEX_BACKUP_STATE_DIR": str(tmp_path / "b2")})
        harness2.runner.script["backup"] = _inv("backup", rc=1)
        assert harness2.run() == EXIT_RESTIC
        state2 = harness2.state()
        assert state2 is not None
        assert state2.running_since is None

    def test_backup_rc1_exit_4_failure_stamped_alert(self, tmp_path: Path) -> None:
        """F4: remote unreachable — failure stamped, consecutive_failures bumps."""
        harness = Harness(tmp_path)
        harness.runner.script["backup"] = _inv("backup", rc=1)
        assert harness.run() == EXIT_RESTIC
        assert harness.dedupe_keys() == ["backup-failed:remote_unreachable"]
        state = harness.state()
        assert state is not None
        assert state.last_failure_reason == "remote_unreachable"
        assert state.last_failure_at == NOW
        assert state.consecutive_failures == 1
        assert state.last_success_at is None

    def test_backup_timeout_exit_4(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        harness.runner.script["backup"] = _inv("backup", timed_out=True)
        assert harness.run() == EXIT_RESTIC
        assert harness.dedupe_keys() == ["backup-failed:timeout"]

    def test_binary_missing_exit_4_reason_restic_missing(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        harness.runner.script["backup"] = _inv("backup", binary_missing=True)
        assert harness.run() == EXIT_RESTIC
        assert harness.dedupe_keys() == ["backup-failed:restic_missing"]

    def test_summary_unparseable_is_failure(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        harness.runner.script["backup"] = _inv("backup", stdout="no summary here\n")
        assert harness.run() == EXIT_RESTIC
        assert harness.dedupe_keys() == ["backup-failed:summary_unparseable"]


class TestPartialStreak:
    def test_rc3_partial_stamped_success_warn(self, tmp_path: Path) -> None:
        """F8: rc 3 still stamps SUCCESS; streak 1 → WARN backup_degraded."""
        harness = Harness(tmp_path)
        harness.runner.script["backup"] = _inv("backup", rc=3, stdout=SUMMARY_OUT)
        with structlog.testing.capture_logs() as logs:
            assert harness.run() == EXIT_OK
        state = harness.state()
        assert state is not None
        assert state.last_success_at == NOW  # success IS stamped
        assert state.consecutive_partials == 1
        assert harness.dedupe_keys() == ["backup-degraded:partial"]
        assert any(entry["event"] == "backup.run.partial" for entry in logs)

    def test_partial_streak_third_escalates_critical(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        save_state(
            state_path(harness.cfg),
            replace(empty_backup_state(), consecutive_partials=PARTIAL_STREAK_CRITICAL - 1),
        )
        harness.runner.script["backup"] = _inv("backup", rc=3, stdout=SUMMARY_OUT)
        assert harness.run() == EXIT_OK
        assert harness.dedupe_keys() == ["backup-failed:partial_streak"]
        state = harness.state()
        assert state is not None
        assert state.consecutive_partials == PARTIAL_STREAK_CRITICAL

    def test_clean_run_resets_partial_streak(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        save_state(state_path(harness.cfg), replace(empty_backup_state(), consecutive_partials=2))
        assert harness.run() == EXIT_OK
        state = harness.state()
        assert state is not None
        assert state.consecutive_partials == 0
        assert harness.dedupe_keys() == []


class TestVerify:
    def test_check_failure_exit_6_no_success_stamp(self, tmp_path: Path) -> None:
        """F11: verify is INSIDE the success contract — no stamp, CRITICAL corrupt."""
        harness = Harness(tmp_path)
        harness.runner.script["check"] = _inv("check", rc=1)
        with structlog.testing.capture_logs() as logs:
            assert harness.run() == EXIT_CORRUPTION
        assert harness.dedupe_keys() == ["backup-repo-corrupt"]
        state = harness.state()
        assert state is not None
        assert state.last_success_at is None
        assert state.last_failure_reason == "check_failed"
        assert state.consecutive_failures == 1
        assert any(entry["event"] == "backup.check.failed" for entry in logs)

    def test_check_timeout_exit_4(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        harness.runner.script["check"] = _inv("check", timed_out=True)
        assert harness.run() == EXIT_RESTIC
        assert harness.dedupe_keys() == ["backup-failed:timeout"]

    def test_check_binary_missing_exit_4(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        harness.runner.script["check"] = _inv("check", binary_missing=True)
        assert harness.run() == EXIT_RESTIC
        assert harness.dedupe_keys() == ["backup-failed:restic_missing"]

    def test_subset_auto_cursor_advances_only_on_pass(self, tmp_path: Path) -> None:
        """auto: bucket = cursor%56+1; the cursor advances ONLY on a passing subset."""
        harness = Harness(tmp_path)
        save_state(state_path(harness.cfg), replace(empty_backup_state(), verify_subset_cursor=5))
        assert harness.run() == EXIT_OK
        data_calls = [kw for op, kw in harness.runner.calls if op == "check-data"]
        assert data_calls == [{"read_data_subset": "6/56"}]
        state = harness.state()
        assert state is not None
        assert state.verify_subset_cursor == 6

        # A FAILING subset check: no advance, no success stamp (F11).
        harness2 = Harness(tmp_path, env={"BRUTEX_BACKUP_STATE_DIR": str(tmp_path / "b2")})
        save_state(state_path(harness2.cfg), replace(empty_backup_state(), verify_subset_cursor=5))
        harness2.runner.script["check-data"] = _inv("check", rc=1)
        assert harness2.run() == EXIT_CORRUPTION
        state2 = harness2.state()
        assert state2 is not None
        assert state2.verify_subset_cursor == 5

    def test_subset_none_skips(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path, env={"BRUTEX_BACKUP_VERIFY_SUBSET": "none"})
        assert harness.run() == EXIT_OK
        assert "check-data" not in harness.ops()
        state = harness.state()
        assert state is not None
        assert state.verify_subset_cursor == 0  # never advances

    def test_subset_passthrough_verbatim(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path, env={"BRUTEX_BACKUP_VERIFY_SUBSET": "2%"})
        assert harness.run() == EXIT_OK
        data_calls = [kw for op, kw in harness.runner.calls if op == "check-data"]
        assert data_calls == [{"read_data_subset": "2%"}]
        state = harness.state()
        assert state is not None
        assert state.verify_subset_cursor == 0  # passthrough never advances the cursor

    def test_subset_timeout_exit_4(self, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        harness.runner.script["check-data"] = _inv("check", timed_out=True)
        assert harness.run() == EXIT_RESTIC
        assert harness.dedupe_keys() == ["backup-failed:timeout"]


class TestMaintenance:
    def test_maintenance_failure_exit_7_stamp_stands_warn(self, tmp_path: Path) -> None:
        """F23: forget failure NEVER retracts the data-success stamp."""
        harness = Harness(tmp_path)
        harness.runner.script["forget"] = _inv("forget", rc=1)
        with structlog.testing.capture_logs() as logs:
            assert harness.run() == EXIT_MAINTENANCE
        state = harness.state()
        assert state is not None
        assert state.last_success_at == NOW  # the stamp STANDS
        assert state.maintenance_ok is False
        assert harness.dedupe_keys() == ["backup-degraded:maintenance"]
        assert any(entry["event"] == "backup.maintenance.failed" for entry in logs)

    def test_prune_flag_only_on_prune_weekday(self, tmp_path: Path) -> None:
        """The weekly prune gate is judged in IST (both branches)."""
        # NOW is Monday 17:30 IST; default prune_weekday=6 (Sunday) → no prune.
        harness = Harness(tmp_path)
        assert harness.run() == EXIT_OK
        forget_calls = [kw for op, kw in harness.runner.calls if op == "forget"]
        assert forget_calls[0]["prune"] is False
        assert forget_calls[0]["keep_daily"] == 14
        assert forget_calls[0]["keep_weekly"] == 8
        assert forget_calls[0]["keep_monthly"] == 12

        # Monday-IST gate met while UTC still reads Sunday: 2026-07-05 19:30 UTC
        # = 2026-07-06 01:00 IST (Monday) — a UTC-judged gate would miss it.
        harness2 = Harness(
            tmp_path,
            env={
                "BRUTEX_BACKUP_STATE_DIR": str(tmp_path / "b2"),
                "BRUTEX_BACKUP_PRUNE_WEEKDAY": "0",
            },
        )
        sunday_utc_monday_ist = datetime(2026, 7, 5, 19, 30, tzinfo=UTC)
        assert harness2.run(clock=lambda: sunday_utc_monday_ist) == EXIT_OK
        forget_calls2 = [kw for op, kw in harness2.runner.calls if op == "forget"]
        assert forget_calls2[0]["prune"] is True


class TestStateWriteFailures:
    def test_state_write_oserror_exit_4_critical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F10 at mark-running: nothing snapshotted, fail loud (exit 4, CRITICAL)."""
        import brutex.backup.run as run_mod

        harness = Harness(tmp_path)

        def _boom(path: Path, state: BackupState) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(run_mod, "save_state", _boom)
        assert harness.run() == EXIT_RESTIC
        assert harness.dedupe_keys() == ["backup-failed:state_write"]
        assert harness.ops() == []  # failed BEFORE any restic verb

    def test_success_stamp_oserror_exit_4(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F10 at stamp-success: alarm-over-silence — unstamped success alarms later."""
        import brutex.backup.run as run_mod

        harness = Harness(tmp_path)
        calls: list[int] = []

        def _boom_second(path: Path, state: BackupState) -> None:
            calls.append(1)
            if len(calls) >= 2:
                raise OSError(28, "No space left on device")
            save_state(path, state)

        monkeypatch.setattr(run_mod, "save_state", _boom_second)
        assert harness.run() == EXIT_RESTIC
        assert harness.dedupe_keys() == ["backup-failed:state_write"]

    def test_fail_path_state_write_error_never_masks_exit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A best-effort failure stamp that ALSO fails keeps the original exit code."""
        import brutex.backup.run as run_mod

        harness = Harness(tmp_path)
        harness.runner.script["backup"] = _inv("backup", rc=1)
        calls: list[int] = []

        def _boom_second(path: Path, state: BackupState) -> None:
            calls.append(1)
            if len(calls) >= 2:
                raise OSError(28, "No space left on device")
            save_state(path, state)

        monkeypatch.setattr(run_mod, "save_state", _boom_second)
        with structlog.testing.capture_logs() as logs:
            assert harness.run() == EXIT_RESTIC  # the ORIGINAL failure's exit
        assert harness.dedupe_keys() == ["backup-failed:remote_unreachable"]
        assert any(entry["event"] == "backup.state.write_failed" for entry in logs)

    def test_maintenance_state_rewrite_error_best_effort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The maintenance_ok=False rewrite is best-effort — exit stays 7."""
        import brutex.backup.run as run_mod

        harness = Harness(tmp_path)
        harness.runner.script["forget"] = _inv("forget", rc=1)
        calls: list[int] = []

        def _boom_third(path: Path, state: BackupState) -> None:
            calls.append(1)
            if len(calls) >= 3:
                raise OSError(28, "No space left on device")
            save_state(path, state)

        monkeypatch.setattr(run_mod, "save_state", _boom_third)
        assert harness.run() == EXIT_MAINTENANCE


class TestDryRun:
    def test_dry_run_zero_restic_calls_zero_state_writes(self, tmp_path: Path) -> None:
        """--dry-run: plan + WOULD-run argv logged; no lock, no restic, no state."""
        harness = Harness(tmp_path)

        def _refusing_factory(_cfg: BackupEnvConfig) -> ResticRunner:
            raise AssertionError("dry-run must not build a runner")

        with structlog.testing.capture_logs() as logs:
            code = harness.run(dry_run=True, runner_factory=_refusing_factory)
        assert code == EXIT_OK
        assert harness.state() is None
        dry = [entry for entry in logs if entry["event"] == "backup.run.dry_run"]
        assert len(dry) == 1
        argv = dry[0]["would_run_argv"]
        assert argv[0:2] == ["restic", "backup"]
        assert "--exclude-caches" in argv

    def test_dry_run_still_refuses_bad_preflight(self, tmp_path: Path) -> None:
        """Preflight runs BEFORE the dry-run short-circuit (same gate, same exit)."""
        harness = Harness(tmp_path)

        def _refuse(lake: Path, *, allow_empty: bool = False) -> PreflightVerdict:
            del allow_empty
            return PreflightVerdict(ok=False, reason="backup_set_empty", identity=_identity(lake))

        assert harness.run(dry_run=True, preflight_fn=_refuse) == EXIT_PREFLIGHT


class TestDefaultClock:
    def test_default_utc_clock_sanity(self) -> None:
        """run.py's default clock seam executed for real — aware UTC, sane year."""
        from brutex.backup.run import _default_utc_clock

        stamp = _default_utc_clock()
        assert stamp.tzinfo is UTC
        assert 2026 <= stamp.year <= 2100


class TestLakeRootDefault:
    def test_lake_root_none_resolves_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """lake_root=None → resolve_lake_root() (BRUTEX_LAKE_ROOT honoured)."""
        lake = tmp_path / "env-lake"
        lake.mkdir()
        monkeypatch.setenv("BRUTEX_LAKE_ROOT", str(lake))
        harness = Harness(tmp_path)
        seen: list[Path] = []

        def _spy(target: Path, *, allow_empty: bool = False) -> PreflightVerdict:
            del allow_empty
            seen.append(target)
            return _ok_preflight(target)

        assert harness.run(lake_root=None, preflight_fn=_spy) == EXIT_OK
        assert seen == [lake]
