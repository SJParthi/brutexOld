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
* **Reliability — crash-safe write + graceful disk-full halt.**
  :func:`save_window_ledger` writes a sibling ``*.tmp`` then :func:`os.replace`
  (atomic on POSIX within one filesystem) via the COMMON
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
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import structlog

from brutex.data.atomic_io import atomic_write_via
from brutex.data.fetch_coverage import WindowKey
from brutex.data.window_ledger import (
    EMPTY,
    EMPTY_TERMINAL_THRESHOLD,
    PLANNED,
    SKIPPED,
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
            ("empty_streak", pa.int64()),
            ("in_pull_fetches", pa.int64()),
        ]
    )
    table = pa.table({name: by_col[name] for name in _COLUMNS}, schema=schema)
    path = ledger_path(lake_root)

    def _write_parquet(tmp: Path) -> None:
        pq.write_table(table, tmp)  # type: ignore[no-untyped-call, unused-ignore]

    # COMMON lake-safe atomic write (tmp→fsync→os.replace→cleanup, CLAUDE.md §11,
    # DEC-WINDOW-LEDGER-WRITE-HALT-001): the ledger is a LAKE write (it lives under
    # <lake_root>/window_ledger/), so a disk-full (ENOSPC) / OOM here fails every
    # remaining window identically — it must HALT the pull gracefully, not be
    # mis-quarantined per-symbol (which would keep hammering a full disk). `atomic_write_via`
    # maps ENOSPC→LakeWriteHalt("disk_full") / MemoryError→LakeWriteHalt("out_of_memory")
    # (every other error UNCHANGED — a non-ENOSPC OSError such as EACCES propagates raw,
    # never mis-escalated). LakeWriteHalt is a member of BOTH ingest spines'
    # _GLOBAL_STOP_ERRORS, so it propagates past the per-symbol/per-contract quarantine
    # to the CLI `_pull_halt_errors` / dashboard `_is_pull_halt` halt handling already in
    # place (the call-path verification the prior deferral flagged — now complete). The
    # fsync (durability for the durable ledger) + temp cleanup on any error are unchanged.
    atomic_write_via(path, _write_parquet)


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
#: the WHOLE ``bars/`` tree — spot AND ``segment="FNO"`` contract partitions alike).
_SpanIndex = dict[tuple[str, str, str, str], dict[str, Any]]

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
) -> int:
    """Seed ``SUCCESS`` (only-if-absent) for each candidate window the lake ENCLOSES — the COMMON core.

    The single reconcile engine shared by the spot + F&O cold-starts (CLAUDE.md §11
    COMMON). For each ``(window_key, span_key)`` candidate it looks up the lake span,
    and — iff the span has ≥ 1 bar and its stored ``[first_date, last_date]`` FULLY
    encloses the window (``first_date <= window.start AND window.end <= last_date``, the
    conservative DEC-LAKE-RECONCILE-001 rule) — seeds the window ``SUCCESS`` via
    :meth:`~brutex.data.window_ledger.WindowLedger.seed_lake_success` (which NEVER
    overwrites an existing ``FAILED`` / ``PLANNED`` / settled row, so a genuinely-missing
    or partially-covered window is left re-pullable). Returns the number of windows newly
    seeded. Pure over *span_index* + *candidates*; O(candidates) with O(1) lookups.
    """
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
        if not (first_date <= window_key.start and window_key.end <= last_date):
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
    planned window (``first_date <= window_start AND window_end <= last_date``, span
    has ≥ 1 bar — the conservative DEC-LAKE-RECONCILE-001 rule), seed that window
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
    "begin_pull_ledger",
    "end_pull_ledger",
    "index_lake_spans",
    "ledger_dir",
    "ledger_path",
    "load_or_migrate_window_ledger",
    "load_window_ledger",
    "make_live_ledger_flush",
    "plan_windows",
    "reconcile_ledger_windows",
    "reconcile_ledger_with_lake",
    "save_window_ledger",
    "seed_enclosed_windows",
]
