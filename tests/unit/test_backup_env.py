"""Unit tests for :mod:`brutex.config.backup_env` (DEC-BACKUP-OFFBOX-001 §4).

The config leaf: every default, every override, the loud malformed-numeric
fallback, the ambiguous-password normalisation (F6), the derived timeout, the
prune-weekday range guard, the retention keep guards
(DEC-BACKUP-RETENTION-KNOB-GUARD-001), the extra-excludes split, and the hypothesis
totality property (the resolver NEVER raises — check/status must resolve with
an arbitrarily broken backup env).
"""

from __future__ import annotations

import math
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

    @pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "Inf", "-inf", "+inf", "Infinity"])
    def test_deadman_hours_non_finite_falls_back_loud(self, tmp_path: Path, bad: str) -> None:
        """Round-8 MINOR pin: ``float()`` parses nan/inf cleanly, so the
        malformed-numeric fallback never fired — yet a non-finite threshold
        SILENTLY DISABLES the dead-man (``elapsed > nan`` is False for a
        year-stale backup → verdict FRESH, exit 0, plain success pings; the
        exact inversion of the "silence = alarm" law). Non-finite must fall
        back to the default LOUDLY like any other malformed value."""
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env({BACKUP_DEADMAN_HOURS_ENV: bad}, home=tmp_path)
        assert cfg.deadman_hours == 30.0
        events = [entry for entry in logs if entry["event"] == "backup.config.malformed_numeric"]
        assert [entry["env_var"] for entry in events] == [BACKUP_DEADMAN_HOURS_ENV]

    @pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
    def test_max_runtime_non_finite_falls_back_and_timeout_rederives(
        self, tmp_path: Path, bad: str
    ) -> None:
        """Round-8 MINOR pin: ``max_runtime_hours=inf`` (a plausible runbook
        step-6 "no limit" seed raise) would mute the HUNG lane forever AND
        derive ``timeout_s=inf`` — every restic deadline unbounded, and
        ``min(nan, 120.0)`` even defeats the deep-check cap. Both knobs must
        resolve back to finite defaults."""
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env({BACKUP_MAX_RUNTIME_HOURS_ENV: bad}, home=tmp_path)
        assert cfg.max_runtime_hours == 48.0
        assert cfg.timeout_s == 48.0 * 3600.0  # derived from the GUARDED value
        events = [entry for entry in logs if entry["event"] == "backup.config.malformed_numeric"]
        assert [entry["env_var"] for entry in events] == [BACKUP_MAX_RUNTIME_HOURS_ENV]

    @pytest.mark.parametrize(
        ("env_var", "attr", "default"),
        [
            (BACKUP_REALERT_HOURS_ENV, "realert_hours", 6.0),
            (BACKUP_TIMEOUT_S_ENV, "timeout_s", 48.0 * 3600.0),
            (BACKUP_CLOCK_SKEW_TOLERANCE_S_ENV, "clock_skew_tolerance_s", 300.0),
        ],
    )
    def test_other_float_knobs_reject_non_finite(
        self, tmp_path: Path, env_var: str, attr: str, default: float
    ) -> None:
        """Every float knob rejects non-finite: nan realert mutes every
        re-alert after the first; nan skew tolerance mutes CLOCK_SKEW."""
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env({env_var: "nan"}, home=tmp_path)
        assert getattr(cfg, attr) == default
        events = [entry for entry in logs if entry["event"] == "backup.config.malformed_numeric"]
        assert [entry["env_var"] for entry in events] == [env_var]

    @pytest.mark.parametrize(
        ("env_var", "attr", "default"),
        [
            (BACKUP_DEADMAN_HOURS_ENV, "deadman_hours", 30.0),
            (BACKUP_REALERT_HOURS_ENV, "realert_hours", 6.0),
            (BACKUP_MAX_RUNTIME_HOURS_ENV, "max_runtime_hours", 48.0),
            (BACKUP_TIMEOUT_S_ENV, "timeout_s", 48.0 * 3600.0),
        ],
    )
    @pytest.mark.parametrize("bad", ["0", "-1.5"])
    def test_hour_deadline_knobs_reject_non_positive(
        self, tmp_path: Path, env_var: str, attr: str, default: float, bad: str
    ) -> None:
        """The hour/deadline knobs also reject ≤0 (round-8: a zero/negative
        threshold or subprocess deadline is nonsensical — same loud-fallback
        law as prune_weekday / drill_sample)."""
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env({env_var: bad}, home=tmp_path)
        assert getattr(cfg, attr) == default
        events = [entry for entry in logs if entry["event"] == "backup.config.malformed_numeric"]
        assert [entry["env_var"] for entry in events] == [env_var]

    def test_clock_skew_zero_is_respected_no_warn(self, tmp_path: Path) -> None:
        """Zero skew tolerance is a MEANINGFUL strict setting — not gated."""
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env({BACKUP_CLOCK_SKEW_TOLERANCE_S_ENV: "0"}, home=tmp_path)
        assert cfg.clock_skew_tolerance_s == 0.0
        assert logs == []

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

    @pytest.mark.parametrize("bad", ["0", "-16"])
    def test_drill_sample_below_one_default_plus_warn(self, tmp_path: Path, bad: str) -> None:
        """Round-6 NIT pin: a nonsensical (≤0) drill sample parses as a VALID
        int — the malformed-numeric loud-fallback never fires — yet it would
        silently degrade the monthly drill to canary-only (the stratified
        byte-verification loop runs zero times) while still exiting 0 with
        ``backup.drill.ok``. Same law as the prune_weekday guard above:
        fall back to the default LOUDLY, never a quiet nonsense value."""
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env({BACKUP_DRILL_SAMPLE_ENV: bad}, home=tmp_path)
        assert cfg.drill_sample == 16
        assert [entry["event"] for entry in logs] == ["backup.config.drill_sample_out_of_range"]

    def test_drill_sample_of_one_is_respected_no_warn(self, tmp_path: Path) -> None:
        """The floor is <1 — a deliberate tiny-but-real sample stays as-is."""
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env({BACKUP_DRILL_SAMPLE_ENV: "1"}, home=tmp_path)
        assert cfg.drill_sample == 1
        assert logs == []


class TestRetentionKeepGuards:
    """Round-10 MINOR regression pins (DEC-BACKUP-RETENTION-KNOB-GUARD-001):
    a 0/negative ``BRUTEX_BACKUP_KEEP_*`` parses as a VALID int — the
    malformed-numeric loud-fallback never fires — yet it flows verbatim into
    ``restic forget --keep-daily/--keep-weekly/--keep-monthly``, where restic
    treats 0 as "keep NONE for this bucket": retention silently DISABLED with
    rc=0 and no WARN, on the one data-destroying verb (``forget --prune``).
    Same law as the prune_weekday / drill_sample guards: <1 falls back to the
    default LOUDLY, naming the env var."""

    @pytest.mark.parametrize("bad", ["0", "-14"])
    def test_keep_daily_below_one_default_plus_warn(self, tmp_path: Path, bad: str) -> None:
        """keep_daily 0/negative → default 14 + ONE keep_daily_out_of_range WARN."""
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env({BACKUP_KEEP_DAILY_ENV: bad}, home=tmp_path)
        assert cfg.keep_daily == 14
        assert [entry["event"] for entry in logs] == ["backup.config.keep_daily_out_of_range"]
        assert logs[0]["env_var"] == BACKUP_KEEP_DAILY_ENV

    @pytest.mark.parametrize("bad", ["0", "-8"])
    def test_keep_weekly_below_one_default_plus_warn(self, tmp_path: Path, bad: str) -> None:
        """keep_weekly 0/negative → default 8 + ONE keep_weekly_out_of_range WARN."""
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env({BACKUP_KEEP_WEEKLY_ENV: bad}, home=tmp_path)
        assert cfg.keep_weekly == 8
        assert [entry["event"] for entry in logs] == ["backup.config.keep_weekly_out_of_range"]
        assert logs[0]["env_var"] == BACKUP_KEEP_WEEKLY_ENV

    @pytest.mark.parametrize("bad", ["0", "-12"])
    def test_keep_monthly_below_one_default_plus_warn(self, tmp_path: Path, bad: str) -> None:
        """keep_monthly 0/negative → default 12 + ONE keep_monthly_out_of_range WARN."""
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env({BACKUP_KEEP_MONTHLY_ENV: bad}, home=tmp_path)
        assert cfg.keep_monthly == 12
        assert [entry["event"] for entry in logs] == ["backup.config.keep_monthly_out_of_range"]
        assert logs[0]["env_var"] == BACKUP_KEEP_MONTHLY_ENV

    def test_keep_of_one_is_respected_no_warn(self, tmp_path: Path) -> None:
        """The floor is <1 — a deliberate keep=1 on every knob stays as-is,
        and valid overrides (already pinned in TestOverrides) never warn."""
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env(
                {
                    BACKUP_KEEP_DAILY_ENV: "1",
                    BACKUP_KEEP_WEEKLY_ENV: "1",
                    BACKUP_KEEP_MONTHLY_ENV: "1",
                },
                home=tmp_path,
            )
        assert (cfg.keep_daily, cfg.keep_weekly, cfg.keep_monthly) == (1, 1, 1)
        assert logs == []


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
    @pytest.mark.parametrize(
        "value",
        [
            "auto",
            "none",
            "3/7",
            "1/56",
            "01/56",  # Go ParseUint accepts a leading zero (probed, restic 0.16.4)
            "56/56",
            "1/256",  # the checkFlags t ≤ 256 boundary, inclusive
            "2%",
            "2.5%",
            ".5%",
            "2.%",
            "100%",  # the 0 < x ≤ 100 boundary, inclusive
            "10G",
            "512M",
            "10k",
            "1b",
            "8388607t",  # 2^63 − 2^40 bytes — the largest restic-valid t size (probed)
        ],
    )
    def test_verify_subset_valid_shapes_pass_through(self, tmp_path: Path, value: str) -> None:
        """auto | none | every restic-valid passthrough shape resolves unchanged, no WARN."""
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env({BACKUP_VERIFY_SUBSET_ENV: value}, home=tmp_path)
        assert cfg.verify_subset == value
        assert logs == []

    @pytest.mark.parametrize(
        "value",
        [
            "garbage",  # the round-7 empirical repro: rc=1 flag error ≡ corruption rc
            "0/56",  # the round-7 empirical repro: n must be positive
            "00/56",  # ParseUint("00") == 0 → restic-refused (probed)
            "5/3",  # n ≤ t violated
            "1/257",  # t ≤ 256 violated
            "1/2/3",
            "0%",
            "150%",  # x ≤ 100 violated
            "1000",  # a BARE integer is restic-INVALID (parses as a 1-element n/t)
            "1.5G",  # the size prefix is Go ParseInt — no float sizes
            "0G",  # size must be positive
            # Round-9 MINOR: ui.ParseBytes overflows Go int64 with the SAME
            # rc=1 flag-invalid as genuine corruption (probed on 0.16.4 —
            # 8388608t is exactly 2^63 bytes):
            "8388608t",
            "9999999999t",
            "99999999999999999999b",
            "10GB",  # ONE suffix char only
            "2 %",  # interior whitespace (only outer whitespace is stripped)
            # restic's Go ParseFloat would accept these exotic spellings; the
            # validator is deliberately CONSERVATIVE (plain decimals only) —
            # a false fallback costs one WARN + the safe auto default, a
            # false pass costs a fake repo-corrupt CRITICAL:
            "+2%",
            "2e1%",
        ],
    )
    def test_verify_subset_invalid_falls_back_auto_loud(self, tmp_path: Path, value: str) -> None:
        """Round-7 MINOR pin: an invalid ``--read-data-subset`` shape must NEVER
        reach restic — restic refuses it rc=1 on FLAG VALIDATION (reading zero
        repo data), indistinguishable in rc from genuine corruption, so an
        unvalidated typo paged repo-corrupt (exit 6 + the restore-from-older /
        re-seed CRITICAL) every night with success never stamped (a dead-man
        OVERDUE cascade). Same law as prune_weekday/drill_sample: fall back to
        ``auto`` LOUDLY (empirical basis: restic 0.16.4 checkFlags probes)."""
        with structlog.testing.capture_logs() as logs:
            cfg = resolve_backup_env({BACKUP_VERIFY_SUBSET_ENV: value}, home=tmp_path)
        assert cfg.verify_subset == "auto"
        events = [
            entry for entry in logs if entry["event"] == "backup.config.verify_subset_invalid"
        ]
        assert len(events) == 1
        assert events[0]["value"] == value
        assert events[0]["default"] == "auto"


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
        """TOTAL over arbitrary env maps — check/status must always resolve (graft A).

        Round-8 invariant: whatever the env says (incl. ``nan``/``inf``
        strings, which ``float()`` parses cleanly), the resolved float knobs
        are ALWAYS finite, and the hour/deadline knobs ALWAYS positive — the
        production-domain guarantee that justifies the finite-only strategies
        of ``tests/property/test_backup_deadman_property.py``."""
        cfg = resolve_backup_env(env, home=Path("/tmp/hyp-home"))
        assert isinstance(cfg, BackupEnvConfig)
        assert 0 <= cfg.prune_weekday <= 6
        assert math.isfinite(cfg.clock_skew_tolerance_s)
        for knob in (cfg.deadman_hours, cfg.realert_hours, cfg.max_runtime_hours, cfg.timeout_s):
            assert math.isfinite(knob)
            assert knob > 0.0


class TestOsEnvironDefault:
    def test_env_none_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """env=None → os.environ (the paths.py idiom)."""
        monkeypatch.setenv(BACKUP_REPO_ENV, "s3:live-env/bucket")
        assert resolve_backup_env().repo == "s3:live-env/bucket"

    def test_module_reexports(self) -> None:
        """The leaf's __all__ names all resolve (import-surface pin)."""
        for name in backup_env.__all__:
            assert hasattr(backup_env, name)
