"""Rebuild the DuckDB catalog ``partitions`` table from the on-disk Parquet lake.

DEC-CATALOG-RECONCILE-WIRE-001. The production ingest sinks historically
constructed :class:`~brutex.data.sink.ParquetBarSink` WITHOUT a catalog (the
``catalog=None`` default), so a real ``fetch`` / dashboard pull wrote Parquet
partitions to disk but never recorded them in the DuckDB ``partitions`` table.
:meth:`~brutex.data.catalog.Catalog.get_bars` reads bars *only* through that
table, so ``brutex sweep-all`` found zero bars after any real pull.

This module is the READ-side safety net + recovery tool: it scans the lake's
Parquet partition tree (the source of truth on disk) and upserts one
:class:`~brutex.data.models.CatalogEntry` per partition, so an existing lake
becomes sweep-able with **no re-pull**. The write-side fix (wiring a catalog
into the sinks) makes *future* pulls self-register; this reconciler repairs
*already-pulled* lakes and is invoked automatically at sweep start.

Honest envelope (CLAUDE.md Golden Rule #10): reconciliation is **O(P) one-time
work** over the P partition files of the requested coordinates — each file is
opened once to read only its ``timestamp`` column (O(rows-in-file) I/O) plus an
O(1) ``sha256`` over the file bytes. It is NOT in the per-combo sweep hot loop
and makes no O(1)-total claim. It is additive (upsert-only): partitions whose
Parquet file was deleted out from under the lake are left as stale catalog rows
(this append-only lake does not delete partitions); callers reading a missing
file get the catalog's existing missing-file error, unchanged.

Wave-D core (ADR-0020 §6, DEC-RUST-LAKE-WAVED-001 ENDGAME): the PER-FILE
read/hash/classify body (the ``timestamp``-column read + row count + min/max
span + the run-identity ``sha256`` + the L1 dup/foreign classification) is
OWNED by the ``brutex_lake`` crate via
:func:`brutex.data.lake_rust_waved.reconcile_file_via_rust` — the Python
``_scan_file`` oracle is DELETED. A crate-side refusal (a corrupt file, an
out-of-envelope timestamp shape, a crate panic) raises ``ValueError`` naming
the file — a member of this loop's GAP-#9 skip set — so the pinned per-file
``catalog.reconcile.parquet_skip`` verdict survives with the Rust-rendered
reason (the ruled endgame semantics). The ``raw_hash`` sha256 is byte-equal
by the crate's own hashlib-vector pins (Golden Rule #4 — it feeds the
FR-CAT-004 catalog rollup). The LOOP — enumeration, ``parse_partition_path``,
the stat-cache branch, ``CatalogEntry`` build + upsert, the drift fold, all
logging — stays here. An absent/stale wheel raises the typed
:class:`~brutex.data.errors.LakeRustWavedUnavailableError` (the
REQUIRED-wheel law).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import structlog

from brutex.data.catalog import Catalog
from brutex.data.errors import (
    CatalogError,
    PartitionError,
    datetime_out_of_range_errors,
    parquet_read_skip_errors,
)
from brutex.data.lake_invariants import (
    LakeFinding,
    PartitionCoords,
    detect_legacy_drift,
    log_lake_finding,
)
from brutex.data.lake_rust_waved import reconcile_file_via_rust
from brutex.data.models import CatalogEntry
from brutex.data.partition import BAR_DIR_NAME, parse_partition_path

__all__ = ["ReconcileResult", "reconcile_catalog_from_lake"]

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from brutex.data.lake_invariants import ForeignMonthRows, IntraFileDup

    #: The per-file scan payload ``(row_count, first_ts, last_ts, raw_hash,
    #: findings)`` — the Wave-D crate wire (``None`` = a zero-row file, the
    #: caller's counted skip). ``first_ts``/``last_ts`` are the min/max
    #: values; :func:`_as_utc` normalises them at ``CatalogEntry`` build.
    _FileScan = tuple[int, object, object, str, tuple["IntraFileDup | ForeignMonthRows", ...]]

#: One ``catalog.reconcile.parquet_skip`` warning per unreadable partition file, so a
#: corrupt Parquet that would otherwise crash the FIRST step of every sweep load + F&O
#: window derivation is surfaced + skipped, never silent (GAP #9).
_LOG = structlog.get_logger("brutex.data.catalog_reconcile")


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of a :func:`reconcile_catalog_from_lake` pass.

    Attributes
    ----------
    partitions_reconciled
        Number of Parquet partitions upserted into the catalog.
    bars_total
        Total bars across every reconciled partition.
    skipped
        Partition files skipped because they could not be parsed as a
        valid partition path, held zero rows, or were UNREADABLE (corrupt /
        version-incompatible Parquet — logged ``catalog.reconcile.parquet_skip``
        and skipped instead of crashing the whole reconcile, GAP #9).
    findings
        Lake invariant findings detected by the free L1 piggyback
        (DEC-LAKE-HEAL-001, ``check_invariants=True``): intra-file duplicate
        timestamps + foreign-month rows classified from the SAME timestamp
        column reconcile already reads (zero extra reads;
        ``overlap_rows=None`` — the free path skips the canonical-sibling
        read), plus #911 legacy-key dir drift derived from the coords already
        parsed. Reconcile itself NEVER heals — healing is the caller's
        one-liner (``lake_heal.heal_after_reconcile``). Defaulted ``()`` so
        the pre-existing three-counter construction stays compatible.
    stat_cache_hits
        DEC-SWEEP-RECONCILE-STATCACHE-001: number of partitions SKIPPED via the
        opt-in ``stat_cache`` fast path — a byte-unchanged file (exact
        ``(st_mtime_ns, st_size, st_ino)`` token match against its stored catalog
        row) whose ``sha256`` + timestamp read + upsert were all avoided. Always
        ``0`` when ``stat_cache=False`` (every caller except the sweep-all
        launcher), so the pre-existing counter constructions stay compatible.
    """

    partitions_reconciled: int
    bars_total: int
    skipped: int
    findings: tuple[LakeFinding, ...] = ()
    stat_cache_hits: int = 0


def _partition_files(
    lake_root: Path,
    *,
    exchange: str | None,
    segment: str | None,
    symbol: str | None,
    interval: str | None,
) -> list[Path]:
    """Return every ``<year>/<month>.parquet`` partition file matching the coords.

    ``None`` for a coordinate means "any value" (a ``*`` glob segment). The
    six-segment pattern (``exchange/segment/symbol/interval/year/month.parquet``)
    matches exactly the spot bar layout under ``bars/`` and never the option
    lake's deeper tree, so reconcile only ever touches index/spot partitions.
    """
    base = lake_root / BAR_DIR_NAME
    if not base.exists():
        return []
    pattern = f"{exchange or '*'}/{segment or '*'}/{symbol or '*'}/{interval or '*'}/*/*.parquet"
    return sorted(base.glob(pattern))


def reconcile_catalog_from_lake(
    *,
    lake_root: Path,
    catalog: Catalog,
    exchange: str | None = None,
    segment: str | None = None,
    symbol: str | None = None,
    interval: str | None = None,
    check_invariants: bool = True,
    stat_cache: bool = False,
) -> ReconcileResult:
    """Upsert one catalog row per on-disk Parquet partition under *lake_root*.

    Scans ``<lake_root>/bars/<exchange>/<segment>/<symbol>/<interval>/<year>/
    <month>.parquet`` (each unset coordinate widening to a ``*`` glob) and, for
    every partition file, records a :class:`~brutex.data.models.CatalogEntry`
    via :meth:`~brutex.data.catalog.Catalog.upsert`. ``raw_hash`` is the sha256
    of the Parquet file bytes — bit-identical to what
    :class:`~brutex.data.parquet_writer.ParquetWriter` records at write time, so
    reconciling an already-ingested partition is a no-op upsert. ``fetched_at``
    is the file's modification time (UTC) so repeated reconciles are
    deterministic.

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/``).
    catalog
        Open :class:`~brutex.data.catalog.Catalog` to upsert into. The
        catalog's internal lock serialises the writes.
    exchange, segment, symbol, interval
        Optional coordinate filters; ``None`` reconciles every value for
        that coordinate.
    check_invariants
        DEC-LAKE-HEAL-001 free L1 detection (default on): classify each file's
        ALREADY-READ timestamp column via
        :func:`~brutex.data.lake_invariants.classify_bar_keys` (intra-file dup +
        foreign-month). ZERO extra I/O — the ``n_unique`` uniqueness pass runs
        on the timestamp column this loop already read for ``first_ts``/
        ``last_ts``, so the added cost is a CPU-only O(rows) hash pass per
        file, never a second read (stated honestly, Golden Rule #10) — plus
        :func:`~brutex.data.lake_invariants.detect_legacy_drift` over the
        coords already parsed. Findings are LOGGED (the ``lake.invariant.*``
        events) + returned on ``ReconcileResult.findings``; reconcile itself
        NEVER heals (a read-side walk must not mutate mid-walk). ``False``
        restores the pre-DEC-LAKE-HEAL-001 behaviour byte-identically — used
        by mid-pull HOT call sites that never heal (the F&O window
        derivation), where the findings would only re-log what the L2
        post-pull heal handles moments later.
    stat_cache
        DEC-SWEEP-RECONCILE-STATCACHE-001 durable reconcile stat-cache
        (default ``False`` = OFF). When ``True``, each partition file is
        ``os.stat``-ed FIRST and, if its ``(st_mtime_ns, st_size, st_ino)``
        token EXACTLY matches the stored catalog row's token, the full-bytes
        ``sha256`` + timestamp read + upsert (and the L1 ``check_invariants``
        classification) are ALL skipped — the stored row is already
        byte-correct. A changed / new / legacy-``NULL``-token file recomputes as
        today and backfills its token (best-effort — a token-write failure is
        logged, never fails the launch). This is **opt-in and set ``True`` ONLY
        at the ``brutex sweep-all`` launcher**, whose reconcile findings are
        discarded: the SHARED callers that drive the cascade-wire L1 auto-heal
        and the ``lake audit-unique`` census keep the default ``stat_cache=False``
        so their findings are still emitted for a byte-unchanged-but-poisoned
        partition (a skip would silently suppress heal/census detection). The
        skip never changes which bars load (``get_bars`` re-reads the actual file
        content) nor their values — run identity is byte-identical (Golden Rule
        #4). ``st_ino`` is part of the token because every lake writer publishes
        via ``os.replace`` (a fresh inode), so an inode change defeats a same-size
        in-place rewrite and a coarse-mtime external-SSD collision.

    Returns
    -------
    ReconcileResult
        Counts of reconciled partitions, total bars, and skipped files, plus
        the L1 invariant ``findings`` and the ``stat_cache`` hit count.
    """
    files = _partition_files(
        lake_root,
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval=interval,
    )
    reconciled = 0
    bars_total = 0
    skipped = 0
    stat_cache_hits = 0
    findings: list[LakeFinding] = []
    symbol_dirs: dict[tuple[str, str], set[str]] = {}
    # DEC-SWEEP-RECONCILE-STATCACHE-001: per-(symbol, interval) stored stat-token
    # maps, loaded lazily in ONE indexed query each, so a warm hit is an os.stat +
    # O(1) dict lookup with zero Parquet reads. Populated only when stat_cache=True.
    stat_token_maps: dict[
        tuple[str, str, str, str], dict[tuple[int, int], tuple[int, int, int] | None]
    ] = {}
    for path in files:
        try:
            coords = parse_partition_path(path, lake_root=lake_root)
        except PartitionError:
            skipped += 1
            continue
        file_stat: os.stat_result | None = None
        if stat_cache:
            try:
                file_stat = path.stat()
            except OSError as exc:
                # TOCTOU: the partition vanished / became unreadable between the
                # glob and the stat. Mirror the corrupt/zero-row skip — counted,
                # never a crash (get_bars would raise on the missing file later).
                _LOG.warning("catalog.reconcile.stat_skip", path=str(path), error=str(exc))
                skipped += 1
                continue
            coord_key = (
                cast(str, coords["exchange"]),
                cast(str, coords["segment"]),
                cast(str, coords["symbol"]),
                cast(str, coords["interval"]),
            )
            if coord_key not in stat_token_maps:
                stat_token_maps[coord_key] = catalog.get_partition_stat_tokens(
                    exchange=coord_key[0],
                    segment=coord_key[1],
                    symbol=coord_key[2],
                    interval=coord_key[3],
                )
            cached_token = stat_token_maps[coord_key].get(
                (cast(int, coords["year"]), cast(int, coords["month"]))
            )
            if cached_token is not None and cached_token == (
                file_stat.st_mtime_ns,
                file_stat.st_size,
                file_stat.st_ino,
            ):
                # WARM HIT: the stored catalog row is already byte-correct for this
                # unchanged file — skip the sha256 + timestamp read + upsert (and
                # the L1 classification). The launcher discards findings; the
                # shared heal/census callers pass stat_cache=False (never skip).
                stat_cache_hits += 1
                continue
        partition_coords: PartitionCoords | None = None
        if check_invariants:
            symbol_dirs.setdefault(
                (cast(str, coords["exchange"]), cast(str, coords["segment"])), set()
            ).add(cast(str, coords["symbol"]))
            partition_coords = PartitionCoords(
                exchange=cast(str, coords["exchange"]),
                segment=cast(str, coords["segment"]),
                symbol=cast(str, coords["symbol"]),
                interval=cast(str, coords["interval"]),
                year=cast(int, coords["year"]),
                month=cast(int, coords["month"]),
            )
        # GAP #9: a single corrupt / version-incompatible Parquet file (a polars
        # PolarsError, or an OSError fault) must NOT crash the whole reconcile — which
        # runs at the START of every sweep load + F&O window derivation. Log + skip that
        # one file (counted in `skipped`) and continue; every readable partition still
        # reconciles. NARROW set (`parquet_read_skip_errors` +
        # `datetime_out_of_range_errors` — the round-18 DEC-LAKE-HEAL-OPTIONS-FOREIGN-001
        # fold: a hand-planted Datetime column holding an out-of-`datetime`-range i64
        # value, e.g. 2**62 µs, makes `min()`/`max()` raise OverflowError — pre-fix ONE
        # such bars file crashed the whole reconcile, and with it every sweep load; if
        # min/max convert, the downstream `classify_bar_keys` min/max convert too, so
        # this ONE guard covers the L1 piggyback as well): a genuine code bug still
        # propagates (Golden Rule #1).
        try:
            # The Wave-D core (module docstring): the per-file body is
            # crate-owned; a crate refusal raises ValueError into THIS
            # guard's skip set (the ruled endgame semantics).
            scan = reconcile_file_via_rust(path=str(path), coords=partition_coords)
        except (*parquet_read_skip_errors(), *datetime_out_of_range_errors()) as exc:
            _LOG.warning("catalog.reconcile.parquet_skip", path=str(path), error=str(exc))
            skipped += 1
            continue
        if scan is None:
            skipped += 1
            continue
        row_count, first_ts, last_ts, raw_hash, file_findings = scan
        for finding in file_findings:
            located = replace(finding, path=path)
            log_lake_finding(located)
            findings.append(located)
        entry = CatalogEntry(
            exchange=cast(str, coords["exchange"]),
            segment=cast(str, coords["segment"]),
            symbol=cast(str, coords["symbol"]),
            interval=cast(str, coords["interval"]),
            year=cast(int, coords["year"]),
            month=cast(int, coords["month"]),
            raw_hash=raw_hash,
            row_count=row_count,
            first_ts=_as_utc(first_ts),
            last_ts=_as_utc(last_ts),
            fetched_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
        )
        catalog.upsert(entry)
        reconciled += 1
        bars_total += row_count
        if file_stat is not None:
            # DEC-SWEEP-RECONCILE-STATCACHE-001 best-effort stat-cache backfill:
            # stamp the (mtime_ns, size, ino) token so the NEXT launch skips this
            # unchanged file on a stat-only match. A write failure (e.g. ENOSPC)
            # must NOT fail the launch — the token stays NULL and this file is
            # simply recomputed next launch. Reachable only when stat_cache=True
            # (file_stat is None on every other caller).
            try:
                catalog.set_partition_stat(
                    exchange=cast(str, coords["exchange"]),
                    segment=cast(str, coords["segment"]),
                    symbol=cast(str, coords["symbol"]),
                    interval=cast(str, coords["interval"]),
                    year=cast(int, coords["year"]),
                    month=cast(int, coords["month"]),
                    file_mtime_ns=file_stat.st_mtime_ns,
                    file_size=file_stat.st_size,
                    file_ino=file_stat.st_ino,
                )
            except CatalogError as exc:
                _LOG.warning("catalog.reconcile.stat_write_failed", path=str(path), error=str(exc))
    if check_invariants:
        # DEC-LAKE-HEAL-001 L1 drift pass: derived from the coords already parsed
        # (no Parquet reads; a drifted dir pays one directory listing for its
        # interval/file profile — anomaly-only).
        for drift in detect_legacy_drift(
            {key: sorted(names) for key, names in symbol_dirs.items()},
            lake_root=lake_root,
        ):
            log_lake_finding(drift)
            findings.append(drift)
    return ReconcileResult(
        partitions_reconciled=reconciled,
        bars_total=bars_total,
        skipped=skipped,
        findings=tuple(findings),
        stat_cache_hits=stat_cache_hits,
    )


def _as_utc(value: object) -> datetime:
    """Coerce a polars min/max timestamp to a timezone-aware UTC datetime.

    Bars are stored as ``timestamp[us, tz=UTC]`` so polars already yields a
    tz-aware ``datetime``; a tz-naive value (defensive) is stamped UTC.
    """
    if not isinstance(value, datetime):  # pragma: no cover — schema guarantees datetime
        raise PartitionError(f"timestamp column yielded a non-datetime: {value!r}")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
