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
DEC-LAKE-HEAL-OPTIONS-FOREIGN-001, closing the #1175 named follow-up); plus,
per ``bars/`` month file, (f) **price-sanity** violations — non-positive /
null prices or broken OHLC ordering (DEC-STORED-DATA-VALIDATION-001, riding
the SAME per-file read via :data:`BAR_SCAN_COLUMNS`; the ``options/`` tree
stays key-census only — dup + foreign/overlap, no price screen). A price
finding is OBSERVE-ONLY: the healer explicitly skips it (healing is strictly
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
token. A file with foreign rows costs one extra targeted read PER AFFECTED
canonical month sibling (memoised per series — a multi-month stray pays one
read per home month, the round-3 honesty wording) to split ``overlap_rows``.
A bar file MISSING a scan
column — or carrying a non-temporal ``timestamp`` / non-numeric OHLC dtype —
violates the canonical 7-column schema and is itself a defect: it skips the
dup/foreign/price classification AND emits a real :class:`SchemaAlienFile`
finding (DEC-ALIEN-SCHEMA-READ-HEAL-001 — previously a bare
``scan_skip`` WARN with NO finding, so the healer never quarantined the
schema-poisoned file the dashboard read side was skipping per poll; the
missing-column classification pays one anomaly-only
``pl.read_parquet_schema`` footer probe, never a happy-path cost). The
healer quarantine-MOVES the whole alien file (strictly file-quarantining —
never value-editing; the bytes stay recoverable under ``.quarantine/``).

This module must NOT import ``catalog_reconcile`` (which imports THIS module
for its free L1 piggyback) or ``lake_heal`` (which imports this module) —
the dependency edges stay acyclic.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import polars as pl
import structlog

from brutex.data.errors import (
    datetime_out_of_range_errors,
)
from brutex.data.lake_keys import spot_key_candidates
from brutex.data.partition import (
    BAR_DIR_NAME,
    SPOT_PROBE_SEGMENTS,
    partition_for_timestamp,
)
from brutex.data.price_sanity import PRICE_SANITY_COLUMNS

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


@dataclass(frozen=True, slots=True)
class SchemaAlienFile:
    """A readable month file whose SCHEMA is alien to its tree's canonical shape.

    DEC-ALIEN-SCHEMA-READ-HEAL-001 — the scanner previously WARN-skipped a
    bar file missing a scan column with NO finding, so the healer never
    quarantined the schema-poisoned file while the dashboard read side
    (``db_view._readable_parquet_subset``) skipped the SAME file on every
    poll (pre-fix: 500'd on it). Emitted for (a) a ``bars/`` file missing
    one of :data:`BAR_SCAN_COLUMNS`, (b) a ``bars/`` file whose ``timestamp``
    dtype is not Datetime or whose OHLC dtype is non-numeric (both
    deterministic content properties — never a transient I/O error, which
    stays a plain ``scan_skip``), and (c) an ``options/`` file missing one of
    :data:`UNIQUE_KEY_OPTIONS`. Exactly one of ``coords`` (bars tree) /
    ``series`` (options tree) is set. HEALABLE: the healer quarantine-MOVES
    the whole file (strictly file-quarantining, never value-editing — the
    bytes stay recoverable under ``.quarantine/``; a machine "repair" of an
    alien schema would fabricate data, Golden Rule #1).
    """

    path: Path
    reason: str
    coords: PartitionCoords | None = None
    series: str | None = None


#: Union of every finding kind one scan can emit. Importable by name
#: (``from brutex.data.lake_invariants import LakeFinding``) but intentionally
#: NOT listed in ``__all__``: it is a PEP 695 ``type`` statement, which some
#: static analysers (CodeQL) do not yet model as an ``__all__`` binding, and
#: type aliases are idiomatically not part of the ``import *`` surface anyway.
type LakeFinding = (
    IntraFileDup
    | ForeignMonthRows
    | LegacyKeyDrift
    | OptionIntraFileDup
    | OptionForeignMonthRows
    | PriceSanityRows
    | SchemaAlienFile
)


@dataclass(frozen=True, slots=True)
class LakeScanReport:
    """Outcome of one :func:`scan_lake_invariants` pass.

    ``files_by_segment`` (per-``bars/``-segment scanned counts),
    ``engine_files`` (scanned files on :data:`ENGINE_INDEX_COORDS` series) and
    ``engine_files_skipped`` (files on those series that were UNREADABLE /
    schema-drifted and thus skipped — the census-honesty signal of
    DEC-AUDIT-CENSUS-HONESTY-001; a skipped engine file cannot be classified,
    so a "0 findings" verdict over it would be silently green over corruption)
    are defaulted trailing fields feeding :func:`build_lake_census`.
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
    engine_files_skipped: int = 0
    #: The FIRST unreadable/skipped file encountered in scan order (bars first,
    #: then options), or ``None`` when nothing was skipped. Named on the human
    #: ``brutex lake audit-unique`` disambiguation line so a cron/operator that
    #: never sees the ``lake.invariant.scan_skip`` stderr log still knows WHICH
    #: file left the census incomplete (DEC-AUDIT-CENSUS-HONESTY-001).
    first_skipped_path: str | None = None


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
    as-if-UTC instead of raising. Shared by the bars/options classifiers
    (:func:`_count_foreign_rows`, via :func:`classify_bar_keys` /
    :func:`classify_option_keys`); the ``brutex_lake`` crate's scan core
    mirrors this exact law (the W-6a Rust-only scan) and the frozen #1916
    corpus pins the parity.
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
    (``partition, series, foreign_rows, overlap_rows``),
    ``lake.invariant.price_sanity_rows`` (``partition, bad_rows`` —
    DEC-STORED-DATA-VALIDATION-001), ``lake.invariant.schema_alien_file``
    (``partition, reason`` — DEC-ALIEN-SCHEMA-READ-HEAL-001). On the option
    dup + foreign events the ``partition`` token embeds the RAW filesystem
    series dir name, so it rides via ``repr()`` (the #907 convention; joined
    in the round-7 NIT fold — a control-char series name must not inject into
    the rendered log line); the foreign event's separate ``series`` field is
    likewise ``repr()``-wrapped (the dup event carries NO ``series`` field).
    The bars ``partition`` fields stay raw: they are catalogued coordinates
    (``PartitionCoords.relpath()``), not vendor-derived dir names.
    """
    if isinstance(finding, SchemaAlienFile):
        # Options-arm partition rides repr() — it embeds the RAW vendor-
        # influenceable series dir name (the round-8/17 log-injection folds);
        # the bars arm stays raw (catalogued coordinates, the established
        # convention).
        _LOG.warning(
            "lake.invariant.schema_alien_file",
            partition=(
                finding.coords.relpath()
                if finding.coords is not None
                else repr(f"{OPTIONS_DIR_NAME}/{finding.series}/{finding.path.name}")
            ),
            reason=finding.reason,
        )
    elif isinstance(finding, PriceSanityRows):
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
    elif isinstance(finding, OptionForeignMonthRows):
        _LOG.warning(
            "lake.invariant.option_foreign_month_rows",
            partition=repr(f"{OPTIONS_DIR_NAME}/{finding.series}/{finding.path.name}"),
            series=repr(finding.series),
            foreign_rows=finding.foreign_rows,
            overlap_rows=finding.overlap_rows,
        )
    else:
        _LOG.warning(
            "lake.invariant.option_intra_file_dup",
            partition=repr(f"{OPTIONS_DIR_NAME}/{finding.series}/{finding.path.name}"),
            physical_rows=finding.physical_rows,
            unique_keys=finding.unique_keys,
        )


# ── options-tree classification (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001) ──────────


def option_key_dtype_error(frame: pl.DataFrame) -> str | None:
    """Why *frame*'s option key columns are UNCLASSIFIABLE, or ``None`` when sound.

    The dtype gate run BEFORE any ``(side, timestamp)`` classification
    (hostile-review round-12 fold, DEC-LAKE-HEAL-OPTIONS-FOREIGN-001): a
    hand-planted file whose ``side`` column carries a NESTED dtype
    (``List`` / ``Struct``) reads back as UNHASHABLE Python values, so every
    key-set build — the crate scan's foreign-key builder (the W-6a Rust-only
    scan mirrors this gate), the healer's removal-key ``frozenset`` —
    raises ``TypeError``, which is neither a ``parquet_read_skip_errors``
    member nor in the healer's per-finding isolation set: pre-fix ONE crafted
    file crashed the WHOLE scan (ALL healing inert — the L3 boot sweep, every
    L2 batch, the census surfaces) and, planted in the scan→heal window,
    aborted the WHOLE heal batch. ``side`` must be ``String`` (the writer's
    own rendered dtype) or ``Null`` (an all-null hand-planted column — the
    null side rides verbatim as key payload); ``timestamp`` must be
    ``Datetime`` (naive OR aware — naive routes as-if-UTC via
    :func:`utc_aware`) or ``Null`` (abstains from routing). Anything else:
    the file is skipped LOUDLY at scan (``lake.invariant.scan_skip``, path
    repr()'d — the rounds-8/9 log-injection convention) and refused as an
    isolated ``OptionWriterError`` at heal — BOTH at the healer's consistent
    read (``lake_heal._heal_option_foreign``) AND at the writer's OWN keyed
    re-read (``OptionParquetWriter.heal_partition`` with ``remove_keys`` —
    the round-13 fold: that re-read happens AFTER the merge legs, so gating
    only the consistent read left the second, wider swap window open) —
    never classified, never healed, never a crash. PUBLIC + in ``__all__``
    for the healer's + writer's reuse (§11 COMMON — the
    :func:`is_foreign_timestamp` precedent).

    VALUE-range probe (round-18 MAJOR fold): a sound ``Datetime`` DTYPE can
    still hold an i64 value outside Python's ``datetime`` range, and every
    downstream Python-side conversion then faults (``OverflowError`` from
    ``min``/``max``, the BaseException ``pyo3_runtime.PanicException`` from
    ``to_list`` — empirically verified, polars 1.42.1) — the SAME whole-scan /
    whole-batch DoS the dtype arms exist to stop. The gate therefore also
    probes ``min()``/``max()`` under
    :func:`~brutex.data.errors.datetime_out_of_range_errors` and verdicts
    "timestamp value out of datetime range" — months are contiguous, so a
    convertible min AND max bound every value between them (two vectorized
    O(rows) scans, honestly NOT O(1)).

    TOTAL over its input shapes (round-14 fold): an ABSENT key column is the
    same "unclassifiable" verdict, never a raise — the healer's
    consistent-read site is the ONE consumer fed a FULL
    ``read_partition_frame`` read with no column selection, so a scan→heal
    swap of the flagged source to a valid parquet MISSING ``side`` or
    ``timestamp`` (e.g. a bars-style timestamp-only file) previously raised
    bare ``KeyError`` from ``frame.schema[...]`` — in neither
    ``_PER_FINDING_HEAL_ERRORS`` nor ``LakeWriteHalt`` — aborting the WHOLE
    heal batch (the round-3/5/12/13 isolation-escape class the round-12 gate
    itself introduced for this shape; every other call site reads the key
    columns by name first, so a missing column raises ``PolarsError`` at the
    read and is loudly skipped/isolated there).
    """
    side_dtype = frame.schema.get("side")
    if side_dtype is None:
        return "option side column missing — unclassifiable"
    if not (isinstance(side_dtype, pl.String) or side_dtype == pl.Null):
        return f"option side column dtype {side_dtype} is not String/Null — unclassifiable"
    ts_dtype = frame.schema.get("timestamp")
    if ts_dtype is None:
        return "option timestamp column missing — unclassifiable"
    if not (isinstance(ts_dtype, pl.Datetime) or ts_dtype == pl.Null):
        return f"option timestamp column dtype {ts_dtype} is not Datetime/Null — unclassifiable"
    if isinstance(ts_dtype, pl.Datetime):
        # Round-18 MAJOR fold: the dtype alone is NOT enough — a hand-planted
        # Datetime column can hold an in-dtype i64 VALUE no Python ``datetime``
        # represents (e.g. ``2**62`` µs ≈ year 146,000), and every downstream
        # conversion then faults: ``min()``/``max()`` raise ``OverflowError``
        # ("date value out of range") and ``to_list()`` PANICS as the
        # BaseException ``pyo3_runtime.PanicException`` — neither is a
        # ``parquet_read_skip_errors`` member nor in the healer's per-finding
        # isolation set, so pre-fix ONE such file crashed the WHOLE scan
        # (L3 boot sweep permanently inert — the sentinel never stamps — plus
        # a 500 on every ``/universe`` lake-uniqueness cache miss and a dead
        # L2 options census) and, swapped in scan→heal, escaped the batch
        # isolation. Months are contiguous, so if min AND max convert every
        # value between them does — two vectorized O(rows) scans bound the
        # whole column (the ``_count_foreign_rows`` shorthand; NOT O(1)).
        column = frame.get_column("timestamp")
        try:
            column.min()
            column.max()
        except datetime_out_of_range_errors():
            return "option timestamp value out of datetime range — unclassifiable"
    return None


def option_unclassifiable_message(path: Path, reason: str) -> str:
    """The ONE refusal message for an unclassifiable option partition.

    §11 COMMON (review round-15 NIT fold): both consumers of a non-``None``
    :func:`option_key_dtype_error` verdict — the healer's consistent-read gate
    (``lake_heal._heal_option_foreign``) and the writer's own keyed-removal
    re-read gate (``OptionParquetWriter.heal_partition``) — raise
    ``OptionWriterError`` with this exact message shape; two inline f-string
    copies could drift apart. Pure formatting — *path* rides raw here (the
    exception MESSAGE convention; the LOG sites repr() their path fields per
    the rounds-8/9 log-injection folds).
    """
    return f"unclassifiable option partition {path}: {reason}"


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

    Wave-D endgame (ADR-0020, DEC-RUST-LAKE-WAVED-001 W-6a): the scan is
    Rust-ONLY — it routes unconditionally through
    :func:`brutex.data.lake_rust_waved.scan_lake_via_rust` (the ``brutex_lake``
    crate is a REQUIRED runtime dependency; an absent/stale wheel raises the
    typed loud
    :class:`~brutex.data.errors.LakeRustWavedUnavailableError`, and a SET
    ``BRUTEX_LAKE_WAVED_RUST`` env — any value — refuses loudly per the
    retired-flag law). The Python scan core was DELETED in W-6a; the frozen
    #1916 poison-corpus expectations are the behavior pin. The
    ``detect_legacy_drift`` fold + all structlog output stay Python (Wave-D
    house laws i-ii). An out-of-envelope shape is a per-file/per-entry LOUD
    counted ``lake.invariant.scan_skip`` — crate-owned isolation, never a
    whole-scan abort.

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

    Space honesty (DEC-LAKE-INVARIANTS-PREINDEX-001): the ``bars/`` tree is
    streamed ONE SERIES AT A TIME (a sorted directory walk — the series key
    IS the directory structure), so resident memory is O(largest single
    series + findings + symbol dirs) — NEVER O(files). The pre-streaming
    scanner materialised a whole-tree pre-index (~1.2 KB/file measured) and
    held it across the entire throttled read phase (~3.1-3.5 GiB resident for
    ≥32.8 h at the operator-measured 2.36M-file lake under the default L3
    throttle). ``findings`` stays deliberately UNCAPPED — it is O(poisoned
    files), not O(files), each finding is small, and the healer requires the
    complete list (a cap would silently un-heal; refusing is worse than
    holding).

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
    from brutex.data.lake_rust_waved import scan_lake_via_rust

    return scan_lake_via_rust(
        lake_root,
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval=interval,
        months=months,
        include_options=include_options,
        include_bars=include_bars,
        canonical_keys=canonical_keys,
        per_file_pause=per_file_pause,
    )


# ── census ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SegmentCensus:
    """Per-segment clean/poisoned file counts (``OPTIONS`` = the options tree).

    ``price_files`` (DEC-STORED-DATA-VALIDATION-001) counts month files with at
    least one price-insane row — a defaulted trailing field so existing
    constructors stay valid; the ``OPTIONS`` row keeps a structural ``0``
    (the options tree is dup-census only — honest scope, mirroring the
    foreign/overlap follow-up note in :func:`build_lake_census`).

    ``alien_files`` (DEC-ALIEN-SCHEMA-READ-HEAL-001, defaulted trailing)
    counts :class:`SchemaAlienFile` findings per segment. An alien file is
    SKIPPED before classification, so it is NOT in ``files`` (the scanned
    count) and does not reduce ``clean`` — it is reported as its own honest
    column (the ``OPTIONS`` row counts its series-level aliens).
    """

    segment: str
    files: int
    clean: int
    dup_files: int
    foreign_files: int
    overlap_files: int
    drift_dirs: int
    price_files: int = 0
    alien_files: int = 0


@dataclass(frozen=True, slots=True)
class LakeCensus:
    """The operator-facing census fold of one :class:`LakeScanReport`.

    ``files_skipped`` (bars + options that were UNREADABLE / schema-drifted and
    thus skipped) and ``engine_files_skipped`` (that subset on the
    :data:`ENGINE_INDEX_COORDS` series) are the DEC-AUDIT-CENSUS-HONESTY-001
    completeness signals: a skipped file is never classified, so a census with
    ``files_skipped > 0`` cannot honestly certify "0 findings", and
    ``engine_files_skipped > 0`` flips ``engine_clean`` (the operator's trust
    verdict) to False regardless of ``findings_total``.
    """

    segments: tuple[SegmentCensus, ...]
    engine_clean: bool
    engine_detail: str
    findings_total: int
    files_scanned: int
    files_skipped: int = 0
    engine_files_skipped: int = 0
    #: The FIRST unreadable/skipped file (mirrors
    #: :attr:`LakeScanReport.first_skipped_path`) — carried through so the CLI
    #: can NAME it on the disambiguation line without a second scan.
    first_skipped_path: str | None = None


def _engine_relevant(finding: LakeFinding) -> bool:
    """True when *finding* touches an :data:`ENGINE_INDEX_COORDS` series.

    BOTH options-tree finding kinds return ``False`` here — the fall-through
    reads ``finding.coords``, which neither carries (the totality pin,
    DEC-LAKE-HEAL-OPTIONS-FOREIGN-001).
    """
    if isinstance(finding, OptionIntraFileDup | OptionForeignMonthRows):
        return False
    if isinstance(finding, SchemaAlienFile):
        if finding.coords is None:
            return False  # options-tree alien — never an engine bars series
        return (
            finding.coords.exchange,
            finding.coords.segment,
            finding.coords.symbol,
        ) in ENGINE_INDEX_COORDS
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
    """A short human token naming one engine-series offender.

    HONEST (round-18 NIT): the two option-finding arms are TOTALITY arms only
    — production-unreachable, because the sole caller feeds
    ``engine_findings`` filtered by :func:`_engine_relevant`, which returns
    ``False`` for BOTH option kinds (the options tree carries no engine
    series). They exist so this function stays total over the
    :data:`LakeFinding` union (a future caller can never fall through to a
    mislabelled token) and are pinned by direct unit calls, not an
    end-to-end census path.
    """
    if isinstance(finding, LegacyKeyDrift):
        return f"drift {finding.drifted_symbol!r}->{finding.canonical_symbol!r}"
    if isinstance(finding, IntraFileDup):
        return f"dup {finding.coords.relpath()}"
    if isinstance(finding, ForeignMonthRows):
        return f"foreign {finding.coords.relpath()}"
    if isinstance(finding, PriceSanityRows):
        return f"price {finding.coords.relpath()}"
    if isinstance(finding, SchemaAlienFile):
        # An engine-relevant alien always carries bars coords (the options
        # arm is filtered out by _engine_relevant), but stay total anyway.
        location = (
            finding.coords.relpath()
            if finding.coords is not None
            else f"{OPTIONS_DIR_NAME}/{finding.series}/{finding.path.name}"
        )
        return f"alien {location}"
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
    :class:`SchemaAlienFile` findings count into the per-segment
    ``alien_files`` column (DEC-ALIEN-SCHEMA-READ-HEAL-001) — an alien file was
    skipped BEFORE classification, so it is outside ``files``/``clean``; the
    options-tree aliens (``coords is None``) are TYPE-keyed into the
    ``OPTIONS`` row like the other option accumulators below.

    The OPTIONS accumulators are TYPE-keyed (``isinstance`` on the option
    finding kinds), never label-keyed (round-8 NIT fold): keying them into the
    shared per-segment dicts under the ``"OPTIONS"`` sentinel let a BARS
    finding whose ``coords.segment`` happened to read ``"OPTIONS"`` (findings
    are freely constructible — :class:`PartitionCoords` is unvalidated even
    though :func:`~brutex.data.partition.parse_partition_path` rejects that
    segment at the scan boundary today) cross-contaminate the OPTIONS row's
    counts and drive ``clean`` negative. Such a rogue-segment bars finding now
    lands in NO segment row (``_BAR_SEGMENTS`` is fixed) while still counting
    in ``findings_total``.
    """
    seg_files = dict(report.files_by_segment)
    dup_paths: dict[str, set[str]] = {}
    foreign_paths: dict[str, set[str]] = {}
    overlap_paths: dict[str, set[str]] = {}
    option_dup_paths: set[str] = set()
    option_foreign_paths: set[str] = set()
    option_overlap_paths: set[str] = set()
    option_alien_paths: set[str] = set()
    price_paths: dict[str, set[str]] = {}
    alien_paths: dict[str, set[str]] = {}
    drift_dirs: dict[str, int] = {}
    engine_findings: list[LakeFinding] = []
    for finding in report.findings:
        if _engine_relevant(finding):
            engine_findings.append(finding)
        if isinstance(finding, SchemaAlienFile):
            # TYPE-keyed like the option accumulators (round-8 NIT posture):
            # an options-tree alien (coords is None) lands in the OPTIONS
            # row's own set, never under a label-keyed "OPTIONS" sentinel.
            if finding.coords is None:
                option_alien_paths.add(str(finding.path))
            else:
                alien_paths.setdefault(finding.coords.segment, set()).add(str(finding.path))
        elif isinstance(finding, OptionForeignMonthRows):
            option_foreign_paths.add(str(finding.path))
            if (finding.overlap_rows or 0) > 0:
                option_overlap_paths.add(str(finding.path))
        elif isinstance(finding, OptionIntraFileDup):
            option_dup_paths.add(str(finding.path))
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
                alien_files=len(alien_paths.get(seg, set())),
            )
        )
    rows.append(
        SegmentCensus(
            segment=OPTIONS_SEGMENT,
            files=report.option_files_scanned,
            clean=report.option_files_scanned - len(option_dup_paths | option_foreign_paths),
            dup_files=len(option_dup_paths),
            foreign_files=len(option_foreign_paths),
            overlap_files=len(option_overlap_paths),
            drift_dirs=0,  # honestly N/A — no legacy-key-drift concept for series dirs
            alien_files=len(option_alien_paths),
        )
    )
    # A skipped ENGINE-index file (unreadable / schema-drifted) is never
    # classified, so it MUST flip the trust verdict — otherwise "0 findings"
    # reads silently green over engine corruption (DEC-AUDIT-CENSUS-HONESTY-001).
    engine_skipped = report.engine_files_skipped
    skip_note = (
        f" — WARNING: {engine_skipped} engine-index file(s) UNREADABLE (skipped), "
        "census INCOMPLETE, cannot certify clean"
        if engine_skipped
        else ""
    )
    if engine_findings:
        offenders = [_engine_offender(f) for f in engine_findings[:_ENGINE_DETAIL_CAP]]
        extra = len(engine_findings) - len(offenders)
        suffix = f" (+{extra} more)" if extra else ""
        detail = (
            f"{_ENGINE_LABEL}: {len(engine_findings)} findings — "
            + ", ".join(offenders)
            + suffix
            + skip_note
        )
    elif engine_skipped:
        detail = (
            f"{_ENGINE_LABEL}: {engine_skipped} engine-index file(s) UNREADABLE "
            f"(skipped) over {report.engine_files} readable file(s) — census "
            "INCOMPLETE, cannot certify clean"
        )
    else:
        detail = f"{_ENGINE_LABEL}: 0 findings over {report.engine_files} files"
    return LakeCensus(
        segments=tuple(rows),
        engine_clean=not engine_findings and engine_skipped == 0,
        engine_detail=detail,
        findings_total=len(report.findings),
        files_scanned=report.files_scanned + report.option_files_scanned,
        files_skipped=report.files_skipped + report.option_files_skipped,
        engine_files_skipped=engine_skipped,
        first_skipped_path=report.first_skipped_path,
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
        "files_skipped": census.files_skipped,
        "engine_files_skipped": census.engine_files_skipped,
        "first_skipped_path": census.first_skipped_path,
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
                "alien_files": row.alien_files,
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
    "OptionForeignMonthRows",
    "OptionIntraFileDup",
    "PartitionCoords",
    "PriceSanityRows",
    "SchemaAlienFile",
    "SegmentCensus",
    "build_lake_census",
    "census_to_dict",
    "classify_bar_keys",
    "classify_option_keys",
    "detect_legacy_drift",
    "is_foreign_timestamp",
    "log_lake_finding",
    "option_key_dtype_error",
    "option_unclassifiable_message",
    "scan_lake_invariants",
    "utc_aware",
]
