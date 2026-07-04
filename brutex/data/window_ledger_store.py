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
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.calendars.nse import IST
from brutex.data.atomic_io import atomic_write_via
from brutex.data.errors import PartitionLockTimeout
from brutex.data.fetch_coverage import WindowKey
from brutex.data.interior_gap_detect import classify_interior_gap_dates
from brutex.data.partition_lock import partition_write_lock
from brutex.data.window_ledger import (
    EMPTY,
    EMPTY_TERMINAL_THRESHOLD,
    FAILED,
    PLANNED,
    SKIPPED,
    SUCCESS,
    WindowLedger,
    WindowLedgerRecord,
)

#: Structured logger — surfaces a corrupt/absent ledger into ``/logs``.
_LOG = structlog.get_logger("brutex.data.window_ledger_store")

#: The ledger table directory + file under the lake root (browsable on /db).
LEDGER_DIRNAME: str = "window_ledger"
LEDGER_FILENAME: str = "ledger.parquet"

#: Minimum seconds between live mid-pull ledger flushes (mirrors the status store):
#: small enough that /coverage/windows feels live, large enough that a
#: multi-thousand-row Parquet is not rewritten on every completed pair.
LIVE_FLUSH_MIN_INTERVAL_S: float = 2.0

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
#: ``in_pull_fetches`` were each appended LAST (DEC-WINDOW-LEDGER-001 R1 /
#: DEC-WINDOW-INPULL-FETCHES-001), so a legacy file written before either column
#: existed loads cleanly: the core columns are read and each optional column is
#: derived per-row (see :func:`_rows_to_records`). Backward-compatible, never a re-pull.
_COLUMNS: tuple[str, ...] = (*_CORE_COLUMNS, "empty_streak", "in_pull_fetches")


def ledger_dir(lake_root: Path) -> Path:
    """Return the ledger directory for *lake_root* (``<lake_root>/window_ledger``)."""
    return lake_root / LEDGER_DIRNAME


def ledger_path(lake_root: Path) -> Path:
    """Return the ledger Parquet path (``<lake_root>/window_ledger/ledger.parquet``)."""
    return ledger_dir(lake_root) / LEDGER_FILENAME


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


def _rows_to_records(rows: Iterable[Sequence[Any]]) -> list[tuple[Any, ...]]:
    """Coerce raw rows to the exact 13-field tuple :meth:`WindowLedger.from_rows` expects.

    Each row is the core 11 fields plus ``empty_streak`` (DEC-WINDOW-LEDGER-001 R1)
    plus ``in_pull_fetches`` (DEC-WINDOW-INPULL-FETCHES-001). A ``None`` optional column
    (a legacy file written before that column) is derived: ``empty_streak`` from
    ``(status, attempts)`` via :func:`_derive_empty_streak`; ``in_pull_fetches`` from
    ``status`` via :func:`_derive_in_pull_fetches`. A wrong arity / non-coercible element
    raises here (``ValueError`` / ``TypeError``) and is caught by the caller, degrading
    to an empty ledger.
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
            )
        )
    return coerced


def load_window_ledger(
    lake_root: Path, *, threshold: int = EMPTY_TERMINAL_THRESHOLD
) -> WindowLedger:
    """Load the persisted ledger for *lake_root*, or an EMPTY one if unavailable.

    **Never raises** (reliability): a missing file is a clean empty ledger; a
    malformed / wrong-shaped / unreadable Parquet logs a WARNING and also degrades
    to empty (re-pulling is safe — the idempotent sink no-ops stored bars).
    """
    path = ledger_path(lake_root)
    if not path.is_file():
        return WindowLedger(_threshold=threshold)
    try:
        import pyarrow.parquet as pq

        table = pq.read_table(path)  # type: ignore[no-untyped-call, unused-ignore]
        present = set(table.schema.names)
        # The core 11 columns are REQUIRED — a missing one raises KeyError below and
        # degrades to empty (the wrong-shape guard). ``empty_streak`` + ``in_pull_fetches``
        # are OPTIONAL: a legacy file lacks one (or both), so fill ``None`` for the missing
        # column(s) and let `_rows_to_records` derive each per-row.
        columns = {name: table.column(name).to_pylist() for name in _CORE_COLUMNS}
        n = len(columns[_CORE_COLUMNS[0]])
        streaks = (
            table.column("empty_streak").to_pylist() if "empty_streak" in present else [None] * n
        )
        fetches = (
            table.column("in_pull_fetches").to_pylist()
            if "in_pull_fetches" in present
            else [None] * n
        )
        rows = zip(*(columns[name] for name in _CORE_COLUMNS), streaks, fetches, strict=True)
        return WindowLedger.from_rows(_rows_to_records(rows), threshold=threshold)
    except (OSError, ValueError, TypeError, KeyError, ImportError) as exc:
        _LOG.warning("window_ledger.load_failed", path=str(path), error=str(exc))
        # LOUD degrade (DEC-WINDOW-LEDGER-RESCUE-001): the file EXISTED but could not
        # be read — mark the returned empty ledger so the pull drivers can announce
        # the failed reload (window ledger reload FAILED — running full re-verify
        # pull, no skip) on the pull report / note instead of silently re-fetching
        # everything. Still never raises (a lost ledger costs a re-pull, never data).
        degraded = WindowLedger(_threshold=threshold)
        degraded.load_failed = True
        return degraded


#: One serialised ledger row — EXACTLY the 13-field tuple :meth:`WindowLedger.to_rows`
#: emits (and :meth:`WindowLedger.from_rows` restores). The merge-on-save operates on
#: this shape directly so the conflict rule below is pure + unit-testable.
LedgerRow = tuple[str, str, str, str, str, str, int, int, str | None, str, str | None, int, int]

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


def save_window_ledger(ledger: WindowLedger, lake_root: Path) -> None:
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
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    directory = ledger_dir(lake_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = ledger_path(lake_root)
    try:
        # Cross-process lock keyed on the ledger path (DEC-LAKE-PARTITION-FLOCK-001
        # mechanics, DEC-WINDOW-LEDGER-RESCUE-001 wiring): held across the WHOLE
        # read-merge-write so the later writer reads AFTER the earlier writer's
        # os.replace — its merge then includes the earlier rows (no lost update).
        with partition_write_lock(lake_root, path):
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
            # Build per-column arrays in the fixed order so the schema is stable.
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
                ]
            )
            table = pa.table({name: by_col[name] for name in _COLUMNS}, schema=schema)

            def _write_parquet(tmp: Path) -> None:
                pq.write_table(table, tmp)  # type: ignore[no-untyped-call, unused-ignore]

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
    except PartitionLockTimeout as exc:
        # A peer held the ledger lock past the bounded deadline (a wedged writer).
        # SKIP this save loudly — never crash the pull mid-flush: the in-memory
        # ledger is intact and the next throttled flush / pull-end save retries.
        _LOG.warning(
            "window_ledger.save_lock_timeout",
            path=str(path),
            error=str(exc),
        )


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
    """
    if ledger_path(lake_root).is_file():
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
_SpanIndex = dict[tuple[str, str, str, str], dict[str, Any]]

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


def index_lake_spans(spans: Sequence[dict[str, Any]]) -> _SpanIndex:
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
    span_index: _SpanIndex,
    candidates: Iterable[SeedCandidate],
    *,
    now: datetime,
    today_ist: date | None = None,
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

    **Interior-gap density cross-check (DEC-COLDSTART-SEED-INTERIOR-GAP-001):** a
    span test alone would stamp ``SUCCESS`` over a window whose lake span contains
    an interior multi-session hole — and since ``SUCCESS`` is skippable, the hole
    would be masked FOREVER on a lost-ledger cold-start (the spot /completeness
    verdict is LEDGER-backed, so it would read ``full``; the fetch-time detector
    of DEC-INTERIOR-GAP-DETECT-001 never sees a seeded window). So when a span
    entry carries ``session_dates`` (attached ONLY for the dense INDEX ``1minute``
    spans — :data:`_SESSION_DATE_SEGMENTS` via
    :func:`reconcile_ledger_with_lake`) AND the candidate window's interval is
    the FETCHED spot granularity (``TRACK1_SPOT_INTERVAL`` — belt-and-braces with
    the read-side gate: a DERIVED ``1day`` window is never density-checked, or it
    could sit permanently un-seedable since it is never fetched), the window's
    in-range session dates are classified by the COMMON
    :func:`~brutex.data.interior_gap_detect.classify_interior_gap_dates` (one
    source of interior-hole truth — CLAUDE.md §11). A confirmed interior gap
    SKIPS the seed with ONE ``window_ledger.seed_interior_gap_skip`` WARNING —
    the window stays absent → ``PLANNED`` this pull → fetched → the fetch-time
    detector bounds it (``record(degraded=True)``, re-pullable then terminal).
    FAIL-CLOSED: an error inside the check logs
    ``window_ledger.seed_interior_gap_error`` and skips the seed (a re-pull is
    always data-safe — idempotent keep-first sink), never a crash. Honest
    residuals: CASH reference equities + the F&O reconcile stay span-only (their
    spans carry no ``session_dates``); an out-of-calendar-coverage hole
    honestly ABSTAINS (seeded — consistent with the fetch-time detector); and a
    hole at the window's LEADING/TRAILING edge — bars resuming only in a
    NEIGHBOURING window, so the dataset span still encloses this one — is
    invisible to the INTERIOR classifier (it walks strictly between the window's
    own first/last covered dates) and stays span-seeded, a deliberate
    interior-scope non-goal of DEC-COLDSTART-SEED-INTERIOR-GAP-001 (the
    fetch-path trailing-truncation demotion, DEC-PARTIAL-WINDOW-REPULL-001,
    watches edges on FETCHED windows only).

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
        # Density cross-check (DEC-COLDSTART-SEED-INTERIOR-GAP-001, docstring above):
        # only a FETCHED-interval window whose span carries `session_dates` is
        # checked — every other candidate (F&O / CASH / derived `1day` / legacy
        # span shape) takes the historical span-only path byte-identically.
        if window_key.interval == TRACK1_SPOT_INTERVAL:
            sessions = entry.get("session_dates")
            if sessions is not None:
                try:
                    window_sessions = [d for d in sessions if window_key.start <= d <= last_covered]
                    verdict = classify_interior_gap_dates(covered_dates=window_sessions)
                except Exception as exc:  # fail-CLOSED — a re-pull is always data-safe
                    _LOG.warning(
                        "window_ledger.seed_interior_gap_error",
                        span_key=span_key,
                        window_start=window_key.start.isoformat(),
                        window_end=window_key.end.isoformat(),
                        error=str(exc),
                    )
                    continue
                if verdict.is_interior_gap():
                    _LOG.warning(
                        "window_ledger.seed_interior_gap_skip",
                        span_key=span_key,
                        window_start=window_key.start.isoformat(),
                        window_end=window_key.end.isoformat(),
                        largest_gap_sessions=verdict.largest_gap_sessions,
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

    return seed_enclosed_windows(ledger, span_index, _candidates(), now=stamp)


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
    tests; O(N) only on the calls that actually write.

    The throttle is ADAPTIVE (DEC-OPTION-WINDOW-PLAN-001, hostile C2): the
    effective interval is ``max(min_interval_s, _FLUSH_DUTY_FACTOR ×
    last_save_duration)``, measured with the same injectable *clock*. A small
    ledger keeps the snappy fixed cadence; a pre-planned universe-scale ledger
    (~841k rows, ~4 s/save measured) self-paces so the flush can never dominate
    wall time or hold the ledger lock back-to-back.
    """
    from time import monotonic

    tick = clock if clock is not None else monotonic
    last_write: list[float | None] = [None]
    #: Seconds the LAST save took — drives the adaptive interval (0.0 until one runs).
    last_duration: list[float] = [0.0]

    def _flush() -> None:
        if ledger is None:
            return
        now = tick()
        interval = max(min_interval_s, _FLUSH_DUTY_FACTOR * last_duration[0])
        if last_write[0] is not None and now - last_write[0] < interval:
            return
        last_write[0] = now
        save_window_ledger(ledger, lake_root)
        last_duration[0] = max(tick() - now, 0.0)

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


def begin_pull_ledger(
    lake_root: Path, *, dry_run: bool, now: datetime | None = None
) -> WindowLedger | None:
    """Resolve the ledger to thread into a pull (load/migrate for a live run).

    A **live** pull loads (or one-shot migrates) the persisted ledger so the table
    reflects the full cross-run history; a **dry-run** returns ``None`` (no
    recording, no disk touch) — its synthetic fetch would otherwise pollute the
    real ledger.
    """
    if dry_run:
        return None
    return load_or_migrate_window_ledger(lake_root, now=now)


def end_pull_ledger(ledger: WindowLedger | None, lake_root: Path, *, dry_run: bool) -> None:
    """Persist *ledger* after a live pull — a no-op for dry-run or an absent ledger."""
    if not dry_run and ledger is not None:
        save_window_ledger(ledger, lake_root)


__all__ = [
    "LEDGER_DIRNAME",
    "LEDGER_FILENAME",
    "LIVE_FLUSH_MIN_INTERVAL_S",
    "LedgerRow",
    "begin_pull_ledger",
    "end_pull_ledger",
    "index_lake_spans",
    "ledger_dir",
    "ledger_path",
    "load_or_migrate_window_ledger",
    "load_window_ledger",
    "make_live_ledger_flush",
    "merge_ledger_rows",
    "plan_windows",
    "reconcile_ledger_windows",
    "reconcile_ledger_with_lake",
    "save_window_ledger",
    "seed_enclosed_windows",
]
