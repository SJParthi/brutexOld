"""Durable DuckDB/Parquet backing for the :class:`~brutex.data.window_ledger.WindowLedger`.

DEC-WINDOW-LEDGER-001 (PR-2). The unified per-window ledger is only useful ACROSS
runs if it SURVIVES one — so this is its durable home: a real, queryable
**Parquet table** at ``<lake_root>/window_ledger/ledger.parquet`` (the lake
convention the candle viewer already reads via DuckDB ``read_parquet``), NOT a
JSON sidecar. The ledger is the SINGLE source of truth for skip + retry; storing
it as a lake table is what makes "every window in the DB, browsable on /db"
literally true.

Design (priority law — Correctness > Reliability > … > Performance):

* **Correctness / idempotence.** :meth:`WindowLedger.to_rows` is sorted +
  deterministic, and the Parquet is written with a FIXED explicit schema over
  those sorted rows, so a load→save→load is a **row-level fixed point** (CLAUDE.md
  §2 Rule 7). (Honest envelope: Parquet *file bytes* may differ across writers via
  embedded library metadata; the ROW CONTENT is the reproducible contract, and
  every test asserts the row fixed point, not the byte stream.)
* **Reliability — never crash a pull, never SILENT.** :func:`load_window_ledger`
  degrades to an EMPTY ledger (a structured WARNING, never an exception) on a
  missing / unreadable / malformed / wrong-shaped file — a lost ledger costs a
  re-pull (idempotent keep-first sink), never data. When the file EXISTED but could
  not be read, the returned ledger carries ``load_failed=True``
  (DEC-WINDOW-LEDGER-RESCUE-001) so the pull drivers ANNOUNCE the full re-verify in
  the pull report / note instead of quietly re-downloading everything.
* **Reliability — crash-safe, cross-process-safe write + graceful disk-full halt.**
  :func:`save_window_ledger` holds the COMMON per-partition
  :func:`~brutex.data.partition_lock.partition_write_lock` (keyed on the ledger
  path) across a READ-MERGE-WRITE — the on-disk rows are merged with the in-memory
  ones per key (:func:`merge_ledger_rows`) so two concurrent pulls on one lake never
  last-writer-wins clobber each other's rows (DEC-WINDOW-LEDGER-RESCUE-001, the
  ledger twin of DEC-LAKE-PARTITION-FLOCK-001) — then writes a sibling ``*.tmp`` and
  :func:`os.replace` (atomic on POSIX within one filesystem) via the COMMON
  :func:`~brutex.data.atomic_io.atomic_write_via`, which maps a disk-full
  (``ENOSPC``) / out-of-memory to a :class:`~brutex.data.errors.LakeWriteHalt` —
  a pull-wide global stop (a member of both ingest spines'
  ``_GLOBAL_STOP_ERRORS``), so a full disk HALTS the pull gracefully instead of
  being mis-quarantined as one symbol's problem (which would keep hammering a full
  disk). The ledger lives under ``<lake_root>/window_ledger/`` — it IS a lake write
  (DEC-WINDOW-LEDGER-WRITE-HALT-001).
* **Migration (one-shot).** :func:`load_or_migrate_window_ledger` folds the
  legacy ``window_status.json`` sidecar into the ledger the first time, so no
  history is lost when the unified ledger supersedes it. (The ``fetch_coverage.json``
  sidecar fold was retired with the coverage set in DEC-WINDOW-LEDGER-001 Stage e.)

The O(1) per-window probe/record stays in :class:`WindowLedger`; only the
one-shot load (run start) + save (run end) are O(N) over the recorded windows.
The throttled MID-pull flush is O(dirty) since DEC-LEDGER-DELTA-FLUSH-001: it
appends only the dirtied rows to ``window_ledger/deltas/``
(:func:`append_ledger_delta`); :func:`load_window_ledger` folds ``base +
deltas`` newer-file-wins and the pull-end :func:`save_window_ledger` subsumes +
deletes the deltas. Scheduled compaction of crash-orphaned deltas (and an
SQL-side delta fold for the /coverage DuckDB fast paths) are NAMED follow-ups.

**Load-cost honesty (DEC-LEDGER-LOAD-SPACE-V1-001, measured 2026-07-09):** the
full load materialises EVERY recorded row — measured pre-fix at **794 B/row
logical / 1,305 B/row RSS** (~13.6 µs/row) on an 87,573-row real-writer
fixture, ~60-100× the ~13 B/row on disk, because ``to_pylist()`` decodes a
DISTINCT ``str`` per cell. The per-load intern memo
(:data:`_INTERNED_COLUMNS`) cuts the low-cardinality string share (~40% of the
logical per-row cost) and :func:`shared_pull_ledger_scope` collapses the full
pipeline's TWO whole-ledger materialisations to ONE. Both reduce the
PER-ROW/PER-RUN constant only — the base itself still grows without bound
(~4.4M rows/yr at the documented accumulation rate; the only in-memory delete
is the plan-scoped ``prune_superseded``). Bounding the growth CLASS (an
expiry-keyed ARCHIVAL policy for terminal F&O rows) is the NAMED follow-up
F1-FU1 — designed, deliberately NOT claimed here.
"""

from __future__ import annotations

import contextvars
import os
import threading
import time
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.calendars.nse import IST
from brutex.data.atomic_io import atomic_write_via
from brutex.data.errors import PartitionLockTimeout
from brutex.data.fetch_coverage import WindowKey
from brutex.data.guarded_read import open_regular_binary, read_regular_text
from brutex.data.interior_gap_detect import interior_gap_runs
from brutex.data.lake_rust_ledger_render import (
    lake_rust_ledger_render_enabled,
    render_ledger_parquet_bytes,
)
from brutex.data.partition_lock import partition_write_lock
from brutex.data.window_ledger import (
    EMPTY,
    EMPTY_TERMINAL_THRESHOLD,
    FAILED,
    FAILED_TERMINAL_THRESHOLD,
    PLANNED,
    SKIPPED,
    SKIPPED_AUDIT_PER_PULL,
    SUCCESS,
    LedgerRowTuple,
    WindowLedger,
    WindowLedgerRecord,
)
from brutex.observability.progress_beat import emit_progress_beat

#: Structured logger — surfaces a corrupt/absent ledger into ``/logs``.
_LOG = structlog.get_logger("brutex.data.window_ledger_store")

#: The ledger table directory + file under the lake root (browsable on /db).
LEDGER_DIRNAME: str = "window_ledger"
LEDGER_FILENAME: str = "ledger.parquet"

#: The incremental delta directory under :data:`LEDGER_DIRNAME`
#: (DEC-LEDGER-DELTA-FLUSH-001): each throttled mid-pull flush appends ONE small
#: Parquet of ONLY the rows dirtied since the last flush
#: (:func:`append_ledger_delta`), instead of rewriting the whole O(N) base file.
#: :func:`load_window_ledger` folds ``base + deltas`` newer-file-wins; the
#: pull-end :func:`save_window_ledger` subsumes + deletes them.
DELTAS_DIRNAME: str = "deltas"

#: Minimum seconds between live mid-pull ledger flushes (mirrors the status store):
#: small enough that /coverage/windows feels live, large enough that a
#: multi-thousand-row Parquet is not rewritten on every completed pair.
LIVE_FLUSH_MIN_INTERVAL_S: float = 2.0

#: Default slow-save WARNING threshold (seconds) for :func:`save_window_ledger`
#: (DEC-TELEMETRY-RECONCILE-LEDGERSAVE-001). A universe-scale ledger save was measured at
#: ~3.94 s (DEC-OPTION-WINDOW-PLAN-001, hostile C2), so 10 s is comfortably above the
#: healthy envelope — only a genuinely degraded volume / pathological ledger trips it.
#: Operator-tunable via :data:`LEDGER_SAVE_SLOW_WARN_ENV`.
DEFAULT_LEDGER_SAVE_SLOW_WARN_S: Final[float] = 10.0

#: Environment override for the slow-save WARNING threshold (operator escape hatch).
LEDGER_SAVE_SLOW_WARN_ENV: Final[str] = "BRUTEX_LEDGER_SAVE_SLOW_WARN_S"


def resolve_ledger_save_slow_warn_seconds() -> float:
    """Resolve the slow-save WARN threshold: env override, else the audited default.

    Reads :data:`LEDGER_SAVE_SLOW_WARN_ENV` (``BRUTEX_LEDGER_SAVE_SLOW_WARN_S``); falls back
    to :data:`DEFAULT_LEDGER_SAVE_SLOW_WARN_S` when unset, blank, non-numeric, or
    non-positive — a bad value can never crash a save or silently disable the telemetry
    (fail-safe to the audited default, mirroring
    :func:`brutex.dashboard.pull_stall.resolve_stall_timeout_seconds`). O(1).
    """
    raw = os.environ.get(LEDGER_SAVE_SLOW_WARN_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_LEDGER_SAVE_SLOW_WARN_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_LEDGER_SAVE_SLOW_WARN_S
    if value <= 0.0:
        return DEFAULT_LEDGER_SAVE_SLOW_WARN_S
    return value


#: The CORE columns every ledger Parquet MUST carry — their absence is a
#: wrong-shape file that degrades to an empty ledger (a re-pull, never a crash).
_CORE_COLUMNS: tuple[str, ...] = (
    "market",
    "security_id",
    "interval",
    "window_start",
    "window_end",
    "status",
    "bars",
    "attempts",
    "last_error",
    "first_seen_at",
    "last_attempt_at",
)

#: The full on-disk column order — fixed so the Parquet schema is stable + the row
#: tuples round-trip :meth:`WindowLedger.to_rows` exactly. ``empty_streak`` then
#: ``in_pull_fetches`` then ``bars_net_new`` then the ``empty_retries`` /
#: ``transient_retries`` split then ``streak_provenance`` then
#: ``failed_streak`` were each appended LAST (DEC-WINDOW-LEDGER-001 R1 /
#: DEC-WINDOW-INPULL-FETCHES-001 / DEC-INGEST-BOUNDARY-INTEGRITY-001 /
#: DEC-RETRY-COUNTER-SPLIT-001 / DEC-STREAK-PROVENANCE-001 /
#: DEC-AUTO-RESUME-ZERO-SURPRISES-001), so a legacy
#: file written before any of the optional columns existed loads cleanly: the
#: core columns are read and each optional column is filled per-row (see
#: :func:`_rows_to_records`; an absent ``bars_net_new`` / ``empty_retries`` /
#: ``transient_retries`` / ``streak_provenance`` is an honest ``None``; an
#: absent ``failed_streak`` reads the honest running-counter ``0``).
#: Backward-compatible, never a re-pull.
_COLUMNS: tuple[str, ...] = (
    *_CORE_COLUMNS,
    "empty_streak",
    "in_pull_fetches",
    "bars_net_new",
    "empty_retries",
    "transient_retries",
    "streak_provenance",
    "failed_streak",
)


def ledger_dir(lake_root: Path) -> Path:
    """Return the ledger directory for *lake_root* (``<lake_root>/window_ledger``)."""
    return lake_root / LEDGER_DIRNAME


def ledger_path(lake_root: Path) -> Path:
    """Return the ledger Parquet path (``<lake_root>/window_ledger/ledger.parquet``)."""
    return ledger_dir(lake_root) / LEDGER_FILENAME


def deltas_dir(lake_root: Path) -> Path:
    """The delta directory (``<lake_root>/window_ledger/deltas`` — DEC-LEDGER-DELTA-FLUSH-001)."""
    return ledger_dir(lake_root) / DELTAS_DIRNAME


def list_ledger_deltas(lake_root: Path) -> list[Path]:
    """Every delta Parquet, in APPEND (filename) order — ``[]`` when none exist.

    Delta filenames embed a zero-padded ``time_ns`` prefix (see
    :func:`append_ledger_delta`), so lexicographic filename order IS chronological
    append order — the fold order :func:`load_window_ledger` replays. The prefix
    is minted INSIDE the ledger's cross-process ``partition_write_lock`` AND
    bumped strictly past every delta already on disk
    (:func:`_next_delta_path`, DEC-LEDGER-DELTA-LOCKED-TS-001), so the creation
    order is real by construction — not a clock guess, immune to lock-contention
    reordering and to a wall-clock step-back. O(deltas) directory listing.
    """
    return sorted(deltas_dir(lake_root).glob("*.parquet"))


def _derive_empty_streak(status: str, attempts: int) -> int:
    """Best-effort ``empty_streak`` for a LEGACY row that predates the column (R1).

    A pre-R1 Parquet has no ``empty_streak``; reconstruct it from the row's terminal
    state so the loaded ledger keeps behaving sensibly: a settled ``SKIPPED`` reads
    as a full streak (it is already terminal, so this is inert), a still-``EMPTY``
    row reads as ``attempts`` (a legacy EMPTY row is below the old threshold — else
    it would have been promoted to ``SKIPPED`` — so it never spuriously promotes),
    and every other status resets to 0. New pulls then use the exact streak counter.
    """
    if status == SKIPPED:
        return EMPTY_TERMINAL_THRESHOLD
    if status == EMPTY:
        return attempts
    return 0


def _derive_in_pull_fetches(status: str) -> int:
    """Best-effort ``in_pull_fetches`` for a LEGACY row that predates the column.

    DEC-WINDOW-INPULL-FETCHES-001. A pre-column Parquet has no in-pull fetch count;
    reconstruct the honest FLOOR from the row's status: a ``PLANNED`` row was never
    fetched (``0``); every other status (``SUCCESS`` / ``EMPTY`` / ``FAILED`` /
    ``SKIPPED``) was fetched at least once (``1``). The true in-pull retry count of a
    pre-feature pull is unrecorded, so we never overstate it — the next pull's
    :meth:`WindowLedger.record` stamps the exact count.
    """
    return 0 if status == PLANNED else 1


def _derive_bars_net_new() -> int | None:
    """Honest ``bars_net_new`` for a LEGACY row that predates the column: ``None``.

    DEC-INGEST-BOUNDARY-INTEGRITY-001, mirroring the :func:`_derive_empty_streak`
    / :func:`_derive_in_pull_fetches` optional-column template — but unlike those
    (whose safe floors ARE derivable from the row's terminal state) a legacy
    pull's keep-first-deduped net-new count is genuinely UNRECORDED: any number
    would be a fabrication (Golden Rule #1), and ``0`` in particular would
    falsely claim "that pull added nothing new". The absent column therefore
    ALWAYS loads as ``None`` (honest unknown — the ``int`` side of the annotation
    is the field's domain, deliberately never produced here); the next pull's
    :meth:`WindowLedger.record` stamps the exact count. The helper exists so the
    optional-column template stays uniform and the honest-unknown rule has ONE
    documented home.
    """
    return None


def _derive_retry_count() -> int | None:
    """Honest ``empty_retries`` / ``transient_retries`` for a LEGACY row: ``None``.

    DEC-RETRY-COUNTER-SPLIT-001, the :func:`_derive_bars_net_new` template
    verbatim: a legacy pull persisted only the CONFLATED ``in_pull_fetches``
    total — how that total split between clean-empty re-fetches and transient
    (429/timeout) re-attempts is genuinely UNRECORDED, and any number (a ``0``
    especially — it would falsely claim "no retries of that class fired") would
    be a fabrication (Golden Rule #1). Both absent columns therefore ALWAYS load
    as ``None``; the next pull's :meth:`WindowLedger.record` stamps the exact
    split. ONE helper serves both columns — they share this single derivation
    rationale (CLAUDE.md §11 COMMON).
    """
    return None


def _derive_streak_provenance() -> str | None:
    """Honest ``streak_provenance`` for a LEGACY row that predates the column: ``None``.

    DEC-STREAK-PROVENANCE-001, the :func:`_derive_bars_net_new` twin of the
    optional-column template: a legacy pull's contributing (channel, pull_id)
    bumps are genuinely UNRECORDED — any reconstruction from
    ``(status, streak, last_error)`` would be a fabrication (Golden Rule #1),
    and ``""`` in particular would falsely claim "a KNOWN zero contributing
    bumps" (the tri-state's honest reset value, reserved for a real
    SUCCESS/reopen/plan reset). The absent column therefore ALWAYS loads as
    ``None`` (unknown lineage — every provenance consumer then takes its
    byte-identical legacy fallback); the ``str`` side of the annotation is the
    field's domain, deliberately never produced here. The helper exists so the
    optional-column template stays uniform and the honest-unknown rule has ONE
    documented home for this column.
    """
    return None


def _derive_failed_streak() -> int:
    """Honest ``failed_streak`` for a LEGACY row that predates the column: ``0``.

    DEC-AUTO-RESUME-ZERO-SURPRISES-001, the optional-column template — but
    deliberately ``0``, NOT ``None`` (the :func:`_derive_bars_net_new` honest
    unknown), mirroring ``empty_streak``: ``failed_streak`` is a RUNNING
    counter, not an observation the ledger may have missed — a legacy row has
    made exactly ZERO tracked failed-streak observations under the new
    machinery, so ``0`` is the true count and can never fabricate exhaustion
    (a legacy long-failing window simply starts its K-attempt budget fresh —
    the conservative direction, Golden Rule #1). The helper exists so the
    optional-column template stays uniform with ONE documented home per column.
    """
    return 0


def _rows_to_records(rows: Iterable[Sequence[Any]]) -> list[tuple[Any, ...]]:
    """Coerce raw rows to the exact 18-field tuple :meth:`WindowLedger.from_rows` expects.

    Each row is the core 11 fields plus ``empty_streak`` (DEC-WINDOW-LEDGER-001 R1)
    plus ``in_pull_fetches`` (DEC-WINDOW-INPULL-FETCHES-001) plus ``bars_net_new``
    (DEC-INGEST-BOUNDARY-INTEGRITY-001) plus the ``empty_retries`` /
    ``transient_retries`` split (DEC-RETRY-COUNTER-SPLIT-001) plus
    ``streak_provenance`` (DEC-STREAK-PROVENANCE-001). A ``None`` optional
    column (a legacy file written before that column) is derived: ``empty_streak``
    from ``(status, attempts)`` via :func:`_derive_empty_streak`;
    ``in_pull_fetches`` from ``status`` via :func:`_derive_in_pull_fetches`;
    ``bars_net_new`` stays an honest ``None`` via :func:`_derive_bars_net_new`;
    ``empty_retries`` / ``transient_retries`` stay an honest ``None`` via
    :func:`_derive_retry_count` (a NULL cell in a NEW file — e.g. a lake
    cold-start seed — reads ``None`` the same way); ``streak_provenance``
    stays an honest ``None`` via :func:`_derive_streak_provenance` (unknown
    lineage, never a fabricated ``""``); ``failed_streak``
    (DEC-AUTO-RESUME-ZERO-SURPRISES-001) reads the honest running-counter ``0``
    via :func:`_derive_failed_streak`. A wrong arity / non-coercible
    element raises here (``ValueError`` / ``TypeError``) and is caught by the
    caller, degrading to an empty ledger.
    """
    coerced: list[tuple[Any, ...]] = []
    for (
        market,
        security_id,
        interval,
        window_start,
        window_end,
        status,
        bars,
        attempts,
        last_error,
        first_seen_at,
        last_attempt_at,
        empty_streak,
        in_pull_fetches,
        bars_net_new,
        empty_retries,
        transient_retries,
        streak_provenance,
        failed_streak,
    ) in rows:
        status_s = str(status)
        attempts_i = int(attempts)
        streak = (
            _derive_empty_streak(status_s, attempts_i)
            if empty_streak is None
            else int(empty_streak)
        )
        fetches = (
            _derive_in_pull_fetches(status_s) if in_pull_fetches is None else int(in_pull_fetches)
        )
        net_new = _derive_bars_net_new() if bars_net_new is None else int(bars_net_new)
        empties = _derive_retry_count() if empty_retries is None else int(empty_retries)
        transients = _derive_retry_count() if transient_retries is None else int(transient_retries)
        provenance = (
            _derive_streak_provenance() if streak_provenance is None else str(streak_provenance)
        )
        failed_run = _derive_failed_streak() if failed_streak is None else int(failed_streak)
        coerced.append(
            (
                str(market),
                str(security_id),
                str(interval),
                str(window_start),
                str(window_end),
                status_s,
                int(bars),
                attempts_i,
                (None if last_error is None else str(last_error)),
                str(first_seen_at),
                (None if last_attempt_at is None else str(last_attempt_at)),
                streak,
                fetches,
                net_new,
                empties,
                transients,
                provenance,
                failed_run,
            )
        )
    return coerced


#: The exception set a per-file ledger Parquet read degrades on — the SAME set the
#: pre-delta loader caught (wrong shape / IO / import), ONE source of truth for the
#: base-degrade AND the per-delta skip (DEC-LEDGER-DELTA-FLUSH-001).
_LEDGER_READ_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    ValueError,
    TypeError,
    KeyError,
    ImportError,
)

#: The LOW-cardinality string columns the per-load intern memo deduplicates
#: (DEC-LEDGER-LOAD-SPACE-V1-001): ``pyarrow``'s ``to_pylist()`` yields a
#: DISTINCT ``str`` object per CELL — even the constant ``"1minute"`` shares
#: nothing across rows (measured 794 B/row logical / 1,305 B/row RSS on an
#: 87,573-row real-writer fixture; the on-disk row is ~13 B). These seven
#: columns are the ones whose value sets are small relative to the row count
#: (market ~3 · interval 1 · status ~5 · window dates ~hundreds · security_id
#: ~2-3 windows per contract · last_error a small error-string set), so
#: deduplicating equal cells onto ONE shared object cuts ~40% of the per-row
#: decode cost. ``first_seen_at`` / ``last_attempt_at`` are DELIBERATELY
#: excluded — µs-ISO timestamps are essentially unique, so a memo would just
#: double-hold them (skip decided per COLUMN, never a per-value heuristic).
_INTERNED_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "market",
        "security_id",
        "interval",
        "window_start",
        "window_end",
        "status",
        "last_error",
    }
)


def _intern_cells(cells: list[Any], memo: dict[str, str]) -> list[Any]:
    """Dedup equal ``str`` cells onto one shared object — values byte-identical.

    DEC-LEDGER-LOAD-SPACE-V1-001. Only object IDENTITY changes (``==``-equal
    strings collapse onto the memo's first instance); a non-``str`` cell (the
    ``None`` of a nullable column) passes through untouched. The *memo* is
    per-:func:`load_window_ledger`-call (shared across the base + every delta
    file) — deliberately NOT :func:`sys.intern`, whose process-lifetime table
    would accumulate high-cardinality ``security_id`` strings across many
    lakes/tests forever; the per-call dict has exactly the load's lifetime.
    O(cells) with one O(1) dict op per string cell.
    """
    return [memo.setdefault(v, v) if type(v) is str else v for v in cells]


def _read_ledger_file_rows(
    path: Path, *, memo: dict[str, str] | None = None
) -> list[tuple[Any, ...]]:
    """Read ONE ledger-shaped Parquet into the coerced 18-field row tuples.

    The single per-file read (CLAUDE.md §11 COMMON) shared by the BASE load and
    the per-DELTA fold. The core 11 columns are REQUIRED — a missing one raises
    ``KeyError`` (the wrong-shape guard the caller degrades on). ``empty_streak``
    + ``in_pull_fetches`` + ``bars_net_new`` + ``empty_retries`` +
    ``transient_retries`` + ``streak_provenance`` + ``failed_streak`` are
    OPTIONAL (a legacy file lacks one or more) and derived per-row by
    :func:`_rows_to_records` (an absent ``bars_net_new`` / ``empty_retries`` /
    ``transient_retries`` / ``streak_provenance`` stays an honest ``None`` —
    DEC-INGEST-BOUNDARY-INTEGRITY-001 / DEC-RETRY-COUNTER-SPLIT-001 /
    DEC-STREAK-PROVENANCE-001; an absent ``failed_streak`` reads the honest
    running-counter ``0`` — DEC-AUTO-RESUME-ZERO-SURPRISES-001). Raises any
    of :data:`_LEDGER_READ_ERRORS` on a malformed / unreadable file — the CALLER
    decides the degrade (base → empty + ``load_failed``; delta → skip that one
    file).

    *memo* is the per-load string-intern table (DEC-LEDGER-LOAD-SPACE-V1-001):
    :func:`load_window_ledger` threads ONE dict across the base + every delta so
    equal low-cardinality cells (:data:`_INTERNED_COLUMNS`) share one object
    across ALL folded files; ``None`` (a direct call) interns within this one
    file only. Values are byte-identical either way — only identity changes.
    """
    import pyarrow.parquet as pq

    with open_regular_binary(path) as handle:
        table = pq.read_table(handle)  # type: ignore[no-untyped-call, unused-ignore]
    present = set(table.schema.names)
    intern_memo: dict[str, str] = {} if memo is None else memo
    columns = {
        name: (
            _intern_cells(table.column(name).to_pylist(), intern_memo)
            if name in _INTERNED_COLUMNS
            else table.column(name).to_pylist()
        )
        for name in _CORE_COLUMNS
    }
    n = len(columns[_CORE_COLUMNS[0]])

    def _optional(name: str) -> list[Any]:
        """One OPTIONAL column's cells — ``None``-filled when the file predates it."""
        return table.column(name).to_pylist() if name in present else [None] * n

    streaks = _optional("empty_streak")
    fetches = _optional("in_pull_fetches")
    net_new = _optional("bars_net_new")
    empties = _optional("empty_retries")
    transients = _optional("transient_retries")
    provenance = _optional("streak_provenance")
    failed_runs = _optional("failed_streak")
    rows = zip(
        *(columns[name] for name in _CORE_COLUMNS),
        streaks,
        fetches,
        net_new,
        empties,
        transients,
        provenance,
        failed_runs,
        strict=True,
    )
    return _rows_to_records(rows)


def load_window_ledger(
    lake_root: Path,
    *,
    threshold: int = EMPTY_TERMINAL_THRESHOLD,
    threshold_failed: int = FAILED_TERMINAL_THRESHOLD,
) -> WindowLedger:
    """Load the persisted ledger (base **+ delta fold**), or an EMPTY one if unavailable.

    *threshold_failed* (DEC-AUTO-RESUME-ZERO-SURPRISES-001) mirrors *threshold*
    for the fetch-exhausted ``FAILED`` → ``SKIPPED`` settle — an additive
    keyword, default byte-identical for every existing caller.

    **Never raises** (reliability): a missing base file is a clean empty ledger; a
    malformed / wrong-shaped / unreadable base Parquet logs a WARNING and degrades
    to empty with ``load_failed=True`` (re-pulling is safe — the idempotent sink
    no-ops stored bars).

    **Delta fold (DEC-LEDGER-DELTA-FLUSH-001):** every delta Parquet under
    ``window_ledger/deltas/`` is replayed over the base in APPEND (filename) order,
    **newer-file-wins per window key** — a WAL replay of one lake's serialised
    write chronology (appends run under the ledger's cross-process lock), NOT a
    cross-process merge. The item sketch named :func:`merge_ledger_rows` for this
    fold; that rule is DELIBERATELY not used here because its more-settled
    preference would resurrect a deliberate demotion (a ``reopen_skipped`` /
    degraded ``SUCCESS→FAILED`` row) from an older, more-settled copy —
    regression-pinned. :func:`merge_ledger_rows` STAYS the cross-process arbiter
    where it belongs: the read-merge-write inside :func:`save_window_ledger`. A
    malformed individual delta logs ``window_ledger.delta_load_failed`` and is
    SKIPPED (the never-raises degrade contract) — only a failed BASE read sets
    ``load_failed=True``. A base-less lake with deltas (a first pull's mid-flight
    checkpoint) reconstructs from the deltas alone.

    **Interned decode (DEC-LEDGER-LOAD-SPACE-V1-001):** ONE per-call string
    memo is threaded through every per-file read (base + deltas), so equal
    low-cardinality cells (:data:`_INTERNED_COLUMNS`) share one object across
    the whole load instead of ``to_pylist()``'s one-distinct-``str``-per-cell
    (~40% of the measured 794 B/row logical decode cost). Values byte-identical
    — only object identity changes; the memo dies with this call.
    """
    path = ledger_path(lake_root)
    delta_files = list_ledger_deltas(lake_root)
    load_failed = False
    # The per-LOAD intern memo — shared across the base + every delta file so a
    # delta's re-write of a base row re-uses the base's string objects too.
    memo: dict[str, str] = {}
    base_rows: list[tuple[Any, ...]] = []
    if path.is_file():
        try:
            base_rows = _read_ledger_file_rows(path, memo=memo)
        except _LEDGER_READ_ERRORS as exc:
            _LOG.warning("window_ledger.load_failed", path=str(path), error=str(exc))
            # LOUD degrade (DEC-WINDOW-LEDGER-RESCUE-001): the file EXISTED but could
            # not be read — mark the returned ledger so the pull drivers announce the
            # failed reload (window ledger reload FAILED — running full re-verify
            # pull, no skip) instead of silently re-fetching everything. Still never
            # raises (a lost ledger costs a re-pull, never data). Readable deltas are
            # still folded below — better a partial rescue than none.
            load_failed = True
    elif not delta_files:
        return WindowLedger(_threshold=threshold, _threshold_failed=threshold_failed)
    folded: dict[tuple[str, str, str, str, str], tuple[Any, ...]] = {
        (row[0], row[1], row[2], row[3], row[4]): row for row in base_rows
    }
    for delta in delta_files:
        try:
            delta_rows = _read_ledger_file_rows(delta, memo=memo)
        except _LEDGER_READ_ERRORS as exc:
            _LOG.warning("window_ledger.delta_load_failed", path=str(delta), error=str(exc))
            continue
        for row in delta_rows:
            folded[(row[0], row[1], row[2], row[3], row[4])] = row  # newer-file-wins
    ledger = WindowLedger.from_rows(
        (folded[key] for key in sorted(folded)),
        threshold=threshold,
        threshold_failed=threshold_failed,
    )
    ledger.load_failed = load_failed
    return ledger


#: One serialised ledger row — EXACTLY the 18-field tuple :meth:`WindowLedger.to_rows`
#: emits (and :meth:`WindowLedger.from_rows` restores); the ONE spelling lives on the
#: ledger module as :data:`~brutex.data.window_ledger.LedgerRowTuple` (CLAUDE.md §11
#: COMMON) and this store-side name aliases it. The merge-on-save operates on this
#: shape directly so the conflict rule below is pure + unit-testable. The
#: ``bars_net_new`` / ``empty_retries`` / ``transient_retries`` (``int | None``)
#: and ``streak_provenance`` (``str | None``) fields are nullable (``None`` =
#: honest unknown for a legacy row); the trailing ``failed_streak`` is a plain
#: ``int`` running counter (a legacy row loads ``0`` —
#: DEC-AUTO-RESUME-ZERO-SURPRISES-001).
LedgerRow = LedgerRowTuple

#: The window identity — the leading 5 fields of a :data:`LedgerRow` (the same 5-tuple
#: :meth:`WindowLedger._key` derives from a :class:`WindowKey`).
_RowKey = tuple[str, str, str, str, str]

#: Settledness rank for the same-key merge conflict rule (DEC-WINDOW-LEDGER-RESCUE-001):
#: a MORE-SETTLED status wins — ``SUCCESS`` / ``SKIPPED`` (terminal, skippable) over
#: ``EMPTY`` (a genuine observation) over ``FAILED`` (transient) over ``PLANNED`` (pure
#: intent). An UNKNOWN status ranks BELOW everything (defensive — never let a malformed
#: row displace a real one).
_STATUS_SETTLE_RANK: dict[str, int] = {SUCCESS: 3, SKIPPED: 3, EMPTY: 2, FAILED: 1, PLANNED: 0}


def _row_key(row: LedgerRow) -> _RowKey:
    """The window identity of one serialised row — O(1)."""
    return (row[0], row[1], row[2], row[3], row[4])


def _row_preference(row: LedgerRow) -> tuple[int, str, int, tuple[str, ...]]:
    """The total order that decides a same-key merge conflict — pure, O(1).

    DEC-WINDOW-LEDGER-RESCUE-001 conflict rule (documented in
    :func:`merge_ledger_rows`): (1) the MORE-SETTLED status wins
    (``SUCCESS``/``SKIPPED`` > ``EMPTY`` > ``FAILED`` > ``PLANNED``); (2) then the
    NEWER ``last_attempt_at`` (ISO-8601 UTC strings compare lexicographically;
    ``None`` — never attempted — sorts oldest); (3) then the HIGHER ``attempts``;
    (4) then the lexicographically greater full row (``None`` → ``""``) — a pure
    determinism tie-break so the merged result is identical regardless of which
    writer's save runs last (order-independence, Golden Rule #7).
    """
    return (
        _STATUS_SETTLE_RANK.get(row[5], -1),
        row[10] or "",
        row[7],
        tuple("" if v is None else str(v) for v in row),
    )


def merge_ledger_rows(
    memory_rows: Sequence[LedgerRow],
    disk_rows: Sequence[LedgerRow],
    *,
    pruned_keys: frozenset[_RowKey] = frozenset(),
    reopened_keys: frozenset[_RowKey] = frozenset(),
    degraded_keys: frozenset[_RowKey] = frozenset(),
) -> list[LedgerRow]:
    """Merge the in-memory rows with the on-disk rows per window key — pure.

    DEC-WINDOW-LEDGER-RESCUE-001. The save is a whole-file replace, so without this
    merge two CONCURRENT pulls sharing one lake (a CLI ``brutex fetch`` + a dashboard
    pull) last-writer-wins clobber each other's settled rows — the exact silent
    cross-process data-LOSS class DEC-LAKE-PARTITION-FLOCK-001 closed for the bar
    partitions. Rules (per key):

    * **memory-only** → the in-memory row (this writer's new work).
    * **disk-only** → the on-disk row is KEPT (a concurrent writer's work) — UNLESS
      its key is in *pruned_keys* (a row THIS ledger deliberately pruned via
      :meth:`~brutex.data.window_ledger.WindowLedger.prune_superseded`), which is
      dropped so the DEC-WINDOW-TRAILING-GC-001 orphan GC is not undone by the merge.
    * **both (a genuine semantic conflict — same key, both updated)** → the winner by
      :func:`_row_preference`: the MORE-SETTLED status first
      (``SUCCESS``/``SKIPPED`` > ``EMPTY`` > ``FAILED`` > ``PLANNED``), then the newer
      ``last_attempt_at`` timestamp, then the higher ``attempts``, then a
      deterministic full-row tie-break — so the merged file is IDENTICAL regardless
      of writer order (no row loss, deterministic winner). EXCEPTION: a key in
      *reopened_keys* (a ``SKIPPED`` row THIS ledger DELIBERATELY demoted to a
      retryable ``FAILED`` via
      :meth:`~brutex.data.window_ledger.WindowLedger.reopen_skipped`,
      DEC-EMPTY-WINDOW-CIRCUIT-BREAKER-001) keeps the IN-MEMORY row outright — the
      more-settled rule would otherwise resurrect the on-disk ``SKIPPED`` copy and
      silently undo the operator's re-open (the ``brutex reopen-skipped`` CLI runs
      under the account-wide pull single-flight lock, so no concurrent pull is
      mid-flight while it saves; a re-verify pull's later genuine outcome then wins
      normally on ITS save). Likewise a key in *degraded_keys* (a clean SUCCESS THIS
      ledger demoted to a re-pullable ``FAILED`` for a DATA-COMPLETENESS reason —
      a confirmed interior gap / trailing truncation, ``record(degraded=True)``,
      DEC-WINDOW-LEDGER-DEGRADED-TOMBSTONE-001) keeps the IN-MEMORY row outright:
      without it a CONCURRENT pull's on-disk ``SUCCESS`` (rank 3) would beat the
      demoted ``FAILED`` (rank 1) on this writer's own save and silently re-mask
      the gap forever. A genuine later fill still surfaces correctly — the filling
      pull records ``SUCCESS`` with an EMPTY tombstone set, so ITS save keeps the
      ``SUCCESS`` (honest residual: a concurrent writer that does not itself run
      the gap detector can re-mask under the more-settled rule on ITS flush — a
      pre-existing property of the merge rule. A re-masked ``SUCCESS`` is
      skippable, so an ordinary later pull does NOT re-fetch it and the fetch-time
      detector never sees it again; an INTERIOR gap re-demotes only on the next
      lost-ledger COLD-START — the seed's density cross-check,
      DEC-COLDSTART-SEED-INTERIOR-GAP-001, leaves it un-seeded → re-planned →
      re-fetched — or on an explicit operator re-plan, and a re-masked TRAILING
      truncation needs the re-plan (the cold-start check is interior-only) —
      hostile-review wording fix: the residual does NOT self-heal on a
      steady-state pull).

    Output is sorted by window key (the same order :meth:`WindowLedger.to_rows`
    emits), so a merge with an empty/absent disk side is byte-row-identical to the
    pre-merge save. O(memory + disk) with O(1) per-row work.
    """
    merged: dict[_RowKey, LedgerRow] = {_row_key(row): row for row in memory_rows}
    for row in disk_rows:
        key = _row_key(row)
        mine = merged.get(key)
        if mine is None:
            if key not in pruned_keys:
                merged[key] = row
            continue
        if key in reopened_keys or key in degraded_keys:
            continue  # a deliberate demotion (re-open / degraded) — the in-memory row wins
        if _row_preference(row) > _row_preference(mine):
            merged[key] = row
    return sorted(merged.values(), key=_row_key)


def _rows_to_table(rows: Sequence[LedgerRow]) -> Any:
    """Serialise ledger rows to a pyarrow table with the FIXED :data:`_COLUMNS` schema.

    The ONE table builder (CLAUDE.md §11 COMMON) shared by the full
    :func:`save_window_ledger` and the incremental :func:`append_ledger_delta`, so
    a delta Parquet is byte-schema-identical to the base and loads through the
    SAME :func:`_read_ledger_file_rows`.
    """
    import pyarrow as pa

    by_col: dict[str, list[Any]] = {name: [] for name in _COLUMNS}
    for row in rows:
        for name, value in zip(_COLUMNS, row, strict=True):
            by_col[name].append(value)
    schema = pa.schema(
        [
            ("market", pa.string()),
            ("security_id", pa.string()),
            ("interval", pa.string()),
            ("window_start", pa.string()),
            ("window_end", pa.string()),
            ("status", pa.string()),
            ("bars", pa.int64()),
            ("attempts", pa.int64()),
            ("last_error", pa.string()),
            ("first_seen_at", pa.string()),
            ("last_attempt_at", pa.string()),
            ("empty_streak", pa.int64()),
            ("in_pull_fetches", pa.int64()),
            # Nullable by design: a legacy/seeded row's net-new is an honest
            # unknown ``None`` (DEC-INGEST-BOUNDARY-INTEGRITY-001).
            ("bars_net_new", pa.int64()),
            # Nullable by design: a legacy row's retry SPLIT is an honest
            # unknown ``None`` (DEC-RETRY-COUNTER-SPLIT-001).
            ("empty_retries", pa.int64()),
            ("transient_retries", pa.int64()),
            # Nullable by design: a legacy row's streak lineage is an honest
            # unknown ``None`` (DEC-STREAK-PROVENANCE-001 — never a fabricated
            # ``""`` known-zero).
            ("streak_provenance", pa.string()),
            # A running counter, always written as a real int (a LEGACY file
            # simply lacks the column and loads ``0`` per row —
            # DEC-AUTO-RESUME-ZERO-SURPRISES-001).
            ("failed_streak", pa.int64()),
        ]
    )
    return pa.table({name: by_col[name] for name in _COLUMNS}, schema=schema)


def _render_ledger_parquet_python(rows: Sequence[LedgerRowTuple]) -> bytes:
    """The VERBATIM pyarrow ledger render — the retained live oracle.

    DEC-LAKE-RUST-LEDGERSTORE-CONVERT-001 (MIGRATION.md #1918): the exact
    render both write sites ran inline pre-conversion —
    :func:`_rows_to_table` then a zero-kwarg ``pq.write_table`` — extracted
    to ONE place, rendering to BYTES (``pq.write_table`` writes identical
    bytes to a buffer and to a path — pinned in the store suite beside the
    byte-determinism golden probe; "byte-identical" is scoped to the BYTES —
    the buffer hop adds a bounded one-render peak-memory delta on the default
    path, the full parquet buffer held in RAM before the temp write, KB–MB at
    ledger scale beside the already-materialised pyarrow table). Flag-OFF
    production runs exactly this;
    flag-ON it is the ROW-EQUIVALENCE burn-in oracle the facade compares
    the crate render against (never bytes — the #1917 measured-infeasible
    verdict; see :mod:`brutex.data.lake_rust_ledger_render`).
    """
    import io

    import pyarrow.parquet as pq

    table = _rows_to_table(rows)
    buffer = io.BytesIO()
    pq.write_table(table, buffer)  # type: ignore[no-untyped-call, unused-ignore]
    return buffer.getvalue()


def _ledger_parquet_bytes(rows: Sequence[LedgerRowTuple]) -> bytes:
    """The ONE shared ledger render body — base save + delta append, flag-gated.

    DEC-LAKE-RUST-LEDGERSTORE-CONVERT-001: both write sites
    (:func:`append_ledger_delta` + :func:`save_window_ledger`) render their
    parquet bytes HERE, so the default-OFF ``BRUTEX_LAKE_RUST_LEDGER_RENDER``
    seam has exactly one home. Flag OFF (the default) runs the verbatim
    pyarrow oracle directly — the crate is never imported (bomb-spy-pinned
    in the facade suite); flag ON routes through
    :func:`~brutex.data.lake_rust_ledger_render.render_ledger_parquet_bytes`,
    whose EVERY path still returns valid parquet bytes (guarded / degraded /
    burn-in-mismatched calls serve the oracle). Callers run this INSIDE
    :func:`~brutex.data.atomic_io.atomic_write_via`'s guarded callable, so
    the ENOSPC/OOM → :class:`~brutex.data.errors.LakeWriteHalt` law
    (DEC-WINDOW-LEDGER-WRITE-HALT-001) covers the render on BOTH arms,
    byte-identical to the pre-conversion semantics.
    """
    if lake_rust_ledger_render_enabled():
        return render_ledger_parquet_bytes(
            rows=rows,
            python_render=lambda: _render_ledger_parquet_python(rows),
        )
    return _render_ledger_parquet_python(rows)


def _next_delta_path(directory: Path) -> Path:
    """Mint the next delta filename — call ONLY while holding the ledger flock.

    P2-1 fix (DEC-LEDGER-DELTA-LOCKED-TS-001): the ``time_ns`` prefix used to be
    sampled BEFORE :func:`~brutex.data.partition_lock.partition_write_lock` was
    acquired, so under cross-process contention (``flock`` has no FIFO fairness
    — the exact CLI+dashboard shared-lake scenario of
    DEC-LAKE-PARTITION-FLOCK-001) the later-locking writer could carry the
    EARLIER filename, inverting the fold order relative to the real serialised
    append order — breaking the "filename order == append order" invariant the
    newer-file-wins fold in :func:`load_window_ledger` relies on. Minting the
    prefix UNDER the same flock that serialises the append — and bumping it
    STRICTLY past every delta already on disk — makes filename order equal to
    append order by construction, immune even to a wall-clock step-back (the
    on-disk maximum is the floor, not the clock). A foreign ``*.parquet`` file
    with a non-numeric prefix is ignored (it never participated in the ordering
    contract). O(deltas) per append — bounded, because the pull-end full save
    prunes the directory (delta subsumption, DEC-LEDGER-DELTA-FLUSH-001).
    """
    import os
    import time

    ts = time.time_ns()
    latest = 0
    for existing in directory.glob("*.parquet"):
        prefix = existing.name.split("-", 1)[0]
        try:
            latest = max(latest, int(prefix))
        except ValueError:  # a foreign file — never blocks the append
            continue
    if ts <= latest:
        ts = latest + 1
    return directory / f"{ts:020d}-{os.getpid()}.parquet"


def append_ledger_delta(ledger: WindowLedger, lake_root: Path) -> int:
    """Append ONLY *ledger*'s dirtied rows as one new delta Parquet — the O(dirty) flush.

    DEC-LEDGER-DELTA-FLUSH-001 (first step — base compaction of crash-orphaned
    deltas is a NAMED follow-up, not built). The throttled mid-pull flush used to
    call :func:`save_window_ledger` — an O(N) whole-file READ-MERGE-WRITE
    (~841k rows ≈ ~4 s/save measured) on every fire. This appends ONE small
    Parquet of just the rows dirtied since the last flush
    (:meth:`~brutex.data.window_ledger.WindowLedger.dirty_rows`) to
    ``window_ledger/deltas/``, under the SAME cross-process
    :func:`~brutex.data.partition_lock.partition_write_lock` (keyed on the ledger
    path) the full save holds — so appends serialise against each other AND
    against a concurrent full save. The filename embeds a zero-padded ``time_ns``
    + pid, minted INSIDE the lock and bumped strictly past every delta already
    on disk (:func:`_next_delta_path`, DEC-LEDGER-DELTA-LOCKED-TS-001), so
    filename order == append order (the fold order) by construction.

    Contracts preserved:

    * **No dirty rows** → a pure no-op (no lock, no file).
    * **Disk-full / OOM** → the COMMON :func:`~brutex.data.atomic_io.atomic_write_via`
      maps ENOSPC/MemoryError to :class:`~brutex.data.errors.LakeWriteHalt`
      (DEC-WINDOW-LEDGER-WRITE-HALT-001 — a full disk still halts the pull
      gracefully); every other error propagates unchanged.
    * **Lock timeout** → a loud ``window_ledger.delta_lock_timeout`` WARNING, the
      keys STAY dirty, and the next throttled flush retries (mirrors the full
      save's skip contract).
    * The dirty keys are cleared ONLY after a successful atomic write.

    Returns the number of rows appended (0 for a no-op / a skipped flush).
    """
    rows = ledger.dirty_rows()
    if not rows:
        return 0
    # MID-PULL COMPACTION FOLLOW-UP (deferred by the coordinator with
    # DEC-WINDOWS-MIDPULL-FLOOR-001, distinct from the crash-orphan compactor
    # named above): deltas accrue HERE ~1 file per LIVE_FLUSH_MIN_INTERVAL_S
    # (2 s) and are pruned only by the next FULL save's subsumption, so a
    # multi-hour pull reaches thousands of pending files and every fresh-stamp
    # reader fold pays one Parquet footer open per file (db_view's
    # `_ledger_scan_source` envelope). The read side is floor-throttled + page-
    # cached (MIGRATION.md #1637); bounding the FILE COUNT itself (periodic
    # mid-pull delta consolidation under this same lock) stays the named
    # follow-up.
    directory = deltas_dir(lake_root)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with partition_write_lock(lake_root, ledger_path(lake_root)):
            # The filename timestamp is minted UNDER the flock (P2-1,
            # DEC-LEDGER-DELTA-LOCKED-TS-001) — sampling it before the acquire
            # let a later-locking writer carry an earlier filename under
            # contention, inverting the newer-file-wins fold order.
            path = _next_delta_path(directory)

            def _write_parquet(tmp: Path) -> None:
                # The ONE shared render body (flag-gated Rust seam,
                # DEC-LAKE-RUST-LEDGERSTORE-CONVERT-001) runs INSIDE the
                # guarded callable, so a render-time ENOSPC/OOM still maps to
                # LakeWriteHalt exactly as the inline pq.write_table did.
                tmp.write_bytes(_ledger_parquet_bytes(rows))

            atomic_write_via(path, _write_parquet)
    except PartitionLockTimeout as exc:
        _LOG.warning(
            "window_ledger.delta_lock_timeout",
            path=str(directory),
            error=str(exc),
        )
        return 0
    ledger.clear_dirty((row[0], row[1], row[2], row[3], row[4]) for row in rows)
    _LOG.debug("window_ledger.delta_appended", path=str(path), rows=len(rows))
    return len(rows)


def save_window_ledger(
    ledger: WindowLedger, lake_root: Path, *, clock: Callable[[], float] | None = None
) -> None:
    """Persist *ledger* — cross-process-locked READ-MERGE-WRITE, then atomic replace.

    DEC-WINDOW-LEDGER-RESCUE-001 (supersedes the unlocked whole-file replace): the
    whole save runs under the COMMON per-partition cross-process
    :func:`~brutex.data.partition_lock.partition_write_lock` keyed on the ledger
    path, and under the lock it (1) loads the CURRENT on-disk ledger (never raises —
    a corrupt/absent file merges as empty), (2) merges it with the in-memory rows via
    :func:`merge_ledger_rows` (per-key, preferring the more-settled status →
    newer ``last_attempt_at`` → higher ``attempts``; a row this ledger deliberately
    PRUNED is dropped, not resurrected, and a row it deliberately DEMOTED — RE-OPENED
    via ``reopen_skipped`` or degraded-demoted via ``record(degraded=True)``
    (DEC-WINDOW-LEDGER-DEGRADED-TOMBSTONE-001) — keeps the in-memory demotion,
    so a concurrent disk ``SUCCESS`` can never silently re-mask a confirmed
    interior gap), then (3) serialises the merged rows to a
    Parquet table with the fixed :data:`_COLUMNS` schema and atomically replaces the
    file. Two concurrent pulls on one lake therefore NEVER clobber each other's rows
    (the later writer reads AFTER the earlier writer's ``os.replace``). The ledger
    directory is created if absent. O(N) over the recorded windows plus one on-disk
    read (once per run / per throttled flush — the adaptive flush throttle self-paces
    on the measured save duration, so the added read never dominates wall time).

    A bounded lock-acquire expiry (:class:`~brutex.data.errors.PartitionLockTimeout`
    — a peer wedged past the deadline) SKIPS this save with a loud
    ``window_ledger.save_lock_timeout`` WARNING instead of crashing the pull: the
    in-memory ledger is intact, the next throttled flush / pull-end save retries, and
    even a lost final save only costs a re-pull (idempotent keep-first sink), never
    data. A disk-full/OOM still maps to the pull-wide
    :class:`~brutex.data.errors.LakeWriteHalt` (see below), and any other error
    propagates unchanged.

    **Save telemetry (DEC-TELEMETRY-RECONCILE-LEDGERSAVE-001, observe-only):** every
    COMPLETED save — this is the ONE common FULL-save chokepoint every production
    pull-end save goes through (the throttled mid-pull flush is the SEPARATE
    O(dirty) :func:`append_ledger_delta` since DEC-LEDGER-DELTA-FLUSH-001, with its
    own ``delta_appended`` / ``flush_paced`` events) — emits ONE
    ``window_ledger.save_completed`` INFO carrying the merged row count + the
    whole-body ``duration_ms`` (lock acquire + disk read incl. the delta fold +
    merge + serialize + atomic replace + delta subsumption prune), and additionally
    ONE ``window_ledger.save_slow`` WARNING when the duration EXCEEDS the
    env-tunable :func:`resolve_ledger_save_slow_warn_seconds` threshold (default
    :data:`DEFAULT_LEDGER_SAVE_SLOW_WARN_S` = 10 s). The single ``save_completed``
    event name is deliberate — a coordinated sibling session instrumenting this same
    save path (torn-read forensics) converges on ONE event, never two. A lock-timeout
    SKIP emits its existing warning only (no ``save_completed`` — nothing completed);
    a raising save emits nothing new (the error propagates unchanged). *clock* is an
    injectable monotonic source for deterministic tests (``None`` → wall
    :func:`time.monotonic`); logging is the ONLY delta — the save's control flow,
    stored rows and error contracts are byte-identical.

    **Delta subsumption (DEC-LEDGER-DELTA-FLUSH-001):** the under-lock
    :func:`load_window_ledger` read transparently FOLDS every pending delta into
    ``disk_rows``, so the merged full file SUBSUMES them; after the atomic
    replace (still under the SAME lock) the folded delta files are deleted —
    lifecycle hygiene so deltas cannot grow unboundedly across normal pulls (NOT
    the compaction follow-up: deltas orphaned by a CRASHED pull are folded +
    subsumed by the NEXT pull's first save; a scheduled standalone compactor
    stays the named follow-up). A failed per-file unlink is loudly logged
    (``window_ledger.delta_prune_failed``) — honest envelope: a stale delta left
    behind would newer-wins-override this fresher base on the next load until a
    later successful save prunes it.
    """
    import contextlib

    # Liveness beats around the whole synchronous save body
    # (DEC-HEAL-SCAN-LIVENESS-BEATS-001): the drain-time full save is a single
    # multi-second synchronous read-merge-write (a forensically-measured
    # 12.96 s ``window_ledger.save_slow`` over 68,234 rows) running on the
    # beat-watched pull worker AFTER the last fetched-window heartbeat — the
    # entry beat marks the save alive and the completion beat (both arms of
    # the try/except below) refreshes liveness before the next silent stage.
    # HONEST envelope: no mid-save heartbeat thread — a save LONGER than the
    # ~600 s stall timeout would still gap (the ``save_slow`` WARN makes it
    # visible); ``emit_progress_beat`` is a cheap no-op with no watchdog.
    emit_progress_beat()
    tick = clock if clock is not None else time.monotonic
    started = tick()
    directory = ledger_dir(lake_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = ledger_path(lake_root)
    try:
        # Cross-process lock keyed on the ledger path (DEC-LAKE-PARTITION-FLOCK-001
        # mechanics, DEC-WINDOW-LEDGER-RESCUE-001 wiring): held across the WHOLE
        # read-merge-write so the later writer reads AFTER the earlier writer's
        # os.replace — its merge then includes the earlier rows (no lost update).
        with partition_write_lock(lake_root, path):
            # Capture the delta set FIRST (no new delta can appear while the lock
            # is held — append_ledger_delta takes the SAME lock), then load: the
            # load folds exactly these files, so deleting them after the replace
            # is safe (DEC-LEDGER-DELTA-FLUSH-001).
            subsumed_deltas = list_ledger_deltas(lake_root)
            disk_rows = load_window_ledger(lake_root).to_rows()
            # LOAD-BEARING snapshot ORDER (hostile-review fold, DEC-WINDOW-LEDGER-
            # DEGRADED-TOMBSTONE-001): the ROW snapshot is taken BEFORE the
            # tombstone snapshots, and a tombstone only ever moves "forward" (added
            # at the demotion, under the SAME ledger lock as the row; discarded only
            # when the SAME key's row is superseded by a settled outcome). So a
            # demoted FAILED observed in `memory_rows` is guaranteed still
            # tombstoned in `degraded_keys` UNLESS a newer settled row (which the
            # next flush persists) genuinely superseded it — the reverse order
            # would open a race where a fresh demotion is saved un-tombstoned and
            # the disk SUCCESS silently wins.
            memory_rows = ledger.to_rows()
            rows = merge_ledger_rows(
                memory_rows,
                disk_rows,
                pruned_keys=ledger.pruned_keys(),
                reopened_keys=ledger.reopened_keys(),
                degraded_keys=ledger.degraded_keys(),
            )

            def _write_parquet(tmp: Path) -> None:
                # The ONE shared render body (flag-gated Rust seam,
                # DEC-LAKE-RUST-LEDGERSTORE-CONVERT-001) runs INSIDE the
                # guarded callable — see the delta twin above.
                tmp.write_bytes(_ledger_parquet_bytes(rows))

            # COMMON lake-safe atomic write (tmp→fsync→os.replace→cleanup, CLAUDE.md §11,
            # DEC-WINDOW-LEDGER-WRITE-HALT-001): the ledger is a LAKE write (it lives under
            # <lake_root>/window_ledger/), so a disk-full (ENOSPC) / OOM here fails every
            # remaining window identically — it must HALT the pull gracefully, not be
            # mis-quarantined per-symbol (which would keep hammering a full disk).
            # `atomic_write_via` maps ENOSPC→LakeWriteHalt("disk_full") /
            # MemoryError→LakeWriteHalt("out_of_memory") (every other error UNCHANGED — a
            # non-ENOSPC OSError such as EACCES propagates raw, never mis-escalated).
            # LakeWriteHalt is a member of BOTH ingest spines' _GLOBAL_STOP_ERRORS, so it
            # propagates past the per-symbol/per-contract quarantine to the CLI
            # `_pull_halt_errors` / dashboard `is_pull_halt` halt handling already in
            # place. The fsync (durability for the durable ledger) + temp cleanup on any
            # error are unchanged.
            atomic_write_via(path, _write_parquet)
            # The merged base now carries every folded delta row — delete the
            # subsumed files (still under the lock, so a concurrent append can
            # never interleave with the prune).
            for delta in subsumed_deltas:
                try:
                    delta.unlink()
                except OSError as exc:
                    _LOG.warning(
                        "window_ledger.delta_prune_failed",
                        path=str(delta),
                        error=str(exc),
                    )
            with contextlib.suppress(OSError):
                # Best-effort tidy of an emptied deltas dir (its absence is the
                # signal the /coverage fast paths use to stay base-only).
                deltas_dir(lake_root).rmdir()
        # The full file now persists EVERY in-memory row — nothing is left for
        # the incremental stream to carry (DEC-LEDGER-DELTA-FLUSH-001).
        ledger.clear_dirty(ledger.dirty_keys())
        # Whole-body duration (lock + read incl. delta fold + merge + serialize +
        # replace + delta subsumption prune) — emitted only after the save fully
        # COMPLETED, so a raising save never fabricates a line
        # (DEC-TELEMETRY-RECONCILE-LEDGERSAVE-001, observe-only).
        duration_s = max(tick() - started, 0.0)
        _LOG.info(
            "window_ledger.save_completed",
            rows=len(rows),
            duration_ms=duration_s * 1000.0,
        )
        threshold_s = resolve_ledger_save_slow_warn_seconds()
        if duration_s > threshold_s:
            _LOG.warning(
                "window_ledger.save_slow",
                rows=len(rows),
                duration_ms=duration_s * 1000.0,
                threshold_s=threshold_s,
            )
    except PartitionLockTimeout as exc:
        # A peer held the ledger lock past the bounded deadline (a wedged writer).
        # SKIP this save loudly — never crash the pull mid-flush: the in-memory
        # ledger is intact and the next throttled flush / pull-end save retries.
        _LOG.warning(
            "window_ledger.save_lock_timeout",
            path=str(path),
            error=str(exc),
        )
    # Completion beat (both the completed-save AND lock-timeout-skip arms; a
    # RAISING save skips it — the pull is halting anyway): a multi-second save
    # can no longer leave the watchdog's last heartbeat stranded at its entry
    # (DEC-HEAL-SCAN-LIVENESS-BEATS-001).
    emit_progress_beat()


def _migrate_legacy_sidecars(lake_root: Path, *, threshold: int) -> WindowLedger:
    """Fold the legacy ``window_status.json`` sidecar into a fresh ledger — one-shot.

    The status-log row carries bars / attempts / last_error. An ``EMPTY`` status
    at/above *threshold* migrates straight to ``SKIPPED`` (the EMPTY-terminal
    settle), mirroring :meth:`WindowLedger.record`. (The ``fetch_coverage.json``
    coverage-set fold was retired with the coverage set in DEC-WINDOW-LEDGER-001
    Stage e — only the status-log sidecar is folded now.)
    """
    from brutex.data.window_status_store import load_window_status

    by_window: dict[tuple[str, str, str, str, str], WindowLedgerRecord] = {}

    status_log = load_window_status(lake_root)
    for r in status_log.records():
        status = r.status
        if status == EMPTY and r.attempts >= threshold:
            status = SKIPPED
        by_window[(r.market, r.security_id, r.interval, r.window_start, r.window_end)] = (
            WindowLedgerRecord(
                market=r.market,
                security_id=r.security_id,
                interval=r.interval,
                window_start=r.window_start,
                window_end=r.window_end,
                status=status,
                bars=r.bars,
                attempts=r.attempts,
                last_error=r.last_error,
                first_seen_at=r.last_attempt_at,  # best available origin stamp
                last_attempt_at=r.last_attempt_at,
                # The legacy status log has no streak column; derive it (R1) so the
                # migrated ledger keeps the EMPTY-terminal behaviour the old
                # reconcile_empty_terminal gave (status-log attempts == EMPTY count).
                empty_streak=_derive_empty_streak(status, r.attempts),
                # The legacy status log has no in-pull fetch count either; derive the
                # honest floor from the (settled) status (DEC-WINDOW-INPULL-FETCHES-001).
                in_pull_fetches=_derive_in_pull_fetches(status),
                # The legacy status log never recorded a deduped net-new count —
                # honest unknown (DEC-INGEST-BOUNDARY-INTEGRITY-001).
                bars_net_new=_derive_bars_net_new(),
                # ... nor how its fetch total split between empty and transient
                # retries — honest unknown (DEC-RETRY-COUNTER-SPLIT-001).
                empty_retries=_derive_retry_count(),
                transient_retries=_derive_retry_count(),
                # ...nor the streak's per-bump lineage — honest unknown
                # (DEC-STREAK-PROVENANCE-001).
                streak_provenance=_derive_streak_provenance(),
                # ... and zero tracked failed-streak observations — the honest
                # running-counter 0 (DEC-AUTO-RESUME-ZERO-SURPRISES-001).
                failed_streak=_derive_failed_streak(),
            )
        )

    return WindowLedger.from_rows(
        (
            (
                r.market,
                r.security_id,
                r.interval,
                r.window_start,
                r.window_end,
                r.status,
                r.bars,
                r.attempts,
                r.last_error,
                r.first_seen_at,
                r.last_attempt_at,
                r.empty_streak,
                r.in_pull_fetches,
                r.bars_net_new,
                r.empty_retries,
                r.transient_retries,
                r.streak_provenance,
                r.failed_streak,
            )
            for r in by_window.values()
        ),
        threshold=threshold,
    )


def load_or_migrate_window_ledger(
    lake_root: Path,
    *,
    threshold: int = EMPTY_TERMINAL_THRESHOLD,
    now: datetime | None = None,
) -> WindowLedger:
    """Load the ledger, or seed it once from the legacy status-log sidecar (no data loss).

    Steady state: the Parquet exists → :func:`load_window_ledger`. First run after
    the unification: no Parquet → fold ``window_status.json``
    (:func:`_migrate_legacy_sidecars`). The legacy file is LEFT on disk as a
    rollback net — and since DEC-STATUS-SIDECAR-WRITE-RETIRE-001 retired its
    WRITE path it is a FROZEN pre-retirement snapshot, not a live mirror (a
    ledger loss re-migrates that snapshot; newer windows simply re-fetch,
    idempotent keep-first). Stage-6 deletes it. (The ``fetch_coverage.json``
    fold was retired with the coverage set in DEC-WINDOW-LEDGER-001 Stage e.)
    A base-less lake that HOLDS deltas (a first pull's mid-flight checkpoint that
    crashed before its pull-end save) is a LIVE ledger, not a legacy lake — it
    loads via the delta fold, never the sidecar migration
    (DEC-LEDGER-DELTA-FLUSH-001).
    """
    if ledger_path(lake_root).is_file() or list_ledger_deltas(lake_root):
        return load_window_ledger(lake_root, threshold=threshold)
    return _migrate_legacy_sidecars(lake_root, threshold=threshold)


def plan_windows(
    ledger: WindowLedger,
    *,
    instruments: Iterable[Any],
    intervals: Sequence[str],
    start: date,
    end: date,
    now: datetime | None = None,
    now_ist: datetime | None = None,
    market: str | None = None,
    window_days_for: Callable[[str], int | None] | None = None,
    symbol_windows: Mapping[str, tuple[date, date]] | None = None,
) -> int:
    """Upsert every planned window for the pull as ``PLANNED`` — returns the count.

    For each ``(instrument, interval)`` over ``[start, end)`` the canonical tiler
    (:func:`~brutex.adapters.ingest_common.tile_windows`, ONE source of truth — the
    same tiling the ingest fetches) yields the windows; each is
    :meth:`WindowLedger.plan`-ned (idempotent — a settled row is untouched). This
    is what guarantees EVERY window of the CURRENT pull's tiling has a row BEFORE
    any fetch, so a window that is never reached still shows ``PLANNED`` (never
    silently missing). SUPERSEDED trailing rows of the planned series — keys a
    previous pull's shorter ``end`` produced, which this tiling can never emit
    again — are pruned afterwards (:meth:`WindowLedger.prune_superseded`,
    DEC-WINDOW-TRAILING-GC-001), so the ledger tracks the live tiling instead of
    accumulating one orphan per series per day. O(windows) plan + one O(rows)
    prune scan, once at pull start. Generic over the instrument's
    ``exch_id``/``segment``/``symbol_name``, so it serves spot AND option series.

    CLAMP PARITY (DEC-PLAN-CLAMP-PARITY-001): the planner tiles the SAME
    settled-data bound the fetch spine does —
    :func:`~brutex.adapters.ingest_common.clamp_window_end` per interval
    (DEC-DAILY-CANDLE-CLAMP-001: daily → ``today``; intraday → ``today + 1``
    only from the 15:30 IST close). Planning the RAW ``end`` while the fetch
    clamps minted a key the fetch could never record (one permanently-PLANNED
    ghost per ``(instrument, interval)`` per pull — the operator's live 783
    pending rows), and :meth:`~WindowLedger.prune_superseded`'s shorter-end
    rule kept them (the ghost's end is LONGER than every later clamped plan).
    ``now_ist`` injects the clock for tests; the boundary race (the clamp
    moving between this plan and a fetch crossing 15:30 IST) self-heals on the
    next pull's plan→prune cycle.

    Per-symbol bounds: *symbol_windows* is an OPTIONAL
    ``{symbol_name: (start, end)}`` map (the spot twin of the option
    ``contract_windows``, type :data:`~brutex.adapters.groww.spot_bounds.SpotWindows`).
    When supplied, a SUB-DAILY interval of a symbol in the map is tiled from its
    mapped ``(start, end)`` instead of the global range — so the PLANNED rows match
    EXACTLY the windows the spine
    (:func:`~brutex.adapters.groww.run_ingest._resolve_spot_window`) actually fetches.
    The DAILY interval is NEVER bounded (it must advance to the requested end) and a
    symbol absent from the map uses the global range (the never-narrow fallback). The
    SPOT pull paths pass ``None`` since DEC-SPOT-INTERVAL-DECOUPLE-001 (MIGRATION.md
    #851 — the 1-minute fetch is fully decoupled from the 1-day coverage; both phases
    plan the full ``[start, end]`` range per symbol); the planned/fetched-key parity
    invariant holds in BOTH modes.
    """
    # Vendor-neutral tiling/clamp helpers + the (relocated-to-neutral) interval→width
    # policy + coverage market namespace all live in ingest_common (CLAUDE.md §11
    # COMMON; relocated out of the vendor package in PR-J4a — _MARKET's value unchanged).
    # Imported locally to avoid an import cycle (this module is in brutex.data, which
    # the ingest spines depend on).
    from brutex.adapters.ingest_common import (
        COVERAGE_MARKET,
        DAILY_INTERVAL,
        clamp_window_end,
        tile_windows,
    )
    from brutex.adapters.ingest_common import (
        now_ist as _now_ist,
    )
    from brutex.adapters.ingest_common import (
        window_days_for_interval as _window_days_for,
    )

    # Default to the legacy coverage namespace + tiling-width policy (back-compat); a
    # Groww spot pull passes ``market=GROWW_COVERAGE_MARKET`` AND Groww's
    # ``window_days_for`` so the PLANNED rows it seeds carry the SAME namespace AND
    # the SAME tile boundaries as the windows the Groww ingest fetches+records — the
    # "planned keys == fetched keys" invariant holds per vendor (PR-J4b, CLAUDE.md §11).
    coverage_market = market if market is not None else COVERAGE_MARKET
    width_for = window_days_for if window_days_for is not None else _window_days_for
    stamp = now if now is not None else datetime.now(UTC)
    ist = now_ist if now_ist is not None else _now_ist()
    # ONE tiling per interval (the tiler is pure in (interval, start, end)) with
    # its ISO bounds materialised once — the GC index below then shares those
    # exact strings instead of re-serialising per (instrument × window). The end
    # bound is CLAMPED exactly as `ingest_spot` clamps it (including the
    # start < clamped_end empty-range guard), so planned keys == fetched keys.
    clamped_ends: dict[str, date] = {
        interval: clamp_window_end(interval, end, now_ist=ist) for interval in intervals
    }
    # GLOBAL (unbounded) tiles per interval — the common case AND the fallback for any
    # symbol the caller did not bound. The tiler is pure in (interval, start, end), so
    # these are computed ONCE and shared by every unbounded (symbol, interval); the iso
    # form (the prune index's value) is shared by-reference too, unchanged from before.
    global_tiles: dict[str, list[tuple[date, date]]] = {
        interval: (
            tile_windows(interval, start, clamped, window_days_for=width_for)
            if start < clamped
            else []
        )
        for interval, clamped in clamped_ends.items()
    }
    global_tiles_iso: dict[str, dict[str, str]] = {
        interval: {ws.isoformat(): we.isoformat() for ws, we in tiles}
        for interval, tiles in global_tiles.items()
    }

    def _tiles_for(symbol: str, interval: str) -> tuple[list[tuple[date, date]], Mapping[str, str]]:
        """Return (tiles, iso) for one (symbol, interval) — bounded for a 1-day-first symbol.

        DEC-SPOT-1DAY-FIRST-001: when *symbol_windows* bounds this symbol's SUB-DAILY
        pull, tile from its discovered ``(start, end)`` — clamped IDENTICALLY to the
        spine (:func:`clamp_window_end`) so the planned keys equal the fetched keys — and
        build a FRESH iso map for it. The DAILY interval + any unbounded symbol reuse the
        shared global tiles + their shared iso object (O(1) memory, byte-identical to the
        pre-feature path).
        """
        if symbol_windows is not None and interval != DAILY_INTERVAL and symbol in symbol_windows:
            sym_start, sym_end = symbol_windows[symbol]
            clamped = clamp_window_end(interval, sym_end, now_ist=ist)
            tiles = (
                tile_windows(interval, sym_start, clamped, window_days_for=width_for)
                if sym_start < clamped
                else []
            )
            return tiles, {ws.isoformat(): we.isoformat() for ws, we in tiles}
        return global_tiles[interval], global_tiles_iso[interval]

    planned_tiles: dict[tuple[str, str, str], Mapping[str, str]] = {}
    planned = 0
    for instrument in instruments:
        symbol = instrument.symbol_name
        for interval in intervals:
            tiles, tiles_iso = _tiles_for(symbol, interval)
            planned_tiles[(coverage_market, symbol, interval)] = tiles_iso
            for window_start, window_end in tiles:
                ledger.plan(
                    WindowKey(
                        market=coverage_market,
                        security_id=symbol,
                        interval=interval,
                        start=window_start,
                        end=window_end,
                    ),
                    now=stamp,
                )
                planned += 1
    pruned = ledger.prune_superseded(planned_tiles)
    if pruned:
        _LOG.info("window_ledger.pruned_superseded", pruned=pruned, planned=planned)
    return planned


#: The shape a lake-span index maps: ``(exchange, segment, symbol, interval) -> span``.
#: The span dict carries ``bars`` / ``first_date`` / ``last_date`` (the WHAT-is-stored
#: aggregate from :func:`~brutex.dashboard.db_view.summarize_spot_datasets`, which lists
#: the WHOLE ``bars/`` tree — spot AND ``segment="FNO"`` contract partitions alike),
#: plus an OPTIONAL ``session_dates`` frozenset on the opted-in dense INDEX ``1minute``
#: spans (DEC-COLDSTART-SEED-INTERIOR-GAP-001 — the seed's density cross-check input).
LakeSpanIndex = dict[tuple[str, str, str, str], dict[str, Any]]

#: The lake segments whose SPOT cold-start spans carry ``session_dates`` for the
#: interior-gap density cross-check (DEC-COLDSTART-SEED-INTERIOR-GAP-001): the DENSE
#: index partitions only — the literal matches the on-disk partition dir name +
#: ``Segment.INDEX.value`` (brutex/data/partition.py ``VALID_SEGMENTS``), mirroring
#: DEC-INTERIOR-GAP-DETECT-001's dense-``is_index`` demote scope. CASH reference
#: equities keep span-only seeding (sparse-but-healthy names must never false-flag);
#: the F&O cold-start reads its spans WITHOUT session dates (a deliberate non-goal).
_SESSION_DATE_SEGMENTS: Final[frozenset[str]] = frozenset({"INDEX"})

#: One reconcile candidate: the ledger window to seed + the span-index lookup key whose
#: stored ``(first_date, last_date)`` must ENCLOSE the window for the seed to fire.
SeedCandidate = tuple[WindowKey, tuple[str, str, str, str]]


def index_lake_spans(spans: Sequence[dict[str, Any]]) -> LakeSpanIndex:
    """Build the ``(exchange, segment, symbol, interval) -> span`` lookup — the COMMON index.

    Shared by BOTH the spot (:func:`reconcile_ledger_windows`) and the F&O
    (:func:`~brutex.adapters.groww.option_ingest.reconcile_fno_ledger_with_lake`)
    cold-start seeds so the two never drift on how a lake span is keyed (CLAUDE.md §11
    COMMON). *spans* is the :func:`~brutex.dashboard.coverage_reconcile.load_lake_spans`
    shape — one aggregate row per ``bars/`` partition set (spot AND ``segment="FNO"``
    contract partitions alike), so ONE span read feeds both reconciles.
    """
    return {
        (str(s["exchange"]), str(s["segment"]), str(s["symbol"]), str(s["interval"])): s
        for s in spans
    }


def seed_enclosed_windows(
    ledger: WindowLedger,
    span_index: LakeSpanIndex,
    candidates: Iterable[SeedCandidate],
    *,
    now: datetime,
    today_ist: date | None = None,
    observed: AbstractSet[date] = frozenset(),
) -> int:
    """Seed ``SUCCESS`` (only-if-absent) for each candidate window the lake ENCLOSES — the COMMON core.

    The single reconcile engine shared by the spot + F&O cold-starts (CLAUDE.md §11
    COMMON). For each ``(window_key, span_key)`` candidate it looks up the lake span,
    and — iff the span has ≥ 1 bar and its stored ``[first_date, last_date]`` FULLY
    encloses the window — seeds the window ``SUCCESS`` via
    :meth:`~brutex.data.window_ledger.WindowLedger.seed_lake_success` (which NEVER
    overwrites an existing ``FAILED`` / ``PLANNED`` / settled row, so a genuinely-missing
    or partially-covered window is left re-pullable). Returns the number of windows newly
    seeded. Pure over *span_index* + *candidates*; O(candidates) with O(1) lookups.

    **Enclosure rule (DEC-WINDOW-LEDGER-RESCUE-001, fixing the DEC-LAKE-RECONCILE-001
    off-by-one):** a window key's ``end`` is fetch-EXCLUSIVE (the last requested bar
    is on ``end − 1 day``) while the lake span's ``last_date`` is bar-INCLUSIVE, so
    the test is ``first_date <= window.start AND (window.end − 1 day) <= last_date``.
    The prior ``window.end <= last_date`` form compared an exclusive bound against an
    inclusive one — for a default-window F&O contract (``end = expiry + 1 day``,
    stored bars end ON expiry) it was ``expiry + 1 <= expiry``: ALWAYS false, so the
    F&O rescue deterministically seeded 0 windows even over a maximally dense lake.
    Boundary: ``end − 1 == last_date`` seeds; ``end − 1 == last_date + 1`` does not.

    **Honesty — this fix GROWS the span-masking surface from ~zero to live (Golden
    Rule #10):** pre-fix the rescue was largely DEAD, not merely boundary-drifted —
    the F&O reconcile seeded 0 windows EVER, and on a DEFAULT (floor → today) spot
    pull the daily whole-range window and the trailing sub-daily window were equally
    dead (their exclusive ``end`` sits at today while the storable ``last_date`` is
    at most yesterday); only interior sub-daily tiles (and historical sub-range
    pulls) ever seeded. Now that the rescue is LIVE, remember it is a SPAN test, not
    a DENSITY test — EXCEPT for the one surface below. The load-bearing safety
    stands: seeding is strictly ONLY-IF-ABSENT
    (:meth:`~brutex.data.window_ledger.WindowLedger.seed_lake_success`), so a
    ``FAILED`` / ``EMPTY`` / ``PLANNED`` row is never masked.

    **Interior-gap continuity gate (DEC-COLDSTART-SEED-INTERIOR-GAP-001, upgraded
    DATASET-wide by DEC-DATA-HONESTY-ALLFILTERED-SEED-001):** a span test alone
    would stamp ``SUCCESS`` over a window whose lake span contains
    an interior multi-session hole — and since ``SUCCESS`` is skippable, the hole
    would be masked FOREVER on a lost-ledger cold-start (the spot /completeness
    verdict is LEDGER-backed, so it would read ``full``; the fetch-time detector
    of DEC-INTERIOR-GAP-DETECT-001 never sees a seeded window). So when a span
    entry carries ``session_dates`` (attached ONLY for the dense INDEX ``1minute``
    spans — :data:`_SESSION_DATE_SEGMENTS` via
    :func:`reconcile_ledger_with_lake`) AND the candidate window's interval is
    the FETCHED spot granularity (``TRACK1_SPOT_INTERVAL`` — belt-and-braces with
    the read-side gate: a DERIVED ``1day`` window is never density-checked, or it
    could sit permanently un-seedable since it is never fetched), the DATASET's
    confirmed >tolerance bar-less runs are computed ONCE per span by the COMMON
    :func:`~brutex.data.interior_gap_detect.interior_gap_runs` (one source of
    interior-hole truth + ONE tolerance knob — CLAUDE.md §11) and a window
    INTERSECTING a confirmed run is REFUSED with one
    ``window_ledger.seed_interior_gap_skip`` WARNING carrying the run's
    ``gap_first``/``gap_last`` — the window stays absent → ``PLANNED`` this pull
    → fetched → the fetch-time detectors bound it (``record(degraded=True)``,
    re-pullable then terminal). DATASET-wide runs (not the original per-window
    classification) deliberately close the two documented per-window blind
    spots: a >tolerance hole STRADDLING a window boundary (two sub-tolerance
    leading/trailing edges per window under the old check) now refuses BOTH
    windows, and a window ENTIRELY inside a giant hole (zero in-range dates —
    the old check could only abstain ``too_few_sessions``) is refused too. A
    long-existing row is never re-warned: :meth:`~brutex.data.window_ledger.
    WindowLedger.has_window` short-circuits BEFORE the gate (the only-if-absent
    seed would no-op anyway). FAIL-CLOSED: an error computing the runs logs
    ``window_ledger.seed_interior_gap_error`` per window and skips the seed (a
    re-pull is always data-safe — idempotent keep-first sink), never a crash.
    Honest residuals: CASH reference equities + the F&O reconcile stay span-only
    (their spans carry no ``session_dates``); an out-of-calendar-coverage span
    honestly ABSTAINS dataset-wide (seeded — consistent with the fetch-time
    detector; note the abstain now keys on the DATASET's first/last covered
    dates, so a span reaching past holiday coverage abstains for ALL its
    windows — conservative in the seed direction, byte-identical to the
    span-only path); the seed threads the DEC-CALENDAR-OBSERVED-SESSIONS-001
    *observed* drift overlay into :func:`interior_gap_runs` — CLOSED by
    DEC-SEED-OBSERVED-OVERLAY-001 (previously the reconcile chokepoints had no
    overlay plumbed, so on a vendor-observed holiday the seed counted one more
    bar-less "session" than the fetch-time classifier and refused a window the
    classifier called continuous — one redundant vendor re-fetch per cold
    start); the caller HOISTS its ONE per-pull ``load_observed_holidays`` read
    above the reconcile so the seed and the fetch-time detectors share ONE
    consistent snapshot (single source of truth; the shared run-walker breaks
    a run on every COVERED static session BEFORE consulting the overlay, so
    threading the remove-only overlay can only SHORTEN runs — the seed
    becomes strictly MORE permissive, never newly refusing, for EVERY overlay
    shape: a STALE entry over a since-backfilled covered day — reachable,
    because an entry self-heals only when ``brutex detect-session-drift``
    next re-runs, NOT the moment bars appear — can no longer MERGE its two
    flanking runs into a super-tolerance refusal; the property-pinned
    monotonicity law, pinned WITHOUT a disjointness restriction);
    sub-tolerance holes still seed; and CONVERGENCE after a
    refusal is fetch-verdict-dependent — a run CONTAINED in one window is
    demoted at fetch time (``INTERIOR_GAP``, bounded → SKIPPED), while a
    STRADDLING run's halves read as edge holes per fetched window (the
    trailing-truncation detector may demote the leading half; the trailing-edge
    half of the NEXT window re-records what the vendor returns) — either way a
    REAL vendor fetch re-verifies the state instead of a blind span stamp.

    **Live-edge guard:** a window whose coverage INCLUDES the current IST day
    (``end − 1 >= today_ist``) is NEVER seeded — the day's session data may still be
    partial (a mid-session store puts a bar on ``today`` without completing it), and a
    span-level check cannot tell partial from complete, so the trailing live-edge
    window is conservatively left to the fetch (idempotent; no data ever masked).
    *today_ist* defaults to *now* converted to the IST calendar day; the F&O reconcile
    injects its clamp clock's date so reconcile == plan == fetch stays deterministic.
    """
    # The fetched spot granularity — the ONE interval the density gate may fire on
    # (REVISION 1 of DEC-COLDSTART-SEED-INTERIOR-GAP-001: a derived `1day` window is
    # never fetched, so un-seeding it could never self-heal). Imported locally,
    # mirroring the module's ingest_common pattern (brutex.data must not import the
    # vendor package at module import time).
    from brutex.adapters.groww.constants import TRACK1_SPOT_INTERVAL

    if today_ist is None:
        today_ist = now.astimezone(IST).date()
    seeded = 0
    # Per-span memo of the DATASET-wide confirmed interior-hole runs
    # (DEC-DATA-HONESTY-ALLFILTERED-SEED-001): computed ONCE per gated span per
    # call — `None` = calendar abstain (gate inert), `()` = no confirmed run, an
    # Exception = the fail-CLOSED sentinel (every window of that span refuses the
    # seed, one error WARNING each — a bad density signal must never mint SUCCESS).
    runs_by_span: dict[
        tuple[str, str, str, str],
        tuple[tuple[date, date], ...] | None | Exception,
    ] = {}
    for window_key, span_key in candidates:
        entry = span_index.get(span_key)
        if (
            entry is None
            or int(entry["bars"]) <= 0
            or entry["first_date"] is None
            or entry["last_date"] is None
        ):
            continue
        first_date = date.fromisoformat(str(entry["first_date"]))
        last_date = date.fromisoformat(str(entry["last_date"]))
        last_covered = window_key.end - timedelta(days=1)  # end is fetch-EXCLUSIVE
        if last_covered >= today_ist:
            continue  # live edge — today's data may be partial; never seed it settled
        if not (first_date <= window_key.start and last_covered <= last_date):
            continue
        # Continuity gate (DEC-COLDSTART-SEED-INTERIOR-GAP-001, upgraded DATASET-wide
        # by DEC-DATA-HONESTY-ALLFILTERED-SEED-001 — docstring above): only a
        # FETCHED-interval window whose span carries `session_dates` is checked —
        # every other candidate (F&O / CASH / derived `1day` / legacy span shape)
        # takes the historical span-only path byte-identically. The confirmed runs
        # are computed over the WHOLE dataset's covered dates (not the window's
        # in-range slice), then each window is refused iff it INTERSECTS a run —
        # so a >tolerance hole STRADDLING a window boundary refuses BOTH windows,
        # and a window ENTIRELY inside a giant hole (zero in-range dates, which a
        # per-window classifier could only abstain on) is refused too.
        if window_key.interval == TRACK1_SPOT_INTERVAL:
            sessions = entry.get("session_dates")
            if sessions is not None:
                if ledger.has_window(window_key):
                    # An existing row (ANY status) makes the only-if-absent seed
                    # below a no-op, so the density check + refusal WARNING are
                    # skipped outright — a long-settled window must not re-WARN
                    # on every steady-state reconcile
                    # (DEC-DATA-HONESTY-ALLFILTERED-SEED-001, refuter F-4 fold;
                    # the seeded count is unchanged — `seed_lake_success`
                    # returned False for this row before). The ungated span-only
                    # path below stays byte-identical (its only-if-absent probe
                    # lives inside `seed_lake_success` as it always did).
                    continue
                if span_key not in runs_by_span:
                    try:
                        # SAME per-pull *observed* snapshot the fetch-time detectors
                        # read (DEC-SEED-OBSERVED-OVERLAY-001) — the memo stays valid
                        # because *observed* is constant per call.
                        runs_by_span[span_key] = interior_gap_runs(
                            covered_dates=sessions, observed=observed
                        )
                    except Exception as exc:  # fail-CLOSED — a re-pull is always data-safe
                        runs_by_span[span_key] = exc
                runs = runs_by_span[span_key]
                if isinstance(runs, Exception):
                    _LOG.warning(
                        "window_ledger.seed_interior_gap_error",
                        span_key=span_key,
                        window_start=window_key.start.isoformat(),
                        window_end=window_key.end.isoformat(),
                        error=str(runs),
                    )
                    continue
                if runs:
                    hit = next(
                        (r for r in runs if r[0] <= last_covered and window_key.start <= r[1]),
                        None,
                    )
                    if hit is not None:
                        # REFUSE the seed: this enclosed window intersects a
                        # CONFIRMED >tolerance interior hole — span-stamping it
                        # SUCCESS would make the hole PERMANENT on a lost-ledger
                        # cold start. The window is left ABSENT → PLANNED-able →
                        # fetched; a refusal touches NO existing ledger row
                        # (property-pinned).
                        _LOG.warning(
                            "window_ledger.seed_interior_gap_skip",
                            span_key=span_key,
                            window_start=window_key.start.isoformat(),
                            window_end=window_key.end.isoformat(),
                            gap_first=hit[0].isoformat(),
                            gap_last=hit[1].isoformat(),
                        )
                        continue
        if ledger.seed_lake_success(window_key, bars=int(entry["bars"]), now=now):
            seeded += 1
    return seeded


def reconcile_ledger_windows(
    ledger: WindowLedger,
    spans: Sequence[dict[str, Any]],
    *,
    instruments: Iterable[Any],
    intervals: Sequence[str],
    start: date,
    end: date,
    now: datetime | None = None,
    market: str | None = None,
    window_days_for: Callable[[str], int | None] | None = None,
    symbol_windows: Mapping[str, tuple[date, date]] | None = None,
    observed: AbstractSet[date] = frozenset(),
) -> int:
    """Seed ``SUCCESS`` for every lake-enclosed window with no ledger row — **pure** (cold-start).

    DEC-WINDOW-LEDGER-001. On a fresh clone / lost ledger the lake's Parquet holds
    bars but the ledger is empty, so once the ledger drives the skip a re-pull would
    re-fetch EVERYTHING (the rescue the old ``reconcile_coverage_with_lake`` gave the
    coverage set). For each ``(instrument, interval)`` whose stored span ENCLOSES a
    planned window (``first_date <= window_start AND window_end − 1 day <=
    last_date``, span has ≥ 1 bar — the fetch-exclusive-end enclosure rule of
    DEC-WINDOW-LEDGER-RESCUE-001, plus the live-edge guard; see
    :func:`seed_enclosed_windows`), seed that window
    ``SUCCESS`` — but ONLY if ABSENT (:meth:`WindowLedger.seed_lake_success` NEVER
    overwrites a ``FAILED`` / ``PLANNED`` / settled row, so it can't mask a re-pull).
    *spans* is the :func:`~brutex.dashboard.coverage_reconcile.load_lake_spans`
    shape. O(windows) once at pull start. Returns the number of windows seeded.

    1-DAY-FIRST per-symbol bounds (DEC-SPOT-1DAY-FIRST-001): *symbol_windows* mirrors
    :func:`plan_windows` — a SUB-DAILY interval of a bounded symbol is tiled from its
    discovered 1-day-first ``(start, end)`` so the cold-start seed keys EQUAL the
    bounded planned keys. Without this, a global-tiled seed (anchored at the 2020 floor)
    would land a SUCCESS row at a window boundary the bounded plan never created — an
    ORPHAN row the completeness page would miscount. ``None`` (the default) tiles the
    global range, byte-identical to the pre-feature behaviour.

    *observed* (DEC-SEED-OBSERVED-OVERLAY-001): the per-pull observed-sessions
    drift overlay (``load_observed_holidays``), threaded into the seed's
    interior-gap continuity gate so the seed and the fetch-time detectors judge
    the SAME calendar (see :func:`seed_enclosed_windows`). ``frozenset()`` (the
    default) is byte-identical to the pre-threading behaviour.
    """
    # Shared vendor-neutral tiler + the (relocated-to-neutral) width policy +
    # coverage market namespace, all in ingest_common (CLAUDE.md §11 COMMON; moved
    # out of the vendor package in PR-J4a — _MARKET's value unchanged). Local import
    # avoids the brutex.data ↔ ingest spine import cycle.
    from brutex.adapters.ingest_common import (
        COVERAGE_MARKET,
        DAILY_INTERVAL,
        tile_windows,
    )
    from brutex.adapters.ingest_common import (
        window_days_for_interval as _window_days_for,
    )

    # Default to the legacy namespace + tiling width (back-compat); the Groww spot
    # pull passes ``market=GROWW_COVERAGE_MARKET`` AND Groww's ``window_days_for`` so
    # cold-start SUCCESS seeds match the namespace + tile boundaries the Groww ingest
    # records (PR-J4b, CLAUDE.md §11 COMMON).
    coverage_market = market if market is not None else COVERAGE_MARKET
    width_for = window_days_for if window_days_for is not None else _window_days_for
    stamp = now if now is not None else datetime.now(UTC)
    span_index = index_lake_spans(spans)

    def _reconcile_tiles(symbol: str, interval: str) -> list[tuple[date, date]]:
        """Tile one (symbol, interval) — bounded to the symbol's 1-day-first window.

        DEC-SPOT-1DAY-FIRST-001: a bounded SUB-DAILY symbol tiles from its discovered
        ``(start, end)`` (the SAME anchor :func:`plan_windows` uses), so a seeded SUCCESS
        key matches a planned key exactly; every other (symbol, interval) tiles the global
        range (unchanged).
        """
        if symbol_windows is not None and interval != DAILY_INTERVAL and symbol in symbol_windows:
            sym_start, sym_end = symbol_windows[symbol]
            return tile_windows(interval, sym_start, sym_end, window_days_for=width_for)
        return tile_windows(interval, start, end, window_days_for=width_for)

    def _candidates() -> Iterable[SeedCandidate]:
        for instrument in instruments:
            for interval in intervals:
                span_key = (
                    instrument.exch_id.value,
                    instrument.segment.value,
                    instrument.symbol_name,
                    interval,
                )
                for window_start, window_end in _reconcile_tiles(instrument.symbol_name, interval):
                    yield (
                        WindowKey(
                            market=coverage_market,
                            security_id=instrument.symbol_name,
                            interval=interval,
                            start=window_start,
                            end=window_end,
                        ),
                        span_key,
                    )

    return seed_enclosed_windows(ledger, span_index, _candidates(), now=stamp, observed=observed)


def reconcile_ledger_with_lake(
    ledger: WindowLedger,
    lake_root: Path,
    *,
    instruments: Iterable[Any],
    intervals: Sequence[str],
    start: date,
    end: date,
    now: datetime | None = None,
    market: str | None = None,
    window_days_for: Callable[[str], int | None] | None = None,
    symbol_windows: Mapping[str, tuple[date, date]] | None = None,
    observed: AbstractSet[date] = frozenset(),
) -> int:
    """Load the lake spans + seed the ledger cold-start (the wired entry point).

    Thin composition of :func:`~brutex.dashboard.coverage_reconcile.load_lake_spans`
    (the DuckDB span read) + :func:`reconcile_ledger_windows` (pure). Called once at
    pull start so a fresh-clone ledger inherits the lake's already-stored windows as
    ``SUCCESS`` — no full re-pull / quota blowout once the ledger drives skip.
    O(datasets) aggregate reads, never per-window.

    This SPOT wrapper requests ``session_dates`` for the dense INDEX spans
    (:data:`_SESSION_DATE_SEGMENTS` — DEC-COLDSTART-SEED-INTERIOR-GAP-001) so
    :func:`seed_enclosed_windows` can density-cross-check the fetched ``1minute``
    windows for an interior multi-session hole before span-seeding ``SUCCESS``;
    the F&O cold-start (:func:`~brutex.adapters.groww.option_ingest.
    reconcile_fno_ledger_with_lake`) reads its spans WITHOUT session dates and is
    byte-identical. Cost: one O(sessions) DISTINCT-date scan per index dataset,
    once at pull start — never the hot /coverage or /completeness poll.
    """
    from brutex.dashboard.coverage_reconcile import load_lake_spans

    spans = load_lake_spans(lake_root, session_date_segments=_SESSION_DATE_SEGMENTS)
    return reconcile_ledger_windows(
        ledger,
        spans,
        instruments=instruments,
        intervals=intervals,
        start=start,
        end=end,
        now=now,
        market=market,
        window_days_for=window_days_for,
        symbol_windows=symbol_windows,
        observed=observed,
    )


#: Adaptive-throttle duty factor (DEC-OPTION-WINDOW-PLAN-001, hostile C2): the
#: live flush waits at least ``_FLUSH_DUTY_FACTOR × last_save_duration`` between
#: writes, so flushing can never consume more than ~1/factor of wall time. The
#: review measured an 841k-row ledger save at ~3.94 s — ABOVE the fixed 2 s
#: throttle, which degenerated to flush-per-tick (a 12.5k-outcome drain would
#: have spent ~13.7 h rewriting Parquet). Factor 5 caps flush overhead at ~20%.
_FLUSH_DUTY_FACTOR: float = 5.0


def make_live_ledger_flush(
    ledger: WindowLedger | None,
    lake_root: Path,
    *,
    min_interval_s: float = LIVE_FLUSH_MIN_INTERVAL_S,
    clock: Any = None,
) -> Any:
    """Return a THROTTLED mid-pull ledger-flush callback (live /db updates).

    During a long pull every window's outcome accumulates in *ledger* in memory;
    this persists it MID-pull (throttled) so /db + /coverage/windows tick up live
    instead of jumping only at pull end. A ``None`` *ledger* (dry-run) yields a
    no-op callable. *clock* / *min_interval_s* are injectable for deterministic
    tests; O(dirty) only on the calls that actually write.

    INCREMENTAL since DEC-LEDGER-DELTA-FLUSH-001: each eligible fire calls
    :func:`append_ledger_delta` — an O(dirty-rows) append of ONLY the rows
    changed since the last flush — instead of :func:`save_window_ledger`'s O(N)
    whole-file read-merge-write (~4 s/save measured on a ~841k-row pre-planned
    universe ledger, the very cost the adaptive throttle existed to absorb). The
    pull-end :func:`end_pull_ledger` still runs the FULL merge-save, which
    subsumes + prunes the deltas.

    The throttle is ADAPTIVE (DEC-OPTION-WINDOW-PLAN-001, hostile C2): the
    effective interval is ``max(min_interval_s, _FLUSH_DUTY_FACTOR ×
    last_flush_duration)``, measured with the same injectable *clock* — it now
    measures the DELTA append (the actual work), so a cheap delta keeps the
    snappy fixed cadence by construction.

    **Pacing telemetry (DEC-TELEMETRY-RECONCILE-LEDGERSAVE-001, observe-only):** the
    measured flush duration was already driving the adaptive throttle but was never
    logged; each eligible fire (an actual write attempt — since
    DEC-LEDGER-DELTA-FLUSH-001 the :func:`append_ledger_delta` call) now emits ONE
    ``window_ledger.flush_paced`` DEBUG carrying the measured ``duration_ms`` plus the
    ``next_interval_s`` the throttle derives from it — logging is the ONLY delta (same
    flush calls, same throttle decisions, same measured values).
    """
    from time import monotonic

    tick = clock if clock is not None else monotonic
    last_write: list[float | None] = [None]
    #: Seconds the LAST delta append took — drives the adaptive interval.
    last_duration: list[float] = [0.0]

    def _flush() -> None:
        if ledger is None:
            return
        now = tick()
        interval = max(min_interval_s, _FLUSH_DUTY_FACTOR * last_duration[0])
        if last_write[0] is not None and now - last_write[0] < interval:
            return
        last_write[0] = now
        append_ledger_delta(ledger, lake_root)
        last_duration[0] = max(tick() - now, 0.0)
        # Flush pacing (observe-only): the EXACT duration the throttle just measured +
        # the adaptive interval it implies for the NEXT flush — visible at DEBUG so a
        # self-paced (slow-save) flush cadence is diagnosable from the log alone.
        _LOG.debug(
            "window_ledger.flush_paced",
            duration_ms=last_duration[0] * 1000.0,
            next_interval_s=max(min_interval_s, _FLUSH_DUTY_FACTOR * last_duration[0]),
        )

    return _flush


def make_locked_flush(flush: Callable[[], None]) -> Callable[[], None]:
    """Serialise concurrent fires of *flush* behind one lock (thread-safe checkpoint).

    The ingest engine fires its per-window ``on_window`` hook from PARALLEL worker
    threads (:class:`~brutex.orchestrator.parallel.PoolRunParallel`), but the
    per-pull ledger flush (:func:`make_live_ledger_flush`) — though throttled — is
    NOT thread-safe: two threads writing the ledger Parquet at once could clobber a
    half-written temp file. This wraps any 0-arg *flush* so the writes serialise,
    single-flight, under one lock; the wrapped flush's own throttle still bounds the
    write RATE (the lock only bounds CONCURRENCY). O(1) lock acquire per fire.

    The dashboard's :func:`~brutex.dashboard.pull_controller.make_live_sync` keeps
    its OWN lock because that critical section also reads ``ledger.progress_counts``
    atomically with the flush for the live bar push; this helper is the minimal
    "just make a flush thread-safe" primitive for the flush-only CLI checkpoint
    (CLAUDE.md §11 COMMON — one lock-wrap primitive, not copy-pasted per caller).
    """
    lock = threading.Lock()

    def _locked() -> None:
        with lock:
            flush()

    return _locked


#: The round-robin skipped-audit cursor sidecar (DEC-SKIPPED-AUDIT-ROUNDROBIN-001)
#: — a tiny JSON next to the ledger, NOT a new ledger Parquet column (the fixed
#: ``_COLUMNS`` schema + the cross-process merge rank stay byte-untouched).
SKIPPED_AUDIT_CURSOR_FILENAME: str = "skipped_audit_cursor.json"

#: The 5-field window identity arity the cursor key must carry.
_CURSOR_KEY_ARITY: int = 5


def skipped_audit_cursor_path(lake_root: Path) -> Path:
    """The audit-cursor sidecar path (``<lake_root>/window_ledger/skipped_audit_cursor.json``)."""
    return ledger_dir(lake_root) / SKIPPED_AUDIT_CURSOR_FILENAME


def load_skipped_audit_cursor(
    lake_root: Path,
) -> tuple[str, tuple[str, str, str, str, str] | None] | None:
    """Read the persisted audit cursor — ``(last_audit_pull_id, cursor_key)`` or ``None``.

    TOLERANT by contract (the audit is best-effort — a cursor problem must never
    block a pull): a missing / unreadable / malformed / wrong-shaped sidecar
    logs ``window_ledger.skipped_audit_cursor_invalid`` (missing is silent) and
    reads as ``None`` — the next audit simply restarts the rotation from the
    head, which is safe (re-opening is always DATA-SAFE).
    """
    import json

    path = skipped_audit_cursor_path(lake_root)
    try:
        payload = json.loads(read_regular_text(path))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _LOG.warning("window_ledger.skipped_audit_cursor_invalid", path=str(path), error=str(exc))
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("last_audit_pull_id"), str):
        _LOG.warning(
            "window_ledger.skipped_audit_cursor_invalid", path=str(path), error="wrong shape"
        )
        return None
    raw_key = payload.get("cursor_key")
    if raw_key is None:
        return str(payload["last_audit_pull_id"]), None
    if (
        isinstance(raw_key, list)
        and len(raw_key) == _CURSOR_KEY_ARITY
        and all(isinstance(part, str) for part in raw_key)
    ):
        key = (str(raw_key[0]), str(raw_key[1]), str(raw_key[2]), str(raw_key[3]), str(raw_key[4]))
        return str(payload["last_audit_pull_id"]), key
    _LOG.warning(
        "window_ledger.skipped_audit_cursor_invalid", path=str(path), error="wrong key shape"
    )
    return None


def save_skipped_audit_cursor(
    lake_root: Path,
    *,
    pull_id: str,
    cursor_key: tuple[str, str, str, str, str] | None,
) -> None:
    """Persist the audit cursor — best-effort, atomic, NEVER raises.

    Uses the guard-free :func:`~brutex.data.atomic_io.atomic_write_simple`
    (DELIBERATELY not the lake-guarded path: a cursor write failure — even a
    disk-full — must never :class:`~brutex.data.errors.LakeWriteHalt` a pull;
    the cursor is a convenience, the audit restarts from the head without it).
    An :class:`OSError` logs ``window_ledger.skipped_audit_cursor_save_failed``.
    """
    import json

    from brutex.data.atomic_io import atomic_write_simple

    path = skipped_audit_cursor_path(lake_root)
    body = json.dumps(
        {
            "last_audit_pull_id": pull_id,
            "cursor_key": list(cursor_key) if cursor_key is not None else None,
        },
        sort_keys=True,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_simple(path, lambda tmp: tmp.write_text(body, encoding="utf-8"))
    except OSError as exc:
        _LOG.warning(
            "window_ledger.skipped_audit_cursor_save_failed", path=str(path), error=str(exc)
        )


def run_skipped_audit(
    ledger: WindowLedger,
    lake_root: Path,
    *,
    pull_id: str,
    cap: int = SKIPPED_AUDIT_PER_PULL,
) -> int:
    """Mechanized round-robin re-audit of ≤ *cap* terminally-``SKIPPED`` windows — once per pull.

    DEC-SKIPPED-AUDIT-ROUNDROBIN-001. Loads the sidecar cursor; a matching
    ``last_audit_pull_id`` short-circuits to 0 (idempotent re-entry — the audit
    fires AT MOST ONCE per pull, Golden Rule #7); otherwise
    :meth:`~brutex.data.window_ledger.WindowLedger.audit_reopen_skipped` demotes
    the next *cap* rows in rotation order (wrapping at the tail) on the pull's
    OWN in-memory ledger — so the ``reopened_keys`` tombstones flow into the
    pull-end :func:`save_window_ledger` merge and the demotions persist. The new
    cursor (the last key selected; the PRIOR cursor when nothing was selected)
    is saved best-effort. Logs ONE ``pull.skipped_audit`` naming the count +
    cursor. Budget honesty lives on
    :data:`~brutex.data.window_ledger.SKIPPED_AUDIT_PER_PULL`.

    **Crash-window honesty (P2-3, Golden Rule #10):** the advanced cursor is
    persisted here — BEFORE the pull-end :func:`save_window_ledger` persists
    the in-memory demotions. A pull that crashes between the two therefore
    advances the rotation while its ≤ *cap* demotions are LOST: those windows
    stay ``SKIPPED`` and wait one full rotation cycle for their next re-audit.
    Best-effort by design and DATA-SAFE (nothing was fetched or stored on
    their behalf; a later rotation pass reaches them again) — deliberately NOT
    inverted, since the cursor save must not ride the pull-end save's
    lock/halt paths.
    """
    prior = load_skipped_audit_cursor(lake_root)
    if prior is not None and prior[0] == pull_id:
        _LOG.debug("pull.skipped_audit_already_ran", pull_id=pull_id)
        return 0
    start_after = prior[1] if prior is not None else None
    reopened, last_key = ledger.audit_reopen_skipped(start_after=start_after, cap=cap)
    save_skipped_audit_cursor(
        lake_root,
        pull_id=pull_id,
        cursor_key=last_key if last_key is not None else start_after,
    )
    _LOG.info(
        "pull.skipped_audit",
        pull_id=pull_id,
        reopened=reopened,
        cap=cap,
        cursor=list(last_key) if last_key is not None else None,
    )
    return reopened


def run_skipped_audit_guarded(
    ledger: WindowLedger | None,
    lake_root: Path,
    *,
    pull_id: str,
    cap: int = SKIPPED_AUDIT_PER_PULL,
) -> int:
    """Best-effort :func:`run_skipped_audit` — a ``None`` ledger / any error is a 0 no-op.

    The pull-seam wrapper (the #935/#1014 guarded-wiring precedent): a dry-run
    threads ``ledger=None`` (no audit — nothing persists on a dry-run) and ANY
    internal error logs ``pull.skipped_audit_error`` and returns 0, so the
    mechanized audit can never block, slow, or crash a pull.
    """
    if ledger is None:
        return 0
    try:
        return run_skipped_audit(ledger, lake_root, pull_id=pull_id, cap=cap)
    except Exception as exc:  # pylint: disable=broad-exception-caught — best-effort seam
        _LOG.warning("pull.skipped_audit_error", pull_id=pull_id, error=str(exc))
        return 0


#: The ACTIVE shared-pull-ledger scope (DEC-LEDGER-LOAD-SPACE-V1-001): ``None``
#: outside any scope (every :func:`begin_pull_ledger` loads fresh — the
#: pre-existing behaviour, byte-identical); inside a
#: :func:`shared_pull_ledger_scope` it holds one ``{str(lake_root) →
#: WindowLedger}`` reuse map. A :class:`~contextvars.ContextVar` (the
#: ``pull_id_scope`` correlation precedent, NOT module-global mutable state):
#: the scope is confined to the opening thread/context, so an unrelated thread's
#: ``begin_pull_ledger`` can never observe another pipeline's live instance.
_SHARED_PULL_LEDGER: contextvars.ContextVar[dict[str, WindowLedger] | None] = (
    contextvars.ContextVar("brutex_shared_pull_ledger", default=None)
)


@contextmanager
def shared_pull_ledger_scope() -> Generator[None, None, None]:
    """Reuse ONE loaded ledger per lake across this scope's :func:`begin_pull_ledger` calls.

    DEC-LEDGER-LOAD-SPACE-V1-001. A full-pipeline run performs TWO whole-ledger
    materialisations of the SAME lake — ``pull_all_spot`` and the historical
    phase's ``_run_phase`` each ``begin_pull_ledger`` (measured 1,305 B/row RSS
    + ~13.6 µs/row per load; ~16 s + a second full-resident copy across the
    phase boundary at the documented ~1.2M-row scale). Inside this scope the
    FIRST ``begin_pull_ledger`` per lake loads; every later one returns the SAME
    instance, so one pipeline run pays ONE load and never holds two distinct
    materialisations.

    Safety (why reuse is sound where the pipeline uses it):

    * the pipeline phases run **sequentially on one thread** under the
      account-wide pull single-flight lock — SAME-HOST concurrency is excluded
      by the flock/single-flight (``reopen_skipped`` refuses under the same
      lock), so on this host the re-load this scope skips could only ever have
      observed what this same process just saved (the save is a
      read-MERGE-write whose merge partner is our own prior save — the fold is
      idempotent). CROSS-HOST the lease is DETECTION, not mutual exclusion
      (DEC-ACCOUNT-LEASE-CROSSHOST-001): a cross-host peer's rows are folded
      at the save's read-MERGE-write — the in-memory instance may not see them
      until then, the same visibility as any mid-window write, and negligible
      in practice (foreign keys never enter group-scoped ``progress_counts``);
    * each phase's END-OF-PHASE SAVE is UNCHANGED (durability byte-identical —
      the mid-pull delta flush + per-phase :func:`end_pull_ledger` cadence all
      still run); ONLY the redundant re-load is skipped;
    * :class:`~brutex.data.window_ledger.WindowLedger` is internally locked, so
      even a caller that DID share across threads mutates safely;
    * a **dry-run** ``begin_pull_ledger`` still returns ``None`` and never
      enters the map; lakes are keyed by ``str(lake_root)`` — two spellings of
      one lake simply load twice (today's behaviour, never worse).

    Disclosed edge (Golden Rule #10): a corrupt base at pipeline start now
    carries ``load_failed=True`` into the LATER phases too (pre-scope, the
    second load read the first phase's rescue save and reported a clean
    reload) — the honest carry: the history genuinely was lost at pull start,
    and the re-verify note already fired from the first phase's flag.

    The scope always resets on exit (exception included). O(1) per call.
    """
    token = _SHARED_PULL_LEDGER.set({})
    try:
        yield
    finally:
        _SHARED_PULL_LEDGER.reset(token)


def begin_pull_ledger(
    lake_root: Path, *, dry_run: bool, now: datetime | None = None
) -> WindowLedger | None:
    """Resolve the ledger to thread into a pull (load/migrate for a live run).

    A **live** pull loads (or one-shot migrates) the persisted ledger so the table
    reflects the full cross-run history; a **dry-run** returns ``None`` (no
    recording, no disk touch) — its synthetic fetch would otherwise pollute the
    real ledger. Under an active :func:`shared_pull_ledger_scope`
    (DEC-LEDGER-LOAD-SPACE-V1-001) the first live call per lake loads and every
    later one returns the SAME instance — one materialisation per pipeline run;
    outside any scope (every other caller) the behaviour is byte-identical to
    before.
    """
    if dry_run:
        return None
    shared = _SHARED_PULL_LEDGER.get()
    if shared is None:
        return load_or_migrate_window_ledger(lake_root, now=now)
    key = str(lake_root)
    ledger = shared.get(key)
    if ledger is None:
        ledger = load_or_migrate_window_ledger(lake_root, now=now)
        shared[key] = ledger
    return ledger


def end_pull_ledger(ledger: WindowLedger | None, lake_root: Path, *, dry_run: bool) -> None:
    """Persist *ledger* after a live pull — a no-op for dry-run or an absent ledger."""
    if not dry_run and ledger is not None:
        save_window_ledger(ledger, lake_root)


__all__ = [
    "DEFAULT_LEDGER_SAVE_SLOW_WARN_S",
    "DELTAS_DIRNAME",
    "LEDGER_DIRNAME",
    "LEDGER_FILENAME",
    "LEDGER_SAVE_SLOW_WARN_ENV",
    "LIVE_FLUSH_MIN_INTERVAL_S",
    "SKIPPED_AUDIT_CURSOR_FILENAME",
    "LakeSpanIndex",
    "LedgerRow",
    "append_ledger_delta",
    "begin_pull_ledger",
    "deltas_dir",
    "end_pull_ledger",
    "index_lake_spans",
    "ledger_dir",
    "ledger_path",
    "list_ledger_deltas",
    "load_or_migrate_window_ledger",
    "load_skipped_audit_cursor",
    "load_window_ledger",
    "make_live_ledger_flush",
    "merge_ledger_rows",
    "plan_windows",
    "reconcile_ledger_windows",
    "reconcile_ledger_with_lake",
    "resolve_ledger_save_slow_warn_seconds",
    "run_skipped_audit",
    "run_skipped_audit_guarded",
    "save_skipped_audit_cursor",
    "save_window_ledger",
    "seed_enclosed_windows",
    "shared_pull_ledger_scope",
    "skipped_audit_cursor_path",
]
