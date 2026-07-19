"""The byte-verified restore drill (``brutex backup restore-drill``) — §8 of the design.

The drill proves the restore MACHINERY end-to-end (auth → snapshot select →
restore → bytes intact) with a KNOWN-bytes proof plus a stratified sample:

1. **Canary** — 1 MiB of deterministic bytes (sha256-expanded from the fixed
   seed, byte-stable forever) is backed up as its OWN tiny snapshot
   (``brutex-drill-canary`` tag — outside the ``--tag brutex`` forget/deep
   universe), restored, and byte-compared. This closes write → encrypt →
   upload → download → decrypt → bytes with a KNOWN expected value,
   independent of live-lake drift (F20).
2. **Stratified sample** — the drill restores from the snapshot recorded in
   ``state.json`` (the last VERIFIED success), NEVER ``latest`` — a monthly
   drill can never race tonight's in-flight backup. Files are grouped by
   top-level category and ranked by ``sha256(snapshot_id + path)`` — the rng
   IS the snapshot-seeded hash, so the sample is deterministic + reproducible
   per snapshot, and every category with members is sampled every drill.
3. **Byte verification law** — restored bytes vs LIVE bytes; a live file that
   moved on since the snapshot is honestly ``skipped_changed`` (never a false
   alarm on a live lake); an unchanged-live byte mismatch is CORRUPTION; a
   restored ``audit/`` file must additionally be a byte-PREFIX of the live
   file (hash-chained JSONL is append-only — Golden Rule #8) REGARDLESS of
   mtime.

Subset honesty (Golden Rule #10): the drill proves the restore machinery
end-to-end and spot-checks integrity; it does NOT prove all 8 TB restores.
The restored temp tree is KEPT on failure for forensics, removed on success.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final

import structlog

from brutex.backup.errors import (
    EXIT_BUSY,
    EXIT_CONFIG,
    EXIT_CORRUPTION,
    EXIT_OK,
    EXIT_RESTIC,
    BackupBusyError,
    BackupError,
    BackupStateError,
)
from brutex.backup.plan import backup_singleflight_lock
from brutex.backup.restic_runner import (
    CANARY_TAG,
    default_runner_factory,
    parse_backup_summary_snapshot_id,
    parse_ls_file_paths,
    parse_ls_snapshot_time,
)
from brutex.backup.state import canary_dir, drill_tmp_dir, load_state, state_path
from brutex.config.paths import brutex_home, resolve_lake_root
from brutex.data.guarded_read import open_regular_binary, read_regular_bytes
from brutex.notifications.sweep_alerts import SweepAlerter

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from brutex.backup.deadman import Clock
    from brutex.backup.restic_runner import ResticRunner
    from brutex.config.backup_env import BackupEnvConfig

_LOG = structlog.get_logger(__name__)

#: The canary's fixed expansion seed — byte-stable forever (never change it:
#: the canary's value is that its expected bytes are KNOWN across years).
CANARY_SEED: Final[bytes] = b"brutex-backup-canary-v1"

#: The canary payload size (1 MiB — bounded egress per drill).
CANARY_SIZE_BYTES: Final[int] = 1024 * 1024

_SHA256_BLOCK: Final[int] = 32
_READ_CHUNK: Final[int] = 1 << 20


def canary_bytes() -> bytes:
    """Return the deterministic 1 MiB canary payload (sha256-counter expansion)."""
    blocks: list[bytes] = []
    for counter in range(CANARY_SIZE_BYTES // _SHA256_BLOCK):
        blocks.append(hashlib.sha256(CANARY_SEED + counter.to_bytes(8, "big")).digest())
    return b"".join(blocks)


def _file_sha256(path: Path) -> str:
    """Stream-sha256 of *path* (bounded memory)."""
    digest = hashlib.sha256()
    with open_regular_binary(path) as handle:
        while True:
            chunk = handle.read(_READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _is_prefix_of(restored: Path, live: Path) -> bool:
    """True iff *restored*'s bytes are a byte-PREFIX of *live*'s (audit law)."""
    with open_regular_binary(restored) as r_handle, open_regular_binary(live) as l_handle:
        while True:
            r_chunk = r_handle.read(_READ_CHUNK)
            if not r_chunk:
                return True
            l_chunk = l_handle.read(len(r_chunk))
            if l_chunk != r_chunk:
                return False


@dataclass(slots=True)
class _DrillTally:
    """Per-category verification counters for the final report."""

    verified: dict[str, int] = field(default_factory=dict)
    skipped_changed: dict[str, int] = field(default_factory=dict)
    corrupt: dict[str, int] = field(default_factory=dict)
    corrupt_paths: list[str] = field(default_factory=list)
    restore_errors: int = 0

    def bump(self, bucket: dict[str, int], category: str) -> None:
        """Increment *category* in *bucket*."""
        bucket[category] = bucket.get(category, 0) + 1


def _categorize(path: str, *, lake_root: Path, home_audit: Path) -> str:
    """Group a snapshot path by its top-level restore category (§8 step 4)."""
    pure = PurePosixPath(path)
    lake = PurePosixPath(str(lake_root))
    audit = PurePosixPath(str(home_audit))
    if pure.is_relative_to(audit):
        return "audit"
    if pure.is_relative_to(lake):
        relative = pure.relative_to(lake)
        if len(relative.parts) == 1:
            return "duckdb" if relative.suffix == ".duckdb" else "lake-root"
        return str(relative.parts[0])
    return "other"


def _stratified_sample(
    paths: tuple[str, ...],
    *,
    snapshot_id: str,
    sample: int,
    lake_root: Path,
    home_audit: Path,
) -> tuple[str, ...]:
    """Deterministic round-robin sample across categories (§8 step 4).

    Within each category paths are ranked by ``sha256(snapshot_id + path)``
    (the snapshot-seeded hash IS the rng); the quota is taken round-robin so
    every category with members is sampled every drill.
    """
    by_category: dict[str, list[str]] = {}
    for path in paths:
        category = _categorize(path, lake_root=lake_root, home_audit=home_audit)
        by_category.setdefault(category, []).append(path)
    for members in by_category.values():
        members.sort(key=lambda p: hashlib.sha256((snapshot_id + p).encode("utf-8")).hexdigest())
    chosen: list[str] = []
    cursor = 0
    while len(chosen) < sample:
        took_any = False
        for category in sorted(by_category):
            members = by_category[category]
            if cursor < len(members) and len(chosen) < sample:
                chosen.append(members[cursor])
                took_any = True
        if not took_any:
            break
        cursor += 1
    return tuple(chosen)


def _verify_sampled_file(
    path: str,
    *,
    target: Path,
    snapshot_time: datetime | None,
    category: str,
    tally: _DrillTally,
) -> None:
    """Apply the §8 step-6 byte-verification law to ONE sampled file."""
    restored = target / path.lstrip("/")
    live = Path(path)
    if not restored.exists():
        _LOG.error("backup.drill.restore_missing", path=path)
        tally.restore_errors += 1
        return
    if not live.exists():
        tally.bump(tally.skipped_changed, category)
        return
    try:
        if category == "audit":
            # Hash-chained JSONL is append-only — the restored copy must be a
            # byte-PREFIX of the live file REGARDLESS of mtime (Golden Rule #8).
            if _is_prefix_of(restored, live):
                tally.bump(tally.verified, category)
            else:
                _LOG.error("backup.drill.mismatch", path=path, detail="audit non-prefix")
                tally.bump(tally.corrupt, category)
                tally.corrupt_paths.append(path)
            return
        if _file_sha256(restored) == _file_sha256(live):
            tally.bump(tally.verified, category)
            return
        snapshot_epoch_ns = (
            int(snapshot_time.timestamp() * 1_000_000_000)
            if snapshot_time is not None
            else restored.stat().st_mtime_ns
        )
        if live.stat().st_mtime_ns > snapshot_epoch_ns:
            tally.bump(tally.skipped_changed, category)
            return
    except FileNotFoundError:
        # The live file vanished BETWEEN exists() and the read — a lake-heal
        # DELETE-and-rebuild / prebuild rewrite racing the drill on a LIVE
        # lake (DEC-LAKE-HEAL-001). The honest skipped_changed verdict,
        # never a false alarm and never a raw traceback (§8 law).
        _LOG.info("backup.drill.live_vanished", path=path)
        tally.bump(tally.skipped_changed, category)
        return
    _LOG.error("backup.drill.mismatch", path=path, detail="bytes differ, live not newer")
    tally.bump(tally.corrupt, category)
    tally.corrupt_paths.append(path)


def _run_canary(cfg: BackupEnvConfig, runner: ResticRunner, target_root: Path) -> int | None:
    """§8 step 3 — the known-bytes canary roundtrip; ``None`` = verified."""
    canary_home = canary_dir(cfg)
    canary_home.mkdir(parents=True, exist_ok=True)
    canary_path = canary_home / "canary.bin"
    expected = canary_bytes()
    canary_path.write_bytes(expected)
    backup_inv = runner.backup([canary_path], excludes=(), tags=(CANARY_TAG,))
    if not backup_inv.ok:
        _LOG.error(
            "backup.restic.failed",
            op="backup",
            rc=backup_inv.returncode,
            stderr_tail=backup_inv.stderr_tail,
        )
        return EXIT_RESTIC
    try:
        canary_snap = parse_backup_summary_snapshot_id(backup_inv.stdout_full)
    except BackupError as exc:
        _LOG.error("backup.drill.canary_unparseable", error=str(exc))
        return EXIT_RESTIC
    restore_inv = runner.restore(
        canary_snap, target=target_root / "canary", includes=[str(canary_path)]
    )
    if not restore_inv.ok:
        _LOG.error(
            "backup.restic.failed",
            op="restore",
            rc=restore_inv.returncode,
            stderr_tail=restore_inv.stderr_tail,
        )
        return EXIT_RESTIC
    restored = next((target_root / "canary").rglob("canary.bin"), None)
    verdict: int | None = None
    if restored is None or read_regular_bytes(restored) != expected:
        _LOG.error(
            "backup.drill.mismatch",
            path=str(canary_path),
            detail="canary bytes differ" if restored is not None else "canary not restored",
        )
        verdict = EXIT_CORRUPTION
    forget_inv = runner.forget_snapshot(canary_snap)
    if not forget_inv.ok:
        # Expected under Object-Lock retention — never fails the drill.
        _LOG.warning(
            "backup.drill.canary_forget_failed",
            rc=forget_inv.returncode,
            stderr_tail=forget_inv.stderr_tail,
        )
    return verdict


def run_restore_drill(
    cfg: BackupEnvConfig,
    *,
    lake_root: Path | None = None,
    sample: int | None = None,
    target_dir: Path | None = None,
    snapshot_id: str | None = None,
    keep: bool = False,
    alerter: SweepAlerter | None = None,
    clock: Clock | None = None,
    runner_factory: Callable[[BackupEnvConfig], ResticRunner] = default_runner_factory,
) -> int:
    """Run one restore drill (§8 flow) — returns an exit code, raises nothing.

    Parameters
    ----------
    cfg
        The resolved backup config (repo + password source REQUIRED — exit 2).
    lake_root
        Lake root override for categorisation; ``None`` →
        :func:`brutex.config.paths.resolve_lake_root`.
    sample
        Sampled file count; ``None`` → ``cfg.drill_sample``.
    target_dir
        Restore target; ``None`` → a fresh temp dir under
        ``<state_dir>/drill-tmp`` (removed on success unless *keep*).
    snapshot_id
        Snapshot override; ``None`` → ``state.last_snapshot_id`` (the last
        VERIFIED success — NEVER ``latest``).
    keep
        Keep the restored tree even on success.
    alerter
        Injectable alerter; ``None`` → :class:`SweepAlerter`.
    clock
        Accepted for signature symmetry with the other entry points; the
        drill's verdicts derive from file bytes + stamps, not the wall clock.
    runner_factory
        Injectable :class:`~brutex.backup.restic_runner.ResticRunner` factory.

    Returns
    -------
    int
        ``EXIT_OK`` iff canary verified AND zero corrupt AND zero restore
        errors; ``EXIT_CORRUPTION`` on any byte mismatch (CRITICAL alert);
        ``EXIT_RESTIC`` on a restic invocation failure; ``EXIT_CONFIG`` when
        config/snapshot are unusable; ``EXIT_BUSY`` when the nightly run (or
        another drill) holds the single-flight lock — the drill is SKIPPED
        with a WARN ``backup_degraded(aspect="drill_skipped")`` (round 6:
        unlike the F15 run-vs-run overlap, where the other holder does the
        SAME work, a skipped drill is a month of LOST verification and
        launchd one-shots never retry — re-run manually).
    """
    del clock  # deliberate: no wall-clock dependence (documented above)
    active_alerter = alerter if alerter is not None else SweepAlerter()
    if cfg.repo is None or (cfg.password_file is None) == (cfg.password_command is None):
        _LOG.error("backup.config.no_password_source", context="restore-drill")
        active_alerter.backup_failed(
            reason="config",
            detail="restore-drill needs BRUTEX_BACKUP_REPO + exactly one password source.",
        )
        return EXIT_CONFIG

    if snapshot_id is None:
        try:
            state = load_state(state_path(cfg))
        except BackupStateError:
            state = None
        snapshot_id = state.last_snapshot_id if state is not None else None
        if snapshot_id is None:
            _LOG.error("backup.drill.no_snapshot", detail="run a backup first")
            return EXIT_CONFIG

    try:
        lake = lake_root if lake_root is not None else resolve_lake_root()
    except (KeyError, RuntimeError) as exc:
        # ``Path.expanduser()`` on a ``~nosuchuser`` BRUTEX_LAKE_ROOT typo
        # raises RuntimeError — which the OSError guard below cannot catch.
        # Map to the deterministic config triple, never a raw monthly
        # traceback (the run_backup twin of this guard).
        _LOG.error(
            "backup.config.malformed_path",
            env_var="BRUTEX_LAKE_ROOT",
            error=str(exc),
            context="restore-drill",
        )
        active_alerter.backup_failed(
            reason="config", detail=f"BRUTEX_LAKE_ROOT unresolvable: {exc}"
        )
        return EXIT_CONFIG

    try:
        # Round-5 MAJOR: the drill holds the SAME single-flight lock as the
        # nightly run (F15). A drill fire landing inside the nightly 03:35
        # run's verify tail (§5 egress math) would otherwise collide its
        # canary `restic backup`/`restore` with the nightly `check`/`forget
        # --prune` EXCLUSIVE repo locks, reading as a false `drill_restic`
        # failure or (drill-wins race) mapping the nightly's lock-conflict
        # `check` rc to the false backup_repo_corrupt triple. Round 6 pairs
        # this with (a) the default fire time moved to Day-1 13:00 — outside
        # the nightly window, so the busy skip is the exception, not the
        # deterministic monthly norm — and (b) a WARN on the skip below (a
        # skipped drill is LOST verification, not the benign F15 redundancy).
        with backup_singleflight_lock(cfg.state_dir):
            return _drill_flow(
                cfg,
                snapshot_id=snapshot_id,
                lake_root=lake,
                sample=sample,
                target_dir=target_dir,
                keep=keep,
                alerter=active_alerter,
                runner_factory=runner_factory,
            )
    except BackupBusyError:
        # Round-6 MAJOR (observability half): the F15 "no alert on benign
        # overlap" rationale is for run-vs-run — the other holder does the
        # SAME work. A busy-skipped DRILL is a month of lost verification
        # (monthly one-shot, launchd never retries) and was previously
        # visible only in drill.err.log — violating §11 "zero clicks/day"
        # and the alert-or-explicit-no-alert law. Monthly cadence makes the
        # WARN non-spammy; a recurring skip now pages instead of vanishing.
        _LOG.warning("backup.drill.busy", state_dir=str(cfg.state_dir))
        active_alerter.backup_degraded(
            aspect="drill_skipped",
            detail=(
                "monthly restore drill skipped: the backup single-flight lock is held "
                "(nightly run still in its verify tail?) — re-run `brutex backup "
                "restore-drill` manually; recurring skips mean the drill fire time "
                "needs moving"
            ),
        )
        return EXIT_BUSY
    except OSError as exc:
        # The drill's own F10 direction: canary/tmp setup or verification I/O
        # failed (ENOSPC / permissions on the home volume) — a deterministic
        # exit + alert triple, never a raw traceback out of the monthly
        # launchd job ("returns an exit code, raises nothing").
        _LOG.error("backup.drill.io_failed", error=str(exc))
        active_alerter.backup_failed(reason="drill_io", detail=str(exc))
        return EXIT_RESTIC


def _drill_flow(
    cfg: BackupEnvConfig,
    *,
    snapshot_id: str,
    lake_root: Path,
    sample: int | None,
    target_dir: Path | None,
    keep: bool,
    alerter: SweepAlerter,
    runner_factory: Callable[[BackupEnvConfig], ResticRunner],
) -> int:
    """The §8 drill body (canary → ls → sample restore → verify).

    *lake_root* arrives RESOLVED (the caller owns the total lake-root
    resolution). Every ``OSError`` raised here is mapped by
    :func:`run_restore_drill` to ``EXIT_RESTIC`` +
    ``backup_failed(reason="drill_io")``.
    """
    active_alerter = alerter
    lake = lake_root
    home_audit = brutex_home() / "audit"
    sample_count = sample if sample is not None else cfg.drill_sample
    if sample_count < 1:
        # Round-6 NIT: a typo'd `--sample -16` (or 0) would silently degrade
        # the monthly drill to canary-only while still logging backup.drill.ok
        # — the stratified byte-verification lane OFF with a green launchd
        # job. Clamp LOUDLY to the resolved config default (the env path is
        # already floored by resolve_backup_env, mirroring its prune_weekday
        # guard); canary-only is not a silent fall-through mode.
        _LOG.warning(
            "backup.config.drill_sample_out_of_range",
            value=sample_count,
            fallback=cfg.drill_sample,
        )
        sample_count = cfg.drill_sample
    explicit_target = target_dir is not None
    if target_dir is None:
        drill_tmp_dir(cfg).mkdir(parents=True, exist_ok=True)
        target_dir = Path(tempfile.mkdtemp(dir=drill_tmp_dir(cfg), prefix="drill-"))
    runner = runner_factory(cfg)
    _LOG.info(
        "backup.drill.start",
        snapshot_id=snapshot_id,
        sample=sample_count,
        target=str(target_dir),
    )

    canary_verdict = _run_canary(cfg, runner, target_dir)
    if canary_verdict == EXIT_CORRUPTION:
        active_alerter.backup_repo_corrupt(
            detail="drill mismatch: canary bytes differ", source="drill"
        )
        return EXIT_CORRUPTION  # tree KEPT for forensics
    if canary_verdict is not None:
        active_alerter.backup_failed(
            reason="drill_restic", detail="canary backup/restore invocation failed."
        )
        return canary_verdict

    ls_inv = runner.ls_json(snapshot_id)
    if not ls_inv.ok:
        _LOG.error(
            "backup.restic.failed", op="ls", rc=ls_inv.returncode, stderr_tail=ls_inv.stderr_tail
        )
        active_alerter.backup_failed(reason="drill_restic", detail="restic ls failed.")
        return EXIT_RESTIC
    all_paths = parse_ls_file_paths(ls_inv.stdout_full)
    snapshot_time = parse_ls_snapshot_time(ls_inv.stdout_full)
    sampled = _stratified_sample(
        all_paths,
        snapshot_id=snapshot_id,
        sample=sample_count,
        lake_root=lake,
        home_audit=home_audit,
    )

    tally = _DrillTally()
    if sampled:
        restore_inv = runner.restore(snapshot_id, target=target_dir, includes=list(sampled))
        if not restore_inv.ok:
            _LOG.error(
                "backup.restic.failed",
                op="restore",
                rc=restore_inv.returncode,
                stderr_tail=restore_inv.stderr_tail,
            )
            active_alerter.backup_failed(reason="drill_restic", detail="restic restore failed.")
            return EXIT_RESTIC
        for path in sampled:
            category = _categorize(path, lake_root=lake, home_audit=home_audit)
            _verify_sampled_file(
                path,
                target=target_dir,
                snapshot_time=snapshot_time,
                category=category,
                tally=tally,
            )

    if tally.corrupt_paths:
        active_alerter.backup_repo_corrupt(
            detail=f"drill mismatch: {tally.corrupt_paths[0]}", source="drill"
        )
        return EXIT_CORRUPTION  # tree KEPT for forensics
    if tally.restore_errors:
        active_alerter.backup_failed(
            reason="drill_restic",
            detail=f"{tally.restore_errors} sampled file(s) missing after restore.",
        )
        return EXIT_RESTIC

    _LOG.info(
        "backup.drill.ok",
        snapshot_id=snapshot_id,
        canary="verified",
        verified=tally.verified,
        skipped_changed=tally.skipped_changed,
        corrupt=tally.corrupt,
        honesty=(
            "the drill proves the restore machinery end-to-end and spot-checks "
            "integrity; it does NOT prove all data restores — full-data assurance "
            "is the rotating read-data-subset cycle + the quarterly manual DR "
            "exercise (ADR-0017)"
        ),
    )
    if not keep and not explicit_target:
        shutil.rmtree(target_dir, ignore_errors=True)
    return EXIT_OK


__all__ = [
    "CANARY_SEED",
    "CANARY_SIZE_BYTES",
    "canary_bytes",
    "run_restore_drill",
]
