"""The ONE bounded lake invariant scanner + census (DEC-LAKE-HEAL-001).

PR #1067 (DEC-LAKE-WRITE-UNIQUE-001) made every WRITE store unique keep-first
bars and self-heal a poisoned partition on the next merge touch — but a settled
month nobody re-pulls stays poisoned forever, cross-file/foreign-month rows are
checked nowhere, and legacy-key dir drift has only read-side display recovery
(#911). This module is the DETECTION half of DEC-LAKE-HEAL-001: one bounded
scan pass that classifies, per ``bars/`` month file, (a) intra-file duplicate
timestamps, (b) rows whose UTC ``(year, month)`` differs from the file's (which
provably subsumes cross-file overlap — one file per UTC month per series), and,
per ``bars/<exch>/<seg>/`` directory, (c) #911 legacy-key drift; plus, per
``options/<series>/`` month file, (d) intra-file duplicate ``(side, timestamp)``
keys; plus, per ``bars/`` month file, (e) **price-sanity** violations —
non-positive / null prices or broken OHLC ordering
(DEC-STORED-DATA-VALIDATION-001, riding the SAME per-file read via
:data:`BAR_SCAN_COLUMNS`; the ``options/`` tree stays dup-census only — the
same honest scope as its foreign/overlap follow-up). A price finding is
OBSERVE-ONLY: the healer explicitly skips it (healing is strictly
row-removing/row-reassigning, never value-editing — "repairing" a price would
fabricate data). Findings are frozen value dataclasses consumed by the healer
(``brutex.data.lake_heal``) and the census surfaces.

**Month invariant (recon-B §1e):** a bar's canonical month file is
``partition_for_timestamp(ts)`` = the **UTC** ``(year, month)`` — the 1day
midnight-IST straddle (a derived daily bar stamped 18:30 UTC of the previous
calendar day) and the session-exempt GIFTNIFTY early-IST bars are CONSISTENT
with UTC routing; comparing IST months would false-flag them, so this scanner
never does.

**Cost honesty (Golden Rule #10):** the scan is O(files-in-scope) TOTAL, per
file O(footer + ONE read of the 5 :data:`BAR_SCAN_COLUMNS` columns — the
timestamp key + OHLC; was 1 column before DEC-STORED-DATA-VALIDATION-001, so
the per-file read is wider but the pass/file counts are unchanged) — the
column read scales with the file's ROW COUNT, so this is NOT O(1). L2 bounds
it by coordinates × months; L3 pays the full O(files) once per lake content
token. A file with foreign rows costs one extra targeted read of its
canonical sibling to split ``overlap_rows``. A bar file MISSING a price
column now skips (unreadable-columns WARN) instead of being dup-scanned —
such a file violates the canonical 7-column schema and is itself a defect.

This module must NOT import ``catalog_reconcile`` (which imports THIS module
for its free L1 piggyback) or ``lake_heal`` (which imports this module) —
the dependency edges stay acyclic.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import polars as pl
import structlog

from brutex.data.errors import PartitionError, parquet_read_skip_errors
from brutex.data.lake_keys import spot_key_candidates
from brutex.data.partition import (
    BAR_DIR_NAME,
    SPOT_PROBE_SEGMENTS,
    build_partition_path,
    parse_partition_path,
    partition_for_timestamp,
)
from brutex.data.price_sanity import PRICE_SANITY_COLUMNS, count_price_violations

_LOG = structlog.get_logger("brutex.data.lake_invariants")

#: The uniqueness key of a ``bars/`` month file (one row per timestamp).
UNIQUE_KEY_BARS: Final[tuple[str, ...]] = ("timestamp",)

#: The uniqueness key of an ``options/<series>/`` month file (the option writer's
#: own established merge key, ``option_parquet_writer._merge_partition``).
UNIQUE_KEY_OPTIONS: Final[tuple[str, ...]] = ("side", "timestamp")

#: The columns ONE ``bars/`` month-file scan pass reads: the uniqueness key
#: PLUS the four OHLC price columns, so the price-sanity check
#: (DEC-STORED-DATA-VALIDATION-001) rides the SAME per-file read — same file
#: count, same single pass, the scan stays O(files) total (the per-file read
#: grows from 1 to 5 columns; honest envelope in the module docstring).
BAR_SCAN_COLUMNS: Final[tuple[str, ...]] = (*UNIQUE_KEY_BARS, *PRICE_SANITY_COLUMNS)

#: Engine index lake coords (symbol dir = stripped ``trading_symbol``; groww
#: ``NSE-NIFTY`` etc. per ``adapters/groww/constants.py:97-100``; lake key law
#: recon-B §1a). The census answers "are THESE series duplicate-free" everywhere.
ENGINE_INDEX_COORDS: Final[tuple[tuple[str, str, str], ...]] = (
    ("NSE", "INDEX", "NIFTY"),
    ("NSE", "INDEX", "BANKNIFTY"),
    ("BSE", "INDEX", "SENSEX"),
)

#: The expired-options tree dirname under the lake root (mirrors the private
#: ``option_parquet_writer._OPTIONS_SUBDIR`` — the layout is
#: ``options/<series>/<YYYY>-<MM>.parquet``).
OPTIONS_DIR_NAME: Final[str] = "options"

#: The census segment label for the expired-options tree (NOT a ``bars/``
#: segment — :data:`brutex.data.partition.VALID_SEGMENTS` stays untouched).
OPTIONS_SEGMENT: Final[str] = "OPTIONS"

#: The ``bars/`` segments in census display order.
_BAR_SEGMENTS: Final[tuple[str, ...]] = ("INDEX", "CASH", "FNO")

#: Human label for the engine-index verdict lines.
_ENGINE_LABEL: Final[str] = "NIFTY/BANKNIFTY/SENSEX"

#: Cap on offender names in :attr:`LakeCensus.engine_detail`.
_ENGINE_DETAIL_CAP: Final[int] = 5


@dataclass(frozen=True, slots=True)
class PartitionCoords:
    """The six coordinates addressing one ``bars/`` month partition file."""

    exchange: str
    segment: str
    symbol: str
    interval: str
    year: int
    month: int

    @property
    def series(self) -> tuple[str, str, str, str]:
        """The series key ``(exchange, segment, symbol, interval)``."""
        return (self.exchange, self.segment, self.symbol, self.interval)

    def relpath(self) -> str:
        """The lake-relative path, e.g. ``bars/NSE/INDEX/NIFTY/1minute/2025/09.parquet``."""
        return (
            f"{BAR_DIR_NAME}/{self.exchange}/{self.segment}/{self.symbol}/"
            f"{self.interval}/{self.year:04d}/{self.month:02d}.parquet"
        )


@dataclass(frozen=True, slots=True)
class IntraFileDup:
    """``physical_rows > unique timestamp count`` in ONE ``bars/`` month file.

    ``path`` is the concrete file when emitted by :func:`scan_lake_invariants`
    or the reconcile piggyback; the pure :func:`classify_bar_keys` core stamps
    the lake-relative ``coords.relpath()`` and callers substitute the real path.
    """

    path: Path
    coords: PartitionCoords
    physical_rows: int
    unique_keys: int

    @property
    def dup_rows(self) -> int:
        """The surplus physical rows (``physical_rows - unique_keys``)."""
        return self.physical_rows - self.unique_keys


@dataclass(frozen=True, slots=True)
class ForeignMonthRows:
    """Rows whose UTC ``(year, month)`` != the file's ``(year, month)``.

    Subsumes cross-file overlap: one series holds exactly ONE file per UTC month
    (``partition_for_timestamp`` routing + single-partition write enforcement,
    recon-B §4), so any same-timestamp row in two files is by construction a
    foreign row in one of them. ``overlap_rows`` = the foreign keys ALSO present
    in their canonical month file (the CrossFileOverlap sub-case); ``None`` =
    not classified (the L1 free path skips the extra canonical-file read).
    """

    path: Path
    coords: PartitionCoords
    foreign_rows: int
    overlap_rows: int | None


@dataclass(frozen=True, slots=True)
class LegacyKeyDrift:
    """A ``bars/<exch>/<seg>/`` symbol DIR whose key is a #911 drift of a canonical key."""

    exchange: str
    segment: str
    drifted_symbol: str
    canonical_symbol: str
    drifted_dir: Path
    intervals: tuple[str, ...]
    files: int


@dataclass(frozen=True, slots=True)
class OptionIntraFileDup:
    """``physical_rows > unique (side, timestamp)`` count in ONE ``options/<series>/`` file."""

    path: Path
    series: str
    physical_rows: int
    unique_keys: int


@dataclass(frozen=True, slots=True)
class PriceSanityRows:
    """Rows violating price positivity / OHLC ordering in ONE ``bars/`` month file.

    DEC-STORED-DATA-VALIDATION-001 — the census/heal scanner previously read
    NO price column, so its "0 findings" verdict was BLIND to price
    corruption (a stored negative / ``high < low`` bar). OBSERVE-ONLY by
    design: the healer NEVER consumes this finding (healing is strictly
    row-removing/row-reassigning — "repairing" a price would fabricate data,
    Golden Rule #1); remediation is a re-pull / manual quarantine, guided by
    the pinned ``lake.invariant.price_sanity_rows`` WARNING + the census.
    """

    path: Path
    coords: PartitionCoords
    bad_rows: int


#: Union of every finding kind one scan can emit. Importable by name
#: (``from brutex.data.lake_invariants import LakeFinding``) but intentionally
#: NOT listed in ``__all__``: it is a PEP 695 ``type`` statement, which some
#: static analysers (CodeQL) do not yet model as an ``__all__`` binding, and
#: type aliases are idiomatically not part of the ``import *`` surface anyway.
type LakeFinding = (
    IntraFileDup | ForeignMonthRows | LegacyKeyDrift | OptionIntraFileDup | PriceSanityRows
)


@dataclass(frozen=True, slots=True)
class LakeScanReport:
    """Outcome of one :func:`scan_lake_invariants` pass.

    ``files_by_segment`` (per-``bars/``-segment scanned counts) and
    ``engine_files`` (scanned files on :data:`ENGINE_INDEX_COORDS` series) are
    defaulted trailing fields feeding :func:`build_lake_census`.
    """

    findings: tuple[LakeFinding, ...]
    files_scanned: int
    files_skipped: int
    series_scanned: int
    option_files_scanned: int
    option_files_skipped: int
    duration_s: float
    files_by_segment: tuple[tuple[str, int], ...] = ()
    engine_files: int = 0


def _utc_aware(ts: datetime) -> datetime:
    """Stamp a (defensive) tz-naive timestamp UTC so month routing never raises.

    Lake bars are stored ``timestamp[us, tz=UTC]``; a hand-planted naive column
    is routed as-if-UTC (deterministic) instead of crashing the whole scan.
    """
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts


def _count_foreign_rows(coords: PartitionCoords, timestamps: pl.Series) -> int:
    """Count ROWS whose UTC month differs from the file's month (O(1) fast path).

    Months are contiguous time intervals, so when the column's min AND max both
    route to the file's own ``(year, month)`` every row between them does too —
    a clean file never pays the O(rows) Python loop. A null timestamp (only
    plantable by hand) abstains: it carries no month to route.
    """
    home = (coords.year, coords.month)
    first = timestamps.min()
    last = timestamps.max()
    if (
        isinstance(first, datetime)
        and isinstance(last, datetime)
        and partition_for_timestamp(_utc_aware(first)) == home
        and partition_for_timestamp(_utc_aware(last)) == home
    ):
        return 0
    return sum(
        1
        for ts in timestamps.to_list()
        if isinstance(ts, datetime) and partition_for_timestamp(_utc_aware(ts)) != home
    )


def _foreign_timestamps(coords: PartitionCoords, timestamps: pl.Series) -> set[datetime]:
    """The UNIQUE (UTC-normalised) timestamps routed outside the file's month.

    Anomaly-only cost: called solely for files :func:`classify_bar_keys` already
    flagged foreign, to drive the targeted canonical-sibling overlap read.
    """
    home = (coords.year, coords.month)
    return {
        _utc_aware(ts)
        for ts in timestamps.to_list()
        if isinstance(ts, datetime) and partition_for_timestamp(_utc_aware(ts)) != home
    }


def classify_bar_keys(
    coords: PartitionCoords, timestamps: pl.Series
) -> tuple[IntraFileDup | None, ForeignMonthRows | None]:
    """PURE classification of one already-read timestamp column (the L1 piggyback core).

    ``physical`` = length; ``unique`` = ``n_unique``; ``foreign`` = count of
    timestamps whose :func:`~brutex.data.partition.partition_for_timestamp`
    differs from ``(coords.year, coords.month)``. ``overlap_rows=None`` here (no
    extra I/O at this layer). Shared verbatim by the scanner and the reconcile
    hook — ONE scanner core (CLAUDE.md §11 COMMON). The returned findings carry
    the lake-relative ``coords.relpath()`` as ``path``; callers that know the
    concrete file substitute the absolute path via :func:`dataclasses.replace`.

    Parameters
    ----------
    coords
        The file's partition coordinates.
    timestamps
        The file's ``timestamp`` column (already read by the caller).

    Returns
    -------
    tuple[IntraFileDup | None, ForeignMonthRows | None]
        The optional dup + foreign findings; ``(None, None)`` for a clean or
        empty column.
    """
    physical = timestamps.len()
    if physical == 0:
        return None, None
    dup: IntraFileDup | None = None
    unique = timestamps.n_unique()
    if physical > unique:
        dup = IntraFileDup(
            path=Path(coords.relpath()),
            coords=coords,
            physical_rows=physical,
            unique_keys=unique,
        )
    foreign: ForeignMonthRows | None = None
    foreign_rows = _count_foreign_rows(coords, timestamps)
    if foreign_rows:
        foreign = ForeignMonthRows(
            path=Path(coords.relpath()),
            coords=coords,
            foreign_rows=foreign_rows,
            overlap_rows=None,
        )
    return dup, foreign


def detect_legacy_drift(
    symbol_dirs: Mapping[tuple[str, str], Sequence[str]],
    *,
    lake_root: Path,
    canonical_keys: Collection[str] | None = None,
) -> tuple[LegacyKeyDrift, ...]:
    """Drift detection over already-enumerated symbol dirs (no Parquet file I/O).

    ONLY for ``segment`` in :data:`~brutex.data.partition.SPOT_PROBE_SEGMENTS`
    (``"INDEX"``/``"CASH"``) — FNO dirs are ``groww_symbol``-keyed and
    legitimately ``"<exch>-"``-prefixed (``parse_fno_symbol``), so the
    prefix-strip ladder must never touch them (honest scope, documented). A dir
    ``D`` is drifted iff a :func:`~brutex.data.lake_keys.spot_key_candidates`
    candidate ``C != D`` and (``C`` is a sibling dir under the same
    ``(exchange, segment)`` OR ``C`` in *canonical_keys*). Genuinely unknown
    strays are NOT flagged (mirrors #911's "unscored stray STAYS dropped").
    The finding's ``intervals`` / ``files`` profile is one directory listing of
    the drifted dir (anomaly-only; no Parquet reads).

    Parameters
    ----------
    symbol_dirs
        ``(exchange, segment)`` → the symbol directory names seen under it.
    lake_root
        Lake root, used to build the absolute ``drifted_dir`` + its profile.
    canonical_keys
        Optional extra canonical key universe (e.g. instrument-master
        ``symbol_name`` keys) a candidate may resolve into when the canonical
        sibling dir does not (yet) exist.

    Returns
    -------
    tuple[LegacyKeyDrift, ...]
        Deterministically ordered drift findings.
    """
    canon = frozenset(canonical_keys) if canonical_keys is not None else frozenset()
    findings: list[LegacyKeyDrift] = []
    for exchange, segment in sorted(symbol_dirs):
        if segment not in SPOT_PROBE_SEGMENTS:
            continue
        siblings = set(symbol_dirs[(exchange, segment)])
        for name in sorted(siblings):
            canonical = _drift_canonical(name, exchange, siblings, canon)
            if canonical is None:
                continue
            drifted_dir = lake_root / BAR_DIR_NAME / exchange / segment / name
            intervals, files = _dir_profile(drifted_dir)
            findings.append(
                LegacyKeyDrift(
                    exchange=exchange,
                    segment=segment,
                    drifted_symbol=name,
                    canonical_symbol=canonical,
                    drifted_dir=drifted_dir,
                    intervals=intervals,
                    files=files,
                )
            )
    return tuple(findings)


def _drift_canonical(
    name: str, exchange: str, siblings: Collection[str], canonical_keys: Collection[str]
) -> str | None:
    """The first ladder candidate of *name* that names a sibling dir or canonical key."""
    for candidate in spot_key_candidates(name, exchange):
        if candidate in siblings or candidate in canonical_keys:
            return candidate
    return None


def _dir_profile(drifted_dir: Path) -> tuple[tuple[str, ...], int]:
    """The interval subdir names + total ``*.parquet`` count of one symbol dir."""
    if not drifted_dir.is_dir():
        return (), 0
    intervals = tuple(sorted(p.name for p in drifted_dir.iterdir() if p.is_dir()))
    files = sum(1 for _ in drifted_dir.rglob("*.parquet"))
    return intervals, files


def log_lake_finding(finding: LakeFinding) -> None:
    """Emit the pinned WARNING event for one finding (shared scanner/L1 logging).

    Events + fields (pinned by tests): ``lake.invariant.intra_file_dup``
    (``partition, physical_rows, unique_timestamps``),
    ``lake.invariant.foreign_month_rows`` (``partition, foreign_rows,
    overlap_rows``), ``lake.invariant.legacy_key_drift`` (``drifted, canonical,
    exchange, segment, files`` — drifted/canonical via ``repr()``, the #907
    convention), ``lake.invariant.option_intra_file_dup`` (``partition,
    physical_rows, unique_keys``), ``lake.invariant.price_sanity_rows``
    (``partition, bad_rows`` — DEC-STORED-DATA-VALIDATION-001).
    """
    if isinstance(finding, PriceSanityRows):
        _LOG.warning(
            "lake.invariant.price_sanity_rows",
            partition=finding.coords.relpath(),
            bad_rows=finding.bad_rows,
        )
    elif isinstance(finding, IntraFileDup):
        _LOG.warning(
            "lake.invariant.intra_file_dup",
            partition=finding.coords.relpath(),
            physical_rows=finding.physical_rows,
            unique_timestamps=finding.unique_keys,
        )
    elif isinstance(finding, ForeignMonthRows):
        _LOG.warning(
            "lake.invariant.foreign_month_rows",
            partition=finding.coords.relpath(),
            foreign_rows=finding.foreign_rows,
            overlap_rows=finding.overlap_rows,
        )
    elif isinstance(finding, LegacyKeyDrift):
        _LOG.warning(
            "lake.invariant.legacy_key_drift",
            drifted=repr(finding.drifted_symbol),
            canonical=repr(finding.canonical_symbol),
            exchange=finding.exchange,
            segment=finding.segment,
            files=finding.files,
        )
    else:
        _LOG.warning(
            "lake.invariant.option_intra_file_dup",
            partition=f"{OPTIONS_DIR_NAME}/{finding.series}/{finding.path.name}",
            physical_rows=finding.physical_rows,
            unique_keys=finding.unique_keys,
        )


def _read_key_columns(path: Path, columns: Sequence[str]) -> pl.DataFrame:
    """The ONE per-file read seam (spy tests count calls through this)."""
    return pl.read_parquet(path, columns=list(columns))


def _pause(per_file_pause: Callable[[], None] | None) -> None:
    """Run the injected throttle callback, if any (the L3 gentleness seam)."""
    if per_file_pause is not None:
        per_file_pause()


def _bar_files(
    lake_root: Path,
    *,
    exchange: str | None,
    segment: str | None,
    symbol: str | None,
    interval: str | None,
) -> list[Path]:
    """Every ``<year>/<month>.parquet`` bar file matching the coordinate filters.

    Deliberately its OWN tiny glob with the SAME six-segment shape as
    ``catalog_reconcile._partition_files`` (importing the sibling's private
    would be the worse coupling — documented as the same pattern, not shared
    code). The fixed depth never matches the ``options/`` tree, and the
    lake-root-sibling ``.quarantine/`` / ``.locks/`` dirs live outside ``bars/``
    so quarantined copies are never re-scanned.
    """
    base = lake_root / BAR_DIR_NAME
    if not base.exists():
        return []
    pattern = f"{exchange or '*'}/{segment or '*'}/{symbol or '*'}/{interval or '*'}/*/*.parquet"
    return sorted(base.glob(pattern))


def _classify_overlap(
    lake_root: Path,
    coords: PartitionCoords,
    foreign_ts: set[datetime],
    per_file_pause: Callable[[], None] | None,
    canonical_cache: dict[tuple[int, int], set[datetime] | None],
) -> int | None:
    """Split one file's foreign keys into the overlap-with-canonical count.

    One targeted read per affected canonical month file per SERIES (memoised in
    *canonical_cache*): ``overlap`` = |foreign ∩ canonical file's timestamps|.
    A missing canonical file contributes 0 (pure misplacement); an UNREADABLE
    canonical file leaves the finding honestly unclassified (``None``) after a
    ``lake.invariant.scan_skip`` WARNING.
    """
    overlap = 0
    unresolved = False
    grouped: dict[tuple[int, int], set[datetime]] = {}
    for ts in foreign_ts:
        grouped.setdefault(partition_for_timestamp(ts), set()).add(ts)
    for year, month in sorted(grouped):
        if (year, month) not in canonical_cache:
            canonical_cache[(year, month)] = _canonical_timestamps(
                lake_root, coords, year, month, per_file_pause
            )
        stored = canonical_cache[(year, month)]
        if stored is None:
            unresolved = True
            continue
        overlap += len(grouped[(year, month)] & stored)
    return None if unresolved else overlap


def _canonical_timestamps(
    lake_root: Path,
    coords: PartitionCoords,
    year: int,
    month: int,
    per_file_pause: Callable[[], None] | None,
) -> set[datetime] | None:
    """The canonical ``(year, month)`` sibling file's timestamp set.

    ``set()`` for a missing / impossible canonical partition (pure
    misplacement); ``None`` for an unreadable one (honest abstain).
    """
    try:
        canonical = build_partition_path(
            lake_root=lake_root,
            exchange=coords.exchange,
            segment=coords.segment,
            symbol=coords.symbol,
            interval=coords.interval,
            year=year,
            month=month,
        )
    except PartitionError:
        return set()  # out-of-range coords — no canonical partition can exist
    if not canonical.exists():
        return set()
    try:
        frame = _read_key_columns(canonical, UNIQUE_KEY_BARS)
    except parquet_read_skip_errors() as exc:
        _LOG.warning("lake.invariant.scan_skip", path=str(canonical), error=str(exc))
        _pause(per_file_pause)
        return None
    _pause(per_file_pause)
    return {
        _utc_aware(ts) for ts in frame.get_column("timestamp").to_list() if isinstance(ts, datetime)
    }


def scan_lake_invariants(
    lake_root: Path,
    *,
    exchange: str | None = None,
    segment: str | None = None,
    symbol: str | None = None,
    interval: str | None = None,
    months: Collection[tuple[int, int]] | None = None,
    include_options: bool = True,
    canonical_keys: Collection[str] | None = None,
    per_file_pause: Callable[[], None] | None = None,
) -> LakeScanReport:
    """The ONE bounded scanner over ``bars/`` (+ optionally ``options/``).

    Cost honesty (Golden Rule #10): O(files-in-scope) TOTAL, per file
    O(footer + ONE key-column read) — the column read scales with the file's
    ROW COUNT, so this is NOT O(1); L2 bounds it by coordinates × *months*, L3
    pays the full O(files) once per lake content token. At most ONE read call
    per file in the clean case (spy-pinned); a file with foreign rows costs one
    extra targeted read of its canonical sibling to split ``overlap_rows``.
    *per_file_pause* runs after every file read — including a failed/skipped
    read and the targeted canonical read (the L3 throttle seam). An unreadable
    file → :func:`~brutex.data.errors.parquet_read_skip_errors` skip + a
    ``lake.invariant.scan_skip`` WARNING (mirrors
    ``catalog.reconcile.parquet_skip``).

    Parameters
    ----------
    lake_root
        Top-level lake directory (the parent of ``bars/`` + ``options/``).
    exchange, segment, symbol, interval
        Optional ``bars/`` coordinate filters; ``None`` widens to ``*``.
    months
        Optional ``(year, month)`` scope; an out-of-scope bar file is skipped
        BEFORE reading (the L2 bound; counted in neither ``files_scanned`` nor
        ``files_skipped``). Applies to the ``bars/`` tree only.
    include_options
        Scan the ``options/`` tree for ``(side, timestamp)`` dups.
    canonical_keys
        Extra canonical key universe for :func:`detect_legacy_drift`.
    per_file_pause
        Optional post-read throttle callback (L3 passes a sleep; L1/L2 pass
        ``None``).

    Returns
    -------
    LakeScanReport
        Findings + scan counters (``duration_s`` wall clock).
    """
    started = time.perf_counter()
    month_scope = set(months) if months is not None else None
    findings: list[LakeFinding] = []
    files_scanned = 0
    files_skipped = 0
    engine_files = 0
    files_by_segment: dict[str, int] = {}
    symbol_dirs: dict[tuple[str, str], set[str]] = {}
    per_series: dict[tuple[str, str, str, str], list[tuple[PartitionCoords, Path]]] = {}
    for path in _bar_files(
        lake_root, exchange=exchange, segment=segment, symbol=symbol, interval=interval
    ):
        try:
            parsed = parse_partition_path(path, lake_root=lake_root)
        except PartitionError:
            files_skipped += 1
            continue
        coords = PartitionCoords(
            exchange=cast(str, parsed["exchange"]),
            segment=cast(str, parsed["segment"]),
            symbol=cast(str, parsed["symbol"]),
            interval=cast(str, parsed["interval"]),
            year=cast(int, parsed["year"]),
            month=cast(int, parsed["month"]),
        )
        symbol_dirs.setdefault((coords.exchange, coords.segment), set()).add(coords.symbol)
        if month_scope is not None and (coords.year, coords.month) not in month_scope:
            continue  # the L2 scope bound — never read
        per_series.setdefault(coords.series, []).append((coords, path))

    series_scanned = 0
    for series_key in sorted(per_series):
        entries = sorted(per_series[series_key], key=lambda cp: (cp[0].year, cp[0].month))
        scanned_in_series = 0
        pending_overlap: list[tuple[int, PartitionCoords, set[datetime]]] = []
        canonical_cache: dict[tuple[int, int], set[datetime] | None] = {}
        for coords, path in entries:
            try:
                frame = _read_key_columns(path, BAR_SCAN_COLUMNS)
            except parquet_read_skip_errors() as exc:
                _LOG.warning("lake.invariant.scan_skip", path=str(path), error=str(exc))
                files_skipped += 1
                _pause(per_file_pause)
                continue
            _pause(per_file_pause)
            files_scanned += 1
            scanned_in_series += 1
            files_by_segment[coords.segment] = files_by_segment.get(coords.segment, 0) + 1
            if (coords.exchange, coords.segment, coords.symbol) in ENGINE_INDEX_COORDS:
                engine_files += 1
            timestamps = frame.get_column("timestamp")
            dup, foreign = classify_bar_keys(coords, timestamps)
            if dup is not None:
                dup = replace(dup, path=path)
                log_lake_finding(dup)
                findings.append(dup)
            if foreign is not None:
                foreign = replace(foreign, path=path)
                pending_overlap.append(
                    (len(findings), coords, _foreign_timestamps(coords, timestamps))
                )
                findings.append(foreign)
            # Price-sanity census (DEC-STORED-DATA-VALIDATION-001): rides the
            # SAME per-file read (BAR_SCAN_COLUMNS carries the OHLC columns),
            # one vectorized pass. OBSERVE-ONLY — the healer never consumes it.
            bad_rows = count_price_violations(frame)
            if bad_rows:
                price_finding = PriceSanityRows(path=path, coords=coords, bad_rows=bad_rows)
                log_lake_finding(price_finding)
                findings.append(price_finding)
        if scanned_in_series:
            series_scanned += 1
        for idx, coords, foreign_ts in pending_overlap:
            overlap = _classify_overlap(
                lake_root, coords, foreign_ts, per_file_pause, canonical_cache
            )
            updated = replace(cast(ForeignMonthRows, findings[idx]), overlap_rows=overlap)
            log_lake_finding(updated)
            findings[idx] = updated

    for drift in detect_legacy_drift(
        {key: sorted(names) for key, names in symbol_dirs.items()},
        lake_root=lake_root,
        canonical_keys=canonical_keys,
    ):
        log_lake_finding(drift)
        findings.append(drift)

    option_files_scanned = 0
    option_files_skipped = 0
    if include_options:
        options_root = lake_root / OPTIONS_DIR_NAME
        if options_root.is_dir():
            # Any dir containing *.parquet is scanned — no regex gate, an
            # unrecognized series dir is still checked (Golden Rule #1).
            for series_dir in sorted(p for p in options_root.iterdir() if p.is_dir()):
                for path in sorted(series_dir.glob("*.parquet")):
                    try:
                        frame = _read_key_columns(path, UNIQUE_KEY_OPTIONS)
                    except parquet_read_skip_errors() as exc:
                        _LOG.warning("lake.invariant.scan_skip", path=str(path), error=str(exc))
                        option_files_skipped += 1
                        _pause(per_file_pause)
                        continue
                    _pause(per_file_pause)
                    option_files_scanned += 1
                    physical = frame.height
                    unique = frame.unique(subset=list(UNIQUE_KEY_OPTIONS)).height
                    if physical > unique:
                        finding = OptionIntraFileDup(
                            path=path,
                            series=series_dir.name,
                            physical_rows=physical,
                            unique_keys=unique,
                        )
                        log_lake_finding(finding)
                        findings.append(finding)

    report = LakeScanReport(
        findings=tuple(findings),
        files_scanned=files_scanned,
        files_skipped=files_skipped,
        series_scanned=series_scanned,
        option_files_scanned=option_files_scanned,
        option_files_skipped=option_files_skipped,
        duration_s=time.perf_counter() - started,
        files_by_segment=tuple(sorted(files_by_segment.items())),
        engine_files=engine_files,
    )
    _LOG.info(
        "lake.invariant.scan_complete",
        files=report.files_scanned + report.option_files_scanned,
        findings=len(report.findings),
        duration_ms=round(report.duration_s * 1000.0, 3),
    )
    return report


# ── census ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SegmentCensus:
    """Per-segment clean/poisoned file counts (``OPTIONS`` = the options tree).

    ``price_files`` (DEC-STORED-DATA-VALIDATION-001) counts month files with at
    least one price-insane row — a defaulted trailing field so existing
    constructors stay valid; the ``OPTIONS`` row keeps a structural ``0``
    (the options tree is dup-census only — honest scope, mirroring the
    foreign/overlap follow-up note in :func:`build_lake_census`).
    """

    segment: str
    files: int
    clean: int
    dup_files: int
    foreign_files: int
    overlap_files: int
    drift_dirs: int
    price_files: int = 0


@dataclass(frozen=True, slots=True)
class LakeCensus:
    """The operator-facing census fold of one :class:`LakeScanReport`."""

    segments: tuple[SegmentCensus, ...]
    engine_clean: bool
    engine_detail: str
    findings_total: int
    files_scanned: int


def _engine_relevant(finding: LakeFinding) -> bool:
    """True when *finding* touches an :data:`ENGINE_INDEX_COORDS` series."""
    if isinstance(finding, OptionIntraFileDup):
        return False
    if isinstance(finding, LegacyKeyDrift):
        return (finding.exchange, finding.segment, finding.canonical_symbol) in (
            ENGINE_INDEX_COORDS
        )
    return (
        finding.coords.exchange,
        finding.coords.segment,
        finding.coords.symbol,
    ) in ENGINE_INDEX_COORDS


def _engine_offender(finding: LakeFinding) -> str:
    """A short human token naming one engine-series offender."""
    if isinstance(finding, LegacyKeyDrift):
        return f"drift {finding.drifted_symbol!r}->{finding.canonical_symbol!r}"
    if isinstance(finding, IntraFileDup):
        return f"dup {finding.coords.relpath()}"
    if isinstance(finding, ForeignMonthRows):
        return f"foreign {finding.coords.relpath()}"
    if isinstance(finding, PriceSanityRows):
        return f"price {finding.coords.relpath()}"
    return f"option-dup {OPTIONS_DIR_NAME}/{finding.series}/{finding.path.name}"


def build_lake_census(report: LakeScanReport) -> LakeCensus:
    """Fold one scan report into the per-segment + engine-verdict census.

    ``clean`` counts files with ZERO findings; a file carrying both a dup and a
    foreign finding is one poisoned file. Drift is a DIR-level count (the
    drifted dir's own files are also scanned + counted in ``files``). The
    ``OPTIONS`` row covers dups only — foreign/overlap classification for the
    options tree is a named follow-up (honest scope, DEC-LAKE-HEAL-001).
    """
    seg_files = dict(report.files_by_segment)
    dup_paths: dict[str, set[str]] = {}
    foreign_paths: dict[str, set[str]] = {}
    overlap_paths: dict[str, set[str]] = {}
    price_paths: dict[str, set[str]] = {}
    drift_dirs: dict[str, int] = {}
    engine_findings: list[LakeFinding] = []
    for finding in report.findings:
        if _engine_relevant(finding):
            engine_findings.append(finding)
        if isinstance(finding, OptionIntraFileDup):
            dup_paths.setdefault(OPTIONS_SEGMENT, set()).add(str(finding.path))
        elif isinstance(finding, LegacyKeyDrift):
            drift_dirs[finding.segment] = drift_dirs.get(finding.segment, 0) + 1
        elif isinstance(finding, IntraFileDup):
            dup_paths.setdefault(finding.coords.segment, set()).add(str(finding.path))
        elif isinstance(finding, PriceSanityRows):
            price_paths.setdefault(finding.coords.segment, set()).add(str(finding.path))
        else:
            foreign_paths.setdefault(finding.coords.segment, set()).add(str(finding.path))
            if (finding.overlap_rows or 0) > 0:
                overlap_paths.setdefault(finding.coords.segment, set()).add(str(finding.path))
    rows: list[SegmentCensus] = []
    for seg in _BAR_SEGMENTS:
        files = seg_files.get(seg, 0)
        dups = dup_paths.get(seg, set())
        foreigns = foreign_paths.get(seg, set())
        prices = price_paths.get(seg, set())
        rows.append(
            SegmentCensus(
                segment=seg,
                files=files,
                clean=files - len(dups | foreigns | prices),
                dup_files=len(dups),
                foreign_files=len(foreigns),
                overlap_files=len(overlap_paths.get(seg, set())),
                drift_dirs=drift_dirs.get(seg, 0),
                price_files=len(prices),
            )
        )
    option_dups = dup_paths.get(OPTIONS_SEGMENT, set())
    rows.append(
        SegmentCensus(
            segment=OPTIONS_SEGMENT,
            files=report.option_files_scanned,
            clean=report.option_files_scanned - len(option_dups),
            dup_files=len(option_dups),
            foreign_files=0,
            overlap_files=0,
            drift_dirs=0,
        )
    )
    if engine_findings:
        offenders = [_engine_offender(f) for f in engine_findings[:_ENGINE_DETAIL_CAP]]
        extra = len(engine_findings) - len(offenders)
        suffix = f" (+{extra} more)" if extra else ""
        detail = f"{_ENGINE_LABEL}: {len(engine_findings)} findings — " + (
            ", ".join(offenders) + suffix
        )
    else:
        detail = f"{_ENGINE_LABEL}: 0 findings over {report.engine_files} files"
    return LakeCensus(
        segments=tuple(rows),
        engine_clean=not engine_findings,
        engine_detail=detail,
        findings_total=len(report.findings),
        files_scanned=report.files_scanned + report.option_files_scanned,
    )


def _int_stat(stats: Mapping[str, object] | None, key: str) -> int:
    """A tolerant int read of one stats key (a foreign JSON shape reads 0)."""
    if stats is None:
        return 0
    value = stats.get(key)
    return value if isinstance(value, int) else 0


def _str_stat(stats: Mapping[str, object] | None, key: str) -> str | None:
    """A tolerant str read of one stats key (a foreign JSON shape reads None)."""
    if stats is None:
        return None
    value = stats.get(key)
    return value if isinstance(value, str) else None


def census_to_dict(
    census: LakeCensus,
    *,
    mode: str,
    stats: Mapping[str, object] | None = None,
    last_sweep: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """The JSON shape shared by ``brutex lake audit-unique --json`` + the ``/universe`` route.

    *stats* is the ``.lake_heal/stats.json`` mapping
    (``healed_partitions_total`` / ``quarantined_files_total`` / ``last_heal_utc``);
    *last_sweep* is the ``.lake_heal/last_sweep.json`` sentinel (``completed_utc``).
    Both are tolerant reads — an absent / foreign-shaped value degrades to
    ``0`` / ``None``, never a crash.
    """
    return {
        "present": True,
        "mode": mode,
        "engine_clean": census.engine_clean,
        "engine_detail": census.engine_detail,
        "findings_total": census.findings_total,
        "files_scanned": census.files_scanned,
        "segments": [
            {
                "segment": row.segment,
                "files": row.files,
                "clean": row.clean,
                "dup_files": row.dup_files,
                "foreign_files": row.foreign_files,
                "overlap_files": row.overlap_files,
                "drift_dirs": row.drift_dirs,
                "price_files": row.price_files,
            }
            for row in census.segments
        ],
        "healed_total": _int_stat(stats, "healed_partitions_total"),
        "quarantined_files_total": _int_stat(stats, "quarantined_files_total"),
        "last_heal_utc": _str_stat(stats, "last_heal_utc"),
        "last_sweep_utc": _str_stat(last_sweep, "completed_utc"),
    }


__all__ = [
    "BAR_SCAN_COLUMNS",
    "ENGINE_INDEX_COORDS",
    "OPTIONS_DIR_NAME",
    "OPTIONS_SEGMENT",
    "UNIQUE_KEY_BARS",
    "UNIQUE_KEY_OPTIONS",
    "ForeignMonthRows",
    "IntraFileDup",
    "LakeCensus",
    "LakeScanReport",
    "LegacyKeyDrift",
    "OptionIntraFileDup",
    "PartitionCoords",
    "PriceSanityRows",
    "SegmentCensus",
    "build_lake_census",
    "census_to_dict",
    "classify_bar_keys",
    "detect_legacy_drift",
    "log_lake_finding",
    "scan_lake_invariants",
]
