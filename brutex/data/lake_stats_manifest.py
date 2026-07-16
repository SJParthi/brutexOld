"""Per-file lake STATS manifest — write-time partition statistics (DEC-LAKE-STATS-MANIFEST-001).

The PER-FILE stats/paging EXTENSION of the shipped dataset-discovery manifest
(DEC-LAKE-MANIFEST-O1-001, :mod:`brutex.data.lake_manifest`). It claims the
MIGRATION.md #867/#1025 named future work ("true O(page) needs a write-time
lake manifest"): the ``/db`` candle pagers' remaining O(files) Parquet-footer
scans (the per-dataset ``COUNT(*)`` + the whole-glob page ``ORDER BY``) can be
answered from per-file DISTINCT-identity-key row counts + timestamp bounds
maintained AT WRITE TIME — one small JSON shard per dataset, revalidated
against the live directory on EVERY consult.

Like the Bloom prefilter, this is a **results-safe accelerator**: it is NEVER
part of run identity (the blake3 formula), NEVER in partition bytes, NEVER in
``data_snapshot_hash``. ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX).

Storage
-------
One self-contained JSON per dataset, atomically replaced whole::

    <lake_root>/.manifest/stats/<hh>/<sha256(kind|key)>.json     (mode 0o600)

``hh`` is the first two hex chars of the digest (a fanout dir); the sha256
filename mirrors ``.locks/`` so ``&``, spaces, and #911-drift directory names
need no escaping. ``.manifest/`` is a dot-dir sibling of ``bars/`` +
``options/`` holding no ``*.parquet``, so every lake scanner is provably blind
to it. An EMPTY dataset has NO shard (the last row's removal unlinks it).

Shard schema (:data:`STATS_MANIFEST_VERSION` = 1)
-------------------------------------------------
``{"version", "kind", "key", "identity_key", "row_count", "min_ts", "max_ts",
"files": [{"rel", "rows", "min_ts", "max_ts", "mtime_ns", "size",
"raw_hash"}], "updated_utc"}`` where:

* ``key`` is recorded VERBATIM from the on-disk dir (a #911 drift dir like
  ``NSE-M&M`` is its own shard) and ECHOED + compared at load — the
  hash-collision / mis-file guard.
* ``rows`` is the per-file DISTINCT-identity-key count — ``("timestamp",)``
  bars, ``("side", "timestamp")`` options (the test-pinned twins of
  ``lake_invariants.UNIQUE_KEY_BARS`` / ``UNIQUE_KEY_OPTIONS``) — NEVER
  physical rows.
* ``files`` is sorted ascending by ``rel`` (the filename order the lake's
  keep-FIRST QUALIFY dedup depends on) and ``row_count == Σ files[].rows`` is
  checked at load. Every ``rel`` must match its kind's ASCII month grammar
  and the parsed ``(year, month)`` sequence must be STRICTLY increasing in
  file order (⇔ sorted unique rels under the fixed-width zero-padded
  grammar) — the structural guarantee behind the /db page planner's
  "file order ≡ timestamp order" prefix-sum walk (endgame round 3: a
  Unicode-digit rel previously parsed under the Unicode-aware ``\\d`` yet
  sorted after every ASCII rel, so a crafted shard could serve a
  chronologically MISORDERED page while passing all four clauses).
* ``updated_utc`` is observability ONLY — never validated, never identity.

Anti-illusion law (Golden Rule #1)
----------------------------------
:func:`load_valid_dataset_stats` serves the manifest ONLY when ALL of the
following hold, re-checked on every consult (O(D) in the ONE dataset's month
files — metadata syscalls only, independent of total lake depth):

1. parse + version + key echo + well-formed rows + ASCII-month-grammar rels
   in strictly-increasing chronological order (⇔ sorted unique) + sum
   invariant — over a BOUNDED read (at most :data:`MAX_SHARD_BYTES` + 1 bytes; an
   out-of-band multi-GB shard fails closed as ``oversized``, never read
   whole) of a REGULAR file only (the shard is opened ``O_NONBLOCK`` +
   ``S_ISREG``-checked on the open descriptor, so a FIFO/device/socket inode
   planted at the shard path fails closed as ``not_regular`` instead of
   blocking the open forever — review round 6);
2. FILE-SET EQUALITY — one recursive walk of the dataset dir (``*.parquet``
   only, ANY depth — the exact file set the readers' legacy ``**/*.parquet``
   DuckDB glob scans) yields exactly the shard's ``rel`` set;
3. PER-FILE STAT EQUALITY — a fresh ``os.stat`` ``mtime_ns`` + ``size``
   equals the stamped token for EVERY listed file;
4. MONTH-BOUNDS SANITY — each file's stamped ``[min_ts, max_ts]`` lies inside
   the UTC month its ``rel`` path names (the prefix-sum exactness /
   anti-G-RACE-1 guard: a foreign-month-poisoned partition self-disables the
   manifest until lake_heal rewrites it).

ANY failure ⇒ the manifest is treated ABSENT for the request (the caller runs
the byte-identical legacy live scan) + ONE ``lake.stats_manifest.fallback``
structlog event naming the failed clause. The reader NEVER serves a number it
could not revalidate against the live directory THIS request.

**Honest residual (Golden Rule #10):** an out-of-band overwrite preserving
BOTH ``mtime_ns`` and ``size`` exactly is stat-undetectable — the same
documented envelope every stat token in the tree carries (#1149); impossible
via the production ``mkstemp`` + ``os.replace`` path (new inode). ``raw_hash``
is stored for forensics and deliberately NOT re-hashed per request (O(bytes)
would defeat the point).

Write side (best-effort · incremental · lazily accreted)
--------------------------------------------------------
Every writer hook (:func:`record_partition_stats` /
:func:`remove_partition_stats` / :func:`remove_dataset_stats`) is BEST-EFFORT
— it logs, best-effort unlinks the shard, and returns; it NEVER raises into
the pull (the caught set is exactly ``(OSError, OverflowError,
PartitionLockTimeout, ValueError)``; a
:class:`~brutex.data.errors.LakeWriteHalt` raised by
``partition_write_lock``'s ``.locks/`` creation on a genuinely full disk is
deliberately NOT swallowed). Each upsert is INCREMENTAL: ONE file-row upsert +
an in-memory aggregate refold — never a dataset rescan.

LAZY ACCRETION (the anti-storm law): a hook first probes shard EXISTENCE —
:func:`partition_stats_tracked`, one shard-path key digest + ONE failed
``os.stat``, an O(1) probe with zero shard/partition IO — and returns
immediately when no shard exists; the writer hooks run this probe BEFORE any
other per-write work (never an ``os.stat(dest)`` or a frame-bounds fold on an
unviewed dataset). Shards are seeded ONLY by the read side
(:func:`seed_dataset_stats`, the write-behind rebuild after a clean legacy
scan), so the prebuild's ~10⁵–10⁶ derived-TF partition writes cost one such
probe each — no shard-IO/inode storm — and never-viewed datasets stay
~zero-cost.

Locking: shards serialise on the COMMON
:func:`~brutex.data.partition_lock.partition_write_lock` keyed on the SHARD
path, with the dedicated short :data:`STATS_LOCK_TIMEOUT_S` bound (env
:data:`STATS_LOCK_TIMEOUT_ENV`, fail-loud validated). Lock ORDER is the
sanctioned acyclic direction — data-partition lock → shard lock; no path ever
takes a partition lock while holding a shard lock. Readers take NO lock
(``os.replace`` atomicity: a read sees a complete old or new shard).

Kill switch: env :data:`STATS_MANIFEST_ENV` = ``off`` ⇒ every writer hook AND
every reader consult no-ops ⇒ byte-identical legacy SCAN behaviour — modulo
the pagers' sanctioned deterministic ORDER BY tie-order pin, which applies
unconditionally (MIGRATION.md #1614: an options CE/PE same-minute tie renders
in ONE pinned order either way; the pre-manifest tie order was engine-plan
whim, the row SET is identical). Default ON.

Read-side deadline (DEC-DASHBOARD-COLD-IO-DEADLINE-001, guarantee-audit
BUG-2): the read entry's whole revalidation sweep (clause-1 shard open/read +
clause-2 walk + clause-3 D-stat loop) and the /db post-query TOCTOU re-stat
pass run under the :class:`~brutex.data.stat_deadline.StatSweepGate` — a
WEDGED volume (a hang that never raises) times out after
``BRUTEX_LAKE_STAT_DEADLINE_S`` (default 5 s) into the NAMED
``consult_deadline`` / ``restat_deadline`` fallback clauses (never
misattributed as ``stat_token``/``file_set`` data drift), and while the
zombie sweep stays wedged every later consult on that lake fails fast with
ZERO new threads (the breach-armed latch). The WRITE hooks are deliberately
NOT gated — they already run under the 90 s ``GROWW_WRITE_DEADLINE_S`` spine
guard, and re-guarding ``_load_shard_with_clause`` (shared by both sides)
would add a surprise thread inside the pull's partition-locked section.

Leaf discipline (PR #1177 ``py/cyclic-import``): imports are stdlib +
:mod:`brutex.data.atomic_io` + :mod:`brutex.data.partition_lock` +
:mod:`brutex.data.lake_manifest` + :mod:`brutex.data.stat_deadline` (itself
config-layer + structlog only) — no duckdb/polars/dashboard. The
month-grammar rel-path parsing (``<yyyy>/<mm>.parquet`` bars,
``<YYYY>-<MM>.parquet`` options) is implemented locally and TEST-PINNED
against ``partition.py`` / ``option_parquet_writer.py`` (the
``ledger_file_path`` precedent) rather than imported.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import MAXYEAR, UTC, datetime
from itertools import pairwise
from pathlib import Path
from stat import S_ISREG
from typing import Final

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.data.errors import PartitionLockTimeout
from brutex.data.lake_manifest import manifest_dir
from brutex.data.partition_lock import partition_write_lock
from brutex.data.stat_deadline import StatSweepGate, StatSweepTimeout

# ``__name__`` (== "brutex.data.lake_stats_manifest") — no string literal, so
# the logger name carries no unkillable mutant surface (review round 8).
_LOG = structlog.get_logger(__name__)

#: On-disk shard format version. A reader seeing any OTHER version treats the
#: shard as invalid (legacy-scan fallback + read-side reseed) — never a guess.
STATS_MANIFEST_VERSION: Final[int] = 1

#: The stats shards' home under ``.manifest/`` — a sibling of the
#: DEC-LAKE-MANIFEST-O1-001 dataset-discovery files, which stay byte-untouched.
STATS_SUBDIRNAME: Final[str] = "stats"

#: Dataset kind: the spot/F&O candle tree (``bars/<exch>/<seg>/<sym>/<tf>/…``).
KIND_BARS: Final[str] = "bars"

#: Dataset kind: the expired-options tree (``options/<series>/…``).
KIND_OPTIONS: Final[str] = "options"

#: The coordinate fields each kind's ``key`` mapping must carry (as strings).
STATS_KEY_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    KIND_BARS: ("exchange", "segment", "symbol", "interval"),
    KIND_OPTIONS: ("series",),
}

#: Per-kind DISTINCT-identity-key columns the ``rows`` counts are defined over.
#: Test-pinned twins of ``lake_invariants.UNIQUE_KEY_BARS`` /
#: ``UNIQUE_KEY_OPTIONS`` — deliberately NOT imported (``lake_invariants``
#: pulls polars/duckdb; this module is a leaf).
STATS_IDENTITY_KEYS: Final[Mapping[str, tuple[str, ...]]] = {
    KIND_BARS: ("timestamp",),
    KIND_OPTIONS: ("side", "timestamp"),
}

#: Default bounded-acquire deadline for the SHARD flock (seconds). Deliberately
#: SHORT: the shard upsert runs inside the writer's already-held data-partition
#: lock section, and the deadline-ordering law (75 s partition acquire +
#: :data:`STATS_LOCK_TIMEOUT_S` here) must stay strictly inside the 90 s
#: ``GROWW_WRITE_DEADLINE_S`` margin (DEC-WRITE-DEADLINE-LOCK-ORDER-001).
STATS_LOCK_TIMEOUT_S: Final[float] = 5.0

#: Environment override for the shard-lock deadline — FAIL-LOUD validated by
#: :func:`resolve_stats_lock_timeout_seconds` (blank / non-numeric / ``nan`` /
#: ``inf`` / non-positive raise :class:`ValueError`), unlike the silently
#: defaulting partition-lock env: a misconfigured stats knob must be seen, not
#: absorbed. DEADLINE-ORDERING CAUTION: the override is NOT clamped against the
#: 90 s ``GROWW_WRITE_DEADLINE_S`` write deadline (importing the adapter
#: constant here would break this module's leaf discipline) — the pinned
#: ordering law (:data:`STATS_LOCK_TIMEOUT_S` + the 75 s partition acquire
#: strictly inside 90 s) holds for the DEFAULTS only, so a large override
#: (e.g. ``120``) plus a wedged shard flock can push a viewed dataset's write
#: past the deadline into the fail-safe ``WriteDeadlineExceeded`` defer loop
#: (window deferred + re-pulled, never corruption). Keep any override well
#: under ``90 − 75 = 15`` seconds.
STATS_LOCK_TIMEOUT_ENV: Final[str] = "BRUTEX_STATS_LOCK_TIMEOUT_S"

#: Kill-switch environment variable. Set to :data:`STATS_MANIFEST_OFF`
#: (case-insensitive, whitespace-stripped) to disable the whole feature —
#: every writer hook AND every reader consult no-ops (byte-identical legacy
#: behaviour). Any other value (including unset) keeps it ON.
STATS_MANIFEST_ENV: Final[str] = "BRUTEX_LAKE_STATS_MANIFEST"

#: The one value of :data:`STATS_MANIFEST_ENV` that disables the manifest.
STATS_MANIFEST_OFF: Final[str] = "off"

#: Test-pinned twin of ``brutex.data.partition.BAR_DIR_NAME`` (leaf module —
#: deliberately not imported; ``tests/unit/test_lake_stats_manifest.py`` pins
#: the two names equal so they can never drift).
_BARS_TREE_DIRNAME: Final[str] = "bars"

#: Test-pinned twin of ``brutex.data.option_parquet_writer._OPTIONS_SUBDIR``.
_OPTIONS_TREE_DIRNAME: Final[str] = "options"

#: Bars month grammar: ``<yyyy>/<mm>.parquet`` (nested year dir — the
#: ``build_partition_path`` tail). Month range is checked numerically.
#: ``re.ASCII`` is LOAD-BEARING (endgame round 3): the Unicode-aware ``\d``
#: default ALSO matches e.g. Arabic-Indic digits — and ``int()`` happily
#: converts them — so a planted ``٢٠٢٣/١٢.parquet`` parsed as (2023, 12) with
#: VALID month bounds, yet sorts lexicographically AFTER every ASCII rel
#: (its code points sit above ``9``), silently breaking the rel-sort ≡
#: chronological law the /db page planner's prefix-sum walk depends on. The
#: production writers only ever mint ASCII zero-padded rels (test-pinned),
#: so restricting ``\d`` to ``[0-9]`` rejects only out-of-band names — the
#: DEC-CLI-GREEKS-ENRICH-001 ``_is_canonical_digits`` precedent.
_BARS_REL_RE: Final[re.Pattern[str]] = re.compile(r"\A(\d{4})/(\d{2})\.parquet\Z", re.ASCII)

#: Options month grammar: ``<YYYY>-<MM>.parquet`` (flat series dir — the
#: ``OptionParquetWriter`` partition filename). ``re.ASCII``: same law as
#: :data:`_BARS_REL_RE`.
_OPTIONS_REL_RE: Final[re.Pattern[str]] = re.compile(r"\A(\d{4})-(\d{2})\.parquet\Z", re.ASCII)

#: The write-side best-effort catch set: a shard failure logs + unlinks +
#: returns, NEVER raising into the pull. ``json.JSONDecodeError`` is a
#: ``ValueError`` subclass. ``OverflowError`` is defence-in-depth for the
#: arbitrary-shard-bytes class (review round 3): every KNOWN member is already
#: mapped to ``ValueError`` at its source (:func:`_require_ts` wraps the
#: ``astimezone`` overflow; the clause-1 parser fails closed), but a future
#: datetime-arithmetic overflow must degrade best-effort, never raise into the
#: pull. :class:`~brutex.data.errors.LakeWriteHalt` is deliberately ABSENT — a
#: genuinely full disk at ``.locks/`` creation must still halt the pull (the
#: ``partition_write_lock`` contract).
_BEST_EFFORT_ERRORS: Final[tuple[type[BaseException], ...]] = (
    OSError,
    OverflowError,
    PartitionLockTimeout,
    ValueError,
)

_MONTHS_PER_YEAR: Final[int] = 12

#: Cap on the per-rel enumerations a ``file_set`` fallback event carries. The
#: event fires on EVERY consult while a dataset stays drifted, so an uncapped
#: stray list (thousands of hand-copied files — exactly the #911-drift class
#: this clause catches) would inflate every structured log line to ~100 KB+
#: (a log-volume exhaustion vector). Mirrors the repo's capped-enumeration
#: precedent (option_ingest caps at 20 names; the pipeline headline at 10);
#: the TRUE totals always ride the ``extra_count`` / ``missing_count`` ints.
#: PUBLIC (review round 2): the ONE cap every stats-manifest rel enumeration
#: shares — the read-side seed's ``seed_skipped`` event (db_view) reuses it
#: rather than growing a private copy (CLAUDE.md §11 COMMON).
FALLBACK_REL_LOG_CAP: Final[int] = 20

#: Verdict tokens for :func:`bounded_token_sweep` — the /db post-query TOCTOU
#: re-stat pass's three outcomes (DEC-DASHBOARD-COLD-IO-DEADLINE-001).
TOKEN_SWEEP_OK: Final[str] = "ok"  # noqa: S105  # nosec B105 — a sweep verdict, not a secret
TOKEN_SWEEP_MOVED: Final[str] = "moved"  # noqa: S105  # nosec B105 — a sweep verdict, not a secret
TOKEN_SWEEP_TIMEOUT: Final[str] = "timeout"  # noqa: S105  # nosec B105 — a sweep verdict, not a secret

#: The ONE read-side deadline gate for this module's metadata sweeps
#: (DEC-DASHBOARD-COLD-IO-DEADLINE-001) — keyed per ``str(lake_root)`` at
#: every run, so unrelated lakes (unique ``tmp_path`` test lakes) never
#: collide while every dataset on ONE wedged volume shares one latch. A
#: module-level instance is the sanctioned shape for a process-wide volume
#: fact (the ``partition_lock`` process-registry precedent); its
#: ``wedged(key)`` accessor is the introspection/test seam.
_SWEEP_GATE: Final[StatSweepGate] = StatSweepGate(what="lake stats sweep")

#: Upper bound (bytes) on a shard READ. A real shard is ~16 KB (one JSON row
#: per month file — spec §7), so 16 MiB is ~1000× headroom; a shard replaced
#: out-of-band with a multi-GB file would otherwise be read WHOLE on every
#: ``/db`` consult AND inside the writers' partition-locked hooks, where the
#: resulting :class:`MemoryError` is OUTSIDE :data:`_BEST_EFFORT_ERRORS` and
#: would raise RAW into the pull — breaking the hooks' "never raises into the
#: pull" contract for arbitrary shard bytes (review round 4; the same class
#: as the round-3 deep-nesting / huge-int / invalid-UTF-8 inputs). The loader
#: reads AT MOST ``MAX_SHARD_BYTES + 1`` bytes (a BOUNDED read — race-free,
#: strictly stronger than a ``stat`` pre-check a growing file could defeat)
#: and anything larger fails CLOSED: the read side falls back to the legacy
#: scan under the distinct ``oversized`` clause (+ reseed), the write side
#: corrupt-unlinks the shard.
MAX_SHARD_BYTES: Final[int] = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class StatsFileRow:
    """One month-partition file's stamped statistics inside a dataset shard.

    Attributes
    ----------
    rel
        The file's path relative to the dataset dir — ``"2026/07.parquet"``
        (bars) or ``"2026-07.parquet"`` (options).
    rows
        DISTINCT-identity-key row count of the file (``(timestamp,)`` bars /
        ``(side, timestamp)`` options) — NEVER physical rows.
    min_ts, max_ts
        The file's whole-partition timestamp bounds (timezone-aware UTC).
    mtime_ns, size
        The content stat token, captured by the writer immediately after its
        ``os.replace`` while still holding the data-partition lock.
        Equality-compared at read time, never ordered (clock-skew immune).
    raw_hash
        The partition's sha256 hex digest when the writer computes one (the
        bars tree), ``None`` on the options tree — forensics only, never
        re-hashed per request.
    """

    rel: str
    rows: int
    min_ts: datetime
    max_ts: datetime
    mtime_ns: int
    size: int
    raw_hash: str | None


@dataclass(frozen=True, slots=True)
class DatasetStats:
    """A whole dataset's validated stats shard (one JSON file, schema v1).

    Attributes
    ----------
    kind
        :data:`KIND_BARS` or :data:`KIND_OPTIONS`.
    key
        The dataset's coordinate mapping, recorded VERBATIM from the on-disk
        directory names (:data:`STATS_KEY_FIELDS` names the fields per kind).
    identity_key
        The DISTINCT-count columns the ``rows`` figures are defined over —
        self-description for evolution (:data:`STATS_IDENTITY_KEYS`).
    row_count
        ``Σ files[].rows`` — the dataset's total distinct-key count.
    min_ts, max_ts
        Fold of the per-file bounds (timezone-aware UTC).
    files
        The per-file rows, sorted ascending by ``rel``.
    updated_utc
        Observability-only write stamp (ISO-8601) — never validated, never
        identity.
    """

    kind: str
    key: Mapping[str, str]
    identity_key: tuple[str, ...]
    row_count: int
    min_ts: datetime
    max_ts: datetime
    files: tuple[StatsFileRow, ...]
    updated_utc: str


@dataclass(frozen=True, slots=True)
class StatsConsult:
    """One deadline-aware manifest consult's outcome (DEC-DASHBOARD-COLD-IO-DEADLINE-001).

    Returned by :func:`load_valid_dataset_stats_guarded` so the /db read side
    can tell a plain validation failure (``stats is None, timed_out=False`` —
    reseed after the legacy scan, today's exact behaviour) apart from a
    WEDGED-VOLUME timeout (``timed_out=True`` — degrade to legacy AND
    suppress the seed, whose three unbounded stat sweeps would re-hit the
    same wedged volume). The new exception type never crosses the module
    boundary — return shapes only.

    Attributes
    ----------
    stats
        The fully revalidated shard, or ``None`` (validation failed OR the
        sweep timed out OR the kill switch is off).
    timed_out
        ``True`` iff the revalidation sweep blew (or was latched behind) the
        stat deadline — a volume-outage verdict, never a data-drift one.
    """

    stats: DatasetStats | None
    timed_out: bool = False


# ────────────────────────────────────────────────────────────────────────
# Knobs
# ────────────────────────────────────────────────────────────────────────


def stats_manifest_enabled() -> bool:
    """Whether the stats manifest is enabled — the kill switch check. O(1).

    Returns ``False`` iff :data:`STATS_MANIFEST_ENV` is set to
    :data:`STATS_MANIFEST_OFF` (case-insensitive, whitespace-stripped).
    Disabled means every writer hook AND every reader consult no-ops, so the
    pagers run the byte-identical legacy SCAN — modulo the sanctioned
    deterministic ORDER BY tie-order pin, which applies unconditionally
    (MIGRATION.md #1614: an options CE/PE same-minute tie renders in ONE
    pinned order with the switch on OR off; the pre-manifest tie order was
    engine-plan whim, the row SET is identical).
    """
    raw = os.environ.get(STATS_MANIFEST_ENV)
    if raw is None:
        return True
    return raw.strip().lower() != STATS_MANIFEST_OFF


def resolve_stats_lock_timeout_seconds() -> float:
    """Resolve the shard-flock deadline: env override, else :data:`STATS_LOCK_TIMEOUT_S`.

    FAIL-LOUD (the DEC-RAM-BUDGETS-001 env precedent, deliberately STRICTER
    than the silently-defaulting ``resolve_partition_lock_timeout_seconds``):
    a SET-but-invalid :data:`STATS_LOCK_TIMEOUT_ENV` — blank, non-numeric,
    ``nan`` / ``inf``, or non-positive — raises :class:`ValueError` instead of
    quietly falling back, so a misconfigured knob is seen. The write hooks
    catch ``ValueError`` in their best-effort envelope (logged, never raised
    into the pull). O(1).

    DEADLINE ORDERING (documented, deliberately NOT clamped): the shard flock
    is acquired INSIDE the writer's partition-lock section, which itself runs
    under the 90 s ``GROWW_WRITE_DEADLINE_S`` wall-clock guard — the pinned
    ordering law (default :data:`STATS_LOCK_TIMEOUT_S` 5 s + the 75 s
    partition acquire < 90 s) covers the DEFAULTS only. Clamping the override
    against the write deadline would require importing the Groww adapter
    constant, breaking this module's leaf discipline, so an oversized override
    (e.g. 120 s) is accepted and instead documented: combined with a wedged
    shard flock it turns a viewed dataset's write into the FAIL-SAFE
    ``WriteDeadlineExceeded`` defer loop (window deferred + re-pulled — never
    corruption; the same property the pre-existing partition-lock env carries).
    Keep any override well under the ``90 − 75 = 15`` second margin.

    Raises
    ------
    ValueError
        If the env var is set to a blank / non-numeric / non-finite /
        non-positive value.
    """
    raw = os.environ.get(STATS_LOCK_TIMEOUT_ENV)
    if raw is None:
        return STATS_LOCK_TIMEOUT_S
    text = raw.strip()
    if not text:
        raise ValueError(
            f"{STATS_LOCK_TIMEOUT_ENV} is set but blank — "
            "set a positive number of seconds or unset it"
        )
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"{STATS_LOCK_TIMEOUT_ENV}={raw!r} is not a number") from exc
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(
            f"{STATS_LOCK_TIMEOUT_ENV}={raw!r} must be a finite positive number of seconds"
        )
    return value


# ────────────────────────────────────────────────────────────────────────
# Paths + month grammar
# ────────────────────────────────────────────────────────────────────────


def _canonical_key(kind: str, key: Mapping[str, str]) -> dict[str, str]:
    """Validate + project *key* to exactly the fields *kind* requires.

    Raises :class:`ValueError` on an unknown kind or a missing / non-string /
    empty field (a programming error at the call site — the best-effort hooks
    catch it, the readers document it).
    """
    fields = STATS_KEY_FIELDS.get(kind)
    if fields is None:
        raise ValueError(f"unknown stats-manifest kind {kind!r}")
    canonical: dict[str, str] = {}
    for field in fields:
        value = key.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"stats-manifest key field {field!r} must be a non-empty string, got {value!r}"
            )
        canonical[field] = value
    return canonical


def stats_dir(lake_root: Path) -> Path:
    """Return ``<lake_root>/.manifest/stats`` (never created here). O(1)."""
    return manifest_dir(lake_root) / STATS_SUBDIRNAME


def stats_shard_path(lake_root: Path, kind: str, key: Mapping[str, str]) -> Path:
    """Return the dataset's shard path — ``stats/<hh>/<sha256>.json``. O(1).

    The digest is ``sha256`` of the canonical JSON ``[kind, key]`` form
    (injective — no delimiter-stuffing collisions); ``hh`` is its first two
    hex chars (a fanout dir). Because the filename is a digest, drifted
    dataset dir names (``&``, spaces, ``NSE-M&M``) need no escaping — the
    ``.locks/`` precedent. Never created here.

    Raises
    ------
    ValueError
        On an unknown *kind* or a malformed *key* (missing / empty field).
    """
    canonical = _canonical_key(kind, key)
    token = json.dumps([kind, canonical], sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return stats_dir(lake_root) / digest[:2] / f"{digest}.json"


def dataset_dir(lake_root: Path, kind: str, key: Mapping[str, str]) -> Path:
    """Return the dataset's on-disk directory for *kind* + *key*. O(1).

    ``bars`` → ``<lake_root>/bars/<exchange>/<segment>/<symbol>/<interval>``;
    ``options`` → ``<lake_root>/options/<series>``. The dir names are the
    key's VERBATIM values (drift dirs stay their own dataset). Never created
    here.

    Raises
    ------
    ValueError
        On an unknown *kind* or a malformed *key*.
    """
    canonical = _canonical_key(kind, key)
    if kind == KIND_BARS:
        return (
            lake_root
            / _BARS_TREE_DIRNAME
            / canonical["exchange"]
            / canonical["segment"]
            / canonical["symbol"]
            / canonical["interval"]
        )
    return lake_root / _OPTIONS_TREE_DIRNAME / canonical["series"]


def parse_rel_month(kind: str, rel: str) -> tuple[int, int] | None:
    """Parse *rel* under *kind*'s month grammar → ``(year, month)`` or ``None``.

    Bars: ``<yyyy>/<mm>.parquet`` (zero-padded, nested year dir); options:
    ``<YYYY>-<MM>.parquet`` (flat). Digits are ASCII ``[0-9]`` ONLY (the
    ``re.ASCII`` grammar — a Unicode-decimal rel like ``٢٠٢٦/٠٧.parquet``
    is rejected, endgame round 3). Anything else — wrong grammar, month
    outside 1–12, year ``0000`` — returns ``None`` (fail-closed: the caller
    treats the row as invalid). Test-pinned against
    ``partition.build_partition_path`` / the option writer's partition
    filename so the local grammar can never drift from the owning modules.

    Raises
    ------
    ValueError
        On an unknown *kind* (a programming error, not bad data).
    """
    if kind == KIND_BARS:
        match = _BARS_REL_RE.match(rel)
    elif kind == KIND_OPTIONS:
        match = _OPTIONS_REL_RE.match(rel)
    else:
        raise ValueError(f"unknown stats-manifest kind {kind!r}")
    if match is None:
        return None
    year = int(match.group(1))
    month = int(match.group(2))
    if year < 1 or not (1 <= month <= _MONTHS_PER_YEAR):
        return None
    return (year, month)


def rel_month_bounds(kind: str, rel: str) -> tuple[datetime, datetime] | None:
    """The UTC half-open month window ``[start, next_start)`` *rel* names.

    Returns ``None`` when *rel* fails :func:`parse_rel_month` (fail-closed).
    Used by the month-bounds sanity clause: every stamped bar bound must lie
    inside its file's own UTC month — the prefix-sum exactness guard.
    """
    parsed = parse_rel_month(kind, rel)
    if parsed is None:
        return None
    year, month = parsed
    if year == MAXYEAR and month == _MONTHS_PER_YEAR:
        # The half-open window's next-month bound would be year 10000 —
        # unrepresentable by ``datetime`` (``ValueError: year 10000 is out of
        # range``), which previously escaped the read path's clause-4 loop as
        # a raw raise (review round 3). Only a CRAFTED shard rel reaches this
        # (the real writers never stamp year-9999 partitions) — fail closed.
        return None
    start = datetime(year, month, 1, tzinfo=UTC)
    if month == _MONTHS_PER_YEAR:
        nxt = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        nxt = datetime(year, month + 1, 1, tzinfo=UTC)
    return (start, nxt)


def dest_rel(kind: str, dest: Path) -> str:
    """Derive the shard ``rel`` from a partition *dest* path. O(1).

    Bars take the last TWO components (``<yyyy>/<mm>.parquet``), options the
    last ONE (``<YYYY>-<MM>.parquet``).

    Raises
    ------
    ValueError
        If the derived rel does not match *kind*'s month grammar (or *kind*
        is unknown) — the best-effort hooks catch it.
    """
    rel = f"{dest.parent.name}/{dest.name}" if kind == KIND_BARS else dest.name
    if parse_rel_month(kind, rel) is None:
        raise ValueError(f"dest {dest!r} does not match the {kind!r} month grammar")
    return rel


# ────────────────────────────────────────────────────────────────────────
# Stat tokens
# ────────────────────────────────────────────────────────────────────────


def stat_token(stat: os.stat_result) -> str:
    """Render the content token ``"<st_mtime_ns>:<st_size>"`` from *stat*. O(1).

    Equality-compared, never ordered — clock-skew / backwards-mtime immune.
    """
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def file_token_matches(path: Path, row: StatsFileRow) -> bool:
    """Whether *path*'s CURRENT stat equals *row*'s stamped token. O(1).

    A fresh ``os.stat`` compared for ``mtime_ns`` + ``size`` equality; any
    :class:`OSError` (including a vanished file) is ``False`` — fail-closed.
    Used per-file by clause 3 of the read-time validation and by the
    read side's post-query TOCTOU re-stat.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return False
    return stat.st_mtime_ns == row.mtime_ns and stat.st_size == row.size


# ────────────────────────────────────────────────────────────────────────
# Row / shard construction
# ────────────────────────────────────────────────────────────────────────


def _require_int(value: object, *, field: str, minimum: int) -> int:
    """Coerce-check an int field (bool rejected — JSON ``true`` is not a count)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an int, got {value!r}")
    if value < minimum:
        raise ValueError(f"{field} must be >= {minimum}, got {value}")
    return value


def _require_ts(value: object, *, field: str) -> datetime:
    """Coerce-check a timestamp field to a timezone-aware UTC datetime.

    Accepts an aware :class:`~datetime.datetime` or an ISO-8601 string with an
    explicit offset; a naive datetime / naive string / anything else raises
    :class:`ValueError` (an ambiguous bound could mis-place a month check).
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp, got {value!r}") from exc
    else:
        raise ValueError(f"{field} must be a timestamp, got {value!r}")
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware, got naive {value!r}")
    try:
        return parsed.astimezone(UTC)
    except OverflowError as exc:
        # An extreme-but-parseable offset timestamp (e.g. a crafted shard's
        # "0001-01-01T00:00:00+05:30") passes ``fromisoformat`` but overflows
        # datetime's representable range during UTC normalization.
        # ``OverflowError`` is neither a ``ValueError`` nor an ``OSError``, so
        # unmapped it escaped BOTH the clause-1 parser and the hooks'
        # best-effort envelope — raising into the pull and 500ing a ``/db``
        # read (review round 3, the round-1 RecursionError CLASS, not just the
        # name). Map it to the module's fail-closed ``ValueError``.
        raise ValueError(f"{field} timestamp out of datetime range: {value!r}") from exc


def _normalized_row(row: StatsFileRow) -> StatsFileRow:
    """Validate *row*'s values and return a UTC-normalized copy.

    Raises :class:`ValueError` on any malformation: empty rel, ``rows < 1``,
    naive / inverted bounds, negative stat fields, or an empty-string
    ``raw_hash``. The ONE row validator shared by the factory and the shard
    parser (CLAUDE.md §11 COMMON).
    """
    if not isinstance(row.rel, str) or not row.rel:
        raise ValueError(f"rel must be a non-empty string, got {row.rel!r}")
    rows = _require_int(row.rows, field="rows", minimum=1)
    min_ts = _require_ts(row.min_ts, field="min_ts")
    max_ts = _require_ts(row.max_ts, field="max_ts")
    if min_ts > max_ts:
        raise ValueError(f"file {row.rel!r} has min_ts > max_ts")
    mtime_ns = _require_int(row.mtime_ns, field="mtime_ns", minimum=0)
    size = _require_int(row.size, field="size", minimum=0)
    if row.raw_hash is not None and (not isinstance(row.raw_hash, str) or not row.raw_hash):
        raise ValueError(f"raw_hash must be a non-empty string or None, got {row.raw_hash!r}")
    return StatsFileRow(
        rel=row.rel,
        rows=rows,
        min_ts=min_ts,
        max_ts=max_ts,
        mtime_ns=mtime_ns,
        size=size,
        raw_hash=row.raw_hash,
    )


def build_dataset_stats(
    *,
    kind: str,
    key: Mapping[str, str],
    files: Iterable[StatsFileRow],
    updated_utc: str | None = None,
) -> DatasetStats:
    """Build a validated :class:`DatasetStats` from per-file rows — the ONE factory.

    Sorts *files* ascending by ``rel`` (the keep-FIRST filename order),
    normalizes every timestamp to UTC, enforces the month grammar AND the
    month-bounds sanity per row (so a writer can never persist a
    foreign-month-poisoned shard — it fails here, the shard is unlinked, and
    the reader falls back), and computes the ``row_count`` / ``min_ts`` /
    ``max_ts`` aggregates. O(files · log files).

    Parameters
    ----------
    kind, key
        Dataset coordinates (validated via :data:`STATS_KEY_FIELDS`).
    files
        The per-file rows; must be non-empty (an empty dataset has NO shard).
    updated_utc
        Observability stamp; defaults to now (UTC ISO-8601).

    Raises
    ------
    ValueError
        On any malformation: unknown kind, bad key, empty / duplicate-rel
        files, grammar-violating rel, bounds outside the rel's UTC month, or
        a malformed row value.
    """
    canonical = _canonical_key(kind, key)
    rows = sorted((_normalized_row(row) for row in files), key=lambda r: r.rel)
    if not rows:
        raise ValueError("files must be non-empty — an empty dataset has NO shard")
    rels = [row.rel for row in rows]
    if len(set(rels)) != len(rels):
        raise ValueError(f"duplicate rel in files: {rels!r}")
    for row in rows:
        bounds = rel_month_bounds(kind, row.rel)
        if bounds is None:
            raise ValueError(f"rel {row.rel!r} does not match the {kind!r} month grammar")
        start, nxt = bounds
        if row.min_ts < start or row.max_ts >= nxt:
            raise ValueError(f"file {row.rel!r} bounds lie outside its UTC month")
    return DatasetStats(
        kind=kind,
        key=canonical,
        identity_key=STATS_IDENTITY_KEYS[kind],
        row_count=sum(row.rows for row in rows),
        min_ts=min(row.min_ts for row in rows),
        max_ts=max(row.max_ts for row in rows),
        files=tuple(rows),
        updated_utc=(updated_utc if updated_utc is not None else datetime.now(tz=UTC).isoformat()),
    )


def render_shard(stats: DatasetStats) -> str:
    """Serialize *stats* to its canonical shard JSON (sorted keys, compact).

    Deterministic byte-for-byte for identical inputs — the golden schema-freeze
    pin (``tests/golden/test_lake_stats_manifest_golden.py``) compares this
    literal.
    """
    payload = {
        "version": STATS_MANIFEST_VERSION,
        "kind": stats.kind,
        "key": dict(stats.key),
        "identity_key": list(stats.identity_key),
        "row_count": stats.row_count,
        "min_ts": stats.min_ts.isoformat(),
        "max_ts": stats.max_ts.isoformat(),
        "files": [
            {
                "rel": row.rel,
                "rows": row.rows,
                "min_ts": row.min_ts.isoformat(),
                "max_ts": row.max_ts.isoformat(),
                "mtime_ns": row.mtime_ns,
                "size": row.size,
                "raw_hash": row.raw_hash,
            }
            for row in stats.files
        ],
        "updated_utc": stats.updated_utc,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _row_from_json(obj: object) -> StatsFileRow:
    """Parse one shard file-row object → :class:`StatsFileRow`, fail-closed.

    Raises :class:`ValueError` on any shape/type/value malformation (mapped to
    the ``row_malformed`` clause by the shard parser).
    """
    if not isinstance(obj, dict):
        raise ValueError(f"file row must be an object, got {obj!r}")
    rel = obj.get("rel")
    if not isinstance(rel, str) or not rel:
        raise ValueError(f"rel must be a non-empty string, got {rel!r}")
    raw_hash = obj.get("raw_hash")
    if raw_hash is not None and not isinstance(raw_hash, str):
        raise ValueError(f"raw_hash must be a string or null, got {raw_hash!r}")
    return _normalized_row(
        StatsFileRow(
            rel=rel,
            rows=_require_int(obj.get("rows"), field="rows", minimum=1),
            min_ts=_require_ts(obj.get("min_ts"), field="min_ts"),
            max_ts=_require_ts(obj.get("max_ts"), field="max_ts"),
            mtime_ns=_require_int(obj.get("mtime_ns"), field="mtime_ns", minimum=0),
            size=_require_int(obj.get("size"), field="size", minimum=0),
            raw_hash=raw_hash,
        )
    )


# ────────────────────────────────────────────────────────────────────────
# Structural load (validation clause 1)
# ────────────────────────────────────────────────────────────────────────


def _load_shard_with_clause(
    lake_root: Path, *, kind: str, key: Mapping[str, str]
) -> DatasetStats | str:
    """Read + structurally validate the shard → ``DatasetStats`` or the clause name.

    Clause 1 of the read-time validation: a REGULAR-file-only open
    (``O_NONBLOCK`` + ``S_ISREG`` on the open descriptor — a FIFO / device /
    socket inode planted at the shard path fails closed as ``not_regular``
    instead of blocking ``open(2)`` forever, review round 6) + a BOUNDED read
    (at most :data:`MAX_SHARD_BYTES` + 1 bytes — an out-of-band multi-GB
    shard fails closed as ``oversized`` instead of being read whole into
    memory, review round 4) + parse + version + kind/key echo + identity-key
    echo + well-formed rows + ASCII-month-grammar rels in strictly-increasing
    chronological order (``rel_grammar`` / ``files_unsorted`` — the structural
    "file order ≡ timestamp order" guarantee the /db page planner consumes,
    endgame round 3) + sum invariant + aggregate-bounds
    consistency. Takes NO lock (``os.replace`` atomicity — a read sees a
    complete old or new shard). Returns the parsed :class:`DatasetStats` on
    success, or the fail-closed clause NAME (a ``str``) — exactly one of the
    two, STRUCTURALLY in the return type (endgame round 2: the prior
    ``(stats, clause)`` tuple forced a defensive dead ``"unknown"`` literal at
    the ``load_valid_dataset_stats`` call site). Raises :class:`ValueError`
    only for a malformed *kind*/*key* argument (a programming error, not bad
    data).
    """
    canonical = _canonical_key(kind, key)
    shard_path = stats_shard_path(lake_root, kind, key)
    try:
        # O_NONBLOCK: POSIX ``open(2)`` with O_RDONLY on a FIFO blocks until
        # a writer opens it — BEFORE any fail-closed clause below could fire —
        # so a FIFO planted at the shard path would hang every ``/db`` consult
        # on the dataset (a dashboard threadpool-exhaustion vector) AND wedge
        # the writers' partition-locked hooks, which re-read the shard inside
        # the held data-partition lock (review round 6: the blocking-inode
        # member of the rounds-3/4 "shard replaced out-of-band" tamper class).
        # A regular file ignores the flag, so the bounded read is unchanged.
        fd = os.open(shard_path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "shard_read"
    # fd ownership handoff: ``owned`` is True exactly while THIS frame must
    # close the descriptor; ``os.fdopen`` transfers ownership to the
    # BufferedReader, which closes it internally. Every mutant of the flag is
    # KILLABLE (TestMutationHardening's os.close spy: a stuck-True flag
    # double-closes on the served path, a stuck-False flag leaks the fd on
    # the not-regular path) — unlike the round-8 ``fd = -1`` integer sentinel
    # (its ``-2`` mutant was equivalent and needed line pragmas) AND unlike
    # an ``fd: int | None = None`` sentinel, whose annotation-only
    # ``int & None`` mutant is never evaluated at runtime (PEP 526 — a local
    # annotation is unexecuted) and provably survives (endgame round 2; the
    # first re-enumeration caught exactly that survivor).
    owned = True
    try:
        if not S_ISREG(os.fstat(fd).st_mode):
            # ``fstat`` on the OPEN descriptor — no check-to-read race. The
            # write hooks corrupt-unlink this clause (healing the inode); the
            # read side falls back + the seed's ``os.replace`` restores a
            # regular file.
            return "not_regular"
        with os.fdopen(fd, "rb") as handle:
            # The file object now owns (and closes) the descriptor.
            owned = False
            # The bound stays the READ CALLS themselves (race-free — a file
            # growing after any pre-check cannot defeat it): read exactly the
            # cap, then probe ONE byte past it. A non-empty probe = content
            # strictly past the cap = oversized (checked below, outside the
            # ``finally``). Total bytes read ≤ MAX_SHARD_BYTES + 1, exactly
            # as before; the split replaces the round-8 pragma'd
            # ``read(MAX_SHARD_BYTES + 1)`` literal with fully KILLABLE
            # mutation surfaces (endgame round 2 — the exact-at-cap boundary
            # is pinned by test_shard_exactly_at_cap_still_loads).
            raw = handle.read(MAX_SHARD_BYTES)
            over = handle.read(1)
    except OSError:
        # A failure on the OPEN descriptor (fstat / fdopen / read) — the
        # FileNotFoundError→``absent`` mapping above applies only to the
        # ``os.open`` itself (an open fd cannot "vanish").
        return "shard_read"
    finally:
        if owned:
            os.close(fd)
    if len(over) == 1:
        # A shard replaced out-of-band with a huge file: the bytes past the
        # cap (+1 probe) were never read, so neither this read thread nor the
        # writers' partition-locked hooks can MemoryError on it (review
        # round 4 — MemoryError is deliberately NOT in _BEST_EFFORT_ERRORS;
        # the bound removes the vector instead of swallowing genuine OOM).
        return "oversized"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Invalid-UTF-8 shard bytes raise out of the DECODE — a ``ValueError``
        # subclass, NOT an ``OSError``, so it previously slipped past the two
        # read handlers and escaped the read path as a raw raise (review
        # round 3): arbitrary shard bytes, the ``parse`` fail-closed clause.
        return "parse"
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        # ValueError covers ``json.JSONDecodeError`` (its subclass) AND the
        # plain-``ValueError`` escapes a bare JSONDecodeError catch missed —
        # e.g. CPython's >4300-digit int-conversion limit raises a plain
        # ``ValueError`` from ``json.loads`` (review round 3). RecursionError:
        # a shard replaced out-of-band with deeply-nested JSON (e.g. 100k
        # nested arrays) blows the C scanner's recursion limit — a
        # RuntimeError subclass, OUTSIDE every other net here (review
        # round 1). The clause-1 parser exists precisely to fail CLOSED on
        # arbitrary shard bytes; both input classes are no different.
        return "parse"
    if not isinstance(obj, dict):
        return "parse"
    version = obj.get("version")
    if isinstance(version, bool) or version != STATS_MANIFEST_VERSION:
        return "version"
    if obj.get("kind") != kind:
        return "kind"
    if obj.get("key") != canonical:
        return "key_echo"
    if obj.get("identity_key") != list(STATS_IDENTITY_KEYS[kind]):
        return "identity_key"
    files_raw = obj.get("files")
    if not isinstance(files_raw, list):
        return "row_malformed"
    if not files_raw:
        return "files_empty"
    rows: list[StatsFileRow] = []
    for entry in files_raw:
        try:
            rows.append(_row_from_json(entry))
        except ValueError:
            return "row_malformed"
    # Chronological-order law, STRUCTURAL (endgame round 3): every rel must
    # parse under the kind's ASCII month grammar, and the parsed (year, month)
    # sequence must be STRICTLY increasing in stored file order. Under the
    # fixed-width zero-padded grammar this is exactly "rels sorted ascending +
    # unique" (the keep-FIRST QUALIFY filename order the prior lexicographic
    # check asserted), and it is the invariant the /db page planner's
    # prefix-sum walk consumes as "file order ≡ timestamp order" — enforced
    # HERE so no shard that validates can EVER plan a misordered page, instead
    # of being assumed from a string sort a non-grammar rel could subvert.
    months: list[tuple[int, int]] = []
    for row in rows:
        parsed = parse_rel_month(kind, row.rel)
        if parsed is None:
            return "rel_grammar"
        months.append(parsed)
    if any(prev >= cur for prev, cur in pairwise(months)):
        return "files_unsorted"
    # The ``field=`` names in the three guarded checks below feed messages the
    # ``except`` clauses DISCARD (only the clause tag survives) — a provably-
    # equivalent mutant surface, pragma'd PER LINE so the values and minima on
    # the sibling lines stay fully mutable (review round 8).
    try:
        row_count = _require_int(
            obj.get("row_count"),
            field="row_count",  # pragma: no mutate
            minimum=1,
        )
    except ValueError:
        return "sum_invariant"
    if row_count != sum(row.rows for row in rows):
        return "sum_invariant"
    try:
        min_ts = _require_ts(
            obj.get("min_ts"),
            field="min_ts",  # pragma: no mutate
        )
        max_ts = _require_ts(
            obj.get("max_ts"),
            field="max_ts",  # pragma: no mutate
        )
    except ValueError:
        return "aggregate_bounds"
    if min_ts != min(row.min_ts for row in rows) or max_ts != max(row.max_ts for row in rows):
        return "aggregate_bounds"
    updated_utc = obj.get("updated_utc")
    return DatasetStats(
        kind=kind,
        key=canonical,
        identity_key=STATS_IDENTITY_KEYS[kind],
        row_count=row_count,
        min_ts=min_ts,
        max_ts=max_ts,
        files=tuple(rows),
        updated_utc=updated_utc if isinstance(updated_utc, str) else "",
    )


def load_shard_stats(lake_root: Path, *, kind: str, key: Mapping[str, str]) -> DatasetStats | None:
    """Read + structurally validate the dataset's shard (clause 1 ONLY).

    Parse + version + kind/key echo + identity-key echo + well-formed rows +
    month-grammar rels in strictly-increasing chronological order + sum
    invariant. Does NOT check the live directory — use
    :func:`load_valid_dataset_stats` for the full anti-illusion validation.
    Returns ``None`` on any structural failure (or when the kill switch is
    off). Takes no lock.

    Raises
    ------
    ValueError
        On an unknown *kind* or malformed *key* (programming error).
    """
    if not stats_manifest_enabled():
        return None
    loaded = _load_shard_with_clause(lake_root, kind=kind, key=key)
    return None if isinstance(loaded, str) else loaded


# ────────────────────────────────────────────────────────────────────────
# Read-time revalidation (clauses 2–4) — the anti-illusion gate
# ────────────────────────────────────────────────────────────────────────


def scan_dataset_rels(lake_root: Path, kind: str, key: Mapping[str, str]) -> set[str]:
    """One recursive walk of the dataset dir → EVERY ``*.parquet`` rel path.

    ANY depth, deliberately — the readers' legacy ``<dataset>/**/*.parquet``
    DuckDB glob matches zero-or-more directories, so a stray ``stray.parquet``
    at depth 1 (bars) or a nested one at depth 3 IS read by the legacy scan
    (empirically verified against the pinned DuckDB). File-set equality
    against the shard therefore FAILS for any such stray: the manifest is
    honestly disabled for the dataset (fallback to the live scan that DOES
    count the stray) instead of silently diverging from the legacy numbers —
    the anti-illusion law. A prior fixed-depth version of this scan was blind
    to exactly that stray class. A missing dataset dir is the empty set; any
    other :class:`OSError` (including one raised mid-walk) propagates via
    ``os.walk``'s ``onerror`` hook (the validation caller fails closed to the
    legacy scan). O(D).

    Raises
    ------
    ValueError
        On an unknown *kind* or malformed *key*.
    OSError
        On a walk failure other than a missing dataset dir.
    """

    def _raise(error: OSError) -> None:
        raise error

    root = dataset_dir(lake_root, kind, key)
    rels: set[str] = set()
    try:
        for dirpath, _dirnames, filenames in os.walk(root, onerror=_raise):
            rel_dir = os.path.relpath(dirpath, root)
            for name in filenames:
                if not name.endswith(".parquet"):
                    continue
                rels.add(name if rel_dir == "." else f"{rel_dir.replace(os.sep, '/')}/{name}")
    except FileNotFoundError:
        # The ROOT (or a dir mid-walk) vanished: an empty/partial set is the
        # fail-closed direction — a non-empty shard can never match it.
        return set()
    return rels


def _log_fallback(kind: str, key: Mapping[str, str], clause: str, **extra: object) -> None:
    """Emit the ONE ``lake.stats_manifest.fallback`` event naming the clause."""
    _LOG.info("lake.stats_manifest.fallback", kind=kind, key=dict(key), clause=clause, **extra)


def _validate_dataset_stats(
    lake_root: Path, *, kind: str, key: Mapping[str, str], canonical: Mapping[str, str]
) -> DatasetStats | None:
    """The FULL clause-1..4 revalidation sweep — the guarded region's body.

    Extracted VERBATIM from the pre-DEC-DASHBOARD-COLD-IO-DEADLINE-001
    :func:`load_valid_dataset_stats` body (after the kill switch +
    ``_canonical_key``) so :func:`load_valid_dataset_stats_guarded` can run
    the WHOLE sweep — clause-1 shard open/read/parse, clause-2 walk, clause-3
    D-stat loop, clause-4 pure bounds — under ONE
    :class:`~brutex.data.stat_deadline.StatSweepGate` daemon (never per
    file). Pure metadata syscalls + JSON parse — no DuckDB. Runs on the gate
    daemon in production, so its clause fallback logs fire there (structlog
    is thread-safe; the daemon carries the caller's contextvars copy).
    """
    loaded = _load_shard_with_clause(lake_root, kind=kind, key=key)
    if isinstance(loaded, str):
        # The parser returns EXACTLY one of (stats, clause) — structurally,
        # in its return type (endgame round 2: the prior tuple contract
        # forced a defensive dead ``"unknown"`` literal + line pragma here;
        # the live clause is pinned by the clause-name asserts throughout
        # the suite).
        _log_fallback(kind, canonical, loaded)
        return None
    stats = loaded
    root = dataset_dir(lake_root, kind, key)
    try:
        on_disk = scan_dataset_rels(lake_root, kind, key)
    except OSError as exc:
        _log_fallback(kind, canonical, "file_set", error_type=type(exc).__name__, error=str(exc))
        return None
    stamped = {row.rel for row in stats.files}
    if on_disk != stamped:
        # Capped enumerations (log-volume exhaustion guard): this event fires
        # on EVERY consult while the dataset stays drifted, so the rel lists
        # are bounded at FALLBACK_REL_LOG_CAP entries; the honest TRUE totals
        # ride the *_count integers.
        extra_rels = sorted(on_disk - stamped)
        missing_rels = sorted(stamped - on_disk)
        _log_fallback(
            kind,
            canonical,
            "file_set",
            extra=extra_rels[:FALLBACK_REL_LOG_CAP],
            extra_count=len(extra_rels),
            missing=missing_rels[:FALLBACK_REL_LOG_CAP],
            missing_count=len(missing_rels),
        )
        return None
    for row in stats.files:
        if not file_token_matches(root / row.rel, row):
            _log_fallback(kind, canonical, "stat_token", rel=row.rel)
            return None
    for row in stats.files:
        bounds = rel_month_bounds(kind, row.rel)
        if bounds is None:
            _log_fallback(kind, canonical, "month_bounds", rel=row.rel)
            return None
        start, nxt = bounds
        if row.min_ts < start or row.max_ts >= nxt:
            _log_fallback(kind, canonical, "month_bounds", rel=row.rel)
            return None
    return stats


def load_valid_dataset_stats_guarded(
    lake_root: Path, *, kind: str, key: Mapping[str, str]
) -> StatsConsult:
    """Deadline-aware manifest consult — the anti-illusion gate, wedge-bounded.

    The anti-illusion gate (module docstring): clause 1 structural (parse +
    version + key echo + rows well-formed + month-grammar rels in
    strictly-increasing chronological order + sum invariant), clause 2
    FILE-SET EQUALITY (recursive walk, any depth — the legacy-glob file set),
    clause 3 PER-FILE STAT EQUALITY (``mtime_ns`` + ``size`` vs the stamped
    token), clause 4 MONTH-BOUNDS SANITY (stamped bounds inside the rel's UTC
    month). ANY validation failure yields ``StatsConsult(None)`` after ONE
    ``lake.stats_manifest.fallback`` log naming the failed clause — the
    caller runs the byte-identical legacy live scan (+ best-effort reseed).
    Kill switch off yields ``StatsConsult(None)`` silently (no consult
    happened, zero gate calls).

    DEC-DASHBOARD-COLD-IO-DEADLINE-001 (guarantee-audit BUG-2a): the WHOLE
    sweep runs under the module :data:`_SWEEP_GATE` (ONE daemon per consult,
    never per file; keyed ``str(lake_root)``) — a WEDGED volume times out
    after ``BRUTEX_LAKE_STAT_DEADLINE_S`` into ``StatsConsult(None,
    timed_out=True)`` with ONE NAMED ``consult_deadline`` fallback log on the
    CALLER thread (a volume outage is never misattributed as
    ``stat_token``/``file_set`` data drift), and while the zombie stays
    wedged every later consult on that lake fails fast with ZERO new threads.

    Cost: O(D) metadata syscalls + O(D) JSON-row parse, D = the ONE dataset's
    month files — independent of total lake depth; plus one short-lived
    guard-daemon spawn/join per consult (~0.1 ms). NOT a literal O(1).
    Readers take NO lock.

    Raises
    ------
    ValueError
        On an unknown *kind* or malformed *key* (programming error at the
        call site, never a data condition) — raised EAGERLY on the caller,
        before any guarded I/O.
    """
    if not stats_manifest_enabled():
        return StatsConsult(None)
    canonical = _canonical_key(kind, key)
    try:
        stats = _SWEEP_GATE.run(
            str(lake_root),
            lambda: _validate_dataset_stats(lake_root, kind=kind, key=key, canonical=canonical),
            op="stats_consult",
        )
    except StatSweepTimeout:
        _log_fallback(kind, canonical, "consult_deadline")
        return StatsConsult(None, timed_out=True)
    return StatsConsult(stats)


def load_valid_dataset_stats(
    lake_root: Path, *, kind: str, key: Mapping[str, str]
) -> DatasetStats | None:
    """Back-compat thin wrapper over :func:`load_valid_dataset_stats_guarded`.

    NEVER raises a new type (the pre-DEC-DASHBOARD-COLD-IO-DEADLINE-001
    contract preserved for every caller that needs only the stats): a
    wedged-volume timeout reads as a plain ``None`` here — a caller that must
    TELL the timeout apart (the /db seed suppression) uses the guarded
    variant's :class:`StatsConsult` return shape.

    Raises
    ------
    ValueError
        On an unknown *kind* or malformed *key* (programming error).
    """
    return load_valid_dataset_stats_guarded(lake_root, kind=kind, key=key).stats


def bounded_token_sweep(
    lake_root: Path, root: Path, rows: Sequence[StatsFileRow]
) -> tuple[str, str | None]:
    """Deadline-bounded stat-token re-check over *rows* — the TOCTOU re-stat pass.

    ONE :data:`_SWEEP_GATE` daemon for the WHOLE pass (never per file), same
    gate + key as the consult — so a mid-request wedge that latched the
    consult fails this fast with no second daemon. The sweep calls
    :func:`file_token_matches` per row (dynamic ``os.stat`` resolution
    PRESERVED — the bench stat-spy law) in the given order, so the
    first-moved rel matches the pre-guard iteration order byte-for-byte.

    Returns
    -------
    tuple[str, str | None]
        ``(TOKEN_SWEEP_OK, None)`` — every token intact;
        ``(TOKEN_SWEEP_MOVED, rel)`` — the FIRST moved file's rel;
        ``(TOKEN_SWEEP_TIMEOUT, None)`` — the pass blew (or was latched
        behind) the stat deadline. No log here — the caller names its own
        fallback clause (``stat_token_moved`` / ``restat_deadline``).
    """

    def _sweep() -> tuple[str, str | None]:
        for row in rows:
            if not file_token_matches(root / row.rel, row):
                return (TOKEN_SWEEP_MOVED, row.rel)
        return (TOKEN_SWEEP_OK, None)

    try:
        return _SWEEP_GATE.run(str(lake_root), _sweep, op="stats_restat")
    except StatSweepTimeout:
        return (TOKEN_SWEEP_TIMEOUT, None)


# ────────────────────────────────────────────────────────────────────────
# Write-side hooks (best-effort · incremental · lazy accretion)
# ────────────────────────────────────────────────────────────────────────


def _unlink_shard(shard_path: Path) -> None:
    """Remove the shard best-effort — a failure logs, never raises.

    A DIRECTORY planted at the shard path (local filesystem tamper — the same
    threat model as every other lake sidecar) is not ``unlink``-able (EISDIR
    on Linux, EPERM on macOS), so before review round 7 the corrupt-unlink
    heal could never succeed on it: every later write to a viewed dataset
    re-emitted the ``corrupt_unlinked`` + ``unlink_failed`` warn pair forever.
    The planted tree is removed via ``shutil.rmtree`` instead (it does not
    follow symlinks, so the removal stays inside ``.manifest/stats/``); any
    OTHER unlink failure keeps the warn-only contract.
    """
    try:
        shard_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        # ``os.path.isdir`` (not ``Path.is_dir``): it swallows EVERY OSError
        # to False, so the never-raises contract holds even when the stat
        # itself is denied. A symlink is always ``unlink``-able, so reaching
        # here with isdir=True means a REAL directory — rmtree is safe.
        if not os.path.isdir(shard_path):
            _LOG.warning(
                "lake.stats_manifest.unlink_failed",
                shard=str(shard_path),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return
        try:
            shutil.rmtree(shard_path)
        except OSError as tree_exc:
            _LOG.warning(
                "lake.stats_manifest.unlink_failed",
                shard=str(shard_path),
                error_type=type(tree_exc).__name__,
                error=str(tree_exc),
            )
            return
        _LOG.warning(
            "lake.stats_manifest.dir_shard_removed",
            shard=str(shard_path),
            error_type=type(exc).__name__,
        )


def _write_shard(shard_path: Path, stats: DatasetStats) -> None:
    """Atomically replace the shard (0o600) via the COMMON guard-free core.

    ``atomic_write_simple`` — NOT ``atomic_write_via`` — because a shard
    ENOSPC must never escalate to a pull-wide
    :class:`~brutex.data.errors.LakeWriteHalt` (the accelerator is never a
    correctness surface; the ``lake_manifest`` precedent).
    """
    body = render_shard(stats)
    atomic_write_simple(shard_path, lambda tmp: tmp.write_text(body, encoding="utf-8"), chmod=0o600)


def _shard_exists(shard_path: Path) -> bool:
    """The lazy-accretion existence probe — ONE ``os.stat``, fail-closed False."""
    try:
        os.stat(shard_path)
    except OSError:
        return False
    return True


def partition_stats_tracked(lake_root: Path, kind: str, key: Mapping[str, str]) -> bool:
    """Whether a stats shard EXISTS for the dataset — the writers' pre-probe. O(1).

    The lazy-accretion fast-path check the write hooks run FIRST, before ANY
    other per-write work (``os.stat(dest)``, the option twin's frame-bounds
    fold): an unviewed dataset's write costs exactly this probe — one
    shard-path key digest + ONE failed ``os.stat`` — and nothing else (review
    round 2; the honest cost is "one cheap O(1) probe", never literally "one
    failed stat" — the digest is unavoidable, it locates the shard).
    Fail-closed: the kill switch, an unknown *kind*, or a malformed *key*
    (both programming-error classes at a hook site, which derives the key
    from a real on-disk path) return ``False`` silently — the hook abstains;
    :func:`record_partition_stats` still validates its own inputs loudly for
    direct callers.
    """
    if not stats_manifest_enabled():
        return False
    try:
        shard_path = stats_shard_path(lake_root, kind, key)
    except ValueError:
        return False
    return _shard_exists(shard_path)


def record_partition_stats(
    lake_root: Path,
    *,
    kind: str,
    key: Mapping[str, str],
    dest: Path,
    rows: int,
    min_ts: datetime,
    max_ts: datetime,
    raw_hash: str | None,
    stat: os.stat_result,
) -> bool:
    """Upsert ONE file row after a partition write — best-effort, incremental.

    Called by the lake writers INSIDE their already-held data-partition lock,
    immediately after the durable ``os.replace`` (*stat* must be the fresh
    ``os.stat(dest)`` taken while that lock is still held, so the token is the
    durable file's). LAZY ACCRETION: when NO shard exists this is one
    shard-path key digest + ONE failed ``os.stat`` and an immediate ``False``
    — shards are seeded only by the read side (:func:`seed_dataset_stats`),
    which is what caps the prebuild's ~10⁵–10⁶ writes to one cheap probe each
    (the writer hooks additionally pre-probe via
    :func:`partition_stats_tracked` BEFORE their ``os.stat(dest)`` /
    frame-bounds work, so an unviewed dataset's write pays ONLY that probe).

    When the shard exists: take the shard flock
    (:func:`resolve_stats_lock_timeout_seconds`-bounded), re-read + parse
    (corrupt / old-version ⇒ unlink, NEVER merge into garbage), upsert the one
    row keyed by ``rel``, refold the aggregates in memory, and atomically
    rewrite. Any best-effort failure (``OSError`` / lock timeout /
    ``ValueError`` / ``OverflowError``) logs
    ``lake.stats_manifest.record_failed``, unlinks the
    shard (the reader falls back + reseeds), and returns ``False`` — it NEVER
    raises into the pull. A
    :class:`~brutex.data.errors.LakeWriteHalt` from the ``.locks/`` creation
    on a genuinely full disk deliberately PROPAGATES.

    Parameters
    ----------
    lake_root, kind, key
        Dataset coordinates.
    dest
        The partition file just written (its ``rel`` is derived per *kind*).
    rows
        The file's DISTINCT-identity-key count (``WriteResult.row_count`` /
        the option writer's post-``unique`` ``combined.height``) — never
        physical rows.
    min_ts, max_ts
        The write's whole-partition timestamp bounds (timezone-aware).
    raw_hash
        The partition's sha256 hex (bars) or ``None`` (options).
    stat
        The post-replace ``os.stat(dest)`` result (the content token source).

    Returns
    -------
    bool
        ``True`` iff the shard was updated.
    """
    if not stats_manifest_enabled():
        return False
    try:
        shard_path = stats_shard_path(lake_root, kind, key)
    except ValueError as exc:
        _LOG.warning(
            "lake.stats_manifest.record_failed",
            kind=kind,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return False
    if not _shard_exists(shard_path):
        return False
    try:
        rel = dest_rel(kind, dest)
        new_row = StatsFileRow(
            rel=rel,
            rows=rows,
            min_ts=min_ts,
            max_ts=max_ts,
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            raw_hash=raw_hash,
        )
        timeout = resolve_stats_lock_timeout_seconds()
        with partition_write_lock(lake_root, shard_path, timeout_seconds=timeout):
            loaded = _load_shard_with_clause(lake_root, kind=kind, key=key)
            if isinstance(loaded, str):
                if loaded == "absent":
                    # Unlinked between the probe and the lock — stay lazy.
                    return False
                _LOG.warning(
                    "lake.stats_manifest.corrupt_unlinked",
                    kind=kind,
                    clause=loaded,
                    shard=str(shard_path),
                )
                _unlink_shard(shard_path)
                return False
            merged = [row for row in loaded.files if row.rel != rel]
            merged.append(new_row)
            rebuilt = build_dataset_stats(kind=kind, key=key, files=merged)
            _write_shard(shard_path, rebuilt)
    except _BEST_EFFORT_ERRORS as exc:
        _LOG.warning(
            "lake.stats_manifest.record_failed",
            kind=kind,
            key=dict(key),
            dest=str(dest),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        _unlink_shard(shard_path)
        return False
    _LOG.debug("lake.stats_manifest.recorded", kind=kind, rel=rel, rows=rows)
    return True


def remove_partition_stats(
    lake_root: Path, *, kind: str, key: Mapping[str, str], dest: Path
) -> bool:
    """Remove ONE file's row after its quarantine-move — best-effort.

    The heal-side twin of :func:`record_partition_stats`: called beside the
    existing ``invalidate_manifest`` when a heal MOVES a partition file out of
    the dataset. Lazy probe first (absent shard ⇒ no-op ``False``); under the
    shard flock the file's row is deleted and the aggregates re-summed; the
    LAST row's removal unlinks the whole shard (an empty dataset has no
    shard). A rel with no stored row is an idempotent no-op ``True``. Failures
    log ``lake.stats_manifest.remove_failed`` + unlink + return ``False`` —
    never raise into the pull (:class:`~brutex.data.errors.LakeWriteHalt`
    from ``.locks/`` creation still propagates).

    Returns
    -------
    bool
        ``True`` iff the shard reflects the removal (including the no-op and
        unlinked-last-row cases).
    """
    if not stats_manifest_enabled():
        return False
    try:
        shard_path = stats_shard_path(lake_root, kind, key)
    except ValueError as exc:
        _LOG.warning(
            "lake.stats_manifest.remove_failed",
            kind=kind,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return False
    if not _shard_exists(shard_path):
        return False
    try:
        rel = dest_rel(kind, dest)
        timeout = resolve_stats_lock_timeout_seconds()
        with partition_write_lock(lake_root, shard_path, timeout_seconds=timeout):
            loaded = _load_shard_with_clause(lake_root, kind=kind, key=key)
            if isinstance(loaded, str):
                if loaded == "absent":
                    return False
                _LOG.warning(
                    "lake.stats_manifest.corrupt_unlinked",
                    kind=kind,
                    clause=loaded,
                    shard=str(shard_path),
                )
                _unlink_shard(shard_path)
                return False
            remaining = [row for row in loaded.files if row.rel != rel]
            if len(remaining) == len(loaded.files):
                _LOG.debug("lake.stats_manifest.remove_noop", kind=kind, rel=rel)
                return True
            if not remaining:
                _unlink_shard(shard_path)
                _LOG.debug("lake.stats_manifest.removed_last_row", kind=kind, rel=rel)
                return True
            rebuilt = build_dataset_stats(kind=kind, key=key, files=remaining)
            _write_shard(shard_path, rebuilt)
    except _BEST_EFFORT_ERRORS as exc:
        _LOG.warning(
            "lake.stats_manifest.remove_failed",
            kind=kind,
            key=dict(key),
            dest=str(dest),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        _unlink_shard(shard_path)
        return False
    _LOG.debug("lake.stats_manifest.row_removed", kind=kind, rel=rel)
    return True


def remove_dataset_stats(lake_root: Path, *, kind: str, key: Mapping[str, str]) -> None:
    """Unlink the dataset's WHOLE shard — the heal / TF-rebuild chokepoint hook.

    Called beside ``invalidate_manifest`` wherever a whole dataset's files are
    quarantined / deleted-and-rebuilt (``lake_heal._quarantine_move``).
    Prefers the shard flock; if the lock cannot be taken the unlink still
    proceeds (a racing upsert then re-creates a shard whose stat tokens the
    reader revalidates — never a wrong number, only a re-seed). Idempotent,
    O(1), never raises (a
    :class:`~brutex.data.errors.LakeWriteHalt` from ``.locks/`` creation
    still propagates — a genuinely full disk halts the pull).
    """
    if not stats_manifest_enabled():
        return
    try:
        shard_path = stats_shard_path(lake_root, kind, key)
    except ValueError as exc:
        _LOG.warning(
            "lake.stats_manifest.remove_dataset_failed",
            kind=kind,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return
    if not _shard_exists(shard_path):
        return
    try:
        timeout = resolve_stats_lock_timeout_seconds()
        with partition_write_lock(lake_root, shard_path, timeout_seconds=timeout):
            _unlink_shard(shard_path)
    except _BEST_EFFORT_ERRORS as exc:
        _LOG.warning(
            "lake.stats_manifest.remove_dataset_lock_failed",
            kind=kind,
            key=dict(key),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        _unlink_shard(shard_path)
        return
    _LOG.debug("lake.stats_manifest.dataset_removed", kind=kind, key=dict(key))


def seed_dataset_stats(
    lake_root: Path, *, kind: str, key: Mapping[str, str], files: Iterable[StatsFileRow]
) -> bool:
    """Write-behind a WHOLE rebuilt shard from a clean legacy scan — the ONLY seeder.

    The read side calls this after a fallback whose live scan ran CLEAN,
    passing per-file rows whose stat tokens it captured BEFORE its grouped
    query and re-checked AFTER (any movement drops the rebuild — that
    discipline lives with the caller, which owns the DuckDB connection). The
    rows are validated through the ONE factory (:func:`build_dataset_stats`)
    — a month-bounds-violating row REJECTS the whole seed
    (``lake.stats_manifest.seed_rejected``), so known-unservable garbage is
    never persisted. EMPTY *files* unlinks any existing shard and returns
    ``False`` (an empty dataset has no shard).

    The final shard is written under the shard flock, LAST-WRITER-WINS: a
    concurrent writer upsert that completed BEFORE the seed acquired the flock
    is overwritten by the seed's (older) snapshot — honestly a clobber, not a
    graceful loss. That stale state is FAIL-CLOSED, never wrong-numbered: the
    overwritten upsert's file is either absent from the seeded snapshot
    (file-set inequality) or carries a moved stat token, so the NEXT read's
    per-request validation detects it, falls back to the live scan, and
    reseeds — converging in one extra O(files) cycle. A seed FAILURE (lock
    timeout / ``OSError``) deliberately does NOT unlink an existing shard —
    the reader's per-request validation governs whatever survives. Never
    raises (:class:`~brutex.data.errors.LakeWriteHalt` from ``.locks/``
    creation still propagates — the read-side caller in ``db_view`` catches
    it, so a full disk never 500s a ``/db`` read).

    Returns
    -------
    bool
        ``True`` iff the rebuilt shard was written.
    """
    if not stats_manifest_enabled():
        return False
    try:
        shard_path = stats_shard_path(lake_root, kind, key)
    except ValueError as exc:
        _LOG.warning(
            "lake.stats_manifest.seed_failed",
            kind=kind,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return False
    rows = list(files)
    if not rows:
        _LOG.warning("lake.stats_manifest.seed_rejected", kind=kind, reason="empty_files")
        _unlink_shard(shard_path)
        return False
    try:
        stats = build_dataset_stats(kind=kind, key=key, files=rows)
    except ValueError as exc:
        _LOG.warning("lake.stats_manifest.seed_rejected", kind=kind, key=dict(key), reason=str(exc))
        return False
    try:
        timeout = resolve_stats_lock_timeout_seconds()
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        with partition_write_lock(lake_root, shard_path, timeout_seconds=timeout):
            _write_shard(shard_path, stats)
    except _BEST_EFFORT_ERRORS as exc:
        _LOG.warning(
            "lake.stats_manifest.seed_failed",
            kind=kind,
            key=dict(key),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return False
    _LOG.info(
        "lake.stats_manifest.seeded",
        kind=kind,
        key=dict(key),
        files=len(stats.files),
        row_count=stats.row_count,
    )
    return True


__all__ = [
    "FALLBACK_REL_LOG_CAP",
    "KIND_BARS",
    "KIND_OPTIONS",
    "MAX_SHARD_BYTES",
    "STATS_IDENTITY_KEYS",
    "STATS_KEY_FIELDS",
    "STATS_LOCK_TIMEOUT_ENV",
    "STATS_LOCK_TIMEOUT_S",
    "STATS_MANIFEST_ENV",
    "STATS_MANIFEST_OFF",
    "STATS_MANIFEST_VERSION",
    "STATS_SUBDIRNAME",
    "TOKEN_SWEEP_MOVED",
    "TOKEN_SWEEP_OK",
    "TOKEN_SWEEP_TIMEOUT",
    "DatasetStats",
    "StatsConsult",
    "StatsFileRow",
    "bounded_token_sweep",
    "build_dataset_stats",
    "dataset_dir",
    "dest_rel",
    "file_token_matches",
    "load_shard_stats",
    "load_valid_dataset_stats",
    "load_valid_dataset_stats_guarded",
    "parse_rel_month",
    "partition_stats_tracked",
    "record_partition_stats",
    "rel_month_bounds",
    "remove_dataset_stats",
    "remove_partition_stats",
    "render_shard",
    "resolve_stats_lock_timeout_seconds",
    "scan_dataset_rels",
    "seed_dataset_stats",
    "stat_token",
    "stats_dir",
    "stats_manifest_enabled",
    "stats_shard_path",
]
