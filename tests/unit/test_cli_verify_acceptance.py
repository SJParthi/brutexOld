"""CLI shim pins: registration, dispatch, flag validation, exit mapping.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). The shim is a thin
``# pragma: no cover`` orchestration layer by house convention; these tests
drive it through ``main()`` (the CLI-harness discipline) so the parser wiring
+ user-error arms are genuinely executed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import pytest
import structlog
from brutex import cli
from brutex.cli import EXIT_INTERNAL_ERROR, EXIT_OK, EXIT_USER_ERROR, build_parser, main
from brutex.observability.logging import LOG_LEVEL_ENV, reset_configuration

from tests.fakes.acceptance_twin import make_twin


@pytest.fixture(autouse=True)
def _clean_logging_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate global structlog config + the env override per test.

    Round 3 (C-F2): the verify-acceptance dispatch now runs the #1630
    logging guard (process-global), so every ``main()`` drive here starts
    genuinely UNCONFIGURED — the fails-before state of the stdout-purity
    regressions — and leaks nothing to later tests.
    """
    monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
    reset_configuration()
    yield
    reset_configuration()


class TestParserRegistration:
    def test_subcommand_registered_with_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["verify-acceptance"])
        assert args.command == "verify-acceptance"
        assert args.sample == 12
        assert args.months == 6
        assert args.top_n == 20
        assert args.latency_rounds == 5
        assert args.abstain_budget == 3
        assert args.checks is None
        assert args.self_test is False
        assert args.no_self_test is False

    def test_all_flags_parse(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "verify-acceptance",
                "--lake-root",
                "/tmp/lake",
                "--checks",
                "unique,tf",
                "--full",
                "--sample",
                "5",
                "--seed",
                "2026-07-01",
                "--months",
                "3",
                "--date",
                "2026-07-09",
                "--top-n",
                "10",
                "--latency-budget",
                "movers_hit=900",
                "--latency-rounds",
                "2",
                "--abstain-budget",
                "0",
                "--max-runtime-s",
                "60",
                "--force-during-pull",
                "--json",
                "--out",
                "/tmp/out",
                "--repro",
                "tf",
            ]
        )
        assert args.full is True
        assert args.latency_budget == ["movers_hit=900"]
        assert args.repro == "tf"


class TestReproAndUserErrors:
    def test_repro_prints_command_exit_0(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["verify-acceptance", "--repro", "unique"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "brutex verify-acceptance --checks unique" in out

    def test_repro_unknown_check_exit_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["verify-acceptance", "--repro", "bogus"]) == EXIT_USER_ERROR
        assert "unknown check id" in capsys.readouterr().err

    def test_missing_lake_root_exit_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(
            ["verify-acceptance", "--lake-root", str(tmp_path / "nowhere"), "--no-self-test"]
        )
        assert code == EXIT_USER_ERROR

    def test_unknown_checks_token_exit_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        lake = make_twin(tmp_path)
        code = main(["verify-acceptance", "--lake-root", str(lake), "--checks", "bogus"])
        assert code == EXIT_USER_ERROR
        assert "unknown check id(s)" in capsys.readouterr().err

    def test_empty_checks_exit_1(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        assert (
            main(["verify-acceptance", "--lake-root", str(lake), "--checks", " , "])
            == EXIT_USER_ERROR
        )

    def test_bad_latency_budget_exit_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        lake = make_twin(tmp_path)
        code = main(
            [
                "verify-acceptance",
                "--lake-root",
                str(lake),
                "--latency-budget",
                "movers_hit=fast",
            ]
        )
        assert code == EXIT_USER_ERROR
        assert "bad --latency-budget" in capsys.readouterr().err

    def test_top_n_cap_exit_1(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        code = main(["verify-acceptance", "--lake-root", str(lake), "--top-n", "500"])
        assert code == EXIT_USER_ERROR

    def test_conflicting_selftest_flags_exit_1(self, tmp_path: Path) -> None:
        lake = make_twin(tmp_path)
        code = main(
            ["verify-acceptance", "--lake-root", str(lake), "--no-self-test", "--self-test"]
        )
        assert code == EXIT_USER_ERROR


class TestEndToEndViaMain:
    def test_json_run_exit_0(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        import json

        lake = make_twin(tmp_path)
        capsys.readouterr()  # drain the twin-build setup logs (pre-CLI, in-process)
        code = main(
            [
                "verify-acceptance",
                "--lake-root",
                str(lake),
                "--checks",
                "unique",
                "--no-self-test",
                "--json",
            ]
        )
        assert code == EXIT_OK
        out = capsys.readouterr().out
        # Round-3 fix (C-F2): the FULL stdout must be the JSON summary —
        # the dispatch logging guard routes every log line to stderr, so
        # `brutex verify-acceptance --json | jq .` works. (The shipped
        # round-2 test papered over the defect by slicing from '{'.)
        payload = json.loads(out)
        assert payload["schema"] == "brutex-acceptance-report-v1"
        assert payload["self_test"]["verdict"] == "TEETH_SKIPPED"

    def test_human_run_with_full_flag_warns_cost(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        lake = make_twin(tmp_path)
        code = main(
            [
                "verify-acceptance",
                "--lake-root",
                str(lake),
                "--checks",
                "unique",
                "--full",
                "--no-self-test",
            ]
        )
        assert code == EXIT_OK
        captured = capsys.readouterr()
        assert "--full lifts sampling" in captured.err
        assert "BruteX acceptance" in captured.out

    def test_human_table_is_first_thing_on_stdout(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Round-3 fix (C-F2): on an UNCONFIGURED process the human table was
        # buried under ~60 raw PrintLogger lines on stdout.
        lake = make_twin(tmp_path)
        capsys.readouterr()  # drain the twin-build setup logs (pre-CLI, in-process)
        code = main(
            ["verify-acceptance", "--lake-root", str(lake), "--checks", "unique", "--no-self-test"]
        )
        assert code == EXIT_OK
        out = capsys.readouterr().out
        assert out.startswith("BruteX acceptance")


class TestLoggingGuard:
    """Round-3 fix (C-F2): verify-acceptance rides the #1630 logging seam."""

    def test_guard_configures_before_the_handler(self) -> None:
        assert not structlog.is_configured()
        with mock.patch.object(cli, "cmd_verify_acceptance", return_value=0) as handler:
            rc = main(["verify-acceptance", "--repro", "unique"])
        assert rc == 0
        handler.assert_called_once()
        assert structlog.is_configured()

    def test_junk_log_level_fails_loud_before_the_handler(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(LOG_LEVEL_ENV, "DEBG")
        with mock.patch.object(cli, "cmd_verify_acceptance", return_value=0) as handler:
            rc = main(["verify-acceptance", "--repro", "unique"])
        assert rc == EXIT_USER_ERROR
        handler.assert_not_called()
        assert "BRUTEX_LOG_LEVEL" in capsys.readouterr().err


class TestReportWriteFailureAndSampleGuard:
    def test_negative_sample_exit_1(self, tmp_path: Path) -> None:
        # Round-3 NIT (C-F5): a negative N is a Python slice, not a sample.
        lake = make_twin(tmp_path)
        code = main(["verify-acceptance", "--lake-root", str(lake), "--sample", "-1"])
        assert code == EXIT_USER_ERROR

    def test_report_write_failure_is_loud_and_truthful(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Round-3 fix (C-F4): read-only/unwritable report destination →
        # exit 2, one loud stderr SUSPECT line, and the printed table's exit
        # line reads 2 — never a table claiming exit 0 on a 2-exit process.
        lake = make_twin(tmp_path)
        capsys.readouterr()  # drain the twin-build setup logs (pre-CLI, in-process)
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a dir", encoding="utf-8")
        code = main(
            [
                "verify-acceptance",
                "--lake-root",
                str(lake),
                "--checks",
                "unique",
                "--no-self-test",
                "--out",
                str(blocker / "sub"),
            ]
        )
        assert code == EXIT_INTERNAL_ERROR
        captured = capsys.readouterr()
        assert "report write failed" in captured.err
        assert "→ exit 2" in captured.out
        assert "→ exit 0" not in captured.out
