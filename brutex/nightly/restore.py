"""R50 restore — verify-EVERYTHING-then-write (DEC-SWEEP-NIGHTLY-AUTOMATION-001).

The read side of the sealed-snapshot backup (:mod:`brutex.nightly.backup`),
mechanizing the ``docs/runbooks/lake-backup-restore.md`` law plus the
deltas-poison fix:

* **Phase A — verify everything:** every snapshot entry is re-hashed BEFORE a
  single destination byte is written (R42 "nothing touched" on the refusal
  path). A mismatch raises :class:`RestoreVerifyError` (CLI:
  ``EXIT_VERIFY_FINDINGS`` — the ``audit verify`` semantic precedent).
* **Phase C — deterministic copy order:** ``restore_order`` 0 files first
  (per-file tmp→fsync→``os.replace`` — each file either old or new, never
  torn), then the destination ``window_ledger/deltas/`` is CLEARED of every
  ``*.parquet`` NOT in the manifest before the manifest's deltas are copied
  (leftover post-snapshot delta files would fold latest-wins OVER the restored
  base ledger — the #1533 SQL QUALIFY law — silently resurrecting
  post-snapshot state), and ``ledger.parquet`` lands STRICTLY LAST (its stat
  token is every dashboard cache's cross-process refresh signal,
  ``lake_revision`` / DEC-HARDENING-LEFTOVERS-001 #1149 — caches must refresh
  only once the rest of the restored state is in place).
* **Phase D — post-copy re-hash** of every restored destination file
  (paranoia pass; a mismatch raises :class:`RestoreVerifyError`).

Locks are the CALLER's job (Phase B): the CLI acquires-and-HOLDs the nightly
+ account + lake pull locks and runs the sweep-liveness probe before invoking
this module — restore IS a lake writer.

Honest envelope (Golden Rule #10): restore is file-copy semantics, not a
transactional snapshot; a crash mid-Phase-C leaves a mixed old/new store set —
remediation is RE-RUNNING the restore (idempotent: Phase A re-verifies, Phase
C re-replaces). Manifest-extraneous files in OTHER namespaces (e.g. tuple
parquets written after the snapshot) are NOT deleted — only the deltas dir
gets the surgical clear, because only the ledger fold silently MERGES stale
state; a stray tuple parquet is inert without its sentinel pairing.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import structlog

from brutex.models import BruteXError
from brutex.nightly.backup import (
    MANIFEST_NAME,
    NIGHTLY_AUDIT_REL_PARTS,
    PARTIAL_SUFFIX,
    BackupEntry,
    BackupManifest,
    BackupManifestError,
    sha256_file,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "RestorePlan",
    "RestoreRefused",
    "RestoreResult",
    "RestoreVerifyError",
    "SnapshotVerification",
    "plan_restore",
    "restore_snapshot",
    "verify_snapshot",
]

#: The destination deltas dir (relative to the LAKE root) that gets the
#: surgical manifest-extraneous clear.
_DELTAS_DIR_REL: Final[str] = "window_ledger/deltas"

#: Mismatch names shown in a verify error (plus the total count).
_VERIFY_NAME_CAP: Final[int] = 10


class RestoreRefused(BruteXError):
    """Structural refusal BEFORE any verification/writing (nothing touched).

    ``.partial`` snapshot dir, missing/unparseable manifest, wrong schema —
    the CLI maps this to ``EXIT_USER_ERROR``.
    """


class RestoreVerifyError(BruteXError):
    """Hash/size verification found corruption (CLI: ``EXIT_VERIFY_FINDINGS``)."""

    def __init__(self, message: str, mismatches: tuple[str, ...]) -> None:
        super().__init__(message)
        self.mismatches = mismatches


@dataclass(frozen=True, slots=True)
class SnapshotVerification:
    """Phase-A verdict over one sealed snapshot."""

    ok: bool
    checked: int
    mismatches: tuple[str, ...]  # "<namespace>/<relpath>" with a reason suffix
    manifest: BackupManifest | None


def _load_manifest(snapshot_dir: Path) -> BackupManifest:
    """Parse the snapshot manifest, refusing structurally-broken snapshots."""
    if snapshot_dir.name.endswith(PARTIAL_SUFFIX):
        raise RestoreRefused(
            f"{snapshot_dir} is an UNSEALED .partial staging dir — never restorable "
            "(only sealed snapshots carry a verified manifest)"
        )
    manifest_path = snapshot_dir / MANIFEST_NAME
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RestoreRefused(f"cannot read snapshot manifest {manifest_path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RestoreRefused(f"snapshot manifest {manifest_path} is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RestoreRefused(f"snapshot manifest {manifest_path} must be a JSON object")
    try:
        return BackupManifest.from_dict(data)
    except BackupManifestError as exc:
        raise RestoreRefused(f"snapshot manifest {manifest_path} is invalid: {exc}") from exc


def verify_snapshot(snapshot_dir: Path) -> SnapshotVerification:
    """Re-hash EVERY snapshot entry against the manifest (Phase A; read-only).

    Raises
    ------
    RestoreRefused
        For structural problems (``.partial`` dir, missing/junk manifest) —
        those are refusals, not findings.
    """
    manifest = _load_manifest(snapshot_dir)
    mismatches: list[str] = []
    for entry in manifest.entries:
        source = snapshot_dir / entry.namespace / entry.relpath
        key = f"{entry.namespace}/{entry.relpath}"
        try:
            if source.stat().st_size != entry.size_bytes:
                mismatches.append(f"{key} (size)")
            elif sha256_file(source) != entry.sha256:
                mismatches.append(f"{key} (sha256)")
        except OSError:
            mismatches.append(f"{key} (missing)")
    return SnapshotVerification(
        ok=not mismatches,
        checked=len(manifest.entries),
        mismatches=tuple(mismatches),
        manifest=manifest,
    )


@dataclass(frozen=True, slots=True)
class RestorePlan:
    """The ordered restore plan (printable for the ``--yes`` gate)."""

    snapshot: Path
    manifest: BackupManifest
    items: tuple[BackupEntry, ...]  # sorted (restore_order, namespace, relpath) — ledger LAST
    lake_root: Path
    results_root: Path
    skipped_live_audit: tuple[str, ...] = ()  # legacy-manifest nightly/audit/ entries, NEVER copied


def _is_live_audit_entry(entry: BackupEntry) -> bool:
    """True for a ``results`` entry under the shared live ``nightly/audit/`` chain."""
    parts = tuple(entry.relpath.split("/"))
    return (
        entry.namespace == "results"
        and parts[: len(NIGHTLY_AUDIT_REL_PARTS)] == NIGHTLY_AUDIT_REL_PARTS
    )


def plan_restore(snapshot_dir: Path, *, lake_root: Path, results_root: Path) -> RestorePlan:
    """Build the deterministic restore plan (no verification, no writes).

    A LEGACY snapshot (sealed before the ``nightly/audit/`` scope exclusion)
    may carry the shared cross-run nightly audit chain; those entries are
    SKIPPED here — restore itself appends RESTORE_START/RESTORE_DONE to that
    very chain, and copying the snapshot copy over it would ``os.replace``
    the writer's open inode, silently rolling the forensic chain back to
    snapshot time and losing the restore's own audit records (Golden Rule #8).
    """
    manifest = _load_manifest(snapshot_dir)
    live_audit = tuple(
        f"{e.namespace}/{e.relpath}" for e in manifest.entries if _is_live_audit_entry(e)
    )
    if live_audit:
        logger.warning(
            "nightly.restore.live_audit_skipped",
            snapshot=str(snapshot_dir),
            skipped=list(live_audit),
        )
    items = tuple(
        sorted(
            (e for e in manifest.entries if not _is_live_audit_entry(e)),
            key=lambda e: (e.restore_order, e.namespace, e.relpath),
        )
    )
    return RestorePlan(
        snapshot=snapshot_dir,
        manifest=manifest,
        items=items,
        lake_root=lake_root,
        results_root=results_root,
        skipped_live_audit=live_audit,
    )


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """What a completed restore reports."""

    restored: int
    total_bytes: int
    deltas_cleared: int
    ledger_restored_last: bool


def _default_copy(source: Path, dest: Path) -> None:
    import shutil

    shutil.copyfile(source, dest)


def _fsync_file(path: Path) -> None:
    """Best-effort fsync so the per-file replace is durable."""
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _mismatch_message(prefix: str, mismatches: tuple[str, ...]) -> str:
    named = ", ".join(mismatches[:_VERIFY_NAME_CAP])
    more = (
        f" (+{len(mismatches) - _VERIFY_NAME_CAP} more)"
        if len(mismatches) > _VERIFY_NAME_CAP
        else ""
    )
    return f"{prefix}: {len(mismatches)} file(s) — {named}{more}"


def restore_snapshot(
    plan: RestorePlan,
    *,
    copy_file: Callable[[Path, Path], None] | None = None,
) -> RestoreResult:
    """Execute *plan*: Phase A verify → Phase C ordered copy → Phase D re-hash.

    Raises
    ------
    RestoreVerifyError
        Phase A found corruption (NOTHING written — destination byte-untouched)
        or Phase D found a post-copy mismatch.
    OSError
        A raw copy failure mid-Phase-C (CLI: ``EXIT_INTERNAL_ERROR``);
        re-running the restore converges (idempotent re-replace).
    """
    # ── Phase A — verify EVERYTHING before writing a single byte ─────────
    verification = verify_snapshot(plan.snapshot)
    if not verification.ok:
        raise RestoreVerifyError(
            _mismatch_message(
                f"snapshot {plan.snapshot.name} FAILED verification; nothing restored",
                verification.mismatches,
            ),
            verification.mismatches,
        )

    copier = _default_copy if copy_file is None else copy_file
    roots = {"lake": plan.lake_root, "results": plan.results_root}

    # ── Phase C — deterministic ordered copy ─────────────────────────────
    restored = 0
    total_bytes = 0
    deltas_cleared = 0
    deltas_cleared_done = False
    last_key: str | None = None
    ledger_entries = [e for e in plan.items if e.restore_order >= 1]

    for entry in plan.items:
        if entry.restore_order >= 1 and not deltas_cleared_done:
            deltas_cleared = _clear_extraneous_deltas(plan)
            deltas_cleared_done = True
        root = roots.get(entry.namespace)
        if root is None:
            raise RestoreRefused(
                f"manifest entry {entry.relpath!r} has unknown namespace "
                f"{entry.namespace!r} — refusing to guess a destination"
            )
        dest = root / entry.relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".brutex-restore-tmp")
        copier(plan.snapshot / entry.namespace / entry.relpath, tmp)
        _fsync_file(tmp)
        os.replace(tmp, dest)
        restored += 1
        total_bytes += entry.size_bytes
        last_key = f"{entry.namespace}/{entry.relpath}"

    # The ledger family (when present in the snapshot) must have landed LAST.
    ledger_restored_last = bool(ledger_entries) and last_key == "lake/window_ledger/ledger.parquet"

    # ── Phase D — post-copy re-hash (paranoia pass) ──────────────────────
    post_mismatches: list[str] = []
    for entry in plan.items:
        dest = roots[entry.namespace] / entry.relpath
        try:
            if dest.stat().st_size != entry.size_bytes or sha256_file(dest) != entry.sha256:
                post_mismatches.append(f"{entry.namespace}/{entry.relpath}")
        except OSError:
            post_mismatches.append(f"{entry.namespace}/{entry.relpath} (missing)")
    if post_mismatches:
        raise RestoreVerifyError(
            _mismatch_message(
                "post-restore re-hash FAILED (destination drifted mid-restore?)",
                tuple(post_mismatches),
            ),
            tuple(post_mismatches),
        )

    logger.info(
        "nightly.restore.done",
        snapshot=str(plan.snapshot),
        restored=restored,
        bytes=total_bytes,
        deltas_cleared=deltas_cleared,
        ledger_restored_last=ledger_restored_last,
    )
    return RestoreResult(
        restored=restored,
        total_bytes=total_bytes,
        deltas_cleared=deltas_cleared,
        ledger_restored_last=ledger_restored_last,
    )


def _clear_extraneous_deltas(plan: RestorePlan) -> int:
    """Delete destination ``window_ledger/deltas/*.parquet`` NOT in the manifest.

    The deltas-poison fix: a post-snapshot delta file left in place would fold
    latest-wins OVER the restored base ledger (the #1533 QUALIFY law) and
    silently resurrect post-snapshot state. Runs ONLY when the snapshot
    carries ledger-family state (a snapshot with no ledger must not strip a
    live lake's deltas). Surgical: ONLY this directory, ONLY ``*.parquet``.
    """
    deltas_dir = plan.lake_root / _DELTAS_DIR_REL
    if not deltas_dir.is_dir():
        return 0
    keep = {Path(entry.relpath).name for entry in plan.manifest.entries if entry.restore_order == 1}
    cleared = 0
    for file in sorted(deltas_dir.glob("*.parquet")):
        if file.name not in keep:
            file.unlink()
            cleared += 1
            logger.info("nightly.restore.delta_cleared", name=file.name)
    return cleared
