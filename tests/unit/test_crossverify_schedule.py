# pyright: reportPrivateUsage=none
"""Cross-verify launchd scheduling + trading-day-aware dead-man.

DEC-CROSSVERIFY-PRODUCER-001 §8: ``install_crossverify`` (--at REQUIRED —
probe-decided, NO default fire time ships; delegation to the ONE nightly
write/load/verify law with the ``crossverify-launchd`` log stem + the
mandatory ``launchctl list`` verification via the injected runner),
``uninstall_crossverify``, ``newest_journal_age`` (strict journal-name
grammar), ``seconds_since_trading_fire`` (the export-day walk that keeps
Monday mornings and the day after Diwali from false-MISSing),
``deadman_verdict`` (every arm), and ``status`` (honest lines even when
nothing ran).
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

import pytest
from brutex.crossverify.schedule import (
    AT_REQUIRED_MESSAGE,
    CROSSVERIFY_CRON_GUIDANCE,
    CROSSVERIFY_LABEL,
    CROSSVERIFY_LOG_STEM,
    DEADMAN_GRACE_SECONDS,
    _default_is_export_day,
    deadman_verdict,
    install_crossverify,
    newest_journal_age,
    resolve_program_arguments,
    seconds_since_trading_fire,
    status,
    uninstall_crossverify,
)


def _ok_runner(calls: list[list[str]]) -> object:
    def runner(argv: object) -> tuple[int, str, str]:
        calls.append(list(argv))  # type: ignore[call-overload]
        return 0, "", ""

    return runner


def _weekday_only(d: date) -> bool:
    return d.weekday() < 5


# ── install / uninstall ──────────────────────────────────────────────────


class TestInstall:
    def test_at_none_refuses_with_the_probe_message(self, tmp_path: Path) -> None:
        outcome = install_crossverify(
            at=None, plist_dir=tmp_path, runner=lambda argv: (0, "", ""), lake_root=None
        )
        assert not outcome.ok
        assert outcome.detail == AT_REQUIRED_MESSAGE
        assert "settle probe" in outcome.detail

    def test_at_blank_refuses_identically(self, tmp_path: Path) -> None:
        outcome = install_crossverify(
            at="   ", plist_dir=tmp_path, runner=lambda argv: (0, "", ""), lake_root=None
        )
        assert not outcome.ok
        assert outcome.detail == AT_REQUIRED_MESSAGE

    def test_junk_at_refuses_with_the_parse_message(self, tmp_path: Path) -> None:
        outcome = install_crossverify(
            at="25:99", plist_dir=tmp_path, runner=lambda argv: (0, "", ""), lake_root=None
        )
        assert not outcome.ok
        assert "--at must be HH:MM" in outcome.detail

    def test_install_renders_label_argv_stem_and_verifies(self, tmp_path: Path) -> None:
        calls: list[list[str]] = []
        outcome = install_crossverify(
            at="16:15",
            plist_dir=tmp_path / "agents",
            runner=_ok_runner(calls),  # type: ignore[arg-type]
            lake_root=Path("/data/lake"),
            program_arguments=["/usr/local/bin/brutex", "crossverify", "run"],
            log_dir=tmp_path / "logs",
        )
        assert outcome.ok
        assert "com.brutex.crossverify listed" in outcome.detail
        assert "Next step: brutex crossverify run --dry-run" in outcome.detail
        payload = (tmp_path / "agents" / f"{CROSSVERIFY_LABEL}.plist").read_text(encoding="utf-8")
        assert "<string>com.brutex.crossverify</string>" in payload
        assert "<string>crossverify</string>" in payload
        assert f"{CROSSVERIFY_LOG_STEM}.out.log" in payload
        assert "<key>Hour</key><integer>16</integer><key>Minute</key><integer>15</integer>" in (
            payload.replace("\n    ", "")
        )
        assert "<key>BRUTEX_LAKE_ROOT</key><string>/data/lake</string>" in payload
        # The MANDATORY launchctl-list verification ran (the TickVault lesson).
        assert ["launchctl", "list", CROSSVERIFY_LABEL] in calls

    def test_install_verify_failure_is_a_failure(self, tmp_path: Path) -> None:
        def runner(argv: object) -> tuple[int, str, str]:
            argv_list = list(argv)  # type: ignore[call-overload]
            if argv_list[:2] == ["launchctl", "list"]:
                return 1, "", "not listed"
            return 0, "", ""

        outcome = install_crossverify(
            at="16:15",
            plist_dir=tmp_path,
            runner=runner,
            lake_root=None,
            program_arguments=["/bin/brutex", "crossverify", "run"],
            log_dir=tmp_path / "logs",
        )
        assert not outcome.ok
        assert "does NOT list" in outcome.detail

    def test_default_program_arguments_and_log_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The None-arg defaults resolve the argv + ~/.brutex/logs home."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        calls: list[list[str]] = []
        outcome = install_crossverify(
            at="16:15",
            plist_dir=tmp_path / "agents",
            runner=_ok_runner(calls),  # type: ignore[arg-type]
            lake_root=None,
        )
        assert outcome.ok
        payload = (tmp_path / "agents" / f"{CROSSVERIFY_LABEL}.plist").read_text(encoding="utf-8")
        assert "<string>crossverify</string>" in payload
        assert "<string>run</string>" in payload
        assert str(tmp_path / "home" / ".brutex" / "logs") in payload
        # No lake_root → no EnvironmentVariables dict at all.
        assert "EnvironmentVariables" not in payload

    def test_uninstall_delegates_and_verifies_unlisted(self, tmp_path: Path) -> None:
        plist = tmp_path / f"{CROSSVERIFY_LABEL}.plist"
        plist.write_text("x", encoding="utf-8")

        def runner(argv: object) -> tuple[int, str, str]:
            argv_list = list(argv)  # type: ignore[call-overload]
            return (1, "", "") if argv_list[:2] == ["launchctl", "list"] else (0, "", "")

        outcome = uninstall_crossverify(plist_dir=tmp_path, runner=runner)
        assert outcome.ok
        assert not plist.exists()


def test_resolve_program_arguments_which_hit_and_fallback() -> None:
    assert resolve_program_arguments(lambda _n: "/opt/bin/brutex") == [
        "/opt/bin/brutex",
        "crossverify",
        "run",
    ]
    fallback = resolve_program_arguments(lambda _n: None)
    assert fallback[1:] == ["-m", "brutex", "crossverify", "run"]


def test_cron_guidance_names_the_command() -> None:
    assert "crossverify run" in CROSSVERIFY_CRON_GUIDANCE


# ── newest_journal_age ───────────────────────────────────────────────────


class TestNewestJournalAge:
    def test_none_when_no_journal_dir_or_files(self, tmp_path: Path) -> None:
        assert newest_journal_age(tmp_path, now=1000.0) is None
        (tmp_path / ".crossverify").mkdir()
        assert newest_journal_age(tmp_path, now=1000.0) is None

    def test_strays_and_staging_are_ignored(self, tmp_path: Path) -> None:
        cv = tmp_path / ".crossverify"
        (cv / "staging" / "2026-07-10").mkdir(parents=True)
        (cv / "notes.txt").write_text("x", encoding="utf-8")
        (cv / "2026-7-1.json").write_text("{}", encoding="utf-8")  # non-canonical stem
        assert newest_journal_age(tmp_path, now=1000.0) is None

    def test_picks_newest_and_clamps_negative_age(self, tmp_path: Path) -> None:
        cv = tmp_path / ".crossverify"
        cv.mkdir()
        old = cv / "2026-07-09.json"
        new = cv / "2026-07-10.json"
        old.write_text("{}", encoding="utf-8")
        new.write_text("{}", encoding="utf-8")
        os.utime(old, (1000.0, 1000.0))
        os.utime(new, (2000.0, 2000.0))
        entry = newest_journal_age(tmp_path, now=2500.0)
        assert entry == (500.0, "2026-07-10.json")
        clamped = newest_journal_age(tmp_path, now=1500.0)  # clock skew — never negative
        assert clamped == (0.0, "2026-07-10.json")

    def test_now_fn_used_when_now_unset(self, tmp_path: Path) -> None:
        cv = tmp_path / ".crossverify"
        cv.mkdir()
        journal = cv / "2026-07-10.json"
        journal.write_text("{}", encoding="utf-8")
        os.utime(journal, (100.0, 100.0))
        entry = newest_journal_age(tmp_path, now_fn=lambda: 400.0)
        assert entry == (300.0, "2026-07-10.json")


# ── seconds_since_trading_fire ───────────────────────────────────────────


class TestSecondsSinceTradingFire:
    def test_today_fire_passed_on_trading_day(self) -> None:
        now = datetime(2026, 7, 10, 18, 0)  # Friday
        got = seconds_since_trading_fire(
            hour=16, minute=15, now_local=now, is_export_day=_weekday_only
        )
        assert got == 105 * 60.0

    def test_before_todays_fire_walks_to_the_prior_trading_day(self) -> None:
        now = datetime(2026, 7, 10, 9, 0)  # Friday pre-fire → Thursday's fire
        got = seconds_since_trading_fire(
            hour=16, minute=15, now_local=now, is_export_day=_weekday_only
        )
        assert got == (24 * 3600.0) - (16.25 - 9.0) * 3600.0

    def test_monday_morning_walks_over_the_weekend_to_friday(self) -> None:
        now = datetime(2026, 7, 13, 8, 0)  # Monday 08:00 → Friday 16:15
        got = seconds_since_trading_fire(
            hour=16, minute=15, now_local=now, is_export_day=_weekday_only
        )
        assert got is not None
        assert got == pytest.approx((2 * 24 + 15.75) * 3600.0)

    def test_no_export_day_inside_window_is_none(self) -> None:
        got = seconds_since_trading_fire(
            hour=16,
            minute=15,
            now_local=datetime(2026, 7, 10, 18, 0),
            is_export_day=lambda _d: False,
        )
        assert got is None

    def test_default_export_day_skips_weekend_and_muhurat(self) -> None:
        # 2024-11-01 held the Diwali Muhurat evening session — a skipped
        # export day; 2024-11-02/03 are a weekend; 2024-10-31 is the newest
        # export day. Uses the real NSE calendar (covered span).
        assert _default_is_export_day(date(2024, 10, 31)) is True
        assert _default_is_export_day(date(2024, 11, 1)) is False  # Muhurat
        assert _default_is_export_day(date(2024, 11, 2)) is False  # Saturday
        got = seconds_since_trading_fire(
            hour=16,
            minute=15,
            now_local=datetime(2024, 11, 3, 12, 0),
            is_export_day=_default_is_export_day,
        )
        assert got is not None
        # Sunday 12:00 back to Thursday 2024-10-31 16:15.
        assert got == pytest.approx((2 * 24 + 19.75) * 3600.0)


# ── deadman_verdict ──────────────────────────────────────────────────────


def _write_journal(tmp_path: Path, name: str, mtime: float) -> None:
    cv = tmp_path / ".crossverify"
    cv.mkdir(exist_ok=True)
    journal = cv / name
    journal.write_text("{}", encoding="utf-8")
    os.utime(journal, (mtime, mtime))


class TestDeadmanVerdict:
    def test_no_schedule_is_unknown(self, tmp_path: Path) -> None:
        verdict = deadman_verdict(lake_root=tmp_path, schedule_hm=None, now=1000.0)
        assert verdict.verdict == "UNKNOWN"
        assert verdict.allowed_age_seconds is None

    def test_unrepresentable_clock_is_unknown(self, tmp_path: Path) -> None:
        verdict = deadman_verdict(lake_root=tmp_path, schedule_hm=(16, 15), now=float("1e300"))
        assert verdict.verdict == "UNKNOWN"
        assert "not representable" in verdict.detail

    def test_calendar_failure_is_unknown_never_a_crash(self, tmp_path: Path) -> None:
        def boom(_d: date) -> bool:
            raise RuntimeError("calendar unavailable")

        verdict = deadman_verdict(
            lake_root=tmp_path,
            schedule_hm=(16, 15),
            now=1000.0,
            now_local=datetime(2026, 7, 10, 18, 0),
            is_export_day=boom,
        )
        assert verdict.verdict == "UNKNOWN"
        assert "calendar unavailable" in verdict.detail

    def test_no_export_day_in_window_is_unknown(self, tmp_path: Path) -> None:
        verdict = deadman_verdict(
            lake_root=tmp_path,
            schedule_hm=(16, 15),
            now=1000.0,
            now_local=datetime(2026, 7, 10, 18, 0),
            is_export_day=lambda _d: False,
        )
        assert verdict.verdict == "UNKNOWN"
        assert "no export" in verdict.detail

    def test_no_journal_ever_is_missed_with_pmset_hint(self, tmp_path: Path) -> None:
        verdict = deadman_verdict(
            lake_root=tmp_path,
            schedule_hm=(16, 15),
            now=1000.0,
            now_local=datetime(2026, 7, 10, 18, 0),
            is_export_day=_weekday_only,
        )
        assert verdict.verdict == "MISSED"
        assert verdict.newest_run_age_seconds is None
        assert "pmset repeat wakeorpoweron" in verdict.detail

    def test_fresh_journal_is_ok(self, tmp_path: Path) -> None:
        _write_journal(tmp_path, "2026-07-10.json", 990.0)
        verdict = deadman_verdict(
            lake_root=tmp_path,
            schedule_hm=(16, 15),
            now=1000.0,
            now_local=datetime(2026, 7, 10, 18, 0),
            is_export_day=_weekday_only,
        )
        assert verdict.verdict == "OK"
        assert verdict.newest_run_age_seconds == 10.0
        assert verdict.allowed_age_seconds == 105 * 60.0 + DEADMAN_GRACE_SECONDS

    def test_monday_morning_friday_journal_is_ok_not_false_missed(self, tmp_path: Path) -> None:
        """The trading-day walk: a weekend without journals never false-MISSes."""
        friday_age = (2 * 24 + 15.0) * 3600.0  # journal written Friday ~17:00
        _write_journal(tmp_path, "2026-07-10.json", 1_000_000.0 - friday_age)
        verdict = deadman_verdict(
            lake_root=tmp_path,
            schedule_hm=(16, 15),
            now=1_000_000.0,
            now_local=datetime(2026, 7, 13, 8, 0),  # Monday 08:00
            is_export_day=_weekday_only,
        )
        assert verdict.verdict == "OK"

    def test_stale_journal_is_missed(self, tmp_path: Path) -> None:
        stale_age = 3 * 24 * 3600.0
        _write_journal(tmp_path, "2026-07-07.json", 1_000_000.0 - stale_age)
        verdict = deadman_verdict(
            lake_root=tmp_path,
            schedule_hm=(16, 15),
            now=1_000_000.0,
            now_local=datetime(2026, 7, 10, 18, 0),  # Friday post-fire
            is_export_day=_weekday_only,
        )
        assert verdict.verdict == "MISSED"
        assert "slept through the trigger" in verdict.detail

    def test_negative_grace_is_clamped(self, tmp_path: Path) -> None:
        _write_journal(tmp_path, "2026-07-10.json", 1000.0 - 3600.0)
        verdict = deadman_verdict(
            lake_root=tmp_path,
            schedule_hm=(16, 15),
            now=1000.0,
            now_local=datetime(2026, 7, 10, 18, 0),
            is_export_day=_weekday_only,
            grace_seconds=-5.0,
        )
        assert verdict.allowed_age_seconds == 105 * 60.0

    def test_wallclock_now_local_default_path(self, tmp_path: Path) -> None:
        """now_local=None derives local time from `now` (any verdict is fine)."""
        import time as time_mod

        verdict = deadman_verdict(
            lake_root=tmp_path,
            schedule_hm=(16, 15),
            now=time_mod.time(),
            is_export_day=_weekday_only,
        )
        assert verdict.verdict in {"OK", "MISSED", "UNKNOWN"}


# ── status ───────────────────────────────────────────────────────────────


class TestStatus:
    def test_nothing_yet_is_honest_lines(self, tmp_path: Path) -> None:
        report = status(
            lake_root=tmp_path,
            plist_dir=tmp_path / "agents",
            runner=lambda argv: (1, "", ""),
            is_export_day=_weekday_only,
        )
        lines = report.lines()
        assert lines[0] == "schedule: plist ABSENT · launchctl NOT listed"
        assert lines[1] == "newest journal: none yet"
        assert lines[2].startswith("dead-man: UNKNOWN")

    def test_all_present(self, tmp_path: Path) -> None:
        calls: list[list[str]] = []
        install_crossverify(
            at="16:15",
            plist_dir=tmp_path / "agents",
            runner=_ok_runner(calls),  # type: ignore[arg-type]
            lake_root=None,
            program_arguments=["/bin/brutex", "crossverify", "run"],
            log_dir=tmp_path / "logs",
        )
        _write_journal(tmp_path, "2026-07-10.json", 1_000_000.0 - 60.0)
        report = status(
            lake_root=tmp_path,
            plist_dir=tmp_path / "agents",
            runner=lambda argv: (0, "", ""),
            now=lambda: 1_000_000.0,
            is_export_day=_weekday_only,
        )
        assert report.plist_present
        assert report.launchd_listed
        assert report.next_fire == "16:15"
        assert report.newest_journal_line == "2026-07-10.json · 60s ago"
        assert report.deadman.verdict in {"OK", "MISSED"}
        assert "next fire 16:15" in report.lines()[0]

    def test_plist_without_calendar_has_no_next_fire(self, tmp_path: Path) -> None:
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "com.brutex.crossverify.plist").write_text(
            "<plist><dict></dict></plist>", encoding="utf-8"
        )
        report = status(
            lake_root=tmp_path,
            plist_dir=agents,
            runner=lambda argv: (0, "", ""),
            is_export_day=_weekday_only,
        )
        assert report.plist_present
        assert report.next_fire is None
        assert report.deadman.verdict == "UNKNOWN"
