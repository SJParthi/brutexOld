"""The CLI temporal-gate resolution (DEC-SWEEP-TEMPORAL-ROBUSTNESS-001).

``resolve_temporal_config``: flag > ``$BRUTEX_TEMPORAL`` (case-insensitive) >
the shipped ``observe`` default; junk fails LOUD (``CliUserError`` → one clean
``[brutex]`` line + exit 1) BEFORE any lake I/O — never a silent fallback; the
``off`` arm refuses stray threshold flags (a silently-ignored knob is a config
lie). Plus the parser wiring for the three flags.
"""

from __future__ import annotations

import pytest
from brutex.cli import (
    TEMPORAL_MODE_ENV,
    CliUserError,
    build_parser,
    main,
    resolve_temporal_config,
)
from brutex.ranking.temporal import MODE_GATE, MODE_OBSERVE, TemporalRobustnessConfig


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TEMPORAL_MODE_ENV, raising=False)


class TestResolveTemporalConfig:
    def test_default_is_observe(self) -> None:
        cfg = resolve_temporal_config(None, None, None)
        assert isinstance(cfg, TemporalRobustnessConfig)
        assert cfg.mode == MODE_OBSERVE
        assert cfg.min_profitable_years_fraction == 0.5
        assert cfg.max_single_year_pnl_share == 0.6

    def test_env_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TEMPORAL_MODE_ENV, "gate")
        cfg = resolve_temporal_config(None, None, None)
        assert cfg is not None and cfg.mode == MODE_GATE

    def test_env_is_case_insensitive_and_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TEMPORAL_MODE_ENV, "  GATE ")
        cfg = resolve_temporal_config(None, None, None)
        assert cfg is not None and cfg.mode == MODE_GATE

    def test_env_off_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TEMPORAL_MODE_ENV, "off")
        assert resolve_temporal_config(None, None, None) is None

    def test_blank_env_falls_back_to_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TEMPORAL_MODE_ENV, "   ")
        cfg = resolve_temporal_config(None, None, None)
        assert cfg is not None and cfg.mode == MODE_OBSERVE

    def test_flag_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TEMPORAL_MODE_ENV, "gate")
        assert resolve_temporal_config("off", None, None) is None

    def test_junk_env_fails_loud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TEMPORAL_MODE_ENV, "yes-please")
        with pytest.raises(CliUserError, match="invalid BRUTEX_TEMPORAL"):
            resolve_temporal_config(None, None, None)

    def test_off_with_threshold_flags_refused(self) -> None:
        with pytest.raises(CliUserError, match="require --temporal-mode"):
            resolve_temporal_config("off", 0.7, None)
        with pytest.raises(CliUserError, match="require --temporal-mode"):
            resolve_temporal_config("off", None, 0.5)

    def test_threshold_overrides_applied(self) -> None:
        cfg = resolve_temporal_config("gate", 0.75, 0.5)
        assert cfg is not None
        assert cfg.min_profitable_years_fraction == 0.75
        assert cfg.max_single_year_pnl_share == 0.5

    def test_out_of_range_threshold_fails_loud(self) -> None:
        with pytest.raises(CliUserError, match="invalid temporal gate config"):
            resolve_temporal_config("gate", 1.5, None)
        with pytest.raises(CliUserError, match="invalid temporal gate config"):
            resolve_temporal_config("observe", None, 0.0)


class TestParserWiring:
    def test_sweep_all_flags_parse(self) -> None:
        args = build_parser().parse_args(
            [
                "sweep-all",
                "--start",
                "2024-01-01",
                "--end",
                "2024-12-31",
                "--temporal-mode",
                "gate",
                "--temporal-min-profitable-frac",
                "0.75",
                "--temporal-max-year-share",
                "0.5",
            ]
        )
        assert args.temporal_mode == "gate"
        assert args.temporal_min_profitable_frac == 0.75
        assert args.temporal_max_year_share == 0.5

    def test_defaults_are_none(self) -> None:
        args = build_parser().parse_args(
            ["sweep-all", "--start", "2024-01-01", "--end", "2024-12-31"]
        )
        assert args.temporal_mode is None
        assert args.temporal_min_profitable_frac is None
        assert args.temporal_max_year_share is None

    def test_unknown_mode_is_an_argparse_error(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                [
                    "sweep-all",
                    "--start",
                    "2024-01-01",
                    "--end",
                    "2024-12-31",
                    "--temporal-mode",
                    "audit",
                ]
            )

    def test_junk_env_exits_one_clean_line_pre_sweep(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: object,
    ) -> None:
        """End-to-end: a junk $BRUTEX_TEMPORAL kills the sweep-all launch with
        the one-line [brutex] error + exit 1 BEFORE any lake I/O."""
        monkeypatch.setenv(TEMPORAL_MODE_ENV, "junk")
        rc = main(
            [
                "sweep-all",
                "--start",
                "2024-01-01",
                "--end",
                "2024-12-31",
                "--lake-root",
                str(tmp_path),
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "[brutex] invalid BRUTEX_TEMPORAL" in err
