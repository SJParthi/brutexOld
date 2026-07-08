"""Nightly report, diagnostic bundle + hash-chained audit helpers
(DEC-SWEEP-NIGHTLY-AUTOMATION-001).

One R43 id (:func:`mint_nightly_id`) threads the report, the audit chain, the
bundle, the Telegram summary and the snapshot. Artifacts live under the
nightly-OWNED ``<results_root>/nightly/`` (never P2's ``runs/``):

* ``nightly/<id>/report.json`` — the frozen ``brutex-nightly-report-v1``
  contract (the future dashboard card reads it).
* ``nightly/<id>/bundle/`` — the LIGHTWEIGHT R40-shaped diagnostic bundle
  (report copy · this run's nightly-audit tail · the sweep-audit tail ·
  progress snapshot copy · allowlisted env · versions · per-link timings).
  P2's full R40 bundle is P2's zone; when it lands the report points at it via
  the run-manifest probe.
* ``nightly/audit/nightly.jsonl`` — ONE nightly-owned hash-chained audit
  (:class:`~brutex.engine.sweep_audit.SweepAuditWriter`, ``resume=True`` —
  torn-tail truncation built in), shared by ``nightly run`` and the one-shot
  ``backup``/``restore`` subcommands.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess  # nosec B404 — fixed constant argv, no shell (see run() below)
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from brutex import __version__
from brutex.nightly.chain import LinkStatus, NightlyReport, first_breaker

if TYPE_CHECKING:
    from brutex.engine.sweep_audit import SweepAuditWriter

logger = structlog.get_logger(__name__)

__all__ = [
    "NIGHTLY_AUDIT_FILENAME",
    "NIGHTLY_DIRNAME",
    "REPORT_FILENAME",
    "mint_nightly_id",
    "nightly_audit_path",
    "nightly_dir",
    "open_nightly_audit",
    "summary_lines",
    "write_bundle",
    "write_report",
]

NIGHTLY_DIRNAME: Final[str] = "nightly"  # <results_root>/nightly/ — nightly-OWNED
NIGHTLY_AUDIT_FILENAME: Final[str] = "nightly.jsonl"
REPORT_FILENAME: Final[str] = "report.json"

#: Non-secret env keys copied into the bundle's ``env.json``. NEVER Telegram
#: token/chat-id or any credential material (AWS-only creds never live in env).
_ENV_ALLOWLIST_EXACT: Final[frozenset[str]] = frozenset(
    {"BRUTEX_LAKE_ROOT", "BRUTEX_PIPELINE_STRICT_GATE", "BRUTEX_SWEEP_WORKERS"}
)
_ENV_ALLOWLIST_PREFIX: Final[str] = "BRUTEX_NIGHTLY_"

#: Sweep-audit tail lines copied into the bundle (bounded — never the whole chain).
_SWEEP_TAIL_LINES: Final[int] = 200


def mint_nightly_id(
    now: Callable[[], float] = time.time,
    entropy: Callable[[int], bytes] = os.urandom,
) -> str:
    """Mint the one R43 nightly id: ``nightly-YYYYMMDDTHHMMSSZ-<6 hex>``.

    UTC-stamped + 3 entropy bytes ⇒ collision-safe and filesystem-safe (it is
    the snapshot id, the report dir name, and the audit subject).
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now()))
    return f"nightly-{stamp}-{entropy(3).hex()}"


def nightly_dir(results_root: Path, nightly_id: str) -> Path:
    """``<results_root>/nightly/<nightly_id>`` (never created here)."""
    return results_root / NIGHTLY_DIRNAME / nightly_id


def nightly_audit_path(results_root: Path) -> Path:
    """``<results_root>/nightly/audit/nightly.jsonl`` — the ONE nightly chain."""
    return results_root / NIGHTLY_DIRNAME / "audit" / NIGHTLY_AUDIT_FILENAME


def open_nightly_audit(results_root: Path) -> SweepAuditWriter:
    """Open (resuming) the hash-chained nightly audit writer.

    ``resume=path.exists()`` — a fresh lake starts a new chain; an existing
    one continues from its last committed entry (torn-tail truncation is the
    writer's own contract, DEC-AUDIT-TORN-TAIL-001).
    """
    from brutex.engine.sweep_audit import SweepAuditWriter

    path = nightly_audit_path(results_root)
    return SweepAuditWriter.open(path, actor="nightly", resume=path.exists())


def write_report(results_root: Path, report: NightlyReport) -> Path:
    """Atomically write ``report.json`` for *report*; returns its path."""
    from brutex.data.atomic_io import atomic_write_simple

    dest = nightly_dir(results_root, report.nightly_id) / REPORT_FILENAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report.to_dict(), indent=2)

    def _write(tmp: Path) -> None:
        tmp.write_text(payload, encoding="utf-8")

    atomic_write_simple(dest, _write)
    logger.info("nightly.report.written", path=str(dest), nightly_id=report.nightly_id)
    return dest


def _code_commit_sha(runner: Callable[..., str] | None = None) -> str:
    """Best-effort ``git rev-parse HEAD`` (the run-identity mirror); honest fallback."""
    if runner is not None:
        with contextlib.suppress(Exception):
            return runner()
        return f"brutex-{__version__}"
    try:
        completed = subprocess.run(  # nosec B603 B607 — fixed constant argv, no shell
            ["git", "rev-parse", "HEAD"],  # noqa: S607 — fixed constant argv, no shell/user input
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return f"brutex-{__version__}"
    sha = completed.stdout.strip()
    if completed.returncode == 0 and sha:
        return sha
    return f"brutex-{__version__}"


def _allowlisted_env(env: Mapping[str, str]) -> dict[str, str]:
    """The redaction law: exact allowlist + the BRUTEX_NIGHTLY_ prefix, nothing else."""
    return {
        key: value
        for key, value in sorted(env.items())
        if key in _ENV_ALLOWLIST_EXACT or key.startswith(_ENV_ALLOWLIST_PREFIX)
    }


def write_bundle(
    results_root: Path,
    report: NightlyReport,
    *,
    sweep_audit_path: Path,
    nightly_audit_path: Path,
    progress_path: Path,
    env: Mapping[str, str],
    code_commit: Callable[[], str] | None = None,
) -> Path:
    """Write the lightweight diagnostic bundle (best-effort per member, atomic files).

    ``<results_root>/nightly/<id>/bundle/``: ``report.json`` copy · this run's
    nightly-audit tail (the lines carrying this ``nightly_id``) · the
    sweep-audit tail (last 200 lines) · a progress-snapshot copy when present ·
    ``env.json`` (ALLOWLISTED non-secret keys only) · ``versions.json`` ·
    ``timings.json`` (per-link start/finish). An unreadable member is SKIPPED
    (the bundle is forensics, never a chain-breaker); the bundle dir always
    lands with at least the report + env + versions + timings + README.
    """
    from brutex.data.atomic_io import atomic_write_simple

    bundle = nightly_dir(results_root, report.nightly_id) / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)

    def _write_text(dest: Path, text: str) -> None:
        atomic_write_simple(dest, lambda tmp: tmp.write_text(text, encoding="utf-8"))

    _write_text(bundle / REPORT_FILENAME, json.dumps(report.to_dict(), indent=2))

    with contextlib.suppress(OSError):
        lines = nightly_audit_path.read_text(encoding="utf-8").splitlines()
        own = [line for line in lines if report.nightly_id in line]
        _write_text(bundle / "nightly-audit-tail.jsonl", "\n".join(own) + ("\n" if own else ""))

    with contextlib.suppress(OSError):
        sweep_lines = sweep_audit_path.read_text(encoding="utf-8").splitlines()
        tail = sweep_lines[-_SWEEP_TAIL_LINES:]
        _write_text(bundle / "sweep-audit-tail.jsonl", "\n".join(tail) + ("\n" if tail else ""))

    with contextlib.suppress(OSError):
        _write_text(bundle / "sweep-progress.json", progress_path.read_text(encoding="utf-8"))

    _write_text(bundle / "env.json", json.dumps(_allowlisted_env(env), indent=2))
    _write_text(
        bundle / "versions.json",
        json.dumps(
            {
                "brutex": __version__,
                "python": sys.version,
                "code_commit_sha": _code_commit_sha(code_commit),
            },
            indent=2,
        ),
    )
    _write_text(
        bundle / "timings.json",
        json.dumps(
            {
                outcome.link.value: {
                    "started_utc": outcome.started_utc,
                    "ended_utc": outcome.ended_utc,
                    "status": outcome.status.value,
                }
                for outcome in report.links
            },
            indent=2,
        ),
    )
    _write_text(
        bundle / "README.txt",
        "BruteX nightly LIGHTWEIGHT diagnostic bundle "
        "(DEC-SWEEP-NIGHTLY-AUTOMATION-001).\n"
        "P2's full R40 flight-recorder bundle is P2's zone; when it lands, the\n"
        "report points at it via the run-manifest probe.\n",
    )
    logger.info("nightly.bundle.written", path=str(bundle), nightly_id=report.nightly_id)
    return bundle


def _count_text(value: int | None) -> str:
    """Unknown counts render ``"?"`` — NEVER a fabricated 0 (Golden Rule #1)."""
    return "?" if value is None else str(value)


def _verdict(report: NightlyReport) -> str:
    """Derive the terminal verdict label for the nightly report."""
    breaker = first_breaker(report.links)
    if breaker is not None:
        return "REFUSED" if breaker.status is LinkStatus.REFUSED else "FAILED"
    if report.suspect:
        return "SUSPECT"
    return "OK"


def summary_lines(report: NightlyReport) -> tuple[list[str], str]:
    """The pure Telegram body builder: ``(lines, verdict)`` (testable w/o a notifier).

    ``verdict`` ∈ {"OK", "SUSPECT", "REFUSED", "FAILED"}; the lines carry the
    chain-order per-link verdicts + the honest counts (``?`` for unknown) and
    the ``--skip-backup`` nag when the operator opted out.
    """
    verdict = _verdict(report)
    duration = max(0.0, report.ended_utc - report.started_utc)
    hours, rem = divmod(int(duration), 3600)
    minutes = rem // 60
    breaker = first_breaker(report.links)
    headline = {
        "OK": "✅ OK",
        "SUSPECT": "⚠️ SUSPECT",
        "REFUSED": f"⛔ REFUSED at {breaker.link.value}" if breaker else "⛔ REFUSED",
        "FAILED": f"❌ FAILED at {breaker.link.value}" if breaker else "❌ FAILED",
    }[verdict]
    lines = [f"🌙 Nightly {report.nightly_id} — {headline} in {hours}:{minutes:02d}."]

    parts: list[str] = []
    for outcome in report.links:
        status = outcome.status.value
        if outcome.status is LinkStatus.BLOCKED and outcome.blocked_by is not None:
            status = f"blocked({outcome.blocked_by.value})"
        counts = report.counts
        if outcome.link.value == "backup" and outcome.status is not LinkStatus.BLOCKED:
            status += f" ({_count_text(counts.get('backup_files'))} files)"
        elif outcome.link.value == "pull" and outcome.status is not LinkStatus.BLOCKED:
            status += f" ({_count_text(counts.get('pull_bars'))} bars)"
        elif outcome.link.value == "prebuild" and outcome.status is not LinkStatus.BLOCKED:
            status += f" ({_count_text(counts.get('prebuild_built'))} built)"
        elif outcome.link.value == "sweep" and outcome.status is not LinkStatus.BLOCKED:
            status += (
                f" ({_count_text(counts.get('sweep_run'))} run · "
                f"{_count_text(counts.get('sweep_skipped_cache'))} skipped-cache · "
                f"{_count_text(counts.get('sweep_failed'))} failed)"
            )
        parts.append(f"{outcome.link.value} {status}")
    lines.append(" · ".join(parts))

    for wire in report.tripwires:
        lines.append(f"⚠️ tripwire {wire.name}: {wire.detail}")
    if (
        any(o.link.value == "backup" and o.status is LinkStatus.SKIPPED for o in report.links)
        and not report.dry_run
    ):
        lines.append("⚠️ backup SKIPPED by operator flag — stores unprotected tonight.")
    for note in report.honesty_notes:
        lines.append(f"ℹ️ {note}")
    return lines, verdict
