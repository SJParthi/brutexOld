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
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import polars as pl
import structlog

from brutex.data.catalog import Catalog
from brutex.data.errors import (
    CatalogError,
    PartitionError,
    datetime_out_of_range_errors,
)
from brutex.data.guarded_read import read_parquet_regular, read_regular_bytes
from brutex.data.lake_invariants import (
    LakeFinding,
    PartitionCoords,
    classify_bar_keys,
    detect_legacy_drift,
    log_lake_finding,
)
from brutex.data.models import CatalogEntry
from brutex.data.partition import BAR_DIR_NAME, parse_partition_path

__all__ = ["ReconcileResult", "reconcile_catalog_from_lake"]

#: One ``catalog.reconcile.parquet_skip`` warning per unreadable partition file, so a
#: corrupt / present-but-unreadable Parquet that would otherwise crash the FIRST step of
#: every sweep load + F&O window derivation is surfaced + skipped, never silent (GAP #9).
#: A present-but-unreadable fault (corrupt decode, or a NON-ENOENT OSError — directory /
#: permission / EIO) additionally lands on ``ReconcileResult.unreadable`` so the launch
#: REFUSES; a ``FileNotFoundError`` vanished file stays a benign skip (#1924 round 2 M3).
#: A PRESENT, readable, EXACTLY-0-row partition lands on the distinct
#: ``ReconcileResult.empty_partitions`` (one ``catalog.reconcile.empty_partition`` WARN) so
#: the launch REFUSES it too: no BruteX writer ever emits a valid 0-row parquet
#: (``ParquetWriter.write`` raises ``WriterError`` on empty input, the all-filtered heal
#: UNLINKS the partition, a footer-last crash truncates → ``PolarsError`` above), so a
#: present 0-row file is unambiguously externally-forged / anomalous, never a legitimately
#: data-less month (which simply has NO file — benign ENOENT). DEC-LAKE-STATS-MANIFEST-001
#: residual R2N-06.
_LOG = structlog.get_logger("brutex.data.catalog_reconcile")

#: Exception set for the per-file min/max Parquet read below: a
#: :class:`polars.exceptions.PolarsError` decode fault OR a stored
#: out-of-``datetime``-range timestamp conversion fault
#: (:func:`datetime_out_of_range_errors` — the round-18
#: DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 fold, present-but-unreadable in effect).
#: Hoisted to a module-level constant explicitly typed
#: ``tuple[type[BaseException], ...]`` so the ``except`` site is a plain tuple of
#: exception classes — mypy ``--strict`` rejects a ``*``-unpacked variadic tuple
#: written inline in an ``except`` clause. Behaviour is identical to the inline
#: ``(pl.exceptions.PolarsError, *datetime_out_of_range_errors())``:
#: ``datetime_out_of_range_errors()`` returns a constant tuple, so building it
#: once at import equals rebuilding it per file.
_PARQUET_UNREADABLE_ERRORS: tuple[type[BaseException], ...] = (
    pl.exceptions.PolarsError,
    *datetime_out_of_range_errors(),
)


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
    unreadable
        Absolute paths of the CORRUPT / PRESENT-but-unreadable Parquet
        partition files — the distinct data-LOSS subset of ``skipped``
        (DEC-SWEEP-RECONCILE-CORRUPT-REFUSE-001, MIGRATION.md #1924). Populated
        by (a) the ``except pl.exceptions.PolarsError`` arm (a corrupt /
        version-incompatible decode) AND (b) the ``except OSError`` arm for
        every NON-``FileNotFoundError`` OS fault — a data-bearing DIRECTORY
        named ``<mm>.parquet/`` (``IsADirectoryError``), a chmod-000 /
        wrong-owner file (``PermissionError``), an ``EIO`` device fault — each
        holds REAL data the sweep would silently skip (review round 2 M3
        REVERSED the round-1 "all OSError → benign" carve-out, which holed the
        launch for present-but-unreadable data). ONLY three classes stay
        benignly on ``skipped`` and NEVER reach here (the false-refuse guard,
        "absent != corrupt"): a ``PartitionError`` (unparseable path) and a
        ``FileNotFoundError`` VANISHED file (ENOENT from a concurrent heal/fetch
        racing the glob — the escape-less launch refusal must not abort on a
        transient glitch). A PRESENT, readable, EXACTLY-0-row partition is a
        DISTINCT anomaly class handled on ``empty_partitions`` (below), never
        here (it is readable, not corrupt). Populated so the
        ``brutex sweep-all`` launch can REFUSE loudly on such a month — an
        INTERIOR hole invisible in the swept output that silently poisons the
        PDH/PDL/pivot/Fib anchors (bits 6-29+48-51) while ``data_snapshot_hash``
        stamps the holed series as canonical (Golden Rule #4). Defaulted ``()``
        so every existing three-/four-field construction stays compatible.
    empty_partitions
        Absolute paths of PRESENT, readable, EXACTLY-0-row Parquet partition
        files (DEC-LAKE-STATS-MANIFEST-001 residual R2N-06). A benign
        ``skipped`` count still increments (an empty partition IS a skip), but
        the distinct field lets the ``brutex sweep-all`` launch REFUSE the month
        loudly — mirroring the ``unreadable`` corrupt-month refusal. RE-FUTE
        SAFETY (why refusing cannot false-refuse a legitimate lake): no BruteX
        writer ever emits a valid 0-row parquet — ``ParquetWriter.write`` raises
        ``WriterError`` on empty input, the all-filtered heal quarantine-UNLINKS
        the partition, and a footer-last-write crash truncates the file (read →
        ``PolarsError`` → ``unreadable`` above). A legitimately data-less month
        simply has NO partition file (absence = benign ENOENT), and a
        legitimately THIN present month (holiday / half-day / late-listing first
        partial month) has real rows > 0, so it is UNAFFECTED — refusal is scoped
        to the unambiguous exactly-0-row present case (a row-COUNT density oracle
        is DEFERRED — DEC-RECONCILE-ROW-DENSITY-ORACLE-001, follow-up only).
        Defaulted ``()`` so every existing construction stays compatible.
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
    unreadable: tuple[str, ...] = ()
    empty_partitions: tuple[str, ...] = ()


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
    unreadable: list[str] = []
    empty_partitions: list[str] = []
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
        if check_invariants:
            symbol_dirs.setdefault(
                (cast(str, coords["exchange"]), cast(str, coords["segment"])), set()
            ).add(cast(str, coords["symbol"]))
        # GAP #9: a single unreadable Parquet file (a polars PolarsError decode
        # failure, or an OSError fault) must NOT crash the whole reconcile — which
        # runs at the START of every sweep load + F&O window derivation. Log + skip that
        # one file (counted in `skipped`) and continue; every readable partition still
        # reconciles. NARROW set (the three arms below): a genuine code bug still
        # propagates (Golden Rule #1). The three arms split the fault by MEANING:
        # PolarsError + `datetime_out_of_range_errors` (the round-18
        # DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 fold: a hand-planted Datetime column
        # holding an out-of-`datetime`-range i64 value, e.g. 2**62 µs, makes
        # `min()`/`max()` raise OverflowError — pre-fix ONE such bars file crashed
        # the whole reconcile, and with it every sweep load; if min/max convert,
        # the downstream `classify_bar_keys` min/max convert too, so the ONE guard
        # covers the L1 piggyback as well) + non-ENOENT OSError are
        # present-but-unreadable (→ `unreadable` → launch REFUSES), while
        # FileNotFoundError alone is a benign vanished-file skip.
        try:
            # Guarded literal read of ONE concrete walked file
            # (DEC-SPOT-DERIVE-TS-RANGE-HARDENING-001, the #1594 round-15
            # metachar law — held by construction inside read_parquet_regular
            # — a `[`-carrying symbol dir must never glob-read a sibling's
            # rows into this row's reconcile stats; +
            # DEC-LAKE-READ-INODE-GUARD-001 fd validation).
            timestamps = read_parquet_regular(path, columns=("timestamp",)).get_column("timestamp")
            row_count = timestamps.len()
            if row_count == 0:
                # DEC-LAKE-STATS-MANIFEST-001 residual R2N-06: a PRESENT, readable,
                # EXACTLY-0-row partition is NOT a benign absence — no BruteX writer
                # emits a valid 0-row parquet (write() raises WriterError on empty
                # input; the all-filtered heal quarantine-UNLINKS the partition; a
                # footer-last crash truncates → PolarsError above), so a present
                # 0-row file is externally-forged / anomalous. Skip-and-COUNT it
                # (an empty partition IS a skip) AND record its path DISTINCTLY on
                # ``empty_partitions`` + one loud WARN, so the sweep launch can
                # REFUSE the month (else the whole month silently vanishes with a
                # data_snapshot_hash bit-identical to the drop, Golden Rule #4).
                # A legitimately data-less month has NO file (benign ENOENT); a
                # legitimately THIN present month has real rows > 0 and is
                # untouched — a row-COUNT density oracle is DEFERRED
                # (DEC-RECONCILE-ROW-DENSITY-ORACLE-001, follow-up only).
                _LOG.warning("catalog.reconcile.empty_partition", path=str(path))
                skipped += 1
                empty_partitions.append(str(path))
                continue
            first_ts = timestamps.min()
            last_ts = timestamps.max()
            # Guarded raw-hash read (DEC-LAKE-READ-INODE-GUARD-001, the
            # lake-guards fold): read_regular_bytes fd-validates the SAME inode
            # before hashing — a planted non-regular inode raises
            # NotRegularFileError (an OSError → the unreadable arm below),
            # never a hang.
            raw_hash = hashlib.sha256(read_regular_bytes(path)).hexdigest()
        except _PARQUET_UNREADABLE_ERRORS as exc:
            # DEC-SWEEP-RECONCILE-CORRUPT-REFUSE-001 (#1924): a CORRUPT /
            # version-incompatible partition file — or one whose Datetime column
            # holds an out-of-range value (the round-18 OverflowError class, folded
            # into THIS unreadable arm at the club-S1 merge: present-but-unreadable
            # in effect, the same interior-hole class) — is an INTERIOR data hole,
            # not a benign absence — record its absolute path DISTINCTLY so the
            # sweep launch can REFUSE loudly (a silent skip poisons the
            # PDH/PDL/pivot/Fib anchors + hashes the holed series as canonical,
            # Golden Rule #4). The ``PartitionError`` (unparseable path) +
            # zero-row skips above stay benign ``skipped`` counts and never reach
            # here (the false-refuse guard).
            _LOG.warning("catalog.reconcile.parquet_skip", path=str(path), error=str(exc))
            skipped += 1
            unreadable.append(str(path))
            continue
        except FileNotFoundError as exc:
            # #1924 review round 2 (M3): ``FileNotFoundError`` (ENOENT) ONLY is a
            # genuine VANISHED file — a concurrent heal/fetch raced the glob and the
            # partition was gone by read time — data ABSENCE, not data LOSS. Count it
            # a benign ``skipped`` (WARN kept) and NEVER on ``unreadable``: the
            # escape-less sweep-launch refusal must not abort a multi-day launch on a
            # transient glitch ("absent != corrupt"). This is the ONLY OSError subclass
            # treated benignly — every other is present-but-unreadable (the arm below).
            _LOG.warning("catalog.reconcile.parquet_skip", path=str(path), error=str(exc))
            skipped += 1
            continue
        except OSError as exc:
            # #1924 review round 2 (M3): a NON-ENOENT OSError is a PRESENT-but-
            # UNREADABLE partition, NOT absence — the exact silent-partial class this
            # refusal exists to close. Concrete shapes: a data-bearing DIRECTORY named
            # ``<mm>.parquet/`` (the fd-validated readers raise
            # ``NotRegularFileError``/``IsADirectoryError`` — the #1558 FIX-ROUND-8
            # dataset-directory shape); a planted non-regular inode
            # (``NotRegularFileError``, DEC-LAKE-READ-INODE-GUARD-001); a chmod-000 /
            # wrong-owner file (``PermissionError``); an ``EIO`` device fault. Each
            # (bar the planted inode) holds REAL data the
            # sweep would silently skip (poisoning the PDH/PDL/pivot/Fib anchors while
            # ``data_snapshot_hash`` stamps the holed series canonical), so it joins
            # ``unreadable`` and REFUSES the launch loudly (present != absent). This
            # REVERSES the round-1 "all OSError → benign" carve-out, which holed the
            # launch for present-but-unreadable data.
            _LOG.warning("catalog.reconcile.parquet_skip", path=str(path), error=str(exc))
            skipped += 1
            unreadable.append(str(path))
            continue
        if check_invariants:
            # DEC-LAKE-HEAL-001 L1: classification rides the timestamp column this
            # loop ALREADY read — zero extra I/O (spy-pinned), only the n_unique
            # hash pass. overlap_rows stays None (no canonical-sibling read here).
            partition_coords = PartitionCoords(
                exchange=cast(str, coords["exchange"]),
                segment=cast(str, coords["segment"]),
                symbol=cast(str, coords["symbol"]),
                interval=cast(str, coords["interval"]),
                year=cast(int, coords["year"]),
                month=cast(int, coords["month"]),
            )
            for finding in classify_bar_keys(partition_coords, timestamps):
                if finding is not None:
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
        unreadable=tuple(unreadable),
        empty_partitions=tuple(empty_partitions),
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
