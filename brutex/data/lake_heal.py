"""The automatic keep-FIRST lake healer + derived-data recall (DEC-LAKE-HEAL-001).

The ACTION half of DEC-LAKE-HEAL-001 (the detection half is
:mod:`brutex.data.lake_invariants`): given the scanner's findings, heal each one
through the NORMAL write path — both partition locks in the writers' order →
quarantine-COPY the original to ``<lake>/.quarantine/<UTC-ts>/<lake-relative
path>`` → mode-independent keep-FIRST row-removing/row-REASSIGNING transform →
atomic rewrite via the public :meth:`~brutex.data.parquet_writer.ParquetWriter.heal_partition`
/ :meth:`~brutex.data.option_parquet_writer.OptionParquetWriter.heal_partition`
(so ``raw_hash`` ROTATES and the catalog row is re-upserted — a healed partition
can never masquerade as untouched, Golden Rule #4) → one hash-chained audit
JSONL line per partition + ONE alert callback per batch. STRICTLY row-removing /
row-reassigning — never value-editing.

**Mode (default AUTO-ON):** :func:`resolve_lake_heal_mode` reads
``BRUTEX_LAKE_HEAL``; ``observe`` is the escape hatch — poison is reported
(WARNING + audit + alert) and NOTHING is touched (byte-pinned). The tolerant
parse fail-safes to ``auto`` — the deliberate INVERSE of the usual opt-in
flags, per the operator's default-AUTO-ON decision.

**Safety rails:** a batch that would rewrite more than
:data:`MASS_HEAL_FRACTION` of ≥ :data:`MASS_HEAL_MIN_FILES` scanned files is
REFUSED (the ``sweep_heal`` C7 twin — a scanner bug / mount mix-up must never
machine-rewrite half a lake); :class:`~brutex.data.errors.LakeWriteHalt`
propagates untouched (disk-full/OOM = pull-wide stop); every other per-finding
failure is isolated (counted ``errors``, logged, the batch continues).
Quarantined originals are NEVER auto-deleted — restore = stop pulls, copy the
file back from ``<lake>/.quarantine/<ts>/<relpath>``, then ``brutex lake
audit-unique`` (the next auto-heal will re-heal unless ``BRUTEX_LAKE_HEAL=
observe`` is set first).

**Derived recall (mandatory):** after a symbol's ``1minute`` heals, its ``1day``
is RE-DERIVED over the affected months (latest-wins overwrites the poisoned
daily bar; F&O passes ``include_oi`` + the OI-carry resampler) and its
materialised intraday-TF month files are DELETE-and-REBUILT via
:func:`~brutex.data.timeframe_materialize.materialize_lake_timeframes` — an
in-place re-derive would silently KEEP a poisoned keep-first bucket. The TF
rebuild is MONTH-BOUNDED (DEC-TF-REBUILD-MONTH-BOUND-001 — closes the named
follow-up of the original entry): it reads ONLY the affected months' 1-minute
partitions (``months=``), sound because an NSE session never crosses a UTC
month boundary (invariant test-pinned); a rebuild failure leaves the months
deleted + loudly logged (the pipeline prebuild rebuilds lazily;
``sweep_preflight`` refuses honestly on a missing slice).

Audit/alert are INJECTED seams (:class:`LakeAuditSink` Protocol + callbacks) so
``brutex/data`` gains no ``orchestrator``/``notifications`` module-level edge;
the one production convenience :func:`open_lake_heal_audit` uses a
function-local import (the ``window_ledger_store`` precedent).

**Heal order** (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 extends the original 4-class
order): drift → foreign → dup → option-foreign → option-dup, by path within
each class. **L1 honesty:** the reconcile-piggyback heal is structurally N/A
for ``options/`` — the reconcile walks the CATALOG, and the options tree is
uncatalogued by design.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

import structlog

from brutex.data.atomic_io import atomic_write_simple, atomic_write_via, lake_write_guard
from brutex.data.catalog import Catalog
from brutex.data.derive_stamp import remove_derive_stamp
from brutex.data.errors import (
    LakeWriteHalt,
    PartitionLockTimeout,
    WriterError,
    parquet_read_skip_errors,
)
from brutex.data.fno_daily_resample import resample_1m_to_fno_daily_positional
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.lake_invariants import (
    OPTIONS_DIR_NAME,
    ForeignMonthRows,
    IntraFileDup,
    LakeFinding,
    LakeScanReport,
    LegacyKeyDrift,
    OptionForeignMonthRows,
    OptionIntraFileDup,
    is_foreign_timestamp,
    scan_lake_invariants,
    utc_aware,
)
from brutex.data.lake_manifest import invalidate_manifest
from brutex.data.models import CandleBar
from brutex.data.option_parquet_writer import OptionParquetWriter, OptionWriterError
from brutex.data.parquet_writer import ParquetWriter, PartitionHealResult
from brutex.data.partition import BAR_DIR_NAME, partition_for_timestamp
from brutex.data.partition_lock import partition_write_lock
from brutex.data.spot_daily_resample import (
    DailyDeriveTarget,
    derive_daily_for_targets,
    ist_span_partition_months,
    make_lake_read_1m_window,
    make_lake_write_1day,
)
from brutex.data.timeframe_materialize import (
    INTRADAY_TF_INTERVALS,
    PrebuildKind,
    materialize_lake_timeframes,
)

if TYPE_CHECKING:  # pragma: no cover
    import polars as pl

    from brutex.data.catalog_reconcile import ReconcileResult

_LOG = structlog.get_logger("brutex.data.lake_heal")

#: IST is a fixed +05:30 offset (no DST) — the forming-session cutoff timezone.
_IST: Final[timezone] = timezone(timedelta(hours=5, minutes=30))

# ── mode + throttle env ──────────────────────────────────────────────────────

#: The heal-mode env var (``auto`` | ``observe``; default ``auto``).
LAKE_HEAL_ENV: Final[str] = "BRUTEX_LAKE_HEAL"

#: The two heal modes. Importable by name
#: (``from brutex.data.lake_heal import LakeHealMode``) but intentionally
#: NOT listed in ``__all__``: it is a PEP 695 ``type`` statement, which some
#: static analysers (CodeQL) do not yet model as an ``__all__`` binding, and
#: type aliases are idiomatically not part of the ``import *`` surface anyway.
type LakeHealMode = Literal["auto", "observe"]

#: Default heal mode — AUTO-ON per the DEC-LAKE-HEAL-001 operator decision.
DEFAULT_LAKE_HEAL_MODE: Final[LakeHealMode] = "auto"

#: The L3 startup-sweep per-file throttle env var (float seconds, ≥ 0).
LAKE_HEAL_THROTTLE_ENV: Final[str] = "BRUTEX_LAKE_HEAL_THROTTLE_S"

#: Default per-file pause on the L3 sweep — 50 ms/file keeps a boot sweep
#: gentle on a pull-active disk (a 10k-file lake spreads ~8 min of idle).
DEFAULT_SWEEP_THROTTLE_S: Final[float] = 0.05

#: Quarantine dir name — a lake-root SIBLING of ``bars/``+``options/`` (the
#: ``.locks/`` precedent) so quarantined copies are invisible to every walker.
QUARANTINE_DIRNAME: Final[str] = ".quarantine"

#: The L3 one-time-sweep sentinel, relative to the lake root.
SENTINEL_RELPATH: Final[str] = ".lake_heal/last_sweep.json"

#: The cumulative heal stats sidecar, relative to the lake root.
STATS_RELPATH: Final[str] = ".lake_heal/stats.json"

#: Mass-heal refusal rail (the ``sweep_heal`` C7 twin): refuse a batch that
#: would rewrite > this fraction of the scanned files …
MASS_HEAL_FRACTION: Final[float] = 0.5

#: … once at least this many files were scanned (a tiny lake never trips it).
MASS_HEAL_MIN_FILES: Final[int] = 20

#: December — the year-rollover month for :func:`_first_day_after`.
_DECEMBER: Final[int] = 12

#: The bars/ intervals a drifted legacy dir MERGES into its canonical dir
#: (both can carry vendor-authoritative rows). Every OTHER interval dir is
#: derived (keep-first-frozen, rebuildable) and is quarantine-MOVED instead —
#: merging derived data across dirs would freeze whichever landed first.
_MERGE_INTERVALS: Final[tuple[str, ...]] = ("1minute", "1day")


def resolve_lake_heal_mode(env: Mapping[str, str] | None = None) -> LakeHealMode:
    """Resolve the heal mode: ``observe`` opts OUT, anything else is ``auto``.

    ``observe`` (case-insensitive, stripped) → ``observe``; unset / blank /
    anything else → ``auto``. The operator decision (DEC-LAKE-HEAL-001) is
    default AUTO-ON, so the tolerant parse deliberately fail-safes to ``auto``
    — the INVERSE of the usual opt-in flags, stated loudly here. ``auto`` is
    also accepted explicitly.

    Parameters
    ----------
    env
        Environment mapping override (tests); ``None`` reads ``os.environ``.

    Returns
    -------
    LakeHealMode
        ``"observe"`` or ``"auto"``.
    """
    mapping: Mapping[str, str] = os.environ if env is None else env
    raw = mapping.get(LAKE_HEAL_ENV, "").strip().lower()
    if raw == "observe":
        return "observe"
    return "auto"


def resolve_sweep_throttle_s() -> float:
    """Resolve the L3 per-file sweep pause (seconds), tolerantly.

    Unset / blank / unparsable → :data:`DEFAULT_SWEEP_THROTTLE_S`; a negative
    value clamps to ``0.0`` (no pause). Env-tunable to 0 for an idle machine.

    Returns
    -------
    float
        The per-file pause in seconds (≥ 0).
    """
    raw = os.environ.get(LAKE_HEAL_THROTTLE_ENV, "").strip()
    if not raw:
        return DEFAULT_SWEEP_THROTTLE_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SWEEP_THROTTLE_S
    return max(0.0, value)


# ── audit seam ───────────────────────────────────────────────────────────────


class LakeAuditSink(Protocol):
    """The ``append`` slice of the hash-chained audit writer the healer uses.

    Mirrors ``brutex.data.option_sink.OptionAuditSink`` — an injected seam so
    the data layer never imports :mod:`brutex.orchestrator` at module level.
    """

    def append(self, *, action: str, subject: str, payload: dict[str, Any] | None = None) -> object:
        """Append one audit entry; return the writer's result."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


#: Audit action names (Golden Rule #8; hash-chained JSONL).
AUDIT_HEAL_DEDUP: Final[str] = "LAKE_HEAL_DEDUP"
AUDIT_HEAL_REASSIGN: Final[str] = "LAKE_HEAL_REASSIGN"
AUDIT_HEAL_DRIFT_MERGE: Final[str] = "LAKE_HEAL_DRIFT_MERGE"
AUDIT_HEAL_QUARANTINE_FILE: Final[str] = "LAKE_HEAL_QUARANTINE_FILE"
AUDIT_HEAL_BATCH: Final[str] = "LAKE_HEAL_BATCH"


# ── report value types ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HealAction:
    """One per-partition heal action, for the report/alert surface.

    Attributes
    ----------
    action
        One ``AUDIT_HEAL_*`` name.
    partition
        Lake-relative path (or ``"<exch>/<seg>/<symbol>"`` for a drift merge).
    kept
        Rows remaining in the healed partition.
    dropped
        Rows dropped (dup collapse + foreign removals).
    moved
        Rows moved home (reassignment / drift merge, deduped counts).
    conflicts
        Keep-first :class:`~brutex.data.parquet_writer.BarConflict` count
        recorded while moving rows (the canonical stored bar won).
    raw_hash_before, raw_hash_after
        The partition's byte hashes around the heal (``None`` where a hash
        does not apply — a drift merge spans files; a moved-away file has no
        after-hash).
    quarantine_path
        Where the original bytes were copied (``None`` = no copy was needed).
    """

    action: str
    partition: str
    kept: int
    dropped: int
    moved: int
    conflicts: int
    raw_hash_before: str | None
    raw_hash_after: str | None
    quarantine_path: str | None


@dataclass(frozen=True, slots=True)
class RecallReport:
    """Outcome of the derived-data recall after a batch's ``1minute`` heals.

    Attributes
    ----------
    daily_targets
        Symbols whose ``1day`` was re-derived (latest-wins) over the affected
        months.
    daily_bars
        Total daily bars written by the re-derive.
    tf_rebuilt
        Intraday-TF interval dirs delete-and-rebuilt successfully.
    tf_failed
        Intraday-TF interval dirs whose rebuild FAILED — their affected month
        files stay deleted (honest: rebuilt lazily by the derive route /
        gated pre-build; never a silently poisoned bucket).
    """

    daily_targets: int
    daily_bars: int
    tf_rebuilt: int
    tf_failed: int


@dataclass(frozen=True, slots=True)
class HealReport:
    """Outcome of one :func:`heal_findings` batch.

    Attributes
    ----------
    mode
        The mode the batch ran under.
    findings
        Findings handed to the batch.
    healed
        Findings actually healed (files changed).
    skipped
        Findings skipped (vanished path / already clean / stale — re-found by
        the next scan if still relevant; idempotent convergence).
    errors
        Findings that errored (isolated per finding, batch continued).
    refused_mass_heal
        ``True`` when the C7-twin rail refused the whole batch untouched.
    actions
        The per-partition actions taken.
    quarantine_root
        ``"<lake>/.quarantine/<ts>"`` when any original was quarantined,
        else ``None`` (observe / no-op batches create no quarantine dir).
    recall
        The derived-data recall outcome (``None`` when no ``1minute`` was
        touched or ``recall=False``).
    """

    mode: LakeHealMode
    findings: int
    healed: int
    skipped: int
    errors: int
    refused_mass_heal: bool
    actions: tuple[HealAction, ...]
    quarantine_root: str | None
    recall: RecallReport | None


# ── batch internals ──────────────────────────────────────────────────────────

#: Per-finding isolated failure set (the reconcile/movers narrow-skip
#: discipline): a vanished/corrupt file, a lock timeout, or a writer refusal
#: skips THAT finding; :class:`LakeWriteHalt` (not a member) propagates.
_PER_FINDING_HEAL_ERRORS: Final[tuple[type[BaseException], ...]] = (
    WriterError,
    OptionWriterError,
    PartitionLockTimeout,
    *parquet_read_skip_errors(),
)


@dataclass
class _BatchState:
    """Mutable working state threaded through one heal batch (not a value type)."""

    lake_root: Path
    quarantine_root: Path
    audit: LakeAuditSink | None
    catalog: Catalog | None
    writer: ParquetWriter
    option_writer: OptionParquetWriter
    quarantined_files: int = 0
    actions: list[HealAction] = field(default_factory=list)
    touched_minute: dict[tuple[str, str, str], set[tuple[int, int]]] = field(default_factory=dict)

    def quarantine_path(self, source: Path) -> Path:
        """The quarantine destination preserving *source*'s lake-relative path."""
        return self.quarantine_root / source.relative_to(self.lake_root)

    def touch(self, exchange: str, segment: str, symbol: str, months: set[tuple[int, int]]) -> None:
        """Record that *symbol*'s ``1minute`` months were touched (recall driver)."""
        self.touched_minute.setdefault((exchange, segment, symbol), set()).update(months)


def _utc_now() -> datetime:
    """The injectable-clock default (quarantine ts / stats / sentinel stamps)."""
    return datetime.now(UTC)


def _audit_append(
    audit: LakeAuditSink | None, *, action: str, subject: str, payload: dict[str, Any]
) -> None:
    """Best-effort audit append — an audit failure never fails the heal."""
    if audit is None:
        return
    try:
        audit.append(action=action, subject=subject, payload=payload)
    except Exception as exc:  # pylint: disable=broad-exception-caught — best-effort seam
        _LOG.warning("lake_heal.audit_append_failed", action=action, error=str(exc))


def _order_findings(findings: Sequence[LakeFinding]) -> tuple[LakeFinding, ...]:
    """Deterministic heal order: drift → foreign → dup → option-foreign → option-dup, by path.

    A drift merge changes which files exist, so it heals FIRST; the later
    per-partition healers find consumed paths gone and skip them (counted
    ``skipped``, re-found next scan if still relevant — idempotent
    convergence, never a double-heal). Option-foreign heals BEFORE option-dup
    for the same reason bars foreign precedes dup: the reassignment's
    remove-rewrite also collapses the source file's dups, so a same-file
    ``OptionIntraFileDup`` afterwards reads ``changed=False`` → counted
    ``skipped``, never a double-heal and never a second quarantine copy
    (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001).
    """

    def key(finding: LakeFinding) -> tuple[int, str]:
        if isinstance(finding, LegacyKeyDrift):
            return (0, str(finding.drifted_dir))
        if isinstance(finding, ForeignMonthRows):
            return (1, str(finding.path))
        if isinstance(finding, IntraFileDup):
            return (2, str(finding.path))
        if isinstance(finding, OptionForeignMonthRows):
            return (3, str(finding.path))
        return (4, str(finding.path))

    return tuple(sorted(findings, key=key))


def _affected_files(findings: Sequence[LakeFinding]) -> int:
    """Distinct files a batch would touch (the mass-heal refusal numerator)."""
    paths: set[str] = set()
    drift_files = 0
    for finding in findings:
        if isinstance(finding, LegacyKeyDrift):
            drift_files += finding.files
        else:
            paths.add(str(finding.path))
    return len(paths) + drift_files


def _finding_token(finding: LakeFinding) -> str:
    """A short human token naming one finding (error-log subject)."""
    if isinstance(finding, LegacyKeyDrift):
        return f"{finding.exchange}/{finding.segment}/{finding.drifted_symbol}"
    return str(finding.path)


def _relpath(lake_root: Path, path: Path) -> str:
    """*path*'s lake-relative POSIX string (audit subjects)."""
    return path.relative_to(lake_root).as_posix()


def _file_month(file: Path) -> tuple[int, int] | None:
    """The ``(year, month)`` a ``<yyyy>/<mm>.parquet`` file addresses, or ``None``."""
    try:
        return int(file.parent.name), int(file.stem)
    except ValueError:
        return None


def _quarantine_move(state: _BatchState, source: Path) -> Path:
    """Quarantine-COPY *source* then unlink it (a whole-file move, audited)."""
    dest = state.quarantine_path(source)
    content = source.read_bytes()
    with lake_write_guard():
        dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_via(dest, lambda tmp: tmp.write_bytes(content))
    source.unlink()
    with contextlib.suppress(OSError):
        source.parent.rmdir()  # prune a now-empty <yyyy>/ dir; non-empty is fine
    # A quarantine-MOVE removes a stored file, which can empty its dataset —
    # the durable dataset manifest may now over-list, so invalidate it
    # (DEC-LAKE-MANIFEST-O1-001; best-effort + idempotent, the next discovery
    # walks + rebuilds). Covers BOTH removal routes through this ONE
    # chokepoint: the whole-file quarantine AND the intraday-TF
    # delete-and-rebuild (`_rebuild_tf_months` moves each month through here).
    invalidate_manifest(state.lake_root)
    state.quarantined_files += 1
    _LOG.warning("lake_heal.file_quarantined", source=str(source), quarantine=str(dest))
    _audit_append(
        state.audit,
        action=AUDIT_HEAL_QUARANTINE_FILE,
        subject=_relpath(state.lake_root, source),
        payload={"quarantine": str(dest)},
    )
    return dest


def _write_bars_grouped_by_month(
    state: _BatchState,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    bars: Sequence[CandleBar],
) -> tuple[set[tuple[int, int]], int, int]:
    """Write *bars* home through the NORMAL write path, one UTC month per write.

    The ONE shared grouping loop behind the reassignment (§3.2) and drift-merge
    (§3.3) heals: groups by :func:`~brutex.data.partition.partition_for_timestamp`
    (so a legacy file that ALSO carries foreign-month rows lands each row in its
    canonical month), writes ``merge_mode="keep-first"`` (a canonical file's
    EXISTING row always WINS — a value mismatch records a
    :class:`~brutex.data.parquet_writer.BarConflict`; a missing row is added),
    and upserts each :class:`~brutex.data.parquet_writer.WriteResult` into the
    catalog when one is wired.

    Returns
    -------
    tuple[set[tuple[int, int]], int, int]
        ``(months written, rows actually added, keep-first conflicts)``.
    """
    groups: dict[tuple[int, int], list[CandleBar]] = {}
    for bar in bars:
        groups.setdefault(partition_for_timestamp(bar.timestamp), []).append(bar)
    moved = 0
    conflicts = 0
    for key in sorted(groups):
        result = state.writer.write(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            bars=groups[key],
            merge_mode="keep-first",
        )
        moved += result.bars_added
        conflicts += len(result.conflicts)
        if state.catalog is not None:
            entry = state.writer.make_catalog_entry(
                exchange=exchange, segment=segment, symbol=symbol, interval=interval, result=result
            )
            state.catalog.upsert(entry)
    return set(groups), moved, conflicts


def _sync_catalog_after_heal(
    state: _BatchState,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    interval: str,
    year: int,
    month: int,
    result: PartitionHealResult,
) -> None:
    """Rotate/delete the healed partition's catalog row (Golden Rule #4)."""
    if state.catalog is None:
        return
    if result.raw_hash_after is None:
        state.catalog.delete_partition(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            year=year,
            month=month,
        )
    elif result.write is not None:
        entry = state.writer.make_catalog_entry(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            result=result.write,
        )
        state.catalog.upsert(entry)


def _heal_dup(finding: IntraFileDup, state: _BatchState) -> bool:
    """Heal one intra-file duplicate finding; ``True`` iff the file changed."""
    path = finding.path
    if not path.exists():
        return False  # consumed by an earlier drift merge / reassignment move
    coords = finding.coords
    result = state.writer.heal_partition(path, quarantine_to=state.quarantine_path(path))
    if not result.changed:
        return False  # already clean (healed earlier in this batch) or probe abstain
    state.quarantined_files += 1
    _LOG.warning(
        "lake_heal.partition_healed",
        partition=coords.relpath(),
        kept=result.kept,
        dropped=result.dropped_duplicates,
    )
    state.actions.append(
        HealAction(
            action=AUDIT_HEAL_DEDUP,
            partition=coords.relpath(),
            kept=result.kept,
            dropped=result.dropped_duplicates,
            moved=0,
            conflicts=0,
            raw_hash_before=result.raw_hash_before,
            raw_hash_after=result.raw_hash_after,
            quarantine_path=str(state.quarantine_path(path)),
        )
    )
    _audit_append(
        state.audit,
        action=AUDIT_HEAL_DEDUP,
        subject=coords.relpath(),
        payload={
            "kept": result.kept,
            "dropped_duplicates": result.dropped_duplicates,
            "dropped_removed": result.dropped_removed,
            "raw_hash_before": result.raw_hash_before,
            "raw_hash_after": result.raw_hash_after,
            "quarantine_path": str(state.quarantine_path(path)),
        },
    )
    _sync_catalog_after_heal(
        state,
        exchange=coords.exchange,
        segment=coords.segment,
        symbol=coords.symbol,
        interval=coords.interval,
        year=coords.year,
        month=coords.month,
        result=result,
    )
    if coords.interval == "1minute":
        state.touch(coords.exchange, coords.segment, coords.symbol, {(coords.year, coords.month)})
    return True


def _heal_foreign(finding: ForeignMonthRows, state: _BatchState) -> bool:
    """Reassign one file's foreign-month rows home; ``True`` iff rows moved.

    Crash-safe order = copy-forward FIRST (keep-first into the canonical month
    files), remove SECOND (:meth:`~brutex.data.parquet_writer.ParquetWriter.heal_partition`
    with ``remove_timestamps``) — a crash between the two leaves the rows in
    BOTH files, the finding re-appears on the next scan, and the keep-first
    union makes the re-run converge (idempotent). Locks are taken per partition
    SEQUENTIALLY (write canonical → release → heal source) — never two
    partitions locked at once, so no deadlock. A wholly-foreign source file is
    quarantine-MOVED (and its catalog row deleted).
    """
    coords = finding.coords
    src = finding.path
    if not src.exists():
        return False  # consumed by an earlier drift merge
    bars = state.writer.read_partition_bars(src)
    home = (coords.year, coords.month)
    foreign = {ts: bar for ts, bar in bars.items() if partition_for_timestamp(ts) != home}
    if not foreign:
        return False  # stale finding — already reassigned (a prior crashed run)
    months, moved, conflicts = _write_bars_grouped_by_month(
        state,
        exchange=coords.exchange,
        segment=coords.segment,
        symbol=coords.symbol,
        interval=coords.interval,
        bars=list(foreign.values()),
    )
    result = state.writer.heal_partition(
        src, remove_timestamps=frozenset(foreign), quarantine_to=state.quarantine_path(src)
    )
    if not result.changed:
        # Probe abstain (the #1067 "never guess" pin — mirrors _heal_dup): the
        # source rows were NOT removed, so do NOT count/audit/touch. The
        # copy-forward above is keep-first idempotent, the finding stays live,
        # and the next scan re-runs the reassignment (convergent re-try).
        return False
    state.quarantined_files += 1
    _LOG.warning(
        "lake_heal.rows_reassigned",
        partition=coords.relpath(),
        moved=len(foreign),
        added=moved,
        months=[f"{y:04d}-{m:02d}" for y, m in sorted(months)],
        conflicts=conflicts,
    )
    state.actions.append(
        HealAction(
            action=AUDIT_HEAL_REASSIGN,
            partition=coords.relpath(),
            kept=result.kept,
            dropped=result.dropped_duplicates + result.dropped_removed,
            moved=len(foreign),
            conflicts=conflicts,
            raw_hash_before=result.raw_hash_before,
            raw_hash_after=result.raw_hash_after,
            quarantine_path=str(state.quarantine_path(src)),
        )
    )
    _audit_append(
        state.audit,
        action=AUDIT_HEAL_REASSIGN,
        subject=coords.relpath(),
        payload={
            "moved": len(foreign),
            "added": moved,
            "months": [f"{y:04d}-{m:02d}" for y, m in sorted(months)],
            "conflicts": conflicts,
            "kept": result.kept,
            "dropped_duplicates": result.dropped_duplicates,
            "raw_hash_before": result.raw_hash_before,
            "raw_hash_after": result.raw_hash_after,
            "quarantine_path": str(state.quarantine_path(src)),
        },
    )
    _sync_catalog_after_heal(
        state,
        exchange=coords.exchange,
        segment=coords.segment,
        symbol=coords.symbol,
        interval=coords.interval,
        year=coords.year,
        month=coords.month,
        result=result,
    )
    if coords.interval == "1minute":
        touched = set(months)
        touched.add(home)
        state.touch(coords.exchange, coords.segment, coords.symbol, touched)
    return True


def _interval_merge_order(interval_dir: Path) -> tuple[int, str]:
    """Sort key: ``1minute`` first, then ``1day``, then the derived rest."""
    name = interval_dir.name
    if name in _MERGE_INTERVALS:
        return (_MERGE_INTERVALS.index(name), name)
    return (len(_MERGE_INTERVALS), name)


def _heal_drift(finding: LegacyKeyDrift, state: _BatchState) -> bool:
    """Merge one drifted legacy symbol dir into its canonical dir; ``True`` iff files moved.

    ``1minute`` + ``1day`` files are MERGED keep-first into the canonical
    symbol (the canonical dir's stored bars WIN — the #1139 law; rows are
    grouped by UTC month so a legacy file that also carries foreign rows lands
    each row home); every OTHER interval dir (derived intraday TFs) is
    quarantine-MOVED without merging — merging derived keep-first data would
    freeze whichever landed first; the recall rebuilds it from the merged
    ``1minute`` instead. Legacy files are quarantine-copied then unlinked; the
    emptied legacy dirs are pruned; the legacy catalog rows are deleted.
    """
    drifted_dir = finding.drifted_dir
    if not drifted_dir.is_dir():
        return False  # already merged (a prior run)
    merged_rows = 0
    conflicts_total = 0
    files_moved = 0
    per_interval: dict[str, int] = {}
    canonical_minute_months: set[tuple[int, int]] = set()
    for interval_dir in sorted(
        (p for p in drifted_dir.iterdir() if p.is_dir()), key=_interval_merge_order
    ):
        interval = interval_dir.name
        files = sorted(interval_dir.rglob("*.parquet"))
        for file in files:
            if interval in _MERGE_INTERVALS:
                bars = state.writer.read_partition_bars(file)
                if bars:
                    months, moved, conflicts = _write_bars_grouped_by_month(
                        state,
                        exchange=finding.exchange,
                        segment=finding.segment,
                        symbol=finding.canonical_symbol,
                        interval=interval,
                        bars=sorted(bars.values(), key=lambda b: b.timestamp),
                    )
                    merged_rows += moved
                    conflicts_total += conflicts
                    if interval == "1minute":
                        canonical_minute_months |= months
            _quarantine_move(state, file)
            files_moved += 1
            per_interval[interval] = per_interval.get(interval, 0) + 1
            coords_month = _file_month(file)
            if state.catalog is not None and coords_month is not None:
                state.catalog.delete_partition(
                    exchange=finding.exchange,
                    segment=finding.segment,
                    symbol=finding.drifted_symbol,
                    interval=interval,
                    year=coords_month[0],
                    month=coords_month[1],
                )
    _prune_empty_dirs(drifted_dir)
    subject = f"{finding.exchange}/{finding.segment}/{finding.drifted_symbol}"
    _LOG.warning(
        "lake_heal.drift_merged",
        drifted=repr(finding.drifted_symbol),
        canonical=repr(finding.canonical_symbol),
        exchange=finding.exchange,
        segment=finding.segment,
        files=files_moved,
        rows=merged_rows,
        conflicts=conflicts_total,
    )
    state.actions.append(
        HealAction(
            action=AUDIT_HEAL_DRIFT_MERGE,
            partition=subject,
            kept=0,
            dropped=0,
            moved=merged_rows,
            conflicts=conflicts_total,
            raw_hash_before=None,
            raw_hash_after=None,
            quarantine_path=str(state.quarantine_root),
        )
    )
    _audit_append(
        state.audit,
        action=AUDIT_HEAL_DRIFT_MERGE,
        subject=subject,
        payload={
            "canonical": finding.canonical_symbol,
            "files_by_interval": per_interval,
            "rows_merged": merged_rows,
            "conflicts": conflicts_total,
            "quarantine_root": str(state.quarantine_root),
        },
    )
    if canonical_minute_months:
        state.touch(
            finding.exchange, finding.segment, finding.canonical_symbol, canonical_minute_months
        )
    return True


def _heal_option_dup(finding: OptionIntraFileDup, state: _BatchState) -> bool:
    """Heal one options-tree ``(side, timestamp)`` dup file; ``True`` iff changed."""
    path = finding.path
    if not path.exists():
        return False
    result = state.option_writer.heal_partition(path, quarantine_to=state.quarantine_path(path))
    if not result.changed:
        return False
    state.quarantined_files += 1
    partition = f"{OPTIONS_DIR_NAME}/{finding.series}/{path.name}"
    _LOG.warning(
        "lake_heal.partition_healed",
        partition=partition,
        kept=result.kept,
        dropped=result.dropped_duplicates,
    )
    state.actions.append(
        HealAction(
            action=AUDIT_HEAL_DEDUP,
            partition=partition,
            kept=result.kept,
            dropped=result.dropped_duplicates,
            moved=0,
            conflicts=0,
            raw_hash_before=None,
            raw_hash_after=None,
            quarantine_path=str(state.quarantine_path(path)),
        )
    )
    _audit_append(
        state.audit,
        action=AUDIT_HEAL_DEDUP,
        subject=partition,
        payload={
            "tree": "options",
            "kept": result.kept,
            "dropped_duplicates": result.dropped_duplicates,
            "quarantine_path": str(state.quarantine_path(path)),
        },
    )
    return True  # no catalog (the options tree is not catalogued), no recall


def _option_foreign_frame(home: tuple[int, int], frame: pl.DataFrame) -> pl.DataFrame:
    """The keep-first-unique foreign-month rows of one raw options frame (all columns).

    Keep-first ``(side, timestamp)`` dedup BEFORE the split (a physically
    duplicated foreign key moves ONCE, first physical row wins — mirrors
    ``read_partition_bars``'s collapse on the bars path), then a Python row
    mask over the ONE shared routing predicate
    (:func:`~brutex.data.lake_invariants.is_foreign_timestamp` — §11 COMMON).
    A null timestamp abstains (kept home, never moved) — the bars null-abstain
    twin. Side is NEVER consulted (the timestamp-only routing law,
    DEC-LAKE-HEAL-OPTIONS-FOREIGN-001).
    """
    import polars as pl

    deduped = frame.unique(subset=["side", "timestamp"], keep="first", maintain_order=True)
    mask = [is_foreign_timestamp(home, ts) for ts in deduped.get_column("timestamp").to_list()]
    return deduped.filter(pl.Series(mask))


def _heal_option_foreign(finding: OptionForeignMonthRows, state: _BatchState) -> bool:
    """Reassign one options-tree file's foreign-month rows home; ``True`` iff rows moved.

    The options twin of :func:`_heal_foreign` — SAME crash-safe order:
    copy-forward FIRST (keep-first union into the canonical month siblings via
    :meth:`~brutex.data.option_parquet_writer.OptionParquetWriter.merge_partition_frame`
    — an existing canonical ``(side, ts)`` key WINS, the foreign copy is then
    simply REMOVED; an absent key is added = REASSIGNED), remove SECOND
    (:meth:`~brutex.data.option_parquet_writer.OptionParquetWriter.heal_partition`
    with ``remove_keys`` built from the SAME consistent read's RAW stored
    values). A crash between the two leaves the rows in BOTH files; the
    finding re-appears next scan and the keep-first union converges
    (idempotent). Locks are taken per partition SEQUENTIALLY (read source →
    release; merge sibling → release; heal source) — never two partitions
    locked at once, no deadlock. Routing is timestamp-only through
    :func:`~brutex.data.lake_invariants.utc_aware` (a hand-planted naive
    column routes as-if-UTC instead of crashing the batch —
    ``PartitionError`` is NOT in ``_PER_FINDING_HEAL_ERRORS``); removal keys
    are the RAW stored tuples. A wholly-foreign file is quarantine-MOVED by
    the writer. Both mutation legs run under the writer's symlink-containment
    rail (``OptionParquetWriter._assert_options_containment``, the round-2
    TOCTOU fold): a series dir swapped to a symlink between scan and heal is
    REFUSED with :class:`OptionWriterError` — a member of
    ``_PER_FINDING_HEAL_ERRORS``, so the refusal is an isolated counted error
    and nothing outside the lake is ever mutated. No catalog (the options
    tree is uncatalogued), no ``state.touch`` (NOTHING derives from
    ``options/`` — recall stays ``None``), ``conflicts=0`` honestly (option
    vendor-drift detection is the writer's own deferred follow-up — no
    ``BarConflict`` twin exists). STRICTLY row-removing/row-reassigning —
    moved rows keep their stored column values verbatim.
    """
    src = finding.path
    if not src.exists():
        return False  # consumed earlier / vanished — skipped
    frame = state.option_writer.read_partition_frame(src)  # OptionWriterError → isolated
    home = (finding.year, finding.month)
    foreign = _option_foreign_frame(home, frame)  # keep-first unique foreign rows, all cols
    if foreign.height == 0:
        return False  # stale finding (a prior crashed run) — skipped
    sides = foreign.get_column("side").to_list()
    stamps = foreign.get_column("timestamp").to_list()
    keys = frozenset(zip(sides, stamps, strict=True))  # RAW stored values
    # ONE-pass dict-group of row indices by home month (the bars twin's
    # `_write_bars_grouped_by_month` O(rows) shape — review round-2 NIT: the
    # prior per-month boolean-mask rebuild was O(months × rows)).
    rows_by_month: dict[tuple[int, int], list[int]] = {}
    for idx, ts in enumerate(stamps):
        rows_by_month.setdefault(partition_for_timestamp(utc_aware(ts)), []).append(idx)
    added = 0
    target_months = sorted(rows_by_month)
    for target in target_months:
        group = foreign[rows_by_month[target]]
        dest = src.parent / f"{target[0]:04d}-{target[1]:02d}.parquet"
        added += state.option_writer.merge_partition_frame(dest, group)
    result = state.option_writer.heal_partition(
        src, remove_keys=keys, quarantine_to=state.quarantine_path(src)
    )
    if not result.changed:
        return False  # defensive convergence guard (the _heal_foreign symmetry)
    state.quarantined_files += 1
    partition = f"{OPTIONS_DIR_NAME}/{finding.series}/{src.name}"
    months_str = [f"{y:04d}-{m:02d}" for y, m in target_months]
    _LOG.warning(
        "lake_heal.rows_reassigned",  # the SAME pinned event as bars
        partition=partition,
        series=repr(finding.series),  # RAW filesystem dir name — the #907 repr() convention
        tree="options",
        moved=len(keys),
        added=added,
        months=months_str,
        conflicts=0,
    )
    state.actions.append(
        HealAction(
            action=AUDIT_HEAL_REASSIGN,
            partition=partition,
            kept=result.kept,
            dropped=result.dropped_duplicates + result.dropped_removed,
            moved=len(keys),
            conflicts=0,
            raw_hash_before=None,  # uncatalogued — the _heal_option_dup precedent
            raw_hash_after=None,
            quarantine_path=str(state.quarantine_path(src)),
        )
    )
    _audit_append(
        state.audit,
        action=AUDIT_HEAL_REASSIGN,
        subject=partition,
        payload={
            "tree": "options",  # the _heal_option_dup convention
            "moved": len(keys),
            "added": added,
            "months": months_str,
            "kept": result.kept,
            "dropped_duplicates": result.dropped_duplicates,
            "dropped_removed": result.dropped_removed,
            "quarantine_path": str(state.quarantine_path(src)),
        },
    )
    return True


def _heal_one(finding: LakeFinding, state: _BatchState) -> bool:
    """Dispatch one finding to its heal algorithm; ``True`` iff it healed."""
    if isinstance(finding, LegacyKeyDrift):
        return _heal_drift(finding, state)
    if isinstance(finding, ForeignMonthRows):
        return _heal_foreign(finding, state)
    if isinstance(finding, IntraFileDup):
        return _heal_dup(finding, state)
    if isinstance(finding, OptionForeignMonthRows):
        return _heal_option_foreign(finding, state)
    return _heal_option_dup(finding, state)


def _prune_empty_dirs(root: Path) -> None:
    """Remove *root*'s now-empty subdirs bottom-up, then *root* itself if empty."""
    if not root.is_dir():
        return
    subdirs = sorted(
        (p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True
    )
    for path in subdirs:
        with contextlib.suppress(OSError):
            path.rmdir()
    with contextlib.suppress(OSError):
        root.rmdir()


# ── derived-data recall ──────────────────────────────────────────────────────


def _first_day_after(year: int, month: int) -> date:
    """The first day of the month AFTER ``(year, month)``."""
    if month == _DECEMBER:
        return date(year + 1, 1, 1)
    return date(year, month + 1, 1)


def _prebuild_kind_from_coords(segment: str, symbol: str) -> PrebuildKind | None:
    """The TF-rebuild kind for one lake coordinate, or ``None`` = never rebuild.

    ``INDEX``/``CASH`` → SPOT; ``FNO`` + a ``groww_symbol`` decoding to a
    ``FUT`` contract → FUTURES; a CE/PE contract or an undecodable FNO symbol →
    ``None`` (options intraday is never materialised —
    :func:`~brutex.data.timeframe_materialize.materialize_lake_timeframes`
    raises for OPTIONS by design).
    """
    if segment != "FNO":
        return PrebuildKind.SPOT
    parts = parse_fno_symbol(symbol)
    if parts is None or parts.instrument_type != "FUT":
        return None
    return PrebuildKind.FUTURES


def _rebuild_tf_months(
    state: _BatchState,
    *,
    exchange: str,
    segment: str,
    symbol: str,
    months: set[tuple[int, int]],
) -> tuple[int, int]:
    """DELETE-and-rebuild one symbol's materialised TF months; ``(rebuilt, failed)`` TF dirs.

    The TF writer is keep-first, so an in-place re-derive would record a
    :class:`~brutex.data.parquet_writer.BarConflict` and KEEP the poisoned
    stored bucket — the affected months' files are therefore quarantine-MOVED
    from EVERY existing TF interval dir first, then re-materialised through the
    EXISTING production driver **bounded to those months**
    (``materialize_lake_timeframes(months=…)``,
    DEC-TF-REBUILD-MONTH-BOUND-001 — the rebuild read is O(affected-months),
    no longer the whole O(symbol-history) 1-minute tree; sound because an NSE
    session never crosses a UTC month boundary, so a month's intraday-TF bars
    derive fully from that month's own 1-minute partition — invariant pinned by
    ``test_calendar_nse_utc_month_invariant.py``, byte-equality vs the full
    rebuild pinned by the roundtrip suite). The quarantine-delete loop is
    already month-scoped, so writes land only in the deleted months
    (keep-first makes any overlap a no-op). A rebuild failure leaves the
    months deleted + loudly logged (``lake_heal.tf_rebuild_failed``) — lazily
    rebuildable, never a silently poisoned bucket.
    :class:`~brutex.data.errors.LakeWriteHalt` propagates (disk-full during
    recall = graceful halt).
    """
    symbol_dir = state.lake_root / BAR_DIR_NAME / exchange / segment / symbol
    tf_labels = [
        label
        for label, interval in INTRADAY_TF_INTERVALS.items()
        if (symbol_dir / interval).is_dir()
    ]
    if not tf_labels:
        return 0, 0  # TF partitions exist only when the operator materialised
    kind = _prebuild_kind_from_coords(segment, symbol)
    if kind is None:
        return 0, 0  # options intraday is never materialised (defensive)
    try:
        for label in tf_labels:
            interval = INTRADAY_TF_INTERVALS[label]
            for year, month in sorted(months):
                dest = symbol_dir / interval / f"{year:04d}" / f"{month:02d}.parquet"
                if not dest.exists():
                    continue
                _quarantine_move(state, dest)
                if state.catalog is not None:
                    state.catalog.delete_partition(
                        exchange=exchange,
                        segment=segment,
                        symbol=symbol,
                        interval=interval,
                        year=year,
                        month=month,
                    )
        materialize_lake_timeframes(
            lake_root=state.lake_root,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            kind=kind,
            tfs=tf_labels,
            months=months,
            # The heal just quarantine-MOVED these months — the rebuild must
            # ALWAYS re-derive and must NOT write a fresh stamp (its tf subset /
            # no-daily shape is not the pipeline's; DEC-PREBUILD-INCREMENTAL-001).
            incremental=False,
        )
    except LakeWriteHalt:
        raise
    except Exception as exc:  # pylint: disable=broad-exception-caught — per-symbol isolation
        _LOG.warning(
            "lake_heal.tf_rebuild_failed", symbol=symbol, tfs=sorted(tf_labels), error=str(exc)
        )
        return 0, len(tf_labels)
    _LOG.info("lake_heal.recall_tf_rebuilt", symbol=symbol, tfs=sorted(tf_labels))
    return len(tf_labels), 0


def _run_recall(state: _BatchState, *, before: date) -> RecallReport:
    """Recall the derived data of every symbol whose ``1minute`` was healed.

    ``1day`` is RE-DERIVED over the affected months ±1 day (the midnight-IST
    straddle pad) — the derived-1day write is ``latest-wins``, so a poisoned /
    stale stored daily bar at the same timestamp is OVERWRITTEN by the
    corrected derivation (identical values are a no-op). Heal never NET-removes
    a timestamp from a series, so every IST date that had 1-minute still has it
    and the re-derive covers it — no stale-daily orphan. A vendor-fetched 1day
    row (a CLI lake) at the same ts IS overwritten by the derived value —
    accepted: derived-from-healed-1m is the corrected truth (recorded in
    MIGRATION #1145). The F&O branch reads OI + resamples through the OI-carry
    positional resampler. Then the symbol's materialised intraday TFs are
    delete-and-rebuilt (:func:`_rebuild_tf_months`).
    """
    daily_targets = 0
    daily_bars = 0
    tf_rebuilt = 0
    tf_failed = 0
    for (exchange, segment, symbol), months in sorted(state.touched_minute.items()):
        # DEC-PREBUILD-INCREMENTAL-001: the heal rewrote this symbol's 1-minute
        # source, so its derive stamp is stale by definition — remove it OUTRIGHT
        # (belt-and-braces: the rewrite already moved the source token) so the
        # next pipeline prebuild can never skip a healed symbol.
        remove_derive_stamp(state.lake_root, exchange, segment, symbol)
        y_min, m_min = min(months)
        y_max, m_max = max(months)
        target = DailyDeriveTarget(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            start=date(y_min, m_min, 1) - timedelta(days=1),
            end=_first_day_after(y_max, m_max) + timedelta(days=1),
        )
        report = derive_daily_for_targets(
            targets=[target],
            build_read_1m=lambda t: make_lake_read_1m_window(
                lake_root=state.lake_root,
                exchange=t.exchange,
                segment=t.segment,
                symbol=t.symbol,
                start=t.start,
                end=t.end,
                include_oi=t.segment == "FNO",
            ),
            build_write_1day=lambda t: make_lake_write_1day(
                lake_root=state.lake_root, exchange=t.exchange, segment=t.segment, symbol=t.symbol
            ),
            before=before,
            resample_fn=resample_1m_to_fno_daily_positional if segment == "FNO" else None,
        )
        daily_targets += 1
        daily_bars += report.bars
        _LOG.info(
            "lake_heal.recall_daily",
            symbol=symbol,
            months=[f"{y:04d}-{m:02d}" for y, m in sorted(months)],
            bars=report.bars,
            errored=report.errored,
        )
        rebuilt, failed = _rebuild_tf_months(
            state, exchange=exchange, segment=segment, symbol=symbol, months=months
        )
        tf_rebuilt += rebuilt
        tf_failed += failed
    return RecallReport(
        daily_targets=daily_targets,
        daily_bars=daily_bars,
        tf_rebuilt=tf_rebuilt,
        tf_failed=tf_failed,
    )


# ── stats sidecar ────────────────────────────────────────────────────────────


def _stat_int(value: object) -> int:
    """A tolerant int read (a foreign JSON shape reads 0)."""
    return value if isinstance(value, int) else 0


def _bump_stats(
    lake_root: Path, *, healed: int, quarantined: int, now_fn: Callable[[], datetime]
) -> None:
    """Read-modify-write the cumulative heal stats sidecar, best-effort.

    Guard-FREE write (:func:`~brutex.data.atomic_io.atomic_write_simple` is
    correct here): a stats write failure must NOT masquerade as a lake halt —
    EVERY failure (even a disk-full) is swallowed + logged
    (``lake_heal.stats_write_failed``) and never fails the heal. Serialised
    across processes by the COMMON per-path flock.
    """
    stats_path = lake_root / STATS_RELPATH
    try:
        with partition_write_lock(lake_root, stats_path):
            current = load_heal_stats(lake_root)
            payload = json.dumps(
                {
                    "healed_partitions_total": _stat_int(current.get("healed_partitions_total"))
                    + healed,
                    "quarantined_files_total": _stat_int(current.get("quarantined_files_total"))
                    + quarantined,
                    "last_heal_utc": now_fn().astimezone(UTC).isoformat(),
                },
                sort_keys=True,
            )
            stats_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_simple(stats_path, lambda tmp: tmp.write_text(payload, encoding="utf-8"))
    except Exception as exc:  # pylint: disable=broad-exception-caught — best-effort sidecar
        _LOG.warning("lake_heal.stats_write_failed", error=str(exc))


def load_heal_stats(lake_root: Path) -> dict[str, Any]:
    """Read the cumulative heal stats sidecar; ``{}`` when absent/corrupt.

    Tolerant by design (the census surfaces read this): a missing / unreadable
    / non-object JSON degrades to ``{}``, never a crash.
    """
    path = lake_root / STATS_RELPATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_last_sweep(lake_root: Path) -> dict[str, Any] | None:
    """Read the L3 sweep sentinel; ``None`` when absent/corrupt (fail-open).

    A missing / unreadable / non-object sentinel means "never swept" — the
    next :func:`run_startup_sweep` runs (fail-open to CHECKING, never to
    skipping).
    """
    path = lake_root / SENTINEL_RELPATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ── the batch healer ─────────────────────────────────────────────────────────


def heal_findings(
    findings: Sequence[LakeFinding],
    *,
    lake_root: Path,
    files_scanned: int,
    mode: LakeHealMode | None = None,
    catalog: Catalog | None = None,
    audit: LakeAuditSink | None = None,
    on_report: Callable[[HealReport], None] | None = None,
    on_healed: Callable[[], None] | None = None,
    recall: bool = True,
    now: Callable[[], datetime] | None = None,
) -> HealReport:
    """Heal one batch of scanner *findings* — the ONE heal chokepoint.

    Order: LegacyKeyDrift first (dir merges change which files exist), then
    ForeignMonthRows (reassignment), then IntraFileDup, then
    OptionForeignMonthRows (options reassignment), then OptionIntraFileDup
    — sorted by path within each class. Observe mode short-circuits with ZERO
    file mutation of any kind (no quarantine dir, no locks, no catalog writes,
    no stats bump — byte-pinned); the mass-heal rail refuses a
    ``≥ MASS_HEAL_MIN_FILES ∧ > MASS_HEAL_FRACTION`` batch untouched. Each
    finding heals in per-finding isolation (a vanished/corrupt file / lock
    timeout counts ``errors`` and the batch continues);
    :class:`~brutex.data.errors.LakeWriteHalt` propagates untouched. After the
    batch: the derived-data recall (when any ``1minute`` was touched), ONE
    stats bump, ONE ``on_report`` fire, and ``on_healed`` iff ``healed > 0``.

    Parameters
    ----------
    findings
        The scanner findings to heal.
    lake_root
        The lake root (the parent of ``bars/`` + ``options/``).
    files_scanned
        The producing scan's file count — the mass-heal refusal denominator.
    mode
        Heal mode override; ``None`` resolves :func:`resolve_lake_heal_mode`.
    catalog
        When provided, healed rows are re-upserted / deleted (Golden Rule #4).
    audit
        Hash-chained audit sink (one line per healed partition), best-effort.
    on_report
        Alert seam — fires ONCE, post-batch (wiring layers pass
        ``lambda r: emit_lake_heal_alert(r, scope=...)``).
    on_healed
        Generation-bump seam — fires iff ``healed > 0`` (the dashboard passes
        ``summary_cache.mark_dirty``).
    recall
        Run the derived-data recall (1day re-derive + TF delete-and-rebuild).
    now
        Injectable clock (quarantine timestamp dir, stats stamps).

    Returns
    -------
    HealReport
        The batch outcome.
    """
    resolved: LakeHealMode = resolve_lake_heal_mode() if mode is None else mode
    now_fn = _utc_now if now is None else now
    ordered = _order_findings(findings)
    if resolved == "observe":
        report = HealReport(
            mode="observe",
            findings=len(ordered),
            healed=0,
            skipped=0,
            errors=0,
            refused_mass_heal=False,
            actions=(),
            quarantine_root=None,
            recall=None,
        )
        if ordered:
            _LOG.warning(
                "lake_heal.observe_only",
                findings=len(ordered),
                detail=[_finding_token(f) for f in ordered],
            )
            _audit_append(
                audit,
                action=AUDIT_HEAL_BATCH,
                subject="lake-heal",
                payload={"mode": "observe", "findings": len(ordered), "healed": 0},
            )
        if on_report is not None:
            on_report(report)
        return report
    affected = _affected_files(ordered)
    if files_scanned >= MASS_HEAL_MIN_FILES and affected / files_scanned > MASS_HEAL_FRACTION:
        # The sweep_heal C7 twin: a systematic scanner bug / mount mix-up must
        # never machine-rewrite half a lake. Refuse the WHOLE batch untouched.
        _LOG.error(
            "lake_heal.mass_heal_refused",
            affected_files=affected,
            files_scanned=files_scanned,
            fraction=round(affected / files_scanned, 3),
        )
        report = HealReport(
            mode=resolved,
            findings=len(ordered),
            healed=0,
            skipped=0,
            errors=0,
            refused_mass_heal=True,
            actions=(),
            quarantine_root=None,
            recall=None,
        )
        _audit_append(
            audit,
            action=AUDIT_HEAL_BATCH,
            subject="lake-heal",
            payload={
                "mode": resolved,
                "findings": len(ordered),
                "refused_mass_heal": True,
                "affected_files": affected,
                "files_scanned": files_scanned,
            },
        )
        if on_report is not None:
            on_report(report)
        return report
    quarantine_root = (
        lake_root / QUARANTINE_DIRNAME / (now_fn().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"))
    )
    state = _BatchState(
        lake_root=lake_root,
        quarantine_root=quarantine_root,
        audit=audit,
        catalog=catalog,
        writer=ParquetWriter(lake_root=lake_root),
        option_writer=OptionParquetWriter(lake_root=lake_root),
    )
    healed = 0
    skipped = 0
    errors = 0
    if ordered:
        _LOG.info(
            "lake_heal.batch_start",
            findings=len(ordered),
            files_scanned=files_scanned,
            mode=resolved,
        )
    for finding in ordered:
        try:
            did_heal = _heal_one(finding, state)
        except LakeWriteHalt:
            raise  # disk-full/OOM = pull-wide graceful stop, never swallowed
        except _PER_FINDING_HEAL_ERRORS as exc:
            errors += 1
            _LOG.warning(
                "lake_heal.partition_error",
                finding=type(finding).__name__,
                partition=_finding_token(finding),
                error=str(exc),
            )
            continue
        if did_heal:
            healed += 1
        else:
            skipped += 1
    recall_report: RecallReport | None = None
    if recall and state.touched_minute:
        recall_report = _run_recall(state, before=now_fn().astimezone(_IST).date())
    report = HealReport(
        mode=resolved,
        findings=len(ordered),
        healed=healed,
        skipped=skipped,
        errors=errors,
        refused_mass_heal=False,
        actions=tuple(state.actions),
        quarantine_root=str(quarantine_root) if state.quarantined_files > 0 else None,
        recall=recall_report,
    )
    if healed > 0:
        _bump_stats(lake_root, healed=healed, quarantined=state.quarantined_files, now_fn=now_fn)
    if on_report is not None:
        on_report(report)
    if healed > 0 and on_healed is not None:
        on_healed()
    return report


# ── the L1 + L2 seam cores ───────────────────────────────────────────────────


def heal_after_pull(
    *,
    lake_root: Path,
    targets: Sequence[tuple[str, str, str]],
    start: date,
    end: date,
    scope: str,
    catalog: Catalog | None = None,
    audit: LakeAuditSink | None = None,
    on_report: Callable[[HealReport], None] | None = None,
    on_healed: Callable[[], None] | None = None,
    include_options: bool = False,
) -> HealReport:
    """L2 post-pull heal: scan the pulled coordinates × span months, then heal.

    The scan is BOUNDED to each pulled ``(exchange, segment, symbol)`` and the
    UTC months the pulled IST span ``[start, end)`` touches
    (:func:`~brutex.data.spot_daily_resample.ist_span_partition_months` — the
    ±1-day pad already covers the midnight-IST 1day straddle). Options tree
    excluded by default; the F&O seam opts in (``include_options=True``,
    DEC-LAKE-HEAL-OPTIONS-FOREIGN-001) — a bounded, months-scoped census+heal
    of the legacy expired-options table. HONEST scope bound (review round-2
    NIT — the earlier "zero reads out of scope" claim was overbroad): the
    bound is by FILENAME, an O(1) parse, so a CLEAN out-of-scope file is
    never read; but a FLAGGED in-scope file still costs targeted
    canonical-sibling reads — and, in auto mode, reassignment writes — in its
    foreign rows' HOME months, which may lie OUTSIDE the span (anomaly-only
    cost, mirroring the bars overlap classification). Run ONCE (not per
    target — the whole tree would otherwise be walked O(targets) times; no
    current production pull WRITES ``options/``, so this is opportunistic —
    the always-on coverage is L3).
    Its ``option_files_scanned`` folds into the mass-heal denominator. Runs
    BEFORE the pull's own daily derive at the wiring seams, so the derive
    reads clean 1-minute.

    Parameters
    ----------
    lake_root
        The lake root.
    targets
        The pulled ``(exchange, segment, symbol)`` coordinates (deduplicated).
    start, end
        The pulled IST date span (half-open).
    scope
        A log/alert token, e.g. ``f"pull:{pull_id}"``.
    catalog, audit, on_report, on_healed
        Forwarded to :func:`heal_findings`.
    include_options
        ALSO scan+heal the ``options/`` tree, bounded to the span months by
        FILENAME (default ``False`` — every existing caller byte-preserved).

    Returns
    -------
    HealReport
        The batch outcome.
    """
    months = set(ist_span_partition_months(start, end))
    findings: list[LakeFinding] = []
    files_scanned = 0
    unique_targets = list(dict.fromkeys(targets))
    for exchange, segment, symbol in unique_targets:
        scan = scan_lake_invariants(
            lake_root,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            months=months,
            include_options=False,
        )
        findings.extend(scan.findings)
        files_scanned += scan.files_scanned
    option_files = 0
    if include_options:
        option_scan = scan_lake_invariants(
            lake_root, months=months, include_bars=False, include_options=True
        )
        findings.extend(option_scan.findings)
        option_files = option_scan.option_files_scanned
        # An include_bars=False scan reports files_scanned == 0 — folded anyway
        # for form; the addition puts option files in the mass-rail DENOMINATOR.
        files_scanned += option_scan.files_scanned + option_files
    _LOG.info(
        "lake_heal.post_pull_scan",
        scope=scope,
        targets=len(unique_targets),
        months=len(months),
        files=files_scanned,
        findings=len(findings),
        option_files=option_files,
    )
    return heal_findings(
        findings,
        lake_root=lake_root,
        files_scanned=files_scanned,
        catalog=catalog,
        audit=audit,
        on_report=on_report,
        on_healed=on_healed,
    )


def heal_after_reconcile(
    result: ReconcileResult,
    *,
    lake_root: Path,
    catalog: Catalog,
    audit: LakeAuditSink | None = None,
    on_report: Callable[[HealReport], None] | None = None,
    on_healed: Callable[[], None] | None = None,
) -> HealReport | None:
    """L1 heal hook: heal the findings a catalog reconcile detected for free.

    ``None`` when the reconcile found nothing (the steady-state fast path —
    zero extra work). Otherwise one :func:`heal_findings` batch over
    ``result.findings`` with the reconcile's own file count as the mass-heal
    denominator; healed coordinates are re-upserted into *catalog* (the
    rotated ``raw_hash`` reaches ``data_snapshot_hash``, Golden Rule #4).

    Parameters
    ----------
    result
        The :func:`~brutex.data.catalog_reconcile.reconcile_catalog_from_lake`
        outcome (its L1 piggyback populates ``findings``).
    lake_root
        The lake root the reconcile walked.
    catalog
        The catalog the reconcile upserted into (required — L1 exists to keep
        the sweep's catalog honest).
    audit, on_report, on_healed
        Forwarded to :func:`heal_findings`.

    Returns
    -------
    HealReport | None
        The batch outcome, or ``None`` for a clean reconcile.
    """
    if not result.findings:
        return None
    return heal_findings(
        result.findings,
        lake_root=lake_root,
        files_scanned=result.partitions_reconciled,
        catalog=catalog,
        audit=audit,
        on_report=on_report,
        on_healed=on_healed,
    )


# ── L3 startup sweep ─────────────────────────────────────────────────────────


def lake_content_token(lake_root: Path) -> str:
    """A sha256 content token over every ``*.parquet`` under ``bars/`` + ``options/``.

    Hashes the sorted ``relpath|size|mtime_ns`` lines — an O(files) stat walk
    with NO file reads. Any add / remove / rewrite (a pull, a derive, a heal, a
    manual copy) moves the token, so the sentinel comparison re-arms the sweep.
    """
    lines: list[str] = []
    for tree in (BAR_DIR_NAME, OPTIONS_DIR_NAME):
        base = lake_root / tree
        if not base.is_dir():
            continue
        for path in base.rglob("*.parquet"):
            stat = path.stat()
            lines.append(
                f"{path.relative_to(lake_root).as_posix()}|{stat.st_size}|{stat.st_mtime_ns}"
            )
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StartupSweepResult:
    """Outcome of one :func:`run_startup_sweep` invocation.

    Attributes
    ----------
    ran
        ``False`` = the sentinel token matched (no-op).
    token
        The lake content token the sweep observed (POST-heal when it ran).
    scan, report
        The scan + heal outcomes (``None`` on a token-match no-op).
    """

    ran: bool
    token: str
    scan: LakeScanReport | None
    report: HealReport | None


def _sentinel_skip_reason(report: HealReport) -> str | None:
    """Why the L3 sentinel must NOT be stamped, or ``None`` when it may be.

    The sentinel means "this lake content token was swept AND left clean" —
    stamping it after a refused / observe / errored / partially-healed batch
    would token-match the NEXT boot past live poison (self-healing + the
    one-shot alert would silently stop until unrelated lake content moved the
    token). Stamp ONLY a genuinely-completed auto sweep: every finding healed
    with zero errors (a zero-finding clean sweep trivially qualifies).
    """
    if report.mode != "auto":
        return "observe_mode"
    if report.refused_mass_heal:
        return "mass_heal_refused"
    if report.errors > 0:
        return "heal_errors"
    if report.healed != report.findings:
        return "unhealed_findings"
    return None


def run_startup_sweep(
    lake_root: Path,
    *,
    force: bool = False,
    per_file_pause: Callable[[], None] | None = None,
    catalog: Catalog | None = None,
    audit: LakeAuditSink | None = None,
    on_report: Callable[[HealReport], None] | None = None,
    on_healed: Callable[[], None] | None = None,
    now: Callable[[], datetime] | None = None,
) -> StartupSweepResult:
    """The L3 one-time whole-lake sweep: scan + heal + restamp the sentinel.

    ONE-TIME semantics: when the stored sentinel's ``lake_token`` equals the
    current :func:`lake_content_token` (and *force* is False) the sweep is a
    no-op (``lake_heal.sweep_skip_token_match``). Otherwise it runs ONE full
    :func:`~brutex.data.lake_invariants.scan_lake_invariants` (throttled by
    *per_file_pause*, default ``time.sleep(resolve_sweep_throttle_s())``), one
    :func:`heal_findings` batch, and stamps :data:`SENTINEL_RELPATH` with the
    POST-heal recomputed token (a heal rewrites files, so the pre-scan token is
    stale by design). The sentinel is stamped ONLY when the sweep genuinely
    completed its job — ``mode == "auto"``, not mass-heal-refused, zero
    ``errors``, and ``healed == findings`` (a zero-finding clean sweep also
    stamps). A refused / observe-mode / errored / partially-healed sweep SKIPS
    the stamp (``lake_heal.sweep_sentinel_skipped`` with the reason) so the
    next boot re-attempts instead of token-matching past live poison. Cost
    honesty (Golden Rule #10): O(files) total with a per-file key-column read
    that scales with row count — NOT O(1); paid once per lake content token.

    Parameters
    ----------
    lake_root
        The lake root.
    force
        Sweep even when the sentinel token matches.
    per_file_pause
        The per-file throttle; ``None`` = sleep
        :func:`resolve_sweep_throttle_s` seconds per file.
    catalog, audit, on_report, on_healed, now
        Forwarded to :func:`heal_findings`.

    Returns
    -------
    StartupSweepResult
        ``ran=False`` + the matching token, or the scan + heal outcomes.
    """
    now_fn = _utc_now if now is None else now
    token = lake_content_token(lake_root)
    sentinel = load_last_sweep(lake_root)
    if not force and sentinel is not None and sentinel.get("lake_token") == token:
        _LOG.info("lake_heal.sweep_skip_token_match", token=token)
        return StartupSweepResult(ran=False, token=token, scan=None, report=None)
    pause = per_file_pause
    if pause is None:
        throttle = resolve_sweep_throttle_s()

        def _sleep_pause() -> None:
            time.sleep(throttle)

        pause = _sleep_pause
    scan = scan_lake_invariants(lake_root, per_file_pause=pause)
    files_scanned = scan.files_scanned + scan.option_files_scanned
    report = heal_findings(
        scan.findings,
        lake_root=lake_root,
        files_scanned=files_scanned,
        catalog=catalog,
        audit=audit,
        on_report=on_report,
        on_healed=on_healed,
        now=now,
    )
    post_token = lake_content_token(lake_root)
    skip_reason = _sentinel_skip_reason(report)
    if skip_reason is None:
        sentinel_path = lake_root / SENTINEL_RELPATH
        sentinel_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "version": 1,
                "lake_token": post_token,
                "completed_utc": now_fn().astimezone(UTC).isoformat(),
                "files_scanned": files_scanned,
                "findings": len(scan.findings),
                "healed": report.healed,
                "mode": report.mode,
            },
            sort_keys=True,
        )
        atomic_write_simple(sentinel_path, lambda tmp: tmp.write_text(payload, encoding="utf-8"))
    else:
        # The sweep did NOT complete its job — leave the old sentinel (or none)
        # in place so the next boot's token check re-attempts the sweep.
        _LOG.warning(
            "lake_heal.sweep_sentinel_skipped",
            reason=skip_reason,
            mode=report.mode,
            findings=report.findings,
            healed=report.healed,
            errors=report.errors,
        )
    _LOG.info(
        "lake_heal.sweep_complete",
        files=files_scanned,
        findings=len(scan.findings),
        healed=report.healed,
        token=post_token,
    )
    return StartupSweepResult(ran=True, token=post_token, scan=scan, report=report)


def open_lake_heal_audit(lake_root: Path) -> LakeAuditSink | None:
    """Best-effort open of the hash-chained lake-heal audit JSONL, or ``None``.

    ``AuditWriter.open('<lake>/audit/lake-heal.jsonl', resume=True)`` — resume
    continues an existing chain; a missing file starts a fresh one. The
    orchestrator import is FUNCTION-LOCAL (the ``window_ledger_store``
    precedent) so :mod:`brutex.data` gains no module-level orchestrator edge.
    An open failure logs ``lake_heal.audit_open_failed`` and returns ``None``
    — the heal proceeds un-audited rather than not at all (the optional-audit
    ingest precedent).
    """
    try:
        from brutex.orchestrator.audit import AuditWriter

        path = lake_root / "audit" / "lake-heal.jsonl"
        return AuditWriter.open(path, resume=True)
    except Exception as exc:  # pylint: disable=broad-exception-caught — best-effort convenience
        _LOG.warning("lake_heal.audit_open_failed", error=str(exc))
        return None


@contextlib.contextmanager
def lake_heal_audit(lake_root: Path) -> Generator[LakeAuditSink | None, None, None]:
    """Context-managed :func:`open_lake_heal_audit` — the ONE close idiom.

    Yields the best-effort sink (or ``None``) and CLOSES it on exit when it
    exposes a callable ``close`` (a real ``AuditWriter`` holds an open file
    handle; leaking it trips ``-W error`` ``ResourceWarning``s and leaves the
    chain tail unflushed). Shared by every heal seam — the L1 reconcile
    wiring, the L2 post-pull glue, and the L3 :class:`StartupSweeper` — so no
    seam hand-rolls the ``getattr(..., "close")`` dance (CLAUDE.md §11 COMMON).
    """
    audit = open_lake_heal_audit(lake_root)
    try:
        yield audit
    finally:
        close = getattr(audit, "close", None)
        if callable(close):
            close()


def open_lake_catalog(lake_root: Path) -> Catalog | None:
    """Best-effort open of ``<lake>/catalog.duckdb`` — ONLY when the file exists.

    The L2 post-pull seams' twin of the L3 :class:`StartupSweeper`'s
    ``catalog_path`` wiring: passing the returned catalog to
    :func:`heal_after_pull` lets a healed partition's rotated ``raw_hash``
    reach the catalog (and so ``data_snapshot_hash``, Golden Rule #4)
    IMMEDIATELY instead of waiting for the next L1 reconcile. A lake with NO
    catalog file (a CLI-only lake) gets ``None`` — the heal never conjures a
    catalog into existence; any open error logs
    ``lake_heal.catalog_open_failed`` and degrades to ``None`` (a catalog
    problem must never block a pull). The caller owns the close.
    """
    path = lake_root / "catalog.duckdb"
    if not path.exists():
        return None
    try:
        return Catalog.open(path)
    except Exception as exc:  # pylint: disable=broad-exception-caught — best-effort seam
        _LOG.warning("lake_heal.catalog_open_failed", path=str(path), error=str(exc))
        return None


def _default_spawn(body: Callable[[], None]) -> None:
    """Run *body* on a named daemon thread (the production sweep lifecycle)."""
    threading.Thread(target=body, name="brutex-lake-heal-sweep", daemon=True).start()


class StartupSweeper:
    """Daemon-thread lifecycle over :func:`run_startup_sweep` (RatePanelSampler-shaped).

    The dashboard boot constructs one and calls :meth:`start`; the injectable
    *spawn* seam lets tests run the body synchronously (no real thread under
    coverage). :meth:`run_sync` is the 100%-tested synchronous core — blanket-
    guarded so boot can NEVER crash on a sweep failure.

    Parameters
    ----------
    lake_root
        The lake root to sweep.
    on_healed
        Generation-bump seam (the dashboard passes ``summary_cache.mark_dirty``).
    catalog_path
        When given, a :class:`~brutex.data.catalog.Catalog` is opened there for
        the sweep and closed after (healed rows rotate their ``raw_hash``).
    spawn
        Thread-spawn seam; ``None`` = a named daemon thread.
    """

    def __init__(
        self,
        lake_root: Path,
        *,
        on_healed: Callable[[], None] | None = None,
        catalog_path: Path | None = None,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._lake_root = lake_root
        self._on_healed = on_healed
        self._catalog_path = catalog_path
        self._spawn = spawn

    def run_sync(self) -> StartupSweepResult | None:
        """The blanket-guarded body the thread calls: any exception → ``None``.

        NEVER raises (``lake_heal.sweep_failed`` WARNING instead) — the ONE
        blanket guard in the healer, whose job is "boot can never crash".
        """
        try:
            with lake_heal_audit(self._lake_root) as audit:
                catalog = None if self._catalog_path is None else Catalog.open(self._catalog_path)
                try:
                    return run_startup_sweep(
                        self._lake_root,
                        catalog=catalog,
                        audit=audit,
                        on_report=_startup_sweep_alert,
                        on_healed=self._on_healed,
                    )
                finally:
                    if catalog is not None:
                        catalog.close()
        except Exception as exc:  # pylint: disable=broad-exception-caught — boot never crashes
            _LOG.warning("lake_heal.sweep_failed", error=str(exc))
            return None

    def start(self) -> None:
        """Launch :meth:`run_sync` via the spawn seam (default: daemon thread)."""
        spawn = _default_spawn if self._spawn is None else self._spawn
        spawn(self._run)

    def _run(self) -> None:
        """The spawned body — just the guarded synchronous core."""
        self.run_sync()


def _startup_sweep_alert(report: HealReport) -> None:
    """The L3 alert binding: one best-effort Telegram alert per sweep batch.

    Function-local notifications import (the :func:`open_lake_heal_audit`
    precedent) — ``emit_lake_heal_alert`` is itself blanket-guarded + a silent
    no-op when Telegram is disabled, so this can never fail the sweep.
    """
    from brutex.notifications.sweep_alerts import emit_lake_heal_alert

    emit_lake_heal_alert(report, scope="startup-sweep")


__all__ = [
    "AUDIT_HEAL_BATCH",
    "AUDIT_HEAL_DEDUP",
    "AUDIT_HEAL_DRIFT_MERGE",
    "AUDIT_HEAL_QUARANTINE_FILE",
    "AUDIT_HEAL_REASSIGN",
    "DEFAULT_LAKE_HEAL_MODE",
    "DEFAULT_SWEEP_THROTTLE_S",
    "LAKE_HEAL_ENV",
    "LAKE_HEAL_THROTTLE_ENV",
    "MASS_HEAL_FRACTION",
    "MASS_HEAL_MIN_FILES",
    "QUARANTINE_DIRNAME",
    "SENTINEL_RELPATH",
    "STATS_RELPATH",
    "HealAction",
    "HealReport",
    "LakeAuditSink",
    "RecallReport",
    "StartupSweepResult",
    "StartupSweeper",
    "heal_after_pull",
    "heal_after_reconcile",
    "heal_findings",
    "lake_content_token",
    "lake_heal_audit",
    "load_heal_stats",
    "load_last_sweep",
    "open_lake_catalog",
    "open_lake_heal_audit",
    "resolve_lake_heal_mode",
    "resolve_sweep_throttle_s",
    "run_startup_sweep",
]
