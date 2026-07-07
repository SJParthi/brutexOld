"""Unit tests for :mod:`brutex.backup.restic_runner` (DEC-BACKUP-OFFBOX-001 §5.2).

The ONE subprocess seam: exact-argv goldens per verb (the designed mutant
killers), the rc/timeout/missing-binary mapping through the REAL
``_default_run`` (executable fake-restic stub — no monkeypatched subprocess),
child-env hygiene by construction (F19 — inherited ``RESTIC_PASSWORD``
stripped, AWS/B2 passthrough, planted-password absence from every argv and
every captured log event), and the pure ``--json`` parse helpers.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import structlog.testing
from brutex.backup.errors import BackupError
from brutex.backup.restic_runner import (
    BACKUP_EXIT_PARTIAL,
    BACKUP_HOST,
    BACKUP_TAG,
    CANARY_TAG,
    ResticRunner,
    SnapshotInfo,
    build_backup_argv,
    default_runner_factory,
    parse_backup_summary_snapshot_id,
    parse_ls_file_paths,
    parse_ls_snapshot_time,
    parse_snapshots_latest,
)
from brutex.config.backup_env import resolve_backup_env

from tests.fakes.fake_restic import write_fake_restic


class RecordingRunner:
    """Captures every (argv, env, timeout) and returns a scripted CompletedProcess."""

    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, str], float]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, argv: Any, *, env: Any, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(argv), dict(env), timeout))
        return subprocess.CompletedProcess(
            list(argv), self.returncode, stdout=self.stdout, stderr=self.stderr
        )


def _runner(
    *,
    run: Any = None,
    password_file: Path | None = Path("/tmp/pw"),
    password_command: str | None = None,
    base_env: dict[str, str] | None = None,
    binary: str = "restic",
    timeout_s: float = 30.0,
) -> ResticRunner:
    """Build a ResticRunner with test defaults."""
    return ResticRunner(
        binary=binary,
        repo="s3:host/bucket",
        password_file=password_file,
        password_command=password_command,
        timeout_s=timeout_s,
        run=run,
        base_env=base_env if base_env is not None else {"HOME": "/h", "PATH": "/usr/bin"},
    )


class TestArgvGoldens:
    @pytest.mark.parametrize(
        ("call", "expected_op", "expected_argv"),
        [
            (
                lambda r: r.cat_config(),
                "cat-config",
                ("restic", "cat", "config"),
            ),
            (lambda r: r.init(), "init", ("restic", "init")),
            (lambda r: r.unlock(), "unlock", ("restic", "unlock")),
            (
                lambda r: r.backup(
                    [Path("/lake"), Path("/audit")],
                    excludes=(".locks", "*.tmp"),
                    tags=("brutex", "pull-quiet"),
                ),
                "backup",
                (
                    "restic",
                    "backup",
                    "/lake",
                    "/audit",
                    "--exclude",
                    ".locks",
                    "--exclude",
                    "*.tmp",
                    "--exclude-caches",
                    "--tag",
                    "brutex",
                    "--tag",
                    "pull-quiet",
                    "--host",
                    "brutex",
                    "--json",
                ),
            ),
            (
                lambda r: r.forget(keep_daily=14, keep_weekly=8, keep_monthly=12, prune=False),
                "forget",
                (
                    "restic",
                    "forget",
                    "--keep-daily",
                    "14",
                    "--keep-weekly",
                    "8",
                    "--keep-monthly",
                    "12",
                    "--tag",
                    "brutex",
                    "--group-by",
                    "host,tags",
                ),
            ),
            (
                lambda r: r.forget(keep_daily=1, keep_weekly=2, keep_monthly=3, prune=True),
                "forget",
                (
                    "restic",
                    "forget",
                    "--keep-daily",
                    "1",
                    "--keep-weekly",
                    "2",
                    "--keep-monthly",
                    "3",
                    "--tag",
                    "brutex",
                    "--group-by",
                    "host,tags",
                    "--prune",
                ),
            ),
            (
                lambda r: r.forget_snapshot("ab12"),
                "forget-snapshot",
                ("restic", "forget", "ab12"),
            ),
            (lambda r: r.check(), "check", ("restic", "check")),
            (
                lambda r: r.check(read_data_subset="3/56"),
                "check",
                ("restic", "check", "--read-data-subset=3/56"),
            ),
            (
                lambda r: r.snapshots_latest_json(),
                "snapshots",
                ("restic", "snapshots", "--json", "--latest", "1", "--tag", "brutex"),
            ),
            (
                lambda r: r.snapshots_latest_json(tag="brutex-drill-canary"),
                "snapshots",
                ("restic", "snapshots", "--json", "--latest", "1", "--tag", "brutex-drill-canary"),
            ),
            (lambda r: r.ls_json("cafe12"), "ls", ("restic", "ls", "--json", "cafe12")),
            (
                lambda r: r.restore("cafe12", target=Path("/tmp/t"), includes=("/a", "/b")),
                "restore",
                (
                    "restic",
                    "restore",
                    "cafe12",
                    "--target",
                    "/tmp/t",
                    "--include",
                    "/a",
                    "--include",
                    "/b",
                ),
            ),
        ],
    )
    def test_argv_golden_per_op(
        self, call: Any, expected_op: str, expected_argv: tuple[str, ...]
    ) -> None:
        """Exact argv tuple per verb — the designed mutant killers."""
        recorder = RecordingRunner()
        inv = call(_runner(run=recorder))
        assert inv.op == expected_op
        assert inv.argv == expected_argv
        assert recorder.calls[0][0] == expected_argv

    def test_backup_argv_contains_exclude_caches_and_tags_host_json(self) -> None:
        """--exclude-caches always on (graft B) + tags + fixed host + --json."""
        argv = build_backup_argv("restic", [Path("/lake")], excludes=(), tags=(BACKUP_TAG,))
        assert "--exclude-caches" in argv
        assert argv[-3:] == ("--host", BACKUP_HOST, "--json")
        assert argv[argv.index("--tag") : argv.index("--tag") + 2] == ("--tag", "brutex")

    def test_constants(self) -> None:
        assert BACKUP_TAG == "brutex"
        assert CANARY_TAG == "brutex-drill-canary"
        assert BACKUP_HOST == "brutex"
        assert BACKUP_EXIT_PARTIAL == 3


class TestInvocationMapping:
    def test_rc_mapping_ok_nonzero(self) -> None:
        ok = _runner(run=RecordingRunner(returncode=0, stdout="x", stderr="y")).check()
        assert ok.ok is True
        assert ok.returncode == 0
        assert ok.stdout_tail == "x"
        assert ok.stderr_tail == "y"
        bad = _runner(run=RecordingRunner(returncode=1)).check()
        assert bad.ok is False
        assert bad.returncode == 1
        assert bad.timed_out is False
        assert bad.binary_missing is False

    def test_stream_tails_bounded_full_kept(self) -> None:
        """stdout_tail is bounded to 2000 chars; stdout_full keeps everything."""
        big = "a" * 5000
        inv = _runner(run=RecordingRunner(stdout=big, stderr="e" * 900)).check()
        assert len(inv.stdout_tail) == 2000
        assert inv.stdout_full == big
        assert len(inv.stderr_tail) == 500

    def test_timeout_maps_timed_out(self, tmp_path: Path) -> None:
        """A sleeping REAL stub + tiny timeout through the REAL _default_run (F17a)."""
        fake = write_fake_restic(tmp_path, {"check": {"sleep": 10}})
        inv = _runner(binary=str(fake.binary), timeout_s=0.5, base_env=None).check()
        assert inv.timed_out is True
        assert inv.ok is False
        assert inv.returncode is None
        assert inv.binary_missing is False

    def test_timeout_exc_streams_captured(self) -> None:
        """A TimeoutExpired carrying bytes output exercises the bytes-decode branch."""

        def _raise(argv: Any, *, env: Any, timeout: float) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(
                cmd=list(argv), timeout=timeout, output=b"partial-out", stderr=b"partial-err"
            )

        inv = _runner(run=_raise).check()
        assert inv.timed_out is True
        assert inv.stdout_tail == "partial-out"
        assert inv.stderr_tail == "partial-err"
        assert inv.stdout_full == "partial-out"

    def test_missing_binary_maps_binary_missing(self, tmp_path: Path) -> None:
        """A nonexistent binary through the REAL _default_run → binary_missing (F3)."""
        inv = _runner(binary=str(tmp_path / "no-such-restic"), base_env=None).cat_config()
        assert inv.binary_missing is True
        assert inv.ok is False
        assert inv.returncode is None
        assert inv.timed_out is False

    def test_default_run_executes_real_binary(self, tmp_path: Path) -> None:
        """The default seam runs a REAL executable stub — no pragma needed."""
        fake = write_fake_restic(tmp_path, {"unlock": {"exit": 0, "stdout": "unlocked"}})
        inv = _runner(binary=str(fake.binary), base_env=None).unlock()
        assert inv.ok is True
        assert inv.stdout_full == "unlocked"
        assert fake.verbs() == ["unlock"]

    def test_stdin_devnull(self, tmp_path: Path) -> None:
        """The stub exits non-zero unless stdin is at EOF — proves DEVNULL wiring."""
        fake = write_fake_restic(tmp_path, {"init": {"require_empty_stdin": True}})
        inv = _runner(binary=str(fake.binary), base_env=None).init()
        assert inv.ok is True  # DEVNULL → stdin.read() == "" → rc 0


class TestChildEnv:
    def test_child_env_password_file_vs_command_exclusive(self) -> None:
        recorder_file = RecordingRunner()
        _runner(run=recorder_file, password_file=Path("/tmp/pw")).init()
        env_file = recorder_file.calls[0][1]
        assert env_file["RESTIC_PASSWORD_FILE"] == "/tmp/pw"
        assert "RESTIC_PASSWORD_COMMAND" not in env_file
        assert env_file["RESTIC_REPOSITORY"] == "s3:host/bucket"

        recorder_cmd = RecordingRunner()
        _runner(run=recorder_cmd, password_file=None, password_command="security find -w").init()
        env_cmd = recorder_cmd.calls[0][1]
        assert env_cmd["RESTIC_PASSWORD_COMMAND"] == "security find -w"
        assert "RESTIC_PASSWORD_FILE" not in env_cmd

    def test_child_env_strips_inherited_restic_password(self) -> None:
        """An inherited RESTIC_PASSWORD is NEVER copied to the child (F19)."""
        recorder = RecordingRunner()
        _runner(
            run=recorder,
            base_env={"HOME": "/h", "RESTIC_PASSWORD": "hunter2", "RESTIC_REPOSITORY": "evil"},
        ).init()
        env = recorder.calls[0][1]
        assert "RESTIC_PASSWORD" not in env
        assert env["RESTIC_REPOSITORY"] == "s3:host/bucket"  # ours, not the inherited one

    def test_child_env_aws_b2_passthrough(self) -> None:
        """HOME/PATH/TMPDIR exact + AWS_*/B2_* prefixes pass through; others dropped."""
        recorder = RecordingRunner()
        _runner(
            run=recorder,
            base_env={
                "HOME": "/h",
                "PATH": "/p",
                "TMPDIR": "/t",
                "AWS_ACCESS_KEY_ID": "AKIA",
                "AWS_SECRET_ACCESS_KEY": "sk",
                "B2_ACCOUNT_ID": "b2id",
                "TELEGRAM_TOKEN": "leak-me-not",
            },
        ).init()
        env = recorder.calls[0][1]
        assert env["HOME"] == "/h"
        assert env["PATH"] == "/p"
        assert env["TMPDIR"] == "/t"
        assert env["AWS_ACCESS_KEY_ID"] == "AKIA"
        assert env["AWS_SECRET_ACCESS_KEY"] == "sk"
        assert env["B2_ACCOUNT_ID"] == "b2id"
        assert "TELEGRAM_TOKEN" not in env

    def test_base_env_none_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """base_env=None → os.environ (and the strip still applies)."""
        monkeypatch.setenv("RESTIC_PASSWORD", "leaked")
        monkeypatch.setenv("AWS_REGION", "ap-south-1")
        recorder = RecordingRunner()
        ResticRunner(
            binary="restic",
            repo="s3:host/bucket",
            password_file=Path("/tmp/pw"),
            password_command=None,
            timeout_s=30.0,
            run=recorder,
        ).init()
        env = recorder.calls[0][1]
        assert "RESTIC_PASSWORD" not in env
        assert env["AWS_REGION"] == "ap-south-1"

    def test_construction_refuses_zero_or_two_password_sources(self) -> None:
        """The belt: neither / both password sources refuse at construction (F6)."""
        with pytest.raises(BackupError, match="exactly one"):
            _runner(password_file=None, password_command=None)
        with pytest.raises(BackupError, match="exactly one"):
            _runner(password_file=Path("/tmp/pw"), password_command="cmd")

    def test_secret_hygiene_planted_password_absent_from_argv_and_logs(
        self, tmp_path: Path
    ) -> None:
        """F19: a planted password VALUE appears in NO argv and NO log event."""
        secret = "sup3r-s3cret-restic-pw"
        pw_file = tmp_path / "pw.txt"
        pw_file.write_text(secret, encoding="utf-8")
        recorder = RecordingRunner(stdout="{}")
        runner = _runner(run=recorder, password_file=pw_file)
        with structlog.testing.capture_logs() as logs:
            runner.cat_config()
            runner.backup([tmp_path], excludes=("x",), tags=("brutex",))
            runner.forget(keep_daily=1, keep_weekly=1, keep_monthly=1, prune=True)
            runner.restore("id", target=tmp_path, includes=("/a",))
        for argv, env, _timeout in recorder.calls:
            assert all(secret not in part for part in argv)
            assert env.get("RESTIC_PASSWORD") is None
        for entry in logs:
            assert secret not in repr(entry)


class TestFactory:
    def test_default_runner_factory_requires_repo(self, tmp_path: Path) -> None:
        cfg = resolve_backup_env({}, home=tmp_path)
        with pytest.raises(BackupError, match="BRUTEX_BACKUP_REPO"):
            default_runner_factory(cfg)

    def test_default_runner_factory_builds(self, tmp_path: Path) -> None:
        cfg = resolve_backup_env(
            {
                "BRUTEX_BACKUP_REPO": "s3:host/bucket",
                "BRUTEX_BACKUP_PASSWORD_FILE": str(tmp_path / "pw"),
            },
            home=tmp_path,
        )
        runner = default_runner_factory(cfg)
        assert isinstance(runner, ResticRunner)


class TestParseBackupSummary:
    def test_parse_summary_snapshot_id_good(self) -> None:
        stdout = '{"message_type": "summary", "snapshot_id": "ab12cd34"}\n'
        assert parse_backup_summary_snapshot_id(stdout) == "ab12cd34"

    def test_parse_summary_interleaved_status_lines(self) -> None:
        stdout = (
            '{"message_type": "status", "percent_done": 0.5}\n'
            "restic non-json noise line\n"
            "\n"
            '{"message_type": "summary", "snapshot_id": "feed0042"}\n'
        )
        assert parse_backup_summary_snapshot_id(stdout) == "feed0042"

    def test_parse_summary_missing_raises(self) -> None:
        with pytest.raises(BackupError, match="no summary snapshot_id"):
            parse_backup_summary_snapshot_id('{"message_type": "status"}\n')

    def test_parse_summary_empty_id_raises(self) -> None:
        with pytest.raises(BackupError, match="no summary snapshot_id"):
            parse_backup_summary_snapshot_id('{"message_type": "summary", "snapshot_id": ""}\n')

    def test_parse_summary_skips_bad_json_and_arrays(self) -> None:
        stdout = '{broken json\n[1, 2]\n{"message_type": "summary", "snapshot_id": "aa"}\n'
        assert parse_backup_summary_snapshot_id(stdout) == "aa"


class TestParseSnapshotsLatest:
    def test_parse_snapshots_latest_good(self) -> None:
        stdout = '[{"short_id": "ab12", "time": "2026-07-06T22:19:41Z"}]'
        info = parse_snapshots_latest(stdout)
        assert info == SnapshotInfo(
            snapshot_id="ab12", time_utc=datetime(2026, 7, 6, 22, 19, 41, tzinfo=UTC)
        )

    def test_parse_snapshots_latest_full_id_fallback(self) -> None:
        stdout = '[{"id": "deadbeef", "time": "2026-07-06T22:19:41+05:30"}]'
        info = parse_snapshots_latest(stdout)
        assert info is not None
        assert info.snapshot_id == "deadbeef"
        assert info.time_utc == datetime(2026, 7, 6, 16, 49, 41, tzinfo=UTC)

    def test_parse_snapshots_latest_empty(self) -> None:
        assert parse_snapshots_latest("[]") is None

    @pytest.mark.parametrize(
        ("stdout", "match"),
        [
            ("{nope", "not valid JSON"),
            ('{"a": 1}', "not a JSON array"),
            ("[1]", "not a JSON object"),
            ('[{"time": "2026-07-06T22:19:41Z"}]', "carries no id"),
            ('[{"short_id": "ab", "time": 42}]', "not a string"),
            ('[{"short_id": "ab", "time": "junk"}]', "unparseable"),
            ('[{"short_id": "ab", "time": "2026-07-06T22:19:41"}]', "naive"),
        ],
    )
    def test_parse_snapshots_latest_malformed(self, stdout: str, match: str) -> None:
        with pytest.raises(BackupError, match=match):
            parse_snapshots_latest(stdout)


class TestParseLs:
    def test_parse_ls_file_nodes_only(self) -> None:
        stdout = (
            '{"time": "2026-07-06T22:19:41Z", "paths": ["/lake"]}\n'
            '{"type": "dir", "path": "/lake/bars"}\n'
            '{"type": "file", "path": "/lake/bars/a.parquet"}\n'
            '{"type": "file", "path": "/lake/bars/b.parquet"}\n'
            '{"type": "file", "path": ""}\n'
        )
        assert parse_ls_file_paths(stdout) == ("/lake/bars/a.parquet", "/lake/bars/b.parquet")

    def test_parse_ls_snapshot_time_header(self) -> None:
        stdout = (
            '{"time": "2026-07-06T22:19:41Z", "paths": ["/lake"]}\n{"type": "file", "path": "/x"}\n'
        )
        assert parse_ls_snapshot_time(stdout) == datetime(2026, 7, 6, 22, 19, 41, tzinfo=UTC)

    def test_parse_ls_snapshot_time_bad_time_none(self) -> None:
        assert parse_ls_snapshot_time('{"time": "junk"}\n') is None

    def test_parse_ls_snapshot_time_absent_none(self) -> None:
        assert parse_ls_snapshot_time('{"type": "file", "path": "/x"}\n') is None
