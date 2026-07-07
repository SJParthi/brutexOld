"""launchd scheduling for the nightly chain (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

The one-time operator bootstrap: ``brutex nightly install`` renders a launchd
LaunchAgent plist (absolute paths, token-array ProgramArguments, NO secrets —
``bootstrap_env()`` loads ``~/.brutex/.env`` at run time), writes it
atomically, loads it, and then runs the MANDATORY post-install verification:
``launchctl list <label>`` must succeed, else the install FAILS LOUDLY —
"installed the plist but launchctl does NOT list it — the job will NEVER
fire" (the TickVault never-installed lesson, mechanized). Every ``launchctl``
invocation goes through an injected :data:`CommandRunner`, so tests never
exec launchctl.

Sleep/wake honesty (Golden Rule #10, reproduced in the runbook + install
stdout): launchd ``StartCalendarInterval`` does NOT wake a sleeping Mac —
jobs missed while ASLEEP fire ONCE at next wake (coalesced); missed while
POWERED OFF are skipped entirely. ``keep_awake`` (caffeinate) holds the
machine awake WHILE the nightly runs but cannot wake it at 01:30.
Guaranteed-on-time fires need the operator's one-time
``sudo pmset repeat wakeorpoweron MTWRFSU 01:25:00`` — documented, NEVER run
by BruteX (a system-wide power setting; we do not sudo on the operator's
behalf). Linux gets cron guidance (:data:`CRON_GUIDANCE`); plain cron missed
= skipped (no catch-up; anacron is the named alternative).
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import structlog

from brutex.nightly.backup import MANIFEST_NAME, PARTIAL_SUFFIX

logger = structlog.get_logger(__name__)

__all__ = [
    "CRON_GUIDANCE",
    "DEFAULT_AT",
    "DEFAULT_LABEL",
    "CommandRunner",
    "InstallOutcome",
    "StatusReport",
    "install",
    "parse_at",
    "render_launchd_plist",
    "resolve_program_arguments",
    "status",
    "uninstall",
]

#: ``(argv) -> (returncode, stdout, stderr)`` — injected; tests never exec launchctl.
CommandRunner = Callable[[Sequence[str]], tuple[int, str, str]]

DEFAULT_LABEL: Final[str] = "com.brutex.nightly"

#: Default fire time (local; operator envelope = IST): after the prior
#: session's data is final, before the 06:00 token re-mint, beside (not
#: colliding with) the sweep-daemon 02:30 precedent — the operator must NOT
#: run both against one lake (the liveness probe refuses if they overlap).
DEFAULT_AT: Final[str] = "01:30"

#: The Linux path (docs-only — no systemd unit shipped this phase).
CRON_GUIDANCE: Final[str] = (
    "30 1 * * * BRUTEX_LAKE_ROOT=<abs lake root> /abs/path/to/brutex nightly run "
    ">> ~/.brutex/logs/nightly-cron.log 2>&1"
)

_AT_RE: Final[re.Pattern[str]] = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_at(text: str) -> tuple[int, int]:
    """Parse ``HH:MM`` fail-loud; junk raises :class:`ValueError` naming the value."""
    match = _AT_RE.match(text.strip())
    if match is None:
        raise ValueError(f"--at must be HH:MM (24h), got {text!r}")
    return int(match.group(1)), int(match.group(2))


def resolve_program_arguments(
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    """The absolute argv the plist runs: the ``brutex`` script, else ``python -m``."""
    brutex_bin = which("brutex")
    if brutex_bin:
        return [brutex_bin, "nightly", "run"]
    return [sys.executable, "-m", "brutex", "nightly", "run"]


def render_launchd_plist(
    *,
    label: str,
    program_arguments: Sequence[str],
    hour: int,
    minute: int,
    env_vars: Mapping[str, str],
    log_dir: Path,
) -> str:
    """Render the LaunchAgent plist (golden-pinned bytes).

    Absolute log paths (launchd does NOT expand ``~``), token-array argv, an
    ``EnvironmentVariables`` dict emitted ONLY when *env_vars* is non-empty —
    and NEVER Telegram secrets (``bootstrap_env`` loads ``~/.brutex/.env`` in
    the process itself).
    """
    argv_xml = "".join(
        f"    <string>{_xml_escape(token)}</string>\n" for token in program_arguments
    )
    env_xml = ""
    if env_vars:
        rows = "".join(
            f"    <key>{_xml_escape(key)}</key><string>{_xml_escape(value)}</string>\n"
            for key, value in sorted(env_vars.items())
        )
        env_xml = f"  <key>EnvironmentVariables</key><dict>\n{rows}  </dict>\n"
    out_log = log_dir / "nightly-launchd.out.log"
    err_log = log_dir / "nightly-launchd.err.log"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f"  <key>Label</key><string>{_xml_escape(label)}</string>\n"
        f"  <key>ProgramArguments</key><array>\n{argv_xml}  </array>\n"
        "  <key>StartCalendarInterval</key><dict>\n"
        f"    <key>Hour</key><integer>{hour}</integer>"
        f"<key>Minute</key><integer>{minute}</integer>\n"
        "  </dict>\n"
        "  <key>RunAtLoad</key><false/>\n"
        f"{env_xml}"
        f"  <key>StandardOutPath</key><string>{_xml_escape(str(out_log))}</string>\n"
        f"  <key>StandardErrorPath</key><string>{_xml_escape(str(err_log))}</string>\n"
        "  <key>ProcessType</key><string>Background</string>\n"
        "  <key>ThrottleInterval</key><integer>30</integer>\n"
        "</dict></plist>\n"
    )


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


@dataclass(frozen=True, slots=True)
class InstallOutcome:
    """One install/uninstall attempt's honest verdict."""

    ok: bool
    plist_path: Path
    detail: str


def install(
    *,
    label: str,
    plist_dir: Path,
    runner: CommandRunner,
    program_arguments: Sequence[str],
    hour: int,
    minute: int,
    env_vars: Mapping[str, str],
    log_dir: Path,
) -> InstallOutcome:
    """Write + load the LaunchAgent, then VERIFY it is actually listed.

    Install flow: render → atomic write → ``launchctl unload -w`` (best-effort
    — idempotent re-install) → ``launchctl load -w`` → the MANDATORY
    ``launchctl list <label>`` verification. installed-but-unverified IS a
    failure (exit 2 at the CLI) — the job would NEVER fire.
    """
    from brutex.data.atomic_io import atomic_write_simple

    plist_path = plist_dir / f"{label}.plist"
    payload = render_launchd_plist(
        label=label,
        program_arguments=program_arguments,
        hour=hour,
        minute=minute,
        env_vars=env_vars,
        log_dir=log_dir,
    )
    plist_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_simple(plist_path, lambda tmp: tmp.write_text(payload, encoding="utf-8"))

    runner(["launchctl", "unload", "-w", str(plist_path)])  # best-effort re-install
    rc, _out, err = runner(["launchctl", "load", "-w", str(plist_path)])
    if rc != 0:
        return InstallOutcome(
            ok=False,
            plist_path=plist_path,
            detail=f"launchctl load failed (rc {rc}): {err.strip()}",
        )
    rc, _out, err = runner(["launchctl", "list", label])
    if rc != 0:
        return InstallOutcome(
            ok=False,
            plist_path=plist_path,
            detail=(
                f"installed the plist but launchctl does NOT list {label} — "
                f"the job will NEVER fire (rc {rc}: {err.strip()})"
            ),
        )
    logger.info("nightly.schedule.installed", label=label, plist=str(plist_path))
    return InstallOutcome(
        ok=True,
        plist_path=plist_path,
        detail=(
            f"installed + VERIFIED ({label} listed). Next fire {hour:02d}:{minute:02d} "
            "local. Next step: brutex nightly run --dry-run"
        ),
    )


def uninstall(*, label: str, plist_dir: Path, runner: CommandRunner) -> InstallOutcome:
    """Unload + remove the LaunchAgent, verifying launchctl no longer lists it."""
    plist_path = plist_dir / f"{label}.plist"
    runner(["launchctl", "unload", "-w", str(plist_path)])  # best-effort
    removed = False
    if plist_path.exists():
        plist_path.unlink()
        removed = True
    rc, _out, _err = runner(["launchctl", "list", label])
    if rc == 0:
        return InstallOutcome(
            ok=False,
            plist_path=plist_path,
            detail=f"{label} is STILL listed by launchctl after unload — not uninstalled",
        )
    logger.info("nightly.schedule.uninstalled", label=label, removed=removed)
    return InstallOutcome(
        ok=True,
        plist_path=plist_path,
        detail=f"uninstalled ({'plist removed' if removed else 'no plist present'}; not listed)",
    )


@dataclass(frozen=True, slots=True)
class StatusReport:
    """The 6 a.m. command's honest snapshot (three lines even when nothing ran)."""

    plist_present: bool
    launchd_listed: bool
    next_fire: str | None  # "HH:MM" parsed from the plist, else None
    last_report_line: str | None  # "<id> exit <code> blocker <x> · <age>s ago"
    latest_snapshot_line: str | None  # "<name> · <age>s ago"
    lock_note: str  # existence ≠ held — flocks cannot be observed without acquiring

    def lines(self) -> list[str]:
        plist = "present" if self.plist_present else "ABSENT"
        listed = "listed" if self.launchd_listed else "NOT listed"
        fire = f" · next fire {self.next_fire}" if self.next_fire else ""
        return [
            f"schedule: plist {plist} · launchctl {listed}{fire}",
            f"last run: {self.last_report_line or 'none yet'}",
            f"latest snapshot: {self.latest_snapshot_line or 'none yet'}",
            self.lock_note,
        ]


_HOUR_MIN_RE: Final[re.Pattern[str]] = re.compile(
    r"<key>Hour</key><integer>(\d+)</integer><key>Minute</key><integer>(\d+)</integer>"
)


def status(
    *,
    label: str,
    plist_dir: Path,
    lake_root: Path,
    results_root: Path,
    backup_root: Path,
    runner: CommandRunner,
    now: Callable[[], float] = time.time,
) -> StatusReport:
    """Assemble the honest status snapshot (read-only; never raises on absence)."""
    plist_path = plist_dir / f"{label}.plist"
    next_fire: str | None = None
    plist_present = plist_path.exists()
    if plist_present:
        match = _HOUR_MIN_RE.search(plist_path.read_text(encoding="utf-8"))
        if match is not None:
            next_fire = f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
    rc, _out, _err = runner(["launchctl", "list", label])

    last_report_line: str | None = None
    reports = sorted(
        (results_root / "nightly").glob("*/report.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if reports:
        newest = reports[-1]
        age = int(max(0.0, now() - newest.stat().st_mtime))
        try:
            data = json.loads(newest.read_text(encoding="utf-8"))
            last_report_line = (
                f"{data.get('nightly_id')} exit {data.get('exit_code')} "
                f"blocker {data.get('blocker') or 'none'} · {age}s ago"
            )
        except (OSError, ValueError):
            last_report_line = f"{newest.parent.name} (report unreadable) · {age}s ago"

    latest_snapshot_line: str | None = None
    if backup_root.exists():
        sealed = [
            child
            for child in sorted(backup_root.iterdir())
            if child.is_dir()
            and not child.name.endswith(PARTIAL_SUFFIX)
            and (child / MANIFEST_NAME).exists()
        ]
        if sealed:
            newest_snap = max(sealed, key=lambda p: p.stat().st_mtime)
            age = int(max(0.0, now() - newest_snap.stat().st_mtime))
            latest_snapshot_line = f"{newest_snap.name} · {age}s ago"

    lock_file = lake_root / ".locks" / "nightly_singleflight.lock"
    lock_note = (
        f"nightly lock file {'exists' if lock_file.exists() else 'absent'} at {lock_file} "
        "(existence ≠ held — a flock cannot be observed without acquiring it)"
    )
    return StatusReport(
        plist_present=plist_present,
        launchd_listed=rc == 0,
        next_fire=next_fire,
        last_report_line=last_report_line,
        latest_snapshot_line=latest_snapshot_line,
        lock_note=lock_note,
    )
