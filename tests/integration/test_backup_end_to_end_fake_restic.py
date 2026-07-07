"""End-to-end backup flows against the REAL executable fake-restic stub.

DEC-BACKUP-OFFBOX-001 §14 integration tier: real files, a real flock, real
state JSON on disk, the stub binary wired via ``BRUTEX_BACKUP_RESTIC_BIN`` and
executed through the production ``default_runner_factory`` subprocess seam —
run → check FRESH, the exclude/include set as restic actually receives it
(the ``.quarantine`` inclusion regression pin), failure → OVERDUE after a
clock advance, real-flock concurrency, the deep state-vs-remote cross-check
(agreement + divergence), and the runbook step-9 negative dead-man drill in
CI form.
"""

from __future__ import annotations

import fcntl
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from brutex.backup.deadman import run_deadman_check
from brutex.backup.errors import (
    EXIT_BUSY,
    EXIT_DEADMAN_BREACH,
    EXIT_OK,
    EXIT_RESTIC,
)
from brutex.backup.run import run_backup
from brutex.backup.state import deadman_path, load_deadman, load_state, state_path
from brutex.config.backup_env import BackupEnvConfig, resolve_backup_env
from brutex.notifications.sweep_alerts import SweepAlerter

from tests.fakes.fake_restic import FakeRestic, write_fake_restic

if TYPE_CHECKING:
    from brutex.notifications.telegram import TelegramEvent

NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)


class FakeNotifier:
    """Records every published TelegramEvent."""

    def __init__(self) -> None:
        self.published: list[TelegramEvent] = []

    def publish(self, event: TelegramEvent) -> None:
        """Record *event*."""
        self.published.append(event)


class World:
    """One end-to-end world: tiny lake + stub restic + env-resolved config."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        self.lake = tmp_path / "lake"
        (self.lake / "bars" / "NSE").mkdir(parents=True)
        (self.lake / "bars" / "NSE" / "09.parquet").write_bytes(b"real-bars")
        (self.lake / ".quarantine" / "2026").mkdir(parents=True)
        (self.lake / ".quarantine" / "2026" / "poison.parquet").write_bytes(b"heal-copy")
        self.fake: FakeRestic = write_fake_restic(tmp_path / "bin")
        pw = tmp_path / "pw"
        pw.write_text("password", encoding="utf-8")
        pw.chmod(0o600)
        monkeypatch.setenv("BRUTEX_BACKUP_REPO", "s3:host/bucket")
        monkeypatch.setenv("BRUTEX_BACKUP_PASSWORD_FILE", str(pw))
        monkeypatch.delenv("BRUTEX_BACKUP_PASSWORD_COMMAND", raising=False)
        monkeypatch.setenv("BRUTEX_BACKUP_STATE_DIR", str(tmp_path / "bstate"))
        monkeypatch.setenv("BRUTEX_BACKUP_RESTIC_BIN", str(self.fake.binary))
        self.cfg: BackupEnvConfig = resolve_backup_env()
        self.notifier = FakeNotifier()
        self.alerter = SweepAlerter(notifier=self.notifier)

    def run(self, *, clock: Any = None, **kwargs: Any) -> int:
        """Production run_backup: default runner factory → the stub binary."""
        params: dict[str, Any] = {"lake_root": self.lake, "alerter": self.alerter}
        if clock is not None:
            params["clock"] = clock
        params.update(kwargs)
        return run_backup(self.cfg, **params)

    def check(self, *, clock: Any, **kwargs: Any) -> int:
        params: dict[str, Any] = {"alerter": self.alerter, "clock": clock}
        params.update(kwargs)
        return run_deadman_check(self.cfg, **params)

    def snapshots_json(self, stamp: datetime) -> str:
        iso = stamp.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return f'[{{"short_id": "ab12", "time": "{iso}"}}]'

    def backup_argv(self) -> list[str]:
        """The recorded restic backup invocation's argv."""
        return next(argv for argv in self.fake.calls() if argv and argv[0] == "backup")

    def excludes(self) -> list[str]:
        argv = self.backup_argv()
        return [argv[i + 1] for i, a in enumerate(argv) if a == "--exclude"]

    def dedupe_keys(self) -> list[str]:
        return [event.dedupe_key for event in self.notifier.published]


@pytest.fixture(name="world")
def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_path, monkeypatch)


class TestEndToEnd:
    def test_full_run_then_check_fresh(self, world: World) -> None:
        """Tiny lake → run (stub) → state stamped → hourly check reads FRESH."""
        world.fake.set_script({"snapshots": {"stdout": world.snapshots_json(NOW)}})
        assert world.run(clock=lambda: NOW) == EXIT_OK
        state = load_state(state_path(world.cfg))
        assert state is not None
        assert state.last_success_at == NOW
        assert state.last_snapshot_id == "fakesnap01"
        assert state.running_since is None
        # The stub really executed the full §6 verb sequence.
        assert world.fake.verbs() == ["cat", "unlock", "backup", "check", "check", "forget"]
        # One hour later the dead-man reads FRESH (deep agrees against the stub).
        later = NOW + timedelta(hours=1)
        assert world.check(clock=lambda: later) == EXIT_OK
        assert world.dedupe_keys() == []
        assert load_deadman(deadman_path(world.cfg)).last_deep_check_at == later

    def test_snapshot_contents_exclude_locks_rate_tmp_logs_dedupcache(self, world: World) -> None:
        """The exclude set restic actually receives (fixed + documented default)."""
        assert world.run(clock=lambda: NOW) == EXIT_OK
        assert world.excludes() == [
            ".locks",
            ".rate",
            "*.tmp",
            ".brutex-write-*",
            "logs",
            "dedup-cache",
        ]
        argv = world.backup_argv()
        assert "--exclude-caches" in argv

    def test_quarantine_dir_is_included(self, world: World) -> None:
        """The B-premise regression pin (§15 R7): .quarantine heal copies — the
        ONLY record of removed poison rows — ride the whole-root include."""
        assert world.run(clock=lambda: NOW) == EXIT_OK
        argv = world.backup_argv()
        assert str(world.lake) in argv  # whole-root include ⇒ .quarantine ships
        assert all(".quarantine" not in exclude for exclude in world.excludes())

    def test_induced_rc1_then_check_overdue_after_clock_advance(self, world: World) -> None:
        """Success → vendor failure → the dead-man escalates once stale (F4→F12 lane)."""
        world.fake.set_script({"snapshots": {"stdout": world.snapshots_json(NOW)}})
        assert world.run(clock=lambda: NOW) == EXIT_OK
        world.fake.set_script(
            {
                "backup": {"exit": 1, "stderr": "connect: network unreachable"},
                "snapshots": {"stdout": world.snapshots_json(NOW)},
            }
        )
        assert world.run(clock=lambda: NOW + timedelta(hours=24)) == EXIT_RESTIC
        assert world.dedupe_keys() == ["backup-failed:remote_unreachable"]
        state = load_state(state_path(world.cfg))
        assert state is not None
        assert state.last_success_at == NOW  # the OLD stamp stands
        assert state.consecutive_failures == 1
        # 40 h after the last success the checker pages OVERDUE.
        later = NOW + timedelta(hours=40)
        assert world.check(clock=lambda: later) == EXIT_DEADMAN_BREACH
        assert world.dedupe_keys()[-1] == "backup-overdue:overdue"

    def test_two_concurrent_runs_second_exits_busy(self, world: World) -> None:
        """A REAL held flock on backup.lock → the overlapping run exits 5, silent."""
        world.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(world.cfg.state_dir / "backup.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert world.run(clock=lambda: NOW) == EXIT_BUSY
        finally:
            os.close(fd)
        assert world.dedupe_keys() == []  # benign overlap never pages (F15)
        assert world.run(clock=lambda: NOW) == EXIT_OK  # lock released → next run fine

    def test_deep_check_against_stub_snapshots_agreement(self, world: World) -> None:
        world.fake.set_script({"snapshots": {"stdout": world.snapshots_json(NOW)}})
        assert world.run(clock=lambda: NOW) == EXIT_OK
        later = NOW + timedelta(hours=2)
        assert world.check(clock=lambda: later, deep=True) == EXIT_OK
        assert world.dedupe_keys() == []
        assert load_deadman(deadman_path(world.cfg)).last_deep_check_at == later
        snapshots_calls = [argv for argv in world.fake.calls() if argv[0] == "snapshots"]
        assert snapshots_calls[-1] == ["snapshots", "--json", "--latest", "1", "--tag", "brutex"]

    def test_deep_check_against_stub_snapshots_divergence(self, world: World) -> None:
        """F21: a plausible LOCAL stamp diverging from remote truth ALARMS."""
        world.fake.set_script({"snapshots": {"stdout": world.snapshots_json(NOW)}})
        assert world.run(clock=lambda: NOW) == EXIT_OK
        # Remote latest suddenly reads 90 days OLD — a poisoned local stamp.
        world.fake.set_script(
            {"snapshots": {"stdout": world.snapshots_json(NOW - timedelta(days=90))}}
        )
        later = NOW + timedelta(hours=2)
        assert world.check(clock=lambda: later, deep=True) == EXIT_DEADMAN_BREACH
        assert world.dedupe_keys() == ["backup-overdue:state_remote_divergence"]

    def test_negative_deadman_drill_threshold_zero_pages(self, world: World) -> None:
        """Runbook step 9 in CI form: a near-zero threshold MUST page (an untested
        recovery path is unverified)."""
        assert world.run(clock=lambda: NOW) == EXIT_OK
        tiny = resolve_backup_env(
            {
                **{k: v for k, v in os.environ.items() if k.startswith("BRUTEX_BACKUP_")},
                "BRUTEX_BACKUP_DEADMAN_HOURS": "0.0001",
            }
        )
        code = run_deadman_check(
            tiny, alerter=world.alerter, clock=lambda: NOW + timedelta(minutes=5)
        )
        assert code == EXIT_DEADMAN_BREACH
        assert world.dedupe_keys() == ["backup-overdue:overdue"]
