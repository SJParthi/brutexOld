"""``brutex backup {run,check,restore-drill,status}`` CLI tests (DEC-BACKUP-OFFBOX-001 §13).

Parser surface (every subcommand + flag), ``main`` dispatch threading with
exit-code passthrough (the deterministic §1 vocabulary passes through the thin
``cmd_backup`` adapter unchanged), the O(1) zero-side-effect ``status``
command (repo REDACTED to scheme+host, the password VALUE never read), and
the package import-hygiene pin (``brutex.backup`` never pulls polars/duckdb/
``dashboard``).
"""

# pyright: reportPrivateUsage=none

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from brutex import cli
from brutex.backup.errors import (
    EXIT_CONFIG,
    EXIT_DEADMAN_BREACH,
    EXIT_MAINTENANCE,
    EXIT_OK,
)
from brutex.backup.state import (
    DeadmanState,
    deadman_path,
    empty_backup_state,
    save_deadman,
    save_state,
    state_path,
)
from brutex.cli import _redact_backup_repo, main
from brutex.config.backup_env import resolve_backup_env


class TestParser:
    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (
                ["backup", "run"],
                {
                    "backup_command": "run",
                    "allow_empty": False,
                    "lake_root": None,
                    "dry_run": False,
                },
            ),
            (
                ["backup", "run", "--allow-empty", "--lake-root", "/tmp/lake", "--dry-run"],
                {
                    "backup_command": "run",
                    "allow_empty": True,
                    "lake_root": Path("/tmp/lake"),
                    "dry_run": True,
                },
            ),
            (["backup", "check"], {"backup_command": "check", "deep": False, "dry_run": False}),
            (
                ["backup", "check", "--deep", "--dry-run"],
                {"backup_command": "check", "deep": True, "dry_run": True},
            ),
            (
                ["backup", "restore-drill"],
                {
                    "backup_command": "restore-drill",
                    "sample": None,
                    "target": None,
                    "snapshot": None,
                    "keep": False,
                },
            ),
            (
                [
                    "backup",
                    "restore-drill",
                    "--sample",
                    "4",
                    "--target",
                    "/tmp/t",
                    "--snapshot",
                    "ab12",
                    "--keep",
                ],
                {
                    "backup_command": "restore-drill",
                    "sample": 4,
                    "target": Path("/tmp/t"),
                    "snapshot": "ab12",
                    "keep": True,
                },
            ),
            (["backup", "status"], {"backup_command": "status", "json_output": False}),
            (["backup", "status", "--json"], {"backup_command": "status", "json_output": True}),
        ],
    )
    def test_parser_accepts_each_subcommand_and_flags(
        self, argv: list[str], expected: dict[str, Any]
    ) -> None:
        args = cli.build_parser().parse_args(argv)
        assert args.command == "backup"
        for field, value in expected.items():
            assert getattr(args, field) == value

    def test_backup_without_subcommand_argparse_error(self) -> None:
        """The nested subparser is required — bare ``brutex backup`` refuses."""
        with pytest.raises(SystemExit) as excinfo:
            cli.build_parser().parse_args(["backup"])
        assert excinfo.value.code == 2


class TestDispatch:
    def test_dispatch_routes_run_with_exit_passthrough(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_run(cfg: Any, **kwargs: Any) -> int:
            captured.update(kwargs, cfg=cfg)
            return EXIT_MAINTENANCE

        monkeypatch.setattr("brutex.backup.run_backup", _fake_run)
        assert (
            main(["backup", "run", "--allow-empty", "--lake-root", "/tmp/lk", "--dry-run"])
            == EXIT_MAINTENANCE
        )
        assert captured["allow_empty"] is True
        assert captured["lake_root"] == Path("/tmp/lk")
        assert captured["dry_run"] is True

    def test_dispatch_routes_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def _fake_check(cfg: Any, **kwargs: Any) -> int:
            captured.update(kwargs)
            return EXIT_DEADMAN_BREACH

        monkeypatch.setattr("brutex.backup.run_deadman_check", _fake_check)
        assert main(["backup", "check", "--deep", "--dry-run"]) == EXIT_DEADMAN_BREACH
        assert captured == {"deep": True, "dry_run": True}

    def test_dispatch_routes_restore_drill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def _fake_drill(cfg: Any, **kwargs: Any) -> int:
            captured.update(kwargs)
            return EXIT_OK

        monkeypatch.setattr("brutex.backup.run_restore_drill", _fake_drill)
        assert (
            main(
                [
                    "backup",
                    "restore-drill",
                    "--sample",
                    "3",
                    "--target",
                    "/tmp/t",
                    "--snapshot",
                    "ab12",
                    "--keep",
                ]
            )
            == EXIT_OK
        )
        assert captured == {
            "sample": 3,
            "target_dir": Path("/tmp/t"),
            "snapshot_id": "ab12",
            "keep": True,
        }

    def test_dispatch_falls_through_past_backup_branch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-backup command traverses (does not take) the backup dispatch branch."""
        monkeypatch.setattr(cli, "cmd_sweep", lambda **kwargs: 42)
        code = main(["sweep", "NIFTY", "--start", "2025-01-01", "--end", "2025-01-02"])
        assert code == 42

    def test_config_error_exits_config_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken backup env exits with the deterministic EXIT_CONFIG (2) —
        the handlers return exit codes; CliUserError is never raised for F6."""
        monkeypatch.setenv("HOME", str(tmp_path))
        for var in (
            "BRUTEX_BACKUP_REPO",
            "BRUTEX_BACKUP_PASSWORD_FILE",
            "BRUTEX_BACKUP_PASSWORD_COMMAND",
        ):
            monkeypatch.delenv(var, raising=False)
        assert main(["backup", "run", "--lake-root", str(tmp_path)]) == EXIT_CONFIG


def _status_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the CLI at a tmp state dir + a redactable repo; return the cfg."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BRUTEX_BACKUP_STATE_DIR", str(tmp_path / "bstate"))
    monkeypatch.setenv("BRUTEX_BACKUP_REPO", "s3:s3.ap-south-1.amazonaws.com/bkt/brutex")
    monkeypatch.setenv("BRUTEX_BACKUP_PASSWORD_FILE", str(tmp_path / "pw-secret"))
    monkeypatch.delenv("BRUTEX_BACKUP_PASSWORD_COMMAND", raising=False)
    monkeypatch.delenv("BRUTEX_TELEGRAM_ENABLED", raising=False)
    return resolve_backup_env()


class TestStatus:
    def test_status_json_golden_over_fixture_state_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The --json payload over a fixed state fixture (time-free fields pinned)."""
        cfg = _status_env(tmp_path, monkeypatch)
        success = datetime.now(UTC) - timedelta(hours=1)
        save_state(
            state_path(cfg),
            replace(
                empty_backup_state(),
                last_attempt_at=success,
                last_success_at=success,
                last_snapshot_id="ab12cd34",
                verify_subset_cursor=14,
                lake_device=16777233,
            ),
        )
        save_deadman(deadman_path(cfg), DeadmanState())
        assert main(["backup", "status", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "fresh"
        assert payload["state_error"] is None
        assert payload["state"]["last_snapshot_id"] == "ab12cd34"
        assert payload["state"]["verify_subset_cursor"] == 14
        assert payload["deadman"] == {
            "last_deep_check_at": None,
            "last_overdue_alert_at": None,
            "schema": 1,
        }
        assert payload["config"] == {
            "repo": "s3:s3.ap-south-1.amazonaws.com",  # REDACTED to scheme+host
            "password_file": str(tmp_path / "pw-secret"),
            "password_command_set": False,
            "restic_bin": "restic",
            "state_dir": str(tmp_path / "bstate"),
            "deadman_hours": 30.0,
            "realert_hours": 6.0,
            "max_runtime_hours": 48.0,
            "verify_subset": "auto",
            "drill_sample": 16,
            "extra_excludes": ["logs", "dedup-cache"],
        }
        assert payload["telegram_alerts"] == "DISABLED"
        assert payload["healthcheck_ping"] == "UNSET"

    def test_status_redacts_repo_url_never_reads_password(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The bucket path never prints; a password file that would EXPLODE on
        read (a directory) proves the value is never touched."""
        cfg = _status_env(tmp_path, monkeypatch)
        (tmp_path / "pw-secret").mkdir()  # any read_text() would raise
        save_state(
            state_path(cfg),
            replace(empty_backup_state(), last_success_at=datetime.now(UTC)),
        )
        assert main(["backup", "status", "--json"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "/bkt/brutex" not in out  # the bucket path is redacted away
        assert str(tmp_path / "pw-secret") in out  # the PATH is shown

    def test_status_exit_mirrors_verdict_without_side_effects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An overdue state → exit 1 with ZERO alert/ping/sidecar side effects."""
        cfg = _status_env(tmp_path, monkeypatch)
        monkeypatch.setenv("BRUTEX_BACKUP_HEALTHCHECK_URL", "http://127.0.0.1:9/never")
        save_state(
            state_path(cfg),
            replace(
                empty_backup_state(),
                last_success_at=datetime.now(UTC) - timedelta(hours=100),
            ),
        )
        assert main(["backup", "status"]) == EXIT_DEADMAN_BREACH
        assert not deadman_path(cfg).exists()  # sidecar never written
        out = capsys.readouterr().out
        assert "backup verdict: overdue" in out

    def test_status_human_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Human output: verdict + redacted repo + Telegram/healthcheck lines."""
        cfg = _status_env(tmp_path, monkeypatch)
        save_state(
            state_path(cfg),
            replace(empty_backup_state(), last_success_at=datetime.now(UTC)),
        )
        assert main(["backup", "status"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "backup verdict: fresh" in out
        assert "repo: s3:s3.ap-south-1.amazonaws.com" in out
        assert "Telegram alerts: DISABLED" in out
        assert "healthcheck ping: UNSET" in out

    def test_status_missing_state_reads_never_succeeded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _status_env(tmp_path, monkeypatch)
        assert main(["backup", "status"]) == EXIT_DEADMAN_BREACH
        assert "never_succeeded" in capsys.readouterr().out

    def test_status_corrupt_state_shown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A corrupt state.json reads state_corrupt (and the error is surfaced)."""
        cfg = _status_env(tmp_path, monkeypatch)
        state_file = state_path(cfg)
        state_file.parent.mkdir(parents=True)
        state_file.write_text("{broken", encoding="utf-8")
        assert main(["backup", "status", "--json"]) == EXIT_DEADMAN_BREACH
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "state_corrupt"
        assert payload["state"] is None
        assert payload["state_error"] is not None

    def test_status_telegram_enabled_reads_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _status_env(tmp_path, monkeypatch)
        monkeypatch.setenv("BRUTEX_TELEGRAM_ENABLED", "true")
        monkeypatch.setenv("BRUTEX_BACKUP_HEALTHCHECK_URL", "https://hc.io/x")
        save_state(
            state_path(cfg),
            replace(empty_backup_state(), last_success_at=datetime.now(UTC)),
        )
        assert main(["backup", "status", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["telegram_alerts"] == "ENABLED"
        assert payload["healthcheck_ping"] == "SET"


class TestRedaction:
    def test_redact_backup_repo_branches(self) -> None:
        """unset / scheme-less local / scheme:host — all three branches."""
        assert _redact_backup_repo(None) == "unset"
        assert _redact_backup_repo("/mnt/backup/repo") == "local"
        assert _redact_backup_repo("s3:s3.ap-south-1.amazonaws.com/bkt/x") == (
            "s3:s3.ap-south-1.amazonaws.com"
        )
        assert _redact_backup_repo("b2:bucketname/prefix") == "b2:bucketname"


class TestImportHygiene:
    def test_backup_package_imports_no_polars_dashboard(self) -> None:
        """§3 pin: importing brutex.backup pulls neither polars/duckdb nor
        brutex.dashboard (the checker must start fast + wheel-free)."""
        probe = (
            "import sys; import brutex.backup; mods = set(sys.modules); "
            "banned = [m for m in mods if m == 'polars' or m == 'duckdb' "
            "or m.startswith('brutex.dashboard')]; "
            "print('BANNED:' + ','.join(sorted(banned)))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        assert completed.stdout.strip() == "BANNED:"
