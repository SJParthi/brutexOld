"""Unit tests for :mod:`brutex.config.backup_env` (DEC-BACKUP-OFFBOX-001 §4).

The config leaf: every default, every override, the loud malformed-numeric
fallback, the ambiguous-password normalisation (F6), the derived timeout, the
prune-weekday range guard, the extra-excludes split, and the hypothesis
totality property (the resolver NEVER raises — check/status must resolve with
an arbitrarily broken backup env).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog.testing
from brutex.config import backup_env
from brutex.config.backup_env import (
    BACKUP_CLOCK_SKEW_TOLERANCE_S_ENV,
    BACKUP_DEADMAN_HOURS_ENV,
    BACKUP_DRILL_SAMPLE_ENV,
    BACKUP_EXTRA_EXCLUDES_ENV,
    BACKUP_HEALTHCHECK_URL_ENV,
    BACKUP_KEEP_DAILY_ENV,
    BACKUP_KEEP_MONTHLY_ENV,
    BACKUP_KEEP_WEEKLY_ENV,
    BACKUP_MAX_RUNTIME_HOURS_ENV,
    BACKUP_PASSWORD_COMMAND_ENV,
    BACKUP_PASSWORD_FILE_ENV,
    BACKUP_PRUNE_WEEKDAY_ENV,
    BACKUP_REALERT_HOURS_ENV,
    BACKUP_REPO_ENV,
    BACKUP_RESTIC_BIN_ENV,
    BACKUP_STATE_DIR_ENV,
    BACKUP_TIMEOUT_S_ENV,
    BACKUP_VERIFY_SUBSET_ENV,
    DEEP_CHECK_INTERVAL_HOURS,
    VERIFY_SUBSET_BUCKETS,
    BackupEnvConfig,
    resolve_backup_env,
)
from hypothesis import given
from hypothesis import strategies as st


class TestDefaults:
    def test_defaults_when_env_empty(self, tmp_path: Path) -> None:
        """Every §4 default resolves from an EMPTY env (home injected)."""
        cfg = resolve_backup_env({}, home=tmp_path)
        assert cfg.repo is None
        assert cfg.password_file is None
        assert cfg.password_command is None
        assert cfg.restic_bin == "restic"
        assert cfg.state_dir == tmp_path / ".brutex" / "backup"
        assert cfg.deadman_hours == 30.0
        assert cfg.realert_hours == 6.0
        assert cfg.clock_skew_tolerance_s == 300.0
        assert cfg.max_runtime_hours == 48.0
        assert cfg.timeout_s == 48.0 * 3600.0
        assert cfg.keep_daily == 14
        assert cfg.keep_weekly == 8
        assert cfg.keep_monthly == 12
        assert cfg.prune_weekday == 6
        assert cfg.verify_subset == "auto"
        assert cfg.healthcheck_url is None
        assert cfg.drill_sample == 16
        assert cfg.extra_excludes == ("logs", "dedup-cache")

    def test_constants(self) -> None:
        """The two deliberate non-env constants (§15 R1: fewest knobs)."""
        assert VERIFY_SUBSET_BUCKETS == 56
        assert DEEP_CHECK_INTERVAL_HOURS == 24.0

    def test_state_dir_default_under_brutex_home(self, tmp_path: Path) -> None:
        """Default state_dir = brutex_home(home)/backup — COMMON, never re-derived."""
        cfg = resolve_backup_env({}, home=tmp_path / "custom-home")
        assert cfg.state_dir == tmp_path / "custom-home" / ".brutex" / "backup"


class TestOverrides:
    @pytest.mark.parametrize(
        ("var", "value", "field", "expected"),
        [
            (BACKUP_REPO_ENV, "s3:host/bucket", "repo", "s3:host/bucket"),
            (BACKUP_PASSWORD_FILE_ENV, "/tmp/pw", "password_file", Path("/tmp/pw")),
            (BACKUP_PASSWORD_COMMAND_ENV, "security find", "password_command", "security find"),
            (BACKUP_RESTIC_BIN_ENV, "/opt/restic", "restic_bin", "/opt/restic"),
            (BACKUP_STATE_DIR_ENV, "/var/backup-state", "state_dir", Path("/var/backup-state")),
            (BACKUP_DEADMAN_HOURS_ENV, "12.5", "deadman_hours", 12.5),
            (BACKUP_REALERT_HOURS_ENV, "1.5", "realert_hours", 1.5),
            (BACKUP_CLOCK_SKEW_TOLERANCE_S_ENV, "60", "clock_skew_tolerance_s", 60.0),
            (BACKUP_MAX_RUNTIME_HOURS_ENV, "10", "max_runtime_hours", 10.0),
            (BACKUP_TIMEOUT_S_ENV, "123.0", "timeout_s", 123.0),
            (BACKUP_KEEP_DAILY_ENV, "7", "keep_daily", 7),
            (BACKUP_KEEP_WEEKLY_ENV, "4", "keep_weekly", 4),
            (BACKUP_KEEP_MONTHLY_ENV, "6", "keep_monthly", 6),
            (BACKUP_PRUNE_WEEKDAY_ENV, "0", "prune_weekday", 0),
            (BACKUP_VERIFY_SUBSET_ENV, "2%", "verify_subset", "2%"),
            (BACKUP_HEALTHCHECK_URL_ENV, "https://hc.io/x", "healthcheck_url", "https://hc.io/x"),
            (BACKUP_DRILL_SAMPLE_ENV, "5", "drill_sample", 5),
            (BACKUP_EXTRA_EXCLUDES_ENV, "a:b", "extra_excludes", ("a", "b")),
        ],
    )
    def test_each_var_override(
        self, tmp_path: Path, var: str, value: str, field: str, expected: object
    ) -> None:
        """Every one of the 18 env vars overrides its field."""
        cfg = resolve_backup_env({var: value}, home=tmp_path)
        assert getattr(cfg, field) == expected

    def test_blank_value_reads_as_unset(self, tmp_path: Path) -> None:
        """A whitespace-only value is UNSET (defaults win), not an empty token."""
        cfg = resolve_backup_env({BACKUP_REPO_ENV: "  ", BACKUP_RESTIC_BIN_ENV: ""}, home=tmp_path)
        assert cfg.repo is None
        assert cfg.restic_bin == "restic"

    def test_password_file_expands_user(self, tmp_path: Path) -> None:
        """A ``~`` password-file path is expanded (launchd env convenience)."""
        cfg = resolve_backup_env({BACKUP_PASSWORD_FILE_ENV: "~/pw.txt"}, home=tmp_path)
        assert cfg.password_file is not None
        assert "~" not in str(cfg.password_file)


class TestNumericFallback:
    def test_malformed_float_falls_back_loud(self, tmp_path: Path) -> None:
        """A malformed float → default + ONE backup.config.malformed_numeric WARN."""
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env({BACKUP_DEADMAN_HOURS_ENV: "not-a-number"}, home=tmp_path)
        assert cfg.deadman_hours == 30.0
        events = [entry for entry in logs if entry["event"] == "backup.config.malformed_numeric"]
        assert len(events) == 1
        assert events[0]["env_var"] == BACKUP_DEADMAN_HOURS_ENV

    def test_malformed_int_falls_back_loud(self, tmp_path: Path) -> None:
        """A malformed int → default + ONE WARN (the int twin)."""
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env({BACKUP_KEEP_DAILY_ENV: "fourteen"}, home=tmp_path)
        assert cfg.keep_daily == 14
        assert [entry["env_var"] for entry in logs] == [BACKUP_KEEP_DAILY_ENV]

    def test_timeout_derived_from_max_runtime(self, tmp_path: Path) -> None:
        """Unset timeout derives from max_runtime_hours × 3600 (one knob, two guards)."""
        cfg = resolve_backup_env({BACKUP_MAX_RUNTIME_HOURS_ENV: "2"}, home=tmp_path)
        assert cfg.timeout_s == 7200.0

    def test_timeout_env_override_wins(self, tmp_path: Path) -> None:
        """An explicit timeout wins over the derived value."""
        cfg = resolve_backup_env(
            {BACKUP_MAX_RUNTIME_HOURS_ENV: "2", BACKUP_TIMEOUT_S_ENV: "99"}, home=tmp_path
        )
        assert cfg.timeout_s == 99.0

    @pytest.mark.parametrize("bad", ["7", "-1"])
    def test_prune_weekday_out_of_range_default_plus_warn(self, tmp_path: Path, bad: str) -> None:
        """An out-of-range weekday (valid ints only reach the range check) → 6 + WARN."""
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env({BACKUP_PRUNE_WEEKDAY_ENV: bad}, home=tmp_path)
        assert cfg.prune_weekday == 6
        assert [entry["event"] for entry in logs] == ["backup.config.prune_weekday_out_of_range"]


class TestPasswordSources:
    def test_both_password_sources_set_yields_none_plus_warn(self, tmp_path: Path) -> None:
        """BOTH set is ambiguous (F6): both fields None + ONE WARN — run refuses later."""
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env(
                {BACKUP_PASSWORD_FILE_ENV: "/tmp/pw", BACKUP_PASSWORD_COMMAND_ENV: "cmd"},
                home=tmp_path,
            )
        assert cfg.password_file is None
        assert cfg.password_command is None
        events = [entry["event"] for entry in logs]
        assert events == ["backup.config.ambiguous_password_source"]

    def test_neither_password_source_is_representable(self, tmp_path: Path) -> None:
        """Neither set resolves cleanly to None/None (run/drill refuse at entry)."""
        cfg = resolve_backup_env({}, home=tmp_path)
        assert cfg.password_file is None
        assert cfg.password_command is None


class TestExtraExcludes:
    def test_extra_excludes_default_logs_dedupcache(self, tmp_path: Path) -> None:
        """Unset → the documented default logs:dedup-cache (§15 R6)."""
        cfg = resolve_backup_env({}, home=tmp_path)
        assert cfg.extra_excludes == ("logs", "dedup-cache")

    def test_extra_excludes_empty_string_clears(self, tmp_path: Path) -> None:
        """An explicit empty string CLEARS the default (operator wants logs backed up)."""
        cfg = resolve_backup_env({BACKUP_EXTRA_EXCLUDES_ENV: ""}, home=tmp_path)
        assert cfg.extra_excludes == ()

    def test_extra_excludes_multi_split(self, tmp_path: Path) -> None:
        """Multiple globs split on ':' with blanks dropped + whitespace stripped."""
        cfg = resolve_backup_env(
            {BACKUP_EXTRA_EXCLUDES_ENV: " logs : scratch :: *.bak "}, home=tmp_path
        )
        assert cfg.extra_excludes == ("logs", "scratch", "*.bak")


class TestVerifySubset:
    @pytest.mark.parametrize("value", ["auto", "none", "3/7"])
    def test_verify_subset_auto_none_passthrough(self, tmp_path: Path, value: str) -> None:
        """auto | none | verbatim passthrough all resolve unchanged."""
        cfg = resolve_backup_env({BACKUP_VERIFY_SUBSET_ENV: value}, home=tmp_path)
        assert cfg.verify_subset == value


class TestPathExpansionTotality:
    """Round-1 hostile-review BLOCKER pin: ``~<nonexistent-user>`` values must
    NOT crash the resolver (Path.expanduser raises RuntimeError for them) —
    the hourly dead-man check/status must always resolve (graft A)."""

    def test_unknown_tilde_user_password_file_keeps_raw_plus_warn(self, tmp_path: Path) -> None:
        raw = "~brutex-no-such-user-x9/pw.txt"
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env({BACKUP_PASSWORD_FILE_ENV: raw}, home=tmp_path)
        assert cfg.password_file == Path(raw)
        events = [entry for entry in logs if entry["event"] == "backup.config.malformed_path"]
        assert len(events) == 1
        assert events[0]["env_var"] == BACKUP_PASSWORD_FILE_ENV
        assert events[0]["value"] == raw

    def test_unknown_tilde_user_state_dir_keeps_raw_plus_warn(self, tmp_path: Path) -> None:
        raw = "~brutex-no-such-user-x9/state"
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env({BACKUP_STATE_DIR_ENV: raw}, home=tmp_path)
        assert cfg.state_dir == Path(raw)
        assert [entry["event"] for entry in logs] == ["backup.config.malformed_path"]

    def test_expandable_tilde_still_expands(self, tmp_path: Path) -> None:
        """The happy ``~/`` expansion is unchanged by the totality guard."""
        cfg = resolve_backup_env({BACKUP_STATE_DIR_ENV: "~/bstate"}, home=tmp_path)
        assert "~" not in str(cfg.state_dir)


class TestResolverTotality:
    @given(
        env=st.dictionaries(
            st.sampled_from(
                [
                    BACKUP_REPO_ENV,
                    BACKUP_PASSWORD_FILE_ENV,
                    BACKUP_PASSWORD_COMMAND_ENV,
                    BACKUP_RESTIC_BIN_ENV,
                    BACKUP_STATE_DIR_ENV,
                    BACKUP_DEADMAN_HOURS_ENV,
                    BACKUP_REALERT_HOURS_ENV,
                    BACKUP_CLOCK_SKEW_TOLERANCE_S_ENV,
                    BACKUP_MAX_RUNTIME_HOURS_ENV,
                    BACKUP_TIMEOUT_S_ENV,
                    BACKUP_KEEP_DAILY_ENV,
                    BACKUP_KEEP_WEEKLY_ENV,
                    BACKUP_KEEP_MONTHLY_ENV,
                    BACKUP_PRUNE_WEEKDAY_ENV,
                    BACKUP_VERIFY_SUBSET_ENV,
                    BACKUP_HEALTHCHECK_URL_ENV,
                    BACKUP_DRILL_SAMPLE_ENV,
                    BACKUP_EXTRA_EXCLUDES_ENV,
                ]
            ),
            st.text(alphabet=st.characters(codec="utf-8", exclude_characters="\x00"), max_size=40),
            max_size=18,
        )
    )
    @pytest.mark.property
    def test_resolver_never_raises_property(self, env: dict[str, str]) -> None:
        """TOTAL over arbitrary env maps — check/status must always resolve (graft A)."""
        cfg = resolve_backup_env(env, home=Path("/tmp/hyp-home"))
        assert isinstance(cfg, BackupEnvConfig)
        assert 0 <= cfg.prune_weekday <= 6


class TestOsEnvironDefault:
    def test_env_none_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """env=None → os.environ (the paths.py idiom)."""
        monkeypatch.setenv(BACKUP_REPO_ENV, "s3:live-env/bucket")
        assert resolve_backup_env().repo == "s3:live-env/bucket"

    def test_module_reexports(self) -> None:
        """The leaf's __all__ names all resolve (import-surface pin)."""
        for name in backup_env.__all__:
            assert hasattr(backup_env, name)
