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
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from brutex.nightly.schedule import (
    CRON_GUIDANCE,
    DEFAULT_AT,
    DEFAULT_LABEL,
    InstallOutcome,
    install,
    parse_at,
    render_launchd_plist,
    resolve_program_arguments,
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
    assert "existence ≠ held" in lines[3]


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
    assert "exists" in report.lines()[3]
    assert "next fire 01:30" in report.lines()[0]


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
