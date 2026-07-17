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
On-time fires therefore REQUIRE the operator's one-time scheduled wake —
:func:`pmset_wake_command` computes the exact
``sudo pmset repeat wakeorpoweron MTWRFSU HH:MM:00`` for
:data:`PMSET_WAKE_LEAD_MINUTES` minutes before the trigger, printed by
``nightly install`` and MANDATED by the runbook (gap-register row 20) —
documented + printed, NEVER run by BruteX (a system-wide power setting; we
do not sudo on the operator's behalf). Linux gets cron guidance
(:data:`CRON_GUIDANCE`); plain cron missed = skipped (no catch-up; anacron
is the named alternative).

The dead-man half of row 20 lives here too: :func:`deadman_verdict` is the
pure staleness check (expected schedule + the newest per-run nightly dir
under ``<results_root>/nightly/`` → ``OK`` / ``MISSED`` / ``UNKNOWN``),
surfaced on ``brutex nightly status`` and via
``SweepAlerter.nightly_deadman``; the dashboard-banner half is P4's zone —
the banner wires this same verdict at merge-integration.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

import structlog

from brutex.nightly.backup import MANIFEST_NAME, PARTIAL_SUFFIX

logger = structlog.get_logger(__name__)

__all__ = [
    "CRON_GUIDANCE",
    "DEADMAN_GRACE_SECONDS",
    "DEFAULT_AT",
    "DEFAULT_LABEL",
    "PMSET_WAKE_LEAD_MINUTES",
    "CommandRunner",
    "DeadmanVerdict",
    "InstallOutcome",
    "StatusReport",
    "deadman_verdict",
    "install",
    "newest_nightly_run_age",
    "parse_at",
    "pmset_wake_command",
    "render_launchd_plist",
    "resolve_program_arguments",
    "seconds_since_scheduled_fire",
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

#: Scheduled-wake lead before the launchd trigger (gap-register row 20: the
#: wake must land a few minutes BEFORE the fire so the machine is up).
PMSET_WAKE_LEAD_MINUTES: Final[int] = 5

#: Dead-man grace past the most recent expected fire before a run with no
#: evidence is declared MISSED. Generous by design: the per-run nightly dir
#: appears at CHAIN START (not chain end), so the grace only needs to cover
#: schedule jitter + a next-wake coalesced late fire, never sweep duration.
DEADMAN_GRACE_SECONDS: Final[float] = 3 * 3600.0


def pmset_wake_command(
    hour: int, minute: int, *, lead_minutes: int = PMSET_WAKE_LEAD_MINUTES
) -> str:
    """The EXACT one-time scheduled-wake command for a ``HH:MM`` trigger.

    Row 20 law: the runbook + install stdout MANDATE a
    ``pmset repeat wakeorpoweron`` wake *lead_minutes* before the launchd
    trigger (launchd cannot wake a sleeping Mac); BruteX documents + prints
    this command and NEVER runs it (sudo, system-wide power setting).
    Wraps across midnight (a 00:02 trigger wakes at 23:57).
    """
    total = (hour * 60 + minute - lead_minutes) % (24 * 60)
    return f"sudo pmset repeat wakeorpoweron MTWRFSU {total // 60:02d}:{total % 60:02d}:00"


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
    log_stem: str = "nightly-launchd",
) -> str:
    """Render the LaunchAgent plist (golden-pinned bytes).

    Absolute log paths (launchd does NOT expand ``~``), token-array argv, an
    ``EnvironmentVariables`` dict emitted ONLY when *env_vars* is non-empty —
    and NEVER Telegram secrets (``bootstrap_env`` loads ``~/.brutex/.env`` in
    the process itself).

    *log_stem* (DEC-CROSSVERIFY-PRODUCER-001, MIGRATION.md #1776 — ADDITIVE,
    defaulted so the nightly plist bytes are UNCHANGED, golden-pinned) names
    the ``<stem>.out.log`` / ``<stem>.err.log`` pair, so a SECOND LaunchAgent
    (the cross-verify producer, label ``com.brutex.crossverify``) renders
    through this ONE law without clobbering the nightly's log files.
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
    out_log = log_dir / f"{log_stem}.out.log"
    err_log = log_dir / f"{log_stem}.err.log"
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
    """Return ``text`` XML-escaped for embedding in a plist body."""
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
    log_stem: str = "nightly-launchd",
    next_step: str = "brutex nightly run --dry-run",
) -> InstallOutcome:
    """Write + load the LaunchAgent, then VERIFY it is actually listed.

    Install flow: render → atomic write → ``launchctl unload -w`` (best-effort
    — idempotent re-install) → ``launchctl load -w`` → the MANDATORY
    ``launchctl list <label>`` verification. installed-but-unverified IS a
    failure (exit 2 at the CLI) — the job would NEVER fire.

    *log_stem* rides through to :func:`render_launchd_plist` (ADDITIVE,
    defaulted — every nightly caller byte-identical) so the cross-verify
    producer's install DELEGATES here instead of copying the whole
    write/load/verify flow (CLAUDE.md §11 COMMON;
    DEC-CROSSVERIFY-PRODUCER-001); *next_step* names the delegating
    surface's own follow-up command in the success detail (same additive
    defaulted posture).
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
        log_stem=log_stem,
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
            f"local. Next step: {next_step}"
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
class DeadmanVerdict:
    """The row-20 dead-man staleness verdict over the nightly evidence trail.

    ``verdict`` ∈ ``{"OK", "MISSED", "UNKNOWN"}`` — ``MISSED`` means a fire
    was expected (per the installed schedule) and no per-run nightly dir is
    young enough to be it; ``UNKNOWN`` means no schedule could be read, so
    staleness is honestly unjudgeable (never a guessed OK — Golden Rule #1).
    """

    verdict: str
    detail: str
    newest_run_age_seconds: float | None  # None = no per-run nightly dir at all
    allowed_age_seconds: float | None  # since-last-expected-fire + grace; None when UNKNOWN


def seconds_since_scheduled_fire(*, hour: int, minute: int, now_local: datetime) -> float:
    """Seconds from the most recent ``HH:MM`` local fire time to *now_local*.

    Pure calendar arithmetic: today's fire if already past (or exactly now),
    else yesterday's. Always ``>= 0`` and ``< 24 h``.
    """
    fire = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if fire > now_local:
        fire -= timedelta(days=1)
    return (now_local - fire).total_seconds()


def newest_nightly_run_age(
    results_root: Path, *, now: float = -1.0, now_fn: Callable[[], float] = time.time
) -> float | None:
    """Age (s) of the newest PER-RUN nightly dir under ``<results_root>/nightly/``.

    The per-run dir appears at chain START, so a long still-running sweep leg
    never reads stale. The shared cross-run ``audit/`` dir is excluded — it is
    appended by restore/backup commands too and would mask a missed run.
    ``None`` = no per-run dir has ever been written.
    """
    current = now_fn() if now < 0 else now
    newest: float | None = None
    nightly_root = results_root / "nightly"
    with contextlib.suppress(OSError):
        for child in nightly_root.iterdir():
            if not child.is_dir() or child.name == "audit":
                continue
            with contextlib.suppress(OSError):
                mtime = child.stat().st_mtime
                if newest is None or mtime > newest:
                    newest = mtime
    if newest is None:
        return None
    return max(0.0, current - newest)


def deadman_verdict(
    *,
    results_root: Path,
    schedule_hm: tuple[int, int] | None,
    now: float,
    now_local: datetime | None = None,
    grace_seconds: float = DEADMAN_GRACE_SECONDS,
) -> DeadmanVerdict:
    """The row-20 dead-man check: did the scheduled nightly actually run?

    A sleeping Mac fires nothing and alerts nothing — this is the ONLY signal
    for that perfectly-silent staleness. ``schedule_hm=None`` (no plist / no
    parseable fire time) → ``UNKNOWN``; a fresh evidence dir (age ≤ seconds
    since the last expected fire + *grace_seconds*) → ``OK``; anything else
    (incl. no evidence ever) → ``MISSED`` with the pmset remediation named.
    """
    if schedule_hm is None:
        return DeadmanVerdict(
            verdict="UNKNOWN",
            detail="no schedule installed/parsed — staleness unjudgeable",
            newest_run_age_seconds=newest_nightly_run_age(results_root, now=now),
            allowed_age_seconds=None,
        )
    hour, minute = schedule_hm
    if now_local is None:
        try:
            local = datetime.fromtimestamp(now)
        except (OverflowError, OSError, ValueError):
            # A clock outside datetime's range must degrade honestly, never
            # crash the read-only status command (its never-raises contract).
            return DeadmanVerdict(
                verdict="UNKNOWN",
                detail=f"clock reading {now!r} not representable — staleness unjudgeable",
                newest_run_age_seconds=newest_nightly_run_age(results_root, now=now),
                allowed_age_seconds=None,
            )
    else:
        local = now_local
    allowed = seconds_since_scheduled_fire(hour=hour, minute=minute, now_local=local) + max(
        0.0, grace_seconds
    )
    age = newest_nightly_run_age(results_root, now=now)
    if age is None:
        return DeadmanVerdict(
            verdict="MISSED",
            detail=(
                f"schedule {hour:02d}:{minute:02d} installed but NO nightly run "
                "evidence exists — if just installed this clears after the first "
                f"fire; else the Mac likely slept through it ({pmset_wake_command(hour, minute)})"
            ),
            newest_run_age_seconds=None,
            allowed_age_seconds=allowed,
        )
    if age <= allowed:
        return DeadmanVerdict(
            verdict="OK",
            detail=f"newest nightly run dir is {int(age)}s old (≤ allowed {int(allowed)}s)",
            newest_run_age_seconds=age,
            allowed_age_seconds=allowed,
        )
    return DeadmanVerdict(
        verdict="MISSED",
        detail=(
            f"newest nightly run dir is {int(age)}s old > allowed {int(allowed)}s "
            f"(last expected fire {hour:02d}:{minute:02d} + grace) — the Mac "
            f"likely slept through the trigger; scheduled wake: "
            f"{pmset_wake_command(hour, minute)}"
        ),
        newest_run_age_seconds=age,
        allowed_age_seconds=allowed,
    )


@dataclass(frozen=True, slots=True)
class StatusReport:
    """The 6 a.m. command's honest snapshot (all lines even when nothing ran)."""

    plist_present: bool
    launchd_listed: bool
    next_fire: str | None  # "HH:MM" parsed from the plist, else None
    last_report_line: str | None  # "<id> exit <code> blocker <x> · <age>s ago"
    latest_snapshot_line: str | None  # "<name> · <age>s ago"
    lock_note: str  # existence ≠ held — flocks cannot be observed without acquiring
    deadman: DeadmanVerdict  # row 20 — the sleeping-Mac silent-staleness check

    def lines(self) -> list[str]:
        """Return the human-readable status lines for this report."""
        plist = "present" if self.plist_present else "ABSENT"
        listed = "listed" if self.launchd_listed else "NOT listed"
        fire = f" · next fire {self.next_fire}" if self.next_fire else ""
        return [
            f"schedule: plist {plist} · launchctl {listed}{fire}",
            f"last run: {self.last_report_line or 'none yet'}",
            f"latest snapshot: {self.latest_snapshot_line or 'none yet'}",
            f"dead-man: {self.deadman.verdict} — {self.deadman.detail}",
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
    schedule_hm: tuple[int, int] | None = None
    if next_fire is not None:
        hour_text, minute_text = next_fire.split(":")
        schedule_hm = (int(hour_text), int(minute_text))
    deadman = deadman_verdict(results_root=results_root, schedule_hm=schedule_hm, now=now())
    return StatusReport(
        plist_present=plist_present,
        launchd_listed=rc == 0,
        next_fire=next_fire,
        last_report_line=last_report_line,
        latest_snapshot_line=latest_snapshot_line,
        lock_note=lock_note,
        deadman=deadman,
    )
