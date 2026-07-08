# pyright: reportPrivateUsage=none
"""CLI tests for `brutex nightly …` (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

House conventions (the test_cli_enrich_greeks_cmd.py template): parser-level
tests (build_parser + SystemExit/capsys for --help), dispatch-wiring tests
(mock.patch.object(cli, "cmd_nightly_*") asserting exact call_args), and
direct cmd_* tests over real fixture lakes — NO subprocess. Pins: the
three-flag hard refusal (STRICTER than the daemon precedent), junk env
fail-loud pre-chain, dates_required=False fills default_date_range() while
``sweep-all``/``sweep-daemon`` still REQUIRE dates (regression), the restore
--yes gate + EXIT_VERIFY_FINDINGS arm, and the launchd verify-fail = exit 2.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from brutex import cli
from brutex.config.credentials_types import CredentialsError
from brutex.nightly.backup import create_snapshot
from brutex.nightly.chain import LINK_ORDER, LinkOutcome, LinkStatus, NightlyReport


@pytest.fixture(autouse=True)
def _no_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never resolve real AWS credentials (the test_cli_retry_storm idiom)."""

    def refuse() -> Any:
        raise CredentialsError("no AWS in tests")

    monkeypatch.setattr("brutex.config.credentials.resolve_groww_aws_credentials", refuse)


def _mini_lake(tmp_path: Path) -> tuple[Path, Path]:
    lake = tmp_path / "lake"
    results = lake / "sweeps"
    (lake / "window_ledger").mkdir(parents=True)
    (lake / "window_ledger" / "ledger.parquet").write_bytes(b"LEDGER")
    (lake / "registry.duckdb").write_bytes(b"R")
    results.mkdir(parents=True)
    return lake, results


def _green_report(results_root: Path) -> NightlyReport:
    return NightlyReport(
        schema_version=1,
        nightly_id="nightly-cli-test",
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


class _FakeRunner:
    def __init__(self, *, list_rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.list_rc = list_rc

    def __call__(self, argv: Sequence[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        if argv[1] == "list":
            return (self.list_rc, "", "")
        return (0, "", "")


# ── parser tree ──────────────────────────────────────────────────────────


def test_parser_tree_and_defaults() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["nightly", "run"])
    assert args.command == "nightly"
    assert args.nightly_command == "run"
    assert args.start is None  # dates_required=False on the nightly grid
    assert args.end is None
    assert args.keep is None
    assert args.backup_root is None
    assert args.skip_backup is False
    assert args.skip_sweep is False
    assert args.dry_run is False
    assert args.symbols == "NIFTY,BANKNIFTY"  # the ONE shared grid contract
    install = parser.parse_args(["nightly", "install"])
    assert install.at == "01:30"
    assert install.label == "com.brutex.nightly"
    restore = parser.parse_args(["nightly", "restore", "--snapshot", "/x"])
    assert restore.snapshot == Path("/x")
    assert restore.yes is False
    status = parser.parse_args(["nightly", "status"])
    assert status.label == "com.brutex.nightly"


def test_nightly_help_names_all_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["nightly", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for sub in ("run", "backup", "restore", "install", "uninstall", "status"):
        assert sub in out


def test_nightly_requires_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(["nightly"])
    assert excinfo.value.code == 2
    assert "required" in capsys.readouterr().err


def test_sweep_all_and_daemon_still_require_dates(capsys: pytest.CaptureFixture[str]) -> None:
    """The dates_required=False relaxation must NOT leak to the sweep surfaces."""
    parser = cli.build_parser()
    for argv in (["sweep-all"], ["sweep-daemon"]):
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(argv)
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "--start" in err or "--end" in err


# ── dispatch wiring ──────────────────────────────────────────────────────


def test_dispatch_run_passes_the_namespace() -> None:
    with mock.patch.object(cli, "cmd_nightly_run", return_value=0) as spy:
        assert cli.main(["nightly", "run", "--dry-run"]) == 0
    [call] = spy.call_args_list
    (namespace,) = call.args
    assert namespace.nightly_command == "run"
    assert namespace.dry_run is True


def test_dispatch_backup_exact_kwargs(tmp_path: Path) -> None:
    with mock.patch.object(cli, "cmd_nightly_backup", return_value=0) as spy:
        assert (
            cli.main(
                [
                    "nightly",
                    "backup",
                    "--lake-root",
                    str(tmp_path),
                    "--backup-root",
                    "/bk",
                    "--keep",
                    "3",
                ]
            )
            == 0
        )
    assert spy.call_args == mock.call(tmp_path, results_root=None, backup_root=Path("/bk"), keep=3)


def test_dispatch_restore_exact_kwargs() -> None:
    with mock.patch.object(cli, "cmd_nightly_restore", return_value=0) as spy:
        assert cli.main(["nightly", "restore", "--snapshot", "/snap", "--yes"]) == 0
    assert spy.call_args == mock.call(
        snapshot=Path("/snap"), lake_root=None, results_root=None, dry_run=False, yes=True
    )


def test_dispatch_install_uninstall_status_exact_kwargs() -> None:
    with mock.patch.object(cli, "cmd_nightly_install", return_value=0) as spy:
        assert cli.main(["nightly", "install", "--at", "02:15"]) == 0
    assert spy.call_args == mock.call(
        at="02:15", label="com.brutex.nightly", plist_dir=None, lake_root=None, backup_root=None
    )
    with mock.patch.object(cli, "cmd_nightly_uninstall", return_value=0) as spy:
        assert cli.main(["nightly", "uninstall"]) == 0
    assert spy.call_args == mock.call(label="com.brutex.nightly", plist_dir=None)
    with mock.patch.object(cli, "cmd_nightly_status", return_value=0) as spy:
        assert cli.main(["nightly", "status"]) == 0
    assert spy.call_args == mock.call(
        lake_root=None, results_root=None, label="com.brutex.nightly", plist_dir=None
    )


# ── cmd_nightly_run ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "flag", ["--allow-out-of-scope", "--acknowledge-sla-overrun", "--allow-missing-slices"]
)
def test_run_hard_refuses_every_escape_flag(
    flag: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lake, _results = _mini_lake(tmp_path)
    rc = cli.main(["nightly", "run", "--lake-root", str(lake), flag])
    assert rc == cli.EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "nightly never overrides safety gates" in err
    assert flag in err
    assert "sweep-all manually" in err


def test_run_missing_lake_is_user_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["nightly", "run", "--lake-root", str(tmp_path / "absent")])
    assert rc == cli.EXIT_USER_ERROR
    assert "[brutex] lake root not found" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("env_key", "env_value", "match"),
    [
        ("BRUTEX_NIGHTLY_BACKUP_KEEP", "weekly", "BRUTEX_NIGHTLY_BACKUP_KEEP"),
        ("BRUTEX_NIGHTLY_BACKUP_VERIFY", "maybe", "BRUTEX_NIGHTLY_BACKUP_VERIFY"),
        ("BRUTEX_NIGHTLY_SWEEP_FRESH_SECONDS", "fast", "BRUTEX_NIGHTLY_SWEEP_FRESH_SECONDS"),
        ("BRUTEX_PIPELINE_STRICT_GATE", "banana", "BRUTEX_PIPELINE_STRICT_GATE"),
    ],
)
def test_run_junk_env_fails_loud_pre_chain(
    env_key: str,
    env_value: str,
    match: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lake, _results = _mini_lake(tmp_path)
    monkeypatch.setenv(env_key, env_value)
    called = {"n": 0}
    monkeypatch.setattr(
        "brutex.nightly.run.run_nightly", lambda *a, **k: called.__setitem__("n", 1)
    )
    rc = cli.main(["nightly", "run", "--lake-root", str(lake)])
    assert rc == cli.EXIT_USER_ERROR
    assert match in capsys.readouterr().err
    assert called["n"] == 0  # the chain NEVER started


def test_run_keep_below_one_and_bad_dates_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lake, _results = _mini_lake(tmp_path)
    assert cli.main(["nightly", "run", "--lake-root", str(lake), "--keep", "0"]) == 1
    assert "--keep must be >= 1" in capsys.readouterr().err
    assert cli.main(["nightly", "run", "--lake-root", str(lake), "--from-date", "not-a-date"]) == 1
    assert "not-a-date" in capsys.readouterr().err
    rc = cli.main(
        [
            "nightly",
            "run",
            "--lake-root",
            str(lake),
            "--from-date",
            "2025-01-01",
            "--to-date",
            "2024-01-01",
        ]
    )
    assert rc == 1
    assert "is after" in capsys.readouterr().err


def test_run_green_builds_plan_and_prints_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lake, results = _mini_lake(tmp_path)
    captured: dict[str, Any] = {}

    def fake_run_nightly(plan: Any, *, seams: Any = None) -> NightlyReport:
        captured["plan"] = plan
        return _green_report(results)

    monkeypatch.setattr("brutex.nightly.run.run_nightly", fake_run_nightly)
    rc = cli.main(
        [
            "nightly",
            "run",
            "--lake-root",
            str(lake),
            "--keep",
            "3",
            "--skip-sweep",
            "--top",
            "50",
        ]
    )
    assert rc == 0
    plan = captured["plan"]
    assert plan.lake_root == lake
    assert plan.results_root == lake / "sweeps"
    assert plan.keep == 3
    assert plan.skip_sweep is True
    assert plan.skip_backup is False
    # The sweep kwargs rode the daemon key-list law with date defaults filled.
    assert plan.sweep_kwargs["top"] == 50
    assert plan.sweep_kwargs["start"] == "2020-01-01"
    assert len(plan.sweep_kwargs["end"]) == 10  # previous IST day, YYYY-MM-DD
    assert "allow_out_of_scope" not in plan.sweep_kwargs
    out = capsys.readouterr().out
    assert "🌙 Nightly nightly-cli-test — ✅ OK in 0:01." in out


def test_run_dry_run_end_to_end_real_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The documented post-install smoke, driven through the REAL run_nightly."""
    lake, _results = _mini_lake(tmp_path)
    monkeypatch.setenv("BRUTEX_NIGHTLY_MIN_FREE_LAKE_BYTES", "0")
    monkeypatch.setenv("BRUTEX_NIGHTLY_MIN_FREE_BACKUP_BYTES", "0")
    before = {p.as_posix() for p in lake.rglob("*")}
    rc = cli.main(["nightly", "run", "--lake-root", str(lake), "--dry-run"])
    assert rc == 0
    assert {p.as_posix() for p in lake.rglob("*")} == before  # zero mutation
    out = capsys.readouterr().out
    assert (
        "backup skipped (dry-run)" in out.replace("SKIPPED", "skipped").lower() or "dry-run" in out
    )


# ── cmd_nightly_backup / restore (real modules over fixture lakes) ──────


def test_backup_seals_and_audits(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lake, results = _mini_lake(tmp_path)
    rc = cli.main(
        [
            "nightly",
            "backup",
            "--lake-root",
            str(lake),
            "--backup-root",
            str(tmp_path / "bk"),
            "--keep",
            "2",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "SEALED" in out
    sealed = [p for p in (tmp_path / "bk").iterdir() if not p.name.endswith(".partial")]
    assert len(sealed) == 1
    assert (sealed[0] / "manifest.json").exists()
    audit = results / "nightly" / "audit" / "nightly.jsonl"
    assert "BACKUP_SNAPSHOT" in audit.read_text(encoding="utf-8")


def test_backup_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lake, results = _mini_lake(tmp_path)
    # Junk keep env.
    monkeypatch.setenv("BRUTEX_NIGHTLY_BACKUP_KEEP", "many")
    assert cli.main(["nightly", "backup", "--lake-root", str(lake)]) == 1
    monkeypatch.delenv("BRUTEX_NIGHTLY_BACKUP_KEEP")
    # Missing lake.
    assert cli.main(["nightly", "backup", "--lake-root", str(tmp_path / "nope")]) == 1
    # Nightly lock held by a peer.
    from brutex.adapters.groww import pull_singleflight as ps

    with ps.nightly_singleflight_lock(lake, holder_note="nightly:peer"):
        rc = cli.main(
            ["nightly", "backup", "--lake-root", str(lake), "--backup-root", str(tmp_path / "b")]
        )
    assert rc == 1
    assert "(holder: nightly:peer)" in capsys.readouterr().err
    # A live manual pull.
    with ps.pull_singleflight_lock(lake):
        rc = cli.main(
            ["nightly", "backup", "--lake-root", str(lake), "--backup-root", str(tmp_path / "b")]
        )
    assert rc == 1
    # A live sweep (fresh running progress).
    progress = results / "progress" / "sweep-progress.json"
    progress.parent.mkdir(parents=True)
    progress.write_text('{"state": "running"}', encoding="utf-8")
    rc = cli.main(
        ["nightly", "backup", "--lake-root", str(lake), "--backup-root", str(tmp_path / "b")]
    )
    assert rc == 1
    assert "never back up a live results tree" in capsys.readouterr().err
    progress.unlink()
    # Disk floor breached.
    monkeypatch.setenv("BRUTEX_NIGHTLY_MIN_FREE_BACKUP_BYTES", str(10**18))
    rc = cli.main(
        ["nightly", "backup", "--lake-root", str(lake), "--backup-root", str(tmp_path / "b")]
    )
    assert rc == 1
    assert "disk floor breached" in capsys.readouterr().err


def test_backup_copy_failure_is_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lake, _results = _mini_lake(tmp_path)

    def boom(**_kwargs: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("brutex.nightly.backup.create_snapshot", boom)
    # cmd imports create_snapshot call-time FROM the module — patch the source.
    rc = cli.main(
        ["nightly", "backup", "--lake-root", str(lake), "--backup-root", str(tmp_path / "b")]
    )
    assert rc == 2
    assert "backup FAILED" in capsys.readouterr().err


def _seal_snapshot(tmp_path: Path, lake: Path, results: Path) -> Path:
    return create_snapshot(
        lake_root=lake,
        results_root=results,
        backup_root=tmp_path / "bk",
        snapshot_id="snap-cli",
        nightly_id=None,
    ).snapshot_path


def test_restore_dry_run_plan_and_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lake, results = _mini_lake(tmp_path)
    snapshot = _seal_snapshot(tmp_path, lake, results)
    rc = cli.main(
        ["nightly", "restore", "--snapshot", str(snapshot), "--lake-root", str(lake), "--dry-run"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "restore plan" in out
    assert "verify OK" in out
    assert "2 lake/window_ledger/ledger.parquet" in out  # the ordered plan, ledger LAST
    # A flipped byte → EXIT_VERIFY_FINDINGS, still without locks/writes.
    target = snapshot / "lake" / "registry.duckdb"
    target.write_bytes(b"FLIPPED!")
    rc = cli.main(
        ["nightly", "restore", "--snapshot", str(snapshot), "--lake-root", str(lake), "--dry-run"]
    )
    assert rc == cli.EXIT_VERIFY_FINDINGS
    assert "verify FAILED" in capsys.readouterr().err


def test_restore_requires_yes_then_restores(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lake, results = _mini_lake(tmp_path)
    snapshot = _seal_snapshot(tmp_path, lake, results)
    (lake / "registry.duckdb").write_bytes(b"POISONED")
    rc = cli.main(["nightly", "restore", "--snapshot", str(snapshot), "--lake-root", str(lake)])
    assert rc == 1
    assert "refusing to restore without --yes" in capsys.readouterr().err
    assert (lake / "registry.duckdb").read_bytes() == b"POISONED"  # untouched
    rc = cli.main(
        ["nightly", "restore", "--snapshot", str(snapshot), "--lake-root", str(lake), "--yes"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert (lake / "registry.duckdb").read_bytes() == b"R"  # byte-restored
    assert "ledger restored last: True" in out
    assert "brutex doctor" in out
    audit_text = (results / "nightly" / "audit" / "nightly.jsonl").read_text(encoding="utf-8")
    assert "RESTORE_START" in audit_text
    assert "RESTORE_DONE" in audit_text


def test_restore_preserves_live_nightly_audit_chain_on_disk(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression (review round 5 MAJOR, reproduced end-to-end pre-fix): a
    snapshot taken while the cross-run nightly audit chain already existed
    used to CONTAIN that chain; restore then os.replace'd the writer's open
    inode, so RESTORE_START/RESTORE_DONE were written to a dead inode and the
    on-disk chain rolled back to snapshot time (Golden Rule #8 violation)."""
    from brutex.engine.sweep_audit import verify_chain
    from brutex.nightly.report import open_nightly_audit

    lake, results = _mini_lake(tmp_path)
    audit_path = results / "nightly" / "audit" / "nightly.jsonl"
    with open_nightly_audit(results) as audit:
        audit.append(action="SNAPSHOT_ERA", subject="nightly:pre", payload={})
    snapshot = _seal_snapshot(tmp_path, lake, results)
    # The chain moves on AFTER the snapshot — restore must not roll it back.
    with open_nightly_audit(results) as audit:
        audit.append(action="POST_SNAPSHOT", subject="nightly:post", payload={})

    rc = cli.main(
        ["nightly", "restore", "--snapshot", str(snapshot), "--lake-root", str(lake), "--yes"]
    )
    assert rc == 0
    capsys.readouterr()
    text = audit_path.read_text(encoding="utf-8")
    assert "SNAPSHOT_ERA" in text
    assert "POST_SNAPSHOT" in text  # NOT rolled back to snapshot time
    assert "RESTORE_START" in text  # durable ON DISK, not on a dead inode
    assert "RESTORE_DONE" in text
    assert verify_chain(audit_path) == 4  # the hash chain is still green


def test_restore_legacy_snapshot_skips_live_audit_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A LEGACY snapshot (sealed pre-round-5, audit chain in-manifest) restores
    everything EXCEPT the live audit chain, and the plan says so loudly."""
    from tests.fakes.nightly_snapshot import inject_legacy_audit_entry

    lake, results = _mini_lake(tmp_path)
    snapshot = _seal_snapshot(tmp_path, lake, results)
    key = inject_legacy_audit_entry(snapshot)
    live_audit = results / "nightly" / "audit" / "nightly.jsonl"
    live_audit.parent.mkdir(parents=True)

    rc = cli.main(
        ["nightly", "restore", "--snapshot", str(snapshot), "--lake-root", str(lake), "--yes"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert f"SKIP {key}" in out
    assert "never overwritten by restore" in out
    text = live_audit.read_text(encoding="utf-8")
    assert "SNAPSHOT-ERA-CHAIN" not in text  # the snapshot copy never landed
    assert "RESTORE_START" in text
    assert "RESTORE_DONE" in text


def test_restore_refusals_and_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lake, results = _mini_lake(tmp_path)
    snapshot = _seal_snapshot(tmp_path, lake, results)
    # Structural refusal: junk manifest.
    bad = tmp_path / "bad-snap"
    bad.mkdir()
    (bad / "manifest.json").write_text("{torn", encoding="utf-8")
    assert (
        cli.main(["nightly", "restore", "--snapshot", str(bad), "--lake-root", str(lake), "--yes"])
        == 1
    )
    # Live verify failure → EXIT_VERIFY_FINDINGS, lake untouched.
    flipped = snapshot / "lake" / "window_ledger" / "ledger.parquet"
    flipped.write_bytes(b"XXXXXX")
    rc = cli.main(
        ["nightly", "restore", "--snapshot", str(snapshot), "--lake-root", str(lake), "--yes"]
    )
    assert rc == cli.EXIT_VERIFY_FINDINGS
    assert "FAILED verification; nothing restored" in capsys.readouterr().err
    # Lock held → refused.
    from brutex.adapters.groww import pull_singleflight as ps

    snapshot2 = create_snapshot(
        lake_root=lake,
        results_root=results,
        backup_root=tmp_path / "bk2",
        snapshot_id="snap-2",
        nightly_id=None,
    ).snapshot_path
    with ps.nightly_singleflight_lock(lake):
        rc = cli.main(
            ["nightly", "restore", "--snapshot", str(snapshot2), "--lake-root", str(lake), "--yes"]
        )
    assert rc == 1
    with ps.pull_singleflight_lock(lake):
        rc = cli.main(
            ["nightly", "restore", "--snapshot", str(snapshot2), "--lake-root", str(lake), "--yes"]
        )
    assert rc == 1
    # A live sweep refuses too.
    progress = results / "progress" / "sweep-progress.json"
    progress.parent.mkdir(parents=True)
    progress.write_text('{"state": "running"}', encoding="utf-8")
    rc = cli.main(
        ["nightly", "restore", "--snapshot", str(snapshot2), "--lake-root", str(lake), "--yes"]
    )
    assert rc == 1
    assert "stop the dashboard/pull/sweep first" in capsys.readouterr().err
    progress.unlink()

    # Mid-copy OSError → EXIT_INTERNAL_ERROR with the re-run remediation.
    def exploding_restore(_plan: Any, **_kwargs: Any) -> Any:
        raise OSError(5, "io error")

    monkeypatch.setattr("brutex.nightly.restore.restore_snapshot", exploding_restore)
    rc = cli.main(
        ["nightly", "restore", "--snapshot", str(snapshot2), "--lake-root", str(lake), "--yes"]
    )
    assert rc == 2
    assert "RE-RUN this restore" in capsys.readouterr().err


def test_restore_refused_exception_maps_to_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lake, results = _mini_lake(tmp_path)
    snapshot = _seal_snapshot(tmp_path, lake, results)
    from brutex.nightly.restore import RestoreRefused

    def refusing_restore(_plan: Any, **_kwargs: Any) -> Any:
        raise RestoreRefused("unknown namespace nonsense")

    monkeypatch.setattr("brutex.nightly.restore.restore_snapshot", refusing_restore)
    rc = cli.main(
        ["nightly", "restore", "--snapshot", str(snapshot), "--lake-root", str(lake), "--yes"]
    )
    assert rc == 1
    assert "unknown namespace nonsense" in capsys.readouterr().err


# ── install / uninstall / status ─────────────────────────────────────────


def test_install_non_darwin_prints_cron_guidance(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cli.cmd_nightly_install(
        at="01:30",
        label="com.brutex.nightly",
        plist_dir=None,
        lake_root=None,
        backup_root=None,
        platform="linux",
    )
    assert rc == cli.EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "cron" in err
    assert "nightly run" in err


def test_install_bad_at_refused(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.cmd_nightly_install(
        at="25:99",
        label="com.brutex.nightly",
        plist_dir=None,
        lake_root=None,
        backup_root=None,
        platform="darwin",
    )
    assert rc == cli.EXIT_USER_ERROR
    assert "--at must be HH:MM" in capsys.readouterr().err


def test_install_green_and_verify_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))  # log dir under a temp HOME
    runner = _FakeRunner()
    rc = cli.cmd_nightly_install(
        at="01:30",
        label="com.brutex.nightly",
        plist_dir=tmp_path / "LaunchAgents",
        lake_root=Path("/Volumes/lake"),
        backup_root=Path("/Volumes/backups"),
        runner=runner,
        platform="darwin",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "VERIFIED" in out
    assert "pmset repeat wakeorpoweron" in out  # the sleep/wake honesty note
    plist = (tmp_path / "LaunchAgents" / "com.brutex.nightly.plist").read_text(encoding="utf-8")
    assert "/Volumes/lake" in plist
    assert "/Volumes/backups" in plist
    # Verify-fail (launchctl list rc 1) ⇒ installed-but-unverified IS a failure.
    rc = cli.cmd_nightly_install(
        at="01:30",
        label="com.brutex.nightly",
        plist_dir=tmp_path / "LaunchAgents",
        lake_root=None,
        backup_root=None,
        runner=_FakeRunner(list_rc=1),
        platform="darwin",
    )
    assert rc == cli.EXIT_INTERNAL_ERROR
    assert "the job will NEVER fire" in capsys.readouterr().err


def test_uninstall_arms(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    (plist_dir / "com.brutex.nightly.plist").write_text("<plist/>", encoding="utf-8")
    rc = cli.cmd_nightly_uninstall(
        label="com.brutex.nightly", plist_dir=plist_dir, runner=_FakeRunner(list_rc=1)
    )
    assert rc == 0
    assert "uninstalled" in capsys.readouterr().out
    rc = cli.cmd_nightly_uninstall(
        label="com.brutex.nightly", plist_dir=plist_dir, runner=_FakeRunner(list_rc=0)
    )
    assert rc == cli.EXIT_INTERNAL_ERROR
    assert "STILL listed" in capsys.readouterr().err


def test_status_prints_honest_lines(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lake, results = _mini_lake(tmp_path)
    run_dir = results / "nightly" / "nightly-S"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(
        json.dumps({"nightly_id": "nightly-S", "exit_code": 0, "blocker": None}),
        encoding="utf-8",
    )
    rc = cli.cmd_nightly_status(
        lake_root=lake,
        results_root=None,
        label="com.brutex.nightly",
        plist_dir=tmp_path / "LaunchAgents",
        runner=_FakeRunner(list_rc=1),
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "schedule: plist ABSENT" in out
    assert "nightly-S exit 0" in out
    assert "latest snapshot: none yet" in out
    assert "existence ≠ held" in out


def test_status_missing_lake_is_user_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["nightly", "status", "--lake-root", str(tmp_path / "absent")])
    assert rc == 1
    assert "lake root not found" in capsys.readouterr().err


def test_launchctl_runner_arms() -> None:
    import sys as _sys

    rc, out, _err = cli._launchctl_runner([_sys.executable, "-c", "print('hi')"])
    assert rc == 0
    assert out.strip() == "hi"
    rc, _out, err = cli._launchctl_runner(["/nonexistent/never-a-binary"])
    assert rc == 127
    assert err  # the OSError text, honestly surfaced


def test_default_plist_dir_is_launch_agents() -> None:
    assert cli._default_plist_dir() == Path.home() / "Library" / "LaunchAgents"


def test_backup_explicit_keep_below_one_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lake, _results = _mini_lake(tmp_path)
    rc = cli.main(["nightly", "backup", "--lake-root", str(lake), "--keep", "0"])
    assert rc == 1
    assert "--keep must be >= 1" in capsys.readouterr().err


def test_backup_retention_prune_is_audited(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lake, results = _mini_lake(tmp_path)
    broot = tmp_path / "bk"
    for _ in range(2):
        assert (
            cli.main(
                [
                    "nightly",
                    "backup",
                    "--lake-root",
                    str(lake),
                    "--backup-root",
                    str(broot),
                    "--keep",
                    "1",
                ]
            )
            == 0
        )
    out = capsys.readouterr().out
    assert "pruned 1" in out  # the older snapshot was retired
    audit_text = (results / "nightly" / "audit" / "nightly.jsonl").read_text(encoding="utf-8")
    assert "BACKUP_PRUNE" in audit_text
    sealed = [p for p in broot.iterdir() if not p.name.endswith(".partial")]
    assert len(sealed) == 1
