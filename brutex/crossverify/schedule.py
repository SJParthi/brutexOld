"""launchd scheduling + dead-man status for the cross-verify producer.

DEC-CROSSVERIFY-PRODUCER-001 §8. The one-time operator bootstrap:
``brutex crossverify install --at HH:MM`` — ``--at`` is REQUIRED (the run
time is INJECTED operator config decided by the 5-day post-close settle
probe; NO default time ships anywhere — Golden Rule #1). The install
DELEGATES to the proven :func:`brutex.nightly.schedule.install` flow
(render → atomic write → load → the MANDATORY ``launchctl list <label>``
verification — installed-but-unlisted FAILS LOUDLY, the TickVault lesson)
with this producer's label + argv + log stem; every ``launchctl``
invocation rides the injected :data:`~brutex.nightly.schedule.CommandRunner`
so tests never exec launchctl (CLAUDE.md §11 COMMON — no second copy of the
install flow).

The dead-man half: :func:`deadman_verdict` judges staleness off the newest
per-day publish JOURNAL's mtime (``<lake_root>/.crossverify/<day>.json`` —
the journal is this producer's durable run evidence, written at the staging
step of every non-dry-run that got past the session gate) against the most
recent EXPECTED fire. Unlike the nightly twin the expected fire is
TRADING-DAY-AWARE: a non-trading / Muhurat day's run exits ``skipped_*``
WITHOUT writing a journal (run.py step B), so a naive daily expectation
would false-MISS every Monday morning and the day after Diwali. Honest
limits (Golden Rule #10): the journal lands near run END (staging, after
the pull + export legs), so the grace must cover the whole 4 h hard
deadline — a dead box is still silent on-box (the #1596 posture). A
``nothing_to_do`` run DOES write its journal (the staging step saves it
even with zero publishable entries — run.py step K, pinned), so the
deadman reads OK for it; the journal-LESS exits past the session gate are
the PRE-staging ones — market-hours deferral, lock refusal, gate-absent,
a pull halt — which read MISSED once past grace. They SPLIT on alerting
(round-4 polish honesty): lock refusal / gate-absent / pull halt each
fired their own CRITICAL/refusal alert, so for those MISSED is
corroboration — but a MID-DAY (08:45–15:35 IST) vendor-calling fire's
step-E ``deferred_market_hours`` exit 0 is SILENT (no alert, no journal),
so for that mis-schedule this deadman is the ONLY signal.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from brutex.crossverify.journal import CROSSVERIFY_DIRNAME
from brutex.nightly.schedule import (
    CommandRunner,
    DeadmanVerdict,
    InstallOutcome,
    pmset_wake_command,
)
from brutex.nightly.schedule import (
    install as _install_launchagent,
)
from brutex.nightly.schedule import (
    uninstall as _uninstall_launchagent,
)

if TYPE_CHECKING:  # pragma: no cover — typing-only imports
    from collections.abc import Callable

logger = structlog.get_logger(__name__)

__all__ = [
    "AT_REQUIRED_MESSAGE",
    "CROSSVERIFY_CRON_GUIDANCE",
    "CROSSVERIFY_LABEL",
    "CROSSVERIFY_LOG_STEM",
    "DEADMAN_GRACE_SECONDS",
    "CrossverifyStatusReport",
    "deadman_verdict",
    "install_crossverify",
    "newest_journal_age",
    "resolve_program_arguments",
    "seconds_since_trading_fire",
    "status",
    "uninstall_crossverify",
]

CROSSVERIFY_LABEL: Final[str] = "com.brutex.crossverify"

#: The log-file stem baked into the plist — distinct from the nightly's
#: ``nightly-launchd`` pair so the two LaunchAgents never clobber each other.
CROSSVERIFY_LOG_STEM: Final[str] = "crossverify-launchd"

#: The refusal printed when ``install`` is invoked without ``--at`` — the run
#: time is probe-decided operator config; NO default ships (Golden Rule #1).
AT_REQUIRED_MESSAGE: Final[str] = (
    "crossverify install requires --at HH:MM — the post-close run time is "
    "operator config: run the 5-day post-close settle probe first, then "
    "choose --at (recommended: earliest time the probe shows 5/5 settled "
    "days, plus a 15-minute margin)"
)

#: The Linux path (docs-only — mirrors the nightly CRON_GUIDANCE shape).
CROSSVERIFY_CRON_GUIDANCE: Final[str] = (
    "MM HH * * * BRUTEX_LAKE_ROOT=<abs lake root> /abs/path/to/brutex crossverify run "
    ">> ~/.brutex/logs/crossverify-cron.log 2>&1"
)

#: Dead-man grace past the most recent expected TRADING-day fire. The journal
#: appears near run END (staging — after the pull + export legs), unlike the
#: nightly's chain-START evidence dir, so the grace covers the producer's
#: whole 4 h hard deadline (config DEFAULT_DEADLINE_S) + 1 h of schedule
#: jitter / next-wake coalesced late fire.
DEADMAN_GRACE_SECONDS: Final[float] = 5 * 3600.0

#: How many calendar days the expected-fire walk looks back for a trading
#: day. The longest NSE closure runs (long weekend + holiday cluster) are a
#: few days; 14 is a generous bound — no trading day inside it means the
#: calendar predicate itself is broken, reported honestly as UNKNOWN.
_EXPECTED_FIRE_LOOKBACK_DAYS: Final[int] = 14

_HOUR_MIN_RE: Final[re.Pattern[str]] = re.compile(
    r"<key>Hour</key><integer>(\d+)</integer><key>Minute</key><integer>(\d+)</integer>"
)

#: Journal filenames are exactly ``YYYY-MM-DD.json`` (journal.journal_path).
_JOURNAL_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


def resolve_program_arguments(
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    """The absolute argv the plist runs: the ``brutex`` script, else ``python -m``."""
    brutex_bin = which("brutex")
    if brutex_bin:
        return [brutex_bin, "crossverify", "run"]
    return [sys.executable, "-m", "brutex", "crossverify", "run"]


def install_crossverify(
    *,
    at: str | None,
    plist_dir: Path,
    runner: CommandRunner,
    lake_root: Path | None,
    label: str = CROSSVERIFY_LABEL,
    program_arguments: list[str] | None = None,
    log_dir: Path | None = None,
) -> InstallOutcome:
    """Install the producer's LaunchAgent — ``--at`` REQUIRED, probe-decided.

    ``at=None`` / blank REFUSES with :data:`AT_REQUIRED_MESSAGE` (no default
    fire time ships — the operator's settle probe decides it); a junk ``at``
    refuses with the ``parse_at`` message. Everything else DELEGATES to the
    nightly install flow (ONE write/load/verify law) with this producer's
    label, argv, ``crossverify-launchd`` log stem, and the baked
    ``BRUTEX_LAKE_ROOT`` when *lake_root* is given.
    """
    from brutex.nightly.schedule import parse_at

    if at is None or not at.strip():
        return InstallOutcome(
            ok=False,
            plist_path=plist_dir / f"{label}.plist",
            detail=AT_REQUIRED_MESSAGE,
        )
    try:
        hour, minute = parse_at(at)
    except ValueError as exc:
        return InstallOutcome(ok=False, plist_path=plist_dir / f"{label}.plist", detail=str(exc))
    env_vars: dict[str, str] = {}
    if lake_root is not None:
        env_vars["BRUTEX_LAKE_ROOT"] = str(lake_root)
    outcome = _install_launchagent(
        label=label,
        plist_dir=plist_dir,
        runner=runner,
        program_arguments=(
            resolve_program_arguments() if program_arguments is None else program_arguments
        ),
        hour=hour,
        minute=minute,
        env_vars=env_vars,
        log_dir=Path.home() / ".brutex" / "logs" if log_dir is None else log_dir,
        log_stem=CROSSVERIFY_LOG_STEM,
        next_step="brutex crossverify run --dry-run",
    )
    if outcome.ok:
        logger.info("crossverify.schedule.installed", label=label, at=f"{hour:02d}:{minute:02d}")
    return outcome


def uninstall_crossverify(
    *, plist_dir: Path, runner: CommandRunner, label: str = CROSSVERIFY_LABEL
) -> InstallOutcome:
    """Unload + remove the producer's LaunchAgent (the ONE nightly law)."""
    return _uninstall_launchagent(label=label, plist_dir=plist_dir, runner=runner)


def newest_journal_age(
    lake_root: Path, *, now: float = -1.0, now_fn: Callable[[], float] = time.time
) -> tuple[float, str] | None:
    """Age (s) + filename of the newest per-day journal under ``.crossverify/``.

    The journal (``<lake_root>/.crossverify/<YYYY-MM-DD>.json``) is the
    producer's durable run evidence — retained after a full publish as the
    done-record. Staging subtrees and non-journal strays are ignored by the
    strict filename grammar. ``None`` = no journal has ever been written.
    """
    current = now_fn() if now < 0 else now
    newest: tuple[float, str] | None = None
    journal_dir = lake_root / CROSSVERIFY_DIRNAME
    with contextlib.suppress(OSError):
        for child in journal_dir.iterdir():
            if not child.is_file() or _JOURNAL_NAME_RE.match(child.name) is None:
                continue
            with contextlib.suppress(OSError):
                mtime = child.stat().st_mtime
                if newest is None or mtime > newest[0]:
                    newest = (mtime, child.name)
    if newest is None:
        return None
    return max(0.0, current - newest[0]), newest[1]


def _default_is_export_day(d: date) -> bool:
    """The run.py step-B skip law: a session-less / Muhurat day writes NO journal."""
    from brutex.calendars.nse import SessionType, get_session

    session = get_session(d)
    return session is not None and session.session_type is not SessionType.MUHURAT


def seconds_since_trading_fire(
    *,
    hour: int,
    minute: int,
    now_local: datetime,
    is_export_day: Callable[[date], bool],
    lookback_days: int = _EXPECTED_FIRE_LOOKBACK_DAYS,
) -> float | None:
    """Seconds since the most recent ``HH:MM`` fire on an EXPORT day.

    Walks back from *now_local* over at most *lookback_days* calendar days,
    returning the elapsed seconds since the newest fire instant that (a) has
    already passed and (b) fell on a day the producer actually exports
    (trading, non-Muhurat). ``None`` = no such day inside the window — the
    caller reports honest UNKNOWN, never a guessed verdict.
    """
    for back in range(lookback_days + 1):
        candidate = (now_local - timedelta(days=back)).date()
        fire = datetime.combine(candidate, dt_time(hour, minute), tzinfo=now_local.tzinfo)
        if fire > now_local:
            continue  # today's fire has not happened yet
        if not is_export_day(candidate):
            continue  # skipped day — no journal is ever expected for it
        return (now_local - fire).total_seconds()
    return None


def deadman_verdict(
    *,
    lake_root: Path,
    schedule_hm: tuple[int, int] | None,
    now: float,
    now_local: datetime | None = None,
    grace_seconds: float = DEADMAN_GRACE_SECONDS,
    is_export_day: Callable[[date], bool] | None = None,
) -> DeadmanVerdict:
    """Did the scheduled cross-verify producer actually run? (row-20 shape.)

    ``schedule_hm=None`` → ``UNKNOWN`` (no schedule, staleness unjudgeable);
    a calendar failure inside the export-day predicate → ``UNKNOWN`` (never
    a guessed verdict); fresh journal (age ≤ seconds since the last expected
    TRADING-day fire + *grace_seconds*) → ``OK``; anything else — including
    no journal ever — → ``MISSED`` with the pmset remediation named. The
    verdict dataclass is the nightly's (ONE shape for both status surfaces).
    """
    predicate = _default_is_export_day if is_export_day is None else is_export_day
    age_entry = newest_journal_age(lake_root, now=now)
    newest_age = None if age_entry is None else age_entry[0]
    if schedule_hm is None:
        return DeadmanVerdict(
            verdict="UNKNOWN",
            detail="no schedule installed/parsed — staleness unjudgeable",
            newest_run_age_seconds=newest_age,
            allowed_age_seconds=None,
        )
    hour, minute = schedule_hm
    if now_local is None:
        try:
            local = datetime.fromtimestamp(now)
        except (OverflowError, OSError, ValueError):
            return DeadmanVerdict(
                verdict="UNKNOWN",
                detail=f"clock reading {now!r} not representable — staleness unjudgeable",
                newest_run_age_seconds=newest_age,
                allowed_age_seconds=None,
            )
    else:
        local = now_local
    try:
        since_fire = seconds_since_trading_fire(
            hour=hour, minute=minute, now_local=local, is_export_day=predicate
        )
    except Exception as exc:  # calendar predicate failure — never crash status
        return DeadmanVerdict(
            verdict="UNKNOWN",
            detail=f"export-day calendar unavailable ({type(exc).__name__}: {exc}) — "
            "staleness unjudgeable",
            newest_run_age_seconds=newest_age,
            allowed_age_seconds=None,
        )
    if since_fire is None:
        return DeadmanVerdict(
            verdict="UNKNOWN",
            detail=(
                f"no export (trading, non-Muhurat) day fire inside the last "
                f"{_EXPECTED_FIRE_LOOKBACK_DAYS} days — staleness unjudgeable"
            ),
            newest_run_age_seconds=newest_age,
            allowed_age_seconds=None,
        )
    allowed = since_fire + max(0.0, grace_seconds)
    if newest_age is None:
        return DeadmanVerdict(
            verdict="MISSED",
            detail=(
                f"schedule {hour:02d}:{minute:02d} installed but NO publish journal "
                "exists — if just installed this clears after the first trading-day "
                f"fire; else the Mac likely slept through it "
                f"({pmset_wake_command(hour, minute)})"
            ),
            newest_run_age_seconds=None,
            allowed_age_seconds=allowed,
        )
    if newest_age <= allowed:
        return DeadmanVerdict(
            verdict="OK",
            detail=f"newest journal is {int(newest_age)}s old (≤ allowed {int(allowed)}s)",
            newest_run_age_seconds=newest_age,
            allowed_age_seconds=allowed,
        )
    return DeadmanVerdict(
        verdict="MISSED",
        detail=(
            f"newest journal is {int(newest_age)}s old > allowed {int(allowed)}s "
            f"(last expected trading-day fire {hour:02d}:{minute:02d} + grace) — "
            f"the Mac likely slept through the trigger; scheduled wake: "
            f"{pmset_wake_command(hour, minute)}"
        ),
        newest_run_age_seconds=newest_age,
        allowed_age_seconds=allowed,
    )


@dataclass(frozen=True, slots=True)
class CrossverifyStatusReport:
    """The honest producer status snapshot (all lines even when nothing ran)."""

    plist_present: bool
    launchd_listed: bool
    next_fire: str | None
    newest_journal_line: str | None
    deadman: DeadmanVerdict

    def lines(self) -> list[str]:
        """Return the human-readable status lines for this report."""
        plist = "present" if self.plist_present else "ABSENT"
        listed = "listed" if self.launchd_listed else "NOT listed"
        fire = f" · next fire {self.next_fire}" if self.next_fire else ""
        return [
            f"schedule: plist {plist} · launchctl {listed}{fire}",
            f"newest journal: {self.newest_journal_line or 'none yet'}",
            f"dead-man: {self.deadman.verdict} — {self.deadman.detail}",
        ]


def status(
    *,
    lake_root: Path,
    plist_dir: Path,
    runner: CommandRunner,
    label: str = CROSSVERIFY_LABEL,
    now: Callable[[], float] = time.time,
    is_export_day: Callable[[date], bool] | None = None,
) -> CrossverifyStatusReport:
    """Assemble the honest status snapshot (read-only; never raises on absence)."""
    plist_path = plist_dir / f"{label}.plist"
    next_fire: str | None = None
    plist_present = plist_path.exists()
    if plist_present:
        match = _HOUR_MIN_RE.search(plist_path.read_text(encoding="utf-8"))
        if match is not None:
            next_fire = f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"
    rc, _out, _err = runner(["launchctl", "list", label])
    instant = now()
    age_entry = newest_journal_age(lake_root, now=instant)
    newest_journal_line = (
        None if age_entry is None else f"{age_entry[1]} · {int(age_entry[0])}s ago"
    )
    schedule_hm: tuple[int, int] | None = None
    if next_fire is not None:
        hour_text, minute_text = next_fire.split(":")
        schedule_hm = (int(hour_text), int(minute_text))
    verdict = deadman_verdict(
        lake_root=lake_root, schedule_hm=schedule_hm, now=instant, is_export_day=is_export_day
    )
    return CrossverifyStatusReport(
        plist_present=plist_present,
        launchd_listed=rc == 0,
        next_fire=next_fire,
        newest_journal_line=newest_journal_line,
        deadman=verdict,
    )
