"""CLI shim pins: registration, dispatch, flag validation, exit mapping.

DEC-OPERATOR-ACCEPTANCE-VERIFY-001 (MIGRATION.md #1756). The shim is a thin
``# pragma: no cover`` orchestration layer by house convention; these tests
drive it through ``main()`` (the CLI-harness discipline) so the parser wiring
+ user-error arms are genuinely executed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brutex.cli import EXIT_OK, EXIT_USER_ERROR, build_parser, main
from tests.fakes.acceptance_twin import make_twin


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
    def test_json_run_exit_0(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import json

        lake = make_twin(tmp_path)
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
        # structlog INFO lines may precede the JSON summary on stdout in an
        # unconfigured test process — parse from the object start.
        payload = json.loads(out[out.index("{\n") :])
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
