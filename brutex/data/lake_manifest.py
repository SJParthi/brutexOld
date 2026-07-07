"""Durable write-time lake dataset manifest — O(manifest-rows) discovery (DEC-LAKE-MANIFEST-O1-001).

The problem this closes (operator-hit, measured): after a dashboard restart the
FIRST ``/movers`` (and ``/db`` datasets) request re-discovers which
``(exchange, segment, symbol, interval)`` candle datasets exist by WALKING the
whole ``bars/`` tree — on the post-prebuild fully-materialised lake that is
**2,355,002 files** (the ``lake.invariant.scan_complete`` census of the
operator's 2026-07-05 restart log) and the first uncached request measured
``latency_ms: 1784347`` — **29.7 minutes** — before the first row rendered.
The walk result is memoised per write-generation IN-PROCESS
(``_db_datasets_cache`` / ``MoversSnapshotCache``), but a restart loses it, and
the O(files) discovery walk was already named future work in MIGRATION.md
#867/#1025 ("true O(page) needs a write-time lake manifest, NOT claimed").
This module IS that write-time manifest.

Design — write-time maintenance, read-time O(manifest-rows)
-----------------------------------------------------------
* ``<lake_root>/.manifest/datasets.jsonl`` — one JSON object per line, one line
  per dataset ``{"exchange", "segment", "symbol", "interval", "first_seen"}``.
  Append-only on the write path (an O(1) append per NEW dataset — an
  existing-partition bar append costs ZERO manifest work), fully rewritten only
  by :func:`rebuild_manifest` (the read-side self-heal after a legacy walk).
* ``<lake_root>/.manifest/datasets.meta.json`` — the tiny integrity header:
  ``{"version", "ledger_token", "rows"}``. ``rows`` MUST equal the physical
  JSONL line count (a torn append / truncation fails closed); ``ledger_token``
  is the window-ledger ``os.stat`` freshness token (below).
* The ``.manifest/`` dir sits OUTSIDE ``bars/`` + ``options/`` and holds no
  ``*.parquet``, so no lake scanner / dataset glob ever sees it (the exact
  placement rule of ``.locks/`` — DEC-LAKE-PARTITION-FLOCK-001 #887).
* Cross-process writers serialise on the manifest's OWN flock sidecar via the
  COMMON :func:`~brutex.data.partition_lock.partition_write_lock` keyed on the
  JSONL path (CLAUDE.md §11 COMMON — no second lock implementation).

Staleness honesty (Golden Rule #1 — bounded, stated windows, never silent)
--------------------------------------------------------------------------
The freshness token mirrors the proven ``lake_revision`` approach
(DEC-HARDENING-LEFTOVERS-001 #1149): ONE ``os.stat`` of the durable
window-ledger Parquet (every production pull — dashboard AND CLI — checkpoints
it per window, DEC-CLI-LEDGER-CHECKPOINT-001 #884). A reader serves the
manifest ONLY when the CURRENT ledger token equals the token stamped at the
last manifest write; a mismatch (a vendor pull landed since) falls back to the
legacy walk and REBUILDS the manifest from the walk result, stamped with the
token captured BEFORE the walk started (so a LEDGER-MOVING write landing
DURING the walk stales the rebuilt manifest immediately) and UNIONED
keep-first with any parseable on-disk rows (so a concurrent writer's
:func:`record_dataset` append survives the rebuild). Every writer-side
:func:`record_dataset` upsert re-stamps the CURRENT token, so the post-pull
prebuild (which creates the ~10⁵ derived-TF datasets WITHOUT touching the
ledger) converges the manifest to a served-hot state with no walk at all.

Two BOUNDED under/over-listing windows remain (honest, not claimed away):

* **Cold walk ∩ active prebuild** — while NO manifest base exists (the very
  walk that is rebuilding it), :func:`record_dataset` no-ops, so a NON-ledger
  writer (the prebuild) creating a dataset AFTER the walk passed its directory
  is in NEITHER the walk result NOR the on-disk union; the rebuild is stamped
  with a still-current token and UNDER-LISTS that dataset until the next
  ledger move / heal invalidation. Not a regression — the pre-manifest
  generation-keyed memo had the exact same window (a walk racing a prebuild
  missed the same dirs) — but the bound is stated, not hidden.
* **Failed invalidation ghost row** — a heal that removes a dataset's LAST
  file invalidates best-effort; if BOTH unlinks fail with a real
  :class:`OSError` (e.g. a permissions fault on the whole ``.manifest`` dir —
  logged ``lake_manifest.unlink_failed``; a meta-ONLY failure still fails
  closed, the meta-first order's point), the surviving manifest can serve one
  GHOST row (an empty, queryable-but-bar-less dataset — an empty page, never
  wrong data) until the next ledger move.

Honest envelope (Golden Rule #10 — stated, not hidden)
------------------------------------------------------
* A manifest-served discovery is **O(manifest-rows) ≈ O(datasets)** — one
  JSONL read + parse — NOT a literal O(1)-total. It is O(1) **in partition
  depth / file count**: adding more month files to existing datasets does not
  grow it. The walk it replaces is O(files) (~2.36M on the measured lake).
* Manifest-first covers **writer-created data**: every dataset creation goes
  through :meth:`~brutex.data.parquet_writer.ParquetWriter.write`'s fresh
  branch (which calls :func:`record_dataset`), and every heal/quarantine
  removal calls :func:`invalidate_manifest`. A **hand-copied partition** (the
  documented #1149 out-of-band bypass) updates neither the manifest nor the
  ledger and is therefore NOT visible until the next walk — exactly the same
  envelope ``lake_revision`` already documents. A vendor pull into EXISTING
  datasets moves the ledger without touching the manifest, so the first
  discovery after it pays one walk + rebuild (the same cost that read already
  paid pre-manifest via the generation bump — no regression, no free lunch
  claimed).
* Every write-side helper here is **best-effort**: a manifest failure logs +
  invalidates (so a half-written manifest can never be served) and NEVER
  raises into the pull — the manifest is an accelerator, not a correctness
  surface. The lake's source of truth remains the ``bars/`` tree itself.

Log-volume envelope (DEC-MANIFEST-LOG-FLOOD-001)
------------------------------------------------
:func:`record_dataset` fires once per CREATED dataset, and the prebuild
creates ~10⁵ derived-TF datasets per run — at INFO the per-dataset
``lake_manifest.dataset_recorded`` line was the EXACT
DEC-PREBUILD-LOG-FLOOD-001 flood class re-introduced by this module's PR
(measured on the operator's 2026-07-05 run: **94,190 lines at ~190 lines/s**,
~95% of two 10 MiB rotation files in ~8 minutes, rotating away the
prebuild-completion audit trail — the ``gate_released`` /
``pipeline.prebuild.done`` evidence). So the per-dataset event rides
**DEBUG** (same event name + fields — full detail one ``--log-level debug``
away), and the surviving INFO record is the AGGREGATE
``lake_manifest.record_summary`` (dataset count + elapsed + per-field
histograms — see :func:`flush_record_summary`), emitted at most once per
:data:`RECORD_SUMMARY_INTERVAL_S` of recording activity plus once at each
natural manifest boundary (:func:`load_manifest_datasets` /
:func:`rebuild_manifest` / :func:`invalidate_manifest` flush the pending
window), so a full prebuild's manifest activity costs
``≤ ceil(elapsed / interval) + boundary-flushes`` INFO lines — bounded by
RUN DURATION, never O(datasets). HONEST: the pending window is per-PROCESS,
best-effort state — a crash before a flush loses the trailing (≤ interval)
window's aggregate line (the per-dataset DEBUG lines exist only in a
deliberately-verbose session).
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Generator, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.data.errors import PartitionLockTimeout
from brutex.data.partition_lock import partition_write_lock

_LOG = structlog.get_logger("brutex.data.lake_manifest")

#: The manifest's home under the lake root — OUTSIDE ``bars/`` + ``options/``
#: and never ``*.parquet``, so no scanner/glob sees it (the ``.locks/`` rule,
#: DEC-LAKE-PARTITION-FLOCK-001).
MANIFEST_DIRNAME: Final[str] = ".manifest"

#: The append-only dataset rows (one JSON object per line).
MANIFEST_DATA_FILENAME: Final[str] = "datasets.jsonl"

#: The tiny integrity header (version + ledger token + row count).
MANIFEST_META_FILENAME: Final[str] = "datasets.meta.json"

#: On-disk format version. A reader seeing any OTHER version treats the
#: manifest as invalid (graceful walk fallback + rebuild) — never a guess.
MANIFEST_VERSION: Final[int] = 1

#: Stable ledger-token sentinel for a lake whose window ledger does not exist
#: yet (a fresh/empty or ledger-less test lake). Mirrors
#: ``brutex.dashboard.lake_revision.ABSENT_TOKEN`` semantics: the FIRST pull
#: that creates the ledger moves ``absent`` → a real stat token exactly once.
ABSENT_LEDGER_TOKEN: Final[str] = "absent"  # noqa: S105  # nosec B105 — a freshness sentinel, not a secret

#: The four coordinate fields every manifest row must carry (as strings).
_ROW_FIELDS: Final[tuple[str, str, str, str]] = ("exchange", "segment", "symbol", "interval")

#: The durable window-ledger Parquet's location relative to the lake root.
#: MUST equal ``brutex.data.window_ledger_store.ledger_path(lake_root)`` —
#: PINNED BY TEST, deliberately NOT imported: ``window_ledger_store`` pulls
#: the ledger/calendar import graph (``window_ledger`` →
#: ``interior_gap_detect`` → ``observed_sessions``, whose probes lazily reach
#: back into the lake/adapter modules), and importing it from here — even
#: function-locally — placed this module inside that import cycle (the CodeQL
#: ``py/cyclic-import`` findings on PR #1177: the ``parquet_writer`` /
#: ``db_view`` → ``lake_manifest`` edges then closed the loop). The manifest
#: must stay a LEAF (stdlib + ``atomic_io`` + ``partition_lock`` only) so the
#: lake writer AND the dashboard can both import it cycle-free — the exact
#: ``lake_revision._SWEEPS_DIRNAME`` test-pinned-not-imported precedent
#: (DEC-HARDENING-LEFTOVERS-001).
_LEDGER_DIRNAME: Final[str] = "window_ledger"
_LEDGER_FILENAME: Final[str] = "ledger.parquet"

#: Maximum recording-activity span (seconds) one pending aggregate window may
#: cover before :func:`record_dataset` emits it inline
#: (DEC-MANIFEST-LOG-FLOOD-001). Bounds the INFO volume of a sustained record
#: stream to ``≤ ceil(elapsed / interval)`` lines per lake per process (the
#: operator's measured 94,190-record / ~8-minute prebuild flood collapses to
#: ≤ ~9 aggregate lines) AND bounds how long manifest activity can stay
#: invisible at INFO in a process that never reaches a boundary flush.
RECORD_SUMMARY_INTERVAL_S: Final[float] = 60.0


@dataclass(slots=True)
class _RecordSummaryWindow:
    """One pending per-lake aggregate of :func:`record_dataset` successes.

    Accumulates the per-dataset fields in AGGREGATE form (count histograms
    for exchange/segment/interval, a distinct-symbol set) between flushes.
    Memory is O(distinct coordinates per window) — bounded by the flush
    interval / boundary flushes, and cleared on every emit.
    """

    started_monotonic: float
    last_monotonic: float
    count: int
    exchanges: dict[str, int]
    segments: dict[str, int]
    intervals: dict[str, int]
    symbols: set[str]


#: Pending aggregate windows keyed by ``str(lake_root)``, guarded by
#: :data:`_RECORD_SUMMARY_LOCK`. Process-local best-effort telemetry state —
#: NEVER part of the on-disk manifest contract.
_RECORD_SUMMARY_WINDOWS: dict[str, _RecordSummaryWindow] = {}
_RECORD_SUMMARY_LOCK = threading.Lock()


def _emit_record_summary(lake_root_key: str, window: _RecordSummaryWindow) -> None:
    """Emit ONE aggregate INFO for a closed window (DEC-MANIFEST-LOG-FLOOD-001).

    The aggregate form of the per-dataset ``lake_manifest.dataset_recorded``
    DEBUG fields: every field survives as a count histogram (exchange /
    segment / interval) or a distinct count (symbol), plus the window's
    recording-activity span (first → LAST record, not flush time).
    """
    _LOG.info(
        "lake_manifest.record_summary",
        lake_root=lake_root_key,
        datasets_recorded=window.count,
        elapsed_s=round(window.last_monotonic - window.started_monotonic, 3),
        exchanges=dict(sorted(window.exchanges.items())),
        segments=dict(sorted(window.segments.items())),
        intervals=dict(sorted(window.intervals.items())),
        distinct_symbols=len(window.symbols),
    )


def _note_dataset_recorded(
    lake_root: Path, *, exchange: str, segment: str, symbol: str, interval: str
) -> None:
    """Fold one successful :func:`record_dataset` into the pending window.

    Opens the lake's window on the first record; emits + resets it inline
    once the window's span reaches :data:`RECORD_SUMMARY_INTERVAL_S` (read at
    call time so tests can patch it). The log call runs OUTSIDE the tiny
    module lock; O(1) per record.
    """
    now = time.monotonic()
    key = str(lake_root)
    closed: _RecordSummaryWindow | None = None
    with _RECORD_SUMMARY_LOCK:
        window = _RECORD_SUMMARY_WINDOWS.get(key)
        if window is None:
            window = _RecordSummaryWindow(
                started_monotonic=now,
                last_monotonic=now,
                count=0,
                exchanges={},
                segments={},
                intervals={},
                symbols=set(),
            )
            _RECORD_SUMMARY_WINDOWS[key] = window
        window.last_monotonic = now
        window.count += 1
        window.exchanges[exchange] = window.exchanges.get(exchange, 0) + 1
        window.segments[segment] = window.segments.get(segment, 0) + 1
        window.intervals[interval] = window.intervals.get(interval, 0) + 1
        window.symbols.add(symbol)
        if now - window.started_monotonic >= RECORD_SUMMARY_INTERVAL_S:
            closed = _RECORD_SUMMARY_WINDOWS.pop(key)
    if closed is not None:
        _emit_record_summary(key, closed)


def flush_record_summary(lake_root: Path) -> int:
    """Emit the pending aggregate INFO for *lake_root*, if any — O(1).

    Returns the number of dataset recordings the flushed window covered
    (``0`` when nothing was pending — nothing is logged then). Called at
    every natural manifest boundary (:func:`load_manifest_datasets` /
    :func:`rebuild_manifest` / :func:`invalidate_manifest`) so the trailing
    sub-interval window — e.g. the tail of a prebuild's record burst — is
    surfaced at INFO by the first post-run manifest touch instead of being
    lost. Safe to call from any thread; never raises past the log call.
    """
    key = str(lake_root)
    with _RECORD_SUMMARY_LOCK:
        window = _RECORD_SUMMARY_WINDOWS.pop(key, None)
    if window is None:
        return 0
    _emit_record_summary(key, window)
    return window.count


def ledger_file_path(lake_root: Path) -> Path:
    """The window-ledger Parquet path the freshness token stats — O(1), no I/O.

    The test-pinned twin of ``brutex.data.window_ledger_store.ledger_path``
    (see :data:`_LEDGER_DIRNAME` for why it is duplicated rather than
    imported — the manifest is a leaf module by design).
    """
    return lake_root / _LEDGER_DIRNAME / _LEDGER_FILENAME


def manifest_dir(lake_root: Path) -> Path:
    """Return ``<lake_root>/.manifest`` (never created here)."""
    return lake_root / MANIFEST_DIRNAME


def manifest_data_path(lake_root: Path) -> Path:
    """Return the dataset-rows JSONL path (never created here)."""
    return manifest_dir(lake_root) / MANIFEST_DATA_FILENAME


def manifest_meta_path(lake_root: Path) -> Path:
    """Return the integrity-header JSON path (never created here)."""
    return manifest_dir(lake_root) / MANIFEST_META_FILENAME


def ledger_stat_token(lake_root: Path) -> str | None:
    """The window-ledger freshness token: ``"<mtime_ns>:<size>"`` — O(1).

    The data-layer twin of ``lake_revision._ledger_revision`` (that module is
    dashboard-layer and composes a sweeps-tree component this manifest does not
    need — a sweep write never creates a candle dataset). The ledger path is
    the test-pinned :func:`ledger_file_path` twin of
    ``window_ledger_store.ledger_path``, NOT an import (see
    :data:`_LEDGER_DIRNAME` — the import, even function-local, put this module
    inside the ledger/calendar import cycle CodeQL flagged on PR #1177).
    Returns :data:`ABSENT_LEDGER_TOKEN` when the ledger does not exist yet,
    ``None`` ("unknown — no freshness decision") on any other
    :class:`OSError`, logged.
    """
    target = ledger_file_path(lake_root)
    try:
        st = os.stat(target)
    except FileNotFoundError:
        return ABSENT_LEDGER_TOKEN
    except OSError as exc:
        _LOG.warning(
            "lake_manifest.ledger_stat_failed",
            ledger_path=str(target),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None
    return f"{st.st_mtime_ns}:{st.st_size}"


@contextlib.contextmanager
def _manifest_lock(lake_root: Path) -> Generator[None, None, None]:
    """Hold the manifest's cross-process flock (the COMMON sidecar lock).

    Keyed on the JSONL path via :func:`partition_write_lock` — the SAME
    ``.locks/<sha256(realpath)>.lock`` machinery the partition writers use
    (CLAUDE.md §11 COMMON), so concurrent dashboard + CLI writers serialise
    their read-modify-write of the tiny meta header. Lock ORDER: the manifest
    lock is only ever taken while holding NOTHING or a partition lock (the
    heal-quarantine invalidate), and no code path takes a partition lock while
    holding the manifest lock — no cross-lock deadlock.
    """
    with partition_write_lock(lake_root, manifest_data_path(lake_root)):
        yield


def _read_meta(lake_root: Path) -> dict[str, Any] | None:
    """Parse the meta header, or ``None`` when absent / corrupt / wrong shape."""
    try:
        raw = manifest_meta_path(lake_root).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        _LOG.warning(
            "lake_manifest.meta_read_failed", error_type=type(exc).__name__, error=str(exc)
        )
        return None
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError:
        _LOG.warning("lake_manifest.meta_corrupt")
        return None
    if (
        not isinstance(meta, dict)
        or meta.get("version") != MANIFEST_VERSION
        or not isinstance(meta.get("ledger_token"), str)
        or not isinstance(meta.get("rows"), int)
        or meta["rows"] < 0
    ):
        _LOG.warning("lake_manifest.meta_invalid")
        return None
    return meta


def _write_meta(lake_root: Path, *, ledger_token: str, rows: int) -> None:
    """Atomically write the meta header (guard-free — callers map errors)."""
    payload = json.dumps(
        {"version": MANIFEST_VERSION, "ledger_token": ledger_token, "rows": rows},
        sort_keys=True,
    )
    atomic_write_simple(
        manifest_meta_path(lake_root), lambda tmp: tmp.write_text(payload, encoding="utf-8")
    )


def _row_line(row: Mapping[str, Any], *, first_seen: str) -> str:
    """Render one dataset row as its canonical JSONL line."""
    return json.dumps(
        {
            "exchange": str(row["exchange"]),
            "segment": str(row["segment"]),
            "symbol": str(row["symbol"]),
            "interval": str(row["interval"]),
            "first_seen": first_seen,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_lines(text: str) -> list[dict[str, str]] | None:
    """Parse the JSONL body into rows, or ``None`` on ANY malformed line.

    Fail-closed: a single unparseable / wrong-shape line invalidates the whole
    manifest (the reader falls back to the walk) — a partially-read manifest
    would be exactly the stale illusion Golden Rule #1 forbids.
    """
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        row: dict[str, str] = {}
        for field in _ROW_FIELDS:
            value = obj.get(field)
            if not isinstance(value, str) or not value:
                return None
            row[field] = value
        rows.append(row)
    return rows


def load_manifest_datasets(lake_root: Path) -> list[dict[str, str]] | None:
    """Return the manifest's dataset rows iff it validates, else ``None``.

    Validation (ALL must hold, else ``None`` — the caller falls back to the
    legacy walk; never a 500, never a guessed dataset):

    1. the meta header exists, parses, and carries this format version;
    2. the CURRENT window-ledger stat token equals the stamped
       ``ledger_token`` (a mismatch means a pull landed since the last
       manifest write — the row set may be stale);
    3. the physical JSONL line count equals ``meta["rows"]`` (a torn append /
       truncated file fails closed);
    4. every line parses to the four required string fields.

    Duplicate coordinates (a cross-process double-append) are collapsed
    keep-first. Cost: **O(manifest-rows)** — one bounded read + parse,
    independent of the lake's partition/file count (the honest envelope; NOT a
    literal O(1)-total). Returns rows sorted by
    ``(exchange, segment, symbol, interval)`` — the walk's order.
    """
    # Boundary flush (DEC-MANIFEST-LOG-FLOOD-001): the first manifest read
    # after a record burst (e.g. the prebuild) surfaces the trailing
    # aggregate window at INFO; a no-burst read flushes (and logs) nothing.
    flush_record_summary(lake_root)
    current_token = ledger_stat_token(lake_root)
    if current_token is None:
        return None
    try:
        with _manifest_lock(lake_root):
            meta = _read_meta(lake_root)
            if meta is None:
                return None
            try:
                body = manifest_data_path(lake_root).read_text(encoding="utf-8")
            except FileNotFoundError:
                _LOG.warning("lake_manifest.data_missing_with_meta")
                return None
    except (OSError, PartitionLockTimeout) as exc:
        _LOG.warning("lake_manifest.load_failed", error_type=type(exc).__name__, error=str(exc))
        return None
    if meta["ledger_token"] != current_token:
        _LOG.info(
            "lake_manifest.stale",
            stamped_token=meta["ledger_token"],
            current_token=current_token,
        )
        return None
    lines = body.splitlines()
    if len(lines) != meta["rows"]:
        _LOG.warning("lake_manifest.row_count_mismatch", lines=len(lines), rows=meta["rows"])
        return None
    rows = _parse_lines(body)
    if rows is None:
        _LOG.warning("lake_manifest.line_corrupt")
        return None
    deduped: dict[tuple[str, str, str, str], dict[str, str]] = {}
    for row in rows:
        key = (row["exchange"], row["segment"], row["symbol"], row["interval"])
        deduped.setdefault(key, row)
    return [deduped[key] for key in sorted(deduped)]


def rebuild_manifest(
    lake_root: Path, datasets: Iterable[Mapping[str, Any]], *, ledger_token: str
) -> bool:
    """Rewrite the whole manifest from a fresh UNFILTERED walk result — best-effort.

    *ledger_token* MUST be the token captured **BEFORE** the walk began: a
    vendor write landing DURING the walk then reads as stale on the very next
    :func:`load_manifest_datasets` (the pre-walk stamp can never claim
    freshness the walk did not have). The new row set is the UNION of the walk
    rows and any parseable rows already on disk (keep-first by coordinate) —
    the on-disk extras cover datasets a concurrent prebuild writer recorded
    AFTER the walk passed their directory (rows only ever enter via the
    validated writers or a walk, and every heal REMOVAL invalidates the whole
    manifest, so the union can only ever ADD writer-created truth, never
    resurrect a healed-away dataset).

    Returns ``True`` on success. Any failure logs, best-effort invalidates
    (a half-written manifest must never be servable), and returns ``False`` —
    NEVER raises (the manifest is an accelerator; the caller's walk result is
    already correct).
    """
    # Boundary flush (DEC-MANIFEST-LOG-FLOOD-001): a rebuild replaces the
    # manifest — close out the prior record burst's aggregate first.
    flush_record_summary(lake_root)
    now = datetime.now(tz=UTC).isoformat()
    try:
        with _manifest_lock(lake_root):
            merged: dict[tuple[str, str, str, str], str] = {}
            for row in datasets:
                key = (
                    str(row["exchange"]),
                    str(row["segment"]),
                    str(row["symbol"]),
                    str(row["interval"]),
                )
                as_row: dict[str, str] = dict(zip(_ROW_FIELDS, key, strict=True))
                merged.setdefault(key, _row_line(as_row, first_seen=now))
            with contextlib.suppress(OSError):
                existing = _parse_lines(manifest_data_path(lake_root).read_text(encoding="utf-8"))
                for old in existing or ():
                    key = (old["exchange"], old["segment"], old["symbol"], old["interval"])
                    merged.setdefault(key, _row_line(old, first_seen=now))
            lines = [merged[key] for key in sorted(merged)]
            body = "".join(f"{line}\n" for line in lines)
            data_path = manifest_data_path(lake_root)
            data_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_simple(data_path, lambda tmp: tmp.write_text(body, encoding="utf-8"))
            _write_meta(lake_root, ledger_token=ledger_token, rows=len(lines))
    except (OSError, PartitionLockTimeout, KeyError, TypeError, ValueError) as exc:
        _LOG.warning("lake_manifest.rebuild_failed", error_type=type(exc).__name__, error=str(exc))
        _unlink_manifest(lake_root)
        return False
    _LOG.info("lake_manifest.rebuilt", rows=len(lines), ledger_token=ledger_token)
    return True


def record_dataset(
    lake_root: Path, *, exchange: str, segment: str, symbol: str, interval: str
) -> bool:
    """Append one NEW dataset row + re-stamp the meta — O(1), best-effort.

    Called by the lake writer's fresh branch when a write CREATES a dataset
    (its interval dir held zero ``*.parquet`` before the write). Under the
    manifest flock: read the tiny meta (absent → **no-op** ``False``: appends
    are only ever layered onto a base a full walk built, so a missing manifest
    can never accrete a silently-partial row set), append ONE JSONL line
    (flushed + fsynced), then atomically rewrite the meta with ``rows + 1`` and
    the CURRENT ledger token (the post-pull prebuild's records converge the
    manifest to a served-hot state — see the module docstring). An
    existing-partition append never reaches this function at all — zero
    manifest cost on the hot bar-append path.

    Any failure logs + best-effort invalidates + returns ``False`` — never
    raises into the pull.

    Success telemetry is per-dataset at DEBUG plus the aggregate
    ``lake_manifest.record_summary`` INFO (DEC-MANIFEST-LOG-FLOOD-001 —
    see the module docstring's log-volume envelope): recording N datasets
    costs a BOUNDED number of INFO lines, never N.
    """
    try:
        with _manifest_lock(lake_root):
            meta = _read_meta(lake_root)
            if meta is None:
                _LOG.debug(
                    "lake_manifest.record_skipped_no_base",
                    symbol=symbol,
                    interval=interval,
                )
                return False
            data_path = manifest_data_path(lake_root)
            if not data_path.exists():
                _LOG.warning("lake_manifest.data_missing_with_meta")
                _unlink_manifest(lake_root)
                return False
            line = _row_line(
                {"exchange": exchange, "segment": segment, "symbol": symbol, "interval": interval},
                first_seen=datetime.now(tz=UTC).isoformat(),
            )
            with data_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{line}\n")
                fh.flush()
                os.fsync(fh.fileno())
            token = ledger_stat_token(lake_root)
            _write_meta(
                lake_root,
                # An errored token probe (None) keeps the PRIOR stamp — honest:
                # the manifest stays valid only while that older state holds.
                ledger_token=meta["ledger_token"] if token is None else token,
                rows=meta["rows"] + 1,
            )
    except (OSError, PartitionLockTimeout) as exc:
        _LOG.warning(
            "lake_manifest.record_failed",
            symbol=symbol,
            interval=interval,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        _unlink_manifest(lake_root)
        return False
    # Per-dataset success telemetry at DEBUG (DEC-MANIFEST-LOG-FLOOD-001):
    # the prebuild records ~10⁵ datasets per run — at INFO these lines
    # (94,190 at ~190/s, measured 2026-07-05) rotated away the
    # prebuild-completion audit trail, the exact DEC-PREBUILD-LOG-FLOOD-001
    # class. Same event name + fields; the INFO record is the aggregate
    # `lake_manifest.record_summary` (see the module docstring).
    _LOG.debug(
        "lake_manifest.dataset_recorded",
        exchange=exchange,
        segment=segment,
        symbol=symbol,
        interval=interval,
    )
    _note_dataset_recorded(
        lake_root, exchange=exchange, segment=segment, symbol=symbol, interval=interval
    )
    return True


def _unlink_manifest(lake_root: Path) -> None:
    """Remove meta THEN data, best-effort (meta-first: a torn removal fails closed)."""
    for path in (manifest_meta_path(lake_root), manifest_data_path(lake_root)):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            _LOG.warning(
                "lake_manifest.unlink_failed",
                path=str(path),
                error_type=type(exc).__name__,
                error=str(exc),
            )


def invalidate_manifest(lake_root: Path) -> None:
    """Delete the manifest so the next discovery walks + rebuilds — best-effort.

    Called by every lake path that REMOVES stored files (the heal quarantine
    move + the intraday-TF delete-and-rebuild — DEC-LAKE-HEAL-001), because a
    removal can empty a dataset and an incremental row-delete would need the
    very walk this module exists to avoid. Removal is meta-FIRST so any torn
    state fails closed (a data file without a meta never validates). Prefers
    the manifest flock; if the lock itself cannot be taken the unlink still
    proceeds (an interleaved append then fails the row-count check — safe).
    Idempotent, O(1), never raises.
    """
    # Boundary flush (DEC-MANIFEST-LOG-FLOOD-001): an invalidation ends the
    # manifest's life — close out the pending record aggregate first.
    flush_record_summary(lake_root)
    try:
        with _manifest_lock(lake_root):
            _unlink_manifest(lake_root)
    except (OSError, PartitionLockTimeout) as exc:
        _LOG.warning(
            "lake_manifest.invalidate_lock_failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        _unlink_manifest(lake_root)


__all__ = [
    "ABSENT_LEDGER_TOKEN",
    "MANIFEST_DATA_FILENAME",
    "MANIFEST_DIRNAME",
    "MANIFEST_META_FILENAME",
    "MANIFEST_VERSION",
    "RECORD_SUMMARY_INTERVAL_S",
    "flush_record_summary",
    "invalidate_manifest",
    "ledger_file_path",
    "ledger_stat_token",
    "load_manifest_datasets",
    "manifest_data_path",
    "manifest_dir",
    "manifest_meta_path",
    "rebuild_manifest",
    "record_dataset",
]
