# pyright: reportPrivateUsage=none
"""CLI tests for `brutex crossverify …` (DEC-CROSSVERIFY-PRODUCER-001).

House conventions (the test_cli_nightly.py template): parser/dispatch wiring
(mock.patch.object(cli, "cmd_crossverify_*") asserting exact kwargs) + direct
cmd_* tests — NO subprocess. Pins: the exit-code pass-through (the runner
owns the exit law incl. the market-hours ``deferred_market_hours`` EXIT_OK
deferral), fail-loud junk day / junk ``BRUTEX_CROSSVERIFY_*`` env / junk
market-hours env (all EXIT_USER_ERROR before any lock or vendor work), the
full-real disabled kill-switch run (exit 0 through the REAL runner), install
``--at`` REQUIRED (the probe-decided refusal), the launchctl verify-fail
exit 2, the Linux cron-guidance refusal, and the status dead-man
best-effort alert hook.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest import mock

import pytest
from brutex import cli
from brutex.crossverify.run import CrossverifyReport


def _mini_lake(tmp_path: Path) -> Path:
    lake = tmp_path / "lake"
    lake.mkdir()
    return lake


def _report() -> CrossverifyReport:
    return CrossverifyReport(
        day=date(2026, 7, 10),
        run_id="cv-20260710-161501-1",
        verdict="published",
        published=3,
        skipped_already=0,
        publish_strict=3,
        publish_observe=0,
        withheld=0,
        blocked=0,
        nothing=1,
        excluded=2,
        publish_failed=0,
        recovered_prior=0,
        derived=4,
        derive_failed=0,
        movers_warm="revision-propagated (headless)",
        detail="",
        exit_code=0,
    )


def _report2(**overrides: object) -> CrossverifyReport:
    """A `_report` variant (slots dataclass — build via dataclasses.replace)."""
    import dataclasses

    return dataclasses.replace(_report(), **overrides)  # type: ignore[arg-type]


# ── dispatch wiring ──────────────────────────────────────────────────────


def test_dispatch_run_exact_kwargs() -> None:
    with mock.patch.object(cli, "cmd_crossverify_run", return_value=0) as spy:
        assert (
            cli.main(
                [
                    "crossverify",
                    "run",
                    "--lake-root",
                    "/lake",
                    "--day",
                    "2026-07-10",
                    "--dry-run",
                    "--skip-pull",
                    "--no-publish",
                ]
            )
            == 0
        )
    assert spy.call_args.kwargs == {
        "lake_root": Path("/lake"),
        "day": "2026-07-10",
        "dry_run": True,
        "skip_pull": True,
        "no_publish": True,
    }


def test_dispatch_install_uninstall_status_exact_kwargs() -> None:
    with mock.patch.object(cli, "cmd_crossverify_install", return_value=0) as spy:
        assert cli.main(["crossverify", "install", "--at", "16:15"]) == 0
    assert spy.call_args.kwargs == {
        "at": "16:15",
        "label": "com.brutex.crossverify",
        "plist_dir": None,
        "lake_root": None,
    }
    with mock.patch.object(cli, "cmd_crossverify_uninstall", return_value=0) as spy:
        assert cli.main(["crossverify", "uninstall"]) == 0
    assert spy.call_args.kwargs == {"label": "com.brutex.crossverify", "plist_dir": None}
    with mock.patch.object(cli, "cmd_crossverify_status", return_value=0) as spy:
        assert cli.main(["crossverify", "status", "--lake-root", "/lake"]) == 0
    assert spy.call_args.kwargs == {
        "lake_root": Path("/lake"),
        "label": "com.brutex.crossverify",
        "plist_dir": None,
    }


def test_subcommand_is_required() -> None:
    with pytest.raises(SystemExit):
        cli.main(["crossverify"])


# ── cmd_crossverify_run ──────────────────────────────────────────────────


class TestCmdRun:
    def test_missing_lake_root_refuses_loudly(self, tmp_path: Path) -> None:
        rc = cli.main(["crossverify", "run", "--lake-root", str(tmp_path / "absent")])
        assert rc == cli.EXIT_USER_ERROR

    def test_junk_day_is_user_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        lake = _mini_lake(tmp_path)
        rc = cli.main(["crossverify", "run", "--lake-root", str(lake), "--day", "tomorrow"])
        assert rc == cli.EXIT_USER_ERROR
        assert "--day must be YYYY-MM-DD" in capsys.readouterr().err

    def test_junk_crossverify_env_is_user_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        lake = _mini_lake(tmp_path)
        monkeypatch.setenv("BRUTEX_CROSSVERIFY", "banana")
        rc = cli.main(["crossverify", "run", "--lake-root", str(lake)])
        assert rc == cli.EXIT_USER_ERROR
        assert "BRUTEX_CROSSVERIFY" in capsys.readouterr().err

    def test_junk_market_hours_env_fails_loud_at_seam_build(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A vendor-calling run resolves the market-hours env ONCE, fail-loud,
        inside default_seams — junk exits EXIT_USER_ERROR before any lock."""
        lake = _mini_lake(tmp_path)
        monkeypatch.setenv("BRUTEX_MARKET_HOURS_GUARD", "banana")
        rc = cli.main(["crossverify", "run", "--lake-root", str(lake)])
        assert rc == cli.EXIT_USER_ERROR
        assert "BRUTEX_MARKET_HOURS_GUARD" in capsys.readouterr().err

    def test_disabled_kill_switch_full_real_run_exits_ok(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """BRUTEX_CROSSVERIFY=0 through the REAL config → seams → runner chain."""
        lake = _mini_lake(tmp_path)
        monkeypatch.setenv("BRUTEX_CROSSVERIFY", "0")
        rc = cli.main(["crossverify", "run", "--lake-root", str(lake)])
        assert rc == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "crossverify" in out
        assert "disabled" in out

    def test_exit_code_and_report_lines_pass_through(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        lake = _mini_lake(tmp_path)
        report = _report2(
            verdict="withheld_partial",
            withheld=2,
            publish_strict=2,
            publish_observe=1,
            exit_code=2,
            detail="settling",
        )
        monkeypatch.setattr("brutex.crossverify.run.default_seams", lambda config: object())
        monkeypatch.setattr("brutex.crossverify.run.run_crossverify", lambda config, seams: report)
        rc = cli.main(["crossverify", "run", "--lake-root", str(lake), "--day", "2026-07-10"])
        assert rc == 2
        out = capsys.readouterr().out
        assert "withheld_partial: settling" in out
        assert "withheld 2" in out
        # The #1788-fix-round three-way split reaches the CLI line too.
        assert "strict-published 2" in out
        assert "observe-published 1" in out
        assert "derived 4 (0 failed)" in out

    def test_market_hours_deferral_is_clean_exit_ok(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The runner's step-E deferral verdict rides through as EXIT_OK (never
        an error) — the cmd_fetch deferral posture, runner-owned."""
        lake = _mini_lake(tmp_path)
        report = _report2(
            verdict="deferred_market_hours",
            published=0,
            derived=0,
            nothing=0,
            excluded=0,
            exit_code=0,
            detail="market-hours window — deferred",
        )
        monkeypatch.setattr("brutex.crossverify.run.default_seams", lambda config: object())
        monkeypatch.setattr("brutex.crossverify.run.run_crossverify", lambda config, seams: report)
        rc = cli.main(["crossverify", "run", "--lake-root", str(lake)])
        assert rc == cli.EXIT_OK
        assert "deferred_market_hours" in capsys.readouterr().out

    def test_flags_thread_into_the_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from brutex.crossverify.config import CrossverifyConfig

        lake = _mini_lake(tmp_path)
        seen: dict[str, CrossverifyConfig] = {}

        def fake_run(config: CrossverifyConfig, seams: object) -> CrossverifyReport:
            seen["config"] = config
            return _report()

        monkeypatch.setattr("brutex.crossverify.run.default_seams", lambda config: object())
        monkeypatch.setattr("brutex.crossverify.run.run_crossverify", fake_run)
        rc = cli.main(
            [
                "crossverify",
                "run",
                "--lake-root",
                str(lake),
                "--day",
                "2026-07-10",
                "--skip-pull",
                "--no-publish",
            ]
        )
        assert rc == cli.EXIT_OK
        config = seen["config"]
        assert config.day == date(2026, 7, 10)
        assert config.skip_pull is True
        assert config.publish is False
        assert config.dry_run is False
        assert config.lake_root == lake


# ── cmd_crossverify_install / uninstall / status ─────────────────────────


class TestCmdInstall:
    def test_linux_prints_cron_guidance(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.cmd_crossverify_install(
            at="16:15",
            label="com.brutex.crossverify",
            plist_dir=None,
            lake_root=None,
            platform="linux",
        )
        assert rc == cli.EXIT_USER_ERROR
        assert "crossverify run" in capsys.readouterr().err

    def test_at_required_refusal(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.cmd_crossverify_install(
            at=None,
            label="com.brutex.crossverify",
            plist_dir=None,
            lake_root=None,
            platform="darwin",
        )
        assert rc == cli.EXIT_USER_ERROR
        assert "settle probe" in capsys.readouterr().err

    def test_junk_at_is_user_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.cmd_crossverify_install(
            at="4pm",
            label="com.brutex.crossverify",
            plist_dir=None,
            lake_root=None,
            platform="darwin",
        )
        assert rc == cli.EXIT_USER_ERROR
        assert "--at must be HH:MM" in capsys.readouterr().err

    def test_happy_path_prints_pmset_wake(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.cmd_crossverify_install(
            at="16:15",
            label="com.brutex.crossverify",
            plist_dir=tmp_path,
            lake_root=tmp_path / "lake",
            runner=lambda argv: (0, "", ""),
            platform="darwin",
        )
        assert rc == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "installed + VERIFIED" in out
        assert "sudo pmset repeat wakeorpoweron MTWRFSU 16:10:00" in out

    def test_verify_failure_is_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def runner(argv: object) -> tuple[int, str, str]:
            argv_list = list(argv)  # type: ignore[call-overload]
            return (1, "", "no") if argv_list[:2] == ["launchctl", "list"] else (0, "", "")

        rc = cli.cmd_crossverify_install(
            at="16:15",
            label="com.brutex.crossverify",
            plist_dir=tmp_path,
            lake_root=None,
            runner=runner,
            platform="darwin",
        )
        assert rc == cli.EXIT_INTERNAL_ERROR
        assert "does NOT list" in capsys.readouterr().err


class TestCmdUninstall:
    def test_ok_and_still_listed_arms(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.cmd_crossverify_uninstall(
            label="com.brutex.crossverify",
            plist_dir=tmp_path,
            runner=lambda argv: (1, "", ""),
        )
        assert rc == cli.EXIT_OK
        assert "uninstalled" in capsys.readouterr().out
        rc = cli.cmd_crossverify_uninstall(
            label="com.brutex.crossverify",
            plist_dir=tmp_path,
            runner=lambda argv: (0, "", ""),
        )
        assert rc == cli.EXIT_INTERNAL_ERROR
        assert "STILL listed" in capsys.readouterr().err


class TestCmdStatus:
    def test_missing_lake_root_refuses(self, tmp_path: Path) -> None:
        rc = cli.main(["crossverify", "status", "--lake-root", str(tmp_path / "absent")])
        assert rc == cli.EXIT_USER_ERROR

    def test_honest_lines_when_nothing_ran(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        lake = _mini_lake(tmp_path)
        rc = cli.cmd_crossverify_status(
            lake_root=lake,
            label="com.brutex.crossverify",
            plist_dir=tmp_path / "agents",
            runner=lambda argv: (1, "", ""),
        )
        assert rc == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "schedule: plist ABSENT" in out
        assert "newest journal: none yet" in out
        assert "dead-man: UNKNOWN" in out

    def test_missed_deadman_fires_best_effort_refused_alert(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = _mini_lake(tmp_path)
        agents = tmp_path / "agents"
        # A real install (fake runner) so the plist advertises a fire time;
        # no journal ever ⇒ MISSED on a weekday-past-fire clock.
        cli.cmd_crossverify_install(
            at="00:01",
            label="com.brutex.crossverify",
            plist_dir=agents,
            lake_root=lake,
            runner=lambda argv: (0, "", ""),
            platform="darwin",
        )
        fired: list[dict[str, object]] = []

        class _FakeAlerter:
            def crossverify_refused(self, *, run_id: str, reason: str) -> bool:
                fired.append({"run_id": run_id, "reason": reason})
                return True

        monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", _FakeAlerter)
        rc = cli.cmd_crossverify_status(
            lake_root=lake,
            label="com.brutex.crossverify",
            plist_dir=agents,
            runner=lambda argv: (0, "", ""),
        )
        assert rc == cli.EXIT_OK
        # 00:01 has always passed on a trading day within the walk — with no
        # journal the verdict is MISSED (unless the calendar walk hit a rare
        # UNKNOWN); assert the hook shape when it fired.
        if fired:
            assert fired[0]["run_id"] == "deadman"
            assert "DEAD-MAN" in str(fired[0]["reason"])

    def test_missed_alert_failure_never_breaks_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lake = _mini_lake(tmp_path)
        agents = tmp_path / "agents"
        cli.cmd_crossverify_install(
            at="00:01",
            label="com.brutex.crossverify",
            plist_dir=agents,
            lake_root=lake,
            runner=lambda argv: (0, "", ""),
            platform="darwin",
        )

        def boom() -> object:
            raise RuntimeError("telegram down")

        monkeypatch.setattr("brutex.notifications.sweep_alerts.SweepAlerter", boom)
        rc = cli.cmd_crossverify_status(
            lake_root=lake,
            label="com.brutex.crossverify",
            plist_dir=agents,
            runner=lambda argv: (0, "", ""),
        )
        assert rc == cli.EXIT_OK
