# pyright: reportPrivateUsage=none
"""Tests for `brutex.nightly.schedule` (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

Plist golden bytes (both env-var arms; absolute log paths), the
which-hit/module-fallback argv resolution, the install argv sequence
(unload→load→list) via a fake runner, the MANDATORY verify-FAIL arm (the
TickVault never-installed lesson), uninstall + still-listed failure, bad
``--at`` values, and the status assembly (all-present / nothing-yet /
"existence ≠ held" caveat). Tests never exec a real launchctl.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest
from brutex.nightly.schedule import (
    CRON_GUIDANCE,
    DEFAULT_AT,
    DEFAULT_LABEL,
    InstallOutcome,
    deadman_verdict,
    install,
    newest_nightly_run_age,
    parse_at,
    pmset_wake_command,
    render_launchd_plist,
    resolve_program_arguments,
    seconds_since_scheduled_fire,
    status,
    uninstall,
)


class FakeRunner:
    """Records every argv; returns scripted (rc, out, err) per command verb."""

    def __init__(self, *, list_rc: int = 0, load_rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.list_rc = list_rc
        self.load_rc = load_rc

    def __call__(self, argv: Sequence[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        verb = argv[1]
        if verb == "load":
            return (self.load_rc, "", "load says no" if self.load_rc else "")
        if verb == "list":
            return (self.list_rc, "", "not found" if self.list_rc else "")
        return (0, "", "")  # unload is best-effort


# ── parse_at + argv resolution ───────────────────────────────────────────


def test_parse_at_valid_and_default() -> None:
    assert parse_at("01:30") == (1, 30)
    assert parse_at("23:59") == (23, 59)
    assert parse_at(" 9:05 ") == (9, 5)
    assert parse_at(DEFAULT_AT) == (1, 30)


@pytest.mark.parametrize("bad", ["24:00", "1:5", "01-30", "noon", "", "01:60"])
def test_parse_at_junk_fails_loud(bad: str) -> None:
    with pytest.raises(ValueError, match="--at must be HH:MM"):
        parse_at(bad)


def test_resolve_program_arguments_which_hit_and_fallback() -> None:
    assert resolve_program_arguments(which=lambda _n: "/opt/bin/brutex") == [
        "/opt/bin/brutex",
        "nightly",
        "run",
    ]
    assert resolve_program_arguments(which=lambda _n: None) == [
        sys.executable,
        "-m",
        "brutex",
        "nightly",
        "run",
    ]


# ── plist golden ─────────────────────────────────────────────────────────


def test_plist_golden_with_env_vars() -> None:
    rendered = render_launchd_plist(
        label=DEFAULT_LABEL,
        program_arguments=["/opt/bin/brutex", "nightly", "run"],
        hour=1,
        minute=30,
        env_vars={
            "BRUTEX_LAKE_ROOT": "/Volumes/lake",
            "BRUTEX_NIGHTLY_BACKUP_ROOT": "/Volumes/backups",
        },
        log_dir=Path("/Users/op/.brutex/logs"),
    )
    assert rendered == (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        "  <key>Label</key><string>com.brutex.nightly</string>\n"
        "  <key>ProgramArguments</key><array>\n"
        "    <string>/opt/bin/brutex</string>\n"
        "    <string>nightly</string>\n"
        "    <string>run</string>\n"
        "  </array>\n"
        "  <key>StartCalendarInterval</key><dict>\n"
        "    <key>Hour</key><integer>1</integer><key>Minute</key><integer>30</integer>\n"
        "  </dict>\n"
        "  <key>RunAtLoad</key><false/>\n"
        "  <key>EnvironmentVariables</key><dict>\n"
        "    <key>BRUTEX_LAKE_ROOT</key><string>/Volumes/lake</string>\n"
        "    <key>BRUTEX_NIGHTLY_BACKUP_ROOT</key><string>/Volumes/backups</string>\n"
        "  </dict>\n"
        "  <key>StandardOutPath</key>"
        "<string>/Users/op/.brutex/logs/nightly-launchd.out.log</string>\n"
        "  <key>StandardErrorPath</key>"
        "<string>/Users/op/.brutex/logs/nightly-launchd.err.log</string>\n"
        "  <key>ProcessType</key><string>Background</string>\n"
        "  <key>ThrottleInterval</key><integer>30</integer>\n"
        "</dict></plist>\n"
    )
    # Absolute log paths — launchd does NOT expand '~'.
    assert "~" not in rendered
    # No secrets baked in — Telegram config rides bootstrap_env, never the plist.
    assert "TELEGRAM" not in rendered


def test_plist_omits_env_dict_when_empty_and_escapes_xml() -> None:
    rendered = render_launchd_plist(
        label="com.brutex.<test>&co",
        program_arguments=[sys.executable, "-m", "brutex", "nightly", "run"],
        hour=23,
        minute=5,
        env_vars={},
        log_dir=Path("/logs"),
    )
    assert "EnvironmentVariables" not in rendered
    assert "com.brutex.&lt;test&gt;&amp;co" in rendered
    assert "<key>Hour</key><integer>23</integer><key>Minute</key><integer>5</integer>" in rendered


# ── install ──────────────────────────────────────────────────────────────


def _install(tmp_path: Path, runner: FakeRunner) -> InstallOutcome:
    return install(
        label=DEFAULT_LABEL,
        plist_dir=tmp_path / "LaunchAgents",
        runner=runner,
        program_arguments=["/opt/bin/brutex", "nightly", "run"],
        hour=1,
        minute=30,
        env_vars={},
        log_dir=tmp_path / "logs",
    )


def test_install_writes_loads_and_verifies(tmp_path: Path) -> None:
    runner = FakeRunner()
    outcome = _install(tmp_path, runner)
    assert outcome.ok is True
    plist = tmp_path / "LaunchAgents" / f"{DEFAULT_LABEL}.plist"
    assert outcome.plist_path == plist
    assert plist.exists()
    assert (tmp_path / "logs").is_dir()
    # The exact launchctl sequence: unload (idempotent) → load → list (VERIFY).
    assert runner.calls == [
        ["launchctl", "unload", "-w", str(plist)],
        ["launchctl", "load", "-w", str(plist)],
        ["launchctl", "list", DEFAULT_LABEL],
    ]
    assert "VERIFIED" in outcome.detail
    assert "Next fire 01:30" in outcome.detail
    assert "brutex nightly run --dry-run" in outcome.detail  # the ONE next step


def test_install_load_failure_is_loud(tmp_path: Path) -> None:
    outcome = _install(tmp_path, FakeRunner(load_rc=1))
    assert outcome.ok is False
    assert "launchctl load failed (rc 1)" in outcome.detail


def test_install_verify_failure_is_a_failure(tmp_path: Path) -> None:
    """The TickVault pin: written + loaded but NOT listed ⇒ the install FAILED."""
    outcome = _install(tmp_path, FakeRunner(list_rc=1))
    assert outcome.ok is False
    assert "does NOT list" in outcome.detail
    assert "the job will NEVER fire" in outcome.detail


# ── uninstall ────────────────────────────────────────────────────────────


def test_uninstall_removes_and_verifies_not_listed(tmp_path: Path) -> None:
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    plist = plist_dir / f"{DEFAULT_LABEL}.plist"
    plist.write_text("<plist/>", encoding="utf-8")
    runner = FakeRunner(list_rc=1)  # after unload, list must FAIL
    outcome = uninstall(label=DEFAULT_LABEL, plist_dir=plist_dir, runner=runner)
    assert outcome.ok is True
    assert not plist.exists()
    assert "plist removed" in outcome.detail


def test_uninstall_without_plist_is_ok(tmp_path: Path) -> None:
    outcome = uninstall(label=DEFAULT_LABEL, plist_dir=tmp_path, runner=FakeRunner(list_rc=1))
    assert outcome.ok is True
    assert "no plist present" in outcome.detail


def test_uninstall_still_listed_is_a_failure(tmp_path: Path) -> None:
    outcome = uninstall(label=DEFAULT_LABEL, plist_dir=tmp_path, runner=FakeRunner(list_rc=0))
    assert outcome.ok is False
    assert "STILL listed" in outcome.detail


# ── status ───────────────────────────────────────────────────────────────


def test_status_nothing_yet_is_three_honest_lines(tmp_path: Path) -> None:
    report = status(
        label=DEFAULT_LABEL,
        plist_dir=tmp_path / "LaunchAgents",
        lake_root=tmp_path / "lake",
        results_root=tmp_path / "lake" / "sweeps",
        backup_root=tmp_path / "backups",
        runner=FakeRunner(list_rc=1),
    )
    assert report.plist_present is False
    assert report.launchd_listed is False
    assert report.next_fire is None
    assert report.last_report_line is None
    assert report.latest_snapshot_line is None
    lines = report.lines()
    assert lines[0] == "schedule: plist ABSENT · launchctl NOT listed"
    assert lines[1] == "last run: none yet"
    assert lines[2] == "latest snapshot: none yet"
    # No plist → no schedule → the row-20 dead-man is honestly UNKNOWN.
    assert lines[3].startswith("dead-man: UNKNOWN")
    assert report.deadman.verdict == "UNKNOWN"
    assert "existence ≠ held" in lines[4]


def test_status_all_present(tmp_path: Path) -> None:
    plist_dir = tmp_path / "LaunchAgents"
    lake = tmp_path / "lake"
    results = lake / "sweeps"
    backups = tmp_path / "backups"
    _install(tmp_path, FakeRunner())
    # A last-run report.
    run_dir = results / "nightly" / "nightly-A"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(
        json.dumps({"nightly_id": "nightly-A", "exit_code": 0, "blocker": None}),
        encoding="utf-8",
    )
    # A sealed snapshot (+ a .partial and an unsealed dir that must be ignored).
    sealed = backups / "nightly-A"
    sealed.mkdir(parents=True)
    (sealed / "manifest.json").write_text("{}", encoding="utf-8")
    (backups / "nightly-B.partial").mkdir()
    (backups / "not-a-snapshot").mkdir()
    # The lock file exists (from some prior run) — existence ≠ held.
    (lake / ".locks").mkdir(parents=True)
    (lake / ".locks" / "nightly_singleflight.lock").write_text("", encoding="utf-8")

    report = status(
        label=DEFAULT_LABEL,
        plist_dir=plist_dir,
        lake_root=lake,
        results_root=results,
        backup_root=backups,
        runner=FakeRunner(list_rc=0),
        now=lambda: 10**12,
    )
    assert report.plist_present is True
    assert report.launchd_listed is True
    assert report.next_fire == "01:30"
    assert report.last_report_line is not None
    assert report.last_report_line.startswith("nightly-A exit 0 blocker none")
    assert report.latest_snapshot_line is not None
    assert report.latest_snapshot_line.startswith("nightly-A ·")
    assert "exists" in report.lines()[4]
    assert "next fire 01:30" in report.lines()[0]
    # The fake clock (10**12) is outside datetime's range — the dead-man must
    # degrade honestly to UNKNOWN, never crash the read-only status command.
    assert report.deadman.verdict == "UNKNOWN"
    assert "not representable" in report.deadman.detail


def test_status_unreadable_report_degrades_honestly(tmp_path: Path) -> None:
    results = tmp_path / "sweeps"
    run_dir = results / "nightly" / "nightly-Z"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text("{torn", encoding="utf-8")
    report = status(
        label=DEFAULT_LABEL,
        plist_dir=tmp_path,
        lake_root=tmp_path,
        results_root=results,
        backup_root=tmp_path / "none",
        runner=FakeRunner(list_rc=1),
    )
    assert report.last_report_line is not None
    assert "(report unreadable)" in report.last_report_line


def test_status_plist_without_calendar_has_no_next_fire(tmp_path: Path) -> None:
    plist_dir = tmp_path / "LaunchAgents"
    plist_dir.mkdir()
    (plist_dir / f"{DEFAULT_LABEL}.plist").write_text("<plist/>", encoding="utf-8")
    report = status(
        label=DEFAULT_LABEL,
        plist_dir=plist_dir,
        lake_root=tmp_path,
        results_root=tmp_path,
        backup_root=tmp_path,
        runner=FakeRunner(list_rc=1),
    )
    assert report.plist_present is True
    assert report.next_fire is None


def test_cron_guidance_names_the_command() -> None:
    assert "nightly run" in CRON_GUIDANCE
    assert CRON_GUIDANCE.startswith("30 1 * * *")


# ── row 20 — pmset scheduled-wake command (gap-register jul-8) ───────────


def test_pmset_wake_command_default_lead() -> None:
    """The install/runbook-mandated wake lands PMSET_WAKE_LEAD_MINUTES early."""
    assert pmset_wake_command(1, 30) == "sudo pmset repeat wakeorpoweron MTWRFSU 01:25:00"


def test_pmset_wake_command_wraps_midnight() -> None:
    assert pmset_wake_command(0, 2) == "sudo pmset repeat wakeorpoweron MTWRFSU 23:57:00"
    assert pmset_wake_command(0, 0, lead_minutes=1) == (
        "sudo pmset repeat wakeorpoweron MTWRFSU 23:59:00"
    )


def test_pmset_wake_command_custom_lead() -> None:
    assert pmset_wake_command(13, 45, lead_minutes=15) == (
        "sudo pmset repeat wakeorpoweron MTWRFSU 13:30:00"
    )


# ── row 20 — dead-man staleness verdict ──────────────────────────────────


def test_seconds_since_scheduled_fire_after_and_before() -> None:
    at_ten = datetime(2026, 7, 8, 10, 0, 0)
    # Fire already past today (01:30) → hours since 01:30 today.
    assert seconds_since_scheduled_fire(hour=1, minute=30, now_local=at_ten) == (8 * 3600 + 30 * 60)
    # Fire later today (23:00) → measured from YESTERDAY's fire.
    assert seconds_since_scheduled_fire(hour=23, minute=0, now_local=at_ten) == 11 * 3600
    # Exactly at the fire instant → 0 (today's fire counts).
    assert seconds_since_scheduled_fire(hour=10, minute=0, now_local=at_ten) == 0.0


def test_newest_nightly_run_age_none_and_audit_excluded(tmp_path: Path) -> None:
    results = tmp_path / "sweeps"
    assert newest_nightly_run_age(results, now=100.0) is None
    # Only the shared cross-run audit/ dir (and a stray file) — still None:
    # audit/ is appended by restore/backup commands too and would mask a miss.
    (results / "nightly" / "audit").mkdir(parents=True)
    (results / "nightly" / "stray.txt").write_text("x", encoding="utf-8")
    assert newest_nightly_run_age(results, now=100.0) is None


def test_newest_nightly_run_age_picks_newest_and_clamps(tmp_path: Path) -> None:
    results = tmp_path / "sweeps"
    old = results / "nightly" / "nightly-OLD"
    new = results / "nightly" / "nightly-NEW"
    old.mkdir(parents=True)
    new.mkdir()
    os.utime(old, (1_000.0, 1_000.0))
    os.utime(new, (2_000.0, 2_000.0))
    age = newest_nightly_run_age(results, now=2_600.0)
    assert age == 600.0
    # Clock skew (dir mtime ahead of now) clamps to 0, never negative.
    assert newest_nightly_run_age(results, now=1_500.0) == 0.0
    # The default now_fn path (now < 0 sentinel) reads the real clock.
    real = newest_nightly_run_age(results, now_fn=lambda: 2_600.0)
    assert real == 600.0


def test_deadman_no_schedule_is_unknown(tmp_path: Path) -> None:
    verdict = deadman_verdict(results_root=tmp_path, schedule_hm=None, now=1_000.0)
    assert verdict.verdict == "UNKNOWN"
    assert "unjudgeable" in verdict.detail
    assert verdict.allowed_age_seconds is None


def test_deadman_no_evidence_is_missed_with_pmset_hint(tmp_path: Path) -> None:
    now_local = datetime(2026, 7, 8, 10, 0, 0)
    verdict = deadman_verdict(
        results_root=tmp_path,
        schedule_hm=(1, 30),
        now=1_000.0,
        now_local=now_local,
    )
    assert verdict.verdict == "MISSED"
    assert "NO nightly run evidence" in verdict.detail
    assert "pmset repeat wakeorpoweron MTWRFSU 01:25:00" in verdict.detail
    assert verdict.newest_run_age_seconds is None
    assert verdict.allowed_age_seconds is not None


def test_deadman_fresh_evidence_is_ok(tmp_path: Path) -> None:
    run_dir = tmp_path / "nightly" / "nightly-A"
    run_dir.mkdir(parents=True)
    now = run_dir.stat().st_mtime + 60.0  # evidence 60 s old
    verdict = deadman_verdict(
        results_root=tmp_path,
        schedule_hm=(1, 30),
        now=now,
        now_local=datetime(2026, 7, 8, 1, 31, 0),
    )
    assert verdict.verdict == "OK"
    assert verdict.newest_run_age_seconds is not None
    assert verdict.newest_run_age_seconds <= 61.0


def test_deadman_stale_evidence_is_missed(tmp_path: Path) -> None:
    """Evidence older than 24h + grace is MISSED at ANY local time (tz-robust)."""
    run_dir = tmp_path / "nightly" / "nightly-A"
    run_dir.mkdir(parents=True)
    stale_mtime = 1_000.0
    os.utime(run_dir, (stale_mtime, stale_mtime))
    now = stale_mtime + 30 * 3600.0  # 30 h later > max(24 h) + 3 h grace
    verdict = deadman_verdict(
        results_root=tmp_path,
        schedule_hm=(1, 30),
        now=now,
        now_local=datetime(2026, 7, 8, 10, 0, 0),
    )
    assert verdict.verdict == "MISSED"
    assert "slept through" in verdict.detail
    assert "pmset repeat wakeorpoweron" in verdict.detail


def test_deadman_negative_grace_clamped(tmp_path: Path) -> None:
    """A negative grace never subtracts from the allowed window."""
    run_dir = tmp_path / "nightly" / "nightly-A"
    run_dir.mkdir(parents=True)
    now = run_dir.stat().st_mtime + 1.0
    verdict = deadman_verdict(
        results_root=tmp_path,
        schedule_hm=(10, 0),
        now=now,
        now_local=datetime(2026, 7, 8, 10, 0, 30),  # 30 s past the fire
        grace_seconds=-999.0,
    )
    assert verdict.allowed_age_seconds == 30.0  # fire delta only, clamp at 0
    assert verdict.verdict == "OK"  # evidence 1 s old <= 30 s


def test_deadman_unrepresentable_clock_is_unknown(tmp_path: Path) -> None:
    verdict = deadman_verdict(results_root=tmp_path, schedule_hm=(1, 30), now=float(10**12))
    assert verdict.verdict == "UNKNOWN"
    assert "not representable" in verdict.detail


def test_status_deadman_missed_line(tmp_path: Path) -> None:
    """An installed schedule + stale evidence reads MISSED on the status line."""
    plist_dir = tmp_path / "LaunchAgents"
    results = tmp_path / "lake" / "sweeps"
    _install(tmp_path, FakeRunner())
    run_dir = results / "nightly" / "nightly-STALE"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(
        json.dumps({"nightly_id": "nightly-STALE", "exit_code": 0, "blocker": None}),
        encoding="utf-8",
    )
    stale = 1_000.0
    os.utime(run_dir, (stale, stale))  # AFTER the write — writes bump dir mtime
    report = status(
        label=DEFAULT_LABEL,
        plist_dir=plist_dir,
        lake_root=tmp_path / "lake",
        results_root=results,
        backup_root=tmp_path / "backups",
        runner=FakeRunner(list_rc=0),
        now=lambda: stale + 30 * 3600.0,  # 30 h > 24 h + 3 h grace, tz-robust
    )
    assert report.deadman.verdict == "MISSED"
    assert any(line.startswith("dead-man: MISSED") for line in report.lines())
