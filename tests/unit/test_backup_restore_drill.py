"""Unit tests for :mod:`brutex.backup.restore_drill` (DEC-BACKUP-OFFBOX-001 §8).

Every drill flow runs the REAL executable fake-restic stub through the REAL
:class:`ResticRunner` subprocess seam (``BRUTEX_BACKUP_RESTIC_BIN``): the
1 MiB known-bytes canary roundtrip INCLUDING the corruption negative (a real
byte flip performed by the child process — the byte-verifier's teeth are
proven, not assumed), the drill-from-state-snapshot pin (never ``latest``),
the deterministic stratified sample, the §8 step-6 mtime law, the audit
byte-PREFIX law, and the kept-on-failure/removed-on-success tree contract.
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import structlog.testing
from brutex.backup.errors import EXIT_CONFIG, EXIT_CORRUPTION, EXIT_OK, EXIT_RESTIC
from brutex.backup.restore_drill import (
    CANARY_SIZE_BYTES,
    _categorize,
    _stratified_sample,
    canary_bytes,
    run_restore_drill,
)
from brutex.backup.state import empty_backup_state, save_state, state_path
from brutex.config.backup_env import BackupEnvConfig, resolve_backup_env
from brutex.notifications.sweep_alerts import SweepAlerter

from tests.fakes.fake_restic import FakeRestic, write_fake_restic

if TYPE_CHECKING:
    from brutex.notifications.telegram import TelegramEvent


class FakeNotifier:
    """Records every published TelegramEvent."""

    def __init__(self) -> None:
        self.published: list[TelegramEvent] = []

    def publish(self, event: TelegramEvent) -> None:
        """Record *event*."""
        self.published.append(event)


class DrillWorld:
    """One drill test's real filesystem world: lake + home audit + stub + state."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))  # brutex_home() → tmp/.brutex
        self.tmp = tmp_path
        self.lake = tmp_path / "lake"
        self.home_audit = tmp_path / ".brutex" / "audit"
        self.files: dict[str, Path] = {}
        for rel, content in (
            ("bars/2025/09.parquet", b"bars-data-AAA"),
            ("options/NSE/opt.parquet", b"options-data-BBB"),
            ("window_ledger/ledger.parquet", b"ledger-data-CCC"),
            ("catalog.duckdb", b"duckdb-master-DDD"),
            ("audit/chain.jsonl", b'{"seq": 1}\n{"seq": 2}\n'),
        ):
            target = self.lake / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            self.files[str(target)] = target
        home_chain = self.home_audit / "home_chain.jsonl"
        home_chain.parent.mkdir(parents=True, exist_ok=True)
        home_chain.write_bytes(b'{"h": 1}\n{"h": 2}\n')
        self.files[str(home_chain)] = home_chain
        self.fake: FakeRestic = write_fake_restic(tmp_path / "bin")
        self.cfg: BackupEnvConfig = resolve_backup_env(
            {
                "BRUTEX_BACKUP_REPO": "s3:host/bucket",
                "BRUTEX_BACKUP_PASSWORD_FILE": str(tmp_path / "pw"),
                "BRUTEX_BACKUP_STATE_DIR": str(tmp_path / "bstate"),
                "BRUTEX_BACKUP_RESTIC_BIN": str(self.fake.binary),
            },
            home=tmp_path,
        )
        (tmp_path / "pw").write_text("test-password", encoding="utf-8")
        save_state(state_path(self.cfg), replace(empty_backup_state(), last_snapshot_id="snap42"))
        self.notifier = FakeNotifier()
        self.alerter = SweepAlerter(notifier=self.notifier)

    def ls_stdout(self, *, time: str | None = "2026-07-06T12:00:00Z") -> str:
        """Build the ls --json output: optional snapshot header + all live files."""
        lines: list[str] = []
        if time is not None:
            lines.append(json.dumps({"time": time, "paths": [str(self.lake)]}))
        lines.extend(json.dumps({"type": "file", "path": path}) for path in sorted(self.files))
        return "\n".join(lines) + "\n"

    def set_script(self, script: dict[str, Any]) -> None:
        """Write the stub's behaviour script."""
        self.fake.set_script(script)

    def drill(self, **kwargs: Any) -> int:
        """Run the drill against the stub (lake_root pinned, alerter wired)."""
        params: dict[str, Any] = {"lake_root": self.lake, "alerter": self.alerter}
        params.update(kwargs)
        return run_restore_drill(self.cfg, **params)

    def drill_tmp_children(self) -> list[Path]:
        """Restored temp trees still on disk."""
        root = self.cfg.state_dir / "drill-tmp"
        return sorted(root.iterdir()) if root.exists() else []

    def dedupe_keys(self) -> list[str]:
        """Every published alert's dedupe key."""
        return [event.dedupe_key for event in self.notifier.published]


@pytest.fixture(name="world")
def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DrillWorld:
    return DrillWorld(tmp_path, monkeypatch)


class TestCanary:
    def test_canary_roundtrip_verified(self, world: DrillWorld) -> None:
        """Happy path: canary verified, sample verified, tree removed, exit 0."""
        world.set_script({"ls": {"stdout": world.ls_stdout()}})
        with structlog.testing.capture_logs() as logs:
            assert world.drill() == EXIT_OK
        assert world.dedupe_keys() == []
        ok_events = [entry for entry in logs if entry["event"] == "backup.drill.ok"]
        assert len(ok_events) == 1
        assert ok_events[0]["canary"] == "verified"
        assert "does NOT prove all data" in ok_events[0]["honesty"]
        assert world.drill_tmp_children() == []  # removed on success
        # The canary snapshot was forgotten best-effort with ITS id.
        assert ["forget", "fakesnap01"] in world.fake.calls()

    def test_canary_corruption_negative_exit_6_alert(self, world: DrillWorld) -> None:
        """F20 negative: the stub FLIPS a canary byte → exit 6 + CRITICAL + tree kept."""
        canary_path = str(world.cfg.state_dir / "canary" / "canary.bin")
        world.set_script(
            {
                "ls": {"stdout": world.ls_stdout()},
                "restore": [{"corrupt": [canary_path]}],
            }
        )
        assert world.drill() == EXIT_CORRUPTION
        assert world.dedupe_keys() == ["backup-repo-corrupt"]
        assert len(world.drill_tmp_children()) == 1  # KEPT for forensics

    def test_canary_not_restored_exit_6(self, world: DrillWorld) -> None:
        """A rc-0 restore that produced NO canary file is corruption, not success."""
        world.set_script({"ls": {"stdout": world.ls_stdout()}, "restore": [{"skip_copy": True}]})
        with structlog.testing.capture_logs() as logs:
            assert world.drill() == EXIT_CORRUPTION
        mismatches = [entry for entry in logs if entry["event"] == "backup.drill.mismatch"]
        assert mismatches[0]["detail"] == "canary not restored"

    def test_canary_forget_best_effort_failure_warns_not_fails(self, world: DrillWorld) -> None:
        """Object-Lock refusing the canary forget is EXPECTED — WARN, never a fail."""
        world.set_script({"ls": {"stdout": world.ls_stdout()}, "forget-snapshot": {"exit": 1}})
        with structlog.testing.capture_logs() as logs:
            assert world.drill() == EXIT_OK
        assert any(entry["event"] == "backup.drill.canary_forget_failed" for entry in logs)

    def test_canary_backup_invocation_fails_exit_4(self, world: DrillWorld) -> None:
        world.set_script({"backup": {"exit": 1}})
        assert world.drill() == EXIT_RESTIC
        assert world.dedupe_keys() == ["backup-failed:drill_restic"]

    def test_canary_summary_unparseable_exit_4(self, world: DrillWorld) -> None:
        world.set_script({"backup": {"stdout": "not json at all\n"}})
        with structlog.testing.capture_logs() as logs:
            assert world.drill() == EXIT_RESTIC
        assert any(entry["event"] == "backup.drill.canary_unparseable" for entry in logs)

    def test_canary_restore_invocation_fails_exit_4(self, world: DrillWorld) -> None:
        world.set_script({"restore": [{"exit": 1}]})
        assert world.drill() == EXIT_RESTIC
        assert world.dedupe_keys() == ["backup-failed:drill_restic"]

    def test_canary_bytes_deterministic_golden(self) -> None:
        """1 MiB, byte-stable forever — the whole-path known-bytes oracle."""
        payload = canary_bytes()
        assert len(payload) == CANARY_SIZE_BYTES
        assert payload == canary_bytes()
        assert hashlib.sha256(payload).hexdigest() == hashlib.sha256(canary_bytes()).hexdigest()
        # The first block is sha256(seed + counter 0) — pins the expansion law.
        assert (
            payload[:32]
            == hashlib.sha256(b"brutex-backup-canary-v1" + (0).to_bytes(8, "big")).digest()
        )


class TestSnapshotSelection:
    def test_drills_state_snapshot_not_latest(self, world: DrillWorld) -> None:
        """Graft A pin: the sample restores state.last_snapshot_id, NEVER 'latest'."""
        world.set_script({"ls": {"stdout": world.ls_stdout()}})
        assert world.drill() == EXIT_OK
        calls = world.fake.calls()
        ls_calls = [argv for argv in calls if argv[0] == "ls"]
        assert ls_calls == [["ls", "--json", "snap42"]]
        restores = [argv for argv in calls if argv[0] == "restore"]
        assert restores[1][1] == "snap42"  # the SAMPLE restore (canary is [0])
        assert all("latest" not in argv for argv in calls)

    def test_snapshot_flag_overrides(self, world: DrillWorld) -> None:
        world.set_script({"ls": {"stdout": world.ls_stdout()}})
        assert world.drill(snapshot_id="override7") == EXIT_OK
        ls_calls = [argv for argv in world.fake.calls() if argv[0] == "ls"]
        assert ls_calls == [["ls", "--json", "override7"]]

    def test_no_recorded_snapshot_exit_2(self, world: DrillWorld) -> None:
        save_state(state_path(world.cfg), empty_backup_state())
        with structlog.testing.capture_logs() as logs:
            assert world.drill() == EXIT_CONFIG
        assert any(entry["event"] == "backup.drill.no_snapshot" for entry in logs)
        assert world.fake.calls() == []  # zero restic verbs

    def test_corrupt_state_reads_as_no_snapshot(self, world: DrillWorld) -> None:
        state_path(world.cfg).write_text("{broken", encoding="utf-8")
        assert world.drill() == EXIT_CONFIG

    def test_config_missing_exit_2(self, world: DrillWorld, tmp_path: Path) -> None:
        """No repo / no password source → EXIT_CONFIG + CRITICAL config alert."""
        broken = resolve_backup_env(
            {"BRUTEX_BACKUP_STATE_DIR": str(tmp_path / "bstate")}, home=tmp_path
        )
        assert run_restore_drill(broken, alerter=world.alerter) == EXIT_CONFIG
        assert world.dedupe_keys() == ["backup-failed:config"]


class TestStratifiedSample:
    def test_sample_deterministic_per_snapshot(self) -> None:
        """Same snapshot id → same set; a different id reshuffles (the hash IS the rng)."""
        paths = tuple(f"/lake/bars/file{i}.parquet" for i in range(40))
        first = _stratified_sample(
            paths,
            snapshot_id="snapA",
            sample=8,
            lake_root=Path("/lake"),
            home_audit=Path("/home/x/.brutex/audit"),
        )
        second = _stratified_sample(
            paths,
            snapshot_id="snapA",
            sample=8,
            lake_root=Path("/lake"),
            home_audit=Path("/home/x/.brutex/audit"),
        )
        other = _stratified_sample(
            paths,
            snapshot_id="snapB",
            sample=8,
            lake_root=Path("/lake"),
            home_audit=Path("/home/x/.brutex/audit"),
        )
        assert first == second
        assert len(first) == 8
        assert first != other  # 40!/(32!) shuffles — a collision would be astronomical

    def test_stratification_every_category_sampled(self) -> None:
        """Round-robin: every category with members appears in every drill."""
        paths = (
            *(f"/lake/bars/b{i}.parquet" for i in range(30)),
            "/lake/options/o1.parquet",
            "/lake/audit/a1.jsonl",
            "/lake/catalog.duckdb",
            "/home/x/.brutex/audit/h1.jsonl",
        )
        sampled = _stratified_sample(
            paths,
            snapshot_id="snapZ",
            sample=6,
            lake_root=Path("/lake"),
            home_audit=Path("/home/x/.brutex/audit"),
        )
        categories = {
            _categorize(p, lake_root=Path("/lake"), home_audit=Path("/home/x/.brutex/audit"))
            for p in sampled
        }
        assert {"bars", "options", "audit", "duckdb"} <= categories

    def test_sample_exhausts_small_population(self) -> None:
        """sample > population returns every path once, then stops (no spin)."""
        paths = ("/lake/bars/only.parquet",)
        sampled = _stratified_sample(
            paths,
            snapshot_id="s",
            sample=16,
            lake_root=Path("/lake"),
            home_audit=Path("/h/.brutex/audit"),
        )
        assert sampled == paths

    def test_categorize_buckets(self) -> None:
        lake = Path("/lake")
        audit = Path("/home/x/.brutex/audit")
        assert _categorize("/lake/bars/a.parquet", lake_root=lake, home_audit=audit) == "bars"
        assert _categorize("/lake/catalog.duckdb", lake_root=lake, home_audit=audit) == "duckdb"
        assert _categorize("/lake/loose.txt", lake_root=lake, home_audit=audit) == "lake-root"
        assert _categorize("/home/x/.brutex/audit/c.jsonl", lake_root=lake, home_audit=audit) == (
            "audit"
        )
        assert _categorize("/elsewhere/f.bin", lake_root=lake, home_audit=audit) == "other"


class TestByteVerificationLaw:
    def test_match_verified(self, world: DrillWorld) -> None:
        """Unchanged live bytes hash-match the restored copy → verified."""
        world.set_script({"ls": {"stdout": world.ls_stdout()}})
        with structlog.testing.capture_logs() as logs:
            assert world.drill() == EXIT_OK
        ok_events = [entry for entry in logs if entry["event"] == "backup.drill.ok"]
        verified: dict[str, int] = ok_events[0]["verified"]
        assert sum(verified.values()) == len(world.files)
        assert ok_events[0]["corrupt"] == {}

    def test_live_newer_skipped_changed(self, world: DrillWorld) -> None:
        """Bytes differ but the live file moved on since the snapshot → honest skip."""
        bars = str(world.lake / "bars" / "2025" / "09.parquet")
        world.set_script(
            {
                # Snapshot time far in the PAST → live mtime > snapshot time.
                "ls": {"stdout": world.ls_stdout(time="1990-01-01T00:00:00Z")},
                "restore": [{}, {"corrupt": [bars]}],
            }
        )
        with structlog.testing.capture_logs() as logs:
            assert world.drill() == EXIT_OK
        ok_events = [entry for entry in logs if entry["event"] == "backup.drill.ok"]
        assert ok_events[0]["skipped_changed"] == {"bars": 1}

    def test_live_missing_skipped(self, world: DrillWorld) -> None:
        """A snapshot-only file (live deleted) restores fine and skips honestly."""
        ghost = str(world.lake / "bars" / "2025" / "deleted-since.parquet")
        ls = world.ls_stdout() + json.dumps({"type": "file", "path": ghost}) + "\n"
        world.set_script(
            {
                "ls": {"stdout": ls},
                "restore": [{}, {"extra_writes": {ghost: "old bytes"}}],
            }
        )
        with structlog.testing.capture_logs() as logs:
            assert world.drill() == EXIT_OK
        ok_events = [entry for entry in logs if entry["event"] == "backup.drill.ok"]
        assert ok_events[0]["skipped_changed"] == {"bars": 1}

    def test_live_unchanged_bytes_differ_corrupt_exit_6(self, world: DrillWorld) -> None:
        """F20 mtime-law negative: live mtime ≤ snapshot time AND bytes differ."""
        bars = str(world.lake / "bars" / "2025" / "09.parquet")
        world.set_script(
            {
                # Snapshot time in the FUTURE → live cannot be "newer" — corruption.
                "ls": {"stdout": world.ls_stdout(time="2100-01-01T00:00:00Z")},
                "restore": [{}, {"corrupt": [bars]}],
            }
        )
        with structlog.testing.capture_logs() as logs:
            assert world.drill() == EXIT_CORRUPTION
        assert world.dedupe_keys() == ["backup-repo-corrupt"]
        mismatches = [entry for entry in logs if entry["event"] == "backup.drill.mismatch"]
        assert mismatches[0]["detail"] == "bytes differ, live not newer"
        assert len(world.drill_tmp_children()) == 1  # kept for forensics

    def test_no_snapshot_time_falls_back_to_restored_mtime(self, world: DrillWorld) -> None:
        """No ls header → the restored copy's own mtime anchors the law."""
        bars_path = world.lake / "bars" / "2025" / "09.parquet"
        world.set_script(
            {
                "ls": {"stdout": world.ls_stdout(time=None)},
                "restore": [{}, {"corrupt": [str(bars_path)]}],
            }
        )
        # Live mtime pushed FAR past any restore-time mtime → skipped_changed.
        far_future = 4102444800  # 2100-01-01
        os.utime(bars_path, (far_future, far_future))
        with structlog.testing.capture_logs() as logs:
            assert world.drill() == EXIT_OK
        ok_events = [entry for entry in logs if entry["event"] == "backup.drill.ok"]
        assert ok_events[0]["skipped_changed"] == {"bars": 1}

    def test_audit_prefix_ok(self, world: DrillWorld) -> None:
        """A restored audit file that is a byte-PREFIX of the live one → verified."""
        chain = str(world.lake / "audit" / "chain.jsonl")
        world.set_script(
            {
                "ls": {"stdout": world.ls_stdout()},
                "restore": [{}, {"truncate": [chain]}],
            }
        )
        with structlog.testing.capture_logs() as logs:
            assert world.drill() == EXIT_OK
        ok_events = [entry for entry in logs if entry["event"] == "backup.drill.ok"]
        assert ok_events[0]["verified"]["audit"] == 2  # lake chain + home chain
        assert ok_events[0]["corrupt"] == {}

    def test_audit_non_prefix_corrupt_even_with_newer_mtime(self, world: DrillWorld) -> None:
        """The prefix law's teeth: a non-prefix audit restore is corruption
        REGARDLESS of mtime (hash-chained JSONL is append-only — Golden Rule #8)."""
        chain = str(world.lake / "audit" / "chain.jsonl")
        world.set_script(
            {
                # Snapshot time far in the past — mtime would excuse a NON-audit file.
                "ls": {"stdout": world.ls_stdout(time="1990-01-01T00:00:00Z")},
                "restore": [{}, {"corrupt": [chain]}],
            }
        )
        with structlog.testing.capture_logs() as logs:
            assert world.drill() == EXIT_CORRUPTION
        assert world.dedupe_keys() == ["backup-repo-corrupt"]
        mismatches = [entry for entry in logs if entry["event"] == "backup.drill.mismatch"]
        assert mismatches[0]["detail"] == "audit non-prefix"

    def test_restore_missing_sampled_file_exit_4(self, world: DrillWorld) -> None:
        """A sampled file ABSENT after a rc-0 restore is a restore error (exit 4)."""
        world.set_script(
            {"ls": {"stdout": world.ls_stdout()}, "restore": [{}, {"skip_copy": True}]}
        )
        with structlog.testing.capture_logs() as logs:
            assert world.drill() == EXIT_RESTIC
        assert world.dedupe_keys() == ["backup-failed:drill_restic"]
        assert any(entry["event"] == "backup.drill.restore_missing" for entry in logs)


class TestRestoreFailures:
    def test_ls_failure_exit_4(self, world: DrillWorld) -> None:
        world.set_script({"ls": {"exit": 1}})
        assert world.drill() == EXIT_RESTIC
        assert world.dedupe_keys() == ["backup-failed:drill_restic"]

    def test_sample_restore_failure_exit_4(self, world: DrillWorld) -> None:
        world.set_script({"ls": {"stdout": world.ls_stdout()}, "restore": [{}, {"exit": 1}]})
        assert world.drill() == EXIT_RESTIC
        assert world.dedupe_keys() == ["backup-failed:drill_restic"]

    def test_empty_snapshot_sample_skips_restore(self, world: DrillWorld) -> None:
        """Zero file nodes → no sample restore, canary alone decides (exit 0)."""
        world.set_script({"ls": {"stdout": '{"time": "2026-07-06T12:00:00Z"}\n'}})
        assert world.drill() == EXIT_OK
        restores = [argv for argv in world.fake.calls() if argv[0] == "restore"]
        assert len(restores) == 1  # the canary only


class TestTargetTree:
    def test_keep_flag_keeps(self, world: DrillWorld) -> None:
        world.set_script({"ls": {"stdout": world.ls_stdout()}})
        assert world.drill(keep=True) == EXIT_OK
        assert len(world.drill_tmp_children()) == 1

    def test_explicit_target_kept_on_success(self, world: DrillWorld, tmp_path: Path) -> None:
        """An operator-chosen --target is never auto-removed."""
        target = tmp_path / "chosen-target"
        target.mkdir()
        world.set_script({"ls": {"stdout": world.ls_stdout()}})
        assert world.drill(target_dir=target) == EXIT_OK
        assert target.exists()

    def test_sample_override_bounds_sample(self, world: DrillWorld) -> None:
        """--sample N bounds the restored include set."""
        world.set_script({"ls": {"stdout": world.ls_stdout()}})
        assert world.drill(sample=2) == EXIT_OK
        restores = [argv for argv in world.fake.calls() if argv[0] == "restore"]
        sample_includes = [a for a in restores[1] if a == "--include"]
        assert len(sample_includes) == 2


class TestDrillIOResilience:
    """Round-1 review pins: the drill "returns an exit code, raises nothing" —
    an OSError in canary/tmp setup maps to EXIT_RESTIC + drill_io, and a live
    file vanishing MID-verify (lake-heal DELETE-and-rebuild racing the drill)
    reads skipped_changed, never a traceback."""

    def test_tmp_setup_oserror_exit_4_drill_io_alert(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """state_dir occupied by a FILE → mkdir OSError → deterministic triple."""
        monkeypatch.setenv("HOME", str(tmp_path))
        blocker = tmp_path / "state-as-file"
        blocker.write_text("not-a-dir", encoding="utf-8")
        password = tmp_path / "pw"
        password.write_text("p", encoding="utf-8")
        cfg = resolve_backup_env(
            {
                "BRUTEX_BACKUP_REPO": "s3:host/bucket",
                "BRUTEX_BACKUP_PASSWORD_FILE": str(password),
                "BRUTEX_BACKUP_STATE_DIR": str(blocker),
            },
            home=tmp_path,
        )
        notifier = FakeNotifier()
        with structlog.testing.capture_logs() as logs:
            code = run_restore_drill(
                cfg,
                snapshot_id="cafe12",
                lake_root=tmp_path / "lake",
                alerter=SweepAlerter(notifier=notifier),
            )
        assert code == EXIT_RESTIC
        assert [event.dedupe_key for event in notifier.published] == ["backup-failed:drill_io"]
        assert any(entry["event"] == "backup.drill.io_failed" for entry in logs)

    def test_live_file_vanished_mid_verify_skipped_changed(
        self, world: DrillWorld, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FileNotFoundError from the live-side read AFTER exists() → skipped_changed."""
        from brutex.backup import restore_drill as drill_mod

        world.set_script({"ls": {"stdout": world.ls_stdout()}})
        real_sha = drill_mod._file_sha256  # pyright: ignore[reportPrivateUsage]
        racy_live = str(world.lake / "bars" / "2025" / "09.parquet")

        def _racy(path: Path) -> str:
            if str(path) == racy_live:
                raise FileNotFoundError(racy_live)
            return real_sha(path)

        monkeypatch.setattr(drill_mod, "_file_sha256", _racy)
        with structlog.testing.capture_logs() as logs:
            code = world.drill()
        assert code == EXIT_OK
        vanished = [entry for entry in logs if entry["event"] == "backup.drill.live_vanished"]
        assert len(vanished) == 1
        assert vanished[0]["path"] == racy_live
        ok_events = [entry for entry in logs if entry["event"] == "backup.drill.ok"]
        assert len(ok_events) == 1
        assert ok_events[0]["skipped_changed"].get("bars") == 1
        assert not ok_events[0]["corrupt"]
