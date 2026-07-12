"""Unit tests — :mod:`brutex.crossverify.config` (fail-loud env resolution)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from brutex.crossverify.config import (
    DEFAULT_BUCKET,
    DEFAULT_CATCHUP_DAYS,
    DEFAULT_DEADLINE_S,
    DEFAULT_LOCK_RETRY_S,
    DEFAULT_LOCK_WAIT_S,
    DEFAULT_PREFIX,
    DEFAULT_PULL_LOOKBACK_DAYS,
    DEFAULT_REGION,
    DEFAULT_SETTLE_RETRY_DELAY_S,
    DEFAULT_SETTLE_RETRY_ROUNDS,
    ENV_BUCKET,
    ENV_CATCHUP_DAYS,
    ENV_DEADLINE_S,
    ENV_KILL_SWITCH,
    ENV_LOCK_RETRY_S,
    ENV_LOCK_WAIT_S,
    ENV_PREFIX,
    ENV_PUBLISH,
    ENV_PULL_LOOKBACK_DAYS,
    ENV_REGION,
    ENV_SETTLE_RETRY_DELAY_S,
    ENV_SETTLE_RETRY_ROUNDS,
    resolve_crossverify_config,
)

_IST = ZoneInfo("Asia/Kolkata")
_LAKE = Path("/tmp/lake")
_DAY = date(2026, 7, 10)


class TestDefaults:
    def test_all_defaults(self) -> None:
        cfg = resolve_crossverify_config(lake_root=_LAKE, env={}, day=_DAY)
        assert cfg.lake_root == _LAKE
        assert cfg.day == _DAY
        assert cfg.bucket == DEFAULT_BUCKET == "tv-prod-cold"
        assert cfg.key_prefix == DEFAULT_PREFIX == "crossverify/groww"
        assert cfg.region == DEFAULT_REGION == "ap-south-1"
        assert cfg.enabled is True
        assert cfg.publish is True
        assert cfg.pull_lookback_days == DEFAULT_PULL_LOOKBACK_DAYS == 7
        assert cfg.lock_wait_s == DEFAULT_LOCK_WAIT_S == 2700.0
        assert cfg.lock_retry_s == DEFAULT_LOCK_RETRY_S == 60.0
        assert cfg.deadline_s == DEFAULT_DEADLINE_S == 14400.0
        assert cfg.settle_retry_rounds == DEFAULT_SETTLE_RETRY_ROUNDS == 1
        assert cfg.settle_retry_delay_s == DEFAULT_SETTLE_RETRY_DELAY_S == 600.0
        assert cfg.catchup_days == DEFAULT_CATCHUP_DAYS == 5
        assert cfg.dry_run is False
        assert cfg.skip_pull is False

    def test_blank_values_read_as_unset(self) -> None:
        env = {ENV_BUCKET: "  ", ENV_PULL_LOOKBACK_DAYS: "", ENV_LOCK_WAIT_S: " "}
        cfg = resolve_crossverify_config(lake_root=_LAKE, env=env, day=_DAY)
        assert cfg.bucket == DEFAULT_BUCKET
        assert cfg.pull_lookback_days == DEFAULT_PULL_LOOKBACK_DAYS
        assert cfg.lock_wait_s == DEFAULT_LOCK_WAIT_S

    def test_env_none_reads_os_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_BUCKET, "other-bucket")
        cfg = resolve_crossverify_config(lake_root=_LAKE, env=None, day=_DAY)
        assert cfg.bucket == "other-bucket"


class TestOverrides:
    def test_every_var_overrides(self) -> None:
        env = {
            ENV_KILL_SWITCH: "on",
            ENV_PUBLISH: "yes",
            ENV_BUCKET: "b",
            ENV_PREFIX: "p/q",
            ENV_REGION: "us-east-1",
            ENV_PULL_LOOKBACK_DAYS: "3",
            ENV_LOCK_WAIT_S: "10.5",
            ENV_LOCK_RETRY_S: "0.5",
            ENV_DEADLINE_S: "60",
            ENV_SETTLE_RETRY_ROUNDS: "2",
            ENV_SETTLE_RETRY_DELAY_S: "1.5",
            ENV_CATCHUP_DAYS: "9",
        }
        cfg = resolve_crossverify_config(lake_root=_LAKE, env=env, day=_DAY)
        assert (cfg.enabled, cfg.publish) == (True, True)
        assert (cfg.bucket, cfg.key_prefix, cfg.region) == ("b", "p/q", "us-east-1")
        assert cfg.pull_lookback_days == 3
        assert (cfg.lock_wait_s, cfg.lock_retry_s, cfg.deadline_s) == (10.5, 0.5, 60.0)
        assert (cfg.settle_retry_rounds, cfg.settle_retry_delay_s) == (2, 1.5)
        assert cfg.catchup_days == 9

    def test_kill_switch_off(self) -> None:
        cfg = resolve_crossverify_config(lake_root=_LAKE, env={ENV_KILL_SWITCH: "OFF"}, day=_DAY)
        assert cfg.enabled is False

    def test_publish_env_off(self) -> None:
        cfg = resolve_crossverify_config(lake_root=_LAKE, env={ENV_PUBLISH: "0"}, day=_DAY)
        assert cfg.publish is False

    def test_no_publish_flag_forces_off_over_env_on(self) -> None:
        cfg = resolve_crossverify_config(
            lake_root=_LAKE, env={ENV_PUBLISH: "on"}, day=_DAY, no_publish=True
        )
        assert cfg.publish is False

    def test_dry_run_and_skip_pull_flags(self) -> None:
        cfg = resolve_crossverify_config(
            lake_root=_LAKE, env={}, day=_DAY, dry_run=True, skip_pull=True
        )
        assert cfg.dry_run is True
        assert cfg.skip_pull is True


class TestJunkFailsLoud:
    @pytest.mark.parametrize("var", [ENV_KILL_SWITCH, ENV_PUBLISH])
    def test_junk_switch(self, var: str) -> None:
        with pytest.raises(ValueError, match="not a recognised switch value"):
            resolve_crossverify_config(lake_root=_LAKE, env={var: "maybe"}, day=_DAY)

    @pytest.mark.parametrize(
        "var", [ENV_PULL_LOOKBACK_DAYS, ENV_SETTLE_RETRY_ROUNDS, ENV_CATCHUP_DAYS]
    )
    def test_junk_int(self, var: str) -> None:
        with pytest.raises(ValueError, match="not an integer"):
            resolve_crossverify_config(lake_root=_LAKE, env={var: "x"}, day=_DAY)

    @pytest.mark.parametrize(
        "var", [ENV_PULL_LOOKBACK_DAYS, ENV_SETTLE_RETRY_ROUNDS, ENV_CATCHUP_DAYS]
    )
    def test_negative_int(self, var: str) -> None:
        with pytest.raises(ValueError, match="must be >="):
            resolve_crossverify_config(lake_root=_LAKE, env={var: "-1"}, day=_DAY)

    @pytest.mark.parametrize(
        "var",
        [ENV_LOCK_WAIT_S, ENV_LOCK_RETRY_S, ENV_DEADLINE_S, ENV_SETTLE_RETRY_DELAY_S],
    )
    def test_junk_float(self, var: str) -> None:
        with pytest.raises(ValueError, match="not a number"):
            resolve_crossverify_config(lake_root=_LAKE, env={var: "soon"}, day=_DAY)

    @pytest.mark.parametrize("bad", ["nan", "inf", "-1"])
    def test_non_finite_or_below_min_float(self, bad: str) -> None:
        with pytest.raises(ValueError, match="finite number"):
            resolve_crossverify_config(lake_root=_LAKE, env={ENV_LOCK_WAIT_S: bad}, day=_DAY)

    @pytest.mark.parametrize("bad", ["/lead", "trail/"])
    def test_slash_edged_string(self, bad: str) -> None:
        with pytest.raises(ValueError, match="must not start or end with"):
            resolve_crossverify_config(lake_root=_LAKE, env={ENV_PREFIX: bad}, day=_DAY)


class TestDayResolution:
    def test_explicit_day_wins(self) -> None:
        cfg = resolve_crossverify_config(lake_root=_LAKE, env={}, day=date(2026, 1, 2))
        assert cfg.day == date(2026, 1, 2)

    def test_injected_clock_resolves_ist_date(self) -> None:
        # 2026-07-10 20:00 UTC is 2026-07-11 01:30 IST — the IST date wins.
        clock = lambda: datetime(2026, 7, 10, 20, 0, tzinfo=UTC)  # noqa: E731
        cfg = resolve_crossverify_config(lake_root=_LAKE, env={}, day=None, now=clock)
        assert cfg.day == date(2026, 7, 11)

    def test_default_clock_is_today_ist(self) -> None:
        before = datetime.now(tz=_IST).date()
        cfg = resolve_crossverify_config(lake_root=_LAKE, env={}, day=None, now=None)
        after = datetime.now(tz=_IST).date()
        assert cfg.day in {before, after}
