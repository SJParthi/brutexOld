"""The automatic keep-FIRST lake healer + derived-data recall (DEC-LAKE-HEAL-001).

The ACTION half of DEC-LAKE-HEAL-001 (the detection half is
:mod:`brutex.data.lake_invariants`): given the scanner's findings, heal each one
through the NORMAL write path — both partition locks in the writers' order →
quarantine-COPY the original to ``<lake>/.quarantine/<UTC-ts>/<lake-relative
path>`` (a ``-NN`` serial suffix on the second-granular ``<UTC-ts>`` when a
same-second earlier batch already created the dir — the earlier batch's
before-image is never overwritten) → mode-independent keep-FIRST
row-removing/row-REASSIGNING transform →
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
machine-rewrite half a lake); a SYMLINKED ``<lake>/.quarantine`` refuses the
whole batch untouched (round-17, DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 — the
quarantine BEFORE-image write is a heal-side mutation too, so it must never
follow a pre-planted link outside the lake root; every finding counts an
error so the L3 sentinel never stamps and the next boot re-attempts; a
DANGLING symlink at the stamp dir is serialed past like an existing dir;
honest envelope: a probe→write swap inside ONE batch is the same TOCTOU
class as the same-second cross-process race below — not claimed closed);
:class:`~brutex.data.errors.LakeWriteHalt`
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
import json
import math
import os
import threading
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast

import structlog

from brutex.calendars.nse import IST_FIXED_OFFSET, daily_derive_cutoff
from brutex.config.rust_strict import RustFallbackRefused
from brutex.data.atomic_io import atomic_write_simple, atomic_write_via, lake_write_guard
from brutex.data.catalog import Catalog
from brutex.data.derive_stamp import remove_derive_stamp
from brutex.data.errors import (
    LakeWriteHalt,
    PartitionLockTimeout,
    WriterError,
    datetime_out_of_range_escape_errors,
    parquet_read_skip_errors,
)
from brutex.data.fno_candle_lake import is_option_contract_coords
from brutex.data.fno_daily_resample import resample_1m_to_fno_daily_positional
from brutex.data.fno_symbol import parse_fno_symbol
from brutex.data.guarded_read import read_regular_bytes, read_regular_text
from brutex.data.lake_invariants import (
    OPTIONS_DIR_NAME,
    ForeignMonthRows,
    IntraFileDup,
    LakeFinding,
    LakeScanReport,
    LegacyKeyDrift,
    OptionForeignMonthRows,
    OptionIntraFileDup,
    PriceSanityRows,
    SchemaAlienFile,
    scan_lake_invariants,
)
from brutex.data.lake_manifest import invalidate_manifest, record_dataset
from brutex.data.lake_stats_manifest import (
    KIND_BARS,
    KIND_OPTIONS,
    remove_dataset_stats,
    stats_manifest_enabled,
)
from brutex.data.option_parquet_writer import OptionParquetWriter, OptionWriterError
from brutex.data.parquet_writer import ParquetWriter, WriteResult
from brutex.data.partition import (
    BAR_DIR_NAME,
    PARTITION_PATH_COMPONENTS,
    option_partition_stem,
)
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
from brutex.observability.progress_beat import emit_progress_beat

if TYPE_CHECKING:  # pragma: no cover
    from brutex.data.catalog_reconcile import ReconcileResult

_LOG = structlog.get_logger("brutex.data.lake_heal")

#: IST is a fixed +05:30 offset (no DST) — the forming-session cutoff timezone;
#: the ONE canonical :data:`brutex.calendars.nse.IST_FIXED_OFFSET` root
#: (DEC-TIMESTAMP-IST-SINGLESOURCE-001).
_IST: Final[timezone] = IST_FIXED_OFFSET

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

#: The L3 sweep ATTEMPT record, relative to the lake root — a SIBLING of the
#: clean sentinel (DEC-LAKE-INVARIANTS-PREINDEX-001). Written when a sweep ran
#: but did NOT stamp the sentinel (:func:`_sentinel_skip_reason` non-``None``),
#: so the next boot can distinguish "this exact lake content was already
#: attempted and deterministically cannot complete" (observe mode / mass-heal
#: refusal — honest skip with a loud WARNING) from "it failed, retry after a
#: bounded backoff" (heal errors / unhealed findings). The sentinel's meaning
#: ("this token was swept AND left clean") is untouched.
ATTEMPT_RELPATH: Final[str] = ".lake_heal/last_attempt.json"

#: The transient-failure re-attempt backoff env var (float seconds, ≥ 0).
LAKE_HEAL_RETRY_ENV: Final[str] = "BRUTEX_LAKE_HEAL_RETRY_S"

#: Default re-attempt backoff after a transient non-stamp (heal errors /
#: unhealed findings): retried DAILY instead of on every boot — the full
#: O(files) scan is a pure re-cost while the lake content is unchanged.
DEFAULT_LAKE_HEAL_RETRY_S: Final[float] = 86_400.0

#: Attempt reasons that are DETERMINISTIC functions of (lake token, heal
#: mode): re-running the scan on an unchanged token + unchanged mode provably
#: reproduces the same non-stamp — the boot skips it with a loud WARNING.
_DETERMINISTIC_ATTEMPT_REASONS: Final[frozenset[str]] = frozenset(
    {"observe_mode", "mass_heal_refused"}
)

#: Attempt reasons that may be TRANSIENT (I/O): re-attempted after the
#: :data:`LAKE_HEAL_RETRY_ENV` backoff (or immediately on any token move).
_TRANSIENT_ATTEMPT_REASONS: Final[frozenset[str]] = frozenset({"heal_errors", "unhealed_findings"})

#: The cumulative heal stats sidecar, relative to the lake root.
STATS_RELPATH: Final[str] = ".lake_heal/stats.json"

#: Mass-heal refusal rail (the ``sweep_heal`` C7 twin): refuse a batch that
#: would rewrite > this fraction of the scanned files …
MASS_HEAL_FRACTION: Final[float] = 0.5

#: … once at least this many files were scanned (a tiny lake never trips it).
MASS_HEAL_MIN_FILES: Final[int] = 20

#: December — the year-rollover month for :func:`_first_day_after`.
_DECEMBER: Final[int] = 12

#: Number of components in a canonical options-tree partition path relative
#: to the lake root: ``options / <series> / <YYYY>-<MM>.parquet`` (the flat
#: layout ``option_parquet_writer`` renders). The bars-tree twin is
#: :data:`~brutex.data.partition.PARTITION_PATH_COMPONENTS`.
_OPTIONS_PATH_COMPONENTS: Final[int] = 3

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


def resolve_heal_retry_s() -> float:
    """Resolve the transient-non-stamp re-attempt backoff (seconds), tolerantly.

    Unset / blank / unparsable / NON-FINITE → :data:`DEFAULT_LAKE_HEAL_RETRY_S`;
    a negative value clamps to ``0.0`` (= re-attempt on every boot, the pre-fix
    cadence). The :func:`resolve_sweep_throttle_s` parse contract, applied to
    :data:`LAKE_HEAL_RETRY_ENV` (DEC-LAKE-INVARIANTS-PREINDEX-001). The
    non-finite arm is the review fold: ``inf`` parses as a valid float yet
    means "skip forever within one token" — violating the "any ambiguity →
    re-run" law — and ``nan`` poisons the elapsed compare; both take the
    junk-value treatment (the default), never a silent forever-skip.

    Returns
    -------
    float
        The backoff in seconds (≥ 0, finite).
    """
    raw = os.environ.get(LAKE_HEAL_RETRY_ENV, "").strip()
    if not raw:
        return DEFAULT_LAKE_HEAL_RETRY_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_LAKE_HEAL_RETRY_S
    if not math.isfinite(value):
        return DEFAULT_LAKE_HEAL_RETRY_S
    return max(0.0, value)


# ── audit seam ───────────────────────────────────────────────────────────────


class LakeAuditSink(Protocol):
    """The ``append`` slice of the hash-chained audit writer the healer uses.

    An injected seam so the data layer never imports
    :mod:`brutex.orchestrator` at module level (the same ``append`` slice
    :class:`~brutex.orchestrator.audit.AuditWriter` satisfies).
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
    option_daily_skipped
        Healed CE/PE OPTION contracts whose ``1day`` recall was SKIPPED by
        the 1-minute-native law (DEC-OPTIONS-PREBUILD-RETIRE-001 — the
        operator ruling of 2026-07-15 as relayed via the coordinator,
        ADR-0021 RULING B, draft PR #1555): the derived option ``1day`` is
        retired, so a healed option partition gets NO daily re-derive —
        counted here + logged ONCE per batch, never silently dropped.
    """

    daily_targets: int
    daily_bars: int
    tf_rebuilt: int
    tf_failed: int
    option_daily_skipped: int = 0


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
#: :func:`~brutex.data.errors.datetime_out_of_range_escape_errors` joins the set
#: (round-18 MAJOR fold): a stored out-of-``datetime``-range timestamp faults
#: every Python-side conversion — ``OverflowError`` from ``min``/``max`` and
#: the **BaseException** ``pyo3_runtime.PanicException`` from ``to_list``,
#: which escapes even the L3 ``except Exception`` daemon guard (killing the
#: healer thread past the "boot never crashes" contract). The gate
#: (:func:`~brutex.data.lake_invariants.option_key_dtype_error` value probe)
#: refuses such a file FIRST at the consistent read + the writer's keyed
#: re-read; this membership is the defense-in-depth rail so any residual
#: conversion fault is ONE counted error, never a whole-batch abort.
#: W-6a endgame: the per-finding Python loop that caught this set was
#: DELETED — the crate's ``HealErr::Isolated`` arm (``waved/heal.rs``) is the
#: live twin of exactly this membership; the constant stays as the documented
#: isolation LAW the crate mirrors (drift-pinned by the heal suites).
_PER_FINDING_HEAL_ERRORS: Final[tuple[type[BaseException], ...]] = (
    WriterError,
    OptionWriterError,
    PartitionLockTimeout,
    *parquet_read_skip_errors(),
    *datetime_out_of_range_escape_errors(),
)

#: The L3 boot blanket (``StartupSweeper.run_sync``): ``Exception`` PLUS the
#: conversion-fault types that ESCAPE it — round-18 fold: polars' out-of-range
#: datetime conversion PANICS as ``pyo3_runtime.PanicException``, a
#: BaseException that sailed past the ``except Exception`` guard and killed
#: the sweep daemon thread (the "boot never crashes" contract). The scan gate
#: + ``_PER_FINDING_HEAL_ERRORS`` stop it upstream; this is the last-line rail
#: (OverflowError is already an ``Exception`` — the fold only ADDS the panic).
#: ALSO consumed by ``_rebuild_tf_months``'s per-symbol isolation
#: (DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001): the recall's months-bounded
#: re-materialise converts bars Python-side, so the SAME panic class escaped
#: its former bare ``except Exception`` and aborted the whole recall batch.
#: A named constant because mypy --strict rejects a starred-unpack tuple
#: DISPLAY in an ``except`` clause while accepting a tuple-typed expression.
_SWEEP_GUARD_ERRORS: Final[tuple[type[BaseException], ...]] = (
    Exception,
    *datetime_out_of_range_escape_errors(),
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


#: The finding kinds the healer can ACT on. PriceSanityRows is deliberately
#: absent (observe-only, DEC-STORED-DATA-VALIDATION-001) — the type system
#: makes routing it into a healer impossible, not merely untested.
#: SchemaAlienFile IS healable (DEC-ALIEN-SCHEMA-READ-HEAL-001): its heal is a
#: whole-file quarantine MOVE — strictly file-quarantining, never
#: value-editing (a machine "repair" of an alien schema would fabricate data).
type _HealableFinding = (
    LegacyKeyDrift
    | ForeignMonthRows
    | IntraFileDup
    | OptionForeignMonthRows
    | OptionIntraFileDup
    | SchemaAlienFile
)


def _order_findings(findings: Sequence[_HealableFinding]) -> tuple[_HealableFinding, ...]:
    """Heal order: drift → foreign → dup → option-foreign → option-dup → alien, by path.

    A drift merge changes which files exist, so it heals FIRST; the later
    per-partition healers find consumed paths gone and skip them (counted
    ``skipped``, re-found next scan if still relevant — idempotent
    convergence, never a double-heal). Option-foreign heals BEFORE option-dup
    for the same reason bars foreign precedes dup: the reassignment's
    remove-rewrite also collapses the source file's dups, so a same-file
    ``OptionIntraFileDup`` afterwards reads ``changed=False`` → counted
    ``skipped``, never a double-heal and never a second quarantine copy
    (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001). The alien whole-file quarantine
    (DEC-ALIEN-SCHEMA-READ-HEAL-001) runs LAST — it removes a file the other
    healers never read (they read canonical-schema columns the alien file
    lacks), and it still lands BEFORE the post-batch derived-data recall, so
    a 1-minute alien file is gone before any re-derive reads the tree.
    """

    def key(finding: _HealableFinding) -> tuple[int, str]:
        if isinstance(finding, LegacyKeyDrift):
            return (0, str(finding.drifted_dir))
        if isinstance(finding, ForeignMonthRows):
            return (1, str(finding.path))
        if isinstance(finding, IntraFileDup):
            return (2, str(finding.path))
        if isinstance(finding, OptionForeignMonthRows):
            return (3, str(finding.path))
        if isinstance(finding, OptionIntraFileDup):
            return (4, str(finding.path))
        return (5, str(finding.path))

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
    """A short human token naming one finding (error-log subject).

    Option-tree tokens ride via ``repr()`` — ``finding.path`` embeds the RAW
    vendor-derived ``options/<series>/`` dir name, and
    ``OptionParquetWriter._safe_segment`` does not strip control characters,
    so a newline/ANSI series name must not forge lines in a rendered
    structlog stream (the #907 convention; the ``lake_heal.partition_error``
    site is precisely the attacker-triggerable one — it fires on the
    containment refusal / a corrupt partition / a lock timeout). The bars
    tokens stay raw: catalogued coordinates, not vendor-derived dir names.
    """
    if isinstance(finding, LegacyKeyDrift):
        return f"{finding.exchange}/{finding.segment}/{finding.drifted_symbol}"
    if isinstance(finding, OptionForeignMonthRows | OptionIntraFileDup):
        return repr(str(finding.path))
    return str(finding.path)


def _relpath(lake_root: Path, path: Path) -> str:
    """*path*'s lake-relative POSIX string (audit subjects)."""
    return path.relative_to(lake_root).as_posix()


def _stats_dataset_coords(lake_root: Path, source: Path) -> tuple[str, dict[str, str]] | None:
    """Derive *source*'s stats-manifest ``(kind, key)`` from its on-disk path.

    DEC-LAKE-STATS-MANIFEST-001: a bars-tree partition
    (``bars/<exch>/<seg>/<sym>/<tf>/<yyyy>/<mm>.parquet``) yields
    ``("bars", {exchange, segment, symbol, interval})``; an options-tree
    partition (``options/<series>/<YYYY>-<MM>.parquet``) yields
    ``("options", {"series": ...})`` — every component VERBATIM as it appears
    on disk (a #911-drift dir stays its own dataset). Any other shape —
    outside the lake, wrong tree, wrong depth — returns ``None`` (the hook
    abstains; only these two trees carry stats shards). O(1).
    """
    try:
        rel = source.relative_to(lake_root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) == PARTITION_PATH_COMPONENTS and parts[0] == BAR_DIR_NAME:
        return (
            KIND_BARS,
            {"exchange": parts[1], "segment": parts[2], "symbol": parts[3], "interval": parts[4]},
        )
    if len(parts) == _OPTIONS_PATH_COMPONENTS and parts[0] == OPTIONS_DIR_NAME:
        return (KIND_OPTIONS, {"series": parts[1]})
    return None


def _remove_dataset_stats_for(lake_root: Path, source: Path) -> None:
    """Unlink the stats-manifest shard of the dataset *source* belongs to.

    The heal-chokepoint twin of ``invalidate_manifest``
    (DEC-LAKE-STATS-MANIFEST-001): a whole-file quarantine / TF
    delete-and-rebuild removes stored rows the shard still counts, so the
    WHOLE shard is dropped — the rebuild's writes flow back through the
    writers' record hooks and the read-side seed re-accretes. Best-effort +
    idempotent (:func:`~brutex.data.lake_stats_manifest.remove_dataset_stats`
    never raises; kill switch off ⇒ no-op); an underivable path abstains.
    """
    coords = _stats_dataset_coords(lake_root, source)
    if coords is None:
        return
    kind, key = coords
    remove_dataset_stats(lake_root, kind=kind, key=key)


def _quarantine_move(state: _BatchState, source: Path) -> Path:
    """Quarantine-COPY *source* then unlink it (a whole-file move, audited)."""
    dest = state.quarantine_path(source)
    content = read_regular_bytes(source)
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
    # The stats manifest's twin hook (DEC-LAKE-STATS-MANIFEST-001): the moved
    # file's dataset shard is unlinked WHOLE beside the dataset-manifest
    # invalidation — a per-row edit would leave prefix sums lying about the
    # months a delete-and-rebuild is about to replace. Best-effort +
    # idempotent; re-accretes via the writers' record hooks + read-side seed.
    _remove_dataset_stats_for(state.lake_root, source)
    state.quarantined_files += 1
    _LOG.warning("lake_heal.file_quarantined", source=str(source), quarantine=str(dest))
    _audit_append(
        state.audit,
        action=AUDIT_HEAL_QUARANTINE_FILE,
        subject=_relpath(state.lake_root, source),
        payload={"quarantine": str(dest)},
    )
    return dest


# ── the Wave-D Rust heal seam (DEC-RUST-LAKE-WAVED-001, design §B2) ──────────


def _serialize_rust_findings(
    ordered: Sequence[_HealableFinding],
) -> list[dict[str, object]]:
    """Project *ordered* into the crate's plain-dict finding wire (deviation D-10).

    W-2b (deviation D-22) closes the W-2 unported set: ALL SIX healable kinds
    now serialize — ``LegacyKeyDrift`` maps ``drifted_dir``→``path`` /
    ``drifted_symbol``→``symbol`` / ``canonical_symbol``→``canonical`` (the
    crate ``Finding`` wire is path-keyed), ``ForeignMonthRows`` rides its
    coords, ``OptionForeignMonthRows`` its stem-month ``year``/``month``. The
    crate-side preflight stays the zero-mutation degrade authority for
    out-of-envelope SHAPES (read probes), and its unknown-kind arm stays as
    defense in depth against a foreign future kind.
    """
    out: list[dict[str, object]] = []
    for finding in ordered:
        if isinstance(finding, IntraFileDup):
            coords = finding.coords
            out.append(
                {
                    "kind": "IntraFileDup",
                    "path": str(finding.path),
                    "exchange": coords.exchange,
                    "segment": coords.segment,
                    "symbol": coords.symbol,
                    "interval": coords.interval,
                    "year": coords.year,
                    "month": coords.month,
                    "has_coords": True,
                }
            )
        elif isinstance(finding, OptionIntraFileDup):
            out.append(
                {
                    "kind": "OptionIntraFileDup",
                    "path": str(finding.path),
                    "series": finding.series,
                    "has_coords": False,
                }
            )
        elif isinstance(finding, ForeignMonthRows):
            coords = finding.coords
            out.append(
                {
                    "kind": "ForeignMonthRows",
                    "path": str(finding.path),
                    "exchange": coords.exchange,
                    "segment": coords.segment,
                    "symbol": coords.symbol,
                    "interval": coords.interval,
                    "year": coords.year,
                    "month": coords.month,
                    "has_coords": True,
                }
            )
        elif isinstance(finding, OptionForeignMonthRows):
            out.append(
                {
                    "kind": "OptionForeignMonthRows",
                    "path": str(finding.path),
                    "series": finding.series,
                    "year": finding.year,
                    "month": finding.month,
                    "has_coords": False,
                }
            )
        elif isinstance(finding, LegacyKeyDrift):
            # The path-keyed wire: `drifted_dir` rides the `path` slot; the
            # DRIFTED symbol rides `symbol` (its legacy rows are the ones
            # moved) and the canonical target rides the D-22 `canonical` slot.
            out.append(
                {
                    "kind": "LegacyKeyDrift",
                    "path": str(finding.drifted_dir),
                    "exchange": finding.exchange,
                    "segment": finding.segment,
                    "symbol": finding.drifted_symbol,
                    "canonical": finding.canonical_symbol,
                    "has_coords": False,
                }
            )
        else:
            record: dict[str, object] = {
                "kind": "SchemaAlienFile",
                "path": str(finding.path),
                "reason": finding.reason,
                "series": finding.series or "",
                "has_coords": finding.coords is not None,
            }
            if finding.coords is not None:
                record.update(
                    {
                        "exchange": finding.coords.exchange,
                        "segment": finding.coords.segment,
                        "symbol": finding.coords.symbol,
                        "interval": finding.coords.interval,
                        "year": finding.coords.year,
                        "month": finding.coords.month,
                    }
                )
            out.append(record)
    return out


def _rust_heal_epoch_us(us: int) -> datetime:
    """A UTC-aware datetime from epoch µs — exact integer arithmetic.

    The crate's bars envelope admits only ``us``-unit UTC-aware timestamps
    (``read_bars_file``), so the reconstruction is lossless by construction
    (``fromtimestamp(us / 1e6)`` would round through a float — never used).
    """
    return _EPOCH_UTC + timedelta(microseconds=us)


#: The Unix epoch, UTC-aware (the µs-reconstruction anchor).
_EPOCH_UTC: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)


def _rust_heal_on_event(state: _BatchState) -> Callable[[str, dict[str, Any]], None]:
    """Build the ONE ``on_event(kind, payload)`` replay channel for the crate batch.

    Wave-D house laws i-iii: the crate streams STRUCTURED side-effect records
    and this closure replays each through the SAME Python surface the
    verbatim loop uses — the pinned structlog sites (WARN default; the
    ``level="info"`` wire carries the writer's ``lake.partition.write``
    telemetry at its default-level INFO; the #907
    ``repr()`` law applied exactly where the Python arm applies it — the
    single-field ``partition_repr`` W-2 form plus the W-2b ``repr_fields``
    name-list generalization), the
    best-effort hash-chained audit sink (:func:`_audit_append`), the catalog
    upsert/delete (Golden Rule #4 — via the writer's own
    ``make_catalog_entry`` so the ``year``/``month`` derivation stays ONE
    law), the writers' stats-manifest hooks and the dataset-manifest
    invalidation. ZERO new laws: every handler DELEGATES; the two private
    hook accesses are documented friend access (the §11 no-copy alternative
    — re-minting the probe-first gating here would be the forbidden second
    copy). An unknown kind raises loudly (refuse-poison — a foreign crate
    must never silently drop a side effect). O-cost note: the
    ``option_stats_record`` replay re-reads the just-rewritten file once to
    feed the verbatim ``_record_option_stats`` law (anomaly-gated — one
    bounded read per actually-healed options file; skipped entirely when the
    stats manifest is disabled).
    """

    def _write_result(payload: dict[str, Any], *, dest: Path) -> WriteResult:
        return WriteResult(
            path=dest,
            row_count=int(payload["rows"] if "rows" in payload else payload["row_count"]),
            raw_hash=str(payload["raw_hash"]),
            first_ts=_rust_heal_epoch_us(int(payload["first_ts_us"])),
            last_ts=_rust_heal_epoch_us(int(payload["last_ts_us"])),
            bytes_written=0,  # unread by both consumers (stats hook + catalog entry)
            bars_added=0,  # a heal never adds bars (Golden Rule #1)
            conflicts=(),
            file_rewritten=True,
        )

    def handle(kind: str, payload: dict[str, Any]) -> None:
        if kind == "beat":
            emit_progress_beat()
        elif kind == "log":
            fields = {
                key: value
                for key, value in payload.items()
                if key not in ("event", "partition_repr", "level", "repr_fields")
            }
            event = str(payload["event"])
            if payload.get("partition_repr"):
                # The #907 law: an options-arm partition token embeds the RAW
                # vendor-derived series dir name — repr()'d at the LOG site
                # exactly as the Python arm does (HealAction/audit copies raw).
                fields["partition"] = repr(str(fields["partition"]))
            for name in cast("list[str]", payload.get("repr_fields", [])):
                # The W-2b generalized #907 wire: the crate NAMES the fields
                # whose values embed RAW vendor-derived tokens; the shell
                # repr()'s each at the LOG site exactly as the Python arm does
                # (`drift_merged` drifted/canonical, the options-arm `series`).
                fields[name] = repr(str(fields[name]))
            if event == "lake_heal.partition_error":
                fields["error"] = repr(str(fields["error"]))
            level = payload.get("level")
            if level == "info":
                # The writer's default `partition_log_level` — the heal state's
                # writers are built level-default, so `lake.partition.write`
                # telemetry rides INFO exactly as the Python arm does.
                _LOG.info(event, **fields)
            else:
                _LOG.warning(event, **fields)
        elif kind == "audit":
            _audit_append(
                state.audit,
                action=str(payload["action"]),
                subject=str(payload["subject"]),
                payload=dict(payload["payload"]),
            )
        elif kind == "file_removed_move":
            # The `_quarantine_move` manifest twin-hooks, verbatim order.
            invalidate_manifest(state.lake_root)
            _remove_dataset_stats_for(state.lake_root, Path(str(payload["path"])))
        elif kind == "catalog_upsert":
            if state.catalog is not None:
                dest = state.lake_root.joinpath(
                    BAR_DIR_NAME,
                    str(payload["exchange"]),
                    str(payload["segment"]),
                    str(payload["symbol"]),
                    str(payload["interval"]),
                    f"{int(payload['year']):04d}",
                    f"{int(payload['month']):02d}.parquet",
                )
                entry = state.writer.make_catalog_entry(
                    exchange=str(payload["exchange"]),
                    segment=str(payload["segment"]),
                    symbol=str(payload["symbol"]),
                    interval=str(payload["interval"]),
                    result=_write_result(payload, dest=dest),
                )
                state.catalog.upsert(entry)
        elif kind == "catalog_delete":
            if state.catalog is not None:
                state.catalog.delete_partition(
                    exchange=str(payload["exchange"]),
                    segment=str(payload["segment"]),
                    symbol=str(payload["symbol"]),
                    interval=str(payload["interval"]),
                    year=int(payload["year"]),
                    month=int(payload["month"]),
                )
        elif kind == "bars_stats_record":
            dest = Path(str(payload["path"]))
            # Friend access to the writer's OWN stats hook — the ONE
            # probe-first gating law (DEC-LAKE-STATS-MANIFEST-001); a re-mint
            # here would be the forbidden second copy (§11 COMMON).
            state.writer._record_stats_hook(  # pyright: ignore[reportPrivateUsage]
                dest, _write_result(payload, dest=dest)
            )
        elif kind == "option_stats_record":
            if stats_manifest_enabled():
                import polars as pl

                from brutex.data.option_parquet_writer import (
                    _record_option_stats,  # pyright: ignore[reportPrivateUsage]
                )

                dest = Path(str(payload["path"]))
                _record_option_stats(
                    state.lake_root, dest=dest, frame=pl.read_parquet(dest, glob=False)
                )
        elif kind == "bars_stats_remove":
            # The bars writer's wholly-removed quarantine-MOVE twin hooks,
            # verbatim order (`_heal_partition_locked`'s kept==0 branch):
            # manifest invalidation + the writer's OWN stats-remove hook
            # (self-gated on `stats_manifest_enabled`).
            invalidate_manifest(state.lake_root)
            state.writer._remove_stats_hook(  # pyright: ignore[reportPrivateUsage]
                Path(str(payload["path"]))
            )
        elif kind == "option_stats_remove":
            # The options twin (`OptionParquetWriter.heal_partition` kept==0):
            # stats-remove ONLY — deliberately NO invalidate_manifest (the
            # options tree is uncatalogued/unmanifested by design).
            from brutex.data.option_parquet_writer import (
                _remove_option_stats,  # pyright: ignore[reportPrivateUsage]
            )

            _remove_option_stats(state.lake_root, dest=Path(str(payload["path"])))
        elif kind == "record_dataset":
            # The write()'s dataset-created manifest upsert (W-2b — a foreign
            # reassignment / drift merge can CREATE a dataset dir), emitted
            # after the partition locks are released exactly as write() does.
            record_dataset(
                state.lake_root,
                exchange=str(payload["exchange"]),
                segment=str(payload["segment"]),
                symbol=str(payload["symbol"]),
                interval=str(payload["interval"]),
            )
        else:
            raise WriterError(f"unknown waved heal replay event kind {kind!r}")

    return handle


def _rust_heal_route(
    ordered: tuple[_HealableFinding, ...], state: _BatchState
) -> tuple[int, int, int]:
    """Route the batch mutation core through the Wave-D crate (the ONE arm).

    Returns ``(healed, skipped, errors)`` with *state* mutated exactly as the
    retired per-finding Python loop did (actions, quarantined-file count, the
    touched-``1minute`` recall driver). W-6a endgame
    (DEC-RUST-LAKE-WAVED-001): the crate is the ONLY heal implementation —
    an absent/stale wheel raises the typed loud
    :class:`~brutex.data.errors.LakeRustWavedUnavailableError` (never a
    silent no-heal), and an OUT-OF-ENVELOPE finding is a per-finding
    ISOLATED crate-side refusal (counted in ``errors`` + logged through the
    pinned ``lake_heal.partition_error`` replay — the batch continues; the
    old whole-batch ``degrade`` state is retired). The §B2 asymmetry stands:
    NO degrade around the engaged call — any exception out of it is LOUD
    (mutation may have happened; the healer is idempotent-convergent, so the
    posture is the documented re-run next batch/boot). ``lake_write_guard``
    wraps the call so the crate's real ``OSError(ENOSPC)`` maps to
    ``LakeWriteHalt("disk_full")`` exactly as the writers' own guards do.
    """
    from brutex.data.lake_rust_waved import heal_batch_via_rust

    serialized = _serialize_rust_findings(ordered)
    with lake_write_guard():
        outcome = heal_batch_via_rust(
            lake_root=str(state.lake_root),
            quarantine_root=str(state.quarantine_root),
            findings=serialized,
            on_event=_rust_heal_on_event(state),
        )
    for action in cast("list[dict[str, Any]]", outcome["actions"]):
        state.actions.append(HealAction(**action))
    state.quarantined_files += cast(int, outcome["quarantined_files"])
    for entry in cast("list[list[Any]]", outcome["touched_minute"]):
        exchange, segment, symbol, months = entry
        state.touch(
            str(exchange),
            str(segment),
            str(symbol),
            {(int(year), int(month)) for year, month in months},
        )
    return (
        cast(int, outcome["healed"]),
        cast(int, outcome["skipped"]),
        cast(int, outcome["errors"]),
    )


# ── derived-data recall ──────────────────────────────────────────────────────

#: Cap on the SYMBOL names carried by the ONE per-batch
#: ``lake_heal.recall_option_daily_skipped`` log (DEC-OPTIONS-PREBUILD-RETIRE-001
#: — a healed option chain can be hundreds of CE/PE contracts; the log names a
#: bounded sample, the TRUE total stays in its ``count`` field + in
#: :attr:`RecallReport.option_daily_skipped` — the MAX_REPORTED cap idiom).
RECALL_OPTION_SKIP_LOG_CAP: Final[int] = 10


def _first_day_after(year: int, month: int) -> date:
    """The first day of the month AFTER ``(year, month)``."""
    if month == _DECEMBER:
        return date(year + 1, 1, 1)
    return date(year, month + 1, 1)


def _prebuild_kind_from_coords(segment: str, symbol: str) -> PrebuildKind | None:
    """The TF-rebuild kind for one lake coordinate, or ``None`` = never rebuild.

    ``INDEX``/``CASH`` → SPOT; ``FNO`` + a ``groww_symbol`` decoding to a
    ``FUT`` contract → FUTURES; a CE/PE contract or an undecodable FNO
    symbol → ``None``. For CE/PE the ``None`` is the 1-minute-native law
    (DEC-OPTIONS-PREBUILD-RETIRE-001 — the operator ruling of 2026-07-15 as
    relayed via the coordinator, ADR-0021 RULING B, draft PR #1555: the
    pipeline's derived option TFs are RETIRED; a stored legacy option TF dir
    stays INERT, never quarantine-rebuilt here). For an undecodable FNO
    symbol the ``None`` is conservatism — the kind cannot be classified, so
    nothing is rebuilt. NOTE the pre-retirement docstring's claim that
    :func:`~brutex.data.timeframe_materialize.materialize_lake_timeframes`
    "raises for OPTIONS by design" was STALE — it has materialised
    ``PrebuildKind.OPTIONS`` since DEC-PIPELINE-AUTOPREBUILD-001; this gate
    is the real (and now law-aligned) reason options are never rebuilt.
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
        return 0, 0  # CE/PE (1-minute-native law) / undecodable FNO — never rebuilt
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
    # DEC-RUST-STRICT-NO-FALLBACK-001: a strict-mode refusal is NEVER absorbed.
    # TRADE-OFF, stated plainly: this trades the per-symbol recall isolation
    # (one bad symbol never kills the batch) for a crash that aborts the WHOLE
    # recall — accepted, because the refusal means the Rust arm could not serve
    # and Python was forbidden, so "the batch continued" would report progress
    # over months that stay quarantine-DELETED. Inert unless strict mode is on:
    # `refuse_if_strict` raises nothing when BRUTEX_RUST_STRICT=0, so the
    # kill-switched process keeps today's degrade byte-identically.
    except RustFallbackRefused:
        raise
    # _SWEEP_GUARD_ERRORS = Exception + the out-of-range-timestamp conversion
    # panics (DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001, the round-18 idiom): a
    # planted 2**62-us stored stamp panics the rebuild's Rust→Python conversion
    # as a BaseException that sailed PAST the former bare `except Exception`
    # and aborted the WHOLE recall batch; the read-boundary gate refuses it
    # FIRST as ResampleInputError — this is the defense-in-depth rail so any
    # residual panic stays per-symbol isolation. KeyboardInterrupt/SystemExit
    # still propagate (the guard is never a blanket BaseException).
    except _SWEEP_GUARD_ERRORS as exc:  # pylint: disable=broad-exception-caught — per-symbol isolation
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

    A healed CE/PE OPTION contract is EXEMPT from the whole recall
    (DEC-OPTIONS-PREBUILD-RETIRE-001 — the operator ruling of 2026-07-15 as
    relayed via the coordinator, ADR-0021 RULING B, draft PR #1555): the
    derived option ``1day`` is retired (options are 1-minute-native), so
    this recall must not stay its second producer. The skip is COUNTED
    (:attr:`RecallReport.option_daily_skipped`) and logged ONCE per batch
    (capped symbol list — never per-contract spam); a stored legacy option
    ``1day``/TF stays INERT (never rebuilt, never deleted). FUT + spot keep
    their recall byte-identically.
    """
    daily_targets = 0
    daily_bars = 0
    tf_rebuilt = 0
    tf_failed = 0
    option_daily_skipped: list[str] = []
    for (exchange, segment, symbol), months in sorted(state.touched_minute.items()):
        # DEC-PREBUILD-INCREMENTAL-001: the heal rewrote this symbol's 1-minute
        # source, so its derive stamp is stale by definition — remove it OUTRIGHT
        # (belt-and-braces: the rewrite already moved the source token) so the
        # next pipeline prebuild can never skip a healed symbol.
        remove_derive_stamp(state.lake_root, exchange, segment, symbol)
        if is_option_contract_coords(segment, symbol):
            # DEC-OPTIONS-PREBUILD-RETIRE-001: no derived 1day (and no TF
            # rebuild — the healed option stays 1-minute-native); counted +
            # batch-logged below, never a silent drop.
            option_daily_skipped.append(symbol)
            continue
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
            # §11 COMMON YYYY-MM formatter (round-18 NIT — see rows_reassigned).
            months=[option_partition_stem(y, m) for y, m in sorted(months)],
            bars=report.bars,
            errored=report.errored,
        )
        rebuilt, failed = _rebuild_tf_months(
            state, exchange=exchange, segment=segment, symbol=symbol, months=months
        )
        tf_rebuilt += rebuilt
        tf_failed += failed
    if option_daily_skipped:
        # ONE bounded log per recall BATCH (the MAX_REPORTED cap idiom —
        # never per-contract spam): a healed option chain can be hundreds of
        # contracts; the TRUE total rides ``count`` + the report field.
        _LOG.info(
            "lake_heal.recall_option_daily_skipped",
            count=len(option_daily_skipped),
            symbols=option_daily_skipped[:RECALL_OPTION_SKIP_LOG_CAP],
            law=(
                "DEC-OPTIONS-PREBUILD-RETIRE-001 — options are 1-minute-native; "
                "no derived 1day/TF recall for CE/PE"
            ),
        )
    return RecallReport(
        daily_targets=daily_targets,
        daily_bars=daily_bars,
        tf_rebuilt=tf_rebuilt,
        tf_failed=tf_failed,
        option_daily_skipped=len(option_daily_skipped),
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
    # DEC-RUST-STRICT-NO-FALLBACK-001: a strict-mode refusal is NEVER absorbed.
    # TRADE-OFF, stated plainly: this trades "a stats-sidecar write must never
    # masquerade as a lake halt" for exactly that — a refusal from
    # `partition_write_lock` / `atomic_write_simple` (both route the strict-wired
    # `lake_rust_locks` seam) now fails the heal instead of silently skipping the
    # sidecar. Accepted: the sidecar is best-effort about ITS OWN I/O, not about
    # the Rust arm being unable to serve at all. Inert unless strict mode is on
    # (BRUTEX_RUST_STRICT=0 keeps today's degrade byte-identically).
    except RustFallbackRefused:
        raise
    except Exception as exc:  # pylint: disable=broad-exception-caught — best-effort sidecar
        _LOG.warning("lake_heal.stats_write_failed", error=str(exc))


def load_heal_stats(lake_root: Path) -> dict[str, Any]:
    """Read the cumulative heal stats sidecar; ``{}`` when absent/corrupt.

    Tolerant by design (the census surfaces read this): a missing / unreadable
    / non-object JSON degrades to ``{}``, never a crash.
    """
    path = lake_root / STATS_RELPATH
    try:
        data = json.loads(read_regular_text(path))
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
        data = json.loads(read_regular_text(path))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_last_attempt(lake_root: Path) -> dict[str, Any] | None:
    """Read the L3 sweep ATTEMPT record; ``None`` when absent/corrupt (fail-open).

    The :func:`load_last_sweep` tolerance contract on
    :data:`ATTEMPT_RELPATH`: a missing / unreadable / non-object record means
    "no recorded attempt" — the next :func:`run_startup_sweep` runs
    (fail-open to CHECKING, never to skipping —
    DEC-LAKE-INVARIANTS-PREINDEX-001).
    """
    path = lake_root / ATTEMPT_RELPATH
    try:
        data = json.loads(read_regular_text(path))
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
    OptionForeignMonthRows (options reassignment), then OptionIntraFileDup,
    then SchemaAlienFile (whole-file quarantine —
    DEC-ALIEN-SCHEMA-READ-HEAL-001) — sorted by path within each class. A
    :class:`~brutex.data.lake_invariants.PriceSanityRows` finding is FILTERED
    OUT up-front (observe-only — never healed, never counted in ``findings``;
    one ``lake_heal.price_findings_observe_only`` WARNING names the count —
    DEC-STORED-DATA-VALIDATION-001). Observe mode short-circuits with ZERO
    file mutation of any kind (no quarantine dir, no locks, no catalog writes,
    no stats bump — byte-pinned); the mass-heal rail refuses a
    ``≥ MASS_HEAL_MIN_FILES ∧ > MASS_HEAL_FRACTION`` batch untouched; a
    SYMLINKED ``<lake>/.quarantine`` refuses the whole batch untouched too
    (round-17 — the before-image write must never land outside the lake; a
    dangling symlink at the stamp dir is serialed past). Each
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
    # DEC-STORED-DATA-VALIDATION-001: a PriceSanityRows finding is OBSERVE-ONLY
    # by design — the healer is STRICTLY row-removing/row-reassigning, never
    # value-editing (a machine-"repaired" price would fabricate data, Golden
    # Rule #1). It is filtered out here (the ONE heal chokepoint) so an L2/L3
    # scan that surfaces price corruption can never route it into a per-file
    # healer built for a different finding shape; remediation stays a re-pull /
    # operator quarantine, guided by the scan's own pinned WARNING + census.
    actionable: list[_HealableFinding] = []
    for candidate in findings:
        if isinstance(candidate, PriceSanityRows):
            continue
        actionable.append(candidate)
    if len(actionable) != len(findings):
        _LOG.warning(
            "lake_heal.price_findings_observe_only",
            price_findings=len(findings) - len(actionable),
        )
    ordered = _order_findings(actionable)
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
    quarantine_base = lake_root / QUARANTINE_DIRNAME
    if ordered and quarantine_base.is_symlink():
        # Round-17 fold (refuter-confirmed MINOR): the quarantine BEFORE-image
        # write was the ONE heal-side mutation outside the rounds-2/4/6
        # containment program — ``.quarantine`` pre-planted as a symlink (the
        # same lake-write capability class as the round-2 V1 symlinked-
        # options-root vector) sent every batch's forensic before-image
        # OUTSIDE the lake root BEFORE the in-lake source was rewritten /
        # unlinked (and ``atomic_write_via``'s ``os.replace`` could clobber
        # files under the link target) — silently voiding the "the quarantine
        # copy IS the forensic before-image" guarantee that justifies the
        # options no-raw_hash decision. Refuse the WHOLE batch untouched
        # (zero mutation of any kind); every finding counts as an error so
        # the L3 sentinel is never stamped (``heal_errors``) and the next
        # boot re-attempts once the operator removes the link. The path is
        # logged raw — lake root + a fixed literal, operator-controlled, no
        # vendor component (the options-ROOT skip convention). Honest
        # envelope: a swap AFTER this probe (inside one batch) is the same
        # sub-call TOCTOU class as the documented same-second cross-process
        # quarantine race — not claimed closed.
        _LOG.error(
            "lake_heal.quarantine_symlink_refused",
            quarantine=str(quarantine_base),
            findings=len(ordered),
        )
        report = HealReport(
            mode=resolved,
            findings=len(ordered),
            healed=0,
            skipped=0,
            errors=len(ordered),
            refused_mass_heal=False,
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
                "healed": 0,
                "errors": len(ordered),
                "refused_quarantine_symlink": True,
            },
        )
        if on_report is not None:
            on_report(report)
        return report
    stamp = now_fn().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    quarantine_root = quarantine_base / stamp
    serial = 0
    while quarantine_root.exists() or quarantine_root.is_symlink():
        # Round-5 NIT fold (pre-existing DEC-LAKE-HEAL-001 machinery): the
        # stamp is second-granular, so a SECOND batch in the same second
        # reused the dir and its atomic quarantine copy OVERWROTE the earlier
        # batch's before-image at the same lake-relative path — losing the
        # forensic original the quarantine exists to preserve. A ``-NN``
        # serial picks the first unused dir; the probe only fires when the
        # earlier batch actually quarantined something (a no-op batch never
        # creates its dir). The ``is_symlink()`` arm (round-17): a DANGLING
        # symlink pre-planted at the stamp path reads ``exists() == False``
        # yet would redirect the batch's before-images through the link —
        # serialed past like an existing dir (an EXISTING-target symlink
        # already takes the ``exists()`` arm). Best-effort: an
        # exists()-then-use race between two same-second PROCESSES remains
        # theoretically possible — honest envelope, not claimed closed.
        serial += 1
        quarantine_root = quarantine_base / f"{stamp}-{serial:02d}"
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
    # The Wave-D mutation core (DEC-RUST-LAKE-WAVED-001 §B2, W-6a endgame):
    # the batch's MUTATION CORE runs in the `brutex_lake` crate — the ONLY
    # heal implementation since the Python per-finding loop was deleted —
    # with every side effect (log/audit/catalog/stats/manifest/beat)
    # replayed through `_rust_heal_on_event`. Per-finding isolation
    # (LakeWriteHalt propagation, the pinned `lake_heal.partition_error`
    # WARN with its #907 repr law, the out-of-envelope isolated refusal) is
    # CRATE-OWNED and replayed through the same pinned Python sinks.
    if ordered:
        healed, skipped, errors = _rust_heal_route(ordered, state)
    recall_report: RecallReport | None = None
    if recall and state.touched_minute:
        # Session-close-aware forming-session cutoff (DEC-DAILY-CUTOFF-
        # GROUNDED-001 Move 1 — the third divergent pin repointed at the ONE
        # law): a post-close heal recall re-derives the run day's own
        # completed session instead of deferring it (the prior plain
        # today-IST date pin regressed DEC-PREBUILD-SESSION-CLOSE-CUTOFF-001
        # here too). CLOCK arm only, honestly: recall is historical heal —
        # data-grounding it is a named non-goal.
        recall_report = _run_recall(state, before=daily_derive_cutoff(now_fn().astimezone(_IST)))
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
        # Liveness beat per considered target + per scanned file
        # (DEC-HEAL-SCAN-LIVENESS-BEATS-001, the #1558 greeks-backfill scan
        # precedent): this L2 scan is O(pulled files) — forensically measured
        # 36 min over 101,682 files on the production lake — and it runs on
        # the pull worker thread AFTER the last fetched-window heartbeat, so
        # without beats the stall watchdog falsely fired ``stalled_suspect``
        # on a provably-healthy run. ``emit_progress_beat`` routes by the
        # worker's ambient pull id (the #1537 keyed channels) and is a cheap
        # no-op for the CLI / any caller with no registered watchdog.
        emit_progress_beat()
        scan = scan_lake_invariants(
            lake_root,
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            months=months,
            include_options=False,
            per_file_pause=emit_progress_beat,
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

    Feeds one ``relpath|size|mtime_ns\\n`` line per entry into an INCREMENTAL
    hasher over a deterministic sorted-at-each-level directory walk — an
    O(files) stat WALL cost with NO file reads (inherent to content-change
    detection without a write-time manifest), but O(largest single directory
    + tree depth) RESIDENT (DEC-LAKE-INVARIANTS-PREINDEX-001: the previous
    list + ``sorted()`` copy + ``"\\n".join`` materialised ~256 B/file —
    a ~575-760 MiB transient at the operator-measured 2.36M-file lake, paid
    on EVERY boot before the sentinel compare). Any add / remove / rewrite
    (a pull, a derive, a heal, a manual copy) moves the token, so the
    sentinel comparison re-arms the sweep.

    TOKEN FORMAT CHANGE (sanctioned, ledgered): lines are hashed in walk
    order (each level sorted; a directory's file lines before its subtrees')
    with a per-line ``\\n`` terminator, not the old whole-string-sorted join
    — so the token VALUE differs from the pre-streaming format for the same
    lake content. First post-deploy boot per lake: one sentinel mismatch →
    ONE full L3 sweep, after which the new-format token is stamped (the same
    cost as any pull-triggered re-arm). The walk mirrors the old
    ``rglob("*.parquet")`` selection exactly (empirically pinned on the
    pinned 3.12 pathlib): symlinked DIRECTORIES are not descended, hidden
    entries ARE matched, and any directory entry whose NAME matches — a
    plain file, a dangling/file symlink, even a dir named ``x.parquet``
    (also descended) — contributes a line.

    Each entry is ``lstat``-ed under a per-entry guard (hostile-review
    round-11 fold, DEC-LAKE-HEAL-OPTIONS-FOREIGN-001): pre-fix ``path.stat()``
    FOLLOWED a dangling ``*.parquet`` symlink and raised — and a file deleted
    between the glob and its stat (a concurrent pull/heal) raced the same way
    — so ONE such entry crashed this walk before any scan, and the guarded
    ``StartupSweeper.run_sync`` degraded to ``lake_heal.sweep_failed`` on
    EVERY boot (the L3 sweep permanently inert: loud, never healing).
    ``lstat`` never follows a link, so a dangling symlink contributes its OWN
    link metadata deterministically (the scanner skips symlinked files anyway
    — nothing healable hides behind one); a raced-away entry is skipped
    LOUDLY (``lake_heal.token_stat_skipped``, path repr()'d — an options
    entry embeds the vendor-influenceable series dir name, the rounds-8/9
    convention), keeping the token deterministic for identical lake content.
    A directory raced away mid-walk is likewise skipped loudly
    (``lake_heal.token_walk_skipped`` — the old ``rglob`` suppressed the
    same race silently), deterministically hashing the surviving entries.

    Wave-D endgame (DEC-RUST-LAKE-WAVED-001 §C-5, W-6a): the walk is
    Rust-ONLY — the crate twin owns it (the Python walk was deleted; the
    frozen differential expectations are the behavior pin), with skip events
    replayed through the SAME two pinned WARN sites via
    :func:`_replay_token_skip` (the ``repr()`` law applied here, where the
    events are owned). An absent/stale wheel raises the typed loud
    :class:`~brutex.data.errors.LakeRustWavedUnavailableError`.
    """
    from brutex.data.lake_rust_waved import lake_content_token_via_rust

    return lake_content_token_via_rust(
        lake_root=str(lake_root),
        on_skip=_replay_token_skip,
    )


def _replay_token_skip(event: str, path: str, error: str) -> None:
    """Replay one crate walk-skip through the pinned WARN sites (the repr law)."""
    _LOG.warning(event, path=repr(path), error=repr(error))


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


def _write_attempt_record(
    lake_root: Path,
    *,
    token: str,
    reason: str,
    mode: str,
    findings: int,
    files_scanned: int,
    now: datetime,
) -> None:
    """Stamp :data:`ATTEMPT_RELPATH` after a sweep that did NOT stamp the sentinel.

    Records the POST-heal token the next boot will observe over unchanged
    content, the :func:`_sentinel_skip_reason` verdict, the heal mode the
    attempt ran under (a mode flip must re-run ANY skip), the REAL batch
    ``findings`` + scan ``files_scanned`` tallies (the persisted values the
    :func:`_realert_mass_heal_refused` re-alert reads — never a fabricated
    report), and the attempt wall time (the transient backoff anchor).

    BEST-EFFORT (review fold, MINOR-2): an :class:`OSError` (a read-only /
    wedged lake mount) logs ONE targeted ``lake_heal.attempt_record_failed``
    WARNING and returns — the sweep genuinely ran, so its real
    :class:`StartupSweepResult` must still reach the caller instead of being
    discarded by the :class:`StartupSweeper` blanket guard as a scary
    every-boot ``lake_heal.sweep_failed`` (with the skip machinery then —
    ironically — never engaging, so the O(files) scan re-ran every boot too).
    The cost of a failed write is honestly the pre-record cadence: the next
    boot re-scans. :class:`MemoryError` is NOT caught (house law) — it
    propagates to the pull-wide/boot guards untouched.
    """
    path = lake_root / ATTEMPT_RELPATH
    payload = json.dumps(
        {
            "version": 1,
            "lake_token": token,
            "reason": reason,
            "mode": mode,
            "findings": findings,
            "files_scanned": files_scanned,
            "attempt_utc": now.astimezone(UTC).isoformat(),
        },
        sort_keys=True,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_simple(path, lambda tmp: tmp.write_text(payload, encoding="utf-8"))
    except OSError as exc:
        _LOG.warning("lake_heal.attempt_record_failed", path=str(path), error=str(exc))


def _attempt_skip_verdict(
    attempt: Mapping[str, Any] | None, *, token: str, mode: LakeHealMode, now: datetime
) -> str | None:
    """Why THIS boot may skip re-scanning an already-attempted token, or ``None``.

    The scan+heal verdict is a pure function of (lake content token, heal
    mode) — re-running a full O(files) scan after a DETERMINISTIC non-stamp
    (observe mode, mass-heal refusal) on an unchanged token + unchanged mode
    provably reproduces the same outcome: pure waste
    (DEC-LAKE-INVARIANTS-PREINDEX-001; pre-fix such a lake full-rescanned on
    EVERY boot — ≥32.8 h per sweep at the operator-measured 2.36M-file lake).
    A TRANSIENT non-stamp (heal errors / unhealed findings — I/O may heal
    itself) is re-attempted once the :func:`resolve_heal_retry_s` backoff
    elapses, or immediately when the token moves (any pull/heal/manual copy).
    A MODE FLIP re-runs for ALL reasons (review fold, MAJOR): the mode gate
    is hoisted ABOVE the reason dispatch — pre-fix only the deterministic arm
    honoured it, so an operator flipping ``auto`` → ``observe`` to DIAGNOSE a
    ``heal_errors`` lake was backoff-skipped for up to 24 h; the operator
    changed the sweep's inputs, so the recorded outcome no longer predicts
    this boot's. Every ambiguity — no record, foreign token, unknown reason,
    unparsable or future-skewed timestamp — biases to ``None`` = re-run
    (fail-open to CHECKING, the sentinel loaders' contract).
    ``"deterministic"`` / ``"backoff"`` name the skip class for the caller's
    WARNING.
    """
    if attempt is None or attempt.get("lake_token") != token:
        return None
    if attempt.get("mode") != mode:
        return None  # mode flipped since the attempt — re-run under the new mode
    reason = attempt.get("reason")
    if reason in _DETERMINISTIC_ATTEMPT_REASONS:
        return "deterministic"
    if reason in _TRANSIENT_ATTEMPT_REASONS:
        raw = attempt.get("attempt_utc")
        if not isinstance(raw, str):
            return None  # absent / non-string stamp — fail-open to checking
        try:
            attempted = datetime.fromisoformat(raw)
            elapsed = (now.astimezone(UTC) - attempted).total_seconds()
        except (TypeError, ValueError):
            return None  # unparsable / naive timestamp — fail-open to checking
        if 0.0 <= elapsed < resolve_heal_retry_s():
            return "backoff"
        # Elapsed backoff — or a FUTURE-skewed stamp (elapsed < 0): re-run.
        return None
    return None  # unknown reason (forward compat / hand-edited) — re-run


def _realert_mass_heal_refused(attempt: Mapping[str, Any]) -> None:
    """Re-fire the standing mass-heal-refusal CRITICAL alert on a skipped boot.

    Review fold (judgment ruling): the deterministic skip dropped the
    per-boot Telegram re-alert — sharpest for ``mass_heal_refused``, a
    CRITICAL human-required state the pre-change every-boot re-sweep re-fired
    on each boot. The re-alert reads ONLY the PERSISTED REAL scalars the
    refused sweep itself recorded in the attempt sidecar (``findings`` /
    ``files_scanned`` / ``attempt_utc`` — :func:`_write_attempt_record`) and
    routes them through
    :func:`~brutex.notifications.sweep_alerts.emit_lake_heal_still_refused_alert`,
    whose wording is explicitly a re-alert of the PRIOR sweep ("still refused
    since <ts>") — NEVER a fabricated per-boot :class:`HealReport` for a
    sweep that did not run. A pre-fix-shaped record missing any of the three
    fields (or carrying junk types — a ``bool`` masquerading as an int, or a
    negative tally, both only reachable via a hand-edited sidecar) ABSTAINS
    silently — honesty over noise (refuter NIT-1: ``type(x) is int`` +
    ``x >= 0``, never bare ``isinstance`` which admits ``bool``).
    Best-effort end to end: the notifications import is function-local (the
    :func:`_startup_sweep_alert` precedent), the emit helper is itself
    blanket-guarded, and this wrapper's own guard means an alert-path fault
    is ONE ``lake_heal.still_refused_alert_failed`` WARNING — alerts never
    crash boot.
    """
    findings = attempt.get("findings")
    files_scanned = attempt.get("files_scanned")
    attempt_utc = attempt.get("attempt_utc")
    if (
        type(findings) is not int
        or findings < 0
        or type(files_scanned) is not int
        or files_scanned < 0
        or not isinstance(attempt_utc, str)
    ):
        return  # pre-fix / hand-edited record shape — abstain, never fabricate
    try:
        from brutex.notifications.sweep_alerts import emit_lake_heal_still_refused_alert

        emit_lake_heal_still_refused_alert(
            findings=findings,
            files_scanned=files_scanned,
            attempt_utc=attempt_utc,
            scope="startup-sweep",
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught — best-effort alert seam
        _LOG.warning("lake_heal.still_refused_alert_failed", error=str(exc))


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
    the stamp (``lake_heal.sweep_sentinel_skipped`` with the reason) so a
    later boot re-attempts instead of token-matching past live poison — and
    additionally stamps the :data:`ATTEMPT_RELPATH` sibling record
    (DEC-LAKE-INVARIANTS-PREINDEX-001; BEST-EFFORT — an unwritable record
    logs ``lake_heal.attempt_record_failed`` and the real result still
    returns, degrading to the pre-record every-boot cadence): on the next
    boot with an UNCHANGED token AND UNCHANGED mode (a
    :data:`LAKE_HEAL_ENV` flip re-runs for ALL reasons — the operator
    changed the sweep's inputs), a DETERMINISTIC non-stamp (observe mode /
    mass-heal refusal) skips the provably-identical re-scan with a loud
    ``lake_heal.sweep_skip_attempt`` WARNING naming the standing poison +
    verdict-specific remediation — a skipped standing MASS-HEAL REFUSAL
    additionally re-fires the CRITICAL Telegram alert from the record's
    persisted real tallies (:func:`_realert_mass_heal_refused`, "still
    refused since <ts>") — and a TRANSIENT non-stamp (heal errors / unhealed
    findings) re-attempts only after the :func:`resolve_heal_retry_s`
    backoff (default 24 h, env :data:`LAKE_HEAL_RETRY_ENV`) — every skipped
    boot still WARNs, any token move (a pull/heal/manual copy) re-arms
    immediately, and *force* bypasses both records. This never weakens the
    clean-sentinel meaning: the silent fast path still requires a
    genuinely-clean stamp.
    Cost honesty (Golden Rule #10): O(files) total with a per-file key-column
    read that scales with row count — NOT O(1); paid once per lake content
    token; the per-boot token walk itself stays an O(files) stat cost.

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
    attempt = load_last_attempt(lake_root) if not force else None
    if attempt is not None:
        mode = resolve_lake_heal_mode()
        verdict = _attempt_skip_verdict(attempt, token=token, mode=mode, now=now_fn())
        if verdict is not None:
            # LOUD by design — the lake still carries whatever kept the
            # sentinel unstamped; the skip only avoids re-paying the O(files)
            # scan for a provably-identical (or backoff-bounded) outcome.
            # Remediation is VERDICT-specific (review fold): a deterministic
            # skip names its lever (the mode flip), a backoff skip names ITS
            # lever (the retry env — =0 + a reboot re-runs now); pre-fix both
            # verdicts read the deterministic wording.
            if verdict == "deterministic":
                remediation = (
                    "fix the recorded cause then re-arm: any lake content change "
                    f"re-runs immediately; a {LAKE_HEAL_ENV} mode flip re-runs; "
                    "force=True bypasses"
                )
            else:
                remediation = (
                    "fix the recorded cause then re-arm: any lake content change "
                    f"re-runs immediately; {LAKE_HEAL_RETRY_ENV}=0 + a reboot "
                    "re-runs now; force=True bypasses"
                )
            _LOG.warning(
                "lake_heal.sweep_skip_attempt",
                verdict=verdict,
                reason=attempt.get("reason"),
                mode=attempt.get("mode"),
                attempt_utc=attempt.get("attempt_utc"),
                retry_s=resolve_heal_retry_s() if verdict == "backoff" else None,
                remediation=remediation,
            )
            if verdict == "deterministic" and attempt.get("reason") == "mass_heal_refused":
                # The standing CRITICAL human-required state keeps its
                # per-boot Telegram cadence — from the PERSISTED real tallies
                # (review fold; see _realert_mass_heal_refused).
                _realert_mass_heal_refused(attempt)
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
        # in place, and stamp the ATTEMPT record so the next boot re-attempts
        # per the deterministic-skip / bounded-backoff law instead of
        # re-paying the full O(files) scan unconditionally
        # (DEC-LAKE-INVARIANTS-PREINDEX-001).
        _LOG.warning(
            "lake_heal.sweep_sentinel_skipped",
            reason=skip_reason,
            mode=report.mode,
            findings=report.findings,
            healed=report.healed,
            errors=report.errors,
        )
        _write_attempt_record(
            lake_root,
            token=post_token,
            reason=skip_reason,
            mode=report.mode,
            findings=report.findings,
            files_scanned=files_scanned,
            now=now_fn(),
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
        except _SWEEP_GUARD_ERRORS as exc:  # pylint: disable=broad-exception-caught
            # — boot never crashes (the ONE blanket guard; see the constant).
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
    "ATTEMPT_RELPATH",
    "AUDIT_HEAL_BATCH",
    "AUDIT_HEAL_DEDUP",
    "AUDIT_HEAL_DRIFT_MERGE",
    "AUDIT_HEAL_QUARANTINE_FILE",
    "AUDIT_HEAL_REASSIGN",
    "DEFAULT_LAKE_HEAL_MODE",
    "DEFAULT_LAKE_HEAL_RETRY_S",
    "DEFAULT_SWEEP_THROTTLE_S",
    "LAKE_HEAL_ENV",
    "LAKE_HEAL_RETRY_ENV",
    "LAKE_HEAL_THROTTLE_ENV",
    "MASS_HEAL_FRACTION",
    "MASS_HEAL_MIN_FILES",
    "QUARANTINE_DIRNAME",
    "RECALL_OPTION_SKIP_LOG_CAP",
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
    "load_last_attempt",
    "load_last_sweep",
    "open_lake_catalog",
    "open_lake_heal_audit",
    "resolve_heal_retry_s",
    "resolve_lake_heal_mode",
    "resolve_sweep_throttle_s",
    "run_startup_sweep",
]
