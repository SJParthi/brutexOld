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
keys and (e) foreign-month rows (UTC routing, overlap subsumed —
DEC-LAKE-HEAL-OPTIONS-FOREIGN-001, closing the #1175 named follow-up).
Findings are frozen value dataclasses consumed by the healer
(``brutex.data.lake_heal``) and the census surfaces.

**Month invariant (recon-B §1e):** a bar's canonical month file is
``partition_for_timestamp(ts)`` = the **UTC** ``(year, month)`` — the 1day
midnight-IST straddle (a derived daily bar stamped 18:30 UTC of the previous
calendar day) and the session-exempt GIFTNIFTY early-IST bars are CONSISTENT
with UTC routing; comparing IST months would false-flag them, so this scanner
never does.

**Cost honesty (Golden Rule #10):** the scan is O(files-in-scope) TOTAL, per
file O(footer + ONE key-column read) — the column read scales with the file's
ROW COUNT, so this is NOT O(1). L2 bounds it by coordinates × months; L3 pays
the full O(files) once per lake content token. A file with foreign rows costs
one extra targeted read PER AFFECTED canonical month sibling (memoised per
series — a multi-month stray pays one read per home month, the round-3
honesty wording) to split ``overlap_rows``.

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

_LOG = structlog.get_logger("brutex.data.lake_invariants")

#: The uniqueness key of a ``bars/`` month file (one row per timestamp).
UNIQUE_KEY_BARS: Final[tuple[str, ...]] = ("timestamp",)

#: The uniqueness key of an ``options/<series>/`` month file (the option writer's
#: own established merge key, ``option_parquet_writer._merge_partition``).
UNIQUE_KEY_OPTIONS: Final[tuple[str, ...]] = ("side", "timestamp")

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

#: December — the highest valid month in an options-tree ``YYYY-MM`` stem.
_MAX_MONTH: Final[int] = 12


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
class OptionForeignMonthRows:
    """Rows in ONE ``options/<series>/`` file whose UTC month != the file's own ``YYYY-MM``.

    Subsumes cross-file overlap exactly as :class:`ForeignMonthRows` does for
    ``bars/``: ``OptionParquetWriter.write`` routes every bar by
    ``partition_for_timestamp`` (one file per UTC month per series,
    ``option_parquet_writer.py`` write → ``_merge_partition``), so any
    same-``(side, timestamp)`` key in two files is by construction a foreign
    row in one of them. ``overlap_rows`` = the foreign KEYS also present in
    their canonical month sibling (canonical wins on heal); ``None`` = the
    sibling was unreadable (honest abstain).
    DEC-LAKE-HEAL-OPTIONS-FOREIGN-001.
    """

    path: Path
    series: str
    year: int  # the file's own stem month = the home month
    month: int
    foreign_rows: int  # ROW count (dup copies counted individually — mirrors bars)
    overlap_rows: int | None


#: Union of every finding kind one scan can emit. Importable by name
#: (``from brutex.data.lake_invariants import LakeFinding``) but intentionally
#: NOT listed in ``__all__``: it is a PEP 695 ``type`` statement, which some
#: static analysers (CodeQL) do not yet model as an ``__all__`` binding, and
#: type aliases are idiomatically not part of the ``import *`` surface anyway.
type LakeFinding = (
    IntraFileDup | ForeignMonthRows | LegacyKeyDrift | OptionIntraFileDup | OptionForeignMonthRows
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


def utc_aware(ts: datetime) -> datetime:
    """Stamp a (defensive) tz-naive timestamp UTC so month routing never raises.

    Lake bars are stored ``timestamp[us, tz=UTC]``; a hand-planted naive column
    is routed as-if-UTC (deterministic) instead of crashing the whole scan.

    PUBLIC (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001): shared by the scanner AND the
    healer's option-foreign routing (``lake_heal._heal_option_foreign``) —
    :func:`~brutex.data.partition.partition_for_timestamp` raises
    :class:`~brutex.data.errors.PartitionError` on a naive timestamp
    (``partition.py`` rejects naive by documented stance), and
    ``PartitionError`` is NOT in the healer's per-finding isolation set
    (``lake_heal._PER_FINDING_HEAL_ERRORS`` — it is a ``DataLakeError``, not a
    ``WriterError``), so an unguarded routing call on a hand-planted naive
    stored column would crash the WHOLE heal batch, not one finding. A
    duplicated private copy in ``lake_heal`` would violate §11 COMMON.
    """
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts


def is_foreign_timestamp(home: tuple[int, int], ts: object) -> bool:
    """True iff *ts* is a real datetime whose UTC month routes OUTSIDE *home*.

    THE one foreign-routing predicate (§11 COMMON — a hostile-review fold
    killed four inline copies): a non-datetime (null) abstains — it carries no
    month to route (the bars null-abstain law); routing is
    :func:`utc_aware`-normalised so a hand-planted naive column routes
    as-if-UTC instead of raising. Shared by the bars classifiers
    (:func:`_count_foreign_rows` / :func:`_foreign_timestamps`), the options
    key builder (:func:`_option_foreign_keys`) AND the healer's row mask
    (``lake_heal._option_foreign_frame``).
    """
    return isinstance(ts, datetime) and partition_for_timestamp(utc_aware(ts)) != home


def _count_foreign_rows(home: tuple[int, int], timestamps: pl.Series) -> int:
    """Count ROWS whose UTC month differs from the *home* month (clean-file fast path).

    Months are contiguous time intervals, so when the column's min AND max both
    route to *home* every row between them does too — a clean file pays only
    the two VECTORIZED columnar min/max scans (O(rows), NOT O(1) — the win is
    skipping the O(rows) PYTHON loop below, honest shorthand per Golden Rule
    #10). A null timestamp (only plantable by hand)
    abstains: it carries no month to route. Shared by the bars AND options
    classifiers (the ONLY coordinate fields either ever used are the
    ``(year, month)`` pair — DEC-LAKE-HEAL-OPTIONS-FOREIGN-001).
    """
    first = timestamps.min()
    last = timestamps.max()
    if (
        isinstance(first, datetime)
        and isinstance(last, datetime)
        and partition_for_timestamp(utc_aware(first)) == home
        and partition_for_timestamp(utc_aware(last)) == home
    ):
        return 0
    return sum(1 for ts in timestamps.to_list() if is_foreign_timestamp(home, ts))


def _foreign_timestamps(home: tuple[int, int], timestamps: pl.Series) -> set[datetime]:
    """The UNIQUE (UTC-normalised) timestamps routed outside the *home* month.

    Anomaly-only cost: called solely for files :func:`classify_bar_keys` already
    flagged foreign, to drive the targeted canonical-sibling overlap read.
    """
    return {utc_aware(ts) for ts in timestamps.to_list() if is_foreign_timestamp(home, ts)}


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
    foreign_rows = _count_foreign_rows((coords.year, coords.month), timestamps)
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
    physical_rows, unique_keys``), ``lake.invariant.option_foreign_month_rows``
    (``partition, series, foreign_rows, overlap_rows`` — ``series`` is the RAW
    filesystem dir name, so it rides via ``repr()``, the #907 convention).
    """
    if isinstance(finding, IntraFileDup):
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
    elif isinstance(finding, OptionForeignMonthRows):
        _LOG.warning(
            "lake.invariant.option_foreign_month_rows",
            partition=f"{OPTIONS_DIR_NAME}/{finding.series}/{finding.path.name}",
            series=repr(finding.series),
            foreign_rows=finding.foreign_rows,
            overlap_rows=finding.overlap_rows,
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
        utc_aware(ts) for ts in frame.get_column("timestamp").to_list() if isinstance(ts, datetime)
    }


# ── options-tree classification (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001) ──────────


def _option_file_month(path: Path) -> tuple[int, int] | None:
    """The ``(year, month)`` an ``options/<series>/<YYYY>-<MM>.parquet`` name addresses.

    ``None`` for an unparsable stem (non-numeric / missing separator / month
    outside 1..12) — such a file keeps today's dup-only treatment and honestly
    abstains from foreign classification (no home month to route against).

    Hardened (hostile-review folds, DEC-LAKE-HEAL-OPTIONS-FOREIGN-001):
    ``str.isdigit`` accepts Unicode digit-PROPERTY characters ``int()``
    rejects (``"²²".isdigit()`` is True but ``int("²²")`` raises), so the
    ``int()`` calls are guarded — one crafted filename must abstain, never
    crash the whole scan (the bars ``_file_month`` guard twin). Only the
    CANONICAL zero-padded form the writer renders (``f"{year:04d}-{month:02d}"``)
    parses: a non-canonical-but-parsable stem (``2024-6`` / a non-ASCII-digit
    year) would claim the SAME month as its canonical twin file — two files
    addressing one month is exactly the state the one-file-per-month
    invariant forbids, so such a stem honestly abstains to dup-only instead
    of masquerading as the month's home file.
    """
    stem, sep, month_text = path.stem.rpartition("-")
    if not sep or not stem.isdigit() or not month_text.isdigit():
        return None
    try:
        year = int(stem)
        month = int(month_text)
    except ValueError:
        return None  # Unicode digit-property chars pass isdigit() but not int()
    if not 1 <= month <= _MAX_MONTH:
        return None
    if path.stem != f"{year:04d}-{month:02d}":
        return None  # non-canonical stem — the same-month twin-file blind spot
    return year, month


def _option_foreign_keys(
    home: tuple[int, int], frame: pl.DataFrame
) -> set[tuple[str | None, datetime]]:
    """The UNIQUE raw ``(side, timestamp)`` keys routed outside the file's month.

    Anomaly-only cost (called only for already-flagged files). Routing is
    TIMESTAMP-ONLY (the one shared law): a null timestamp abstains (no month
    to route — the bars null-abstain); ``side`` is key payload, carried RAW
    (even a hand-planted null side rides verbatim) so the scanner's foreign
    count and the healer's movable set can never diverge (liveness).
    Timestamps are ``utc_aware``-ROUTED but stored RAW in the key (removal
    matching is exact stored-value membership).
    """
    sides = frame.get_column("side").to_list()
    stamps = frame.get_column("timestamp").to_list()
    return {
        (side, ts) for side, ts in zip(sides, stamps, strict=True) if is_foreign_timestamp(home, ts)
    }


def _canonical_option_keys(
    series_dir: Path, year: int, month: int, per_file_pause: Callable[[], None] | None
) -> set[tuple[str | None, datetime]] | None:
    """The canonical sibling ``<series>/<YYYY>-<MM>.parquet``'s raw (side, ts) key set.

    ``set()`` for a missing sibling (pure misplacement); ``None`` for an
    unreadable one (honest abstain + one ``lake.invariant.scan_skip`` WARNING
    + ``_pause``) — and for a SYMLINKED one (round-5 NIT fold: the main glob
    loop skips a symlinked partition file loudly, but this targeted sibling
    read followed one; a scan-time READ only — the writer's containment rail
    still guards the heal — yet skipped for consistency, same loud
    ``lake.invariant.symlink_skipped`` event, overlap honestly unclassified,
    no ``_pause`` because no read is attempted). Reads via the ONE
    :func:`_read_key_columns` seam (the
    spy-count tests keep working). Simpler than the bars twin
    :func:`_canonical_timestamps`: the sibling lives in the SAME directory —
    no ``build_partition_path``, no ``PartitionError`` arm.
    """
    canonical = series_dir / f"{year:04d}-{month:02d}.parquet"
    if canonical.is_symlink():
        _LOG.warning("lake.invariant.symlink_skipped", path=str(canonical))
        return None
    if not canonical.exists():
        return set()
    try:
        frame = _read_key_columns(canonical, UNIQUE_KEY_OPTIONS)
    except parquet_read_skip_errors() as exc:
        _LOG.warning("lake.invariant.scan_skip", path=str(canonical), error=str(exc))
        _pause(per_file_pause)
        return None
    _pause(per_file_pause)
    sides = frame.get_column("side").to_list()
    stamps = frame.get_column("timestamp").to_list()
    return set(zip(sides, stamps, strict=True))


def _classify_option_overlap(
    series_dir: Path,
    foreign_keys: set[tuple[str | None, datetime]],
    per_file_pause: Callable[[], None] | None,
    canonical_cache: dict[tuple[int, int], set[tuple[str | None, datetime]] | None],
) -> int | None:
    """Split one file's foreign keys into the overlap-with-canonical count.

    Groups keys by ``partition_for_timestamp(utc_aware(ts))``; ONE targeted
    2-key-column read per affected canonical month per SERIES (memoised in
    *canonical_cache*); any ``None`` cache entry leaves the finding honestly
    unclassified (``None``). Mirror of :func:`_classify_overlap` (bars).
    Overlap membership compares RAW keys — canonical keys are read raw too,
    so naive/aware columns compare like-for-like within one series.
    """
    overlap = 0
    unresolved = False
    grouped: dict[tuple[int, int], set[tuple[str | None, datetime]]] = {}
    for side, ts in foreign_keys:
        grouped.setdefault(partition_for_timestamp(utc_aware(ts)), set()).add((side, ts))
    for year, month in sorted(grouped):
        if (year, month) not in canonical_cache:
            canonical_cache[(year, month)] = _canonical_option_keys(
                series_dir, year, month, per_file_pause
            )
        stored = canonical_cache[(year, month)]
        if stored is None:
            unresolved = True
            continue
        overlap += len(grouped[(year, month)] & stored)
    return None if unresolved else overlap


def classify_option_keys(
    path: Path, series: str, home: tuple[int, int] | None, frame: pl.DataFrame
) -> tuple[OptionIntraFileDup | None, OptionForeignMonthRows | None]:
    """PURE classification of one already-read ``(side, timestamp)`` key frame.

    The options twin of :func:`classify_bar_keys` — dup = physical rows above
    the unique ``(side, timestamp)`` count (the option writer's own merge
    key); foreign = rows whose UTC month != *home* via the SHARED
    :func:`_count_foreign_rows` (``home=None`` ⇒ dup-only honest abstain — an
    unparsable filename has no month to route against); ``overlap_rows=None``
    here (the pure core never reads siblings). An empty frame →
    ``(None, None)``. Findings carry the CONCRETE *path* (the scanner passes
    it — no relpath substitution needed for the options tree).

    Parameters
    ----------
    path
        The concrete ``options/<series>/<YYYY>-<MM>.parquet`` file.
    series
        The series directory name.
    home
        The file's own ``(year, month)``, or ``None`` for an unparsable stem.
    frame
        The file's ``(side, timestamp)`` key frame (already read).

    Returns
    -------
    tuple[OptionIntraFileDup | None, OptionForeignMonthRows | None]
        The optional dup + foreign findings; ``(None, None)`` for a clean or
        empty frame.
    """
    physical = int(frame.height)
    if physical == 0:
        return None, None
    dup: OptionIntraFileDup | None = None
    unique = int(frame.unique(subset=list(UNIQUE_KEY_OPTIONS)).height)
    if physical > unique:
        dup = OptionIntraFileDup(
            path=path,
            series=series,
            physical_rows=physical,
            unique_keys=unique,
        )
    foreign: OptionForeignMonthRows | None = None
    if home is not None:
        foreign_rows = _count_foreign_rows(home, frame.get_column("timestamp"))
        if foreign_rows:
            foreign = OptionForeignMonthRows(
                path=path,
                series=series,
                year=home[0],
                month=home[1],
                foreign_rows=foreign_rows,
                overlap_rows=None,
            )
    return dup, foreign


def scan_lake_invariants(
    lake_root: Path,
    *,
    exchange: str | None = None,
    segment: str | None = None,
    symbol: str | None = None,
    interval: str | None = None,
    months: Collection[tuple[int, int]] | None = None,
    include_options: bool = True,
    include_bars: bool = True,
    canonical_keys: Collection[str] | None = None,
    per_file_pause: Callable[[], None] | None = None,
) -> LakeScanReport:
    """The ONE bounded scanner over ``bars/`` (+ optionally ``options/``).

    Cost honesty (Golden Rule #10): O(files-in-scope) TOTAL, per file
    O(footer + ONE key-column read) — the column read scales with the file's
    ROW COUNT, so this is NOT O(1); L2 bounds it by coordinates × *months*, L3
    pays the full O(files) once per lake content token. At most ONE read call
    per file in the clean case (spy-pinned); a file with foreign rows costs one
    extra targeted read per AFFECTED canonical month sibling (memoised per
    series — a multi-month stray pays one read per home month) to split
    ``overlap_rows``.
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
        ``files_skipped``). Applies to the ``bars/`` tree AND — by filename,
        an O(1) parse with no read — the ``options/`` tree; an options file
        whose name does not parse to a month is skipped under a months scope
        (it cannot be proven in scope; the unbounded L3 sweep still scans it).
        The scope bound runs BEFORE the per-file symlink probe (round-5 NIT
        fold), so an out-of-scope symlinked option file is silently
        scope-skipped — neither warned nor counted; the unbounded L3 sweep
        still warns on it.
    include_options
        Scan the ``options/`` tree for ``(side, timestamp)`` dups +
        foreign-month rows (overlap subsumed —
        DEC-LAKE-HEAL-OPTIONS-FOREIGN-001).
    include_bars
        Scan the ``bars/`` tree (+ the legacy-key drift fold). ``False``
        exists for the bounded L2 options-only census
        (``lake_heal.heal_after_pull(include_options=True)``) — the bars
        counters (``files_scanned`` / ``files_skipped`` / ``series_scanned``
        / ``files_by_segment`` / ``engine_files``) all stay 0.
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
    series_scanned = 0
    files_by_segment: dict[str, int] = {}
    if include_bars:
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

        for series_key in sorted(per_series):
            entries = sorted(per_series[series_key], key=lambda cp: (cp[0].year, cp[0].month))
            scanned_in_series = 0
            pending_overlap: list[tuple[int, PartitionCoords, set[datetime]]] = []
            canonical_cache: dict[tuple[int, int], set[datetime] | None] = {}
            for coords, path in entries:
                try:
                    frame = _read_key_columns(path, UNIQUE_KEY_BARS)
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
                        (
                            len(findings),
                            coords,
                            _foreign_timestamps((coords.year, coords.month), timestamps),
                        )
                    )
                    findings.append(foreign)
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
        if options_root.is_symlink():
            # Round-2 MAJOR fold (Vector 1): ``is_dir()`` FOLLOWS a symlinked
            # ``options/`` root, and the series dirs inside its target are
            # REAL dirs, so the per-series probe below never fires — the
            # whole outside tree would be scanned and its findings handed to
            # the healer. Skipped loudly, like a series-dir symlink.
            _LOG.warning("lake.invariant.symlink_skipped", path=str(options_root))
        elif options_root.is_dir():
            # Any dir containing *.parquet is scanned — no regex gate, an
            # unrecognized series dir is still checked (Golden Rule #1). A
            # SYMLINKED series dir is skipped LOUDLY (hostile-review fold):
            # the healer's primitives unlink/rmdir/create INSIDE a series
            # dir; a SYMLINKED partition FILE is skipped loudly too (round-4
            # fold — see the per-file probe below). HONEST (round-2): these
            # scan-time probes are best-effort only — defeated by a symlinked
            # options ROOT (guarded above) and by a path swapped to a symlink
            # AFTER the scan (TOCTOU). The ENFORCEMENT lives at the writer's
            # heal-side boundary: ``OptionParquetWriter._assert_options_containment``
            # refuses any heal-side read/write/unlink whose RESOLVED path
            # escapes ``<lake_root>/options/``, at heal time.
            for series_dir in sorted(p for p in options_root.iterdir() if p.is_dir()):
                if series_dir.is_symlink():
                    _LOG.warning("lake.invariant.symlink_skipped", path=str(series_dir))
                    continue
                option_canonical_cache: dict[
                    tuple[int, int], set[tuple[str | None, datetime]] | None
                ] = {}
                pending_option_overlap: list[tuple[int, set[tuple[str | None, datetime]]]] = []
                for path in sorted(series_dir.glob("*.parquet")):
                    home = _option_file_month(path)
                    if month_scope is not None and (home is None or home not in month_scope):
                        # The L2 scope bound — never read (bars precedent).
                        # Checked BEFORE the symlink probe (round-5 NIT fold):
                        # the bound is a pure filename parse, so an
                        # out-of-scope symlinked file is silently
                        # scope-skipped like any other out-of-scope file —
                        # neither warned nor counted (the unbounded L3 sweep
                        # still warns on it).
                        continue
                    if path.is_symlink():
                        # Round-4 MINOR fold (refuter-confirmed): the root +
                        # series-dir probes above never check the PARTITION
                        # FILE itself — a symlinked <YYYY-MM>.parquet was
                        # scanned, classified OptionForeignMonthRows, and
                        # handed to the healer, whose consistent read followed
                        # the link OUTSIDE the lake; the copy-forward leg then
                        # IMPORTED the outside rows into an in-lake canonical
                        # file (dest.parent is a REAL series dir, so the
                        # write-leg containment passed) while the source
                        # removal was refused — a permanently non-converging
                        # finding. Never scanned ⇒ never healed; skipped
                        # loudly like the dir probes (the writer's
                        # read/mutation containment is the TOCTOU-safe rail).
                        _LOG.warning("lake.invariant.symlink_skipped", path=str(path))
                        option_files_skipped += 1
                        continue
                    try:
                        frame = _read_key_columns(path, UNIQUE_KEY_OPTIONS)
                    except parquet_read_skip_errors() as exc:
                        _LOG.warning("lake.invariant.scan_skip", path=str(path), error=str(exc))
                        option_files_skipped += 1
                        _pause(per_file_pause)
                        continue
                    _pause(per_file_pause)
                    option_files_scanned += 1
                    option_dup, option_foreign = classify_option_keys(
                        path, series_dir.name, home, frame
                    )
                    if option_dup is not None:
                        log_lake_finding(option_dup)
                        findings.append(option_dup)
                    if option_foreign is not None:
                        keys = _option_foreign_keys(
                            (option_foreign.year, option_foreign.month), frame
                        )
                        pending_option_overlap.append((len(findings), keys))
                        findings.append(option_foreign)
                for idx, keys in pending_option_overlap:
                    found = cast(OptionForeignMonthRows, findings[idx])
                    overlap = _classify_option_overlap(
                        series_dir, keys, per_file_pause, option_canonical_cache
                    )
                    updated_option = replace(found, overlap_rows=overlap)
                    log_lake_finding(updated_option)
                    findings[idx] = updated_option

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
    """Per-segment clean/poisoned file counts (``OPTIONS`` = the options tree)."""

    segment: str
    files: int
    clean: int
    dup_files: int
    foreign_files: int
    overlap_files: int
    drift_dirs: int


@dataclass(frozen=True, slots=True)
class LakeCensus:
    """The operator-facing census fold of one :class:`LakeScanReport`."""

    segments: tuple[SegmentCensus, ...]
    engine_clean: bool
    engine_detail: str
    findings_total: int
    files_scanned: int


def _engine_relevant(finding: LakeFinding) -> bool:
    """True when *finding* touches an :data:`ENGINE_INDEX_COORDS` series.

    BOTH options-tree finding kinds return ``False`` here — the fall-through
    reads ``finding.coords``, which neither carries (the totality pin,
    DEC-LAKE-HEAL-OPTIONS-FOREIGN-001).
    """
    if isinstance(finding, OptionIntraFileDup | OptionForeignMonthRows):
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
    if isinstance(finding, OptionForeignMonthRows):
        return f"option-foreign {OPTIONS_DIR_NAME}/{finding.series}/{finding.path.name}"
    return f"option-dup {OPTIONS_DIR_NAME}/{finding.series}/{finding.path.name}"


def build_lake_census(report: LakeScanReport) -> LakeCensus:
    """Fold one scan report into the per-segment + engine-verdict census.

    ``clean`` counts files with ZERO findings; a file carrying both a dup and a
    foreign finding is one poisoned file. Drift is a DIR-level count (the
    drifted dir's own files are also scanned + counted in ``files``). The
    ``OPTIONS`` row now carries REAL dup + foreign + overlap counts
    (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 closed the #1175 named follow-up);
    ``drift_dirs`` stays 0 for it — honestly N/A, no legacy-key-drift concept
    exists for vendor-coordinate-derived series dirs.
    """
    seg_files = dict(report.files_by_segment)
    dup_paths: dict[str, set[str]] = {}
    foreign_paths: dict[str, set[str]] = {}
    overlap_paths: dict[str, set[str]] = {}
    drift_dirs: dict[str, int] = {}
    engine_findings: list[LakeFinding] = []
    for finding in report.findings:
        if _engine_relevant(finding):
            engine_findings.append(finding)
        if isinstance(finding, OptionForeignMonthRows):
            foreign_paths.setdefault(OPTIONS_SEGMENT, set()).add(str(finding.path))
            if (finding.overlap_rows or 0) > 0:
                overlap_paths.setdefault(OPTIONS_SEGMENT, set()).add(str(finding.path))
        elif isinstance(finding, OptionIntraFileDup):
            dup_paths.setdefault(OPTIONS_SEGMENT, set()).add(str(finding.path))
        elif isinstance(finding, LegacyKeyDrift):
            drift_dirs[finding.segment] = drift_dirs.get(finding.segment, 0) + 1
        elif isinstance(finding, IntraFileDup):
            dup_paths.setdefault(finding.coords.segment, set()).add(str(finding.path))
        else:
            foreign_paths.setdefault(finding.coords.segment, set()).add(str(finding.path))
            if (finding.overlap_rows or 0) > 0:
                overlap_paths.setdefault(finding.coords.segment, set()).add(str(finding.path))
    rows: list[SegmentCensus] = []
    for seg in _BAR_SEGMENTS:
        files = seg_files.get(seg, 0)
        dups = dup_paths.get(seg, set())
        foreigns = foreign_paths.get(seg, set())
        rows.append(
            SegmentCensus(
                segment=seg,
                files=files,
                clean=files - len(dups | foreigns),
                dup_files=len(dups),
                foreign_files=len(foreigns),
                overlap_files=len(overlap_paths.get(seg, set())),
                drift_dirs=drift_dirs.get(seg, 0),
            )
        )
    option_dups = dup_paths.get(OPTIONS_SEGMENT, set())
    option_foreigns = foreign_paths.get(OPTIONS_SEGMENT, set())
    rows.append(
        SegmentCensus(
            segment=OPTIONS_SEGMENT,
            files=report.option_files_scanned,
            clean=report.option_files_scanned - len(option_dups | option_foreigns),
            dup_files=len(option_dups),
            foreign_files=len(option_foreigns),
            overlap_files=len(overlap_paths.get(OPTIONS_SEGMENT, set())),
            drift_dirs=0,  # honestly N/A — no legacy-key-drift concept for series dirs
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
            }
            for row in census.segments
        ],
        "healed_total": _int_stat(stats, "healed_partitions_total"),
        "quarantined_files_total": _int_stat(stats, "quarantined_files_total"),
        "last_heal_utc": _str_stat(stats, "last_heal_utc"),
        "last_sweep_utc": _str_stat(last_sweep, "completed_utc"),
    }


__all__ = [
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
    "OptionForeignMonthRows",
    "OptionIntraFileDup",
    "PartitionCoords",
    "SegmentCensus",
    "build_lake_census",
    "census_to_dict",
    "classify_bar_keys",
    "classify_option_keys",
    "detect_legacy_drift",
    "is_foreign_timestamp",
    "log_lake_finding",
    "scan_lake_invariants",
    "utc_aware",
]
