"""R50 automated backup — sealed snapshots of the result-critical stores
(DEC-SWEEP-NIGHTLY-AUTOMATION-001).

Scope (fixed inventory, spec §5.2): namespace ``lake`` (rooted at the lake
root) covers ``window_ledger/`` (ledger.parquet + deltas), ``registry.duckdb``,
``instruments.duckdb``, ``catalog.duckdb`` and the lake-level ``audit/``;
namespace ``results`` (rooted at the results root — default ``<lake>/sweeps``,
correctly covered even when the operator points ``--results-root`` OUTSIDE the
lake) covers the whole sweeps tree EXCEPT ``progress/`` (transient, rewritten
every publish — restoring a stale "running" snapshot would false-positive the
liveness probe), ``.locks/`` (kernel-released flocks), the SHARED cross-run
``nightly/audit/`` chain (LIVE-APPENDED by the very command that snapshots —
``nightly run`` writes it between links, and ``nightly restore`` appends
RESTORE_START/RESTORE_DONE to it while restoring: an in-scope copy would
``os.replace`` the writer's open inode, silently rolling the forensic chain
back to snapshot time and losing the restore's own audit records to a dead
inode — Golden Rule #8; each run's tail is captured durably in its PRIOR
``nightly/<id>/bundle/``) and the CURRENT ``nightly/<id>/`` dir (actively
appended during this run; PRIOR nightly dirs are included). ``bars/`` is
EXCLUDED BY LAW — re-pullable; full-lake
protection stays the manual rsync/Time-Machine guidance in
``docs/runbooks/lake-backup-restore.md``.

Atomicity (spec §5.3): everything is staged under
``<backup_root>/<snapshot_id>.partial/`` — per-file tmp→``os.replace``, the
manifest written LAST (presence = the completeness sentinel, the
``tuple_complete.py`` written-strictly-last law reused conceptually), a FULL
pre-seal re-hash of every copied file, then ONE atomic same-filesystem
``os.rename`` seals the snapshot. **A non-``.partial`` snapshot dir is by
construction complete + verified.** Any failure leaves only ``.partial``
(never consumed by restore/retention; pruned after 24 h).

Hashes are ``hashlib.sha256`` over the DESTINATION bytes (the manifest can
never disagree with the sealed snapshot, even under an undetected source race
or a torn destination write). Integrity-only — NOT run identity — so the
BLAKE3 house rule (CLAUDE.md §2 Rule 4) does not apply; sha256 keeps the
module wheel-free stdlib.

Honest envelope (Golden Rule #10): backup is O(files + bytes) — never claimed
O(1); the verify pass is an O(bytes) second read (escape
``BRUTEX_NIGHTLY_BACKUP_VERIFY=0`` for huge stores, default ON).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.models import BruteXError
from brutex.nightly.probes import NightlyEnvError

logger = structlog.get_logger(__name__)

__all__ = [
    "BACKUP_SCHEMA_VERSION",
    "DELTAS_REL_PREFIX",
    "LEDGER_REL",
    "MANIFEST_NAME",
    "NIGHTLY_AUDIT_REL_PARTS",
    "NIGHTLY_BACKUP_KEEP_ENV",
    "NIGHTLY_BACKUP_ROOT_ENV",
    "NIGHTLY_BACKUP_VERIFY_ENV",
    "PARTIAL_SUFFIX",
    "BackupEntry",
    "BackupItem",
    "BackupManifest",
    "BackupManifestError",
    "BackupResult",
    "backup_inventory",
    "create_snapshot",
    "default_backup_root",
    "estimate_backup_bytes",
    "prune_snapshots",
    "resolve_backup_keep",
    "resolve_backup_verify",
    "sha256_file",
]

BACKUP_SCHEMA_VERSION: Final[int] = 1
MANIFEST_NAME: Final[str] = "manifest.json"
PARTIAL_SUFFIX: Final[str] = ".partial"

#: Combined ``<namespace>/<relpath>`` keys of the ledger family — the restore
#: ordering law's anchors (deltas cleared+copied SECOND-to-last, the ledger
#: parquet STRICTLY LAST: its stat token is every dashboard cache's
#: cross-process refresh signal, ``lake_revision`` / DEC-HARDENING-LEFTOVERS-001).
LEDGER_REL: Final[str] = "lake/window_ledger/ledger.parquet"
DELTAS_REL_PREFIX: Final[str] = "lake/window_ledger/deltas/"

NIGHTLY_BACKUP_ROOT_ENV: Final[str] = "BRUTEX_NIGHTLY_BACKUP_ROOT"
NIGHTLY_BACKUP_KEEP_ENV: Final[str] = "BRUTEX_NIGHTLY_BACKUP_KEEP"
NIGHTLY_BACKUP_VERIFY_ENV: Final[str] = "BRUTEX_NIGHTLY_BACKUP_VERIFY"

_DEFAULT_KEEP: Final[int] = 7

#: Age past which an orphaned ``.partial`` staging dir is garbage-collected.
#: (Paranoia guard — the nightly single-flight lock makes a CONCURRENT partial
#: impossible during a run; a fresh partial can only be a crash artifact.)
_PARTIAL_GC_SECONDS: Final[float] = 24 * 3600.0

#: The lake-namespace members (explicit list — never a lake_root walk, so
#: ``bars/`` and the dot-dirs are structurally unreachable).
_LAKE_MEMBERS: Final[tuple[str, ...]] = (
    "window_ledger",
    "registry.duckdb",
    "instruments.duckdb",
    "catalog.duckdb",
    "audit",
)

#: Results-namespace top-level dirs excluded from the walk.
_RESULTS_EXCLUDED_TOPLEVEL: Final[frozenset[str]] = frozenset({"progress", ".locks"})

#: Leading ``results``-relative path parts of the SHARED cross-run nightly
#: audit chain (``<results_root>/nightly/audit/`` — the
#: :func:`brutex.nightly.report.nightly_audit_path` law, mirror-pinned by
#: test). ALWAYS excluded from the backup scope AND skipped by restore
#: (:func:`brutex.nightly.restore.plan_restore` — the belt for legacy
#: snapshots sealed before this exclusion): the chain is live-appended by
#: the very command doing the snapshot/restore, so copying it back would
#: clobber the writer's open inode and lose RESTORE_START/RESTORE_DONE.
NIGHTLY_AUDIT_REL_PARTS: Final[tuple[str, ...]] = ("nightly", "audit")

_HASH_CHUNK: Final[int] = 1024 * 1024

#: Path-depth of ``nightly/<id>/...`` (the current-run exclusion test).
_NIGHTLY_PARTS_MIN: Final[int] = 2

#: Max mismatched relpaths NAMED in the pre-seal verify error (plus a count).
_VERIFY_NAME_CAP: Final[int] = 10


class BackupManifestError(BruteXError):
    """A snapshot manifest is missing / unparseable / structurally invalid."""


def default_backup_root(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the backup root: ``$BRUTEX_NIGHTLY_BACKUP_ROOT`` else ``~/.brutex/backups``.

    Honest caveat (runbook §4): the default may share the boot disk — NOT
    disaster protection; point it at a real second volume for that.
    """
    source = os.environ if env is None else env
    raw = source.get(NIGHTLY_BACKUP_ROOT_ENV, "")
    if raw.strip():
        return Path(raw).expanduser()
    return Path.home() / ".brutex" / "backups"


def resolve_backup_keep(env: Mapping[str, str] | None = None) -> int:
    """Resolve the retention count (default 7), fail-loud, ``>= 1``."""
    source = os.environ if env is None else env
    raw = source.get(NIGHTLY_BACKUP_KEEP_ENV, "")
    if not raw.strip():
        return _DEFAULT_KEEP
    try:
        value = int(raw)
    except ValueError as exc:
        raise NightlyEnvError(f"{NIGHTLY_BACKUP_KEEP_ENV} must be an integer, got {raw!r}") from exc
    if value < 1:
        raise NightlyEnvError(f"{NIGHTLY_BACKUP_KEEP_ENV} must be >= 1, got {raw!r}")
    return value


def resolve_backup_verify(env: Mapping[str, str] | None = None) -> bool:
    """Resolve the pre-seal full-verify switch: default ON; ``"0"`` is the escape.

    Anything other than ``""``/``"0"``/``"1"`` fails loud — a junk value must
    never silently disable an integrity pass (Golden Rule #1).
    """
    source = os.environ if env is None else env
    raw = source.get(NIGHTLY_BACKUP_VERIFY_ENV, "").strip()
    if raw in ("", "1"):
        return True
    if raw == "0":
        return False
    raise NightlyEnvError(f"{NIGHTLY_BACKUP_VERIFY_ENV} must be '0' or '1', got {raw!r}")


def sha256_file(path: Path) -> str:
    """Streamed sha256 hex digest of *path* (integrity-only — not run identity)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        while True:
            chunk = fp.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class BackupItem:
    """One source file selected for the snapshot."""

    namespace: str  # "lake" | "results"
    relpath: str  # POSIX, relative to its root
    source: Path
    restore_order: int  # 0 normal · 1 window_ledger/deltas/* · 2 ledger.parquet (LAST)


@dataclass(frozen=True, slots=True)
class BackupEntry:
    """One manifest row — hashes the DESTINATION bytes."""

    namespace: str
    relpath: str
    size_bytes: int
    sha256: str
    restore_order: int

    def to_dict(self) -> dict[str, object]:
        """Return this backup entry as a JSON-serializable dict."""
        return {
            "namespace": self.namespace,
            "relpath": self.relpath,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "restore_order": self.restore_order,
        }


def _restore_order(namespace: str, relpath: str) -> int:
    """Return the restore-ordering rank for a namespace/relpath pair."""
    key = f"{namespace}/{relpath}"
    if key == LEDGER_REL:
        return 2
    if key.startswith(DELTAS_REL_PREFIX):
        return 1
    return 0


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """The sealed snapshot's self-description (written STRICTLY LAST)."""

    schema_version: int
    snapshot_id: str
    nightly_id: str | None
    created_utc: float
    lake_root: str
    results_root: str
    entries: tuple[BackupEntry, ...]
    item_count: int
    total_bytes: int
    skipped_absent: tuple[str, ...]
    excluded_by_law: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return this backup manifest as a JSON-serializable dict."""
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "nightly_id": self.nightly_id,
            "created_utc": self.created_utc,
            "lake_root": self.lake_root,
            "results_root": self.results_root,
            "entries": [entry.to_dict() for entry in self.entries],
            "item_count": self.item_count,
            "total_bytes": self.total_bytes,
            "skipped_absent": list(self.skipped_absent),
            "excluded_by_law": list(self.excluded_by_law),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> BackupManifest:
        """Parse + validate a manifest dict; ANY junk → :class:`BackupManifestError`."""
        try:
            schema_version = data["schema_version"]
            if schema_version != BACKUP_SCHEMA_VERSION:
                raise BackupManifestError(
                    f"unsupported manifest schema_version {schema_version!r} "
                    f"(this build reads {BACKUP_SCHEMA_VERSION})"
                )
            raw_entries = data["entries"]
            if not isinstance(raw_entries, list):
                raise BackupManifestError("manifest 'entries' must be a list")
            entries: list[BackupEntry] = []
            for raw in raw_entries:
                if not isinstance(raw, Mapping):
                    raise BackupManifestError(f"manifest entry must be a mapping, got {raw!r}")
                entries.append(
                    BackupEntry(
                        namespace=str(raw["namespace"]),
                        relpath=str(raw["relpath"]),
                        size_bytes=int(str(raw["size_bytes"])),
                        sha256=str(raw["sha256"]),
                        restore_order=int(str(raw["restore_order"])),
                    )
                )
            nightly_id = data.get("nightly_id")
            return cls(
                schema_version=int(str(schema_version)),
                snapshot_id=str(data["snapshot_id"]),
                nightly_id=None if nightly_id is None else str(nightly_id),
                created_utc=float(str(data["created_utc"])),
                lake_root=str(data["lake_root"]),
                results_root=str(data["results_root"]),
                entries=tuple(entries),
                item_count=int(str(data["item_count"])),
                total_bytes=int(str(data["total_bytes"])),
                skipped_absent=tuple(str(x) for x in _as_list(data, "skipped_absent")),
                excluded_by_law=tuple(str(x) for x in _as_list(data, "excluded_by_law")),
            )
        except BackupManifestError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise BackupManifestError(f"manifest is structurally invalid: {exc!r}") from exc


def _as_list(data: Mapping[str, object], key: str) -> list[object]:
    """Return the value at ``key`` coerced to a list, empty when absent."""
    value = data[key]
    if not isinstance(value, list):
        raise BackupManifestError(f"manifest {key!r} must be a list, got {value!r}")
    return value


def backup_inventory(
    lake_root: Path,
    results_root: Path,
    *,
    exclude_nightly_id: str | None,
) -> tuple[list[BackupItem], list[str]]:
    """Enumerate the fixed backup scope (deterministic order, O(files) stat walk).

    Returns ``(items, skipped_absent)`` — an absent member (e.g. no
    ``catalog.duckdb`` yet) is an honest note, never an error.

    Raises
    ------
    BackupManifestError
        If *results_root* equals *lake_root* — a results walk over the whole
        lake would drag ``bars/`` into scope, which is excluded BY LAW.
    """
    if results_root.resolve() == lake_root.resolve():
        raise BackupManifestError(
            "results_root must not equal lake_root — the results walk would "
            "cover the whole lake (bars/ is excluded by law)"
        )

    items: list[BackupItem] = []
    skipped_absent: list[str] = []

    for member in _LAKE_MEMBERS:
        source = lake_root / member
        if not source.exists():
            skipped_absent.append(member)
            continue
        if source.is_file():
            items.append(
                BackupItem(
                    namespace="lake",
                    relpath=member,
                    source=source,
                    restore_order=_restore_order("lake", member),
                )
            )
            continue
        for file in sorted(p for p in source.rglob("*") if p.is_file()):
            relpath = file.relative_to(lake_root).as_posix()
            items.append(
                BackupItem(
                    namespace="lake",
                    relpath=relpath,
                    source=file,
                    restore_order=_restore_order("lake", relpath),
                )
            )

    if not results_root.exists():
        skipped_absent.append(f"results root {results_root}")
    else:
        for file in sorted(p for p in results_root.rglob("*") if p.is_file()):
            rel = file.relative_to(results_root)
            parts = rel.parts
            if parts[0] in _RESULTS_EXCLUDED_TOPLEVEL:
                continue
            if parts[: len(NIGHTLY_AUDIT_REL_PARTS)] == NIGHTLY_AUDIT_REL_PARTS:
                continue  # the LIVE cross-run nightly audit chain — never in scope
            if (
                exclude_nightly_id is not None
                and len(parts) >= _NIGHTLY_PARTS_MIN
                and parts[0] == "nightly"
                and parts[1] == exclude_nightly_id
            ):
                continue
            items.append(
                BackupItem(
                    namespace="results",
                    relpath=rel.as_posix(),
                    source=file,
                    restore_order=0,
                )
            )

    items.sort(key=lambda item: (item.namespace, item.relpath))
    return items, skipped_absent


def _excluded_by_law(exclude_nightly_id: str | None) -> tuple[str, ...]:
    """The fixed exclusion reasons recorded in every manifest."""
    excluded = [
        "bars/ (re-pullable)",
        ".locks/ (transient flocks)",
        ".rate/ (transient)",
        ".quarantine/ (heal forensics)",
        "results progress/ (transient)",
        "results nightly/audit/ (live cross-run nightly audit chain)",
    ]
    if exclude_nightly_id is not None:
        excluded.append(f"results nightly/{exclude_nightly_id}/ (live nightly dir)")
    return tuple(excluded)


def estimate_backup_bytes(items: Sequence[BackupItem]) -> int:
    """Total source bytes of *items* (feeds the DISK link's inventory sizing)."""
    total = 0
    for item in items:
        with contextlib.suppress(OSError):
            total += item.source.stat().st_size
    return total


@dataclass(frozen=True, slots=True)
class BackupResult:
    """What :func:`create_snapshot` returns on a sealed snapshot."""

    snapshot_id: str
    snapshot_path: Path
    file_count: int
    total_bytes: int
    pruned: tuple[str, ...]
    skipped_absent: tuple[str, ...]


def _default_copy(source: Path, dest: Path) -> None:
    """Copy ``source`` to ``dest`` using the default filesystem copier."""
    shutil.copyfile(source, dest)


def create_snapshot(
    *,
    lake_root: Path,
    results_root: Path,
    backup_root: Path,
    snapshot_id: str,
    nightly_id: str | None,
    copy_file: Callable[[Path, Path], None] | None = None,  # test seam
    keep: int | None = None,  # None → env NIGHTLY_BACKUP_KEEP_ENV → 7
    env: Mapping[str, str] | None = None,
    now: Callable[[], float] = time.time,
) -> BackupResult:
    """Create one SEALED snapshot at ``<backup_root>/<snapshot_id>`` (spec §5.3).

    Stages under ``<snapshot_id>.partial/``, copies per-file atomically,
    hashes the DESTINATION bytes into the manifest (written LAST), runs the
    FULL pre-seal re-verify (default ON), then seals with one atomic
    ``os.rename``. Retention runs AFTER a successful seal and never deletes
    the snapshot just written.

    Raises
    ------
    BackupManifestError
        Staging/final dir already exists (an id collision is a bug), or the
        pre-seal verify found a mismatch (the ``.partial`` is left behind for
        forensics, never sealed).
    OSError
        Any copy/hash/rename failure (incl. ENOSPC) propagates RAW — the
        BACKUP link wrapper records it FAILED and the chain breaks (a box
        that cannot back up must not mutate further).
    """
    verify = resolve_backup_verify(env)
    keep_resolved = resolve_backup_keep(env) if keep is None else keep
    if keep_resolved < 1:
        raise NightlyEnvError(f"keep must be >= 1, got {keep_resolved}")

    staging = backup_root / f"{snapshot_id}{PARTIAL_SUFFIX}"
    final = backup_root / snapshot_id
    if staging.exists() or final.exists():
        raise BackupManifestError(
            f"snapshot {snapshot_id} already exists at {backup_root} "
            "(staging or sealed) — the id embeds time+entropy, a collision is a bug"
        )
    staging.mkdir(parents=True)

    items, skipped_absent = backup_inventory(lake_root, results_root, exclude_nightly_id=nightly_id)
    copier = _default_copy if copy_file is None else copy_file

    entries: list[BackupEntry] = []
    total_bytes = 0
    for item in items:
        dest = staging / item.namespace / item.relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".brutex-copy-tmp")
        copier(item.source, tmp)
        os.replace(tmp, dest)
        digest = sha256_file(dest)
        size = dest.stat().st_size
        total_bytes += size
        entries.append(
            BackupEntry(
                namespace=item.namespace,
                relpath=item.relpath,
                size_bytes=size,
                sha256=digest,
                restore_order=item.restore_order,
            )
        )

    manifest = BackupManifest(
        schema_version=BACKUP_SCHEMA_VERSION,
        snapshot_id=snapshot_id,
        nightly_id=nightly_id,
        created_utc=now(),
        lake_root=str(lake_root),
        results_root=str(results_root),
        entries=tuple(entries),
        item_count=len(entries),
        total_bytes=total_bytes,
        skipped_absent=tuple(skipped_absent),
        excluded_by_law=_excluded_by_law(nightly_id),
    )
    _write_manifest(staging / MANIFEST_NAME, manifest)

    if verify:
        mismatches = _verify_staged(staging, manifest)
        if mismatches:
            raise BackupManifestError(
                f"pre-seal verify FAILED for {len(mismatches)} file(s): "
                + ", ".join(mismatches[:_VERIFY_NAME_CAP])
                + (" …" if len(mismatches) > _VERIFY_NAME_CAP else "")
                + f" — {staging} left UNSEALED"
            )
    else:
        logger.warning(
            "nightly.backup.verify_skipped",
            snapshot_id=snapshot_id,
            reason=f"{NIGHTLY_BACKUP_VERIFY_ENV}=0",
        )

    os.rename(staging, final)
    _fsync_dir(backup_root)
    logger.info(
        "nightly.backup.snapshot_sealed",
        snapshot_id=snapshot_id,
        files=len(entries),
        bytes=total_bytes,
        path=str(final),
    )

    pruned = prune_snapshots(backup_root, keep=keep_resolved, protect=final, now=now)
    return BackupResult(
        snapshot_id=snapshot_id,
        snapshot_path=final,
        file_count=len(entries),
        total_bytes=total_bytes,
        pruned=pruned,
        skipped_absent=tuple(skipped_absent),
    )


def _write_manifest(path: Path, manifest: BackupManifest) -> None:
    """Write the manifest atomically (guard-free: raw OSError propagates)."""
    payload = json.dumps(manifest.to_dict(), indent=2, sort_keys=False)

    def _write(tmp: Path) -> None:
        tmp.write_text(payload, encoding="utf-8")

    atomic_write_simple(path, _write)


def _verify_staged(staging: Path, manifest: BackupManifest) -> list[str]:
    """Re-stream + re-hash EVERY staged file against the manifest (pre-seal)."""
    mismatches: list[str] = []
    for entry in manifest.entries:
        dest = staging / entry.namespace / entry.relpath
        try:
            if dest.stat().st_size != entry.size_bytes or sha256_file(dest) != entry.sha256:
                mismatches.append(f"{entry.namespace}/{entry.relpath}")
        except OSError:
            mismatches.append(f"{entry.namespace}/{entry.relpath} (unreadable)")
    return mismatches


def _fsync_dir(path: Path) -> None:
    """Best-effort directory fsync so the seal rename is durable."""
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _manifest_created_utc(snapshot_dir: Path) -> float:
    """Snapshot age for retention ordering: manifest ``created_utc``, mtime fallback."""
    manifest_path = snapshot_dir / MANIFEST_NAME
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return float(str(data["created_utc"]))
    except (OSError, ValueError, KeyError, TypeError):
        with contextlib.suppress(OSError):
            return snapshot_dir.stat().st_mtime
    return 0.0


def prune_snapshots(
    backup_root: Path,
    *,
    keep: int,
    protect: Path | None,
    now: Callable[[], float] = time.time,
) -> tuple[str, ...]:
    """Retention: keep the newest *keep* SEALED snapshots; GC stale ``.partial`` dirs.

    Laws (spec §5.3 step 6): NEVER delete *protect* (the snapshot just
    written); NEVER delete the last remaining complete snapshot even if
    *keep* would; ``.partial`` dirs older than 24 h are garbage-collected
    (a fresh one can only be a crash artifact — the nightly lock forbids a
    concurrent run). Returns the deleted directory names.
    """
    if keep < 1:
        raise NightlyEnvError(f"keep must be >= 1, got {keep}")
    if not backup_root.exists():
        return ()

    deleted: list[str] = []
    sealed: list[Path] = []
    for child in sorted(backup_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.endswith(PARTIAL_SUFFIX):
            with contextlib.suppress(OSError):
                if now() - child.stat().st_mtime > _PARTIAL_GC_SECONDS:
                    shutil.rmtree(child)
                    deleted.append(child.name)
                    logger.info("nightly.backup.partial_pruned", name=child.name)
            continue
        if (child / MANIFEST_NAME).exists():
            sealed.append(child)

    # keep >= 1 (enforced above) always retains the NEWEST sealed snapshot —
    # that IS the never-delete-the-last-complete-snapshot law; *protect* is
    # additionally retained regardless of its manifest age (clock skew safe).
    sealed.sort(key=_manifest_created_utc, reverse=True)  # newest first
    for candidate in sealed[keep:]:
        if protect is not None and candidate.resolve() == protect.resolve():
            continue
        shutil.rmtree(candidate)
        deleted.append(candidate.name)
        logger.info("nightly.backup.snapshot_pruned", name=candidate.name)
    return tuple(deleted)
