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
* **Reliability — never crash a pull.** :func:`load_window_ledger` degrades to an
  EMPTY ledger (a structured WARNING, never an exception) on a missing /
  unreadable / malformed / wrong-shaped file — a lost ledger costs a re-pull
  (idempotent keep-first sink), never data.
* **Reliability — crash-safe write.** :func:`save_window_ledger` writes a sibling
  ``*.tmp`` then :func:`os.replace` (atomic on POSIX within one filesystem).
* **Migration (one-shot).** :func:`load_or_migrate_window_ledger` folds the two
  legacy JSON sidecars (``window_status.json`` + ``fetch_coverage.json``) into the
  ledger the first time, the richer status-log row WINNING on conflict, so no
  history is lost when the unified ledger supersedes them.

The O(1) per-window probe/record stays in :class:`WindowLedger`; only the
one-shot load (run start) + save (run end) are O(N) over the recorded windows.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import structlog

from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import (
    EMPTY,
    EMPTY_TERMINAL_THRESHOLD,
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

#: The on-disk column order — fixed so the Parquet schema is stable + the row
#: tuples round-trip :meth:`WindowLedger.to_rows` exactly.
_COLUMNS: tuple[str, ...] = (
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


def ledger_dir(lake_root: Path) -> Path:
    """Return the ledger directory for *lake_root* (``<lake_root>/window_ledger``)."""
    return lake_root / LEDGER_DIRNAME


def ledger_path(lake_root: Path) -> Path:
    """Return the ledger Parquet path (``<lake_root>/window_ledger/ledger.parquet``)."""
    return ledger_dir(lake_root) / LEDGER_FILENAME


def _rows_to_records(rows: Iterable[Sequence[Any]]) -> list[tuple[Any, ...]]:
    """Coerce raw rows to the exact 11-field tuple :meth:`WindowLedger.from_rows` expects.

    A wrong arity / non-coercible element raises here (``ValueError`` /
    ``TypeError``) and is caught by the caller, degrading to an empty ledger.
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
    ) in rows:
        coerced.append(
            (
                str(market),
                str(security_id),
                str(interval),
                str(window_start),
                str(window_end),
                str(status),
                int(bars),
                int(attempts),
                (None if last_error is None else str(last_error)),
                str(first_seen_at),
                (None if last_attempt_at is None else str(last_attempt_at)),
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

        table = pq.read_table(path, columns=list(_COLUMNS))  # type: ignore[no-untyped-call]
        columns = {name: table.column(name).to_pylist() for name in _COLUMNS}
        rows = zip(*(columns[name] for name in _COLUMNS), strict=True)
        return WindowLedger.from_rows(_rows_to_records(rows), threshold=threshold)
    except (OSError, ValueError, TypeError, KeyError, ImportError) as exc:
        _LOG.warning("window_ledger.load_failed", path=str(path), error=str(exc))
        return WindowLedger(_threshold=threshold)


def save_window_ledger(ledger: WindowLedger, lake_root: Path) -> None:
    """Persist *ledger* to *lake_root*'s Parquet table atomically (crash-safe).

    Serialises :meth:`WindowLedger.to_rows` (sorted, deterministic) to a Parquet
    table with the fixed :data:`_COLUMNS` schema, writes a sibling ``*.tmp`` then
    :func:`os.replace` onto the final path. The ledger directory is created if
    absent. O(N) over the recorded windows (once per run / per throttled flush).
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    directory = ledger_dir(lake_root)
    directory.mkdir(parents=True, exist_ok=True)
    rows = ledger.to_rows()
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
        ]
    )
    table = pa.table({name: by_col[name] for name in _COLUMNS}, schema=schema)
    path = ledger_path(lake_root)
    tmp = path.with_name(f"{LEDGER_FILENAME}.tmp")
    pq.write_table(table, tmp)  # type: ignore[no-untyped-call]
    os.replace(tmp, path)


def _migrate_legacy_sidecars(lake_root: Path, *, threshold: int, now: datetime) -> WindowLedger:
    """Fold ``window_status.json`` + ``fetch_coverage.json`` into a fresh ledger — one-shot.

    The richer status-log row WINS on conflict (it carries bars / attempts /
    last_error); a coverage-only key (the coarse "complete" set) seeds a minimal
    ``SUCCESS`` row. An ``EMPTY`` status at/above *threshold* migrates straight to
    ``SKIPPED`` (the EMPTY-terminal settle), mirroring :meth:`WindowLedger.record`.
    """
    from brutex.data.fetch_coverage_store import load_fetch_coverage
    from brutex.data.window_status_store import load_window_status

    by_window: dict[tuple[str, str, str, str, str], WindowLedgerRecord] = {}

    status_log = load_window_status(lake_root)
    for r in status_log.records():
        status = r.status
        if status == EMPTY and r.attempts >= threshold:
            status = "SKIPPED"
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
            )
        )

    coverage = load_fetch_coverage(lake_root)
    stamp = now.isoformat()
    for market, security_id, interval, start, end in coverage.to_records():
        key = (market, security_id, interval, start, end)
        if key in by_window:
            continue  # status-log row already richer — it wins
        by_window[key] = WindowLedgerRecord(
            market=market,
            security_id=security_id,
            interval=interval,
            window_start=start,
            window_end=end,
            status=SUCCESS,  # the coarse set's only signal is "complete"
            bars=0,
            attempts=1,
            last_error=None,
            first_seen_at=stamp,
            last_attempt_at=stamp,
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
    """Load the ledger, or seed it once from the two legacy sidecars (no data loss).

    Steady state: the Parquet exists → :func:`load_window_ledger`. First run after
    the unification: no Parquet → fold ``window_status.json`` +
    ``fetch_coverage.json`` (:func:`_migrate_legacy_sidecars`). The legacy files
    are LEFT on disk (a one-release rollback net); a later slice removes them.
    """
    if ledger_path(lake_root).is_file():
        return load_window_ledger(lake_root, threshold=threshold)
    stamp = now if now is not None else datetime.now(UTC)
    return _migrate_legacy_sidecars(lake_root, threshold=threshold, now=stamp)


def plan_windows(
    ledger: WindowLedger,
    *,
    instruments: Iterable[Any],
    intervals: Sequence[str],
    start: date,
    end: date,
    now: datetime | None = None,
) -> int:
    """Upsert every planned window for the pull as ``PLANNED`` — returns the count.

    For each ``(instrument, interval)`` over ``[start, end)`` the canonical tiler
    (:func:`~brutex.adapters.dhan.ingest._windows`, ONE source of truth — the same
    tiling the ingest fetches) yields the windows; each is
    :meth:`WindowLedger.plan`-ned (idempotent — a settled row is untouched). This
    is what guarantees EVERY window the pull will touch has a row BEFORE any fetch,
    so a window that is never reached still shows ``PLANNED`` (never silently
    missing). O(windows) once at pull start. Generic over the instrument's
    ``exch_id``/``segment``/``symbol_name``, so it serves spot AND option series.
    """
    from brutex.adapters.dhan.ingest import _MARKET, _windows

    stamp = now if now is not None else datetime.now(UTC)
    planned = 0
    for instrument in instruments:
        for interval in intervals:
            for window_start, window_end in _windows(interval, start, end):
                ledger.plan(
                    WindowKey(
                        market=_MARKET,
                        security_id=instrument.symbol_name,
                        interval=interval,
                        start=window_start,
                        end=window_end,
                    ),
                    now=stamp,
                )
                planned += 1
    return planned


def reconcile_ledger_windows(
    ledger: WindowLedger,
    spans: Sequence[dict[str, Any]],
    *,
    instruments: Iterable[Any],
    intervals: Sequence[str],
    start: date,
    end: date,
    now: datetime | None = None,
) -> int:
    """Seed ``SUCCESS`` for every lake-enclosed window with no ledger row — **pure** (cold-start).

    DEC-WINDOW-LEDGER-001. On a fresh clone / lost ledger the lake's Parquet holds
    bars but the ledger is empty, so once the ledger drives the skip a re-pull would
    re-fetch EVERYTHING (the rescue the old ``reconcile_coverage_with_lake`` gave the
    coverage set). For each ``(instrument, interval)`` whose stored span ENCLOSES a
    planned window (``first_date <= window_start AND window_end <= last_date``, span
    has ≥ 1 bar — the conservative DEC-LAKE-RECONCILE-001 rule), seed that window
    ``SUCCESS`` — but ONLY if ABSENT (:meth:`WindowLedger.seed_lake_success` NEVER
    overwrites a ``FAILED`` / ``PLANNED`` / settled row, so it can't mask a re-pull).
    *spans* is the :func:`~brutex.dashboard.coverage_reconcile.load_lake_spans`
    shape. O(windows) once at pull start. Returns the number of windows seeded.
    """
    from brutex.adapters.dhan.ingest import _MARKET, _windows

    stamp = now if now is not None else datetime.now(UTC)
    span_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {
        (str(s["exchange"]), str(s["segment"]), str(s["symbol"]), str(s["interval"])): s
        for s in spans
    }
    seeded = 0
    for instrument in instruments:
        for interval in intervals:
            entry = span_by_key.get(
                (
                    instrument.exch_id.value,
                    instrument.segment.value,
                    instrument.symbol_name,
                    interval,
                )
            )
            if (
                entry is None
                or int(entry["bars"]) <= 0
                or entry["first_date"] is None
                or entry["last_date"] is None
            ):
                continue
            first_date = date.fromisoformat(str(entry["first_date"]))
            last_date = date.fromisoformat(str(entry["last_date"]))
            for window_start, window_end in _windows(interval, start, end):
                if not (first_date <= window_start and window_end <= last_date):
                    continue
                if ledger.seed_lake_success(
                    WindowKey(
                        market=_MARKET,
                        security_id=instrument.symbol_name,
                        interval=interval,
                        start=window_start,
                        end=window_end,
                    ),
                    bars=int(entry["bars"]),
                    now=stamp,
                ):
                    seeded += 1
    return seeded


def reconcile_ledger_with_lake(
    ledger: WindowLedger,
    lake_root: Path,
    *,
    instruments: Iterable[Any],
    intervals: Sequence[str],
    start: date,
    end: date,
    now: datetime | None = None,
) -> int:
    """Load the lake spans + seed the ledger cold-start (the wired entry point).

    Thin composition of :func:`~brutex.dashboard.coverage_reconcile.load_lake_spans`
    (the DuckDB span read) + :func:`reconcile_ledger_windows` (pure). Called once at
    pull start so a fresh-clone ledger inherits the lake's already-stored windows as
    ``SUCCESS`` — no full re-pull / quota blowout once the ledger drives skip.
    O(datasets) aggregate reads, never per-window.
    """
    from brutex.dashboard.coverage_reconcile import load_lake_spans

    spans = load_lake_spans(lake_root)
    return reconcile_ledger_windows(
        ledger,
        spans,
        instruments=instruments,
        intervals=intervals,
        start=start,
        end=end,
        now=now,
    )


def make_live_ledger_flush(
    ledger: WindowLedger | None,
    lake_root: Path,
    *,
    min_interval_s: float = LIVE_FLUSH_MIN_INTERVAL_S,
    clock: Any = None,
) -> Any:
    """Return a THROTTLED mid-pull ledger-flush callback (live /db updates).

    During a long pull every window's outcome accumulates in *ledger* in memory;
    this persists it MID-pull (throttled to once per *min_interval_s*) so /db +
    /coverage/windows tick up live instead of jumping only at pull end. A ``None``
    *ledger* (dry-run) yields a no-op callable. *clock* / *min_interval_s* are
    injectable for deterministic tests; O(N) only on the calls that actually write.
    """
    from time import monotonic

    tick = clock if clock is not None else monotonic
    last_write: list[float | None] = [None]

    def _flush() -> None:
        if ledger is None:
            return
        now = tick()
        if last_write[0] is not None and now - last_write[0] < min_interval_s:
            return
        last_write[0] = now
        save_window_ledger(ledger, lake_root)

    return _flush


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
    "begin_pull_ledger",
    "end_pull_ledger",
    "ledger_dir",
    "ledger_path",
    "load_or_migrate_window_ledger",
    "load_window_ledger",
    "make_live_ledger_flush",
    "plan_windows",
    "reconcile_ledger_windows",
    "reconcile_ledger_with_lake",
    "save_window_ledger",
]
